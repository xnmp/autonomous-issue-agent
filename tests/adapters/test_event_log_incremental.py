"""The dashboard's incremental feed reader must equal a from-scratch read.

Reading every role feed from byte zero on every dashboard poll pinned a CPU
core for hours (keifu, 2026-08-02) and grew without bound as history grew.
These tests pin the contract that makes the incremental reader substitutable:
identical output to `read_event_log`, and no re-parse of bytes already seen.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.event_log import (
    IncrementalEventLogReader,
    read_event_log,
)
from issue_agent.domain.events import (
    Correlation,
    EventEnvelope,
    Producer,
    WorldObserved,
    encode_event_line,
    new_event_id,
)

BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _line(index: int) -> str:
    now = BASE + timedelta(seconds=index)
    return encode_event_line(
        EventEnvelope(
            payload=WorldObserved(
                "issue",
                index % 50 + 1,
                f"hash-{index}",
                {"state": "open", "title": f"issue {index}"},
            ),
            event_id=new_event_id(now),
            seq=index,
            occurred_at=now,
            observed_at=now,
            monotonic_ns=index * 1_000_000,
            producer=Producer("dep", "owner/repo", "fix", 7, "boot"),
            correlation=Correlation(issue=index % 50 + 1),
        )
    )


def _write(path, indexes, *, mode: str = "w") -> None:
    with open(path, mode, encoding="utf-8") as stream:
        for index in indexes:
            stream.write(_line(index) + "\n")


def _bump_mtime(path) -> None:
    """Force a distinguishable mtime; a same-second write can share one."""

    status = path.stat()
    import os

    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000))


def _identity(result):
    """Everything the reader promises, in a comparable form."""

    return (
        tuple(
            (event.event_id, event.seq, event.type, event.payload)
            for event in result.events
        ),
        result.malformed_lines,
        result.source_files,
    )


def test_unchanged_file_is_not_re_decoded(tmp_path) -> None:
    """The saving is the decode, not the open: an unchanged feed is reopened
    (only `(dev, ino)` and the bytes can witness a change) but decodes nothing."""

    path = tmp_path / "events-v2.jsonl"
    _write(path, range(20))
    reader = IncrementalEventLogReader()
    first = reader.read(str(path))
    expected = _identity(read_event_log(str(path)))

    parsed: list = []
    second = _read_counting(reader, path, parsed)

    assert _identity(second) == _identity(first) == expected
    assert parsed == []


def test_append_parses_only_the_tail_and_matches_a_full_read(tmp_path) -> None:
    path = tmp_path / "events-v2.jsonl"
    _write(path, range(30))
    reader = IncrementalEventLogReader()
    reader.read(str(path))

    _write(path, range(30, 45), mode="a")
    _bump_mtime(path)
    parsed: list[str] = []
    incremental = _read_counting(reader, path, parsed)

    assert _identity(incremental) == _identity(read_event_log(str(path)))
    # Only the 15 appended lines were decoded the second time round.
    assert len(parsed) == 15


def _read_counting(reader, path, parsed):
    """Read while recording every line handed to the decoder."""

    import issue_agent.adapters.event_log as module

    original = module.decode_event

    def counting(raw):
        parsed.append(raw)
        return original(raw)

    module.decode_event = counting
    try:
        return reader.read(str(path))
    finally:
        module.decode_event = original


def test_one_reader_serves_several_feeds_without_evicting_them(
    tmp_path,
) -> None:
    """The dashboard reads two role feeds through one reader; reading one
    must not throw away the parses of the others (they are the whole saving)."""

    feeds = []
    for role in ("fix", "review"):
        (tmp_path / role).mkdir()
        path = tmp_path / role / "events-v2.jsonl"
        _write(path, range(20))
        feeds.append(path)
    reader = IncrementalEventLogReader()
    for path in feeds:
        reader.read(str(path))

    _write(feeds[0], range(20, 23), mode="a")
    _bump_mtime(feeds[0])
    parsed: list = []
    for path in feeds:
        result = _read_counting(reader, path, parsed)
        assert _identity(result) == _identity(read_event_log(str(path)))

    assert len(parsed) == 3, "only the three appended lines should be decoded"


def test_truncated_file_is_re_read_in_full(tmp_path) -> None:
    path = tmp_path / "events-v2.jsonl"
    _write(path, range(40))
    reader = IncrementalEventLogReader()
    assert len(reader.read(str(path)).events) == 40

    _write(path, range(100, 105))  # truncate + rewrite: shorter, new content
    _bump_mtime(path)
    result = reader.read(str(path))

    assert _identity(result) == _identity(read_event_log(str(path)))
    assert len(result.events) == 5


def test_replacement_of_the_same_size_is_detected_by_the_head(tmp_path) -> None:
    """Rotation can leave a same-size file; the leading bytes must catch it."""

    path = tmp_path / "events-v2.jsonl"
    _write(path, range(10, 22))
    reader = IncrementalEventLogReader()
    first = reader.read(str(path))
    size = path.stat().st_size

    # Same field widths throughout, so the replacement is byte-identical in
    # length and only the leading-bytes probe can tell the two feeds apart.
    _write(path, range(30, 42))
    assert path.stat().st_size == size, "fixture must produce an equal-size file"
    _bump_mtime(path)
    result = reader.read(str(path))

    assert _identity(result) == _identity(read_event_log(str(path)))
    assert {event.event_id for event in result.events}.isdisjoint(
        {event.event_id for event in first.events}
    )


def test_real_rotation_replaying_a_retained_level_is_not_resumed(
    tmp_path,
) -> None:
    """`_rotate_if_needed` replays the retained set verbatim into the new file,
    so a feed with a long-lived open level opens its next segment with a
    byte-identical first line. The head probe cannot see that; file identity
    must. Resuming across it would skip the new segment's prefix and
    double-count the old one's events."""

    from issue_agent.adapters.event_log import EventLog
    from issue_agent.domain.events import DaemonStateChanged, LevelChanged

    path = tmp_path / "events-v2.jsonl"
    log = EventLog(
        str(path),
        Producer("dep", "owner/repo", "fix", 7, "boot"),
        max_bytes=8192,
    )
    # An open level is retained across every rotation and sorts first, so it
    # leads every segment byte-identically. The rest are payloads rotation does
    # NOT retain, so each segment's body is genuinely different content.
    log.emit(LevelChanged("gate:blocked", "open", "held"))
    counter = iter(range(1, 10_000))

    def emit_one() -> None:
        index = next(counter)
        log.emit(DaemonStateChanged("idle", "running", f"tick {index}"))

    def emit_until_rotation() -> None:
        before_ino = path.stat().st_ino
        while path.stat().st_ino == before_ino:
            emit_one()

    # Read early in a segment's life, so the cached offset is small enough that
    # the NEXT segment grows past it — the state in which neither the size guard
    # nor the head probe can tell a rotation from an append.
    emit_until_rotation()
    for _ in range(10):
        emit_one()
    reader = IncrementalEventLogReader()
    before = reader.read(str(path))
    assert _identity(before) == _identity(read_event_log(str(path)))
    head_before = path.read_bytes()[:256]
    offset_before = path.stat().st_size

    emit_until_rotation()
    while path.stat().st_size <= offset_before:
        emit_one()

    assert path.read_bytes()[:256] == head_before, (
        "fixture must reproduce the replayed-retained-line head collision"
    )

    after = reader.read(str(path))

    # Parity is the whole contract: resuming across the rotation skipped the new
    # segment's prefix and replayed the old one's parse, which shows up here as
    # both missing and duplicated events.
    assert _identity(after) == _identity(read_event_log(str(path)))


