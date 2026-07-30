"""Schema-v2 telemetry adapter.

Call sites still use stable event names while this adapter converts them to
typed envelopes. The legacy path is retained only so historical tools can
locate old files; live events are written once, to the canonical v2 feed.
"""

from __future__ import annotations

import hashlib
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from .event_log import EventLog
from .journal import Journal
from ..domain.events import (
    ActionApplied,
    ActionFailed,
    ActionRetried,
    ActionVerified,
    AgentRunAbandoned,
    AgentRunCompleted,
    AgentRunStallCorrected,
    AgentRunStarted,
    BudgetPathEntered,
    CiFlakeDetected,
    CiPendingDeadline,
    Correlation,
    DaemonStateChanged,
    DuplicatePrPrevented,
    EscalationRaised,
    GithubUnavailable,
    HandoverWritten,
    IssueGroomed,
    LevelChanged,
    PathPrediction,
    RecurrenceDetected,
    ReviewSkipped,
    RoutingEscalated,
    SpendBlocked,
    SpendRecorded,
    StallDetected,
    TelemetryWarning,
    TickGated,
    decode_event_line,
)
from ..domain.contracts import RunId


@dataclass(frozen=True)
class TypedJournal:
    """Write only the canonical typed feed."""

    legacy: Journal
    events: EventLog
    canonical: bool = field(default=True, init=False)
    _start_ids: dict[str, str] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def path(self) -> str:
        return self.legacy.path

    def emit(self, event: str, **fields: object) -> None:
        try:
            correlation = _correlation(fields)
            with self._lock:
                if event == "agent_run_stall":
                    corrects = self._start_ids.get(correlation.run_id or "")
                    payload = (
                        AgentRunStallCorrected(
                            corrects,
                            str(fields.get("stall_id") or uuid.uuid4().hex),
                            max(
                                0,
                                int(
                                    _number(fields.get("suspended_seconds"))
                                    * 1000
                                ),
                            ),
                        )
                        if corrects
                        else TelemetryWarning(
                            "agent_run_stall_without_start",
                            _warning(fields),
                        )
                    )
                else:
                    payload = _payload(event, fields)
                envelope = self.events.emit(payload, correlation=correlation)
                if event == "agent_run_start" and correlation.run_id:
                    self._start_ids[correlation.run_id] = envelope.event_id
                elif event == "agent_run_stall" and correlation.run_id:
                    self._start_ids.pop(correlation.run_id, None)
        except Exception as exc:  # telemetry cannot fail orchestration
            sys.stderr.write(f"typed journal: dropped {event!r} event ({exc})\n")

    def abandon_run(self, run_id: RunId, reason: str) -> bool:
        """Correct a persisted start after recovery proves its process is dead."""

        start = None
        corrected: set[str] = set()
        for path in self.events.paths():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    event = decode_event_line(line)
                except (ValueError, TypeError):
                    continue
                payload = event.payload
                if isinstance(payload, AgentRunAbandoned):
                    corrected.add(payload.corrects)
                if (
                    isinstance(payload, AgentRunStarted)
                    and event.correlation.run_id == str(run_id)
                ):
                    start = event
        if start is None:
            self.events.emit(
                TelemetryWarning(
                    "abandoned_run_without_start",
                    f"run {run_id}: {reason}",
                ),
                correlation=Correlation(run_id=str(run_id)),
            )
            return False
        if start.event_id in corrected:
            return True
        self.events.emit(
            AgentRunAbandoned(start.event_id, reason),
            correlation=start.correlation,
        )
        return True


