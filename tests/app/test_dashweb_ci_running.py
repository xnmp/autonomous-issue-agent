"""CI lifecycle signals come from observed checks, without a GitHub read."""

from tests.domain.builders import observed_pr, with_checks

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json

import pytest

from issue_agent.app.dashweb import service
from issue_agent.domain import dashboard as dash
from issue_agent.domain.events import Correlation, EventEnvelope, Producer, WorldObserved, WorldFeedRetired
from issue_agent.domain.projection import fold
from issue_agent.domain.labels import Label
from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.event_log import EventLog
from issue_agent.adapters.github import GitHubGateway
from issue_agent.app.config import from_env
from issue_agent.app.roles import _reported_world
from issue_agent.app.world_reporter import WorldReporter
from issue_agent.domain.events import decode_event_line
from issue_agent.domain.world import Check, IssueView, PRView, WorldState
from issue_agent.app.dashweb import server
from asgi_client import ASGITestClient


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def check(state="IN_PROGRESS", bucket="pending", **overrides):
    return {"name": "build", "state": state, "bucket": bucket,  **overrides}


def observation(kind, number, facts, seq, role="fix"):
    return EventEnvelope(
        WorldObserved(kind, number, str(seq), facts), str(seq), seq,
        NOW + timedelta(seconds=seq), NOW + timedelta(seconds=seq), seq,
        Producer("demo", "o/r", role, 10, "boot"), Correlation(issue=7, pr=12 if kind == "pr" else None),
    )


def backend(monkeypatch, checks, *, extra=(), ignore="", enrichment=None):
    issue = observation("issue", 7, {"present": True, "state": "OPEN", "labels": ["ai-working"]}, 0)
    events = [issue]
    if checks is not None:
        events.append(observation("pr", 12, {
            "present": True, "closing_issue": 7, "state": "OPEN", "head_sha": "current",
            "checks_known": True, "checks_total": len(checks), "checks": checks,
        }, 1))
    events.extend(extra)
    deployment = dash.build_deployment_v2("demo", "o/r", (),
        fold(events, ), lambda: NOW)
    result = service.Backend(now_fn=lambda: NOW)
    monkeypatch.setattr(result, "snapshot", lambda _name: dash.Snapshot("demo", "o/r", "/target", deployment, (NOW,)))
    monkeypatch.setattr(result, "known", lambda _: True)
    monkeypatch.setattr(result, "_deferred_gh_context", lambda *_args: (({}, enrichment or {}, []), True))
    monkeypatch.setattr(result, "verification", lambda: {"rows": []})
    monkeypatch.setattr(result, "_cached_detail_limits", lambda *_args: ({}, 0))
    monkeypatch.setattr(service.cli, "deployment_env_field", lambda _name, _field: ignore)
    return result


def signal(result):
    listed = result.issues("demo")["in_flight"][0]["pr_ci_state"]
    detailed = result.issue("demo", 7)["pr_ci_state"]
    assert listed == detailed
    return listed


def reported_pr(tmp_path, pr, seq, *, role="fix", ignore=""):
    path = tmp_path / f"{role}-{seq}.jsonl"
    events = EventLog(str(path), Producer("demo", "o/r", role, 10, f"boot-{seq}"))
    control = SQLiteControlStore(str(tmp_path / f"{role}-{seq}.db"))
    control.initialize()
    control.bind_event_outbox(events.prepare, events.append_committed)
    cfg = from_env({"REPO": "o/r"})
    _reported_world(lambda: WorldState(prs=(pr,)), control, cfg, role, lambda _: None)()
    payload = next(
        event.payload for event in map(decode_event_line, path.read_text().splitlines())
        if isinstance(event.payload, WorldObserved) and event.payload.subject_kind == "pr"
    )
    return observation("pr", pr.number, payload.facts, seq, role)


