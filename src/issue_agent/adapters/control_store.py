"""SQLite operational authority (ADR 0003): money, leases and idempotency.

Three tables (refactor decision Q4):

- ``legs``: one row per leg, paid or refused: what it was dispatched as and
  on, its money (reserved, then settled or released), and what it returned.
  Daily and lifetime caps are sums over it; attempts and backoff are derived
  from it.
- ``leases``: one claim per (repository, role, item), bound to its leg once
  reserved. It holds the process whose death ends the leg: the claiming
  daemon, then the leg's own process group once that registers.
- ``idempotency``: keyed remote writes (the act layer's stop comments).

``PRAGMA user_version`` is the schema version; there is no migration history.
A file still in the legacy schema (it has ``schema_migrations``) is cut over
once, at open, by ``_cut_over``.

Transactions are short: no network, model or subprocess work runs inside one,
and process probes run before the write transaction that acts on them.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from .process_supervisor import ProcessSupervisor
from ..domain.contracts import (
    BudgetScope,
    ChargeOrigin,
    Money,
    PricingBasis,
    RunId,
    SettlementRequest,
)
from ..domain.repository import canonical_repository

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_PAID_ROLES = ("fix", "review")
# Legs copied from the legacy store at cutover: money only, never a leg derive
# counts (attempts restart at the cutover; GitHub carries the rest).


class ControlStoreError(RuntimeError):
    pass


class IncompatibleSchema(ControlStoreError):
    pass


class CorruptControlStore(ControlStoreError):
    pass


class CutoverRefused(ControlStoreError):
    """The legacy store still holds unsettled money; the operator settles it first."""


class AccountingDisposition(str, Enum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"
    CONFLICT = "conflict"


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


@dataclass(frozen=True)
class BudgetLimit:
    """One independently enforced ceiling on a reservation."""

    scope: BudgetScope
    cap: Money

    def __post_init__(self) -> None:
        if self.scope.currency != self.cap.currency:
            raise ValueError("budget limit scope and cap currency differ")


@dataclass(frozen=True)
class ReservationRequest:
    """One paid leg's identity and the maximum it may charge.

    ``lease_target`` is the item lease the leg binds to in the reservation's
    transaction; a paid leg without a held lease is refused.
    """

    run_id: RunId
    repository: str
    role: str
    kind: str
    owner_id: str
    maximum: Money
    limits: tuple[BudgetLimit, ...]
    lease_target: int
    issue: int | None = None
    number: int = 0
    head: str = ""
    runtime: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository", canonical_repository(self.repository))
        if not str(self.run_id) or not self.repository or not self.kind or not self.owner_id:
            raise ValueError("leg, repository, kind and owner identities are required")
        if self.role not in _PAID_ROLES:
            raise ValueError(f"paid legs belong to a paid role, not {self.role!r}")
        if not self.limits:
            raise ValueError("a reservation needs at least one limit")
        if any(limit.scope.currency != self.maximum.currency for limit in self.limits):
            raise ValueError("all reservation limits must use the reservation's currency")
        if type(self.lease_target) is not int or self.lease_target == 0:
            raise ValueError("a paid leg binds to a held item lease")
        if self.issue is not None and (type(self.issue) is not int or self.issue <= 0):
            raise ValueError("a leg's issue is a positive number")
        if type(self.number) is not int or self.number < 0:
            raise ValueError("a leg's item number is non-negative")


@dataclass(frozen=True)
class ReservationResult:
    disposition: AccountingDisposition
    committed: Money
    requested: Money
    cap: Money
    detail: str = ""
    # The limit that actually tripped, which is not always the first one when
    # a leg carries repository, global and issue caps at once.
    tripped_dimension: str = ""

    @property
    def admitted(self) -> bool:
        return self.disposition is AccountingDisposition.AVAILABLE


@dataclass(frozen=True)
class SettlementResult:
    disposition: AccountingDisposition
    amount: Money
    idempotent: bool = False
    detail: str = ""


@dataclass(frozen=True)
class ReleaseResult:
    disposition: AccountingDisposition
    run_id: RunId
    idempotent: bool = False
    detail: str = ""


@dataclass(frozen=True)
class Lease:
    """One item claim. ``target`` is signed: an issue, or ``-pr`` for a PR
    with no resolvable issue. ``run_id`` is the bound leg, once reserved."""

    repository: str
    role: str
    target: int
    owner_id: str
    run_id: RunId | None
    pid: int
    pgid: int
    process_start_ticks: int
    acquired_at: datetime


@dataclass(frozen=True)
class RunCancellationIntent:
    run_id: RunId
    kind: str
    issue_number: int | None
    reason: str


_DDL = (
    """CREATE TABLE legs (
    leg_id TEXT PRIMARY KEY CHECK (leg_id <> ''),
    repository TEXT NOT NULL CHECK (repository <> ''),
    role TEXT NOT NULL CHECK (role IN ('fix', 'review', 'carried')),
    kind TEXT NOT NULL CHECK (kind <> ''),
    issue INTEGER CHECK (issue > 0),
    number INTEGER NOT NULL DEFAULT 0 CHECK (number >= 0),
    head TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL CHECK (owner <> ''),
    runtime TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK (state IN ('reserved', 'running', 'ended', 'released')),
    reserved_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    end_reason TEXT,
    currency TEXT NOT NULL,
    reserved_micros INTEGER NOT NULL CHECK (reserved_micros >= 0),
    charged_micros INTEGER CHECK (charged_micros >= 0),
    basis TEXT,
    origin TEXT CHECK (origin IN ('normal_return', 'recovery_estimate')),
    usage_json TEXT,
    rate_card_id TEXT,
    rate_card_version TEXT,
    provider_receipt_id TEXT UNIQUE,
    settled_at TEXT,
    leg_class TEXT CHECK (leg_class IN ('agent', 'asked', 'machine')),
    session_id TEXT,
    result_json TEXT,
    CHECK ((state = 'ended') = (charged_micros IS NOT NULL)),
    CHECK ((charged_micros IS NULL) = (basis IS NULL)
       AND (charged_micros IS NULL) = (origin IS NULL)
       AND (charged_micros IS NULL) = (settled_at IS NULL)
       AND (charged_micros IS NULL) = (provider_receipt_id IS NULL)),
    CHECK (state <> 'reserved' OR started_at IS NULL),
    CHECK (state IN ('reserved', 'running') OR ended_at IS NOT NULL)
)""",
    "CREATE INDEX legs_item ON legs(repository, issue)",
    "CREATE INDEX legs_reserved_at ON legs(reserved_at)",
    "CREATE INDEX legs_unsettled ON legs(repository, role) WHERE state IN ('reserved', 'running')",
    """CREATE TABLE leases (
    repository TEXT NOT NULL CHECK (repository <> ''),
    role TEXT NOT NULL CHECK (role IN ('fix', 'review')),
    target INTEGER NOT NULL CHECK (target <> 0),
    owner TEXT NOT NULL CHECK (owner <> ''),
    leg_id TEXT UNIQUE REFERENCES legs(leg_id),
    pid INTEGER NOT NULL CHECK (pid > 0),
    pgid INTEGER NOT NULL CHECK (pgid > 0),
    start_ticks INTEGER NOT NULL CHECK (start_ticks > 0),
    acquired_at TEXT NOT NULL,
    PRIMARY KEY (repository, role, target)
)""",
    """CREATE TABLE idempotency (
    key TEXT PRIMARY KEY CHECK (key <> ''),
    state TEXT NOT NULL CHECK (state IN ('claimed', 'done')),
    claimed_at TEXT NOT NULL
)""",
)

# A leg's committed money: its charge once settled, nothing once released,
# its reserved maximum while it may still spend.
_COMMITTED = (
    "CASE state WHEN 'ended' THEN charged_micros WHEN 'released' THEN 0 "
    "ELSE reserved_micros END"
)
_UNSETTLED = "state IN ('reserved', 'running')"


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

    def unavailable(detail: str) -> SettledSpend:
        return SettledSpend(AccountingDisposition.UNAVAILABLE, None, MappingProxyType({}), detail)

    if not os.path.isfile(path):
        return unavailable(f"control store does not exist: {path}")
    try:
        db = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
        )
    except sqlite3.Error as exc:
        return unavailable(f"control store could not be opened read-only: {exc}")
    try:
        db.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA query_only=ON")
        _check_authority(db)
        rows = db.execute(
            "SELECT repository, COALESCE(SUM(charged_micros), 0) FROM legs "
            "WHERE state='ended' AND currency=? AND settled_at>=? AND settled_at<? "
            "GROUP BY repository",
            (currency, _stamp(period_start), _stamp(period_end)),
        ).fetchall()
    except (sqlite3.Error, ControlStoreError) as exc:
        return unavailable(f"settled spend could not be read: {exc}")
    finally:
        db.close()
    by_repository = {str(repository): Money(currency, int(amount)) for repository, amount in rows}
    return SettledSpend(
        AccountingDisposition.AVAILABLE,
        Money(currency, sum(item.micros for item in by_repository.values())),
        MappingProxyType(by_repository),
    )


class SQLiteControlStore:
    """One SQLite database, safe for threads and cooperating daemon processes."""

    def __init__(
        self,
        path: str,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        now: Callable[[], datetime] | None = None,
    ):
        if not path:
            raise ValueError("control-store path is required")
        if busy_timeout_ms <= 0:
            raise ValueError("busy timeout must be positive")
        self.path = os.path.abspath(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- opening -----------------------------------------------------------

    def initialize(self) -> None:
        """Create the store, or cut a legacy one over, then verify it.

        Serialized across role processes: both daemons of a deployment boot
        together and must not both create or cut over the same file.
        """
        parent = os.path.dirname(self.path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        os.chmod(parent, stat.S_IRWXU)
        # Probed before the lock is taken (taking it creates the file).
        opened_before = os.path.lexists(f"{self.path}.initialize.lock")
        with self._initialization_lock():
            try:
                metadata = os.stat(self.path, follow_symlinks=False)
            except FileNotFoundError:
                _refuse_prior_use(self.path, opened_before)
                _create(self.path)
            else:
                if not stat.S_ISREG(metadata.st_mode):
                    raise CorruptControlStore(f"control-store path is not a regular file: {self.path}")
                if metadata.st_size == 0:
                    raise CorruptControlStore(f"control-store authority database is empty: {self.path}")
                if _is_legacy(self.path):
                    _cut_over(self.path, self._now())
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            with closing(self._connect()) as db:
                _check_authority(db)

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        path = f"{self.path}.initialize.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ControlStoreError(f"unsafe control-store initialization lock: {path}")
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(
                f"{Path(self.path).as_uri()}?mode=rw",
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                uri=True,
            )
        except sqlite3.OperationalError as exc:
            if not os.path.exists(self.path):
                raise CorruptControlStore("control-store authority database disappeared") from exc
            raise
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        db.execute("PRAGMA foreign_keys=ON")
        # Reassigning WAL reopens its transition path on every short-lived
        # connection; check the persisted mode first.
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            yield db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _stamp_now(self) -> str:
        return _stamp(self._now())

    # -- money -------------------------------------------------------------

    def snapshot(self, scope: BudgetScope) -> AccountingSnapshot:
        try:
            with self._read() as db:
                settled, reserved = _balance(db, scope)
        except sqlite3.DatabaseError as exc:
            zero = Money.zero(scope.currency)
            return AccountingSnapshot(AccountingDisposition.CORRUPT, scope, zero, zero, str(exc))
        return AccountingSnapshot(
            AccountingDisposition.AVAILABLE, scope,
            Money(scope.currency, settled), Money(scope.currency, reserved),
        )

    def reserve(self, request: ReservationRequest) -> ReservationResult:
        """Reserve one leg's maximum against every cap and bind its lease, in
        one transaction, before anything is spawned (ADR 0002)."""

        requested = request.maximum
        first = request.limits[0]

        def result(disposition: AccountingDisposition, committed: Money, detail: str = "",
                   cap: Money = first.cap, tripped: str = "") -> ReservationResult:
            return ReservationResult(disposition, committed, requested, cap, detail, tripped)

        zero = Money.zero(requested.currency)
        try:
            with self._transaction() as db:
                _check_authority(db)
                prior = db.execute(
                    "SELECT repository, role, kind, owner, reserved_micros, currency, state "
                    "FROM legs WHERE leg_id=?", (str(request.run_id),),
                ).fetchone()
                if prior is not None:
                    identical = tuple(prior) == (
                        request.repository, request.role, request.kind, request.owner_id,
                        requested.micros, requested.currency, prior["state"],
                    ) and prior["state"] in ("reserved", "running")
                    return result(
                        AccountingDisposition.AVAILABLE if identical else AccountingDisposition.CONFLICT,
                        zero, "idempotent reservation" if identical else "conflicting reservation",
                    )
                committed = zero
                for index, limit in enumerate(request.limits):
                    settled, reserved = _balance(db, limit.scope)
                    limit_committed = Money(limit.scope.currency, settled + reserved)
                    if index == 0:
                        committed = limit_committed
                    if limit_committed.micros + requested.micros > limit.cap.micros:
                        return result(
                            AccountingDisposition.EXHAUSTED, limit_committed,
                            "settled + active reservations + requested maximum "
                            f"exceeds {limit.scope.dimension} cap",
                            limit.cap, limit.scope.dimension,
                        )
                lease = db.execute(
                    "SELECT leg_id FROM leases WHERE repository=? AND role=? AND target=? AND owner=?",
                    (request.repository, request.role, request.lease_target, request.owner_id),
                ).fetchone()
                if lease is None or lease["leg_id"] is not None:
                    return result(AccountingDisposition.CONFLICT, committed,
                                  "the leg's item lease is not held")
                db.execute(
                    "INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, "
                    "runtime, state, reserved_at, currency, reserved_micros) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)",
                    (str(request.run_id), request.repository, request.role, request.kind,
                     request.issue, request.number, request.head, request.owner_id,
                     request.runtime, self._stamp_now(), requested.currency, requested.micros),
                )
                db.execute(
                    "UPDATE leases SET leg_id=? WHERE repository=? AND role=? AND target=? AND owner=?",
                    (str(request.run_id), request.repository, request.role,
                     request.lease_target, request.owner_id),
                )
                return result(AccountingDisposition.AVAILABLE, committed + requested)
        except CorruptControlStore as exc:
            return result(AccountingDisposition.CORRUPT, zero, str(exc))
        except (sqlite3.DatabaseError, OSError, ControlStoreError) as exc:
            return result(AccountingDisposition.UNAVAILABLE, zero, str(exc))

    def settle(self, request: SettlementRequest) -> SettlementResult:
        """Settle a leg once. A normal return may correct a recovery estimate."""

        with self._transaction() as db:
            return self._settle(db, request)

    def _settle(self, db: sqlite3.Connection, request: SettlementRequest) -> SettlementResult:
        recovery = request.origin is ChargeOrigin.RECOVERY_ESTIMATE
        encoded_usage = _json(asdict(request.usage)) if request.usage is not None else None
        leg = db.execute(
            "SELECT state, currency, reserved_micros, started_at, charged_micros, basis, origin, "
            "usage_json, rate_card_id, rate_card_version, provider_receipt_id "
            "FROM legs WHERE leg_id=?", (str(request.run_id),),
        ).fetchone()
        if leg is None:
            return SettlementResult(AccountingDisposition.CONFLICT, request.amount,
                                    detail="leg has no reservation")
        if leg["currency"] != request.amount.currency:
            return SettlementResult(AccountingDisposition.CONFLICT, request.amount,
                                    detail="settlement currency differs from reservation")
        other = db.execute(
            "SELECT 1 FROM legs WHERE provider_receipt_id=? AND leg_id<>?",
            (request.provider_receipt_id, str(request.run_id)),
        ).fetchone()
        if other is not None:
            return SettlementResult(AccountingDisposition.CONFLICT, request.amount,
                                    detail="conflicting settlement receipt")
        if leg["state"] == "ended":
            identical = (
                leg["provider_receipt_id"] == request.provider_receipt_id
                and leg["charged_micros"] == request.amount.micros
                and leg["basis"] == request.basis.value
                and leg["origin"] == request.origin.value
                and leg["usage_json"] == encoded_usage
                and leg["rate_card_id"] == request.rate_card_id
                and leg["rate_card_version"] == request.rate_card_version
            )
            if identical:
                return SettlementResult(AccountingDisposition.AVAILABLE, request.amount, idempotent=True)
            if leg["origin"] != ChargeOrigin.RECOVERY_ESTIMATE.value or recovery:
                return SettlementResult(AccountingDisposition.CONFLICT, request.amount,
                                        detail="conflicting settlement receipt")
        elif leg["state"] == "released":
            return SettlementResult(AccountingDisposition.CONFLICT, request.amount,
                                    detail="reservation is already released")
        settled_at = self._stamp_now()
        db.execute(
            "UPDATE legs SET state='ended', charged_micros=?, basis=?, origin=?, usage_json=?, "
            "rate_card_id=?, rate_card_version=?, provider_receipt_id=?, settled_at=?, "
            "ended_at=COALESCE(ended_at, ?) WHERE leg_id=?",
            (request.amount.micros, request.basis.value, request.origin.value, encoded_usage,
             request.rate_card_id, request.rate_card_version, request.provider_receipt_id,
             settled_at, settled_at, str(request.run_id)),
        )
        db.execute("DELETE FROM leases WHERE leg_id=?", (str(request.run_id),))
        return SettlementResult(AccountingDisposition.AVAILABLE, request.amount)

    def settle_terminal_estimate(self, run_id: RunId, reason: str) -> bool:
        """Settle an ended, unsettled leg at zero (ESTIMATE): its process is
        gone and no usable request evidence survived. A leg still holding a
        lease may still be spending and is left alone."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT state, currency, ended_at FROM legs WHERE leg_id=? "
                "AND NOT EXISTS (SELECT 1 FROM leases WHERE leases.leg_id=legs.leg_id)",
                (str(run_id),),
            ).fetchone()
            if row is None or row["ended_at"] is None or row["state"] == "released":
                return False
            if row["state"] == "ended":
                return True
            result = self._settle(db, SettlementRequest(
                run_id, Money.zero(row["currency"]), PricingBasis.ESTIMATE,
                f"terminal-estimate:{run_id}", None, None, None, reason[:2048],
                ChargeOrigin.RECOVERY_ESTIMATE,
            ))
            return result.disposition is AccountingDisposition.AVAILABLE

    def release_unlaunched(self, run_id: RunId, reason: str) -> ReleaseResult:
        """Return the reservation of a leg whose process never registered
        (still `reserved`): it charges nothing. Running work is never
        released; it settles from evidence or a zero estimate once ended."""
        if not reason:
            raise ValueError("reservation release reason is required")
        with self._transaction() as db:
            row = db.execute("SELECT state, end_reason FROM legs WHERE leg_id=?", (str(run_id),)).fetchone()
            if row is None:
                return ReleaseResult(AccountingDisposition.CONFLICT, run_id, detail="leg has no reservation")
            if row["state"] == "released":
                identical = row["end_reason"] == reason
                return ReleaseResult(
                    AccountingDisposition.AVAILABLE if identical else AccountingDisposition.CONFLICT,
                    run_id, idempotent=identical, detail="" if identical else "conflicting release reason",
                )
            if row["state"] != "reserved":
                return ReleaseResult(AccountingDisposition.CONFLICT, run_id,
                                     detail=f"a {row['state']} leg is not eligible for this release")
            self._release_reserved(db, str(run_id), reason)
            return ReleaseResult(AccountingDisposition.AVAILABLE, run_id)

    def _release_reserved(self, db: sqlite3.Connection, leg_id: str, reason: str) -> None:
        db.execute(
            "UPDATE legs SET state='released', end_reason=?, ended_at=COALESCE(ended_at, ?) "
            "WHERE leg_id=? AND state='reserved'",
            (reason, self._stamp_now(), leg_id),
        )
        db.execute("DELETE FROM leases WHERE leg_id=?", (leg_id,))

    def record_reservation_retained(self, run_id: RunId, reason: str) -> bool:
        """End a leg whose spend is uncertain, keeping its reservation for recovery."""
        if not reason:
            raise ValueError("reservation retention reason is required")
        with self._transaction() as db:
            updated = db.execute(
                f"UPDATE legs SET ended_at=COALESCE(ended_at, ?), end_reason=COALESCE(end_reason, ?) "
                f"WHERE leg_id=? AND {_UNSETTLED}",
                (self._stamp_now(), reason, str(run_id)),
            ).rowcount
            db.execute("DELETE FROM leases WHERE leg_id=?", (str(run_id),))
            return bool(updated)

    def active_reservation_runs(self, owner_id: str) -> tuple[RunId, ...]:
        repository, role = _scope(owner_id)
        with self._read() as db:
            rows = db.execute(
                f"SELECT leg_id FROM legs WHERE repository=? AND role=? AND {_UNSETTLED} ORDER BY reserved_at",
                (repository, role),
            ).fetchall()
        return tuple(RunId(row["leg_id"]) for row in rows)

    def stale_active_reservation_runs(self, owner_id: str, created_before: datetime) -> tuple[RunId, ...]:
        """Ended work needs settlement at once; the spawn grace protects the rest.

        Scoped to the owner's repository and role, not its deployment: a
        repointed deployment recovers its predecessor's legs, and a live leg of
        any deployment is protected by its lease.
        """
        repository, role = _scope(owner_id)
        with self._read() as db:
            rows = db.execute(
                f"SELECT leg_id FROM legs WHERE repository=? AND role=? AND {_UNSETTLED} "
                "AND (reserved_at<=? OR ended_at IS NOT NULL) ORDER BY reserved_at",
                (repository, role, _stamp(created_before)),
            ).fetchall()
        return tuple(RunId(row["leg_id"]) for row in rows)

    def run_completed_at(self, run_id: RunId) -> float | None:
        """The harness-owned end of a leg (epoch seconds), or None while it has none."""
        with self._read() as db:
            row = db.execute("SELECT ended_at FROM legs WHERE leg_id=?", (str(run_id),)).fetchone()
        return _parse_stamp(row["ended_at"]).timestamp() if row is not None and row["ended_at"] else None

    def leg_runtime(self, run_id: RunId) -> str | None:
        with self._read() as db:
            row = db.execute("SELECT runtime FROM legs WHERE leg_id=?", (str(run_id),)).fetchone()
        return str(row["runtime"]) if row is not None else None

    def record_owned_cancellation_intents(self, owner_id: str, reason: str) -> tuple[RunCancellationIntent, ...]:
        """Mark this deployment's in-flight legs as stopped by shutdown, and
        return every unsettled leg shutdown stopped (including ones that have
        ended since).

        A killed provider process can leave usage that becomes readable only
        after it is reaped, so the reservation stays until post-kill or boot
        recovery settles that evidence.

        The intent is `end_reason` set while `ended_at` is not: every other
        writer of `end_reason` (retention, reclaim, release) sets `ended_at` in
        the same statement, so the two states cannot be confused.
        """
        if not owner_id or not reason:
            raise ValueError("run owner and cancellation reason are required")
        with self._transaction() as db:
            db.execute(
                f"UPDATE legs SET end_reason=? WHERE owner=? AND {_UNSETTLED} "
                "AND ended_at IS NULL AND end_reason IS NULL",
                (reason, owner_id),
            )
            rows = db.execute(
                f"SELECT leg_id, kind, issue, end_reason FROM legs WHERE owner=? AND {_UNSETTLED} "
                "AND end_reason=? ORDER BY reserved_at", (owner_id, reason),
            ).fetchall()
        return tuple(
            RunCancellationIntent(RunId(row["leg_id"]), row["kind"], row["issue"], row["end_reason"])
            for row in rows
        )

    def has_cancellation_intent(self, run_id: RunId) -> bool:
        """Shutdown asked this leg to stop while it was still in flight."""
        with self._read() as db:
            return db.execute(
                f"SELECT 1 FROM legs WHERE leg_id=? AND {_UNSETTLED} "
                "AND ended_at IS NULL AND end_reason IS NOT NULL",
                (str(run_id),),
            ).fetchone() is not None

    # -- legs ----------------------------------------------------------------

    def record_result(
        self, run_id: RunId, leg_class: str, *, session: str = "",
        result: Mapping[str, object] | None = None,
    ) -> bool:
        """Record what a leg returned, before it settles. A leg ended without
        one (died, reclaimed, refused) is a machine end to derive. Recorded
        once; a different second record is refused."""
        encoded = _json(result or {})
        with self._transaction() as db:
            row = db.execute(
                "SELECT leg_class, session_id, result_json FROM legs WHERE leg_id=?", (str(run_id),),
            ).fetchone()
            if row is None:
                raise KeyError(str(run_id))
            if row["leg_class"] is not None:
                return tuple(row) == (leg_class, session or None, encoded)
            db.execute(
                "UPDATE legs SET leg_class=?, session_id=?, result_json=? WHERE leg_id=?",
                (leg_class, session or None, encoded, str(run_id)),
            )
            return True

    def record_unpaid_leg(
        self, run_id: RunId, *, repository: str, role: str, kind: str, owner_id: str,
        issue: int, number: int, head: str, runtime: str,
        leg_class: str | None = None, result: Mapping[str, object] | None = None,
    ) -> None:
        """A leg refused before admission: an ended, zero-cost row, so the
        backoff sees it (#262). With a class it counts as that class."""
        now = self._stamp_now()
        with self._transaction() as db:
            db.execute(
                "INSERT INTO legs(leg_id, repository, role, kind, issue, number, head, owner, runtime, "
                "state, reserved_at, started_at, ended_at, end_reason, currency, reserved_micros, "
                "leg_class, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'released', ?, ?, ?, "
                "'admission_refused', 'USD', 0, ?, ?)",
                (str(run_id), canonical_repository(repository), role, kind, issue, number, head,
                 owner_id, runtime, now, now, now, leg_class,
                 _json(result) if leg_class is not None and result is not None else None),
            )

    def leg_runs(self, repository: str, issues: Iterable[int]) -> tuple[LegRun, ...]:
        """Every ended leg of either role for these issues, with its receipt (if any)."""
        wanted = sorted({n for n in issues if type(n) is int and n > 0})
        out: list[LegRun] = []
        with self._read() as db:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                rows = db.execute(
                    "SELECT kind, issue, number, head, runtime, started_at, ended_at, leg_class, "
                    "session_id, result_json FROM legs WHERE repository=? AND role IN ('fix', 'review') "
                    f"AND ended_at IS NOT NULL AND issue IN ({','.join('?' for _ in chunk)}) "
                    "ORDER BY ended_at, rowid",
                    (canonical_repository(repository), *chunk),
                ).fetchall()
                out.extend(_leg_run(row) for row in rows)
        return tuple(out)

    # -- leases ----------------------------------------------------------------

    def lease(self, repository: str, role: str, target: int, owner_id: str) -> bool:
        """Claim one item for one role across processes and deployments. The
        claim holds this daemon's process until the leg's own registers."""
        repository = canonical_repository(repository)
        if not repository or role not in _PAID_ROLES or target == 0 or not owner_id:
            raise ValueError("lease identity is incomplete")
        identity = ProcessSupervisor.process_identity(os.getpid())
        if identity is None:
            raise ProcessLookupError("lease holder identity is unavailable")
        key = (repository, role, target)
        with self._read() as db:
            seen = _lease_row(db, key)
        # Probed before the write transaction; a row changed since is live.
        stale = seen is not None and not _lease_live(seen)
        with self._transaction() as db:
            current = _lease_row(db, key)
            if current is not None:
                if not stale or seen is None or tuple(current) != tuple(seen):
                    return False
                db.execute("DELETE FROM leases WHERE repository=? AND role=? AND target=?", key)
            db.execute(
                "INSERT INTO leases(repository, role, target, owner, pid, pgid, start_ticks, acquired_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*key, owner_id, identity.pid, os.getpgid(0), identity.start_ticks, self._stamp_now()),
            )
            return True

    def unlease(self, repository: str, role: str, target: int, owner_id: str) -> bool:
        """Release a claim. A bound leg that never launched (still `reserved`)
        is released with it: it charges nothing and holds no usage to wait
        for, so a failed release on the runtime's error path cannot leave the
        daemon-pid lease and its reservation stuck until restart.

        A bound `running` leg keeps the lease: its process (killed at
        shutdown, or crashed) may have left usage that only becomes readable
        once it is reaped, and settling it at a zero estimate now could not be
        corrected later. Liveness and dead-lease recovery govern it instead.
        """
        key = (canonical_repository(repository), role, target)
        with self._transaction() as db:
            row = db.execute(
                "SELECT l.leg_id, g.state FROM leases l LEFT JOIN legs g ON g.leg_id=l.leg_id "
                "WHERE l.repository=? AND l.role=? AND l.target=? AND l.owner=?",
                (*key, owner_id),
            ).fetchone()
            if row is None or row["state"] == "running":
                return False
            if row["state"] == "reserved":
                self._release_reserved(db, row["leg_id"], "unlaunched_reservation")
            db.execute("DELETE FROM leases WHERE repository=? AND role=? AND target=?", key)
            return True

    def register_process_lease(
        self, run_id: RunId, *, expected_owner_id: str, pid: int, pgid: int, process_start_ticks: int,
    ) -> None:
        """Bind a leg's spawned process group to its lease before paid work runs
        (ADR 0005); the leg is now running."""
        if (
            not expected_owner_id
            or type(pid) is not int or pid <= 0
            or type(pgid) is not int or pgid <= 0
            or type(process_start_ticks) is not int or process_start_ticks <= 0
        ):
            raise ValueError("live process identity is required")
        with self._transaction() as db:
            leg = db.execute("SELECT state, owner FROM legs WHERE leg_id=?", (str(run_id),)).fetchone()
            if leg is None:
                raise ControlStoreError(f"process lease has no reserved leg: {run_id}")
            if leg["state"] not in ("reserved", "running"):
                raise ControlStoreError(f"process lease cannot attach to a {leg['state']} leg: {run_id}")
            if leg["owner"] != expected_owner_id:
                raise ControlStoreError(f"leg {run_id} belongs to {leg['owner']!r}, not {expected_owner_id!r}")
            lease = db.execute(
                "SELECT pid, pgid, start_ticks FROM leases WHERE leg_id=?", (str(run_id),),
            ).fetchone()
            if lease is None:
                raise ControlStoreError(f"leg {run_id} holds no lease")
            replacing = (int(lease["pid"]), int(lease["start_ticks"])) != (pid, process_start_ticks)
            # A transport retry shares its paid leg; it replaces the prior
            # process group only once that group is gone.
            if leg["state"] == "running" and replacing and (
                ProcessSupervisor.matches_identity(int(lease["pid"]), int(lease["start_ticks"]))
                or not ProcessSupervisor.group_is_absent(int(lease["pgid"]))
            ):
                raise ControlStoreError(f"conflicting live process lease for leg: {run_id}")
            now = self._stamp_now()
            db.execute(
                "UPDATE leases SET pid=?, pgid=?, start_ticks=? WHERE leg_id=?",
                (pid, pgid, process_start_ticks, str(run_id)),
            )
            db.execute(
                "UPDATE legs SET state='running', started_at=COALESCE(started_at, ?) WHERE leg_id=?",
                (now, str(run_id)),
            )

    def leases(self) -> tuple[Lease, ...]:
        with self._read() as db:
            rows = db.execute("SELECT * FROM leases ORDER BY acquired_at").fetchall()
        return tuple(
            Lease(
                row["repository"], row["role"], int(row["target"]), row["owner"],
                RunId(row["leg_id"]) if row["leg_id"] is not None else None,
                int(row["pid"]), int(row["pgid"]), int(row["start_ticks"]),
                _parse_stamp(row["acquired_at"]),
            )
            for row in rows
        )

    def held_targets(self, repository: str) -> frozenset[int]:
        """Items either role holds a live lease on: its process is alive, or
        its bound leg has not ended. Read-only; rows are probed outside any
        write transaction."""
        with self._read() as db:
            rows = db.execute(
                "SELECT l.target, l.pid, l.start_ticks, g.state, g.ended_at FROM leases l "
                "LEFT JOIN legs g ON g.leg_id=l.leg_id WHERE l.repository=?",
                (canonical_repository(repository),),
            ).fetchall()
        return frozenset(int(row["target"]) for row in rows if _lease_live(row))

    def reclaim_dead_leases(
        self, owner_id: str, is_alive: Callable[[int, int], bool],
    ) -> tuple[RunId, ...]:
        """Drop leases of the owner's repository and role whose process is gone;
        a running leg they held has ended and awaits spend reconciliation.

        Liveness probes (``ps`` on macOS) run before the write transaction,
        which revalidates each row so a lease replaced meanwhile survives.
        """
        repository, role = _scope(owner_id)
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM leases WHERE repository=? AND role=?", (repository, role),
            ).fetchall()
        dead = [tuple(row) for row in rows if not is_alive(int(row["pid"]), int(row["start_ticks"]))]
        reclaimed: list[RunId] = []
        with self._transaction() as db:
            for identity in dead:
                current = db.execute(
                    "SELECT * FROM leases WHERE repository=? AND role=? AND target=?", identity[:3],
                ).fetchone()
                if current is None or tuple(current) != identity:
                    continue
                db.execute("DELETE FROM leases WHERE repository=? AND role=? AND target=?", identity[:3])
                leg_id = current["leg_id"]
                if leg_id is not None and db.execute(
                    "UPDATE legs SET ended_at=?, end_reason=COALESCE(end_reason, 'dead_lease') "
                    "WHERE leg_id=? AND state='running' AND ended_at IS NULL",
                    (self._stamp_now(), leg_id),
                ).rowcount:
                    reclaimed.append(RunId(leg_id))
        return tuple(reclaimed)

    # -- idempotency -----------------------------------------------------------

    def claim_key(self, key: str) -> str:
        """Claim an idempotency key before its remote effect. Returns "new" when
        this call claimed it, "claimed" when an earlier claim never confirmed
        (the effect may or may not have landed: read it back), "done" when the
        effect was confirmed."""
        if not key:
            raise ValueError("idempotency key is required")
        with self._transaction() as db:
            row = db.execute("SELECT state FROM idempotency WHERE key=?", (key,)).fetchone()
            if row is not None:
                return str(row["state"])
            db.execute(
                "INSERT INTO idempotency(key, state, claimed_at) VALUES (?, 'claimed', ?)",
                (key, self._stamp_now()),
            )
            return "new"

    def confirm_key(self, key: str) -> None:
        with self._transaction() as db:
            db.execute("UPDATE idempotency SET state='done' WHERE key=?", (key,))


