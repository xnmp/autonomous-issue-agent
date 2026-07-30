"""Fix-watcher decision behaviour — the poll_once control flow + invariants."""

from tests.domain.builders import Check, Comment, Review, closing_ref, issue, pr, world

from issue_agent.domain.actions import (
    AgentKind,
    ClearIssueState,
    CloseIssue,
    EnableAutoMerge,
    EscalateCiPending,
    UpdateBranch,
    PostComment,
    Relabel,
    RemoveWorktree,
    RerunFailedChecks,
    RunAgent,
    Target,
)
from issue_agent.domain.fix import (
    CiFlake,
    FixConfig,
    FixState,
    InFlightFix,
    HOLD_BLOCKER,
    HOLD_OVERLAP,
    Hold,
    breaker_tripped,
    breaker_key,
    ci_flakes,
    ci_pending_stalls,
    cleanup_actions,
    decide_fix,
    duplicate_pr_guards,
    issue_holds,
    issues_needing_resume,
    orphaned_cleanup_actions,
    pick_ready_issue,
    prs_needing_resume,
    reap_actions,
    should_automerge,
)
from issue_agent.domain.labels import Label

CFG = FixConfig(fix_bot="fix-bot")


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


# ── holds: WHY a ready issue was not picked (harness #6) ──────────────────────
def test_issue_holds_names_the_overlapping_pr():
    iss = issue(2, "ai-ready", declared_paths=frozenset({"backend/app/services/x.py"}))
    open_pr = pr(100, "ai-pr-open", state="OPEN",
                 changed_files=frozenset({"backend/app/services/x.py"}))
    assert issue_holds(world(issues=[iss], prs=[open_pr])) == (
        Hold(2, HOLD_OVERLAP, blocker_pr=100),
    )


def test_issue_holds_reports_a_dependency_hold_with_no_nameable_blocker():
    # blocked_by_open is a settled boolean, so there is no number to name; the
    # REASON is still the thing the operator needs.
    w = world(issues=[issue(3, "ai-ready", blocked_by_open=True)])
    assert issue_holds(w) == (Hold(3, HOLD_BLOCKER),)


def test_issue_holds_reports_both_reasons_for_a_doubly_held_issue():
    iss = issue(4, "ai-ready", blocked_by_open=True,
                declared_paths=frozenset({"a/b.py"}))
    open_pr = pr(7, state="OPEN", changed_files=frozenset({"a/b.py"}))
    assert issue_holds(world(issues=[iss], prs=[open_pr])) == (
        Hold(4, HOLD_BLOCKER), Hold(4, HOLD_OVERLAP, blocker_pr=7),
    )


def test_issue_holds_is_empty_when_nothing_is_held():
    # A dispatchable issue, an unrelated open PR, and an issue that is not
    # ai-ready at all (its non-dispatch is not a hold — it was never queued).
    iss = issue(2, "ai-ready", declared_paths=frozenset({"routes/y.py"}))
    other = issue(9, "ai-working", blocked_by_open=True)
    open_pr = pr(100, state="OPEN", changed_files=frozenset({"services/x.py"}))
    assert issue_holds(world(issues=[iss, other], prs=[open_pr])) == ()


def test_issue_holds_ignores_a_closed_issue():
    iss = issue(2, "ai-ready", state="closed", blocked_by_open=True)
    assert issue_holds(world(issues=[iss])) == ()


def test_a_held_issue_is_exactly_one_absent_from_pick_ready_issue():
    """The anti-drift contract: the journal must explain the REAL decision, so
    `issue_holds` reports a hold for exactly those ai-ready issues
    `pick_ready_issue` refuses to pick. Driven over every combination of the two
    gate inputs against one open PR (plus the fail-open no-paths case)."""
    open_pr = pr(100, state="OPEN", changed_files=frozenset({"x.py"}))
    for n, (blocked, paths) in enumerate(
        [(b, p) for b in (False, True)
         for p in (frozenset(), frozenset({"x.py"}), frozenset({"y.py"}))],
        start=1,
    ):
        w = world(
            issues=[issue(n, "ai-ready", blocked_by_open=blocked, declared_paths=paths)],
            prs=[open_pr],
        )
        held = bool(issue_holds(w))
        assert held is (pick_ready_issue(w) is None), (blocked, paths)


