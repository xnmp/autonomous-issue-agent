"""Archived round lineage observes retained SQL sources from real controls."""

from dataclasses import replace

import pytest

from issue_agent.adapters.review_execution_archive_round import (
    observe_review_execution_archive_round,
)
from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.effects import bound_review_command_id
from issue_agent.adapters.review_effects import BoundReviewSubmissionOp
from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.contracts import CommandKind, EffectReceipt, EffectStatus, LifecycleCommand
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.review_rounds import FinalizedReviewRound, ReviewSubmissionKind
from issue_agent.domain.review_workflow import ObservedPullRequest, PullRequestState
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey, WorkflowWriter
from tests.app.test_review_execution_activation import rig as activation_rig
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_control import OWNER, current
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_execution_stage import stage
from tests.adapters.test_review_execution_submission import command_for
from tests.adapters.test_review_productive_settlement import (
    accepted_generation_with_open_submission, confirmed_escalation, workflow_for,
)
from issue_agent.adapters.review_workflow_control import SQLiteReviewWorkflowControl
from issue_agent.domain.review_rejection import CountingRule
from issue_agent.domain.workflow import WorkflowCommitted, WorkflowRejected


@pytest.fixture
def rig(activation_rig):
    cfg, store, gateway, _, files, links, sql, _, activation = activation_rig
    activation.activate(activation.plan(files, links, sql), files, links, sql)
    gateway.views[9] = replace(gateway.views[9], head_ref_oid="b" * 40)
    key = WorkflowKey(cfg.repo, PullRequestRef(9))
    with store.transaction() as db:
        snapshot = load_review_workflow_row(db, key)
    assert snapshot is not None
    assert store.claim_dispatch(cfg.repo, "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    occurrence = store.active_dispatch_claims(cfg.repo, "review")[0].claim_key
    view = gateway.views[9]
    selection = ReviewPreflightSelection(
        "archive:first", snapshot.revision, snapshot.execution.binding, occurrence, -9,
        ObservedPullRequest(key, view.head_ref_oid, PullRequestState.OPEN,
                            view.labels, gateway.reviews[9], store.now()),
        Label.AI_PR_OPEN, ReviewMode.SINGLE, RuntimeName.CLAUDE, 5,
    )
    control = SQLiteReviewExecutionControl(store, WorkflowWriter(cfg.repo, "review", OWNER), gateway)
    return store, snapshot, gateway, selection, control


def _covered_rig(rig):
    store, _, gateway, selection, control = rig
    gateway.views[9] = replace(gateway.views[9], head_ref_oid="a" * 40)
    selection = replace(selection, observation=replace(selection.observation, head_sha="a" * 40))
    paid_plan = control.preflight(selection)
    paid_stage = stage(paid_plan)
    control.register_stage(paid_plan.round.round_id, current(store, selection).revision, paid_stage)
    paid = request(paid_stage.children[0])
    assert store.reserve(paid).admitted
    register(store, paid)
    store.settle_review_return(settlement(paid), returned(store, paid))
    binding = current(store, selection).binding
    op = BoundReviewSubmissionOp(9, "APPROVE", "approved", "a" * 40,
                                 "review-bot", "c" * 64, None)
    command_id = bound_review_command_id(op, binding)
    command = LifecycleCommand(command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
                               "pr:9", op.head, op.payload(), "test", "o/r", OWNER,
                               workflow=binding)
    control.register_submission(paid_plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.PAID)
    control.seal_completed_planning(paid_plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:paid"))
    workflow_for(store, gateway).record_review(command.command_id)
    control.finalize_completed_round(paid_plan.round.round_id)
    gateway.views[9] = replace(gateway.views[9], head_ref_oid="b" * 40)
    updated = current(store, selection)
    next_selection = replace(
        selection, request_id="archive:productive", expected=updated.revision,
        binding=updated.execution.binding,
        observation=replace(selection.observation, head_sha="b" * 40, observed_at=store.now()),
    )
    return store, updated, gateway, next_selection, control


def test_archived_abandoned_round_has_stable_source_and_detects_changed_claim(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    assert isinstance(control.finalize_abandoned_round(plan.round.round_id), FinalizedReviewRound)
    with store.transaction() as db:
        observed = observe_review_execution_archive_round(db, plan.round)
        assert observe_review_execution_archive_round(db, plan.round) == observed
        claim_key = f"{selection.claim_occurrence_id}:released"
        original = db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' "
            "AND key=?", (claim_key,),
        ).fetchone()[0]
        db.execute(
            "UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='dispatch-claim-lifecycle' "
            "AND key=?", (claim_key,),
        )
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='dispatch-claim-lifecycle' "
                   "AND key=?", (original, claim_key))
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-round-preflight' "
                   "AND key=?", ("x" * (2**21 + 1), plan.round.round_id))
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)


