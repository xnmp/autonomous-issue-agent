"""Bounded stopped SQL evidence: historical attribution grants only import holds."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
import re

from ..domain.continuation_import import (
    MAX_IMPORT_TARGETS, LegacyContinuationRun, digest, legacy_dispatch_object,
    parse_legacy_continuation_run,
)
from ..domain.workflow_identity import WorkflowFamily, canonical_workflow_repository
from .resolve_activation import ResolveActivationRefused
from .sql_read_budget import SqlReadBudgetExceeded, bounded_sql_source_reads


@dataclass(frozen=True)
class LegacyContinuationRunSnapshot:
    runs: tuple[LegacyContinuationRun, ...]
    source_digest: str

    def __post_init__(self) -> None:
        if (not isinstance(self.runs, tuple) or len(self.runs) > MAX_IMPORT_TARGETS
                or any(not isinstance(r, LegacyContinuationRun) for r in self.runs)
                or not isinstance(self.source_digest, str) or not re.fullmatch("[0-9a-f]{64}", self.source_digest)):
            raise ValueError("historical snapshot requires bounded typed runs and source digest")


def _rows(db: sqlite3.Connection, sql: str) -> tuple[dict[str, object], ...]:
    try:
        with bounded_sql_source_reads(db, maximum_bytes=16 * 1024 * 1024):
            cursor = db.execute(sql + " LIMIT ?", (MAX_IMPORT_TARGETS + 1,))
            names = tuple(d[0] for d in cursor.description or ())
            rows = tuple(dict(zip(names, row)) for row in cursor)
    except SqlReadBudgetExceeded as exc:
        raise ResolveActivationRefused("historical SQL evidence is unreadable or oversized") from exc
    if len(rows) > MAX_IMPORT_TARGETS:
        raise ResolveActivationRefused("historical SQL evidence exceeds its row bound")
    if any(not isinstance(v, (str, int, type(None))) or isinstance(v, str) and len(v.encode()) > 65536
           for row in rows for v in row.values()) or len(str(rows).encode()) > 16 * 1024 * 1024:
        raise ResolveActivationRefused("historical SQL evidence exceeds its value bound")
    return rows


def _passive(run: dict[str, object], reservations: tuple[dict[str, object], ...],
             charges: tuple[dict[str, object], ...], limits: tuple[dict[str, object], ...]) -> bool:
    rid = run["run_id"]
    if not isinstance(rid, str) or not re.fullmatch("[0-9a-f]{64}", rid):
        return False
    if (run["kind"], run["owner_id"], run["state"], run["config_version"], run["outcome"],
        run["dispatch_id"], run["provider_receipt_id"]) != (
            "legacy-import", "migration", "completed", "legacy-v1", "settled", f"legacy:{rid}", f"legacy:{rid}"):
        return False
    if len(reservations) != 1 or len(charges) != 1 or len(limits) != 3:
        return False
    reservation, charge = reservations[0], charges[0]
    repository = reservation["workload_repository"]
    if not isinstance(repository, str) or canonical_workflow_repository(repository) != repository:
        return False
    stamp = run["started_at"]
    if not isinstance(stamp, str) or stamp != run["completed_at"] or stamp != charge["settled_at"]:
        return False
    if (reservation["reservation_id"], reservation["repository"], reservation["dimension"],
        reservation["state"], reservation["currency"], reservation["config_version"],
        reservation["rate_card_id"], reservation["rate_card_version"], reservation["maximum_micros"],
        charge["provider_receipt_id"], charge["basis"], charge["currency"]) != (
            f"legacy:{rid}", repository, "repository", "settled", "USD", "legacy-v1", "legacy", "1",
            charge["amount_micros"], f"legacy:{rid}", "synthetic", "USD"):
        return False
    if (reservation["cap_micros"] != 9_000_000_000_000_000_000
            or reservation["period_timezone"] != "UTC" or reservation["quote_effective_at"] != stamp
            or reservation["created_at"] != stamp or reservation["updated_at"] != stamp
            or reservation["release_reason"] is not None
            or any(l["cap_micros"] != 9_000_000_000_000_000_000 or l["period_timezone"] != "UTC" for l in limits)):
        return False
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") != stamp:
            return False
        start = parsed.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    except (ValueError, OverflowError):
        return False
    day, next_day = start.isoformat(timespec="milliseconds").replace("+00:00", "Z"), (start + timedelta(days=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if (reservation["period_id"], reservation["period_start"], reservation["period_end"]) != (start.date().isoformat(), day, next_day):
        return False
    scopes = {(repository, "repository", start.date().isoformat(), day, next_day), ("*", "global", start.date().isoformat(), day, next_day)}
    issue = next((str(l["dimension"])[6:] for l in limits if str(l["dimension"]).startswith("issue:")), "")
    if not issue.isdigit() or str(int(issue)) != issue or int(issue) <= 0:
        return False
    scopes.add((repository, "issue:" + issue, "lifetime:" + issue, "1970-01-01T00:00:00.000Z", "9999-12-31T23:59:59.999Z"))
    return (type(charge["amount_micros"]) is int and charge["amount_micros"] >= 0
            and {(l["repository"], l["dimension"], l["period_id"], l["period_start"], l["period_end"]) for l in limits if l["currency"] == "USD"} == scopes)


def _migration_canary(run: dict[str, object], reservations: tuple[dict[str, object], ...],
                      charges: tuple[dict[str, object], ...], limits: tuple[dict[str, object], ...],
                      repository: str) -> bool:
    """Recognize the verifier's settled accounting probes, never model executions."""
    rid = run['run_id']
    if not isinstance(rid, str):
        return False
    identity = re.fullmatch(r'migration-canary-(?:(v2|v3)-)?([0-9a-f]{16})(?:-([0-9a-f]{16}))?-'
                            + re.escape(repository), rid)
    if identity is None or bool(identity[3]) != (identity[1] == 'v3'):
        return False
    if (run['owner_id'], run['kind'], run['state'], run['outcome'], run['config_version'],
        run['dispatch_id'], run['provider_receipt_id'], run['started_at']) != (
            repository + ':migration', 'migration-canary', 'completed', 'settled', 'migration-canary-v1',
            'dispatch-' + rid, 'receipt-' + rid, None):
        return False
    if any(run[name] is not None for name in ('workflow_binding_json', 'workflow_episode_key', 'review_round_id', 'review_child_id')):
        return False
    continuations = legacy_dispatch_object(str(run['continuations_json']))
    if (set(continuations) != {'format', 'value'} or type(continuations['format']) is not int
            or continuations['format'] != 1 or continuations['value'] != []):
        return False
    if len(reservations) != 1 or len(charges) != 1 or len(limits) != 1:
        return False
    reservation, charge, limit = reservations[0], charges[0], limits[0]
    dimension = 'repository' if identity[1] is None else 'migration-canary:' + identity[2]
    if (reservation['reservation_id'], reservation['repository'], reservation['workload_repository'],
        reservation['dimension'], reservation['state'], reservation['currency'], reservation['maximum_micros'],
        reservation['cap_micros'], reservation['model'], reservation['rate_card_id'], reservation['rate_card_version'],
        reservation['config_version'], reservation['period_timezone'], reservation['release_reason']) != (
            'reservation-' + rid, repository, repository, dimension, 'settled', 'USD', 1,
            9_000_000_000_000_000_000, 'migration-canary', 'migration-canary', '1', 'migration-canary-v1', 'UTC', None):
        return False
    if (charge['provider_receipt_id'], charge['amount_micros'], charge['currency'], charge['basis'],
        charge['usage_json'], charge['rate_card_id'], charge['rate_card_version']) != (
            'receipt-' + rid, 1, 'USD', 'synthetic', None, None, None):
        return False
    scope_fields = ('repository', 'dimension', 'period_id', 'period_start', 'period_end',
                    'period_timezone', 'currency', 'cap_micros')
    if any(limit[name] != reservation[name] for name in scope_fields):
        return False
    # The verifier settles before completing the run; those timestamps can differ.
    stamps = (reservation['quote_effective_at'], reservation['created_at'], charge['settled_at'], run['completed_at'])
    try:
        parsed = tuple(datetime.fromisoformat(str(stamp).replace('Z', '+00:00')) for stamp in stamps)
        if any(value.tzinfo is None or value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z') != stamp
               for value, stamp in zip(parsed, stamps)):
            return False
        start = parsed[0].replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return (start <= parsed[0] <= parsed[1] <= parsed[2] <= parsed[3] < end
                and reservation['updated_at'] == charge['settled_at']
                and (reservation['period_id'], reservation['period_start'], reservation['period_end']) == (
                    start.date().isoformat(), start.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                    end.isoformat(timespec='milliseconds').replace('+00:00', 'Z')))
    except (ValueError, OverflowError):
        return False


