"""An issue's shown cost is settled money; a running leg's reservation is a
worst-case hold and is reported apart, never summed into the cost."""

from datetime import UTC, datetime

import pytest

from issue_agent.app.dashweb.contracts import IssueDetailPayload
from issue_agent.app.dashweb.views import issue_detail
from issue_agent.domain import dashboard as dash

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _leg(kind, state, *, reserved=0.0, charged=None, basis="provider_reported", n=1):
    running = state in ("reserved", "running")
    return dash.LegRow(
        leg_id=f"{kind}-{n}", role="fix", kind=kind, issue=7, number=0, head="h", runtime="claude",
        state=state, reserved_at=T0, started_at=T0 if running else None, ended_at=None if running else T0,
        reserved_usd=reserved, charged_usd=charged, basis="" if running else basis,
    )


def _detail(*legs):
    return issue_detail(7, "o/r", None, legs, [], None)


def test_running_leg_is_held_not_spent():
    d = _detail(_leg("STEWARD", "ended", reserved=1.0, charged=0.05), _leg("RESOLVE", "running", reserved=50.0, n=2))
    assert (d["cost_usd"], d["held_usd"], d["estimated"]) == (0.05, 50.0, False)


def test_no_running_leg_holds_nothing():
    d = _detail(_leg("STEWARD", "ended", reserved=1.0, charged=0.05))
    assert (d["cost_usd"], d["held_usd"]) == (0.05, 0.0)


def test_settled_estimated_basis_marks_cost_estimated():
    d = _detail(_leg("STEWARD", "ended", charged=0.05, basis="estimate"), _leg("RESOLVE", "running", reserved=5.0, n=2))
    assert d["estimated"] is True and d["held_usd"] == 5.0


def test_running_leg_with_zero_reservation_holds_zero():
    d = _detail(_leg("RESOLVE", "running", reserved=0.0))
    assert (d["cost_usd"], d["held_usd"], d["estimated"]) == (0.0, 0.0, False)


def test_released_leg_is_neither_spent_nor_held():
    d = _detail(_leg("RESOLVE", "released", reserved=50.0))
    assert (d["cost_usd"], d["held_usd"]) == (0.0, 0.0)


def test_payload_validates_against_contract():
    payload = {**_detail(_leg("RESOLVE", "running", reserved=50.0)), "warnings": [], "observed_at": "2026-01-01T00:00:00Z"}
    assert IssueDetailPayload.model_validate(payload).held_usd == 50.0
