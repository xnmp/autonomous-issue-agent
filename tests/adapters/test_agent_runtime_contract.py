"""Shared contract tests for headless agent runtime adapters.

The concrete Claude SDK and Codex CLI adapters have different transport details,
but workflow code depends on the same normalized `AgentResult` semantics. These
tests intentionally exercise pure adapter mapping functions so they stay offline
and do not pin subprocess or SDK internals.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from issue_agent.adapters.agent_runner import result_to_agent_result
from issue_agent.adapters.codex_runner import parse_codex_envelope
from issue_agent.adapters.journal import Journal, JournalingRuntime, read_events
from issue_agent.domain.dashboard import build_issue_rows, first_pass_verdicts, stage_for
from issue_agent.domain.agent_task import AgentRuntime, AgentTask, McpServer, SubagentDef
from issue_agent.domain.outcome import AgentResult, Outcome, classify_outcome


def _claude_result(*, structured, is_error=False, result="", session_id="s1"):
    return result_to_agent_result(
        SimpleNamespace(
            structured_output=structured,
            is_error=is_error,
            result=result,
            total_cost_usd=0.25,
            session_id=session_id,
        )
    )


def _codex_result(*, structured, exit_code=0, session_id="s1", stderr=""):
    return parse_codex_envelope(
        exit_code=exit_code,
        last_message=json.dumps(structured),
        session_id=session_id,
        stderr=stderr,
    )


class _StaticRuntime:
    def __init__(self, result):
        self.result = result

    async def run(self, _task):
        return self.result


def _settled_review_event(tmp_path, result, kind: str) -> dict:
    journal = Journal(str(tmp_path / f"{kind}-{result.status}.jsonl"))
    runtime = JournalingRuntime(_StaticRuntime(result), journal, role="review")
    runtime.set_context("review", kind, 125, 125)

    asyncio.run(runtime.run(AgentTask(prompt="review")))

    return read_events(journal.path)[1]


def test_runtime_contract_accepts_task_and_returns_agent_result_shape():
    class FakeRuntime:
        async def run(self, task: AgentTask) -> AgentResult:
            return AgentResult(
                is_error=False,
                status="done",
                summary=task.prompt,
                session_id="fake-session",
            )

    assert isinstance(FakeRuntime(), AgentRuntime)


def test_task_interface_collects_tool_policy_for_runner_adapters():
    task = AgentTask(
        prompt="review",
        allowed_tools=("Read",),
        mcp_servers=(McpServer("sonar", {"command": "sonar"}, "mcp__sonar__*"),),
        subagents=(SubagentDef("scout", "find code", "search", tools=("Read",)),),
    )

    assert task.all_allowed_tools() == ["Read", "mcp__sonar__*", "Task"]


def test_successful_structured_envelope_contract_is_shared():
    structured = {"status": "pr_opened", "summary": "opened", "needs": ""}

    for result in (
        _claude_result(structured=structured),
        _codex_result(structured=structured),
    ):
        assert result.is_error is False
        assert result.status == "pr_opened"
        assert result.summary == "opened"
        assert result.needs == ""
        assert result.session_id == "s1"
        assert result.structured == structured
        assert classify_outcome(result) is Outcome.OK


def test_blocked_envelope_preserves_needs_for_followup_logic():
    structured = {
        "status": "blocked",
        "summary": "need decision",
        "needs": "which endpoint?",
    }

    for result in (
        _claude_result(structured=structured),
        _codex_result(structured=structured),
    ):
        assert result.is_error is False
        assert result.status == "blocked"
        assert result.needs == "which endpoint?"
        assert classify_outcome(result) is Outcome.BLOCKED


def test_structured_error_status_is_error_for_all_runtimes():
    structured = {"status": "error", "summary": "tool crashed", "needs": ""}

    for result in (
        _claude_result(structured=structured, is_error=False),
        _codex_result(structured=structured, exit_code=0),
    ):
        assert result.is_error is True
        assert result.status == "error"
        assert result.summary == "tool crashed"
        assert classify_outcome(result) is Outcome.ERROR


def test_review_verdict_contract_is_shared_and_drives_dashboard_guardrails():
    """Both runtime envelopes must expose review decisions as the common status.

    The dashboard's review stages and first-pass approval guardrail consume
    terminal journal status, not a runtime-specific structured field.
    """
    claude = _claude_result(
        structured={"decision": "APPROVE", "body": "looks good"},
    )
    codex_changes_requested = _codex_result(
        structured={"decision": "REQUEST_CHANGES", "body": "please fix this"},
    )
    codex_approve = _codex_result(
        structured={"decision": "APPROVE", "body": "looks good"},
    )

    assert claude.status == "APPROVE"
    assert codex_changes_requested.status == "REQUEST_CHANGES"
    assert codex_approve.status == "APPROVE"

    codex_event = {
        "event": "agent_run", "role": "review", "kind": "REVIEW",
        "number": 125, "issue": 125, "status": codex_changes_requested.status,
        "model": "gpt-5.6-sol",
    }
    codex_approve_event = {
        "event": "agent_run", "role": "review", "kind": "REVIEW",
        "number": 127, "issue": 127, "status": codex_approve.status,
        "model": "gpt-5.6-sol",
    }
    claude_event = {
        "event": "agent_run", "role": "review", "kind": "REVIEW",
        "number": 126, "issue": 126, "status": claude.status,
        "model": "claude-opus-5",
    }

    assert stage_for([codex_event]) == "changes requested"
    assert stage_for([codex_approve_event]) == "awaiting merge"
    assert stage_for([claude_event]) == "awaiting merge"
    assert first_pass_verdicts(build_issue_rows([codex_event, claude_event], [])) == (1, 1)


@pytest.mark.parametrize("kind", ["REVIEW", "RE_REVIEW"])
@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES"])
def test_review_journal_envelope_is_runtime_invariant(tmp_path, kind, verdict):
    """The settled journal event, not only AgentResult, is cross-runtime stable."""
    structured = {"decision": verdict, "body": "review body"}
    claude_event = _settled_review_event(
        tmp_path / "claude", _claude_result(structured=structured), kind,
    )
    codex_event = _settled_review_event(
        tmp_path / "codex", _codex_result(structured=structured), kind,
    )

    assert set(codex_event) == set(claude_event)
    for field in set(claude_event) - {"ts", "dispatch", "duration_s", "cost_usd"}:
        assert codex_event[field] == claude_event[field]
    assert codex_event["status"] == verdict


def test_transport_failure_without_envelope_is_error():
    claude = result_to_agent_result(
        SimpleNamespace(
            structured_output=None,
            is_error=True,
            subtype="error_during_execution",
            result="socket closed",
            total_cost_usd=0,
            session_id="s2",
        )
    )
    codex = parse_codex_envelope(
        exit_code=1,
        last_message="",
        session_id="s2",
        stderr="stream disconnected before completion",
    )

    for result in (claude, codex):
        assert result.is_error is True
        assert result.session_id == "s2"
        assert classify_outcome(result) is Outcome.ERROR

    assert claude.status == "error_during_execution"
    assert "stream disconnected" in codex.summary
