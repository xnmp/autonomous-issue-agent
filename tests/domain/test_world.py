"""Pure world helpers: declared-path extraction + open-PR overlap.

These feed the file-overlap gate (pick_ready_issue): an ai-ready issue whose
body-declared paths overlap an in-flight PR's changed files is held back to avoid
a guaranteed merge conflict (the #59-vs-#60 collision).
"""

from tests.domain.builders import observed_pr

import pytest

from tests.domain.builders import Check, issue, pr, world

from issue_agent.domain.labels import Label

from issue_agent.domain.world import (
    Comment,
    HeadRef,
    HeadSignal,
    PROBE_HEAD_CEILING,
    PRView,
    ProbeSignals,
    extract_declared_paths,
    fix_probe_safe,
    probe_signal_heads,
    normalize_predicted_paths,
    paths_overlap_open_prs,
)


# ── extract_declared_paths ────────────────────────────────────────────────────
def test_extract_picks_backticked_paths():
    body = "Add `backend/app/services/conversation_service.py` and update `services/fakes.py`."
    got = extract_declared_paths(body)
    assert got == frozenset(
        {"backend/app/services/conversation_service.py", "services/fakes.py"}
    )


def test_extract_matches_bare_extension_file():
    assert extract_declared_paths("touch `foo.ts` and `bar.py`") == frozenset(
        {"foo.ts", "bar.py"}
    )


def test_extract_ignores_prose_in_backticks():
    # backticked words that aren't paths (no slash, no source extension) are ignored.
    assert extract_declared_paths("set `status` to `True` in the `handler`") == frozenset()


def test_extract_strips_leading_dot_slash():
    assert extract_declared_paths("see `./backend/app/x.py`") == frozenset(
        {"backend/app/x.py"}
    )


def test_extract_empty_body():
    assert extract_declared_paths("") == frozenset()
    assert extract_declared_paths(None) == frozenset()  # tolerant of None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["src/action.rs", "./src/help.rs"], frozenset({"src/action.rs", "src/help.rs"})),
        (["src/action.rs", 7], frozenset()),
        ([f"src/{n}.py" for n in range(33)], frozenset()),
        (["/etc/passwd"], frozenset()),
        (["src\\windows.py"], frozenset()),
        ([" src/action.rs"], frozenset()),
        ("src/action.rs", frozenset()),
        (None, frozenset()),
    ],
)
def test_normalize_predicted_paths_fails_open_for_untrusted_router_output(raw, expected):
    assert normalize_predicted_paths(raw) == expected


# ── paths_overlap_open_prs ────────────────────────────────────────────────────
def _pr(number, state, files):
    return observed_pr(number=number, state=state, changed_files=frozenset(files))


def test_overlap_true_when_declared_hits_open_pr():
    prs = (_pr(1, "OPEN", {"a.py", "b.py"}),)
    assert paths_overlap_open_prs(frozenset({"b.py"}), prs) is True


def test_overlap_false_when_no_common_file():
    prs = (_pr(1, "OPEN", {"a.py"}),)
    assert paths_overlap_open_prs(frozenset({"z.py"}), prs) is False


def test_overlap_ignores_non_open_prs():
    prs = (_pr(1, "MERGED", {"a.py"}), _pr(2, "CLOSED", {"a.py"}))
    assert paths_overlap_open_prs(frozenset({"a.py"}), prs) is False


def test_overlap_empty_declared_is_failopen():
    prs = (_pr(1, "OPEN", {"a.py"}),)
    assert paths_overlap_open_prs(frozenset(), prs) is False


# A PR escalated to a human (ai-blocked / needs-human-review) is not moving,
# and the wait is unbounded — it must not serialize new work behind it
# (keifu #163/#164 deferred behind escalated PR #181, 2026-08-03). A later
# hand-merge conflict is the REBASE leg's job.
def test_overlap_ignores_blocked_prs():
    blocked = observed_pr(
        number=1,
        state="OPEN",
        changed_files=frozenset({"a.py"}),
        labels=frozenset({Label.AI_PR_OPEN, Label.AI_BLOCKED}),
    )
    assert paths_overlap_open_prs(frozenset({"a.py"}), (blocked,)) is False


def test_overlap_ignores_needs_human_review_prs():
    escalated = observed_pr(
        number=1,
        state="OPEN",
        changed_files=frozenset({"a.py"}),
        labels=frozenset({Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW}),
    )
    assert paths_overlap_open_prs(frozenset({"a.py"}), (escalated,)) is False


