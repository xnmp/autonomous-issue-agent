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


def test_repos_payload_falls_back_to_journal_staleness_without_proc():
    dep = build([{"ts": ts(minutes=1), "event": "tick", "role": "fix"}])
    snap = dash.Snapshot("demo", "o/r", "/t", dep, (NOW,))
    payload = snapshots.repos_payload([snap], {}, False, NOW)
    assert payload["liveness"] == "journal-heuristic"
    assert payload["warnings"]
    assert payload["repos"][0]["active"] is True


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
    leg = dash.Leg(None, "fix", "RESOLVE", 1, 1, "m", "", "budget_exhausted", 0.0, 0.0, False, "")
    assert snapshots.leg_state(leg) == "failed"


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


def _pr_row():
    return row_for([
        RESOLVE,
        {"ts": ts(hours=1), "event": "agent_run", "role": "review",
         "kind": "REVIEW", "issue": 7, "number": 90, "status": "APPROVE"},
    ], 7)


def test_approved_green_open_pr_without_automerge_is_merge_ready():
    card = snapshots.issue_card(
        _pr_row(),
        NOW,
        gh_prs={90: {
            "state": "OPEN",
            "reviewDecision": "APPROVED",
            "autoMergeRequest": None,
            "statusCheckRollup": [{
                "conclusion": "SUCCESS",
                "completedAt": ts(minutes=20),
            }],
            "latestReviews": [{
                "state": "APPROVED",
                "submittedAt": ts(minutes=30),
            }],
        }},
    )

    assert card["human_action"] == "merge-ready"
    assert card["human_wait_s"] == 1_200
    assert "merge-ready" in card["flags"]


@pytest.mark.parametrize(
    "override",
    [
        {"statusCheckRollup": [{"conclusion": "FAILURE"}]},
        {"autoMergeRequest": {"enabledAt": "now"}},
        {"state": "MERGED", "mergedAt": ts()},
    ],
)
def test_pr_that_still_has_harness_work_is_not_merge_ready(override):
    pr = {
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "autoMergeRequest": None,
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        **override,
    }

    assert snapshots.issue_card(
        _pr_row(), NOW, gh_prs={90: pr}
    )["human_action"] is None


def test_ai_blocked_question_is_awaiting_reply_until_a_later_comment():
    question = {
        "createdAt": ts(minutes=40),
        "body": "**Agent needs input:** choose A or B\n\n_Reply to unblock._",
        "author": {"login": "agent-bot"},
    }
    issue = {
        "labels": [{"name": "ai-blocked"}],
        "comments": [question],
    }

    card = snapshots.issue_card(row_for([RESOLVE], 7), NOW, gh_issue=issue)
    assert card["human_action"] == "awaiting-reply"
    assert card["human_wait_s"] == 2_400

    issue["comments"].append({
        "createdAt": ts(minutes=20),
        "body": "Automated progress update.",
        "author": {"login": "other-app[bot]"},
    })
    still_waiting = snapshots.issue_card(row_for([RESOLVE], 7), NOW, gh_issue=issue)
    assert still_waiting["human_action"] == "awaiting-reply"

    issue["comments"].append({
        "createdAt": ts(minutes=10),
        "body": "Choose A.",
        "author": {"login": "operator"},
    })
    resumed = snapshots.issue_card(row_for([RESOLVE], 7), NOW, gh_issue=issue)
    assert resumed["human_action"] is None


def test_pr_thread_ai_blocked_question_is_awaiting_reply():
    card = snapshots.issue_card(
        _pr_row(),
        NOW,
        gh_prs={90: {
            "state": "OPEN",
            "labels": [{"name": "ai-blocked"}],
            "comments": [{
                "createdAt": ts(minutes=25),
                "body": (
                    "**Agent needs clarification on review:** choose a path\n\n"
                    "_Reply to unblock._"
                ),
                "author": {"login": "agent-bot"},
            }],
        }},
    )

    assert card["human_action"] == "awaiting-reply"
    assert card["human_wait_s"] == 1_500


def test_past_escalation_is_not_a_current_human_action_after_resume():
    row = row_for([
        RESOLVE,
        {"ts": ts(hours=2), "event": "escalation", "issue": 7},
        {"ts": ts(hours=1), "event": "action",
         "desc": "relabel issue #7: -needs-human-review +ai-working"},
    ], 7)

    card = snapshots.issue_card(
        row,
        NOW,
        gh_issue={"labels": [{"name": "ai-working"}]},
    )
    assert card["human_action"] is None

    done = snapshots.issue_card(
        replace(row, done=True),
        NOW,
        gh_issue={"labels": [{"name": "needs-human-review"}]},
    )
    assert done["human_action"] is None


def test_malformed_check_rollup_is_not_merge_ready():
    pr = {
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "autoMergeRequest": None,
        "statusCheckRollup": ["invalid"],
    }

    assert snapshots.issue_card(
        _pr_row(), NOW, gh_prs={90: pr}
    )["human_action"] is None


def test_malformed_comment_author_does_not_break_human_action_projection():
    issue = {
        "labels": [{"name": "ai-blocked"}],
        "comments": [{
            "createdAt": ts(minutes=10),
            "body": "**Agent needs input:** choose A\n\n_Reply to unblock._",
            "author": ["invalid"],
        }],
    }

    card = snapshots.issue_card(row_for([RESOLVE], 7), NOW, gh_issue=issue)
    assert card["human_action"] == "awaiting-reply"


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
