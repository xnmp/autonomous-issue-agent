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


@pytest.mark.parametrize("kind,stalls,infra", [(cc.StewardFailureKind.PAID, 2, 4), (cc.StewardFailureKind.INFRASTRUCTURE, 1, 5)])
def test_classified_failure_charges_only_a_paid_attempt(kind, stalls, infra):
    selection, source = admitted(cc.StewardLaneState(stalls=1, infra_failures=4))
    state = cc.steward_return_state(selection, cc.StewardFailed(source, kind))
    assert state is not None
    assert state.triage.stalls == stalls
    assert state.triage.infra_failures == infra and state.triage.baseline_at == ""
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


def groom_attempt(*, lane=cc.StewardLaneState(), content=cc.GroomContent(), signature="a" * 64):
    before = selected()
    assert isinstance(before, cc.SelectedSteward)
    before = replace(before, component=replace(before.component,
        state=replace(before.component.state, groom=lane, groom_content=content)))
    purchase = cc.StewardPurchase(cc.ContinuationCapture(before.binding, before.revision, "groom:first"),
                                  cc.StewardLane.GROOM, AT, 3, signature)
    version = before.revision.version + 1
    component = cc.consume_steward(before, purchase, version)
    source = cc.StewardAttemptSource(ContinuationAdmission(
        WorkflowAttemptIdentity(before.binding, RunId("run:groom"), purchase.capture.leg_id), version),
        cc.StewardLane.GROOM, AT, signature)
    return cc.SelectedSteward(replace(before.revision, version=version), component), source


def test_successful_no_finding_groom_consumes_content_without_effects():
    selection, source = groom_attempt()
    state = cc.steward_return_state(selection, cc.GroomNoFinding(source))
    assert state is not None
    assert state.groom.baseline_at == AT and not state.groom.pending_delivery
    assert state.groom_content.successful == source.content_signature
    assert state.triage == selection.component.state.triage
    completed = replace(selection, component=replace(selection.component, state=state,
        watermarks=replace(selection.component.watermarks, last_applied=source.admission.admission_version)))
    assert not cc.groom_admissible(completed, "2026-10-02T00:00:00Z", source.content_signature, 3)
    assert cc.steward_return_state(completed, cc.GroomNoFinding(source)) is None


def test_groom_stall_episode_changes_only_with_monotonic_new_content():
    lane = cc.StewardLaneState(stalls=3, attempted_at="2026-09-30T00:00:00Z")
    content = cc.GroomContent(attempted="a" * 64)
    with pytest.raises(ValueError):
        groom_attempt(lane=lane, content=content)
    new, _ = groom_attempt(lane=lane, content=content, signature="b" * 64)
    assert new.component.state.groom.stalls == 0
    assert new.component.state.groom_content.attempted == "b" * 64
    assert new.component.state.groom_content.successful == ""
    with pytest.raises(ValueError):
        groom_attempt(lane=replace(lane, attempted_at=AT), content=content, signature="b" * 64)


@pytest.mark.parametrize("first", [cc.StewardLane.TRIAGE, cc.StewardLane.GROOM])
def test_owed_delivery_fences_sibling_admission_without_resetting_sibling(first):
    selection, source = admitted() if first is cc.StewardLane.TRIAGE else groom_attempt()
    state = cc.steward_return_state(selection, cc.StewardVerdict(source, ("selected:command",)))
    assert state is not None
    owed = replace(selection, component=replace(selection.component, state=state,
        watermarks=replace(selection.component.watermarks, last_applied=source.admission.admission_version)))
    sibling = cc.StewardLane.GROOM if first is cc.StewardLane.TRIAGE else cc.StewardLane.TRIAGE
    purchase = cc.StewardPurchase(cc.ContinuationCapture(owed.binding, owed.revision, "sibling"), sibling,
                                  "2026-10-02T00:00:00Z", 3, "b" * 64 if sibling is cc.StewardLane.GROOM else "")
    with pytest.raises(ValueError):
        cc.consume_steward(owed, purchase, owed.revision.version + 1)
    delivered = cc.steward_delivered_state(owed, source, AT)
    assert delivered is not None
    assert getattr(delivered, sibling.value) == getattr(owed.component.state, sibling.value)
    if sibling is cc.StewardLane.GROOM:
        assert delivered.groom_content == owed.component.state.groom_content
    ready = replace(owed, component=replace(owed.component, state=delivered))
    cc.consume_steward(ready, purchase, ready.revision.version + 1)


@pytest.mark.parametrize("signature", [None, "", "a" * 63, "a" * 65, "g" * 64, "A" * 64, "a" * 65537])
def test_absent_or_malformed_groom_input_refuses(signature):
    with pytest.raises(ValueError):
        groom_attempt(signature=signature)


def test_late_groom_no_finding_cannot_erase_new_signature_or_stalls():
    before, source = groom_attempt()
    newer = replace(before, revision=replace(before.revision, fence=replace(before.revision.fence, generation=3)),
        component=replace(before.component, state=replace(before.component.state,
            groom=cc.StewardLaneState(stalls=3, attempted_at="2026-10-02T00:00:00Z"),
            groom_content=cc.GroomContent("b" * 64, "c" * 64))))
    assert cc.steward_return_state(newer, cc.GroomNoFinding(source)) is None
    assert newer.component.state.groom_content == cc.GroomContent("b" * 64, "c" * 64)
    assert newer.component.state.groom.stalls == 3
