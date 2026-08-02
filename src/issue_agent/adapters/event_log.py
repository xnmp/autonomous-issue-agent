"""ADR 0004's durable, bounded schema-v2 telemetry writer."""

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
    source_files: int = 0


@dataclass(frozen=True)
class EventLogSegments:
    """The canonical feed and only rotations the writer is allowed to own."""

    current: Path

    @property
    def rotations(self) -> tuple[Path, ...]:
        if not self.current.parent.exists():
            return ()
        prefix = f"{self.current.name}."
        retained: list[Path] = []
        for candidate in sorted(
            self.current.parent.glob(f"{self.current.name}.*")
        ):
            if not _ROTATION_SUFFIX.fullmatch(
                candidate.name.removeprefix(prefix)
            ):
                continue
            try:
                metadata = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                retained.append(candidate)
        return tuple(retained)

    @property
    def paths(self) -> tuple[Path, ...]:
        current = (self.current,) if self.current.exists() else ()
        return (*self.rotations, *current)


def _parse_event_lines(text: str) -> tuple[tuple[EventEnvelope, ...], int]:
    """Decode whole lines, counting rather than raising on the undecodable ones."""

    events: list[EventEnvelope] = []
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("event line is not an object")
            events.append(decode_event(raw))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            malformed += 1
    return tuple(events), malformed


def read_event_log(path: str) -> EventReadResult:
    """Read the current feed and writer-generated rotations, skipping bad lines."""

    candidates = EventLogSegments(Path(path)).paths
    events: list[EventEnvelope] = []
    malformed = 0
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            malformed += 1
            continue
        parsed, bad = _parse_event_lines(text)
        events.extend(parsed)
        malformed += bad
    return EventReadResult(tuple(events), malformed, len(candidates))


# Leading bytes kept to recognise a segment rewritten in place. Raw rather than
# hashed: comparing 256 bytes is cheaper than digesting them. This is a
# secondary check only — file identity is `(st_dev, st_ino)`, because the head
# alone is NOT sufficient. `_rotate_if_needed` replays the retained set into the
# new file verbatim, so a feed with a long-lived open level opens its next
# segment with a byte-identical first line.
_HEAD_PROBE_BYTES = 256


@dataclass(frozen=True)
class _SegmentCache:
    dev: int
    ino: int
    offset: int  # end of the last newline-terminated line parsed into `events`
    head: bytes
    events: tuple[EventEnvelope, ...]
    malformed: int
    # A feed can be observed mid-append, so the bytes after the last newline are
    # parsed for this read but never committed to `events`/`offset` — the next
    # read re-parses them once the line is complete, and no event is counted
    # twice.
    tail_events: tuple[EventEnvelope, ...]
    tail_malformed: int


class IncrementalEventLogReader:
    """`read_event_log` that re-parses only what an append-only feed appended.

    Cost per call is O(bytes appended) instead of O(entire history). Reading the
    three role feeds from scratch every few seconds pinned a CPU core on the
    dashboard (keifu, 2026-08-02) and grew without bound as the deployment ran.

    Results equal what `read_event_log` returns for the same files. Only the
    already-parsed prefix is reused, and only when the segment is still the same
    inode with the same leading bytes; a rotation, a truncation, or a rewrite
    re-reads in full. Every read reopens the segment and re-reads the bytes past
    that prefix, because `(mtime, size)` cannot witness a change: `st_mtime_ns`
    carries the kernel's coarse tick, so a same-size rewrite inside one tick is
    invisible to it. What makes the prefix safe to keep is the schema-v2
    append-only contract, not the timestamp.

    Not thread-safe; the owning collector serialises its reads. It retains every
    parsed envelope for every live segment for its lifetime — that retention is
    the saving, and it is bounded by the writer's own rotation limits.
    """

    def __init__(self) -> None:
        self._segments: dict[str, _SegmentCache] = {}
        self._feeds: dict[str, frozenset[str]] = {}

    def read(self, path: str) -> EventReadResult:
        candidates = EventLogSegments(Path(path)).paths
        events: list[EventEnvelope] = []
        malformed = 0
        for candidate in candidates:
            entry = self._segment(candidate)
            if entry is None:
                malformed += 1  # `read_event_log`'s unreadable-segment count
                continue
            events.extend(entry.events)
            events.extend(entry.tail_events)
            malformed += entry.malformed + entry.tail_malformed
        # Rotations are pruned by the writer; retaining their parses would make
        # this cache the very unbounded thing it exists to remove. Eviction is
        # scoped to the feed just read — one reader serves all three role feeds,
        # and a global sweep here made each read evict the other two.
        live = frozenset(str(candidate) for candidate in candidates)
        for stale in self._feeds.get(path, frozenset()) - live:
            self._segments.pop(stale, None)
        self._feeds[path] = live
        return EventReadResult(tuple(events), malformed, len(candidates))

    def _segment(self, candidate: Path) -> _SegmentCache | None:
        key = str(candidate)
        cached = self._segments.get(key)
        try:
            with candidate.open("rb") as stream:
                # `fstat` on the open handle, not `stat` on the path: it is the
                # identity of the file actually being read, so a rotation
                # landing between the two cannot be attributed to the wrong one.
                entry = _read_segment(stream, cached, os.fstat(stream.fileno()))
        except OSError:
            self._segments.pop(key, None)
            return None
        self._segments[key] = entry
        return entry


def _read_segment(stream, cached: _SegmentCache | None, status) -> _SegmentCache:
    head = stream.read(_HEAD_PROBE_BYTES)
    appended_only = (
        cached is not None
        and (status.st_dev, status.st_ino) == (cached.dev, cached.ino)
        # The committed prefix must still be there. A file shorter than it was
        # but no shorter than the prefix is the writer's own
        # `_truncate_incomplete_tail` discarding a killed append, which only
        # ever cuts back to a newline at or after `offset` — that is a resume,
        # not a rewrite.
        and status.st_size >= cached.offset
        and cached.head == head[: len(cached.head)]
    )
    start = cached.offset if appended_only and cached is not None else 0
    stream.seek(start)
    chunk = stream.read()
    # A newline is never part of a multi-byte UTF-8 sequence, so cutting here
    # decodes to exactly the text a whole-file read would have produced.
    boundary = chunk.rfind(b"\n")
    complete, partial = chunk[: boundary + 1], chunk[boundary + 1 :]
    fresh, fresh_malformed = _parse_event_lines(complete.decode("utf-8"))
    tail, tail_malformed = _parse_event_lines(partial.decode("utf-8"))
    return _SegmentCache(
        dev=status.st_dev,
        ino=status.st_ino,
        offset=start + len(complete),
        head=head,
        events=(
            cached.events + fresh
            if appended_only and cached is not None
            else fresh
        ),
        malformed=(
            cached.malformed + fresh_malformed
            if appended_only and cached is not None
            else fresh_malformed
        ),
        tail_events=tail,
        tail_malformed=tail_malformed,
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
        for path in EventLogSegments(current).paths:
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
        rotated = EventLogSegments(Path(self.path)).rotations
        for expired in rotated[:-self.max_rotated_files]:
            try:
                expired.unlink()
            except FileNotFoundError:
                continue

    def paths(self) -> tuple[Path, ...]:
        return EventLogSegments(Path(self.path)).paths
