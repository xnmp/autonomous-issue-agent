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
from dataclasses import dataclass, field
from enum import Enum

from .actions import Action, AgentKind, Relabel, RunAgent, Target
from .identity import login_eq
from .labels import Label
from .lifecycle import pr_escalation, retry_breaker
from .world import PRView, Review, WorldState, REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED

# Ceiling on UNPRODUCTIVE review rounds per PR (see ReviewConfig). Deliberately
# the same order of magnitude as the fixer's MAX_INFRA_DEATHS: high enough that
# ordinary transport flakiness never hands a PR to a human, low enough that a
# permanently-broken PR costs single-digit sessions instead of one per poll
# forever.
DEFAULT_MAX_UNPRODUCTIVE_ROUNDS = 5


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


class IdentityMode(str, Enum):
    """How many GitHub identities the harness runs under.

      THREE — fix, review and steward are DISTINCT logins, so the reviewer can
              approve the fixer's PR and auto-merge can land it.
      SOLO  — one login does everything; GitHub 422s self-approval, so the
              reviewer is disabled and a human reviews + merges.

    An enum, not a bare string (review M16): the two guards that read this knob
    have OPPOSITE polarity (`!= three` disables auto-merge, `== solo` disables
    the reviewer), so an unrecognised value such as `Three` used to fall
    BETWEEN them — the reviewer ran and spent, approvals landed, and nothing
    ever merged, with nothing in the log saying why.
    """

    THREE = "three"
    SOLO = "solo"

    @classmethod
    def from_env(cls, mode: str) -> "IdentityMode":
        """Resolve IDENTITY_MODE. Whitespace is trimmed; anything else must
        match a member EXACTLY or this raises ValueError for the config
        boundary to turn into a validation error (review M16).

        Deliberately stricter than `ReviewMode.from_env`, which falls back to
        SINGLE. ReviewMode has a safe default — the cheapest, most-proven
        review path. This knob has none: guessing THREE makes every approval
        422 on a solo install, guessing SOLO silently disables the reviewer.
        Case is NOT folded either, because the bash half of the harness
        (`bin/doctor.sh`, `agent/watcher-lib.sh`, `review/review-watcher.sh`)
        compares this env var with `[ "$IDENTITY_MODE" = three ]` — accepting
        `Three` here while bash rejects it would just move the divergence."""
        raw = (mode or "").strip()
        return cls(raw)  # ValueError on anything unrecognised — see docstring


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


def count_prior_rejections(reviews: tuple, review_bot: str) -> int:
    """How many times the review bot has REQUESTED_CHANGES on this PR SINCE its
    most recent APPROVE. This — not the number of author pushes — is what the
    escalation ceiling counts, with two properties:

      * a rebase or a post-approval housekeeping push advances the push counter
        but is not a rejection, so it never consumes a round; and
      * an APPROVE resets the count. If the bot approved and the author then
        re-opened work (a new commit on an approved-but-unmerged PR — CI failed,
        auto-merge off, etc.), the prior rejections were SATISFIED; the reviewer
        rejecting the new revision starts a fresh cycle, not the ceiling-breaker.
        Escalation must reflect UNRESOLVED dissatisfaction, and an APPROVE clears
        that.

    Derived purely from GitHub review state (no persisted counter), so it is
    correct across daemon restarts. `reviews` is chronologically ordered (gh
    returns oldest-first / by submittedAt), so 'since the last APPROVE' is the
    suffix after the last APPROVE index."""
    mine = [r for r in reviews if login_eq(r.author, review_bot)]
    last_approve = max(
        (i for i, r in enumerate(mine) if r.state == REVIEW_APPROVED),
        default=-1,
    )
    return sum(
        1 for r in mine[last_approve + 1:] if r.state == REVIEW_CHANGES_REQUESTED
    )


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
    to a human; adoption must not take it back."""
    return (
        cfg.adopt_unlabeled
        and pr.state == "OPEN"
        and not pr.has(cfg.trigger_label)
        and not pr.has(Label.NO_AI_REVIEW)
        and not pr.has(Label.NEEDS_HUMAN_REVIEW)
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
    stuck relabel cannot turn the paid loop into a comment loop."""
    if cfg.identity_mode == IdentityMode.SOLO:
        return []
    actions: list[Action] = []
    for pr in world.prs_with(cfg.trigger_label):
        if pr.state != "OPEN":
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


# ── review verdict → submission effect (review-pr.sh / re-review-pr.sh tails) ─
@dataclass(frozen=True)
class ReviewSubmission:
    """What to do with a review verdict. `event` is the gh review event to POST
    (APPROVE / REQUEST_CHANGES / COMMENT); `escalate` adds needs-human-review and
    drops ai-pr-open. The dispatcher submits the review then applies the labels."""

    event: str
    body: str
    escalate: bool = False


def review_submission(
    decision: str, body: str, prior_rejections: int, max_rejections: int
) -> ReviewSubmission:
    """Port of the review-pr.sh / re-review-pr.sh decision tails.

    APPROVE → APPROVE; ESCALATE (or the ceiling reached) → a COMMENT review +
    needs-human-review; anything else → REQUEST_CHANGES.

    Rejection ceiling: an APPROVE always wins (a satisfied reviewer ends the
    loop), but a REQUEST_CHANGES that would be the `max_rejections`-th rejection
    can no longer bounce the PR back to the fixer — it escalates. The ceiling
    counts the reviewer's OWN prior REQUEST_CHANGES verdicts (`prior_rejections`),
    NOT author pushes: a rebase or a post-approval housekeeping push that never
    drew a rejection does not consume a round toward the human handoff. This
    verdict is the (prior_rejections + 1)-th rejection, so escalate when that
    reaches the cap. Previously escalation counted every push and depended on the
    model voluntarily returning ESCALATE; this makes the ceiling a real breaker
    keyed on genuine dissatisfaction."""
    if decision == "APPROVE":
        return ReviewSubmission(event="APPROVE", body=body)
    this_rejection = prior_rejections + 1
    if decision == "ESCALATE" or this_rejection >= max_rejections:
        return ReviewSubmission(
            event="COMMENT",
            body=(
                f"Through {this_rejection} rounds of requested changes without "
                f"resolution. Requesting human review.\n\n{body}"
            ),
            escalate=True,
        )
    return ReviewSubmission(event="REQUEST_CHANGES", body=body)


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


def gate_submission(prior_rejections: int, max_rejections: int) -> ReviewSubmission:
    """A non-reviewable PR (the skip-gate found no behavioural change: docs /
    formatting / config only) → APPROVE. Pure so the gate→verdict mapping is a
    tested domain decision, not an inline dispatcher policy (same contract as
    synthesize_review / review_submission). The counts are passed for signature
    symmetry only — an APPROVE never escalates regardless."""
    return review_submission(
        "APPROVE",
        "No behavioural change to review (docs/formatting/config only).",
        prior_rejections, max_rejections,
    )


def synthesize_review(
    findings: list[Finding], prior_rejections: int, max_rejections: int,
    complete: bool = True,
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
    return review_submission(decision, body, prior_rejections, max_rejections)
