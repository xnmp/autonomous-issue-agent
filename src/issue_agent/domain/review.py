"""Review-watcher decision logic — pure port of review-watcher.sh:poll_once.

The reviewer is a DISTINCT identity from the fixer (no-self-approval). It runs
an initial review on each ai-pr-open PR with no session yet, a re-review when
the head SHA advances past the last reviewed SHA, and drops session state for
merged/closed PRs. In solo identity mode the reviewer is disabled (a human
reviews) — modelled here so the orchestration layer can honour it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .actions import Action, AgentKind, Relabel, RunAgent, Target
from .labels import Label
from .world import PRView, WorldState, REVIEW_APPROVED, REVIEW_CHANGES_REQUESTED


class ReviewMode(str, Enum):
    """How the reviewer examines a PR. Three genuine strategies (was a single
    REVIEW_MULTI_AGENT bool; a bool can't encode three states without an illegal
    combination, so it is an enum):

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
    def from_env(cls, mode: str, multi_agent_alias: bool) -> "ReviewMode":
        """Resolve REVIEW_MODE, honouring the legacy REVIEW_MULTI_AGENT=1 alias.
        Explicit REVIEW_MODE wins; else the alias maps to MULTI; else SINGLE.
        Unknown REVIEW_MODE values fall back to SINGLE (fail-safe: the cheapest,
        most-proven path, never a silent no-review)."""
        raw = (mode or "").strip().lower()
        if raw:
            try:
                return cls(raw)
            except ValueError:
                return cls.SINGLE
        return cls.MULTI if multi_agent_alias else cls.SINGLE


@dataclass(frozen=True)
class ReviewConfig:
    review_bot: str
    identity_mode: str = "three"
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


@dataclass(frozen=True)
class ReviewState:
    """Per-PR review session state (STATE_DIR files), passed in purely."""

    sessions: frozenset[int] = frozenset()       # PRs already reviewed once
    last_sha: dict[int, str] = field(default_factory=dict)  # PR → last reviewed head SHA


def needs_initial_review(pr: PRView, state: ReviewState) -> bool:
    return pr.number not in state.sessions


def needs_re_review(pr: PRView, state: ReviewState) -> bool:
    """Head SHA advanced past the last reviewed SHA. Mirrors the headRefOid vs
    pr-N.last-sha comparison in review-watcher.sh."""
    if pr.number not in state.sessions:
        return False
    cur = pr.head_ref_oid
    return bool(cur) and cur != state.last_sha.get(pr.number, "")


def _login_eq(a: str, b: str) -> bool:
    """Compare two GitHub logins, tolerating the `[bot]` suffix asymmetry. A
    GitHub App configured as `my-reviewer-bot[bot]` surfaces as that literal
    in some API shapes but as the bare `my-reviewer-bot` in the review
    author field `gh pr view --json reviews` returns. A raw `==` therefore never
    matches, which would silently defeat both the self-review guard and the
    rejection ceiling. Strip the suffix on both sides before comparing."""
    strip = lambda s: (s or "").removesuffix("[bot]")
    return strip(a) == strip(b)


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
    mine = [r for r in reviews if _login_eq(r.author, review_bot)]
    last_approve = max(
        (i for i, r in enumerate(mine) if r.state == REVIEW_APPROVED),
        default=-1,
    )
    return sum(
        1 for r in mine[last_approve + 1:] if r.state == REVIEW_CHANGES_REQUESTED
    )


def should_adopt(pr: PRView, cfg: ReviewConfig) -> bool:
    """A rogue (non-pipeline) PR that should be pulled into review. True iff
    adoption is on, the PR is OPEN, carries neither the trigger label nor the
    opt-out label, is not a draft, and was not opened by the review bot itself
    (never review our own output; avoids a self-review loop)."""
    return (
        cfg.adopt_unlabeled
        and pr.state == "OPEN"
        and not pr.has(cfg.trigger_label)
        and not pr.has(Label.NO_AI_REVIEW)
        and not pr.is_draft
        and not _login_eq(pr.author, cfg.review_bot)
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
    re-reviewed next poll.)"""
    if cfg.identity_mode == "solo":
        return []
    actions: list[Action] = []
    for pr in world.prs_with(cfg.trigger_label):
        if pr.state != "OPEN":
            continue
        if needs_initial_review(pr, state):
            actions.append(RunAgent(AgentKind.REVIEW, pr.number))
        elif needs_re_review(pr, state):
            actions.append(RunAgent(AgentKind.RE_REVIEW, pr.number))
    for pr in world.prs:
        if should_adopt(pr, cfg):
            actions.append(Relabel(Target.PR, pr.number, None, cfg.trigger_label))
            actions.append(RunAgent(AgentKind.REVIEW, pr.number))
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


# ── multi-agent review synthesis (REVIEW_MULTI_AGENT) ─────────────────────────
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
