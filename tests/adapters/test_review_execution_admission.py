"""Paid child reservations retain exact outer-round and parent provenance."""

from dataclasses import replace
import json
import subprocess
import sys

import pytest

from issue_agent.adapters.control_store import AccountingDisposition, BudgetLimit
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import encode_workflow_episodes
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.review_execution import ExecutionNotActivated, ReviewChildAdmission, ReviewChildAdmissionRefusal
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError
from issue_agent.domain.contracts import Money
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


def refusal_receipt(store, child):
    key = json.dumps((child.round.round_id, child.child_id), separators=(",", ":"))
    with store.transaction() as db:
        row = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='review-execution-refusal' "
            "AND scope='o/r' AND key=?", (key,),
        ).fetchone()
    return decode_workflow(row[0], ReviewChildAdmissionRefusal) if row else None


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


def test_parent_seal_closes_stage_list_but_preserves_admission_of_planned_child(planned):
    store, selection, control, child = planned
    sealed = control.seal_completed_planning(child.round.round_id, current(store, selection).revision)
    assert isinstance(sealed, ReviewPlanningSealed)

    admitted = store.reserve(request(child))

    assert admitted.admitted
    assert receipt(store).child == child
    with store.transaction() as db:
        row = db.execute("SELECT planning_json,closure_json,outcome_json FROM review_execution_rounds").fetchone()
        assert decode_workflow(row["planning_json"], ReviewPlanningSealed) == sealed
        assert row["closure_json"] is None and row["outcome_json"] is None


def test_parent_can_seal_after_child_admission_advanced_the_shared_revision(planned):
    store, selection, control, child = planned
    assert store.reserve(request(child)).admitted
    before = current(store, selection)

    sealed = control.seal_completed_planning(child.round.round_id, before.revision)

    assert isinstance(sealed, ReviewPlanningSealed)
    assert current(store, selection).revision.version == before.revision.version + 1
    assert store.reserve(request(child)).admitted


def test_missing_completed_seal_receipt_fences_planned_child_admission(planned):
    store, selection, control, child = planned
    control.seal_completed_planning(child.round.round_id, current(store, selection).revision)
    with store.transaction() as db:
        db.execute("DELETE FROM dedupe_keys WHERE namespace='review-round-seal'")
    before = dump(store)

    assert store.reserve(request(child)).disposition is AccountingDisposition.UNAVAILABLE

    assert receipt(store) is None and dump(store) == before
    assert_unpaid(store)


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


def test_final_exhaustion_records_exact_child_refusal_without_paid_state(planned):
    store, selection, _, child = planned
    proposed = request(child, cap=59)
    before = current(store, selection)
    assert store.reserve(proposed).disposition is AccountingDisposition.EXHAUSTED
    refusal = store.record_review_admission_refusal(proposed)
    assert refusal == refusal_receipt(store, child)
    assert refusal.child == child
    assert refusal.claim_occurrence_id == selection.claim_occurrence_id
    assert refusal.refused_revision.version == before.revision.version + 1
    assert (refusal.limit_repository, refusal.limit_dimension, refusal.limit_period_id,
            refusal.committed_micros, refusal.requested_micros, refusal.cap_micros) == (
                proposed.scope.repository, proposed.scope.dimension, proposed.scope.period_id, 0, 60, 59)
    assert current(store, selection).revision == refusal.refused_revision
    assert_unpaid(store)
    snapshot = dump(store)
    assert store.record_review_admission_refusal(proposed) == refusal
    assert dump(store) == snapshot
    assert store.reserve(replace(proposed, cap=Money("USD", 100))).disposition is AccountingDisposition.UNAVAILABLE
    assert dump(store) == snapshot


def test_changed_request_cannot_replay_a_child_refusal(planned):
    store, _, _, child = planned
    proposed = request(child, cap=59)
    store.record_review_admission_refusal(proposed)
    before = dump(store)
    for changed in (replace(proposed, cap=Money("USD", 58)),
                    replace(proposed, dispatch_id=proposed.dispatch_id + " "),
                    replace(proposed, run_id=type(proposed.run_id)("another-run"))):
        with pytest.raises(WorkflowAuthorityError, match="conflicting refusal"):
            store.record_review_admission_refusal(changed)
        assert dump(store) == before


