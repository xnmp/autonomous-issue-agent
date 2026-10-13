"""Shared contract tests for headless agent runtime adapters.

The concrete Claude SDK and Codex CLI adapters have different transport details,
but workflow code depends on the same normalized `AgentResult` semantics. These
tests intentionally exercise pure adapter mapping functions so they stay offline
and do not pin subprocess or SDK internals.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from issue_agent.adapters.agent_runner import result_to_agent_result
from issue_agent.adapters.codex_runner import parse_codex_envelope
from issue_agent.domain.outcome import Outcome, classify_outcome


def _claude_result(*, structured, is_error=False, result="", session_id="s1"):
    return result_to_agent_result(
        SimpleNamespace(
            structured_output=structured, is_error=is_error, result=result, total_cost_usd=0.25, session_id=session_id
        )
    )


def _codex_result(*, structured, exit_code=0, session_id="s1", stderr=""):
    return parse_codex_envelope(
        exit_code=exit_code, last_message=json.dumps(structured), session_id=session_id, stderr=stderr
    )


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
        exit_code=1, last_message="", session_id="s2", stderr="stream disconnected before completion"
    )

    for result in (claude, codex):
        assert result.is_error is True
        assert result.session_id == "s2"
        assert classify_outcome(result) is Outcome.ERROR

    assert claude.status == "error_during_execution"
    assert "stream disconnected" in codex.summary
