"""ADR 0001's narrow executor for durable GitHub lifecycle commands."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping, NoReturn, Protocol, runtime_checkable

from ..domain.actions import (
    Action,
    is_harness_signal,
    AddDependency,
    CloseIssue,
    EnableAutoMerge,
    EscalateCiPending,
    PostComment,
    Relabel,
    CancelWorkflowRuns,
    RerunFailedChecks,
    Target,
    UpdateBranch,
)
from ..domain.labels import Label
from ..domain.eligibility import IssueCompletionGuard, IssueHoldGuard, LifecycleOperation
from ..domain.lifecycle import (
    AllowedEligibility, completion_guard, completion_matches, issue_eligibility,
    completion_eligibility, hold_guard_applies,
)
from ..domain.repository import canonical_repository
from ..domain.workflow_identity import WorkflowBinding, WorkflowFamily
from ..domain.workflow import (
    ActiveReconciliation, ReconciliationOnly, RetiredUncertainCommand,
    RetryBegan, RetryDeclined,
)
from ..domain.workflow_reconciliation import reconciliation_due, reexecution_exhausted
from ..domain.workflow_retry import RetryAbsenceRead
from ..domain.contracts import (
    CommandId,
    CommandKind,
    EffectStatus,
    LifecycleCommand,
)
from ..domain.effect_retry import (
    DEFAULT_EFFECT_RETRY_CEILING,
    effect_ceiling_reached,
    exhausted_effect_advice,
    exhausted_effect_reason,
)
from ..domain.events import Correlation, LevelChanged, TelemetryEvent
from ..domain.ports import EventSink
from .deployment_authority import DeploymentAuthority, deployment_role
from .durable_effects import DurableEffectRunner, EffectOutcome
from .journal import alert
from ..domain.review_workflow import PartialReviews, PullRequestState, PullRequestUnavailable, mark_review_body
from .review_effects import (
    BoundReviewSubmissionOp, PullRequestProjectionOp, ReviewCheck, ReviewCommandCheck, ReviewDismissalOp,
    ReviewDismissals, ReviewFamilyOp, ReviewReadUnavailable, ReviewReads, bound_submission_op, check_bound_submission, check_review_op,
    is_review_family, observe_pull_request, review_family_op,
)
from .workflow_codec import encode_workflow

_TARGET = {Target.ISSUE: "issue", Target.PR: "pr"}

# The lifecycle command kinds this executor owns: every kind it mints from an
# Action (`_command_identity`) and can reconstruct (`action_from_command`),
# plus SUBMIT_REVIEW, which the drain runs through its own path.
#
# Deliberately an ALLOW-list, not a broker-kinds deny-list. The control store
# is shared, so the drain also sees publish_branch/open_pull_request
# (AgentPublicationBroker), the operator effect kinds, and the Jira kinds. A
# deny-list would silently inherit every kind a future ADR adds and hand it to
# `action_from_command`, which raises — that is exactly the per-tick
# "command kind cannot be reconstructed as Action: CommandKind.PUBLISH_BRANCH"
# noise this replaces (#156). A kind outside the set is LEFT QUEUED for its
# owner — never receipted, never dropped — and reported once per process, so a
# kind nobody owns stays visible instead of silently stranded.
_OWNED_COMMAND_KINDS = frozenset(
    {
        CommandKind.RELABEL,
        CommandKind.POST_COMMENT,
        CommandKind.CLOSE_ISSUE,
        CommandKind.ENABLE_AUTO_MERGE,
        CommandKind.UPDATE_BRANCH,
        CommandKind.RERUN_CHECKS,
        CommandKind.CANCEL_CHECKS,
        CommandKind.ADD_DEPENDENCY,
        CommandKind.SUBMIT_REVIEW,
    }
)
# ADR 0015: review dismissal executes only as the review daemon's owner, with
# its gateway and credential; the fix executor never owns it.
_REVIEW_ROLE_COMMAND_KINDS = frozenset({CommandKind.DISMISS_REVIEW})


def _stderr_log(line: str) -> None:
    sys.stderr.write(f"effects: {line}\n")


def _target_correlation(target: str) -> Correlation:
    """`issue:42` / `pr:7` → the matching correlation, or an empty one for a
    target shape this build does not know (never raise while escalating)."""
    kind, _, raw = (target or "").partition(":")
    try:
        number = int(raw)
    except ValueError:
        return Correlation()
    if kind == "issue":
        return Correlation(issue=number)
    if kind == "pr":
        return Correlation(pr=number)
    return Correlation()


class StaleExpectedRevision(RuntimeError):
    """The command's revision guard no longer matches the remote target.

    Deliberately NOT a transient write error: the mismatch is a fact about the
    decision being stale, so the in-tick retry ladder (1s/3s/9s + a reconcile
    read per attempt) cannot possibly succeed — it burned ~13s per discarded
    repair for nothing (adversarial F2, 2026-08-02). The caller logs the
    failure and the next tick re-decides from a fresh world, which mints a
    fresh revision. The permanent-failure receipt is recorded BEFORE this is
    raised, so the durable command is already settled when it propagates."""


class UnknownEffectOutcome(RuntimeError):
    pass


@runtime_checkable
class LabelVerifier(Protocol):
    def has_label(self, kind: str, number: int, label: str) -> bool: ...


@runtime_checkable
class CommentVerifier(Protocol):
    def has_comment(self, kind: str, number: int, body: str) -> bool: ...


@runtime_checkable
class LinkedPullRequestReads(Protocol):
    def pr_for_issue(self, number: int) -> int | None: ...


@runtime_checkable
class IssueClosureVerifier(Protocol):
    def issue_closure(self, issue: int) -> tuple[bool, str]: ...


@runtime_checkable
class PullRequestMergeVerifier(Protocol):
    def is_expected_head_merged(
        self, pr: int, expected_head: str
    ) -> bool: ...


@runtime_checkable
class BranchUpdateVerifier(Protocol):
    def is_branch_updated(self, pr: int, prior_head: str) -> bool: ...


@runtime_checkable
class ChecksRerunVerifier(Protocol):
    def workflow_run_attempts(
        self, head: str, run_ids: tuple[int, ...]
    ) -> dict[int, int] | None: ...


@runtime_checkable
class WorkflowRunCancelVerifier(Protocol):
    def workflow_run_states(
        self, head: str, run_ids: tuple[int, ...]
    ) -> dict[int, str] | None: ...


@runtime_checkable
class DependencyVerifier(Protocol):
    def has_blocked_by(self, issue: int, blocker: int) -> bool: ...


@runtime_checkable
class ReviewVerifier(Protocol):
    def has_review(self, pr: int, event: str, body: str, head: str) -> bool: ...


class ImmediateGitHubEffectExecutor:
    """Non-durable adapter for isolated tests and explicit compatibility tools.

    Production composition uses :class:`GitHubEffectExecutor`.
    """

    def __init__(self, gateway: Any):
        self.gateway = gateway

    def handles(self, action: Action) -> bool:
        return GitHubEffectExecutor.handles(self, action)

    def _validate_completion(self, action, *, partial=False):
        return GitHubEffectExecutor._validate_completion(self, action, partial=partial)

    def _close_state_matches(self, action):
        return GitHubEffectExecutor._close_state_matches(self, action)

    def execute(self, action: Action):
        if isinstance(action, CloseIssue) and action.completion_guard is None:
            current = self.gateway.get_issue(action.number)
            if current.state == "open":
                action = replace(action, completion_guard=completion_guard(current, action.operation))
        if isinstance(action, Relabel):
            if _is_compound_relabel(action):
                current = self.gateway.get_issue(action.number)
                if (
                    current.updated_at != action.expected_revision
                    and not GitHubEffectExecutor._relabel_partially_applied(
                        self, action
                    )
                ):
                    raise StaleExpectedRevision(
                        f"issue:{action.number}: expected revision no longer "
                        "matches the remote target"
                    )
            GitHubEffectExecutor._apply(self, action)
            verified = GitHubEffectExecutor._reconcile(self, action)
            return (
                True
                if verified is None and not _is_compound_relabel(action)
                else verified
            )
        if isinstance(action, PostComment):
            if (
                not isinstance(self.gateway, CommentVerifier)
                or not self.gateway.has_comment(
                    _TARGET[action.target], action.number, action.body
                )
            ):
                self.gateway.comment(
                    _TARGET[action.target], action.number, action.body
                )
            if not isinstance(self.gateway, CommentVerifier):
                return True
            return self.gateway.has_comment(
                _TARGET[action.target], action.number, action.body
            )
        if isinstance(action, EnableAutoMerge):
            if not action.head:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: expected revision is unavailable"
                )
            return bool(
                self.gateway.enable_auto_merge(
                    action.pr, action.method, action.head
                )
            )
        if isinstance(action, UpdateBranch):
            if not action.head:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: expected revision is unavailable"
                )
            return bool(self.gateway.update_branch(action.pr, action.head))
        if isinstance(action, RerunFailedChecks):
            return self.gateway.rerun_failed_checks(action.head, action.run_ids)
        if isinstance(action, CancelWorkflowRuns):
            return self.gateway.cancel_workflow_runs(action.head, action.run_ids)
        if isinstance(action, EscalateCiPending):
            if (
                not isinstance(self.gateway, CommentVerifier)
                or not self.gateway.has_comment("pr", action.pr, action.body)
            ):
                self.gateway.comment("pr", action.pr, action.body)
            if (
                not isinstance(self.gateway, LabelVerifier)
                or not self.gateway.has_label(
                    "pr", action.pr, "needs-human-review"
                )
            ):
                self.gateway.relabel(
                    "pr", action.pr, "ai-pr-open", "needs-human-review"
                )
            return (
                (
                    not isinstance(self.gateway, CommentVerifier)
                    or self.gateway.has_comment("pr", action.pr, action.body)
                )
                and (
                    not isinstance(self.gateway, LabelVerifier)
                    or self.gateway.has_label("pr", action.pr, "needs-human-review")
                )
            )
        return GitHubEffectExecutor._apply(self, action)

    def submit_review(
        self, pr: int, event: str, body: str, expected_head: str
    ) -> bool:
        if not expected_head:
            raise UnknownEffectOutcome(f"pr:{pr}: review head is unavailable")
        current = self.gateway.get_pr(
            pr, mergeability=False, checks=False
        ).head_ref_oid
        if current != expected_head:
            return False
        return bool(
            self.gateway.submit_review(pr, event, body, expected_head)
        )

    def reconcile(self, action: Action) -> bool | None:
        return GitHubEffectExecutor._reconcile(self, action)

    def drain_pending_commands(self) -> int:
        return 0


class GitHubEffectExecutor:
    """Execute only commands first committed to the control store."""

    def __init__(
        self,
        gateway: Any,
        store: Any,
        *,
        repository: str,
        identity: str,
        config_version: str = "",
        retry_ceiling: int = DEFAULT_EFFECT_RETRY_CEILING,
        journal: EventSink | None = None,
        alert_cmd: str = "",
        log: Callable[[str], None] | None = None,
    ):
        if not repository or not identity:
            raise ValueError("effect repository and executor identity are required")
        self.gateway = gateway
        self.store = store
        self.repository = canonical_repository(repository)
        self.identity = identity
        self.config_version = config_version
        self.retry_ceiling = retry_ceiling
        self.journal = journal
        self.alert_cmd = alert_cmd
        self.log = log if log is not None else _stderr_log
        self._unowned_kinds_seen: set[CommandKind] = set()
        self.owned_kinds = _OWNED_COMMAND_KINDS | (
            _REVIEW_ROLE_COMMAND_KINDS if deployment_role(self.repository, identity) == "review" else frozenset()
        )
        self.effects = DurableEffectRunner(
            store,
            DeploymentAuthority(
                store,
                self.repository,
                deployment_role(self.repository, identity),
                identity,
            ),
            executor_identity=identity,
        )

    def handles(self, action: Action) -> bool:
        return isinstance(
            action,
            (
                Relabel,
                PostComment,
                CloseIssue,
                EnableAutoMerge,
                UpdateBranch,
                RerunFailedChecks,
                CancelWorkflowRuns,
                EscalateCiPending,
                AddDependency,
            ),
        )

    def execute(self, action: Action):
        pending = self._pending_for_action(action)
        if pending is not None and _unresolved_workflow_command(pending):
            return self._reconcile_scheduled(pending, raise_unresolved=True)
        return self._execute(action, pending)

    def _reconcile_scheduled(self, pending, *, raise_unresolved: bool) -> bool:
        """Bounded pre-expiry read of an unresolved workflow command (ADR 0015).

        Presence settles the same command; unavailability advances the
        persisted schedule toward its uncertainty gate; only proven absence
        reaches the existing re-execution path. Expired or retired commands
        are read solely by the drain's reconciliation-only path.
        """
        command = pending.command
        state = self.store.command_reconciliation(command.command_id)
        if state is not None and not isinstance(state, ActiveReconciliation):
            if raise_unresolved:
                raise UnknownEffectOutcome(f"{command.command_id}: outcome is reconciliation-only")
            return False
        if state is not None and not reconciliation_due(state, self.store.now()):
            if raise_unresolved:
                raise UnknownEffectOutcome(
                    f"{command.command_id}: unresolved outcome is next read at {state.next_read_at.isoformat()}"
                )
            return False
        started = self.store.now()
        present, error, remote_object_id = self._observe_postcondition_evidence(command)
        ceiling = present is False and state is not None and reexecution_exhausted(state, started)
        if ceiling:
            error = "repeated unknown outcomes reached the re-execution ceiling; absence needs an operator grant"
        elif present is False:
            return self._resume_absent(pending)
        if self.store.record_scheduled_read(
            command.command_id, state, expected_attempts=pending.attempts, read_started_at=started,
            executor_identity=self.identity, observed=present, error=error, reexecution_ceiling=ceiling,
            remote_object_id=remote_object_id,
        ):
            return True
        if raise_unresolved:
            raise UnknownEffectOutcome(f"{command.command_id}: remote outcome remains unknown ({error or 'unverified'})")
        return False

    def _resume_absent(self, pending) -> bool:
        """Proven absence re-enters the command's own execution path."""
        command = pending.command
        if command.kind is CommandKind.SUBMIT_REVIEW:
            return self._run_submission(pending, raise_deferred=False)
        op = review_family_op(command)
        if op is not None:
            return self._execute_review(op, pending)
        return self._execute(action_from_command(command), pending)

    def _check_review(self, op: ReviewFamilyOp, command: LifecycleCommand) -> ReviewCommandCheck:
        """One fresh read of the pull request, classified against the op's accepted guard."""
        assert command.workflow is not None
        observation = observe_pull_request(
            self.gateway, command.workflow.fence.key, self.store.now(),
            reviews=isinstance(op, ReviewDismissalOp),
        )
        return check_review_op(op, observation)

    def _write_review(self, op: ReviewFamilyOp) -> bool:
        """The single remote write; False is GitHub's refusal of this identity's request."""
        if isinstance(op, ReviewDismissalOp):
            if not isinstance(self.gateway, ReviewDismissals):
                raise UnknownEffectOutcome(f"pr:{op.pr}: the executor gateway cannot dismiss reviews")
            return bool(self.gateway.dismiss_review(op.pr, op.review_id, op.message))
        self.gateway.relabel("pr", op.pr, op.remove.value if op.remove is not None else None, op.add.value)
        return True

    def _execute_review(self, op: ReviewFamilyOp, pending) -> bool:
        """Run one review-family obligation with its guard enforced before any write.

        Mirrors `_execute` for a queued command: presence settles without a
        write; a changed guard is a verified refusal (permanent, never a write);
        an unavailable read leaves the command's state as it was. The write
        runs as this executor's own identity; GitHub refusing it is a visible
        permanent failure, never a reason to try another credential (ADR 0015).
        """
        command = pending.command
        if self._retire_if_exhausted(pending):
            return False
        if pending.state == EffectStatus.PERMANENT_FAILURE.value:
            return False
        unresolved = pending.state in {"executing", EffectStatus.UNKNOWN_OUTCOME.value}
        due, schedule = (True, None) if unresolved else self._prewrite_schedule(pending)
        if not due:
            return False
        started = self.store.now()
        check = self._check_review(op, command)
        if unresolved:
            if check.check is ReviewCheck.PRESENT:
                return self.effects.resolve_unknown(command, EffectOutcome(True, EffectStatus.SUCCEEDED))
            if check.check is ReviewCheck.UNAVAILABLE:
                raise UnknownEffectOutcome(f"{command.command_id}: remote outcome remains unknown ({check.detail})")
            self.effects.retry_reconciled(command)
        elif check.check is ReviewCheck.PRESENT:
            return self.effects.execute(command, lambda: EffectOutcome(True, EffectStatus.SUCCEEDED))
        elif check.check is ReviewCheck.UNAVAILABLE:
            self._defer_prewrite(pending, schedule, started, check.detail)
        if check.check is ReviewCheck.REFUSED:
            reason = f"guard changed: {check.detail}"
            self.effects.execute(command, lambda: EffectOutcome(False, EffectStatus.PERMANENT_FAILURE, error=reason))
            self.log(f"lifecycle command {command.command_id} ({command.kind.value} on {command.target}) refused: {reason}")
            return False

        def write_and_verify() -> EffectOutcome[bool]:
            accepted = self._write_review(op)
            after = self._check_review(op, command)
            if after.check is ReviewCheck.PRESENT:
                return EffectOutcome(True, EffectStatus.SUCCEEDED)
            if after.check is ReviewCheck.UNAVAILABLE:
                raise UnknownEffectOutcome(f"{command.command_id}: remote postcondition is unavailable ({after.detail})")
            if after.check is ReviewCheck.REFUSED:
                return EffectOutcome(False, EffectStatus.PERMANENT_FAILURE, error=f"guard changed: {after.detail}")
            if isinstance(op, PullRequestProjectionOp):
                return EffectOutcome(False, EffectStatus.RETRYABLE_FAILURE,
                                     error="GitHub did not confirm the requested remote state")
            if not accepted:
                return EffectOutcome(False, EffectStatus.PERMANENT_FAILURE, error=(
                    "GitHub refused the dismissal for the review identity (missing permission or branch "
                    "protection); credentials are never switched"
                ))
            raise UnknownEffectOutcome(f"{command.command_id}: accepted dismissal is not yet visible")

        return bool(self.effects.execute(command, write_and_verify))

    def _prewrite_schedule(self, pending) -> tuple[bool, ActiveReconciliation | None]:
        """Whether a pending command's guard read is due now, and its pre-write schedule."""
        schedule = self.store.prewrite_read(pending.command.command_id)
        return schedule is None or reconciliation_due(schedule, self.store.now()), schedule

    def _defer_prewrite(self, pending, schedule: ActiveReconciliation | None, started, detail: str) -> NoReturn:
        """An unavailable pre-write guard read: bounded backoff, journaled, gated at the ceiling (ADR 0012)."""
        command = pending.command
        self.store.record_prewrite_unavailable(
            command.command_id, schedule, expected_attempts=pending.attempts, read_started_at=started,
            executor_identity=self.identity, error=detail,
        )
        raise UnknownEffectOutcome(f"{command.command_id}: pre-write observation unavailable ({detail})")

    def _execute(self, action: Action, pending):
        if pending is None and isinstance(action, CloseIssue) and self._reconcile(action) is True:
            return True
        if isinstance(action, CloseIssue) or (
            isinstance(action, Relabel) and action.target is Target.ISSUE
            and action.remove is Label.AI_WORKING and not action.harness_signal
        ):
            if action.completion_guard is None and pending is None:
                current = self.gateway.get_issue(action.number)
                operation = (action.operation if isinstance(action, CloseIssue)
                             else LifecycleOperation.COMPLETE_RESOLVE)
                if not isinstance(issue_eligibility(current, operation), AllowedEligibility):
                    raise StaleExpectedRevision(f"issue:{action.number}: completion is no longer eligible")
                action = replace(action, completion_guard=completion_guard(current, operation))
        if pending is None:
            command = self._command(action)
            pending = self.effects.enqueue(command)
        else:
            command = pending.command
            action = action_from_command(command)
        if pending.state == EffectStatus.SUCCEEDED.value:
            current = self._reconcile(action, command)
            if current is True:
                return True
            if current is None:
                raise UnknownEffectOutcome(
                    f"{command.command_id}: succeeded effect cannot be verified"
                )
            pending = self.effects.enqueue_occurrence(command)
            command = pending.command
        if self._retire_if_exhausted(pending):
            return False
        if pending.state == EffectStatus.PERMANENT_FAILURE.value:
            return False
        if pending.state == "pending" and not self._revision_matches(command):
            self.effects.execute(
                command,
                lambda: EffectOutcome(
                    False,
                    EffectStatus.PERMANENT_FAILURE,
                    error="expected revision no longer matches remote target",
                ),
            )
            raise StaleExpectedRevision(
                f"{command.target}: expected revision "
                f"{command.expected_revision!r} no longer matches the remote "
                "target — decision stale, discarded; next tick re-decides"
            )
        repairing_partial_close = (
            pending.state != "pending"
            and (
                isinstance(action, CloseIssue) and self._close_effect_partially_applied(action)
                or isinstance(action, Relabel) and self._relabel_partially_applied(action)
            )
        )
        if pending.state in {
            "executing",
            EffectStatus.UNKNOWN_OUTCOME.value,
        }:
            reconciled = self._reconcile(action, command)
            if reconciled is True:
                return self.effects.resolve_unknown(
                    command,
                    EffectOutcome(True, EffectStatus.SUCCEEDED),
                )
            if reconciled is None:
                raise UnknownEffectOutcome(
                    f"{command.command_id}: remote outcome remains unknown"
                )
            self.effects.retry_reconciled(command)
        else:
            reconciled = self._reconcile(action, command)
            if reconciled is True:
                # A deployment cutover or target revision change can produce a
                # new command identity for an effect that already landed.
                return self.effects.execute(
                    command,
                    lambda: EffectOutcome(
                        True,
                        EffectStatus.SUCCEEDED,
                    ),
                )
        if (
            pending.state != "pending"
            and not repairing_partial_close
            and not self._revision_matches(command)
        ):
            self.effects.execute(
                command,
                lambda: EffectOutcome(
                    False,
                    EffectStatus.PERMANENT_FAILURE,
                    error="expected revision no longer matches remote target",
                ),
            )
            raise StaleExpectedRevision(
                f"{command.target}: expected revision "
                f"{command.expected_revision!r} no longer matches the remote "
                "target — decision stale, discarded; next tick re-decides"
            )

        def apply_and_verify() -> EffectOutcome[bool]:
            applied = self._apply(action, command)
            if isinstance(action, (CancelWorkflowRuns, RerunFailedChecks)):
                # These effects' postcondition is that GitHub ACCEPTED the
                # request for runs still belonging to the observed head — which
                # is what `_apply` returns. Verifying convergence here instead
                # fails every successful call, because GitHub applies both
                # asynchronously: a cancelled run comes down later (adversarial
                # review S1), and `run_attempt` increments later (issue #177 —
                # on xnmp/tauri-explorer 2026-08-08 EVERY free re-run landed
                # yet was receipted `ok:false` / "did not confirm", because the
                # counter caught up ~20s after the POST). The harness tracks
                # convergence itself: the cancel in the await-cancel step
                # bounded by `cancel_grace_breached`, the re-run through the
                # checks flipping to PENDING and the one-shot head marker.
                outcome = bool(applied)
            elif not applied:
                outcome = False
            else:
                outcome = self._reconcile(action, command)
            if outcome is None:
                raise UnknownEffectOutcome(
                    f"{command.command_id}: remote postcondition is unavailable"
                )
            if outcome:
                return EffectOutcome(True, EffectStatus.SUCCEEDED)
            if isinstance(action, RerunFailedChecks):
                # Terminal, not retryable (ADR 0012 ceiling, issue #177). A
                # refusal means the captured runs no longer match the observed
                # head — a fact no retry changes — and the free re-run's one
                # attempt for this head is already spent (the marker is written
                # before the call). Redriving it would re-POST a re-run the
                # decision layer will never ask for again; the paid FIX_CI leg
                # is the designed successor.
                return EffectOutcome(
                    False,
                    EffectStatus.PERMANENT_FAILURE,
                    error=(
                        "captured failed runs are no longer rerunnable at "
                        "this head"
                    ),
                )
            return EffectOutcome(
                False,
                EffectStatus.RETRYABLE_FAILURE,
                error="GitHub did not confirm the requested remote state",
            )

        return self.effects.execute(command, apply_and_verify)

    def submit_review(
        self, pr: int, event: str, body: str, expected_head: str
    ) -> bool:
        if not expected_head:
            raise UnknownEffectOutcome(f"pr:{pr}: review head is unavailable")
        payload = {
            "pr": pr,
            "event": event,
            "body": body,
            "head": expected_head,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"submit-review:{encoded}".encode()).hexdigest()
        command = LifecycleCommand(
            CommandId(digest),
            f"github:{digest}",
            CommandKind.SUBMIT_REVIEW,
            f"pr:{pr}",
            expected_head,
            payload,
            self.config_version,
            self.repository,
            self.identity,
        )
        pending = self.effects.enqueue(command)
        return self._run_review_command(pending)

    def submit_bound_review(
        self, pr: int, event: str, body: str, expected_head: str, *,
        workflow: WorkflowBinding, reviewer: str, patch_id: str | None, escalate_cause: str | None = None,
    ) -> bool:
        """SUBMIT_REVIEW under an active review-rejection workflow (ADR 0015).

        The command carries the captured binding, so the store fences it by
        generation at enqueue and begin, and its payload records the reviewed
        head, patch identity and any escalation cause: the SUCCEEDED receipt
        of this exact command is the only evidence a later RECORD_REVIEW or
        ceiling decision may use. `reviewer` is the review identity's login:
        reconciliation recognizes this command's review by it, never by
        content. Unbound submissions keep `submit_review`.
        """
        if not isinstance(workflow, WorkflowBinding) or workflow.authority.family is not WorkflowFamily.REVIEW_REJECTIONS:
            raise ValueError("a bound review submission requires a review-rejection binding")
        if workflow.fence.key.target.number != pr:
            raise ValueError("a bound review submission belongs to its workflow's pull request")
        if not expected_head:
            raise UnknownEffectOutcome(f"pr:{pr}: review head is unavailable")
        op = BoundReviewSubmissionOp(pr, event, body, expected_head, reviewer, patch_id, escalate_cause)
        payload = op.payload()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"submit-review:{encoded}:{encode_workflow(workflow)}".encode()).hexdigest()
        command = LifecycleCommand(
            CommandId(digest), f"github:{digest}", CommandKind.SUBMIT_REVIEW, f"pr:{pr}", expected_head, payload,
            self.config_version, self.repository, self.identity, workflow=workflow,
        )
        pending = self.effects.enqueue(command)
        if _unresolved_workflow_command(pending):
            return self._reconcile_scheduled(pending, raise_unresolved=True)
        return self._run_bound_review_command(op, pending, raise_deferred=True)

    def _run_submission(self, pending, *, raise_deferred: bool) -> bool:
        op = bound_submission_op(pending.command)
        if op is None:
            return self._run_review_command(pending)
        return self._run_bound_review_command(op, pending, raise_deferred=raise_deferred)

    def _check_bound_submission(self, op: BoundReviewSubmissionOp, command: LifecycleCommand) -> ReviewCommandCheck:
        """One read of every review, matched to this command's own submission."""
        if not isinstance(self.gateway, ReviewReads):
            return ReviewCommandCheck(ReviewCheck.UNAVAILABLE, "the executor gateway cannot read pull request reviews")
        try:
            reviews = self.gateway.pull_request_reviews(op.pr)
        except sqlite3.Error:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed read is unavailable evidence, never absence
            reviews = PartialReviews(f"{type(exc).__name__}: {exc}"[:512])
        return check_bound_submission(op, reviews, command_id=str(command.command_id))

    def _run_bound_review_command(self, op: BoundReviewSubmissionOp, pending, *, raise_deferred: bool) -> bool:
        """Submit a bound review at most once per proven absence (ADR 0015).

        Its body carries a final-line marker naming this command, and only a
        review by the review identity with that marker is its own
        (`check_bound_submission`); the receipt names that review. A read that cannot decide never buys a write.
        """
        command = pending.command
        if pending.state == EffectStatus.SUCCEEDED.value:
            # Reviews are never deleted, only dismissed: the receipt is final.
            return True
        if self._retire_if_exhausted(pending):
            return False
        if pending.state == EffectStatus.PERMANENT_FAILURE.value:
            return False
        due, schedule = True, None
        if pending.state in {"executing", EffectStatus.UNKNOWN_OUTCOME.value}:
            check = self._check_bound_submission(op, command)
            if check.check is ReviewCheck.PRESENT:
                return self.effects.resolve_unknown(command, EffectOutcome(
                    True, EffectStatus.SUCCEEDED, remote_object_id=check.remote_object_id))
            if check.check is not ReviewCheck.APPLIES:
                raise UnknownEffectOutcome(f"{command.command_id}: remote outcome remains unknown ({check.detail})")
            self.effects.retry_reconciled(command)
        else:
            due, schedule = self._prewrite_schedule(pending)
        if not due:
            if raise_deferred:
                raise UnknownEffectOutcome(f"{command.command_id}: pre-write read is deferred on its schedule")
            return False
        started = self.store.now()
        try:
            current = self._current_revision(command)
        except sqlite3.Error:
            raise
        except Exception as exc:  # noqa: BLE001 - bounded pre-write evidence
            self._defer_prewrite(pending, schedule, started, f"{type(exc).__name__}: {exc}"[:512])
        if current != op.head:
            return bool(self.effects.execute(command, lambda: EffectOutcome(
                False, EffectStatus.PERMANENT_FAILURE, error="review head changed before submission")))

        def submit_and_verify() -> EffectOutcome[bool]:
            if not self.gateway.submit_review(op.pr, op.event, mark_review_body(op.body, command.command_id), op.head):
                return EffectOutcome(False, EffectStatus.PERMANENT_FAILURE, error="GitHub rejected review submission")
            after = self._check_bound_submission(op, command)
            if after.check is ReviewCheck.PRESENT:
                return EffectOutcome(True, EffectStatus.SUCCEEDED, remote_object_id=after.remote_object_id)
            raise UnknownEffectOutcome(
                f"{command.command_id}: submitted review is not verifiable ({after.detail or 'not yet visible'})")

        return bool(self.effects.execute(command, submit_and_verify))

    def _run_review_command(self, pending) -> bool:
        command = pending.command
        payload = command.payload
        pr = int(payload["pr"])
        event = str(payload["event"])
        body = str(payload["body"])
        head = str(payload["head"])
        if pending.state == EffectStatus.SUCCEEDED.value:
            if not isinstance(self.gateway, ReviewVerifier):
                raise UnknownEffectOutcome(
                    f"{command.command_id}: succeeded review cannot be verified"
                )
            if self.gateway.has_review(pr, event, body, head):
                return True
            pending = self.effects.enqueue_occurrence(command)
            command = pending.command
        if self._retire_if_exhausted(pending):
            return False
        if pending.state == EffectStatus.PERMANENT_FAILURE.value:
            return False
        if pending.state in {"executing", EffectStatus.UNKNOWN_OUTCOME.value}:
            if not isinstance(self.gateway, ReviewVerifier):
                raise UnknownEffectOutcome(
                    f"{command.command_id}: review outcome requires reconciliation"
                )
            if self.gateway.has_review(pr, event, body, head):
                return self.effects.resolve_unknown(
                    command,
                    EffectOutcome(True, EffectStatus.SUCCEEDED),
                )
            self.effects.retry_reconciled(command)
        elif isinstance(self.gateway, ReviewVerifier):
            try:
                present = self.gateway.has_review(pr, event, body, head)
            except ReviewReadUnavailable as exc:
                # Fail closed without a write, and say why: this is GitHub
                # refusing the read, not an infra death. The command stays
                # pending; the caller's unproductive-round bound escalates a
                # refusal that persists.
                self.log(f"lifecycle command {command.command_id} (submit_review on {command.target}) "
                         f"not submitted: {exc}")
                return False
            if present:
                # Deployment-scoped identities change the command digest. Reconcile
                # a matching pre-cutover review before the new identity can submit
                # the same GitHub review again.
                return self.effects.execute(
                    command,
                    lambda: EffectOutcome(True, EffectStatus.SUCCEEDED),
                )
        if not self._revision_matches(command):
            return self.effects.execute(
                command,
                lambda: EffectOutcome(
                    False,
                    EffectStatus.PERMANENT_FAILURE,
                    error="review head changed before submission",
                ),
            )

        def submit_and_verify() -> EffectOutcome[bool]:
            applied = bool(
                self.gateway.submit_review(pr, event, body, head)
            )
            if applied:
                if not isinstance(self.gateway, ReviewVerifier):
                    raise UnknownEffectOutcome(
                        f"{command.command_id}: review outcome requires "
                        "reconciliation"
                    )
                if not self.gateway.has_review(pr, event, body, head):
                    raise UnknownEffectOutcome(
                        f"{command.command_id}: submitted review is not "
                        "visible on the captured revision"
                    )
            return EffectOutcome(
                applied,
                (
                    EffectStatus.SUCCEEDED
                    if applied
                    else EffectStatus.PERMANENT_FAILURE
                ),
                error=(
                    None
                    if applied
                    else "GitHub rejected review submission"
                )
            )

        return self.effects.execute(command, submit_and_verify)

    def drain_pending_commands(self) -> int:
        """Resume durable commands after restart without blindly repeating ambiguity."""

        completed = 0
        for pending in self.store.runnable_commands(
            repository=self.repository,
            executor_identity=self.identity,
            diagnose=self.log,
        ):
            command = pending.command
            if command.kind not in self.owned_kinds or (
                command.kind in _REVIEW_ROLE_COMMAND_KINDS and not is_review_family(command)
            ):
                self._note_unowned_kind(command.kind)
                continue
            try:
                reconciliation = self.store.command_reconciliation(command.command_id)
                if isinstance(reconciliation, (ReconciliationOnly, RetiredUncertainCommand)):
                    applied = self._reconcile_uncertain(command, reconciliation, pending.attempts)
                elif _unresolved_workflow_command(pending):
                    applied = self._reconcile_scheduled(pending, raise_unresolved=False)
                elif command.kind is CommandKind.SUBMIT_REVIEW:
                    applied = self._run_submission(pending, raise_deferred=False)
                elif (op := review_family_op(command)) is not None:
                    applied = self._execute_review(op, pending)
                else:
                    applied = self.execute(action_from_command(command))
            except sqlite3.Error:
                # Storage failure invalidates this recovery pass, unlike one
                # malformed or unavailable remote target.
                raise
            except Exception as exc:  # noqa: BLE001 — see below
                # One command must not stop the drain: an unreconstructable
                # payload or a single wedged target would otherwise strand
                # every other queued write behind it. But it must not be
                # SILENT either — a swallowed GhUnavailable here was
                # indistinguishable from a clean drain, which is how the
                # unbounded 403 loop stayed invisible. The command keeps its
                # state, so the next tick retries it under the same ceiling.
                self.log(
                    f"lifecycle command {command.command_id} "
                    f"({command.kind.value} on {command.target}) could not be "
                    f"drained: {type(exc).__name__}: {exc}"
                )
                continue
            if applied:
                completed += 1
        return completed

    def _observe_postcondition(self, command: LifecycleCommand) -> tuple[bool | None, str]:
        present, error, _remote_object_id = self._observe_postcondition_evidence(command)
        return present, error

    def _observe_postcondition_evidence(self, command: LifecycleCommand) -> tuple[bool | None, str, str | None]:
        """One complete harness read; failure is unavailable, never absence.

        The third value names the remote object that proves presence, when
        the read can identify it (a bound review submission's review id).
        """
        try:
            op = review_family_op(command)
            submission = bound_submission_op(command)
            if submission is not None:
                found = self._check_bound_submission(submission, command)
                if found.check is ReviewCheck.UNAVAILABLE:
                    return None, found.detail, None
                present = found.check is ReviewCheck.PRESENT
                return present, "", found.remote_object_id if present else None
            if command.kind is CommandKind.SUBMIT_REVIEW:
                payload = command.payload
                present = self.gateway.has_review(int(payload["pr"]), str(payload["event"]), str(payload["body"]), str(payload["head"])) if isinstance(self.gateway, ReviewVerifier) else None
            elif op is not None:
                # A refused guard is absence of this effect; every write path
                # re-checks the guard first, so it never buys a write.
                check = self._check_review(op, command)
                if check.check is ReviewCheck.UNAVAILABLE:
                    return None, check.detail, None
                present = check.check is ReviewCheck.PRESENT
            else:
                action = action_from_command(command)
                if isinstance(action, Relabel) and _is_compound_relabel(action):
                    # Current postcondition evidence does not authorize a
                    # mutation. A changed guard cannot hide retired debt.
                    issue = self.gateway.get_issue(action.number)
                    present = (
                        all(label in issue.labels for label in action.added_labels)
                        and all(label not in issue.labels for label in action.removed_labels)
                        and (not action.comment or any(item.body == action.comment for item in issue.comments))
                    )
                else:
                    present = self._reconcile(action, command)
        except sqlite3.Error:
            raise
        except Exception as exc:  # noqa: BLE001 — bounded durable failed-read evidence
            return None, f"{type(exc).__name__}: {exc}", None
        return present, "", None

    def _current_revision(self, command: LifecycleCommand) -> str:
        number = int(command.target.split(":", 1)[1])
        current = (
            self.gateway.get_pr(number, mergeability=False, checks=False).head_ref_oid
            if command.target.startswith("pr:") else self.gateway.get_issue(number).updated_at
        )
        if not current:
            raise UnknownEffectOutcome(f"{command.command_id}: current revision is unavailable")
        return str(current)

    def _reconcile_uncertain(
        self, command: LifecycleCommand,
        state: ReconciliationOnly | RetiredUncertainCommand, attempts: int,
    ) -> bool:
        """Read an uncertain command, spending an operator grant if one is open.

        Everything runs inside the command's exclusive span (a live attempt
        excludes it) and the shared effect authority, which operator grant
        acceptance excludes, so whether a grant exists is decided atomically
        with the read. Reads are stamped at their start.

        With a grant (ADR 0015): present settles without spending it;
        unavailable keeps it open under backoff up to its ceiling; absent
        re-evaluates the original decision's guards (never adopting a newer
        revision) and the store makes the grant's single spend decision. The
        write's only receipts are verified success or unknown outcome.
        """
        if command.workflow is None:
            raise ValueError("uncertain command reconciliation requires a workflow-bound command")
        settled = False

        def record(started, present: bool | None, error: str, remote_object_id: str | None = None) -> bool:
            return self.store.record_reconciliation_read(
                command.command_id, state, expected_attempts=attempts, read_started_at=started,
                executor_identity=self.identity, observed=present, error=error, remote_object_id=remote_object_id,
            )

        def begin() -> bool:
            nonlocal settled
            grant = (self.store.retry_authorization(command.command_id)
                     if isinstance(state, ReconciliationOnly) else None)
            started = self.store.now()
            present, error, remote_object_id = self._observe_postcondition_evidence(command)
            if grant is None or present is not False:
                settled = record(started, present, error, remote_object_id)
                return False
            try:
                guard_holds = self._decision_applies(command)
                revision = self._current_revision(command)
                outcome = self.store.begin_authorized_retry(
                    command.command_id, grant.authorization_id, expected=state, expected_attempts=attempts,
                    read=RetryAbsenceRead(started, revision, guard_holds),
                    executor_identity=self.identity,
                )
            except sqlite3.Error:
                raise
            except Exception as exc:  # noqa: BLE001 — a transient fault counts toward the grant's ceiling
                self.log(f"lifecycle command {command.command_id} retry spend unavailable: {type(exc).__name__}: {exc}")
                record(started, None, f"retry spend unavailable: {type(exc).__name__}: {exc}")
                return False
            if isinstance(outcome, RetryDeclined):
                self.log(f"lifecycle command {command.command_id} retry grant {grant.authorization_id} "
                         f"{outcome.closed.disposition.value}: {outcome.closed.reason}")
            return isinstance(outcome, RetryBegan)

        def write() -> EffectOutcome[bool]:
            op = review_family_op(command)
            submission = bound_submission_op(command)
            if submission is not None:
                applied = bool(self.gateway.submit_review(
                    submission.pr, submission.event, mark_review_body(submission.body, command.command_id),
                    submission.head))
            elif command.kind is CommandKind.SUBMIT_REVIEW:
                payload = command.payload
                applied = bool(self.gateway.submit_review(int(payload["pr"]), str(payload["event"]), str(payload["body"]), str(payload["head"])))
            elif op is not None:
                applied = self._write_review(op)
            else:
                applied = bool(self._apply(action_from_command(command), command))
            verified, error, remote_object_id = (
                self._observe_postcondition_evidence(command) if applied else (False, "", None))
            if verified is True:
                return EffectOutcome(True, EffectStatus.SUCCEEDED, remote_object_id=remote_object_id)
            raise UnknownEffectOutcome(
                f"{command.command_id}: authorized retry did not verify its postcondition"
                + (f" ({error})" if error else "" if applied else " (write refused)")
            )

        written = self.effects.execute_reconciled(command, begin, write)
        return settled or bool(written)

    def _note_unowned_kind(self, kind: CommandKind) -> None:
        """Report a queued kind belonging to another executor once per process.

        Once, not per tick: the command legitimately stays pending until its
        owner drains it, so a per-tick line is pure noise on a log an operator
        has to read (#156). Once still surfaces a kind that NO executor owns.
        """
        if kind in self._unowned_kinds_seen:
            return
        self._unowned_kinds_seen.add(kind)
        self.log(
            f"lifecycle command kind {kind.value} is not owned by the GitHub "
            "effect executor; leaving it queued for its owner"
        )

    def _retire_if_exhausted(self, pending) -> bool:
        """Retire a command that has burned its retry ceiling, once.

        True means the caller must stop: the command is now (or was already
        moved to) `permanent_failure`. Infra failures never arrive here — they
        receipt `unknown_outcome`, which the ceiling predicate ignores.
        """
        command = pending.command
        if not effect_ceiling_reached(
            pending.state, pending.refusals, self.retry_ceiling
        ):
            return False
        retired = self.effects.exhaust(
            command,
            exhausted_effect_reason(
                command.kind.value,
                command.target,
                pending.refusals,
                self.retry_ceiling,
            ),
        )
        if not retired:
            # Another process already retired (and escalated) this command.
            return True
        advice = exhausted_effect_advice(command.kind.value, command.target)
        self.log(advice)
        if self.journal is not None:
            self.journal.emit(
                TelemetryEvent(
                    LevelChanged(
                        f"command:{command.command_id}:effect_retry",
                        "exhausted",
                        "retry_ceiling",
                        {
                            "kind": command.kind.value,
                            "target": command.target,
                            "attempts": pending.attempts,
                            "refusals": pending.refusals,
                            "ceiling": self.retry_ceiling,
                            "executor_identity": self.identity,
                        },
                    ),
                    _target_correlation(command.target),
                )
            )
        alert(
            self.alert_cmd,
            json.dumps(
                {
                    "event": "effect_retry_exhausted",
                    "command_id": str(command.command_id),
                    "kind": command.kind.value,
                    "target": command.target,
                    "attempts": pending.attempts,
                    "refusals": pending.refusals,
                    "ceiling": self.retry_ceiling,
                    "identity": self.identity,
                    "advice": advice,
                },
                sort_keys=True,
            ),
        )
        return True

    def _apply(
        self,
        action: Action,
        command: LifecycleCommand | None = None,
    ):
        """Apply one effect, re-validating its completion facts between writes."""
        if isinstance(action, Relabel):
            if _is_compound_relabel(action):
                self._validate_completion(action, partial=True)
                current = self.gateway.get_issue(action.number)
                if action.comment and not any(
                    item.body == action.comment for item in current.comments
                ):
                    self.gateway.comment("issue", action.number, action.comment)
                    self._validate_completion(action, partial=True)
                for label in action.added_labels:
                    current = self.gateway.get_issue(action.number)
                    if label not in current.labels:
                        self.gateway.relabel(
                            "issue", action.number, None, label.value
                        )
                        self._validate_completion(action, partial=True)
                for label in action.removed_labels:
                    current = self.gateway.get_issue(action.number)
                    if label in current.labels:
                        self.gateway.relabel(
                            "issue", action.number, label.value, None
                        )
                        self._validate_completion(action, partial=True)
                return True
            self._validate_completion(action, partial=True)
            removals = action.removed_labels
            self.gateway.relabel(
                _TARGET[action.target],
                action.number,
                removals[0].value if removals else None,
                action.add.value if action.add else None,
            )
            for label in removals[1:]:
                self._validate_completion(action, partial=True)
                self.gateway.relabel(
                    _TARGET[action.target], action.number, label.value, None
                )
            return True
        elif isinstance(action, PostComment):
            self.gateway.comment(_TARGET[action.target], action.number, action.body)
            return True
        elif isinstance(action, CloseIssue):
            if self._close_state_matches(action):
                if self.reconcile(action) is True:
                    return True
                raise UnknownEffectOutcome("closed target has an incomplete disposition; reconcile remaining effects")
            self._validate_completion(action, partial=True)
            has_comment = (
                self.gateway.has_comment("issue", action.number, action.comment)
                if isinstance(self.gateway, CommentVerifier)
                else any(c.body == action.comment for c in self.gateway.get_issue(action.number).comments)
            )
            if action.comment and not has_comment:
                self.gateway.comment("issue", action.number, action.comment)
            self._validate_completion(action, partial=True)
            for label in action.add_labels:
                self.gateway.relabel("issue", action.number, None, label.value)
                self._validate_completion(action, partial=True)
            for label in action.remove_labels:
                self.gateway.relabel("issue", action.number, label.value, None)
                self._validate_completion(action, partial=True)
            if not self._close_state_matches(action):
                self.gateway.close_issue(action.number, action.reason, "")
            return True
        elif isinstance(action, EnableAutoMerge):
            if command is None or not command.expected_revision:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: auto-merge revision is unavailable"
                )
            return bool(
                self.gateway.enable_auto_merge(
                    action.pr,
                    action.method,
                    command.expected_revision,
                )
            )
        elif isinstance(action, UpdateBranch):
            if command is None or not command.expected_revision:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: branch-update revision is unavailable"
                )
            return bool(
                self.gateway.update_branch(
                    action.pr, command.expected_revision
                )
            )
        elif isinstance(action, RerunFailedChecks):
            if command is None:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: rerun command evidence is unavailable"
                )
            baseline = _run_attempt_baseline(command, action.run_ids)
            if baseline is None:
                # A refusal, not an ambiguity (issue #177). The baseline is the
                # command's OWN captured payload: if it is missing or malformed
                # it will still be missing or malformed next tick, so raising
                # here parked the command in `unknown_outcome` and every drain
                # re-raised it — a loop with no ceiling, because unknown
                # outcomes do not count refusals. Refusing is terminal for this
                # head, which the free re-run has already spent.
                return None
            return self.gateway.rerun_failed_checks(
                action.head, action.run_ids, baseline
            )
        elif isinstance(action, CancelWorkflowRuns):
            # `()` is SUCCESS, not a refusal: it means every targeted run was
            # already completed, which is exactly the postcondition asked for.
            # Only None (the head-SHA guard, or an untrustworthy read) is a
            # failure. `_reconcile` verifies the real state either way.
            return self.gateway.cancel_workflow_runs(
                action.head, action.run_ids
            ) is not None
        elif isinstance(action, EscalateCiPending):
            if not isinstance(self.gateway, CommentVerifier) or not self.gateway.has_comment(
                "pr", action.pr, action.body
            ):
                self.gateway.comment("pr", action.pr, action.body)
            if not isinstance(self.gateway, LabelVerifier) or not self.gateway.has_label(
                "pr", action.pr, "needs-human-review"
            ):
                self.gateway.relabel(
                    "pr", action.pr, "ai-pr-open", "needs-human-review"
                )
            return True
        elif isinstance(action, AddDependency):
            self.gateway.add_blocked_by(action.issue, action.blocker)
            return True
        else:
            raise TypeError(f"unsupported remote effect {action!r}")

    def _reconcile(
        self, action: Action, command: LifecycleCommand | None = None
    ) -> bool | None:
        if isinstance(action, Relabel):
            if _is_compound_relabel(action):
                self._validate_completion(action, partial=True)
                current = self.gateway.get_issue(action.number)
                return (
                    all(label in current.labels for label in action.added_labels)
                    and all(
                        label not in current.labels
                        for label in action.removed_labels
                    )
                    and (
                        not action.comment
                        or any(
                            item.body == action.comment
                            for item in current.comments
                        )
                    )
                )
            if not isinstance(self.gateway, LabelVerifier):
                return None
            added = (
                action.add is None
                or self.gateway.has_label(
                    _TARGET[action.target], action.number, action.add.value
                )
            )
            removed = all(
                not self.gateway.has_label(
                    _TARGET[action.target], action.number, label.value
                )
                for label in action.removed_labels
            )
            return added and removed
        if isinstance(action, PostComment):
            if not isinstance(self.gateway, CommentVerifier):
                return None
            return self.gateway.has_comment(
                _TARGET[action.target], action.number, action.body
            )
        if isinstance(action, CloseIssue):
            if not isinstance(self.gateway, IssueClosureVerifier):
                return None
            if not self._close_state_matches(action):
                return False
            if action.remove_labels or action.add_labels:
                current = self.gateway.get_issue(action.number)
                if any(label in current.labels for label in action.remove_labels) or any(
                    label not in current.labels for label in action.add_labels
                ):
                    return False
            if not action.comment:
                return True
            if not isinstance(self.gateway, CommentVerifier):
                return None
            return self.gateway.has_comment(
                "issue", action.number, action.comment
            )
        if isinstance(action, EscalateCiPending):
            if not isinstance(
                self.gateway, CommentVerifier
            ) or not isinstance(self.gateway, LabelVerifier):
                return None
            return self.gateway.has_comment(
                "pr", action.pr, action.body
            ) and self.gateway.has_label(
                "pr", action.pr, "needs-human-review"
            )
        if isinstance(action, EnableAutoMerge):
            if (
                command is None
                or not command.expected_revision
                or not isinstance(self.gateway, PullRequestMergeVerifier)
            ):
                return None
            return self.gateway.is_expected_head_merged(
                action.pr, command.expected_revision
            )
        if isinstance(action, UpdateBranch):
            if (
                command is None
                or not command.expected_revision
                or not isinstance(self.gateway, BranchUpdateVerifier)
            ):
                return None
            return self.gateway.is_branch_updated(
                action.pr, command.expected_revision
            )
        if isinstance(action, RerunFailedChecks):
            if (
                command is None
                or not isinstance(self.gateway, ChecksRerunVerifier)
            ):
                return None
            baseline = _run_attempt_baseline(command, action.run_ids)
            current = self.gateway.workflow_run_attempts(
                action.head, action.run_ids
            )
            if baseline is None or current is None:
                return None
            advanced: list[bool] = []
            for run_id in action.run_ids:
                prior = baseline.get(run_id)
                attempt = current.get(run_id)
                if (
                    not isinstance(prior, int)
                    or isinstance(prior, bool)
                    or not isinstance(attempt, int)
                ):
                    return None
                advanced.append(attempt > prior)
            if all(advanced):
                return True
            if any(advanced):
                return None
            return False
        if isinstance(action, CancelWorkflowRuns):
            # "Are these runs already down?" — the right question BEFORE acting
            # (skip a run GitHub finished on its own) and on an unknown-outcome
            # replay (if it is not down, re-cancelling is safe: the gateway
            # re-verifies the head SHA and skips completed runs).
            #
            # It is NOT the question after a cancel we just issued: GitHub
            # completes a cancelled run asynchronously, and that lag is the whole
            # reason the await-cancel step exists. See `apply_and_verify`, where
            # acceptance is the postcondition instead (adversarial review S1 —
            # every successful cancel was otherwise receipted as a failure,
            # re-POSTed to the retry ceiling and alerted to the operator).
            if not isinstance(self.gateway, WorkflowRunCancelVerifier):
                return None
            states = self.gateway.workflow_run_states(
                action.head, action.run_ids
            )
            if states is None:
                return None
            return all(status == "completed" for status in states.values())
        if isinstance(action, AddDependency):
            if not isinstance(self.gateway, DependencyVerifier):
                return None
            return self.gateway.has_blocked_by(action.issue, action.blocker)
        return None

    def _close_state_matches(self, action: CloseIssue) -> bool:
        if not isinstance(self.gateway, IssueClosureVerifier):
            return False
        closed, reason = self.gateway.issue_closure(action.number)
        return closed and reason.lower() == action.reason.lower()

    def _close_effect_partially_applied(self, action: CloseIssue) -> bool:
        if self._close_state_matches(action):
            return True
        return (
            bool(action.comment)
            and isinstance(self.gateway, CommentVerifier)
            and self.gateway.has_comment(
                "issue", action.number, action.comment
            )
        )

    def _relabel_partially_applied(self, action: Relabel) -> bool:
        # A hold guard already admits this command's own partial effects.
        if action.completion_guard is None:
            return False
        current = self.gateway.get_issue(action.number)
        guard = action.completion_guard
        current_comments = tuple(
            (item.author, item.body, item.created_at)
            for item in current.comments
        )
        return (
            current.labels != guard.labels or current_comments != guard.comments
        ) and completion_matches(
            guard,
            current,
            removed=action.removed_labels,
            added=action.added_labels,
            comment=action.comment,
        )

    def reconcile(self, action: Action) -> bool | None:
        return self._reconcile(action)

    def _command(self, action: Action) -> LifecycleCommand:
        expected = _expected_revision(action)
        extra: dict[str, object] = {}
        _kind, target = _command_identity(action)
        if is_harness_signal(action):
            # ADR 0012: a harness-owned signal binds NO revision, so
            # `_revision_matches` short-circuits to True and the write is never
            # discarded because the issue moved. Escalations explain why the
            # harness stopped; a human commenting in the same tick must not
            # delete that explanation. See Relabel.harness_signal.
            expected = None
        elif expected is None and target.startswith("pr:"):
            number = int(target.split(":", 1)[1])
            expected = self.gateway.get_pr(
                number, mergeability=False, checks=False
            ).head_ref_oid
            if not expected:
                raise UnknownEffectOutcome(
                    f"{target}: expected revision is unavailable"
                )
        elif expected is None and target.startswith("issue:"):
            number = int(target.split(":", 1)[1])
            expected = self.gateway.get_issue(number).updated_at
            if not expected:
                raise UnknownEffectOutcome(
                    f"{target}: expected revision is unavailable"
                )
        if isinstance(action, RerunFailedChecks):
            if not action.run_ids:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: exact failed workflow runs are unavailable"
                )
            if not isinstance(self.gateway, ChecksRerunVerifier):
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: workflow-run verification is unavailable"
                )
            attempts = self.gateway.workflow_run_attempts(
                action.head, action.run_ids
            )
            if attempts is None:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: failed workflow runs do not match "
                    "the observed head"
                )
            extra["prior_run_attempts"] = {
                str(run_id): attempt
                for run_id, attempt in attempts.items()
            }
        if isinstance(action, CancelWorkflowRuns):
            if not action.run_ids:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: wedged workflow runs are unavailable"
                )
            if not isinstance(self.gateway, WorkflowRunCancelVerifier):
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: workflow-run verification is unavailable"
                )
            states = self.gateway.workflow_run_states(
                action.head, action.run_ids
            )
            if states is None:
                # Includes the head-SHA guard: a run belonging to a newer push
                # is NOT this decision's to cancel, so no command is minted.
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: wedged workflow runs do not match "
                    "the observed head"
                )
            extra["prior_run_states"] = dict(
                (str(run_id), status) for run_id, status in states.items()
            )
        return command_for_action(
            action,
            self.config_version,
            expected_revision=expected,
            payload_extra=extra,
            repository=self.repository,
            executor_identity=self.identity,
        )

    def _pending_for_action(self, action: Action):
        kind, target = _command_identity(action)
        for pending in self.store.pending_commands(
            repository=self.repository,
            executor_identity=self.identity,
        ):
            command = pending.command
            if command.kind is not kind or command.target != target:
                continue
            try:
                candidate = action_from_command(command)
                if isinstance(action, (CloseIssue, Relabel)) and action.completion_guard is None and isinstance(candidate, type(action)):
                    candidate = replace(candidate, completion_guard=None)
                if candidate == action:
                    return pending
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def _hold_guard_holds(self, action: Relabel) -> bool:
        """ADR 0015: the accepted hold facts, never the raw issue revision."""
        guard = action.hold_guard
        assert guard is not None
        current = self.gateway.get_issue(action.number)
        if not isinstance(self.gateway, LinkedPullRequestReads):
            raise UnknownEffectOutcome(f"issue:{action.number}: linked-PR absence cannot be verified")
        return hold_guard_applies(
            guard, current, linked_pr=self.gateway.pr_for_issue(action.number),
            removed=action.removed_labels, added=action.added_labels,
        )

    def _decision_applies(self, command: LifecycleCommand) -> bool:
        """Whether the original decision still applies to an authorized retry.

        Re-evaluates the command's own decision facts and never adopts a newer
        revision (ADR 0015). Its own partially applied effect satisfies them:
        repairing that half-landed write is what the retry is for. A
        completion is judged on its full original guard, conversation
        included: a completion that asks a question must not be re-posted over
        a human's answer. Only a re-arm hold projection uses the semantic hold
        fence. Commands carrying only a raw revision are re-derived from fresh
        observations each tick, so their raw fence stays authoritative.
        A review-family command is judged on its own accepted guard: a
        dismissal on `dismissal_check`, a PR projection on its
        `PullRequestHoldGuard`, and a bound submission on its reviewed head
        of a still-open pull request (its marker's absence was just proven by
        the caller's postcondition read; the store re-checks the binding).
        """
        op = review_family_op(command)
        if op is not None:
            check = self._check_review(op, command)
            if check.check is ReviewCheck.UNAVAILABLE:
                raise UnknownEffectOutcome(f"{command.command_id}: decision facts are unavailable ({check.detail})")
            return check.check is ReviewCheck.APPLIES
        submission = bound_submission_op(command)
        if submission is not None:
            assert command.workflow is not None
            observed = observe_pull_request(self.gateway, command.workflow.fence.key, self.store.now(), reviews=False)
            if isinstance(observed, PullRequestUnavailable):
                raise UnknownEffectOutcome(f"{command.command_id}: decision facts are unavailable ({observed.reason})")
            return observed.state is PullRequestState.OPEN and observed.head_sha == submission.head
        if command.kind in {CommandKind.RELABEL, CommandKind.CLOSE_ISSUE}:
            action = action_from_command(command)
            if isinstance(action, Relabel) and action.hold_guard is not None:
                return self._hold_guard_holds(action)
            if isinstance(action, (Relabel, CloseIssue)) and action.completion_guard is not None:
                guard = action.completion_guard
                current = self.gateway.get_issue(action.number)
                removed, added = (
                    (action.removed_labels, action.added_labels) if isinstance(action, Relabel)
                    else (action.remove_labels, action.add_labels)
                )
                return completion_matches(
                    guard, current, removed=removed, added=added, comment=action.comment,
                ) and isinstance(
                    completion_eligibility(current, guard.operation, action), AllowedEligibility,
                )
        return self._revision_matches(command)

    def _revision_matches(self, command: LifecycleCommand) -> bool:
        if command.kind is CommandKind.RELABEL:
            action = action_from_command(command)
            if isinstance(action, Relabel) and action.hold_guard is not None:
                return self._hold_guard_holds(action)
        if command.kind in {CommandKind.RELABEL, CommandKind.CLOSE_ISSUE}:
            action = action_from_command(command)
            try:
                self._validate_completion(action, partial=isinstance(action, CloseIssue))
            except StaleExpectedRevision:
                return False
        if command.expected_revision is None:
            return True
        number = int(command.target.split(":", 1)[1])
        if command.target.startswith("pr:"):
            current = self.gateway.get_pr(
                number, mergeability=False, checks=False
            ).head_ref_oid
        elif command.target.startswith("issue:"):
            current = self.gateway.get_issue(number).updated_at
        else:
            return False
        if not current:
            raise UnknownEffectOutcome(
                f"{command.command_id}: current revision is unavailable"
            )
        return current == command.expected_revision

    def _validate_completion(self, action: Action, *, partial: bool = False) -> None:
        if not isinstance(action, (Relabel, CloseIssue)):
            return
        if action.completion_guard is None:
            if isinstance(action, CloseIssue) and self.reconcile(action) is not True:
                raise UnknownEffectOutcome("legacy close lacks completion facts; reconcile before any further write")
            return
        guard = action.completion_guard
        current = self.gateway.get_issue(action.number)
        if isinstance(action, CloseIssue) and self._close_state_matches(action):
            if self.reconcile(action) is True:
                return
            raise StaleExpectedRevision(f"issue:{action.number}: closed target has incomplete disposition")
        removed = added = ()
        comment = ""
        if partial:
            if isinstance(action, CloseIssue):
                removed, added, comment = action.remove_labels, action.add_labels, action.comment
            else:
                removed = action.removed_labels
                added = action.added_labels
                comment = action.comment
        if not completion_matches(guard, current, removed=removed, added=added, comment=comment):
            raise StaleExpectedRevision(f"issue:{action.number}: completion facts changed")
        permission = completion_eligibility(current, guard.operation, action)
        if not isinstance(permission, AllowedEligibility):
            raise StaleExpectedRevision(f"issue:{action.number}: completion held: {permission}")