def test_overlap_still_holds_on_unblocked_labeled_pr():
    open_pr = observed_pr(
        number=1,
        state="OPEN",
        changed_files=frozenset({"a.py"}),
        labels=frozenset({Label.AI_PR_OPEN}),
    )
    assert paths_overlap_open_prs(frozenset({"a.py"}), (open_pr,)) is True


# ── fix_probe_safe (#3: when may the fixer coast on the cheap signature?) ────
# The 1-point change signature is blind to CI completion and mergeability flips.
# Those only matter while something is in flight, so "nothing in flight" is the
# whole predicate — and it must read OPEN state, not just labels.
def test_probe_safe_on_an_empty_world():
    assert fix_probe_safe(world()) is True


def test_probe_unsafe_while_a_fixer_holds_an_issue():
    assert fix_probe_safe(world(issues=[issue(1, "ai-working")])) is False


def test_probe_unsafe_while_a_harness_pr_is_open():
    # This is the CI/mergeability case: the fixer acts on checks and
    # mergeStateStatus for this PR, neither of which bumps updated_at.
    assert fix_probe_safe(world(prs=[pr(7, "ai-pr-open")])) is False


def test_probe_safe_when_the_labelled_pr_is_already_merged_or_closed():
    # Lifecycle labels outlive the PR; only OPEN ones are in flight.
    merged = world(prs=[pr(7, "ai-pr-open", state="MERGED")])
    closed = world(prs=[pr(8, "ai-pr-open", state="CLOSED")])
    assert (fix_probe_safe(merged), fix_probe_safe(closed)) == (True, True)


def test_probe_safe_when_the_ai_working_issue_is_closed():
    assert fix_probe_safe(world(issues=[issue(1, "ai-working", state="closed")])) is True


def test_probe_safe_with_only_queued_and_finished_work():
    # ai-ready is NOT in flight: labelling it bumped updated_at, so the very
    # signature the probe reads already announced it. ai-done is history.
    w = world(
        issues=[issue(1, "ai-ready"), issue(2, "ai-done")],
        prs=[pr(3, state="OPEN")],  # an unlabelled human PR is not the fixer's
    )
    assert fix_probe_safe(w) is True


# ── #163: the in-flight blind spot, re-read and folded into the gate ─────────
# "An open harness PR exists" was the whole in-flight test, so the fixer never
# probed during a CI wait (~180 full GraphQL builds/hour, the dominant term in
# the 2026-08-02 pool exhaustion). The blind spot there is only the CI and
# mergeability of the in-flight heads, so a reading of exactly those — unchanged
# since the last full build — restores the cheap tick.
def _signal(number, **kw):
    kw.setdefault("head_ref_oid", "sha1")
    kw.setdefault("ci", "build\tCOMPLETED\tpass")
    kw.setdefault("mergeable", "true/clean")
    return HeadSignal(number=number, **kw)


IN_FLIGHT = world(prs=[pr(7, "ai-pr-open", head_ref_oid="sha1")])


def _signals(current, previous):
    return ProbeSignals(current=current, previous=previous)


def test_probe_heads_are_the_open_harness_prs():
    assert probe_signal_heads(IN_FLIGHT) == (HeadRef(7),)


def test_probe_heads_are_empty_when_a_fixer_holds_an_issue():
    # Nothing this reading covers could make that world gateable, so paying for
    # it would be waste.
    held = world(issues=[issue(1, "ai-working")], prs=[pr(7, "ai-pr-open")])
    assert probe_signal_heads(held) == ()


def test_probe_heads_ignore_merged_and_unlabelled_prs():
    other = world(prs=[pr(7, "ai-pr-open", state="MERGED"), pr(8)])
    assert probe_signal_heads(other) == ()


def test_probe_safe_with_an_open_pr_whose_blind_spot_has_not_moved():
    reading = (_signal(7),)
    assert fix_probe_safe(IN_FLIGHT, _signals(reading, reading)) is True


