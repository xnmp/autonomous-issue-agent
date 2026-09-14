"""Review-watcher decision logic — pure port of review-watcher.sh:poll_once.

The reviewer is a DISTINCT identity from the fixer (no-self-approval). It runs
an initial review on each ai-pr-open PR with no session yet, a re-review when
the head SHA advances past the last reviewed SHA, and drops session state for
merged/closed PRs. In solo identity mode the reviewer is disabled (a human
reviews) — modelled here so the orchestration layer can honour it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, final

from .actions import Action, AgentKind, Relabel, RunAgent, Target
from .exemptions import AuthenticatedGrantPairs
from .identity import IdentityMode, login_eq
from .labels import Label
from .lifecycle import (
    AllowedEligibility, LifecycleOperation, pr_eligibility, pr_escalation,
    retry_breaker,
)
from .world import PRView, Review, WorldState, REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED

# Ceiling on UNPRODUCTIVE review rounds per PR (see ReviewConfig). Deliberately
# the same order of magnitude as the fixer's MAX_INFRA_DEATHS: high enough that
# ordinary transport flakiness never hands a PR to a human, low enough that a
# permanently-broken PR costs single-digit sessions instead of one per poll
# forever.
DEFAULT_MAX_UNPRODUCTIVE_ROUNDS = 5

# Ceiling on the reviewer's OWN change requests since its last APPROVE. Mirrors
# `HarnessConfig.max_review_rejections` (REVIEW_MAX_REJECTIONS) so the decide
# side can reason about the same breaker the submission side applies; a missing
# knob must never read as "no ceiling".
DEFAULT_MAX_REVIEW_REJECTIONS = 4

REVIEW_COMMENTED = "COMMENTED"
REVIEW_DISMISSED = "DISMISSED"

# Round-trip-stable marker inside a downgraded-for-ungranted-exemptions
# verdict body. The re-review patch-id shortcut must NEVER copy such a
# verdict onto a new head: its remedy is reviewer-side (emit the grant), so
# copying it denies the one thing that can clear it — a fresh reviewer run —
# and an unchanged patch livelocks to the rejection ceiling (tauri #634,
# 2026-08-09: reviewer approved on the merits, forgot the grant line, and
# every mechanical rebase re-copied the downgrade until needs-human-review).
UNGRANTED_DOWNGRADE_MARKER = (
    "did not grant every gate exemption required by the current patch"
)


def latest_reviewer_verdict(pr: PRView, reviewer: str) -> Review | None:
    """The reviewer's latest non-dismissed submitted verdict, if known."""
    own_verdicts = [
        (index, review)
        for index, review in enumerate(pr.reviews)
        if login_eq(review.author, reviewer)
        and review.state.upper() not in {"", "PENDING", "DISMISSED", "COMMENTED"}
    ]
    if not own_verdicts:
        return None
    _, latest = max(own_verdicts, key=lambda item: (item[1].submitted_at, item[0]))
    return latest


def reviewer_verdict_on_head(pr: PRView, reviewer: str) -> bool:
    """Whether `reviewer`'s latest submitted verdict targets this exact head."""
    latest = latest_reviewer_verdict(pr, reviewer)
    return bool(latest and pr.head_ref_oid and latest.commit_id == pr.head_ref_oid)


class ReviewMode(str, Enum):
    """How the reviewer examines a PR. Three genuine strategies (was a single
    boolean; a bool cannot encode three states without an illegal combination,
    so it is an enum):

      SINGLE    — one session-resumed agent reads the diff + files itself.
      MULTI     — the /code-review fan-out (skip-gate → parallel dimension
                  specialists → per-finding adversarial validation → synthesis).
      DELEGATED — like SINGLE, but the (expensive) reviewer is given a cheap
                  `code-scout` subagent and told to delegate codebase search /
                  context-gathering to it, so the expensive model reasons over
                  compressed summaries instead of burning its own tokens reading
                  raw files.
    """

    SINGLE = "single"
    MULTI = "multi"
    DELEGATED = "delegated"

    @classmethod
    def from_env(cls, mode: str) -> "ReviewMode":
        """Resolve REVIEW_MODE; malformed values fail safe to SINGLE."""
        raw = (mode or "").strip().lower()
        if raw:
            try:
                return cls(raw)
            except ValueError:
                return cls.SINGLE
        return cls.SINGLE


@dataclass(frozen=True)
class ReviewConfig:
    review_bot: str
    # A plain "three"/"solo" string still compares equal (str-Enum), so a
    # hand-built config keeps working; the config boundary hands over a member.
    identity_mode: IdentityMode = IdentityMode.THREE
    # The PR label the reviewer triggers on. Default `ai-pr-open` (the harness's own
    # pipeline PRs); an external-repo deployment sets this to `ai-review` so the same
    # daemon reviews human-authored PRs carrying that tag.
    trigger_label: Label = Label.AI_PR_OPEN
    # Rogue-PR adoption: when true, an OPEN PR carrying NO trigger label is adopted
    # (labelled with trigger_label) so it enters the normal review flow — closes the
    # gap where a PR opened directly (never via an issue) is reviewed by nobody.
    # Guarded: drafts, the review bot's OWN PRs, and PRs carrying the opt-out label
    # are never adopted.
    adopt_unlabeled: bool = False
    # Ceiling on UNPRODUCTIVE review rounds for one PR (review P0R-3). <=0 disables,
    # like every other breaker in the harness.
    #
    # The reviewer had NO ceiling of any kind. Every non-persisting outcome — a
    # session that died before returning a result, an is_error result, a MULTI
    # fan-out whose gate or dimension legs died so `synthesize_review` returned
    # None — leaves `needs_initial_review`/`needs_re_review` True, so the SAME
    # dispatch (under MULTI: the whole gate + 5 dimensions + N validators
    # fan-out) is paid for again on the very next poll, forever. This is the
    # bound. See ReviewState.unproductive_rounds for what is counted.
    max_unproductive_rounds: int = DEFAULT_MAX_UNPRODUCTIVE_ROUNDS
    # The submission-side rejection ceiling (REVIEW_MAX_REJECTIONS), mirrored
    # here so the decide side can recognise a PR that is PAST it. <=0 disables,
    # like every other breaker. Only `reviewer_abandoned_at_ceiling` reads it;
    # the ceiling is still APPLIED in `review_submission`.
    max_rejections: int = DEFAULT_MAX_REVIEW_REJECTIONS


