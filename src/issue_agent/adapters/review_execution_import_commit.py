"""Atomic SQL publication for a caller-revalidated mixed review import."""

from dataclasses import replace
from datetime import datetime
import hashlib
import sqlite3

from ..domain.review_execution import (
    CurrentReviewExecution, ExecutionNotActivated, ImportedReviewExecutionOriginReceipt,
    BlockedReviewExecutionMigration,
)
from ..domain.review_execution_import_plan import ReviewExecutionImportPlan, EligibleZeroHistoryReviewImport
from ..domain.review_workflow import ReviewWorkflowSnapshot
from ..domain.review_workflow_policy import initial_review_workflow
from ..domain.workflow_identity import FamilyAuthority, WorkflowFamily, WorkflowKey, PullRequestRef
from .control_store import SQLiteControlStore
from .workflow_codec import encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, insert_imported_review_execution_origin,
    encode_workflow_episodes, insert_workflow_component, load_family_authority, load_review_workflow_row, persist_workflow_snapshot, workflow_scope,
)


def commit_review_execution_import(
    db: sqlite3.Connection, plan: ReviewExecutionImportPlan, *, now: datetime,
) -> tuple[ReviewWorkflowSnapshot, ...]:
    """Publish under held caller barriers; a caught failure leaves no partial epoch."""
    if not isinstance(db, sqlite3.Connection) or not db.in_transaction:
        raise ValueError("review execution import requires an open SQL transaction")
    if not isinstance(plan, ReviewExecutionImportPlan):
        raise ValueError("review execution import requires a typed plan")
    with SQLiteControlStore.mutation_savepoint(db):
        decisions = plan.decisions_at(now)
        family = WorkflowFamily.REVIEW_EXECUTION
        if db.execute(
            "SELECT 1 FROM workflow_authority_epochs WHERE repository=? AND family=?",
            (plan.repository, family.value),
        ).fetchone() is not None:
            raise WorkflowAuthorityError("review execution family is already active")
        current: list[ReviewWorkflowSnapshot | None] = []
        for candidate in plan.candidates:
            key = WorkflowKey(plan.repository, PullRequestRef(candidate.pr))
            exists = db.execute(
                "SELECT 1 FROM workflows WHERE repository=? AND target_kind=? AND target_number=?",
                workflow_scope(key),
            ).fetchone()
            snapshot = load_review_workflow_row(db, key) if exists is not None else None
            if isinstance(candidate, EligibleZeroHistoryReviewImport):
                if snapshot != candidate.workflow:
                    raise WorkflowAuthorityError("review execution import workflow changed before commit")
            elif snapshot is not None and not isinstance(snapshot.execution, ExecutionNotActivated):
                raise WorkflowAuthorityError("blocked review import retains incompatible active state")
            current.append(snapshot)

        db.execute("INSERT INTO workflow_authority_epochs VALUES (?, ?, ?, ?, ?, ?)", (
            plan.repository, family.value, plan.execution_epoch, plan.source_digest,
            plan.inventory_digest, now.isoformat(),
        ))
        db.execute("INSERT INTO workflow_activation_evidence VALUES (?, ?, ?, ?, ?)", (
            plan.repository, family.value, plan.execution_epoch, 0, plan.plan_digest,
        ))
        authority = FamilyAuthority(family, plan.execution_epoch)
        committed: list[ReviewWorkflowSnapshot] = []
        for candidate, snapshot, decision in zip(plan.candidates, current, decisions, strict=True):
            key = WorkflowKey(plan.repository, PullRequestRef(candidate.pr))
            if snapshot is None and isinstance(decision, BlockedReviewExecutionMigration):
                rejection_authority = load_family_authority(db, WorkflowFamily.REVIEW_REJECTIONS, plan.repository)
                if rejection_authority is None:
                    raise WorkflowAuthorityError("blocked migration requires existing rejection authority")
                snapshot = replace(initial_review_workflow(key, rejection_authority), execution=decision)
                from .continuation_activation import retain_deferred_pr_repair
                snapshot = retain_deferred_pr_repair(db, snapshot)
                db.execute("INSERT INTO workflows VALUES (?, ?, ?, 0, 0, ?)",
                           (*workflow_scope(key), encode_workflow_episodes(snapshot)))
                insert_workflow_component(db, key, rejection_authority, snapshot.episode.episode_id)
            encoded = encode_workflow(decision)
            db.execute("INSERT INTO workflow_activation_imports VALUES (?, ?, ?, ?, ?, ?, ?)", (
                plan.repository, family.value, plan.execution_epoch, *workflow_scope(key)[1:],
                plan.source_digest, encoded,
            ))
            if isinstance(decision, BlockedReviewExecutionMigration):
                execution = decision
            else:
                if snapshot is None:
                    raise WorkflowAuthorityError("eligible review target lost its workflow")
                execution = CurrentReviewExecution(decision.origin, decision.checkpoint, decision.checkpoint.state, 0, 0)
                receipt = ImportedReviewExecutionOriginReceipt(
                    decision.origin, decision.checkpoint, plan.plan_digest, plan.source_digest,
                    plan.inventory_digest, hashlib.sha256(encoded.encode()).hexdigest(),
                )
                insert_workflow_component(db, key, authority, execution.binding.execution_episode)
                insert_imported_review_execution_origin(db, plan.repository, receipt, claimed_at=now)
            from .continuation_activation import retain_deferred_pr_repair
            snapshot = retain_deferred_pr_repair(db, snapshot)
            after = replace(snapshot, revision=replace(snapshot.revision, version=snapshot.revision.version + 1),
                            execution=execution)
            persist_workflow_snapshot(db, after)
            loaded = load_review_workflow_row(db, key)
            if loaded != after:
                raise WorkflowAuthorityError("review execution import failed canonical readback")
            committed.append(loaded)
        return tuple(committed)
