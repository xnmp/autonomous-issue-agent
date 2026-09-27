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


def test_split_reply_routing_and_contract_provenance_survive_restart():
    from tests.domain.test_resolve_split import split_example
    from issue_agent.domain.resolve_split import SplitImplement
    original = split_example()
    recovered = decode_workflow(encode_workflow(original), SplitImplement)
    assert recovered.context.human_reply == "Keep the old API."
    assert recovered.context.selection.model == "model"
    assert recovered.baseline.revision.head_sha == "a" * 40
    assert recovered.source == original.source


def test_old_ordinary_return_does_not_gain_split_authority():
    from tests.domain.test_resolve_return import returned
    receipt = returned()
    raw = json.loads(encode_workflow(receipt))
    for field in ("split_input", "verdict", "rationale", "contract_revision"):
        del raw["value"]["fields"][field]
    recovered = decode_workflow(json.dumps(raw), ReturnedResolve)
    assert recovered == receipt


def test_partially_missing_new_receipt_fields_are_not_treated_as_legacy():
    from tests.domain.test_resolve_return import returned
    raw = json.loads(encode_workflow(returned()))
    del raw["value"]["fields"]["split_input"]
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ReturnedResolve)


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


def test_histories_persisted_before_re_execution_counting_decode_as_none_spent():
    from issue_agent.domain.workflow import ActiveReconciliation

    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    history = ActiveReconciliation(now, now, 3, 2)
    raw = json.loads(encode_workflow(history))
    assert decode_workflow(json.dumps(raw), ActiveReconciliation) == history
    del raw["value"]["fields"]["reexecutions"]
    assert decode_workflow(json.dumps(raw), ActiveReconciliation) == ActiveReconciliation(now, now, 3, 0)


def review_v24_proposal():
    """Rebuild the domain decision independently of its frozen historical bytes."""
    from pathlib import Path
    from issue_agent.domain.review_workflow import (
        BoundReviewSubmission, FreshReviewRequired, ReviewEpisode, ReviewWorkflowSnapshot,
    )
    from issue_agent.domain.review_workflow_policy import ReviewSubmissionReceipt, record_review
    from issue_agent.domain.workflow import WorkflowChanged
    from issue_agent.domain.workflow_identity import PullRequestRef

    fixture = json.loads((Path(__file__).parents[1] / "fixtures/workflow_review_v24_observation.json").read_text())
    facts = fixture["facts"]
    prior = ReviewWorkflowSnapshot(
        WorkflowRevision(WorkflowFence(WorkflowKey(facts["repository"], PullRequestRef(facts["pr_number"])),
                                       facts["generation"]), facts["version"]),
        FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, facts["rejection_epoch"]),
        ReviewEpisode(facts["episode_id"], facts["guidance"], FreshReviewRequired(facts["owed_since_generation"])),
    )
    receipt = ReviewSubmissionReceipt(BoundReviewSubmission(CommandId(facts["command_id"]), prior.binding),
                                      facts["head_sha"], facts["patch_id"], facts["receipt_id"], facts["escalated"])
    proposal = record_review(prior, receipt)
    assert isinstance(proposal, WorkflowChanged)
    return fixture, proposal


def activated_review_execution(state, *, imported=False):
    from issue_agent.domain.agent_task import RuntimeName
    from issue_agent.domain.review_execution import (
        ActivatedReviewExecution, ImportedReviewSession, LaunchedReviewChild, ReusableReviewSession,
        ReviewChildIdentity, ReviewChildKind, ReviewExecutionBinding, ReviewExecutionCheckpoint,
        ReviewExecutionOrigin, ReviewExecutionOriginKind, ReviewExecutionPolicy, ReviewExecutionState,
        ReviewRoundIdentity,
    )

    binding = ReviewExecutionBinding(state.binding, 3, "execution:codec")
    source = (ImportedReviewSession("import:codec") if imported else
              LaunchedReviewChild(ReviewChildIdentity(ReviewRoundIdentity(binding, "round:codec", 4),
                                                       "review", "reviewer", ReviewChildKind.REVIEWER),
                                  "run:codec", "launch:codec"))
    execution = ReviewExecutionState(binding, failures=2, pin_deferrals=7,
                                     session=ReusableReviewSession(RuntimeName.CLAUDE, "session:codec", source),
                                     session_sequence=0 if imported else 4, displayed_round=3,
                                     display_sequence=4, productive_sequence=4)
    checkpoint = ReviewExecutionCheckpoint(6, execution, ReviewExecutionPolicy(free_pin_deferrals=1))
    return ActivatedReviewExecution(ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "origin:codec"),
                                    checkpoint, execution, 6)


