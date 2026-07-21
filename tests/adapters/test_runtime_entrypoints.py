"""Operator entrypoint templates use Python runtime by default."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_makefile_defaults_to_python_watchers_and_keeps_compat_shell_targets():
    makefile = _read("Makefile")

    assert "run-fix:" in makefile
    assert "python -m issue_agent.app.main fix" in makefile
    assert "python -m issue_agent.app.main review" in makefile
    assert "python -m issue_agent.app.main steward" in makefile
    assert "run-fix-sh:" in makefile and "bash bin/run-role.sh fix" in makefile
    assert "run-review-sh:" in makefile and "bash bin/run-role.sh review" in makefile
    assert "run-steward-sh:" in makefile and "bash bin/run-role.sh steward" in makefile
    assert "bash agent/watcher.sh" not in makefile
    assert "bash review/review-watcher.sh" not in makefile
    assert "bash steward/steward.sh" not in makefile


def test_shell_compat_wrapper_delegates_to_python_runtime():
    text = _read("bin/run-role.sh")

    assert "python -m issue_agent.app.main" in text
    assert "agent/watcher.sh" not in text
    assert "review/review-watcher.sh" not in text
    assert "steward/steward.sh" not in text


def test_systemd_units_run_python_from_harness_root_with_target_dir_env():
    units = {
        "ops/systemd/issue-agent-fix-watcher.service": "fix",
        "ops/systemd/issue-agent-review-watcher.service": "review",
        "ops/systemd/issue-agent-steward.service": "steward",
    }

    for path, role in units.items():
        text = _read(path)
        assert f"python -m issue_agent.app.main {role}" in text
        assert "WorkingDirectory=@HARNESS_ROOT@" in text
        assert "Environment=TARGET_DIR=@TARGET_DIR@" in text
        assert "/agent/watcher.sh" not in text
        assert "/review/review-watcher.sh" not in text
        assert "/steward/steward.sh" not in text


def test_launchd_plists_run_python_from_harness_root_with_target_dir_env():
    plists = {
        "ops/launchd/dev.issue-agent.fix-watcher.plist": "fix",
        "ops/launchd/dev.issue-agent.review-watcher.plist": "review",
        "ops/launchd/dev.issue-agent.steward.plist": "steward",
    }

    for path, role in plists.items():
        text = _read(path)
        assert "<string>uv</string>" in text
        assert "<string>issue_agent.app.main</string>" in text
        assert f"<string>{role}</string>" in text
        assert "<key>TARGET_DIR</key><string>@TARGET_DIR@</string>" in text
        assert "<key>WorkingDirectory</key><string>@HARNESS_ROOT@</string>" in text
        assert "/agent/watcher.sh" not in text
        assert "/review/review-watcher.sh" not in text
        assert "/steward/steward.sh" not in text
