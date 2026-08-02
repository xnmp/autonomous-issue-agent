"""Journal-side collectors: frecency, liveness, cards, steps.

Every assertion is on what the API promises a client — a sort order, a flag, a
step's state — never on how the aggregation stores it.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.dashweb import ghdata, system
from issue_agent.app.dashweb import service, snapshots
from issue_agent.domain import dashboard as dash

NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def ts(**delta) -> str:
    return (NOW - timedelta(**delta)).replace(microsecond=0).isoformat()


def build(events, name="demo", repo="o/r"):
    feeds = (("fix", "/j/fix", "/l/fix", events), ("review", "/j/rev", "/l/rev", []),
             ("steward", "/j/std", "/l/std", []))
    return dash.build_deployment(name, repo, feeds, (), now_fn=lambda: NOW)


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
    assert steps[-1] == {"kind": "REVIEW", "state": "running", "status": None, "cost_usd": 0.0}


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


def test_issue_card_partitions_completed_time_into_working_and_idle():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=3), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "overlap", "blocker_pr": 90},
        {"ts": ts(hours=2), "event": "deferred", "state": "cleared",
         "issue": 7, "reason": "overlap", "blocker_pr": 90},
        {"ts": ts(), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 14_400
    assert card["working_s"] == 10_800
    assert card["idle_s"] == 3_600
    assert card["idle_active"] is False


def test_issue_card_keeps_an_active_hold_ticking_as_idle_not_working():
    events = [
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(minutes=30), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "overlap", "blocker_issue": 8},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 7_200
    assert card["working_s"] == 5_400
    assert card["idle_s"] == 1_800
    assert card["idle_active"] is True


def test_stall_overlapping_a_hold_is_removed_from_idle_not_working_time():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=2), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "overlap", "blocker_pr": 90},
        {"ts": ts(hours=1), "event": "agent_run_stall", "role": "fix",
         "issue": 7, "dispatch": "d1", "stall_started_at": ts(hours=1.5),
         "suspended_seconds": 3600},
        {"ts": ts(), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]

    card = snapshots.issue_card(row_for(events, 7), NOW)

    assert card["elapsed_s"] == 10_800
    assert card["working_s"] == 7_200
    assert card["idle_s"] == 3_600


def test_identified_stall_cluster_keeps_duration_and_idle_from_one_detector():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(minutes=30), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "overlap", "blocker_pr": 90},
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
    assert card["working_s"] == 9_000
    assert card["idle_s"] == 1_800


def test_legacy_stall_cluster_keeps_duration_and_idle_from_one_detector():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(minutes=30), "event": "deferred", "state": "waiting",
         "issue": 7, "reason": "overlap", "blocker_pr": 90},
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
    assert card["working_s"] == 9_900
    assert card["idle_s"] == 900


def test_issue_card_and_eta_exclude_annotated_stall_time():
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
    assert snapshots.mean_wall_clock_s([row]) == 6000.0


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
    assert snapshots.mean_wall_clock_s([row]) == 6000.0


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
    assert snapshots.mean_wall_clock_s([row]) == 7200.0


def test_issue_detail_uses_the_same_working_idle_partition_as_cards():
    events = [
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"},
        {"ts": ts(hours=1), "event": "hold", "issue": 7, "reason": "overlap"},
    ]

    detail = snapshots.issue_detail(row_for(events, 7), NOW)

    assert detail["working_s"] == 3_600
    assert detail["idle_s"] == 3_600
    assert detail["idle_active"] is True


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


def test_issue_card_flags_escalation_recurrence_spend_block_and_hold():
    events = [
        RESOLVE,
        {"ts": ts(hours=2), "event": "escalation", "target": "issue", "number": 7},
        {"ts": ts(hours=2), "event": "recurrence", "issue": 7},
        {"ts": ts(hours=2), "event": "spend_block", "issue": 7},
        {"ts": ts(hours=1), "event": "hold", "issue": 7, "reason": "overlaps PR #12"},
    ]
    card = snapshots.issue_card(row_for(events, 7), NOW)
    assert set(card["flags"]) == {"escalated", "recurred", "spend-blocked", "waiting"}


def test_historical_hold_is_cleared_by_a_later_resolve_dispatch():
    events = [
        {"ts": ts(hours=3), "event": "hold", "issue": 7, "reason": "overlap"},
        {"ts": ts(hours=2), "event": "agent_run_start", "role": "fix",
         "kind": "RESOLVE", "issue": 7, "number": 7},
    ]
    assert "waiting" not in snapshots.issue_card(row_for(events, 7), NOW)["flags"]


def test_structured_defer_stays_active_until_its_clear_transition():
    waiting = {
        "ts": ts(hours=3), "event": "deferred", "state": "waiting",
        "issue": 7, "reason": "overlap", "blocker_pr": 90,
    }
    cleared = {**waiting, "ts": ts(hours=1), "state": "cleared"}
    assert snapshots.pending_card({}, row_for([waiting], 7))["hold_reason"] == "waiting on PR #90"
    assert snapshots.pending_card({}, row_for([waiting, cleared], 7))["held"] is False


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

    payload = backend.issues("demo")

    assert [card["issue"] for card in payload["pending"]] == [7]
    assert payload["in_flight"] == []


def test_mean_wall_clock_is_none_until_an_issue_is_done():
    assert snapshots.mean_wall_clock_s(build([RESOLVE]).issues) is None


def test_mean_wall_clock_measures_done_issues_only():
    events = [
        {"ts": ts(hours=4), "event": "agent_run_start", "role": "fix", "kind": "RESOLVE",
         "issue": 7, "number": 7, "dispatch": "d1"},
        RESOLVE,
        {"ts": ts(hours=2), "event": "action",
         "desc": "clear state for pr #12, issue #7 (merged)"},
    ]
    assert snapshots.mean_wall_clock_s(build(events).issues) == 7200.0


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


def test_backlog_excludes_any_issue_the_harness_lifecycle_already_owns():
    assert snapshots.is_backlog({"labels": [{"name": "bug"}]}) is True
    assert snapshots.is_backlog({"labels": [{"name": "ai-working"}]}) is False


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
    card = snapshots.issue_card(row_view([BREAKER_COMMENT, ESCALATION]), NOW)
    assert card["escalation_reason"] == (
        "Circuit breaker: 5 review rounds on this PR ended without a verdict "
        "— a human must decide."
    )


def test_an_escalation_with_no_comment_beside_it_has_no_reason():
    card = snapshots.issue_card(row_view([ESCALATION]), NOW)
    assert card["escalation_reason"] is None


def test_escalation_reason_ignores_a_comment_about_another_target():
    other = {**BREAKER_COMMENT,
             "action": {**BREAKER_COMMENT["action"], "number": 99}}
    assert snapshots.issue_card(row_view([other, ESCALATION]), NOW)[
        "escalation_reason"
    ] is None


def test_escalation_reason_ignores_a_comment_many_events_away():
    filler = [
        {"ts": ts(hours=2), "event": "action", "issue": 7,
         "desc": f"relabel issue #7: +ai-working{n}"}
        for n in range(6)
    ]
    row = row_view([BREAKER_COMMENT, *filler, ESCALATION])
    assert snapshots.issue_card(row, NOW)["escalation_reason"] is None


@pytest.mark.parametrize("body", [None, "", "   ", {"not": "a string"}])
def test_a_malformed_escalation_comment_body_yields_no_reason(body):
    broken = {**BREAKER_COMMENT,
              "action": {**BREAKER_COMMENT["action"], "body": body}}
    assert snapshots.issue_card(row_view([broken, ESCALATION]), NOW)[
        "escalation_reason"
    ] is None


def test_a_reason_retires_with_the_escalation_that_produced_it():
    """The clients gate their unblock affordance on this field: once a relabel
    has taken needs-human-review off, there is nothing left to unblock."""
    cleared = {"ts": ts(hours=1), "event": "action", "issue": 7,
               "desc": "relabel issue #7: -needs-human-review"}
    row = row_view([BREAKER_COMMENT, ESCALATION, cleared])
    assert snapshots.issue_card(row, NOW)["escalation_reason"] is None


def test_a_finished_issue_reports_no_escalation_reason():
    row = replace(row_view([BREAKER_COMMENT, ESCALATION]), done=True)
    assert snapshots.issue_card(row, NOW)["escalation_reason"] is None


def test_a_body_less_comment_does_not_shadow_the_real_reason():
    empty = {**BREAKER_COMMENT,
             "action": {**BREAKER_COMMENT["action"], "body": ""}}
    row = row_view([BREAKER_COMMENT, empty, ESCALATION])
    assert snapshots.issue_card(row, NOW)["escalation_reason"].startswith(
        "Circuit breaker:"
    )


def test_two_comments_equally_close_resolve_to_the_later_one():
    later = {**BREAKER_COMMENT,
             "action": {**BREAKER_COMMENT["action"], "body": "**Later:** this one"}}
    row = row_view([BREAKER_COMMENT, ESCALATION, later])
    assert snapshots.issue_card(row, NOW)["escalation_reason"] == "Later: this one"


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

    assert card["human_action"] == "escalated"
    assert card["elapsed_s"] == 3_600  # 3h ago → escalated 2h ago
    assert card["human_wait_s"] == 7_200  # and still waiting, live


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

    assert card["human_action"] == "escalated"
    assert card["elapsed_s"] == 3_600
    assert card["human_wait_s"] == 7_200


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
    assert card["human_action"] is None
    assert card["human_wait_s"] is None


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
    assert card["human_action"] is None


def test_a_journal_block_freezes_the_clock_and_a_cleared_one_does_not():
    start = {"ts": ts(hours=3), "event": "agent_run_start", "role": "fix",
             "kind": "RESOLVE", "issue": 7, "number": 7, "dispatch": "d1"}
    blocked = {"ts": ts(hours=2), "event": "action", "role": "fix", "issue": 7,
               "desc": "relabel issue #7: -ai-working +ai-blocked"}
    card = snapshots.issue_card(row_view([start, RESOLVE, blocked]), NOW)
    assert card["human_action"] == "awaiting-reply"
    assert card["elapsed_s"] == 3_600

    cleared = {"ts": ts(hours=1), "event": "action", "role": "fix", "issue": 7,
               "desc": "relabel issue #7: -ai-blocked +ai-working"}
    card = snapshots.issue_card(
        row_view([start, RESOLVE, blocked, cleared]), NOW
    )
    assert card["human_action"] is None
    assert card["human_wait_s"] is None


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
    assert card["human_action"] is None and card["elapsed_s"] == 7_200


def test_a_budget_review_label_also_reads_as_needing_a_human():
    row = row_for([RESOLVE], 7)
    card = snapshots.issue_card(
        row, NOW,
        gh_issue={"labels": [{"name": "needs-budget-review"}], "updatedAt": ts(hours=1)},
    )
    assert card["human_action"] == "escalated"
    assert card["human_wait_s"] == 3_600


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
