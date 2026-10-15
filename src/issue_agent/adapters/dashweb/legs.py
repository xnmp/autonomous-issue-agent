"""The dashboard's read of the control store's `legs` table.

Rows come through `ReadOnlyControlStore` (`mode=ro`, `query_only`), so the
dashboard can never create, migrate or write the authority the daemons own.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .readonly_store import ReadOnlyControlStore
from ...domain.dashboard import LegRow, Tokens
from ...domain.repository import canonical_repository
from ...domain.triage import parse_triage, steward_from_record

_COLUMNS = (
    "leg_id, role, kind, issue, number, head, runtime, state, reserved_at, started_at, ended_at, "
    "end_reason, reserved_micros, charged_micros, basis, origin, leg_class, session_id, usage_json, result_json"
)


def read_legs(store: ReadOnlyControlStore, repository: str | None = None) -> tuple[LegRow, ...]:
    """Every leg of `repository` (every repository when None), oldest first."""
    where, args = ("WHERE repository=?", (canonical_repository(repository),)) if repository else ("", ())
    rows = store.query(f"SELECT {_COLUMNS} FROM legs {where} ORDER BY reserved_at, rowid", args)
    return tuple(leg for row in rows if (leg := parse_leg(row)) is not None)


def parse_leg(row: sqlite3.Row) -> LegRow | None:
    """One row as a `LegRow`; a row whose identity or stamps do not parse is
    skipped rather than failing the whole read."""
    reserved = _stamp(row["reserved_at"])
    if reserved is None or not row["leg_id"] or not row["kind"]:
        return None
    result = _object(row["result_json"])
    verdict = result.get("verdict") if isinstance(result.get("verdict"), dict) else {}
    issue = row["issue"]
    triage = parse_triage(result.get("triage")) if row["kind"] == "TRIAGE" else None
    steward = steward_from_record(result.get("steward")) if row["kind"] == "STEWARD" else None
    return LegRow(
        leg_id=str(row["leg_id"]),
        role=str(row["role"] or ""),
        kind=str(row["kind"]),
        issue=int(issue) if type(issue) is int and issue > 0 else None,
        number=int(row["number"] or 0),
        head=str(row["head"] or ""),
        runtime=str(row["runtime"] or ""),
        state=str(row["state"] or ""),
        reserved_at=reserved,
        started_at=_stamp(row["started_at"]),
        ended_at=_stamp(row["ended_at"]),
        end_reason=str(row["end_reason"] or ""),
        reserved_usd=_usd(row["reserved_micros"]) or 0.0,
        charged_usd=_usd(row["charged_micros"]),
        basis=str(row["basis"] or ""),
        origin=str(row["origin"] or ""),
        leg_class=str(row["leg_class"] or ""),
        session_id=str(row["session_id"] or ""),
        tokens=_tokens(_object(row["usage_json"])),
        summary=_text(result.get("summary")),
        question=_text(result.get("question")),
        verdict=_text(verdict.get("event")).upper(),
        verdict_body=_text(verdict.get("body")),
        tier=triage.tier.value if triage is not None else _text(result.get("tier")),
        triage=triage,
        steward=steward,
    )


def _stamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _usd(micros: object) -> float | None:
    return micros / 1_000_000 if type(micros) is int else None


def _object(raw: object) -> dict:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _tokens(usage: dict) -> Tokens | None:
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")]
    if type(counts[0]) is not int or type(counts[1]) is not int:
        return None
    return Tokens(*(value if type(value) is int else 0 for value in counts))
