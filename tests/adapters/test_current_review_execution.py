"""Current authority construction, exact receipts and caught-error atomicity."""

from dataclasses import replace
import json
from itertools import count
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.workflow_codec import WorkflowEncodingError, decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import (
    REVIEW_EXECUTION_ORIGIN_NAMESPACE, REVIEW_OUTCOME_NAMESPACE,
    WorkflowAuthorityError, load_review_workflow_row,
    persist_workflow_snapshot, read_current_review_pending,
)
from issue_agent.domain.events import ControlTransitionCommitted, EventEnvelope, Producer, new_event_id
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.review_execution import (
    CurrentReviewExecution, ReviewExecutionCheckpoint, ImportedReviewSession,
    ReusableReviewSession, NoReviewSession, advance_current_review_generation,
)
from issue_agent.domain.workflow import WorkflowMutation
from issue_agent.domain.workflow_identity import WorkflowRevision
from issue_agent.domain.review_workflow import FreshReviewRequired
from issue_agent.domain.review_rounds import CurrentReviewOutcomeReceipt
from tests.adapters.test_control_store import _historical_fixture
from tests.adapters.test_review_execution_control import OWNER, rig


@pytest.fixture
def current_rig(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    facts = control.read_round_facts(plan.round.round_id)
    key = selection.expected.fence.key
    with store.transaction() as db:
        current = load_review_workflow_row(db, key)
    published = []
    sequence = count()
    producer = Producer("construction", "o/r", "review", 123, "boot")

    def factory(payload, correlation):
        index = next(sequence)
        return EventEnvelope(payload, new_event_id(store.now()), index, store.now(), store.now(),
                             index, producer, correlation)

    store.bind_event_outbox(factory, published.append)
    return store, key, control, facts, current, published


def test_current_wire_format_is_explicit_and_has_no_ancestry_defaults(current_rig):
    _, _, _, _, snapshot, _ = current_rig
    encoded = encode_workflow(snapshot.execution)
    assert json.loads(encoded)["value"]["type"] == "CurrentReviewExecutionV1"
    assert decode_workflow(encoded, CurrentReviewExecution) == snapshot.execution
    payload = json.loads(encoded)
    del payload["value"]["fields"]["generation_start_sequence"]
    with pytest.raises(WorkflowEncodingError, match="wrong fields"):
        decode_workflow(json.dumps(payload), CurrentReviewExecution)


def test_current_outcome_commits_receipt_projection_and_event_together(current_rig):
    store, key, control, facts, before, published = current_rig
    with store.transaction() as db:
        prior_events = db.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
        result = control._write_current_outcome_in_tx(db, facts, before.revision)
        after = load_review_workflow_row(db, key)
        assert after.execution.baseline.through_sequence == facts.plan.round.sequence
        assert after.execution.state == before.execution.state
        assert after.revision.version == before.revision.version + 1
        assert read_current_review_pending(db, after.execution) == ()
        assert db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
            (REVIEW_OUTCOME_NAMESPACE, key.repository, facts.plan.round.round_id),
        ).fetchone()[0] == encode_workflow(CurrentReviewOutcomeReceipt(facts, result.outcome, before.revision, OWNER))
        assert published == []
        assert db.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0] == prior_events + 1
    assert len(published) == 1
    assert isinstance(published[0].payload, ControlTransitionCommitted)
    assert published[0].payload.entity_id == facts.plan.round.round_id


@pytest.mark.parametrize("stage", ["receipt", "state", "event"])
def test_caught_current_write_failure_rolls_back_all_halves_and_keeps_outer_work(current_rig, stage):
    store, key, control, facts, before, published = current_rig
    table, operation, condition = {
        "receipt": ("dedupe_keys", "INSERT", f"WHEN NEW.namespace='{REVIEW_OUTCOME_NAMESPACE}'"),
        "state": ("workflows", "UPDATE", ""),
        "event": ("event_outbox", "INSERT", ""),
    }[stage]
    with store.transaction() as db:
        prior_events = db.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0]
        db.execute(f"CREATE TEMP TRIGGER inject_failure BEFORE {operation} ON {table} {condition} "
                   "BEGIN SELECT RAISE(ABORT,'injected current write failure'); END")
        db.execute("INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) VALUES (?,?,?,?,?)",
                   ("test-outer", "o/r", "before", "{}", store.now().isoformat()))
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            control._write_current_outcome_in_tx(db, facts, before.revision)
        assert load_review_workflow_row(db, key) == before
        assert db.execute("SELECT closure_json,outcome_json FROM review_execution_rounds WHERE round_id=?",
                          (facts.plan.round.round_id,)).fetchone()[:] == (None, None)
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace=?",
                          (REVIEW_OUTCOME_NAMESPACE,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0] == prior_events
        db.execute("INSERT INTO level_state(namespace,scope,key,payload_json,updated_at) VALUES (?,?,?,?,?)",
                   ("test-outer", "o/r", "after", "{}", store.now().isoformat()))
    with store.transaction() as db:
        assert db.execute("SELECT key FROM level_state WHERE namespace='test-outer' ORDER BY key").fetchall()[0][0] == "after"
        assert db.execute("SELECT COUNT(*) FROM level_state WHERE namespace='test-outer'").fetchone()[0] == 2
        assert load_review_workflow_row(db, key) == before
    assert published == []


