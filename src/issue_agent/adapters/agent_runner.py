"""AgentRunner — the ONLY module that imports the Claude Agent SDK.

Replaces the bash `run_claude` + `claude_outcome` + the per-script arg assembly.
Translates a pure `AgentTask` into `ClaudeAgentOptions`, runs `query()` to
completion, and normalises the final `ResultMessage` into the domain's
`AgentResult` (decision read from the envelope, NEVER prose — same contract as
agent/lib.sh:claude_outcome).

The SDK import is lazy (inside methods) so importing this module — e.g. to unit
test `build_option_kwargs` — does not require the SDK to be installed. The pure
mapping is factored into `build_option_kwargs` and `result_to_agent_result` so
both are tested with no live model call.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from typing import Any

from ..domain.agent_task import AgentRuntime, AgentTask
from ..domain.outcome import AgentResult, ExitReason

# Longest single slice we block waiting for the next stream event before we come
# up for air to re-check the WALL clock. Small enough that a sleep-spanning gap is
# noticed promptly on wake; large enough to add negligible overhead on a healthy
# steadily-streaming run. Only relevant when idle_timeout or max_runtime is set.
_WATCHDOG_POLL_SLICE = 30.0

# Sentinel: "no event this iteration" (idle slice elapsed / stream ended). Distinct
# from any real SDK message and from None (which could be a legitimate yielded value).
_NO_MESSAGE = object()


def _default_wall_clock() -> float:
    return time.time()


def _option_field_names() -> frozenset[str]:
    """Names of ClaudeAgentOptions fields for the installed SDK build, cached.
    Guards optional kwargs (e.g. `stderr`) so we only pass them when the build
    accepts them — an older SDK would reject an unknown field at construction."""
    global _OPTION_FIELDS
    if _OPTION_FIELDS is None:
        import dataclasses

        try:
            from claude_agent_sdk import ClaudeAgentOptions

            _OPTION_FIELDS = frozenset(
                f.name for f in dataclasses.fields(ClaudeAgentOptions)
            )
        except (ImportError, TypeError):
            # Older SDK, or a stubbed test double that isn't a dataclass → treat as
            # "no optional fields known"; callers fall back to the base kwargs.
            _OPTION_FIELDS = frozenset()
    return _OPTION_FIELDS


_OPTION_FIELDS: frozenset[str] | None = None


# Default governed permission posture. `auto` exists in the Python SDK
# (Literal includes it) but we default to bypassPermissions so the in-process
# governor hook is unambiguously THE gate — matching the bash design where the
# settings allow-list + hook, not the classifier, enforced safety. Override via
# AgentRunner(permission_mode=…) if a target wants the classifier too.
DEFAULT_PERMISSION_MODE = "bypassPermissions"


def build_option_kwargs(
    task: AgentTask,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    extra_hooks: dict | None = None,
) -> dict[str, Any]:
    """Pure: AgentTask → kwargs for ClaudeAgentOptions. No SDK import, no I/O, so
    it is unit-tested directly. `extra_hooks` is the already-built
    {"PreToolUse": [HookMatcher(...)]} dict from the governor adapter (passed in
    so this stays SDK-free); merged verbatim."""
    kwargs: dict[str, Any] = {
        "permission_mode": permission_mode,
        "max_budget_usd": task.max_budget_usd,
    }

    # System prompt: claude_code preset + append (matches --append-system-prompt,
    # and crucially keeps the full Claude Code prompt the bash agents rely on —
    # the SDK uses a MINIMAL prompt by default).
    if task.append_system_prompt:
        kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": task.append_system_prompt,
        }
    else:
        kwargs["system_prompt"] = {"type": "preset", "preset": "claude_code"}

    if task.schema is not None:
        kwargs["output_format"] = {"type": "json_schema", "schema": task.schema}

    allowed = task.all_allowed_tools()
    if allowed:
        kwargs["allowed_tools"] = allowed
    if task.disallowed_tools:
        kwargs["disallowed_tools"] = list(task.disallowed_tools)
    if task.model:
        kwargs["model"] = task.model
    if task.cwd:
        kwargs["cwd"] = task.cwd
    if task.resume:
        kwargs["resume"] = task.resume
    if task.fork_session:
        kwargs["fork_session"] = True
    if task.env:
        kwargs["env"] = dict(task.env)
    if task.mcp_servers:
        kwargs["mcp_servers"] = {m.name: m.config for m in task.mcp_servers}
    if task.subagents:
        # Emit a plain-dict spec here (SDK-free, so this stays unit-testable); run()
        # converts each to the SDK's AgentDefinition before constructing options.
        # `Task` is already in all_allowed_tools() so the parent can spawn them;
        # each subagent carries its OWN tool allow-list + model.
        kwargs["agents"] = {
            s.name: {
                "description": s.description,
                "prompt": s.prompt,
                "tools": list(s.tools) or None,
                "model": s.model,
            }
            for s in task.subagents
        }
    if extra_hooks:
        kwargs["hooks"] = extra_hooks

    # Load project settings so a target repo's .claude config (if any) still
    # applies; the governor hook is injected via `hooks`, not settings.
    kwargs["setting_sources"] = ["project"]
    return kwargs


def exit_reason_from_envelope(subtype: str, is_error: bool) -> ExitReason:
    """Pure: SDK ResultMessage (subtype, is_error) → why the session ended.

    Read off the ENVELOPE, like every other decision here — never from prose or an
    exit code. The one case worth singling out is budget: the SDK reports it as
    just another `is_error` subtype, but a spend ceiling is an operator-knob
    outcome, not a failure of the agent's reasoning, and the cost ledger needs to
    tell them apart. Everything else that errored is AGENT_ERROR: the agent was
    alive and answering, so it made an attempt (contrast `infra_death`)."""
    st = (subtype or "").lower()
    if st == "success" and not is_error:
        return ExitReason.OK
    if "budget" in st:
        return ExitReason.BUDGET_EXHAUSTED
    if is_error:
        return ExitReason.AGENT_ERROR
    return ExitReason.OK


def result_to_agent_result(msg: Any) -> AgentResult:
    """Pure: SDK ResultMessage → domain AgentResult. Duck-typed (reads attrs) so
    a test can pass a stand-in object. Mirrors claude_outcome / save_session:
    is_error from the envelope, status/summary/needs from structured_output."""
    structured = getattr(msg, "structured_output", None) or {}
    status = structured.get("status", "") if isinstance(structured, dict) else ""
    # On an errored envelope with no structured status, surface the SDK's own error
    # kind (subtype, e.g. "error_max_turns" / "error_during_execution") as the
    # status so the daemon log distinguishes a turn-limit abort from a budget abort
    # from a transport death — otherwise every failure reads as a bare "error".
    if not status and bool(getattr(msg, "is_error", True)):
        status = str(getattr(msg, "subtype", "") or "error")
    summary = ""
    needs = ""
    if isinstance(structured, dict):
        summary = structured.get("summary", "") or (getattr(msg, "result", "") or "")
        needs = structured.get("needs", "")
    else:
        summary = getattr(msg, "result", "") or ""
    is_error = bool(getattr(msg, "is_error", True)) or status == "error"
    # Raw usage block, verbatim: it carries cache_read/cache_creation token counts
    # that no other field exposes, and a subscription-plan run reports
    # total_cost_usd == 0.00 — tokens are the only cost substrate that survives.
    usage = getattr(msg, "usage", None)
    return AgentResult(
        is_error=is_error,
        status=status,
        # Captured unconditionally — NOT under `if not status` like the fallback
        # above. When the agent declares its own status the envelope's error kind
        # would otherwise be thrown away, which is exactly what made #501's
        # is_error=True / status="success" run undiagnosable.
        envelope_subtype=str(getattr(msg, "subtype", "") or ""),
        summary=summary,
        needs=needs,
        cost_usd=float(getattr(msg, "total_cost_usd", 0.0) or 0.0),
        session_id=getattr(msg, "session_id", "") or "",
        structured=structured if isinstance(structured, dict) else None,
        exit_reason=exit_reason_from_envelope(
            str(getattr(msg, "subtype", "") or ""), is_error
        ),
        usage=usage if isinstance(usage, dict) else None,
    )


def _short(value: Any, limit: int = 200) -> str:
    """One-line, length-bounded repr. A diagnostic that floods the log is a
    diagnostic operators disable."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def error_forensics(msg: Any) -> str:
    """WHY an errored ResultMessage errored, as one grep-able line.

    The SDK envelope carries far more than we map onto AgentResult:
    `errors`, `permission_denials` (the governor blocking the agent in a loop —
    invisible everywhere else), `num_turns` (a max-turns wall), `stop_reason`,
    `terminal_reason`, `api_error_status`, and the model's own `result` text.
    All of it was being discarded, so a failed run left `exit_reason=agent_error`
    and nothing else. Duck-typed and exception-free: a diagnostic must never add
    a failure mode to the path it is diagnosing.
    """
    parts: list[str] = []
    try:
        for name in ("subtype", "stop_reason", "terminal_reason",
                     "api_error_status", "num_turns", "duration_ms"):
            value = getattr(msg, name, None)
            if value not in (None, "", 0):
                parts.append(f"{name}={_short(value, 80)}")
        errors = getattr(msg, "errors", None)
        if errors:
            parts.append(f"errors={_short(errors, 400)}")
        denials = getattr(msg, "permission_denials", None)
        if denials:
            # The count matters as much as the content: one denial is a blocked
            # command, forty is an agent wedged against the governor.
            try:
                count = len(denials)
            except TypeError:
                count = "?"
            parts.append(f"permission_denials={count} {_short(denials, 400)}")
        text = (getattr(msg, "result", "") or "").strip()
        if text:
            parts.append(f"result={_short(text, 500)}")
    except Exception as exc:  # noqa: BLE001 — diagnostics never raise
        parts.append(f"(forensics failed: {exc})")
    return "; ".join(parts) or "envelope carried no diagnostic fields"


