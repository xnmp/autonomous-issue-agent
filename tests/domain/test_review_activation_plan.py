"""Pure review-rejection family activation planning and shadow comparison; ADR 0015."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.domain.labels import Label
from issue_agent.domain.review import labels_holding_review
from issue_agent.domain.resolve_activation import (
    ActivationBlocked, DeploymentInventory, ExistingActivation, LegacyAbsent, LegacyPresent, LegacyUnreadable,
    UnmappedLegacyEntry,
)
from issue_agent.domain.review_activation import (
    ImportedReviewTarget, LegacyHoldProvenance, LegacyReviewRecord, OpenPullRequestsListed, OpenPullRequestsUnavailable,
    ReviewActivationFacts, ReviewActivationPlan, ReviewFamilyActive, imported_snapshot, plan_review_activation, shadow_compare,
)
from issue_agent.domain.review_rejection import ReviewRejectionCeiling
from issue_agent.domain.review_workflow import (
    CompleteReviews, FreshReviewRequired, ImportedBaseline, ObservedPullRequest, PartialReviews, PullRequestState,
    PullRequestUnavailable, ReviewedBaseline, SubmittedReviewFact, SubmittedReviewState,
)
from issue_agent.domain.review_workflow_policy import ReviewNotOwed, ReviewOwed, review_dispatch_decision
from issue_agent.domain.workflow import GUIDANCE_MAX_CHARS, OpenGate
from issue_agent.domain.workflow_identity import PullRequestRef, WorkflowKey

REPO = "o/r"
BOT = "review-bot[bot]"
HEAD = "a" * 40
OLD = "b" * 40
PATCH = "c" * 64
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
TRIGGER = Label.AI_PR_OPEN


def present(text: str) -> LegacyPresent:
    return LegacyPresent(hashlib.sha256(text.encode()).hexdigest(), text)


def file(value):
    if value is None:
        return LegacyAbsent()
    return value if isinstance(value, (LegacyPresent, LegacyUnreadable, LegacyAbsent)) else present(value)


def record(pr=7, last_sha: object = HEAD, patch_id: object = PATCH, guidance: object = None, root="review"):
    return LegacyReviewRecord(pr, file(last_sha), file(patch_id), file(guidance), root)


_seq = iter(range(10**6))


def review(state="CHANGES_REQUESTED", commit=HEAD, author=BOT, minutes=0, review_id=None):
    return SubmittedReviewFact(review_id or f"r{next(_seq)}", author, SubmittedReviewState(state), commit,
                               NOW - timedelta(hours=1) + timedelta(minutes=minutes))


def observed(pr=7, *reviews, head=HEAD, state=PullRequestState.OPEN, labels=frozenset({TRIGGER}), partial=False,
             at=NOW):
    read = PartialReviews("page 2 failed") if partial else CompleteReviews(tuple(reviews))
    return ObservedPullRequest(WorkflowKey(REPO, PullRequestRef(pr)), head, state, frozenset(labels), read, at)


def unavailable(pr=7):
    return PullRequestUnavailable(WorkflowKey(REPO, PullRequestRef(pr)), NOW, "HTTP 502")


def quiet(**changes) -> DeploymentInventory:
    return replace(DeploymentInventory(REPO), **changes)


def listed(*numbers):
    return OpenPullRequestsListed(tuple(sorted(numbers)))


def plan(entries, reads, *, listing=None, ceiling=3, inventory=None, trigger=TRIGGER):
    open_numbers = tuple(r.key.target.number for r in reads
                         if isinstance(r, ObservedPullRequest) and r.state is PullRequestState.OPEN)
    return plan_review_activation(
        REPO, tuple(entries), tuple(reads), listing if listing is not None else listed(*open_numbers),
        inventory or quiet(), ceiling=ceiling, review_bot=BOT, trigger_label=trigger,
        facts=tuple(ReviewActivationFacts(read.key.target.number, labels_holding_review(read.labels), (), "f" * 64,
                                           LegacyHoldProvenance.CEILING)
                    for read in sorted((r for r in reads if isinstance(r, ObservedPullRequest)
                                        and r.state is PullRequestState.OPEN), key=lambda r: r.key.target.number)),
    )


def reasons(result) -> str:
    assert isinstance(result, ActivationBlocked), result
    return "\n".join(result.reasons)


def only(result) -> ImportedReviewTarget:
    assert isinstance(result, ReviewActivationPlan), result
    assert len(result.imports) == 1, result.imports
    return result.imports[0]


# -- the reviewed baseline --------------------------------------------------------------


def test_a_confirmed_legacy_baseline_imports_and_owes_no_review_at_that_head():
    result = plan([record(guidance="Scope: parser only")], [observed(7, review("APPROVED"))])
    target = only(result)
    assert target.baseline is not None and (target.baseline.head_sha, target.baseline.patch_id) == (HEAD, PATCH)
    assert target.fresh_reason == "" and target.guidance == "Scope: parser only" and target.ceiling is None
    snapshot = imported_snapshot(result, target)
    continuation = snapshot.episode.continuation
    assert isinstance(continuation, ReviewedBaseline) and isinstance(continuation.source, ImportedBaseline)
    assert continuation.source.evidence_id.startswith("review-activation-import:")
    assert (snapshot.revision.fence.generation, snapshot.revision.version, snapshot.gates) == (0, 0, ())
    decision = review_dispatch_decision(snapshot, observed(7, review("APPROVED")), patch_id=None)
    assert isinstance(decision, ReviewNotOwed)  # activation buys no paid review here
    moved = review_dispatch_decision(snapshot, observed(7, head=OLD), patch_id=None)
    assert isinstance(moved, ReviewOwed)  # a moved head with an unknown patch is owed a review


@pytest.mark.parametrize("last_sha,patch_id,reviews,partial,why", [
    (None, PATCH, [review()], False, "no legacy reviewed head"),
    ("", PATCH, [review()], False, "no legacy reviewed head"),
    (HEAD, None, [review()], False, "no legacy reviewed patch identity"),
    (HEAD[:12], PATCH, [review()], False, "not a full commit SHA"),
    (HEAD.upper(), PATCH, [review()], False, "not a full commit SHA"),
    (HEAD, "x" * 64, [review()], False, "not a SHA-256 digest"),
    (HEAD, PATCH[:63], [review()], False, "not a SHA-256 digest"),
    (HEAD, PATCH, [review()], True, "only partially readable"),
    (HEAD, PATCH, [], False, "no review on the pull request"),
    (HEAD, PATCH, [review(author="someone")], False, "no review on the pull request"),
    (HEAD, PATCH, [review("APPROVED"), review("DISMISSED", minutes=5)], False, "latest review was dismissed"),
    (HEAD, PATCH, [review(commit=OLD)], False, "not at the legacy reviewed head"),
    (HEAD, PATCH, [review(commit=None)], False, "not at the legacy reviewed head"),
    (OLD, PATCH, [review(commit=OLD), review(commit=HEAD, minutes=5)], False, "not at the legacy reviewed head"),
])
def test_an_unconfirmed_baseline_owes_a_fresh_review_and_never_guesses(last_sha, patch_id, reviews, partial, why):
    target = only(plan([record(last_sha=last_sha, patch_id=patch_id)], [observed(7, *reviews, partial=partial)]))
    assert target.baseline is None and why in target.fresh_reason
    assert target.ceiling is None


def test_a_baseline_at_an_older_head_imports_and_the_moved_head_is_owed():
    result = plan([record(last_sha=OLD)], [observed(7, review(commit=OLD))])
    target = only(result)
    assert target.baseline is not None and target.baseline.head_sha == OLD
    assert isinstance(review_dispatch_decision(imported_snapshot(result, target), observed(7), patch_id=None), ReviewOwed)


def test_a_fresh_import_is_owed_a_review():
    result = plan([record(last_sha=None, guidance="note")], [observed(7)])
    snapshot = imported_snapshot(result, only(result))
    assert snapshot.episode.continuation == FreshReviewRequired(0)
    assert isinstance(review_dispatch_decision(snapshot, observed(7), patch_id=None), ReviewOwed)


# -- guidance ----------------------------------------------------------------------------


def test_guidance_moves_verbatim_including_whitespace_and_unicode():
    text = "  keep the API stable — café 𝄞\n\t"
    target = only(plan([record(last_sha=None, patch_id=None, guidance=text)], [observed(7)]))
    assert target.guidance == text and target.guidance_source != "absent"


@pytest.mark.parametrize("text,why", [
    ("x" * (GUIDANCE_MAX_CHARS + 1), "exceeds"),
    ("scope <<<OPERATOR_GUIDANCE>>> break out", "fence marker"),
    ("nul\x00byte", "NUL"),
    (" " + "x" * GUIDANCE_MAX_CHARS, "exceeds"),
])
def test_guidance_that_could_never_have_been_accepted_refuses_activation(text, why):
    found = reasons(plan([record(guidance=text), record(8)], [observed(7), observed(8, review())]))
    assert "pull request #7" in found and why in found and "#8" not in found


def test_guidance_at_the_bound_imports():
    target = only(plan([record(last_sha=None, guidance="g" * GUIDANCE_MAX_CHARS)], [observed(7)]))
    assert len(target.guidance) == GUIDANCE_MAX_CHARS


@pytest.mark.parametrize("field", ["last_sha", "patch_id", "guidance"])
def test_an_unreadable_legacy_file_refuses_activation(field):
    found = reasons(plan([record(**{field: LegacyUnreadable("symbolic link")})], [observed(7, review())]))
    assert "pull request #7" in found and "unreadable: symbolic link" in found


# -- sources that cannot be imported ------------------------------------------------------


def test_conflicting_records_across_state_roots_refuse_without_choosing_either():
    found = reasons(plan([record(root="review"), record(last_sha=OLD, root="fix")], [observed(7, review())]))
    assert "ambiguous duplicate legacy records in fix, review" in found


@pytest.mark.parametrize('partial', [record(patch_id=None, root='old'), record(last_sha=None, root='old'), record(root='old')])
def test_compatible_duplicates_use_one_original_verified_baseline_and_bind_all_sources(partial):
    complete = record(guidance='Keep the API stable')
    read = observed(7, review('APPROVED'))
    single = plan([complete], [read])
    combined = plan([partial, complete], [read])
    assert isinstance(combined, ReviewActivationPlan)
    assert combined.source_digest != single.source_digest
    assert (combined.imports[0].baseline, combined.imports[0].guidance) == (single.imports[0].baseline, single.imports[0].guidance)
    assert combined == plan([complete, partial], [read])
    fresh = only(plan([complete, partial], [observed(7)]))
    assert fresh.baseline is None and fresh.fresh_reason


@pytest.mark.parametrize('entries', [
    [record(patch_id=None), record(last_sha=None, root='old')],
    [record(), record(patch_id='d' * 64, root='old')],
    [record(guidance='first'), record(guidance='second', root='old')],
    [record(), record(last_sha=LegacyUnreadable('unreadable'), root='old')],
])
def test_duplicate_fragments_conflicts_and_unreadable_sources_still_refuse(entries):
    assert 'ambiguous duplicate legacy records' in reasons(plan(entries, [observed(7, review('APPROVED'))]))


def test_unmapped_legacy_keys_refuse():
    found = reasons(plan([record(), UnmappedLegacyEntry("pr-abc.last-sha", "not-a-number", "review")],
                         [observed(7, review())]))
    assert "unmapped legacy key 'pr-abc.last-sha'" in found


def test_a_legacy_pull_request_whose_read_failed_refuses():
    found = reasons(plan([record()], [unavailable(7)], listing=listed()))
    assert "pull request #7: its read failed: HTTP 502" in found


def test_a_legacy_pull_request_that_was_never_read_refuses():
    found = reasons(plan([record(9)], [observed(7)], listing=listed(7)))
    assert "pull request #9 was not read" in found


def test_an_incomplete_open_pull_request_listing_refuses():
    found = reasons(plan([record()], [observed(7, review())],
                         listing=OpenPullRequestsUnavailable("listing reached its 500-item limit")))
    assert "not complete: listing reached its 500-item limit" in found


@pytest.mark.parametrize("state", [PullRequestState.CLOSED, PullRequestState.MERGED])
def test_terminal_pull_request_files_are_evidence_only(state):
    reads = [observed(7, review(), state=state), observed(8, review())]
    result = plan([record(7, guidance=LegacyUnreadable("not valid UTF-8")), record(8)], reads, listing=listed(8))
    assert isinstance(result, ReviewActivationPlan)
    assert [target.pr for target in result.imports] == [8]
    without = plan([record(8)], reads, listing=listed(8))
    assert isinstance(without, ReviewActivationPlan) and without.source_digest != result.source_digest


def test_an_open_pull_request_without_legacy_state_or_hold_is_left_to_first_observation():
    result = plan([], [observed(9)])
    assert isinstance(result, ReviewActivationPlan) and result.imports == ()


# -- rejection ceilings --------------------------------------------------------------------


def rejections(count, *, commit=HEAD):
    return [review(commit=commit, minutes=index) for index in range(count)]


def escalation():
    return review("COMMENTED", minutes=30)


def test_a_pull_request_held_at_the_ceiling_imports_its_gate_under_the_runtime_counting_rule():
    counted = rejections(2)
    read = observed(7, review("APPROVED", minutes=-5), *counted, escalation(),
                    labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    result = plan([record()], [read])
    target = only(result)
    assert target.ceiling is not None
    assert [r.review_id for r in target.ceiling.counted] == [r.review_id for r in counted]
    snapshot = imported_snapshot(result, target)
    (gate,) = snapshot.gates
    assert isinstance(gate, OpenGate) and isinstance(gate.cause, ReviewRejectionCeiling)
    assert gate.cause.episode_id == target.episode_id and gate.cause.ceiling == 3 and gate.cause.head_sha == HEAD
    assert gate.cause.review_bot == BOT and gate.opened_generation == 0
    assert target.baseline is not None  # the escalation comment is the bot's latest review at the head
    assert review_dispatch_decision(snapshot, read, patch_id=None).__class__.__name__ == "ReviewHeld"


def test_the_abandoned_escalation_dead_state_imports_its_gate():
    """keifu #173: the escalation stands at the head, but both labels are gone."""
    read = observed(7, *rejections(2), escalation(), labels=frozenset())
    target = only(plan([record()], [read]))
    assert target.ceiling is not None


