"""Agent-result interpretation + post-run follow-up decisions.

Three pure concerns ported from agent/lib.sh + resolve/resume/address-review:
  * `classify_outcome` — read the decision from the envelope, NEVER prose. The
    is_error subtlety (jq `//` treats false as empty) is the key lesson: only an
    explicit is_error==false counts as success.
  * `followup_*` — given the outcome AND GitHub ground truth (did a PR actually
    open?), decide the label moves + comments. Ground truth wins over prose: a
    PR that opened means success even if the model said "blocked".
  * `ExitReason` / `infra_death` — WHY the session ended, separate from WHAT it
    decided. An agent that errored made an attempt; a session the OS killed made
    none, and must not spend a retry or move a label (see the table below).

## Terminal transition table (outcome × leg → label move)

Read it as: given this classification, what does the leg's `*_actions` emit?

    outcome        resolve                resume                 address-review        fix-ci / rebase
    -------------  ---------------------  ---------------------  --------------------  ------------------
    PR opened      →ai-pr-open (+PR)      →ai-pr-open (+PR)      n/a                   n/a
    blocked        ask + →ai-blocked      ask + →ai-blocked      ask + +ai-blocked,    +ai-blocked
                                                                 NO retry bump
    error          →ai-ready (requeue)    →ai-blocked  (*)       retry bump, no move   no move (+ retry bump)
    infra death    (no move — stay        (no move — stay        no move, NO retry     no move, NO retry
                   ai-working)            ai-working)            bump                  bump

The author/implement legs (AUTHOR_TESTS_SPLIT) read as extra rows of the resolve
column: AUTHORED keeps ai-working for the implementer, blocked asks, error
requeues, and an infra death moves nothing (review M15).

The two PR columns move NO label except on blocked, because the PR-side
`ai-working` remove they used to open with was deleted (review M22/D9): nothing
ever added that label, so it was a `gh` call per leg removing nothing.

(*) The resolve-ERROR→`ai-ready` vs resume-ERROR→`ai-blocked` divergence is
INTENTIONAL, not an oversight. A resolve error is unattended work that must never
strand in `ai-working`, so it requeues. A resume error happens on a thread where a
HUMAN is already engaged (they answered a question to get here); silently
requeueing would discard that engagement and re-run a paid session on the same
unanswered ambiguity. Keeping it `ai-blocked` keeps the human in the loop.

**Infra death** (`KILLED_EXTERNALLY` / `TRANSPORT_DEATH`) emits no label move on
the issue legs and burns no retry anywhere. The session died for a reason the
agent had no part in — an OOM/jetsam SIGKILL mid-e2e (FAILURE-MODES #58), an
operator kill, a severed control stream. Treating that as an attempt did two
concrete harms: it marched the CI/review/rebase breakers toward
`needs-human-review` on work that was never actually tried, and it relabelled
`ai-working`→`ai-ready`, racing an operator's manual `ai-blocked` (#65). Leaving
the issue `ai-working` is the DESIGNED settle path: the in-process reaper sees an
`ai-working` issue with no live session on the next idle tick and requeues it once.
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


class ExitReason(str, Enum):
    """WHY a session ended — orthogonal to `Outcome` (what it decided).

    `Outcome` answers "did the agent succeed?"; this answers "did the agent even
    get to finish?". The distinction only matters for the two infra members (see
    `infra_death`), but naming every terminal path keeps the classification total
    and makes a log line say which watchdog fired.
    """

    OK = "ok"                              # clean envelope, no error
    AGENT_ERROR = "agent_error"            # the agent finished and reported failure
    BUDGET_EXHAUSTED = "budget_exhausted"  # spend ceiling hit mid-run
    IDLE_TIMEOUT = "idle_timeout"          # no stream event within the idle window
    WALL_TIMEOUT = "wall_timeout"          # total runtime exceeded the wall cap
    KILLED_EXTERNALLY = "killed_externally"  # SIGKILL from outside (OOM, operator)
    TRANSPORT_DEATH = "transport_death"    # stream/gateway died before any result
    NO_RESULT = "no_result"                # stream ended cleanly, no ResultMessage


def infra_death(reason: ExitReason | None) -> bool:
    """True iff the session died from something the AGENT had no part in, so the
    run must not count as an attempt.

    Deliberately narrow: only an external kill and a pre-result transport death
    qualify. A timeout (idle or wall) does NOT — the agent was alive and had the
    chance to make progress, so a hang must still march the breaker forward or a
    reliably-hanging task loops at full session cost forever. `None` (an
    unclassified/legacy result) is never an infra death, which keeps every caller
    at exactly today's behavior until an adapter starts populating the field.
    """
    return reason in (ExitReason.KILLED_EXTERNALLY, ExitReason.TRANSPORT_DEATH)


@dataclass(frozen=True)
class AgentResult:
    """Normalised result of a headless agent run (the SDK ResultMessage, or the
    CLI JSON envelope). Adapter populates this; the domain reads only this.

    `status`/`summary`/`needs` are the FIX-schema convenience accessors; `field`
    reaches any other schema's keys (e.g. the review schema's decision/body) from
    the raw `structured` dict."""

    is_error: bool
    status: str = ""        # structured_output.status — the agent's own CLAIM
    # The SDK envelope's own subtype (error_max_turns, error_during_execution,
    # success…), captured ALWAYS. `status` is only reported when the agent chose
    # to declare one, and it used to shadow this: #501 returned is_error=True with
    # status="success", and because the fallback was `if not status`, the envelope's
    # error kind was discarded — leaving "why did that run fail?" unanswerable from
    # the journal. A claim and an observation are different things; keep both.
    envelope_subtype: str = ""
    summary: str = ""       # structured_output.summary / .result
    needs: str = ""         # structured_output.needs
    cost_usd: float = 0.0
    session_id: str = ""
    structured: dict | None = None  # the full structured_output (any schema)
    # WHY the session ended. None = unclassified (a legacy/hand-built result, or a
    # runtime that doesn't classify yet) and every consumer treats it exactly as it
    # treated results before the taxonomy existed — so populating it is additive,
    # never a silent behavior change.
    exit_reason: "ExitReason | None" = None
    # Raw SDK usage block (input/output/cache_read/cache_creation token counts).
    # `cost_usd` is the gateway's own dollar figure, which is 0.00 on a subscription
    # plan and absent on Codex — tokens are the only substrate that always exists,
    # so capture them verbatim now to feed the cost ledger rather than re-deriving
    # them later from a log.
    usage: dict | None = None

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


def status_label(result: AgentResult) -> str:
    """What a run ACTUALLY did, for the operator's log line — envelope first.

    `result.status` is `structured_output.status`: the agent's own CLAIM about
    itself, not an observation. The two can disagree, and did. Issue #501's first
    resolve returned `is_error=True, exit_reason=agent_error` after 290s having
    committed nothing, while declaring `status="success"` — and the log line,
    which read `result.status or (…)`, printed:

        agent #501: resolve — done (status=success, $1.93)

    An operator (or a grep) reading that sees a clean success. `classify_outcome`
    was never fooled — error wins there, which is why the harness correctly
    requeued the issue — but the LOG, the thing humans actually read, said the
    opposite of what happened. A monitoring surface that reports the agent's
    self-assessment in place of the outcome is worse than no surface at all.

    So: a failed envelope can never render as a bare success. The claim is still
    shown, marked as a claim, because a disagreement between the two is itself
    the interesting signal.
    """
    if not result.is_error:
        return result.status or "ok"
    reason = result.exit_reason.value if result.exit_reason else "error"
    label = f"ERROR/{reason}"
    if result.envelope_subtype and result.envelope_subtype != reason:
        label += f":{result.envelope_subtype}"
    if result.status:
        label += f" (agent claimed {result.status!r})"
    return label


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
    'needs' and ai-blocked; error → back to ai-ready (never strand ai-working);
    infra death → NOTHING (stay ai-working for the reaper; see module docstring)."""
    fu = resolve_followup(result, pr is not None)
    if fu is ResolveFollowup.PR_OPENED:
        assert pr is not None
        return [
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_PR_OPEN),
            Relabel(Target.PR, pr, None, Label.AI_PR_OPEN),
        ]
    # Checked AFTER the ground-truth PR branch: how the session died says nothing
    # about whether a PR exists on GitHub, and a real PR must never be stranded
    # unlabelled just because the session was killed a second after opening it.
    if infra_death(result.exit_reason):
        return []
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
    → ask the human + ai-blocked. ERROR → back to ai-ready (never strand);
    infra death → NOTHING (stay ai-working for the reaper, like every other leg)."""
    fu = author_followup(result)
    if fu is AuthorFollowup.AUTHORED:
        return []  # stay ai-working; dispatcher persists phase=authored
    # The same guard the other three tails carry (review M15) — this leg was the
    # one that missed it. `author_followup` maps a killed session to RETRY, so an
    # OOM'd author leg relabelled ai-working→ai-ready, the fixer re-picked the
    # issue next poll, and the author leg was dispatched into the same OOM: a
    # relabel→dispatch→kill loop at full session cost with no breaker in it (the
    # infra-death ceiling only counts, it doesn't stop the requeue). Emitting
    # nothing leaves the issue ai-working, which is the designed settle path —
    # see the module docstring's "infra deaths are not attempts".
    if infra_death(result.exit_reason):
        return []
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
    requeue to ai-ready here (differs from resolve; the divergence is documented
    as intentional in the module docstring). Infra death → NOTHING: the session
    never ran, so it must not consume the human's reply by moving the label."""
    if pr is not None:
        return [
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_PR_OPEN),
            Relabel(Target.PR, pr, None, Label.AI_PR_OPEN),
        ]
    if infra_death(result.exit_reason):
        return []
    if classify_outcome(result) == Outcome.BLOCKED:
        needs = result.needs or result.summary or "see logs"
        return [
            PostComment(Target.ISSUE, issue, f"**Agent still needs input:**\n\n{needs}{_BLOCKED_FOOTER}"),
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED),
        ]
    return [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED)]