@dataclass(frozen=True)
class ReviewState:
    """Per-PR review session state (STATE_DIR files), passed in purely."""

    sessions: frozenset[int] = frozenset()       # PRs already reviewed once
    last_sha: dict[int, str] = field(default_factory=dict)  # PR → last reviewed head SHA
    # PR number → how many review rounds were PAID FOR without advancing any
    # state (review P0R-3/P0R-4). One counter, because both shapes cost exactly
    # one full paid round and neither leaves a trace the round-based state
    # machine can see:
    #
    #   * the session died before returning a result — the dispatcher's
    #     `except BaseException` bump, or (when the daemon itself was killed and
    #     could not run that bump) a journal `agent_run_start` with no settled
    #     twin; and
    #   * the workflow ran to completion but persisted nothing — an is_error
    #     result, a failed submit POST, a MULTI gate/dimension leg that died so
    #     synthesis returned None.
    #
    # Keyed by PR NUMBER here: the two write sides use different store keys (the
    # linked issue number, or the negated PR number when a PR has no resolvable
    # issue), and reconciling those key spaces is the adapter's job
    # (`app.roles._review_state` reads their union) so the domain sees one
    # number per PR.
    unproductive_rounds: dict[int, int] = field(default_factory=dict)
    control_errors: Mapping[int, str] = field(default_factory=dict)


def needs_initial_review(pr: PRView, state: ReviewState) -> bool:
    return pr.number not in state.sessions


def needs_re_review(pr: PRView, state: ReviewState) -> bool:
    """Head SHA advanced past the last reviewed SHA. Mirrors the headRefOid vs
    pr-N.last-sha comparison in review-watcher.sh."""
    if pr.number not in state.sessions:
        return False
    cur = pr.head_ref_oid
    return bool(cur) and cur != state.last_sha.get(pr.number, "")


# ── rebase-invariant patch identity (harness #2) ──────────────────────────────
# A force-push moves the head SHA even when the PR's diff against its base is
# semantically identical (a mechanical rebase, an amended commit message, a
# reordered-but-unchanged history). `needs_re_review` can only see the SHA, so
# every such push used to buy a full paid re-review — three of them, ~$5 and
# 5-10 minutes each, on one deployment. This is the cheap discriminator: hash
# the diff after erasing exactly the parts a rebase perturbs.
#
# The two rebase-variant regions, and nothing else:
#   * `index <blob>..<blob> <mode>` — blob hashes are recomputed against the new
#     base (also `similarity index N%` on a rename, which git recomputes).
#   * `@@ -a,b +c,d @@ <section>` — line offsets shift when unrelated hunks
#     upstream of them move, and the trailing section heading is derived from
#     surrounding context git found, not from the change.
#
# Everything else is kept VERBATIM — file headers, mode/rename lines and every
# hunk body line (context, `+` and `-`). Keeping context lines is deliberate and
# matches `git patch-id`: if the rebase pulled in upstream edits adjacent to our
# change, the context differs and the signature must differ, because the reviewer
# is now looking at different code.
_INDEX_LINE = re.compile(r"^(?:index [0-9a-f]+\.\.[0-9a-f]+|(?:dis)?similarity index \d)")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def patch_signature(diff_text: object) -> str:
    """Stable hex digest of a unified diff, invariant under a no-op rebase.

    Two diffs share a signature iff they describe the same change to the same
    files — blob hashes and hunk offsets excluded. Equal signature ⇒ a re-review
    can only reach the verdict already on the PR, so the caller may carry that
    approval forward instead of paying for the round.

    TOTAL by construction (it gates a money decision, and a crash here would
    strand the PR mid-poll): any input is coerced to text, an empty or malformed
    diff simply hashes to whatever it hashes to. Line endings are normalised via
    `splitlines`, so a CRLF/LF transport difference is not a patch change.
    """
    if isinstance(diff_text, str):
        text = diff_text
    elif diff_text is None:
        text = ""
    elif isinstance(diff_text, (bytes, bytearray)):
        text = bytes(diff_text).decode("utf-8", "replace")
    else:
        text = str(diff_text)
    normalized = [
        "@@" if _HUNK_HEADER.match(line) else line
        for line in text.splitlines()
        if not _INDEX_LINE.match(line)
    ]
    return hashlib.sha256("\n".join(normalized).encode("utf-8", "replace")).hexdigest()


