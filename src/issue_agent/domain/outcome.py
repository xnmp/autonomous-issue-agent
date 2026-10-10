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
  resumed leg rather than being discarded in an automatic retry loop.

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
from dataclasses import dataclass
from enum import Enum

MAX_BUDGET_STATUS = "error_max_budget_usd"

from .actions import (  # noqa: F401
    Action,
    ClearIssueState,
    CloseIssue,
    PostComment,
    Relabel,
    RemoveWorktree,
    Target,
)
from .labels import Label
from .contracts import WorkflowAttemptIdentity
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
    ACCOUNTING_UNAVAILABLE = "accounting_unavailable"  # work ran; its complete charge is unknown
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


def reclassify_pending_background_block(
    result: AgentResult, pending_children: int
) -> AgentResult:
    """Strip a self-inflicted human-input block from a headless leg (#255)."""
    return result


def infra_class_failure(result: AgentResult) -> bool:
    """True iff this failed leg measures the HARNESS, not the issue.

    The single question every QUALITY counter asks before it advances — the
    resolve breaker, the CI/rebase breakers, and (through the counters they
    read) the router's difficulty evidence. A leg that never ran, or that died
    without the agent's participation, says nothing about how hard the issue is
    or how good the agent's work was; charging it walks a healthy issue to
    `needs-human-review` at $0 per strike and inflates effort routing on
    phantom evidence (keifu #130).

    Callers still bump the separate, higher infra-death ceiling, so a
    PERSISTENT harness fault still reaches a human — under an INFRA reason
    string, pointed at the runtime rather than at the code the agent never got
    to touch.

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
        or result.exit_reason is ExitReason.ACCOUNTING_UNAVAILABLE
        or result.envelope_subtype in ADMISSION_REFUSAL_SUBTYPES
    )


def infra_class_reason(result: AgentResult) -> str:
    """Machine-readable why, for the journal event that records the exemption.

    Blank when the leg is not infra-class, so a caller can use it as the
    predicate and the payload at once.
    """
    if result.envelope_subtype in ADMISSION_REFUSAL_SUBTYPES:
        return result.envelope_subtype
    if (infra_death(result.exit_reason) or runtime_unavailable(result.exit_reason)
            or result.exit_reason is ExitReason.ACCOUNTING_UNAVAILABLE):
        return result.exit_reason.value if result.exit_reason else ""
    return ""


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
class AdmissionBudgetBlock:
    """Harness-authored facts from a paid-admission denial.

    Agent structured output is not allowed to populate this field. It is minted
    only by the accounting adapter after its authoritative reservation check.
    """

    scope: str
    reservation_micros: int
    cap_micros: int
    committed_micros: int
    currency: str

    def __post_init__(self) -> None:
        if not self.scope or not self.currency:
            raise ValueError("admission budget facts require scope and currency")
        if min(
            self.reservation_micros,
            self.cap_micros,
            self.committed_micros,
        ) < 0:
            raise ValueError("admission budget facts cannot be negative")


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
    # Machine-readable warning raised by a runtime when a completed result is
    # structurally missing telemetry. Journaling persists this as a dedicated
    # warning event; blank means the runtime observed no telemetry fault.
    telemetry_warning: str = ""
    # Harness-authored admission evidence. Unlike `structured`, this cannot be
    # supplied by the agent and is the only budget result allowed to authorize
    # a future cap re-evaluation. Last for positional compatibility with the
    # established result envelope.
    admission_budget_block: AdmissionBudgetBlock | None = None
    # Minted by accounting from captured admission context, never model output.
    workflow_attempt: WorkflowAttemptIdentity | None = None
    # Runtime-authored only: structured model output cannot supply accounting.
    run_usage: RunUsage | None = None

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


def unblock_prompt(heading: str, message: str) -> str:
    """Build a blocked-thread prompt that `last_human_reply` can resume from."""
    return f"{heading}\n\n{message}{_BLOCKED_FOOTER}"


def budget_actions(issue: int) -> list[Action]:
    """Stop a budget-capped resolve-family leg for an operator cap review."""
    return [
        PostComment(
            Target.ISSUE,
            issue,
            unblock_prompt(
                "**Agent needs input:**",
                "**Budget review needed:** the resolve leg reached its configured "
                "spend cap before finishing. Review the cap and resume when ready; "
                "the saved handover will be provided to the next fixer leg.",
            ),
        ),
        Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED),
        Relabel(Target.ISSUE, issue, None, Label.NEEDS_BUDGET_REVIEW),
    ]


def pr_budget_actions(pr: int) -> list[Action]:
    """Stop a budget-capped PR-repair leg (address-review / fix-ci / rebase)
    for an operator cap review — the #98 class on the PR path (#155).

    A BUDGET_EXHAUSTED here is very often a DETERMINISTIC admission rejection
    (settled daily spend + one full reservation no longer fits the cap), so
    retrying is guaranteed to fail at $0 while marching the review/CI breaker:
    observed live 2026-08-02 (keifu PRs #171/#174 walked to needs-human-review
    on three instant `error_max_budget_usd` rejections each). A cap decision,
    not an agent breaker, controls continuation. ai-blocked stops re-dispatch;
    needs-budget-review names WHICH decision unblocks it (remove both labels
    after raising the cap, or wait for the daily window to roll)."""
    return [
        PostComment(
            Target.PR,
            pr,
            unblock_prompt(
                "**Agent needs input:**",
                "**Budget review needed:** the repair leg hit its configured "
                "spend cap (or the daily cap left no admission headroom for "
                "its reservation) before finishing. Raise the cap or wait for "
                "the daily window, then remove `ai-blocked` and "
                "`needs-budget-review` to resume.",
            ),
        ),
        Relabel(Target.PR, pr, None, Label.AI_BLOCKED),
        Relabel(Target.PR, pr, None, Label.NEEDS_BUDGET_REVIEW),
    ]


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
    if budget_exhausted(result.exit_reason):
        return budget_actions(issue)
    if infra_death(result.exit_reason):
        return []
    if fu is ResolveFollowup.BLOCKED:
        needs = result.needs or result.summary or "see logs"
        return [
            PostComment(Target.ISSUE, issue, unblock_prompt("**Agent needs input:**", needs)),
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
    if budget_exhausted(result.exit_reason):
        return budget_actions(issue)
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
            PostComment(Target.ISSUE, issue, unblock_prompt("**Author needs input:**", needs)),
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
            PostComment(Target.ISSUE, issue, unblock_prompt("**Agent still needs input:**", needs)),
            Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED),
        ]
    return [Relabel(Target.ISSUE, issue, Label.AI_WORKING, Label.AI_BLOCKED)]


def pr_resume_actions(pr: int, result: AgentResult) -> list[Action]:
    """Return a resumed PR-stage clarification to its PR lifecycle.

    The linked issue already remains `ai-pr-open`; only the PR was blocked, so
    changing the issue here would manufacture a second lifecycle state to keep
    coherent. The PR stays blocked while the one-at-a-time dispatcher runs, so
    a raised runner exception cannot strand an invented `ai-working` marker.
    """
    if infra_death(result.exit_reason):
        return []
    if classify_outcome(result) is Outcome.BLOCKED:
        needs = result.needs or result.summary or "see logs"
        return [
            PostComment(Target.PR, pr, unblock_prompt("**Agent still needs input:**", needs)),
        ]
    return [Relabel(Target.PR, pr, Label.AI_BLOCKED, Label.AI_PR_OPEN)]


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
        return AddressReviewOutcome(pr_budget_actions(pr), bump_retry=False)
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
    if budget_exhausted(result.exit_reason):
        # Same rule as address-review: a cap decision, not the CI breaker,
        # controls continuation (#155 — the caller also skips its retry bump).
        return pr_budget_actions(pr)
    if classify_outcome(result) is Outcome.BLOCKED:
        # Post the agent's 'needs' alongside the label, same as the resolve
        # path's blocked arm. Without it a blocked rebase/fix-ci escalated to a
        # human with the WHY discarded — observed on PR #534 (2026-07-26): the
        # rebase agent declared blocked over two doc-file conflicts, ai-blocked
        # landed, and no comment, journal field, or log line anywhere said what
        # it thought it needed. The human inherits a mystery.
        needs = result.needs or result.summary or "see logs"
        return [
            PostComment(Target.PR, pr, unblock_prompt("**Agent needs input:**", needs)),
            Relabel(Target.PR, pr, None, Label.AI_BLOCKED),
        ]
    return []
