from __future__ import annotations

import dataclasses
import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.control_path import control_store_path
from issue_agent.adapters.control_store import (
    AccountingDisposition,
    BudgetLimit,
    ControlStoreError,
    CorruptControlStore,
    IncompatibleSchema,
    LEGACY_V8_SCHEMA_DIGEST,
    LegacySpendMigrationError,
    Lease,
    ReservationRequest,
    RetiredOwnerError,
    SQLiteControlStore,
    SettlementRequest,
    read_settled_spend,
)
from issue_agent.adapters.deployment_authority import DeploymentAuthority
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.domain.contracts import (
    BudgetScope,
    CommandId,
    CommandKind,
    EffectReceipt,
    EffectStatus,
    LifecycleCommand,
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
    RunId,
    TokenUsage,
)
from issue_agent.domain.events import (
    BudgetVarianceDetected,
    Correlation,
    ControlTransitionCommitted,
    EventEnvelope,
    Producer,
    SpendRecorded,
    TelemetryWarning,
    WorldFeedRetired,
    WorldObserved,
    new_event_id,
)
from issue_agent.domain.projection import fold


NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
EXPECTED_SCHEMA_VERSIONS = tuple(range(9, 39))


def _store(tmp_path: Path) -> SQLiteControlStore:
    store = SQLiteControlStore(str(tmp_path / "state" / "control.db"), now=lambda: NOW)
    store.initialize()
    return store


def _scope() -> BudgetScope:
    return BudgetScope(
        "o/r",
        "daily",
        "USD",
        "2026-07-30",
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )


def _request(index: int, maximum: int = 60, cap: int = 100) -> ReservationRequest:
    return ReservationRequest(
        ReservationId(f"reservation-{index}"),
        RunId(f"run-{index}"),
        f"dispatch-{index}",
        _scope(),
        PriceQuote("gpt", "card", "v1", NOW, Money("USD", maximum)),
        Money("USD", cap),
        "config-v1",
        "o/r",
    )


def _reserve_in_process(path: str, index: int, start, results) -> None:
    store = SQLiteControlStore(path)
    store.initialize()
    start.wait(timeout=5)
    results.put(store.reserve(_request(index)).disposition.value)


def _initialize_in_process(path: str, start, results) -> None:
    start.wait(timeout=5)
    try:
        SQLiteControlStore(path).initialize()
    except BaseException as exc:
        results.put(f"{type(exc).__name__}: {exc}")
    else:
        results.put("ok")


def _historical_fixture(db: sqlite3.Connection, version: int) -> None:
    # This constructs test fixtures, not a production downgrade: migrations
    # which rewrite rows need their historical inputs seeded by the caller.
    from issue_agent.adapters.control_store import _SCHEMA_MIGRATIONS, _sql_statements

    removals = {
        38: ("DROP INDEX lifecycle_self_clear_pending",),
        37: ("DROP INDEX runs_fix_issue_dispatch",),
        36: (
            "DROP TRIGGER runs_continuation_selections_update",
            "DROP TRIGGER runs_continuation_identity_update",
            "ALTER TABLE runs DROP COLUMN continuations_json",
            next(statement for migration in _SCHEMA_MIGRATIONS if migration.version == 25
                 for statement in _sql_statements(migration.source) if statement.startswith("CREATE TABLE workflow_components "))
            .replace("CREATE TABLE workflow_components (", "CREATE TABLE workflow_components_before36 ("),
            "INSERT INTO workflow_components_before36 SELECT * FROM workflow_components",
            "DROP TABLE workflow_components",
            "ALTER TABLE workflow_components_before36 RENAME TO workflow_components",
            "CREATE INDEX workflow_components_family ON workflow_components(repository,family)",
        ),
        35: ("DROP INDEX workflow_gates_current",),
        # Recreate retired projections only to construct the old fixture schema.
        34: tuple(statement for migration in _SCHEMA_MIGRATIONS if 27 <= migration.version <= 32
                  for statement in _sql_statements(migration.source)),
        33: ("DROP INDEX review_execution_rounds_generation_pending",),
        32: ("DROP TABLE review_archive_identity_members",
             "DROP TABLE review_archive_identity_certifications",
             "DELETE FROM review_archive_source_versions WHERE source='identity_members' AND scope=X''") + tuple(
            f"DROP TRIGGER review_lineage_{table}_{event}"
            for table in (
                "dedupe_keys", "dispatch_claims", "runs", "leases", "budget_reservations",
                "reservation_recovery", "charges", "workflow_evidence", "reservation_limits",
                "quota_debits", "run_cancellation_intents",
            ) for event in ("insert", "update", "delete")
        ),
        31: (
            "DROP TRIGGER review_archive_source_version_overflow",
            "DROP TRIGGER review_lifecycle_command_source_insert",
            "DROP TRIGGER review_lifecycle_command_source_update",
            "DROP TRIGGER review_lifecycle_command_source_delete",
            "DROP TRIGGER review_lifecycle_effect_source_insert",
            "DROP TRIGGER review_lifecycle_effect_source_update",
            "DROP TRIGGER review_lifecycle_effect_source_delete",
            "DELETE FROM review_archive_source_versions WHERE source='lifecycle_rows' AND scope=X''",
        ),
        30: (
            "DROP TRIGGER review_checkpoint_source_no_replace_insert",
            "DROP TRIGGER review_checkpoint_source_minus_one_insert",
            "DROP TRIGGER review_checkpoint_source_no_replace_update",
            "DROP TRIGGER review_checkpoint_source_insert",
            "DROP TRIGGER review_checkpoint_source_update",
            "DROP TRIGGER review_checkpoint_source_delete",
            "DROP TRIGGER review_workflow_source_insert",
            "DROP TRIGGER review_workflow_source_update",
            "DROP TRIGGER review_workflow_source_delete",
            "DROP TRIGGER review_component_source_insert",
            "DROP TRIGGER review_component_source_update",
            "DROP TRIGGER review_component_source_delete",
            "DELETE FROM review_archive_source_versions WHERE source='checkpoint_sources' AND scope=X''",
            "DELETE FROM review_archive_source_versions WHERE source='workflow_rows' AND scope=X''",
        ),
        29: (
            "DROP TRIGGER review_archive_dedupe_no_replace_insert",
            "DROP TRIGGER review_archive_dedupe_minus_one_insert",
            "DROP TRIGGER review_archive_dedupe_no_replace_update",
            "DROP TRIGGER review_generation_source_insert",
            "DROP TRIGGER review_generation_source_update",
            "DROP TRIGGER review_generation_source_delete",
            "DELETE FROM review_archive_source_versions WHERE source='generation_sources' AND scope=X''",
        ),
        28: (
            "DROP TRIGGER review_all_round_source_insert",
            "DROP TRIGGER review_all_round_source_update",
            "DROP TRIGGER review_all_round_source_delete",
            "DELETE FROM review_archive_source_versions WHERE source='all_round_rows' AND scope=X''",
        ),
        27: (
            "DROP TRIGGER review_workflow_round_source_insert",
            "DROP TRIGGER review_round_source_no_replace_insert",
            "DROP TRIGGER review_round_source_no_replace_update",
            "DROP TRIGGER review_round_source_insert",
            "DROP TRIGGER review_round_source_update",
            "DROP TRIGGER review_round_source_delete",
            "DROP TRIGGER review_receipt_source_insert",
            "DROP TRIGGER review_receipt_source_update",
            "DROP TRIGGER review_receipt_source_delete",
            "DROP TABLE review_archive_source_versions",
        ),
        26: ("DROP INDEX review_execution_rounds_repository",),
        25: (
            "DROP TRIGGER runs_review_attachment_insert", "DROP TRIGGER runs_review_attachment_update",
            "DROP INDEX runs_review_child",
            "ALTER TABLE runs DROP COLUMN review_child_id", "ALTER TABLE runs DROP COLUMN review_round_id",
            "DROP TABLE review_execution_rounds", "DROP TABLE workflow_components",
            "DROP TRIGGER dispatch_claims_occurrence_insert", "DROP TRIGGER dispatch_claims_occurrence_update",
            "DROP INDEX dispatch_claims_occurrence", "ALTER TABLE dispatch_claims DROP COLUMN occurrence_id",
        ),
        24: (
            "DROP INDEX effect_receipts_remote_object",
            "ALTER TABLE lifecycle_commands DROP COLUMN prewrite_read_json",
        ),
        23: (
            "DROP TRIGGER workflow_retry_grant_insert", "DROP TRIGGER workflow_retry_grant_update",
            "DROP INDEX workflow_retry_open_grant",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposition",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposed_at",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN disposition_reason",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN accepted_at",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN first_visit_at",
            "ALTER TABLE workflow_retry_authorizations DROP COLUMN unavailable_reads",
        ),
        22: ("DROP TABLE workflow_activation_evidence", "DROP TABLE workflow_activation_imports"),
        21: ("DROP INDEX runs_workflow_scope", "DROP INDEX lifecycle_publication_runs"),
        20: (
            "DROP TRIGGER runs_require_workflow_episode_insert",
            "DROP TRIGGER runs_require_workflow_episode_update",
            "DROP INDEX runs_adjudication_episode",
            "ALTER TABLE runs DROP COLUMN workflow_episode_key",
            "UPDATE runs SET outcome='released' WHERE outcome='unlaunched_released'",
        ),
        19: (
            "DROP TABLE workflow_retry_authorizations",
            "DROP TABLE workflow_evidence",
            "DROP TABLE workflow_rearm_commands",
            "DROP TABLE workflow_rearms",
            "DROP TABLE workflow_gates",
            "DROP TABLE workflows",
            "DROP TABLE workflow_authority_epochs",
            "ALTER TABLE runs DROP COLUMN workflow_binding_json",
            "ALTER TABLE lifecycle_commands DROP COLUMN workflow_binding_json",
            "ALTER TABLE lifecycle_commands DROP COLUMN workflow_reconciliation_json",
            "ALTER TABLE lifecycle_commands DROP COLUMN attempt_owner",
        ),
        18: ("ALTER TABLE lifecycle_commands DROP COLUMN refusals",),
        17: ("ALTER TABLE budget_reservations DROP COLUMN workload_repository",),
        16: ("DROP TABLE reservation_recovery",),
        15: ("DROP TABLE retired_owners",),
        14: ("DROP TABLE dispatch_claim_runs",),
        13: (),
        12: (
            "CREATE UNIQUE INDEX runs_active_dispatch_unique ON runs(dispatch_id) "
            "WHERE state IN ('reserved', 'starting', 'running')",
        ),
        11: ("DROP INDEX runs_active_dispatch_unique", "DROP TABLE dispatch_claims"),
        10: ("DROP TABLE run_cancellation_intents",),
    }
    assert version == 8 or version in EXPECTED_SCHEMA_VERSIONS
    history = tuple(row[0] for row in db.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ))
    assert history == EXPECTED_SCHEMA_VERSIONS
    assert set(removals) == set(EXPECTED_SCHEMA_VERSIONS[1:])
    for applied in reversed(history):
        if applied <= max(version, 9):
            break
        for statement in removals[applied]:
            db.execute(statement)
        db.execute("DELETE FROM schema_migrations WHERE version=?", (applied,))
    if version == 8:
        db.execute("DROP TABLE level_state")
        db.execute("DELETE FROM schema_migrations")
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at, source_digest) "
            "VALUES (8, ?, ?)",
            (NOW.isoformat(), LEGACY_V8_SCHEMA_DIGEST),
        )


def test_control_store_initializes_with_restrictive_permissions_and_integrity(tmp_path):
    store = _store(tmp_path)
    assert Path(store.path).stat().st_mode & 0o777 == 0o600
    assert Path(store.authority_witness_path).read_text().strip() == (
        "issue-agent-control-authority-v1"
    )
    store.integrity_check()
    with sqlite3.connect(store.path) as db:
        history = db.execute(
            "SELECT version, length(source_digest) FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
    assert history == [(version, 64) for version in EXPECTED_SCHEMA_VERSIONS]


def test_established_control_store_disappearance_is_not_fresh_initialized(
    tmp_path,
):
    store = _store(tmp_path)
    assert store.reserve(_request(1)).admitted
    Path(store.path).unlink()

    with pytest.raises(CorruptControlStore, match="disappeared"):
        SQLiteControlStore(store.path).initialize()

    assert not Path(store.path).exists()


def test_established_control_store_read_does_not_recreate_deleted_authority(
    tmp_path,
):
    store = _store(tmp_path)
    assert store.reserve(_request(1)).admitted
    Path(store.path).unlink()

    with pytest.raises(CorruptControlStore, match="disappeared"):
        store.snapshot(_scope())

    assert not Path(store.path).exists()


def test_repository_alias_migration_unifies_authoritative_rows(tmp_path):
    store = _store(tmp_path)
    assert store.reserve(_request(1)).admitted
    command = LifecycleCommand(
        CommandId("alias-command"),
        "github:alias-command",
        CommandKind.POST_COMMENT,
        "issue:7",
        None,
        {"target": "issue", "number": 7, "body": "done"},
        "cfg",
        "o/r",
        "o/r:fix",
    )
    store.enqueue_command(command)
    with store._connect() as db:
        _historical_fixture(db, 12)
        db.execute(
            "UPDATE lifecycle_commands SET repository='O/R'"
        )
        db.execute(
            "UPDATE budget_reservations SET repository='O/R'"
        )
        db.execute(
            "UPDATE reservation_limits SET repository='O/R' "
            "WHERE repository='o/r'"
        )

    reopened = SQLiteControlStore(store.path, now=lambda: NOW)
    reopened.initialize()

    with reopened._connect() as db:
        assert db.execute(
            "SELECT DISTINCT repository FROM lifecycle_commands"
        ).fetchall()[0][0] == "o/r"
        assert db.execute(
            "SELECT DISTINCT repository FROM budget_reservations"
        ).fetchall()[0][0] == "o/r"
        repositories = {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT repository FROM reservation_limits"
            ).fetchall()
        }
    assert repositories == {"o/r"}


