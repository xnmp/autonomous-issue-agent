"""The role's plain JSONL journal and the runtime decorator that feeds it.

One human-readable JSON object per line. Telemetry only: no decision reads it
back. SQLite holds runs, leases and money; metrics come from those tables plus
GitHub.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from .runtime_plan import ContextualRuntime, RunIdentityRuntime, RuntimePlan
from ..domain.contracts import Money
from ..domain.workflow_identity import WorkflowBinding
from ..domain.review_execution import ReviewChildIdentity
from ..domain.dashboard import ISSUE_NUMBERED
from ..domain.events import (
    ActionApplied,
    ActionFailed,
    ActionRetried,
    AgentRunCompleted,
    AgentRunStarted,
    BudgetPathEntered,
    Correlation,
    DaemonStateChanged,
    EscalationRaised,
    GithubUnavailable,
    HandoverWritten,
    LevelChanged,
    RoutingEscalated,
    StallDetected,
    TelemetryEvent,
    TelemetryWarning,
    TickGated,
)
from ..domain.outcome import pre_admission_refusal, AgentResult, ExitReason, deterministic_death
from ..domain.run_usage import MeasuredRunUsage, EstimatedRunUsage
from ..domain.ports import EventSink
from .handover import (
    HandoverScope,
    retire_delivered_handover,
    with_previous_handover,
    write_exhaustion_handover,
)

# The four SDK usage counters worth keeping. `cost_usd` is the gateway's dollar
# figure, which is 0.00 on a subscription plan and absent on Codex — tokens are
# the substrate that always exists, so they are captured verbatim.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_BLOCKED_TEXT_LIMIT = 1_000


def _bounded_blocked_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:_BLOCKED_TEXT_LIMIT] or None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now_fn=_now) -> str:
    """UTC ISO-8601 at SECONDS resolution. Sub-second precision buys nothing for
    a poll loop that ticks every 20s, and a fixed-width stamp keeps the file
    greppable by date prefix (`grep '"ts": "2026-07-25'`)."""
    return now_fn().replace(microsecond=0).isoformat()


def record_fields(event: TelemetryEvent) -> tuple[str, dict[str, object]]:
    """One typed record as its journal line's event name and flat fields."""

    payload = event.payload
    correlation = event.correlation
    common: dict[str, object] = {
        key: value
        for key, value in {
            "issue": correlation.issue,
            "pr": correlation.pr,
            "dispatch": correlation.dispatch_id,
            "run_id": correlation.run_id,
            "leg_id": correlation.leg_id,
            "session_id": correlation.session_id,
        }.items()
        if value is not None
    }
    if isinstance(payload, AgentRunStarted):
        return "agent_run_start", {
            **common,
            "model": payload.model,
            "kind": payload.kind,
            **dict(payload.attributes or {}),
        }
    if isinstance(payload, AgentRunCompleted):
        result = {
            **common,
            "status": payload.status,
            "is_error": payload.is_error,
            "exit_reason": payload.exit_reason,
            "duration_s": payload.duration_ms / 1000,
            "model": payload.model,
            "runtime": payload.runtime,
            "usage": dict(payload.usage) if payload.usage is not None else None,
            "kind": payload.kind,
            **dict(payload.attributes or {}),
        }
        if payload.summary is not None:
            result["summary"] = payload.summary
        if payload.cost_micros is not None:
            result["cost_usd"] = payload.cost_micros / 1_000_000
        return "agent_run", result
    if isinstance(payload, ActionApplied):
        structured = dict(payload.attributes or {})
        structured.setdefault("type", payload.kind)
        structured.setdefault("target", payload.target.upper())
        if payload.number is not None:
            structured.setdefault("number", payload.number)
        return "action", {
            **common,
            "desc": payload.display,
            "action": structured,
            **{
                key: value
                for key, value in structured.items()
                if key in {"attempts", "replayed"}
            },
        }
    if isinstance(payload, ActionFailed):
        return "action_failed", {
            **common,
            "desc": payload.display,
            "error": payload.error,
            "pending": payload.disposition == "retryable_failure",
            "attempts": payload.attempts,
        }
    if isinstance(payload, ActionRetried):
        return "action_retry", {
            **common,
            "desc": payload.display,
            "attempt": payload.attempt,
            "delay_seconds": payload.delay_ms / 1000,
            "error": payload.error,
        }
    if isinstance(payload, EscalationRaised):
        return "escalation", {
            "target": payload.target,
            "number": payload.number,
        }
    if isinstance(payload, LevelChanged):
        issue = correlation.issue
        attributes = dict(payload.attributes or {})
        return "deferred", {
            **common,
            "state": payload.state,
            "reason": payload.reason,
            **(
                {}
                if "blocker_pr" in attributes or "blocker_issue" in attributes
                else {"blocker": payload.state_key.rsplit(":", 1)[-1] or None}
            ),
            **attributes,
            **({"issue": issue} if issue is not None else {}),
        }
    if isinstance(payload, TickGated):
        return "tick_gated", {**common, "reason": payload.reason}
    if isinstance(payload, GithubUnavailable):
        return "gh_unavailable", {
            **common,
            "kind": payload.kind,
            "detail": payload.detail,
            "retry_after_s": (
                payload.retry_after_ms / 1000
                if payload.retry_after_ms is not None
                else None
            ),
        }
    if isinstance(payload, StallDetected):
        return "stall", {
            **common,
            "stall_id": payload.stall_id,
            "started_at": payload.started_at,
            "woke_at": payload.woke_at,
            "suspended_seconds": payload.suspended_ms / 1000,
            "elapsed_seconds": payload.elapsed_ms / 1000,
            "stall_clock_offset_seconds": payload.clock_offset_ms / 1000,
        }
    if isinstance(payload, HandoverWritten):
        return "handover_written", {
            **common,
            "trigger": payload.trigger,
            "bytes": payload.bytes_written,
        }
    if isinstance(payload, RoutingEscalated):
        return "route_escalation", {
            **common,
            "leg": payload.leg,
            "from_tier": payload.from_tier,
            "to_tier": payload.to_tier,
            "from_effort": payload.from_effort,
            "to_effort": payload.to_effort,
            "model": payload.model,
            "ceiling": payload.ceiling,
            **dict(payload.evidence),
        }
    if isinstance(payload, BudgetPathEntered):
        return "budget_path", {
            **common,
            "leg": payload.leg,
            "exit_reason": payload.exit_reason,
        }
    if isinstance(payload, DaemonStateChanged):
        return "daemon_state", {
            **common,
            "previous": payload.previous,
            "state": payload.state,
            "reason": payload.reason,
        }
    if isinstance(payload, TelemetryWarning):
        name = payload.code if payload.code in {
            "action_quarantined",
            "action_reconcile_skipped",
            "branch_refresh",
            "ci_cancel",
            "ci_rerun",
            "ci_stall_step",
        } else "telemetry_warning"
        return name, {
            **common,
            **dict(payload.details or {}),
            "warning": payload.warning,
        }
    return "telemetry_warning", {
        **common,
        "warning": type(payload).__name__,
    }


