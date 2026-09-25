"""Tagged-codec round trips and database-boundary rejection for review-family values."""

from dataclasses import replace
import json
import random

import pytest

from issue_agent.adapters.workflow_codec import (
    MAX_WORKFLOW_PAYLOAD_BYTES, WorkflowEncodingError, decode_workflow, encode_workflow,
)
from issue_agent.domain.contracts import CommandId
from issue_agent.domain.labels import Label
from issue_agent.domain.review_workflow import (
    MAX_COUNTED_REVIEWS, BoundReviewSubmission, CompleteReviews, CountedReview, FreshReviewRequired,
    ImportedBaseline, ObservedPullRequest, PartialReviews, PendingReviewRearm, PullRequestAbsent, PullRequestState,
    PullRequestUnavailable, ReviewEpisode, ReviewRearmPreflight, ReviewRejectionCeiling, ReviewWorkflowSnapshot,
    ReviewedBaseline, SubmittedReviewFact, SubmittedReviewState,
)
from issue_agent.domain.workflow import (
    AuthorizedOperatorCommand, ContinueGrantedRearm, NoAuthorizedRemediation, OpenGate, RearmNeedsDecision,
    RemoteOutcomeUncertain, ResolveCeiling, StopReason, WorkflowSnapshot,
)
from issue_agent.domain.workflow_identity import (
    PullRequestRef, WorkflowBinding, WorkflowFence, WorkflowKey,
)
from tests.fakes.review_workflow import (
    ACTOR, EPISODE, HEAD, NOW, PATCH, PR_KEY, REVIEW, cancelled, ceiling_cause, completed, counted, held, later,
    operations, pending, pending_intent, preflight, request, revision, superseded,
)


def roundtrip(value, root):
    encoded = encode_workflow(value)
    decoded = decode_workflow(encoded, root)
    assert decoded == value
    assert encode_workflow(decoded) == encoded
    return decoded


def reviewed(generation=1):
    binding = WorkflowBinding(WorkflowFence(PR_KEY, generation), REVIEW, EPISODE)
    return ReviewedBaseline(HEAD, PATCH, generation, BoundReviewSubmission(CommandId("submit:1"), binding))


def observed():
    reviews = CompleteReviews((
        SubmittedReviewFact("r1", "review-bot[bot]", SubmittedReviewState.CHANGES_REQUESTED, HEAD, NOW),
        SubmittedReviewFact("r2", "alice", SubmittedReviewState.COMMENTED, None, later(5)),
    ))
    return ObservedPullRequest(PR_KEY, HEAD, PullRequestState.OPEN, frozenset({Label.NEEDS_HUMAN_REVIEW, Label.AI_REVIEW}),
                               reviews, later(10))


@pytest.mark.parametrize("build", [held, pending, completed, cancelled, superseded], ids=lambda f: f.__name__)
def test_every_review_snapshot_lifecycle_state_survives_restart(build):
    state = build()
    recovered = roundtrip(state, ReviewWorkflowSnapshot)
    assert recovered.binding == state.binding


def test_reviewed_baseline_and_episode_json_roundtrip():
    state = ReviewWorkflowSnapshot(revision(1, 3), REVIEW, ReviewEpisode(EPISODE, "multi\nline", reviewed()))
    roundtrip(state, ReviewWorkflowSnapshot)
    roundtrip((state.authority, state.episode), tuple)
    roundtrip(ReviewedBaseline(HEAD, PATCH, 4, ImportedBaseline("legacy:patch-id")), ReviewedBaseline)


def test_operator_audit_observations_and_reads_roundtrip():
    roundtrip(AuthorizedOperatorCommand(ACTOR, request(), preflight()), AuthorizedOperatorCommand)
    roundtrip(AuthorizedOperatorCommand(ACTOR, ContinueGrantedRearm("request:2", held().revision, "request:1"), preflight(reviews=())),
              AuthorizedOperatorCommand)
    roundtrip(observed(), ObservedPullRequest)
    roundtrip(PartialReviews("page 2 failed"), PartialReviews)
    roundtrip(PullRequestAbsent(PR_KEY, NOW), PullRequestAbsent)
    roundtrip(PullRequestUnavailable(PR_KEY, NOW, "rate limited"), PullRequestUnavailable)
    roundtrip(ceiling_cause(), ReviewRejectionCeiling)
    roundtrip(replace(observed(), reviews=PartialReviews("page 2 failed")), ObservedPullRequest)
    unresolved = ReviewRejectionCeiling(EPISODE, HEAD, "bot", (CountedReview("r1", None), CountedReview("r2", HEAD)), 3, ())
    assert roundtrip(unresolved, ReviewRejectionCeiling).counted[0].commit_id is None
    holds = replace(preflight(), state=PullRequestState.CLOSED, external_holds=("merge queue hold",))
    assert roundtrip(AuthorizedOperatorCommand(ACTOR, request(), holds), AuthorizedOperatorCommand).preflight == holds