@dataclass(frozen=True)
class AddressReviewOutcome:
    """address-review.sh tail. The retry counter (keyed to the review's
    submitted_at) is orchestration state, so this returns the action list AND
    whether the attempt should COUNT: bump_retry on ok AND on error — an attempt
    was made either way, and a claimed "pushed" is not trusted as progress (a
    no-op push, or a push the reviewer rejects again, must still march the
    breaker forward). No bump on blocked (waiting on a human ≠ a failed
    attempt; ai-blocked already stops re-dispatch).

    Deliberately NO clear-on-success (the V2 lesson): the counter resets only
    when a NEW review re-keys it (supersession, see FixState.review_retries).
    The old clear_retry-on-push reset the counter every round, so the breaker
    could never trip and a downed review daemon meant an infinite loop at full
    session cost."""

    actions: list[Action]
    bump_retry: bool = False


def address_review_actions(pr: int, result: AgentResult) -> AddressReviewOutcome:
    """The clear-ai-working relabel this tail used to open with is GONE (review
    M22/D9): nothing in the SDK port ever ADDS ai-working to a PR, so every
    address-review leg spent a real `gh pr edit --remove-label` call removing a
    label that was never there — a busy-marker protocol with only a receiving
    half. Single-fixer is enforced by process topology (see `decide_fix`), so
    the ok/error paths now emit nothing at all."""
    if infra_death(result.exit_reason):
        # An external kill is not an attempt: leave the breaker where it was, or
        # a machine under memory pressure walks a healthy PR to
        # needs-human-review without the fixer ever having read the review.
        return AddressReviewOutcome([], bump_retry=False)
    if classify_outcome(result) is Outcome.BLOCKED:
        body = (
            "**Agent needs clarification on review:**\n\n"
            f"{result.summary or result.needs}\n\n---\n_Reply to unblock._"
        )
        # blocked: do NOT burn a retry; ai-blocked stops re-dispatch until a reply.
        return AddressReviewOutcome(
            [PostComment(Target.PR, pr, body),
             Relabel(Target.PR, pr, None, Label.AI_BLOCKED)]
        )
    # ok AND error both count: only a superseding review proves progress.
    return AddressReviewOutcome([], bump_retry=True)


def fix_ci_actions(pr: int, result: AgentResult) -> list[Action]:
    """fix-ci.sh tail (also the rebase tail). ONLY blocked moves a label: add
    ai-blocked so the watcher stops re-dispatching until a human replies. The
    pushed/ok/error paths emit nothing — CI re-runs and the watcher re-evaluates
    next poll. On infra death: nothing either — never add ai-blocked for a
    session that was killed rather than stuck, and the CALLER additionally skips
    its retry bump (see app/workflows/fix.py).

    The clear-ai-working relabel that used to lead every one of these branches
    is GONE (review M22/D9) — see `address_review_actions` for why."""
    if infra_death(result.exit_reason):
        return []
    if classify_outcome(result) is Outcome.BLOCKED:
        return [Relabel(Target.PR, pr, None, Label.AI_BLOCKED)]
    return []