def test_repository_alias_claim_collision_fails_as_permanent_schema_error(
    tmp_path,
):
    store = _store(tmp_path)
    with store._connect() as db:
        _historical_fixture(db, 12)
        claim = (
            "fix",
            "issue:7",
            "RESOLVE",
            "owner",
            123,
            10,
            "[]",
            NOW.isoformat(),
        )
        db.execute(
            "INSERT INTO dispatch_claims("
            "repository, role, target, leg, owner_id, owner_pid, "
            "owner_start_ticks, paths_json, claimed_at"
            ") VALUES ('Acme/App', ?, ?, ?, ?, ?, ?, ?, ?)",
            claim,
        )
        db.execute(
            "INSERT INTO dispatch_claims("
            "repository, role, target, leg, owner_id, owner_pid, "
            "owner_start_ticks, paths_json, claimed_at"
            ") VALUES ('acme/app', ?, ?, ?, ?, ?, ?, ?, ?)",
            claim,
        )

    with pytest.raises(
        IncompatibleSchema,
        match="conflicting authority rows; operator repair is required",
    ):
        SQLiteControlStore(store.path).initialize()

    with store._connect() as db:
        assert db.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 12


def test_valid_pre_witness_database_is_adopted_once(tmp_path):
    store = _store(tmp_path)
    Path(store.authority_witness_path).unlink()

    SQLiteControlStore(store.path).initialize()

    assert Path(store.authority_witness_path).read_text().strip() == (
        "issue-agent-control-authority-v1"
    )


def test_empty_interrupted_first_initialization_can_resume(tmp_path):
    path = tmp_path / "state" / "control.db"
    path.parent.mkdir()
    path.touch()

    SQLiteControlStore(str(path)).initialize()

    assert path.stat().st_size > 0


def test_historyless_nonempty_database_is_rejected_without_adoption(tmp_path):
    path = tmp_path / "state" / "control.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated_authority(value TEXT)")

    with pytest.raises(IncompatibleSchema, match="historyless nonempty"):
        SQLiteControlStore(str(path)).initialize()

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='unrelated_authority'"
        ).fetchone() == ("unrelated_authority",)
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone() is None