def test_holds_do_not_mask_a_pickable_issue():
    # Anti-vacuity for the consistency test: with one held and one free issue,
    # the free one is still dispatched and only the held one is reported.
    held = issue(1, "ai-ready", "P1", declared_paths=frozenset({"x.py"}))
    free = issue(2, "ai-ready", "P2")
    open_pr = pr(100, state="OPEN", changed_files=frozenset({"x.py"}))
    w = world(issues=[held, free], prs=[open_pr])
    assert pick_ready_issue(w) == 2
    assert issue_holds(w) == (Hold(1, HOLD_OVERLAP, blocker_pr=100),)


def test_dependency_hold_names_the_blocking_issue():
    w = world(
        issues=[
            issue(
                3,
                "ai-ready",
                blocked_by_open=True,
                open_blocker_numbers=(42,),
            )
        ]
    )
    assert issue_holds(w) == (Hold(3, HOLD_BLOCKER, blocker_issue=42),)


def test_resolve_dispatch_relabels_then_runs():
    w = world(issues=[issue(5, "ai-ready")])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert Relabel(Target.ISSUE, 5, Label.AI_READY, Label.AI_WORKING) in actions
    assert RunAgent(AgentKind.RESOLVE, 5) in actions


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
        RunAgent(AgentKind.RESOLVE, 1),
        RunAgent(AgentKind.RESOLVE, 2),
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

    assert _run_agents(actions) == [RunAgent(AgentKind.RESOLVE, 1)]


def test_active_unknown_path_resolve_excludes_other_work():
    w = world(issues=[
        issue(2, "ai-ready", predicted_paths=frozenset({"src/free.py"})),
    ])
    inflight = (InFlightFix(1, exclusive=True),)

    assert pick_ready_issue(w, inflight) is None
    assert issue_holds(w, inflight) == (
        Hold(2, HOLD_OVERLAP, blocker_issue=1),
    )


def test_inflight_paths_hold_a_ready_issue_before_it_has_a_pr():
    w = world(issues=[
        issue(2, "ai-ready", predicted_paths=frozenset({"src/shared.py"})),
        issue(3, "ai-ready", predicted_paths=frozenset({"src/free.py"})),
    ])
    inflight = (InFlightFix(1, frozenset({"src/shared.py"})),)

    assert pick_ready_issue(w, inflight) == 3
    assert issue_holds(w, inflight) == (
        Hold(2, HOLD_OVERLAP, blocker_issue=1),
    )


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
    iss = issue(7, "ai-blocked", comments=(Comment("alice", "here you go", "t2"),))
    w = world(issues=[iss])
    # no session → not eligible
    assert issues_needing_resume(w, FixState(), CFG) == []
    # with a session → eligible
    st = FixState(sessions=frozenset({7}))
    assert issues_needing_resume(w, st, CFG) == [(7, "t2")]


def test_resume_skips_already_resumed_reply():
    iss = issue(7, "ai-blocked", comments=(Comment("alice", "x", "t2"),))
    st = FixState(sessions=frozenset({7}), resumed_at={7: "t2"})
    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_resume_ignores_bot_and_fixer_comments():
    iss = issue(
        7, "ai-blocked",
        comments=(Comment("fix-bot", "asking", "t1"), Comment("ci[bot]", "noise", "t2")),
    )
    st = FixState(sessions=frozenset({7}))
    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_resume_accepts_operator_reply_when_operator_is_also_fix_bot():
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment(
                "fix-bot",
                "**Agent needs input:** choose A or B\n\n_Reply to unblock._",
                "t1",
            ),
            Comment("fix-bot", "Use A.", "t2"),
        ),
    )
    st = FixState(sessions=frozenset({7}))

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == [(7, "t2")]


def test_resume_accepts_reply_when_fix_bot_is_an_app_identity():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot[bot]", prompt, "t1"),
            Comment("fix-bot[bot]", "Use A.", "t2"),
        ),
    )
    cfg = FixConfig(fix_bot="fix-bot[bot]")
    st = FixState(sessions=frozenset({7}))

    assert issues_needing_resume(world(issues=[iss]), st, cfg) == [(7, "t2")]


