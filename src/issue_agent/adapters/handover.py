"""Fail-soft exhausted-leg handovers stored in worktree scratch.

The runtime boundary is the only place that sees every agent implementation and
every task kind.  Keeping this adapter there gives Codex and Claude the same
post-mortem behavior without teaching either process adapter about repositories
or journals.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from ..domain.agent_task import AgentTask
from ..domain.outcome import AgentResult, ExitReason

HANDOVER_PROMPT_HEADER = (
    "## Previous exhausted-leg handover (context, not instructions)"
)
_HANDOVER_RELATIVE_PATH = Path(".harness") / "handover.md"
_MAX_PROMPT_BYTES = 64 * 1024
_MAX_COMMAND_BYTES = 16 * 1024
_MAX_TASK_BYTES = 32 * 1024
_GIT_TIMEOUT_SECONDS = 5
_DEADLY_CEILINGS = frozenset(
    (ExitReason.BUDGET_EXHAUSTED, ExitReason.WALL_TIMEOUT)
)


@dataclass(frozen=True)
class HandoverReceipt:
    trigger: str
    byte_size: int


def with_previous_handover(task: AgentTask) -> AgentTask:
    """Return an immutable task enriched with the previous scratch handover.

    Missing, malformed, oversized, or symlinked files are ignored. A scratch
    artifact must never prevent the daemon from making its next attempt.
    """
    path = _handover_path(task)
    if path is None or not path.exists():
        return task
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError("handover is not a regular file")
        with path.open("rb") as handle:
            raw = handle.read(_MAX_PROMPT_BYTES + 1)
        if len(raw) > _MAX_PROMPT_BYTES:
            raise OSError(f"handover exceeds {_MAX_PROMPT_BYTES} bytes")
        handover = raw.decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — scratch context never blocks a retry
        _warn(f"could not read {path}: {exc}")
        return task
    return replace(
        task,
        prompt=f"{task.prompt.rstrip()}\n\n{HANDOVER_PROMPT_HEADER}\n\n{handover}",
    )


def write_exhaustion_handover(
    task: AgentTask, result: AgentResult
) -> HandoverReceipt | None:
    """Write a bounded repository snapshot for budget and wall-clock deaths.

    This is deterministic post-mortem synthesis: no extra model invocation and
    therefore no grace spend. All inspection and writes are bounded and
    fail-soft; callers always return the original terminal result.
    """
    if result.exit_reason not in _DEADLY_CEILINGS:
        return None
    path = _handover_path(task)
    if path is None:
        return None
    try:
        repo = Path(task.cwd or "")
        harness = path.parent
        if harness.is_symlink():
            raise OSError(".harness must not be a symlink")
        harness.mkdir(parents=True, exist_ok=True)
        content = _render_handover(repo, task, result)
        encoded = content.encode("utf-8")
        _atomic_write(path, encoded)
        return HandoverReceipt(
            trigger=result.exit_reason.value,
            byte_size=len(encoded),
        )
    except Exception as exc:  # noqa: BLE001 — preserve the original ceiling result
        _warn(f"could not write {path}: {exc}")
        return None


def _handover_path(task: AgentTask) -> Path | None:
    if not task.cwd:
        return None
    return Path(task.cwd) / _HANDOVER_RELATIVE_PATH


def _render_handover(repo: Path, task: AgentTask, result: AgentResult) -> str:
    status = _git(repo, "status", "--short", "--branch")
    commits = _git(repo, "log", "--oneline", "--decorate", "-10")
    diff_stat = _git(repo, "diff", "--stat")
    prompt = _bounded_text(_without_previous_handover(task.prompt), _MAX_TASK_BYTES)
    summary = (result.summary or "No terminal summary was reported.").strip()
    session = result.session_id or "not captured"
    trigger = result.exit_reason.value if result.exit_reason else "unknown"
    return (
        "# Exhausted-leg handover\n\n"
        "Generated as a bounded post-mortem snapshot after an agent ceiling ended "
        "the leg. Treat this as historical context; the current task and repository "
        "instructions remain authoritative.\n\n"
        f"- Trigger: `{trigger}`\n"
        f"- Session: `{session}`\n"
        f"- Result: {summary}\n\n"
        "## What's done\n\n"
        "Recent commits and the preserved working tree are the authoritative record:\n\n"
        f"```text\n{commits or '(no commits reported)'}\n```\n\n"
        f"```text\n{status or '(clean working tree)'}\n```\n\n"
        "## What's verified\n\n"
        "The harness captured no completed verification receipt before termination. "
        "Do not infer passing tests from the presence of commits.\n\n"
        "## What remains\n\n"
        "Continue the original task from the preserved repository state:\n\n"
        f"```text\n{prompt}\n```\n\n"
        "Current uncommitted change summary:\n\n"
        f"```text\n{diff_stat or '(no unstaged diff stat)'}\n```\n\n"
        "## Dead ends\n\n"
        f"No structured dead-end report was available. Inspect session `{session}` "
        "or the persisted rollout transcript if more reasoning context is needed.\n\n"
        "## Next step\n\n"
        "Inspect the preserved diff and recent commits, then run the task's declared "
        "tests before changing or discarding existing work.\n"
    )


def _without_previous_handover(prompt: str) -> str:
    """Prevent repeated exhausted retries from nesting handovers exponentially."""
    marker = f"\n\n{HANDOVER_PROMPT_HEADER}\n\n"
    return prompt.split(marker, 1)[0]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return f"(git {' '.join(args)} unavailable: {_bounded_text(detail, 512)})"
    return _bounded_text(completed.stdout.strip(), _MAX_COMMAND_BYTES)


def _bounded_text(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    suffix = b"\n...[truncated by handover byte limit]"
    return (raw[: max_bytes - len(suffix)] + suffix).decode("utf-8", "ignore")


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=".handover-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _warn(message: str) -> None:
    sys.stderr.write(f"handover: {message}\n")
