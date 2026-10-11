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
from .lifecycle import pr_lifecycle
from .refs import issue_from_branch
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
REVIEW_COMMENTED = "COMMENTED"


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
class IssueView:
    number: int
    title: str = ""
    body: str = ""
    labels: frozenset[Label] = field(default_factory=frozenset)
    comments: tuple[Comment, ...] = ()
    updated_at: str = ""
    author: str = ""
    state: str = "open"  # open | closed
    # GitHub's `stateReason`, lower-cased: "" | "completed" | "not_planned" |
    # "reopened". Carried on the world (it costs nothing — the same `gh issue
    # view` already runs) because it is the ONLY deterministic evidence that an
    # issue's lifecycle already ended once: an OPEN issue reading "reopened" was
    # closed and put back by a human, which is the #10 recurrence signal and must
    # never be re-closed by the harness. "" = never closed.
    state_reason: str = ""
    # GitHub-native issue dependencies: True iff this issue has at least one OPEN
    # "blocked by" dependency (the adapter resolves the summary + list; the domain
    # only sees the settled boolean). The fixer must NOT pick up a blocked issue —
    # it would implement against a base branch missing its prerequisite. Default
    # False = no blockers (safe: an issue with no dependency graph is dispatchable).
    blocked_by_open: bool = False
    # The open dependency numbers behind ``blocked_by_open``. Keeping the
    # witnesses lets operator surfaces name what will unblock the issue instead
    # of exposing the gate's internal "held" terminology.
    open_blocker_numbers: tuple[int, ...] = ()
    # File paths the issue body explicitly names (backticked `pkg/mod.py` etc.),
    # extracted by the adapter. Used to skip dispatching an issue whose files
    # overlap an in-flight PR's changed files (would guarantee a merge conflict
    # when that PR lands — the #59-vs-#60 collision). Empty = no paths declared →
    # unpredictable → the gate can't apply (fail-open, don't over-block).
    declared_paths: frozenset[str] = field(default_factory=frozenset)
    # Repository-relative paths predicted by the cheap router. These are
    # advisory like declared_paths: malformed or absent predictions remain empty
    # and therefore cannot hold an issue.
    predicted_paths: frozenset[str] = field(default_factory=frozenset)

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
    # Appended to preserve the positional compatibility of the historical
    # PRView fields while binding review evidence to an immutable base.
    base_ref_oid: str = ""
    # False when the connection-shaped fields (body, comments, reviews,
    # changed_files) were NOT fetched — the terminal-PR fast path builds a view
    # from the `gh pr list` payload, which carries none of them (#163). Same
    # absence contract as the CI observation: absent ≠ empty. No lifecycle decision reads
    # them off a non-OPEN PR, but telemetry does, and a metric that cannot tell
    # "no files" from "files unread" reports a false zero (adversarial review
    # F4: the predicted-path hit rate is a primary metric).
    content_known: bool = True

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


def issue_for_pr(pr: PRView) -> int | None:
    """THE PR→issue linkage rule: prefer GitHub's closing-reference, fall back
    to the branch number. `closing_issue` is default-branch-only (failure
    modes #34/#35) — a consumer keying on it alone silently no-ops on any repo
    whose working base is not the default branch (the self-hosting repo:
    default main, base dev). Every linkage consumer must go through here."""
    if pr.closing_issue is not None:
        return pr.closing_issue
    return issue_from_branch(pr.head_ref_name)


@dataclass(frozen=True)
class PullRequestDiff:
    """Immutable review evidence for one exact base/head pair."""

    base_oid: str
    head_oid: str
    text: str


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


def normalize_predicted_paths(raw: object) -> frozenset[str]:
    """Return safe, exact repo-relative router predictions, else no signal.

    This input is structured model output, not GitHub ground truth. One malformed
    value must never turn the overlap serializer into a broad deny gate, so an
    invalid list fails open as a whole rather than preserving a partial guess.
    """
    if not isinstance(raw, (list, tuple, frozenset)) or len(raw) > 32:
        return frozenset()
    paths: set[str] = set()
    for path in raw:
        if not isinstance(path, str) or not path or path != path.strip():
            return frozenset()
        normalized = path.removeprefix("./")
        parts = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or "\\" in normalized
            or any(part in ("", ".", "..") for part in parts)
        ):
            return frozenset()
        paths.add(normalized)
    return frozenset(paths)


def paths_overlap_open_prs(
    declared: frozenset[str], prs: "tuple[PRView, ...]"
) -> bool:
    """True iff any declared path is changed by an OPEN, UNBLOCKED PR. The
    overlap test is exact-path (both sides are repo-relative). Empty
    `declared` → False (unpredictable → fail-open, never block on no signal).

    A BLOCKED PR (ai-blocked / needs-human-review) does not hold the path
    lock: it is not moving until a human acts, and that wait is unbounded —
    keifu #163/#164 sat deferred behind escalated PR #181 for the length of
    a human decision (2026-08-03), converting one PR's human latency into a
    pipeline-wide stall. The risk this accepts is bounded and recoverable:
    if the human later merges the blocked PR by hand, an overlapping
    in-flight branch may conflict, and the REBASE leg already owns that."""
    if not declared:
        return False
    for pr in prs:
        if (
            pr.state == "OPEN"
            and not pr_lifecycle(pr).is_blocked
            and declared & pr.changed_files
        ):
            return True
    return False


