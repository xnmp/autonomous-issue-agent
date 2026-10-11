"""Filesystem, process-table, and git readers for the dashboard."""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from glob import glob
from pathlib import Path

from ...adapters.control_store import read_settled_spend
from ...adapters.event_log import (
    EventLogSegments,
    IncrementalEventLogReader,
    read_event_log,
)
from ...adapters.heartbeat import read_heartbeat_time
from ...adapters.journal import read_events
from ...domain import dashboard as dash
from ...domain.projection import fold


def _system_local_date(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp).date()


def _system_local_midnight(day: date) -> float:
    return datetime.combine(day, time.min).timestamp()


def _local_time_functions(
    local_tz: tzinfo | None,
) -> tuple[Callable[[float], date], Callable[[date], float]]:
    if local_tz is None:
        return _system_local_date, _system_local_midnight
    return (
        lambda timestamp: datetime.fromtimestamp(timestamp, local_tz).date(),
        lambda day: datetime.combine(day, time.min, local_tz).timestamp(),
    )


def deployment_names(repos_dir: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            os.path.splitext(os.path.basename(path))[0]
            for path in glob(os.path.join(repos_dir, "*.env"))
        )
    )


def _environ_agent_env(blob: bytes) -> str:
    for item in blob.split(b"\0"):
        if item.startswith(b"AGENT_ENV="):
            return item[len(b"AGENT_ENV=") :].decode(errors="replace")
    return ""


def deployment_of_env(agent_env: str) -> str:
    """Deployment name from a daemon's AGENT_ENV path, "" when not a daemon.

    Two launch layouts exist: `point-at` composes `config/.active.<name>.env`,
    and the systemd/launchd units (HARNESS.md service install) point at
    `~/.config/issue-agent/<name>.env`. The second arm requires the parent
    directory to be literally `issue-agent` so repo overlays
    (`config/repos/<name>.env`) and unrelated `.env` files never read as live
    daemons. Missing the second arm made every systemd deployment show
    `active: false` on the dashboard (keifu, observed 2026-08-02)."""
    base = os.path.basename(agent_env or "")
    if base.startswith(".active.") and base.endswith(".env"):
        return base[len(".active.") : -len(".env")]
    if (
        base.endswith(".env")
        and not base.startswith(".")
        and os.path.basename(os.path.dirname(agent_env or "")) == "issue-agent"
    ):
        return base[: -len(".env")]
    return ""


def scan_proc_daemons(proc_root: str = "/proc") -> tuple[dict[str, set[str]], bool]:
    """Return live roles by deployment and whether foreign processes were readable."""
    found: dict[str, set[str]] = {}
    read_foreign = False
    own_pid = str(os.getpid())
    try:
        pids = [entry for entry in os.listdir(proc_root) if entry.isdigit()]
    except OSError:
        return {}, False
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "environ"), "rb") as handle:
                environ = handle.read()
        except OSError:
            continue
        if pid != own_pid:
            read_foreign = True
        name = deployment_of_env(_environ_agent_env(environ))
        if not name:
            continue
        roles = found.setdefault(name, set())
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as handle:
                argv = handle.read().decode(errors="replace").split("\0")
        except OSError:
            continue
        roles.update(argument for argument in argv if argument in dash.ROLE_NAMES)
    return found, read_foreign