@pytest.mark.parametrize("entries", [(), (record(),)])
def test_partial_history_of_unlabelled_pr_cannot_erase_a_legacy_ceiling(entries):
    complete = observed(7, *rejections(2), escalation(), labels=frozenset())
    target = only(plan(entries, [complete]))
    assert target.ceiling is not None
    partial = replace(complete, reviews=PartialReviews("second page unavailable"))
    assert "whether a rejection ceiling holds it is unknown" in reasons(plan(entries, [partial]))


def test_a_pull_request_the_reviewer_still_owns_is_not_held_by_activation():
    """At ceiling-1 rejections without an escalation the next review round still decides."""
    read = observed(7, *rejections(2), labels=frozenset({TRIGGER}))
    target = only(plan([record()], [read]))
    assert target.ceiling is None and target.baseline is not None


def test_an_escalation_comment_under_the_trigger_label_is_not_a_hold():
    read = observed(7, *rejections(2), escalation(), labels=frozenset({TRIGGER}))
    assert only(plan([record()], [read])).ceiling is None


def test_a_held_pull_request_below_the_ceiling_keeps_its_hold_external():
    read = observed(7, *rejections(1), escalation(), labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    assert only(plan([record()], [read])).ceiling is None


def test_an_approval_resets_the_count_exactly_as_the_runtime_counts():
    read = observed(7, *rejections(2), review("APPROVED", minutes=10), review(minutes=20), escalation(),
                    labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    assert only(plan([record()], [read])).ceiling is None


def test_a_disabled_ceiling_imports_no_gate_and_ignores_partial_history():
    held = observed(7, *rejections(5), escalation(), labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    assert only(plan([record()], [held], ceiling=0)).ceiling is None
    partial = observed(7, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}), partial=True)
    assert only(plan([record()], [partial], ceiling=0)).baseline is None


def test_a_held_pull_request_with_partial_history_refuses_rather_than_guessing_its_ceiling():
    read = observed(7, labels=frozenset({Label.NEEDS_HUMAN_REVIEW}), partial=True)
    assert "only partially readable" in reasons(plan([record()], [read]))


def test_a_held_pull_request_without_legacy_files_still_imports_its_ceiling():
    read = observed(9, *rejections(2), escalation(), labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    result = plan([], [read])
    target = only(result)
    assert target.pr == 9 and target.ceiling is not None and target.baseline is None
    assert (target.last_sha_source, target.patch_id_source, target.guidance_source) == ("absent",) * 3


def test_ceiling_one_holds_an_escalation_with_nothing_counted():
    read = observed(7, escalation(), labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    target = only(plan([record()], [read], ceiling=1))
    assert target.ceiling is not None and target.ceiling.counted == ()


# -- inventory and existing authority --------------------------------------------------


def test_non_quiescent_inventory_and_retained_authority_refuse():
    found = reasons(plan([record()], [observed(7, review())], inventory=quiet(
        active_runs=("run-1",), open_commands=("cmd-1",), retained_targets=("pr:7",))))
    assert "active runs must be drained" in found and "non-terminal lifecycle commands" in found
    assert "retained without an activation: pr:7" in found


def test_an_activated_family_is_recognised_from_its_record_and_never_replanned():
    first = plan([record()], [observed(7, review())])
    assert isinstance(first, ReviewActivationPlan)
    existing = ExistingActivation(1, first.source_digest, first.inventory_digest, first.imports_digest,
                                  first.ceiling, first.plan_digest)
    # Today's sources (a moved legacy file, a failed read) are evidence only once activated.
    again = plan_review_activation(REPO, (record(last_sha=OLD),), (unavailable(7),), listed(),
                                   quiet(activation=existing, retained_targets=("pr:7",)), ceiling=4,
                                   review_bot=BOT, trigger_label=TRIGGER, recorded=first.imports)
    assert isinstance(again, ReviewFamilyActive) and again.activation == existing and again.imports == first.imports
    with pytest.raises(ValueError, match="disagree"):
        plan_review_activation(REPO, (), (), listed(), quiet(activation=existing), ceiling=3,
                               review_bot=BOT, trigger_label=TRIGGER, recorded=())


# -- digests -----------------------------------------------------------------------------


def test_the_plan_is_deterministic_and_ignores_read_time():
    entries = [record(7), record(8, last_sha=None)]
    reads = [observed(7, review(review_id="x")), observed(8)]
    later = [replace(read, observed_at=NOW + timedelta(minutes=3)) for read in reads]
    first, second = plan(entries, reads), plan(list(reversed(entries)), list(reversed(later)))
    assert isinstance(first, ReviewActivationPlan) and first == second


def test_every_source_change_changes_the_digest_the_operator_confirms():
    base = plan([record()], [observed(7, review(review_id="x"))])
    changes = [
        plan([record(guidance="new note")], [observed(7, review(review_id="x"))]),
        plan([record()], [observed(7, review(review_id="x"), review("APPROVED", review_id="y", minutes=9))]),
        plan([record()], [observed(7, review(review_id="x"), labels=frozenset({TRIGGER, Label.P1}))]),
        plan([record()], [observed(7, review(review_id="x")), observed(8)]),
        plan([record()], [observed(7, review(review_id="x"))], ceiling=4),
        plan([record()], [observed(7, review(review_id="x"))], inventory=quiet(leases=())) ,
    ]
    assert isinstance(base, ReviewActivationPlan)
    digests = {base.plan_digest}
    for changed in changes[:-1]:
        assert isinstance(changed, ReviewActivationPlan)
        digests.add(changed.plan_digest)
    assert len(digests) == 1 + len(changes) - 1
    assert changes[-1] == base  # an identical inventory reproduces the same plan


def test_episode_identity_is_bound_to_repository_pull_request_and_sources():
    one = only(plan([record()], [observed(7, review(review_id="x"))]))
    other = only(plan([record(guidance="n")], [observed(7, review(review_id="x"))]))
    assert one.episode_id.startswith("review-imported:") and one.episode_id != other.episode_id


# -- canonical decisions -----------------------------------------------------------------


def test_import_decisions_round_trip_only_in_canonical_form():
    read = observed(7, *rejections(2), escalation(), labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    target = only(plan([record(guidance="é ✓ 𝄞")], [read]))
    raw = target.canonical()
    assert ImportedReviewTarget.parse(raw) == target
    with pytest.raises(ValueError):
        ImportedReviewTarget.parse(raw.replace('"pr":7', '"pr": 7'))
    with pytest.raises(ValueError):
        ImportedReviewTarget.parse(raw[:-1] + ',"extra":1}')
    with pytest.raises(ValueError):
        ImportedReviewTarget.parse("x" * 10**7)


def test_an_import_must_either_establish_a_baseline_or_say_why_not():
    target = only(plan([record()], [observed(7, review())]))
    with pytest.raises(ValueError):
        replace(target, fresh_reason="both")
    with pytest.raises(ValueError):
        replace(target, baseline=None)


# -- shadow --------------------------------------------------------------------------------


def test_shadow_reports_legacy_and_new_dispatch_and_the_paid_reviews_activation_causes():
    reads = [
        observed(7, review(), labels=frozenset({TRIGGER})),                         # baseline at head
        observed(8, review(commit=OLD), labels=frozenset({TRIGGER})),               # head moved since review
        observed(9, *rejections(2), escalation(), labels=frozenset({Label.NEEDS_HUMAN_REVIEW})),
        observed(10, review(), labels=frozenset({TRIGGER})),                        # reviewed, files lost
        observed(11, state=PullRequestState.CLOSED),
    ]
    entries = [record(7), record(8, last_sha=OLD), record(9), record(11)]
    result = plan(entries, reads, listing=listed(7, 8, 9, 10))
    assert isinstance(result, ReviewActivationPlan)
    rows = {row.pr: row for row in shadow_compare(result, tuple(entries), tuple(reads))}
    assert set(rows) == {7, 8, 9, 10}
    assert (rows[7].legacy_dispatch, rows[7].new_dispatch, rows[7].paid_review_on_activation) == (
        "reviewed", "reviewed", False)
    assert rows[8].agrees and rows[8].paid_review_on_activation
    assert rows[9].ceiling_gate and rows[9].new_dispatch.startswith("held") and rows[9].legacy_dispatch.startswith("held")
    assert rows[10].legacy_dispatch == "reviewed" and rows[10].paid_review_on_activation and not rows[10].agrees


def test_authenticated_counting_facts_survive_the_imported_gate_and_canonical_evidence():
    read = observed(7, *rejections(2), review("APPROVED", minutes=5), escalation(),
                    labels=frozenset({Label.NEEDS_HUMAN_REVIEW}))
    facts = ReviewActivationFacts(7, ("needs-human-review",), (HEAD,), "f" * 64, LegacyHoldProvenance.CEILING)
    result = plan_review_activation(REPO, (record(),), (read,), listed(7), quiet(),
                                    ceiling=3, review_bot=BOT, trigger_label=TRIGGER, facts=(facts,))
    target = only(result)
    assert target.ceiling is not None and target.ceiling.non_resetting_commits == (HEAD,)
    assert ImportedReviewTarget.parse(target.canonical()) == target
    assert imported_snapshot(result, target).gates[0].cause.non_resetting_commits == (HEAD,)


@pytest.mark.parametrize("excluded,expected", [((), "review"), (("draft",), "held: draft"),
                                                (("unproductive review rounds",), "held: unproductive review rounds")])
def test_shadow_uses_the_fresh_runtime_eligibility_for_unlabelled_pull_requests(excluded, expected):
    read = observed(7, labels=frozenset())
    facts = ReviewActivationFacts(7, excluded, (), "f" * 64)
    result = plan_review_activation(REPO, (), (read,), listed(7), quiet(),
                                    ceiling=3, review_bot=BOT, trigger_label=TRIGGER, facts=(facts,))
    assert isinstance(result, ReviewActivationPlan)
    row, = shadow_compare(result, (), (read,))
    assert row.legacy_dispatch == row.new_dispatch == expected


def test_even_unchanged_runtime_decision_is_bound_to_its_evidence_digest():
    read = observed(7)
    first = ReviewActivationFacts(7, (), (), "e" * 64)
    second = replace(first, evidence_digest="f" * 64)
    plans = [plan_review_activation(REPO, (), (read,), listed(7), quiet(),
                                    ceiling=3, review_bot=BOT, trigger_label=TRIGGER, facts=(facts,))
             for facts in (first, second)]
    assert all(isinstance(plan, ReviewActivationPlan) for plan in plans)
    assert plans[0].plan_digest != plans[1].plan_digest


@pytest.mark.parametrize("change", [{"pr": True}, {"excluded": []}, {"excluded": (None,)},
                                     {"non_resetting_commits": ("short",)}, {"evidence_digest": "bad"}])
def test_runtime_facts_validate_external_values(change):
    with pytest.raises(ValueError):
        ReviewActivationFacts(**{"pr": 7, "excluded": (), "non_resetting_commits": (),
                                 "evidence_digest": "f" * 64, **change})


@pytest.mark.parametrize("facts", [None, [], (None,),
    (ReviewActivationFacts(8, (), (), "f" * 64),),
    (ReviewActivationFacts(7, (), (), "f" * 64),) * 2])
def test_import_rejects_malformed_duplicated_or_unobserved_runtime_facts(facts):
    with pytest.raises(ValueError):
        plan_review_activation(REPO, (), (observed(7),), listed(7), quiet(),
                               ceiling=3, review_bot=BOT, trigger_label=TRIGGER, facts=facts)