def test_shipped_v8_database_upgrades_without_rewriting_history_or_data(
    tmp_path,
):
    store = _store(tmp_path)
    request = _request(1)
    assert store.reserve(request).admitted
    with sqlite3.connect(store.path) as db:
        _historical_fixture(db, 8)
        db.commit()

    SQLiteControlStore(store.path).initialize()

    with sqlite3.connect(store.path) as db:
        history = db.execute(
            "SELECT version, source_digest FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        reserved = db.execute(
            "SELECT state FROM budget_reservations WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
        workflow_binding = db.execute(
            "SELECT workflow_binding_json FROM runs WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
        workflow_count = db.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        level_state = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='level_state'"
        ).fetchone()
    assert tuple(version for version, _digest in history) == (8, *EXPECTED_SCHEMA_VERSIONS)
    assert history[0][1] == (
        "1740c6b03aae7ba5beccea7dd522d0859396e901dee6a40d12ff4284621745b5"
    )
    assert reserved == ("active",)
    assert workflow_binding == (None,)
    assert workflow_count == 0
    assert level_state == ("level_state",)


@pytest.mark.parametrize(
    ("launched", "expected_policy"),
    ((False, "spawn_pending"), (True, "reconcile_only")),
)
def test_v17_recovers_pre_v16_launch_proof(
    tmp_path,
    launched,
    expected_policy,
) -> None:
    store = _store(tmp_path)
    request = dataclasses.replace(
        _request(1),
        dispatch_id=json.dumps(
            {} if launched else {"repository": "o/r"}
        ),
        scope=dataclasses.replace(
            _scope(),
            repository="*",
            dimension="global",
        ),
        workload_repository="o/r",
        additional_limits=(
            (BudgetLimit(_scope(), Money("USD", 100)),)
            if launched
            else ()
        ),
    )
    lease = (
        Lease(
            request.run_id,
            request.dispatch_id,
            request.owner_id,
            123,
            123,
            NOW,
            99,
        )
        if launched
        else None
    )
    assert store.reserve(request, lease=lease).admitted
    with sqlite3.connect(store.path) as db:
        _historical_fixture(db, 15)

    reopened = SQLiteControlStore(store.path, now=lambda: NOW)
    reopened.initialize()

    with sqlite3.connect(store.path) as db:
        policy, workload_repository = db.execute(
            "SELECT rr.policy, r.workload_repository "
            "FROM reservation_recovery rr "
            "JOIN budget_reservations r ON r.run_id=rr.run_id "
            "WHERE rr.run_id=?",
            (str(request.run_id),),
        ).fetchone()
    assert policy == expected_policy
    assert workload_repository == "o/r"


@pytest.mark.parametrize("starting_version", ("fresh", "v8", "v9", "v24"))
def test_concurrent_role_boot_serializes_schema_migrations(
    tmp_path,
    starting_version,
):
    path = tmp_path / f"concurrent-{starting_version}.db"
    if starting_version != "fresh":
        store = SQLiteControlStore(str(path))
        store.initialize()
        if starting_version == "v24":
            _seed_v24_workflow_history(store)
        else:
            with sqlite3.connect(path) as db:
                _historical_fixture(db, int(starting_version[1:]))
                db.commit()
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = tuple(
        context.Process(
            target=_initialize_in_process,
            args=(str(path), start, results),
        )
        for _index in range(3)
    )
    for process in processes:
        process.start()
    start.set()
    observed = tuple(results.get(timeout=10) for _process in processes)
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert observed == ("ok", "ok", "ok")
    with sqlite3.connect(path) as db:
        assert tuple(
            row[0]
            for row in db.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ) == (
            (8, *EXPECTED_SCHEMA_VERSIONS)
            if starting_version == "v8"
            else EXPECTED_SCHEMA_VERSIONS
        )
        if starting_version == "v24":
            assert db.execute("SELECT COUNT(*) FROM workflow_components").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM review_execution_rounds").fetchone()[0] == 0


def test_dispatch_claims_serialize_targets_and_overlapping_paths(
    tmp_path,
) -> None:
    store = _store(tmp_path)

    assert store.claim_dispatch(
        "o/r",
        "fix",
        7,
        "RESOLVE",
        "deployment-a",
        frozenset({"src/shared.py"}),
    )
    assert not store.claim_dispatch(
        "o/r",
        "fix",
        7,
        "RESUME",
        "deployment-b",
        frozenset({"src/other.py"}),
    )
    assert not store.claim_dispatch(
        "o/r",
        "fix",
        8,
        "RESOLVE",
        "deployment-b",
        frozenset({"src/shared.py"}),
    )
    assert store.claim_dispatch(
        "o/r",
        "fix",
        8,
        "RESOLVE",
        "deployment-b",
        frozenset({"src/independent.py"}),
    )

    assert not store.release_dispatch_claim(
        "o/r", "fix", 7, "deployment-b"
    )
    assert store.release_dispatch_claim(
        "o/r", "fix", 7, "deployment-a"
    )
    assert store.claim_dispatch(
        "o/r",
        "fix",
        7,
        "RESUME",
        "deployment-b",
        frozenset({"src/other.py"}),
    )


def test_dispatch_claim_process_probe_runs_outside_write_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    assert store.claim_dispatch(
        "o/r", "fix", 1, "RESOLVE", "owner-a", frozenset({"a.py"})
    )
    transaction_open = False
    original_transaction = store.transaction
    original_matches = ProcessSupervisor.matches_identity

    @contextmanager
    def observed_transaction():
        nonlocal transaction_open
        with original_transaction() as db:
            transaction_open = True
            try:
                yield db
            finally:
                transaction_open = False

    def observed_matches(pid: int, start_ticks: int) -> bool:
        assert not transaction_open
        return original_matches(pid, start_ticks)

    monkeypatch.setattr(store, "transaction", observed_transaction)
    monkeypatch.setattr(
        ProcessSupervisor,
        "matches_identity",
        staticmethod(observed_matches),
    )

    assert store.claim_dispatch(
        "o/r", "fix", 2, "RESOLVE", "owner-b", frozenset({"b.py"})
    )


def test_dispatch_claim_survives_dead_daemon_while_bound_child_is_live(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    owner = "o/r:fix:checkout-a"
    run_id = RunId("bound-child")
    request = dataclasses.replace(
        _request(1),
        run_id=run_id,
        reservation_id=ReservationId("bound-child-reservation"),
        owner_id=owner,
        kind="RESOLVE",
    )
    assert store.claim_dispatch(
        "o/r", "fix", 42, "RESOLVE", owner, frozenset({"src/shared.py"})
    )
    assert store.reserve(request).admitted
    assert store.bind_dispatch_claim("o/r", "fix", 42, owner, run_id)

    process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        identity = ProcessSupervisor.process_identity(process.pid)
        assert identity is not None
        store.register_lease(
            Lease(
                run_id,
                request.dispatch_id,
                owner,
                process.pid,
                os.getpgid(process.pid),
                NOW,
                identity.start_ticks,
            )
        )
        with store._connect() as db:
            db.execute(
                "UPDATE dispatch_claims SET owner_pid=?, owner_start_ticks=? "
                "WHERE repository='o/r' AND role='fix' AND target='issue:42'",
                (2_000_000_000, 1),
            )

        assert not store.claim_dispatch(
            "o/r",
            "fix",
            42,
            "RESOLVE",
            "o/r:fix:checkout-b",
            frozenset({"src/shared.py"}),
        )
    finally:
        process.terminate()
        process.wait(timeout=5)

    assert store.reclaim_dead_leases(
        owner, lambda _pid, _start: False
    ) == (run_id,)
    assert store.release(run_id, "spend_unknown").disposition.value == (
        "available"
    )
    assert store.claim_dispatch(
        "o/r",
        "fix",
        42,
        "RESOLVE",
        "o/r:fix:checkout-b",
        frozenset({"src/shared.py"}),
    )


def test_legacy_owner_adoption_preserves_active_runs_leases_and_commands(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    legacy = "o/r:fix"
    current = "o/r:fix:deployment-2"
    request = dataclasses.replace(_request(1), owner_id=legacy)
    assert store.reserve(request).disposition is AccountingDisposition.AVAILABLE
    identity = ProcessSupervisor.process_identity(os.getpid())
    assert identity is not None
    store.register_lease(
        Lease(
            request.run_id,
            request.dispatch_id,
            legacy,
            identity.pid,
            os.getpgid(identity.pid),
            NOW,
            identity.start_ticks,
        )
    )
    command = LifecycleCommand(
        CommandId("legacy-command"),
        "github:legacy-command",
        CommandKind.POST_COMMENT,
        "issue:7",
        None,
        {"target": "issue", "number": 7, "body": "done"},
        "cfg",
        "o/r",
        legacy,
    )
    store.enqueue_command(command)

    assert store.adopt_legacy_owner("o/r", "fix", current) == (1, 1)

    with store._connect() as db:
        assert db.execute(
            "SELECT owner_id FROM runs WHERE run_id=?",
            (request.run_id,),
        ).fetchone()[0] == current
        assert db.execute(
            "SELECT owner_id FROM leases WHERE run_id=?",
            (request.run_id,),
        ).fetchone()[0] == current
    assert (
        store.pending_commands(
            repository="o/r",
            executor_identity=current,
        )[0].command.command_id
        == command.command_id
    )


def test_case_alias_owner_is_adopted_by_canonical_deployment(tmp_path) -> None:
    store = _store(tmp_path)
    prior = "Acme/App:fix:old-deployment"
    prior_legacy = "Acme/App:fix"
    current = "acme/app:fix:new-deployment"
    request = dataclasses.replace(_request(1), owner_id=prior)
    assert store.reserve(request).admitted
    command = LifecycleCommand(
        CommandId("alias-owner-command"),
        "github:alias-owner-command",
        CommandKind.POST_COMMENT,
        "issue:7",
        None,
        {"target": "issue", "number": 7, "body": "done"},
        "cfg",
        "Acme/App",
        prior_legacy,
    )
    store.enqueue_command(command)

    assert store.adopt_legacy_owner(
        "acme/app",
        "fix",
        current,
        prior_owner_ids=(prior_legacy, prior),
    ) == (1, 1)

    assert store.pending_commands(
        repository="acme/app",
        executor_identity=current,
    )[0].command.command_id == command.command_id


def test_repoint_permanently_fences_the_prior_owner_from_authority(
    tmp_path,
) -> None:
    current_store = _store(tmp_path)
    old_owner = "o/r:fix:deployment-1"
    current_owner = "o/r:fix:deployment-2"
    old_store = SQLiteControlStore(current_store.path, now=lambda: NOW)
    old_store.initialize()
    old_store.bind_active_owner(old_owner)

    assert current_store.adopt_legacy_owner(
        "o/r",
        "fix",
        current_owner,
        prior_owner_ids=(old_owner,),
    ) == (0, 0)

    with pytest.raises(RetiredOwnerError, match="deployment-2"):
        old_store.assert_owner_active(old_owner)
    assert not old_store.claim_dispatch(
        "o/r",
        "fix",
        91,
        "RESOLVE",
        old_owner,
        frozenset({"src/shared.py"}),
    )
    refused = old_store.reserve(
        dataclasses.replace(
            _request(91),
            owner_id=old_owner,
        )
    )
    assert refused.disposition is AccountingDisposition.UNAVAILABLE
    assert "retired" in refused.detail
    with pytest.raises(RetiredOwnerError, match="deployment-2"):
        old_store.replace_level_state("fix", "o/r", {})

    current_store.bind_active_owner(current_owner)
    assert current_store.claim_dispatch(
        "o/r",
        "fix",
        91,
        "RESOLVE",
        current_owner,
        frozenset({"src/shared.py"}),
    )


def test_repoint_waits_for_inflight_remote_effect_authority(
    tmp_path,
) -> None:
    current_store = _store(tmp_path)
    old_store = SQLiteControlStore(current_store.path, now=lambda: NOW)
    old_store.initialize()
    old_owner = "o/r:fix:deployment-1"
    current_owner = "o/r:fix:deployment-2"
    authority = DeploymentAuthority(
        old_store,
        "o/r",
        "fix",
        old_owner,
    )
    retirement_started = Event()
    retirement_completed = Event()

    def retire_owner() -> tuple[int, int]:
        retirement_started.set()
        result = current_store.adopt_legacy_owner(
            "o/r",
            "fix",
            current_owner,
            prior_owner_ids=(old_owner,),
        )
        retirement_completed.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as pool:
        with authority.effect():
            retirement = pool.submit(retire_owner)
            assert retirement_started.wait(timeout=1)
            assert not retirement_completed.wait(timeout=0.1)
            old_store.assert_owner_active(old_owner)

        assert retirement.result(timeout=1) == (0, 0)

    assert retirement_completed.is_set()
    with pytest.raises(RetiredOwnerError, match="deployment-2"):
        with authority.effect():
            pass


def test_peer_effects_share_authority_without_serializing_each_other(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    authority = DeploymentAuthority(
        store,
        "o/r",
        "fix",
        "o/r:fix:deployment-1",
    )
    first_entered = Event()
    second_entered = Event()
    release = Event()

    def first() -> None:
        with authority.effect():
            first_entered.set()
            assert release.wait(timeout=1)

    def second() -> None:
        assert first_entered.wait(timeout=1)
        with authority.effect():
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(first)
        second_result = pool.submit(second)
        assert second_entered.wait(timeout=1)
        release.set()
        first_result.result(timeout=1)
        second_result.result(timeout=1)


def test_deployment_authority_rejects_a_mismatched_lock_scope(tmp_path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="role does not match"):
        DeploymentAuthority(
            store,
            "o/r",
            "review",
            "o/r:fix:deployment-1",
        )


def test_repoint_fences_delayed_pre_spawn_child_on_fresh_store(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    old_owner = "o/r:fix:deployment-1"
    current_owner = "o/r:fix:deployment-2"
    request = dataclasses.replace(_request(1), owner_id=old_owner)
    assert store.reserve(request).admitted
    assert store.adopt_legacy_owner(
        "o/r",
        "fix",
        current_owner,
        prior_owner_ids=(old_owner,),
    ) == (1, 0)

    delayed_child_store = SQLiteControlStore(store.path, now=lambda: NOW)
    delayed_child_store.initialize()
    with pytest.raises(RetiredOwnerError, match="deployment-2"):
        delayed_child_store.register_process_lease(
            request.run_id,
            expected_owner_id=old_owner,
            pid=123,
            pgid=123,
            process_start_ticks=99,
        )

    assert delayed_child_store.leases() == ()


def test_repoint_retirement_is_transitive_and_cannot_be_reversed(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    first = "o/r:fix:deployment-1"
    second = "o/r:fix:deployment-2"
    third = "o/r:fix:deployment-3"

    store.adopt_legacy_owner(
        "o/r",
        "fix",
        second,
        prior_owner_ids=(first,),
    )
    store.adopt_legacy_owner(
        "o/r",
        "fix",
        third,
        prior_owner_ids=(first, second),
    )

    with store._connect() as db:
        retired = dict(
            db.execute(
                "SELECT owner_id, successor_owner_id FROM retired_owners "
                "WHERE owner_id IN (?, ?)",
                (first, second),
            ).fetchall()
        )
    assert retired == {first: third, second: third}
    with pytest.raises(RetiredOwnerError, match="deployment-3"):
        store.adopt_legacy_owner(
            "o/r",
            "fix",
            second,
            prior_owner_ids=(first,),
        )
    store.assert_owner_active(third)


def test_concurrent_sublegs_may_share_a_dispatch_context(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    first = _request(1, maximum=40, cap=100)
    second = dataclasses.replace(
        _request(2, maximum=40, cap=100),
        dispatch_id=first.dispatch_id,
    )

    assert store.reserve(first).disposition is AccountingDisposition.AVAILABLE
    assert store.reserve(second).disposition is AccountingDisposition.AVAILABLE


def test_publication_authority_requires_active_reservation_and_live_lease(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    request = dataclasses.replace(
        _request(1),
        owner_id="o/r:fix:deployment",
        dispatch_id=json.dumps(
            {
                "repository": "o/r",
                "role": "fix",
                "kind": "RESOLVE",
                "number": 1,
                "issue": 1,
                "cwd": "/work/issue-1",
                "publication_branch": "fix/1-test",
            },
            sort_keys=True,
        ),
    )
    assert store.reserve(request).admitted
    assert store.publication_authority(request.run_id) is None
    pgid = os.getpgid(0)
    identity = ProcessSupervisor.process_identity(pgid)
    assert identity is not None
    store.register_process_lease(
        request.run_id,
        expected_owner_id=request.owner_id,
        pid=pgid,
        pgid=pgid,
        process_start_ticks=identity.start_ticks,
    )

    authority = store.publication_authority(request.run_id)

    assert authority is not None
    assert authority.repository == "o/r"
    assert authority.owner_id == "o/r:fix:deployment"
    assert authority.branch == "fix/1-test"
    assert authority.pgid == pgid


@pytest.mark.parametrize("version", EXPECTED_SCHEMA_VERSIONS)
@pytest.mark.parametrize("stage", ("before_commit", "committed"))
def test_schema_migration_restart_is_atomic_at_every_boundary(
    tmp_path,
    version,
    stage,
):
    path = tmp_path / f"control-{version}-{stage}.db"
    tripped = False

    def interrupt(observed_version, observed_stage):
        nonlocal tripped
        if (
            not tripped
            and observed_version == version
            and observed_stage == stage
        ):
            tripped = True
            raise RuntimeError("simulated process death")

    with pytest.raises(RuntimeError, match="simulated process death"):
        SQLiteControlStore(
            str(path),
            migration_observer=interrupt,
        ).initialize()

    reopened = SQLiteControlStore(str(path))
    reopened.initialize()
    reopened.integrity_check()
    with sqlite3.connect(path) as db:
        versions = tuple(
            row[0]
            for row in db.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        cancellation_table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='run_cancellation_intents'"
        ).fetchone()
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
        command_columns = {row[1] for row in db.execute("PRAGMA table_info(lifecycle_commands)")}
    assert versions == EXPECTED_SCHEMA_VERSIONS
    assert cancellation_table == ("run_cancellation_intents",)
    assert {
        "workflow_authority_epochs", "workflows", "workflow_gates",
        "workflow_rearms", "workflow_rearm_commands", "workflow_evidence",
        "workflow_retry_authorizations",
    } <= tables
    assert "workflow_binding_json" in run_columns
    assert {"workflow_binding_json", "workflow_reconciliation_json", "attempt_owner"} <= command_columns


def test_schema_migration_digest_and_contiguity_are_validated(tmp_path):
    store = _store(tmp_path)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE schema_migrations SET source_digest='tampered' "
            "WHERE version=9"
        )
        db.commit()

    with pytest.raises(IncompatibleSchema, match="digest mismatch"):
        SQLiteControlStore(store.path).initialize()

    other = SQLiteControlStore(str(tmp_path / "other.db"))
    other.initialize()
    with sqlite3.connect(other.path) as db:
        db.execute("DELETE FROM schema_migrations WHERE version=9")
        db.commit()
    with pytest.raises(IncompatibleSchema, match="not contiguous"):
        SQLiteControlStore(other.path).initialize()


def test_corrupt_database_is_rejected_before_migration_or_admission(tmp_path):
    path = tmp_path / "state" / "control.db"
    path.parent.mkdir()
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(CorruptControlStore, match="integrity check failed"):
        SQLiteControlStore(str(path)).initialize()


def test_semantically_corrupt_accounting_is_rejected_before_admission(tmp_path):
    store = _store(tmp_path)
    assert store.reserve(_request(1)).admitted
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE reservation_limits SET cap_micros=cap_micros+1 "
            "WHERE run_id='run-1' AND dimension='daily'"
        )
        db.commit()

    reopened = SQLiteControlStore(store.path)
    with pytest.raises(CorruptControlStore, match="primary limit"):
        reopened.initialize()
    result = reopened.reserve(_request(2))
    assert result.disposition is AccountingDisposition.CORRUPT
    assert "primary limit" in result.detail


def test_active_reservation_requires_recovery_classification(tmp_path):
    store = _store(tmp_path)
    assert store.reserve(_request(1)).admitted
    with sqlite3.connect(store.path) as db:
        db.execute(
            "DELETE FROM reservation_recovery WHERE run_id='run-1'"
        )

    with pytest.raises(CorruptControlStore, match="classification is absent"):
        SQLiteControlStore(store.path).initialize()


def test_semantically_corrupt_dispatch_binding_is_rejected(tmp_path):
    store = _store(tmp_path)
    owner = "o/r:fix:deployment"
    request = dataclasses.replace(_request(1), owner_id=owner)
    assert store.claim_dispatch(
        "o/r", "fix", 1, "RESOLVE", owner, frozenset({"src/a.py"})
    )
    assert store.reserve(request).admitted
    assert store.bind_dispatch_claim(
        "o/r", "fix", 1, owner, request.run_id
    )
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE runs SET owner_id='other-owner' WHERE run_id='run-1'"
        )

    with pytest.raises(CorruptControlStore, match="bound run owners"):
        SQLiteControlStore(store.path).initialize()


@pytest.mark.parametrize(
    "message",
    ["database or disk is full", "attempt to write a readonly database"],
)
def test_storage_write_faults_fail_paid_admission_closed(
    tmp_path, monkeypatch, message
):
    store = _store(tmp_path)

    @contextmanager
    def unavailable_transaction():
        raise sqlite3.OperationalError(message)
        yield

    monkeypatch.setattr(store, "transaction", unavailable_transaction)

    result = store.reserve(_request(1))

    assert result.disposition is AccountingDisposition.UNAVAILABLE
    assert message in result.detail


def test_abandoned_run_counts_come_from_control_state_not_telemetry(tmp_path):
    store = _store(tmp_path)
    request = dataclasses.replace(
        _request(1),
        dispatch_id=json.dumps({"breaker_key": 42}),
        kind="RESOLVE",
        owner_id="o/r:fix",
    )
    assert store.reserve(request).admitted
    store.register_lease(
        Lease(
            request.run_id,
            request.dispatch_id,
            "o/r:fix:999",
            999,
            999,
            NOW,
        )
    )

    assert store.reclaim_dead_leases(lambda _pid, _start: False) == (
        request.run_id,
    )
    assert store.release(request.run_id, "spend_unknown").disposition.value == "available"
    assert store.abandoned_dispatch_counts("o/r:fix") == {42: 1}


def test_shutdown_intent_preserves_active_spend_for_recovery(tmp_path):
    store = _store(tmp_path)
    request = dataclasses.replace(
        _request(1),
        dispatch_id=json.dumps(
            {
                "kind": "RESOLVE",
                "number": 42,
                "issue": 42,
                "breaker_key": 42,
            }
        ),
        kind="RESOLVE",
        owner_id="o/r:fix",
    )
    assert store.reserve(request).admitted
    store.register_process_lease(
        request.run_id,
        expected_owner_id=request.owner_id,
        pid=123,
        pgid=123,
        process_start_ticks=99,
    )

    intents = store.record_owned_cancellation_intents(
        "o/r:fix",
        "shutdown_deadline_spend_unknown",
    )

    assert len(intents) == 1
    assert intents[0].issue_number == 42
    assert store.active_reservation_runs("o/r:fix") == (request.run_id,)
    assert tuple(lease.run_id for lease in store.leases("o/r:fix")) == (
        request.run_id,
    )
    assert store.pending_cancellation_intents("o/r:fix") == intents
    with sqlite3.connect(store.path) as db:
        run = db.execute(
            "SELECT state, outcome FROM runs WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
        reservation = db.execute(
            "SELECT state, release_reason FROM budget_reservations "
            "WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
    assert run == ("running", None)
    assert reservation == ("active", None)

    store.mark_cancellation_handover_written(request.run_id)
    assert store.pending_cancellation_intents("o/r:fix") == ()


def test_transactional_reservations_admit_only_one_racer_for_final_capacity(tmp_path):
    store = _store(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(store.reserve, (_request(1), _request(2))))
    assert sorted(result.disposition.value for result in results) == [
        "available",
        "exhausted",
    ]
    assert store.snapshot(_scope()).reserved == Money("USD", 60)


def test_reservation_replay_requires_the_complete_same_paid_authority(tmp_path):
    store = _store(tmp_path)
    original = _request(1)
    assert store.reserve(original).admitted
    assert store.reserve(original).detail == "idempotent reservation"

    other_scope = dataclasses.replace(
        original.scope,
        repository="other/repo",
        dimension="weekly",
        period_id="2026-W31",
        period_end=original.scope.period_end + timedelta(days=6),
    )
    global_scope = dataclasses.replace(
        original.scope,
        repository="*",
        dimension="global",
    )
    conflicting = dataclasses.replace(
        original,
        dispatch_id="other-dispatch",
        scope=other_scope,
        quote=dataclasses.replace(
            original.quote,
            model="other-model",
            effective_at=NOW + timedelta(seconds=1),
        ),
        cap=Money("USD", 1),
        config_version="config-v2",
        additional_limits=(BudgetLimit(global_scope, Money("USD", 1)),),
        kind="REVIEW",
        owner_id="other-owner",
    )

    result = store.reserve(conflicting)

    assert result.disposition is AccountingDisposition.CONFLICT
    with store._connect() as db:
        persisted = db.execute(
            "SELECT b.repository, b.cap_micros, r.dispatch_id, r.owner_id "
            "FROM budget_reservations b JOIN runs r ON r.run_id=b.run_id "
            "WHERE b.run_id=?",
            (str(original.run_id),),
        ).fetchone()
        limit_count = db.execute(
            "SELECT COUNT(*) FROM reservation_limits WHERE run_id=?",
            (str(original.run_id),),
        ).fetchone()[0]
    assert tuple(persisted) == ("o/r", 100, "dispatch-1", "reservation")
    assert limit_count == 1


@pytest.mark.parametrize("terminal", ("settled", "released"))
def test_terminal_reservation_is_not_a_new_paid_admission(tmp_path, terminal):
    store = _store(tmp_path)
    request = _request(1)
    assert store.reserve(request).admitted
    if terminal == "settled":
        store.settle(
            SettlementRequest(
                request.run_id,
                Money("USD", 1),
                PricingBasis.PROVIDER_REPORTED,
                "terminal-receipt",
                None,
                None,
                None,
            )
        )
    else:
        store.release(request.run_id, "cancelled")

    assert (
        store.reserve(request).disposition
        is AccountingDisposition.CONFLICT
    )


def test_two_processes_racing_final_capacity_admit_exactly_one(tmp_path):
    path = str(tmp_path / "state" / "control.db")
    _store(tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_reserve_in_process,
            args=(path, index, start, results),
        )
        for index in (1, 2)
    ]
    for process in processes:
        process.start()
    start.set()
    values = sorted(results.get(timeout=10) for _ in processes)
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert values == ["available", "exhausted"]


def test_hundreds_of_concurrent_reservations_all_reach_one_terminal_state(
    tmp_path,
):
    store = _store(tmp_path)
    requests = tuple(
        _request(index, maximum=1, cap=200) for index in range(200)
    )
    with ThreadPoolExecutor(max_workers=32) as pool:
        admissions = tuple(pool.map(store.reserve, requests))
    assert all(result.admitted for result in admissions)

    def finalize(request: ReservationRequest):
        if int(str(request.run_id).split("-")[-1]) % 2:
            return store.release(request.run_id, "stress")
        return store.settle(
            SettlementRequest(
                request.run_id,
                Money("USD", 1),
                PricingBasis.PROVIDER_REPORTED,
                f"receipt-{request.run_id}",
                None,
                None,
                None,
            )
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        tuple(pool.map(finalize, requests))

    snapshot = store.snapshot(_scope())
    assert snapshot.reserved == Money("USD", 0)
    assert snapshot.committed == Money("USD", 100)
    assert store.active_reservation_runs() == ()


def test_reservation_and_owner_lease_commit_atomically(tmp_path):
    store = _store(tmp_path)
    request = dataclasses.replace(_request(1), owner_id="owner")
    lease = Lease(
        request.run_id,
        request.dispatch_id,
        "owner",
        123,
        123,
        NOW,
    )

    assert store.reserve(request, lease=lease).admitted
    assert store.reserve(request, lease=lease).detail == "idempotent reservation"

    assert store.leases() == (lease,)
    assert store.active_reservation_runs() == (request.run_id,)


def test_spawned_process_lease_binds_to_reserved_run_identity(tmp_path):
    store = _store(tmp_path)
    request = dataclasses.replace(_request(1), owner_id="owner")
    assert store.reserve(request).admitted

    lease = store.register_process_lease(
        request.run_id,
        expected_owner_id=request.owner_id,
        pid=123,
        pgid=124,
        process_start_ticks=99,
    )

    assert lease == Lease(
        request.run_id,
        request.dispatch_id,
        "owner",
        123,
        124,
        NOW,
        99,
    )
    assert store.register_process_lease(
        request.run_id,
        expected_owner_id=request.owner_id,
        pid=123,
        pgid=124,
        process_start_ticks=99,
    ) == lease
    with pytest.raises(ControlStoreError, match="conflicting"):
        store.register_process_lease(
            request.run_id,
            expected_owner_id=request.owner_id,
            pid=125,
            pgid=125,
            process_start_ticks=100,
        )


def test_pid_reuse_does_not_keep_a_dead_lease(tmp_path):
    store = _store(tmp_path)
    request = dataclasses.replace(_request(1), owner_id="owner")
    lease = Lease(
        request.run_id,
        request.dispatch_id,
        "owner",
        123,
        123,
        NOW,
        10,
    )
    assert store.reserve(request, lease=lease).admitted

    assert store.reclaim_dead_leases(
        "owner", lambda pid, start: pid == 123 and start == 11
    ) == (request.run_id,)


def test_dead_lease_liveness_probe_does_not_hold_sqlite_write_lock(
    tmp_path,
):
    store = _store(tmp_path)
    request = dataclasses.replace(_request(1), owner_id="owner")
    lease = Lease(
        request.run_id,
        request.dispatch_id,
        "owner",
        123,
        123,
        NOW,
        10,
    )
    assert store.reserve(request, lease=lease).admitted
    competing = SQLiteControlStore(store.path, busy_timeout_ms=50)
    probe_wrote = False

    def alive(_pid, _start):
        nonlocal probe_wrote
        competing.start_run(
            RunId("probe-write"),
            "probe-dispatch",
            "RESOLVE",
            "other-owner",
            "cfg",
        )
        probe_wrote = True
        return True

    assert store.reclaim_dead_leases("owner", alive) == ()
    assert probe_wrote


def test_one_transaction_enforces_shared_global_limit_across_repositories(tmp_path):
    authority = tmp_path / "machine-state"
    paths = (
        control_store_path(
            (str(tmp_path / "repo-a" / "fix"),),
            authority_root=str(authority),
        ),
        control_store_path(
            (str(tmp_path / "repo-b" / "fix"),),
            authority_root=str(authority),
        ),
    )
    assert paths[0] == paths[1]
    stores = tuple(SQLiteControlStore(path, now=lambda: NOW) for path in paths)
    for store in stores:
        store.initialize()
    global_scope = BudgetScope(
        "*",
        "global",
        "USD",
        "2026-07-30",
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
        ZoneInfo("UTC"),
    )

    def request(repository: str, index: int) -> ReservationRequest:
        scope = dataclasses.replace(_scope(), repository=repository)
        return dataclasses.replace(
            _request(index, maximum=60, cap=1_000),
            scope=scope,
            workload_repository=repository,
            additional_limits=(BudgetLimit(global_scope, Money("USD", 100)),),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda pair: pair[0].reserve(pair[1]),
                (
                    (stores[0], request("o/one", 1)),
                    (stores[1], request("o/two", 2)),
                ),
            )
        )

    assert sorted(result.disposition.value for result in results) == [
        "available",
        "exhausted",
    ]
    assert stores[0].snapshot(global_scope).reserved == Money("USD", 60)


def test_settled_spend_read_model_uses_authoritative_charges(tmp_path) -> None:
    store = _store(tmp_path)
    first = _request(1)
    second = dataclasses.replace(
        _request(2),
        scope=dataclasses.replace(_scope(), repository="o/other"),
        workload_repository="o/other",
    )
    assert store.reserve(first).admitted
    assert store.reserve(second).admitted
    assert store.settle(
        SettlementRequest(
            first.run_id,
            Money("USD", 25),
            PricingBasis.PROVIDER_REPORTED,
            "receipt-1",
            None,
            None,
            None,
        )
    ).disposition is AccountingDisposition.AVAILABLE
    assert store.settle(
        SettlementRequest(
            second.run_id,
            Money("USD", 40),
            PricingBasis.PROVIDER_REPORTED,
            "receipt-2",
            None,
            None,
            None,
        )
    ).disposition is AccountingDisposition.AVAILABLE

    summary = read_settled_spend(
        store.path,
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
    )

    assert summary.disposition is AccountingDisposition.AVAILABLE
    assert summary.total == Money("USD", 65)
    assert summary.for_repository("O/R") == Money("USD", 25)
    assert summary.for_repository("o/other") == Money("USD", 40)
    assert summary.for_repository("o/absent") == Money.zero("USD")


def test_global_only_reservation_keeps_workload_repository_attribution(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    request = dataclasses.replace(
        _request(1),
        scope=dataclasses.replace(
            _scope(),
            repository="*",
            dimension="global",
        ),
        workload_repository="o/r",
    )
    assert store.reserve(request).admitted
    assert store.settle(
        SettlementRequest(
            request.run_id,
            Money("USD", 50),
            PricingBasis.PROVIDER_REPORTED,
            "global-only-receipt",
            None,
            None,
            None,
        )
    ).disposition is AccountingDisposition.AVAILABLE

    summary = read_settled_spend(
        store.path,
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
    )

    assert summary.total == Money("USD", 50)
    assert summary.for_repository("o/r") == Money("USD", 50)
    assert summary.for_repository("*") == Money.zero("USD")


def test_missing_control_store_is_unknown_without_creating_it(tmp_path) -> None:
    path = tmp_path / "absent" / "control.db"

    summary = read_settled_spend(
        str(path),
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
    )

    assert summary.disposition is AccountingDisposition.UNAVAILABLE
    assert summary.total is None
    assert summary.for_repository("o/r") is None
    assert summary.by_repository == {}
    assert "does not exist" in summary.detail
    assert not path.exists()


def test_invalid_control_store_is_unknown_without_mutating_it(tmp_path) -> None:
    path = tmp_path / "control.db"
    original = b"not a SQLite database"
    path.write_bytes(original)

    summary = read_settled_spend(
        str(path),
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
    )

    assert summary.disposition is AccountingDisposition.UNAVAILABLE
    assert summary.total is None
    assert summary.for_repository("o/r") is None
    assert summary.detail
    assert path.read_bytes() == original


def test_semantically_corrupt_control_store_spend_is_unknown(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    request = _request(1)
    assert store.reserve(request).admitted
    assert store.settle(
        SettlementRequest(
            request.run_id,
            Money("USD", 50),
            PricingBasis.PROVIDER_REPORTED,
            "receipt",
            None,
            None,
            None,
        )
    ).disposition is AccountingDisposition.AVAILABLE
    with sqlite3.connect(store.path) as db:
        db.execute(
            "DELETE FROM reservation_limits WHERE run_id=?",
            (str(request.run_id),),
        )

    summary = read_settled_spend(
        store.path,
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
    )

    assert summary.disposition is AccountingDisposition.UNAVAILABLE
    assert summary.total is None
    assert "semantic integrity failed" in summary.detail


def test_settle_is_idempotent_and_conflicting_receipt_is_rejected(tmp_path):
    store = _store(tmp_path)
    assert store.reserve(_request(1)).admitted
    request = SettlementRequest(
        RunId("run-1"),
        Money("USD", 50),
        PricingBasis.CONFIGURED_RATE_CARD,
        "provider-1",
        TokenUsage(1, 2),
        "card",
        "v1",
    )
    assert store.settle(request).disposition is AccountingDisposition.AVAILABLE
    assert store.settle(request).idempotent
    conflict = SettlementRequest(
        RunId("run-1"),
        Money("USD", 51),
        PricingBasis.CONFIGURED_RATE_CARD,
        "provider-1",
        TokenUsage(1, 2),
        "card",
        "v1",
    )
    assert store.settle(conflict).disposition is AccountingDisposition.CONFLICT
    assert store.snapshot(_scope()).settled == Money("USD", 50)
    assert store.snapshot(_scope()).reserved == Money("USD", 0)


@pytest.mark.parametrize("recovery_settles_first", (False, True))
def test_normal_settlement_converges_with_dead_lease_recovery(
    tmp_path,
    recovery_settles_first,
) -> None:
    store = _store(tmp_path)
    request = dataclasses.replace(_request(1), owner_id="o/r:fix")
    assert store.reserve(
        request,
        lease=Lease(
            request.run_id,
            request.dispatch_id,
            request.owner_id,
            123,
            123,
            NOW,
            99,
        ),
    ).admitted
    assert store.reclaim_dead_leases(
        request.owner_id, lambda _pid, _start: False
    ) == (request.run_id,)
    recovered = SettlementRequest(
        request.run_id,
        Money("USD", 50),
        PricingBasis.RECONCILED,
        "codex-recovered:session:run-1",
        TokenUsage(1, 2),
        "card",
        "v1",
    )
    normal = SettlementRequest(
        request.run_id,
        Money("USD", 50),
        PricingBasis.CONFIGURED_RATE_CARD,
        "codex:session:run-1",
        TokenUsage(1, 2),
        "card",
        "v1",
    )
    if recovery_settles_first:
        assert (
            store.settle(recovered).disposition
            is AccountingDisposition.AVAILABLE
        )

    normal_result = store.settle(normal)

    assert normal_result.disposition is AccountingDisposition.AVAILABLE
    assert store.settle(normal).disposition is AccountingDisposition.AVAILABLE
    with sqlite3.connect(store.path) as db:
        state = db.execute(
            "SELECT state, outcome FROM runs WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
        reservation_state = db.execute(
            "SELECT state FROM budget_reservations WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()[0]
        charges = db.execute(
            "SELECT COUNT(*) FROM charges WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()[0]
    assert state == ("completed", "settled")
    assert reservation_state == "settled"
    assert charges == 1


def test_dead_lease_cannot_use_completed_unmeasured_release_path(
    tmp_path,
) -> None:
    store = _store(tmp_path)
    request = dataclasses.replace(_request(1), owner_id="o/r:fix")
    assert store.reserve(
        request,
        lease=Lease(
            request.run_id,
            request.dispatch_id,
            request.owner_id,
            123,
            123,
            NOW,
            99,
        ),
    ).admitted
    assert store.reclaim_dead_leases(
        request.owner_id, lambda _pid, _start: False
    ) == (request.run_id,)

    released = store.release_retained(
        request.run_id,
        "unmeasured_completed_reservation",
        NOW + timedelta(minutes=3),
    )

    assert released.disposition is AccountingDisposition.CONFLICT
    assert store.active_reservation_runs(request.owner_id) == (
        request.run_id,
    )


def test_release_is_terminal_idempotent_and_returns_capacity(tmp_path):
    store = _store(tmp_path)
    store.reserve(_request(1))
    released = store.release(RunId("run-1"), "cancelled")
    assert released.disposition is AccountingDisposition.AVAILABLE
    assert store.release(RunId("run-1"), "cancelled").idempotent
    assert (
        store.release(RunId("run-1"), "different").disposition
        is AccountingDisposition.CONFLICT
    )
    assert store.reserve(_request(2)).admitted


def test_dedupe_claim_survives_reopen_and_receipt_detects_conflict(tmp_path):
    store = _store(tmp_path)
    assert store.claim("alert", "repo:o/r", "same")
    assert not store.claim("alert", "repo:o/r", "same")
    reopened = SQLiteControlStore(store.path, now=lambda: NOW)
    reopened.initialize()
    assert not reopened.claim("alert", "repo:o/r", "same")
    assert reopened.receipt("effect", "repo:o/r", "key", {"id": 1})
    assert reopened.receipt("effect", "repo:o/r", "key", {"id": 1})
    assert not reopened.receipt("effect", "repo:o/r", "key", {"id": 2})


def test_leases_are_durable_and_terminal_run_releases_them(tmp_path):
    store = _store(tmp_path)
    store.start_run(RunId("run"), "dispatch", "resolve", "owner", "cfg")
    store.register_lease(
        Lease(RunId("run"), "dispatch", "owner", 123, 124, NOW)
    )
    assert store.leases()[0].pgid == 124
    assert store.complete_run(RunId("run"), state="cancelled", outcome="signal")
    assert store.leases() == ()


def test_lifecycle_command_and_effect_receipt_are_durable_and_idempotent(tmp_path):
    store = _store(tmp_path)
    command = LifecycleCommand(
        CommandId("cmd"),
        "comment:issue/1:digest",
        CommandKind.POST_COMMENT,
        "issue/1",
        "revision",
        {"body": "hello"},
        repository="o/r",
        executor_identity="o/r:fix",
    )
    assert store.enqueue_command(command).state == "pending"
    assert store.enqueue_command(command).state == "pending"
    store.begin_command(command.command_id)
    receipt = EffectReceipt(
        command.command_id,
        "broker",
        NOW,
        NOW,
        EffectStatus.SUCCEEDED,
        remote_object_id="comment-1",
    )
    assert store.record_effect(receipt)
    assert store.record_effect(receipt)
    assert store.effect_receipt(command.command_id) == receipt

    repeated = store.enqueue_command_occurrence(command)
    assert repeated.command.command_id != command.command_id
    assert repeated.command.idempotency_key != command.idempotency_key
    assert repeated.state == "pending"
    store.begin_command(repeated.command.command_id)
    assert store.record_effect(
        EffectReceipt(
            repeated.command.command_id,
            "broker",
            NOW,
            NOW,
            EffectStatus.SUCCEEDED,
            remote_object_id="comment-2",
        )
    )
    assert store.pending_commands() == ()


def test_large_effect_error_is_bounded_and_receipt_still_commits(tmp_path):
    store = _store(tmp_path)
    command = LifecycleCommand(
        CommandId("large-error"),
        "comment:issue/1:large-error",
        CommandKind.POST_COMMENT,
        "issue:1",
        None,
        {"body": "hello"},
        repository="o/r",
        executor_identity="o/r:fix",
    )
    store.enqueue_command(command)
    store.begin_command(command.command_id)

    receipt = EffectReceipt(
        command.command_id,
        "broker",
        NOW,
        NOW,
        EffectStatus.RETRYABLE_FAILURE,
        error="x" * 70_000,
    )

    assert store.record_effect(receipt)
    persisted = store.effect_receipt(command.command_id)
    assert persisted is not None
    assert persisted.status is EffectStatus.RETRYABLE_FAILURE
    assert persisted.error == receipt.error
    assert len((persisted.error or "").encode()) <= 8 * 1024


def test_concurrent_recurrence_claims_share_one_active_occurrence(tmp_path):
    store = _store(tmp_path)
    command = LifecycleCommand(
        CommandId("base-command"),
        "label:issue/1",
        CommandKind.RELABEL,
        "issue/1",
        None,
        {"add": "ai-working"},
        repository="o/r",
        executor_identity="o/r:fix",
    )
    store.enqueue_command(command)
    store.begin_command(command.command_id)
    store.record_effect(
        EffectReceipt(
            command.command_id,
            "broker",
            NOW,
            NOW,
            EffectStatus.SUCCEEDED,
        )
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        occurrences = tuple(
            pool.map(
                lambda _index: store.enqueue_command_occurrence(command),
                range(32),
            )
        )

    assert len(
        {occurrence.command.command_id for occurrence in occurrences}
    ) == 1
    assert store.pending_commands() == (occurrences[0],)


def test_outbox_retries_until_sink_acknowledges_and_preserves_order(tmp_path):
    store = _store(tmp_path)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    events = tuple(
        EventEnvelope(
            TelemetryWarning("warning", str(index)),
            new_event_id(NOW + timedelta(milliseconds=index)),
            index,
            NOW + timedelta(milliseconds=index),
            NOW + timedelta(milliseconds=index),
            index,
            producer,
            Correlation(issue=1),
        )
        for index in range(2)
    )
    for event in events:
        store.enqueue_event(event)

    seen = []
    with pytest.raises(RuntimeError):
        store.flush_outbox(lambda event: (_ for _ in ()).throw(RuntimeError("down")))
    assert store.flush_outbox(seen.append) == 2
    assert seen == list(events)
    assert store.flush_outbox(seen.append) == 0


def test_shared_outbox_flushes_only_the_bound_role_feed(tmp_path):
    store = _store(tmp_path)
    fix = Producer("deployment", "o/r", "fix", 123, "fix-boot")
    review = Producer("deployment", "o/r", "review", 124, "review-boot")

    def event(producer, event_id):
        return EventEnvelope(
            TelemetryWarning("feed", event_id),
            event_id,
            0,
            NOW,
            NOW,
            0,
            producer,
        )

    store.enqueue_event(event(fix, "fix-event"))
    store.enqueue_event(event(review, "review-event"))
    fix_seen = []
    review_seen = []

    assert store.flush_outbox(
        review_seen.append, destination=review.feed_identity
    ) == 1
    assert [item.event_id for item in review_seen] == ["review-event"]
    assert store.flush_outbox(
        fix_seen.append, destination=fix.feed_identity
    ) == 1
    assert [item.event_id for item in fix_seen] == ["fix-event"]


def test_oversized_event_cannot_roll_back_operational_state_or_poison_outbox(
    tmp_path,
):
    store = _store(tmp_path)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    sequence = 0

    def factory(payload, correlation):
        nonlocal sequence
        event = EventEnvelope(
            payload,
            new_event_id(NOW + timedelta(milliseconds=sequence)),
            sequence,
            NOW,
            NOW,
            sequence,
            producer,
            correlation,
        )
        sequence += 1
        return event

    published = []
    store.bind_event_outbox(
        factory, published.append, line_limit=600
    )
    assert store.record_world_observation(
        deployment="deployment",
        subject_kind="issue",
        subject_number=1,
        content_hash="large",
        facts={"body": "x" * 1_000},
        correlation=Correlation(issue=1),
    )
    assert store.telemetry_enqueue_failures == 1

    assert store.record_world_observation(
        deployment="deployment",
        subject_kind="issue",
        subject_number=2,
        content_hash="small",
        facts={"state": "OPEN"},
        correlation=Correlation(issue=2),
    )
    assert [
        event.payload.subject_number
        for event in published
        if isinstance(event.payload, WorldObserved)
    ] == [2]
    with store._connect() as db:
        assert db.execute(
            "SELECT content_hash FROM world_observations "
            "WHERE subject_kind='issue' AND subject_number=1"
        ).fetchone()[0] == "large"


def test_existing_oversized_outbox_row_is_dead_lettered_not_blocking(
    tmp_path,
):
    store = _store(tmp_path)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    valid = EventEnvelope(
        TelemetryWarning("later", "valid"),
        "valid-event",
        2,
        NOW,
        NOW,
        2,
        producer,
    )
    with store.transaction() as db:
        db.execute(
            "INSERT INTO event_outbox(event_id, event_json, committed_at) "
            "VALUES (?, ?, ?)",
            ("poison", "x" * 700, NOW.isoformat()),
        )
    store.enqueue_event(valid)
    seen = []
    store._event_line_limit = 600

    assert store.flush_outbox(seen.append) == 1
    assert seen == [valid]
    with store._connect() as db:
        failed = db.execute(
            "SELECT failed_at, failure FROM event_outbox "
            "WHERE event_id='poison'"
        ).fetchone()
    assert failed["failed_at"]
    assert "line limit" in failed["failure"]


def test_committed_accounting_transitions_publish_through_bound_outbox(tmp_path):
    store = _store(tmp_path)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    sequence = 0

    def factory(payload, correlation):
        nonlocal sequence
        event = EventEnvelope(
            payload,
            new_event_id(NOW + timedelta(milliseconds=sequence)),
            sequence,
            NOW + timedelta(milliseconds=sequence),
            NOW + timedelta(milliseconds=sequence),
            sequence,
            producer,
            correlation,
        )
        sequence += 1
        return event

    published = []
    store.bind_event_outbox(factory, published.append)
    store.reserve(_request(1))
    store.settle(
        SettlementRequest(
            RunId("run-1"),
            Money("USD", 50),
            PricingBasis.CONFIGURED_RATE_CARD,
            "provider-1",
            TokenUsage(1, 2),
            "card",
            "v1",
        )
    )

    assert [
        (type(event.payload), event.correlation.run_id)
        for event in published
    ] == [
        (ControlTransitionCommitted, "run-1"),
        (SpendRecorded, "run-1"),
        (ControlTransitionCommitted, "run-1"),
    ]


def test_provider_exceeds_quote_emits_atomic_variance_alert(tmp_path):
    store = _store(tmp_path)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    sequence = 0

    def factory(payload, correlation):
        nonlocal sequence
        event = EventEnvelope(
            payload,
            new_event_id(NOW + timedelta(milliseconds=sequence)),
            sequence,
            NOW,
            NOW,
            sequence,
            producer,
            correlation,
        )
        sequence += 1
        return event

    published = []
    store.bind_event_outbox(factory, published.append)
    store.reserve(_request(1, maximum=60))

    result = store.settle(
        SettlementRequest(
            RunId("run-1"),
            Money("USD", 75),
            PricingBasis.RECONCILED,
            "provider-overrun",
            None,
            None,
            None,
        )
    )

    assert result.variance == Money("USD", 15)
    variance = next(
        event.payload
        for event in published
        if isinstance(event.payload, BudgetVarianceDetected)
    )
    assert variance.quoted_micros == 60
    assert variance.actual_micros == 75
    assert variance.variance_micros == 15


def test_failed_bound_publication_stays_pending_until_rebind(tmp_path):
    store = _store(tmp_path)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")

    def factory(payload, correlation):
        return EventEnvelope(
            payload,
            new_event_id(NOW),
            0,
            NOW,
            NOW,
            0,
            producer,
            correlation,
        )

    store.bind_event_outbox(
        factory,
        lambda _event: (_ for _ in ()).throw(RuntimeError("sink down")),
    )
    assert store.reserve(_request(1)).admitted
    published = []
    store.bind_event_outbox(factory, published.append)
    assert len(published) == 1
    assert isinstance(published[0].payload, ControlTransitionCommitted)


def test_projection_feed_adoption_flushes_old_outbox_and_retires_membership(
    tmp_path,
):
    store = _store(tmp_path)
    old = Producer("old-deployment", "Acme/App", "fix", 123, "old-boot")
    current = Producer(
        "current-deployment", "acme/app", "fix", 124, "current-boot"
    )
    sequences = {old.identity: 0, current.identity: 0}

    def factory(producer):
        def create(payload, correlation):
            sequence = sequences[producer.identity]
            sequences[producer.identity] += 1
            return EventEnvelope(
                payload,
                new_event_id(NOW + timedelta(milliseconds=sequence)),
                sequence,
                NOW + timedelta(milliseconds=sequence),
                NOW + timedelta(milliseconds=sequence),
                sequence,
                producer,
                correlation,
            )

        return create

    old_destination = "old-deployment:Acme/App:fix"
    store.bind_event_outbox(
        factory(old),
        lambda _event: (_ for _ in ()).throw(RuntimeError("sink down")),
        destination=old_destination,
    )
    assert store.reserve(_request(1)).admitted
    facts = {"present": True, "state": "open"}
    content_hash = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert store.record_world_snapshot(
        deployment="old-deployment",
        subject_kind="issue",
        observations={
            7: (content_hash, facts, Correlation(issue=7)),
        },
    ) == 1
    with store._connect() as db:
        db.execute(
            "UPDATE event_outbox SET destination=? "
            "WHERE destination=? AND emitted_at IS NULL",
            (old_destination, old.feed_identity),
        )

    published = []
    store.bind_event_outbox(
        factory(current),
        published.append,
        destination=current.feed_identity,
    )
    assert store.adopt_projection_feed(
        prior_deployment="old-deployment",
        current_deployment="current-deployment",
        repository="acme/app",
        role="fix",
        prior_destination=old_destination,
        current_destination=current.feed_identity,
    ) == (1, 2)
    assert isinstance(published[-1].payload, WorldFeedRetired)

    assert store.record_world_snapshot(
        deployment="current-deployment",
        subject_kind="issue",
        observations={},
    ) == 1
    model = fold(published)
    assert model.world_facts[("issue", 7)] == {"present": False}
    assert all(key[0] != "old-deployment" for key in model.observed_by_feed)
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE emitted_at IS NULL"
        ).fetchone()[0] == 0
        assert [
            row["deployment"]
            for row in db.execute(
                "SELECT DISTINCT deployment FROM world_observations"
            ).fetchall()
        ] == ["current-deployment"]


def test_clean_shutdown_retires_world_membership_until_role_restarts(
    tmp_path,
):
    store = _store(tmp_path)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    published = []
    sequence = 0

    def factory(payload, correlation):
        nonlocal sequence
        event = EventEnvelope(
            payload,
            new_event_id(NOW + timedelta(milliseconds=sequence)),
            sequence,
            NOW + timedelta(milliseconds=sequence),
            NOW + timedelta(milliseconds=sequence),
            sequence,
            producer,
            correlation,
        )
        sequence += 1
        return event

    store.bind_event_outbox(
        factory,
        published.append,
        destination=producer.feed_identity,
    )
    facts = {"present": True, "state": "OPEN"}
    content_hash = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    observations = {
        7: (content_hash, facts, Correlation(issue=7)),
    }
    assert store.record_world_snapshot(
        deployment="deployment",
        subject_kind="issue",
        observations=observations,
    ) == 1

    assert store.retire_projection_feed(
        deployment="deployment",
        repository="o/r",
        role="fix",
    ) == 1

    retirement = published[-1].payload
    assert isinstance(retirement, WorldFeedRetired)
    assert retirement.restartable is True
    assert ("issue", 7) not in fold(published).world_facts
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM world_observations "
            "WHERE deployment='deployment'"
        ).fetchone()[0] == 0

    assert store.record_world_snapshot(
        deployment="deployment",
        subject_kind="issue",
        observations=observations,
    ) == 1
    assert fold(published).world_facts[("issue", 7)] == facts


def test_world_observation_dedup_is_latest_state_and_outbox_atomic(tmp_path):
    store = _store(tmp_path)
    producer = Producer("deployment", "o/r", "fix", 123, "boot")
    sequence = 0

    def factory(payload, correlation):
        nonlocal sequence
        event = EventEnvelope(
            payload,
            new_event_id(NOW + timedelta(milliseconds=sequence)),
            sequence,
            NOW + timedelta(milliseconds=sequence),
            NOW + timedelta(milliseconds=sequence),
            sequence,
            producer,
            correlation,
        )
        sequence += 1
        return event

    store.bind_event_outbox(
        factory,
        lambda _event: (_ for _ in ()).throw(RuntimeError("sink down")),
    )
    common = {
        "deployment": "deployment",
        "subject_kind": "issue",
        "subject_number": 7,
        "correlation": Correlation(issue=7),
    }
    assert store.record_world_observation(
        **common, content_hash="A", facts={"state": "open"}
    )
    assert not store.record_world_observation(
        **common, content_hash="A", facts={"state": "open"}
    )
    assert store.record_world_observation(
        **common, content_hash="B", facts={"state": "closed"}
    )
    assert store.record_world_observation(
        **common, content_hash="A", facts={"state": "open"}
    )

    published = []
    store.bind_event_outbox(factory, published.append)
    observations = [
        event.payload
        for event in published
        if isinstance(event.payload, WorldObserved)
    ]
    assert [event.content_hash for event in observations] == ["A", "B", "A"]


def test_legacy_spend_import_preserves_all_live_cap_dimensions(tmp_path):
    store = _store(tmp_path)
    ledger = tmp_path / "spend.jsonl"
    first = {
        "event": "spend",
        "ts": NOW.isoformat(),
        "repo": "o/r",
        "issue": 42,
        "cost_usd": 1.25,
        "model": "legacy",
    }
    second = {**first, "cost_usd": 0.75, "model": "legacy-2"}
    ledger.write_text(
        "\n".join(json.dumps(row) for row in (first, second)) + "\n"
    )

    report = store.import_legacy_spend(str(ledger))
    assert report.imported == 2
    assert report.amount == Money("USD", 2_000_000)
    assert store.import_legacy_spend(str(ledger)).idempotent

    daily = _scope()
    repository = dataclasses.replace(daily, dimension="repository")
    global_scope = dataclasses.replace(
        daily, repository="*", dimension="global"
    )
    issue_scope = BudgetScope(
        "o/r",
        "issue:42",
        "USD",
        "lifetime:42",
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        datetime.max.replace(tzinfo=timezone.utc),
        ZoneInfo("UTC"),
    )
    assert store.snapshot(repository).settled == Money("USD", 2_000_000)
    assert store.snapshot(global_scope).settled == Money("USD", 2_000_000)
    assert store.snapshot(issue_scope).settled == Money("USD", 2_000_000)

    third = {**first, "cost_usd": 0.5, "model": "legacy-3"}
    ledger.write_text(
        "\n".join(json.dumps(row) for row in (third, first, second)) + "\n"
    )
    appended = store.import_legacy_spend(str(ledger))
    assert appended.imported == 1
    assert store.snapshot(repository).settled == Money("USD", 2_500_000)


def test_legacy_spend_import_uses_canonical_repository_scope(tmp_path):
    store = _store(tmp_path)
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "spend",
                "ts": NOW.isoformat(),
                "repo": " Acme/App ",
                "issue": 7,
                "cost_usd": 49,
                "model": "legacy",
            }
        )
        + "\n"
    )

    assert store.import_legacy_spend(str(ledger)).imported == 1

    scope = dataclasses.replace(
        _scope(), repository="acme/app", dimension="repository"
    )
    assert store.snapshot(scope).settled == Money("USD", 49_000_000)


def test_distinct_rotated_ledgers_keep_identical_semantic_spend_rows(
    tmp_path,
):
    store = _store(tmp_path)
    common = {
        "event": "spend",
        "ts": NOW.isoformat(),
        "repo": "o/r",
        "issue": 7,
        "cost_usd": 1.25,
        "model": "legacy",
    }
    first = tmp_path / "spend.1.jsonl"
    second = tmp_path / "spend.2.jsonl"
    first.write_text(
        "\n".join(
            json.dumps(row)
            for row in (dict(common, issue=8), common)
        )
        + "\n"
    )
    second.write_text(
        "\n".join(
            json.dumps(row)
            for row in (common, dict(common, issue=9))
        )
        + "\n"
    )

    assert store.import_legacy_spend(str(first)).imported == 2
    assert store.import_legacy_spend(str(second)).imported == 2

    scope = dataclasses.replace(_scope(), dimension="repository")
    assert store.snapshot(scope).settled == Money("USD", 5_000_000)


def test_byte_identical_rotated_ledgers_are_distinct_sources(tmp_path):
    store = _store(tmp_path)
    row = {
        "event": "spend",
        "ts": NOW.isoformat(),
        "repo": "o/r",
        "issue": 7,
        "cost_usd": 1.25,
        "model": "legacy",
    }
    first = tmp_path / "spend.1.jsonl"
    second = tmp_path / "spend.2.jsonl"
    content = json.dumps(row) + "\n"
    first.write_text(content)
    second.write_text(content)

    assert store.import_legacy_spend(str(first)).imported == 1
    assert store.import_legacy_spend(str(second)).imported == 1
    assert store.import_legacy_spend(str(first)).idempotent
    assert store.import_legacy_spend(str(second)).idempotent

    scope = dataclasses.replace(_scope(), dimension="repository")
    assert store.snapshot(scope).settled == Money("USD", 2_500_000)


def test_legacy_digest_only_receipt_remains_idempotent_for_same_source(
    tmp_path,
):
    store = _store(tmp_path)
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "spend",
                "ts": NOW.isoformat(),
                "repo": "o/r",
                "issue": 7,
                "cost_usd": 1.25,
            }
        )
        + "\n"
    )
    store.import_legacy_spend(str(ledger))
    legacy_id = "legacy-spend:" + hashlib.sha256(
        ledger.read_bytes()
    ).hexdigest()
    with store._connect() as db:
        db.execute(
            "UPDATE migration_imports SET migration_id=?",
            (legacy_id,),
        )

    repeated = store.import_legacy_spend(str(ledger))

    assert repeated.idempotent
    assert repeated.migration_id == legacy_id
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM migration_imports"
        ).fetchone()[0] == 1


def test_legacy_spend_import_rejects_unattributed_rows(tmp_path):
    store = _store(tmp_path)
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "spend",
                "ts": NOW.isoformat(),
                "repo": "o/r",
                "issue": None,
                "cost_usd": 1,
            }
        )
        + "\n"
    )

    with pytest.raises(LegacySpendMigrationError, match="no rows were imported"):
        store.import_legacy_spend(str(ledger))

    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM migration_imports"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM runs WHERE kind='legacy-import'"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("timestamp", "amount"),
    (
        (NOW.replace(tzinfo=None).isoformat(), "1"),
        (NOW.isoformat(), "-0.0000001"),
        (NOW.isoformat(), "NaN"),
        (NOW.isoformat(), "Infinity"),
    ),
)
def test_legacy_spend_import_rejects_invalid_authority_rows_atomically(
    tmp_path,
    timestamp,
    amount,
):
    store = _store(tmp_path)
    ledger = tmp_path / "spend.jsonl"
    valid = {
        "event": "spend",
        "ts": NOW.isoformat(),
        "repo": "o/r",
        "issue": 42,
        "cost_usd": 1,
    }
    invalid = {**valid, "ts": timestamp, "cost_usd": amount}
    ledger.write_text(
        "\n".join(json.dumps(row) for row in (valid, invalid)) + "\n"
    )

    with pytest.raises(LegacySpendMigrationError, match="no rows were imported"):
        store.import_legacy_spend(str(ledger))

    with sqlite3.connect(store.path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM runs WHERE kind='legacy-import'"
        ).fetchone() == (0,)