def command_for_action(
    action: Action,
    config_version: str = "",
    *,
    expected_revision: str | None = None,
    payload_extra: Mapping[str, object] | None = None,
    repository: str,
    executor_identity: str,
    workflow: WorkflowBinding | None = None,
) -> LifecycleCommand:
    repository = canonical_repository(repository)
    captured = action.workflow if isinstance(action, (Relabel, PostComment, CloseIssue)) else None
    if captured is not None and workflow is not None and captured != workflow:
        raise ValueError("command cannot replace an action's captured workflow binding")
    workflow = captured if captured is not None else workflow
    kind, target = _command_identity(action)
    action_payload = _jsonable(action)
    action_payload.pop("workflow", None)
    if isinstance(action, Relabel):
        # These optional fields extend the existing relabel command. Omitting
        # empty defaults preserves the digest of commands minted before the
        # compound projection capability existed.
        if not action.adds:
            action_payload.pop("adds", None)
        if not action.comment:
            action_payload.pop("comment", None)
        if action.hold_guard is None:
            action_payload.pop("hold_guard", None)
    payload = {**action_payload, **dict(payload_extra or {})}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    revision = (
        expected_revision
        if expected_revision is not None
        else _expected_revision(action)
    )
    digest = hashlib.sha256(
        (
            f"{repository}:{executor_identity}:{kind.value}:{target}:"
            f"{revision or ''}:{encoded}"
            + (f":{encode_workflow(workflow)}" if workflow is not None else "")
        ).encode()
    ).hexdigest()
    return LifecycleCommand(
        CommandId(digest),
        f"github:{digest}",
        kind,
        target,
        expected_revision=revision,
        payload=payload,
        config_version=config_version,
        repository=repository,
        executor_identity=executor_identity,
        workflow=workflow,
    )