def _payload(event: str, fields: Mapping[str, object]):
    if event == "agent_run_start":
        return AgentRunStarted(_text(fields, "model"), _text(fields, "kind"))
    if event == "agent_run":
        amount = _micros(
            fields.get("cost_usd") or fields.get("cost_estimated_usd")
        )
        return AgentRunCompleted(
            _text(fields, "status"),
            bool(fields.get("is_error")),
            _optional_text(fields.get("exit_reason")),
            max(0, int(_number(fields.get("duration_s")) * 1000)),
            _text(fields, "model"),
            _text(fields, "runtime"),
            amount,
            "USD",
            (
                "learned_estimate"
                if fields.get("cost_estimated_usd")
                else "provider_reported"
            ),
            _usage(fields.get("usage")),
            _optional_text(fields.get("summary")),
            _text(fields, "kind"),
        )
    if event == "action":
        structured = fields.get("action")
        kind = ""
        target = ""
        if isinstance(structured, Mapping):
            kind = str(structured.get("type") or structured.get("kind") or "")
            target = str(structured.get("target") or "")
        display = _text(fields, "desc")
        return ActionApplied(_stable_id(event, fields), kind, target, display)
    if event == "action_failed":
        return ActionFailed(
            _stable_id(event, fields),
            _text(fields, "desc"),
            "retryable_failure" if fields.get("pending") else "permanent_failure",
            _text(fields, "error"),
        )
    if event == "action_retry":
        return ActionRetried(
            _stable_id("action", fields),
            _text(fields, "desc"),
            _integer(fields.get("attempt"), 1),
            max(0, int(_number(fields.get("delay_seconds")) * 1000)),
            _text(fields, "error"),
        )
    if event == "action_verified":
        return ActionVerified(
            _stable_id("action", fields), _text(fields, "desc")
        )
    if event in {"action_quarantined", "action_reconcile_skipped"}:
        return TelemetryWarning(event, _warning(fields))
    if event == "escalation":
        return EscalationRaised(
            _text(fields, "target"), _positive(fields.get("number"))
        )
    if event == "deferred":
        issue = _positive(fields.get("issue"))
        key = (
            f"issue:{issue}:deferred:"
            f"{_text(fields, 'reason')}:{_text(fields, 'blocker')}"
        )
        return LevelChanged(key, _text(fields, "state"), _text(fields, "reason"))
    if event == "spend":
        return SpendRecorded(
            _text(fields, "run_id") or _stable_id(event, fields),
            _micros(fields.get("cost_usd")),
            _text(fields, "currency") or "USD",
            _text(fields, "basis"),
            _optional_text(fields.get("rate_card_id")),
            _optional_text(fields.get("rate_card_version")),
        )
    if event == "spend_block":
        return SpendBlocked(
            _text(fields, "scope"),
            "exhausted",
            _micros(fields.get("spent")),
            _micros(fields.get("cap")),
            "USD",
            _text(fields, "block_key"),
        )
    if event == "telemetry_warning":
        return TelemetryWarning(
            _text(fields, "code") or "runtime", _text(fields, "warning")
        )
    if event in {"cost_blindspot", "ci_rerun"}:
        return TelemetryWarning(event, _warning(fields))
    if event == "tick_gated":
        return TickGated(_text(fields, "reason"))
    if event == "gh_unavailable":
        retry = fields.get("retry_after_s")
        return GithubUnavailable(
            _text(fields, "kind"),
            _text(fields, "detail"),
            None if retry is None else max(0, int(_number(retry) * 1000)),
        )
    if event == "stall":
        return StallDetected(
            _text(fields, "stall_id") or _stable_id(event, fields),
            _text(fields, "started_at"),
            _text(fields, "woke_at"),
            max(0, int(_number(fields.get("suspended_seconds")) * 1000)),
        )
    if event == "handover_written":
        return HandoverWritten(
            _text(fields, "trigger"), _integer(fields.get("bytes"), 0)
        )
    if event == "path_prediction":
        return PathPrediction(
            _strings(fields.get("predicted_paths")),
            _strings(fields.get("changed_files")),
            _strings(fields.get("matched_paths")),
        )
    if event == "recurrence":
        original = fields.get("of_issue")
        return RecurrenceDetected(
            _text(fields, "kind"),
            _positive(original) if original is not None else None,
        )
    if event == "review_skip":
        return ReviewSkipped(
            _text(fields, "reviewer"),
            _text(fields, "head_sha"),
            _text(fields, "reason"),
        )
    if event == "ci_flake":
        return CiFlakeDetected(_text(fields, "head"))
    if event == "ci_pending_deadline":
        return CiPendingDeadline(
            _text(fields, "head"),
            _integer(fields.get("seconds"), 0),
            _strings(fields.get("checks")),
        )
    if event == "duplicate_pr_guard":
        return DuplicatePrPrevented(_text(fields, "author"))
    if event == "route_escalation":
        evidence = {
            key: value
            for key, value in fields.items()
            if key
            in {
                "notches",
                "ceiling",
                "recurrence",
                "resolve_retries",
                "ci_retries",
            }
            and isinstance(value, (int, bool))
        }
        return RoutingEscalated(
            _text(fields, "leg"),
            _integer(fields.get("from_tier"), 0),
            _integer(fields.get("to_tier"), 0),
            _optional_text(fields.get("from_effort")),
            _optional_text(fields.get("to_effort")),
            evidence,
        )
    if event == "budget_path":
        return BudgetPathEntered(
            _text(fields, "leg"), _text(fields, "exit_reason")
        )
    if event == "groomed":
        return IssueGroomed(
            _text(fields, "block_key"), _text(fields, "kind")
        )
    if event == "daemon_state":
        return DaemonStateChanged(
            _text(fields, "previous"),
            _text(fields, "state"),
            _text(fields, "reason"),
        )
    return TelemetryWarning("unknown-producer-event", event)


def _correlation(fields: Mapping[str, object]) -> Correlation:
    issue_raw = fields.get("issue")
    pr_raw = fields.get("pr")
    target = str(fields.get("target") or "")
    if pr_raw is None and target == "pr":
        pr_raw = fields.get("number")
    if issue_raw is None and target == "issue":
        issue_raw = fields.get("number")
    return Correlation(
        issue=_positive(issue_raw) if issue_raw is not None else None,
        pr=_positive(pr_raw) if pr_raw is not None else None,
        dispatch_id=_optional_text(fields.get("dispatch")),
        run_id=_optional_text(fields.get("run_id")),
        leg_id=_optional_text(fields.get("leg_id")),
        session_id=_optional_text(fields.get("session_id")),
    )


def _stable_id(event: str, fields: Mapping[str, object]) -> str:
    material = "|".join(
        (event, _text(fields, "desc"), _text(fields, "block_key"))
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _text(fields: Mapping[str, object], key: str) -> str:
    value = fields.get(key)
    return "" if value is None else str(value)


def _optional_text(value: object) -> str | None:
    text = "" if value is None else str(value)
    return text or None


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _positive(value: object) -> int:
    parsed = _integer(value, 0)
    if parsed <= 0:
        raise ValueError("correlated target number must be positive")
    return parsed


def _micros(value: object) -> int:
    return max(0, int(round(_number(value) * 1_000_000)))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        return ()
    return tuple(str(item) for item in value)


def _usage(value: object) -> Mapping[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        str(key): max(0, _integer(item, 0))
        for key, item in value.items()
    }


def _warning(fields: Mapping[str, object]) -> str:
    return ", ".join(
        f"{key}={value}"
        for key, value in sorted(fields.items())
        if isinstance(value, (str, int, float, bool)) or value is None
    )
