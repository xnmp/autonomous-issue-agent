"""Session file observations stay tied to a fresh configured-root read."""

from issue_agent.adapters.review_execution_activation import (
    observe_legacy_review_session_import, read_complete_legacy_execution_root_files,
)
from issue_agent.domain.review_execution import NoReviewSession, ReviewExecutionBinding
from issue_agent.domain.review_execution_session_import import (
    BlockedLegacyReviewSessionImport, UnprovenLegacyReviewSession,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)


def _binding() -> ReviewExecutionBinding:
    parent = WorkflowBinding(
        WorkflowFence(WorkflowKey("acme/widget", PullRequestRef(7)), 2),
        FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1), "review:7",
    )
    return ReviewExecutionBinding(parent, 1, "execution:7")


def test_session_observation_rechecks_complete_root_before_retaining_evidence(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    session = state / "pr-7.session"
    session.write_text("claude:session-7")
    roots = (("review", str(state)),)
    observed = read_complete_legacy_execution_root_files(roots)

    unproven = observe_legacy_review_session_import(roots, observed, _binding(), review_state_dir=str(state))
    assert isinstance(unproven, UnprovenLegacyReviewSession)
    assert unproven.candidate.handle == "session-7"
    assert unproven.roots_digest == observed.source_digest

    session.write_text("claude:other-session")
    changed = observe_legacy_review_session_import(roots, observed, _binding(), review_state_dir=str(state))
    assert isinstance(changed, BlockedLegacyReviewSessionImport)
    assert "changed" in changed.reason


def test_session_observation_refuses_changed_root_even_with_equal_file_bytes(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for state in (first, second):
        (state / "pr-7.session").write_text("claude:session-7")
    pointer = tmp_path / "state"
    pointer.symlink_to(first, target_is_directory=True)
    roots = (("review", str(pointer)),)
    observed = read_complete_legacy_execution_root_files(roots)

    pointer.unlink()
    pointer.symlink_to(second, target_is_directory=True)
    changed = observe_legacy_review_session_import(roots, observed, _binding(), review_state_dir=str(pointer))

    assert isinstance(changed, BlockedLegacyReviewSessionImport)
    assert "changed" in changed.reason


def test_session_observation_uses_only_the_physical_review_root(tmp_path):
    fix = tmp_path / "fix"
    review = tmp_path / "review"
    fix.mkdir()
    review.mkdir()
    (fix / "pr-7.session").write_text("claude:fix-session")
    roots = (("fix", str(fix)), ("review", str(review)))
    observed = read_complete_legacy_execution_root_files(roots)

    absent = observe_legacy_review_session_import(
        roots, observed, _binding(), review_state_dir=str(review), other_role_state_dirs=(str(fix),),
    )
    assert isinstance(absent, NoReviewSession)

    (review / "pr-7.session").write_text("claude:review-session")
    observed = read_complete_legacy_execution_root_files(roots)
    unproven = observe_legacy_review_session_import(
        roots, observed, _binding(), review_state_dir=str(review), other_role_state_dirs=(str(fix),),
    )
    assert isinstance(unproven, UnprovenLegacyReviewSession)
    assert unproven.candidate.handle == "review-session"


def test_session_observation_blocks_an_open_pr_file_in_a_historical_root(tmp_path):
    review = tmp_path / "review"
    old = tmp_path / "old-review"
    review.mkdir()
    old.mkdir()
    (old / "pr-7.session").write_text("claude:old-session")
    roots = (("review", str(review)), (f"extra:{old.resolve()}", str(old)))
    observed = read_complete_legacy_execution_root_files(roots)

    result = observe_legacy_review_session_import(
        roots, observed, _binding(), review_state_dir=str(review),
    )
    assert isinstance(result, BlockedLegacyReviewSessionImport)
    assert "historical" in result.reason


def test_session_observation_does_not_trust_a_foreign_root_label(tmp_path):
    review = tmp_path / "review"
    old = tmp_path / "old-review"
    review.mkdir()
    old.mkdir()
    (old / "pr-7.session").write_text("claude:old-session")
    roots = (("review", str(review)), ("fix", str(old)))
    observed = read_complete_legacy_execution_root_files(roots)

    result = observe_legacy_review_session_import(
        roots, observed, _binding(), review_state_dir=str(review),
    )
    assert isinstance(result, BlockedLegacyReviewSessionImport)
    assert "historical" in result.reason
