"""Corrupt workspace evidence cannot deserialize as verified absence/readiness."""

from dataclasses import replace
import json

import pytest

from issue_agent.adapters.workflow_codec import (
    MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow,
)
from issue_agent.domain.resolve_workspace import (
    EpisodeWorkspaceHandoff, EpisodeWorkspacePrepared, NoPriorResolveAdmission,
    PrepareResolveWorkspace, PriorResolveAdmission,
)
from tests.domain.test_resolve_workspace import handoff, prepared


@pytest.mark.parametrize("value", [handoff().base, handoff().predecessor, handoff(absent=True).predecessor,
    handoff(), handoff(absent=True), PrepareResolveWorkspace(handoff()), prepared(), prepared(generation=6)])
def test_workspace_evidence_roundtrip_preserves_exact_typed_obligation(value):
    restored = decode_workflow(encode_workflow(value), type(value))
    assert restored == value
    assert encode_workflow(restored) == encode_workflow(value)


def test_absence_and_admitted_predecessor_remain_distinct_after_restart():
    absent = decode_workflow(encode_workflow(handoff(absent=True)), EpisodeWorkspaceHandoff)
    present = decode_workflow(encode_workflow(handoff()), EpisodeWorkspaceHandoff)
    assert isinstance(absent.predecessor, NoPriorResolveAdmission)
    assert isinstance(present.predecessor, PriorResolveAdmission)
    assert absent.handoff_id != present.handoff_id


@pytest.mark.parametrize("mutation", ["omit", "null", "unknown", "empty_absence", "extra"])
def test_malformed_predecessor_never_defaults_to_absent(mutation):
    raw = json.loads(encode_workflow(handoff()))
    fields = raw["value"]["fields"]
    if mutation == "omit":
        del fields["predecessor"]
    elif mutation == "null":
        fields["predecessor"] = None
    elif mutation == "unknown":
        fields["predecessor"]["type"] = "UnknownResolveAdmission"
    elif mutation == "empty_absence":
        fields["predecessor"] = {"type": "NoPriorResolveAdmission", "fields": {}}
    else:
        fields["predecessor"]["fields"]["verified"] = True
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), EpisodeWorkspaceHandoff)


@pytest.mark.parametrize("mutation", ["sha", "archive", "episode", "generation", "time", "missing_result"])
def test_persisted_ack_rechecks_exact_scope_and_postcondition(mutation):
    raw = json.loads(encode_workflow(prepared()))
    fields = raw["value"]["fields"]
    if mutation == "sha":
        fields["result"]["fields"]["revision"]["fields"]["head_sha"] = "b" * 40
    elif mutation == "archive":
        fields["result"]["fields"]["archive_ref"] = "refs/issue-agent/contract-reset/" + "b" * 64
    elif mutation == "episode":
        fields["performed_under"]["fields"]["episode_id"] = "old"
    elif mutation == "generation":
        fields["performed_under"]["fields"]["fence"]["fields"]["generation"] = True
    elif mutation == "time":
        fields["observed_at"]["value"] = "2026-09-23T00:00:00"
    else:
        del fields["result"]
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), EpisodeWorkspacePrepared)


def test_obligation_cannot_be_decoded_as_preparation_acknowledgement():
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(encode_workflow(PrepareResolveWorkspace(handoff())), EpisodeWorkspacePrepared)


def test_corrupt_predecessor_episode_cannot_alias_same_generation():
    raw = json.loads(encode_workflow(handoff()))
    prior = raw["value"]["fields"]["predecessor"]["fields"]["attempt"]["fields"]["binding"]["fields"]
    prior["episode_id"] = "contradictory"
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), EpisodeWorkspaceHandoff)


def test_replayed_continuation_ack_keeps_original_handoff_identity():
    original = prepared()
    continued = replace(original, performed_under=prepared(generation=6).performed_under)
    restored = decode_workflow(encode_workflow(continued), EpisodeWorkspacePrepared)
    assert restored.handoff.handoff_id == original.handoff.handoff_id
    assert restored.result.archive_ref == original.result.archive_ref


def test_oversized_workspace_payload_fails_instead_of_becoming_empty_state():
    raw = json.loads(encode_workflow(handoff()))
    raw["value"]["fields"]["grant_request_id"] = "x" * MAX_WORKFLOW_PAYLOAD_BYTES
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), EpisodeWorkspaceHandoff)