class ReadOnlyControlStore(SQLiteControlStore):
    """The store's reads over one `mode=ro`, `query_only` connection, for a
    consumer that must never write the authority (the dashboard): no
    journal-mode or synchronous pragma, no checkpoint, no creation, no
    cutover. Opening refuses a missing, legacy or damaged file. Every write
    path raises. Use as a context manager; the connection closes on exit."""

    def __init__(self, path: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        super().__init__(path, busy_timeout_ms=busy_timeout_ms)
        self._db: sqlite3.Connection | None = None

    def __enter__(self) -> "ReadOnlyControlStore":
        if not os.path.isfile(self.path):
            raise ControlStoreError(f"control store does not exist: {self.path}")
        try:
            db = sqlite3.connect(f"{Path(self.path).as_uri()}?mode=ro", uri=True,
                                 timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        except sqlite3.Error as exc:
            raise ControlStoreError(f"control store could not be opened read-only: {exc}") from exc
        try:
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            db.execute("PRAGMA query_only=ON")
            _check_authority(db)
        except BaseException:
            db.close()
            raise
        self._db = db
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def initialize(self) -> None:
        raise ControlStoreError("a read-only control store cannot be initialized")

    def _connect(self) -> sqlite3.Connection:
        raise ControlStoreError("a read-only control store cannot write")

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        if self._db is None:
            raise ControlStoreError("the read-only control store is not open")
        yield self._db

    def query(self, sql: str, parameters: Iterable[object] = ()) -> list[sqlite3.Row]:
        """Rows of one SELECT (the connection is `query_only`)."""
        with self._read() as db:
            return db.execute(sql, tuple(parameters)).fetchall()


def _refuse_prior_use(path: str, opened_before: bool) -> None:
    """A missing store is created only where none ever was.

    Recreating it over a lost one would silently reopen every daily and
    lifetime cap at zero, and a leftover -wal would be replayed into the
    "fresh" file. The evidence needs no marker of its own: every open by this
    release or the previous one leaves `<path>.initialize.lock` (never
    removed), SQLite leaves -wal/-shm beside a store it had open, the cutover
    leaves `.retired-v*`, and the previous release left its `.authority`
    witness. A concurrent first open is not prior use: it holds the lock until
    the store exists, so this check never sees the store missing then.
    """
    directory, name = os.path.split(path)
    evidence = sorted(
        str(candidate) for candidate in (
            *(Path(path + suffix) for suffix in ("-wal", "-shm", ".authority")),
            *Path(directory).glob(f"{name}.retired-v*"),
        )
        if os.path.lexists(candidate)
    )
    if opened_before:
        evidence.insert(0, f"{path}.initialize.lock")
    if not evidence:
        return
    refusal = (
        f"control store {path} is missing but was used before ({', '.join(evidence)}); "
        "refusing to recreate it empty, which would reset spend and caps to zero. Stop every role daemon and "
    )
    retired = sorted(item for item in evidence if ".retired-v" in item)
    if retired:
        # The retired pre-cutover file is the only record of that spend:
        # renamed back, it is cut over again on the next start. It left WAL
        # mode before it was retired, so any -wal/-shm here belong to the lost
        # store and would be replayed into it.
        raise CorruptControlStore(
            refusal + f"restore a backup of {path}, or move any -wal/-shm listed aside and rename "
            f"{' or '.join(retired)} back to {path}: it is cut over again on start and keeps the "
            "pre-cutover spend. Do not delete it."
        )
    raise CorruptControlStore(
        refusal + "restore control.db (with its -wal, if any) from a backup, or, to deliberately "
        "start with no spend history, delete the listed files and start again."
    )


def _create(path: str) -> None:
    """A fresh store appears at `path` complete or not at all."""
    temporary = f"{path}.new-{uuid.uuid4().hex}"
    try:
        _write_schema(temporary)
        os.replace(temporary, path)
        _fsync_directory(os.path.dirname(path))
    finally:
        _remove(temporary)


def _write_schema(path: str, populate: Callable[[sqlite3.Connection], None] | None = None) -> None:
    """Write a complete current-schema file in rollback-journal mode, so no
    -wal file outlives it when it is renamed into place."""
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _DDL:
                db.execute(statement)
            if populate is not None:
                populate(db)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            db.rollback()
            raise
        db.commit()
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_authority(db: sqlite3.Connection) -> None:
    """Refuse a store this code does not own: a newer or legacy schema, or a
    file SQLite finds damaged."""
    try:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            legacy = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            raise IncompatibleSchema(
                "control store predates the 3-table cutover; start a role daemon once to cut it over"
                if legacy is not None
                else f"control store schema {version} is not {SCHEMA_VERSION}"
            )
        result = db.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise CorruptControlStore(f"control-store integrity check failed: {exc}") from exc
    if result != "ok":
        raise CorruptControlStore(f"control-store integrity check failed: {result}")


def _balance(db: sqlite3.Connection, scope: BudgetScope) -> tuple[int, int]:
    """Settled and still-reserved micros of every leg inside one budget scope.

    A scope is a dimension over the legs reserved in its period: a
    repository's day, every repository's day (``global``), or one issue's
    lifetime (``issue:<n>``).
    """
    where = ["currency=?", "reserved_at>=?", "reserved_at<?"]
    parameters: list[object] = [
        scope.currency, _stamp(scope.period_start), _stamp(scope.period_end),
    ]
    dimension, _, issue = scope.dimension.partition(":")
    if dimension != "global":
        where.append("repository=?")
        parameters.append(scope.repository)
    if dimension == "issue":
        if not issue.isdigit():
            raise ValueError(f"malformed issue scope: {scope.dimension!r}")
        where.append("issue=?")
        parameters.append(int(issue))
    elif dimension not in ("repository", "global"):
        raise ValueError(f"unknown budget dimension: {scope.dimension!r}")
    row = db.execute(
        "SELECT COALESCE(SUM(CASE state WHEN 'ended' THEN charged_micros ELSE 0 END), 0), "
        f"COALESCE(SUM(CASE WHEN {_UNSETTLED} THEN reserved_micros ELSE 0 END), 0) "
        f"FROM legs WHERE {' AND '.join(where)}",
        parameters,
    ).fetchone()
    return int(row[0]), int(row[1])


def _scope(owner_id: str) -> tuple[str, str]:
    """The repository and role an owner identity (`<repository>:<role>[:…]`) names."""
    repository, _, rest = owner_id.partition(":")
    role = rest.partition(":")[0]
    if not repository or not role:
        raise ValueError(f"owner identity names no repository and role: {owner_id!r}")
    return canonical_repository(repository), role


def _lease_row(db: sqlite3.Connection, key: tuple[str, str, int]) -> sqlite3.Row | None:
    return db.execute(
        "SELECT l.*, g.state, g.ended_at FROM leases l LEFT JOIN legs g ON g.leg_id=l.leg_id "
        "WHERE l.repository=? AND l.role=? AND l.target=?",
        key,
    ).fetchone()


def _lease_live(row: sqlite3.Row) -> bool:
    if row["state"] in ("reserved", "running") and row["ended_at"] is None:
        return True
    return ProcessSupervisor.matches_identity(int(row["pid"]), int(row["start_ticks"]))


def _leg_run(row: sqlite3.Row) -> LegRun:
    receipt: dict[str, object] | None = None
    if row["leg_class"] is not None:
        try:
            result = json.loads(row["result_json"]) if row["result_json"] else {}
        except json.JSONDecodeError:
            result = {}
        receipt = {
            **(result if isinstance(result, dict) else {}),
            "leg": row["leg_class"],
            "session": row["session_id"] or "",
            "runtime": row["runtime"],
        }
    issue = int(row["issue"])
    return LegRun(
        str(row["kind"]),
        int(row["number"]) or issue,
        issue,
        str(row["head"]),
        _parse_stamp(row["started_at"]) if row["started_at"] else None,
        _parse_stamp(row["ended_at"]),
        receipt,
    )


# -- cutover from the legacy schema ---------------------------------------------


def _is_legacy(path: str) -> bool:
    try:
        with closing(sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)) as db:
            return db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone() is not None
    except sqlite3.DatabaseError as exc:
        raise CorruptControlStore(f"control store cannot be read: {exc}") from exc


def _cut_over(path: str, now: datetime) -> None:
    """Replace a legacy store with the 3-table schema, carrying its money.

    Settled charges become `carried` legs, so daily and lifetime spend survive
    (refactor decision Q5), and unconfirmed or confirmed comment keys carry
    over; workflow rows, receipts, holds and every other namespace are dropped
    and re-derived from GitHub. Refused while any legacy run may still spend.

    Crash-safe at every step: the new file is written complete beside the old
    one, the old file gains its retired name by a hard link, and one atomic
    rename swaps the new file in. `path` names the old or the new store at
    every instant, never neither.
    """
    directory = os.path.dirname(path)
    for leftover in Path(directory).glob(f"{os.path.basename(path)}.cutover-*"):
        _remove(str(leftover))
    with closing(sqlite3.connect(path, isolation_level=None)) as legacy:
        legacy.row_factory = sqlite3.Row
        # fetchall: a half-read statement keeps a read transaction open, and
        # SQLite cannot leave WAL mode inside one.
        quick_check = legacy.execute("PRAGMA quick_check").fetchall()[0][0]
        if quick_check != "ok":
            raise CorruptControlStore(f"legacy control store failed its integrity check: {quick_check}")
        # Refused before anything is written, so a refusal leaves the file as
        # the previous release expects it.
        _refuse_unsettled(legacy, path)
        # Leaving WAL needs every other connection gone at this instant, so a
        # daemon or dashboard of the previous release caught mid-operation
        # refuses the cutover. It is point-in-time only: the previous release
        # opens a connection per operation, so an idle one passes and its next
        # operation meets the new schema and fails closed. Stopping every
        # daemon first (HARNESS.md) is the real guard. It also folds the -wal
        # into the file the retired name keeps.
        try:
            mode = legacy.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        except sqlite3.OperationalError:
            mode = "busy"
        if mode != "delete":
            raise CutoverRefused(
                f"another process holds {path} open; stop every role daemon and the dashboard, then start again"
            )
        legacy.execute("BEGIN EXCLUSIVE")
        try:
            _refuse_unsettled(legacy, path)
            recorded = legacy.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if type(recorded) is not int:
                raise CorruptControlStore(f"legacy control store records no schema version: {path}")
            version = recorded
            expected = legacy.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_micros), 0) FROM charges"
            ).fetchone()
            temporary = f"{path}.cutover-{uuid.uuid4().hex}"
            try:
                _write_schema(temporary, lambda db: _carry(db, legacy))
                with closing(sqlite3.connect(temporary)) as written:
                    carried = written.execute(
                        "SELECT COUNT(*), COALESCE(SUM(charged_micros), 0) FROM legs"
                    ).fetchone()
                if tuple(carried) != tuple(expected):
                    raise CorruptControlStore(
                        f"cutover carried {tuple(carried)} legs/micros, the legacy ledger holds {tuple(expected)}"
                    )
                _verify_scopes(legacy, temporary)
                _retire(path, version, now)
                # The legacy -wal was folded in when it left WAL; a stale
                # -wal or -shm must not be read as the new file's.
                for suffix in ("-wal", "-shm"):
                    _remove(path + suffix)
                os.replace(temporary, path)
                _fsync_directory(directory)
            finally:
                _remove(temporary)
        finally:
            legacy.rollback()


