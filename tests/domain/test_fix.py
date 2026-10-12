"""Fix-watcher decision behaviour — the poll_once control flow + invariants."""

from tests.domain.builders import with_checks

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tests.domain.builders import Check, Comment, Review, closing_ref, issue, pr, world

from issue_agent.domain.actions import (
    AgentKind,
    CancelWorkflowRuns,
    ClearIssueState,
    CloseIssue,
    EnableAutoMerge,
    EscalateCiPending,
    UpdateBranch,
    PostComment,
    Relabel,
    RemoveWorktree,
    RerunFailedChecks,
    ResetCiRetries,
    RunAgent,
    NoOp,
    Target,
)
from issue_agent.domain.fix import (
    CiStallStep,
    FixConfig,
    FixState,
    InFlightFix,
    HOLD_BLOCKER,
    HOLD_OVERLAP,
    Hold,
    breaker_tripped,
    breaker_key,
    ci_pending_stalls,
    ci_remediations,
    ci_stall_remedies,
    cleanup_actions,
    label_repair_actions,
    decide_fix as raw_decide_fix,
    duplicate_pr_guards,
    issues_needing_any_resume,
    issues_needing_resume,
    orphaned_cleanup_actions,
    pick_ready_issue,
    prs_needing_resume,
    reap_actions,
    should_automerge,
    should_update_branch,
)
from issue_agent.domain.backoff import LegEnd
from issue_agent.domain.labels import Label
from issue_agent.domain.contracts import RunId, WorkflowAttemptIdentity
from issue_agent.domain.workflow import (
    FreshResolve, NoAuthorizedRemediation, OpenGate, ResolveCeiling,
    ResolveEpisode, ResolveSession, WorkflowAbsent, WorkflowSnapshot, WorkflowUnavailable,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, WorkflowFamily, WorkflowFence, WorkflowKey, WorkflowRevision,
)

from tests.fakes.workflow_authority import with_issue_recovery
from issue_agent.domain.continuation_components import IssueRecoveryState
from issue_agent.domain import continuation_components as cc
from issue_agent.domain.contracts import ContinuationAdmission

CFG = FixConfig(fix_bot="fix-bot", )


def _workflow(number, *, session=False, failures=0, held=False, error=""):
    snapshot = WorkflowSnapshot(
        WorkflowRevision(WorkflowFence(WorkflowKey("o/r", IssueRef(number)), 2), 1),
        FamilyAuthority(WorkflowFamily.RESOLVE, 1), ResolveEpisode(f"episode-{number}", failures, error),
    )
    if session:
        source = WorkflowAttemptIdentity(snapshot.binding, RunId(f"run-{number}"), f"leg-{number}")
        snapshot = replace(snapshot, episode=replace(snapshot.episode, continuation=FreshResolve(
            ResolveSession(source, f"session-{number}", "codex", "model"), 1,
        )))
    if held:
        snapshot = replace(snapshot, gates=(OpenGate(
            f"gate-{number}", 2, ResolveCeiling(snapshot.episode.episode_id, failures, 3, error),
            NoAuthorizedRemediation("Operator decision required"),
        ),))
    return with_issue_recovery(snapshot)


def _with_session(number=7, **kwargs):
    consumed = kwargs.pop("resumed_at", {}).get(number, "")
    snapshot = with_issue_recovery(_workflow(number, session=True), state=IssueRecoveryState(consumed_reply_at=consumed))
    return FixState(resolve_workflows={number: snapshot}, **kwargs)


def decide_fix(observed, cfg, state, **kwargs):
    """Legacy behavior fixtures explicitly assume activated, available episodes.

    Authority-negative tests call raw_decide_fix so absent input stays absent.
    """
    snapshots = {item.number: _workflow(item.number) for item in observed.issues}
    return raw_decide_fix(observed, cfg, replace(
        state, resolve_workflows={**snapshots, **state.resolve_workflows},
    ), **kwargs)


def _run_agents(actions):
    return [a for a in actions if isinstance(a, RunAgent)]


# ── priority ordering ─────────────────────────────────────────────────────────
def test_pick_ready_prefers_p1():
    w = world(issues=[issue(3, "ai-ready"), issue(1, "ai-ready", "P1"), issue(2, "ai-ready", "P2")])
    assert pick_ready_issue(w) == 1


def test_pick_ready_none_when_empty():
    assert pick_ready_issue(world(issues=[issue(1, "ai-working")])) is None


def test_pick_ready_skips_blocked_by_open_dependency():
    # An ai-ready issue with an OPEN blocked-by dependency must be skipped — its
    # prerequisite isn't merged yet (the P3–P6-depends-on-P2 case).
    w = world(issues=[issue(1, "ai-ready", "P1", blocked_by_open=True)])
    assert pick_ready_issue(w) is None


def test_pick_ready_prefers_unblocked_over_higher_priority_blocked():
    # A blocked P1 is passed over for an unblocked P2 — priority never overrides
    # the dependency gate.
    w = world(issues=[
        issue(1, "ai-ready", "P1", blocked_by_open=True),
        issue(2, "ai-ready", "P2"),
    ])
    assert pick_ready_issue(w) == 2


def test_pick_ready_takes_issue_once_blocker_closes():
    # blocked_by_open=False (blocker closed) → the issue is pickable again.
    w = world(issues=[issue(1, "ai-ready", "P1", blocked_by_open=False)])
    assert pick_ready_issue(w) == 1


def test_pr_repair_is_dispatched_before_a_new_resolve():
    ready = issue(7, "ai-ready")
    repair = pr(
        100,
        "ai-pr-open",
        state="OPEN",
        head_ref_name="fix/5-x",
        closing_issue=5,
        reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),),
    )

    actions = decide_fix(
        world(issues=[ready], prs=[repair]), CFG, FixState(), fixer_busy=False
    )

    assert _run_agents(actions) == [RunAgent(AgentKind.ADDRESS_REVIEW, 100)]


def test_pick_ready_skips_issue_overlapping_open_pr_files():
    # #59-vs-#60 collision: an ai-ready issue whose declared paths overlap an OPEN
    # PR's changed files is held back (dispatching races a guaranteed conflict).
    iss = issue(2, "ai-ready", "P2", declared_paths=frozenset({"backend/app/services/x.py"}))
    open_pr = pr(100, "ai-pr-open", state="OPEN",
                 changed_files=frozenset({"backend/app/services/x.py", "other.py"}))
    assert pick_ready_issue(world(issues=[iss], prs=[open_pr])) is None


def test_pick_ready_skips_issue_when_router_prediction_overlaps_open_pr_files():
    """A routed path protects human-written issues that name no paths themselves."""
    iss = issue(2, "ai-ready", "P2", predicted_paths=frozenset({"src/action.rs"}))
    open_pr = pr(100, "ai-pr-open", state="OPEN",
                 changed_files=frozenset({"src/action.rs", "src/help.rs"}))
    assert pick_ready_issue(world(issues=[iss], prs=[open_pr])) is None


def test_pick_ready_allows_issue_with_no_file_overlap():
    iss = issue(2, "ai-ready", "P2", declared_paths=frozenset({"backend/app/routes/y.py"}))
    open_pr = pr(100, "ai-pr-open", state="OPEN",
                 changed_files=frozenset({"backend/app/services/x.py"}))
    assert pick_ready_issue(world(issues=[iss], prs=[open_pr])) == 2


def test_pick_ready_failopen_when_issue_declares_no_paths():
    # No declared paths → unpredictable → NOT held (gate only fires on known overlap).
    iss = issue(2, "ai-ready", "P2")  # declared_paths defaults empty
    open_pr = pr(100, "ai-pr-open", state="OPEN",
                 changed_files=frozenset({"backend/app/services/x.py"}))
    assert pick_ready_issue(world(issues=[iss], prs=[open_pr])) == 2


def test_pick_ready_overlap_ignores_closed_pr():
    # A MERGED/CLOSED PR no longer races — overlap with it must not hold the issue.
    iss = issue(2, "ai-ready", "P2", declared_paths=frozenset({"backend/app/services/x.py"}))
    merged = pr(100, state="MERGED", changed_files=frozenset({"backend/app/services/x.py"}))
    assert pick_ready_issue(world(issues=[iss], prs=[merged])) == 2



def test_holds_do_not_mask_a_pickable_issue():
    # With one held and one free issue, the free one is still dispatched.
    held = issue(1, "ai-ready", "P1", declared_paths=frozenset({"x.py"}))
    free = issue(2, "ai-ready", "P2")
    open_pr = pr(100, state="OPEN", changed_files=frozenset({"x.py"}))
    w = world(issues=[held, free], prs=[open_pr])
    assert pick_ready_issue(w) == 2



# ── the blocked-by dependency also holds the PR REPAIR arm ───────────────────
# keifu #130/#157 (2026-08-03): both issues already had an OPEN PR when their
# blocker #185 (a Windows CI wedge) was filed. The admission-stage gate only
# covers ai-ready issues, so unparking them would have re-dispatched FIX_CI
# straight into the wedge the blocker existed to fix.
def _blocked_pr_world(*, blocked: bool, blockers=(185,), **pr_kw):
    """One OPEN fixer PR with red CI, linked to an OPEN issue whose GitHub
    dependency is (or isn't) still open."""
    linked = issue(
        130,
        "ai-pr-open",
        blocked_by_open=blocked,
        open_blocker_numbers=blockers if blocked else (),
    )
    pr_kw.setdefault("checks", (Check("unit", bucket="fail"),))
    repair = pr(100, "ai-pr-open", state="OPEN", head_ref_name="fix/130-x",
                closing_issue=130, **pr_kw)
    return world(issues=[linked], prs=[repair])


def test_open_blocker_withholds_every_repair_leg_on_the_linked_pr():
    actions = decide_fix(
        _blocked_pr_world(blocked=True), CFG, FixState(), fixer_busy=False
    )
    assert _run_agents(actions) == []
    assert not any(isinstance(a, RerunFailedChecks) for a in actions)


def test_the_same_pr_is_repaired_once_its_blocker_closes():
    # Anti-vacuity twin: identical world, blocker closed → the normal red-CI
    # repair path fires, so the test above is asserting the gate and not a
    # world that could never dispatch.
    actions = decide_fix(
        _blocked_pr_world(blocked=False), CFG, FixState(), fixer_busy=False
    )
    assert _run_agents(actions) == [RunAgent(AgentKind.FIX_CI, 100)]


def test_a_closed_linked_issue_never_holds_its_open_pr():
    # An operator closing a blocked issue while its PR is open must not wedge
    # the PR permanently AND invisibly: the world carries the closed
    # ai-pr-open slice, cleanup skips a covered PR, and holds skip closed
    # issues — so a decide-side hold here would be silent forever
    # (adversarial review F2, 2026-08-03; decide and report share
    # `dependency_blocked`).
    w = _blocked_pr_world(blocked=True)
    closed = replace(w.issues[0], state="closed")
    w = replace(w, issues=(closed,))
    assert _run_agents(decide_fix(w, CFG, FixState(), fixer_busy=False)) == [
        RunAgent(AgentKind.FIX_CI, 100)
    ]


def test_settled_green_still_closes_its_breaker_episode_while_held():
    # The blocker hold is not allowed to freeze breaker bookkeeping: a PR that
    # settles green while held must still emit ResetCiRetries, or the lifetime
    # counter defect returns (adversarial review F4, 2026-08-03).
    w = _blocked_pr_world(blocked=True, checks=(Check("unit", bucket="pass"),))
    actions = decide_fix(
        w, CFG, FixState(ci_retries={130: 2}), fixer_busy=False
    )
    assert ResetCiRetries(130) in actions
    assert _run_agents(actions) == []


def test_open_blocker_withholds_address_review_and_rebase_too():
    # The gate is the whole arm, not just the CI branch.
    for extra in (
        {"reviews": (Review("CHANGES_REQUESTED", "review-bot", "t1"),),
         "checks": ()},
        {"mergeable": "CONFLICTING", "checks": ()},
    ):
        held = decide_fix(_blocked_pr_world(blocked=True, **extra), CFG,
                          FixState(), fixer_busy=False)
        free = decide_fix(_blocked_pr_world(blocked=False, **extra), CFG,
                          FixState(), fixer_busy=False)
        assert _run_agents(held) == [], extra
        assert _run_agents(free) != [], extra


def test_a_blocked_issues_pr_that_went_green_and_approved_still_merges():
    # Deliberately ungated: the blocker holds paid repair, not a PR that is
    # already merge-ready — holding that would strand finished work.
    linked = issue(130, "ai-pr-open", blocked_by_open=True,
                   open_blocker_numbers=(185,))
    ready = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/130-x",
        closing_issue=130, head_ref_oid="head-100",
        reviews=(Review("APPROVED", "review-bot", "t1", commit_id="head-100"),),
        checks=(Check("build", bucket="pass"),),
    )
    actions = decide_fix(world(issues=[linked], prs=[ready]), CFG, FixState(),
                         fixer_busy=False)
    assert EnableAutoMerge(100, "squash", "head-100") in actions



def test_resolve_dispatch_relabels_then_runs():
    w = world(issues=[issue(5, "ai-ready", updated_at="issue-rev-5")])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    claim = Relabel(Target.ISSUE, 5,
        Label.AI_READY,
        Label.AI_WORKING,
        "issue-rev-5", workflow=_workflow(5).binding)
    assert claim in actions
    dispatch = next(action for action in actions if isinstance(action, RunAgent))
    assert dispatch == RunAgent(AgentKind.RESOLVE, 5, workflow=_workflow(5).binding)
    assert dispatch.requires == claim


# ── single-fixer invariant ─────────────────────────────────────────────────────
def test_only_one_fixer_action_per_poll():
    w = world(issues=[issue(5, "ai-ready"), issue(6, "ai-ready")])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert len(_run_agents(actions)) == 1


def test_busy_fixer_dispatches_nothing_new():
    w = world(issues=[issue(5, "ai-ready")])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=True)
    assert _run_agents(actions) == []
    # and a busy fixer does not reap (the run-lock is held).
    assert not any(isinstance(a, Relabel) for a in actions)


def test_available_slots_dispatch_only_path_disjoint_issues():
    w = world(issues=[
        issue(1, "ai-ready", predicted_paths=frozenset({"src/a.py"})),
        issue(2, "ai-ready", predicted_paths=frozenset({"src/b.py"})),
        issue(3, "ai-ready", predicted_paths=frozenset({"src/b.py"})),
    ])

    actions = decide_fix(
        w,
        CFG,
        FixState(),
        fixer_busy=False,
        dispatch_slots=3,
    )

    assert _run_agents(actions) == [
        RunAgent(AgentKind.RESOLVE, 1, workflow=_workflow(1).binding),
        RunAgent(AgentKind.RESOLVE, 2, workflow=_workflow(2).binding),
    ]


def test_unknown_path_resolves_are_staged_until_router_prediction_is_observed():
    w = world(issues=[
        issue(1, "ai-ready"),
        issue(2, "ai-ready"),
    ])

    actions = decide_fix(
        w,
        CFG,
        FixState(),
        fixer_busy=False,
        dispatch_slots=2,
    )

    assert _run_agents(actions) == [RunAgent(AgentKind.RESOLVE, 1, workflow=_workflow(1).binding)]


def test_active_unknown_path_resolve_excludes_other_work():
    w = world(issues=[
        issue(2, "ai-ready", predicted_paths=frozenset({"src/free.py"})),
    ])
    inflight = (InFlightFix(1, exclusive=True),)

    assert pick_ready_issue(w, inflight) is None


def test_inflight_paths_hold_a_ready_issue_before_it_has_a_pr():
    w = world(issues=[
        issue(2, "ai-ready", predicted_paths=frozenset({"src/shared.py"})),
        issue(3, "ai-ready", predicted_paths=frozenset({"src/free.py"})),
    ])
    inflight = (InFlightFix(1, frozenset({"src/shared.py"})),)

    assert pick_ready_issue(w, inflight) == 3


