"""Isolated SQLite activation, legacy import reads and rehearsal copies; ADR 0015."""

from contextlib import closing
import gc
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

import pytest

from issue_agent.adapters import control_store as control_module
from issue_agent.adapters import resolve_activation as activation_module
from issue_agent.adapters.control_store import CorruptControlStore, IncompatibleSchema, SQLiteControlStore
from issue_agent.adapters.effects import command_for_action
from issue_agent.adapters.resolve_activation import (
    ActivationSourcesChanged, ResolveActivationRefused, SQLiteResolveActivation,
    copy_for_rehearsal, file_digests, read_legacy_resolve_roots, read_legacy_resolve_state,
)
from issue_agent.adapters.workflow_control import SQLiteWorkflowControl
from issue_agent.domain.actions import Relabel, Target
from issue_agent.domain.contracts import EffectReceipt, EffectStatus, RunId
from issue_agent.domain.labels import Label
from issue_agent.domain.resolve_activation import (
    MAX_LEGACY_FILE_BYTES, ActivationBlocked, ActiveWithDifferences, AlreadyActive, DeploymentInventory,
    LegacyAbsent, LegacyPresent,
    LegacyResolveRecord, LegacyUnreadable, ResolveActivationPlan, UnmappedLegacyEntry,
    plan_resolve_activation,
)
from issue_agent.domain.workflow import (
    ObservedIssue, OpenGate, ResolveCeiling, WorkflowAbsent, WorkflowMigrationRequired, WorkflowSnapshot,
)
from issue_agent.domain.world import IssueView
from issue_agent.domain.workflow_identity import IssueRef, WorkflowKey, WorkflowWriter

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
REPO = "o/r"
OWNER = "o/r:fix:deployment"
WRITER = WorkflowWriter(REPO, "fix", OWNER)


@pytest.fixture
def control(tmp_path):
    store = SQLiteControlStore(str(tmp_path / "control" / "db.sqlite"), now=lambda: NOW)
    store.initialize()
    return store


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


def evaluate(control, state, ceiling=3):
    return plan_resolve_activation(
        REPO, read_legacy_resolve_state(str(state)), SQLiteResolveActivation(control).inventory(REPO), ceiling=ceiling,
    )


def activate(control, state, ceiling=3):
    plan = evaluate(control, state, ceiling)
    assert isinstance(plan, ResolveActivationPlan), plan
    result = SQLiteResolveActivation(control).activate(plan, revalidate=lambda: evaluate(control, state, ceiling))
    return plan, result


def workflows(control):
    return SQLiteWorkflowControl(control, WRITER, resolve_ceiling=3)


def key(number):
    return WorkflowKey(REPO, IssueRef(number))


def dump(control):
    with closing(sqlite3.connect(control.path)) as db:
        return tuple(db.iterdump())


