"""Source-backed ownership of one retained review round receipt."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..domain.review_execution import ReviewExecutionCompactionRoundSource, ReviewRoundIdentity
from ..domain.review_receipt_attribution import (
    BoundReviewRoundReceipt, CommandReviewRoundReceipt, NamedReviewRoundReceipt,
    ReviewRoundReceiptNamespace,
)
from ..domain.review_rounds import (
    ReviewParentAbandoned, ReviewPlanningComplete, ReviewPlanningOpen,
    ReviewPlanningSealed,
    ReviewRoundPlan, ReviewSealedParentAbandoned,
)
from .review_execution_activation import (
    _printable, _read_recording_source, read_checkpoint_round_receipt_members,
    read_checkpoint_round_row_source,
)
from .review_round_receipt_sweep import (
    ClassifiedReviewRoundReceiptSweepSource, ReviewRoundReceiptSweepCursor,
    _classify_source_payload,
)
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, decode_workflow, encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, review_stage_receipt_key, review_submission_receipt_key,
)


@dataclass(frozen=True)
class MatchedReviewRoundReceiptOwner:
    source: ClassifiedReviewRoundReceiptSweepSource
    round: ReviewRoundIdentity
    round_source: ReviewExecutionCompactionRoundSource
    registration_source_sha256: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.source, ClassifiedReviewRoundReceiptSweepSource)
                or not isinstance(self.round, ReviewRoundIdentity)
                or not isinstance(self.round_source, ReviewExecutionCompactionRoundSource)
                or self.round_source.round != self.round
                or self.round.binding.rejection.fence.key.repository
                != self.source.source.cursor.scope
                or isinstance(self.source.reference, BoundReviewRoundReceipt)
                and self.source.reference.round != self.round
                or isinstance(self.source.reference, NamedReviewRoundReceipt)
                and self.source.reference.round_id != self.round.round_id
                or isinstance(self.source.reference, CommandReviewRoundReceipt)
                != (self.registration_source_sha256 is not None)):
            raise ValueError("matched receipt owner requires its exact round and source")
        if self.registration_source_sha256 is not None and (
            not isinstance(self.registration_source_sha256, str)
            or len(self.registration_source_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.registration_source_sha256)
        ):
            raise ValueError("matched receipt owner requires a registration source digest")


@dataclass(frozen=True)
class PendingOpenReviewRoundReceiptOwner:
    source: ClassifiedReviewRoundReceiptSweepSource
    round: ReviewRoundIdentity
    round_source_sha256: str

    def __post_init__(self) -> None:
        if (not isinstance(self.source, ClassifiedReviewRoundReceiptSweepSource)
                or not isinstance(self.round, ReviewRoundIdentity)
                or self.round.binding.rejection.fence.key.repository
                != self.source.source.cursor.scope
                or isinstance(self.source.reference, BoundReviewRoundReceipt)
                and self.source.reference.round != self.round
                or isinstance(self.source.reference, NamedReviewRoundReceipt)
                and self.source.reference.round_id != self.round.round_id
                or isinstance(self.source.reference, CommandReviewRoundReceipt)
                or not isinstance(self.round_source_sha256, str)
                or len(self.round_source_sha256) != 64
                or any(char not in "0123456789abcdef"
                       for char in self.round_source_sha256)):
            raise ValueError("pending open receipt requires its source and round")


@dataclass(frozen=True)
class BlockedReviewRoundReceiptOwner:
    reason: str
    source_rowid: int

    def __post_init__(self) -> None:
        if (not isinstance(self.reason, str) or not 1 <= len(self.reason) <= 512
                or type(self.source_rowid) is not int):
            raise ValueError("blocked receipt owner requires its source and bounded reason")


def _named_round(db: sqlite3.Connection, repository: str, round_id: str) -> ReviewRoundIdentity:
    rows = db.execute(
        "SELECT rowid,typeof(round_id) AS round_id_type,"
        "typeof(repository) AS repository_type,typeof(plan_json) AS plan_type,"
        "length(CAST(plan_json AS BLOB)) AS plan_bytes "
        "FROM review_execution_rounds WHERE round_id IN (?,CAST(? AS BLOB)) LIMIT 2",
        (round_id, round_id),
    ).fetchall()
    if (len(rows) != 1 or rows[0]["round_id_type"] != "text"
            or rows[0]["repository_type"] != "text" or rows[0]["plan_type"] != "text"
            or type(rows[0]["plan_bytes"]) is not int
            or rows[0]["plan_bytes"] > MAX_WORKFLOW_PAYLOAD_BYTES):
        raise ValueError("named receipt lacks one bounded canonical round row")
    row = db.execute(
        "SELECT plan_json FROM review_execution_rounds "
        "WHERE rowid=? AND round_id=? AND repository=? "
        "AND length(CAST(plan_json AS BLOB))<=?",
        (rows[0]["rowid"], round_id, repository, MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None:
        raise ValueError("named receipt has a foreign or changed round row")
    plan = decode_workflow(row["plan_json"], ReviewRoundPlan)
    if (encode_workflow(plan) != row["plan_json"]
            or plan.round.round_id != round_id
            or plan.round.binding.rejection.fence.key.repository != repository):
        raise ValueError("named receipt disagrees with its round plan")
    return plan.round


def _registered_round(
    db: sqlite3.Connection, repository: str, command_id: str,
) -> tuple[ReviewRoundIdentity, str]:
    fields, digest = _read_recording_source(
        db, ReviewRoundReceiptNamespace.SUBMISSION.value, repository, command_id,
    )
    values = dict(fields)
    reference = _classify_source_payload(
        ReviewRoundReceiptSweepCursor(
            ReviewRoundReceiptNamespace.SUBMISSION.value, repository, command_id,
        ),
        values["claimed_at"], values["reusable_after"], values["payload_json"],
    )
    if not isinstance(reference, BoundReviewRoundReceipt):
        raise ValueError("recorded review command lacks a bound registration")
    return reference.round, digest


def _open_receipt_is_planned(
    source: ClassifiedReviewRoundReceiptSweepSource,
    plan: ReviewRoundPlan,
    planning: ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
) -> bool:
    cursor = source.source.cursor
    namespace = ReviewRoundReceiptNamespace(cursor.namespace)
    if namespace is ReviewRoundReceiptNamespace.PREFLIGHT:
        return cursor.key == plan.round.round_id
    if namespace is ReviewRoundReceiptNamespace.STAGE:
        return cursor.key in {
            review_stage_receipt_key(plan.round.round_id, stage.stage_id)
            for stage in plan.stages
        }
    if namespace is ReviewRoundReceiptNamespace.SUBMISSION:
        return cursor.key in {item.command_id for item in plan.submissions}
    if namespace is ReviewRoundReceiptNamespace.SUBMISSION_INDEX:
        return cursor.key in {
            review_submission_receipt_key(plan.round.round_id, item.command_id)
            for item in plan.submissions
        }
    if namespace is ReviewRoundReceiptNamespace.SEAL:
        completed = (planning.completed_seal if isinstance(
            planning, ReviewSealedParentAbandoned) else planning)
        return (cursor.key == plan.round.round_id
                and isinstance(completed, ReviewPlanningSealed)
                and isinstance(completed.conclusion, ReviewPlanningComplete))
    if namespace is ReviewRoundReceiptNamespace.ABANDONMENT:
        return cursor.key == plan.round.round_id and isinstance(planning, ReviewParentAbandoned)
    return False


def reconcile_review_round_receipt_owner(
    db: sqlite3.Connection, source: ClassifiedReviewRoundReceiptSweepSource,
) -> MatchedReviewRoundReceiptOwner | PendingOpenReviewRoundReceiptOwner | BlockedReviewRoundReceiptOwner:
    """Join one source to a finalized member or retain explicit open uncertainty.

    The result is observational; command effects, claim authority, checkpoint
    ancestry and complete namespace coverage require independent proof.
    """
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(source, ClassifiedReviewRoundReceiptSweepSource)):
        raise ValueError("receipt owner reconciliation requires a transaction and source")
    cursor = source.source.cursor
    registration_digest: str | None = None
    try:
        reference = source.reference
        if isinstance(reference, BoundReviewRoundReceipt):
            round_id = reference.round
        elif isinstance(reference, NamedReviewRoundReceipt):
            round_id = _named_round(db, cursor.scope, reference.round_id)
        elif isinstance(reference, CommandReviewRoundReceipt):
            round_id, registration_digest = _registered_round(
                db, cursor.scope, reference.command_id,
            )
        else:
            raise ValueError("receipt has an unknown round reference")
        row = read_checkpoint_round_row_source(db, round_id)
        values = dict(row.fields)
        if values["closure_json"] is None and values["outcome_json"] is None:
            plan = decode_workflow(values["plan_json"], ReviewRoundPlan)
            planning = decode_workflow(
                values["planning_json"],
                ReviewPlanningOpen | ReviewPlanningSealed | ReviewParentAbandoned,
            )
            if (encode_workflow(planning) != values["planning_json"]
                    or not _open_receipt_is_planned(source, plan, planning)):
                raise ValueError("open round does not plan this receipt")
            _, current_digest = _read_recording_source(
                db, cursor.namespace, cursor.scope, cursor.key,
            )
            if current_digest != source.source.source_sha256:
                raise ValueError("open round receipt changed after its sweep")
            return PendingOpenReviewRoundReceiptOwner(source, round_id, row.source_digest)
        members = read_checkpoint_round_receipt_members(db, round_id)
        exact = next((item for item in members.members
                      if (item.namespace, item.key)
                      == (cursor.namespace, cursor.key)), None)
        if exact is None or exact.source_sha256 != source.source.source_sha256:
            raise ValueError("receipt is absent or changed in its round member set")
        return MatchedReviewRoundReceiptOwner(
            source, round_id, members.source, registration_digest,
        )
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError,
            RecursionError, sqlite3.Error, WorkflowAuthorityError) as exc:
        return BlockedReviewRoundReceiptOwner(
            (_printable(str(exc)) or "receipt owner source is unreadable")[:512],
            source.source.source_rowid,
        )