def test_current_review_snapshot_has_a_distinct_tag_and_required_inactive_component():
    from issue_agent.domain.review_execution import ExecutionNotActivated
    from issue_agent.domain.review_workflow import ReviewWorkflowSnapshot

    _, proposal = review_v24_proposal()
    raw = json.loads(encode_workflow(proposal.snapshot))
    assert raw["value"]["type"] == "ReviewWorkflowSnapshotV2"
    assert raw["value"]["fields"]["execution"]["type"] == "ExecutionNotActivated"
    recovered = decode_workflow(json.dumps(raw), ReviewWorkflowSnapshot)
    assert recovered == proposal.snapshot and recovered.execution == ExecutionNotActivated()


def test_exact_v24_review_snapshot_decodes_inactive_without_changing_baseline_or_revision():
    from issue_agent.domain.review_execution import ExecutionNotActivated
    from issue_agent.domain.review_workflow import ReviewWorkflowSnapshot

    fixture, proposal = review_v24_proposal()
    raw = json.loads(fixture["accepted_proposal"])["snapshot"]
    assert json.loads(raw)["value"]["type"] == "ReviewWorkflowSnapshot"
    recovered = decode_workflow(raw, ReviewWorkflowSnapshot)
    assert recovered == proposal.snapshot
    assert recovered.execution == ExecutionNotActivated()
    assert recovered.revision.version == 8 and recovered.revision.fence.generation == 2
    assert recovered.episode.continuation.head_sha == "a" * 40


@pytest.mark.parametrize("change", ["old_extra_execution", "old_missing_intent", "new_missing_execution",
                                     "new_null_execution", "new_unknown_execution", "new_unknown_field"])
def test_review_snapshot_wire_evolution_never_synthesizes_missing_current_authority(change):
    from issue_agent.domain.review_workflow import ReviewWorkflowSnapshot

    fixture, proposal = review_v24_proposal()
    raw = json.loads(json.loads(fixture["accepted_proposal"])["snapshot"] if change.startswith("old") else
                     encode_workflow(proposal.snapshot))
    fields_ = raw["value"]["fields"]
    if change == "old_extra_execution":
        fields_["execution"] = {"type": "ExecutionNotActivated", "fields": {}}
    elif change == "old_missing_intent":
        del fields_["intent"]
    elif change == "new_missing_execution":
        del fields_["execution"]
    elif change == "new_null_execution":
        fields_["execution"] = None
    elif change == "new_unknown_execution":
        fields_["execution"] = {"type": "UnknownReviewExecution", "fields": {}}
    else:
        fields_["future_allowance"] = 100
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ReviewWorkflowSnapshot)


@pytest.mark.parametrize("variant", ["inactive", "active_imported", "active_launched"])
def test_review_family_envelope_and_snapshot_preserve_execution_policy_and_provenance(variant):
    from issue_agent.domain.review_execution import ActivatedReviewExecution
    from issue_agent.domain.review_workflow import ReviewFamilyEpisodes, ReviewWorkflowSnapshot

    _, proposal = review_v24_proposal()
    state = proposal.snapshot
    assert isinstance(state, ReviewWorkflowSnapshot)
    if variant != "inactive":
        state = replace(state, execution=activated_review_execution(state, imported=variant == "active_imported"))
    envelope = ReviewFamilyEpisodes(state.authority, state.episode, state.execution)
    recovered = decode_workflow(encode_workflow(envelope), ReviewFamilyEpisodes)
    recovered_state = decode_workflow(encode_workflow(state), ReviewWorkflowSnapshot)
    assert recovered == envelope and recovered_state == state
    if isinstance(recovered.execution, ActivatedReviewExecution):
        assert recovered.execution.checkpoint.policy.free_pin_deferrals == 1
        assert recovered.execution.state.failures == 2
        assert recovered.execution.state.pin_deferrals == 7
        assert recovered.execution.state.session.handle == "session:codec"
        assert recovered.execution.state.session.source == state.execution.state.session.source


@pytest.mark.parametrize("change", ["missing", "null", "unknown"])
def test_review_family_envelope_requires_its_explicit_execution_variant(change):
    from issue_agent.domain.review_workflow import ReviewFamilyEpisodes

    _, proposal = review_v24_proposal()
    state = proposal.snapshot
    raw = json.loads(encode_workflow(ReviewFamilyEpisodes(state.authority, state.episode, state.execution)))
    fields_ = raw["value"]["fields"]
    if change == "missing":
        del fields_["execution"]
    else:
        fields_["execution"] = None if change == "null" else {"type": "FutureExecution", "fields": {}}
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ReviewFamilyEpisodes)


def test_historical_review_observation_exact_replay_retains_accepted_semantics():
    import hashlib
    from issue_agent.adapters.workflow_commit import accepted_proposal, same_receipt

    fixture, proposal = review_v24_proposal()
    raw = fixture["accepted_proposal"]
    assert hashlib.sha256(raw.encode()).hexdigest() == fixture["accepted_proposal_sha256"]
    current = accepted_proposal(proposal)
    assert current != raw
    assert same_receipt(raw, proposal, current)
    assert same_receipt(current, proposal, current)


