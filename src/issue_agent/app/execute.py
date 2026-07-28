"""Action executor — interpret a domain Action as adapter side-effects.

The decision functions return `[Action]`; this turns each into a gateway /
worktree / agent call. It is deliberately a flat dispatch with NO decision logic
of its own (that all lives in `domain`), so it's verified by asserting which
adapter method each Action triggers, using a fake gateway.

Leaf side-effects (Relabel/PostComment/CloseIssue/EnableAutoMerge/RemoveWorktree)
are handled fully here. `RunAgent` is delegated to an injected `run_agent`
callable — building the per-kind AgentTask (resolve/resume/fix-ci/review/…) is
Phase 4 work; the executor just routes to it.
"""

from __future__ import annotations

from collections.abc import Callable

from ..adapters.journal import alert, event_line
from ..domain.actions import (
    Action,
    AddDependency,
    ClearIssueState,
    CloseIssue,
    EnableAutoMerge,
    EscalateCiPending,
    UpdateBranch,
    NoOp,
    PostComment,
    Relabel,
    RemoveWorktree,
    RerunFailedChecks,
    RunAgent,
    Target,
)
from ..domain.labels import Label

# gh subcommand kind per target.
_KIND = {Target.ISSUE: "issue", Target.PR: "pr"}


class Executor:
    """Applies Actions. `gateway` is a GitHubGateway (or fake). `remove_worktree`
    and `run_agent` are injected so the executor stays free of git/SDK imports
    and is unit-testable in isolation."""

    def __init__(
        self,
        gateway,
        remove_worktree: Callable[[int], None] | None = None,
        run_agent: Callable[[RunAgent], None] | None = None,
        log: Callable[[str], None] | None = None,
        mark_automerge: Callable[[int], None] | None = None,
        mark_ci_rerun: Callable[[int, str], None] | None = None,
        mark_ci_pending_escalated: Callable[[int, str], None] | None = None,
        clear_issue_state: Callable[[int, int | None, bool], None] | None = None,
        journal=None,
        alert_cmd: str = "",
    ):
        self.gateway = gateway
        self._remove_worktree = remove_worktree or (lambda issue: None)
        self._run_agent = run_agent or (lambda action: None)
        # Persist the "already armed" marker the decide side reads
        # (FixState.automerge_enabled). Without this write the marker file was
        # never created (T3 probe finding: store.mark_automerge had zero
        # production call sites), so should_automerge re-armed every poll —
        # one wasted `gh pr merge --auto` per merge-ready PR per tick.
        self._mark_automerge = mark_automerge or (lambda pr: None)
        # Persists the "this head has spent its ONE free CI re-run" marker the
        # decide side reads (FixState.ci_rerun_head). Only the fix daemon wires
        # it (Store.set_ci_rerun_head); a default no-op would make the re-run
        # loop forever, which is why the arm below refuses to call gh at all
        # when it is absent — see there.
        self._mark_ci_rerun = mark_ci_rerun
        self._mark_ci_pending_escalated = (
            mark_ci_pending_escalated or (lambda pr, head: None)
        )
        # Retires STATE_DIR files on the ClearIssueState arm (review M4/D8:
        # Store.clear_issue/clear_pr_review had zero production call sites).
        # Default no-op keeps tests/other roles' Executors quiet — only the
        # fix daemon wires this (Store.cleanup_pr).
        self._clear_issue_state = clear_issue_state or (
            lambda pr, issue, merged: None
        )
        # Log EVERY applied action so state changes (label moves, comments, close,
        # auto-merge, worktree removal, agent dispatch) are visible instead of
        # inferred. One funnel = one audit trail. Default no-op keeps tests quiet.
        self._log = log or (lambda _m: None)
        # Structured twin of the log line above: the same audit trail, machine-
        # readable, so `issue-agent status` can answer "what changed overnight?"
        # from a file instead of a human eyeballing rotated stderr. None = off.
        self._journal = journal
        self._alert_cmd = alert_cmd

    def _describe(self, action: Action) -> str:
        """Human-readable one-liner for the action being applied."""
        if isinstance(action, Relabel):
            k = _KIND[action.target]
            rm = action.remove.value if action.remove else None
            add = action.add.value if action.add else None
            move = " ".join(p for p in (f"-{rm}" if rm else "", f"+{add}" if add else "") if p)
            return f"relabel {k} #{action.number}: {move or '(no-op)'}"
        if isinstance(action, PostComment):
            return f"comment {_KIND[action.target]} #{action.number} ({len(action.body)} chars)"
        if isinstance(action, CloseIssue):
            return f"close issue #{action.number} ({action.reason})"
        if isinstance(action, EnableAutoMerge):
            return f"enable auto-merge PR #{action.pr} ({action.method})"
        if isinstance(action, UpdateBranch):
            return f"update branch PR #{action.pr} (BEHIND → up-to-date)"
        if isinstance(action, RerunFailedChecks):
            return f"re-run failed checks PR #{action.pr} ({action.head[:7]})"
        if isinstance(action, EscalateCiPending):
            return f"escalate stalled CI PR #{action.pr} ({action.head[:7]})"
        if isinstance(action, AddDependency):
            return f"link #{action.issue} blocked-by #{action.blocker}"
        if isinstance(action, RemoveWorktree):
            return f"remove worktree for #{action.issue}"
        if isinstance(action, ClearIssueState):
            scope = "merged" if action.merged else "closed-unmerged"
            issue_part = f", issue #{action.issue}" if action.issue is not None else ""
            return f"clear state for pr #{action.pr}{issue_part} ({scope})"
        if isinstance(action, RunAgent):
            return f"dispatch {action.kind.name} #{action.number}"
        return repr(action)

    def _escalated(self, action: Relabel) -> None:
        """A move to needs-human-review is the harness saying "I'm out" — the ONE
        state change that requires a person and the only one worth waking someone
        for. Detected here, at the single funnel every write passes through, so no
        future escalation path can be added without it being noticed."""
        fields = {"target": _KIND[action.target], "number": action.number}
        if self._journal is not None:
            self._journal.emit("escalation", **fields)
        alert(self._alert_cmd, event_line("escalation", **fields))

    def execute(self, action: Action) -> None:
        if not isinstance(action, NoOp):
            desc = self._describe(action)
            self._log(desc)
            if self._journal is not None:
                self._journal.emit("action", desc=desc)
        if isinstance(action, Relabel):
            self.gateway.relabel(
                _KIND[action.target],
                action.number,
                action.remove.value if action.remove else None,
                action.add.value if action.add else None,
            )
            # AFTER the write: an escalation that GitHub rejected did not happen,
            # and a journal that claims it did is worse than one that is silent.
            if action.add is Label.NEEDS_HUMAN_REVIEW:
                self._escalated(action)
        elif isinstance(action, EscalateCiPending):
            self.gateway.comment("pr", action.pr, action.body)
            self.gateway.relabel(
                "pr", action.pr, Label.AI_PR_OPEN.value,
                Label.NEEDS_HUMAN_REVIEW.value,
            )
            verifier = getattr(self.gateway, "has_label", None)
            if verifier is not None and not verifier(
                "pr", action.pr, Label.NEEDS_HUMAN_REVIEW.value
            ):
                self._log(
                    f"CI stall escalation for PR #{action.pr} was not verified; "
                    "marker withheld so the next poll retries"
                )
                return
            self._mark_ci_pending_escalated(action.pr, action.head)
            self._escalated(Relabel(
                Target.PR, action.pr, Label.AI_PR_OPEN,
                Label.NEEDS_HUMAN_REVIEW,
            ))
        elif isinstance(action, PostComment):
            self.gateway.comment(_KIND[action.target], action.number, action.body)
        elif isinstance(action, CloseIssue):
            self.gateway.close_issue(action.number, action.reason, action.comment)
        elif isinstance(action, EnableAutoMerge):
            # The marker is the domain's "don't re-arm" latch, so it must record
            # what GITHUB did, not what we attempted: an unconditional write here
            # wedged #537 (arm failed on an unprotected base branch, marker set,
            # should_automerge suppressed forever on a PR that was APPROVED +
            # green + CLEAN). Falsy/None returns (old gateways, test fakes) are
            # treated as failure — better to re-arm next poll than to latch a lie.
            if self.gateway.enable_auto_merge(action.pr, action.method):
                self._mark_automerge(action.pr)
            else:
                self._log(
                    f"auto-merge PR #{action.pr}: NOT armed (gh failed or GitHub "
                    "refused); marker withheld so next poll retries"
                )
        elif isinstance(action, UpdateBranch):
            self.gateway.update_branch(action.pr)
        elif isinstance(action, RerunFailedChecks):
            self._rerun_failed_checks(action)
        elif isinstance(action, AddDependency):
            self.gateway.add_blocked_by(action.issue, action.blocker)
        elif isinstance(action, RemoveWorktree):
            self._remove_worktree(action.issue)
        elif isinstance(action, ClearIssueState):
            self._clear_issue_state(action.pr, action.issue, action.merged)
        elif isinstance(action, RunAgent):
            self._run_agent(action)
        elif isinstance(action, NoOp):
            return
        else:  # pragma: no cover - exhaustive ADT guard
            raise TypeError(f"unhandled action: {action!r}")

    def _rerun_failed_checks(self, action: RerunFailedChecks) -> None:
        """The free CI re-run (#38): spend the head's ONE allowance, then ask
        GitHub to re-run the failed checks.

        The marker is written FIRST, deliberately. The two orderings fail in
        opposite directions, and only one of them is bounded:

          * mark → rerun: if this process dies (or gh fails) in between, the
            allowance is spent and the next poll dispatches the paid FIX_CI leg
            — precisely what would have happened with no re-run at all.
          * rerun → mark: a death in between leaves a spent re-run unrecorded,
            so the next poll re-runs again, and again — an unbounded free-CI
            loop with no breaker anywhere behind it (every retry counter in the
            fixer counts AGENT attempts, and this dispatches no agent).

        Without an injected marker writer there is no way to record the
        allowance, so the re-run is SKIPPED rather than run unbounded — the
        review/steward Executors, and any test that doesn't wire one, keep the
        pre-#38 behaviour instead of gaining a loop."""
        if self._mark_ci_rerun is None:
            self._log(
                f"CI re-run PR #{action.pr}: skipped (no marker writer wired) — "
                "the paid CI-fix leg dispatches next poll"
            )
            return
        self._mark_ci_rerun(action.pr, action.head)
        run_id = self.gateway.rerun_failed_checks(action.pr)
        if run_id is None:
            # Nothing to re-run (external status contexts, no Actions run) or gh
            # refused. Soft by design: the marker above already guarantees the
            # next poll pays for the leg instead of retrying this.
            self._log(
                f"CI re-run PR #{action.pr}: no failed Actions run found (or gh "
                "refused) — the paid CI-fix leg dispatches next poll"
            )
        if self._journal is not None:
            self._journal.emit(
                "ci_rerun", pr=action.pr, head=action.head, run_id=run_id or 0,
                ok=run_id is not None,
            )

    def execute_all(self, actions) -> None:
        """Apply every action, even if one raises (review S7): before this, one
        raising action propagated IMMEDIATELY out of the whole call, so sibling
        cleanup after it never ran (a failed relabel could strand a worktree
        RemoveWorktree never reached), and the exception surfaced wherever this
        batch was awaited — typically a workflow's post-run tail — which read
        as "the agent run failed" when the run had actually succeeded and only
        a follow-up write broke. Each failure is now logged loudly (naming the
        action — misattribution gone) and NOT re-raised: every remaining
        action still gets its turn, and the caller (e.g.
        `AgentDispatcher.dispatch`'s tail) sees the batch complete normally
        instead of unwinding on the first bad write —
        tests/adapters/test_dispatch.py's mid-dispatch-failure suite (review
        T3, landed concurrently) pins this: a follow-up relabel failure is now
        isolated at the ACTION level, not conflated with a pre-result session
        death (infra-deaths stays untouched; that counter is for the dispatch
        handler's own outer crash-isolation net, a different failure mode).

        RunAgent never flows through this method today (outcome.py only ever
        emits Relabel/PostComment/CloseIssue/EnableAutoMerge/RemoveWorktree/
        ClearIssueState follow-ups here — decide_fix's own RunAgent actions
        are applied one-at-a-time by Daemon.tick, which already isolates per
        action)."""
        for action in actions:
            try:
                self.execute(action)
            except Exception as exc:  # noqa: BLE001 - one bad action must not sink its siblings
                desc = self._describe(action)
                msg = f"FAILED: {desc} — {exc!r}; continuing with remaining actions"
                self._log(msg)
                if self._journal is not None:
                    self._journal.emit("action_failed", desc=desc, error=str(exc))