def count_prior_rejections(
    reviews: tuple,
    review_bot: str,
    *,
    non_resetting_approval_commits: frozenset[str] = frozenset(),
) -> int:
    """How many times the review bot has REQUESTED_CHANGES on this PR SINCE its
    most recent APPROVE. This — not the number of author pushes — is what the
    escalation ceiling counts, with two properties:

      * a rebase or a post-approval housekeeping push advances the push counter
        but is not a rejection, so it never consumes a round; and
      * a mergeable APPROVE resets the count. If the bot approved and the author then
        re-opened work (a new commit on an approved-but-unmerged PR — CI failed,
        auto-merge off, etc.), the prior rejections were SATISFIED; the reviewer
        rejecting the new revision starts a fresh cycle, not the ceiling-breaker.
        Escalation must reflect UNRESOLVED dissatisfaction, and an APPROVE clears
        that.

    Derived purely from GitHub review state (no persisted counter), so it is
    correct across daemon restarts. `reviews` is chronologically ordered (gh
    returns oldest-first / by submittedAt), so 'since the last APPROVE' is the
    suffix after the last APPROVE index. `non_resetting_approval_commits`
    carries current-head approvals the workflow has already proven cannot
    merge because required grants are absent; those must not manufacture a
    reset merely because GitHub calls their raw event APPROVED."""
    mine = [r for r in reviews if login_eq(r.author, review_bot)]
    last_approve = max(
        (
            i
            for i, r in enumerate(mine)
            if r.state == REVIEW_APPROVED
            and r.commit_id not in non_resetting_approval_commits
        ),
        default=-1,
    )
    return sum(
        1 for r in mine[last_approve + 1:] if r.state == REVIEW_CHANGES_REQUESTED
    )


@dataclass(frozen=True)
class SubmittedReview:
    """One review as GitHub's REST list returns it, carrying the id a dismissal
    needs. `world.Review` deliberately has no id (nothing in the decide path may
    address an individual review); the operator re-arm does, so it gets its own
    value object rather than widening the world model."""

    review_id: str
    state: str
    author: str
    submitted_at: str = ""


def stale_rejections(
    reviews: tuple[SubmittedReview, ...],
    review_bot: str,
    limit: int = 0,
    *,
    trailing_approvals_reset: bool = True,
) -> tuple[SubmittedReview, ...]:
    """Exactly the reviews an operator re-arm must dismiss: the review bot's
    CHANGES_REQUESTED verdicts that still COUNT toward the rejection ceiling.

    Same suffix rule as `count_prior_rejections` by default — an APPROVE
    satisfied every rejection before it — so dismissing this set and nothing
    else is what makes `count_prior_rejections` read zero again. Anything
    outside it (a human's change request, a rejection already answered by an
    APPROVE, an already-dismissed review) is not the harness's to retract.

    A PR durably attributed to a reviewer escalation may instead set
    `trailing_approvals_reset=False`. This recovers the rejection sequence
    preceding a trailing APPROVE that did not actually settle the PR (for
    example, because required authenticated exemption grants were absent).
    Retracting those resolved bot verdicts is safe only on that reviewer-held
    operator path; the default preserves live feedback.

    keifu PR #173 (2026-08-02): the dashboard unblock cleared the attention
    labels and the retry counters but left these reviews standing, so the
    ceiling was still tripped the instant the daemon looked again — an
    invisible dead state no amount of unblocking could clear.

    `limit` (<=0 disables) keeps only the newest `limit` of them, so a
    pathological history cannot turn one operator click into an unbounded
    dismissal fan-out. At the ceiling's own value this discards nothing that
    could still be holding the breaker down.
    """
    # States are compared EXACTLY, like `count_prior_rejections` — the two must
    # select the same set or the re-arm's contract ("dismiss these and the
    # ceiling reads zero") quietly stops holding. GitHub returns them
    # upper-case; anything else is a shape this harness does not count and must
    # therefore not retract either.
    mine = [
        review
        for review in reviews
        if login_eq(review.author, review_bot)
        and review.state not in {"", "PENDING", "DISMISSED"}
    ]
    last_rejection = max(
        (i for i, r in enumerate(mine) if r.state == REVIEW_CHANGES_REQUESTED),
        default=-1,
    )
    last_approve = max(
        (
            i
            for i, r in enumerate(mine)
            if r.state == REVIEW_APPROVED
            and (trailing_approvals_reset or i < last_rejection)
        ),
        default=-1,
    )
    counted = tuple(
        r for r in mine[last_approve + 1:] if r.state == REVIEW_CHANGES_REQUESTED
    )
    if limit > 0 and len(counted) > limit:
        return counted[-limit:]
    return counted


def count_outstanding_change_requests(reviews: tuple, fix_bot: str) -> int:
    """`count_prior_rejections` read from the FIXER's side of the PR (#16).

    The reviewer counts its OWN rejections because it owns the escalation
    ceiling and knows which login it reviews as. The fixer knows only which
    login IT pushes as, and must count everyone else's — a human reviewer's
    CHANGES_REQUESTED is exactly as much evidence that this PR is not
    converging as the review bot's, and the fixer cannot tell them apart
    anyway.

    Note the fail direction is INVERTED relative to its twin, deliberately: the
    twin SELECTS `login_eq(...)`, so an unset bot login counts nothing; this one
    EXCLUDES it, so an unset `fix_bot` excludes nothing and every rejection
    counts. Over-counting is the safe direction here (worst case: escalating an
    already-struggling PR), and GitHub forbids a PR's author from submitting
    APPROVE/REQUEST_CHANGES on their own PR, so there is in practice nothing of
    ours to over-count.

    Same two properties as its twin, for the same reasons: an APPROVE resets
    the count (prior dissatisfaction was satisfied), and author pushes are not
    rejections. Derived purely from GitHub review state, so it is correct
    across daemon restarts and — unlike the per-review attempt counter in the
    store — it does NOT reset when a new review supersedes the old one, which
    is what makes it a usable "rounds on this PR are climbing" signal.
    """
    others = [r for r in reviews if not login_eq(r.author, fix_bot)]
    last_approve = max(
        (i for i, r in enumerate(others) if r.state == REVIEW_APPROVED),
        default=-1,
    )
    return sum(
        1 for r in others[last_approve + 1:] if r.state == REVIEW_CHANGES_REQUESTED
    )


