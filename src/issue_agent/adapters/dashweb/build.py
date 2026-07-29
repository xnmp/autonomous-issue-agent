"""Locked, bounded publication and collection of dashboard frontend builds."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

BUILD_TIMEOUT_S = 300.0
TIMEOUT_EXIT = 124


def _terminate_process_group(process: subprocess.Popen) -> None:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_process(argv: list[str], cwd: Path, timeout: float) -> int:
    process = subprocess.Popen(argv, cwd=cwd, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        return TIMEOUT_EXIT
    except KeyboardInterrupt:
        _terminate_process_group(process)
        raise


def garbage_collect_builds(dashboard: Path) -> None:
    dist = dashboard / "dist"
    builds = dist / "builds"
    try:
        pointer = json.loads((dist / "current.json").read_text(encoding="utf-8"))
        current = Path(pointer["build"]).name
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return
    if not builds.is_dir():
        return

    leases = dist / ".leases"
    leases.mkdir(parents=True, exist_ok=True)
    for version in builds.iterdir():
        if not version.is_dir() or version.name == current:
            continue
        with (leases / f"{version.name}.lock").open("a+b") as lease:
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            shutil.rmtree(version)


def build_dashboard(harness_root: Path) -> int:
    dashboard = harness_root / "dashboard"
    lock_path = dashboard / ".build.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        deadline = time.monotonic() + BUILD_TIMEOUT_S
        install = run_process(
            ["npm", "ci"], dashboard, max(0.001, deadline - time.monotonic())
        )
        if install != 0:
            return install
        build = run_process(
            ["npm", "run", "build"],
            dashboard,
            max(0.001, deadline - time.monotonic()),
        )
        if build == 0:
            garbage_collect_builds(dashboard)
        return build
