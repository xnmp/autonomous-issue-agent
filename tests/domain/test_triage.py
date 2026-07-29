"""Steward triage behaviour — candidate selection, dedupe, no-feedback-loop,
and the two-audience verdict→action mapping."""

from builders import issue, world

from issue_agent.domain.actions import AddDependency, PostComment, Relabel, Target
from issue_agent.domain.labels import Label
from issue_agent.domain.triage import (
    TriageConfig,
    TriageState,
    TriageVerdict,
    actions_for_verdict,
    grooming_candidates,
    grooming_actions,
    is_untriaged,
    parse_codeowners_default,
    recurrence_actions,
    triage_candidates,
)

CFG = TriageConfig(steward_bot="steward-bot", enabled=True)


# ── candidate selection + dedupe ───────────────────────────────────────────────
def test_untriaged_excludes_lifecycle_labels():
    assert is_untriaged(issue(1)) is True
    assert is_untriaged(issue(1, "ai-ready")) is False
    assert is_untriaged(issue(1, "ai-pr-open")) is False
    assert is_untriaged(issue(1, "needs-design")) is False


def test_ai_triage_label_is_not_a_lifecycle_label():
    # ai-triage is the steward's OUTPUT marker; an issue carrying only it is still
    # a candidate (the updatedAt dedupe — not the label — keeps it out).
    assert is_untriaged(issue(1, "ai-triage")) is True


def test_candidates_skip_unchanged_issue():
    w = world(issues=[issue(1, updated_at="t1")])
    st = TriageState(seen_at={1: "t1"})
    assert triage_candidates(w, CFG, st) == []


def test_candidates_include_changed_issue():
    w = world(issues=[issue(1, updated_at="t2")])
    st = TriageState(seen_at={1: "t1"})  # author replied → updatedAt advanced
    assert triage_candidates(w, CFG, st) == [1]


def test_candidates_empty_when_disabled():
    cfg = TriageConfig(steward_bot="steward-bot", enabled=False)
    assert triage_candidates(world(issues=[issue(1)]), cfg, TriageState()) == []


def test_no_feedback_loop_after_recording_post_action_updatedat():
    # The lesson: after the steward comments+relabels (which bump updatedAt to
    # t_post), recording t_post as the baseline means an unchanged issue is NOT
    # re-triaged next pass. Recording the PRE-action value (t1) would re-trigger.
    w = world(issues=[issue(1, "ai-triage", updated_at="t_post")])
    st_correct = TriageState(seen_at={1: "t_post"})
    assert triage_candidates(w, CFG, st_correct) == []
    st_buggy = TriageState(seen_at={1: "t1"})
    assert triage_candidates(w, CFG, st_buggy) == [1]  # the old infinite-bounce


def test_grooming_candidates_skip_unchanged_and_closed_issues():
    w = world(issues=[
        issue(1, updated_at="t1"),
        issue(2, updated_at="t2", state="closed"),
    ])
    assert grooming_candidates(w, {1: "t1"}, True) == []


def test_grooming_candidates_include_changed_issue_and_respect_enabled_flag():
    w = world(issues=[issue(1, updated_at="t2")])
    assert grooming_candidates(w, {1: "t1"}, True) == [1]
    assert grooming_candidates(w, {1: "t1"}, False) == []


def test_grooming_candidates_damp_comment_only_updates_and_cap_first_pass_spend():
    issues = [issue(n, title=f"T{n}", body="unchanged", updated_at="new") for n in range(1, 8)]
    from issue_agent.domain.triage import grooming_content
    seen_content = {item.number: grooming_content(item) for item in issues}
    assert grooming_candidates(world(issues=issues), {}, True, seen_content) == []
    assert grooming_candidates(world(issues=issues), {}, True) == [1, 2, 3, 4, 5]


# ── verdict → actions (audience split) ────────────────────────────────────────
def test_creator_question_is_plain_and_addressed_to_author():
    v = TriageVerdict(verdict="blocked", plain_summary="Need a detail.",
                      creator_questions=("Should it retry or warn?",),
                      acceptance_criteria=("GET /x returns 200",))
    acts = actions_for_verdict(1, "alice", v, CFG)
    comment = next(a for a in acts if isinstance(a, PostComment))
    assert "@alice" in comment.body
    assert "Should it retry or warn?" in comment.body
    assert "For you, @alice" in comment.body
    # no owner_questions → no maintainer section
    assert "For a maintainer" not in comment.body
    # technical AC is WITHHELD from the author-facing comment
    assert "GET /x returns 200" not in comment.body
    assert Relabel(Target.ISSUE, 1, None, Label.AI_TRIAGE) in acts


