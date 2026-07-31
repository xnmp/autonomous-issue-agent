from __future__ import annotations

import dataclasses
import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    SQLiteControlStore,
    SettlementRequest,
    read_settled_spend,
)
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
    assert history == [
        (9, 64), (10, 64), (11, 64), (12, 64), (13, 64), (14, 64)
    ]


def test_established_control_store_disappearance_is_not_fresh_initialized(
    tmp_path,
):
    store = _store(tmp_path)
    assert store.reserve(_request(1)).admitted
    Path(store.path).unlink()

    with pytest.raises(CorruptControlStore, match="disappeared"):
        SQLiteControlStore(store.path).initialize()

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
        db.execute("DROP TABLE dispatch_claim_runs")
        db.execute("DELETE FROM schema_migrations WHERE version=14")
        db.execute("DELETE FROM schema_migrations WHERE version=13")
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
        db.execute("DROP TABLE dispatch_claim_runs")
        db.execute("DELETE FROM schema_migrations WHERE version=14")
        db.execute("DELETE FROM schema_migrations WHERE version=13")
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
        db.execute("DROP TABLE dispatch_claim_runs")
        db.execute("DROP TABLE run_cancellation_intents")
        db.execute("DROP TABLE level_state")
        db.execute("DROP TABLE dispatch_claims")
        db.execute("DELETE FROM schema_migrations")
        db.execute(
            "INSERT INTO schema_migrations("
            "version, applied_at, source_digest) VALUES (8, ?, ?)",
            (NOW.isoformat(), LEGACY_V8_SCHEMA_DIGEST),
        )
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
        level_state = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='level_state'"
        ).fetchone()
    assert [version for version, _digest in history] == [
        8,
        9,
        10,
        11,
        12,
        13,
        14,
    ]
    assert history[0][1] == (
        "1740c6b03aae7ba5beccea7dd522d0859396e901dee6a40d12ff4284621745b5"
    )
    assert reserved == ("active",)
    assert level_state == ("level_state",)


@pytest.mark.parametrize("starting_version", ("fresh", "v8", "v9"))
def test_concurrent_role_boot_serializes_schema_migrations(
    tmp_path,
    starting_version,
):
    path = tmp_path / f"concurrent-{starting_version}.db"
    if starting_version != "fresh":
        SQLiteControlStore(str(path)).initialize()
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE dispatch_claim_runs")
            db.execute("DELETE FROM schema_migrations WHERE version=14")
            db.execute("DELETE FROM schema_migrations WHERE version=13")
            db.execute("DELETE FROM schema_migrations WHERE version=12")
            db.execute("DROP TABLE dispatch_claims")
            db.execute("DELETE FROM schema_migrations WHERE version=11")
            db.execute("DROP TABLE run_cancellation_intents")
            db.execute("DELETE FROM schema_migrations WHERE version=10")
            if starting_version == "v8":
                db.execute("DROP TABLE level_state")
                db.execute("DELETE FROM schema_migrations")
                db.execute(
                    "INSERT INTO schema_migrations("
                    "version, applied_at, source_digest) VALUES (8, ?, ?)",
                    (NOW.isoformat(), LEGACY_V8_SCHEMA_DIGEST),
                )
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
            (8, 9, 10, 11, 12, 13, 14)
            if starting_version == "v8"
            else (9, 10, 11, 12, 13, 14)
        )


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


@pytest.mark.parametrize("version", (9, 10, 11, 12, 13, 14))
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
    assert versions == (9, 10, 11, 12, 13, 14)
    assert cancellation_table == ("run_cancellation_intents",)


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
        pid=123,
        pgid=124,
        process_start_ticks=99,
    ) == lease
    with pytest.raises(ControlStoreError, match="conflicting"):
        store.register_process_lease(
            request.run_id,
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

    assert summary.total == Money("USD", 65)
    assert summary.for_repository("O/R") == Money("USD", 25)
    assert summary.for_repository("o/other") == Money("USD", 40)
    assert summary.for_repository("o/absent") == Money.zero("USD")


def test_missing_control_store_reads_as_zero_without_creating_it(tmp_path) -> None:
    path = tmp_path / "absent" / "control.db"

    summary = read_settled_spend(
        str(path),
        NOW.replace(hour=0),
        NOW.replace(hour=0) + timedelta(days=1),
    )

    assert summary.total == Money.zero("USD")
    assert summary.by_repository == {}
    assert not path.exists()


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


def test_oversized_event_rolls_back_and_cannot_poison_later_outbox_rows(
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
    with pytest.raises(ValueError, match="outbox event line"):
        store.record_world_observation(
            deployment="deployment",
            subject_kind="issue",
            subject_number=1,
            content_hash="large",
            facts={"body": "x" * 1_000},
            correlation=Correlation(issue=1),
        )

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
