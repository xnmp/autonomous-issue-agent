"""Read-only compatibility tools for historical JSONL plus typed run telemetry.

Live producers write only schema-v2 events through ``TypedJournal``. The
legacy readers tolerate malformed/truncated lines so old deployments remain
inspectable and migratable; this module intentionally exposes no legacy writer.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import math
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from .runtime_plan import ContextualRuntime, RunIdentityRuntime, RuntimePlan
from ..domain.contracts import Money
from ..domain.workflow_identity import WorkflowBinding
from ..domain.review_execution import ReviewChildIdentity
from ..domain.dashboard import ISSUE_NUMBERED
from ..domain.events import (
    ActionApplied,
    ActionFailed,
    ActionRetried,
    ActionVerified,
    AgentRunCompleted,
    AgentRunStarted,
    BudgetPathEntered,
    CiFlakeDetected,
    CiPendingDeadline,
    DaemonStateChanged,
    DuplicatePrPrevented,
    EscalationRaised,
    EventEnvelope,
    GithubUnavailable,
    HandoverWritten,
    InfraBreakerCleared,
    InfraBreakerTripped,
    InfraPathEntered,
    IssueGroomed,
    LevelChanged,
    PathPrediction,
    RecurrenceDetected,
    ReviewSkipped,
    RoutingEscalated,
    SpendBlocked,
    StallDetected,
    TelemetryEvent,
    TelemetryWarning,
    TickGated,
    TriageDispositionRecorded,
    Correlation,
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


@dataclass(frozen=True)
class LegacyJournalLocator:
    """Path metadata for read-only historical JSONL compatibility."""

    path: str
    canonical: bool = False


def _legacy_event_record(event: TelemetryEvent) -> tuple[str, dict[str, object]]:
    """Lossy schema-v2 → legacy view used only by historical test fixtures."""

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
    if isinstance(payload, ActionVerified):
        return "action_verified", {**common, "desc": payload.display}
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
    if isinstance(payload, PathPrediction):
        return "path_prediction", {
            **common,
            "predicted_paths": list(payload.predicted_paths),
            "changed_files": list(payload.changed_files),
            "matched_paths": list(payload.matched_paths),
            "block_key": payload.signature,
        }
    if isinstance(payload, RecurrenceDetected):
        return "recurrence", {
            **common,
            "pr": correlation.pr or 0,
            "kind": payload.kind,
            "of_issue": payload.original_issue,
            "block_key": payload.signature,
        }
    if isinstance(payload, TriageDispositionRecorded):
        return "triage_disposition", {
            **common,
            "triage_state": payload.triage_state,
        }
    if isinstance(payload, ReviewSkipped):
        return "review_skip", {
            **common,
            "reviewer": payload.reviewer,
            "head_sha": payload.head_sha,
            "reason": payload.reason,
        }
    if isinstance(payload, CiFlakeDetected):
        return "ci_flake", {
            **common,
            "head": payload.head_sha,
            "block_key": payload.signature,
        }
    if isinstance(payload, CiPendingDeadline):
        return "ci_pending_deadline", {
            **common,
            "head": payload.head_sha,
            "seconds": payload.seconds,
            "checks": list(payload.checks),
            "block_key": payload.signature,
        }
    if isinstance(payload, DuplicatePrPrevented):
        return "duplicate_pr_guard", {
            **common,
            "author": payload.author,
            "block_key": payload.signature,
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
    if isinstance(payload, SpendBlocked):
        return "spend_block", {
            **common,
            "scope": payload.scope,
            "status": payload.status,
            "amount_micros": payload.amount_micros,
            "cap_micros": payload.cap_micros,
            "currency": payload.currency,
            "reason": payload.reason,
        }
    if isinstance(payload, InfraPathEntered):
        return "infra_path", {
            **common,
            "leg": payload.leg,
            "exit_reason": payload.exit_reason,
            "infra_deaths": payload.infra_deaths,
        }
    if isinstance(payload, InfraBreakerTripped):
        return "infra_breaker", {
            **common,
            "cause": payload.cause,
            "remediation": payload.remediation,
            "deaths": payload.deaths,
            "issues": list(payload.issues),
            "window_seconds": payload.window_seconds,
        }
    if isinstance(payload, InfraBreakerCleared):
        return "infra_breaker_cleared", {
            **common,
            "reason": payload.reason,
            "cause": payload.cause,
            "deaths": payload.deaths,
        }
    if isinstance(payload, IssueGroomed):
        return "groomed", {
            **common,
            "block_key": payload.signature,
            "kind": payload.disposition,
            **dict(payload.attributes or {}),
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
            "cost_blindspot",
        } else "telemetry_warning"
        return name, {
            **common,
            **dict(payload.details or {}),
            "warning": payload.warning,
        }
    return "telemetry_warning", {
        **common,
        "warning": payload.event_type,
    }


def legacy_event_view(event: EventEnvelope) -> dict[str, object]:
    """One schema-v2 envelope as the bounded legacy telemetry projection.

    Live producers no longer write the v1 journal, but a few read-only
    operator summaries intentionally retain its compact vocabulary. Keeping
    this projection beside the exhaustive payload mapping prevents each reader
    from inventing a partial second translation.
    """
    event_name, fields = _legacy_event_record(event)
    return {
        "ts": event.occurred_at.isoformat(),
        "event": event_name,
        **fields,
    }


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
    # kinds absent from `ISSUE_NUMBERED` (GROOM, an unset kind) would otherwise
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
    context: RunContext | None = None
    run_id: str = ""
    leg_id: str = ""

    def decide(self, model: str | None, effort: object | None, runtime: str | None = None) -> None:
        self.selected_runtime = runtime
        self.model = model or ""
        self.effort = getattr(effort, "value", effort) or None


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

    @property
    def workflow_binding(self) -> WorkflowBinding | None:
        return self.context.workflow

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
            telemetry_warning = str(
                getattr(result, "telemetry_warning", "") or ""
            )
            if telemetry_warning:
                self.journal.emit(
                    TelemetryEvent(
                        TelemetryWarning(
                            "runtime",
                            telemetry_warning,
                            {
                                "role": ctx.role,
                                "kind": ctx.kind,
                                "number": ctx.number,
                                "model": (
                                    task.model
                                    or getattr(result, "model", "")
                                    or ""
                                ),
                            },
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


# How much journal tail a per-poll reader looks at. 1 MiB is ~3-4k events (a
# settled `agent_run` line is ~300 bytes) — days of a busy daemon — and it is
# read on EVERY poll, so the cost of the scan must not grow with the age of the
# install. Bytes rather than lines because a byte offset is one `seek`, whereas
# "the last N lines" of a growing file is a full read.
_TAIL_BYTES = 1 << 20


def read_events_tail(path: str, max_bytes: int = _TAIL_BYTES) -> list[dict]:
    """`read_events` over at most the LAST `max_bytes` of the file.

    For the readers that run on the poll loop rather than on an operator's
    `status` command. The first line of the window is dropped whenever the
    window did not start at byte 0, because a mid-line seek would otherwise hand
    the parser a truncated object — the same tolerance rule as a killed writer's
    torn tail, applied to the read side.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - max(0, max_bytes))
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    if start > 0:
        _, _, text = text.partition("\n")  # discard the partial leading line
    return _parse_lines(text.splitlines())


