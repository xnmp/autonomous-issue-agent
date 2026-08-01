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
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from ..domain.contracts import (
    CommandId,
    CommandKind,
    EffectStatus,
    LifecycleCommand,
    RunId,
)
from .control_store import PendingCommand, SQLiteControlStore
from .deployment_authority import DeploymentAuthority, deployment_role
from .durable_effects import DurableEffectRunner, EffectOutcome
from .event_log import EventLog
from ..domain.events import Producer
from ..domain.refs import is_governed_branch, issue_from_branch
from ..domain.repository import canonical_repository


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
    "ISSUE_AGENT_BASE_BRANCH",
    "ISSUE_AGENT_PUBLICATION_CWD",
    "ISSUE_AGENT_PUBLICATION_BRANCH",
    "ISSUE_AGENT_PUBLICATION_ISSUE",
    "ISSUE_AGENT_PUBLICATION_KIND",
    "ISSUE_AGENT_GIT_BIN",
    "ISSUE_AGENT_GH_BIN",
    "ISSUE_AGENT_EXEC_PATH",
)
_AUTHORITY_BUNDLE = "ISSUE_AGENT_AUTHORITY_BUNDLE"


def _run_command(
    argv: tuple[str, ...],
    cwd: str,
    *,
    environment: dict[str, str] | None = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=300,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(125, "", f"{type(exc).__name__}: {exc}")
    return CommandResult(
        completed.returncode, completed.stdout, completed.stderr
    )


def _publication_environment(
    trusted_path: str, *, host: str, gh_binary: str = ""
) -> dict[str, str]:
    environment = dict(os.environ)
    dangerous = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GH_CONFIG_DIR",
    }
    for key in tuple(environment):
        if (
            key in dangerous
            or key.startswith("GIT_CONFIG_KEY_")
            or key.startswith("GIT_CONFIG_VALUE_")
        ):
            environment.pop(key, None)
    environment["PATH"] = trusted_path
    environment["GH_HOST"] = host
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    # Nulling the global config above also removed the operator's
    # credential.helper, so an https push prompted for a username and died
    # headless (`could not read Username`, keifu #39, 2026-08-01). Restore
    # auth through the ONE path this broker already trusts: the absolute gh
    # binary recovered from the supervised ancestor, reading its ambient
    # login (GH_CONFIG_DIR was scrubbed, so gh uses the operator's real
    # config, not an agent-supplied redirect). The empty first helper entry
    # clears any inherited helper list; prompting stays forbidden.
    if gh_binary:
        environment["GIT_CONFIG_COUNT"] = "2"
        environment["GIT_CONFIG_KEY_0"] = f"credential.https://{host}.helper"
        environment["GIT_CONFIG_VALUE_0"] = ""
        environment["GIT_CONFIG_KEY_1"] = f"credential.https://{host}.helper"
        environment["GIT_CONFIG_VALUE_1"] = f"!{gh_binary} auth git-credential"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


