"""Snapshot collection must cost nothing while the deployment is idle.

A browser tab polling the dashboard held a CPU core at 100% for hours (keifu,
2026-08-02): every TTL expiry re-read and re-folded all three role feeds from
byte zero, so the cost grew with history rather than with change. These tests
pin the guardrails on the fix — identical payloads to a from-scratch collect,
and staleness indicators that still turn over on time.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from issue_agent.adapters.dashweb import system
from issue_agent.adapters.event_log import EventLog
from issue_agent.domain.events import (
    Correlation,
    Producer,
    WorldObserved,
)

# Fixed and in the past: `causation_time` may not run ahead of the writer's own
# `observed_at`, which is the real clock.
NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
ROLES = ("fix", "review")


def _journal_paths(tmp_path) -> list[str]:
    paths = []
    for role in ROLES:
        (tmp_path / role).mkdir(exist_ok=True)
        paths.append(str(tmp_path / role / "journal.jsonl"))
    return paths


def _emit(tmp_path, role: str, issue: int, *, at: datetime = NOW, count: int = 1):
    log = EventLog(
        str(tmp_path / role / "events-v2.jsonl"),
        Producer("demo", "owner/repo", role, 100, "boot"),
    )
    for index in range(count):
        log.emit(
            WorldObserved(
                "issue",
                issue + index,
                f"hash-{issue + index}",
                {"present": True, "state": "open", "labels": ["ai-ready"]},
            ),
            correlation=Correlation(issue=issue + index),
            causation_time=at,
        )


def _kwargs(tmp_path, *, now=NOW, stale_after_s: float = 60.0) -> dict:
    return {
        "name": "demo",
        "repo": "owner/repo",
        "target_dir": str(tmp_path),
        "journal_paths": _journal_paths(tmp_path),
        "log_dir": str(tmp_path / "logs"),
        "ledger_path": str(tmp_path / "spend.jsonl"),
        "now_fn": (now if callable(now) else (lambda: now)),
        "stale_after_s": stale_after_s,
    }


def _payload(snapshot) -> tuple:
    """The observable snapshot payload, independent of object identity."""

    deployment = snapshot.deployment
    return (
        snapshot.name,
        snapshot.repo,
        snapshot.target_dir,
        snapshot.timestamps,
        deployment.schema_version,
        deployment.warnings,
        deployment.health,
        deployment.issues,
        deployment.historical_issues,
        deployment.metrics,
        deployment.unattributed,
    )


def test_unchanged_sources_return_the_cached_snapshot_object(tmp_path) -> None:
    _emit(tmp_path, "fix", 7)
    collector = system.SnapshotCollector()
    first = collector.collect(**_kwargs(tmp_path))

    def refuse(path):
        raise AssertionError(f"unchanged deployment re-read {path}")

    collector._readers["demo"].read = refuse  # noqa: SLF001 - the point of the test
    second = collector.collect(**_kwargs(tmp_path))

    assert second is first


def test_cached_snapshot_equals_a_from_scratch_collect(tmp_path) -> None:
    _emit(tmp_path, "fix", 7, count=40)
    _emit(tmp_path, "review", 200, count=10)
    collector = system.SnapshotCollector()
    collector.collect(**_kwargs(tmp_path))

    _emit(tmp_path, "fix", 900, count=5)
    incremental = collector.collect(**_kwargs(tmp_path))
    from_scratch = system.collect_snapshot(**_kwargs(tmp_path))

    assert _payload(incremental) == _payload(from_scratch)


def test_appended_events_invalidate_the_cached_snapshot(tmp_path) -> None:
    _emit(tmp_path, "fix", 7)
    collector = system.SnapshotCollector()
    before = collector.collect(**_kwargs(tmp_path))
    assert {row.issue for row in before.deployment.issues} == {7}

    _emit(tmp_path, "fix", 8)
    after = collector.collect(**_kwargs(tmp_path))

    assert {row.issue for row in after.deployment.issues} == {7, 8}
    assert _payload(after) == _payload(system.collect_snapshot(**_kwargs(tmp_path)))


def test_a_silent_deployment_still_goes_stale(tmp_path) -> None:
    """The short-circuit must not freeze liveness: a dead daemon writes nothing,
    so its fingerprint never moves, yet it must still be reported stale."""

    _emit(tmp_path, "fix", 7)
    collector = system.SnapshotCollector()
    live = collector.collect(**_kwargs(tmp_path, now=NOW + timedelta(seconds=10)))
    assert [health.stale for health in live.deployment.health] == [
        False,
        True,
    ]

    dead = collector.collect(**_kwargs(tmp_path, now=NOW + timedelta(seconds=90)))

    assert dead is not live
    assert all(health.stale for health in dead.deployment.health)


def test_a_new_local_day_invalidates_the_cached_snapshot(tmp_path) -> None:
    """Today's spend is bucketed by host day; local midnight cannot be cached over."""

    _emit(tmp_path, "fix", 7)
    collector = system.SnapshotCollector()
    local = timezone(timedelta(hours=10))
    late = NOW.replace(hour=13, minute=59, second=59)
    first = collector.collect(
        **_kwargs(tmp_path, now=late, stale_after_s=10**9), local_tz=local
    )
    assert collector.collect(
        **_kwargs(tmp_path, now=late, stale_after_s=10**9), local_tz=local
    ) is first

    tomorrow = collector.collect(
        **_kwargs(tmp_path, now=late + timedelta(seconds=2), stale_after_s=10**9),
        local_tz=local,
    )

    assert tomorrow is not first