def collect_snapshot(
    *,
    name: str,
    repo: str,
    target_dir: str,
    journal_paths: list[str],
    log_dir: str,
    ledger_path: str,
    now_fn,
    stale_after_s: float,
    control_db_path: str | None = None,
    local_tz: tzinfo | None = None,
    read_log=None,
    ci_ignore_checks: str = "",
) -> dash.Snapshot:
    read_log = read_log or read_event_log
    now = now_fn()
    to_local_date, local_midnight = _local_time_functions(local_tz)
    settled_today_usd, settled_today_unavailable, spend_warnings = (
        _settled_today_usd(
            control_db_path, repo, now, to_local_date, local_midnight
        )
    )
    typed_paths = tuple(
        os.path.join(os.path.dirname(path), "events-v2.jsonl")
        for path in journal_paths
    )
    results = tuple(read_log(path) for path in typed_paths)
    if not any(result.source_files for result in results) and any(
        os.path.isfile(path) for path in (*journal_paths, ledger_path)
    ):
        legacy_feeds = tuple(
            (
                role,
                path,
                os.path.join(log_dir, f"{role}-{name}.log"),
                tuple(read_events(path)),
            )
            for role, path in zip(dash.ROLE_NAMES, journal_paths)
        )
        deployment = dash.build_deployment(
            name,
            repo,
            legacy_feeds,
            tuple(read_events(ledger_path)),
            now_fn,
            stale_after_s,
            (
                "showing read-only schema-v1 dashboard history",
                *spend_warnings,
            ),
            settled_today_usd,
            settled_today_unavailable,
            to_local_date,
        )
        timestamps = tuple(
            stamp
            for _role, _path, _log, events in legacy_feeds
            for event in events
            if (stamp := dash.parse_ts(event.get("ts"))) is not None
        )
        return dash.Snapshot(
            name, repo, target_dir, deployment, timestamps
        )
    feeds = tuple(
        (
            role,
            path,
            os.path.join(log_dir, f"{role}-{name}.log"),
            result.events,
        )
        for role, path, result in zip(
            dash.ROLE_NAMES, typed_paths, results
        )
    )
    model = fold((
        event
        for _role, _path, _log, events in feeds
        for event in events
    ), ci_ignore_checks=ci_ignore_checks)
    heartbeat_times = {
        role: stamp
        for role, legacy_path in zip(dash.ROLE_NAMES, journal_paths)
        if (
            stamp := read_heartbeat_time(
                os.path.join(os.path.dirname(legacy_path), "heartbeat.json")
            )
        )
        is not None
    }
    malformed = sum(result.malformed_lines for result in results)
    warnings = spend_warnings + (
        (f"skipped {malformed} malformed schema-v2 event line(s)",)
        if malformed
        else ()
    )
    deployment = dash.build_deployment_v2(
        name,
        repo,
        feeds,
        now_fn=now_fn,
        stale_after_s=stale_after_s,
        model=model,
        heartbeat_times=heartbeat_times,
        warnings=warnings,
        settled_today_usd=settled_today_usd,
        settled_today_unavailable=settled_today_unavailable,
        to_local_date=to_local_date,
    )
    timestamps = tuple(
        event.observed_at
        for _role, _path, _log, events in feeds
        for event in events
    )
    return dash.Snapshot(name, repo, target_dir, deployment, timestamps)


def _stat_key(path: str) -> tuple[str, int, int]:
    """Identity of a source file's content: absent files are a state too."""

    try:
        status = os.stat(path)
    except OSError:
        return path, -1, -1
    return path, status.st_mtime_ns, status.st_size


def source_fingerprint(
    *,
    journal_paths: list[str],
    ledger_path: str,
    control_db_path: str | None = None,
) -> tuple[tuple[str, int, int], ...]:
    """Every file a snapshot's payload is derived from, with its (mtime, size).

    Includes the SQLite sidecars: under WAL the database file's own mtime does
    not move when a transaction commits, so fingerprinting `.db` alone would
    miss settled spend.
    """
    paths: list[str] = []
    for legacy in journal_paths:
        state_dir = os.path.dirname(legacy)
        typed = os.path.join(state_dir, "events-v2.jsonl")
        paths.extend(str(segment) for segment in EventLogSegments(Path(typed)).paths)
        paths.append(typed)
        paths.append(legacy)
        paths.append(os.path.join(state_dir, "heartbeat.json"))
    paths.append(ledger_path)
    if control_db_path:
        paths.extend(
            (control_db_path, f"{control_db_path}-wal", f"{control_db_path}-shm")
        )
    seen: dict[str, None] = dict.fromkeys(paths)
    return tuple(_stat_key(path) for path in seen)


def _argument_key(**kwargs) -> tuple:
    """The non-clock arguments a snapshot's payload depends on, comparably."""

    return tuple(
        sorted(
            (
                key,
                tuple(value) if isinstance(value, list) else value,
            )
            for key, value in kwargs.items()
        )
    )


def _valid_until(
    snapshot: dash.Snapshot,
    now: datetime,
    stale_after_s: float,
    local_midnight: Callable[[date], float],
    to_local_date: Callable[[float], date],
) -> datetime:
    """The instant up to which this snapshot stays correct with no file change.

    Every field is derived from the source files except two, and both turn over
    on a schedule the snapshot already states:

    * `DaemonHealth.stale` is false only while the role's last activity is
      within `stale_after_s`, so it flips at `last_ts + stale_after_s`. It can
      never flip back without a new event or heartbeat write, and both move the
      fingerprint — an already-stale role therefore imposes no bound.
    * today's spend is bucketed by the host calendar day, so it can only change
      at the next local midnight.

    Bounding reuse this way is what keeps the short-circuit from freezing the
    liveness indicators: a deployment whose daemons died writes nothing further,
    yet still goes stale on time.

    The read-only schema-v1 fallback is never reused. Its `running` set is a
    rolling 24h window over event timestamps rather than a calendar bucket, so
    it turns over on a schedule this function cannot state from the snapshot.
    It is also the branch the incremental reader does not accelerate, so
    refusing to reuse it costs nothing the incident was about.
    """
    if snapshot.deployment.schema_version != 2:
        return now
    next_day = to_local_date(now.timestamp()) + timedelta(days=1)
    midnight = datetime.fromtimestamp(local_midnight(next_day), timezone.utc)
    deadlines = [midnight]
    for health in snapshot.deployment.health:
        last = health.last_ts
        if health.stale or last is None:
            continue
        if last.tzinfo is None:
            return now  # cannot bound this row's flip; refuse to reuse at all
        deadlines.append(last + timedelta(seconds=stale_after_s))
    return min(deadlines)


