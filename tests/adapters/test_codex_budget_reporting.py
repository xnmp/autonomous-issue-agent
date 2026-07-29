"""Truthful startup reporting for codex budget enforcement (#29)."""

from __future__ import annotations

import pytest

from issue_agent.adapters.codex_runner import codex_budget_not_enforced_warning
from issue_agent.app.config import from_env


def _describe(**env: str) -> str:
    return from_env({
        "REPO": "o/r",
        "FIXER_RUNTIME": "codex",
        "MAX_BUDGET_USD": "20",
        "AGENT_MAX_RUNTIME_SECONDS": "3600",
        **env,
    }).describe()


def test_default_priced_config_reports_harness_enforcement():
    description = _describe()
    assert "fix_budget=$20.00" in description
    assert "harness-enforced on codex" in description


def test_disabled_enforcement_reports_wall_only_bound():
    description = _describe(CODEX_ENFORCE_BUDGET="0")
    assert "CODEX_ENFORCE_BUDGET=0" in description
    assert "wall cap 3600s is the only per-run bound" in description


@pytest.mark.parametrize(
    "env",
    [
        {"CODEX_COST_PER_MTOK_IN": "0"},
        {"CODEX_COST_PER_MTOK_OUT": "0"},
        {"CODEX_COST_PER_MTOK": "gpt-5.6-terra=2/0"},
    ],
)
def test_incomplete_pricing_reports_unmeasurable_budget(env):
    description = _describe(**env)
    assert "dollar threshold is not measurable" in description
    assert "wall cap 3600s is the only per-run bound" in description


def test_budget_warning_is_explicit_when_enforcement_is_disabled():
    warning = codex_budget_not_enforced_warning(
        fix_budget_usd=20.0,
        max_runtime=3600.0,
        enforced=False,
        priced=True,
    )
    assert warning is not None
    assert "CODEX_ENFORCE_BUDGET=0" in warning
    assert "AGENT_MAX_RUNTIME_SECONDS=3600s" in warning


def test_budget_notice_names_active_harness_enforcement():
    notice = codex_budget_not_enforced_warning(
        fix_budget_usd=20.0,
        max_runtime=3600.0,
        enforced=True,
        priced=True,
    )
    assert notice is not None
    assert "harness enforcement ACTIVE" in notice
    assert "MAX_BUDGET_USD=$20.00" in notice