def test_same_size_rewrite_within_one_mtime_tick_is_re_read(tmp_path) -> None:
    """`st_mtime_ns` carries the kernel's coarse tick, so a same-size rewrite
    inside one tick leaves (mtime, size) identical. The bytes must decide."""

    import os

    path = tmp_path / "events-v2.jsonl"
    _write(path, range(10, 22))
    reader = IncrementalEventLogReader()
    first = reader.read(str(path))
    status = path.stat()

    _write(path, range(30, 42))  # equal field widths, so equal total size
    assert path.stat().st_size == status.st_size
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))

    result = reader.read(str(path))

    assert _identity(result) == _identity(read_event_log(str(path)))
    assert {event.event_id for event in result.events}.isdisjoint(
        {event.event_id for event in first.events}
    )


def test_truncated_tail_then_append_resumes_without_losing_the_line(
    tmp_path,
) -> None:
    """The writer discards a killed append by cutting back to the last newline,
    then writes a complete line. Same size, same tick — the torn fragment must
    not survive as the cached tail."""

    import os

    path = tmp_path / "events-v2.jsonl"
    _write(path, range(4))
    complete = _line(4) + "\n"
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(complete[:40])
    reader = IncrementalEventLogReader()
    reader.read(str(path))
    status = path.stat()

    with open(path, "r+", encoding="utf-8") as stream:
        stream.seek(0, 2)
        stream.truncate(status.st_size - 40)  # drop the fragment
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(complete[: 40] + complete[40:])
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))

    result = reader.read(str(path))

    assert _identity(result) == _identity(read_event_log(str(path)))
    assert len(result.events) == 5


