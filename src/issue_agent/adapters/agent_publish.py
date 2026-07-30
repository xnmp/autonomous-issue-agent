"""Durable publication broker invoked by paid agent children.

The child proposes a narrowly classified push or PR creation. The command is
committed before the subprocess starts, and an ambiguous prior attempt is
reconciled against the remote before any retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..domain.contracts import (
    CommandId,
    CommandKind,
    EffectReceipt,
    EffectStatus,
    LifecycleCommand,
)
from .control_store import PendingCommand, SQLiteControlStore
from .event_log import EventLog
from ..domain.events import Producer


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str = ""
    stderr: str = ""


RunCommand = Callable[[tuple[str, ...], str], CommandResult]


def _run_command(argv: tuple[str, ...], cwd: str) -> CommandResult:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(125, "", f"{type(exc).__name__}: {exc}")
    return CommandResult(
        completed.returncode, completed.stdout, completed.stderr
    )


class AgentPublicationBroker:
    """Execute and recover the two remote mutations required by fixer agents."""

    def __init__(
        self,
        store: SQLiteControlStore,
        *,
        repository: str,
        config_version: str = "",
        identity: str = "agent-publication-broker",
        owner: str = "",
        run: RunCommand = _run_command,
    ):
        if not repository or not owner:
            raise ValueError(
                "publication repository and executor owner are required"
            )
        self.store = store
        self.repository = repository
        self.config_version = config_version
        self.identity = identity
        self.owner = owner
        self.run = run

    def publish(self, argv: tuple[str, ...], cwd: str) -> CommandResult:
        command = self._command(argv, cwd)
        pending = self.store.enqueue_command(command)
        return self._run_pending(pending)

    def drain_pending_commands(self) -> int:
        completed = 0
        for pending in self.store.pending_commands(
            repository=self.repository,
            executor_identity=self.owner,
        ):
            if pending.command.kind not in {
                CommandKind.PUBLISH_BRANCH,
                CommandKind.OPEN_PULL_REQUEST,
            }:
                continue
            try:
                result = self._run_pending(pending)
            except Exception:
                continue
            if result.code == 0:
                completed += 1
        return completed

    def _run_pending(self, pending: PendingCommand) -> CommandResult:
        command = pending.command
        if pending.state == EffectStatus.SUCCEEDED.value:
            reconciled = self._reconcile(command)
            if reconciled is True:
                return CommandResult(0)
            return CommandResult(
                75,
                stderr="previous publication no longer matches the remote",
            )
        if pending.state == EffectStatus.PERMANENT_FAILURE.value:
            return CommandResult(1, stderr="publication permanently failed")
        if pending.state in {"executing", EffectStatus.UNKNOWN_OUTCOME.value}:
            reconciled = self._reconcile(command)
            if reconciled is True:
                self.store.resolve_unknown_effect(
                    self._receipt(command, EffectStatus.SUCCEEDED)
                )
                return CommandResult(0)
            if reconciled is None:
                return CommandResult(
                    75,
                    stderr="publication outcome remains unknown; refusing retry",
                )
            self.store.retry_reconciled_command(command.command_id)

        reconciled = self._reconcile(command)
        if reconciled is True:
            self.store.begin_command(command.command_id)
            self.store.record_effect(
                self._receipt(command, EffectStatus.SUCCEEDED)
            )
            return CommandResult(0)
        if reconciled is None:
            return CommandResult(
                75,
                stderr=(
                    "remote state diverged from the captured expected revision; "
                    "refusing publication"
                ),
            )
        self.store.begin_command(command.command_id)
        started = datetime.now(timezone.utc)
        payload = command.payload
        argv = tuple(str(item) for item in payload["argv"])
        cwd = str(payload["cwd"])
        result = self.run(argv, cwd)
        reconciled = self._reconcile(command)
        if result.code == 0 or reconciled is True:
            status = EffectStatus.SUCCEEDED
            error = None
        elif reconciled is False:
            status = EffectStatus.RETRYABLE_FAILURE
            error = (
                result.stderr.strip()[:1000]
                or f"publication command exited {result.code}"
            )
        else:
            status = EffectStatus.UNKNOWN_OUTCOME
            error = (
                result.stderr.strip()[:1000]
                or f"publication command exited {result.code}; readback unavailable"
            )
        self.store.record_effect(
            EffectReceipt(
                command.command_id,
                self.identity,
                started,
                datetime.now(timezone.utc),
                status,
                remote_object_id=self._remote_object(command),
                remote_revision=(
                    command.expected_revision
                    if status is EffectStatus.SUCCEEDED
                    else None
                ),
                error=error,
            )
        )
        return (
            result
            if status is not EffectStatus.SUCCEEDED
            else CommandResult(0, result.stdout, result.stderr)
        )

    def _command(self, argv: tuple[str, ...], cwd: str) -> LifecycleCommand:
        resolved_cwd = str(Path(cwd).resolve())
        head = self._required_output(
            ("git", "rev-parse", "HEAD"), resolved_cwd, "local HEAD"
        )
        branch = self._required_output(
            ("git", "branch", "--show-current"),
            resolved_cwd,
            "current branch",
        )
        if argv[:2] == ("git", "push"):
            remote, destination = _push_target(argv[2:], branch)
            prior = self._remote_branch(remote, destination, resolved_cwd)
            if prior is None:
                raise RuntimeError("cannot verify remote branch before push")
            kind = CommandKind.PUBLISH_BRANCH
            target = f"branch:{self.repository}:{destination}"
            payload: dict[str, object] = {
                "argv": list(argv),
                "cwd": resolved_cwd,
                "remote": remote,
                "branch": destination,
                "prior_remote_revision": prior,
                "owner": self.owner,
            }
        elif argv[:3] == ("gh", "pr", "create"):
            normalized = _snapshot_body_file(argv, resolved_cwd)
            prior = self._pull_request(branch, head, resolved_cwd)
            if prior is None:
                raise RuntimeError("cannot verify pull-request state before create")
            if prior:
                return self._command_for_existing_pr(
                    normalized, resolved_cwd, branch, head, prior
                )
            kind = CommandKind.OPEN_PULL_REQUEST
            target = f"pull-request:{self.repository}:{branch}"
            payload = {
                "argv": list(normalized),
                "cwd": resolved_cwd,
                "branch": branch,
                "prior_remote_object": "",
                "owner": self.owner,
            }
        else:
            raise ValueError(
                "durable publisher supports only `git push` and `gh pr create`"
            )
        return _lifecycle_command(
            kind,
            target,
            head,
            payload,
            self.config_version,
            self.repository,
            self.owner,
        )

    def _command_for_existing_pr(
        self,
        argv: tuple[str, ...],
        cwd: str,
        branch: str,
        head: str,
        remote_object: str,
    ) -> LifecycleCommand:
        payload: dict[str, object] = {
            "argv": list(argv),
            "cwd": cwd,
            "branch": branch,
            "prior_remote_object": remote_object,
            "owner": self.owner,
        }
        return _lifecycle_command(
            CommandKind.OPEN_PULL_REQUEST,
            f"pull-request:{self.repository}:{branch}",
            head,
            payload,
            self.config_version,
            self.repository,
            self.owner,
        )

    def _reconcile(self, command: LifecycleCommand) -> bool | None:
        payload = command.payload
        cwd = str(payload["cwd"])
        if command.kind is CommandKind.PUBLISH_BRANCH:
            current = self._remote_branch(
                str(payload["remote"]), str(payload["branch"]), cwd
            )
            if current is None:
                return None
            if current == command.expected_revision:
                return True
            if current == str(payload["prior_remote_revision"]):
                return False
            return None
        if command.kind is CommandKind.OPEN_PULL_REQUEST:
            current = self._pull_request(
                str(payload["branch"]),
                str(command.expected_revision),
                cwd,
            )
            if current is None:
                return None
            if current:
                return True
            if not str(payload.get("prior_remote_object") or ""):
                return False
            return None
        return None

    def _remote_branch(
        self, remote: str, branch: str, cwd: str
    ) -> str | None:
        result = self.run(
            ("git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"),
            cwd,
        )
        if result.code != 0:
            return None
        line = result.stdout.strip().splitlines()
        return line[0].split()[0] if line else ""

    def _pull_request(
        self, branch: str, head: str, cwd: str
    ) -> str | None:
        result = self.run(
            (
                "gh",
                "pr",
                "list",
                "--repo",
                self.repository,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,headRefOid,url",
            ),
            cwd,
        )
        if result.code != 0:
            return None
        try:
            rows = json.loads(result.stdout or "[]")
            if not isinstance(rows, list):
                return None
            matching = [
                row
                for row in rows
                if isinstance(row, dict) and row.get("headRefOid") == head
            ]
            if not matching:
                return ""
            row = matching[0]
            return str(row.get("url") or row.get("number") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _required_output(
        self, argv: tuple[str, ...], cwd: str, description: str
    ) -> str:
        result = self.run(argv, cwd)
        value = result.stdout.strip()
        if result.code != 0 or not value:
            raise RuntimeError(f"cannot resolve {description}")
        return value

    def _remote_object(self, command: LifecycleCommand) -> str | None:
        if command.kind is CommandKind.PUBLISH_BRANCH:
            return str(command.payload["branch"])
        return self._pull_request(
            str(command.payload["branch"]),
            str(command.expected_revision),
            str(command.payload["cwd"]),
        )

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
            remote_object_id=self._remote_object(command),
            remote_revision=command.expected_revision,
            error=None,
        )


def _lifecycle_command(
    kind: CommandKind,
    target: str,
    expected_revision: str,
    payload: dict[str, object],
    config_version: str,
    repository: str,
    executor_identity: str,
) -> LifecycleCommand:
    encoded = json.dumps(
        {
            "kind": kind.value,
            "target": target,
            "expected_revision": expected_revision,
            "payload": payload,
            "repository": repository,
            "executor_identity": executor_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return LifecycleCommand(
        CommandId(digest),
        f"agent-publication:{digest}",
        kind,
        target,
        expected_revision,
        payload,
        config_version,
        repository,
        executor_identity,
    )


def _push_target(
    args: tuple[str, ...], current_branch: str
) -> tuple[str, str]:
    unsupported = {
        "--all",
        "--branches",
        "--delete",
        "-d",
        "--mirror",
        "--tags",
        "--dry-run",
        "-n",
        "--receive-pack",
        "--exec",
        "-o",
        "--push-option",
    }
    if any(
        arg in unsupported
        or arg.startswith(
            ("--receive-pack=", "--exec=", "--push-option=")
        )
        for arg in args
    ):
        raise ValueError(
            "durable publisher accepts one concrete branch update only"
        )
    positional = tuple(arg for arg in args if not arg.startswith("-"))
    remote = positional[0] if positional else "origin"
    refspecs = positional[1:]
    if len(refspecs) > 1:
        raise ValueError("durable publisher accepts one push refspec")
    refspec = refspecs[0] if refspecs else current_branch
    destination = refspec.rsplit(":", 1)[-1]
    if destination in {"HEAD", ""}:
        destination = current_branch
    destination = destination.removeprefix("refs/heads/")
    if (
        not destination
        or destination.startswith("-")
        or destination in {"main", "master", "dev"}
    ):
        raise ValueError("push destination branch is unavailable")
    return remote, destination


def _snapshot_body_file(
    argv: tuple[str, ...], cwd: str
) -> tuple[str, ...]:
    normalized: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--body-file":
            if index + 1 >= len(argv) or argv[index + 1] == "-":
                raise ValueError("--body-file requires a stable file path")
            body = (Path(cwd) / argv[index + 1]).read_text(encoding="utf-8")
            normalized.extend(("--body", body))
            index += 2
            continue
        if arg.startswith("--body-file="):
            path = arg.split("=", 1)[1]
            if path == "-":
                raise ValueError("--body-file requires a stable file path")
            body = (Path(cwd) / path).read_text(encoding="utf-8")
            normalized.extend(("--body", body))
            index += 1
            continue
        normalized.append(arg)
        index += 1
    return tuple(normalized)


def main(argv: list[str] | None = None) -> None:
    args = tuple(argv if argv is not None else sys.argv[1:])
    path = os.environ.get("ISSUE_AGENT_CONTROL_DB", "")
    repository = os.environ.get("ISSUE_AGENT_REPOSITORY", "")
    if not path or not repository:
        raise SystemExit(
            "issue-agent-publish is available only inside a supervised agent run"
        )
    store = SQLiteControlStore(path)
    store.initialize()
    event_path = os.environ.get("ISSUE_AGENT_EVENT_LOG", "")
    if event_path:
        producer = Producer(
            os.environ["ISSUE_AGENT_EVENT_DEPLOYMENT"],
            repository,
            os.environ["ISSUE_AGENT_EVENT_ROLE"],
            os.getpid(),
            f"{os.environ.get('ISSUE_AGENT_RUN_ID', 'unscoped')}:{uuid.uuid4().hex}",
        )
        events = EventLog(
            event_path,
            producer,
            line_limit=int(
                os.environ.get("ISSUE_AGENT_EVENT_LINE_LIMIT", "65536")
            ),
        )
        store.bind_event_outbox(
            events.prepare,
            events.append_committed,
            line_limit=events.line_limit,
            destination=producer.feed_identity,
        )
    broker = AgentPublicationBroker(
        store,
        repository=repository,
        config_version=os.environ.get("ISSUE_AGENT_CONFIG_VERSION", ""),
        identity=f"agent-publication:{os.getpid()}",
        owner=os.environ.get("ISSUE_AGENT_PUBLICATION_OWNER", ""),
    )
    try:
        result = broker.publish(args, os.getcwd())
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"issue-agent-publish: {exc}\n")
        raise SystemExit(1) from exc
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.code)
