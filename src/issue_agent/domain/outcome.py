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

The two PR columns move NO label except on blocked, because the PR-side
`ai-working` remove they used to open with was deleted (review M22/D9): nothing
ever added that label, so it was a `gh` call per leg removing nothing.

(*) The resolve-ERROR→`ai-ready` vs resume-ERROR→`ai-blocked` divergence is
INTENTIONAL, not an oversight. A resolve error is unattended work that must never
strand in `ai-working`, so it requeues. A resume error happens on a thread where a
HUMAN is already engaged (they answered a question to get here); silently
requeueing would discard that engagement and re-run a paid session on the same
unanswered ambiguity. Keeping it `ai-blocked` keeps the human in the loop.

## Exit-reason taxonomy

There are four policy classes, defined here so retry callers cannot drift:

* **agent strikes** (`AGENT_ERROR`, timeouts, no result) mean the agent returned
  or remained alive long enough to make an attempt; the relevant retry breaker
  advances.
* **infra deaths** (`KILLED_EXTERNALLY`, `TRANSPORT_DEATH`) are non-strikes.
  They leave issue legs alone for the reaper and count only against the separate
  infra-death ceiling.
* **runtime unavailability** (`RUNTIME_UNAVAILABLE`, plus the admission
  refusals the accounting runtime mints as `AGENT_ERROR` envelope subtypes) is
  also a non-strike. The leg never started at all, so it is evidence about the
  deployment, not about the issue. It shares the infra-death ceiling —
  `infra_class_failure` is the single predicate every quality counter asks.
* **budget exhaustion** (`BUDGET_EXHAUSTED`, surfaced by Codex as
  `error_max_budget_usd`) is also a non-strike, but it is actionable: the resolve
  path blocks the issue for a cap review and labels it `needs-budget-review`.
  The graceful wind-down handover is therefore preserved for the next deliberately
  resumed leg rather than being discarded in an automatic retry loop. The one
  exception is an admission refused for daily repository/global headroom
  (`DAILY_HEADROOM_STATUS`): time clears it, so the leg waits label-free.

## Why the fourth class exists (keifu #130, 2026-08-02)

A broken `codex` binary answered every dispatch with an instant HTTP 400: exit
non-zero, no envelope, no session id, no tokens, $0. `parse_codex_envelope`
had one bucket for "non-zero exit, no envelope" — `AGENT_ERROR` — so three
stillborn legs read as three agent attempts. Hours after the binary was fixed
the FIRST healthy dispatch escalated on `resolve failed 3× without opening a
PR`, and the same phantom counter fed `EscalationSignals.resolve_retries`,
which escalated an unrelated issue to high effort and spent $14 on it.

A failure the harness caused measures the harness. It must reach the operator
as an INFRA escalation, on the infra ceiling, and it must never appear as
difficulty evidence for a router. The quality counters therefore ask
`infra_class_failure`, never `infra_death`, and the classification is minted at
the adapter boundary that can actually observe "no session existed" — never
inferred later from prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

MAX_BUDGET_STATUS = "error_max_budget_usd"
# Harness-minted by the accounting runtime when a daily repository or global
# cap has no headroom for the leg's reservation; never read from agent output.
DAILY_HEADROOM_STATUS = "daily_headroom_exhausted"

from .actions import Action, ClearIssueState, Relabel, RemoveWorktree, Target
from .labels import Label
from .lifecycle import attention_actions
from .contracts import PricingBasis, WorkflowAttemptIdentity
from .run_usage import RunUsage


_DECLARED_STATUS_KEYS = ("status", "decision")


def declared_status(structured: object) -> str:
    """Return an agent's terminal claim from any supported output schema.

    Review schemas name the same terminal value ``decision`` while the other
    task schemas use ``status``. Keeping that translation at the runtime
    contract boundary prevents consumers from needing runtime-specific grammar.
    """
    if not isinstance(structured, dict):
        return ""
    for key in _DECLARED_STATUS_KEYS:
        value = structured.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


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
    OPERATOR_CANCELLED = "operator_cancelled"  # orderly daemon/operator teardown
    NO_RESULT = "no_result"                # stream ended cleanly, no ResultMessage
    # The runtime could not run the leg AT ALL: a broken/missing binary, a spawn
    # failure, a gateway that refused before any session existed. Distinct from
    # TRANSPORT_DEATH, which is a session that started and then lost its stream,
    # and from AGENT_ERROR, which is a session that ran and reported failure.
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    ACCOUNTING_UNAVAILABLE = "accounting_unavailable"  # live budget authority failed; terminal spend still settles
    # The leg ended its own turn while a background child task it started was
    # still pending. A headless leg can never receive that child's notification,
    # so a `blocked` it declares is waiting on itself, not on a human (#255).
    BACKGROUND_CHILD_PENDING = "background_child_pending"


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