def test_inflight_issue_cannot_receive_a_second_pr_leg():
    repair = pr(
        100,
        "ai-pr-open",
        state="OPEN",
        head_ref_name="fix/5-x",
        closing_issue=5,
        changed_files=frozenset({"src/a.py"}),
        reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),),
    )

    actions = decide_fix(
        world(prs=[repair]),
        CFG,
        FixState(inflight=(InFlightFix(5, frozenset({"src/a.py"})),)),
        fixer_busy=True,
        dispatch_slots=1,
    )

    assert _run_agents(actions) == []


# ── resume dedupe (anti-loop) ──────────────────────────────────────────────────
def test_resume_needs_session_and_new_reply():
    iss = issue(7, "ai-blocked", comments=(Comment("alice", "here you go", "2026-10-01T00:00:02Z"),))
    w = world(issues=[iss])
    # no session → not eligible
    assert issues_needing_resume(w, FixState(resolve_workflows={7: _workflow(7)}), CFG) == []
    # with a session → eligible
    st = _with_session()
    assert issues_needing_resume(w, st, CFG) == [(7, "2026-10-01T00:00:02Z")]


def test_resume_skips_already_resumed_reply():
    iss = issue(7, "ai-blocked", comments=(Comment("alice", "x", "2026-10-01T00:00:02Z"),))
    st = _with_session(resumed_at={7: "2026-10-01T00:00:02Z"})
    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_resume_never_fires_on_a_closed_issue():
    """A closed issue is terminal even while it still carries ai-blocked: the
    operator's closing comment on a duplicate read as an unconsumed reply and
    bought a paid resume leg that could only re-ask (keifu #196, 2026-08-10)."""
    iss = issue(
        7, "ai-blocked", state="closed",
        comments=(Comment("alice", "closing as duplicate of #192", "2026-10-01T00:00:02Z"),),
    )
    st = _with_session()
    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []
    assert issues_needing_any_resume(world(issues=[iss]), st, CFG) == []


def test_resume_ignores_bot_and_fixer_comments():
    iss = issue(
        7, "ai-blocked",
        comments=(Comment("fix-bot", "asking", "2026-10-01T00:00:01Z"), Comment("ci[bot]", "noise", "2026-10-01T00:00:02Z")),
    )
    st = _with_session()
    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_resume_accepts_operator_reply_when_operator_is_also_fix_bot():
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment(
                "fix-bot",
                "**Agent needs input:** choose A or B\n\n_Reply to unblock._",
                "2026-10-01T00:00:01Z",
            ),
            Comment("fix-bot", "Use A.", "2026-10-01T00:00:02Z"),
        ),
    )
    st = _with_session()

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == [(7, "2026-10-01T00:00:02Z")]


def test_resume_accepts_reply_when_fix_bot_is_an_app_identity():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot[bot]", prompt, "2026-10-01T00:00:01Z"),
            Comment("fix-bot[bot]", "Use A.", "2026-10-01T00:00:02Z"),
        ),
    )
    cfg = FixConfig(fix_bot="fix-bot[bot]")
    st = _with_session()

    assert issues_needing_resume(world(issues=[iss]), st, cfg) == [(7, "2026-10-01T00:00:02Z")]


def test_unblock_prompt_does_not_count_as_its_own_reply():
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment(
                "fix-bot",
                "**Agent needs input:** choose A or B\n\n_Reply to unblock._",
                "2026-10-01T00:00:01Z",
            ),
        ),
    )
    st = _with_session()

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_second_round_unblock_prompt_supersedes_the_original_reply():
    first = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    second = "**Agent still needs input:** provide a value\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", first, "2026-10-01T00:00:01Z"),
            Comment("alice", "Use A.", "2026-10-01T00:00:02Z"),
            Comment("fix-bot", second, "2026-10-01T00:00:03Z"),
        ),
    )
    st = _with_session(resumed_at={7: "2026-10-01T00:00:02Z"})

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_author_leg_prompt_accepts_fix_bot_operator_reply():
    prompt = "**Author needs input:** expected behavior?\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"),
            Comment("fix-bot", "Use the documented behavior.", "2026-10-01T00:00:02Z"),
        ),
    )
    st = _with_session()

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == [(7, "2026-10-01T00:00:02Z")]


def test_quoted_unblock_prompt_is_consumed_as_the_reply():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"),
            Comment("alice", f"> {prompt}\n\nUse A.", "2026-10-01T00:00:02Z"),
        ),
    )
    st = _with_session()

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == [(7, "2026-10-01T00:00:02Z")]


def test_unrelated_bot_noise_after_prompt_does_not_trigger_resume():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"),
            Comment("ci[bot]", "build finished", "2026-10-01T00:00:02Z"),
        ),
    )
    st = _with_session()

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_resume_dispatch_relabels_blocked_to_working():
    # A blocked issue with a new human reply + a saved session → RESUME dispatch,
    # and the issue must flip ai-blocked → ai-working for the run's duration (so it
    # doesn't read as still-blocked while actively resuming).
    iss = issue(7, "ai-blocked", comments=(Comment("alice", "go ahead", "2026-10-01T00:00:02Z"),))
    st = _with_session()
    actions = decide_fix(world(issues=[iss]), CFG, st, fixer_busy=False)
    relabels = [a for a in actions if isinstance(a, Relabel)]
    assert any(
        r.target == Target.ISSUE and r.number == 7
        and r.remove == Label.AI_BLOCKED and r.add == Label.AI_WORKING
        for r in relabels
    )
    runs = _run_agents(actions)
    assert runs and runs[0].kind == AgentKind.RESUME and runs[0].number == 7


def test_blocked_pr_reply_dispatches_pr_resume_for_linked_issue_session():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"), Comment("alice", "Use A.", "2026-10-01T00:00:02Z")),
    )
    state = FixState(sessions=frozenset({7}))
    w = world(prs=[blocked])

    assert prs_needing_resume(w, state, CFG) == [(100, 7, "2026-10-01T00:00:02Z")]
    actions = decide_fix(w, CFG, state, fixer_busy=False)

    assert RunAgent(AgentKind.PR_RESUME, 100) in _run_agents(actions)


def test_blocked_pr_review_prompt_ignores_comments_before_the_prompt():
    review_prompt = (
        "**Agent needs clarification on review:**\n\n"
        "Which approach should I take?\n\n_Reply to unblock._"
    )
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(
            Comment("alice", "Earlier feedback", "2026-10-01T00:00:01Z"),
            Comment("fix-bot", review_prompt, "2026-10-01T00:00:02Z"),
        ),
    )
    state = FixState(sessions=frozenset({7}))

    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == []
    assert _run_agents(decide_fix(world(prs=[blocked]), CFG, state, fixer_busy=False)) == []


@pytest.mark.parametrize("blank", [False, True])
def test_unavailable_pr_reply_authority_refuses_only_the_selected_reply(blank):
    comments = (Comment("alice", " " if blank else "Use A.", "2026-10-01T00:00:02Z"),)
    blocked = pr(100, "ai-pr-open", "ai-blocked", closing_issue=7, comments=comments)
    sibling = pr(101, "ai-pr-open", "ai-blocked", closing_issue=8, comments=comments)
    state = FixState(sessions=frozenset({7, 8}), pr_reply_control_errors={100: "missing selected receipt"})
    observed = world(prs=[blocked, sibling])

    assert prs_needing_resume(observed, state, CFG) == [(101, 8, "2026-10-01T00:00:02Z")]
    runs = _run_agents(decide_fix(observed, CFG, state, fixer_busy=False))
    assert RunAgent(AgentKind.PR_RESUME, 100) not in runs
    assert RunAgent(AgentKind.PR_RESUME, 101) in runs


@pytest.mark.parametrize("operation", ["cleanup", "review", "ci", "reset", "rebase"])
def test_pr_reply_read_refusal_preserves_other_action_outcomes(operation):
    observed, state, expected = {
        "cleanup": (pr(100, "ai-pr-open", state="MERGED", closing_issue=9), FixState(), ClearIssueState(100, 9, True)),
        "review": (pr(100, "ai-pr-open", reviews=(Review("CHANGES_REQUESTED", "review-bot", "2026-10-01T00:00:01Z"),)), FixState(), RunAgent(AgentKind.ADDRESS_REVIEW, 100)),
        "ci": (pr(100, "ai-pr-open", checks=(Check("unit", bucket="fail"),)), FixState(), RunAgent(AgentKind.FIX_CI, 100)),
        "reset": (pr(100, "ai-pr-open", closing_issue=9, checks=(Check("unit", bucket="pass"),)), FixState(ci_retries={9: 2}), ResetCiRetries(9)),
        "rebase": (pr(100, "ai-pr-open", mergeable="CONFLICTING"), FixState(), RunAgent(AgentKind.REBASE, 100)),
    }[operation]
    current = world(prs=[observed], issues=[issue(9, "ai-pr-open")])
    baseline = decide_fix(current, CFG, state, fixer_busy=False)
    held = decide_fix(current, CFG,
        replace(state, pr_reply_control_errors={100: "unavailable reply source"}), fixer_busy=False)

    assert expected in baseline
    assert held == baseline


def test_blocked_pr_reply_already_resumed_does_not_dispatch_again():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"), Comment("alice", "Use A.", "2026-10-01T00:00:02Z")),
    )
    state = FixState(sessions=frozenset({7}), pr_resumed_at={100: "2026-10-01T00:00:02Z"})

    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == []
    assert _run_agents(decide_fix(world(prs=[blocked]), CFG, state, fixer_busy=False)) == []


def test_blocked_pr_newer_reply_after_dedupe_dispatches_once_more():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(
            Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"),
            Comment("alice", "Use A.", "2026-10-01T00:00:02Z"),
            Comment("alice", "Actually use B.", "2026-10-01T00:00:03Z"),
        ),
    )
    state = FixState(sessions=frozenset({7}), pr_resumed_at={100: "2026-10-01T00:00:02Z"})

    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == [(100, 7, "2026-10-01T00:00:03Z")]
    assert RunAgent(AgentKind.PR_RESUME, 100) in _run_agents(
        decide_fix(world(prs=[blocked]), CFG, state, fixer_busy=False)
    )


def test_blocked_pr_reply_from_operator_who_is_fix_bot_dispatches_resume():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"), Comment("fix-bot", "Use A.", "2026-10-01T00:00:02Z")),
    )
    state = FixState(sessions=frozenset({7}))

    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == [(100, 7, "2026-10-01T00:00:02Z")]


@pytest.mark.parametrize("blank", [False, True])
def test_older_pr_reply_does_not_dispatch_after_newer_consumption(blank):
    blocked = pr(100, "ai-pr-open", "ai-blocked", closing_issue=7,
                 comments=(Comment("alice", " " if blank else "old", "2026-10-01T00:00:01Z"),))
    state = FixState(sessions=frozenset({7}), pr_resumed_at={100: "2026-10-01T00:00:02Z"})
    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == []


def test_blank_pr_reply_dispatches_without_saved_session_for_unpaid_consumption():
    blocked = pr(100, "ai-pr-open", "ai-blocked", closing_issue=7,
                 comments=(Comment("alice", " ", "2026-10-01T00:00:01Z"),))
    assert prs_needing_resume(world(prs=[blocked]), FixState(), CFG) == [(100, 7, "2026-10-01T00:00:01Z")]


def test_pr_resume_machine_death_waits_for_backoff_and_never_escalates():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"), Comment("alice", "Use A.", "2026-10-01T00:00:02Z")),
    )
    state = FixState(sessions=frozenset({7}), leg_ends=_machine_deaths(7, AgentKind.PR_RESUME, 2))

    waiting = decide_fix(world(prs=[blocked], now=_stamp(timedelta(seconds=119))), CFG, state, fixer_busy=False)
    resumed = decide_fix(world(prs=[blocked], now=_stamp(timedelta(seconds=120))), CFG, state, fixer_busy=False)

    assert _run_agents(waiting) == [] and not _escalated(waiting)
    assert RunAgent(AgentKind.PR_RESUME, 100) in _run_agents(resumed)


def test_needs_human_review_pr_reply_never_reopens_the_escalation():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    escalated = pr(
        100, "ai-pr-open", "needs-human-review", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "2026-10-01T00:00:01Z"), Comment("alice", "Use A.", "2026-10-01T00:00:02Z")),
    )
    state = FixState(sessions=frozenset({7}))

    assert prs_needing_resume(world(prs=[escalated]), state, CFG) == []
    assert _run_agents(decide_fix(world(prs=[escalated]), CFG, state, fixer_busy=False)) == []


