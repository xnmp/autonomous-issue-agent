"""Observable, isolated SQLite workflow acceptance and recovery contracts."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from issue_agent.adapters.control_store import ControlStoreError, RetiredOwnerError, SQLiteControlStore
from issue_agent.adapters.effects import command_for_action
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, workflow_scope
from issue_agent.domain.contracts import (
    CommandKind, EffectReceipt, EffectStatus, RunId, WorkflowAttemptIdentity,
)
from issue_agent.domain.events import EventEnvelope, Producer, new_event_id
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow import (
    CancelRearm, CancelledRearm, ContinueGrantedRearm, EvidenceUnavailable,
    KeepGuidance, NoAuthorizedRemediation, NoLinkedPr, ObservedIssue, OpenGate,
    OperatorActor, PendingRearm, RemoteOutcomeUncertain, RequestRearm,
    ResolveAttemptResult, ResolveEpisode, ResolveRearmPreflight, ResolveResultKind, RetiredSettledCommand,
    RetiredUncertainCommand, SetGuidance, StopReason, WorkflowAbsent, WorkflowChanged, WorkflowCommitted,
    WorkflowConflict, WorkflowSnapshot,
)
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowWriter
from issue_agent.domain.workflow_policy import (
    cancel_resolve_rearm, complete_resolve_rearm, continue_resolve_rearm,
    reconcile_resolve_ceiling, record_resolve_attempt, request_resolve_rearm,
    resolve_rearm_projection, supersede_resolve_rearm,
)
from issue_agent.domain.world import IssueView
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
WRITER = WorkflowWriter("o/r", "fix", "o/r:fix:deployment")
ACTOR = OperatorActor("operator:alice", "dashboard")


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control" / "db.sqlite"), now=lambda: NOW)
    store.initialize()
    return store


def adapter(control):
    return SQLiteWorkflowControl(control, WRITER, resolve_ceiling=3)


def held(*, extra=()):
    state = replace(resolve_snapshot(), episode=ResolveEpisode("initial", 3), gates=extra)
    return reconcile_resolve_ceiling(state, ceiling=3, evidence_ids=("import",)).snapshot


def observation(state, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}), at=NOW):
    return ObservedIssue(state.revision.fence.key, IssueView(
        state.revision.fence.key.target.number, labels=labels, updated_at="revision:1",
    ), at)


def enrich(proposal):
    preflight = proposal.audit.preflight
    action = resolve_rearm_projection(
        proposal.snapshot, expected_revision=preflight.issue.issue.updated_at,
        external_holds=preflight.external_holds,
    )
    command = command_for_action(action, repository="o/r", executor_identity=WRITER.owner_id, workflow=proposal.snapshot.binding)
    command = replace(command, command_id=proposal.snapshot.intent.required_commands[0], idempotency_key=f"workflow:{proposal.snapshot.intent.request_id}")
    return replace(proposal, commands=(command,))


def request(state, name="request:1", *, guidance=None, external_holds=()):
    gate = next(g for g in state.gates if getattr(g.cause, "episode_id", None) == state.episode.episode_id)
    return enrich(request_resolve_rearm(
        state, RequestRearm(name, state.revision, gate.gate_id, guidance or KeepGuidance()),
        ACTOR, observation(state), NoLinkedPr(state.revision.fence.key, NOW),
        now=NOW, active_attempts=(), external_holds=external_holds,
    ))


def commit(store, proposal):
    return adapter(store).commit_workflow(proposal.expected, (proposal.snapshot.authority,), proposal)


def evidence(control, state, name="observation:1", payload=None):
    payload = payload or ResolveRearmPreflight(observation(state, frozenset({Label.AI_READY})), NoLinkedPr(state.revision.fence.key, NOW))
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                   (name, *workflow_scope(state.revision.fence.key), encode_workflow(payload)))


def run(control, state, *, name="run:1", status="completed", leg="leg:1"):
    control.start_run(RunId(name), json.dumps({"repository": "o/r", "issue": 1, "number": 1, "leg_id": leg}), "resolve", WRITER.owner_id, "config", state=status)
    with control.transaction() as db:
        db.execute("UPDATE runs SET workflow_binding_json=? WHERE run_id=?", (encode_workflow(state.binding), name))
    return ResolveAttemptResult(WorkflowAttemptIdentity(state.binding, RunId(name), leg), ResolveResultKind.COUNTED_REQUEUE, "failed experiment")


def command_row(control, command_id):
    with closing(control._connect()) as db:
        return db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()


def test_read_requires_activation_and_distinguishes_verified_absence(control):
    key = resolve_snapshot().revision.fence.key
    with pytest.raises(WorkflowAuthorityError, match="migration"):
        adapter(control).load_workflow(key)
    install_workflow(control, resolve_snapshot())
    assert isinstance(adapter(control).load_workflow(WorkflowKey("o/r", IssueRef(999))), WorkflowAbsent)
    with pytest.raises(WorkflowAuthorityError, match="scope"):
        adapter(control).load_workflow(WorkflowKey("other/repo", IssueRef(1)))


def test_acceptance_restart_and_duplicate_request_grant_one_episode(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before, guidance=SetGuidance("bounded operator guidance"))
    accepted = commit(control, proposal)
    assert isinstance(accepted, WorkflowCommitted) and not accepted.idempotent
    recovered = adapter(control).load_workflow(before.revision.fence.key)
    assert recovered.episode.failures == 0
    assert recovered.episode.guidance == "bounded operator guidance"
    assert recovered.revision.fence.generation == before.revision.fence.generation + 1
    assert adapter(control).held_projection(before.revision.fence.key).held
    assert isinstance(adapter(control).lookup_rearm(before.revision.fence.key, "request:1"), PendingRearm)
    assert command_row(control, proposal.commands[0].command_id)["state"] == "pending"
    replay = commit(control, proposal)
    assert isinstance(replay, WorkflowCommitted) and replay.idempotent
    assert replay.snapshot == recovered
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 1


def test_changed_request_payload_and_competing_request_do_not_renew_again(control):
    before = held()
    install_workflow(control, before)
    commit(control, request(before))
    assert isinstance(commit(control, request(before, guidance=SetGuidance("changed"))), WorkflowConflict)
    assert isinstance(commit(control, request(before, "request:other")), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 0


def test_reconstructed_request_replays_original_acceptance_after_fresh_observation(control):
    before = held()
    install_workflow(control, before)
    original = request(before, guidance=SetGuidance("same operator intent"))
    accepted = commit(control, original).snapshot
    with closing(control._connect()) as db:
        original_audit = db.execute("SELECT actor_json FROM workflow_rearms WHERE request_id='request:1'").fetchone()[0]
    later = NOW + timedelta(seconds=90)
    reconstructed = enrich(request_resolve_rearm(
        before, original.audit.command, ACTOR,
        observation(before, at=later), NoLinkedPr(before.revision.fence.key, later),
        now=later, active_attempts=(),
    ))
    reconstructed = replace(reconstructed, commands=(replace(reconstructed.commands[0], config_version="new-config"),))
    reopened_store = SQLiteControlStore(control.path, now=lambda: later)
    result = commit(reopened_store, reconstructed)
    assert isinstance(result, WorkflowCommitted) and result.idempotent
    assert result.snapshot == accepted
    with closing(control._connect()) as db:
        assert db.execute("SELECT actor_json FROM workflow_rearms WHERE request_id='request:1'").fetchone()[0] == original_audit
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 1
    assert command_row(control, original.commands[0].command_id)["config_version"] == original.commands[0].config_version


def test_request_replay_belongs_to_same_authenticated_subject(control):
    before = held()
    install_workflow(control, before)
    original = request(before)
    commit(control, original)
    other_subject = replace(original, audit=replace(original.audit, actor=OperatorActor("operator:bob", "dashboard")))
    assert isinstance(commit(control, other_subject), WorkflowConflict)
    other_channel = replace(original, audit=replace(original.audit, actor=OperatorActor(ACTOR.subject, "operator_cli")))
    assert commit(control, other_channel).idempotent


def test_request_id_reuse_for_another_workflow_conflicts(control):
    before = held()
    install_workflow(control, before)
    commit(control, request(before))
    other = replace(resolve_snapshot(2), episode=ResolveEpisode("episode-2", 3))
    other = reconcile_resolve_ceiling(other, ceiling=3, evidence_ids=("import-2",)).snapshot
    install_workflow(control, other)
    assert isinstance(commit(control, request(other)), WorkflowConflict)
    assert adapter(control).load_workflow(other.revision.fence.key).episode.failures == 3


def test_concurrent_acceptances_serialize_to_one_winning_intent(control):
    before = held()
    install_workflow(control, before)
    proposals = (request(before, "left"), request(before, "right"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: commit(control, p), proposals))
    assert sum(isinstance(r, WorkflowCommitted) for r in results) == 1
    assert sum(isinstance(r, WorkflowConflict) for r in results) == 1


@pytest.mark.parametrize("table", ["lifecycle_commands", "workflow_rearm_commands", "dedupe_keys"])
def test_sql_failure_rolls_back_generation_allowance_gates_intent_and_outbox(control, table):
    before = held()
    install_workflow(control, before)
    with control.transaction() as db:
        db.execute(f"CREATE TRIGGER fail_workflow BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected crash"):
        commit(control, request(before))
    assert adapter(control).load_workflow(before.revision.fence.key) == before
    assert adapter(control).lookup_rearm(before.revision.fence.key, "request:1") is None
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM lifecycle_commands").fetchone()[0] == 0


def test_accepted_state_and_events_survive_a_down_telemetry_sink(control):
    before = held()
    install_workflow(control, before)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    def factory(payload, correlation):
        return EventEnvelope(payload, new_event_id(NOW), 0, NOW, NOW, 0, producer, correlation)
    def offline(_event):
        raise RuntimeError("sink offline")
    control.bind_event_outbox(factory, offline)
    commit(control, request(before))
    published = []
    control.bind_event_outbox(factory, published.append)
    assert {e.payload.entity_kind for e in published} == {"workflow", "lifecycle_command"}
    assert isinstance(adapter(control).lookup_rearm(before.revision.fence.key, "request:1"), PendingRearm)


def test_retired_owner_epoch_change_and_wrong_executor_cannot_commit(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    with control.transaction() as db:
        db.execute("INSERT INTO retired_owners VALUES (?, ?, 'o/r', 'fix', ?)", (WRITER.owner_id, "o/r:fix:new", NOW.isoformat()))
    with pytest.raises(RetiredOwnerError):
        commit(control, proposal)
    active = SQLiteWorkflowControl(control, replace(WRITER, owner_id="o/r:fix:new"), resolve_ceiling=3)
    with pytest.raises(WorkflowAuthorityError, match="owner"):
        active.commit_workflow(proposal.expected, (before.authority,), proposal)
    with control.transaction() as db:
        db.execute("UPDATE workflow_authority_epochs SET epoch=2")
    with pytest.raises(WorkflowAuthorityError, match="epoch"):
        active.load_workflow(before.revision.fence.key)


@pytest.mark.parametrize("status,lease", [("reserved", False), ("running", False), ("completed", True)])
def test_acceptance_checks_real_active_runs_and_leases(control, status, lease):
    before = held()
    install_workflow(control, before)
    run(control, before, status=status)
    if lease:
        with control.transaction() as db:
            db.execute("INSERT INTO leases(run_id, dispatch_id, owner_id, pid, pgid, started_at) VALUES ('run:1', 'dispatch', ?, 1, 1, ?)", (WRITER.owner_id, NOW.isoformat()))
    with pytest.raises(WorkflowAuthorityError, match="active admitted"):
        commit(control, request(before))
    assert adapter(control).load_workflow(before.revision.fence.key) == before


def test_result_exact_run_dedup_and_leg_identity_prevent_double_charge(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    result = run(control, before)
    proposal = record_resolve_attempt(before, result, ceiling=3)
    assert commit(control, proposal).snapshot.episode.failures == 1
    assert commit(control, proposal).idempotent
    after = adapter(control).load_workflow(before.revision.fence.key)
    forged = replace(result, attempt=replace(result.attempt, leg_id="another-leg"))
    assert isinstance(commit(control, record_resolve_attempt(after, forged, ceiling=3)), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 1


def test_unadmitted_or_wrong_leg_result_cannot_consume_allowance(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    result = ResolveAttemptResult(WorkflowAttemptIdentity(before.binding, RunId("missing"), "leg"), ResolveResultKind.COUNTED_REQUEUE)
    with pytest.raises(WorkflowAuthorityError, match="admitted"):
        commit(control, record_resolve_attempt(before, result, ceiling=3))
    result = run(control, before)
    forged = replace(result, attempt=replace(result.attempt, leg_id="forged-leg"))
    with pytest.raises(WorkflowAuthorityError, match="disagrees"):
        commit(control, record_resolve_attempt(before, forged, ceiling=3))


def test_projection_preserves_other_gates_and_explicit_external_holds(control):
    other = OpenGate("dependency", 2, EvidenceUnavailable("linked_issue", NOW, 1, "offline"), NoAuthorizedRemediation("read required"))
    before = held(extra=(other,))
    install_workflow(control, before)
    proposal = request(before)
    assert proposal.commands[0].payload["remove"] is None
    assert proposal.commands[0].payload["add"] == Label.NEEDS_HUMAN_REVIEW
    malicious = replace(proposal.commands[0], payload={**proposal.commands[0].payload, "remove": Label.NEEDS_HUMAN_REVIEW.value, "add": Label.AI_READY.value})
    with pytest.raises(ValueError, match="unrelated"):
        commit(control, replace(proposal, commands=(malicious,)))
    commit(control, proposal)
    assert other in adapter(control).load_workflow(before.revision.fence.key).gates


def test_stale_preflight_is_rechecked_inside_acceptance_transaction(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    control._now = lambda: NOW + timedelta(minutes=6)
    with pytest.raises(ValueError, match="authorized state"):
        commit(control, proposal)


def test_cancel_pending_obligation_is_audited_and_never_reexecutes(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    stopped = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, "request:1", "operator cancelled"), ACTOR)
    state = commit(control, stopped).snapshot
    assert isinstance(state.intent, CancelledRearm)
    assert state.episode == pending.episode
    row = command_row(control, proposal.commands[0].command_id)
    assert isinstance(decode_workflow(row["workflow_reconciliation_json"], RetiredSettledCommand), RetiredSettledCommand)
    with pytest.raises(ControlStoreError, match="retired"):
        control.begin_command(proposal.commands[0].command_id)
    continued = enrich(continue_resolve_rearm(
        state, ContinueGrantedRearm("continue", state.revision, "request:1"), ACTOR,
        observation(state), NoLinkedPr(state.revision.fence.key, NOW), now=NOW, active_attempts=(),
    ))
    assert commit(control, continued).snapshot.episode == pending.episode
    assert isinstance(adapter(control).lookup_rearm(before.revision.fence.key, "request:1"), CancelledRearm)


def test_cancel_unknown_retains_uncertainty_receipt_and_stops_new_writes(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME, error="connection lost")
    control.record_effect(receipt)
    command = CancelRearm("cancel", pending.revision, "request:1", "operator cancelled")
    with pytest.raises(WorkflowAuthorityError, match="uncertainty gate"):
        commit(control, cancel_resolve_rearm(pending, command, ACTOR))
    stopped = cancel_resolve_rearm(pending, command, ACTOR, uncertainty=(RemoteOutcomeUncertain(command_id, NOW, 3, "read unavailable"),))
    state = commit(control, stopped).snapshot
    assert state.episode == pending.episode
    assert control.effect_receipt(command_id) == receipt
    retired = decode_workflow(command_row(control, command_id)["workflow_reconciliation_json"], RetiredUncertainCommand)
    assert retired.uncertainty.history.failed_reads == 3
    assert adapter(control).held_projection(before.revision.fence.key).held
    with pytest.raises(ControlStoreError, match="retired"):
        control.begin_command(command_id)


def test_completion_requires_durable_evidence_and_success_receipt(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    complete = complete_resolve_rearm(
        pending, observation(pending, frozenset({Label.AI_READY})), NoLinkedPr(pending.revision.fence.key, NOW),
        verified_commands=(command_id,), evidence_ids=("observation:1",), now=NOW,
    )
    with pytest.raises(WorkflowAuthorityError, match="evidence"):
        commit(control, complete)
    evidence(control, pending)
    with pytest.raises(WorkflowAuthorityError, match="receipt"):
        commit(control, complete)
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.SUCCEEDED))
    completed = commit(control, complete).snapshot
    assert not adapter(control).held_projection(before.revision.fence.key).held
    assert completed.episode == pending.episode
    assert control.effect_receipt(command_id).status is EffectStatus.SUCCEEDED


def test_corrupt_workflow_payload_never_falls_back_to_empty_authority(control):
    before = held()
    install_workflow(control, before)
    with control.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='{}'")
    with pytest.raises(WorkflowAuthorityError, match="invalid"):
        adapter(control).load_workflow(before.revision.fence.key)


def test_well_formed_arbitrary_replacement_count_does_not_become_authority(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    proposal = record_resolve_attempt(before, run(control, before), ceiling=3)
    assert isinstance(proposal, WorkflowChanged)
    forged = replace(proposal, snapshot=replace(proposal.snapshot, episode=replace(proposal.snapshot.episode, failures=10)))
    with pytest.raises(ValueError, match="pure transition"):
        commit(control, forged)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 0


def test_missing_required_outbox_does_not_commit_partial_intent(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    with pytest.raises(ValueError, match="outbox obligations"):
        commit(control, replace(proposal, commands=()))
    assert adapter(control).load_workflow(before.revision.fence.key) == before


def test_completed_receipt_cannot_override_fresh_remote_hold_evidence(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.SUCCEEDED))
    complete = complete_resolve_rearm(
        pending, observation(pending, frozenset({Label.AI_READY})), NoLinkedPr(pending.revision.fence.key, NOW),
        verified_commands=(command_id,), evidence_ids=("observation:1",), now=NOW,
    )
    evidence(control, pending, payload=ResolveRearmPreflight(observation(pending), NoLinkedPr(pending.revision.fence.key, NOW)))
    with pytest.raises(ValueError, match="authorized state"):
        commit(control, complete)
    assert adapter(control).held_projection(before.revision.fence.key).held


def test_unchanged_remote_revision_is_not_material_decision_supersession(control):
    before = held()
    install_workflow(control, before)
    pending = commit(control, request(before)).snapshot
    evidence(control, pending, payload=observation(pending))
    proposal = supersede_resolve_rearm(pending, StopReason.DECISION_CHANGED, "observation:1")
    with pytest.raises(ValueError, match="materially changed"):
        commit(control, proposal)
    changed = replace(observation(pending), issue=replace(observation(pending).issue, body="new requirements"))
    evidence(control, pending, "changed", changed)
    accepted = commit(control, supersede_resolve_rearm(pending, StopReason.DECISION_CHANGED, "changed"))
    assert accepted.snapshot.episode == pending.episode
    assert adapter(control).held_projection(before.revision.fence.key).held


def test_terminal_supersession_requires_verified_closed_issue(control):
    before = held()
    install_workflow(control, before)
    pending = commit(control, request(before)).snapshot
    evidence(control, pending, payload=observation(pending))
    with pytest.raises(ValueError, match="terminal issue"):
        commit(control, supersede_resolve_rearm(pending, StopReason.TARGET_TERMINAL, "observation:1"))
    closed = replace(observation(pending), issue=replace(observation(pending).issue, state="closed"))
    evidence(control, pending, "closed", closed)
    assert commit(control, supersede_resolve_rearm(pending, StopReason.TARGET_TERMINAL, "closed")).snapshot.episode == pending.episode


def test_required_effect_rejection_cannot_be_faked_by_an_observation(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    evidence(control, pending, payload=observation(pending))
    stopped = supersede_resolve_rearm(pending, StopReason.REQUIRED_EFFECT_REJECTED, "observation:1")
    with pytest.raises(WorkflowAuthorityError, match="exact required"):
        commit(control, stopped)
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.PERMANENT_FAILURE, error="stale revision")
    control.record_effect(receipt)
    assert commit(control, stopped).snapshot.episode == pending.episode
    assert control.effect_receipt(command_id) == receipt


def test_external_holds_are_persisted_in_acceptance_and_cannot_be_relabelled_away(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before, external_holds=("budget authority unavailable",))
    assert proposal.commands[0].payload["remove"] is None
    assert proposal.commands[0].payload["add"] == Label.NEEDS_HUMAN_REVIEW
    commit(control, proposal)
    with closing(control._connect()) as db:
        row = db.execute("SELECT actor_json FROM workflow_rearms WHERE request_id='request:1'").fetchone()
    from issue_agent.domain.workflow import AuthorizedOperatorCommand
    audit = decode_workflow(row["actor_json"], AuthorizedOperatorCommand)
    assert audit.preflight.external_holds == ("budget authority unavailable",)


@pytest.mark.parametrize("kind", [
    ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL,
    ResolveResultKind.BLOCKED, ResolveResultKind.PR_OPENED,
])
def test_uncounted_results_are_durable_and_conflicting_reclassification_never_charges(control, kind):
    before = resolve_snapshot()
    install_workflow(control, before)
    admitted = run(control, before)
    result = replace(admitted, kind=kind)
    recorded = adapter(control).record_attempt(result)
    assert recorded.snapshot == before
    assert adapter(control).record_attempt(result).idempotent
    assert isinstance(adapter(control).record_attempt(admitted), WorkflowConflict)
    assert isinstance(commit(control, record_resolve_attempt(before, admitted, ceiling=3)), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 0


def test_old_generation_result_is_audited_but_never_charges_renewed_allowance(control):
    before = held()
    install_workflow(control, before)
    late = run(control, before)
    current = commit(control, request(before)).snapshot
    recorded = adapter(control).record_attempt(late)
    assert recorded.snapshot.episode == current.episode
    assert recorded.snapshot.revision == current.revision
    assert adapter(control).record_attempt(late).idempotent
    conflict = adapter(control).record_attempt(replace(late, kind=ResolveResultKind.BLOCKED))
    assert isinstance(conflict, WorkflowConflict)


def test_counted_record_attempt_and_commit_share_one_run_receipt(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    result = run(control, before)
    assert adapter(control).record_attempt(result).snapshot.episode.failures == 1
    replay = commit(control, record_resolve_attempt(before, result, ceiling=3))
    assert replay.idempotent
    assert replay.snapshot.episode.failures == 1


def test_result_dedupe_crash_rolls_back_its_counter_and_gate(control):
    before = replace(resolve_snapshot(), episode=ResolveEpisode("initial", 2))
    install_workflow(control, before)
    result = run(control, before)
    with control.transaction() as db:
        db.execute("CREATE TRIGGER fail_result BEFORE INSERT ON dedupe_keys BEGIN SELECT RAISE(ABORT, 'result crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="result crash"):
        adapter(control).record_attempt(result)
    assert adapter(control).load_workflow(before.revision.fence.key) == before
    assert not adapter(control).held_projection(before.revision.fence.key).held


@pytest.mark.parametrize("status", [EffectStatus.SUCCEEDED, EffectStatus.PERMANENT_FAILURE, EffectStatus.RETRYABLE_FAILURE, EffectStatus.UNKNOWN_OUTCOME])
def test_workflow_progression_refuses_foreign_persisted_receipt_executor(control, status):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, status,
                                       error=None if status is EffectStatus.SUCCEEDED else "failed"))
    # Corrupt persistence is independent of the now-hardened record_effect
    # boundary; workflow authority must not bless an existing foreign row.
    with control.transaction() as db:
        db.execute("UPDATE effect_receipts SET executor_identity='foreign:executor' WHERE command_id=?", (command_id,))
    if status is EffectStatus.SUCCEEDED:
        evidence(control, pending)
        transition = complete_resolve_rearm(pending, observation(pending, frozenset({Label.AI_READY})),
            NoLinkedPr(pending.revision.fence.key, NOW), verified_commands=(command_id,),
            evidence_ids=("observation:1",), now=NOW)
    elif status is EffectStatus.PERMANENT_FAILURE:
        evidence(control, pending, payload=observation(pending))
        transition = supersede_resolve_rearm(pending, StopReason.REQUIRED_EFFECT_REJECTED, "observation:1")
    else:
        uncertainty = (RemoteOutcomeUncertain(command_id, NOW, 0, "unknown"),) if status is EffectStatus.UNKNOWN_OUTCOME else ()
        transition = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, "request:1", "cancelled"), ACTOR, uncertainty=uncertainty)
    with pytest.raises(WorkflowAuthorityError, match="receipt|rejection"):
        commit(control, transition)
    assert isinstance(adapter(control).lookup_rearm(before.revision.fence.key, "request:1"), PendingRearm)


@pytest.mark.parametrize("status", [EffectStatus.SUCCEEDED, EffectStatus.PERMANENT_FAILURE])
def test_terminal_receipt_remains_valid_after_audited_command_owner_adoption(control, status):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, status,
                            error=None if status is EffectStatus.SUCCEEDED else "stale revision")
    control.record_effect(receipt)
    successor = replace(WRITER, owner_id="o/r:fix:successor")
    control.adopt_legacy_owner("o/r", "fix", successor.owner_id, prior_owner_ids=(WRITER.owner_id,))
    if status is EffectStatus.SUCCEEDED:
        evidence(control, pending)
        transition = complete_resolve_rearm(pending, observation(pending, frozenset({Label.AI_READY})),
            NoLinkedPr(pending.revision.fence.key, NOW), verified_commands=(command_id,),
            evidence_ids=("observation:1",), now=NOW)
    else:
        evidence(control, pending, payload=observation(pending))
        transition = supersede_resolve_rearm(pending, StopReason.REQUIRED_EFFECT_REJECTED, "observation:1")
    row = command_row(control, command_id)
    assert row["executor_identity"] == row["attempt_owner"] == WRITER.owner_id
    new_control = SQLiteWorkflowControl(control, successor, resolve_ceiling=3)
    accepted = new_control.commit_workflow(transition.expected, (pending.authority,), transition)
    assert isinstance(accepted, WorkflowCommitted)
    assert accepted.snapshot.episode == pending.episode
    assert control.effect_receipt(command_id) == receipt


def test_unknown_receipt_retains_original_executor_after_active_command_adoption(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME, error="lost connection")
    control.record_effect(receipt)
    successor = replace(WRITER, owner_id="o/r:fix:successor")
    control.adopt_legacy_owner("o/r", "fix", successor.owner_id, prior_owner_ids=(WRITER.owner_id,))
    row = command_row(control, command_id)
    assert row["executor_identity"] == successor.owner_id
    assert row["attempt_owner"] == WRITER.owner_id
    stopped = cancel_resolve_rearm(pending, CancelRearm("cancel", pending.revision, "request:1", "cancelled"), ACTOR,
        uncertainty=(RemoteOutcomeUncertain(command_id, NOW, 0, "unavailable"),))
    new_control = SQLiteWorkflowControl(control, successor, resolve_ceiling=3)
    assert isinstance(new_control.commit_workflow(stopped.expected, (pending.authority,), stopped), WorkflowCommitted)
    assert control.effect_receipt(command_id) == receipt
    assert isinstance(decode_workflow(command_row(control, command_id)["workflow_reconciliation_json"], RetiredUncertainCommand), RetiredUncertainCommand)
