"""Bounded stopped source reads and publication into existing target aggregates."""

from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from datetime import datetime
import os
import re
import sqlite3

from ..domain import continuation_components as cc
from ..domain.continuation_import import (
    ISSUE_FIELDS, PR_FIELDS, MAX_IMPORT_TARGETS,
    ContinuationImportPlan, ContinuationImportTarget, LegacyContinuationRecord, digest,
)
from ..domain.resolve_activation import LegacyAbsent, LegacyPresent, LegacyUnreadable, MAX_LEGACY_FILE_BYTES
from ..domain.workflow import IssueFamilyEpisodes, IssueWorkflowShell
from ..domain.review_workflow import ReviewFamilyEpisodes, ReviewWorkflowSnapshot
from ..domain.workflow_identity import (
    ContinuationComponentIdentity, FamilyAuthority, IssueRef, PullRequestRef, WorkflowFamily,
    WorkflowFence, WorkflowKey, WorkflowRevision,
)
from .control_store import SQLiteControlStore
from .continuation_run_import import collect_continuation_runs
from .resolve_activation import LegacyRoots, ResolveActivationRefused, _read_legacy_file, family_inventory
from .workflow_codec import MAX_WORKFLOW_PAYLOAD_BYTES, _unique_object, decode_workflow, encode_workflow
from .workflow_rows import (
    WorkflowAuthorityError, _require_secondary_component, encode_workflow_episodes, insert_workflow_component, load_issue_aggregate,
    load_review_workflow_row, persist_workflow_snapshot, workflow_scope,
)

LEGACY_CONTINUATION_NAME = re.compile(r"(?:issue|pr)-.*\.(?:" + "|".join(set(ISSUE_FIELDS + PR_FIELDS)) + r")")
_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
MAX_SOURCE_BYTES = 4 * MAX_LEGACY_FILE_BYTES


