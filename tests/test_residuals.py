"""Executable registry of accepted security residuals.

Every test states the desired closed behavior and is a strict expected failure
while the matching entry remains accepted in ``docs/open/residuals.md``. A fix
therefore becomes an XPASS and fails the suite until code and registry move
together.
"""

from __future__ import annotations

import pytest

from issue_agent.adapters.child_env import child_process_env
from issue_agent.adapters.store import HarnessMarkers
from issue_agent.domain.gate import resolve_test_cmd
from issue_agent.domain.governor_policy import find_violations


@pytest.mark.xfail(
    strict=True,
    reason="CRED-HOME: the paid child still inherits the operator HOME",
)
def test_paid_child_cannot_reach_operator_home_credentials() -> None:
    selected = child_process_env({"HOME": "/operator/home", "PATH": "/bin"})

    assert selected.get("HOME") != "/operator/home"


@pytest.mark.xfail(
    strict=True,
    reason="GOV-SCRIPT-FILE: script-file execution remains ungoverned",
)
@pytest.mark.parametrize("command", ("bash ./bypass.sh", "./bypass.sh"))
def test_script_file_execution_is_denied(command: str) -> None:
    assert find_violations(command)


@pytest.mark.xfail(
    strict=True,
    reason="GOV-SCRIPT-C: util-linux script -c remains ungoverned",
)
def test_script_command_wrapper_is_denied() -> None:
    assert find_violations(
        "script -q /dev/null -c 'git push --force origin main'"
    )


@pytest.mark.xfail(
    strict=True,
    reason="GOV-INTERPRETER-E: interpreter -e forms remain ungoverned",
)
@pytest.mark.parametrize(
    "command",
    (
        "node -e 'require(\"child_process\").execSync(\"git push --force\")'",
        "perl -e 'system(\"git push --force\")'",
        "ruby -e 'system(\"git push --force\")'",
    ),
)
def test_dynamic_interpreter_eval_is_denied(command: str) -> None:
    assert find_violations(command)


@pytest.mark.xfail(
    strict=True,
    reason="GOV-REBASE-EXEC: git rebase --exec remains ungoverned",
)
def test_git_rebase_exec_is_denied() -> None:
    assert find_violations(
        "git rebase --exec 'git push --force origin main' dev"
    )


@pytest.mark.xfail(
    strict=True,
    reason="CLAIM-TEST-CMD: agent command selection remains the no-operator fallback",
)
def test_agent_marker_cannot_select_the_gate_test_command() -> None:
    assert resolve_test_cmd("true", None) != "true"
