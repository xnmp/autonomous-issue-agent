"""The composed review daemon finalizes an abandoned execution round during recovery."""

from issue_agent.adapters.review_effects import read_pull_request
from issue_agent.adapters.review_execution_control import SQLiteReviewExecutionControl
from issue_agent.adapters.workflow_codec import encode_workflow, decode_workflow
from issue_agent.adapters.workflow_rows import load_review_workflow_row
from issue_agent.domain.actions import AgentKind
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.labels import Label
from issue_agent.domain.recovery import RecoveryTrigger
from issue_agent.domain.review import ReviewMode
from issue_agent.domain.review_execution import (
    ActivatedReviewExecution, ReviewExecutionBinding, ReviewExecutionCheckpoint,
    ReviewExecutionOrigin, ReviewExecutionOriginKind, ReviewExecutionPolicy, ReviewExecutionState,
)
from issue_agent.domain.review_preflight import ReviewPreflightSelection
from issue_agent.domain.review_rounds import ReviewParentAbandoned
from issue_agent.domain.review_workflow import ObservedPullRequest, ReviewFamilyEpisodes
from issue_agent.domain.workflow_identity import WorkflowWriter
from tests.app.review_acceptance_harness import KEY, PR, REPO, compose_review, create_review_deployment
from tests.app.resolve_acceptance_harness import install_clock, isolate_authority


def test_review_daemon_recovery_fences_a_round_from_its_released_parent_claim(tmp_path, monkeypatch):
    deployment = create_review_deployment(tmp_path)
    isolate_authority(deployment, monkeypatch)
    install_clock(deployment, monkeypatch)
    deployment.activate()
    composed = compose_review(deployment)
    review = composed.review
    composed.workflow.ensure_observed_workflow(KEY)
    with review.control.transaction() as db:
        prior = load_review_workflow_row(db, KEY)
        assert prior is not None
        binding = ReviewExecutionBinding(prior.binding, 1, "execution:recovery")
        state = ReviewExecutionState(binding, failures=0, pin_deferrals=0)
        execution = ActivatedReviewExecution(
            ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "import:recovery"),
            ReviewExecutionCheckpoint(0, state, ReviewExecutionPolicy(1)), state, 0,
        )
        db.execute("UPDATE workflows SET episode_json=? WHERE repository=? AND target_kind='pr' AND target_number=?",
                   (encode_workflow(ReviewFamilyEpisodes(prior.authority, prior.episode, execution)), REPO, PR))
        db.execute("INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   (REPO, "review_execution", 1, "test", "test", deployment.clock.now().isoformat()))
        db.execute("INSERT INTO workflow_components VALUES (?,?,?,?,?,?)",
                   (REPO, "pr", PR, "review_execution", 1, "execution:recovery"))
        snapshot = load_review_workflow_row(db, KEY)
        assert snapshot is not None
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
    assert any("finalized 1 abandoned parent round" in line for line in composed.log)