def test_paid_failed_round_binds_charge_and_return(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    planned = control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    proposed = request(planned.children[0])
    assert store.reserve(proposed).admitted
    register(store, proposed)
    store.settle_review_return(settlement(proposed), returned(
        store, proposed, result=AgentResult(True, status="error", exit_reason=ExitReason.AGENT_ERROR),
    ))
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    assert isinstance(control.finalize_completed_round(plan.round.round_id), FinalizedReviewRound)
    with store.transaction() as db:
        observed = observe_review_execution_archive_round(db, plan.round)
        db.execute("UPDATE charges SET amount_micros=amount_micros+1 WHERE run_id=?", (str(proposed.run_id),))
        changed = observe_review_execution_archive_round(db, plan.round)
        db.execute(
            "UPDATE dedupe_keys SET claimed_at=claimed_at||'.000001' "
            "WHERE namespace='review-execution-child-admission'",
        )
        changed_index = observe_review_execution_archive_round(db, plan.round)
        db.execute("UPDATE reservation_limits SET cap_micros=cap_micros+1 WHERE run_id=?",
                   (str(proposed.run_id),))
        changed_limit = observe_review_execution_archive_round(db, plan.round)
        db.execute(
            "INSERT INTO run_cancellation_intents "
            "(run_id,dispatch_id,kind,issue_number,reason,created_at) "
            "SELECT run_id,dispatch_id,kind,NULL,'operator',completed_at FROM runs WHERE run_id=?",
            (str(proposed.run_id),),
        )
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)
        db.execute(
            "UPDATE run_cancellation_intents SET handover_written_at=created_at WHERE run_id=?",
            (str(proposed.run_id),),
        )
        terminal_intent = observe_review_execution_archive_round(db, plan.round)
        db.execute(
            "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
            "SELECT namespace,scope,CAST(key AS BLOB),claimed_at,payload_json "
            "FROM dedupe_keys WHERE namespace='review-execution-child-admission'",
        )
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)
    assert changed.source_sha256 != observed.source_sha256
    assert changed_index.source_sha256 != changed.source_sha256
    assert changed_limit.source_sha256 != changed_index.source_sha256
    assert terminal_intent.source_sha256 != changed_limit.source_sha256


def test_refused_child_has_no_fabricated_paid_source(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    planned = control.register_stage(plan.round.round_id, current(store, selection).revision, stage(plan))
    proposed = request(planned.children[0], cap=59)
    assert not store.reserve(proposed).admitted
    assert store.record_review_admission_refusal(proposed) is not None
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        observed = observe_review_execution_archive_round(db, plan.round)
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
        db.execute("UPDATE dedupe_keys SET payload_json='{}' "
                   "WHERE namespace='review-execution-refusal'")
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)
    assert observed.source_bytes > 0