def live_feed(tmp_path, role, *, ignore=""):
    events = EventLog(str(tmp_path / f"{role}.jsonl"), Producer("demo", "o/r", role, 10, "boot"))
    control = SQLiteControlStore(str(tmp_path / f"{role}.db"))
    control.initialize()

    def prepare(payload, correlation):
        stamp = NOW + timedelta(seconds=len(reported_events(tmp_path)) + 1)
        return replace(events.prepare(payload, correlation), occurred_at=stamp, observed_at=stamp)

    control.bind_event_outbox(prepare, events.append_committed,
        destination=events.producer.feed_identity)
    reporter = WorldReporter(control, "demo", )
    return (
        lambda world: reporter.wrap(lambda: world)(),
        lambda: control.retire_projection_feed(deployment="demo", repository="o/r", role=role),
    )


def reported_events(tmp_path):
    return tuple(decode_event_line(line)
        for path in tmp_path.glob("*.jsonl") for line in path.read_text().splitlines())


def assert_http_signal(result, expected, group="in_flight"):
    http = ASGITestClient(server.create_app(backend=result, offload_backend=False),
        base_url="http://127.0.0.1:8321")
    listed = http.get("/api/repos/demo/issues").json()[group][0]["pr_ci_state"]
    detailed = http.get("/api/repos/demo/issues/7").json()["pr_ci_state"]
    assert (listed, detailed) == (expected, expected)


def test_steward_same_head_preserves_known_ci_after_deduplicated_fixer_read(monkeypatch, tmp_path):
    raw = {"number": 12, "state": "OPEN", "headRefOid": "current",
        "closingIssuesReferences": [{"number": 7}]}

    def runner(args, _token):
        if args[1:3] == ["issue", "list"]:
            return "[]"
        if args[1:3] == ["pr", "list"]:
            return '[{"number":12}]'
        return json.dumps(raw)

    gateway = GitHubGateway("o/r", runner=runner)
    from issue_agent.domain.checks import Observed
    monkeypatch.setattr("issue_agent.adapters.github.observe_checks", lambda _repo, p, _query:
        Observed(p.head_ref_oid, p.head_ref_oid, frozenset(), (Check("build", "IN_PROGRESS", "pending"),)))
    fixer_world = WorldState(prs=(gateway.get_pr(12),))
    fix, _ = live_feed(tmp_path, "fix")
    steward, _ = live_feed(tmp_path, "steward")
    fix(fixer_world)
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")
    steward(gateway.build_triage_world())
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")
    before = reported_events(tmp_path)
    fix(fixer_world)
    assert reported_events(tmp_path) == before
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")


@pytest.mark.parametrize("removal", ["absence", "retirement"])
@pytest.mark.parametrize("done", [False, True])
def test_merged_ci_never_resurrects_after_feed_removal(monkeypatch, tmp_path, removal, done):
    fix, _ = live_feed(tmp_path, "fix")
    review, retire = live_feed(tmp_path, "review")
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    fix(WorldState(prs=(pr,)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")
    review(WorldState(prs=(with_checks(pr, state="MERGED", checks_known=False),)))
    if done:
        steward, _ = live_feed(tmp_path, "steward")
        steward(WorldState(issues=(IssueView(7, state="CLOSED"),)))
    group = "done" if done else "in_flight"
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), None, group)
    if removal == "absence":
        review(WorldState())
    else:
        retire()
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), None, group)


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_terminal_pr_observation_clears_old_feed_ci(monkeypatch, tmp_path, state):
    pending = reported_pr(tmp_path, observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),)), 1)
    latest = reported_pr(tmp_path, observed_pr(12, head_ref_oid="current", closing_issue=7,
        state=state, checks_known=False), 2, role="review")
    result = backend(monkeypatch, None, extra=(pending, latest),
        enrichment={12: {"statusCheckRollup": [{"name": "build", "conclusion": "SUCCESS"}]}})
    assert signal(result) is None


@pytest.mark.parametrize("count", [100, 101, 1000])
def test_reported_matrix_keeps_running_signal(monkeypatch, tmp_path, count):
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=tuple(Check(f"job-{i}", "IN_PROGRESS", "pending") for i in range(count)))
    assert signal(backend(monkeypatch, None, extra=(reported_pr(tmp_path, pr, 1),))) == "pending"