def random_snapshot(rng: random.Random) -> ReviewWorkflowSnapshot:
    count = rng.randint(0, MAX_COUNTED_REVIEWS)
    commits = tuple(f"{rng.getrandbits(160):040x}" for _ in range(rng.randint(0, 5)))
    reviews = tuple(CountedReview(f"review-{rng.getrandbits(48)}-{index}", f"{rng.getrandbits(160):040x}") for index in range(count))
    cause = ReviewRejectionCeiling(EPISODE, f"{rng.getrandbits(256):064x}", "bot", reviews, rng.randint(1, count + 1),
                                   tuple(dict.fromkeys(commits)))
    gate = OpenGate("gate:review-ceiling", 0, cause, NoAuthorizedRemediation("decide"))
    guidance = "".join(rng.choice("ab \n\té") for _ in range(rng.randint(0, 40)))
    extra = tuple(
        OpenGate(f"gate:uncertain-{index}", 0, RemoteOutcomeUncertain(CommandId(f"c{index}"), later(index), index, "unknown"),
                 NoAuthorizedRemediation("reconcile only"))
        for index in range(rng.randint(0, 3))
    )
    generation = rng.randint(0, 5)
    return ReviewWorkflowSnapshot(
        revision(generation, generation + rng.randint(0, 9)), REVIEW,
        ReviewEpisode(EPISODE, guidance, FreshReviewRequired(rng.randint(0, generation))), (gate,) + extra,
    )


@pytest.mark.parametrize("seed", range(60))
def test_generated_review_snapshots_roundtrip_byte_stably(seed):
    roundtrip(random_snapshot(random.Random(seed)), ReviewWorkflowSnapshot)


def mutate(value, path, replacement):
    raw = json.loads(encode_workflow(value))
    node = raw["value"]
    for key in path[:-1]:
        node = node[key]
    if replacement is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = replacement
    return json.dumps(raw)


@pytest.mark.parametrize("path, replacement", [
    (("fields", "counted"), []),                                   # exhaustion no longer holds
    (("fields", "counted", 0, "fields", "commit_id"), ""),         # unplaceable counted review
    (("fields", "counted", 0, "fields", "commit_id"), "A" * 40),
    (("fields", "counted", 1, "fields", "review_id"), "review-0"),  # duplicate identity
    (("fields", "ceiling"), 0),
    (("fields", "ceiling"), True),
    (("fields", "head_sha"), "a" * 12),
    (("fields", "non_resetting_commits"), ["a" * 40, "a" * 40]),
    (("fields", "review_bot"), KeyError),
    (("fields", "surprise"), 1),
    (("type",), "ReviewRejectionCeilingV2"),
])
def test_malformed_persisted_ceiling_never_becomes_authority(path, replacement):
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(mutate(ceiling_cause(), path, replacement), ReviewRejectionCeiling)


@pytest.mark.parametrize("path, replacement", [
    (("fields", "authority", "fields", "family", "value"), "resolve"),
    (("fields", "authority", "fields", "family", "value"), "reviews"),
    (("fields", "revision", "fields", "fence", "fields", "key", "fields", "target", "type"), "IssueRef"),
    (("fields", "episode", "type"), "ResolveEpisode"),
    (("fields", "episode", "fields", "continuation", "fields", "owed_since_generation"), 7),
    (("fields", "intent", "fields", "to_generation"), 2),
    (("fields", "gates"), []),
])
def test_malformed_persisted_review_snapshot_is_rejected(path, replacement):
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(mutate(pending(), path, replacement), ReviewWorkflowSnapshot)


def test_cross_family_payloads_fail_at_the_database_boundary():
    with pytest.raises(WorkflowEncodingError, match="wrong root"):
        decode_workflow(encode_workflow(held()), WorkflowSnapshot)
    from tests.domain.test_workflow_policy import exhausted
    resolve = exhausted()
    with pytest.raises(WorkflowEncodingError, match="wrong root"):
        decode_workflow(encode_workflow(resolve), ReviewWorkflowSnapshot)
    # A resolve ceiling smuggled into a review snapshot, and vice versa.
    resolve_gate = OpenGate("gate:resolve", 0, ResolveCeiling("episode", 3, 3), NoAuthorizedRemediation("n"))
    raw = json.loads(encode_workflow(held()))
    raw["value"]["fields"]["gates"] = [json.loads(encode_workflow(resolve_gate))["value"]]
    with pytest.raises(WorkflowEncodingError, match="resolve ceiling cause"):
        decode_workflow(json.dumps(raw), ReviewWorkflowSnapshot)
    raw = json.loads(encode_workflow(resolve))
    raw["value"]["fields"]["gates"].append(json.loads(encode_workflow(held().gates[0]))["value"])
    with pytest.raises(WorkflowEncodingError, match="review rejection cause"):
        decode_workflow(json.dumps(raw), WorkflowSnapshot)


