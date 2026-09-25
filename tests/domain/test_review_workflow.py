"""Review-rejection workflow values: every validator branch and cross-family confusion."""

from dataclasses import replace
from datetime import datetime

import pytest

from issue_agent.domain.contracts import CommandId, RunId, WorkflowAttemptIdentity
from issue_agent.domain.labels import Label
from issue_agent.domain.review_workflow import (
    MAX_COUNTED_REVIEWS, MAX_OBSERVED_REVIEWS, BoundReviewSubmission, CancelledReviewRearm, CompleteReviews,
    CompletedReviewRearm, CountedReview, DismissCountedReview, EnsureReviewProjection, FreshReviewRequired,
    ImportedBaseline, ObservedPullRequest, PartialReviews, PendingReviewRearm, PullRequestAbsent, PullRequestState,
    PullRequestUnavailable, RequireFreshReview, ReviewEpisode, ReviewProjectionPlan, ReviewRearmPreflight,
    ReviewRejectionCeiling,
    ReviewWorkflowSnapshot, ReviewedBaseline, SubmittedReviewFact, SubmittedReviewState, SupersededReviewRearm,
    decisive_reviews,
)
from issue_agent.domain.workflow import (
    GUIDANCE_MAX_CHARS, MAX_DETAIL_CHARS, MAX_REARM_COMMANDS, AuthorizeAbsentRetry, AuthorizedOperatorCommand, CancelRearm,
    ContinueGrantedRearm, EvidenceUnavailable, FreshResolve, HeldProjection, NoAuthorizedRemediation,
    ObservedTransition, OpenGate, RearmNeedsDecision, RearmingGate, RemoteOutcomeUncertain, RenewResolveAllowance,
    ResolveAttemptResult, ResolveCeiling, ResolveEpisode, ResolveResultKind, RetryAuthorizationRequired, StopReason,
    WorkflowChanged, WorkflowCommitted, WorkflowMigrationRequired, WorkflowMutation, WorkflowSnapshot,
    WorkflowUnchanged,
)
from issue_agent.domain.workflow_identity import (
    FamilyAuthority, IssueRef, PullRequestRef, WorkflowBinding, WorkflowFamily, WorkflowFence, WorkflowKey,
    WorkflowRevision, target_family,
)
from tests.fakes.review_workflow import (
    ACTOR, EPISODE, GATE, HEAD, ISSUE_KEY, NOW, OTHER_HEAD, PATCH, PR, PR_KEY, RESOLVE, REVIEW, RULE,
    cancelled, ceiling_cause, ceiling_gate, completed, counted, held, operations, pending, pending_intent,
    preflight, projection, request, revision, superseded,
)

PROJ = projection()

