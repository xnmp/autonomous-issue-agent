"""The role's plain JSONL journal and the runtime decorator that feeds it.

One human-readable JSON object per line: a leg's start and end, and operator
interventions. Telemetry only: no decision reads it back. SQLite holds legs,
leases and money; metrics come from those tables plus GitHub (Q15).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..domain.contracts import Money
from ..domain.outcome import AgentResult, ExitReason, deterministic_death, pre_admission_refusal
from ..domain.run_usage import EstimatedRunUsage, MeasuredRunUsage
from .runtime_plan import AgentRuntime, ContextualRuntime, RunIdentityRuntime, RuntimePlan

# The SDK usage counters worth keeping. `cost_usd` is 0.00 on a subscription
# and absent on Codex; tokens always exist, so they are captured verbatim.
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_BLOCKED_TEXT_LIMIT = 1_000


def _bounded(value: object) -> str | None:
    return str(value or "").strip()[:_BLOCKED_TEXT_LIMIT] or None


@dataclass(frozen=True)
class Journal:
    """Append one JSON line per record. Best effort: a failed write is
    reported on stderr and dropped; a log line never stops orchestration."""

    path: str
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def emit(self, event: str, fields: Mapping[str, object]) -> None:
        try:
            stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            line = json.dumps({"ts": stamp, "event": event, **fields}, default=str, sort_keys=True)
            with self._lock:
                if parent := os.path.dirname(self.path):
                    os.makedirs(parent, mode=0o700, exist_ok=True)
                # 0600: agent summaries and error text from possibly private repos.
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 - a log line never fails a tick
            sys.stderr.write(f"journal: dropped {event} ({exc})\n")


@dataclass(frozen=True)
class RunContext:
    """Which dispatch a leg belongs to, set once per dispatch. `dispatch_id`
    is random so it survives a process death."""

    role: str = ""
    kind: str = ""
    number: int = 0
    issue: int | None = None
    dispatch_id: str = ""

    def correlation(self, run_id: str, leg_id: str, session_id: str | None = None) -> dict[str, object]:
        # The kind decides whether `number` is a PR (keifu 2026-08-03: a REVIEW
        # leg knows both numbers). Issues and PRs share one number sequence.
        pr = self.number if self.number > 0 and self.kind != "RESOLVE" and self.number != self.issue else None
        values = {"issue": self.issue, "pr": pr, "dispatch": self.dispatch_id or None,
                  "run_id": run_id, "leg_id": leg_id, "session_id": session_id}
        return {key: value for key, value in values.items() if value is not None}


class JournalingRuntime:
    """An `AgentRuntime` decorator: journals each leg's start before the await
    and its end after it (including when the await raises)."""

    def __init__(self, inner: AgentRuntime, journal: Journal, repo: str = "", role: str = "",
                 runtime: str = "", sandbox: Callable[[], str] | None = None):
        self.inner = inner
        # What the leg starts under ("bwrap 0.13.0" or "unavailable: ..."), #413.
        self.sandbox = sandbox
        self.journal = journal
        self.repo = repo
        self.role = role
        self.runtime = runtime
        self._context = contextvars.ContextVar(f"journal_run_context_{id(self)}", default=RunContext(role=role))

    def __getattr__(self, name: str):
        """The wrapped runtime's operational surface (preflight, identity)."""
        return getattr(object.__getattribute__(self, "inner"), name)

    @property
    def context(self) -> RunContext:
        return self._context.get()

    def set_context(self, role: str, kind: str, number: int, issue: int | None,
                    claim_target: int | None = None, *, head: str = "", resumed: bool = False,
                    tier: str = "") -> None:
        self._context.set(RunContext(role, kind, number, issue, uuid.uuid4().hex[:12]))
        if isinstance(self.inner, ContextualRuntime):
            self.inner.set_context(role, kind, number, issue, claim_target=claim_target, head=head,
                                   resumed=resumed, tier=tier)

    async def run(self, task: RuntimePlan) -> AgentResult:
        """Review C3: a runner that raises on a pre-result transport death used
        to skip every record while the item was re-dispatched each poll. So
        `agent_run_start` comes first (the only trace if this process dies
        mid-run), and the raise path writes its own end line before the
        exception continues upward unchanged."""
        ctx = self.context
        run_id, leg_id = uuid.uuid4().hex, uuid.uuid4().hex
        try:
            self.journal.emit("agent_run_start", {**ctx.correlation(run_id, leg_id), "model": task.model or "",
                                                  "kind": ctx.kind, "role": ctx.role, "number": ctx.number,
                                                  **({"sandbox": self.sandbox()} if self.sandbox else {})})
        except Exception as exc:  # noqa: BLE001 — never fail a run over its receipt
            sys.stderr.write(f"journal: could not record agent_run_start ({exc})\n")
        if isinstance(self.inner, RunIdentityRuntime):
            self.inner.set_run_identity(run_id, leg_id)
        t0 = time.monotonic()
        try:
            result = await self.inner.run(task)
        except BaseException as exc:  # noqa: BLE001 — recorded, never handled
            # Cancellation and Ctrl-C settle the started leg too, as an
            # operator cancel instead of an infrastructure death.
            cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit))
            # A transport death the INPUT causes gets its own shape (#13).
            det = None if cancelled else deterministic_death(
                f"{type(exc).__name__}: {exc}", str(getattr(exc, "harness_last_trace", "") or ""))
            terminal = AgentResult(
                is_error=True, status="cancelled" if cancelled else "raised", cost_usd=0.0,
                model=str(getattr(exc, "harness_resolved_model", "") or ""),
                session_id=str(getattr(exc, "harness_session_id", "") or ""),
                exit_reason=ExitReason.OPERATOR_CANCELLED if cancelled else ExitReason.TRANSPORT_DEATH)
            self._record(task, terminal, time.monotonic() - t0, run_id, leg_id,
                         error=f"{type(exc).__name__}: {str(exc)[:200]}",
                         deterministic=det.signature if det else None, detail=(det.path or None) if det else None)
            raise
        self._record(task, result, time.monotonic() - t0, run_id, leg_id)
        return result

    def _record(self, task: RuntimePlan, result: AgentResult, duration_s: float, run_id: str, leg_id: str,
                **extra: object) -> None:
        try:
            ctx = self.context
            runtime = task.runtime.value if task.runtime is not None else self.runtime
            fields: dict[str, object] = {
                **ctx.correlation(run_id, leg_id, result.session_id or None),
                "status": result.status, "is_error": result.is_error,
                "exit_reason": result.exit_reason.value if result.exit_reason else None,
                "duration_s": max(0, int(round(duration_s * 1000))) / 1000,
                "model": task.model or result.model or "", "runtime": runtime,
                "usage": _usage(result.usage), "kind": ctx.kind,
                # Settlement prices from the control store; this is never pricing authority.
                "cost_usd": Money.from_major("USD", max(0.0, float(result.cost_usd or 0.0))).micros / 1_000_000,
                "role": ctx.role, "number": ctx.number,
                "envelope_subtype": result.envelope_subtype or None,
                # Stream-summed counters beside `usage`: a calibration pair.
                "usage_stream": _usage(result.stream_usage),
                "turns": result.num_turns,
                "needs": _bounded(result.needs) if result.status == "blocked" else None,
            }
            if result.status == "blocked" or pre_admission_refusal(result):
                fields["summary"] = _bounded(result.summary)
            if isinstance(result.run_usage, (MeasuredRunUsage, EstimatedRunUsage)):
                fields["usage_by_session"] = [
                    {"session_id": s.session_id, "model": s.model, "input_tokens": s.usage.input_tokens,
                     "output_tokens": s.usage.output_tokens, "cache_read_tokens": s.usage.cache_read_tokens,
                     "cache_write_tokens": s.usage.cache_write_tokens, "source": s.usage.source.value}
                    for s in result.run_usage.samples]
                if isinstance(result.run_usage, EstimatedRunUsage):
                    fields["usage_estimate_gap"] = result.run_usage.detail
            fields = {k: v for k, v in fields.items() if v is not None or k in ("usage", "exit_reason")}
            self.journal.emit("agent_run", {**fields, **extra})
        except Exception as exc:  # noqa: BLE001 — never fail a run over its receipt
            sys.stderr.write(f"journal: could not record agent_run ({exc})\n")


def _usage(raw) -> dict | None:
    """The kept counters; a non-dict (Codex reports none) stays None, so 'no
    usage' and 'zero tokens' remain distinct."""
    if not isinstance(raw, dict):
        return None
    normalized = {k: raw.get(k, 0) for k in _USAGE_KEYS}
    if "total_tokens" in raw:
        normalized["total_tokens"] = raw.get("total_tokens", 0)
    return normalized
