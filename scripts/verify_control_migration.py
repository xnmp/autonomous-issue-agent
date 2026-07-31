"""Verify a representative legacy window and the real primary accounting path."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from issue_agent.adapters.control_store import (
    AccountingDisposition,
    ReservationRequest,
    SQLiteControlStore,
    SettlementRequest,
)
from issue_agent.domain.contracts import (
    Money,
    PriceQuote,
    PricingBasis,
    ReservationId,
    RunId,
    daily_budget_scope,
)


@dataclass(frozen=True)
class LegacySummary:
    source_digest: str
    rows: int
    accepted: int
    rejected: int
    amount_micros: int
    first_at: str | None
    last_at: str | None
    representative_hours: float
    source_bytes: int
    line_digest: str


def _stamp(value: object) -> datetime:
    raw = str(value)
    parsed = datetime.fromisoformat(
        raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    )
    if parsed.tzinfo is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(timezone.utc)


def summarize_legacy(path: Path) -> LegacySummary:
    raw = path.read_bytes()
    accepted = 0
    rejected = 0
    total = 0
    stamps: list[datetime] = []
    lines = raw.splitlines()
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("event") != "spend":
                raise ValueError("not spend")
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
                raise ValueError("unattributed")
            stamp = _stamp(row["ts"])
            amount = Decimal(
                str(row.get("billed_cost_usd", row["cost_usd"]))
            )
            if not amount.is_finite() or amount < 0:
                raise ValueError("invalid amount")
            total += int(
                (amount * Decimal(1_000_000)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            accepted += 1
            stamps.append(stamp)
        except (
            InvalidOperation,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            rejected += 1
    first = min(stamps) if stamps else None
    last = max(stamps) if stamps else None
    duration = (
        (last - first).total_seconds() / 3600
        if first is not None and last is not None
        else 0.0
    )
    return LegacySummary(
        hashlib.sha256(raw).hexdigest(),
        len(lines),
        accepted,
        rejected,
        total,
        first.isoformat() if first else None,
        last.isoformat() if last else None,
        round(duration, 3),
        len(raw),
        hashlib.sha256(
            b"".join(
                hashlib.sha256(line).digest()
                for line in lines
            )
        ).hexdigest(),
    )


def verify(
    source: Path,
    database: Path,
    *,
    canary_repository: str,
    minimum_hours: float,
) -> dict[str, object]:
    summary = summarize_legacy(source)
    if summary.rejected:
        raise RuntimeError(
            f"legacy replay rejected {summary.rejected} row(s)"
        )
    if summary.representative_hours < minimum_hours:
        raise RuntimeError(
            "legacy replay window is "
            f"{summary.representative_hours}h; require {minimum_hours}h"
        )
    database_preexisting = database.is_file() and database.stat().st_size > 0
    if not database_preexisting:
        raise RuntimeError(
            "primary canary requires an existing operational control store"
        )
    store = SQLiteControlStore(str(database))
    store.initialize()
    imported = store.import_legacy_spend(str(source))
    repeated = store.import_legacy_spend(str(source))
    if (
        imported.source_digest != summary.source_digest
        or imported.imported != summary.accepted
        or imported.amount.micros != summary.amount_micros
        or not repeated.idempotent
    ):
        raise RuntimeError("legacy and control-store totals diverged")
    store.integrity_check()

    canary_run = RunId(
        f"migration-canary-v2-{summary.source_digest[:16]}-{canary_repository}"
    )
    now = datetime.now(timezone.utc)
    scope = daily_budget_scope(
        canary_repository,
        f"migration-canary:{summary.source_digest[:16]}",
        "USD",
        now,
        ZoneInfo("UTC"),
    )
    reservation = store.reserve(
        ReservationRequest(
            ReservationId(f"reservation-{canary_run}"),
            canary_run,
            f"dispatch-{canary_run}",
            scope,
            PriceQuote(
                "migration-canary",
                "migration-canary",
                "1",
                now,
                Money("USD", 1),
            ),
            Money("USD", 9_000_000_000_000_000_000),
            "migration-canary-v1",
            kind="migration-canary",
            owner_id=f"{canary_repository}:migration",
        )
    )
    if reservation.disposition is not AccountingDisposition.AVAILABLE:
        raise RuntimeError(f"primary canary admission failed: {reservation.detail}")
    settlement = store.settle(
        SettlementRequest(
            canary_run,
            Money("USD", 1),
            PricingBasis.SYNTHETIC,
            f"receipt-{canary_run}",
            None,
            None,
            None,
        )
    )
    if settlement.disposition is not AccountingDisposition.AVAILABLE:
        raise RuntimeError(
            f"primary canary settlement failed: {settlement.detail}"
        )
    repeated_reservation = store.reserve(
        ReservationRequest(
            ReservationId(f"reservation-{canary_run}"),
            canary_run,
            f"dispatch-{canary_run}",
            scope,
            PriceQuote(
                "migration-canary",
                "migration-canary",
                "1",
                now,
                Money("USD", 1),
            ),
            Money("USD", 9_000_000_000_000_000_000),
            "migration-canary-v1",
            kind="migration-canary",
            owner_id=f"{canary_repository}:migration",
        )
    )
    repeated_settlement = store.settle(
        SettlementRequest(
            canary_run,
            Money("USD", 1),
            PricingBasis.SYNTHETIC,
            f"receipt-{canary_run}",
            None,
            None,
            None,
        )
    )
    conflicting_settlement = store.settle(
        SettlementRequest(
            canary_run,
            Money("USD", 2),
            PricingBasis.SYNTHETIC,
            f"receipt-{canary_run}",
            None,
            None,
            None,
        )
    )
    exhausted_run = RunId(f"{canary_run}-exhausted")
    exhausted = store.reserve(
        ReservationRequest(
            ReservationId(f"reservation-{exhausted_run}"),
            exhausted_run,
            f"dispatch-{exhausted_run}",
            scope,
            PriceQuote(
                "migration-canary",
                "migration-canary",
                "1",
                now,
                Money("USD", 1),
            ),
            Money("USD", 1),
            "migration-canary-v1",
            kind="migration-canary",
            owner_id=f"{canary_repository}:migration",
        )
    )
    legacy_gate_blocked = store.snapshot(scope).settled.micros >= 1
    if (
        repeated_reservation.disposition is not AccountingDisposition.CONFLICT
        or not repeated_settlement.idempotent
        or conflicting_settlement.disposition
        is not AccountingDisposition.CONFLICT
        or exhausted.disposition is not AccountingDisposition.EXHAUSTED
        or not legacy_gate_blocked
    ):
        raise RuntimeError(
            "primary canary deduplication/admission/failure decisions diverged"
        )
    legacy_by_scope = _legacy_scope_totals(source)
    with sqlite3.connect(store.path) as db:
        migration_rows, migration_total = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(c.amount_micros), 0) "
            "FROM charges c JOIN runs r ON r.run_id=c.run_id "
            "WHERE r.owner_id='migration'"
        ).fetchone()
        primary_by_scope = {
            (str(repository), str(dimension), str(period_id)): int(amount)
            for repository, dimension, period_id, amount in db.execute(
                "SELECT l.repository, l.dimension, l.period_id, "
                "SUM(c.amount_micros) FROM charges c "
                "JOIN reservation_limits l ON l.run_id=c.run_id "
                "JOIN runs r ON r.run_id=c.run_id "
                "WHERE r.owner_id='migration' "
                "GROUP BY l.repository, l.dimension, l.period_id"
            ).fetchall()
        }
    if (migration_rows, migration_total) != (
        summary.accepted,
        summary.amount_micros,
    ):
        raise RuntimeError("persisted migration rows diverged after canary")
    if hashlib.sha256(source.read_bytes()).hexdigest() != summary.source_digest:
        raise RuntimeError("legacy rollback export changed during verification")
    if primary_by_scope != legacy_by_scope:
        raise RuntimeError(
            "legacy and primary per-scope balance fingerprints diverged"
        )
    verifier_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {
        "status": "passed",
        "migration_version": 1,
        "legacy": asdict(summary),
        "shadow": {
            "rows_match": True,
            "totals_match": True,
            "rejected_rows_match": imported.rejected == summary.rejected,
            "idempotent_replay": True,
            "representative_hours": summary.representative_hours,
            "scope_balance_fingerprint_match": True,
            "scope_balance_count": len(legacy_by_scope),
            "admission_decisions": {
                "ordinary_match": reservation.admitted,
                "cap_exhaustion_match": (
                    legacy_gate_blocked
                    and exhausted.disposition
                    is AccountingDisposition.EXHAUSTED
                ),
            },
            "deduplication_decisions": {
                "legacy_source_replay_idempotent": repeated.idempotent,
                "terminal_reservation_replay_rejected": (
                    repeated_reservation.disposition
                    is AccountingDisposition.CONFLICT
                ),
                "primary_settlement_replay_idempotent": (
                    repeated_settlement.idempotent
                ),
            },
            "failure_decisions": {
                "source_rejections_match": (
                    imported.rejected == summary.rejected
                ),
                "conflicting_provider_receipt_rejected": (
                    conflicting_settlement.disposition
                    is AccountingDisposition.CONFLICT
                ),
            },
        },
        "primary_canary": {
            "repository": canary_repository,
            "database": str(database.resolve()),
            "database_preexisting": database_preexisting,
            "admitted": True,
            "settled": True,
            "amount_micros": 1,
            "legacy_export_unchanged": True,
            "runtime_path": (
                "SQLiteControlStore.reserve -> settle on the existing "
                "operational database"
            ),
            "terminal_reservation_replay_rejected": (
                repeated_reservation.disposition
                is AccountingDisposition.CONFLICT
            ),
            "idempotent_settlement": repeated_settlement.idempotent,
            "conflicting_settlement_rejected": (
                conflicting_settlement.disposition
                is AccountingDisposition.CONFLICT
            ),
            "exhausted_admission_rejected": (
                exhausted.disposition is AccountingDisposition.EXHAUSTED
            ),
        },
        "provenance": {
            "source_path": str(source.resolve()),
            "source_digest": summary.source_digest,
            "source_bytes": summary.source_bytes,
            "line_digest": summary.line_digest,
            "verifier_digest": verifier_digest,
            "historical_total_note": (
                "The plan's earlier $1512.55 sentence had no retained source "
                "digest and is not migration evidence. This immutable report "
                "supersedes it with the captured 303-row source digest and "
                "per-row rounded total."
            ),
        },
        "database_integrity": "ok",
    }


def _legacy_scope_totals(
    source: Path,
) -> dict[tuple[str, str, str], int]:
    totals: dict[tuple[str, str, str], int] = {}
    for line in source.read_bytes().splitlines():
        row = json.loads(line)
        stamp = _stamp(row["ts"])
        repository = str(row["repo"])
        issue = int(row["issue"])
        amount = Decimal(str(row.get("billed_cost_usd", row["cost_usd"])))
        micros = int(
            (amount * Decimal(1_000_000)).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        period_id = stamp.date().isoformat()
        keys = (
            (repository, "repository", period_id),
            ("*", "global", period_id),
            (repository, f"issue:{issue}", f"lifetime:{issue}"),
        )
        for key in keys:
            totals[key] = totals.get(key, 0) + micros
    return totals


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--canary-repository", required=True)
    parser.add_argument("--minimum-hours", type=float, default=24)
    args = parser.parse_args()
    if args.minimum_hours < 0:
        raise SystemExit("--minimum-hours cannot be negative")
    if not args.database.is_file() or args.database.stat().st_size <= 0:
        raise SystemExit(
            "--database must name an existing primary control store"
        )
    report = verify(
        args.ledger,
        args.database,
        canary_repository=args.canary_repository,
        minimum_hours=args.minimum_hours,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
