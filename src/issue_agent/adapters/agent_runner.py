"""AgentRunner: the ONLY module that imports the Claude Agent SDK.

Translates a `RuntimePlan` into `ClaudeAgentOptions`, runs `query()` to
completion and maps the final `ResultMessage` into `AgentResult`, reading the
decision from the envelope, never from prose or an exit code. The SDK import
is lazy so the pure mappings test without it.

The session id is kept from the first stream message that names one (the init
SystemMessage), so every returned end, including a watchdog abort, and a
transport death carry it (#340: read only from the final ResultMessage, a
wall-capped leg could not resume).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .child_env import CHILD_ENV_MANIFEST
from .leg_sandbox import LegSandbox, SandboxRefused, claude_runtime_state
from .runtime_plan import AgentRuntime, RuntimePlan, git_identity_env
from ..domain.contracts import Money, RunId, TokenUsage, UsageSource
from ..domain.pricing import RateTable, claude_model
from ..domain.run_usage import (
    EstimatedRunUsage,
    MeasuredRunUsage,
    ModelUsage,
    RunUsage,
    bounded_by_rates,
    price_run_usage,
    reserved_maximum_usage,
)
from ..domain.outcome import (
    AgentResult,
    SESSION_LIMIT,
    ExitReason,
    PROVIDER_REFUSED_SUBTYPE,
    declared_status,
    reclassify_pending_background_block,
)

# Longest single wait on the stream before re-checking the WALL clock, so a
# sleep-spanning gap is noticed promptly on wake.
_WATCHDOG_POLL_SLICE = 30.0
_NO_MESSAGE = object()

# bypassPermissions: the leg sandbox (#413) is the boundary, not the classifier.
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# Ceiling on ONE decoded SDK stream message. The SDK default (1MB) kills the
# transport on any Read above ~750KB once base64'd, deterministically, since
# the next dispatch reads the same file (#13, a 2.3MB PNG). Not preallocated.
DEFAULT_MAX_BUFFER_BYTES = 16 * 1024 * 1024


def build_option_kwargs(
    task: RuntimePlan,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    max_buffer_size: int | None = None,
) -> dict[str, Any]:
    """Pure: RuntimePlan → kwargs for ClaudeAgentOptions."""
    kwargs: dict[str, Any] = {"permission_mode": permission_mode, "max_budget_usd": task.max_budget_usd}
    if max_buffer_size:
        kwargs["max_buffer_size"] = int(max_buffer_size)
    # The claude_code preset keeps the full Claude Code prompt (the SDK default
    # is minimal); the task's system text is appended to it.
    kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code"}
    if task.append_system_prompt:
        kwargs["system_prompt"]["append"] = task.append_system_prompt
    if task.schema is not None:
        kwargs["output_format"] = {"type": "json_schema", "schema": task.schema}
    if task.allowed_tools:
        kwargs["allowed_tools"] = list(task.allowed_tools)
    if task.disallowed_tools:
        kwargs["disallowed_tools"] = list(task.disallowed_tools)
    if task.model:
        kwargs["model"] = task.model
    # Only when the task has an opinion: no effort keeps the SDK's default (#16).
    if task.effort is not None:
        kwargs["effort"] = task.effort.value
    if task.cwd:
        kwargs["cwd"] = task.cwd
    if task.resume:
        kwargs["resume"] = task.resume
    # Background tasks are off and cannot be overridden: a backgrounded
    # Bash/Task ends the stream with no ResultMessage, a paid verdict-less leg.
    kwargs["env"] = {**git_identity_env(task.model), **task.env, "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
    # Target settings and CLAUDE.md are untrusted checkout data: none load.
    kwargs["setting_sources"] = []
    return kwargs


def resolve_claude_cli(pinned: str, bundled: Path, which: Callable[[str], str | None] = shutil.which) -> str | None:
    """CLAUDE_BIN pin → SDK-bundled binary → PATH. The pin exists because a
    `uv sync` can delete the venv under a live daemon (keifu 2026-08-03: every
    review leg died pre-launch); it is taken as given."""
    if pinned.strip():
        return pinned.strip()
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return which("claude")


def exit_reason_from_envelope(subtype: str, is_error: bool) -> ExitReason:
    """Budget is singled out: a spend ceiling is an operator knob, not a failed
    attempt. Any other error is AGENT_ERROR (the agent was alive)."""
    st = (subtype or "").lower()
    if st == "success" and not is_error:
        return ExitReason.OK
    if "budget" in st:
        return ExitReason.BUDGET_EXHAUSTED
    return ExitReason.AGENT_ERROR if is_error else ExitReason.OK


def reported_model(msg: Any) -> str:
    """The concrete model an init or result message names (an unpinned task
    lets the SDK choose), from either SDK shape."""
    direct = getattr(msg, "model", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    data = getattr(msg, "data", None)
    nested = data.get("model") if isinstance(data, dict) else None
    return nested.strip() if isinstance(nested, str) and nested.strip() else ""


# A session id is a UUID; anything longer is malformed (ModelUsage's bound in
# domain/run_usage.py).


def reported_session(msg: Any) -> str:
    """The session id a stream message names, from either SDK shape (the init
    SystemMessage keeps it in `data`), or "" when absent or malformed."""
    value = getattr(msg, "session_id", None)
    if not isinstance(value, str) or not value.strip():
        data = getattr(msg, "data", None)
        value = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > SESSION_LIMIT:
        return ""
    return value.strip()


def result_to_agent_result(msg: Any) -> AgentResult:
    """Pure: SDK ResultMessage → AgentResult (duck-typed). An errored envelope
    with no declared status reports its subtype ("error_max_turns", …), and
    the subtype is always kept (#501 was undiagnosable without it)."""
    structured = getattr(msg, "structured_output", None) or {}
    status = declared_status(structured)
    subtype = str(getattr(msg, "subtype", "") or "")
    if not status and bool(getattr(msg, "is_error", True)):
        status = subtype or "error"
    if isinstance(structured, dict):
        summary = structured.get("summary", "") or (getattr(msg, "result", "") or "")
        needs = structured.get("needs") or ""
    else:
        summary, needs = getattr(msg, "result", "") or "", ""
    is_error = bool(getattr(msg, "is_error", True)) or status == "error"
    # Raw usage, verbatim: cache counters live only here, and a subscription
    # run reports total_cost_usd == 0.00.
    usage = getattr(msg, "usage", None)
    turns = getattr(msg, "num_turns", None)
    exit_reason = exit_reason_from_envelope(subtype, is_error)
    if is_error and getattr(msg, "terminal_reason", None) == "api_error":
        # The provider ended the leg (auth failure, 5xx, overload, or a
        # permanent 400; #418): codex's shape, so `domain.attempt` bounds it.
        exit_reason, subtype = ExitReason.TRANSPORT_DEATH, PROVIDER_REFUSED_SUBTYPE
    return AgentResult(
        is_error=is_error, status=status, envelope_subtype=subtype, summary=summary, needs=needs,
        cost_usd=float(getattr(msg, "total_cost_usd", 0.0) or 0.0), model=reported_model(msg),
        session_id=reported_session(msg),
        structured=structured if isinstance(structured, dict) else None,
        exit_reason=exit_reason,
        usage=usage if isinstance(usage, dict) else None, num_turns=turns if isinstance(turns, int) else None,
    )


# Counter keys in the envelope's per-model `modelUsage` and in its top-level
# `usage`, in TokenUsage order; all four are always present (CLI 2.1.294).
_MODEL_USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_MAX_TOKENS = 10**12  # accounting's bound on one counter
_MAX_MODELS = 64


def _tokens(raw: object, keys: tuple[str, ...]) -> TokenUsage:
    if not isinstance(raw, dict) or any(key not in raw for key in keys):
        raise ValueError("a token counter is missing")
    values = tuple(raw[key] for key in keys)
    if any(type(value) is not int or not 0 <= value <= _MAX_TOKENS for value in values):
        raise ValueError("a token counter is not a bounded integer")
    return TokenUsage(*values, source=UsageSource.PROVIDER)


def _per_model(session: str, raw: object) -> tuple[ModelUsage, ...]:
    if not isinstance(raw, dict) or len(raw) > _MAX_MODELS:
        raise ValueError("per-model usage is not a bounded mapping")
    if len({str(name).strip().casefold() for name in raw}) != len(raw):
        raise ValueError("per-model usage names a model twice")
    return tuple(ModelUsage(session, name, _tokens(counters, _MODEL_USAGE_KEYS)) for name, counters in raw.items())


def claude_leg_usage(msg: Any, *, session_id: str, main_model: str, resumed: bool, delegated: bool,
                     maximum: Money, rates: RateTable | None) -> tuple[RunUsage, tuple[ModelUsage, ...]]:
    """(this leg's usage, the session's cumulative per-model usage), from the
    result envelope (#389).

    On CLI 2.1.294 `modelUsage` names every model the leg used, subagents
    included, but on a resumed session it and `total_cost_usd` are
    session-cumulative; the top-level `usage` is this leg's own main loop
    only. A fresh leg is priced from `modelUsage`. A resumed leg's exact usage
    is its cumulative counters less the previous leg's, which accounting
    holds; this returns the upper bound used when that baseline is missing:
    the main loop's own usage plus the other models' session-cumulative
    usage, or all of it when the leg started a subagent (which may share the
    main model). Absent usage is charged zero with a gap (ADR 0002); malformed
    counters settle at the reserved maximum.
    """
    session = session_id or "unknown-session"
    main = main_model.strip()
    main = main if 0 < len(main) <= 256 else "<runtime-default>"
    own_raw, per_model_raw = getattr(msg, "usage", None), getattr(msg, "model_usage", None)
    try:
        cumulative = _per_model(session, per_model_raw) if per_model_raw is not None else ()
        if not resumed and per_model_raw is not None:
            priced, gap = cumulative, ""
        elif resumed and cumulative and (delegated or own_raw is None):
            priced, gap = cumulative, "upper bound: the session-cumulative usage"
        elif own_raw is not None:
            own = ModelUsage(session, main, _tokens(own_raw, _USAGE_KEYS))
            others = tuple(s for s in cumulative if s.model.casefold() not in
                           {main.casefold(), claude_model(main).casefold()})
            priced = (own, *others)
            gap = ("upper bound: the main loop's own usage plus other models' session-cumulative usage" if resumed
                   else "the result reported no per-model usage; priced its main loop only")
        else:
            return EstimatedRunUsage("the result reported no usage; charged zero", (), rates), ()
        # A model entry with no tokens bought nothing, so it needs no rate.
        samples = tuple(s for s in priced if s.usage.total_tokens) or (ModelUsage(session, main, TokenUsage(0, 0)),)
        usage = EstimatedRunUsage(gap, samples, rates) if gap else MeasuredRunUsage(samples, rates)
        return usage, cumulative
    except (ValueError, TypeError) as exc:
        detail = f"Claude usage counters malformed ({str(exc)[:200]}); charged the reserved maximum"
    return reserved_maximum_usage(session, main, maximum, UsageSource.PROVIDER, detail), ()


# The CLI's own dollar figure is a cross-check, never settlement (#389). It
# warns only when the CLI's figure is over 3x ours: the direction in which the
# harness could be under-charging (stale rates) or the CLI over-reporting (a
# stale CLI's fallback price, the session total 35x a leg's tokens on #1078).
# The other direction is the card's deliberate upper bounds (Haiku's
# long-prompt tier, 1h writes). 3x clears the known 2x disagreement on Sonnet
# 5.5 cache reads ($0.10 published, $0.20 in CLI 2.1.294).
_CROSS_CHECK_RATIO = 3


def cli_overstates(harness: Money, reported_usd: float) -> bool:
    """Whether the CLI's figure is over _CROSS_CHECK_RATIO times ours. A zero
    or unusable figure (a subscription reports 0.00) compares nothing."""
    try:
        reported = Money.from_major(harness.currency, reported_usd).micros
    except (ValueError, TypeError, ArithmeticError):
        return False
    return reported > _CROSS_CHECK_RATIO * harness.micros


def _short(value: Any, limit: int = 200) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def error_forensics(msg: Any) -> str:
    """WHY an errored ResultMessage errored, as one grep-able line: the
    envelope fields AgentResult drops (errors, permission denials, stop and
    terminal reasons, the model's own text). Never raises."""
    parts: list[str] = []
    try:
        for name in ("subtype", "stop_reason", "terminal_reason", "api_error_status", "num_turns", "duration_ms"):
            value = getattr(msg, name, None)
            if value not in (None, "", 0):
                parts.append(f"{name}={_short(value, 80)}")
        errors = getattr(msg, "errors", None)
        if errors:
            parts.append(f"errors={_short(errors, 400)}")
        denials = getattr(msg, "permission_denials", None)
        if denials:
            # One denial is a blocked command; forty is an agent wedged on policy.
            try:
                count: object = len(denials)
            except TypeError:
                count = "?"
            parts.append(f"permission_denials={count} {_short(denials, 400)}")
        text = (getattr(msg, "result", "") or "").strip()
        if text:
            parts.append(f"result={_short(text, 500)}")
    except Exception as exc:  # noqa: BLE001 — diagnostics never raise
        parts.append(f"(forensics failed: {exc})")
    return "; ".join(parts) or "envelope carried no diagnostic fields"


class StreamUsage:
    """Running token total of a stream, so an aborted run knows what it spent:
    dollars arrive only on the final ResultMessage (a 906s session once
    ledgered $0.00). ADR 0002: a killed leg is settled from usable evidence,
    never charged below what the stream shows.

    The CLI emits one AssistantMessage per content block, each repeating its
    API call's usage, and `output_tokens` is the message_start placeholder
    (1-3), never the final count (observed on CLI 2.1.294, #426); the true
    output arrives only in the ResultMessage. So only AssistantMessages count
    (task progress/notification and result messages carry other usage shapes),
    each message id once with the per-field maximum of its blocks, and a call's
    output is bounded above by what the stream still shows: the next call's
    prompt (input+cache) contains this call's output, so the growth between
    consecutive calls of one thread (a subagent's `parent_tool_use_id` is its
    own thread) bounds it. The thread's last call has no next prompt; a
    heuristic, not a bound, gives it the largest bound seen in its thread. The
    growth over-counts tool results and under-counts only across a compaction;
    it never falls below the placeholder."""

    _PROMPT = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

    def __init__(self) -> None:
        # thread -> call key -> per-field max usage, in arrival order. An id-less
        # message cannot be deduplicated: it gets its own key, counted not dropped.
        self._threads: dict[str | None, dict[object, dict[str, float]]] = {}

    def add(self, message: Any) -> dict | None:
        raw = getattr(message, "usage", None)
        if type(message).__name__ != "AssistantMessage" or not isinstance(raw, dict):
            return self.total()
        mid = getattr(message, "message_id", None)
        calls = self._threads.setdefault(getattr(message, "parent_tool_use_id", None), {})
        key = mid if isinstance(mid, str) else object()
        usage = calls.setdefault(key, {})
        for k, v in raw.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                usage[k] = max(usage.get(k, v), v)
        return self.total()

    def total(self) -> dict | None:
        calls_by_thread = [list(calls.values()) for calls in self._threads.values()]
        if not any(calls_by_thread):
            return None
        total: dict[str, float] = {}
        for calls in calls_by_thread:
            prompts = [sum(c.get(k, 0) for k in self._PROMPT) for c in calls]
            outputs = [max(c.get("output_tokens", 0), nxt - cur)
                       for c, cur, nxt in zip(calls, prompts, prompts[1:])]
            outputs.append(max([calls[-1].get("output_tokens", 0), *outputs]))
            for call, out in zip(calls, outputs):
                for k, v in call.items():
                    total[k] = total.get(k, 0) + (out if k == "output_tokens" else v)
                if out and "output_tokens" not in call:
                    total["output_tokens"] = total.get("output_tokens", 0) + out
        return total


def _trace_message(msg: Any) -> str:
    """One-line summary of a stream message, naming tool_use blocks so the
    LAST action before a transport death is visible. Never raises."""
    try:
        kind = type(msg).__name__
        blocks = getattr(msg, "content", None)
        if isinstance(blocks, list):
            parts = []
            for b in blocks:
                name = getattr(b, "name", None)
                if name:
                    ti = getattr(b, "input", None)
                    raw = str(ti.get("command") or ti.get("file_path") or "") if isinstance(ti, dict) else ""
                    hint = raw.strip().splitlines()[0][:80] if raw.strip() else ""
                    parts.append(f"{name}({hint})" if hint else name)
            if parts:
                return f"{kind} → {', '.join(parts)}"
        return kind
    except Exception:
        return "??"


# `killed` (task_updated) and `stopped` (task_notification) both end a task.
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})
# Only delegated agent work wakes the leg; a background shell may never end.
_DELEGATED_TASK_TYPES = frozenset({"local_agent", "local_workflow"})