def test_heartbeat_write_invalidates_the_cached_snapshot(tmp_path) -> None:
    collector = system.SnapshotCollector()
    _emit(tmp_path, "fix", 7)
    first = collector.collect(**_kwargs(tmp_path))

    (tmp_path / "review" / "heartbeat.json").write_text(
        '{"state": "running", "updated_at": "' + NOW.isoformat() + '"}',
        encoding="utf-8",
    )
    after = collector.collect(**_kwargs(tmp_path))

    assert after is not first
    assert [health.stale for health in after.deployment.health] == [
        False,
        False,
    ]


def test_truncated_feed_is_recollected_without_crashing(tmp_path) -> None:
    _emit(tmp_path, "fix", 7, count=30)
    collector = system.SnapshotCollector()
    collector.collect(**_kwargs(tmp_path))

    (tmp_path / "fix" / "events-v2.jsonl").write_text("", encoding="utf-8")
    after = collector.collect(**_kwargs(tmp_path))

    assert _payload(after) == _payload(system.collect_snapshot(**_kwargs(tmp_path)))
    assert after.deployment.issues == ()


def test_malformed_appended_line_matches_a_from_scratch_collect(
    tmp_path,
) -> None:
    _emit(tmp_path, "fix", 7, count=3)
    collector = system.SnapshotCollector()
    collector.collect(**_kwargs(tmp_path))

    with open(tmp_path / "fix" / "events-v2.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"schema_version": 2, "type": "nope"}\n')
        fh.write("}}}} not json\n")
    after = collector.collect(**_kwargs(tmp_path))

    from_scratch = system.collect_snapshot(**_kwargs(tmp_path))
    assert _payload(after) == _payload(from_scratch)
    assert any(
        "malformed schema-v2 event line" in warning
        for warning in after.deployment.warnings
    )


def test_absent_state_dirs_collect_without_error(tmp_path) -> None:
    collector = system.SnapshotCollector()
    kwargs = _kwargs(tmp_path)
    snapshot = collector.collect(**kwargs)

    assert _payload(snapshot) == _payload(system.collect_snapshot(**kwargs))
    assert snapshot.deployment.issues == ()


def test_polling_a_large_journal_twice_does_not_re_fold_it(tmp_path) -> None:
    """The scalability regression: idle polling cost must not track history."""

    _emit(tmp_path, "fix", 1, count=5000)
    collector = system.SnapshotCollector()

    started = time.perf_counter()
    first = collector.collect(**_kwargs(tmp_path, stale_after_s=10**9))
    cold_s = time.perf_counter() - started

    started = time.perf_counter()
    second = collector.collect(**_kwargs(tmp_path, stale_after_s=10**9))
    warm_s = time.perf_counter() - started

    assert second is first
    assert warm_s < cold_s / 20, (
        f"idle poll of a 5k-event deployment cost {warm_s:.4f}s against a "
        f"{cold_s:.4f}s cold collect; it should be stats, not a re-fold"
    )


def test_an_edited_overlay_is_not_served_from_cache(tmp_path) -> None:
    """`repo` and `target_dir` come from an operator-editable overlay; editing
    one moves no journal byte, so it must be part of the cache key."""

    _emit(tmp_path, "fix", 7)
    collector = system.SnapshotCollector()
    first = collector.collect(**_kwargs(tmp_path))

    retargeted = _kwargs(tmp_path) | {"repo": "owner/other"}
    second = collector.collect(**retargeted)

    assert second.repo == "owner/other"
    assert _payload(second) == _payload(system.collect_snapshot(**retargeted))


def test_legacy_v1_fallback_is_never_served_from_cache(tmp_path) -> None:
    """v1's `running` set is a rolling 24h window over `now`, not a calendar
    bucket, so no fingerprint can prove a cached v1 snapshot still correct."""

    paths = _journal_paths(tmp_path)
    with open(paths[0], "w", encoding="utf-8") as stream:
        stream.write(
            '{"ts": "%s", "event": "agent_run_start", "issue": 7, '
            '"dispatch": "d1"}\n' % NOW.isoformat()
        )
    collector = system.SnapshotCollector()
    first = collector.collect(**_kwargs(tmp_path))
    assert first.deployment.schema_version == 1

    second = collector.collect(**_kwargs(tmp_path))

    assert second is not first
    assert _payload(second) == _payload(system.collect_snapshot(**_kwargs(tmp_path)))


def test_concurrent_collects_agree_with_a_from_scratch_collect(tmp_path) -> None:
    """Dashboard routes are sync `def` and run in a threadpool; concurrent polls
    must not interleave into a wrong or duplicated parse."""

    from concurrent.futures import ThreadPoolExecutor

    _emit(tmp_path, "fix", 7, count=60)
    _emit(tmp_path, "review", 500, count=20)
    collector = system.SnapshotCollector()
    expected = _payload(system.collect_snapshot(**_kwargs(tmp_path)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda _: collector.collect(**_kwargs(tmp_path)), range(24))
        )

    assert all(_payload(result) == expected for result in results)


def test_source_fingerprint_tracks_sqlite_wal_sidecars(tmp_path) -> None:
    """Under WAL the .db mtime does not move on commit; the -wal file does."""

    db = tmp_path / "control.sqlite3"
    db.write_bytes(b"x")
    wal = tmp_path / "control.sqlite3-wal"
    wal.write_bytes(b"")
    paths = _journal_paths(tmp_path)
    before = system.source_fingerprint(
        journal_paths=paths,
        ledger_path=str(tmp_path / "spend.jsonl"),
        control_db_path=str(db),
    )

    wal.write_bytes(b"committed")
    os.utime(wal, ns=(0, 1))

    assert (
        system.source_fingerprint(
            journal_paths=paths,
            ledger_path=str(tmp_path / "spend.jsonl"),
            control_db_path=str(db),
        )
        != before
    )
