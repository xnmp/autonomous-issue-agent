"""Offline rehearsal of modern rounds after source-backed execution activation."""

import sqlite3

from issue_agent.adapters.control_store import AccountingDisposition
from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_rows import load_review_workflow_row
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.labels import Label
from issue_agent.domain.outcome import AgentResult, ExitReason
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_execution import (
    ReviewExecutionOriginKind, ReviewRoundCancelled, ReviewRoundFailure,
)
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.review_rounds import FinalizedReviewRound
from issue_agent.domain.review_workflow import ObservedPullRequest, PullRequestState
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey, WorkflowWriter
from tests.app.test_review_execution_activation import rig
from tests.adapters.test_review_execution_admission import request
from tests.adapters.test_review_execution_control import OWNER
from tests.adapters.test_review_execution_launch import register
from tests.adapters.test_review_execution_return import returned, settlement
from tests.adapters.test_review_execution_stage import stage


def test_imported_empty_origin_survives_paid_failure_and_later_abandonment(rig):
    cfg, store, gateway, _files_store, files, links, sql, _liveness, activation = rig
    activation_plan = activation.plan(files, links, sql)
    assert not activation.activate(activation_plan, files, links, sql).idempotent

    key = WorkflowKey(cfg.repo, PullRequestRef(9))
    with store.transaction() as db:
        imported = load_review_workflow_row(db, key)
        origin_receipts = db.execute(
            "SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-execution-origin' AND scope=?",
            (cfg.repo,),
        ).fetchone()[0]
    assert imported is not None
    assert imported.execution.origin.kind is ReviewExecutionOriginKind.IMPORTED
    assert origin_receipts == 2
    assert imported.execution.last_sequence == 0
    assert imported.execution.checkpoint.through_sequence == 0

    assert store.claim_dispatch(cfg.repo, "review", -9, AgentKind.REVIEW.name, OWNER, frozenset())
    occurrence = store.active_dispatch_claims(cfg.repo, "review")[0].claim_key
    control = SQLiteReviewExecutionControl(store, WorkflowWriter(cfg.repo, "review", OWNER), gateway)

    def select(request_id):
        with store.transaction() as db:
            current = load_review_workflow_row(db, key)
        assert current is not None
        view = gateway.views[9]
        return ReviewPreflightSelection(
            request_id, current.revision, current.execution.binding, occurrence, -9,
            ObservedPullRequest(key, view.head_ref_oid, PullRequestState.OPEN,
                                view.labels, gateway.reviews[9], store.now()),
            Label.AI_PR_OPEN, ReviewMode.SINGLE, RuntimeName.CLAUDE, 5,
        )

    first = control.preflight(select("rehearsal:first"))
    with store.transaction() as db:
        current = load_review_workflow_row(db, key)
    assert current is not None
    planned = control.register_stage(first.round.round_id, current.revision, stage(first))
    with store.transaction() as db:
        current = load_review_workflow_row(db, key)
    assert current is not None
    control.seal_completed_planning(first.round.round_id, current.revision)
    paid = request(planned.children[0])
    assert store.reserve(paid).admitted
    register(store, paid)
    failure = returned(store, paid, result=AgentResult(
        True, status="error", exit_reason=ExitReason.AGENT_ERROR,
    ))
    assert store.settle_review_return(settlement(paid), failure).disposition is AccountingDisposition.AVAILABLE

    second = control.preflight(select("rehearsal:second"))
    assert (first.round.sequence, second.round.sequence) == (1, 2)
    assert store.release_dispatch_claim(cfg.repo, "review", -9, OWNER)
    control.seal_abandoned_planning(second.round.round_id)
    abandoned = control.finalize_abandoned_round(second.round.round_id)
    assert isinstance(abandoned, FinalizedReviewRound)
    assert isinstance(abandoned.outcome.disposition, ReviewRoundCancelled)
    with store.transaction() as db:
        before_first = load_review_workflow_row(db, key)
    assert before_first is not None
    assert before_first.execution.state.failures == imported.execution.state.failures

    failed = control.finalize_completed_round(first.round.round_id)
    assert isinstance(failed, FinalizedReviewRound)
    assert isinstance(failed.outcome.disposition, ReviewRoundFailure)
    with store.transaction() as db:
        after = load_review_workflow_row(db, key)
        assert after is not None
        assert db.execute("SELECT COUNT(*) FROM review_execution_rounds WHERE outcome_json IS NOT NULL").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM budget_reservations WHERE state='active'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM dedupe_keys WHERE namespace='review-round-outcome'").fetchone()[0] == 2
    assert after.execution.last_sequence == 2
    assert after.execution.state.failures == imported.execution.state.failures + 1
    assert store.snapshot(paid.scope).committed.micros == 12
    assert store.snapshot(paid.scope).reserved.micros == 0

    gateway.unavailable = True
    with sqlite3.connect(store.path) as db:
        before_retry = tuple(db.iterdump())
    assert activation.activate(activation_plan, files, links, sql).idempotent
    with sqlite3.connect(store.path) as db:
        assert tuple(db.iterdump()) == before_retry
