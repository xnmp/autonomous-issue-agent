"""Operator resolve-family activation: inventory, shadow, activate, rehearse.

Scope comes from one validated active launch snapshot. Inventory and shadow
are read-only. Activation commits exactly the plan whose source digest the
operator confirmed. Rehearsal migrates and activates an isolated copy and
proves the originals are byte-for-byte unchanged.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace

from ..adapters.control_path import control_store_path
from ..adapters.control_store import SQLiteControlStore
from ..adapters.resolve_activation import (
    ActivationSourcesChanged, LegacyRoots, ResolveActivationRefused, SQLiteResolveActivation, copy_for_rehearsal, distinct_roots,
    file_digests, read_legacy_resolve_roots,
)
from ..adapters.workflow_control import SQLiteWorkflowControl
from ..domain.resolve_activation import (
    LEGACY_SUFFIXES, ActivationBlocked, ActivationDecision, ActiveWithDifferences, AlreadyActive,
    DeploymentInventory, LegacyAbsent,
    LegacyEntry, LegacyFile, LegacyPresent, LegacyResolveRecord, ResolveActivationPlan,
    UnmappedLegacyEntry, plan_resolve_activation, shadow_compare,
)
from ..domain.workflow import WorkflowSnapshot
from ..domain.workflow_identity import IssueRef, WorkflowKey, WorkflowWriter, canonical_workflow_repository
from .config import owner_identity_for_target
from .resolve_control import load_operator_configuration

JsonReport = dict[str, object]


@dataclass(frozen=True)
class ActivationScope:
    """`legacy_roots` are (label, directory): every configured role state root
    plus operator-named older roots. `event_dir` receives the operator feed."""

    name: str
    repository: str
    control_path: str
    legacy_roots: LegacyRoots
    event_dir: str
    ceiling: int
    fix_owner: str


def activation_scope(name: str, *, harness_root: str, extra_roots: tuple[str, ...] = ()) -> ActivationScope:
    cfg, _env = load_operator_configuration(name, harness_root=harness_root)
    path = control_store_path((cfg.state_dir, cfg.review_state_dir),
                              authority_root=cfg.control_authority_root)
    if not cfg.state_dir or not os.path.isabs(cfg.state_dir):
        raise ValueError("resolve activation requires an explicit absolute fix STATE_DIR")
    configured = tuple((role, directory) for role, directory in (
        ("fix", cfg.state_dir), ("review", cfg.review_state_dir),
    ) if directory)
    for extra in extra_roots:
        if not os.path.isabs(extra) or not os.path.isdir(extra):
            raise ValueError(f"legacy state root must be an existing absolute directory: {extra}")
    roots = distinct_roots(configured + tuple((f"extra:{os.path.realpath(e)}", e) for e in extra_roots))
    return ActivationScope(
        name, canonical_workflow_repository(cfg.repo), path, roots, cfg.state_dir, cfg.max_resolve_retries,
        owner_identity_for_target(cfg.repo, cfg.target_dir, "fix"),
    )


@dataclass(frozen=True)
class Evaluation:
    records: tuple[LegacyEntry, ...]
    inventory: DeploymentInventory
    decision: ActivationDecision


def evaluate(scope: ActivationScope, activation: SQLiteResolveActivation) -> Evaluation:
    records = read_legacy_resolve_roots(scope.legacy_roots)
    inventory = activation.inventory(scope.repository)
    return Evaluation(records, inventory, plan_resolve_activation(
        scope.repository, records, inventory, ceiling=scope.ceiling,
    ))


def _file(value: LegacyFile) -> JsonReport:
    if isinstance(value, LegacyPresent):
        return {"state": "present", "digest": value.digest}
    if isinstance(value, LegacyAbsent):
        return {"state": "absent"}
    return {"state": "unreadable", "reason": value.reason}


def _plan(plan: ResolveActivationPlan) -> JsonReport:
    return {
        "plan_digest": plan.plan_digest,
        "epoch": plan.epoch, "ceiling": plan.ceiling, "source_digest": plan.source_digest,
        "inventory_digest": plan.inventory_digest, "imports_digest": plan.imports_digest,
        "imports": [{
            "issue": t.issue, "episode_id": t.episode_id, "failures": t.failures,
            "last_failure": t.last_failure, "held": t.held,
        } for t in plan.imports],
    }


def report(scope: ActivationScope, evaluation: Evaluation, *, shadow: bool) -> JsonReport:
    inventory = evaluation.inventory
    decision = evaluation.decision
    plan = (decision.plan if isinstance(decision, AlreadyActive) else decision.recorded
            if isinstance(decision, ActiveWithDifferences) else decision
            if isinstance(decision, ResolveActivationPlan) else None)
    result: JsonReport = {
        "repository": scope.repository, "control_path": scope.control_path,
        "legacy_roots": [{"label": label, "path": path} for label, path in scope.legacy_roots],
        "ceiling": scope.ceiling,
        "status": "blocked" if isinstance(decision, ActivationBlocked) else
                  "already_active" if isinstance(decision, AlreadyActive) else
                  "active_with_differences" if isinstance(decision, ActiveWithDifferences) else "ready",
        "blockers": list(decision.reasons) if isinstance(decision, ActivationBlocked) else [],
        "differences": list(decision.reasons) if isinstance(decision, ActiveWithDifferences) else [],
        "records": [
            {"root": r.root, "issue": r.issue, "resolve_retries": _file(r.retries), "last_error": _file(r.last_error)}
            for r in evaluation.records if isinstance(r, LegacyResolveRecord)
        ],
        "unmapped": [
            {"root": r.root, "name": r.name, "reason": r.reason}
            for r in evaluation.records if isinstance(r, UnmappedLegacyEntry)
        ],
        "inventory": {
            "quiescent": inventory.quiescent, "digest": inventory.digest,
            "active_runs": list(inventory.active_runs), "leases": list(inventory.leases),
            "dispatch_claims": list(inventory.dispatch_claims), "open_commands": list(inventory.open_commands),
            "active_reservations": list(inventory.active_reservations),
            "activation": None if inventory.activation is None else {
                "epoch": inventory.activation.epoch, "source_digest": inventory.activation.source_digest,
                "inventory_digest": inventory.activation.inventory_digest,
                "imports_digest": inventory.activation.imports_digest,
                "ceiling": inventory.activation.ceiling,
                "plan_digest": inventory.activation.plan_digest,
            },
            "retained_targets": list(inventory.retained_targets),
        },
        "plan": None if plan is None else _plan(plan),
    }
    if shadow:
        result["shadow"] = [] if plan is None else [{
            "issue": row.issue, "legacy_failures": row.legacy_failures,
            "legacy_exhausted": row.legacy_exhausted, "legacy_eligible": row.legacy_eligible,
            "legacy_remaining": row.legacy_remaining, "new_failures": row.new_failures,
            "new_held": row.new_held, "new_eligible": row.new_eligible,
            "new_remaining": row.new_remaining, "agrees": row.agrees,
        } for row in shadow_compare(plan, evaluation.records, plan.ceiling)]
    return result



def _operator_activation(scope: ActivationScope) -> SQLiteResolveActivation:
    control = SQLiteControlStore(scope.control_path)
    control.require_initialized()
    return SQLiteResolveActivation(control)


def inspect(scope: ActivationScope, *, shadow: bool) -> JsonReport:
    return report(scope, evaluate(scope, _operator_activation(scope)), shadow=shadow)


class ActivationNotConfirmed(ValueError):
    pass


def activate(scope: ActivationScope, *, confirm_plan_digest: str) -> JsonReport:
    activation = _operator_activation(scope)
    evaluation = evaluate(scope, activation)
    decision = evaluation.decision
    if isinstance(decision, ActiveWithDifferences):
        result = report(scope, evaluation, shadow=True)
        # Replaying the digest that was confirmed at activation is answered
        # from the recorded evidence; nothing is written either way.
        if decision.recorded is not None and confirm_plan_digest == decision.recorded.plan_digest:
            result["status"] = "already_active_with_this_plan"
        return result
    if isinstance(decision, ActivationBlocked):
        return report(scope, evaluation, shadow=True)
    plan = decision.plan if isinstance(decision, AlreadyActive) else decision
    if confirm_plan_digest != plan.plan_digest:
        raise ActivationNotConfirmed(
            "confirmed plan digest differs from the freshly computed plan; review `shadow` again"
        )
    committed = activation.activate(plan, revalidate=lambda: evaluate(scope, activation).decision)
    result = report(scope, evaluate(scope, activation), shadow=False)
    result["status"] = "already_active_with_this_plan" if committed.idempotent else "activated"
    return result


def _verify_imports(scope: ActivationScope, copy: SQLiteControlStore, plan: ResolveActivationPlan) -> list[str]:
    workflows = SQLiteWorkflowControl(copy, WorkflowWriter(scope.repository, "fix", scope.fix_owner),
                                      resolve_ceiling=scope.ceiling)
    failures: list[str] = []
    for target in plan.imports:
        read = workflows.load_workflow(WorkflowKey(scope.repository, IssueRef(target.issue)))
        if not isinstance(read, WorkflowSnapshot):
            failures.append(f"issue #{target.issue}: imported workflow is unavailable ({type(read).__name__})")
        elif (read.episode.episode_id, read.episode.failures, read.episode.last_failure, bool(read.gates)) != (
            target.episode_id, target.failures, target.last_failure, target.held,
        ):
            failures.append(f"issue #{target.issue}: imported workflow disagrees with its decision")
    return failures


def rehearse(scope: ActivationScope, *, copy_to: str) -> JsonReport:
    """Activate an isolated copy; the originals are never opened for writing."""
    witness = f"{scope.control_path}.authority"
    legacy = tuple(sorted(
        os.path.join(directory, entry.name) for _, directory in scope.legacy_roots
        for entry in (os.scandir(directory) if os.path.isdir(directory) else ())
        if entry.name.endswith(tuple(f".{suffix}" for suffix in LEGACY_SUFFIXES))
    ))
    originals = (scope.control_path, witness, os.path.join(scope.event_dir, "events-v2.jsonl"), *legacy)
    before = file_digests(originals)
    original_records = read_legacy_resolve_roots(scope.legacy_roots)
    copied = copy_for_rehearsal(scope.control_path, scope.legacy_roots, copy_to, protected=(scope.event_dir,))
    copy_control = SQLiteControlStore(copied.control_path)
    copy_control.initialize()
    events = os.path.join(copy_to, "events")
    os.makedirs(events, mode=0o700, exist_ok=True)
    copy_scope = replace(scope, control_path=copied.control_path, legacy_roots=copied.roots, event_dir=events)
    activation = SQLiteResolveActivation(copy_control)
    evaluation = evaluate(copy_scope, activation)
    result = report(copy_scope, evaluation, shadow=True)
    checks: dict[str, bool] = {"copied_sources_match": evaluation.records == original_records}
    decision = evaluation.decision
    failures: list[str] = []
    if isinstance(decision, ResolveActivationPlan) and checks["copied_sources_match"]:
        def revalidate() -> ActivationDecision:
            return evaluate(copy_scope, activation).decision

        committed = activation.activate(decision, revalidate=revalidate)
        checks["activated"] = not committed.idempotent
        failures = _verify_imports(scope, copy_control, decision)
        checks["imports_read_back"] = not failures
        checks["reactivation_idempotent"] = activation.activate(decision, revalidate=revalidate).idempotent
        different = replace(decision, source_digest=hashlib.sha256(
            f"rehearsal-different-plan\0{decision.source_digest}".encode()).hexdigest())
        # Revalidation agreeing with the different plan simulates a stale
        # reviewer racing the activation, so the in-transaction guard decides.
        try:
            activation.activate(different, revalidate=lambda: different)
        except ActivationSourcesChanged:
            checks["different_plan_refused"] = False
        except ResolveActivationRefused:
            checks["different_plan_refused"] = True
        else:
            checks["different_plan_refused"] = False
    after = file_digests(originals)
    checks["originals_unchanged"] = before == after
    result.update({
        "status": "rehearsed" if isinstance(decision, ResolveActivationPlan) and all(checks.values()) else
                  "blocked" if isinstance(decision, ActivationBlocked) else
                  "already_active" if isinstance(decision, AlreadyActive) and all(checks.values()) else
                  "active_with_differences" if isinstance(decision, ActiveWithDifferences) else "failed",
        "copy": {"control_path": copied.control_path,
                 "legacy_roots": [{"label": label, "path": path} for label, path in copied.roots]},
        "checks": checks, "verification_failures": failures,
    })
    return result
