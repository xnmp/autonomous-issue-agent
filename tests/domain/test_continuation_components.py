"""Secondary authority stays independent across real primary transitions."""

from dataclasses import replace

import pytest

from issue_agent.domain.continuation_components import (
    ActiveIssueRecovery, ActivePRRepair, ActiveSteward, BlockedIssueRecovery, BlockedPRRepair, BlockedSteward,
    ContinuationImportBlocker, ContinuationWatermarks, InactiveIssueRecovery, InactivePRRepair, InactiveSteward,
    IssueRecoveryState, PRRepairState, RepairSession, SelectedIssueRecovery, SelectedPRRepair, SelectedSteward,
    StewardLaneState, StewardState, continuation_return_is_current, select_issue_recovery, select_pr_repair, select_steward,
)
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.review_workflow import ReviewWorkflowSnapshot
from issue_agent.domain.contracts import CommandId, ContinuationAdmission, RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow import (
    IssueWorkflowShell, NoAuthorizedRemediation, OpenGate, RemoteOutcomeUncertain, WorkflowChanged,
    WorkflowMigrationRequired, resolve_from_issue_aggregate,
)
from issue_agent.domain.workflow_identity import (
    ContinuationComponentIdentity, FamilyAuthority, IssueRef, PullRequestRef, WorkflowBinding, WorkflowFamily,
    WorkflowFence, WorkflowKey, WorkflowRevision, target_family,
)
from tests.domain.test_workflow_policy import NOW, exhausted, start
from tests.domain.test_review_workflow_policy import accept, gated


def identity(key, family, episode="secondary", epoch=2):
    return ContinuationComponentIdentity(key, FamilyAuthority(family, epoch), episode)


def hold():
    return OpenGate("secondary:hold", 0, RemoteOutcomeUncertain(CommandId("delivery:1"), NOW, 2, "unavailable"),
                    NoAuthorizedRemediation("settle selected delivery"))


def admission(selection, version, run="run:secondary"):
    return ContinuationAdmission(WorkflowAttemptIdentity(selection.binding, RunId(run), "leg:secondary"), version)


@pytest.mark.parametrize("runs", [None, (), (True,), (0,), (-1,), (1.5,), (2**63,), (2, 1), (1, 1), tuple(range(1, 66))])
def test_ci_capture_refuses_absent_malformed_or_unbounded_run_identity(runs):
    from issue_agent.domain import continuation_components as cc
    key = WorkflowKey("o/r", PullRequestRef(7))
    binding = identity(key, WorkflowFamily.PR_REPAIR).bind(WorkflowFence(key, 0))
    with pytest.raises(ValueError, match="workflow runs"):
        cc.CIRemediationCapture(binding, "head-1", runs)


def test_ci_settlement_requires_same_command_and_verified_terminal_outcome():
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.contracts import EffectReceipt, EffectStatus
    key = WorkflowKey("o/r", PullRequestRef(7))
    binding = identity(key, WorkflowFamily.PR_REPAIR).bind(WorkflowFence(key, 0))
    owed = cc.OwedCIRemediation(CommandId("ci:1"), cc.CIRemediationCapture(binding, "head-1", (42,)))
    receipt = EffectReceipt(CommandId("ci:1"), "o/r:fix", NOW, NOW, EffectStatus.SUCCEEDED)
    assert cc.SettledCIRemediation(owed, receipt).owed == owed
    for invalid in (replace(receipt, command_id=CommandId("ci:other")),
                    replace(receipt, status=EffectStatus.UNKNOWN_OUTCOME, error="unreadable"),
                    replace(receipt, status=EffectStatus.RETRYABLE_FAILURE, error="refused")):
        with pytest.raises(ValueError, match="terminal receipt"):
            cc.SettledCIRemediation(owed, invalid)


