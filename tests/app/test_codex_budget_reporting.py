"""Truthful startup reporting for codex budget enforcement (#29)."""

from __future__ import annotations

import pytest

from issue_agent.app.config import from_env


def _describe(**env: str) -> str:
    return from_env(
        {"REPO": "o/r", "FIXER_RUNTIME": "codex", "MAX_BUDGET_USD": "20", "AGENT_MAX_RUNTIME_SECONDS": "3600", **env}
    ).describe()


def test_default_priced_config_reports_harness_enforcement():
    description = _describe()
    assert "fix_budget=$20.00" in description
    assert "harness-enforced on codex" in description


@pytest.mark.parametrize(
    "env",
    [{"CODEX_COST_PER_MTOK_IN": "0"}, {"CODEX_COST_PER_MTOK_OUT": "0"}, {"CODEX_COST_PER_MTOK": "gpt-5.6-terra=2/0"}],
)
def test_incomplete_pricing_reports_unmeasurable_budget(env):
    description = _describe(**env)
    assert "NOT ENFORCED on codex" in description