def test_observation_review_read_decodes_only_as_a_typed_read():
    raw = json.loads(encode_workflow(observed()))
    raw["value"]["fields"]["reviews"] = json.loads(encode_workflow(counted(1)[0]))["value"]
    with pytest.raises(WorkflowEncodingError, match="typed review read"):
        decode_workflow(json.dumps(raw), ObservedPullRequest)
    raw["value"]["fields"]["reviews_complete"] = raw["value"]["fields"].pop("reviews")
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ObservedPullRequest)


def test_review_intent_and_submission_cannot_cross_families_when_decoded():
    raw = json.loads(encode_workflow(reviewed()))
    raw["value"]["fields"]["source"]["fields"]["binding"]["fields"]["authority"]["fields"]["family"]["value"] = "resolve"
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ReviewedBaseline)
    raw = json.loads(encode_workflow(pending_intent()))
    raw["value"]["fields"]["operations"].append({"type": "RenewResolveAllowance", "fields": {"episode_id": EPISODE}})
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), PendingReviewRearm)


def test_huge_review_payloads_are_bounded():
    reviews = tuple(
        SubmittedReviewFact(f"review-{'x' * 480}-{index}", "a" * 500, SubmittedReviewState.COMMENTED, HEAD, later(index))
        for index in range(512)
    )
    huge = ObservedPullRequest(PR_KEY, HEAD, PullRequestState.OPEN, frozenset(), CompleteReviews(reviews), NOW)
    with pytest.raises(WorkflowEncodingError, match="too large"):
        encode_workflow(huge)
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(" " * (MAX_WORKFLOW_PAYLOAD_BYTES + 1), ReviewWorkflowSnapshot)


def test_counted_dismissal_authorization_survives_restart_exactly():
    state = pending()
    recovered = roundtrip(state, ReviewWorkflowSnapshot)
    assert recovered.intent is not None and isinstance(recovered.intent, PendingReviewRearm)
    assert recovered.intent.dismissals == counted()
    assert recovered.episode.continuation == FreshReviewRequired(1)
    assert operations() == recovered.intent.operations


def test_stopped_review_decision_gate_roundtrips():
    decision = OpenGate("gate:stopped", 1, RearmNeedsDecision("request:1", (CommandId("c"),), StopReason.DECISION_CHANGED),
                        NoAuthorizedRemediation("decide"))
    state = replace(cancelled(), gates=(decision,))
    roundtrip(state, ReviewWorkflowSnapshot)


def test_unknown_review_state_enum_is_rejected():
    raw = json.loads(encode_workflow(observed()))
    raw["value"]["fields"]["state"]["value"] = "draft"
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ObservedPullRequest)
    raw = json.loads(encode_workflow(preflight()))
    raw["value"]["fields"]["pr_key"]["fields"]["target"] = {"type": "IssueRef", "fields": {"number": 9}}
    with pytest.raises(WorkflowEncodingError):
        decode_workflow(json.dumps(raw), ReviewRearmPreflight)
    assert WorkflowKey("o/r", PullRequestRef(9)) == PR_KEY


@pytest.mark.parametrize("build", [pending_intent, lambda: cancelled().intent, lambda: superseded().intent],
                         ids=["pending", "cancelled", "superseded"])
def test_authorized_dismissal_set_is_typed_persisted_state(build):
    intent = build()
    recovered = roundtrip(intent, type(intent))
    assert recovered.authorized == counted()
    for path, replacement in (
        (("fields", "authorized"), KeyError),                                   # never a silent legacy default
        (("fields", "authorized"), None),
        (("fields", "authorized", 1, "fields", "review_id"), "review-0"),       # duplicate identity
        (("fields", "authorized", 0, "fields", "commit_id"), ""),               # unplaceable review
        (("fields", "authorized", 0, "type"), "SubmittedReviewFact"),
    ):
        with pytest.raises(WorkflowEncodingError):
            decode_workflow(mutate(intent, path, replacement), type(intent))


def test_pending_dismissal_outside_its_persisted_authorization_is_rejected():
    raw = mutate(pending_intent(), ("fields", "authorized"), [])
    with pytest.raises(WorkflowEncodingError, match="authorized counted reviews"):
        decode_workflow(raw, PendingReviewRearm)


def test_completed_review_rearm_carries_no_authorization():
    intent = completed().intent
    with pytest.raises(WorkflowEncodingError, match="wrong fields"):
        decode_workflow(mutate(intent, ("fields", "authorized"), []), type(intent))
