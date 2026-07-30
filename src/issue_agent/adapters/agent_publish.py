"""Durable publication broker invoked by paid agent children.

The child proposes a narrowly classified push or PR creation. The command is
committed before the subprocess starts, and an ambiguous prior attempt is
reconciled against the remote before any retry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from ..domain.contracts import (
    CommandId,
    CommandKind,
    EffectReceipt,
    EffectStatus,
    LifecycleCommand,
    RunId,
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

_AUTHORITY_ENV_KEYS = (
    "ISSUE_AGENT_CONTROL_DB",
    "ISSUE_AGENT_REPOSITORY",
    "ISSUE_AGENT_PUBLICATION_OWNER",
    "ISSUE_AGENT_RUN_ID",
    "ISSUE_AGENT_CONFIG_VERSION",
    "ISSUE_AGENT_GITHUB_HOST",
)
_AUTHORITY_BUNDLE = "ISSUE_AGENT_AUTHORITY_BUNDLE"


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
        host: str = "github.com",
        config_version: str = "",
        identity: str = "agent-publication-broker",
        owner: str = "",
        run: RunCommand = _run_command,
    ):
        if not repository or not owner or not host or any(char.isspace() for char in host):
            raise ValueError(
                "publication repository, host, and executor owner are required"
            )
        self.store = store
        self.repository = repository
        self.host = host.lower()
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
        if reconciled is True:
            status = EffectStatus.SUCCEEDED
            error = None
        elif reconciled is False:
            status = EffectStatus.RETRYABLE_FAILURE
            error = (
                result.stderr.strip()[:1000]
                or (
                    f"publication command exited {result.code}, but its "
                    "postcondition did not match"
                )
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
        if status is EffectStatus.SUCCEEDED:
            return CommandResult(0, result.stdout, result.stderr)
        return CommandResult(
            result.code if result.code != 0 else 75,
            result.stdout,
            error or result.stderr,
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
            flags, destination = _push_spec(argv[2:], branch)
            remote = "origin"
            remote_url = self._required_output(
                ("git", "remote", "get-url", "--push", remote),
                resolved_cwd,
                "origin push URL",
            )
            _require_repository_remote(
                remote_url,
                self.repository,
                self.host,
            )
            prior = self._remote_branch(remote, destination, resolved_cwd)
            if prior is None:
                raise RuntimeError("cannot verify remote branch before push")
            normalized = _captured_push(
                flags,
                destination,
                captured_head=head,
                prior_remote_revision=prior,
            )
            kind = CommandKind.PUBLISH_BRANCH
            target = f"branch:{self.repository}:{destination}"
            payload: dict[str, object] = {
                "argv": list(normalized),
                "cwd": resolved_cwd,
                "remote": remote,
                "remote_url": remote_url,
                "branch": destination,
                "prior_remote_revision": prior,
                "owner": self.owner,
            }
        elif argv[:3] == ("gh", "pr", "create"):
            normalized = _normalized_pr_create(
                argv,
                resolved_cwd,
                repository=self.repository,
                host=self.host,
                branch=branch,
            )
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
                f"{self.host}/{self.repository}",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number,headRefOid,state,url",
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
                if (
                    isinstance(row, dict)
                    and row.get("headRefOid") == head
                    and str(row.get("state") or "").upper() == "OPEN"
                )
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


def _push_spec(
    args: tuple[str, ...], current_branch: str
) -> tuple[tuple[str, ...], str]:
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
        "--repo",
        "--no-verify",
        "--force",
        "-f",
        "--force-with-lease",
    }
    if any(
        arg in unsupported
        or arg.startswith(
            (
                "--receive-pack=",
                "--exec=",
                "--push-option=",
                "--force-with-lease=",
            )
        )
        for arg in args
    ):
        raise ValueError(
            "durable publisher accepts one concrete branch update only"
        )
    supported_flags = {
        "--set-upstream",
        "-u",
        "--porcelain",
        "--quiet",
        "-q",
        "--verbose",
        "-v",
    }
    flags = tuple(arg for arg in args if arg.startswith("-"))
    if any(
        flag not in supported_flags
        for flag in flags
    ):
        raise ValueError("durable publisher received an unsupported push option")
    positional = tuple(arg for arg in args if not arg.startswith("-"))
    if positional and positional[0] != "origin":
        raise ValueError("durable publisher accepts only the configured origin")
    refspecs = positional[1:] if positional else ()
    if len(refspecs) > 1:
        raise ValueError("durable publisher accepts one push refspec")
    refspec = refspecs[0] if refspecs else current_branch
    if ":" in refspec:
        source, destination = refspec.rsplit(":", 1)
        if source != "HEAD":
            raise ValueError("push source must be the captured HEAD")
    else:
        if refspec not in {"HEAD", current_branch}:
            raise ValueError("push source must be the captured HEAD")
        destination = current_branch
    if destination in {"HEAD", ""}:
        destination = current_branch
    destination = destination.removeprefix("refs/heads/")
    if (
        not destination
        or destination.startswith("-")
        or destination in {"main", "master", "dev"}
    ):
        raise ValueError("push destination branch is unavailable")
    return flags, destination


def _captured_push(
    flags: tuple[str, ...],
    destination: str,
    *,
    captured_head: str,
    prior_remote_revision: str,
) -> tuple[str, ...]:
    remote_ref = f"refs/heads/{destination}"
    return (
        "git",
        "push",
        *flags,
        f"--force-with-lease={remote_ref}:{prior_remote_revision}",
        "origin",
        f"{captured_head}:{remote_ref}",
    )


def _require_repository_remote(
    remote_url: str,
    repository: str,
    approved_host: str,
) -> None:
    """Reject a same-command push whose remote is outside configured authority."""

    host, resolved_repository = _remote_identity(remote_url)
    if (
        host.lower() != approved_host.lower()
        or resolved_repository.lower() != repository.lower()
    ):
        raise ValueError(
            "origin push URL does not match the configured repository"
        )


def _remote_identity(remote_url: str) -> tuple[str, str]:
    raw = remote_url.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        path = parsed.path
    elif "@" in raw and ":" in raw:
        authority, path = raw.split(":", 1)
        host = authority.rsplit("@", 1)[-1]
    else:
        raise ValueError("origin push URL is not a remote repository URL")
    parts = tuple(part for part in path.strip("/").split("/") if part)
    if len(parts) != 2 or not host:
        raise ValueError("origin push URL has no owner/repository identity")
    owner, name = parts
    return host, f"{owner}/{name.removesuffix('.git')}"


def _normalized_pr_create(
    argv: tuple[str, ...],
    cwd: str,
    *,
    repository: str,
    host: str,
    branch: str,
) -> tuple[str, ...]:
    snapshotted = _snapshot_body_file(argv, cwd)
    forbidden = {"--repo", "-R", "--head", "-H"}
    for index, arg in enumerate(snapshotted[3:], start=3):
        if (
            arg in forbidden
            or arg.startswith("--repo=")
            or arg.startswith("--head=")
            or (arg.startswith("-R") and arg != "-R")
            or (arg.startswith("-H") and arg != "-H")
        ):
            raise ValueError(
                "pull-request repository and head are fixed by the durable command"
            )
        if index > 3 and snapshotted[index - 1] in forbidden:
            raise ValueError(
                "pull-request repository and head are fixed by the durable command"
            )
    return (
        *snapshotted,
        "--repo",
        f"{host}/{repository}",
        "--head",
        branch,
    )


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


def _process_environment(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except FileNotFoundError:
        if sys.platform != "darwin":
            raise
        completed = subprocess.run(
            ("ps", "eww", "-p", str(pid), "-o", "command="),
            text=True,
            capture_output=True,
            timeout=5,
        )
        if completed.returncode != 0:
            raise ProcessLookupError(pid)
        match = re.search(
            rf"(?:^|\s){_AUTHORITY_BUNDLE}=([A-Za-z0-9_=-]+)(?:\s|$)",
            completed.stdout,
        )
        if match is None:
            return {}
        return _decode_authority_bundle(match.group(1))
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        name, value = item.split(b"=", 1)
        result[name.decode("utf-8", "replace")] = value.decode(
            "utf-8", "replace"
        )
    bundle = result.get(_AUTHORITY_BUNDLE)
    if bundle:
        return _decode_authority_bundle(bundle)
    return result


def _decode_authority_bundle(encoded: str) -> dict[str, str]:
    try:
        decoded = json.loads(
            base64.urlsafe_b64decode(encoded.encode()).decode()
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("publication authority bundle is malformed") from exc
    if not isinstance(decoded, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in decoded.items()
    ):
        raise ValueError("publication authority bundle has invalid values")
    return decoded


def _process_parent(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        if sys.platform != "darwin":
            raise
        completed = subprocess.run(
            ("ps", "-p", str(pid), "-o", "ppid="),
            text=True,
            capture_output=True,
            timeout=5,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise ProcessLookupError(pid)
        return int(completed.stdout.strip())
    close = raw.rfind(")")
    fields = raw[close + 1 :].split()
    if close < 0 or len(fields) < 3:
        raise RuntimeError(f"cannot parse process identity for pid {pid}")
    return int(fields[1])


def _ancestor_pids(pid: int) -> tuple[int, ...]:
    ancestors: list[int] = []
    seen: set[int] = set()
    current = _process_parent(pid)
    while current > 1 and current not in seen and len(ancestors) < 64:
        seen.add(current)
        ancestors.append(current)
        current = _process_parent(current)
    return tuple(reversed(ancestors))


def _trusted_publication_environment() -> dict[str, str]:
    """Recover immutable run authority from the supervised ancestor process."""

    from .process_supervisor import ProcessSupervisor

    for pid in _ancestor_pids(os.getpid()):
        try:
            candidate = _process_environment(pid)
        except OSError:
            continue
        if any(not candidate.get(key) for key in _AUTHORITY_ENV_KEYS):
            continue
        try:
            store = SQLiteControlStore(candidate["ISSUE_AGENT_CONTROL_DB"])
            store.integrity_check()
            authority = store.publication_authority(
                RunId(candidate["ISSUE_AGENT_RUN_ID"])
            )
            candidate_pgid = os.getpgid(pid)
        except (OSError, RuntimeError, ValueError):
            continue
        if (
            authority is None
            or authority.repository
            != candidate["ISSUE_AGENT_REPOSITORY"]
            or authority.owner_id
            != candidate["ISSUE_AGENT_PUBLICATION_OWNER"]
            or candidate_pgid != authority.pgid
            or not ProcessSupervisor.matches_identity(
                authority.pgid, authority.process_start_ticks
            )
        ):
            continue
        return candidate
    raise RuntimeError(
        "publication authority is unavailable from a live supervised run"
    )


def main(argv: list[str] | None = None) -> None:
    args = tuple(argv if argv is not None else sys.argv[1:])
    try:
        authority_env = _trusted_publication_environment()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(
            "issue-agent-publish is available only inside a supervised "
            f"agent run: {exc}"
        ) from exc
    path = authority_env["ISSUE_AGENT_CONTROL_DB"]
    repository = authority_env["ISSUE_AGENT_REPOSITORY"]
    store = SQLiteControlStore(path)
    store.integrity_check()
    event_path = authority_env.get("ISSUE_AGENT_EVENT_LOG", "")
    if event_path:
        producer = Producer(
            authority_env["ISSUE_AGENT_EVENT_DEPLOYMENT"],
            repository,
            authority_env["ISSUE_AGENT_EVENT_ROLE"],
            os.getpid(),
            f"{authority_env['ISSUE_AGENT_RUN_ID']}:{uuid.uuid4().hex}",
        )
        events = EventLog(
            event_path,
            producer,
            line_limit=int(
                authority_env.get(
                    "ISSUE_AGENT_EVENT_LINE_LIMIT", "65536"
                )
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
        host=authority_env["ISSUE_AGENT_GITHUB_HOST"],
        config_version=authority_env["ISSUE_AGENT_CONFIG_VERSION"],
        identity=f"agent-publication:{os.getpid()}",
        owner=authority_env["ISSUE_AGENT_PUBLICATION_OWNER"],
    )
    try:
        result = broker.publish(args, os.getcwd())
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"issue-agent-publish: {exc}\n")
        raise SystemExit(1) from exc
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.code)