def read_continuation_roots(repository: str, roots: LegacyRoots, families: tuple[WorkflowFamily, ...]) -> tuple[tuple[LegacyContinuationRecord, ...], str]:
    """One bounded observation of the explicitly selected source universe."""
    if len(roots) > 32 or len({os.path.realpath(path) for _, path in roots}) != len(roots) or len({label for label, _ in roots}) != len(roots):
        raise ResolveActivationRefused("duplicate or excessive continuation roots/owners")
    records: list[LegacyContinuationRecord] = []
    identities: list[object] = []
    file_sources: list[object] = []
    total_bytes = 0
    listed = 0
    entries_seen = 0
    for label, path in roots:
        try:
            metadata = os.stat(path)
            identities.append((label, os.path.realpath(path), metadata.st_dev, metadata.st_ino))
            selected: dict[tuple[WorkflowKey, WorkflowFamily], dict[str, object]] = {}
            with os.scandir(path) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > MAX_IMPORT_TARGETS * 8:
                        raise ResolveActivationRefused("continuation root inventory exceeds its directory entry bound")
                    if not LEGACY_CONTINUATION_NAME.fullmatch(entry.name):
                        continue
                    listed += 1
                    if listed > MAX_IMPORT_TARGETS:
                        raise ResolveActivationRefused("selected continuation files exceed the inventory bound")
                    kind, tail = entry.name.split("-", 1)
                    number, field = tail.rsplit(".", 1)
                    family = WorkflowFamily.PR_REPAIR if kind == "pr" else WorkflowFamily.ISSUE_RECOVERY
                    target = PullRequestRef if kind == "pr" else IssueRef
                    if kind == "issue" and number.startswith("-") and field == "infra-deaths":
                        number, family, target = number[1:], WorkflowFamily.PR_REPAIR, PullRequestRef
                    # Review-owned session/death files belong to its accepted importer.
                    if family not in families or label == "review" and (field == "infra-deaths" or entry.name.startswith("pr-") and field == "session"):
                        continue
                    if not _NUMBER.fullmatch(number) or int(number) > 2**63 - 1:
                        raise ResolveActivationRefused(f"unassigned continuation identity: {entry.name[:128]}")
                    names = ISSUE_FIELDS if family is WorkflowFamily.ISSUE_RECOVERY else PR_FIELDS
                    if field not in names:
                        raise ResolveActivationRefused("selected continuation field has contradictory target ownership")
                    key = WorkflowKey(repository, target(int(number)))
                    source = _read_legacy_file(entry.path)
                    if isinstance(source, LegacyAbsent):
                        source = LegacyUnreadable("selected file disappeared during inventory")
                    total_bytes += len(source.text.encode()) if isinstance(source, LegacyPresent) else 0
                    if total_bytes > MAX_SOURCE_BYTES:
                        raise ResolveActivationRefused("selected continuation sources exceed their byte bound")
                    fingerprint = source.digest if isinstance(source, LegacyPresent) else digest((type(source).__name__, source.reason))
                    file_sources.append((label, entry.name, fingerprint))
                    selected.setdefault((key, family), {}).setdefault(field, []).append((entry.name, source))
            for (key, family), fields in sorted(selected.items(), key=lambda x: (x[0][1].value, x[0][0].target.number)):
                names = ISSUE_FIELDS if family is WorkflowFamily.ISSUE_RECOVERY else PR_FIELDS
                values = []
                for name in names:
                    sources = sorted(fields.get(name, []), key=lambda item: item[0])
                    if not sources:
                        value = LegacyAbsent()
                    elif len(sources) == 1:
                        value = sources[0][1]
                    else:
                        evidence = tuple((filename, source.digest if isinstance(source, LegacyPresent) else source.reason) for filename, source in sources)
                        value = LegacyUnreadable("duplicate normalized field sources: " + digest(evidence))
                    values.append((name, value))
                records.append(LegacyContinuationRecord(key, family, label, tuple(values)))
        except FileNotFoundError:
            if identities and len(identities[-1]) == 4 and identities[-1][0] == label:
                raise ResolveActivationRefused(f"continuation root disappeared during observation: {label}")
            identities.append((label, os.path.realpath(path), "absent"))
        except OSError as exc:
            raise ResolveActivationRefused(f"continuation root is unreadable: {label}: {exc}") from exc
    records.sort(key=lambda r: (r.family.value, r.key.target.number, r.root))
    return tuple(records), digest((identities, sorted(file_sources), tuple(r.source_digest for r in records)))