def test_unblock_prompt_does_not_count_as_its_own_reply():
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment(
                "fix-bot",
                "**Agent needs input:** choose A or B\n\n_Reply to unblock._",
                "t1",
            ),
        ),
    )
    st = FixState(sessions=frozenset({7}))

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_second_round_unblock_prompt_supersedes_the_original_reply():
    first = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    second = "**Agent still needs input:** provide a value\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", first, "t1"),
            Comment("alice", "Use A.", "t2"),
            Comment("fix-bot", second, "t3"),
        ),
    )
    st = FixState(sessions=frozenset({7}), resumed_at={7: "t2"})

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_author_leg_prompt_accepts_fix_bot_operator_reply():
    prompt = "**Author needs input:** expected behavior?\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", prompt, "t1"),
            Comment("fix-bot", "Use the documented behavior.", "t2"),
        ),
    )
    st = FixState(sessions=frozenset({7}))

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == [(7, "t2")]


def test_quoted_unblock_prompt_is_consumed_as_the_reply():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", prompt, "t1"),
            Comment("alice", f"> {prompt}\n\nUse A.", "t2"),
        ),
    )
    st = FixState(sessions=frozenset({7}))

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == [(7, "t2")]


def test_unrelated_bot_noise_after_prompt_does_not_trigger_resume():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    iss = issue(
        7,
        "ai-blocked",
        comments=(
            Comment("fix-bot", prompt, "t1"),
            Comment("ci[bot]", "build finished", "t2"),
        ),
    )
    st = FixState(sessions=frozenset({7}))

    assert issues_needing_resume(world(issues=[iss]), st, CFG) == []


def test_resume_dispatch_relabels_blocked_to_working():
    # A blocked issue with a new human reply + a saved session → RESUME dispatch,
    # and the issue must flip ai-blocked → ai-working for the run's duration (so it
    # doesn't read as still-blocked while actively resuming).
    iss = issue(7, "ai-blocked", comments=(Comment("alice", "go ahead", "t2"),))
    st = FixState(sessions=frozenset({7}))
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
        comments=(Comment("fix-bot", prompt, "t1"), Comment("alice", "Use A.", "t2")),
    )
    state = FixState(sessions=frozenset({7}))
    w = world(prs=[blocked])

    assert prs_needing_resume(w, state, CFG) == [(100, 7, "t2")]
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
            Comment("alice", "Earlier feedback", "t1"),
            Comment("fix-bot", review_prompt, "t2"),
        ),
    )
    state = FixState(sessions=frozenset({7}))

    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == []
    assert _run_agents(decide_fix(world(prs=[blocked]), CFG, state, fixer_busy=False)) == []


def test_blocked_pr_reply_already_resumed_does_not_dispatch_again():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "t1"), Comment("alice", "Use A.", "t2")),
    )
    state = FixState(sessions=frozenset({7}), pr_resumed_at={100: "t2"})

    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == []
    assert _run_agents(decide_fix(world(prs=[blocked]), CFG, state, fixer_busy=False)) == []


def test_blocked_pr_newer_reply_after_dedupe_dispatches_once_more():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(
            Comment("fix-bot", prompt, "t1"),
            Comment("alice", "Use A.", "t2"),
            Comment("alice", "Actually use B.", "t3"),
        ),
    )
    state = FixState(sessions=frozenset({7}), pr_resumed_at={100: "t2"})

    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == [(100, 7, "t3")]
    assert RunAgent(AgentKind.PR_RESUME, 100) in _run_agents(
        decide_fix(world(prs=[blocked]), CFG, state, fixer_busy=False)
    )


def test_blocked_pr_reply_from_operator_who_is_fix_bot_dispatches_resume():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "t1"), Comment("fix-bot", "Use A.", "t2")),
    )
    state = FixState(sessions=frozenset({7}))

    assert prs_needing_resume(world(prs=[blocked]), state, CFG) == [(100, 7, "t2")]


def test_pr_resume_infra_ceiling_replaces_the_blocked_label():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    blocked = pr(
        100, "ai-pr-open", "ai-blocked", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "t1"), Comment("alice", "Use A.", "t2")),
    )
    state = FixState(sessions=frozenset({7}), infra_deaths={7: 5})

    assert Relabel(Target.PR, 100, Label.AI_BLOCKED, Label.NEEDS_HUMAN_REVIEW) in decide_fix(
        world(prs=[blocked]), INFRA_CFG, state, fixer_busy=False
    )


