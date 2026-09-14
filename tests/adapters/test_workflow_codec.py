from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from issue_agent.adapters.workflow_codec import (
    MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow,
)
from issue_agent.domain.workflow import (
    NoAuthorizedRemediation, OpenGate, RemoteOutcomeUncertain, ResolveEpisode,
    WorkflowSnapshot,
)
from issue_agent.domain.contracts import CommandId
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)


def snapshot():
    return WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(WorkflowKey("o/r", IssueRef(7)), 3), 11),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1),
        ResolveEpisode("episode", 2, "first\nsecond", "operator guidance"),
        (OpenGate("gate", 1, RemoteOutcomeUncertain(
            CommandId("command"), datetime(2026, 9, 14, tzinfo=timezone.utc), 4, "offline"
        ), NoAuthorizedRemediation("reconcile only")),),
    )


def test_roundtrip_preserves_scope_provenance_and_multiline_guidance():
    original = snapshot()
    recovered = decode_workflow(encode_workflow(original), WorkflowSnapshot)
    assert recovered == original
    assert recovered.binding == original.binding
    assert encode_workflow(recovered) == encode_workflow(original)


@pytest.mark.parametrize("raw", [
    "null", "[]", "{}", "{", '{"format":true,"value":null}',
    '{"format":1,"format":1,"value":null}',
    '{"format":1,"value":{"type":"os.system","fields":{}}}',
    '{"format":1,"value":{"type":"ResolveEpisode","fields":{"episode_id":"x"}}}',
    '{"format":1,"value":NaN}',
    "[" * 1000 + "]" * 1000,
    " " * (MAX_WORKFLOW_PAYLOAD_BYTES + 1),
])
def test_untrusted_encoding_never_becomes_default_authority(raw):
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(raw, WorkflowSnapshot)


@pytest.mark.parametrize("failures", [True, -1, 1.5, "2", 2**63])
def test_episode_counter_is_validated_at_database_boundary(failures):
    raw = json.loads(encode_workflow(ResolveEpisode("episode")))
    raw["value"]["fields"]["failures"] = failures
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ResolveEpisode)


def test_wrong_root_variant_and_unknown_fields_are_rejected():
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(encode_workflow(ResolveEpisode("episode")), WorkflowSnapshot)
    raw = json.loads(encode_workflow(ResolveEpisode("episode")))
    raw["value"]["fields"]["future_allowance"] = 100
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ResolveEpisode)


def test_nested_constructor_rejects_future_gate_generation():
    raw = json.loads(encode_workflow(snapshot()))
    raw["value"]["fields"]["gates"][0]["fields"]["opened_generation"] = 100
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), WorkflowSnapshot)
