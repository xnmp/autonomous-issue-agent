"""Unpaid review preflight commits over real SQLite and exact parent claims."""

from tests.domain.builders import observed_pr

from dataclasses import replace
import sqlite3
from contextlib import contextmanager

import pytest

from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, WorkflowRevisionConflict, load_review_workflow_row,
)
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.labels import Label
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.review_workflow import CompleteReviews, ObservedPullRequest, PartialReviews, PullRequestState
from issue_agent.domain.workflow_identity import WorkflowWriter
from issue_agent.domain.world import IssueView, PRView
from tests.adapters.test_control_store import _activate_execution_fixture, _store
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot

OWNER = "o/r:review:test"


class Gateway:
    def __init__(self):
        self.view = observed_pr(9, head_ref_oid="b" * 40, labels=frozenset({Label.AI_PR_OPEN}))
        self.reviews = CompleteReviews(())
        self.issue = IssueView(7)
        self.dependencies = ()
        self.calls = []
        self.on_pr = None
        self.error = None

    def get_pr(self, number, *, mergeability=False, checks=False):
        self.calls.append(("pr", number))
        if self.error is not None:
            raise self.error
        if self.on_pr is not None:
            self.on_pr()
        return self.view

    def pull_request_reviews(self, number):
        self.calls.append(("reviews", number))
        return self.reviews

    def get_issue(self, number):
        self.calls.append(("issue", number))
        if self.error is not None:
            raise self.error
        return self.issue

    def open_blockers(self, number):
        return self.dependencies


@pytest.fixture
def rig(tmp_path, request):
    """PR 9's live execution; indirect param False drops the covered-patch baseline."""
    store = _store(tmp_path)
    gateway = Gateway()
    snapshot = _activate_execution_fixture(store, gateway, baseline=getattr(request, "param", True))
    install_workflow(store, resolve_snapshot(7))
    gateway.calls.clear()
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    occurrence = store.active_dispatch_claims("o/r", "review")[0].claim_key
    selection = ReviewPreflightSelection("preflight:1", snapshot.revision, snapshot.execution.binding, occurrence, -9,
                                        ObservedPullRequest(snapshot.revision.fence.key, gateway.view.head_ref_oid,
                                                            PullRequestState.OPEN, gateway.view.labels, gateway.reviews, store.now()),
                                        Label.AI_PR_OPEN, ReviewMode.SINGLE, RuntimeName.CLAUDE, 5)
    control = SQLiteReviewExecutionControl(store, WorkflowWriter("o/r", "review", OWNER), gateway)
    return store, snapshot, gateway, selection, control


def dump(store):
    with sqlite3.connect(store.path) as db:
        return tuple(db.iterdump())


def current(store, selection):
    with store.transaction() as db:
        return load_review_workflow_row(db, selection.expected.fence.key)


def assert_unpaid(store):
    with store.transaction() as db:
        for table in ("runs", "leases", "budget_reservations", "charges", "lifecycle_commands"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0




def test_exact_preflight_replay_is_inert_after_claim_release_and_without_gateway_reads(rig):
    store, _, gateway, selection, control = rig
    first = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    before = dump(store)
    gateway.error = AssertionError("replay read gateway")
    reads = list(gateway.calls)
    restarted = SQLiteReviewExecutionControl(store, WorkflowWriter("o/r", "review", OWNER), gateway)
    assert restarted.preflight(selection) == first
    assert gateway.calls == reads and dump(store) == before
    assert_unpaid(store)


@pytest.mark.parametrize("field,value", [("failure_ceiling", 6), ("mode", ReviewMode.MULTI), ("runtime", RuntimeName.CODEX)])
def test_reused_preflight_identity_with_changed_selection_refuses_without_a_read(rig, field, value):
    store, _, gateway, selection, control = rig
    control.preflight(selection)
    before = dump(store)
    reads = list(gateway.calls)
    with pytest.raises(WorkflowAuthorityError, match="conflicting facts"):
        control.preflight(replace(selection, **{field: value}))
    assert dump(store) == before and gateway.calls == reads


def test_next_preflight_requires_current_aggregate_revision_and_allocates_next_sequence(rig):
    store, _, _, selection, control = rig
    control.preflight(selection)
    stale = replace(selection, request_id="preflight:2")
    before = dump(store)
    with pytest.raises(WorkflowRevisionConflict):
        control.preflight(stale)
    assert dump(store) == before
    updated = current(store, selection)
    second = control.preflight(replace(stale, expected=updated.revision))
    assert second.round.sequence == 2
    assert current(store, selection).execution.state.failures == 0
    assert_unpaid(store)


@pytest.mark.parametrize("corruption", ["released", "replacement", "missing_created", "changed_created", "wrong_owner", "wrong_leg"])
def test_preflight_refuses_absent_changed_or_unproven_parent_claim_without_writes(rig, corruption):
    store, _, _, selection, control = rig
    if corruption in {"released", "replacement"}:
        assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
        if corruption == "replacement":
            assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    else:
        with store.transaction() as db:
            if corruption == "missing_created":
                db.execute("DELETE FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' AND key=?",
                           (f"{selection.claim_occurrence_id}:created",))
            elif corruption == "changed_created":
                db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='dispatch-claim-lifecycle' AND key=?",
                           (f"{selection.claim_occurrence_id}:created",))
            else:
                field = "owner_id" if corruption == "wrong_owner" else "leg"
                db.execute(f"UPDATE dispatch_claims SET {field}='different'")
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="claim"):
        control.preflight(selection)
    assert dump(store) == before
    assert_unpaid(store)


