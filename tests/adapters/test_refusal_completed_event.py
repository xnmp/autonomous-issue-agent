"""The completed event says which authority a refused leg lacked (#262)."""

import asyncio
import json

from issue_agent.adapters.journal import Journal, JournalingRuntime
from issue_agent.domain.outcome import AgentResult, ExitReason


class Refusing:
    def __init__(self, result):
        self.result = result

    async def run(self, _task):
        return self.result


def _completed_payloads(tmp_path, result):
    from issue_agent.adapters.runtime_plan import RuntimePlan

    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(Refusing(result), journal, repo="o/r", role="fix")
    runtime.set_context("fix", "ADDRESS_REVIEW", 100, 5)
    asyncio.run(runtime.run(RuntimePlan(prompt="p", cwd=str(tmp_path), model="m")))
    records = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    return [r for r in records if r["event"] == "agent_run"]


def test_refused_leg_completed_event_carries_the_missing_authority(tmp_path):
    refused = AgentResult(
        is_error=True,
        status="return_authority_unavailable",
        envelope_subtype="return_authority_unavailable",
        summary="paid admission refused: PR repair purchase is held, consumed or exhausted",
        exit_reason=ExitReason.RUNTIME_UNAVAILABLE,
    )

    (payload,) = _completed_payloads(tmp_path, refused)

    assert "PR repair purchase is held" in payload["summary"]
