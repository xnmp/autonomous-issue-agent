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
from issue_agent.domain.actions import PostComment, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import (
    ExitReason,
    Outcome,
    classify_outcome,
    infra_class_failure,
    infra_class_reason,
    resolve_actions,
    resume_actions,
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


def _started(task_id: str) -> _SystemMessage:
    return _SystemMessage("task_started", {"task_id": task_id, "description": "adversarial review"})


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


def _asks_human(actions) -> bool:
    return any(isinstance(a, PostComment) for a in actions) or any(
        isinstance(a, Relabel) and Label.AI_BLOCKED in a.added_labels for a in actions
    )


def test_blocked_over_pending_background_child_is_a_typed_infra_outcome(monkeypatch):
    logged: list[str] = []
    result = _run(
        monkeypatch,
        [_started("t1"), _ResultMessage(structured_output=_BLOCKED)],
        log=logged.append,
    )
    assert classify_outcome(result) is Outcome.ERROR
    assert result.exit_reason is ExitReason.BACKGROUND_CHILD_PENDING
    assert infra_class_failure(result)
    assert infra_class_reason(result) == "background_child_pending"
    # The agent's own claim survives as a claim for the operator's log line.
    assert result.status == "blocked"
    assert any("t1" in line and "BACKGROUND CHILD PENDING" in line for line in logged)


def test_resolve_requeues_instead_of_asking_a_human(monkeypatch):
    result = _run(monkeypatch, [_started("t1"), _ResultMessage(structured_output=_BLOCKED)])
    actions = resolve_actions(253, result, None)
    assert not _asks_human(actions)
    assert actions == [Relabel(Target.ISSUE, 253, Label.AI_WORKING, Label.AI_READY)]


def test_resume_requeues_instead_of_reblocking(monkeypatch):
    result = _run(monkeypatch, [_started("t1"), _ResultMessage(structured_output=_BLOCKED)])
    actions = resume_actions(253, result, None)
    assert not _asks_human(actions)
    assert actions == [Relabel(Target.ISSUE, 253, Label.AI_WORKING, Label.AI_READY)]


def test_child_still_pending_at_result_counts_even_if_it_finishes_later(monkeypatch):
    followup = _ResultMessage(structured_output={"status": "pr_opened"})
    followup.origin = {"kind": "task-notification"}
    result = _run(
        monkeypatch,
        [_started("t1"), _ResultMessage(structured_output=_BLOCKED), _notified("t1"), followup],
    )
    assert result.exit_reason is ExitReason.BACKGROUND_CHILD_PENDING
    assert not _asks_human(resolve_actions(253, result, None))


def test_non_terminal_task_update_keeps_the_child_pending(monkeypatch):
    result = _run(
        monkeypatch,
        [_started("t1"), _updated("t1", "running"), _ResultMessage(structured_output=_BLOCKED)],
    )
    assert result.exit_reason is ExitReason.BACKGROUND_CHILD_PENDING


def test_genuine_clarification_block_still_surfaces_to_humans(monkeypatch):
    result = _run(monkeypatch, [_ResultMessage(structured_output=_BLOCKED)])
    assert classify_outcome(result) is Outcome.BLOCKED
    assert result.exit_reason is ExitReason.OK
    actions = resolve_actions(253, result, None)
    assert _asks_human(actions)
    assert any(isinstance(a, PostComment) and _BLOCKED["needs"] in a.body for a in actions)


def test_block_after_children_finished_still_surfaces_to_humans(monkeypatch):
    result = _run(
        monkeypatch,
        [
            _started("t1"), _notified("t1", "failed"),
            _started("t2"), _updated("t2", "killed"),
            _ResultMessage(structured_output=_BLOCKED),
        ],
    )
    assert classify_outcome(result) is Outcome.BLOCKED
    assert _asks_human(resolve_actions(253, result, None))


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


def test_pending_child_does_not_demote_a_non_blocked_result(monkeypatch):
    result = _run(
        monkeypatch,
        [_started("t1"), _ResultMessage(structured_output={"status": "pr_opened"})],
    )
    assert classify_outcome(result) is Outcome.OK
    assert result.exit_reason is ExitReason.OK

