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


# ── gap 22: the base branch's workflows by their latest verdict ──────────────

from issue_agent.domain.checks import BaseRun  # noqa: E402
from issue_agent.domain.ci import base_verdicts  # noqa: E402


def _run(workflow: str, run: int, conclusion: str, at: str | None = None) -> BaseRun:
    stamp = at or f"2026-10-14T00:{run // 60 % 60:02d}:{run % 60:02d}Z"
    return BaseRun(workflow, run, f"https://x/runs/{run}", f"sha{run}", conclusion, stamp, stamp)


def test_the_latest_verdict_decides_and_its_streak_runs_back_to_the_last_other_verdict():
    runs = [_run("a", 1, "failure"), _run("a", 2, "success"), _run("a", 3, "failure"), _run("a", 4, "cancelled"),
            _run("a", 5, "timed_out"), _run("a", 6, "skipped")]
    ((latest, since, bounded),) = base_verdicts(reversed(runs))  # the listing's order does not matter
    assert (latest.run_id, since.run_id, bounded) == (5, 3, True)


def test_a_streak_that_reaches_the_earliest_listed_run_is_unbounded():
    ((_, since, bounded),) = base_verdicts([_run("a", 1, "failure"), _run("a", 2, "cancelled"), _run("a", 3, "failure")])
    assert (since.run_id, bounded) == (1, False)


def test_a_green_latest_verdict_is_green_and_cancelled_or_skipped_runs_count_neither_way():
    verdicts = base_verdicts([_run("a", 1, "failure"), _run("a", 2, "neutral"), _run("a", 3, "cancelled"),
                              _run("b", 4, "skipped"), _run("b", 5, "action_required"), _run("b", 6, "unknown")])
    assert [(latest.workflow, latest.conclusion, since.run_id) for latest, since, _ in verdicts] == [("a", "neutral", 2)]


def test_creation_time_orders_runs_before_the_run_id():
    # A run created later is newer even with a smaller id; a re-run keeps its run.
    ((latest, _, _),) = base_verdicts([_run("a", 9, "failure", "2026-10-14T00:00:00Z"),
                                    _run("a", 2, "success", "2026-10-14T01:00:00Z")])
    assert latest.run_id == 2


def test_a_huge_run_list_reduces_to_one_verdict_per_workflow():
    same = "2026-10-14T00:00:00Z"  # equal times: the run id orders them
    runs = [_run(f"w{w}", w * 100_000 + n, "failure" if n % 3 else "success", same)
            for w in range(1, 21) for n in range(1, 2001)]
    verdicts = base_verdicts(runs)
    assert len(verdicts) == 20
    assert all(latest.run_id % 100_000 == 2000 and since.run_id % 100_000 == 1999 for latest, since, _ in verdicts)


def test_no_runs_is_no_verdicts():
    assert base_verdicts([]) == ()


@pytest.mark.parametrize("bad", [dict(run_id=0), dict(run_id=True), dict(url=""), dict(conclusion=None),
                                 dict(workflow="")])
def test_a_malformed_base_run_is_refused(bad):
    fields = dict(workflow="wf", run_id=2, url="u", head_sha="s", conclusion="failure",
                  created_at="2026-10-14T00:00:00Z", completed_at="2026-10-14T00:10:00Z")
    with pytest.raises(ValueError):
        BaseRun(**{**fields, **bad})
