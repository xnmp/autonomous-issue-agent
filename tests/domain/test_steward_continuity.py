"""Steward progress requires delivered facts from the selected paid attempt."""

from dataclasses import replace

import pytest

from issue_agent.domain import continuation_components as cc
from issue_agent.domain.contracts import ContinuationAdmission, RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow import IssueWorkflowShell
from issue_agent.domain.workflow_identity import ContinuationComponentIdentity, FamilyAuthority, WorkflowFamily
from tests.fakes.workflow_authority import resolve_snapshot


AT = "2026-10-01T00:00:00Z"


def selected(lane=cc.StewardLaneState()):
    revision = resolve_snapshot(7).revision
    identity = ContinuationComponentIdentity(revision.fence.key, FamilyAuthority(WorkflowFamily.STEWARD, 1), "triage:episode")
    shell = IssueWorkflowShell(revision, steward=cc.ActiveSteward(identity, cc.StewardState(triage=lane)))
    return cc.select_steward(shell)


@pytest.mark.parametrize("input_at", [AT, "2026-09-30T00:00:00Z"])
def test_delivered_triage_input_cannot_purchase_same_or_older_work(input_at):
    selection = selected(cc.StewardLaneState(baseline_at=AT))
    purchase = cc.StewardPurchase(cc.ContinuationCapture(selection.binding, selection.revision, "triage:first"),
                                  cc.StewardLane.TRIAGE, input_at, 3)
    with pytest.raises(ValueError):
        cc.consume_steward(selection, purchase, selection.revision.version + 1)


def admitted(lane=cc.StewardLaneState(), input_at=AT):
    before = selected(lane)
    purchase = cc.StewardPurchase(cc.ContinuationCapture(before.binding, before.revision, "triage:first"),
                                  cc.StewardLane.TRIAGE, input_at, 3)
    version = before.revision.version + 1
    component = cc.consume_steward(before, purchase, version)
    source = cc.StewardAttemptSource(ContinuationAdmission(
        WorkflowAttemptIdentity(before.binding, RunId("run:triage"), purchase.capture.leg_id), version),
        cc.StewardLane.TRIAGE, input_at)
    return cc.SelectedSteward(replace(before.revision, version=version), component), source


def test_queued_verdict_keeps_baseline_and_stalls_until_delivery():
    selection, source = admitted(cc.StewardLaneState(stalls=2))
    state = cc.steward_return_state(selection, cc.StewardVerdict(source, ("command:comment", "command:label")))
    assert state is not None
    assert state.triage.baseline_at == "" and state.triage.stalls == 2
    component = replace(selection.component, state=state,
                        watermarks=replace(selection.component.watermarks, last_applied=source.admission.admission_version))
    owed = cc.SelectedSteward(selection.revision, component)
    purchase = cc.StewardPurchase(cc.ContinuationCapture(owed.binding, owed.revision, "triage:again"),
                                  cc.StewardLane.TRIAGE, "2026-10-02T00:00:00Z", 3)
    with pytest.raises(ValueError):
        cc.consume_steward(owed, purchase, owed.revision.version + 1)
    delivered = cc.steward_delivered_state(owed, source, "2026-10-01T00:01:00Z")
    assert delivered is not None
    assert delivered.triage.baseline_at == "2026-10-01T00:01:00Z"
    assert delivered.triage.stalls == 0 and delivered.triage.pending_delivery == ()
    assert delivered.groom == state.groom


@pytest.mark.parametrize("kind,stalls", [(cc.StewardFailureKind.PAID, 2), (cc.StewardFailureKind.INFRASTRUCTURE, 1)])
def test_classified_failure_charges_only_a_paid_attempt(kind, stalls):
    selection, source = admitted(cc.StewardLaneState(stalls=1, infra_failures=4))
    state = cc.steward_return_state(selection, cc.StewardFailed(source, kind))
    assert state is not None
    assert state.triage.stalls == stalls
    assert state.triage.infra_failures == 4 and state.triage.baseline_at == ""
    applied = cc.SelectedSteward(selection.revision, replace(selection.component, state=state,
        watermarks=replace(selection.component.watermarks, last_applied=source.admission.admission_version)))
    assert cc.steward_return_state(applied, cc.StewardFailed(source, kind)) is None


def test_late_result_and_delivery_cannot_overwrite_newer_generation():
    selection, source = admitted()
    late = cc.StewardVerdict(source, ("command:comment",))
    newer = cc.SelectedSteward(replace(selection.revision, fence=replace(selection.revision.fence, generation=3)),
                              selection.component)
    assert cc.steward_return_state(newer, late) is None
    pending = replace(selection.component.state.triage, pending_delivery=late.commands, delivery_source=source)
    component = replace(selection.component, state=replace(selection.component.state, triage=pending),
                        watermarks=replace(selection.component.watermarks, last_applied=source.admission.admission_version))
    newer = cc.SelectedSteward(newer.revision, component)
    assert cc.steward_delivered_state(newer, source, "2026-10-02T00:00:00Z") is None


def test_new_input_starts_a_new_stall_episode_without_reviving_old_input():
    exhausted = cc.StewardLaneState(stalls=3, attempted_at=AT)
    before = selected(exhausted)
    purchase = cc.StewardPurchase(cc.ContinuationCapture(before.binding, before.revision, "triage:again"),
                                  cc.StewardLane.TRIAGE, AT, 3)
    with pytest.raises(ValueError):
        cc.consume_steward(before, purchase, before.revision.version + 1)
    after = cc.consume_steward(before, replace(purchase, baseline_at="2026-10-02T00:00:00Z"), before.revision.version + 1)
    assert after.state.triage.stalls == 0 and after.state.triage.baseline_at == ""


@pytest.mark.parametrize("commands", [(), ("same", "same"), tuple("c:"+str(i) for i in range(65)), (None,)])
def test_unknown_or_unbounded_verdict_commands_refuse(commands):
    _, source = admitted()
    with pytest.raises(ValueError):
        cc.StewardVerdict(source, commands)