def should_adopt(pr: PRView, cfg: ReviewConfig) -> bool:
    """A rogue (non-pipeline) PR that should be pulled into review. True iff
    adoption is on, the PR is OPEN, carries neither the trigger label nor the
    opt-out label nor needs-human-review, is not a draft, and was not opened by
    the review bot itself (never review our own output; avoids a self-review
    loop).

    The needs-human-review exclusion is what makes the unproductive-round
    ceiling below actually STOP the money on an adopting deployment: escalation
    swaps the trigger label for needs-human-review, which drops the PR out of
    `prs_with(trigger_label)` — straight into the adoption sweep, which would
    re-label and re-dispatch it on the very next poll. An escalated PR belongs
    to a human; adoption must not take it back.

    ai-parked is excluded for the same reason: an operator park removes the
    trigger label to take the PR OUT of the pipeline, and the adoption sweep
    re-labeled it on the next poll — undoing the park and letting the budget
    arm re-escalate it (keifu PR #179, 2026-08-03). A parked PR is the
    operator's until they unpark."""
    return (
        cfg.adopt_unlabeled
        and pr.state == "OPEN"
        and not pr.has(cfg.trigger_label)
        and not pr.has(Label.NO_AI_REVIEW)
        and not pr.has(Label.NEEDS_HUMAN_REVIEW)
        and not pr.has(Label.AI_PARKED)
        and not pr.is_draft
        and not login_eq(pr.author, cfg.review_bot)
    )


# ── circuit breaker (review P0R-3) ────────────────────────────────────────────
def unproductive_ceiling_hit(state: ReviewState, cfg: ReviewConfig, pr: int) -> bool:
    """This PR has burned its budget of review rounds that bought nothing.
    Mirrors `domain.fix.infra_ceiling_hit` (same `retry_breaker` primitive,
    same cap<=0-disables convention); separate because the reviewer's counter
    is PR-keyed and folds in the non-dispatch-death stalls too."""
    return retry_breaker(
        state.unproductive_rounds.get(pr, 0), cfg.max_unproductive_rounds
    ).tripped


def reviewer_abandoned_at_ceiling(pr: PRView, cfg: ReviewConfig) -> bool:
    """This PR is at the rejection ceiling, the reviewer has already given up on
    its current head, and NOTHING now holds it on a human.

    That combination is a dead state, not a hold (keifu PR #173 / issue #25,
    2026-08-02). The ceiling escalation posts a COMMENT review and swaps the
    trigger label for needs-human-review; a legacy/manual clear can then remove
    needs-human-review without restoring the trigger, so the PR carries no
    lifecycle label at all. The reviewer will not re-review (its verdict is
    already on this head), the fixer will not dispatch address-review (the
    latest review is a COMMENT, not CHANGES_REQUESTED), and the escalation event
    stays frozen at the moment before the clear. Recognising it is what lets the
    reviewer re-emit the escalation instead of going quiet forever.

    The three conjuncts are what keep this from firing on a HEALTHY loop:

      * the rejection sequence before the escalation reached the ceiling. A
        trailing APPROVE is deliberately not a reset here: a mergeable approval
        settles the workflow, so a later reviewer COMMENT on the same head is
        durable evidence that the approval did not settle it;
      * the reviewer's newest review is a COMMENT on the CURRENT head — only
        the ceiling/verdict escalation posts COMMENT, and pinning it to the head
        means a fixer push still earns a real re-review (which may APPROVE and
        reset the count) rather than an instant re-escalation;
      * needs-human-review is absent — its presence IS the hold, and it is also
        what makes the re-emission self-dedup: escalating re-applies the label,
        so this reads False again until the next operator unblock (ADR 0012:
        one journalled re-emission per unblock generation, never per tick).

    An unknown commit_id ("" — the adapter could not resolve it) fails CLOSED:
    the unproductive-rounds breaker is the backstop, and re-escalating a PR we
    cannot place is worse than leaving it to that bound.
    """
    if cfg.max_rejections <= 0 or pr.state != "OPEN":
        return False
    if pr.has(Label.NEEDS_HUMAN_REVIEW):
        return False
    # A PR that still carries the trigger label is not abandoned — the
    # reviewer's own loop still owns it, and the labelled arm above decides
    # whether it gets a round. Excluding it also removes the only window in
    # which this arm and that one could act on the same PR in one tick.
    if pr.has(cfg.trigger_label):
        return False
    mine = [r for r in pr.reviews if login_eq(r.author, cfg.review_bot)]
    newest = next(
        (r for r in reversed(mine) if r.state != REVIEW_DISMISSED),
        None,
    )
    if not (
        newest is not None
        and newest.state == REVIEW_COMMENTED
        and pr.head_ref_oid
        and newest.commit_id == pr.head_ref_oid
    ):
        return False
    active = [
        review
        for review in mine
        if review.state not in {"", "PENDING", REVIEW_DISMISSED}
    ]
    last_rejection = max(
        (
            i
            for i, review in enumerate(active)
            if review.state == REVIEW_CHANGES_REQUESTED
        ),
        default=-1,
    )
    last_approve_before_rejection = max(
        (
            i
            for i, review in enumerate(active)
            if review.state == REVIEW_APPROVED and i < last_rejection
        ),
        default=-1,
    )
    prior_rejections = sum(
        1
        for review in active[last_approve_before_rejection + 1 :]
        if review.state == REVIEW_CHANGES_REQUESTED
    )
    return prior_rejections + 1 >= cfg.max_rejections