@dataclass(frozen=True)
class ClosingRef:
    """An OPEN pull request GitHub's closing-reference graph says closes `issue`.

    Deliberately SEPARATE from `WorldState.prs`, which the fix world populates
    only from `ai-pr-open` — i.e. the harness's own PRs. The duplicate-resolve
    guard (#39) needs the opposite population: PRs the harness has never touched,
    typically a human's, which is exactly what `prs` cannot carry without
    changing what every other fixer pass (path-overlap holds, reap, cleanup,
    auto-merge) considers "a PR of ours". One cheap repo-wide `gh pr list` fills
    this instead, and nothing but the guard reads it.

    `author` is the opener's login, "" when GitHub didn't resolve it (a deleted
    account) — the guard treats an unknown author as NOT the fix bot, the same
    fail-safe direction `fixer_prs` uses.

    `base_ref_name` is recorded but deliberately NOT gated on: GitHub only
    AUTO-closes an issue when the PR merges into the default branch, so a PR
    based elsewhere leaves the issue open — but the fix in it still exists, so
    resolving the issue again would still buy a duplicate. The money is wasted
    either way; the base is carried only so the comment can be honest about it
    if that ever matters."""

    pr: int
    issue: int
    author: str = ""
    base_ref_name: str = ""


@dataclass(frozen=True)
class WorldState:
    """Everything a single daemon poll observes. A daemon reads only the slice
    it needs (the fixer reads issues+prs; the reviewer reads prs)."""

    issues: tuple[IssueView, ...] = ()
    prs: tuple[PRView, ...] = ()
    # Open PRs that close one of this repo's issues, harness-authored or not
    # (#39). Empty = NOT COLLECTED (a hand-built world, a builder that predates
    # this, a repo with no open PRs) — indistinguishable on purpose, because the
    # only consumer is a guard that must fail OPEN: no evidence of a duplicate
    # is not evidence of no duplicate, but refusing to dispatch on missing data
    # would strand every issue on any world builder that doesn't fill this.
    closing_refs: tuple[ClosingRef, ...] = ()
    # When this snapshot was BUILT (ISO-8601 UTC), stamped by the adapter. The
    # domain is pure and may not read a clock, so any decision with a time
    # component (the CI pending deadline, #30) reads it from here. Empty =
    # unstamped (a hand-built world, a builder that predates this) → every
    # time-based gate treats the elapsed time as unknown and does not fire.
    # Safe to read only because a world is decided on the tick it was built:
    # WorldProbe raises WorldUnchanged instead of re-serving a cached world.
    now: str = ""
    def open_issues(self) -> tuple[IssueView, ...]:
        return tuple(i for i in self.issues if i.state == "open")

    def issues_with(self, label: Label) -> tuple[IssueView, ...]:
        return tuple(i for i in self.issues if i.has(label))

    def prs_with(self, label: Label) -> tuple[PRView, ...]:
        return tuple(p for p in self.prs if p.has(label))


@dataclass(frozen=True)
class HeadRef:
    """One in-flight PR whose head the change signature cannot speak for. Carries
    the PR number only: the head SHA a reading must be taken against is whatever
    GitHub serves at read time, and a head that moved since the world was built
    shows up as a CHANGED reading, which is exactly the answer wanted."""

    number: int


@dataclass(frozen=True)
class HeadSignal:
    """What the change signature is blind to, for ONE in-flight PR head, read
    outside the world build (cheap REST, a different pool from the GraphQL world).

    Compared tick-over-tick against the reading taken for the last full build:
    the fields only have to be internally consistent between two readings of the
    same source, never equal to the GraphQL world's own encoding of the same
    facts. `failing` is separate from `ci` because a failing check is exactly the
    state whose meaning depends on enrichment the signature does not carry (the
    repository-required policy read), so it forces a full build."""

    number: int
    head_ref_oid: str = ""
    state: str = ""
    ci: str = ""
    mergeable: str = ""
    failing: bool = False


@dataclass(frozen=True)
class ProbeSignals:
    """The blind-spot readings a probe-gated tick has in hand: `current` taken
    this tick, `previous` taken for the last full build. `None` on either side
    means NOT READ (never taken, or the read failed) — never "nothing to see"."""

    current: tuple[HeadSignal, ...] | None = None
    previous: tuple[HeadSignal, ...] | None = None


