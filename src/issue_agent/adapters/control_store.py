"""SQLite operational authority defined by ADR 0003.

The store owns money (reservations, charges), runs and leases, dispatch
claims, and idempotency keys. Transactions are intentionally short: no network,
model, or subprocess work is accepted by this adapter.

The workflow, lifecycle-command and effect-receipt tables are no longer read or
written (slice 5d-1); their DDL stays until the store rewrite drops it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from .codex_child_usage import NativeFailure

from ..domain.contracts import (
    BudgetScope,
    Money,
    PriceQuote,
    PricingBasis,
    ChargeOrigin,
    ReservationId,
    SettlementRequest,
    RunId,
)
from ..domain.repository import canonical_repository

SCHEMA_VERSION = 46


SCHEMA_BASELINE_VERSION = 9
LEGACY_V8_SCHEMA_DIGEST = (
    "1740c6b03aae7ba5beccea7dd522d0859396e901dee6a40d12ff4284621745b5"
)
DEFAULT_BUSY_TIMEOUT_MS = 5_000
AUTHORITY_WITNESS_FORMAT = "issue-agent-control-authority-v1"


class ControlStoreError(RuntimeError):
    pass


class IncompatibleSchema(ControlStoreError):
    pass


class CorruptControlStore(ControlStoreError):
    pass


class LegacySpendMigrationError(ControlStoreError):
    pass


class RetiredOwnerError(ControlStoreError):
    pass


class AccountingDisposition(str, Enum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"
    CONFLICT = "conflict"


# Per-run leg class, written before settlement (see `leg_runs`) by both roles.
# 5b's name, kept like every stored namespace (act's `fix-comment`): a rename
# orphans the receipts already written under the old one.
LEG_NAMESPACE = "fix-leg"


@dataclass(frozen=True)
class LegRun:
    """One ended leg of either role: what it was dispatched as and on, and its receipt."""

    kind: str
    number: int
    issue: int
    head: str
    started_at: datetime | None
    completed_at: datetime
    receipt: Mapping[str, object] | None


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
class SettledSpend:
    disposition: AccountingDisposition
    total: Money | None
    by_repository: Mapping[str, Money]
    detail: str = ""

    def for_repository(self, repository: str) -> Money | None:
        if self.total is None:
            return None
        return self.by_repository.get(
            canonical_repository(repository),
            Money.zero(self.total.currency),
        )


def read_settled_spend(
    path: str,
    period_start: datetime,
    period_end: datetime,
    *,
    currency: str = "USD",
) -> SettledSpend:
    """Read settled charges without creating or mutating the control store."""

    if period_end <= period_start:
        raise ValueError("spend period end must follow its start")
    start = _stamp(period_start)
    end = _stamp(period_end)
    if not os.path.isfile(path):
        return SettledSpend(
            AccountingDisposition.UNAVAILABLE,
            None,
            MappingProxyType({}),
            f"control store does not exist: {path}",
        )

    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        db = sqlite3.connect(
            uri,
            uri=True,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
        )
    except sqlite3.Error as exc:
        return SettledSpend(
            AccountingDisposition.UNAVAILABLE,
            None,
            MappingProxyType({}),
            f"control store could not be opened read-only: {exc}",
        )
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA query_only=ON")
        quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise CorruptControlStore(
                f"control-store integrity check failed: {quick_check}"
            )
        checker = SQLiteControlStore(path)
        checker._validate_migration_history(
            checker._migration_history(db),
            require_current=True,
        )
        checker._assert_semantic_integrity(db)
        rows = db.execute(
            "SELECT r.workload_repository, "
            "COALESCE(SUM(c.amount_micros), 0) AS amount "
            "FROM charges c JOIN budget_reservations r ON r.run_id=c.run_id "
            "WHERE c.currency=? AND c.settled_at>=? AND c.settled_at<? "
            "GROUP BY r.workload_repository",
            (currency, start, end),
        ).fetchall()
    except (sqlite3.Error, ControlStoreError) as exc:
        return SettledSpend(
            AccountingDisposition.UNAVAILABLE,
            None,
            MappingProxyType({}),
            f"settled spend could not be read: {exc}",
        )
    finally:
        db.close()
    by_repository = {
        str(row["workload_repository"]): Money(
            currency, int(row["amount"])
        )
        for row in rows
    }
    return SettledSpend(
        AccountingDisposition.AVAILABLE,
        Money(currency, sum(item.micros for item in by_repository.values())),
        MappingProxyType(by_repository),
    )


@dataclass(frozen=True)
class ReservationRequest:
    reservation_id: ReservationId
    run_id: RunId
    dispatch_id: str
    scope: BudgetScope
    quote: PriceQuote
    cap: Money
    config_version: str
    workload_repository: str
    additional_limits: tuple["BudgetLimit", ...] = ()
    kind: str = "paid"
    owner_id: str = "reservation"

    def __post_init__(self) -> None:
        workload_repository = canonical_repository(
            self.workload_repository
        )
        object.__setattr__(
            self, "workload_repository", workload_repository
        )
        if self.scope.currency != self.quote.maximum_charge.currency:
            raise ValueError("reservation scope and quote currency differ")
        if self.cap.currency != self.scope.currency:
            raise ValueError("reservation cap and scope currency differ")
        if not str(self.reservation_id) or not str(self.run_id):
            raise ValueError("reservation and run identities are required")
        if (
            not self.config_version
            or not self.kind
            or not self.owner_id
            or not workload_repository
        ):
            raise ValueError(
                "configuration version, run kind, owner identity, and workload "
                "repository are required"
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
    # The limit that actually tripped, which is not always the primary scope
    # when a leg carries repository, global, and issue caps at once.
    tripped_dimension: str = ""

    @property
    def admitted(self) -> bool:
        return self.disposition is AccountingDisposition.AVAILABLE


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
class _DispatchClaimObservation:
    target: str
    leg: str
    owner_id: str
    owner_pid: int
    owner_start_ticks: int
    occurrence_id: str
    claimed_at: str
    bound_runs: tuple[tuple[str, str, int, int], ...]


@dataclass(frozen=True)
class ActiveDispatchClaim:
    """One live paid claim, as another role observes it.

    ``target_key`` uses the harness-wide signed convention: positive is an
    issue number, negative is ``-pr`` for a PR with no resolvable issue.
    ``claim_key`` identifies this CLAIM EPISODE for once-per-key journalling.
    Deliberately NOT the bound run id: a claim is taken at decision time and
    bound to a run later (and may bind several), so a run-derived key changes
    under an observer mid-hold and can be picked nondeterministically when two
    runs are live.
    """

    target_key: int
    role: str
    leg: str
    owner_id: str
    claimed_at: datetime
    claim_key: str


@dataclass(frozen=True)
class RunCancellationIntent:
    run_id: RunId
    dispatch_id: str
    kind: str
    issue_number: int | None
    reason: str


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
    transform_spec: str = ""

    @property
    def digest(self) -> str:
        material = self.source if not self.transform_spec else self.source + "\0" + self.transform_spec
        return hashlib.sha256(material.encode()).hexdigest()


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

_MIGRATION_V13 = """
UPDATE lifecycle_commands
SET repository=LOWER(TRIM(repository))
WHERE repository <> LOWER(TRIM(repository));

UPDATE budget_reservations
SET repository=LOWER(TRIM(repository))
WHERE repository <> '*' AND repository <> LOWER(TRIM(repository));

UPDATE reservation_limits
SET repository=LOWER(TRIM(repository))
WHERE repository <> '*' AND repository <> LOWER(TRIM(repository));

UPDATE dispatch_claims
SET repository=LOWER(TRIM(repository))
WHERE repository <> LOWER(TRIM(repository));
"""

_MIGRATION_V14 = """
CREATE TABLE dispatch_claim_runs (
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    target TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    bound_at TEXT NOT NULL,
    PRIMARY KEY (repository, role, target, run_id),
    FOREIGN KEY (repository, role, target)
        REFERENCES dispatch_claims(repository, role, target)
        ON DELETE CASCADE
);

CREATE INDEX dispatch_claim_runs_run
ON dispatch_claim_runs(run_id);
"""

_MIGRATION_V15 = """
CREATE TABLE retired_owners (
    owner_id TEXT PRIMARY KEY,
    successor_owner_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    role TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    CHECK (owner_id <> ''),
    CHECK (successor_owner_id <> ''),
    CHECK (repository <> ''),
    CHECK (role <> ''),
    CHECK (owner_id <> successor_owner_id)
);