BAD_IDENTIFIERS = [None, 1, "", " ", "a" * 513, "line\nbreak", "nul\x00"]
BAD_SHAS = ["", "a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40, "g" * 40, " " + "a" * 39, 40]
SHARED_CAUSES = [
    RemoteOutcomeUncertain(CommandId("command"), NOW, 2, "offline"),
    RetryAuthorizationRequired(CommandId("command"), "absence:1"),
    EvidenceUnavailable("pull request reviews", NOW, 3, "pagination failed"),
    RearmNeedsDecision("request:0", (CommandId("command"),), StopReason.DECISION_CHANGED),
]


def resolve_snapshot():
    return WorkflowSnapshot(revision(key=ISSUE_KEY), RESOLVE, ResolveEpisode("episode"))


def review_binding(generation=1, key=PR_KEY, episode=EPISODE):
    return WorkflowBinding(WorkflowFence(key, generation), REVIEW, episode)


def baseline(generation=1, *, command="submit:1", binding=None):
    return ReviewedBaseline(HEAD, PATCH, generation, BoundReviewSubmission(
        CommandId(command), binding or review_binding(generation)))


# -- identities ---------------------------------------------------------------

def test_family_is_owned_by_its_target_kind():
    assert WorkflowFamily.REVIEW_REJECTIONS.value == "review_rejections"
    assert target_family(IssueRef(1)) is WorkflowFamily.RESOLVE
    assert target_family(PullRequestRef(1)) is WorkflowFamily.REVIEW_REJECTIONS
    with pytest.raises(ValueError):
        target_family(1)  # type: ignore[arg-type]


def test_issue_target_review_binding_and_pr_target_resolve_binding_are_unconstructible():
    with pytest.raises(ValueError, match="pull requests, not issues"):
        WorkflowBinding(WorkflowFence(ISSUE_KEY, 0), REVIEW, EPISODE)
    with pytest.raises(ValueError, match="issues, not pull requests"):
        WorkflowBinding(WorkflowFence(PR_KEY, 0), RESOLVE, EPISODE)
    assert WorkflowBinding(WorkflowFence(PR_KEY, 0), REVIEW, EPISODE).authority is REVIEW


# -- counted reviews and the ceiling cause ------------------------------------

@pytest.mark.parametrize("review_id", BAD_IDENTIFIERS)
def test_counted_review_requires_a_bounded_identity(review_id):
    with pytest.raises(ValueError):
        CountedReview(review_id, HEAD)


@pytest.mark.parametrize("commit", BAD_SHAS)
def test_counted_review_must_be_placeable_on_a_full_commit(commit):
    with pytest.raises(ValueError):
        CountedReview("review-1", commit)


@pytest.mark.parametrize("commit", ["0" * 40, "f" * 64, None])
def test_counted_review_accepts_full_object_names_or_an_unresolved_commit(commit):
    assert CountedReview("review-1", commit).commit_id == commit


def test_github_empty_commit_maps_to_unresolved_at_the_boundary():
    assert CountedReview.from_github("review-1", "").commit_id is None
    assert CountedReview.from_github("review-1", HEAD).commit_id == HEAD
    with pytest.raises(ValueError):
        CountedReview.from_github("review-1", "abc")


@pytest.mark.parametrize("ceiling", [0, -1, True, "3", 1.0, 2**63])
def test_ceiling_must_be_a_positive_bounded_integer(ceiling):
    with pytest.raises(ValueError):
        ReviewRejectionCeiling(EPISODE, HEAD, "bot", counted(2), ceiling, ())


@pytest.mark.parametrize("ceiling, count, valid", [
    (1, 0, True), (3, 2, True), (3, 3, True), (3, 1, False), (2, 0, False),
    (MAX_COUNTED_REVIEWS + 1, MAX_COUNTED_REVIEWS, True), (MAX_COUNTED_REVIEWS + 2, MAX_COUNTED_REVIEWS, False),
])
def test_ceiling_requires_exhaustion_at_ceiling_minus_one_counted_rejections(ceiling, count, valid):
    build = lambda: ReviewRejectionCeiling(EPISODE, HEAD, "bot", counted(count), ceiling, ())
    if valid:
        assert len(build().counted) == count
    else:
        with pytest.raises(ValueError):
            build()


def test_counted_set_is_bounded_unique_and_typed():
    with pytest.raises(ValueError, match="bounded"):
        ReviewRejectionCeiling(EPISODE, HEAD, "bot", counted(MAX_OBSERVED_REVIEWS + 1), 3, ())
    with pytest.raises(ValueError, match="duplicate"):
        ReviewRejectionCeiling(EPISODE, HEAD, "bot", counted(2) + counted(1), 3, ())
    with pytest.raises(ValueError):
        ReviewRejectionCeiling(EPISODE, HEAD, "bot", list(counted(2)), 3, ())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ReviewRejectionCeiling(EPISODE, HEAD, "bot", ("review-1", "review-2"), 3, ())  # type: ignore[arg-type]


@pytest.mark.parametrize("field, value", [
    ("episode_id", ""), ("episode_id", "x" * 513), ("head_sha", ""), ("head_sha", "a" * 7),
    ("review_bot", ""), ("review_bot", "bot\n"),
    ("non_resetting_commits", ("a" * 40, "a" * 40)), ("non_resetting_commits", ("abc",)),
    ("non_resetting_commits", ["a" * 40]), ("non_resetting_commits", tuple(f"{i:040x}" for i in range(65))),
])
def test_ceiling_fields_are_validated(field, value):
    with pytest.raises(ValueError):
        replace(ceiling_cause(), **{field: value})


def test_ceiling_keeps_its_bounded_non_resetting_commits():
    commits = tuple(f"{i:040x}" for i in range(64))
    assert replace(ceiling_cause(), non_resetting_commits=commits).non_resetting_commits == commits


# -- gate cause family scoping ------------------------------------------------

def test_open_gate_accepts_the_review_ceiling_cause():
    assert isinstance(ceiling_gate().cause, ReviewRejectionCeiling)


def test_resolve_snapshot_rejects_a_review_rejection_cause():
    with pytest.raises(ValueError, match="review rejection cause"):
        replace(resolve_snapshot(), gates=(ceiling_gate(),))


def test_review_snapshot_rejects_a_resolve_ceiling_cause():
    gate = OpenGate("gate:resolve", 0, ResolveCeiling("episode", 3, 3), NoAuthorizedRemediation("none"))
    with pytest.raises(ValueError, match="resolve ceiling cause"):
        replace(held(), gates=(gate,))


@pytest.mark.parametrize("cause", SHARED_CAUSES, ids=lambda cause: type(cause).__name__)
def test_both_families_accept_shared_causes(cause):
    gate = OpenGate("gate:shared", 0, cause, NoAuthorizedRemediation("reconcile only"))
    assert replace(resolve_snapshot(), gates=(gate,)).gates == (gate,)
    # A review decision gate exists only beside its stopped re-arm.
    review = cancelled("request:0") if isinstance(cause, RearmNeedsDecision) else held()
    assert replace(review, gates=(gate,)).gates == (gate,)


# -- continuation -------------------------------------------------------------

@pytest.mark.parametrize("generation", [-1, True, "1", 2**63])
def test_fresh_review_generation_is_validated(generation):
    with pytest.raises(ValueError):
        FreshReviewRequired(generation)


def test_bound_submission_requires_a_review_family_binding():
    resolve_binding = WorkflowBinding(WorkflowFence(ISSUE_KEY, 1), RESOLVE, EPISODE)
    with pytest.raises(ValueError, match="review-family binding"):
        BoundReviewSubmission(CommandId("submit:1"), resolve_binding)
    with pytest.raises(ValueError):
        BoundReviewSubmission(CommandId(""), review_binding())
    with pytest.raises(ValueError):
        ImportedBaseline("")


@pytest.mark.parametrize("head, patch", [("", PATCH), ("A" * 40, PATCH), (HEAD, "a" * 40), (HEAD, "C" * 64), (HEAD, "")])
def test_reviewed_baseline_requires_full_head_and_patch_digests(head, patch):
    with pytest.raises(ValueError):
        ReviewedBaseline(head, patch, 1, ImportedBaseline("import:1"))


def test_bound_review_cannot_establish_another_generations_baseline():
    with pytest.raises(ValueError, match="another generation"):
        ReviewedBaseline(HEAD, PATCH, 2, BoundReviewSubmission(CommandId("submit:1"), review_binding(1)))
    with pytest.raises(ValueError, match="typed provenance"):
        ReviewedBaseline(HEAD, PATCH, 1, "import:1")  # type: ignore[arg-type]
    assert ReviewedBaseline(HEAD, PATCH, 5, ImportedBaseline("import:1")).reviewed_generation == 5


def test_review_episode_bounds_guidance_and_owns_its_baseline():
    assert ReviewEpisode(EPISODE, "one\ntwo").guidance == "one\ntwo"
    assert ReviewEpisode(EPISODE, "g" * GUIDANCE_MAX_CHARS).continuation == FreshReviewRequired(0)
    with pytest.raises(ValueError):
        ReviewEpisode(EPISODE, "g" * (GUIDANCE_MAX_CHARS + 1))
    with pytest.raises(ValueError):
        ReviewEpisode("")
    with pytest.raises(ValueError, match="another episode"):
        ReviewEpisode(EPISODE, "", baseline(binding=review_binding(episode="other")))
    with pytest.raises(ValueError, match="validated continuation"):
        ReviewEpisode(EPISODE, "", FreshResolve())  # type: ignore[arg-type]


# -- operations and intents ---------------------------------------------------

def test_review_operations_require_pull_request_targets():
    for build in (lambda: DismissCountedReview(IssueRef(9), counted(1)[0]),  # type: ignore[arg-type]
                  lambda: RequireFreshReview(IssueRef(9)),  # type: ignore[arg-type]
                  lambda: EnsureReviewProjection(IssueRef(9))):  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="pull request"):
            build()
    with pytest.raises(ValueError, match="counted review"):
        DismissCountedReview(PR, "review-0")  # type: ignore[arg-type]