def action_from_command(command: LifecycleCommand) -> Action:
    """Reconstruct the closed Action vocabulary for boot-time command draining.

    Review-family commands never reconstruct: an Action cannot carry their
    guard, so rebuilding one would be an unfenced write (ADR 0015).
    """

    payload = command.payload
    if is_review_family(command) or (command.kind is CommandKind.RELABEL and "pull_request_hold_guard" in payload):
        raise ValueError("review-family commands execute only through their guarded review path")
    if command.kind is CommandKind.RELABEL:
        if {"pr", "head", "body"} <= payload.keys():
            return EscalateCiPending(
                int(payload["pr"]),
                str(payload["head"]),
                str(payload["body"]),
            )
        adds, comment = _relabel_projection_from_payload(payload)
        return Relabel(
            Target(payload["target"]),
            int(payload["number"]),
            Label(str(payload["remove"])) if payload.get("remove") else None,
            Label(str(payload["add"])) if payload.get("add") else None,
            (
                (
                    str(payload["expected_revision"])
                    if payload["expected_revision"] is not None
                    else None
                )
                if "expected_revision" in payload
                else command.expected_revision
            ),
            bool(payload.get("harness_signal", False)),
            tuple(Label(str(label)) for label in payload.get("removes", ())),
            _completion_guard_from_payload(payload.get("completion_guard")),
            workflow=command.workflow,
            adds=adds,
            comment=comment,
            hold_guard=_hold_guard_from_payload(payload.get("hold_guard")),
        )
    if command.kind is CommandKind.POST_COMMENT:
        return PostComment(
            Target(payload["target"]),
            int(payload["number"]),
            str(payload["body"]),
            bool(payload.get("harness_signal", False)),
            workflow=command.workflow,
        )
    if command.kind is CommandKind.CLOSE_ISSUE:
        operation = LifecycleOperation(payload.get("operation", LifecycleOperation.CLOSE.value))
        guard = _completion_guard_from_payload(payload.get("completion_guard"))
        if operation not in {LifecycleOperation.CLOSE, LifecycleOperation.CLOSE_MERGED} or (
            operation is LifecycleOperation.CLOSE_MERGED and payload.get("reason", "completed") != "completed"
        ) or (guard is not None and guard.operation is not operation):
            raise ValueError("inconsistent close completion operation")
        return CloseIssue(
            int(payload["number"]),
            str(payload.get("reason", "completed")),
            str(payload.get("comment", "")),
            expected_revision=payload.get("expected_revision", command.expected_revision),
            completion_guard=guard,
            remove_labels=tuple(Label(label) for label in payload.get("remove_labels", ())),
            add_labels=tuple(Label(label) for label in payload.get("add_labels", ())),
            operation=operation,
            workflow=command.workflow,
        )
    if command.kind is CommandKind.ENABLE_AUTO_MERGE:
        return EnableAutoMerge(
            int(payload["pr"]),
            str(payload["method"]),
            str(
                payload.get("head")
                or command.expected_revision
                or ""
            ),
        )
    if command.kind is CommandKind.UPDATE_BRANCH:
        return UpdateBranch(
            int(payload["pr"]),
            str(
                payload.get("head")
                or command.expected_revision
                or ""
            ),
        )
    if command.kind is CommandKind.RERUN_CHECKS:
        return RerunFailedChecks(
            int(payload["pr"]),
            str(payload["head"]),
            tuple(int(run_id) for run_id in payload.get("run_ids", ())),
        )
    if command.kind is CommandKind.CANCEL_CHECKS:
        return CancelWorkflowRuns(
            int(payload["pr"]),
            str(payload["head"]),
            tuple(int(run_id) for run_id in payload.get("run_ids", ())),
            # Round-trip EVERY field the action carries. `_pending_for_action`
            # matches a queued command by reconstructing it and comparing for
            # equality, so a dropped field makes the rebuilt action differ from
            # the one being executed — and the tick mints a SECOND command for
            # the cancel already queued, every tick (adversarial review N4).
            str(payload.get("decided_at", "") or ""),
        )
    if command.kind is CommandKind.ADD_DEPENDENCY:
        return AddDependency(int(payload["issue"]), int(payload["blocker"]))
    raise ValueError(f"command kind cannot be reconstructed as Action: {command.kind}")