def death_key(event: dict) -> int | None:
    """The `infra-deaths` counter key an event's dispatch would have been
    charged to, or None if the event names no usable target.

    ADR 0008 makes review convergence stably PR-keyed; fix and steward failures
    use their issue when known and the negated PR fallback otherwise. This
    mirrors the application policy because `unsettled_starts` must feed the
    same ceiling as a surviving-process bump. Kept local because adapters
    cannot import app.
    """
    number = _int(event.get("number"))
    if str(event.get("role") or "").casefold() == "review":
        return None if number is None else -number
    issue = _int(event.get("issue"))
    if issue is not None:
        return issue
    return None if number is None else -number


def unsettled_start_events(
    events, window_s: float = 86400.0, now_fn=_now
) -> list[dict]:
    """The `agent_run_start` EVENTS that never reached a settlement, in journal
    order. `unsettled_starts` counts these by `death_key`; the dashboard needs
    the events themselves (which leg, which model, since when), so the selection
    rule lives here once and both readers share it — see `unsettled_starts` for
    why the grouping is per-dispatch and counting, not positional.
    """
    scoped = events_within(events, window_s, now_fn) if window_s > 0 else list(events)
    starts: dict[tuple, list[dict]] = {}
    settled: dict[tuple, int] = {}
    for e in scoped:
        event = e.get("event")
        if event not in ("agent_run_start", "agent_run"):
            continue
        dispatch = str(e.get("dispatch") or "")
        key = (
            ("dispatch", dispatch) if dispatch
            else ("context", str(e.get("kind", "")), _int(e.get("number")))
        )
        if event == "agent_run_start":
            starts.setdefault(key, []).append(e)
        else:
            settled[key] = settled.get(key, 0) + 1
    out: list[dict] = []
    for key, start_events in starts.items():
        if key[0] == "dispatch":
            missing = 1 if len(start_events) > settled.get(key, 0) else 0
        else:
            missing = len(start_events) - settled.get(key, 0)
        if missing <= 0:
            continue
        # Attribute to the MOST RECENT unsettled starts: when a key's context
        # changed (a PR whose closing-ref appeared between runs), the newest
        # events describe the dispatch the next poll will repeat.
        out.extend(start_events[-missing:])
    return out


