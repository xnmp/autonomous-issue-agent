"""Steward triage workflow dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ...domain import triage as triage_dom
from ...adapters.runtime_plan import AgentRuntime
from ...domain.events import (
    Correlation,
    IssueGroomed,
    LevelChanged,
    RecurrenceDetected,
    TelemetryEvent,
)
from ...domain.outcome import AgentResult, MAX_BUDGET_STATUS, budget_exhausted
from ...domain.steward_stall import (
    DEFAULT_STEWARD_ERROR_CEILING,
    stall_charge,
    steward_stall_actions,
    steward_stall_tripped,
)
from ...domain.leg import Leg, LegTarget
from ...domain.contracts import EffectStatus
from ...domain.ports import (
    ActionExecutor,
    ControlStore,
    EventSink,
    PendingEffectReads,
    RecentMergesReads,
    WorkflowGitHubReads,
    WorkflowState,
)
from ...domain.triage import TriageConfig
from ...domain.triage import TriageVerdict
from ...domain.world import IssueView
from ...domain.trust import as_untrusted
from ..planners import tasks as task_builders
from ..planners.tasks import GroomTaskConfig, TriageTaskConfig
from .base import run_leg


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

    def _groomed(self, issue: int, kind: str, *, ref: str = "", pr: int = 0) -> bool:
        key = f"groomed:{self.cfg.repo}:{issue}:{kind}:{ref or pr}"
        if key in self._emitted:
            return True
        # Durable effects are independently idempotent. Do not consume the
        # telemetry dedupe key before the remote advisory write succeeds.
        return False

    def _record_baseline(self, issue: int, leg: str, fallback: IssueView) -> None:
        """Write the durable marker the leg's candidate predicate reads, so a
        stalled issue stops being re-selected. Re-read AFTER any escalation
        write when possible: a comment moves `updatedAt`, and recording the
        pre-comment value re-selects the issue once on the next sweep.

        When the re-read fails, the leg's ALREADY-FETCHED view is the baseline.
        Recording the pre-escalation timestamp costs at most one extra
        re-selection if the escalation comment landed; leaving no baseline at
        all buys a full paid leg every sweep with no ceiling — the exact class
        this module exists to close (adversarial review, 2026-08-02)."""
        try:
            updated = self.gateway.get_issue(issue)
        except Exception as exc:  # noqa: BLE001 — see docstring
            self.cfg.log(
                f"{leg} #{issue}: baseline re-read failed ({exc}); recording "
                "the leg's own view of the issue instead"
            )
            updated = fallback
        if leg == "groom":
            self.store.set_groom_at(issue, updated.updated_at)
            self.store.set_groom_content(issue, triage_dom.grooming_content(updated))
        else:
            self.store.set_triage_at(issue, updated.updated_at)

    def _deferred_to_outbox(self, issue: int) -> bool:
        """Whether this issue's writes are STILL OWED by the durable queue.

        `execute_all` returning False means "not confirmed", which covers two
        opposite worlds: the commands were committed and will be retried by the
        outbox without paying for another leg, or nothing was ever enqueued
        (the store was unreachable, the revision unreadable, no effect executor
        was wired). Only the second one loses the verdict, so only the second
        may charge a paid-leg ceiling.

        Fails CLOSED — no reader, an unreadable queue, or no matching row all
        answer "not deferred", which charges. Over-charging costs at most
        `error_ceiling` legs; under-charging is the unbounded loop.
        """
        store = self.cfg.dedupe
        if not isinstance(store, PendingEffectReads):
            return False
        target = f"issue:{issue}"
        try:
            pending = store.pending_commands()
        except Exception as exc:  # noqa: BLE001 — see docstring
            self.cfg.log(f"pending-effect read failed for #{issue}: {exc}")
            return False
        return any(
            entry.command.target == target
            and entry.state != EffectStatus.PERMANENT_FAILURE.value
            for entry in pending
        )

    def _write_unconfirmed(self, issue: int, leg: str, view: IssueView) -> None:
        """Tail for a leg whose verdict returned but whose writes did not
        confirm. Charges the paid-leg ceiling ONLY when the writes were never
        committed — see `_deferred_to_outbox`."""
        if not self._deferred_to_outbox(issue):
            self.cfg.log(
                f"{leg} #{issue}: verdict write was never queued — will retry"
            )
            self._stall(issue, leg, "write_not_queued", "write_not_queued",
                        view, budget=False)
            return
        self.cfg.log(
            f"{leg} #{issue}: verdict queued but not confirmed — delivery "
            "deferred to the durable outbox, not re-bought"
        )
        journal = self.cfg.journal
        if journal is not None:
            journal.emit(
                TelemetryEvent(
                    LevelChanged(
                        f"issue:{issue}:steward:{leg}",
                        "deferred",
                        "durable_outbox",
                        {"leg": leg},
                    ),
                    Correlation(issue=issue),
                )
            )
        # The paid work is done and the writes are owed by a queue that has its
        # own ADR 0012 ceiling (EFFECT_RETRY_CEILING). Record the baseline so no
        # further leg is bought, and end the stall series.
        self._record_baseline(issue, leg, view)
        self.store.clear_steward_stalls(issue, leg)

    def _charge_stall(
        self, issue: int, leg: str, result: AgentResult, view: IssueView
    ) -> None:
        """Account one paid leg that returned no usable verdict (ADR 0012).

        Below the ceiling this is exactly the previous behaviour — log and let
        the next sweep re-select the issue. At the ceiling the issue's baseline
        is recorded anyway, which is what stops the harness buying the same
        leg again on the same unchanged issue.
        """
        status = result.status or result.envelope_subtype or "error"
        if stall_charge(result.exit_reason, result.envelope_subtype) == 0:
            # An infra death made no attempt, and an admission refusal never
            # started one. The infra-death breaker (roles.decide) owns the
            # first; the second is a deployment condition, not a property of
            # this issue, and must never sideline it.
            self.cfg.log(
                f"{leg} #{issue}: {status} — no attempt was made, "
                "not charged to the stall ceiling"
            )
            return
        # `status` may be AGENT-AUTHORED on some paths (declared_status reads
        # the structured envelope): fine for the local log line above, but the
        # escalation body and journal are harness authority, and the revision
        # guard is disabled for them precisely because they carry no agent
        # output (ADR 0012). `detail` is therefore minted from harness-owned
        # fields only.
        detail = result.envelope_subtype or (
            result.exit_reason.value if result.exit_reason else "error"
        )
        self._stall(
            issue,
            leg,
            status,
            detail,
            view,
            budget=budget_exhausted(result.exit_reason)
            or MAX_BUDGET_STATUS in (result.status, result.envelope_subtype),
        )

    def _stall(
        self,
        issue: int,
        leg: str,
        status: str,
        detail: str,
        view: IssueView,
        *,
        budget: bool,
    ) -> None:
        """One charge against this issue's consecutive-failure ceiling."""
        ceiling = self.cfg.error_ceiling
        stalls = self.store.bump_steward_stalls(issue, leg)
        if not steward_stall_tripped(stalls, ceiling):
            self.cfg.log(
                f"{leg} #{issue}: {status} — no verdict, will retry "
                f"({stalls}/{ceiling})"
            )
            return
        self.cfg.log(
            f"{leg} #{issue}: {status} — {stalls} consecutive failures, "
            f"ceiling {ceiling} reached; no further paid {leg} legs for this "
            "version of the issue"
        )
        # Result deliberately unchecked: these are harness-owned signals minted
        # without a revision guard, so an unconfirmed one is committed to the
        # durable queue and retried there under EFFECT_RETRY_CEILING. Re-running
        # this trip to re-deliver them would buy another paid leg to say the
        # same thing — and would post a second comment, since the attempt count
        # in the body defeats the executor's content-level idempotence.
        self.executor.execute_all(
            steward_stall_actions(issue, leg, stalls, budget=budget, detail=detail)
        )
        journal = self.cfg.journal
        if journal is not None:
            journal.emit(
                TelemetryEvent(
                    LevelChanged(
                        f"issue:{issue}:steward:{leg}",
                        "stalled",
                        "budget_ceiling" if budget else "error_ceiling",
                        {
                            "leg": leg,
                            "attempts": stalls,
                            "ceiling": ceiling,
                            # Harness-minted token, never the agent-authored
                            # status string (see _charge_stall).
                            "status": detail,
                        },
                    ),
                    Correlation(issue=issue),
                )
            )
        # Order matters: the baseline is read after the escalation writes, and
        # the counter is reset so a human-edited issue starts a fresh series.
        # _record_baseline cannot fail (it falls back to the leg's own view),
        # so a trip always ends its series — no held state, no re-bought legs.
        self._record_baseline(issue, leg, view)
        self.store.clear_steward_stalls(issue, leg)

    async def groom(self, issue: int) -> None:
        """Classify an existing issue for one-shot, advisory queue cleanup."""
        if self.cfg.groom is None:
            return
        iss = self.gateway.get_issue(issue)
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
            task_builders.build_groom_task(
                issue,
                as_untrusted(iss.title),
                as_untrusted(iss.body),
                self.cfg.groom,
                as_untrusted(_merge_lines(merged)),
            ),
        )
        if result.is_error or not isinstance(result.structured, dict):
            self._charge_stall(issue, "groom", result, iss)
            return
        raw = result.structured
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
        actions = triage_dom.grooming_actions(
            issue, resolved_by=resolved_by, stale_reference=stale_reference,
            merged_by_issue=merged_by_issue,
        )
        if not self.executor.execute_all(actions):
            self._write_unconfirmed(issue, "groom", iss)
            return
        if resolved_by is not None and resolved_by in merged_by_issue and resolved_by != issue:
            self._journal_groomed(issue, "resolved_by", pr=merged_by_issue[resolved_by])
        if stale_reference.strip():
            self._journal_groomed(issue, "stale", ref=stale_reference.strip())
        # Record after writes because comments update GitHub's timestamp. That
        # makes an unchanged issue one-shot while a later human edit reopens the
        # advisory scan without relying on process memory.
        updated = self.gateway.get_issue(issue)
        self.store.set_groom_at(issue, updated.updated_at)
        self.store.set_groom_content(issue, triage_dom.grooming_content(updated))
        self.store.clear_steward_stalls(issue, "groom")

    async def triage(self, issue: int) -> None:
        self.cfg.log(f"triage #{issue}: classifying")
        iss = self.gateway.get_issue(issue)
        thread = "\n\n---\n\n".join(f"@{c.author}: {c.body}" for c in iss.comments)
        merged = self._recent_merges()
        task = task_builders.build_triage_task(
            issue,
            as_untrusted(iss.title),
            as_untrusted(iss.body),
            as_untrusted(thread),
            self.cfg.triage,
            recent_merges=as_untrusted(_merge_lines(merged)),
        )
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
        if result.is_error or not isinstance(result.structured, dict):
            self._charge_stall(issue, "triage", result, iss)
            return
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
        if not self.executor.execute_all(
            [
                *triage_dom.actions_for_verdict(
                    issue,
                    iss.author,
                    verdict,
                    self.cfg.triage_cfg,
                    state_reason=iss.state_reason,
                    labels=iss.labels,
                ),
                *rec,
            ]
        ):
            # Mirrors the groom path. Whether this costs another paid leg
            # depends on whether the writes are DURABLY OWED or were never
            # committed — `_write_unconfirmed` is the adjudication.
            self._write_unconfirmed(issue, "triage", iss)
            return
        # After the actions, so a journalled recurrence always has its label and
        # comment behind it — and only ever for a recurrence the DOMAIN validated.
        if rec:
            of = verdict.recurrence_of or 0
            self._journal_recurrence(issue, of, merged_by_issue.get(of, 0))
        after = self.gateway.get_issue(issue)
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
        self.store.set_triage_at(issue, after.updated_at)
        # This issue produced a verdict AND landed it: the consecutive-failure
        # series is over, so a future failure starts counting from zero.
        self.store.clear_steward_stalls(issue, "triage")