# ── conflict backup plan: auto-rebase a CONFLICTING PR ────────────────────────
def test_conflicting_pr_dispatches_rebase():
    p = pr(100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x", mergeable="CONFLICTING")
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    runs = _run_agents(actions)
    assert runs and runs[0].kind == AgentKind.REBASE and runs[0].number == 100


def _workflow_with_pending_rebase(pending_generation):
    snapshot = _workflow(5)
    if pending_generation is not None:
        component = snapshot.issue_recovery
        assert isinstance(component, cc.ActiveIssueRecovery)
        fence = WorkflowFence(snapshot.revision.fence.key, pending_generation)
        pending = ContinuationAdmission(
            WorkflowAttemptIdentity(component.identity.bind(fence), RunId("pending-rebase"), "rebase-leg"),
            1,
        )
        snapshot = replace(snapshot, issue_recovery=replace(
            component,
            state=replace(component.state, pending_rebase=pending),
            watermarks=cc.ContinuationWatermarks(1, 0, fence),
        ))
    return snapshot


@pytest.mark.parametrize("pending_generation", [None, 1, 2])
def test_unresolved_current_rebase_does_not_starve_ready_issue(pending_generation):
    snapshot = _workflow_with_pending_rebase(pending_generation)
    observed = world(
        issues=[issue(995, "ai-ready")],
        prs=[pr(100, "ai-pr-open", head_ref_name="fix/5-x", mergeable="CONFLICTING")],
    )
    actions = decide_fix(observed, CFG, FixState(resolve_workflows={5: snapshot}),
                         fixer_busy=False, dispatch_slots=1)
    runs = _run_agents(actions)
    expected = (AgentKind.RESOLVE, 995) if pending_generation == 2 else (AgentKind.REBASE, 100)
    assert [(run.kind, run.number) for run in runs] == [expected]
    if pending_generation == 2:
        assert any(isinstance(action, NoOp) and action.reason for action in actions)



def test_pending_rebase_preserves_retry_ceiling_and_unrelated_ready_work():
    state = FixState(
        resolve_workflows={5: _workflow_with_pending_rebase(2)},
        rebase_retries={5: CFG.max_rebase_retries},
    )
    observed = world(issues=[issue(995, "ai-ready")], prs=[
        pr(100, "ai-pr-open", head_ref_name="fix/5-x", mergeable="CONFLICTING"),
    ])
    actions = decide_fix(observed, CFG, state, fixer_busy=False, dispatch_slots=1)
    assert any(isinstance(action, Relabel) and action.number == 100
               and action.add == Label.NEEDS_HUMAN_REVIEW for action in actions)
    assert [(run.kind, run.number) for run in _run_agents(actions)] == [(AgentKind.RESOLVE, 995)]


def test_mergeable_pr_does_not_dispatch_rebase():
    p = pr(100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x", mergeable="MERGEABLE")
    runs = _run_agents(decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False))
    assert not any(r.kind == AgentKind.REBASE for r in runs)


def test_rebase_breaker_escalates_to_human():
    p = pr(100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x", mergeable="CONFLICTING")
    # at the retry cap → escalate, not another rebase
    st = FixState(rebase_retries={5: CFG.max_rebase_retries})
    actions = decide_fix(world(prs=[p]), CFG, st, fixer_busy=False)
    assert not any(isinstance(a, RunAgent) and a.kind == AgentKind.REBASE for a in actions)
    relabels = [a for a in actions if isinstance(a, Relabel)]
    assert any(r.add == Label.NEEDS_HUMAN_REVIEW for r in relabels)


def test_auto_rebase_off_does_not_dispatch():
    cfg = FixConfig(fix_bot="fix-bot", auto_rebase_conflicts=False)
    p = pr(100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x", mergeable="CONFLICTING")
    runs = _run_agents(decide_fix(world(prs=[p]), cfg, FixState(), fixer_busy=False))
    assert not any(r.kind == AgentKind.REBASE for r in runs)


# ── issueless PR (Jira-keyed branch, no closing-ref) → escalate, never loop ────
# Regression for PR #112: a CONFLICTING PR whose branch carries no issue number
# re-dispatched REBASE every poll because the fixer's workflow early-returns
# (no issue → no worktree) and the breaker key collapsed to a dead sentinel.
def _issueless_conflicting_pr():
    return pr(112, "ai-pr-open", state="OPEN",
              head_ref_name="feat/ACME-2468-add-feature", mergeable="CONFLICTING")


def test_issueless_conflicting_pr_escalates_not_rebase():
    actions = decide_fix(world(prs=[_issueless_conflicting_pr()]), CFG, FixState(), fixer_busy=False)
    assert not any(isinstance(a, RunAgent) and a.kind == AgentKind.REBASE for a in actions)
    relabels = [a for a in actions if isinstance(a, Relabel)]
    assert any(r.add == Label.NEEDS_HUMAN_REVIEW for r in relabels)


def test_issueless_clean_pr_left_alone():
    # A clean issueless PR needs no fixer work → NOT escalated (reviewer/auto-merge
    # still handle it); only an issueless PR that actually needs the fixer escalates.
    p = pr(113, "ai-pr-open", state="OPEN", head_ref_name="feat/ACME-9-x",
           reviews=(Review("APPROVED", "review-bot", "t1"),),
           checks=(Check("build", bucket="pass"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert not any(
        isinstance(a, Relabel) and a.add == Label.NEEDS_HUMAN_REVIEW for a in actions
    )


def test_issueless_pr_breaker_key_is_negative_pr():
    assert breaker_key(_issueless_conflicting_pr()) == -112
    # An issue-linked PR keys on the issue number (positive).
    assert breaker_key(pr(50, head_ref_name="fix/7-x")) == 7


# ── auto-merge gating ───────────────────────────────────────────────────────────
def _approved_green_pr():
    return pr(
        100, "ai-pr-open",
        head_ref_oid="head-100",
        reviews=(
            Review(
                "APPROVED", "review-bot", "t1", commit_id="head-100"
            ),
        ),
        checks=(Check("build", bucket="pass"),),
    )


def test_automerge_when_approved_green_three_identity():
    assert should_automerge(_approved_green_pr(), FixState(), CFG) is True


def test_no_automerge_when_only_self_approved():
    p = pr(100, "ai-pr-open", reviews=(Review("APPROVED", "fix-bot", "t1"),),
           checks=(Check("build", bucket="pass"),))
    assert should_automerge(p, FixState(), CFG) is False


def test_no_automerge_in_solo_mode():
    cfg = FixConfig(fix_bot="fix-bot", identity_mode="solo")
    assert should_automerge(_approved_green_pr(), FixState(), cfg) is False


def test_no_automerge_when_ci_pending():
    p = pr(100, "ai-pr-open", reviews=(Review("APPROVED", "review-bot", "t1"),),
           checks=(Check("build", state="IN_PROGRESS"),))
    assert should_automerge(p, FixState(), CFG) is False


def test_eligible_exact_head_remains_reconcilable():
    st = FixState()
    assert should_automerge(_approved_green_pr(), st, CFG) is True


def test_stale_or_unattributed_approval_never_authorizes_current_head():
    stale = pr(
        100,
        "ai-pr-open",
        head_ref_oid="head-2",
        reviews=(
            Review(
                "APPROVED",
                "review-bot",
                "t1",
                commit_id="head-1",
            ),
        ),
        checks=(Check("build", bucket="pass"),),
    )
    unknown = pr(
        101,
        "ai-pr-open",
        head_ref_oid="head-2",
        reviews=(Review("APPROVED", "review-bot", "t1"),),
        checks=(Check("build", bucket="pass"),),
    )

    for candidate in (stale, unknown):
        assert not should_automerge(candidate, FixState(), CFG)
        assert not should_update_branch(
            replace(candidate, mergeable="MERGEABLE", merge_state="BEHIND"),
            FixState(),
            CFG,
        )


def test_arm_on_open_never_bypasses_current_head_review():
    cfg = replace(CFG, )
    candidate = pr(
        100,
        "ai-pr-open",
        head_ref_oid="head-1",
        checks=(Check("build", bucket="pass"),),
    )

    assert not should_automerge(candidate, FixState(), cfg)


def test_decide_emits_enable_automerge():
    w = world(prs=[_approved_green_pr()])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert EnableAutoMerge(100, "squash", "head-100") in actions


# ── BEHIND (clean-but-stale) branch → update-branch so armed auto-merge lands ──
def _approved_green_behind_pr():
    return pr(
        100, "ai-pr-open",
        head_ref_oid="head-100",
        reviews=(
            Review(
                "APPROVED", "review-bot", "t1", commit_id="head-100"
            ),
        ),
        checks=(Check("build", bucket="pass"),),
        mergeable="MERGEABLE", merge_state="BEHIND",
    )


def test_behind_pr_emits_update_branch_with_automerge():
    w = world(prs=[_approved_green_behind_pr()])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert EnableAutoMerge(100, "squash", "head-100") not in actions
    assert UpdateBranch(100, "head-100") in actions


def test_behind_pr_reconciles_branch_before_merge():
    st = FixState()
    actions = decide_fix(world(prs=[_approved_green_behind_pr()]), CFG, st, fixer_busy=False)
    assert EnableAutoMerge(100, "squash", "head-100") not in actions
    assert UpdateBranch(100, "head-100") in actions


def test_clean_pr_does_not_update_branch():
    # A CLEAN approved PR arms auto-merge but needs NO branch update.
    actions = decide_fix(world(prs=[_approved_green_pr()]), CFG, FixState(), fixer_busy=False)
    assert not any(isinstance(a, UpdateBranch) for a in actions)



# ── M14: auto-merge is INDEPENDENT of the fixer lock ──────────────────────────
# It is documented as lock-free but used to live at the tail of the PR-consuming
# loop, so every `continue` in that loop skipped it. The issueless arm `continue`s
# unconditionally, so an approved+green issueless PR armed auto-merge ONLY on the
# polls where the fixer happened to be BUSY — busy skips the whole consuming block,
# so the `continue` never ran. Same world, opposite outcome, decided by an
# unrelated lock. These pin the invariant: for one world, both lock states agree.
def _issueless_approved_green_pr(**kw):
    return pr(113, "ai-pr-open", head_ref_name="feat/ACME-9-x",
              head_ref_oid="head-113",
              reviews=(
                  Review(
                      "APPROVED",
                      "review-bot",
                      "t1",
                      commit_id="head-113",
                  ),
              ),
              checks=(Check("build", bucket="pass"),), **kw)


def test_issueless_approved_green_pr_arms_automerge_idle_and_busy():
    w = world(prs=[_issueless_approved_green_pr()])
    idle = decide_fix(w, CFG, FixState(), fixer_busy=False)
    busy = decide_fix(w, CFG, FixState(), fixer_busy=True)
    assert EnableAutoMerge(113, "squash", "head-113") in idle
    assert EnableAutoMerge(113, "squash", "head-113") in busy


def test_issueless_behind_pr_updates_branch_idle_and_busy():
    # Same divergence on the other 3c arm: an armed-but-BEHIND branch stayed
    # behind forever on idle polls, so the armed auto-merge never landed.
    w = world(prs=[_issueless_approved_green_pr(mergeable="MERGEABLE", merge_state="BEHIND")])
    expected = UpdateBranch(113, "head-113")
    assert expected in decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert expected in decide_fix(w, CFG, FixState(), fixer_busy=True)


def test_dispatching_on_a_pr_no_longer_skips_its_automerge_evaluation():
    # The other family of skipped `continue`s: a DISPATCH used to skip 3c for the
    # same PR. The lock must bound only the dispatch, never the (cheap) arming
    # evaluation — idle and busy agree on 3c's outcome for the same world.
    #
    # A CONFLICTING PR is rebased but NOT armed (changed 2026-07-26): GitHub
    # refuses to arm a DIRTY PR, and the old arm-while-conflicted behavior only
    # looked like it worked because the unconditional marker write hid the
    # refusal — while actually suppressing the REAL arm forever (#537). Post-fix
    # the arm re-fires on the first poll after mergeability returns, so the only
    # cost is one poll of latency after the rebase push; the old behavior was a
    # gh call + refusal EVERY poll on repos without protected-branch rules.
    # A MERGEABLE twin proves the arming itself is lock-independent.
    p = pr(100, "ai-pr-open", mergeable="CONFLICTING",
           reviews=(Review("APPROVED", "review-bot", "t1"),),
           checks=(Check("build", bucket="pass"),))
    idle = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    busy = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=True)
    assert RunAgent(AgentKind.REBASE, 100) in _run_agents(idle)
    assert _run_agents(busy) == []                        # the lock still bounds dispatch
    assert not any(isinstance(action, EnableAutoMerge) for action in idle)
    assert not any(isinstance(action, EnableAutoMerge) for action in busy)

    clean = pr(100, "ai-pr-open", mergeable="MERGEABLE",
               head_ref_oid="head-100",
               reviews=(
                   Review(
                       "APPROVED",
                       "review-bot",
                       "t1",
                       commit_id="head-100",
                   ),
               ),
               checks=(Check("build", bucket="pass"),))
    for busy_flag in (False, True):
        actions = decide_fix(world(prs=[clean]), CFG, FixState(), fixer_busy=busy_flag)
        assert EnableAutoMerge(100, "squash", "head-100") in actions


def test_blocked_pr_is_never_armed_in_either_lock_state():
    # Anti-vacuity twin for the split: 3c is not "arm everything now". A PR whose
    # next move belongs to a human (ai-blocked / needs-human-review) is skipped —
    # in BOTH lock states, so the skip is a property of the world, not the lock.
    w = world(prs=[_issueless_approved_green_pr(), pr(
        114, "ai-pr-open", "needs-human-review", head_ref_name="feat/ACME-8-x",
        reviews=(Review("APPROVED", "review-bot", "t1"),),
        checks=(Check("build", bucket="pass"),),
    )])
    for busy in (False, True):
        actions = decide_fix(w, CFG, FixState(), fixer_busy=busy)
        assert EnableAutoMerge(113, "squash", "head-113") in actions
        assert not any(
            isinstance(action, EnableAutoMerge) and action.pr == 114
            for action in actions
        )


# ── changes-requested → address-review; failure → fix-ci ───────────────────────
def test_changes_requested_dispatches_address_review():
    p = pr(100, "ai-pr-open", reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(actions)


def test_human_authored_pr_is_not_driven_by_fixer():
    """A PR the reviewer ADOPTED (labelled ai-pr-open) but that a HUMAN opened must
    NOT be picked up by the fixer: it has no fixer worktree/session, so ADDRESS_REVIEW
    does no work yet burns a leg and mislabels the PR (observed: #351, opened by a
    human, review-adopted, then fixer-commented + ai-blocked). Regression guard."""
    p = pr(100, "ai-pr-open", author="some-human",
           reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []  # fixer leaves the human's PR alone


def test_human_authored_conflicting_pr_is_not_rebased_by_fixer():
    """Same gate on the REBASE path — a human's conflicting PR is theirs to rebase."""
    p = pr(100, "ai-pr-open", author="some-human", mergeable="CONFLICTING")
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []


def test_unknown_author_pr_is_not_driven_by_fixer():
    """Fail-safe: a PR with an unknown (empty) author is treated as NOT-ours — never
    act on a PR we cannot confirm we authored."""
    p = pr(100, "ai-pr-open", author="",
           reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []


BLANK_BOT_CFG = FixConfig(fix_bot="")


def test_unknown_author_is_not_ours_even_when_fix_bot_is_also_unset():
    """The fail-safe above INVERTED when both sides were blank: `login_eq("", "")`
    is True, so an unset FIX_BOT plus a PR whose author didn't resolve read as the
    fixer's OWN PR — a stranger's PR driven by the fixer, the exact #351 shape the
    authorship gate exists to prevent. Blank never matches, in either position."""
    p = pr(100, "ai-pr-open", author="",
           reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    assert _run_agents(decide_fix(world(prs=[p]), BLANK_BOT_CFG, FixState(), fixer_busy=False)) == []


def test_unset_fix_bot_claims_no_pr_at_all():
    """The other direction: an unset FIX_BOT owns nothing, even a PR whose author
    IS resolvable. A misconfigured (empty) FIX_BOT must disable the fixer's PR legs,
    not hand it every PR it can see."""
    p = pr(100, "ai-pr-open", author="fix-bot",
           reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    assert _run_agents(decide_fix(world(prs=[p]), BLANK_BOT_CFG, FixState(), fixer_busy=False)) == []
    # Anti-vacuity twin: the SAME PR under a configured fixer still dispatches, so
    # the empty lists above are the blank guard, not a broken world fixture.
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(
        decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    )


# ── review C1: the `[bot]` suffix must not disable the fixer ──────────────────
# An operator putting a GitHub App on the fixer writes FIX_BOT='fix-bot[bot]',
# but gh reports the PR author bare as `fix-bot`. Under a raw `==` the authorship
# filter above matched NOTHING, so address-review / fix-ci / rebase / auto-merge
# never dispatched — forever, with no error.
SUFFIXED_CFG = FixConfig(fix_bot="fix-bot[bot]", )


def test_c1_suffixed_fix_bot_still_addresses_review_on_its_own_pr():
    p = pr(100, "ai-pr-open", author="fix-bot",
           reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(world(prs=[p]), SUFFIXED_CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(actions)


def test_c1_suffixed_fix_bot_still_fixes_ci_on_its_own_pr():
    p = pr(100, "ai-pr-open", author="fix-bot", checks=(Check("unit", bucket="fail"),))
    actions = decide_fix(world(prs=[p]), SUFFIXED_CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


def test_c1_suffixed_fix_bot_still_rebases_its_own_conflicting_pr():
    p = pr(100, "ai-pr-open", author="fix-bot", mergeable="CONFLICTING")
    actions = decide_fix(world(prs=[p]), SUFFIXED_CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.REBASE, 100) in _run_agents(actions)


def test_c1_bare_fix_bot_matches_a_suffixed_pr_author():
    # The asymmetry runs both ways: the config may be bare while the API reports
    # the App's login suffixed.
    p = pr(100, "ai-pr-open", author="fix-bot[bot]",
           reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(actions)


def test_c1_suffixed_fix_bot_still_leaves_a_humans_pr_alone():
    # Anti-vacuity twin: the authorship gate must still EXCLUDE strangers — the
    # suffix tolerance is not "match anything".
    p = pr(100, "ai-pr-open", author="some-human",
           reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(world(prs=[p]), SUFFIXED_CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []


def test_c1_suffixed_fix_bot_does_not_treat_its_own_approval_as_independent():
    # `latest_other_review` under the same asymmetry: the fixer's own APPROVE,
    # reported bare, must not satisfy the distinct-approver requirement.
    p = pr(100, "ai-pr-open", author="fix-bot",
           reviews=(Review("APPROVED", "fix-bot", "t1"),),
           checks=(Check("build", bucket="pass"),))
    assert not should_automerge(p, FixState(), SUFFIXED_CFG)
    # Anti-vacuity twin: a genuinely distinct approver still arms auto-merge.
    q = pr(101, "ai-pr-open", author="fix-bot",
           head_ref_oid="head-101",
           reviews=(
               Review(
                   "APPROVED",
                   "review-bot",
                   "t1",
                   commit_id="head-101",
               ),
           ),
           checks=(Check("build", bucket="pass"),))
    assert should_automerge(q, FixState(), SUFFIXED_CFG)


def test_c1_suffixed_fix_bot_does_not_read_its_own_comment_as_a_human_reply():
    # `last_human_reply` under the same asymmetry: the fixer's own bare-login
    # comment must not count as a new human reply, or one unanswered question
    # re-fires a paid RESUME session every poll.
    iss = issue(7, "ai-blocked", comments=(Comment("fix-bot", "asking", "2026-10-01T00:00:01Z"),))
    st = _with_session()
    assert issues_needing_resume(world(issues=[iss]), st, SUFFIXED_CFG) == []
    # Anti-vacuity twin: a real human's reply on the same world still resumes.
    iss2 = issue(7, "ai-blocked", comments=(
        Comment("fix-bot", "asking", "2026-10-01T00:00:01Z"), Comment("alice", "here you go", "2026-10-01T00:00:02Z"),
    ))
    assert issues_needing_resume(world(issues=[iss2]), st, SUFFIXED_CFG) == [(7, "2026-10-01T00:00:02Z")]


def test_ci_failure_dispatches_fix_ci():
    p = pr(100, "ai-pr-open", checks=(Check("unit", bucket="fail"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


def test_ci_pending_does_not_dispatch_fix_ci():
    # pending beats failure → no fix-ci misfire.
    p = pr(100, "ai-pr-open", checks=(Check("unit", bucket="fail"), Check("e2e", state="QUEUED")))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []


def test_blocked_pr_is_left_alone():
    p = pr(100, "ai-pr-open", "ai-blocked",
           reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []


# ── circuit breakers ────────────────────────────────────────────────────────────
def test_breaker_tripped_boundary():
    assert breaker_tripped(2, 3) is False   # under cap
    assert breaker_tripped(3, 3) is True    # at cap
    assert breaker_tripped(4, 3) is True    # over cap
    assert breaker_tripped(99, 0) is False  # cap<=0 disables the breaker


def _changes_requested_pr():
    # closing_issue links the PR to issue 9, so the per-issue retry count is read.
    return pr(100, "ai-pr-open", closing_issue=9,
              reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1"),))


def _ci_fail_pr():
    return pr(100, "ai-pr-open", closing_issue=9, checks=(Check("unit", bucket="fail"),))


def test_address_review_under_cap_still_dispatches():
    st = FixState(review_retries={9: ("t1", 2)})  # counted against THIS review
    actions = decide_fix(world(prs=[_changes_requested_pr()]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(actions)


def test_address_review_at_cap_escalates_not_dispatches():
    st = FixState(review_retries={9: ("t1", 3)})  # cap is 3 (default)
    actions = decide_fix(world(prs=[_changes_requested_pr()]), CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []  # no fixer dispatch
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions
    assert any(isinstance(a, PostComment) and a.number == 100 for a in actions)


def test_address_review_counter_from_older_review_reads_as_zero():
    # The counter is keyed to the review it accumulated against: a count stored
    # under an OLDER review's timestamp must not block a NEW review (supersession
    # is the only reset — see FixState.review_retries).
    st = FixState(review_retries={9: ("t0", 3)})  # at cap, but for review t0
    actions = decide_fix(world(prs=[_changes_requested_pr()]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(actions)


# ── ADDRESS_REVIEW supersession gate (V2/#64) ────────────────────────────────
def _reviewed_pr(commit_id: str, head_oid: str, checks=()):
    return pr(100, "ai-pr-open", closing_issue=9, head_ref_oid=head_oid,
              checks=tuple(checks),
              reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1",
                              commit_id=commit_id),))


def test_superseded_review_not_redispatched():
    # Head moved past the reviewed commit: the fixer already answered; the next
    # move is the reviewer's. Re-dispatching here was the V2 full-cost loop.
    w = world(prs=[_reviewed_pr(commit_id="aaa", head_oid="bbb")])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []


def test_superseded_review_still_allows_ci_fix():
    # While awaiting re-review, a CI failure on the NEW head is still ours.
    # The free re-run (#38) is already spent on this head, so the question the
    # test is actually about — does a superseded review suppress the CI leg? —
    # is asked of the PAID dispatch rather than of the flake probe in front of it.
    w = world(prs=[_reviewed_pr(commit_id="aaa", head_oid="bbb",
                                checks=[Check("unit", bucket="fail")])])
    state = FixState(ci_rerun_head={100: "bbb"})
    actions = decide_fix(w, CFG, state, fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


def test_review_on_current_head_dispatches():
    w = world(prs=[_reviewed_pr(commit_id="aaa", head_oid="aaa")])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(actions)


def test_review_with_unknown_commit_dispatches():
    # commit_id unknown → fail open to dispatch; the attempt counter is the
    # backstop against looping.
    w = world(prs=[_reviewed_pr(commit_id="", head_oid="bbb")])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(actions)


# ── SQLite-owned resolve gates ──────────────────────────────────────────────
def test_resolve_under_cap_dispatches():
    st = FixState(resolve_workflows={5: _workflow(5, failures=2)})
    actions = decide_fix(world(issues=[issue(5, "ai-ready")]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 5, workflow=_workflow(5).binding) in _run_agents(actions)


@pytest.mark.parametrize("labels", [("ai-ready",), ("ai-ready", "needs-human-review")])
def test_remote_relabel_cannot_rearm_an_exhausted_episode(labels):
    st = FixState(resolve_workflows={5: _workflow(5, failures=3, held=True)})
    actions = raw_decide_fix(world(issues=[issue(5, *labels)]), CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []
    projections = [a for a in actions if isinstance(a, Relabel)]
    assert bool(projections) == ("needs-human-review" not in labels)
    assert all(a.add is Label.NEEDS_HUMAN_REVIEW and a.workflow == st.resolve_workflows[5].binding for a in projections)


@pytest.mark.parametrize("authority", ["missing", "absent", "unavailable", "gated"])
@pytest.mark.parametrize("label", ["ai-ready", "ai-blocked"])
def test_resolve_and_resume_require_available_ungated_authority(authority, label):
    snapshot = _workflow(7, session=True, failures=3, held=authority == "gated")
    reads = {
        "missing": {},
        "absent": {7: WorkflowAbsent(snapshot.revision.fence.key)},
        "unavailable": {7: WorkflowUnavailable(snapshot.revision.fence.key, "corrupt workflow row")},
        "gated": {7: snapshot},
    }
    observed = world(issues=[issue(7, label, comments=(Comment("alice", "continue", "t2"),))])
    state = FixState(sessions=frozenset({7}), resolve_workflows=reads[authority])
    assert _run_agents(raw_decide_fix(observed, CFG, state, fixer_busy=False)) == []


@pytest.mark.parametrize("unavailable", [False, True])
def test_held_p1_cannot_starve_healthy_p2(unavailable):
    held = _workflow(1, failures=3, held=True)
    healthy = _workflow(2)
    state = FixState(resolve_workflows={
        1: WorkflowUnavailable(held.revision.fence.key, "invalid row") if unavailable else held,
        2: healthy,
    })
    observed = world(issues=[issue(1, "ai-ready", "P1"), issue(2, "ai-ready", "P2")])
    actions = raw_decide_fix(observed, CFG, state, fixer_busy=False)
    assert _run_agents(actions) == [RunAgent(AgentKind.RESOLVE, 2, workflow=healthy.binding)]


def test_issue_resume_ignores_legacy_session_without_sql_continuation():
    observed = world(issues=[_blocked_issue_with_reply()])
    state = FixState(sessions=frozenset({7}), resolve_workflows={7: _workflow(7)})
    assert issues_needing_resume(observed, state, CFG) == []
    assert issues_needing_any_resume(observed, state, CFG) == [(7, "2026-10-01T00:00:01Z")]


@pytest.mark.parametrize("kind,label", [(AgentKind.RESOLVE, "ai-ready"), (AgentKind.RESUME, "ai-blocked")])
def test_issue_claim_and_dispatch_capture_exact_observed_generation(kind, label):
    original = _workflow(7)
    current = replace(original, revision=WorkflowRevision(replace(original.revision.fence, generation=9), 12))
    observed = world(issues=[issue(7, label, updated_at="revision-7", comments=(Comment("alice", "continue", "2026-10-01T00:00:02Z"),))])
    actions = raw_decide_fix(observed, CFG, FixState(resolve_workflows={7: current}), fixer_busy=False)
    assert _run_agents(actions) == [RunAgent(kind, 7, workflow=current.binding)]
    claim = next(action for action in actions if isinstance(action, Relabel))
    assert claim.workflow == current.binding
    assert claim.expected_revision == "revision-7"
    assert _run_agents(actions)[0].requires == claim


@pytest.mark.parametrize("kind", [AgentKind.FIX_CI, AgentKind.ADDRESS_REVIEW, AgentKind.REBASE, AgentKind.PR_RESUME])
def test_pr_repair_retains_pr_authority_without_issue_workflow_binding(kind):
    variants = {
        AgentKind.FIX_CI: pr(100, "ai-pr-open", checks=(Check("unit", bucket="fail"),)),
        AgentKind.ADDRESS_REVIEW: pr(100, "ai-pr-open", reviews=(Review("CHANGES_REQUESTED", "review-bot", "2026-10-01T00:00:01Z"),)),
        AgentKind.REBASE: pr(100, "ai-pr-open", mergeable="CONFLICTING"),
        AgentKind.PR_RESUME: pr(100, "ai-pr-open", "ai-blocked", closing_issue=7, comments=(Comment("alice", "continue", "2026-10-01T00:00:02Z"),)),
    }
    state = FixState(sessions=frozenset({7}))
    actions = raw_decide_fix(world(prs=[variants[kind]]), CFG, state, fixer_busy=False)
    assert _run_agents(actions) == [RunAgent(kind, 100)]
    assert all(action.workflow is None for action in actions if isinstance(action, (RunAgent, Relabel, PostComment)))


@pytest.mark.parametrize("unavailable", [False, True])
def test_missing_cleanup_authority_cannot_emit_unbound_issue_leaf_effects(unavailable):
    current = _workflow(7)
    reads = {7: WorkflowUnavailable(current.revision.fence.key, "invalid authority")} if unavailable else {}
    observed = world(issues=[issue(7, "ai-pr-open")], prs=[pr(100, "ai-pr-open", state="MERGED", closing_issue=7)])
    actions = raw_decide_fix(observed, CFG, FixState(resolve_workflows=reads), fixer_busy=False)
    assert not any(isinstance(action, CloseIssue) or (
        isinstance(action, (Relabel, PostComment)) and action.target is Target.ISSUE
    ) for action in actions)


def test_issue_cleanup_leaf_effects_capture_existing_workflow_binding():
    current = _workflow(7)
    observed = world(issues=[issue(7, "ai-pr-open")], prs=[pr(100, "ai-pr-open", state="MERGED", closing_issue=7)])
    actions = raw_decide_fix(observed, CFG, FixState(resolve_workflows={7: current}), fixer_busy=False)
    effects = [action for action in actions if isinstance(action, CloseIssue) or (
        isinstance(action, (Relabel, PostComment)) and action.target is Target.ISSUE
    )]
    assert any(isinstance(action, CloseIssue) for action in effects)
    assert any(isinstance(action, Relabel) for action in effects)
    assert all(action.workflow == current.binding for action in effects)


def test_fix_ci_at_cap_escalates_not_dispatches():
    st = FixState(ci_retries={9: 3})
    actions = decide_fix(world(prs=[_ci_fail_pr()]), CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


def test_fix_ci_under_cap_still_dispatches():
    st = FixState(ci_retries={9: 2})
    actions = decide_fix(world(prs=[_ci_fail_pr()]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


# ── the CI breaker's counter is per-EPISODE, not per-issue lifetime ──────────
# The breaker's escalation text is "CI fix failed N× without going green", but
# the counter was lifetime-per-issue: a PR that DID go green kept its spent
# budget, so a later unrelated red episode (base drift, a new review round's
# push) could escalate on its first repair attempt (keifu PR #181 diagnosis,
# 2026-08-03). Settled green closes the episode by emitting ResetCiRetries.
def _ci_green_pr():
    return pr(100, "ai-pr-open", closing_issue=9, checks=(Check("unit", bucket="pass"),))


def test_settled_green_closes_the_red_ci_episode():
    st = FixState(ci_retries={9: 2})
    actions = decide_fix(world(prs=[_ci_green_pr()]), CFG, st, fixer_busy=False)
    assert ResetCiRetries(9) in actions


def test_green_with_an_unspent_counter_writes_nothing():
    # anti-churn twin: without this, every poll of every green PR would issue a
    # redundant reset write.
    actions = decide_fix(world(prs=[_ci_green_pr()]), CFG, FixState(ci_retries={}),
                         fixer_busy=False)
    assert not any(isinstance(a, ResetCiRetries) for a in actions)


def test_red_episode_after_a_green_one_is_repaired_not_escalated():
    # The class itself: state as it stands AFTER the reset above. A fresh
    # failure buys a fresh repair budget instead of inheriting the spent one.
    st = FixState(ci_retries={9: 0})
    actions = decide_fix(world(prs=[_ci_fail_pr()]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)
    assert not any(isinstance(a, Relabel) and a.add == Label.NEEDS_HUMAN_REVIEW
                   for a in actions)
    # The breaker still bites at the cap — see
    # test_fix_ci_at_cap_escalates_not_dispatches above; the reset relaxes WHEN
    # the count starts, never the ceiling.


def test_pending_checks_are_neither_green_nor_red_at_the_cap():
    # PENDING outranks a sibling FAILURE (#49): no escalation (the verdict
    # isn't in), no FIX_CI (nothing settled to repair), and no reset (pending
    # is not green — resetting here would refund the budget mid-episode).
    p = pr(100, "ai-pr-open", closing_issue=9,
           checks=(Check("unit", bucket="fail"), Check("e2e", state="QUEUED")))
    actions = decide_fix(world(prs=[p]), CFG, FixState(ci_retries={9: 3}), fixer_busy=False)
    assert _run_agents(actions) == []
    assert not any(isinstance(a, Relabel) and a.add == Label.NEEDS_HUMAN_REVIEW
                   for a in actions)
    assert not any(isinstance(a, ResetCiRetries) for a in actions)


# ── machine-failure backoff ──────────────────────────────────────────────────
# A machine failure is never an attempt and never escalates; the next dispatch
# of the same kind for the same item waits out the backoff derived from the
# ended legs (domain.backoff). Only the money caps bound the retries.
_DIED = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _stamp(after: timedelta) -> str:
    return (_DIED + after).isoformat()


def _machine_deaths(issue_number, kind, n):
    return {issue_number: tuple(LegEnd(kind, _DIED - timedelta(minutes=5 * i), True) for i in range(n))}


def _blocked_issue_with_reply():
    return issue(7, "ai-blocked", comments=(Comment("alice", "more context", "2026-10-01T00:00:01Z"),))


_ARMS = {
    AgentKind.RESOLVE: (lambda: world(issues=[issue(5, "ai-ready")]), 5, lambda ends: FixState(leg_ends=ends),
                        RunAgent(AgentKind.RESOLVE, 5, workflow=_workflow(5).binding)),
    AgentKind.RESUME: (lambda: world(issues=[_blocked_issue_with_reply()]), 7, lambda ends: _with_session(leg_ends=ends),
                       RunAgent(AgentKind.RESUME, 7, workflow=_workflow(7).binding)),
    AgentKind.ADDRESS_REVIEW: (lambda: world(prs=[_changes_requested_pr()]), 9, lambda ends: FixState(leg_ends=ends),
                               RunAgent(AgentKind.ADDRESS_REVIEW, 100)),
    AgentKind.REBASE: (lambda: world(prs=[pr(100, "ai-pr-open", closing_issue=9, mergeable="CONFLICTING")]), 9,
                       lambda ends: FixState(leg_ends=ends), RunAgent(AgentKind.REBASE, 100)),
    AgentKind.FIX_CI: (lambda: world(prs=[_ci_fail_pr()]), 9, lambda ends: FixState(leg_ends=ends),
                       RunAgent(AgentKind.FIX_CI, 100)),
}


def _decide_at(kind, ends, after):
    build_world, _, build_state, _ = _ARMS[kind]
    return decide_fix(replace(build_world(), now=_stamp(after)), CFG, build_state(ends), fixer_busy=False)


def _escalated(actions):
    return any(isinstance(a, Relabel) and a.add is Label.NEEDS_HUMAN_REVIEW for a in actions)


@pytest.mark.parametrize("kind", list(_ARMS))
def test_machine_death_redispatches_only_after_the_backoff(kind):
    _, number, _, expected = _ARMS[kind]
    ends = _machine_deaths(number, kind, 1)

    waiting = _decide_at(kind, ends, timedelta(seconds=59))
    resumed = _decide_at(kind, ends, timedelta(seconds=60))

    assert _run_agents(waiting) == [] and not _escalated(waiting)
    assert expected in _run_agents(resumed)


@pytest.mark.parametrize("kind", list(_ARMS))
def test_machine_deaths_never_escalate_however_many(kind):
    _, number, _, expected = _ARMS[kind]
    ends = _machine_deaths(number, kind, 500)

    held = _decide_at(kind, ends, timedelta(minutes=59))
    after = _decide_at(kind, ends, timedelta(hours=1))

    assert _run_agents(held) == [] and not _escalated(held)
    assert expected in _run_agents(after) and not _escalated(after)


def test_backoff_is_per_kind():
    ends = _machine_deaths(9, AgentKind.FIX_CI, 3)
    actions = _decide_at(AgentKind.ADDRESS_REVIEW, ends, timedelta(seconds=1))
    assert RunAgent(AgentKind.ADDRESS_REVIEW, 100) in _run_agents(actions)


def test_unstamped_world_cannot_prove_a_backoff_elapsed():
    ends = _machine_deaths(5, AgentKind.RESOLVE, 1)
    actions = decide_fix(world(issues=[issue(5, "ai-ready")]), CFG, FixState(leg_ends=ends), fixer_busy=False)
    assert _run_agents(actions) == []


def test_agent_attempt_end_dispatches_without_backoff():
    ends = {5: (LegEnd(AgentKind.RESOLVE, _DIED, False),)}
    actions = _decide_at(AgentKind.RESOLVE, ends, timedelta(seconds=1))
    assert RunAgent(AgentKind.RESOLVE, 5, workflow=_workflow(5).binding) in _run_agents(actions)


@pytest.mark.parametrize("kind", list(_ARMS))
def test_unreadable_leg_history_holds_every_leg_of_that_issue(kind):
    build_world, number, build_state, expected = _ARMS[kind]
    state = replace(build_state({}), leg_history_errors={number: "issue recovery authority is inactive"})
    held = decide_fix(replace(build_world(), now=_stamp(timedelta(days=1))), CFG, state, fixer_busy=False)
    other = replace(build_state({}), leg_history_errors={number + 1000: "unrelated"})
    free = decide_fix(replace(build_world(), now=_stamp(timedelta(days=1))), CFG, other, fixer_busy=False)
    assert _run_agents(held) == [] and not _escalated(held)
    assert expected in _run_agents(free)


# ── reap ────────────────────────────────────────────────────────────────────────
# Every reap relabel binds expected_revision to the OBSERVED issue's updated_at:
# a leg finishing between observation and write bumps updated_at with its own
# relabel, so the stale repair is discarded instead of resurrecting ai-ready
# next to the leg's fresh label (keifu #18/#25 dual-label race, 2026-08-02).
_REV = "2026-08-02T00:00:00Z"


def test_contradictory_ready_plus_pr_open_repairs_to_pr_open_only():
    # #185 (tauri #482, 2026-08-09): ai-ready + ai-pr-open coexisted for 3+
    # hours — every tick considered dispatch, the dup-PR guard refused, and
    # the standoff read as a level-6 stall and a phantom "pending" row. With
    # the open PR confirmed in the same snapshot, the stale ai-ready is
    # removed (remove-only: the true state is already on the issue), CAS-bound
    # to the observed revision so a mid-transition observation is discarded.
    w = world(
        issues=[issue(482, "ai-ready", "ai-pr-open", updated_at=_REV)],
        prs=[pr(620, "ai-pr-open", head_ref_name="fix/482-backoff", closing_issue=482)],
    )
    assert Relabel(Target.ISSUE, 482, Label.AI_READY, None, expected_revision=_REV) in label_repair_actions(w)
    # ...and decide_fix emits it lock-free even with the fixer busy.
    actions = decide_fix(w, CFG, FixState(), fixer_busy=True, dispatch_slots=0)
    assert Relabel(Target.ISSUE, 482, Label.AI_READY, None, expected_revision=_REV, workflow=_workflow(482).binding) in actions


def test_no_repair_without_an_open_pr_or_without_the_pair():
    # ai-ready + ai-pr-open with NO open PR is closed-PR-cleanup's state, not
    # a repair target; single-label issues are untouched (anti-vacuity).
    orphan_pair = world(
        issues=[issue(7, "ai-ready", "ai-pr-open", updated_at=_REV)]
    )
    assert label_repair_actions(orphan_pair) == []
    healthy = world(
        issues=[
            issue(8, "ai-ready", updated_at=_REV),
            issue(9, "ai-pr-open", updated_at=_REV),
        ],
        prs=[pr(90, "ai-pr-open", closing_issue=9)],
    )
    assert label_repair_actions(healthy) == []


def test_a_closing_ref_alone_is_not_evidence_for_a_repair():
    # #185 review F4: `closing_refs` over-inclusiveness (any author, possibly
    # stale) is fail-safe for the reaper, which does LESS on a false positive.
    # A repair does MORE — it removes a label — so a repo-wide closing ref
    # with no in-snapshot OPEN PR must not fire it: a half-landed requeue plus
    # a stale/foreign ref would strand the issue at ai-pr-open forever.
    w = world(
        issues=[issue(500, "ai-ready", "ai-pr-open", updated_at=_REV)],
        closing_refs=[closing_ref(700, 500, author="human")],
    )
    assert label_repair_actions(w) == []


def test_a_repaired_issue_is_not_claimed_or_dispatched_in_the_same_tick():
    # #185 review F2: the repaired issue still LOOKS ai-ready this tick, and
    # is_ready does not exclude ai-pr-open — without the guard, decide_fix
    # emitted the repair AND a RESOLVE claim CAS-bound to the same revision,
    # leaving a paid duplicate dispatch to lose a write race instead of being
    # refused outright.
    w = world(
        issues=[issue(482, "ai-ready", "ai-pr-open", updated_at=_REV)],
        prs=[pr(620, "ai-pr-open", head_ref_name="fix/482-backoff")],
    )
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False, dispatch_slots=1)
    assert Relabel(Target.ISSUE, 482, Label.AI_READY, None, expected_revision=_REV, workflow=_workflow(482).binding) in actions
    assert RunAgent(AgentKind.RESOLVE, 482, workflow=_workflow(482).binding) not in _run_agents(actions)
    assert not any(
        isinstance(a, Relabel) and a.number == 482 and a.add is not None
        for a in actions
    )


def test_a_repair_preempts_the_duplicate_pr_guard_for_the_same_issue():
    # #185 review F3: repair + dup guard co-firing stacked two Relabels on the
    # same label in one tick and parked the issue at ai-blocked + ai-pr-open —
    # a NEW contradictory pair minted by the repair feature. The repair wins;
    # the guard's real outcome (no dispatch this tick) is kept via `guarded`.
    w = world(
        issues=[issue(500, "ai-ready", "ai-pr-open", updated_at=_REV)],
        prs=[pr(700, "ai-pr-open", head_ref_name="fix/500-x")],
        closing_refs=[closing_ref(701, 500, author="human")],
    )
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False, dispatch_slots=1)
    relabels = [
        a for a in actions
        if isinstance(a, Relabel) and a.number == 500
    ]
    assert relabels == [
        Relabel(Target.ISSUE, 500, Label.AI_READY, None, expected_revision=_REV, workflow=_workflow(500).binding)
    ]
    assert not any(
        isinstance(a, PostComment) and a.number == 500 for a in actions
    )
    assert RunAgent(AgentKind.RESOLVE, 500, workflow=_workflow(500).binding) not in _run_agents(actions)


def test_reap_requeues_orphaned_issue_to_ready():
    w = world(issues=[issue(8, "ai-working", updated_at=_REV)])
    assert Relabel(
        Target.ISSUE, 8, Label.AI_WORKING, Label.AI_READY, expected_revision=_REV
    ) in reap_actions(w)


def test_reap_relabel_fails_closed_without_an_observed_revision():
    # An issue observed WITHOUT updated_at must carry "" (fail-closed contract
    # on Relabel.expected_revision), never None — None would let the command
    # layer mint a fresh revision at write time, a self-fulfilling CAS that
    # cannot catch the decision going stale.
    [action] = reap_actions(world(issues=[issue(8, "ai-working")]))
    assert action.expected_revision == ""


def test_reap_protects_only_active_issues_while_pool_is_occupied():
    w = world(issues=[
        issue(8, "ai-working", updated_at=_REV),
        issue(9, "ai-working", updated_at=_REV),
    ])

    actions = decide_fix(
        w,
        CFG,
        FixState(inflight=(InFlightFix(8, frozenset({"src/a.py"})),)),
        fixer_busy=True,
        dispatch_slots=0,
    )

    reaped = {a.number for a in actions if isinstance(a, Relabel)
              and a.remove is Label.AI_WORKING and a.add is Label.AI_READY}
    assert 8 not in reaped
    assert 9 in reaped


def test_reap_ignores_prs_entirely():
    # review M22/D9: the PR arm is deleted, not merely unused. Nothing in the SDK
    # port adds ai-working to a PR, so the arm could only ever fire on a label a
    # HUMAN applied by hand — a reaper that "protects" a busy marker no writer
    # ever sets is a mechanism that reads live and isn't.
    assert reap_actions(world(prs=[pr(101, "ai-working")])) == []
    # Anti-vacuity twin: the ISSUE arm on the same world still fires, so an empty
    # list here means "PRs are out of scope", not "reap_actions stopped working".
    assert reap_actions(world(
        issues=[issue(8, "ai-working", updated_at=_REV)],
        prs=[pr(101, "ai-working")],
    )) == [
        Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_READY,
                expected_revision=_REV)
    ]


# re-review P0R-5: the reaper must never send an issue that ALREADY has an open
# PR back through RESOLVE — that opens a second PR for work already up, and the
# resolve leg has no counter on its success path, so nothing bounds the repeat.
def test_reap_repairs_to_pr_open_when_the_issue_already_has_an_open_pr():
    w = world(
        issues=[issue(8, "ai-working", updated_at=_REV)],
        prs=[pr(50, "ai-pr-open", closing_issue=8)],
    )
    assert reap_actions(w) == [
        Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_PR_OPEN,
                expected_revision=_REV)
    ]


def test_reap_repairs_via_branch_linkage_not_just_closing_ref():
    # The PR that a died-before-relabel session opened often has no closing-ref
    # yet (the body edit is a separate call), so branch linkage is the case that
    # actually fires in the incident this guards.
    w = world(
        issues=[issue(8, "ai-working", updated_at=_REV)],
        prs=[pr(50, head_ref_name="fix/8-thing")],
    )
    assert reap_actions(w) == [
        Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_PR_OPEN,
                expected_revision=_REV)
    ]


def test_reap_repairs_from_a_closing_ref_when_the_pr_is_parked():
    # `world.prs` carries only OPEN PRs wearing ai-pr-open (fresh-label
    # authority), so parking a PR — removing ai-pr-open — drops it from that
    # set. Without the closing_refs union the reaper requeued to ai-ready and
    # bought a duplicate RESOLVE plus a second PR (keifu #179 park, 2026-08-03).
    w = world(
        issues=[issue(34, "ai-working", updated_at=_REV)],
        closing_refs=[closing_ref(179, 34)],
    )
    assert reap_actions(w) == [
        Relabel(Target.ISSUE, 34, Label.AI_WORKING, Label.AI_PR_OPEN,
                expected_revision=_REV)
    ]


def test_reap_still_requeues_when_the_issues_pr_is_closed():
    # Anti-vacuity twin: a CLOSED PR is not work-already-up, so ai-ready remains
    # correct — otherwise this "fix" would strand every issue whose PR a human
    # closed.
    w = world(
        issues=[issue(8, "ai-working", updated_at=_REV)],
        prs=[pr(50, closing_issue=8, state="CLOSED")],
    )
    assert reap_actions(w) == [
        Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_READY,
                expected_revision=_REV)
    ]


def test_reap_does_not_pay_for_a_second_resolve_when_the_relabel_keeps_failing():
    # The loop the re-review reproduced, at the decide level: an issue whose
    # ai-working → ai-pr-open follow-up never landed (swallowed write) is
    # observed with an open PR. Before P0R-5 this world requeued to ai-ready and
    # the NEXT poll dispatched a full paid RESOLVE. Assert on the money: no
    # RunAgent for issue 8 anywhere in the poll.
    w = world(
        issues=[issue(8, "ai-working", updated_at=_REV)],
        prs=[pr(50, "ai-pr-open", closing_issue=8, checks=(Check("unit", bucket="pass"),))],
    )
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_PR_OPEN,
                   expected_revision=_REV, workflow=_workflow(8).binding) in actions
    assert not [a for a in actions if isinstance(a, RunAgent) and a.number == 8]


# ── cleanup (merge close + re-queue) ────────────────────────────────────────────
def test_cleanup_merged_closes_issue():
    p = pr(100, "ai-pr-open", state="MERGED", head_ref_name="fix/9-x")
    acts = cleanup_actions(p, 9, CFG)
    assert Relabel(Target.ISSUE, 9, Label.AI_PR_OPEN, Label.AI_DONE) in acts
    assert any(isinstance(a, CloseIssue) and a.number == 9 for a in acts)
    assert RemoveWorktree(9) in acts
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, None) in acts


def test_cleanup_merged_respects_close_disabled():
    cfg = FixConfig(fix_bot="fix-bot", close_issue_on_merge=False)
    p = pr(100, "ai-pr-open", state="MERGED", head_ref_name="fix/9-x")
    acts = cleanup_actions(p, 9, cfg)
    assert not any(isinstance(a, CloseIssue) for a in acts)
    assert Relabel(Target.ISSUE, 9, Label.AI_PR_OPEN, Label.AI_DONE) in acts


def test_cleanup_closed_unmerged_requeues_issue():
    p = pr(100, "ai-pr-open", state="CLOSED", head_ref_name="fix/9-x")
    acts = cleanup_actions(p, 9, CFG)
    assert Relabel(Target.ISSUE, 9, Label.AI_PR_OPEN, Label.AI_READY) in acts


def test_cleanup_uses_closing_ref_over_branch():
    p = pr(100, "ai-pr-open", state="MERGED", head_ref_name="fix/9-x", closing_issue=42)
    acts = cleanup_actions(p, 42, CFG)
    assert any(isinstance(a, CloseIssue) and a.number == 42 for a in acts)


# ── cleanup emits ClearIssueState (review M4/D8) ────────────────────────────
def test_cleanup_merged_emits_full_clear_flag():
    # review M4/D8: merged cleanup must retire ALL issue state (incl.
    # infra-deaths, via Store.cleanup_pr's merged=True path) plus the PR's own
    # state (incl. the pr-N.automerge marker, the M4 drift fix).
    p = pr(100, "ai-pr-open", state="MERGED", head_ref_name="fix/9-x")
    acts = cleanup_actions(p, 9, CFG)
    clears = [a for a in acts if isinstance(a, ClearIssueState)]
    assert clears == [ClearIssueState(pr=100, issue=9, merged=True)]


def test_cleanup_closed_unmerged_emits_partial_clear_flag():
    # anti-vacuity twin: the SAME PR, only CLOSED instead of MERGED, must flip
    # the flag to merged=False so Store.cleanup_pr takes the conservative path
    # (keeps resolve-retries/infra-deaths — handover §5.7).
    p = pr(100, "ai-pr-open", state="CLOSED", head_ref_name="fix/9-x")
    acts = cleanup_actions(p, 9, CFG)
    clears = [a for a in acts if isinstance(a, ClearIssueState)]
    assert clears == [ClearIssueState(pr=100, issue=9, merged=False)]


def test_cleanup_issueless_pr_still_emits_clear_with_no_issue():
    # An issueless PR (no closing ref, no numeric branch) has no worktree/issue
    # to touch, but its own state (e.g. a stray automerge marker) still needs
    # retiring — cleanup_actions must not skip ClearIssueState just because
    # issue_number is None.
    p = pr(100, "ai-pr-open", state="MERGED", head_ref_name="feat/ACME-1-x")
    acts = cleanup_actions(p, None, CFG)
    clears = [a for a in acts if isinstance(a, ClearIssueState)]
    assert clears == [ClearIssueState(pr=100, issue=None, merged=True)]
    assert not any(isinstance(a, RemoveWorktree) for a in acts)


# ── P1R-15: cleanup must not stomp a dispatch made in the SAME poll ──────────
# Repro (no failure required): close a PR, then `issue-agent ready N`. The
# world snapshot then shows issue N as ai-ready (dispatchable) AND its own
# stale CLOSED PR still carrying ai-pr-open (not yet cleaned up). Before the
# gate, cleanup_actions unconditionally applied its issue-side arm — assuming
# the observed issue was still awaiting THIS PR's outcome — deleting the
# brand-new worktree the fresh dispatch just created, requeuing the issue that
# just got a new RunAgent, and wiping the session the SAME poll's dispatch
# path had just recorded.
def test_cleanup_does_not_stomp_a_fresh_dispatch_in_the_same_poll():
    stale_pr = pr(50, "ai-pr-open", state="CLOSED", closing_issue=8,
                  head_ref_name="fix/8-thing")
    w = world(issues=[issue(8, "ai-ready")], prs=[stale_pr])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)

    # the fresh dispatch happened
    assert Relabel(Target.ISSUE, 8, Label.AI_READY, Label.AI_WORKING, "", workflow=_workflow(8).binding) in actions
    assert RunAgent(AgentKind.RESOLVE, 8, workflow=_workflow(8).binding) in actions

    # …and cleanup's ISSUE-side arm must NOT fire on top of it
    assert RemoveWorktree(8) not in actions
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_READY, workflow=_workflow(8).binding) not in actions
    assert not any(isinstance(a, ClearIssueState) and a.issue == 8 for a in actions)

    # the PR-side arm (dropping ai-pr-open from the genuinely-dead PR) still
    # fires — that half of the world really is stale and safe to act on.
    assert Relabel(Target.PR, 50, Label.AI_PR_OPEN, None) in actions
    assert any(isinstance(a, ClearIssueState) and a.pr == 50 for a in actions)


def test_cleanup_issue_side_arm_still_fires_when_issue_genuinely_awaits_cleanup():
    # anti-vacuity twin: when the issue in the SAME snapshot still carries
    # ai-pr-open (the ordinary, non-racing case), the gate must not suppress
    # the real cleanup — same stale-PR world as above, only the issue's own
    # label differs.
    stale_pr = pr(50, "ai-pr-open", state="CLOSED", closing_issue=8,
                  head_ref_name="fix/8-thing")
    w = world(issues=[issue(8, "ai-pr-open")], prs=[stale_pr])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_READY, workflow=_workflow(8).binding) in actions
    assert RemoveWorktree(8) in actions
    assert ClearIssueState(pr=50, issue=8, merged=False) in actions


def test_cleanup_does_not_reclose_an_issue_github_already_closed():
    # #28: the world now also carries CLOSED ai-pr-open issues, so the ordinary
    # merge cleanup runs against an issue GitHub closed at merge time from the
    # mandatory `Fixes #n`. Closing it again is a wasted call and a duplicate
    # "Resolved by #50" comment — everything else in the arm must still fire.
    merged = pr(50, "ai-pr-open", state="MERGED", closing_issue=8,
                head_ref_name="fix/8-thing")
    w = world(issues=[issue(8, "ai-pr-open", state="closed")], prs=[merged])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=_workflow(8).binding) in actions
    assert RemoveWorktree(8) in actions
    assert ClearIssueState(pr=50, issue=8, merged=True) in actions
    assert not any(isinstance(a, CloseIssue) for a in actions)


def test_cleanup_still_closes_a_merged_issue_github_left_open():
    # Anti-vacuity twin: on a repo whose base branch is NOT the default, GitHub
    # does not auto-close, and the harness's own close is the only one there is.
    merged = pr(50, "ai-pr-open", state="MERGED", closing_issue=8,
                head_ref_name="fix/8-thing")
    w = world(issues=[issue(8, "ai-pr-open")], prs=[merged])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert any(isinstance(a, CloseIssue) and a.number == 8 for a in actions)


def test_cleanup_defers_active_issue_but_still_cleans_unrelated_terminal_pr():
    active = pr(
        50, "ai-pr-open", state="MERGED", closing_issue=8,
        head_ref_name="fix/8-thing",
    )
    unrelated = pr(
        51, "ai-pr-open", state="MERGED", closing_issue=9,
        head_ref_name="fix/9-thing",
    )
    w = world(
        issues=[
            issue(8, "ai-pr-open", state="closed"),
            issue(9, "ai-pr-open", state="closed"),
        ],
        prs=[active, unrelated],
    )

    actions = decide_fix(
        w,
        CFG,
        FixState(inflight=(InFlightFix(8, frozenset({"src/a.py"})),)),
        fixer_busy=True,
        dispatch_slots=0,
    )

    assert RemoveWorktree(8) not in actions
    assert not any(
        isinstance(action, ClearIssueState) and action.issue == 8
        for action in actions
    )
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=_workflow(8).binding) not in actions
    assert RemoveWorktree(9) in actions
    assert ClearIssueState(pr=51, issue=9, merged=True) in actions


def test_deferred_active_merge_cleanup_runs_after_worker_settles():
    merged = pr(
        50, "ai-pr-open", state="MERGED", closing_issue=8,
        head_ref_name="fix/8-thing",
    )
    w = world(
        issues=[issue(8, "ai-pr-open", state="closed")],
        prs=[merged],
    )

    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)

    assert RemoveWorktree(8) in actions
    assert ClearIssueState(pr=50, issue=8, merged=True) in actions
    assert Relabel(Target.PR, 50, Label.AI_PR_OPEN, None) in actions
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=_workflow(8).binding) in actions


# ── issue-driven cleanup: the PR is gone from the world entirely (#28) ───────
def test_orphaned_cleanup_repairs_a_closed_issue_with_no_covering_pr():
    w = world(issues=[issue(8, "ai-pr-open", state="closed")])
    assert orphaned_cleanup_actions(w) == [
        RemoveWorktree(8),
        Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE),
        ClearIssueState(pr=None, issue=8, merged=True),
    ]


def test_orphaned_cleanup_defers_an_active_issue():
    w = world(issues=[issue(8, "ai-pr-open", state="closed")])

    assert orphaned_cleanup_actions(
        w, active_issues=frozenset({8})
    ) == []


def test_orphaned_cleanup_ignores_an_issue_its_pr_still_covers():
    # An unlabelled (stale-index) PR does NOT cover; a labelled one does, and
    # belongs to the PR-driven arm. The two must never both fire on one issue.
    iss = issue(8, "ai-pr-open", state="closed")
    stale = pr(50, state="MERGED", closing_issue=8, head_ref_name="fix/8-x")
    covering = pr(50, "ai-pr-open", state="MERGED", closing_issue=8,
                  head_ref_name="fix/8-x")
    assert orphaned_cleanup_actions(world(issues=[iss], prs=[stale])) != []
    assert orphaned_cleanup_actions(world(issues=[iss], prs=[covering])) == []


def test_orphaned_cleanup_leaves_an_open_never_closed_issue_alone():
    # Indistinguishable from a healthy in-flight issue whose PR fell out of this
    # one world on a transient read — acting would delete a live worktree.
    w = world(issues=[issue(8, "ai-pr-open")])
    assert orphaned_cleanup_actions(w) == []


def test_orphaned_cleanup_repairs_a_human_reopen_without_closing_it():
    # stateReason proves the lifecycle already ended once, so this is safe to
    # repair — and ai-done is what makes the reopen legible to the steward's
    # recurrence reporter (open + ai-done + reopened).
    w = world(issues=[issue(8, "ai-pr-open", state_reason="reopened")])
    acts = orphaned_cleanup_actions(w)
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE) in acts
    assert not any(isinstance(a, CloseIssue) for a in acts)


def test_orphaned_cleanup_is_one_shot():
    # The repair's own relabel removes ai-pr-open, which is what drops the issue
    # out of both queries that feed this arm — no marker needed.
    healed = world(issues=[issue(8, "ai-done", state="closed")])
    assert orphaned_cleanup_actions(healed) == []


def test_cleanup_never_recloses_a_human_reopen():
    # The PR-driven arm, merged branch: without the guard `close_issue_on_merge`
    # would close an operator's reopen within one poll and erase the #10 signal.
    merged = pr(50, "ai-pr-open", state="MERGED", closing_issue=8,
                head_ref_name="fix/8-thing")
    w = world(issues=[issue(8, "ai-pr-open", state_reason="reopened")], prs=[merged])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE, workflow=_workflow(8).binding) in actions
    assert not any(isinstance(a, CloseIssue) for a in actions)


def test_cleanup_actions_direct_call_defaults_to_the_ordinary_case():
    # cleanup_actions is also called directly (existing tests above all do
    # this without knowing an issue's current label) — the default must keep
    # exercising the ordinary "issue genuinely awaits cleanup" path so none of
    # those pre-existing direct-call tests silently regressed to a no-op.
    p = pr(100, "ai-pr-open", state="MERGED", head_ref_name="fix/9-x")
    acts = cleanup_actions(p, 9, CFG)
    assert RemoveWorktree(9) in acts
    assert Relabel(Target.ISSUE, 9, Label.AI_PR_OPEN, Label.AI_DONE) in acts


# ── CI pending deadline (#30) ────────────────────────────────────────────────
# A check stuck IN_PROGRESS pins ci_state at PENDING forever, and PENDING
# outranks FAILURE, so no leg ever runs, no counter ever moves, and the PR
# quietly stops existing. These assert the deadline turns that into a visible
# escalation without disturbing a healthy long-running pipeline.

_STALL_T0 = "2026-07-28T12:00:00Z"


def _stall_now(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        + timedelta(seconds=seconds)
    ).isoformat()


def _wedged_pr(started_at: str = _STALL_T0, **kw):
    return pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa",
        checks=(
            Check("unit", bucket="pass", started_at=_STALL_T0),
            Check("Test (windows-latest)", state="IN_PROGRESS", started_at=started_at),
        ),
        **kw,
    )


def _escalated(actions):
    relabels = [
        a for a in actions
        if isinstance(a, Relabel) and a.add == Label.NEEDS_HUMAN_REVIEW
    ]
    relabels.extend(
        Relabel(Target.PR, a.pr, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW)
        for a in actions
        if isinstance(a, EscalateCiPending)
    )
    return relabels


def test_pending_past_deadline_escalates_the_pr_to_a_human():
    w = world(prs=[_wedged_pr()], now=_stall_now(3601))
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    relabels = _escalated(actions)
    assert [(r.target, r.number, r.remove) for r in relabels] == [
        (Target.PR, 100, Label.AI_PR_OPEN)
    ]
    # …and says why, naming the check, so the operator can go cancel it.
    escalations = [a for a in actions if isinstance(a, EscalateCiPending)]
    assert escalations and "Test (windows-latest)" in escalations[0].body


def test_pending_deadline_does_not_reescalate_the_same_head_after_operator_override():
    w = world(prs=[_wedged_pr()], now=_stall_now(7200))
    state = FixState(ci_pending_escalated=frozenset({(100, "sha-aaa")}))

    actions = decide_fix(w, CFG, state, fixer_busy=False)

    assert not _escalated(actions)
    assert not any(
        isinstance(a, PostComment) and a.number == 100 for a in actions
    )


def test_pending_under_deadline_leaves_the_pr_exactly_as_before():
    w = world(prs=[_wedged_pr()], now=_stall_now(1380))
    assert decide_fix(w, CFG, FixState(), fixer_busy=False) == decide_fix(
        world(prs=[_wedged_pr()]), CFG, FixState(), fixer_busy=False
    )


def test_a_long_but_healthy_pipeline_is_not_escalated():
    # The power guardrail: webkit-class suites run ~23 minutes. Escalating those
    # would trade a deadlock for a worse escalation rate.
    w = world(prs=[_wedged_pr()], now=_stall_now(23 * 60))
    assert not _escalated(decide_fix(w, CFG, FixState(), fixer_busy=False))


def test_escalation_does_not_repeat_on_the_next_tick():
    # One-shot: the relabel swaps ai-pr-open for needs-human-review, which is
    # what takes the PR out of the fixer's set — the same mechanism every other
    # breaker here relies on. The next tick observes THAT world and does nothing.
    escalated = _wedged_pr()
    escalated = pr(
        100, "needs-human-review", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa", checks=escalated.checks,
    )
    w = world(prs=[escalated], now=_stall_now(10**6))
    assert decide_fix(w, CFG, FixState(), fixer_busy=False) == []


def test_a_push_resets_the_clock():
    # New head ⇒ fresh check runs ⇒ a newer start, inside the window again. No
    # stored per-SHA state is needed for this; it falls out of measuring from
    # the newest pending start.
    fresh = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-bbb",
        checks=(Check("Test (windows-latest)", state="IN_PROGRESS",
                      started_at=_stall_now(4000)),),
    )
    w = world(prs=[fresh], now=_stall_now(4060))
    assert not _escalated(decide_fix(w, CFG, FixState(), fixer_busy=False))


def test_missing_started_at_never_escalates():
    w = world(prs=[_wedged_pr(started_at="")], now=_stall_now(10**6))
    assert not _escalated(decide_fix(w, CFG, FixState(), fixer_busy=False))


def test_unstamped_world_never_escalates():
    # A world built by something that does not stamp `now` must not escalate
    # every wedged-looking PR at once.
    w = world(prs=[_wedged_pr()])
    assert not _escalated(decide_fix(w, CFG, FixState(), fixer_busy=False))


def test_zero_deadline_disables_the_escalation():
    cfg = FixConfig(fix_bot="fix-bot", ci_pending_max_seconds=0)
    w = world(prs=[_wedged_pr()], now=_stall_now(10**6))
    assert not _escalated(decide_fix(w, cfg, FixState(), fixer_busy=False))




def test_a_stalled_pr_is_not_also_dispatched_or_armed_this_tick():
    # Escalating and acting on the same PR in one poll is incoherent, and a
    # rebase leg cannot unwedge a stuck runner anyway.
    p = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa", mergeable="CONFLICTING",
        reviews=(Review("APPROVED", "review-bot", "t1"),),
        checks=(Check("Test (windows-latest)", state="IN_PROGRESS",
                      started_at=_STALL_T0),),
    )
    actions = decide_fix(world(prs=[p], now=_stall_now(3601)), CFG, FixState(),
                         fixer_busy=False)
    assert _escalated(actions)
    assert not any(isinstance(a, RunAgent) for a in actions)
    assert not any(isinstance(a, (EnableAutoMerge, UpdateBranch)) for a in actions)


def test_the_deadline_is_lock_free():
    # Same world, both lock states, same escalation — unlike the retry breakers,
    # this arm dispatches nothing, so it must not depend on `fixer_busy`.
    w = world(prs=[_wedged_pr()], now=_stall_now(3601))
    busy = decide_fix(w, CFG, FixState(), fixer_busy=True)
    idle = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert _escalated(busy) == _escalated(idle) != []


def test_a_stranger_s_wedged_pr_is_left_alone():
    # Containment: the fixer only ever labels its OWN PRs.
    p = _wedged_pr(author="a-human")
    w = world(prs=[p], now=_stall_now(10**6))
    assert not _escalated(decide_fix(w, CFG, FixState(), fixer_busy=False))


def test_stalls_are_reported_as_the_values_the_escalation_acts_on():
    w = world(prs=[_wedged_pr()], now=_stall_now(3601))
    stalls = ci_pending_stalls(w, CFG)
    assert len(stalls) == 1
    assert stalls[0].pr == 100
    assert stalls[0].head == "sha-aaa"
    assert stalls[0].seconds == 3601
    assert stalls[0].checks == ("Test (windows-latest)",)


def test_no_stalls_reported_when_nothing_is_stalled():
    assert ci_pending_stalls(world(prs=[_wedged_pr()], now=_stall_now(60)), CFG) == ()


# ── wedged-run cancel → re-run (keifu PR #177) ───────────────────────────────
# The deadline used to hand every wedged run to a human because GitHub refuses
# `rerun-failed-jobs` on a run that is still in_progress (403 "already
# running"): the harness HAD a re-run budget and could not spend it on the one
# state that needed it. The operator's manual repair — cancel, wait for the run
# to complete, re-run the failed jobs — is now the harness's, spending that same
# budget, with escalation still bounding it.
def _wedged_run_pr(run_id: int = 77, head: str = "sha-aaa", **kw):
    return pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid=head,
        checks=(
            Check("unit", bucket="pass", started_at=_STALL_T0),
            Check("Test (windows-latest)", state="IN_PROGRESS",
                  started_at=_STALL_T0, run_id=run_id),
        ),
        **kw,
    )


def _cancels(actions):
    return [a for a in actions if isinstance(a, CancelWorkflowRuns)]


def test_a_wedged_run_is_cancelled_instead_of_handed_to_a_human():
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3601))

    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)

    assert _cancels(actions) == [
        CancelWorkflowRuns(100, "sha-aaa", (77,), _stall_now(3601))
    ]
    assert not _escalated(actions)


def test_the_cancel_names_only_the_runs_pinning_this_head():
    # Two wedged jobs of the same run must not produce two cancels, and a check
    # that already passed is not this remediation's business.
    p = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa",
        checks=(
            Check("unit", bucket="pass", started_at=_STALL_T0, run_id=41),
            Check("win", state="IN_PROGRESS", started_at=_STALL_T0, run_id=77),
            Check("mac", state="QUEUED", started_at=_STALL_T0, run_id=77),
            Check("lin", state="QUEUED", started_at=_STALL_T0, run_id=78),
        ),
    )
    actions = decide_fix(world(prs=[p], now=_stall_now(3601)), CFG, FixState(),
                         fixer_busy=False)
    # Run 78 is wholly runnerless: cancelling it would only lose its queue place.
    assert [(a.head, a.run_ids) for a in _cancels(actions)] == [
        ("sha-aaa", (77,))
    ]


def test_a_pr_whose_pending_checks_are_all_queued_is_never_cancelled_or_escalated():
    # PRs #1065/#1070: hosted-runner saturation, not a hang.
    p = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa",
        checks=(
            Check("unit", bucket="pass", started_at=_STALL_T0, run_id=41),
            Check("mac", state="QUEUED", started_at=_STALL_T0, run_id=77),
            Check("lin", state="QUEUED", started_at=_STALL_T0, run_id=78),
        ),
    )
    actions = decide_fix(world(prs=[p], now=_stall_now(10**6)), CFG, FixState(),
                         fixer_busy=False)
    assert not _cancels(actions) and not _escalated(actions)


def test_only_queued_jobs_left_after_a_cancel_do_not_escalate_as_a_cancel_that_never_landed():
    # PR #1065: cancellation spawned new runnerless jobs on the head, and the
    # harness reported "GitHub never took it down" over them.
    p = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa",
        checks=(
            Check("win", bucket="fail", started_at=_STALL_T0, run_id=77),
            Check("agg", state="QUEUED", started_at=_STALL_T0, run_id=79),
        ),
    )
    state = FixState(ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"})
    actions = decide_fix(world(prs=[p], now=_stall_now(3601 * 3)), CFG, state,
                         fixer_busy=False)
    assert not _escalated(actions)


def test_a_stale_cancel_marker_does_not_suppress_the_new_head_s_cancel():
    # A push mid-remediation leaves the OLD head's marker behind. The new head is
    # a new fact and gets its own cancel, named for ITS commit — the adapter's
    # head_sha check is what stops the old head's runs being touched.
    state = FixState(ci_cancel_head={100: f"sha-old@{_stall_now(0)}"})
    w = world(prs=[_wedged_run_pr(head="sha-new")], now=_stall_now(3601))

    actions = decide_fix(w, CFG, state, fixer_busy=False)

    assert [(a.head, a.run_ids) for a in _cancels(actions)] == [
        ("sha-new", (77,))
    ]


def test_the_cancel_carries_the_poll_clock_that_will_bound_the_wait():
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3601))
    assert [a.decided_at for a in _cancels(
        decide_fix(w, CFG, FixState(), fixer_busy=False)
    )] == [_stall_now(3601)]


def test_a_cancel_already_sent_waits_for_the_run_instead_of_re_cancelling():
    # Cancellation completing is the NEXT tick's observation, never a blocking
    # wait inside a poll: this tick simply does nothing about the PR.
    state = FixState(ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"})
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3700))

    actions = decide_fix(w, CFG, state, fixer_busy=False)

    assert _cancels(actions) == [] and not _escalated(actions)
    assert not any(isinstance(a, RunAgent) for a in actions)


def test_the_cancelled_run_is_then_re_run_from_the_existing_budget():
    # GitHub reports a cancelled run as a settled failure, which is exactly the
    # state the #38 free re-run acts on — so the re-run happens through the
    # pre-existing budget rather than a second one.
    settled = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa",
        checks=(
            Check("Test (windows-latest)", state="COMPLETED", bucket="fail",
                  started_at=_STALL_T0, run_id=77),
        ),
    )
    state = FixState(ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"})

    actions = decide_fix(world(prs=[settled], now=_stall_now(3700)), CFG, state,
                         fixer_busy=False)

    assert RerunFailedChecks(100, "sha-aaa", (77,)) in actions
    assert not _escalated(actions)


def test_a_head_whose_re_run_budget_is_spent_escalates_on_the_next_wedge():
    # The ceiling: cancel + re-run are ONE unit of the head's single free
    # re-run, so a head that wedges again after spending it reaches a human on
    # the same schedule as before this path existed.
    state = FixState(
        ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"},
        ci_rerun_head={100: "sha-aaa"},
    )
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3601))

    actions = decide_fix(w, CFG, state, fixer_busy=False)

    assert _cancels(actions) == []
    assert [(r.target, r.number, r.add) for r in _escalated(actions)] == [
        (Target.PR, 100, Label.NEEDS_HUMAN_REVIEW)
    ]


