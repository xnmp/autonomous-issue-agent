"""Behavioural contracts for the pure review-rejection workflow policy (ADR 0015, review slice)."""

from dataclasses import replace
from datetime import timedelta
import random

import pytest

from issue_agent.domain.contracts import CommandId, CommandKind, RunId, WorkflowAttemptIdentity
from issue_agent.domain.labels import Label
from issue_agent.domain.review import count_prior_rejections
from issue_agent.domain.review_workflow import (
    MAX_COUNTED_REVIEWS, BoundReviewSubmission, CancelledReviewRearm, CompleteReviews, CompletedReviewRearm,
    CountedReview, CountingRule, DismissCountedReview, FreshReviewRequired, ObservedPullRequest, PartialReviews, PendingReviewRearm,
    PullRequestAbsent, PullRequestState, PullRequestUnavailable, ReviewEpisode, ReviewedBaseline, ReviewRearmPreflight,
    ReviewRejectionCeiling, ReviewWorkflowSnapshot, SubmittedReviewFact, SubmittedReviewState, SupersededReviewRearm,
)
from issue_agent.domain.review_workflow_policy import (
    CeilingEscalation, DismissalCheck, PullRequestHoldGuard, ReviewDismissalGuard, ReviewHeld, ReviewKind,
    ReviewNotOwed, ReviewOwed, ReviewSubmissionReceipt, ReviewTransitionFacts, reconcile_review_ceiling, cancel_review_rearm, complete_review_rearm, continue_review_rearm, counted_rejections,
    dismissal_check, dismissal_command, initial_review_workflow, open_review_ceiling, pull_request_hold_guard,
    pull_request_hold_guard_applies, record_review, request_review_rearm, review_ceiling_cause,
    review_ceiling_gate_id, review_ceiling_rearm_refusal, review_dismissal_command_id, review_dispatch_decision,
    SubmissionUnresolved, bound_submission_owed,
    review_owed, review_projection_command_id, review_rearm_projection, review_rearm_stop_reason,
    supersede_review_rearm, validate_review_transition,
)
from issue_agent.domain.workflow import (
    MAX_REARM_COMMANDS, MAX_WORKFLOW_GATES, AuthorizeAbsentRetry, AuthorizedOperatorCommand, CancelRearm,
    ClearGuidance, ContinueGrantedRearm, EvidenceUnavailable, KeepGuidance, NoAuthorizedRemediation,
    ObservedTransition, OpenGate, RearmingGate, RearmNeedsDecision, RemoteOutcomeUncertain, RequestRearm,
    ResolveEpisode, SetGuidance,
    StopReason, WorkflowChanged, WorkflowMutation, WorkflowRejected, WorkflowRejection, WorkflowSnapshot,
    WorkflowUnchanged,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
)
from issue_agent.domain.world import Review
from tests.fakes.review_workflow import (
    ACTOR, HEAD, ISSUE_KEY, NOW, OTHER_HEAD, PATCH, PR, PR_KEY, RESOLVE, REVIEW, later, revision,
)

BOT = "review-bot[bot]"
RULE = CountingRule(BOT)
EXECUTOR = "review-owner:1"
TRIGGER = Label.AI_PR_OPEN
THIRD_HEAD = "d" * 40
OTHER_PATCH = "e" * 64
CR = SubmittedReviewState.CHANGES_REQUESTED
APPROVED = SubmittedReviewState.APPROVED
COMMENTED = SubmittedReviewState.COMMENTED
DISMISSED = SubmittedReviewState.DISMISSED


# -- builders -------------------------------------------------------------------

def fact(review_id, state=CR, *, author=BOT, commit=HEAD, at=0):
    return SubmittedReviewFact(review_id, author, state, commit, NOW - timedelta(hours=1) + timedelta(seconds=at))


def rejections(count, *, prefix="r", start=0, author=BOT):
    return tuple(fact(f"{prefix}{index}", author=author, at=start + index) for index in range(count))


def dismissed(reviews, *ids):
    return tuple(replace(r, state=DISMISSED) if r.review_id in ids else r for r in reviews)


def observe(reviews=(), *, head=HEAD, state=PullRequestState.OPEN, labels=(Label.NEEDS_HUMAN_REVIEW,), at=NOW,
            partial=False, key=PR_KEY):
    read = PartialReviews("page 2 failed") if partial else CompleteReviews(tuple(reviews))
    return ObservedPullRequest(key, head, state, frozenset(labels), read, at)


def fresh():
    return initial_review_workflow(PR_KEY, REVIEW)


def escalation(state, head=HEAD, cause="ceiling", *, counted=RULE):
    return CeilingEscalation(BoundReviewSubmission(CommandId("escalate:1"), state.binding), head, "receipt:escalate",
                             cause, counted)


def open_new(state, observation, *, ceiling=3, rule=RULE, proof=None, evidence_ids=()):
    return open_review_ceiling(state, observation, now=NOW, rule=rule, ceiling=ceiling,
                               escalation=proof or escalation(state, observation.head_sha
                                                              if isinstance(observation, ObservedPullRequest) else HEAD,
                                                              counted=rule),
                               trigger_label=TRIGGER, executor_identity=EXECUTOR, evidence_ids=evidence_ids)


def opened(state=None, reviews=None, *, ceiling=3, count=2, **kwargs):
    state = state or fresh()
    reviews = rejections(count) if reviews is None else reviews
    return changed(open_new(state, observe(reviews, **kwargs), ceiling=ceiling))


def reconcile(state, observation, *, ceiling=3, holds=()):
    return reconcile_review_ceiling(state, observation, now=NOW, ceiling=ceiling, evidence_ids=("reconcile:1",),
                                    trigger_label=TRIGGER, executor_identity=EXECUTOR, external_holds=holds)


def gated(**kwargs):
    return opened(**kwargs).snapshot


def ceiling_gate_of(state):
    return next(g for g in state.gates if isinstance(g, OpenGate) and isinstance(g.cause, ReviewRejectionCeiling))


def request_for(state, request_id="request:1", guidance=None, gate_id=None):
    gate_id = gate_id or ceiling_gate_of(state).gate_id
    return RequestRearm(request_id, state.revision, gate_id, guidance or KeepGuidance())


def accept(state, reviews=None, *, command=None, observation=None, holds=(), active=(), prior_request=None):
    observation = observation or observe(rejections(2) if reviews is None else reviews)
    return request_review_rearm(
        state, command or request_for(state), ACTOR, observation, now=NOW, active_reviews=active,
        evidence_id="complete-reviews:1", trigger_label=TRIGGER, executor_identity=EXECUTOR,
        external_holds=holds, prior_request=prior_request,
    )


def accepted(state=None, reviews=None, **kwargs):
    state = state or gated()
    result = accept(state, reviews, **kwargs)
    assert isinstance(result, WorkflowChanged), result
    return result


def complete(snapshot, reviews, *, verified=None, labels=(TRIGGER,), holds=(), ceiling=3, **kwargs):
    intent = snapshot.intent
    verified = intent.required_commands if verified is None else verified
    return complete_review_rearm(snapshot, observe(reviews, labels=labels, **kwargs), verified_commands=verified,
                                 evidence_ids=("reconcile:1",), now=NOW, ceiling=ceiling,
                                 trigger_label=TRIGGER, external_holds=holds)


def stop(state, *, verified=(), uncertainty=(), reason=StopReason.DECISION_CHANGED):
    result = supersede_review_rearm(state, reason, "head-moved:1", verified_commands=verified, uncertainty=uncertainty)
    assert isinstance(result, WorkflowChanged), result
    return result


def carry_on(state, reviews, *, command=None, head=HEAD, holds=(), active=()):
    command = command or ContinueGrantedRearm("request:2", state.revision, state.intent.request_id)
    return continue_review_rearm(
        state, command, ACTOR, observe(reviews, head=head), now=NOW, active_reviews=active,
        evidence_id="complete-reviews:2", trigger_label=TRIGGER, executor_identity=EXECUTOR, external_holds=holds,
    )


def facts(**changes):
    values = dict(now=NOW, rule=RULE, ceiling=3, trigger_label=TRIGGER, executor_identity=EXECUTOR)
    values.update(changes)
    return ReviewTransitionFacts(**values)


def submission(state, command="submit:1"):
    return BoundReviewSubmission(CommandId(command), state.binding)


def receipt(state, head=HEAD, patch=PATCH, *, bound=None, receipt_id="receipt:1"):
    return ReviewSubmissionReceipt(bound or submission(state), head, patch, receipt_id)


def resolve_snapshot():
    return WorkflowSnapshot(revision(key=ISSUE_KEY), RESOLVE, ResolveEpisode("resolve-episode"))


def attempt(state):
    return WorkflowAttemptIdentity(state.binding, RunId("review:1"), "leg:1")


def changed(result):
    assert isinstance(result, WorkflowChanged), result
    return result


def rejected(result, code):
    assert isinstance(result, WorkflowRejected), result
    assert result.code is code, result
    return result


def uncertain_gate(command, gate_id="gate:uncertain"):
    return OpenGate(gate_id, 0, RemoteOutcomeUncertain(CommandId(command), NOW, 3, "timed out"),
                    NoAuthorizedRemediation("reconcile only"))


# -- counted-set selector ----------------------------------------------------------

def as_world(reviews):
    return tuple(Review(r.state.value, r.author, commit_id=r.commit_id or "") for r in reviews)


def random_reviews(rng):
    authors = [BOT, "review-bot", "alice", "Review-Bot[bot]", "mallory[bot]"]
    commits = [None, HEAD, OTHER_HEAD, THIRD_HEAD]
    return tuple(
        fact(f"id-{index}", rng.choice(list(SubmittedReviewState)), author=rng.choice(authors),
             commit=rng.choice(commits), at=index)
        for index in range(rng.randint(0, 40))
    )


@pytest.mark.parametrize("seed", range(400))
def test_counted_selector_matches_count_prior_rejections_on_any_history(seed):
    rng = random.Random(seed)
    reviews = random_reviews(rng)
    non_resetting = frozenset(rng.sample([HEAD, OTHER_HEAD, THIRD_HEAD], rng.randint(0, 3)))
    counted = counted_rejections(CompleteReviews(reviews), CountingRule.of(BOT, non_resetting))
    assert len(counted) == count_prior_rejections(as_world(reviews), BOT, non_resetting_approval_commits=non_resetting)
    by_id = {r.review_id: r for r in reviews}
    # Only the bot's change requests, oldest first, with their commits preserved.
    assert all(by_id[c.review_id].state is CR and by_id[c.review_id].commit_id == c.commit_id for c in counted)
    order = [r.review_id for r in reviews]
    assert [c.review_id for c in counted] == sorted((c.review_id for c in counted), key=order.index)
    # The re-arm contract: dismissing exactly the counted set makes the ceiling read zero.
    after = dismissed(reviews, *(c.review_id for c in counted))
    assert counted_rejections(CompleteReviews(after), CountingRule.of(BOT, non_resetting)) == ()
    assert count_prior_rejections(as_world(after), BOT, non_resetting_approval_commits=non_resetting) == 0


def test_selector_counts_only_the_bots_change_requests_since_its_last_resetting_approval():
    reviews = (
        fact("old", at=0), fact("approve", APPROVED, at=1), fact("human", author="alice", at=2),
        fact("comment", COMMENTED, at=3), fact("gone", DISMISSED, at=4), fact("new", commit=None, at=5),
        fact("bare-login", author="review-bot", at=6),
    )
    assert counted_rejections(CompleteReviews(reviews), RULE) == (
        CountedReview("new", None), CountedReview("bare-login", HEAD),
    )


def test_an_approval_proven_unmergeable_does_not_reset_the_count():
    reviews = (fact("r0", commit=HEAD, at=0), fact("approve", APPROVED, commit=OTHER_HEAD, at=1), fact("r1", at=2))
    assert [c.review_id for c in counted_rejections(CompleteReviews(reviews), CountingRule(BOT, (OTHER_HEAD,)))] == ["r0", "r1"]
    assert [c.review_id for c in counted_rejections(CompleteReviews(reviews), RULE)] == ["r1"]


def test_selector_handles_empty_and_maximal_reads():
    assert counted_rejections(CompleteReviews(()), RULE) == ()
    huge = rejections(512)
    assert len(counted_rejections(CompleteReviews(huge), RULE)) == 512


@pytest.mark.parametrize("reviews,rule", [
    (PartialReviews("page 2 failed"), RULE), ((), RULE), (CompleteReviews(()), BOT), (CompleteReviews(()), None),
])
def test_selector_refuses_partial_or_untyped_inputs(reviews, rule):
    with pytest.raises(ValueError):
        counted_rejections(reviews, rule)


@pytest.mark.parametrize("bot,commits", [
    ("", ()), (None, ()), (BOT, ("short",)), (BOT, [HEAD]), (BOT, (OTHER_HEAD, HEAD)), (BOT, (HEAD, HEAD)),
    (BOT, tuple(f"{index:040x}" for index in range(65))),
])
def test_counting_rule_is_canonical_and_bounded(bot, commits):
    with pytest.raises(ValueError):
        CountingRule(bot, commits)


def test_counting_rule_canonicalizes_configured_commits():
    assert CountingRule.of(BOT, frozenset({OTHER_HEAD, HEAD})) == CountingRule(BOT, (HEAD, OTHER_HEAD))
    assert CountingRule.of(BOT, (OTHER_HEAD, HEAD, HEAD)).non_resetting_commits == (HEAD, OTHER_HEAD)
    with pytest.raises(ValueError):
        CountingRule.of(BOT, [HEAD])


# -- ceiling cause -------------------------------------------------------------------

@pytest.mark.parametrize("ceiling,count,expected", [
    (0, 10, False), (-1, 10, False), (1, 0, True), (2, 0, False), (2, 1, True), (3, 1, False), (3, 2, True),
    (64, 62, False), (64, 63, True), (2**63 - 1, 512, False),
])
def test_cause_exists_once_the_next_rejection_would_escalate(ceiling, count, expected):
    cause = review_ceiling_cause(observe(rejections(count)), episode_id="e", ceiling=ceiling,
                                 rule=CountingRule.of(BOT, (OTHER_HEAD, HEAD)))
    assert (cause is not None) is expected
    if cause is not None:
        assert (cause.head_sha, cause.review_bot, cause.ceiling, len(cause.counted)) == (HEAD, BOT, ceiling, count)
        assert cause.non_resetting_commits == (HEAD, OTHER_HEAD)