def background_task_transition(msg: Any) -> tuple[str, bool] | None:
    """Pure: task-lifecycle system message → (task_id, still_pending), read
    from the base `subtype`/`data` every SDK build populates; None otherwise."""
    subtype = getattr(msg, "subtype", None)
    data = getattr(msg, "data", None)
    if not isinstance(data, dict):
        return None
    task_id = data.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    if subtype == "task_started":
        return (task_id, True) if data.get("task_type") in _DELEGATED_TASK_TYPES else None
    if subtype == "task_notification":
        return task_id, False
    if subtype == "task_updated":
        patch = data.get("patch")
        if (patch.get("status") if isinstance(patch, dict) else None) in _TERMINAL_TASK_STATUSES:
            return task_id, False
    return None


async def _aclose_quietly(agen: Any) -> None:
    """Close the SDK generator (tears down the query subprocess); never raises."""
    try:
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            await aclose()
    except Exception:
        pass


async def _cancel_task_quietly(task: Any) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


class AgentRunner(AgentRuntime):
    """Runs headless agent tasks over the SDK; `run` is safe concurrently for
    distinct `cwd`s (each query is its own SDK subprocess)."""

    def __init__(
        self,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        log: Any = None,
        debug: bool = False,
        idle_timeout: float | None = None,
        wall_clock: Callable[[], float] | None = None,
        poll_slice: float = _WATCHDOG_POLL_SLICE,
        max_runtime: float | None = None,
        max_buffer_size: int | None = DEFAULT_MAX_BUFFER_BYTES,
        process_supervisor: Any = None,
        rate_table: RateTable | None = None,
        leg_sandbox: LegSandbox | None = None,
    ):
        # The sandbox wraps the CLI behind the supervisor's wrapper: the SDK
        # takes one executable, so it needs that wrapper to carry bwrap.
        if leg_sandbox is not None and process_supervisor is None:
            raise ValueError("a sandboxed runner needs a process supervisor")
        self.permission_mode = permission_mode
        self._log = log or (lambda _m: None)
        self._debug = debug  # per-message tracing
        # Max seconds BETWEEN stream events (a hang), not total runtime.
        self._idle_timeout = idle_timeout
        # Both caps are measured on the WALL clock in short slices: macOS
        # freezes the monotonic clock during sleep, so a one-shot wait spanning
        # a suspend wedged a review after wake (Myapp #345).
        self._wall_clock = wall_clock or time.time
        self._poll_slice = poll_slice
        # Total runtime: a stream limping after a resume emits an event every
        # few minutes and never trips the idle cap (58 minutes on 3.6s of CPU).
        self._max_runtime = max_runtime
        self._max_buffer_size = max_buffer_size
        self._process_supervisor = process_supervisor
        self._rate_table = rate_table
        self._sandbox = leg_sandbox

    def _slice_timeout(self) -> float:
        """The poll slice, floored by any shorter armed cap."""
        return min([self._poll_slice] + [c for c in (self._idle_timeout, self._max_runtime) if c is not None])

    def _wall_timeout(self, elapsed: float, last_trace: str, usage: dict | None = None, msgs: int = 0) -> AgentResult:
        """The tripped total-runtime cap, costed by the stream usage so far.
        `msgs` tells "wedged early" from "ran out of road"."""
        self._log(f"sdk: WALL — total runtime {elapsed:.0f}s exceeded max {self._max_runtime:.0f}s; aborting after "
                  f"{msgs} stream messages (last: {last_trace or 'none'})")
        return AgentResult(is_error=True, status="timeout", exit_reason=ExitReason.WALL_TIMEOUT,
                           summary=f"agent exceeded max runtime {self._max_runtime:.0f}s",
                           usage=usage, stream_usage=usage)

    def _honour_captured(self, elapsed: float) -> None:
        self._log(f"sdk: WALL — {elapsed:.0f}s exceeded max {self._max_runtime:.0f}s, but a ResultMessage was "
                  "already captured; honouring it")

    def _priced(self, task: RuntimePlan, result: AgentResult, final: Any, delegated: bool,
                exited: bool) -> AgentResult:
        """The leg's usage and its price at the harness rates, the one
        settlement path Codex legs share."""
        maximum = Money.from_major("USD", task.max_budget_usd)
        bound, cumulative = claude_leg_usage(
            final, session_id=result.session_id, main_model=result.model or task.model or "",
            resumed=bool(task.resume), delegated=delegated, maximum=maximum, rates=self._rate_table)
        observed = bounded_by_rates(bound, self._rate_table, maximum)
        # The CLI persists its cumulative counters for the next resume only on
        # its own exit; one torn down after its result may restore stale ones,
        # so its snapshot is no baseline and its next leg prices from an
        # earlier one, erring high.
        cumulative = cumulative if exited else ()
        amount = price_run_usage(observed, self._rate_table)
        # A resumed leg's bound is replaced by its exact delta when accounting
        # holds the session's previous snapshot; it logs only if not.
        if isinstance(observed, EstimatedRunUsage) and (observed is not bound or not (task.resume and cumulative)):
            self._log(f"sdk: PRICING — {observed.detail}")
        # A resumed session's CLI figure is session-cumulative: nothing to compare.
        elif not task.resume and cli_overstates(amount, result.cost_usd):
            self._log(f"sdk: PRICE CROSS-CHECK — tokens at harness rates price this leg ${amount.micros / 1e6:.4f}; "
                      f"the CLI reported ${result.cost_usd:.4f}, over {_CROSS_CHECK_RATIO}x that. Settling on "
                      "the harness figure; check the CLI's model support and the rate table")
        return dataclasses.replace(result, run_usage=observed, session_usage=cumulative,
                                   cost_usd=amount.micros / 1_000_000)

    async def run(self, task: RuntimePlan) -> AgentResult:
        import claude_agent_sdk
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        supervised_run_id = RunId(task.env.get("ISSUE_AGENT_RUN_ID", "") or uuid.uuid4().hex)
        kwargs = build_option_kwargs(task, self.permission_mode, self._max_buffer_size)
        if self._process_supervisor is not None:
            executable = resolve_claude_cli(os.environ.get("CLAUDE_BIN", ""),
                                            Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")
            if executable is None:
                raise FileNotFoundError("Claude CLI executable is unavailable — set CLAUDE_BIN "
                                        "to an absolute path to pin it")
            command: tuple[str, ...] = (os.path.realpath(executable),)
            if self._sandbox is not None:
                cwd = task.cwd or os.getcwd()
                leg_env = {**os.environ, **kwargs["env"]}
                try:
                    launch = self._sandbox.launch(command, cwd=cwd, env=leg_env,
                                                  tmp=self._process_supervisor.leg_tmp(supervised_run_id),
                                                  runtime_state=claude_runtime_state(leg_env, cwd),
                                                  read_only=task.read_only)
                except SandboxRefused as exc:
                    self._log(f"sdk: SANDBOX REFUSED — {exc}")
                    return AgentResult(is_error=True, status="blocked", summary=f"sandbox unavailable: {exc}",
                                       exit_reason=ExitReason.RUNTIME_UNAVAILABLE, model=task.model or "")
                self._log(f"sdk: {launch.summary}")
                command = launch.argv
                kwargs["env"] = {**kwargs["env"], **launch.env}
            try:
                wrapper, supervisor_env = self._process_supervisor.supervised_cli(supervised_run_id, command)
            except BaseException:
                self._process_supervisor.kill_tree(supervised_run_id)  # ends the run's leg directory too
                raise
            kwargs["cli_path"] = wrapper
            kwargs["env"] = {**kwargs.get("env", {}), **supervisor_env}
            kwargs["env"][CHILD_ENV_MANIFEST] = " ".join(sorted(kwargs["env"]))

        # The CLI's own stderr names a gateway failure the SDK otherwise swallows.
        def _cli_stderr(line: str) -> None:
            if s := (line or "").rstrip():
                self._log(f"sdk-stderr: {s[:500]}")
        kwargs["stderr"] = _cli_stderr

        options = ClaudeAgentOptions(**kwargs)
        final: Any = None
        last_trace = ""  # what the agent last did, for abort and death lines
        msg_count, last_kind, last_msg, terminal_seen, resolved_model = 0, "none", None, "", ""
        resolved_session = ""  # the first id the stream names
        # Delegated children still running when OUR result arrived: a headless
        # leg can never consume their notification (#255).
        live_children: set[str] = set()
        children_at_result: frozenset[str] = frozenset()
        delegated = False  # whether this leg started any subagent (resumed-leg pricing)
        exited = True  # False once the harness tears the CLI down after its result

        def with_stream_identity(result: AgentResult) -> AgentResult:
            # The init model and session are the only attribution and resume
            # handle a no-result/timeout leg has. Before the stream names a
            # session, a resumed leg's is the one it resumed (codex's rule).
            return dataclasses.replace(result, model=result.model or resolved_model,
                                       session_id=result.session_id or resolved_session or task.resume or "")

        usage_acc: dict | None = None
        stream_usage = StreamUsage()
        # The clock is injectable: read it only when a cap is armed.
        started_at = self._wall_clock() if self._max_runtime is not None else 0.0
        agen = query(prompt=task.rendered_prompt, options=options).__aiter__()
        pending: Any = None
        try:
            while True:
                # Also checked between receives: a stream faster than one slice
                # never expires a slice, so a chatty runaway would be unbounded.
                if self._max_runtime is not None:
                    elapsed = self._wall_clock() - started_at
                    if elapsed >= self._max_runtime:
                        await _aclose_quietly(agen)
                        if final is None:
                            return with_stream_identity(self._wall_timeout(elapsed, last_trace, usage_acc, msg_count))
                        self._honour_captured(elapsed)
                        exited = False
                        break
                message: Any = _NO_MESSAGE
                stream_ended = False
                pending = asyncio.ensure_future(agen.__anext__())
                if self._idle_timeout is not None or self._max_runtime is not None:
                    # OBSERVE the one receive across slices with asyncio.wait,
                    # which never cancels it: cancelling __anext__ mid-read
                    # closed the stream and dropped the ResultMessage of every
                    # leg that paused longer than a slice.
                    last_event = self._wall_clock()
                    while True:
                        done, _ = await asyncio.wait({pending}, timeout=self._slice_timeout())
                        if done:
                            try:
                                message = pending.result()
                            except StopAsyncIteration:
                                stream_ended = True
                            break
                        now = self._wall_clock()  # one read per slice, shared by both caps
                        idle = now - last_event
                        if self._idle_timeout is not None and idle >= self._idle_timeout:
                            self._log(f"sdk: IDLE — no stream event for {idle:.0f}s (>{self._idle_timeout:.0f}s); "
                                      f"treating as hung (last: {last_trace or 'none'})")
                            await _cancel_task_quietly(pending)
                            await _aclose_quietly(agen)
                            if final is None:
                                return with_stream_identity(AgentResult(
                                    is_error=True, status="timeout", exit_reason=ExitReason.IDLE_TIMEOUT,
                                    summary=f"agent idle >{self._idle_timeout:.0f}s (no stream event)",
                                    usage=usage_acc, stream_usage=usage_acc))
                            stream_ended, exited = True, False
                            break
                        elapsed = now - started_at
                        if self._max_runtime is not None and elapsed >= self._max_runtime:
                            await _cancel_task_quietly(pending)
                            await _aclose_quietly(agen)
                            if final is None:
                                return with_stream_identity(self._wall_timeout(elapsed, last_trace, usage_acc, msg_count))
                            self._honour_captured(elapsed)
                            stream_ended, exited = True, False
                            break
                    if stream_ended:
                        break
                else:
                    try:
                        message = await pending
                    except StopAsyncIteration:
                        break
                msg_count += 1
                last_kind, last_msg = type(message).__name__, message
                # The first model named is the init's, the main loop's: a later
                # assistant message may be a subagent's.
                resolved_model = resolved_model or reported_model(message)
                resolved_session = resolved_session or reported_session(message)
                usage_acc = stream_usage.add(message)
                # A background-requested turn end carries terminal_reason on a
                # non-Result message: the forensic for a no-result drop.
                if tr := getattr(message, "terminal_reason", None):
                    terminal_seen = str(tr)
                if (transition := background_task_transition(message)) is not None:
                    task_id, still_pending = transition
                    (live_children.add if still_pending else live_children.discard)(task_id)
                    delegated = delegated or still_pending
                last_trace = _trace_message(message)  # always: abort lines print it
                if self._debug:
                    self._log(f"sdk: {last_trace}")
                if isinstance(message, ResultMessage):
                    # A finished background task injects a follow-up turn whose
                    # result does not answer our prompt; keep our own.
                    origin = getattr(message, "origin", None)
                    if isinstance(origin, dict) and origin.get("kind") == "task-notification":
                        if self._debug:
                            self._log("sdk: skipping task-notification follow-up result")
                        continue
                    final = message
                    children_at_result = frozenset(live_children)
        except asyncio.CancelledError:
            # Daemon shutdown: closing the generator tears down the subprocess.
            await _cancel_task_quietly(pending)
            await _aclose_quietly(agen)
            raise
        except Exception as exc:
            # The CLI raises when it exits non-zero AFTER its ResultMessage (an
            # is_error result, a benign post-`gh pr create` exit): the envelope
            # wins. Only a crash before any result is a transport death, and
            # it propagates (the caller's crash isolation), carrying the last
            # trace for the deterministic-death signature (#13).
            if final is None:
                self._log(f"sdk: transport died before any ResultMessage — {type(exc).__name__}: {str(exc)[:200]}"
                          + (f" (last: {last_trace})" if last_trace else ""))
                try:
                    setattr(exc, "harness_last_trace", last_trace)
                    setattr(exc, "harness_resolved_model", resolved_model)
                    setattr(exc, "harness_session_id", resolved_session or task.resume or "")
                except Exception:
                    pass
                raise
        finally:
            if self._process_supervisor is not None:
                self._process_supervisor.kill_tree(supervised_run_id)
        if final is None:
            # A clean end with no ResultMessage (result-delivery drop): the
            # count and the last message's fields tell a stream that never
            # started from one that lost only its final turn.
            fields = {}
            for a in dir(last_msg) if last_msg is not None else ():
                if a.startswith("_"):
                    continue
                try:
                    v = getattr(last_msg, a)
                except Exception:
                    continue
                if not callable(v):
                    fields[a] = repr(v)[:120]
            self._log(f"sdk: stream ENDED cleanly with NO ResultMessage after {msg_count} message(s); "
                      f"last kind={last_kind}" + (f", terminal_reason={terminal_seen}" if terminal_seen else "")
                      + (f", last={last_trace}" if last_trace else "") + " — result-delivery drop (no exception raised)"
                      + (f"; last_msg_fields={fields}" if last_msg is not None else ""))
            return with_stream_identity(AgentResult(is_error=True, summary="no ResultMessage from query()",
                                                   exit_reason=ExitReason.NO_RESULT, usage=usage_acc,
                                                   stream_usage=usage_acc))
        result = self._priced(task, with_stream_identity(result_to_agent_result(final)), final, delegated, exited)
        if result.envelope_subtype == PROVIDER_REFUSED_SUBTYPE:
            self._log(f"sdk: provider ended the turn ({result.summary[:200]}) — a machine failure unless this leg resumed one")
        reclassified = reclassify_pending_background_block(result, len(children_at_result))
        if reclassified is not result:
            self._log(f"sdk: BACKGROUND CHILD PENDING — the leg declared {result.status!r} while its own background "
                      f"task(s) {sorted(children_at_result)} were still running; a headless leg cannot receive their "
                      "notification, so this is not a human-input block")
            result = reclassified
        # The stream sum beside the envelope total: a calibration pair.
        if usage_acc:
            result = dataclasses.replace(result, stream_usage=dict(usage_acc))
        # Logged while the raw envelope is in hand; AgentResult drops it.
        if result.is_error:
            self._log(f"sdk: ERRORED — {error_forensics(final)}")
        return result
