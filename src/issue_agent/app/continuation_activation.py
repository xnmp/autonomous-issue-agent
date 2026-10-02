"""Configured offline fix/steward import using existing barriers and copy layout."""

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import os

from ..adapters.continuation_activation import (
    LEGACY_CONTINUATION_NAME, imported_component, publish_continuation_import,
    read_continuation_roots, read_import_plan, verify_original_plan,
)
from ..adapters.control_path import control_store_path
from ..adapters.control_store import SQLiteControlStore
from ..adapters.deployment_authority import deployment_effect_barrier
from ..adapters.github import GitHubGateway
from ..adapters.resolve_activation import (
    LegacyRoots, ResolveActivationRefused, copy_for_rehearsal, family_inventory, file_digests, _owner_repository,
)
from ..adapters.review_activation import read_open_pull_requests
from ..adapters.review_effects import read_pull_request
from ..adapters.workflow_codec import encode_workflow
from ..adapters.workflow_rows import load_issue_aggregate, load_review_workflow_row, workflow_run_scope_bounds
from ..domain import continuation_components as cc
from ..domain.continuation_import import (
    ISSUE_FIELDS, PR_FIELDS, STEWARD_FIELDS, MAX_IMPORT_TARGETS,
    ContinuationImportPlan, ContinuationImportTarget, LegacyContinuationRecord, classify_record, digest, ci_shadow, rebase_shadow, review_shadow, issue_reply_shadow, triage_shadow, groom_shadow,
)
from ..domain.refs import issue_from_branch
from ..domain.resolve_activation import LegacyAbsent
from ..domain.review_activation import OpenPullRequestsListed
from ..domain.review_workflow import ObservedPullRequest
from ..domain.workflow_identity import IssueRef, PullRequestRef, WorkflowFamily, WorkflowKey
from .resolve_control import load_operator_configuration
from .resolve_activation import _bind_operator_feed
from .review_identity import review_identity


@dataclass(frozen=True)
class ContinuationActivationScope:
    name: str
    repository: str
    control_path: str
    legacy_roots: LegacyRoots
    event_dir: str
    families: tuple[WorkflowFamily, ...]
    gateway: object | None = None


def activation_scope(name: str, *, harness_root: str, extra_roots: tuple[str, ...] = (), family: str = "fix") -> ContinuationActivationScope:
    cfg, env = load_operator_configuration(name, harness_root=harness_root)
    if family not in {"fix", "steward"}:
        raise ValueError("continuation activation selects fix or steward")
    selected_root = cfg.state_dir if family == "fix" else cfg.steward_state_dir
    if not selected_root or not os.path.isabs(selected_root):
        raise ValueError(f"{family} activation requires its explicit absolute configured state root")
    path = control_store_path((cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir), authority_root=cfg.control_authority_root)
    roots = tuple((role, value) for role, value in (("fix", cfg.state_dir), ("review", cfg.review_state_dir), ("steward", cfg.steward_state_dir)) if value)
    if any(not os.path.isabs(value) for _, value in roots) or any(not os.path.isabs(value) or not os.path.isdir(value) for value in extra_roots):
        raise ValueError("continuation activation requires absolute configured or declared roots")
    roots += tuple((f"extra:{index}", value) for index, value in enumerate(extra_roots))
    families = (WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR) if family == "fix" else (WorkflowFamily.STEWARD,)
    gateway = GitHubGateway(cfg.repo, review_identity(dict(env))) if family == "fix" else None
    return ContinuationActivationScope(name, cfg.repo, path, roots, selected_root, families, gateway)


def evaluate(scope: ContinuationActivationScope, control: SQLiteControlStore) -> ContinuationImportPlan:
    records, file_digest = read_continuation_roots(scope.repository, scope.legacy_roots, scope.families)
    return _evaluate(scope, control, records, file_digest)


