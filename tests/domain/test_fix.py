"""Fix-watcher decision behaviour — the poll_once control flow + invariants."""

from builders import Check, Comment, Review, issue, pr, world

from issue_agent.domain.actions import (
    AgentKind,
    ClearIssueState,
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
    # same PR. An approved+green PR that went CONFLICTING is both — rebase it AND
    # arm it, so the rebase push lands through auto-merge instead of waiting for a
    # later poll to notice. Idle and busy agree on the arming; only the dispatch
    # (which genuinely needs the lock) differs.
    p = pr(100, "ai-pr-open", mergeable="CONFLICTING",
           reviews=(Review("APPROVED", "review-bot", "t1"),),
           checks=(Check("build", bucket="pass"),))
    idle = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=False)
    busy = decide_fix(world(prs=[p]), CFG, FixState(), fixer_busy=True)
    assert RunAgent(AgentKind.REBASE, 100) in _run_agents(idle)
    assert _run_agents(busy) == []                        # the lock still bounds dispatch
    assert EnableAutoMerge(100, "squash") in idle         # …but not the arming
    assert EnableAutoMerge(100, "squash") in busy


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


def test_cleanup_actions_direct_call_defaults_to_the_ordinary_case():
    # cleanup_actions is also called directly (existing tests above all do
    # this without knowing an issue's current label) — the default must keep
    # exercising the ordinary "issue genuinely awaits cleanup" path so none of
    # those pre-existing direct-call tests silently regressed to a no-op.
    p = pr(100, "ai-pr-open", state="MERGED", head_ref_name="fix/9-x")
    acts = cleanup_actions(p, 9, CFG)
    assert RemoveWorktree(9) in acts
    assert Relabel(Target.ISSUE, 9, Label.AI_PR_OPEN, Label.AI_DONE) in acts
