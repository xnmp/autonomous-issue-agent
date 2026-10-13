"""Secondary authority stays independent across real primary transitions."""

from dataclasses import replace

import pytest

from issue_agent.domain.continuation_components import ActiveIssueRecovery, ActivePRRepair, ContinuationWatermarks, InactiveIssueRecovery, InactivePRRepair, IssueRecoveryState, PRRepairState, SelectedIssueRecovery, SelectedPRRepair, continuation_return_is_current, select_issue_recovery
from issue_agent.domain.contracts import CommandId, ContinuationAdmission, RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow import (
    NoAuthorizedRemediation, OpenGate, RemoteOutcomeUncertain, WorkflowChanged,
)
from issue_agent.domain.workflow_identity import (
    ContinuationComponentIdentity, FamilyAuthority, IssueRef, PullRequestRef, WorkflowBinding, WorkflowFamily,
    WorkflowFence, WorkflowKey, WorkflowRevision,
)
from tests.domain.test_workflow_policy import NOW, exhausted, start


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


def test_resolve_generation_advance_preserves_recovery_state_and_fences_old_returns():
    state = exhausted()
    key = state.revision.fence.key
    recovery = ActiveIssueRecovery(identity(key, WorkflowFamily.ISSUE_RECOVERY),
                                   IssueRecoveryState(consumed_reply_at=NOW.isoformat(), resumes=2, ci_failures=3),
                                   ContinuationWatermarks(2, 1, state.revision.fence), (hold(),))
    state = replace(state, issue_recovery=recovery)
    before_recovery = select_issue_recovery(state)
    old_recovery = admission(before_recovery, 2)
    assert continuation_return_is_current(before_recovery, old_recovery)
    transition = start(state)
    assert isinstance(transition, WorkflowChanged)
    after = transition.snapshot
    assert after.revision.fence.generation == state.revision.fence.generation + 1
    assert after.issue_recovery == recovery
    assert after.episode.episode_id != state.episode.episode_id
    current_recovery = select_issue_recovery(after)
    assert current_recovery.binding.fence == after.revision.fence
    assert current_recovery.binding.authority == recovery.identity.authority
    assert current_recovery.binding.episode_id == recovery.identity.episode_id
    assert not continuation_return_is_current(current_recovery, old_recovery)


@pytest.mark.parametrize("family,kind", [(WorkflowFamily.ISSUE_RECOVERY, IssueRef),
                                        (WorkflowFamily.PR_REPAIR, PullRequestRef)])
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


@pytest.mark.parametrize("family,active,state,inactive", [
    (WorkflowFamily.ISSUE_RECOVERY, ActiveIssueRecovery, IssueRecoveryState, InactiveIssueRecovery),
    (WorkflowFamily.PR_REPAIR, ActivePRRepair, PRRepairState, InactivePRRepair),
])
def test_inactive_components_cannot_be_selected_as_active(family, active, state, inactive):
    key = WorkflowKey("o/r", PullRequestRef(7) if family is WorkflowFamily.PR_REPAIR else IssueRef(7))
    selected = {WorkflowFamily.ISSUE_RECOVERY: SelectedIssueRecovery,
                WorkflowFamily.PR_REPAIR: SelectedPRRepair}[family]
    revision = WorkflowRevision(WorkflowFence(key, 0), 2)
    for value in (inactive(), None):
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
    for state, field in ((IssueRecoveryState, "consumed_reply_at"), (PRRepairState, "consumed_reply_at")):
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
                                    IssueRecoveryState(ci_failures=2, rebase_failures=3))
    selected = SelectedIssueRecovery(base.revision, component)
    purchase = IssueRecoveryPurchase(ContinuationCapture(selected.binding, selected.revision, "leg:resume"),
                                     NOW.isoformat(), "fix/1", 1)
    result = consume_issue_recovery(selected, purchase, base.revision.version + 1)
    assert component.state.resumes == 0 and component.state.consumed_reply_at == ""
    assert result.state.resumes == 1 and result.state.consumed_reply_at == purchase.reply_at
    assert result.state.ci_failures == 2 and result.state.rebase_failures == 3
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