def budget_exhausted(reason: ExitReason | None) -> bool:
    """Whether the harness ended a leg at its configured spend ceiling.

    This is deliberately separate from ``infra_death``: both are non-strikes,
    but a budget cap needs an operator-visible budget path rather than the
    automatic reaper retry used for a transient process or gateway death.
    """
    return reason is ExitReason.BUDGET_EXHAUSTED


def daily_headroom_wait(result: "AgentResult") -> bool:
    """Admission found no daily headroom; the leg waits for the window to roll."""
    return budget_exhausted(result.exit_reason) and result.envelope_subtype == DAILY_HEADROOM_STATUS


def runtime_unavailable(reason: ExitReason | None) -> bool:
    """Whether the runtime never got a session off the ground (keifu #130)."""
    return reason is ExitReason.RUNTIME_UNAVAILABLE


# Envelope subtypes the ADMISSION path mints when a leg is refused before the
# agent runs. They are reported as `AGENT_ERROR` because they end a dispatch,
# but nothing about the ISSUE caused them, so they must not march a per-issue
# quality ceiling. Matched on the envelope subtype the accounting runtime sets,
# which is HARNESS-OWNED — never on agent-supplied text (CLAUDE.md provenance
# rule). Lives here, not in a single caller, because every quality counter must
# read the same list.
ADMISSION_REFUSAL_SUBTYPES: frozenset[str] = frozenset(
    {
        "dispatch_claim_unavailable",
        "publication_authority_unavailable",
    }
)


# Set by the codex runtime from its own rollout (the token counters' trusted
# channel) when the provider refused the final turn for capacity or quota. The
# agent never got that turn, so it is a machine failure; the session it ended
# is intact, so it stays resumable (`resumable_session`).
PROVIDER_REFUSED_SUBTYPE = "provider_refused"


# Minted by the accounting runtime when a paid leg is refused BEFORE any run
# exists (#262); its summary names the missing authority.
RETURN_REFUSED_STATUS = "return_authority_unavailable"


def pre_admission_refusal(result: AgentResult) -> bool:
    """Harness-minted refusal marker (envelope_subtype is never agent-declared)."""
    return result.envelope_subtype == RETURN_REFUSED_STATUS



def no_session_evidence(
    *,
    session_id: str = "",
    usage: object = None,
    cost_usd: float = 0.0,
    output: str = "",
    num_turns: int | None = None,
) -> bool:
    """True iff NOTHING observable says a model session ever existed.

    Used by the runtime adapters to tell a stillborn dispatch (broken binary,
    HTTP 400 before the first turn) apart from an agent that ran and failed.
    Every input is a fact the runtime observed, and ANY of them being present
    means the leg ran: a session id, a token counter, a priced cost, captured
    output, or a completed turn.

    Deliberately conservative — it takes only one shred of evidence to be
    treated as a real attempt. `cost_usd` alone can never carry this decision
    (a subscription-plan run reports $0.00 on a session that certainly ran),
    which is exactly why the predicate is a conjunction over every substrate
    rather than a spend check.
    """
    return not (
        (session_id or "").strip()
        or usage
        or cost_usd > 0.0
        or (output or "").strip()
        or (num_turns or 0) > 0
    )


# Infra-class reasons beyond the death/unavailability predicates. A pending
# background child is NOT one: the agent started it and ended its turn (#255),
# a full paid session of its own behaviour, so it counts as an attempt.
_INFRA_CLASS_EXTRA: frozenset[ExitReason] = frozenset({ExitReason.ACCOUNTING_UNAVAILABLE})


def background_child_pending(reason: ExitReason | None) -> bool:
    """Whether the leg ended while its own background child was pending (#255)."""
    return reason is ExitReason.BACKGROUND_CHILD_PENDING


