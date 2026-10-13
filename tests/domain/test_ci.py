"""CI-state precedence — the watcher-lib lessons."""

import pytest

from issue_agent.domain.checks import Check, Observed, Unavailable
from issue_agent.domain.ci import BaseRed, Clear, Executing, Failed, Queued, Waiting, charge, reduce_ci


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


# ── #313: a red base is not the PR's failure ────────────────────────────────


def _red(*names: str, kind: str = "check", app_id: int | None = 1) -> Failed:
    return Failed(tuple(Check(n, "FAILURE", "fail", required=True, app_id=app_id, kind=kind) for n in names))


def _on_base(name: str, bucket: str, *, state: str = "", kind: str = "check", app_id: int | None = 1) -> Check:
    return Check(name, state or {"fail": "FAILURE", "pass": "SUCCESS", "pending": "IN_PROGRESS"}[bucket], bucket,
                 app_id=app_id, kind=kind)


def test_313_a_check_red_on_the_head_and_the_base_tip_is_the_bases():
    assert charge(_red("frontend"), (_on_base("frontend", "fail"), _on_base("rust", "pass"))) == BaseRed()
    assert isinstance(charge(_red("ci/x", kind="status"), (_on_base("ci/x", "fail", state="ERROR", kind="status"),)),
                      BaseRed)


def test_313_a_check_red_only_on_the_head_is_the_prs():
    failed = _red("frontend")
    assert charge(failed, (_on_base("frontend", "pass"),)) is failed
    assert charge(failed, (_on_base("rust", "fail"),)) is failed  # the base runs no such check
    assert charge(failed, ()) is failed


def test_313_one_failure_beyond_the_bases_owes_the_pr_a_fix():
    failed = _red("frontend", "rust")
    assert charge(failed, (_on_base("frontend", "fail"), _on_base("rust", "pass"))) is failed


def test_313_an_unread_base_charges_the_pr():
    failed = _red("frontend")
    assert charge(failed, None) is failed


def test_313_a_base_check_still_running_is_waited_for_not_charged():
    # Pending outranks failure (#49): a base rerun may still go red or green.
    running = _on_base("frontend", "pending")
    result = charge(_red("frontend"), (_on_base("frontend", "fail"), running))
    assert result == Waiting(Executing((running,)))


@pytest.mark.parametrize("state", ["QUEUED", "WAITING", "PENDING", "EXPECTED"])
def test_313_a_base_twin_that_may_never_start_charges_the_pr(state):
    # Only IN_PROGRESS is bounded (a job timeout); a queued or gated job can sit forever.
    failed = _red("frontend")
    assert charge(failed, (_on_base("frontend", "pending", state=state),)) is failed


@pytest.mark.parametrize("conclusion", ["CANCELLED", "TIMED_OUT", "STALE", "ACTION_REQUIRED", "STARTUP_FAILURE"])
def test_313_a_base_twin_that_did_not_plainly_fail_charges_the_pr(conclusion):
    # Nothing re-reads a cancelled or timed-out base run: waiting on it would never end.
    failed = _red("frontend")
    assert charge(failed, (_on_base("frontend", "fail", state=conclusion),)) is failed


def test_313_any_green_base_report_of_the_same_check_keeps_the_failure_the_prs():
    # A green rerun, or a same-named job in another workflow: not settled red.
    failed = _red("changes")
    assert charge(failed, (_on_base("changes", "fail"), _on_base("changes", "pass"))) is failed
    assert charge(failed, (_on_base("changes", "pending"), _on_base("changes", "pass"))) is failed


def test_313_a_check_run_matches_only_its_own_app_and_kind():
    failed = _red("build", app_id=1)
    assert charge(failed, (_on_base("build", "fail", app_id=2),)) is failed
    assert charge(failed, (_on_base("build", "fail", kind="status", app_id=None),)) is failed
    # A required status may carry the policy's app id; the base's status has none.
    status = _red("ci/jenkins", kind="status", app_id=7)
    assert isinstance(charge(status, (_on_base("ci/jenkins", "fail", kind="status", app_id=None),)), BaseRed)


def test_313_an_unknown_base_bucket_keeps_the_failure_the_prs():
    assert charge(_red("x"), (Check("x", "WEIRD", "", app_id=1),)).__class__ is Failed


def test_313_huge_check_lists_attribute_by_identity():
    base = tuple(_on_base(f"job {i}", "fail" if i % 2 else "pass") for i in range(5000))
    odd = _red(*(f"job {i}" for i in range(1, 5000, 2)))
    assert isinstance(charge(odd, base), BaseRed)
    assert charge(_red("job 1", "job 4998"), base).__class__ is Failed