def collect_continuation_runs(db: sqlite3.Connection, repository: str,
                              families: tuple[WorkflowFamily, ...]) -> LegacyContinuationRunSnapshot:
    if not db.in_transaction or canonical_workflow_repository(repository) != repository:
        raise ValueError("historical import requires a pinned transaction and canonical repository")
    if families not in ((WorkflowFamily.ISSUE_RECOVERY, WorkflowFamily.PR_REPAIR), (WorkflowFamily.STEWARD,)):
        raise ValueError("historical import requires a closed configured family set")
    raw = _rows(db, "SELECT * FROM runs WHERE workflow_episode_key IS NULL ORDER BY run_id")
    predicate = " WHERE run_id IN (SELECT run_id FROM runs WHERE workflow_episode_key IS NULL) ORDER BY run_id"
    reservations = _rows(db, "SELECT * FROM budget_reservations" + predicate)
    charges = _rows(db, "SELECT * FROM charges" + predicate)
    limits = _rows(db, "SELECT * FROM reservation_limits" + predicate)
    output = []
    evidence = []
    for run in raw:
        rid = run["run_id"]
        rs = tuple(r for r in reservations if r["run_id"] == rid)
        cs = tuple(r for r in charges if r["run_id"] == rid)
        ls = tuple(r for r in limits if r["run_id"] == rid)
        try:
            if run["owner_id"] == "migration" or run["kind"] == "legacy-import":
                if not _passive(run, rs, cs, ls):
                    raise ValueError("legacy accounting row lacks exact passive settlement contract")
                evidence.append((run, rs, cs, ls))
                continue
            owner = run["owner_id"]
            kind, dispatch_id = run["kind"], run["dispatch_id"]
            if not isinstance(rid, str) or not isinstance(kind, str) or not isinstance(dispatch_id, str):
                raise ValueError("historical run identity fields must be text")
            if not isinstance(owner, str) or len(owner.encode()) > 512:
                raise ValueError("historical owner is malformed")
            parts = owner.split(":", 2)
            if len(parts) < 2:
                raise ValueError("historical owner lacks attributable repository")
            repo, role = parts[:2]
            if canonical_workflow_repository(repo) != repo or not rs or any(r["workload_repository"] != repo
                    or not (r["repository"] == repo or r["repository"] == "*" and r["dimension"] == "global") for r in rs):
                raise ValueError("historical accounting and owner repositories disagree")
            if repo != repository:
                try:
                    foreign = legacy_dispatch_object(dispatch_id)
                except ValueError:
                    continue
                if any(name in foreign and foreign[name] != expected for name, expected in (
                        ("repository", repo), ("role", role), ("kind", run["kind"]))):
                    raise ValueError("foreign dispatch contradicts explicit accounting owner")
                continue
            if role == 'migration' and _migration_canary(run, rs, cs, ls, repository):
                evidence.append((run, rs, cs, ls))
                continue
            if len(parts) != 3 or role not in {"fix", "review", "steward"} or not parts[2]:
                raise ValueError("historical owner lacks attributable role")
            if role != ("steward" if families == (WorkflowFamily.STEWARD,) else "fix"):
                continue
            if repo != repository or role == "review" or role != ("steward" if families == (WorkflowFamily.STEWARD,) else "fix"):
                continue
            attributed = parse_legacy_continuation_run(run_id=rid, owner_id=owner, kind=kind,
                dispatch_id=dispatch_id, workload_repository=repo)
            output.append(attributed)
            evidence.append((run, rs, cs, ls))
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ResolveActivationRefused(f"unassigned prior historical run {rid}: {exc}") from exc
    return LegacyContinuationRunSnapshot(tuple(output), digest(evidence))
