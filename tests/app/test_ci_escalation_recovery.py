"""Persisted CI decisions must follow current policy and executed receipts."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from issue_agent.adapters.store import Store
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.app.roles import _fix_state
from issue_agent.domain.actions import EscalateCiPending
from issue_agent.domain.checks import Check, Observed, RequiredCheck
from issue_agent.domain.contracts import EffectStatus
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.domain.world import WorldState
from tests.adapters.test_effects import _executor
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery


POLICY = FixConfig(fix_bot="fix-bot")
NOW = "2026-10-12T02:00:00Z"


def capture_advisory_escalation(tmp_path):
    clock = [datetime(2026, 10, 12, 2, tzinfo=timezone.utc)]
    executor, gateway, control = _executor(tmp_path, ci=True,
        now=lambda: clock[0])
    executor.fix_config = POLICY
    install_workflow(control, with_issue_recovery(resolve_snapshot(7)))
    old = replace(gateway.get_pr(7), author="fix-bot", head_ref_name="fix/7-example",
        closing_issue=7, labels=frozenset({Label.AI_PR_OPEN}),
        ci=Observed("head-1", "head-1", frozenset(),
            (Check("advisory", "PENDING", "pending", "2026-10-12T00:00:00Z", kind="status"),)))
    gateway.get_pr = lambda *args, **kwargs: old
    world = WorldState(prs=(old,), now=NOW)
    action = next(action for action in decide_fix(world, POLICY, FixState(), fixer_busy=False)
        if isinstance(action, EscalateCiPending))
    command = executor._command(action)
    executor.effects.enqueue(command)
    return executor, gateway, control, old, command, clock


@pytest.mark.parametrize("required,next_ci", [
    (False, "queued"), (True, "queued"), (True, "missing"),
    (False, "fresh-executing"), (True, "fresh-executing"),
])
def test_persisted_advisory_escalation_is_obsolete_without_current_stall(tmp_path, required, next_ci):
    executor, gateway, control, old, command, _ = capture_advisory_escalation(tmp_path)
    reports = () if next_ci == "missing" else (Check("unit",
        "QUEUED" if next_ci == "queued" else "IN_PROGRESS", "pending",
        "2026-10-12T01:59:59Z", required=required),)
    if required:
        reports += old.ci.reports
    current = replace(old, ci=Observed("head-1", "head-1",
        frozenset({RequiredCheck("unit")}) if required else frozenset(), reports))
    gateway.get_pr = lambda *args, **kwargs: current
    assert not any(isinstance(action, EscalateCiPending) for action in decide_fix(
        WorldState(prs=(current,), now=NOW), POLICY, FixState(), fixer_busy=False))

    for _ in range(2):
        assert executor.drain_pending_commands() == 0
        assert not gateway.calls
        assert control.effect_receipt(command.command_id).status is EffectStatus.PERMANENT_FAILURE
        assert not control.pending_commands()


def test_obsolete_escalation_receipt_allows_later_required_stall(tmp_path):
    executor, gateway, control, old, command, clock = capture_advisory_escalation(tmp_path)
    green = replace(old, ci=Observed("head-1", "head-1", frozenset({RequiredCheck("unit")}),
        (Check("unit", "SUCCESS", "pass", required=True, kind="status"), *old.ci.reports)))
    gateway.get_pr = lambda *args, **kwargs: green
    assert executor.drain_pending_commands() == 0
    assert not gateway.calls
    assert control.effect_receipt(command.command_id).status is EffectStatus.PERMANENT_FAILURE

    assert isinstance(green.ci, Observed)
    stalled = replace(green, ci=replace(green.ci, reports=(Check("unit", "PENDING", "pending",
        NOW, required=True, kind="status"), *old.ci.reports)))
    gateway.get_pr = lambda *args, **kwargs: stalled
    files = Store(str(tmp_path / "state"))
    object.__setattr__(files, "_workflow_db", control)
    workflows = SQLiteWorkflowControl(control, WorkflowWriter("o/r", "fix", "o/r:fix"), resolve_ceiling=3)
    world = WorldState(prs=(stalled,), now="2026-10-12T04:00:00Z")
    actions = decide_fix(world, POLICY, _fix_state(files, world, workflow_control=workflows), fixer_busy=False)
    assert any(isinstance(action, EscalateCiPending) for action in actions)
    clock[0] = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
    escalation = next(action for action in actions if isinstance(action, EscalateCiPending))
    assert executor.execute(escalation)
    assert gateway.calls == [("comment", "pr", 7, escalation.body),
        ("relabel", "pr", 7, "ai-pr-open", "needs-human-review")]
    assert executor.execute(escalation)
    assert len(gateway.calls) == 2
    assert not any(isinstance(action, EscalateCiPending) for action in decide_fix(
        world, POLICY, _fix_state(files, world, workflow_control=workflows), fixer_busy=False))


@pytest.mark.parametrize("change", ["failed", "undated", "malformed", "disabled", "longer-deadline", "cancellable", "blocked"])
def test_persisted_escalation_reuses_current_domain_remedy(tmp_path, change):
    executor, gateway, control, old, command, _ = capture_advisory_escalation(tmp_path)
    report = Check("unit", "PENDING", "pending", "2026-10-12T00:00:00Z", required=True, kind="status")
    if change == "failed":
        report = replace(report, state="FAILURE", bucket="fail")
    elif change in {"undated", "malformed"}:
        report = replace(report, started_at="" if change == "undated" else "unknown")
    elif change == "cancellable":
        report = replace(report, state="IN_PROGRESS", kind="check", run_id=42)
    policy = replace(POLICY, ci_pending_max_seconds=0 if change == "disabled" else 10800) \
        if change in {"disabled", "longer-deadline"} else POLICY
    executor.fix_config = policy
    current = replace(old, ci=Observed("head-1", "head-1", frozenset({RequiredCheck("unit")}),
        (report, *old.ci.reports)), labels=old.labels | {Label.AI_BLOCKED} if change == "blocked" else old.labels)
    gateway.get_pr = lambda *args, **kwargs: current
    assert not any(isinstance(action, EscalateCiPending) for action in decide_fix(
        WorldState(prs=(current,), now=NOW), policy, FixState(), fixer_busy=False))
    assert executor.drain_pending_commands() == 0
    assert not gateway.calls
    assert control.effect_receipt(command.command_id).status is EffectStatus.PERMANENT_FAILURE
