"""Narrow privileged executor for durable GitHub lifecycle commands."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from ..domain.actions import (
    Action,
    AddDependency,
    CloseIssue,
    EnableAutoMerge,
    EscalateCiPending,
    PostComment,
    Relabel,
    RerunFailedChecks,
    Target,
    UpdateBranch,
)
from ..domain.labels import Label
from ..domain.contracts import (
    CommandId,
    CommandKind,
    EffectReceipt,
    EffectStatus,
    LifecycleCommand,
)

_TARGET = {Target.ISSUE: "issue", Target.PR: "pr"}


class UnknownEffectOutcome(RuntimeError):
    pass


@runtime_checkable
class LabelVerifier(Protocol):
    def has_label(self, kind: str, number: int, label: str) -> bool: ...


@runtime_checkable
class CommentVerifier(Protocol):
    def has_comment(self, kind: str, number: int, body: str) -> bool: ...


@runtime_checkable
class IssueStateVerifier(Protocol):
    def is_issue_closed(self, issue: int) -> bool: ...


@runtime_checkable
class AutoMergeVerifier(Protocol):
    def is_auto_merge_enabled_or_merged(self, pr: int) -> bool: ...


@runtime_checkable
class BranchUpdateVerifier(Protocol):
    def is_branch_updated(self, pr: int, prior_head: str) -> bool: ...


@runtime_checkable
class ChecksRerunVerifier(Protocol):
    def max_run_attempt(self, head: str) -> int | None: ...


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
            return bool(self.gateway.enable_auto_merge(action.pr, action.method))
        if isinstance(action, RerunFailedChecks):
            return self.gateway.rerun_failed_checks(action.pr)
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

    def submit_review(self, pr: int, event: str, body: str) -> bool:
        return bool(self.gateway.submit_review(pr, event, body))

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
    ):
        if not repository or not identity:
            raise ValueError("effect repository and executor identity are required")
        self.gateway = gateway
        self.store = store
        self.repository = repository
        self.identity = identity
        self.config_version = config_version

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
                EscalateCiPending,
                AddDependency,
            ),
        )

    def execute(self, action: Action):
        pending = self._pending_for_action(action)
        if pending is None:
            command = self._command(action)
            pending = self.store.enqueue_command(command)
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
            pending = self.store.enqueue_command_occurrence(command)
            command = pending.command
        if pending.state == EffectStatus.PERMANENT_FAILURE.value:
            return False
        if pending.state in {
            "executing",
            EffectStatus.UNKNOWN_OUTCOME.value,
        }:
            reconciled = self._reconcile(action, command)
            if reconciled is True:
                self.store.resolve_unknown_effect(
                    self._receipt(command, EffectStatus.SUCCEEDED)
                )
                return True
            if reconciled is None:
                raise UnknownEffectOutcome(
                    f"{command.command_id}: remote outcome remains unknown"
                )
            self.store.retry_reconciled_command(command.command_id)
        if not self._revision_matches(command):
            now = datetime.now(timezone.utc)
            self.store.begin_command(command.command_id)
            self.store.record_effect(
                EffectReceipt(
                    command.command_id,
                    self.identity,
                    now,
                    now,
                    EffectStatus.PERMANENT_FAILURE,
                    error="expected revision no longer matches remote target",
                )
            )
            return False
        self.store.begin_command(command.command_id)
        started = datetime.now(timezone.utc)
        try:
            self._apply(action)
            outcome = self._reconcile(action, command)
            if outcome is None:
                raise UnknownEffectOutcome(
                    f"{command.command_id}: remote postcondition is unavailable"
                )
        except BaseException as exc:
            completed = datetime.now(timezone.utc)
            receipt = EffectReceipt(
                command.command_id,
                self.identity,
                started,
                completed,
                EffectStatus.UNKNOWN_OUTCOME,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.store.record_effect(receipt)
            raise
        retryable = not outcome
        self.store.record_effect(
            EffectReceipt(
                command.command_id,
                self.identity,
                started,
                datetime.now(timezone.utc),
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
        )
        return outcome

    def submit_review(self, pr: int, event: str, body: str) -> bool:
        head = self.gateway.get_pr(
            pr, mergeability=False, checks=False
        ).head_ref_oid
        if not head:
            raise UnknownEffectOutcome(f"pr:{pr}: review head is unavailable")
        payload = {"pr": pr, "event": event, "body": body, "head": head}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"submit-review:{encoded}".encode()).hexdigest()
        command = LifecycleCommand(
            CommandId(digest),
            f"github:{digest}",
            CommandKind.SUBMIT_REVIEW,
            f"pr:{pr}",
            head,
            payload,
            self.config_version,
            self.repository,
            self.identity,
        )
        pending = self.store.enqueue_command(command)
        return self._run_review_command(pending)

    def _run_review_command(self, pending) -> bool:
        command = pending.command
        payload = command.payload
        pr = int(payload["pr"])
        event = str(payload["event"])
        body = str(payload["body"])
        head = str(payload["head"])
        if pending.state == EffectStatus.SUCCEEDED.value:
            if not isinstance(self.gateway, ReviewVerifier) or self.gateway.has_review(
                pr, event, body, head
            ):
                return True
            pending = self.store.enqueue_command_occurrence(command)
            command = pending.command
        if pending.state == EffectStatus.PERMANENT_FAILURE.value:
            return False
        if pending.state in {"executing", EffectStatus.UNKNOWN_OUTCOME.value}:
            if not isinstance(self.gateway, ReviewVerifier):
                raise UnknownEffectOutcome(
                    f"{command.command_id}: review outcome requires reconciliation"
                )
            if self.gateway.has_review(pr, event, body, head):
                self.store.resolve_unknown_effect(
                    self._receipt(command, EffectStatus.SUCCEEDED)
                )
                return True
            self.store.retry_reconciled_command(command.command_id)
        if not self._revision_matches(command):
            now = datetime.now(timezone.utc)
            self.store.begin_command(command.command_id)
            self.store.record_effect(
                EffectReceipt(
                    command.command_id,
                    self.identity,
                    now,
                    now,
                    EffectStatus.PERMANENT_FAILURE,
                    error="review head changed before submission",
                )
            )
            return False
        self.store.begin_command(command.command_id)
        started = datetime.now(timezone.utc)
        try:
            applied = bool(self.gateway.submit_review(pr, event, body))
        except BaseException as exc:
            self.store.record_effect(
                EffectReceipt(
                    command.command_id,
                    self.identity,
                    started,
                    datetime.now(timezone.utc),
                    EffectStatus.UNKNOWN_OUTCOME,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        status = (
            EffectStatus.SUCCEEDED
            if applied
            else EffectStatus.PERMANENT_FAILURE
        )
        self.store.record_effect(
            EffectReceipt(
                command.command_id,
                self.identity,
                started,
                datetime.now(timezone.utc),
                status,
                error=None if applied else "GitHub rejected review submission",
            )
        )
        return applied

    def drain_pending_commands(self) -> int:
        """Resume durable commands after restart without blindly repeating ambiguity."""

        completed = 0
        for pending in self.store.pending_commands(
            repository=self.repository,
            executor_identity=self.identity,
        ):
            command = pending.command
            try:
                if command.kind is CommandKind.SUBMIT_REVIEW:
                    applied = self._run_review_command(pending)
                else:
                    applied = self.execute(action_from_command(command))
            except Exception:
                continue
            if applied:
                completed += 1
        return completed

    def _apply(self, action: Action):
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
            self.gateway.close_issue(action.number, action.reason, action.comment)
            return True
        elif isinstance(action, EnableAutoMerge):
            return bool(self.gateway.enable_auto_merge(action.pr, action.method))
        elif isinstance(action, UpdateBranch):
            self.gateway.update_branch(action.pr)
            return True
        elif isinstance(action, RerunFailedChecks):
            return self.gateway.rerun_failed_checks(action.pr)
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
            if not isinstance(self.gateway, IssueStateVerifier):
                return None
            return self.gateway.is_issue_closed(action.number)
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
            if not isinstance(self.gateway, AutoMergeVerifier):
                return None
            return self.gateway.is_auto_merge_enabled_or_merged(action.pr)
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
            baseline = command.payload.get("prior_run_attempt")
            current = self.gateway.max_run_attempt(action.head)
            if not isinstance(baseline, int) or current is None:
                return None
            return current > baseline
        if isinstance(action, AddDependency):
            if not isinstance(self.gateway, DependencyVerifier):
                return None
            return self.gateway.has_blocked_by(action.issue, action.blocker)
        return None

    def reconcile(self, action: Action) -> bool | None:
        return self._reconcile(action)

    def _command(self, action: Action) -> LifecycleCommand:
        expected = _expected_revision(action)
        extra: dict[str, object] = {}
        if isinstance(action, (EnableAutoMerge, UpdateBranch)):
            expected = self.gateway.get_pr(
                action.pr, mergeability=False, checks=False
            ).head_ref_oid
            if not expected:
                raise UnknownEffectOutcome(
                    f"pr:{action.pr}: expected revision is unavailable"
                )
        if isinstance(action, RerunFailedChecks) and isinstance(
            self.gateway, ChecksRerunVerifier
        ):
            attempt = self.gateway.max_run_attempt(action.head)
            if attempt is not None:
                extra["prior_run_attempt"] = attempt
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
        if not command.target.startswith("pr:"):
            return False
        pr = int(command.target.split(":", 1)[1])
        current = self.gateway.get_pr(pr).head_ref_oid
        if not current:
            raise UnknownEffectOutcome(
                f"{command.command_id}: current revision is unavailable"
            )
        return current == command.expected_revision

    def _receipt(
        self, command: LifecycleCommand, status: EffectStatus
    ) -> EffectReceipt:
        now = datetime.now(timezone.utc)
        return EffectReceipt(
            command.command_id,
            self.identity,
            now,
            now,
            status,
            error=None if status is EffectStatus.SUCCEEDED else status.value,
        )


def command_for_action(
    action: Action,
    config_version: str = "",
    *,
    expected_revision: str | None = None,
    payload_extra: Mapping[str, object] | None = None,
    repository: str,
    executor_identity: str,
) -> LifecycleCommand:
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
        )
    if command.kind is CommandKind.POST_COMMENT:
        return PostComment(
            Target(payload["target"]),
            int(payload["number"]),
            str(payload["body"]),
        )
    if command.kind is CommandKind.CLOSE_ISSUE:
        return CloseIssue(
            int(payload["number"]),
            str(payload.get("reason", "completed")),
            str(payload.get("comment", "")),
        )
    if command.kind is CommandKind.ENABLE_AUTO_MERGE:
        return EnableAutoMerge(int(payload["pr"]), str(payload["method"]))
    if command.kind is CommandKind.UPDATE_BRANCH:
        return UpdateBranch(int(payload["pr"]))
    if command.kind is CommandKind.RERUN_CHECKS:
        return RerunFailedChecks(int(payload["pr"]), str(payload["head"]))
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
    if isinstance(action, EscalateCiPending):
        return CommandKind.RELABEL, f"pr:{action.pr}"
    if isinstance(action, AddDependency):
        return CommandKind.ADD_DEPENDENCY, f"issue:{action.issue}"
    raise TypeError(f"action has no remote command representation: {action!r}")


def _expected_revision(action: Action) -> str | None:
    return action.head if isinstance(action, (RerunFailedChecks, EscalateCiPending)) else None


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
