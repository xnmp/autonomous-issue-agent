"""Steward triage workflow dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import sqlite3

from ...adapters.github import GhUnavailable
from ...adapters.runtime_plan import AgentRuntime, ContextualRuntime
from ...adapters.effects import commands_for_steward_verdict
from ...domain import continuation_components as cc
from ...domain import triage as triage_dom
from ...domain import steward as steward_dom
from ...domain.events import (
    Correlation,
    IssueGroomed,
    LevelChanged,
    RecurrenceDetected,
    TelemetryEvent,
    TriageDispositionRecorded,
)
from ...domain.labels import Label
from ...domain.lifecycle import (
    AllowedEligibility, LifecycleOperation, completion_guard, completion_matches,
    issue_eligibility,
)
from ...domain.leg import Leg, LegTarget
from ...domain.outcome import AgentResult, MAX_BUDGET_STATUS, budget_exhausted
from ...domain.steward_stall import (
    DEFAULT_STEWARD_ERROR_CEILING,
    stall_charge,
    steward_stall_actions,
    steward_stall_tripped,
)
from ...domain.ports import (
    ActionExecutor,
    ControlStore,
    EventSink,
    RecentMergesReads,
    WorkflowGitHubReads,
    WorkflowState,
)
from ...domain.triage import TriageConfig
from ...domain.triage import TriageVerdict
from ...domain.world import IssueView
from ...domain.workflow_identity import IssueRef, WorkflowKey
from ...domain.workflow_ports import StewardControl
from ...domain.trust import as_untrusted
from ..planners import tasks as task_builders
from ..planners.tasks import GroomTaskConfig, TriageTaskConfig
from .base import run_leg
from .completion import execute_issue_completion


@dataclass(frozen=True)
class StewardWorkflowConfig:
    triage: TriageTaskConfig | None
    triage_cfg: TriageConfig | None
    log: Callable[[str], None]
    groom: GroomTaskConfig | None = None
    # Fixer login — whose merged PRs form the "already shipped by this harness"
    # reference set the recurrence check matches against (harness #10). Blank →
    # no reference set → the check is inert and triage is unchanged.
    fix_bot: str = ""
    # Repo slug, for the recurrence event's dedup key only (a journal is per-role,
    # not per-repo-per-role, and the key must not collide across targets).
    repo: str = ""
    # Where recurrence events land. Optional so every existing caller/test keeps
    # working; None → the label + comment still happen, only the metric is lost.
    journal: EventSink | None = None
    dedupe: ControlStore | None = None
    # ADR 0012: how many consecutive verdict-less paid legs one version of one
    # issue may buy before the steward stops and asks an operator.
    error_ceiling: int = DEFAULT_STEWARD_ERROR_CEILING
    owner_id: str = ""
    config_version: str = ""


def _text(value: object) -> str:
    """An agent-supplied field as a string, or "" for every other shape."""
    return value if isinstance(value, str) else ""


def _merge_lines(merged) -> str:
    """The merged-issue reference set, rendered for the prompt. One line per
    issue, newest first — the order `gh pr list` already returns."""
    return "\n".join(
        f"- #{m.issue} {m.title} (PR #{m.pr}, merged {m.merged_at})" for m in merged
    )


@dataclass
class StewardWorkflow:
    cfg: StewardWorkflowConfig
    gateway: WorkflowGitHubReads
    store: WorkflowState
    runner: AgentRuntime
    executor: ActionExecutor
    # `block_key`s this process has already journalled — the in-memory half of the
    # recurrence edge trigger. The journal read below is the half that survives a
    # daemon restart; this one keeps the steady state free of file reads.
    _emitted: set[str] = field(default_factory=set)

    def _recent_merges(self) -> tuple:
        """The harness's recently-merged issues, or () if that cannot be answered.

        Deliberately total: a gateway that lacks the method (an injected fake, a
        legacy adapter) or one that raises must leave triage EXACTLY as it was
        before this feature — no reference set, no prompt section, no recurrence.
        Same contract as `FixWorkflow._prompt_input`: a context fetch may never
        kill the leg it was only meant to enrich.
        """
        if not isinstance(self.gateway, RecentMergesReads) or not self.cfg.fix_bot:
            return ()
        try:
            return tuple(self.gateway.recently_merged_issues(self.cfg.fix_bot) or ())
        except Exception as exc:  # noqa: BLE001 — see docstring
            self.cfg.log(f"recurrence context unavailable: {exc}")
            return ()

    def _journal_recurrence(self, issue: int, of_issue: int, pr: int) -> None:
        """One `recurrence` event per (repo, issue, original), ever. Deduped in
        process AND through the journal, because the daemon restarts (hot reload,
        Restart=always) that reset the in-memory set do not reset the file."""
        key = f"recurrence:{self.cfg.repo}:{issue}:refiled:{of_issue}"
        if key in self._emitted:
            return
        self._emitted.add(key)
        journal = self.cfg.journal
        if journal is None:
            return
        dedupe = self.cfg.dedupe
        if dedupe is not None and not dedupe.claim(
            "recurrence", self.cfg.repo, key
        ):
            return
        journal.emit(
            TelemetryEvent(
                RecurrenceDetected("refiled", of_issue, key),
                Correlation(issue=issue, pr=(pr or None)),
            )
        )

    def _journal_groomed(self, issue: int, kind: str, *, ref: str = "", pr: int = 0) -> None:
        key = f"groomed:{self.cfg.repo}:{issue}:{kind}:{ref or pr}"
        if key in self._emitted:
            return
        self._emitted.add(key)
        journal = self.cfg.journal
        if journal is None:
            return
        dedupe = self.cfg.dedupe
        if dedupe is not None and not dedupe.claim(
            "groomed", self.cfg.repo, key
        ):
            return
        fields = {"kind": kind, "issue": issue, "block_key": key}
        if ref:
            fields["reference"] = ref
        if pr:
            fields["pr"] = pr
        journal.emit(
            TelemetryEvent(
                IssueGroomed(key, kind, fields),
                Correlation(issue=issue, pr=(pr or None)),
            )
        )

    def _journal_groom_disposition(
        self, issue: int, target: int, outcome: str
    ) -> None:
        """Record a harness-adjudicated GROOM lifecycle claim.

        ``outcome`` is always minted from closed enums or observed issue state;
        agent prose is intentionally excluded from the journal authority rail.
        """
        key = f"groomed:{self.cfg.repo}:{issue}:duplicate_of:{target}:{outcome}"
        if key in self._emitted:
            return
        self._emitted.add(key)
        journal = self.cfg.journal
        if journal is None:
            return
        try:
            dedupe = self.cfg.dedupe
            if dedupe is not None and not dedupe.claim(
                "groomed", self.cfg.repo, key
            ):
                return
            journal.emit(
                TelemetryEvent(
                    IssueGroomed(
                        key,
                        "duplicate_of",
                        {
                            "kind": "duplicate_of",
                            "issue": issue,
                            "duplicate_of": target,
                            "outcome": outcome,
                            "block_key": key,
                        },
                    ),
                    Correlation(issue=issue),
                )
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never blocks effects
            self.cfg.log(
                f"groom #{issue}: duplicate disposition not journalled ({exc})"
            )

    def _journal_triage_close(self, issue: int, kind: str, detail: str) -> None:
        """One event per (repo, issue, disposition) for a triage `close` verdict.

        `detail` is HARNESS-minted (a normalized stateReason, or the token naming
        why a close was refused) — never the classifier's close_reason prose,
        which is agent-authored and reads attacker-writable issue text.
        """
        key = f"triage_close:{self.cfg.repo}:{issue}:{kind}"
        if key in self._emitted:
            return
        self._emitted.add(key)
        journal = self.cfg.journal
        if journal is None:
            return
        dedupe = self.cfg.dedupe
        if dedupe is not None and not dedupe.claim("triage_close", self.cfg.repo, key):
            return
        journal.emit(
            TelemetryEvent(
                IssueGroomed(key, kind, {"issue": issue, "detail": detail}),
                Correlation(issue=issue),
            )
        )

    def _journal_triage_disposition(self, issue: int, *, blocked: bool) -> None:
        """Record the steward state only after its remote disposition lands."""
        journal = self.cfg.journal
        if journal is None:
            return
        journal.emit(
            TelemetryEvent(
                TriageDispositionRecorded(
                    "blocked_on_human_reply" if blocked else "processed"
                ),
                Correlation(issue=issue),
            )
        )

    def _journal_steward_level(self, issue: int, state: str, reason: str,
                               *, lane: cc.StewardLane = cc.StewardLane.TRIAGE, **attributes) -> None:
        if self.cfg.journal is not None:
            self.cfg.journal.emit(TelemetryEvent(
                LevelChanged(f"issue:{issue}:steward:{lane.value}", state, reason, {"leg": lane.value, **attributes}),
                Correlation(issue=issue),
            ))

    def _groomed(self, issue: int, kind: str, *, ref: str = "", pr: int = 0) -> bool:
        key = f"groomed:{self.cfg.repo}:{issue}:{kind}:{ref or pr}"
        if key in self._emitted:
            return True
        # Durable effects are independently idempotent. Do not consume the
        # telemetry dedupe key before the remote advisory write succeeds.
        return False

    async def groom(self, issue: int) -> None:
        """Classify an issue once per content version and apply safe findings."""
        if self.cfg.groom is None:
            return
        control = self.cfg.dedupe
        if not isinstance(control, StewardControl) or not self.cfg.owner_id or not isinstance(self.runner, ContextualRuntime):
            self.cfg.log(f"groom #{issue}: durable steward authority is unavailable")
            return
        selection = control.read_steward(WorkflowKey(self.cfg.repo, IssueRef(issue)))
        if not isinstance(selection, cc.SelectedSteward):
            self.cfg.log(f"groom #{issue}: steward component is inactive or blocked")
            return
        lane = selection.component.state.groom
        if lane.pending_delivery:
            self.executor.replay_pending()
            if lane.delivery_source is not None:
                control.complete_steward_delivery(lane.delivery_source, self.gateway.get_issue(issue))
            return
        iss = self.gateway.get_issue(issue)
        signature = triage_dom.grooming_content(iss)
        try:
            prospective = cc.consume_steward(selection, cc.StewardPurchase(
                cc.ContinuationCapture(selection.binding, selection.revision, "groom:preflight"),
                cc.StewardLane.GROOM, iss.updated_at, self.cfg.error_ceiling, signature), selection.revision.version + 1)
        except ValueError:
            self.cfg.log(f"groom #{issue}: selected continuity refuses admission")
            return
        selected = cc.StewardInput(selection, cc.StewardLane.GROOM, iss.updated_at, self.cfg.error_ceiling, signature)
        self.runner.set_context("steward", "GROOM", issue, issue, workflow=selection.binding)
        merged = self._recent_merges()
        result = await run_leg(
            self.runner,
            self.cfg.log,
            Leg(
                "GROOM",
                LegTarget.ISSUE,
                issue,
                True,
                ("global", "repository", "issue"),
            ),
            replace(task_builders.build_groom_task(
                issue,
                as_untrusted(iss.title),
                as_untrusted(iss.body),
                self.cfg.groom,
                as_untrusted(_merge_lines(merged)),
            ), steward_input=selected),
        )
        if result.workflow_attempt is None:
            self.cfg.log(f"groom #{issue}: no admitted attempt returned")
            return
        source = control.steward_admission(result.workflow_attempt)
        if result.is_error or not isinstance(result.structured, dict):
            charged = stall_charge(result.exit_reason, result.envelope_subtype)
            self._record_steward_failure(source, iss, prospective.state.groom.stalls + charged,
                charged=charged, budget=budget_exhausted(result.exit_reason) or MAX_BUDGET_STATUS in (result.status, result.envelope_subtype),
                detail=result.envelope_subtype or (result.exit_reason.value if result.exit_reason else "error"))
            return
        fresh = self.gateway.get_issue(issue)
        if (not completion_matches(completion_guard(iss, LifecycleOperation.GROOM), fresh)
                or not isinstance(issue_eligibility(fresh, LifecycleOperation.GROOM), AllowedEligibility)):
            control.record_steward_outcome(cc.StewardFailed(source, cc.StewardFailureKind.PAID))
            return
        iss = fresh
        raw = result.structured
        claim = steward_dom.parse_groom_disposition(raw.get("disposition"))
        duplicate_decision: steward_dom.DuplicateDecision | None = None
        if claim is not None:
            promote = bool(self.cfg.triage_cfg and self.cfg.triage_cfg.promote)
            referenced = None
            if promote and claim.issue != issue:
                try:
                    referenced = self.gateway.get_issue(claim.issue)
                except GhUnavailable:
                    self._record_steward_failure(source, iss, prospective.state.groom.stalls + 1,
                        charged=1, budget=False, detail="validation_read_failed")
                    return
                except (AttributeError, KeyError, TypeError, ValueError):
                    # A benign "not found" read is evidence that the claim does
                    # not meet the existence gate, not a daemon failure.
                    referenced = None
            duplicate_decision = steward_dom.validate_duplicate_of(
                iss, claim, referenced, promote=promote
            )
        resolved_by = raw.get("resolved_by")
        resolved_by = resolved_by if isinstance(resolved_by, int) and not isinstance(resolved_by, bool) else None
        stale_reference = raw.get("stale_reference")
        stale_reference = stale_reference if isinstance(stale_reference, str) else ""
        merged_by_issue = {m.issue: m.pr for m in merged}
        if resolved_by is not None and self._groomed(
            issue, "resolved_by", pr=merged_by_issue.get(resolved_by, 0)
        ):
            resolved_by = None
        if stale_reference.strip() and self._groomed(issue, "stale", ref=stale_reference.strip()):
            stale_reference = ""
        # The stale reference must be an exact substring of the issue's data as
        # well as a result from the rg-directed classifier. That containment
        # prevents a malformed verdict from inserting an unrelated repo claim.
        if stale_reference.strip() and stale_reference.strip() not in iss.body:
            stale_reference = ""
        action_issue = iss
        if isinstance(duplicate_decision, steward_dom.ValidatedDuplicateOf):
            # The paid result authorizes nothing by itself. Refresh both sides
            # at the last possible point so a reopen or lifecycle transition
            # during the leg cannot be overwritten by the close batch.
            try:
                action_issue = self.gateway.get_issue(issue)
                action_reference = self.gateway.get_issue(claim.issue)
                if (action_issue.title, action_issue.body, action_issue.author, action_issue.comments) != (
                    iss.title, iss.body, iss.author, iss.comments
                ):
                    self.cfg.log(f"groom #{issue}: duplicate closure deferred; source facts changed")
                    control.record_steward_outcome(cc.StewardFailed(source, cc.StewardFailureKind.PAID))
                    return
            except GhUnavailable:
                self._record_steward_failure(source, iss, prospective.state.groom.stalls + 1,
                    charged=1, budget=False, detail="revalidation_read_failed")
                return
            except (AttributeError, KeyError, TypeError, ValueError):
                action_reference = None
            duplicate_decision = steward_dom.validate_duplicate_of(
                action_issue,
                claim,
                action_reference,
                promote=bool(self.cfg.triage_cfg and self.cfg.triage_cfg.promote),
            )
        if isinstance(duplicate_decision, steward_dom.ValidatedDuplicateOf):
            self._journal_groom_disposition(
                issue, duplicate_decision.issue, "accepted"
            )
        elif isinstance(duplicate_decision, steward_dom.RefusedDuplicateOf):
            self._journal_groom_disposition(
                issue, duplicate_decision.issue, duplicate_decision.refusal.value
            )
        if isinstance(duplicate_decision, steward_dom.ValidatedDuplicateOf):
            triage_label = (
                self.cfg.triage_cfg.triage_label
                if self.cfg.triage_cfg is not None
                else Label.AI_TRIAGE
            )
            actions = list(steward_dom.duplicate_close_actions(
                action_issue, duplicate_decision, triage_label=triage_label
            ))
        else:
            actions = triage_dom.grooming_actions(
                issue, resolved_by=resolved_by, stale_reference=stale_reference,
                merged_by_issue=merged_by_issue,
            )
        if not actions:
            control.record_steward_outcome(cc.GroomNoFinding(source))
            return
        commands = commands_for_steward_verdict(tuple(actions), source,
            completion_guard(iss, LifecycleOperation.CLOSE if isinstance(duplicate_decision, steward_dom.ValidatedDuplicateOf)
                             else LifecycleOperation.GROOM),
            identity=self.cfg.owner_id, writer_login=self.cfg.groom.steward_bot, config_version=self.cfg.config_version)
        try:
            if not control.record_steward_outcome(cc.StewardVerdict(source, tuple(str(command.command_id) for command in commands)), commands):
                return
        except sqlite3.OperationalError:
            self._record_steward_failure(source, iss, prospective.state.groom.stalls + 1,
                charged=1, budget=False, detail="write_not_queued")
            return
        self.executor.replay_pending()
        if not control.complete_steward_delivery(source, self.gateway.get_issue(issue)):
            self._journal_steward_level(issue, "deferred", "durable_outbox", lane=cc.StewardLane.GROOM)
            return
        delivered = control.read_steward(selection.binding.fence.key)
        if not isinstance(delivered, cc.SelectedSteward) or delivered.component.state.groom.stalls:
            return
        if (
            not isinstance(duplicate_decision, steward_dom.ValidatedDuplicateOf)
            and resolved_by is not None
            and resolved_by in merged_by_issue
            and resolved_by != issue
        ):
            self._journal_groomed(issue, "resolved_by", pr=merged_by_issue[resolved_by])
        if not isinstance(
            duplicate_decision, steward_dom.ValidatedDuplicateOf
        ) and stale_reference.strip():
            self._journal_groomed(issue, "stale", ref=stale_reference.strip())

    def _record_steward_failure(self, source: cc.StewardAttemptSource, view: IssueView, stalls: int,
                               *, charged: int, budget: bool, detail: str) -> None:
        control = self.cfg.dedupe
        assert isinstance(control, StewardControl)
        issue = view.number
        leg = source.lane.value
        if charged and steward_stall_tripped(stalls, self.cfg.error_ceiling):
            actions = tuple(steward_stall_actions(issue, leg, stalls, budget=budget, detail=detail))
            operation = LifecycleOperation.GROOM if source.lane is cc.StewardLane.GROOM else LifecycleOperation.COMPLETE_TRIAGE
            writer = self.cfg.groom.steward_bot if source.lane is cc.StewardLane.GROOM and self.cfg.groom else self.cfg.triage_cfg.steward_bot if self.cfg.triage_cfg else ""
            commands = commands_for_steward_verdict(actions, source, completion_guard(view, operation),
                identity=self.cfg.owner_id, writer_login=writer,
                config_version=self.cfg.config_version)
            recorded = control.record_steward_outcome(cc.StewardStopped(source, tuple(str(command.command_id) for command in commands)), commands)
            if recorded:
                self._journal_steward_level(issue, "stalled", "budget_ceiling" if budget else "error_ceiling",
                    lane=source.lane, attempts=stalls, ceiling=self.cfg.error_ceiling, status=detail)
            self.executor.replay_pending()
            try:
                after = self.gateway.get_issue(issue)
            except Exception as exc:
                self.cfg.log(f"{leg} #{issue}: stopped delivery remains owed ({exc})")
                return
            control.complete_steward_delivery(source, after)
        else:
            control.record_steward_outcome(cc.StewardFailed(source,
                cc.StewardFailureKind.PAID if charged else cc.StewardFailureKind.INFRASTRUCTURE))

    async def triage(self, issue: int) -> None:
        iss = self.gateway.get_issue(issue)
        control = self.cfg.dedupe
        if not isinstance(control, StewardControl) or not self.cfg.owner_id or not isinstance(self.runner, ContextualRuntime):
            self.cfg.log(f"triage #{issue}: durable steward authority is unavailable")
            return
        selection = control.read_steward(WorkflowKey(self.cfg.repo, IssueRef(issue)))
        if not isinstance(selection, cc.SelectedSteward):
            self.cfg.log(f"triage #{issue}: steward component is inactive or blocked")
            return
        lane = selection.component.state.triage
        if lane.pending_delivery:
            self.executor.replay_pending()
            if lane.delivery_source is not None:
                completed = control.complete_steward_delivery(lane.delivery_source, self.gateway.get_issue(issue))
                if completed:
                    settled = control.read_steward(selection.binding.fence.key)
                    if (isinstance(settled, cc.SelectedSteward) and lane.stalls < self.cfg.error_ceiling
                            <= settled.component.state.triage.stalls):
                        self._journal_steward_level(issue, "stalled", "error_ceiling",
                            attempts=settled.component.state.triage.stalls, ceiling=self.cfg.error_ceiling, status="write_retired")
            return
        try:
            prospective = cc.consume_steward(selection, cc.StewardPurchase(
                cc.ContinuationCapture(selection.binding, selection.revision, "triage:preflight"),
                cc.StewardLane.TRIAGE, iss.updated_at, self.cfg.error_ceiling), selection.revision.version + 1)
        except ValueError:
            self.cfg.log(f"triage #{issue}: selected continuity refuses admission")
            return
        selected = cc.StewardInput(selection, cc.StewardLane.TRIAGE, iss.updated_at, self.cfg.error_ceiling)
        self.runner.set_context("steward", "TRIAGE", issue, issue, workflow=selection.binding)
        self.cfg.log(f"triage #{issue}: classifying")
        thread = "\n\n---\n\n".join(f"@{c.author}: {c.body}" for c in iss.comments)
        merged = self._recent_merges()
        task = task_builders.build_triage_task(
            issue,
            as_untrusted(iss.title),
            as_untrusted(iss.body),
            as_untrusted(thread),
            self.cfg.triage,
            recent_merges=as_untrusted(_merge_lines(merged)),
            issue_state=iss.state,
            state_reason=iss.state_reason,
            issue_author=iss.author,
        )
        task = replace(task, steward_input=selected)
        result = await run_leg(
            self.runner,
            self.cfg.log,
            Leg(
                "TRIAGE",
                LegTarget.ISSUE,
                issue,
                True,
                ("global", "repository", "issue"),
            ),
            task,
        )
        if result.workflow_attempt is None:
            self.cfg.log(f"triage #{issue}: no admitted attempt returned")
            return
        source = control.steward_admission(result.workflow_attempt)
        if result.is_error or not isinstance(result.structured, dict):
            charged = stall_charge(result.exit_reason, result.envelope_subtype)
            stalls = prospective.state.triage.stalls + charged
            detail = result.envelope_subtype or (result.exit_reason.value if result.exit_reason else "error")
            self._record_steward_failure(source, iss, stalls, charged=charged,
                budget=budget_exhausted(result.exit_reason) or MAX_BUDGET_STATUS in (result.status, result.envelope_subtype), detail=detail)
            return
        fresh = self.gateway.get_issue(issue)
        if not completion_matches(completion_guard(iss, LifecycleOperation.COMPLETE_TRIAGE), fresh) or not isinstance(
            issue_eligibility(fresh, LifecycleOperation.COMPLETE_TRIAGE), AllowedEligibility
        ):
            self.cfg.log(f"triage #{issue}: completion deferred; decision facts changed or issue held")
            control.record_steward_outcome(cc.StewardFailed(source, cc.StewardFailureKind.PAID))
            return
        iss = fresh
        s = result.structured
        verdict = TriageVerdict(
            # `or ""` throughout, not `get(k, "")`: under strict structured
            # output an optional field arrives as an explicit JSON null rather
            # than being absent, and a null must read exactly like absent.
            verdict=s.get("verdict") or "",
            plain_summary=s.get("plain_summary") or "",
            acceptance_criteria=tuple(s.get("acceptance_criteria", []) or ()),
            creator_questions=tuple(s.get("creator_questions", []) or ()),
            owner_questions=tuple(s.get("owner_questions", []) or ()),
            blocked_by_issues=tuple(
                int(n) for n in (s.get("blocked_by_issues", []) or []) if isinstance(n, int)
            ),
            rationale=s.get("rationale") or "",
            # ints only (bool is an int subclass and is NOT a number here); every
            # other shape reads as "no claim". The number is further checked
            # against `merged_by_issue` in the domain, which is what stops the
            # classifier pointing the label at an arbitrary issue.
            recurrence_of=(
                s["recurrence_of"]
                if isinstance(s.get("recurrence_of"), int)
                and not isinstance(s.get("recurrence_of"), bool)
                else None
            ),
            # Strings only. Structured output is enforced provider-side, so a
            # non-conforming runtime can hand back a list or a number here; a
            # close is the one disposition where a TypeError deep in the domain
            # would abort a leg that had already been paid for.
            close_reason=_text(s.get("close_reason")),
            reopen_if=_text(s.get("reopen_if")),
            close_state_reason=_text(s.get("close_state_reason")),
        )
        promote = bool(self.cfg.triage_cfg and self.cfg.triage_cfg.promote)
        # Mirrors `triage.actions_for_verdict`'s close gate, so the log line, the
        # journal and the actions can never disagree about what happened.
        closing = (
            promote
            and verdict.is_close
            and iss.state_reason.strip().casefold() != "reopened"
        )
        if verdict.verdict == "close":
            disp = (
                f"close ({verdict.close_state})" if closing
                else "close refused — handed to a maintainer"
            )
        elif verdict.is_blocked:
            disp = (
                f"blocked ({len(verdict.creator_questions)} creator + "
                f"{len(verdict.owner_questions)} owner question(s)"
                f"{'; needs owner' if verdict.needs_owner else ''})"
            )
        else:
            disp = (
                "ready → ai-ready" if (promote and not verdict.blocked_by_issues)
                else "ready (awaiting human promote)"
            )
        if verdict.blocked_by_issues and verdict.verdict != "close":
            disp += f"; blocked-by {sorted(verdict.blocked_by_issues)}"
        merged_by_issue = {m.issue: m.pr for m in merged}
        # The recurrence marker asks a human to judge an OPEN issue. A close
        # already states its own disposition, so the two would contradict each
        # other on the same thread; the close comment names the merged PR itself.
        rec = (
            [] if closing
            else triage_dom.recurrence_actions(issue, verdict, merged_by_issue)
        )
        if rec:
            disp += f"; possible recurrence of #{verdict.recurrence_of}"
        self.cfg.log(f"triage #{issue}: {disp}")
        actions = tuple([
                *triage_dom.actions_for_verdict(
                    issue,
                    iss.author,
                    verdict,
                    self.cfg.triage_cfg,
                    state_reason=iss.state_reason,
                    labels=iss.labels,
                ),
                *rec,
            ])
        if not actions:
            control.record_steward_outcome(cc.StewardFailed(source, cc.StewardFailureKind.PAID))
            return
        commands = commands_for_steward_verdict(actions, source,
            completion_guard(iss, LifecycleOperation.CLOSE if closing else LifecycleOperation.COMPLETE_TRIAGE),
            identity=self.cfg.owner_id, writer_login=self.cfg.triage_cfg.steward_bot if self.cfg.triage_cfg else "",
            config_version=self.cfg.config_version)
        try:
            if not control.record_steward_outcome(cc.StewardVerdict(source, tuple(str(command.command_id) for command in commands)), commands):
                return
        except sqlite3.OperationalError:
            # Atomic publication rolled back; only this settled attempt is lost.
            self._record_steward_failure(source, iss, prospective.state.triage.stalls + 1,
                charged=1, budget=False, detail="write_not_queued")
            return
        self.executor.replay_pending()
        after = self.gateway.get_issue(issue)
        if not control.complete_steward_delivery(source, after):
            self.cfg.log(f"triage #{issue}: verdict delivery remains owed")
            self._journal_steward_level(issue, "deferred", "durable_outbox")
            if closing:
                self._journal_triage_close(issue, "close_unconfirmed", verdict.close_state)
            return
        delivered = control.read_steward(source.admission.attempt.binding.fence.key)
        if not isinstance(delivered, cc.SelectedSteward) or delivered.component.state.triage.stalls:
            self.cfg.log(f"triage #{issue}: verdict was retired without verified delivery")
            return
        self._journal_triage_disposition(issue, blocked=verdict.is_blocked)
        # After the actions, so a journalled recurrence always has its label and
        # comment behind it — and only ever for a recurrence the DOMAIN validated.
        if rec:
            of = verdict.recurrence_of or 0
            self._journal_recurrence(issue, of, merged_by_issue.get(of, 0))
        if closing:
            # `execute_all` reports "no action raised", not "every remote write
            # landed" — a durable command retired as a permanent failure returns
            # False and is swallowed. The close metric must never claim a close
            # the remote never took, so it is journalled from the OBSERVED state.
            self._journal_triage_close(
                issue,
                "closed" if after.state == "closed" else "close_unconfirmed",
                verdict.close_state,
            )
        elif verdict.verdict == "close":
            self._journal_triage_close(
                issue,
                "close_declined",
                "promote_off" if not promote
                else ("reopened" if verdict.is_close else "incomplete_verdict"),
            )