def _measure_current_read(store, key):
    import time
    from issue_agent.adapters.sql_read_budget import bounded_sql_source_reads

    instructions = 0
    statements = []

    def progress():
        nonlocal instructions
        instructions += 1
        return 0

    with store.transaction() as db:
        db.set_progress_handler(progress, 1)
        db.set_trace_callback(statements.append)
        started = time.perf_counter()
        try:
            with bounded_sql_source_reads(db, 1024 * 1024) as budget:
                snapshot = load_review_workflow_row(db, key)
            elapsed = time.perf_counter() - started
        finally:
            db.set_progress_handler(None, 0)
            db.set_trace_callback(None)
        pending_query = next(statement for statement in statements if statement.startswith(
            'SELECT round_id,generation,sequence,plan_json,planning_json,closure_json,outcome_json ',
        ))
        plan = tuple(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + pending_query))
        assert any('review_execution_rounds_generation_pending' in step for step in plan)
        assert all('SCAN ' not in step for step in plan)
        gates_query = next(statement for statement in statements if statement.startswith(
            'SELECT * FROM workflow_gates WHERE ',
        ))
        gate_plan = tuple(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + gates_query))
        assert any('workflow_gates_current' in step for step in gate_plan)
        assert all('SCAN ' not in step and 'TEMP B-TREE' not in step for step in gate_plan)
        plan += gate_plan
    return snapshot, (budget.rows, budget.source_bytes, instructions, elapsed, plan)






