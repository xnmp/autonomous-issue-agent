"""#255: a Claude leg that ends its turn over its own pending background child.

Canary #253 launched its adversarial reviewer as a background subagent, ended
its turn before the notification arrived, and declared `blocked` — which the
harness surfaced to a human as a clarification question. These tests drive
`AgentRunner.run` over a stubbed SDK stream and assert on what the harness
does with the returned result: the follow-up actions and the infra reason it
journals.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field


from issue_agent.adapters.agent_runner import AgentRunner
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.outcome import (
    ExitReason,
    Outcome,
    classify_outcome,
    infra_class_failure,
)


@dataclass
class _ResultMessage:
    is_error: bool = False
    structured_output: object = None
    result: str = ""
    total_cost_usd: float = 0.0
    session_id: str = "sess"
    subtype: str = "success"
    usage: object = None
    origin: object = None


@dataclass
class _SystemMessage:
    subtype: str
    data: dict = field(default_factory=dict)


def _started(task_id: str, task_type: object = "local_agent") -> _SystemMessage:
    return _SystemMessage(
        "task_started",
        {"task_id": task_id, "description": "adversarial review", "task_type": task_type},
    )


def _notified(task_id: str, status: str = "completed") -> _SystemMessage:
    return _SystemMessage("task_notification", {"task_id": task_id, "status": status})


def _updated(task_id: str, status: str) -> _SystemMessage:
    return _SystemMessage("task_updated", {"task_id": task_id, "patch": {"status": status}})


_BLOCKED = {
    "status": "blocked",
    "summary": "waiting for reviewer",
    "needs": "Resume the leg and wait for the adversarial reviewer's PASS",
}


def _run(monkeypatch, messages, log=None):
    async def fake_query(prompt, options):
        for m in messages:
            yield m

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = lambda **kw: object()
    fake.ResultMessage = _ResultMessage
    fake.query = fake_query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    runner = AgentRunner(log=log) if log is not None else AgentRunner()
    return asyncio.run(runner.run(RuntimePlan(prompt="x")))


def test_blocked_over_pending_background_child_is_a_typed_attempt_outcome(monkeypatch):
    logged: list[str] = []
    result = _run(
        monkeypatch,
        [_started("t1"), _ResultMessage(structured_output=_BLOCKED)],
        log=logged.append,
    )
    assert classify_outcome(result) is Outcome.ERROR
    assert result.exit_reason is ExitReason.BACKGROUND_CHILD_PENDING
    assert not infra_class_failure(result)
    # The agent's own claim survives as a claim for the operator's log line.
    assert result.status == "blocked"
    assert any("t1" in line and "BACKGROUND CHILD PENDING" in line for line in logged)


def test_non_terminal_task_update_keeps_the_child_pending(monkeypatch):
    result = _run(
        monkeypatch,
        [_started("t1"), _updated("t1", "running"), _ResultMessage(structured_output=_BLOCKED)],
    )
    assert result.exit_reason is ExitReason.BACKGROUND_CHILD_PENDING


def test_malformed_task_events_are_ignored(monkeypatch):
    result = _run(
        monkeypatch,
        [
            _SystemMessage("task_started", {"description": "no id"}),
            _SystemMessage("task_started", {"task_id": ""}),
            types.SimpleNamespace(subtype="task_started", data="not a dict"),
            _ResultMessage(structured_output=_BLOCKED),
        ],
    )
    assert classify_outcome(result) is Outcome.BLOCKED


def test_delegated_workflow_task_counts_as_a_pending_child(monkeypatch):
    result = _run(
        monkeypatch,
        [_started("w1", "local_workflow"), _ResultMessage(structured_output=_BLOCKED)],
    )
    assert result.exit_reason is ExitReason.BACKGROUND_CHILD_PENDING


def test_thousands_of_settled_children_leave_no_pending_child(monkeypatch):
    events = []
    for i in range(5_000):
        events += [_started(f"t{i}"), _notified(f"t{i}")]
    result = _run(monkeypatch, [*events, _ResultMessage(structured_output=_BLOCKED)])
    assert classify_outcome(result) is Outcome.BLOCKED