CREATE INDEX retired_owners_successor
ON retired_owners(successor_owner_id);
"""

_MIGRATION_V16 = """
CREATE TABLE reservation_recovery (
    run_id TEXT PRIMARY KEY REFERENCES budget_reservations(run_id)
        ON DELETE CASCADE,
    policy TEXT NOT NULL CHECK (policy IN ('spawn_pending', 'running',
        'reconcile_only')),
    reason TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO reservation_recovery(run_id, policy, reason, updated_at)
SELECT run_id, 'reconcile_only',
       'reservation predates durable recovery classification', updated_at
FROM budget_reservations
WHERE state='active';
"""

_MIGRATION_V17 = """
ALTER TABLE budget_reservations
ADD COLUMN workload_repository TEXT NOT NULL DEFAULT '*'
    CHECK (workload_repository <> '');

UPDATE budget_reservations
SET workload_repository = CASE
    WHEN repository <> '*' THEN repository
    WHEN json_valid((
        SELECT dispatch_id FROM runs
        WHERE runs.run_id=budget_reservations.run_id
    )) THEN CASE
        WHEN json_type((
            SELECT dispatch_id FROM runs
            WHERE runs.run_id=budget_reservations.run_id
        ), '$.repository')='text'
        AND TRIM(json_extract((
            SELECT dispatch_id FROM runs
            WHERE runs.run_id=budget_reservations.run_id
        ), '$.repository')) NOT IN ('', '*')
        THEN LOWER(TRIM(json_extract((
            SELECT dispatch_id FROM runs
            WHERE runs.run_id=budget_reservations.run_id
        ), '$.repository')))
        ELSE '*'
    END
    ELSE '*'
END;

UPDATE budget_reservations
SET workload_repository = (
    SELECT MIN(repository)
    FROM reservation_limits
    WHERE reservation_limits.run_id=budget_reservations.run_id
      AND reservation_limits.repository<>'*'
)
WHERE workload_repository='*'
  AND EXISTS (
      SELECT 1
      FROM reservation_limits
      WHERE reservation_limits.run_id=budget_reservations.run_id
        AND reservation_limits.repository<>'*'
  );

UPDATE reservation_recovery
SET policy='spawn_pending',
    reason='pre-v16 reservation durably proven unlaunched',
    updated_at=(
        SELECT updated_at FROM budget_reservations
        WHERE budget_reservations.run_id=reservation_recovery.run_id
    )
WHERE policy='reconcile_only'
  AND reason='reservation predates durable recovery classification'
  AND EXISTS (
      SELECT 1
      FROM budget_reservations AS reservation
      JOIN runs AS run ON run.run_id=reservation.run_id
      WHERE reservation.run_id=reservation_recovery.run_id
        AND reservation.state='active'
        AND run.state IN ('reserved', 'starting')
        AND NOT EXISTS (
            SELECT 1 FROM leases
            WHERE leases.run_id=reservation.run_id
        )
  );
"""

_MIGRATION_V18 = """
ALTER TABLE lifecycle_commands
ADD COLUMN refusals INTEGER NOT NULL DEFAULT 0 CHECK (refusals >= 0);

UPDATE lifecycle_commands
SET refusals = 1
WHERE state='retryable_failure';
"""

_MIGRATION_V19 = """
CREATE TABLE workflow_authority_epochs (
    repository TEXT NOT NULL,
    family TEXT NOT NULL CHECK (length(family) BETWEEN 1 AND 64),
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    source_digest TEXT NOT NULL,
    inventory_digest TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    PRIMARY KEY (repository, family)
);
CREATE TABLE workflows (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('issue', 'pr')),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    version INTEGER NOT NULL CHECK (version >= 0),
    episode_json TEXT NOT NULL,
    PRIMARY KEY (repository, target_kind, target_number)
);
CREATE TABLE workflow_gates (
    gate_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    opened_generation INTEGER NOT NULL CHECK (opened_generation >= 0),
    cause_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'rearming', 'closed')),
    payload_json TEXT NOT NULL,
    UNIQUE (repository, target_kind, target_number, opened_generation, cause_key),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE TABLE workflow_rearms (
    request_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    gate_id TEXT NOT NULL REFERENCES workflow_gates(gate_id),
    from_generation INTEGER NOT NULL CHECK (from_generation >= 0),
    to_generation INTEGER NOT NULL CHECK (to_generation = from_generation + 1),
    state TEXT NOT NULL CHECK (state IN
        ('pending', 'completed', 'superseded', 'cancelled')),
    request_digest TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    UNIQUE (repository, target_kind, target_number, to_generation),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE UNIQUE INDEX workflow_one_pending_rearm
    ON workflow_rearms(repository, target_kind, target_number)
    WHERE state = 'pending';
CREATE TABLE workflow_rearm_commands (
    request_id TEXT NOT NULL REFERENCES workflow_rearms(request_id),
    operation_key TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE REFERENCES lifecycle_commands(command_id),
    disposition TEXT NOT NULL CHECK (disposition IN ('required', 'retired')),
    disposition_evidence_json TEXT NOT NULL,
    PRIMARY KEY (request_id, operation_key)
);
CREATE TABLE workflow_evidence (
    evidence_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    command_id TEXT REFERENCES lifecycle_commands(command_id),
    kind TEXT NOT NULL CHECK (kind IN ('observation', 'absence', 'retry_applicability', 'prior_receipt')),
    payload_json TEXT NOT NULL,
    CHECK (kind = 'observation' OR command_id IS NOT NULL),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE TABLE workflow_retry_authorizations (
    authorization_id TEXT PRIMARY KEY,
    request_id TEXT REFERENCES workflow_rearms(request_id),
    command_id TEXT NOT NULL REFERENCES lifecycle_commands(command_id),
    gate_id TEXT NOT NULL REFERENCES workflow_gates(gate_id),
    association_kind TEXT NOT NULL CHECK (association_kind IN ('standalone', 'rearm')),
    binding_json TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES workflow_evidence(evidence_id),
    request_digest TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    consumed_attempt INTEGER CHECK (consumed_attempt > 0),
    CHECK ((association_kind = 'standalone' AND request_id IS NULL)
        OR (association_kind = 'rearm' AND request_id IS NOT NULL))
);
ALTER TABLE runs ADD COLUMN workflow_binding_json TEXT;
ALTER TABLE lifecycle_commands ADD COLUMN workflow_binding_json TEXT;
ALTER TABLE lifecycle_commands ADD COLUMN workflow_reconciliation_json TEXT;
ALTER TABLE lifecycle_commands ADD COLUMN attempt_owner TEXT;
"""

_MIGRATION_V20 = """
-- The transaction also runs _backfill_workflow_episode_index_v20, validating
-- legacy binding values before recording their immutable episode projection.
ALTER TABLE runs ADD COLUMN workflow_episode_key TEXT;
UPDATE runs SET outcome='unlaunched_released'
WHERE state='cancelled' AND outcome='released' AND provider_receipt_id IS NULL
    AND EXISTS(SELECT 1 FROM budget_reservations b
               WHERE b.run_id=runs.run_id AND b.state='released')
    AND EXISTS(SELECT 1 FROM reservation_recovery rr
               WHERE rr.run_id=runs.run_id AND rr.policy='spawn_pending')
    AND NOT EXISTS(SELECT 1 FROM charges c WHERE c.run_id=runs.run_id)
    AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.run_id=runs.run_id);
CREATE INDEX runs_adjudication_episode ON runs(workflow_episode_key)
WHERE kind='ADJUDICATE'
    AND NOT(state='cancelled' AND outcome IS 'unlaunched_released' AND provider_receipt_id IS NULL);
CREATE TRIGGER runs_require_workflow_episode_insert BEFORE INSERT ON runs
WHEN (NEW.workflow_binding_json IS NULL) != (NEW.workflow_episode_key IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'workflow admission requires its episode projection');
END;
CREATE TRIGGER runs_require_workflow_episode_update BEFORE UPDATE ON runs
WHEN (NEW.workflow_binding_json IS NULL) != (NEW.workflow_episode_key IS NULL)
    OR (OLD.workflow_episode_key IS NOT NULL AND NEW.workflow_episode_key IS NOT OLD.workflow_episode_key)
BEGIN
    SELECT RAISE(ABORT, 'workflow episode projection is immutable');
END;
"""

_V20_TRANSFORM_SPEC = """workflow-episode-index-v1:
Decode every non-null immutable run binding with the tagged workflow codec.
Require a matching reservation repository and immutable admission run, binding,
stage and leg identity. Persist compact UTF-8 JSON array of repository, typed
target discriminator (issue/pr), target number, family, epoch and episode;
generation is excluded.
Iterate with bounded memory inside the same migration transaction. Any invalid
bound history aborts the entire migration. Never use the current workflow row.
"""


def _backfill_workflow_episode_index_v20(db: sqlite3.Connection) -> None:
    # The workflow model this transform decoded was deleted (slice 5d-1). A
    # pre-v20 store holding bound runs cannot be projected any more, so it
    # refuses: migrate it with a release from before the deletion first.
    if db.execute("SELECT 1 FROM runs WHERE workflow_binding_json IS NOT NULL LIMIT 1").fetchone() is not None:
        raise IncompatibleSchema("a pre-v20 store with workflow-bound runs predates the deleted workflow model")


def _backfill_workflow_components_v25(db: sqlite3.Connection) -> None:
    # As for v20: pre-v25 workflow rows predate the deleted workflow model.
    if db.execute("SELECT 1 FROM workflows LIMIT 1").fetchone() is not None:
        raise IncompatibleSchema("a pre-v25 store with workflow rows predates the deleted workflow model")
    for row in db.execute("SELECT repository,role,target,leg,owner_id,owner_pid,owner_start_ticks,claimed_at FROM dispatch_claims"):
        digest = hashlib.sha256(_json(tuple(row)).encode()).hexdigest()
        occurrence_id = f"legacy:{digest}"
        db.execute(
            "UPDATE dispatch_claims SET occurrence_id=? WHERE repository=? AND role=? AND target=?",
            (occurrence_id, row["repository"], row["role"], row["target"]),
        )
        _claim_lifecycle_receipt(db, row["repository"], occurrence_id, "created", row["claimed_at"], {
            "role": row["role"], "target": row["target"], "leg": row["leg"],
            "owner_id": row["owner_id"],
            "owner_pid": row["owner_pid"], "owner_start_ticks": row["owner_start_ticks"],
            "claimed_at": row["claimed_at"], "source": "v24-migration",
        })


def _claim_lifecycle_receipt(
    db: sqlite3.Connection, repository: str, occurrence_id: str, event: str,
    observed_at: str, fact: dict[str, object],
) -> None:
    db.execute(
        "INSERT INTO dedupe_keys(namespace,scope,key,claimed_at,payload_json) VALUES (?,?,?,?,?)",
        ("dispatch-claim-lifecycle", repository, f"{occurrence_id}:{event}",
         observed_at, _json(fact)),
    )


def _review_execution_lineage_migration() -> str:
    # A global witness also covers REPLACE's implicit deletion when SQLite's
    # recursive delete triggers are disabled: every replacement inserts a row.
    tables = (
        "dedupe_keys", "dispatch_claims", "runs", "leases", "budget_reservations",
        "reservation_recovery", "charges", "workflow_evidence", "reservation_limits",
        "quota_debits", "run_cancellation_intents",
    )
    identities = """CREATE TABLE review_archive_identity_members (
    source_table TEXT NOT NULL,
    source_rowid INTEGER NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    first TEXT NOT NULL,
    second TEXT NOT NULL,
    third TEXT NOT NULL,
    types_json TEXT NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY(source_table,source_rowid)
) WITHOUT ROWID;
CREATE INDEX review_archive_identity_lookup ON review_archive_identity_members
    (source_table,snapshot_sha256,first,second,third);
CREATE TABLE review_archive_identity_certifications (
    round_rows_version INTEGER NOT NULL,
    lifecycle_version INTEGER NOT NULL,
    projection_version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    PRIMARY KEY(round_rows_version,lifecycle_version,projection_version)
) WITHOUT ROWID;
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('identity_members',X'',0);
"""
    identities += "\n".join(
        f"""CREATE TRIGGER review_identity_{table}_{event.lower()}
AFTER {event} ON review_archive_identity_{table}
BEGIN
    SELECT RAISE(ABORT,'identity projection witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='identity_members' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='identity_members' AND scope=X'';
END;"""
        for table in ("members", "certifications") for event in ("INSERT", "UPDATE", "DELETE")
    )
    return identities + "\n" + "\n".join(
        f"""CREATE TRIGGER review_lineage_{table}_{event.lower()}
AFTER {event} ON {table}
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;"""
        for table in tables for event in ("INSERT", "UPDATE", "DELETE")
    )


# The stale split kinds stay: migration 37's digest and its partial index use this exact text.
_FIX_ISSUE_DISPATCH = """CASE WHEN json_valid(dispatch_id) THEN
    json_type(dispatch_id)='object' AND json_type(dispatch_id,'$.repository')='text'
    AND json_extract(dispatch_id,'$.role')='fix' AND json_type(dispatch_id,'$.issue')='integer'
    AND json_extract(dispatch_id,'$.issue')>0 ELSE 0 END
    AND kind IN ('ROUTE','RESOLVE','RESUME','AUTHOR','IMPLEMENT','ADJUDICATE',
                 'PR_RESUME','FIX_CI','ADDRESS_REVIEW','REBASE')"""
# Every leg derive counts: fix legs (above) and the review role's REVIEW legs.
_LEG_DISPATCH = f"""({_FIX_ISSUE_DISPATCH} OR (CASE WHEN json_valid(dispatch_id) THEN
    json_type(dispatch_id)='object' AND json_extract(dispatch_id,'$.role')='review'
    AND json_type(dispatch_id,'$.issue')='integer' AND json_extract(dispatch_id,'$.issue')>0 ELSE 0 END
    AND kind='REVIEW'))"""
_FIX_DISPATCH_REPOSITORY = "CASE WHEN json_valid(dispatch_id) THEN json_extract(dispatch_id,'$.repository') END"
_FIX_DISPATCH_ISSUE = "CASE WHEN json_valid(dispatch_id) THEN json_extract(dispatch_id,'$.issue') END"


# Migration 39's digest and its partial indices use this exact text.
_REPAIR_BINDING = "$.value[1].fields.attempt.fields.binding"
_REPAIR_VERSION = "$.value[1].fields.admission_version"
_REPAIR_ADMISSION = ("namespace='continuation-admission' AND json_valid(payload_json) AND "
    f"json_extract(payload_json,'{_REPAIR_BINDING}.fields.authority.fields.family.value')='pr_repair'")
_REPAIR_CURRENT_OWED = """json_valid(episode_json) AND (
    COALESCE(json_extract(episode_json,'$.value.fields.pr_repair.fields.watermarks.fields.last_admitted'),0)
        > COALESCE(json_extract(episode_json,'$.value.fields.pr_repair.fields.watermarks.fields.last_applied'),0)
    OR json_type(episode_json,'$.value.fields.issue_recovery.fields.state.fields.pending_ci')='object'
    OR json_type(episode_json,'$.value.fields.issue_recovery.fields.state.fields.pending_rebase')='object'
    OR json_type(episode_json,'$.value.fields.issue_recovery.fields.state.fields.review_failures.fields.pending')='object'
)"""


# Only a canonical, constructor-valid ASCII retirement is safe to omit before
# decoding. Malformed, noncanonical and Unicode receipts retain the full reader.
_SETTLED_RETIREMENT_FIELDS = ("outcome_evidence_id", "retirement_evidence_id")
_SETTLED_RETIREMENT_TEXT = " AND ".join(
    f"json_type(workflow_reconciliation_json,'$.value.fields.{name}')='text' "
    f"AND length(json_extract(workflow_reconciliation_json,'$.value.fields.{name}')) BETWEEN 1 AND 512 "
    f"AND trim(json_extract(workflow_reconciliation_json,'$.value.fields.{name}'))<>'' "
    f"AND instr(json_extract(workflow_reconciliation_json,'$.value.fields.{name}'),char(0))=0 "
    f"AND json_extract(workflow_reconciliation_json,'$.value.fields.{name}') NOT GLOB '*[^ -~]*'"
    for name in _SETTLED_RETIREMENT_FIELDS
)
_REVIEW_GENERATION_CANDIDATE = f"""(
    prewrite_read_json IS NOT NULL OR state IN ('succeeded','permanent_failure') OR
    CASE WHEN typeof(workflow_reconciliation_json)='text' AND json_valid(workflow_reconciliation_json)
    THEN NOT (coalesce(({_SETTLED_RETIREMENT_TEXT}),0) AND workflow_reconciliation_json=
        json_object('format',1,'value',json_object('fields',json_object(
            'outcome_evidence_id',json_extract(workflow_reconciliation_json,'$.value.fields.outcome_evidence_id'),
            'retirement_evidence_id',json_extract(workflow_reconciliation_json,'$.value.fields.retirement_evidence_id')
        ),'type','RetiredSettledCommand')))
    ELSE 1 END
)"""

_SCHEMA_MIGRATIONS = (
    # Version 9 is the immutable adoption baseline shipped by the architectural
    # hardening cutover. Its exact source digest already exists in deployed
    # databases, so future code validates rather than rewriting that history.
    SchemaMigration(SCHEMA_BASELINE_VERSION, _DDL),
    SchemaMigration(10, _MIGRATION_V10),
    SchemaMigration(11, _MIGRATION_V11),
    SchemaMigration(12, _MIGRATION_V12),
    SchemaMigration(13, _MIGRATION_V13),
    SchemaMigration(14, _MIGRATION_V14),
    SchemaMigration(15, _MIGRATION_V15),
    SchemaMigration(16, _MIGRATION_V16),
    SchemaMigration(17, _MIGRATION_V17),
    # v18 separates VERIFIED REFUSALS from attempts (ADR 0012). `attempts` is
    # incremented by every `begin_command`, including the ones a GitHub outage
    # produced (unknown_outcome → retry_reconciled → re-execute), so a ten-tick
    # degradation used to pre-burn the retry ceiling and retire a command on
    # its first genuine 403. Existing `retryable_failure` rows backfill to 1 —
    # exactly what that state asserts (the last receipt was a refusal) — rather
    # than to `attempts`, which would import the very over-count being fixed.
    SchemaMigration(18, _MIGRATION_V18),
    SchemaMigration(19, _MIGRATION_V19),
    SchemaMigration(20, _MIGRATION_V20, _V20_TRANSFORM_SPEC),
    SchemaMigration(21, """
CREATE INDEX runs_workflow_scope ON runs(workflow_episode_key);
CREATE INDEX lifecycle_publication_runs ON lifecycle_commands(CAST(json_extract(payload_json,'$.run_id') AS TEXT))
WHERE kind IN ('publish_branch','open_pull_request','update_pull_request_body')
AND state NOT IN ('succeeded','permanent_failure');
"""),
    # ADR 0015 family activation evidence: one immutable decision per imported
    # target, bound to its epoch and installed workflow, plus one row per
    # activation recording its ceiling and confirmed plan digest, so even a
    # row-less activation (nothing to import) can be recognised exactly.
    SchemaMigration(22, """
CREATE TABLE workflow_activation_imports (
    repository TEXT NOT NULL,
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    target_kind TEXT NOT NULL CHECK (target_kind IN ('issue', 'pr')),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    source_digest TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (repository, family, target_kind, target_number),
    FOREIGN KEY (repository, family)
        REFERENCES workflow_authority_epochs (repository, family),
    FOREIGN KEY (repository, target_kind, target_number)
        REFERENCES workflows (repository, target_kind, target_number)
);
CREATE TABLE workflow_activation_evidence (
    repository TEXT NOT NULL,
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    ceiling INTEGER NOT NULL CHECK (ceiling >= 0),
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64),
    PRIMARY KEY (repository, family, epoch),
    FOREIGN KEY (repository, family)
        REFERENCES workflow_authority_epochs (repository, family)
);
"""),
    # ADR 0015 retry grants (review 2026-09-25). Every grant ends terminal;
    # an open grant is the only row that can match; at most one exists per
    # command; a disposition never changes. An open grant may only record its
    # single schedule bypass and count unavailable reads toward its ceiling.
    # Legacy consumed rows take their command's last update as the attempt
    # time, which only ever demands a later absence observation.
    SchemaMigration(23, """
ALTER TABLE workflow_retry_authorizations ADD COLUMN disposition TEXT
    CHECK (disposition IN ('consumed', 'not_needed', 'inapplicable', 'void'));
ALTER TABLE workflow_retry_authorizations ADD COLUMN disposed_at TEXT;
ALTER TABLE workflow_retry_authorizations ADD COLUMN disposition_reason TEXT;
ALTER TABLE workflow_retry_authorizations ADD COLUMN accepted_at TEXT;
ALTER TABLE workflow_retry_authorizations ADD COLUMN first_visit_at TEXT;
ALTER TABLE workflow_retry_authorizations ADD COLUMN unavailable_reads INTEGER NOT NULL DEFAULT 0
    CHECK (unavailable_reads >= 0);
UPDATE workflow_retry_authorizations
SET disposition='consumed', disposition_reason='authorized attempt began',
    disposed_at=(SELECT updated_at FROM lifecycle_commands c
                 WHERE c.command_id=workflow_retry_authorizations.command_id)
WHERE consumed_attempt IS NOT NULL;
UPDATE workflow_retry_authorizations
SET accepted_at=(SELECT updated_at FROM lifecycle_commands c
                 WHERE c.command_id=workflow_retry_authorizations.command_id);
CREATE UNIQUE INDEX workflow_retry_open_grant ON workflow_retry_authorizations(command_id)
WHERE disposition IS NULL;
CREATE TRIGGER workflow_retry_grant_insert BEFORE INSERT ON workflow_retry_authorizations
WHEN NEW.disposition IS NOT NULL OR NEW.consumed_attempt IS NOT NULL OR NEW.disposed_at IS NOT NULL
    OR NEW.disposition_reason IS NOT NULL OR NEW.first_visit_at IS NOT NULL
    OR NEW.unavailable_reads != 0 OR NEW.accepted_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'a retry grant is created open');
END;
CREATE TRIGGER workflow_retry_grant_update BEFORE UPDATE ON workflow_retry_authorizations
WHEN OLD.disposition IS NOT NULL
    OR NEW.authorization_id IS NOT OLD.authorization_id OR NEW.request_id IS NOT OLD.request_id
    OR NEW.command_id IS NOT OLD.command_id OR NEW.gate_id IS NOT OLD.gate_id
    OR NEW.association_kind IS NOT OLD.association_kind OR NEW.binding_json IS NOT OLD.binding_json
    OR NEW.evidence_id IS NOT OLD.evidence_id OR NEW.request_digest IS NOT OLD.request_digest
    OR NEW.actor_json IS NOT OLD.actor_json OR NEW.generation IS NOT OLD.generation
    OR NEW.accepted_at IS NOT OLD.accepted_at
    OR (OLD.first_visit_at IS NOT NULL AND NEW.first_visit_at IS NOT OLD.first_visit_at)
    OR NEW.unavailable_reads NOT IN (OLD.unavailable_reads, OLD.unavailable_reads + 1)
    OR (NEW.disposition IS NULL AND (NEW.disposed_at IS NOT NULL OR NEW.disposition_reason IS NOT NULL
        OR NEW.consumed_attempt IS NOT NULL))
    OR (NEW.disposition IS NOT NULL AND (NEW.disposed_at IS NULL OR NEW.disposition_reason IS NULL
        OR (NEW.disposition = 'consumed') != (NEW.consumed_attempt IS NOT NULL)))
BEGIN
    SELECT RAISE(ABORT, 'a retry grant is disposed exactly once and is otherwise immutable');
END;
"""),
    # ADR 0015 review slice. `prewrite_read_json` is the bounded schedule of a
    # pending command whose pre-write guard read was unavailable (ADR 0012
    # ceiling); it is not outcome uncertainty and never enters the
    # reconciliation JSON. The remote-object index serves the one-receipt-per-
    # review check on bound review submissions.
    SchemaMigration(24, """
ALTER TABLE lifecycle_commands ADD COLUMN prewrite_read_json TEXT;
CREATE INDEX effect_receipts_remote_object ON effect_receipts(remote_object_id)
WHERE remote_object_id IS NOT NULL;
"""),
    SchemaMigration(25, """
CREATE TABLE workflow_components (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    episode_id TEXT NOT NULL CHECK (length(episode_id) BETWEEN 1 AND 512),
    PRIMARY KEY (repository,target_kind,target_number,family),
    FOREIGN KEY (repository,target_kind,target_number)
        REFERENCES workflows(repository,target_kind,target_number),
    CHECK ((family='resolve' AND target_kind='issue') OR
           (family IN ('review_rejections','review_execution') AND target_kind='pr'))
);
CREATE INDEX workflow_components_family ON workflow_components(repository,family);
CREATE TABLE review_execution_rounds (
    round_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind='pr'),
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    execution_epoch INTEGER NOT NULL CHECK (execution_epoch > 0),
    execution_episode TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    claim_occurrence_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    planning_json TEXT NOT NULL,
    closure_json TEXT,
    outcome_json TEXT,
    UNIQUE (repository,target_number,execution_epoch,execution_episode,sequence),
    FOREIGN KEY (repository,target_kind,target_number)
        REFERENCES workflows(repository,target_kind,target_number)
);
CREATE INDEX review_execution_rounds_episode
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence);
CREATE INDEX review_execution_rounds_open
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence)
WHERE outcome_json IS NULL;
CREATE INDEX review_execution_rounds_unclosed
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,sequence)
WHERE closure_json IS NULL;
ALTER TABLE runs ADD COLUMN review_round_id TEXT REFERENCES review_execution_rounds(round_id);
ALTER TABLE runs ADD COLUMN review_child_id TEXT;
CREATE UNIQUE INDEX runs_review_child ON runs(review_round_id,review_child_id)
WHERE review_round_id IS NOT NULL;
CREATE TRIGGER runs_review_attachment_insert BEFORE INSERT ON runs
WHEN (NEW.review_round_id IS NULL) != (NEW.review_child_id IS NULL)
BEGIN SELECT RAISE(ABORT,'review round and child must be attached together'); END;
CREATE TRIGGER runs_review_attachment_update BEFORE UPDATE ON runs
WHEN (NEW.review_round_id IS NULL) != (NEW.review_child_id IS NULL)
    OR NEW.review_round_id IS NOT OLD.review_round_id
    OR NEW.review_child_id IS NOT OLD.review_child_id