def test_pending_capacity_open_gap_and_reordered_paid_returns_survive_restart(rig, monkeypatch):
    from issue_agent.adapters.control_store import SQLiteControlStore
    from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
    from issue_agent.adapters.workflow_rows import read_current_review_pending
    from issue_agent.domain import review_preflight
    from issue_agent.domain.contracts import Money
    from issue_agent.domain.outcome import AgentResult, ExitReason
    from tests.adapters.test_review_execution_admission import request
    from tests.adapters.test_review_execution_launch import register
    from tests.adapters.test_review_execution_return import returned, settlement
    from tests.adapters.test_review_execution_stage import stage

    # The domain suite exercises the real 4096-slot limit. A two-slot boundary
    # isolates adapter refusal, money and recovery without another long history.
    monkeypatch.setattr(review_preflight, 'MAX_EXECUTION_SUFFIX', 2)
    store, initial, gateway, selection, control = rig
    selection = replace(selection, failure_ceiling=initial.execution.state.failures + 2)
    rounds = []
    children = []
    pending_measurements = []
    for index in (1, 2):
        plan = control.preflight(replace(selection, request_id=f's7:paid:{index}',
                                         expected=current(store, selection).revision))
        child_stage = stage(plan)
        control.register_stage(plan.round.round_id, current(store, selection).revision, child_stage)
        proposed = request(child_stage.children[0], index=index, cap=200)
        assert store.reserve(proposed).admitted
        register(store, proposed)
        control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
        rounds.append(plan)
        children.append(proposed)
        pending_measurements.append(_measure_current_read(store, selection.expected.fence.key)[1])
    before_refusal = dump(store)
    with pytest.raises(ValueError, match='pending capacity'):
        control.preflight(replace(selection, request_id='s7:over-capacity',
                                  expected=current(store, selection).revision))
    assert dump(store) == before_refusal
    assert pending_measurements[1][0] > pending_measurements[0][0]
    assert pending_measurements[1][1] > pending_measurements[0][1]
    failed = AgentResult(True, status='error', exit_reason=ExitReason.AGENT_ERROR)
    later = returned(store, children[1], result=failed)
    later_payment = settlement(children[1], amount=13)
    assert not store.settle_review_return(later_payment, later).idempotent
    later_result = control.finalize_completed_round(rounds[1].round.round_id)
    with store.transaction() as db:
        waiting = load_review_workflow_row(db, selection.expected.fence.key)
        assert waiting.execution.baseline.through_sequence == 0
        assert waiting.execution.state.failures == initial.execution.state.failures + 1
        assert len(read_current_review_pending(db, waiting.execution)) == 2
    assert store.snapshot(children[0].scope).settled == Money('USD', 13)
    assert store.snapshot(children[0].scope).reserved == Money('USD', 60)
    assert store.receipt('unrelated-write', 'other/repository', 's7', {'value':'unrelated'})
    assert current(store, selection) == waiting
    reopened = SQLiteControlStore(store.path, now=store.now)
    reopened.initialize()
    recovered = SQLiteReviewExecutionControl(reopened, WorkflowWriter('o/r', 'review', OWNER), gateway)
    assert current(reopened, selection) == waiting
    assert recovered.finalize_completed_round(rounds[1].round.round_id) == later_result
    assert reopened.settle_review_return(later_payment, later).idempotent
    earlier = returned(reopened, children[0], result=failed)
    earlier_payment = settlement(children[0], amount=11)
    assert not reopened.settle_review_return(earlier_payment, earlier).idempotent
    first_result = recovered.finalize_completed_round(rounds[0].round.round_id)
    with reopened.transaction() as db:
        completed = load_review_workflow_row(db, selection.expected.fence.key)
        assert completed.execution.baseline.through_sequence == 2
        assert completed.execution.state.failures == initial.execution.state.failures + 2
        assert read_current_review_pending(db, completed.execution) == ()
        assert db.execute('SELECT COUNT(*),SUM(amount_micros) FROM charges').fetchone()[:] == (2, 24)
    assert reopened.snapshot(children[0].scope).settled == Money('USD', 24)
    assert reopened.snapshot(children[0].scope).reserved == Money('USD', 0)
    before_retry = dump(reopened)
    assert reopened.settle_review_return(earlier_payment, earlier).idempotent
    assert recovered.finalize_completed_round(rounds[0].round.round_id) == first_result
    assert recovered.finalize_completed_round(rounds[1].round.round_id) == later_result
    assert dump(reopened) == before_retry
    with pytest.raises(ValueError, match='allowance is exhausted'):
        recovered.preflight(replace(selection, request_id='s7:exhausted', expected=completed.revision))
    assert dump(reopened) == before_retry
    next_plan = recovered.preflight(replace(
        selection, request_id='s7:after-gap', expected=completed.revision,
        failure_ceiling=initial.execution.state.failures + 3,
    ))
    assert next_plan.round.sequence == 3


@pytest.mark.parametrize("change", ["head", "partial_reviews", "unavailable", "linked_identity"])
def test_fresh_remote_evidence_must_match_selection_and_claim_linkage(rig, change):
    store, _, gateway, selection, control = rig
    if change == "head":
        gateway.view = replace(gateway.view, head_ref_oid="d" * 40)
    elif change == "partial_reviews":
        gateway.reviews = PartialReviews("pagination failed")
    elif change == "unavailable":
        gateway.error = ConnectionError("offline")
    else:
        gateway.view = replace(gateway.view, closing_issue=7)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError):
        control.preflight(selection)
    assert dump(store) == before
    assert_unpaid(store)


def linked_selection(rig):
    store, _, gateway, selection, _ = rig
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    assert store.claim_dispatch("o/r", "review", 7, AgentKind.REVIEW.name, OWNER, frozenset())
    gateway.view = replace(gateway.view, closing_issue=7)
    return replace(selection, claim_target=7, claim_occurrence_id=store.active_dispatch_claims("o/r", "review")[0].claim_key)


@pytest.mark.parametrize("hold", ["label", "closed", "dependency", "unavailable"])
def test_linked_issue_hold_or_unavailable_evidence_refuses_before_round_allocation(rig, hold):
    store, _, gateway, _, control = rig
    selection = linked_selection(rig)
    if hold == "label":
        gateway.issue = replace(gateway.issue, labels=frozenset({Label.AI_PARKED}))
    elif hold == "closed":
        gateway.issue = replace(gateway.issue, state="CLOSED")
    elif hold == "dependency":
        gateway.dependencies = (99,)
    else:
        gateway.issue = None
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="linked issue"):
        control.preflight(selection)
    assert dump(store) == before
    assert_unpaid(store)