def test_pending_review_rearm_is_well_formed():
    intent = pending_intent()
    assert intent.pr == PR and intent.dismissals == counted()
    assert pending_intent(ops=operations(dismissals=())).dismissals == ()


@pytest.mark.parametrize("ops, match", [
    (operations()[:-2] + (EnsureReviewProjection(PR),), "fresh review"),
    (operations() + (RequireFreshReview(PR),), "fresh review"),
    (operations()[:-1], "projection"),
    (operations() + (EnsureReviewProjection(PR),), "projection"),
    (operations(dismissals=counted(1) * 2), "twice"),
    (operations()[:-1] + (EnsureReviewProjection(PullRequestRef(10)),), "one pull request"),
    (operations() + (RenewResolveAllowance("episode"),), "bounded tuple"),
    (list(operations()), "bounded tuple"),
    (operations(dismissals=counted(MAX_COUNTED_REVIEWS + 1)), "bounded tuple"),
])
def test_pending_review_rearm_rejects_malformed_obligations(ops, match):
    with pytest.raises(ValueError, match=match):
        pending_intent(ops=ops)


def test_counted_review_bound_leaves_room_for_the_projection_command():
    assert MAX_COUNTED_REVIEWS == MAX_REARM_COMMANDS - 1 == 63


def test_a_maximal_ceiling_produces_a_representable_pending_intent():
    reviews = counted(MAX_COUNTED_REVIEWS)
    cause = ReviewRejectionCeiling(EPISODE, HEAD, RULE.review_bot, reviews, MAX_COUNTED_REVIEWS + 1, ())
    commands = tuple(CommandId(f"dismiss-{index}") for index in range(MAX_COUNTED_REVIEWS)) + (CommandId("projection"),)
    intent = pending_intent(ops=operations(dismissals=reviews), commands=commands, authorized=reviews)
    assert len(intent.required_commands) == MAX_REARM_COMMANDS and intent.dismissals == reviews
    before = replace(held(), gates=(ceiling_gate(cause=cause),))
    after = replace(pending(), gates=(RearmingGate(ceiling_gate(cause=cause), "request:1"),), intent=intent)
    transition = WorkflowChanged(before.revision, WorkflowMutation.REQUEST_REARM, after,
                                 authorized(request(before), preflight(reviews=reviews)), prior=before)
    assert transition.snapshot.intent == intent
    with pytest.raises(ValueError):
        ReviewRejectionCeiling(EPISODE, HEAD, "bot", reviews, MAX_COUNTED_REVIEWS + 2, ())


def test_observed_ceiling_may_exceed_the_authorization_bound_but_cannot_be_authorized():
    observed = counted(MAX_COUNTED_REVIEWS + 1)
    cause = ReviewRejectionCeiling(EPISODE, HEAD, RULE.review_bot, observed, 3, ())
    assert len(cause.counted) == MAX_COUNTED_REVIEWS + 1
    assert len(ReviewRejectionCeiling(EPISODE, HEAD, "bot", counted(MAX_OBSERVED_REVIEWS), 3, ()).counted) == MAX_OBSERVED_REVIEWS
    with pytest.raises(ValueError):
        ReviewRejectionCeiling(EPISODE, HEAD, "bot", counted(MAX_OBSERVED_REVIEWS + 1), 3, ())
    # The only authorization an envelope accepts is the whole observed set,
    # which no pending intent can carry: increment 3 policy refuses visibly.
    with pytest.raises(ValueError):
        pending_intent(ops=operations(dismissals=()), authorized=observed)
    before = replace(held(), gates=(ceiling_gate(cause=cause),))
    after = replace(pending(), gates=(RearmingGate(ceiling_gate(cause=cause), "request:1"),),
                    intent=pending_intent(ops=operations(dismissals=observed[:1]), authorized=observed[:MAX_COUNTED_REVIEWS]))
    with pytest.raises(ValueError, match="exactly the gate's observed counted set"):
        WorkflowChanged(before.revision, WorkflowMutation.REQUEST_REARM, after,
                        authorized(request(before), preflight(reviews=observed[:1])), prior=before)


def test_pending_review_rearm_cannot_exceed_the_counted_review_bound():
    reviews = counted(MAX_COUNTED_REVIEWS + 1)
    with pytest.raises(ValueError):
        pending_intent(ops=operations(dismissals=reviews), authorized=reviews)
    with pytest.raises(ValueError, match="bounded"):
        pending_intent(ops=operations(dismissals=()), authorized=reviews)


@pytest.mark.parametrize("value", [counted(1) * 2, list(counted()), ("review-0",), counted(MAX_COUNTED_REVIEWS + 1)])
def test_authorized_set_is_unique_bounded_and_typed_in_every_carrying_intent(value):
    for build in (lambda: pending_intent(ops=operations(dismissals=()), authorized=value),
                  lambda: SupersededReviewRearm("r", GATE, 1, StopReason.TARGET_TERMINAL, "e", (), (), value, RULE, PROJ),
                  lambda: CancelledReviewRearm("r", GATE, 1, "cancel", "why", (), (), value, RULE, PROJ)):
        with pytest.raises(ValueError):
            build()


def test_dismissal_obligations_name_only_authorized_reviews():
    with pytest.raises(ValueError, match="authorized counted reviews"):
        pending_intent(authorized=counted(1))
    # Reviews already dismissed before acceptance stay authorized without a new obligation.
    assert pending_intent(ops=operations(dismissals=counted(1)), authorized=counted(2)).authorized == counted(2)
    assert pending_intent(ops=operations(dismissals=()), authorized=()).dismissals == ()


def test_stopped_review_intents_retain_the_exact_authorized_set():
    for state in (cancelled(), superseded()):
        assert state.intent.authorized == pending().intent.authorized == counted()


@pytest.mark.parametrize("commands", [(), (CommandId("a"), CommandId("a")), tuple(CommandId(f"c{i}") for i in range(65)), (CommandId(""),)])
def test_pending_review_rearm_requires_bounded_unique_durable_commands(commands):
    with pytest.raises(ValueError):
        pending_intent(commands=commands)