# ── cross-role observation of live claims (#176) ──────────────────────────────
def test_active_dispatch_claims_report_live_fix_targets(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.claim_dispatch(
        "o/r", "fix", 42, "ADDRESS_REVIEW", "owner-a", frozenset({"a.py"})
    )
    assert store.claim_dispatch(
        "o/r", "fix", -7, "FIX_CI", "owner-a", frozenset({"b.py"})
    )
    assert store.claim_dispatch(
        "o/r", "review", 99, "REVIEW", "owner-a", frozenset({"c.py"})
    )

    claims = store.active_dispatch_claims("o/r", "fix")

    assert {claim.target_key: claim.leg for claim in claims} == {
        42: "ADDRESS_REVIEW",
        -7: "FIX_CI",
    }
    assert all(claim.claimed_at == NOW for claim in claims)


def test_released_and_dead_claims_are_not_reported_as_active(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.claim_dispatch(
        "o/r", "fix", 42, "ADDRESS_REVIEW", "owner-a", frozenset({"a.py"})
    )
    assert store.claim_dispatch(
        "o/r", "fix", 43, "ADDRESS_REVIEW", "owner-a", frozenset({"b.py"})
    )
    assert store.release_dispatch_claim("o/r", "fix", 42, "owner-a")
    # A leaked row whose owner process is gone and which never bound a run: the
    # claim is dead, so review must not wait on it.
    with store._connect() as db:
        db.execute(
            "UPDATE dispatch_claims SET owner_pid=?, owner_start_ticks=? "
            "WHERE target='issue:43'",
            (2_000_000_000, 1),
        )

    assert store.active_dispatch_claims("o/r", "fix") == ()


def test_a_claim_with_an_unreadable_row_is_skipped(tmp_path) -> None:
    """Fail open: this reader only withholds paid work, so one corrupt row must
    not be able to starve review."""
    store = _store(tmp_path)
    assert store.claim_dispatch(
        "o/r", "fix", 42, "ADDRESS_REVIEW", "owner-a", frozenset({"a.py"})
    )
    assert store.claim_dispatch(
        "o/r", "fix", 43, "ADDRESS_REVIEW", "owner-a", frozenset({"b.py"})
    )
    with store._connect() as db:
        db.execute(
            "UPDATE dispatch_claims SET target='not-a-target' "
            "WHERE target='issue:42'"
        )
        db.execute(
            "UPDATE dispatch_claims SET claimed_at='whenever' "
            "WHERE target='issue:43'"
        )

    assert store.active_dispatch_claims("o/r", "fix") == ()


def _selected_review_claim_request(store, request):
    from issue_agent.domain import continuation_components as cc
    from issue_agent.domain.workflow_identity import WorkflowFamily, WorkflowKey, IssueRef, PullRequestRef
    from issue_agent.adapters.workflow_codec import encode_workflow
    from tests.fakes.workflow_authority import install_workflow, resolve_snapshot, with_issue_recovery
    from tests.fakes.review_workflow import held
    from tests.adapters.test_continuation_admission import active, install_secondary

    issue = with_issue_recovery(resolve_snapshot(42))
    install_workflow(store, issue)
    primary = dataclasses.replace(held(key=WorkflowKey("o/r", PullRequestRef(7))), gates=())
    component = active(primary.revision.fence.key, WorkflowFamily.PR_REPAIR)
    primary = dataclasses.replace(primary, pr_repair=component)
    install_workflow(store, primary)
    install_secondary(store, primary, component)
    selected_issue = cc.select_issue_recovery(issue)
    selected_pr = cc.select_pr_repair(primary)
    leg = "leg:bound-review"
    head, review_at = "a" * 40, "2026-10-02T00:00:01Z"
    return dataclasses.replace(request, workflow=selected_pr.binding, continuations=(
        cc.PRRepairPurchase(cc.ContinuationCapture(selected_pr.binding, selected_pr.revision, leg),
            allowance=cc.RepairAllowance.REVIEW, ceiling=3, branch="fix/42-claim", advance_resume=True),
        cc.IssueReviewPurchase(cc.ContinuationCapture(selected_issue.binding, selected_issue.revision, leg),
            PullRequestRef(7), head, review_at, 3)),
        dispatch_id=json.dumps({"repository": "o/r", "role": "fix", "kind": "ADDRESS_REVIEW",
            "number": 7, "issue": 42, "runtime": "claude", "leg_id": leg,
            "review_input": encode_workflow((head, review_at))}))


def test_a_claim_stays_active_and_keyed_the_same_once_a_run_binds(tmp_path) -> None:
    """A claim is taken at decision time and bound to a run later. Keying the
    observation on the run id changed it mid-hold, which double-journalled one
    deferral (#176); the key must survive binding."""
    store = _store(tmp_path)
    owner = "o/r:fix:checkout-a"
    run_id = RunId("bound-child")
    request = dataclasses.replace(
        _request(1),
        run_id=run_id,
        reservation_id=ReservationId("bound-child-reservation"),
        owner_id=owner,
        kind="ADDRESS_REVIEW",
    )
    request = _selected_review_claim_request(store, request)
    assert store.claim_dispatch(
        "o/r", "fix", 42, "ADDRESS_REVIEW", owner, frozenset({"a.py"})
    )
    before = store.active_dispatch_claims("o/r", "fix")

    assert store.reserve(request).admitted
    assert store.bind_dispatch_claim("o/r", "fix", 42, owner, run_id)
    after = store.active_dispatch_claims("o/r", "fix")

    assert [claim.claim_key for claim in before] == [
        claim.claim_key for claim in after
    ]


def test_a_dead_owner_with_a_live_bound_run_is_still_an_active_claim(
    tmp_path,
) -> None:
    """The cross-deployment case: the claiming daemon is gone but its child leg
    is still running and still pushing, so review must still wait on it."""
    store = _store(tmp_path)
    owner = "o/r:fix:checkout-a"
    run_id = RunId("bound-child")
    request = dataclasses.replace(
        _request(1),
        run_id=run_id,
        reservation_id=ReservationId("bound-child-reservation"),
        owner_id=owner,
        kind="ADDRESS_REVIEW",
    )
    request = _selected_review_claim_request(store, request)
    assert store.claim_dispatch(
        "o/r", "fix", 42, "ADDRESS_REVIEW", owner, frozenset({"a.py"})
    )
    assert store.reserve(request).admitted
    assert store.bind_dispatch_claim("o/r", "fix", 42, owner, run_id)
    with store._connect() as db:
        db.execute(
            "UPDATE dispatch_claims SET owner_pid=?, owner_start_ticks=?",
            (2_000_000_000, 1),
        )

    assert [
        claim.target_key for claim in store.active_dispatch_claims("o/r", "fix")
    ] == [42]

    # Once that run reaches a terminal state nothing keeps the claim alive.
    assert store.release(run_id, "spend_unknown").disposition.value == "available"
    assert store.active_dispatch_claims("o/r", "fix") == ()


def _seed_v24_workflow_history(store):
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.domain.review_workflow import ImportedBaseline, ReviewEpisode, ReviewedBaseline
    from issue_agent.domain.workflow import ResolveEpisode
    from issue_agent.domain.workflow_identity import FamilyAuthority, WorkflowFamily

    resolve = encode_workflow((FamilyAuthority(WorkflowFamily.RESOLVE, 2),
                               ResolveEpisode("resolve:legacy", 4, "prior failure", "resolve guidance")))
    review = encode_workflow((FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 3),
                              ReviewEpisode("review:legacy", "review guidance\nexact", ReviewedBaseline(
                                  "a" * 40, "c" * 64, 2, ImportedBaseline("import:review")))))
    with closing(sqlite3.connect(store.path)) as db, db:
        _historical_fixture(db, 24)
        for family, epoch in (("resolve", 2), ("review_rejections", 3)):
            db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r', ?, ?, 'source', 'inventory', ?)",
                       (family, epoch, NOW.isoformat()))
        for kind, number, payload in (("issue", 7, resolve), ("pr", 9, review)):
            db.execute("INSERT INTO workflows VALUES ('o/r', ?, ?, 2, 11, ?)", (kind, number, payload))
        db.execute("INSERT INTO dispatch_claims(repository,role,target,leg,owner_id,owner_pid,owner_start_ticks,paths_json,claimed_at) "
                   "VALUES ('o/r','review','pr:9','REVIEW','legacy-owner',123,456,'[]',?)", (NOW.isoformat(),))
    return resolve, review