def test_unreadable_cached_segment_matches_a_full_read(tmp_path) -> None:
    import os

    path = tmp_path / "events-v2.jsonl"
    _write(path, range(5))
    reader = IncrementalEventLogReader()
    reader.read(str(path))

    os.chmod(path, 0o000)
    try:
        if os.access(path, os.R_OK):  # running as root; the case is unreachable
            return
        assert _identity(reader.read(str(path))) == _identity(
            read_event_log(str(path))
        )
    finally:
        os.chmod(path, 0o600)


def test_size_change_without_mtime_change_is_re_read(tmp_path) -> None:
    import os

    path = tmp_path / "events-v2.jsonl"
    _write(path, range(10))
    reader = IncrementalEventLogReader()
    reader.read(str(path))
    frozen = path.stat().st_mtime_ns

    _write(path, range(10, 18), mode="a")
    os.utime(path, ns=(frozen, frozen))
    result = reader.read(str(path))

    assert _identity(result) == _identity(read_event_log(str(path)))
    assert len(result.events) == 18


@pytest.mark.parametrize(
    "garbage",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"schema_version": 1}',
        '{"schema_version": 2, "type": "no.such.type.v1"}',
        '{"schema_version": 2, "type": "world.observed.v1", "data": {}}',
        "{" * 5000,
    ],
)
def test_malformed_appended_line_behaves_like_a_full_read(
    tmp_path, garbage
) -> None:
    path = tmp_path / "events-v2.jsonl"
    _write(path, range(6))
    reader = IncrementalEventLogReader()
    reader.read(str(path))

    with open(path, "a", encoding="utf-8") as stream:
        stream.write(garbage + "\n")
    _bump_mtime(path)

    assert _identity(reader.read(str(path))) == _identity(
        read_event_log(str(path))
    )


def test_partial_final_line_is_not_double_counted_when_completed(
    tmp_path,
) -> None:
    """A feed observed mid-append must not yield the same event twice."""

    path = tmp_path / "events-v2.jsonl"
    _write(path, range(4))
    complete = _line(4) + "\n"
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(complete[: len(complete) // 2])
    reader = IncrementalEventLogReader()
    torn = reader.read(str(path))
    assert _identity(torn) == _identity(read_event_log(str(path)))

    with open(path, "a", encoding="utf-8") as stream:
        stream.write(complete[len(complete) // 2 :])
    _bump_mtime(path)
    healed = reader.read(str(path))

    assert _identity(healed) == _identity(read_event_log(str(path)))
    assert len(healed.events) == 5
    assert len({event.event_id for event in healed.events}) == 5


def test_missing_and_empty_feeds_match_a_full_read(tmp_path) -> None:
    reader = IncrementalEventLogReader()
    missing = tmp_path / "absent.jsonl"
    assert _identity(reader.read(str(missing))) == _identity(
        read_event_log(str(missing))
    )

    empty = tmp_path / "events-v2.jsonl"
    empty.write_text("", encoding="utf-8")
    assert _identity(reader.read(str(empty))) == _identity(
        read_event_log(str(empty))
    )
    # Blank lines are skipped by both readers, incrementally too.
    empty.write_text("\n\n   \n", encoding="utf-8")
    _bump_mtime(empty)
    assert _identity(reader.read(str(empty))) == _identity(
        read_event_log(str(empty))
    )


def test_large_feed_is_not_re_parsed_when_polled_twice(tmp_path) -> None:
    """The scalability regression: polling cost must not track history size."""

    path = tmp_path / "events-v2.jsonl"
    _write(path, range(5000))
    reader = IncrementalEventLogReader()

    started = time.perf_counter()
    first = reader.read(str(path))
    cold_s = time.perf_counter() - started

    started = time.perf_counter()
    second = reader.read(str(path))
    warm_s = time.perf_counter() - started

    assert len(first.events) == 5000
    assert _identity(second) == _identity(first)
    assert warm_s < cold_s / 20, (
        f"unchanged 5k-event feed cost {warm_s:.4f}s vs {cold_s:.4f}s cold; "
        "it should be a stat, not a re-parse"
    )
