"""Review-rejection lifecycle commands under the review daemon's executor (ADR 0015).

Workflow-owned DISMISS_REVIEW and PR projection RELABEL commands decode into
typed operations that carry their accepted guard; a bound SUBMIT_REVIEW
decodes into its verdict or escalation. They never reconstruct as a
legacy `Action`: that path would drop the guard and write unfenced. Each check
is one fresh read of the pull request (head, state, labels and, for a
dismissal, every review) classified by the pure review policy.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from ..domain.actions import Target
from ..domain.contracts import CommandKind, LifecycleCommand
from ..domain.eligibility import ORDERING_LABELS
from ..domain.identity import login_eq
from ..domain.labels import Label
from ..domain.review_rejection import is_commit_sha
from ..domain.review_workflow import (
    CompleteReviews, ObservedPullRequest, PartialReviews, PullRequestState, PullRequestUnavailable,
    SubmittedReviewState,
)
from ..domain.review_workflow_policy import (
    DismissalCheck, PullRequestHoldGuard, ReviewDismissalGuard, dismissal_check, pull_request_hold_guard_applies,
)
from ..domain.workflow_identity import WorkflowFamily, WorkflowKey, validate_workflow_identifier, validate_workflow_text

_MESSAGE_LIMIT = 4096
# GitHub's review body limit, less room for the command marker line.
_REVIEW_BODY_LIMIT = 65536 - 1024


class ReviewReadUnavailable(RuntimeError):
    """A pull request's reviews were not answered completely (refused, malformed or partial).

    Distinct from `GhUnavailable`: it fails one command, not the whole tick,
    and it is never an answer of "no such review".
    """


@runtime_checkable
class ReviewReads(Protocol):
    def get_pr(self, number: int, *, mergeability: bool = ..., checks: bool = ...) -> Any: ...

    def pull_request_reviews(self, pr: int) -> CompleteReviews | PartialReviews: ...


@runtime_checkable
class ReviewDismissals(Protocol):
    def dismiss_review(self, pr: int, review_id: str, message: str) -> bool: ...


@dataclass(frozen=True)
class ReviewDismissalOp:
    pr: int
    review_id: str
    message: str
    guard: ReviewDismissalGuard


@dataclass(frozen=True)
class PullRequestProjectionOp:
    pr: int
    remove: Label | None
    add: Label
    guard: PullRequestHoldGuard

    @property
    def removed(self) -> tuple[Label, ...]:
        return (self.remove,) if self.remove is not None else ()


ReviewFamilyOp = ReviewDismissalOp | PullRequestProjectionOp


class ReviewCheck(str, Enum):
    PRESENT = "present"          # the postcondition already holds: success, never a write
    APPLIES = "applies"          # absent, and the accepted guard still holds
    REFUSED = "refused"          # the guard changed: a verified refusal, never a write
    UNAVAILABLE = "unavailable"  # no complete read: neither presence nor absence


@dataclass(frozen=True)
class ReviewCommandCheck:
    check: ReviewCheck
    detail: str = ""
    # The remote object that proves PRESENT, when the read can name it.
    remote_object_id: str | None = None


_VERDICT_EVENTS = frozenset({"APPROVE", "REQUEST_CHANGES"})
_ESCALATION_CAUSES = frozenset({"verdict", "ceiling"})


@dataclass(frozen=True)
class BoundReviewSubmissionOp:
    """A generation-bound SUBMIT_REVIEW: a verdict on a patch, or an escalation.

    Verdicts (APPROVE / REQUEST_CHANGES) carry their reviewed patch identity
    and no cause; escalations ("verdict" or "ceiling") are COMMENT reviews.
    Any other pairing is unrepresentable. `author` is the review identity's
    login, the only author whose review can be this command's.
    """
    pr: int
    event: str
    body: str
    head: str
    author: str
    patch_id: str | None
    escalate_cause: str | None

    def __post_init__(self) -> None:
        if type(self.pr) is not int or self.pr <= 0:
            raise ValueError("a bound review submission targets a pull request number")
        if not isinstance(self.body, str) or len(self.body) > _REVIEW_BODY_LIMIT:
            raise ValueError("review body must be bounded text")
        if not is_commit_sha(self.head):
            raise ValueError("a bound review submission records its reviewed head")
        validate_workflow_identifier(self.author, "review identity")
        if not isinstance(self.event, str) or not (self.escalate_cause is None or isinstance(self.escalate_cause, str)):
            raise ValueError("review event and escalation cause must be text")
        if self.escalate_cause is None:
            if self.event not in _VERDICT_EVENTS:
                raise ValueError("a verdict review is APPROVE or REQUEST_CHANGES")
            if self.patch_id is None:
                raise ValueError("a verdict review records its reviewed patch identity")
        elif self.escalate_cause in _ESCALATION_CAUSES:
            if self.event != "COMMENT":
                raise ValueError("an escalation is submitted as a COMMENT review")
        else:
            raise ValueError("review escalation cause is not recognized")
        if self.patch_id is not None and (
            not isinstance(self.patch_id, str) or len(self.patch_id) != 64
            or any(c not in "0123456789abcdef" for c in self.patch_id)
        ):
            raise ValueError("reviewed patch identity must be a SHA-256 digest")

    def payload(self) -> dict[str, object]:
        return {"pr": self.pr, "event": self.event, "body": self.body, "head": self.head, "author": self.author,
                "patch_id": self.patch_id, "escalate_cause": self.escalate_cause}


def bound_submission_op(command: LifecycleCommand) -> BoundReviewSubmissionOp | None:
    """The decoded bound submission of a review-family SUBMIT_REVIEW, else None.

    A malformed payload raises, so it is refused at enqueue and never begins.
    """
    if not isinstance(command, LifecycleCommand):
        raise ValueError("review command decoding requires a lifecycle command")
    if command.kind is not CommandKind.SUBMIT_REVIEW or not is_review_family(command):
        return None
    binding = command.workflow
    assert binding is not None
    raw = _mapping(command.payload, {"pr", "event", "body", "head", "author", "patch_id", "escalate_cause"},
                   "bound review submission payload")
    op = BoundReviewSubmissionOp(
        raw["pr"], _text(raw["event"], "review event"), _text(raw["body"], "review body"),  # type: ignore[arg-type]
        _text(raw["head"], "reviewed head"), _text(raw["author"], "review identity"),
        raw["patch_id"], raw["escalate_cause"],  # type: ignore[arg-type]
    )
    if op.pr != binding.fence.key.target.number or command.target != f"pr:{op.pr}":
        raise ValueError("bound review submission disagrees with its pull request workflow")
    if command.expected_revision != op.head:
        raise ValueError("bound review submission revision disagrees with its reviewed head")
    return op


def check_bound_submission(
    op: BoundReviewSubmissionOp, reviews: CompleteReviews | PartialReviews, *, command_id: str,
) -> ReviewCommandCheck:
    """Whether this command's own review exists, from one read of every review (pure).

    The executor submits every bound review with a final-line marker naming
    its command (`mark_review_body`), the standard idempotency key: neither
    content, state nor time can tell this command's review from an earlier or
    later one, and host clocks drift. PRESENT requires exactly one review by
    the review identity carrying this command's marker, in any state (a
    dismissed review is still the one submitted); its id is the receipt's
    remote object. A marker typed by anyone else never counts. Several
    matches are ambiguous and a partial read proves nothing: both are
    UNAVAILABLE. None on a complete read is proven absence (APPLIES).
    """
    if not isinstance(reviews, CompleteReviews):
        return ReviewCommandCheck(ReviewCheck.UNAVAILABLE, f"partial review read: {reviews.reason}")
    matches = [review for review in reviews.reviews
               if review.review_command == command_id and login_eq(review.author, op.author)]
    if len(matches) == 1:
        return ReviewCommandCheck(ReviewCheck.PRESENT, remote_object_id=matches[0].review_id)
    if matches:
        return ReviewCommandCheck(ReviewCheck.UNAVAILABLE, (
            f"ambiguous review read: {len(matches)} reviews by {op.author} carry command {command_id}'s marker"
        ))
    return ReviewCommandCheck(ReviewCheck.APPLIES)


def is_review_family(command: LifecycleCommand) -> bool:
    return command.workflow is not None and command.workflow.authority.family is WorkflowFamily.REVIEW_REJECTIONS


def _mapping(raw: object, keys: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != keys:
        raise ValueError(f"malformed {name}")
    return raw


def _text(raw: object, name: str) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"malformed {name}")
    return raw


def review_family_op(command: LifecycleCommand) -> ReviewFamilyOp | None:
    """The guarded operation of a review-family command, or None outside that family.

    SUBMIT_REVIEW keeps its own path. A malformed or unguarded review command
    raises: it stays queued and visible, never executes unfenced.
    """
    if not isinstance(command, LifecycleCommand):
        raise ValueError("review command decoding requires a lifecycle command")
    payload = command.payload
    if not is_review_family(command):
        if command.kind is CommandKind.RELABEL and "pull_request_hold_guard" in payload:
            raise ValueError("a pull request hold guard requires review-family workflow authority")
        return None
    binding = command.workflow
    assert binding is not None
    number = binding.fence.key.target.number
    if command.target != f"pr:{number}":
        raise ValueError("review command target disagrees with its pull request workflow")
    if command.kind is CommandKind.SUBMIT_REVIEW:
        return None
    if command.kind is CommandKind.DISMISS_REVIEW:
        raw = _mapping(payload, {"pr", "review_id", "message", "guard"}, "dismissal payload")
        guard = _mapping(raw["guard"], {"review_id", "author", "head_sha", "state"}, "dismissal guard")
        review_id, message = _text(raw["review_id"], "dismissed review"), _text(raw["message"], "dismissal message")
        validate_workflow_text(message, "dismissal message", limit=_MESSAGE_LIMIT)
        if raw["pr"] != number or guard["review_id"] != review_id or guard["state"] != SubmittedReviewState.CHANGES_REQUESTED.value:
            raise ValueError("dismissal payload disagrees with its guard or pull request")
        decoded = ReviewDismissalGuard(review_id, _text(guard["author"], "review bot"), _text(guard["head_sha"], "head"))
        # The command's revision is the authorized head (ADR 0015 dismissal fence).
        if command.expected_revision != decoded.head_sha:
            raise ValueError("dismissal revision disagrees with its authorized head")
        return ReviewDismissalOp(number, review_id, message, decoded)
    if command.kind is CommandKind.RELABEL:
        raw = _mapping(payload, {"target", "number", "remove", "add", "expected_revision", "harness_signal", "removes",
                                 "completion_guard", "pull_request_hold_guard"}, "pull request projection payload")
        if (raw["target"] != Target.PR.value or raw["number"] != number or raw["harness_signal"] is not False
                or raw["removes"] != [] or raw["completion_guard"] is not None):
            raise ValueError("pull request projection carries effects outside its guarded relabel")
        guard_raw = _mapping(raw["pull_request_hold_guard"], {"head_sha", "labels"}, "pull request hold guard")
        labels = guard_raw["labels"]
        if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
            raise ValueError("malformed pull request hold guard labels")
        guard = PullRequestHoldGuard(_text(guard_raw["head_sha"], "head"), frozenset(Label(label) for label in labels))
        if command.expected_revision != guard.head_sha or raw["expected_revision"] != guard.head_sha:
            raise ValueError("pull request projection revision disagrees with its hold guard")
        remove = raw["remove"]
        return PullRequestProjectionOp(
            number, Label(_text(remove, "removed label")) if remove is not None else None,
            Label(_text(raw["add"], "added label")), guard,
        )
    raise ValueError(f"review-rejection workflow has no guarded executor for {command.kind.value}")


_PR_STATES = {state.value.upper(): state for state in PullRequestState}


def observe_pull_request(
    gateway: object, key: WorkflowKey, observed_at: datetime, *, reviews: bool,
) -> ObservedPullRequest | PullRequestUnavailable:
    """One fresh read; any failure is unavailable, never absence (a bare 404 proves nothing)."""
    number = key.target.number
    try:
        if not isinstance(gateway, ReviewReads):
            return PullRequestUnavailable(key, observed_at, "the executor gateway cannot read pull request reviews")
        view = gateway.get_pr(number, mergeability=False, checks=False)
        head, state = getattr(view, "head_ref_oid", ""), _PR_STATES.get(str(getattr(view, "state", "")).upper())
        if not is_commit_sha(head) or state is None:
            return PullRequestUnavailable(key, observed_at, "pull request head or state is unavailable")
        read = gateway.pull_request_reviews(number) if reviews else PartialReviews(
            "reviews are not read for a label projection")
        return ObservedPullRequest(key, head, state, frozenset(getattr(view, "labels", frozenset())), read, observed_at)
    except sqlite3.Error:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed read is unavailable evidence
        return PullRequestUnavailable(key, observed_at, f"{type(exc).__name__}: {exc}"[:512])


def _refusal_reason(guard: ReviewDismissalGuard, pr: ObservedPullRequest, reviews: CompleteReviews) -> str:
    """Why `dismissal_check` refused; it decides, this only explains."""
    review = next((r for r in reviews.reviews if r.review_id == guard.review_id), None)
    if pr.state is not PullRequestState.OPEN:
        return f"pull request is {pr.state.value}"
    if pr.head_sha != guard.head_sha:
        return "pull request head moved since the dismissal was authorized"
    if review is None:
        return "review no longer exists"
    if not login_eq(review.author, guard.review_bot):
        return "review author is no longer the review bot"
    return f"review state is {review.state.value}, not CHANGES_REQUESTED"


def check_review_op(op: ReviewFamilyOp, observation: ObservedPullRequest | PullRequestUnavailable) -> ReviewCommandCheck:
    """Classify one operation against one fresh observation (pure)."""
    if isinstance(observation, PullRequestUnavailable):
        return ReviewCommandCheck(ReviewCheck.UNAVAILABLE, observation.reason)
    if isinstance(op, ReviewDismissalOp):
        reviews = observation.reviews
        if not isinstance(reviews, CompleteReviews):
            return ReviewCommandCheck(ReviewCheck.UNAVAILABLE, f"partial review read: {reviews.reason}")
        decided = dismissal_check(op.guard, observation)
        # Presence outranks a moved head: a dismissal that landed after a push
        # (between the check and the PUT) dismissed exactly the authorized
        # review, and the push supersedes the re-arm via DECISION_CHANGED.
        if decided is DismissalCheck.POSTCONDITION_PRESENT:
            return ReviewCommandCheck(ReviewCheck.PRESENT)
        if decided is DismissalCheck.APPLIES:
            return ReviewCommandCheck(ReviewCheck.APPLIES)
        return ReviewCommandCheck(ReviewCheck.REFUSED, _refusal_reason(op.guard, observation, reviews))
    labels = observation.labels
    if op.add in labels and (op.remove is None or op.remove not in labels):
        return ReviewCommandCheck(ReviewCheck.PRESENT)
    if pull_request_hold_guard_applies(op.guard, observation, removed=op.removed, added=(op.add,)):
        return ReviewCommandCheck(ReviewCheck.APPLIES)
    changed = sorted(label.value for label in (frozenset(labels) - ORDERING_LABELS) ^ op.guard.labels)
    return ReviewCommandCheck(ReviewCheck.REFUSED, (
        f"pull request is {observation.state.value}" if observation.state is not PullRequestState.OPEN
        else "pull request head moved since the projection was accepted" if observation.head_sha != op.guard.head_sha
        else f"pull request hold labels changed since the projection was accepted: {', '.join(changed)}"
    ))
