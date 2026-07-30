"""Journal and stage regressions for blocked agent outcomes."""

import asyncio

from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.outcome import AgentResult

from tests.app.test_dashboard import action, run
from tests.app.test_journal import _runtime, _settled
from issue_agent.domain import dashboard


def test_blocked_agent_run_maps_to_human_reply_without_hiding_errors():
    assert dashboard.stage_for([run(kind="RESOLVE", status="blocked")]) == (
        "awaiting human reply"
    )
    assert dashboard.stage_for([
        run(kind="RESOLVE", status="blocked", is_error=True)
    ]) == "run failed"
    assert dashboard.stage_for([
        action("relabel issue #7: +ai-blocked")
    ]) == "blocked"


def test_blocked_run_journals_bounded_reason_and_summary(tmp_path):
    result = AgentResult(
        is_error=False,
        status="blocked",
        needs="Which endpoint?\n" + ("x" * 2_000),
        summary="Need an API decision",
    )
    runtime, journal, _ = _runtime(tmp_path, result)

    asyncio.run(runtime.run(RuntimePlan(prompt="p")))

    run_event = _settled(journal.path)[0]
    assert run_event["needs"].startswith("Which endpoint?")
    assert len(run_event["needs"]) == 1_000
    assert run_event["summary"] == "Need an API decision"


def test_nonblocked_run_does_not_grow_the_journal_with_prompt_text(tmp_path):
    result = AgentResult(
        is_error=False,
        status="done",
        needs="ignored",
        summary="ignored",
    )
    runtime, journal, _ = _runtime(tmp_path, result)

    asyncio.run(runtime.run(RuntimePlan(prompt="p")))

    run_event = _settled(journal.path)[0]
    assert "needs" not in run_event and "summary" not in run_event