BEGIN SELECT RAISE(ABORT,'review run attachment is immutable'); END;
ALTER TABLE dispatch_claims ADD COLUMN occurrence_id TEXT;
CREATE UNIQUE INDEX dispatch_claims_occurrence ON dispatch_claims(occurrence_id);
CREATE TRIGGER dispatch_claims_occurrence_insert BEFORE INSERT ON dispatch_claims
WHEN NEW.occurrence_id IS NULL OR length(NEW.occurrence_id)=0
BEGIN SELECT RAISE(ABORT,'dispatch claim requires an occurrence'); END;
CREATE TRIGGER dispatch_claims_occurrence_update BEFORE UPDATE ON dispatch_claims
WHEN NEW.occurrence_id IS NULL OR length(NEW.occurrence_id)=0
    OR (OLD.occurrence_id IS NOT NULL AND NEW.occurrence_id IS NOT OLD.occurrence_id)
BEGIN SELECT RAISE(ABORT,'dispatch claim occurrence is immutable'); END;
"""),
    SchemaMigration(26, """
CREATE INDEX review_execution_rounds_repository
ON review_execution_rounds(repository);
"""),
    SchemaMigration(27, """
CREATE TABLE review_archive_source_versions (
    source TEXT NOT NULL CHECK (typeof(source)='text' AND length(source) BETWEEN 1 AND 64),
    scope BLOB NOT NULL CHECK (typeof(scope)='blob'),
    version INTEGER NOT NULL CHECK (
        typeof(version)='integer' AND version BETWEEN 0 AND 9223372036854775806
    ),
    PRIMARY KEY (source,scope)
) WITHOUT ROWID;
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('round_receipts',X'',0);
INSERT INTO review_archive_source_versions(source,scope,version)
SELECT 'round_rows',CAST(repository AS BLOB),0
FROM workflows GROUP BY CAST(repository AS BLOB);
CREATE TRIGGER review_workflow_round_source_insert AFTER INSERT ON workflows
WHEN NOT EXISTS (
    SELECT 1 FROM review_archive_source_versions
    WHERE source='round_rows' AND scope=CAST(NEW.repository AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'existing workflow repository lost its round source witness')
    WHERE EXISTS (
        SELECT 1 FROM workflows
        WHERE repository IN (
            NEW.repository,CAST(NEW.repository AS BLOB),CAST(NEW.repository AS TEXT)
        ) AND rowid<>NEW.rowid LIMIT 1
    );
    INSERT INTO review_archive_source_versions(source,scope,version)
    VALUES ('round_rows',CAST(NEW.repository AS BLOB),0);
END;
-- REPLACE may remove a conflicting row without firing its DELETE trigger.
CREATE TRIGGER review_round_source_no_replace_insert BEFORE INSERT ON review_execution_rounds
WHEN EXISTS (
    SELECT 1 FROM review_execution_rounds
    WHERE round_id=NEW.round_id OR (
        repository=NEW.repository AND target_number=NEW.target_number
        AND execution_epoch=NEW.execution_epoch AND execution_episode=NEW.execution_episode
        AND sequence=NEW.sequence
    ) LIMIT 1
)
BEGIN
    SELECT RAISE(ABORT,'review round replacement would hide a source mutation');
END;
CREATE TRIGGER review_round_source_no_replace_update BEFORE UPDATE ON review_execution_rounds
WHEN (
    NEW.round_id IS NOT OLD.round_id OR NEW.repository IS NOT OLD.repository
    OR NEW.target_number IS NOT OLD.target_number
    OR NEW.execution_epoch IS NOT OLD.execution_epoch
    OR NEW.execution_episode IS NOT OLD.execution_episode
    OR NEW.sequence IS NOT OLD.sequence
) AND EXISTS (
    SELECT 1 FROM review_execution_rounds
    WHERE rowid<>OLD.rowid AND (
        round_id=NEW.round_id OR (
            repository=NEW.repository AND target_number=NEW.target_number
            AND execution_epoch=NEW.execution_epoch AND execution_episode=NEW.execution_episode
            AND sequence=NEW.sequence
        )
    ) LIMIT 1
)
BEGIN
    SELECT RAISE(ABORT,'review round replacement would hide a source mutation');
END;
CREATE TRIGGER review_round_source_insert AFTER INSERT ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_rows' AND scope=CAST(NEW.repository AS BLOB)
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_rows' AND scope=CAST(NEW.repository AS BLOB);
END;
CREATE TRIGGER review_round_source_update AFTER UPDATE ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'prior round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_rows' AND scope=CAST(OLD.repository AS BLOB)
    );
    SELECT RAISE(ABORT,'new round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_rows' AND scope=CAST(NEW.repository AS BLOB)
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_rows' AND scope IN (
        CAST(OLD.repository AS BLOB),CAST(NEW.repository AS BLOB)
    );
