"""Best-effort schema-v2 telemetry sink."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from threading import Lock

from .event_log import EventLog
from ..domain.contracts import RunId
from ..domain.events import (
    AgentRunAbandoned,
    AgentRunStarted,
    Correlation,
    TelemetryEvent,
    TelemetryWarning,
    decode_event_line,
)


@dataclass(frozen=True)
class TypedJournal:
    """Append producer-created typed events to the canonical feed.

    ``legacy_path`` locates historical JSONL for read-only compatibility tools;
    this sink never writes it.
    """

    legacy_path: str
    events: EventLog
    canonical: bool = field(default=True, init=False)
    _lock: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def path(self) -> str:
        return self.legacy_path

    def emit(self, event: TelemetryEvent) -> None:
        self._emit_payload(event.payload, event.correlation)

    def _emit_payload(self, payload, correlation: Correlation) -> bool:
        try:
            with self._lock:
                self.events.emit(
                    payload,
                    correlation=correlation,
                )
        except Exception as exc:  # telemetry cannot fail orchestration
            sys.stderr.write(
                f"typed journal: dropped {payload.event_type!r} "
                f"event ({exc})\n"
            )
            return False
        return True

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
            return self._emit_payload(
                TelemetryWarning(
                    "abandoned_run_without_start",
                    f"run {run_id}: {reason}",
                ),
                Correlation(run_id=str(run_id)),
            )
        if start.event_id in corrected:
            return True
        return self._emit_payload(
            AgentRunAbandoned(start.event_id, reason),
            start.correlation,
        )
