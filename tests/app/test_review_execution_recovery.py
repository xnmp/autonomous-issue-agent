"""The composed review daemon finalizes an abandoned execution round during recovery."""

from issue_agent.adapters.review_effects import read_pull_request
from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_codec import decode_workflow
from issue_agent.adapters.workflow_rows import load_review_workflow_row
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.labels import Label
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_execution import CurrentReviewExecution
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.review_rounds import ReviewParentAbandoned
from issue_agent.domain.review_workflow import ObservedPullRequest
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.app.review_acceptance_harness import KEY, PR, REPO, compose_review, create_review_deployment
from tests.app.acceptance_support import install_clock, isolate_authority


def test_review_daemon_recovery_fences_a_round_from_its_released_parent_claim(tmp_path, monkeypatch):
    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    composed = compose_review(deployment)
    review = composed.review
    composed.activate_execution(monkeypatch)
    with review.control.transaction() as db:
        snapshot = load_review_workflow_row(db, KEY)
        assert snapshot is not None and isinstance(snapshot.execution, CurrentReviewExecution)
    assert review.control.claim_dispatch(REPO, "review", 1, AgentKind.REVIEW.name,
                                         review.owner_id, frozenset())
    claim = review.control.active_dispatch_claims(REPO, "review")[0]
    observed, view = read_pull_request(composed.remote, KEY, deployment.clock.now(), reviews=True)
    assert isinstance(observed, ObservedPullRequest) and view is not None
    selection = ReviewPreflightSelection(
        "recovery:round", snapshot.revision, snapshot.execution.binding, claim.claim_key, 1,
        observed, Label.AI_PR_OPEN, ReviewMode.SINGLE, RuntimeName.CLAUDE, 5,
    )
    writer = SQLiteReviewExecutionControl(
        review.control, WorkflowWriter(REPO, "review", review.owner_id), composed.remote,
    )
    plan = writer.preflight(selection)
    assert review.control.release_dispatch_claim(REPO, "review", 1, review.owner_id)

    review.recovery.reconcile(RecoveryTrigger.PERIODIC)

    with review.control.transaction() as db:
        row = db.execute("SELECT planning_json,closure_json,outcome_json FROM review_execution_rounds WHERE round_id=?",
                         (plan.round.round_id,)).fetchone()
    abandoned = decode_workflow(row["planning_json"], ReviewParentAbandoned)
    assert abandoned.abandonment_receipt.endswith(":released")
    assert row["closure_json"] is not None and row["outcome_json"] is not None
    assert any("fenced 1 abandoned parent round" in line for line in composed.log)
    assert any("finalized 1 parent round" in line for line in composed.log)