def test_owner_question_tags_owner_and_marks_maintainer_section():
    cfg = TriageConfig(steward_bot="sb", enabled=True, owner_handle="@org/maintainers")
    v = TriageVerdict(verdict="blocked", plain_summary="Big call needed.",
                      owner_questions=("Add a User to the domain model?",))
    acts = actions_for_verdict(1, "alice", v, cfg)
    comment = next(a for a in acts if isinstance(a, PostComment))
    assert "For a maintainer" in comment.body
    assert "@org/maintainers" in comment.body            # owner tagged
    assert "Add a User to the domain model?" in comment.body
    assert v.needs_owner is True


def test_both_sections_present_when_both_lists_nonempty():
    cfg = TriageConfig(steward_bot="sb", enabled=True, owner_handle="@lead")
    v = TriageVerdict(verdict="blocked",
                      creator_questions=("What should the button say?",),
                      owner_questions=("Introduce a Theme concept?",))
    body = next(a for a in actions_for_verdict(1, "bob", v, cfg)
                if isinstance(a, PostComment)).body
    # owner sees BOTH the creator Q&A and the owner section
    assert "For you, @bob" in body and "What should the button say?" in body
    assert "For a maintainer — @lead" in body and "Introduce a Theme concept?" in body


def test_owner_section_generic_when_no_handle():
    v = TriageVerdict(verdict="blocked", owner_questions=("Pick an auth model?",))
    body = next(a for a in actions_for_verdict(1, "alice", v, CFG)
                if isinstance(a, PostComment)).body
    assert "a maintainer" in body        # generic fallback, no crash


def test_ready_no_promote_keeps_human_in_loop():
    v = TriageVerdict(verdict="ready", plain_summary="Clear.",
                      acceptance_criteria=("GET /x returns 200",))
    acts = actions_for_verdict(1, "alice", v, CFG)
    comment = next(a for a in acts if isinstance(a, PostComment))
    assert "GET /x returns 200" in comment.body  # AC tucked in <details> for engineer
    assert Relabel(Target.ISSUE, 1, None, Label.AI_TRIAGE) in acts
    assert not any(getattr(a, "add", None) is Label.AI_READY for a in acts)


def test_ready_with_promote_flips_to_ai_ready():
    cfg = TriageConfig(steward_bot="steward-bot", enabled=True, promote=True)
    v = TriageVerdict(verdict="ready", plain_summary="Clear.",
                      acceptance_criteria=("GET /x returns 200",))
    acts = actions_for_verdict(1, "alice", v, cfg)
    assert Relabel(Target.ISSUE, 1, Label.AI_TRIAGE, Label.AI_READY) in acts


def test_blocked_verdict_with_no_questions_is_treated_ready():
    # verdict=blocked but both question lists empty → is_blocked False → ready path.
    v = TriageVerdict(verdict="blocked")
    assert v.is_blocked is False and v.needs_owner is False
    acts = actions_for_verdict(1, "alice", v, CFG)
    assert any("Ready to build" in a.body for a in acts if isinstance(a, PostComment))


# ── cross-issue dependency wiring ─────────────────────────────────────────────
def test_blocked_by_emits_add_dependency_and_notes_it():
    v = TriageVerdict(verdict="blocked",
                      creator_questions=("What should it do?",),
                      blocked_by_issues=(42, 43))
    acts = actions_for_verdict(7, "alice", v, CFG)
    assert AddDependency(7, 42) in acts and AddDependency(7, 43) in acts
    body = next(a for a in acts if isinstance(a, PostComment)).body
    assert "Depends on" in body and "#42" in body and "#43" in body


def test_blocked_by_skips_self_ref_and_dedupes():
    v = TriageVerdict(verdict="blocked", creator_questions=("q",),
                      blocked_by_issues=(7, 42, 42))
    deps = [a for a in actions_for_verdict(7, "alice", v, CFG) if isinstance(a, AddDependency)]
    assert deps == [AddDependency(7, 42)]  # self (7) dropped, 42 deduped


def test_ready_with_blocker_does_not_promote():
    # Even a clear spec must NOT auto-promote to ai-ready while a prerequisite is open.
    cfg = TriageConfig(steward_bot="sb", enabled=True, promote=True)
    v = TriageVerdict(verdict="ready", acceptance_criteria=("AC",), blocked_by_issues=(42,))
    acts = actions_for_verdict(7, "alice", v, cfg)
    assert not any(getattr(a, "add", None) is Label.AI_READY for a in acts)
    assert AddDependency(7, 42) in acts


