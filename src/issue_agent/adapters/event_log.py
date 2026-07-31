"""Durable, bounded writer for schema-v2 telemetry events."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..domain.events import (
    Correlation,
    EventEnvelope,
    EventPayload,
    LevelChanged,
    MAX_EVENT_LINE_BYTES,
    Producer,
    ProjectionSnapshot,
    WorldObserved,
    encode_event_line,
    new_event_id,
)

@dataclass
class EventLog:
    path: str
    producer: Producer
    max_bytes: int = 32 * 1024 * 1024
    max_age_seconds: int = 7 * 24 * 60 * 60
    max_rotated_files: int = 8
    line_limit: int = MAX_EVENT_LINE_BYTES
    _seq: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _opened_at: float = field(default_factory=time.time, init=False)
    _open_levels: dict[str, object] = field(default_factory=dict, init=False)
    _latest_observations: dict[tuple[str, int], EventEnvelope] = field(
        default_factory=dict, init=False
    )

    def __post_init__(self) -> None:
        if min(
            self.max_bytes,
            self.max_age_seconds,
            self.max_rotated_files,
            self.line_limit,
        ) <= 0:
            raise ValueError("event-log bounds must be positive")
        self._restore_open_levels()
        try:
            self._opened_at = os.path.getmtime(self.path)
        except OSError:
            pass

    def emit(
        self,
        payload: EventPayload,
        *,
        correlation: Correlation = Correlation(),
        causation_time: datetime | None = None,
    ) -> EventEnvelope:
        with self._lock:
            envelope = self._prepare(payload, correlation, causation_time)
            self._append(envelope)
            self._remember_level(envelope)
            self._rotate_if_needed()
            return envelope

    def prepare(
        self,
        payload: EventPayload,
        correlation: Correlation = Correlation(),
    ) -> EventEnvelope:
        """Mint an envelope for a control-store transaction without publishing it."""

        with self._lock:
            return self._prepare(payload, correlation, None)

    def append_committed(self, event: EventEnvelope) -> None:
        """Publish an envelope only after the state transition that owns it commits."""

        with self._lock:
            self._append(event)
            self._remember_level(event)
            self._rotate_if_needed()

    def _prepare(
        self,
        payload: EventPayload,
        correlation: Correlation,
        causation_time: datetime | None,
    ) -> EventEnvelope:
        raw_now = datetime.now(timezone.utc)
        now = raw_now.replace(
            microsecond=(raw_now.microsecond // 1000) * 1000
        )
        envelope = EventEnvelope(
            payload=payload,
            event_id=new_event_id(now),
            seq=self._seq,
            occurred_at=causation_time or now,
            observed_at=now,
            monotonic_ns=time.monotonic_ns(),
            producer=self.producer,
            correlation=correlation,
        )
        self._seq += 1
        return envelope

    def _append(self, event: EventEnvelope) -> None:
        encoded = (encode_event_line(event) + "\n").encode()
        if len(encoded) > self.line_limit:
            raise ValueError(
                f"event line is {len(encoded)} bytes; limit is {self.line_limit}"
            )
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        fd = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            starting_size = os.fstat(fd).st_size
            offset = 0
            try:
                while offset < len(encoded):
                    try:
                        written = os.write(fd, encoded[offset:])
                    except InterruptedError:
                        continue
                    if written <= 0:
                        raise OSError(
                            f"short event append: {offset}/{len(encoded)}"
                        )
                    offset += written
            except BaseException:
                os.ftruncate(fd, starting_size)
                raise
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def _rotate_if_needed(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        expired = time.time() - self._opened_at >= self.max_age_seconds
        if size < self.max_bytes and not expired:
            return
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        os.replace(self.path, f"{self.path}.{suffix}")
        self._opened_at = time.time()
        if self._open_levels:
            now = datetime.now(timezone.utc)
            snapshot = EventEnvelope(
                payload=ProjectionSnapshot(dict(self._open_levels)),
                event_id=new_event_id(now),
                seq=self._seq,
                occurred_at=now,
                observed_at=now,
                monotonic_ns=time.monotonic_ns(),
                producer=self.producer,
            )
            self._seq += 1
            self._append(snapshot)
        for key in sorted(self._latest_observations):
            observed = self._latest_observations[key]
            self._append(observed)
        self._prune_rotated_files()

    def _remember_level(self, event: EventEnvelope) -> None:
        payload = event.payload
        if isinstance(payload, ProjectionSnapshot):
            self._open_levels = dict(payload.open_levels)
            return
        if isinstance(payload, WorldObserved):
            self._latest_observations[
                (payload.subject_kind, payload.subject_number)
            ] = event
            return
        if not isinstance(payload, LevelChanged):
            return
        state_key = payload.state_key
        state = payload.state
        if state in {"cleared", "closed", "stopped", "resolved"}:
            self._open_levels.pop(state_key, None)
        else:
            self._open_levels[state_key] = {
                "state": state,
                "event_id": event.event_id,
                "occurred_at": event.occurred_at.isoformat(),
            }

    def _restore_open_levels(self) -> None:
        from ..domain.events import decode_event_line

        current = Path(self.path)
        rotated = sorted(current.parent.glob(f"{current.name}.*"))
        paths = [*rotated, *([current] if current.exists() else [])]
        for path in paths:
            try:
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        if not line.strip() or len(line.encode()) > self.line_limit:
                            continue
                        try:
                            event = decode_event_line(line)
                        except (TypeError, ValueError):
                            continue
                        self._seq = max(self._seq, event.seq + 1)
                        self._remember_level(event)
            except OSError:
                continue

    def _prune_rotated_files(self) -> None:
        current = Path(self.path)
        rotated = sorted(current.parent.glob(f"{current.name}.*"))
        for expired in rotated[:-self.max_rotated_files]:
            try:
                expired.unlink()
            except FileNotFoundError:
                continue

    def paths(self) -> tuple[Path, ...]:
        current = Path(self.path)
        rotated = sorted(current.parent.glob(f"{current.name}.*"))
        return tuple([*rotated, *([current] if current.exists() else [])])