def test_a_cancel_that_never_takes_effect_escalates_within_a_bounded_wait():
    # ADR 0012: the await-cancel wait has a ceiling — one further full deadline
    # — so a cancel GitHub silently ignores cannot become the new deadlock.
    state = FixState(ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"})
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3601 + 3600))

    actions = decide_fix(w, CFG, state, fixer_busy=False)

    assert _cancels(actions) == []
    escalations = [a for a in actions if isinstance(a, EscalateCiPending)]
    assert escalations and "cancelled" in escalations[0].body


def test_a_green_after_our_cancel_escalates_without_a_phantom_pending_check():
    # #303 review: the post-cancel aftermath is synthesised with no pending
    # clock and no checks, so the stall preamble invented "pending for 0
    # minutes (an unnamed check)" on a PR with nothing pending.
    state = FixState(ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"})
    green = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa",
        checks=(Check("unit", state="SUCCESS", bucket="pass"),
                Check("lint", state="SKIPPED", bucket="skipping")),
    )

    actions = decide_fix(world(prs=[green], now=_stall_now(7200)), CFG, state,
                         fixer_busy=False)

    [escalation] = [a for a in actions if isinstance(a, EscalateCiPending)]
    assert escalation.body.startswith("The harness requested cancellation")
    assert "pending for" not in escalation.body


