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
from ..adapters.review_execution_import_commit import commit_review_execution_import
from ..adapters.review_execution_import_replay import verify_committed_review_execution_import
from ..adapters.review_execution_import_sources import (
    HeldLegacyExecutionCounterSources, ObservedLegacyExecutionSqlImportSources,
    held_legacy_execution_counter_sources,
)
from ..adapters.store import Store
from ..domain.review_execution_counter_mapping import CompleteLegacyOpenCounterLinks
from ..domain.review_execution_import_plan import EligibleZeroHistoryReviewImport, ReviewExecutionImportPlan
from ..domain.review_workflow import RecordedPullRequest
from ..domain.workflow_identity import WorkflowFamily, WorkflowKey, canonical_workflow_repository
from ..domain.review_workflow import ReviewWorkflowSnapshot
from .config import HarnessConfig
from .review_execution_import import (
    BlockedReviewImportObservationSet, ObservedReviewExecutionImportCandidates,
    assess_configured_review_execution_import_candidates,
    plan_configured_review_execution_import,
)
from .review_execution_import_remote import (
    BlockedReviewImportRemote, read_configured_review_import_remote,
)


class ReviewExecutionActivationRefused(ValueError):
    """The configured source set cannot enact the reviewed import."""


@dataclass(frozen=True)
class ReviewExecutionActivationCommitted:
    plan: ReviewExecutionImportPlan
    idempotent: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ReviewExecutionImportPlan) or type(self.idempotent) is not bool:
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
            (cfg.state_dir, cfg.review_state_dir),
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

    def convert_current(self, key: WorkflowKey) -> ReviewWorkflowSnapshot:
        """Convert one stopped, bounded prior component; never called by daemons."""
        from ..adapters.workflow_rows import convert_legacy_review_execution

        if not isinstance(key, WorkflowKey) or key.repository != self._cfg.repo:
            raise ValueError("execution conversion requires its configured repository")
        self._control.require_initialized()
        self._require_stopped()
        with self._control.transaction() as db:
            with self._control.mutation_savepoint(db):
                self._require_stopped()
                return convert_legacy_review_execution(db, key)

    def _roots(
        self, extra_roots: tuple[str, ...], observed: CompleteLegacyExecutionRootFiles,
    ) -> tuple[tuple[str, str], ...]:
        if not isinstance(observed, CompleteLegacyExecutionRootFiles):
            raise ValueError("execution activation requires complete typed root observations")
        configured = tuple((role, path) for role, path in (
            ("fix", self._cfg.state_dir), ("review", self._cfg.review_state_dir),
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
    ) -> ReviewExecutionImportPlan:
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
        plan = plan_configured_review_execution_import(observed, inventory, epoch)
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
    ) -> ReviewExecutionImportPlan:
        """Rederive a reviewable plan while excluding all role effects."""
        self._control.require_initialized()
        self._require_stopped()
        with held_legacy_execution_counter_sources(
            self._control, self._gateway, self._cfg.repo, self._roots(extra_roots, files), files, links,
            review_state_dir=self._cfg.review_state_dir,
            other_role_state_dirs=tuple(path for path in (self._cfg.state_dir,) if path),
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
        self, plan: ReviewExecutionImportPlan,
        files: CompleteLegacyExecutionRootFiles, links: CompleteLegacyOpenCounterLinks,
        sql: ObservedLegacyExecutionSqlImportSources, *, extra_roots: tuple[str, ...] = (),
    ) -> ReviewExecutionActivationCommitted:
        """Publish only the identical plan rebuilt from current configured sources."""
        if not isinstance(plan, ReviewExecutionImportPlan) or plan.repository != self._cfg.repo:
            raise ValueError("execution activation requires this repository's reviewed plan")
        self._control.require_initialized()
        with self._control.transaction() as db:
            if verify_committed_review_execution_import(db, plan):
                return ReviewExecutionActivationCommitted(plan, idempotent=True)
        try:
            return self._activate_absent(plan, files, links, sql, extra_roots)
        except Exception:
            # Another operator may commit while this caller waits for a barrier
            # or reads external sources. A winner's exact SQL proof is sufficient
            # even if the old files or remote inputs have since disappeared.
            with self._control.transaction() as db:
                if verify_committed_review_execution_import(db, plan):
                    return ReviewExecutionActivationCommitted(plan, idempotent=True)
            raise

    def _activate_absent(
        self, plan: ReviewExecutionImportPlan,
        files: CompleteLegacyExecutionRootFiles, links: CompleteLegacyOpenCounterLinks,
        sql: ObservedLegacyExecutionSqlImportSources, extra_roots: tuple[str, ...],
    ) -> ReviewExecutionActivationCommitted:
        self._require_stopped()
        with held_legacy_execution_counter_sources(
            self._control, self._gateway, self._cfg.repo, self._roots(extra_roots, files), files, links,
            review_state_dir=self._cfg.review_state_dir,
            other_role_state_dirs=tuple(path for path in (self._cfg.state_dir,) if path),
        ) as held:
            if not isinstance(held, HeldLegacyExecutionCounterSources):
                raise ReviewExecutionActivationRefused(held.reason)
            remote = read_configured_review_import_remote(
                self._cfg, self._control, self._gateway, self._store, held.mapping.links.listing,
            )
            if isinstance(remote, BlockedReviewImportRemote):
                raise ReviewExecutionActivationRefused(remote.reason)
            with self._control.transaction() as db:
                if verify_committed_review_execution_import(db, plan):
                    return ReviewExecutionActivationCommitted(plan, idempotent=True)
                fresh = self._current_plan(db, held, sql, remote, plan.execution_epoch, extra_roots)
                if fresh.plan_digest != plan.plan_digest:
                    raise ReviewExecutionActivationRefused("review execution import sources changed since the plan was reviewed")
                commit_review_execution_import(db, fresh, now=datetime.now(timezone.utc))
        return ReviewExecutionActivationCommitted(plan)


