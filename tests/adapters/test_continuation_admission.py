"""Fixed component admission shares actual SQLite money and immutable run receipts."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from issue_agent.adapters.control_store import AccountingDisposition, SQLiteControlStore, SettlementRequest
from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
from issue_agent.adapters.workflow_rows import (
    WorkflowAuthorityError, encode_workflow_episodes, insert_workflow_component, load_issue_aggregate,
    load_review_workflow_row, load_workflow_read, persist_workflow_snapshot, workflow_scope,
)
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.contracts import Money, PricingBasis
from issue_agent.domain.workflow import IssueWorkflowShell, WorkflowMigrationRequired, WorkflowSnapshot
from issue_agent.domain.workflow_identity import ContinuationComponentIdentity, FamilyAuthority, WorkflowFamily, WorkflowRevision
from tests.adapters.test_workflow_admission import NOW, request
from tests.fakes.review_workflow import held
from tests.fakes.workflow_authority import install_workflow, resolve_snapshot


def active(key, family):
    identity = ContinuationComponentIdentity(key, FamilyAuthority(family, 1), "secondary:" + family.value)
    if family is WorkflowFamily.ISSUE_RECOVERY:
        return cc.ActiveIssueRecovery(identity, cc.IssueRecoveryState())
    if family is WorkflowFamily.PR_REPAIR:
        return cc.ActivePRRepair(identity, cc.PRRepairState())
    return cc.ActiveSteward(identity, cc.StewardState())


def install_secondary(store, aggregate, component):
    key = aggregate.revision.fence.key
    with store.transaction() as db:
        db.execute("INSERT OR IGNORE INTO workflow_authority_epochs VALUES (?,?,?,?,?,?)",
                   (key.repository, component.identity.authority.family.value, 1, "test-source", "test-plan", NOW.isoformat()))
        insert_workflow_component(db, key, component.identity.authority, component.identity.episode_id)
        db.execute("UPDATE workflows SET episode_json=? WHERE repository=? AND target_kind=? AND target_number=?",
                   (encode_workflow_episodes(aggregate), *workflow_scope(key)))


@pytest.fixture
def recovery(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    base = resolve_snapshot()
    install_workflow(store, base)
    component = active(base.revision.fence.key, WorkflowFamily.ISSUE_RECOVERY)
    aggregate = replace(base, issue_recovery=component)
    install_secondary(store, aggregate, component)
    return store, aggregate


def purchase(aggregate, *, reply=None, ceiling=2, leg="leg:first"):
    selected = cc.select_issue_recovery(aggregate)
    return cc.IssueRecoveryPurchase(cc.ContinuationCapture(selected.binding, selected.revision, leg),
                                    reply or NOW.isoformat(), "fix/1-reply", ceiling)


def paid(name, selected, *, cap=100):
    return replace(request(name, cap=cap), workflow=selected.capture.binding,
                   dispatch_id=json.dumps({"repository": "o/r", "role": "fix", "issue": 1,
                                           "leg_id": selected.capture.leg_id}))


def read(store, aggregate):
    with store.transaction() as db:
        return load_issue_aggregate(db, aggregate.revision.fence.key)


def receipt(store, run_id, family=WorkflowFamily.ISSUE_RECOVERY):
    with store.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-admission' AND scope='o/r' AND key=?",
                         (store._continuation_receipt_key(run_id, family),)).fetchone()
    return decode_workflow(raw[0], tuple)[1]


def money(run_id):
    return SettlementRequest(run_id, Money("USD", 1), PricingBasis.PROVIDER_REPORTED,
                             "provider:" + str(run_id), None, None, None)


def test_competing_admissions_consume_reply_and_resume_allowance_once(recovery):
    store, aggregate = recovery
    selected = purchase(aggregate, ceiling=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda name: store.reserve_issue_recovery(paid(name, selected), selected), ("first", "second")))
    assert sum(result.admitted for result in results) == 1
    current = read(store, aggregate)
    assert current.issue_recovery.state.consumed_reply_at == selected.reply_at
    assert current.issue_recovery.state.resumes == 1
    assert current.issue_recovery.state.remembered_branch == selected.branch
    assert current.episode == aggregate.episode
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 1


def test_budget_refusal_consumes_nothing(recovery):
    store, aggregate = recovery
    selected = purchase(aggregate)
    refused = store.reserve_issue_recovery(paid("first", selected, cap=0), selected)
    assert refused.disposition is AccountingDisposition.EXHAUSTED
    assert read(store, aggregate) == aggregate
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='continuation-admission'").fetchone()[0] == 0


def test_failure_after_component_consumption_rolls_back_money_and_receipt(recovery):
    store, aggregate = recovery
    selected = purchase(aggregate)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER fail_reservation BEFORE INSERT ON budget_reservations BEGIN SELECT RAISE(ABORT,'test refusal'); END")
    assert not store.reserve_issue_recovery(paid("first", selected), selected).admitted
    assert read(store, aggregate) == aggregate
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='continuation-admission'").fetchone()[0] == 0


def test_current_duplicate_return_applies_once_and_retains_consumption(recovery):
    store, aggregate = recovery
    selected = purchase(aggregate)
    request = paid("first", selected)
    assert store.reserve_issue_recovery(request, selected).admitted
    admission = receipt(store, request.run_id)
    # CI/review/rebase debt requires its classified paired repair receipt. This legacy
    # reply-return contract still applies unselected sibling state exactly once.
    output = cc.IssueRecoveryState(infra_failures=2)
    assert store.settle_issue_recovery(money(request.run_id), admission, output).disposition is AccountingDisposition.AVAILABLE
    current = read(store, aggregate)
    assert current.issue_recovery.state.infra_failures == 2
    assert current.issue_recovery.state.resumes == 1
    assert current.issue_recovery.state.consumed_reply_at == selected.reply_at
    assert store.settle_issue_recovery(money(request.run_id), admission, output).idempotent
    assert read(store, aggregate) == current
    with pytest.raises(WorkflowAuthorityError, match="immutable result"):
        store.settle_issue_recovery(money(request.run_id), admission, replace(output, infra_failures=0))
    assert store.snapshot(request.scope).settled.micros == 1


def test_old_generation_money_settles_without_erasing_newer_components_and_retry_is_inert(recovery):
    store, aggregate = recovery
    selected = purchase(aggregate)
    request = paid("first", selected)
    assert store.reserve_issue_recovery(request, selected).admitted
    admission = receipt(store, request.run_id)
    current = read(store, aggregate)
    changed = replace(current, revision=WorkflowRevision(replace(current.revision.fence, generation=3), 2),
                      issue_recovery=replace(current.issue_recovery, state=replace(current.issue_recovery.state, infra_failures=3)))
    with store.transaction() as db:
        persist_workflow_snapshot(db, changed)
    restarted = SQLiteControlStore(store.path, now=lambda: NOW)
    assert restarted.reserve_issue_recovery(request, selected).admitted
    assert read(store, aggregate) == changed
    assert restarted.settle_issue_recovery(money(request.run_id), admission, cc.IssueRecoveryState()).disposition is AccountingDisposition.AVAILABLE
    assert read(store, aggregate) == changed
    assert restarted.snapshot(request.scope).settled.micros == 1
    assert restarted.settle_issue_recovery(money(request.run_id), admission, cc.IssueRecoveryState()).idempotent


def test_forged_admission_cannot_settle_or_apply(recovery):
    store, aggregate = recovery
    selected = purchase(aggregate)
    request = paid("first", selected)
    assert store.reserve_issue_recovery(request, selected).admitted
    actual = receipt(store, request.run_id)
    forged = replace(actual, attempt=replace(actual.attempt, leg_id="other"))
    with pytest.raises(WorkflowAuthorityError, match="exact admission"):
        store.settle_issue_recovery(money(request.run_id), forged, cc.IssueRecoveryState())
    assert store.snapshot(request.scope).settled.micros == 0


@pytest.mark.parametrize("corruption", ["missing_payload", "malformed_payload", "missing_index"])
def test_activated_component_corruption_is_refused_without_money(recovery, corruption):
    store, aggregate = recovery
    selected = purchase(aggregate)
    with store.transaction() as db:
        if corruption == "missing_index":
            db.execute("DELETE FROM workflow_components WHERE family='issue_recovery'")
        else:
            raw = encode_workflow_episodes(resolve_snapshot()) if corruption == "missing_payload" else '{"invalid":true}'
            db.execute("UPDATE workflows SET episode_json=?", (raw,))
    assert not store.reserve_issue_recovery(paid("first", selected), selected).admitted
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_steward_only_shell_persists_and_admits_without_resolve_authority(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    revision = resolve_snapshot(7).revision
    component = active(revision.fence.key, WorkflowFamily.STEWARD)
    shell = IssueWorkflowShell(revision, steward=component)
    with store.transaction() as db:
        db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)",
                   (*workflow_scope(revision.fence.key), revision.fence.generation, revision.version, encode_workflow(shell)))
    install_secondary(store, shell, component)
    with store.transaction() as db:
        assert isinstance(load_workflow_read(db, revision.fence.key), WorkflowMigrationRequired)
    selected = cc.select_steward(shell)
    purchase = cc.StewardPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:triage"),
                                  cc.StewardLane.TRIAGE, NOW.isoformat(), 2)
    requested = replace(request("triage"), workflow=selected.binding, owner_id="o/r:steward:test", kind="TRIAGE",
        dispatch_id=json.dumps({"repository":"o/r", "role":"steward", "kind":"TRIAGE", "issue":7, "number":7, "leg_id":"leg:triage"}))
    assert store.reserve_steward(requested, purchase).admitted
    admitted = receipt(store, requested.run_id, WorkflowFamily.STEWARD)
    output = cc.StewardState(triage=cc.StewardLaneState(stalls=1))
    assert store.settle_steward(money(requested.run_id), admitted, output).disposition is AccountingDisposition.AVAILABLE
    current = read(store, shell)
    assert isinstance(current, IssueWorkflowShell) and current.steward.state.triage.stalls == 1
    assert not hasattr(current, "episode")


def test_pr_repair_allowance_and_actual_return_are_independent_of_rejection_episode(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    base = replace(held(), gates=())
    install_workflow(store, base)
    component = active(base.revision.fence.key, WorkflowFamily.PR_REPAIR)
    aggregate = replace(base, pr_repair=component)
    install_secondary(store, aggregate, component)
    selected = cc.select_pr_repair(aggregate)
    purchase = cc.PRRepairPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:repair"),
                                  allowance=cc.RepairAllowance.INFRA, ceiling=1)
    requested = replace(request("repair"), workflow=selected.binding)
    assert store.reserve_pr_repair(requested, purchase).admitted
    admitted = receipt(store, requested.run_id, WorkflowFamily.PR_REPAIR)
    output = cc.PRRepairState(infra_failures=1)
    assert store.settle_pr_repair(money(requested.run_id), admitted, output).disposition is AccountingDisposition.AVAILABLE
    with store.transaction() as db:
        current = load_review_workflow_row(db, base.revision.fence.key)
    assert current.episode == base.episode and current.pr_repair.state.infra_failures == 1
    capture = cc.ContinuationCapture(cc.select_pr_repair(current).binding, current.revision, "leg:repair2")
    assert not store.reserve_pr_repair(replace(request("repair2"), workflow=capture.binding), replace(purchase, capture=capture)).admitted


def test_current_launch_and_exact_registered_retry_preserve_fences(recovery):
    store, aggregate = recovery
    selected = purchase(aggregate)
    requested = paid("first", selected)
    assert not store.requires_workspace_entry(requested)
    assert store.reserve_issue_recovery(requested, selected).admitted
    lease = store.register_process_lease(requested.run_id, expected_owner_id=requested.owner_id,
                                         pid=123, pgid=123, process_start_ticks=456)
    current = read(store, aggregate)
    changed = replace(current, revision=WorkflowRevision(replace(current.revision.fence, generation=3), 2))
    with store.transaction() as db:
        persist_workflow_snapshot(db, changed)
    assert store.register_process_lease(requested.run_id, expected_owner_id=requested.owner_id,
                                        pid=123, pgid=123, process_start_ticks=456) == lease


def test_fresh_launch_after_generation_change_is_refused(recovery):
    store, aggregate = recovery
    selected = purchase(aggregate)
    requested = paid("first", selected)
    assert store.reserve_issue_recovery(requested, selected).admitted
    current = read(store, aggregate)
    changed = replace(current, revision=WorkflowRevision(replace(current.revision.fence, generation=3), 2))
    with store.transaction() as db:
        persist_workflow_snapshot(db, changed)
    with pytest.raises(WorkflowAuthorityError, match="stale or held"):
        store.register_process_lease(requested.run_id, expected_owner_id=requested.owner_id,
                                     pid=123, pgid=123, process_start_ticks=456)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM leases").fetchone()[0] == 0


def test_caught_return_failure_rolls_back_money_and_component_in_outer_transaction(recovery, monkeypatch):
    import issue_agent.adapters.workflow_rows as rows

    store, aggregate = recovery
    selected = purchase(aggregate)
    requested = paid("first", selected)
    assert store.reserve_issue_recovery(requested, selected).admitted
    admitted = receipt(store, requested.run_id)
    current = read(store, aggregate)
    original = rows.persist_workflow_snapshot

    def failure(db, after):
        original(db, after)
        raise RuntimeError("caught return failure")

    with store.transaction() as db:
        @contextmanager
        def existing_transaction():
            yield db

        monkeypatch.setattr(store, "transaction", existing_transaction)
        monkeypatch.setattr(rows, "persist_workflow_snapshot", failure)
        with pytest.raises(RuntimeError, match="caught return failure"):
            store.settle_issue_recovery(money(requested.run_id), admitted, cc.IssueRecoveryState(infra_failures=1))
        assert load_issue_aggregate(db, aggregate.revision.fence.key) == current
        assert db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='continuation-return'").fetchone()[0] == 0
    monkeypatch.undo()
    assert read(store, aggregate) == current


def test_populated_schema35_upgrade_preserves_old_money_and_fences_old_binary(tmp_path, monkeypatch):
    import issue_agent.adapters.control_store as module
    from tests.adapters.test_control_store import _historical_fixture

    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    aggregate = resolve_snapshot()
    install_workflow(store, aggregate)
    first = request("old")
    assert store.reserve(first).admitted
    assert store.settle(money(first.run_id)).disposition is AccountingDisposition.AVAILABLE
    with store.transaction() as db:
        _historical_fixture(db, 35)
        history = tuple(db.execute("SELECT version,source_digest FROM schema_migrations ORDER BY version"))

    def interrupted(version, stage):
        if version == 36 and stage == "before_commit":
            raise RuntimeError("migration interrupted")

    with pytest.raises(RuntimeError, match="migration interrupted"):
        SQLiteControlStore(store.path, now=lambda: NOW, migration_observer=interrupted).initialize()
    with store._connect() as db:
        assert tuple(db.execute("SELECT version,source_digest FROM schema_migrations ORDER BY version")) == history
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    store.initialize()
    assert store.settle(money(first.run_id)).idempotent
    assert store.snapshot(first.scope).settled.micros == 1
    component = active(aggregate.revision.fence.key, WorkflowFamily.ISSUE_RECOVERY)
    current = read(store, aggregate)
    install_secondary(store, replace(current, issue_recovery=component), component)
    with store.transaction() as db:
        before = tuple(db.iterdump())
    monkeypatch.setattr(module, "SCHEMA_VERSION", 35)
    with pytest.raises(module.IncompatibleSchema, match="newer"):
        SQLiteControlStore(store.path, now=lambda: NOW).initialize()
    with store._connect() as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("family", [WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR, WorkflowFamily.STEWARD])
def test_active_component_aggregate_codec_roundtrip_requires_selected_fields(family):
    if family is WorkflowFamily.PR_REPAIR:
        base = replace(held(), gates=())
        aggregate = replace(base, pr_repair=active(base.revision.fence.key, family))
        selected_field = "pr_repair"
    else:
        base = resolve_snapshot()
        selected_field = "issue_recovery" if family is WorkflowFamily.ISSUE_RECOVERY else "steward"
        aggregate = replace(base, **{selected_field: active(base.revision.fence.key, family)})
    raw = encode_workflow(aggregate)
    assert decode_workflow(raw, type(aggregate)) == aggregate
    malformed = json.loads(raw)
    del malformed["value"]["fields"][selected_field]
    with pytest.raises(ValueError, match="wrong fields"):
        decode_workflow(json.dumps(malformed), type(aggregate))


def test_reply_ordering_refuses_old_and_equivalent_timestamp_observations(recovery):
    store, aggregate = recovery
    newer = NOW + timedelta(minutes=1)
    for name, stamp in (("first", NOW.isoformat()), ("second", newer.isoformat())):
        selected = purchase(read(store, aggregate), reply=stamp, ceiling=5)
        assert store.reserve_issue_recovery(paid(name, selected), selected).admitted
    current = read(store, aggregate)
    for stamp in (NOW.isoformat(), newer.isoformat(), newer.isoformat().replace("+00:00", "Z")):
        selected = purchase(current, reply=stamp, ceiling=5)
        assert not store.reserve_issue_recovery(paid("refused:" + stamp, selected), selected).admitted
        assert read(store, aggregate) == current


def test_caught_admission_failure_cannot_leave_partial_rows_in_committed_outer_transaction(recovery, monkeypatch):
    store, aggregate = recovery
    selected = purchase(aggregate)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER fail_budget BEFORE INSERT ON budget_reservations BEGIN SELECT RAISE(ABORT,'refusal'); END")
        @contextmanager
        def borrowed():
            yield db
        monkeypatch.setattr(store, "transaction", borrowed)
        assert not store.reserve_issue_recovery(paid("first", selected), selected).admitted
        assert load_issue_aggregate(db, aggregate.revision.fence.key) == aggregate
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dedupe_keys WHERE namespace='continuation-admission'").fetchone()[0] == 0
    monkeypatch.undo()
    assert read(store, aggregate) == aggregate


@pytest.mark.parametrize("entrypoint", ["register_lease", "register_process_lease"])
@pytest.mark.parametrize("fault", ["generation", "missing_receipt"])
def test_every_fresh_lease_entrypoint_requires_current_selected_admission(recovery, entrypoint, fault):
    from issue_agent.adapters.control_store import Lease
    store, aggregate = recovery
    selected = purchase(aggregate)
    requested = paid("first", selected)
    assert store.reserve_issue_recovery(requested, selected).admitted
    with store.transaction() as db:
        if fault == "missing_receipt":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='continuation-admission'")
        else:
            current = load_issue_aggregate(db, aggregate.revision.fence.key)
            persist_workflow_snapshot(db, replace(current, revision=WorkflowRevision(replace(current.revision.fence, generation=3), 2)))
    with pytest.raises(WorkflowAuthorityError):
        if entrypoint == "register_lease":
            store.register_lease(Lease(requested.run_id, requested.dispatch_id, requested.owner_id, 123, 123, NOW, 456))
        else:
            store.register_process_lease(requested.run_id, expected_owner_id=requested.owner_id, pid=123, pgid=123, process_start_ticks=456)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM leases").fetchone()[0] == 0


def test_actual_run_selection_and_binding_are_immutable_and_corruption_refuses_return(recovery):
    import sqlite3
    store, aggregate = recovery
    selected = purchase(aggregate)
    requested = paid("first", selected)
    assert store.reserve_issue_recovery(requested, selected).admitted
    admitted = receipt(store, requested.run_id)
    altered = replace(admitted.attempt.binding, fence=replace(admitted.attempt.binding.fence, generation=3))
    with store.transaction() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE runs SET workflow_binding_json=? WHERE run_id=?", (encode_workflow(altered), requested.run_id))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE runs SET continuations_json=? WHERE run_id=?", (encode_workflow(()), requested.run_id))
        db.execute("DROP TRIGGER runs_continuation_identity_update")
        db.execute("UPDATE runs SET workflow_binding_json=? WHERE run_id=?", (encode_workflow(altered), requested.run_id))
    with pytest.raises(WorkflowAuthorityError, match="actual run binding"):
        store.settle_issue_recovery(money(requested.run_id), admitted, cc.IssueRecoveryState())
    assert store.snapshot(requested.scope).settled.micros == 0


def test_public_primary_reader_keeps_steward_shell_inactive_in_active_resolve_repository(recovery):
    from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
    from issue_agent.domain.workflow_identity import WorkflowWriter
    store, _ = recovery
    revision = resolve_snapshot(7).revision
    component = active(revision.fence.key, WorkflowFamily.STEWARD)
    shell = IssueWorkflowShell(revision, steward=component)
    with store.transaction() as db:
        db.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)",
                   (*workflow_scope(revision.fence.key), revision.fence.generation, revision.version, encode_workflow_episodes(shell)))
    install_secondary(store, shell, component)
    control = SQLiteWorkflowControl(store, WorkflowWriter("o/r", "fix", "o/r:fix:test"), resolve_ceiling=3)
    assert isinstance(control.load_workflow(revision.fence.key), WorkflowMigrationRequired)
    assert isinstance(read(store, shell), IssueWorkflowShell)


def test_stale_pr_return_preserves_continuity_then_current_work_proceeds_and_old_retry_is_inert(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: NOW)
    store.initialize()
    base = replace(held(), gates=())
    install_workflow(store, base)
    component = active(base.revision.fence.key, WorkflowFamily.PR_REPAIR)
    original = replace(base, pr_repair=component)
    install_secondary(store, original, component)
    selected = cc.select_pr_repair(original)
    purchase = cc.PRRepairPurchase(cc.ContinuationCapture(selected.binding, selected.revision, "leg:repair"), ceiling=2)
    first = replace(request("repair"), workflow=selected.binding)
    assert store.reserve_pr_repair(first, purchase).admitted
    old = receipt(store, first.run_id, WorkflowFamily.PR_REPAIR)
    with store.transaction() as db:
        current = load_review_workflow_row(db, base.revision.fence.key)
        moved = replace(current, revision=WorkflowRevision(replace(current.revision.fence, generation=1), 2))
        persist_workflow_snapshot(db, moved)
    assert store.settle_pr_repair(money(first.run_id), old, cc.PRRepairState()).disposition is AccountingDisposition.AVAILABLE
    with store.transaction() as db:
        current = load_review_workflow_row(db, base.revision.fence.key)
    assert current == moved
    capture = cc.ContinuationCapture(cc.select_pr_repair(current).binding, current.revision, "leg:repair2")
    second = replace(request("repair2"), workflow=capture.binding)
    assert store.reserve_pr_repair(second, replace(purchase, capture=capture)).admitted
    newest = receipt(store, second.run_id, WorkflowFamily.PR_REPAIR)
    assert store.settle_pr_repair(money(second.run_id), newest, cc.PRRepairState(infra_failures=1)).disposition is AccountingDisposition.AVAILABLE
    with store.transaction() as db:
        completed = load_review_workflow_row(db, base.revision.fence.key)
    assert completed.pr_repair.state.infra_failures == 1
    assert store.settle_pr_repair(money(first.run_id), old, cc.PRRepairState()).idempotent
    with store.transaction() as db:
        assert load_review_workflow_row(db, base.revision.fence.key) == completed


def test_v36_preserves_all_accepted_predecessor_migration_bytes():
    import hashlib
    from issue_agent.adapters.control_store import _SCHEMA_MIGRATIONS
    encoded = json.dumps(tuple((m.version, m.source, m.transform_spec, m.digest)
                               for m in _SCHEMA_MIGRATIONS if m.version <= 35),
                         ensure_ascii=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "16230090303e2c6eba8290d6f6d9986e1099037009b5f328dfd6e9b3a8c3818a"


@pytest.mark.parametrize("field", ["leg_id", "admission_version"])
@pytest.mark.parametrize("entrypoint", ["launch", "reservation_retry"])
def test_selected_admission_source_conflict_refuses_without_new_work(recovery, field, entrypoint):
    store, aggregate = recovery
    selected = purchase(aggregate)
    requested = paid("first", selected)
    assert store.reserve_issue_recovery(requested, selected).admitted
    current = read(store, aggregate)
    with store.transaction() as db:
        raw = db.execute("SELECT payload_json FROM dedupe_keys WHERE namespace='continuation-admission'").fetchone()[0]
        original, admitted = decode_workflow(raw, tuple)
        forged = (replace(admitted, attempt=replace(admitted.attempt, leg_id="conflicting-leg"))
                  if field == "leg_id" else replace(admitted, admission_version=99))
        db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='continuation-admission'",
                   (encode_workflow((original, forged)),))
    if entrypoint == "launch":
        with pytest.raises(WorkflowAuthorityError):
            store.register_process_lease(requested.run_id, expected_owner_id=requested.owner_id,
                                         pid=123, pgid=123, process_start_ticks=456)
    else:
        assert not store.reserve_issue_recovery(requested, selected).admitted
    assert read(store, aggregate) == current
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM leases").fetchone()[0] == 0


def test_primary_hold_refuses_fresh_launch_but_preserves_exact_retry_and_actual_settlement(recovery):
    from issue_agent.domain.workflow import OpenGate, RemoteOutcomeUncertain, NoAuthorizedRemediation
    store, aggregate = recovery
    selected = purchase(aggregate)
    requested = paid("first", selected)
    assert store.reserve_issue_recovery(requested, selected).admitted
    admitted = receipt(store, requested.run_id)
    current = read(store, aggregate)
    assert isinstance(current, WorkflowSnapshot)
    gate = OpenGate("primary:uncertainty", current.revision.fence.generation,
                    RemoteOutcomeUncertain("command:pending", NOW, 0, "missing reply"),
                    NoAuthorizedRemediation("reconcile"))
    with store.transaction() as db:
        held = replace(current, revision=replace(current.revision, version=current.revision.version + 1), gates=(gate,))
        persist_workflow_snapshot(db, held)
    with pytest.raises(WorkflowAuthorityError, match="stale or held"):
        store.register_process_lease(requested.run_id, expected_owner_id=requested.owner_id,
                                     pid=123, pgid=123, process_start_ticks=456)
    assert store.reserve_issue_recovery(requested, selected).admitted
    assert read(store, aggregate) == held
    assert store.settle_issue_recovery(money(requested.run_id), admitted, cc.IssueRecoveryState(infra_failures=1)).disposition is AccountingDisposition.AVAILABLE
    completed = read(store, aggregate)
    assert completed.gates == held.gates and completed.issue_recovery.state.infra_failures == 1
    assert store.snapshot(requested.scope).settled.micros == 1


def test_empty_review_value_roundtrips_as_typed_root_but_old_issue_field_keeps_scalar_bytes():
    import json
    from issue_agent.domain import continuation_components as cc
    from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
    state = cc.ReviewRetryState()
    assert decode_workflow(encode_workflow(state), cc.ReviewRetryState) == state
    # Literal accepted pre-F5 issue field, including debt without an episode.
    old = '{"format":1,"value":{"type":"IssueRecoveryState","fields":{"consumed_reply_at":"","resumes":0,"remembered_branch":"","ci_failures":0,"review_failures":3,"rebase_failures":0,"infra_failures":0,"self_clears":0}}}'
    decoded = decode_workflow(old, cc.IssueRecoveryState)
    assert decoded.review_failures == cc.ReviewRetryState(count=3)
    assert encode_workflow(decoded) == json.dumps(json.loads(old), sort_keys=True, separators=(",", ":"))
