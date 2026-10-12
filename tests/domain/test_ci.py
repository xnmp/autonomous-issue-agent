"""CI-state precedence + distinct-actor review — the watcher-lib lessons."""

from tests.domain.builders import Check, Review

from issue_agent.domain.ci import (
    ci_state,
)
from issue_agent.domain.world import CIState
from issue_agent.domain.checks import Unavailable


def test_no_checks_is_none():
    assert ci_state([]) is CIState.NONE


def test_all_pass_is_ok():
    assert ci_state([Check("build", bucket="pass")]) is CIState.OK


def test_any_fail_is_failure():
    assert ci_state([Check("a", bucket="pass"), Check("b", bucket="fail")]) is CIState.FAILURE




def test_advisory_is_not_inferred_without_a_successful_owning_run():
    assert ci_state([Check("Security Audit", bucket="fail")]) is CIState.FAILURE


def test_pending_beats_failure():
    # THE lesson (#49): a failed check + an in-flight check is NOT yet a failure.
    checks = [Check("unit", bucket="fail"), Check("e2e", state="IN_PROGRESS")]
    assert ci_state(checks) is CIState.PENDING


def test_queued_is_pending():
    assert ci_state([Check("x", state="QUEUED")]) is CIState.PENDING








# ── pending deadline (#30) ───────────────────────────────────────────────────
# A check stuck IN_PROGRESS pins ci_state at PENDING, and PENDING outranks
# FAILURE, so nothing downstream ever acts. These bound the wait without
# touching that precedence.

_T0 = "2026-07-28T12:00:00Z"


def _later(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        + timedelta(seconds=seconds)
    ).isoformat()


# ── #48: an unread checks fetch is UNKNOWN, never an answer ───────────────────
def test_unread_checks_are_unknown_not_none():
    # checks=() from a FAILED `gh pr checks` is not "no checks configured".
    assert ci_state(Unavailable("read failed")) is CIState.UNKNOWN


def test_unread_outranks_everything_even_green():
    # Whatever stale tuple rode along, an unread fetch is not an observation.
    assert ci_state(Unavailable("read failed")) is CIState.UNKNOWN
    assert ci_state(Unavailable("read failed")) is CIState.UNKNOWN


def test_honest_empty_read_stays_none():
    # The no-CI-repo path must not regress: a SUCCESSFUL read of zero checks
    # is still NONE (mergeable — nothing to wait on).
    assert ci_state([]) is CIState.NONE