def test_legacy_http_board_and_detail_preserve_running_activity(monkeypatch):
    result = backend(monkeypatch, [check()], ignore="[", enrichment={12: {
        "state": "OPEN", "statusCheckRollup": [{"name": "build", "status": "IN_PROGRESS"}],
    }})
    snap = result.snapshot("demo")
    legacy = replace(snap, deployment=replace(snap.deployment, schema_version=1,
        issues=tuple(replace(row, facts={}) for row in snap.deployment.issues)))
    monkeypatch.setattr(result, "snapshot", lambda _: legacy)
    http = ASGITestClient(server.create_app(backend=result, offload_backend=False), base_url="http://127.0.0.1:8321")
    board = http.get("/api/repos/demo/issues").json()
    detail = http.get("/api/repos/demo/issues/7").json()
    assert board["in_flight"][0]["issue"] == 7
    assert board["in_flight"][0]["pr_ci_state"] == "pending"
    assert detail["issue"] == 7 and detail["pr_ci_state"] == "pending"








@pytest.mark.parametrize("summary", [None, [], 7, {}, {"state": "pending"}])
def test_malformed_ci_summary_falls_back_to_legacy_checks(monkeypatch, summary):
    event = observation("pr", 12, {"present": True, "state": "OPEN", "head_sha": "current",
        "checks_known": True, "checks": [check()], "ci_summary": summary}, 1)
    assert_http_signal(backend(monkeypatch, None, extra=(event,)), "pending")


@pytest.mark.parametrize("state", [None, [], {}, 7, "invalid", "unknown"])
def test_invalid_summary_state_falls_back_to_raw_checks(monkeypatch, tmp_path, state):
    event = reported_pr(tmp_path, observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),)), 1)
    summary = {**event.payload.facts["ci_summary"], "state": state}
    event = replace(event, payload=replace(event.payload, facts={**event.payload.facts, "ci_summary": summary}))
    assert_http_signal(backend(monkeypatch, None, extra=(event,)), "pending")


def test_production_collector_projects_reported_checks(monkeypatch, tmp_path):
    reported_pr(tmp_path, observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),)), 1)
    paths = []
    for role in ("fix", "review", "steward"):
        directory = tmp_path / role
        directory.mkdir()
        paths.append(str(directory / "journal.jsonl"))
    (tmp_path / "fix" / "events-v2.jsonl").write_text((tmp_path / "fix-1.jsonl").read_text())
    steward, _ = live_feed(tmp_path, "steward")
    steward(WorldState(issues=(IssueView(7, labels=frozenset({Label.AI_WORKING})),)))
    (tmp_path / "steward" / "events-v2.jsonl").write_text((tmp_path / "steward.jsonl").read_text())
    result = backend(monkeypatch, None, ignore="build")
    monkeypatch.setattr(result, "snapshot", service.Backend.snapshot.__get__(result))
    monkeypatch.setattr(result, "overlay_field", lambda _name, key: {"REPO": "o/r", "TARGET_DIR": str(tmp_path)}[key])
    monkeypatch.setattr(service.cli, "journal_paths", lambda _: paths)
    assert_http_signal(result, "pending")


@pytest.mark.parametrize("checks", [None, {}, [7], [{}], [check(name=None)], [check(name="")]])
def test_malformed_known_checks_claim_no_ci(monkeypatch, checks):
    event = observation("pr", 12, {"present": True, "state": "OPEN", "head_sha": "current",
        "checks_known": True, "checks": checks, "ci_state": "pending"}, 1)
    assert_http_signal(backend(monkeypatch, None, extra=(event,)), None)


def test_producer_ci_claim_cannot_override_raw_checks(monkeypatch, tmp_path):
    event = reported_pr(tmp_path, observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),)), 1)
    event = replace(event, payload=replace(event.payload, facts={**event.payload.facts, "ci_state": "ok"}))
    assert_http_signal(backend(monkeypatch, None, extra=(event,)), "pending")


@pytest.mark.parametrize("checks,expected", [([check()], "pending"), ([check("SUCCESS", "pass")], None)])
def test_truncated_observation_cannot_claim_ci_settlement(monkeypatch, checks, expected):
    event = observation("pr", 12, {"present": True, "state": "OPEN", "head_sha": "current",
        "checks_known": True, "checks_total": 101, "checks": checks, "observation_truncated": True}, 1)
    assert_http_signal(backend(monkeypatch, None, extra=(event,)), expected)


