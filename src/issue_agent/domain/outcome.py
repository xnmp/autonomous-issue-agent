"""Agent-result interpretation. Pure.

* `classify_outcome` reads the decision from the envelope, never prose: only an
  explicit is_error==false counts as success.
* `ExitReason` says WHY a session ended, separately from WHAT it decided.
* `classify_leg` maps a returned leg to the attempt classes derive counts
  (design rule 3: a machine failure never counts as an attempt).

## Why runtime unavailability is a machine failure (keifu #130, 2026-08-02)

A broken `codex` binary answered every dispatch with an instant HTTP 400: exit
non-zero, no envelope, no session id, no tokens, $0. Read as `AGENT_ERROR`,
three stillborn legs became three agent attempts and escalated a healthy issue.
A failure the harness caused measures the harness, so `infra_class_failure` is
the single predicate, and the classification is minted at the adapter boundary
that can observe "no session existed", never inferred later from prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

MAX_BUDGET_STATUS = "error_max_budget_usd"
# Harness-minted by the accounting runtime when a daily repository or global
# cap has no headroom for the leg's reservation; never read from agent output.
DAILY_HEADROOM_STATUS = "daily_headroom_exhausted"

from .contracts import PricingBasis
from .run_usage import ModelUsage, RunUsage


_DECLARED_STATUS_KEYS = ("status", "decision")


def declared_status(structured: object) -> str:
    """An agent's terminal claim: `decision` in review schemas, `status` in
    the others."""
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
    """WHY a session ended, orthogonal to `Outcome` (what it decided). Every
    terminal path is named so a log line says which watchdog fired."""

    OK = "ok"                              # clean envelope, no error
    AGENT_ERROR = "agent_error"            # the agent finished and reported failure
    BUDGET_EXHAUSTED = "budget_exhausted"  # spend ceiling hit mid-run
    IDLE_TIMEOUT = "idle_timeout"          # no stream event within the idle window
    WALL_TIMEOUT = "wall_timeout"          # total runtime exceeded the wall cap
    KILLED_EXTERNALLY = "killed_externally"  # SIGKILL from outside (OOM, operator)
    TRANSPORT_DEATH = "transport_death"    # stream/gateway died before any result
    OPERATOR_CANCELLED = "operator_cancelled"  # orderly daemon/operator teardown
    NO_RESULT = "no_result"                # stream ended cleanly, no ResultMessage
    # No session existed at all (broken binary, spawn failure, a refusal
    # before the first turn), unlike TRANSPORT_DEATH (a session lost its stream).
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    # `blocked` declared over the leg's own pending background child, which a
    # headless leg can never hear from (#255).
    BACKGROUND_CHILD_PENDING = "background_child_pending"


def infra_death(reason: ExitReason | None) -> bool:
    """The session died of something the agent had no part in: an external
    kill or a pre-result transport death. A timeout is not one (the agent was
    alive; a reliably-hanging task must keep counting)."""
    return reason in (ExitReason.KILLED_EXTERNALLY, ExitReason.TRANSPORT_DEATH)


def leg_cap(reason: ExitReason | None) -> bool:
    """The harness ended a live session at a per-leg resource cap: its wall
    clock or its spend ceiling (owner, 2026-10-14: a budget hit is a wall
    timeout). The session is intact, so the work continues by resuming it."""
    return reason in (ExitReason.WALL_TIMEOUT, ExitReason.BUDGET_EXHAUSTED)


def runtime_unavailable(reason: ExitReason | None) -> bool:
    """Whether the runtime never got a session off the ground (keifu #130)."""
    return reason is ExitReason.RUNTIME_UNAVAILABLE


# Harness-minted envelope subtypes for a leg refused before the agent ran:
# nothing about the issue caused them (provenance: the accounting runtime,
# never agent text).
ADMISSION_REFUSAL_SUBTYPES: frozenset[str] = frozenset({"dispatch_claim_unavailable"})


# Set by the codex runtime from its own rollout when the provider refused the
# final turn (capacity or quota): a machine failure whose session stays
# resumable.
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
    """NOTHING observable says a session existed: a stillborn dispatch, not
    an agent that ran and failed. Any one shred of evidence (session id,
    tokens, cost, output, a turn) makes it a real attempt; cost alone cannot
    decide (a subscription run reports $0.00)."""
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
    """A `blocked` declared while the leg's own background tasks were still
    pending asks a human to do the waiting (#255): mark it an error. Any other
    claim passes through; the claim stays in `status`."""
    if pending_children <= 0 or result.is_error or result.status != "blocked":
        return result
    return replace(
        result,
        is_error=True,
        exit_reason=ExitReason.BACKGROUND_CHILD_PENDING,
    )


def infra_class_failure(result: AgentResult) -> bool:
    """This failed leg measures the HARNESS, not the issue: it never ran, or
    died without the agent (keifu #130: stillborn legs walked a healthy issue
    to a stop at $0 a strike). Timeouts, NO_RESULT and budget hits are not
    included: a session existed."""
    return (
        infra_death(result.exit_reason)
        or runtime_unavailable(result.exit_reason)
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
    """Classify a returned leg. A per-leg cap hit (`leg_cap`: wall clock or
    spend) is a machine failure on a fresh session and an attempt on a resumed
    one: the item resumes once at the same tier (the leg runner resumes the
    latest machine end's session) and a second cap hit on that resume counts,
    whichever cap it is. An orderly harness teardown is never an attempt. The
    per-issue lifetime cap is not a leg cap: it refuses admission and stops
    for a human."""
    if infra_class_failure(result) or result.exit_reason is ExitReason.OPERATOR_CANCELLED:
        return LegClass.MACHINE
    if leg_cap(result.exit_reason):
        return LegClass.AGENT if resumed else LegClass.MACHINE
    if not result.is_error and result.status == "blocked":
        return LegClass.ASKED
    return LegClass.AGENT


def resumable_session(result: AgentResult) -> str:
    """The session a later leg may resume, or "" when this end left none usable."""
    if infra_class_failure(result) and result.envelope_subtype != PROVIDER_REFUSED_SUBTYPE:
        return ""
    return result.session_id or ""


# ── deterministic transport deaths (#13) ──────────────────────────────────────
# Some deaths are caused by the INPUT, so every retry reproduces them (tauri
# PR #552: a 2.3MB PNG Read blew the SDK buffer three times). Matched on the
# runtime's own exception text; keep the list short and specific.
DETERMINISTIC_DEATH_SIGNATURES: tuple[str, ...] = ("exceeded maximum buffer size",)

# The last tool call in a `_trace_message` line is what the session died on.
_TRACE_TOOL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\(([^)]*)\)")


@dataclass(frozen=True)
class DeterministicDeath:
    """Why a transport death is unwinnable; `path` names the input (diagnostic only)."""

    signature: str
    path: str = ""


def deterministic_death(
    error_text: str, last_trace: str = ""
) -> DeterministicDeath | None:
    """A reproducible-on-retry death, or None for the ordinary kind."""
    text = error_text or ""
    for sig in DETERMINISTIC_DEATH_SIGNATURES:
        if sig in text:
            return DeterministicDeath(signature=sig, path=offending_path(last_trace))
    return None


def offending_path(last_trace: str) -> str:
    """The argument of the last tool call in a trace, or ""."""
    matches = _TRACE_TOOL.findall(last_trace or "")
    if not matches:
        return ""
    return matches[-1][1].strip()


@dataclass(frozen=True)
class AgentResult:
    """Normalised result of a headless agent run; adapters populate it."""

    is_error: bool
    status: str = ""        # structured_output.status — the agent's own CLAIM
    # The envelope's own subtype, always: a claim and an observation differ
    # (#501: is_error=True with status="success").
    envelope_subtype: str = ""
    summary: str = ""       # structured_output.summary / .result
    needs: str = ""         # structured_output.needs
    cost_usd: float = 0.0
    # The model that actually ran ("" = not reported; readers fall back to the
    # task's): an unpinned task runs on the runtime's default (#43).
    model: str = ""
    session_id: str = ""
    structured: dict | None = None  # the full structured_output (any schema)
    # None = unclassified.
    exit_reason: "ExitReason | None" = None
    # Raw usage counters: tokens always exist, dollars may not (subscription, Codex).
    usage: dict | None = None
    # Runtime-reported model turns for this leg. None means the runtime did not
    # expose a count; zero is a valid count for a run stopped before generation.
    num_turns: int | None = None
    # The stream-summed counters of the same run: not `usage` (per-message
    # blocks re-report cache, ~3.5x the envelope total); an aborted run has
    # only these.
    stream_usage: dict | None = None
    # Runtime-authored only: structured model output cannot supply accounting.
    run_usage: RunUsage | None = None
    # Runtime-authored: the session's cumulative per-model counters at this
    # leg's end (Claude), the baseline its next resumed leg prices from.
    session_usage: tuple[ModelUsage, ...] = ()
    # Accounting-authored settlement provenance for completion telemetry.
    pricing_basis: PricingBasis | None = None

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
