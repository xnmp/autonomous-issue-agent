"""Lifecycle commands <-> Actions for the resolve workflow store (ADR 0001).

Refactor slice 5c deleted the durable command executor: neither role executes
queued commands any more (both act through `adapters.act.GitHubAct`). What is
left mints and reads back the commands the residual resolve workflow store
still records; slice 5d deletes it with that store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Mapping

from ..domain.actions import (
    Action,
    CancelWorkflowRuns,
    CloseIssue,
    EnableAutoMerge,
    EscalateCiPending,
    EscalatePRRecovery,
    PostComment,
    Relabel,
    RerunFailedChecks,
    Target,
    UpdateBranch,
)
from ..domain.contracts import CommandId, CommandKind, LifecycleCommand
from ..domain.eligibility import IssueCompletionGuard, IssueHoldGuard, LifecycleOperation
from ..domain.labels import Label
from ..domain.repository import canonical_repository
from ..domain.workflow_identity import WorkflowBinding
from .workflow_codec import decode_workflow, encode_workflow

_TARGET = {Target.ISSUE: "issue", Target.PR: "pr"}


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
    if isinstance(action, EscalatePRRecovery):
        action_payload.pop("capture", None)
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
    """Reconstruct the closed Action vocabulary from a recorded command."""

    payload = command.payload
    if command.kind is CommandKind.RELABEL:
        if {"pr", "head", "body"} <= payload.keys():
            if "cause" in payload:
                return EscalatePRRecovery(int(payload["pr"]), str(payload["head"]), str(payload["body"]), str(payload["cause"]), decode_workflow(payload["pr_recovery"], object))
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
            str(payload.get("base", "")),
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
