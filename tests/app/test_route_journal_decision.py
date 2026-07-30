"""Regression coverage for recording a router's dispatch decision (#102)."""

import asyncio

from issue_agent.adapters.journal import Journal, JournalingRuntime, read_events
from issue_agent.adapters.store import Store
from issue_agent.app.execute import Executor
from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.workflows.fix import FixWorkflow, FixWorkflowConfig
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.effort import Effort
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.world import IssueView

from test_dispatch import FakeGateway, FakeStore, FakeWorktrees, SeqRunner


def test_route_event_records_the_decision_not_the_probe_model(tmp_path):
    """Routing analysis needs the resolve choice without joining later legs."""
    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(SeqRunner([
        AgentResult(is_error=False, session_id="rt", structured={"tier": "complex"}),
        AgentResult(is_error=False, status="pr_opened", session_id="s5", cost_usd=3.0),
    ]), journal, str(tmp_path / "spend.jsonl"), "o/r", "fix")
    runtime.set_context("fix", "RESOLVE", 5, 5)
    gateway = FakeGateway(issues={5: IssueView(5, title="t", body="b")}, pr_for_issue={})
    store = FakeStore()
    worktrees = FakeWorktrees()
    workflow = FixWorkflow(
        FixWorkflowConfig(
            base_branch="dev",
            fix=FixTaskConfig("o/r", agent_md="AM", effort=Effort.HIGH),
            fork_threshold=5, author_tests_split=False, max_contract_rounds=2,
            fix_mcp_config="", harness_root="", log=lambda _message: None,
        ),
        gateway, store, worktrees, runtime,
        Executor(
            gateway,
            remove_worktree=worktrees.remove,
            clear_issue_state=getattr(store, "cleanup_pr", None),
        ),
    )

    asyncio.run(workflow.resolve(5))

    route = next(
        event for event in read_events(journal.path)
        if event["event"] == "agent_run" and event["kind"] == "ROUTE"
    )
    assert route["model"] == "opus"
    assert route["effort"] == "high"
    assert route["probe_model"] == "haiku"


def test_concurrent_route_receipts_keep_task_local_decisions(tmp_path):
    """One routed dispatch must not settle another dispatch's deferred probe."""

    class PairedRuntime:
        def __init__(self):
            self.started = 0
            self.both_started = asyncio.Event()

        async def run(self, _task):
            self.started += 1
            if self.started == 2:
                self.both_started.set()
            await self.both_started.wait()
            await asyncio.sleep(0)
            return AgentResult(is_error=False, status="done")

    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(PairedRuntime(), journal, role="fix")

    async def dispatch(number, model):
        runtime.set_context("fix", "RESOLVE", number, number)
        with runtime.route_receipt() as record_decision:
            with runtime.leg_kind("ROUTE"):
                await runtime.run(RuntimePlan(prompt=f"route {number}", model="haiku"))
            record_decision(model, Effort.HIGH)

    async def run_both():
        await asyncio.gather(
            dispatch(5, "opus"),
            dispatch(7, "sonnet"),
        )

    asyncio.run(run_both())

    runs = [
        event for event in read_events(journal.path)
        if event["event"] == "agent_run"
    ]
    assert {
        (event["number"], event["issue"], event["model"], event["probe_model"])
        for event in runs
    } == {
        (5, 5, "opus", "haiku"),
        (7, 7, "sonnet", "haiku"),
    }