def abandoned_escalation_reason(cap: int) -> str:
    """Human-facing text for the re-emitted escalation. It must say what an
    operator has to DO differently, because the obvious move (unblock again) is
    exactly the one that already failed: the ceiling counts GitHub review state,
    so only dismissing the reviewer's change requests re-arms it."""
    return (
        f"the reviewer's rejection ceiling ({cap}) is still in force and the "
        "attention label was cleared without its change requests being "
        "dismissed, so no role can advance this PR."
    )


def unproductive_escalation_reason(cap: int) -> str:
    """Human-facing text for a reviewer ceiling escalation. Names both causes,
    because the operator's next move differs: repeated pre-result deaths point
    at the runtime/daemon, repeated no-verdict rounds at the model, the budget
    or the PR itself (a diff too large to review)."""
    return (
        f"{cap} review rounds on this PR ended without a verdict — the review "
        "session died before returning a result, errored, or could not submit. "
        "No review was recorded, so the harness has stopped paying for retries."
    )


def decide_review(
    world: WorldState, cfg: ReviewConfig, state: ReviewState
) -> list[Action]:
    """One review-watcher poll. In solo mode → nothing (humans review). Else:
    initial review for un-sessioned PRs, re-review when the SHA moved. Reviews
    are independent (no shared lock), so every eligible PR gets an action.

    Rogue-PR adoption: an OPEN PR with no trigger label (opened directly, never
    via the pipeline) is LABELLED with the trigger label AND reviewed the SAME
    poll — the relabel records that the PR is now in-pipeline; the review runs
    immediately so a comment lands on the first tick instead of a poll later.
    (The review reads the diff by PR number and does not depend on the label
    being present yet; saving the session this tick means the labelled PR is not
    re-reviewed next poll.)

    **Ceiling (review P0R-3).** Nothing here consumes a lock or a counter — one
    RunAgent per eligible PR per poll, unconditionally — so a PR whose review
    never persists state (session dies pre-result, agent errors, MULTI fan-out
    synthesises nothing) is re-dispatched at FULL cost every single poll,
    forever. `unproductive_rounds` is the bound: at the cap the dispatch is
    withheld and the PR is handed to a human instead, through the same
    `pr_escalation` every other breaker uses (so the Executor's funnel emits the
    escalation event + ALERT_CMD). The escalation is edge-triggered on the
    needs-human-review label: a PR already carrying it is silently skipped, so a
    stuck relabel cannot turn the paid loop into a comment loop.

    **Re-arm (keifu #173/#25).** A PR left at the rejection ceiling with its
    attention label cleared belongs to nobody: see
    `reviewer_abandoned_at_ceiling`. It is escalated again — free, no paid leg —
    which is what puts a fresh event on the dashboard and restores the hold. The
    escalation re-applies needs-human-review, so the arm is edge-triggered on
    exactly the same label as every other one here."""
    if cfg.identity_mode == IdentityMode.SOLO:
        return []
    actions: list[Action] = []
    for pr in world.prs_with(cfg.trigger_label):
        if pr.number in state.control_errors or not isinstance(
            pr_eligibility(pr, LifecycleOperation.REVIEW), AllowedEligibility
        ):
            continue
        if needs_initial_review(pr, state):
            kind = AgentKind.REVIEW
        elif needs_re_review(pr, state):
            kind = AgentKind.RE_REVIEW
        else:
            continue  # nothing to do — the ceiling is about DISPATCHES, not PRs
        if unproductive_ceiling_hit(state, cfg, pr.number):
            if not pr.has(Label.NEEDS_HUMAN_REVIEW):
                actions.extend(pr_escalation(
                    pr.number,
                    unproductive_escalation_reason(cfg.max_unproductive_rounds),
                    from_label=cfg.trigger_label,
                ).actions())
            continue
        actions.append(RunAgent(kind, pr.number))
    for pr in world.prs:
        if pr.number in state.control_errors or not isinstance(
            pr_eligibility(pr, LifecycleOperation.REVIEW), AllowedEligibility
        ):
            continue
        # Before adoption, deliberately: an abandoned-at-ceiling PR matches
        # `should_adopt` too (no trigger label, no needs-human-review), and
        # adopting it re-labels and re-dispatches a review that can only skip
        # (the reviewer's verdict is already on this head) — the silent half of
        # the keifu #173 dead state. Handing it back to a human wins.
        if reviewer_abandoned_at_ceiling(pr, cfg):
            actions.extend(pr_escalation(
                pr.number,
                abandoned_escalation_reason(cfg.max_rejections),
                from_label=cfg.trigger_label,
            ).actions())
            continue
        # A rogue PR is adopted AND reviewed the same tick, so the ceiling must be
        # checked here too — otherwise adoption is a second, unbounded entrance to
        # exactly the loop above. No escalation on this arm: the PR carries no
        # trigger label to swap yet, and once adoption has labelled it (which
        # happens on the tick BEFORE any dispatch can fail) it escalates through
        # the labelled arm like any other.
        if should_adopt(pr, cfg) and not unproductive_ceiling_hit(state, cfg, pr.number):
            claim = Relabel(
                Target.PR,
                pr.number,
                None,
                cfg.trigger_label,
                pr.head_ref_oid,
            )
            actions.append(claim)
            actions.append(RunAgent(AgentKind.REVIEW, pr.number, claim))
    return actions


# ── review dispatch vs. an in-flight fix leg (harness #176) ───────────────────
# 2026-08-08, tauri-explorer: four complete paid review legs (PRs #581 twice,
# #614, #617) died at submit with "review head changed before submission"
# because the reviewer dispatched while the fixer's ADDRESS_REVIEW leg on the
# same PR was still running and about to push. The submit-time fence is correct
# and stays; this is the upstream half that stops paying for the round at all.