def read_import_plan(path: str) -> ContinuationImportPlan:
    import json
    import stat
    bound = MAX_WORKFLOW_PAYLOAD_BYTES * 4
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as saved:
        metadata = os.fstat(saved.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > bound:
            raise ResolveActivationRefused("original continuation plan is not regular or exceeds its bound")
        raw = saved.read(bound + 1)
    if len(raw) > bound:
        raise ResolveActivationRefused("original continuation plan exceeds its bound")
    report = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(report, dict) or not isinstance(report.get("original_plan"), str):
        raise ResolveActivationRefused("saved continuation report lacks its closed original plan")
    return decode_workflow(report["original_plan"], ContinuationImportPlan)


def verify_original_plan(db: sqlite3.Connection, plan: ContinuationImportPlan) -> bool:
    """Exact immutable selected decisions, independent of later legitimate work."""
    active = tuple(db.execute("SELECT 1 FROM workflow_authority_epochs WHERE repository=? AND family=?", (plan.repository, family.value)).fetchone() for family in plan.families)
    if not any(active):
        return False
    for family in plan.families:
        row = db.execute("SELECT epoch,source_digest,inventory_digest FROM workflow_authority_epochs WHERE repository=? AND family=? AND typeof(epoch)='integer' AND typeof(source_digest)='text' AND typeof(inventory_digest)='text' AND length(CAST(source_digest AS BLOB))=64 AND length(CAST(inventory_digest AS BLOB))=64", (plan.repository, family.value)).fetchone()
        evidence = db.execute("SELECT plan_digest FROM workflow_activation_evidence WHERE repository=? AND family=? AND epoch=? AND typeof(plan_digest)='text' AND length(CAST(plan_digest AS BLOB))=64", (plan.repository, family.value, plan.epoch)).fetchone()
        if row is None or tuple(row) != (plan.epoch, plan.source_digest, plan.inventory_digest) or evidence is None or evidence[0] != plan.plan_digest:
            raise ResolveActivationRefused("continuation original plan conflicts with accepted authority")
    for target in plan.targets:
        deferred = target.family is WorkflowFamily.PR_REPAIR and target.revision is None and target.blockers
        if deferred:
            if deferred_pr_repair(db, target.key) != imported_component(plan, target):
                raise ResolveActivationRefused("continuation deferred selected decision lost its receipt")
        else:
            row = db.execute("SELECT epoch,source_digest,decision_json FROM workflow_activation_imports WHERE repository=? AND family=? AND target_kind=? AND target_number=? AND typeof(epoch)='integer' AND typeof(source_digest)='text' AND length(CAST(source_digest AS BLOB))=64 AND typeof(decision_json)='text' AND length(CAST(decision_json AS BLOB))<=?", (plan.repository, target.family.value, *workflow_scope(target.key)[1:], MAX_WORKFLOW_PAYLOAD_BYTES)).fetchone()
            if row is None or tuple(row) != (plan.epoch, plan.source_digest, encode_workflow(target)):
                raise ResolveActivationRefused("continuation original selected decision is missing or conflicting")
        current = db.execute("SELECT generation,version,episode_json FROM workflows WHERE repository=? AND target_kind=? AND target_number=? AND typeof(episode_json)='text' AND length(CAST(episode_json AS BLOB))<=?", (*workflow_scope(target.key), MAX_WORKFLOW_PAYLOAD_BYTES)).fetchone()
        if current is None:
            if (target.family is WorkflowFamily.PR_REPAIR and target.revision is None
                    and target.blockers and not db.execute(
                        "SELECT 1 FROM workflows WHERE repository=? AND target_kind=? AND target_number=?",
                        workflow_scope(target.key)).fetchone() and not db.execute(
                        "SELECT 1 FROM workflow_components WHERE repository=? AND target_kind=? AND target_number=?",
                        workflow_scope(target.key)).fetchone()):
                continue
            raise ResolveActivationRefused("accepted selected continuation target disappeared or is unreadable")
        expected = IssueFamilyEpisodes | IssueWorkflowShell if isinstance(target.key.target, IssueRef) else ReviewFamilyEpisodes
        payload = decode_workflow(current[2], expected)
        revision = WorkflowRevision(WorkflowFence(target.key, current[0]), current[1])
        if isinstance(payload, IssueWorkflowShell) and payload.revision != revision:
            raise ResolveActivationRefused("selected continuation shell lost its exact revision")
        component = payload.issue_recovery if target.family is WorkflowFamily.ISSUE_RECOVERY else payload.pr_repair
        cc.validate_component_revision(component, revision, target.family)
        _require_secondary_component(db, target.key, component, target.family)
        if target.blockers:
            if component != imported_component(plan, target):
                raise ResolveActivationRefused("accepted continuation migration blocker disappeared")
        elif not isinstance(component, (cc.ActiveIssueRecovery, cc.ActivePRRepair)):
            raise ResolveActivationRefused("accepted selected continuation active authority disappeared")
    return True


def imported_component(plan: ContinuationImportPlan, target):
    authority = FamilyAuthority(target.family, plan.epoch)
    identity = digest((plan.plan_digest, target.family.value, target.key.target.number))
    if target.blockers:
        if target.family is WorkflowFamily.ISSUE_RECOVERY:
            return cc.BlockedIssueRecovery(target.key, authority, identity, target.blockers)
        return cc.BlockedPRRepair(target.key, authority, identity, target.blockers)
    component_id = ContinuationComponentIdentity(target.key, authority, "import:" + identity)
    if target.family is WorkflowFamily.ISSUE_RECOVERY:
        return cc.ActiveIssueRecovery(component_id, target.state)
    return cc.ActivePRRepair(component_id, target.state)


def continuation_inventory_digest(db: sqlite3.Connection, repository: str, families: tuple[WorkflowFamily, ...]) -> str:
    histories = collect_continuation_runs(db, repository, families)
    inventory = digest(tuple(family_inventory(db, repository, family, None).digest for family in families))
    return inventory if histories.source_digest == digest([]) else digest((inventory, histories.source_digest))


def deferred_pr_repair(db: sqlite3.Connection, key: WorkflowKey) -> cc.BlockedPRRepair | None:
    if not isinstance(key.target, PullRequestRef):
        raise ValueError("deferred PR repair requires a PR target")
    row = db.execute(
        "SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-deferred-pr' AND scope=? AND key=? "
        "AND typeof(payload_json)='text' AND length(CAST(payload_json AS BLOB))<=?",
        (key.repository, str(key.target.number), MAX_WORKFLOW_PAYLOAD_BYTES),
    ).fetchone()
    if row is None:
        if db.execute("SELECT 1 FROM dedupe_keys WHERE namespace='continuation-deferred-pr' AND scope=? AND key=?",
                      (key.repository, str(key.target.number))).fetchone():
            raise ResolveActivationRefused("retained PR repair decision is unreadable")
        return None
    receipt = decode_workflow(row[0], tuple)
    if len(receipt) != 5:
        raise ResolveActivationRefused("retained PR repair decision has malformed receipt")
    retained_epoch, source_digest, plan_digest, target, decision_digest = receipt
    if not isinstance(target, ContinuationImportTarget):
        raise ResolveActivationRefused("retained PR repair decision lacks a typed target")
    epoch = db.execute("SELECT epoch,source_digest FROM workflow_authority_epochs WHERE repository=? AND family='pr_repair' "
                       "AND typeof(epoch)='integer' AND typeof(source_digest)='text' AND length(CAST(source_digest AS BLOB))=64",
                       (key.repository,)).fetchone()
    plan_receipt = db.execute("SELECT plan_digest FROM workflow_activation_evidence WHERE repository=? AND family='pr_repair' AND epoch=? "
                             "AND typeof(plan_digest)='text' AND length(CAST(plan_digest AS BLOB))=64",
                             (key.repository, retained_epoch)).fetchone()
    if (type(retained_epoch) is not int or epoch is None or tuple(epoch) != (retained_epoch, source_digest)
            or not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest)
            or plan_receipt is None or plan_receipt[0] != plan_digest
            or not isinstance(plan_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_digest)
            or decision_digest != digest(encode_workflow(target))
            or target.key != key or target.family is not WorkflowFamily.PR_REPAIR
            or target.revision is not None or not target.blockers):
        raise ResolveActivationRefused("retained PR repair decision lacks its exact deferred authority")
    return cc.BlockedPRRepair(key, FamilyAuthority(WorkflowFamily.PR_REPAIR, retained_epoch),
                              digest((plan_digest, WorkflowFamily.PR_REPAIR.value, key.target.number)), target.blockers)


