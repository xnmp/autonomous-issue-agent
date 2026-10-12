from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.outcome import ExitReason
from issue_agent.domain.resolve_return import PendingResolveReturn, ResolveInputs, ReturnedResolve, UnavailableResolveReturn
from issue_agent.domain.workflow import ResolveSession, ResolveStage
from tests.fakes.workflow_authority import resolve_snapshot


def returned():
    attempt = WorkflowAttemptIdentity(resolve_snapshot(7).binding, RunId("paid-run"), "paid-leg")
    return ReturnedResolve(
        attempt, ResolveStage.RESUME, ResolveInputs("title", "body", "author"),
        False, "blocked", "summary", "question", "success", ExitReason.OK,
        ResolveSession(attempt, "session", "claude", "model"), datetime.now(timezone.utc),
    )


def test_raw_return_retains_paid_identity_and_question_without_classifying():
    receipt = returned()
    result = receipt.result()
    assert result.workflow_attempt == receipt.attempt
    assert result.session_id == "session"
    assert result.status == "blocked"
    assert result.needs == "question"


@pytest.mark.parametrize("field,value", [
    ("is_error", None), ("is_error", 1), ("stage", "RESOLVE"),
    ("stage", ResolveStage.ROUTE), ("inputs", None),
    ("exit_reason", "success"), ("returned_at", datetime(2026, 9, 23)),
    ("summary", "x" * 65537), ("needs", "bad\x00text"),
], ids=["null-error", "integer-error", "raw-stage", "route-stage", "null-inputs", "raw-exit", "naive-time", "huge-summary", "nul-needs"])
def test_malformed_return_cannot_become_recovery_authority(field, value):
    with pytest.raises(ValueError):
        replace(returned(), **{field: value})


def test_receipt_cannot_adopt_another_runs_session():
    receipt = returned()
    other = replace(receipt.attempt, run_id=RunId("other-run"))
    with pytest.raises(ValueError):
        replace(receipt, session=ResolveSession(other, "session", "claude", "model"))


@pytest.mark.parametrize("count", [True, -1, 2**63, "1"])
def test_pending_read_counter_is_bounded_integer(count):
    receipt = returned()
    with pytest.raises(ValueError):
        PendingResolveReturn(receipt, receipt.returned_at, count)


def test_pending_observation_cannot_precede_paid_return():
    receipt = returned()
    with pytest.raises(ValueError):
        PendingResolveReturn(receipt, receipt.returned_at - timedelta(seconds=1))


@pytest.mark.parametrize("changes", [
    {"record_key": ""}, {"record_key": None}, {"reason": ""},
    {"reason": "x" * 16385}, {"next_read_at": datetime(2026, 9, 23)},
])
def test_unavailable_return_read_requires_actionable_bounded_diagnostics(changes):
    values = {"record_key": "paid-run", "reason": "missing observation", "next_read_at": datetime.now(timezone.utc)}
    values.update(changes)
    with pytest.raises(ValueError):
        UnavailableResolveReturn(**values)