def test_needs_human_review_pr_reply_never_reopens_the_escalation():
    prompt = "**Agent needs input:** choose A or B\n\n_Reply to unblock._"
    escalated = pr(
        100, "ai-pr-open", "needs-human-review", closing_issue=7,
        comments=(Comment("fix-bot", prompt, "t1"), Comment("alice", "Use A.", "t2")),
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
        reviews=(Review("APPROVED", "review-bot", "t1"),),
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


def test_automerge_marker_is_idempotent():
    st = FixState(automerge_enabled=frozenset({100}))
    assert should_automerge(_approved_green_pr(), st, CFG) is False


def test_decide_emits_enable_automerge():
    w = world(prs=[_approved_green_pr()])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert EnableAutoMerge(100, "squash") in actions


# ── BEHIND (clean-but-stale) branch → update-branch so armed auto-merge lands ──
def _approved_green_behind_pr():
    return pr(
        100, "ai-pr-open",
        reviews=(Review("APPROVED", "review-bot", "t1"),),
        checks=(Check("build", bucket="pass"),),
        mergeable="MERGEABLE", merge_state="BEHIND",
    )


def test_behind_pr_emits_update_branch_with_automerge():
    w = world(prs=[_approved_green_behind_pr()])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert EnableAutoMerge(100, "squash") in actions
    assert UpdateBranch(100) in actions


def test_already_armed_behind_pr_still_updates_branch():
    # The real gap: a PR armed on an EARLIER poll (marker set) that is now BEHIND.
    # should_automerge is marker-guarded → no re-arm, but the branch update MUST
    # still fire, else it stays armed-but-behind forever (Myapp #348/#340).
    st = FixState(automerge_enabled=frozenset({100}))
    actions = decide_fix(world(prs=[_approved_green_behind_pr()]), CFG, st, fixer_busy=False)
    assert not any(isinstance(a, EnableAutoMerge) for a in actions)  # already armed
    assert UpdateBranch(100) in actions                              # but still updated


def test_clean_pr_does_not_update_branch():
    # A CLEAN approved PR arms auto-merge but needs NO branch update.
    actions = decide_fix(world(prs=[_approved_green_pr()]), CFG, FixState(), fixer_busy=False)
    assert not any(isinstance(a, UpdateBranch) for a in actions)


def test_behind_without_approval_does_not_update_branch():
    # BEHIND but not approvable → auto-merge gate fails → no update (nothing to land).
    p = pr(100, "ai-pr-open", checks=(Check("build", bucket="pass"),),
           mergeable="MERGEABLE", merge_state="BEHIND")
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert not any(isinstance(a, UpdateBranch) for a in actions)


def test_conflicting_takes_rebase_not_update_branch():
    # DIRTY/CONFLICTING → the REBASE agent path, never the cheap update-branch.
    p = pr(100, "ai-pr-open", state="OPEN", head_ref_name="fix/5-x",
           reviews=(Review("APPROVED", "review-bot", "t1"),),
           checks=(Check("build", bucket="pass"),),
           mergeable="CONFLICTING", merge_state="DIRTY")
    actions = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    assert not any(isinstance(a, UpdateBranch) for a in actions)
    assert not p.is_behind()


# ── M14: auto-merge is INDEPENDENT of the fixer lock ──────────────────────────
# It is documented as lock-free but used to live at the tail of the PR-consuming
# loop, so every `continue` in that loop skipped it. The issueless arm `continue`s
# unconditionally, so an approved+green issueless PR armed auto-merge ONLY on the
# polls where the fixer happened to be BUSY — busy skips the whole consuming block,
# so the `continue` never ran. Same world, opposite outcome, decided by an
# unrelated lock. These pin the invariant: for one world, both lock states agree.
def _issueless_approved_green_pr(**kw):
    return pr(113, "ai-pr-open", head_ref_name="feat/ACME-9-x",
              reviews=(Review("APPROVED", "review-bot", "t1"),),
              checks=(Check("build", bucket="pass"),), **kw)


def test_issueless_approved_green_pr_arms_automerge_idle_and_busy():
    w = world(prs=[_issueless_approved_green_pr()])
    idle = decide_fix(w, CFG, FixState(), fixer_busy=False)
    busy = decide_fix(w, CFG, FixState(), fixer_busy=True)
    assert EnableAutoMerge(113, "squash") in idle   # this was the missing half
    assert EnableAutoMerge(113, "squash") in busy


def test_issueless_behind_pr_updates_branch_idle_and_busy():
    # Same divergence on the other 3c arm: an armed-but-BEHIND branch stayed
    # behind forever on idle polls, so the armed auto-merge never landed.
    w = world(prs=[_issueless_approved_green_pr(mergeable="MERGEABLE", merge_state="BEHIND")])
    assert UpdateBranch(113) in decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert UpdateBranch(113) in decide_fix(w, CFG, FixState(), fixer_busy=True)


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
    assert EnableAutoMerge(100, "squash") not in idle     # conflicted: rebase owns it
    assert EnableAutoMerge(100, "squash") not in busy

    clean = pr(100, "ai-pr-open", mergeable="MERGEABLE",
               reviews=(Review("APPROVED", "review-bot", "t1"),),
               checks=(Check("build", bucket="pass"),))
    for busy_flag in (False, True):
        actions = decide_fix(world(prs=[clean]), CFG, FixState(), fixer_busy=busy_flag)
        assert EnableAutoMerge(100, "squash") in actions  # arming is lock-independent


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
        assert EnableAutoMerge(113, "squash") in actions          # the unblocked one arms
        assert EnableAutoMerge(114, "squash") not in actions      # the blocked one does not


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
SUFFIXED_CFG = FixConfig(fix_bot="fix-bot[bot]")


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
           reviews=(Review("APPROVED", "review-bot", "t1"),),
           checks=(Check("build", bucket="pass"),))
    assert should_automerge(q, FixState(), SUFFIXED_CFG)