@pytest.mark.parametrize("observation", [
    observe(rejections(5), partial=True), PullRequestAbsent(PR_KEY, NOW), PullRequestUnavailable(PR_KEY, NOW, "rate"),
])
def test_only_a_complete_read_can_establish_or_disprove_a_cause(observation):
    with pytest.raises(ValueError):
        review_ceiling_cause(observation, episode_id="e", rule=RULE, ceiling=3)


@pytest.mark.parametrize("ceiling", [True, 1.0, "3", None, 2**63])
def test_malformed_ceiling_is_refused(ceiling):
    with pytest.raises(ValueError):
        review_ceiling_cause(observe(), episode_id="e", rule=RULE, ceiling=ceiling)


# -- initial workflow ----------------------------------------------------------------

def test_initial_workflow_owes_a_first_review_under_a_stable_episode():
    state = fresh()
    assert state.revision == revision(0, 0) and state.gates == () and state.intent is None
    assert state.episode.continuation == FreshReviewRequired(0)
    assert fresh() == state
    assert initial_review_workflow(PR_KEY, FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 2)).episode != state.episode
    assert initial_review_workflow(WorkflowKey("o/r", replace(PR, number=10)), REVIEW).episode != state.episode


@pytest.mark.parametrize("key,authority", [(ISSUE_KEY, REVIEW), (PR_KEY, RESOLVE), (None, REVIEW), (PR_KEY, None)])
def test_initial_workflow_refuses_foreign_targets_and_families(key, authority):
    with pytest.raises(ValueError):
        initial_review_workflow(key, authority)


# -- opening the ceiling gate --------------------------------------------------------

def test_opening_records_the_observed_counted_set_and_head():
    result = opened()
    assert result.mutation is WorkflowMutation.OPEN_GATE and result.prior == fresh()
    assert result.audit == ObservedTransition(("escalate:1", "receipt:escalate"))
    # The visible hold commits with the gate: one guarded trigger -> needs-human-review swap.
    (projection,) = result.commands
    assert projection.kind is CommandKind.RELABEL and projection.workflow == result.snapshot.binding
    assert (projection.payload["remove"], projection.payload["add"]) == (TRIGGER.value, Label.NEEDS_HUMAN_REVIEW.value)
    assert projection.payload["pull_request_hold_guard"] == {"head_sha": HEAD, "labels": [Label.NEEDS_HUMAN_REVIEW.value]}
    gate = ceiling_gate_of(result.snapshot)
    assert gate.cause.counted == (CountedReview("r0", HEAD), CountedReview("r1", HEAD))
    assert gate.cause.episode_id == fresh().episode.episode_id and gate.cause.head_sha == HEAD
    assert gate.gate_id == review_ceiling_gate_id(PR_KEY, gate.cause, revision(0, 1))
    assert result.snapshot.revision == revision(0, 1)
    assert result.snapshot.episode == fresh().episode


def test_opening_is_idempotent_for_the_same_cause():
    state = gated()
    assert open_new(state, observe(rejections(2))) == WorkflowUnchanged(state)
    assert reconcile(state, observe(rejections(2))) == WorkflowUnchanged(state)


def test_a_refreshed_cause_replaces_the_stale_gate_and_keeps_other_gates():
    state = replace(gated(), gates=gated().gates + (uncertain_gate("c1"),))
    rejected(open_new(state, observe(rejections(2), head=OTHER_HEAD)), WorkflowRejection.INVALID_SCOPE)
    result = changed(reconcile(state, observe(rejections(2), head=OTHER_HEAD)))
    assert result.mutation is WorkflowMutation.OPEN_GATE and result.snapshot.episode == state.episode
    ceilings = [g for g in result.snapshot.gates if isinstance(g.cause, ReviewRejectionCeiling)]
    assert len(ceilings) == 1 and ceilings[0].cause.head_sha == OTHER_HEAD
    assert uncertain_gate("c1") in result.snapshot.gates


