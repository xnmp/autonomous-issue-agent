"""Runtime tests ignore the invoking operator's credentials and rollouts."""

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys


def test_runner_suite_ignores_populated_operator_home(tmp_path):
    operator_home = tmp_path / "operator"
    codex_home = operator_home / ".codex"
    directory = codex_home / "sessions" / datetime.now(timezone.utc).strftime("%Y/%m/%d")
    directory.mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"operator":"private"}')
    for i in range(2000):
        (directory / f"rollout-now-{i}.jsonl").touch()
    # Even the fixed scanner fails closed on an oversized metadata header if
    # the runtime test escapes HOME isolation.
    (directory / "rollout-now-poison.jsonl").write_text("x" * 65537)
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/app/test_codex_runner.py::test_run_retries_after_rate_limit_then_succeeds",
         "tests/adapters/test_service_installers.py::test_service_installers_reject_noncanonical_deployment_identity[True]",
         "tests/adapters/test_dashboard_bundle.py::test_parent_termination_is_forwarded_to_the_nested_process_group",
         "--basetemp", str(tmp_path / "nested-pytest")],
        cwd=repo,
        env={**os.environ, "HOME": str(operator_home), "CODEX_HOME": str(codex_home)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (codex_home / "auth.json").read_text() == '{"operator":"private"}'
