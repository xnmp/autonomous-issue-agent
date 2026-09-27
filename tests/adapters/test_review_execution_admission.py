"""Paid child reservations retain exact outer-round and parent provenance."""

from dataclasses import replace
import json
import subprocess
import sys

import pytest

from issue_agent.adapters.control_store import AccountingDisposition
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import encode_workflow_episodes
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.review_execution import ExecutionNotActivated, ReviewChildAdmission
from issue_agent.domain.review_rounds import ReviewPlanningComplete, ReviewPlanningSealed, ReviewRoundPlan
from tests.adapters.test_control_store import _request, _seed_v24_workflow_history, _store
from tests.adapters.test_review_execution_control import OWNER, assert_unpaid, current, dump, rig
from tests.adapters.test_review_execution_stage import stage


def request(child, *, index=1, cap=100):
    return replace(_request(index, maximum=60, cap=cap), kind="REVIEW", owner_id=OWNER,
                   workflow=child.round.binding.rejection, review_child=child,
                   dispatch_id=json.dumps({"repository": "o/r", "role": "review", "kind": "REVIEW",
                                           "runtime": "claude", "number": 9, "claim_target": -9,
                                           "leg_id": f"leg:{index}"}))


@pytest.fixture
def planned(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    proposed = stage(plan)
    control.register_stage(plan.round.round_id, current(store, selection).revision, proposed)
    return store, selection, control, proposed.children[0]


def receipt(store, run_id="run-1"):
    with store.transaction() as db:
        row = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-admission' "
                         "AND scope='o/r' AND key=?", (run_id,)).fetchone()
    return decode_workflow(row[0], ReviewChildAdmission) if row else None


def test_admission_reserves_money_and_attaches_one_child_without_launching(planned):
    store, selection, _, child = planned
    before = current(store, selection)
    result = store.reserve(request(child))
    assert result.admitted and result.requested.micros == 60
    after = current(store, selection)
    assert after.revision.version == before.revision.version + 1
    assert after.execution == before.execution and after.episode == before.episode
    assert receipt(store) == ReviewChildAdmission(child, "run-1", "reservation-1", selection.claim_occurrence_id,
                                                 after.revision, OWNER)
    with store.transaction() as db:
        run = db.execute("SELECT state,review_round_id,review_child_id FROM runs").fetchone()
        assert tuple(run) == ("reserved", child.round.round_id, child.child_id)
        assert tuple(db.execute("SELECT state,maximum_micros FROM budget_reservations").fetchone()) == ("active", 60)
        assert tuple(db.execute("SELECT target,run_id FROM dispatch_claim_runs").fetchone()) == ("pr:9", "run-1")
        assert db.execute("SELECT policy FROM reservation_recovery").fetchone()[0] == "spawn_pending"
        assert db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0


def test_exact_admission_replay_after_parent_release_is_inert(planned):
    store, _, _, child = planned
    accepted = request(child)
    assert store.reserve(accepted).admitted
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    before = dump(store)
    replay = store.reserve(accepted)
    assert replay.admitted and replay.committed.micros == 60
    assert dump(store) == before


@pytest.mark.parametrize("runtime", [None, "codex"])
def test_paid_child_requires_its_explicit_planned_runtime(planned, runtime):
    store, _, _, child = planned
    proposed = request(child)
    dispatch = json.loads(proposed.dispatch_id)
    if runtime is None:
        dispatch.pop("runtime")
    else:
        dispatch["runtime"] = runtime
    proposed = replace(proposed, dispatch_id=json.dumps(dispatch))
    before = dump(store)
    assert not store.reserve(proposed).admitted
    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)


@pytest.mark.parametrize("change", ["child", "round", "absent_child"])
def test_paid_run_identity_cannot_be_reused_for_different_planned_work(planned, change):
    store, _, _, child = planned
    accepted = request(child)
    assert store.reserve(accepted).admitted
    other = (replace(child, child_id="another-child") if change == "child" else
             replace(child, round=replace(child.round, round_id="another-round")))
    changed = replace(accepted, review_child=None if change == "absent_child" else other)
    before = dump(store)
    assert store.reserve(changed).disposition is AccountingDisposition.CONFLICT
    assert dump(store) == before