@pytest.mark.parametrize("observation,code", [
    (observe(rejections(2), partial=True), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (PullRequestAbsent(PR_KEY, NOW), WorkflowRejection.INVALID_SCOPE),
    (PullRequestUnavailable(PR_KEY, NOW, "rate limited"), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (observe(rejections(2), at=NOW - timedelta(seconds=301)), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (observe(rejections(2), at=NOW + timedelta(seconds=1)), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (observe(rejections(2), state=PullRequestState.CLOSED), WorkflowRejection.INVALID_SCOPE),
    (observe(rejections(2), state=PullRequestState.MERGED), WorkflowRejection.INVALID_SCOPE),
    (observe(rejections(2), key=WorkflowKey("o/other", PR)), WorkflowRejection.INVALID_SCOPE),
    (observe(rejections(1)), WorkflowRejection.NOT_EXHAUSTED),
])
def test_opening_refuses_undecidable_foreign_or_unexhausted_observations(observation, code):
    rejected(open_new(fresh(), observation, proof=escalation(fresh())), code)


def test_opening_accepts_the_freshness_boundary():
    changed(open_new(fresh(), observe(rejections(2), at=NOW - timedelta(seconds=300))))


def test_opening_refuses_while_a_rearm_owns_the_workflow_or_gates_are_full():
    rejected(open_new(accepted().snapshot, observe(rejections(2))), WorkflowRejection.REARM_PENDING)
    full = replace(fresh(), gates=tuple(uncertain_gate(f"c{i}", f"gate:{i}") for i in range(MAX_WORKFLOW_GATES)))
    rejected(open_new(full, observe(rejections(2))), WorkflowRejection.OTHER_HOLD)


def test_opening_refuses_untyped_inputs():
    with pytest.raises(ValueError):
        open_new(resolve_snapshot(), observe(), proof=escalation(fresh()))
    with pytest.raises(ValueError):
        open_review_ceiling(fresh(), observe(rejections(2)), now=NOW.replace(tzinfo=None), rule=RULE, ceiling=3,
                            escalation=escalation(fresh()), trigger_label=TRIGGER, executor_identity=EXECUTOR)
    with pytest.raises(ValueError):
        open_new(fresh(), None, proof=escalation(fresh()))
    with pytest.raises(ValueError):
        open_review_ceiling(fresh(), observe(rejections(2)), now=NOW, rule=RULE, ceiling=3, escalation=None,
                            trigger_label=TRIGGER, executor_identity=EXECUTOR)
    with pytest.raises(ValueError):
        open_new(fresh(), observe(rejections(2)), evidence_ids=["e"])


# -- recording a review and the owed obligation --------------------------------------

def test_a_current_generation_submission_establishes_the_baseline_only():
    state = replace(gated(), episode=replace(gated().episode, guidance="keep me"))
    result = record_review(state, receipt(state, OTHER_HEAD))
    assert isinstance(result, WorkflowChanged) and result.mutation is WorkflowMutation.RECORD_REVIEW
    assert result.snapshot.episode.continuation == ReviewedBaseline(OTHER_HEAD, PATCH, 0, submission(state))
    assert result.snapshot.gates == state.gates and result.snapshot.episode.guidance == "keep me"
    assert result.audit == ObservedTransition(("submit:1", "receipt:1"))
    assert record_review(result.snapshot, receipt(state, OTHER_HEAD)) == WorkflowUnchanged(result.snapshot)


def test_an_old_generation_review_cannot_repopulate_the_baseline():
    old = gated()
    stale = submission(old)
    rearmed = complete(accepted(old).snapshot, dismissed(rejections(2), "r0", "r1")).snapshot
    rejected(record_review(rearmed, receipt(old, bound=stale)), WorkflowRejection.INVALID_SCOPE)
    other_episode = BoundReviewSubmission(CommandId("submit:2"), WorkflowBinding(rearmed.revision.fence, REVIEW, "other"))
    rejected(record_review(rearmed, receipt(old, bound=other_episode)), WorkflowRejection.INVALID_SCOPE)
    assert review_owed(rearmed, HEAD, PATCH)


def test_a_review_cannot_be_recorded_while_a_rearm_is_pending():
    pending = accepted().snapshot
    rejected(record_review(pending, receipt(pending)), WorkflowRejection.REARM_PENDING)


@pytest.mark.parametrize("head,patch,receipt_id", [
    ("a" * 39, PATCH, "r"), (HEAD, "c" * 63, "r"), (HEAD, None, "r"), (HEAD, PATCH, ""), (HEAD, PATCH, None),
])
def test_recording_refuses_malformed_review_facts(head, patch, receipt_id):
    with pytest.raises(ValueError):
        record_review(fresh(), receipt(fresh(), head, patch, receipt_id=receipt_id))
    with pytest.raises(ValueError):
        record_review(fresh(), submission(fresh()))


def with_baseline(head=HEAD, patch=PATCH):
    state = fresh()
    return changed(record_review(state, receipt(state, head, patch))).snapshot


@pytest.mark.parametrize("head,patch,owed", [
    (HEAD, PATCH, False), (HEAD, OTHER_PATCH, False), (HEAD, None, False),
    (OTHER_HEAD, PATCH, False), (OTHER_HEAD, OTHER_PATCH, True), (OTHER_HEAD, None, True),
])
def test_same_generation_baseline_allows_head_or_patch_reuse(head, patch, owed):
    assert review_owed(with_baseline(), head, patch) is owed


def test_patch_reuse_never_discharges_a_fresh_review_obligation():
    assert review_owed(fresh(), HEAD, PATCH)
    rearmed = complete(accepted().snapshot, dismissed(rejections(2), "r0", "r1")).snapshot
    assert rearmed.episode.continuation == FreshReviewRequired(1)
    assert review_owed(rearmed, HEAD, PATCH)


@pytest.mark.parametrize("head,patch", [("", PATCH), (HEAD, ""), (HEAD, "C" * 64), (None, PATCH)])
def test_owed_refuses_malformed_heads_and_patches(head, patch):
    with pytest.raises(ValueError):
        review_owed(fresh(), head, patch)


def test_dispatch_holds_before_owing_and_distinguishes_initial_from_re_review():
    assert review_dispatch_decision(None, observe(), patch_id=None) == ReviewOwed(ReviewKind.INITIAL)
    assert isinstance(review_dispatch_decision(None, observe(state=PullRequestState.MERGED), patch_id=None), ReviewNotOwed)
    assert review_dispatch_decision(fresh(), observe(), patch_id=PATCH) == ReviewOwed(ReviewKind.INITIAL)
    state = gated()
    assert review_dispatch_decision(state, observe(), patch_id=None) == ReviewHeld((ceiling_gate_of(state).gate_id,))
    pending = accepted().snapshot
    assert isinstance(review_dispatch_decision(pending, observe(), patch_id=None), ReviewHeld)
    assert isinstance(review_dispatch_decision(with_baseline(), observe(), patch_id=PATCH), ReviewNotOwed)
    assert review_dispatch_decision(with_baseline(), observe(head=OTHER_HEAD), patch_id=OTHER_PATCH) == ReviewOwed(
        ReviewKind.RE_REVIEW)
    rearmed = complete(pending, dismissed(rejections(2), "r0", "r1")).snapshot
    assert review_dispatch_decision(rearmed, observe(), patch_id=PATCH) == ReviewOwed(ReviewKind.RE_REVIEW)
    assert isinstance(review_dispatch_decision(rearmed, observe(state=PullRequestState.CLOSED), patch_id=None),
                      ReviewNotOwed)


def test_an_unresolved_submission_holds_dispatch_after_gates_and_before_everything_else():
    unresolved = (CommandId("verdict-a"),)
    assert review_dispatch_decision(fresh(), observe(), patch_id=PATCH,
                                    unresolved_submissions=unresolved) == SubmissionUnresolved(unresolved)
    # Neither a covered head nor a pending release projection hides the doubt.
    assert review_dispatch_decision(with_baseline(), observe(), patch_id=PATCH, release_pending=CommandId("release"),
                                    unresolved_submissions=unresolved) == SubmissionUnresolved(unresolved)
    state = gated()
    assert isinstance(review_dispatch_decision(state, observe(), patch_id=None, unresolved_submissions=unresolved),
                      ReviewHeld)
    assert review_dispatch_decision(fresh(), observe(), patch_id=PATCH, unresolved_submissions=()) == ReviewOwed(
        ReviewKind.INITIAL)


@pytest.mark.parametrize("ids", [(), ("",), ["verdict-a"], ("x" * 10_000,)])
def test_an_unresolved_submission_hold_names_valid_commands(ids):
    with pytest.raises(ValueError):
        SubmissionUnresolved(ids)


def test_a_bound_submission_is_owed_only_while_its_binding_and_head_still_need_it():
    state = fresh()
    assert bound_submission_owed(state, state.binding, HEAD, PATCH, settled_sibling=False)
    # Another verdict for this binding and head already landed.
    assert not bound_submission_owed(state, state.binding, HEAD, PATCH, settled_sibling=True)
    # The reviewed baseline already covers this head.
    covered = with_baseline()
    assert not bound_submission_owed(covered, covered.binding, HEAD, PATCH, settled_sibling=False)
    assert bound_submission_owed(covered, covered.binding, OTHER_HEAD, OTHER_PATCH, settled_sibling=False)
    # A submission captured under an earlier generation is not the continuation's.
    rearmed = complete(accepted().snapshot, dismissed(rejections(2), "r0", "r1")).snapshot
    assert not bound_submission_owed(rearmed, state.binding, HEAD, PATCH, settled_sibling=False)
    assert bound_submission_owed(rearmed, rearmed.binding, HEAD, PATCH, settled_sibling=False)


@pytest.mark.parametrize("changes", [{"binding": None}, {"settled_sibling": 0}, {"head": ""}, {"patch": "C" * 64}])
def test_bound_submission_applicability_refuses_malformed_facts(changes):
    state = fresh()
    args = {"binding": state.binding, "head": HEAD, "patch": PATCH, "settled_sibling": False} | changes
    with pytest.raises(ValueError):
        bound_submission_owed(state, args["binding"], args["head"], args["patch"],
                              settled_sibling=args["settled_sibling"])


@pytest.mark.parametrize("observation", [
    PullRequestAbsent(PR_KEY, NOW), PullRequestUnavailable(PR_KEY, NOW, "x"), None,
    observe(key=WorkflowKey("o/other", PR)),
])
def test_dispatch_refuses_absent_or_foreign_observations(observation):
    with pytest.raises(ValueError):
        review_dispatch_decision(fresh(), observation, patch_id=None)


# -- operator re-arm request ---------------------------------------------------------

def test_request_authorizes_exactly_the_observed_counted_set():
    state = gated()
    result = accepted(state, command=request_for(state, guidance=SetGuidance("focus on the test")))
    after, intent = result.snapshot, result.snapshot.intent
    assert result.mutation is WorkflowMutation.REQUEST_REARM and result.prior == state
    assert after.revision == revision(1, 2)
    assert after.episode.episode_id == state.episode.episode_id and after.episode.guidance == "focus on the test"
    assert after.episode.continuation == FreshReviewRequired(1)
    assert isinstance(intent, PendingReviewRearm) and intent.authorized == ceiling_gate_of(state).cause.counted
    assert intent.dismissals == intent.authorized
    assert intent.required_commands == (
        review_dismissal_command_id(PR_KEY, "request:1", "r0"), review_dismissal_command_id(PR_KEY, "request:1", "r1"),
        review_projection_command_id(PR_KEY, "request:1"),
    )
    assert tuple(c.command_id for c in result.commands) == intent.required_commands
    assert [c.kind for c in result.commands] == [CommandKind.DISMISS_REVIEW] * 2 + [CommandKind.RELABEL]
    assert all(c.target == "pr:9" and c.workflow == after.binding and c.executor_identity == EXECUTOR
               for c in result.commands)
    assert RearmingGate(ceiling_gate_of(state), "request:1") in after.gates
    preflight = result.audit.preflight
    assert preflight.counted == ceiling_gate_of(state).cause.counted and preflight.head_sha == HEAD
    assert preflight.complete_reviews_evidence_id == "complete-reviews:1"


@pytest.mark.parametrize("edit,expected", [(KeepGuidance(), "old"), (ClearGuidance(), ""), (SetGuidance("new"), "new")])
def test_request_applies_the_guidance_edit(edit, expected):
    state = replace(gated(), episode=replace(gated().episode, guidance="old"))
    assert accepted(state, command=request_for(state, guidance=edit)).snapshot.episode.guidance == expected


def test_already_dismissed_reviews_need_no_command_but_stay_authorized():
    result = accepted(reviews=dismissed(rejections(2), "r0"))
    intent = result.snapshot.intent
    assert [r.review_id for r in intent.dismissals] == ["r1"]
    assert [r.review_id for r in intent.authorized] == ["r0", "r1"]
    assert len(result.commands) == 2


def test_human_reviews_and_uncounted_bot_reviews_never_enter_the_dismissal_set():
    reviews = rejections(2) + (fact("human", author="alice", at=10), fact("note", COMMENTED, at=11))
    result = accepted(reviews=reviews)
    assert {r.review_id for r in result.snapshot.intent.dismissals} == {"r0", "r1"}
    assert all(c.payload.get("review_id") in (None, "r0", "r1") for c in result.commands)


def test_a_later_bot_rejection_outside_the_gate_needs_a_new_ceiling_decision():
    reviews = rejections(2) + (fact("later", at=10),)
    rejected(accept(gated(), reviews), WorkflowRejection.INVALID_SCOPE)


def test_a_moved_head_rejects_the_request():
    state = gated()
    result = rejected(accept(state, observation=observe(rejections(2), head=OTHER_HEAD)), WorkflowRejection.INVALID_SCOPE)
    assert "head moved" in result.reason


def test_ceiling_one_rearms_without_dismissals():
    state = gated(ceiling=1, count=0)
    result = accepted(state, reviews=())
    assert result.snapshot.intent.dismissals == () and result.snapshot.intent.authorized == ()
    assert [c.kind for c in result.commands] == [CommandKind.RELABEL]


def test_a_maximal_intent_fits_one_rearms_durable_obligations():
    reviews = rejections(MAX_COUNTED_REVIEWS)
    state = gated(ceiling=64, count=MAX_COUNTED_REVIEWS, reviews=reviews)
    result = accepted(state, reviews=reviews)
    assert len(result.snapshot.intent.dismissals) == MAX_COUNTED_REVIEWS
    assert len(result.commands) == len(result.snapshot.intent.required_commands) == MAX_REARM_COMMANDS


def test_an_oversize_observed_set_is_a_visible_typed_refusal():
    reviews = rejections(MAX_COUNTED_REVIEWS + 1)
    state = gated(ceiling=64, reviews=reviews)
    refusal = rejected(accept(state, reviews), WorkflowRejection.AUTHORIZATION_BOUND_EXCEEDED)
    assert review_ceiling_rearm_refusal(ceiling_gate_of(state)) == refusal
    assert "64 counted reviews" in refusal.reason
    assert review_ceiling_rearm_refusal(ceiling_gate_of(gated())) is None
    assert review_ceiling_rearm_refusal(uncertain_gate("c")) is None
    huge = rejections(512)
    rejected(accept(gated(ceiling=64, reviews=huge), huge), WorkflowRejection.AUTHORIZATION_BOUND_EXCEEDED)


def foreign_episode_gate():
    state = gated()
    gate = ceiling_gate_of(state)
    cause = gate.cause
    assert isinstance(cause, ReviewRejectionCeiling)
    return replace(state, gates=(replace(gate, cause=replace(cause, episode_id="older-episode")),))


@pytest.mark.parametrize("build,code", [
    (lambda s: dict(command=request_for(s, gate_id="gate:missing")), WorkflowRejection.NOT_EXHAUSTED),
    (lambda s: dict(command=replace(request_for(s), expected=revision(0, 0))), WorkflowRejection.STALE_REVISION),
    (lambda s: dict(command=replace(request_for(s), expected=revision(0, 1, WorkflowKey("o/x", PR)))),
     WorkflowRejection.INVALID_SCOPE),
    (lambda s: dict(active=(attempt(s),)), WorkflowRejection.OTHER_HOLD),
    (lambda s: dict(observation=observe(rejections(2), partial=True)), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (lambda s: dict(observation=PullRequestAbsent(PR_KEY, NOW)), WorkflowRejection.INVALID_SCOPE),
    (lambda s: dict(observation=PullRequestUnavailable(PR_KEY, NOW, "x")), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (lambda s: dict(observation=observe(rejections(2), at=NOW - timedelta(seconds=301))),
     WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (lambda s: dict(observation=observe(rejections(2), state=PullRequestState.MERGED)), WorkflowRejection.INVALID_SCOPE),
    (lambda s: dict(observation=observe(rejections(2), key=WorkflowKey("o/x", PR))), WorkflowRejection.INVALID_SCOPE),
])
def test_request_rejects_every_unauthorized_branch(build, code):
    state = gated()
    rejected(accept(state, **build(state)), code)


def test_request_rejects_foreign_episode_and_non_ceiling_gates():
    state = foreign_episode_gate()
    rejected(accept(state, command=request_for(state, gate_id=state.gates[0].gate_id)), WorkflowRejection.NOT_EXHAUSTED)
    other = replace(fresh(), gates=(uncertain_gate("c1"),))
    rejected(accept(other, command=request_for(other, gate_id="gate:uncertain")), WorkflowRejection.NOT_EXHAUSTED)


def test_request_rejects_pending_wrong_family_and_replays_idempotently():
    pending = accepted().snapshot
    rejected(accept(pending, command=RequestRearm("request:2", pending.revision, "gate", KeepGuidance())),
             WorkflowRejection.REARM_PENDING)
    resolve = resolve_snapshot()
    rejected(request_review_rearm(resolve, request_for(gated()), ACTOR, observe(), now=NOW, active_reviews=(),
                                  evidence_id="e", trigger_label=TRIGGER, executor_identity=EXECUTOR),
             WorkflowRejection.INVALID_SCOPE)
    state = gated()
    command = request_for(state)
    assert accept(state, command=command, prior_request=command) == WorkflowUnchanged(state)
    rejected(accept(state, command=command, prior_request=replace(command, guidance=ClearGuidance())),
             WorkflowRejection.INVALID_SCOPE)
    with pytest.raises(ValueError):
        accept(state, active=[attempt(state)])


# -- projection and dismissal commands -----------------------------------------------

def test_projection_releases_the_hold_and_adds_the_trigger_under_a_semantic_guard():
    result = accepted(observation=observe(rejections(2), labels=(Label.NEEDS_HUMAN_REVIEW, Label.P1)))
    projection = result.commands[-1]
    assert projection.payload["remove"] == Label.NEEDS_HUMAN_REVIEW.value and projection.payload["add"] == TRIGGER.value
    assert projection.payload["pull_request_hold_guard"] == {"head_sha": HEAD, "labels": [Label.NEEDS_HUMAN_REVIEW.value]}
    assert projection.expected_revision == HEAD and projection.idempotency_key == "workflow:request:1"


@pytest.mark.parametrize("extra_gate,holds", [((uncertain_gate("c1"),), ()), ((), ("linked issue is held",))])
def test_projection_keeps_the_attention_hold_while_other_holds_remain(extra_gate, holds):
    state = replace(gated(), gates=gated().gates + extra_gate)
    projection = accepted(state, holds=holds).commands[-1]
    assert projection.payload["remove"] is None and projection.payload["add"] == Label.NEEDS_HUMAN_REVIEW.value


def test_projection_requires_a_pending_rearm_and_an_open_accepted_observation():
    pending = accepted().snapshot
    command = review_rearm_projection(pending, observe(rejections(2)), executor_identity=EXECUTOR)
    assert command == accepted().commands[-1]
    with pytest.raises(ValueError):
        review_rearm_projection(gated(), observe(), executor_identity=EXECUTOR)
    with pytest.raises(ValueError):
        review_rearm_projection(pending, observe(state=PullRequestState.CLOSED),
                                executor_identity=EXECUTOR)
    with pytest.raises(ValueError):
        request_review_rearm(gated(), request_for(gated()), ACTOR, observe(rejections(2)), now=NOW, active_reviews=(),
                             evidence_id="e", trigger_label=Label.NEEDS_HUMAN_REVIEW, executor_identity=EXECUTOR)
    with pytest.raises(ValueError):
        review_rearm_projection(pending, observe(key=WorkflowKey("o/x", PR)),
                                executor_identity=EXECUTOR)


def test_hold_guard_ignores_harmless_activity_and_its_own_partial_effect():
    pr = observe(labels=(Label.NEEDS_HUMAN_REVIEW,))
    guard = pull_request_hold_guard(pr)
    own = dict(removed=(Label.NEEDS_HUMAN_REVIEW,), added=(TRIGGER,))
    assert pull_request_hold_guard_applies(guard, pr, **own)
    assert pull_request_hold_guard_applies(guard, observe(labels=(TRIGGER,)), **own)
    assert pull_request_hold_guard_applies(guard, observe(labels=(Label.NEEDS_HUMAN_REVIEW, Label.P2)), **own)
    assert pull_request_hold_guard_applies(guard, observe(labels=(Label.NEEDS_HUMAN_REVIEW,), partial=True), **own)
    assert not pull_request_hold_guard_applies(guard, observe(head=OTHER_HEAD), **own)
    assert not pull_request_hold_guard_applies(guard, observe(state=PullRequestState.CLOSED), **own)
    assert not pull_request_hold_guard_applies(guard, observe(labels=(Label.NEEDS_HUMAN_REVIEW, Label.AI_PARKED)), **own)
    assert not pull_request_hold_guard_applies(guard, observe(labels=(TRIGGER,)))
    with pytest.raises(ValueError):
        pull_request_hold_guard(observe(state=PullRequestState.MERGED))
    with pytest.raises(ValueError):
        PullRequestHoldGuard(HEAD, frozenset({Label.P1}))
    with pytest.raises(ValueError):
        pull_request_hold_guard_applies(guard, PullRequestAbsent(PR_KEY, NOW))


@pytest.mark.parametrize("label, refuses", [
    (Label.RECURRENCE, False), (Label.MAYBE_RESOLVED, False), (Label.AI_TRIAGE, False), (Label.P1, False),
    (Label.NO_AI_REVIEW, True), (Label.AI_PARKED, True), (Label.AI_BLOCKED, True), (Label.NEEDS_BUDGET_REVIEW, True),
    (Label.AI_REVIEW, True), (Label.AI_DONE, True),
])
def test_hold_guard_is_refused_only_by_a_hold_fact_never_by_an_advisory_label(label, refuses):
    guard = pull_request_hold_guard(observe(labels=(TRIGGER, Label.RECURRENCE)))
    assert guard.labels == frozenset({TRIGGER})  # the advisory label is not a guarded fact
    later = observe(labels=(TRIGGER, label))
    assert pull_request_hold_guard_applies(guard, later) is not refuses
    with pytest.raises(ValueError):
        PullRequestHoldGuard(HEAD, frozenset({Label.RECURRENCE}))


def test_dismissal_commands_are_deterministic_and_fenced_by_binding_and_review():
    first, second = accepted(), accepted()
    assert first.commands == second.commands
    command = first.commands[0]
    assert command.command_id == review_dismissal_command_id(PR_KEY, "request:1", "r0")
    assert dict(command.payload) == {
        "pr": 9, "review_id": "r0", "message": command.payload["message"],
        "guard": {"review_id": "r0", "author": BOT, "head_sha": HEAD, "state": "CHANGES_REQUESTED"},
    }
    assert command.expected_revision == HEAD
    assert review_dismissal_command_id(PR_KEY, "request:2", "r0") != command.command_id
    assert review_dismissal_command_id(WorkflowKey("o/r", replace(PR, number=10)), "request:1", "r0") != command.command_id
    pending = first.snapshot
    with pytest.raises(ValueError):
        dismissal_command(pending, CountedReview("human", HEAD), head_sha=HEAD, executor_identity=EXECUTOR)
    with pytest.raises(ValueError):
        dismissal_command(gated(), CountedReview("r0", HEAD), head_sha=HEAD, executor_identity=EXECUTOR)
    with pytest.raises(ValueError):
        review_dismissal_command_id(ISSUE_KEY, "request:1", "r0")


@pytest.mark.parametrize("reviews,expected", [
    ((fact("r0"),), DismissalCheck.APPLIES),
    ((fact("r0", author="review-bot"),), DismissalCheck.APPLIES),
    ((fact("r0", DISMISSED),), DismissalCheck.POSTCONDITION_PRESENT),
    ((), DismissalCheck.REFUSED),
    ((fact("r1"),), DismissalCheck.REFUSED),
    ((fact("r0", author="alice"),), DismissalCheck.REFUSED),
    ((fact("r0", APPROVED),), DismissalCheck.REFUSED),
    ((fact("r0", COMMENTED),), DismissalCheck.REFUSED),
])
def test_an_absent_review_is_a_refusal_never_a_dismissal(reviews, expected):
    assert dismissal_check(ReviewDismissalGuard("r0", BOT, HEAD), observe(reviews)) is expected


def test_a_moved_head_or_terminal_pr_is_a_changed_dismissal_guard():
    guard = ReviewDismissalGuard("r0", BOT, HEAD)
    assert dismissal_check(guard, observe((fact("r0"),), head=OTHER_HEAD)) is DismissalCheck.REFUSED
    assert dismissal_check(guard, observe((fact("r0"),), state=PullRequestState.CLOSED)) is DismissalCheck.REFUSED
    # An effect already present is reconciled without a write wherever the head is.
    assert dismissal_check(guard, observe((fact("r0", DISMISSED),), head=OTHER_HEAD)) is DismissalCheck.POSTCONDITION_PRESENT


def test_dismissal_check_refuses_partial_or_absent_reads():
    with pytest.raises(ValueError):
        dismissal_check(ReviewDismissalGuard("r0", BOT, HEAD), observe((fact("r0"),), partial=True))
    with pytest.raises(ValueError):
        dismissal_check(ReviewDismissalGuard("r0", BOT, HEAD), PullRequestAbsent(PR_KEY, NOW))
    with pytest.raises(ValueError):
        ReviewDismissalGuard("r0", BOT, "short")


# -- cancel and supersede ------------------------------------------------------------

def test_cancellation_retains_authorization_and_opens_a_decision_gate():
    state = replace(accepted().snapshot, gates=accepted().snapshot.gates + (uncertain_gate("c-other"),))
    intent = state.intent
    uncertainty = RemoteOutcomeUncertain(intent.required_commands[1], NOW, 1, "timed out")
    command = CancelRearm("cancel:1", state.revision, "request:1", "wrong PR")
    result = cancel_review_rearm(state, command, ACTOR, verified_commands=intent.required_commands[:1],
                                 uncertainty=(uncertainty,))
    assert isinstance(result, WorkflowChanged) and result.mutation is WorkflowMutation.STOP_REARM
    stopped = result.snapshot.intent
    assert isinstance(stopped, CancelledReviewRearm) and stopped.authorized == intent.authorized
    assert stopped.verified_commands == intent.required_commands[:1]
    assert stopped.retired_obligations == intent.required_commands[1:]
    decisions = [g for g in result.snapshot.gates if isinstance(g.cause, RearmNeedsDecision)]
    assert len(decisions) == 1 and decisions[0].cause.obligations == intent.required_commands[1:]
    assert not any(isinstance(g, RearmingGate) for g in result.snapshot.gates)
    assert uncertain_gate("c-other") in result.snapshot.gates
    assert any(g.cause == uncertainty for g in result.snapshot.gates)
    assert result.snapshot.episode == state.episode
    assert result.snapshot.revision.fence.generation == state.revision.fence.generation


def test_cancellation_and_supersession_reject_mismatched_evidence():
    state = accepted().snapshot
    intent = state.intent
    rejected(cancel_review_rearm(state, CancelRearm("c", revision(1, 1), "request:1", "x"), ACTOR),
             WorkflowRejection.STALE_REVISION)
    rejected(cancel_review_rearm(state, CancelRearm("c", state.revision, "request:9", "x"), ACTOR),
             WorkflowRejection.INVALID_SCOPE)
    rejected(cancel_review_rearm(gated(), CancelRearm("c", gated().revision, "request:1", "x"), ACTOR),
             WorkflowRejection.INVALID_SCOPE)
    rejected(supersede_review_rearm(state, StopReason.DECISION_CHANGED, "e", verified_commands=(CommandId("foreign"),)),
             WorkflowRejection.INVALID_SCOPE)
    first = intent.required_commands[0]
    rejected(supersede_review_rearm(state, StopReason.DECISION_CHANGED, "e", verified_commands=(first, first)),
             WorkflowRejection.INVALID_SCOPE)
    clash = RemoteOutcomeUncertain(first, NOW, 1, "x")
    rejected(supersede_review_rearm(state, StopReason.DECISION_CHANGED, "e", verified_commands=(first,),
                                    uncertainty=(clash,)), WorkflowRejection.INVALID_SCOPE)
    rejected(supersede_review_rearm(state, StopReason.DECISION_CHANGED, "e",
                                    uncertainty=(RemoteOutcomeUncertain(CommandId("foreign"), NOW, 1, "x"),)),
             WorkflowRejection.INVALID_SCOPE)
    with pytest.raises(ValueError):
        supersede_review_rearm(state, StopReason.OPERATOR_CANCELLED, "e")
    with pytest.raises(ValueError):
        supersede_review_rearm(state, StopReason.DECISION_CHANGED, "e", uncertainty=("not typed",))


@pytest.mark.parametrize("reason", [StopReason.TARGET_TERMINAL, StopReason.DECISION_CHANGED,
                                    StopReason.REQUIRED_EFFECT_REJECTED])
def test_every_observed_stop_reason_supersedes(reason):
    stopped = stop(accepted().snapshot, reason=reason).snapshot.intent
    assert isinstance(stopped, SupersededReviewRearm) and stopped.reason is reason


def test_stop_reason_derives_from_head_state_and_holds_even_on_a_partial_read():
    preflight = accepted().audit.preflight
    assert review_rearm_stop_reason(preflight, observe(partial=True)) is None
    assert review_rearm_stop_reason(preflight, observe(head=OTHER_HEAD, partial=True)) is StopReason.DECISION_CHANGED
    assert review_rearm_stop_reason(preflight, observe(state=PullRequestState.MERGED, partial=True)) is (
        StopReason.TARGET_TERMINAL)
    assert review_rearm_stop_reason(preflight, observe(), external_holds=("new hold",)) is StopReason.DECISION_CHANGED
    with pytest.raises(ValueError):
        review_rearm_stop_reason(preflight, observe(key=WorkflowKey("o/x", PR)))
    with pytest.raises(ValueError):
        review_rearm_stop_reason(preflight, PullRequestUnavailable(PR_KEY, NOW, "x"))


# -- continuation --------------------------------------------------------------------

def stopped_after_first_dismissal():
    pending = accepted().snapshot
    return stop(pending, verified=pending.intent.required_commands[:1]).snapshot


def test_continuation_replans_only_authorized_reviews_still_active():
    state = stopped_after_first_dismissal()
    result = carry_on(state, dismissed(rejections(2), "r0"))
    assert isinstance(result, WorkflowChanged) and result.mutation is WorkflowMutation.CONTINUE_REARM
    intent = result.snapshot.intent
    assert [r.review_id for r in intent.dismissals] == ["r1"]
    assert intent.authorized == state.intent.authorized
    assert result.snapshot.revision.fence.generation == state.revision.fence.generation + 1
    assert result.snapshot.episode.continuation == FreshReviewRequired(result.snapshot.revision.fence.generation)
    assert result.snapshot.episode.episode_id == state.episode.episode_id
    decision = next(g for g in state.gates if isinstance(g.cause, RearmNeedsDecision))
    assert RearmingGate(decision, "request:2") in result.snapshot.gates
    assert [c.command_id for c in result.commands] == list(intent.required_commands)


def test_continuation_allows_a_moved_head_within_the_stopped_authorization():
    result = carry_on(stopped_after_first_dismissal(), dismissed(rejections(2), "r0"), head=OTHER_HEAD)
    assert isinstance(result, WorkflowChanged)
    assert result.audit.preflight.head_sha == OTHER_HEAD


def test_continuation_rejects_reviews_outside_the_stopped_authorization():
    reviews = rejections(2) + (fact("later", at=10),)
    rejected(carry_on(stopped_after_first_dismissal(), reviews), WorkflowRejection.INVALID_SCOPE)


def test_continuation_keeps_unresolved_prior_commands_held():
    pending = accepted().snapshot
    uncertainty = RemoteOutcomeUncertain(pending.intent.required_commands[0], NOW, 4, "timed out")
    state = stop(pending, uncertainty=(uncertainty,)).snapshot
    continued = carry_on(state, rejections(2))
    assert isinstance(continued, WorkflowChanged)
    assert any(isinstance(g, OpenGate) and g.cause == uncertainty for g in continued.snapshot.gates)
    after = continued.snapshot
    done = changed(complete(after, dismissed(rejections(2), "r0", "r1"), labels=(TRIGGER,)))
    # Completion does not wait on covered uncertainty, but its gate still holds
    # dispatch until outcome reconciliation resolves the prior effect.
    assert any(isinstance(g, OpenGate) and g.cause == uncertainty for g in done.snapshot.gates)
    assert isinstance(review_dispatch_decision(done.snapshot, observe(labels=(TRIGGER,)), patch_id=None), ReviewHeld)


@pytest.mark.parametrize("build,code", [
    (lambda s: dict(command=ContinueGrantedRearm("request:2", revision(1, 1), "request:1")),
     WorkflowRejection.STALE_REVISION),
    (lambda s: dict(command=ContinueGrantedRearm("request:2", s.revision, "request:9")), WorkflowRejection.INVALID_SCOPE),
    (lambda s: dict(active=(attempt(s),)), WorkflowRejection.OTHER_HOLD),
])
def test_continuation_rejects_stale_foreign_or_busy_requests(build, code):
    state = stopped_after_first_dismissal()
    rejected(carry_on(state, dismissed(rejections(2), "r0"), **build(state)), code)


def test_continuation_rejects_undecidable_or_terminal_observations_and_missing_gates():
    state = stopped_after_first_dismissal()
    command = ContinueGrantedRearm("request:2", state.revision, "request:1")
    for observation, code in [
        (observe(rejections(2), partial=True), WorkflowRejection.OBSERVATION_UNAVAILABLE),
        (observe(rejections(2), state=PullRequestState.CLOSED), WorkflowRejection.INVALID_SCOPE),
        (PullRequestAbsent(PR_KEY, NOW), WorkflowRejection.INVALID_SCOPE),
    ]:
        rejected(continue_review_rearm(state, command, ACTOR, observation, now=NOW, active_reviews=(),
                                       evidence_id="e", trigger_label=TRIGGER, executor_identity=EXECUTOR), code)
    with pytest.raises(ValueError, match="exactly for the current stopped re-arm"):
        replace(state, gates=())  # a stopped re-arm without its decision gate is unrepresentable
    pending = accepted().snapshot
    rejected(carry_on(pending, rejections(2), command=ContinueGrantedRearm("request:2", pending.revision, "request:1")),
             WorkflowRejection.REARM_PENDING)
    rejected(carry_on(gated(), rejections(2), command=ContinueGrantedRearm("request:2", gated().revision, "request:1")),
             WorkflowRejection.INVALID_SCOPE)


# -- completion ----------------------------------------------------------------------

def test_completion_requires_verified_obligations_and_a_false_cause():
    pending = accepted().snapshot
    result = complete(pending, dismissed(rejections(2), "r0", "r1"))
    assert isinstance(result, WorkflowChanged) and result.mutation is WorkflowMutation.RECONCILE_REARM
    assert isinstance(result.snapshot.intent, CompletedReviewRearm)
    assert result.snapshot.gates == () and result.snapshot.episode == pending.episode
    assert complete_review_rearm(gated(), observe(), verified_commands=(), evidence_ids=("e",), now=NOW,
                                 ceiling=3, trigger_label=TRIGGER) == WorkflowUnchanged(gated())


def test_completion_rejects_unverified_or_ineffective_dismissals():
    pending = accepted().snapshot
    done = dismissed(rejections(2), "r0", "r1")
    required = pending.intent.required_commands
    rejected(complete(pending, done, verified=required[:-1]), WorkflowRejection.OBSERVATION_UNAVAILABLE)
    rejected(complete(pending, done, verified=required + (CommandId("extra"),)), WorkflowRejection.OBSERVATION_UNAVAILABLE)
    rejected(complete(pending, done, verified=required + required[:1]), WorkflowRejection.OBSERVATION_UNAVAILABLE)
    rejected(complete(pending, dismissed(rejections(2), "r0")), WorkflowRejection.OBSERVATION_UNAVAILABLE)


def test_completion_rejects_a_fresh_ceiling_and_unapplied_projection():
    pending = accepted().snapshot
    done = dismissed(rejections(2), "r0", "r1")
    rejected(complete(pending, done + (fact("new", at=10),), ceiling=2), WorkflowRejection.OTHER_HOLD)
    assert isinstance(complete(pending, done + (fact("new", at=10),), ceiling=3), WorkflowChanged)
    rejected(complete(pending, done, labels=(TRIGGER, Label.NEEDS_HUMAN_REVIEW)), WorkflowRejection.OTHER_HOLD)
    rejected(complete(pending, done, labels=()), WorkflowRejection.OTHER_HOLD)
    rejected(complete(pending, done, labels=(), holds=("linked issue held",)), WorkflowRejection.OTHER_HOLD)
    assert isinstance(complete(pending, done, labels=(Label.NEEDS_HUMAN_REVIEW,), holds=("held",)), WorkflowChanged)


def test_ceiling_one_rearm_completes_once_nothing_is_counted():
    pending = accepted(gated(ceiling=1, count=0), reviews=()).snapshot
    assert isinstance(complete(pending, (), ceiling=1), WorkflowChanged)
    rejected(complete(pending, (fact("new"),), ceiling=1), WorkflowRejection.OTHER_HOLD)


@pytest.mark.parametrize("changes,code", [
    (dict(partial=True), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (dict(state=PullRequestState.MERGED), WorkflowRejection.INVALID_SCOPE),
    (dict(at=NOW - timedelta(seconds=301)), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (dict(key=WorkflowKey("o/x", PR)), WorkflowRejection.INVALID_SCOPE),
])
def test_completion_rejects_undecidable_observations(changes, code):
    rejected(complete(accepted().snapshot, dismissed(rejections(2), "r0", "r1"), **changes), code)


# -- validation seam -----------------------------------------------------------------

def test_every_genuine_review_transition_validates():
    base = fresh()
    gate = opened()
    validate_review_transition(base, gate, facts(observation=observe(rejections(2)), escalation=escalation(base)))
    state = gate.snapshot
    validate_review_transition(state, record_review(state, receipt(state)), facts(submission=receipt(state)))
    request = accepted(state)
    validate_review_transition(state, request, facts(observation=observe(rejections(2))))
    pending = request.snapshot
    cancel = cancel_review_rearm(pending, CancelRearm("c", pending.revision, "request:1", "why"), ACTOR)
    validate_review_transition(pending, cancel, facts())
    done = dismissed(rejections(2), "r0", "r1")
    validate_review_transition(pending, complete(pending, done), facts(observation=observe(done, labels=(TRIGGER,))))
    moved = stop(pending, verified=pending.intent.required_commands[:1])
    validate_review_transition(pending, moved, facts(observation=observe(head=OTHER_HEAD, partial=True),
                                                     accepted_preflight=request.audit.preflight))
    validate_review_transition(pending, stop(pending, reason=StopReason.TARGET_TERMINAL),
                               facts(observation=observe(state=PullRequestState.CLOSED)))
    rejected = stop(pending, reason=StopReason.REQUIRED_EFFECT_REJECTED)
    validate_review_transition(pending, rejected, facts(observation=observe(rejections(2)),
                                                        accepted_preflight=request.audit.preflight))
    # A refusal still cites a fresh complete read taken after acceptance.
    for bad in (facts(), facts(observation=observe(rejections(2), partial=True),
                               accepted_preflight=request.audit.preflight),
                facts(observation=observe(rejections(2), at=NOW - timedelta(seconds=1)),
                      accepted_preflight=request.audit.preflight),
                facts(observation=observe(rejections(2)))):
        with pytest.raises(ValueError):
            validate_review_transition(pending, rejected, bad)
    stopped = moved.snapshot
    validate_review_transition(stopped, carry_on(stopped, dismissed(rejections(2), "r0")),
                               facts(observation=observe(dismissed(rejections(2), "r0"))))


REQUEST_FACTS = dict(observation=observe(rejections(2)))


def refused(before, build, validation_facts=None):
    """A forged proposal must be unrepresentable or fail re-derivation."""
    with pytest.raises(ValueError):
        validate_review_transition(before, build(), validation_facts or facts(**REQUEST_FACTS))


def test_forged_request_commands_are_refused():
    state = gated()
    genuine = accepted(state)
    human = replace(genuine.commands[0], command_id=CommandId("forged"), payload={**genuine.commands[0].payload,
                                                                                    "review_id": "human"})
    refused(state, lambda: replace(genuine, commands=genuine.commands + (human,)))
    refused(state, lambda: replace(genuine, commands=genuine.commands[:-1]))
    widened = replace(genuine.commands[-1], payload={**genuine.commands[-1].payload, "removes": ["ai-parked"]})
    refused(state, lambda: replace(genuine, commands=genuine.commands[:-1] + (widened,)))
    other_executor = tuple(replace(c, executor_identity="someone-else") for c in genuine.commands)
    refused(state, lambda: replace(genuine, commands=other_executor))


def test_forged_extra_dismissal_is_refused():
    state = gated(count=3)
    genuine = accepted(state, reviews=rejections(3))
    narrowed = accepted(state, reviews=dismissed(rejections(3), "r2"))
    # Claims the fresh read still counted r2 so it may be dismissed again.
    intent = genuine.snapshot.intent
    refused(state, lambda: replace(narrowed, snapshot=replace(narrowed.snapshot, intent=intent), commands=genuine.commands))
    outside = CountedReview("human", HEAD)
    assert isinstance(intent, PendingReviewRearm)
    forged_intent = replace(intent, operations=(DismissCountedReview(PR, outside),) + intent.operations,
                            authorized=intent.authorized + (outside,))
    refused(state, lambda: replace(genuine, snapshot=replace(genuine.snapshot, intent=forged_intent)))


def test_request_dropping_another_open_gate_or_rewriting_guidance_is_refused():
    state = replace(gated(), gates=gated().gates + (uncertain_gate("c1"),))
    genuine = accepted(state)
    dropped = tuple(g for g in genuine.snapshot.gates if g != uncertain_gate("c1"))
    refused(state, lambda: replace(genuine, snapshot=replace(genuine.snapshot, gates=dropped)))
    rewritten = replace(genuine.snapshot.episode, guidance="smuggled")
    refused(state, lambda: replace(genuine, snapshot=replace(genuine.snapshot, episode=rewritten)))


def test_record_review_closing_a_gate_or_rewriting_guidance_is_refused():
    state = replace(gated(), gates=gated().gates + (uncertain_gate("c1"),))
    genuine = changed(record_review(state, receipt(state)))
    recorded = facts(submission=receipt(state))
    validate_review_transition(state, genuine, recorded)
    refused(state, lambda: replace(genuine, snapshot=replace(genuine.snapshot, gates=state.gates[:1])), recorded)
    refused(state, lambda: replace(genuine, snapshot=replace(genuine.snapshot, gates=())), recorded)
    rewritten = replace(genuine.snapshot.episode, guidance="smuggled")
    refused(state, lambda: replace(genuine, snapshot=replace(genuine.snapshot, episode=rewritten)), recorded)


def test_continuation_rearming_a_fabricated_decision_gate_is_refused():
    stopped = stopped_after_first_dismissal()
    genuine = changed(carry_on(stopped, dismissed(rejections(2), "r0")))
    rearming = next(g for g in genuine.snapshot.gates if isinstance(g, RearmingGate))
    fabricated = RearmingGate(replace(rearming.gate, gate_id="rearm-stopped:fabricated"), rearming.request_id)
    gates = tuple(fabricated if g == rearming else g for g in genuine.snapshot.gates)
    intent = replace(genuine.snapshot.intent, gate_id="rearm-stopped:fabricated")
    continuing = facts(observation=observe(dismissed(rejections(2), "r0")))
    validate_review_transition(stopped, genuine, continuing)
    refused(stopped, lambda: replace(genuine, snapshot=replace(genuine.snapshot, gates=gates, intent=intent)), continuing)
    continued = genuine.snapshot.intent
    assert isinstance(continued, PendingReviewRearm)
    widened = replace(continued, authorized=continued.authorized + (CountedReview("x", HEAD),))
    refused(stopped, lambda: replace(genuine, snapshot=replace(genuine.snapshot, intent=widened)), continuing)


def test_wrong_continuation_and_generation_skips_are_refused():
    state = gated()
    genuine = accepted(state)
    owed_earlier = replace(genuine.snapshot.episode, continuation=FreshReviewRequired(0))
    refused(state, lambda: replace(genuine, snapshot=replace(genuine.snapshot, episode=owed_earlier)))
    refused(state, lambda: replace(genuine, snapshot=replace(genuine.snapshot, revision=revision(2, 2))))
    recorded = changed(record_review(state, receipt(state)))
    refused(state, lambda: replace(recorded, snapshot=replace(recorded.snapshot, revision=revision(1, 2))),
            facts(submission=receipt(state)))
    gate = opened()
    kept_baseline = replace(gate.snapshot.episode, continuation=FreshReviewRequired(0), guidance="x")
    refused(fresh(), lambda: replace(gate, snapshot=replace(gate.snapshot, episode=kept_baseline)),
            facts(observation=observe(rejections(2)), escalation=escalation(fresh())))


def test_forged_prior_and_foreign_facts_are_refused():
    state = gated()
    genuine = accepted(state)
    other_prior = replace(state, episode=replace(state.episode, guidance="different"))
    with pytest.raises(ValueError):
        validate_review_transition(other_prior, genuine, facts(**REQUEST_FACTS))
    with pytest.raises(ValueError):
        validate_review_transition(state, genuine, None)
    resolve = resolve_snapshot()
    with pytest.raises(ValueError):
        validate_review_transition(resolve, genuine, facts(**REQUEST_FACTS))
    refused(state, lambda: genuine, facts(trigger_label=Label.AI_REVIEW, **REQUEST_FACTS))
    refused(state, lambda: genuine, facts())


def test_a_prior_witness_other_than_the_stored_row_is_refused_even_when_the_result_matches():
    stopped = stop(accepted().snapshot).snapshot
    stored = opened(stopped).snapshot
    assert isinstance(stored.intent, SupersededReviewRearm)
    witness = replace(stored, intent=replace(stored.intent, evidence_id="other-evidence"))
    forged = accepted(witness, command=request_for(witness, "request:2"))
    assert forged.snapshot == accepted(stored, command=request_for(stored, "request:2")).snapshot
    with pytest.raises(ValueError):
        validate_review_transition(stored, forged, facts(**REQUEST_FACTS))


def test_forged_gate_supersession_and_completion_are_refused():
    base = fresh()
    gate = opened()
    proof = escalation(base)
    refused(base, lambda: gate, facts(observation=observe(rejections(3)), escalation=proof))
    refused(base, lambda: gate, facts(observation=observe(rejections(2), head=OTHER_HEAD), escalation=proof))
    refused(base, lambda: gate, facts(escalation=proof))
    pending = accepted().snapshot
    preflight = accepted().audit.preflight
    moved = stop(pending)
    refused(pending, lambda: moved, facts(observation=observe(), accepted_preflight=preflight))
    refused(pending, lambda: moved, facts(observation=observe(head=OTHER_HEAD)))
    refused(pending, lambda: moved, facts(observation=observe(head=OTHER_HEAD, at=NOW - timedelta(seconds=301)),
                                          accepted_preflight=preflight))
    refused(pending, lambda: moved, facts(observation=PullRequestUnavailable(PR_KEY, NOW, "x"),
                                          accepted_preflight=preflight))
    terminal = stop(pending, reason=StopReason.TARGET_TERMINAL)
    refused(pending, lambda: terminal, facts(observation=observe()))
    done = dismissed(rejections(2), "r0", "r1")
    completion = complete(pending, done)
    refused(pending, lambda: completion, facts(observation=observe(done, labels=(TRIGGER, Label.NEEDS_HUMAN_REVIEW))))
    refused(pending, lambda: completion, facts(observation=observe(rejections(2), labels=(TRIGGER,))))
    refused(pending, lambda: completion, facts())


def test_unsupported_review_mutations_are_refused():
    state = gated()
    retry = AuthorizeAbsentRetry("auth:1", state.revision, CommandId("c"), "gate", "evidence")
    proposal = WorkflowChanged(state.revision, WorkflowMutation.AUTHORIZE_RETRY, replace(state, revision=revision(0, 2)),
                               AuthorizedOperatorCommand(ACTOR, retry), prior=state)
    refused(state, lambda: proposal)
    evidence = replace(state, revision=revision(0, 2), gates=state.gates + (OpenGate(
        "gate:evidence", 0, EvidenceUnavailable("reviews", NOW, 1, "x"), NoAuthorizedRemediation("wait")),))
    refused(state, lambda: WorkflowChanged(state.revision, WorkflowMutation.OPEN_GATE, evidence,
                                           ObservedTransition(("e",)), prior=state),
            facts(observation=observe(rejections(2))))


# -- ceiling reconciliation (no dead states) -----------------------------------------

def assert_released(snapshot, head=HEAD):
    assert snapshot.gates == ()
    assert isinstance(review_dispatch_decision(snapshot, observe(head=head), patch_id=None), ReviewOwed)


def assert_release(before, result, *, head=HEAD, held=False):
    """A disproven ceiling releases into a new generation that owes a review and projects labels."""
    assert result.mutation is WorkflowMutation.RELEASE_CEILING and result.audit == ObservedTransition(("reconcile:1",))
    generation = before.revision.fence.generation + 1
    after = result.snapshot
    assert after.revision == revision(generation, before.revision.version + 1)
    assert after.episode == replace(before.episode, continuation=FreshReviewRequired(generation))
    assert after.intent == before.intent
    assert not any(isinstance(g, OpenGate) and isinstance(g.cause, ReviewRejectionCeiling) for g in after.gates)
    [projection] = result.commands
    assert projection.kind is CommandKind.RELABEL and projection.workflow == after.binding
    assert projection.target == f"pr:{PR.number}" and projection.expected_revision == head
    assert projection.idempotency_key == f"workflow:release:{generation}"
    if held:
        assert projection.payload["remove"] is None and projection.payload["add"] == Label.NEEDS_HUMAN_REVIEW.value
    else:
        assert projection.payload["remove"] == Label.NEEDS_HUMAN_REVIEW.value and projection.payload["add"] == TRIGGER.value
    return after


def test_a_human_push_and_dismissal_disprove_the_cause_and_release_the_gate():
    state = gated()
    moved = observe(dismissed(rejections(2), "r0", "r1"), head=OTHER_HEAD)
    rejected(accept(state, observation=moved), WorkflowRejection.INVALID_SCOPE)
    result = changed(reconcile(state, moved))
    assert_release(state, result, head=OTHER_HEAD)
    assert_released(result.snapshot, OTHER_HEAD)
    validate_review_transition(state, result, facts(observation=moved))


def test_a_released_ceiling_is_never_covered_by_the_old_baseline():
    """keifu #173 class: reviewed at A, ceiling at A, human dismissal -> the PR must be reviewed again."""
    baseline = changed(record_review(gated(), receipt(gated()))).snapshot
    assert isinstance(review_dispatch_decision(baseline, observe(), patch_id=PATCH), ReviewHeld)
    human = observe(dismissed(rejections(2), "r0", "r1"))
    release = changed(reconcile(baseline, human))
    after = assert_release(baseline, release)
    assert review_dispatch_decision(after, observe(labels=(TRIGGER,)), patch_id=PATCH) == ReviewOwed(ReviewKind.RE_REVIEW)
    validate_review_transition(baseline, release, facts(observation=human))
    # The old generation's in-flight review can no longer repopulate the baseline.
    rejected(record_review(after, receipt(baseline, bound=submission(baseline, "submit:late"))),
             WorkflowRejection.INVALID_SCOPE)
    with pytest.raises(ValueError, match="advance generation"):
        replace(release, snapshot=replace(after, revision=replace(after.revision, fence=baseline.revision.fence),
                                          episode=replace(after.episode, continuation=FreshReviewRequired(0))))
    with pytest.raises(ValueError, match="belongs only to the current generation"):
        replace(release, snapshot=replace(after, episode=baseline.episode))
    with pytest.raises(ValueError, match="released ceiling owes a fresh review"):
        replace(release, snapshot=replace(after, episode=replace(after.episode, continuation=FreshReviewRequired(0))))
    with pytest.raises(ValueError, match="only the ceiling gate and the continuation"):
        replace(release, snapshot=replace(after, episode=replace(after.episode, guidance="forged")))
    with pytest.raises(ValueError, match="removes only the episode's rejection ceiling"):
        replace(release, snapshot=replace(after, gates=(uncertain_gate("c1"),)))
    with pytest.raises(ValueError, match="open rejection ceiling of this episode"):
        WorkflowChanged(fresh().revision, WorkflowMutation.RELEASE_CEILING,
                        replace(fresh(), revision=revision(1, 1), episode=replace(fresh().episode,
                                continuation=FreshReviewRequired(1))),
                        ObservedTransition(("reconcile:1",)), prior=fresh())


def test_a_release_is_review_only_and_observed():
    state = gated()
    release = changed(reconcile(state, observe(dismissed(rejections(2), "r0", "r1"))))
    with pytest.raises(ValueError, match="observed evidence"):
        replace(release, audit=AuthorizedOperatorCommand(
            ACTOR, CancelRearm("cancel:1", state.revision, "request:1", "not a release")))
    with pytest.raises(ValueError, match="review rejection family"):
        WorkflowChanged(resolve_snapshot().revision, WorkflowMutation.RELEASE_CEILING,
                        replace(resolve_snapshot(), revision=revision(1, 1, key=ISSUE_KEY)),
                        ObservedTransition(("reconcile:1",)))


@pytest.mark.parametrize("ceiling", [0, -1, 4])
def test_a_disabled_or_raised_ceiling_releases_the_gate(ceiling):
    state = gated()
    result = changed(reconcile(state, observe(rejections(2)), ceiling=ceiling))
    assert_release(state, result)
    assert_released(result.snapshot)
    validate_review_transition(state, result, facts(observation=observe(rejections(2)), ceiling=ceiling))
    refused(state, lambda: result, facts(observation=observe(rejections(2))))


@pytest.mark.parametrize("extra_gate,holds", [((uncertain_gate("c1"),), ()), ((), ("linked issue is held",))])
def test_a_release_keeps_the_attention_hold_while_other_holds_remain(extra_gate, holds):
    state = replace(gated(), gates=gated().gates + extra_gate)
    human = observe(dismissed(rejections(2), "r0", "r1"))
    result = changed(reconcile(state, human, holds=holds))
    after = assert_release(state, result, held=True)
    assert after.gates == extra_gate
    validate_review_transition(state, result, facts(observation=human, external_holds=holds))
    if holds:
        refused(state, lambda: result, facts(observation=human))
    projection = result.commands[0]
    refused(state, lambda: replace(result, commands=()), facts(observation=human, external_holds=holds))
    forged = replace(projection, payload={**projection.payload, "remove": Label.NEEDS_HUMAN_REVIEW.value,
                                          "add": TRIGGER.value})
    refused(state, lambda: replace(result, commands=(forged,)), facts(observation=human, external_holds=holds))


def test_a_changed_but_true_cause_is_replaced_in_the_same_generation():
    state = gated()
    moved = observe(rejections(2), head=OTHER_HEAD)
    result = changed(reconcile(state, moved))
    assert result.mutation is WorkflowMutation.OPEN_GATE and result.commands == ()
    assert result.snapshot.revision.fence == state.revision.fence and result.snapshot.episode == state.episode
    validate_review_transition(state, result, facts(observation=moved))
    advanced = replace(result.snapshot, revision=revision(1, 2),
                       episode=replace(state.episode, continuation=FreshReviewRequired(1)))
    with pytest.raises(ValueError):
        replace(result, snapshot=advanced)
    refused(state, lambda: replace(result, mutation=WorkflowMutation.RELEASE_CEILING, snapshot=advanced),
            facts(observation=moved))


# -- gate identity never rematches terminal history ------------------------------

def test_a_reopened_identical_cause_after_cancellation_gets_a_new_gate_id():
    state = gated()
    first = ceiling_gate_of(state)
    pending = accepted(state).snapshot
    cancel = CancelRearm("cancel:1", pending.revision, "request:1", "scope changed")
    cancelled = changed(cancel_review_rearm(pending, cancel, ACTOR)).snapshot
    assert cancelled.intent.gate_id == first.gate_id
    reopened = changed(reconcile(cancelled, observe(rejections(2))))
    gate = ceiling_gate_of(reopened.snapshot)
    assert gate.cause == first.cause and gate.opened_generation == 1
    assert gate.gate_id not in (first.gate_id, cancelled.intent.gate_id)
    assert gate.gate_id == review_ceiling_gate_id(PR_KEY, first.cause, reopened.snapshot.revision)


def test_a_toggled_ceiling_reopens_under_a_new_gate_id():
    state = gated()
    first = ceiling_gate_of(state)
    released = changed(reconcile(state, observe(rejections(2)), ceiling=0)).snapshot
    assert reconcile(released, observe(rejections(2))) == WorkflowUnchanged(released)
    reopened = changed(open_new(released, observe(rejections(2))))
    gate = ceiling_gate_of(reopened.snapshot)
    assert gate.cause == first.cause and gate.gate_id != first.gate_id
    validate_review_transition(released, reopened, facts(observation=observe(rejections(2)),
                                                         escalation=escalation(released)))


@pytest.mark.parametrize("opened", [0, None, (0, 1), revision(0, 1, key=WorkflowKey("o/other", PR))])
def test_a_ceiling_gate_id_requires_the_revision_it_opens_at(opened):
    with pytest.raises(ValueError):
        review_ceiling_gate_id(PR_KEY, ceiling_gate_of(gated()).cause, opened)


def test_every_opening_revision_yields_a_distinct_gate_id():
    cause = ceiling_gate_of(gated()).cause
    ids = {review_ceiling_gate_id(PR_KEY, cause, revision(generation, version))
           for generation in range(3) for version in range(3)}
    assert len(ids) == 9


def test_a_head_moving_back_within_one_generation_reopens_under_a_new_gate_id():
    at_a = gated()
    at_b = changed(reconcile(at_a, observe(rejections(2), head=OTHER_HEAD))).snapshot
    back = changed(reconcile(at_b, observe(rejections(2))))
    first, again = ceiling_gate_of(at_a), ceiling_gate_of(back.snapshot)
    assert again.cause == first.cause and again.opened_generation == first.opened_generation == 0
    assert again.gate_id not in (first.gate_id, ceiling_gate_of(at_b).gate_id)
    validate_review_transition(at_b, back, facts(observation=observe(rejections(2))))


def test_an_oversize_ceiling_closes_or_fits_once_the_operator_dismisses_reviews():
    reviews = rejections(70)
    state = gated(ceiling=64, reviews=reviews)
    rejected(accept(state, reviews), WorkflowRejection.AUTHORIZATION_BOUND_EXCEEDED)
    everything = dismissed(reviews, *(f"r{index}" for index in range(70)))
    assert_released(changed(reconcile(state, observe(everything), ceiling=64)).snapshot)
    some = dismissed(reviews, *(f"r{index}" for index in range(7)))
    refreshed = changed(reconcile(state, observe(some), ceiling=64))
    assert len(ceiling_gate_of(refreshed.snapshot).cause.counted) == MAX_COUNTED_REVIEWS
    validate_review_transition(state, refreshed, facts(observation=observe(some), ceiling=64))
    assert len(accepted(refreshed.snapshot, reviews=some).snapshot.intent.dismissals) == MAX_COUNTED_REVIEWS


def test_reconciliation_recounts_under_the_captured_rule_not_configuration():
    unmergeable = CountingRule(BOT, (OTHER_HEAD,))
    reviews = (fact("r0", at=0), fact("approve", APPROVED, commit=OTHER_HEAD, at=1), fact("r1", at=2))
    state = changed(open_new(fresh(), observe(reviews), rule=unmergeable)).snapshot
    assert ceiling_gate_of(state).cause.rule == unmergeable
    # Under the plain rule the approval would reset the count to one and close the gate.
    assert reconcile(state, observe(reviews)) == WorkflowUnchanged(state)


@pytest.mark.parametrize("observation,code", [
    (observe(rejections(0), partial=True), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (observe(rejections(0), at=NOW - timedelta(seconds=301)), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (observe(rejections(0), at=NOW + timedelta(seconds=1)), WorkflowRejection.OBSERVATION_UNAVAILABLE),
    (observe(rejections(0), state=PullRequestState.CLOSED), WorkflowRejection.INVALID_SCOPE),
    (PullRequestUnavailable(PR_KEY, NOW, "x"), WorkflowRejection.OBSERVATION_UNAVAILABLE),
])
def test_reconciliation_never_disproves_from_undecidable_reads(observation, code):
    rejected(reconcile(gated(), observation), code)


def test_reconciliation_without_a_ceiling_or_stopped_decision_changes_nothing():
    assert reconcile(fresh(), observe(rejections(5))) == WorkflowUnchanged(fresh())
    pending = accepted().snapshot
    assert reconcile(pending, observe()) == WorkflowUnchanged(pending)
    refused(fresh(), lambda: opened(), facts(observation=observe(rejections(2))))
    with pytest.raises(ValueError):
        reconcile_review_ceiling(gated(), observe(), now=NOW, ceiling=3, evidence_ids=("r",),
                                 trigger_label=Label.NEEDS_HUMAN_REVIEW, executor_identity=EXECUTOR)
    with pytest.raises(ValueError):
        reconcile(gated(), observe(), holds=["not a tuple"])


def refreshed_then_continued():
    """stop -> reconcile-add -> continue: a pending continuation beside an open ceiling it did not select."""
    pending = accepted().snapshot
    stopped = stop(pending).snapshot
    refreshed = changed(reconcile(stopped, observe(rejections(2)))).snapshot
    assert ceiling_gate_of(refreshed).opened_generation == 1
    return changed(carry_on(refreshed, rejections(2)))


def test_a_pending_rearm_owns_reconciliation_even_beside_an_unselected_ceiling():
    continued = refreshed_then_continued().snapshot
    ceiling_gate_of(continued)
    # The fresh read disproves the open ceiling, but the pending re-arm settles first.
    assert reconcile(continued, observe(dismissed(rejections(2), "r0", "r1"))) == WorkflowUnchanged(continued)
    assert reconcile(continued, observe(rejections(2), head=OTHER_HEAD)) == WorkflowUnchanged(continued)


def test_stop_refresh_continue_complete_held_then_release_projects_the_trigger():
    continued = refreshed_then_continued()
    # The unselected ceiling is a remaining hold: the projection keeps the attention label.
    assert continued.commands[-1].payload["add"] == Label.NEEDS_HUMAN_REVIEW.value
    done = dismissed(rejections(2), "r0", "r1")
    completion = changed(complete(continued.snapshot, done, labels=(Label.NEEDS_HUMAN_REVIEW,)))
    held = completion.snapshot
    assert isinstance(held.intent, CompletedReviewRearm)
    assert isinstance(review_dispatch_decision(held, observe(done), patch_id=None), ReviewHeld)
    release = changed(reconcile(held, observe(done)))
    after = assert_release(held, release)
    validate_review_transition(held, release, facts(observation=observe(done)))
    assert_released(after)


def test_stop_refresh_rearm_complete_leaves_nothing_held():
    pending = accepted().snapshot
    uncertainty = RemoteOutcomeUncertain(CommandId("unrelated"), NOW, 1, "timed out")
    stopped = replace(stop(pending).snapshot, gates=stop(pending).snapshot.gates + (
        OpenGate("gate:unrelated", 1, uncertainty, NoAuthorizedRemediation("reconcile only")),))
    refresh = changed(reconcile(stopped, observe(rejections(2))))
    validate_review_transition(stopped, refresh, facts(observation=observe(rejections(2))))
    state = refresh.snapshot
    request = accepted(state, command=request_for(state, "request:2"))
    validate_review_transition(state, request, facts(**REQUEST_FACTS))
    gates = request.snapshot.gates
    assert not any(isinstance(g, OpenGate) and isinstance(g.cause, RearmNeedsDecision) for g in gates)
    assert any(isinstance(g, OpenGate) and g.cause == uncertainty for g in gates)
    decision = next(g for g in state.gates if isinstance(g.cause, RearmNeedsDecision))
    with pytest.raises(ValueError, match="exactly for the current stopped re-arm"):
        replace(request, snapshot=replace(request.snapshot, gates=gates + (decision,)))
    unheld = tuple(g for g in gates if not (isinstance(g, OpenGate) and g.cause == uncertainty))
    with pytest.raises(ValueError, match="absorb only stopped decision gates"):
        replace(request, snapshot=replace(request.snapshot, gates=unheld))
    refused(state, lambda: replace(request, snapshot=replace(request.snapshot, gates=unheld)))
    done = dismissed(rejections(2), "r0", "r1")
    resolved = replace(request.snapshot, gates=unheld)
    assert_released(changed(complete(resolved, done)).snapshot)


def test_a_request_absorbing_a_decision_covers_its_retired_uncertainty():
    pending = accepted().snapshot
    retired = pending.intent.required_commands[0]
    uncertainty = RemoteOutcomeUncertain(retired, NOW, 4, "timed out")
    stopped = stop(pending, uncertainty=(uncertainty,)).snapshot
    state = changed(reconcile(stopped, observe(rejections(2)))).snapshot
    request = accepted(state, command=request_for(state, "request:2"))
    validate_review_transition(state, request, facts(**REQUEST_FACTS))
    intent = request.snapshot.intent
    assert isinstance(intent, PendingReviewRearm) and retired in intent.replaces
    # Covered: the projection releases the attention label, as for a continuation.
    projection = request.commands[-1]
    assert projection.payload["remove"] == Label.NEEDS_HUMAN_REVIEW.value and projection.payload["add"] == TRIGGER.value
    done = dismissed(rejections(2), "r0", "r1")
    completion = changed(complete(request.snapshot, done))
    assert any(isinstance(g, OpenGate) and g.cause == uncertainty for g in completion.snapshot.gates)
    rejected(complete(request.snapshot, done, labels=(TRIGGER, Label.NEEDS_HUMAN_REVIEW)), WorkflowRejection.OTHER_HOLD)
    # The coverage is re-derived from the absorbed decision, never claimed.
    for forged in ((), intent.replaces + (CommandId("unrelated"),)):
        with pytest.raises(ValueError, match="exactly the retired dismissals"):
            replace(request, snapshot=replace(request.snapshot, intent=replace(intent, replaces=forged)))
    # An uncertainty the absorbed decision did not retire stays an independent hold.
    unrelated = OpenGate("gate:unrelated", 1, RemoteOutcomeUncertain(CommandId("unrelated"), NOW, 1, "x"),
                         NoAuthorizedRemediation("reconcile only"))
    other = replace(state, gates=state.gates + (unrelated,))
    held = accepted(other, command=request_for(other, "request:2"))
    assert held.commands[-1].payload["add"] == Label.NEEDS_HUMAN_REVIEW.value
    rejected(complete(held.snapshot, done), WorkflowRejection.OTHER_HOLD)


# -- harness-owned facts behind recorded reviews, gates and preflights ----------------

def test_a_recorded_review_must_match_the_submission_receipt():
    state = gated()
    truth = facts(submission=receipt(state))
    genuine = changed(record_review(state, receipt(state)))
    validate_review_transition(state, genuine, truth)
    refused(state, lambda: changed(record_review(state, receipt(state, OTHER_HEAD))), truth)
    refused(state, lambda: changed(record_review(state, receipt(state, patch=OTHER_PATCH))), truth)
    refused(state, lambda: replace(genuine, audit=ObservedTransition(("submit:1", "receipt:forged"))), truth)
    refused(state, lambda: genuine, facts())


def test_a_new_gate_requires_the_harness_ceiling_escalation():
    base = fresh()
    gate = opened()
    proof = escalation(base)
    refused(base, lambda: gate, facts(observation=observe(rejections(2))))
    refused(base, lambda: replace(gate, audit=ObservedTransition(("escalate:1",))),
            facts(observation=observe(rejections(2)), escalation=proof))
    with pytest.raises(ValueError):
        validate_review_transition(base, gate, facts(observation=observe(rejections(2)), escalation=proof, rule=None))
    rejected(open_new(base, observe(rejections(2)), proof=escalation(base, cause="verdict")),
             WorkflowRejection.INVALID_SCOPE)
    rejected(open_new(base, observe(rejections(2)), proof=escalation(base, head=OTHER_HEAD)),
             WorkflowRejection.INVALID_SCOPE)
    rearmed = complete(accepted().snapshot, dismissed(rejections(2), "r0", "r1")).snapshot
    rejected(open_new(rearmed, observe(rejections(3, prefix="n")), proof=escalation(base)),
             WorkflowRejection.INVALID_SCOPE)
    with pytest.raises(ValueError):
        CeilingEscalation(submission(base), HEAD, "receipt", "bored", RULE)


def test_the_operator_preflight_is_rederived_from_the_fresh_observation():
    state = gated()
    genuine = accepted(state)
    assert genuine.audit.preflight.observed_at == NOW
    audit = genuine.audit
    assert isinstance(audit, AuthorizedOperatorCommand) and isinstance(audit.preflight, ReviewRearmPreflight)
    older = replace(audit.preflight, observed_at=NOW - timedelta(seconds=10))
    refused(state, lambda: replace(genuine, audit=replace(audit, preflight=older)))
    for stale in (NOW - timedelta(seconds=301), NOW + timedelta(seconds=1)):
        refused(state, lambda: genuine, facts(observation=observe(rejections(2), at=stale)))
    refused(state, lambda: genuine, facts(observation=observe(dismissed(rejections(2), "r0"))))
    narrowed = accepted(gated(count=3), reviews=dismissed(rejections(3), "r2"))
    refused(gated(count=3), lambda: narrowed, facts(observation=observe(rejections(3))))
    with pytest.raises(ValueError):
        replace(genuine.audit.preflight, observed_at=NOW.replace(tzinfo=None))


# -- captured counting rule -------------------------------------------------------------

def test_intents_carry_and_recount_under_the_captured_rule():
    unmergeable = CountingRule(BOT, (OTHER_HEAD,))
    reviews = (fact("r0", at=0), fact("approve", APPROVED, commit=OTHER_HEAD, at=1), fact("r1", at=2))
    state = changed(open_new(fresh(), observe(reviews), rule=unmergeable)).snapshot
    request = changed(accept(state, reviews))
    assert request.snapshot.intent.rule == unmergeable
    assert [r.review_id for r in request.snapshot.intent.dismissals] == ["r0", "r1"]
    only_r1 = tuple(replace(r, state=DISMISSED) if r.review_id == "r1" else r for r in reviews)
    rejected(complete(request.snapshot, only_r1), WorkflowRejection.OBSERVATION_UNAVAILABLE)
    stopped = stop(request.snapshot).snapshot
    assert stopped.intent.rule == unmergeable
    continued = changed(carry_on(stopped, reviews))
    assert [r.review_id for r in continued.snapshot.intent.dismissals] == ["r0", "r1"]
    continued_intent = continued.snapshot.intent
    assert isinstance(continued_intent, PendingReviewRearm)
    other_rule = replace(continued_intent, rule=RULE)
    with pytest.raises(ValueError, match="stopped intent's rule"):
        replace(continued, snapshot=replace(continued.snapshot, intent=other_rule))
    refused(stopped, lambda: replace(continued, snapshot=replace(continued.snapshot, intent=other_rule)),
            facts(observation=observe(reviews)))


def test_continued_dismissals_are_fenced_by_the_fresh_head():
    continued = changed(carry_on(stopped_after_first_dismissal(), dismissed(rejections(2), "r0"), head=OTHER_HEAD))
    dismissal = continued.commands[0]
    assert dismissal.expected_revision == OTHER_HEAD and dismissal.payload["guard"]["head_sha"] == OTHER_HEAD


def test_the_projection_releases_once_a_retired_uncertainty_resolves():
    pending = accepted().snapshot
    uncertainty = RemoteOutcomeUncertain(pending.intent.required_commands[0], NOW, 4, "timed out")
    stopped = stop(pending, uncertainty=(uncertainty,)).snapshot
    continued = changed(carry_on(stopped, rejections(2)))
    projection = continued.commands[-1]
    # The retired obligation's uncertainty is not an independent hold, so the
    # replacement projects the release and completion expects that release.
    assert projection.payload["remove"] == Label.NEEDS_HUMAN_REVIEW.value
    assert projection.payload["add"] == TRIGGER.value
    done = dismissed(rejections(2), "r0", "r1")
    rejected(complete(continued.snapshot, done, labels=(TRIGGER, Label.NEEDS_HUMAN_REVIEW)), WorkflowRejection.OTHER_HOLD)
    changed(complete(continued.snapshot, done))
    resolved = replace(continued.snapshot, gates=tuple(
        g for g in continued.snapshot.gates if not (isinstance(g, OpenGate) and g.cause == uncertainty)))
    assert_released(changed(complete(resolved, done)).snapshot)


# -- a stopped relabel is covered only in its own direction ----------------------------

HOLD = ("linked issue is held",)


def relabel_uncertain(pending):
    """Stop `pending` with its own relabel's outcome unknown."""
    relabel = pending.intent.projection.command_id
    return stop(pending, uncertainty=(RemoteOutcomeUncertain(relabel, NOW, 4, "timed out"),)).snapshot, relabel


def pending_of(snapshot) -> PendingReviewRearm:
    assert isinstance(snapshot.intent, PendingReviewRearm)
    return snapshot.intent


def retired_of(snapshot) -> tuple[CommandId, ...]:
    assert isinstance(snapshot.intent, (SupersededReviewRearm, CancelledReviewRearm))
    return snapshot.intent.retired_obligations


def assert_completion_held(snapshot, relabel, holds=()):
    done = dismissed(rejections(2), "r0", "r1")
    rejected(complete(snapshot, done, holds=holds), WorkflowRejection.OTHER_HOLD)
    completed = changed(complete(snapshot, done, labels=(Label.NEEDS_HUMAN_REVIEW,), holds=holds)).snapshot
    assert any(isinstance(g, OpenGate) and getattr(g.cause, "command_id", None) == relabel for g in completed.gates)
    assert isinstance(review_dispatch_decision(completed, observe(done), patch_id=None), ReviewHeld)


def test_a_same_direction_relabel_is_covered_by_the_replacement():
    stopped, relabel = relabel_uncertain(accepted().snapshot)
    continued = changed(carry_on(stopped, rejections(2)))
    validate_review_transition(stopped, continued, facts(observation=observe(rejections(2))))
    intent = pending_of(continued.snapshot)
    assert relabel in intent.replaces and intent.projection.labels == stopped.intent.projection.labels
    assert continued.commands[-1].payload["add"] == TRIGGER.value
    changed(complete(continued.snapshot, dismissed(rejections(2), "r0", "r1")))


def test_an_old_release_is_not_covered_by_a_held_replacement():
    stopped, relabel = relabel_uncertain(accepted().snapshot)
    continued = changed(carry_on(stopped, rejections(2), holds=HOLD))
    validate_review_transition(stopped, continued, facts(observation=observe(rejections(2)), external_holds=HOLD))
    intent = pending_of(continued.snapshot)
    assert relabel not in intent.replaces and intent.projection.remove is None
    assert set(intent.replaces) == set(retired_of(stopped)) - {relabel}
    assert_completion_held(continued.snapshot, relabel, HOLD)
    with pytest.raises(ValueError, match="only in the same direction"):
        replace(continued, snapshot=replace(continued.snapshot, intent=replace(
            intent, replaces=retired_of(stopped))))


def test_an_old_hold_is_not_covered_by_a_releasing_replacement():
    stopped, relabel = relabel_uncertain(accepted(holds=HOLD).snapshot)
    assert stopped.intent.projection.remove is None
    continued = changed(carry_on(stopped, rejections(2)))
    validate_review_transition(stopped, continued, facts(observation=observe(rejections(2))))
    intent = pending_of(continued.snapshot)
    # The conflicting relabel stays an independent hold, so the replacement is held too.
    assert relabel not in intent.replaces and intent.projection.remove is None
    assert continued.commands[-1].payload["add"] == Label.NEEDS_HUMAN_REVIEW.value
    assert_completion_held(continued.snapshot, relabel)
    forged = replace(intent, replaces=retired_of(stopped),
                     projection=replace(intent.projection, remove=Label.NEEDS_HUMAN_REVIEW, add=TRIGGER))
    refused(stopped, lambda: replace(continued, snapshot=replace(continued.snapshot, intent=forged)),
            facts(observation=observe(rejections(2))))


def test_a_request_absorbing_a_conflicting_relabel_is_held():
    stopped, relabel = relabel_uncertain(accepted().snapshot)
    state = changed(reconcile(stopped, observe(rejections(2)))).snapshot
    request = accepted(state, command=request_for(state, "request:2"), holds=HOLD)
    validate_review_transition(state, request, facts(observation=observe(rejections(2)), external_holds=HOLD))
    intent = pending_of(request.snapshot)
    assert relabel not in intent.replaces and intent.projection.remove is None
    assert_completion_held(request.snapshot, relabel, HOLD)


# -- re-rejection to the ceiling during a pending re-arm (increment 6) ----------------------------

def test_a_pending_rearm_is_reached_again_only_by_fresh_complete_counted_rejections():
    from issue_agent.domain.review_workflow_policy import ceiling_reached_again

    state = accepted().snapshot
    pending = state.intent
    authorized = rejections(2)
    # The authorized set itself never re-reaches the ceiling, dismissed or not.
    assert not ceiling_reached_again(pending, observe(authorized), 3)
    assert not ceiling_reached_again(pending, observe(dismissed(authorized, "r0", "r1")), 3)
    fresh_two = authorized + rejections(2, prefix="n", start=10)
    assert ceiling_reached_again(pending, observe(fresh_two), 3)
    # Another author's rejection is not the bot's; a partial read decides nothing.
    other = authorized + rejections(2, prefix="h", start=10, author="octocat")
    assert not ceiling_reached_again(pending, observe(other), 3)
    assert not ceiling_reached_again(pending, observe(partial=True), 3)
    # A disabled ceiling never stops a re-arm.
    assert not ceiling_reached_again(pending, observe(fresh_two), 0)


# -- increment 6 review: the hold follows the settled escalation (F1-F3) ------------------------

def escalated_receipt(state, head=HEAD, patch=PATCH):
    return ReviewSubmissionReceipt(submission(state, "escalate:verdict"), head, patch, "receipt:verdict", escalated=True)


def record_escalated(state, observation, receipt_value=None):
    from issue_agent.domain.review_workflow_policy import record_escalated_review
    return record_escalated_review(state, receipt_value or escalated_receipt(state), observation, now=NOW,
                                   trigger_label=TRIGGER, executor_identity=EXECUTOR, evidence_ids=("obs:1",))


def test_an_escalated_verdict_records_its_head_and_its_hold_in_one_transition():
    state = fresh()
    result = changed(record_escalated(state, observe(labels=(TRIGGER,))))
    assert result.mutation is WorkflowMutation.RECORD_REVIEW
    baseline = result.snapshot.episode.continuation
    assert isinstance(baseline, ReviewedBaseline) and (baseline.head_sha, baseline.patch_id) == (HEAD, PATCH)
    (projection,) = result.commands
    assert (projection.payload["remove"], projection.payload["add"]) == (TRIGGER.value, Label.NEEDS_HUMAN_REVIEW.value)
    assert projection.payload["pull_request_hold_guard"]["labels"] == [TRIGGER.value]
    # The recorded head is never owed again; a new head is.
    assert not review_owed(result.snapshot, HEAD, None) and review_owed(result.snapshot, OTHER_HEAD, None)
    validate_review_transition(state, result, facts(observation=observe(labels=(TRIGGER,)),
                                                    submission=escalated_receipt(state)))


def test_an_escalated_verdict_on_a_superseded_head_holds_nothing_and_owes_the_new_head():
    state = fresh()
    rejected(record_escalated(state, observe(head=OTHER_HEAD, labels=(TRIGGER,))), WorkflowRejection.INVALID_SCOPE)
    rejected(record_escalated(state, observe(labels=(TRIGGER,), state=PullRequestState.CLOSED)),
             WorkflowRejection.INVALID_SCOPE)
    rejected(record_escalated(state, PullRequestUnavailable(PR_KEY, NOW, "down")),
             WorkflowRejection.OBSERVATION_UNAVAILABLE)
    # Without its hold an escalated verdict is never a plain baseline.
    rejected(record_review(state, escalated_receipt(state)), WorkflowRejection.INVALID_SCOPE)


def test_a_forged_escalated_record_is_refused_by_rederivation():
    state = fresh()
    genuine = changed(record_escalated(state, observe(labels=(TRIGGER,))))
    evidence = facts(observation=observe(labels=(TRIGGER,)), submission=escalated_receipt(state))
    refused(state, lambda: replace(genuine, commands=()), evidence)
    other = replace(genuine.commands[0], payload={**genuine.commands[0].payload, "remove": None})
    refused(state, lambda: replace(genuine, commands=(other,)), evidence)
    with pytest.raises(ValueError):
        validate_review_transition(state, genuine, facts(submission=escalated_receipt(state)))


def test_the_ceiling_is_judged_by_the_rule_the_escalating_verdict_counted_under():
    unmergeable = CountingRule(BOT, (OTHER_HEAD,))
    reviews = (fact("r0", at=0), fact("approve", APPROVED, commit=OTHER_HEAD, at=1), fact("r1", at=2))
    configured = RULE  # no non-resetting commits: the approval would reset the count
    counted = changed(open_new(fresh(), observe(reviews), rule=configured,
                               proof=escalation(fresh(), counted=unmergeable)))
    assert ceiling_gate_of(counted.snapshot).cause.non_resetting_commits == (OTHER_HEAD,)
    # The same read under a rule that resets on the approval disproves it (no re-buy loop:
    # the settlement and the verdict can no longer disagree about the count).
    rejected(open_new(fresh(), observe(reviews), rule=configured, proof=escalation(fresh(), counted=configured)),
             WorkflowRejection.NOT_EXHAUSTED)
    rejected(open_new(fresh(), observe(reviews), rule=configured,
                      proof=escalation(fresh(), counted=CountingRule("someone-else[bot]", (OTHER_HEAD,)))),
             WorkflowRejection.INVALID_SCOPE)


def test_a_ceiling_disproven_before_settlement_holds_nothing():
    # A human dismissed a counted review between the verdict and its settlement.
    rejected(open_new(fresh(), observe(dismissed(rejections(2), "r0"))), WorkflowRejection.NOT_EXHAUSTED)
    rejected(open_new(fresh(), observe(rejections(2), head=OTHER_HEAD), proof=escalation(fresh())),
             WorkflowRejection.INVALID_SCOPE)


def restoration(snapshot, *, restores=None, evidence="obs:1", **kwargs):
    from issue_agent.domain.review_workflow_policy import ceiling_hold_restoration
    return ceiling_hold_restoration(snapshot, observe(**kwargs), evidence, restores=restores or {},
                                    trigger_label=TRIGGER, executor_identity=EXECUTOR)


@pytest.mark.parametrize("labels, restores", [
    ((Label.NEEDS_HUMAN_REVIEW,), None),
    ((), (None, Label.NEEDS_HUMAN_REVIEW.value)),
    ((TRIGGER,), (TRIGGER.value, Label.NEEDS_HUMAN_REVIEW.value)),
    ((TRIGGER, Label.NEEDS_HUMAN_REVIEW), (TRIGGER.value, Label.NEEDS_HUMAN_REVIEW.value)),
])
def test_an_open_ceiling_restores_its_missing_hold(labels, restores):
    from issue_agent.domain.review_workflow_policy import RestoreHold, escalation_signal_identity
    state = gated()
    decision = restoration(state, labels=labels)
    if restores is None:
        assert decision is None
        return
    assert isinstance(decision, RestoreHold) and decision.gate_id == ceiling_gate_of(state).gate_id
    command = decision.command
    assert (command.payload["remove"], command.payload["add"]) == restores
    assert command.workflow == state.binding and command.executor_identity == EXECUTOR
    # A restored hold makes the same escalation visible as the gate's own projection would.
    assert escalation_signal_identity(command) == decision.gate_id
    again = restoration(state, labels=labels, evidence="obs:2")
    assert again.command.command_id != command.command_id  # one per read that found it missing


def test_hold_restoration_is_bounded_per_gate_then_contested():
    from issue_agent.domain.review_workflow_policy import MAX_HOLD_RESTORES, HoldContested, RestoreHold
    state = gated()
    gate_id = ceiling_gate_of(state).gate_id
    assert isinstance(restoration(state, labels=(TRIGGER,), restores={gate_id: MAX_HOLD_RESTORES - 1}), RestoreHold)
    assert restoration(state, labels=(TRIGGER,), restores={gate_id: MAX_HOLD_RESTORES}) == HoldContested(
        gate_id, MAX_HOLD_RESTORES)
    assert restoration(state, labels=(TRIGGER,), restores={gate_id: 10**9}) == HoldContested(gate_id, 10**9)
    # A count kept for another (changed or released) gate never spends this one's bound.
    assert isinstance(restoration(state, labels=(TRIGGER,), restores={"review-ceiling:old": 99}), RestoreHold)
    with pytest.raises(ValueError):
        restoration(state, labels=(TRIGGER,), restores={gate_id: -1})


def test_hold_restoration_never_acts_without_an_open_ceiling_or_during_a_rearm():
    assert restoration(fresh(), labels=()) is None
    assert restoration(accepted().snapshot, labels=(TRIGGER,)) is None
    assert restoration(gated(), labels=(), state=PullRequestState.CLOSED) is None
    with pytest.raises(ValueError):
        restoration(gated(), labels=(), key=ISSUE_KEY)


@pytest.mark.parametrize("labels, verified", [
    ((Label.NEEDS_HUMAN_REVIEW,), True),
    ((Label.NEEDS_HUMAN_REVIEW, Label.NO_AI_REVIEW), True),  # whoever applied it, beside a human's opt-out
    ((Label.NEEDS_HUMAN_REVIEW, TRIGGER), False),  # the trigger still asks for a review: not a hold
    ((TRIGGER,), False),
    ((), False),
])
def test_a_ceiling_hold_is_verified_by_the_visible_labels_whoever_applied_them(labels, verified):
    from issue_agent.domain.review_workflow_policy import verified_ceiling_holds
    state = gated()
    held = verified_ceiling_holds(state, observe(labels=labels), trigger_label=TRIGGER)
    assert held == (frozenset({ceiling_gate_of(state).gate_id}) if verified else frozenset())


def test_a_ceiling_hold_is_never_verified_without_an_open_ceiling_an_open_pull_request_or_during_a_rearm():
    from issue_agent.domain.review_workflow_policy import verified_ceiling_holds
    held = (Label.NEEDS_HUMAN_REVIEW,)
    assert verified_ceiling_holds(fresh(), observe(labels=held), trigger_label=TRIGGER) == frozenset()
    assert verified_ceiling_holds(accepted().snapshot, observe(labels=held), trigger_label=TRIGGER) == frozenset()
    closed = observe(labels=held, state=PullRequestState.CLOSED)
    assert verified_ceiling_holds(gated(), closed, trigger_label=TRIGGER) == frozenset()
    with pytest.raises(ValueError):
        verified_ceiling_holds(gated(), observe(labels=held, key=ISSUE_KEY), trigger_label=TRIGGER)
    with pytest.raises(ValueError):
        verified_ceiling_holds(gated(), None, trigger_label=TRIGGER)  # type: ignore[arg-type]


# -- increment 6 re-review: a refused verdict-escalation projection (N1) ------------------------

def escalated_state():
    state = fresh()
    return changed(record_escalated(state, observe(labels=(TRIGGER,)))).snapshot, escalated_receipt(state)


def replan(snapshot, receipt, *, replans=0, **kwargs):
    from issue_agent.domain.review_workflow_policy import replan_escalation_hold
    return replan_escalation_hold(snapshot, observe(**kwargs), receipt, replans=replans, trigger_label=TRIGGER,
                                  executor_identity=EXECUTOR)


@pytest.mark.parametrize("labels", [(TRIGGER, Label.NO_AI_REVIEW), (TRIGGER, Label.AI_PARKED), ()])
def test_a_refused_verdict_escalation_is_re_planned_from_the_fresh_hold_facts(labels):
    from issue_agent.domain.review_workflow_policy import (
        EscalationReprojected, escalation_projection_identity, pull_request_hold_guard_applies,
    )
    snapshot, receipt = escalated_state()
    decision = replan(snapshot, receipt, labels=labels)
    assert isinstance(decision, EscalationReprojected)
    command = decision.command
    # A human's hold label is kept: the projection only adds the hold and removes the trigger.
    assert command.payload["add"] == Label.NEEDS_HUMAN_REVIEW.value
    assert command.payload["remove"] == TRIGGER.value
    assert set(command.payload["pull_request_hold_guard"]["labels"]) == {label.value for label in labels}
    assert escalation_projection_identity(command) == str(receipt.submission.command_id)
    original = changed(record_escalated(fresh(), observe(labels=(TRIGGER,)))).commands[0]
    assert command.command_id != original.command_id
    assert escalation_projection_identity(original) == escalation_projection_identity(command)
    guard = PullRequestHoldGuard(HEAD, frozenset(labels))
    assert pull_request_hold_guard_applies(guard, observe(labels=labels))


def test_a_refused_verdict_escalation_is_moot_once_it_no_longer_applies():
    from issue_agent.domain.review_workflow_policy import EscalationHoldPresent, EscalationMoot
    snapshot, receipt = escalated_state()
    assert isinstance(replan(snapshot, receipt, labels=(TRIGGER,), state=PullRequestState.CLOSED), EscalationMoot)
    assert "new head" in replan(snapshot, receipt, labels=(TRIGGER,), head=OTHER_HEAD).reason
    rebaselined = replace(snapshot, episode=replace(snapshot.episode, continuation=FreshReviewRequired(0)))
    assert isinstance(replan(rebaselined, receipt, labels=(TRIGGER,)), EscalationMoot)
    assert replan(snapshot, receipt, labels=(Label.NEEDS_HUMAN_REVIEW,)) == EscalationHoldPresent()
    assert replan(snapshot, receipt, labels=(Label.NEEDS_HUMAN_REVIEW, Label.NO_AI_REVIEW)) == EscalationHoldPresent()


def test_verdict_escalation_re_plans_are_bounded_then_surfaced():
    from issue_agent.domain.review_workflow_policy import (
        MAX_ESCALATION_REPLANS, EscalationProjectionExhausted, EscalationReprojected,
    )
    snapshot, receipt = escalated_state()
    attempts = [replan(snapshot, receipt, replans=n, labels=(TRIGGER,)) for n in range(MAX_ESCALATION_REPLANS)]
    assert all(isinstance(a, EscalationReprojected) for a in attempts)
    assert len({a.command.command_id for a in attempts}) == MAX_ESCALATION_REPLANS
    assert replan(snapshot, receipt, replans=MAX_ESCALATION_REPLANS, labels=(TRIGGER,)) == (
        EscalationProjectionExhausted(MAX_ESCALATION_REPLANS))
    assert isinstance(replan(snapshot, receipt, replans=10**9, labels=(TRIGGER,)), EscalationProjectionExhausted)
    with pytest.raises(ValueError):
        replan(snapshot, receipt, replans=-1, labels=(TRIGGER,))
    with pytest.raises(ValueError):
        replan(snapshot, replace(receipt, escalated=False), labels=(TRIGGER,))


def test_escalation_signal_identity_names_the_escalation_never_the_attempt():
    from issue_agent.domain.review_workflow_policy import (
        escalation_projection_key, escalation_signal_identity, review_escalation_command_id,
    )
    assert escalation_projection_key("abc") == "workflow:escalation:abc"
    assert escalation_projection_key("abc", 2) == "workflow:escalation:abc#2"
    for bad in ("", "a#b", "x" * 600):
        with pytest.raises(ValueError):
            escalation_projection_key(bad)
    with pytest.raises(ValueError):
        escalation_projection_key("abc", 99)
    first = review_escalation_command_id(PR_KEY, CommandId("s:1"))
    assert first == review_escalation_command_id(PR_KEY, CommandId("s:1"), 0)
    assert first != review_escalation_command_id(PR_KEY, CommandId("s:1"), 1)
    ceiling = changed(open_new(fresh(), observe(rejections(2), labels=(TRIGGER,)))).commands[0]
    assert escalation_signal_identity(ceiling) == ceiling_gate_of(changed(
        open_new(fresh(), observe(rejections(2), labels=(TRIGGER,)))).snapshot).gate_id
    assert escalation_signal_identity(accepted().commands[0]) is None