# Above this many in-flight heads the reading stops being cheap: it costs three
# REST calls each, EVERY tick, on the same pool the probe's own signature uses,
# and exhausting that pool blinds the daemon outright (adversarial review F2).
# Ten open harness PRs at a 20s poll would be ~5,000 REST calls/hour — the
# primary limit. Past the ceiling the tick simply builds, which is the
# pre-#163 behaviour.
PROBE_HEAD_CEILING = 4


def _inflight_prs(world: WorldState) -> tuple[PRView, ...]:
    return tuple(
        p for p in world.prs if p.state == "OPEN" and p.has(Label.AI_PR_OPEN)
    )


def probe_signal_heads(world: WorldState) -> tuple[HeadRef, ...]:
    """The heads a probing tick should re-read for `fix_probe_safe` to be
    answerable. NOT a statement about safety — `()` means "reading these cannot
    gate this tick", which for a world with in-flight PRs means a full build.

    Empty when the reading could not pay for itself:

    * an idle world (nothing in flight) — the signature already answers;
    * an OPEN `ai-working` issue — unsafe for reasons no GitHub read covers;
    * more heads than `PROBE_HEAD_CEILING` — see the constant;
    * a head the last build already saw RED or with checks unread. A red PR is
      one the fixer is about to act on (and whose meaning depends on the
      required-policy observation the reading cannot carry), so the reading would be
      bought every tick for a structurally impossible pass."""
    if any(i.has(Label.AI_WORKING) for i in world.issues if i.state == "open"):
        return ()
    inflight = _inflight_prs(world)
    if not inflight or len(inflight) > PROBE_HEAD_CEILING:
        return ()
    for pr in inflight:
        if isinstance(pr.ci, Unavailable):
            return ()
        if any(c.bucket == "fail" for c in pr.checks):
            return ()
    return tuple(HeadRef(p.number) for p in inflight)


def fix_probe_safe(world: WorldState, signals: ProbeSignals | None = None) -> bool:
    """True iff the cheap change signature is a sound stand-in for a full fix
    world build, given the last fully-built world and the blind-spot readings.

    The signature (newest `updated_at` across issues+PRs) is blind to CI
    check-run completion and mergeable/mergeStateStatus flips, which GitHub
    recomputes without bumping `updated_at`. Two shapes answer that blindness:

    * Nothing in flight — no OPEN `ai-working` issue and no OPEN `ai-pr-open`
      PR. There is no PR whose checks/mergeability the fixer would act on and no
      issue a fixer already holds, so the blind spot is moot. Work arriving into
      an idle world always bumps `updated_at` (labelling `ai-ready`, a comment,
      an edit), so the fixer wakes on the very next tick.
    * In-flight PRs whose blind spot was RE-READ this tick and is UNCHANGED
      since the last full build. The reading (`ProbeSignals`) folds CI and
      mergeability into the signature, so the fixer coasts through a CI wait —
      the ~180 full builds/hour that made the fixer the dominant GraphQL
      consumer — and builds the instant any of it moves.

    An OPEN `ai-working` issue is still unconditionally unsafe: a fixer holds it,
    and what the tick must not miss there is the leg's own progress, which no
    GitHub read describes.

    Fails toward observation at every ambiguity — no reading, a failed reading,
    a head the reading does not cover, a head the reading covers but the world
    does not, a failing check whose required-policy observation the reading cannot
    carry, or a world `probe_signal_heads` refuses to plan a reading for — all
    return False, i.e. a full build. A wasted build costs points; a wrongly
    skipped tick costs a stalled PR.

    What a gated tick DOES delay, bounded by the probe's TTL: any fix decision
    driven by the wall clock alone rather than by an observable GitHub change
    (the CI-pending escalation on a genuinely stuck check, stall markers, a
    capacity- or overlap-deferred `ai-ready` retry). All are recoverable and
    none exceed the TTL.

    `state` is checked explicitly because `issues_with`/`prs_with` filter by
    label only — a closed issue or a merged PR can keep its lifecycle label.
    """
    if any(i.has(Label.AI_WORKING) for i in world.issues if i.state == "open"):
        return False
    inflight = _inflight_prs(world)
    if not inflight:
        return True
    # Non-empty in flight: only a reading can answer, and only the heads
    # `probe_signal_heads` deems readable count — an empty plan there means the
    # reading cannot gate this world (ceiling, red CI, unread checks) → build.
    if not probe_signal_heads(world):
        return False
    if signals is None or signals.current is None or signals.previous is None:
        return False
    if any(s.failing for s in signals.current):
        return False
    wanted = {p.number for p in inflight}
    current = {s.number: s for s in signals.current}
    previous = {s.number: s for s in signals.previous}
    if set(current) != wanted or set(previous) != wanted:
        return False
    return all(current[n] == previous[n] for n in wanted)


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
    "ClosingRef",
    "WorldState",
    "HeadRef",
    "HeadSignal",
    "ProbeSignals",
    "probe_signal_heads",
    "fix_probe_safe",
    "replace",
]