@pytest.mark.parametrize("from_generation, to_generation", [(0, 0), (0, 2), (1, 1), (-1, 0)])
def test_review_rearm_advances_generation_exactly_once(from_generation, to_generation):
    with pytest.raises(ValueError):
        PendingReviewRearm("request", GATE, from_generation, to_generation, operations(), (CommandId("c"),), counted(), RULE,
                           ReviewProjectionPlan(CommandId("c"), None, Label.NEEDS_HUMAN_REVIEW))


def test_terminal_review_intents_keep_verified_and_retired_obligations_disjoint():
    with pytest.raises(ValueError, match="observed stop reason"):
        SupersededReviewRearm("r", GATE, 1, StopReason.OPERATOR_CANCELLED, "e", (), (), (), RULE, PROJ)
    with pytest.raises(ValueError, match="observed stop reason"):
        SupersededReviewRearm("r", GATE, 1, "decision_changed", "e", (), (), (), RULE, PROJ)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="also be retired"):
        SupersededReviewRearm("r", GATE, 1, StopReason.TARGET_TERMINAL, "e", (CommandId("c"),), (CommandId("c"),), (), RULE, PROJ)
    with pytest.raises(ValueError, match="also be retired"):
        CancelledReviewRearm("r", GATE, 1, "cancel", "why", (CommandId("c"),), (CommandId("c"),), (), RULE, PROJ)
    with pytest.raises(ValueError):
        CancelledReviewRearm("r", GATE, 1, "cancel", "", (), (), (), RULE, PROJ)
    with pytest.raises(ValueError):
        CancelledReviewRearm("r", GATE, 1, "cancel", "x" * (MAX_DETAIL_CHARS + 1), (), (), (), RULE, PROJ)
    with pytest.raises(ValueError):
        CompletedReviewRearm("r", GATE, 0, ())
    assert CompletedReviewRearm("r", GATE, 1, ()).verified_commands == ()


# -- review snapshot invariants -----------------------------------------------

def test_review_snapshots_in_every_lifecycle_state_are_constructible():
    for state in (held(), pending(), completed(), cancelled(), superseded()):
        assert state.binding == WorkflowBinding(state.revision.fence, REVIEW, EPISODE)


def test_review_snapshot_requires_review_authority_pull_request_and_review_episode():
    with pytest.raises(ValueError, match="only review rejections"):
        ReviewWorkflowSnapshot(revision(), RESOLVE, ReviewEpisode(EPISODE))
    with pytest.raises(ValueError, match="requires a pull request"):
        ReviewWorkflowSnapshot(revision(key=ISSUE_KEY), REVIEW, ReviewEpisode(EPISODE))
    with pytest.raises(ValueError, match="validated revision"):
        ReviewWorkflowSnapshot(revision(), REVIEW, ResolveEpisode(EPISODE))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only resolve"):
        WorkflowSnapshot(revision(), REVIEW, ResolveEpisode(EPISODE))


def test_reviewed_baseline_belongs_only_to_the_current_generation():
    state = ReviewWorkflowSnapshot(revision(1, 1), REVIEW, ReviewEpisode(EPISODE, "", baseline(1)))
    assert state.episode.continuation.reviewed_generation == 1
    with pytest.raises(ValueError, match="current generation"):
        ReviewWorkflowSnapshot(revision(2, 2), REVIEW, ReviewEpisode(EPISODE, "", baseline(1)))
    with pytest.raises(ValueError, match="current generation"):
        ReviewWorkflowSnapshot(revision(2, 2), REVIEW, ReviewEpisode(EPISODE, "", ReviewedBaseline(HEAD, PATCH, 1, ImportedBaseline("i"))))
    with pytest.raises(ValueError, match="future generation"):
        ReviewWorkflowSnapshot(revision(1, 1), REVIEW, ReviewEpisode(EPISODE, "", FreshReviewRequired(2)))


def test_bound_baseline_from_another_pull_request_or_epoch_is_rejected():
    foreign = review_binding(1, key=WorkflowKey("o/r", PullRequestRef(10)))
    with pytest.raises(ValueError, match="provenance"):
        ReviewWorkflowSnapshot(revision(1, 1), REVIEW, ReviewEpisode(EPISODE, "", baseline(1, binding=foreign)))
    other_epoch = WorkflowBinding(WorkflowFence(PR_KEY, 1), FamilyAuthority(WorkflowFamily.REVIEW_REJECTIONS, 2), EPISODE)
    with pytest.raises(ValueError, match="provenance"):
        ReviewWorkflowSnapshot(revision(1, 1), REVIEW, ReviewEpisode(EPISODE, "", baseline(1, binding=other_epoch)))


def test_review_snapshot_gate_invariants_mirror_resolve():
    with pytest.raises(ValueError, match="unique"):
        replace(held(), gates=(ceiling_gate(), ceiling_gate()))
    with pytest.raises(ValueError, match="future generation"):
        replace(held(), gates=(ceiling_gate(1),))
    with pytest.raises(ValueError, match="bounded tuple"):
        replace(held(), gates=[ceiling_gate()])
    with pytest.raises(ValueError, match="rearming gate requires"):
        replace(held(), gates=(RearmingGate(ceiling_gate(), "request:1"),))


def test_pending_review_intent_owns_its_generation_gate_and_fresh_review():
    state = pending()
    with pytest.raises(ValueError, match="current generation"):
        replace(state, revision=revision(2, 2), episode=replace(state.episode, continuation=FreshReviewRequired(2)))
    with pytest.raises(ValueError, match="exactly one rearming gate"):
        replace(state, gates=(ceiling_gate(),))
    with pytest.raises(ValueError, match="exactly one rearming gate"):
        replace(state, gates=(RearmingGate(ceiling_gate(), "request:other"),))
    with pytest.raises(ValueError, match="owes a fresh review"):
        replace(state, episode=replace(state.episode, continuation=FreshReviewRequired(0)))
    with pytest.raises(ValueError, match="owes a fresh review"):
        replace(state, episode=replace(state.episode, continuation=baseline(1)))


def test_pending_review_intent_must_target_this_pull_request():
    other = PullRequestRef(10)
    intent = pending_intent(ops=operations(pr=other))
    with pytest.raises(ValueError, match="belong to this workflow"):
        replace(pending(), intent=intent)


