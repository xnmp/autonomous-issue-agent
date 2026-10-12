"""Observable, isolated SQLite workflow acceptance and recovery contracts."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_store import (
    AccountingDisposition, ControlStoreError, ReservationRequest, RetiredOwnerError,
    SettlementRequest, SQLiteControlStore,
)
from issue_agent.adapters.effects import command_for_action
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, workflow_scope, load_workflow_row
from issue_agent.domain.contracts import (
    BudgetScope, EffectReceipt, EffectStatus, Money, PriceQuote,
    PricingBasis, ReservationId, RunId, WorkflowAttemptIdentity,
)
from issue_agent.domain.events import EventEnvelope, Producer, new_event_id
from issue_agent.domain.labels import Label
from issue_agent.domain.workflow import (
    FreshResolve, ResolveAdmission, ResolveSession, ResolveStage,
    CancelRearm, CancelledRearm, ContinueGrantedRearm, EvidenceUnavailable,
    KeepGuidance, NoAuthorizedRemediation, NoLinkedPr, ObservedIssue, OpenGate,
    OperatorActor, PendingRearm, RemoteOutcomeUncertain, RequestRearm,
    ResolveAttemptResult, ResolveEpisode, ResolveRearmPreflight, ResolveResultKind, RetiredSettledCommand,
    RetiredUncertainCommand, SetGuidance, StopReason, WorkflowAbsent, WorkflowChanged, WorkflowCommitted,
    WorkflowMigrationRequired,
    WorkflowConflict, WorkflowSnapshot,
)
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowRevision, WorkflowWriter
from issue_agent.domain.workflow_policy import (
    cancel_resolve_rearm, complete_resolve_rearm, continue_resolve_rearm,
    reconcile_resolve_ceiling, record_resolve_attempt, request_resolve_rearm,
    resolve_rearm_projection, supersede_resolve_rearm,
)
from issue_agent.domain.world import IssueView
from issue_agent.domain.resolve_return import PendingResolveReturn, ResolveInputs, ReturnedResolve, UnavailableResolveReturn
from issue_agent.domain.resolve_workspace import PriorResolveAdmission
from issue_agent.domain.workflow_policy import return_gate_id
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot as initial_snapshot
from tests.fakes.workflow_workspace import workspace_observation
from tests.fakes.workflow_workspace import workspace_ack

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
WRITER = WorkflowWriter("o/r", "fix", "o/r:fix:deployment")
ACTOR = OperatorActor("operator:alice", "dashboard")


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control" / "db.sqlite"), now=lambda: NOW)
    store.initialize()
    return store


def resolve_snapshot(number=1, *, generation=2, version=1):
    return initial_snapshot(number, generation=generation, version=version)


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
        proposal.snapshot, preflight.issue.issue, external_holds=preflight.external_holds,
    )
    command = command_for_action(action, repository="o/r", executor_identity=WRITER.owner_id, workflow=proposal.snapshot.binding)
    command = replace(command, command_id=proposal.snapshot.intent.required_commands[0], idempotency_key=f"workflow:{proposal.snapshot.intent.request_id}")
    return replace(proposal, commands=(command,))


def request(state, name="request:1", *, guidance=None, external_holds=(), predecessor=None):
    gate = next(g for g in state.gates if getattr(g.cause, "episode_id", None) == state.episode.episode_id)
    return enrich(request_resolve_rearm(
        state, RequestRearm(name, state.revision, gate.gate_id, guidance or KeepGuidance()),
        ACTOR, observation(state), NoLinkedPr(state.revision.fence.key, NOW),
        now=NOW, active_attempts=(), external_holds=external_holds,
        workspace=workspace_observation(state.binding, NOW, predecessor=predecessor),
    ))


def commit(store, proposal):
    return adapter(store).commit_workflow(proposal.expected, (proposal.snapshot.authority,), proposal)


def prepare_workspace(store, pending):
    acknowledgement = workspace_ack(pending.intent.handoff, NOW, performed_under=pending.binding)
    return adapter(store).complete_workspace_handoff(acknowledgement)


def evidence(control, state, name="observation:1", payload=None):
    payload = payload or ResolveRearmPreflight(observation(state, frozenset({Label.AI_READY})), NoLinkedPr(state.revision.fence.key, NOW))
    with control.transaction() as db:
        db.execute("INSERT INTO workflow_evidence VALUES (?, ?, ?, ?, NULL, 'observation', ?)",
                   (name, *workflow_scope(state.revision.fence.key), encode_workflow(payload)))


def run(control, state, *, name="run:1", status="completed", leg="leg:1", stage=ResolveStage.RESOLVE, session_id=None, runtime="codex", model="<runtime-default>"):
    """An isolated completed admission, including its immutable ordering receipt."""
    result = ResolveAttemptResult(WorkflowAttemptIdentity(state.binding, RunId(name), leg), ResolveResultKind.COUNTED_REQUEUE, "failed experiment", stage)
    control.start_run(RunId(name), json.dumps({"repository": "o/r", "issue": state.revision.fence.key.target.number, "number": state.revision.fence.key.target.number, "leg_id": leg, "kind": stage.value, "runtime": runtime, "input_fingerprint": "a" * 64, "publication_branch": f"fix/{state.revision.fence.key.target.number}-workspace"}), stage.value, WRITER.owner_id, "config", state=status)
    with control.transaction() as db:
        from issue_agent.adapters.control_store import workflow_episode_key
        db.execute("UPDATE runs SET workflow_binding_json=?,workflow_episode_key=? WHERE run_id=?", (encode_workflow(state.binding), workflow_episode_key(state.binding), name))
        admitted = admission(state, result)
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-admission', ?, ?, ?, ?)",
                   (WRITER.repository, name, NOW.isoformat(), encode_workflow(admitted)))
        if stage is not ResolveStage.ROUTE:
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-work-latest',?,?,?,?) "
                "ON CONFLICT(namespace,scope,key) DO UPDATE SET claimed_at=excluded.claimed_at,payload_json=excluded.payload_json",
                (WRITER.repository, f"issue:{state.revision.fence.key.target.number}", NOW.isoformat(), encode_workflow(admitted)),
            )
        if session_id is not None:
            result = replace(result, session=ResolveSession(result.attempt, session_id, runtime, model))
        if status == "completed" or session_id is not None:
            receipt_id = f"{runtime}:{session_id or 'no-session'}:{name}"
            db.execute("UPDATE runs SET provider_receipt_id=? WHERE run_id=?", (receipt_id, name))
            db.execute(
                "INSERT INTO budget_reservations(reservation_id,run_id,repository,workload_repository,dimension,period_id,period_start,period_end,period_timezone,currency,maximum_micros,cap_micros,model,rate_card_id,rate_card_version,quote_effective_at,config_version,state,created_at,updated_at) "
                "VALUES (?,?,'o/r','o/r','resolve','period',?,?,'UTC','USD',1,100,?,'rate','1',?,'config','settled',?,?)",
                (f"reservation:{name}", name, NOW.isoformat(), (NOW + timedelta(days=1)).isoformat(), model, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )
            db.execute("INSERT INTO charges(run_id,provider_receipt_id,amount_micros,currency,basis,settled_at) VALUES (?,?,1,'USD','provider_reported',?)",
                       (name, receipt_id, NOW.isoformat()))
    return result


def admission(state, attempt_result):
    """Captured post-admission fixture; production reads the immutable SQL receipt."""
    return ResolveAdmission(attempt_result.attempt, WorkflowRevision(attempt_result.attempt.binding.fence, state.revision.version), attempt_result.stage,
                            input_fingerprint="a" * 64)


def command_row(control, command_id):
    with closing(control._connect()) as db:
        return db.execute("SELECT * FROM lifecycle_commands WHERE command_id=?", (str(command_id),)).fetchone()


def paid_result(control, state, name, stage, *, money_scope="o/r", model="<runtime-default>", session_id=None, launch=True, settle=True, historical=False):
    """Real accounting; ``historical`` installs a concurrent pre-cutover admission.

    Only this fixture may install the historical shape, never the production
    admission path.
    """
    attempt = WorkflowAttemptIdentity(state.binding, RunId(name), f"leg:{name}")
    session_id = f"session:{name}" if session_id is None else session_id
    session = ResolveSession(attempt, session_id, "codex", model) if session_id else None
    request = ReservationRequest(
        ReservationId(f"reservation:{name}"), attempt.run_id,
        json.dumps({"repository": "o/r", "role": "fix", "kind": stage.value, "leg_id": attempt.leg_id,
                    "runtime": "codex", "input_fingerprint": "a" * 64, "issue": state.revision.fence.key.target.number,
                    "publication_branch": f"fix/{state.revision.fence.key.target.number}-workspace"}),
        BudgetScope(money_scope, "daily", "USD", "2026-09-14", NOW, NOW + timedelta(days=1), ZoneInfo("UTC")),
        PriceQuote(model, "card", "1", NOW, Money("USD", 1)), Money("USD", 100), "config", "o/r",
        kind=stage.value, owner_id=WRITER.owner_id, workflow=state.binding,
    )
    if historical:
        assert control.reserve(replace(request, kind="historical-fixture", workflow=None)).admitted
        with control.transaction() as db:
            current = load_workflow_row(db, state.revision.fence.key)
            assert current is not None
            revision = replace(current.revision, version=current.revision.version + 1)
            from issue_agent.adapters.control_store import workflow_episode_key
            db.execute("UPDATE runs SET kind=?,workflow_binding_json=?,workflow_episode_key=? WHERE run_id=?",
                       (stage.value, encode_workflow(state.binding), workflow_episode_key(state.binding), name))
            db.execute("UPDATE workflows SET version=? WHERE repository=? AND target_kind=? AND target_number=?",
                       (revision.version, *workflow_scope(state.revision.fence.key)))
            legacy = ResolveAdmission(attempt, revision, stage, "a" * 64)
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-admission',?,?,?,?)",
                       (WRITER.repository, name, NOW.isoformat(), encode_workflow(legacy)))
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-work-latest',?,?,?,?) "
                "ON CONFLICT(namespace,scope,key) DO UPDATE SET claimed_at=excluded.claimed_at,payload_json=excluded.payload_json",
                (WRITER.repository, f"issue:{state.revision.fence.key.target.number}", NOW.isoformat(), encode_workflow(legacy)),
            )
    else:
        assert control.reserve(request).admitted
    if not launch:
        released = control.release_unlaunched(attempt.run_id, "spawn never started")
        assert released.disposition is AccountingDisposition.AVAILABLE
        return ResolveAttemptResult(attempt, ResolveResultKind.STAGE_PROGRESSED, stage=stage)
    if settle:
        settled = control.settle(SettlementRequest(attempt.run_id, Money("USD", 1), PricingBasis.PROVIDER_REPORTED,
                                                   f"codex:{session_id or 'no-session'}:{name}", None, None, None))
        assert settled.disposition is AccountingDisposition.AVAILABLE
    return ResolveAttemptResult(attempt, ResolveResultKind.STAGE_PROGRESSED, stage=stage,
                                session=None if stage is ResolveStage.ROUTE else session)


def paid_return(control, before, *, name="paid-return", stage=ResolveStage.RESOLVE, status="pushed", historical=False):
    paid = paid_result(control, before, name, stage, historical=historical)
    return ReturnedResolve(paid.attempt, stage, ResolveInputs("title", "body", "alice"), False,
                           status, "paid summary", "which API?", "", None, paid.session, NOW)


def unsettled_return(control, before):
    paid = paid_result(control, before, "atomic-return", ResolveStage.RESOLVE, settle=False)
    returned = ReturnedResolve(paid.attempt, paid.stage, ResolveInputs("title", "body", "alice"), False,
                               "pushed", "paid summary", "", "", None, paid.session, NOW)
    assert paid.session is not None
    request = SettlementRequest(paid.attempt.run_id, Money("USD", 1), PricingBasis.PROVIDER_REPORTED,
                                f"codex:{paid.session.session_id}:{paid.attempt.run_id}", None, None, None)
    return returned, request


@pytest.mark.parametrize("basis", [PricingBasis.PROVIDER_REPORTED, PricingBasis.ESTIMATE])
def test_settlement_and_paid_return_gate_commit_and_replay_as_one_authority(control, basis):
    before = initial_snapshot()
    install_workflow(control, before)
    returned, request = unsettled_return(control, before)
    request = replace(request, basis=basis)
    workflow = adapter(control)
    assert workflow.returned_result(returned.attempt) is None
    settled = workflow.settle_return(request, returned)
    assert settled.disposition is AccountingDisposition.AVAILABLE and not settled.idempotent
    restarted = adapter(SQLiteControlStore(control.path, now=lambda: NOW))
    assert restarted.returned_result(returned.attempt) == returned
    assert restarted.pending_returns()[0].returned == returned
    assert restarted.settle_return(request, returned).idempotent
    state = restarted.load_workflow(before.revision.fence.key)
    assert state.episode.continuation.session is None and len(state.gates) == 1
    with control.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "settled"


@pytest.mark.parametrize("table,condition", [("dedupe_keys", "NEW.namespace='workflow-return'"), ("workflow_gates", "1")])
def test_capture_failure_rolls_back_charge_reservation_receipt_and_gate(control, table, condition):
    before = initial_snapshot()
    install_workflow(control, before)
    returned, request = unsettled_return(control, before)
    with control.transaction() as db:
        db.execute(f"CREATE TRIGGER reject_capture BEFORE INSERT ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'capture failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="capture failed"):
        adapter(control).settle_return(request, returned)
    with control.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT state FROM budget_reservations").fetchone()[0] == "active"
        assert db.execute("SELECT state FROM runs").fetchone()[0] != "completed"
        assert db.execute("SELECT COUNT(*) FROM workflow_gates").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='workflow-return'").fetchone()[0] == 0
        db.execute("DROP TRIGGER reject_capture")
    assert adapter(control).settle_return(request, returned).disposition is AccountingDisposition.AVAILABLE


def test_late_return_capture_failure_rolls_back_recovery_correction(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned, request = unsettled_return(control, before)
    with control.transaction() as db:
        db.execute("UPDATE runs SET state='abandoned',outcome='dead_lease',completed_at=? WHERE run_id=?",
                   (NOW.isoformat(), str(request.run_id)))
    assert control.settle_terminal_estimate(request.run_id, "return not yet captured")
    with control.transaction() as db:
        db.execute("CREATE TRIGGER reject_capture BEFORE INSERT ON dedupe_keys WHEN NEW.namespace='workflow-return' "
                   "BEGIN SELECT RAISE(ABORT,'capture failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="capture failed"):
        adapter(control).settle_return(request, returned)
    with control.transaction() as db:
        assert tuple(db.execute("SELECT amount_micros,provider_receipt_id FROM charges").fetchone()) == (0, f"terminal-estimate:{request.run_id}")
        assert tuple(db.execute("SELECT state,outcome FROM runs").fetchone()) == ("abandoned", "dead_lease")
        db.execute("DROP TRIGGER reject_capture")
    assert adapter(control).settle_return(request, returned).disposition is AccountingDisposition.AVAILABLE
    assert adapter(control).returned_result(returned.attempt) == returned


def test_atomic_return_rejects_conflicting_content_and_foreign_lookup_leg(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned, request = unsettled_return(control, before)
    workflow = adapter(control)
    workflow.settle_return(request, returned)
    with pytest.raises(WorkflowAuthorityError, match="different evidence"):
        workflow.settle_return(request, replace(returned, summary="contradictory"))
    assert workflow.returned_result(returned.attempt) == returned
    with pytest.raises(WorkflowAuthorityError, match="disagrees"):
        workflow.returned_result(replace(returned.attempt, leg_id="other-leg"))


def test_settlement_conflict_cannot_capture_a_paid_return(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned, request = unsettled_return(control, before)
    result = adapter(control).settle_return(replace(request, amount=Money("AUD", 1)), returned)
    assert result.disposition is AccountingDisposition.CONFLICT
    assert adapter(control).returned_result(returned.attempt) is None
    assert not adapter(control).load_workflow(before.revision.fence.key).gates


@pytest.mark.parametrize("basis", [PricingBasis.RECONCILED, PricingBasis.PROVIDER_REPORTED])
def test_reconciled_charge_cannot_mint_normal_returned_continuation(control, basis):
    before = initial_snapshot()
    install_workflow(control, before)
    returned, request = unsettled_return(control, before)
    control.settle(replace(request, basis=PricingBasis.RECONCILED))
    if basis is PricingBasis.RECONCILED:
        with pytest.raises(WorkflowAuthorityError, match="settlement"):
            adapter(control).settle_return(replace(request, basis=basis), returned)
    else:
        assert adapter(control).settle_return(replace(request, basis=basis), returned).disposition is AccountingDisposition.CONFLICT
    assert adapter(control).returned_result(returned.attempt) is None
    assert not adapter(control).load_workflow(before.revision.fence.key).gates


def return_observations(returned, **changes):
    fields = dict(number=returned.attempt.binding.fence.key.target.number, title="title", body="body", author="alice",
                  labels=frozenset({Label.AI_WORKING}), updated_at="revision:1")
    fields.update(changes)
    return (ObservedIssue(returned.attempt.binding.fence.key, IssueView(**fields), NOW),
            NoLinkedPr(returned.attempt.binding.fence.key, NOW))


def test_paid_return_capture_survives_restart_without_publishing_pending_session(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    captured = adapter(control).capture_return(returned)
    assert captured.snapshot.episode.continuation.session is None
    assert captured.snapshot.episode.failures == 0
    assert any(g.gate_id == return_gate_id(returned) for g in captured.snapshot.gates)
    restarted = adapter(SQLiteControlStore(control.path, now=lambda: NOW))
    assert restarted.pending_returns()[0].returned == returned
    assert restarted.capture_return(returned).idempotent
    assert isinstance(restarted.capture_return(replace(returned, needs="different")), WorkflowConflict)
    finalized = restarted.finalize_return(returned, *return_observations(returned))
    assert finalized.snapshot.episode.failures == 1
    assert finalized.snapshot.episode.continuation.session == returned.session
    assert finalized.snapshot.gates == ()
    assert restarted.pending_returns() == ()
    assert restarted.finalize_return(returned, *return_observations(returned)).idempotent
    assert len(control.pending_commands(repository="o/r", executor_identity=WRITER.owner_id)) == 1


def test_finalized_blocked_resume_clears_handle_after_raw_capture(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before, stage=ResolveStage.RESUME, status="blocked")
    adapter(control).capture_return(returned)
    finalized = adapter(control).finalize_return(returned, *return_observations(returned))
    assert finalized.snapshot.episode.continuation.session is None
    assert finalized.snapshot.episode.continuation.last_applied_admission_version == 1
    assert finalized.snapshot.episode.failures == 0
    commands = control.pending_commands(repository="o/r", executor_identity=WRITER.owner_id)
    assert len(commands) == 1
    assert commands[0].command.payload["comment"].startswith("**Agent still needs input:**")


def test_return_outbox_failure_rolls_back_classification_gate_and_continuation(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    captured = adapter(control).capture_return(returned)
    with control.transaction() as db:
        db.execute("CREATE TRIGGER fail_return_command BEFORE INSERT ON lifecycle_commands BEGIN SELECT RAISE(ABORT,'outbox failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="outbox failed"):
        adapter(control).finalize_return(returned, *return_observations(returned))
    assert adapter(control).load_workflow(before.revision.fence.key) == captured.snapshot
    assert adapter(control).pending_returns()[0].returned == returned
    with control.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='workflow-result'").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_return_command")
    assert adapter(control).finalize_return(returned, *return_observations(returned)).snapshot.episode.failures == 1


def test_stale_return_is_retired_by_bounded_scan_without_remote_observation(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    adapter(control).capture_return(returned)
    with control.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1,version=version+1")
    assert adapter(control).pending_returns() == ()
    after = adapter(control).load_workflow(before.revision.fence.key)
    assert after.episode.failures == 0 and after.episode.continuation.session is None
    assert after.gates == ()
    assert control.pending_commands(repository="o/r", executor_identity=WRITER.owner_id) == ()


def test_return_backoff_survives_restart_and_retains_original_unresolved_time(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    adapter(control).capture_return(returned)
    adapter(control).defer_return(returned, "linkage lookup unavailable")
    assert adapter(control).pending_returns() == ()
    later = adapter(SQLiteControlStore(control.path, now=lambda: NOW + timedelta(seconds=5)))
    pending = later.pending_returns()[0]
    assert pending.failed_reads == 1 and pending.returned == returned
    cause = later.load_workflow(before.revision.fence.key).gates[0].cause
    assert cause.unresolved_since == NOW and cause.failed_reads == 1


@pytest.mark.parametrize("other_gate", [False, True])
def test_return_finalization_retains_unrelated_or_new_ceiling_hold_without_ready_command(control, other_gate):
    before = initial_snapshot()
    if not other_gate:
        before = replace(before, episode=replace(before.episode, failures=2))
    install_workflow(control, before)
    returned = paid_return(control, before)
    adapter(control).capture_return(returned)
    if other_gate:
        gate = OpenGate("other-hold", before.revision.fence.generation,
                        EvidenceUnavailable("independent fact", NOW, 1, "unavailable"), NoAuthorizedRemediation("observe"))
        with control.transaction() as db:
            db.execute("INSERT INTO workflow_gates VALUES (?,?,?,?,?,?,?,?)",
                       (gate.gate_id, *workflow_scope(before.revision.fence.key), gate.opened_generation, "other", "open", encode_workflow(gate)))
    finalized = adapter(control).finalize_return(returned, *return_observations(returned))
    if other_gate:
        assert isinstance(finalized, WorkflowCommitted)
        assert adapter(control).pending_returns() == ()
        assert len(finalized.snapshot.gates) == 1 and finalized.snapshot.episode.failures == 1
        commands = control.pending_commands(repository="o/r", executor_identity=WRITER.owner_id)
        assert len(commands) == 1
        command = commands[0].command
        with pytest.raises(WorkflowAuthorityError, match="unresolved gates"):
            control.begin_command(command.command_id)
        assert command_row(control, command.command_id)["attempts"] == 0
        assert adapter(control).finalize_return(returned, *return_observations(returned)).idempotent
        assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 1
        with control.transaction() as db:
            db.execute("UPDATE workflow_gates SET state='closed' WHERE gate_id='other-hold'")
            db.execute("UPDATE workflows SET version=version+1")
        control.begin_command(command.command_id)
        assert command_row(control, command.command_id)["state"] == "executing"
        assert command_row(control, command.command_id)["attempts"] == 1
        return
    assert len(finalized.snapshot.gates) == 1
    assert finalized.snapshot.gates[0].gate_id != return_gate_id(returned)
    assert not control.pending_commands(repository="o/r", executor_identity=WRITER.owner_id)


def test_closed_return_keeps_local_cleanup_explicitly_pending(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before, status="done")
    captured = adapter(control).capture_return(returned)
    refused = adapter(control).finalize_return(returned, *return_observations(returned, state="closed"))
    assert isinstance(refused, WorkflowConflict) and "local cleanup" in refused.reason
    assert adapter(control).load_workflow(before.revision.fence.key) == captured.snapshot
    assert adapter(control).pending_returns()[0].returned == returned


def test_stale_generation_cannot_enter_return_local_effect_authority(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before, status="done")
    workflow = adapter(control)
    workflow.capture_return(returned)
    with control.transaction() as db:
        db.execute("UPDATE workflows SET generation=generation+1,version=version+1")
    with pytest.raises(WorkflowAuthorityError, match="current unfinished"):
        with workflow.return_effect_authority(returned):
            pytest.fail("stale return must not execute cleanup")


@pytest.mark.parametrize("replay", ["capture", "finalize"])
def test_corrupt_return_disposition_does_not_silently_hide_pending_recovery(control, replay):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    workflow = adapter(control)
    workflow.capture_return(returned)
    workflow.finalize_return(returned, *return_observations(returned))
    with control.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-return-disposition'")
    with pytest.raises(ValueError):
        if replay == "capture":
            workflow.capture_return(returned)
        else:
            workflow.finalize_return(returned, *return_observations(returned))


@pytest.mark.parametrize("damaged", ["workflow-return", "workflow-return-observation", "missing-observation", "workflow-admission"])
def test_unavailable_return_is_isolated_and_cannot_starve_a_bounded_scan(control, damaged):
    workflow = adapter(control)
    returns = []
    for number in (1, 2):
        before = initial_snapshot(number)
        install_workflow(control, before)
        returned = paid_return(control, before, name=f"return:{number}")
        workflow.capture_return(returned)
        returns.append(returned)
    broken, healthy = returns
    with control.transaction() as db:
        if damaged == "missing-observation":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-return-observation' AND key=?", (str(broken.attempt.run_id),))
        else:
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace=? AND key=?", (damaged, str(broken.attempt.run_id)))
    unavailable = workflow.pending_returns(limit=1)
    assert len(unavailable) == 1
    assert isinstance(unavailable[0], UnavailableResolveReturn)
    assert unavailable[0].record_key == broken.attempt.run_id
    assert unavailable[0].next_read_at == NOW + timedelta(hours=1)
    restarted = adapter(SQLiteControlStore(control.path, now=lambda: NOW))
    available = restarted.pending_returns(limit=1)
    assert len(available) == 1 and isinstance(available[0], PendingResolveReturn)
    assert available[0].returned == healthy
    restarted.finalize_return(healthy, *return_observations(healthy))
    assert restarted.pending_returns() == ()
    held = restarted.load_workflow(broken.attempt.binding.fence.key)
    assert isinstance(held, WorkflowSnapshot) and held.gates
    assert held.episode.failures == 0 and held.episode.continuation.session is None
    later = adapter(SQLiteControlStore(control.path, now=lambda: NOW + timedelta(hours=1)))
    assert isinstance(later.pending_returns()[0], UnavailableResolveReturn)
    with control._connect() as db:
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 2
        if damaged != "missing-observation":
            assert db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace=? AND key=?", (damaged, str(broken.attempt.run_id))).fetchone()[0] == "{}"


@pytest.mark.parametrize("index", ["", "x" * 513, "broken\x00key", "\t"])
def test_malformed_orphan_return_index_cannot_break_isolated_diagnostics(control, index):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    workflow = adapter(control)
    workflow.capture_return(returned)
    with control.transaction() as db:
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('workflow-return', 'o/r', ?, ?, '{}')", (index, (NOW - timedelta(minutes=1)).isoformat()))
    unavailable = workflow.pending_returns(limit=1)
    assert len(unavailable) == 1 and isinstance(unavailable[0], UnavailableResolveReturn)
    assert unavailable[0].record_key.startswith("invalid-index:")
    assert "Invalid paid return index" in unavailable[0].reason
    restarted = adapter(SQLiteControlStore(control.path, now=lambda: NOW))
    pending = restarted.pending_returns(limit=1)
    assert len(pending) == 1 and isinstance(pending[0], PendingResolveReturn)
    assert pending[0].returned == returned


def test_unsettled_return_cannot_publish_session_or_clear_paid_admission_hold(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    with control.transaction() as db:
        db.execute("UPDATE budget_reservations SET state='active'")
    with pytest.raises(WorkflowAuthorityError, match="settlement"):
        adapter(control).capture_return(returned)
    assert adapter(control).pending_returns() == ()
    assert adapter(control).load_workflow(before.revision.fence.key).episode.continuation.session is None


def test_captured_return_cannot_bypass_observation_by_direct_final_result_write(control):
    before = initial_snapshot()
    install_workflow(control, before)
    returned = paid_return(control, before)
    captured = adapter(control).capture_return(returned)
    result = ResolveAttemptResult(returned.attempt, ResolveResultKind.COUNTED_REQUEUE, stage=returned.stage, session=returned.session)
    assert isinstance(adapter(control).record_attempt(result), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key) == captured.snapshot
    assert adapter(control).pending_returns()[0].returned == returned


def test_unlaunched_reservation_cannot_mint_returned_progress(control):
    before = initial_snapshot()
    install_workflow(control, before)
    unlaunched = paid_result(control, before, "never-started", ResolveStage.RESOLVE, launch=False)
    returned = ReturnedResolve(unlaunched.attempt, ResolveStage.RESOLVE, ResolveInputs("title", "body", "alice"),
                               False, "pushed", "", "", "", None, None, NOW)
    with pytest.raises(WorkflowAuthorityError, match="settlement"):
        adapter(control).capture_return(returned)
    assert adapter(control).pending_returns() == ()


@pytest.mark.parametrize("corruption", [
    "missing_admission", "wrong_admission_scope", "malformed_admission", "admission_run", "admission_stage",
    "future_admission", "run_stage", "dispatch_stage", "dispatch_runtime", "unknown_receipt", "recovered_receipt",
    "different_session", "different_model", "missing_reservation", "foreign_workload",
])
def test_unproven_continuation_is_rejected_without_audit_or_episode_mutation(control, corruption):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "author", ResolveStage.RESOLVE)
    admitted = adapter(control).load_workflow(before.revision.fence.key)
    with control.transaction() as db:
        if corruption == "missing_admission":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='workflow-admission'")
        elif corruption == "wrong_admission_scope":
            db.execute("UPDATE dedupe_keys SET scope='foreign/repo' WHERE namespace='workflow-admission'")
        elif corruption == "malformed_admission":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='workflow-admission'")
        elif corruption in ("admission_run", "admission_stage", "future_admission"):
            receipt = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='workflow-admission'").fetchone()
            proof = decode_workflow(receipt[0], ResolveAdmission)
            if corruption == "admission_run":
                proof = replace(proof, attempt=replace(proof.attempt, run_id=RunId("wrong-run")))
            elif corruption == "admission_stage":
                proof = replace(proof, stage=ResolveStage.RESUME)
            else:
                proof = replace(proof, revision=replace(proof.revision, version=admitted.revision.version + 1))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='workflow-admission'", (encode_workflow(proof),))
        elif corruption == "run_stage":
            db.execute("UPDATE runs SET kind='RESUME'")
        elif corruption in ("dispatch_stage", "dispatch_runtime"):
            metadata = json.loads(db.execute("SELECT dispatch_id FROM runs").fetchone()[0])
            metadata["kind" if corruption == "dispatch_stage" else "runtime"] = "wrong"
            db.execute("UPDATE runs SET dispatch_id=?", (json.dumps(metadata),))
        elif corruption in ("unknown_receipt", "recovered_receipt", "different_session"):
            receipt = {"unknown_receipt": None, "recovered_receipt": "recovered:author", "different_session": "codex:other:author"}[corruption]
            db.execute("UPDATE runs SET provider_receipt_id=?", (receipt,))
        elif corruption == "different_model":
            db.execute("UPDATE budget_reservations SET model='another-model'")
        elif corruption == "foreign_workload":
            db.execute("UPDATE budget_reservations SET workload_repository='foreign/repo'")
        else:
            db.execute("DELETE FROM reservation_limits")
            db.execute("DELETE FROM budget_reservations")
    if corruption == "future_admission":
        assert isinstance(adapter(control).record_attempt(result), WorkflowConflict)
    else:
        with pytest.raises((WorkflowAuthorityError, ValueError)):
            adapter(control).record_attempt(result)
    assert adapter(control).load_workflow(before.revision.fence.key) == admitted
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='workflow-result'").fetchone()[0] == 0


def test_result_proposal_uses_durable_admission_not_a_forged_newer_order(control):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "old", ResolveStage.RESOLVE, historical=True)
    latest_result = paid_result(control, adapter(control).load_workflow(before.revision.fence.key), "new", ResolveStage.RESOLVE, historical=True)
    latest = adapter(control).record_attempt(latest_result).snapshot
    forged_proof = ResolveAdmission(result.attempt, latest.revision, result.stage, input_fingerprint="a" * 64)
    forged = record_resolve_attempt(latest, result, ceiling=3, admission=forged_proof)
    assert isinstance(forged, WorkflowChanged)
    with pytest.raises(ValueError, match="authorized state transition|pure transition"):
        commit(control, forged)
    assert adapter(control).load_workflow(before.revision.fence.key) == latest


def test_continuation_and_result_audit_roll_back_together(control):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "author", ResolveStage.RESOLVE)
    admitted = adapter(control).load_workflow(before.revision.fence.key)
    with control.transaction() as db:
        db.execute("CREATE TRIGGER fail_classification BEFORE INSERT ON dedupe_keys WHEN NEW.namespace='workflow-result' BEGIN SELECT RAISE(ABORT, 'classification crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="classification crash"):
        adapter(control).record_attempt(result)
    assert adapter(control).load_workflow(before.revision.fence.key) == admitted


@pytest.mark.parametrize("kind", [ResolveResultKind.STAGE_PROGRESSED, ResolveResultKind.COUNTED_REQUEUE,
                                  ResolveResultKind.BLOCKED, ResolveResultKind.PR_OPENED])
def test_actual_unlaunched_resolve_cannot_claim_progress_or_a_strike(control, kind):
    before = initial_snapshot()
    install_workflow(control, before)
    unlaunched = paid_result(control, before, "unlaunched", ResolveStage.RESOLVE, launch=False)
    admitted = adapter(control).load_workflow(before.revision.fence.key)
    with pytest.raises(WorkflowAuthorityError, match="settlement provenance"):
        adapter(control).record_attempt(replace(unlaunched, kind=kind))
    assert adapter(control).load_workflow(before.revision.fence.key) == admitted
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='workflow-result'").fetchone()[0] == 0


@pytest.mark.parametrize("kind", [ResolveResultKind.INFRA_FAILURE, ResolveResultKind.BUDGET_REFUSAL])
def test_unlaunched_infrastructure_or_budget_result_remains_auditable_without_settlement(control, kind):
    before = initial_snapshot()
    install_workflow(control, before)
    result = replace(paid_result(control, before, "unlaunched", ResolveStage.RESOLVE, launch=False), kind=kind)
    admitted = adapter(control).load_workflow(before.revision.fence.key)
    recorded = adapter(control).record_attempt(result)
    assert recorded.snapshot == admitted
    assert adapter(control).record_attempt(result).idempotent


@pytest.mark.parametrize("corruption", ["cancelled", "abandoned", "released", "reconciled", "missing_charge", "foreign_charge"])
def test_sessionless_resolve_needs_normal_completed_settlement(control, corruption):
    before = initial_snapshot()
    install_workflow(control, before)
    result = paid_result(control, before, "author", ResolveStage.RESOLVE, session_id="")
    with control.transaction() as db:
        if corruption in ("cancelled", "abandoned"):
            db.execute("UPDATE runs SET state=?", (corruption,))
        elif corruption == "released":
            db.execute("UPDATE budget_reservations SET state='released'")
        elif corruption == "reconciled":
            db.execute("UPDATE charges SET basis='reconciled'")
        elif corruption == "missing_charge":
            db.execute("DELETE FROM charges")
        else:
            db.execute("UPDATE charges SET provider_receipt_id='foreign:receipt'")
    with pytest.raises(WorkflowAuthorityError, match="settlement provenance"):
        adapter(control).record_attempt(result)
    assert isinstance(adapter(control).load_workflow(before.revision.fence.key).episode.continuation, FreshResolve)


def test_read_requires_activation_and_distinguishes_verified_absence(control):
    key = resolve_snapshot().revision.fence.key
    assert adapter(control).load_workflow(key) == WorkflowMigrationRequired(key)
    install_workflow(control, resolve_snapshot())
    assert isinstance(adapter(control).load_workflow(WorkflowKey("o/r", IssueRef(999))), WorkflowAbsent)
    with pytest.raises(WorkflowAuthorityError, match="scope"):
        adapter(control).load_workflow(WorkflowKey("other/repo", IssueRef(1)))


def test_discovery_requires_explicit_activation_before_creating_authority(control):
    with pytest.raises(WorkflowAuthorityError, match="migration"):
        adapter(control).ensure_observed_workflow(observation(resolve_snapshot()))
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0


def test_new_issue_discovery_is_durable_and_repeated_observation_never_resets_it(control):
    install_workflow(control, resolve_snapshot())
    observed = observation(resolve_snapshot(2))
    first = adapter(control).ensure_observed_workflow(observed)
    assert not first.idempotent
    assert first.snapshot.revision.fence.generation == first.snapshot.revision.version == 0
    assert first.snapshot.episode.failures == 0
    later = replace(observed, observed_at=NOW + timedelta(seconds=10))
    control._now = lambda: later.observed_at
    duplicate = adapter(control).ensure_observed_workflow(later)
    assert duplicate.idempotent and duplicate.snapshot == first.snapshot
    with closing(control._connect()) as db:
        rows = db.execute("SELECT payload_json FROM workflow_evidence WHERE target_number=2").fetchall()
    assert len(rows) == 1
    assert decode_workflow(rows[0]["payload_json"], ObservedIssue) == observed


def test_discovery_of_existing_exhausted_episode_preserves_gates_and_guidance(control):
    before = held()
    before = replace(before, episode=replace(before.episode, guidance="original decision", last_failure="prior failure"))
    install_workflow(control, before)
    result = adapter(control).ensure_observed_workflow(observation(before, frozenset({Label.AI_READY})))
    assert result.idempotent and result.snapshot == before
    assert adapter(control).held_projection(before.revision.fence.key).held


def test_concurrent_first_observations_create_one_episode_and_audit(control):
    install_workflow(control, resolve_snapshot())
    observed = observation(resolve_snapshot(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adapter(control).ensure_observed_workflow(observed), range(2)))
    assert sum(not r.idempotent for r in results) == 1
    assert results[0].snapshot == results[1].snapshot
    with closing(control._connect()) as db:
        assert db.execute("SELECT count(*) FROM workflow_evidence WHERE target_number=2").fetchone()[0] == 1


def test_discovery_failure_rolls_back_initial_episode_and_audit(control):
    install_workflow(control, resolve_snapshot())
    observed = observation(resolve_snapshot(2))
    with control.transaction() as db:
        db.execute("CREATE TRIGGER fail_discovery BEFORE INSERT ON workflow_evidence BEGIN SELECT RAISE(ABORT, 'discovery crash'); END")
    with pytest.raises(sqlite3.IntegrityError, match="discovery crash"):
        adapter(control).ensure_observed_workflow(observed)
    assert isinstance(adapter(control).load_workflow(observed.key), WorkflowAbsent)


@pytest.mark.parametrize("existing", [False, True])
def test_corrupt_activation_cannot_create_or_reset_a_discovered_workflow(control, existing):
    install_workflow(control, resolve_snapshot())
    observed = observation(resolve_snapshot(1 if existing else 2))
    with control.transaction() as db:
        db.execute("UPDATE workflow_authority_epochs SET epoch='corrupt'")
    with pytest.raises(WorkflowAuthorityError, match="epoch"):
        adapter(control).ensure_observed_workflow(observed)


def test_corrupt_existing_workflow_is_not_treated_as_undiscovered(control):
    install_workflow(control, resolve_snapshot())
    with control.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='{}'")
    with pytest.raises(WorkflowAuthorityError, match="invalid"):
        adapter(control).ensure_observed_workflow(observation(resolve_snapshot()))


@pytest.mark.parametrize("offset", [-1, 301])
def test_discovery_rechecks_observation_time_under_transaction(control, offset):
    install_workflow(control, resolve_snapshot())
    control._now = lambda: NOW + timedelta(seconds=offset)
    with pytest.raises(ValueError, match="stale or from the future"):
        adapter(control).ensure_observed_workflow(observation(resolve_snapshot(2)))


def test_discovery_checks_bound_owner_and_repository(control):
    install_workflow(control, resolve_snapshot())
    foreign = replace(observation(resolve_snapshot(2)), key=WorkflowKey("foreign/repo", IssueRef(2)))
    with pytest.raises(WorkflowAuthorityError, match="scope"):
        adapter(control).ensure_observed_workflow(foreign)
    control.adopt_legacy_owner("o/r", "fix", "o/r:fix:new", prior_owner_ids=(WRITER.owner_id,))
    with pytest.raises(RetiredOwnerError):
        adapter(control).ensure_observed_workflow(observation(resolve_snapshot(2)))


def test_discovery_ignores_stale_legacy_files_after_activation(control):
    from pathlib import Path
    install_workflow(control, resolve_snapshot())
    Path(control.path).parent.joinpath("issue-2.resolve-retries").write_text("999", encoding="utf-8")
    result = adapter(control).ensure_observed_workflow(observation(resolve_snapshot(2)))
    assert result.snapshot.episode.failures == 0


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
        now=later, active_attempts=(), workspace=workspace_observation(before.binding, later),
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
    with pytest.raises(WorkflowAuthorityError, match="requires drained"):
        commit(control, request(before))
    assert adapter(control).load_workflow(before.revision.fence.key) == before


def test_result_exact_run_dedup_and_leg_identity_prevent_double_charge(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    result = run(control, before)
    proposal = record_resolve_attempt(before, result, ceiling=3, admission=admission(before, result))
    assert commit(control, proposal).snapshot.episode.failures == 1
    assert commit(control, proposal).idempotent
    after = adapter(control).load_workflow(before.revision.fence.key)
    forged = replace(result, attempt=replace(result.attempt, leg_id="another-leg"))
    assert isinstance(commit(control, record_resolve_attempt(after, forged, ceiling=3, admission=admission(after, forged))), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 1


def test_unadmitted_or_wrong_leg_result_cannot_consume_allowance(control):
    before = resolve_snapshot()
    install_workflow(control, before)
    result = ResolveAttemptResult(WorkflowAttemptIdentity(before.binding, RunId("missing"), "leg"), ResolveResultKind.COUNTED_REQUEUE)
    with pytest.raises(WorkflowAuthorityError, match="admitted"):
        commit(control, record_resolve_attempt(before, result, ceiling=3, admission=admission(before, result)))
    result = run(control, before)
    forged = replace(result, attempt=replace(result.attempt, leg_id="forged-leg"))
    with pytest.raises(WorkflowAuthorityError, match="disagrees"):
        commit(control, record_resolve_attempt(before, forged, ceiling=3, admission=admission(before, forged)))


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
    with pytest.raises((ControlStoreError, WorkflowAuthorityError), match="retired|pending authorized"):
        control.begin_command(proposal.commands[0].command_id)
    assert command_row(control, proposal.commands[0].command_id)["attempts"] == 0
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
    with pytest.raises(ControlStoreError, match="awaits verified workspace"):
        control.begin_command(command_id)
    assert command_row(control, command_id)["attempts"] == 0
    prepare_workspace(control, pending)
    control.begin_command(command_id)
    receipt = EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME, error="connection lost")
    control.record_effect(receipt)
    command = CancelRearm("cancel", pending.revision, "request:1", "operator cancelled")
    with pytest.raises(WorkflowAuthorityError, match="uncertainty gate"):
        commit(control, cancel_resolve_rearm(pending, command, ACTOR))
    # A fabricated read count cannot replace the history persisted with the outcome.
    forged = cancel_resolve_rearm(pending, command, ACTOR, uncertainty=(RemoteOutcomeUncertain(command_id, NOW, 3, "read unavailable"),))
    with pytest.raises(WorkflowAuthorityError, match="cannot reset"):
        commit(control, forged)
    stopped = cancel_resolve_rearm(pending, command, ACTOR, uncertainty=(RemoteOutcomeUncertain(command_id, NOW, 0, "connection lost"),))
    state = commit(control, stopped).snapshot
    assert state.episode == pending.episode
    assert control.effect_receipt(command_id) == receipt
    retired = decode_workflow(command_row(control, command_id)["workflow_reconciliation_json"], RetiredUncertainCommand)
    assert retired.uncertainty.history.unresolved_since == NOW and retired.uncertainty.history.failed_reads == 0
    assert adapter(control).held_projection(before.revision.fence.key).held
    with pytest.raises((ControlStoreError, WorkflowAuthorityError), match="retired|pending authorized"):
        control.begin_command(command_id)


def test_completion_requires_durable_evidence_and_success_receipt(control):
    before = held()
    install_workflow(control, before)
    proposal = request(before)
    pending = commit(control, proposal).snapshot
    command_id = proposal.commands[0].command_id
    workspace = prepare_workspace(control, pending)
    complete = complete_resolve_rearm(
        pending, observation(pending, frozenset({Label.AI_READY})), NoLinkedPr(pending.revision.fence.key, NOW),
        verified_commands=(command_id,), evidence_ids=("observation:1",), now=NOW,
        workspace=workspace,
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
    attempted = run(control, before)
    proposal = record_resolve_attempt(before, attempted, ceiling=3, admission=admission(before, attempted))
    assert isinstance(proposal, WorkflowChanged) and isinstance(proposal.snapshot, WorkflowSnapshot)
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
    workspace = prepare_workspace(control, pending)
    control.begin_command(command_id)
    control.record_effect(EffectReceipt(command_id, WRITER.owner_id, NOW, NOW, EffectStatus.SUCCEEDED))
    complete = complete_resolve_rearm(
        pending, observation(pending, frozenset({Label.AI_READY})), NoLinkedPr(pending.revision.fence.key, NOW),
        verified_commands=(command_id,), evidence_ids=("observation:1",), now=NOW,
        workspace=workspace,
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
    with pytest.raises(ValueError, match="linked PR or changed holds"):
        commit(control, proposal)
    # A requirements edit is not a hold fact (ADR 0015 semantic fence).
    edited = replace(observation(pending), issue=replace(observation(pending).issue, body="new requirements"))
    evidence(control, pending, "edited", edited)
    with pytest.raises(ValueError, match="linked PR or changed holds"):
        commit(control, supersede_resolve_rearm(pending, StopReason.DECISION_CHANGED, "edited"))
    held_now = ResolveRearmPreflight(observation(pending), NoLinkedPr(pending.revision.fence.key, NOW), ("ai-parked",))
    evidence(control, pending, "changed", held_now)
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
    prepare_workspace(control, pending)
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
    assert recorded.snapshot.episode.failures == before.episode.failures
    assert recorded.snapshot.episode.continuation.session is None
    assert adapter(control).record_attempt(result).idempotent
    assert isinstance(adapter(control).record_attempt(admitted), WorkflowConflict)
    assert isinstance(commit(control, record_resolve_attempt(before, admitted, ceiling=3, admission=admission(before, admitted))), WorkflowConflict)
    assert adapter(control).load_workflow(before.revision.fence.key).episode.failures == 0


def test_old_generation_result_is_audited_but_never_charges_renewed_allowance(control):
    before = held()
    install_workflow(control, before)
    late = run(control, before)
    current = commit(control, request(
        before,
        predecessor=PriorResolveAdmission(late.attempt, admission(before, late).revision.version),
    )).snapshot
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
    replay = commit(control, record_resolve_attempt(before, result, ceiling=3, admission=admission(before, result)))
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
    workspace = prepare_workspace(control, pending)
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
            evidence_ids=("observation:1",), now=NOW, workspace=workspace)
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
    workspace = prepare_workspace(control, pending)
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
            evidence_ids=("observation:1",), now=NOW, workspace=workspace)
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
    prepare_workspace(control, pending)
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


@pytest.mark.parametrize("shape", ["plain", "family"])
def test_v45_turns_an_authored_checkpoint_into_a_fresh_continuation(control, shape):
    """An authored split checkpoint survives schema 45 as a fresh continuation at
    the same applied watermark, in both stored workflow shapes: the plain
    (authority, episode) tuple and the IssueFamilyEpisodes envelope."""
    from issue_agent.domain.workflow import FreshResolve
    from tests.adapters.test_control_store import _historical_fixture

    before = initial_snapshot()
    install_workflow(control, before)
    watermark = before.revision.version + 1  # an authored checkpoint follows its admission
    authored = ("json_object('type','AuthoredResolve','fields',json_object("
                "'author_admission',json_object('retired',1),"
                f"'last_applied_admission_version',{watermark},"
                "'session',json('null'),'split',json('null'),'split_applied_admission_version',0))")
    with sqlite3.connect(control.path) as db:
        db.execute(f"UPDATE workflows SET version=?,episode_json=json_set(episode_json,"
                   f"'$.value[1].fields.continuation',{authored})", (watermark,))
        if shape == "family":
            db.execute("UPDATE workflows SET episode_json=json_object('format',1,'value',json_object("
                       "'fields',json_object('authority',json_extract(episode_json,'$.value[0]'),"
                       "'episode',json_extract(episode_json,'$.value[1]'),"
                       "'issue_recovery',json_object('fields',json_object(),'type','InactiveIssueRecovery')),"
                       "'type','IssueFamilyEpisodes'))")
        _historical_fixture(db, 44)

    SQLiteControlStore(control.path, now=lambda: NOW).initialize()

    loaded = adapter(SQLiteControlStore(control.path, now=lambda: NOW)).load_workflow(before.revision.fence.key)
    assert loaded == replace(before, revision=replace(before.revision, version=watermark),
                             episode=replace(before.episode, continuation=FreshResolve(None, watermark)))
    with closing(sqlite3.connect(control.path)) as db:
        assert "AuthoredResolve" not in db.execute("SELECT episode_json FROM workflows").fetchone()[0]


def test_v45_retires_split_records_and_keeps_resolve_records_byte_canonical(control):
    """Schema 45 forgets the deleted author/implement/adjudicate split by
    migration: split-only rows go, the always-encoded split fields leave every
    surviving resolve record, and what remains still re-encodes byte-for-byte
    (the codec compares stored payloads against a fresh encoding)."""
    from tests.adapters.test_control_store import _historical_fixture

    before = initial_snapshot()
    install_workflow(control, before)
    returned, request = unsettled_return(control, before)
    assert adapter(control).settle_return(request, returned).disposition is AccountingDisposition.AVAILABLE
    settled_state = adapter(control).load_workflow(before.revision.fence.key)
    with sqlite3.connect(control.path) as db:
        db.execute("UPDATE dedupe_keys SET payload_json=json_set(payload_json,"
                   "'$.value.fields.adjudication_ceiling',json('null'),'$.value.fields.split_fingerprint',json('null')) "
                   "WHERE namespace IN ('workflow-admission','workflow-work-latest')")
        for prefix, namespace in (("$.value.fields.", "workflow-return"),
                                  ("$.value.fields.returned.fields.", "workflow-return-observation")):
            db.execute(f"UPDATE dedupe_keys SET payload_json=json_set(payload_json,"
                       f"'{prefix}split_input',json('null'),'{prefix}verdict','','{prefix}rationale','',"
                       f"'{prefix}contract_revision',json('null')) WHERE namespace=?", (namespace,))
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                   "VALUES ('workflow-split-consumed','o/r','spent',?,'{}')", (NOW.isoformat(),))
        _historical_fixture(db, 44)

    SQLiteControlStore(control.path, now=lambda: NOW).initialize()

    restarted = adapter(SQLiteControlStore(control.path, now=lambda: NOW))
    assert restarted.load_workflow(before.revision.fence.key) == settled_state
    assert restarted.returned_result(returned.attempt) == returned
    assert [pending.returned for pending in restarted.pending_returns()] == [returned]
    with closing(sqlite3.connect(control.path)) as db:
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='workflow-split-consumed'").fetchone()[0] == 0
        assert "split" not in "".join(row[0] for row in db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace LIKE 'workflow-%'"))
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='runs_adjudication_episode'").fetchone()[0] == 0


@pytest.mark.parametrize("split_stage", [None, "AUTHOR", "IMPLEMENT", "ADJUDICATE"])
def test_v45_split_history_still_admits_the_next_resolve(control, split_stage):
    """An issue whose latest work admission was a split leg is not wedged by
    schema 45: the split run and every receipt recording its stage become
    RESOLVE, so the latest-admission chain stays valid, the next RESOLVE is
    admitted, and the old leg still counts as a resolve attempt for backoff."""
    from issue_agent.domain.actions import AgentKind
    from tests.adapters.test_control_store import _historical_fixture

    before = initial_snapshot()
    install_workflow(control, before)
    paid_result(control, before, "run-a", ResolveStage.RESOLVE)
    with closing(sqlite3.connect(control.path)) as db, db:
        if split_stage is not None:  # the rows a split leg left at schema 44
            db.execute("UPDATE runs SET kind=?,dispatch_id=json_set(dispatch_id,'$.kind',?) WHERE run_id='run-a'",
                       (split_stage, split_stage))
            db.execute("UPDATE dedupe_keys SET payload_json=json_set(payload_json,'$.value.fields.stage.value',?) "
                       "WHERE namespace IN ('workflow-admission','workflow-work-latest')", (split_stage,))
        _historical_fixture(db, 44)
    store = SQLiteControlStore(control.path, now=lambda: NOW)
    store.initialize()

    state = adapter(store).load_workflow(before.revision.fence.key)
    paid_result(store, state, "run-b", ResolveStage.RESOLVE)
    assert [end.kind for end in store.fix_leg_ends("o/r", before.revision.fence.key.target.number)] == [
        AgentKind.RESOLVE, AgentKind.RESOLVE]
