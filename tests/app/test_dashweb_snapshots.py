"""Journal-side collectors: frecency, liveness, cards, steps.

Every assertion is on what the API promises a client — a sort order, a flag, a
step's state — never on how the aggregation stores it.
"""

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.dashweb import ghdata, system
from issue_agent.adapters.store import Store
from tests.adapters.test_review_execution_control import rig as current_review_rig
from issue_agent.app.dashweb import service, snapshots
from issue_agent.domain import dashboard as dash
from issue_agent.domain.events import (
    AgentRunCompleted,
    AgentRunStarted,
    Correlation,
    EventEnvelope,
    Producer,
    SpendBlocked,
    SpendRecorded,
    TriageDispositionRecorded,
    WorldObserved,
)
from issue_agent.domain.labels import LIFECYCLE_LABELS
from issue_agent.domain.projection import fold

NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def eventually(load, predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = load()
        if predicate(value):
            return value
        time.sleep(0.01)
    raise AssertionError("GitHub enrichment did not reach a later poll")


def ts(**delta) -> str:
    return (NOW - timedelta(**delta)).replace(microsecond=0).isoformat()


def build(events, name="demo", repo="o/r"):
    feeds = (("fix", "/j/fix", "/l/fix", events), ("review", "/j/rev", "/l/rev", []),
             ("steward", "/j/std", "/l/std", []))
    return dash.build_deployment(name, repo, feeds, (), now_fn=lambda: NOW)


def v2_event(payload, event_id, seq, *, role="fix", **correlation):
    stamp = NOW - timedelta(hours=1)
    return EventEnvelope(
        payload,
        event_id,
        seq,
        stamp,
        stamp,
        seq,
        Producer("demo", "o/r", role, 10, "boot"),
        Correlation(**correlation),
    )


def v2_deployment(events, name="demo", repo="o/r"):
    """A deployment built the way the daemons build one: schema-v2 events only,
    folded, with no journal-dict or GitHub input."""
    feeds = (
        ("fix", "/j/fix", "/l/fix", tuple(events)),
        ("review", "/j/rev", "/l/rev", ()),
        ("steward", "/j/std", "/l/std", ()),
    )
    return dash.build_deployment_v2(
        name, repo, feeds, fold(events), lambda: NOW
    )


def row_for(events, issue):
    dep = build(events)
    return next(r for r in dep.issues if r.issue == issue)


def row_view(events, legs=(), issue=7):
    """A row whose feed is exactly `events`, in order.

    The v2 fold attributes an event to an issue by correlation; the legacy
    dict builder can only re-parse a prose `desc`, so it cannot express the
    structured actions the current journal emits.
    """
    row = row_for([*legs] or [RESOLVE], issue)
    return replace(row, events=tuple(events))


# ── frecency ──────────────────────────────────────────────────────────────────
def test_frecency_weights_recent_activity_above_bulk_history():
    fresh = [NOW - timedelta(hours=1)] * 2
    stale = [NOW - timedelta(days=20)] * 6
    assert snapshots.frecency_score(fresh, NOW) > snapshots.frecency_score(stale, NOW)


def test_frecency_ignores_events_outside_every_window():
    assert snapshots.frecency_score([NOW - timedelta(days=400)], NOW) == 0.0


def test_frecency_tolerates_unparseable_timestamps():
    assert snapshots.frecency_score([None, NOW], NOW) == snapshots.frecency_score([NOW], NOW)


def test_normalise_frecency_scales_to_the_busiest_deployment():
    assert snapshots.normalise_frecency({"a": 8.0, "b": 2.0}) == {"a": 1.0, "b": 0.25}


def test_normalise_frecency_on_an_idle_fleet_is_all_zero_not_a_crash():
    assert snapshots.normalise_frecency({"a": 0.0, "b": 0.0}) == {"a": 0.0, "b": 0.0}


# ── liveness ──────────────────────────────────────────────────────────────────
def test_deployment_name_is_read_off_the_composed_env_path():
    assert system.deployment_of_env("/h/config/.active.my-repo.env") == "my-repo"
    assert system.deployment_of_env("/h/config/shared.env") == ""
    assert system.deployment_of_env("") == ""


def test_deployment_name_is_read_off_the_systemd_service_env_path():
    # The service units point AGENT_ENV at ~/.config/issue-agent/<name>.env,
    # not the point-at composed snapshot. Every systemd deployment rendered
    # active:false until this arm existed (keifu, 2026-08-02).
    assert (
        system.deployment_of_env("/home/u/.config/issue-agent/keifu.env")
        == "keifu"
    )
    # Repo overlays and dotfiles in other directories must NOT read as live
    # daemons: the parent directory has to be literally `issue-agent`.
    assert system.deployment_of_env("/h/config/repos/keifu.env") == ""
    assert system.deployment_of_env("/home/u/.config/issue-agent/.hidden.env") == ""
    assert system.deployment_of_env("/home/u/.config/issue-agent/keifu.txt") == ""


def test_system_adapter_lists_configured_deployments(tmp_path):
    (tmp_path / "b.env").write_text("REPO=o/b\n")
    (tmp_path / "a.env").write_text("REPO=o/a\n")
    (tmp_path / "notes.txt").write_text("ignored\n")
    assert system.deployment_names(str(tmp_path)) == ("a", "b")
    assert system.deployment_names(str(tmp_path / "missing")) == ()


def test_collected_snapshot_reports_the_deployment_specific_log_paths(tmp_path):
    journals = []
    for role in dash.ROLE_NAMES:
        path = tmp_path / f"{role}.jsonl"
        path.write_text("")
        journals.append(str(path))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")

    snapshot = system.collect_snapshot(
        name="demo",
        repo="o/r",
        target_dir="/tmp/demo",
        journal_paths=journals,
        log_dir=str(tmp_path / "logs"),
        ledger_path=str(ledger),
        now_fn=lambda: NOW,
        stale_after_s=60,
    )

    assert [health.log_path for health in snapshot.deployment.health] == [
        str(tmp_path / "logs" / f"{role}-demo.log") for role in dash.ROLE_NAMES
    ]


def _fake_proc(tmp_path, pids):
    for pid, (env, argv) in pids.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "environ").write_bytes(b"\0".join(e.encode() for e in env) + b"\0")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    return str(tmp_path)


def test_proc_scan_maps_each_deployment_to_its_live_roles(tmp_path):
    root = _fake_proc(tmp_path, {
        101: (["AGENT_ENV=/h/config/.active.demo.env", "HOME=/h"],
              ["python", "-m", "issue_agent.app.main", "fix"]),
        102: (["AGENT_ENV=/h/config/.active.demo.env"],
              ["python", "-m", "issue_agent.app.main", "review"]),
        103: (["PATH=/usr/bin"], ["sleep", "1"]),
    })
    live, ok = system.scan_proc_daemons(root)
    assert ok
    assert live == {"demo": {"fix", "review"}}


def test_proc_scan_reports_a_launcher_only_deployment_as_active_with_no_roles(tmp_path):
    root = _fake_proc(tmp_path, {
        7: (["AGENT_ENV=/h/config/.active.demo.env"], ["bash", "bin/point-at.sh", "demo"]),
    })
    live, ok = system.scan_proc_daemons(root)
    assert ok and live == {"demo": set()}


def test_proc_scan_failure_is_reported_not_swallowed(tmp_path):
    live, ok = system.scan_proc_daemons(str(tmp_path / "nope"))
    assert (live, ok) == ({}, False)


def test_repos_payload_reports_unknown_liveness_without_proc():
    dep = build([{"ts": ts(minutes=1), "event": "tick", "role": "fix"}])
    snap = dash.Snapshot("demo", "o/r", "/t", dep, (NOW,))
    payload = snapshots.repos_payload([snap], {}, False, NOW)
    assert payload["liveness"] == "unknown"
    assert payload["warnings"]
    assert payload["repos"][0]["active"] is None
    assert all(role["alive"] is None for role in payload["repos"][0]["roles"])


def test_repos_payload_is_pre_sorted_by_frecency():
    quiet = dash.Snapshot("quiet", "o/q", "", build([]), (NOW - timedelta(days=20),))
    busy = dash.Snapshot("busy", "o/b", "", build([]), (NOW, NOW))
    payload = snapshots.repos_payload([quiet, busy], {}, True, NOW)
    assert [r["name"] for r in payload["repos"]] == ["busy", "quiet"]
    assert payload["repos"][0]["frecency"] == 1.0


# ── steps ─────────────────────────────────────────────────────────────────────
RESOLVE = {"ts": ts(hours=3), "event": "agent_run", "role": "fix", "kind": "RESOLVE",
           "issue": 7, "number": 7, "cost_usd": 2.0, "status": "done",
           "exit_reason": "ok", "model": "claude-sonnet-5", "session_id": "abc",
           "turns": 12, "usage": {"input_tokens": 10}}
FIX_CI_1 = {"ts": ts(hours=2), "event": "agent_run", "role": "fix", "kind": "FIX_CI",
            "number": 12, "issue": 7, "cost_usd": 1.0, "exit_reason": "wall_cap"}
FIX_CI_2 = {"ts": ts(hours=1), "event": "agent_run", "role": "fix", "kind": "FIX_CI",
            "number": 12, "issue": 7, "cost_usd": 0.5, "exit_reason": "ok"}


def test_collapsed_steps_keep_one_entry_per_kind_with_the_latest_state():
    steps = snapshots.collapse_steps(row_for([RESOLVE, FIX_CI_1, FIX_CI_2], 7))
    by_kind = {s["kind"]: s for s in steps}
    assert set(by_kind) == {"RESOLVE", "FIX_CI"}
    assert by_kind["FIX_CI"]["state"] == "ok"


def test_collapsed_steps_sum_a_kinds_retries_so_repair_spend_is_not_understated():
    steps = snapshots.collapse_steps(row_for([RESOLVE, FIX_CI_1, FIX_CI_2], 7))
    assert next(s for s in steps if s["kind"] == "FIX_CI")["cost_usd"] == 1.5


def test_a_running_leg_is_appended_as_a_running_step():
    events = [RESOLVE, {"ts": ts(minutes=5), "event": "agent_run_start", "role": "fix",
                        "kind": "REVIEW", "number": 12, "issue": 7, "dispatch": "d1"}]
    steps = snapshots.collapse_steps(row_for(events, 7))
    assert steps[-1] == {"kind": "REVIEW", "state": "running", "status": None,
                         "cost_usd": 0.0, "count": 1}


def test_the_strip_keeps_a_recurring_kind_as_separate_episodes():
    # review → fix → review again is the story; the old one-entry-per-kind
    # collapse folded both reviews into one chip and erased it.
    review_1 = {"ts": ts(hours=3), "event": "agent_run", "role": "review",
                "kind": "REVIEW", "number": 12, "issue": 7, "cost_usd": 0.2,
                "status": "REQUEST_CHANGES", "exit_reason": "ok"}
    fix = {"ts": ts(hours=2), "event": "agent_run", "role": "fix",
           "kind": "ADDRESS_REVIEW", "number": 12, "issue": 7, "cost_usd": 1.0,
           "exit_reason": "ok"}
    review_2 = {"ts": ts(hours=1), "event": "agent_run", "role": "review",
                "kind": "REVIEW", "number": 12, "issue": 7, "cost_usd": 0.3,
                "status": "APPROVE", "exit_reason": "ok"}
    steps = snapshots.collapse_steps(row_for([review_1, fix, review_2], 7))
    assert [(s["kind"], s["count"]) for s in steps] == [
        ("REVIEW", 1), ("ADDRESS_REVIEW", 1), ("REVIEW", 1)
    ]
    assert steps[0]["status"] == "REQUEST_CHANGES"
    assert steps[2]["status"] == "APPROVE"


def test_consecutive_retries_fold_into_one_episode_with_count_and_summed_cost():
    steps = snapshots.collapse_steps(row_for([RESOLVE, FIX_CI_1, FIX_CI_2], 7))
    episode = next(s for s in steps if s["kind"] == "FIX_CI")
    assert episode["count"] == 2
    assert episode["cost_usd"] == 1.5
    assert episode["state"] == "ok"


def test_the_strip_interleaves_an_intervention_where_it_happened():
    mid_unblock = {
        "ts": ts(hours=2, minutes=30), "event": "action", "issue": 7,
        "role": "dashboard",
        "desc": "operator unblock issue #7",
        "action": {"type": "operator_unblock", "target": "ISSUE", "number": 7,
                   "add": [], "remove": ["needs-human-review"]},
    }
    row = row_view([mid_unblock], legs=[RESOLVE, FIX_CI_1, FIX_CI_2])
    assert [(s["kind"], s["count"]) for s in snapshots.collapse_steps(row)] == [
        ("RESOLVE", 1), ("unblock", 1), ("FIX_CI", 2)
    ]


def test_detailed_steps_keep_every_retry_and_carry_the_drill_down_fields():
    steps = snapshots.detailed_steps(row_for([RESOLVE, FIX_CI_1, FIX_CI_2], 7))
    assert [s["kind"] for s in steps] == ["RESOLVE", "FIX_CI", "FIX_CI"]
    assert [s["state"] for s in steps] == ["ok", "failed", "ok"]
    assert steps[0]["turns"] == 12
    assert steps[0]["usage"] == {"input_tokens": 10}
    assert steps[0]["has_transcript"] is True
    assert steps[1]["has_transcript"] is False


def test_detailed_step_recovers_model_from_its_matching_start():
    start = {
        "ts": ts(hours=3),
        "event": "agent_run_start",
        "role": "review",
        "kind": "REVIEW",
        "number": 12,
        "issue": 7,
        "dispatch": "review-1",
        "model": "claude-opus-4-1",
    }
    settled = {
        "ts": ts(hours=2),
        "event": "agent_run",
        "role": "review",
        "kind": "REVIEW",
        "number": 12,
        "issue": 7,
        "dispatch": "review-1",
        "status": "APPROVE",
        "exit_reason": "ok",
    }

    assert snapshots.detailed_steps(row_for([start, settled], 7))[0]["model"] == "claude-opus-4-1"


def test_detailed_review_step_uses_the_model_reported_at_settlement():
    start = {
        "ts": ts(hours=3), "event": "agent_run_start", "role": "review",
        "kind": "REVIEW", "number": 12, "issue": 7, "dispatch": "review-1", "model": "",
    }
    settled = {
        "ts": ts(hours=2), "event": "agent_run", "role": "review", "kind": "REVIEW",
        "number": 12, "issue": 7, "dispatch": "review-1", "status": "APPROVE",
        "exit_reason": "ok", "model": "claude-opus-4-1",
    }

    assert snapshots.detailed_steps(row_for([start, settled], 7))[0]["model"] == "claude-opus-4-1"


def test_a_leg_that_exited_for_any_reason_but_ok_reads_as_failed():
    leg = dash.RunSummary(None, "fix", "RESOLVE", 1, 1, "m", "", "budget_exhausted", 0.0, 0.0, False, "")
    assert snapshots.leg_state(leg) == "failed"


def test_an_advisory_route_budget_overrun_is_a_warning_not_a_failure():
    route = {
        "ts": ts(hours=2), "event": "agent_run", "role": "fix", "kind": "ROUTE",
        "issue": 7, "number": 7, "exit_reason": "budget_exhausted",
        "is_error": True, "summary": "router result retained",
    }

    row = row_for([route], 7)

    assert snapshots.collapse_steps(row)[0]["state"] == "advisory"
    step = snapshots.detailed_steps(row)[0]
    assert step["state"] == "advisory"
    assert step["note"] == "budget_exhausted — router result retained"


# (exit_reason, is_error, failed?) — codex settles a clean leg as `ok`, claude
# settles it empty, so an exit reason's presence is not a failure signal (#158).
@pytest.mark.parametrize(
    "exit_reason,is_error,failed",
    [
        pytest.param("ok", False, False, id="codex-clean"),
        pytest.param("OK", False, False, id="clean-uppercased"),
        pytest.param("", False, False, id="claude-clean"),
        pytest.param("", True, True, id="error-without-reason"),
        pytest.param("budget_exhausted", True, True, id="budget"),
        pytest.param("runtime_unavailable", False, True, id="runtime-unavailable"),
        pytest.param("agent_error", False, True, id="agent-error"),
        pytest.param("transport_death", False, True, id="infra-death"),
        pytest.param("!" + "x" * 100_000, False, True, id="unknown-huge-reason"),
    ],
)
def test_leg_state_keys_on_the_error_signal_not_the_exit_reasons_presence(
    exit_reason, is_error, failed
):
    leg = dash.RunSummary(
        ts=None, role="steward", kind="TRIAGE", number=7, issue=7, model="gpt-5",
        status="done", exit_reason=exit_reason, duration_s=1.0, cost_usd=0.68,
        estimated=False, session_id="s", is_error=is_error,
    )
    assert snapshots.leg_state(leg) == ("failed" if failed else "ok")


def test_a_blocked_leg_with_a_clean_codex_exit_reason_still_reads_as_blocked():
    leg = dash.RunSummary(
        ts=None, role="fix", kind="RESOLVE", number=7, issue=7, model="gpt-5",
        status="blocked", exit_reason="ok", duration_s=1.0, cost_usd=0.1,
        estimated=False, session_id="s", is_error=False,
    )
    assert snapshots.leg_state(leg) == "blocked"