def test_fix_infra_ordering_preserves_newer_episode_and_every_sibling_field():
    from issue_agent.domain import continuation_components as cc
    key = WorkflowKey("o/r", IssueRef(5))
    component = ActiveIssueRecovery(identity(key, WorkflowFamily.ISSUE_RECOVERY),
        IssueRecoveryState(resumes=2, remembered_branch="fix/5-x", ci_failures=3, rebase_failures=4, self_clears=1),
        gates=(hold(),))
    selected = SelectedIssueRecovery(WorkflowRevision(WorkflowFence(key, 0), 10), component)
    old = admission(selected, 1, "old")
    normal = admission(selected, 2, "normal-error")
    newer = admission(selected, 3, "newer-death")
    after, counted = cc.apply_fix_infra(selected, old, cc.FixInfraOutcome.RECLAIMED)
    assert after is not None and after.state.infra_failures == 1
    selected = replace(selected, component=after)
    retry, receipt = cc.apply_fix_infra(selected, old, cc.FixInfraOutcome.RETURNED_INFRA, counted)
    assert retry is None and receipt.failures == 1
    reset, _ = cc.apply_fix_infra(selected, normal, cc.FixInfraOutcome.RETURNED_NORMAL)
    assert reset is not None and reset.state.infra_failures == 0
    selected = replace(selected, component=reset)
    late, _ = cc.apply_fix_infra(selected, old, cc.FixInfraOutcome.RETURNED_INFRA)
    assert late is None
    newer_state, _ = cc.apply_fix_infra(selected, newer, cc.FixInfraOutcome.RECLAIMED)
    assert newer_state is not None and newer_state.state.infra_failures == 1
    selected = replace(selected, component=newer_state)
    late_normal, _ = cc.apply_fix_infra(selected, normal, cc.FixInfraOutcome.RETURNED_NORMAL)
    assert late_normal is None
    assert replace(newer_state.state, infra_failures=0, infra_episode=None) == component.state
    assert newer_state.gates == component.gates and newer_state.watermarks == component.watermarks


def test_fix_infra_reclaimed_normal_return_resets_once_and_refuses_conflicting_reply():
    from issue_agent.domain import continuation_components as cc
    key = WorkflowKey("o/r", IssueRef(5))
    selected = SelectedIssueRecovery(WorkflowRevision(WorkflowFence(key, 0), 1),
        ActiveIssueRecovery(identity(key, WorkflowFamily.ISSUE_RECOVERY), IssueRecoveryState()))
    source = admission(selected, 1)
    after, receipt = cc.apply_fix_infra(selected, source, cc.FixInfraOutcome.RECLAIMED)
    assert after is not None
    selected = replace(selected, component=after)
    reset, normal = cc.apply_fix_infra(selected, source, cc.FixInfraOutcome.RETURNED_NORMAL, receipt)
    assert reset is not None and reset.state.infra_failures == 0
    assert cc.apply_fix_infra(replace(selected, component=reset), source, cc.FixInfraOutcome.RECLAIMED, normal) == (None, normal)
    with pytest.raises(ValueError):
        cc.apply_fix_infra(selected, source, cc.FixInfraOutcome.RETURNED_INFRA, normal)


@pytest.mark.parametrize("mode", ["issue", "episode", "epoch", "future_version", "future_generation"])
def test_pending_review_requires_exact_possible_component_provenance(mode):
    from issue_agent.domain.continuation_components import ReviewRetryState
    key = WorkflowKey("o/r", IssueRef(5))
    component = ActiveIssueRecovery(identity(key, WorkflowFamily.ISSUE_RECOVERY), IssueRecoveryState())
    fence = WorkflowFence(key, 0)
    binding = component.identity.bind(fence)
    version = 1
    if mode == "issue":
        binding = replace(binding, fence=WorkflowFence(WorkflowKey("o/r", IssueRef(6)), 0))
    elif mode == "episode":
        binding = replace(binding, episode_id="other-episode")
    elif mode == "epoch":
        binding = replace(binding, authority=replace(binding.authority, epoch=3))
    elif mode == "future_version":
        version = 2
    else:
        binding = replace(binding, fence=WorkflowFence(key, 1))
    pending = ContinuationAdmission(WorkflowAttemptIdentity(binding, RunId("pending-review"), "leg"), version)
    debt = ReviewRetryState("2026-10-02T00:00:01Z", 0, PullRequestRef(100), pending)
    with pytest.raises(ValueError):
        component = replace(component, state=replace(component.state, review_failures=debt),
            watermarks=ContinuationWatermarks(1, 0, fence))
        SelectedIssueRecovery(WorkflowRevision(fence, 1), component)


