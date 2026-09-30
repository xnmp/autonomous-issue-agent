"""Certified archive admission follows episode identity and an explicit deadline."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.review_archive_source_versions import ReviewArchiveSourceVersions
from issue_agent.domain.review_execution import (
    ActivatedReviewExecution, ReviewExecutionCheckpoint, ReviewExecutionOrigin,
    ReviewExecutionOriginKind, advance_review_execution_generation,
)
from issue_agent.domain.review_execution_archive_gate import (
    BlockedReviewExecutionArchive, CertifiedReviewExecutionArchive,
    certified_review_execution_archive, review_execution_archive_admission_refusal,
)
from issue_agent.domain.workflow import WorkflowMutation
from issue_agent.domain.workflow_identity import WorkflowFence, WorkflowRevision
from tests.domain.test_review_execution import activated


NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def _execution() -> ActivatedReviewExecution:
    initial = activated(origin=ReviewExecutionOriginKind.IMPORTED)
    checkpoint = ReviewExecutionCheckpoint(1, initial.state)
    return replace(initial, checkpoint=checkpoint, last_sequence=1,
                   checkpoint_receipt="checkpoint:one")


def _certificate(execution=None) -> CertifiedReviewExecutionArchive:
    execution = execution or _execution()
    return CertifiedReviewExecutionArchive(
        execution.origin, execution.binding, execution.checkpoint_receipt, execution.checkpoint,
        "a" * 64, "b" * 64, "c" * 64,
        ReviewArchiveSourceVersions("o/r", 1, 2, 3, 4, 5, 6), 7,
        NOW, NOW + timedelta(hours=1),
    )


def test_certified_current_episode_allows_admission_until_exact_deadline():
    execution = _execution()
    certificate = _certificate(execution)
    assert certified_review_execution_archive(certificate, execution) is certificate
    assert review_execution_archive_admission_refusal(certificate, execution, NOW) is None
    assert review_execution_archive_admission_refusal(
        certificate, execution, certificate.next_due_at - timedelta(microseconds=1),
    ) is None
    assert review_execution_archive_admission_refusal(
        certificate, execution, certificate.next_due_at,
    ) == "review execution archive audit is overdue"
    assert review_execution_archive_admission_refusal(
        certificate, execution, certificate.next_due_at + timedelta(seconds=1),
    ) == "review execution archive audit is overdue"
    with pytest.raises(FrozenInstanceError):
        setattr(certificate, "next_due_at", NOW)


def test_generation_carry_keeps_original_episode_certification():
    execution = _execution()
    certificate = _certificate(execution)
    from_revision = WorkflowRevision(execution.binding.rejection.fence, 4)
    to_revision = WorkflowRevision(
        WorkflowFence(from_revision.fence.key, from_revision.fence.generation + 1), 5,
    )
    progressed = advance_review_execution_generation(
        execution, from_revision, to_revision, WorkflowMutation.REQUEST_REARM, (),
    )
    assert progressed.binding != execution.binding
    assert certified_review_execution_archive(certificate, progressed) is certificate
    assert review_execution_archive_admission_refusal(certificate, progressed, NOW) is None


@pytest.mark.parametrize("change", ("origin", "episode", "checkpoint_receipt"))
def test_other_authority_cannot_use_certificate(change):
    execution = _execution()
    certificate = _certificate(execution)
    if change == "origin":
        changed = replace(execution, origin=ReviewExecutionOrigin(
            ReviewExecutionOriginKind.IMPORTED, "origin:other",
        ))
    elif change == "episode":
        binding = replace(execution.binding, execution_episode="execution:other")
        state = replace(execution.state, binding=binding)
        checkpoint = replace(execution.checkpoint, state=state)
        changed = replace(execution, checkpoint=checkpoint, state=state)
    else:
        changed = replace(execution, checkpoint_receipt="checkpoint:other")
    with pytest.raises(ValueError, match="does not match current execution authority"):
        certified_review_execution_archive(certificate, changed)
    with pytest.raises(ValueError, match="does not match current execution authority"):
        review_execution_archive_admission_refusal(certificate, changed, NOW)


@pytest.mark.parametrize("change", (
    {"origin_source_sha256": "A" * 64},
    {"checkpoint_source_sha256": "x"},
    {"application_source_sha256": ""},
    {"checkpoint_receipt_id": ""},
    {"checkpoint": None},
    {"checkpoint": "checkpoint:one"},
    {"source_versions": ReviewArchiveSourceVersions("other/repo", 1, 2, 3, 4, 5, 6)},
    {"source_versions": None},
    {"lifecycle_source_version": -1},
    {"lifecycle_source_version": True},
    {"certified_at": NOW.replace(tzinfo=None)},
    {"next_due_at": NOW},
))
def test_malformed_certification_is_rejected(change):
    with pytest.raises(ValueError):
        replace(_certificate(), **change)


def test_changed_checkpoint_is_rejected_even_with_same_receipt_id():
    execution = _execution()
    certificate = _certificate(execution)
    changed = replace(
        execution, checkpoint=ReviewExecutionCheckpoint(2, execution.state),
        last_sequence=2,
    )
    assert changed.checkpoint_receipt == execution.checkpoint_receipt
    with pytest.raises(ValueError, match="does not match current execution authority"):
        certified_review_execution_archive(certificate, changed)
    with pytest.raises(ValueError, match="does not match current execution authority"):
        review_execution_archive_admission_refusal(certificate, changed, NOW)


def test_foreign_checkpoint_cannot_be_certified():
    execution = _execution()
    foreign_binding = replace(execution.binding, execution_episode="execution:foreign")
    foreign_checkpoint = replace(
        execution.checkpoint,
        state=replace(execution.checkpoint.state, binding=foreign_binding),
    )
    with pytest.raises(ValueError, match="exact episode and source revisions"):
        replace(_certificate(execution), checkpoint=foreign_checkpoint)


def test_blocked_discrepancy_takes_precedence_over_overdue_but_not_integrity():
    execution = _execution()
    certificate = _certificate(execution)
    blocked = BlockedReviewExecutionArchive(certificate, "changed archived owner", NOW)
    assert certified_review_execution_archive(blocked, execution) is certificate
    assert review_execution_archive_admission_refusal(
        blocked, execution, certificate.next_due_at,
    ) == "review execution archive discrepancy: changed archived owner"
    with pytest.raises(ValueError, match="does not match current execution authority"):
        review_execution_archive_admission_refusal(
            blocked, replace(execution, checkpoint_receipt="checkpoint:other"),
            certificate.next_due_at,
        )


@pytest.mark.parametrize("change", (
    {"reason": ""}, {"reason": "x" * 513},
    {"detected_at": NOW - timedelta(microseconds=1)},
    {"detected_at": NOW.replace(tzinfo=None)},
))
def test_malformed_blocked_discrepancy_is_rejected(change):
    with pytest.raises(ValueError):
        replace(BlockedReviewExecutionArchive(_certificate(), "changed source", NOW), **change)


def test_premature_or_naive_clock_is_refused():
    execution = _execution()
    certificate = _certificate(execution)
    assert review_execution_archive_admission_refusal(
        certificate, execution, NOW - timedelta(microseconds=1),
    ) == "review execution archive certification is premature"
    with pytest.raises(ValueError, match="aware time"):
        review_execution_archive_admission_refusal(certificate, execution, NOW.replace(tzinfo=None))


def test_broad_source_revisions_do_not_renew_a_due_certificate():
    execution = _execution()
    certificate = _certificate(execution)
    changed_sources = replace(certificate, source_versions=ReviewArchiveSourceVersions(
        "o/r", 80, 81, 82, 83, 84, 85,
    ), lifecycle_source_version=90)
    assert changed_sources.next_due_at == certificate.next_due_at
    assert review_execution_archive_admission_refusal(
        changed_sources, execution, certificate.next_due_at,
    ) == "review execution archive audit is overdue"
