from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from issue_agent.domain.agent_task import RunIntent, run_input_fingerprint
from issue_agent.adapters.workflow_codec import (
    MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow,
)
from issue_agent.domain.workflow import (
    AuthoredResolve, ResolveAdmission, ResolveSession, ResolveStage,
    NoAuthorizedRemediation, OpenGate, RemoteOutcomeUncertain, ResolveEpisode, RetryAuthorizationRequired,
    WorkflowSnapshot,
)
from issue_agent.domain.contracts import CommandId, RunId, WorkflowAttemptIdentity
from issue_agent.domain.resolve_return import PendingResolveReturn, ReturnedResolve
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


def test_unclassified_return_roundtrip_retains_exact_provider_evidence():
    from tests.domain.test_resolve_return import returned
    receipt = returned()
    assert decode_workflow(encode_workflow(receipt), ReturnedResolve) == receipt
    pending = PendingResolveReturn(receipt, receipt.returned_at, 2, "read unavailable")
    assert decode_workflow(encode_workflow(pending), PendingResolveReturn) == pending


def test_roundtrip_preserves_scope_provenance_and_multiline_guidance():
    original = snapshot()
    recovered = decode_workflow(encode_workflow(original), WorkflowSnapshot)
    assert recovered == original
    assert recovered.binding == original.binding
    assert encode_workflow(recovered) == encode_workflow(original)


def test_verified_absence_gate_survives_restart_without_becoming_unknown_or_open_for_execution():
    original = snapshot()
    original_gate = original.gates[0]
    assert isinstance(original_gate, OpenGate)
    gate = replace(original_gate, cause=RetryAuthorizationRequired(CommandId("command"), "absence-proof"))
    original = replace(original, gates=(gate,))
    recovered = decode_workflow(encode_workflow(original), WorkflowSnapshot)
    assert recovered == original
    assert recovered.gates[0].cause == RetryAuthorizationRequired(CommandId("command"), "absence-proof")


@pytest.mark.parametrize("value", [None, "", " ", 1, "a" * 513, "proof\ninvalid"])
def test_absence_gate_requires_a_valid_durable_evidence_identity(value):
    raw = json.loads(encode_workflow(RetryAuthorizationRequired(CommandId("command"), "proof")))
    raw["value"]["fields"]["absence_evidence_id"] = value
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), RetryAuthorizationRequired)


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


@pytest.mark.parametrize("with_session", [False, True])
def test_authored_checkpoint_and_independent_session_order_survive_roundtrip(with_session):
    state = snapshot()
    author = WorkflowAttemptIdentity(state.binding, RunId("author"), "author-leg")
    source = ResolveAdmission(author, replace(state.revision, version=5), ResolveStage.AUTHOR,
                              input_fingerprint=run_input_fingerprint(RunIntent("Author issue 7 contract")))
    implement = WorkflowAttemptIdentity(state.binding, RunId("implement"), "implement-leg")
    session = ResolveSession(implement, "session:id", "codex", "<runtime-default>") if with_session else None
    checkpoint = AuthoredResolve(source, 8, session)
    state = replace(state, episode=replace(state.episode, continuation=checkpoint))
    recovered = decode_workflow(encode_workflow(state), WorkflowSnapshot)
    assert recovered == state
    assert recovered.episode.continuation.author_admission == source
    assert recovered.episode.continuation.last_applied_admission_version == 8
    assert recovered.episode.continuation.session == session


def test_other_admitted_stage_cannot_be_deserialized_as_authored_checkpoint():
    state = snapshot()
    source = ResolveAdmission(WorkflowAttemptIdentity(state.binding, RunId("run"), "leg"),
                              replace(state.revision, version=5), ResolveStage.AUTHOR,
                              input_fingerprint=run_input_fingerprint(RunIntent("Author issue 7 contract")))
    raw = json.loads(encode_workflow(AuthoredResolve(source, 5)))
    raw["value"]["fields"]["author_admission"]["fields"]["stage"]["value"] = "IMPLEMENT"
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), AuthoredResolve)


@pytest.mark.parametrize("fingerprint", [None, "", True, 123, "a" * 63, "a" * 65, "G" * 64])
def test_checkpoint_without_valid_input_provenance_cannot_be_recovered(fingerprint):
    state = snapshot()
    source = ResolveAdmission(WorkflowAttemptIdentity(state.binding, RunId("author"), "author-leg"),
                              replace(state.revision, version=5), ResolveStage.AUTHOR,
                              input_fingerprint=run_input_fingerprint(RunIntent("Author issue 7 contract")))
    raw = json.loads(encode_workflow(AuthoredResolve(source, 5)))
    raw["value"]["fields"]["author_admission"]["fields"]["input_fingerprint"] = fingerprint
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), AuthoredResolve)