def _refuse_unsettled(legacy: sqlite3.Connection, path: str) -> None:
    rows = legacy.execute(
        "SELECT r.run_id, r.owner_id FROM runs r LEFT JOIN budget_reservations b ON b.run_id=r.run_id "
        "WHERE r.state IN ('reserved', 'starting', 'running') OR b.state='active' "
        "OR EXISTS (SELECT 1 FROM leases l WHERE l.run_id=r.run_id) ORDER BY r.run_id"
    ).fetchall()
    if rows:
        owners = sorted({str(row["owner_id"]) for row in rows})
        raise CutoverRefused(
            f"{path} holds {len(rows)} unsettled legacy run(s) "
            f"({', '.join(str(row['run_id']) for row in rows[:5])}{', …' if len(rows) > 5 else ''}) "
            f"owned by {', '.join(owners)}. The new store would drop their reservations: start the "
            "previous release for each owning deployment, let its boot recovery settle them "
            "(`issue-agent status` shows no in-flight legs), stop it, then start this release."
        )


def _carry(new: sqlite3.Connection, legacy: sqlite3.Connection) -> None:
    columns = {
        table: {row[1] for row in legacy.execute(f"PRAGMA table_info({table})")}
        for table in ("budget_reservations", "charges")
    }
    workload = (
        "b.workload_repository" if "workload_repository" in columns["budget_reservations"] else "'*'"
    )
    origin = "c.origin" if "origin" in columns["charges"] else "'normal_return'"
    rows = legacy.execute(
        f"SELECT r.run_id, r.kind, r.owner_id, r.dispatch_id, r.started_at, r.completed_at, r.outcome, "
        f"b.repository, {workload} AS workload, b.created_at, b.maximum_micros, "
        f"c.amount_micros, c.currency, c.basis, {origin} AS origin, c.usage_json, c.rate_card_id, "
        "c.rate_card_version, c.provider_receipt_id, c.settled_at, "
        "(SELECT MIN(l.dimension) FROM reservation_limits l WHERE l.run_id=r.run_id "
        " AND l.dimension LIKE 'issue:%') AS issue_dimension "
        "FROM charges c JOIN runs r ON r.run_id=c.run_id "
        "JOIN budget_reservations b ON b.run_id=c.run_id ORDER BY c.charge_id"
    )
    for row in rows:
        new.execute(
            "INSERT INTO legs(leg_id, repository, role, kind, issue, number, owner, runtime, "
            "state, reserved_at, started_at, ended_at, end_reason, currency, reserved_micros, "
            "charged_micros, basis, origin, usage_json, rate_card_id, rate_card_version, "
            "provider_receipt_id, settled_at) "
            "VALUES (?, ?, 'carried', ?, ?, ?, ?, ?, 'ended', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _carried_leg(row),
        )
    new.executemany(
        "INSERT INTO idempotency(key, state, claimed_at) VALUES (?, ?, ?)",
        (
            (f"fix-comment:{row['scope']}:{row['key']}",
             "done" if row["payload_json"] == '"done"' else "claimed", row["claimed_at"])
            for row in legacy.execute(
                "SELECT scope, key, payload_json, claimed_at FROM dedupe_keys WHERE namespace='fix-comment'"
            )
        ),
    )