@pytest.mark.parametrize("state", ["QUEUED", "IN_PROGRESS"])
def test_current_head_pending_checks_outrank_failure(monkeypatch, tmp_path, state):
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", state), Check("lint", "FAILURE", "fail")))
    event = reported_pr(tmp_path, pr, 1)
    assert signal(backend(monkeypatch, None, extra=(event,),
        enrichment={12: {"headRefOid": "old", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}})) == "pending"


@pytest.mark.parametrize("state,bucket,expected", [
    ("SUCCESS", "pass", "passing"), ("FAILURE", "fail", "failing"),
    ("CANCELLED", "cancel", "passing"), ("SKIPPED", "skipping", "passing"),
])
def test_newest_feed_settlement_clears_running(monkeypatch, tmp_path, state, bucket, expected):
    pending = reported_pr(tmp_path, observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),)), 1)
    settled = reported_pr(tmp_path, observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", state, bucket),)), 2, role="review")
    assert signal(backend(monkeypatch, None, extra=(pending, settled))) == expected




@pytest.mark.parametrize("state", [None, [], {}, 7, "invalid"])
def test_unreadable_or_historical_ci_claims_no_indicator(monkeypatch, state):
    event = observation("pr", 12, {"present": True, "state": "OPEN", "ci_state": state}, 1)
    assert signal(backend(monkeypatch, None, extra=(event,))) is None


def test_truncation_cannot_hide_last_running_job(monkeypatch, tmp_path):
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("pass", "SUCCESS", "pass"),) * 100 + (Check("last", "IN_PROGRESS", "pending"),))
    assert signal(backend(monkeypatch, None, extra=(reported_pr(tmp_path, pr, 1),))) == "pending"


def test_large_matrix_preserves_last_running_job_when_check_metadata_exceeds_budget(monkeypatch, tmp_path):
    checks = tuple(Check(f"job-{i}", "SUCCESS", "pass", NOW.isoformat(), run_id=i + 1)
        for i in range(999)) + (Check("last", "IN_PROGRESS", "pending", NOW.isoformat()),)
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7, checks=checks)
    assert_http_signal(backend(monkeypatch, None, extra=(reported_pr(tmp_path, pr, 1),)), "pending")


@pytest.mark.parametrize("settled", [False, True])
def test_thousand_long_named_jobs_preserve_ci_transitions(monkeypatch, tmp_path, settled):
    fix, _ = live_feed(tmp_path, "fix")
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    if settled:
        fix(WorldState(prs=(pr,)))
        assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")
    checks = tuple(Check(f"job-{i:04d}-" + "x" * 60, "SUCCESS", "pass") for i in range(1000))
    if not settled:
        checks = checks[:-1] + (replace(checks[-1], state="IN_PROGRESS", bucket="pending"),)
    fix(WorldState(prs=(with_checks(pr, checks=checks),)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)),
        "passing" if settled else "pending")