def reclassify_pending_background_block(
    result: AgentResult, pending_children: int
) -> AgentResult:
    """Strip a self-inflicted human-input block from a headless leg.

    `pending_children` is the runtime-observed count of background tasks this
    leg started that had not reached a terminal state when its own turn's
    result arrived. A non-interactive leg cannot receive their notifications,
    so a `blocked` declared over them asks a human to do the waiting (#253 →
    #255). Only that claim is rewritten: a result that is already an error, or
    that claims anything other than `blocked`, passes through, so a missed task
    terminal can never demote real work. The agent's claim stays in `status`.
    """
    if pending_children <= 0 or result.is_error or result.status != "blocked":
        return result
    return replace(
        result,
        is_error=True,
        exit_reason=ExitReason.BACKGROUND_CHILD_PENDING,
    )


def infra_class_failure(result: AgentResult) -> bool:
    """True iff this failed leg measures the HARNESS, not the issue.

    The single question every QUALITY counter asks before it advances — the
    resolve breaker, the CI/rebase breakers, and (through the counters they
    read) the router's difficulty evidence. A leg that never ran, or that died
    without the agent's participation, says nothing about how hard the issue is
    or how good the agent's work was; charging it walks a healthy issue to
    `needs-human-review` at $0 per strike and inflates effort routing on
    phantom evidence (keifu #130).

    Nothing escalates a machine failure either: the next dispatch of the same
    kind waits out a per-item backoff (`domain.backoff`) and the money caps
    bound the retries.

    NOT included, deliberately:

    * `WALL_TIMEOUT`/`IDLE_TIMEOUT` — the leg ran and spent its budget. A
      reliably-hanging task is a difficulty signal and must keep marching its
      breaker, or it loops forever at full session cost.
    * `NO_RESULT` — the stream ended cleanly, so a session existed.
    * `BUDGET_EXHAUSTED` — a non-strike too, but it has its own operator
      surface (`budget_exhausted`); callers branch on it first.
    * `None` — an unclassified/legacy result keeps exactly today's behavior.
    """
    return (
        infra_death(result.exit_reason)
        or runtime_unavailable(result.exit_reason)
        or result.exit_reason in _INFRA_CLASS_EXTRA
        or result.envelope_subtype in ADMISSION_REFUSAL_SUBTYPES
    )


class LegClass(str, Enum):
    """How one returned fix leg counts (design: only agent work is an attempt).

    AGENT    the agent did work: one attempt.
    ASKED    the agent returned asking a human for judgment: one attempt, and
             the item stops for a human (derive's QUESTION stop).
    MACHINE  the harness, provider or OS ended it: never an attempt; the item
             backs off and the money caps bound the retries.
    """

    AGENT = "agent"
    ASKED = "asked"
    MACHINE = "machine"


def classify_leg(result: "AgentResult", *, resumed: bool) -> LegClass:
    """Classify a returned leg. A wall-cap hit is a machine failure on a fresh
    session and an attempt on a resumed one: the item resumes once (the leg
    runner resumes the latest machine end's session) and a second wall-cap on
    that resume counts. A per-leg budget hit mid-run is agent work, and an
    orderly harness teardown never is."""
    if infra_class_failure(result) or result.exit_reason is ExitReason.OPERATOR_CANCELLED:
        return LegClass.MACHINE
    if result.exit_reason is ExitReason.WALL_TIMEOUT:
        return LegClass.AGENT if resumed else LegClass.MACHINE
    if not result.is_error and result.status == "blocked":
        return LegClass.ASKED
    return LegClass.AGENT


def resumable_session(result: AgentResult) -> str:
    """The session a later leg may resume, or "" when this end left none usable."""
    if budget_exhausted(result.exit_reason) or (
            infra_class_failure(result) and result.envelope_subtype != PROVIDER_REFUSED_SUBTYPE):
        return ""
    return result.session_id or ""


# ── deterministic transport deaths (#13) ──────────────────────────────────────
# The breakers assume a death is STOCHASTIC: retry the identical dispatch and it
# may survive. Some deaths are caused by the INPUT instead, so every retry
# reproduces them exactly — an unwinnable run bought N times at full session
# cost. Observed 2026-07-27 (xnmp/tauri-explorer PR #552): the reviewer Read a
# 2.3MB PNG, base64 x4/3 blew the SDK's message buffer, the transport died, and
# two retries died on the same Read.
#
# Matched against the exception text, so the signature must be a substring the
# runtime itself emits. Keep this list SHORT and specific: a false positive here
# escalates a PR a retry would have rescued.
DETERMINISTIC_DEATH_SIGNATURES: tuple[str, ...] = ("exceeded maximum buffer size",)