def merge_usage(acc: dict | None, raw: Any) -> dict | None:
    """Accumulate one SDK per-turn usage block into a running total.

    `AssistantMessage.usage` is the underlying API call's OWN usage block
    (SDK message_parser: `usage=data["message"].get("usage")`), i.e. per turn —
    so summing across turns gives the run's BILLED totals, since every call bills
    its full input including cache reads. That distinction matters: if the field
    were cumulative, summing would overcount by roughly the turn count.

    Exists so an ABORTED run still knows what it spent. Cost only ever arrives on
    the final ResultMessage, so a wall-cap/idle/dropped-result abort used to
    report cost_usd=0.0 AND usage=None — a 906s session with four commits
    ledgered as $0.00, invisible to both spend caps. Tokens are the only substrate
    that survives an abort.

    Returns None until something is seen, keeping "no usage reported" distinct
    from "zero tokens".
    """
    if not isinstance(raw, dict):
        return acc
    out = dict(acc or {})
    for k, v in raw.items():
        # bool is an int subclass; a flag is not a token count.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out[k] = out.get(k, 0) + v
    return out


def total_stream_tokens(usage: dict) -> int:
    """Sum of every numeric counter in a usage block — same contract as
    journal.total_tokens, local so the runner keeps its no-journal-import
    layering. Used only by the calibration log line."""
    return sum(
        int(v) for v in usage.values()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )


