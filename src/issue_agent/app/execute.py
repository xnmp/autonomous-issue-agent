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
from ..adapters.store import encode_lifecycle_action
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
from ..domain.labels import ADVISORY_LABELS, Label
from ..domain.events import (
    ActionApplied,
    ActionFailed,
    ActionRetried,
    Correlation,
    EscalationRaised,
    LevelChanged,
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
    RerunFailedChecks,
    EscalateCiPending,
    AddDependency,
)
_DEFAULT_RETRY_DELAYS = (1.0, 3.0, 9.0)


def _ignore_log(_message: str) -> None:
    pass


def _missing_handler(name: str):
    def missing(*_args) -> None:
        raise RuntimeError(f"required executor handler is not configured: {name}")

    return missing


def _is_advisory_relabel(action: Relabel) -> bool:
    return (
        action.remove in ADVISORY_LABELS
        or action.add in ADVISORY_LABELS
    )


def _action_spec(action: Action) -> tuple[str, str, int | None, dict]:
    """Return one structured telemetry shape for every action verb."""

    if isinstance(action, _DURABLE_ACTIONS):
        raw = encode_lifecycle_action(action)
        target = str(raw.get("target") or (
            "PR" if isinstance(action, EscalateCiPending) else "ISSUE"
        )).lower()
        number = (
            action.pr if isinstance(action, EscalateCiPending) else action.number
        )
        return str(raw["type"]), target, number, raw
    if isinstance(action, EnableAutoMerge):
        return "enable_auto_merge", "pr", action.pr, {
            "type": "enable_auto_merge", "pr": action.pr, "method": action.method,
        }
    if isinstance(action, UpdateBranch):
        return "update_branch", "pr", action.pr, {
            "type": "update_branch", "pr": action.pr,
        }
    if isinstance(action, RerunFailedChecks):
        return "rerun_failed_checks", "pr", action.pr, {
            "type": "rerun_failed_checks", "pr": action.pr, "head": action.head,
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
    if isinstance(action, RunAgent):
        return "run_agent", action.kind.name.lower(), action.number or None, {
            "type": "run_agent",
            "kind": action.kind.name,
            "number": action.number,
        }
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
        (EnableAutoMerge, UpdateBranch, RerunFailedChecks, EscalateCiPending),
    ):
        return Correlation(pr=action.pr)
    if isinstance(action, AddDependency):
        return Correlation(issue=action.issue)
    if isinstance(action, (RemoveWorktree,)):
        return Correlation(issue=action.issue)
    if isinstance(action, ClearIssueState):
        return Correlation(issue=action.issue, pr=action.pr)
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

    def submit_review(self, _pr: int, _event: str, _body: str) -> bool:
        raise RuntimeError("durable effect executor is not configured")

    def reconcile(self, _action: Action) -> bool | None:
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
        mark_ci_pending_escalated: Callable[[int, str], None] | None = None,
        clear_issue_state: Callable[[int, int | None, bool], None] | None = None,
        journal=None,
        alert_cmd: str = "",
        pending_actions=None,
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
        # Log EVERY applied action so state changes (label moves, comments, close,
        # auto-merge, worktree removal, agent dispatch) are visible instead of
        # inferred. One funnel = one audit trail. Default no-op keeps tests quiet.
        self._log = log or _ignore_log
        # Structured twin of the log line above: the same audit trail, machine-
        # readable, so `issue-agent status` can answer "what changed overnight?"
        # from a file instead of a human eyeballing rotated stderr. None = off.
        self._journal = journal
        self._alert_cmd = alert_cmd
        # STATE_DIR-backed queue shared with Daemon.tick. A terminal write that
        # exhausts its immediate transient retries remains here across process
        # death/hot reload; the next tick drains it before observing or buying
        # another session.
        self._pending_actions = pending_actions
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._effect_executor = effect_executor or MissingEffectExecutor()
        # Paid dispatches run outside this lock. Every lifecycle side effect
        # they emit is serialized with the observation loop, preserving the
        # single-writer pending queue and deterministic GitHub transitions.
        self._action_lock = threading.RLock()

    def bind_dispatch(self, run_agent: Callable[[RunAgent], None]) -> None:
        """Bind the paid-dispatch handler exactly once.

        Lifecycle-only executors may stay unbound. Executing ``RunAgent`` on an
        unbound executor is a composition error, never a successful no-op.
        """

        if self._run_agent is not None:
            raise RuntimeError("dispatch handler is already bound")
        self._run_agent = run_agent

    def submit_review(self, pr: int, event: str, body: str) -> bool:
        if self._effect_executor is None:
            raise RuntimeError("review effect executor is not configured")
        return self._effect_executor.submit_review(pr, event, body)

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
            action.remove is Label.AI_BLOCKED
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
            elif isinstance(action, RerunFailedChecks):
                if outcome is None:
                    self._log(
                        f"CI re-run PR #{action.pr}: no failed Actions run found "
                        "(or gh refused) — the paid CI-fix leg dispatches next poll"
                    )
                if self._journal is not None:
                    self._journal.emit(
                        TelemetryEvent(
                            TelemetryWarning(
                                "ci_rerun",
                                "free CI rerun attempted",
                                {
                                    "head": action.head,
                                    "run_id": outcome or 0,
                                    "ok": outcome is not None,
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
        if (
            isinstance(last, LifecycleWriteUnverified)
            and self._pending_actions is None
        ):
            raise last
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

    def _persist_pending(self, action: Action) -> bool:
        if self._pending_actions is None:
            return False
        pending = self._pending_actions.load_pending_actions()
        if action not in pending:
            self._pending_actions.save_pending_actions((*pending, action))
        return True

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
            pending = (
                isinstance(action, _DURABLE_ACTIONS)
                and isinstance(exc, _TRANSIENT_WRITE_ERRORS)
                and self._persist_pending(action)
            )
            if self._journal is not None:
                self._journal.emit(
                    TelemetryEvent(
                        ActionFailed(
                            _action_id(action),
                            desc,
                            (
                                "retryable_failure"
                                if pending
                                else "permanent_failure"
                            ),
                            str(exc),
                            attempts + len(self._retry_delays),
                        ),
                        _action_correlation(action),
                    )
                )
            if (
                isinstance(exc, LifecycleWriteUnverified)
                and self._pending_actions is None
            ):
                return
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
        """Replay durable writes, quarantining unverifiable entries after the bound."""
        if self._pending_actions is None:
            return True
        try:
            pending = self._pending_actions.load_pending_actions()
        except (OSError, ValueError) as exc:
            self._log(f"FAILED: cannot read pending lifecycle actions — {exc}")
            if self._journal is not None:
                self._journal.emit(
                    TelemetryEvent(
                        ActionFailed(
                            None,
                            "load pending lifecycle actions",
                            "retryable_failure",
                            str(exc),
                        )
                    )
                )
            return False
        for index, action in enumerate(pending):
            try:
                self.execute(action, replayed=True)
                self._pending_actions.save_pending_actions(pending[index + 1 :])
            except LifecycleWriteUnverified as exc:
                # A non-raising GitHub write followed by an unverifiable read is
                # not evidence of an outage: stale/locked PRs and benign gh read
                # failures all reach this arm. One immediate retry cycle plus one
                # queued replay is the bound. Preserve the action for inspection,
                # alert loudly, and let unrelated issues continue to converge.
                try:
                    self._pending_actions.quarantine_pending_action(action)
                    self._pending_actions.save_pending_actions(
                        pending[index + 1 :]
                    )
                except (OSError, ValueError) as quarantine_exc:
                    self._log(
                        f"FAILED: cannot quarantine pending lifecycle action — "
                        f"{quarantine_exc}"
                    )
                    return False
                desc = self._describe(action)
                self._log(f"QUARANTINED: {desc} — {exc}")
                fields = {"desc": desc, "error": str(exc)}
                if self._journal is not None:
                    self._journal.emit(
                        TelemetryEvent(
                            TelemetryWarning(
                                "action_quarantined",
                                str(exc),
                                fields,
                            ),
                            _action_correlation(action),
                        )
                    )
                alert(
                    self._alert_cmd,
                    json.dumps(
                        {"event": "action_quarantined", **fields},
                        sort_keys=True,
                    ),
                )
            except Exception:
                return False
        return True

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
        for action in actions:
            try:
                self.execute(action)
            except Exception as exc:  # noqa: BLE001 - one bad action must not sink its siblings
                succeeded = False
                desc = self._describe(action)
                msg = f"FAILED: {desc} — {exc!r}; continuing with remaining actions"
                self._log(msg)
        return succeeded