@pytest.mark.parametrize("damage", ("command", "accepted"))
def test_copied_productive_round_observes_command_and_settlement(rig, damage):
    store, _, gateway, selection, control = rig
    paid_plan = control.preflight(selection)
    paid_stage = stage(paid_plan)
    control.register_stage(paid_plan.round.round_id, current(store, selection).revision, paid_stage)
    paid = request(paid_stage.children[0])
    assert store.reserve(paid).admitted
    register(store, paid)
    store.settle_review_return(settlement(paid), returned(store, paid))
    paid_command = command_for(current(store, selection), patch_id="0" * 64)
    control.register_submission(paid_plan.round.round_id, current(store, selection).revision,
                                paid_command, ReviewSubmissionKind.PAID)
    control.seal_completed_planning(paid_plan.round.round_id, current(store, selection).revision)
    store.begin_command(paid_command.command_id)
    store.record_effect(EffectReceipt(paid_command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:paid"))
    workflow_for(store, gateway).record_review(paid_command.command_id)
    control.finalize_completed_round(paid_plan.round.round_id)

    gateway.views[9] = replace(gateway.views[9], head_ref_oid="c" * 40)
    updated = current(store, selection)
    next_selection = replace(
        selection, request_id="archive:copy", expected=updated.revision,
        binding=updated.execution.binding,
        observation=replace(selection.observation, head_sha="c" * 40, observed_at=store.now()),
    )
    plan = control.preflight(next_selection)
    op = BoundReviewSubmissionOp(9, "APPROVE", "approved", "c" * 40,
                                 "review-bot", "0" * 64, None)
    binding = current(store, next_selection).binding
    command_id = bound_review_command_id(op, binding)
    command = LifecycleCommand(command_id, f"github:{command_id}", CommandKind.SUBMIT_REVIEW,
                               "pr:9", op.head, op.payload(), "test", "o/r", OWNER,
                               workflow=binding)
    control.register_submission(plan.round.round_id, current(store, selection).revision,
                                command, ReviewSubmissionKind.COPIED)
    control.seal_completed_planning(plan.round.round_id, current(store, selection).revision)
    store.begin_command(command.command_id)
    store.record_effect(EffectReceipt(command.command_id, OWNER, store.now(), store.now(),
                                      EffectStatus.SUCCEEDED, remote_object_id="review:9"))
    workflow_for(store, gateway).record_review(command.command_id)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.finalize_completed_round(plan.round.round_id)
    with store.transaction() as db:
        observed = observe_review_execution_archive_round(db, plan.round)
        if damage == "command":
            db.execute("UPDATE lifecycle_commands SET payload_digest='0' WHERE command_id=?",
                       (str(command.command_id),))
        else:
            db.execute("UPDATE dedupe_keys SET payload_json='{}' "
                       "WHERE namespace='workflow-transition' AND scope='o/r'")
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)
    assert observed.source_bytes > 0


def test_round_lineage_rejects_small_cumulative_budget(rig):
    store, _, _, selection, control = rig
    plan = control.preflight(selection)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    control.seal_abandoned_planning(plan.round.round_id)
    control.finalize_abandoned_round(plan.round.round_id)
    with store.transaction() as db:
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round, max_source_bytes=1)


def test_stale_productivity_observes_generation_crossing(rig):
    store, _, selection, _, plan, command, workflow = accepted_generation_with_open_submission(_covered_rig(rig))
    assert isinstance(workflow.settle_submission(command.command_id), WorkflowRejected)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        observed = observe_review_execution_archive_round(db, plan.round)
        db.execute("UPDATE dedupe_keys SET payload_json='{}' "
                   "WHERE namespace='review-execution-generation'")
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)
    assert observed.source_bytes > 0


def test_superseded_productivity_observes_exact_pr_evidence(rig):
    store, gateway, selection, _, plan, command = confirmed_escalation(_covered_rig(rig))
    gateway.views[9] = replace(gateway.views[9], head_ref_oid="d" * 40)
    assert isinstance(workflow_for(store, gateway).settle_submission(command.command_id), WorkflowRejected)
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        observed = observe_review_execution_archive_round(db, plan.round)
        db.execute("UPDATE workflow_evidence SET payload_json='{}' WHERE kind='observation'")
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)
    assert observed.source_bytes > 0


def test_ceiling_productivity_observes_replayed_decision(rig):
    store, gateway, selection, _, plan, command = confirmed_escalation(_covered_rig(rig), cause="ceiling")
    workflow = SQLiteReviewWorkflowControl(
        store, WorkflowWriter("o/r", "review", OWNER), gateway=gateway, ceiling=1,
        trigger_label=Label.AI_PR_OPEN, rule=CountingRule("review-bot[bot]"),
    )
    assert isinstance(workflow.settle_submission(command.command_id), (WorkflowCommitted, WorkflowRejected))
    assert store.release_dispatch_claim("o/r", "review", -9, OWNER)
    with store.transaction() as db:
        observed = observe_review_execution_archive_round(db, plan.round)
        db.execute("UPDATE workflow_evidence SET payload_json='{}' WHERE kind='observation'")
        with pytest.raises(WorkflowAuthorityError):
            observe_review_execution_archive_round(db, plan.round)
    assert observed.source_bytes > 0
