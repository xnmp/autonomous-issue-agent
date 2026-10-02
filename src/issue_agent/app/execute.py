"""Action executor — interpret a domain Action as adapter side-effects.

The decision functions return `[Action]`; this turns each into a gateway /
worktree / agent call. It is deliberately a flat dispatch with NO decision logic
of its own (that all lives in `domain`), so it's verified by asserting which
adapter method each Action triggers, using a fake gateway.

Leaf side-effects (Relabel/PostComment/CloseIssue/EnableAutoMerge/RemoveWorktree)
are handled fully here. `RunAgent` is delegated to an injected `run_agent`
callable — building the per-kind RuntimePlan (resolve/resume/fix-ci/review/…)
belongs to application planning; the executor just routes to it.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import threading
import time

from ..adapters.github import GhUnavailable
from ..adapters.journal import alert
from ..domain.actions import (
    Action,
    AddDependency,
    ClearIssueState,
    CloseIssue,
    DiscardSelfClearMarker,
    EnableAutoMerge,
    FinalizeBudgetBlockRetirement,
    FinalizeMachineBlockRetirement,
    EscalateCiPending,
    UpdateBranch,
    NoOp,
    PostComment,
    Relabel,
    RemoveWorktree,
    CancelWorkflowRuns,
    RerunFailedChecks,
    ResetCiRetries,
    ReportSelfClearCeiling,
    RunAgent,
    Target,
)
from ..domain.labels import ADVISORY_LABELS, Label
from ..domain.events import (
    ActionApplied,
    ActionFailed,
    ActionRetried,
    BlockRetired,
    BudgetBlockRetired,
    Correlation,
    EscalationRaised,
    LevelChanged,
    SelfClearCeilingReached,
    TelemetryEvent,
    TelemetryWarning,
)

# gh subcommand kind per target.
_KIND = {Target.ISSUE: "issue", Target.PR: "pr"}
_DURABLE_ACTIONS = (Relabel, PostComment, CloseIssue, EscalateCiPending)
_PRIVILEGED_ACTIONS = (
    Relabel,
    PostComment,
    CloseIssue,
    EnableAutoMerge,
    UpdateBranch,
    CancelWorkflowRuns,
    RerunFailedChecks,
    EscalateCiPending,
    AddDependency,
)
_DEFAULT_RETRY_DELAYS = (1.0, 3.0, 9.0)


def _effect_run_ids(outcome: object) -> tuple[int, ...]:
    """The workflow-run ids an effect reports having touched.

    Only the immediate adapter returns them; the durable executor normalises
    every outcome to a bool receipt. The journal must degrade to "ids unknown"
    rather than crash on that bool - an observability field may never take a
    remote mutation down with it.
    """
    return tuple(outcome) if isinstance(outcome, tuple) else ()


def _effect_refused(outcome: object) -> bool:
    """Whether the remote refused the effect, across both executors: None (the
    immediate adapter could not act) and False (the durable one got no
    confirmation) are the same operational fact."""
    return outcome is None or outcome is False


def _ignore_log(_message: str) -> None:
    pass


def _missing_handler(name: str):
    def missing(*_args) -> None:
        raise RuntimeError(f"required executor handler is not configured: {name}")

    return missing


def _is_advisory_relabel(action: Relabel) -> bool:
    return (
        any(label in ADVISORY_LABELS for label in action.removed_labels)
        or any(label in ADVISORY_LABELS for label in action.added_labels)
    )


def _action_spec(action: Action) -> tuple[str, str, int | None, dict]:
    """Return one structured telemetry shape for every action verb."""

    if isinstance(action, Relabel):
        payload: dict[str, object] = {
            "type": "relabel",
            "target": action.target.name,
            "number": action.number,
            "remove": action.remove.value if action.remove else None,
            "add": action.add.value if action.add else None,
        }
        if action.removes:
            payload["removes"] = [label.value for label in action.removes]
        if action.adds:
            payload["adds"] = [label.value for label in action.adds]
        if action.comment:
            payload["comment"] = action.comment
        return "relabel", action.target.name.lower(), action.number, payload
    if isinstance(action, PostComment):
        return "comment", action.target.name.lower(), action.number, {
            "type": "comment",
            "target": action.target.name,
            "number": action.number,
            "body": action.body,
        }
    if isinstance(action, CloseIssue):
        return "close", "issue", action.number, {
            "type": "close",
            "number": action.number,
            "reason": action.reason,
            "comment": action.comment,
        }
    if isinstance(action, EscalateCiPending):
        return "escalate_ci_pending", "pr", action.pr, {
            "type": "escalate_ci_pending",
            "pr": action.pr,
            "head": action.head,
            "body": action.body,
        }
    if isinstance(action, EnableAutoMerge):
        return "enable_auto_merge", "pr", action.pr, {
            "type": "enable_auto_merge",
            "pr": action.pr,
            "method": action.method,
            "head": action.head,
        }
    if isinstance(action, UpdateBranch):
        return "update_branch", "pr", action.pr, {
            "type": "update_branch", "pr": action.pr, "head": action.head,
        }
    if isinstance(action, RerunFailedChecks):
        return "rerun_failed_checks", "pr", action.pr, {
            "type": "rerun_failed_checks",
            "pr": action.pr,
            "head": action.head,
            "run_ids": list(action.run_ids),
        }
    if isinstance(action, CancelWorkflowRuns):
        return "cancel_workflow_runs", "pr", action.pr, {
            "type": "cancel_workflow_runs",
            "pr": action.pr,
            "head": action.head,
            "run_ids": list(action.run_ids),
        }
    if isinstance(action, AddDependency):
        return "add_dependency", "issue", action.issue, {
            "type": "add_dependency",
            "issue": action.issue,
            "blocker": action.blocker,
        }
    if isinstance(action, RemoveWorktree):
        return "remove_worktree", "worktree", action.issue, {
            "type": "remove_worktree", "issue": action.issue,
        }
    if isinstance(action, ClearIssueState):
        number = action.pr or action.issue
        return "clear_issue_state", "state", number, {
            "type": "clear_issue_state",
            "pr": action.pr,
            "issue": action.issue,
            "merged": action.merged,
        }
    if isinstance(action, ResetCiRetries):
        return "reset_ci_retries", "state", action.issue, {
            "type": "reset_ci_retries",
            "issue": action.issue,
        }
    if isinstance(action, FinalizeMachineBlockRetirement):
        return "block_retired", "state", action.issue, {
            "type": "block_retired",
            "issue": action.issue,
            "target": action.target.name.lower(),
            "number": action.number,
            "blocker": action.blocker,
            "blocked_head": action.blocked_head,
            "observed_head": action.observed_head,
        }
    if isinstance(action, FinalizeBudgetBlockRetirement):
        return "budget_block_retired", "state", action.issue, {
            "type": "budget_block_retired",
            "issue": action.issue,
            "target": action.target.name.lower(),
            "number": action.number,
            "scope": action.scope,
        }
    if isinstance(action, ReportSelfClearCeiling):
        return "self_clear_ceiling", "state", action.issue, {
            "type": "self_clear_ceiling",
            "issue": action.issue,
            "target": action.target.name.lower(),
            "number": action.number,
            "blocker": action.blocker,
            "ceiling": action.ceiling,
        }
    if isinstance(action, DiscardSelfClearMarker):
        return "discard_self_clear_marker", "state", action.issue, {
            "type": "discard_self_clear_marker",
            "issue": action.issue,
            "marker": action.marker,
        }
    if isinstance(action, RunAgent):
        payload: dict[str, object] = {
            "type": "run_agent",
            "kind": action.kind.name,
            "number": action.number,
        }
        if action.requires is not None:
            payload["requires"] = _action_id(action.requires)
        return "run_agent", action.kind.name.lower(), action.number or None, payload
    if isinstance(action, NoOp):
        return "noop", "local", None, {
            "type": "noop", "reason": action.reason,
        }
    raise TypeError(f"unhandled action telemetry: {action!r}")


def _action_correlation(action: Action) -> Correlation:
    if isinstance(action, (Relabel, PostComment)):
        if action.target is Target.ISSUE:
            return Correlation(issue=action.number)
        return Correlation(pr=action.number)
    if isinstance(action, CloseIssue):
        return Correlation(issue=action.number)
    if isinstance(
        action,
        (
            EnableAutoMerge,
            UpdateBranch,
            RerunFailedChecks,
            CancelWorkflowRuns,
            EscalateCiPending,
        ),
    ):
        return Correlation(pr=action.pr)
    if isinstance(action, AddDependency):
        return Correlation(issue=action.issue)
    if isinstance(action, (RemoveWorktree,)):
        return Correlation(issue=action.issue)
    if isinstance(action, ClearIssueState):
        return Correlation(issue=action.issue, pr=action.pr)
    if isinstance(action, ResetCiRetries):
        return Correlation(issue=action.issue)
    if isinstance(
        action,
        (
            FinalizeMachineBlockRetirement,
            FinalizeBudgetBlockRetirement,
            ReportSelfClearCeiling,
        ),
    ):
        return Correlation(
            issue=action.issue,
            pr=action.number if action.target is Target.PR else None,
        )
    if isinstance(action, DiscardSelfClearMarker):
        return Correlation(issue=action.issue)
    if isinstance(action, RunAgent):
        issue_kinds = {"RESOLVE", "RESUME", "TRIAGE", "GROOM"}
        if action.number <= 0:
            return Correlation()
        if action.kind.name in issue_kinds:
            return Correlation(issue=action.number)
        return Correlation(pr=action.number)
    return Correlation()


def _action_id(action: Action) -> str:
    _kind, _target, _number, structured = _action_spec(action)
    material = json.dumps(structured, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


class LifecycleWriteUnverified(RuntimeError):
    """GitHub returned without raising, but the requested state is still absent."""


_TRANSIENT_WRITE_ERRORS = (GhUnavailable, LifecycleWriteUnverified)


class MissingEffectExecutor:
    """Fail closed when composition omitted the durable effect boundary."""

    def handles(self, action: Action) -> bool:
        return isinstance(action, _PRIVILEGED_ACTIONS)

    def execute(self, _action: Action) -> bool:
        raise RuntimeError("durable effect executor is not configured")

    def submit_review(
        self, _pr: int, _event: str, _body: str, _expected_head: str
    ) -> bool:
        raise RuntimeError("durable effect executor is not configured")

    def reconcile(self, _action: Action) -> bool | None:
        raise RuntimeError("durable effect executor is not configured")

    def drain_pending_commands(self) -> int:
        raise RuntimeError("durable effect executor is not configured")


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
        mark_ci_cancel: Callable[[int, str], None] | None = None,
        mark_branch_update: Callable[[int, str], None] | None = None,
        mark_ci_pending_escalated: Callable[[int, str], None] | None = None,
        clear_issue_state: Callable[[int, int | None, bool], None] | None = None,
        reset_ci_retries: Callable[[ResetCiRetries], None] | None = None,
        retire_machine_block: Callable[[FinalizeMachineBlockRetirement], int] | None = None,
        retire_budget_block: Callable[[FinalizeBudgetBlockRetirement], int] | None = None,
        mark_self_clear_ceiling: Callable[[int], None] | None = None,
        discard_machine_block: Callable[[DiscardSelfClearMarker], None] | None = None,
        discard_budget_block: Callable[[DiscardSelfClearMarker], None] | None = None,
        journal=None,
        alert_cmd: str = "",
        retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
        effect_executor=None,
    ):
        self.gateway = gateway
        self._remove_worktree = remove_worktree or _missing_handler(
            "remove_worktree"
        )
        self._run_agent = run_agent
        # Persist the "already armed" marker the decide side reads
        # (FixState.automerge_enabled). Without this write the marker file was
        # never created (T3 probe finding: store.mark_automerge had zero
        # production call sites), so should_automerge re-armed every poll —
        # one wasted `gh pr merge --auto` per merge-ready PR per tick.
        self._mark_automerge = mark_automerge or _missing_handler(
            "mark_automerge"
        )
        # Persists the "this head has spent its ONE free CI re-run" marker the
        # decide side reads (FixState.ci_rerun_head). Only the fix daemon wires
        # it (Store.set_ci_rerun_head); a default no-op would make the re-run
        # loop forever, which is why the arm below refuses to call gh at all
        # when it is absent — see there.
        self._mark_ci_rerun = mark_ci_rerun
        # Persists "a cancel has been sent for this head's wedged runs"
        # (FixState.ci_cancel_head). Written BEFORE the cancel call for the same
        # reason the ci-rerun marker is: marked-but-not-cancelled costs one
        # await-cancel wait and then the bounded escalation, whereas cancelled-
        # but-not-marked re-cancels the run every poll forever. Unwired = the
        # arm refuses to call gh at all, exactly like the re-run above.
        self._mark_ci_cancel = mark_ci_cancel
        # Persists the head an advisory-UNSTABLE branch-refresh was dispatched
        # for (FixState.update_branch_head). Written BEFORE the update call,
        # same ordering rationale as the ci-rerun marker: marked-but-not-
        # refreshed costs one retry next poll; refreshed-but-not-marked is an
        # unbounded push loop.
        self._mark_branch_update = mark_branch_update
        self._mark_ci_pending_escalated = (
            mark_ci_pending_escalated
            or _missing_handler("mark_ci_pending_escalated")
        )
        # Retires STATE_DIR files on the ClearIssueState arm (review M4/D8:
        # Store.clear_issue/clear_pr_review had zero production call sites).
        # Default no-op keeps tests/other roles' Executors quiet — only the
        # fix daemon wires this (Store.cleanup_pr).
        self._clear_issue_state = clear_issue_state or _missing_handler(
            "clear_issue_state"
        )
        # Closes a red-CI episode (ResetCiRetries): green settles → the FIX_CI
        # breaker starts the next episode from zero. Only the fix daemon wires
        # it with the exact selected green observation; default no-op keeps other
        # roles' Executors quiet — an unwired reset costs nothing but a stale
        # count, which is exactly the pre-fix behaviour.
        self._reset_ci_retries = reset_ci_retries
        self._retire_machine_block = retire_machine_block or _missing_handler(
            "retire_machine_block"
        )
        self._retire_budget_block = retire_budget_block or _missing_handler(
            "retire_budget_block"
        )
        self._mark_self_clear_ceiling = (
            mark_self_clear_ceiling or _missing_handler("mark_self_clear_ceiling")
        )
        self._discard_machine_block = discard_machine_block or _missing_handler(
            "discard_machine_block"
        )
        self._discard_budget_block = discard_budget_block or _missing_handler(
            "discard_budget_block"
        )
        # Log EVERY applied action so state changes (label moves, comments, close,
        # auto-merge, worktree removal, agent dispatch) are visible instead of
        # inferred. One funnel = one audit trail. Default no-op keeps tests quiet.
        self._log = log or _ignore_log
        # Structured twin of the log line above: the same audit trail, machine-
        # readable, so `issue-agent status` can answer "what changed overnight?"
        # from a file instead of a human eyeballing rotated stderr. None = off.
        self._journal = journal
        self._alert_cmd = alert_cmd
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._effect_executor = effect_executor or MissingEffectExecutor()
        # Paid dispatches run outside this lock. Every lifecycle side effect
        # they emit is serialized with the observation loop, preserving the
        # single-writer pending queue and deterministic GitHub transitions.
        self._action_lock = threading.RLock()
        self._reconcile_workflow: Callable[[], int] | None = None

    def bind_workflow_reconciliation(self, reconcile: Callable[[], int]) -> None:
        if self._reconcile_workflow is not None:
            raise ValueError("workflow reconciliation is already bound")
        self._reconcile_workflow = reconcile

    def bind_dispatch(self, run_agent: Callable[[RunAgent], None]) -> None:
        """Bind the paid-dispatch handler exactly once.

        Lifecycle-only executors may stay unbound. Executing ``RunAgent`` on an
        unbound executor is a composition error, never a successful no-op.
        """

        if self._run_agent is not None:
            raise RuntimeError("dispatch handler is already bound")
        self._run_agent = run_agent

    def submit_review(
        self, pr: int, event: str, body: str, expected_head: str
    ) -> bool:
        if self._effect_executor is None:
            raise RuntimeError("review effect executor is not configured")
        return self._effect_executor.submit_review(
            pr, event, body, expected_head
        )

    def _describe(self, action: Action) -> str:
        """Human-readable one-liner for the action being applied."""
        if isinstance(action, Relabel):
            k = _KIND[action.target]
            removed = ",".join(label.value for label in action.removed_labels)
            add = action.add.value if action.add else None
            move = " ".join(
                part
                for part in (
                    f"-{removed}" if removed else "",
                    f"+{add}" if add else "",
                )
                if part
            )
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
            runs = ",".join(str(run_id) for run_id in action.run_ids)
            return (
                f"re-run failed checks PR #{action.pr} "
                f"({action.head[:7]}; runs {runs or '?'})"
            )
        if isinstance(action, CancelWorkflowRuns):
            runs = ",".join(str(run_id) for run_id in action.run_ids)
            return (
                f"cancel wedged CI runs PR #{action.pr} "
                f"({action.head[:7]}; runs {runs or '?'})"
            )
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
        if isinstance(action, ResetCiRetries):
            return f"reset CI-repair count for #{action.issue} (went green)"
        if isinstance(action, FinalizeMachineBlockRetirement):
            return f"finalize {action.blocker} block retirement for #{action.issue}"
        if isinstance(action, FinalizeBudgetBlockRetirement):
            return f"finalize budget block retirement for #{action.issue}"
        if isinstance(action, ReportSelfClearCeiling):
            return f"report self-clear ceiling for #{action.issue} ({action.blocker})"
        if isinstance(action, DiscardSelfClearMarker):
            return f"discard stale {action.marker} block marker for #{action.issue}"
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
            self._journal.emit(
                TelemetryEvent(
                    EscalationRaised(fields["target"], action.number),
                    _action_correlation(action),
                )
            )
        alert(
            self._alert_cmd,
            json.dumps({"event": "escalation", **fields}, sort_keys=True),
        )

    def _human_wait_transition(self, action: Relabel) -> None:
        """Journal the durable edges of an issue's human-input wait."""
        if self._journal is None or action.target is not Target.ISSUE:
            return
        state = None
        if action.add is Label.AI_BLOCKED:
            state = "waiting"
        elif (
            Label.AI_BLOCKED in action.removed_labels
            and action.add is Label.AI_WORKING
        ):
            state = "cleared"
        if state is not None:
            self._journal.emit(
                TelemetryEvent(
                    LevelChanged(
                        f"issue:{action.number}:deferred:human",
                        state,
                        "human",
                    ),
                    Correlation(issue=action.number),
                )
            )

    def _already_applied(self, action: Action) -> bool:
        """Best-effort idempotency probe for a retry after an ambiguous reply.

        A timeout can happen after GitHub committed the mutation. Checking the
        observable state before sending again prevents duplicate comments and
        makes replay safe across a crash between the API acknowledgement and
        queue removal. Older test gateways without these read methods simply
        fall back to the write attempt.
        """
        return self._effect_executor.reconcile(action) is True

    def _report_recovered_escalation(self, action: Action) -> None:
        """Preserve the operator signal when a lost acknowledgement is recovered."""
        if (
            isinstance(action, Relabel)
            and action.add is Label.NEEDS_HUMAN_REVIEW
        ):
            self._escalated(action)
        if isinstance(action, Relabel):
            self._human_wait_transition(action)
        elif isinstance(action, EscalateCiPending):
            self._mark_ci_pending_escalated(action.pr, action.head)
            self._escalated(
                Relabel(
                    Target.PR,
                    action.pr,
                    Label.AI_PR_OPEN,
                    Label.NEEDS_HUMAN_REVIEW,
                )
            )

    def _apply_once(self, action: Action) -> None:
        if self._effect_executor.handles(action):
            if isinstance(action, RerunFailedChecks):
                if self._mark_ci_rerun is None:
                    self._log(
                        f"CI re-run PR #{action.pr}: skipped (no marker writer "
                        "wired) — the paid CI-fix leg dispatches next poll"
                    )
                    return
                self._mark_ci_rerun(action.pr, action.head)
            if isinstance(action, CancelWorkflowRuns):
                if self._mark_ci_cancel is None:
                    self._log(
                        f"CI cancel PR #{action.pr}: skipped (no marker writer "
                        "wired) - the stalled-CI escalation stands"
                    )
                    return
                # `<head>@<decided_at>` — the marker dates the cancel so the
                # await-cancel wait is bounded by time SINCE THE CANCEL. The
                # stall clock cannot bound it: cancelling is what lets follow-on
                # jobs start, and one fresh pending check resets that clock
                # (adversarial review S2). An unstamped world writes the bare
                # SHA, which the domain reads as an undated marker.
                self._mark_ci_cancel(
                    action.pr,
                    f"{action.head}@{action.decided_at}"
                    if action.decided_at
                    else action.head,
                )
            if isinstance(action, UpdateBranch) and self._mark_branch_update is not None:
                self._mark_branch_update(
                    action.pr, f"{action.head}:{action.base}"
                )
            outcome = self._effect_executor.execute(action)
            if isinstance(action, Relabel):
                if outcome is False:
                    raise LifecycleWriteUnverified(
                        f"relabel for {_KIND[action.target]} "
                        f"#{action.number} was not verified"
                    )
                if action.add is Label.NEEDS_HUMAN_REVIEW:
                    self._escalated(action)
                self._human_wait_transition(action)
            elif isinstance(action, EnableAutoMerge):
                if outcome:
                    self._mark_automerge(action.pr)
                else:
                    self._log(
                        f"auto-merge PR #{action.pr}: NOT armed (gh failed or "
                        "GitHub refused); marker withheld so next poll retries"
                    )
            elif isinstance(action, UpdateBranch):
                # The failure must be OBSERVABLE (adversarial F2): the marker
                # was written before the call (see above — the safe ordering),
                # so a failed/no-op update spends the one attempt for this
                # (head, base) pair. A no-op ("already up to date") is the
                # designed loop terminator; a transient gh failure re-arms on
                # the next base advance — but only an operator who can SEE it
                # can act sooner.
                if outcome is False:
                    self._log(
                        f"branch refresh PR #{action.pr}: update did not land "
                        "(already up to date, or gh failed) — attempt spent for "
                        "this head; re-arms when the base next advances"
                    )
                if self._journal is not None:
                    self._journal.emit(
                        TelemetryEvent(
                            TelemetryWarning(
                                "branch_refresh",
                                "branch update attempted",
                                {
                                    "head": action.head,
                                    "base": action.base,
                                    "ok": bool(outcome),
                                },
                            ),
                            Correlation(pr=action.pr),
                        )
                    )
            elif isinstance(action, RerunFailedChecks):
                if _effect_refused(outcome):
                    self._log(
                        f"CI re-run PR #{action.pr}: captured workflow runs were "
                        "not executable — the paid CI-fix leg dispatches next poll"
                    )
                if self._journal is not None:
                    # The durable executor normalises its outcome to a bool, so
                    # the ids it re-ran can only come from the request itself —
                    # which names them exactly. Reading them off the outcome
                    # alone reported `run_ids:[]` for every re-run the durable
                    # path made, success included (issue #177), and that empty
                    # field was read as "the failed-run lookup found nothing".
                    refused = _effect_refused(outcome)
                    rerun_ids = _effect_run_ids(outcome) or (
                        () if refused else action.run_ids
                    )
                    self._journal.emit(
                        TelemetryEvent(
                            TelemetryWarning(
                                "ci_rerun",
                                "free CI rerun attempted",
                                {
                                    "head": action.head,
                                    "run_id": rerun_ids[0] if rerun_ids else 0,
                                    "run_ids": list(rerun_ids),
                                    "requested_run_ids": list(action.run_ids),
                                    "ok": not refused,
                                },
                            ),
                            Correlation(pr=action.pr),
                        )
                    )
            elif isinstance(action, CancelWorkflowRuns):
                if _effect_refused(outcome):
                    self._log(
                        f"CI cancel PR #{action.pr}: wedged runs were not "
                        "cancellable (head moved, or gh failed) - the pending "
                        "deadline escalates once the cancel grace expires"
                    )
                if self._journal is not None:
                    cancelled = _effect_run_ids(outcome)
                    self._journal.emit(
                        TelemetryEvent(
                            TelemetryWarning(
                                "ci_cancel",
                                "wedged CI runs cancelled",
                                {
                                    "head": action.head,
                                    "run_ids": list(cancelled),
                                    "requested_run_ids": list(action.run_ids),
                                    "ok": not _effect_refused(outcome),
                                },
                            ),
                            Correlation(pr=action.pr),
                        )
                    )
            elif isinstance(action, EscalateCiPending):
                if outcome is False:
                    raise LifecycleWriteUnverified(
                        f"CI stall escalation for PR #{action.pr} was not verified"
                    )
                self._mark_ci_pending_escalated(action.pr, action.head)
                self._escalated(
                    Relabel(
                        Target.PR,
                        action.pr,
                        Label.AI_PR_OPEN,
                        Label.NEEDS_HUMAN_REVIEW,
                    )
                )
            return
        if isinstance(action, _PRIVILEGED_ACTIONS):
            raise RuntimeError(
                f"privileged effect adapter does not handle {type(action).__name__}"
            )
        if isinstance(action, RemoveWorktree):
            self._remove_worktree(action.issue)
        elif isinstance(action, ClearIssueState):
            self._clear_issue_state(action.pr, action.issue, action.merged)
        elif isinstance(action, ResetCiRetries):
            if self._reset_ci_retries is not None:
                self._reset_ci_retries(action)
        elif isinstance(action, FinalizeMachineBlockRetirement):
            kind = _KIND[action.target]
            if self.gateway.has_label(kind, action.number, Label.AI_BLOCKED.value):
                raise LifecycleWriteUnverified(
                    f"{kind} #{action.number} is still ai-blocked"
                )
            count = self._retire_machine_block(action)
            if self._journal is not None:
                self._journal.emit(TelemetryEvent(
                    BlockRetired(
                        action.blocker,
                        action.blocked_head,
                        action.observed_head,
                        count,
                    ),
                    _action_correlation(action),
                ))
        elif isinstance(action, FinalizeBudgetBlockRetirement):
            kind = _KIND[action.target]
            remaining = tuple(
                label.value
                for label in (Label.AI_BLOCKED, Label.NEEDS_BUDGET_REVIEW)
                if self.gateway.has_label(kind, action.number, label.value)
            )
            if remaining:
                raise LifecycleWriteUnverified(
                    f"{kind} #{action.number} still has {', '.join(remaining)}"
                )
            count = self._retire_budget_block(action)
            if self._journal is not None:
                self._journal.emit(TelemetryEvent(
                    BudgetBlockRetired(
                        action.scope,
                        action.old_headroom_micros,
                        action.new_headroom_micros,
                        action.reservation_micros,
                        action.cap_micros,
                        action.currency,
                        count,
                    ),
                    _action_correlation(action),
                ))
        elif isinstance(action, ReportSelfClearCeiling):
            self._mark_self_clear_ceiling(action.issue)
            if self._journal is not None:
                self._journal.emit(TelemetryEvent(
                    SelfClearCeilingReached(
                        action.blocker,
                        _KIND[action.target],
                        action.number,
                        action.ceiling,
                    ),
                    _action_correlation(action),
                ))
        elif isinstance(action, DiscardSelfClearMarker):
            if action.marker == "machine":
                self._discard_machine_block(action)
            elif action.marker == "budget":
                self._discard_budget_block(action)
            else:
                raise ValueError(f"unknown self-clear marker {action.marker!r}")
        elif isinstance(action, RunAgent):
            if self._run_agent is None:
                raise RuntimeError("RunAgent executed before dispatch was bound")
            self._run_agent(action)
        elif isinstance(action, NoOp):
            return
        else:  # pragma: no cover - exhaustive ADT guard
            raise TypeError(f"unhandled action: {action!r}")

    def _apply_retry_once(self, action: Action) -> None:
        self._apply_once(action)

    def _apply_with_retry(
        self, action: Action, desc: str, *, resume_first: bool = False
    ) -> int:
        attempts = 1
        try:
            if resume_first:
                self._apply_retry_once(action)
            else:
                self._apply_once(action)
            return attempts
        except _TRANSIENT_WRITE_ERRORS as exc:
            last = exc
        for delay in self._retry_delays:
            if self._journal is not None:
                self._journal.emit(
                    TelemetryEvent(
                        ActionRetried(
                            _action_id(action),
                            desc,
                            attempts,
                            int(delay * 1000),
                            str(last),
                        ),
                        _action_correlation(action),
                    )
                )
            self._log(
                f"RETRY: {desc} after "
                f"{getattr(last, 'headline', 'unverified write')}; "
                f"waiting {delay:g}s"
            )
            self._sleep(delay)
            attempts += 1
            try:
                if self._already_applied(action):
                    self._report_recovered_escalation(action)
                    return attempts
                self._apply_retry_once(action)
                return attempts
            except _TRANSIENT_WRITE_ERRORS as exc:
                last = exc
        raise last

    def execute(self, action: Action, *, replayed: bool = False) -> None:
        if isinstance(action, RunAgent):
            self._execute(action, replayed=replayed)
            return
        with self._action_lock:
            self._execute(action, replayed=replayed)

    def _execute(self, action: Action, *, replayed: bool = False) -> None:
        if isinstance(action, NoOp):
            return
        desc = self._describe(action)
        self._log(desc)
        attempts = 1
        try:
            if isinstance(action, _DURABLE_ACTIONS):
                if replayed and self._already_applied(action):
                    attempts = 0
                    self._report_recovered_escalation(action)
                else:
                    attempts = self._apply_with_retry(
                        action, desc, resume_first=replayed
                    )
            else:
                self._apply_once(action)
        except Exception as exc:
            retryable = (
                isinstance(action, _DURABLE_ACTIONS)
                and isinstance(exc, _TRANSIENT_WRITE_ERRORS)
            )
            if self._journal is not None:
                self._journal.emit(
                    TelemetryEvent(
                        ActionFailed(
                            _action_id(action),
                            desc,
                            (
                                "retryable_failure"
                                if retryable
                                else "permanent_failure"
                            ),
                            str(exc),
                            attempts + len(self._retry_delays),
                        ),
                        _action_correlation(action),
                    )
                )
            raise
        if self._journal is not None:
            kind, target, number, structured = _action_spec(action)
            structured.update({"attempts": attempts, "replayed": replayed})
            self._journal.emit(
                TelemetryEvent(
                    ActionApplied(
                        _action_id(action),
                        kind,
                        target,
                        desc,
                        number,
                        structured,
                    ),
                    _action_correlation(action),
                )
            )

    def replay_pending(self) -> bool:
        with self._action_lock:
            return self._replay_pending()

    def _replay_pending(self) -> bool:
        """Drain the SQLite lifecycle-command authority before new decisions."""
        try:
            if self._reconcile_workflow is not None:
                self._reconcile_workflow()
            self._effect_executor.drain_pending_commands()
            return True
        except Exception as exc:
            self._log(f"FAILED: cannot drain lifecycle commands — {exc}")
            if self._journal is not None:
                self._journal.emit(
                    TelemetryEvent(
                        ActionFailed(
                            None,
                            "drain lifecycle commands",
                            "retryable_failure",
                            str(exc),
                        )
                    )
                )
            return False

    def execute_all(self, actions) -> bool:
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
        succeeded = True
        failed_worktree_cleanup: set[int] = set()
        for action in actions:
            issue = _cleanup_dependency_issue(action)
            if (
                issue is not None
                and issue in failed_worktree_cleanup
                and not isinstance(action, RemoveWorktree)
            ):
                succeeded = False
                self._log(
                    f"SKIPPED: {self._describe(action)} — worktree cleanup "
                    f"for issue #{issue} was not verified"
                )
                continue
            try:
                self.execute(action)
            except Exception as exc:  # noqa: BLE001 - one bad action must not sink its siblings
                succeeded = False
                if isinstance(action, RemoveWorktree):
                    failed_worktree_cleanup.add(action.issue)
                desc = self._describe(action)
                msg = f"FAILED: {desc} — {exc!r}; continuing with remaining actions"
                self._log(msg)
        return succeeded


def _cleanup_dependency_issue(action: Action) -> int | None:
    if isinstance(action, RemoveWorktree):
        return action.issue
    if isinstance(action, ClearIssueState):
        return action.issue
    if isinstance(action, CloseIssue):
        return action.number
    if isinstance(action, Relabel) and action.target is Target.ISSUE:
        return action.number
    return None