class SnapshotCollector:
    """`collect_snapshot` that does no work while the sources are unchanged.

    Two layers, because the two costs are different in kind. The outer layer
    stats the source files and returns the previous `Snapshot` object when
    nothing has changed and the clock cannot have changed anything either. The
    inner layer re-parses only the bytes a feed appended, so even a changed
    deployment pays for the delta rather than for its whole history.

    Collection is serialised per deployment: the dashboard's routes are sync
    `def`, so they run concurrently in a threadpool, and letting two of them
    collect the same deployment at once would both duplicate the expensive work
    and race the incremental reader's per-segment offsets. Per deployment rather
    than globally, so one deployment's slow SQLite read cannot stall every route
    on the server — each deployment gets its own reader and its own lock.
    """

    def __init__(self) -> None:
        self._registry = threading.Lock()  # guards `_readers`/`_locks` only
        self._readers: dict[str, IncrementalEventLogReader] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._cached: dict[str, tuple[tuple, datetime, dash.Snapshot]] = {}

    def _for(self, name: str) -> tuple[threading.Lock, IncrementalEventLogReader]:
        with self._registry:
            if name not in self._locks:
                self._locks[name] = threading.Lock()
                self._readers[name] = IncrementalEventLogReader()
            return self._locks[name], self._readers[name]

    def collect(
        self,
        *,
        name: str,
        journal_paths: list[str],
        ledger_path: str,
        now_fn,
        stale_after_s: float,
        control_db_path: str | None = None,
        local_tz: tzinfo | None = None,
        **kwargs,
    ) -> dash.Snapshot:
        lock, reader = self._for(name)
        with lock:
            now = now_fn()
            to_local_date, local_midnight = _local_time_functions(local_tz)
            # The caller's arguments are half the fingerprint, not just the
            # files: `repo`, `target_dir` and the state-dir paths come from an
            # overlay the operator can edit, and an edited overlay moves no
            # journal byte.
            fingerprint = (
                _argument_key(
                    journal_paths=journal_paths,
                    ledger_path=ledger_path,
                    control_db_path=control_db_path,
                    stale_after_s=stale_after_s,
                    local_tz=local_tz,
                    **kwargs,
                ),
                source_fingerprint(
                    journal_paths=journal_paths,
                    ledger_path=ledger_path,
                    control_db_path=control_db_path,
                ),
            )
            entry = self._cached.get(name)
            if entry is not None and entry[0] == fingerprint and now < entry[1]:
                return entry[2]
            snapshot = collect_snapshot(
                name=name,
                journal_paths=journal_paths,
                ledger_path=ledger_path,
                now_fn=now_fn,
                stale_after_s=stale_after_s,
                control_db_path=control_db_path,
                local_tz=local_tz,
                read_log=reader.read,
                **kwargs,
            )
            self._cached[name] = (
                fingerprint,
                _valid_until(
                    snapshot,
                    now,
                    stale_after_s,
                    local_midnight,
                    to_local_date,
                ),
                snapshot,
            )
            return snapshot


def _settled_today_usd(
    control_db_path,
    repository,
    now,
    to_local_date: Callable[[float], date],
    local_midnight: Callable[[date], float],
):
    if not control_db_path:
        return None, False, ()
    today = to_local_date(now.timestamp())
    start = datetime.fromtimestamp(local_midnight(today), timezone.utc)
    end = datetime.fromtimestamp(
        local_midnight(today + timedelta(days=1)), timezone.utc
    )
    spend = read_settled_spend(
        control_db_path,
        start,
        end,
    )
    amount = spend.for_repository(repository)
    if amount is None:
        return (
            None,
            True,
            (f"settled SQLite spend unavailable: {spend.detail}",),
        )
    return amount.micros / 1_000_000, False, ()


def remote_repo(root: str) -> str:
    """Return the origin as owner/name; an unavailable origin is cosmetic."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode:
        return ""
    url = result.stdout.strip().removesuffix(".git")
    if url.startswith("git@") and ":" in url:
        return url.split(":", 1)[1]
    marker = "github.com/"
    return url.split(marker, 1)[1] if marker in url else ""