def test_step_window_events_are_bounded_by_the_legs_dispatch_and_settlement():
    events = [
        {"ts": ts(hours=5), "event": "action", "desc": "relabel issue #7: +ai-working"},
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix", "kind": "RESOLVE",
         "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=1), "event": "action", "desc": "comment pr #12"},
    ]
    row = row_for(events, 7)
    window = snapshots.step_window_events(row, 0)
    assert [e["event"] for e in window] == ["agent_run_start", "agent_run"]


def test_step_window_events_for_an_out_of_range_index_is_empty_not_an_error():
    assert snapshots.step_window_events(row_for([RESOLVE], 7), 99) == []


# ── cards ─────────────────────────────────────────────────────────────────────
def test_issue_card_reports_elapsed_to_now_while_in_flight():
    events = [{"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
               "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"}]
    card = snapshots.issue_card(row_for(events, 7), NOW)
    assert card["elapsed_s"] == 7200
    assert card["done"] is False
    assert card["stage"] == "resolving"


def test_issue_card_stops_the_clock_once_the_issue_is_done():
    events = [
        RESOLVE,
        {"ts": ts(hours=1), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]
    card = snapshots.issue_card(row_for(events, 7), NOW)
    assert card["done"] is True
    assert card["elapsed_s"] == 7200


def test_issue_card_counts_only_task_runtime_as_working_time():
    # keifu #34 (2026-08-02): "working 23:35" while the issue sat awaiting
    # merge. Working time is the sum of the task durations the drill-down
    # lists; every hour the issue merely lived — waiting on CI, review slots,
    # dependencies — is idle.
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=3), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 3600},
        {"ts": ts(), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 14_400
    assert card["working_s"] == 3_600
    assert card["idle_s"] == 10_800
    assert card["idle_active"] is False


def test_an_issue_between_tasks_ticks_idle_not_working():
    events = [
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=1, minutes=30), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 1800},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 7_200
    assert card["working_s"] == 1_800
    assert card["idle_s"] == 5_400
    assert card["idle_active"] is True


def test_a_running_task_ticks_the_work_clock():
    events = [
        {"ts": ts(minutes=30), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["working_s"] == 1_800
    assert card["idle_s"] == 0
    assert card["idle_active"] is False


def test_a_stall_during_a_task_is_removed_from_working_time():
    # The machine slept through the leg's window: the leg's runtime covers the
    # nap, so the correction comes out of working, not idle.
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=2), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 7200},
        {"ts": ts(hours=1), "event": "agent_run_stall", "role": "fix",
         "issue": 7, "dispatch": "d1", "stall_started_at": ts(hours=3.5),
         "suspended_seconds": 3600},
        {"ts": ts(), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 10_800
    assert card["working_s"] == 3_600
    assert card["idle_s"] == 7_200


def test_identified_stall_cluster_outside_tasks_shortens_only_idle():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=3), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 3600},
        {"ts": ts(hours=1), "event": "agent_run_stall", "role": "fix",
         "issue": 7, "stall_started_at": ts(hours=2),
         "stall_clock_offset_seconds": 1234, "suspended_seconds": 3600},
        {"ts": ts(), "event": "agent_run_stall", "role": "review",
         "issue": 7, "stall_started_at": ts(minutes=30),
         "stall_clock_offset_seconds": 1235, "suspended_seconds": 1800},
        {"ts": ts(), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 10_800
    assert card["working_s"] == 3_600
    assert card["idle_s"] == 7_200


def test_legacy_stall_cluster_outside_tasks_shortens_only_idle():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=3), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 3600},
        {"ts": ts(minutes=15), "event": "agent_run_stall", "role": "fix",
         "issue": 7, "stall_started_at": ts(hours=1.25),
         "suspended_seconds": 3600},
        {"ts": ts(), "event": "agent_run_stall", "role": "review",
         "issue": 7, "stall_started_at": ts(minutes=30),
         "suspended_seconds": 1800},
        {"ts": ts(), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 10_800
    assert card["working_s"] == 3_600
    assert card["idle_s"] == 7_200


def test_issue_card_excludes_annotated_stall_time():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=3), "event": "agent_run_stall", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "stall_started_at": ts(hours=3.5), "suspended_seconds": 1200},
        {"ts": ts(hours=2), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]
    row = row_for(events, 7)

    assert snapshots.issue_card(row, NOW)["elapsed_s"] == 6000


def test_duplicate_leg_annotations_for_one_stall_are_subtracted_once():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=3), "event": "agent_run_stall", "issue": 7,
         "dispatch": "d1", "stall_started_at": ts(hours=3.5),
         "suspended_seconds": 1200},
        {"ts": ts(hours=3), "event": "agent_run_stall", "issue": 7,
         "dispatch": "d2", "stall_started_at": ts(hours=3.5),
         "suspended_seconds": 1200},
        {"ts": ts(hours=2), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    assert snapshots.issue_card(row_for(events, 7), NOW)["elapsed_s"] == 6000


def test_overlapping_cross_role_stall_estimates_are_subtracted_once():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=3), "event": "agent_run_stall", "role": "fix",
         "issue": 7, "dispatch": "d1", "stall_started_at": ts(hours=3.5),
         "suspended_seconds": 1200},
        {"ts": ts(hours=3), "event": "agent_run_stall", "role": "review",
         "issue": 7, "dispatch": "d2",
         "stall_started_at": ts(hours=3, minutes=29),
         "suspended_seconds": 1200},
        {"ts": ts(hours=2), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    assert snapshots.issue_card(row_for(events, 7), NOW)["elapsed_s"] == 6000


def test_nonoverlapping_role_estimates_for_same_physical_stall_subtract_once():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=3), "event": "agent_run_stall", "role": "fix",
         "issue": 7, "dispatch": "d1", "stall_started_at": ts(hours=3.5),
         "stall_clock_offset_seconds": 1_234_567.0,
         "suspended_seconds": 1200},
        {"ts": ts(hours=3), "event": "agent_run_stall", "role": "review",
         "issue": 7, "dispatch": "d2", "stall_started_at": ts(hours=2.5),
         "stall_clock_offset_seconds": 1_234_567.8,
         "suspended_seconds": 1200},
        {"ts": ts(hours=2), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    row = row_for(events, 7)
    assert snapshots.issue_card(row, NOW)["elapsed_s"] == 6000


@pytest.mark.parametrize("malformed_seconds", [float("inf"), 10**400])
def test_nonfinite_or_oversized_stall_correction_is_ignored(malformed_seconds):
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=3), "event": "agent_run_stall", "role": "fix",
         "issue": 7, "dispatch": "d1", "stall_started_at": ts(hours=3.5),
         "stall_clock_offset_seconds": 1_234_567.0,
         "suspended_seconds": malformed_seconds},
        {"ts": ts(hours=2), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    row = row_for(events, 7)
    assert snapshots.issue_card(row, NOW)["elapsed_s"] == 7200


def test_issue_detail_uses_the_same_working_idle_partition_as_cards():
    events = [
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=1), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 3600},
    ]

    detail = snapshots.issue_detail(row_for(events, 7), NOW)

    assert detail["working_s"] == 3_600
    assert detail["idle_s"] == 3_600
    assert detail["idle_active"] is True


def test_backlog_dwell_before_admission_is_not_pipeline_idle():
    # keifu #185 (2026-08-03): filed and triaged at 00:24, admitted and routed
    # at 01:25. The card showed idle 1:00 the instant its resolve began. The
    # clock anchors at the first fix-pipeline leg; the triage's own five
    # minutes still count as work, the backlog hour around it counts as
    # nothing.
    events = [
        {"ts": ts(hours=3), "event": "agent_run_start", "role": "steward",
         "kind": "TRIAGE", "issue": 7, "number": 7, "dispatch": "t1"},
        {"ts": ts(hours=2, minutes=55), "event": "agent_run", "role": "steward",
         "kind": "TRIAGE", "issue": 7, "number": 7, "dispatch": "t1",
         "cost_usd": 0.1, "status": "done", "exit_reason": "ok",
         "duration_s": 300},
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "ROUTE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=1), "event": "agent_run", "role": "fix",
         "kind": "ROUTE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 3600},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["working_s"] == 3_900
    assert card["idle_s"] == 3_600
    assert card["elapsed_s"] == 7_500
    assert card["idle_active"] is True


def test_a_triaged_issue_waiting_in_the_backlog_reports_no_idle():
    events = [
        {"ts": ts(hours=3), "event": "agent_run_start", "role": "steward",
         "kind": "TRIAGE", "issue": 7, "number": 7, "dispatch": "t1"},
        {"ts": ts(hours=2, minutes=55), "event": "agent_run", "role": "steward",
         "kind": "TRIAGE", "issue": 7, "number": 7, "dispatch": "t1",
         "cost_usd": 0.1, "status": "done", "exit_reason": "ok",
         "duration_s": 300},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 300
    assert card["working_s"] == 300
    assert card["idle_s"] == 0
    assert card["idle_active"] is False


def test_a_groom_leg_is_pre_admission_like_triage():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "steward",
         "kind": "GROOM", "issue": 7, "number": 7, "dispatch": "g1"},
        {"ts": ts(hours=3, minutes=50), "event": "agent_run", "role": "steward",
         "kind": "GROOM", "issue": 7, "number": 7, "dispatch": "g1",
         "cost_usd": 0.1, "status": "done", "exit_reason": "ok",
         "duration_s": 600},
        {"ts": ts(hours=1), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(minutes=30), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 1800},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 4_200  # 600 grooming + the hour since resolve
    assert card["working_s"] == 2_400
    assert card["idle_s"] == 1_800


def test_the_clock_anchors_on_the_first_leg_not_the_first_journal_line():
    # Was: elapsed measured from the row's first event, so any journal line
    # emitted before admission (a relabel, an observation) started the idle
    # clock. Anchoring on the first fix-pipeline leg means the hour between
    # the label and the dispatch is charged to nobody.
    events = [
        {"ts": ts(hours=3), "event": "action", "role": "steward",
         "desc": "relabel issue #7: +ai-ready"},
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=1), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 3600},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 7_200
    assert card["working_s"] == 3_600
    assert card["idle_s"] == 3_600


def test_a_stall_before_admission_does_not_shorten_the_pipeline_clock():
    # The suspension correction applies to the anchored span only; the
    # pre-admission contribution is leg runtime and has no gaps to correct.
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "steward",
         "kind": "TRIAGE", "issue": 7, "number": 7, "dispatch": "t1"},
        {"ts": ts(hours=3, minutes=55), "event": "agent_run", "role": "steward",
         "kind": "TRIAGE", "issue": 7, "number": 7, "dispatch": "t1",
         "cost_usd": 0.1, "status": "done", "exit_reason": "ok",
         "duration_s": 300},
        {"ts": ts(hours=3), "event": "agent_run_stall", "role": "steward",
         "issue": 7, "dispatch": "t1", "stall_started_at": ts(hours=3.5),
         "suspended_seconds": 1800},
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=1), "event": "agent_run", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1",
         "cost_usd": 2.0, "status": "done", "exit_reason": "ok",
         "duration_s": 3600},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 7_500
    assert card["working_s"] == 3_900
    assert card["idle_s"] == 3_600


def test_issue_card_carries_gh_title_pr_state_and_merge_stamp():
    events = [RESOLVE, {"ts": ts(hours=1), "event": "action",
                        "desc": "clear state for pr #12, issue #7 (merged)"}]
    card = snapshots.issue_card(
        row_for(events, 7), NOW,
        gh_issue={"title": "the bug", "url": "u/7"},
        gh_prs={12: {"url": "u/12", "state": "MERGED", "mergedAt": "2026-07-29T10:00:00Z"}},
    )
    assert card["title"] == "the bug"
    assert (card["pr"], card["pr_state"]) == (12, "merged")
    assert card["merged_ts"] == "2026-07-29T10:00:00Z"


def test_issue_card_without_gh_shows_numbers_only_never_a_stale_title():
    card = snapshots.issue_card(row_for([RESOLVE], 7), NOW)
    assert card["title"] is None and card["url"] is None and card["pr_state"] is None


def test_a_journalled_escalation_outranks_a_later_hold_on_the_board():
    events = [
        RESOLVE,
        {"ts": ts(hours=2), "event": "escalation", "target": "issue", "number": 7},
        {"ts": ts(hours=2), "event": "recurrence", "issue": 7},
        {"ts": ts(hours=2), "event": "spend_block", "issue": 7},
        {"ts": ts(hours=1), "event": "hold", "issue": 7, "reason": "overlaps PR #12"},
    ]
    card = snapshots.issue_card(row_for(events, 7), NOW)
    assert ask_of(card) in ESCALATED_ASKS
    # Without the escalation the hold is what the row waits on.
    held = snapshots.issue_card(row_for([events[0], events[-1]], 7), NOW)
    assert board_of(held)["status"] == "blocked"


def test_historical_hold_is_cleared_by_a_later_resolve_dispatch():
    events = [
        {"ts": ts(hours=3), "event": "hold", "issue": 7, "reason": "overlap"},
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7},
    ]
    assert board_of(snapshots.issue_card(row_for(events, 7), NOW))["status"] != "blocked"


def test_structured_defer_stays_active_until_its_clear_transition():
    waiting = {
        "ts": ts(hours=3), "event": "deferred", "state": "waiting",
        "issue": 7, "reason": "overlap", "blocker_pr": 90,
    }
    cleared = {**waiting, "ts": ts(hours=1), "state": "cleared"}
    assert snapshots.pending_card({}, row_for([waiting], 7))["hold_reason"] == "waiting on PR #90"
    assert snapshots.pending_card({}, row_for([waiting, cleared], 7))["held"] is False


def test_dependency_hold_reads_blocked_by_not_waiting_on():
    # A GitHub blocked-by dependency hold names its blocker in the operator's
    # own words ("blocked by #185" — keifu #130/#157, 2026-08-03); the overlap
    # hold keeps "waiting on" because its blocker is our own in-flight work.
    blocker = {
        "ts": ts(hours=3), "event": "deferred", "state": "waiting",
        "issue": 130, "reason": "blocker", "blocker_issue": 185,
    }
    overlap = {**blocker, "reason": "overlap"}
    assert snapshots.pending_card(
        {}, row_for([blocker], 130)
    )["hold_reason"] == "blocked by #185"
    assert snapshots.pending_card(
        {}, row_for([overlap], 130)
    )["hold_reason"] == "waiting on #185"


def test_structured_clear_supersedes_a_legacy_hold():
    legacy = {
        "ts": ts(hours=4), "event": "hold", "issue": 7,
        "reason": "overlap", "blocker": "pr#90",
    }
    waiting = {
        "ts": ts(hours=3), "event": "deferred", "state": "waiting",
        "issue": 7, "reason": "overlap", "blocker_pr": 90,
    }
    cleared = {**waiting, "ts": ts(hours=1), "state": "cleared"}

    assert snapshots.pending_card(
        {}, row_for([legacy, waiting, cleared], 7)
    )["held"] is False


def test_ai_ready_issue_with_historical_activity_is_pending_not_in_flight(monkeypatch):
    snapshot = dash.Snapshot("demo", "o/r", "/target", build([RESOLVE]), (NOW,))

    class Gh:
        def issues(self, _repo):
            return ([{
                "number": 7,
                "state": "OPEN",
                "labels": [{"name": "ai-ready"}],
                "title": "queued again",
            }], "")

        def prs(self, _repo):
            return ([], "")

    backend = service.Backend(gh=Gh(), now_fn=lambda: NOW)
    monkeypatch.setattr(backend, "snapshot", lambda _name: snapshot)
    monkeypatch.setattr(backend, "verification", lambda: {"rows": []})

    first = backend.issues("demo")
    assert [card["issue"] for card in first["in_flight"]] == [7]

    payload = eventually(
        lambda: backend.issues("demo"),
        lambda current: bool(current["pending"]),
    )
    assert [card["issue"] for card in payload["pending"]] == [7]
    assert payload["in_flight"] == []