@pytest.mark.parametrize("change", ["baseline", "guidance", "gates", "execution", "audit", "commands", "mutation",
                                     "expected", "outer_extra", "old_snapshot_extra"])
def test_historical_review_observation_compatibility_does_not_accept_changed_facts(change):
    from issue_agent.adapters.workflow_commit import accepted_proposal, same_receipt
    from issue_agent.domain.review_workflow import ReviewWorkflowSnapshot

    fixture, proposal = review_v24_proposal()
    assert isinstance(proposal.snapshot, ReviewWorkflowSnapshot)
    accepted = json.loads(fixture["accepted_proposal"])
    if change in {"baseline", "guidance", "gates", "execution", "old_snapshot_extra"}:
        snapshot_raw = json.loads(accepted["snapshot"])
        fields_ = snapshot_raw["value"]["fields"]
        if change == "baseline":
            fields_["episode"]["fields"]["continuation"]["fields"]["head_sha"] = "b" * 40
        elif change == "guidance":
            fields_["episode"]["fields"]["guidance"] = "different guidance"
        elif change == "gates":
            fields_["gates"] = [json.loads(encode_workflow(snapshot().gates[0]))["value"]]
        elif change == "execution":
            snapshot_raw = json.loads(encode_workflow(replace(proposal.snapshot,
                                                             execution=activated_review_execution(proposal.snapshot))))
        else:
            fields_["execution"] = {"type": "ExecutionNotActivated", "fields": {}}
        accepted["snapshot"] = json.dumps(snapshot_raw, sort_keys=True, separators=(",", ":"))
    elif change == "audit":
        from issue_agent.domain.workflow import ObservedTransition
        accepted["audit"] = encode_workflow(ObservedTransition(("submit:review-v24", "other:receipt")))
    elif change == "commands":
        accepted["commands"] = [{"id": "another-command"}]
    elif change == "mutation":
        accepted["mutation"] = "open_gate"
    elif change == "expected":
        accepted["expected"] = encode_workflow(replace(proposal.expected, version=6))
    else:
        accepted["unrecognized"] = "must refuse"
    changed = json.dumps(accepted, sort_keys=True, separators=(",", ":"))
    assert not same_receipt(changed, proposal, accepted_proposal(proposal))


def test_historical_observation_replay_accepts_equivalent_caller_gate_order():
    from issue_agent.adapters.workflow_commit import accepted_proposal, same_receipt
    from issue_agent.domain.review_workflow import ReviewWorkflowSnapshot
    from issue_agent.domain.review_workflow_policy import ReviewSubmissionReceipt, record_review

    fixture, original = review_v24_proposal()
    assert original.prior is not None
    assert isinstance(original.snapshot, ReviewWorkflowSnapshot)
    gate = OpenGate("z-gate", 2, RemoteOutcomeUncertain(CommandId("z-command"), datetime(2026, 9, 27, tzinfo=timezone.utc),
                                                     1, "awaiting observation"), NoAuthorizedRemediation("reconcile"))
    assert isinstance(gate.cause, RemoteOutcomeUncertain)
    first = replace(gate, gate_id="a-gate", cause=replace(gate.cause, command_id=CommandId("a-command")))
    prior = replace(original.prior, gates=(gate, first))
    facts = fixture["facts"]
    baseline = original.snapshot.episode.continuation
    proposal = record_review(prior, ReviewSubmissionReceipt(baseline.source, facts["head_sha"], facts["patch_id"],
                                                           facts["receipt_id"]))
    from issue_agent.domain.workflow import WorkflowChanged
    assert isinstance(proposal, WorkflowChanged)
    current = accepted_proposal(proposal)
    historical = json.loads(current)
    encoded_snapshot = json.loads(historical["snapshot"])
    encoded_snapshot["value"]["type"] = "ReviewWorkflowSnapshot"
    del encoded_snapshot["value"]["fields"]["execution"]
    historical["snapshot"] = json.dumps(encoded_snapshot, sort_keys=True, separators=(",", ":"))
    raw = json.dumps(historical, sort_keys=True, separators=(",", ":"))
    assert same_receipt(raw, proposal, current)
    # Changed gate evidence is a changed accepted fact, even under canonical ordering.
    changed = json.loads(historical["snapshot"])
    changed["value"]["fields"]["gates"][0]["fields"]["cause"]["fields"]["last_error"] = "changed evidence"
    historical["snapshot"] = json.dumps(changed, sort_keys=True, separators=(",", ":"))
    assert not same_receipt(json.dumps(historical, sort_keys=True, separators=(",", ":")), proposal, current)