# Margin added to the fixer's own total-runtime cap to get the deferral ceiling
# (ADR 0012). The ceiling MUST sit above the longest leg the harness will let
# run, or it fires on healthy work and re-creates the very race it prevents:
# `agent_max_runtime` is that cap, and the margin covers what happens after the
# session ends but before the claim is released (teardown, push, spend settle).
FIX_HOLD_SETTLE_SECONDS = 300.0

# Fallback when the total-runtime cap is disabled (0): only the idle watchdog
# bounds a leg then, so there is nothing to derive from and the ceiling is a
# declared operator choice instead.
FIX_HOLD_CEILING_SECONDS = 3600.0


def fix_hold_ceiling_seconds(agent_max_runtime: float) -> float:
    """Wall-time bound on a review deferral, derived from the fixer's leg cap.

    Past it the review dispatches anyway: a wedged or leaked claim must not
    starve review forever, and a wasted review round is cheaper than a PR
    nothing will ever look at again.

    The bound is per HOLD, so a fixer dispatching back-to-back legs on one PR
    restarts it. That is not a starvation hole: each of those legs pushes (which
    is what would have invalidated the review anyway), and the fixer's own
    retry/infra-death breakers cap how many it may run — ADDRESS_REVIEW in
    particular needs a non-superseded review to address, which only a dispatched
    review can create.
    """
    if agent_max_runtime <= 0:
        return FIX_HOLD_CEILING_SECONDS
    return agent_max_runtime + FIX_HOLD_SETTLE_SECONDS


@dataclass(frozen=True)
class FixHold:
    """A live fix-role claim on the same target as a pending review dispatch.

    `claim_key` identifies the CLAIM EPISODE, not whichever run is bound to it:
    a claim is taken at decision time and bound to a run later, so keying the
    journal on the run id emitted the same deferral twice under two keys.
    """

    pr: int
    claim_key: str
    held_seconds: float


@dataclass(frozen=True)
class ReviewDispatchPlan:
    """What survives the fix-hold filter, and what the journal owes."""

    actions: tuple[Action, ...]
    deferred: tuple[FixHold, ...]
    forced: tuple[FixHold, ...]


def hold_review_dispatch(
    actions: list[Action] | tuple[Action, ...],
    holds: Mapping[int, FixHold],
    *,
    ceiling_seconds: float = FIX_HOLD_CEILING_SECONDS,
) -> ReviewDispatchPlan:
    """Withhold this tick's review dispatches for PRs the fixer still holds.

    Only REVIEW/RE_REVIEW dispatches are withheld, and only for the held PR —
    every other PR's review is decided independently, as it always was. An
    adoption `Relabel` is dropped with its dispatch (as the reservation filter
    does), so a deferred PR is not claimed a tick before anyone reviews it.

    A hold older than the ceiling forces the dispatch through and is reported
    as `forced`; nothing here consumes state, so the next tick re-derives the
    whole answer from the store and a finished leg releases itself.

    A forced dispatch can still lose the submit-time head race, and the next
    tick will force it again. That loop is NOT unbounded: a review round that
    fails to submit persists nothing, which is exactly what
    `ReviewState.unproductive_rounds` counts, so `max_unproductive_rounds`
    withholds the dispatch and hands the PR to a human. The ceiling here only
    decides who bounds a wedged claim — it never removes the bound.
    """
    withheld: dict[int, FixHold] = {}
    forced: list[FixHold] = []
    for action in actions:
        if not isinstance(action, RunAgent):
            continue
        if action.kind not in {AgentKind.REVIEW, AgentKind.RE_REVIEW}:
            continue
        hold = holds.get(action.number)
        if hold is None:
            continue
        if hold.held_seconds >= ceiling_seconds:
            forced.append(hold)
            continue
        withheld[action.number] = hold
    if not withheld:
        return ReviewDispatchPlan(tuple(actions), (), tuple(forced))
    kept = tuple(
        action
        for action in actions
        if not (
            isinstance(action, RunAgent)
            and action.kind in {AgentKind.REVIEW, AgentKind.RE_REVIEW}
            and action.number in withheld
        )
        and not (
            isinstance(action, Relabel)
            and action.target is Target.PR
            and action.number in withheld
        )
    )
    return ReviewDispatchPlan(kept, tuple(withheld.values()), tuple(forced))


# ── review verdict → submission effect (review-pr.sh / re-review-pr.sh tails) ─
GrantRequest = tuple[str, str]
_COMPLETE_GRANTS_PROOF = object()


@dataclass(frozen=True)
class ReviewTarget:
    """The issue and patch whose review verdict will be submitted."""

    issue_number: int | None
    patch_id: str


@final
class CompleteGrants:
    """Opaque proof that every exemption required by one patch is authenticated."""

    __slots__ = ("_issue_number", "_patch_id", "_requests")

    def __init__(
        self,
        issue_number: int | None,
        patch_id: str,
        requests: tuple[GrantRequest, ...],
        proof: object,
    ) -> None:
        if proof is not _COMPLETE_GRANTS_PROOF:
            raise TypeError(
                "complete grants must come from authenticated grant resolution"
            )
        object.__setattr__(self, "_issue_number", issue_number)
        object.__setattr__(self, "_patch_id", patch_id)
        object.__setattr__(self, "_requests", requests)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("complete grant proof is immutable")

    @property
    def issue_number(self) -> int | None:
        return self._issue_number

    @property
    def patch_id(self) -> str:
        return self._patch_id

    @property
    def requests(self) -> tuple[GrantRequest, ...]:
        return self._requests