@dataclass(frozen=True)
class Journal:
    """Append one JSON line per record to the role's journal file.

    Best effort: a write failure is reported on stderr and dropped, because a
    log line can never be what stops orchestration.
    """

    path: str
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def emit(self, event: TelemetryEvent) -> None:
        try:
            name, fields = record_fields(event)
            line = json.dumps(
                {"ts": _stamp(), "event": name, **fields},
                default=str,
                sort_keys=True,
            )
            with self._lock:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, mode=0o700, exist_ok=True)
                # 0600: the journal carries agent summaries and error text
                # from possibly private repos.
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                    0o600,
                )
                with os.fdopen(fd, "a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 - a log line never fails a tick
            sys.stderr.write(
                f"journal: dropped {type(event.payload).__name__} ({exc})\n"
            )


def default_ledger_path() -> str:
    """The GLOBAL spend ledger every role on this machine appends to. Global by
    construction: one billing account, one file, so a daily cap can see the true
    sum instead of each repo's slice of it."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "issue-agent", "spend.jsonl")


# ── alert hook ────────────────────────────────────────────────────────────────
def alert(cmd: str, event_json: str) -> None:
    """Pipe `event_json` to `cmd` via `sh -c` (blank cmd → no-op).

    Deliberately a shell string, not an executable + argv: the operator's alert
    is invariably a pipeline (`jq -r .event | mail -s harness me@…`), and making
    them wrap it in a script file is friction that ends with alerts unconfigured.
    Bounded at 10s and swallows every failure — an unreachable pager must not
    wedge the poll loop, and the event is already durably journalled.

    The payload is newline-TERMINATED: every plausible alert sink is line
    oriented (`tee -a alerts.jsonl`, `while read`, `jq`), and two unterminated
    events appended to one file concatenate into a line nothing can parse.
    """
    if not (cmd or "").strip():
        return
    try:
        subprocess.run(
            ["/bin/sh", "-c", cmd],
            input=event_json if event_json.endswith("\n") else event_json + "\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — an alert that fails stays failed
        sys.stderr.write(f"journal: alert command failed ({exc})\n")


# ── journaling runtime decorator ──────────────────────────────────────────────
@dataclass(frozen=True)
class RunContext:
    """Which dispatch a run belongs to. Set once per dispatch; a multi-leg review
    (gate → dimensions → validation) emits one `agent_run` per leg all carrying
    the SAME context, which is per-leg cost attribution for free.

    `dispatch_id` is what makes those legs identifiable AS one dispatch. Minted
    here rather than passed in, so no call site has to thread it: the dispatcher
    already calls `set_context` exactly once per dispatch, which is the same
    boundary. Without it, "how many dispatches never settled?" is unanswerable —
    a daemon killed mid-fan-out leaves six unsettled starts for ONE dead review,
    and a ceiling of five would hand the PR to a human after a single
    `systemctl restart` (see `unsettled_starts`).
    """

    role: str = ""
    kind: str = ""
    number: int = 0
    issue: int | None = None
    claim_target: int | None = None
    dispatch_id: str = ""
    workflow: WorkflowBinding | None = None
    review_child: ReviewChildIdentity | None = None
    # The PR head a fix leg was decided on (accounting records it per run).
    head: str = ""
    # A fix leg continuing a wall-capped leg (the resume-once rule's input).
    resumed: bool = False


def _run_correlation(
    context: RunContext,
    run_id: str,
    leg_id: str,
    session_id: str | None = None,
) -> Correlation:
    # 2026-08-03 (keifu): this used to claim a `pr` only when `issue` was
    # unknown, which conflated "the number is a PR" with "the issue is
    # unknown" — a REVIEW leg knows both, so PR #186 for issue #185 journalled
    # pr=None and the dashboard relabelled the run "PR #185". The kind decides.
    # `number == issue` is treated as issue-numbered regardless of kind: GitHub
    # numbers issues and PRs from one sequence, so they can never coincide, and
    # kinds absent from `ISSUE_NUMBERED` (an unset kind) would otherwise
    # claim their own issue as a PR.
    numbers_a_pr = (
        context.number > 0
        and context.kind not in ISSUE_NUMBERED
        and context.number != context.issue
    )
    return Correlation(
        issue=context.issue,
        pr=context.number if numbers_a_pr else None,
        dispatch_id=context.dispatch_id or None,
        run_id=run_id,
        leg_id=leg_id,
        session_id=session_id,
    )


@dataclass
class _DeferredRouteReceipt:
    """A completed router receipt, held until its dispatch decision is known."""

    task: RuntimePlan | None = None
    result: AgentResult | None = None
    duration_s: float = 0.0
    model: str = ""
    effort: str | None = None
    selected_runtime: str | None = None
    # The router's difficulty tier, when it named one on the ladder.
    tier: str | None = None
    context: RunContext | None = None
    run_id: str = ""
    leg_id: str = ""

    def decide(
        self, model: str | None, effort: object | None, runtime: str | None = None,
        *, tier: str | None = None,
    ) -> None:
        self.selected_runtime = runtime
        self.model = model or ""
        self.effort = getattr(effort, "value", effort) or None
        self.tier = tier


class JournalingRuntime:
    """An `AgentRuntime` that wraps another and records what each run cost.

    A decorator rather than a change to `AgentRunner`/`CodexRunner` because both
    runtimes need it identically and neither should learn about journals: the
    port stays `run(task) -> AgentResult`, and the cost ledger becomes a
    composition concern that `roles.py` wires in one place.
    """

    def __init__(
        self,
        inner,
        journal: EventSink,
        repo: str = "",
        role: str = "",
        runtime: str = "",
        state_dir: str = "",
    ):
        self.inner = inner
        self.journal = journal
        self.repo = repo
        self.role = role
        self.runtime = runtime
        self.state_dir = state_dir
        self._context = contextvars.ContextVar(
            f"journal_run_context_{id(self)}",
            default=RunContext(role=role),
        )
        self._deferred_route_receipt = contextvars.ContextVar[
            _DeferredRouteReceipt | None
        ](
            f"journal_route_receipt_{id(self)}",
            default=None,
        )

    def __getattr__(self, name: str):
        """Preserve the decorated runtime's explicit operational surface.

        The wrapper owns journaling only. Runtime-specific preflight and
        identity attributes remain capabilities of the wrapped adapter rather
        than being copied into this composition layer.
        """

        inner = object.__getattribute__(self, "inner")
        return getattr(inner, name)

    @property
    def context(self) -> RunContext:
        return self._context.get()

    @context.setter
    def context(self, value: RunContext) -> None:
        self._context.set(value)

    def set_context(
        self,
        role: str,
        kind: str,
        number: int,
        issue: int | None,
        claim_target: int | None = None,
        *,
        workflow: WorkflowBinding | None = None,
        review_child: ReviewChildIdentity | None = None,
        head: str = "",
        resumed: bool = False,
    ) -> None:
        """Called by the dispatcher immediately before it hands off to a handler.
        Mutable state on purpose: journaling and paid admission consume the
        same dispatch context across every workflow leg.

        Each call mints a fresh `dispatch_id`; every leg the handler runs
        inherits it. Random rather than sequential because the counter's whole
        purpose is surviving a process death, and a per-process sequence
        restarts at 0 with the daemon."""
        self._context.set(RunContext(
            role=role,
            kind=kind,
            number=number,
            issue=issue,
            claim_target=claim_target,
            dispatch_id=uuid.uuid4().hex[:12],
            workflow=workflow,
            review_child=review_child,
            head=head,
            resumed=resumed,
        ))
        if isinstance(self.inner, ContextualRuntime):
            self.inner.set_context(
                role,
                kind,
                number,
                issue,
                claim_target=claim_target,
                workflow=workflow,
                **({"review_child": review_child} if review_child is not None else {}),
                head=head,
                resumed=resumed,
            )

    @contextlib.contextmanager
    def leg_kind(self, kind: str):
        """Journal the legs run inside this block under `kind` instead of the
        dispatch's own (#40).

        The `dispatch_id` is deliberately KEPT: a sub-leg is part of the
        dispatch that ran it, and minting a new id would make the dispatch look
        like two — `unsettled_starts` counts one death per dispatch, so a
        daemon killed during a classify probe would be charged twice.

        Only the NAME changes, so the leg still reaches the spend ledger (a
        router run is real spend); it just lands in its own bucket instead of
        inflating the one it precedes.
        """
        previous = self.context
        token = self._context.set(replace(previous, kind=kind))
        if isinstance(self.inner, ContextualRuntime):
            self.inner.set_context(
                previous.role,
                previous.kind if previous.review_child is not None else kind,
                previous.number,
                previous.issue,
                claim_target=previous.claim_target,
                workflow=previous.workflow,
                **({"review_child": previous.review_child} if previous.review_child is not None else {}),
                head=previous.head,
                resumed=previous.resumed,
            )
        try:
            yield
        finally:
            if isinstance(self.inner, ContextualRuntime):
                self.inner.set_context(
                    previous.role,
                    previous.kind,
                    previous.number,
                    previous.issue,
                    claim_target=previous.claim_target,
                    workflow=previous.workflow,
                    **({"review_child": previous.review_child} if previous.review_child is not None else {}),
                    head=previous.head,
                    resumed=previous.resumed,
                )
            self._context.reset(token)

    @contextlib.contextmanager
    def route_receipt(self):
        """Delay one ROUTE settlement until its selected resolve stance is known.

        A probe yields a difficulty tier, while the workflow selects the actual
        resolve model and effort after applying its escalation policy. The start
        receipt remains immediate; this only delays the successful settlement.
        """
        receipt = _DeferredRouteReceipt()
        receipt_token = self._deferred_route_receipt.set(receipt)
        try:
            yield receipt.decide
        finally:
            self._deferred_route_receipt.reset(receipt_token)
            if receipt.result is not None and receipt.task is not None:
                context_token = self._context.set(receipt.context or self.context)
                try:
                    self._record(
                        receipt.task, receipt.result, receipt.duration_s,
                        route_decision=receipt,
                        run_id=receipt.run_id,
                        leg_id=receipt.leg_id,
                    )
                finally:
                    self._context.reset(context_token)

    async def run(self, task: RuntimePlan) -> AgentResult:
        """Journal the dispatch BEFORE the await and the settlement after it —
        including when the await raises.

        Review C3: `AgentRunner` deliberately *raises* on a pre-result transport
        death (`agent_runner.py:522-531`), which used to skip every
        attempt-recording write — no journal line, no ledger line — while
        gh-side reaping requeued the issue and re-dispatched it next poll. One
        full paid session every two polls, forever, with `$0.00` on the ledger
        and nothing on `issue-agent status`. So: `agent_run_start` first (it is
        the only durable trace if this process is killed mid-run), and the raise
        path gets its own settled line before the exception continues upward
        UNCHANGED — the crash isolation the raise buys (`Daemon.tick` catches,
        issue keeps `ai-working`, reaper requeues) must not change.
        """
        scope = HandoverScope(
            role=self.context.role,
            issue=self.context.issue,
            state_dir=self.state_dir,
        )
        prepared = with_previous_handover(task, scope)
        run_id, leg_id = self._record_start(task)
        if isinstance(self.inner, RunIdentityRuntime):
            self.inner.set_run_identity(run_id, leg_id)
        t0 = time.monotonic()
        try:
            result = await self.inner.run(prepared.task)
        except BaseException as exc:  # noqa: BLE001 — recorded, never handled
            # BaseException, not Exception: cancellation and Ctrl-C must also
            # settle the started run. They receive an operator-cancelled
            # handover rather than consuming an infrastructure-death life. The
            # bare `raise` preserves the exception and traceback verbatim.
            operator_cancelled = isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            )
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            # A transport death the INPUT causes gets its own journal shape
            # (#13), so the ledger can distinguish it from a stochastic death.
            det = (
                None
                if operator_cancelled
                else deterministic_death(
                    f"{type(exc).__name__}: {exc}",
                    str(getattr(exc, "harness_last_trace", "") or ""),
                )
            )
            terminal = AgentResult(
                is_error=True,
                status="cancelled" if operator_cancelled else "raised",
                cost_usd=0.0,
                model=str(getattr(exc, "harness_resolved_model", "") or ""),
                exit_reason=(
                    ExitReason.OPERATOR_CANCELLED
                    if operator_cancelled
                    else ExitReason.TRANSPORT_DEATH
                ),
            )
            receipt = (
                write_exhaustion_handover(task, terminal, scope)
                if operator_cancelled
                else None
            )
            self._record(
                task,
                terminal,
                time.monotonic() - t0,
                run_id=run_id,
                leg_id=leg_id,
                error=error,
                deterministic=(det.signature if det else None),
                detail=((det.path or None) if det else None),
            )
            if receipt is not None:
                ctx = self.context
                self.journal.emit(
                    TelemetryEvent(
                        HandoverWritten(receipt.trigger, receipt.byte_size),
                        _run_correlation(ctx, run_id, leg_id),
                    )
                )
            raise
        duration_s = time.monotonic() - t0
        deferred = self._deferred_route_receipt.get()
        if deferred is None:
            self._record(
                task, result, duration_s, run_id=run_id, leg_id=leg_id
            )
        else:
            deferred.task = task
            deferred.result = result
            deferred.duration_s = duration_s
            deferred.context = self.context
            deferred.run_id = run_id
            deferred.leg_id = leg_id
        retire_delivered_handover(
            task, result, scope, prepared.handover_digest
        )
        receipt = write_exhaustion_handover(task, result, scope)
        if receipt is not None:
            ctx = self.context
            self.journal.emit(
                TelemetryEvent(
                    HandoverWritten(receipt.trigger, receipt.byte_size),
                    _run_correlation(ctx, run_id, leg_id),
                )
            )
        return result

    def _record_start(self, task: RuntimePlan) -> tuple[str, str]:
        """The pre-await half of the pair. Carries the same
        role/kind/number/issue as the settled line so the two correlate."""
        run_id = uuid.uuid4().hex
        leg_id = uuid.uuid4().hex
        try:
            ctx = self.context
            self.journal.emit(
                TelemetryEvent(
                    AgentRunStarted(
                        task.model or "",
                        ctx.kind,
                        {"role": ctx.role, "number": ctx.number},
                    ),
                    _run_correlation(ctx, run_id, leg_id),
                )
            )
        except Exception as exc:  # noqa: BLE001 — never fail a run over its receipt
            sys.stderr.write(f"journal: could not record agent_run_start ({exc})\n")
        return run_id, leg_id

    def _record(
        self, task: RuntimePlan, result: AgentResult, duration_s: float,
        route_decision: _DeferredRouteReceipt | None = None,
        *,
        run_id: str,
        leg_id: str,
        **extra,
    ) -> None:
        try:
            ctx = self.context
            actual_runtime = task.runtime.value if task.runtime is not None else self.runtime
            cost = float(getattr(result, "cost_usd", 0.0) or 0.0)
            usage = _usage(result.usage)
            # The stream-summed accumulator, journaled beside `usage`. The two are
            # different quantities (~3.5x apart: per-message blocks re-report cache
            # counters), so every completed run that carries both is a calibration
            # pair, and estimates below are priced in stream units against a rate
            # learned in stream units.
            stream = _usage(getattr(result, "stream_usage", None))
            probe_model = task.model or getattr(result, "model", "") or ""
            route_fields = (
                {
                    # ROUTE is a routing-decision event. Keep the probe model
                    # explicit so its small spend remains attributable too.
                    "model": route_decision.model,
                    "effort": route_decision.effort,
                    "probe_model": probe_model,
                    "probe_runtime": actual_runtime,
                    "selected_runtime": route_decision.selected_runtime,
                    "tier": route_decision.tier,
                }
                if route_decision is not None else {"model": probe_model}
            )
            # Admission and settlement use the control store's configured rate
            # table. A telemetry stream must never become a pricing authority.
            attributes = {
                "role": ctx.role,
                "number": ctx.number,
                "effort": route_fields.get("effort"),
                "probe_model": route_fields.get("probe_model"),
                "probe_runtime": route_fields.get("probe_runtime"),
                "selected_runtime": route_fields.get("selected_runtime"),
                "tier": route_fields.get("tier"),
                "envelope_subtype": (
                    getattr(result, "envelope_subtype", "") or None
                ),
                "usage_stream": stream,
                "turns": getattr(result, "num_turns", None),
                "needs": (
                    _bounded_blocked_text(result.needs)
                    if result.status == "blocked"
                    else None
                ),
            }
            if isinstance(result.run_usage, (MeasuredRunUsage, EstimatedRunUsage)):
                attributes["usage_by_session"] = [
                    {"session_id": sample.session_id, "model": sample.model,
                     "input_tokens": sample.usage.input_tokens,
                     "output_tokens": sample.usage.output_tokens,
                     "cache_read_tokens": sample.usage.cache_read_tokens,
                     "cache_write_tokens": sample.usage.cache_write_tokens,
                     "source": sample.usage.source.value}
                    for sample in result.run_usage.samples
                ]
                if isinstance(result.run_usage, EstimatedRunUsage):
                    attributes["usage_estimate_gap"] = result.run_usage.detail
            attributes = {
                key: value
                for key, value in attributes.items()
                if value is not None
            }
            for key in ("error", "deterministic", "detail"):
                if key in extra:
                    attributes[key] = extra[key]
            self.journal.emit(
                TelemetryEvent(
                    AgentRunCompleted(
                        result.status,
                        result.is_error,
                        (
                            result.exit_reason.value
                            if result.exit_reason
                            else None
                        ),
                        max(0, int(round(duration_s * 1000))),
                        str(route_fields.get("model") or probe_model),
                        actual_runtime,
                        Money.from_major("USD", max(0.0, cost)).micros,
                        "USD",
                        (
                            result.pricing_basis.value
                            if result.pricing_basis is not None
                            else "configured_rate_card"
                            if actual_runtime == "codex"
                            else "provider_reported"
                        ),
                        usage,
                        (
                            _bounded_blocked_text(result.summary)
                            if result.status == "blocked" or pre_admission_refusal(result)
                            else None
                        ),
                        ctx.kind,
                        attributes,
                    ),
                    _run_correlation(
                        ctx,
                        run_id,
                        leg_id,
                        result.session_id or None,
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 — never fail a run over its receipt
            sys.stderr.write(f"journal: could not record agent_run ({exc})\n")


def _usage(raw) -> dict | None:
    """Normalise the SDK usage block to the four counters we keep. A non-dict
    (Codex reports none) → None, so 'no usage' and 'zero tokens' stay distinct."""
    if not isinstance(raw, dict):
        return None
    normalized = {k: raw.get(k, 0) for k in _USAGE_KEYS}
    if "total_tokens" in raw:
        normalized["total_tokens"] = raw.get("total_tokens", 0)
    return normalized


# ── readers (pure over the file's bytes; tolerate a truncated tail) ───────────
def _parse_lines(lines) -> list[dict]:
    events: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # truncated/garbled line — skip, never poison the read
        if isinstance(obj, dict):
            events.append(obj)
    return events


def read_events(path: str) -> list[dict]:
    """Every parseable JSON object in `path`, oldest first. A missing file is an
    empty history, not an error — the first status check runs before any daemon
    has written a line."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    return _parse_lines(lines)


# A per-poll reader must not re-parse the whole journal every refresh (keifu
# 2026-08-02: one polling tab pinned a core, cost growing with install age).
# 1 MiB is days of a busy daemon; older history drops off the board.
_TAIL_BYTES = 1 << 20


def read_events_tail(path: str, max_bytes: int | None = None) -> list[dict]:
    """`read_events` over at most the LAST `max_bytes` of the file. The first
    line of the window is dropped when it did not start at byte 0, because a
    mid-line seek would hand the parser a truncated object."""
    window = _TAIL_BYTES if max_bytes is None else max_bytes
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - max(0, window))
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    if start > 0:
        _, _, text = text.partition("\n")
    return _parse_lines(text.splitlines())


def parse_ts(value) -> datetime | None:
    """`ts` → aware UTC datetime, or None if it is missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
