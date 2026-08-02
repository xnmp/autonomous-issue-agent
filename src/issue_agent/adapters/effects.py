"""ADR 0001's narrow executor for durable GitHub lifecycle commands."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

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
from ..domain.repository import canonical_repository
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

    def execute(self, action: Action):
        if isinstance(action, Relabel):
            self.gateway.relabel(
                _TARGET[action.target],
                action.number,
                action.remove.value if action.remove else None,
                action.add.value if action.add else None,
            )
            if not isinstance(self.gateway, LabelVerifier):
                return True
            return (
                (
                    action.add is None
                    or self.gateway.has_label(
                        _TARGET[action.target], action.number, action.add.value
                    )
                )
                and (
                    action.remove is None
                    or not self.gateway.has_label(
                        _TARGET[action.target], action.number, action.remove.value
                    )
                )
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
            and isinstance(action, CloseIssue)
            and self._close_effect_partially_applied(action)
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
            if isinstance(action, CancelWorkflowRuns):
                # This effect's postcondition is that GitHub ACCEPTED the cancel
                # for runs still belonging to the observed head — which is what
                # `_apply` returns. Verifying "the run is down" here instead
                # would fail every successful cancel, because GitHub takes the
                # run down asynchronously (adversarial review S1). The harness
                # tracks that convergence itself, in the await-cancel step
                # bounded by `cancel_grace_breached`.
                outcome = bool(applied)
            elif not applied:
                outcome = False
            else:
                outcome = self._reconcile(action, command)
            if outcome is None:
                raise UnknownEffectOutcome(
                    f"{command.command_id}: remote postcondition is unavailable"
                )
            retryable = not outcome
            return EffectOutcome(
                outcome,
                (
                    EffectStatus.RETRYABLE_FAILURE
                    if retryable
                    else EffectStatus.SUCCEEDED
                ),
                error=(
                    "GitHub did not confirm the requested remote state"
                    if retryable
                    else None
                ),
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
        elif isinstance(self.gateway, ReviewVerifier) and self.gateway.has_review(
            pr, event, body, head
        ):
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
        for pending in self.store.pending_commands(
            repository=self.repository,
            executor_identity=self.identity,
        ):
            command = pending.command
            if command.kind not in _OWNED_COMMAND_KINDS:
                self._note_unowned_kind(command.kind)
                continue
            try:
                if command.kind is CommandKind.SUBMIT_REVIEW:
                    applied = self._run_review_command(pending)
                else:
                    applied = self.execute(action_from_command(command))
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
        if isinstance(action, Relabel):
            self.gateway.relabel(
                _TARGET[action.target],
                action.number,
                action.remove.value if action.remove else None,
                action.add.value if action.add else None,
            )
            return True
        elif isinstance(action, PostComment):
            self.gateway.comment(_TARGET[action.target], action.number, action.body)
            return True
        elif isinstance(action, CloseIssue):
            if (
                not isinstance(self.gateway, IssueClosureVerifier)
                or (
                    action.comment
                    and not isinstance(self.gateway, CommentVerifier)
                )
            ):
                self.gateway.close_issue(
                    action.number, action.reason, action.comment
                )
                return True
            if action.comment and not self.gateway.has_comment(
                "issue", action.number, action.comment
            ):
                self.gateway.comment("issue", action.number, action.comment)
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
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: rerun attempt baseline is unavailable"
                )
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
            if not isinstance(self.gateway, LabelVerifier):
                return None
            added = (
                action.add is None
                or self.gateway.has_label(
                    _TARGET[action.target], action.number, action.add.value
                )
            )
            removed = (
                action.remove is None
                or not self.gateway.has_label(
                    _TARGET[action.target], action.number, action.remove.value
                )
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
                if action_from_command(command) == action:
                    return pending
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def _revision_matches(self, command: LifecycleCommand) -> bool:
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

def command_for_action(
    action: Action,
    config_version: str = "",
    *,
    expected_revision: str | None = None,
    payload_extra: Mapping[str, object] | None = None,
    repository: str,
    executor_identity: str,
) -> LifecycleCommand:
    repository = canonical_repository(repository)
    kind, target = _command_identity(action)
    payload = {**_jsonable(action), **dict(payload_extra or {})}
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
    )


def action_from_command(command: LifecycleCommand) -> Action:
    """Reconstruct the closed Action vocabulary for boot-time command draining."""

    payload = command.payload
    if command.kind is CommandKind.RELABEL:
        if {"pr", "head", "body"} <= payload.keys():
            return EscalateCiPending(
                int(payload["pr"]),
                str(payload["head"]),
                str(payload["body"]),
            )
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
        )
    if command.kind is CommandKind.POST_COMMENT:
        return PostComment(
            Target(payload["target"]),
            int(payload["number"]),
            str(payload["body"]),
            bool(payload.get("harness_signal", False)),
        )
    if command.kind is CommandKind.CLOSE_ISSUE:
        return CloseIssue(
            int(payload["number"]),
            str(payload.get("reason", "completed")),
            str(payload.get("comment", "")),
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
    if isinstance(action, Relabel):
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


def _jsonable(value: object) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported command payload value {type(value).__name__}")
