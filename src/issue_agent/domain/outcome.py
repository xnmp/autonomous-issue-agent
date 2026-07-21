"""Agent-result interpretation + post-run follow-up decisions.

Two pure concerns ported from agent/lib.sh + resolve/resume/address-review:
  * `classify_outcome` — read the decision from the envelope, NEVER prose. The
    is_error subtlety (jq `//` treats false as empty) is the key lesson: only an
    explicit is_error==false counts as success.
  * `followup_*` — given the outcome AND GitHub ground truth (did a PR actually
    open?), decide the label moves + comments. Ground truth wins over prose: a
    PR that opened means success even if the model said "blocked".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .actions import Action, CloseIssue, PostComment, Relabel, Target  # noqa: F401
from .labels import Label


class Outcome(str, Enum):
    OK = "ok"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class AgentResult:
    """Normalised result of a headless agent run (the SDK ResultMessage, or the
    CLI JSON envelope). Adapter populates this; the domain reads only this.

    `status`/`summary`/`needs` are the FIX-schema convenience accessors; `field`
    reaches any other schema's keys (e.g. the review schema's decision/body) from
    the raw `structured` dict."""

    is_error: bool
    status: str = ""        # structured_output.status
    summary: str = ""       # structured_output.summary / .result
    needs: str = ""         # structured_output.needs
    cost_usd: float = 0.0
    session_id: str = ""
    structured: dict | None = None  # the full structured_output (any schema)

    def field(self, name: str, default: str = "") -> str:
        if isinstance(self.structured, dict):
            return self.structured.get(name, default) or default
        return default

    def field_bool(self, name: str, default: bool = False) -> bool:
        """Read a boolean structured-output field (review gate `reviewable`,
        validator `confirmed`). A missing/non-dict envelope → default; a present
        value is coerced with normal truthiness so a JSON true/false passes
        through unchanged."""
        if isinstance(self.structured, dict) and name in self.structured:
            return bool(self.structured.get(name))
        return default


def classify_outcome(result: AgentResult) -> Outcome:
    """Port of agent/lib.sh:claude_outcome. Error wins; then map status."""
    if result.is_error:
        return Outcome.ERROR
    if result.status in ("pr_opened", "pushed", "done"):
        return Outcome.OK
    if result.status == "blocked":
        return Outcome.BLOCKED
    # Unknown/empty status: treat as ok (the bash defaulted to ENV_STATUS or ok).
    return Outcome.OK


# Sentinels describing the follow-up the orchestration layer should apply. These
# are intentionally small enums/dataclasses rather than Action lists, because the
# adapter needs to interleave ground-truth lookups (pr_for_issue) it owns.
class ResolveFollowup(Enum):
    PR_OPENED = "pr_opened"        # → issue ai-working→ai-pr-open, label PR
    BLOCKED = "blocked"            # → comment 'needs', issue ai-working→ai-blocked
    RETRY = "retry"               # error → issue ai-working→ai-ready (never strand)


def resolve_followup(result: AgentResult, pr_opened: bool) -> ResolveFollowup:
    """Port of resolve-issue.sh tail. GROUND TRUTH first: if a PR actually opened
    (discovered by issue-number, independent of the model's claimed branch/status)
    it's a success. Otherwise blocked→ask, error→requeue."""
    if pr_opened:
        return ResolveFollowup.PR_OPENED
    if classify_outcome(result) == Outcome.BLOCKED:
        return ResolveFollowup.BLOCKED
    return ResolveFollowup.RETRY


# ── concrete follow-up action lists (ports of the script tails) ───────────────
_BLOCKED_FOOTER = "\n\n---\n_Reply to unblock. Any team member can answer._"


def resolve_actions(
    issue: int, result: AgentResult, pr: int | None
) -> list[Action]:
    """resolve-issue.sh tail as Actions. pr is the discovered PR number (ground
    truth) or None. PR opened → label issue+PR ai-pr-open; blocked → comment the
    'needs' and ai-blocked; error → back to ai-ready (never strand ai-working)."""
    fu = resolve_followup(result, pr is not None)
    if fu is ResolveFollowup.PR_OPENED:
        assert pr is not None
        return [
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_PR_OPEN),
            Relabel(Target.PR, pr, None, Label.AI_PR_OPEN),
        ]
    if fu is ResolveFollowup.BLOCKED:
        needs = result.needs or result.summary or "see logs"
        return [
            PostComment(Target.ISSUE, issue, f"**Agent needs input:**\n\n{needs}{_BLOCKED_FOOTER}"),
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED),
        ]
    return [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)]


class AuthorFollowup(Enum):
    AUTHORED = "authored"   # tests written → keep ai-working; implementer runs next
    BLOCKED = "blocked"     # → comment 'needs', issue ai-working→ai-blocked
    RETRY = "retry"         # error → issue ai-working→ai-ready (never strand)


def author_followup(result: AgentResult) -> AuthorFollowup:
    """Classify the author (test-writer) leg's terminal envelope. tests_authored
    is the success terminal here (the author never opens a PR). Ground truth (a PR)
    is irrelevant — the author doesn't create one."""
    if result.is_error:
        return AuthorFollowup.RETRY
    if result.status == "tests_authored":
        return AuthorFollowup.AUTHORED
    if result.status == "blocked":
        return AuthorFollowup.BLOCKED
    return AuthorFollowup.RETRY


