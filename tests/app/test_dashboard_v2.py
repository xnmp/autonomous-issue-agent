import json
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.event_log import EventLog
from issue_agent.app.dashboard import collect, render_html, render_text
from issue_agent.app.dashweb import service, snapshots
from issue_agent.adapters.dashweb import system
from issue_agent.domain.dashboard import Snapshot
from issue_agent.domain.events import (
    AgentRunCompleted,
    AgentRunStarted,
    Correlation,
    DaemonStateChanged,
    Producer,
    SpendRecorded,
    WorldFeedRetired,
    WorldObserved,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def test_collect_prefers_v2_fold_and_ignores_legacy_lifecycle(tmp_path) -> None:
    state = tmp_path / "fix"
    state.mkdir()
    legacy = state / "journal.jsonl"
    legacy.write_text(
        '{"ts":"2026-07-30T09:00:00Z","event":"action",'
        '"desc":"relabel issue #7: +ai-done"}\n'
    )
    events = EventLog(
        str(state / "events-v2.jsonl"),
        Producer("deployment", "owner/repo", "fix", 100, "boot"),
    )
    events.emit(
        WorldObserved(
            "issue",
            7,
            "hash",
            {"state": "open", "labels": ["ai-ready", "P2"]},
        ),
        correlation=Correlation(issue=7),
        causation_time=NOW,
    )
    correlation = Correlation(
        issue=7,
        dispatch_id="dispatch-1",
        run_id="run-1",
        leg_id="leg-1",
    )
    events.emit(
        AgentRunStarted("model", "RESOLVE"),
        correlation=correlation,
        causation_time=NOW,
    )
    events.emit(
        AgentRunCompleted(
            "done",
            False,
            None,
            1_000,
            "model",
            "codex",
            500_000,
            "USD",
            "configured_rate_card",
            None,
            None,
            "RESOLVE",
        ),
        correlation=correlation,
        causation_time=NOW,
    )

    deployment = collect(
        "demo",
        "owner/repo",
        (
            str(legacy),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
    )

    assert deployment.schema_version == 2
    assert deployment.issues[0].issue == 7
    assert deployment.issues[0].stage == "awaiting review"
    assert deployment.issues[0].lane == "pending"
    assert deployment.issues[0].done is False
    assert deployment.issues[0].cost_usd == 0.5
    assert deployment.metrics.in_flight_issues == 0


def test_collect_counts_reconciled_spend_without_a_completion_event(
    tmp_path,
) -> None:
    state = tmp_path / "fix"
    state.mkdir()
    events = EventLog(
        str(state / "events-v2.jsonl"),
        Producer("deployment", "owner/repo", "fix", 100, "boot"),
    )
    events.emit(
        WorldObserved(
            "issue",
            7,
            "hash",
            {"state": "open", "labels": ["ai-working"]},
        ),
        correlation=Correlation(issue=7),
        causation_time=NOW,
    )
    events.emit(
        SpendRecorded(
            "recovered-run",
            700_000,
            "USD",
            "reconciled",
            None,
            None,
        ),
        correlation=Correlation(issue=7, run_id="recovered-run"),
        causation_time=NOW,
    )

    deployment = collect(
        "demo",
        "owner/repo",
        (
            str(state / "journal.jsonl"),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
    )

    assert deployment.issues[0].cost_usd == 0.7
    assert deployment.metrics.today_usd == 0.7
    assert deployment.metrics.total_cost_usd == 0.7


@pytest.mark.parametrize("retirement", ["tombstone", "feed"])
def test_v2_dashboard_excludes_issues_absent_from_the_current_world(
    tmp_path,
    retirement,
) -> None:
    state = tmp_path / "fix"
    state.mkdir()
    events = EventLog(
        str(state / "events-v2.jsonl"),
        Producer("deployment", "owner/repo", "fix", 100, "boot"),
    )
    events.emit(
        WorldObserved(
            "issue",
            7,
            "present",
            {"present": True, "state": "open", "labels": ["ai-working"]},
        ),
        correlation=Correlation(issue=7),
        causation_time=NOW,
    )
    events.emit(
        (
            WorldObserved(
                "issue",
                7,
                "absent",
                {"present": False, "reason": "absent_from_feed"},
            )
            if retirement == "tombstone"
            else WorldFeedRetired(
                "deployment",
                "owner/repo",
                "fix",
                restartable=True,
            )
        ),
        correlation=Correlation(issue=7),
        causation_time=NOW,
    )

    deployment = collect(
        "demo",
        "owner/repo",
        (
            str(state / "journal.jsonl"),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
    )

    assert deployment.issues == ()
    assert deployment.metrics.in_flight_issues == 0


def test_v2_dashboard_places_current_unowned_open_issue_in_backlog(
    tmp_path,
    monkeypatch,
) -> None:
    state = tmp_path / "fix"
    state.mkdir()
    events = EventLog(
        str(state / "events-v2.jsonl"),
        Producer("deployment", "owner/repo", "fix", 100, "boot"),
    )
    events.emit(
        WorldObserved(
            "issue",
            7,
            "present",
            {
                "present": True,
                "state": "open",
                "title": "Untriaged bug",
                "labels": ["bug", "P1"],
                "comments_total": 3,
            },
        ),
        correlation=Correlation(issue=7),
        causation_time=NOW,
    )
    deployment = collect(
        "demo",
        "owner/repo",
        (
            str(state / "journal.jsonl"),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
    )
    snapshot = Snapshot(
        "demo",
        "owner/repo",
        str(tmp_path),
        deployment,
        (NOW,),
    )

    class Gh:
        def issues(self, _repo):
            return ([], "")

        def prs(self, _repo):
            return ([], "")

    backend = service.Backend(gh=Gh(), now_fn=lambda: NOW)
    monkeypatch.setattr(backend, "snapshot", lambda _name: snapshot)
    monkeypatch.setattr(backend, "verification", lambda: {"rows": []})

    payload = backend.issues("demo")

    assert payload["in_flight"] == []
    assert payload["pending"] == []
    assert payload["done"] == []
    assert payload["backlog"] == [
        {
            "issue": 7,
            "title": "Untriaged bug",
            "url": None,
            "body": None,
            "labels": ["bug", "P1"],
            "recurrence": False,
            "recurrence_count": 0,
            "age_days": None,
            "age_s": None,
            "comments": 3,
            "issue_type": "bug",
            "difficulty": None,
            "impact": None,
        }
    ]
    assert deployment.metrics.in_flight_issues == 0


def test_v2_health_uses_scheduler_heartbeat_when_events_are_quiet(
    tmp_path,
) -> None:
    state = tmp_path / "fix"
    state.mkdir()
    old = NOW - timedelta(hours=2)
    events = EventLog(
        str(state / "events-v2.jsonl"),
        Producer("deployment", "owner/repo", "fix", 100, "boot"),
    )
    events.emit(
        WorldObserved("issue", 7, "hash", {"state": "open"}),
        correlation=Correlation(issue=7),
        causation_time=old,
    )
    (state / "heartbeat.json").write_text(
        json.dumps(
            {
                "pid": 100,
                "state": "running",
                "in_flight": [],
                "updated_at": NOW.isoformat(),
            }
        )
    )

    deployment = collect(
        "demo",
        "owner/repo",
        (
            str(state / "journal.jsonl"),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
        stale_after_s=60,
    )

    fix_health = next(item for item in deployment.health if item.role == "fix")
    assert fix_health.stale is False
    assert fix_health.last_ts == NOW


def test_v2_emit_fold_issue_detail_preserves_run_drilldown_fields(
    tmp_path,
) -> None:
    state = tmp_path / "fix"
    state.mkdir()
    events = EventLog(
        str(state / "events-v2.jsonl"),
        Producer("deployment", "owner/repo", "fix", 100, "boot"),
    )
    events.emit(
        WorldObserved(
            "issue",
            7,
            "present",
            {"present": True, "state": "open", "labels": ["ai-working"]},
        ),
        correlation=Correlation(issue=7),
        causation_time=NOW,
    )
    correlation = Correlation(
        issue=7,
        dispatch_id="dispatch-1",
        run_id="run-1",
        leg_id="leg-1",
        session_id="session-1",
    )
    events.emit(
        AgentRunStarted(
            "gpt-test",
            "RESOLVE",
            {"effort": "high"},
        ),
        correlation=correlation,
        causation_time=NOW,
    )
    events.emit(
        AgentRunCompleted(
            "blocked",
            False,
            None,
            2_500,
            "gpt-test",
            "codex",
            500_000,
            "USD",
            "configured_rate_card",
            {"input_tokens": 10, "output_tokens": 2},
            "Need an API credential",
            "RESOLVE",
            {"turns": 4, "needs": "Need an API credential"},
        ),
        correlation=correlation,
        causation_time=NOW,
    )

    deployment = collect(
        "demo",
        "owner/repo",
        (
            str(state / "journal.jsonl"),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
    )

    detail = snapshots.issue_detail(deployment.issues[0], NOW)
    assert detail["attention_reason"] == "Need an API credential"
    assert detail["steps"][0]["effort"] == "high"
    assert detail["steps"][0]["turns"] == 4
    assert detail["steps"][0]["usage"] == {
        "input_tokens": 10,
        "output_tokens": 2,
    }
    assert detail["steps"][0]["session_id"] == "session-1"


def test_bad_v2_lines_warn_without_crashing_any_dashboard_renderer(
    tmp_path,
) -> None:
    state = tmp_path / "fix"
    state.mkdir()
    path = state / "events-v2.jsonl"
    path.write_text(
        '{"schema_version":2,"type":"unknown.event.v1"}\n'
        '{"schema_version":1}\n'
        '{not json\n'
    )
    path.with_suffix(".jsonl.bak").write_text("{ignored backup\n")

    deployment = collect(
        "demo",
        "owner/repo",
        (
            str(state / "journal.jsonl"),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
    )

    assert deployment.schema_version == 2
    assert deployment.warnings == (
        "skipped 3 malformed schema-v2 event line(s)",
    )
    assert "skipped 3 malformed" in render_text(
        [deployment], now_fn=lambda: NOW
    )
    assert "skipped 3 malformed" in render_html(
        [deployment], now_fn=lambda: NOW
    )

    snapshot = system.collect_snapshot(
        name="demo",
        repo="owner/repo",
        target_dir=str(tmp_path),
        journal_paths=[
            str(state / "journal.jsonl"),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ],
        log_dir=str(tmp_path / "logs"),
        ledger_path=str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
        stale_after_s=60,
    )
    overview = snapshots.overview_payload(
        snapshot, {}, scan_ok=True
    )
    assert overview["warnings"] == [
        "skipped 3 malformed schema-v2 event line(s)"
    ]


def test_fresh_deployment_without_event_file_is_defined_as_empty_v2(
    tmp_path,
) -> None:
    deployment = collect(
        "demo",
        "owner/repo",
        tuple(
            str(tmp_path / role / "journal.jsonl")
            for role in ("fix", "review", "steward")
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
    )

    assert deployment.schema_version == 2
    assert deployment.issues == ()
    assert deployment.warnings == ()
    assert all(item.stale for item in deployment.health)


def test_v2_health_rejects_stopped_heartbeat_and_terminal_state(
    tmp_path,
) -> None:
    state = tmp_path / "fix"
    state.mkdir()
    events = EventLog(
        str(state / "events-v2.jsonl"),
        Producer("deployment", "owner/repo", "fix", 100, "boot"),
    )
    events.emit(
        DaemonStateChanged("draining", "stopped", "shutdown complete"),
        causation_time=NOW,
    )
    (state / "heartbeat.json").write_text(
        json.dumps(
            {
                "pid": 100,
                "state": "stopped",
                "in_flight": [],
                "updated_at": NOW.isoformat(),
            }
        )
    )

    deployment = collect(
        "demo",
        "owner/repo",
        (
            str(state / "journal.jsonl"),
            str(tmp_path / "review" / "journal.jsonl"),
            str(tmp_path / "steward" / "journal.jsonl"),
        ),
        str(tmp_path / "logs"),
        str(tmp_path / "spend.jsonl"),
        now_fn=lambda: NOW,
        stale_after_s=1_800,
    )

    fix_health = next(item for item in deployment.health if item.role == "fix")
    assert fix_health.stale is True
