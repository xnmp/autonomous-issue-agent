"""A pre-admission `return_authority_unavailable` refusal is bounded (#262).

2026-10-11 (tauri-explorer PR #1019): the fix role re-dispatched ADDRESS_REVIEW
every ~90 s for a refusal minted BEFORE any run existed. The infra ceiling is
charged from the admitted run's receipt, so a refusal never advanced it and
`infra_deaths` stayed at 2 across every retry.
"""

import asyncio

from issue_agent.app.planners.tasks import FixTaskConfig
from issue_agent.app.roles import _fix_state
from issue_agent.domain.actions import AgentKind, Relabel, RunAgent
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.fix import decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.world import IssueView

from tests.domain.builders import issue as build_issue, world
from tests.app.test_infra_class_counters import (
    CFG, ISSUE, FakeGateway, FakeRunner, FakeStore, ran_and_failed, workflow,
)


def refusing_resolve(store, times):
    """Each call is refused before admission: the selected provider is not the
    accounting runtime's, a harness precondition (never the agent's work)."""
    gateway = FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={})
    runner = FakeRunner(ran_and_failed())
    for _ in range(times):
        wf = workflow(
            gateway, store, runner,
            fix=FixTaskConfig("o/r", agent_md="AM", model="m", runtime=RuntimeName.CODEX),
        )
        asyncio.run(wf.resolve(ISSUE))
    return wf, runner


def decide_after(store, wf):
    view = world(issues=[build_issue(ISSUE, "ai-ready")])
    state = _fix_state(store, view, workflow_control=wf.workflow_control,
                       available_runtimes=frozenset({"claude"}))
    return decide_fix(view, CFG, state, fixer_busy=False)


def test_repeated_refusals_stop_at_the_infra_ceiling_with_one_escalation():
    store = FakeStore()
    wf, runner = refusing_resolve(store, CFG.max_infra_deaths - 1)
    assert [a for a in decide_after(store, wf) if isinstance(a, RunAgent)], "below the ceiling it still retries"

    wf, runner = refusing_resolve(store, 1)
    actions = decide_after(store, wf)

    assert runner.tasks == []
    assert not [a for a in actions if isinstance(a, RunAgent) and a.kind is AgentKind.RESOLVE]
    escalations = [a for a in actions if isinstance(a, Relabel) and a.add is Label.NEEDS_HUMAN_REVIEW]
    assert len(escalations) == 1


def test_each_refusal_is_journalled_with_the_authority_it_lacked_and_its_ceiling_position():
    from tests.app.test_infra_class_counters import RecordingJournal
    from issue_agent.domain.events import InfraPathEntered

    store = FakeStore()
    journal = RecordingJournal()
    gateway = FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={})
    for _ in range(3):
        wf = workflow(
            gateway, store, FakeRunner(ran_and_failed()), journal=journal,
            fix=FixTaskConfig("o/r", agent_md="AM", model="m", runtime=RuntimeName.CODEX),
        )
        asyncio.run(wf.resolve(ISSUE))

    paths = [e.payload for e in journal.events if isinstance(e.payload, InfraPathEntered)]
    assert [p.infra_deaths for p in paths] == [1, 2, 3]
    assert all("selected provider differs from accounting runtime" in p.exit_reason for p in paths)


def test_a_later_admitted_run_resets_the_refusal_count():
    store = FakeStore()
    wf, _ = refusing_resolve(store, CFG.max_infra_deaths - 1)
    assert wf.workflow_control.fix_admission_refusals(ISSUE) == CFG.max_infra_deaths - 1

    healthy = workflow(
        FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={}),
        store, FakeRunner(ran_and_failed()),
    )
    asyncio.run(healthy.resolve(ISSUE))

    assert healthy.workflow_control.fix_admission_refusals(ISSUE) == 0
    assert [a for a in decide_after(store, healthy) if isinstance(a, RunAgent)]