def test_gateway_read_occurs_outside_sql_write_lock_and_same_request_race_commits_once(rig):
    store, before, gateway, selection, control = rig
    winner = []

    def concurrent_preflight():
        with sqlite3.connect(store.path, timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        gateway.on_pr = None
        winner.append(control.preflight(selection))

    gateway.on_pr = concurrent_preflight
    assert control.preflight(selection) == winner[0]
    after = current(store, selection)
    assert after.revision.version == before.revision.version + 1 and after.execution.last_sequence == 1
    assert_unpaid(store)


def test_receipt_write_failure_rolls_back_round_and_revision_together(rig):
    store, _, _, selection, control = rig
    with store.transaction() as db:
        db.execute("CREATE TRIGGER refuse_preflight_receipt BEFORE INSERT ON dedupe_keys "
                   "WHEN NEW.namespace='review-round-preflight' BEGIN SELECT RAISE(ABORT,'receipt conflict'); END")
    before = dump(store)
    with pytest.raises(sqlite3.IntegrityError, match="receipt conflict"):
        control.preflight(selection)
    assert dump(store) == before
    with store.transaction() as db:
        db.execute("DROP TRIGGER refuse_preflight_receipt")
    assert control.preflight(selection).round.sequence == 1
    assert_unpaid(store)


def test_preflight_replay_refuses_a_missing_durable_round(rig):
    store, _, _, selection, control = rig
    control.preflight(selection)
    with store.transaction() as db:
        db.execute("DELETE FROM review_execution_rounds")
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="durable round"):
        control.preflight(selection)
    assert dump(store) == before


def test_unchanged_fresh_pr_read_remains_eligible_when_the_authority_clock_advances(rig, monkeypatch):
    from datetime import timedelta

    store, _, _, selection, control = rig
    monkeypatch.setattr(store, "_now", lambda: selection.observation.observed_at + timedelta(seconds=1))
    assert control.preflight(selection).round.sequence == 1
    assert_unpaid(store)


def test_linked_issue_sql_hold_that_lands_after_remote_read_is_checked_before_commit(rig, monkeypatch):
    from issue_agent.adapters import review_execution_control
    from issue_agent.adapters.workflow_rows import load_resolve_workflow_row, persist_workflow_snapshot
    from issue_agent.domain.workflow import EvidenceUnavailable, NoAuthorizedRemediation, OpenGate
    from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey

    store, _, _, _, control = rig
    selection = linked_selection(rig)
    original_read = review_execution_control.review_linked_issue_holds
    accepted_hold = []

    def hold_after_read(*args):
        holds = original_read(*args)
        with store.transaction() as db:
            issue = load_resolve_workflow_row(db, WorkflowKey("o/r", IssueRef(7)))
            gate = OpenGate("late-issue-hold", issue.revision.fence.generation,
                            EvidenceUnavailable("linked issue", store.now(), 3, "operator attention"),
                            NoAuthorizedRemediation("resolve this issue hold"))
            persist_workflow_snapshot(db, replace(issue, gates=(gate,)))
        accepted_hold.append(dump(store))
        return holds

    monkeypatch.setattr(review_execution_control, "review_linked_issue_holds", hold_after_read)
    with pytest.raises(WorkflowAuthorityError, match="linked issue workflow"):
        control.preflight(selection)
    assert dump(store) == accepted_hold[0]
    assert_unpaid(store)