def _carried_leg(row: sqlite3.Row) -> tuple[object, ...]:
    try:
        dispatch = json.loads(row["dispatch_id"])
    except (TypeError, ValueError):
        dispatch = None
    dispatch = dispatch if isinstance(dispatch, dict) else {}

    def positive(value: object) -> int | None:
        return value if type(value) is int and value > 0 else None

    issue = positive(dispatch.get("issue"))
    if issue is None and isinstance(row["issue_dimension"], str):
        suffix = row["issue_dimension"].removeprefix("issue:")
        issue = int(suffix) if suffix.isdigit() and int(suffix) > 0 else None
    repository = next(
        (
            canonical_repository(value)
            for value in (row["workload"], dispatch.get("repository"), row["repository"])
            if isinstance(value, str) and value.strip() not in ("", "*")
        ),
        "*",
    )
    runtime = dispatch.get("runtime")
    return (
        row["run_id"], repository, row["kind"] or "carried", issue,
        positive(dispatch.get("number")) or 0, row["owner_id"] or "carried",
        runtime if isinstance(runtime, str) else "",
        row["created_at"], row["started_at"], row["completed_at"] or row["settled_at"],
        row["outcome"], row["currency"], max(int(row["maximum_micros"]), 0),
        int(row["amount_micros"]), row["basis"], row["origin"], row["usage_json"],
        row["rate_card_id"], row["rate_card_version"], row["provider_receipt_id"], row["settled_at"],
    )