def test_current_exact_retry_precedes_unavailable_current_authority_and_conflicts_refuse(current_rig):
    store, key, control, facts, before, published = current_rig
    with store.transaction() as db:
        accepted = control._write_current_outcome_in_tx(db, facts, before.revision)
    with store.transaction() as db:
        db.execute("UPDATE workflows SET episode_json='malformed' WHERE target_kind='pr'")
        assert control._write_current_outcome_in_tx(db, facts, before.revision) == accepted
        with pytest.raises(WorkflowAuthorityError, match="conflicts"):
            control._write_current_outcome_in_tx(db, replace(facts, closure_receipt="different"), before.revision)
        with pytest.raises(WorkflowAuthorityError, match="conflicts"):
            control._write_current_outcome_in_tx(db, facts, replace(before.revision, version=before.revision.version + 1))
    assert len(published) == 1


@pytest.mark.parametrize("field,value", [
    ("reusable_after", "2026-09-30T00:00:00+00:00"), ("claimed_at", "malformed"),
    ("claimed_at", "2026-09-30T00:00:00"), ("payload_json", b"{}"),
])
def test_current_retry_requires_the_same_immutable_receipt_contract_as_active_reads(current_rig, field, value):
    store, _, control, facts, before, published = current_rig
    with store.transaction() as db:
        control._write_current_outcome_in_tx(db, facts, before.revision)
    with store.transaction() as db:
        db.execute(f"UPDATE dedupe_keys SET {field}=? WHERE namespace=? AND scope=? AND key=?",
                   (value, REVIEW_OUTCOME_NAMESPACE, "o/r", facts.plan.round.round_id))
        with pytest.raises(WorkflowAuthorityError, match="receipt"):
            control._write_current_outcome_in_tx(db, facts, before.revision)
    assert len(published) == 1


def test_current_imported_session_requires_its_actual_origin_after_baseline_advances(current_rig):
    store, key, control, facts, before, _ = current_rig
    with store.transaction() as db:
        control._write_current_outcome_in_tx(db, facts, before.revision)
        completed = load_review_workflow_row(db, key)
        invented = ReusableReviewSession(RuntimeName.CLAUDE, "invented-session", ImportedReviewSession("invented-evidence"))
        state = replace(completed.execution.state, session=invented)
        invalid = replace(completed.execution, state=state, baseline=replace(completed.execution.baseline, state=state))
        persist_workflow_snapshot(db, replace(completed, execution=invalid))
        with pytest.raises(WorkflowAuthorityError, match="verified origin"):
            load_review_workflow_row(db, key)


@pytest.mark.parametrize("session", [NoReviewSession(), ReusableReviewSession(
    RuntimeName.CLAUDE, "invented-session", ImportedReviewSession("invented-evidence")),
])
def test_current_generation_carry_requires_the_transition_session_clear_and_cutoff(current_rig, session):
    store, key, control, facts, before, _ = current_rig
    with store.transaction() as db:
        control._write_current_outcome_in_tx(db, facts, before.revision)
        completed = load_review_workflow_row(db, key)
        next_revision = WorkflowRevision(replace(completed.revision.fence, generation=completed.revision.fence.generation + 1),
                                         completed.revision.version + 1)
        execution, boundary = advance_current_review_generation(
            completed.execution, (), completed.revision, next_revision, WorkflowMutation.REQUEST_REARM,
        )
        db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
                   ("review-execution-generation", "o/r", boundary.receipt_id, store.now().isoformat(), encode_workflow(boundary)))
        advanced = replace(completed, revision=next_revision, execution=execution,
                           episode=replace(completed.episode, continuation=FreshReviewRequired(next_revision.fence.generation)))
        persist_workflow_snapshot(db, advanced)
        assert load_review_workflow_row(db, key) == advanced
        state = replace(execution.state, session=session, session_sequence=0)
        invalid = replace(execution, state=state, baseline=replace(execution.baseline, state=state))
        persist_workflow_snapshot(db, replace(advanced, execution=invalid))
        with pytest.raises(WorkflowAuthorityError, match="generation transition"):
            load_review_workflow_row(db, key)