def test_claim_replaced_during_gateway_read_cannot_allocate_using_old_occurrence(rig):
    store, _, gateway, selection, control = rig
    after_replacement = []

    def replace_claim():
        gateway.on_pr = None
        assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
        assert store.claim_dispatch("o/r", "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
        after_replacement.append(dump(store))

    gateway.on_pr = replace_claim
    with pytest.raises(WorkflowAuthorityError, match="claim"):
        control.preflight(selection)
    assert dump(store) == after_replacement[0]
    assert_unpaid(store)


@pytest.mark.parametrize("refusal", ["held_pr", "missing_trigger", "already_reviewed", "retired_owner"])
def test_policy_or_retired_authority_refusal_never_allocates_a_round(rig, refusal):
    store, _, gateway, selection, control = rig
    if refusal == "retired_owner":
        with store.transaction() as db:
            db.execute("INSERT INTO retired_owners VALUES (?,?,'o/r','review',?)",
                       (OWNER, "o/r:review:successor", store.now().isoformat()))
    else:
        if refusal == "held_pr":
            gateway.view = replace(gateway.view, labels=frozenset({Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW}))
        elif refusal == "missing_trigger":
            gateway.view = replace(gateway.view, labels=frozenset())
        else:
            gateway.view = replace(gateway.view, head_ref_oid="a" * 40)
        selection = replace(selection, observation=replace(selection.observation, head_sha=gateway.view.head_ref_oid,
                                                            labels=gateway.view.labels))
    before = dump(store)
    from issue_agent.adapters.control_store import RetiredOwnerError
    with pytest.raises((ValueError, RetiredOwnerError)):
        control.preflight(selection)
    assert dump(store) == before
    assert_unpaid(store)


def test_preflight_accepts_the_rereview_claim_leg(rig):
    store, _, _, selection, control = rig
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    assert store.claim_dispatch("o/r", "review", -9, AgentKind.RE_REVIEW.name, OWNER, frozenset())
    occurrence = store.active_dispatch_claims("o/r", "review")[0].claim_key
    plan = control.preflight(replace(selection, claim_occurrence_id=occurrence))
    assert plan.round.sequence == 1 and plan.round.binding == selection.binding
    assert_unpaid(store)


@pytest.mark.parametrize("label", [Label.NO_AI_REVIEW, Label.AI_PARKED])
def test_review_opt_out_and_parked_labels_hold_even_when_trigger_remains(rig, label):
    store, _, gateway, selection, control = rig
    gateway.view = replace(gateway.view, labels=frozenset({Label.AI_PR_OPEN, label}))
    selection = replace(selection, observation=replace(selection.observation, labels=gateway.view.labels))
    before = dump(store)
    with pytest.raises(ValueError):
        control.preflight(selection)
    assert dump(store) == before
    assert_unpaid(store)


def test_pending_current_generation_release_projection_blocks_preflight(rig):
    from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
    from issue_agent.domain.review_rejection import CountingRule
    from issue_agent.domain.workflow import WorkflowCommitted
    from tests.adapters.test_review_productive_settlement import confirmed_escalation

    store, gateway, selection, control, _, command = confirmed_escalation(rig, cause="ceiling")

    def workflow(ceiling):
        return SQLiteReviewWorkflowControl(store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway,
                                           ceiling=ceiling, trigger_label=Label.AI_PR_OPEN,
                                           rule=CountingRule("review-bot[bot]"))

    assert isinstance(workflow(1).settle_submission(command.command_id), WorkflowCommitted)
    assert isinstance(workflow(0).reconcile_ceiling(selection.expected.fence.key), WorkflowCommitted)
    released = current(store, selection)
    assert released.revision.fence.generation == selection.expected.fence.generation + 1
    selection = replace(selection, request_id="preflight:after-release", expected=released.revision,
                        binding=released.execution.binding)
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="unverified release projection"):
        control.preflight(selection)
    assert dump(store) == before
    assert current(store, selection) == released


def test_exact_preflight_replay_after_child_stage_planning_is_inert(rig):
    from issue_agent.domain.review_execution import ReviewChildIdentity, ReviewChildKind
    from issue_agent.domain.review_rounds import ReviewStagePlan

    store, _, gateway, selection, control = rig
    initial = control.preflight(selection)
    child = ReviewChildIdentity(initial.round, "review", "reviewer", ReviewChildKind.REVIEWER)
    control.register_stage(initial.round.round_id, current(store, selection).revision,
                           ReviewStagePlan("review", ReviewChildKind.REVIEWER, (child,)))
    before = dump(store)
    gateway.error = AssertionError("preflight replay read gateway")
    reads = list(gateway.calls)
    assert control.preflight(selection) == initial
    assert gateway.calls == reads and dump(store) == before


def test_retained_claim_of_an_exited_real_parent_cannot_authorize_a_new_round(rig):
    import subprocess
    import sys
    from issue_agent.adapters.process_supervisor import ProcessSupervisor

    store, _, _, selection, control = rig
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    child = subprocess.run(
        [sys.executable, "-c", """
import sys
from issue_agent.adapters.control_store import SQLiteControlStore
store = SQLiteControlStore(sys.argv[1])
store.initialize()
assert store.claim_dispatch('o/r', 'review', -9, 'REVIEW', sys.argv[2], frozenset())
print(store.active_dispatch_claims('o/r', 'review')[0].claim_key)
""", store.path, OWNER],
        capture_output=True, text=True, check=True,
    )
    occurrence = child.stdout.strip()
    assert occurrence
    with store.transaction() as db:
        row = db.execute("SELECT owner_pid,owner_start_ticks,occurrence_id FROM dispatch_claims").fetchone()
        assert row["occurrence_id"] == occurrence
    assert not ProcessSupervisor.matches_identity(row["owner_pid"], row["owner_start_ticks"])
    before = dump(store)
    with pytest.raises(WorkflowAuthorityError, match="claim|parent"):
        control.preflight(replace(selection, claim_occurrence_id=occurrence))
    assert dump(store) == before
    assert_unpaid(store)
