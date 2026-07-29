"""Journal-side collectors: frecency, liveness, cards, steps.

Every assertion is on what the API promises a client — a sort order, a flag, a
step's state — never on how the aggregation stores it.
"""

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
    assert set(card["flags"]) == {"escalated", "recurred", "spend-blocked", "held"}


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


def test_pending_card_reads_priority_and_the_newest_hold():
    events = [{"ts": ts(hours=1), "event": "hold", "issue": 9, "reason": "overlap"}]
    row = row_for(events, 9)
    card = snapshots.pending_card(
        {"number": 9, "title": "t", "url": "u",
         "labels": [{"name": "ai-ready"}, {"name": "P2"}]}, row)
    assert card["priority"] == "P2"
    assert (card["held"], card["hold_reason"]) == (True, "overlap")
    assert card["overlap"] is None


def test_pending_card_with_no_journal_history_is_not_held():
    card = snapshots.pending_card({"number": 9, "labels": ["ai-ready"]}, None)
    assert (card["held"], card["priority"]) == (False, None)


def test_backlog_excludes_any_issue_the_harness_lifecycle_already_owns():
    assert snapshots.is_backlog({"labels": [{"name": "bug"}]}) is True
    assert snapshots.is_backlog({"labels": [{"name": "ai-working"}]}) is False


def test_backlog_card_ages_the_issue_and_marks_recurrences():
    card = snapshots.backlog_card(
        {"number": 3, "title": "t", "url": "u", "createdAt": ts(days=12),
         "labels": [{"name": "recurrence"}], "comments": 4},
        NOW,
    )
    assert (card["age_days"], card["recurrence"], card["comments"]) == (12, True, 4)


def test_backlog_card_survives_a_missing_created_at_and_list_shaped_comments():
    card = snapshots.backlog_card({"number": 3, "comments": [{}, {}]}, NOW)
    assert card["age_days"] is None and card["comments"] == 2


# ── overview ──────────────────────────────────────────────────────────────────
def test_health_trend_groups_target_issue_lifecycle_and_recurrence_by_utc_day():
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
        issues, recurrences, first_observed_day="2026-07-29"
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

    assert backend.overview("demo")["health_trend"] == [
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
