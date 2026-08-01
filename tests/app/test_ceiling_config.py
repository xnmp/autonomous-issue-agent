"""Operator input for the two ADR 0012 ceilings (EFFECT_RETRY_CEILING,
STEWARD_ERROR_CEILING).

Both are bounds on a loop that costs money or blocks a lifecycle write, so an
unusable value must be REPORTED, never silently accepted: 0 does not mean
"disabled" here, it means "restore the unbounded loop the ceiling exists to
stop". Malformed input must not traceback out of config construction either —
`from_env` substitutes the default and records the parse error.
"""

from __future__ import annotations

import pytest

from issue_agent.app.config import from_env
from issue_agent.domain.effect_retry import DEFAULT_EFFECT_RETRY_CEILING
from issue_agent.domain.steward_stall import DEFAULT_STEWARD_ERROR_CEILING


def _errors(env: dict) -> str:
    return "\n".join(from_env({"REPO": "o/r", **env}).validation_errors())


def test_defaults_are_bounded_and_valid():
    cfg = from_env({"REPO": "o/r"})
    assert cfg.effect_retry_ceiling == DEFAULT_EFFECT_RETRY_CEILING
    assert cfg.steward_error_ceiling == DEFAULT_STEWARD_ERROR_CEILING
    assert "CEILING" not in "\n".join(cfg.validation_errors())


def test_operator_value_is_honoured():
    cfg = from_env(
        {"REPO": "o/r", "EFFECT_RETRY_CEILING": "4", "STEWARD_ERROR_CEILING": "2"}
    )
    assert (cfg.effect_retry_ceiling, cfg.steward_error_ceiling) == (4, 2)
    assert "CEILING" not in "\n".join(cfg.validation_errors())


@pytest.mark.parametrize("value", ["0", "-1", "-999999"])
def test_non_positive_ceilings_are_rejected(value):
    assert "EFFECT_RETRY_CEILING must be > 0" in _errors(
        {"EFFECT_RETRY_CEILING": value}
    )
    assert "STEWARD_ERROR_CEILING must be > 0" in _errors(
        {"STEWARD_ERROR_CEILING": value}
    )


@pytest.mark.parametrize("value", ["abc", "3.5", "", " ", "1e9", "ten"])
def test_malformed_ceilings_fall_back_to_the_default_without_raising(value):
    cfg = from_env(
        {
            "REPO": "o/r",
            "EFFECT_RETRY_CEILING": value,
            "STEWARD_ERROR_CEILING": value,
        }
    )
    assert cfg.effect_retry_ceiling == DEFAULT_EFFECT_RETRY_CEILING
    assert cfg.steward_error_ceiling == DEFAULT_STEWARD_ERROR_CEILING
    errors = "\n".join(cfg.validation_errors())
    if value.strip():
        # Empty/whitespace is "unset"; anything else is a typo worth naming.
        assert "EFFECT_RETRY_CEILING must be an integer" in errors
        assert "STEWARD_ERROR_CEILING must be an integer" in errors
    else:
        assert "CEILING" not in errors


def test_huge_ceiling_is_accepted_but_stays_a_bound():
    cfg = from_env({"REPO": "o/r", "EFFECT_RETRY_CEILING": str(10**12)})
    assert cfg.effect_retry_ceiling == 10**12
    assert "CEILING" not in "\n".join(cfg.validation_errors())