@pytest.mark.parametrize("corruption", ["origin", "component", "malformed", "high_water", "pending_identity"])
def test_current_active_authority_and_required_references_fail_closed(current_rig, corruption):
    store, key, _, facts, before, _ = current_rig
    with store.transaction() as db:
        if corruption == "origin":
            db.execute("DELETE FROM dedupe_keys WHERE namespace=?", (REVIEW_EXECUTION_ORIGIN_NAMESPACE,))
        elif corruption == "component":
            db.execute("DELETE FROM workflow_components WHERE family='review_execution'")
        elif corruption == "malformed":
            db.execute("UPDATE workflows SET episode_json='malformed' WHERE target_kind='pr'")
        elif corruption == "high_water":
            persist_workflow_snapshot(db, replace(before, execution=replace(before.execution, last_sequence=2)))
        else:
            plan = replace(facts.plan, round=replace(facts.plan.round, round_id="foreign"))
            db.execute("UPDATE review_execution_rounds SET plan_json=? WHERE round_id=?",
                       (encode_workflow(plan), facts.plan.round.round_id))
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, key)


def test_current_writer_refuses_unbacked_source_facts_and_missing_event_outbox(current_rig):
    store, key, control, facts, before, _ = current_rig
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError, match="actual durable"):
            control._write_current_outcome_in_tx(db, replace(facts, closure_receipt="invented"), before.revision)
        store._event_factory = None
        with pytest.raises(WorkflowAuthorityError, match="durable event"):
            control._write_current_outcome_in_tx(db, facts, before.revision)
        assert load_review_workflow_row(db, key) == before


def test_pending_selection_uses_generation_range_index_and_canonical_current_authority(current_rig):
    store, key, _, facts, before, _ = current_rig
    with store.transaction() as db:
        statements = []
        db.set_trace_callback(statements.append)
        pending = read_current_review_pending(db, before.execution)
        db.set_trace_callback(None)
        assert tuple(item.round for item in pending) == (facts.plan.round,)
        selected = next(statement for statement in statements
                        if "FROM review_execution_rounds INDEXED BY review_execution_rounds_generation_pending" in statement)
        query = db.execute("EXPLAIN QUERY PLAN " + selected).fetchall()
        assert any("review_execution_rounds_generation_pending" in row[3] for row in query)
        assert all("SCAN " not in row[3] for row in query)
        assert load_review_workflow_row(db, key) == before


def test_schema32_upgrade_preserves_current_authority_without_mutating_historical_schema(rig):
    store, before, _, _, _ = rig
    with store.transaction() as db:
        retained = db.execute("SELECT episode_json FROM workflows WHERE target_kind='pr'").fetchone()[0]
        old_digest = db.execute("SELECT source_digest FROM schema_migrations WHERE version=32").fetchone()[0]
        _historical_fixture(db, 32)
    reopened = SQLiteControlStore(store.path, now=store.now)
    reopened.initialize()
    with reopened.transaction() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 33
        assert db.execute("SELECT source_digest FROM schema_migrations WHERE version=32").fetchone()[0] == old_digest
        assert db.execute("SELECT episode_json FROM workflows WHERE target_kind='pr'").fetchone()[0] == retained
        assert load_review_workflow_row(db, before.revision.fence.key) == before



@pytest.mark.parametrize("missing", ["launch", "return", "charge", "effect", "outcome", "preflight"])
def test_current_live_productive_reference_requires_actual_launch_settlement_and_effect_sources(rig, missing):
    from tests.adapters.test_review_execution_stage import stage
    from tests.adapters.test_review_execution_admission import request
    from tests.adapters.test_review_execution_launch import register
    from tests.adapters.test_review_execution_return import returned, settlement
    from tests.adapters.test_review_execution_submission import command_for
    from tests.adapters.test_review_productive_settlement import workflow_for
    from issue_agent.domain.review_rounds import ReviewSubmissionKind
    from issue_agent.domain.contracts import EffectReceipt, EffectStatus
    from tests.adapters.test_review_execution_control import current

    store, _, gateway, selection, control = rig
    key = selection.expected.fence.key
    plan = control.preflight(selection)
    planned = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, planned)
    proposed = request(planned.children[0])
    assert store.reserve(proposed).admitted
    register(store, proposed)
    store.settle_review_return(settlement(proposed), returned(store, proposed))
    command = command_for(current(store, selection), patch_id="0" * 64)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.PAID)
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:paid"))
    workflow_for(store, gateway).record_review(command.command_id)
    with store.transaction() as db:
        before = load_review_workflow_row(db, key)
        execution = CurrentReviewExecution(
            before.execution.origin,
            ReviewExecutionCheckpoint(before.execution.last_sequence, before.execution.state,
                                      before.execution.baseline.policy),
            before.execution.state, 0, before.execution.last_sequence,
        )
        selected = replace(before, execution=execution)
        persist_workflow_snapshot(db, selected)
        assert load_review_workflow_row(db, key) == selected
        if missing in {"launch", "return", "outcome", "preflight"}:
            namespace = {
                "launch": "review-execution-launch", "return": "review-execution-return",
                "outcome": REVIEW_OUTCOME_NAMESPACE, "preflight": "review-round-preflight",
            }[missing]
            db.execute("DELETE FROM dedupe_keys WHERE namespace=?", (namespace,))
        elif missing == "charge":
            db.execute("DELETE FROM charges")
        else:
            db.execute("DELETE FROM effect_receipts")
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, key)