def test_projected_cost_groups_completed_issues_by_resolve_model():
    events = [
        {**RESOLVE, "issue": 7, "number": 7, "model": "gpt-5.6-terra", "cost_usd": 2.0},
        {"ts": ts(hours=2), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
        {**RESOLVE, "ts": ts(hours=3), "issue": 8, "number": 8,
         "model": "gpt-5.6-sol", "cost_usd": 9.0},
        {"ts": ts(hours=2), "event": "action",
         "desc": "clear state for pr #13, issue #8 (merged)"},
    ]
    assert snapshots.projected_cost_by_model(build(events).issues) == {
        "gpt-5.6-terra": 2.0,
        "gpt-5.6-sol": 9.0,
    }


def test_pending_card_reads_priority_and_the_newest_hold():
    events = [{"ts": ts(hours=1), "event": "hold", "issue": 9, "reason": "overlap"}]
    row = row_for(events, 9)
    card = snapshots.pending_card(
        {"number": 9, "title": "t", "url": "u",
         "labels": [{"name": "ai-ready"}, {"name": "P2"}]}, row)
    assert card["priority"] == "P2"
    assert (card["held"], card["hold_reason"]) == (True, "waiting on dependency")
    assert card["overlap"] is None


def test_pending_card_with_no_journal_history_is_not_held():
    card = snapshots.pending_card({"number": 9, "labels": ["ai-ready"]}, None)
    assert (card["held"], card["priority"]) == (False, None)


def test_a_parked_issue_renders_as_a_held_pending_row_with_the_parked_reason():
    card = snapshots.pending_card(
        {"number": 34, "labels": ["ai-parked", "ai-triage"]}, None)
    assert (card["held"], card["hold_reason"]) == (True, "parked")


def test_a_parked_pending_card_keeps_its_pr_link_and_ci_state():
    row = replace(row_for([RESOLVE], 7), prs=(179,))
    card = snapshots.pending_card(
        {"number": 7, "labels": ["ai-parked", "ai-triage"]},
        row,
        gh_prs={
            179: {
                "url": "https://github.com/o/r/pull/179",
                "state": "OPEN",
                "statusCheckRollup": [
                    {"conclusion": "SUCCESS"},
                    {"conclusion": "NEUTRAL"},
                ],
            }
        },
    )

    assert card["pr"] == 179
    assert card["pr_url"] == "https://github.com/o/r/pull/179"
    assert card["pr_state"] == "open"
    assert card["pr_ci_state"] == "passing"


def test_a_parked_issue_leaves_the_in_flight_lane_for_pending():
    # Park moves an issue off the active board even mid-history: an operator
    # decision outranks whatever legs or holds the journal accumulated.
    dep = v2_deployment([
        v2_event(AgentRunStarted("m", kind="RESOLVE"), "a", 0, issue=34, run_id="r"),
        v2_event(
            AgentRunCompleted(
                "done", False, "ok", 1000, "m", "codex",
                cost_micros=750_000, currency="USD", pricing_basis="api",
                kind="RESOLVE",
            ),
            "b", 1, issue=34, run_id="r",
        ),
        v2_event(
            WorldObserved(
                "issue", 34, "h",
                {"present": True, "state": "OPEN",
                 "labels": ["ai-parked", "ai-triage"]},
            ),
            "c", 2, issue=34,
        ),
    ])
    assert dep.issues[0].lane == "pending"


def test_pending_card_carries_the_issue_body_for_the_drill_down():
    card = snapshots.pending_card(
        {"number": 9, "labels": ["ai-ready"], "body": "repro steps"}, None)
    assert card["body"] == "repro steps"
    assert snapshots.pending_card({"number": 9, "labels": []}, None)["body"] is None


def test_backlog_excludes_any_issue_the_harness_lifecycle_already_owns():
    assert snapshots.is_backlog({"labels": [{"name": "bug"}]}) is True
    assert snapshots.is_backlog({"labels": [{"name": "ai-working"}]}) is False


@pytest.mark.parametrize("label", sorted(label.value for label in LIFECYCLE_LABELS))
def test_backlog_uses_the_canonical_lifecycle_label_set(label):
    """#144: the dashboard kept its own copy of the lifecycle set and omitted
    `ai-pr-open`, so an issue with a live PR showed in both In Flight and
    Backlog. Parametrized over the canonical set so a lifecycle label added
    later cannot drift out of the dashboard's definition again."""
    assert snapshots.is_backlog({"labels": [{"name": label}]}) is False


def test_backlog_card_ages_the_issue_and_marks_recurrences():
    card = snapshots.backlog_card(
        {"number": 3, "title": "t", "url": "u", "body": "context", "createdAt": ts(days=12),
         "labels": [{"name": "recurrence"}], "comments": 4},
        NOW,
    )
    assert (card["age_days"], card["recurrence"], card["comments"]) == (12, True, 4)
    assert card["body"] == "context" and card["recurrence_count"] == 1


def test_backlog_card_uses_journal_recurrence_count_when_available():
    recurrence = {"ts": ts(hours=1), "event": "recurrence", "issue": 3}
    row = row_for([recurrence, {**recurrence, "ts": ts(hours=2)}], 3)
    card = snapshots.backlog_card(
        {"number": 3, "labels": [{"name": "recurrence"}]},
        NOW,
        row,
    )
    assert card["recurrence_count"] == 2


@pytest.mark.parametrize(
    ("labels", "expected"),
    [([], "raw"), (["ai-triage"], "processed")],
)
def test_backlog_card_preserves_pre_marker_steward_states(labels, expected):
    card = snapshots.backlog_card({"number": 3, "labels": labels}, NOW)

    assert card["steward_state"] == expected


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["blocked_on_human_reply"], "blocked_on_human_reply"),
        (["blocked_on_human_reply", "processed"], "processed"),
    ],
)
def test_backlog_card_uses_the_latest_folded_triage_disposition(states, expected):
    events = [
        v2_event(
            WorldObserved(
                "issue", 3, "issue-hash",
                {"present": True, "state": "OPEN", "labels": ["ai-triage"]},
            ),
            "world", 0, issue=3,
        ),
        *[
            v2_event(
                TriageDispositionRecorded(state),
                f"triage-{index}", index, role="steward", issue=3,
            )
            for index, state in enumerate(states, start=1)
        ],
    ]
    row = v2_deployment(events).issues[0]
    card = snapshots.backlog_card(
        {"number": 3, "labels": ["ai-triage"]}, NOW, row,
    )

    assert card["steward_state"] == expected
    assert snapshots.backlog_card(
        {"number": 3, "labels": []}, NOW, row,
    )["steward_state"] == "raw"


@pytest.mark.parametrize(
    ("comments", "expected"),
    [
        (
            [{"body": "**A quick check before we start** 🛠️\n\nQuestion"}],
            "blocked_on_human_reply",
        ),
        (
            [
                {"body": "**A quick check before we start** 🛠️\n\nQuestion"},
                {"body": "**Ready to build ✅**\n\nThanks"},
            ],
            "processed",
        ),
        ([{"body": ["malformed"]}, "not a comment"], "processed"),
    ],
)
def test_backlog_card_migrates_pre_marker_state_from_folded_comments(
    comments, expected,
):
    row = v2_deployment([
        v2_event(
            WorldObserved(
                "issue", 3, "issue-hash",
                {
                    "present": True,
                    "state": "OPEN",
                    "labels": ["ai-triage"],
                    "comments": comments,
                },
            ),
            "world", 0, issue=3,
        ),
    ]).issues[0]

    assert snapshots.backlog_card(
        {"number": 3, "labels": ["ai-triage"]}, NOW, row,
    )["steward_state"] == expected


def test_backlog_card_survives_a_missing_created_at_and_list_shaped_comments():
    card = snapshots.backlog_card({"number": 3, "comments": [{}, {}]}, NOW)
    assert card["age_days"] is None and card["comments"] == 2


# ── overview ──────────────────────────────────────────────────────────────────
def test_health_trend_groups_target_issue_lifecycle_and_recurrence_by_native_day():
    issues = [
        {"number": 1, "createdAt": "2026-07-28T23:30:00-02:00", "closedAt": None},
        {"number": 2, "createdAt": "2026-07-29T01:00:00Z", "closedAt": "2026-07-30T04:00:00Z"},
        # A malformed external record must not make the advisory panel disappear.
        {"number": 3, "createdAt": "not-a-date", "closedAt": "also-not-a-date"},
    ]
    recurrences = [
        {"event": "recurrence", "ts": "2026-07-30T23:30:00-02:00", "issue": 2},
        {"event": "recurrence", "ts": "2026-07-31T01:00:00Z", "issue": 2},
    ]

    assert snapshots.health_trend(
        issues,
        recurrences,
        first_observed_day="2026-07-29",
        local_tz=timezone.utc,
    ) == [
        {"day": "2026-07-29", "opened": 2, "closed": 0, "net_open": 2,
         "recurrences": 0, "first_observation": True},
        {"day": "2026-07-30", "opened": 0, "closed": 1, "net_open": -1,
         "recurrences": 0, "first_observation": False},
        {"day": "2026-07-31", "opened": 0, "closed": 0, "net_open": 0,
         "recurrences": 2, "first_observation": False},
    ]


def test_health_trend_is_empty_without_issue_activity_or_recurrence():
    assert snapshots.health_trend([], [], first_observed_day="2026-07-29") == []


def test_health_trend_uses_the_native_timezone_day_boundary():
    local = timezone(timedelta(hours=10))
    rows = snapshots.health_trend(
        [{"createdAt": "2026-07-29T16:00:00Z", "closedAt": None}],
        [],
        first_observed_day="2026-07-30",
        local_tz=local,
    )
    assert rows == [{
        "day": "2026-07-30",
        "opened": 1,
        "closed": 0,
        "net_open": 1,
        "recurrences": 0,
        "first_observation": True,
    }]


def test_v2_today_spend_uses_the_injected_local_day_boundary():
    local = timezone(timedelta(hours=10))
    now = datetime(2026, 7, 2, 0, 30, tzinfo=timezone.utc)
    stamp = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
    event = EventEnvelope(
        SpendRecorded("run", 700_000, "USD", "configured", "card", "v1"),
        "spend-local-day",
        1,
        stamp,
        stamp,
        1,
        Producer("demo", "o/r", "fix", 10, "boot"),
        Correlation(run_id="run"),
    )
    feeds = (
        ("fix", "/j/fix", "/l/fix", (event,)),
        ("review", "/j/rev", "/l/rev", ()),
        ("steward", "/j/std", "/l/std", ()),
    )

    deployment = dash.build_deployment_v2(
        "demo",
        "o/r",
        feeds,
        fold((event,)),
        lambda: now,
        to_local_date=lambda timestamp: datetime.fromtimestamp(
            timestamp, local
        ).date(),
    )

    assert deployment.metrics.today_usd == 0.7


def test_overview_projects_lifecycle_recurrence_and_first_observed_day(monkeypatch):
    recurrence = {"ts": "2026-07-30T01:00:00Z", "event": "recurrence", "issue": 7}
    deployment = build([recurrence])
    snapshot = dash.Snapshot("demo", "o/r", "/target", deployment, (NOW,))

    class Gh:
        def issues(self, repo):
            assert repo == "o/r"
            return ([{"createdAt": "2026-07-29T01:00:00Z", "closedAt": "2026-07-30T02:00:00Z"}], "")

    backend = service.Backend(gh=Gh())
    monkeypatch.setattr(backend, "snapshot", lambda _name: snapshot)
    monkeypatch.setattr(backend, "names", lambda: ("demo",))
    monkeypatch.setattr(system, "scan_proc_daemons", lambda: ({}, True))

    payload = backend.overview("demo")
    assert payload["first_observed_day"] == "2026-07-29"
    assert payload["health_trend"] == [
        {"day": "2026-07-29", "opened": 1, "closed": 0, "net_open": 1,
         "recurrences": 0, "first_observation": True},
        {"day": "2026-07-30", "opened": 0, "closed": 1, "net_open": -1,
         "recurrences": 1, "first_observation": False},
    ]


def test_overview_warns_when_the_github_history_limit_is_reached(monkeypatch):
    snapshot = dash.Snapshot("demo", "o/r", "/target", build([]), ())

    class Gh:
        def issues(self, _repo):
            return ([{}] * ghdata.ISSUE_HISTORY_LIMIT, "")

    backend = service.Backend(gh=Gh())
    monkeypatch.setattr(backend, "snapshot", lambda _name: snapshot)
    monkeypatch.setattr(backend, "names", lambda: ("demo",))
    monkeypatch.setattr(system, "scan_proc_daemons", lambda: ({}, True))

    assert "may be incomplete" in backend.overview("demo")["warnings"][-1]


def test_overview_marks_only_the_selected_deployments_first_observed_day(monkeypatch):
    target = dash.Snapshot(
        "target",
        "o/r",
        "/target",
        build([]),
        (NOW - timedelta(days=2), NOW),
    )

    class Gh:
        def issues(self, _repo):
            return ([{"createdAt": ts(days=2), "closedAt": None}], "")

    backend = service.Backend(gh=Gh())
    monkeypatch.setattr(backend, "snapshot", lambda _name: target)
    monkeypatch.setattr(system, "scan_proc_daemons", lambda: ({}, True))

    assert backend.overview("target")["health_trend"] == [
        {"day": "2026-07-27", "opened": 1, "closed": 0, "net_open": 1,
         "recurrences": 0, "first_observation": True},
    ]


def test_cost_mix_splits_spend_into_the_four_leg_families():
    metrics = dash.build_metrics(
        build([RESOLVE, FIX_CI_1,
               {"ts": ts(hours=1), "event": "agent_run", "role": "review",
                "kind": "REVIEW", "number": 12, "issue": 7, "cost_usd": 3.0,
                "status": "APPROVE"}]).issues,
        (), 0.0,
    )
    mix = snapshots.cost_mix(metrics)
    assert mix == {"resolve_usd": 2.0, "review_usd": 3.0, "repair_usd": 1.0,
                   "steward_usd": 0.0}


def test_metrics_json_exposes_first_pass_rate_as_none_before_any_verdict():
    metrics = dash.build_metrics(build([RESOLVE]).issues, (), 0.0)
    assert snapshots.metrics_json(metrics)["first_pass_rate"] is None


def test_metrics_json_carries_the_intervention_split_and_ratio():
    """The overview payload is the dashboard's only view of the metric, so the
    per-class split has to survive serialisation, not just the total."""
    unblock = {
        "ts": ts(hours=1),
        "event": "action",
        "role": "fix",
        "event_id": "op-1",
        "command_id": "operator-unblock:1",
        "desc": "operator unblock issue #7: -ai-blocked",
        "action": {
            "type": "operator_unblock",
            "operator": "o/r:dashboard",
            "add": [],
            "remove": ["ai-blocked"],
        },
    }
    metrics = dash.build_metrics(build([RESOLVE, unblock]).issues, (), 0.0)

    payload = snapshots.metrics_json(metrics)

    assert payload["interventions_total"] == 1
    assert payload["interventions_by_class"] == [
        {"name": "rearm", "count": 1},
        {"name": "policy", "count": 0},
        {"name": "other", "count": 0},
    ]
    assert payload["interventions_per_merged_pr"] is None  # nothing merged yet


# ── settled-leg stage in the fold ─────────────────────────────────────────────
# The third mapping site for the #158 class, and the one an operator actually
# reads: the stage the fold puts on the issue row. Claude settles a clean leg
# with an empty exit reason and codex settles it `ok`, so keying failure on the
# field's presence rendered whole healthy codex batches as "run failed".
@pytest.mark.parametrize(
    "exit_reason,is_error,stage",
    [
        pytest.param("ok", False, "working", id="codex-clean"),
        pytest.param("OK", False, "working", id="clean-uppercased"),
        pytest.param(None, False, "working", id="claude-clean"),
        pytest.param("ok", True, "run failed", id="error-despite-clean-reason"),
        pytest.param("budget_exhausted", True, "run failed", id="budget"),
        pytest.param("agent_error", False, "run failed", id="agent-error"),
        pytest.param("wall_cap", False, "run failed", id="infra-death"),
        pytest.param("!" + "x" * 10_000, False, "run failed", id="huge-unknown"),
    ],
)
def test_the_fold_stages_a_settled_leg_by_its_error_signal(
    exit_reason, is_error, stage
):
    dep = v2_deployment([
        v2_event(AgentRunStarted("m", kind="TRIAGE"), "a", 0, issue=7, run_id="r"),
        v2_event(
            AgentRunCompleted(
                "done", is_error, exit_reason, 1000, "m", "codex",
                kind="TRIAGE", cost_micros=680_000, currency="USD",
                pricing_basis="api",
            ),
            "b", 1, issue=7, run_id="r",
        ),
        _observed(7, ["ai-working"], 2),
    ])

    # A clean settle renders the issue's lifecycle state; only an error signal
    # renders "run failed".
    assert dep.issues[0].stage == stage


def test_a_clean_codex_leg_does_not_flag_the_card_as_failed():
    dep = v2_deployment([
        v2_event(AgentRunStarted("m", kind="RESOLVE"), "a", 0, issue=7, run_id="r"),
        v2_event(
            AgentRunCompleted(
                "done", False, "ok", 1000, "m", "codex", kind="RESOLVE",
                cost_micros=680_000, currency="USD", pricing_basis="api",
            ),
            "b", 1, issue=7, run_id="r",
        ),
        _observed(7, ["ai-pr-open"], 2),
    ])

    card = snapshots.issue_card(dep.issues[0], NOW)

    assert card["stage"] == "awaiting review"
    assert [step["state"] for step in card["steps"]] == ["ok"]


# ── escalation reasons ────────────────────────────────────────────────────────
def _deployment_of(*rows):
    return replace(build([RESOLVE]), issues=tuple(rows))


def test_the_overview_aggregates_a_reason_per_escalated_issue():
    dep = _deployment_of(
        row_view([BREAKER_COMMENT, ESCALATION], issue=7),
        replace(row_view([BREAKER_COMMENT, ESCALATION], issue=7), issue=9),
    )

    assert [item["issue"] for item in snapshots.escalation_reasons(dep)] == [7, 9]
    assert snapshots.escalation_reasons(dep)[0]["reason"] == (
        "Circuit breaker: 5 review rounds on this PR ended without a verdict "
        "— a human must decide."
    )


