"""domain.effort — the escalation ladder (#16).

Behaviour under test: how much evidence exists, what a notch buys, and that the
ladder can only ever hand back a stance at least as strong as the one it was
given. Nothing here touches a runtime, a store, or GitHub — that wiring is
covered in tests/adapters/test_fix_workflow.py.
"""

import pytest

from issue_agent.domain.effort import Effort, parse_effort


# ── totality on junk ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["HIGH", " high ", "High"])
def test_parse_effort_normalises_case_and_whitespace(raw):
    assert parse_effort(raw) is Effort.HIGH


@pytest.mark.parametrize("raw", ["", "hihg", None, 7, [], "medium-high"])
def test_parse_effort_returns_the_default_on_anything_unrecognised(raw):
    assert parse_effort(raw) is None
    assert parse_effort(raw, Effort.LOW) is Effort.LOW


def test_parse_effort_passes_an_effort_through():
    assert parse_effort(Effort.MEDIUM) is Effort.MEDIUM
