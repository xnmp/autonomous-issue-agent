"""WorldState and its value objects.

These are the pure, frozen representations of the GitHub ground truth the
daemons read each poll. An adapter builds them from `gh` JSON; the
domain decision functions consume them and never touch GitHub directly.

Naming mirrors the GitHub JSON the bash reads (headRefName, headRefOid,
submittedAt, createdAt) so the adapter mapping is mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from .identity import is_bot_login
from .labels import Label
from .checks import Check, CIObservation, Observed, Unavailable


class CIState(str, Enum):
    """Settled state of a PR's checks. CRITICAL precedence (watcher-lib.sh
    ci_state): pending OUTRANKS failure — a run with one failed check but
    others still queued/running is NOT yet a failure. Acting on it would push
    a commit that re-triggers in-flight jobs (the #49 loop)."""

    OK = "ok"            # settled, all gating checks passed
    PENDING = "pending"  # at least one gating check queued/in-progress
    FAILURE = "failure"  # settled, at least one gating check failed
    NONE = "none"        # no checks configured (nothing to wait on)
    UNKNOWN = "unknown"  # the checks read FAILED this tick — not an answer (#48)


class ReviewDecision(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    ESCALATE = "ESCALATE"


# GitHub review states (as returned by the reviews API / pr view).
REVIEW_APPROVED = "APPROVED"
REVIEW_CHANGES_REQUESTED = "CHANGES_REQUESTED"


@dataclass(frozen=True)
class Review:
    """A submitted review. `author` is the login; `submitted_at` orders them.

    `commit_id` is the head SHA the review was submitted against (gh: the
    review's `commit.oid`, REST: `commit_id`). It drives the ADDRESS_REVIEW
    supersession gate: a CHANGES_REQUESTED review whose commit is no longer the
    PR head has already been answered by a push — re-dispatching the fixer on it
    burns a full session per poll until the reviewer re-reviews (V2/#64).
    Empty = unknown (adapter couldn't resolve it) → the gate stays open and the
    attempt-counting breaker is the backstop."""

    state: str
    author: str
    submitted_at: str = ""
    body: str = ""
    commit_id: str = ""


@dataclass(frozen=True)
class Comment:
    author: str
    body: str = ""
    created_at: str = ""

    @property
    def is_bot(self) -> bool:
        # Routed through identity.is_bot_login (review R2-11) rather than a
        # raw `.endswith("[bot]")`: the one other login-shaped comparison in
        # the codebase, now case/whitespace-tolerant like everything else
        # identity.py owns.
        return is_bot_login(self.author)


@dataclass(frozen=True)
class Push:
    """A push to a PR's head branch: who pushed it and when. A push by anyone
    but the harness is a reset event for attempt counting (refactor decision
    Q3); the harness's own pushes are not, which keeps FIX_CI bounded. The
    observe adapter sources it from the head commit's author login and date in
    the same GraphQL query (Q24)."""

    actor: str
    pushed_at: str = ""


@dataclass(frozen=True)
class Branch:
    """A pushed harness branch (`fix/<issue>-…`) that has never had a pull
    request. The agent pushes and returns; the harness opens the PR (Q11), so
    a branch observed here is publication still owed, e.g. after a crash
    between the push and the PR-open."""

    name: str
    committed_at: str = ""


@dataclass(frozen=True)
class IssueView:
    number: int
    title: str = ""
    body: str = ""
    labels: frozenset[Label] = field(default_factory=frozenset)
    comments: tuple[Comment, ...] = ()
    updated_at: str = ""
    author: str = ""
    state: str = "open"  # open | closed
    # When `needs-human` was last applied ("" = not observed). A reply older
    # than that never releases: the operator parked the item after it.
    needs_human_labeled_at: str = ""

    def has(self, label: Label) -> bool:
        return label in self.labels


@dataclass(frozen=True)
class PRView:
    number: int
    head_ref_name: str = ""        # the REAL branch (feat/12-…), not the worktree name
    head_ref_oid: str = ""         # head SHA — drives review/re-review
    base_ref_name: str = ""
    title: str = ""
    body: str = ""
    labels: frozenset[Label] = field(default_factory=frozenset)
    # PR-stage repair legs ask for clarification on the PR itself. Keeping that
    # thread in the world lets the fixer consume a newer operator reply without
    # requiring an incoherent duplicate issue label.
    comments: tuple[Comment, ...] = ()
    reviews: tuple[Review, ...] = ()
    ci: CIObservation = Unavailable("checks not observed")
    state: str = "OPEN"            # OPEN | MERGED | CLOSED
    # Files this PR changes; derive stops a PR touching `.github/` for a human.
    changed_files: frozenset[str] = field(default_factory=frozenset)
    # GitHub mergeability (mergeable: MERGEABLE|CONFLICTING|UNKNOWN). CONFLICTING
    # means the branch won't merge until rebased — the auto-rebase backup plan
    # keys off this. Empty/UNKNOWN → treat as not-conflicting (don't act on a
    # transient UNKNOWN GitHub is still computing).
    mergeable: str = ""
    # GitHub mergeStateStatus (CLEAN|BEHIND|BLOCKED|DIRTY|UNKNOWN|…). BEHIND means
    # the branch is a clean fast-forwardable descendant of an OLDER base tip: it
    # WILL merge, but GitHub's armed auto-merge won't fire until the branch is
    # brought up to date (repos that require up-to-date branches). Distinct from
    # `mergeable=CONFLICTING` (DIRTY) — BEHIND has no conflict, just needs a base
    # update, so it takes the cheap update-branch path, not a full agent rebase.
    merge_state: str = ""
    author: str = ""
    # Appended to preserve the positional compatibility of the historical
    # PRView fields while binding review evidence to an immutable base.
    base_ref_oid: str = ""
    # False when the connection-shaped fields (body, comments, reviews,
    # changed_files) were NOT fetched — the terminal-PR fast path builds a view
    # from the `gh pr list` payload, which carries none of them (#163). Same
    # absence contract as the CI observation: absent ≠ empty.
    content_known: bool = True
    # Head-branch pushes, any order. Empty = none observed.
    pushes: tuple[Push, ...] = ()
    needs_human_labeled_at: str = ""  # as on IssueView

    @property
    def checks(self) -> tuple[Check, ...]:
        return self.ci.reports if isinstance(self.ci, Observed) else ()

    def has(self, label: Label) -> bool:
        return label in self.labels

    def is_conflicting(self) -> bool:
        return self.mergeable.upper() == "CONFLICTING"

    def is_behind(self) -> bool:
        """Clean but stale: mergeable (no conflict) yet the base advanced. Needs an
        update-branch before an armed auto-merge can land it."""
        return self.merge_state.upper() == "BEHIND" and not self.is_conflicting()


@dataclass(frozen=True)
class PullRequestDiff:
    """Immutable review evidence for one exact base/head pair."""

    base_oid: str
    head_oid: str
    text: str


@dataclass(frozen=True)
class WorldState:
    """Everything a single daemon poll observes. A daemon reads only the slice
    it needs (the fixer reads issues+prs; the reviewer reads prs)."""

    issues: tuple[IssueView, ...] = ()
    prs: tuple[PRView, ...] = ()
    # When this snapshot was BUILT (ISO-8601 UTC), stamped by the adapter. The
    # domain is pure and may not read a clock, so any decision with a time
    # component (the CI pending deadline, #30) reads it from here. Empty =
    # unstamped (a hand-built world, a builder that predates this) → every
    # time-based gate treats the elapsed time as unknown and does not fire.
    now: str = ""
    # Harness branches with no PR in any state. Empty = none observed.
    branches: tuple[Branch, ...] = ()


# Convenience re-export so callers can `from .world import replace`.
__all__ = [
    "CIState",
    "ReviewDecision",
    "REVIEW_APPROVED",
    "REVIEW_CHANGES_REQUESTED",
    "Check",
    "Review",
    "Comment",
    "Push",
    "Branch",
    "IssueView",
    "PRView",
    "WorldState",
    "replace",
]
