"""Steward triage workflow dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...adapters.journal import groomed_keys, read_events_tail, recurrence_keys
from ...domain import triage as triage_dom
from ...domain.agent_task import AgentRuntime
from ...domain.triage import TriageConfig
from ...domain.triage import TriageVerdict
from ..planners import tasks as task_builders
from ..planners.tasks import GroomTaskConfig, TriageTaskConfig


@dataclass(frozen=True)
class StewardWorkflowConfig:
    triage: TriageTaskConfig | None
    triage_cfg: TriageConfig | None
    log: object
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
    journal: object | None = None


def _merge_lines(merged) -> str:
    """The merged-issue reference set, rendered for the prompt. One line per
    issue, newest first — the order `gh pr list` already returns."""
    return "\n".join(
        f"- #{m.issue} {m.title} (PR #{m.pr}, merged {m.merged_at})" for m in merged
    )


@dataclass
class StewardWorkflow:
    cfg: StewardWorkflowConfig
    gateway: object
    store: object
    runner: AgentRuntime
    executor: object
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
        fn = getattr(self.gateway, "recently_merged_issues", None)
        if fn is None or not self.cfg.fix_bot:
            return ()
        try:
            return tuple(fn(self.cfg.fix_bot) or ())
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
        if key in recurrence_keys(read_events_tail(journal.path)):
            return
        journal.emit(
            "recurrence", kind="refiled", issue=issue, of_issue=of_issue,
            pr=pr, block_key=key,
        )

    def _journal_groomed(self, issue: int, kind: str, *, ref: str = "", pr: int = 0) -> None:
        key = f"groomed:{self.cfg.repo}:{issue}:{kind}:{ref or pr}"
        if key in self._emitted:
            return
        self._emitted.add(key)
        journal = self.cfg.journal
        if journal is None or key in groomed_keys(read_events_tail(journal.path)):
            return
        fields = {"kind": kind, "issue": issue, "block_key": key}
        if ref:
            fields["reference"] = ref
        if pr:
            fields["pr"] = pr
        journal.emit("groomed", **fields)

    def _groomed(self, issue: int, kind: str, *, ref: str = "", pr: int = 0) -> bool:
        key = f"groomed:{self.cfg.repo}:{issue}:{kind}:{ref or pr}"
        if key in self._emitted:
            return True
        journal = self.cfg.journal
        return journal is not None and key in groomed_keys(read_events_tail(journal.path))

    async def groom(self, issue: int) -> None:
        """Classify an existing issue for one-shot, advisory queue cleanup."""
        if self.cfg.groom is None:
            return
        iss = self.gateway.get_issue(issue)
        merged = self._recent_merges()
        result = await self.runner.run(task_builders.build_groom_task(
            issue, iss.title, iss.body, self.cfg.groom, _merge_lines(merged)
        ))
        if result.is_error or not isinstance(result.structured, dict):
            self.cfg.log(f"groom #{issue}: {result.status or 'error'} — no finding, will retry")
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
            self.cfg.log(f"groom #{issue}: advisory write failed, will retry")
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

    async def triage(self, issue: int) -> None:
        self.cfg.log(f"triage #{issue}: classifying")
        iss = self.gateway.get_issue(issue)
        thread = "\n\n---\n\n".join(f"@{c.author}: {c.body}" for c in iss.comments)
        merged = self._recent_merges()
        task = task_builders.build_triage_task(
            issue, iss.title, iss.body, thread, self.cfg.triage,
            recent_merges=_merge_lines(merged),
        )
        result = await self.runner.run(task)
        if result.is_error or not isinstance(result.structured, dict):
            self.cfg.log(f"triage #{issue}: {result.status or 'error'} — no verdict, will retry")
            return
        s = result.structured
        verdict = TriageVerdict(
            verdict=s.get("verdict", ""),
            plain_summary=s.get("plain_summary", ""),
            acceptance_criteria=tuple(s.get("acceptance_criteria", []) or ()),
            creator_questions=tuple(s.get("creator_questions", []) or ()),
            owner_questions=tuple(s.get("owner_questions", []) or ()),
            blocked_by_issues=tuple(
                int(n) for n in (s.get("blocked_by_issues", []) or []) if isinstance(n, int)
            ),
            rationale=s.get("rationale", ""),
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
        )
        promote = bool(self.cfg.triage_cfg and self.cfg.triage_cfg.promote)
        if verdict.is_blocked:
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
        if verdict.blocked_by_issues:
            disp += f"; blocked-by {sorted(verdict.blocked_by_issues)}"
        merged_by_issue = {m.issue: m.pr for m in merged}
        rec = triage_dom.recurrence_actions(issue, verdict, merged_by_issue)
        if rec:
            disp += f"; possible recurrence of #{verdict.recurrence_of}"
        self.cfg.log(f"triage #{issue}: {disp}")
        self.executor.execute_all(
            [
                *triage_dom.actions_for_verdict(
                    issue, iss.author, verdict, self.cfg.triage_cfg
                ),
                *rec,
            ]
        )
        # After the actions, so a journalled recurrence always has its label and
        # comment behind it — and only ever for a recurrence the DOMAIN validated.
        if rec:
            of = verdict.recurrence_of or 0
            self._journal_recurrence(issue, of, merged_by_issue.get(of, 0))
        self.store.set_triage_at(issue, self.gateway.get_issue(issue).updated_at)