@pytest.mark.parametrize("change", ["absent_child", "unplanned_child", "missing_round", "sealed", "stale_generation",
                                    "released_parent", "replaced_parent", "missing_stage_receipt"])
def test_invalid_child_or_parent_never_buys_an_admission(planned, change):
    store, _, _, child = planned
    proposed = request(child)
    if change == "absent_child":
        proposed = replace(proposed, review_child=None)
    elif change == "unplanned_child":
        proposed = replace(proposed, review_child=replace(child, child_id="unplanned"))
    elif change in {"released_parent", "replaced_parent"}:
        assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
        if change == "replaced_parent":
            assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    else:
        with store.transaction() as db:
            if change == "missing_round":
                db.execute("DELETE FROM review_execution_rounds")
            elif change == "sealed":
                db.execute("UPDATE review_execution_rounds SET planning_json=?",
                           (encode_workflow(ReviewPlanningSealed("sealed", ReviewPlanningComplete())),))
            elif change == "stale_generation":
                db.execute("UPDATE workflows SET generation=generation+1,version=version+1 WHERE target_kind='pr'")
            else:
                db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-stage'")
    before = dump(store)
    assert not store.reserve(proposed).admitted
    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)


def test_exhausted_budget_allocates_neither_admission_receipt_nor_aggregate_revision(planned):
    store, _, _, child = planned
    before = dump(store)
    assert store.reserve(request(child, cap=59)).disposition is AccountingDisposition.EXHAUSTED
    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)


def test_inactive_execution_cannot_accept_a_child_captured_from_another_authority(planned, tmp_path):
    _, selection, _, child = planned
    inactive = _store(tmp_path / "inactive")
    _seed_v24_workflow_history(inactive)
    inactive.initialize()
    snapshot = current(inactive, selection)
    assert isinstance(snapshot.execution, ExecutionNotActivated)
    assert snapshot.binding == child.round.binding.rejection
    before = dump(inactive)
    assert not inactive.reserve(request(child)).admitted
    assert receipt(inactive) is None and dump(inactive) == before
    assert_unpaid(inactive)


@pytest.mark.parametrize("current_hold", ["failure_ceiling", "already_reviewed"])
def test_progression_after_planning_can_make_new_child_money_ineligible(planned, current_hold):
    store, selection, _, child = planned
    snapshot = current(store, selection)
    if current_hold == "failure_ceiling":
        state = replace(snapshot.execution.state, failures=selection.failure_ceiling)
        updated = replace(snapshot, execution=replace(snapshot.execution, state=state,
                          checkpoint=replace(snapshot.execution.checkpoint, state=state)))
    else:
        updated = replace(snapshot, episode=replace(snapshot.episode,
                          continuation=replace(snapshot.episode.continuation, head_sha=selection.observation.head_sha)))
    with store.transaction() as db:
        db.execute("UPDATE workflows SET episode_json=?,version=version+1 WHERE target_kind='pr'",
                   (encode_workflow_episodes(updated),))
    assert current(store, selection).execution.state == updated.execution.state
    assert current(store, selection).episode == updated.episode
    before = dump(store)
    assert not store.reserve(request(child)).admitted
    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)


@pytest.mark.parametrize("missing_epochs", [("review_rejections",), ("review_rejections", "review_execution")])
def test_missing_epochs_cannot_restore_unbound_paid_review_with_authority_retained(planned, missing_epochs):
    store, _, _, child = planned
    proposed = replace(request(child), workflow=None, review_child=None)
    with store.transaction() as db:
        for family in missing_epochs:
            db.execute("DELETE FROM workflow_authority_epochs WHERE family=?", (family,))
    before = dump(store)
    assert not store.reserve(proposed).admitted
    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)