@pytest.mark.parametrize("count", [20, 800])
@pytest.mark.parametrize("identity", ["started_at", "run_id"])
def test_same_head_large_matrix_rerun_emits_fresh_observation(monkeypatch, tmp_path, count, identity):
    fix, _ = live_feed(tmp_path, "fix")
    review, _ = live_feed(tmp_path, "review")
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=tuple(Check(f"job-{i}", "IN_PROGRESS", "pending",
            NOW.isoformat(), run_id=i + 1) for i in range(count)))
    fix(WorldState(prs=(pr,)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")
    review(WorldState(prs=(with_checks(pr, checks=tuple(
        replace(check, state="SUCCESS", bucket="pass") for check in pr.checks)),)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "passing")
    before = reported_events(tmp_path)
    rerun = replace(pr.checks[-1], **{identity:
        (NOW + timedelta(minutes=1)).isoformat() if identity == "started_at" else count + 1})
    world = WorldState(prs=(with_checks(pr, checks=pr.checks[:-1] + (rerun,)),))
    fix(world)
    assert len(reported_events(tmp_path)) == len(before) + 1
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")
    before = reported_events(tmp_path)
    fix(world)
    assert reported_events(tmp_path) == before




def test_incomplete_same_head_checks_preserve_ci_until_a_complete_settlement(monkeypatch, tmp_path):
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    pending = reported_pr(tmp_path, pr, 1)
    checks = tuple(Check(f"job-{i}-" + "x" * 60, "SUCCESS", "pass") for i in range(999)) + pr.checks
    incomplete = reported_pr(tmp_path, with_checks(pr, checks=checks), 2)
    assert_http_signal(backend(monkeypatch, None, extra=(pending, incomplete)), "pending")
    settled = reported_pr(tmp_path, with_checks(pr, checks=(Check("build", "SUCCESS", "pass"),)), 3)
    assert_http_signal(backend(monkeypatch, None, extra=(pending, incomplete, settled)), "passing")
    empty = reported_pr(tmp_path, with_checks(pr, checks=()), 4)
    assert_http_signal(backend(monkeypatch, None, extra=(pending, incomplete, settled, empty)), None)


def test_ready_issue_carries_running_signal_in_pending_list(monkeypatch, tmp_path):
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    ready = observation("issue", 7, {"present": True, "state": "OPEN", "labels": ["ai-ready"]}, 2)
    result = backend(monkeypatch, None, extra=(reported_pr(tmp_path, pr, 1), ready))
    assert result.issues("demo")["pending"][0]["pr_ci_state"] == "pending"




@pytest.mark.parametrize("head,known", [("", True), ("current", False)])
def test_unread_or_unidentified_head_claims_no_running_ci(monkeypatch, tmp_path, head, known):
    pr = observed_pr(12, head_ref_oid=head, closing_issue=7, checks_known=known,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    assert signal(backend(monkeypatch, None, extra=(reported_pr(tmp_path, pr, 1),))) is None


def test_newer_observation_without_ci_preserves_same_head_knowledge(monkeypatch, tmp_path):
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    newer = observation("pr", 12, {"present": True, "state": "OPEN", "head_sha": "current"}, 2, "review")
    assert signal(backend(monkeypatch, None, extra=(reported_pr(tmp_path, pr, 1), newer))) == "pending"


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("role", ["fix", "steward"])
def test_only_current_head_checks_count(monkeypatch, tmp_path, known, role):
    fix, _ = live_feed(tmp_path, "fix")
    publish = fix if role == "fix" else live_feed(tmp_path, role)[0]
    pr = observed_pr(12, head_ref_oid="old", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    fix(WorldState(prs=(pr,)))
    publish(WorldState(prs=(with_checks(pr, head_ref_oid="current", checks=(), checks_known=known),)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), None)


@pytest.mark.parametrize("removal", ["absence", "retirement"])
def test_head_advance_survives_feed_removal(monkeypatch, tmp_path, removal):
    fix, _ = live_feed(tmp_path, "fix")
    steward, retire = live_feed(tmp_path, "steward")
    pr = observed_pr(12, head_ref_oid="old", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    fix(WorldState(prs=(pr,)))
    steward(WorldState(prs=(with_checks(pr, head_ref_oid="current", checks=(), checks_known=False),)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), None)
    if removal == "absence":
        steward(WorldState())
    else:
        retire()
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), None)


@pytest.mark.parametrize("removal", ["absence", "retirement"])
def test_ci_is_hidden_without_any_present_pr_feed(monkeypatch, tmp_path, removal):
    fix, retire = live_feed(tmp_path, "fix")
    steward, _ = live_feed(tmp_path, "steward")
    steward(WorldState(issues=(IssueView(7, state="OPEN", labels=frozenset({Label.AI_WORKING})),)))
    fix(WorldState(prs=(observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),)),)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")
    if removal == "absence":
        fix(WorldState())
    else:
        retire()
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), None)


def test_unread_same_feed_checks_preserve_knowledge(monkeypatch, tmp_path):
    fix, _ = live_feed(tmp_path, "fix")
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    fix(WorldState(prs=(pr,)))
    fix(WorldState(prs=(with_checks(pr, checks=(), checks_known=False),)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending")


def test_known_empty_checks_clear_previous_activity(monkeypatch, tmp_path):
    fix, _ = live_feed(tmp_path, "fix")
    review, _ = live_feed(tmp_path, "review")
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    fix(WorldState(prs=(pr,)))
    review(WorldState(prs=(with_checks(pr, checks=()),)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), None)


@pytest.mark.parametrize("state,labels", [
    ("CLOSED", frozenset()), ("OPEN", frozenset({Label.AI_DONE})),
])
def test_done_issue_keeps_open_pr_ci_until_pr_is_terminal(monkeypatch, tmp_path, state, labels):
    fix, _ = live_feed(tmp_path, "fix")
    steward, _ = live_feed(tmp_path, "steward")
    steward(WorldState(issues=(IssueView(7, state=state, labels=labels),)))
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    fix(WorldState(prs=(pr,)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), "pending", "done")
    fix(WorldState(prs=(with_checks(pr, state="MERGED", checks_known=False),)))
    assert_http_signal(backend(monkeypatch, None, extra=reported_events(tmp_path)), None, "done")


def test_late_retired_alias_cannot_replace_successful_ci(monkeypatch, tmp_path):
    pr = observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),))
    alias = reported_pr(tmp_path, pr, 1)
    alias = replace(alias, producer=replace(alias.producer, deployment="old-alias"))
    retired = replace(observation("pr", 12, {}, 2, "steward"),
        payload=WorldFeedRetired("old-alias", "o/r", "fix"))
    success = reported_pr(tmp_path, with_checks(pr, checks=(Check("build", "SUCCESS", "pass"),)), 3)
    late = replace(reported_pr(tmp_path, pr, 4), producer=alias.producer)
    events = (alias, retired, success, late)
    assert_http_signal(backend(monkeypatch, None, extra=events), "passing")


