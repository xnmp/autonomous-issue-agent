"""Runtime budget meters: Claude OAuth usage and the codex rollout rate limits.

No test touches the network — `claude_usage` takes an injectable fetcher, and
the codex reader is pure over lines. The recurring assertion is that an
unreadable meter reports an ERROR rather than 0%: a false "0% used" is the one
reading that would talk an operator into starting a batch they cannot finish.
"""

import json

import pytest

from issue_agent.adapters.dashweb import usage
from issue_agent.adapters.dashweb.ghdata import TtlCache


def fresh_cache():
    return TtlCache(ttl_s=60.0)


# ── claude ────────────────────────────────────────────────────────────────────
def creds(tmp_path, token="sk-oauth-secret"):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token}}))
    return str(path)


def test_utilization_objects_are_read_as_percentages():
    parsed = usage.parse_claude_usage({
        "five_hour": {"utilization": 12.5, "resets_at": "2026-07-29T15:00:00Z"},
        "seven_day": {"utilization": 43.0, "resets_at": 1785728237},
    })
    assert parsed["five_hour_pct"] == 12.5
    assert parsed["five_hour_resets_at"] == "2026-07-29T15:00:00Z"
    assert parsed["seven_day_pct"] == 43.0
    assert parsed["seven_day_resets_at"].startswith("2026-")
    assert parsed["error"] is None


def test_a_flat_pct_variant_is_accepted_too():
    parsed = usage.parse_claude_usage({"five_hour_pct": 5, "seven_day_pct": 9})
    assert (parsed["five_hour_pct"], parsed["seven_day_pct"]) == (5.0, 9.0)


@pytest.mark.parametrize("payload", [{}, [], None, {"quota": "unknown"}, "text"])
def test_an_unrecognised_shape_errors_rather_than_reporting_zero_used(payload):
    parsed = usage.parse_claude_usage(payload)
    assert parsed["error"]
    assert parsed["five_hour_pct"] is None and parsed["seven_day_pct"] is None


def test_claude_usage_sends_the_token_to_the_fetcher_and_never_returns_it(tmp_path):
    seen = {}

    def fetch(token):
        seen["token"] = token
        return {"five_hour": {"utilization": 1.0}}

    result = usage.claude_usage(fetch=fetch, credentials_path=creds(tmp_path),
                               cache=fresh_cache())
    assert seen["token"] == "sk-oauth-secret"
    assert "sk-oauth-secret" not in json.dumps(result)


def test_a_fetch_failure_reports_an_error_without_leaking_the_token(tmp_path):
    def fetch(token):
        raise OSError(f"connection refused while sending {token}")

    result = usage.claude_usage(fetch=fetch, credentials_path=creds(tmp_path),
                               cache=fresh_cache())
    assert result["error"] and "sk-oauth-secret" not in result["error"]
    assert result["five_hour_pct"] is None


def test_a_transient_fetch_failure_keeps_the_last_successful_reading(tmp_path):
    last_good = {}
    first = usage.claude_usage(
        fetch=lambda _token: {
            "five_hour": {"utilization": 17.0},
            "seven_day": {"utilization": 42.0},
        },
        credentials_path=creds(tmp_path),
        cache=fresh_cache(),
        last_good=last_good,
    )

    def fail(_token):
        raise OSError("temporary outage")

    stale = usage.claude_usage(
        fetch=fail,
        credentials_path=creds(tmp_path),
        cache=fresh_cache(),
        last_good=last_good,
    )

    assert first["error"] is None
    assert (stale["five_hour_pct"], stale["seven_day_pct"]) == (17.0, 42.0)
    assert stale["source"] == "oauth · cached"
    assert "last successful" in stale["error"]


