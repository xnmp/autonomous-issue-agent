"""CI-state precedence — the watcher-lib lessons."""

import pytest

from issue_agent.domain.checks import Check, Observed, Unavailable
from issue_agent.domain.ci import Clear, Executing, Failed, Queued, Waiting, reduce_ci


def _observed(*checks: Check) -> Observed:
    return Observed("h", "h", frozenset(), checks)


@pytest.mark.parametrize(
    "checks,expected",
    [
        ((), Clear),  # an honest empty read: nothing to wait on
        ((Check("build", bucket="pass"),), Clear),
        ((Check("a", bucket="pass"), Check("b", bucket="fail")), Failed),
        ((Check("Security Audit", bucket="fail"),), Failed),  # advisory is never inferred
    ],
)
def test_settled_checks_reduce_to_clear_or_failed(checks, expected):
    assert type(reduce_ci(_observed(*checks))) is expected


def test_pending_beats_failure():
    # THE lesson (#49): a failed check + an in-flight check is NOT yet a failure.
    result = reduce_ci(_observed(Check("unit", bucket="fail"), Check("e2e", state="IN_PROGRESS")))
    assert isinstance(result, Waiting) and isinstance(result.reason, Executing)
    queued = reduce_ci(_observed(Check("x", state="QUEUED")))
    assert isinstance(queued, Waiting) and isinstance(queued.reason, Queued)


def test_unread_checks_are_unknown_not_none():
    # #48: a FAILED `gh pr checks` is not "no checks configured".
    assert isinstance(reduce_ci(Unavailable("read failed")), Unavailable)
