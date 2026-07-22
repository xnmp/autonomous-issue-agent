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

import time
from collections.abc import Callable
from typing import Any

from ..domain.agent_task import AgentRuntime, AgentTask
from ..domain.outcome import AgentResult

# Longest single slice we block waiting for the next stream event before we come
# up for air to re-check the WALL clock. Small enough that a sleep-spanning gap is
# noticed promptly on wake; large enough to add negligible overhead on a healthy
# steadily-streaming run. Only relevant when idle_timeout is set.
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
    return AgentResult(
        is_error=bool(getattr(msg, "is_error", True)) or status == "error",
        status=status,
        summary=summary,
        needs=needs,
        cost_usd=float(getattr(msg, "total_cost_usd", 0.0) or 0.0),
        session_id=getattr(msg, "session_id", "") or "",
        structured=structured if isinstance(structured, dict) else None,
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
        agen = query(prompt=task.prompt, options=options).__aiter__()
        try:
            while True:
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
                if self._idle_timeout is not None:
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
                            {pending},
                            timeout=min(self._poll_slice, self._idle_timeout),
                        )
                        if done:
                            try:
                                message = pending.result()
                            except StopAsyncIteration:
                                stream_ended = True
                            break  # got an event (or the stream ended)
                        idle = self._wall_clock() - last_event
                        if idle >= self._idle_timeout:
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
                                    summary=f"agent idle >{self._idle_timeout:.0f}s "
                                            "(no stream event)",
                                )
                            stream_ended = True  # had a result; honour it below
                            break
                        # slice elapsed but still inside the window — keep observing the
                        # SAME pending receive (do NOT cancel/restart it)
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
                # Capture any terminal_reason the SDK attaches to ANY message — a
                # background-requested turn end carries it on a non-Result message,
                # so it never reaches the ResultMessage branch. This is the forensic
                # for the persistent no-ResultMessage drop on subagent-spawning runs.
                tr = getattr(message, "terminal_reason", None)
                if tr:
                    terminal_seen = str(tr)
                if self._debug:
                    last_trace = _trace_message(message)
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
            return AgentResult(is_error=True, summary="no ResultMessage from query()")
        return result_to_agent_result(final)
