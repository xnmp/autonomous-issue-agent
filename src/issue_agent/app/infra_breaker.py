"""Process-lifetime holder for the deployment-wide infra breaker.

Composition only: the decision lives in `domain.infra_breaker`, this object
owns the observations, the clock, the journal, and the lock.

STATE IS IN-MEMORY, deliberately. Re-deriving the window from the journal at
boot would mean reading telemetry to authorize an admission decision, which
inverts the authority ADR; and a durable breaker whose evidence outlives the
process would keep a deployment paused across the very restart that usually
FIXES the runtime (the observed incident was cured by restarting the unit with
a correct PATH). A restart therefore re-arms, and if the runtime is still
broken the next death storm re-trips inside one window — one storm's cost, paid
once, in exchange for never inheriting a stale stop.

Thread-safe: fix dispatches run on `Daemon`'s worker pool, so deaths arrive
from dispatch threads while the tick thread reads the gate.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..domain.events import (
    EventPayload,
    InfraBreakerCleared,
    InfraBreakerTripped,
    TelemetryEvent,
)
from ..domain.infra_breaker import (
    CLEAR_PROBE_SUCCEEDED,
    BreakerPolicy,
    BreakerTrip,
    InfraDeath,
    clear_reason,
    gate_reason,
    trip_for,
    within_window,
)
from ..domain.ports import EventSink


def _ignore_log(_message: str) -> None:
    return None


class InfraBreaker:
    """Records infra-class deaths; reports whether paid admission is paused.

    `gate_reason` matches `Daemon.tick_gate`'s contract: a string means "skip
    paid dispatch this tick", None means proceed. Observation, reconciliation
    and lifecycle replay continue either way — the same posture as the spend
    cap, which is the gate this one stands beside.
    """

    def __init__(
        self,
        policy: BreakerPolicy | None = None,
        *,
        journal: EventSink | None = None,
        log: Callable[[str], None] = _ignore_log,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy if policy is not None else BreakerPolicy()
        self._journal = journal
        self._log = log
        self._clock = clock
        self._lock = threading.Lock()
        self._deaths: list[InfraDeath] = []
        self._trip: BreakerTrip | None = None
        self._tripped_at = 0.0

    @property
    def policy(self) -> BreakerPolicy:
        return self._policy

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._trip is not None

    def record_death(self, issue: int, leg: str = "", reason: str = "") -> None:
        """Observe one infra-class death, and trip when the evidence crosses
        issues (see `domain.infra_breaker.trip_for`).

        Called from the same place that charges the per-issue infra ceiling, so
        the two counters can never disagree about what happened.
        """
        now = self._clock()
        with self._lock:
            self._deaths = list(
                within_window(
                    [*self._deaths, InfraDeath(issue, now, leg, reason)],
                    now=now,
                    window_seconds=self._policy.window_seconds,
                )
            )
            if self._trip is not None:
                # Already held: the storm is what we already escalated once.
                return
            trip = trip_for(self._deaths, now=now, policy=self._policy)
            if trip is None:
                return
            self._trip = trip
            self._tripped_at = now
        self._log(gate_reason(trip, policy=self._policy))
        self._emit(
            InfraBreakerTripped(
                cause=trip.cause,
                remediation=trip.remediation,
                deaths=trip.deaths,
                issues=trip.issues,
                window_seconds=trip.window_seconds,
            )
        )

    def record_success(self) -> None:
        """A leg completed — the probe this breaker recovers on.

        Any RETURNED result counts, error included: the question is whether the
        runtime can run a leg at all, not whether the answer was good. The
        death history is dropped too, so a healthy run starts a fresh window
        instead of leaving a storm's evidence one death short of re-tripping.
        """
        now = self._clock()
        with self._lock:
            self._deaths = []
            trip = self._trip
            if trip is None:
                return
            self._trip = None
        self._announce_clear(CLEAR_PROBE_SUCCEEDED, trip)

    def gate_reason(self) -> str | None:
        """Tick gate. Also the cooldown's only evaluation point — the daemon
        polls, so there is no timer to arm and no loop needing its own bound.
        """
        now = self._clock()
        with self._lock:
            trip = self._trip
            if trip is None:
                return None
            reason = clear_reason(
                tripped_at=self._tripped_at, now=now, policy=self._policy
            )
            if not reason:
                return gate_reason(trip, policy=self._policy)
            self._trip = None
            self._deaths = []
        self._announce_clear(reason, trip)
        return None

    def _announce_clear(self, reason: str, trip: BreakerTrip) -> None:
        self._log(f"infra breaker cleared ({reason}) — paid dispatch resumes")
        self._emit(
            InfraBreakerCleared(
                reason=reason, cause=trip.cause, deaths=trip.deaths
            )
        )

    def _emit(self, payload: EventPayload) -> None:
        journal = self._journal
        if journal is None:
            return
        try:
            journal.emit(TelemetryEvent(payload))
        except Exception as exc:  # noqa: BLE001 — telemetry never fails a leg
            self._log(f"infra breaker not journalled ({exc})")