def _trace_message(msg: Any) -> str:
    """One-line summary of an SDK stream message for debug tracing. Duck-typed so
    a test can pass a stand-in; never raises (diagnostics must not add failure
    modes). Surfaces tool_use blocks (name + a short command/path) so the LAST
    action before a transport death is visible in the log."""
    try:
        kind = type(msg).__name__
        blocks = getattr(msg, "content", None)
        if isinstance(blocks, list):
            parts = []
            for b in blocks:
                name = getattr(b, "name", None)
                if name:  # a ToolUseBlock
                    ti = getattr(b, "input", None)
                    hint = ""
                    if isinstance(ti, dict):
                        raw = str(ti.get("command") or ti.get("file_path") or "")
                        hint = raw.strip().splitlines()[0][:80] if raw.strip() else ""
                    parts.append(f"{name}({hint})" if hint else name)
            if parts:
                return f"{kind} → {', '.join(parts)}"
        return kind
    except Exception:
        return "??"


async def _aclose_quietly(agen: Any) -> None:
    """Best-effort close of the SDK async generator after an idle-timeout abort —
    tears down the underlying query subprocess. Never raises (we're already in an
    error path; a close failure must not mask the timeout)."""
    try:
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            await aclose()
    except Exception:
        pass


class AgentRunner(AgentRuntime):
    """Runs headless agent tasks over the SDK. One instance per process; safe to
    call `run` concurrently for distinct `cwd`s (each query is its own SDK
    subprocess — see REFACTOR-SDK.md §15). Implements the domain AgentRuntime
    port (the Claude-SDK realisation; CodexRunner is the other)."""

    def __init__(
        self,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        hooks: dict | None = None,
        log: Any = None,
        debug: bool = False,
        idle_timeout: float | None = None,
        wall_clock: Callable[[], float] | None = None,
        poll_slice: float = _WATCHDOG_POLL_SLICE,
        max_runtime: float | None = None,
    ):
        self.permission_mode = permission_mode
        # Pre-built {"PreToolUse": [HookMatcher(...)]} from the governor adapter.
        self.hooks = hooks
        # Diagnostics: `log` is a str->None sink (stderr in prod). `debug` turns on
        # per-message tracing so a mid-session transport death ("Stream closed")
        # shows the LAST tool the agent ran before it — the blind spot today.
        self._log = log or (lambda _m: None)
        self._debug = debug
        # Idle watchdog: max seconds BETWEEN stream events before we call it a hang
        # (not a total-runtime cap — a hard task streams events steadily and keeps
        # resetting the clock; only genuine silence trips it). None disables. See
        # AGENT_IDLE_TIMEOUT_SECONDS; default derived from the healthy #56 run where
        # the worst legit inter-event gap was ~124s.
        self._idle_timeout = idle_timeout
        # The idle window is measured against the WALL clock (time.time), NOT the
        # asyncio/monotonic clock. On macOS the monotonic clock (mach_absolute_time)
        # FREEZES during system sleep, so a single `wait_for(timeout=idle_timeout)`
        # spanning a suspend does not count the suspended time — after the laptop
        # wakes, a stream whose socket died during sleep would need a WHOLE fresh
        # idle window (or hang forever on a half-open socket) before tripping. That
        # was the observed post-sleep review wedge (Myapp #345). Polling the wall clock
        # in short slices instead makes a sleep-spanning gap visible on the first
        # slice after wake → prompt recovery, and lets the Mac sleep freely.
        self._wall_clock = wall_clock or _default_wall_clock
        self._poll_slice = poll_slice
        # Total-runtime ceiling — the cap the idle watchdog CANNOT provide. A stream
        # left "limping" by a suspend/resume (FAILURE-MODES #60) emits an event every
        # few minutes: each one resets the idle clock, so a session burned 58 minutes
        # on 3.6s of CPU and never tripped. Only elapsed wall time bounds that shape.
        # None disables. See AGENT_MAX_RUNTIME_SECONDS.
        self._max_runtime = max_runtime

    def _slice_timeout(self) -> float:
        """How long to block on one observation slice. The poll slice, floored by
        whichever caps are active so a cap SHORTER than the slice still trips on
        time (a 0.2s cap must not wait 30s to notice)."""
        caps = [self._poll_slice]
        if self._idle_timeout is not None:
            caps.append(self._idle_timeout)
        if self._max_runtime is not None:
            caps.append(self._max_runtime)
        return min(caps)

    def _wall_timeout(
        self, elapsed: float, last_trace: str, usage: dict | None = None,
        msgs: int = 0,
    ) -> AgentResult:
        """Log + build the terminal result for a tripped total-runtime cap.

        `usage` carries whatever the stream reported before the abort, so the run
        is costed rather than ledgered as $0.00 (see `merge_usage`). `msgs` is how
        many stream messages arrived first — it distinguishes "wedged early" from
        "worked the whole time and ran out of road", which need opposite responses
        (investigate vs. raise the cap / trim the task)."""
        self._log(
            f"sdk: WALL — total runtime {elapsed:.0f}s exceeded "
            f"max {self._max_runtime:.0f}s; aborting after {msgs} stream "
            f"messages (last: {last_trace or 'none'})"
        )
        return AgentResult(
            is_error=True, status="timeout",
            exit_reason=ExitReason.WALL_TIMEOUT,
            summary=f"agent exceeded max runtime {self._max_runtime:.0f}s",
            usage=usage,
            stream_usage=usage,
        )

    async def run(self, task: AgentTask) -> AgentResult:
        # Lazy import: keeps the SDK out of domain-only / unit-test paths.
        import asyncio

        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        kwargs = build_option_kwargs(task, self.permission_mode, self.hooks)
        # Materialise the SDK-free `agents` dict-spec into AgentDefinition objects
        # (build_option_kwargs stays SDK-free for unit tests; the SDK type is
        # applied only here, at the edge that already imports the SDK). Imported
        # inside the branch so a task with no subagents never needs the symbol —
        # keeps the stubbed-SDK unit tests (which omit AgentDefinition) working.
        if kwargs.get("agents"):
            from claude_agent_sdk import AgentDefinition

            kwargs["agents"] = {
                name: AgentDefinition(**spec) for name, spec in kwargs["agents"].items()
            }
        # Capture the bundled CLI's OWN stderr. The SDK otherwise swallows it, so a
        # gateway-side failure (an HTTP status, an SSE `response.failed`, a reconnect
        # loop) that kills the result-delivery turn is invisible — we see only the
        # symptom ("no ResultMessage"), never the cause. Route each stderr line into
        # our log (tagged) so the real reason sits next to the $0.00 error. Only
        # wired when the SDK build exposes the `stderr` option (older builds don't).
        if "stderr" in _option_field_names():
            def _cli_stderr(line: str) -> None:
                s = (line or "").rstrip()
                if s:
                    self._log(f"sdk-stderr: {s[:500]}")
            kwargs["stderr"] = _cli_stderr

        options = ClaudeAgentOptions(**kwargs)
        final: Any = None
        last_trace = ""  # last thing we saw before a transport death (for the log)
        # Diagnostics for the clean-end-no-result drop (gateway fails at result
        # delivery): the stream ends via StopAsyncIteration WITHOUT ever yielding a
        # ResultMessage, so the "transport died" exception branch never fires and
        # nothing explains the $0.00 error. Track how far we got — total messages
        # seen and the KIND of the last one — so the log distinguishes "died on the
        # first event" (never started) from "died mid-task after N tool turns"
        # (gateway dropped the final result turn). ALWAYS logged (not debug-gated).
        msg_count = 0
        last_kind = "none"
        last_msg: Any = None  # the actual last message object (for drop-path forensics)
        terminal_seen = ""    # any terminal_reason observed across the stream
        # Running token total, so an abort still reports what it spent. Only the
        # final ResultMessage carries dollars, so without this every wall-cap /
        # idle / dropped-result run ledgers $0.00 no matter how long it ran.
        usage_acc: dict | None = None
        # Wall-cap baseline. Read the clock ONLY when the cap is armed: the clock is
        # injectable and a spurious read would shift a test's fake sequence, and an
        # unarmed cap has no baseline to keep.
        started_at = self._wall_clock() if self._max_runtime is not None else 0.0
        agen = query(prompt=task.prompt, options=options).__aiter__()
        try:
            while True:
                # Total-runtime cap, checked BETWEEN receives as well as inside the
                # slice loop below. A stream delivering events FASTER than one poll
                # slice never expires a slice, so the in-slice check alone would
                # leave a chatty runaway session unbounded — exactly the spend hole
                # the cap exists to close. Between receives there is no in-flight
                # task to cancel; just close the generator (tears down the query
                # subprocess) and honour any result we already captured.
                if self._max_runtime is not None:
                    elapsed = self._wall_clock() - started_at
                    if elapsed >= self._max_runtime:
                        await _aclose_quietly(agen)
                        if final is None:
                            return self._wall_timeout(
                                elapsed, last_trace, usage_acc, msg_count
                            )
                        self._log(
                            f"sdk: WALL — {elapsed:.0f}s exceeded max "
                            f"{self._max_runtime:.0f}s, but a ResultMessage was "
                            "already captured; honouring it"
                        )
                        break
                # Bound the wait for the NEXT event, not the whole run. A hard task
                # emits messages continuously so the idle clock resets every few
                # seconds; only a true hang (no event at all) exceeds the window.
                #
                # The window is measured against the WALL clock in short poll slices,
                # not a single monotonic-clock wait_for: macOS freezes the monotonic
                # clock during system sleep, so a one-shot wait spanning a suspend
                # would not count the suspended time and the post-wake wedge (a review
                # whose socket died mid-sleep) could hang for a full extra window. By
                # re-checking wall-clock elapsed each slice, a sleep-spanning gap trips
                # on the first slice after wake — and the laptop is free to sleep.
                message: Any = _NO_MESSAGE
                stream_ended = False
                if self._idle_timeout is not None or self._max_runtime is not None:
                    # ONE observation loop serves BOTH caps. They are checked
                    # independently (each only when armed) but share the slice
                    # machinery — arming only the wall cap must not fall through to
                    # the unbounded `await __anext__()` branch below, or the cap
                    # would silently never fire.
                    #
                    # Poll the NEXT event without cancelling the in-flight receive.
                    #
                    # The previous form — asyncio.wait_for(agen.__anext__(), slice) —
                    # cancelled the pending __anext__() on every slice timeout. The SDK
                    # generator awaits its transport read inside __anext__(); cancelling
                    # that mid-read closes the stream, so the NEXT __anext__() returns
                    # StopAsyncIteration and the whole run ends with NO ResultMessage
                    # ($0.00 "result-delivery drop"). Any run where the model pauses
                    # longer than one slice between messages (routine for a reviewer
                    # thinking over a large diff) hit this — the persistent ~all-review
                    # failure. See the wait_for-cancel repro in the drop investigation.
                    #
                    # Fix: start the __anext__() ONCE as a task and OBSERVE it across
                    # slices with asyncio.wait (which does NOT cancel on timeout). The
                    # receive runs to completion regardless of how many slices elapse;
                    # slices exist only to re-check the wall-clock idle budget (so a
                    # true hang, or a sleep-spanning gap on macOS where the monotonic
                    # clock freezes, still trips the idle timeout and frees the laptop).
                    last_event = self._wall_clock()
                    pending = asyncio.ensure_future(agen.__anext__())
                    while True:
                        done, _ = await asyncio.wait(
                            {pending}, timeout=self._slice_timeout()
                        )
                        if done:
                            try:
                                message = pending.result()
                            except StopAsyncIteration:
                                stream_ended = True
                            break  # got an event (or the stream ended)
                        # ONE clock read per slice, shared by both checks — the clock
                        # is injectable and extra reads would advance a fake sequence.
                        now = self._wall_clock()
                        idle = now - last_event
                        if self._idle_timeout is not None and idle >= self._idle_timeout:
                            self._log(
                                f"sdk: IDLE — no stream event for {idle:.0f}s "
                                f"(>{self._idle_timeout:.0f}s); treating as hung "
                                f"(last: {last_trace or 'none'})"
                            )
                            pending.cancel()
                            await _aclose_quietly(agen)
                            if final is None:
                                return AgentResult(
                                    is_error=True, status="timeout",
                                    exit_reason=ExitReason.IDLE_TIMEOUT,
                                    summary=f"agent idle >{self._idle_timeout:.0f}s "
                                            "(no stream event)",
                                    usage=usage_acc,
                                    stream_usage=usage_acc,
                                )
                            stream_ended = True  # had a result; honour it below
                            break
                        elapsed = now - started_at
                        if self._max_runtime is not None and elapsed >= self._max_runtime:
                            # The limping-stream cap: events keep arriving (so the idle
                            # watchdog never fires) but the session has run past any
                            # plausible task length. Tear down exactly like the idle
                            # path — a capped session must not leak its subprocess.
                            pending.cancel()
                            await _aclose_quietly(agen)
                            if final is None:
                                # usage_acc MUST ride along here exactly as on the
                                # between-receives wall path above: this branch used
                                # to omit it, so a limping-stream wall abort ledgered
                                # $0.00 — the precise hole merge_usage exists to close.
                                return self._wall_timeout(
                                    elapsed, last_trace, usage_acc, msg_count
                                )
                            self._log(
                                f"sdk: WALL — {elapsed:.0f}s exceeded max "
                                f"{self._max_runtime:.0f}s, but a ResultMessage was "
                                "already captured; honouring it"
                            )
                            stream_ended = True  # had a result; honour it below
                            break
                        # slice elapsed but still inside both windows — keep observing
                        # the SAME pending receive (do NOT cancel/restart it)
                    if stream_ended:
                        break
                else:
                    try:
                        message = await agen.__anext__()
                    except StopAsyncIteration:
                        break
                msg_count += 1
                last_kind = type(message).__name__
                last_msg = message
                usage_acc = merge_usage(usage_acc, getattr(message, "usage", None))
                # Capture any terminal_reason the SDK attaches to ANY message — a
                # background-requested turn end carries it on a non-Result message,
                # so it never reaches the ResultMessage branch. This is the forensic
                # for the persistent no-ResultMessage drop on subagent-spawning runs.
                tr = getattr(message, "terminal_reason", None)
                if tr:
                    terminal_seen = str(tr)
                # ALWAYS maintained, not debug-gated: this string is what the
                # wall/idle abort lines print as "(last: …)". Gated behind
                # _debug it was never assigned in production, so every abort
                # reported "(last: none)" — at the exact moment "what was the
                # agent doing?" is the question (observed: #506's wall abort,
                # 2026-07-26). Only the per-message log line stays debug-only.
                last_trace = _trace_message(message)
                if self._debug:
                    self._log(f"sdk: {last_trace}")
                if isinstance(message, ResultMessage):
                    # Skip background-task follow-up results. When a background task
                    # finishes, the SDK injects a synthetic follow-up turn whose
                    # ResultMessage carries origin={"kind":"task-notification"} — it
                    # does NOT answer our prompt, and letting it overwrite `final`
                    # would clobber (or stand in for) the real verdict. Keep only the
                    # result for our own turn. (Background Bash is separately blocked
                    # at the governor hook; this is defense-in-depth for any other
                    # background path the preset may open.) Duck-typed: older SDK
                    # builds have no `origin` → getattr None → treated as ours.
                    origin = getattr(message, "origin", None)
                    if isinstance(origin, dict) and origin.get("kind") == "task-notification":
                        if self._debug:
                            self._log("sdk: skipping task-notification follow-up result")
                        continue
                    final = message
        except Exception as exc:
            # The SDK raises the CLI's trailing ProcessError when Claude Code
            # exits non-zero AFTER delivering its ResultMessage — e.g. an
            # is_error result, or a benign post-`gh pr create` non-zero exit.
            # That exit code is for shell consumers; we read the ENVELOPE, never
            # the exit code (same contract as bash claude_outcome). So if we
            # already captured a ResultMessage, honour it; only a crash BEFORE
            # any result is a genuine transport failure.
            if final is None:
                # ALWAYS log a pre-result transport death (not just under debug):
                # this is the "Stream closed" wedge, and without the exception
                # type + last-seen message it was undiagnosable from the log.
                self._log(
                    f"sdk: transport died before any ResultMessage — "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                    + (f" (last: {last_trace})" if last_trace else "")
                )
                # This raise IS the TRANSPORT_DEATH path for the SDK runtime —
                # deliberately NOT converted into an AgentResult(exit_reason=
                # TRANSPORT_DEATH). Callers depend on the exception for crash
                # isolation: Daemon.tick catches it, so the issue keeps ai-working
                # and the reaper requeues it — precisely the settle path
                # infra_death() produces for a returned result. Returning instead
                # would run the follow-up tail on a session that never happened.
                # The enum member exists for the runtimes that CAN'T raise
                # (CodexRunner reads an exit code) and for future supervision.
                raise
        if final is None:
            # No ResultMessage emitted (transport died) → surface as an error,
            # never silently as success. Matches the bash rc!=0 path.
            #
            # This is the clean-end drop (stream StopAsyncIteration without a
            # ResultMessage), NOT the exception path above — so log WHY here or the
            # $0.00 error is opaque. `msg_count`/`last_kind` distinguish a stream
            # that never started (0 msgs → auth/launch/gateway-reject) from one that
            # ran N turns then lost only the final result turn (mid-task gateway
            # drop). ALWAYS logged (this is the diagnostic for the persistent
            # result-delivery failure).
            # Forensic dump of the LAST message: its public non-callable attrs, so a
            # terminal_reason / subtype / num_turns / origin that explains the drop is
            # captured instead of guessed. Truncated per-value to keep the log sane.
            forensic = ""
            if last_msg is not None:
                fields = {}
                for a in dir(last_msg):
                    if a.startswith("_"):
                        continue
                    try:
                        v = getattr(last_msg, a)
                    except Exception:
                        continue
                    if callable(v):
                        continue
                    fields[a] = repr(v)[:120]
                forensic = f"; last_msg_fields={fields}"
            self._log(
                f"sdk: stream ENDED cleanly with NO ResultMessage after "
                f"{msg_count} message(s); last kind={last_kind}"
                + (f", terminal_reason={terminal_seen}" if terminal_seen else "")
                + (f", last={last_trace}" if last_trace else "")
                + " — result-delivery drop (no exception raised)"
                + forensic
            )
            return AgentResult(
                is_error=True,
                summary="no ResultMessage from query()",
                exit_reason=ExitReason.NO_RESULT,
                usage=usage_acc,
                stream_usage=usage_acc,
            )
        result = result_to_agent_result(final)
        # Attach the stream sum to the COMPLETED result too: journaled beside the
        # ResultMessage total, each completed run becomes a calibration pair, and
        # the estimator learns its $/token rate in stream units — the only units
        # an aborted run can ever be priced in (see observed_cost_per_token).
        if usage_acc:
            result = dataclasses.replace(result, stream_usage=dict(usage_acc))
        # Calibration line for the abort-cost estimator: on a COMPLETED run both
        # totals exist — the ResultMessage's own cumulative usage (authoritative,
        # what the ledger keeps) and merge_usage's stream-summed accumulator (all
        # an ABORTED run ever has). Logging the pair is the diagnostic for how far
        # estimates on aborted runs can be trusted, per counter. Suspected skew:
        # per-message usage blocks appear to carry message_start-style output
        # placeholders — #506's 2707s wall abort accumulated output_tokens=4764
        # across ~15 commits of code, vs 55-66k on comparable completed runs.
        if usage_acc and isinstance(result.usage, dict):
            acc_t, res_t = total_stream_tokens(usage_acc), total_stream_tokens(result.usage)
            self._log(
                f"sdk: usage calibration — stream-accumulated {acc_t} tokens "
                f"(out={usage_acc.get('output_tokens', 0)}) vs result "
                f"{res_t} (out={result.usage.get('output_tokens', 0)})"
            )
        return result
        # An errored envelope gets its forensics logged AT THE MOMENT OF FAILURE,
        # while the raw ResultMessage is still in hand. Everything below is
        # dropped by the AgentResult mapping, so without this the daemon log says
        # only "done (status=…)" and the journal only "exit_reason=agent_error" —
        # which is what made #501's 290s / $1.93 / zero-commit failure impossible
        # to diagnose after the fact.
        if result.is_error:
            self._log(f"sdk: ERRORED — {error_forensics(final)}")
        return result