def test_pending_review_intent_may_authorize_only_the_gate_counted_reviews():
    unrelated = (CountedReview("human-review", HEAD),)
    with pytest.raises(ValueError, match="authorize only the gate's counted reviews"):
        replace(pending(), intent=pending_intent(ops=operations(dismissals=unrelated), authorized=unrelated))
    with pytest.raises(ValueError, match="authorize only the gate's counted reviews"):
        replace(pending(), intent=pending_intent(ops=operations(dismissals=()), authorized=counted() + unrelated))
    decision = OpenGate(GATE, 0, RearmNeedsDecision("request:0", (), StopReason.DECISION_CHANGED), NoAuthorizedRemediation("n"))
    continued = replace(pending(), gates=(RearmingGate(decision, "request:1"),))
    assert continued.intent is not None


def test_review_snapshot_rejects_resolve_intents_and_future_terminal_intents():
    from tests.domain.test_workflow_policy import exhausted, start
    resolve_intent = start(exhausted()).snapshot.intent
    with pytest.raises(ValueError, match="review variant"):
        replace(held(), intent=resolve_intent)
    with pytest.raises(ValueError, match="future generation"):
        replace(held(), intent=CompletedReviewRearm("request:1", GATE, 1, ()))


def test_resolve_snapshot_rejects_review_intents():
    with pytest.raises(ValueError, match="validated variant"):
        replace(resolve_snapshot(), intent=CompletedReviewRearm("request:1", GATE, 0 + 1, ()))


# -- transition envelopes -----------------------------------------------------

def test_envelopes_accept_either_family_snapshot_and_nothing_else():
    for state in (held(), resolve_snapshot()):
        assert WorkflowCommitted(state).snapshot == state
        assert WorkflowUnchanged(state).snapshot == state
        assert HeldProjection(state).held == bool(state.gates)
    for build in (WorkflowCommitted, WorkflowUnchanged, HeldProjection):
        with pytest.raises(ValueError):
            build(ReviewEpisode(EPISODE))  # type: ignore[arg-type]


def authorized(command, preflight_value=None):
    return AuthorizedOperatorCommand(ACTOR, command, preflight_value)


def test_review_request_rearm_is_a_valid_generation_advancing_transition():
    before = held()
    transition = WorkflowChanged(before.revision, WorkflowMutation.REQUEST_REARM, pending(),
                                 authorized(request(before), preflight()), prior=before)
    assert transition.snapshot.revision.fence.generation == 1


def test_review_rearm_cannot_advance_without_its_pending_review_intent():
    before = held()
    after = replace(before, revision=revision(1, 1))
    with pytest.raises(ValueError, match="matching pending intent"):
        WorkflowChanged(before.revision, WorkflowMutation.REQUEST_REARM, after, authorized(request(before), preflight()), prior=before)


def test_review_continuation_and_cancellation_use_review_terminal_intents():
    stopped = cancelled()
    cancel = CancelRearm("cancel:1", pending().revision, "request:1", "scope changed")
    WorkflowChanged(pending().revision, WorkflowMutation.STOP_REARM, stopped, authorized(cancel), prior=pending())
    WorkflowChanged(pending().revision, WorkflowMutation.STOP_REARM, superseded(), ObservedTransition(("head-moved:1",)), prior=pending())
    WorkflowChanged(pending().revision, WorkflowMutation.RECONCILE_REARM, completed(), ObservedTransition(("receipts",)), prior=pending())
    with pytest.raises(ValueError, match="terminal intent"):
        WorkflowChanged(pending().revision, WorkflowMutation.STOP_REARM, completed(), ObservedTransition(("x",)), prior=pending())
    with pytest.raises(ValueError, match="completed intent"):
        WorkflowChanged(pending().revision, WorkflowMutation.RECONCILE_REARM, cancelled(), ObservedTransition(("x",)), prior=pending())
    wrong = CancelRearm("cancel:other", pending().revision, "request:1", "scope changed")
    with pytest.raises(ValueError, match="matching operator cancellation"):
        WorkflowChanged(pending().revision, WorkflowMutation.STOP_REARM, stopped, authorized(wrong), prior=pending())


def recorded_review(*, source=None, audit=None, generation=1):
    before = ReviewWorkflowSnapshot(revision(generation, 1), REVIEW, ReviewEpisode(EPISODE, "", FreshReviewRequired(generation)))
    reviewed = ReviewedBaseline(HEAD, PATCH, generation, source or BoundReviewSubmission(CommandId("submit:1"), review_binding(generation)))
    after = replace(before, revision=revision(generation, 2), episode=replace(before.episode, continuation=reviewed))
    return WorkflowChanged(before.revision, WorkflowMutation.RECORD_REVIEW, after, audit or ObservedTransition(("submit:1",)), prior=before)


def test_record_review_establishes_a_bound_baseline_of_the_current_generation():
    transition = recorded_review()
    assert transition.snapshot.episode.continuation.reviewed_generation == 1


@pytest.mark.parametrize("changes, match", [
    ({"source": ImportedBaseline("import:1")}, "bound baseline"),
    ({"audit": ObservedTransition(("other-evidence",))}, "bound submission evidence"),
    ({"audit": authorized(CancelRearm("c", revision(1, 1), "r", "why"))}, "bound submission evidence"),
])
def test_record_review_requires_bound_submission_provenance(changes, match):
    with pytest.raises(ValueError, match=match):
        recorded_review(**changes)


def test_record_review_cannot_leave_a_fresh_review_owed():
    before = ReviewWorkflowSnapshot(revision(1, 1), REVIEW, ReviewEpisode(EPISODE, "", FreshReviewRequired(1)))
    after = replace(before, revision=revision(1, 2))
    with pytest.raises(ValueError, match="bound baseline"):
        WorkflowChanged(before.revision, WorkflowMutation.RECORD_REVIEW, after, ObservedTransition(("submit:1",)), prior=before)


def test_record_review_is_review_only_and_record_result_is_resolve_only():
    state = resolve_snapshot()
    after = replace(state, revision=revision(0, 1, key=ISSUE_KEY))
    with pytest.raises(ValueError, match="review rejection family"):
        WorkflowChanged(state.revision, WorkflowMutation.RECORD_REVIEW, after, ObservedTransition(("submit:1",)))
    before = held()
    attempt = WorkflowAttemptIdentity(before.binding, RunId("run:1"), "leg:1")
    result = ResolveAttemptResult(attempt, ResolveResultKind.COUNTED_REQUEUE)
    with pytest.raises(ValueError, match="resolve family"):
        WorkflowChanged(before.revision, WorkflowMutation.RECORD_RESULT, replace(before, revision=revision(0, 1)), result)