def unsettled_starts(
    events, window_s: float = 86400.0, now_fn=_now
) -> dict[int, int]:
    """Paid agent runs that never reached ANY settlement, per `death_key`.

    Review P0R-4: every place that counts a dead session requires the daemon to
    survive that session. A SIGKILL, an OOM kill or a `systemctl restart`
    mid-`await` leaves an `agent_run_start` and nothing else — no settled line,
    no ledger line, no counter — while systemd's `Restart=always` brings the
    daemon back to re-dispatch the very same work. One full paid session per
    (RestartSec + session length), forever, invisible to every breaker. This is
    the read that makes those deaths countable WITHOUT adding a write on the
    death path (there is no such write to add: the process is already gone).

    **Matching rule.** Group by `dispatch` — the id `set_context` mints once per
    dispatch and every leg inherits — and count a group as ONE death when it has
    more starts than settled runs. Then, for pre-`dispatch` journals, fall back
    to grouping by `(kind, number)` and counting `starts - settled`, floored at
    zero.

    Two decisions, both of which cost real money to get wrong:

      * **A dispatch, not a leg.** A `REVIEW_MODE=multi` dispatch fans 8+ legs
        out under one context. Counting legs would charge one `systemctl
        restart` mid-review as six deaths — a ceiling of five, tuned for "the
        session keeps dying", tripped by a single routine restart. FAILURE-MODES
        #58 is explicit that a machine under memory pressure must not walk
        healthy work to a human.
      * **Counting, not positional pairing.** Those legs settle in arbitrary
        order, so "a start with no settled event before the next start" reports
        7 phantom deaths for every healthy multi-agent review. Counting is
        order-free and answers the only question that matters: is this dispatch
        still owed a settlement?

    Consequences worth stating, all of which the tests pin:
      * the RAISE path (a pre-result transport death the process SURVIVED) emits
        a settled `transport_death` line, so it nets to zero here and is counted
        exactly once — by the dispatcher's store bump, not twice;
      * a window edge that cuts a start off from its settlement can only make
        the count NEGATIVE, and negatives floor at zero, so the bound can
        under-report but never invent a death.

    `window_s<=0` scans every event given. The default 24h matches the horizon
    an operator debugs over; older deaths are already spent money and their
    counter file, if the process lived to write one, persists anyway.
    """
    out: dict[int, int] = {}
    for e in unsettled_start_events(events, window_s, now_fn):
        k = death_key(e)
        if k is not None:
            out[k] = out.get(k, 0) + 1
    return out