END;
CREATE TRIGGER review_round_source_delete AFTER DELETE ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_rows' AND scope=CAST(OLD.repository AS BLOB)
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_rows' AND scope=CAST(OLD.repository AS BLOB);
END;
CREATE TRIGGER review_receipt_source_insert AFTER INSERT ON dedupe_keys
WHEN substr(CAST(NEW.namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
    OR CAST(NEW.namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
BEGIN
    SELECT RAISE(ABORT,'round receipt source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_receipts' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_receipts' AND scope=X'';
END;
CREATE TRIGGER review_receipt_source_update AFTER UPDATE ON dedupe_keys
WHEN substr(CAST(OLD.namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
    OR CAST(OLD.namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
    OR substr(CAST(NEW.namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
    OR CAST(NEW.namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
BEGIN
    SELECT RAISE(ABORT,'round receipt source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_receipts' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_receipts' AND scope=X'';
END;
CREATE TRIGGER review_receipt_source_delete AFTER DELETE ON dedupe_keys
WHEN substr(CAST(OLD.namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
    OR CAST(OLD.namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
BEGIN
    SELECT RAISE(ABORT,'round receipt source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_receipts' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_receipts' AND scope=X'';
END;
"""),
    SchemaMigration(28, """
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('all_round_rows',X'',0);
CREATE TRIGGER review_all_round_source_insert AFTER INSERT ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'global round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='all_round_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='all_round_rows' AND scope=X'';
END;
CREATE TRIGGER review_all_round_source_update AFTER UPDATE ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'global round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='all_round_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='all_round_rows' AND scope=X'';
END;
CREATE TRIGGER review_all_round_source_delete AFTER DELETE ON review_execution_rounds
BEGIN
    SELECT RAISE(ABORT,'global round source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='all_round_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='all_round_rows' AND scope=X'';
END;
"""),
    SchemaMigration(29, """
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('generation_sources',X'',0);
-- REPLACE can delete a selected source without running its DELETE trigger.
CREATE TRIGGER review_archive_dedupe_no_replace_insert BEFORE INSERT ON dedupe_keys
WHEN NEW.rowid IS NOT -1 AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=NEW.rowid AND (
        substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
        OR CAST(namespace AS BLOB) IN (
            CAST('review-recorded-submission' AS BLOB),
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    )
)
BEGIN
    SELECT RAISE(ABORT,'review archive replacement would hide a source mutation');
END;
-- SQLite reports an unspecified NEW.rowid (currently -1) for automatic inserts.
-- Conservatively advance a source at -1 so ordinary inserts remain possible.
CREATE TRIGGER review_archive_dedupe_minus_one_insert BEFORE INSERT ON dedupe_keys
WHEN NEW.rowid=-1 AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND (
        substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
        OR CAST(namespace AS BLOB) IN (
            CAST('review-recorded-submission' AS BLOB),
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    )
)
BEGIN
    SELECT RAISE(ABORT,'round receipt source witness is absent')
    WHERE EXISTS (
        SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND (
            substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
            OR CAST(namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
        )
    ) AND NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='round_receipts' AND scope=X''
    );
    SELECT RAISE(ABORT,'generation source witness is absent')
    WHERE EXISTS (
        SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND CAST(namespace AS BLOB) IN (
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    ) AND NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='generation_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='round_receipts' AND scope=X'' AND EXISTS (
        SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND (
            substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
            OR CAST(namespace AS BLOB)=CAST('review-recorded-submission' AS BLOB)
        )
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='generation_sources' AND scope=X'' AND EXISTS (
        SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND CAST(namespace AS BLOB) IN (
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    );
END;
CREATE TRIGGER review_archive_dedupe_no_replace_update BEFORE UPDATE ON dedupe_keys
WHEN NEW.rowid IS NOT OLD.rowid AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=NEW.rowid AND (
        substr(CAST(namespace AS BLOB),1,13)=CAST('review-round-' AS BLOB)
        OR CAST(namespace AS BLOB) IN (
            CAST('review-recorded-submission' AS BLOB),
            CAST('review-execution-generation' AS BLOB),
            CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
        )
    )
)
BEGIN
    SELECT RAISE(ABORT,'review archive replacement would hide a source mutation');
END;
CREATE TRIGGER review_generation_source_insert AFTER INSERT ON dedupe_keys
WHEN CAST(NEW.namespace AS BLOB) IN (
    CAST('review-execution-generation' AS BLOB),
    CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'generation source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='generation_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='generation_sources' AND scope=X'';
END;
CREATE TRIGGER review_generation_source_update AFTER UPDATE ON dedupe_keys
WHEN CAST(OLD.namespace AS BLOB) IN (
    CAST('review-execution-generation' AS BLOB),
    CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
) OR CAST(NEW.namespace AS BLOB) IN (
    CAST('review-execution-generation' AS BLOB),
    CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'generation source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='generation_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='generation_sources' AND scope=X'';
END;
CREATE TRIGGER review_generation_source_delete AFTER DELETE ON dedupe_keys
WHEN CAST(OLD.namespace AS BLOB) IN (
    CAST('review-execution-generation' AS BLOB),
    CAST('workflow-request' AS BLOB),CAST('workflow-transition' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'generation source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='generation_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='generation_sources' AND scope=X'';
END;
"""),
    SchemaMigration(30, """
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('checkpoint_sources',X'',0),('workflow_rows',X'',0);
CREATE TRIGGER review_checkpoint_source_no_replace_insert BEFORE INSERT ON dedupe_keys
WHEN NEW.rowid IS NOT -1 AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=NEW.rowid AND CAST(namespace AS BLOB) IN (
        CAST('review-execution-origin' AS BLOB),
        CAST('review-execution-checkpoint' AS BLOB),
        CAST('review-checkpoint-application' AS BLOB)
    )
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source replacement would hide a mutation');
END;
-- An automatic INSERT exposes NEW.rowid=-1 before SQLite allocates its rowid.
CREATE TRIGGER review_checkpoint_source_minus_one_insert BEFORE INSERT ON dedupe_keys
WHEN NEW.rowid=-1 AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=-1 AND CAST(namespace AS BLOB) IN (
        CAST('review-execution-origin' AS BLOB),
        CAST('review-execution-checkpoint' AS BLOB),
        CAST('review-checkpoint-application' AS BLOB)
    )
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='checkpoint_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='checkpoint_sources' AND scope=X'';
END;
CREATE TRIGGER review_checkpoint_source_no_replace_update BEFORE UPDATE ON dedupe_keys
WHEN NEW.rowid IS NOT OLD.rowid AND EXISTS (
    SELECT 1 FROM dedupe_keys WHERE rowid=NEW.rowid AND CAST(namespace AS BLOB) IN (
        CAST('review-execution-origin' AS BLOB),
        CAST('review-execution-checkpoint' AS BLOB),
        CAST('review-checkpoint-application' AS BLOB)
    )
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source replacement would hide a mutation');
END;
CREATE TRIGGER review_checkpoint_source_insert AFTER INSERT ON dedupe_keys
WHEN CAST(NEW.namespace AS BLOB) IN (
    CAST('review-execution-origin' AS BLOB),
    CAST('review-execution-checkpoint' AS BLOB),
    CAST('review-checkpoint-application' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='checkpoint_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='checkpoint_sources' AND scope=X'';
END;
CREATE TRIGGER review_checkpoint_source_update AFTER UPDATE ON dedupe_keys
WHEN CAST(OLD.namespace AS BLOB) IN (
    CAST('review-execution-origin' AS BLOB),
    CAST('review-execution-checkpoint' AS BLOB),
    CAST('review-checkpoint-application' AS BLOB)
) OR CAST(NEW.namespace AS BLOB) IN (
    CAST('review-execution-origin' AS BLOB),
    CAST('review-execution-checkpoint' AS BLOB),
    CAST('review-checkpoint-application' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='checkpoint_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='checkpoint_sources' AND scope=X'';
END;
CREATE TRIGGER review_checkpoint_source_delete AFTER DELETE ON dedupe_keys
WHEN CAST(OLD.namespace AS BLOB) IN (
    CAST('review-execution-origin' AS BLOB),
    CAST('review-execution-checkpoint' AS BLOB),
    CAST('review-checkpoint-application' AS BLOB)
)
BEGIN
    SELECT RAISE(ABORT,'checkpoint source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='checkpoint_sources' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='checkpoint_sources' AND scope=X'';
END;
CREATE TRIGGER review_workflow_source_insert AFTER INSERT ON workflows
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_workflow_source_update AFTER UPDATE ON workflows
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_workflow_source_delete AFTER DELETE ON workflows
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_component_source_insert AFTER INSERT ON workflow_components
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_component_source_update AFTER UPDATE ON workflow_components
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
CREATE TRIGGER review_component_source_delete AFTER DELETE ON workflow_components
BEGIN
    SELECT RAISE(ABORT,'workflow source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='workflow_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='workflow_rows' AND scope=X'';
END;
"""),
    SchemaMigration(31, """
INSERT INTO review_archive_source_versions(source,scope,version)
VALUES ('lifecycle_rows',X'',0);
CREATE TRIGGER review_archive_source_version_overflow
BEFORE UPDATE OF version ON review_archive_source_versions
WHEN typeof(NEW.version)!='integer'
    OR NEW.version NOT BETWEEN 0 AND 9223372036854775806
BEGIN
    SELECT RAISE(ABORT,'archive source witness version overflow');
END;
CREATE TRIGGER review_lifecycle_command_source_insert AFTER INSERT ON lifecycle_commands
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_command_source_update AFTER UPDATE ON lifecycle_commands
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_command_source_delete AFTER DELETE ON lifecycle_commands
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_effect_source_insert AFTER INSERT ON effect_receipts
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_effect_source_update AFTER UPDATE ON effect_receipts
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
CREATE TRIGGER review_lifecycle_effect_source_delete AFTER DELETE ON effect_receipts
BEGIN
    SELECT RAISE(ABORT,'lifecycle source witness is absent')
    WHERE NOT EXISTS (
        SELECT 1 FROM review_archive_source_versions
        WHERE source='lifecycle_rows' AND scope=X''
    );
    UPDATE review_archive_source_versions SET version=version+1
    WHERE source='lifecycle_rows' AND scope=X'';
END;
"""),
    SchemaMigration(32, _review_execution_lineage_migration()),
    SchemaMigration(33, """
CREATE INDEX review_execution_rounds_generation_pending
ON review_execution_rounds(repository,target_number,execution_epoch,execution_episode,generation,sequence);
"""),
    SchemaMigration(34, """
DROP TRIGGER review_all_round_source_delete;
DROP TRIGGER review_all_round_source_insert;
DROP TRIGGER review_all_round_source_update;
DROP TRIGGER review_archive_dedupe_minus_one_insert;
DROP TRIGGER review_archive_dedupe_no_replace_insert;
DROP TRIGGER review_archive_dedupe_no_replace_update;
DROP TRIGGER review_archive_source_version_overflow;
DROP TRIGGER review_checkpoint_source_delete;
DROP TRIGGER review_checkpoint_source_insert;
DROP TRIGGER review_checkpoint_source_minus_one_insert;
DROP TRIGGER review_checkpoint_source_no_replace_insert;
DROP TRIGGER review_checkpoint_source_no_replace_update;
DROP TRIGGER review_checkpoint_source_update;
DROP TRIGGER review_component_source_delete;
DROP TRIGGER review_component_source_insert;
DROP TRIGGER review_component_source_update;
DROP TRIGGER review_generation_source_delete;
DROP TRIGGER review_generation_source_insert;
DROP TRIGGER review_generation_source_update;
DROP TRIGGER review_identity_certifications_delete;
DROP TRIGGER review_identity_certifications_insert;
DROP TRIGGER review_identity_certifications_update;
DROP TRIGGER review_identity_members_delete;
DROP TRIGGER review_identity_members_insert;
DROP TRIGGER review_identity_members_update;
DROP TRIGGER review_lifecycle_command_source_delete;
DROP TRIGGER review_lifecycle_command_source_insert;
DROP TRIGGER review_lifecycle_command_source_update;
DROP TRIGGER review_lifecycle_effect_source_delete;
DROP TRIGGER review_lifecycle_effect_source_insert;
DROP TRIGGER review_lifecycle_effect_source_update;
DROP TRIGGER review_lineage_budget_reservations_delete;
DROP TRIGGER review_lineage_budget_reservations_insert;
DROP TRIGGER review_lineage_budget_reservations_update;
DROP TRIGGER review_lineage_charges_delete;
DROP TRIGGER review_lineage_charges_insert;
DROP TRIGGER review_lineage_charges_update;
DROP TRIGGER review_lineage_dedupe_keys_delete;
DROP TRIGGER review_lineage_dedupe_keys_insert;
DROP TRIGGER review_lineage_dedupe_keys_update;
DROP TRIGGER review_lineage_dispatch_claims_delete;
DROP TRIGGER review_lineage_dispatch_claims_insert;
DROP TRIGGER review_lineage_dispatch_claims_update;
DROP TRIGGER review_lineage_leases_delete;
DROP TRIGGER review_lineage_leases_insert;
DROP TRIGGER review_lineage_leases_update;
DROP TRIGGER review_lineage_quota_debits_delete;
DROP TRIGGER review_lineage_quota_debits_insert;
DROP TRIGGER review_lineage_quota_debits_update;
DROP TRIGGER review_lineage_reservation_limits_delete;
DROP TRIGGER review_lineage_reservation_limits_insert;
DROP TRIGGER review_lineage_reservation_limits_update;
DROP TRIGGER review_lineage_reservation_recovery_delete;
DROP TRIGGER review_lineage_reservation_recovery_insert;
DROP TRIGGER review_lineage_reservation_recovery_update;
DROP TRIGGER review_lineage_run_cancellation_intents_delete;
DROP TRIGGER review_lineage_run_cancellation_intents_insert;
DROP TRIGGER review_lineage_run_cancellation_intents_update;
DROP TRIGGER review_lineage_runs_delete;
DROP TRIGGER review_lineage_runs_insert;
DROP TRIGGER review_lineage_runs_update;
DROP TRIGGER review_lineage_workflow_evidence_delete;
DROP TRIGGER review_lineage_workflow_evidence_insert;
DROP TRIGGER review_lineage_workflow_evidence_update;
DROP TRIGGER review_receipt_source_delete;
DROP TRIGGER review_receipt_source_insert;
DROP TRIGGER review_receipt_source_update;
DROP TRIGGER review_round_source_delete;
DROP TRIGGER review_round_source_insert;
DROP TRIGGER review_round_source_no_replace_insert;
DROP TRIGGER review_round_source_no_replace_update;
DROP TRIGGER review_round_source_update;
DROP TRIGGER review_workflow_round_source_insert;
DROP TRIGGER review_workflow_source_delete;
DROP TRIGGER review_workflow_source_insert;
DROP TRIGGER review_workflow_source_update;
DROP TABLE review_archive_identity_members;
DROP TABLE review_archive_identity_certifications;
DROP TABLE review_archive_source_versions;
"""),
    SchemaMigration(35, """
CREATE INDEX workflow_gates_current
    ON workflow_gates(repository,target_kind,target_number,gate_id)
    WHERE state<>'closed';
"""),
    SchemaMigration(36, """
CREATE TABLE workflow_components_v36 (
    repository TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_number INTEGER NOT NULL CHECK (target_number > 0),
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch > 0),
    episode_id TEXT NOT NULL CHECK (length(episode_id) BETWEEN 1 AND 512),
    PRIMARY KEY (repository,target_kind,target_number,family),
    FOREIGN KEY (repository,target_kind,target_number)
        REFERENCES workflows(repository,target_kind,target_number),
    CHECK ((family IN ('resolve','issue_recovery','steward') AND target_kind='issue') OR
           (family IN ('review_rejections','review_execution','pr_repair') AND target_kind='pr'))
);
INSERT INTO workflow_components_v36 SELECT * FROM workflow_components;
DROP TABLE workflow_components;
ALTER TABLE workflow_components_v36 RENAME TO workflow_components;
CREATE INDEX workflow_components_family ON workflow_components(repository,family);
ALTER TABLE runs ADD COLUMN continuations_json TEXT NOT NULL DEFAULT '{"format":1,"value":[]}'
    CHECK (length(CAST(continuations_json AS BLOB)) BETWEEN 1 AND 262144);
CREATE TRIGGER runs_continuation_selections_update BEFORE UPDATE OF continuations_json ON runs
WHEN NEW.continuations_json IS NOT OLD.continuations_json
BEGIN SELECT RAISE(ABORT,'run continuation selections are immutable'); END;
CREATE TRIGGER runs_continuation_identity_update BEFORE UPDATE ON runs
WHEN OLD.continuations_json <> '{"format":1,"value":[]}' AND (
    NEW.workflow_binding_json IS NOT OLD.workflow_binding_json OR
    NEW.workflow_episode_key IS NOT OLD.workflow_episode_key OR
    NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.owner_id IS NOT OLD.owner_id OR
    NEW.kind IS NOT OLD.kind OR NEW.config_version IS NOT OLD.config_version)
BEGIN SELECT RAISE(ABORT,'continuation run identity is immutable'); END;
"""),
    SchemaMigration(37, f"""
CREATE INDEX runs_fix_issue_dispatch ON runs({_FIX_DISPATCH_REPOSITORY},{_FIX_DISPATCH_ISSUE})
WHERE {_FIX_ISSUE_DISPATCH};
"""),
    SchemaMigration(38, """
CREATE INDEX lifecycle_self_clear_pending ON lifecycle_commands(repository,target)
WHERE state IN ('pending','executing','retryable_failure','unknown_outcome')
    AND CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$.self_clear') END='text';
"""),
    SchemaMigration(39, f"""
CREATE INDEX workflow_repair_owed ON workflows(repository,target_kind,target_number)
WHERE {_REPAIR_CURRENT_OWED};
CREATE INDEX continuation_pr_admission_identity
ON dedupe_keys(scope,json_extract(payload_json,'{_REPAIR_BINDING}'),json_extract(payload_json,'{_REPAIR_VERSION}'))
WHERE {_REPAIR_ADMISSION};
CREATE INDEX reservations_active_workload ON budget_reservations(workload_repository,run_id)
WHERE state='active';
"""),
    SchemaMigration(40, f"""
CREATE INDEX event_outbox_pending ON event_outbox(outbox_id)
WHERE emitted_at IS NULL AND failed_at IS NULL;
CREATE INDEX event_outbox_pending_destination ON event_outbox(destination,outbox_id)
WHERE emitted_at IS NULL AND failed_at IS NULL;
CREATE INDEX lifecycle_review_generation_candidates ON lifecycle_commands(repository,target)
WHERE workflow_binding_json IS NOT NULL AND {_REVIEW_GENERATION_CANDIDATE};
"""),
    SchemaMigration(41, """
ALTER TABLE charges ADD COLUMN origin TEXT NOT NULL DEFAULT 'normal_return'
CHECK (origin IN ('normal_return','recovery_estimate'));
UPDATE charges SET origin='recovery_estimate'
WHERE provider_receipt_id='terminal-estimate:' || run_id
    OR (basis IN ('estimate','reconciled') AND provider_receipt_id LIKE 'codex-recovered:%'
        AND substr(provider_receipt_id,-length(run_id)-1)=':' || run_id);
"""),
    # Review-time test evidence is retired by deletion, never parsed. An
    # unresolved approval whose payload still names `test_evidence_ref` is
    # forgotten with its unclosed round registration; GitHub stays authority
    # and the reopened round is abandoned by recovery, so the head is reviewed
    # afresh.
    SchemaMigration(42, """
CREATE TEMP TABLE retired_review_submissions AS
    SELECT c.repository,c.command_id,json_extract(d.payload_json,'$.value.fields.round.fields.round_id') AS round_id
    FROM lifecycle_commands c LEFT JOIN dedupe_keys d
        ON d.namespace='review-round-submission' AND d.scope=c.repository AND d.key=c.command_id
    WHERE c.kind='submit_review' AND json_type(c.payload_json,'$.test_evidence_ref') IS NOT NULL
        AND (c.state IN ('pending','executing','retryable_failure','unknown_outcome')
            OR c.state='succeeded' AND NOT EXISTS (
                SELECT 1 FROM dedupe_keys s WHERE s.scope=c.repository AND s.key=c.command_id
                    AND s.namespace IN ('review-recorded-submission','review-submission-settled')))
        AND NOT EXISTS (SELECT 1 FROM review_execution_rounds r
            WHERE r.round_id=json_extract(d.payload_json,'$.value.fields.round.fields.round_id')
                AND (r.closure_json IS NOT NULL OR r.outcome_json IS NOT NULL));
CREATE TEMP TABLE retired_review_gates AS
    SELECT g.gate_id FROM workflow_gates g
    WHERE g.target_kind='pr' AND (
        g.gate_id LIKE 'ordinary-test:%'
        OR COALESCE(
            json_extract(g.payload_json,'$.value.fields.cause.fields.submission_command_id'),
            json_extract(g.payload_json,'$.value.fields.gate.fields.cause.fields.submission_command_id')
        ) IN (
            SELECT command_id FROM lifecycle_commands
            WHERE json_extract(payload_json,'$.event')='REQUEST_CHANGES'
                AND json_extract(payload_json,'$.test_evidence_ref') IS NOT NULL
        )
    );
DELETE FROM workflow_retry_authorizations WHERE gate_id IN (SELECT gate_id FROM retired_review_gates);
DELETE FROM workflow_retry_authorizations WHERE request_id IN (
    SELECT request_id FROM workflow_rearms WHERE gate_id IN (SELECT gate_id FROM retired_review_gates)
);
DELETE FROM workflow_rearm_commands WHERE request_id IN (
    SELECT request_id FROM workflow_rearms WHERE gate_id IN (SELECT gate_id FROM retired_review_gates)
);
DELETE FROM workflow_rearms WHERE gate_id IN (SELECT gate_id FROM retired_review_gates);
DELETE FROM workflow_gates WHERE gate_id IN (SELECT gate_id FROM retired_review_gates);
DROP TABLE retired_review_gates;
UPDATE review_execution_rounds SET
    plan_json=json_set(plan_json,'$.value.fields.submissions',json((
        SELECT json_group_array(json(s.value)) FROM json_each(plan_json,'$.value.fields.submissions') s
        WHERE json_extract(s.value,'$.fields.command_id') NOT IN (SELECT command_id FROM retired_review_submissions)
    ))),
    planning_json='{"format":1,"value":{"fields":{},"type":"ReviewPlanningOpen"}}'
WHERE round_id IN (SELECT round_id FROM retired_review_submissions);
DELETE FROM dedupe_keys WHERE namespace IN ('review-round-seal','review-round-abandonment',
    'review-round-recovery-wait','review-round-recovery-block')
    AND (scope,key) IN (SELECT repository,round_id FROM retired_review_submissions);
DELETE FROM dedupe_keys WHERE namespace IN ('review-round-submission','review-round-submission-index')
    AND (scope,json_extract(payload_json,'$.value.fields.submission.fields.command_id'))
        IN (SELECT repository,command_id FROM retired_review_submissions);
DELETE FROM workflow_retry_authorizations WHERE command_id IN (SELECT command_id FROM retired_review_submissions);
DELETE FROM workflow_rearm_commands WHERE command_id IN (SELECT command_id FROM retired_review_submissions);
DELETE FROM workflow_evidence WHERE command_id IN (SELECT command_id FROM retired_review_submissions);
DELETE FROM effect_receipts WHERE command_id IN (SELECT command_id FROM retired_review_submissions);
DELETE FROM lifecycle_commands WHERE command_id IN (SELECT command_id FROM retired_review_submissions);
DROP TABLE retired_review_submissions;
DELETE FROM dedupe_keys WHERE namespace IN (
    'review-round-test-intent', 'review-round-test-invocation',
    'review-round-test-completion', 'review-round-test-evidence'
);
"""),
    # Self-clear is retired by deletion. Its relabels, persisted with a
    # `self_clear` key, would otherwise drain as plain relabels and release
    # ai-blocked without a human reply; GitHub stays authority, so the labels
    # they left are what the next tick reads.
    SchemaMigration(43, """
CREATE TEMP TABLE retired_self_clears AS
    SELECT command_id FROM lifecycle_commands
    WHERE CASE WHEN json_valid(payload_json) THEN json_type(payload_json,'$.self_clear') END='text';
DELETE FROM workflow_retry_authorizations WHERE command_id IN (SELECT command_id FROM retired_self_clears)
    OR evidence_id IN (SELECT evidence_id FROM workflow_evidence
        WHERE command_id IN (SELECT command_id FROM retired_self_clears));
DELETE FROM workflow_rearm_commands WHERE command_id IN (SELECT command_id FROM retired_self_clears);
DELETE FROM workflow_evidence WHERE command_id IN (SELECT command_id FROM retired_self_clears);
DELETE FROM effect_receipts WHERE command_id IN (SELECT command_id FROM retired_self_clears);
DELETE FROM lifecycle_commands WHERE command_id IN (SELECT command_id FROM retired_self_clears);
DROP TABLE retired_self_clears;
DROP INDEX lifecycle_self_clear_pending;
DELETE FROM dedupe_keys WHERE namespace='fix-budget-refusal';
"""),
    # The steward role is deleted. Its family authority, components, commands,
    # dispatch claims, run admissions/returns and one-shot keys are retired by
    # deletion, never parsed; issue aggregates drop their steward component.
    # Settled steward runs and charges stay. No steward daemon remains to
    # reconcile its own unfinished runs: never-launched ones (spawn_pending)
    # become cancelled/unlaunched_released, as every never-launched release
    # does; completed-unreconciled ones get the stale-hold-release treatment
    # (hold released, outcome spend_unmeasured_released). Other unfinished runs
    # are abandoned. Pending telemetry has no flusher.
    SchemaMigration(44, """
CREATE TEMP TABLE retired_steward_commands AS
    SELECT command_id FROM lifecycle_commands
    WHERE kind IN ('add_dependency','create_jira_issue','transition_jira_issue')
        OR instr(payload_json,'"steward_guard"')>0
        OR idempotency_key LIKE 'steward-verdict:%' OR logical_key LIKE 'steward-verdict:%'
        OR executor_identity LIKE '%:steward' OR executor_identity LIKE '%:steward:%';
DELETE FROM workflow_retry_authorizations WHERE command_id IN (SELECT command_id FROM retired_steward_commands);
DELETE FROM workflow_rearm_commands WHERE command_id IN (SELECT command_id FROM retired_steward_commands);
DELETE FROM workflow_evidence WHERE command_id IN (SELECT command_id FROM retired_steward_commands);
DELETE FROM effect_receipts WHERE command_id IN (SELECT command_id FROM retired_steward_commands);
DELETE FROM lifecycle_commands WHERE command_id IN (SELECT command_id FROM retired_steward_commands);
DROP TABLE retired_steward_commands;
DELETE FROM dedupe_keys WHERE namespace IN ('continuation-admission','continuation-return')
    AND key LIKE '%:steward';
DELETE FROM dedupe_keys WHERE namespace IN ('groomed','triage_close','recurrence');
DELETE FROM workflow_components WHERE family='steward';
DELETE FROM workflow_activation_imports WHERE family='steward';
DELETE FROM workflow_activation_evidence WHERE family='steward';
DELETE FROM workflow_authority_epochs WHERE family='steward';
DELETE FROM dispatch_claim_runs WHERE role='steward';
DELETE FROM dispatch_claims WHERE role='steward';
CREATE TEMP TABLE retired_steward_runs AS
    SELECT run_id FROM runs WHERE owner_id LIKE '%:steward' OR owner_id LIKE '%:steward:%';
UPDATE runs SET state='cancelled', outcome='unlaunched_released',
    completed_at=COALESCE(completed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
WHERE run_id IN (SELECT rr.run_id FROM reservation_recovery rr JOIN retired_steward_runs USING(run_id)
                 JOIN runs r USING(run_id) JOIN budget_reservations b USING(run_id)
                 WHERE rr.policy='spawn_pending' AND b.state='active' AND r.provider_receipt_id IS NULL AND r.started_at IS NULL
                 AND r.state NOT IN ('completed','cancelled','abandoned'));
UPDATE runs SET outcome='spend_unmeasured_released',
    state=CASE WHEN state IN ('completed','cancelled','abandoned') THEN state ELSE 'abandoned' END,
    completed_at=COALESCE(completed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
WHERE run_id IN (SELECT b.run_id FROM budget_reservations b JOIN retired_steward_runs USING(run_id)
                 WHERE b.state='active')
    AND NOT(state='cancelled' AND outcome IS 'unlaunched_released');
UPDATE runs SET state='abandoned', outcome=COALESCE(outcome,'dead_lease'),
    completed_at=COALESCE(completed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
WHERE run_id IN (SELECT run_id FROM retired_steward_runs)
    AND state NOT IN ('completed','cancelled','abandoned');
UPDATE budget_reservations SET state='released', release_reason='steward role retired before settlement',
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE state='active' AND run_id IN (SELECT run_id FROM retired_steward_runs);
DELETE FROM leases WHERE run_id IN (SELECT run_id FROM retired_steward_runs);
DROP TABLE retired_steward_runs;
DELETE FROM event_outbox WHERE emitted_at IS NULL AND destination LIKE '%:steward';
UPDATE workflows SET episode_json=CASE
    WHEN json_extract(episode_json,'$.value.type')='IssueFamilyEpisodes'
        AND json_extract(episode_json,'$.value.fields.issue_recovery.type')='InactiveIssueRecovery'
    THEN json_object('format',1,'value',json_array(
        json_extract(episode_json,'$.value.fields.authority'),
        json_extract(episode_json,'$.value.fields.episode')))
    ELSE json_remove(episode_json,'$.value.fields.steward') END
WHERE json_extract(episode_json,'$.value.type') IN ('IssueFamilyEpisodes','IssueWorkflowShell')
    AND json_type(episode_json,'$.value.fields.steward') IS NOT NULL;
"""),
    # The author/implement/adjudicate split is retired, never parsed. Its
    # reset evidence is forgotten, and an authored checkpoint becomes a fresh
    # continuation on the same branch. Split legs were resolve attempts (the
    # old backoff counted them as RESOLVE), so their runs and receipts are
    # relabelled RESOLVE in every place that records the stage: deleting them
    # would break the latest-admission chain, since the paid runs (and their
    # immutable episode key and spend) must stay. The continuation identity
    # trigger is lifted only around that relabel. The always-encoded split
    # fields leave every surviving resolve record.
    SchemaMigration(45, """
DELETE FROM dedupe_keys WHERE namespace IN (
    'workflow-split-consumed', 'workflow-contract-reset',
    'workflow-contract-reset-completed', 'workflow-return-contract'
);
UPDATE dedupe_keys SET payload_json=json_set(payload_json,'$.value.fields.stage.value','RESOLVE')
WHERE namespace IN ('workflow-admission','workflow-work-latest','workflow-result','workflow-return')
    AND json_extract(payload_json,'$.value.fields.stage.value') IN ('AUTHOR','IMPLEMENT','ADJUDICATE');
UPDATE dedupe_keys SET payload_json=json_set(payload_json,'$.value.fields.result.fields.stage.value','RESOLVE')
WHERE namespace='workflow-return-disposition'
    AND json_extract(payload_json,'$.value.fields.result.fields.stage.value') IN ('AUTHOR','IMPLEMENT','ADJUDICATE');
UPDATE dedupe_keys SET payload_json=json_set(payload_json,'$.value.fields.returned.fields.stage.value','RESOLVE')
WHERE namespace='workflow-return-observation'
    AND json_extract(payload_json,'$.value.fields.returned.fields.stage.value') IN ('AUTHOR','IMPLEMENT','ADJUDICATE');
UPDATE leases SET dispatch_id=json_set(dispatch_id,'$.kind','RESOLVE')
WHERE json_valid(dispatch_id)
    AND run_id IN (SELECT run_id FROM runs WHERE kind IN ('AUTHOR','IMPLEMENT','ADJUDICATE'));
UPDATE run_cancellation_intents SET kind='RESOLVE',
    dispatch_id=CASE WHEN json_valid(dispatch_id) THEN json_set(dispatch_id,'$.kind','RESOLVE') ELSE dispatch_id END
WHERE kind IN ('AUTHOR','IMPLEMENT','ADJUDICATE');
DROP TRIGGER runs_continuation_identity_update;
UPDATE runs SET kind='RESOLVE',
    dispatch_id=CASE WHEN json_valid(dispatch_id) THEN json_set(dispatch_id,'$.kind','RESOLVE') ELSE dispatch_id END
WHERE kind IN ('AUTHOR','IMPLEMENT','ADJUDICATE');
CREATE TRIGGER runs_continuation_identity_update BEFORE UPDATE ON runs
WHEN OLD.continuations_json <> '{"format":1,"value":[]}' AND (
    NEW.workflow_binding_json IS NOT OLD.workflow_binding_json OR
    NEW.workflow_episode_key IS NOT OLD.workflow_episode_key OR
    NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.owner_id IS NOT OLD.owner_id OR
    NEW.kind IS NOT OLD.kind OR NEW.config_version IS NOT OLD.config_version)
BEGIN SELECT RAISE(ABORT,'continuation run identity is immutable'); END;
UPDATE dedupe_keys SET payload_json=json_remove(payload_json,
    '$.value.fields.adjudication_ceiling','$.value.fields.split_fingerprint')
WHERE namespace IN ('workflow-admission','workflow-work-latest')
    AND json_extract(payload_json,'$.value.type')='ResolveAdmission';
UPDATE dedupe_keys SET payload_json=json_remove(payload_json,'$.value.fields.split')
WHERE namespace='workflow-result' AND json_extract(payload_json,'$.value.type')='ResolveAttemptResult';
UPDATE dedupe_keys SET payload_json=json_remove(payload_json,'$.value.fields.result.fields.split')
WHERE namespace='workflow-return-disposition'
    AND json_extract(payload_json,'$.value.type')='FinalizedResolveReturn';
UPDATE dedupe_keys SET payload_json=json_remove(payload_json,
    '$.value.fields.split_input','$.value.fields.verdict',
    '$.value.fields.rationale','$.value.fields.contract_revision')
WHERE namespace='workflow-return' AND json_extract(payload_json,'$.value.type')='ReturnedResolve';
UPDATE dedupe_keys SET payload_json=json_remove(payload_json,
    '$.value.fields.returned.fields.split_input','$.value.fields.returned.fields.verdict',
    '$.value.fields.returned.fields.rationale','$.value.fields.returned.fields.contract_revision')
WHERE namespace='workflow-return-observation'
    AND json_extract(payload_json,'$.value.type')='PendingResolveReturn';
UPDATE workflows SET episode_json=json_set(episode_json,'$.value.fields.episode.fields.continuation',
    json_object('fields', json_object(
        'last_applied_admission_version',
        json_extract(episode_json,'$.value.fields.episode.fields.continuation.fields.last_applied_admission_version'),
        'session', json('null')), 'type', 'FreshResolve'))
WHERE json_extract(episode_json,'$.value.fields.episode.fields.continuation.type')='AuthoredResolve';
UPDATE workflows SET episode_json=json_set(episode_json,'$.value[1].fields.continuation',
    json_object('fields', json_object(
        'last_applied_admission_version',
        json_extract(episode_json,'$.value[1].fields.continuation.fields.last_applied_admission_version'),
        'session', json('null')), 'type', 'FreshResolve'))
WHERE json_type(episode_json,'$.value')='array'
    AND json_extract(episode_json,'$.value[1].fields.continuation.type')='AuthoredResolve';
DROP INDEX runs_adjudication_episode;
"""),
    # Slice 8a: telemetry no longer passes through SQLite. The outbox, the
    # world-observation dedupe and the reporters' level state lost their only
    # writers, and these dedupe namespaces only deduplicated telemetry lines.
    SchemaMigration(46, """
DROP TABLE event_outbox;
DROP TABLE world_observations;
DROP TABLE level_state;
DELETE FROM dedupe_keys WHERE namespace IN ('measurement-observation','path-prediction',
    'ci-pending','duplicate-pr','ci-flake','world-feed-retirement','native-remetered');
"""),
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
        self._bound_owner_id: str | None = None


    def initialize(self) -> None:
        parent = os.path.dirname(self.path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, stat.S_IRWXU)
        with self._initialization_lock():
            witness_exists = self._authority_witness_exists()
            database_exists = self._database_exists(
                allow_empty=not witness_exists
            )
            if witness_exists:
                self._validate_authority_witness()
                if not database_exists:
                    raise CorruptControlStore(
                        "control-store authority database disappeared after "
                        "initialization"
                    )
            if database_exists:
                self.integrity_check()
            with closing(self._connect(create=not database_exists)) as db, db:
                applied = self._migration_history(db)
                if not applied and self._has_user_schema(db):
                    raise IncompatibleSchema(
                        "historyless nonempty control store cannot be adopted"
                    )
            self._validate_migration_history(applied)
            for migration in _SCHEMA_MIGRATIONS:
                self._apply_schema_migration(migration)
            with closing(self._connect()) as db, db:
                final_history = self._migration_history(db)
            self._validate_migration_history(
                final_history, require_current=True
            )
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            self.integrity_check()
            if not witness_exists:
                self._write_authority_witness()

    @property
    def authority_witness_path(self) -> str:
        return f"{self.path}.authority"


    def _database_exists(self, *, allow_empty: bool = False) -> bool:
        try:
            metadata = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            raise CorruptControlStore(
                f"control-store path is not a regular file: {self.path}"
            )
        if metadata.st_size <= 0:
            if allow_empty:
                return False
            raise CorruptControlStore(
                f"control-store authority database is empty: {self.path}"
            )
        return True

    def _authority_witness_exists(self) -> bool:
        try:
            os.stat(self.authority_witness_path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _validate_authority_witness(self) -> None:
        path = self.authority_witness_path
        try:
            metadata = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise CorruptControlStore(
                    f"unsafe control-store authority witness: {path}"
                )
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                encoded = os.read(fd, 256)
                if os.read(fd, 1):
                    raise CorruptControlStore(
                        "control-store authority witness is oversized"
                    )
            finally:
                os.close(fd)
        except OSError as exc:
            raise CorruptControlStore(
                f"cannot read control-store authority witness: {exc}"
            ) from exc
        if encoded.decode(errors="replace").strip() != AUTHORITY_WITNESS_FORMAT:
            raise CorruptControlStore(
                "control-store authority witness has an unknown format"
            )

    def _write_authority_witness(self) -> None:
        path = self.authority_witness_path
        parent = os.path.dirname(path)
        prefix = f".{os.path.basename(path)}."
        fd, temporary = tempfile.mkstemp(prefix=prefix, dir=parent)
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            encoded = f"{AUTHORITY_WITNESS_FORMAT}\n".encode()
            written = 0
            while written < len(encoded):
                written += os.write(fd, encoded[written:])
            os.fsync(fd)
            os.close(fd)
            fd = -1
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                self._validate_authority_witness()
            directory_fd = os.open(
                parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        """Serialize WAL selection and schema migration across role processes."""

        path = f"{self.path}.initialize.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise ControlStoreError(
                    f"unsafe control-store initialization lock: {path}"
                )
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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

    @staticmethod
    def _has_user_schema(db: sqlite3.Connection) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            is not None
        )

    def _apply_schema_migration(self, migration: SchemaMigration) -> None:
        applied = False
        with closing(self._connect()) as db, db:
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
                if migration.version == 20:
                    _backfill_workflow_episode_index_v20(db)
                if migration.version == 25:
                    _backfill_workflow_components_v25(db)
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
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise IncompatibleSchema(
                    f"control-store migration {migration.version} found "
                    "conflicting authority rows; operator repair is required"
                ) from exc
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        if applied and self._migration_observer is not None:
            self._migration_observer(migration.version, "committed")

    def integrity_check(self) -> None:
        if not self._database_exists():
            raise CorruptControlStore(
                "control-store authority database does not exist"
            )
        try:
            with closing(self._connect()) as db, db:
                result = db.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise CorruptControlStore(
                        f"control-store integrity check failed: {result}"
                    )
                history = self._migration_history(db)
                self._validate_migration_history(history)
                if history and history[-1][0] == SCHEMA_VERSION:
                    self._assert_semantic_integrity(db)
        except sqlite3.DatabaseError as exc:
            raise CorruptControlStore(f"control-store integrity check failed: {exc}") from exc

    @staticmethod
    def _assert_semantic_integrity(db: sqlite3.Connection) -> None:
        foreign_key_error = db.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise CorruptControlStore(
                "control-store foreign-key integrity check failed: "
                f"{tuple(foreign_key_error)}"
            )

        violations = (
            (
                "never-launched release contradicts execution or accounting evidence",
                """
                SELECT r.run_id FROM runs r
                LEFT JOIN budget_reservations b USING(run_id)
                LEFT JOIN reservation_recovery rr USING(run_id)
                WHERE r.outcome='unlaunched_released' AND (
                    r.state != 'cancelled' OR r.provider_receipt_id IS NOT NULL
                    OR b.state IS NOT 'released' OR rr.policy IS NOT 'spawn_pending'
                    OR EXISTS(SELECT 1 FROM charges c WHERE c.run_id=r.run_id)
                    OR EXISTS(SELECT 1 FROM leases l WHERE l.run_id=r.run_id)
                ) LIMIT 1
                """,
            ),
            (
                "active reservation recovery classification is absent",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                LEFT JOIN reservation_recovery AS rr ON rr.run_id=r.run_id
                WHERE r.state='active' AND rr.run_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "reservation primary limit is absent or inconsistent",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                WHERE NOT EXISTS (
                    SELECT 1 FROM reservation_limits AS l
                    WHERE l.run_id=r.run_id
                      AND l.repository=r.repository
                      AND l.dimension=r.dimension
                      AND l.period_id=r.period_id
                      AND l.period_start=r.period_start
                      AND l.period_end=r.period_end
                      AND l.period_timezone=r.period_timezone
                      AND l.currency=r.currency
                      AND l.cap_micros=r.cap_micros
                )
                LIMIT 1
                """,
            ),
            (
                "reservation limits use a conflicting currency",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                JOIN reservation_limits AS l ON l.run_id=r.run_id
                WHERE l.currency<>r.currency
                LIMIT 1
                """,
            ),
            (
                "workload repository conflicts with reservation limits",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                JOIN reservation_limits AS l ON l.run_id=r.run_id
                WHERE l.repository<>'*'
                  AND l.repository<>r.workload_repository
                LIMIT 1
                """,
            ),
            (
                "reservation settlement and charge disagree",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                LEFT JOIN charges AS c ON c.run_id=r.run_id
                GROUP BY r.run_id, r.state
                HAVING (r.state='settled' AND COUNT(c.charge_id)<>1)
                    OR (r.state<>'settled' AND COUNT(c.charge_id)<>0)
                LIMIT 1
                """,
            ),
            (
                "charge and reservation currencies disagree",
                """
                SELECT r.run_id
                FROM budget_reservations AS r
                JOIN charges AS c ON c.run_id=r.run_id
                WHERE r.currency<>c.currency
                LIMIT 1
                """,
            ),
            (
                "run and charge provider receipts disagree",
                """
                SELECT r.run_id
                FROM runs AS r
                JOIN charges AS c ON c.run_id=r.run_id
                WHERE r.provider_receipt_id IS NULL
                   OR r.provider_receipt_id<>c.provider_receipt_id
                LIMIT 1
                """,
            ),
            (
                "retired owner scope disagrees with its successor",
                """
                SELECT retired.owner_id
                FROM retired_owners AS retired
                JOIN retired_owners AS successor
                  ON successor.owner_id=retired.successor_owner_id
                WHERE retired.repository<>successor.repository
                   OR retired.role<>successor.role
                LIMIT 1
                """,
            ),
        )
        for detail, query in violations:
            row = db.execute(query).fetchone()
            if row is not None:
                raise CorruptControlStore(
                    f"control-store semantic integrity failed: {detail} "
                    f"({row[0]})"
                )

        # A run bound to a claim belongs to the claim's owner or to a retired
        # predecessor of it.
        for row in db.execute(
            "SELECT r.run_id,r.owner_id,dc.repository,dc.owner_id AS successor "
            "FROM dispatch_claim_runs dcr JOIN dispatch_claims dc "
            "ON dc.repository=dcr.repository AND dc.role=dcr.role AND dc.target=dcr.target "
            "JOIN runs r ON r.run_id=dcr.run_id WHERE dc.owner_id<>r.owner_id"
        ):
            if row["owner_id"] not in SQLiteControlStore._owner_lineage(
                    db, canonical_repository(row["repository"]), row["successor"]):
                raise CorruptControlStore(
                    f"dispatch claim and bound run owners disagree ({row['run_id']})"
                )

    @staticmethod
    def _assert_owner_active(
        db: sqlite3.Connection,
        owner_id: str,
    ) -> None:
        row = db.execute(
            "SELECT successor_owner_id FROM retired_owners WHERE owner_id=?",
            (owner_id,),
        ).fetchone()
        if row is not None:
            raise RetiredOwnerError(
                f"deployment owner {owner_id!r} was retired in favor of "
                f"{row['successor_owner_id']!r}"
            )

    def assert_owner_active(self, owner_id: str) -> None:
        """Fail when a deployment identity has permanently lost authority."""

        if not owner_id:
            raise ValueError("deployment owner identity is required")
        with closing(self._connect()) as db, db:
            self._assert_owner_active(db, owner_id)

    def bind_active_owner(self, owner_id: str) -> None:
        """Fence every subsequent write made through this role's store."""

        self.assert_owner_active(owner_id)
        self._bound_owner_id = owner_id

    def adopt_legacy_owner(
        self,
        repository: str,
        role: str,
        owner_id: str,
        *,
        prior_owner_ids: tuple[str, ...] = (),
    ) -> int:
        """Atomically move pre-deployment ownership into the first upgrader.

        Older schemas scoped all same-repository role daemons as
        ``<repository>:<role>``. The first deployment to boot after the
        deployment-identity cutover adopts that legacy scope, preserving active
        run recovery. Returns the number of runs adopted.
        """

        legacy = f"{repository}:{role}"
        if not repository or not role or not owner_id:
            raise ValueError("legacy owner adoption identity is incomplete")
        if _deployment_role(repository, owner_id) != role:
            raise ValueError("legacy owner adoption role does not match owner")
        prior = tuple(
            candidate
            for candidate in dict.fromkeys((legacy, *prior_owner_ids))
            if candidate and candidate != owner_id
        )
        if not prior:
            return 0
        if any(_deployment_role(repository, f"{canonical_repository(candidate.partition(':')[0])}:{candidate.partition(':')[2]}") != role
               for candidate in prior):
            raise ValueError("prior owner adoption must retain repository and role")
        placeholders = ",".join("?" for _candidate in prior)
        # single_instance keeps one daemon per role, so no other writer races
        # this adoption (Q17: it is the only deployment fence).
        with self.transaction() as db:
            self._assert_owner_active(db, owner_id)
            prior_rows = db.execute(
                f"SELECT owner_id, repository, role FROM retired_owners "
                f"WHERE owner_id IN ({placeholders})",
                prior,
            ).fetchall()
            for row in prior_rows:
                if (
                    row["repository"] != repository
                    or row["role"] != role
                ):
                    raise ControlStoreError(
                        f"retired owner {row['owner_id']!r} belongs to "
                        f"{row['repository']}:{row['role']}, not "
                        f"{repository}:{role}"
                    )
            runs = db.execute(
                f"UPDATE runs SET owner_id=? WHERE owner_id IN ({placeholders})",
                (owner_id, *prior),
            ).rowcount
            db.execute(
                f"UPDATE leases SET owner_id=? WHERE owner_id IN ({placeholders})",
                (owner_id, *prior),
            )
            db.execute(
                f"UPDATE dispatch_claims SET owner_id=? "
                f"WHERE repository=? AND role=? "
                f"AND owner_id IN ({placeholders})",
                (owner_id, repository, role, *prior),
            )
            retired_at = _stamp(self._now())
            db.executemany(
                "INSERT INTO retired_owners("
                "owner_id, successor_owner_id, repository, role, retired_at"
                ") VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(owner_id) DO UPDATE SET "
                "successor_owner_id=excluded.successor_owner_id, "
                "retired_at=excluded.retired_at",
                (
                    (
                        candidate,
                        owner_id,
                        repository,
                        role,
                        retired_at,
                    )
                    for candidate in prior
                ),
            )
            return runs


    def now(self) -> datetime:
        """This store's authority clock; executors stamp their reads with it."""
        return self._now()

    @staticmethod
    @contextmanager
    def mutation_savepoint(db: sqlite3.Connection) -> Iterator[None]:
        """A caught helper error cannot leave half a mutation in its caller's transaction."""
        if not db.in_transaction:
            raise ControlStoreError("workflow mutation requires an enclosing transaction")
        name = "workflow_mutation_" + uuid.uuid4().hex
        db.execute(f"SAVEPOINT {name}")
        try:
            yield
            db.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            db.execute(f"RELEASE SAVEPOINT {name}")
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db, db:
            try:
                db.execute("BEGIN IMMEDIATE")
                if self._bound_owner_id is not None:
                    self._assert_owner_active(db, self._bound_owner_id)
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

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
            self._assert_owner_active(db, owner_id)
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
        if outcome == "unlaunched_released":
            raise ValueError("never-launched evidence can only be minted by reservation release")
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
            return True

    def register_lease(self, lease: Lease) -> None:
        with self.transaction() as db:
            run = db.execute("SELECT state FROM runs WHERE run_id=?", (str(lease.run_id),)).fetchone()
            if run is None or run["state"] in {"completed", "cancelled", "abandoned"}:
                raise ControlStoreError("process lease requires a nonterminal run")
            self._assert_owner_active(db, lease.owner_id)
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
            db.execute(
                "UPDATE reservation_recovery SET policy='running', "
                "reason=NULL, updated_at=? WHERE run_id=?",
                (_stamp(self._now()), str(lease.run_id)),
            )

    def register_process_lease(
        self,
        run_id: RunId,
        *,
        expected_owner_id: str,
        pid: int,
        pgid: int,
        process_start_ticks: int,
    ) -> Lease:
        """Bind a spawned process to its reserved run before paid work executes."""
        if (
            not expected_owner_id
            or type(pid) is not int or pid <= 0
            or type(pgid) is not int or pgid <= 0
            or type(process_start_ticks) is not int or process_start_ticks <= 0
        ):
            raise ValueError("live process identity is required")
        return self._register_process_lease(
            run_id, expected_owner_id=expected_owner_id, pid=pid, pgid=pgid,
            process_start_ticks=process_start_ticks,
        )

    def _register_process_lease(
        self, run_id: RunId, *, expected_owner_id: str, pid: int, pgid: int,
        process_start_ticks: int,
    ) -> Lease:
        with self.transaction() as db:
            self._assert_owner_active(db, expected_owner_id)
            run = db.execute(
                "SELECT dispatch_id, kind, owner_id, state FROM runs WHERE run_id=?",
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
                repository = expected_owner_id.partition(":")[0]
                identical = (
                    int(existing["pid"]) == pid
                    and int(existing["pgid"]) == pgid
                    and int(existing["process_start_ticks"]) == process_start_ticks
                    and existing["owner_id"] == expected_owner_id
                    and existing["dispatch_id"] == run["dispatch_id"]
                    and (run["owner_id"] == expected_owner_id
                         or run["owner_id"] in self._owner_lineage(db, repository, expected_owner_id))
                )
                if not identical:
                    from .process_supervisor import ProcessSupervisor
                    try:
                        incoming_group_matches = os.getpgid(pid) == pgid
                    except OSError:
                        incoming_group_matches = False
                    if (existing["owner_id"] != expected_owner_id
                            or run["owner_id"] != expected_owner_id
                            or existing["dispatch_id"] != run["dispatch_id"]
                            or not incoming_group_matches
                            or not ProcessSupervisor.matches_identity(pid, process_start_ticks)
                            or ProcessSupervisor.matches_identity(existing["pid"], existing["process_start_ticks"])
                            or not ProcessSupervisor.group_is_absent(existing["pgid"])):
                        raise ControlStoreError(f"conflicting live process lease for run: {run_id}")
                    # A transport retry shares its paid run. It can replace
                    # ownership only after the prior process group is gone.
                    started_at = _parse_stamp(_stamp(self._now()))
                    db.execute("UPDATE leases SET pid=?,pgid=?,process_start_ticks=?,started_at=? WHERE run_id=?",
                               (pid, pgid, process_start_ticks, _stamp(started_at), str(run_id)))
                    return Lease(run_id, run["dispatch_id"], expected_owner_id,
                                 pid, pgid, started_at, process_start_ticks)
                return Lease(
                    run_id,
                    existing["dispatch_id"],
                    existing["owner_id"],
                    pid,
                    pgid,
                    _parse_stamp(existing["started_at"]),
                    process_start_ticks,
                )
            if run["owner_id"] != expected_owner_id:
                raise RetiredOwnerError(
                    f"reserved run owner changed from {expected_owner_id!r} "
                    f"to {run['owner_id']!r} before process registration"
                )
            started_at = _parse_stamp(_stamp(self._now()))
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
            db.execute(
                "UPDATE reservation_recovery SET policy='running', "
                "reason=NULL, updated_at=? WHERE run_id=?",
                (_stamp(started_at), str(run_id)),
            )
            return lease


    def leases(self, owner_id: str | None = None) -> tuple[Lease, ...]:
        with closing(self._connect()) as db, db:
            rows = (
                db.execute(
                    "SELECT l.run_id, l.dispatch_id, l.owner_id, l.pid, "
                    "l.pgid, l.started_at, l.process_start_ticks FROM leases l JOIN runs r "
                    "ON r.run_id=l.run_id WHERE l.owner_id=? "
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


    def reclaim_dead_leases(
        self,
        owner_id: str | Callable[[int, int], bool],
        is_alive: Callable[[int, int], bool] | None = None,
    ) -> tuple[RunId, ...]:
        """Abandon leases whose snapshotted durable owner process is gone.

        Process inspection may invoke ``ps`` on macOS, so all liveness probes
        run before the short write transaction. The transaction revalidates the
        complete lease identity before changing state to avoid reclaiming a
        lease that was replaced while the probe ran.
        """

        if callable(owner_id):
            alive = owner_id
            scoped_owner: str | None = None
        else:
            if is_alive is None:
                raise ValueError("liveness probe is required")
            alive = is_alive
            scoped_owner = owner_id
        with closing(self._connect()) as db, db:
            if scoped_owner is not None:
                self._assert_owner_active(db, scoped_owner)
            rows = (
                db.execute(
                    "SELECT l.run_id, l.dispatch_id, l.owner_id, l.pid, "
                    "l.pgid, l.process_start_ticks, l.started_at "
                    "FROM leases l JOIN runs r "
                    "ON r.run_id=l.run_id WHERE l.owner_id=? "
                    "ORDER BY l.started_at",
                    (scoped_owner,),
                ).fetchall()
                if scoped_owner is not None
                else db.execute(
                    "SELECT run_id, dispatch_id, owner_id, pid, pgid, "
                    "process_start_ticks, started_at "
                    "FROM leases ORDER BY started_at"
                ).fetchall()
            )
        dead = tuple(
            (
                str(row["run_id"]),
                str(row["dispatch_id"]),
                str(row["owner_id"]),
                int(row["pid"]),
                int(row["pgid"]),
                int(row["process_start_ticks"]),
                str(row["started_at"]),
            )
            for row in rows
            if not alive(int(row["pid"]), int(row["process_start_ticks"]))
        )
        reclaimed: list[RunId] = []
        with self.transaction() as db, self.mutation_savepoint(db):
            for identity in dead:
                run_id = RunId(identity[0])
                current = db.execute(
                    "SELECT run_id, dispatch_id, owner_id, pid, pgid, "
                    "process_start_ticks, started_at FROM leases "
                    "WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                if current is None:
                    continue
                current_identity = (
                    str(current["run_id"]),
                    str(current["dispatch_id"]),
                    str(current["owner_id"]),
                    int(current["pid"]),
                    int(current["pgid"]),
                    int(current["process_start_ticks"]),
                    str(current["started_at"]),
                )
                if current_identity != identity:
                    continue
                db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
                updated = db.execute(
                    "UPDATE runs SET state='abandoned', outcome='dead_lease', "
                    "completed_at=? WHERE run_id=? AND state NOT IN "
                    "('completed','cancelled','abandoned')",
                    (_stamp(self._now()), str(run_id)),
                ).rowcount
                if not updated:
                    continue
                retained_reason = (
                    "process ownership lost before spend reconciliation"
                )
                db.execute(
                    "UPDATE reservation_recovery SET policy='reconcile_only', "
                    "reason=?, updated_at=? WHERE run_id=?",
                    (retained_reason, _stamp(self._now()), str(run_id)),
                )
                reclaimed.append(run_id)
        return tuple(reclaimed)

    def active_reservation_runs(
        self, owner_id: str | None = None
    ) -> tuple[RunId, ...]:
        with closing(self._connect()) as db, db:
            owners = self._recovery_owners(db, owner_id) if owner_id is not None else ()
            rows = (
                db.execute(
                    "SELECT b.run_id FROM budget_reservations b JOIN runs r "
                    "ON r.run_id=b.run_id WHERE b.state='active' "
                    f"AND r.owner_id IN ({','.join('?' for _ in owners)}) ORDER BY b.created_at",
                    owners,
                ).fetchall()
                if owner_id is not None
                else db.execute(
                    "SELECT run_id FROM budget_reservations "
                    "WHERE state='active' ORDER BY created_at"
                ).fetchall()
            )
        return tuple(RunId(row["run_id"]) for row in rows)

    def stale_active_reservation_runs(
        self,
        owner_id: str,
        created_before: datetime,
    ) -> tuple[RunId, ...]:
        """Terminal work needs settlement immediately; spawn grace protects other work."""

        cutoff = _stamp(created_before)
        with closing(self._connect()) as db, db:
            owners = self._recovery_owners(db, owner_id)
            rows = db.execute(
                "SELECT b.run_id FROM budget_reservations b JOIN runs r "
                "ON r.run_id=b.run_id WHERE b.state='active' "
                f"AND r.owner_id IN ({','.join('?' for _ in owners)}) "
                "AND (b.created_at<=? OR r.state IN ('completed','cancelled','abandoned')) ORDER BY b.created_at",
                (*owners, cutoff),
            ).fetchall()
        return tuple(RunId(row["run_id"]) for row in rows)

    def record_reservation_retained(
        self,
        run_id: RunId,
        reason: str,
    ) -> bool:
        """Retain uncertain spend."""

        if not reason:
            raise ValueError("reservation retention reason is required")
        with self.transaction() as db:
            row = db.execute(
                "SELECT b.state,b.workload_repository,r.dispatch_id FROM budget_reservations b "
                "JOIN runs r ON r.run_id=b.run_id WHERE b.run_id=?",
                (str(run_id),),
            ).fetchone()
            if row is None or row["state"] != "active":
                return False
            now = _stamp(self._now())
            db.execute(
                "UPDATE reservation_recovery SET policy='reconcile_only', "
                "reason=?, updated_at=? WHERE run_id=? "
                "AND (policy IS NOT 'reconcile_only' OR reason IS NOT ?)",
                (reason, now, str(run_id), reason),
            )
            db.execute(
                "UPDATE runs SET "
                "state=CASE WHEN state IN ('completed','cancelled','abandoned') "
                "THEN state ELSE 'completed' END, "
                "outcome=CASE WHEN state IN ('cancelled','abandoned') THEN outcome "
                "WHEN state IN ('completed','cancelled','abandoned') "
                "THEN outcome ELSE 'spend_unreconciled' END, "
                "completed_at=COALESCE(completed_at, ?) WHERE run_id=? "
                "AND (state NOT IN ('completed','cancelled','abandoned') "
                "OR completed_at IS NULL)",
                (now, str(run_id)),
            )
            db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
            return True

    def settle_terminal_estimate(self, run_id: RunId, reason: str) -> bool:
        """Adopt legacy terminal holds through normal settlement, never a row rewrite.

        The lease and terminal-state checks are repeated in the settlement
        transaction so recovery cannot release money belonging to live work.
        """
        with self.transaction() as db:
            row = db.execute(
                "SELECT b.state,b.currency,r.state AS run_state FROM budget_reservations b "
                "JOIN runs r USING(run_id) WHERE b.run_id=? "
                "AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.run_id=r.run_id)",
                (str(run_id),),
            ).fetchone()
            if row is None or row["run_state"] not in ("completed", "cancelled", "abandoned"):
                return False
            if row["state"] == "settled":
                return True
            if row["state"] != "active":
                return False
            result = self._settle(db, SettlementRequest(
                run_id, Money.zero(row["currency"]), PricingBasis.ESTIMATE,
                f"terminal-estimate:{run_id}", None, None, None, reason[:2048],
                ChargeOrigin.RECOVERY_ESTIMATE,
            ))
            return result.disposition is AccountingDisposition.AVAILABLE

    def run_completed_at(self, run_id: RunId) -> float | None:
        """Harness-owned end of a run (epoch seconds), or None while it has none."""
        with self.transaction() as db:
            row = db.execute("SELECT completed_at FROM runs WHERE run_id=?", (str(run_id),)).fetchone()
        return _parse_stamp(row["completed_at"]).timestamp() if row is not None and row["completed_at"] else None

    def settle_remetered(self, request: SettlementRequest, failure: NativeFailure) -> SettlementResult:
        """Settle a re-metered run and journal it in the same transaction, at most once."""
        if request.basis is not PricingBasis.RECONCILED or request.origin is not ChargeOrigin.RECOVERY_ESTIMATE:
            raise ValueError("measured recovery requires reconciled request evidence")
        with self.transaction() as db:
            result = self._settle(db, request)
            return result


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
            owners = self._recovery_owners(db, owner_id)
            rows = db.execute(
                "SELECT r.run_id, r.dispatch_id, r.kind, r.state FROM runs r "
                "JOIN budget_reservations b ON b.run_id=r.run_id "
                f"WHERE r.owner_id IN ({','.join('?' for _ in owners)}) AND b.state='active' "
                "AND r.state NOT IN ('completed','cancelled','abandoned') "
                "ORDER BY b.created_at",
                owners,
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
        return tuple(intents)

    def pending_cancellation_intents(
        self,
        owner_id: str,
    ) -> tuple[RunCancellationIntent, ...]:
        if not owner_id:
            raise ValueError("run owner is required")
        with closing(self._connect()) as db, db:
            owners = self._recovery_owners(db, owner_id)
            rows = db.execute(
                "SELECT i.run_id, i.dispatch_id, i.kind, i.issue_number, "
                "i.reason FROM run_cancellation_intents i JOIN runs r "
                f"ON r.run_id=i.run_id WHERE r.owner_id IN ({','.join('?' for _ in owners)}) "
                "AND i.handover_written_at IS NULL ORDER BY i.created_at",
                owners,
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
        with closing(self._connect()) as db, db:
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


    def claim_dispatch(
        self,
        repository: str,
        role: str,
        issue: int,
        leg: str,
        owner_id: str,
    ) -> bool:
        """Atomically reserve one paid target across deployments. Targets are
        the only conflict: the path-overlap holds were deleted (Q13), and the
        `paths_json` column is written empty until the schema drops it."""

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
        with closing(self._connect()) as db, db:
            observed = self._dispatch_claim_observations(
                db, repository, role
            )
        liveness = {
            claim: (
                ProcessSupervisor.matches_identity(
                    claim.owner_pid,
                    claim.owner_start_ticks,
                )
                or any(
                    state in {"reserved", "starting", "running"}
                    for (
                        _run_id,
                        state,
                        _lease_pid,
                        _lease_start_ticks,
                    ) in claim.bound_runs
                )
            )
            for claim in observed
        }
        try:
            with self.transaction() as db:
                self._assert_owner_active(db, owner_id)
                current = self._dispatch_claim_observations(
                    db, repository, role
                )
                live: list[_DispatchClaimObservation] = []
                for claim in current:
                    # A claim or child binding changed after the read-only snapshot
                    # is conservatively live. Process inspection must never run
                    # while SQLite's cross-process write lock is held.
                    if liveness.get(claim, True):
                        live.append(claim)
                        continue
                    db.execute(
                        "DELETE FROM dispatch_claims "
                        "WHERE repository=? AND role=? AND target=? "
                        "AND owner_pid=? AND owner_start_ticks=? AND occurrence_id=?",
                        (
                            repository,
                            role,
                            claim.target,
                            claim.owner_pid,
                            claim.owner_start_ticks,
                            claim.occurrence_id,
                        ),
                    )
                    _claim_lifecycle_receipt(db, repository, claim.occurrence_id, "reclaimed", _stamp(self._now()), {
                        "role": role, "target": claim.target, "leg": claim.leg,
                        "owner_id": claim.owner_id, "owner_pid": claim.owner_pid,
                        "owner_start_ticks": claim.owner_start_ticks,
                        "claimed_at": claim.claimed_at,
                    })
                if any(claim.target == target for claim in live):
                    return False
                occurrence_id = uuid.uuid4().hex
                claimed_at = _stamp(self._now())
                db.execute(
                    "INSERT INTO dispatch_claims("
                    "repository, role, target, leg, owner_id, owner_pid, "
                    "owner_start_ticks, paths_json, claimed_at, occurrence_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        repository,
                        role,
                        target,
                        leg,
                        owner_id,
                        identity.pid,
                        identity.start_ticks,
                        "[]",
                        claimed_at,
                        occurrence_id,
                    ),
                )
                _claim_lifecycle_receipt(db, repository, occurrence_id, "created", claimed_at, {
                    "role": role, "target": target, "leg": leg, "owner_id": owner_id,
                    "owner_pid": identity.pid, "owner_start_ticks": identity.start_ticks,
                    "claimed_at": claimed_at, "source": "admission",
                })
                return True
        except RetiredOwnerError:
            return False

    @staticmethod
    def _dispatch_claim_observations(
        db: sqlite3.Connection,
        repository: str,
        role: str,
    ) -> tuple[_DispatchClaimObservation, ...]:
        rows = db.execute(
            "SELECT dc.target, dc.leg, dc.owner_id, dc.owner_pid, "
            "dc.owner_start_ticks, dc.occurrence_id, dc.claimed_at, dcr.run_id, r.state, "
            "l.pid AS lease_pid, l.process_start_ticks AS lease_start_ticks "
            "FROM dispatch_claims dc "
            "LEFT JOIN dispatch_claim_runs dcr "
            "ON dcr.repository=dc.repository AND dcr.role=dc.role "
            "AND dcr.target=dc.target "
            "LEFT JOIN runs r ON r.run_id=dcr.run_id "
            "LEFT JOIN leases l ON l.run_id=dcr.run_id "
            "WHERE dc.repository=? AND dc.role=? "
            "ORDER BY dc.target, dcr.run_id",
            (repository, role),
        ).fetchall()
        grouped: dict[
            str,
            tuple[sqlite3.Row, list[tuple[str, str, int, int]]],
        ] = {}
        for row in rows:
            target = str(row["target"])
            base, bindings = grouped.setdefault(target, (row, []))
            if row["run_id"] is not None:
                bindings.append(
                    (
                        str(row["run_id"]),
                        str(row["state"]),
                        int(row["lease_pid"] or 0),
                        int(row["lease_start_ticks"] or 0),
                    )
                )
        return tuple(
            _DispatchClaimObservation(
                target,
                str(base["leg"]),
                str(base["owner_id"]),
                int(base["owner_pid"]),
                int(base["owner_start_ticks"]),
                str(base["occurrence_id"]),
                str(base["claimed_at"]),
                tuple(bindings),
            )
            for target, (base, bindings) in grouped.items()
        )

    def bind_dispatch_claim(
        self,
        repository: str,
        role: str,
        issue: int,
        owner_id: str,
        run_id: RunId,
    ) -> bool:
        """Attach one admitted run so child liveness protects its global claim."""

        if (
            not repository
            or not role
            or issue == 0
            or not owner_id
            or not str(run_id)
        ):
            raise ValueError("dispatch claim binding identity is incomplete")
        target = _dispatch_claim_target(issue)
        try:
            with self.transaction() as db:
                self._assert_owner_active(db, owner_id)
                claim = db.execute(
                    "SELECT owner_id FROM dispatch_claims "
                    "WHERE repository=? AND role=? AND target=?",
                    (repository, role, target),
                ).fetchone()
                run = db.execute(
                    "SELECT owner_id, state FROM runs WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                if (
                    claim is None
                    or run is None
                    or claim["owner_id"] != owner_id
                    or run["owner_id"] != owner_id
                    or run["state"] not in {"reserved", "starting", "running"}
                ):
                    return False
                db.execute(
                    "INSERT OR IGNORE INTO dispatch_claim_runs("
                    "repository, role, target, run_id, bound_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        repository,
                        role,
                        target,
                        str(run_id),
                        _stamp(self._now()),
                    ),
                )
                return True
        except RetiredOwnerError:
            return False

    def release_dispatch_claim(
        self,
        repository: str,
        role: str,
        issue: int,
        owner_id: str,
        *,
        expected_occurrence_id: str | None = None,
        expected_process_identity: tuple[int, int] | None = None,
    ) -> bool:
        target = _dispatch_claim_target(issue)
        with self.transaction() as db:
            claim = db.execute(
                "SELECT occurrence_id,leg,owner_pid,owner_start_ticks,claimed_at FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target=? AND owner_id=?",
                (repository, role, target, owner_id),
            ).fetchone()
            if (claim is None
                    or (expected_occurrence_id is not None and claim["occurrence_id"] != expected_occurrence_id)
                    or (expected_process_identity is not None
                        and (claim["owner_pid"], claim["owner_start_ticks"]) != expected_process_identity)):
                return False
            cursor = db.execute(
                "DELETE FROM dispatch_claims "
                "WHERE repository=? AND role=? AND target=? AND owner_id=? AND occurrence_id=?",
                (repository, role, target, owner_id, claim["occurrence_id"]),
            )
            if cursor.rowcount == 1:
                _claim_lifecycle_receipt(db, repository, claim["occurrence_id"], "released", _stamp(self._now()), {
                    "role": role, "target": target, "leg": claim["leg"],
                    "owner_id": owner_id, "owner_pid": claim["owner_pid"],
                    "owner_start_ticks": claim["owner_start_ticks"],
                    "claimed_at": claim["claimed_at"],
                })
            return cursor.rowcount == 1

    def active_dispatch_claims(
        self, repository: str, role: str
    ) -> tuple[ActiveDispatchClaim, ...]:
        """Live paid targets one role currently holds (harness #176).

        Read-only cross-role observation: the reviewer asks whether the fixer is
        mid-push on a PR before paying for a review that the freshness fence
        would then reject. Liveness uses the same rule `claim_dispatch` applies
        when it garbage-collects — owner process alive, or a bound run still
        non-terminal.

        A row whose target or timestamp cannot be parsed is SKIPPED rather than
        reported. This reader only withholds work, so an unreadable row must
        fail open to dispatch: the submit-time head fence still protects
        correctness, whereas failing closed would let one corrupt row starve
        review indefinitely.
        """

        with closing(self._connect()) as db, db:
            rows = db.execute(
                "SELECT dc.target, dc.leg, dc.owner_id, dc.owner_pid, "
                "dc.owner_start_ticks, dc.claimed_at, dc.occurrence_id, dcr.run_id, r.state "
                "FROM dispatch_claims dc "
                "LEFT JOIN dispatch_claim_runs dcr "
                "ON dcr.repository=dc.repository AND dcr.role=dc.role "
                "AND dcr.target=dc.target "
                "LEFT JOIN runs r ON r.run_id=dcr.run_id "
                "WHERE dc.repository=? AND dc.role=? "
                "ORDER BY dc.target, dcr.run_id",
                (repository, role),
            ).fetchall()
        grouped: dict[str, tuple[sqlite3.Row, list[tuple[str, str]]]] = {}
        for row in rows:
            _base, bindings = grouped.setdefault(str(row["target"]), (row, []))
            if row["run_id"] is not None:
                bindings.append((str(row["run_id"]), str(row["state"])))
        from .process_supervisor import ProcessSupervisor

        claims: list[ActiveDispatchClaim] = []
        for target, (base, bindings) in grouped.items():
            target_key = _dispatch_claim_issue(target)
            if target_key is None:
                continue
            try:
                claimed_at = _parse_stamp(str(base["claimed_at"]))
            except ValueError:
                continue
            occurrence_id = base["occurrence_id"]
            if not isinstance(occurrence_id, str) or not occurrence_id:
                continue
            live_run = any(
                state in {"reserved", "starting", "running"}
                for _run_id, state in bindings
            )
            if not live_run and not ProcessSupervisor.matches_identity(
                int(base["owner_pid"]), int(base["owner_start_ticks"])
            ):
                continue
            owner_id = str(base["owner_id"])
            claims.append(
                ActiveDispatchClaim(
                    target_key=target_key,
                    role=role,
                    leg=str(base["leg"]),
                    owner_id=owner_id,
                    claimed_at=claimed_at,
                    claim_key=occurrence_id,
                )
            )
        return tuple(claims)

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


    def leg_runs(self, repository: str, issues: Iterable[int]) -> tuple[LegRun, ...]:
        """Every ended leg of either role for these issues, with its receipt (if any).

        The `leg` receipt carries the leg's class, written before settlement.
        A run ended without one never returned (cancelled, reclaimed, refused
        before admission): a machine end to its reader. Both roles read every
        leg: a leg of either role holds its item busy and breaks attempt runs.
        """
        wanted = sorted({n for n in issues if type(n) is int and n > 0})
        if not wanted:
            return ()
        out: list[LegRun] = []
        with closing(self._connect()) as db, db:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                rows = db.execute(
                    "SELECT r.kind, r.started_at, r.completed_at, r.dispatch_id, k.payload_json "
                    "FROM runs r "
                    "LEFT JOIN dedupe_keys k ON k.namespace=? AND k.scope=? AND k.key=r.run_id "
                    f"WHERE {_LEG_DISPATCH} AND {_FIX_DISPATCH_REPOSITORY}=? "
                    f"AND {_FIX_DISPATCH_ISSUE} IN ({','.join('?' for _ in chunk)}) "
                    "AND r.completed_at IS NOT NULL",
                    (LEG_NAMESPACE, repository, repository, *chunk),
                ).fetchall()
                for row in rows:
                    dispatch = json.loads(row["dispatch_id"])
                    try:
                        receipt = json.loads(row["payload_json"]) if row["payload_json"] is not None else None
                    except json.JSONDecodeError:
                        receipt = None
                    number = dispatch.get("number")
                    head = dispatch.get("head")
                    out.append(LegRun(
                        str(row["kind"]),
                        number if type(number) is int else dispatch["issue"],
                        int(dispatch["issue"]),
                        head if isinstance(head, str) else "",
                        _parse_stamp(row["started_at"]) if row["started_at"] else None,
                        _parse_stamp(row["completed_at"]),
                        receipt if isinstance(receipt, dict) else None,
                    ))
        return tuple(out)

    def claim_key(self, namespace: str, scope: str, key: str) -> str:
        """Claim an idempotency key before its remote effect. Returns "new" when
        this call claimed it, "claimed" when an earlier claim never confirmed
        (the effect may or may not have landed: read it back), "done" when the
        effect was confirmed."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT payload_json FROM dedupe_keys WHERE namespace=? AND scope=? AND key=?",
                (namespace, scope, key),
            ).fetchone()
            if row is not None:
                return "done" if row["payload_json"] == '"done"' else "claimed"
            db.execute(
                "INSERT INTO dedupe_keys(namespace, scope, key, claimed_at, payload_json) VALUES (?, ?, ?, ?, ?)",
                (namespace, scope, key, _stamp(self._now()), '"claimed"'),
            )
            return "new"

    def confirm_key(self, namespace: str, scope: str, key: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE dedupe_keys SET payload_json='\"done\"' WHERE namespace=? AND scope=? AND key=?",
                (namespace, scope, key),
            )

    def snapshot(self, scope: BudgetScope) -> AccountingSnapshot:
        try:
            with closing(self._connect()) as db, db:
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
        return self._reserve(request, lease=lease)


    def _reserve(
        self, request: ReservationRequest, *, lease: Lease | None = None,
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
            self.integrity_check()
            with self.transaction() as db, self.mutation_savepoint(db):
                self._assert_owner_active(db, request.owner_id)
                prior = db.execute(
                    "SELECT * FROM budget_reservations WHERE run_id=?",
                    (str(request.run_id),),
                ).fetchone()
                if prior is not None:
                    identical = self._reservation_request_matches(
                        db,
                        prior,
                        request,
                        limits,
                        lease,
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
                            limit.scope.dimension,
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
                    "reservation_id, run_id, repository, workload_repository, "
                    "dimension, period_id, period_start, period_end, "
                    "period_timezone, currency, "
                    "maximum_micros, cap_micros, model, rate_card_id, "
                    "rate_card_version, quote_effective_at, config_version, "
                    "state, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, 'active', ?, ?)",
                    (
                        str(request.reservation_id),
                        str(request.run_id),
                        request.scope.repository,
                        request.workload_repository,
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
                db.execute(
                    "INSERT INTO reservation_recovery("
                    "run_id, policy, reason, updated_at"
                    ") VALUES (?, 'spawn_pending', NULL, ?)",
                    (str(request.run_id), now),
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
                    db.execute(
                        "UPDATE reservation_recovery SET policy='running', "
                        "reason=NULL, updated_at=? WHERE run_id=?",
                        (now, str(lease.run_id)),
                    )
                return ReservationResult(
                    AccountingDisposition.AVAILABLE,
                    request.reservation_id,
                    committed + requested,
                    requested,
                    request.cap,
                )
        except CorruptControlStore as exc:
            return ReservationResult(
                AccountingDisposition.CORRUPT,
                None,
                Money.zero(request.scope.currency),
                requested,
                request.cap,
                str(exc),
            )
        except RetiredOwnerError as exc:
            return ReservationResult(
                AccountingDisposition.UNAVAILABLE,
                None,
                Money.zero(request.scope.currency),
                requested,
                request.cap,
                str(exc),
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


    @staticmethod
    def _reservation_request_matches(
        db: sqlite3.Connection,
        prior: sqlite3.Row,
        request: ReservationRequest,
        limits: tuple[BudgetLimit, ...],
        lease: Lease | None,
    ) -> bool:
        """Match the complete authority, not a convenient subset of its IDs."""

        run = db.execute(
            "SELECT dispatch_id, kind, owner_id, state, config_version FROM runs WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
        if run is None:
            return False
        actual_reservation = tuple(
            prior[name]
            for name in (
                "reservation_id",
                "repository",
                "workload_repository",
                "dimension",
                "period_id",
                "period_start",
                "period_end",
                "period_timezone",
                "currency",
                "maximum_micros",
                "cap_micros",
                "model",
                "rate_card_id",
                "rate_card_version",
                "quote_effective_at",
                "config_version",
                "state",
            )
        )
        expected_reservation = (
            str(request.reservation_id),
            request.scope.repository,
            request.workload_repository,
            request.scope.dimension,
            request.scope.period_id,
            _stamp(request.scope.period_start),
            _stamp(request.scope.period_end),
            str(request.scope.period_timezone),
            request.scope.currency,
            request.quote.maximum_charge.micros,
            request.cap.micros,
            request.quote.model,
            request.quote.rate_card_id,
            request.quote.rate_card_version,
            _stamp(request.quote.effective_at),
            request.config_version,
            "active",
        )
        actual_run = tuple(
            run[name]
            for name in (
                "dispatch_id",
                "kind",
                "owner_id",
                "state",
                "config_version",
            )
        )
        expected_run = (
            request.dispatch_id,
            request.kind,
            request.owner_id,
            "running" if lease is not None else "reserved",
            request.config_version,
        )
        actual_limits = [
            tuple(row)
            for row in db.execute(
                "SELECT repository, dimension, period_id, period_start, "
                "period_end, period_timezone, currency, cap_micros "
                "FROM reservation_limits WHERE run_id=? "
                "ORDER BY repository, dimension, period_id, currency",
                (str(request.run_id),),
            ).fetchall()
        ]
        expected_limits = sorted(
            (
                limit.scope.repository,
                limit.scope.dimension,
                limit.scope.period_id,
                _stamp(limit.scope.period_start),
                _stamp(limit.scope.period_end),
                str(limit.scope.period_timezone),
                limit.scope.currency,
                limit.cap.micros,
            )
            for limit in limits
        )
        persisted_lease = db.execute(
            "SELECT dispatch_id, owner_id, pid, pgid, process_start_ticks, "
            "started_at FROM leases WHERE run_id=?",
            (str(request.run_id),),
        ).fetchone()
        expected_lease = (
            None
            if lease is None
            else (
                lease.dispatch_id,
                lease.owner_id,
                lease.pid,
                lease.pgid,
                lease.process_start_ticks,
                _stamp(lease.started_at),
            )
        )
        actual_lease = (
            None if persisted_lease is None else tuple(persisted_lease)
        )
        return (
            actual_reservation == expected_reservation
            and actual_run == expected_run
            and actual_limits == expected_limits
            and actual_lease == expected_lease
        )

    def settle(self, request: SettlementRequest) -> SettlementResult:
        with self.transaction() as db:
            return self._settle(db, request)


    def _settle(
        self, db: sqlite3.Connection, request: SettlementRequest,
    ) -> SettlementResult:
        recovery_receipt = request.origin is ChargeOrigin.RECOVERY_ESTIMATE
        reservation = db.execute(
            "SELECT b.state, b.maximum_micros, b.currency, r.dispatch_id, r.started_at "
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
        encoded_usage = _json(asdict(request.usage)) if request.usage is not None else None
        if existing is not None:
            identical = (
                existing["run_id"] == str(request.run_id)
                and existing["provider_receipt_id"] == request.provider_receipt_id
                and existing["amount_micros"] == request.amount.micros
                and existing["currency"] == request.amount.currency
                and existing["basis"] == request.basis.value
                and existing["origin"] == request.origin.value
                and existing["usage_json"] == encoded_usage
                and existing["rate_card_id"] == request.rate_card_id
                and existing["rate_card_version"] == request.rate_card_version
            )
            if identical:
                return SettlementResult(AccountingDisposition.AVAILABLE, request.amount, idempotent=True)
            recovered_correction = (
                existing["run_id"] == str(request.run_id)
                and existing["origin"] == ChargeOrigin.RECOVERY_ESTIMATE.value
                and not recovery_receipt
                and existing["currency"] == request.amount.currency
            )
            if not recovered_correction:
                return SettlementResult(AccountingDisposition.CONFLICT, request.amount,
                                        detail="conflicting settlement receipt")
        if existing is None and reservation["state"] != "active" and not (
            reservation["state"] == "released" and reservation["started_at"] is not None and not recovery_receipt
        ):
            return SettlementResult(
                AccountingDisposition.CONFLICT,
                request.amount,
                detail=f"reservation is already {reservation['state']}",
            )
        maximum = int(reservation["maximum_micros"])
        variance = (
            Money(request.amount.currency, request.amount.micros - maximum)
            if request.amount.micros > maximum
            else None
        )
        # One instant per settlement: the migration-canary recognizer requires
        # reservation updated_at == charge settled_at, and separate clock reads
        # straddled millisecond boundaries (native CI, #249).
        settled_at = _stamp(self._now())
        db.execute(
            "INSERT INTO charges(run_id, provider_receipt_id, amount_micros, "
            "currency, basis, usage_json, rate_card_id, rate_card_version, "
            "settled_at, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET provider_receipt_id=excluded.provider_receipt_id, "
            "amount_micros=excluded.amount_micros,basis=excluded.basis,usage_json=excluded.usage_json, "
            "rate_card_id=excluded.rate_card_id,rate_card_version=excluded.rate_card_version, "
            "settled_at=excluded.settled_at,origin=excluded.origin",
            (
                str(request.run_id),
                request.provider_receipt_id,
                request.amount.micros,
                request.amount.currency,
                request.basis.value,
                encoded_usage,
                request.rate_card_id,
                request.rate_card_version,
                settled_at,
                request.origin.value,
            ),
        )
        db.execute(
            "UPDATE budget_reservations SET state='settled', updated_at=? "
            "WHERE run_id=?",
            (settled_at, str(request.run_id)),
        )
        db.execute(
            "UPDATE runs SET "
            "state=CASE WHEN state IN ('completed','cancelled','abandoned') "
            "THEN state ELSE 'completed' END, "
            "outcome=CASE WHEN outcome='accounting_unavailable' THEN 'settled' "
            "WHEN state IN ('completed','cancelled','abandoned') "
            "THEN outcome ELSE 'settled' END, "
            "provider_receipt_id=?, completed_at=COALESCE(completed_at, ?) "
            "WHERE run_id=?",
            (
                request.provider_receipt_id,
                settled_at,
                str(request.run_id),
            ),
        )
        corrected_dead_lease = False
        if not recovery_receipt:
            # The normal caller can win after lease reclamation but before
            # the recovery reconciler. Its successful settlement proves
            # the accounting owner survived the child, so dead_lease is a
            # stale recovery observation rather than a terminal outcome.
            corrected_dead_lease = bool(
                db.execute(
                    "UPDATE runs SET state='completed', outcome='settled', "
                    "completed_at=? WHERE run_id=? AND state='abandoned' "
                    "AND outcome='dead_lease'",
                    (settled_at, str(request.run_id)),
                ).rowcount
            )
            if corrected_dead_lease:
                db.execute(
                    "UPDATE reservation_recovery SET "
                    "reason='normal settlement won recovery race', "
                    "updated_at=? WHERE run_id=?",
                    (settled_at, str(request.run_id)),
                )
        db.execute("DELETE FROM leases WHERE run_id=?", (str(request.run_id),))
        return SettlementResult(
            AccountingDisposition.AVAILABLE,
            request.amount,
            variance=variance,
        )

    def release(self, run_id: RunId, reason: str) -> ReleaseResult:
        if not reason:
            raise ValueError("reservation release reason is required")
        with self.transaction() as db, self.mutation_savepoint(db):
            return self._release(db, run_id, reason)

    def release_unlaunched(
        self, run_id: RunId, reason: str
    ) -> ReleaseResult:
        """Release only a reservation durably proven never to have run."""

        if not reason:
            raise ValueError("reservation release reason is required")
        with self.transaction() as db, self.mutation_savepoint(db):
            recovery = db.execute(
                "SELECT rr.policy, r.state AS run_state, "
                "EXISTS(SELECT 1 FROM leases l WHERE l.run_id=rr.run_id) "
                "AS has_lease "
                "FROM reservation_recovery rr JOIN runs r "
                "ON r.run_id=rr.run_id WHERE rr.run_id=?",
                (str(run_id),),
            ).fetchone()
            if (
                recovery is None
                or recovery["policy"] != "spawn_pending"
                or recovery["run_state"] not in {"reserved", "starting"}
                or bool(recovery["has_lease"])
            ):
                return ReleaseResult(
                    AccountingDisposition.CONFLICT,
                    run_id,
                    detail=(
                        "reservation lacks durable proof that paid execution "
                        "never started"
                    ),
                )
            return self._release(db, run_id, reason)

    def release_retained(
        self,
        run_id: RunId,
        reason: str,
        retained_before: datetime,
    ) -> ReleaseResult:
        """Release a completed, unmeasured run after its reconciliation grace."""

        if not reason:
            raise ValueError("retained reservation release reason is required")
        cutoff = _stamp(retained_before)
        with self.transaction() as db:
            row = db.execute(
                "SELECT b.state AS reservation_state, b.release_reason, "
                "rr.policy, rr.updated_at AS recovery_updated_at, "
                "r.state AS run_state, r.outcome, "
                "EXISTS(SELECT 1 FROM leases l WHERE l.run_id=b.run_id) "
                "AS has_lease "
                "FROM budget_reservations b "
                "LEFT JOIN reservation_recovery rr ON rr.run_id=b.run_id "
                "JOIN runs r ON r.run_id=b.run_id WHERE b.run_id=?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                return ReleaseResult(
                    AccountingDisposition.CONFLICT,
                    run_id,
                    detail="run has no reservation",
                )
            if row["reservation_state"] == "released":
                identical = row["release_reason"] == reason
                return ReleaseResult(
                    (
                        AccountingDisposition.AVAILABLE
                        if identical
                        else AccountingDisposition.CONFLICT
                    ),
                    run_id,
                    idempotent=identical,
                    detail=(
                        ""
                        if identical
                        else "conflicting release reason"
                    ),
                )
            eligible = (
                row["reservation_state"] == "active"
                and row["policy"] == "reconcile_only"
                and row["run_state"] == "completed"
                and row["outcome"] == "spend_unreconciled"
                and not bool(row["has_lease"])
                and row["recovery_updated_at"] is not None
                and str(row["recovery_updated_at"]) <= cutoff
            )
            if not eligible:
                return ReleaseResult(
                    AccountingDisposition.CONFLICT,
                    run_id,
                    detail=(
                        "reservation is not a stale completed "
                        "accounting-retained run"
                    ),
                )
            now = _stamp(self._now())
            db.execute(
                "UPDATE budget_reservations SET state='released', "
                "release_reason=?, updated_at=? WHERE run_id=?",
                (reason, now, str(run_id)),
            )
            db.execute(
                "UPDATE runs SET outcome='spend_unmeasured_released' "
                "WHERE run_id=?",
                (str(run_id),),
            )
            return ReleaseResult(AccountingDisposition.AVAILABLE, run_id)

    def _release(
        self,
        db: sqlite3.Connection,
        run_id: RunId,
        reason: str,
    ) -> ReleaseResult:
        reservation = db.execute(
            "SELECT state, release_reason FROM budget_reservations WHERE run_id=?",
            (str(run_id),),
        ).fetchone()
        if reservation is None:
            return ReleaseResult(
                AccountingDisposition.CONFLICT,
                run_id,
                detail="run has no reservation",
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
        never_launched = db.execute(
            "SELECT 1 FROM runs r JOIN reservation_recovery rr USING(run_id) "
            "WHERE r.run_id=? AND r.state IN ('reserved','starting') AND rr.policy='spawn_pending' "
            "AND r.provider_receipt_id IS NULL AND NOT EXISTS(SELECT 1 FROM charges c WHERE c.run_id=r.run_id) "
            "AND NOT EXISTS(SELECT 1 FROM leases l WHERE l.run_id=r.run_id)", (str(run_id),),
        ).fetchone()
        db.execute(
            "UPDATE budget_reservations SET state='released', release_reason=?, "
            "updated_at=? WHERE run_id=?",
            (reason, now, str(run_id)),
        )
        db.execute(
            "UPDATE runs SET "
            "state=CASE WHEN state='abandoned' THEN 'abandoned' ELSE 'cancelled' END, "
            "outcome=CASE WHEN state='abandoned' THEN outcome ELSE ? END, "
            "completed_at=? WHERE run_id=?",
            ("unlaunched_released" if never_launched is not None else "released", now, str(run_id)),
        )
        db.execute("DELETE FROM leases WHERE run_id=?", (str(run_id),))
        return ReleaseResult(AccountingDisposition.AVAILABLE, run_id)


    @classmethod
    def _recovery_owners(cls, db: sqlite3.Connection, owner_id: str) -> tuple[str, ...]:
        cls._assert_owner_active(db, owner_id)
        repository, separator, _ = owner_id.partition(":")
        return cls._owner_lineage(db, repository, owner_id) if separator else (owner_id,)

    @staticmethod
    def _owner_lineage(db: sqlite3.Connection, repository: str, owner_id: str) -> tuple[str, ...]:
        """This owner and every retired owner of its repository and role that it (transitively) succeeded.

        The reverse of `owns_command`'s walk: a terminal receipt keeps the
        executor that settled it, so a predecessor's unclaimed rows belong
        to its successor. Bounded like that walk.
        """
        role = _deployment_role(repository, owner_id)
        lineage, frontier = [owner_id], [owner_id]
        for _ in range(64):
            if not frontier:
                break
            marks = ",".join("?" for _ in frontier)
            found = [row["owner_id"] for row in db.execute(
                f"SELECT owner_id, repository, role FROM retired_owners WHERE successor_owner_id IN ({marks}) "
                "ORDER BY owner_id", frontier,
            ) if canonical_repository(row["repository"]) == repository and row["role"] == role
                and row["owner_id"] not in lineage]
            lineage.extend(found)
            frontier = found
        return tuple(lineage)


    def backup(self, destination: str) -> None:
        target = os.path.abspath(destination)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with closing(self._connect()) as source, closing(sqlite3.connect(target)) as dest, source, dest:
            source.backup(dest)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)

    def import_legacy_spend(self, source_path: str) -> MigrationReport:
        """Import an append-only legacy ledger without resetting live caps."""

        source = Path(source_path)
        raw = source.read_bytes()
        source_digest = hashlib.sha256(raw).hexdigest()
        source_identity = str(source.resolve())
        migration_id = "legacy-spend:" + hashlib.sha256(
            f"{source_identity}:{source_digest}".encode()
        ).hexdigest()
        legacy_migration_id = f"legacy-spend:{source_digest}"
        parsed: list[tuple[str, str, int, int, datetime, str]] = []
        occurrences: dict[str, int] = {}
        rejected = 0
        for line in raw.splitlines():
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
                if not amount.is_finite() or amount < 0:
                    raise ValueError("amount must be finite and nonnegative")
                micros = int(
                    (amount * Decimal(1_000_000)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                normalized_repository = canonical_repository(repository)
                model = str(row.get("model") or "<unknown>")
                normalized_stamp = _stamp(stamp)
                semantic_row = json.dumps(
                    {
                        "amount_micros": micros,
                        "issue": issue,
                        "model": model,
                        "repository": normalized_repository,
                        "settled_at": normalized_stamp,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                occurrence = occurrences.get(semantic_row, 0) + 1
                occurrences[semantic_row] = occurrence
                row_identity = hashlib.sha256(
                    f"{source_identity}:{semantic_row}:{occurrence}".encode()
                ).hexdigest()
                run_id = hashlib.sha256(
                    f"legacy-spend-row:{row_identity}".encode()
                ).hexdigest()
                parsed.append(
                    (
                        run_id,
                        normalized_repository,
                        issue,
                        micros,
                        stamp,
                        model,
                    )
                )
            except (
                InvalidOperation,
                KeyError,
                OverflowError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                rejected += 1

        if rejected:
            raise LegacySpendMigrationError(
                f"{source}: rejected {rejected} legacy spend row(s); "
                "no rows were imported"
            )

        imported = 0
        total = 0
        with self.transaction() as db:
            existing = db.execute(
                "SELECT migration_id, row_count, rejected_count, amount_micros "
                "FROM migration_imports WHERE migration_id=?",
                (migration_id,),
            ).fetchone()
            if existing is None:
                existing = db.execute(
                    "SELECT migration_id, row_count, rejected_count, "
                    "amount_micros FROM migration_imports "
                    "WHERE migration_id=? AND source_path=?",
                    (legacy_migration_id, source_identity),
                ).fetchone()
            if existing is not None:
                return MigrationReport(
                    str(existing["migration_id"]),
                    source_digest,
                    int(existing["row_count"]),
                    int(existing["rejected_count"]),
                    Money("USD", int(existing["amount_micros"])),
                    True,
                )
            for (
                run_id,
                repository,
                issue,
                micros,
                stamp,
                model,
            ) in parsed:
                period_start = stamp.astimezone(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                period_end = period_start + timedelta(days=1)
                period_id = period_start.date().isoformat()
                settled_at = _stamp(stamp)
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
                    "reservation_id, run_id, repository, workload_repository, "
                    "dimension, period_id, period_start, period_end, "
                    "period_timezone, currency, maximum_micros, cap_micros, "
                    "model, rate_card_id, rate_card_version, "
                    "quote_effective_at, config_version, state, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, 'repository', ?, ?, ?, "
                    "'UTC', 'USD', ?, ?, ?, 'legacy', '1', ?, 'legacy-v1', "
                    "'settled', ?, ?)",
                    (
                        f"legacy:{run_id}",
                        run_id,
                        repository,
                        repository,
                        period_id,
                        _stamp(period_start),
                        _stamp(period_end),
                        micros,
                        9_000_000_000_000_000_000,
                        model,
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

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        mode = "rwc" if create else "rw"
        uri = f"{Path(self.path).as_uri()}?mode={mode}"
        try:
            db = sqlite3.connect(
                uri,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                uri=True,
            )
        except sqlite3.OperationalError as exc:
            if not create and not os.path.exists(self.path):
                raise CorruptControlStore(
                    "control-store authority database disappeared after "
                    "initialization"
                ) from exc
            raise
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        db.execute("PRAGMA foreign_keys=ON")
        # Reassigning WAL reopens its transition path on every short-lived
        # connection. Check the persisted mode first; retain the same repair.
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db


def _deployment_role(repository: str, owner_id: str) -> str:
    """The role an owner identity (`<repository>:<role>:…`) names."""
    prefix = f"{canonical_repository(repository)}:"
    if not owner_id.startswith(prefix):
        raise ValueError("deployment owner does not match its repository")
    role = owner_id.removeprefix(prefix).partition(":")[0]
    if not role:
        raise ValueError("deployment owner role is unavailable")
    return role


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


def _dispatch_claim_issue(target: str) -> int | None:
    """Inverse of `_dispatch_claim_target`; None when the row is unreadable."""
    prefix, _, number = target.partition(":")
    if prefix not in {"issue", "pr"} or not number.isdigit():
        return None
    value = int(number)
    if value <= 0:
        return None
    return value if prefix == "issue" else -value


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
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("control-store timestamps must be timezone-aware")
    return parsed