def test_v25_backfills_only_validated_legacy_components_and_preserves_claim_occurrence_on_restart(tmp_path):
    store = _store(tmp_path)
    resolve, review = _seed_v24_workflow_history(store)
    reopened = SQLiteControlStore(store.path, now=lambda: NOW)
    reopened.initialize()
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT target_kind,generation,version,episode_json FROM workflows ORDER BY target_kind").fetchall()
        components = db.execute("SELECT target_kind,target_number,family,epoch,episode_id FROM workflow_components ORDER BY target_kind").fetchall()
        occurrence = db.execute("SELECT occurrence_id FROM dispatch_claims").fetchone()[0]
        imported_claim = json.loads(db.execute(
            "SELECT payload_json FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' AND scope='o/r' AND key=?",
            (f"{occurrence}:created",),
        ).fetchone()[0])
        assert imported_claim == {
            "role": "review", "target": "pr:9", "leg": "REVIEW", "owner_id": "legacy-owner",
            "owner_pid": 123, "owner_start_ticks": 456, "claimed_at": NOW.isoformat(), "source": "v24-migration",
        }
        assert db.execute("SELECT COUNT(*) FROM review_execution_rounds").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM workflow_authority_epochs WHERE family='review_execution'").fetchone()[0] == 0
        before = tuple(db.iterdump())
    assert rows == [("issue", 2, 11, resolve), ("pr", 2, 11, review)]
    assert components == [("issue", 7, "resolve", 2, "resolve:legacy"), ("pr", 9, "review_rejections", 3, "review:legacy")]
    assert isinstance(occurrence, str) and occurrence
    reopened.initialize()
    with sqlite3.connect(store.path) as db:
        assert tuple(db.iterdump()) == before
        assert db.execute("SELECT occurrence_id FROM dispatch_claims").fetchone()[0] == occurrence