def parse_ts(value) -> datetime | None:
    """`ts` → aware UTC datetime, or None if it is missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sum_today(path: str, now_fn=_now, repo: str | None = None) -> float:
    """Total `spend` today (UTC). The daily cap's window resets at midnight UTC
    rather than local midnight so the same number is read by every daemon on the
    box regardless of the timezone its shell was launched with.

    `repo` scopes the sum to one deployment (#32: the ledger is global, but
    DAILY_SPEND_CAP_USD is configured per repo, so an unscoped sum let the first
    deployment to spend starve every other one). None = every repo, which is
    what a genuinely global cap and the status readout want.

    An entry with no `repo` field — a legacy line, or a truncated write — is
    counted by the global sum only: unattributable spend is still real money off
    the box, but charging it to an arbitrary deployment would be a lie.
    """
    today = _stamp(now_fn)[:10]
    return sum(
        _cost(e) for e in read_events(path)
        if e.get("event") == "spend" and str(e.get("ts", ""))[:10] == today
        and (repo is None or e.get("repo") == repo)
    )


def sum_issue(path: str, repo: str, issue: int) -> float:
    """LIFETIME spend on one issue (all roles, all legs, all days). Lifetime, not
    windowed: a per-issue cap exists to stop a single issue grinding forever, and
    a windowed one would forgive exactly that by ticking over at midnight."""
    return sum(
        _cost(e) for e in read_events(path)
        if e.get("event") == "spend"
        and e.get("repo") == repo
        and _int(e.get("issue")) == issue
    )


def recent_runs(path: str, n: int) -> list[dict]:
    """The last `n` `agent_run` events, oldest first."""
    runs = [e for e in read_events(path) if e.get("event") == "agent_run"]
    return runs[-n:] if n > 0 else []


def events_within(events, seconds: float, now_fn=_now) -> list[dict]:
    """Events stamped within the last `seconds`. Unparseable/absent `ts` → dropped
    (an event we cannot place in time cannot answer a "recently?" question)."""
    cutoff = now_fn() - timedelta(seconds=seconds)
    out = []
    for e in events:
        ts = parse_ts(e.get("ts"))
        if ts is not None and ts >= cutoff:
            out.append(e)
    return out


def stalled_run_annotations(
    events: list[dict],
    observed_from: datetime,
    woke_at: datetime,
    detected_suspended_seconds: float,
) -> list[dict]:
    """Settled agent legs whose wall clock contains detected suspension time.

    The heartbeat only observes the total wall-vs-monotonic gap when control
    returns to the loop, so ``woke_at - detected_suspended_seconds`` is not the
    suspension's start when work continued after resume. Each agent leg already
    records the same two clocks: journal timestamps are wall time while
    ``duration_s`` is monotonic. Their difference attributes the gap without
    guessing where inside the observed heartbeat interval it occurred.
    """
    starts: dict[str, list[dict]] = {}
    for event in events:
        if event.get("event") != "agent_run_start":
            continue
        dispatch = str(event.get("dispatch") or "")
        if dispatch:
            starts.setdefault(dispatch, []).append(event)

    annotations: list[dict] = []
    seen: set[tuple] = set()
    for event in events:
        if event.get("event") != "agent_run":
            continue
        dispatch = str(event.get("dispatch") or "")
        finished_at = parse_ts(event.get("ts"))
        if not dispatch or finished_at is None:
            continue
        candidates = [
            start
            for start in starts.get(dispatch, ())
            if start.get("kind") == event.get("kind")
            and start.get("number") == event.get("number")
            and (parse_ts(start.get("ts")) or finished_at) <= finished_at
        ]
        if not candidates:
            continue
        start = max(candidates, key=lambda value: parse_ts(value.get("ts")) or finished_at)
        run_started_at = parse_ts(start.get("ts"))
        if (
            run_started_at is None
            or run_started_at >= woke_at
            or finished_at <= observed_from
        ):
            continue
        try:
            active_seconds = float(event.get("duration_s"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(active_seconds) or active_seconds < 0:
            continue
        wall_seconds = (finished_at - run_started_at).total_seconds()
        # Journal stamps have one-second precision and duration_s is rounded to
        # 0.1s. A healthy leg can therefore show a sub-second positive residue;
        # it is timestamp quantisation, not a box suspension.
        leg_suspended_seconds = wall_seconds - active_seconds
        if leg_suspended_seconds <= 1.1:
            continue
        suspended_seconds = min(
            max(0.0, detected_suspended_seconds),
            round(leg_suspended_seconds),
        )
        if suspended_seconds <= 0:
            continue
        identity = (
            dispatch,
            event.get("kind"),
            event.get("number"),
            event.get("ts"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        annotations.append(
            {
                "role": event.get("role"),
                "kind": event.get("kind"),
                "number": event.get("number"),
                "issue": event.get("issue"),
                "dispatch": dispatch,
                "run_started_at": start.get("ts"),
                "run_finished_at": event.get("ts"),
                "suspended_seconds": suspended_seconds,
                "run_id": start.get("run_id") or event.get("run_id"),
                "leg_id": start.get("leg_id") or event.get("leg_id"),
            }
        )
    return annotations


def repeated_dispatch_warning(events, window_s: float, threshold: int) -> str:
    """Warn when the same (kind, number) was DISPATCHED `threshold`+ times inside
    any `window_s` span. Returns "" when nothing is looping.

    This is the cheap detector for the failure shape that costs the most money:
    a leg that never converges (agent errors, requeue, re-dispatch) burns a full
    paid session per poll and looks, in the log, exactly like healthy activity.
    Pure over `events` and self-referenced — the window is measured between the
    events themselves, not against wall-clock now — so it is deterministic to
    test and does not need a clock injected.

    Counts STARTS as well as settled runs (review C3): the loop that costs the
    most is the one where the session dies before any result, which used to emit
    no `agent_run` at all and so was invisible here — the detector reported
    health while the harness burned a session every other poll.

    **A dispatch, not a leg** (review P1R-4). Every leg of one dispatch carries
    the `dispatch` id `set_context` mints, so legs collapse to ONE timestamp
    (the earliest of the group). Counting legs made a single healthy
    `REVIEW_MODE=multi` review — a gate, five dimension specialists and N
    validators, 8+ start/settled pairs under one (kind, number) — report
    "dispatched 8x … likely a non-converging retry loop" on EVERY review. A
    warning that is always on is a warning that is off, and this is the only
    operator-visible signal for the process deaths of P0R-4.

    Events with no `dispatch` field (journals written before the id existed)
    keep the old rule: a target's count is max(starts, settleds), never their
    sum, since a healthy run emits both halves and only the missing half falls
    back to the other. The two rules add, so a journal that spans the upgrade
    counts its old and new events once each.
    """
    if threshold <= 1:
        return ""
    dispatched: dict[tuple, dict[str, datetime]] = {}
    starts: dict[tuple, list[datetime]] = {}
    settled: dict[tuple, list[datetime]] = {}
    for e in events:
        bucket = {"agent_run_start": starts, "agent_run": settled}.get(e.get("event"))
        if bucket is None:
            continue
        ts = parse_ts(e.get("ts"))
        if ts is None:
            continue
        key = (e.get("kind", ""), _int(e.get("number")))
        dispatch = str(e.get("dispatch") or "")
        if not dispatch:
            bucket.setdefault(key, []).append(ts)
            continue
        seen = dispatched.setdefault(key, {})
        first = seen.get(dispatch)
        if first is None or ts < first:
            seen[dispatch] = ts  # a dispatch is stamped when it STARTED
    groups: dict[tuple, list[datetime]] = {
        key: [
            *dispatched.get(key, {}).values(),
            *max(starts.get(key, []), settled.get(key, []), key=len),
        ]
        for key in {**dispatched, **starts, **settled}
    }
    worst = ""
    for (kind, number), stamps in sorted(groups.items(), key=lambda kv: str(kv[0])):
        stamps.sort()
        for i in range(len(stamps) - threshold + 1):
            span = (stamps[i + threshold - 1] - stamps[i]).total_seconds()
            if span <= window_s:
                worst = worst or (
                    f"{kind or 'agent'} #{number} dispatched {len(stamps)}x "
                    f"within {int(window_s)}s — likely a non-converging retry loop"
                )
                break
    return worst


def _emitted_block_keys(events, event: str) -> set[str]:
    """The `block_key`s `event` has already been emitted for. Events with no
    `block_key` (written before the field existed) are ignored rather than
    guessed at — the cost of that is at most one extra emit per key after an
    upgrade."""
    return {
        str(e["block_key"])
        for e in events
        if e.get("event") == event and e.get("block_key")
    }


def spend_blocked_keys(events) -> set[str]:
    """The `block_key`s a `spend_block` event has ALREADY been emitted for.

    The read half of the spend cap's edge trigger (review P1R-2). A cap is a
    LEVEL — once an issue is over its lifetime cap it stays over — so the naive
    "emit on every blocked dispatch" wrote one journal event and ran one
    `ALERT_CMD` subprocess per poll, ~4,300 a day at POLL_SECONDS=20, which is
    how a pager gets muted. Deduping through the journal rather than in memory
    is deliberate: the shape that generates the flood is a wedged daemon under
    `Restart=always`, and an in-process set is empty again after every restart.

    Events with no `block_key` (written before the field existed) are ignored
    rather than guessed at — the cost of that is at most one extra alert per
    key after an upgrade.
    """
    return _emitted_block_keys(events, "spend_block")


def hold_keys(events) -> set[str]:
    """The `block_key`s a `hold` event has ALREADY been emitted for.

    Same edge-trigger read as `spend_blocked_keys`, for the same reason: a hold
    is a LEVEL (an ai-ready issue overlapping an in-flight PR stays held until
    that PR lands, which can be hours), so an un-deduped emit writes one line
    per poll — ~4,300 a day at POLL_SECONDS=20 — and buries the one line that
    answers "why is nothing being dispatched?". Deduping THROUGH the journal
    rather than only in memory keeps that true across the daemon restarts a
    hot reload or `Restart=always` produces.
    """
    return _emitted_block_keys(events, "hold")


def ci_pending_keys(events) -> set[str]:
    """The `block_key`s a `ci_pending_deadline` event has ALREADY been emitted for.

    Fourth instance of the same edge-trigger read (#30). A wedged check is a
    LEVEL — it stays wedged until a human cancels it — and the key carries the
    PR's HEAD SHA, so a re-push (which starts fresh check runs, and so is a
    genuinely new stall if it wedges again) reports again while the same stuck
    run does not. Escalation RATE is a named power guardrail, and a rate counted
    from duplicated events is not a rate.
    """
    return _emitted_block_keys(events, "ci_pending_deadline")


def duplicate_pr_keys(events) -> set[str]:
    """The `block_key`s a `duplicate_pr_guard` event has ALREADY been emitted for.

    Same edge-trigger read (#39), and the same LEVEL argument: the guard's own
    relabel (ai-ready → ai-blocked) normally makes it one-shot, but a relabel
    that GitHub rejected leaves the issue ai-ready with the duplicate PR still
    open — a state that re-derives identically every poll. The key carries the
    PR number, so a DIFFERENT duplicate PR on the same issue is reported again.
    """
    return _emitted_block_keys(events, "duplicate_pr_guard")


def ci_flake_keys(events) -> set[str]:
    """The `block_key`s a `ci_flake` event has ALREADY been emitted for.

    A confirmed flake (#38) is the most level-like signal of the set: the marker
    and the green head both persist until the PR merges, so the derivation is
    true on every poll in between. The key carries the head SHA — the flake
    count is the measurement behind "repair-leg share of spend", and a count
    inflated by one line per poll measures the poll interval, not the flakes.
    """
    return _emitted_block_keys(events, "ci_flake")


def recurrence_keys(events) -> set[str]:
    """The `block_key`s a `recurrence` event has ALREADY been emitted for.

    Third instance of the same edge-trigger read (harness #10). A recurrence is
    the most level-like of the three: a reopened issue stays reopened until
    someone closes it, and the steward re-derives that fact on every fresh world
    build, so an un-deduped emit would write the same line for days. The
    recurrence RATE is the metric this journal has to answer, and a metric
    computed by counting events is destroyed by duplicates — dedup here is a
    correctness property, not just noise control.
    """
    return _emitted_block_keys(events, "recurrence")


def groomed_keys(events) -> set[str]:
    """Durable one-shot keys for harness-adjudicated steward grooming findings."""
    return _emitted_block_keys(events, "groomed")


def _cost(event: dict) -> float:
    try:
        return float(event.get("cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "JournalingRuntime",
    "LegacyJournalLocator",
    "RunContext",
    "alert",
    "death_key",
    "default_ledger_path",
    "events_within",
    "ci_flake_keys",
    "ci_pending_keys",
    "duplicate_pr_keys",
    "hold_keys",
    "parse_ts",
    "read_events",
    "read_events_tail",
    "recent_runs",
    "recurrence_keys",
    "groomed_keys",
    "repeated_dispatch_warning",
    "spend_blocked_keys",
    "sum_issue",
    "sum_today",
    "unsettled_start_events",
    "unsettled_starts",
]
