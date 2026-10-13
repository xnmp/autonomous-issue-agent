"""Review verdict journaling stays identical across runtime adapters."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from issue_agent.adapters.agent_runner import result_to_agent_result
from issue_agent.adapters.codex_runner import parse_codex_envelope
from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.journal import JournalingRuntime, read_events
from issue_agent.adapters.runtime_plan import RuntimePlan


class _StaticRuntime:
    def __init__(self, result):
        self.result = result

    async def run(self, _task):
        return self.result


def _claude_result(structured):
    return result_to_agent_result(SimpleNamespace(
        structured_output=structured, is_error=False, result="", total_cost_usd=0.25,
        session_id="s1",
    ))


def _codex_result(structured):
    return parse_codex_envelope(
        exit_code=0, last_message=json.dumps(structured), session_id="s1",
    )


def _settled_review_event(tmp_path, result, kind: str) -> dict:
    journal = Journal(str(tmp_path / f"{kind}-{result.status}.jsonl"))
    runtime = JournalingRuntime(_StaticRuntime(result), journal, role="review")
    runtime.set_context("review", kind, 125, 125)
    asyncio.run(runtime.run(RuntimePlan(prompt="review")))
    return read_events(journal.path)[1]


@pytest.mark.parametrize("kind", ["REVIEW", "RE_REVIEW"])
@pytest.mark.parametrize("verdict", ["APPROVE", "REQUEST_CHANGES"])
def test_review_journal_envelope_is_runtime_invariant(tmp_path, kind, verdict):
    structured = {"decision": verdict, "body": "review body"}
    claude_event = _settled_review_event(
        tmp_path / "claude", _claude_result(structured), kind,
    )
    codex_event = _settled_review_event(
        tmp_path / "codex", _codex_result(structured), kind,
    )

    assert set(codex_event) == set(claude_event)
    generated_identity = {
        "ts",
        "dispatch",
        "run_id",
        "leg_id",
        "duration_s",
        "cost_usd",
    }
    for field in set(claude_event) - generated_identity:
        assert codex_event[field] == claude_event[field]
    assert codex_event["status"] == verdict