@dataclass(frozen=True)
class RejectedExemptions:
    """The non-empty exemption set that prevents an approval."""

    requests: tuple[GrantRequest, ...]

    def __post_init__(self) -> None:
        if not self.requests:
            raise ValueError("rejected exemptions require at least one request")
        object.__setattr__(self, "requests", tuple(dict.fromkeys(self.requests)))


GrantResolution = CompleteGrants | RejectedExemptions


def resolve_authenticated_grants(
    required: tuple[GrantRequest, ...],
    authenticated: AuthenticatedGrantPairs,
    *,
    target: ReviewTarget,
) -> GrantResolution:
    """Mint a patch-bound proof only when authenticated grants cover the request."""
    normalized = tuple(dict.fromkeys(required))
    proof_matches = (
        authenticated.issue_number == target.issue_number
        and authenticated.patch_id == target.patch_id
    )
    missing = tuple(
        request
        for request in normalized
        if not proof_matches or request not in authenticated
    )
    if missing or normalized and (target.issue_number is None or not target.patch_id):
        return RejectedExemptions(missing or normalized)
    return CompleteGrants(
        target.issue_number,
        target.patch_id,
        normalized,
        _COMPLETE_GRANTS_PROOF,
    )


@dataclass(frozen=True)
class ApprovedSubmission:
    """A GitHub approval whose required exemptions are proven complete."""

    body: str
    grants: CompleteGrants
    target: ReviewTarget
    event: Literal["APPROVE"] = field(default="APPROVE", init=False)
    escalate: Literal[False] = field(default=False, init=False)
    escalate_cause: Literal[""] = field(default="", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.grants, CompleteGrants):
            raise TypeError("an approval requires complete exemption grants")
        if (self.grants.issue_number, self.grants.patch_id) != (
            self.target.issue_number,
            self.target.patch_id,
        ):
            raise ValueError("approval grant proof belongs to a different review target")


@dataclass(frozen=True)
class ChangesRequestedSubmission:
    """A rejecting verdict, optionally caused by refused exemption requests."""

    body: str
    rejected_exemptions: RejectedExemptions | None = None
    event: Literal["REQUEST_CHANGES"] = field(
        default="REQUEST_CHANGES", init=False
    )
    escalate: Literal[False] = field(default=False, init=False)
    escalate_cause: Literal[""] = field(default="", init=False)


EscalationCause = Literal["verdict", "ceiling"]


@dataclass(frozen=True)
class EscalatedSubmission:
    """A comment verdict that transfers the PR to a human."""

    body: str
    escalate_cause: EscalationCause
    event: Literal["COMMENT"] = field(default="COMMENT", init=False)
    escalate: Literal[True] = field(default=True, init=False)

    def __post_init__(self) -> None:
        if self.escalate_cause not in {"verdict", "ceiling"}:
            raise ValueError("an escalation requires a recognized cause")


ReviewSubmission = (
    ApprovedSubmission
    | ChangesRequestedSubmission
    | EscalatedSubmission
)


def review_submission(
    decision: str,
    body: str,
    prior_rejections: int,
    max_rejections: int,
    *,
    grant_resolution: GrantResolution,
    target: ReviewTarget,
) -> ReviewSubmission:
    """Port of the review-pr.sh / re-review-pr.sh decision tails.

    APPROVE → APPROVE only when every gate exemption required by the current
    patch was granted. An approval that omits one or more required grants is an
    actionable REQUEST_CHANGES: revise the patch so it no longer needs those
    exemptions, or grant them on the next review. ESCALATE (or the ceiling
    reached) → a COMMENT review + needs-human-review; anything else →
    REQUEST_CHANGES.

    Rejection ceiling: a mergeable APPROVE always wins (a satisfied reviewer
    ends the loop), but a REQUEST_CHANGES — including a grant-incomplete
    approval normalized above — that would be the `max_rejections`-th rejection
    can no longer bounce the PR back to the fixer; it escalates. The ceiling
    counts the reviewer's OWN prior REQUEST_CHANGES verdicts (`prior_rejections`),
    NOT author pushes: a rebase or a post-approval housekeeping push that never
    drew a rejection does not consume a round toward the human handoff. This
    verdict is the (prior_rejections + 1)-th rejection, so escalate when that
    reaches the cap. Previously escalation counted every push and depended on the
    model voluntarily returning ESCALATE; this makes the ceiling a real breaker
    keyed on genuine dissatisfaction.

    `grant_resolution` is harness-derived, not model prose. Keeping this
    contradiction repair here makes the submitted GitHub verdict and rejection
    accounting agree: an unmergeable approval must not reset the rejection
    sequence or strand the PR behind a later merge-side escalation."""
    rejected_exemptions = (
        grant_resolution
        if isinstance(grant_resolution, RejectedExemptions)
        else None
    )
    if decision == "APPROVE" and rejected_exemptions is not None:
        unique = rejected_exemptions.requests
        requests = "\n".join(
            f"- `{rule}` for `{subject}`" for rule, subject in unique
        )
        guidance = (
            "The reviewer returned APPROVE but "
            + UNGRANTED_DOWNGRADE_MARKER
            + ". This is therefore "
            "REQUEST_CHANGES, not a mergeable approval. Either revise the "
            "patch so these exemptions are no longer required, or explicitly "
            "grant each pair on the next review. For `tests-immutable`, "
            "additive coverage belongs in a new test file when changing an "
            "existing test is not itself justified."
        )
        rationale = f"\n\nReviewer rationale:\n{body}" if body else ""
        body = f"{guidance}\n\nUngranted requests:\n{requests}{rationale}"
        decision = "REQUEST_CHANGES"
    if decision == "APPROVE":
        return ApprovedSubmission(body, grant_resolution, target)
    this_rejection = prior_rejections + 1
    ceiling_hit = max_rejections > 0 and this_rejection >= max_rejections
    if decision == "ESCALATE" or ceiling_hit:
        # The posted body names the real cause too — a verdict escalation
        # phrased as exhausted rounds reads like a fencepost bug (keifu #177).
        if decision == "ESCALATE":
            preamble = (
                f"The reviewer returned ESCALATE on rejection {this_rejection} "
                f"of {max_rejections}. Requesting human review."
            )
        else:
            preamble = (
                f"Through {this_rejection} rounds of requested changes without "
                "resolution. Requesting human review."
            )
        return EscalatedSubmission(
            f"{preamble}\n\n{body}",
            "verdict" if decision == "ESCALATE" else "ceiling",
        )
    return ChangesRequestedSubmission(body, rejected_exemptions)