def test_resolve_generation_advance_preserves_both_secondary_states_and_fences_old_returns():
    state = exhausted()
    key = state.revision.fence.key
    recovery = ActiveIssueRecovery(identity(key, WorkflowFamily.ISSUE_RECOVERY),
                                   IssueRecoveryState(consumed_reply_at=NOW.isoformat(), resumes=2, ci_failures=3),
                                   ContinuationWatermarks(2, 1, state.revision.fence), (hold(),))
    steward = ActiveSteward(identity(key, WorkflowFamily.STEWARD, "steward", 3),
                            StewardState(triage=StewardLaneState(NOW.isoformat(), 4, 2, ("delivery:1",))),
                            ContinuationWatermarks(2, 1, state.revision.fence), (hold(),))
    state = replace(state, issue_recovery=recovery, steward=steward)
    before_recovery, before_steward = select_issue_recovery(state), select_steward(state)
    old_recovery = admission(before_recovery, 2)
    old_steward = admission(before_steward, 2, "run:steward")
    assert continuation_return_is_current(before_recovery, old_recovery)
    assert continuation_return_is_current(before_steward, old_steward)
    transition = start(state)
    assert isinstance(transition, WorkflowChanged)
    after = transition.snapshot
    assert after.revision.fence.generation == state.revision.fence.generation + 1
    assert after.issue_recovery == recovery and after.steward == steward
    assert after.episode.episode_id != state.episode.episode_id
    current_recovery, current_steward = select_issue_recovery(after), select_steward(after)
    assert current_recovery.binding.fence == current_steward.binding.fence == after.revision.fence
    assert current_recovery.binding.authority == recovery.identity.authority
    assert current_recovery.binding.episode_id == recovery.identity.episode_id
    assert not continuation_return_is_current(current_recovery, old_recovery)
    assert not continuation_return_is_current(current_steward, old_steward)


def test_review_generation_advance_preserves_repair_session_allowance_and_its_original_provenance():
    state = gated()
    assert isinstance(state, ReviewWorkflowSnapshot)
    key = state.revision.fence.key
    component_identity = identity(key, WorkflowFamily.PR_REPAIR)
    source = ContinuationAdmission(WorkflowAttemptIdentity(component_identity.bind(state.revision.fence),
                                                          RunId("run:repair"), "CI_REPAIR"), 1)
    session = RepairSession(source, "repair-handle", RuntimeName.CLAUDE, "repair-model")
    repair = ActivePRRepair(component_identity, PRRepairState(NOW.isoformat(), "fix/7-work", session,
                                                            ci_failures=2, rebase_failures=4),
                            ContinuationWatermarks(1, 1), (hold(),))
    state = replace(state, pr_repair=repair)
    transition = accept(state)
    assert isinstance(transition, WorkflowChanged)
    after = transition.snapshot
    selected = select_pr_repair(after)
    assert after.pr_repair == repair and selected.component.state.session == session
    assert selected.binding.fence == after.revision.fence
    assert source.attempt.binding.fence == state.revision.fence
    assert selected.binding.authority == source.attempt.binding.authority
    assert not continuation_return_is_current(selected, source)
    assert after.episode.episode_id == state.episode.episode_id


