"""The review daemon's recorded dispatch decision and the admission it shares with dispatch (ADR 0015 operator surface)."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from issue_agent.domain.actions import AgentKind, RunAgent
from issue_agent.domain.identity import IdentityMode
from issue_agent.domain.labels import Label
from issue_agent.domain.review import (
    UNPRODUCTIVE_ROUND_HOLD, BoundReviewState, ReviewAdmitted, ReviewConfig, ReviewExcluded, decide_bound_review,
    review_candidacy, review_eligibility,
)
from issue_agent.domain.review_dispatch_record import (
    CLOSED_PROMPT_SECONDS, CLOSED_RECONFIRM_SECONDS, DECISION_CONFIRM_SECONDS, DEPARTED_CONFIRM_SECONDS,
    MAX_DEPARTED_BACKOFF_SECONDS, AdmissionInFlight, AdmissionWithheld, DecisionBasis, DecisionRead, DecisionUpdate,
    DepartedReadFailed, DepartedReadFailure, RecordedReviewDecision, ReviewDeferred, ReviewInFlight,
    ReviewNotAdmitted, ReviewTerminal, ReviewWithheld, WithheldReason, admitted_decision, departed_backoff_at_ceiling,
    departed_backoff_seconds, departed_read_after, departed_read_due, next_decision_record,
    record_departed_read_failure, record_review_decision,
)
from issue_agent.domain.review_workflow import PullRequestState
from issue_agent.domain.review_workflow_policy import ReviewHeld, ReviewKind, ReviewNotOwed, ReviewOwed
from issue_agent.domain.workflow_identity import IssueRef, PullRequestRef, WorkflowFence, WorkflowKey
from tests.domain.builders import pr, world
from tests.fakes.review_workflow import HEAD, NOW, PR_KEY, held

OPEN = DecisionBasis(HEAD, PullRequestState.OPEN, frozenset({Label.AI_PR_OPEN}), ())
LISTED, DEPARTED = DecisionRead.REVIEW_READ, DecisionRead.DEPARTED_READ
CFG = ReviewConfig(review_bot="review-bot")
ADOPT = ReviewConfig(review_bot="review-bot", adopt_unlabeled=True)


def recorded(decision=ReviewOwed(ReviewKind.RE_REVIEW), basis=OPEN, *, read=LISTED, at=NOW):
    return RecordedReviewDecision(PR_KEY, decision, basis, read, at, at)


# -- write on change, refresh on a bounded cadence ---------------------------------------------------

def test_a_first_decision_is_recorded_as_made_and_confirmed_now():
    record = record_review_decision(None, PR_KEY, ReviewOwed(ReviewKind.INITIAL), OPEN, LISTED, NOW)
    assert record == RecordedReviewDecision(PR_KEY, ReviewOwed(ReviewKind.INITIAL), OPEN, LISTED, NOW, NOW)


def test_an_unchanged_decision_is_not_rewritten_until_its_confirmation_is_due():
    prior = recorded()
    soon = NOW + timedelta(seconds=DECISION_CONFIRM_SECONDS - 1)
    assert record_review_decision(prior, PR_KEY, prior.decision, OPEN, LISTED, soon) is None
    due = NOW + timedelta(seconds=DECISION_CONFIRM_SECONDS)
    refreshed = record_review_decision(prior, PR_KEY, prior.decision, OPEN, LISTED, due)
    assert refreshed is not None and refreshed.as_of == due and refreshed.decided_at == NOW


@pytest.mark.parametrize("decision,basis", [
    (ReviewNotOwed("the reviewed baseline covers this head"), OPEN),
    (ReviewOwed(ReviewKind.RE_REVIEW), replace(OPEN, head_sha="b" * 40)),
    (ReviewOwed(ReviewKind.RE_REVIEW), replace(OPEN, labels=frozenset({Label.AI_PR_OPEN, Label.AI_PARKED}))),
])
def test_any_change_of_decision_or_the_facts_behind_it_is_written_at_once(decision, basis):
    later = NOW + timedelta(seconds=1)
    record = record_review_decision(recorded(), PR_KEY, decision, basis, LISTED, later)
    assert record == RecordedReviewDecision(PR_KEY, decision, basis, LISTED, later, later)


def test_the_same_decision_from_the_pull_requests_own_read_is_written_at_once():
    later = NOW + timedelta(seconds=1)
    prior = recorded(ReviewNotAdmitted(("needs-human-review",)))
    record = record_review_decision(prior, PR_KEY, prior.decision, OPEN, DEPARTED, later)
    assert record == RecordedReviewDecision(PR_KEY, prior.decision, OPEN, DEPARTED, later, later)


def test_an_update_replaces_only_this_pull_requests_record_with_an_aware_time():
    other = WorkflowKey("o/r", PullRequestRef(PR_KEY.target.number + 1))
    with pytest.raises(ValueError):
        record_review_decision(recorded(), other, ReviewOwed(ReviewKind.INITIAL), OPEN, LISTED, NOW)
    with pytest.raises(ValueError):
        record_review_decision(None, PR_KEY, ReviewOwed(ReviewKind.INITIAL), OPEN, LISTED, NOW.replace(tzinfo=None))


# -- a record is internally consistent ---------------------------------------------------------------

@pytest.mark.parametrize("build", [
    lambda: RecordedReviewDecision(PR_KEY, ReviewTerminal(PullRequestState.CLOSED), OPEN, LISTED, NOW, NOW),
    lambda: RecordedReviewDecision(PR_KEY, ReviewOwed(ReviewKind.INITIAL),
                                   replace(OPEN, state=PullRequestState.MERGED), LISTED, NOW, NOW),
    lambda: RecordedReviewDecision(PR_KEY, ReviewTerminal(PullRequestState.MERGED),
                                   replace(OPEN, state=PullRequestState.CLOSED), LISTED, NOW, NOW),
    lambda: RecordedReviewDecision(PR_KEY, ReviewOwed(ReviewKind.INITIAL), OPEN, "review_read", NOW, NOW),  # type: ignore[arg-type]
    lambda: RecordedReviewDecision(PR_KEY, ReviewOwed(ReviewKind.INITIAL), None, LISTED, NOW, NOW),
    lambda: RecordedReviewDecision(PR_KEY, ReviewOwed(ReviewKind.INITIAL), OPEN, LISTED, NOW + timedelta(seconds=1), NOW),
    lambda: RecordedReviewDecision(WorkflowKey("o/r", IssueRef(7)), ReviewOwed(ReviewKind.INITIAL), OPEN, LISTED, NOW, NOW),
    lambda: RecordedReviewDecision(PR_KEY, ReviewHeld(()), OPEN, LISTED, NOW, NOW),
    lambda: RecordedReviewDecision(PR_KEY, ReviewHeld(("gate:a", "gate:a")), OPEN, LISTED, NOW, NOW),
    lambda: RecordedReviewDecision(PR_KEY, ReviewOwed("initial"), OPEN, LISTED, NOW, NOW),  # type: ignore[arg-type]
    lambda: RecordedReviewDecision(PR_KEY, ReviewNotOwed(""), OPEN, LISTED, NOW, NOW),
    lambda: RecordedReviewDecision(PR_KEY, "owed", OPEN, LISTED, NOW, NOW),  # type: ignore[arg-type]
    lambda: ReviewTerminal(PullRequestState.OPEN),
    lambda: ReviewNotAdmitted(()),
    lambda: ReviewNotAdmitted(tuple(f"hold {index}" for index in range(17))),
    lambda: ReviewDeferred(""),
    lambda: ReviewDeferred("x" * 16385),
    lambda: DecisionBasis("not-a-sha", PullRequestState.OPEN, frozenset(), ()),
    lambda: DecisionBasis(HEAD, PullRequestState.OPEN, frozenset({"ai-pr-open"}), ()),  # type: ignore[arg-type]
    lambda: DecisionBasis(HEAD, PullRequestState.OPEN, frozenset(), ("ai-parked", "ai-parked")),
])
def test_contradictory_or_malformed_records_are_refused(build):
    with pytest.raises(ValueError):
        build()


def test_only_a_deferred_decision_may_lack_its_facts_and_a_huge_reason_fits_the_bound():
    RecordedReviewDecision(PR_KEY, ReviewDeferred("x" * 16384), None, LISTED, NOW, NOW)
    RecordedReviewDecision(PR_KEY, ReviewNotAdmitted(tuple(f"hold {index}" for index in range(16))), OPEN, LISTED, NOW, NOW)


# -- a pull request that left the daemon's review read is read on its own, boundedly --------------------

@pytest.mark.parametrize("gated", [True, False])
@pytest.mark.parametrize("record", [
    None,
    recorded(),
    recorded(ReviewNotAdmitted(("needs-human-review",))),
    recorded(ReviewTerminal(PullRequestState.MERGED), replace(OPEN, state=PullRequestState.MERGED)),
])
def test_a_departure_from_the_review_read_is_read_at_once(record, gated):
    # A decision made in the review read is stale the moment the pull request leaves it.
    assert departed_read_due(record, NOW, holds_gates=gated)


CLOSED = replace(OPEN, state=PullRequestState.CLOSED)


@pytest.mark.parametrize("record,gated,wait", [
    (recorded(ReviewNotAdmitted(("needs-human-review",)), read=DEPARTED), False, DEPARTED_CONFIRM_SECONDS),
    (recorded(ReviewHeld(("gate:a",)), read=DEPARTED), True, DEPARTED_CONFIRM_SECONDS),
    # A pull request closed under a day ago still holding gates is re-read on the short cadence, so a reopen
    # finds its lever promptly.
    (recorded(ReviewTerminal(PullRequestState.CLOSED), CLOSED, read=DEPARTED), True, DEPARTED_CONFIRM_SECONDS),
    (recorded(ReviewTerminal(PullRequestState.CLOSED), CLOSED, read=DEPARTED), False, CLOSED_RECONFIRM_SECONDS),
])
def test_after_its_own_read_a_departed_pull_request_is_due_exactly_when_its_record_says(record, gated, wait):
    boundary = NOW + timedelta(seconds=wait)
    assert departed_read_after(record, holds_gates=gated) == boundary
    assert not departed_read_due(record, boundary - timedelta(microseconds=1), holds_gates=gated)
    assert departed_read_due(record, boundary, holds_gates=gated)


def test_a_closed_pull_request_holding_gates_falls_back_to_the_slow_cadence_a_day_after_its_recorded_close():
    closed = recorded(ReviewTerminal(PullRequestState.CLOSED), CLOSED, read=DEPARTED)
    inside = replace(closed, as_of=NOW + timedelta(seconds=CLOSED_PROMPT_SECONDS - 1))
    assert departed_read_after(inside, holds_gates=True) == inside.as_of + timedelta(seconds=DEPARTED_CONFIRM_SECONDS)
    beyond = replace(closed, as_of=NOW + timedelta(seconds=CLOSED_PROMPT_SECONDS))
    assert departed_read_after(beyond, holds_gates=True) == beyond.as_of + timedelta(seconds=CLOSED_RECONFIRM_SECONDS)


def test_reads_of_a_closed_pull_request_holding_gates_are_bounded_over_a_week():
    record = recorded(ReviewTerminal(PullRequestState.CLOSED), CLOSED, read=DEPARTED)
    now, reads, end = NOW, 0, NOW + timedelta(days=7)
    while now < end:
        if departed_read_due(record, now, holds_gates=True):
            record = record_review_decision(record, PR_KEY, record.decision, CLOSED, DEPARTED, now) or record
            reads += 1
        now += timedelta(minutes=5)
    # 96 prompt reads in its first day, then four a day.
    assert reads <= 24 * 4 + 6 * 4 + 1


@pytest.mark.parametrize("gated", [True, False])
def test_a_pull_request_its_own_read_found_merged_is_never_read_again(gated):
    record = recorded(ReviewTerminal(PullRequestState.MERGED), replace(OPEN, state=PullRequestState.MERGED),
                      read=DEPARTED)
    assert departed_read_after(record, holds_gates=gated) is None
    assert not departed_read_due(record, datetime.max.replace(tzinfo=NOW.tzinfo), holds_gates=gated)


# -- failed departed reads back off to a ceiling (ADR 0012) ------------------------------------------------

def test_a_pull_request_that_just_left_the_review_read_fails_into_a_factless_deferral():
    # The review read's decision is stale once the pull request left that read: it is not kept.
    first = record_departed_read_failure(recorded(ReviewOwed(ReviewKind.RE_REVIEW)), PR_KEY, "read failed", NOW)
    assert (first.failed_reads, first.decided_at, first.as_of, first.basis) == (1, NOW, NOW, None)
    assert first.decision == ReviewDeferred("read failed") and first.read is DEPARTED
    assert first.read_failure == DepartedReadFailure(1, "read failed", NOW, NOW)
    later = NOW + timedelta(hours=1)
    second = record_departed_read_failure(first, PR_KEY, "read failed again", later)
    assert (second.failed_reads, second.decided_at, second.as_of) == (2, NOW, later)
    assert second.read_failure == DepartedReadFailure(2, "read failed again", NOW, later)


def test_a_failed_read_keeps_the_last_good_facts_of_its_own_read_and_annotates_them():
    closed = recorded(ReviewTerminal(PullRequestState.CLOSED), CLOSED, read=DEPARTED)
    later = NOW + timedelta(hours=1)
    failed = record_departed_read_failure(closed, PR_KEY, "502", later)
    assert (failed.decision, failed.basis, failed.decided_at, failed.as_of) == (
        closed.decision, CLOSED, NOW, NOW)  # still closed, as of the last good read
    assert failed.read_failure == DepartedReadFailure(1, "502", later, later)
    again = record_departed_read_failure(failed, PR_KEY, "timeout", later + timedelta(hours=1))
    assert again.decision == closed.decision and again.read_failure == DepartedReadFailure(
        2, "timeout", later, later + timedelta(hours=1))
    # The backoff runs from the latest failure, not the facts' age.
    assert departed_read_after(again, holds_gates=True) == later + timedelta(hours=1, seconds=1800)


def test_a_successful_read_ends_the_failure_run_at_once():
    failed = record_departed_read_failure(recorded(ReviewTerminal(PullRequestState.CLOSED), CLOSED, read=DEPARTED),
                                          PR_KEY, "502", NOW + timedelta(seconds=30))
    # Even within the confirmation bound, the annotation is not left standing.
    recovered = record_review_decision(failed, PR_KEY, failed.decision, CLOSED, DEPARTED, NOW + timedelta(seconds=60))
    assert recovered is not None and recovered.read_failure is None and recovered.failed_reads == 0
    assert record_departed_read_failure(recovered, PR_KEY, "read failed", NOW + timedelta(hours=2)).failed_reads == 1


def test_failed_departed_reads_back_off_exponentially_to_the_ceiling():
    waits = [departed_backoff_seconds(n) for n in range(1, 9)]
    assert waits == [900, 1800, 3600, 7200, 14400, 21600, 21600, 21600]
    assert [n for n in range(1, 40) if departed_backoff_at_ceiling(n)] == [6]
    assert departed_backoff_seconds(10**6) == MAX_DEPARTED_BACKOFF_SECONDS
    record = record_departed_read_failure(None, PR_KEY, "read failed", NOW)
    for _ in range(4):
        record = record_departed_read_failure(record, PR_KEY, "read failed", record.as_of)
    assert departed_read_after(record, holds_gates=True) == record.as_of + timedelta(seconds=14400)
    # A closed pull request's kept facts do not shorten the backoff.
    closed = recorded(ReviewTerminal(PullRequestState.CLOSED), CLOSED, read=DEPARTED)
    for index in range(6):
        closed = record_departed_read_failure(closed, PR_KEY, "gone", NOW + timedelta(days=index))
    assert departed_read_after(closed, holds_gates=True) == NOW + timedelta(days=5, seconds=21600)


def test_reads_after_lost_access_are_bounded_over_days():
    record, now, reads = None, NOW, 0
    end = NOW + timedelta(days=3)
    while now < end:
        if departed_read_due(record, now, holds_gates=True):
            record = record_departed_read_failure(record, PR_KEY, "not found", now)
            reads += 1
        now += timedelta(minutes=1)
    # 5 backing-off reads in the first ~7.75 hours, then one per 6 hours.
    assert reads <= 5 + 3 * 24 // 6 + 1


FAILED_ONCE = DepartedReadFailure(1, "x", NOW, NOW)


@pytest.mark.parametrize("build", [
    lambda: RecordedReviewDecision(PR_KEY, ReviewDeferred("x"), None, LISTED, NOW, NOW, read_failure=FAILED_ONCE),
    lambda: RecordedReviewDecision(PR_KEY, ReviewNotAdmitted(("h",)), None, DEPARTED, NOW, NOW,
                                   read_failure=FAILED_ONCE),
    lambda: RecordedReviewDecision(PR_KEY, ReviewDeferred("x"), None, DEPARTED, NOW + timedelta(seconds=1),
                                   NOW + timedelta(seconds=1), read_failure=FAILED_ONCE),
    lambda: RecordedReviewDecision(PR_KEY, ReviewDeferred("x"), None, DEPARTED, NOW, NOW, read_failure=1),
    lambda: DepartedReadFailure(0, "x", NOW, NOW),
    lambda: DepartedReadFailure(10**7, "x", NOW, NOW),
    lambda: DepartedReadFailure(True, "x", NOW, NOW),
    lambda: DepartedReadFailure(1, "", NOW, NOW),
    lambda: DepartedReadFailure(1, "x" * 100_000, NOW, NOW),
    lambda: DepartedReadFailure(1, "x", NOW + timedelta(seconds=1), NOW),
    lambda: DepartedReadFailure(1, "x", NOW.replace(tzinfo=None), NOW),
    lambda: departed_backoff_seconds(0),
    lambda: ReviewInFlight("re_review"),  # type: ignore[arg-type]
    lambda: admitted_decision(ReviewOwed(ReviewKind.INITIAL), ("reserved", "x")),  # type: ignore[arg-type]
    lambda: next_decision_record(None, ("pr", 7), NOW),  # type: ignore[arg-type]
    lambda: ReviewWithheld(ReviewKind.RE_REVIEW, WithheldReason.FIX_CLAIM, ""),
    lambda: ReviewWithheld(ReviewKind.RE_REVIEW, "fix_claim", "held"),  # type: ignore[arg-type]
    lambda: ReviewWithheld("re_review", WithheldReason.RESERVED, "held"),  # type: ignore[arg-type]
])
def test_failure_counts_and_withheld_decisions_are_well_formed(build):
    with pytest.raises(ValueError):
        build()


# -- what dispatch admission did with an owed review ----------------------------------------------------

@pytest.mark.parametrize("admission,expected", [
    (None, ReviewOwed(ReviewKind.RE_REVIEW)),
    (AdmissionInFlight(), ReviewInFlight(ReviewKind.RE_REVIEW)),
    (AdmissionWithheld(WithheldReason.RESERVED, "target 1 is reserved"),
     ReviewWithheld(ReviewKind.RE_REVIEW, WithheldReason.RESERVED, "target 1 is reserved")),
    # An empty or huge detail never makes the record unwritable.
    (AdmissionWithheld(WithheldReason.FIX_CLAIM, ""), ReviewWithheld(ReviewKind.RE_REVIEW, WithheldReason.FIX_CLAIM,
                                                                     "fix_claim")),
    (AdmissionWithheld(WithheldReason.FIX_CLAIM, "x" * 100_000),
     ReviewWithheld(ReviewKind.RE_REVIEW, WithheldReason.FIX_CLAIM, "x" * 16384)),
])
def test_admission_rewrites_only_an_owed_review(admission, expected):
    assert admitted_decision(ReviewOwed(ReviewKind.RE_REVIEW), admission) == expected
    for other in (ReviewHeld(("gate:a",)), ReviewNotOwed("covered"), ReviewTerminal(PullRequestState.CLOSED)):
        assert admitted_decision(other, admission) == other


def test_an_entry_records_a_factless_decision_as_deferred_and_a_failure_against_its_schedule():
    unreadable = next_decision_record(None, DecisionUpdate(PR_KEY, ReviewOwed(ReviewKind.INITIAL), None, LISTED), NOW)
    assert unreadable is not None and isinstance(unreadable.decision, ReviewDeferred) and unreadable.basis is None
    failed = next_decision_record(None, DepartedReadFailed(PR_KEY, "gone"), NOW)
    assert failed is not None and failed.failed_reads == 1 and failed.read is DEPARTED


# -- admission: one predicate for the plan, the record and dispatch ------------------------------------

@pytest.mark.parametrize("labels,cfg,expected", [
    (("ai-pr-open",), CFG, ReviewAdmitted(adopt=False)),
    ((), ADOPT, ReviewAdmitted(adopt=True)),
    (("needs-human-review",), CFG, ReviewExcluded(("needs-human-review",))),
    (("needs-human-review", "ai-parked"), ADOPT, ReviewExcluded(("needs-human-review", "ai-parked"))),
    (("no-ai-review",), ADOPT, ReviewExcluded(("no-ai-review",))),
    ((), CFG, ReviewExcluded(("no ai-pr-open label",))),
])
def test_candidacy_names_why_a_pull_request_is_outside_review(labels, cfg, expected):
    assert review_candidacy(pr(9, *labels), cfg) == expected


def test_adoption_names_a_draft_and_the_review_bots_own_pull_request():
    assert review_candidacy(pr(9, is_draft=True), ADOPT) == ReviewExcluded(("draft",))
    assert review_candidacy(pr(9, author="review-bot"), ADOPT) == ReviewExcluded(("opened by the review bot",))


@pytest.mark.parametrize("labels,rounds,cfg,expected", [
    (("ai-pr-open",), 0, replace(CFG, identity_mode=IdentityMode.SOLO), ReviewExcluded(("solo identity mode",))),
    (("ai-pr-open", "ai-blocked"), 0, CFG, ReviewExcluded(("ai-blocked",))),
    (("ai-pr-open",), CFG.max_unproductive_rounds, CFG, ReviewExcluded((UNPRODUCTIVE_ROUND_HOLD,), breaker=True)),
    (("ai-pr-open",), CFG.max_unproductive_rounds - 1, CFG, ReviewAdmitted(adopt=False)),
])
def test_eligibility_adds_lifecycle_holds_the_breaker_and_the_identity_mode(labels, rounds, cfg, expected):
    assert review_eligibility(pr(9, *labels), cfg, rounds) == expected


def test_an_exclusion_names_at_least_one_hold():
    with pytest.raises(ValueError):
        ReviewExcluded(())


def test_bound_dispatch_buys_a_review_for_exactly_the_pull_requests_admission_admits():
    prs = [pr(1, "ai-pr-open"), pr(2, "ai-pr-open", "ai-blocked"), pr(3, "needs-human-review"), pr(4),
           pr(5, "ai-pr-open"), pr(6, is_draft=True), pr(7, "ai-pr-open", "no-ai-review")]
    rounds = {5: ADOPT.max_unproductive_rounds}
    owed = {p.number: (AgentKind.REVIEW, replace(held().binding, fence=WorkflowFence(
        WorkflowKey("o/r", PullRequestRef(p.number)), 0))) for p in prs}
    actions = decide_bound_review(world(prs=prs), ADOPT, BoundReviewState(owed=owed, unproductive_rounds=rounds))
    dispatched = {action.number for action in actions if isinstance(action, RunAgent)}
    admitted = {p.number for p in prs if isinstance(review_eligibility(p, ADOPT, rounds.get(p.number, 0)),
                                                        ReviewAdmitted)}
    assert dispatched == admitted == {1, 4}