# -- operator authorization and preflight ---------------------------------------

def test_review_rearm_requires_this_pull_requests_complete_review_preflight():
    before = held()
    continued = ContinueGrantedRearm("request:2", before.revision, "request:1")
    for command in (request(before), continued):
        with pytest.raises(ValueError, match="complete review preflight"):
            authorized(command)
        with pytest.raises(ValueError, match="complete review preflight"):
            authorized(command, preflight(WorkflowKey("o/r", PullRequestRef(10))))
        assert authorized(command, preflight()).preflight == preflight()


def test_review_preflight_cannot_authorize_resolve_or_ride_on_other_commands():
    from tests.domain.test_workflow_policy import exhausted
    from tests.domain.test_workflow_policy import request as resolve_request
    with pytest.raises(ValueError, match="observed workspace acceptance"):
        authorized(resolve_request(exhausted()), preflight())
    cancel = CancelRearm("cancel", held().revision, "request:1", "why")
    retry = AuthorizeAbsentRetry("grant", held().revision, CommandId("c"), "gate", "evidence")
    for command in (cancel, retry):
        with pytest.raises(ValueError, match="does not accept"):
            authorized(command, preflight())


@pytest.mark.parametrize("changes", [
    {"pr_key": ISSUE_KEY}, {"head_sha": "abc"}, {"complete_reviews_evidence_id": ""},
    {"counted": counted(1) * 2}, {"counted": counted(MAX_OBSERVED_REVIEWS + 1)}, {"counted": list(counted())},
    {"state": "open"}, {"labels": frozenset({"ai-ready"})}, {"labels": {Label.AI_READY}},
    {"external_holds": ["hold"]}, {"external_holds": ("",)}, {"external_holds": ("x" * (MAX_DETAIL_CHARS + 1),)},
    {"external_holds": ("hold",) * 65},
])
def test_review_preflight_is_validated(changes):
    with pytest.raises(ValueError):
        replace(preflight(), **changes)


def test_review_preflight_may_observe_that_no_counted_review_remains():
    assert preflight(reviews=()).counted == ()


def test_review_preflight_carries_pr_state_labels_holds_and_observed_bound():
    value = replace(preflight(reviews=counted(MAX_OBSERVED_REVIEWS)), state=PullRequestState.MERGED,
                    external_holds=("branch protection hold",))
    assert value.state is PullRequestState.MERGED and Label.NEEDS_HUMAN_REVIEW in value.labels
    assert len(value.counted) == MAX_OBSERVED_REVIEWS and value.external_holds == ("branch protection hold",)


# -- migration-required per family --------------------------------------------

def test_migration_required_reason_is_family_scoped_and_resolve_text_is_stable():
    assert WorkflowMigrationRequired(ISSUE_KEY).reason == "resolve_migration_required"
    assert WorkflowMigrationRequired(ISSUE_KEY) == WorkflowMigrationRequired(ISSUE_KEY, WorkflowFamily.RESOLVE)
    review = WorkflowMigrationRequired(PR_KEY, WorkflowFamily.REVIEW_REJECTIONS)
    assert review.reason == "review_rejections_migration_required"


@pytest.mark.parametrize("key, family", [
    (PR_KEY, WorkflowFamily.RESOLVE), (ISSUE_KEY, WorkflowFamily.REVIEW_REJECTIONS), (PR_KEY, "review_rejections"),
])
def test_migration_required_family_must_own_the_queried_target(key, family):
    with pytest.raises(ValueError):
        WorkflowMigrationRequired(key, family)


# -- observations ---------------------------------------------------------------

def fact(review_id="r1", *, at=NOW, state=SubmittedReviewState.CHANGES_REQUESTED, commit=HEAD, author="review-bot[bot]"):
    return SubmittedReviewFact(review_id, author, state, commit, at)


def test_review_fact_allows_explicitly_unresolved_commit_but_not_malformed():
    assert fact(commit=None).commit_id is None
    for bad in ("", "abc", "A" * 40):
        with pytest.raises(ValueError):
            fact(commit=bad)
    with pytest.raises(ValueError):
        fact(state="CHANGES_REQUESTED")
    with pytest.raises(ValueError):
        fact(at=datetime(2026, 9, 25))
    with pytest.raises(ValueError):
        fact(review_id="")
    with pytest.raises(ValueError):
        fact(author="")


def test_complete_reviews_are_unique_chronological_and_bounded():
    from tests.fakes.review_workflow import later
    assert CompleteReviews((fact("r1"), fact("r2", at=later(1)), fact("r3", at=later(1)))).reviews[2].review_id == "r3"
    assert CompleteReviews(()).reviews == ()
    with pytest.raises(ValueError, match="duplicate"):
        CompleteReviews((fact("r1"), fact("r1", at=later(1))))
    with pytest.raises(ValueError, match="chronological"):
        CompleteReviews((fact("r1", at=later(2)), fact("r2", at=later(1))))
    many = tuple(fact(f"r{i}", at=later(i)) for i in range(MAX_OBSERVED_REVIEWS))
    assert len(CompleteReviews(many).reviews) == MAX_OBSERVED_REVIEWS
    with pytest.raises(ValueError):
        CompleteReviews(many + (fact("overflow", at=later(MAX_OBSERVED_REVIEWS)),))
    with pytest.raises(ValueError):
        CompleteReviews(("r1",))  # type: ignore[arg-type]


def observed_pr(**changes):
    values = dict(key=PR_KEY, head_sha=HEAD, state=PullRequestState.OPEN,
                  labels=frozenset({Label.NEEDS_HUMAN_REVIEW}), reviews=CompleteReviews((fact(),)),
                  observed_at=NOW)
    values.update(changes)
    return ObservedPullRequest(**values)