def test_ready_no_blocker_still_promotes():
    cfg = TriageConfig(steward_bot="sb", enabled=True, promote=True)
    v = TriageVerdict(verdict="ready", acceptance_criteria=("AC",))
    acts = actions_for_verdict(7, "alice", v, cfg)
    assert Relabel(Target.ISSUE, 7, Label.AI_TRIAGE, Label.AI_READY) in acts
    assert not any(isinstance(a, AddDependency) for a in acts)


# ── recurrence marking (advisory; harness #10) ────────────────────────────────
MERGED = {42: 300, 43: 301}   # issue → the PR that shipped it


def test_recurrence_names_the_original_and_its_pr_and_adds_the_label():
    v = TriageVerdict(verdict="ready", recurrence_of=42)
    acts = recurrence_actions(7, v, MERGED)
    body = next(a for a in acts if isinstance(a, PostComment)).body
    assert "#42" in body and "PR #300" in body
    assert "recurrence" in body           # tells the human how to undo it
    assert Relabel(Target.ISSUE, 7, None, Label.RECURRENCE) in acts


def test_no_recurrence_claimed_says_nothing():
    assert recurrence_actions(7, TriageVerdict(verdict="ready"), MERGED) == []


def test_a_recurrence_of_an_unmerged_issue_says_nothing():
    # Containment: the classifier reads attacker-writable issue bodies, so it must
    # not be able to label/comment on an issue the harness never shipped.
    v = TriageVerdict(verdict="ready", recurrence_of=999)
    assert recurrence_actions(7, v, MERGED) == []


def test_an_issue_cannot_recur_as_itself():
    v = TriageVerdict(verdict="ready", recurrence_of=42)
    assert recurrence_actions(42, v, MERGED) == []


def test_recurrence_never_reopens_dispatches_or_links():
    """AC: advisory only — the actions are exactly one comment + one label add."""
    acts = recurrence_actions(7, TriageVerdict(verdict="ready", recurrence_of=43), MERGED)
    assert [type(a) for a in acts] == [PostComment, Relabel]


def test_grooming_names_a_verified_merging_pr_without_closing_the_issue():
    acts = grooming_actions(7, resolved_by=42, stale_reference="", merged_by_issue=MERGED)
    body = next(a for a in acts if isinstance(a, PostComment)).body
    assert "#42" in body and "PR #300" in body
    assert Relabel(Target.ISSUE, 7, None, Label.MAYBE_RESOLVED) in acts
    assert [type(a) for a in acts] == [PostComment, Relabel]


def test_grooming_stale_reference_is_advisory_and_does_not_label_or_close():
    acts = grooming_actions(7, resolved_by=None, stale_reference="src/removed.py", merged_by_issue=MERGED)
    assert len(acts) == 1
    assert isinstance(acts[0], PostComment)
    assert "src/removed.py" in acts[0].body


def test_grooming_discards_an_unverified_resolving_issue():
    assert grooming_actions(7, resolved_by=999, stale_reference="", merged_by_issue=MERGED) == []


def test_the_verdict_actions_are_untouched_by_a_recurrence():
    """The two mappings are independent: a recurrence must not change what triage
    would otherwise have done with the issue."""
    plain = TriageVerdict(verdict="ready", plain_summary="Clear.",
                          acceptance_criteria=("AC",))
    recurring = TriageVerdict(verdict="ready", plain_summary="Clear.",
                              acceptance_criteria=("AC",), recurrence_of=42)
    assert actions_for_verdict(7, "alice", recurring, CFG) == \
        actions_for_verdict(7, "alice", plain, CFG)


def test_the_recurrence_label_is_not_a_lifecycle_label():
    # An observation, not a pipeline position: labelling an issue `recurrence`
    # must not make it look triaged nor drop it out of any selection set.
    assert is_untriaged(issue(1, "recurrence")) is True


# ── CODEOWNERS parsing ────────────────────────────────────────────────────────
def test_parse_codeowners_default_takes_star_rule():
    text = "# comment\n*.py  @py-team\n*  @org/maintainers @alice\ndocs/  @doc-team\n"
    assert parse_codeowners_default(text) == "@org/maintainers @alice"


def test_parse_codeowners_last_star_wins():
    assert parse_codeowners_default("*  @a\n*  @b\n") == "@b"


def test_parse_codeowners_none_when_no_star():
    assert parse_codeowners_default("*.py @x\ndocs/ @y\n") == ""
    assert parse_codeowners_default("") == ""
