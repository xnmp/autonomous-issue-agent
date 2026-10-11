"""The completed event says which authority a refused leg lacked (#262)."""

import asyncio
from issue_agent.domain.events import AgentRunCompleted, decode_event_line

from issue_agent.adapters.event_log import EventLog
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.typed_journal import TypedJournal
from issue_agent.domain.events import Producer
from issue_agent.domain.outcome import AgentResult, ExitReason


class Refusing:
    def __init__(self, result):
        self.result = result

    async def run(self, _task):
        return self.result


def _completed_payloads(tmp_path, result):
    from tests.app.test_dispatch import FakeRunner  # noqa: F401  (task shape reuse)
    from issue_agent.adapters.runtime_plan import RuntimePlan

    journal = TypedJournal(
        str(tmp_path / "journal.jsonl"),
        EventLog(str(tmp_path / "events-v2.jsonl"), Producer("deployment", "o/r", "fix", 100, "boot")),
    )
    runtime = JournalingRuntime(Refusing(result), journal, repo="o/r", role="fix")
    runtime.set_context("fix", "ADDRESS_REVIEW", 100, 5)
    asyncio.run(runtime.run(RuntimePlan(prompt="p", cwd=str(tmp_path), model="m")))
    lines = (tmp_path / "events-v2.jsonl").read_text().splitlines()
    envelopes = [decode_event_line(line) for line in lines]
    return [
        e.payload for e in envelopes
        if isinstance(e.payload, AgentRunCompleted)
    ]


def test_refused_leg_completed_event_carries_the_missing_authority(tmp_path):
    refused = AgentResult(
        is_error=True, status="return_authority_unavailable",
        envelope_subtype="return_authority_unavailable",
        summary="paid admission refused: PR repair purchase is held, consumed or exhausted",
        exit_reason=ExitReason.RUNTIME_UNAVAILABLE,
    )

    (payload,) = _completed_payloads(tmp_path, refused)

    assert "PR repair purchase is held" in payload.summary


def test_ordinary_failure_summary_is_still_not_copied_into_the_event(tmp_path):
    failed = AgentResult(is_error=True, status="error", summary="agent prose, untrusted",
                         exit_reason=ExitReason.AGENT_ERROR)

    (payload,) = _completed_payloads(tmp_path, failed)

    assert payload.summary is None
