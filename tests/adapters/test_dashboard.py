"""Operator dashboard — aggregation, stage inference, renders, CLI wiring.

Behavioural throughout: every assertion is about what an operator reads off the
view (this issue is 'resolving', that repair leg cost $2, this daemon is STALE),
never about how the rows are built. Fixtures are hand-written JSONL lines in
tmp_path — the same shape the daemons actually append — including malformed and
truncated ones, since the journal's own contract is that readers tolerate
garbage.
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.app import cli, dashboard as dash
from issue_agent.domain import dashboard as dashboard_domain

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def _now():
    return NOW


def at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()


def run(**kw) -> dict:
    e = {"ts": at(10), "event": "agent_run", "role": "fix", "kind": "RESOLVE",
         "number": 7, "issue": 7, "cost_usd": 1.0, "status": "ok",
         "exit_reason": "ok", "duration_s": 30.0, "dispatch": "d1"}
    e.update(kw)
    return e


def start(**kw) -> dict:
    e = {"ts": at(5), "event": "agent_run_start", "role": "fix", "kind": "RESOLVE",
         "number": 7, "issue": 7, "dispatch": "s1", "model": "sonnet"}
    e.update(kw)
    return e


def action(desc: str, ts_min: float = 9) -> dict:
    return {"ts": at(ts_min), "event": "action", "desc": desc}


def write_jsonl(path: str, *lines) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
    return path


def feeds(fix=(), review=(), steward=()):
    return (
        ("fix", "/j/fix.jsonl", "/l/fix.log", list(fix)),
        ("review", "/j/review.jsonl", "/l/review.log", list(review)),
        ("steward", "/j/steward.jsonl", "/l/steward.log", list(steward)),
    )


# ── parse_action ──────────────────────────────────────────────────────────────
def test_parse_action_relabel_splits_added_and_removed():
    f = dash.parse_action("relabel issue #7: -ai-ready +ai-working")
    assert (f.verb, f.target, f.number) == ("relabel", "issue", 7)
    assert f.added == ("ai-working",) and f.removed == ("ai-ready",)


def test_parse_action_dispatch_numbers_issue_or_pr_by_kind():
    assert dash.parse_action("dispatch RESOLVE #7").target == "issue"
    assert dash.parse_action("dispatch REVIEW #90").target == "pr"
    assert dash.parse_action("dispatch TRIAGE #7").target == "issue"


def test_parse_action_clear_state_carries_merge_and_issue():
    f = dash.parse_action("clear state for pr #90, issue #7 (merged)")
    assert f.merged is True and f.issue == 7 and f.number == 90
    assert dash.parse_action("clear state for pr #90 (closed-unmerged)").merged is False


def test_parse_action_pr_and_issue_verbs():
    assert dash.parse_action("enable auto-merge PR #90").number == 90
    assert dash.parse_action("remove worktree for #7").target == "issue"
    assert dash.parse_action("close issue #7 (completed)").number == 7


def test_parse_action_unknown_sentence_is_none_not_an_error():
    assert dash.parse_action("some future action nobody parsed") is None
    assert dash.parse_action("") is None


# ── issue attribution ─────────────────────────────────────────────────────────
def test_pr_events_attribute_to_the_issue_the_pr_closes():
    events = [run(role="review", kind="REVIEW", number=90, issue=7, status="APPROVE")]
    pr_issue = dash.pr_issue_map(events)
    assert pr_issue == {90: 7}
    # A PR-numbered action with no issue field still lands on issue 7.
    assert dash.event_issue(action("enable auto-merge PR #90"), pr_issue) == 7


def test_deployment_level_events_name_no_issue():
    assert dash.event_issue({"event": "spend_block", "scope": "daily"}, {}) is None
    assert dash.event_issue({"event": "tick_gated", "reason": "x"}, {}) is None


def test_escalation_target_decides_issue_vs_pr():
    pr_issue = {90: 7}
    assert dash.event_issue(
        {"event": "escalation", "target": "issue", "number": 7}, pr_issue) == 7
    assert dash.event_issue(
        {"event": "escalation", "target": "pr", "number": 90}, pr_issue) == 7


# ── stage inference (the heuristic table) ─────────────────────────────────────
def test_stage_running_leg_beats_history():
    events = [run(kind="RESOLVE"), action("relabel issue #7: +ai-working")]
    running = (dashboard_domain._running_leg(start(kind="FIX_CI", number=90)),)
    assert dash.stage_for(events, running) == "fixing CI"


def test_done_issue_cannot_also_be_running():
    events = [
        start(kind="RESOLVE"),
        action("relabel issue #7: +ai-done", ts_min=1),
    ]

    row = dash.build_deployment(
        "demo",
        "o/r",
        (("fix", "/j", "/l", events),),
        now_fn=lambda: NOW,
    ).issues[0]

    assert row.done is True
    assert row.stage == "done"
    assert row.running == ()


def test_recurrence_after_done_reopens_the_issue():
    dep = dashboard_domain.build_deployment(
        "demo",
        "o/r",
        feeds(
            fix=[
                action("relabel issue #7: +ai-done", ts_min=2),
                {"ts": at(1), "event": "recurrence", "kind": "reopen", "issue": 7},
            ]
        ),
        now_fn=_now,
    )
    row = next(row for row in dep.issues if row.issue == 7)
    assert row.done is False
    assert row.stage != "done"


def test_done_marker_after_a_torn_start_remains_terminal():
    dep = dashboard_domain.build_deployment(
        "demo",
        "o/r",
        feeds(
            fix=[
                start(kind="RESOLVE", issue=7, number=7, ts=at(2)),
                action("relabel issue #7: +ai-done", ts_min=1),
            ]
        ),
        now_fn=_now,
    )
    row = next(row for row in dep.issues if row.issue == 7)
    assert row.done is True
    assert row.running == ()


def test_post_merge_jira_sync_does_not_reopen_a_done_issue():
    dep = dashboard_domain.build_deployment(
        "demo",
        "o/r",
        feeds(
            fix=[action("relabel issue #7: +ai-done", ts_min=2)],
            steward=[
                run(
                    ts=at(1),
                    role="steward",
                    kind="JIRA_SYNC",
                    issue=7,
                    number=7,
                )
            ],
        ),
        now_fn=_now,
    )
    row = next(row for row in dep.issues if row.issue == 7)
    assert row.done is True
    assert row.stage == "done"
    assert dep.metrics.done_issues == 1
    assert dep.metrics.in_flight_issues == 0


def test_router_model_is_not_reported_as_an_issue_execution_model():
    events = [
        run(kind="ROUTE", model="luna"),
        run(kind="RESOLVE", model="claude-sonnet-5"),
    ]

    row = dash.build_deployment(
        "demo",
        "o/r",
        (("fix", "/j", "/l", events),),
        now_fn=lambda: NOW,
    ).issues[0]

    assert row.models == ("claude-sonnet-5",)


def test_models_include_live_and_start_only_reviewer_models():
    events = [
        start(kind="RESOLVE", model="gpt-5.6-terra", dispatch="resolve"),
        run(kind="RESOLVE", model="", dispatch="resolve"),
        start(
            role="review",
            kind="REVIEW",
            number=90,
            issue=7,
            model="claude-opus-4-1",
            dispatch="review",
        ),
        run(
            role="review",
            kind="REVIEW",
            number=90,
            issue=7,
            model="",
            dispatch="review",
        ),
    ]

    row = dash.build_deployment(
        "demo",
        "o/r",
        (("fix", "/j", "/l", events),),
        now_fn=lambda: NOW,
    ).issues[0]

    assert row.models == ("gpt-5.6-terra", "claude-opus-4-1")


def test_feed_role_is_attached_to_open_shaped_action_events():
    events = [action("comment issue #7: progress")]

    row = dash.build_deployment(
        "demo",
        "o/r",
        (("steward", "/j", "/l", events),),
        now_fn=lambda: NOW,
    ).issues[0]

    assert row.events[0]["role"] == "steward"


def test_stage_table():
    cases = [
        ([action("relabel issue #7: -ai-pr-open +ai-done")], "done"),
        ([action("clear state for pr #90, issue #7 (merged)")], "done"),
        ([{"ts": at(1), "event": "escalation", "target": "issue", "number": 7}],
         "escalated"),
        ([action("relabel issue #7: +needs-human-review")], "escalated"),
        ([{"ts": at(1), "event": "recurrence", "kind": "reopen", "issue": 7}],
         "recurred"),
        ([{"ts": at(1), "event": "spend_block", "issue": 7}], "blocked (spend)"),
        ([{"ts": at(1), "event": "hold", "issue": 7}], "held"),
        ([action("relabel issue #7: +ai-blocked")], "blocked"),
        ([run(role="review", kind="REVIEW", number=90, status="APPROVE")],
         "awaiting merge"),
        ([run(role="review", kind="REVIEW", number=90, status="REQUEST_CHANGES")],
         "changes requested"),
        ([run(kind="RESOLVE")], "awaiting review"),
        ([run(kind="FIX_CI", number=90)], "awaiting review"),
        ([run(kind="RESOLVE", is_error=True, exit_reason="transport_death")],
         "run failed"),
        ([action("relabel issue #7: -ai-ready +ai-working")], "working"),
        ([action("relabel issue #7: +ai-ready")], "queued"),
        ([], "unknown"),
        ([{"ts": at(1), "event": "tick_gated"}], "unknown"),
    ]
    for events, expected in cases:
        assert dash.stage_for(events) == expected, events


def test_stage_uses_the_newest_decisive_event():
    events = [
        action("relabel issue #7: +ai-ready", ts_min=30),
        run(kind="RESOLVE", ts=at(20)),
        run(role="review", kind="REVIEW", number=90, status="APPROVE", ts=at(10)),
    ]
    assert dash.stage_for(events) == "awaiting merge"


# ── rows ──────────────────────────────────────────────────────────────────────
def test_issue_row_sums_cost_counts_rounds_and_finds_the_pr():
    events = [
        action("relabel issue #7: -ai-ready +ai-working", ts_min=40),
        run(kind="RESOLVE", cost_usd=5.0, ts=at(30)),
        run(role="review", kind="REVIEW", number=90, issue=7,
            status="REQUEST_CHANGES", cost_usd=2.0, ts=at(20)),
        run(kind="ADDRESS_REVIEW", number=90, issue=7, cost_usd=1.0, ts=at(15)),
        run(role="review", kind="RE_REVIEW", number=90, issue=7,
            status="APPROVE", cost_usd=2.0, ts=at(10)),
    ]
    (row,) = dash.build_issue_rows(events, [])
    assert row.issue == 7
    assert row.cost_usd == 10.0
    assert row.rounds == 2 and row.approvals == 1 and row.changes_requested == 1
    assert row.prs == (90,)
    assert row.stage == "awaiting merge"
    assert row.done is False


def test_done_is_the_merge_or_ai_done_trace():
    events = [
        run(kind="RESOLVE", cost_usd=3.0),
        action("relabel issue #7: -ai-pr-open +ai-done", ts_min=1),
    ]
    (row,) = dash.build_issue_rows(events, [])
    assert row.done is True and row.stage == "done"


def test_aborted_run_is_costed_at_its_token_estimate():
    """cost_usd 0 + cost_estimated_usd > 0 is exactly what the ledger bills, so
    the dashboard must not report the leg as free."""
    events = [run(cost_usd=0.0, cost_estimated_usd=4.25, exit_reason="wall_cap")]
    (row,) = dash.build_issue_rows(events, [])
    assert row.cost_usd == 4.25
    assert row.legs[0].estimated is True


def test_running_leg_is_reported_per_issue():
    events = [start(kind="RESOLVE", dispatch="d9", ts=at(3))]
    running = [e for e in events]
    (row,) = dash.build_issue_rows(events, running)
    assert row.stage == "resolving"
    assert row.running[0].kind == "RESOLVE" and row.running[0].model == "sonnet"


def test_settled_dispatch_is_not_reported_as_running():
    from issue_agent.adapters.journal import unsettled_start_events

    events = [
        start(dispatch="d1", ts=at(9)),
        run(dispatch="d1", ts=at(8)),
    ]
    assert unsettled_start_events(events, 86400.0, _now) == []
    (row,) = dash.build_issue_rows(events, unsettled_start_events(events, 86400.0, _now))
    assert row.running == ()


def test_rows_sorted_by_most_recent_activity():
    events = [
        run(number=1, issue=1, ts=at(60)),
        run(number=2, issue=2, ts=at(5)),
    ]
    rows = dash.build_issue_rows(events, [])
    assert [r.issue for r in rows] == [2, 1]


# ── metrics ───────────────────────────────────────────────────────────────────
def test_first_pass_verdicts_counts_one_verdict_per_pr():
    events = [
        run(role="review", kind="REVIEW", number=90, issue=7,
            status="REQUEST_CHANGES", ts=at(30)),
        run(role="review", kind="RE_REVIEW", number=90, issue=7,
            status="APPROVE", ts=at(20)),
        run(role="review", kind="REVIEW", number=91, issue=8,
            status="APPROVE", ts=at(10)),
    ]
    rows = dash.build_issue_rows(events, [])
    assert dash.first_pass_verdicts(rows) == (1, 1)


def test_metrics_cost_mix_and_cost_per_done_issue():
    events = [
        run(number=7, issue=7, kind="RESOLVE", cost_usd=6.0, ts=at(40)),
        run(number=90, issue=7, kind="REVIEW", role="review",
            status="APPROVE", cost_usd=2.0, ts=at(30)),
        action("clear state for pr #90, issue #7 (merged)", ts_min=25),
        run(number=8, issue=8, kind="RESOLVE", cost_usd=2.0, ts=at(20)),
    ]
    dep = dash.build_deployment("n", "o/r", feeds(fix=events), now_fn=_now)
    m = dep.metrics
    assert m.done_issues == 1 and m.in_flight_issues == 1
    assert m.total_cost_usd == 10.0
    assert m.cost_per_done_usd == 10.0  # $ per merged PR: all spend / merges
    assert m.by_kind == (("RESOLVE", 8.0, 2), ("REVIEW", 2.0, 1))
    assert m.first_pass_rate == 1.0


def test_metrics_with_no_done_issue_reports_no_rate_not_a_crash():
    dep = dash.build_deployment("n", "o/r", feeds(fix=[run()]), now_fn=_now)
    assert dep.metrics.done_issues == 0
    assert dep.metrics.cost_per_done_usd == 0.0
    assert dep.metrics.first_pass_rate is None


def test_escalations_and_spend_blocks_counted_even_when_unattributed():
    events = [
        {"ts": at(5), "event": "spend_block", "scope": "daily", "cap": 50},
        {"ts": at(4), "event": "escalation", "target": "pr", "number": 999},
    ]
    dep = dash.build_deployment("n", "o/r", feeds(fix=events), now_fn=_now)
    assert dep.metrics.spend_blocks == 1
    assert dep.metrics.escalations == 1
    assert len(dep.unattributed) == 2


def test_today_usd_scopes_the_global_ledger_to_this_repo():
    ledger = [
        {"ts": at(30), "event": "spend", "repo": "o/r", "issue": 7, "cost_usd": 3.0},
        {"ts": at(30), "event": "spend", "repo": "other/x", "issue": 1, "cost_usd": 9.0},
        {"ts": "2026-07-01T00:00:00+00:00", "event": "spend", "repo": "o/r",
         "cost_usd": 100.0},
    ]
    dep = dash.build_deployment("n", "o/r", feeds(), ledger, now_fn=_now)
    assert dep.metrics.today_usd == 3.0


# ── daemon health ─────────────────────────────────────────────────────────────
def test_health_marks_a_quiet_daemon_stale_and_a_live_one_ok():
    dep = dash.build_deployment(
        "n", "o/r",
        feeds(fix=[run(ts=at(1))], review=[run(ts=at(600), role="review")]),
        now_fn=_now, stale_after_s=1800.0,
    )
    by_role = {h.role: h for h in dep.health}
    assert by_role["fix"].stale is False and by_role["fix"].last_event == "agent_run"
    assert by_role["review"].stale is True
    # A role that never ran is stale too — "produced nothing" is the condition.
    assert by_role["steward"].stale is True and by_role["steward"].last_ts is None


def test_health_keeps_the_journal_and_log_paths_for_drill_down():
    dep = dash.build_deployment("n", "o/r", feeds(fix=[run()]), now_fn=_now)
    assert dep.health[0].journal_path == "/j/fix.jsonl"
    assert dep.health[0].log_path == "/l/fix.log"


# ── merging + malformed input ─────────────────────────────────────────────────
def test_all_three_journals_merge_into_one_issue_timeline(tmp_path):
    fix = write_jsonl(str(tmp_path / "fix" / "journal.jsonl"),
                      run(kind="RESOLVE", cost_usd=1.0, ts=at(30)))
    review = write_jsonl(str(tmp_path / "review" / "journal.jsonl"),
                         run(role="review", kind="REVIEW", number=90, issue=7,
                             status="APPROVE", cost_usd=2.0, ts=at(20)))
    steward = write_jsonl(str(tmp_path / "steward" / "journal.jsonl"),
                          {"ts": at(10), "event": "recurrence", "kind": "refiled",
                           "issue": 7})
    dep = dash.collect("n", "o/r", [fix, review, steward], str(tmp_path / "logs"),
                       str(tmp_path / "ledger.jsonl"), now_fn=_now)
    (row,) = dep.issues
    assert row.cost_usd == 3.0 and row.rounds == 1 and row.recurrences == 1
    assert row.stage == "recurred"  # newest event across all three files wins


def test_malformed_and_empty_lines_are_skipped(tmp_path):
    path = write_jsonl(
        str(tmp_path / "fix" / "journal.jsonl"),
        "",
        "{not json",
        "[1, 2, 3]",  # valid JSON, wrong shape
        json.dumps(run(cost_usd=1.5)),
        '{"ts": "2026-07-28T11:5',  # torn tail of a killed writer
    )
    dep = dash.collect("n", "o/r", [path, str(tmp_path / "nope.jsonl"),
                                    str(tmp_path / "nope2.jsonl")],
                       str(tmp_path / "logs"), str(tmp_path / "ledger.jsonl"),
                       now_fn=_now)
    (row,) = dep.issues
    assert row.cost_usd == 1.5


def test_missing_journals_and_ledger_render_an_empty_deployment(tmp_path):
    dep = dash.collect("n", "o/r", [str(tmp_path / f"{r}.jsonl") for r in dash.ROLE_NAMES],
                       str(tmp_path / "logs"), str(tmp_path / "ledger.jsonl"),
                       now_fn=_now)
    assert dep.issues == ()
    assert dep.metrics.total_cost_usd == 0.0
    assert all(h.stale for h in dep.health)
    # And both renders survive it.
    assert "o/r" in dash.render_text([dep], now_fn=_now)
    assert "o/r" in dash.render_html([dep], now_fn=_now)


def test_events_with_unparseable_ts_do_not_break_the_view():
    events = [run(ts="not-a-timestamp"), run(ts=at(1), cost_usd=2.0)]
    dep = dash.build_deployment("n", "o/r", feeds(fix=events), now_fn=_now)
    assert dep.metrics.total_cost_usd == 3.0


def test_deployment_names_globs_overlays(tmp_path):
    (tmp_path / "b.env").write_text("REPO=o/b\n")
    (tmp_path / "a.env").write_text("REPO=o/a\n")
    (tmp_path / "notes.txt").write_text("ignored\n")
    assert dash.deployment_names(str(tmp_path)) == ("a", "b")
    assert dash.deployment_names(str(tmp_path / "missing")) == ()


# ── renders ───────────────────────────────────────────────────────────────────
def _demo_deployment():
    events = [
        action("relabel issue #7: -ai-ready +ai-working", ts_min=40),
        run(kind="RESOLVE", cost_usd=6.0, ts=at(35), model="opus"),
        run(role="review", kind="REVIEW", number=90, issue=7, status="APPROVE",
            cost_usd=2.0, ts=at(30), session_id="sess-abc"),
        action("clear state for pr #90, issue #7 (merged)", ts_min=25),
        start(kind="RESOLVE", number=8, issue=8, dispatch="live", ts=at(2)),
    ]
    return dash.build_deployment("depl", "o/r", feeds(fix=events), now_fn=_now)


def test_render_text_shows_health_metrics_and_both_issue_sections():
    out = dash.render_text([_demo_deployment()], now_fn=_now)
    assert "o/r" in out and "[depl]" in out
    assert "done (1)" in out and "in flight (1)" in out
    assert "#7" in out and "$    8.00" in out
    assert "resolving" in out  # the live leg on #8
    assert "first-pass approval: 1/1 (100%)" in out
    assert "RESOLVE $6.00" in out


def test_render_text_details_dumps_raw_events():
    out = dash.render_text([_demo_deployment()], now_fn=_now, details=True)
    assert "#7 events" in out and '"event": "agent_run"' in out


def test_render_html_is_self_contained_with_drill_down():
    out = dash.render_html([_demo_deployment()], now_fn=_now)
    assert out.startswith("<!doctype html>")
    assert "<details>" in out and "sess-abc" in out
    assert "http://" not in out and "<script" not in out  # no external assets, no JS
    assert "first-pass approval" in out


def test_render_html_escapes_journal_text():
    events = [action("relabel issue #7: +<img src=x onerror=alert(1)>")]
    dep = dash.build_deployment("d", "o/<r>", feeds(fix=events), now_fn=_now)
    out = dash.render_html([dep], now_fn=_now)
    assert "<img src=x" not in out
    assert "&lt;img" in out and "o/&lt;r&gt;" in out


def test_render_html_empty_deployment_list():
    assert "issue-agent dashboard" in dash.render_html([], now_fn=_now)
    assert "0 deployment" in dash.render_text([], now_fn=_now)


# ── CLI wiring ────────────────────────────────────────────────────────────────
def _args(**kw):
    base = dict(names=[], html="", serve=cli.DASHBOARD_PORT, text=True,
                details=False, stale_minutes=30)
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_dashboard_parses_its_flags():
    args = cli.build_parser().parse_args(
        ["dashboard", "--name", "a", "--name", "b", "--html", "/tmp/d.html",
         "--stale-minutes", "5"]
    )
    assert args.names == ["a", "b"] and args.html == "/tmp/d.html"
    assert args.stale_minutes == 5 and args.func is cli.cmd_dashboard


def test_a_bare_dashboard_invocation_serves_on_the_default_port():
    """The mode an operator gets by typing the shortest command. Changing this
    silently back to text mode would strand anyone who bookmarked the port."""
    args = cli.build_parser().parse_args(["dashboard"])
    assert args.serve == cli.DASHBOARD_PORT == 8321 and args.text is False


def test_serve_takes_an_optional_port_and_keeps_the_default_bare():
    assert cli.build_parser().parse_args(["dashboard", "--serve", "9000"]).serve == 9000
    assert cli.build_parser().parse_args(["dashboard", "--serve"]).serve == cli.DASHBOARD_PORT


def test_text_wins_over_the_default_serve(tmp_path, monkeypatch, capsys):
    """--text must not need `--serve 0` to escape the new default: an operator
    piping the report would otherwise hang on a server."""
    repos = tmp_path / "repos"
    repos.mkdir()
    state = tmp_path / "state"
    (repos / "depl.env").write_text(f'REPO="o/r"\nSTATE_DIR="{state}/fix"\n')
    write_jsonl(str(state / "fix" / "journal.jsonl"), run(cost_usd=3.0))
    monkeypatch.setattr(cli, "REPOS_DIR", str(repos))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(cli, "_dashweb_ready", lambda: pytest.fail("served despite --text"))
    assert cli.cmd_dashboard(_args(text=True, serve=cli.DASHBOARD_PORT)) == 0
    assert "o/r" in capsys.readouterr().out


def test_html_wins_over_the_default_serve(tmp_path, monkeypatch):
    repos = tmp_path / "repos"
    repos.mkdir()
    state = tmp_path / "state"
    (repos / "depl.env").write_text(f'REPO="o/r"\nSTATE_DIR="{state}/fix"\n')
    write_jsonl(str(state / "fix" / "journal.jsonl"), run(cost_usd=3.0))
    monkeypatch.setattr(cli, "REPOS_DIR", str(repos))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(cli, "_dashweb_ready", lambda: pytest.fail("served despite --html"))
    out = tmp_path / "dash.html"
    assert cli.cmd_dashboard(_args(html=str(out), text=False, serve=cli.DASHBOARD_PORT)) == 0
    assert "o/r" in out.read_text()


def test_cli_dashboard_writes_html_snapshot(tmp_path, monkeypatch, capsys):
    repos = tmp_path / "repos"
    repos.mkdir()
    state = tmp_path / "state"
    (repos / "depl.env").write_text(
        f'REPO="o/r"\nSTATE_DIR="{state}/fix"\n'
        f'REVIEW_STATE_DIR="{state}/review"\nSTEWARD_STATE_DIR="{state}/steward"\n'
    )
    write_jsonl(str(state / "fix" / "journal.jsonl"), run(cost_usd=3.0))
    monkeypatch.setattr(cli, "REPOS_DIR", str(repos))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    out = tmp_path / "out" / "dash.html"
    assert cli.cmd_dashboard(_args(html=str(out))) == 0
    body = out.read_text()
    assert "o/r" in body and "#7" in body


def test_cli_dashboard_text_mode_prints(tmp_path, monkeypatch, capsys):
    repos = tmp_path / "repos"
    repos.mkdir()
    state = tmp_path / "state"
    (repos / "depl.env").write_text(f'REPO="o/r"\nSTATE_DIR="{state}/fix"\n')
    write_jsonl(str(state / "fix" / "journal.jsonl"), run(cost_usd=3.0))
    monkeypatch.setattr(cli, "REPOS_DIR", str(repos))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    assert cli.cmd_dashboard(_args()) == 0
    assert "o/r" in capsys.readouterr().out


def test_cli_dashboard_with_no_deployments_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path / "none"))
    assert cli.cmd_dashboard(_args()) == 2


def test_cli_dashboard_writes_nothing_but_the_html(tmp_path, monkeypatch):
    """Read-only contract: a text run must not create or touch any file."""
    repos = tmp_path / "repos"
    repos.mkdir()
    state = tmp_path / "state"
    (repos / "depl.env").write_text(f'REPO="o/r"\nSTATE_DIR="{state}/fix"\n')
    journal = write_jsonl(str(state / "fix" / "journal.jsonl"), run())
    monkeypatch.setattr(cli, "REPOS_DIR", str(repos))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    before = {p: os.stat(p).st_mtime_ns for p in (journal,)}
    listing_before = sorted(str(p) for p in tmp_path.rglob("*"))
    assert cli.cmd_dashboard(_args()) == 0
    assert {p: os.stat(p).st_mtime_ns for p in (journal,)} == before
    assert sorted(str(p) for p in tmp_path.rglob("*")) == listing_before
