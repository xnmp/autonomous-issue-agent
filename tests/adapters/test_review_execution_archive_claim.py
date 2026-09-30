"""Archived review claims retain exact owner and lifecycle ancestry."""

from dataclasses import replace
import sqlite3

import pytest

from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.review_execution_archive_claim import observe_review_execution_archive_claim
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.actions import AgentKind
from tests.adapters.test_review_execution_control import OWNER, dump, rig


def test_live_then_released_claim_has_stable_bounded_proof(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    before = dump(store)
    with store.transaction() as db:
        live = observe_review_execution_archive_claim(db, plan.round)
        assert (live.owner_id, live.occurrence_id, live.terminal_kind) == (
            OWNER, selection.claim_occurrence_id, "live",
        )
        assert live.source_bytes > 0 and len(live.source_sha256) == 64
    assert dump(store) == before
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    after = dump(store)
    with store.transaction() as db:
        released = observe_review_execution_archive_claim(db, plan.round)
        assert released.terminal_kind == "released"
        assert released.owner_id == OWNER and released.occurrence_id == selection.claim_occurrence_id
        assert released.source_sha256 != live.source_sha256
        assert released.source_bytes >= live.source_bytes
    assert dump(store) == after


def test_reclaimed_prior_occurrence_survives_successor_claim(rig, monkeypatch):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    monkeypatch.setattr(ProcessSupervisor, "matches_identity", staticmethod(lambda *_: False))
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    with store.transaction() as db:
        observed = observe_review_execution_archive_claim(db, plan.round)
    assert observed.terminal_kind == "reclaimed"
    assert observed.occurrence_id == selection.claim_occurrence_id


@pytest.mark.parametrize("sql,params", (
    ("DELETE FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' AND key LIKE '%:created'", ()),
    ("UPDATE dedupe_keys SET payload_json=payload_json||' ' "
     "WHERE namespace='dispatch-claim-lifecycle' AND key LIKE '%:created'", ()),
    ("UPDATE review_execution_rounds SET claim_occurrence_id='changed'", ()),
    ("UPDATE review_execution_rounds SET plan_json='{}'", ()),
))
def test_changed_claim_source_is_conflict_without_mutation(rig, sql, params):
    store, _, _, _, control = rig
    plan = control.preflight(rig[3])
    with store.transaction() as db:
        db.execute(sql, params)
    before = dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        observe_review_execution_archive_claim(db, plan.round)
    assert dump(store) == before


def test_missing_terminal_after_claim_deletion_is_conflict(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        db.execute("DELETE FROM dispatch_claims WHERE occurrence_id=?", (selection.claim_occurrence_id,))
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        observe_review_execution_archive_claim(db, plan.round)


def test_extra_terminal_event_is_conflict(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "SELECT namespace,scope,?,claimed_at,payload_json FROM dedupe_keys "
            "WHERE namespace='dispatch-claim-lifecycle' AND key=?",
            (f"{selection.claim_occurrence_id}:reclaimed", f"{selection.claim_occurrence_id}:released"),
        )
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        observe_review_execution_archive_claim(db, plan.round)


def test_source_budget_refuses_before_payload_decode(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError, match="cumulative source byte bound"):
        observe_review_execution_archive_claim(db, plan.round, max_source_bytes=1)


def test_requires_transaction_and_typed_round(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with sqlite3.connect(store.path) as db, pytest.raises(ValueError, match="transaction"):
        observe_review_execution_archive_claim(db, plan.round)
    with store.transaction() as db, pytest.raises(ValueError, match="typed round"):
        observe_review_execution_archive_claim(db, replace(plan.round, round_id="changed"), max_source_bytes=0)


def test_released_claim_remains_provable_after_production_abandonment_seal(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    before = dump(store)
    with store.transaction() as db:
        observed = observe_review_execution_archive_claim(db, plan.round)
        assert observed.terminal_kind == "released"
    assert dump(store) == before


@pytest.mark.parametrize("sql", (
    "UPDATE dedupe_keys SET scope='o/r\u00a0' "
    "WHERE namespace='dispatch-claim-lifecycle' AND key LIKE '%:created'",
    "UPDATE dedupe_keys SET namespace='\tdispatch-claim-lifecycle\n' "
    "WHERE namespace='dispatch-claim-lifecycle' AND key LIKE '%:created'",
    "UPDATE dedupe_keys SET key='\t'||key||'\n' "
    "WHERE namespace='dispatch-claim-lifecycle' AND key LIKE '%:created'",
    "UPDATE review_execution_rounds SET round_id=CAST(round_id AS BLOB)",
))
def test_claim_identity_aliases_are_conflicts(rig, sql):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(sql)
    before = dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        observe_review_execution_archive_claim(db, plan.round)
    assert dump(store) == before


def test_unknown_occurrence_event_is_conflict(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('dispatch-claim-lifecycle','o/r',?,?,?)",
            (f"{selection.claim_occurrence_id}:unexpected", store.now().isoformat(), "{}"),
        )
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError, match="extra or repeated"):
        observe_review_execution_archive_claim(db, plan.round)


def test_oversized_lifecycle_payload_refuses_before_decode(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        db.execute(
            "UPDATE dedupe_keys SET payload_json=? WHERE namespace='dispatch-claim-lifecycle' "
            "AND key=?",
            ("x" * 262_145, f"{selection.claim_occurrence_id}:created"),
        )
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError, match="field bound"):
        observe_review_execution_archive_claim(db, plan.round)


def test_cumulative_budget_accepts_exact_limit_and_rejects_one_byte_less(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        observed = observe_review_execution_archive_claim(db, plan.round)
        assert observe_review_execution_archive_claim(
            db, plan.round, max_source_bytes=observed.source_bytes,
        ) == observed
        with pytest.raises(WorkflowAuthorityError, match="cumulative source byte bound"):
            observe_review_execution_archive_claim(
                db, plan.round, max_source_bytes=observed.source_bytes - 1,
            )


def test_changed_current_claim_owner_is_conflict(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    with store.transaction() as db:
        db.execute("UPDATE dispatch_claims SET owner_id='other-owner' WHERE occurrence_id=?",
                   (selection.claim_occurrence_id,))
    before = dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError, match="current row disagrees"):
        observe_review_execution_archive_claim(db, plan.round)
    assert dump(store) == before