@pytest.mark.parametrize("corruption", ["invalid_json", "wrong_target_family", "missing_epoch", "different_epoch"])
def test_v25_invalid_legacy_authority_rolls_back_schema_and_all_component_backfill(tmp_path, corruption):
    store = _store(tmp_path)
    resolve, _ = _seed_v24_workflow_history(store)
    with sqlite3.connect(store.path) as db:
        if corruption in {"invalid_json", "wrong_target_family"}:
            db.execute("UPDATE workflows SET episode_json=? WHERE target_kind='pr'",
                       ("not valid json" if corruption == "invalid_json" else resolve,))
        elif corruption == "missing_epoch":
            db.execute("DELETE FROM workflow_authority_epochs WHERE family='review_rejections'")
        else:
            db.execute("UPDATE workflow_authority_epochs SET epoch=4 WHERE family='review_rejections'")
        before = tuple(db.iterdump())
    with pytest.raises(IncompatibleSchema, match="historical workflow component"):
        SQLiteControlStore(store.path, now=lambda: NOW).initialize()
    with sqlite3.connect(store.path) as db:
        assert tuple(db.iterdump()) == before
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 24
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('workflow_components','review_execution_rounds')").fetchall()


def test_v25_interrupted_nonempty_backfill_restarts_without_partial_authority(tmp_path):
    store = _store(tmp_path)
    _seed_v24_workflow_history(store)
    with sqlite3.connect(store.path) as db:
        before = tuple(db.iterdump())

    def interrupt(version, phase):
        if version == 25 and phase == "before_commit":
            raise RuntimeError("interrupted v25 backfill")

    with pytest.raises(RuntimeError, match="interrupted v25"):
        SQLiteControlStore(store.path, now=lambda: NOW, migration_observer=interrupt).initialize()
    with sqlite3.connect(store.path) as db:
        assert tuple(db.iterdump()) == before
    SQLiteControlStore(store.path, now=lambda: NOW).initialize()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM workflow_components").fetchone()[0] == 2
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == EXPECTED_SCHEMA_VERSIONS[-1]