@pytest.mark.parametrize("enrichment", [None, {12: {
    "state": "OPEN", "headRefOid": "current",
    "statusCheckRollup": [{"name": "build", "status": "IN_PROGRESS"}],
}}])
def test_origin_dev_observations_without_producer_ci_state_keep_ci(monkeypatch, tmp_path, enrichment):
    event = reported_pr(tmp_path, observed_pr(12, head_ref_oid="current", closing_issue=7,
        checks=(Check("build", "IN_PROGRESS", "pending"),)), 1)
    # origin/dev publishes raw checks without a classified CI field.
    event = replace(event, payload=replace(event.payload,
        facts={**{key: value for key, value in event.payload.facts.items() if key not in {"ci_state", "ci_summary"}},
            "checks": [{**value,  "started_at": "", "run_id": None}
                for value in event.payload.facts["checks"]]}))
    assert_http_signal(backend(monkeypatch, None, extra=(event,), enrichment=enrichment), "pending")


def test_required_gate_and_all_report_activity_survive_compaction(monkeypatch, tmp_path):
    from issue_agent.domain.checks import Observed, RequiredCheck
    reports = tuple(Check(f"matrix-{i}", "SUCCESS", "pass", required=True) for i in range(1000))
    reports += (Check("rust (macos-latest)", "IN_PROGRESS", "pending"),)
    policy = frozenset(RequiredCheck(c.name) for c in reports[:-1])
    pr = PRView(12, head_ref_oid="current", closing_issue=7,
        ci=Observed("current", "current", policy, reports))
    event = reported_pr(tmp_path, pr, 1)
    assert event.payload.facts["observation_truncated"] is True
    result = backend(monkeypatch, None, extra=(event,))
    assert_http_signal(result, "pending")
    projected = fold((event,)).world_facts[("pr", 12)]
    assert projected["ci_gate"] == "ok"
    assert projected["ci_summary"]["tested_revision"] == "current"


def test_missing_required_names_are_journaled_without_running_activity(tmp_path):
    from issue_agent.domain.checks import Observed, RequiredCheck
    pr = PRView(12, head_ref_oid="current", closing_issue=7,
        ci=Observed("current", "current", frozenset({RequiredCheck("unit", 1)}), ()))
    facts = reported_pr(tmp_path, pr, 1).payload.facts
    assert facts["missing_required"] == ["unit (app 1)"]
    assert facts["ci_summary"]["state"] == "pending"
    assert facts["ci_summary"]["activity"] == "settled"