def _evaluate(scope: ContinuationActivationScope, control: SQLiteControlStore, records: tuple[LegacyContinuationRecord, ...], file_digest: str) -> ContinuationImportPlan:
    mappings = []
    current_branches: dict[int, str] = {}
    mapped_issues: dict[int, list[int]] = {}
    linkage_blocks: dict[WorkflowKey, str] = {}
    remote_keys: set[WorkflowKey] = set()
    if WorkflowFamily.PR_REPAIR in scope.families:
        listing = read_open_pull_requests(scope.gateway, scope.repository)
        if not isinstance(listing, OpenPullRequestsListed):
            raise ResolveActivationRefused("fix import requires complete current PR linkage: " + listing.reason)
        for number in listing.numbers:
            key = WorkflowKey(scope.repository, PullRequestRef(number))
            observed, view = read_pull_request(scope.gateway, key, datetime.now(timezone.utc), reviews=False)
            if not isinstance(observed, ObservedPullRequest) or view is None:
                raise ResolveActivationRefused(f"PR #{number}: current linkage observation unavailable")
            branch_issue = issue_from_branch(view.head_ref_name)
            linked = view.closing_issue if view.closing_issue is not None else branch_issue
            mappings.append((number, view.closing_issue, branch_issue, view.head_ref_name, view.head_ref_oid))
            current_branches[number] = view.head_ref_name
            remote_keys.add(key)
            if view.closing_issue is not None and branch_issue is not None and branch_issue != view.closing_issue:
                linkage_blocks[key] = "closing issue and branch identity conflict"
                for issue in (view.closing_issue, branch_issue):
                    linkage_blocks[WorkflowKey(scope.repository, IssueRef(issue))] = "conflicting issue-to-PR identity"
            if linked is not None:
                mapped_issues.setdefault(linked, []).append(number)
                remote_keys.add(WorkflowKey(scope.repository, IssueRef(linked)))
                if any(r.key.target == IssueRef(linked) and r.family is WorkflowFamily.ISSUE_RECOVERY
                       and any(name in {"session", "model", "branch"} and not isinstance(source, LegacyAbsent)
                               for name, source in r.fields) for r in records):
                    linkage_blocks[key] = "linked issue repair continuity requires its fixed handoff provenance parser"
        if read_open_pull_requests(scope.gateway, scope.repository) != listing:
            raise ResolveActivationRefused("current PR listing changed during continuation inventory")
        for issue, prs in mapped_issues.items():
            if len(prs) > 1:
                linkage_blocks[WorkflowKey(scope.repository, IssueRef(issue))] = "issue maps to multiple current PRs"
                linkage_blocks.update({WorkflowKey(scope.repository, PullRequestRef(pr)): "ambiguous shared issue-to-PR ownership" for pr in prs})
    with closing(control._connect()) as db:
        db.execute("BEGIN")
        try:
            inventories = tuple(family_inventory(db, scope.repository, family, None) for family in scope.families)
            if any(not i.quiescent for i in inventories):
                raise ResolveActivationRefused("continuation inventory has pending work: " + "; ".join(reason for i in inventories for reason in i.blockers()))
            if any(db.execute("SELECT 1 FROM workflow_authority_epochs WHERE repository=? AND family=?", (scope.repository, family.value)).fetchone() for family in scope.families):
                raise ResolveActivationRefused("continuation family is already active; retry its original saved plan")
            rows = db.execute("SELECT rowid,typeof(repository),length(CAST(repository AS BLOB)),typeof(target_kind),length(CAST(target_kind AS BLOB)),typeof(target_number),length(CAST(episode_json AS BLOB)) FROM workflows WHERE lower(trim(CAST(repository AS TEXT)))=lower(?) LIMIT ?", (scope.repository, MAX_IMPORT_TARGETS + 1)).fetchall()
            if len(rows) > MAX_IMPORT_TARGETS or any(row[1] != "text" or row[2] > 512 or row[3] != "text" or row[4] > 8 or row[5] != "integer" or row[6] > 262144 for row in rows):
                raise ResolveActivationRefused("current target authority exceeds continuation source bounds")
            identities = tuple(db.execute("SELECT repository,target_kind,target_number FROM workflows WHERE rowid=?", (row[0],)).fetchone() for row in rows)
            if any(row[0] != scope.repository or row[1] not in {"issue", "pr"} for row in identities):
                raise ResolveActivationRefused("current target authority has noncanonical source ownership")
            canonical_keys = {WorkflowKey(scope.repository, IssueRef(row[2]) if row[1] == "issue" else PullRequestRef(row[2])) for row in identities}
            keys = remote_keys | canonical_keys | {r.key for r in records}
            keys = {key for key in keys if isinstance(key.target, IssueRef) or WorkflowFamily.PR_REPAIR in scope.families}
            if len(keys) > MAX_IMPORT_TARGETS:
                raise ResolveActivationRefused("continuation target inventory exceeds its bound")
            kinds = ("TRIAGE", "GROOM") if scope.families == (WorkflowFamily.STEWARD,) else ("ROUTE", "RESOLVE", "RESUME", "AUTHOR", "IMPLEMENT", "ADJUDICATE", "PR_RESUME", "FIX_CI", "ADDRESS_REVIEW", "REBASE")
            unbound = db.execute("SELECT rowid,typeof(owner_id),length(CAST(owner_id AS BLOB)) FROM runs WHERE workflow_episode_key IS NULL LIMIT ?", (MAX_IMPORT_TARGETS + 1,)).fetchall()
            if len(unbound) > MAX_IMPORT_TARGETS:
                raise ResolveActivationRefused("unassigned run identity sources exceed the stopped import bound")
            selected_role = "steward" if scope.families == (WorkflowFamily.STEWARD,) else "fix"
            for row in unbound:
                if row[1] != "text" or type(row[2]) is not int or row[2] > 512:
                    raise ResolveActivationRefused("unassigned run owner is malformed or exceeds its identity bound")
                owner = db.execute("SELECT owner_id FROM runs WHERE rowid=?", (row[0],)).fetchone()[0]
                repository = _owner_repository(owner)
                role = owner.split(":", 2)[1] if ":" in owner else ""
                if repository is None or repository == scope.repository and (role == selected_role or role not in {"fix", "review", "steward"}):
                    raise ResolveActivationRefused("unassigned prior fix/steward run evidence requires its fixed recovery parser")
            targets = []
            authority_sources = []
            for key in sorted(keys, key=lambda k: (type(k.target).__name__, k.target.number)):
                if isinstance(key.target, PullRequestRef) and key not in canonical_keys:
                    raise ResolveActivationRefused(f"PR #{key.target.number}: canonical rejection workflow prerequisite is missing")
                current = load_issue_aggregate(db, key) if isinstance(key.target, IssueRef) else load_review_workflow_row(db, key)
                if isinstance(key.target, PullRequestRef) and current is None:
                    raise ResolveActivationRefused(f"PR #{key.target.number}: canonical rejection workflow prerequisite is missing")
                authority_sources.append((type(key.target).__name__, key.target.number, None if current is None else encode_workflow(current)))
                selected_families = (WorkflowFamily.STEWARD,) if scope.families == (WorkflowFamily.STEWARD,) else (WorkflowFamily.ISSUE_RECOVERY,) if isinstance(key.target, IssueRef) else (WorkflowFamily.PR_REPAIR,)
                for family in selected_families:
                    found = tuple(r for r in records if r.key == key and r.family is family)
                    names = ISSUE_FIELDS if family is WorkflowFamily.ISSUE_RECOVERY else PR_FIELDS if family is WorkflowFamily.PR_REPAIR else STEWARD_FIELDS
                    selected = found[0] if found else LegacyContinuationRecord(key, family, "steward" if family is WorkflowFamily.STEWARD else "fix", tuple((name, LegacyAbsent()) for name in names))
                    value = classify_record(selected)
                    blockers = list(value) if isinstance(value, tuple) else []
                    if isinstance(value, cc.IssueRecoveryState) and value.review_failures.review_at:
                        prs = mapped_issues.get(key.target.number, [])
                        if len(prs) != 1:
                            blockers.append(cc.ContinuationImportBlocker("review-linkage:" + selected.source_digest,
                                "review-retries: requires one verified current linked PR"))
                        else:
                            value = replace(value, review_failures=replace(value.review_failures, pr=PullRequestRef(prs[0])))
                    if len(found) > 1 or found and selected.root not in (("steward",) if family is WorkflowFamily.STEWARD else ("fix",)) and not selected.root.startswith("extra:"):
                        blockers.append(cc.ContinuationImportBlocker("roots:" + selected.source_digest, "duplicate or conflicting source owner"))
                    if key in linkage_blocks:
                        blockers.append(cc.ContinuationImportBlocker("linkage:" + digest(mappings), linkage_blocks[key]))
                    if family is WorkflowFamily.PR_REPAIR and isinstance(value, cc.PRRepairState) and value.branch and value.branch != current_branches.get(key.target.number):
                        blockers.append(cc.ContinuationImportBlocker("branch:" + selected.source_digest, "legacy PR branch lacks matching current remote ownership"))
                    prior = db.execute("SELECT 1 FROM runs WHERE workflow_episode_key>=? AND workflow_episode_key<? AND kind IN (" + ",".join("?" for _ in kinds) + ") LIMIT 1", (*workflow_run_scope_bounds(key), *kinds)).fetchone()
                    if prior:
                        blockers.append(cc.ContinuationImportBlocker("runs:" + digest((scope.repository, family.value, key.target.number)), "prior paid recovery history requires its fixed family parser"))
                    targets.append(ContinuationImportTarget(key, family, None if current is None else current.revision,
                                   digest(tuple(r.source_digest for r in found) or (selected.source_digest,)),
                                   None if blockers else value, tuple(blockers),
                                   authority_digest=digest(None if current is None else encode_workflow(current))))
            return ContinuationImportPlan(scope.repository, scope.families, 1, digest((file_digest, mappings, authority_sources)),
                                          digest(tuple(i.digest for i in inventories)), file_digest, tuple(targets))
        finally:
            db.rollback()


