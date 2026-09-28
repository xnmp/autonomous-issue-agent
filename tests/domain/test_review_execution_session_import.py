"""Legacy session files remain evidence, never a paid admission."""

from dataclasses import replace
import hashlib

import pytest

from issue_agent.domain.resolve_activation import LegacyAbsent, LegacyPresent
from issue_agent.domain.review_execution import NoReviewSession, ReviewExecutionBinding
from issue_agent.domain.review_execution_activation import (
    CompleteLegacyExecutionFileValues, LegacyExecutionPRRecord, classify_legacy_execution_file_values,
)
from issue_agent.domain.review_execution_session_import import (
    UnprovenLegacyReviewSession, plan_legacy_review_session_import,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)


def _source(text: str) -> LegacyPresent:
    return LegacyPresent(hashlib.sha256(text.encode()).hexdigest(), text)


def _files(session: str | None, *, pr: int = 7, root: str = "review") -> CompleteLegacyExecutionFileValues:
    record = LegacyExecutionPRRecord(pr, _source(session) if session is not None else LegacyAbsent(),
                                     _source("1") if session is None else LegacyAbsent(), root)
    result = classify_legacy_execution_file_values((record,))
    assert isinstance(result, CompleteLegacyExecutionFileValues)
    return result


def _binding(*, repository: str = "acme/widget", pr: int = 7) -> ReviewExecutionBinding:
    parent = WorkflowBinding(
        WorkflowFence(WorkflowKey(repository, PullRequestRef(pr)), 2),
        FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 1), "review:7",
    )
    return ReviewExecutionBinding(parent, 1, "execution:7")


def test_real_handle_retains_repository_root_and_file_source_without_granting_reuse():
    files = _files("claude:session-7")
    result = plan_legacy_review_session_import(files, "c" * 64, _binding())

    assert isinstance(result, UnprovenLegacyReviewSession)
    assert result.repository == "acme/widget"
    assert result.pr == 7 and result.root == "review"
    assert result.roots_digest == "c" * 64
    assert result.candidate.handle == "session-7"
    assert result.candidate.file_digest == files.prs[0].session.file_digest
    assert "latest producing review" in result.reason
    assert result != plan_legacy_review_session_import(files, "d" * 64, _binding())
    assert result != plan_legacy_review_session_import(files, "c" * 64, _binding(repository="acme/other"))


@pytest.mark.parametrize("session", (None, "codex:codex-review", "multi"))
def test_absent_or_mode_marker_never_becomes_a_reusable_session(session):
    result = plan_legacy_review_session_import(_files(session), "c" * 64, _binding())
    assert isinstance(result, NoReviewSession)


def test_real_handle_stays_unproven_even_with_a_round_file():
    files = classify_legacy_execution_file_values((
        LegacyExecutionPRRecord(7, _source("claude:old-session"), _source("3")),
    ))
    assert isinstance(files, CompleteLegacyExecutionFileValues)
    result = plan_legacy_review_session_import(files, "c" * 64, _binding())
    assert isinstance(result, UnprovenLegacyReviewSession)
    assert result.candidate.handle == "old-session"


def test_session_for_another_pr_is_not_imported():
    result = plan_legacy_review_session_import(_files("claude:session-7", pr=8), "c" * 64, _binding())
    assert isinstance(result, NoReviewSession)


def test_source_identity_rejects_invalid_digest_or_candidate():
    result = plan_legacy_review_session_import(_files("claude:session-7"), "c" * 64, _binding())
    assert isinstance(result, UnprovenLegacyReviewSession)
    with pytest.raises(ValueError, match="complete file source"):
        replace(result, roots_digest="bad")
    with pytest.raises(ValueError, match="root source digest"):
        plan_legacy_review_session_import(_files("claude:session-7"), "bad", _binding())