def test_the_wait_is_bounded_even_when_new_jobs_keep_resetting_the_stall_clock():
    # The regression (adversarial review S2): cancelling a wedged run is exactly
    # what lets follow-on jobs start, and one fresh pending check resets
    # `pending_age_seconds`. Bounding the wait on THAT clock left a PR in
    # await_cancel for 11 simulated days — no cancel, no escalation, no leg.
    state = FixState(ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"})
    follow_on = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa",
        checks=(Check("late-job", state="IN_PROGRESS",
                      started_at=_stall_now(100_000), run_id=78),),
    )

    actions = decide_fix(
        world(prs=[follow_on], now=_stall_now(103_601)), CFG, state,
        fixer_busy=False,
    )

    assert [a.pr for a in actions if isinstance(a, EscalateCiPending)] == [100]


def test_an_undated_legacy_marker_escalates_rather_than_waiting_on_no_clock():
    # Markers written before the cancel carried a timestamp are bare SHAs, so
    # there is NO clock to bound their wait — the stall clock is the very one a
    # cancel resets. Waiting on it kept the S2 hole alive in a fallback
    # (adversarial review N2), so an undated marker is grace-spent on sight.
    # The clock here RESETS (a follow-on job starts late), which is precisely
    # the shape the old fallback never terminated on.
    state = FixState(ci_cancel_head={100: "sha-aaa"})
    follow_on = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa",
        checks=(Check("late-job", state="IN_PROGRESS",
                      started_at=_stall_now(100_000), run_id=78),),
    )
    w = world(prs=[follow_on], now=_stall_now(103_601))

    actions = decide_fix(w, CFG, state, fixer_busy=False)

    assert [a.pr for a in actions if isinstance(a, EscalateCiPending)] == [100]
    assert _cancels(actions) == []