def _command_identity(action: Action) -> tuple[CommandKind, str]:
    if isinstance(action, Relabel):
        return CommandKind.RELABEL, f"{_TARGET[action.target]}:{action.number}"
    if isinstance(action, PostComment):
        return CommandKind.POST_COMMENT, f"{_TARGET[action.target]}:{action.number}"
    if isinstance(action, CloseIssue):
        return CommandKind.CLOSE_ISSUE, f"issue:{action.number}"
    if isinstance(action, EnableAutoMerge):
        return CommandKind.ENABLE_AUTO_MERGE, f"pr:{action.pr}"
    if isinstance(action, UpdateBranch):
        return CommandKind.UPDATE_BRANCH, f"pr:{action.pr}"
    if isinstance(action, RerunFailedChecks):
        return CommandKind.RERUN_CHECKS, f"pr:{action.pr}"
    if isinstance(action, CancelWorkflowRuns):
        return CommandKind.CANCEL_CHECKS, f"pr:{action.pr}"
    if isinstance(action, EscalateCiPending):
        return CommandKind.RELABEL, f"pr:{action.pr}"
    if isinstance(action, AddDependency):
        return CommandKind.ADD_DEPENDENCY, f"issue:{action.issue}"
    raise TypeError(f"action has no remote command representation: {action!r}")


