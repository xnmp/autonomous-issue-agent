"""SQLite operational authority for one deployment.

The store owns reservations, run/lease state, deduplication, lifecycle commands,
effect receipts, and the event outbox.  Transactions are intentionally short:
no network, model, or subprocess work is accepted by this adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any

from ..domain.contracts import (
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
from ..domain.events import (
    ActionApplied,
    ActionFailed,
    BudgetVarianceDetected,
    ControlTransitionCommitted,
    Correlation,
    EventEnvelope,
    EventPayload,
    JsonValue,
    MAX_EVENT_LINE_BYTES,
    SpendRecorded,
    WorldObserved,
    decode_event_line,
    encode_event_line,
)

SCHEMA_VERSION = 12
SCHEMA_BASELINE_VERSION = 9
LEGACY_V8_SCHEMA_DIGEST = (
    "1740c6b03aae7ba5beccea7dd522d0859396e901dee6a40d12ff4284621745b5"
)
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class ControlStoreError(RuntimeError):
    pass


class IncompatibleSchema(ControlStoreError):
    pass


class CorruptControlStore(ControlStoreError):
    pass


class LegacySpendMigrationError(ControlStoreError):
    pass


class AccountingDisposition(str, Enum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class AccountingSnapshot:
    disposition: AccountingDisposition
    scope: BudgetScope
    settled: Money
    reserved: Money
    detail: str = ""

    @property
    def committed(self) -> Money:
        return self.settled + self.reserved


@dataclass(frozen=True)
class ReservationRequest:
    reservation_id: ReservationId
    run_id: RunId
    dispatch_id: str
    scope: BudgetScope
    quote: PriceQuote
    cap: Money
    config_version: str
    additional_limits: tuple["BudgetLimit", ...] = ()
    kind: str = "paid"
    owner_id: str = "reservation"

    def __post_init__(self) -> None:
        if self.scope.currency != self.quote.maximum_charge.currency:
            raise ValueError("reservation scope and quote currency differ")
        if self.cap.currency != self.scope.currency:
            raise ValueError("reservation cap and scope currency differ")
        if not str(self.reservation_id) or not str(self.run_id):
            raise ValueError("reservation and run identities are required")
        if not self.config_version or not self.kind or not self.owner_id:
            raise ValueError(
                "configuration version, run kind, and owner identity are required"
            )
        for limit in self.additional_limits:
            if limit.scope.currency != self.scope.currency:
                raise ValueError("all reservation limits must use one currency")


@dataclass(frozen=True)
class BudgetLimit:
    """One independently enforced ceiling attached to a reservation."""

    scope: BudgetScope
    cap: Money

    def __post_init__(self) -> None:
        if self.scope.currency != self.cap.currency:
            raise ValueError("budget limit scope and cap currency differ")


@dataclass(frozen=True)
class ReservationResult:
    disposition: AccountingDisposition
    reservation_id: ReservationId | None
    committed: Money
    requested: Money
    cap: Money
    detail: str = ""

    @property
    def admitted(self) -> bool:
        return self.disposition is AccountingDisposition.AVAILABLE


@dataclass(frozen=True)
class SettlementRequest:
    run_id: RunId
    amount: Money
    basis: PricingBasis
    provider_receipt_id: str
    usage: TokenUsage | None
    rate_card_id: str | None
    rate_card_version: str | None

    def __post_init__(self) -> None:
        if not str(self.run_id) or not self.provider_receipt_id:
            raise ValueError("run and provider receipt identities are required")
        if (
            self.basis is PricingBasis.CONFIGURED_RATE_CARD
            and (not self.rate_card_id or not self.rate_card_version)
        ):
            raise ValueError("configured charges require rate-card provenance")


@dataclass(frozen=True)
class SettlementResult:
    disposition: AccountingDisposition
    amount: Money
    idempotent: bool = False
    variance: Money | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReleaseResult:
    disposition: AccountingDisposition
    run_id: RunId
    idempotent: bool = False
    detail: str = ""


@dataclass(frozen=True)
class Lease:
    run_id: RunId
    dispatch_id: str
    owner_id: str
    pid: int
    pgid: int
    started_at: datetime
    process_start_ticks: int = 0


@dataclass(frozen=True)
class PublicationAuthority:
    run_id: RunId
    repository: str
    owner_id: str
    pgid: int
    process_start_ticks: int


@dataclass(frozen=True)
class RunCancellationIntent:
    run_id: RunId
    dispatch_id: str
    kind: str
    issue_number: int | None
    reason: str


@dataclass(frozen=True)
class PendingCommand:
    command: LifecycleCommand
    state: str
    attempts: int


@dataclass(frozen=True)
class MigrationReport:
    migration_id: str
    source_digest: str
    imported: int
    rejected: int
    amount: Money
    idempotent: bool = False


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    source: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()


_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    source_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('reserved','starting','running','completed','cancelled','abandoned')),
    config_version TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    outcome TEXT,
    provider_receipt_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS leases (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    dispatch_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    pid INTEGER NOT NULL CHECK (pid > 0),
    pgid INTEGER NOT NULL CHECK (pgid > 0),
    process_start_ticks INTEGER NOT NULL DEFAULT 0
        CHECK (process_start_ticks >= 0),
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dedupe_keys (
    namespace TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    reusable_after TEXT,
    payload_json TEXT,
    PRIMARY KEY (namespace, scope, key)
);

CREATE TABLE IF NOT EXISTS level_state (
    namespace TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, scope, key)
);

CREATE TABLE IF NOT EXISTS lifecycle_commands (
    command_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    logical_key TEXT NOT NULL,
    occurrence INTEGER NOT NULL DEFAULT 0 CHECK (occurrence >= 0),
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    expected_revision TEXT,
    config_version TEXT NOT NULL DEFAULT '',
    repository TEXT NOT NULL DEFAULT '',
    executor_identity TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('pending','executing','succeeded','retryable_failure',
         'permanent_failure','unknown_outcome')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS effect_receipts (
    command_id TEXT PRIMARY KEY
        REFERENCES lifecycle_commands(command_id) ON DELETE RESTRICT,
    executor_identity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('succeeded','retryable_failure','permanent_failure','unknown_outcome')),
    remote_object_id TEXT,
    remote_revision TEXT,
    error TEXT,
    receipt_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
    repository TEXT NOT NULL,
    dimension TEXT NOT NULL,
    period_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_timezone TEXT NOT NULL,
    currency TEXT NOT NULL,
    maximum_micros INTEGER NOT NULL CHECK (maximum_micros >= 0),
    cap_micros INTEGER NOT NULL CHECK (cap_micros >= 0),
    model TEXT NOT NULL,
    rate_card_id TEXT NOT NULL,
    rate_card_version TEXT NOT NULL,
    quote_effective_at TEXT NOT NULL,
    config_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active','settled','released')),
    release_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reservations_scope_state
ON budget_reservations(repository, dimension, period_id, currency, state);

CREATE TABLE IF NOT EXISTS reservation_limits (
    run_id TEXT NOT NULL REFERENCES budget_reservations(run_id) ON DELETE RESTRICT,
    repository TEXT NOT NULL,
    dimension TEXT NOT NULL,
    period_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_timezone TEXT NOT NULL,
    currency TEXT NOT NULL,
    cap_micros INTEGER NOT NULL CHECK (cap_micros >= 0),
    PRIMARY KEY (run_id, repository, dimension, period_id, currency)
);

CREATE INDEX IF NOT EXISTS reservation_limits_scope
ON reservation_limits(repository, dimension, period_id, currency);

CREATE TABLE IF NOT EXISTS charges (
    charge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) ON DELETE RESTRICT,
    provider_receipt_id TEXT NOT NULL UNIQUE,
    amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0),
    currency TEXT NOT NULL,
    basis TEXT NOT NULL,
    usage_json TEXT,
    rate_card_id TEXT,
    rate_card_version TEXT,
    settled_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS charges_scope
ON charges(run_id, currency);

CREATE TABLE IF NOT EXISTS quota_debits (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    quota TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units >= 0),
    calculation_version TEXT NOT NULL,
    PRIMARY KEY (run_id, quota)
);

CREATE TABLE IF NOT EXISTS event_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    destination TEXT NOT NULL DEFAULT '',
    event_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    emitted_at TEXT,
    failed_at TEXT,
    failure TEXT
);

CREATE TABLE IF NOT EXISTS world_observations (
    deployment TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_number INTEGER NOT NULL CHECK (subject_number > 0),
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (deployment, subject_kind, subject_number)
);

CREATE TABLE IF NOT EXISTS migration_imports (
    migration_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    amount_micros INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
"""

_MIGRATION_V10 = """
CREATE TABLE run_cancellation_intents (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE RESTRICT,
    dispatch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    issue_number INTEGER CHECK (issue_number IS NULL OR issue_number > 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    handover_written_at TEXT
);
"""

_MIGRATION_V11 = """
CREATE TABLE dispatch_claims (
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    target TEXT NOT NULL,
    leg TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_start_ticks INTEGER NOT NULL CHECK (owner_start_ticks > 0),
    paths_json TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (repository, role, target)
);

CREATE UNIQUE INDEX runs_active_dispatch_unique
ON runs(dispatch_id)
WHERE state IN ('reserved', 'starting', 'running');
"""

_MIGRATION_V12 = """
DROP INDEX IF EXISTS runs_active_dispatch_unique;
"""