def test_c1_suffixed_fix_bot_does_not_read_its_own_comment_as_a_human_reply():
    # `last_human_reply` under the same asymmetry: the fixer's own bare-login
    # comment must not count as a new human reply, or one unanswered question
    # re-fires a paid RESUME session every poll.
    iss = issue(7, "ai-blocked", comments=(Comment("fix-bot", "asking", "t1"),))
    st = FixState(sessions=frozenset({7}))
    assert issues_needing_resume(world(issues=[iss]), st, SUFFIXED_CFG) == []
    # Anti-vacuity twin: a real human's reply on the same world still resumes.
    iss2 = issue(7, "ai-blocked", comments=(
        Comment("fix-bot", "asking", "t1"), Comment("alice", "here you go", "t2"),
    ))
    assert issues_needing_resume(world(issues=[iss2]), st, SUFFIXED_CFG) == [(7, "t2")]


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


# ── resolve breaker (T3 finding: the most expensive leg had no ceiling) ──────
def test_resolve_under_cap_dispatches():
    st = FixState(resolve_retries={5: 2})
    actions = decide_fix(world(issues=[issue(5, "ai-ready")]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 5) in _run_agents(actions)


def test_resolve_at_cap_escalates_issue_not_dispatches():
    st = FixState(resolve_retries={5: 3})  # cap is 3 (default)
    actions = decide_fix(world(issues=[issue(5, "ai-ready")]), CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.ISSUE, 5, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW) in actions
    assert any(isinstance(a, PostComment) and a.number == 5 for a in actions)


def test_fix_ci_at_cap_escalates_not_dispatches():
    st = FixState(ci_retries={9: 3})
    actions = decide_fix(world(prs=[_ci_fail_pr()]), CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


def test_fix_ci_under_cap_still_dispatches():
    st = FixState(ci_retries={9: 2})
    actions = decide_fix(world(prs=[_ci_fail_pr()]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


# ── infra-death ceiling (review C3/D2c) ──────────────────────────────────────
# A session that dies BEFORE returning a result records no attempt anywhere (that
# exemption is FAILURE-MODES #58), so none of the retry breakers above can see it
# — the death cycle was unbounded. These pin the SEPARATE, higher ceiling: same
# escalation shape, its own counter, its own reason text.
INFRA_CFG = FixConfig(fix_bot="fix-bot", max_infra_deaths=2)


def test_ready_issue_at_infra_ceiling_escalates_instead_of_dispatching():
    st = FixState(infra_deaths={5: 2})
    actions = decide_fix(world(issues=[issue(5, "ai-ready")]), INFRA_CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.ISSUE, 5, Label.AI_READY, Label.NEEDS_HUMAN_REVIEW) in actions
    assert any(isinstance(a, PostComment) and "died 2×" in a.body for a in actions)


def test_ready_issue_below_infra_ceiling_still_dispatches():
    # Anti-vacuity twin: the ceiling is HIGHER than the retry caps on purpose —
    # a couple of transport deaths must not stop a healthy issue.
    st = FixState(infra_deaths={5: 1})
    actions = decide_fix(world(issues=[issue(5, "ai-ready")]), INFRA_CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 5) in _run_agents(actions)


def test_infra_deaths_are_counted_against_their_own_higher_cap():
    # Defaults: max_resolve_retries=3, max_infra_deaths=5. Four infra deaths is
    # past the RETRY cap and must still dispatch — the two counters are separate
    # ceilings, and "resolve failed 4× without opening a PR" would be a lie
    # about work that never ran.
    st = FixState(infra_deaths={5: 4}, resolve_retries={5: 0})
    actions = decide_fix(world(issues=[issue(5, "ai-ready")]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 5) in _run_agents(actions)


def test_infra_ceiling_of_zero_never_escalates():
    st = FixState(infra_deaths={5: 99})
    cfg = FixConfig(fix_bot="fix-bot", max_infra_deaths=0)  # 0 = off, like every cap
    actions = decide_fix(world(issues=[issue(5, "ai-ready")]), cfg, st, fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 5) in _run_agents(actions)


def _blocked_issue_with_reply():
    return issue(7, "ai-blocked", comments=(Comment("alice", "more context", "t1"),))


def test_resume_at_infra_ceiling_escalates_from_blocked():
    # A resume that dies pre-result never consumes the reply (resume_actions
    # returns nothing on an infra death), so the dedup mark cannot stop the loop
    # either — only this ceiling can. The issue is ai-blocked here, so that is
    # the label the escalation removes.
    st = FixState(sessions=frozenset({7}), infra_deaths={7: 2})
    actions = decide_fix(world(issues=[_blocked_issue_with_reply()]), INFRA_CFG, st,
                         fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.ISSUE, 7, Label.AI_BLOCKED, Label.NEEDS_HUMAN_REVIEW) in actions


def test_resume_below_infra_ceiling_still_dispatches():
    st = FixState(sessions=frozenset({7}), infra_deaths={7: 1})
    actions = decide_fix(world(issues=[_blocked_issue_with_reply()]), INFRA_CFG, st,
                         fixer_busy=False)
    assert RunAgent(AgentKind.RESUME, 7) in _run_agents(actions)


def test_ci_leg_at_infra_ceiling_escalates_the_pr():
    st = FixState(infra_deaths={9: 2})  # keyed by breaker_key → closing_issue 9
    actions = decide_fix(world(prs=[_ci_fail_pr()]), INFRA_CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


def test_address_review_leg_at_infra_ceiling_escalates_the_pr():
    st = FixState(infra_deaths={9: 2})
    actions = decide_fix(world(prs=[_changes_requested_pr()]), INFRA_CFG, st,
                         fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


def test_rebase_leg_at_infra_ceiling_escalates_the_pr():
    st = FixState(infra_deaths={9: 2})
    p = pr(100, "ai-pr-open", closing_issue=9, mergeable="CONFLICTING")
    actions = decide_fix(world(prs=[p]), INFRA_CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


def test_pr_leg_below_infra_ceiling_still_dispatches():
    st = FixState(infra_deaths={9: 1})
    actions = decide_fix(world(prs=[_ci_fail_pr()]), INFRA_CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


def test_infra_ceiling_does_not_escalate_a_pr_with_no_work_to_do():
    # A clean, green PR carrying stale infra deaths from an earlier leg must be
    # left alone: the ceiling gates DISPATCH, it is not a standing verdict on
    # the PR. Escalating here would hand a healthy PR to a human for nothing.
    st = FixState(infra_deaths={9: 5})
    p = pr(100, "ai-pr-open", closing_issue=9, checks=(Check("unit", bucket="pass"),))
    actions = decide_fix(world(prs=[p]), INFRA_CFG, st, fixer_busy=False)
    assert actions == []


# ── reap ────────────────────────────────────────────────────────────────────────
def test_reap_requeues_orphaned_issue_to_ready():
    w = world(issues=[issue(8, "ai-working")])
    assert Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_READY) in reap_actions(w)


def test_reap_protects_only_active_issues_while_pool_is_occupied():
    w = world(issues=[
        issue(8, "ai-working"),
        issue(9, "ai-working"),
    ])

    actions = decide_fix(
        w,
        CFG,
        FixState(inflight=(InFlightFix(8, frozenset({"src/a.py"})),)),
        fixer_busy=True,
        dispatch_slots=0,
    )

    assert Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_READY) not in actions
    assert Relabel(Target.ISSUE, 9, Label.AI_WORKING, Label.AI_READY) in actions


def test_reap_ignores_prs_entirely():
    # review M22/D9: the PR arm is deleted, not merely unused. Nothing in the SDK
    # port adds ai-working to a PR, so the arm could only ever fire on a label a
    # HUMAN applied by hand — a reaper that "protects" a busy marker no writer
    # ever sets is a mechanism that reads live and isn't.
    assert reap_actions(world(prs=[pr(101, "ai-working")])) == []
    # Anti-vacuity twin: the ISSUE arm on the same world still fires, so an empty
    # list here means "PRs are out of scope", not "reap_actions stopped working".
    assert reap_actions(world(issues=[issue(8, "ai-working")], prs=[pr(101, "ai-working")])) == [
        Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_READY)
    ]


# re-review P0R-5: the reaper must never send an issue that ALREADY has an open
# PR back through RESOLVE — that opens a second PR for work already up, and the
# resolve leg has no counter on its success path, so nothing bounds the repeat.
def test_reap_repairs_to_pr_open_when_the_issue_already_has_an_open_pr():
    w = world(issues=[issue(8, "ai-working")], prs=[pr(50, "ai-pr-open", closing_issue=8)])
    assert reap_actions(w) == [
        Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_PR_OPEN)
    ]


def test_reap_repairs_via_branch_linkage_not_just_closing_ref():
    # The PR that a died-before-relabel session opened often has no closing-ref
    # yet (the body edit is a separate call), so branch linkage is the case that
    # actually fires in the incident this guards.
    w = world(issues=[issue(8, "ai-working")], prs=[pr(50, head_ref_name="fix/8-thing")])
    assert reap_actions(w) == [
        Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_PR_OPEN)
    ]


def test_reap_still_requeues_when_the_issues_pr_is_closed():
    # Anti-vacuity twin: a CLOSED PR is not work-already-up, so ai-ready remains
    # correct — otherwise this "fix" would strand every issue whose PR a human
    # closed.
    w = world(
        issues=[issue(8, "ai-working")],
        prs=[pr(50, closing_issue=8, state="CLOSED")],
    )
    assert reap_actions(w) == [
        Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_READY)
    ]


def test_reap_does_not_pay_for_a_second_resolve_when_the_relabel_keeps_failing():
    # The loop the re-review reproduced, at the decide level: an issue whose
    # ai-working → ai-pr-open follow-up never landed (swallowed write) is
    # observed with an open PR. Before P0R-5 this world requeued to ai-ready and
    # the NEXT poll dispatched a full paid RESOLVE. Assert on the money: no
    # RunAgent for issue 8 anywhere in the poll.
    w = world(
        issues=[issue(8, "ai-working")],
        prs=[pr(50, "ai-pr-open", closing_issue=8, checks=(Check("unit", bucket="pass"),))],
    )
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_PR_OPEN) in actions
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
    assert Relabel(Target.ISSUE, 8, Label.AI_READY, Label.AI_WORKING) in actions
    assert RunAgent(AgentKind.RESOLVE, 8) in actions

    # …and cleanup's ISSUE-side arm must NOT fire on top of it
    assert RemoveWorktree(8) not in actions
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_READY) not in actions
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
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_READY) in actions
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
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE) in actions
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
    assert Relabel(
        Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE
    ) not in actions
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
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE) in actions


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
    assert Relabel(Target.ISSUE, 8, Label.AI_PR_OPEN, Label.AI_DONE) in actions
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


