from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.store import Store
from issue_agent.app.roles import _hold_reporter, _stall_reporter, compose_fix_daemon
from issue_agent.app.config import HarnessConfig
from issue_agent.domain.fix import InFlightFix
from issue_agent.domain.outcome import AgentResult
from tests.app.test_roles import FakeGateway, FakeRunner, FakeWorktrees, _held_world
from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.journal import read_events


def at_seconds(world, seconds):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return replace(world, now=now.isoformat())


@pytest.mark.parametrize("hold, suffix", [
    ("overlap", "deferred: waiting on PR #123"),
    ("dependency", "deferred: blocked by #42"),
    ("inflight", "deferred: waiting on #99"),
    ("both", "deferred: blocked by #42; waiting on PR #123"),
    ("absent", "nothing is moving it"),
])
def test_stall_annotation_preserves_targets_timing_levels_and_markers(tmp_path, hold, suffix):
    observed = _held_world()
    if hold in {"dependency", "both"}:
        observed = replace(observed, issues=(replace(
            observed.issues[0], blocked_by_open=True, open_blocker_numbers=(42,),
        ),))
    if hold not in {"overlap", "both"}:
        observed = replace(observed, prs=(replace(observed.prs[0], changed_files=frozenset()),))
    inflight = (InFlightFix(99, exclusive=True),) if hold == "inflight" else ()
    store = Store(str(tmp_path))
    journal = Journal(str(tmp_path / "journal.jsonl"))
    logs = []
    holds = _hold_reporter(None, "o/r", lambda _: None)
    report = _stall_reporter(journal, store, logs.append)
    markers = None
    expected = []
    for seconds, level in [(0, 0), (1799, 0), (1800, 1), (1801, 1), (3600, 2), (3601, 2), (36060, 20)]:
        world = at_seconds(observed, seconds)
        report(world, holds(world, inflight))
        if seconds in {1800, 3600, 36060}:
            minutes = seconds // 60
            expected.extend([
                f"STALL: pr #123 unchanged for {minutes} min (level {level}) — nothing is moving it",
                f"STALL: issue #522 unchanged for {minutes} min (level {level}) — {suffix}",
            ])
        assert logs == expected
        current = (store.get_stall_marker("pr", 123), store.get_stall_marker("issue", 522))
        if markers is None:
            markers = current
        assert current == markers
    warnings = [event for event in read_events(journal.path) if event.get("event") == "telemetry_warning"]
    assert [(event["kind"], event["level"], event["age_seconds"]) for event in warnings] == [
        (kind, level, seconds)
        for seconds, level in [(1800, 1), (3600, 2), (36060, 20)]
        for kind in ("pr", "issue")
    ]


@pytest.mark.parametrize("holds", [None, {}, [], "invalid", {522: None}, {522: []}, {522: {}}, {522: 42}, {522: ""}])
def test_malformed_or_absent_stall_hold_data_keeps_original_text(tmp_path, holds):
    logs = []
    report = _stall_reporter(None, Store(str(tmp_path)), logs.append)
    observed = replace(_held_world(), prs=())
    report(at_seconds(observed, 0), holds)
    report(at_seconds(observed, 1800), holds)
    assert logs == ["STALL: issue #522 unchanged for 30 min (level 1) — nothing is moving it"]


def test_fix_daemon_stall_uses_current_deferral_and_still_fires_after_it_clears(tmp_path):
    class Gateway(FakeGateway):
        observed = at_seconds(_held_world(), 0)

        def build_world(self):
            return self.observed

    gateway = Gateway()
    logs = []
    store = Store(str(tmp_path / "state"))
    daemon = compose_fix_daemon(
        HarnessConfig(repo="o/r", fix_bot="fix-bot", target_dir=str(tmp_path),
                      state_dir=str(tmp_path / "state"),
                      control_authority_root=str(tmp_path / "authority")),
        gateway, store, FakeWorktrees(), FakeRunner(AgentResult(is_error=False)),
        log=logs.append,
    ).daemon
    daemon.tick()
    marker = store.get_stall_marker("issue", 522)
    gateway.observed = at_seconds(_held_world(), 1800)
    daemon.tick()
    assert "STALL: issue #522 unchanged for 30 min (level 1) — deferred: waiting on PR #123" in logs
    gateway.observed = at_seconds(_held_world("MERGED"), 3600)
    daemon.tick()
    assert "STALL: issue #522 unchanged for 60 min (level 2) — nothing is moving it" in logs
    assert store.get_stall_marker("issue", 522) == marker
