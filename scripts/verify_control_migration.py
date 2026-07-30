"""Verify a legacy-spend replay and a primary-store canary without remote I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
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
        f"migration-canary-{summary.source_digest[:16]}-{canary_repository}"
    )
    now = datetime.now(timezone.utc)
    scope = daily_budget_scope(
        canary_repository,
        "repository",
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
    with sqlite3.connect(store.path) as db:
        migration_rows, migration_total = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(c.amount_micros), 0) "
            "FROM charges c JOIN runs r ON r.run_id=c.run_id "
            "WHERE r.owner_id='migration'"
        ).fetchone()
    if (migration_rows, migration_total) != (
        summary.accepted,
        summary.amount_micros,
    ):
        raise RuntimeError("persisted migration rows diverged after canary")
    if hashlib.sha256(source.read_bytes()).hexdigest() != summary.source_digest:
        raise RuntimeError("legacy rollback export changed during verification")
    return {
        "status": "passed",
        "migration_version": 1,
        "legacy": asdict(summary),
        "shadow": {
            "rows_match": True,
            "totals_match": True,
            "idempotent_replay": True,
        },
        "primary_canary": {
            "repository": canary_repository,
            "admitted": True,
            "settled": True,
            "legacy_export_unchanged": True,
        },
        "database_integrity": "ok",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--canary-repository", required=True)
    parser.add_argument("--minimum-hours", type=float, default=24)
    args = parser.parse_args()
    if args.minimum_hours < 0:
        raise SystemExit("--minimum-hours cannot be negative")
    if args.database is not None:
        report = verify(
            args.ledger,
            args.database,
            canary_repository=args.canary_repository,
            minimum_hours=args.minimum_hours,
        )
    else:
        with tempfile.TemporaryDirectory(
            prefix="issue-agent-migration-"
        ) as raw:
            report = verify(
                args.ledger,
                Path(raw) / "control.db",
                canary_repository=args.canary_repository,
                minimum_hours=args.minimum_hours,
            )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