def settle(control):
    """Close fixture connections and checkpoint, so digests measure only the rehearsal."""
    gc.collect()
    with closing(sqlite3.connect(control.path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def authority_rows(control):
    with closing(sqlite3.connect(control.path)) as db:
        return tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("workflow_authority_epochs", "workflows", "workflow_gates", "workflow_activation_imports")
        )


# ── legacy reads ──────────────────────────────────────────────────────────────

def test_absent_state_directory_has_no_legacy_sources(tmp_path):
    assert read_legacy_resolve_state(str(tmp_path / "missing")) == ()


@pytest.mark.parametrize("name", ("issue-.resolve-retries", "issue-7\n.last-error"))
def test_malformed_legacy_names_block_activation(state, name):
    path = state / name
    path.write_text("1")
    entries = read_legacy_resolve_state(str(state))
    assert len(entries) == 1 and isinstance(entries[0], UnmappedLegacyEntry)
    decision = plan_resolve_activation(REPO, entries, DeploymentInventory(REPO), ceiling=3)
    assert isinstance(decision, ActivationBlocked)
    assert path.read_text() == "1"


def test_legacy_reader_classifies_every_resolve_key_without_writing(state, tmp_path):
    (state / "issue-7.resolve-retries").write_bytes(b"3")
    (state / "issue-7.last-error").write_text("tests failed")
    (state / "issue-8.last-error").write_bytes(b"\xff\xfe")
    (state / "issue-9.resolve-retries").write_bytes(b"1" * (MAX_LEGACY_FILE_BYTES + 1))
    (state / "issue-10.resolve-retries").write_bytes(b"1" + b"\n" * (MAX_LEGACY_FILE_BYTES - 1))
    (tmp_path / "elsewhere").write_text("5")
    (state / "issue-11.resolve-retries").symlink_to(tmp_path / "elsewhere")
    (state / "issue-12.resolve-retries").mkdir()
    for name in ("issue-07.resolve-retries", "issue-0.last-error", "issue--112.resolve-retries", "issue-x.last-error"):
        (state / name).write_text("1")
    for ignored in ("issue-7.session", "issue-7.resolve-retries.123.tmp", "pr-7.resolve-retries", "journal.jsonl"):
        (state / ignored).write_text("ignored")
    before = {p.name: (p.lstat().st_mtime_ns, p.lstat().st_size) for p in state.iterdir()}

    entries = read_legacy_resolve_state(str(state))

    assert {p.name: (p.lstat().st_mtime_ns, p.lstat().st_size) for p in state.iterdir()} == before
    records = {e.issue: e for e in entries if isinstance(e, LegacyResolveRecord)}
    assert set(records) == {7, 8, 9, 10, 11, 12}
    assert isinstance(records[7].retries, LegacyPresent) and records[7].retries.text == "3"
    assert records[7].last_error == LegacyPresent(records[7].last_error.digest, "tests failed")
    assert records[8] == LegacyResolveRecord(8, LegacyAbsent(), LegacyUnreadable("not valid UTF-8"))
    assert isinstance(records[9].retries, LegacyUnreadable) and "exceeds" in records[9].retries.reason
    assert isinstance(records[10].retries, LegacyPresent)
    assert records[11].retries == LegacyUnreadable("symbolic link")
    assert records[12].retries == LegacyUnreadable("not a regular file")
    unmapped = {e.name for e in entries if isinstance(e, UnmappedLegacyEntry)}
    assert unmapped == {"issue-07.resolve-retries", "issue-0.last-error", "issue--112.resolve-retries", "issue-x.last-error"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_permission_denied_is_unreadable_not_absent(state):
    path = state / "issue-7.resolve-retries"
    path.write_text("1")
    path.chmod(0)
    try:
        (entry,) = read_legacy_resolve_state(str(state))
        assert isinstance(entry, LegacyResolveRecord) and isinstance(entry.retries, LegacyUnreadable)
    finally:
        path.chmod(0o600)


def test_every_root_is_scanned_and_aliases_are_not_duplicates(tmp_path):
    fix, review, older = (tmp_path / name for name in ("fix", "review", "older"))
    for directory in (fix, review, older):
        directory.mkdir()
    (fix / "issue-7.resolve-retries").write_text("1")
    (older / "issue-9.resolve-retries").write_text("3")
    (review / "issue-7.last-error").write_text("same issue elsewhere")
    (tmp_path / "alias").symlink_to(fix)
    entries = read_legacy_resolve_roots((
        ("fix", str(fix)), ("fix-alias", str(tmp_path / "alias")), ("review", str(review)), ("extra", str(older)),
    ))
    found = sorted((e.root, e.issue) for e in entries if isinstance(e, LegacyResolveRecord))
    assert found == [("extra", 9), ("fix", 7), ("review", 7)]
    decision = plan_resolve_activation(REPO, entries, DeploymentInventory(REPO), ceiling=3)
    assert isinstance(decision, ActivationBlocked)
    assert any("issue #7: ambiguous duplicate" in reason for reason in decision.reasons)


def test_state_path_that_is_not_a_directory_blocks(tmp_path):
    path = tmp_path / "state"
    path.write_text("not a directory")
    (entry,) = read_legacy_resolve_state(str(path))
    assert isinstance(entry, UnmappedLegacyEntry)


# ── inventory ─────────────────────────────────────────────────────────────────

def seed_work(control):
    control.start_run(RunId("ours"), "dispatch-ours", "RESOLVE", OWNER, "cfg", state="running")
    control.start_run(RunId("foreign"), "dispatch-foreign", "RESOLVE", "other/repo:fix:x", "cfg", state="running")
    control.start_run(RunId("done"), "dispatch-done", "RESOLVE", OWNER, "cfg", state="running")
    control.complete_run(RunId("done"), state="completed", outcome="ok")
    with control.transaction() as db:
        db.execute("INSERT INTO runs(run_id, dispatch_id, kind, owner_id, state, config_version) "
                   "VALUES ('unattributed', 'd', 'RESOLVE', 'legacy-owner', 'reserved', 'cfg')")


def test_inventory_counts_unbound_and_unattributable_work_but_not_other_repositories(control):
    seed_work(control)
    inventory = SQLiteResolveActivation(control).inventory(REPO)
    assert inventory.active_runs == ("ours", "unattributed")
    assert not inventory.quiescent and inventory.activation is None


def test_inventory_attributes_legacy_repository_variants_and_unparseable_values(control):
    with control.transaction() as db:
        for command_id, repository in (("upper", "O/R"), ("padded", " o/r "), ("garbage", "not a repo"),
                                       ("foreign", "other/repo")):
            db.execute(
                "INSERT INTO lifecycle_commands(command_id, idempotency_key, logical_key, kind, target, repository, "
                "executor_identity, payload_json, payload_digest, state, created_at, updated_at) "
                "VALUES (?, ?, ?, 'relabel', 'issue:7', ?, ?, '{}', 'x', 'pending', ?, ?)",
                (command_id, command_id, command_id, repository, OWNER, NOW.isoformat(), NOW.isoformat()))
        for repository, target in (("O/R", "issue:7"), ("other/repo", "issue:8")):
            db.execute("INSERT INTO dispatch_claims VALUES (?, 'fix', ?, 'RESOLVE', ?, 1, 1, '[]', ?)",
                       (repository, target, OWNER, NOW.isoformat()))
    inventory = SQLiteResolveActivation(control).inventory(REPO)
    assert inventory.open_commands == ("garbage", "padded", "upper")
    assert inventory.dispatch_claims == ("fix:issue:7",)
    assert not inventory.quiescent


def test_inventory_counts_open_commands_claims_and_reservations(control):
    command = command_for_action(Relabel(Target.ISSUE, 7, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW),
                                 expected_revision="r1", repository=REPO, executor_identity=OWNER)
    control.enqueue_command(command)
    with control.transaction() as db:
        db.execute("INSERT INTO dispatch_claims VALUES ('o/r', 'fix', 'issue:7', 'RESOLVE', ?, 1, 1, '[]', ?)",
                   (OWNER, NOW.isoformat()))
    inventory = SQLiteResolveActivation(control).inventory(REPO)
    assert inventory.open_commands == (str(command.command_id),)
    assert inventory.dispatch_claims == ("fix:issue:7",)
    assert "non-terminal lifecycle command" in "; ".join(inventory.blockers())


# ── activation ────────────────────────────────────────────────────────────────

def test_activation_atomically_installs_epoch_workflows_gates_and_decisions(control, state):
    (state / "issue-7.resolve-retries").write_text("3")
    (state / "issue-7.last-error").write_text("  tests\nfailed ")
    (state / "issue-8.resolve-retries").write_text("1")
    (state / "issue-9.last-error").write_text("config missing")

    plan, result = activate(control, state)

    assert not result.idempotent and result.plan == plan
    assert authority_rows(control) == (1, 3, 1, 3)
    exhausted = workflows(control).load_workflow(key(7))
    assert isinstance(exhausted, WorkflowSnapshot)
    assert (exhausted.episode.failures, exhausted.episode.last_failure) == (3, "tests failed")
    (gate,) = exhausted.gates
    assert isinstance(gate, OpenGate) and isinstance(gate.cause, ResolveCeiling)
    assert workflows(control).held_projection(key(7)).held
    partial = workflows(control).load_workflow(key(8))
    assert isinstance(partial, WorkflowSnapshot) and partial.episode.failures == 1 and not partial.gates
    text_only = workflows(control).load_workflow(key(9))
    assert isinstance(text_only, WorkflowSnapshot)
    assert (text_only.episode.failures, text_only.episode.last_failure) == (0, "config missing")
    assert isinstance(workflows(control).load_workflow(key(10)), WorkflowAbsent)


def test_discovery_after_activation_returns_the_imported_episode_instead_of_reinitializing(control, state):
    (state / "issue-7.resolve-retries").write_text("3")
    activate(control, state)
    before = workflows(control).load_workflow(key(7))
    observed = ObservedIssue(key(7), IssueView(7, labels=frozenset({Label.AI_READY}), updated_at="r"), NOW)
    committed = workflows(control).ensure_observed_workflow(observed)
    assert committed.idempotent and committed.snapshot == before
    assert committed.snapshot.gates and committed.snapshot.episode.failures == 3
    fresh = workflows(control).ensure_observed_workflow(
        ObservedIssue(key(11), IssueView(11, updated_at="r"), NOW))
    assert not fresh.idempotent and fresh.snapshot.episode.failures == 0


def test_inactive_repository_reads_migration_required(control):
    assert workflows(control).load_workflow(key(7)) == WorkflowMigrationRequired(key(7))


def test_empty_sources_activate_without_import_rows(control, state):
    _, result = activate(control, state)
    assert not result.idempotent
    assert authority_rows(control) == (1, 0, 0, 0)
    assert isinstance(workflows(control).load_workflow(key(7)), WorkflowAbsent)


@pytest.mark.parametrize("work", ["run", "lease", "claim", "command"])
def test_non_quiescent_deployment_cannot_activate(control, state, work):
    (state / "issue-7.resolve-retries").write_text("1")
    if work in {"run", "lease"}:
        control.start_run(RunId("live"), "d", "RESOLVE", OWNER, "cfg", state="running")
        if work == "lease":
            control.complete_run(RunId("live"), state="completed", outcome="ok")
            with control.transaction() as db:
                db.execute("INSERT INTO leases VALUES ('live', 'd', ?, 10, 10, 0, ?)", (OWNER, NOW.isoformat()))
    elif work == "claim":
        with control.transaction() as db:
            db.execute("INSERT INTO dispatch_claims VALUES ('o/r', 'fix', 'issue:7', 'RESOLVE', ?, 1, 1, '[]', ?)",
                       (OWNER, NOW.isoformat()))
    else:
        control.enqueue_command(command_for_action(
            Relabel(Target.ISSUE, 7, Label.AI_READY, None), expected_revision="r", repository=REPO, executor_identity=OWNER))
    assert isinstance(evaluate(control, state), ActivationBlocked)
    assert authority_rows(control) == (0, 0, 0, 0)


def test_sources_changed_after_review_refuse_without_writing(control, state):
    (state / "issue-7.resolve-retries").write_text("1")
    plan = evaluate(control, state)
    assert isinstance(plan, ResolveActivationPlan)
    (state / "issue-7.resolve-retries").write_text("2")
    before = dump(control)
    with pytest.raises(ActivationSourcesChanged):
        SQLiteResolveActivation(control).activate(plan, revalidate=lambda: evaluate(control, state))
    assert dump(control) == before


def test_work_started_after_revalidation_is_caught_inside_the_transaction(control, state):
    (state / "issue-7.resolve-retries").write_text("1")
    plan = evaluate(control, state)
    assert isinstance(plan, ResolveActivationPlan)

    def revalidate_then_race():
        fresh = evaluate(control, state)
        control.start_run(RunId("racing"), "d", "RESOLVE", OWNER, "cfg", state="reserved")
        return fresh

    with pytest.raises(ActivationSourcesChanged, match="active run"):
        SQLiteResolveActivation(control).activate(plan, revalidate=revalidate_then_race)
    assert authority_rows(control) == (0, 0, 0, 0)


def test_activation_is_recognised_by_its_evidence_despite_later_live_work(control, state):
    (state / "issue-7.resolve-retries").write_text("3")
    plan, _ = activate(control, state)
    control.start_run(RunId("after"), "d", "RESOLVE", OWNER, "cfg", state="running")
    assert evaluate(control, state) == AlreadyActive(plan)
    again = SQLiteResolveActivation(control).activate(plan, revalidate=lambda: evaluate(control, state))
    assert again.idempotent


def test_post_activation_differences_are_diagnosed_precisely(control, state):
    (state / "issue-7.resolve-retries").write_text("3")
    plan, _ = activate(control, state)
    after = dump(control)
    changed_ceiling = evaluate(control, state, ceiling=5)
    assert isinstance(changed_ceiling, ActiveWithDifferences)
    assert "configured resolve ceiling 5 differs from the ceiling 3" in changed_ceiling.reasons[0]
    (state / "issue-7.resolve-retries").write_text("0")
    changed_sources = evaluate(control, state)
    assert isinstance(changed_sources, ActiveWithDifferences)
    assert "legacy sources changed since activation" in changed_sources.reasons[0]
    with pytest.raises(ActivationSourcesChanged):
        SQLiteResolveActivation(control).activate(plan, revalidate=lambda: evaluate(control, state))
    assert dump(control) == after


def test_activation_records_ceiling_and_plan_digest_even_without_imports(control, state):
    plan, _ = activate(control, state, ceiling=4)
    with closing(sqlite3.connect(control.path)) as db:
        assert db.execute("SELECT ceiling, plan_digest FROM workflow_activation_evidence").fetchall() == [
            (4, plan.plan_digest)]
    assert evaluate(control, state, ceiling=4) == AlreadyActive(plan)
    changed = evaluate(control, state, ceiling=9)
    assert isinstance(changed, ActiveWithDifferences) and changed.recorded == plan


def test_in_transaction_guard_refuses_a_different_plan_when_revalidation_agrees(control, state):
    (state / "issue-7.resolve-retries").write_text("3")
    plan, _ = activate(control, state)
    after = dump(control)
    different = replace(plan, source_digest="0" * 64)
    with pytest.raises(ResolveActivationRefused, match="already active with a different plan") as refused:
        SQLiteResolveActivation(control).activate(different, revalidate=lambda: different)
    assert not isinstance(refused.value, ActivationSourcesChanged)
    assert dump(control) == after


def test_reactivation_is_idempotent_and_a_different_plan_is_refused(control, state):
    (state / "issue-7.resolve-retries").write_text("3")
    plan, _ = activate(control, state)
    after = dump(control)
    assert evaluate(control, state) == AlreadyActive(plan)
    again = SQLiteResolveActivation(control).activate(plan, revalidate=lambda: evaluate(control, state))
    assert again.idempotent and dump(control) == after
    for different in (replace(plan, source_digest="0" * 64), replace(plan, inventory_digest="1" * 64)):
        with pytest.raises(ResolveActivationRefused):
            SQLiteResolveActivation(control).activate(different, revalidate=lambda: evaluate(control, state))
    assert isinstance(evaluate(control, state, ceiling=4), ActiveWithDifferences)
    assert dump(control) == after


@pytest.mark.parametrize("failure_point", ["gate", "second_import"])
def test_crash_after_epoch_insert_rolls_back_everything(control, state, monkeypatch, failure_point):
    (state / "issue-7.resolve-retries").write_text("3")
    (state / "issue-8.resolve-retries").write_text("1")
    plan = evaluate(control, state)
    assert isinstance(plan, ResolveActivationPlan)

    def crash(*args, **kwargs):
        raise RuntimeError("injected crash")

    if failure_point == "gate":
        monkeypatch.setattr(activation_module, "persist_workflow_snapshot", crash)
    else:
        real = activation_module.imported_workflow
        calls = []

        def crash_on_second(plan, target):
            calls.append(target.issue)
            if len(calls) == 2:
                raise RuntimeError("injected crash")
            return real(plan, target)

        monkeypatch.setattr(activation_module, "imported_workflow", crash_on_second)
    with pytest.raises(RuntimeError, match="injected"):
        SQLiteResolveActivation(control).activate(plan, revalidate=lambda: evaluate(control, state))
    assert authority_rows(control) == (0, 0, 0, 0)
    monkeypatch.undo()
    _, result = activate(control, state)
    assert not result.idempotent and authority_rows(control) == (1, 2, 1, 2)


def test_activation_never_creates_or_migrates_authority(tmp_path, state):
    missing = SQLiteControlStore(str(tmp_path / "absent" / "db.sqlite"))
    with pytest.raises(CorruptControlStore):
        SQLiteResolveActivation(missing).inventory(REPO)
    assert not Path(missing.path).exists()


def test_corrupt_retained_decision_is_unavailable_not_absent(control, state):
    (state / "issue-7.resolve-retries").write_text("1")
    activate(control, state)
    with control.transaction() as db:
        db.execute("UPDATE workflow_activation_imports SET decision_json='{}'")
    with pytest.raises(ValueError, match="activation evidence"):
        SQLiteResolveActivation(control).inventory(REPO)


# ── rollback posture ──────────────────────────────────────────────────────────

def test_rollback_restart_keeps_sql_authority_uncertain_commands_and_ignores_stale_files(control, state):
    (state / "issue-7.resolve-retries").write_text("3")
    (state / "issue-7.last-error").write_text("original failure")
    activate(control, state)
    imported = workflows(control).load_workflow(key(7))
    assert isinstance(imported, WorkflowSnapshot)
    command = command_for_action(
        Relabel(Target.ISSUE, 7, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW, workflow=imported.binding),
        expected_revision="r1", repository=REPO, executor_identity=OWNER)
    control.enqueue_command(command)
    control.begin_command(command.command_id)
    receipt = EffectReceipt(command.command_id, OWNER, NOW, NOW, EffectStatus.UNKNOWN_OUTCOME, error="502")
    assert control.record_effect(receipt)
    # A stale backup of the retired file must never be replayed.
    (state / "issue-7.resolve-retries").write_text("0")
    (state / "issue-7.last-error").unlink()

    restarted = SQLiteControlStore(control.path, now=lambda: NOW)
    restarted.initialize()
    restored = SQLiteWorkflowControl(restarted, WRITER, resolve_ceiling=3).load_workflow(key(7))
    assert isinstance(restored, WorkflowSnapshot)
    assert (restored.episode.failures, restored.episode.last_failure, bool(restored.gates)) == (3, "original failure", True)
    pending = {str(p.command.command_id): p.state for p in restarted.pending_commands()}
    assert pending[str(command.command_id)] == "unknown_outcome"
    assert restarted.effect_receipt(command.command_id) == receipt
    stale = evaluate(restarted, state)
    assert isinstance(stale, ActiveWithDifferences) and "never re-imported" in stale.reasons[0]


def test_older_schema_binary_fails_closed_on_activated_authority(control, state, monkeypatch):
    activate(control, state)
    monkeypatch.setattr(control_module, "SCHEMA_VERSION", 21)
    monkeypatch.setattr(control_module, "_KNOWN_SCHEMA_DIGESTS", {
        version: digest for version, digest in control_module._KNOWN_SCHEMA_DIGESTS.items() if version <= 21
    })
    older = SQLiteControlStore(control.path)
    with pytest.raises(IncompatibleSchema, match="newer"):
        older.require_initialized()
    with pytest.raises((IncompatibleSchema, CorruptControlStore)):
        older.initialize()


# ── rehearsal copies ──────────────────────────────────────────────────────────

def test_rehearsal_copy_is_isolated_and_never_writes_the_originals(control, state, tmp_path):
    (state / "issue-7.resolve-retries").write_text("3")
    (state / "journal.jsonl").write_text("not copied")
    (state / "issue-8.last-error").write_text("boom")
    settle(control)
    originals = (control.path, control.authority_witness_path,
                 str(state / "issue-7.resolve-retries"), str(state / "issue-8.last-error"))
    before = file_digests(originals)

    copy = copy_for_rehearsal(control.path, (("fix", str(state)),), str(tmp_path / "rehearsal"))

    assert file_digests(originals) == before
    # A read-only WAL reader may leave SQLite's empty sidecar, never content.
    wal = Path(control.path + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    assert sorted(os.listdir(copy.roots[0][1])) == ["issue-7.resolve-retries", "issue-8.last-error"]
    assert read_legacy_resolve_state(copy.roots[0][1]) == read_legacy_resolve_state(str(state))
    copied = SQLiteControlStore(copy.control_path, now=lambda: NOW)
    copied.require_initialized()
    SQLiteResolveActivation(copied).activate(
        evaluate(copied, copy.roots[0][1]), revalidate=lambda: evaluate(copied, copy.roots[0][1]))
    assert authority_rows(copied) == (1, 2, 1, 2)
    assert file_digests(originals) == before
    assert authority_rows(control) == (0, 0, 0, 0)


def test_rehearsal_refuses_a_non_empty_or_original_destination(control, state, tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("x")
    inside_state = state / "issue-5.resolve-retries"
    protected = tmp_path / "feed"
    protected.mkdir()
    for destination in (str(occupied), str(state), os.path.dirname(control.path), str(inside_state),
                        os.path.join(os.path.dirname(control.path), "copy"), str(tmp_path), str(protected / "x")):
        with pytest.raises(ResolveActivationRefused):
            copy_for_rehearsal(control.path, (("fix", str(state)),), destination, protected=(str(protected),))
    assert (occupied / "keep").read_text() == "x"
    assert not inside_state.exists() and not (protected / "x").exists()


def test_rehearsal_reproduces_unreadable_legacy_shapes(control, state, tmp_path):
    (tmp_path / "target").write_text("1")
    (state / "issue-8.resolve-retries").symlink_to(tmp_path / "target")
    (state / "issue-9.resolve-retries").mkdir()
    copy = copy_for_rehearsal(control.path, (("fix", str(state)),), str(tmp_path / "rehearsal"))
    assert read_legacy_resolve_state(copy.roots[0][1]) == read_legacy_resolve_state(str(state))
    assert isinstance(evaluate(SQLiteControlStore(copy.control_path), copy.roots[0][1]), ActivationBlocked)