@pytest.mark.parametrize("different_process", [False, True])
def test_parent_replacement_between_liveness_probe_and_admission_is_refused(planned, monkeypatch, different_process):
    store, _, _, child = planned
    original = ProcessSupervisor.matches_identity
    captured = []

    def replace_after_real_probe(pid, ticks):
        live = original(pid, ticks)
        if not captured:
            assert live
            captured.append((pid, ticks))
            assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
            if different_process:
                subprocess.run([sys.executable, "-c", """
import sys
from issue_agent.adapters.control_store import SQLiteControlStore
store = SQLiteControlStore(sys.argv[1])
assert store.claim_dispatch('o/r','review',-9,'REVIEW',sys.argv[2],frozenset())
""", store.path, OWNER], check=True, capture_output=True, text=True)
            else:
                assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
            captured.append(dump(store))
        return live

    monkeypatch.setattr(ProcessSupervisor, "matches_identity", replace_after_real_probe)
    assert not store.reserve(request(child)).admitted
    assert len(captured) == 2
    assert receipt(store) is None and dump(store) == captured[1]
    assert_unpaid(store)


def test_receipt_failure_rolls_back_money_run_claim_attachment_and_workflow_version(planned):
    store, _, _, child = planned
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_review_admission BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-execution-admission' BEGIN SELECT RAISE(ABORT,'receipt refused'); END")
    before = dump(store)
    assert not store.reserve(request(child)).admitted
    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_review_admission")
    assert store.reserve(request(child)).admitted


def test_one_planned_child_cannot_buy_two_runs(planned):
    store, _, _, child = planned
    assert store.reserve(request(child)).admitted
    before = dump(store)
    assert not store.reserve(request(child, index=2, cap=200)).admitted
    assert receipt(store, "run-2") is None and dump(store) == before


def test_late_claim_attachment_failure_rolls_back_admission_and_reserved_money(planned):
    store, _, _, child = planned
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_child_attachment BEFORE INSERT ON dispatch_claim_runs "
                   "BEGIN SELECT RAISE(ABORT,'attachment refused'); END")
    before = dump(store)
    assert not store.reserve(request(child)).admitted
    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)


@pytest.mark.parametrize("corruption", ["missing", "changed_child", "changed_claim", "unreadable", "future_revision"])
def test_exact_replay_requires_the_original_admission_receipt(planned, corruption):
    store, _, _, child = planned
    accepted = request(child)
    assert store.reserve(accepted).admitted
    recorded = receipt(store)
    with store.transaction() as db:
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-admission'")
        else:
            payload = ("unreadable" if corruption == "unreadable" else
                       encode_workflow(replace(recorded, admitted_revision=replace(recorded.admitted_revision,
                                                                                  version=recorded.admitted_revision.version + 100)))
                       if corruption == "future_revision" else
                       encode_workflow(replace(recorded, child=replace(child, child_id="changed")))
                       if corruption == "changed_child" else encode_workflow(replace(recorded, claim_occurrence_id="changed")))
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-admission'", (payload,))
    before = dump(store)
    assert not store.reserve(accepted).admitted
    assert dump(store) == before


def test_an_exited_real_parent_cannot_admit_its_planned_child(rig):
    store, _, _, selection, _ = rig
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    child = subprocess.run([sys.executable, "-c", """
import sys
from dataclasses import replace
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.adapters.test_review_execution_control import Gateway, current
from tests.adapters.test_review_execution_stage import stage
selection = decode_workflow(sys.argv[3], ReviewPreflightSelection)
store = SQLiteControlStore(sys.argv[1], now=lambda: selection.observation.observed_at)
store.initialize()
assert store.claim_dispatch('o/r','review',-9,'REVIEW',sys.argv[2],frozenset())
claim = store.active_dispatch_claims('o/r','review')[0]
selection = replace(selection, claim_occurrence_id=claim.claim_key)
control = SQLiteReviewExecutionControl(store, WorkflowWriter('o/r','review',sys.argv[2]), Gateway())
plan = control.preflight(selection)
print(encode_workflow(control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))))
""", store.path, OWNER, encode_workflow(selection)], capture_output=True, text=True, check=True)
    plan = decode_workflow(child.stdout.strip(), ReviewRoundPlan)
    before = dump(store)
    assert not store.reserve(request(plan.children[0])).admitted
    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)
