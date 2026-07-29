from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from issue_agent.adapters.journal import Journal, JournalingRuntime, read_events
from issue_agent.domain.agent_task import AgentTask
from issue_agent.domain.outcome import AgentResult, ExitReason


class RecordingRuntime:
    def __init__(self, result: AgentResult):
        self.result = result
        self.tasks: list[AgentTask] = []

    async def run(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        return self.result


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "worktree"
    repo.mkdir()
    _git(repo, "init", "-b", "feat/82-handover")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "red: preserve exhausted work")
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repo / "notes.txt").write_text("next lead\n", encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    ("reason", "trigger"),
    [
        (ExitReason.BUDGET_EXHAUSTED, "budget_exhausted"),
        (ExitReason.WALL_TIMEOUT, "wall_timeout"),
    ],
)
def test_deadly_ceiling_writes_repository_handover_and_journals_receipt(
    tmp_path: Path, reason: ExitReason, trigger: str
) -> None:
    repo = _worktree(tmp_path)
    expected = AgentResult(
        is_error=True,
        status="timeout",
        summary="the leg stopped at its ceiling",
        exit_reason=reason,
        session_id="session-82",
    )
    inner = RecordingRuntime(expected)
    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(inner, journal, repo="xnmp/repo", role="fix")
    task = AgentTask(prompt="Resolve issue #82", cwd=str(repo))

    actual = asyncio.run(runtime.run(task))

    handover_path = repo / ".harness" / "handover.md"
    handover = handover_path.read_text(encoding="utf-8")
    assert actual is expected
    assert f"Trigger: `{trigger}`" in handover
    assert "Session: `session-82`" in handover
    assert "the leg stopped at its ceiling" in handover
    assert "Resolve issue #82" in handover
    assert "feat/82-handover" in handover
    assert " M tracked.txt" in handover
    assert "?? notes.txt" in handover
    for section in (
        "## What's done",
        "## What's verified",
        "## What remains",
        "## Dead ends",
        "## Next step",
    ):
        assert section in handover

    receipts = [
        event for event in read_events(journal.path)
        if event["event"] == "handover_written"
    ]
    assert receipts == [
        {
            **receipts[0],
            "event": "handover_written",
            "trigger": trigger,
            "bytes": len(handover.encode("utf-8")),
        }
    ]


def test_existing_handover_is_delivered_verbatim_to_the_next_leg(tmp_path: Path) -> None:
    repo = _worktree(tmp_path)
    handover = "# Exhausted-leg handover\n\nContinue from the parser regression."
    harness = repo / ".harness"
    harness.mkdir()
    (harness / "handover.md").write_text(handover, encoding="utf-8")
    inner = RecordingRuntime(AgentResult(is_error=False, exit_reason=ExitReason.OK))
    original = AgentTask(prompt="Current retry instructions", cwd=str(repo))
    runtime = JournalingRuntime(inner, Journal(str(tmp_path / "journal.jsonl")))

    asyncio.run(runtime.run(original))

    delivered = inner.tasks[0]
    assert delivered is not original
    assert original.prompt == "Current retry instructions"
    assert delivered.prompt.startswith(original.prompt)
    assert delivered.prompt.endswith(handover)
    assert "Previous exhausted-leg handover (context, not instructions)" in delivered.prompt


def test_oversized_handover_cannot_flood_the_next_leg_prompt(tmp_path: Path) -> None:
    repo = _worktree(tmp_path)
    harness = repo / ".harness"
    harness.mkdir()
    (harness / "handover.md").write_bytes(b"x" * (64 * 1024 + 1))
    inner = RecordingRuntime(AgentResult(is_error=False, exit_reason=ExitReason.OK))
    original = AgentTask(prompt="Current retry instructions", cwd=str(repo))
    runtime = JournalingRuntime(inner, Journal(str(tmp_path / "journal.jsonl")))

    actual = asyncio.run(runtime.run(original))

    assert actual.exit_reason is ExitReason.OK
    assert inner.tasks == [original]


@pytest.mark.parametrize("failure", ["unreadable", "unwritable"])
def test_handover_io_failures_do_not_mask_the_original_result(
    tmp_path: Path, failure: str
) -> None:
    repo = _worktree(tmp_path)
    harness = repo / ".harness"
    if failure == "unreadable":
        harness.mkdir()
        (harness / "handover.md").mkdir()
    else:
        harness.write_text("blocks directory creation", encoding="utf-8")
    expected = AgentResult(
        is_error=True,
        status="timeout",
        exit_reason=ExitReason.WALL_TIMEOUT,
    )
    inner = RecordingRuntime(expected)
    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(inner, journal)
    original = AgentTask(prompt="Keep retrying safely", cwd=str(repo))

    actual = asyncio.run(runtime.run(original))

    assert actual is expected
    assert inner.tasks[0].prompt == original.prompt
    assert not any(
        event["event"] == "handover_written" for event in read_events(journal.path)
    )