def test_steward_only_shell_selects_no_resolve_allowance_or_episode():
    key = WorkflowKey("o/r", IssueRef(7))
    revision = WorkflowRevision(WorkflowFence(key, 2), 4)
    steward = ActiveSteward(identity(key, WorkflowFamily.STEWARD), StewardState())
    shell = IssueWorkflowShell(revision, steward=steward)
    resolve = resolve_from_issue_aggregate(shell)
    assert isinstance(resolve, WorkflowMigrationRequired)
    assert resolve.family is WorkflowFamily.RESOLVE
    assert isinstance(select_issue_recovery(shell), InactiveIssueRecovery)
    assert isinstance(select_steward(shell), SelectedSteward)
    assert not hasattr(shell, "episode") and not hasattr(shell, "authority")
    assert target_family(key.target) is WorkflowFamily.RESOLVE


@pytest.mark.parametrize("family,kind", [(WorkflowFamily.ISSUE_RECOVERY, IssueRef),
                                        (WorkflowFamily.STEWARD, IssueRef), (WorkflowFamily.PR_REPAIR, PullRequestRef)])
def test_selected_component_identity_cannot_alias_target_kinds(family, kind):
    key = WorkflowKey("o/r", kind(7))
    value = identity(key, family)
    assert value.bind(WorkflowFence(key, 3)).authority.family is family
    other = WorkflowKey("o/r", (PullRequestRef if kind is IssueRef else IssueRef)(7))
    with pytest.raises(ValueError):
        identity(other, family)
    with pytest.raises(ValueError):
        value.bind(WorkflowFence(other, 3))
    with pytest.raises(ValueError):
        WorkflowBinding(WorkflowFence(other, 3), value.authority, value.episode_id)


@pytest.mark.parametrize("family,blocked,active,state,inactive", [
    (WorkflowFamily.ISSUE_RECOVERY, BlockedIssueRecovery, ActiveIssueRecovery, IssueRecoveryState, InactiveIssueRecovery),
    (WorkflowFamily.PR_REPAIR, BlockedPRRepair, ActivePRRepair, PRRepairState, InactivePRRepair),
    (WorkflowFamily.STEWARD, BlockedSteward, ActiveSteward, StewardState, InactiveSteward),
])
def test_inactive_and_blocked_components_cannot_be_selected_as_active(family, blocked, active, state, inactive):
    key = WorkflowKey("o/r", PullRequestRef(7) if family is WorkflowFamily.PR_REPAIR else IssueRef(7))
    unavailable = blocked(key, FamilyAuthority(family, 2), "decision:blocked",
                          (ContinuationImportBlocker("configured-root:field", "unverified history"),))
    selected = {WorkflowFamily.ISSUE_RECOVERY: SelectedIssueRecovery,
                WorkflowFamily.PR_REPAIR: SelectedPRRepair, WorkflowFamily.STEWARD: SelectedSteward}[family]
    revision = WorkflowRevision(WorkflowFence(key, 0), 2)
    for value in (inactive(), unavailable, None):
        with pytest.raises(ValueError):
            selected(revision, value)
    with pytest.raises(ValueError):
        active(identity(key, family), None)


@pytest.mark.parametrize("value", [None, True, -1, 2**63, "2"])
def test_watermarks_refuse_malformed_and_unbounded_values(value):
    with pytest.raises(ValueError):
        ContinuationWatermarks(value, 0)


def test_newer_admission_and_duplicate_return_cannot_apply_older_continuity():
    state = exhausted()
    component = ActiveIssueRecovery(identity(state.revision.fence.key, WorkflowFamily.ISSUE_RECOVERY),
                                    IssueRecoveryState(resumes=2), ContinuationWatermarks(2, 0, state.revision.fence))
    selected = SelectedIssueRecovery(state.revision, component)
    assert not continuation_return_is_current(selected, admission(selected, 1))
    latest = admission(selected, 2)
    assert continuation_return_is_current(selected, latest)
    applied = SelectedIssueRecovery(state.revision, replace(component, watermarks=ContinuationWatermarks(2, 2)))
    assert not continuation_return_is_current(applied, latest)
    next_revision = replace(state.revision, version=3)
    newer = SelectedIssueRecovery(next_revision, replace(component, watermarks=component.watermarks.admit(3, next_revision.fence)))
    assert not continuation_return_is_current(newer, latest)
    with pytest.raises(ValueError):
        continuation_return_is_current(selected.binding, latest)