class AgentPublicationBroker:
    """Execute and recover the two remote mutations required by fixer agents."""

    def __init__(
        self,
        store: SQLiteControlStore,
        *,
        repository: str,
        base_branch: str,
        host: str = "github.com",
        config_version: str = "",
        identity: str = "agent-publication-broker",
        owner: str = "",
        authorized_cwd: str = "",
        authorized_branch: str = "",
        expected_issue: int | None = None,
        git_binary: str = "git",
        gh_binary: str = "gh",
        trusted_path: str = "",
        run: RunCommand = _run_command,
    ):
        if (
            not repository
            or not owner
            or not host
            or not base_branch
            or base_branch.startswith("-")
            or any(char.isspace() for char in host + base_branch)
        ):
            raise ValueError(
                "publication repository, base branch, host, and executor "
                "owner are required"
            )
        self.store = store
        self.repository = canonical_repository(repository)
        self.host = host.lower()
        self.base_branch = base_branch
        self.config_version = config_version
        self.identity = identity
        self.owner = owner
        self.authorized_cwd = (
            str(Path(authorized_cwd).resolve()) if authorized_cwd else ""
        )
        self.expected_issue = expected_issue
        self.authorized_branch = authorized_branch
        self.git_binary = git_binary
        self.gh_binary = gh_binary
        self.trusted_path = trusted_path
        if (
            authorized_cwd
            and (
                (expected_issue is not None and expected_issue < 0)
                or not authorized_branch
                or not Path(git_binary).is_absolute()
                or not Path(gh_binary).is_absolute()
                or not trusted_path
            )
        ):
            raise ValueError("publication execution authority is incomplete")
        self.store.bind_active_owner(owner)
        self.effects = DurableEffectRunner(
            store,
            DeploymentAuthority(
                store,
                self.repository,
                deployment_role(self.repository, owner),
                owner,
            ),
            executor_identity=identity,
        )
        self.run = (
            (
                lambda argv, cwd: _run_command(
                    argv,
                    cwd,
                    environment=_publication_environment(
                        trusted_path, host=self.host, gh_binary=gh_binary
                    ),
                )
            )
            if run is _run_command and trusted_path
            else run
        )

    def publish(self, argv: tuple[str, ...], cwd: str) -> CommandResult:
        command = self._command(argv, cwd)
        pending = self.effects.enqueue(command)
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

    def _execute(
        self, argv: tuple[str, ...], cwd: str
    ) -> CommandResult:
        if not argv:
            raise ValueError("publication command is empty")
        executable = {
            "git": self.git_binary,
            "gh": self.gh_binary,
        }.get(argv[0])
        if executable is None:
            raise ValueError("publication command executable is unsupported")
        return self.run((executable, *argv[1:]), cwd)

    def _execute_mutation(
        self, argv: tuple[str, ...], cwd: str
    ) -> CommandResult:
        return self.effects.authorized(lambda: self._execute(argv, cwd))

    def _run_pending(self, pending: PendingCommand) -> CommandResult:
        command = pending.command
        authority_error = self._origin_authority_error(command)
        if authority_error:
            return CommandResult(75, stderr=authority_error)
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
                return self.effects.resolve_unknown(
                    command,
                    self._successful_outcome(command),
                )
            if reconciled is None:
                return CommandResult(
                    75,
                    stderr="publication outcome remains unknown; refusing retry",
                )
            self.effects.retry_reconciled(command)

        reconciled = self._reconcile(command)
        if reconciled is True:
            return self.effects.execute(
                command,
                lambda: self._successful_outcome(command),
            )
        if reconciled is None:
            return CommandResult(
                75,
                stderr=(
                    "remote state diverged from the captured expected revision; "
                    "refusing publication"
                ),
            )
        return self.effects.execute(
            command,
            lambda: self._perform_publication(command),
        )

    def _perform_publication(
        self, command: LifecycleCommand
    ) -> EffectOutcome[CommandResult]:
        payload = command.payload
        argv = tuple(str(item) for item in payload["argv"])
        cwd = str(payload["cwd"])
        if command.kind is CommandKind.OPEN_PULL_REQUEST:
            try:
                current_head = self._required_output(
                    ("git", "rev-parse", "HEAD"), cwd, "local HEAD"
                )
                current_branch = self._required_output(
                    ("git", "branch", "--show-current"),
                    cwd,
                    "current branch",
                )
            except RuntimeError as exc:
                return EffectOutcome(
                    CommandResult(75, stderr=str(exc)),
                    EffectStatus.RETRYABLE_FAILURE,
                    error=str(exc),
                )
            if (
                current_head != command.expected_revision
                or current_branch != str(payload["branch"])
            ):
                error = (
                    "local branch revision changed before pull-request "
                    "creation"
                )
                return EffectOutcome(
                    CommandResult(
                        75,
                        stderr=(
                            "local branch revision changed; refusing stale "
                            "pull-request creation"
                        ),
                    ),
                    EffectStatus.PERMANENT_FAILURE,
                    error=error,
                )
            remote_head = self._remote_branch(
                str(payload["remote_url"]), str(payload["branch"]), cwd
            )
            if remote_head is None:
                error = (
                    "cannot verify remote branch revision before pull-request "
                    "creation"
                )
                return EffectOutcome(
                    CommandResult(75, stderr=error),
                    EffectStatus.RETRYABLE_FAILURE,
                    error=error,
                )
            if remote_head != command.expected_revision:
                error = (
                    "remote branch revision changed before pull-request "
                    "creation"
                )
                return EffectOutcome(
                    CommandResult(
                        75,
                        stderr=(
                            "remote branch revision changed; refusing stale "
                            "pull-request creation"
                        ),
                    ),
                    EffectStatus.PERMANENT_FAILURE,
                    error=error,
                )
        authority_error = self._origin_authority_error(command)
        if authority_error:
            return EffectOutcome(
                CommandResult(75, stderr=authority_error),
                EffectStatus.RETRYABLE_FAILURE,
                error=authority_error,
            )
        if (
            command.kind is CommandKind.PUBLISH_BRANCH
            and not self._disable_auto_merge_before_push(
                str(payload["branch"]), cwd, authority_held=True
            )
        ):
            error = (
                "could not verify auto-merge was disabled before updating "
                "the pull-request branch"
            )
            return EffectOutcome(
                CommandResult(
                    75,
                    stderr=(
                    "could not verify auto-merge was disabled; refusing "
                    "branch publication"
                    ),
                ),
                EffectStatus.RETRYABLE_FAILURE,
                error=error,
            )
        result = self._execute(argv, cwd)
        reconciled = self._reconcile(command, authority_held=True)
        compensated = False
        if (
            command.kind is CommandKind.OPEN_PULL_REQUEST
            and reconciled is False
            and result.code == 0
        ):
            compensated = self._close_mismatched_pull_request(
                command, result, authority_held=True
            )
            reconciled = self._reconcile(command, authority_held=True)
        if reconciled is True:
            status = EffectStatus.SUCCEEDED
            error = None
        elif compensated and reconciled is False:
            status = EffectStatus.PERMANENT_FAILURE
            error = (
                "pull request was created from a different head and was "
                "closed; refusing stale publication"
            )
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
        if status is EffectStatus.SUCCEEDED:
            value = CommandResult(0, result.stdout, result.stderr)
        else:
            value = CommandResult(
                result.code if result.code != 0 else 75,
                result.stdout,
                error or result.stderr,
            )
        return EffectOutcome(
            value,
            status,
            remote_object_id=self._remote_object(command),
            remote_revision=(
                command.expected_revision
                if status is EffectStatus.SUCCEEDED
                else None
            ),
            error=error,
        )

    def _successful_outcome(
        self, command: LifecycleCommand
    ) -> EffectOutcome[CommandResult]:
        return EffectOutcome(
            CommandResult(0),
            EffectStatus.SUCCEEDED,
            remote_object_id=self._remote_object(command),
            remote_revision=command.expected_revision,
        )

    def _command(self, argv: tuple[str, ...], cwd: str) -> LifecycleCommand:
        resolved_cwd = str(Path(cwd).resolve())
        if (
            self.authorized_cwd
            and resolved_cwd != self.authorized_cwd
        ):
            raise ValueError(
                "publication cwd does not match the supervised paid run"
            )
        head = self._required_output(
            ("git", "rev-parse", "HEAD"), resolved_cwd, "local HEAD"
        )
        branch = self._required_output(
            ("git", "branch", "--show-current"),
            resolved_cwd,
            "current branch",
        )
        if not is_governed_branch(branch):
            raise ValueError(
                "durable publisher requires a governed current branch"
            )
        if self.authorized_branch and branch != self.authorized_branch:
            raise ValueError(
                "publication branch does not match the supervised paid run"
            )
        if (
            self.expected_issue is not None
            and self.expected_issue > 0
            and issue_from_branch(branch) != self.expected_issue
        ):
            raise ValueError(
                "publication branch does not match the supervised issue"
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
            prior = self._remote_branch(
                remote_url, destination, resolved_cwd
            )
            if prior is None:
                raise RuntimeError("cannot verify remote branch before push")
            self._require_no_foreign_discard(
                head, prior, destination, resolved_cwd
            )
            normalized = _captured_push(
                flags,
                destination,
                captured_head=head,
                prior_remote_revision=prior,
                remote=remote_url,
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
                base_branch=self.base_branch,
            )
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
            remote_head = self._remote_branch(
                remote_url, branch, resolved_cwd
            )
            if remote_head is None:
                raise RuntimeError(
                    "cannot verify remote branch before pull-request creation"
                )
            if remote_head != head:
                raise ValueError(
                    "remote branch does not match the captured local HEAD"
                )
            rows = self._open_pull_requests(branch, resolved_cwd)
            if rows is None:
                raise RuntimeError("cannot verify pull-request state before create")
            if any(
                str(row.get("headRefOid") or "") != head
                or str(row.get("baseRefName") or "") != self.base_branch
                for row in rows
            ):
                raise ValueError(
                    "an open pull request already exists for this branch with "
                    "a different revision or base"
                )
            prior = self._matching_pull_request(
                rows, head, self.base_branch
            )
            if prior:
                return self._command_for_existing_pr(
                    normalized,
                    resolved_cwd,
                    branch,
                    head,
                    prior,
                    remote_url,
                )
            kind = CommandKind.OPEN_PULL_REQUEST
            target = f"pull-request:{self.repository}:{branch}"
            payload = {
                "argv": list(normalized),
                "cwd": resolved_cwd,
                "branch": branch,
                "base_branch": self.base_branch,
                "prior_remote_object": "",
                "owner": self.owner,
                "remote": remote,
                "remote_url": remote_url,
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
        remote_url: str,
    ) -> LifecycleCommand:
        payload: dict[str, object] = {
            "argv": list(argv),
            "cwd": cwd,
            "branch": branch,
            "base_branch": self.base_branch,
            "prior_remote_object": remote_object,
            "owner": self.owner,
            "remote": "origin",
            "remote_url": remote_url,
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

    def _reconcile(
        self,
        command: LifecycleCommand,
        *,
        authority_held: bool = False,
    ) -> bool | None:
        if self._origin_authority_error(command):
            return None
        payload = command.payload
        cwd = str(payload["cwd"])
        if command.kind is CommandKind.PUBLISH_BRANCH:
            current = self._remote_branch(
                str(payload["remote_url"]), str(payload["branch"]), cwd
            )
            if current is None:
                return None
            if current == command.expected_revision:
                return self._disable_auto_merge_before_push(
                    str(payload["branch"]),
                    cwd,
                    authority_held=authority_held,
                )
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

    def _origin_authority_error(self, command: LifecycleCommand) -> str:
        if command.kind not in {
            CommandKind.PUBLISH_BRANCH,
            CommandKind.OPEN_PULL_REQUEST,
        }:
            return "publication command kind is outside broker authority"
        payload = command.payload
        cwd = str(payload.get("cwd") or "")
        captured = str(payload.get("remote_url") or "").strip()
        remote = str(payload.get("remote") or "")
        if not cwd or not captured or remote != "origin":
            return "publication command lacks captured origin authority"
        result = self._execute(
            ("git", "remote", "get-url", "--push", remote), cwd
        )
        if result.code != 0 or not result.stdout.strip():
            return "origin push URL is unavailable; refusing publication"
        current = result.stdout.strip()
        try:
            _require_repository_remote(current, self.repository, self.host)
            _require_repository_remote(captured, self.repository, self.host)
        except ValueError:
            return (
                "origin push URL does not match the configured repository; "
                "refusing publication"
            )
        if current != captured:
            return (
                "origin push URL changed after command capture; refusing "
                "publication"
            )
        return ""

    def _require_no_foreign_discard(
        self, head: str, prior: str, destination: str, cwd: str
    ) -> None:
        """Refuse a non-fast-forward publish over a remote tip this deployment
        did not put there (ADR 0012 §2; xnmp/autonomous-issue-agent#154).

        The lease (`--force-with-lease=<ref>:<prior>`) makes the push
        race-safe, not authorship-safe: observed live on keifu #169
        (2026-08-01), an agent's rebase publish legally force-replaced the
        remote tip and erased an operator's fix, which the reviewer then
        re-demanded. "Foreign" is decided by the broker's OWN receipts, not a
        patch heuristic: a remote tip equal to the revision we last
        successfully published is the agent's own history, freely rewritable
        (the ordinary conflicting-rebase flow, whose resolved commits change
        patch-ids and would false-refuse under `git cherry`-style
        equivalence). Any other non-fast-forward tip was written by someone
        else — or predates this deployment's receipts — and must be MERGED,
        not replaced. A fast-forward always passes (nothing is discarded)."""
        if not prior:
            return  # branch does not exist remotely yet — nothing to discard
        if self._execute(
            ("git", "cat-file", "-e", f"{prior}^{{commit}}"), cwd
        ).code != 0:
            fetched = self._execute(
                ("git", "fetch", "origin", f"refs/heads/{destination}"), cwd
            )
            if (
                fetched.code != 0
                or self._execute(
                    ("git", "cat-file", "-e", f"{prior}^{{commit}}"), cwd
                ).code
                != 0
            ):
                raise ValueError(
                    f"cannot verify the commits on origin/{destination} that "
                    "this push would replace (fetch failed) — run "
                    f"`git fetch origin {destination}` and retry"
                )
        if self._execute(
            ("git", "merge-base", "--is-ancestor", prior, head), cwd
        ).code == 0:
            return  # fast-forward — discards nothing
        published = self.store.last_published_revision(
            f"branch:{self.repository}:{destination}"
        )
        if published == prior:
            return  # rewriting only history this harness itself published
        provenance = (
            "it was not published by this harness (control-store provenance)"
            if published
            else "the remote history predates this deployment's publish "
            "receipts, so its provenance cannot be established"
        )
        raise ValueError(
            f"push refused: origin/{destination} is at {prior[:10]}, which is "
            f"not what this harness last published — {provenance}. Merge "
            f"origin/{destination} into your branch (never rebase over "
            "commits you have not incorporated) and push again — a "
            "fast-forward is always accepted"
        )

    def _remote_branch(
        self, remote: str, branch: str, cwd: str
    ) -> str | None:
        result = self._execute(
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
        rows = self._open_pull_requests(branch, cwd)
        if rows is None:
            return None
        return self._matching_pull_request(rows, head, self.base_branch)

    def _open_pull_requests(
        self, branch: str, cwd: str
    ) -> list[dict[str, object]] | None:
        result = self._execute(
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
                "number,headRefOid,baseRefName,state,url,autoMergeRequest",
            ),
            cwd,
        )
        if result.code != 0:
            return None
        try:
            rows = json.loads(result.stdout or "[]")
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) for row in rows
            ):
                return None
            return [
                row
                for row in rows
                if str(row.get("state") or "").upper() == "OPEN"
            ]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _matching_pull_request(
        rows: list[dict[str, object]], head: str, base: str
    ) -> str:
        matching = [
            row
            for row in rows
            if str(row.get("headRefOid") or "") == head
            and str(row.get("baseRefName") or "") == base
        ]
        if not matching:
            return ""
        row = matching[0]
        return str(row.get("url") or row.get("number") or "")

    def _disable_auto_merge_before_push(
        self,
        branch: str,
        cwd: str,
        *,
        authority_held: bool = False,
    ) -> bool:
        """A write-capable push must not inherit a merge armed for an old SHA."""

        rows = self._open_pull_requests(branch, cwd)
        if rows is None:
            return False
        armed = [
            row
            for row in rows
            if row.get("autoMergeRequest") is not None
        ]
        for row in armed:
            number = row.get("number")
            if not isinstance(number, int) or number <= 0:
                return False
            execute = (
                self._execute
                if authority_held
                else self._execute_mutation
            )
            disabled = execute(
                (
                    "gh",
                    "pr",
                    "merge",
                    str(number),
                    "--repo",
                    f"{self.host}/{self.repository}",
                    "--disable-auto",
                ),
                cwd,
            )
            if disabled.code != 0:
                return False
        verified = self._open_pull_requests(branch, cwd)
        if verified is None:
            return False
        return not any(
            row.get("autoMergeRequest") is not None
            for row in verified
        )

    def _close_mismatched_pull_request(
        self,
        command: LifecycleCommand,
        result: CommandResult,
        *,
        authority_held: bool = False,
    ) -> bool:
        branch = str(command.payload["branch"])
        cwd = str(command.payload["cwd"])
        rows = self._open_pull_requests(branch, cwd)
        if rows is None:
            return False
        match = re.search(r"(?:/|^)([1-9][0-9]*)/?$", result.stdout.strip())
        if match is None:
            return False
        created_number = int(match.group(1))
        mismatched = [
            row
            for row in rows
            if row.get("number") == created_number
            and str(row.get("headRefOid") or "")
            != command.expected_revision
            and str(row.get("baseRefName") or "")
            == str(command.payload["base_branch"])
        ]
        if len(mismatched) != 1:
            return False
        execute = (
            self._execute
            if authority_held
            else self._execute_mutation
        )
        close = execute(
            (
                "gh",
                "pr",
                "close",
                str(created_number),
                "--repo",
                f"{self.host}/{self.repository}",
                "--comment",
                (
                    "Closed automatically: the branch head changed during "
                    "durable pull-request creation."
                ),
            ),
            cwd,
        )
        if close.code != 0:
            return False
        remaining = self._open_pull_requests(branch, cwd)
        return remaining is not None and not any(
            row.get("number") == created_number
            for row in remaining
        )

    def _required_output(
        self, argv: tuple[str, ...], cwd: str, description: str
    ) -> str:
        result = self._execute(argv, cwd)
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