def test_disabling_free_re_runs_disables_the_cancel_that_feeds_them():
    # Otherwise the operator's switch buys the worst of both: a destroyed run
    # AND a paid repair leg pointed at a failure the harness itself caused.
    cfg = FixConfig(fix_bot="fix-bot", ci_free_rerun=False)
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3601))

    actions = decide_fix(w, cfg, FixState(), fixer_busy=False)

    assert _cancels(actions) == [] and _escalated(actions)


def test_a_zero_deadline_disables_the_remediation_along_with_the_escalation():
    # The deadline is the only thing that detects a wedge, so disabling it must
    # disable the cancel too — not leave a cancel arm running with no bound.
    cfg = FixConfig(fix_bot="fix-bot", ci_pending_max_seconds=0)
    state = FixState(ci_cancel_head={100: f"sha-aaa@{_stall_now(0)}"})
    w = world(prs=[_wedged_run_pr()], now=_stall_now(10**6))

    assert decide_fix(w, cfg, state, fixer_busy=False) == []


def test_a_wedged_check_with_no_run_identity_still_escalates():
    # An external status has no immutable Actions run to cancel; the human path
    # that predates this remediation is the fail-safe, not an unguarded cancel.
    w = world(prs=[_wedged_pr()], now=_stall_now(3601))

    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)

    assert _cancels(actions) == [] and _escalated(actions)


