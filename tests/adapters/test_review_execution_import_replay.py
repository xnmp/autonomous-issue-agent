"""SQL replay of an atomic zero-history execution import."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3

import pytest

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_import_commit import commit_review_execution_import
from issue_agent.adapters.review_execution_import_replay import verify_committed_review_execution_import
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, encode_workflow_episodes, insert_workflow_component,
    insert_initialized_review_execution_origin, load_review_workflow_row, persist_workflow_snapshot,
)
from issue_agent.adapters.workflow_codec import encode_workflow
from issue_agent.domain.labels import Label
from issue_agent.domain.review_activation import OpenPullRequestsListed
from issue_agent.domain.review_execution import initialized_review_execution, CurrentReviewExecution
from issue_agent.domain.review_execution_counter_mapping import LegacyOpenPRCounterLink
from issue_agent.domain.review_execution_history_import import ReviewImportNoHistoryObserved
from issue_agent.domain.review_execution_import_plan import (
    EligibleZeroHistoryReviewImport, ReviewExecutionImportPlan, screen_zero_history_review_import,
)
from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PullRequestState, RecordedPullRequest
from issue_agent.domain.review_workflow_policy import initial_review_workflow
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowFamily, WorkflowKey,
)


NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
REPOSITORY = "o/r"


def _rig(tmp_path, *, store=None):
    if store is None:
        store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
        store.initialize()
    authority = FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1)
    candidates = []
    with store.transaction() as db:
        db.execute("INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   (REPOSITORY, authority.family.value, authority.epoch, "review-source", "review-inventory", NOW.isoformat()))
        for pr in (9, 10):
            key = WorkflowKey(REPOSITORY, PullRequestRef(pr))
            workflow = initial_review_workflow(key, authority)
            db.execute("INSERT INTO workflows VALUES (?, 'pr', ?, 0, 0, ?)",
                       (REPOSITORY, pr, encode_workflow_episodes(workflow)))
            insert_workflow_component(db, key, authority, workflow.episode.episode_id)
            recorded = RecordedPullRequest(ObservedPullRequest(
                key, "a" * 40, PullRequestState.OPEN, frozenset({Label.AI_PR_OPEN}),
                CompleteReviews(()), NOW,
            ))
            candidate = screen_zero_history_review_import(
                ReviewImportNoHistoryObserved(pr, "b" * 64), workflow, recorded,
                LegacyOpenPRCounterLink(pr, "a" * 40, f"work/{pr}", None, None),
                "review-bot[bot]", now=NOW,
            )
            assert isinstance(candidate, EligibleZeroHistoryReviewImport)
            candidates.append(candidate)
    plan = ReviewExecutionImportPlan(
        REPOSITORY, OpenPullRequestsListed((9, 10)), 2,
        "b" * 64, "c" * 64, "d" * 64, tuple(candidates), NOW,
    )
    return store, plan


def _dump(store):
    with sqlite3.connect(store.path) as db:
        return tuple(db.iterdump())


def test_absence_and_historical_retry_are_inert(tmp_path):
    store, plan = _rig(tmp_path)
    before = _dump(store)
    with store.transaction() as db:
        assert verify_committed_review_execution_import(db, plan) is False
    assert _dump(store) == before
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
    after = _dump(store)
    with store.transaction() as db:
        assert verify_committed_review_execution_import(db, plan) is True
    assert _dump(store) == after
    with store.transaction() as db:
        assert verify_committed_review_execution_import(db, plan) is True
    assert _dump(store) == after


def test_replay_requires_transaction(tmp_path):
    store, plan = _rig(tmp_path)
    with sqlite3.connect(store.path) as db, pytest.raises(ValueError, match="open SQL transaction"):
        verify_committed_review_execution_import(db, plan)


@pytest.mark.parametrize("sql,params", [
    ("UPDATE workflow_authority_epochs SET source_digest=? WHERE family='review_execution'", ("e" * 64,)),
    ("UPDATE workflow_activation_evidence SET plan_digest=? WHERE family='review_execution'", ("e" * 64,)),
    ("UPDATE workflow_activation_imports SET decision_json=decision_json||' ' WHERE target_number=9", ()),
    ("DELETE FROM workflow_activation_imports WHERE target_number=10", ()),
    ("UPDATE workflow_components SET episode_id='changed' WHERE family='review_execution' AND target_number=9", ()),
    ("UPDATE dedupe_keys SET payload_json=payload_json||' ' WHERE namespace='review-execution-origin'", ()),
    ("UPDATE workflow_activation_imports SET repository=CAST(repository AS BLOB) WHERE target_number=9", ()),
    ("UPDATE workflow_activation_evidence SET family='Review_Execution'", ()),
    ("UPDATE workflow_authority_epochs SET activated_at=? WHERE family='review_execution'", ((NOW - timedelta(seconds=1)).isoformat(),)),
])
def test_corrupt_authority_is_conflict_without_mutation(tmp_path, sql, params):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(sql, params)
    before = _dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        verify_committed_review_execution_import(db, plan)
    assert _dump(store) == before


def test_changed_plan_is_not_a_retry(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
    changed = ReviewExecutionImportPlan(
        plan.repository, plan.listing, plan.execution_epoch, plan.source_set_digest,
        plan.retained_digest, "e" * 64, plan.candidates, plan.planned_at, plan.policy,
    )
    before = _dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        verify_committed_review_execution_import(db, changed)
    assert _dump(store) == before


def test_generation_progress_keeps_import_replay_inert(tmp_path):
    from tests.adapters.test_review_workflow_control import Rig
    from issue_agent.domain.workflow import KeepGuidance, OperatorActor, RequestRearm, WorkflowCommitted
    from issue_agent.domain.review_workflow_policy import review_ceiling_cause, review_ceiling_gate
    from issue_agent.domain.review_rejection import CountingRule

    rig = Rig(tmp_path, reviews=0)
    store, plan = _rig(tmp_path, store=rig.store)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
    rig.remote.seed_review(9, "later-bot-review", author="review-bot[bot]",
                           commit="a" * 40, submitted_at=NOW.isoformat())
    control = rig.control(ceiling=1)
    key = WorkflowKey(REPOSITORY, PullRequestRef(9))
    observed = control._observe(key)
    cause = review_ceiling_cause(
        observed.observation, episode_id=plan.candidates[0].workflow.episode.episode_id,
        rule=CountingRule("review-bot[bot]"), ceiling=1,
    )
    assert cause is not None
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
        assert snapshot is not None
        next_revision = replace(snapshot.revision, version=snapshot.revision.version + 1)
        gate = review_ceiling_gate(key, cause, next_revision)
        gated = replace(snapshot, revision=next_revision, gates=(gate,))
        persist_workflow_snapshot(db, gated)
    request = RequestRearm(
        "rearm:import-replay", gated.revision, gated.gates[0].gate_id,
        KeepGuidance(), expected_binding=gated.binding,
    )
    accepted = control.request_rearm(request, OperatorActor("operator", "test"))
    assert isinstance(accepted, WorkflowCommitted)
    assert accepted.snapshot.revision.fence.generation == 1
    before = _dump(store)
    with store.transaction() as db:
        assert load_review_workflow_row(db, WorkflowKey(REPOSITORY, PullRequestRef(9))) == accepted.snapshot
        assert verify_committed_review_execution_import(db, plan) is True
    assert _dump(store) == before


def test_newly_initialized_pr_keeps_import_replay_inert(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
        key = WorkflowKey(REPOSITORY, PullRequestRef(11))
        rejection = initial_review_workflow(
            key, FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1),
        )
        db.execute("INSERT INTO workflows VALUES (?, 'pr', ?, 0, 0, ?)",
                   (REPOSITORY, 11, encode_workflow_episodes(rejection)))
        insert_workflow_component(db, key, rejection.authority, rejection.episode.episode_id)
        observation = RecordedPullRequest(ObservedPullRequest(
            key, "f" * 40, PullRequestState.OPEN, frozenset({Label.AI_PR_OPEN}),
            CompleteReviews(()), NOW,
        ))
        payload = encode_workflow(observation)
        evidence_id = "pr-observation:" + hashlib.sha256(payload.encode()).hexdigest()
        db.execute(
            "INSERT INTO workflow_evidence VALUES (?, ?, 'pr', ?, NULL, 'observation', ?)",
            (evidence_id, REPOSITORY, 11, payload),
        )
        execution, receipt = initialized_review_execution(
            rejection.binding, plan.execution_epoch, evidence_id,
            plan.plan_digest, plan.source_digest, plan.inventory_digest, NOW,
        )
        execution = CurrentReviewExecution(execution.origin, execution.checkpoint, execution.state, 0, 0)
        insert_workflow_component(
            db, key, FamilyAuthority(WorkflowFamily.REVIEW_EXECUTION, plan.execution_epoch),
            execution.binding.execution_episode,
        )
        insert_initialized_review_execution_origin(db, REPOSITORY, receipt, claimed_at=NOW)
        persist_workflow_snapshot(db, replace(
            rejection, revision=replace(rejection.revision, version=1), execution=execution,
        ))
        assert load_review_workflow_row(db, key) is not None
    before = _dump(store)
    with store.transaction() as db:
        assert verify_committed_review_execution_import(db, plan) is True
    assert _dump(store) == before


def test_unselected_foreign_round_does_not_reaudit_original_plan(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
        db.execute(
            "INSERT INTO review_execution_rounds "
            "(round_id,repository,target_kind,target_number,execution_epoch,execution_episode,"
            "generation,sequence,claim_occurrence_id,plan_json,planning_json) "
            "VALUES (?,?,'pr',?,?,?,?,1,?,?,?)",
            ("foreign-round", REPOSITORY, 9, plan.execution_epoch, "foreign-episode",
             0, "claim", "{}", "{}"),
        )
    before = _dump(store)
    with store.transaction() as db:
        assert verify_committed_review_execution_import(db, plan) is True
    assert _dump(store) == before


def test_unmatched_global_rows_exceeding_scan_bound_fail_closed(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        db.executemany(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('foreign','elsewhere',?,?,'{}')",
            ((f"foreign-{index}", NOW.isoformat()) for index in range(65_537)),
        )
        before = db.total_changes
        with pytest.raises(WorkflowAuthorityError, match="global census bound"):
            verify_committed_review_execution_import(db, plan)
        assert db.total_changes == before


@pytest.mark.parametrize("orphan", ("component", "origin"))
def test_absent_activation_with_retained_execution_row_is_conflict(tmp_path, orphan):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        if orphan == "component":
            key = plan.candidates[0].workflow.revision.fence.key
            insert_workflow_component(
                db, key, FamilyAuthority(WorkflowFamily.REVIEW_EXECUTION, plan.execution_epoch),
                "orphan-execution-episode",
            )
        else:
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('review-execution-origin',?,?,?,'{}')",
                (REPOSITORY, "orphan-origin", NOW.isoformat()),
            )
    before = _dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        verify_committed_review_execution_import(db, plan)
    assert _dump(store) == before


@pytest.mark.parametrize("table,column,where", (
    ("workflow_activation_imports", "decision_json", "target_number=9"),
    ("dedupe_keys", "payload_json", "namespace='review-execution-origin'"),
    ("workflows", "episode_json", "target_kind='pr' AND target_number=9"),
))
def test_selected_oversized_payload_is_conflict_without_mutation(tmp_path, table, column, where):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
        db.execute(f"UPDATE {table} SET {column}=? WHERE {where}", ("x" * 262_145,))
    before = _dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        verify_committed_review_execution_import(db, plan)
    assert _dump(store) == before


def test_unrelated_inactive_target_refuses_at_reader_without_invalidating_original_retry(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
        key = WorkflowKey(REPOSITORY, PullRequestRef(11))
        workflow = initial_review_workflow(
            key, FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1),
        )
        db.execute("INSERT INTO workflows VALUES (?, 'pr', ?, 0, 0, ?)",
                   (REPOSITORY, 11, encode_workflow_episodes(workflow)))
        insert_workflow_component(db, key, workflow.authority, workflow.episode.episode_id)
    before = _dump(store)
    with store.transaction() as db:
        assert verify_committed_review_execution_import(db, plan) is True
        with pytest.raises(WorkflowAuthorityError, match="component is absent"):
            load_review_workflow_row(db, key)
    assert _dump(store) == before


def test_sql_length_guard_rejects_huge_linked_gate_and_restores_limit(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
        db.execute(
            "INSERT INTO workflow_gates VALUES (? ,?, 'pr', 9, 0, ?, 'open', ?)",
            ("huge-gate", REPOSITORY, "huge-cause", "x" * (8 * 262_144 + 1)),
        )
    with store.transaction() as db:
        category = sqlite3.SQLITE_LIMIT_LENGTH
        caller_limit = db.setlimit(category, 4 * 1024 * 1024)
        try:
            with pytest.raises(WorkflowAuthorityError):
                verify_committed_review_execution_import(db, plan)
            assert db.getlimit(category) == 4 * 1024 * 1024
        finally:
            db.setlimit(category, caller_limit)


def test_sql_length_guard_preserves_lower_caller_limit_on_success_and_refusal(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
    with store.transaction() as db:
        category = sqlite3.SQLITE_LIMIT_LENGTH
        caller_limit = db.setlimit(category, 512 * 1024)
        try:
            assert verify_committed_review_execution_import(db, plan) is True
            assert db.getlimit(category) == 512 * 1024
            db.execute("UPDATE workflow_activation_evidence SET plan_digest=? WHERE family='review_execution'",
                       ("e" * 64,))
            with pytest.raises(WorkflowAuthorityError):
                verify_committed_review_execution_import(db, plan)
            assert db.getlimit(category) == 512 * 1024
        finally:
            db.setlimit(category, caller_limit)


def test_active_gate_cardinality_is_checked_before_decode(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
        db.executemany(
            "INSERT INTO workflow_gates VALUES (?,?,'pr',9,0,?,'open','{}')",
            ((f"gate:{index}", REPOSITORY, f"cause:{index}") for index in range(65)),
        )
    before = _dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError, match="gate count"):
        verify_committed_review_execution_import(db, plan)
    assert _dump(store) == before


@pytest.mark.parametrize("table", ("dedupe_keys", "workflow_evidence"))
def test_unreferenced_payload_is_not_part_of_original_plan_retry(tmp_path, table):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
        if table == "dedupe_keys":
            db.execute(
                "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                "VALUES ('foreign',?,?,?,?)",
                (REPOSITORY, "other", NOW.isoformat(), "x" * 262_145),
            )
        else:
            db.execute(
                "INSERT INTO workflow_evidence VALUES (?,?,'pr',9,NULL,'observation',?)",
                ("foreign-evidence", REPOSITORY, "x" * 262_145),
            )
    before = _dump(store)
    with store.transaction() as db:
        assert verify_committed_review_execution_import(db, plan) is True
    assert _dump(store) == before


def test_foreign_repository_payload_is_not_charged_to_corpus(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "VALUES ('foreign','another/repo','other',?,?)",
            (NOW.isoformat(), "x" * 262_145),
        )
    before = _dump(store)
    with store.transaction() as db:
        assert verify_committed_review_execution_import(db, plan) is True
    assert _dump(store) == before


@pytest.mark.parametrize("table,sql,params", (
    ("workflow_authority_epochs",
     "INSERT INTO workflow_authority_epochs VALUES (?, 'review_execution', 2, ?, ?, ?)",
     ("\to/r\n", "b" * 64, "d" * 64, NOW.isoformat())),
    ("workflow_authority_epochs",
     "INSERT INTO workflow_authority_epochs VALUES (?, 'review_execution', 2, ?, ?, ?)",
     ("o/r\u00a0", "b" * 64, "d" * 64, NOW.isoformat())),
    ("dedupe_keys",
     "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
     ("review-execution-origin", "\to/r\n", "aliased-origin", NOW.isoformat(), "{}")),
    ("dedupe_keys",
     "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
     ("review-execution-origin", "o/r\u00a0", "aliased-origin", NOW.isoformat(), "{}")),
))
def test_absent_execution_with_whitespace_repository_alias_is_conflict(tmp_path, table, sql, params):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        db.execute(sql, params)
    before = _dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError, match="repository identity is aliased"):
        verify_committed_review_execution_import(db, plan)
    assert _dump(store) == before


@pytest.mark.parametrize("sql", (
    "UPDATE workflow_activation_evidence SET family='\treview_execution\n'",
    "UPDATE dedupe_keys SET namespace='\treview-execution-origin\n' "
    "WHERE namespace='review-execution-origin'",
))
def test_committed_family_or_origin_namespace_whitespace_alias_is_conflict(tmp_path, sql):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(sql)
    before = _dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        verify_committed_review_execution_import(db, plan)
    assert _dump(store) == before


def test_committed_target_kind_whitespace_alias_is_conflict(tmp_path):
    store, plan = _rig(tmp_path)
    with store.transaction() as db:
        commit_review_execution_import(db, plan, now=NOW)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE workflows SET target_kind='\tpr\n' WHERE target_number=9")
    before = _dump(store)
    with store.transaction() as db, pytest.raises(WorkflowAuthorityError):
        verify_committed_review_execution_import(db, plan)
    assert _dump(store) == before


def test_missing_sqlite_length_limit_api_refuses_without_mutation(tmp_path):
    class LegacyConnection(sqlite3.Connection):
        def __getattribute__(self, name):
            if name in {"getlimit", "setlimit"}:
                return None
            return super().__getattribute__(name)

    store, plan = _rig(tmp_path)
    before = _dump(store)
    with sqlite3.connect(store.path, factory=LegacyConnection) as db:
        db.execute("BEGIN")
        changes = db.total_changes
        with pytest.raises(WorkflowAuthorityError, match="available in Python 3.11\\+"):
            verify_committed_review_execution_import(db, plan)
        assert db.total_changes == changes
        db.rollback()
    assert _dump(store) == before
