"""Durable operator-initiated GitHub effects.

The dashboard, CLI bootstrap commands, and webhook supervisor all use this
adapter.  Every mutation is committed as a ``LifecycleCommand`` before ``gh``
runs, and every ambiguous result is reconciled from GitHub before retry.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import quote

from .control_path import control_store_path
from .control_store import SQLiteControlStore
from .deployment_authority import UnfencedEffectAuthority
from .durable_effects import DurableEffectRunner, EffectOutcome
from ..domain.contracts import (
    CommandId,
    CommandKind,
    EffectStatus,
    LifecycleCommand,
)
from ..domain.repository import canonical_repository

EFFECT_TIMEOUT_SECONDS = 60.0


class OperatorEffectError(RuntimeError):
    pass


class UnknownOperatorEffect(OperatorEffectError):
    pass


def build_operator_effects(
    repository: str,
    state_dirs: Iterable[str],
    *,
    identity: str,
    config_version: str = "",
    run: Callable[..., Any] = subprocess.run,
) -> "GitHubOperatorEffects":
    path = control_store_path(
        state_dirs,
        temporary_on_missing=False,
    )
    store = SQLiteControlStore(path)
    store.initialize()
    return GitHubOperatorEffects(
        repository,
        store,
        identity=identity,
        config_version=config_version,
        run=run,
    )


class GitHubOperatorEffects:
    """Closed, durable vocabulary for non-daemon GitHub mutations."""

    def __init__(
        self,
        repository: str,
        store: SQLiteControlStore,
        *,
        identity: str,
        config_version: str = "",
        run: Callable[..., Any] = subprocess.run,
    ):
        if not repository or not identity:
            raise ValueError("operator effect repository and identity are required")
        self.repository = canonical_repository(repository)
        self.store = store
        self.identity = identity
        self.config_version = config_version
        self.run = run
        self.effects = DurableEffectRunner(
            store,
            UnfencedEffectAuthority(),
            executor_identity=identity,
        )

    def ensure_label(
        self,
        label: str,
        *,
        color: str = "ededed",
        description: str = "",
    ) -> tuple[bool, str]:
        try:
            if self._label_exists(label):
                return True, "ok"
            command = self._command(
                CommandKind.ENSURE_LABEL,
                f"label:{label}",
                "absent",
                {
                    "label": label,
                    "color": color,
                    "description": description,
                },
            )
            self._execute(
                command,
                reconcile=lambda: self._label_exists(label),
                revision=lambda: "present" if self._label_exists(label) else "absent",
                apply=lambda: self._mutate(
                    [
                        "gh",
                        "label",
                        "create",
                        label,
                        "-R",
                        self.repository,
                        "--color",
                        color,
                        "--description",
                        description,
                    ]
                ),
            )
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return False, _detail(exc)

    def set_issue_labels(
        self,
        issue: int,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
    ) -> tuple[bool, str]:
        added = tuple(dict.fromkeys(add))
        removed = tuple(label for label in dict.fromkeys(remove) if label not in added)
        if issue <= 0 or not added and not removed:
            return False, "a positive issue and at least one label change are required"
        try:
            labels, revision = self._issue_labels(issue)
            if _labels_match(labels, added, removed):
                return True, "ok"
            payload = {
                "issue": issue,
                "add": list(added),
                "remove": list(removed),
            }
            command = self._command(
                CommandKind.SET_ISSUE_LABELS,
                f"issue:{issue}",
                revision,
                payload,
            )

            def reconcile() -> bool:
                current, _current_revision = self._issue_labels(issue)
                return _labels_match(current, added, removed)

            def current_revision() -> str:
                _current, value = self._issue_labels(issue)
                return value

            # REST, not `gh issue edit`: the edit path's GraphQL metadata query
            # needs read:org, which a repo-scoped bot PAT lacks (same failure
            # class as the PR #171 relabel wedge — see build_relabel_args).
            # Adds first so a failing second call strands "both labels", never
            # a label-less target; deletes are limited to labels observed
            # present because `gh api` raises on the 404 an absent label earns.
            argvs: list[list[str]] = []
            if added:
                argvs.append(
                    ["gh", "api", "--method", "POST",
                     f"repos/{self.repository}/issues/{issue}/labels"]
                    + [arg for label in added for arg in ("-f", f"labels[]={label}")]
                )
            argvs.extend(
                ["gh", "api", "--method", "DELETE",
                 f"repos/{self.repository}/issues/{issue}/labels/"
                 f"{quote(label, safe='')}"]
                for label in removed
                if label in labels
            )

            def apply() -> None:
                for argv in argvs:
                    self._mutate(argv)

            self._execute(
                command,
                reconcile=reconcile,
                revision=current_revision,
                apply=apply,
            )
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return False, _detail(exc)

    def set_repository_auto_merge(self, enabled: bool) -> tuple[bool, str]:
        try:
            current, revision = self._repository_auto_merge()
            if current is enabled:
                return True, "ok"
            command = self._command(
                CommandKind.SET_REPOSITORY_AUTO_MERGE,
                f"repository:{self.repository}",
                revision,
                {"enabled": enabled},
            )
            self._execute(
                command,
                reconcile=lambda: self._repository_auto_merge()[0] is enabled,
                revision=lambda: self._repository_auto_merge()[1],
                apply=lambda: self._mutate(
                    [
                        "gh",
                        "api",
                        "-X",
                        "PATCH",
                        f"repos/{self.repository}",
                        "-F",
                        f"allow_auto_merge={'true' if enabled else 'false'}",
                    ]
                ),
                unconfirmed_status=EffectStatus.PERMANENT_FAILURE,
            )
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return False, _detail(exc)

    def publish_base_branch(
        self,
        cwd: str,
        branch: str,
    ) -> tuple[bool, str]:
        """Create one missing remote base branch from a verified local head."""

        try:
            local_head = self._text(
                ["git", "-C", cwd, "rev-parse", f"refs/heads/{branch}"]
            ).strip()
            if not local_head:
                raise OperatorEffectError(f"local branch {branch!r} has no head")
            remote = self._remote_branch(cwd, branch)
            if remote == local_head:
                return True, "ok"
            if remote:
                raise OperatorEffectError(
                    f"origin/{branch} exists at an unexpected revision"
                )
            command = self._command(
                CommandKind.PUBLISH_BRANCH,
                f"branch:{self.repository}:{branch}",
                "absent",
                {
                    "cwd": cwd,
                    "branch": branch,
                    "head": local_head,
                    "remote": "origin",
                },
            )
            self._execute(
                command,
                reconcile=lambda: self._remote_branch(cwd, branch) == local_head,
                revision=lambda: self._remote_branch(cwd, branch) or "absent",
                apply=lambda: self._mutate(
                    ["git", "-C", cwd, "push", "-u", "origin", branch]
                ),
            )
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return False, _detail(exc)

    def delete_relay_hooks(self) -> tuple[bool, str]:
        try:
            hooks = self._relay_hooks()
            for hook in hooks:
                hook_id = str(hook["id"])
                expected = _digest(hook)
                command = self._command(
                    CommandKind.DELETE_RELAY_HOOK,
                    f"relay-hook:{hook_id}",
                    expected,
                    {"hook_id": hook_id},
                )
                self._execute(
                    command,
                    reconcile=lambda hook_id=hook_id: all(
                        str(item["id"]) != hook_id for item in self._relay_hooks()
                    ),
                    revision=lambda hook_id=hook_id: self._hook_revision(hook_id),
                    apply=lambda hook_id=hook_id: self._mutate(
                        [
                            "gh",
                            "api",
                            "-X",
                            "DELETE",
                            f"repos/{self.repository}/hooks/{hook_id}",
                        ]
                    ),
                )
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - webhook remains poll-capable
            return False, _detail(exc)

    def start_relay_forward(
        self,
        events: str,
        *,
        spawn: Callable[..., Any] = subprocess.Popen,
    ) -> Any:
        """Start the extension only after a durable command has begun."""

        self._reconcile_pending_relay_start()
        deleted, detail = self.delete_relay_hooks()
        if not deleted:
            raise OperatorEffectError(f"relay-hook cleanup failed: {detail}")
        baseline = _digest(self._relay_hooks())
        command = self._command(
            CommandKind.START_RELAY_FORWARD,
            f"relay-forward:{self.repository}",
            baseline,
            {"events": events},
        )
        pending = self.effects.enqueue(command)
        if pending.state == EffectStatus.SUCCEEDED.value:
            pending = self.effects.enqueue_occurrence(command)
            command = pending.command
        elif pending.state in {"executing", EffectStatus.UNKNOWN_OUTCOME.value}:
            if self._relay_hooks():
                self.effects.resolve_unknown(
                    command,
                    EffectOutcome(None, EffectStatus.SUCCEEDED),
                )
                pending = self.effects.enqueue_occurrence(command)
                command = pending.command
            else:
                self.effects.retry_reconciled(command)
        argv = [
            "gh",
            "webhook",
            "forward",
            f"--repo={self.repository}",
            f"--events={events}",
        ]

        def spawn_and_verify() -> EffectOutcome[Any]:
            try:
                process = spawn(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    text=True,
                    bufsize=1,
                )
            except BaseException as exc:
                return EffectOutcome(
                    exc,
                    EffectStatus.RETRYABLE_FAILURE,
                    error=f"{type(exc).__name__}: {exc}",
                )
            try:
                hooks = self._relay_hooks()
            except Exception as exc:  # noqa: BLE001 - outcome is ambiguous
                return EffectOutcome(
                    process,
                    EffectStatus.UNKNOWN_OUTCOME,
                    error=f"relay hook could not be verified: {exc}",
                )
            if hooks:
                return EffectOutcome(
                    process,
                    EffectStatus.SUCCEEDED,
                    remote_object_id=str(hooks[-1]["id"]),
                    remote_revision=_digest(hooks),
                )
            return EffectOutcome(
                process,
                EffectStatus.UNKNOWN_OUTCOME,
                error="forwarder started but relay hook is not yet observable",
            )

        result = self.effects.execute(command, spawn_and_verify)
        if isinstance(result, BaseException):
            raise result
        return result

    def _execute(
        self,
        command: LifecycleCommand,
        *,
        reconcile: Callable[[], bool],
        revision: Callable[[], str],
        apply: Callable[[], object],
        unconfirmed_status: EffectStatus = EffectStatus.RETRYABLE_FAILURE,
    ) -> None:
        pending = self.effects.enqueue(command)
        command = pending.command
        if pending.state == EffectStatus.SUCCEEDED.value:
            if reconcile():
                return
            pending = self.effects.enqueue_occurrence(command)
            command = pending.command
        if pending.state == EffectStatus.PERMANENT_FAILURE.value:
            pending = self.effects.enqueue_occurrence(command)
            command = pending.command
        if pending.state in {"executing", EffectStatus.UNKNOWN_OUTCOME.value}:
            if reconcile():
                self.effects.resolve_unknown(
                    command,
                    EffectOutcome(None, EffectStatus.SUCCEEDED),
                )
                return
            if revision() != command.expected_revision:
                raise UnknownOperatorEffect(
                    f"{command.command_id}: target changed while outcome was unknown"
                )
            self.effects.retry_reconciled(command)
        if revision() != command.expected_revision:
            self.effects.execute(
                command,
                lambda: EffectOutcome(
                    None,
                    EffectStatus.PERMANENT_FAILURE,
                    error="expected revision no longer matches remote target",
                ),
            )
            raise OperatorEffectError("remote target changed before mutation")

        def apply_and_verify() -> EffectOutcome[bool]:
            try:
                apply()
            except BaseException:
                try:
                    applied = reconcile()
                except Exception:
                    applied = False
                if not applied:
                    raise
                return EffectOutcome(True, EffectStatus.SUCCEEDED)
            applied = reconcile()
            error = (
                None
                if applied
                else (
                    "remote rejected the requested state"
                    if unconfirmed_status is EffectStatus.PERMANENT_FAILURE
                    else "remote state did not confirm mutation"
                )
            )
            return EffectOutcome(
                applied,
                (
                    EffectStatus.SUCCEEDED
                    if applied
                    else unconfirmed_status
                ),
                error=error,
            )

        applied = self.effects.execute(command, apply_and_verify)
        if not applied:
            error = (
                "remote rejected the requested state"
                if unconfirmed_status is EffectStatus.PERMANENT_FAILURE
                else "remote state did not confirm mutation"
            )
            raise OperatorEffectError(error)

    def _command(
        self,
        kind: CommandKind,
        target: str,
        expected_revision: str,
        payload: Mapping[str, object],
    ) -> LifecycleCommand:
        encoded = json.dumps(
            {
                "kind": kind.value,
                "target": target,
                "expected_revision": expected_revision,
                "payload": payload,
                "repository": self.repository,
                "identity": self.identity,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        return LifecycleCommand(
            CommandId(digest),
            f"operator-github:{digest}",
            kind,
            target,
            expected_revision,
            payload,
            self.config_version,
            self.repository,
            self.identity,
        )

    def _label_exists(self, label: str) -> bool:
        rows = self._json(
            [
                "gh",
                "label",
                "list",
                "-R",
                self.repository,
                "--json",
                "name",
                "--limit",
                "1000",
            ]
        )
        return isinstance(rows, list) and any(
            isinstance(row, dict) and row.get("name") == label for row in rows
        )

    def _issue_labels(self, issue: int) -> tuple[frozenset[str], str]:
        row = self._json(
            [
                "gh",
                "issue",
                "view",
                str(issue),
                "-R",
                self.repository,
                "--json",
                "labels,updatedAt",
            ]
        )
        if not isinstance(row, dict) or not isinstance(row.get("updatedAt"), str):
            raise OperatorEffectError(f"issue #{issue} did not return a revision")
        labels = frozenset(
            str(item["name"])
            for item in row.get("labels", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        return labels, str(row["updatedAt"])

    def _repository_auto_merge(self) -> tuple[bool, str]:
        row = self._json(
            [
                "gh",
                "api",
                f"repos/{self.repository}",
                "--jq",
                "{allow_auto_merge,updated_at}",
            ]
        )
        if not isinstance(row, dict) or not isinstance(row.get("updated_at"), str):
            raise OperatorEffectError("repository did not return a revision")
        return bool(row.get("allow_auto_merge")), str(row["updated_at"])

    def _relay_hooks(self) -> list[dict[str, object]]:
        rows = self._json(
            [
                "gh",
                "api",
                f"repos/{self.repository}/hooks",
                "--jq",
                '[.[] | select(.name == "cli") '
                "| {id,active,updated_at,created_at}]",
            ]
        )
        if not isinstance(rows, list):
            raise OperatorEffectError("relay-hook query returned an invalid payload")
        return [
            dict(row)
            for row in rows
            if isinstance(row, dict) and row.get("id") is not None
        ]

    def _remote_branch(self, cwd: str, branch: str) -> str:
        output = self._text(
            [
                "git",
                "-C",
                cwd,
                "ls-remote",
                "--heads",
                "origin",
                f"refs/heads/{branch}",
            ]
        ).strip()
        if not output:
            return ""
        return output.split()[0]

    def _hook_revision(self, hook_id: str) -> str:
        for hook in self._relay_hooks():
            if str(hook["id"]) == hook_id:
                return _digest(hook)
        return "absent"

    def _reconcile_pending_relay_start(self) -> None:
        for pending in self.store.pending_commands(
            repository=self.repository,
            executor_identity=self.identity,
        ):
            if pending.command.kind is not CommandKind.START_RELAY_FORWARD:
                continue
            hooks = self._relay_hooks()
            if hooks:
                self.effects.resolve_unknown(
                    pending.command,
                    EffectOutcome(None, EffectStatus.SUCCEEDED),
                )
            elif pending.state in {
                "executing",
                EffectStatus.UNKNOWN_OUTCOME.value,
            }:
                self.effects.retry_reconciled(pending.command)

    def _json(self, argv: list[str]) -> object:
        completed = self.run(
            argv,
            capture_output=True,
            text=True,
            timeout=EFFECT_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise OperatorEffectError(
                (completed.stderr or completed.stdout or "gh command failed").strip()
            )
        try:
            return json.loads(completed.stdout or "null")
        except json.JSONDecodeError as exc:
            raise OperatorEffectError("gh returned malformed JSON") from exc

    def _text(self, argv: list[str]) -> str:
        completed = self.run(
            argv,
            capture_output=True,
            text=True,
            timeout=EFFECT_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise OperatorEffectError(
                (completed.stderr or completed.stdout or "command failed").strip()
            )
        return str(completed.stdout or "")

    def _mutate(self, argv: list[str]) -> None:
        completed = self.run(
            argv,
            capture_output=True,
            text=True,
            timeout=EFFECT_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise OperatorEffectError(
                (completed.stderr or completed.stdout or "gh mutation failed").strip()
            )


def _labels_match(
    current: frozenset[str],
    added: tuple[str, ...],
    removed: tuple[str, ...],
) -> bool:
    return all(label in current for label in added) and all(
        label not in current for label in removed
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]