def test_partial_review_read_keeps_pr_facts_but_can_never_decide_a_cause():
    partial = PartialReviews("page 3 of reviews failed")
    observation = observed_pr(reviews=partial)
    assert observation.head_sha == HEAD and observation.state is PullRequestState.OPEN
    with pytest.raises(ValueError, match="neither establish nor disprove"):
        decisive_reviews(observation)
    complete = observed_pr()
    assert decisive_reviews(complete) == complete.reviews
    with pytest.raises(ValueError, match="typed review read"):
        observed_pr(reviews=(fact(),))
    with pytest.raises(ValueError):
        decisive_reviews(PullRequestUnavailable(PR_KEY, NOW, "rate limited"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PartialReviews("")
    with pytest.raises(ValueError):
        PartialReviews("x" * (MAX_DETAIL_CHARS + 1))


@pytest.mark.parametrize("changes", [
    {"key": ISSUE_KEY}, {"head_sha": OTHER_HEAD[:10]}, {"state": "open"}, {"labels": frozenset({"ai-ready"})},
    {"labels": {Label.AI_READY}}, {"observed_at": datetime(2026, 9, 25)},
])
def test_observed_pull_request_is_validated(changes):
    with pytest.raises(ValueError):
        observed_pr(**changes)


def test_pull_request_absence_and_unavailability_require_pull_request_scope():
    assert PullRequestAbsent(PR_KEY, NOW).key == PR_KEY
    assert PullRequestUnavailable(PR_KEY, NOW, "rate limited").reason == "rate limited"
    with pytest.raises(ValueError):
        PullRequestAbsent(ISSUE_KEY, NOW)
    with pytest.raises(ValueError):
        PullRequestUnavailable(PR_KEY, NOW, "")
    with pytest.raises(ValueError):
        PullRequestUnavailable(ISSUE_KEY, NOW, "x")


# -- review provenance carried by the transition envelope -----------------------

HUMAN = CountedReview("human-review", HEAD)


def rearm(*, before=None, after=None, pf=None, dismissals=None, allowed=None, prior="before"):
    before = before or held()
    reviews = counted() if dismissals is None else dismissals
    intent = pending_intent(ops=operations(dismissals=reviews), authorized=counted() if allowed is None else allowed)
    after = after or replace(pending(), intent=intent)
    return WorkflowChanged(before.revision, WorkflowMutation.REQUEST_REARM, after,
                           authorized(request(before), pf or preflight(reviews=reviews)),
                           prior=before if prior == "before" else prior)


def test_review_rearm_may_dismiss_only_what_is_still_counted():
    transition = rearm(pf=preflight(reviews=counted(1)), dismissals=counted(1))
    intent = transition.snapshot.intent
    assert isinstance(intent, PendingReviewRearm)
    assert intent.dismissals == counted(1) and intent.authorized == counted()


@pytest.mark.parametrize("changes, match", [
    ({"prior": None}, "exact prior review snapshot"),
    ({"prior": replace(held(), revision=revision(0, 1))}, "expected revision or authority"),
    ({"prior": replace(held(), episode=ReviewEpisode("other-episode"))}, "replace its episode"),
    ({"allowed": counted(1), "dismissals": counted(1), "pf": preflight(reviews=counted(1))}, "exactly the gate's observed counted set"),
    ({"pf": preflight(head=OTHER_HEAD)}, "head moved"),
    ({"pf": preflight(reviews=counted() + (HUMAN,))}, "outside the gate's observed set"),
    ({"pf": preflight(reviews=counted(1))}, "exactly the freshly counted"),
    ({"pf": preflight(reviews=counted()), "dismissals": counted(1)}, "exactly the freshly counted"),
    ({"pf": preflight(reviews=()), "dismissals": ()}, None),
])
def test_review_rearm_binds_authorization_to_the_gate_and_the_fresh_preflight(changes, match):
    if match is None:
        assert rearm(**changes).snapshot.intent is not None
        return
    with pytest.raises(ValueError, match=match):
        rearm(**changes)


def test_review_rearm_requires_an_open_rejection_ceiling_retaining_its_cause():
    evidence = OpenGate(GATE, 0, EvidenceUnavailable("reviews", NOW, 1, "x"), NoAuthorizedRemediation("n"))
    with pytest.raises(ValueError, match="open review rejection ceiling"):
        rearm(before=replace(held(), gates=(evidence,)))
    wider = ceiling_gate(cause=ReviewRejectionCeiling(EPISODE, HEAD, "review-bot[bot]", counted(3), 3, ()))
    after = replace(pending(), gates=(RearmingGate(wider, "request:1"),))
    with pytest.raises(ValueError, match="exact accepted ceiling cause"):
        rearm(after=after)


def test_resolve_transitions_cannot_carry_a_review_prior():
    from tests.domain.test_workflow_policy import exhausted, start
    transition = start(exhausted())
    assert isinstance(transition, WorkflowChanged)
    with pytest.raises(ValueError, match="do not carry a review prior"):
        replace(transition, prior=held())


def decision_gate(stopped="request:1"):
    return OpenGate("gate:decision", 1, RearmNeedsDecision(stopped, (CommandId("request:1:dismiss-1"),), StopReason.DECISION_CHANGED),
                    NoAuthorizedRemediation("decide the stopped rearm"))


def continued(*, stopped=None, gate=None, allowed=None, pf=None, dismissals=None, stopped_id="request:1",
              prior_gates=None):
    gate = gate or decision_gate()
    prior = replace(stopped or cancelled(), gates=(gate,) if prior_gates is None else prior_gates)
    reviews = counted(1) if dismissals is None else dismissals
    intent = PendingReviewRearm("request:2", "gate:decision", 1, 2, operations(dismissals=reviews),
                                (CommandId("request:2:projection"),), counted() if allowed is None else allowed, RULE,
                                projection("request:2"), gate.cause.obligations)
    after = ReviewWorkflowSnapshot(revision(2, 3), REVIEW, ReviewEpisode(EPISODE, "", FreshReviewRequired(2)),
                                   (RearmingGate(gate, "request:2"),), intent)
    command = ContinueGrantedRearm("request:2", prior.revision, stopped_id)
    return WorkflowChanged(prior.revision, WorkflowMutation.CONTINUE_REARM, after,
                           authorized(command, pf or preflight(reviews=counted(1))), prior=prior)


def test_review_continuation_may_narrow_the_stopped_authorization():
    assert continued().snapshot.revision.fence.generation == 2
    assert continued(stopped=superseded()).snapshot.intent is not None
    narrowed = continued(allowed=counted(1))
    assert isinstance(narrowed.snapshot.intent, PendingReviewRearm) and narrowed.snapshot.intent.authorized == counted(1)


@pytest.mark.parametrize("changes, match", [
    ({"allowed": counted() + (HUMAN,)}, "cannot widen"),
    ({"pf": preflight(reviews=counted(1) + (HUMAN,))}, "outside the stopped authorization"),
    ({"pf": preflight(reviews=()), "dismissals": counted(1)}, "freshly counted"),
    ({"stopped_id": "request:0"}, "current stopped review intent"),
    ({"stopped": completed(), "prior_gates": ()}, "current stopped review intent"),
    ({"gate": decision_gate("request:0"), "prior_gates": (decision_gate(),)}, "stopped request's decision gate"),
])
def test_review_continuation_stays_inside_the_stopped_authorization_chain(changes, match):
    with pytest.raises(ValueError, match=match):
        continued(**changes)


def test_a_stopped_review_rearm_is_visible_as_exactly_its_own_decision_gate():
    stopped = cancelled()
    with pytest.raises(ValueError, match="exactly for the current stopped re-arm"):
        replace(stopped, gates=())
    with pytest.raises(ValueError, match="exactly for the current stopped re-arm"):
        replace(stopped, gates=(decision_gate("request:0"),))
    with pytest.raises(ValueError, match="exactly for the current stopped re-arm"):
        replace(stopped, gates=stopped.gates + (replace(decision_gate(), gate_id="gate:decision-2"),))
    for other in (held(generation=1), completed(), pending()):
        with pytest.raises(ValueError, match="exactly for the current stopped re-arm"):
            replace(other, gates=other.gates + (decision_gate(),))


def test_a_review_continuation_replaces_exactly_its_stopped_decisions_obligations():
    change = continued()
    intent = change.snapshot.intent
    assert isinstance(intent, PendingReviewRearm) and intent.replaces == decision_gate().cause.obligations
    with pytest.raises(ValueError, match="replaces only its stopped decision"):
        replace(change.snapshot, intent=replace(intent, replaces=(CommandId("request:9:projection"),)))
    with pytest.raises(ValueError, match="exactly the retired dismissals"):
        replace(change, snapshot=replace(change.snapshot, intent=replace(intent, replaces=())))
    with pytest.raises(ValueError, match="cannot replace its own obligations"):
        replace(intent, replaces=intent.required_commands)


def test_pending_review_rearm_requires_a_ceiling_or_an_earlier_stopped_decision():
    evidence = OpenGate(GATE, 0, EvidenceUnavailable("reviews", NOW, 1, "x"), NoAuthorizedRemediation("n"))
    with pytest.raises(ValueError, match="earlier stopped decision"):
        replace(pending(), gates=(RearmingGate(evidence, "request:1"),))
    own = OpenGate(GATE, 0, RearmNeedsDecision("request:1", (), StopReason.DECISION_CHANGED), NoAuthorizedRemediation("n"))
    with pytest.raises(ValueError, match="earlier stopped decision"):
        replace(pending(), gates=(RearmingGate(own, "request:1"),))


IMPORTED = ReviewedBaseline(HEAD, PATCH, 0, ImportedBaseline("import:1"))


@pytest.mark.parametrize("mutation, prior, after, audit", [
    (WorkflowMutation.OPEN_GATE, ReviewWorkflowSnapshot(revision(0, 0), REVIEW, ReviewEpisode(EPISODE)),
     ReviewWorkflowSnapshot(revision(0, 1), REVIEW, ReviewEpisode(EPISODE, "", IMPORTED), (ceiling_gate(),)),
     ObservedTransition(("ceiling:1",))),
    (WorkflowMutation.RECONCILE_REARM, pending(),
     replace(completed(), episode=ReviewEpisode(EPISODE, "fix the flaky test", baseline(1))),
     ObservedTransition(("receipts",))),
    (WorkflowMutation.STOP_REARM, pending(),
     replace(superseded(), episode=ReviewEpisode(EPISODE, "fix the flaky test", baseline(1))),
     ObservedTransition(("head-moved:1",))),
    (WorkflowMutation.OPEN_GATE, held(),
     replace(held(), revision=revision(0, 1), episode=ReviewEpisode(EPISODE, "", FreshReviewRequired(0)),
             gates=(ceiling_gate(), replace(ceiling_gate(), gate_id="gate:2"))), None),
])
def test_only_a_recorded_review_or_accepted_rearm_changes_the_continuation(mutation, prior, after, audit):
    if audit is None:
        # Control: an observed transition preserving the continuation is fine.
        assert WorkflowChanged(prior.revision, mutation, after, ObservedTransition(("x",)), prior=prior).prior == prior
        return
    with pytest.raises(ValueError, match="only a recorded review, an accepted rearm or a ceiling release"):
        WorkflowChanged(prior.revision, mutation, after, audit, prior=prior)


def test_imported_baseline_survives_only_by_preservation():
    prior = ReviewWorkflowSnapshot(revision(0, 0), REVIEW, ReviewEpisode(EPISODE, "", IMPORTED))
    after = replace(prior, revision=revision(0, 1), gates=(ceiling_gate(),))
    assert WorkflowChanged(prior.revision, WorkflowMutation.OPEN_GATE, after, ObservedTransition(("x",)), prior=prior)


@pytest.mark.parametrize("remove, add", [
    (None, Label.AI_PR_OPEN), (Label.AI_PR_OPEN, Label.NEEDS_HUMAN_REVIEW),
    (Label.NEEDS_HUMAN_REVIEW, Label.NEEDS_HUMAN_REVIEW), ("needs-human-review", Label.AI_PR_OPEN), (None, None),
])
def test_a_review_projection_plan_is_either_a_release_or_a_hold(remove, add):
    with pytest.raises(ValueError):
        ReviewProjectionPlan(CommandId("c"), remove, add)


def test_a_pending_review_rearm_plans_its_projection_among_its_obligations():
    with pytest.raises(ValueError, match="projection among its obligations"):
        replace(pending_intent(), projection=replace(PROJ, command_id=CommandId("elsewhere")))
    for build in (cancelled, superseded):
        intent = build().intent
        assert isinstance(intent, (CancelledReviewRearm, SupersededReviewRearm))
        with pytest.raises(ValueError, match="planned projection"):
            replace(intent, projection=None)