def test_a_wedged_pr_with_an_unknown_head_is_never_cancelled():
    # No SHA means no way to prove a cancel targets this commit's runs, and an
    # empty marker would compare equal to an empty head.
    p = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="",
        checks=(Check("win", state="IN_PROGRESS", started_at=_STALL_T0,
                      run_id=77),),
    )
    actions = decide_fix(world(prs=[p], now=_stall_now(3601)), CFG, FixState(),
                         fixer_busy=False)
    assert _cancels(actions) == [] and _escalated(actions)


def test_no_repair_leg_is_dispatched_at_a_pr_being_unwedged():
    # Cancelling CI and paying an agent to "fix" the same PR in one tick is
    # incoherent, exactly as escalating and dispatching was.
    p = _wedged_run_pr(mergeable="CONFLICTING")
    actions = decide_fix(world(prs=[p], now=_stall_now(3601)), CFG, FixState(),
                         fixer_busy=False)
    assert _cancels(actions)
    assert not any(isinstance(a, RunAgent) for a in actions)
    assert not any(isinstance(a, (EnableAutoMerge, UpdateBranch)) for a in actions)


def test_remediation_is_lock_free_like_the_deadline_it_replaces():
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3601))
    assert _cancels(decide_fix(w, CFG, FixState(), fixer_busy=True)) == _cancels(
        decide_fix(w, CFG, FixState(), fixer_busy=False)
    ) != []


def test_an_already_escalated_head_is_left_alone_entirely():
    state = FixState(ci_pending_escalated=frozenset({(100, "sha-aaa")}))
    w = world(prs=[_wedged_run_pr()], now=_stall_now(10**6))
    actions = decide_fix(w, CFG, state, fixer_busy=False)
    assert _cancels(actions) == [] and not _escalated(actions)


@pytest.mark.parametrize("verdict", ["SUCCESS", "SKIPPED", "empty"])
def test_empty_policy_clear_ci_after_cancel_escalates_instead_of_merging(verdict):
    approved = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa", mergeable="MERGEABLE",
        reviews=(Review("APPROVED", "review-bot", "t1", commit_id="sha-aaa"),),
        checks=() if verdict == "empty" else (Check("Test (windows-latest)", state=verdict,
                      bucket="pass", started_at=_STALL_T0, run_id=77),),
    )
    state = FixState(ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"})

    actions = decide_fix(world(prs=[approved], now=_stall_now(3700)), CFG,
                         state, fixer_busy=False)

    assert not any(isinstance(a, EnableAutoMerge) for a in actions)
    assert any(isinstance(a, EscalateCiPending) for a in actions)
    assert all(remedy.step is CiStallStep.ESCALATE for remedy in ci_remediations(
        world(prs=[approved], now=_stall_now(3700)), CFG, state))


def test_a_green_head_the_harness_never_cancelled_is_untouched():
    approved = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa", mergeable="MERGEABLE",
        reviews=(Review("APPROVED", "review-bot", "t1", commit_id="sha-aaa"),),
        checks=(Check("Test (windows-latest)", state="COMPLETED",
                      bucket="pass", started_at=_STALL_T0, run_id=77),),
    )

    actions = decide_fix(world(prs=[approved], now=_stall_now(3700)), CFG,
                         FixState(), fixer_busy=False)

    assert any(isinstance(a, EnableAutoMerge) for a in actions)
    assert not any(isinstance(a, EscalateCiPending) for a in actions)


def test_a_spent_re_run_hands_the_verdict_back_to_github():
    approved = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa", mergeable="MERGEABLE",
        reviews=(Review("APPROVED", "review-bot", "t1", commit_id="sha-aaa"),),
        checks=(Check("Test (windows-latest)", state="COMPLETED",
                      bucket="pass", started_at=_STALL_T0, run_id=77),),
    )
    state = FixState(
        ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"},
        ci_rerun_head={100: "sha-aaa"},
    )

    actions = decide_fix(world(prs=[approved], now=_stall_now(3700)), CFG,
                         state, fixer_busy=False)

    assert any(isinstance(a, EnableAutoMerge) for a in actions)
    assert not any(isinstance(a, EscalateCiPending) for a in actions)


def test_clear_ci_does_not_repeat_a_previous_pending_escalation():
    approved = pr(
        100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
        head_ref_oid="sha-aaa", mergeable="MERGEABLE",
        checks=(Check("Test (windows-latest)", state="COMPLETED",
                      bucket="pass", started_at=_STALL_T0, run_id=77),),
    )
    state = FixState(
        ci_cancel_head={100: f"sha-aaa@{_stall_now(3601)}"},
        ci_pending_escalated=frozenset({(100, "sha-aaa")}),
    )

    actions = decide_fix(world(prs=[approved], now=_stall_now(3700)), CFG,
                         state, fixer_busy=False)

    assert not any(isinstance(a, EscalateCiPending) for a in actions)


def test_the_cancel_action_carries_everything_its_command_must_round_trip():
    # Guards adversarial review N4 from the domain side: every field the action
    # carries has to survive `command_for_action`/`action_from_command`, or the
    # executor mints a duplicate command per tick. This pins the field set so a
    # new field cannot be added without the adapter noticing.
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3601))
    cancel = _cancels(decide_fix(w, CFG, FixState(), fixer_busy=False))[0]

    assert (cancel.pr, cancel.head, cancel.run_ids, cancel.decided_at) == (
        100, "sha-aaa", (77,), _stall_now(3601)
    )


def test_remedies_are_the_values_the_decision_acts_on():
    # The journal reads this derivation and the decision emits it, so the two
    # can never disagree about what happened to a stalled head.
    w = world(prs=[_wedged_run_pr()], now=_stall_now(3601))
    remedies = ci_remediations(w, CFG, FixState())
    assert [r.action() for r in remedies] == _cancels(
        decide_fix(w, CFG, FixState(), fixer_busy=False)
    )
    assert remedies[0].step is CiStallStep.CANCEL
    assert remedies[0].pr == 100 and remedies[0].issue == 5


# ── duplicate-PR guard (#39) ─────────────────────────────────────────────────
# Evidence: the operator's own open PR already closed the issue; labelling it
# ai-ready dispatched a full resolve anyway → a duplicate PR for ≈$32, racing a
# fix that was already written.
def _guard_world(*refs, issue_labels=("ai-ready", "P1")):
    return world(issues=[issue(7, *issue_labels)], closing_refs=refs)


def _relabels(actions):
    return [a for a in actions if isinstance(a, Relabel)]


