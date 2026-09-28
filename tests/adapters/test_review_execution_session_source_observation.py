"""Session file observations stay tied to a fresh configured-root read."""

from issue_agent.adapters.review_execution_activation import (
    observe_legacy_review_session_import, read_complete_legacy_execution_root_files,
)
from issue_agent.domain.review_execution import ReviewExecutionBinding
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

    unproven = observe_legacy_review_session_import(roots, observed, _binding())
    assert isinstance(unproven, UnprovenLegacyReviewSession)
    assert unproven.candidate.handle == "session-7"
    assert unproven.roots_digest == observed.source_digest

    session.write_text("claude:other-session")
    changed = observe_legacy_review_session_import(roots, observed, _binding())
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
    changed = observe_legacy_review_session_import(roots, observed, _binding())

    assert isinstance(changed, BlockedLegacyReviewSessionImport)
    assert "changed" in changed.reason