def test_an_escalation_whose_reason_was_never_recorded_is_still_listed():
    dep = _deployment_of(row_view([ESCALATION], issue=7))

    assert snapshots.escalation_reasons(dep) == [{"issue": 7, "reason": None}]


def test_a_retired_escalation_leaves_the_overview_list():
    dep = _deployment_of(row_view([
        BREAKER_COMMENT,
        ESCALATION,
        {"ts": ts(minutes=30), "event": "action", "issue": 7,
         "desc": "relabel issue #7: -needs-human-review",
         "action": {"type": "relabel", "target": "ISSUE", "number": 7,
                    "add": [], "remove": ["needs-human-review"]}},
    ], issue=7))

    assert snapshots.escalation_reasons(dep) == []


def _observed_view(
    labels, *, hours, kind="issue", number=7, present=True, truncated=False
):
    # ts() counts BACKWARDS: smaller `hours` is later. ESCALATION sits at
    # ts(hours=2); observations after it use hours<2, before it hours>2.
    facts = {"present": present, "labels": list(labels)}
    if truncated:
        facts["observation_truncated"] = True
    return {
        "ts": ts(hours=hours), "event": "world.observed.v1", "issue": 7,
        "subject_kind": kind, "subject_number": number,
        "facts": facts,
    }


def test_a_label_removed_out_of_band_retires_the_escalation():
    # GitHub UI / raw-API removals journal no action event; the observed
    # world is the retirement authority (tauri #650, 2026-08-10).
    row = row_view([
        BREAKER_COMMENT, ESCALATION,
        _observed_view(["ai-pr-open"], hours=1),
    ])

    assert snapshots.escalation_reasons(_deployment_of(row)) == []
    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is None


def test_an_observation_from_before_the_escalation_cannot_retire_it():
    row = row_view([
        _observed_view(["ai-ready"], hours=3),
        BREAKER_COMMENT, ESCALATION,
    ])

    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is not None


def test_an_observation_still_carrying_the_label_keeps_the_escalation():
    row = row_view([
        BREAKER_COMMENT, ESCALATION,
        _observed_view(["needs-human-review"], hours=1),
    ])

    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is not None


def test_a_stale_clearing_observation_self_heals_on_the_next_poll():
    # A fetch that started before the label landed can publish after the
    # escalation event; observation is level-triggered so the next fresh
    # poll restores the affordance instead of losing it forever.
    row = row_view([
        BREAKER_COMMENT, ESCALATION,
        _observed_view(["ai-pr-open"], hours=1),
        _observed_view(["needs-human-review"], hours=0),
    ])

    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is not None


def test_a_truncated_observation_proves_presence_but_never_absence():
    # world_reporter clips label lists to five when facts overflow; a
    # clipped list omitting needs-human-review says nothing about the hold.
    clipped = row_view([
        BREAKER_COMMENT, ESCALATION,
        _observed_view(["a", "b", "c", "d", "e"], hours=1, truncated=True),
    ])
    held = row_view([
        BREAKER_COMMENT, ESCALATION,
        _observed_view(["ai-pr-open"], hours=1),
        _observed_view(["needs-human-review"], hours=0, truncated=True),
    ])

    assert snapshots.issue_detail(clipped, NOW)["escalation_reason"] is not None
    assert snapshots.issue_detail(held, NOW)["escalation_reason"] is not None


def test_a_pr_escalation_survives_issue_observations_but_not_pr_ones():
    # F6: the issue can look healthy while the PR still carries the hold.
    pr_escalation = {**ESCALATION, "target": "pr", "number": 12}
    held = row_view([
        BREAKER_COMMENT, pr_escalation,
        _observed_view(["ai-pr-open"], hours=1),
    ])
    released = row_view([
        BREAKER_COMMENT, pr_escalation,
        _observed_view(["ai-pr-open"], hours=1, kind="pr", number=12),
    ])

    assert snapshots.issue_detail(held, NOW)["escalation_reason"] is not None
    assert snapshots.issue_detail(released, NOW)["escalation_reason"] is None


def test_a_tombstoned_observation_does_not_retire_the_escalation():
    row = row_view([
        BREAKER_COMMENT, ESCALATION,
        _observed_view([], hours=1, present=False),
    ])

    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is not None


def test_a_finished_issue_is_not_still_escalated_on_the_overview():
    row = replace(row_view([BREAKER_COMMENT, ESCALATION], issue=7), done=True)

    assert snapshots.escalation_reasons(_deployment_of(row)) == []


def test_the_tile_sub_lines_and_the_issue_card_state_the_same_reason():
    row = row_view([BREAKER_COMMENT, ESCALATION], issue=7)

    assert snapshots.escalation_reasons(_deployment_of(row))[0]["reason"] == (
        snapshots.issue_detail(row, NOW)["escalation_reason"]
    )


# ── needs attention ───────────────────────────────────────────────────────────
def _observed(issue: int, labels, seq: int, *, title="a title", present=True):
    return v2_event(
        WorldObserved(
            "issue", issue, f"h{issue}",
            {"present": present, "state": "OPEN", "title": title,
             "labels": list(labels)},
        ),
        f"w{issue}-{seq}", seq, issue=issue,
    )


def _attention(events):
    return snapshots.needs_attention(v2_deployment(events))


def test_needs_attention_names_the_issue_and_why_it_is_waiting():
    items = _attention([
        _observed(7, ["ai-blocked"], 0),
        _observed(8, ["needs-human-review"], 1, title="escalated one"),
        _observed(9, ["needs-budget-review", "ai-working"], 2),
    ])

    assert [(item["issue"], item["kind"]) for item in items] == [
        (7, "blocked"), (8, "escalated"), (9, "needs-budget-review"),
    ]
    assert items[1]["title"] == "escalated one"


def test_a_blocked_issue_carries_the_question_the_agent_actually_asked():
    items = _attention([
        _observed(7, ["ai-blocked"], 0),
        v2_event(AgentRunStarted("m", kind="RESOLVE"), "s", 1, issue=7, run_id="r"),
        v2_event(
            AgentRunCompleted(
                "blocked", False, "ok", 1000, "m", "codex",
                kind="RESOLVE", cost_micros=0, currency="USD",
                pricing_basis="api",
                attributes={
                    "needs": "Which database should the migration target?\nmore"
                },
            ),
            "c", 2, issue=7, run_id="r",
        ),
    ])

    assert [item["kind"] for item in items] == ["blocked"]
    assert items[0]["detail"] == "Which database should the migration target?"


def test_one_issue_waiting_for_two_reasons_is_reported_as_both():
    items = _attention([
        _observed(7, ["ai-blocked", "needs-budget-review"], 0),
    ])

    assert [item["kind"] for item in items] == ["blocked", "needs-budget-review"]


def test_a_parked_issue_is_reported_as_held_with_the_operators_reason():
    items = _attention([_observed(7, ["ai-parked"], 0)])

    assert [(item["kind"], item["detail"]) for item in items] == [("held", "parked")]


def test_the_attention_panel_is_ordered_by_urgency_then_issue_number():
    items = _attention([
        _observed(30, ["ai-parked"], 0),
        _observed(20, ["needs-human-review"], 1),
        _observed(12, ["ai-blocked"], 2),
        _observed(11, ["ai-blocked"], 3),
    ])

    assert [item["kind"] for item in items] == [
        "blocked", "blocked", "escalated", "held",
    ]
    assert [item["issue"] for item in items][:2] == [11, 12]


def test_a_finished_or_untouched_issue_needs_no_attention():
    assert _attention([_observed(7, ["ai-done"], 0)]) == []
    assert _attention([_observed(7, [], 0)]) == []


def test_a_resolved_spend_block_drops_out_of_current_attention():
    events = [
        v2_event(
            SpendBlocked("issue", "exhausted", 2_000_000, 1_000_000, "USD", "cap"),
            "spend-block",
            0,
            issue=7,
        ),
        _observed(7, ["ai-working"], 1),
    ]
    deployment = v2_deployment(events)

    assert deployment.issues[0].spend_blocks == 1
    assert snapshots.needs_attention(deployment) == []


def test_needs_attention_tolerates_an_issue_with_no_observed_labels():
    """A row the world reporter never described must not crash the overview —
    it simply has nothing to say about who is waiting."""
    dep = v2_deployment([
        v2_event(AgentRunStarted("m", kind="RESOLVE"), "a", 0, issue=7, run_id="r"),
    ])

    assert snapshots.needs_attention(dep) == []


def test_the_pending_card_and_the_attention_panel_share_one_hold_definition():
    row = v2_deployment([_observed(7, ["ai-parked"], 0)]).issues[0]
    card = snapshots.pending_card(
        {"number": 7, "labels": ["ai-parked", "ai-ready"]}, row
    )

    assert card["held"] is True
    assert card["hold_reason"] == "parked"
    assert snapshots.hold_state(row, ["ai-parked"]) == (True, "parked")


# ── operator-actions timeline ─────────────────────────────────────────────────
def _operator_event(ts_value, action, **extra):
    return {"ts": ts_value, "event": "action", "role": "fix", "action": action,
            **extra}


UNBLOCK_WITH_NOTE = _operator_event(
    ts(hours=2),
    {
        "type": "operator_unblock", "operator": "o/r:dashboard",
        "add": [], "remove": ["ai-blocked"], "labels_applied": True,
        "retries_cleared": True, "guidance": "set",
    },
    event_id="a", issue=7,
)
PROMOTE = _operator_event(
    ts(hours=1),
    {"type": "operator_relabel", "operator": "o/r:dashboard",
     "add": ["ai-ready"], "remove": []},
    event_id="b", issue=8,
)
API_UNBLOCK = _operator_event(
    ts(hours=3),
    {"type": "operator_unblock", "operator": "o/r:dashboard",
     "add": [], "remove": ["ai-blocked"], "labels_applied": True,
     "channel": "api"},
    event_id="c", issue=9, pr=90,
)


def _timeline(events):
    metrics = dash.build_metrics((), tuple(events), 0.0)
    return snapshots.intervention_timeline(metrics)


def test_the_operator_timeline_is_newest_first_and_says_what_was_done():
    rows = _timeline([UNBLOCK_WITH_NOTE, PROMOTE, API_UNBLOCK])

    assert [row["issue"] for row in rows] == [8, 7, 9]
    assert [row["action_class"] for row in rows] == ["policy", "guidance", "rearm"]
    assert rows[1]["detail"] == (
        "removed ai-blocked; reset retry counters; with scope note"
    )
    assert rows[0]["kind"] == "relabel (operator)"


def test_an_api_driven_action_is_never_shown_as_a_humans_judgement_call():
    row = _timeline([API_UNBLOCK])[0]

    assert row["kind"] == "unblock (api)"
    assert (row["issue"], row["pr"]) == (9, 90)


def test_the_operator_timeline_is_bounded_to_one_screenful():
    events = [
        _operator_event(
            ts(minutes=index),
            {"type": "operator_unblock", "operator": "o/r:dashboard"},
            event_id=f"e{index}", issue=index + 1,
        )
        for index in range(400)
    ]

    rows = snapshots.intervention_timeline(dash.build_metrics((), tuple(events), 0.0))

    assert len(rows) == snapshots.INTERVENTION_TIMELINE_LIMIT
    # Newest first means the most recent action survives the cut, not the first.
    assert rows[0]["ts"] == ts(minutes=0)


def test_a_deployment_nobody_touched_has_an_empty_timeline_not_a_missing_one():
    payload = snapshots.overview_payload(
        dash.Snapshot("demo", "o/r", "/t", build([RESOLVE]), ()), {}, True
    )

    assert payload["interventions"] == []
    assert payload["unattributed_count"] == 0


def test_the_overview_reports_uncorrelated_events_as_a_count_not_as_rows():
    payload = snapshots.overview_payload(
        dash.Snapshot(
            "demo", "o/r", "/t",
            build([RESOLVE, {"ts": ts(hours=1), "event": "tick_gated"}]), (),
        ),
        {}, True,
    )

    assert payload["unattributed_count"] == 1
    assert "unattributed" not in payload


@pytest.mark.parametrize("events", [[], [{"garbage": True}], [{"ts": "not-a-date"}]])
def test_a_deployment_with_no_usable_events_still_produces_a_payload(events):
    dep = build(events)
    snap = dash.Snapshot("demo", "o/r", "/t", dep, ())
    payload = snapshots.repos_payload([snap], {}, True, NOW)
    assert payload["repos"][0]["in_flight"] == 0
    assert snapshots.overview_payload(snap, {}, True)["metrics"]["total_cost_usd"] == 0.0


# ── escalation reason ─────────────────────────────────────────────────────────
BREAKER_COMMENT = {
    "ts": ts(hours=2), "event": "action", "issue": 7, "role": "review",
    "desc": "comment pr #12",
    "action": {
        "type": "comment", "target": "PR", "number": 12,
        "body": "**Circuit breaker:** 5 review rounds on this PR ended "
                "without a verdict — a human must decide.\nsecond line",
    },
}
ESCALATION = {"ts": ts(hours=2), "event": "escalation", "issue": 7, "pr": 12}


def test_escalation_reason_is_the_comment_the_escalating_pass_posted():
    card = snapshots.issue_detail(row_view([BREAKER_COMMENT, ESCALATION]), NOW)
    assert card["escalation_reason"] == (
        "Circuit breaker: 5 review rounds on this PR ended without a verdict "
        "— a human must decide."
    )


def test_escalation_reason_reads_the_ci_pending_stall_body():
    # The pending-stall path escalates via escalate_ci_pending, which carries
    # its reason in its OWN body and posts no separate comment — keying on
    # "comment" alone left those escalations reasonless and the UI fell back
    # to a generic line (keifu PRs #182/#183, 2026-08-03).
    stall = {
        "ts": ts(hours=2), "event": "action", "issue": 7, "role": "fix",
        "desc": "escalate stalled CI PR #12",
        "action": {
            "type": "escalate_ci_pending", "pr": 12, "head": "abc1234",
            "body": "CI has been pending for 60 minutes without settling "
                    "(Test (windows-latest)) — a stuck check pins this PR "
                    "indefinitely.\ndetail line",
        },
    }
    card = snapshots.issue_detail(row_view([stall, ESCALATION]), NOW)
    assert card["escalation_reason"] == (
        "CI has been pending for 60 minutes without settling "
        "(Test (windows-latest)) — a stuck check pins this PR indefinitely."
    )


def test_an_escalation_with_no_comment_beside_it_has_no_reason():
    card = snapshots.issue_detail(row_view([ESCALATION]), NOW)
    assert card["escalation_reason"] is None


def _UNPARK(hours: float) -> dict:
    # The real journal shape: the display line names only the park label; the
    # actual moves live in the structured attributes (keifu #130, 2026-08-03 —
    # the row stayed "escalated" with the stale CI-pending reason after an
    # unpark re-armed it and CI went green).
    return {
        "ts": ts(hours=hours), "event": "action", "issue": 7,
        "desc": "operator unpark issue #7: -ai-parked",
        "action": {
            "type": "operator_unpark", "target": "ISSUE", "number": 7,
            "add": ["ai-pr-open"], "remove": ["ai-parked"],
        },
    }


def test_operator_unpark_retires_the_escalation_and_its_reason():
    stall = {
        "ts": ts(hours=3), "event": "action", "issue": 7, "role": "fix",
        "desc": "escalate stalled CI PR #12",
        "action": {
            "type": "escalate_ci_pending", "pr": 12, "head": "abc1234",
            "body": "CI has been pending for 60 minutes without settling.",
        },
    }
    row = row_view([stall, ESCALATION, _UNPARK(hours=1)])
    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is None
    assert ask_of(snapshots.issue_card(row, NOW)) is None


def test_the_escalation_stands_until_the_unpark_actually_happens():
    card = snapshots.issue_card(row_view([ESCALATION]), NOW)
    assert ask_of(card) in ESCALATED_ASKS


def test_a_daemon_relabel_does_not_strip_the_reason_off_a_live_escalation():
    # -ai-blocked +ai-working lands when the operator replies, while GitHub
    # may still carry needs-human-review on the PR — the escalation is live
    # and its reason (and unblock affordance) must survive (adversarial
    # review F6, 2026-08-03).
    stall = {
        "ts": ts(hours=3), "event": "action", "issue": 7, "role": "fix",
        "desc": "escalate stalled CI PR #12",
        "action": {
            "type": "escalate_ci_pending", "pr": 12, "head": "abc1234",
            "body": "CI has been pending for 60 minutes without settling.",
        },
    }
    resume = {
        "ts": ts(hours=1), "event": "action", "issue": 7,
        "desc": "relabel issue #7: +ai-working -ai-blocked",
    }
    card = snapshots.issue_detail(row_view([stall, ESCALATION, resume]), NOW)
    assert card["escalation_reason"] == (
        "CI has been pending for 60 minutes without settling."
    )