def _verify_scopes(legacy: sqlite3.Connection, carried_path: str) -> None:
    """Every charge a legacy cap scope counted is counted by the same scope
    in the new file, so no repository day, global day or issue lifetime loses
    spend. A carried leg may also count toward a scope its reservation did not
    arm (the new caps count every matching leg): that only makes a historic
    total higher, never lower.
    """
    with closing(sqlite3.connect(f"{Path(carried_path).as_uri()}?mode=ro", uri=True)) as new:
        legs = {
            row[0]: row[1:]
            for row in new.execute("SELECT leg_id, repository, issue, reserved_at, currency FROM legs")
        }
    scopes = legacy.execute(
        "SELECT run_id, repository, dimension, period_start, period_end, currency FROM budget_reservations "
        "WHERE run_id IN (SELECT run_id FROM charges) UNION "
        "SELECT run_id, repository, dimension, period_start, period_end, currency FROM reservation_limits "
        "WHERE run_id IN (SELECT run_id FROM charges)"
    ).fetchall()
    lost: list[str] = []
    for run_id, repository, dimension, start, end, currency in scopes:
        kind, _, issue = str(dimension).partition(":")
        if kind not in ("repository", "global", "issue"):
            continue  # no current cap reads it
        leg = legs.get(run_id)
        counted = leg is not None and leg[3] == currency and (
            _parse_stamp(start) <= _parse_stamp(leg[2]) < _parse_stamp(end)
        ) and (kind == "global" or leg[0] == canonical_repository(repository)) and (
            kind != "issue" or (issue.isdigit() and leg[1] == int(issue))
        )
        if not counted:
            lost.append(f"{run_id} from {repository} {dimension} {start}")
    if lost:
        raise CorruptControlStore(
            f"cutover would drop {len(lost)} charge(s) from the cap scopes that counted them: "
            + "; ".join(lost[:5])
        )


def _retire(path: str, version: int, now: datetime) -> str:
    """Give the legacy file its retired name (a second hard link): the swap
    that follows unlinks only `path`. A crash after this and before the swap
    leaves `path` intact, and the retry reuses the same retired link."""
    for existing in Path(os.path.dirname(path)).glob(f"{os.path.basename(path)}.retired-v*"):
        if os.path.samefile(existing, path):
            return str(existing)
    retired = f"{path}.retired-v{version}-{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    os.link(path, retired)
    _fsync_directory(os.path.dirname(path))
    return retired


def _fsync_directory(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _json(value: object) -> str:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control-store timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("control-store timestamps must be timezone-aware")
    return parsed
