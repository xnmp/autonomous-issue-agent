"""Filesystem, process-table, and git readers for the dashboard."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from glob import glob

from ...adapters.control_store import read_settled_spend
from ...adapters.heartbeat import read_heartbeat_time
from ...adapters.journal import read_events, read_events_tail
from ...domain import dashboard as dash


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
) -> dash.Snapshot:
    now = now_fn()
    to_local_date, local_midnight = _local_time_functions(local_tz)
    settled_today_usd, settled_today_unavailable, spend_warnings = (
        _settled_today_usd(
            control_db_path, repo, now, to_local_date, local_midnight
        )
    )
    feeds = tuple(
        (
            role,
            path,
            os.path.join(log_dir, f"{role}-{name}.log"),
            tuple(read_events_tail(path)),
        )
        for role, path in zip(dash.ROLE_NAMES, journal_paths)
    )
    deployment = dash.build_deployment(
        name,
        repo,
        feeds,
        tuple(read_events(ledger_path)),
        now_fn,
        stale_after_s,
        spend_warnings,
        settled_today_usd,
        settled_today_unavailable,
        to_local_date,
        heartbeats={
            role: read_heartbeat_time(
                os.path.join(os.path.dirname(path), "heartbeat.json")
            )
            for role, path in zip(dash.ROLE_NAMES, tuple(journal_paths))
        },
    )
    timestamps = tuple(
        stamp
        for _role, _path, _log, events in feeds
        for event in events
        if (stamp := dash.parse_ts(event.get("ts"))) is not None
    )
    return dash.Snapshot(name, repo, target_dir, deployment, timestamps)


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
