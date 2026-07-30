"""Review verdict journaling stays identical across runtime adapters."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from issue_agent.adapters.agent_runner import result_to_agent_result
from issue_agent.adapters.codex_runner import parse_codex_envelope
from issue_agent.adapters.journal import Journal, JournalingRuntime, read_events
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.dashboard import build_issue_rows, first_pass_verdicts, stage_for


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


def test_codex_review_status_drives_dashboard_stages_and_first_pass_metric(tmp_path):
    changes_requested = _settled_review_event(
        tmp_path / "changes", _codex_result({"decision": "REQUEST_CHANGES"}), "REVIEW",
    )
    approved = _settled_review_event(
        tmp_path / "approved", _codex_result({"decision": "APPROVE"}), "REVIEW",
    )
    approved["number"] = approved["issue"] = 126

    assert stage_for([changes_requested]) == "changes requested"
    assert stage_for([approved]) == "awaiting merge"
    assert first_pass_verdicts(build_issue_rows([changes_requested, approved], [])) == (1, 1)