def test_a_v24_binary_refuses_v25_without_rewriting_authority(tmp_path, monkeypatch):
    import issue_agent.adapters.control_store as control_module

    store = _store(tmp_path)
    _seed_v24_workflow_history(store)
    SQLiteControlStore(store.path, now=lambda: NOW).initialize()
    with sqlite3.connect(store.path) as db:
        before = tuple(db.iterdump())
    monkeypatch.setattr(control_module, "SCHEMA_VERSION", 24)
    with pytest.raises(IncompatibleSchema, match="newer than 24"):
        SQLiteControlStore(store.path, now=lambda: NOW).initialize()
    with sqlite3.connect(store.path) as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("kind,number", [("issue", 7), ("pr", 9)])
@pytest.mark.parametrize("corruption", ["deleted", "epoch", "episode"])
def test_v25_component_index_corruption_refuses_authoritative_reads_without_repair(tmp_path, kind, number, corruption):
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_workflow_read
    from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowKey

    store = _store(tmp_path)
    _seed_v24_workflow_history(store)
    store.initialize()
    key = WorkflowKey("o/r", IssueRef(number) if kind == "issue" else PullRequestRef(number))
    with store.transaction() as db:
        assert load_workflow_read(db, key).episode.episode_id == f"{'resolve' if kind == 'issue' else 'review'}:legacy"
        if corruption == "deleted":
            db.execute("DELETE FROM workflow_components WHERE target_kind=?", (kind,))
        elif corruption == "epoch":
            db.execute("UPDATE workflow_components SET epoch=99 WHERE target_kind=?", (kind,))
        else:
            db.execute("UPDATE workflow_components SET episode_id='other-episode' WHERE target_kind=?", (kind,))
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="component index"):
            load_workflow_read(db, key)
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("retained", ["index", "activation", "both"])
def test_v25_inactive_review_cannot_load_with_retained_execution_authority(tmp_path, retained):
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_workflow_read
    from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey

    store = _store(tmp_path)
    _seed_v24_workflow_history(store)
    store.initialize()
    with store.transaction() as db:
        if retained in {"activation", "both"}:
            db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r','review_execution',5,'source','inventory',?)",
                       (NOW.isoformat(),))
        if retained in {"index", "both"}:
            db.execute("INSERT INTO workflow_components VALUES ('o/r','pr',9,'review_execution',5,'execution:legacy')")
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="execution"):
            load_workflow_read(db, WorkflowKey("o/r", PullRequestRef(9)))
        assert tuple(db.iterdump()) == before


def _activate_legacy_execution_fixture(db):
    from issue_agent.adapters.workflow_codec import encode_workflow
    from issue_agent.adapters.workflow_rows import (
        insert_imported_review_execution_origin, load_legacy_review_workflow_row,
    )
    from issue_agent.domain.review_execution import (
        ActivatedReviewExecution, ImportedReviewExecutionDecision,
        ImportedReviewExecutionOriginReceipt, ReviewExecutionBinding,
        ReviewExecutionCheckpoint, ReviewExecutionOrigin, ReviewExecutionOriginKind,
        ReviewExecutionPolicy, ReviewExecutionState,
    )
    from issue_agent.domain.review_workflow import ReviewFamilyEpisodes
    from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey

    key = WorkflowKey("o/r", PullRequestRef(9))
    prior = load_legacy_review_workflow_row(db, key)
    assert prior is not None
    state = ReviewExecutionState(ReviewExecutionBinding(prior.binding, 5, "execution:legacy"), failures=3, pin_deferrals=2)
    execution = ActivatedReviewExecution(ReviewExecutionOrigin(ReviewExecutionOriginKind.IMPORTED, "import:execution"),
                                         ReviewExecutionCheckpoint(0, state, ReviewExecutionPolicy(1)), state, 0)
    snapshot = dataclasses.replace(prior, execution=execution)
    db.execute("UPDATE workflows SET episode_json=? WHERE target_kind='pr'",
               (encode_workflow(ReviewFamilyEpisodes(prior.authority, prior.episode, execution)),))
    source_digest = hashlib.sha256(b"fixture:execution:sources").hexdigest()
    inventory_digest = hashlib.sha256(b"fixture:execution:inventory").hexdigest()
    plan_digest = hashlib.sha256(b"fixture:execution:plan").hexdigest()
    import_json = encode_workflow(ImportedReviewExecutionDecision(
        execution.origin, execution.checkpoint, plan_digest, source_digest,
    ))
    db.execute("INSERT INTO workflow_authority_epochs VALUES ('o/r','review_execution',5,?,?,?)",
               (source_digest, inventory_digest, NOW.isoformat()))
    db.execute("INSERT INTO workflow_activation_evidence VALUES ('o/r','review_execution',5,0,?)",
               (plan_digest,))
    db.execute("INSERT INTO workflow_components VALUES ('o/r','pr',9,'review_execution',5,'execution:legacy')")
    db.execute("INSERT INTO workflow_activation_imports VALUES ('o/r','review_execution',5,'pr',9,?,?)",
               (source_digest, import_json))
    insert_imported_review_execution_origin(
        db, "o/r", ImportedReviewExecutionOriginReceipt(
            execution.origin, execution.checkpoint, plan_digest, source_digest,
            inventory_digest, hashlib.sha256(import_json.encode()).hexdigest(),
        ), claimed_at=NOW,
    )
    assert load_legacy_review_workflow_row(db, key) == snapshot
    return snapshot


def _activate_execution_fixture(db):
    from issue_agent.adapters.workflow_rows import load_review_workflow_row, persist_workflow_snapshot
    from issue_agent.domain.review_execution import CurrentReviewExecution

    legacy = _activate_legacy_execution_fixture(db)
    current = dataclasses.replace(legacy, execution=CurrentReviewExecution(
        legacy.execution.origin, legacy.execution.checkpoint, legacy.execution.state, 0, 0,
    ))
    persist_workflow_snapshot(db, current)
    assert load_review_workflow_row(db, current.revision.fence.key) == current
    return current


@pytest.mark.parametrize("corruption", ["missing_index", "index_epoch", "index_episode", "missing_activation",
                                         "activation_epoch", "parent_generation", "missing_index_and_activation"])
def test_v25_active_execution_requires_matching_canonical_index_and_activation(tmp_path, corruption):
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row

    store = _store(tmp_path)
    _seed_v24_workflow_history(store)
    store.initialize()
    with store.transaction() as db:
        original = _activate_execution_fixture(db)
        if corruption in {"missing_index", "missing_index_and_activation"}:
            db.execute("DELETE FROM workflow_components WHERE family='review_execution'")
        if corruption in {"missing_activation", "missing_index_and_activation"}:
            db.execute("DELETE FROM workflow_activation_imports WHERE family='review_execution'")
            db.execute("DELETE FROM workflow_activation_evidence WHERE family='review_execution'")
            db.execute("DELETE FROM workflow_authority_epochs WHERE family='review_execution'")
        if corruption == "index_epoch":
            db.execute("UPDATE workflow_components SET epoch=6 WHERE family='review_execution'")
        elif corruption == "index_episode":
            db.execute("UPDATE workflow_components SET episode_id='other-execution' WHERE family='review_execution'")
        elif corruption == "activation_epoch":
            db.execute("UPDATE workflow_authority_epochs SET epoch=6 WHERE family='review_execution'")
        elif corruption == "parent_generation":
            db.execute("UPDATE workflows SET generation=3 WHERE target_kind='pr'")
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError):
            load_review_workflow_row(db, original.revision.fence.key)
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("corruption", (
    "missing_origin", "changed_origin", "oversized_origin", "aliased_origin", "aliased_activation",
    "aliased_activation_epoch",
    "malformed_claim", "different_claim", "changed_plan", "changed_import",
    "forged_decision", "missing_import", "foreign_episode",
))
def test_active_execution_requires_exact_imported_origin_sources(tmp_path, corruption):
    from issue_agent.adapters.workflow_codec import decode_workflow, encode_workflow
    from issue_agent.adapters.workflow_rows import WorkflowAuthorityError, load_review_workflow_row
    from issue_agent.domain.review_execution import (
        ImportedReviewExecutionDecision, ImportedReviewExecutionOriginReceipt,
    )

    store = _store(tmp_path)
    _seed_v24_workflow_history(store)
    store.initialize()
    with store.transaction() as db:
        snapshot = _activate_execution_fixture(db)
        if corruption == "missing_origin":
            db.execute("DELETE FROM dedupe_keys WHERE namespace='review-execution-origin'")
        elif corruption == "changed_origin":
            db.execute("UPDATE dedupe_keys SET payload_json='{}' WHERE namespace='review-execution-origin'")
        elif corruption == "oversized_origin":
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-origin'",
                       ("x" * 262145,))
        elif corruption == "aliased_origin":
            db.execute("INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) "
                       "SELECT CAST(namespace AS BLOB),scope,key,claimed_at,payload_json FROM dedupe_keys "
                       "WHERE namespace='review-execution-origin'")
        elif corruption == "aliased_activation":
            db.execute("INSERT INTO workflow_authority_epochs "
                       "SELECT CAST(repository AS BLOB),family,epoch,source_digest,inventory_digest,activated_at "
                       "FROM workflow_authority_epochs WHERE family='review_execution'")
        elif corruption == "aliased_activation_epoch":
            db.execute("INSERT INTO workflow_activation_evidence "
                       "SELECT repository,family,CAST(epoch AS BLOB),ceiling,plan_digest "
                       "FROM workflow_activation_evidence WHERE family='review_execution'")
        elif corruption == "malformed_claim":
            db.execute("UPDATE dedupe_keys SET claimed_at='invalid' WHERE namespace='review-execution-origin'")
        elif corruption == "different_claim":
            db.execute("UPDATE dedupe_keys SET claimed_at=? WHERE namespace='review-execution-origin'",
                       ((NOW + timedelta(seconds=1)).isoformat(),))
        elif corruption == "changed_plan":
            db.execute("UPDATE workflow_activation_evidence SET plan_digest=? WHERE family='review_execution'",
                       ("f" * 64,))
        elif corruption == "changed_import":
            db.execute("UPDATE workflow_activation_imports SET decision_json='{}' WHERE family='review_execution'")
        elif corruption == "forged_decision":
            raw = db.execute("SELECT decision_json FROM workflow_activation_imports "
                             "WHERE family='review_execution'").fetchone()["decision_json"]
            decision = decode_workflow(raw, ImportedReviewExecutionDecision)
            changed_decision = dataclasses.replace(
                decision, checkpoint=dataclasses.replace(
                    decision.checkpoint, state=dataclasses.replace(
                        decision.checkpoint.state, failures=decision.checkpoint.state.failures + 1,
                    ),
                ),
            )
            forged_json = encode_workflow(changed_decision)
            db.execute("UPDATE workflow_activation_imports SET decision_json=? "
                       "WHERE family='review_execution'", (forged_json,))
            receipt_row = db.execute("SELECT payload_json FROM dedupe_keys "
                                     "WHERE namespace='review-execution-origin'").fetchone()
            receipt = decode_workflow(receipt_row["payload_json"], ImportedReviewExecutionOriginReceipt)
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-origin'",
                       (encode_workflow(dataclasses.replace(
                           receipt, import_decision_digest=hashlib.sha256(forged_json.encode()).hexdigest(),
                       )),))
        elif corruption == "missing_import":
            db.execute("DELETE FROM workflow_activation_imports WHERE family='review_execution'")
        else:
            source = db.execute("SELECT payload_json FROM dedupe_keys "
                                "WHERE namespace='review-execution-origin'").fetchone()
            receipt = decode_workflow(source["payload_json"], ImportedReviewExecutionOriginReceipt)
            initial = receipt.checkpoint.state
            foreign = dataclasses.replace(
                initial.binding, execution_episode="execution:foreign",
            )
            changed = dataclasses.replace(
                receipt, checkpoint=dataclasses.replace(
                    receipt.checkpoint, state=dataclasses.replace(initial, binding=foreign),
                ),
            )
            db.execute("UPDATE dedupe_keys SET payload_json=? WHERE namespace='review-execution-origin'",
                       (encode_workflow(changed),))
        before = tuple(db.iterdump())
        with pytest.raises(WorkflowAuthorityError, match="origin"):
            load_review_workflow_row(db, snapshot.revision.fence.key)
        assert tuple(db.iterdump()) == before