def test_issue_only_unpark_keeps_a_live_awaiting_reply_stamp():
    # An unpark that re-armed no PR journals add=[] (real moves only). The
    # PR-level ai-blocked stamp — a question still awaiting the operator's
    # answer — must survive it (adversarial review F5, 2026-08-03: a
    # fabricated +ai-pr-open wiped it).
    blocked = {
        "ts": ts(hours=3), "event": "action", "issue": 7,
        "desc": "relabel pr #12: +ai-blocked",
    }
    unpark = {
        "ts": ts(hours=1), "event": "action", "issue": 7,
        "desc": "operator unpark issue #7: -ai-parked",
        "action": {
            "type": "operator_unpark", "target": "ISSUE", "number": 7,
            "add": [], "remove": ["ai-parked"],
        },
    }
    card = snapshots.issue_card(row_view([blocked, unpark]), NOW)
    assert ask_of(card) == "awaiting_reply"


def test_escalation_reason_ignores_a_comment_about_another_target():
    other = {**BREAKER_COMMENT,
             "action": {**BREAKER_COMMENT["action"], "number": 99}}
    assert snapshots.issue_detail(row_view([other, ESCALATION]), NOW)[
        "escalation_reason"
    ] is None


def test_escalation_reason_ignores_a_comment_many_events_away():
    filler = [
        {"ts": ts(hours=2), "event": "action", "issue": 7,
         "desc": f"relabel issue #7: +ai-working{n}"}
        for n in range(6)
    ]
    row = row_view([BREAKER_COMMENT, *filler, ESCALATION])
    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is None


@pytest.mark.parametrize("body", [None, "", "   ", {"not": "a string"}])
def test_a_malformed_escalation_comment_body_yields_no_reason(body):
    broken = {**BREAKER_COMMENT,
              "action": {**BREAKER_COMMENT["action"], "body": body}}
    assert snapshots.issue_detail(row_view([broken, ESCALATION]), NOW)[
        "escalation_reason"
    ] is None


def test_a_reason_retires_with_the_escalation_that_produced_it():
    """The clients gate their unblock affordance on this field: once a relabel
    has taken needs-human-review off, there is nothing left to unblock."""
    cleared = {"ts": ts(hours=1), "event": "action", "issue": 7,
               "desc": "relabel issue #7: -needs-human-review"}
    row = row_view([BREAKER_COMMENT, ESCALATION, cleared])
    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is None


def test_a_finished_issue_reports_no_escalation_reason():
    row = replace(row_view([BREAKER_COMMENT, ESCALATION]), done=True)
    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is None


def test_a_body_less_comment_does_not_shadow_the_real_reason():
    empty = {**BREAKER_COMMENT,
             "action": {**BREAKER_COMMENT["action"], "body": ""}}
    row = row_view([BREAKER_COMMENT, empty, ESCALATION])
    assert snapshots.issue_detail(row, NOW)["escalation_reason"].startswith(
        "Circuit breaker:"
    )


def test_two_comments_equally_close_resolve_to_the_later_one():
    later = {**BREAKER_COMMENT,
             "action": {**BREAKER_COMMENT["action"], "body": "**Later:** this one"}}
    row = row_view([BREAKER_COMMENT, ESCALATION, later])
    assert snapshots.issue_detail(row, NOW)["escalation_reason"] == "Later: this one"


def test_an_issue_with_no_escalation_at_all_has_no_reason():
    row = row_view([BREAKER_COMMENT])
    assert snapshots.issue_detail(row, NOW)["escalation_reason"] is None


# ── the clock stops at human attention ────────────────────────────────────────
ESCALATED_ISSUE = {
    "title": "wedged",
    "labels": [{"name": "needs-human-review"}],
    "updatedAt": ts(hours=2),
}


def test_working_time_stops_accruing_once_the_issue_is_on_a_human():
    events = [
        {"ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        ESCALATION,
    ]
    card = snapshots.issue_card(row_for(events, 7), NOW, gh_issue=ESCALATED_ISSUE)

    assert ask_of(card) in ESCALATED_ASKS
    assert card["elapsed_s"] == 3_600  # 3h ago → escalated 2h ago
    assert card["board"]["since_ts"] == ts(seconds=7_200)  # and still waiting, live


def test_the_clock_freezes_without_any_github_label_view():
    """keifu PRs #173/#177 (2026-08-02): the escalation relabel lived on the
    PR, and the schema-v2 enrichment strips PRs to title/url — so the row read
    "escalated" while the work clock ran to NOW. The journal alone must freeze
    the clock."""
    events = [
        {"ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=2), "event": "action", "role": "review", "issue": 7,
         "desc": "relabel pr #12: -ai-pr-open +needs-human-review"},
    ]
    card = snapshots.issue_card(
        row_view(events), NOW,
        gh_issue={"title": "t", "url": "u"},   # v2 enrichment: no labels
        gh_prs={12: {"title": "t", "url": "u"}},
    )

    assert ask_of(card) in ESCALATED_ASKS
    assert card["elapsed_s"] == 3_600
    assert card["board"]["since_ts"] == ts(seconds=7_200)


def test_a_lifecycle_add_resumes_the_clock_after_an_escalation():
    events = [
        {"ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=2), "event": "action", "role": "review", "issue": 7,
         "desc": "relabel pr #12: -ai-pr-open +needs-human-review"},
        {"ts": ts(hours=1), "event": "action", "role": "fix", "issue": 7,
         "desc": "relabel issue #7: -ai-ready +ai-working"},
    ]
    card = snapshots.issue_card(row_view(events), NOW)
    assert ask_of(card) is None


def test_an_operator_unblock_event_resumes_the_clock():
    events = [
        {"ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=2), "event": "action", "role": "review", "issue": 7,
         "desc": "relabel pr #12: -ai-pr-open +needs-human-review"},
        {"ts": ts(hours=1), "event": "action", "role": "fix", "issue": 7,
         "desc": "operator unblock issue #7: -needs-human-review"},
    ]
    card = snapshots.issue_card(row_view(events), NOW)
    assert ask_of(card) is None


def test_a_journal_block_freezes_the_clock_and_a_cleared_one_does_not():
    start = {"ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
             "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"}
    blocked = {"ts": ts(hours=2), "event": "action", "role": "fix", "issue": 7,
               "desc": "relabel issue #7: -ai-working +ai-blocked"}
    card = snapshots.issue_card(row_view([start, RESOLVE, blocked]), NOW)
    assert ask_of(card) == "awaiting_reply"
    assert card["elapsed_s"] == 3_600

    cleared = {"ts": ts(hours=1), "event": "action", "role": "fix", "issue": 7,
               "desc": "relabel issue #7: -ai-blocked +ai-working"}
    card = snapshots.issue_card(
        row_view([start, RESOLVE, blocked, cleared]), NOW
    )
    assert ask_of(card) is None


# ── a live label view outranks the sticky journal fallback ────────────────────
_BLOCK_START = {"ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
                "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"}
_BLOCK_RELABEL = {"ts": ts(hours=2), "event": "action", "role": "fix", "issue": 7,
                  "desc": "relabel pr #12: -ai-pr-open +ai-blocked"}
_BLOCKED_EVENTS = [_BLOCK_START, RESOLVE, _BLOCK_RELABEL]


def _blocked_row():
    """A row whose journal blocked PR #12 two hours ago and never cleared it."""
    return replace(row_view(_BLOCKED_EVENTS), prs=(12,))


def test_a_live_label_view_without_the_hold_releases_the_journals_stale_block():
    """keifu #142/#181 (2026-08-03): the journal witnesses only the harness's
    own writes, so an operator clearing ai-blocked with a plain `gh` write left
    the fallback holding the row forever — both clocks frozen at the block
    stamp while paid legs kept spending."""
    card = snapshots.issue_card(
        _blocked_row(), NOW,
        gh_issue={"title": "t", "url": "u", "labels": [{"name": "ai-working"}]},
        gh_prs={12: {"title": "t", "url": "u", "labels": [{"name": "ai-pr-open"}]}},
    )

    assert ask_of(card) is None
    # The clock is no longer pinned to the 2h-ago block: it runs to now.
    assert card["elapsed_s"] == 10_800


def test_a_legacy_enrichment_without_labels_keeps_the_journal_fallback():
    """The pre-#181 enrichment shape carried title/url only. With no live label
    view to contradict it the journal remains the only block signal, and must
    still freeze the clock (keifu PRs #173/#177)."""
    card = snapshots.issue_card(
        _blocked_row(), NOW,
        gh_issue={"title": "t", "url": "u"},
        gh_prs={12: {"title": "t", "url": "u"}},
    )

    assert ask_of(card) == "awaiting_reply"
    assert card["board"]["since_ts"] == ts(seconds=7_200)
    assert card["elapsed_s"] == 3_600


def test_a_live_label_view_that_still_carries_the_hold_keeps_the_row_held():
    card = snapshots.issue_card(
        _blocked_row(), NOW,
        gh_issue={"title": "t", "url": "u", "labels": []},
        gh_prs={12: {"title": "t", "url": "u", "labels": [{"name": "ai-blocked"}]}},
    )

    assert ask_of(card) == "awaiting_reply"
    assert card["board"]["since_ts"] == ts(seconds=7_200)
    assert card["elapsed_s"] == 3_600


def test_a_partial_live_view_cannot_release_the_hold_the_journal_recorded():
    # Only the issue carries labels; the PR the block lives on is still the old
    # title/url shape, so nothing observed contradicts the journal.
    card = snapshots.issue_card(
        _blocked_row(), NOW,
        gh_issue={"title": "t", "url": "u", "labels": [{"name": "ai-working"}]},
        gh_prs={12: {"title": "t", "url": "u"}},
    )
    assert ask_of(card) == "awaiting_reply"
    assert card["elapsed_s"] == 3_600


# ── the card says outright whether a paid leg is executing ────────────────────
def test_a_card_reports_a_running_leg_so_the_client_keeps_the_work_clock_moving():
    row = row_for([_BLOCK_START], 7)
    assert row.running  # the start was never settled
    assert snapshots.issue_card(row, NOW)["leg_running"] is True
    assert snapshots.issue_detail(row, NOW)["leg_running"] is True


def test_a_done_card_never_reports_a_running_leg():
    row = replace(row_for([_BLOCK_START], 7), done=True)
    assert row.running  # a stale start survives the completion
    assert snapshots.issue_card(row, NOW)["leg_running"] is False
    assert snapshots.issue_detail(row, NOW)["leg_running"] is False


def test_a_settled_row_with_no_live_leg_reports_no_running_leg():
    assert snapshots.issue_card(row_for([RESOLVE], 7), NOW)["leg_running"] is False


def test_a_held_row_still_reports_the_leg_that_is_spending_on_it():
    # The bug's shape: human_action set AND a leg running. The card must carry
    # both, so the client can tick work rather than dead-stop on the hold.
    row = replace(row_for([_BLOCK_START], 7), events=tuple(_BLOCKED_EVENTS))
    card = snapshots.issue_card(row, NOW)
    assert ask_of(card) == "awaiting_reply"
    assert card["leg_running"] is True


def test_the_detail_view_freezes_the_same_clock_as_the_card():
    events = [
        {"ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        ESCALATION,
    ]
    detail = snapshots.issue_detail(row_for(events, 7), NOW, ESCALATED_ISSUE)
    assert detail["elapsed_s"] == 3_600


def test_a_wait_stamp_older_than_the_first_event_cannot_bound_the_clock():
    """Nothing can be measured to before the issue existed — the pre-existing
    'measure to now' behaviour must survive a nonsense stamp."""
    events = [
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=2), "event": "escalation", "issue": 7},
    ]
    stale = {**ESCALATED_ISSUE, "updatedAt": ts(hours=9)}
    card = snapshots.issue_card(row_for(events, 7), NOW, gh_issue=stale)
    assert card["elapsed_s"] == 7_200


def test_an_unescalated_issue_still_measures_its_clock_to_now():
    events = [
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
    ]
    card = snapshots.issue_card(row_for(events, 7), NOW)
    assert ask_of(card) is None and card["elapsed_s"] == 7_200


def test_a_budget_review_label_also_reads_as_needing_a_human():
    row = row_for([RESOLVE], 7)
    card = snapshots.issue_card(
        row, NOW,
        gh_issue={"labels": [{"name": "needs-budget-review"}], "updatedAt": ts(hours=1)},
    )
    assert ask_of(card) in ESCALATED_ASKS
    assert card["board"]["since_ts"] == ts(seconds=3_600)


# ── operator interventions render as steps ────────────────────────────────────
UNBLOCK = {
    "ts": ts(hours=1), "event": "action", "issue": 7, "role": "dashboard",
    "desc": "operator unblock issue #7: -needs-human-review -ai-blocked",
    "action": {
        "type": "operator_unblock", "target": "ISSUE", "number": 7,
        "add": [], "remove": ["needs-human-review", "ai-blocked"],
        "retries_cleared": True,
    },
}


def test_a_manual_unblock_is_rendered_as_a_free_step_that_says_what_it_did():
    steps = snapshots.detailed_steps(row_view([UNBLOCK]))
    assert [s["kind"] for s in steps] == ["RESOLVE", "unblock"]
    assert steps[-1]["state"] == "done" and steps[-1]["cost_usd"] == 0.0
    assert steps[-1]["note"] == (
        "removed needs-human-review, ai-blocked; reset retry counters"
    )


@pytest.mark.parametrize(
    "action,expected",
    [("set", "with scope note"), ("cleared", "scope note cleared")],
)
def test_an_unblock_that_rescoped_the_review_says_so_in_history(action, expected):
    # Three identical "unblock" rows read as an operator going in circles; the
    # marker is what shows the third one changed what the reviewer was asked.
    rescoped = {
        **UNBLOCK,
        "action": {**UNBLOCK["action"], "guidance": action},
    }
    step = snapshots.detailed_steps(row_view([rescoped]))[-1]
    assert step["note"].endswith(expected)


def test_an_unblock_that_left_the_note_alone_adds_no_marker():
    step = snapshots.detailed_steps(row_view([UNBLOCK]))[-1]
    assert "scope note" not in step["note"]


def test_a_failed_unblock_intent_does_not_retire_journal_attention():
    escalation = {
        "ts": ts(hours=2), "event": "action", "issue": 7, "role": "fix",
        "desc": "relabel issue #7: +needs-human-review",
    }
    failed = {
        **UNBLOCK,
        "action": {
            **UNBLOCK["action"],
            "labels_applied": False,
            # Retain the attempted move to reproduce old/foreign writers; the
            # fold must key on the explicit outcome, not merely empty lists.
            "remove": ["needs-human-review", "ai-blocked"],
        },
    }
    row = row_for([escalation, failed], 7)

    assert snapshots._journal_attention_at(row)[0] == "escalated"
    step = snapshots.detailed_steps(row_view([failed]))[-1]
    assert step["state"] == "failed"
    assert step["note"] == "label edit failed; reset retry counters"
    assert "removed" not in step["note"]
    assert snapshots.collapse_steps(row_view([failed]))[-1]["state"] == "failed"


def test_the_card_strip_shows_the_intervention_too():
    steps = snapshots.collapse_steps(row_view([UNBLOCK]))
    assert [s["kind"] for s in steps] == ["RESOLVE", "unblock"]


@pytest.mark.parametrize("labels", [None, 7, "needs-human-review"])
def test_a_malformed_label_list_on_an_intervention_is_survivable(labels):
    broken = {**UNBLOCK, "action": {"type": "operator_unblock", "remove": labels}}
    step = snapshots.detailed_steps(row_view([broken]))[-1]
    assert step["kind"] == "unblock" and step["note"] is None


def test_an_operator_relabel_is_its_own_step_kind():
    relabel = {**UNBLOCK, "action": {
        "type": "operator_relabel", "target": "ISSUE", "number": 7,
        "add": ["P1"], "remove": [],
    }}
    steps = snapshots.detailed_steps(row_view([relabel]))
    assert steps[-1]["kind"] == "relabel (operator)"
    assert steps[-1]["note"] == "added P1"


def test_an_api_channel_unblock_wears_its_channel_in_the_step_kind():
    # An unblock journaled by a bare API caller (curl, an agent) must never be
    # mistaken for a deliberate human clicking the dashboard button.
    api_unblock = {**UNBLOCK, "action": {**UNBLOCK["action"], "channel": "api"}}
    steps = snapshots.detailed_steps(row_view([api_unblock]))
    assert steps[-1]["kind"] == "unblock (api)"


@pytest.mark.parametrize("channel", ["ui", None, 7, ""])
def test_a_ui_or_unstamped_channel_keeps_the_plain_operator_kind(channel):
    action = {**UNBLOCK["action"]}
    if channel is not None:
        action["channel"] = channel
    steps = snapshots.detailed_steps(row_view([{**UNBLOCK, "action": action}]))
    assert steps[-1]["kind"] == "unblock"


@pytest.mark.parametrize(
    "action",
    ["not-a-dict", {}, {"type": "comment"}, {"type": None}, None],
)
def test_events_that_are_not_operator_actions_add_no_steps(action):
    noise = {"ts": ts(hours=1), "event": "action", "issue": 7,
             "desc": "comment pr #12", "action": action}
    assert [s["kind"] for s in snapshots.detailed_steps(row_view([noise]))] == [
        "RESOLVE"
    ]


