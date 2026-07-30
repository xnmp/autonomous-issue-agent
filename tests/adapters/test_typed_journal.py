from issue_agent.adapters.event_log import EventLog
from issue_agent.adapters.journal import Journal
from issue_agent.adapters.typed_journal import TypedJournal
from issue_agent.domain.contracts import RunId
from issue_agent.domain.events import (
    AgentRunAbandoned,
    AgentRunStallCorrected,
    Producer,
    decode_event_line,
)
from issue_agent.domain.projection import fold


def _journal(tmp_path) -> TypedJournal:
    return TypedJournal(
        Journal(str(tmp_path / "journal.jsonl")),
        EventLog(
            str(tmp_path / "events-v2.jsonl"),
            Producer("deployment", "owner/repo", "fix", 100, "boot"),
        ),
    )


def test_run_pair_has_first_class_run_and_leg_correlation(tmp_path) -> None:
    journal = _journal(tmp_path)
    common = {
        "role": "fix",
        "kind": "RESOLVE",
        "number": 7,
        "issue": 7,
        "dispatch": "dispatch-1",
        "run_id": "run-1",
        "leg_id": "leg-1",
        "model": "model",
    }
    journal.emit("agent_run_start", **common)
    journal.emit(
        "agent_run",
        **common,
        runtime="codex",
        duration_s=1.25,
        cost_usd=0.5,
        status="done",
        is_error=False,
    )

    events = tuple(
        decode_event_line(line)
        for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()
    )
    model = fold(events)

    assert events[0].correlation.run_id == "run-1"
    assert events[0].correlation.leg_id == "leg-1"
    assert model.runs["run-1"].completion == events[1]


def test_every_named_producer_maps_to_a_typed_event_without_dual_write(
    tmp_path,
) -> None:
    journal = _journal(tmp_path)
    names = (
        "action",
        "action_failed",
        "action_quarantined",
        "action_reconcile_skipped",
        "action_retry",
        "action_verified",
        "agent_run",
        "agent_run_stall",
        "agent_run_start",
        "budget_path",
        "ci_flake",
        "ci_pending_deadline",
        "ci_rerun",
        "cost_blindspot",
        "daemon_state",
        "deferred",
        "duplicate_pr_guard",
        "escalation",
        "gh_unavailable",
        "groomed",
        "handover_written",
        "path_prediction",
        "recurrence",
        "review_skip",
        "route_escalation",
        "spend",
        "spend_block",
        "stall",
        "telemetry_warning",
        "tick_gated",
    )
    fields = {
        "issue": 1,
        "pr": 2,
        "number": 1,
        "target": "issue",
        "kind": "RESOLVE",
        "state": "waiting",
        "from_tier": 1,
        "to_tier": 2,
        "started_at": "2026-07-30T00:00:00Z",
        "woke_at": "2026-07-30T00:00:01Z",
    }
    for name in names:
        journal.emit(name, **fields)

    lines = (tmp_path / "events-v2.jsonl").read_text().splitlines()
    assert len(lines) == len(names)
    assert all(decode_event_line(line).schema_version == 2 for line in lines)
    assert not (tmp_path / "journal.jsonl").exists()


def test_stall_correction_references_the_run_start_event_id(tmp_path) -> None:
    journal = _journal(tmp_path)
    common = {
        "issue": 7,
        "kind": "RESOLVE",
        "number": 7,
        "dispatch": "dispatch-1",
        "run_id": "run-1",
        "leg_id": "leg-1",
        "model": "model",
    }
    journal.emit("agent_run_start", **common)
    journal.emit(
        "agent_run",
        **common,
        runtime="codex",
        duration_s=1,
        cost_usd=0,
        status="done",
        is_error=False,
    )
    journal.emit(
        "agent_run_stall",
        **common,
        stall_id="stall-1",
        suspended_seconds=60,
    )

    events = tuple(
        decode_event_line(line)
        for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()
    )
    correction = events[-1].payload
    assert isinstance(correction, AgentRunStallCorrected)
    assert correction.corrects == events[0].event_id
    assert correction.stall_id == "stall-1"


def test_boot_recovery_abandons_persisted_start_idempotently(tmp_path) -> None:
    journal = _journal(tmp_path)
    journal.emit(
        "agent_run_start",
        issue=7,
        kind="RESOLVE",
        number=7,
        dispatch="dispatch-1",
        run_id="run-1",
        leg_id="leg-1",
        model="model",
    )

    assert journal.abandon_run(RunId("run-1"), "dead lease")
    assert journal.abandon_run(RunId("run-1"), "dead lease")

    events = tuple(
        decode_event_line(line)
        for line in (tmp_path / "events-v2.jsonl").read_text().splitlines()
    )
    corrections = [
        event for event in events if isinstance(event.payload, AgentRunAbandoned)
    ]
    assert len(corrections) == 1
    assert corrections[0].payload.corrects == events[0].event_id