def _claim_rows(store):
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        return tuple(dict(row) for row in db.execute("SELECT * FROM dispatch_claims ORDER BY repository,role,target"))


def _claim_receipts(store):
    with sqlite3.connect(store.path) as db:
        return {key: json.loads(payload) for key, payload in db.execute(
            "SELECT key,payload_json FROM dedupe_keys WHERE namespace='dispatch-claim-lifecycle' AND scope='o/r'")}


def test_dispatch_claim_occurrence_retains_create_and_normal_release_provenance_once(tmp_path):
    store = _store(tmp_path)
    assert store.claim_dispatch("o/r", "fix", 42, "RESOLVE", "owner", frozenset({"file.py"}))
    claim, = _claim_rows(store)
    occurrence = claim["occurrence_id"]
    created = _claim_receipts(store)[f"{occurrence}:created"]
    for field in ("role", "target", "leg", "owner_id", "owner_pid", "owner_start_ticks", "claimed_at"):
        assert created[field] == claim[field]
    assert created["source"] == "admission"
    assert store.active_dispatch_claims("o/r", "fix")[0].claim_key == occurrence
    assert not store.claim_dispatch("o/r", "fix", 42, "RESOLVE", "owner", frozenset({"file.py"}))
    assert _claim_rows(store) == (claim,)
    assert len(_claim_receipts(store)) == 1
    assert store.release_dispatch_claim("o/r", "fix", 42, "owner")
    assert _claim_rows(store) == ()
    retained = _claim_receipts(store)
    released = retained[f"{occurrence}:released"]
    assert released == {key: created[key] for key in created if key != "source"}
    assert not store.release_dispatch_claim("o/r", "fix", 42, "owner")
    assert _claim_receipts(store) == retained


def test_stale_dispatch_reclaim_retains_exact_old_occurrence_and_mints_new_one(tmp_path, monkeypatch):
    store = _store(tmp_path)
    assert store.claim_dispatch("o/r", "fix", 42, "RESOLVE", "owner", frozenset({"file.py"}))
    old, = _claim_rows(store)
    monkeypatch.setattr(ProcessSupervisor, "matches_identity", staticmethod(lambda _pid, _ticks: False))
    assert store.claim_dispatch("o/r", "fix", 42, "RESUME", "successor", frozenset({"file.py"}))
    new, = _claim_rows(store)
    assert old["occurrence_id"] != new["occurrence_id"]
    receipts = _claim_receipts(store)
    reclaimed = receipts[f"{old['occurrence_id']}:reclaimed"]
    for field in ("role", "target", "leg", "owner_id", "owner_pid", "owner_start_ticks", "claimed_at"):
        assert reclaimed[field] == old[field]
    assert receipts[f"{new['occurrence_id']}:created"]["owner_id"] == "successor"
    assert len(receipts) == 3


def test_same_owner_pid_and_timestamp_replacement_is_not_reclaimed_from_an_old_probe(tmp_path, monkeypatch):
    store = _store(tmp_path)
    assert store.claim_dispatch("o/r", "fix", 42, "RESOLVE", "owner", frozenset({"file.py"}))
    old, = _claim_rows(store)
    replacements = []

    def replace_during_probe(pid, start_ticks):
        assert not replacements
        assert (pid, start_ticks) == (old["owner_pid"], old["owner_start_ticks"])
        assert store.release_dispatch_claim("o/r", "fix", 42, "owner")
        assert store.claim_dispatch("o/r", "fix", 42, "RESOLVE", "owner", frozenset({"file.py"}))
        replacements.extend(_claim_rows(store))
        return False

    monkeypatch.setattr(ProcessSupervisor, "matches_identity", staticmethod(replace_during_probe))
    assert not store.claim_dispatch("o/r", "fix", 42, "RESOLVE", "competitor", frozenset({"file.py"}))
    replacement, = replacements
    assert replacement["occurrence_id"] != old["occurrence_id"]
    assert {key: value for key, value in replacement.items() if key != "occurrence_id"} == {
        key: value for key, value in old.items() if key != "occurrence_id"}
    assert _claim_rows(store) == (replacement,)
    receipts = _claim_receipts(store)
    assert set(receipts) == {f"{old['occurrence_id']}:created", f"{old['occurrence_id']}:released",
                             f"{replacement['occurrence_id']}:created"}


@pytest.mark.parametrize("event", ["created", "released", "reclaimed"])
def test_claim_lifecycle_receipt_conflict_rolls_back_its_entire_authority_change(tmp_path, monkeypatch, event):
    import uuid
    import issue_agent.adapters.control_store as control_module

    store = _store(tmp_path)
    if event == "created":
        identity = uuid.UUID("00000000-0000-0000-0000-000000000001")
        monkeypatch.setattr(control_module.uuid, "uuid4", lambda: identity)
        occurrence = identity.hex
    else:
        assert store.claim_dispatch("o/r", "fix", 42, "RESOLVE", "owner", frozenset({"file.py"}))
        occurrence = _claim_rows(store)[0]["occurrence_id"]
    assert store.receipt("dispatch-claim-lifecycle", "o/r", f"{occurrence}:{event}", {"conflicting": "accepted fact"})
    with sqlite3.connect(store.path) as db:
        before = tuple(db.iterdump())
    if event == "reclaimed":
        monkeypatch.setattr(ProcessSupervisor, "matches_identity", staticmethod(lambda _pid, _ticks: False))
    with pytest.raises(sqlite3.IntegrityError):
        if event == "released":
            store.release_dispatch_claim("o/r", "fix", 42, "owner")
        else:
            store.claim_dispatch("o/r", "fix", 42, "RESOLVE", "successor", frozenset({"file.py"}))
    with sqlite3.connect(store.path) as db:
        assert tuple(db.iterdump()) == before


def _retained_schema_rows(db):
    from issue_agent.adapters.workflow_codec import encode_workflow
    retired = {'review_archive_identity_members', 'review_archive_identity_certifications',
               'review_archive_source_versions', 'schema_migrations'}
    tables = tuple(row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    ) if row[0] not in retired)
    rows = {}
    for table in tables:
        columns = [row[1] for row in db.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')]
        if table == 'runs' and 'continuations_json' in columns:
            assert not db.execute("SELECT 1 FROM runs WHERE continuations_json<>? LIMIT 1", (encode_workflow(()),)).fetchone()
            columns.remove('continuations_json')
        selection = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
        rows[table] = tuple(sorted((tuple(row) for row in db.execute(
            'SELECT ' + selection + ' FROM "' + table.replace('"', '""') + '"',
        )), key=repr))
    return rows


def _populated_schema32_copy(tmp_path):
    from tests.adapters.test_control_store_connection_lifecycle import _authority_bytes, _backup_and_collect

    source = _store(tmp_path)
    first, active = _request(1, cap=200), _request(2, cap=200)
    assert source.reserve(first).admitted and source.reserve(active).admitted
    payment = SettlementRequest(first.run_id, Money('USD', 25), PricingBasis.PROVIDER_REPORTED,
                                's6-paid', None, None, None)
    assert source.settle(payment).disposition is AccountingDisposition.AVAILABLE
    command = LifecycleCommand(CommandId('s6-command'), 's6-effect', CommandKind.POST_COMMENT,
                               'issue/1', 'head', {'body': 'done'}, repository='o/r', executor_identity='o/r:fix')
    source.enqueue_command(command)
    source.begin_command(command.command_id)
    effect = EffectReceipt(command.command_id, 'broker', NOW, NOW, EffectStatus.SUCCEEDED,
                           remote_object_id='comment:s6')
    assert source.record_effect(effect)
    _seed_v24_workflow_history(source)
    source.initialize()
    with source.transaction() as db:
        _historical_fixture(db, 32)
    before = _authority_bytes(source)
    copy = tmp_path / 'schema32-copy.db'
    _backup_and_collect(source, copy)
    copied = SQLiteControlStore(str(copy), now=lambda: NOW)
    Path(copied.authority_witness_path).write_bytes(Path(source.authority_witness_path).read_bytes())
    return source, copied, before, active, payment, command, effect


def test_v34_preserves_predecessor_migration_sources_and_digests():
    from issue_agent.adapters.control_store import _SCHEMA_MIGRATIONS

    # Pinned at accepted S4/S5 d7ef565 before the forward-only retirement.
    encoded = json.dumps(tuple((migration.version, migration.source, migration.transform_spec, migration.digest)
                               for migration in _SCHEMA_MIGRATIONS if migration.version <= 33),
                         ensure_ascii=True, separators=(',', ':')).encode()
    assert hashlib.sha256(encoded).hexdigest() == '03bf29fd7238a15690a0ef185adcff55b4d2ec81104cc6eb999ecd90a93201a0'


def test_v34_upgrades_populated_schema32_copy_preserving_money_effects_binding_and_retry(tmp_path):
    from tests.adapters.test_control_store_connection_lifecycle import _authority_bytes

    source, copy, original, active, payment, command, effect = _populated_schema32_copy(tmp_path)
    with copy.transaction() as db:
        before = _retained_schema_rows(db)
        history = tuple(db.execute('SELECT version,source_digest FROM schema_migrations ORDER BY version'))
    copy.initialize()
    with copy.transaction() as db:
        assert _retained_schema_rows(db) == before
        assert tuple(tuple(row) for row in db.execute(
            'SELECT version,source_digest FROM schema_migrations WHERE version<=32 ORDER BY version',
        )) == tuple(tuple(row) for row in history)
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == EXPECTED_SCHEMA_VERSIONS[-1]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'review_archive_%'").fetchall()
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE '%review_archive_source_versions%'").fetchall()
    assert copy.reserve(active).admitted
    assert copy.settle(payment).idempotent
    assert copy.enqueue_command(command).state == 'succeeded'
    assert copy.record_effect(effect)
    assert copy.snapshot(_scope()).settled == Money('USD', 25)
    assert copy.snapshot(_scope()).committed == Money('USD', 85)
    assert copy.snapshot(_scope()).reserved == Money('USD', 60)
    with copy.transaction() as db:
        assert _retained_schema_rows(db) == before
    assert _authority_bytes(source) == original


def test_v34_failure_rolls_back_retirement_and_reopens_previous_coherent_schema(tmp_path):
    source, copy, original, *_ = _populated_schema32_copy(tmp_path)
    with copy.transaction() as db:
        before = _retained_schema_rows(db)
        prior_triggers = tuple(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name",
        ))

    def interrupt(version, stage):
        if version == 34 and stage == 'before_commit':
            raise RuntimeError('retirement interrupted')

    with pytest.raises(RuntimeError, match='retirement interrupted'):
        SQLiteControlStore(copy.path, now=lambda: NOW, migration_observer=interrupt).initialize()
    with copy.transaction() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 33
        assert _retained_schema_rows(db) == before
        assert tuple(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name",
        )) == prior_triggers
    SQLiteControlStore(copy.path, now=lambda: NOW).initialize()
    with copy.transaction() as db:
        assert _retained_schema_rows(db) == before
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == EXPECTED_SCHEMA_VERSIONS[-1]
    from tests.adapters.test_control_store_connection_lifecycle import _authority_bytes
    assert _authority_bytes(source) == original


def test_v33_binary_refuses_new_schema_without_mutating_authority(tmp_path, monkeypatch):
    import issue_agent.adapters.control_store as control_module

    store = _store(tmp_path)
    with store.transaction() as db:
        before = tuple(db.iterdump())
    monkeypatch.setattr(control_module, 'SCHEMA_VERSION', 33)
    with pytest.raises(IncompatibleSchema, match='newer than 33'):
        SQLiteControlStore(store.path, now=lambda: NOW).initialize()
    with store.transaction() as db:
        assert tuple(db.iterdump()) == before


def test_v34_unrelated_writes_need_no_retired_projection_witness(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as db:
        remaining = tuple(row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name",
        ))
        assert remaining == ('dispatch_claims_occurrence_insert', 'dispatch_claims_occurrence_update',
                             'runs_continuation_identity_update', 'runs_continuation_selections_update',
                             'runs_require_workflow_episode_insert', 'runs_require_workflow_episode_update',
                             'runs_review_attachment_insert', 'runs_review_attachment_update',
                             'workflow_retry_grant_insert', 'workflow_retry_grant_update')
    assert store.claim_dispatch('o/r', 'review', -9, 'REVIEW', 's6-owner', frozenset())
    with pytest.raises(sqlite3.IntegrityError, match='occurrence is immutable'):
        with store.transaction() as db:
            db.execute("UPDATE dispatch_claims SET occurrence_id='changed' WHERE repository='o/r'")
    assert store.reserve(_request(1)).admitted
    assert store.claim('unrelated', 'other/repository', 'key')
    assert store.release(RunId('run-1'), 'cancelled').disposition is AccountingDisposition.AVAILABLE
    store.integrity_check()