def test_ignored_checks_do_not_trigger_the_deadline():
    cfg = FixConfig(fix_bot="fix-bot", ci_ignore_checks="^Test \\(windows-latest\\)$")
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
    assert Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_BLOCKED) in actions
    body = next(a.body for a in actions if isinstance(a, PostComment))
    assert "#558" in body and "xnmp" in body
    # ...and specifically NOT the ai-working relabel a dispatch would have made.
    assert Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_WORKING) not in actions


def test_fix_bots_own_open_pr_is_the_normal_in_flight_state_not_a_duplicate():
    # The fixer's own PR closes its issue by construction; guarding on that would
    # block the harness from ever resolving anything.
    w = _guard_world(closing_ref(558, 7, author="fix-bot"))
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 7) in _run_agents(actions)


def test_fix_bot_matched_through_login_eq_not_raw_equality():
    # A FIX_BOT/author pair differing only by the `[bot]` suffix is the SAME
    # identity (review C1) — reading it as a stranger would block every issue.
    w = _guard_world(closing_ref(558, 7, author="Fix-Bot[bot]"))
    assert RunAgent(AgentKind.RESOLVE, 7) in _run_agents(
        decide_fix(w, CFG, FixState(), fixer_busy=False)
    )


def test_unknown_author_on_a_closing_pr_is_treated_as_not_ours():
    # An unresolvable author (deleted account / GraphQL null) still means a PR
    # exists that closes this issue, so the fix is already written. Fail toward
    # not spending: at worst the operator un-blocks in one click.
    w = _guard_world(closing_ref(558, 7, author=""))
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.ISSUE, 7, Label.AI_READY, Label.AI_BLOCKED) in actions