def test_an_unexpected_response_keeps_the_last_successful_reading(tmp_path):
    last_good = {}
    usage.claude_usage(
        fetch=lambda _token: {
            "five_hour": {"utilization": 17.0},
            "seven_day": {"utilization": 42.0},
        },
        credentials_path=creds(tmp_path),
        cache=fresh_cache(),
        last_good=last_good,
    )

    stale = usage.claude_usage(
        fetch=lambda _token: {"unexpected": "response"},
        credentials_path=creds(tmp_path),
        cache=fresh_cache(),
        last_good=last_good,
    )

    assert (stale["five_hour_pct"], stale["seven_day_pct"]) == (17.0, 42.0)
    assert stale["source"] == "oauth · cached"
    assert "last successful" in stale["error"]


def test_missing_credentials_are_reported_and_no_fetch_is_attempted(tmp_path):
    def fetch(token):
        raise AssertionError("must not be called without credentials")

    result = usage.claude_usage(fetch=fetch,
                               credentials_path=str(tmp_path / "absent.json"),
                               cache=fresh_cache())
    assert "credentials" in result["error"]


def test_a_malformed_credentials_file_yields_no_token(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    assert usage.read_oauth_token(str(path)) == ""
    assert usage.read_oauth_token(str(tmp_path / "absent.json")) == ""


def test_the_second_call_inside_the_ttl_does_not_refetch(tmp_path):
    calls = []
    cache = fresh_cache()

    def fetch(token):
        calls.append(token)
        return {"five_hour": {"utilization": 1.0}}

    for _ in range(3):
        usage.claude_usage(fetch=fetch, credentials_path=creds(tmp_path), cache=cache)
    assert len(calls) == 1


# ── codex ─────────────────────────────────────────────────────────────────────
def rollout_line(pct, resets=1785728237, plan="plus"):
    return json.dumps({
        "type": "event_msg",
        "payload": {
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": pct, "window_minutes": 10080,
                            "resets_at": resets},
                "plan_type": plan,
            }
        },
    })


def test_the_last_rate_limits_snapshot_in_the_rollout_wins():
    lines = [rollout_line(1.0), '{"type":"turn"}', rollout_line(7.5)]
    assert usage.parse_rollout_rate_limits(lines)["primary"]["used_percent"] == 7.5


def test_a_torn_or_unrelated_line_is_skipped_not_fatal():
    lines = ['{"type":"turn"}', '{"rate_limits": {"prim', rollout_line(2.0)]
    assert usage.parse_rollout_rate_limits(lines)["primary"]["used_percent"] == 2.0


def test_a_rollout_without_any_snapshot_yields_none():
    assert usage.parse_rollout_rate_limits(['{"type":"turn"}', ""]) is None


def test_codex_block_reports_the_window_plan_and_reset_as_iso():
    block = usage.codex_block(
        json.loads(rollout_line(3.25))["payload"]["rate_limits"], "2026-07-29T12:00:00+00:00"
    )
    assert block["weekly_pct"] == 3.25
    assert block["window_minutes"] == 10080
    assert block["plan"] == "plus"
    assert block["resets_at"].startswith("2026-")
    assert block["error"] is None


@pytest.mark.parametrize("limits", [None, {}, {"primary": "nope"}])
def test_a_missing_or_malformed_snapshot_errors_rather_than_reporting_zero(limits):
    block = usage.codex_block(limits, None)
    assert block["error"] and block["weekly_pct"] is None


def test_newest_rollout_picks_the_most_recently_modified_file(tmp_path):
    import os

    old = tmp_path / "a" / "rollout-1.jsonl"
    new = tmp_path / "b" / "rollout-2.jsonl"
    for p in (old, new):
        p.parent.mkdir(parents=True)
        p.write_text(rollout_line(1.0))
    os.utime(old, (1_700_000_000, 1_700_000_000))
    assert usage.newest_rollout(str(tmp_path)) == str(new)


def test_no_codex_install_is_an_error_string_not_a_crash(tmp_path):
    block = usage.codex_usage(str(tmp_path / "absent"))
    assert block["error"] and block["source"] == "rollout"


