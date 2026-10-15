"""Operator entrypoint templates use the Python runtime by default.

T5 (review): these used to grep the WHOLE file's raw text for a substring — a
match anywhere (a comment, an unrelated target/section) would pass just as
happily as the real thing, and the check couldn't tell "the fix unit's
ExecStart runs python" from "the word appears somewhere in this file". Each
template format has a real parser (`configparser`-shaped for the systemd
units, `plistlib` for the launchd plists, a small target→recipe split for the
Makefile); assertions run against
the PARSED structure — the actual `ExecStart`, `ProgramArguments`, `exec`
argv — instead of the raw text.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


# ── systemd units: parse [Section]/key=value, don't grep the raw text ────────
def _parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """Minimal systemd-unit-file reader: `[Section]` headers + `key=value`
    lines, values collected as a LIST per key — systemd's own semantics (a
    repeated key, e.g. two `Environment=` lines, APPENDS an entry rather than
    overwriting), which a `configparser` read would collapse to the last one."""
    sections: dict[str, dict[str, list[str]]] = {}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections.setdefault(section, {})
            continue
        if section is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        sections[section].setdefault(key.strip(), []).append(value.strip())
    return sections


def test_systemd_units_run_python_from_harness_root_with_target_dir_env():
    units = {
        "ops/systemd/issue-agent-fix-watcher.service": "fix",
        "ops/systemd/issue-agent-review-watcher.service": "review",
    }

    for path, role in units.items():
        service = _parse_unit(_read(path))["Service"]
        exec_start = service["ExecStart"][0]
        assert exec_start.split()[0] == "@UV_BIN@"
        assert exec_start.split()[-3:] == ["-m", "issue_agent.app.main", role]
        assert service["WorkingDirectory"] == ["@HARNESS_ROOT@"]
        assert "TARGET_DIR=@TARGET_DIR@" in service["Environment"]
        # Daemons must resolve the operator's toolchain (codex under nvm,
        # bunx, rustup shims): the installer substitutes its own PATH — the
        # 2026-08-10 incident where systemd's minimal default PATH killed
        # every resolve leg at spawn (#650).
        assert "PATH=@INSTALL_PATH@" in service["Environment"]
        assert service["Restart"] == ["always"]
        assert service["RestartPreventExitStatus"] == ["2 3 4 5 6"]
        # Hot-reload exit (87) is a designed restart, not a crash: it must log
        # clean AND still restart, and must not march the start-limit counter
        # toward a mid-batch permanent kill (observed 2026-08-10: one commit's
        # reload marked four units failed at once under Burst=3).
        assert service["SuccessExitStatus"] == ["87"]
        assert service["RestartForceExitStatus"] == ["87"]
        expected_tasks = "4096" if role in {"fix", "review"} else "512"
        assert service["TasksMax"] == [expected_tasks]
        assert service["MemoryMax"] == ["4G"]
        unit = _parse_unit(_read(path))["Unit"]
        assert unit["StartLimitIntervalSec"] == ["300"]
        assert unit["StartLimitBurst"] == ["10"]
        assert not any("watcher.sh" in v for v in exec_start.split())


def test_systemd_dashboard_is_a_hot_reloading_host_singleton():
    unit = _parse_unit(_read("ops/systemd/issue-agent-dashboard.service"))
    service = unit["Service"]
    exec_start = service["ExecStart"][0].split()

    assert exec_start[0] == "@UV_BIN@"
    assert exec_start[-3:] == ["python", "-m", "issue_agent.app.dashweb.main"]
    assert service["WorkingDirectory"] == ["@HARNESS_ROOT@"]
    assert service["Environment"] == ["PATH=@INSTALL_PATH@"]
    assert service["Restart"] == ["always"]
    assert service["RestartPreventExitStatus"] == ["2 3 4 5 6"]
    assert service["SuccessExitStatus"] == ["87"]
    assert service["RestartForceExitStatus"] == ["87"]
    assert unit["Unit"]["StartLimitIntervalSec"] == ["300"]
    assert unit["Unit"]["StartLimitBurst"] == ["10"]


# ── launchd plists: plistlib gives a real ProgramArguments list ──────────────
def test_launchd_plists_run_python_from_harness_root_with_target_dir_env():
    plists = {
        "ops/launchd/dev.issue-agent.fix-watcher.plist": "fix",
        "ops/launchd/dev.issue-agent.review-watcher.plist": "review",
    }

    for path, role in plists.items():
        data = plistlib.loads(_read(path).encode())
        argv = data["ProgramArguments"]
        assert argv[0] == "@UV_BIN@"
        assert argv[-4:] == ["python", "-m", "issue_agent.app.launchd_main", role]
        assert data["WorkingDirectory"] == "@HARNESS_ROOT@"
        assert data["EnvironmentVariables"]["TARGET_DIR"] == "@TARGET_DIR@"
        # The launchd wrapper requests restart for clean daemon exits and reserves
        # success for permanent failures, which launchd must leave stopped.
        assert data["KeepAlive"] == {"SuccessfulExit": False}
        assert not any("watcher.sh" in arg for arg in argv)