def test_no_closing_refs_collected_dispatches_as_before():
    # Fail-open: an empty closing_refs means "not collected", never "no duplicate
    # exists" — but a guard that refused work on missing data would strand every
    # issue on any world builder that does not fill it.
    actions = decide_fix(_guard_world(), CFG, FixState(), fixer_busy=False)
    assert RunAgent(AgentKind.RESOLVE, 7) in _run_agents(actions)


def test_a_pr_closing_a_different_issue_does_not_block_this_one():
    w = _guard_world(closing_ref(558, 999, author="xnmp"))
    assert RunAgent(AgentKind.RESOLVE, 7) in _run_agents(
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
    # Same single-definition contract as issue_holds / ci_pending_stalls: the
    # journal reads this, decide_fix acts on this, so they cannot disagree.
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
    assert RunAgent(AgentKind.RESOLVE, 7) in _run_agents(
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
              checks=(Check("unit", bucket="fail"),))


def _reruns(actions):
    return [a for a in actions if isinstance(a, RerunFailedChecks)]


def test_first_failure_on_a_head_re_runs_instead_of_paying_for_a_leg():
    w = world(prs=[_red_pr()])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
    assert _reruns(actions) == [RerunFailedChecks(100, "sha-1")]
    assert _run_agents(actions) == []                # the $13–29 never happens


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
        RerunFailedChecks(100, "sha-2")
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


def test_the_re_run_consumes_no_fixer_so_another_pr_can_still_dispatch():
    red = _red_pr()
    other = pr(101, "ai-pr-open", head_ref_oid="sha-9",
               checks=(Check("unit", bucket="fail"),))
    state = FixState(ci_rerun_head={101: "sha-9"})    # #101's re-run is spent
    actions = decide_fix(world(prs=[red, other]), CFG, state, fixer_busy=False)
    assert _reruns(actions) == [RerunFailedChecks(100, "sha-1")]
    assert RunAgent(AgentKind.FIX_CI, 101) in _run_agents(actions)


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


def test_a_head_that_used_its_re_run_and_went_green_is_a_confirmed_flake():
    # The measurement behind #38's metric: a leg that was never bought.
    p = pr(100, "ai-pr-open", head_ref_oid="sha-1",
           checks=(Check("unit", bucket="pass"),))
    state = FixState(ci_rerun_head={100: "sha-1"})
    assert ci_flakes(world(prs=[p]), CFG, state) == (CiFlake(pr=100, head="sha-1"),)


def test_green_without_a_spent_re_run_is_not_a_flake():
    # anti-vacuity twin: an ordinarily-green PR must not inflate the flake count.
    p = pr(100, "ai-pr-open", head_ref_oid="sha-1",
           checks=(Check("unit", bucket="pass"),))
    assert ci_flakes(world(prs=[p]), CFG, FixState()) == ()


def test_a_re_run_head_that_is_still_red_is_not_yet_a_flake():
    p = pr(100, "ai-pr-open", head_ref_oid="sha-1",
           checks=(Check("unit", bucket="fail"),))
    state = FixState(ci_rerun_head={100: "sha-1"})
    assert ci_flakes(world(prs=[p]), CFG, state) == ()
