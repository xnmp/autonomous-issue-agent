"""Repository policy owns the CI gate and paid repair eligibility (#303)."""

from dataclasses import replace

import pytest

from issue_agent.domain.ci import (
    Clear, Executing, Failed, MissingRequired, Queued, Waiting, reduce_ci,
    pending_deadline_breached,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.checks import Check, Observed, RequiredCheck, Unavailable
from issue_agent.domain.actions import AgentKind, EnableAutoMerge, RunAgent, RerunFailedChecks, UpdateBranch
from issue_agent.domain.fix import FixConfig, FixState, decide_fix
from issue_agent.domain.world import PRView, Review, WorldState


def observation(*reports, policy=(RequiredCheck("unit"),)):
    return Observed("head", "head", frozenset(policy), tuple(reports))


def test_empty_policy_gates_every_report_including_nonrequired_failures():
    assert isinstance(reduce_ci(observation(Check("mac", bucket="fail"), policy=())), Failed)
    assert isinstance(reduce_ci(observation(Check("mac", "QUEUED", "pending"), policy=())), Waiting)
    assert isinstance(reduce_ci(observation(policy=())), Clear)


@pytest.mark.parametrize("state,bucket", [("FAILURE", "fail"), ("QUEUED", "pending")])
def test_tauri_1074_advisory_incident_merges_without_repair_spend(state, bucket):
    policy = tuple(RequiredCheck(f"required-{i}") for i in range(6))
    ci = observation(*(Check(r.context, "SUCCESS", "pass", required=True) for r in policy),
                     Check("rust (macos-latest)", state, bucket), policy=policy)
    pr = PRView(1074, head_ref_name="fix/1074-tauri", head_ref_oid="head", author="fix-bot",
                labels=frozenset({Label.AI_PR_OPEN}), ci=ci, merge_state="UNSTABLE",
                reviews=(Review("APPROVED", "review-bot", commit_id="head"),))
    actions = decide_fix(WorldState(prs=(pr,)), FixConfig(fix_bot="fix-bot"), FixState(), fixer_busy=False)
    assert any(isinstance(a, EnableAutoMerge) for a in actions)
    assert not any(isinstance(a, (RunAgent, RerunFailedChecks, UpdateBranch)) for a in actions)


def test_pending_required_report_outranks_failure_and_queued_has_no_deadline():
    ci = observation(Check("unit", "QUEUED", "pending", "2020-01-01T00:00:00Z", required=True),
                     Check("lint", bucket="fail", required=True),
                     policy=(RequiredCheck("unit"), RequiredCheck("lint")))
    assert reduce_ci(ci) == Waiting(Queued((ci.reports[0],)))
    assert not pending_deadline_breached(ci, "2026-10-12T00:00:00Z", 3600)
    running = replace(ci, reports=(replace(ci.reports[0], state="IN_PROGRESS"), ci.reports[1]))
    assert isinstance(reduce_ci(running).reason, Executing)
    assert pending_deadline_breached(running, "2026-10-12T00:00:00Z", 3600)


@pytest.mark.parametrize("reports", [(), (Check("unit-renamed", bucket="pass"),),
    (Check("unit", bucket="pass", app_id=2, required=True),),
    (Check("unit", bucket="pass", app_id=1, required=False),)])
def test_missing_or_wrong_app_requirement_withholds_without_a_deadline(reports):
    ci = observation(*reports, policy=(RequiredCheck("unit", 1),))
    assert reduce_ci(ci) == Waiting(MissingRequired((RequiredCheck("unit", 1),)))
    assert not pending_deadline_breached(ci, "2026-10-12T00:00:00Z", 1)


def test_exact_matrix_names_and_status_check_collisions_keep_both_obligations():
    ci = observation(Check("unit", bucket="pass", required=True, kind="check"),
                     Check("unit", bucket="fail", required=True, kind="status"))
    assert isinstance(reduce_ci(ci), Failed)
    matrix = observation(Check("rust (macos-latest)", bucket="pass", required=True),
                         policy=(RequiredCheck("rust (ubuntu-latest)"),))
    assert isinstance(reduce_ci(matrix).reason, MissingRequired)


@pytest.mark.parametrize("state", ["SKIPPED", "NEUTRAL", "SUCCESS"])
def test_head_report_success_conclusions_clear(state):
    ci = Observed("head", "head", frozenset({RequiredCheck("unit")}),
                  (Check("unit", state, "pass", required=True),))
    assert isinstance(reduce_ci(ci), Clear)


def test_declared_policy_can_observe_test_merge_revision():
    ci = Observed("head", "test-merge", frozenset({RequiredCheck("unit")}),
        (Check("unit", "SUCCESS", "pass", required=True),))
    assert isinstance(reduce_ci(ci), Clear)


def test_unavailable_cannot_become_empty_green_or_drive_remediation():
    assert reduce_ci(Unavailable("partial pagination")) == Unavailable("partial pagination")
    assert not pending_deadline_breached(Unavailable("partial pagination"), "2026-10-12", 1)


def test_large_policy_names_every_absent_requirement():
    policy = frozenset(RequiredCheck(f"matrix-{i}") for i in range(10000))
    result = reduce_ci(Observed("head", "head", policy, ()))
    assert isinstance(result, Waiting) and isinstance(result.reason, MissingRequired)
    assert set(result.reason.requirements) == policy


@pytest.mark.parametrize("args", [(None, "head", frozenset(), ()),
    ("head", "test-merge", frozenset(), ()),
    ("head", "", frozenset(), ()), ("head", "head", (), ()),
    ("head", "head", frozenset(), (None,))])
def test_untrusted_shapes_cannot_construct_an_observed_variant(args):
    with pytest.raises(ValueError):
        Observed(*args)


def _incomplete(missing):
    return observation(policy=(RequiredCheck("never-reported"),)) if missing else Unavailable("incomplete policy")


@pytest.mark.parametrize("missing", [False, True])
def test_incomplete_evidence_holds_other_paid_repairs_that_clear_evidence_would_dispatch(missing):
    pr = PRView(1074, head_ref_name="fix/7-tauri", head_ref_oid="head", closing_issue=7, author="fix-bot",
        labels=frozenset({Label.AI_PR_OPEN}), ci=observation(policy=()), mergeable="MERGEABLE",
        reviews=(Review("CHANGES_REQUESTED", "review-bot", commit_id="head"),))
    cfg = FixConfig(fix_bot="fix-bot")
    assert any(isinstance(a, RunAgent) for a in decide_fix(WorldState(prs=(pr,)), cfg, FixState(), fixer_busy=False))
    assert not any(isinstance(a, RunAgent) for a in decide_fix(WorldState(prs=(replace(pr, ci=_incomplete(missing)),)),
        cfg, FixState(), fixer_busy=False))


# tauri #1068 (2026-10-12): GitHub runs no pull-request checks on a conflicting
# head, so required-check evidence for it never arrives. Holding the rebase on
# that evidence left an approved PR wedged for hours; the rebase reads no CI.
@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("closing_issue", [7, None])
def test_a_conflicting_pr_is_rebased_or_escalated_without_ci_evidence(missing, closing_issue):
    pr = PRView(1068, head_ref_name="fix/7-tauri" if closing_issue else "feat/no-issue", head_ref_oid="head",
        closing_issue=closing_issue, author="fix-bot", labels=frozenset({Label.AI_PR_OPEN}),
        ci=_incomplete(missing), mergeable="CONFLICTING",
        reviews=(Review("APPROVED", "review-bot", commit_id="head"),))
    actions = decide_fix(WorldState(prs=(pr,)), FixConfig(fix_bot="fix-bot"), FixState(), fixer_busy=False)
    if closing_issue:
        assert [a for a in actions if isinstance(a, RunAgent)] == [RunAgent(AgentKind.REBASE, 1068)]
    else:
        assert Label.NEEDS_HUMAN_REVIEW in {getattr(a, "add", None) for a in actions}


@pytest.mark.parametrize("missing", [False, True])
def test_with_auto_rebase_off_a_conflict_releases_no_other_repair_on_incomplete_evidence(missing):
    pr = PRView(1068, head_ref_name="fix/7-tauri", head_ref_oid="head", closing_issue=7, author="fix-bot",
        labels=frozenset({Label.AI_PR_OPEN}), ci=_incomplete(missing), mergeable="CONFLICTING",
        reviews=(Review("CHANGES_REQUESTED", "review-bot", commit_id="head"),))
    actions = decide_fix(WorldState(prs=(pr,)), FixConfig(fix_bot="fix-bot", auto_rebase_conflicts=False),
        FixState(), fixer_busy=False)
    assert not any(isinstance(a, RunAgent) for a in actions)
