"""Cache-aware Codex pricing from configuration to the rate card (#89).

The rate card built here is the one the live budget kill, settlement and
recovery all price with, so these assertions cover every money path.
"""

import pytest

from issue_agent.app.config import from_env
from issue_agent.app.config import rate_table as _rate_table
from issue_agent.domain import pricing
from issue_agent.domain.contracts import TokenUsage
from issue_agent.domain.pricing import price


def _cost(cfg, model, usage: TokenUsage) -> int:
    table = _rate_table(cfg, "codex")
    assert table is not None
    return price(usage, table.rates_for(model)).micros


def test_model_cache_rate_prices_the_issue_71_usage_under_budget():
    cfg = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "CODEX_COST_PER_MTOK": "gpt-5.6-sol=5/30/0.5"})
    # 3,974,000 inclusive input of which 3,773,000 cached, 9,500 out.
    usage = TokenUsage(201_000, 9_500, cache_read_tokens=3_773_000)
    assert _cost(cfg, "gpt-5.6-sol", usage) == 3_176_500


@pytest.mark.parametrize(
    "configured,expected", [("gpt-5.6-sol=5/30", (10.0, 50.0, 1.0)), ("gpt-6-astra=12/60/2", (12.0, 60.0, 2.0))]
)
def test_astra_uses_its_shipped_rates_unless_the_operator_names_it(configured, expected):
    cfg = from_env({"CODEX_COST_PER_MTOK": configured})
    charge = _cost(cfg, "gpt-6-astra", TokenUsage(1_000_000, 1_000_000, cache_read_tokens=1_000_000))
    assert charge == round(sum(expected) * 1_000_000)


def test_rate_card_version_tracks_shipped_prices_without_an_overlay_edit(monkeypatch):
    cfg = from_env({"CODEX_COST_PER_MTOK": "gpt-5.6-sol=5/30"})
    current = _rate_table(cfg, "codex")
    monkeypatch.setattr(pricing, "CODEX_DEFAULT_MODEL_RATES", (("gpt-6-astra", 12.0, 60.0),))
    changed = _rate_table(cfg, "codex")

    assert current is not None and changed is not None
    assert current.rates_for("gpt-6-astra") != changed.rates_for("gpt-6-astra")
    assert current.version != changed.version
    assert current.rate_card_id == changed.rate_card_id


@pytest.mark.parametrize(
    "raw",
    [
        "gpt-5.6-sol=5/30/0",  # cache reads would price free
        "gpt-5.6-sol=5/30/-1",  # and negative would pay the leg back
        "gpt-5.6-sol=5/30/abc",  # non-numeric third component
        "gpt-5.6-sol=5//30",  # empty middle: OUT must not become CACHED_IN
        "gpt-5.6-sol=5/30/1/2",  # four components
    ],
)
def test_a_non_positive_or_malformed_cache_rate_never_reaches_the_card(raw):
    cfg = from_env({"REPO": "x", "FIX_BOT": "fix-bot", "CODEX_COST_PER_MTOK": raw})
    assert cfg.codex_cost_per_mtok == ()
    assert cfg.codex_cost_per_mtok_cached_in == ()
    assert any(raw in e for e in cfg.validation_errors()), cfg.validation_errors()


@pytest.mark.parametrize("model,expected", [("gpt-6.1-sol", (2.0, 10.0, 0.1)), ("gpt-6-luna", (0.1, 0.5, 0.01))])
def test_shipped_model_rates_apply_beside_configured_ones(model, expected):
    cfg = from_env({"CODEX_COST_PER_MTOK": "gpt-5.6-sol=5/30/0.5"})
    charge = _cost(cfg, model, TokenUsage(1_000_000, 1_000_000, cache_read_tokens=1_000_000))
    assert charge == round(sum(expected) * 1_000_000)


def test_sol_operator_prices_override_shipped_defaults():
    cfg = from_env({"CODEX_COST_PER_MTOK": "gpt-6.1-sol=3/12/0.25"})
    table = _rate_table(cfg, "codex")
    assert table is not None
    assert table.rates_for("gpt-6.1-sol").cache_read_micros == 250_000


def test_sol_two_component_operator_rate_uses_its_model_cache_discount():
    cfg = from_env({"CODEX_COST_PER_MTOK": "gpt-6.1-sol=2/10"})
    assert cfg.codex_cost_per_mtok_cached_in == (("gpt-6.1-sol", 0.1),)
    table = _rate_table(cfg, "codex")
    assert table is not None
    assert table.rates_for("gpt-6.1-sol").cache_read_micros == 100_000