def retain_deferred_pr_repair(db: sqlite3.Connection, snapshot: ReviewWorkflowSnapshot) -> ReviewWorkflowSnapshot:
    try:
        retained = deferred_pr_repair(db, snapshot.revision.fence.key)
    except ValueError as exc:
        raise WorkflowAuthorityError("deferred PR repair import is unreadable") from exc
    if retained is None:
        return snapshot
    if not isinstance(snapshot.pr_repair, cc.InactivePRRepair) and snapshot.pr_repair != retained:
        raise WorkflowAuthorityError("canonical PR repair conflicts with its retained migration hold")
    return replace(snapshot, pr_repair=retained)


def publish_continuation_import(db: sqlite3.Connection, plan: ContinuationImportPlan, *, now: datetime, control: SQLiteControlStore) -> None:
    if (not isinstance(db, sqlite3.Connection) or not db.in_transaction
            or not isinstance(plan, ContinuationImportPlan) or not isinstance(now, datetime)
            or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("continuation publication requires an existing transaction, typed plan and aware time")
    with SQLiteControlStore.mutation_savepoint(db):
        if verify_original_plan(db, plan):
            return
        current = {}
        for target in plan.targets:
            if isinstance(target.key.target, PullRequestRef) and not db.execute("SELECT 1 FROM workflows WHERE repository=? AND target_kind=? AND target_number=?", workflow_scope(target.key)).fetchone():
                if target.revision is None and target.blockers:
                    current[target.key] = None
                    continue
                raise ResolveActivationRefused(f"PR #{target.key.target.number}: canonical rejection workflow prerequisite is missing")
            value = load_issue_aggregate(db, target.key) if isinstance(target.key.target, IssueRef) else load_review_workflow_row(db, target.key)
            if isinstance(target.key.target, PullRequestRef) and value is None:
                raise ResolveActivationRefused(f"PR #{target.key.target.number}: canonical rejection workflow prerequisite is missing")
            if (None if value is None else value.revision) != target.revision:
                raise ResolveActivationRefused("selected continuation aggregate changed before publication")
            if digest(None if value is None else encode_workflow(value)) != target.authority_digest:
                raise ResolveActivationRefused("selected continuation authority content changed before publication")
            current[target.key] = value
        inventories = tuple(family_inventory(db, plan.repository, family, None) for family in plan.families)
        if any(not i.quiescent for i in inventories) or continuation_inventory_digest(db, plan.repository, plan.families) != plan.inventory_digest:
            raise ResolveActivationRefused("continuation publication requires unchanged quiescent inventory")
        for family in plan.families:
            db.execute("INSERT INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)", (plan.repository, family.value, plan.epoch, plan.source_digest, plan.inventory_digest, now.isoformat()))
            db.execute("INSERT INTO workflow_activation_evidence VALUES (?,?,?,?,?)", (plan.repository, family.value, plan.epoch, 0, plan.plan_digest))
        for target in plan.targets:
            value = current[target.key]
            if value is None and isinstance(target.key.target, PullRequestRef):
                receipt = (plan.epoch, plan.source_digest, plan.plan_digest, target, digest(encode_workflow(target)))
                db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES ('continuation-deferred-pr',?,?,?,?)",
                           (plan.repository, str(target.key.target.number), now.isoformat(), encode_workflow(receipt)))
                continue
            if value is None:
                value = IssueWorkflowShell(WorkflowRevision(WorkflowFence(target.key, 0), 0))
                db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)", (*workflow_scope(target.key), 0, 0, encode_workflow_episodes(value)))
            component = imported_component(plan, target)
            if isinstance(component, (cc.ActiveIssueRecovery, cc.ActivePRRepair)):
                insert_workflow_component(db, target.key, component.identity.authority, component.identity.episode_id)
            value = replace(value, **({"issue_recovery": component} if target.family is WorkflowFamily.ISSUE_RECOVERY else {"pr_repair": component}))
            current[target.key] = value
            db.execute("INSERT INTO workflow_activation_imports VALUES (?,?,?,?,?,?,?)", (plan.repository, target.family.value, plan.epoch, *workflow_scope(target.key)[1:], plan.source_digest, encode_workflow(target)))
        for value in current.values():
            if value is None:
                continue
            after = replace(value, revision=replace(value.revision, version=value.revision.version + 1))
            persist_workflow_snapshot(db, after)