# `_trace_message` renders the last stream message as e.g.
# "AssistantMessage → Read(evidence/shot.png)". The LAST tool call in that line
# is what the session died on.
_TRACE_TOOL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\(([^)]*)\)")


@dataclass(frozen=True)
class DeterministicDeath:
    """Why a transport death is unwinnable, and (when the runtime captured a
    trace) which input caused it. `path` is diagnostic only — it names the file
    for the operator; nothing branches on it."""

    signature: str
    path: str = ""


def deterministic_death(
    error_text: str, last_trace: str = ""
) -> DeterministicDeath | None:
    """Classify a transport death as reproducible-on-retry, or None if it is the
    ordinary stochastic kind. Pure: both arguments are strings the adapter
    already has (the exception text and the last-message trace)."""
    text = error_text or ""
    for sig in DETERMINISTIC_DEATH_SIGNATURES:
        if sig in text:
            return DeterministicDeath(signature=sig, path=offending_path(last_trace))
    return None


def offending_path(last_trace: str) -> str:
    """The argument of the last tool call in a stream trace, or "" when the
    trace is absent/unparseable. Never raises — a diagnostic must not add a
    failure mode to an already-failing path."""
    matches = _TRACE_TOOL.findall(last_trace or "")
    if not matches:
        return ""
    return matches[-1][1].strip()


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
    # The model that ACTUALLY ran this leg, when the runtime knows it. Blank
    # means "not reported", and every reader falls back to the task's own model
    # exactly as it did before this field existed. It exists because a task can
    # pin no model at all and let the runtime supply its default — codex does
    # this on every unrouted leg — so `task.model` is None for a run that
    # certainly ran on SOMETHING, and the journal recorded no model for it.
    # CLAUDE.md's routing guardrail is "split by routed model tier", which that
    # blank makes impossible (#43).
    model: str = ""
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
    # Runtime-reported model turns for this leg. None means the runtime did not
    # expose a count; zero is a valid count for a run stopped before generation.
    num_turns: int | None = None
    # The stream-summed accumulator for the SAME run (merge_usage over every
    # stream message). NOT the same quantity as `usage`: per-message blocks
    # re-report cache counters, so the stream sum runs ~3.5x the ResultMessage
    # total on identical runs (measured 43.2M vs 12.1M, 2026-07-26). An aborted
    # run only ever has the stream sum, so the abort-cost estimator must learn
    # its $/token rate against THIS field's totals, never against `usage`'s.
    stream_usage: dict | None = None
    # Minted by accounting from captured admission context, never model output.
    workflow_attempt: WorkflowAttemptIdentity | None = None
    # Runtime-authored only: structured model output cannot supply accounting.
    run_usage: RunUsage | None = None
    # Accounting-authored settlement provenance for completion telemetry.
    pricing_basis: PricingBasis | None = None

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


def unblock_prompt(heading: str, message: str) -> str:
    """Build a blocked-thread prompt that `last_human_reply` can resume from."""
    return f"{heading}\n\n{message}{_BLOCKED_FOOTER}"


def budget_actions(issue: int) -> list[Action]:
    """Stop a budget-capped resolve-family leg for an operator cap review."""
    return attention_actions(
        Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED,
        unblock_prompt(
            "**Agent needs input:**",
            "**Budget review needed:** the resolve leg reached its configured "
            "spend cap before finishing. Review the cap and resume when ready; "
            "the saved handover will be provided to the next fixer leg.",
        ),
    ) + [Relabel(Target.ISSUE, issue, None, Label.NEEDS_BUDGET_REVIEW)]