def report(plan: ContinuationImportPlan, *, status: str = "ready") -> dict[str, object]:
    return {"status": status, "repository": plan.repository, "plan_digest": plan.plan_digest,
            "prerequisite": "PR repair requires an existing canonical rejection workflow; inventory and rehearsal never initialize primary authority",
            "targets": [{"family": t.family.value, "number": t.key.target.number, "eligible": t.state is not None,
                         "blockers": [b.reason for b in t.blockers]} for t in plan.targets],
            "original_plan": encode_workflow(plan)}


def inspect(scope: ContinuationActivationScope, *, shadow: bool) -> dict[str, object]:
    control = SQLiteControlStore(scope.control_path)
    control.require_initialized()
    records, file_digest = read_continuation_roots(scope.repository, scope.legacy_roots, scope.families)
    plan = _evaluate(scope, control, records, file_digest)
    result = report(plan)
    if shadow:
        result["issue_reply_shadow"] = [{"issue": key.target.number, "legacy": before, "imported": after, "agrees": before == after}
                                        for key, before, after in issue_reply_shadow(plan, records)]
        result["ci_shadow"] = [{"issue": key.target.number, "legacy": before, "imported": after, "agrees": before == after}
                              for key, before, after in ci_shadow(plan, records)]
        result["rebase_shadow"] = [{"issue": key.target.number, "legacy": before, "imported": after, "agrees": before == after}
                                  for key, before, after in rebase_shadow(plan, records)]
        result["review_shadow"] = [{"issue": key.target.number, "legacy": before, "imported": after, "agrees": before == after}
                                  for key, before, after in review_shadow(plan, records)]
        result["triage_shadow"] = [{"issue": key.target.number, "legacy": before, "imported": after, "agrees": before == after}
                                   for key, before, after in triage_shadow(plan, records)]
        result["groom_shadow"] = [{"issue": key.target.number, "legacy": before, "imported": after, "agrees": before == after}
                                  for key, before, after in groom_shadow(plan, records)]
    return result