@pytest.mark.parametrize("corruption", ["committed", "cap", "scope", "claim", "future_revision", "unreadable"])
def test_refusal_replay_rejects_corrupted_proof(planned, corruption):
    store, _, _, child = planned
    proposed = request(child, cap=59)
    refusal = store.record_review_admission_refusal(proposed)
    assert refusal is not None
    if corruption == "committed":
        changed = replace(refusal, committed_micros=1)
    elif corruption == "cap":
        changed = replace(refusal, cap_micros=58)
    elif corruption == "scope":
        changed = replace(refusal, limit_dimension="global")
    elif corruption == "claim":
        changed = replace(refusal, claim_occurrence_id="other-claim")
    else:
        changed = replace(refusal, refused_revision=replace(refusal.refused_revision,
                                                            version=refusal.refused_revision.version + 100))
    payload = "unreadable" if corruption == "unreadable" else encode_workflow(changed)
    with store.transaction() as db:
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-refusal'", (payload,))
    before = dump(store)
    with pytest.raises((WorkflowAuthorityError, ValueError)):
        store.record_review_admission_refusal(proposed)
    assert dump(store) == before


def test_refusal_rechecks_current_cap_and_parent_before_writing(planned):
    store, _, _, child = planned
    proposed = request(child, cap=59)
    assert store.reserve(proposed).disposition is AccountingDisposition.EXHAUSTED
    before = dump(store)
    assert store.record_review_admission_refusal(replace(proposed, cap=Money("USD", 60))) is None
    assert dump(store) == before
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="live parent"):
        store.record_review_admission_refusal(proposed)
    assert refusal_receipt(store, child) is None and dump(store) == before
    assert_unpaid(store)


def test_admission_and_refusal_are_mutually_exclusive_for_child(planned):
    store, _, _, child = planned
    admitted = request(child)
    assert store.reserve(admitted).admitted
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="paid admission"):
        store.record_review_admission_refusal(replace(admitted, cap=Money("USD", 59)))
    assert refusal_receipt(store, child) is None and dump(store) == before


def test_refusal_retains_the_exact_secondary_limit_scope(planned):
    store, _, _, child = planned
    proposed = request(child)
    secondary = BudgetLimit(replace(proposed.scope, repository="global/all", dimension="global",
                                    period_id="global:day"), Money("USD", 59))
    proposed = replace(proposed, additional_limits=(secondary,))
    assert store.reserve(proposed).tripped_dimension == "global"
    refusal = store.record_review_admission_refusal(proposed)
    assert refusal is not None
    assert (refusal.limit_repository, refusal.limit_dimension, refusal.limit_period_id,
            refusal.limit_period_start, refusal.limit_period_end, refusal.limit_period_timezone,
            refusal.currency, refusal.committed_micros, refusal.requested_micros,
            refusal.cap_micros) == (
                secondary.scope.repository, secondary.scope.dimension, secondary.scope.period_id,
                secondary.scope.period_start, secondary.scope.period_end,
                str(secondary.scope.period_timezone), "USD", 0, 60, 59)
    assert_unpaid(store)


def test_refusal_receipt_write_failure_rolls_back_revision_and_all_paid_state(planned):
    store, _, _, child = planned
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_child_refusal BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-execution-refusal' BEGIN SELECT RAISE(ABORT,'receipt refused'); END")
    before = dump(store)
    with pytest.raises(Exception, match="receipt refused"):
        store.record_review_admission_refusal(request(child, cap=59))
    assert refusal_receipt(store, child) is None and dump(store) == before
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


def test_reviewed_head_after_planning_makes_new_child_money_ineligible(planned):
    store, selection, _, child = planned
    snapshot = current(store, selection)
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


def test_changed_initial_failure_count_after_planning_blocks_paid_admission(planned):
    store, selection, _, child = planned
    snapshot = current(store, selection)
    state = replace(snapshot.execution.state, failures=selection.failure_ceiling)
    changed = replace(snapshot, execution=replace(snapshot.execution, state=state,
                      baseline=replace(snapshot.execution.baseline, state=state)))
    with store.transaction() as db:
        db.execute("UPDATE workflows SET episode_json=?,version=version+1 WHERE target_kind='pr'",
                   (encode_workflow_episodes(changed),))
    with pytest.raises(WorkflowAuthorityError, match="origin"):
        current(store, selection)
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
            if family == "review_execution":
                db.execute("DELETE FROM workflow_activation_imports WHERE family=?", (family,))
                db.execute("DELETE FROM workflow_activation_evidence WHERE family=?", (family,))
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


@pytest.mark.parametrize("corruption", ["missing", "changed_child", "changed_claim", "unreadable", "future_revision",
                                        "missing_child_index", "changed_child_index"])
def test_exact_replay_requires_the_original_admission_receipt(planned, corruption):
    store, _, _, child = planned
    accepted = request(child)
    assert store.reserve(accepted).admitted
    recorded = receipt(store)
    with store.transaction() as db:
        if corruption == "missing":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-admission'")
        elif corruption == "missing_child_index":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-child-admission'")
        elif corruption == "changed_child_index":
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-child-admission'",
                       (encode_workflow(replace(recorded, claim_occurrence_id="changed")),))
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
