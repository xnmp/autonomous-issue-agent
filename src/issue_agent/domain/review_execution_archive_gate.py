"""Admission from a certified archived checkpoint, independently of recovery."""

from dataclasses import dataclass
from datetime import datetime

from .review_archive_source_versions import MAX_ARCHIVE_SOURCE_VERSION, ReviewArchiveSourceVersions
from .review_execution import (
    ActivatedReviewExecution, ReviewExecutionBinding, ReviewExecutionCheckpoint, ReviewExecutionOrigin,
    same_execution_episode,
)
from .workflow_identity import validate_workflow_identifier


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("archive certification requires aware time")


def _digest(value: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError("archive certification requires a SHA-256 source digest")


@dataclass(frozen=True)
class CertifiedReviewExecutionArchive:
    """Minted by the checkpoint writer or a complete transactional source audit.

    Source revisions attest the certification snapshot. Subsequent suffix writes
    do not renew its deadline; only a complete historical audit can do that.
    """

    origin: ReviewExecutionOrigin
    binding: ReviewExecutionBinding
    checkpoint_receipt_id: str
    checkpoint: ReviewExecutionCheckpoint
    origin_source_sha256: str
    checkpoint_source_sha256: str
    application_source_sha256: str
    source_versions: ReviewArchiveSourceVersions
    lifecycle_source_version: int
    certified_at: datetime
    next_due_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.origin, ReviewExecutionOrigin)
                or not isinstance(self.binding, ReviewExecutionBinding)
                or not isinstance(self.checkpoint, ReviewExecutionCheckpoint)
                or not same_execution_episode(self.binding, self.checkpoint.state.binding)
                or not isinstance(self.source_versions, ReviewArchiveSourceVersions)
                or self.source_versions.repository != self.binding.rejection.fence.key.repository
                or type(self.lifecycle_source_version) is not int
                or not 0 <= self.lifecycle_source_version <= MAX_ARCHIVE_SOURCE_VERSION):
            raise ValueError("archive certification requires its exact episode and source revisions")
        validate_workflow_identifier(self.checkpoint_receipt_id, "certified checkpoint")
        for value in (self.origin_source_sha256, self.checkpoint_source_sha256,
                      self.application_source_sha256):
            _digest(value)
        _aware(self.certified_at)
        _aware(self.next_due_at)
        if not self.certified_at < self.next_due_at:
            raise ValueError("archive certification requires a future audit deadline")


@dataclass(frozen=True)
class BlockedReviewExecutionArchive:
    prior: CertifiedReviewExecutionArchive
    reason: str
    detected_at: datetime

    def __post_init__(self) -> None:
        if (not isinstance(self.prior, CertifiedReviewExecutionArchive)
                or not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512):
            raise ValueError("blocked archive requires its prior certification and discrepancy")
        _aware(self.detected_at)
        if self.detected_at < self.prior.certified_at:
            raise ValueError("archive discrepancy predates its certification")


ReviewExecutionArchiveGate = CertifiedReviewExecutionArchive | BlockedReviewExecutionArchive


def certified_review_execution_archive(
    gate: ReviewExecutionArchiveGate, execution: ActivatedReviewExecution,
) -> CertifiedReviewExecutionArchive:
    if not isinstance(gate, (CertifiedReviewExecutionArchive, BlockedReviewExecutionArchive)):
        raise ValueError("archive gate requires a trusted typed certification")
    certificate = gate.prior if isinstance(gate, BlockedReviewExecutionArchive) else gate
    if (not isinstance(execution, ActivatedReviewExecution)
            or certificate.origin != execution.origin
            or not same_execution_episode(certificate.binding, execution.binding)
            or certificate.checkpoint_receipt_id != execution.checkpoint_receipt
            or certificate.checkpoint != execution.checkpoint):
        raise ValueError("archive certification does not match current execution authority")
    return certificate


def review_execution_archive_admission_refusal(
    gate: ReviewExecutionArchiveGate, execution: ActivatedReviewExecution, now: datetime,
) -> str | None:
    certificate = certified_review_execution_archive(gate, execution)
    _aware(now)
    if now < certificate.certified_at:
        return "review execution archive certification is premature"
    if isinstance(gate, BlockedReviewExecutionArchive):
        return "review execution archive discrepancy: " + gate.reason
    if now >= certificate.next_due_at:
        return "review execution archive audit is overdue"
    return None