def test_step_indices_address_the_step_list_after_interventions_are_appended():
    steps = snapshots.detailed_steps(row_view([UNBLOCK], legs=[RESOLVE, FIX_CI_1]))
    assert [s["idx"] for s in steps] == [0, 1, 2]


def test_the_history_interleaves_interventions_with_the_legs_they_preceded():
    # An unblock BETWEEN two legs must render between them, not at the end of
    # the drill-down (keifu batch 3: every operator action showed after every
    # leg, however early it happened).
    mid_unblock = {
        **UNBLOCK,
        "ts": ts(hours=2, minutes=30),  # after RESOLVE (3h ago), before FIX_CI_1 (2h ago)
    }
    steps = snapshots.detailed_steps(
        row_view([mid_unblock], legs=[RESOLVE, FIX_CI_1, FIX_CI_2])
    )
    assert [s["kind"] for s in steps] == ["RESOLVE", "unblock", "FIX_CI", "FIX_CI"]
    assert [s["idx"] for s in steps] == [0, 1, 2, 3]


def test_a_step_with_an_unparseable_stamp_sorts_last_instead_of_crashing():
    stampless = {**UNBLOCK, "ts": "not-a-time"}
    steps = snapshots.detailed_steps(row_view([stampless], legs=[RESOLVE, FIX_CI_1]))
    assert [s["kind"] for s in steps] == ["RESOLVE", "FIX_CI", "unblock"]


# ── per-step notes ────────────────────────────────────────────────────────────
def test_a_review_step_notes_its_verdict_and_round():
    reviews = [
        {"ts": ts(hours=3), "event": "agent_run", "role": "review", "kind": "REVIEW",
         "issue": 7, "number": 12, "status": "REQUEST_CHANGES", "exit_reason": "ok"},
        {"ts": ts(hours=2), "event": "agent_run", "role": "review", "kind": "RE_REVIEW",
         "issue": 7, "number": 12, "status": "APPROVE", "exit_reason": "ok"},
    ]
    notes = [s["note"] for s in snapshots.detailed_steps(row_for(reviews, 7))]
    assert notes == ["REQUEST_CHANGES round 1", "APPROVE round 2"]


def test_failed_review_attempts_do_not_inflate_the_first_verdict_round():
    failures = [
        {
            "ts": ts(hours=8 - attempt), "event": "agent_run", "role": "review",
            "kind": "REVIEW", "issue": 7, "number": 12, "status": "APPROVE",
            "exit_reason": "agent_error", "is_error": True,
        }
        for attempt in range(5)
    ]
    verdict = {
        "ts": ts(hours=2), "event": "agent_run", "role": "review", "kind": "REVIEW",
        "issue": 7, "number": 12, "status": "REQUEST_CHANGES", "exit_reason": "ok",
    }

    row = row_for([*failures, verdict], 7)

    assert row.rounds == 1
    assert snapshots.detailed_steps(row)[-1]["note"] == "REQUEST_CHANGES round 1"


def test_a_blocked_step_notes_the_question_it_asked():
    blocked = {
        "ts": ts(hours=2), "event": "agent_run", "role": "fix", "kind": "RESOLVE",
        "issue": 7, "number": 7, "status": "blocked", "exit_reason": "ok",
        "needs": "**Agent needs input:** which database should this migrate?\nmore",
    }
    step = snapshots.detailed_steps(row_for([blocked], 7))[0]
    assert step["note"] == "Agent needs input: which database should this migrate?"


def test_a_failed_step_notes_why_it_died():
    died = {
        "ts": ts(hours=2), "event": "agent_run", "role": "fix", "kind": "FIX_CI",
        "issue": 7, "number": 12, "exit_reason": "budget_exhausted",
        "summary": "admission rejected before the leg started",
    }
    step = snapshots.detailed_steps(row_for([died], 7))[0]
    assert step["note"] == (
        "budget_exhausted — admission rejected before the leg started"
    )


def test_a_clean_step_has_nothing_to_add():
    assert snapshots.detailed_steps(row_for([RESOLVE], 7))[0]["note"] is None


def test_a_note_is_bounded_for_a_compact_row():
    blocked = {
        "ts": ts(hours=2), "event": "agent_run", "role": "fix", "kind": "RESOLVE",
        "issue": 7, "number": 7, "status": "blocked", "exit_reason": "ok",
        "needs": "x" * 900,
    }
    assert len(snapshots.detailed_steps(row_for([blocked], 7))[0]["note"]) == 240


def test_a_step_exposes_runtime_budget_and_its_evidence_escalation():
    routing = {
        "ts": ts(hours=3, minutes=1),
        "type": "routing.escalated.v1",
        "leg": "address-review",
        "from_effort": "medium",
        "to_effort": "high",
        "evidence": {"notches": 1, "review_rounds": 2},
    }
    started = {
        "ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
        "kind": "ADDRESS_REVIEW", "number": 12, "issue": 7,
        "run_id": "run-2", "model": "gpt-5.6-terra", "effort": "high",
    }
    settled = {
        "ts": ts(hours=2), "event": "agent_run", "role": "fix",
        "kind": "ADDRESS_REVIEW", "number": 12, "issue": 7,
        "run_id": "run-2", "model": "gpt-5.6-terra", "effort": "high",
        "runtime": "codex", "pricing_basis": "configured_rate_card",
        "cost_usd": 0.8, "status": "done", "exit_reason": "ok",
    }
    base = row_for([started, settled], 7)
    row = replace(base, prs=(12,), events=(routing, started, settled))

    step = snapshots.detailed_steps(
        row, budget_caps={"ADDRESS_REVIEW": 10.0}
    )[0]

    assert step["runtime"] == "codex"
    assert step["pricing_basis"] == "configured_rate_card"
    assert step["budget_cap_usd"] == 10.0
    assert step["escalation"] == {
        "notches": 1,
        "from_effort": "medium",
        "to_effort": "high",
        "after_review_round": 2,
    }


def test_review_context_labels_leg_verdicts_and_uses_authoritative_round_count():
    first = {
        "ts": ts(hours=4), "event": "agent_run", "role": "review",
        "kind": "REVIEW", "number": 12, "issue": 7,
        "status": "REQUEST_CHANGES", "exit_reason": "ok",
    }
    failed = {
        "ts": ts(hours=3), "event": "agent_run", "role": "review",
        "kind": "RE_REVIEW", "number": 12, "issue": 7,
        "status": "", "exit_reason": "agent_error", "is_error": True,
    }
    second = {
        "ts": ts(hours=2), "event": "agent_run", "role": "review",
        "kind": "RE_REVIEW", "number": 12, "issue": 7,
        "status": "REQUEST_CHANGES", "exit_reason": "ok",
    }
    first_body = "first requested change"
    second_body = "second requested change " + "x" * 700
    first_review = {
        "state": "CHANGES_REQUESTED", "body": first_body,
        "commit_id": "a", "submitted_at": ts(hours=3, minutes=59),
    }
    second_review = {
        "state": "CHANGES_REQUESTED", "body": second_body,
        "commit_id": "b", "submitted_at": ts(hours=1, minutes=59),
    }
    observed_first = {
        "ts": ts(hours=3, minutes=58), "type": "world.observed.v1",
        "subject_kind": "pr", "subject_number": 12,
        "facts": {"reviews": [first_review]},
    }
    observed_second = {
        "ts": ts(hours=1, minutes=58), "type": "world.observed.v1",
        "subject_kind": "pr", "subject_number": 12,
        "facts": {"reviews": [first_review, second_review]},
    }
    base = row_for([first, failed, second], 7)
    row = replace(
        base,
        prs=(12,),
        events=(first, observed_first, failed, second, observed_second),
    )

    steps = snapshots.detailed_steps(
        row,
        unproductive_round_cap=5,
        unproductive_round_counts={12: 4},
    )

    assert steps[0]["review"]["verdict_excerpt"] == first_body
    assert steps[0]["review"]["rejection"] == 1
    assert steps[1]["review"]["unproductive_rounds"] == 4
    assert steps[2]["review"]["rejection"] == 2
    assert steps[2]["review"]["unproductive_rounds"] == 4
    assert "rejection_cap" not in steps[2]["review"]
    assert steps[2]["review"]["unproductive_cap"] == 5
    assert len(steps[2]["review"]["verdict_excerpt"]) == 500
    assert steps[2]["review"]["verdict_excerpt"].endswith("…")


def test_change_context_splits_commit_progression_across_fix_legs():
    resolve = {
        **RESOLVE, "ts": ts(hours=4), "status": "pr_opened",
    }
    address = {
        "ts": ts(hours=2), "event": "agent_run", "role": "fix",
        "kind": "ADDRESS_REVIEW", "number": 12, "issue": 7,
        "status": "done", "exit_reason": "ok",
    }
    opened = {
        "ts": ts(hours=3), "type": "world.observed.v1",
        "subject_kind": "pr", "subject_number": 12,
        "facts": {
            "head_ref": "feat/7-fix", "head_sha": "a" * 40,
            "changed_files_total": 3,
        },
    }
    repaired = {
        "ts": ts(hours=1), "type": "world.observed.v1",
        "subject_kind": "pr", "subject_number": 12,
        "facts": {
            "head_ref": "feat/7-fix", "head_sha": "b" * 40,
            "changed_files_total": 5,
        },
    }
    base = row_for([resolve, address], 7)
    row = replace(base, prs=(12,), events=(resolve, opened, address, repaired))
    gh_prs = {12: {"commits": [
        {"oid": "a" * 40, "messageHeadline": "red: reproduce reload race"},
        {"oid": "b" * 40, "messageHeadline": "green: serialize watcher writes"},
    ]}}

    steps = snapshots.detailed_steps(row, gh_prs=gh_prs)

    assert steps[0]["change"] == {
        "pr": 12,
        "branch": "feat/7-fix",
        "head_sha": "a" * 40,
        "commit_subjects": ["red: reproduce reload race"],
        "files_changed_count": 3,
    }
    assert steps[1]["change"]["commit_subjects"] == [
        "green: serialize watcher writes"
    ]
    assert steps[1]["change"]["files_changed_count"] == 5


def test_change_context_does_not_attribute_history_across_a_force_push():
    first = {**RESOLVE, "ts": ts(hours=4), "status": "pr_opened"}
    second = {
        "ts": ts(hours=2), "event": "agent_run", "role": "fix",
        "kind": "ADDRESS_REVIEW", "number": 12, "issue": 7,
        "status": "done", "exit_reason": "ok",
    }
    before = {
        "ts": ts(hours=3), "type": "world.observed.v1",
        "subject_kind": "pr", "subject_number": 12,
        "facts": {"head_ref": "feat/7", "head_sha": "a" * 40},
    }
    after = {
        "ts": ts(hours=1), "type": "world.observed.v1",
        "subject_kind": "pr", "subject_number": 12,
        "facts": {
            "head_ref": "feat/7", "head_sha": "c" * 40,
            "changed_files_total": 9,
        },
    }
    row = replace(
        row_for([first, second], 7),
        prs=(12,),
        events=(first, before, second, after),
    )
    gh_prs = {12: {"commits": [
        {"oid": "b" * 40, "messageHeadline": "unrelated rewritten history"},
        {"oid": "c" * 40, "messageHeadline": "actual repair"},
    ], "files": [{"path": "a.py"}] * 9}}

    change = snapshots.detailed_steps(row, gh_prs=gh_prs)[1]["change"]

    assert change["commit_subjects"] == []
    assert change["files_changed_count"] is None


# ── PR merge readiness ────────────────────────────────────────────────────────
def review_card(**pr_fields):
    events = [RESOLVE, {"ts": ts(hours=1), "event": "agent_run", "role": "review",
                        "kind": "REVIEW", "issue": 7, "number": 12,
                        "cost_usd": 0.5, "status": "done", "exit_reason": "ok",
                        "model": "m", "session_id": "s"}]
    return snapshots.issue_card(
        row_for(events, 7), NOW,
        gh_prs={12: {"url": "u/12", "state": "OPEN", **pr_fields}},
    )


def test_an_approved_mergeable_pr_reads_as_mergeable():
    card = review_card(reviewDecision="APPROVED", mergeable="MERGEABLE")
    assert card["pr_review_state"] == "mergeable"


def test_requested_changes_outrank_a_clean_merge_state():
    card = review_card(reviewDecision="CHANGES_REQUESTED", mergeable="MERGEABLE")
    assert card["pr_review_state"] == "changes_requested"


@pytest.mark.parametrize(
    "pr_fields",
    [
        {},
        {"reviewDecision": None, "mergeable": None},
        {"reviewDecision": "APPROVED"},
        {"reviewDecision": "APPROVED", "mergeable": "CONFLICTING"},
        {"reviewDecision": "APPROVED", "mergeable": "UNKNOWN"},
        {"reviewDecision": "REVIEW_REQUIRED", "mergeable": "MERGEABLE"},
        {"reviewDecision": 7, "mergeable": ["nonsense"]},
    ],
)
def test_unknown_or_unreadable_review_data_claims_nothing(pr_fields):
    assert review_card(**pr_fields)["pr_review_state"] is None


def test_a_card_with_no_pull_request_has_no_review_state():
    assert snapshots.issue_card(row_for([RESOLVE], 7), NOW)["pr_review_state"] is None


