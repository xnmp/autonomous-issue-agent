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


# ── the incident shape: ADDRESS_REVIEW on a PR whose repair purchase is refused ──
def _pr_repair_refusing_review(monkeypatch):
    """A PR repair whose previous admitted run left its repair hold in place, so
    reserve_pr_repair refuses at the reservation branch (accounting_runtime
    `_return_refused(reservation.detail)`), exactly as live PR #1019."""
    from dataclasses import replace
    from issue_agent.domain import continuation_components as cc
    from tests.app.test_pr_repair_workflow import review_workflow
    from tests.domain.builders import labels

    workflow, files, inner, key = review_workflow(ran_and_failed())

    def held_purchase(*_args, **_kwargs):
        raise ValueError("PR repair purchase is held, consumed or exhausted")

    monkeypatch.setattr(cc, "consume_pr_repair", held_purchase)
    workflow.gateway.prs[100] = replace(
        workflow.gateway.prs[100], author="fix-bot", state="OPEN", labels=labels("ai-pr-open"),
    )
    return workflow, files, inner


def _decide_pr(files, workflow):
    view = world(
        issues=[build_issue(5, "ai-pr-open")], prs=[workflow.gateway.prs[100]],
    )
    state = _fix_state(files, view, workflow_control=workflow.workflow_control,
                       available_runtimes=frozenset({"claude"}))
    return decide_fix(view, CFG, state, fixer_busy=False)


def test_repeated_pr_repair_refusals_stop_address_review_at_the_ceiling_with_one_escalation(monkeypatch):
    workflow, files, inner = _pr_repair_refusing_review(monkeypatch)
    dispatched = 0
    escalated = 0
    for _ in range(CFG.max_infra_deaths + 3):
        actions = _decide_pr(files, workflow)
        if any(isinstance(a, RunAgent) and a.kind is AgentKind.ADDRESS_REVIEW for a in actions):
            dispatched += 1
            asyncio.run(workflow.address_review(100))
        else:
            escalated += sum(
                isinstance(a, Relabel) and a.add is Label.NEEDS_HUMAN_REVIEW for a in actions
            )
            break

    assert inner.tasks == []
    assert dispatched == CFG.max_infra_deaths
    assert escalated == 1


def test_an_unwritable_refusal_ledger_fails_the_issue_closed_instead_of_proceeding_blind(monkeypatch):
    store = FakeStore()
    gateway = FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={})
    runner = FakeRunner(ran_and_failed())
    refusing = FixTaskConfig("o/r", agent_md="AM", model="m", runtime=RuntimeName.CODEX)
    wf = workflow(gateway, store, runner, fix=refusing)

    def unwritable(*_args, **_kwargs):
        raise OSError("disk I/O error")

    monkeypatch.setattr(store._workflow_db, "replace_level_state", unwritable)
    asyncio.run(wf.resolve(ISSUE))
    assert store._workflow_db.fix_refusal_unrecorded("o/r", ISSUE)

    # The provider is now fine, so absent the guard this leg would be admitted.
    healthy = workflow(gateway, store, runner)
    asyncio.run(healthy.resolve(ISSUE))
    assert runner.tasks == []

    monkeypatch.undo()
    asyncio.run(healthy.resolve(ISSUE))  # still closed: this attempt re-records the refusal
    assert not store._workflow_db.fix_refusal_unrecorded("o/r", ISSUE)
    asyncio.run(healthy.resolve(ISSUE))  # ledger healthy again: admitted
    assert len(runner.tasks) == 1


def test_refusal_detail_reaches_the_done_log_line():

    store = FakeStore()
    lines = []
    gateway = FakeGateway(issues={ISSUE: IssueView(ISSUE, title="t", body="b")}, pr_for_issue={})
    wf = workflow(
        gateway, store, FakeRunner(ran_and_failed()), log=lines.append,
        fix=FixTaskConfig("o/r", agent_md="AM", model="m", runtime=RuntimeName.CODEX),
    )
    asyncio.run(wf.resolve(ISSUE))

    assert any(
        "done" in line and "selected provider differs from accounting runtime" in line for line in lines
    ), lines