def test_third_party_open_pr_closing_the_issue_blocks_the_resolve():
    w = _guard_world(closing_ref(558, 7, author="xnmp"))
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    # The expensive half: no resolve is dispatched at all.
    assert _run_agents(actions) == []
    # The advisory half: the operator is told WHICH PR, and the issue parks in
    # ai-blocked (not needs-human-review — nothing is broken).
    assert Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_BLOCKED, workflow=_workflow(7).binding) in actions
    body = next(a.body for a in actions if isinstance(a, PostComment))
    assert "#558" in body and "xnmp" in body
    assert all(a.workflow == _workflow(7).binding for a in actions if isinstance(a, PostComment))
    # ...and specifically NOT the ai-working relabel a dispatch would have made.
    assert Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_WORKING, workflow=_workflow(7).binding) not in actions


def test_fix_bots_own_open_pr_is_the_normal_in_flight_state_not_a_duplicate():
    # The fixer's own PR closes its issue by construction; guarding on that would
    # block the harness from ever resolving anything.
    w = _guard_world(closing_ref(558, 7, author="fix-bot"))
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 7, workflow=_workflow(7).binding) in _run_agents(actions)


def test_fix_bot_matched_through_login_eq_not_raw_equality():
    # A FIX_BOT/author pair differing only by the `[bot]` suffix is the SAME
    # identity (review C1) — reading it as a stranger would block every issue.
    w = _guard_world(closing_ref(558, 7, author="Fix-Bot[bot]"))
    assert RunAgent(AgentKind.RESOLVE, 7, workflow=_workflow(7).binding) in _run_agents(
        decide_fix(w, CFG, FixState(), fixer_busy=False)
    )


def test_unknown_author_on_a_closing_pr_is_treated_as_not_ours():
    # An unresolvable author (deleted account / GraphQL null) still means a PR
    # exists that closes this issue, so the fix is already written. Fail toward
    # not spending: at worst the operator un-blocks in one click.
    w = _guard_world(closing_ref(558, 7, author=""))
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_BLOCKED, workflow=_workflow(7).binding) in actions


def test_no_closing_refs_collected_dispatches_as_before():
    # Fail-open: an empty closing_refs means "not collected", never "no duplicate
    # exists" — but a guard that refused work on missing data would strand every
    # issue on any world builder that does not fill it.
    actions = decide_fix(_guard_world(), CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 7, workflow=_workflow(7).binding) in _run_agents(actions)


def test_a_pr_closing_a_different_issue_does_not_block_this_one():
    w = _guard_world(closing_ref(558, 999, author="xnmp"))
    assert RunAgent(AgentKind.RESOLVE, 7, workflow=_workflow(7).binding) in _run_agents(
        decide_fix(w, CFG, FixState(), fixer_busy=False)
    )


def test_guard_ignores_issues_that_are_not_ai_ready():
    # An ai-pr-open issue is mid-flight; the guard is a DISPATCH-time refusal
    # and must not relabel work already under way.
    w = _guard_world(closing_ref(558, 7, author="xnmp"),
                     issue_labels=("ai-pr-open",))
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert _relabels(actions) == []


def test_guard_is_one_shot_because_the_relabel_leaves_the_ready_set():
    # The relabel IS the dedup (the `_escalate_pr` pattern): re-deciding the
    # world it produces yields no second comment and no second relabel.
    ref = closing_ref(558, 7, author="xnmp")
    first = decide_fix(_guard_world(ref), CFG, FixState(), fixer_busy=False)
    assert len(_relabels(first)) == 1
    after = world(issues=[issue(7, "ai-blocked")], closing_refs=[ref])
    assert decide_fix(after, CFG, FixState(), fixer_busy=False) == []


def test_guard_reports_exactly_what_it_refuses():
    w = _guard_world(closing_ref(558, 7, author="xnmp"))
    dups = duplicate_pr_guards(w, CFG)
    assert len(dups) == 1
    assert (dups[0].issue, dups[0].pr, dups[0].author) == (7, 558, "xnmp")


def test_guard_reports_one_pr_per_issue_even_when_several_close_it():
    w = _guard_world(closing_ref(558, 7, author="a"), closing_ref(559, 7, author="b"))
    dups = duplicate_pr_guards(w, CFG)
    assert [d.pr for d in dups] == [558]      # first wins: one comment, one relabel
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert len(_relabels(actions)) == 1


def test_guard_can_be_switched_off():
    cfg = FixConfig(fix_bot="fix-bot", duplicate_pr_guard=False)
    w = _guard_world(closing_ref(558, 7, author="xnmp"))
    assert duplicate_pr_guards(w, cfg) == ()
    assert RunAgent(AgentKind.RESOLVE, 7, workflow=_workflow(7).binding) in _run_agents(
        decide_fix(w, cfg, FixState(), fixer_busy=False)
    )


def test_guarded_issue_does_not_consume_the_polls_other_work():
    # The guard emits no RunAgent, so a PR leg in the same world still runs —
    # refusing a resolve must not idle the fixer.
    p = pr(100, "ai-pr-open", head_ref_oid="sha-1",
           checks=(Check("unit", bucket="fail"),))
    w = world(issues=[issue(7, "ai-ready")], prs=[p],
              closing_refs=[closing_ref(558, 7, author="xnmp")])
    state = FixState(ci_rerun_head={100: "sha-1"})   # free re-run already spent
    actions = decide_fix(w, CFG, state, fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


# ── flake-aware CI repair: one free re-run per head SHA (#38) ────────────────
# Evidence: two FIX_CI legs ($13.15 + $28.88) of one night both re-fixed the
# same pre-existing flaky test, unrelated to either PR's diff.
def _red_pr(head="sha-1"):
    return pr(100, "ai-pr-open", head_ref_oid=head,
              checks=(Check("unit", bucket="fail", run_id=9001),))


def _reruns(actions):
    return [a for a in actions if isinstance(a, RerunFailedChecks)]


def test_first_failure_on_a_head_re_runs_instead_of_paying_for_a_leg():
    w = world(prs=[_red_pr()])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert _reruns(actions) == [RerunFailedChecks(100, "sha-1", (9001,))]
    assert _run_agents(actions) == []                # the $13–29 never happens


def test_free_rerun_requires_ids_for_every_failed_gate():
    external = pr(
        100,
        "ai-pr-open",
        head_ref_oid="sha-1",
        checks=(
            Check("actions", bucket="fail", run_id=9001),
            Check("external", bucket="fail"),
        ),
    )

    actions = decide_fix(world(prs=[external]), CFG, FixState(), fixer_busy=False)

    assert _reruns(actions) == []
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


def test_free_rerun_captures_all_unique_failed_workflow_runs():
    red = pr(
        100,
        "ai-pr-open",
        head_ref_oid="sha-1",
        checks=(
            Check("unit", bucket="fail", run_id=9001),
            Check("integration", bucket="fail", run_id=9002),
            Check("unit shard", bucket="fail", run_id=9001),
        ),
    )

    actions = decide_fix(world(prs=[red]), CFG, FixState(), fixer_busy=False)

    assert _reruns(actions) == [
        RerunFailedChecks(100, "sha-1", (9001, 9002))
    ]


def test_second_failure_on_the_same_head_pays_for_the_leg():
    # The re-run was spent and the checks came back red on the identical commit:
    # that is evidence about the diff, not about flake.
    w = world(prs=[_red_pr()])
    state = FixState(ci_rerun_head={100: "sha-1"})
    actions = decide_fix(w, CFG, state, fixer_busy=False)
    assert _reruns(actions) == []
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


def test_a_new_head_gets_a_fresh_free_re_run():
    # A push is a genuinely new failure; the marker keys on the SHA so the
    # allowance resets with no extra bookkeeping.
    w = world(prs=[_red_pr(head="sha-2")])
    state = FixState(ci_rerun_head={100: "sha-1"})
    assert _reruns(decide_fix(w, CFG, state, fixer_busy=False)) == [
        RerunFailedChecks(100, "sha-2", (9001,))
    ]


def test_pending_checks_after_a_re_run_dispatch_nothing():
    # The re-run flips the checks to PENDING and the existing
    # pending-outranks-failure rule waits — no leg, no second re-run.
    p = pr(100, "ai-pr-open", head_ref_oid="sha-1",
           checks=(Check("unit", bucket="pending"), Check("lint", bucket="fail")))
    state = FixState(ci_rerun_head={100: "sha-1"})
    actions = decide_fix(world(prs=[p]), CFG, state, fixer_busy=False)
    assert _reruns(actions) == [] and _run_agents(actions) == []


def test_an_unknown_head_sha_never_re_runs():
    # With no SHA the executor could not record the allowance as spent, so a
    # re-run here would loop forever. Fall through to the paid (breaker-bounded)
    # leg instead.
    p = pr(100, "ai-pr-open", head_ref_oid="",
           checks=(Check("unit", bucket="fail"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert _reruns(actions) == []
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


@pytest.mark.parametrize("paid_first", [False, True])
def test_the_re_run_consumes_no_fixer_so_another_pr_can_still_dispatch(paid_first):
    red = _red_pr()
    other = pr(101, "ai-pr-open", head_ref_oid="sha-9",
               checks=(Check("unit", bucket="fail"),))
    state = FixState(ci_rerun_head={101: "sha-9"})    # #101's re-run is spent
    actions = decide_fix(world(prs=[other, red] if paid_first else [red, other]),
                         CFG, state, fixer_busy=False)
    assert _reruns(actions) == [RerunFailedChecks(100, "sha-1", (9001,))]
    assert RunAgent(AgentKind.FIX_CI, 101) in _run_agents(actions)


@pytest.mark.parametrize(("slots", "inflight"), [
    (1, ()),
    (0, (InFlightFix(7, frozenset({"src/other.py"})),)),
    (0, ()),  # Reload draining closes admission even with no occupied slot.
    (0, (InFlightFix(7, frozenset(), exclusive=True),)),
    (1, (InFlightFix(7, frozenset({"src/shared.py"})),)),
])
def test_free_rerun_is_independent_of_paid_capacity_and_path_reservations(slots, inflight):
    red = replace(
        _red_pr(), merge_state="UNSTABLE", closing_issue=5,
        reviews=(Review("APPROVED", "review-bot", "t1"),),
        changed_files=frozenset({"src/shared.py"}),
    )

    actions = decide_fix(
        world(prs=[red]), CFG, FixState(inflight=inflight),
        fixer_busy=bool(inflight), dispatch_slots=slots,
    )

    assert _reruns(actions) == [RerunFailedChecks(100, "sha-1", (9001,))]
    assert _run_agents(actions) == []


@pytest.mark.parametrize("slots", [0, 1])
def test_free_rerun_still_excludes_active_work_on_its_linked_issue(slots):
    red = replace(_red_pr(), closing_issue=5)
    state = FixState(inflight=(InFlightFix(5, frozenset({"src/other.py"})),))

    actions = decide_fix(
        world(prs=[red]), CFG, state, fixer_busy=True, dispatch_slots=slots,
    )

    assert _reruns(actions) == []
    assert _run_agents(actions) == []


@pytest.mark.parametrize(("slots", "inflight"), [
    (0, ()),
    (1, (InFlightFix(7, frozenset({"src/shared.py"})),)),
])
def test_a_tripped_ci_breaker_neither_reruns_nor_escalates_without_capacity(slots, inflight):
    # Only the free rerun ignores capacity: the breaker's handover keeps the
    # paid path's capacity and path gates and waits for them.
    red = replace(_red_pr(), changed_files=frozenset({"src/shared.py"}))
    cfg = FixConfig(fix_bot="fix-bot", max_ci_retries=1)
    actions = decide_fix(world(prs=[red]), cfg, FixState(ci_retries={100: 1}, inflight=inflight),
                         fixer_busy=bool(inflight), dispatch_slots=slots)
    assert _reruns(actions) == []
    assert _run_agents(actions) == []
    assert not any(isinstance(a, Relabel) for a in actions)


def test_an_issueless_pr_is_not_escalated_on_a_busy_tick():
    stale = with_checks(
        _red_pr(), closing_issue=None, head_ref_name="feat/ACME-2468-x",
        checks=(Check("unit", bucket="pass"),),
        reviews=(Review("CHANGES_REQUESTED", "review-bot", "t1", commit_id="old-sha"),),
    )
    actions = decide_fix(world(prs=[stale]), CFG, FixState(), fixer_busy=True, dispatch_slots=0)
    assert not any(isinstance(a, Relabel) for a in actions)


def test_spent_free_rerun_cannot_launch_paid_repair_without_capacity():
    actions = decide_fix(
        world(prs=[_red_pr()]), CFG, FixState(ci_rerun_head={100: "sha-1"}),
        fixer_busy=False, dispatch_slots=0,
    )

    assert _reruns(actions) == []
    assert _run_agents(actions) == []


@pytest.mark.parametrize(("overrides", "state", "issues"), [
    ({"labels": frozenset({Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW})}, FixState(), ()),
    ({}, FixState(), (issue(100, "ai-pr-open", blocked_by_open=True),)),
    ({"mergeable": "CONFLICTING", "merge_state": "DIRTY"}, FixState(), ()),
    ({"reviews": (Review("CHANGES_REQUESTED", "review-bot", "t1"),)}, FixState(), ()),
    ({}, FixState(ci_remediation_errors={100: "unreadable remediation"}), ()),
    ({"head_ref_oid": ""}, FixState(), ()),
    ({"checks": (Check("unit", bucket="fail"),)}, FixState(), ()),
])
def test_zero_capacity_preserves_free_rerun_safety_and_repair_precedence(overrides, state, issues):
    actions = decide_fix(
        world(prs=[with_checks(_red_pr(), **overrides)], issues=issues),
        replace(CFG, auto_rebase_conflicts=True), state,
        fixer_busy=False, dispatch_slots=0,
    )

    assert _reruns(actions) == []
    assert _run_agents(actions) == []


def test_the_ci_breaker_still_escalates_before_a_free_re_run():
    # A PR that has burnt its CI-fix budget belongs to a human; delaying that by
    # a CI cycle would move the escalation latency the wrong way.
    cfg = FixConfig(fix_bot="fix-bot", max_ci_retries=1)
    w = world(prs=[_red_pr()])
    actions = decide_fix(w, cfg, FixState(ci_retries={100: 1}), fixer_busy=False)
    assert _reruns(actions) == []
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN,
                   Label.NEEDS_HUMAN_REVIEW) in actions


def test_free_re_run_can_be_switched_off():
    cfg = FixConfig(fix_bot="fix-bot", ci_free_rerun=False)
    actions = decide_fix(world(prs=[_red_pr()]), cfg, FixState(), fixer_busy=False)
    assert _reruns(actions) == []
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


def test_a_strangers_red_pr_is_never_re_run():
    # Containment: the fixer acts only on its OWN PRs, and a re-run is an act.
    p = pr(100, "ai-pr-open", author="a-human", head_ref_oid="sha-1",
           checks=(Check("unit", bucket="fail"),))
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert _reruns(actions) == [] and _run_agents(actions) == []



def test_unreadable_leg_history_holds_ready_target_without_starving_peer():
    observed = world(issues=[issue(1, "ai-ready", "P1"), issue(2, "ai-ready", "P2")])
    state = FixState(control_errors={1: "fix leg history is unavailable"})
    actions = decide_fix(observed, FixConfig(fix_bot="fix-bot"), state, fixer_busy=False)
    paid = [action for action in actions if isinstance(action, RunAgent)]
    assert [action.number for action in paid] == [2]
    assert not any(isinstance(action, (Relabel, PostComment, CloseIssue)) and action.number == 1 for action in actions)


def test_approved_green_pr_with_legacy_grant_text_merges_without_exemption_facts():
    approved = _approved_green_pr()
    approved = replace(approved, reviews=tuple(replace(r, body="gate-exemption: granted legacy text") for r in approved.reviews))
    actions = decide_fix(world(prs=(approved,)), FixConfig("fix-bot"), FixState(), fixer_busy=False)
    assert EnableAutoMerge(approved.number, "squash", approved.head_ref_oid) in actions
    assert not any(isinstance(action, Relabel) and action.add == Label.NEEDS_HUMAN_REVIEW for action in actions)
