"""Durable, bounded writer for schema-v2 telemetry events."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
    WorldFeedRetired,
    WorldObserved,
    decode_event,
    encode_event_line,
    new_event_id,
)

_ROTATION_SUFFIX = re.compile(
    r"^\d{8}T\d{6}\.\d{6}Z\.[0-9a-f]{32}$"
)


@dataclass(frozen=True)
class EventReadResult:
    events: tuple[EventEnvelope, ...]
    malformed_lines: int = 0


def read_event_log(path: str) -> EventReadResult:
    """Read the current feed and writer-generated rotations, skipping bad lines."""

    current = Path(path)
    rotations = (
        tuple(
            candidate
            for candidate in sorted(current.parent.glob(f"{current.name}.*"))
            if _ROTATION_SUFFIX.fullmatch(
                candidate.name.removeprefix(f"{current.name}.")
            )
        )
        if current.parent.exists()
        else ()
    )
    candidates = (*rotations, *((current,) if current.exists() else ()))
    events: list[EventEnvelope] = []
    malformed = 0
    for candidate in candidates:
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            malformed += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("event line is not an object")
                events.append(decode_event(raw))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                malformed += 1
    return EventReadResult(tuple(events), malformed)


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
    _open_levels: dict[str, EventEnvelope] = field(
        default_factory=dict, init=False
    )
    _latest_observations: dict[
        tuple[str, str, str, str, int], EventEnvelope
    ] = field(
        default_factory=dict, init=False
    )
    _feed_retirements: dict[
        tuple[str, str, str], EventEnvelope
    ] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.path = str(Path(self.path).resolve())
        if min(
            self.max_bytes,
            self.max_age_seconds,
            self.max_rotated_files,
            self.line_limit,
        ) <= 0:
            raise ValueError("event-log bounds must be positive")
        with self._path_lock():
            self._restore_projection_state()
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
            with self._path_lock():
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
            with self._path_lock():
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
            os.O_APPEND | os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            starting_size = self._truncate_incomplete_tail(fd)
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

    @staticmethod
    def _truncate_incomplete_tail(fd: int) -> int:
        """Return the complete-line boundary after discarding a killed append."""

        size = os.fstat(fd).st_size
        if size == 0:
            return 0
        while True:
            try:
                if os.pread(fd, 1, size - 1) == b"\n":
                    return size
                break
            except InterruptedError:
                continue

        cursor = size
        complete_size = 0
        while cursor > 0:
            start = max(0, cursor - MAX_EVENT_LINE_BYTES)
            while True:
                try:
                    chunk = os.pread(fd, cursor - start, start)
                    break
                except InterruptedError:
                    continue
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                complete_size = start + newline + 1
                break
            cursor = start
        os.ftruncate(fd, complete_size)
        return complete_size

    @contextmanager
    def _path_lock(self) -> Iterator[None]:
        """Serialize append and replacement against every writer for this path."""

        current = Path(self.path)
        current.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = current.parent / f".{current.name}.writer.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise OSError(
                    f"event-log writer lock is not a user-owned regular file: "
                    f"{lock_path}"
                )
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _rotate_if_needed(self) -> None:
        """Rotate while the stable path lock is held by the caller."""

        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        expired = time.time() - self._opened_at >= self.max_age_seconds
        if size < self.max_bytes and not expired:
            return
        suffix = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            + f".{uuid.uuid4().hex}"
        )
        os.replace(self.path, f"{self.path}.{suffix}")
        self._opened_at = time.time()
        self._restore_projection_state(reset=True)
        retained = {
            event.event_id: event
            for event in (
                *self._feed_retirements.values(),
                *self._open_levels.values(),
                *self._latest_observations.values(),
            )
        }
        for event in sorted(retained.values(), key=lambda item: item.order_key):
            self._append(event)
        self._prune_rotated_files()

    def _remember_level(self, event: EventEnvelope) -> None:
        payload = event.payload
        event_feed = (
            event.producer.deployment,
            event.producer.repository,
            event.producer.role,
        )
        if isinstance(payload, WorldFeedRetired):
            retired_feed = (
                payload.deployment,
                payload.repository,
                payload.role,
            )
            self._open_levels = {
                key: level
                for key, level in self._open_levels.items()
                if (
                    level.producer.deployment,
                    level.producer.repository,
                    level.producer.role,
                )
                != retired_feed
            }
            self._latest_observations = {
                key: observed
                for key, observed in self._latest_observations.items()
                if key[:3] != retired_feed
            }
            if not payload.restartable:
                self._feed_retirements[retired_feed] = event
            return
        if event_feed in self._feed_retirements:
            return
        if isinstance(payload, ProjectionSnapshot):
            self._open_levels = {
                key: level
                for key, level in self._open_levels.items()
                if (
                    level.producer.deployment,
                    level.producer.repository,
                    level.producer.role,
                )
                != event_feed
            }
            self._open_levels.update(
                {key: event for key in payload.open_levels}
            )
            return
        if isinstance(payload, WorldObserved):
            self._latest_observations[
                (
                    *event_feed,
                    payload.subject_kind,
                    payload.subject_number,
                )
            ] = event
            return
        if not isinstance(payload, LevelChanged):
            return
        state_key = payload.state_key
        state = payload.state
        if state in {"cleared", "closed", "stopped", "resolved"}:
            self._open_levels.pop(state_key, None)
        else:
            self._open_levels[state_key] = event

    def _restore_projection_state(self, *, reset: bool = False) -> None:
        from ..domain.events import decode_event_line

        if reset:
            self._open_levels.clear()
            self._latest_observations.clear()
            self._feed_retirements.clear()
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
