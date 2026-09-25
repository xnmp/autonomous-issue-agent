"""Review-rejection ceiling facts shared by the resolve-owned gate union; ADR 0015.

These values sit below `workflow.py` because `OpenGate` accepts the review
ceiling as a cause while `review_workflow.py` builds on the gate types. Keeping
them import-free of `workflow.py` breaks that cycle; `review_workflow` re-exports
them as the review family's public surface.

Two bounds are deliberately distinct. An *observed* counted set (a ceiling
cause, a fresh preflight) is bounded only by what one complete review read can
carry. An *authorized* set, and the dismissal fan-out it permits, must fit one
re-arm's durable obligations. Policy refuses, with a typed visible reason, a
request whose observed set exceeds the authorization bound.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .labels import Label
from .workflow_identity import (
    MAX_DETAIL_CHARS, MAX_REARM_COMMANDS, MAX_WORKFLOW_GATES, PullRequestRef, WorkflowKey,
    validate_workflow_identifier, validate_workflow_integer, validate_workflow_text,
)

# One dismissal command per authorized review plus the projection command must
# fit a single re-arm's durable obligations.
MAX_COUNTED_REVIEWS = MAX_REARM_COMMANDS - 1
# A pull request with more reviews than one bounded observation can carry is
# never "complete": the adapter must report it as partial and fail closed.
MAX_OBSERVED_REVIEWS = 512
MAX_NON_RESETTING_COMMITS = 64
_HEX = frozenset("0123456789abcdef")


def is_commit_sha(value: object) -> bool:
    """A full lowercase SHA-1 or SHA-256 object name; abbreviations are ambiguous."""
    return isinstance(value, str) and len(value) in (40, 64) and all(character in _HEX for character in value)


def validate_commit_sha(value: object, name: str) -> None:
    if not is_commit_sha(value):
        raise ValueError(f"{name} must be a full lowercase commit SHA")


def validate_counted(value: object, name: str, *, limit: int = MAX_COUNTED_REVIEWS) -> None:
    """A unique tuple of counted reviews; the default bound is the authorization bound."""
    if not isinstance(value, tuple) or len(value) > limit or any(
        not isinstance(item, CountedReview) for item in value
    ):
        raise ValueError(f"{name} must be a bounded tuple of counted reviews")
    if len({item.review_id for item in value}) != len(value):
        raise ValueError(f"{name} contains duplicate review identities")


@dataclass(frozen=True)
class CountedReview:
    """A review-bot CHANGES_REQUESTED review counted toward the ceiling.

    `commit_id` None means GitHub did not resolve the review's commit. Counting
    and dismissal never need it; the empty string is not a representation.
    """
    review_id: str
    commit_id: str | None

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.review_id, "counted review")
        if self.commit_id is not None:
            validate_commit_sha(self.commit_id, "counted review commit")

    @classmethod
    def from_github(cls, review_id: str, commit_id: str) -> "CountedReview":
        """Boundary mapping: the adapter's "" (unresolved commit) becomes None."""
        return cls(review_id, None if commit_id == "" else commit_id)


def _non_resetting(value: object) -> None:
    if not isinstance(value, tuple) or len(value) > MAX_NON_RESETTING_COMMITS:
        raise ValueError("non-resetting commits must be a bounded tuple")
    for commit in value:
        validate_commit_sha(commit, "non-resetting commit")
    if len(set(value)) != len(value):
        raise ValueError("non-resetting commits must be unique")


@dataclass(frozen=True)
class CountingRule:
    """Which reviews count toward the ceiling: the trusted bot identity and the
    approvals proven unmergeable (no reset). Captured when a gate opens and
    carried by the re-arm intents, so continuation, completion and ceiling
    reconciliation recount under the rule that decided the gate, never under
    whatever configuration is current. Commits are canonical (sorted, unique).
    """
    review_bot: str
    non_resetting_commits: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.review_bot, "review bot")
        _non_resetting(self.non_resetting_commits)
        if list(self.non_resetting_commits) != sorted(self.non_resetting_commits):
            raise ValueError("counting rule commits must be canonically sorted")

    @classmethod
    def of(cls, review_bot: str, non_resetting_commits: object = ()) -> "CountingRule":
        """Canonicalize a configured tuple or frozenset of commits."""
        if not isinstance(non_resetting_commits, (tuple, frozenset)) or len(non_resetting_commits) > MAX_NON_RESETTING_COMMITS:
            raise ValueError("non-resetting commits must be a bounded tuple or frozenset")
        return cls(review_bot, tuple(sorted(set(non_resetting_commits))))


@dataclass(frozen=True)
class ReviewRejectionCeiling:
    """The observed counted set when the gate opened, bounded by one complete read."""
    episode_id: str
    head_sha: str
    review_bot: str
    counted: tuple[CountedReview, ...]
    ceiling: int
    non_resetting_commits: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_workflow_identifier(self.episode_id, "episode")
        validate_commit_sha(self.head_sha, "rejection head")
        validate_workflow_identifier(self.review_bot, "review bot")
        validate_workflow_integer(self.ceiling, "rejection ceiling", minimum=1)
        validate_counted(self.counted, "counted reviews", limit=MAX_OBSERVED_REVIEWS)
        # The escalation is posted after ceiling-1 counted rejections.
        if len(self.counted) < self.ceiling - 1:
            raise ValueError("a review rejection ceiling cause requires exhaustion")
        _non_resetting(self.non_resetting_commits)

    @property
    def rule(self) -> CountingRule:
        """The counting rule this cause was decided under."""
        return CountingRule.of(self.review_bot, self.non_resetting_commits)


class PullRequestState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


def validate_labels(value: object, name: str) -> None:
    if not isinstance(value, frozenset) or any(not isinstance(label, Label) for label in value):
        raise ValueError(f"{name} must be validated label values")


@dataclass(frozen=True)
class ReviewRearmPreflight:
    """Harness-minted under the deployment barriers from one COMPLETE review read.

    `complete_reviews_evidence_id` names that durable observation; a partial
    read has no constructor that yields this value. `counted` is the fresh
    observed counted set, so it is bounded as an observation. Policy validation
    re-derives this value from the observation and requires equality; the
    committing adapter (increment 5) must additionally bind
    `complete_reviews_evidence_id` to a durable read taken at `observed_at`
    and fresh under the acceptance barrier, since policy cannot see storage.
    """
    pr_key: WorkflowKey
    head_sha: str
    complete_reviews_evidence_id: str
    counted: tuple[CountedReview, ...]
    state: PullRequestState
    labels: frozenset[Label]
    observed_at: datetime
    external_holds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.pr_key, WorkflowKey) or not isinstance(self.pr_key.target, PullRequestRef):
            raise ValueError("review rearm preflight requires a pull request identity")
        validate_commit_sha(self.head_sha, "preflight head")
        validate_workflow_identifier(self.complete_reviews_evidence_id, "complete review evidence")
        validate_counted(self.counted, "preflight counted reviews", limit=MAX_OBSERVED_REVIEWS)
        if not isinstance(self.state, PullRequestState):
            raise ValueError("preflight pull request state must be typed")
        validate_labels(self.labels, "preflight labels")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("preflight observation time must be timezone-aware")
        if not isinstance(self.external_holds, tuple) or len(self.external_holds) > MAX_WORKFLOW_GATES:
            raise ValueError("external holds must be a bounded tuple")
        for hold in self.external_holds:
            validate_workflow_text(hold, "external hold", limit=MAX_DETAIL_CHARS)