# ── multi-agent review synthesis (REVIEW_MODE=multi) ──────────────────────────
# The dispatcher runs the LLM legs (skip-gate → parallel dimension specialists →
# per-finding adversarial validators); the DECISION — which findings survived and
# what verdict they add up to — is pure and lives here, so it is unit-tested with
# plain data and the orchestration layer stays thin (mirrors review_submission,
# resolve_actions, etc.).


@dataclass(frozen=True)
class Finding:
    """One candidate defect from a dimension specialist, carried through
    validation. `dimension` is the specialist that raised it (for the body);
    `confirmed` is set by the adversarial validator (None = not yet validated).
    `verdict` is the validator's one-line reasoning."""

    dimension: str
    location: str
    claim: str
    severity: str = "advisory"          # blocking | advisory
    confirmed: bool | None = None
    verdict: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.severity == "blocking"


def parse_findings(dimension: str, structured: dict | None) -> list[Finding]:
    """A dimension result's structured output → Findings. Tolerant of a missing/
    malformed envelope (a specialist that errored contributes no findings rather
    than crashing the review) — only well-formed items with a location + claim
    survive."""
    if not isinstance(structured, dict):
        return []
    out: list[Finding] = []
    for item in structured.get("findings", []) or []:
        if not isinstance(item, dict):
            continue
        loc = str(item.get("location", "")).strip()
        claim = str(item.get("claim", "")).strip()
        if not loc or not claim:
            continue
        sev = item.get("severity", "advisory")
        out.append(Finding(
            dimension=dimension, location=loc, claim=claim,
            severity=sev if sev in ("blocking", "advisory") else "advisory",
        ))
    return out


def _render_body(confirmed: list[Finding], approving: bool) -> str:
    """Markdown review body from the confirmed findings, blocking first. On an
    APPROVE with surviving advisories, they are listed as non-blocking notes."""
    if not confirmed:
        return "No blocking issues found across the review dimensions. LGTM."
    blocking = [f for f in confirmed if f.is_blocking]
    advisory = [f for f in confirmed if not f.is_blocking]

    def line(f: Finding) -> str:
        why = f" — {f.verdict}" if f.verdict else ""
        return f"- **{f.location}** ({f.dimension}): {f.claim}{why}"

    parts: list[str] = []
    if approving:
        parts.append("Approving. Non-blocking notes:")
        parts += [line(f) for f in advisory]
        return "\n".join(parts)
    if blocking:
        parts.append("### Must fix")
        parts += [line(f) for f in blocking]
    if advisory:
        parts.append("\n### Advisory")
        parts += [line(f) for f in advisory]
    return "\n".join(parts)


def gate_submission(
    prior_rejections: int,
    max_rejections: int,
    *,
    grant_resolution: GrantResolution,
    target: ReviewTarget,
) -> ReviewSubmission:
    """A non-reviewable PR (the skip-gate found no behavioural change: docs /
    formatting / config only) → APPROVE. Pure so the gate→verdict mapping is a
    tested domain decision, not an inline dispatcher policy (same contract as
    synthesize_review / review_submission). The counts are passed for signature
    symmetry only — an APPROVE never escalates regardless."""
    return review_submission(
        "APPROVE",
        "No behavioural change to review (docs/formatting/config only).",
        prior_rejections, max_rejections,
        grant_resolution=grant_resolution,
        target=target,
    )


def synthesize_review(
    findings: list[Finding], prior_rejections: int, max_rejections: int,
    complete: bool = True,
    *,
    grant_resolution: GrantResolution,
    target: ReviewTarget,
) -> ReviewSubmission | None:
    """Pure synthesis of validated findings into a verdict, then through the same
    round-ceiling breaker as the single-pass reviewer.

    Rule: keep only confirmed findings (the validator could not refute them); a
    surviving `blocking` finding → REQUEST_CHANGES, else APPROVE — fed through
    `review_submission` so the hard round ceiling still escalates a stuck PR.

    `complete` guards against a SILENT APPROVE when the review is incomplete: a
    dimension or validator leg errored (transport failure), so a defect-detecting
    lens may never have run. When incomplete AND no blocking finding survived,
    return None → the caller submits NOTHING and retries next poll (mirrors the
    gate.is_error path). A confirmed blocking finding is still reported regardless
    of completeness — real problems don't wait on the flaky leg."""
    confirmed = [f for f in findings if f.confirmed]
    has_blocking = any(f.is_blocking for f in confirmed)
    if not has_blocking and not complete:
        return None  # incomplete + nothing found → do not approve; retry next poll
    decision = "REQUEST_CHANGES" if has_blocking else "APPROVE"
    body = _render_body(confirmed, approving=not has_blocking)
    return review_submission(
        decision,
        body,
        prior_rejections,
        max_rejections,
        grant_resolution=grant_resolution,
        target=target,
    )
