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
        if pending.state == "pending" and not self._revision_matches(command):
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
        else:
            reconciled = self._reconcile(action, command)
            if reconciled is True:
                # A deployment cutover or target revision change can produce a
                # new command identity for an effect that already landed.
                self.store.begin_command(command.command_id)
                self.store.record_effect(
                    self._receipt(command, EffectStatus.SUCCEEDED)
                )
                return True
        if pending.state != "pending" and not self._revision_matches(command):
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
            applied = self._apply(action, command)
            if not applied:
                outcome = False
            else:
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
            if not isinstance(self.gateway, ReviewVerifier):
                raise UnknownEffectOutcome(
                    f"{command.command_id}: succeeded review cannot be verified"
                )
            if self.gateway.has_review(pr, event, body, head):
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
        elif isinstance(self.gateway, ReviewVerifier) and self.gateway.has_review(
            pr, event, body, head
        ):
            # Deployment-scoped identities change the command digest. Reconcile
            # a matching pre-cutover review before the new identity can submit
            # the same GitHub review again.
            self.store.begin_command(command.command_id)
            self.store.record_effect(
                self._receipt(command, EffectStatus.SUCCEEDED)
            )
            return True
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
            self.gateway.close_issue(action.number, action.reason, action.comment)
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
        _kind, target = _command_identity(action)
        if expected is None and target.startswith("pr:"):
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
            (
                (
                    str(payload["expected_revision"])
                    if payload["expected_revision"] is not None
                    else None
                )
                if "expected_revision" in payload
                else command.expected_revision
            ),
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