def pr_budget_actions(pr: int) -> list[Action]:
    """Stop a budget-capped PR-repair leg (address-review / fix-ci / rebase)
    for an operator cap review — the #98 class on the PR path (#155).

    A per-issue lifetime refusal is DETERMINISTIC, so retrying is guaranteed
    to fail at $0 while marching the review/CI breaker: observed live
    2026-08-02 (keifu PRs #171/#174 walked to needs-human-review on three
    instant `error_max_budget_usd` rejections each). A cap decision, not an
    agent breaker, controls continuation. ai-blocked stops re-dispatch;
    needs-budget-review names WHICH decision unblocks it. Daily headroom is not
    routed here: `daily_headroom_wait` results wait label-free."""
    return attention_actions(
        Target.PR, pr, None, Label.AI_BLOCKED,
        unblock_prompt(
            "**Agent needs input:**",
            "**Budget review needed:** the repair leg hit its configured "
            "spend cap before finishing. Raise the cap, then remove "
            "`ai-blocked` and `needs-budget-review` to resume.",
        ),
    ) + [Relabel(Target.PR, pr, None, Label.NEEDS_BUDGET_REVIEW)]


def resolve_actions(
    issue: int,
    result: AgentResult,
    pr: int | None,
    *,
    issue_closed_without_pr: bool = False,
) -> list[Action]:
    """resolve-issue.sh tail as Actions. pr is the discovered PR number (ground
    truth) or None. PR opened → label issue+PR ai-pr-open; blocked → comment the
    'needs' and ai-blocked; error → back to ai-ready (never strand ai-working);
    infra death → NOTHING (stay ai-working for the reaper); budget exhaustion →
    ai-blocked + needs-budget-review so a cap decision, not an agent breaker,
    controls continuation (see module docstring)."""
    if issue_closed_without_pr:
        return [
            RemoveWorktree(issue),
            ClearIssueState(pr=None, issue=issue, merged=True),
        ]
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
    if daily_headroom_wait(result):
        return [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)]
    if budget_exhausted(result.exit_reason):
        return budget_actions(issue)
    if infra_death(result.exit_reason):
        return []
    if fu is ResolveFollowup.BLOCKED:
        needs = result.needs or result.summary or "see logs"
        return attention_actions(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED,
                                 unblock_prompt("**Agent needs input:**", needs))
    return [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)]


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
    # The human's reply was consumed by a leg that then waited on itself;
    # parking it ai-blocked again would ask the human a question nobody posed.
    if background_child_pending(result.exit_reason):
        return [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_READY)]
    if classify_outcome(result) == Outcome.BLOCKED:
        needs = result.needs or result.summary or "see logs"
        return attention_actions(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED,
                                 unblock_prompt("**Agent still needs input:**", needs))
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
    if infra_class_failure(result):
        # Not an attempt: leave the breaker where it was, or a machine under
        # memory pressure — or a runtime that cannot start at all (keifu #130)
        # — walks a healthy PR to needs-human-review without the fixer ever
        # having read the review. This is the ONE `infra_class_failure` call in
        # this module: the other guards choose LABEL MOVES, where a stillborn
        # leg keeps the ordinary requeue so the next poll retries it, and only
        # a true death is left to the reaper.
        return AddressReviewOutcome([], bump_retry=False)
    if budget_exhausted(result.exit_reason):
        # Not an attempt either — and frequently deterministic (admission
        # rejection). Route to the budget surface instead of the breaker.
        return AddressReviewOutcome([] if daily_headroom_wait(result) else pr_budget_actions(pr), bump_retry=False)
    if classify_outcome(result) is Outcome.BLOCKED:
        body = (
            "**Agent needs clarification on review:**\n\n"
            f"{result.summary or result.needs}\n\n---\n_Reply to unblock._"
        )
        # blocked: do NOT burn a retry; ai-blocked stops re-dispatch until a reply.
        return AddressReviewOutcome(attention_actions(Target.PR, pr, None, Label.AI_BLOCKED, body))
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
    if budget_exhausted(result.exit_reason):
        # Same rule as address-review: a cap decision, not the CI breaker,
        # controls continuation (#155 — the caller also skips its retry bump).
        return [] if daily_headroom_wait(result) else pr_budget_actions(pr)
    if classify_outcome(result) is Outcome.BLOCKED:
        # Post the agent's 'needs' alongside the label, same as the resolve
        # path's blocked arm. Without it a blocked rebase/fix-ci escalated to a
        # human with the WHY discarded — observed on PR #534 (2026-07-26): the
        # rebase agent declared blocked over two doc-file conflicts, ai-blocked
        # landed, and no comment, journal field, or log line anywhere said what
        # it thought it needed. The human inherits a mystery.
        needs = result.needs or result.summary or "see logs"
        return attention_actions(Target.PR, pr, None, Label.AI_BLOCKED,
                                 unblock_prompt("**Agent needs input:**", needs))
    return []