@pytest.mark.parametrize("value", [None, "not-a-time", "2026-10-01", "x" * 513])
def test_unknown_reply_and_baseline_values_do_not_construct_empty_authority(value):
    for state, field in ((IssueRecoveryState, "consumed_reply_at"), (PRRepairState, "consumed_reply_at"),
                         (StewardLaneState, "baseline_at")):
        with pytest.raises(ValueError):
            state(**{field: value})


@pytest.mark.parametrize("ceiling", [None, True, -1, 0, 2**63])
def test_consumption_capture_requires_bounded_positive_allowance(ceiling):
    from issue_agent.domain.continuation_components import ContinuationCapture, IssueRecoveryPurchase
    base = exhausted()
    component = ActiveIssueRecovery(identity(base.revision.fence.key, WorkflowFamily.ISSUE_RECOVERY), IssueRecoveryState())
    selected = SelectedIssueRecovery(base.revision, component)
    capture = ContinuationCapture(selected.binding, selected.revision, "leg:resume")
    with pytest.raises(ValueError):
        IssueRecoveryPurchase(capture, NOW.isoformat(), "fix/1", ceiling)


def test_issue_consumption_is_pure_and_preserves_other_allowances():
    from issue_agent.domain.continuation_components import ContinuationCapture, IssueRecoveryPurchase, consume_issue_recovery
    base = exhausted()
    component = ActiveIssueRecovery(identity(base.revision.fence.key, WorkflowFamily.ISSUE_RECOVERY),
                                    IssueRecoveryState(ci_failures=2, infra_failures=3))
    selected = SelectedIssueRecovery(base.revision, component)
    purchase = IssueRecoveryPurchase(ContinuationCapture(selected.binding, selected.revision, "leg:resume"),
                                     NOW.isoformat(), "fix/1", 1)
    result = consume_issue_recovery(selected, purchase, base.revision.version + 1)
    assert component.state.resumes == 0 and component.state.consumed_reply_at == ""
    assert result.state.resumes == 1 and result.state.consumed_reply_at == purchase.reply_at
    assert result.state.ci_failures == 2 and result.state.infra_failures == 3
    assert result.watermarks.last_admitted == base.revision.version + 1
    current = SelectedIssueRecovery(replace(base.revision, version=base.revision.version + 1), result)
    next_purchase = replace(purchase, capture=replace(purchase.capture, revision=current.revision))
    with pytest.raises(ValueError, match="consumed or exhausted"):
        consume_issue_recovery(current, next_purchase, current.revision.version + 1)


def test_capture_cannot_alias_a_primary_family_or_another_generation():
    from issue_agent.domain.continuation_components import ContinuationCapture
    base = exhausted()
    with pytest.raises(ValueError, match="secondary"):
        ContinuationCapture(base.binding, base.revision, "leg:resume")
    selected = SelectedIssueRecovery(base.revision, ActiveIssueRecovery(identity(base.revision.fence.key, WorkflowFamily.ISSUE_RECOVERY), IssueRecoveryState()))
    with pytest.raises(ValueError, match="exact aggregate"):
        ContinuationCapture(selected.binding, replace(base.revision, fence=replace(base.revision.fence, generation=base.revision.fence.generation + 1)), "leg:resume")


def test_pending_watermark_requires_exact_possible_historical_target_fence():
    state = exhausted()
    with pytest.raises(ValueError, match="captured admission fence"):
        ContinuationWatermarks(1, 0)
    future = replace(state.revision.fence, generation=state.revision.fence.generation + 1)
    component = ActiveIssueRecovery(identity(state.revision.fence.key, WorkflowFamily.ISSUE_RECOVERY),
                                    IssueRecoveryState(), ContinuationWatermarks(1, 0, future))
    with pytest.raises(ValueError, match="future generation"):
        SelectedIssueRecovery(state.revision, component)
    other = replace(state.revision.fence, key=replace(state.revision.fence.key, target=IssueRef(999)))
    component = replace(component, watermarks=ContinuationWatermarks(1, 0, other))
    with pytest.raises(ValueError, match="another target"):
        SelectedIssueRecovery(state.revision, component)