# ── PR CI state ───────────────────────────────────────────────────────────────
def test_all_green_checks_read_as_passing():
    card = review_card(statusCheckRollup=[
        {"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}, {"state": "NEUTRAL"},
    ])
    assert card["pr_ci_state"] == "passing"


@pytest.mark.parametrize(
    "verdict",
    # STALE is terminal (the check will not re-run on this head): without it a
    # stale check read "pending" forever.
    ["FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE"],
)
def test_pending_check_outranks_failure_until_every_check_settles(verdict):
    card = review_card(statusCheckRollup=[
        {"conclusion": "SUCCESS"}, {"conclusion": verdict}, {"state": "PENDING"},
    ])
    assert card["pr_ci_state"] == "pending"
    assert review_card(statusCheckRollup=[{"conclusion": verdict}])["pr_ci_state"] == "failing"


def test_a_still_running_check_reads_as_pending_not_passing():
    card = review_card(statusCheckRollup=[
        {"conclusion": "SUCCESS"}, {"state": "IN_PROGRESS", "conclusion": None},
    ])
    assert card["pr_ci_state"] == "pending"


@pytest.mark.parametrize(
    "rollup", [None, [], "nonsense", [7, "not-a-check"], [{}]],
)
def test_missing_or_unreadable_checks_claim_no_ci_state(rollup):
    # [{}] normalises to one empty verdict, which must not read as green.
    card = review_card(statusCheckRollup=rollup)
    assert card["pr_ci_state"] in (None, "pending")
    if rollup in (None, [], "nonsense"):
        assert card["pr_ci_state"] is None


def test_issue_detail_uses_the_current_pr_check_rollup_for_ci_state():
    detail = snapshots.issue_detail(
        row_for([RESOLVE, FIX_CI_2], 7),
        NOW,
        gh_prs={12: {
            "state": "OPEN",
            "statusCheckRollup": [
                {"conclusion": "SUCCESS"},
                {"state": "IN_PROGRESS", "conclusion": None},
            ],
        }},
    )

    assert detail["pr_ci_state"] == "pending"


# ── hourly spend ──────────────────────────────────────────────────────────────
def charge(hours_ago: float, usd):
    return NOW - timedelta(hours=hours_ago), usd


def bucket_at(series, hours_ago: int) -> dict:
    hour = (NOW - timedelta(hours=hours_ago)).replace(
        minute=0, second=0, microsecond=0
    )
    return next(b for b in series if b["hour_start_iso"] == hour.isoformat())


def test_an_empty_journal_still_yields_a_dense_zeroed_window():
    series = snapshots.spend_buckets([], NOW)
    assert len(series) == snapshots.SPEND_WINDOW_HOURS
    assert {b["usd"] for b in series} == {0.0}
    assert [b["hour_start_iso"] for b in series] == sorted(
        b["hour_start_iso"] for b in series
    )


def test_a_single_charge_lands_in_the_hour_it_happened():
    series = snapshots.spend_buckets([charge(3.5, 1.25)], NOW)
    assert bucket_at(series, 4)["usd"] == 1.25
    assert sum(b["usd"] for b in series) == 1.25


def test_charges_either_side_of_an_hour_edge_land_in_different_buckets():
    just_before = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    on_the_hour = NOW.replace(minute=0, second=0, microsecond=0)
    series = snapshots.spend_buckets(
        [(just_before, 1.0), (on_the_hour, 2.0)], NOW
    )
    assert bucket_at(series, 1)["usd"] == 1.0
    assert bucket_at(series, 0)["usd"] == 2.0


def test_charges_outside_the_window_are_dropped_not_clamped():
    series = snapshots.spend_buckets(
        [charge(200, 9.0), charge(-5, 9.0), charge(1, 3.0)], NOW
    )
    assert sum(b["usd"] for b in series) == 3.0


@pytest.mark.parametrize(
    "bad", [None, "", "1.5", float("nan"), float("inf"), {"usd": 1}, [2.0]]
)
def test_unpriceable_charges_are_skipped_rather_than_poisoning_the_series(bad):
    series = snapshots.spend_buckets([(NOW, bad), charge(0, 2.0)], NOW)
    assert sum(b["usd"] for b in series) == 2.0


def test_an_undated_charge_is_skipped():
    assert sum(b["usd"] for b in snapshots.spend_buckets([(None, 5.0)], NOW)) == 0.0


def test_a_non_positive_window_is_an_empty_series():
    assert snapshots.spend_buckets([charge(1, 1.0)], NOW, hours=0) == []


def test_spend_per_hour_counts_estimated_legs_and_deployment_level_runs():
    estimated = {**RESOLVE, "ts": ts(hours=1), "cost_usd": 0.5, "estimated": True}
    dep = build([
        estimated,
        {"ts": ts(hours=1), "event": "agent_run", "role": "steward",
         "kind": "TRIAGE", "cost_usd": 0.25, "status": "done",
         "exit_reason": "ok", "model": "m", "session_id": "s"},
    ])

    series = snapshots.spend_per_hour(dep, NOW)

    assert bucket_at(series, 1)["usd"] == 0.75
    assert sum(b["usd"] for b in series) == 0.75


def test_spend_per_hour_counts_a_deployment_level_leg_priced_only_by_estimate():
    """A subscription-priced triage leg has no true cost, only an estimate.
    Reading the raw event dropped it from the chart while the kind totals
    charged it — two answers for one leg."""
    dep = build([
        {"ts": ts(hours=1), "event": "agent_run", "role": "steward",
         "kind": "TRIAGE", "cost_estimated_usd": 0.4, "status": "done",
         "exit_reason": "ok", "model": "m", "session_id": "s", "run_id": "r1"},
    ])

    series = snapshots.spend_per_hour(dep, NOW)

    assert bucket_at(series, 1)["usd"] == 0.4
    assert sum(b["usd"] for b in series) == sum(
        cost for _kind, cost, _runs in dep.metrics.by_kind
    )


def test_spend_per_hour_charges_legs_on_an_issue_the_world_no_longer_shows():
    """A retired issue is filtered out of the display rows but its legs were
    still paid for, and the metrics still charge them. The chart reads the same
    rows or it under-reports the deployment's spend."""
    dep = v2_deployment([
        v2_event(AgentRunStarted("m", kind="RESOLVE"), "a", 0, issue=7, run_id="r"),
        v2_event(
            AgentRunCompleted(
                "done", False, "ok", 1000, "m", "codex",
                cost_micros=750_000, currency="USD", pricing_basis="api",
                kind="RESOLVE",
            ),
            "b", 1, issue=7, run_id="r",
        ),
        v2_event(
            WorldObserved("issue", 7, "hash", {"present": False, "state": "OPEN"}),
            "c", 2, issue=7,
        ),
    ])

    assert [row.issue for row in dep.issues] == []
    assert dep.metrics.total_cost_usd == 0.75
    assert sum(b["usd"] for b in snapshots.spend_per_hour(dep, NOW)) == 0.75


def test_spend_per_hour_totals_a_huge_journal_without_losing_charges():
    dep = build([
        {**RESOLVE, "ts": ts(hours=n % 47, minutes=n % 60), "cost_usd": 0.01}
        for n in range(2000)
    ])
    assert round(sum(b["usd"] for b in snapshots.spend_per_hour(dep, NOW)), 2) == 20.0


# ── v2 GitHub enrichment ──────────────────────────────────────────────────────
REVIEW_LEG = {"ts": ts(hours=1), "event": "agent_run", "role": "review",
              "kind": "REVIEW", "issue": 7, "number": 12, "cost_usd": 0.5,
              "status": "done", "exit_reason": "ok", "model": "m",
              "session_id": "s"}


def v2_backend(monkeypatch, ledger_rows=(), **pr_fields):
    deployment = replace(build([RESOLVE, REVIEW_LEG]), schema_version=2)
    snapshot = dash.Snapshot("demo", "o/r", "/target", deployment, (NOW,))

    class Gh:
        def issues(self, _repo):
            return ([{"number": 7, "state": "OPEN", "title": "the bug",
                      "url": "u/7"}], "")

        def prs(self, _repo):
            return ([{"number": 12, "url": "u/12", "state": "OPEN",
                      **pr_fields}], "")

        def pr(self, _repo, number):
            return ({
                "number": number,
                "headRefName": "feat/7-dashboard",
                "closingIssuesReferences": [{"number": 7}],
                "commits": [],
                "files": [],
                **pr_fields,
            }, "")

    backend = service.Backend(gh=Gh(), now_fn=lambda: NOW)
    monkeypatch.setattr(backend, "snapshot", lambda _name: snapshot)
    monkeypatch.setattr(
        backend, "verification", lambda: {"rows": list(ledger_rows)}
    )
    return backend


def card_for(payload, issue=7):
    everything = payload["in_flight"] + payload["done"]
    return next(card for card in everything if card["issue"] == issue)


def test_v2_cards_carry_pr_state_and_merge_readiness_from_github(monkeypatch):
    backend = v2_backend(monkeypatch, reviewDecision="APPROVED", mergeable="MERGEABLE")

    first = card_for(backend.issues("demo"))
    assert (first["pr"], first["pr_state"]) == (12, None)

    card = eventually(
        lambda: card_for(backend.issues("demo")),
        lambda current: current["pr_state"] is not None,
    )

    assert (card["pr"], card["pr_state"]) == (12, "open")
    assert card["pr_review_state"] == "mergeable"


def test_v2_cards_report_no_merge_readiness_when_github_withholds_it(monkeypatch):
    backend = v2_backend(monkeypatch)
    card = eventually(
        lambda: card_for(backend.issues("demo")),
        lambda current: current["pr_state"] is not None,
    )

    assert card["pr_state"] == "open" and card["pr_review_state"] is None


def test_issue_detail_uses_cached_deployment_leg_and_unproductive_caps(monkeypatch):
    backend = v2_backend(monkeypatch)
    configured = {
        "MAX_BUDGET_USD": "6.5",
        "REVIEW_BUDGET_USD": "2.5",
        "MAX_UNPRODUCTIVE_REVIEW_ROUNDS": "2",
    }
    reads = []
    monkeypatch.setattr(
        service.cli,
        "deployment_env_field",
        lambda _name, field: reads.append(field) or configured.get(field, ""),
    )

    detail = backend.issue("demo", 7)
    backend.issue("demo", 7)

    assert detail is not None
    resolve, review = detail["steps"]
    assert resolve["budget_cap_usd"] == 6.5
    assert review["budget_cap_usd"] == 2.5
    assert review["review"]["unproductive_cap"] == 2
    assert len(reads) == len(service._DETAIL_CONFIG_FIELDS)


def test_issue_detail_reads_current_execution_allowance_and_ignores_retired_file_counters(
    monkeypatch, tmp_path, current_review_rig,
):
    from tests.adapters.test_review_execution_control import dump

    store, _, _, _, _ = current_review_rig
    backend = v2_backend(monkeypatch)
    backend.control_db_path = store.path
    original = backend.snapshot("demo")
    deployment = replace(build([RESOLVE, {**REVIEW_LEG, "number": 9}]), schema_version=2)
    backend.snapshot = lambda _name: replace(original, deployment=deployment)
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    files = Store(str(review_dir))
    files.set_infra_deaths(7, 2)
    files.set_infra_deaths(-9, 1)
    before = dump(store)

    first = backend.issue("demo", 7)
    assert first is not None
    assert first["steps"][1]["review"]["unproductive_rounds"] == 3
    files.set_infra_deaths(7, 99)
    files.set_infra_deaths(-9, 99)
    second = backend.issue("demo", 7)
    assert second is not None
    assert second["steps"][1]["review"]["unproductive_rounds"] == 3
    assert dump(store) == before


def test_cards_carry_the_verification_reason_for_issue_and_pr(monkeypatch):
    ledger = [{"change": "#7 the bug", "shipped": "PR #12", "status": "VERIFIED",
               "reason": "probe stayed green over two deployments"}]

    card = card_for(v2_backend(monkeypatch, ledger_rows=ledger).issues("demo"))

    assert card["issue_verification"] == "VERIFIED"
    assert card["issue_verification_reason"] == "probe stayed green over two deployments"
    assert card["pr_verification_reason"] == "probe stayed green over two deployments"


def test_cards_omit_a_verification_reason_the_ledger_does_not_give(monkeypatch):
    ledger = [{"change": "#7 the bug", "shipped": "PR #12", "status": "VERIFIED",
               "reason": None}]

    card = card_for(v2_backend(monkeypatch, ledger_rows=ledger).issues("demo"))

    assert card["issue_verification"] == "VERIFIED"
    assert card["issue_verification_reason"] is None


def test_a_github_read_cannot_displace_the_projected_issue_state():
    """The v2 projection is authority for an issue's lifecycle. A GitHub read
    that disagrees — a stale list, a closed-then-reopened issue — may only add
    presentation, never overwrite the projected answer."""
    row = replace(
        row_for([RESOLVE], 7),
        facts={"state": "OPEN", "labels": ("ai-ready",)},
    )
    enrichment = service._github_issue_enrichment({
        "number": 7, "title": "the bug", "url": "u/7",
        "state": "CLOSED", "mergedAt": "2026-07-01T00:00:00+00:00",
        "reviewDecision": "APPROVED", "mergeable": "MERGEABLE",
    })

    issue = service._projected_issue(row, enrichment)

    assert issue["state"] == "OPEN"
    assert (issue["title"], issue["url"]) == ("the bug", "u/7")
    assert "mergedAt" not in issue


def test_issue_enrichment_carries_the_body_the_projection_cannot_supply():
    # The v2 facts have no `body`/`createdAt`; without GitHub's, every
    # expanded backlog row rendered empty (2026-08-02).
    row = replace(row_for([RESOLVE], 7), facts={"state": "OPEN", "labels": ()})
    enrichment = service._github_issue_enrichment({
        "number": 7, "title": "the bug", "url": "u/7",
        "body": "## What happened\n\nit broke",
        "createdAt": "2026-08-01T00:00:00+00:00",
        "state": "CLOSED",
    })

    issue = service._projected_issue(row, enrichment)
    card = snapshots.backlog_card(issue, NOW)

    assert card["body"] == "## What happened\n\nit broke"
    assert card["age_days"] is not None
    assert issue["state"] == "OPEN"


def test_pull_request_enrichment_still_carries_its_merge_and_review_state():
    assert service._github_enrichment({
        "state": "OPEN", "mergedAt": None, "reviewDecision": "APPROVED",
        "mergeable": "MERGEABLE", "title": "t", "url": "u",
    }) == {
        "state": "OPEN", "reviewDecision": "APPROVED",
        "mergeable": "MERGEABLE", "title": "t", "url": "u",
    }


# ── card labels ───────────────────────────────────────────────────────────────
def test_a_card_carries_the_projected_labels_when_github_adds_none():
    row = replace(row_for([RESOLVE], 7), facts={"labels": ("ai-done", "bug")})

    card = snapshots.issue_card(row, NOW, gh_issue={"title": "t", "url": "u"})

    assert card["labels"] == ["ai-done", "bug"]


def test_a_card_falls_back_to_the_github_labels_without_a_projection():
    card = snapshots.issue_card(
        row_for([RESOLVE], 7), NOW,
        gh_issue={"labels": [{"name": "bug"}, "P1"]},
    )

    assert card["labels"] == ["bug", "P1"]


def test_a_card_with_no_labels_anywhere_reports_an_empty_list():
    assert snapshots.issue_card(row_for([RESOLVE], 7), NOW)["labels"] == []


@pytest.mark.parametrize("malformed", [None, "bug", 5, {"name": "bug"}])
def test_malformed_projected_labels_fall_back_to_the_github_read(malformed):
    row = replace(row_for([RESOLVE], 7), facts={"labels": malformed})

    card = snapshots.issue_card(row, NOW, gh_issue={"labels": ["P2"]})

    assert card["labels"] == ["P2"]


def test_a_card_reports_every_label_of_a_heavily_tagged_issue():
    row = replace(
        row_for([RESOLVE], 7),
        facts={"labels": tuple(f"label-{n}" for n in range(500))},
    )

    assert len(snapshots.issue_card(row, NOW)["labels"]) == 500


# ── issues board: status union, rail, overspend, tier ─────────────────────────
def board_of(card) -> dict:
    return card["board"]


ESCALATED_ASKS = {"escalated", "rearm", "budget"}


def ask_of(card) -> str | None:
    board = card["board"]
    return board["ask"] if board and board["status"] == "needs_you" else None


_REVIEW_APPROVE = {"ts": ts(hours=1), "event": "agent_run", "role": "review",
                   "kind": "REVIEW", "issue": 7, "number": 12, "status": "APPROVE",
                   "exit_reason": "ok"}
_MERGE_READY_PR = {
    "state": "OPEN", "reviewDecision": "APPROVED", "autoMergeRequest": None,
    "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
    "statusCheckRollup": [{"conclusion": "SUCCESS", "completedAt": ts(minutes=20)}],
    "latestReviews": [{"state": "APPROVED", "submittedAt": ts(minutes=30)}],
}


def test_a_breaker_escalation_asks_for_a_re_arm_and_waits_from_the_escalation():
    card = snapshots.issue_card(row_view([BREAKER_COMMENT, ESCALATION]), NOW)

    assert ask_of(card) == "rearm"
    assert board_of(card)["headline"] == "Re-arm or close"
    assert board_of(card)["reason"].startswith("5 review rounds on this PR ended")
    assert board_of(card)["since_ts"] == ts(hours=2)


def test_an_escalation_without_a_breaker_comment_asks_the_operator_to_answer():
    card = snapshots.issue_card(row_view([ESCALATION]), NOW)

    assert ask_of(card) == "escalated"


def test_a_budget_review_label_asks_for_spend_approval():
    card = snapshots.issue_card(
        row_view([BREAKER_COMMENT, ESCALATION]), NOW,
        gh_issue={"labels": ["needs-budget-review"], "updatedAt": ts(hours=2)},
    )

    assert ask_of(card) == "budget"


def test_a_budget_review_comment_asks_for_spend_approval_without_a_label_view():
    budget = {**BREAKER_COMMENT, "action": {
        **BREAKER_COMMENT["action"],
        "body": "**Budget review needed:** the resolve leg reached its configured cap.",
    }}
    card = snapshots.issue_card(row_view([budget, ESCALATION]), NOW)

    assert ask_of(card) == "budget"
    assert board_of(card)["reason"] == "The resolve leg reached its configured cap."


def test_an_unanswered_agent_question_awaits_a_reply():
    card = snapshots.issue_card(_blocked_row(), NOW)

    assert ask_of(card) == "awaiting_reply"


def test_escalated_while_a_leg_is_running_stays_needs_you():
    start = {"ts": ts(minutes=10), "event": "agent_run_start", "role": "fix",
             "kind": "FIX_CI", "issue": 7, "number": 12, "dispatch": "d9"}
    row = row_for([RESOLVE, BREAKER_COMMENT, ESCALATION, start], 7)

    card = snapshots.issue_card(row, NOW, gh_issue=ESCALATED_ISSUE)

    assert card["leg_running"] is True
    assert board_of(card)["status"] == "needs_you"


def test_a_re_armed_row_with_a_lifetime_escalation_does_not_alarm():
    row = row_view([BREAKER_COMMENT, ESCALATION, UNBLOCK])

    card = snapshots.issue_card(row, NOW)

    assert row.escalations or any(e.get("event") == "escalation" for e in row.events)
    assert board_of(card)["status"] != "needs_you"


def _harness_merges_governed(branch: str) -> bool:
    from issue_agent.domain.fix import harness_merges_branch
    from issue_agent.domain.review import IdentityMode

    return harness_merges_branch(branch, auto_merge=True, identity_mode=IdentityMode.THREE)


def _approved_row(*later):
    return replace(row_for([RESOLVE, _REVIEW_APPROVE, *later], 7), prs=(12,))


@pytest.mark.parametrize(
    "branch,status",
    [
        ("fix/7-test", "working"),
        # A stranger's branch on an auto-merging repo: the fixer never merges
        # it, so the board must not promise that it will.
        ("dependabot/npm_and_yarn/library-1", "needs_you"),
        (None, "needs_you"),
    ],
)
def test_merge_ownership_follows_the_fixers_per_branch_rule(branch, status):
    pr = {**_MERGE_READY_PR, **({"headRefName": branch} if branch else {})}

    card = snapshots.issue_card(
        _approved_row(), NOW, gh_prs={12: pr}, harness_merges=_harness_merges_governed,
    )

    assert board_of(card)["status"] == status
    if status == "needs_you":
        assert ask_of(card) == "merge"
        # The human-wait clock's start: the newest approval or settled check.
        assert board_of(card)["since_ts"] == ts(minutes=20)
    else:
        assert board_of(card)["headline"] == "Ready to merge"
        assert "The harness merges it next." in board_of(card)["reason"]


def test_a_v2_enriched_pr_keeps_the_merge_readiness_the_board_needs():
    # The v2 enrichment once dropped mergeStateStatus, so an approved, green
    # PR on a repo the harness does not merge read "Ready to merge" (Working)
    # instead of asking the operator to merge it.
    full = {**_MERGE_READY_PR, "headRefName": "fix/7-test"}
    enriched = service._github_enrichment(full)

    owned_by_operator = snapshots.issue_card(_approved_row(), NOW, gh_prs={12: enriched})
    behind = snapshots.issue_card(
        _approved_row(), NOW,
        gh_prs={12: service._github_enrichment({**full, "mergeStateStatus": "BEHIND"})},
    )

    assert ask_of(owned_by_operator) == "merge"
    assert board_of(behind)["status"] == "working"
    assert board_of(behind)["headline"] == "Approved"


def test_an_approval_does_not_survive_the_ci_fix_that_changed_the_head():
    # GitHub keeps the approval across the push (stale approvals are not
    # dismissed), so only the journal says the head changed under it.
    fixed = {**_REVIEW_APPROVE, "ts": ts(minutes=30), "role": "fix", "kind": "FIX_CI",
             "status": "done"}
    pr = {**_MERGE_READY_PR, "headRefName": "fix/7-test", "headRefOid": "bbb"}

    card = snapshots.issue_card(
        _approved_row(fixed), NOW, gh_prs={12: pr}, harness_merges=_harness_merges_governed,
    )

    assert board_of(card)["status"] == "working"
    assert board_of(card)["headline"] not in {"Ready to merge", "Approved"}


def test_github_requiring_a_review_on_the_current_head_awaits_review():
    pr = {**_MERGE_READY_PR, "headRefName": "fix/7-test", "reviewDecision": "REVIEW_REQUIRED",
          "mergeStateStatus": "BLOCKED"}

    card = snapshots.issue_card(_approved_row(), NOW, gh_prs={12: pr})

    assert board_of(card)["status"] == "working"
    assert board_of(card)["headline"] == "Awaiting review"


def test_github_auto_merge_armed_by_someone_else_is_not_the_operators_merge():
    pr = {**_MERGE_READY_PR, "headRefName": "feature/manual", "autoMergeRequest": {"enabledAt": "x"}}

    card = snapshots.issue_card(_approved_row(), NOW, gh_prs={12: pr})

    assert board_of(card)["status"] == "working"
    assert "GitHub merges it automatically" in board_of(card)["reason"]


@pytest.mark.parametrize(
    "gh_prs",
    [
        {},  # the enrichment never fetched the PR
        {12: {**_MERGE_READY_PR, "mergeable": "UNKNOWN"}},
        {12: {"state": "OPEN", "reviewDecision": "APPROVED"}},
    ],
)
def test_an_approved_pr_github_has_not_confirmed_never_asks_the_operator_to_merge(gh_prs):
    row = replace(_approved_row(), facts={"pr_ci_gate": "ok"})

    card = snapshots.issue_card(row, NOW, gh_prs=gh_prs, harness_merges=_harness_merges_governed)

    assert board_of(card)["status"] == "working"


@pytest.mark.parametrize(
    "facts,rollup",
    [({"pr_ci_gate": "none"}, None), ({}, [])],
)
def test_a_repo_without_ci_still_asks_the_operator_to_merge(facts, rollup):
    pr = {**_MERGE_READY_PR, "statusCheckRollup": rollup}

    card = snapshots.issue_card(replace(_approved_row(), facts=facts), NOW, gh_prs={12: pr})

    assert ask_of(card) == "merge"


def test_a_reviewer_running_after_the_approval_reads_as_reviewing():
    start = {"ts": ts(minutes=2), "event": "agent_run_start", "role": "review",
             "kind": "RE_REVIEW", "number": 12, "issue": 7, "dispatch": "review-2", "model": ""}

    card = snapshots.issue_card(_approved_row(start), NOW, gh_prs={12: _MERGE_READY_PR})

    assert board_of(card)["status"] == "working"
    assert board_of(card)["headline"] == "Reviewing"


def test_a_projection_without_an_attention_label_keeps_the_journal_escalation():
    # A truncated or stale projection proves no absence: it must not erase
    # the journal's record of an escalation the label view never showed.
    row = replace(row_view([BREAKER_COMMENT, ESCALATION]), facts={"labels": ("ai-working",)})

    card = snapshots.issue_card(row, NOW, gh_issue={"title": "x"})

    assert ask_of(card) == "rearm"


@pytest.mark.parametrize("label,ask", [("needs-human-review", "escalated"), ("needs-budget-review", "budget")])
def test_a_v2_escalation_label_from_the_world_projection_asks_the_operator(label, ask):
    observed = v2_event(
        WorldObserved("issue", 7, "h", {"present": True, "state": "OPEN", "labels": ["ai-working", label]}),
        "observed", 1, issue=7,
    )
    row = v2_deployment([observed]).issues[0]
    # The v2 GitHub enrichment carries no labels: the projection owns them.
    enrichment = service._github_issue_enrichment({"title": "needs action", "labels": [label]})

    card = snapshots.issue_card(row, NOW, gh_issue=enrichment)

    assert ask_of(card) == ask


@pytest.mark.parametrize(
    "age,stamp",
    [(10**400, None), (float("nan"), None), (60, "0001-01-01T00:00:00+00:00"), (-5, None), ("60", None)],
)
def test_a_malformed_stall_never_breaks_the_card(age, stamp):
    event = {"event": "telemetry.warning.v1", "code": "target_stalled",
             "ts": stamp or ts(), "details": {"age_seconds": age}}

    card = snapshots.issue_card(row_view([event]), NOW)

    assert ask_of(card) == "stalled"


def test_a_parked_pending_issue_is_blocked_on_triage():
    card = snapshots.pending_card({"number": 34, "labels": ["ai-parked", "ai-triage"]}, None)

    assert board_of(card) == {
        "status": "blocked", "on": {"kind": "triage", "why": "parked"},
        "headline": "Waiting on triage",
        "reason": "Parked for human review. Unpark it to hand it back to the harness.",
        "since_ts": None,
    }


def test_a_steward_question_puts_a_backlog_row_on_the_board_blocked_on_triage():
    events = [
        v2_event(WorldObserved("issue", 3, "h", {"present": True, "state": "OPEN", "labels": ["ai-triage"]}),
                 "world", 0, issue=3),
        v2_event(TriageDispositionRecorded("blocked_on_human_reply"), "triage", 1, role="steward", issue=3),
    ]
    row = v2_deployment(events).issues[0]

    card = snapshots.backlog_card({"number": 3, "labels": ["ai-triage"]}, NOW, row)

    assert board_of(card)["on"] == {"kind": "triage", "why": "awaiting_reply"}
    assert board_of(card)["since_ts"] is not None
    assert card["rail"]["current"] == "triage"


@pytest.mark.parametrize("labels", [[], ["ai-triage"]])
def test_the_rest_of_the_backlog_stays_off_the_board(labels):
    card = snapshots.backlog_card({"number": 3, "labels": labels}, NOW)

    assert card["board"] is None and card["rail"] is None


@pytest.mark.parametrize(
    "event,on",
    [
        ({"event": "deferred", "state": "waiting", "reason": "blocker", "blocker_issue": 1082},
         {"kind": "issue", "issue": 1082}),
        ({"event": "deferred", "state": "waiting", "reason": "overlap", "blocker_pr": 55},
         {"kind": "overlap", "issue": None, "pr": 55}),
        ({"event": "hold", "reason": "overlap", "blocker": "pr#41"},
         {"kind": "pr", "pr": 41}),
        ({"event": "hold", "reason": "overlap"},
         {"kind": "overlap", "issue": None, "pr": None}),
        # A malformed blocker number is not a target the row can name.
        ({"event": "deferred", "state": "waiting", "reason": "blocker", "blocker_issue": True},
         {"kind": "issue", "issue": None}),
    ],
)
def test_a_hold_is_blocked_on_its_target_from_when_it_began(event, on):
    hold = {"ts": ts(hours=5), "issue": 9, **event}
    card = snapshots.pending_card({"number": 9, "labels": ["ai-ready"]}, row_view([hold], issue=9, legs=[{**RESOLVE, "issue": 9, "number": 9}]))

    assert board_of(card)["status"] == "blocked"
    assert board_of(card)["on"] == on
    assert board_of(card)["since_ts"] == ts(hours=5)


def test_a_queued_pending_issue_waits_on_the_queue_with_no_wait_start():
    card = snapshots.pending_card({"number": 9, "labels": ["ai-ready", "P1"]}, None)

    assert board_of(card)["on"] == {"kind": "queue", "priority": "P1"}
    assert board_of(card)["since_ts"] is None
    assert card["rail"] == {
        "current": "triage", "furthest": "triage", "resolve_attempts": 0,
        "review_rounds": 0, "ci_runs": 0, "steps": 0, "live": False,
    }


def _stalled(**details):
    return {"ts": ts(minutes=30), "event": "telemetry.warning.v1", "issue": 7,
            "code": "target_stalled", "warning": "issue #7 unchanged for 90 min",
            "details": {"kind": "issue", "level": 3, **details}}


def test_a_stall_is_needs_you_and_says_the_harness_stopped_without_escalating():
    card = snapshots.issue_card(row_view([RESOLVE, _stalled(age_seconds=5400)]), NOW)

    assert ask_of(card) == "stalled"
    assert board_of(card)["reason"].endswith("The harness stopped without escalating.")
    # The stall began when the signature stopped changing, not when it was noticed.
    assert board_of(card)["since_ts"] == ts(minutes=120)


def test_a_stall_reports_the_current_non_blocking_deferral_reason():
    defer = {"ts": ts(hours=2), "event": "deferred", "state": "waiting", "issue": 7,
             "reason": "fix_leg_active"}
    card = snapshots.issue_card(row_view([RESOLVE, defer, _stalled(age_seconds=60)]), NOW)

    assert ask_of(card) == "stalled"
    assert board_of(card)["reason"].startswith("Deferred: fix_leg_active.")


@pytest.mark.parametrize("age", [None, "soon", -5, float("nan"), True, 10**18])
def test_a_stall_with_a_malformed_age_is_dated_by_its_own_event(age):
    card = snapshots.issue_card(row_view([RESOLVE, _stalled(age_seconds=age)]), NOW)

    assert ask_of(card) == "stalled"
    assert board_of(card)["since_ts"] == ts(minutes=30)


def test_progress_after_a_stall_answers_it():
    later = {**RESOLVE, "ts": ts(minutes=5)}
    card = snapshots.issue_card(row_view([_stalled(age_seconds=60), later]), NOW)

    assert ask_of(card) is None


def test_a_done_card_leaves_the_board():
    row = replace(row_for([RESOLVE], 7), done=True)

    assert snapshots.issue_card(row, NOW)["board"] is None


@pytest.mark.parametrize(
    "labels",
    [
        ["ai-ready", "needs-human-review", "ai-parked", "ai-blocked", "needs-budget-review"],
        ["ai-done", "ai-working", "P1", "P2"],
        ["", "NEEDS-HUMAN-REVIEW", "ai-parked "],
    ],
)
def test_contradictory_label_sets_produce_one_coherent_board_state(labels):
    row = row_view([BREAKER_COMMENT, ESCALATION])
    card = snapshots.issue_card(row, NOW, gh_issue={"labels": labels})
    pending = snapshots.pending_card({"number": 7, "labels": labels}, row)

    for board in (card["board"], pending["board"]):
        assert board["status"] in {"needs_you", "working", "blocked"}
        assert board["headline"] and board["reason"]
    if "ai-parked" in labels:
        assert pending["board"]["on"] == {"kind": "triage", "why": "parked"}


def _pr_observed(head, state, minutes):
    facts = {"head_sha": head, **({"ci_summary": {"state": state}} if state else {})}
    return {"ts": ts(minutes=minutes), "event": "world.observed.v1", "issue": 7,
            "subject_kind": "pr", "subject_number": 12, "facts": facts}


def test_the_rail_counts_review_rounds_ci_runs_and_resolve_attempts():
    legs = [
        {**RESOLVE, "ts": ts(hours=5), "exit_reason": "wall_cap"},
        {**RESOLVE, "ts": ts(hours=4)},
        {**_REVIEW_APPROVE, "ts": ts(hours=3)},
        {**FIX_CI_2, "ts": ts(minutes=160)},
        {**_REVIEW_APPROVE, "ts": ts(minutes=120), "kind": "RE_REVIEW"},
    ]
    events = [*legs[:3], _pr_observed("aaa", "failure", 170), *legs[3:],
              _pr_observed("bbb", "pending", 100), _pr_observed("bbb", "ok", 90)]
    row = replace(row_view(events, legs=legs), prs=(12,))

    card = snapshots.issue_card(row, NOW)
    detail = snapshots.issue_detail(row, NOW)

    assert card["rail"]["resolve_attempts"] == 2
    assert card["rail"]["review_rounds"] == 2
    assert card["rail"]["ci_runs"] == 2
    assert card["rail"]["current"] == "ci"
    assert [visit["stage"] for visit in detail["history"]] == ["resolve", "review", "ci", "review", "ci"]
    assert detail["history"][-1]["steps"] == [{"kind": "CI", "outcome": "passed", "count": 1}]
    assert detail["rail"] == card["rail"]


def test_checks_reported_before_the_first_approval_stay_in_review():
    legs = [{**RESOLVE, "ts": ts(hours=4)},
            {**_REVIEW_APPROVE, "ts": ts(hours=3), "status": "REQUEST_CHANGES"}]
    events = [legs[0], _pr_observed("aaa", "ok", 200), legs[1]]
    row = replace(row_view(events, legs=legs), prs=(12,))

    detail = snapshots.issue_detail(row, NOW)

    assert detail["rail"]["current"] == "review"
    assert detail["rail"]["furthest"] == "review"
    assert [visit["stage"] for visit in detail["history"]] == ["resolve", "review"]


def test_a_superseded_head_whose_checks_never_settled_is_not_still_running():
    events = [_pr_observed("old", "pending", 50), _pr_observed("new", None, 40)]

    detail = snapshots.issue_detail(row_view(events), NOW)

    ci_chips = [chip for visit in detail["history"] for chip in visit["steps"] if chip["kind"] == "CI"]
    assert ci_chips == [{"kind": "CI", "outcome": "done", "count": 1}]


def test_heads_with_no_checks_reported_are_not_ci_runs():
    # A repo without CI reports `none` on every head; `unknown` is unreadable.
    events = [_pr_observed("a", "none", 60), _pr_observed("b", "none", 50),
              _pr_observed("c", "unknown", 40), _pr_observed("d", "none", 35),
              _pr_observed("d", "failure", 30)]

    detail = snapshots.issue_detail(row_view(events), NOW)

    assert detail["rail"]["ci_runs"] == 1
    ci_chips = [chip for visit in detail["history"] for chip in visit["steps"] if chip["kind"] == "CI"]
    assert ci_chips == [{"kind": "CI", "outcome": "failed", "count": 1}]


def test_the_detail_carries_the_full_harness_message_not_the_row_line():
    detail = snapshots.issue_detail(row_view([BREAKER_COMMENT, ESCALATION]), NOW)

    assert detail["harness_message"].endswith("second line")
    assert detail["escalation_reason"] != detail["harness_message"]


def test_a_huge_harness_message_is_bounded():
    huge = {**BREAKER_COMMENT, "action": {
        **BREAKER_COMMENT["action"], "body": "**Circuit breaker:** " + "x" * 50_000,
    }}

    detail = snapshots.issue_detail(row_view([huge, ESCALATION]), NOW)

    assert len(detail["harness_message"]) == 4000


@pytest.mark.parametrize(
    "cost,projected,ratio",
    [(3.98, 2.0, None), (4.0, 2.0, 2.0), (33.35, 4.28, 7.79), (5.0, None, None), (5.0, 0.0, None)],
)
def test_the_overspend_ratio_appears_only_at_twice_the_projection(cost, projected, ratio):
    row = replace(row_for([RESOLVE], 7), cost_usd=cost)

    assert snapshots.issue_card(row, NOW, projected_cost_usd=projected)["overspend_ratio"] == ratio


@pytest.mark.parametrize("tier,expected", [("complex", "complex"), ("galaxy-brain", None), (None, None), (7, None)])
def test_the_tier_comes_from_the_route_completion_and_only_from_the_ladder(tier, expected):
    route = {"ts": ts(hours=4), "event": "agent_run", "role": "fix", "kind": "ROUTE",
             "issue": 7, "number": 7, "status": "done", "exit_reason": "ok",
             "model": "gpt-6.1-sol", **({"tier": tier} if tier is not None else {})}

    assert snapshots.issue_card(row_view([route, RESOLVE]), NOW)["tier"] == expected


def test_board_cards_validate_against_the_http_contract():
    from issue_agent.app.dashweb.contracts import BacklogCard, IssueCard, PendingCard

    IssueCard.model_validate(snapshots.issue_card(row_view([BREAKER_COMMENT, ESCALATION]), NOW))
    PendingCard.model_validate(snapshots.pending_card({"number": 9, "labels": ["ai-ready"]}, None))
    BacklogCard.model_validate(snapshots.backlog_card({"number": 3, "labels": []}, NOW))
