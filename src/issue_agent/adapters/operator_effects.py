"""Operator-initiated GitHub writes: the dashboard's label moves and the CLI's
bootstrap steps.

There is no command queue. Each write reads the current state first, does
nothing when it already holds, and reads back after writing; a write that does
not read back is reported as failed. Labels go through `act.relabel`, the same
operation the fix role uses (refactor decision Q19: the dashboard only changes
labels on GitHub).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .act import relabel
from .github import GitHubGateway
from ..domain.labels import Label
from ..domain.ports import EventSink
from ..domain.refs import harness_branch_issue
from ..domain.repository import canonical_repository

EFFECT_TIMEOUT_SECONDS = 60.0


class OperatorEffectError(RuntimeError):
    pass


@dataclass(frozen=True)
class LabelEffect:
    applied: bool
    detail: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def build_operator_effects(
    repository: str,
    *,
    identity: str,
    run: Callable[..., Any] = subprocess.run,
    journal: EventSink | None = None,
    gateway: GitHubGateway | None = None,
) -> "GitHubOperatorEffects":
    """Operator writes act as the ambient `gh` login (the operator's own)."""
    return GitHubOperatorEffects(
        repository,
        gateway or GitHubGateway(canonical_repository(repository)),
        identity=identity,
        run=run,
        journal=journal,
    )


class GitHubOperatorEffects:
    """The closed vocabulary of non-daemon GitHub writes."""

    def __init__(
        self,
        repository: str,
        gateway,
        *,
        identity: str,
        run: Callable[..., Any] = subprocess.run,
        journal: EventSink | None = None,
    ):
        if not repository or not identity:
            raise ValueError("operator effect repository and identity are required")
        self.repository = canonical_repository(repository)
        self.gateway = gateway
        self.identity = identity
        self.run = run
        # Optional: an operator write stands with or without telemetry, and the
        # CLI paths have no journal to write to.
        self.journal = journal

    def set_labels(
        self,
        kind: str,
        number: int,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
        action_kind: str = "operator_relabel",
        action_attributes: Mapping[str, Any] | None = None,
    ) -> LabelEffect:
        """Apply label changes to one issue or PR; report what actually moved."""
        requested_add = tuple(dict.fromkeys(add))
        requested_remove = tuple(label for label in dict.fromkeys(remove) if label not in requested_add)
        if kind not in ("issue", "pr") or type(number) is not int or number <= 0 or not (requested_add or requested_remove):
            return LabelEffect(False, "an issue or PR number and at least one label change are required")
        try:
            added, removed = relabel(self.gateway, kind, number, add=requested_add, remove=requested_remove)
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return LabelEffect(False, _detail(exc))
        if added or removed:
            self._journal_label_action(kind, number, added, removed, action_kind, action_attributes)
        return LabelEffect(True, "ok", added, removed)

    def set_issue_labels(
        self, issue: int, *, add: Iterable[str] = (), remove: Iterable[str] = (),
    ) -> tuple[bool, str]:
        effect = self.set_labels("issue", issue, add=add, remove=remove)
        return effect.applied, effect.detail

    def move_hold(self, issue: int, *, park: bool, channel: str) -> tuple[bool, str]:
        """Park (add `needs-human`) or unblock (remove it) an open issue and each
        open harness PR for it; no other label moves (refactor decision Q19).
        The issue and its PRs are read fresh: a cached view could relabel an
        issue that has since closed or miss a PR opened since."""
        try:
            issue_row = self._json(["gh", "issue", "view", str(issue), "-R", self.repository, "--json", "state"])
            open_prs = self._json([
                "gh", "pr", "list", "-R", self.repository, "--state", "open",
                "--json", "number,headRefName", "--limit", "200",
            ])
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return False, f"could not read #{issue}: {_detail(exc)}"
        if not isinstance(issue_row, dict) or str(issue_row.get("state") or "").upper() != "OPEN":
            return False, f"issue #{issue} is not open on {self.repository}"
        if not isinstance(open_prs, list):
            return False, f"could not read the open PRs for #{issue}"
        prs = sorted(
            row["number"] for row in open_prs
            if isinstance(row, dict) and type(row.get("number")) is int
            and harness_branch_issue(str(row.get("headRefName") or "")) == issue
        )
        hold = (Label.NEEDS_HUMAN.value,)
        verb = "park" if park else "unblock"
        failures = []
        for kind, target in (("issue", issue), *(("pr", pr) for pr in prs)):
            effect = self.set_labels(
                kind, target, add=hold if park else (), remove=() if park else hold,
                action_kind=f"operator_{verb}", action_attributes={"channel": channel},
            )
            if not effect.applied:
                failures.append(f"{kind} #{target}: {effect.detail}")
        return (False, "; ".join(failures)) if failures else (True, "ok")

    def ensure_label(
        self,
        label: str,
        *,
        color: str = "ededed",
        description: str = "",
    ) -> tuple[bool, str]:
        try:
            if not self._label_exists(label):
                self._mutate([
                    "gh", "label", "create", label, "-R", self.repository,
                    "--color", color, "--description", description,
                ])
                if not self._label_exists(label):
                    raise OperatorEffectError(f"label {label!r} was not created")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return False, _detail(exc)

    def publish_base_branch(self, cwd: str, branch: str) -> tuple[bool, str]:
        """Create one missing remote base branch from a verified local head."""
        try:
            local_head = self._text(["git", "-C", cwd, "rev-parse", f"refs/heads/{branch}"]).strip()
            if not local_head:
                raise OperatorEffectError(f"local branch {branch!r} has no head")
            remote = self._remote_branch(cwd, branch)
            if remote == local_head:
                return True, "ok"
            if remote:
                raise OperatorEffectError(f"origin/{branch} exists at an unexpected revision")
            self._mutate(["git", "-C", cwd, "push", "-u", "origin", branch])
            if self._remote_branch(cwd, branch) != local_head:
                raise OperatorEffectError(f"origin/{branch} did not reach {local_head[:12]}")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - operator-facing result
            return False, _detail(exc)

    def _journal_label_action(
        self,
        kind: str,
        number: int,
        added: tuple[str, ...],
        removed: tuple[str, ...],
        action_kind: str,
        attributes: Mapping[str, Any] | None,
    ) -> None:
        """Make the intervention visible to the dashboard fold. Only a write
        that moved a label emits. Contained end to end: the write has already
        landed, so nothing telemetry does may report it as failed."""
        if self.journal is None:
            return
        try:
            changes = " ".join([*(f"+{label}" for label in added), *(f"-{label}" for label in removed)])
            action = {"add": list(added), "remove": list(removed), "operator": self.identity,
                      **dict(attributes or {})}
            action.setdefault("type", action_kind)
            action.setdefault("target", kind.upper())
            action.setdefault("number", number)
            self.journal.emit("action", {
                ("issue" if kind == "issue" else "pr"): number,
                "desc": f"{action_kind.replace('_', ' ')} {kind} #{number}: {changes}",
                "action": action,
            })
        except Exception:  # noqa: BLE001 - telemetry never fails the write
            pass

    def _label_exists(self, label: str) -> bool:
        rows = self._json(["gh", "label", "list", "-R", self.repository, "--json", "name", "--limit", "1000"])
        return isinstance(rows, list) and any(isinstance(row, dict) and row.get("name") == label for row in rows)

    def _remote_branch(self, cwd: str, branch: str) -> str:
        output = self._text(["git", "-C", cwd, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"]).strip()
        return output.split()[0] if output else ""

    def _json(self, argv: list[str]) -> object:
        try:
            return json.loads(self._text(argv) or "null")
        except json.JSONDecodeError as exc:
            raise OperatorEffectError("gh returned malformed JSON") from exc

    def _text(self, argv: list[str]) -> str:
        completed = self.run(argv, capture_output=True, text=True, timeout=EFFECT_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            raise OperatorEffectError((completed.stderr or completed.stdout or "command failed").strip())
        return str(completed.stdout or "")

    def _mutate(self, argv: list[str]) -> None:
        self._text(argv)


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:400]