@dataclass(frozen=True)
class ReviewExecutionActivationScope:
    name: str
    cfg: HarnessConfig
    gateway: object
    extra_roots: tuple[str, ...] = ()
    liveness: Callable[[], tuple[str, ...]] = lambda: ()

    @property
    def repository(self) -> str:
        return self.cfg.repo


def activation_scope(name: str, *, harness_root: str, extra_roots: tuple[str, ...] = ()) -> ReviewExecutionActivationScope:
    from ..adapters.github import GitHubGateway
    from .resolve_control import load_operator_configuration
    from .review_identity import review_identity

    cfg, env = load_operator_configuration(name, harness_root=harness_root)
    if "review" not in env.get("ROLES", "fix review").split():
        raise ValueError("execution activation requires the configured review role")
    gateway = GitHubGateway(cfg.repo, review_identity(dict(env)), review_pr_label=cfg.review_trigger_label,
                            review_adopt_unlabeled=cfg.review_adopt_unlabeled)
    _scope_roots(cfg, extra_roots)
    return ReviewExecutionActivationScope(name, cfg, gateway, extra_roots)


def _scope_roots(cfg: HarnessConfig, extra_roots: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    configured = tuple((role, path) for role, path in (
        ("fix", cfg.state_dir), ("review", cfg.review_state_dir),
    ) if path)
    if (not cfg.review_state_dir or any(not os.path.isabs(path) for _, path in configured)
            or len(extra_roots) > MAX_LEGACY_EXECUTION_ROOTS
            or any(not os.path.isabs(path) or not os.path.isdir(path) for path in extra_roots)):
        raise ValueError("execution activation requires explicit absolute configured and declared source roots")
    return distinct_roots(configured + tuple((f"extra:{os.path.realpath(path)}", path) for path in extra_roots))


def _scope_control(scope: ReviewExecutionActivationScope) -> SQLiteControlStore:
    cfg = scope.cfg
    control = SQLiteControlStore(control_store_path((cfg.state_dir, cfg.review_state_dir),
                                                  authority_root=cfg.control_authority_root))
    control.require_initialized()
    return control


def _observed_scope_sources(scope: ReviewExecutionActivationScope, control: SQLiteControlStore):
    from ..adapters.review_execution_activation import read_complete_legacy_execution_root_files
    from ..adapters.review_execution_counter_links import read_complete_legacy_open_counter_links
    from ..adapters.review_execution_import_observation import observe_legacy_execution_sql_import_sources
    from .config import configured_prior_owner_ids, owner_identity_for_target

    cfg = scope.cfg
    files = read_complete_legacy_execution_root_files(_scope_roots(cfg, scope.extra_roots))
    if not isinstance(files, CompleteLegacyExecutionRootFiles):
        raise ReviewExecutionActivationRefused(files.reason)
    links = read_complete_legacy_open_counter_links(control, scope.gateway, cfg.repo)
    if not isinstance(links, CompleteLegacyOpenCounterLinks):
        raise ReviewExecutionActivationRefused(links.reason)
    with control.transaction() as db:
        sql = observe_legacy_execution_sql_import_sources(
            db, cfg.repo, owner_identity_for_target(cfg.repo, cfg.target_dir, "review"),
            configured_prior_owners=configured_prior_owner_ids(cfg, "review"), now=datetime.now(timezone.utc),
        )
    if not isinstance(sql, ObservedLegacyExecutionSqlImportSources):
        raise ReviewExecutionActivationRefused(sql.reason)
    return files, links, sql


def _target_report(decision, *, ceiling: int, current=None) -> dict[str, object]:
    from ..domain.review_execution import BlockedReviewExecutionMigration, CurrentReviewExecution
    from ..domain.review_workflow import PendingReviewRearm
    blocked = isinstance(decision, BlockedReviewExecutionMigration)
    if blocked:
        pr = decision.key.target.number
        blockers = [{"kind": blocker.kind.value,
                     "sources": [{"kind": item.kind, "identity": item.identity, "digest": item.digest}
                                 for item in blocker.sources]} for blocker in decision.history.blockers]
        failures = displayed = None
        session = "not_imported"
    else:
        pr = decision.checkpoint.state.binding.rejection.fence.key.target.number
        blockers = []
        state = current.execution.state if current is not None and isinstance(current.execution, CurrentReviewExecution) else decision.checkpoint.state
        failures, displayed = state.failures, state.displayed_round
        session = type(state.session).__name__
    return {"pr": pr, "decision": "blocked_history" if blocked else "eligible_zero_history",
            "migration_eligible": not blocked, "failures": failures, "displayed_round": displayed,
            "allowance": "not_imported" if blocked else "unlimited" if ceiling == 0 else "bounded",
            "remaining": None if blocked or ceiling == 0 else max(0, ceiling - failures),
            "session": session, "held": blocked or bool(current is not None and
                (current.gates or isinstance(current.intent, PendingReviewRearm))),
            "blockers": blockers, "paid_work_started": False}


def _plan_report(scope: ReviewExecutionActivationScope, plan: ReviewExecutionImportPlan, *, status: str,
                 shadow: bool = False, files=None) -> dict[str, object]:
    from ..adapters.review_execution_import_replay import original_review_execution_plan
    decisions = plan.decisions_at(plan.planned_at)
    result: dict[str, object] = {
        "repository": scope.repository, "status": status, "execution_authority": "migration_required" if status == "migration_required" else "active",
        "plan": {"plan_digest": plan.plan_digest, "source_digest": plan.source_digest,
                 "inventory_digest": plan.inventory_digest, "epoch": plan.execution_epoch},
        "original_plan": original_review_execution_plan(plan),
        "targets": [_target_report(decision, ceiling=scope.cfg.max_review_retries) for decision in decisions],
    }
    if shadow:
        rows = []
        by_pr = {}
        if files is not None:
            for item in files.values.prs:
                by_pr.setdefault(item.source.pr, []).append(item)
        for candidate, decision in zip(plan.candidates, decisions, strict=True):
            clean = isinstance(candidate, EligibleZeroHistoryReviewImport)
            legacy = by_pr.get(candidate.pr, ())
            rows.append({"pr": candidate.pr, "legacy_empty_verified": clean,
                         "legacy_round_sources": [{"root": item.source.root, "round": getattr(item.round, "value", None)} for item in legacy],
                         "legacy_session_sources": [{"root": item.source.root, "kind": type(item.session).__name__} for item in legacy],
                         "new": _target_report(decision, ceiling=scope.cfg.max_review_retries),
                         "differences": [] if clean else ["history is retained under a migration hold; allowance and sessions are not imported"],
                         "comparisons": {"eligibility": "agrees" if clean else "held_pending_history",
                                         "allowance": "agrees_zero" if clean else "unverified_not_imported",
                                         "session": "agrees_none" if clean else "not_imported",
                                         "holds": "agrees_clear" if clean else "migration_hold_retained"}})
        result["shadow"] = rows
    return result


def _active_report(scope: ReviewExecutionActivationScope, control: SQLiteControlStore) -> dict[str, object] | None:
    from ..adapters.review_execution_import_replay import _table
    from ..adapters.workflow_rows import load_family_authority, load_review_execution_import_decision, load_review_workflow_row
    from ..domain.review_activation import MAX_IMPORTED_PULL_REQUESTS
    from ..domain.workflow_identity import PullRequestRef

    with control.transaction() as db:
        authority = load_family_authority(db, WorkflowFamily.REVIEW_EXECUTION, scope.repository)
        if authority is None:
            return None
        metadata = _table(db, "workflow_authority_epochs", ("repository", "family", "source_digest", "inventory_digest"),
                          scope.repository, family=authority.family.value, limit=1)
        evidence = _table(db, "workflow_activation_evidence", ("repository", "family", "plan_digest"),
                          scope.repository, family=authority.family.value, limit=1)
        if (len(metadata) != 1 or len(evidence) != 1
                or metadata[0][:2] != (scope.repository, authority.family.value)
                or evidence[0][:2] != (scope.repository, authority.family.value)):
            raise ReviewExecutionActivationRefused("active execution lacks its original plan receipt")
        targets = db.execute("SELECT target_number FROM workflow_activation_imports WHERE repository=? AND family=? "
                             "ORDER BY target_number LIMIT ?", (scope.repository, authority.family.value, MAX_IMPORTED_PULL_REQUESTS + 1)).fetchall()
        if len(targets) > MAX_IMPORTED_PULL_REQUESTS:
            raise ReviewExecutionActivationRefused("active execution original target scope exceeds its bound")
        rows = []
        for target in targets:
            key = WorkflowKey(scope.repository, PullRequestRef(target[0]))
            decision = load_review_execution_import_decision(db, key)
            current = load_review_workflow_row(db, key)
            if decision is None or current is None:
                raise ReviewExecutionActivationRefused("active execution original target is missing")
            rows.append(_target_report(decision, ceiling=scope.cfg.max_review_retries, current=current))
    return {"repository": scope.repository, "status": "already_active", "execution_authority": "active",
            "plan": {"plan_digest": evidence[0][2], "source_digest": metadata[0][2], "inventory_digest": metadata[0][3], "epoch": authority.epoch},
            "original_plan": None, "targets": rows}


def inspect(scope: ReviewExecutionActivationScope, *, shadow: bool) -> dict[str, object]:
    control = _scope_control(scope)
    active = _active_report(scope, control)
    if active is not None:
        return active
    try:
        files, links, sql = _observed_scope_sources(scope, control)
        activation = ConfiguredReviewExecutionActivation(scope.cfg, control, scope.gateway, Store(scope.cfg.review_state_dir), liveness=lambda: ())
        plan = activation.plan(files, links, sql, extra_roots=scope.extra_roots)
        return _plan_report(scope, plan, status="migration_required", shadow=shadow, files=files)
    except ReviewExecutionActivationRefused as exc:
        return {"repository": scope.repository, "status": "blocked", "execution_authority": "migration_required", "blockers": [str(exc)]}


def activate(scope: ReviewExecutionActivationScope, *, confirm_plan_digest: str, plan_file: str) -> dict[str, object]:
    from ..adapters.review_execution_import_replay import read_original_review_execution_plan
    from .resolve_activation import ActivationNotConfirmed

    plan = read_original_review_execution_plan(plan_file)
    if plan.repository != scope.repository or plan.plan_digest != confirm_plan_digest:
        raise ActivationNotConfirmed("confirmed execution digest or repository differs from the original complete plan")
    control = _scope_control(scope)
    with control.transaction() as db:
        replay = verify_committed_review_execution_import(db, plan)
    if replay:
        return _plan_report(scope, plan, status="already_active_with_this_plan")
    files, links, sql = _observed_scope_sources(scope, control)
    control = _scope_control(scope)
    activation = ConfiguredReviewExecutionActivation(scope.cfg, control, scope.gateway, Store(scope.cfg.review_state_dir), liveness=scope.liveness)
    committed = activation.activate(plan, files, links, sql, extra_roots=scope.extra_roots)
    return _plan_report(scope, plan, status="already_active_with_this_plan" if committed.idempotent else "activated")


def rehearse(scope: ReviewExecutionActivationScope, *, copy_to: str) -> dict[str, object]:
    import re
    from dataclasses import replace
    from ..adapters.resolve_activation import copy_for_rehearsal, file_digests
    from ..adapters.review_execution_import_replay import original_review_execution_plan

    cfg = scope.cfg
    roots = _scope_roots(cfg, scope.extra_roots)
    names = re.compile(r"(?:pr-.*\.(?:session|round|last-sha|patch-id|operator-guidance))|(?:issue-.*\.infra-deaths)", re.DOTALL)
    legacy = tuple(sorted(os.path.join(root, item.name) for _, root in roots
                          for item in (os.scandir(root) if os.path.isdir(root) else ()) if names.fullmatch(item.name)))
    control_path = control_store_path((cfg.state_dir, cfg.review_state_dir),
                                      authority_root=cfg.control_authority_root)
    # Opening the original through the writable store can checkpoint committed
    # WAL frames. Validate and migrate only the read-only backup's copy.
    originals = (control_path, f"{control_path}.authority", os.path.join(cfg.review_state_dir, "events-v2.jsonl"), *legacy)
    before = file_digests(originals)
    copied = copy_for_rehearsal(control_path, roots, copy_to, protected=(cfg.target_dir, cfg.review_state_dir),
                                legacy_name=names, canonical_layout=True)
    translated = {os.path.realpath(old): new for (_, old), (_, new) in zip(roots, copied.roots, strict=True)}
    translate = lambda path: translated[os.path.realpath(path)] if path else ""
    copied_cfg = replace(cfg, state_dir=translate(cfg.state_dir), review_state_dir=translate(cfg.review_state_dir),
                         control_authority_root=os.path.abspath(copy_to))
    copied_scope = replace(scope, cfg=copied_cfg, extra_roots=tuple(translate(path) for path in scope.extra_roots), liveness=lambda: ())
    copied_control = SQLiteControlStore(copied.control_path)
    copied_control.initialize()
    copied_control = _scope_control(copied_scope)
    files, links, sql = _observed_scope_sources(copied_scope, copied_control)
    activation = ConfiguredReviewExecutionActivation(copied_cfg, copied_control, scope.gateway,
                                                    Store(copied_cfg.review_state_dir), liveness=lambda: ())
    plan = activation.plan(files, links, sql, extra_roots=copied_scope.extra_roots)
    activation.activate(plan, files, links, sql, extra_roots=copied_scope.extra_roots)
    with copied_control.transaction() as db:
        committed = tuple(db.iterdump())
    checks = {"exact_retry_inert": activation.activate(plan, None, None, None).idempotent}
    with copied_control.transaction() as db:
        checks["exact_retry_inert"] = checks["exact_retry_inert"] and committed == tuple(db.iterdump())
    different = replace(plan, inventory_digest="0" * 64 if plan.inventory_digest != "0" * 64 else "1" * 64)
    try:
        activation.activate(different, None, None, None)
    except ValueError:
        checks["different_plan_refused"] = True
    else:
        checks["different_plan_refused"] = False
    checks["originals_unchanged"] = before == file_digests(originals)
    result = _plan_report(copied_scope, plan, status="rehearsed" if all(checks.values()) else "failed", shadow=True, files=files)
    result.update({"checks": checks, "copy": {"control_path": copied.control_path, "legacy_roots": copied.roots},
                   "original_plan": original_review_execution_plan(plan)})
    return result
