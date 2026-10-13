from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from issue_agent.adapters.workflow_codec import (
    MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow,
)
from issue_agent.domain.workflow import (
    NoAuthorizedRemediation, OpenGate, RemoteOutcomeUncertain, ResolveEpisode, RetryAuthorizationRequired,
    WorkflowSnapshot,
)
from issue_agent.domain.contracts import ChargeOrigin, CommandId, Money, PricingBasis, RunId, SettlementRequest
from issue_agent.domain.resolve_return import PendingResolveReturn, ReturnedResolve
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)


@pytest.mark.parametrize("raw,field", [
    (r'{"format":1,"\u0066ormat":1,"value":null}', "format"),
    ('{"format":1,"value":[{"type":"datetime","value":"x","value":"y"}]}', "value"),
    ('{"format":1,"value":null,"format":2,"value":false}', "format"),
])
def test_decoder_rejects_duplicate_fields_including_escaped_and_nested_keys(raw, field):
    with pytest.raises(WorkflowEncodingError, match=f"duplicate workflow JSON field: {field}"):
        decode_workflow(raw, object)


def test_legacy_settlement_receipt_remains_byte_stable_and_gap_round_trips():
    receipt = SettlementRequest(RunId("r"), Money.zero(), PricingBasis.ESTIMATE, "provider:r", None, None, None)
    raw = encode_workflow(receipt)
    assert "accounting_gap" not in json.loads(raw)["value"]["fields"]
    for _ in range(2):
        receipt = decode_workflow(raw, SettlementRequest)
        assert receipt.accounting_gap == ""
        assert encode_workflow(receipt) == raw
    corrected = replace(receipt, accounting_gap="completed request has no usage")
    assert decode_workflow(encode_workflow(corrected), SettlementRequest) == corrected


@pytest.mark.parametrize("origin", tuple(ChargeOrigin))
@pytest.mark.parametrize("gap", ["", "receipt gap"])
def test_settlement_origin_survives_command_replay_independently_of_basis(origin, gap):
    receipt = SettlementRequest(RunId("r"), Money("USD", 50), PricingBasis.ESTIMATE,
                                "provider:r", None, None, None, gap, origin)
    assert decode_workflow(encode_workflow(receipt), SettlementRequest) == receipt


def test_repeated_input_still_runs_domain_constructor_validation(monkeypatch):
    from issue_agent.domain import workflow_identity

    value = WorkflowRevision(WorkflowFence(WorkflowKey("o/r", IssueRef(7)), 3), 11)
    raw = encode_workflow(value)
    assert decode_workflow(raw, WorkflowRevision) == value

    def refuse(_value, _name, **_kwargs):
        raise ValueError("current constructor refuses this value")

    monkeypatch.setattr(workflow_identity, "validate_workflow_integer", refuse)
    with pytest.raises(WorkflowEncodingError, match="current constructor refuses"):
        decode_workflow(raw, WorkflowRevision)


@pytest.mark.parametrize("value", ["short", "x" * 20000, "é" * 10000],
                         ids=["small", "large-ascii", "large-utf8"])
def test_repeated_decode_preserves_changed_input_and_expected_root_validation(value):
    raw = json.dumps({"format": 1, "value": value}, ensure_ascii=False)
    assert decode_workflow(raw, str) == value
    with pytest.raises(WorkflowEncodingError, match="wrong root variant"):
        decode_workflow(raw, tuple)
    changed = json.dumps({"format": 1, "value": value + "changed"}, ensure_ascii=False)
    assert decode_workflow(changed, str) == value + "changed"
    assert decode_workflow(raw, str) == value


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


@pytest.mark.parametrize("kind", ["request", "continue", "cancel", "retry"])
def test_operator_command_codec_retains_binding_and_reads_old_resolve_audits(kind):
    from issue_agent.domain.workflow import RequestRearm, ContinueGrantedRearm, CancelRearm, AuthorizeAbsentRetry, KeepGuidance

    state = snapshot()
    commands = {
        "request": RequestRearm("r", state.revision, "gate", KeepGuidance()),
        "continue": ContinueGrantedRearm("r", state.revision, "old"),
        "cancel": CancelRearm("c", state.revision, "r", "operator stopped"),
        "retry": AuthorizeAbsentRetry("a", state.revision, CommandId("command"), "gate", "evidence"),
    }
    command = commands[kind]
    bound = replace(command, expected_binding=state.binding)
    assert decode_workflow(encode_workflow(bound), type(command)) == bound
    raw = json.loads(encode_workflow(command))
    assert "expected_binding" not in raw["value"]["fields"]
    assert decode_workflow(json.dumps(raw), type(command)) == command

    wrong = replace(state.binding, fence=replace(state.binding.fence, generation=99))
    with pytest.raises(ValueError, match="captured revision"):
        replace(command, expected_binding=wrong)
    with pytest.raises(ValueError, match="captured revision"):
        replace(command, expected_binding="not a binding")


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


def test_histories_persisted_before_re_execution_counting_decode_as_none_spent():
    from issue_agent.domain.workflow import ActiveReconciliation

    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    history = ActiveReconciliation(now, now, 3, 2)
    raw = json.loads(encode_workflow(history))
    assert decode_workflow(json.dumps(raw), ActiveReconciliation) == history
    del raw["value"]["fields"]["reexecutions"]
    assert decode_workflow(json.dumps(raw), ActiveReconciliation) == ActiveReconciliation(now, now, 3, 0)


def test_capture_wire_roundtrips_and_retired_infra_ceiling_is_dropped_on_read():
    from pathlib import Path
    from issue_agent.domain import continuation_components as cc
    literal = json.loads((Path(__file__).parents[1] / "fixtures/phase4-pre-f7-capture.json").read_text())
    old = decode_workflow(literal["capture_wire"], cc.ContinuationCapture)
    assert encode_workflow(old) == literal["capture_wire"] and repr(old) == literal["capture_repr"]
    for ceiling in (None, 0, 5):
        wire = json.loads(literal["capture_wire"])
        wire["value"]["fields"]["fix_infra_ceiling"] = ceiling
        stamped = decode_workflow(json.dumps(wire), cc.ContinuationCapture)
        assert stamped == old and encode_workflow(stamped) == literal["capture_wire"]