def activate(scope: ContinuationActivationScope, *, confirm_plan_digest: str, plan_file: str, liveness) -> dict[str, object]:
    plan = read_import_plan(plan_file)
    if plan.repository != scope.repository or plan.families != scope.families or plan.plan_digest != confirm_plan_digest:
        raise ResolveActivationRefused("confirmed continuation plan differs from configured scope/original digest")
    control = SQLiteControlStore(scope.control_path)
    control.require_initialized()
    with control.transaction() as db:
        if verify_original_plan(db, plan):
            return report(plan, status="already_active_with_this_plan")
    with ExitStack() as barriers:
        for role in ("fix", "review", "steward"):
            barriers.enter_context(deployment_effect_barrier(control.path, scope.repository, role))
        with control.transaction() as db:
            if verify_original_plan(db, plan):
                return report(plan, status="already_active_with_this_plan")
        if liveness():
            raise ResolveActivationRefused("watchers may be running; stopped continuation activation required")
        if evaluate(scope, control) != plan:
            raise ResolveActivationRefused("continuation sources changed since original plan review")
        if liveness():
            raise ResolveActivationRefused("watchers may be running before continuation publication")
        _bind_operator_feed(control, scope)
        with control.transaction() as db:
            if read_continuation_roots(scope.repository, scope.legacy_roots, scope.families)[1] != plan.file_digest:
                raise ResolveActivationRefused("continuation sources changed before publication")
            publish_continuation_import(db, plan, now=datetime.now(timezone.utc), control=control)
    return report(plan, status="activated")


def rehearse(scope: ContinuationActivationScope, *, copy_to: str) -> dict[str, object]:
    records, _ = read_continuation_roots(scope.repository, scope.legacy_roots, scope.families)
    files = tuple(os.path.join(path, name) for _, path in scope.legacy_roots if os.path.isdir(path)
                  for name in os.listdir(path) if LEGACY_CONTINUATION_NAME.fullmatch(name))
    originals = (scope.control_path, scope.control_path + ".authority", *files)
    before = file_digests(originals)
    copied = copy_for_rehearsal(scope.control_path, scope.legacy_roots, copy_to, protected=(scope.event_dir,),
                                legacy_name=LEGACY_CONTINUATION_NAME, canonical_layout=True)
    control = SQLiteControlStore(copied.control_path)
    control.initialize()
    selected = replace(scope, control_path=copied.control_path, legacy_roots=copied.roots, event_dir=os.path.join(copy_to, "operator-events"))
    if read_continuation_roots(scope.repository, copied.roots, scope.families)[0] != records:
        raise ResolveActivationRefused("copied continuation sources differ before rehearsal publication")
    plan = evaluate(selected, control)
    _bind_operator_feed(control, selected)
    with control.transaction() as db:
        publish_continuation_import(db, plan, now=datetime.now(timezone.utc), control=control)
        retry = verify_original_plan(db, plan)
    checks = {"sources_match": read_continuation_roots(scope.repository, copied.roots, scope.families)[0] == records,
              "originals_unchanged": file_digests(originals) == before, "original_retry": retry}
    result = report(plan, status="rehearsed" if all(checks.values()) else "refused")
    result["checks"] = checks
    return result