_SCHEMA_MIGRATIONS = (
    # Version 9 is the immutable adoption baseline shipped by the architectural
    # hardening cutover. Its exact source digest already exists in deployed
    # databases, so future code validates rather than rewriting that history.
    SchemaMigration(SCHEMA_BASELINE_VERSION, _DDL),
    SchemaMigration(10, _MIGRATION_V10),
    SchemaMigration(11, _MIGRATION_V11),
    SchemaMigration(12, _MIGRATION_V12),
)

if tuple(migration.version for migration in _SCHEMA_MIGRATIONS) != tuple(
    range(SCHEMA_BASELINE_VERSION, SCHEMA_VERSION + 1)
):
    raise RuntimeError("control-store migrations must be contiguous")

_KNOWN_SCHEMA_DIGESTS = {
    8: LEGACY_V8_SCHEMA_DIGEST,
    **{
        migration.version: migration.digest
        for migration in _SCHEMA_MIGRATIONS
    },
}


class SQLiteControlStore:
    """One SQLite database, safe for threads and cooperating daemon processes."""

    def __init__(
        self,
        path: str,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        now: Callable[[], datetime] | None = None,
        migration_observer: Callable[[int, str], None] | None = None,
    ):
        if not path:
            raise ValueError("control-store path is required")
        if busy_timeout_ms <= 0:
            raise ValueError("busy timeout must be positive")
        self.path = os.path.abspath(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._migration_observer = migration_observer
        self._event_factory: (
            Callable[[EventPayload, Correlation], EventEnvelope] | None
        ) = None
        self._event_sink: Callable[[EventEnvelope], None] | None = None
        self._event_destination: str | None = None
        self._event_line_limit = MAX_EVENT_LINE_BYTES
        self._flushing_outbox = False

    def bind_event_outbox(
        self,
        factory: Callable[[EventPayload, Correlation], EventEnvelope],
        sink: Callable[[EventEnvelope], None],
        *,
        line_limit: int = MAX_EVENT_LINE_BYTES,
        destination: str | None = None,
    ) -> None:
        """Bind telemetry publication after store initialization.

        State commits do not depend on the sink. A failed publication leaves its
        row pending and a later mutation/startup retries it.
        """

        if line_limit <= 0:
            raise ValueError("outbox event line limit must be positive")
        self._event_factory = factory
        self._event_sink = sink
        self._event_line_limit = line_limit
        self._event_destination = destination
        self._flush_bound_outbox()

    def _transition(
        self,
        db: sqlite3.Connection,
        entity_kind: str,
        entity_id: str,
        state: str,
        detail: Mapping[str, JsonValue] | None = None,
        *,
        correlation: Correlation = Correlation(),
    ) -> None:
        if self._event_factory is None:
            return
        payload = ControlTransitionCommitted(
            entity_kind,
            entity_id,
            state,
            dict(detail or {}),
        )
        self.enqueue_event(self._event_factory(payload, correlation), db)

    def _event(
        self,
        db: sqlite3.Connection,
        payload: EventPayload,
        *,
        correlation: Correlation = Correlation(),
    ) -> None:
        if self._event_factory is not None:
            self.enqueue_event(self._event_factory(payload, correlation), db)

    def _flush_bound_outbox(self) -> int:
        if self._event_sink is None or self._flushing_outbox:
            return 0
        self._flushing_outbox = True
        try:
            return self.flush_outbox(
                self._event_sink, destination=self._event_destination
            )
        except Exception:
            return 0
        finally:
            self._flushing_outbox = False

    def flush_bound_outbox(self) -> int:
        """Retry every committed event for this role's bound destination."""

        return self._flush_bound_outbox()

    def initialize(self) -> None:
        parent = os.path.dirname(self.path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, stat.S_IRWXU)
        if os.path.exists(self.path) and os.path.getsize(self.path):
            self.integrity_check()
        with self._connect() as db:
            applied = self._migration_history(db)
        self._validate_migration_history(applied)
        for migration in _SCHEMA_MIGRATIONS:
            self._apply_schema_migration(migration)
        with self._connect() as db:
            final_history = self._migration_history(db)
        self._validate_migration_history(final_history, require_current=True)
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        self.integrity_check()

    def _validate_migration_history(
        self,
        history: tuple[tuple[int, str], ...],
        *,
        require_current: bool = False,
    ) -> None:
        if not history:
            if require_current:
                raise IncompatibleSchema("control-store migration history is empty")
            return
        versions = tuple(version for version, _digest in history)
        if versions[0] not in {8, SCHEMA_BASELINE_VERSION} or versions != tuple(
            range(versions[0], versions[-1] + 1)
        ):
            raise IncompatibleSchema(
                "control-store migration history is not contiguous from "
                f"a supported baseline (8 or {SCHEMA_BASELINE_VERSION}): "
                f"{versions}"
            )
        if versions[-1] > SCHEMA_VERSION:
            raise IncompatibleSchema(
                f"control store schema {versions[-1]} is newer than "
                f"{SCHEMA_VERSION}"
            )
        for version, digest in history:
            if _KNOWN_SCHEMA_DIGESTS.get(version) != digest:
                raise IncompatibleSchema(
                    f"control-store migration {version} digest mismatch"
                )
        if require_current and versions[-1] != SCHEMA_VERSION:
            raise IncompatibleSchema(
                f"control store stopped at schema {versions[-1]}, expected "
                f"{SCHEMA_VERSION}"
            )

    @staticmethod
    def _migration_history(
        db: sqlite3.Connection,
    ) -> tuple[tuple[int, str], ...]:
        existing = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone()
        if existing is None:
            return ()
        return tuple(
            (int(row["version"]), str(row["source_digest"]))
            for row in db.execute(
                "SELECT version, source_digest FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        )

    def _apply_schema_migration(self, migration: SchemaMigration) -> None:
        applied = False
        with self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                history = self._migration_history(db)
                self._validate_migration_history(history)
                if migration.version in {
                    version for version, _digest in history
                }:
                    db.commit()
                    return
                for statement in _sql_statements(migration.source):
                    db.execute(statement)
                db.execute(
                    "INSERT INTO schema_migrations("
                    "version, applied_at, source_digest) VALUES (?, ?, ?)",
                    (
                        migration.version,
                        _stamp(self._now()),
                        migration.digest,
                    ),
                )
                if self._migration_observer is not None:
                    self._migration_observer(
                        migration.version,
                        "before_commit",
                    )
                applied = True
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        if applied and self._migration_observer is not None:
            self._migration_observer(migration.version, "committed")

    def integrity_check(self) -> None:
        try:
            with self._connect() as db:
                result = db.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise CorruptControlStore(f"control-store integrity check failed: {exc}") from exc
        if result != "ok":
            raise CorruptControlStore(f"control-store integrity check failed: {result}")

    def adopt_legacy_owner(
        self,
        repository: str,
        role: str,
        owner_id: str,
    ) -> tuple[int, int]:
        """Atomically move pre-deployment ownership into the first upgrader.

        Older schemas scoped all same-repository role daemons as
        ``<repository>:<role>``. The first deployment to boot after the
        deployment-identity cutover adopts that legacy scope, preserving active
        run recovery and reconcile-before-retry effect semantics.
        """

        legacy = f"{repository}:{role}"
        if not repository or not role or not owner_id:
            raise ValueError("legacy owner adoption identity is incomplete")
        if owner_id == legacy:
            return (0, 0)
        with self.transaction() as db:
            runs = db.execute(
                "UPDATE runs SET owner_id=? WHERE owner_id=?",
                (owner_id, legacy),
            ).rowcount
            db.execute(
                "UPDATE leases SET owner_id=? WHERE owner_id=?",
                (owner_id, legacy),
            )
            commands = db.execute(
                "UPDATE lifecycle_commands SET executor_identity=? "
                "WHERE repository=? AND executor_identity=? "
                "AND state IN ('pending','executing','retryable_failure',"
                "'unknown_outcome')",
                (owner_id, repository, legacy),
            ).rowcount
            return (runs, commands)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
                self._flush_bound_outbox()

    def start_run(
        self,
        run_id: RunId,
        dispatch_id: str,
        kind: str,
        owner_id: str,
        config_version: str,
        *,
        state: str = "starting",
    ) -> None:
        now = _stamp(self._now())
        with self.transaction() as db:
            db.execute(
                "INSERT INTO runs(run_id, dispatch_id, kind, owner_id, state, "
                "config_version, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(run_id),
                    dispatch_id,
                    kind,
                    owner_id,
                    state,
                    config_version,
                    now,
                ),
            )
            self._transition(
                db,
                "run",
                str(run_id),
                state,
                {"dispatch_id": dispatch_id, "kind": kind, "owner_id": owner_id},
                correlation=Correlation(
                    dispatch_id=dispatch_id,
                    run_id=str(run_id),
                ),
            )

    def complete_run(
        self,
        run_id: RunId,
        *,
        state: str,
        outcome: str,
        provider_receipt_id: str | None = None,
    ) -> bool:
        if state not in {"completed", "cancelled", "abandoned"}:
            raise ValueError("run completion requires a terminal state")
        with self.transaction() as db:
            row = db.execute(
                "SELECT state, outcome, provider_receipt_id FROM runs WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(run_id))
            if row["state"] in {"completed", "cancelled", "abandoned"}:
                return (
                    row["state"] == state
                    and row["outcome"] == outcome
                    and row["provider_receipt_id"] == provider_receipt_id
                )
            db.execute(
                "UPDATE runs SET state=?, outcome=?, provider_receipt_id=?, "
                "completed_at=? WHERE run_id=?",
                (state, outcome, provider_receipt_id, _stamp(self._now()), str(run_id)),
            )
            db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
            self._transition(
                db,
                "run",
                str(run_id),
                state,
                {
                    "outcome": outcome,
                    "provider_receipt_id": provider_receipt_id,
                },
                correlation=Correlation(run_id=str(run_id)),
            )
            return True

    def register_lease(self, lease: Lease) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO leases(run_id, dispatch_id, owner_id, pid, pgid, "
                "process_start_ticks, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(lease.run_id),
                    lease.dispatch_id,
                    lease.owner_id,
                    lease.pid,
                    lease.pgid,
                    lease.process_start_ticks,
                    _stamp(lease.started_at),
                ),
            )
            db.execute(
                "UPDATE runs SET state='running' WHERE run_id=?",
                (str(lease.run_id),),
            )
            self._transition(
                db,
                "lease",
                str(lease.run_id),
                "active",
                {
                    "dispatch_id": lease.dispatch_id,
                    "owner_id": lease.owner_id,
                    "pid": lease.pid,
                    "pgid": lease.pgid,
                    "process_start_ticks": lease.process_start_ticks,
                },
                correlation=Correlation(
                    dispatch_id=lease.dispatch_id,
                    run_id=str(lease.run_id),
                ),
            )

    def register_process_lease(
        self,
        run_id: RunId,
        *,
        pid: int,
        pgid: int,
        process_start_ticks: int,
    ) -> Lease:
        """Bind a spawned process to its reserved run before paid work executes."""

        if pid <= 0 or pgid <= 0 or process_start_ticks <= 0:
            raise ValueError("live process identity is required")
        with self.transaction() as db:
            run = db.execute(
                "SELECT dispatch_id, owner_id, state FROM runs WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if run is None:
                raise ControlStoreError(
                    f"process lease has no reserved run: {run_id}"
                )
            if run["state"] in {"completed", "cancelled", "abandoned"}:
                raise ControlStoreError(
                    f"process lease cannot attach to terminal run: {run_id}"
                )
            existing = db.execute(
                "SELECT dispatch_id, owner_id, pid, pgid, process_start_ticks, "
                "started_at FROM leases WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if existing is not None:
                identical = (
                    int(existing["pid"]) == pid
                    and int(existing["pgid"]) == pgid
                    and int(existing["process_start_ticks"]) == process_start_ticks
                )
                if not identical:
                    raise ControlStoreError(
                        f"conflicting live process lease for run: {run_id}"
                    )
                return Lease(
                    run_id,
                    existing["dispatch_id"],
                    existing["owner_id"],
                    pid,
                    pgid,
                    _parse_stamp(existing["started_at"]),
                    process_start_ticks,
                )
            started_at = self._now()
            lease = Lease(
                run_id,
                run["dispatch_id"],
                run["owner_id"],
                pid,
                pgid,
                started_at,
                process_start_ticks,
            )
            db.execute(
                "INSERT INTO leases(run_id, dispatch_id, owner_id, pid, pgid, "
                "process_start_ticks, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(lease.run_id),
                    lease.dispatch_id,
                    lease.owner_id,
                    lease.pid,
                    lease.pgid,
                    lease.process_start_ticks,
                    _stamp(lease.started_at),
                ),
            )
            db.execute(
                "UPDATE runs SET state='running', "
                "started_at=COALESCE(started_at, ?) WHERE run_id=?",
                (_stamp(started_at), str(run_id)),
            )
            self._transition(
                db,
                "lease",
                str(run_id),
                "active",
                {
                    "dispatch_id": lease.dispatch_id,
                    "owner_id": lease.owner_id,
                    "pid": pid,
                    "pgid": pgid,
                    "process_start_ticks": process_start_ticks,
                },
                correlation=Correlation(
                    dispatch_id=lease.dispatch_id,
                    run_id=str(run_id),
                ),
            )
            return lease

    def release_lease(self, run_id: RunId) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
            self._transition(
                db,
                "lease",
                str(run_id),
                "released",
                correlation=Correlation(run_id=str(run_id)),
            )

    def leases(self, owner_id: str | None = None) -> tuple[Lease, ...]:
        with self._connect() as db:
            rows = (
                db.execute(
                    "SELECT l.run_id, l.dispatch_id, l.owner_id, l.pid, "
                    "l.pgid, l.started_at, l.process_start_ticks FROM leases l JOIN runs r "
                    "ON r.run_id=l.run_id WHERE r.owner_id=? "
                    "ORDER BY l.started_at",
                    (owner_id,),
                ).fetchall()
                if owner_id is not None
                else db.execute(
                    "SELECT run_id, dispatch_id, owner_id, pid, pgid, "
                    "started_at, process_start_ticks FROM leases ORDER BY started_at"
                ).fetchall()
            )
        return tuple(
            Lease(
                RunId(row["run_id"]),
                row["dispatch_id"],
                row["owner_id"],
                row["pid"],
                row["pgid"],
                _parse_stamp(row["started_at"]),
                int(row["process_start_ticks"]),
            )
            for row in rows
        )

    def publication_authority(
        self, run_id: RunId
    ) -> PublicationAuthority | None:
        """Return authority only for an active, supervised paid run."""

        with self._connect() as db:
            row = db.execute(
                "SELECT b.repository, r.owner_id, l.pgid, "
                "l.process_start_ticks FROM runs r "
                "JOIN budget_reservations b ON b.run_id=r.run_id "
                "JOIN leases l ON l.run_id=r.run_id "
                "WHERE r.run_id=? "
                "AND r.state IN ('starting', 'running') "
                "AND b.state='active'",
                (str(run_id),),
            ).fetchone()
        if row is None:
            return None
        return PublicationAuthority(
            run_id,
            str(row["repository"]),
            str(row["owner_id"]),
            int(row["pgid"]),
            int(row["process_start_ticks"]),
        )

    def reclaim_dead_leases(
        self,
        owner_id: str | Callable[[int, int], bool],
        is_alive: Callable[[int, int], bool] | None = None,
    ) -> tuple[RunId, ...]:
        """Atomically abandon runs whose durable owner process is gone."""

        if callable(owner_id):
            alive = owner_id
            scoped_owner: str | None = None
        else:
            if is_alive is None:
                raise ValueError("liveness probe is required")
            alive = is_alive
            scoped_owner = owner_id
        reclaimed: list[RunId] = []
        with self.transaction() as db:
            rows = (
                db.execute(
                    "SELECT l.run_id, l.pid, l.process_start_ticks "
                    "FROM leases l JOIN runs r "
                    "ON r.run_id=l.run_id WHERE r.owner_id=? "
                    "ORDER BY l.started_at",
                    (scoped_owner,),
                ).fetchall()
                if scoped_owner is not None
                else db.execute(
                    "SELECT run_id, pid, process_start_ticks "
                    "FROM leases ORDER BY started_at"
                ).fetchall()
            )
            for row in rows:
                if alive(int(row["pid"]), int(row["process_start_ticks"])):
                    continue
                run_id = RunId(row["run_id"])
                db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
                db.execute(
                    "UPDATE runs SET state='abandoned', outcome='dead_lease', "
                    "completed_at=? WHERE run_id=? AND state NOT IN "
                    "('completed','cancelled','abandoned')",
                    (_stamp(self._now()), str(run_id)),
                )
                self._transition(
                    db,
                    "run",
                    str(run_id),
                    "abandoned",
                    {"outcome": "dead_lease"},
                    correlation=Correlation(run_id=str(run_id)),
                )
                reclaimed.append(run_id)
        return tuple(reclaimed)

    def active_reservation_runs(
        self, owner_id: str | None = None
    ) -> tuple[RunId, ...]:
        with self._connect() as db:
            rows = (
                db.execute(
                    "SELECT b.run_id FROM budget_reservations b JOIN runs r "
                    "ON r.run_id=b.run_id WHERE b.state='active' "
                    "AND r.owner_id=? ORDER BY b.created_at",
                    (owner_id,),
                ).fetchall()
                if owner_id is not None
                else db.execute(
                    "SELECT run_id FROM budget_reservations "
                    "WHERE state='active' ORDER BY created_at"
                ).fetchall()
            )
        return tuple(RunId(row["run_id"]) for row in rows)

    def record_owned_cancellation_intents(
        self,
        owner_id: str,
        reason: str,
    ) -> tuple[RunCancellationIntent, ...]:
        """Persist hard-shutdown handovers without discarding spend authority.

        A killed provider process can leave rollout usage that becomes readable
        only after process reap. The active reservation and lease therefore stay
        intact until post-kill or boot recovery settles that evidence.
        """

        if not owner_id or not reason:
            raise ValueError("run owner and cancellation reason are required")
        intents: list[RunCancellationIntent] = []
        with self.transaction() as db:
            rows = db.execute(
                "SELECT r.run_id, r.dispatch_id, r.kind, r.state FROM runs r "
                "JOIN budget_reservations b ON b.run_id=r.run_id "
                "WHERE r.owner_id=? AND b.state='active' "
                "AND r.state NOT IN ('completed','cancelled','abandoned') "
                "ORDER BY b.created_at",
                (owner_id,),
            ).fetchall()
            now = _stamp(self._now())
            for row in rows:
                run_id = RunId(row["run_id"])
                db.execute(
                    "INSERT OR IGNORE INTO run_cancellation_intents("
                    "run_id, dispatch_id, kind, issue_number, reason, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(run_id),
                        row["dispatch_id"],
                        row["kind"],
                        _dispatch_issue(row["dispatch_id"]),
                        reason,
                        now,
                    ),
                )
                stored = db.execute(
                    "SELECT dispatch_id, kind, issue_number, reason "
                    "FROM run_cancellation_intents WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                intents.append(
                    RunCancellationIntent(
                        run_id,
                        stored["dispatch_id"],
                        stored["kind"],
                        (
                            int(stored["issue_number"])
                            if stored["issue_number"] is not None
                            else None
                        ),
                        stored["reason"],
                    )
                )
                self._transition(
                    db,
                    "run",
                    str(run_id),
                    row["state"],
                    {"reason": reason, "spend_recovery_pending": True},
                    correlation=_run_correlation(
                        run_id, row["dispatch_id"]
                    ),
                )
        return tuple(intents)

    def pending_cancellation_intents(
        self,
        owner_id: str,
    ) -> tuple[RunCancellationIntent, ...]:
        if not owner_id:
            raise ValueError("run owner is required")
        with self._connect() as db:
            rows = db.execute(
                "SELECT i.run_id, i.dispatch_id, i.kind, i.issue_number, "
                "i.reason FROM run_cancellation_intents i JOIN runs r "
                "ON r.run_id=i.run_id WHERE r.owner_id=? "
                "AND i.handover_written_at IS NULL ORDER BY i.created_at",
                (owner_id,),
            ).fetchall()
        return tuple(
            RunCancellationIntent(
                RunId(row["run_id"]),
                row["dispatch_id"],
                row["kind"],
                (
                    int(row["issue_number"])
                    if row["issue_number"] is not None
                    else None
                ),
                row["reason"],
            )
            for row in rows
        )

    def has_cancellation_intent(self, run_id: RunId) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM run_cancellation_intents WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                is not None
            )

    def mark_cancellation_handover_written(self, run_id: RunId) -> None:
        with self.transaction() as db:
            updated = db.execute(
                "UPDATE run_cancellation_intents SET handover_written_at=? "
                "WHERE run_id=? AND handover_written_at IS NULL",
                (_stamp(self._now()), str(run_id)),
            )
            if updated.rowcount != 1:
                raise KeyError(str(run_id))

    def abandoned_dispatch_counts(self, owner_id: str) -> dict[int, int]:
        """Return durable crash counts keyed by the domain breaker target.

        New admissions encode their target in ``dispatch_id``. Historical
        opaque ids are ignored: JSONL remains telemetry and is never a fallback
        control database.
        """

        if not owner_id:
            raise ValueError("run owner identity is required")
        with self._connect() as db:
            rows = db.execute(
                "SELECT dispatch_id FROM runs "
                "WHERE owner_id=? AND state='abandoned' ORDER BY completed_at",
                (owner_id,),
            ).fetchall()
        counts: dict[int, int] = {}
        for row in rows:
            try:
                payload = json.loads(row["dispatch_id"])
                target = int(payload["breaker_key"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            counts[target] = counts.get(target, 0) + 1
        return counts

    def claim(
        self,
        namespace: str,
        scope: str,
        key: str,
        *,
        reusable_after: datetime | None = None,
    ) -> bool:
        if not namespace or not scope or not key:
            raise ValueError("dedupe namespace, scope, and key are required")
        now = self._now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT reusable_after FROM dedupe_keys "
                "WHERE namespace=? AND scope=? AND key=?",
                (namespace, scope, key),
            ).fetchone()
            if row is not None:
                expires = (
                    _parse_stamp(row["reusable_after"])
                    if row["reusable_after"] is not None
                    else None
                )
                if expires is None or expires > now:
                    return False
                db.execute(
                    "DELETE FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                    (namespace, scope, key),
                )
            db.execute(
                "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, "
                "reusable_after) VALUES (?, ?, ?, ?, ?)",
                (
                    namespace,
                    scope,
                    key,
                    _stamp(now),
                    _stamp(reusable_after) if reusable_after else None,
                ),
            )
            return True

    def claim_dispatch(
        self,
        repository: str,
        role: str,
        issue: int,
        leg: str,
        owner_id: str,
        paths: frozenset[str],
    ) -> bool:
        """Atomically reserve one paid target/path set across deployments."""

        if (
            not repository
            or not role
            or issue == 0
            or not leg
            or not owner_id
        ):
            raise ValueError("dispatch claim identity is incomplete")
        from .process_supervisor import ProcessSupervisor

        identity = ProcessSupervisor.process_identity(os.getpid())
        if identity is None:
            raise ProcessLookupError("dispatch owner identity is unavailable")
        target = _dispatch_claim_target(issue)
        encoded_paths = _json(sorted(paths))
        with self._connect() as db:
            observed = db.execute(
                "SELECT target, owner_pid, owner_start_ticks "
                "FROM dispatch_claims WHERE repository=? AND role=?",
                (repository, role),
            ).fetchall()
        liveness = {
            (
                str(row["target"]),
                int(row["owner_pid"]),
                int(row["owner_start_ticks"]),
            ): ProcessSupervisor.matches_identity(
                int(row["owner_pid"]),
                int(row["owner_start_ticks"]),
            )
            for row in observed
        }
        with self.transaction() as db:
            rows = db.execute(
                "SELECT target, owner_id, owner_pid, owner_start_ticks, "
                "paths_json FROM dispatch_claims "
                "WHERE repository=? AND role=?",
                (repository, role),
            ).fetchall()
            live = []
            for row in rows:
                claim_identity = (
                    str(row["target"]),
                    int(row["owner_pid"]),
                    int(row["owner_start_ticks"]),
                )
                # A claim created or replaced after the read-only snapshot is
                # conservatively live. Process inspection must never run while
                # SQLite's cross-process write lock is held.
                if liveness.get(claim_identity, True):
                    live.append(row)
                    continue
                db.execute(
                    "DELETE FROM dispatch_claims "
                    "WHERE repository=? AND role=? AND target=? "
                    "AND owner_pid=? AND owner_start_ticks=?",
                    (
                        repository,
                        role,
                        row["target"],
                        row["owner_pid"],
                        row["owner_start_ticks"],
                    ),
                )
            for row in live:
                try:
                    other_paths = frozenset(json.loads(row["paths_json"]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise CorruptControlStore(
                        "dispatch claim contains invalid paths"
                    ) from exc
                if (
                    row["target"] == target
                    or not paths
                    or not other_paths
                    or bool(paths & other_paths)
                ):
                    return False
            db.execute(
                "INSERT INTO dispatch_claims("
                "repository, role, target, leg, owner_id, owner_pid, "
                "owner_start_ticks, paths_json, claimed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repository,
                    role,
                    target,
                    leg,
                    owner_id,
                    identity.pid,
                    identity.start_ticks,
                    encoded_paths,
                    _stamp(self._now()),
                ),
            )
            return True

    def expand_dispatch_claim(
        self,
        repository: str,
        role: str,
        issue: int,
        owner_id: str,
        paths: frozenset[str],
    ) -> bool:
        """Add router-discovered paths without racing another deployment."""

        target = _dispatch_claim_target(issue)
        with self.transaction() as db:
            owned = db.execute(
                "SELECT paths_json FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target=? AND owner_id=?",
                (repository, role, target, owner_id),
            ).fetchone()
            if owned is None:
                return False
            try:
                expanded = frozenset(json.loads(owned["paths_json"])) | paths
            except (TypeError, json.JSONDecodeError) as exc:
                raise CorruptControlStore(
                    "dispatch claim contains invalid paths"
                ) from exc
            rows = db.execute(
                "SELECT target, paths_json FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target<>?",
                (repository, role, target),
            ).fetchall()
            for row in rows:
                try:
                    other_paths = frozenset(json.loads(row["paths_json"]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise CorruptControlStore(
                        "dispatch claim contains invalid paths"
                    ) from exc
                if (
                    not expanded
                    or not other_paths
                    or bool(expanded & other_paths)
                ):
                    return False
            db.execute(
                "UPDATE dispatch_claims SET paths_json=? "
                "WHERE repository=? AND role=? AND target=? AND owner_id=?",
                (
                    _json(sorted(expanded)),
                    repository,
                    role,
                    target,
                    owner_id,
                ),
            )
            return True

    def release_dispatch_claim(
        self,
        repository: str,
        role: str,
        issue: int,
        owner_id: str,
    ) -> bool:
        target = _dispatch_claim_target(issue)
        with self.transaction() as db:
            cursor = db.execute(
                "DELETE FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target=? AND owner_id=?",
                (repository, role, target, owner_id),
            )
            return cursor.rowcount == 1

    def receipt(
        self,
        namespace: str,
        scope: str,
        key: str,
        payload: Mapping[str, object],
    ) -> bool:
        encoded = _json(payload)
        with self.transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys "
                "WHERE namespace=? AND scope=? AND key=?",
                (namespace, scope, key),
            ).fetchone()
            if row is not None:
                return row["payload_json"] == encoded
            db.execute(
                "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, "
                "payload_json) VALUES (?, ?, ?, ?, ?)",
                (namespace, scope, key, _stamp(self._now()), encoded),
            )
            return True

    def load_level_state(
        self, namespace: str, scope: str
    ) -> dict[str, dict[str, JsonValue]]:
        """Read a named level snapshot; malformed persisted payload fails closed."""

        if not namespace or not scope:
            raise ValueError("level namespace and scope are required")
        with self._connect() as db:
            rows = db.execute(
                "SELECT key, payload_json FROM level_state "
                "WHERE namespace=? AND scope=? ORDER BY key",
                (namespace, scope),
            ).fetchall()
        result: dict[str, dict[str, JsonValue]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise CorruptControlStore(
                    f"invalid level payload for {namespace}/{scope}/{row['key']}"
                ) from exc
            if not isinstance(payload, dict):
                raise CorruptControlStore(
                    f"level payload for {namespace}/{scope}/{row['key']} "
                    "is not an object"
                )
            result[str(row["key"])] = payload
        return result

    def replace_level_state(
        self,
        namespace: str,
        scope: str,
        values: Mapping[str, Mapping[str, JsonValue]],
    ) -> None:
        """Atomically replace a restart-safe level snapshot."""

        if not namespace or not scope:
            raise ValueError("level namespace and scope are required")
        if any(not key for key in values):
            raise ValueError("level keys are required")
        updated_at = _stamp(self._now())
        with self.transaction() as db:
            db.execute(
                "DELETE FROM level_state WHERE namespace=? AND scope=?",
                (namespace, scope),
            )
            db.executemany(
                "INSERT INTO level_state(namespace, scope, key, payload_json, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    (namespace, scope, key, _json(payload), updated_at)
                    for key, payload in sorted(values.items())
                ),
            )

    def record_world_observation(
        self,
        *,
        deployment: str,
        subject_kind: str,
        subject_number: int,
        content_hash: str,
        facts: Mapping[str, JsonValue],
        correlation: Correlation,
    ) -> bool:
        """Commit a changed observation and its outbox event atomically.

        Deduplication is against the subject's *latest* hash, not every hash ever
        seen, so ``A → B → A`` remains three observable transitions. A failed
        event sink leaves the committed outbox row pending for retry.
        """

        if (
            not deployment
            or subject_kind not in {"issue", "pr"}
            or subject_number <= 0
            or not content_hash
        ):
            raise ValueError("world observation identity is incomplete")
        with self.transaction() as db:
            row = db.execute(
                "SELECT content_hash FROM world_observations "
                "WHERE deployment=? AND subject_kind=? AND subject_number=?",
                (deployment, subject_kind, subject_number),
            ).fetchone()
            if row is not None and row["content_hash"] == content_hash:
                return False
            db.execute(
                "INSERT INTO world_observations("
                "deployment, subject_kind, subject_number, content_hash, observed_at"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(deployment, subject_kind, subject_number) DO UPDATE SET "
                "content_hash=excluded.content_hash, observed_at=excluded.observed_at",
                (
                    deployment,
                    subject_kind,
                    subject_number,
                    content_hash,
                    _stamp(self._now()),
                ),
            )
            self._event(
                db,
                WorldObserved(
                    subject_kind,
                    subject_number,
                    content_hash,
                    dict(facts),
                ),
                correlation=correlation,
            )
            return True

    def snapshot(self, scope: BudgetScope) -> AccountingSnapshot:
        try:
            with self._connect() as db:
                settled, reserved = self._balance(db, scope)
        except sqlite3.DatabaseError as exc:
            return AccountingSnapshot(
                AccountingDisposition.CORRUPT,
                scope,
                Money.zero(scope.currency),
                Money.zero(scope.currency),
                str(exc),
            )
        return AccountingSnapshot(
            AccountingDisposition.AVAILABLE,
            scope,
            Money(scope.currency, settled),
            Money(scope.currency, reserved),
        )

    def reserve(
        self,
        request: ReservationRequest,
        *,
        lease: Lease | None = None,
    ) -> ReservationResult:
        requested = request.quote.maximum_charge
        if lease is not None and (
            lease.run_id != request.run_id
            or lease.dispatch_id != request.dispatch_id
        ):
            raise ValueError("reservation and lease identities must match")
        limits = (
            BudgetLimit(request.scope, request.cap),
            *request.additional_limits,
        )
        try:
            with self.transaction() as db:
                prior = db.execute(
                    "SELECT * FROM budget_reservations WHERE run_id=?",
                    (str(request.run_id),),
                ).fetchone()
                if prior is not None:
                    identical = (
                        prior["reservation_id"] == str(request.reservation_id)
                        and prior["maximum_micros"] == requested.micros
                        and prior["currency"] == requested.currency
                        and prior["period_id"] == request.scope.period_id
                        and prior["rate_card_id"] == request.quote.rate_card_id
                        and prior["rate_card_version"] == request.quote.rate_card_version
                    )
                    disposition = (
                        AccountingDisposition.AVAILABLE
                        if identical
                        else AccountingDisposition.CONFLICT
                    )
                    settled, reserved = self._balance(db, request.scope)
                    return ReservationResult(
                        disposition,
                        ReservationId(prior["reservation_id"]),
                        Money(request.scope.currency, settled + reserved),
                        requested,
                        request.cap,
                        "idempotent reservation" if identical else "conflicting reservation",
                    )
                committed = Money.zero(request.scope.currency)
                for limit in limits:
                    settled, reserved = self._balance(db, limit.scope)
                    limit_committed = Money(
                        limit.scope.currency, settled + reserved
                    )
                    if limit.scope == request.scope:
                        committed = limit_committed
                    if limit_committed.micros + requested.micros > limit.cap.micros:
                        return ReservationResult(
                            AccountingDisposition.EXHAUSTED,
                            None,
                            limit_committed,
                            requested,
                            limit.cap,
                            "settled + active reservations + requested maximum "
                            f"exceeds {limit.scope.dimension} cap",
                        )
                now = _stamp(self._now())
                db.execute(
                    "INSERT INTO runs(run_id, dispatch_id, kind, owner_id, state, "
                    "config_version) VALUES (?, ?, ?, ?, 'reserved', ?)",
                    (
                        str(request.run_id),
                        request.dispatch_id,
                        request.kind,
                        request.owner_id,
                        request.config_version,
                    ),
                )
                db.execute(
                    "INSERT INTO budget_reservations("
                    "reservation_id, run_id, repository, dimension, period_id, "
                    "period_start, period_end, period_timezone, currency, "
                    "maximum_micros, cap_micros, model, rate_card_id, "
                    "rate_card_version, quote_effective_at, config_version, "
                    "state, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "'active', ?, ?)",
                    (
                        str(request.reservation_id),
                        str(request.run_id),
                        request.scope.repository,
                        request.scope.dimension,
                        request.scope.period_id,
                        _stamp(request.scope.period_start),
                        _stamp(request.scope.period_end),
                        str(request.scope.period_timezone),
                        request.scope.currency,
                        requested.micros,
                        request.cap.micros,
                        request.quote.model,
                        request.quote.rate_card_id,
                        request.quote.rate_card_version,
                        _stamp(request.quote.effective_at),
                        request.config_version,
                        now,
                        now,
                    ),
                )
                for limit in limits:
                    db.execute(
                        "INSERT INTO reservation_limits("
                        "run_id, repository, dimension, period_id, period_start, "
                        "period_end, period_timezone, currency, cap_micros"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(request.run_id),
                            limit.scope.repository,
                            limit.scope.dimension,
                            limit.scope.period_id,
                            _stamp(limit.scope.period_start),
                            _stamp(limit.scope.period_end),
                            str(limit.scope.period_timezone),
                            limit.scope.currency,
                            limit.cap.micros,
                        ),
                    )
                if lease is not None:
                    db.execute(
                        "INSERT INTO leases(run_id, dispatch_id, owner_id, pid, "
                        "pgid, process_start_ticks, started_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(lease.run_id),
                            lease.dispatch_id,
                            lease.owner_id,
                            lease.pid,
                            lease.pgid,
                            lease.process_start_ticks,
                            _stamp(lease.started_at),
                        ),
                    )
                    db.execute(
                        "UPDATE runs SET state='running' WHERE run_id=?",
                        (str(lease.run_id),),
                    )
                    self._transition(
                        db,
                        "lease",
                        str(lease.run_id),
                        "active",
                        {
                            "dispatch_id": lease.dispatch_id,
                            "owner_id": lease.owner_id,
                            "pid": lease.pid,
                            "pgid": lease.pgid,
                            "process_start_ticks": lease.process_start_ticks,
                        },
                        correlation=Correlation(
                            dispatch_id=lease.dispatch_id,
                            run_id=str(lease.run_id),
                        ),
                    )
                self._transition(
                    db,
                    "reservation",
                    str(request.run_id),
                    "active",
                    {
                        "reservation_id": str(request.reservation_id),
                        "maximum_micros": requested.micros,
                        "currency": requested.currency,
                        "rate_card_id": request.quote.rate_card_id,
                        "rate_card_version": request.quote.rate_card_version,
                        "config_version": request.config_version,
                    },
                    correlation=Correlation(
                        dispatch_id=request.dispatch_id,
                        run_id=str(request.run_id),
                    ),
                )
                return ReservationResult(
                    AccountingDisposition.AVAILABLE,
                    request.reservation_id,
                    committed + requested,
                    requested,
                    request.cap,
                )
        except (sqlite3.DatabaseError, OSError) as exc:
            return ReservationResult(
                AccountingDisposition.UNAVAILABLE,
                None,
                Money.zero(request.scope.currency),
                requested,
                request.cap,
                str(exc),
            )

    def settle(self, request: SettlementRequest) -> SettlementResult:
        with self.transaction() as db:
            reservation = db.execute(
                "SELECT b.state, b.maximum_micros, b.currency, r.dispatch_id "
                "FROM budget_reservations b JOIN runs r ON r.run_id=b.run_id "
                "WHERE b.run_id=?",
                (str(request.run_id),),
            ).fetchone()
            if reservation is None:
                return SettlementResult(
                    AccountingDisposition.CONFLICT,
                    request.amount,
                    detail="run has no reservation",
                )
            if reservation["currency"] != request.amount.currency:
                return SettlementResult(
                    AccountingDisposition.CONFLICT,
                    request.amount,
                    detail="settlement currency differs from reservation",
                )
            existing = db.execute(
                "SELECT * FROM charges WHERE run_id=? OR provider_receipt_id=?",
                (str(request.run_id), request.provider_receipt_id),
            ).fetchone()
            encoded_usage = (
                _json(
                    {
                        "input_tokens": request.usage.input_tokens,
                        "output_tokens": request.usage.output_tokens,
                        "cache_read_tokens": request.usage.cache_read_tokens,
                        "cache_write_tokens": request.usage.cache_write_tokens,
                        "source": request.usage.source.value,
                    }
                )
                if request.usage is not None
                else None
            )
            if existing is not None:
                identical = (
                    existing["run_id"] == str(request.run_id)
                    and existing["provider_receipt_id"] == request.provider_receipt_id
                    and existing["amount_micros"] == request.amount.micros
                    and existing["currency"] == request.amount.currency
                    and existing["basis"] == request.basis.value
                    and existing["usage_json"] == encoded_usage
                    and existing["rate_card_id"] == request.rate_card_id
                    and existing["rate_card_version"] == request.rate_card_version
                )
                return SettlementResult(
                    (
                        AccountingDisposition.AVAILABLE
                        if identical
                        else AccountingDisposition.CONFLICT
                    ),
                    request.amount,
                    idempotent=identical,
                    detail="" if identical else "conflicting settlement receipt",
                )
            if reservation["state"] != "active":
                return SettlementResult(
                    AccountingDisposition.CONFLICT,
                    request.amount,
                    detail=f"reservation is already {reservation['state']}",
                )
            correlation = _run_correlation(
                request.run_id, reservation["dispatch_id"]
            )
            maximum = int(reservation["maximum_micros"])
            variance = (
                Money(request.amount.currency, request.amount.micros - maximum)
                if request.amount.micros > maximum
                else None
            )
            db.execute(
                "INSERT INTO charges(run_id, provider_receipt_id, amount_micros, "
                "currency, basis, usage_json, rate_card_id, rate_card_version, "
                "settled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(request.run_id),
                    request.provider_receipt_id,
                    request.amount.micros,
                    request.amount.currency,
                    request.basis.value,
                    encoded_usage,
                    request.rate_card_id,
                    request.rate_card_version,
                    _stamp(self._now()),
                ),
            )
            db.execute(
                "UPDATE budget_reservations SET state='settled', updated_at=? "
                "WHERE run_id=?",
                (_stamp(self._now()), str(request.run_id)),
            )
            db.execute(
                "UPDATE runs SET "
                "state=CASE WHEN state IN ('completed','cancelled','abandoned') "
                "THEN state ELSE 'completed' END, "
                "outcome=CASE WHEN state IN ('completed','cancelled','abandoned') "
                "THEN outcome ELSE 'settled' END, "
                "provider_receipt_id=?, completed_at=COALESCE(completed_at, ?) "
                "WHERE run_id=?",
                (
                    request.provider_receipt_id,
                    _stamp(self._now()),
                    str(request.run_id),
                ),
            )
            db.execute("DELETE FROM leases WHERE run_id=?", (str(request.run_id),))
            self._event(
                db,
                SpendRecorded(
                    str(request.run_id),
                    request.amount.micros,
                    request.amount.currency,
                    request.basis.value,
                    request.rate_card_id,
                    request.rate_card_version,
                ),
                correlation=correlation,
            )
            if variance is not None:
                self._event(
                    db,
                    BudgetVarianceDetected(
                        str(request.run_id),
                        maximum,
                        request.amount.micros,
                        variance.micros,
                        request.amount.currency,
                    ),
                    correlation=correlation,
                )
            self._transition(
                db,
                "reservation",
                str(request.run_id),
                "settled",
                {
                    "amount_micros": request.amount.micros,
                    "currency": request.amount.currency,
                    "basis": request.basis.value,
                    "provider_receipt_id": request.provider_receipt_id,
                },
                correlation=correlation,
            )
            return SettlementResult(
                AccountingDisposition.AVAILABLE,
                request.amount,
                variance=variance,
            )

    def release(self, run_id: RunId, reason: str) -> ReleaseResult:
        if not reason:
            raise ValueError("reservation release reason is required")
        with self.transaction() as db:
            reservation = db.execute(
                "SELECT state, release_reason FROM budget_reservations WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
            if reservation is None:
                return ReleaseResult(
                    AccountingDisposition.CONFLICT, run_id, detail="run has no reservation"
                )
            if reservation["state"] == "released":
                identical = reservation["release_reason"] == reason
                return ReleaseResult(
                    (
                        AccountingDisposition.AVAILABLE
                        if identical
                        else AccountingDisposition.CONFLICT
                    ),
                    run_id,
                    idempotent=identical,
                    detail="" if identical else "conflicting release reason",
                )
            if reservation["state"] != "active":
                return ReleaseResult(
                    AccountingDisposition.CONFLICT,
                    run_id,
                    detail=f"reservation is already {reservation['state']}",
                )
            now = _stamp(self._now())
            db.execute(
                "UPDATE budget_reservations SET state='released', release_reason=?, "
                "updated_at=? WHERE run_id=?",
                (reason, now, str(run_id)),
            )
            db.execute(
                "UPDATE runs SET "
                "state=CASE WHEN state='abandoned' THEN 'abandoned' ELSE 'cancelled' END, "
                "outcome=CASE WHEN state='abandoned' THEN outcome ELSE 'released' END, "
                "completed_at=? WHERE run_id=?",
                (now, str(run_id)),
            )
            db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
            self._transition(
                db,
                "reservation",
                str(run_id),
                "released",
                {"reason": reason},
                correlation=Correlation(run_id=str(run_id)),
            )
            return ReleaseResult(AccountingDisposition.AVAILABLE, run_id)

    def enqueue_command(self, command: LifecycleCommand) -> PendingCommand:
        payload = _json(command.payload)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        now = _stamp(self._now())
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM lifecycle_commands WHERE idempotency_key=?",
                (command.idempotency_key,),
            ).fetchone()
            if row is not None:
                if (
                    row["command_id"] != str(command.command_id)
                    or row["kind"] != command.kind.value
                    or row["target"] != command.target
                    or row["expected_revision"] != command.expected_revision
                    or row["repository"] != command.repository
                    or row["executor_identity"] != command.executor_identity
                    or row["payload_digest"] != digest
                ):
                    raise ControlStoreError(
                        f"idempotency key {command.idempotency_key!r} conflicts"
                    )
                return PendingCommand(
                    _command_from_row(row), row["state"], row["attempts"]
                )
            db.execute(
                "INSERT INTO lifecycle_commands(command_id, idempotency_key, "
                "logical_key, occurrence, kind, "
                "target, expected_revision, config_version, repository, "
                "executor_identity, payload_json, "
                "payload_digest, state, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    str(command.command_id),
                    command.idempotency_key,
                    command.idempotency_key,
                    command.kind.value,
                    command.target,
                    command.expected_revision,
                    command.config_version,
                    command.repository,
                    command.executor_identity,
                    payload,
                    digest,
                    now,
                    now,
                ),
            )
            self._transition(
                db,
                "lifecycle_command",
                str(command.command_id),
                "pending",
                {
                    "kind": command.kind.value,
                    "target": command.target,
                    "idempotency_key": command.idempotency_key,
                    "expected_revision": command.expected_revision,
                    "config_version": command.config_version,
                },
                correlation=_command_correlation(command),
            )
            return PendingCommand(command, "pending", 0)

    def enqueue_command_occurrence(
        self, command: LifecycleCommand
    ) -> PendingCommand:
        """Start a new durable occurrence of a previously satisfied effect.

        This is used only after authoritative readback proves a terminal
        command's desired state was later reversed. The generation allocation
        and insert share one transaction, preventing concurrent daemons from
        executing the same recurrence twice.
        """

        payload = _json(command.payload)
        payload_digest = hashlib.sha256(payload.encode()).hexdigest()
        now = _stamp(self._now())
        with self.transaction() as db:
            existing = db.execute(
                "SELECT logical_key FROM lifecycle_commands WHERE command_id=?",
                (str(command.command_id),),
            ).fetchone()
            logical_key = (
                str(existing["logical_key"])
                if existing is not None
                else command.idempotency_key
            )
            latest = db.execute(
                "SELECT * FROM lifecycle_commands WHERE logical_key=? "
                "ORDER BY occurrence DESC LIMIT 1",
                (logical_key,),
            ).fetchone()
            if latest is not None and latest["state"] in {
                "pending",
                "executing",
                "retryable_failure",
                "unknown_outcome",
            }:
                if (
                    latest["kind"] != command.kind.value
                    or latest["target"] != command.target
                    or latest["expected_revision"] != command.expected_revision
                    or latest["repository"] != command.repository
                    or latest["executor_identity"] != command.executor_identity
                    or latest["payload_digest"] != payload_digest
                ):
                    raise ControlStoreError(
                        f"logical command {logical_key!r} has a conflicting "
                        "active occurrence"
                    )
                return PendingCommand(
                    _command_from_row(latest),
                    str(latest["state"]),
                    int(latest["attempts"]),
                )
            occurrence = (
                int(latest["occurrence"]) + 1 if latest is not None else 0
            )
            occurrence_key = f"{logical_key}:occurrence:{occurrence}"
            command_id = CommandId(
                hashlib.sha256(occurrence_key.encode()).hexdigest()
            )
            repeated = LifecycleCommand(
                command_id,
                occurrence_key,
                command.kind,
                command.target,
                command.expected_revision,
                dict(command.payload),
                command.config_version,
                command.repository,
                command.executor_identity,
            )
            db.execute(
                "INSERT INTO lifecycle_commands(command_id, idempotency_key, "
                "logical_key, occurrence, kind, target, expected_revision, "
                "config_version, repository, executor_identity, payload_json, "
                "payload_digest, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    str(command_id),
                    occurrence_key,
                    logical_key,
                    occurrence,
                    command.kind.value,
                    command.target,
                    command.expected_revision,
                    command.config_version,
                    command.repository,
                    command.executor_identity,
                    payload,
                    payload_digest,
                    now,
                    now,
                ),
            )
            self._transition(
                db,
                "lifecycle_command",
                str(command_id),
                "pending",
                {
                    "kind": command.kind.value,
                    "target": command.target,
                    "idempotency_key": occurrence_key,
                    "logical_key": logical_key,
                    "occurrence": occurrence,
                    "expected_revision": command.expected_revision,
                    "config_version": command.config_version,
                },
                correlation=_command_correlation(repeated),
            )
            return PendingCommand(repeated, "pending", 0)

    def pending_commands(
        self,
        *,
        repository: str | None = None,
        executor_identity: str | None = None,
    ) -> tuple[PendingCommand, ...]:
        predicates = [
            "state IN ('pending','executing','retryable_failure','unknown_outcome')"
        ]
        params: list[str] = []
        if repository is not None:
            predicates.append("repository=?")
            params.append(repository)
        if executor_identity is not None:
            predicates.append("executor_identity=?")
            params.append(executor_identity)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM lifecycle_commands WHERE "
                + " AND ".join(predicates)
                + " ORDER BY created_at, command_id",
                tuple(params),
            ).fetchall()
        return tuple(
            PendingCommand(_command_from_row(row), row["state"], row["attempts"])
            for row in rows
        )

    def begin_command(self, command_id: CommandId) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT state FROM lifecycle_commands WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(command_id))
            if row["state"] not in {"pending", "retryable_failure"}:
                raise ControlStoreError(
                    f"command {command_id} cannot execute from {row['state']}"
                )
            if row["state"] == "retryable_failure":
                db.execute(
                    "DELETE FROM effect_receipts WHERE command_id=?",
                    (str(command_id),),
                )
            db.execute(
                "UPDATE lifecycle_commands SET state='executing', attempts=attempts+1, "
                "updated_at=? WHERE command_id=?",
                (_stamp(self._now()), str(command_id)),
            )
            self._transition(
                db,
                "lifecycle_command",
                str(command_id),
                "executing",
            )

    def retry_reconciled_command(self, command_id: CommandId) -> None:
        """A trusted read proved an ambiguous mutation did not land."""

        with self.transaction() as db:
            row = db.execute(
                "SELECT state FROM lifecycle_commands WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(command_id))
            if row["state"] not in {"executing", "unknown_outcome"}:
                raise ControlStoreError(
                    f"command {command_id} cannot be reconciled from {row['state']}"
                )
            db.execute(
                "DELETE FROM effect_receipts WHERE command_id=?",
                (str(command_id),),
            )
            db.execute(
                "UPDATE lifecycle_commands SET state='retryable_failure', "
                "updated_at=? WHERE command_id=?",
                (_stamp(self._now()), str(command_id)),
            )
            self._transition(
                db,
                "lifecycle_command",
                str(command_id),
                "retryable_failure",
                {"reason": "authoritative verification found no effect"},
            )

    def resolve_unknown_effect(self, receipt: EffectReceipt) -> None:
        """Replace an ambiguous receipt after authoritative remote verification."""

        if receipt.status is not EffectStatus.SUCCEEDED:
            raise ValueError("unknown effect can only resolve to succeeded here")
        with self.transaction() as db:
            row = db.execute(
                "SELECT state FROM lifecycle_commands WHERE command_id=?",
                (str(receipt.command_id),),
            ).fetchone()
            if row is None or row["state"] not in {"executing", "unknown_outcome"}:
                raise ControlStoreError("command is not awaiting reconciliation")
            db.execute(
                "DELETE FROM effect_receipts WHERE command_id=?",
                (str(receipt.command_id),),
            )
            self._insert_effect(db, receipt, _receipt_digest(receipt))

    def record_effect(self, receipt: EffectReceipt) -> bool:
        digest = _receipt_digest(receipt)
        with self.transaction() as db:
            row = db.execute(
                "SELECT receipt_digest FROM effect_receipts WHERE command_id=?",
                (str(receipt.command_id),),
            ).fetchone()
            if row is not None:
                return row["receipt_digest"] == digest
            self._insert_effect(db, receipt, digest)
            return True

    def _insert_effect(
        self, db: sqlite3.Connection, receipt: EffectReceipt, digest: str
    ) -> None:
        db.execute(
            "INSERT INTO effect_receipts(command_id, executor_identity, "
            "started_at, completed_at, status, remote_object_id, "
            "remote_revision, error, receipt_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(receipt.command_id),
                receipt.executor_identity,
                _stamp(receipt.started_at),
                _stamp(receipt.completed_at),
                receipt.status.value,
                receipt.remote_object_id,
                receipt.remote_revision,
                receipt.error,
                digest,
            ),
        )
        db.execute(
            "UPDATE lifecycle_commands SET state=?, updated_at=? WHERE command_id=?",
            (
                receipt.status.value,
                _stamp(self._now()),
                str(receipt.command_id),
            ),
        )
        command_row = db.execute(
            "SELECT * FROM lifecycle_commands WHERE command_id=?",
            (str(receipt.command_id),),
        ).fetchone()
        if command_row is None:
            raise ControlStoreError(
                f"receipt {receipt.command_id} has no lifecycle command"
            )
        command = _command_from_row(command_row)
        if receipt.status is EffectStatus.SUCCEEDED:
            self._event(
                db,
                ActionApplied(
                    str(receipt.command_id),
                    command.kind.value,
                    command.target,
                    command.kind.value,
                ),
                correlation=_command_correlation(command),
            )
        else:
            self._event(
                db,
                ActionFailed(
                    str(receipt.command_id),
                    command.kind.value,
                    receipt.status.value,
                    receipt.error or "",
                ),
                correlation=_command_correlation(command),
            )
        self._transition(
            db,
            "lifecycle_command",
            str(receipt.command_id),
            receipt.status.value,
            {
                "kind": command.kind.value,
                "target": command.target,
                "executor_identity": receipt.executor_identity,
            },
            correlation=_command_correlation(command),
        )

    def effect_receipt(self, command_id: CommandId) -> EffectReceipt | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM effect_receipts WHERE command_id=?",
                (str(command_id),),
            ).fetchone()
        if row is None:
            return None
        return EffectReceipt(
            command_id=CommandId(row["command_id"]),
            executor_identity=row["executor_identity"],
            started_at=_parse_stamp(row["started_at"]),
            completed_at=_parse_stamp(row["completed_at"]),
            status=EffectStatus(row["status"]),
            remote_object_id=row["remote_object_id"],
            remote_revision=row["remote_revision"],
            error=row["error"],
        )

    def enqueue_event(self, event: EventEnvelope, db: sqlite3.Connection | None = None) -> None:
        encoded = encode_event_line(event)
        encoded_bytes = len((encoded + "\n").encode())
        if encoded_bytes > self._event_line_limit:
            raise ValueError(
                f"outbox event line is {encoded_bytes} bytes; "
                f"limit is {self._event_line_limit}"
            )

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR IGNORE INTO event_outbox("
                "event_id, destination, event_json, committed_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.event_id,
                    event.producer.feed_identity,
                    encoded,
                    _stamp(self._now()),
                ),
            )

        if db is not None:
            insert(db)
            return
        with self.transaction() as connection:
            insert(connection)

    def flush_outbox(
        self,
        emit: Callable[[EventEnvelope], None],
        limit: int = 100,
        *,
        destination: str | None = None,
    ) -> int:
        """Emit committed rows in order and acknowledge each only after success."""

        if limit <= 0:
            return 0
        emitted = 0
        processed = 0
        while processed < limit:
            with self._connect() as db:
                row = (
                    db.execute(
                        "SELECT outbox_id, event_json FROM event_outbox "
                        "WHERE emitted_at IS NULL AND failed_at IS NULL "
                        "AND destination=? ORDER BY outbox_id LIMIT 1",
                        (destination,),
                    ).fetchone()
                    if destination is not None
                    else db.execute(
                        "SELECT outbox_id, event_json FROM event_outbox "
                        "WHERE emitted_at IS NULL AND failed_at IS NULL "
                        "ORDER BY outbox_id LIMIT 1"
                    ).fetchone()
                )
            if row is None:
                break
            processed += 1
            try:
                if len((row["event_json"] + "\n").encode()) > self._event_line_limit:
                    raise ValueError("event exceeds configured line limit")
                event = decode_event_line(row["event_json"])
            except (TypeError, ValueError) as exc:
                with self.transaction() as db:
                    db.execute(
                        "UPDATE event_outbox SET failed_at=?, failure=? "
                        "WHERE outbox_id=? AND failed_at IS NULL",
                        (
                            _stamp(self._now()),
                            str(exc)[:500],
                            row["outbox_id"],
                        ),
                    )
                continue
            emit(event)
            with self.transaction() as db:
                db.execute(
                    "UPDATE event_outbox SET emitted_at=? "
                    "WHERE outbox_id=? AND emitted_at IS NULL",
                    (_stamp(self._now()), row["outbox_id"]),
                )
            emitted += 1
        return emitted

    def backup(self, destination: str) -> None:
        target = os.path.abspath(destination)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with self._connect() as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)

    def import_legacy_spend(self, source_path: str) -> MigrationReport:
        """Import an append-only legacy ledger without resetting live caps."""

        source = Path(source_path)
        raw = source.read_bytes()
        source_digest = hashlib.sha256(raw).hexdigest()
        migration_id = f"legacy-spend:{source_digest}"
        parsed: list[tuple[str, dict[str, Any], int, datetime, int]] = []
        rejected = 0
        for index, line in enumerate(raw.splitlines()):
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("event") != "spend":
                    raise ValueError("not a spend row")
                stamp = _parse_stamp(str(row["ts"]))
                repository = row.get("repo")
                issue = row.get("issue")
                if (
                    not isinstance(repository, str)
                    or not repository.strip()
                    or repository == "<unattributed>"
                    or isinstance(issue, bool)
                    or not isinstance(issue, int)
                    or issue <= 0
                ):
                    raise ValueError("spend scope is unattributed")
                amount = Decimal(
                    str(row.get("billed_cost_usd", row["cost_usd"]))
                )
                if not amount.is_finite():
                    raise ValueError("non-finite amount")
                micros = int(
                    (amount * Decimal(1_000_000)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                if micros < 0:
                    raise ValueError("negative amount")
                row_identity = hashlib.sha256(
                    stamp.strftime("%Y-%m").encode()
                    + b":"
                    + str(index).encode()
                    + b":"
                    + line
                ).hexdigest()
                run_id = hashlib.sha256(
                    f"legacy-spend-row:{row_identity}".encode()
                ).hexdigest()
                parsed.append((run_id, row, micros, stamp, issue))
            except (
                InvalidOperation,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                rejected += 1

        imported = 0
        total = 0
        with self.transaction() as db:
            existing = db.execute(
                "SELECT row_count, rejected_count, amount_micros "
                "FROM migration_imports WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if existing is not None:
                return MigrationReport(
                    migration_id,
                    source_digest,
                    int(existing["row_count"]),
                    int(existing["rejected_count"]),
                    Money("USD", int(existing["amount_micros"])),
                    True,
                )
            for run_id, row, micros, stamp, issue in parsed:
                period_start = stamp.astimezone(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                period_end = period_start + timedelta(days=1)
                period_id = period_start.date().isoformat()
                settled_at = _stamp(stamp)
                repository = str(row["repo"])
                db.execute(
                    "INSERT OR IGNORE INTO runs("
                    "run_id, dispatch_id, kind, owner_id, state, "
                    "config_version, started_at, completed_at, outcome, "
                    "provider_receipt_id) VALUES (?, ?, 'legacy-import', "
                    "'migration', 'completed', 'legacy-v1', ?, ?, 'settled', ?)",
                    (
                        run_id,
                        f"legacy:{run_id}",
                        settled_at,
                        settled_at,
                        f"legacy:{run_id}",
                    ),
                )
                if not int(db.execute("SELECT changes()").fetchone()[0]):
                    continue
                imported += 1
                total += micros
                db.execute(
                    "INSERT INTO budget_reservations("
                    "reservation_id, run_id, repository, dimension, period_id, "
                    "period_start, period_end, period_timezone, currency, "
                    "maximum_micros, cap_micros, model, rate_card_id, "
                    "rate_card_version, quote_effective_at, config_version, "
                    "state, created_at, updated_at) VALUES (?, ?, ?, "
                    "'repository', ?, ?, ?, 'UTC', 'USD', ?, ?, ?, 'legacy', "
                    "'1', ?, 'legacy-v1', 'settled', ?, ?)",
                    (
                        f"legacy:{run_id}",
                        run_id,
                        repository,
                        period_id,
                        _stamp(period_start),
                        _stamp(period_end),
                        micros,
                        9_000_000_000_000_000_000,
                        str(row.get("model") or "<unknown>"),
                        settled_at,
                        settled_at,
                        settled_at,
                    ),
                )
                scopes = (
                    (
                        repository,
                        "repository",
                        period_id,
                        period_start,
                        period_end,
                    ),
                    ("*", "global", period_id, period_start, period_end),
                    (
                        repository,
                        f"issue:{issue}",
                        f"lifetime:{issue}",
                        datetime(1970, 1, 1, tzinfo=timezone.utc),
                        datetime.max.replace(tzinfo=timezone.utc),
                    ),
                )
                for scoped_repo, dimension, scoped_period, start, end in scopes:
                    db.execute(
                        "INSERT INTO reservation_limits("
                        "run_id, repository, dimension, period_id, period_start, "
                        "period_end, period_timezone, currency, cap_micros"
                        ") VALUES (?, ?, ?, ?, ?, ?, 'UTC', 'USD', ?)",
                        (
                            run_id,
                            scoped_repo,
                            dimension,
                            scoped_period,
                            _stamp(start),
                            _stamp(end),
                            9_000_000_000_000_000_000,
                        ),
                    )
                db.execute(
                    "INSERT INTO charges(run_id, provider_receipt_id, "
                    "amount_micros, currency, basis, settled_at) "
                    "VALUES (?, ?, ?, 'USD', 'synthetic', ?)",
                    (run_id, f"legacy:{run_id}", micros, settled_at),
                )
            db.execute(
                "INSERT INTO migration_imports(migration_id, source_path, "
                "source_digest, row_count, rejected_count, amount_micros, "
                "imported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    migration_id,
                    str(source.resolve()),
                    source_digest,
                    imported,
                    rejected,
                    total,
                    _stamp(self._now()),
                ),
            )
        return MigrationReport(
            migration_id,
            source_digest,
            imported,
            rejected,
            Money("USD", total),
        )

    def _balance(
        self, db: sqlite3.Connection, scope: BudgetScope
    ) -> tuple[int, int]:
        params = (
            scope.repository,
            scope.dimension,
            scope.period_id,
            scope.currency,
        )
        settled = db.execute(
            "SELECT COALESCE(SUM(c.amount_micros), 0) AS amount "
            "FROM charges c JOIN reservation_limits l ON l.run_id=c.run_id "
            "WHERE l.repository=? AND l.dimension=? AND l.period_id=? "
            "AND c.currency=?",
            params,
        ).fetchone()["amount"]
        reserved = db.execute(
            "SELECT COALESCE(SUM(r.maximum_micros), 0) AS amount "
            "FROM budget_reservations r "
            "JOIN reservation_limits l ON l.run_id=r.run_id "
            "WHERE l.repository=? AND l.dimension=? AND l.period_id=? "
            "AND l.currency=? AND r.state='active'",
            params,
        ).fetchone()["amount"]
        return int(settled), int(reserved)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db


def _command_from_row(row: sqlite3.Row) -> LifecycleCommand:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise CorruptControlStore("lifecycle command payload is not an object")
    return LifecycleCommand(
        CommandId(row["command_id"]),
        row["idempotency_key"],
        CommandKind(row["kind"]),
        row["target"],
        row["expected_revision"],
        payload,
        row["config_version"],
        row["repository"],
        row["executor_identity"],
    )


def _command_correlation(command: LifecycleCommand) -> Correlation:
    kind, separator, raw_number = command.target.partition(":")
    if not separator:
        return Correlation()
    try:
        number = int(raw_number)
    except ValueError:
        return Correlation()
    if number <= 0:
        return Correlation()
    if kind == "issue":
        return Correlation(issue=number)
    if kind == "pr":
        return Correlation(pr=number)
    return Correlation()


def _run_correlation(run_id: RunId, dispatch_id: str) -> Correlation:
    try:
        payload = json.loads(dispatch_id)
        issue = payload.get("issue")
        number = payload.get("number")
        kind = str(payload.get("kind") or "")
    except (AttributeError, TypeError, json.JSONDecodeError):
        return Correlation(run_id=str(run_id))
    correlated_issue = (
        int(issue)
        if isinstance(issue, int) and not isinstance(issue, bool) and issue > 0
        else None
    )
    correlated_pr = (
        int(number)
        if correlated_issue is None
        and isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
        and "REVIEW" in kind
        else None
    )
    return Correlation(
        issue=correlated_issue,
        pr=correlated_pr,
        run_id=str(run_id),
    )


def _dispatch_issue(dispatch_id: str) -> int | None:
    try:
        value = json.loads(dispatch_id).get("issue")
    except (AttributeError, TypeError, json.JSONDecodeError):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _dispatch_claim_target(issue: int) -> str:
    if issue == 0:
        raise ValueError("dispatch claim target cannot be zero")
    return f"issue:{issue}" if issue > 0 else f"pr:{-issue}"


def _receipt_digest(receipt: EffectReceipt) -> str:
    encoded = _json(
        {
            "command_id": str(receipt.command_id),
            "executor_identity": receipt.executor_identity,
            "started_at": _stamp(receipt.started_at),
            "completed_at": _stamp(receipt.completed_at),
            "status": receipt.status.value,
            "remote_object_id": receipt.remote_object_id,
            "remote_revision": receipt.remote_revision,
            "error": receipt.error,
        }
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError(f"{type(value).__name__} is not JSON-serializable control data")


def _sql_statements(source: str) -> tuple[str, ...]:
    """Split immutable migration source without `executescript` auto-commits."""

    statements: list[str] = []
    pending: list[str] = []
    for line in source.splitlines():
        pending.append(line)
        candidate = "\n".join(pending).strip()
        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            pending.clear()
    remainder = "\n".join(pending).strip()
    if remainder:
        raise IncompatibleSchema("control-store migration source is incomplete")
    return tuple(statements)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control-store timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