def _lifecycle_command(
    kind: CommandKind,
    target: str,
    expected_revision: str,
    payload: dict[str, object],
    config_version: str,
    repository: str,
    executor_identity: str,
) -> LifecycleCommand:
    repository = canonical_repository(repository)
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
    if not is_governed_branch(current_branch):
        raise ValueError("push source branch is not governed")
    if destination != current_branch:
        raise ValueError(
            "push destination must be the captured current branch"
        )
    return flags, destination


def _captured_push(
    flags: tuple[str, ...],
    destination: str,
    *,
    captured_head: str,
    prior_remote_revision: str,
    remote: str,
) -> tuple[str, ...]:
    remote_ref = f"refs/heads/{destination}"
    return (
        "git",
        "push",
        *flags,
        f"--force-with-lease={remote_ref}:{prior_remote_revision}",
        remote,
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
    base_branch: str,
) -> tuple[str, ...]:
    del cwd
    snapshotted = _validated_pr_arguments(argv)
    forbidden = {"--repo", "-R", "--head", "-H"}
    normalized: list[str] = list(snapshotted[:3])
    index = 3
    while index < len(snapshotted):
        arg = snapshotted[index]
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
        if arg in {"--base", "-B"}:
            if index + 1 >= len(snapshotted):
                raise ValueError("pull-request base option requires a value")
            index += 2
            continue
        if arg.startswith("--base=") or (
            arg.startswith("-B") and arg != "-B"
        ):
            index += 1
            continue
        if index > 3 and snapshotted[index - 1] in forbidden:
            raise ValueError(
                "pull-request repository and head are fixed by the durable command"
            )
        normalized.append(arg)
        index += 1
    return (
        *normalized,
        "--repo",
        f"{host}/{repository}",
        "--head",
        branch,
        "--base",
        base_branch,
    )


