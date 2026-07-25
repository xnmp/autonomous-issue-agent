"""Fix-watcher decision behaviour — the poll_once control flow + invariants."""

from builders import Check, Comment, Review, issue, pr, world

from issue_agent.domain.actions import (
    AgentKind,
    CloseIssue,
    EnableAutoMerge,
    UpdateBranch,
    PostComment,
    Relabel,
    RemoveWorktree,
    RunAgent,
    Target,
)
from issue_agent.domain.fix import (
    FixConfig,
    FixState,
    breaker_tripped,
    breaker_key,
    cleanup_actions,
    decide_fix,
    issues_needing_resume,
    pick_ready_issue,
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


def test_pick_ready_skips_issue_overlapping_open_pr_files():
    # #59-vs-#60 collision: an ai-ready issue whose declared paths overlap an OPEN
    # PR's changed files is held back (dispatching races a guaranteed conflict).
    iss = issue(2, "ai-ready", "P2", declared_paths=frozenset({"backend/app/services/x.py"}))
    open_pr = pr(100, "ai-pr-open", state="OPEN",
                 changed_files=frozenset({"backend/app/services/x.py", "other.py"}))
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
    w = world(prs=[_reviewed_pr(commit_id="aaa", head_oid="bbb",
                                checks=[Check("unit", bucket="fail")])])
    actions = decide_fix(w, CFG, FixState(), fixer_busy=False)
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


def test_fix_ci_at_cap_escalates_not_dispatches():
    st = FixState(ci_retries={9: 3})
    actions = decide_fix(world(prs=[_ci_fail_pr()]), CFG, st, fixer_busy=False)
    assert _run_agents(actions) == []
    assert Relabel(Target.PR, 100, Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW) in actions


def test_fix_ci_under_cap_still_dispatches():
    st = FixState(ci_retries={9: 2})
    actions = decide_fix(world(prs=[_ci_fail_pr()]), CFG, st, fixer_busy=False)
    assert RunAgent(AgentKind.FIX_CI, 100) in _run_agents(actions)


# ── reap ────────────────────────────────────────────────────────────────────────
def test_reap_requeues_orphaned_issue_to_ready():
    w = world(issues=[issue(8, "ai-working")])
    assert Relabel(Target.ISSUE, 8, Label.AI_WORKING, Label.AI_READY) in reap_actions(w)


def test_reap_clears_working_on_pr_without_readying_it():
    w = world(prs=[pr(101, "ai-working")])
    acts = reap_actions(w)
    assert Relabel(Target.PR, 101, Label.AI_WORKING, None) in acts
    assert all(getattr(a, "add", None) is not Label.AI_READY for a in acts)


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