def test_probe_unsafe_when_the_blind_spot_moved():
    # CI completion and mergeability flips bump no updated_at — this comparison
    # is the only witness the fixer has.
    assert fix_probe_safe(
        IN_FLIGHT, _signals((_signal(7, ci="build\tCOMPLETED\tpending"),), (_signal(7),))
    ) is False
    assert fix_probe_safe(
        IN_FLIGHT, _signals((_signal(7, mergeable="false/dirty"),), (_signal(7),))
    ) is False
    assert fix_probe_safe(
        IN_FLIGHT, _signals((_signal(7, head_ref_oid="sha2"),), (_signal(7),))
    ) is False


def test_probe_unsafe_when_the_blind_spot_is_unreadable():
    reading = (_signal(7),)
    assert fix_probe_safe(IN_FLIGHT, _signals(None, reading)) is False
    assert fix_probe_safe(IN_FLIGHT, _signals(reading, None)) is False
    assert fix_probe_safe(IN_FLIGHT, None) is False


def test_probe_unsafe_when_the_reading_does_not_cover_every_open_pr():
    two = world(prs=[pr(7, "ai-pr-open", head_ref_oid="sha1"),
                     pr(8, "ai-pr-open", head_ref_oid="sha1")])
    reading = (_signal(7),)
    assert fix_probe_safe(two, _signals(reading, reading)) is False
    # …and a reading covering a PR the world no longer carries is equally stale.
    wide = (_signal(7), _signal(9))
    assert fix_probe_safe(IN_FLIGHT, _signals(wide, wide)) is False


def test_probe_unsafe_while_a_check_is_failing_even_if_unchanged():
    # A red check's meaning depends on the advisory-UNSTABLE enrichment the
    # reading cannot carry, and it is the state the fixer most needs to act on.
    red = (_signal(7, failing=True),)
    assert fix_probe_safe(IN_FLIGHT, _signals(red, red)) is False


def test_a_held_issue_stays_unsafe_however_quiet_the_blind_spot_is():
    # A leg can finish, die or be cancelled without touching any issue or PR.
    held = world(issues=[issue(1, "ai-working")], prs=[pr(7, "ai-pr-open")])
    reading = (_signal(7),)
    assert fix_probe_safe(held, _signals(reading, reading)) is False


def test_probe_heads_are_empty_past_the_ceiling_and_that_world_is_unsafe():
    """Each head costs three REST calls EVERY tick on the same pool the probe's
    own signature uses; past the ceiling the reading would trade a GraphQL
    exhaustion for a REST one. The tick builds instead — and this world must
    NOT be mistaken for an idle one."""
    many = world(
        prs=[
            pr(n, "ai-pr-open", head_ref_oid="sha1")
            for n in range(1, PROBE_HEAD_CEILING + 2)
        ]
    )
    assert probe_signal_heads(many) == ()
    reading = tuple(_signal(n) for n in range(1, PROBE_HEAD_CEILING + 2))
    assert fix_probe_safe(many, _signals(reading, reading)) is False


def test_probe_heads_stop_at_a_pr_the_last_build_saw_red():
    """A red PR is one the fixer is about to act on, and a red check's meaning
    depends on the advisory enrichment the reading cannot carry — so the reading
    could never gate this world, and buying it every tick is pure waste."""
    red = world(prs=[pr(7, "ai-pr-open", checks=(Check("ci", "COMPLETED", "fail"),))])
    assert probe_signal_heads(red) == ()
    reading = (_signal(7),)
    assert fix_probe_safe(red, _signals(reading, reading)) is False


def test_probe_heads_stop_at_a_pr_whose_checks_were_never_read():
    unread = world(prs=[pr(7, "ai-pr-open", checks=(), checks_known=False)])
    assert probe_signal_heads(unread) == ()
    reading = (_signal(7),)
    assert fix_probe_safe(unread, _signals(reading, reading)) is False


# ── Comment.is_bot (review R2-11: routed through identity.is_bot_login) ──────
def test_comment_is_bot_true_for_bot_suffixed_author():
    assert Comment(author="dependabot[bot]").is_bot is True


def test_comment_is_bot_false_for_human_author():
    # Anti-vacuity twin.
    assert Comment(author="a-human").is_bot is False


def test_comment_is_bot_tolerates_case_and_whitespace():
    # Same tolerance identity.py's login comparisons get everywhere else — a
    # raw `.endswith("[bot]")` (the pre-fix implementation) would miss both.
    assert Comment(author="DEPENDABOT[BOT]").is_bot is True
    assert Comment(author=" dependabot[bot] ").is_bot is True