def _validated_pr_arguments(
    argv: tuple[str, ...],
) -> tuple[str, ...]:
    max_body_bytes = 60_000
    max_title_bytes = 512
    title_seen = False
    body_seen = False
    index = 3
    while index < len(argv):
        arg = argv[index]
        if arg in {
            "--body-file",
            "--template",
            "-T",
            "-F",
            "--editor",
            "--web",
        } or arg.startswith(
            ("--body-file=", "--template=", "-T", "-F")
        ):
            raise ValueError(
                "durable pull-request creation accepts literal --body only; "
                "file-reading options are forbidden"
            )
        if arg in {"--title", "-t"}:
            if index + 1 >= len(argv):
                raise ValueError("--title requires a literal value")
            if len(argv[index + 1].encode()) > max_title_bytes:
                raise ValueError("pull-request title exceeds the durable limit")
            title_seen = True
            index += 2
            continue
        if arg.startswith("--title=") or (
            arg.startswith("-t") and arg != "-t"
        ):
            value = (
                arg.split("=", 1)[1]
                if "=" in arg
                else arg[2:]
            )
            if len(value.encode()) > max_title_bytes:
                raise ValueError("pull-request title exceeds the durable limit")
            title_seen = True
            index += 1
            continue
        if arg in {"--body", "-b"}:
            if index + 1 >= len(argv):
                raise ValueError("--body requires a literal value")
            if len(argv[index + 1].encode()) > max_body_bytes:
                raise ValueError("pull-request body exceeds the durable limit")
            body_seen = True
            index += 2
            continue
        if arg.startswith("--body=") or (
            arg.startswith("-b") and arg != "-b"
        ):
            value = (
                arg.split("=", 1)[1]
                if "=" in arg
                else arg[2:]
            )
            if len(value.encode()) > max_body_bytes:
                raise ValueError("pull-request body exceeds the durable limit")
            body_seen = True
            index += 1
            continue
        if arg in {"--base", "-B"}:
            if index + 1 >= len(argv):
                raise ValueError("--base requires a value")
            index += 2
            continue
        if arg.startswith("--base=") or (
            arg.startswith("-B") and arg != "-B"
        ):
            index += 1
            continue
        if arg == "--draft":
            index += 1
            continue
        if arg in {"--repo", "-R", "--head", "-H"}:
            if index + 1 >= len(argv):
                raise ValueError(f"{arg} requires a value")
            index += 2
            continue
        if arg.startswith(("--repo=", "--head=")) or (
            arg.startswith(("-R", "-H")) and arg not in {"-R", "-H"}
        ):
            index += 1
            continue
        raise ValueError(
            f"unsupported pull-request creation option: {arg}"
        )
    if not title_seen or not body_seen:
        raise ValueError(
            "durable pull-request creation requires literal title and body"
        )
    return argv


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
            or authority.role != "fix"
            or authority.kind
            != candidate["ISSUE_AGENT_PUBLICATION_KIND"]
            or str(authority.issue or 0)
            != candidate["ISSUE_AGENT_PUBLICATION_ISSUE"]
            or authority.cwd
            != str(Path(candidate["ISSUE_AGENT_PUBLICATION_CWD"]).resolve())
            or authority.branch
            != candidate["ISSUE_AGENT_PUBLICATION_BRANCH"]
            or not Path(candidate["ISSUE_AGENT_GIT_BIN"]).is_absolute()
            or not Path(candidate["ISSUE_AGENT_GH_BIN"]).is_absolute()
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
        base_branch=authority_env["ISSUE_AGENT_BASE_BRANCH"],
        host=authority_env["ISSUE_AGENT_GITHUB_HOST"],
        config_version=authority_env["ISSUE_AGENT_CONFIG_VERSION"],
        identity=f"agent-publication:{os.getpid()}",
        owner=authority_env["ISSUE_AGENT_PUBLICATION_OWNER"],
        authorized_cwd=authority_env["ISSUE_AGENT_PUBLICATION_CWD"],
        authorized_branch=authority_env["ISSUE_AGENT_PUBLICATION_BRANCH"],
        expected_issue=(
            int(authority_env["ISSUE_AGENT_PUBLICATION_ISSUE"]) or None
        ),
        git_binary=authority_env["ISSUE_AGENT_GIT_BIN"],
        gh_binary=authority_env["ISSUE_AGENT_GH_BIN"],
        trusted_path=authority_env["ISSUE_AGENT_EXEC_PATH"],
    )
    try:
        result = broker.publish(args, os.getcwd())
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"issue-agent-publish: {exc}\n")
        raise SystemExit(1) from exc
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.code)