def _expected_revision(action: Action) -> str | None:
    if isinstance(action, (Relabel, CloseIssue)):
        return action.expected_revision
    return (
        action.head
        if isinstance(
            action,
            (
                EnableAutoMerge,
                UpdateBranch,
                RerunFailedChecks,
                CancelWorkflowRuns,
                EscalateCiPending,
            ),
        )
        else None
    )


def _unresolved_workflow_command(pending) -> bool:
    """A migrated-slice command whose outcome is not yet known (ADR 0015)."""
    command = pending.command
    # A bound review submission (review family only) is bounded like any
    # other workflow command; unbound submissions keep their legacy path.
    return (
        command.workflow is not None
        and pending.state in ("executing", EffectStatus.UNKNOWN_OUTCOME.value)
    )


def _is_compound_relabel(action: Relabel) -> bool:
    return bool(action.adds or action.comment)


def _relabel_projection_from_payload(
    payload: Mapping[str, object],
) -> tuple[tuple[Label, ...], str]:
    raw_adds = payload.get("adds", ())
    if not isinstance(raw_adds, (tuple, list)) or not all(
        isinstance(label, str) for label in raw_adds
    ):
        raise ValueError("malformed relabel projection labels")
    raw_comment = payload.get("comment", "")
    if (
        not isinstance(raw_comment, str)
        or len(raw_comment) > 65536
        or "\x00" in raw_comment
    ):
        raise ValueError("malformed relabel projection comment")
    return tuple(Label(label) for label in raw_adds), raw_comment


