"""Executable registry of accepted security residuals.

Every test states the desired closed behavior and is a strict expected failure
while the matching entry remains accepted in ``docs/open/residuals.md``. A fix
therefore becomes an XPASS and fails the suite until code and registry move
together.
"""

from __future__ import annotations

import pytest

from issue_agent.adapters.child_env import child_process_env


@pytest.mark.xfail(
    strict=True,
    reason="CRED-HOME: the paid child still inherits the operator HOME",
)
def test_paid_child_cannot_reach_operator_home_credentials() -> None:
    selected = child_process_env({"HOME": "/operator/home", "PATH": "/bin"})

    assert selected.get("HOME") != "/operator/home"
