"""Configuration-bound offline activation of empty review execution history."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sqlite3

from ..adapters.control_path import control_store_path
from ..adapters.control_store import SQLiteControlStore
from ..adapters.resolve_activation import distinct_roots, family_inventory
from ..adapters.review_execution_activation import (
    BlockedLegacyExecutionFileValues, CompleteLegacyExecutionRootFiles,
    MAX_LEGACY_EXECUTION_ROOTS, revalidate_complete_legacy_execution_file_values,
)
from ..adapters.review_execution_import_commit import commit_empty_review_execution_import
from ..adapters.review_execution_import_replay import verify_committed_empty_review_execution_import
from ..adapters.review_execution_import_sources import (
    HeldLegacyExecutionCounterSources, ObservedLegacyExecutionSqlImportSources,
    held_legacy_execution_counter_sources,
)
from ..adapters.store import Store
from ..domain.review_execution_counter_mapping import CompleteLegacyOpenCounterLinks
from ..domain.review_execution_import_plan import EmptyReviewExecutionImportPlan
from ..domain.review_workflow import RecordedPullRequest
from ..domain.workflow_identity import WorkflowFamily, canonical_workflow_repository
from .config import HarnessConfig
from .review_execution_import import (
    BlockedReviewImportObservationSet, ObservedReviewExecutionImportCandidates,
    assess_configured_review_execution_import_candidates,
    plan_configured_empty_review_execution_import,
)
from .review_execution_import_remote import (
    BlockedReviewImportRemote, read_configured_review_import_remote,
)


class ReviewExecutionActivationRefused(ValueError):
    """The configured source set cannot enact the reviewed import."""


@dataclass(frozen=True)
class ReviewExecutionActivationCommitted:
    plan: EmptyReviewExecutionImportPlan
    idempotent: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.plan, EmptyReviewExecutionImportPlan) or type(self.idempotent) is not bool:
            raise ValueError("execution activation result requires its reviewed plan")


class ConfiguredReviewExecutionActivation:
    """Operator composition; old writers must pass the supplied liveness probe.

    The probe is the operator transport's process and heartbeat reader for all
    same-repository deployments. SQL quiescence is checked independently in
    the acceptance transaction. Neither the daemons nor children construct this.
    """

    def __init__(
        self, cfg: HarnessConfig, control: SQLiteControlStore, gateway: object,
        store: Store, *, liveness: Callable[[], tuple[str, ...]],
    ) -> None:
        if (not isinstance(cfg, HarnessConfig) or not isinstance(control, SQLiteControlStore)
                or not isinstance(store, Store) or not callable(liveness)):
            raise ValueError("execution activation requires configured authority and a liveness probe")
        cfg.validate()
        repository = canonical_workflow_repository(cfg.repo)
        expected = control_store_path(
            (cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir),
            authority_root=cfg.control_authority_root,
        )
        if (cfg.repo != repository or not os.path.isabs(cfg.target_dir)
                or not cfg.review_state_dir or not os.path.isabs(cfg.review_state_dir)
                or os.path.realpath(control.path) != os.path.realpath(expected)
                or os.path.realpath(store.state_dir) != os.path.realpath(cfg.review_state_dir)):
            raise ValueError("execution activation authority differs from deployment configuration")
        self._cfg = cfg
        self._control = control
        self._gateway = gateway
        self._store = store
        self._liveness = liveness

    def _require_stopped(self) -> None:
        reasons = self._liveness()
        if (not isinstance(reasons, tuple)
                or len(reasons) > 4096
                or any(not isinstance(reason, str) or not reason or len(reason) > 512 for reason in reasons)):
            raise ReviewExecutionActivationRefused("deployment liveness evidence is malformed")
        if reasons:
            raise ReviewExecutionActivationRefused("watchers may be running: " + "; ".join(reasons[:4]))

    def _roots(
        self, extra_roots: tuple[str, ...], observed: CompleteLegacyExecutionRootFiles,
    ) -> tuple[tuple[str, str], ...]:
        if not isinstance(observed, CompleteLegacyExecutionRootFiles):
            raise ValueError("execution activation requires complete typed root observations")
        configured = tuple((role, path) for role, path in (
            ("fix", self._cfg.state_dir), ("review", self._cfg.review_state_dir),
            ("steward", self._cfg.steward_state_dir),
        ) if path)
        if (any(not os.path.isabs(path) for _, path in configured)
                or not isinstance(extra_roots, tuple)
                or len(extra_roots) > MAX_LEGACY_EXECUTION_ROOTS
                or any(not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path)
                       for path in extra_roots)):
            raise ValueError("execution activation roots require absolute configured or declared directories")
        roots = configured + tuple(
            (f"extra:{os.path.realpath(path)}", path) for path in extra_roots
        )
        expected = tuple((label, os.path.abspath(path), os.path.realpath(path)) for label, path in roots)
        selected = tuple((root.label, root.configured_path, root.resolved_path) for root in observed.roots)
        # Source readers support both complete role labels and one label per
        # physical directory. Preserve the reviewed representation for shared
        # state roots; the held scope still rereads directory and file identity.
        return roots if selected == expected else distinct_roots(roots)

    def _current_plan(
        self, db: sqlite3.Connection, held: HeldLegacyExecutionCounterSources,
        sql: ObservedLegacyExecutionSqlImportSources, remote: tuple[RecordedPullRequest, ...],
        epoch: int, extra_roots: tuple[str, ...],
    ) -> EmptyReviewExecutionImportPlan:
        if db.execute(
            "SELECT 1 FROM workflow_authority_epochs WHERE repository=? AND family=?",
            (self._cfg.repo, WorkflowFamily.REVIEW_EXECUTION.value),
        ).fetchone() is not None:
            raise ReviewExecutionActivationRefused("review execution family is already active")
        observed = assess_configured_review_execution_import_candidates(
            self._cfg, db, held, sql, remote, extra_roots=extra_roots,
        )
        if not isinstance(observed, ObservedReviewExecutionImportCandidates):
            raise ReviewExecutionActivationRefused(observed.reason)
        inventory = family_inventory(db, self._cfg.repo, WorkflowFamily.REVIEW_EXECUTION, None)
        plan = plan_configured_empty_review_execution_import(observed, inventory, epoch)
        if isinstance(plan, BlockedReviewImportObservationSet):
            raise ReviewExecutionActivationRefused(plan.reason)
        self._require_stopped()
        files = revalidate_complete_legacy_execution_file_values(self._roots(extra_roots, held.roots), held.roots)
        if isinstance(files, BlockedLegacyExecutionFileValues):
            raise ReviewExecutionActivationRefused(files.reason)
        return plan

    def plan(
        self, files: CompleteLegacyExecutionRootFiles, links: CompleteLegacyOpenCounterLinks,
        sql: ObservedLegacyExecutionSqlImportSources, *, execution_epoch: int = 1,
        extra_roots: tuple[str, ...] = (),
    ) -> EmptyReviewExecutionImportPlan:
        """Rederive a reviewable plan while excluding all role effects."""
        self._control.require_initialized()
        self._require_stopped()
        with held_legacy_execution_counter_sources(
            self._control, self._gateway, self._cfg.repo, self._roots(extra_roots, files), files, links,
            review_state_dir=self._cfg.review_state_dir,
            other_role_state_dirs=tuple(path for path in (self._cfg.state_dir, self._cfg.steward_state_dir) if path),
        ) as held:
            if not isinstance(held, HeldLegacyExecutionCounterSources):
                raise ReviewExecutionActivationRefused(held.reason)
            remote = read_configured_review_import_remote(
                self._cfg, self._control, self._gateway, self._store, held.mapping.links.listing,
            )
            if isinstance(remote, BlockedReviewImportRemote):
                raise ReviewExecutionActivationRefused(remote.reason)
            with self._control.transaction() as db:
                return self._current_plan(db, held, sql, remote, execution_epoch, extra_roots)

    def activate(
        self, plan: EmptyReviewExecutionImportPlan,
        files: CompleteLegacyExecutionRootFiles, links: CompleteLegacyOpenCounterLinks,
        sql: ObservedLegacyExecutionSqlImportSources, *, extra_roots: tuple[str, ...] = (),
    ) -> ReviewExecutionActivationCommitted:
        """Publish only the identical plan rebuilt from current configured sources."""
        if not isinstance(plan, EmptyReviewExecutionImportPlan) or plan.repository != self._cfg.repo:
            raise ValueError("execution activation requires this repository's reviewed plan")
        self._control.require_initialized()
        with self._control.transaction() as db:
            if verify_committed_empty_review_execution_import(db, plan):
                return ReviewExecutionActivationCommitted(plan, idempotent=True)
        try:
            return self._activate_absent(plan, files, links, sql, extra_roots)
        except Exception:
            # Another operator may commit while this caller waits for a barrier
            # or reads external sources. A winner's exact SQL proof is sufficient
            # even if the old files or remote inputs have since disappeared.
            with self._control.transaction() as db:
                if verify_committed_empty_review_execution_import(db, plan):
                    return ReviewExecutionActivationCommitted(plan, idempotent=True)
            raise

    def _activate_absent(
        self, plan: EmptyReviewExecutionImportPlan,
        files: CompleteLegacyExecutionRootFiles, links: CompleteLegacyOpenCounterLinks,
        sql: ObservedLegacyExecutionSqlImportSources, extra_roots: tuple[str, ...],
    ) -> ReviewExecutionActivationCommitted:
        self._require_stopped()
        with held_legacy_execution_counter_sources(
            self._control, self._gateway, self._cfg.repo, self._roots(extra_roots, files), files, links,
            review_state_dir=self._cfg.review_state_dir,
            other_role_state_dirs=tuple(path for path in (self._cfg.state_dir, self._cfg.steward_state_dir) if path),
        ) as held:
            if not isinstance(held, HeldLegacyExecutionCounterSources):
                raise ReviewExecutionActivationRefused(held.reason)
            remote = read_configured_review_import_remote(
                self._cfg, self._control, self._gateway, self._store, held.mapping.links.listing,
            )
            if isinstance(remote, BlockedReviewImportRemote):
                raise ReviewExecutionActivationRefused(remote.reason)
            with self._control.transaction() as db:
                if verify_committed_empty_review_execution_import(db, plan):
                    return ReviewExecutionActivationCommitted(plan, idempotent=True)
                fresh = self._current_plan(db, held, sql, remote, plan.execution_epoch, extra_roots)
                if fresh.plan_digest != plan.plan_digest:
                    raise ReviewExecutionActivationRefused("review execution import sources changed since the plan was reviewed")
                commit_empty_review_execution_import(db, fresh, now=datetime.now(timezone.utc))
                self._control._transition(db, "workflow_activation", f"{plan.repository}:review_execution", "activated", {
                    "epoch": plan.execution_epoch, "plan_digest": plan.plan_digest,
                    "source_digest": plan.source_digest, "inventory_digest": plan.inventory_digest,
                    "imported": len(plan.candidates),
                })
        return ReviewExecutionActivationCommitted(plan)
