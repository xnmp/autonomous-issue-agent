"""WorldState and its value objects.

These are the pure, frozen representations of the GitHub ground truth the
daemons read each poll. An adapter (Phase 3) builds them from `gh` JSON; the
domain decision functions consume them and never touch GitHub directly.

Naming mirrors the GitHub JSON the bash reads (headRefName, headRefOid,
submittedAt, createdAt) so the adapter mapping is mechanical.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from .labels import Label


class CIState(str, Enum):
    """Settled state of a PR's checks. CRITICAL precedence (watcher-lib.sh
    ci_state): pending OUTRANKS failure — a run with one failed check but
    others still queued/running is NOT yet a failure. Acting on it would push
    a commit that re-triggers in-flight jobs (the #49 loop)."""

    OK = "ok"            # settled, all (non-ignored) checks passed
    PENDING = "pending"  # at least one non-ignored check queued/in-progress
    FAILURE = "failure"  # settled, at least one non-ignored check failed
    NONE = "none"        # no checks configured (nothing to wait on)


class ReviewDecision(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    ESCALATE = "ESCALATE"


# GitHub review states (as returned by the reviews API / pr view).
REVIEW_APPROVED = "APPROVED"
REVIEW_CHANGES_REQUESTED = "CHANGES_REQUESTED"
REVIEW_COMMENTED = "COMMENTED"


@dataclass(frozen=True)
class Check:
    """One CI check on a PR. `bucket` is gh's roll-up (pass/fail/pending/skipping);
    `state` is the raw status (QUEUED/IN_PROGRESS/SUCCESS/…)."""

    name: str
    state: str = ""
    bucket: str = ""


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
        return self.author.endswith("[bot]")


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
    # GitHub-native issue dependencies: True iff this issue has at least one OPEN
    # "blocked by" dependency (the adapter resolves the summary + list; the domain
    # only sees the settled boolean). The fixer must NOT pick up a blocked issue —
    # it would implement against a base branch missing its prerequisite. Default
    # False = no blockers (safe: an issue with no dependency graph is dispatchable).
    blocked_by_open: bool = False
    # File paths the issue body explicitly names (backticked `pkg/mod.py` etc.),
    # extracted by the adapter. Used to skip dispatching an issue whose files
    # overlap an in-flight PR's changed files (would guarantee a merge conflict
    # when that PR lands — the #59-vs-#60 collision). Empty = no paths declared →
    # unpredictable → the gate can't apply (fail-open, don't over-block).
    declared_paths: frozenset[str] = field(default_factory=frozenset)

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
    reviews: tuple[Review, ...] = ()
    checks: tuple[Check, ...] = ()
    state: str = "OPEN"            # OPEN | MERGED | CLOSED
    closing_issue: int | None = None  # GitHub closing-reference graph (default-branch only)
    # Files this PR changes (adapter reads `gh pr view --json files`). Feeds the
    # file-overlap gate: an ai-ready issue whose declared_paths intersect any OPEN
    # PR's changed_files is held back (dispatching it races a guaranteed conflict).
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
    # Draft PRs are work-in-progress → never auto-adopted for review. `author` is
    # the PR opener's login; used to skip the review bot's OWN PRs when adopting
    # rogue (unlabeled) PRs into the review pipeline.
    is_draft: bool = False
    author: str = ""

    def has(self, label: Label) -> bool:
        return label in self.labels

    def is_conflicting(self) -> bool:
        return self.mergeable.upper() == "CONFLICTING"

    def is_behind(self) -> bool:
        """Clean but stale: mergeable (no conflict) yet the base advanced. Needs an
        update-branch before an armed auto-merge can land it."""
        return self.merge_state.upper() == "BEHIND" and not self.is_conflicting()


import re

# A backticked token that looks like a source path: has a slash OR a code-ish
# extension, so prose words in backticks (`status`, `True`) don't count. Matches
# `backend/app/x.py`, `services/fakes.py`, `foo.ts`. Anchored to the backtick
# span so we only trust paths the author explicitly formatted as code.
_PATH_RE = re.compile(
    r"`([A-Za-z0-9_./-]+(?:\.(?:py|ts|tsx|js|jsx|md|sql|toml|yaml|yml)"
    r"|/[A-Za-z0-9_./-]+))`"
)


def extract_declared_paths(body: str) -> frozenset[str]:
    """File paths a GitHub issue body explicitly names in backticks. Pure. Used
    to predict which files an issue will touch BEFORE it runs (it has no diff
    yet), so the fixer can avoid dispatching an issue that overlaps an in-flight
    PR. Conservative: only backticked tokens that look like real paths (a slash
    or a source extension) — prose-in-backticks is ignored. A normalised set of
    forward-slash paths; no leading `./`."""
    out: set[str] = set()
    for m in _PATH_RE.finditer(body or ""):
        p = m.group(1).strip().lstrip("./")
        if p:
            out.add(p)
    return frozenset(out)


def paths_overlap_open_prs(
    declared: frozenset[str], prs: "tuple[PRView, ...]"
) -> bool:
    """True iff any declared path is changed by an OPEN PR. The overlap test is
    exact-path (both sides are repo-relative). Empty `declared` → False
    (unpredictable → fail-open, never block on no signal)."""
    if not declared:
        return False
    for pr in prs:
        if pr.state == "OPEN" and declared & pr.changed_files:
            return True
    return False


@dataclass(frozen=True)
class WorldState:
    """Everything a single daemon poll observes. A daemon reads only the slice
    it needs (the fixer reads issues+prs; the reviewer reads prs)."""

    issues: tuple[IssueView, ...] = ()
    prs: tuple[PRView, ...] = ()

    def open_issues(self) -> tuple[IssueView, ...]:
        return tuple(i for i in self.issues if i.state == "open")

    def issues_with(self, label: Label) -> tuple[IssueView, ...]:
        return tuple(i for i in self.issues if i.has(label))

    def prs_with(self, label: Label) -> tuple[PRView, ...]:
        return tuple(p for p in self.prs if p.has(label))


# Convenience re-export so callers can `from .world import replace`.
__all__ = [
    "CIState",
    "ReviewDecision",
    "REVIEW_APPROVED",
    "REVIEW_CHANGES_REQUESTED",
    "REVIEW_COMMENTED",
    "Check",
    "Review",
    "Comment",
    "IssueView",
    "PRView",
    "WorldState",
    "replace",
]