@pytest.mark.parametrize("reply_at,body,branch", [
    (None, "answer", "fix/1-reply"), ("invalid", "answer", "fix/1-reply"),
    ("2026-10-01T00:00:00Z", None, "fix/1-reply"),
    ("2026-10-01T00:00:00Z", " ", "fix/1-reply"),
    ("2026-10-01T00:00:00Z", "x" * 65537, "fix/1-reply"),
    ("2026-10-01T00:00:00Z", "answer", "fix/2-other"),
])
def test_selected_issue_reply_refuses_unknown_or_unowned_inputs(reply_at, body, branch):
    from issue_agent.domain.continuation_components import IssueReplyInput
    from tests.fakes.workflow_authority import resolve_snapshot, with_issue_recovery
    selected = select_issue_recovery(with_issue_recovery(resolve_snapshot()))
    with pytest.raises(ValueError):
        IssueReplyInput(selected, reply_at, body, branch)


@pytest.mark.parametrize("branch,runtime", [(None, RuntimeName.CLAUDE), ("", RuntimeName.CLAUDE),
    ("x" * 65537, RuntimeName.CLAUDE), ("fix/7-ci", None), ("fix/7-ci", "claude")])
def test_repair_launch_refuses_missing_huge_or_untyped_inputs(branch, runtime):
    from issue_agent.domain import continuation_components as cc
    base = gated()
    assert isinstance(base, ReviewWorkflowSnapshot)
    component = cc.ActivePRRepair(identity(base.revision.fence.key, WorkflowFamily.PR_REPAIR), cc.PRRepairState())
    selected = cc.select_pr_repair(replace(base, pr_repair=component))
    with pytest.raises(ValueError):
        cc.PRRepairInput(selected, branch, runtime)


def test_repair_retirement_preserves_requeue_debt_but_retires_merged_state():
    from issue_agent.domain import continuation_components as cc
    state = cc.PRRepairState(branch="fix/7-ci", resumes=4, ci_failures=3, infra_failures=2)
    requeued = cc.retire_pr_repair_continuity(state, False)
    assert (requeued.resumes, requeued.ci_failures, requeued.infra_failures, requeued.branch) == (4, 3, 2, "fix/7-ci")
    merged = cc.retire_pr_repair_continuity(state, True)
    assert (merged.resumes, merged.ci_failures, merged.infra_failures, merged.branch) == (0, 0, 0, "")


def test_blank_reply_source_cannot_originate_in_future_current_generation():
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowBinding
    base = gated()
    assert isinstance(base, ReviewWorkflowSnapshot)
    component = identity(base.revision.fence.key, WorkflowFamily.PR_REPAIR)
    binding = WorkflowBinding(replace(base.revision.fence, generation=base.revision.fence.generation+1),
                              component.authority, component.episode_id)
    state = cc.PRRepairState(consumed_reply_at=NOW.isoformat(), blank_reply=cc.PRBlankReplySource(binding, NOW.isoformat()))
    with pytest.raises(ValueError, match="future"):
        replace(base, pr_repair=cc.ActivePRRepair(component, state))


@pytest.mark.parametrize("timestamp,body", [(None, "x"), ("", "x"), ("yesterday", "x"),
    ("2026-10-01", "x"), (NOW.isoformat(), " "), (NOW.isoformat(), None), (NOW.isoformat(), "x" * 65537)])
def test_paid_pr_reply_requires_bounded_nonblank_body_and_aware_timestamp(timestamp, body):
    from issue_agent.domain import continuation_components as cc
    with pytest.raises(ValueError):
        cc.PRReplyInput(timestamp, body)