def test_codex_usage_reads_the_newest_rollout_end_to_end(tmp_path):
    path = tmp_path / "s" / "rollout-1.jsonl"
    path.parent.mkdir()
    path.write_text(rollout_line(6.0) + "\n")
    block = usage.codex_usage(str(tmp_path))
    assert block["weekly_pct"] == 6.0 and block["sampled_at"]


# ── harness caps ──────────────────────────────────────────────────────────────
def test_harness_usage_sums_today_globally_and_per_repo(tmp_path, monkeypatch):
    from issue_agent.app import cli
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from issue_agent.adapters.control_store import (
        BudgetLimit, ReservationRequest, SQLiteControlStore, SettlementRequest,
    )
    from issue_agent.domain.contracts import BudgetScope, Money, PricingBasis, RunId

    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path / "repos"))
    (tmp_path / "repos").mkdir()
    (tmp_path / "repos" / "demo.env").write_text("REPO='o/r'\nDAILY_SPEND_CAP_USD=20\n")
    clock = [datetime(2026, 7, 29, tzinfo=timezone.utc)]
    control = SQLiteControlStore(str(tmp_path / "control.db"), now=lambda: clock[0])
    control.initialize()
    settled = (("2026-07-29T01:00:00+00:00", "o/r", 1, 1_500_000),
               ("2026-07-29T02:00:00+00:00", "o/other", 2, 2_000_000),
               ("2026-07-28T02:00:00+00:00", "o/r", 1, 9_000_000))
    for index, (stamp, repository, issue, micros) in enumerate(settled):
        clock[0] = datetime.fromisoformat(stamp)
        owner = f"{repository}:fix"
        assert control.lease(repository, "fix", issue, owner)
        scope = BudgetScope(repository, "repository", "USD", "all", datetime(2026, 1, 1, tzinfo=timezone.utc),
                            datetime(2027, 1, 1, tzinfo=timezone.utc), ZoneInfo("UTC"))
        run = RunId(f"leg-{index}")
        assert control.reserve(ReservationRequest(
            run, repository, "fix", "RESOLVE", owner, Money("USD", micros),
            (BudgetLimit(scope, Money("USD", 10**9)),), issue, issue=issue,
        )).admitted
        assert control.settle(SettlementRequest(
            run, Money("USD", micros), PricingBasis.PROVIDER_REPORTED, f"receipt-{index}", None, None, None,
        )).disposition.value == "available"
    now = lambda: datetime(2026, 7, 29, 12, tzinfo=timezone.utc)  # noqa: E731
    def config_for(name):
        path = tmp_path / "repos" / f"{name}.env"
        return cli.read_repo_env_field(str(path), "REPO"), cli.daily_cap(name), 0.0

    result = usage.harness_usage(
        ["demo"],
        config_for,
        control.path,
        now_fn=now,
    )
    assert result["today_all_repos_usd"] == 3.5
    assert result["per_repo"][0]["today_usd"] == 1.5
    assert result["per_repo"][0]["daily_cap_usd"] == 20.0


def test_harness_usage_never_reports_missing_authority_as_zero(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        usage.harness_usage(
            [],
            lambda _name: ("", 0.0, 0.0),
            str(tmp_path / "missing" / "control.db"),
        )


def test_usage_payload_marks_unavailable_harness_spend_unknown(monkeypatch):
    monkeypatch.setattr(
        usage,
        "claude_usage",
        lambda fetch=None: {"error": None},
    )
    monkeypatch.setattr(
        usage,
        "codex_usage",
        lambda: {"error": None},
    )

    def unavailable_harness(*_args, **_kwargs):
        raise RuntimeError("control store does not exist")

    monkeypatch.setattr(usage, "harness_usage", unavailable_harness)

    result = usage.usage_payload([], lambda _name: ("", 0.0, 0.0))

    assert result["harness"]["today_all_repos_usd"] is None
    assert result["warnings"] == [
        "harness caps unreadable (control store does not exist)"
    ]