def author_actions(issue: int, result: AgentResult) -> list[Action]:
    """author leg tail. AUTHORED → keep ai-working (the implementer leg picks it up
    on the next dispatch; the phase marker is persisted by the dispatcher). BLOCKED
    → ask the human + ai-blocked. ERROR → back to ai-ready (never strand)."""
    fu = author_followup(result)
    if fu is AuthorFollowup.AUTHORED:
        return []  # stay ai-working; dispatcher persists phase=authored
    if fu is AuthorFollowup.BLOCKED:
        needs = result.needs or result.summary or "see logs"
        return [
            PostComment(Target.ISSUE, issue, f"**Author needs input:**\n\n{needs}{_BLOCKED_FOOTER}"),
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED),
        ]
    return [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)]


# ── implementer leg + contract renegotiation (AUTHOR_TESTS_SPLIT) ──────────────
class ImplementFollowup(Enum):
    PR_OPENED = "pr_opened"            # success (ground truth or status)
    CONTRACT_CHANGE = "contract_change"  # petition: the contract is wrong → adjudicate
    BLOCKED = "blocked"               # ordinary block → human resume
    RETRY = "retry"                   # error → requeue


def implement_followup(result: AgentResult, pr_opened: bool) -> ImplementFollowup:
    """Classify the implementer leg. Ground truth wins (a PR that opened is success
    even if the model said otherwise). The contract_change petition is distinct from
    an ordinary block: it routes to adjudication, not to a human."""
    if pr_opened:
        return ImplementFollowup.PR_OPENED
    if result.is_error:
        return ImplementFollowup.RETRY
    if result.status == "contract_change":
        return ImplementFollowup.CONTRACT_CHANGE
    if result.status == "blocked":
        return ImplementFollowup.BLOCKED
    return ImplementFollowup.RETRY


class Adjudication(Enum):
    REVISED = "revised"   # the contract was amended → re-implement
    UPHELD = "upheld"     # the objection was rejected → re-implement, contract stands


def adjudication(result: AgentResult) -> Adjudication:
    """Read the adjudicator's verdict. Anything that isn't an explicit 'revised'
    (including an errored adjudication) is treated as UPHELD — fail toward keeping
    the contract the author already committed, never silently weaken it."""
    if not result.is_error and result.field("verdict") == "revised":
        return Adjudication.REVISED
    return Adjudication.UPHELD


def contract_escalation_actions(issue: int, objection: str, trail: str) -> list[Action]:
    """Round budget for contract renegotiation exhausted → escalate to a human with
    the implementer's objection and the adjudication trail. Issue ai-working →
    needs-human-review (stops the fixer re-dispatching it)."""
    body = (
        "**Contract dispute unresolved after the round budget.** A human needs to "
        "decide whether the acceptance criteria / tests should change.\n\n"
        f"**Implementer's objection:**\n{objection or 'see logs'}\n\n"
        f"**Adjudication trail:**\n{trail or '(none)'}"
    )
    return [
        PostComment(Target.ISSUE, issue, body),
        Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.NEEDS_HUMAN_REVIEW),
    ]


def resume_actions(
    issue: int, result: AgentResult, pr: int | None
) -> list[Action]:
    """resume-issue.sh tail. Same PR-opened success path; but on blocked OR error
    the issue stays ai-blocked (a human is already engaged) — error does NOT
    requeue to ai-ready here (differs from resolve)."""
    if pr is not None:
        return [
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_PR_OPEN),
            Relabel(Target.PR, pr, None, Label.AI_PR_OPEN),
        ]
    if classify_outcome(result) == Outcome.BLOCKED:
        needs = result.needs or result.summary or "see logs"
        return [
            PostComment(Target.ISSUE, issue, f"**Agent still needs input:**\n\n{needs}{_BLOCKED_FOOTER}"),
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED),
        ]
    return [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED)]


@dataclass(frozen=True)
class AddressReviewOutcome:
    """address-review.sh tail. The retry counter (keyed to the review timestamp)
    is orchestration state, so this returns the action list AND the decision the
    caller persists: clear_retry on success, bump_retry on error, neither on
    blocked (waiting on a human ≠ a failed attempt)."""

    actions: list[Action]
    clear_retry: bool = False
    bump_retry: bool = False


def address_review_actions(pr: int, result: AgentResult) -> AddressReviewOutcome:
    # The watcher added ai-working before dispatch; the tail always clears it.
    clear_working = Relabel(Target.PR, pr, Label.AI_WORKING, None)
    outcome = classify_outcome(result)
    if outcome is Outcome.OK or result.status == "pushed":
        return AddressReviewOutcome([clear_working], clear_retry=True)
    if outcome is Outcome.BLOCKED:
        body = (
            "**Agent needs clarification on review:**\n\n"
            f"{result.summary or result.needs}\n\n---\n_Reply to unblock._"
        )
        # blocked: do NOT burn a retry; ai-blocked stops re-dispatch until a reply.
        return AddressReviewOutcome(
            [clear_working, PostComment(Target.PR, pr, body),
             Relabel(Target.PR, pr, None, Label.AI_BLOCKED)]
        )
    # error/crash → count it so repeated failures on the SAME review escalate.
    return AddressReviewOutcome([clear_working], bump_retry=True)


def fix_ci_actions(pr: int, result: AgentResult) -> list[Action]:
    """fix-ci.sh tail. Always clear ai-working; on blocked also add ai-blocked.
    (The pushed/ok path just clears ai-working — CI re-runs and the watcher
    re-evaluates next poll.)"""
    clear_working = Relabel(Target.PR, pr, Label.AI_WORKING, None)
    outcome = classify_outcome(result)
    if outcome is Outcome.OK or result.status == "pushed":
        return [clear_working]
    if outcome is Outcome.BLOCKED:
        return [clear_working, Relabel(Target.PR, pr, None, Label.AI_BLOCKED)]
    return [clear_working]