def _run_attempt_baseline(
    command: LifecycleCommand, run_ids: tuple[int, ...]
) -> dict[int, int] | None:
    raw = command.payload.get("prior_run_attempts")
    if not isinstance(raw, dict):
        return None
    baseline: dict[int, int] = {}
    for run_id in run_ids:
        attempt = raw.get(str(run_id), raw.get(run_id))
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 0
        ):
            return None
        baseline[run_id] = attempt
    return baseline


def _completion_guard_from_payload(raw: object) -> IssueCompletionGuard | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or any(
        not isinstance(raw.get(key), str)
        for key in ("operation", "title", "body", "author", "state_reason")
    ):
        raise ValueError("malformed completion guard")
    labels, comments = raw.get("labels"), raw.get("comments")
    if not isinstance(labels, (tuple, list)) or not all(isinstance(label, str) for label in labels):
        raise ValueError("malformed completion guard labels")
    if not isinstance(comments, (tuple, list)) or not all(
        isinstance(comment, (tuple, list)) and len(comment) == 3
        and all(isinstance(value, str) for value in comment) for comment in comments
    ):
        raise ValueError("malformed completion guard comments")
    return IssueCompletionGuard(
        LifecycleOperation(raw["operation"]), raw["title"], raw["body"], raw["author"],
        raw["state_reason"], frozenset(Label(label) for label in labels),
        tuple(tuple(comment) for comment in comments),
    )


def _hold_guard_from_payload(raw: object) -> IssueHoldGuard | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"state_reason", "labels"}:
        raise ValueError("malformed hold guard")
    labels = raw["labels"]
    if not isinstance(raw["state_reason"], str) or not isinstance(labels, (tuple, list)) or not all(
        isinstance(label, str) for label in labels
    ):
        raise ValueError("malformed hold guard facts")
    return IssueHoldGuard(raw["state_reason"], frozenset(Label(label) for label in labels))


def _jsonable(value: object) -> Any:
    if isinstance(value, frozenset):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported command payload value {type(value).__name__}")
