"""Observable codex per-leg budget enforcement and telemetry (#29)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import shlex
import sys
import time
from datetime import datetime, timezone

import pytest

from issue_agent.adapters.codex_runner import CodexRunner
from issue_agent.domain.pricing import ModelRates, RateTable, UnknownModelPolicy
from tests.fakes.legacy_journal import Journal
from issue_agent.adapters.journal import JournalingRuntime, read_events
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.outcome import ExitReason, infra_death


SID = "019fa244-0000-7de0-8e18-e56f423bbbcc"


def _fake_codex(tmp_path, body: str) -> str:
    path = tmp_path / "codex"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _rollout_path(codex_home, sid: str = SID):
    day = codex_home / "sessions" / "2026" / "07" / "28"
    day.mkdir(parents=True, exist_ok=True)
    return day / f"rollout-2026-07-28T00-00-00-{sid}.jsonl"


def _session_meta(cwd: str | None = None, sid: str = SID) -> str:
    """The root session's identity record; dated ahead so it postdates the run."""
    return json.dumps({"timestamp": "2099-07-29T00:00:00Z", "type": "session_meta", "payload": {
        "id": sid, "originator": "codex_exec", "source": "exec", "cwd": cwd or os.getcwd()}}) + "\n"


def _write_rollout(
    codex_home,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    sid: str = SID,
    cwd: str | None = None,
) -> None:
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
    }
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": usage, "total_token_usage": usage},
        }
    }
    _rollout_path(codex_home, sid).write_text(_session_meta(cwd, sid) + json.dumps(event) + "\n")


# $5 in / $30 out / $0.50 cached per MTok, the shipped fallback shape.
CARD = RateTable("card", "v1", "USD", {}, UnknownModelPolicy.USE_FALLBACK,
                 ModelRates(5_000_000, 30_000_000, 500_000, 5_000_000))


def _runner(tmp_path, body: str, **overrides) -> CodexRunner:
    values = {
        "codex_bin": _fake_codex(tmp_path, body),
        "env": {"OPENAI_API_KEY": "k"},
        "rate_table": CARD,
        "poll_slice": 0.05,
    }
    values.update(overrides)
    return CodexRunner(**values)


def _chatty_body(sid: str = SID, seconds: int = 40) -> str:
    return (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        f'for i in $(seq 1 {seconds}); do echo "exec step $i" >&2; sleep 0.05; done\n'
        "printf '%s' '{\"status\":\"pr_opened\",\"summary\":\"done\",\"needs\":\"\"}' > \"$out\"\n"
    )


@pytest.mark.parametrize("home_source", [
    "inherited", "runner", "task", "empty_task", "relative_runner", "relative_task",
    "symlink_parent",
])
def test_accounting_uses_the_launched_child_home_without_scanning_foreign_archive(
    tmp_path, monkeypatch, home_source,
):
    ambient = tmp_path / "ambient"
    monkeypatch.setenv("HOME", str(ambient))
    foreign = ambient / ".codex"
    day = foreign / "sessions" / datetime.now(timezone.utc).strftime("%Y/%m/%d")
    day.mkdir(parents=True)
    for number in range(513):
        (day / f"foreign-{number}.txt").touch()
    selected = tmp_path / "selected"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner_env = {"OPENAI_API_KEY": "k"}
    task_env = {}
    monkeypatch.setenv("CODEX_HOME", str(foreign))
    if home_source == "inherited":
        monkeypatch.setenv("CODEX_HOME", str(selected))
    elif home_source == "runner":
        runner_env["CODEX_HOME"] = str(selected)
    elif home_source == "task":
        runner_env["CODEX_HOME"] = str(foreign)
        task_env["CODEX_HOME"] = str(selected)
    elif home_source == "empty_task":
        runner_env["HOME"] = str(tmp_path / "child-home")
        runner_env["CODEX_HOME"] = str(foreign)
        task_env["CODEX_HOME"] = ""
        selected = tmp_path / "child-home" / ".codex"
    elif home_source.startswith("relative_"):
        selected = worktree / "relative-home"
        (runner_env if home_source == "relative_runner" else task_env)["CODEX_HOME"] = "relative-home"
    else:
        actual = tmp_path / "actual" / "work"
        actual.mkdir(parents=True)
        alias = tmp_path / "alias"
        alias.symlink_to(actual, target_is_directory=True)
        runner_env["CODEX_HOME"] = str(alias / ".." / "relative-home")
        selected = actual.parent / "relative-home"
    _write_rollout(selected, input_tokens=10_000, output_tokens=100, cwd=str(worktree))
    observed = tmp_path / "observed-home"
    body = f'(cd -P -- "${{CODEX_HOME:-$HOME/.codex}}" && pwd -P) > "{observed}"\n' + _chatty_body(seconds=1)
    result = asyncio.run(_runner(tmp_path, body, env=runner_env).run(
        RuntimePlan(prompt="x", cwd=str(worktree), env=task_env, max_budget_usd=20.0),
    ))
    assert observed.read_text().strip() == str(selected)
    assert result.exit_reason is ExitReason.OK
    assert result.cost_usd == pytest.approx(0.053)


def test_crossing_budget_kills_run_and_settles_operator_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, input_tokens=5_000_000, output_tokens=100_000)
    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(
        _runner(tmp_path, _chatty_body()),
        journal,
        repo="o/r",
        role="fix",
    )
    runtime.set_context("fix", "RESOLVE", 29, 29)

    result = asyncio.run(
        runtime.run(RuntimePlan(prompt="x", max_budget_usd=20.0))
    )

    assert result.status == "error_max_budget_usd"
    assert result.envelope_subtype == "error_max_budget_usd"
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert infra_death(result.exit_reason) is False
    event = [e for e in read_events(journal.path) if e["event"] == "agent_run"][0]
    assert event["cost_usd"] == pytest.approx(28.0)
    assert event["usage"]["input_tokens"] == 5_000_000
    assert event["exit_reason"] == "budget_exhausted"
    assert event["envelope_subtype"] == "error_max_budget_usd"
    assert event["duration_s"] > 0


def test_final_rollout_crossing_cannot_escape_on_clean_exit(tmp_path, monkeypatch):
    """The final token update can land immediately before EOF, inside one poll
    slice. Completion must re-check it instead of accepting an over-budget OK."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout = _rollout_path(tmp_path)
    rollout.write_text(_session_meta())
    usage = {
        "input_tokens": 5_000_000,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 100_000,
    }
    event = json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": usage, "total_token_usage": usage},
        }
    })
    body = (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        f"printf '%s\\n' '{event}' >> '{rollout}'\n"
        "printf '%s' '{\"status\":\"pr_opened\",\"summary\":\"done\",\"needs\":\"\"}' > \"$out\"\n"
    )

    result = asyncio.run(
        _runner(tmp_path, body, poll_slice=30.0).run(
            RuntimePlan(prompt="x", max_budget_usd=20.0)
        )
    )

    assert result.status == "error_max_budget_usd"
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert result.cost_usd == pytest.approx(28.0)



def test_headerless_run_uses_rollout_for_inflight_budget_enforcement(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage = {
        "input_tokens": 5_000_000,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 100_000,
    }
    session_meta = {
        "timestamp": "2099-07-29T00:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": SID,
            "originator": "codex_exec",
            "source": "exec",
            "cwd": str(worktree),
        },
    }
    token_count = {
        "timestamp": "2099-07-29T00:00:01Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": usage, "total_token_usage": usage},
        }
    }
    _rollout_path(tmp_path).write_text(
        json.dumps(session_meta) + "\n" + json.dumps(token_count) + "\n"
    )
    body = (
        'for i in $(seq 1 40); do echo "exec step $i" >&2; sleep 0.05; done\n'
    )

    result = asyncio.run(
        _runner(tmp_path, body).run(
            RuntimePlan(prompt="x", cwd=str(worktree), max_budget_usd=20.0)
        )
    )

    assert result.status == "error_max_budget_usd"
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert result.session_id == SID
    assert result.cost_usd == pytest.approx(28.0)


def test_echoed_usage_fixture_does_not_price_or_kill_inflight_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    body = (
        "echo 'Token usage: total=5,100,000 input=0 "
        "(+ 0 cached) output=5,100,000' >&2\n"
        'for i in $(seq 1 10); do echo "exec step $i" >&2; sleep 0.03; done\n'
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        "printf '%s' '{\"status\":\"pr_opened\",\"summary\":\"done\","
        "\"needs\":\"\"}' > \"$out\"\n"
    )

    result = asyncio.run(
        _runner(tmp_path, body, poll_slice=0.01).run(
            RuntimePlan(prompt="x", max_budget_usd=20.0)
        )
    )

    assert result.status == "pr_opened"
    assert result.exit_reason is ExitReason.OK
    assert result.cost_usd == 0.0


def _live_owned_rollout_command(tmp_path, *, input_tokens: int, output_tokens: int) -> str:
    script = tmp_path / "write-live-rollout.py"
    script.write_text(f"""import json
from datetime import datetime, timezone
from pathlib import Path
stamp = datetime.now(timezone.utc).isoformat()
usage = {{"input_tokens": {input_tokens}, "output_tokens": {output_tokens}}}
records = [{{"timestamp": stamp, "type": "session_meta", "payload": {{
    "id": {SID!r}, "originator": "codex_exec", "source": "exec", "cwd": {os.getcwd()!r}}}}},
    {{"timestamp": stamp, "type": "event_msg", "payload": {{"type": "token_count",
      "info": {{"last_token_usage": usage, "total_token_usage": usage}}}}}}]
Path({str(_rollout_path(tmp_path))!r}).write_text("".join(json.dumps(r) + "\\n" for r in records))
""")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}\n"


def test_large_owned_rollout_usage_still_enforces_the_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    body = _live_owned_rollout_command(tmp_path, input_tokens=0, output_tokens=5_100_000)
    body += (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        "printf '%s' '{\"status\":\"pr_opened\",\"summary\":\"done\",\"needs\":\"\"}' > \"$out\"\n"
    )
    result = asyncio.run(_runner(tmp_path, body).run(RuntimePlan("x", max_budget_usd=20.0)))
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert result.cost_usd == pytest.approx(153.0)


def test_under_budget_run_completes_normally(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, input_tokens=1_000_000, output_tokens=10_000)

    result = asyncio.run(
        _runner(tmp_path, _chatty_body(seconds=1)).run(
            RuntimePlan(prompt="x", max_budget_usd=20.0)
        )
    )

    assert result.status == "pr_opened"
    assert result.exit_reason is ExitReason.OK


def test_cache_heavy_live_budget_sampler_does_not_kill_a_sub_twenty_dollar_leg(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(
        tmp_path,
        input_tokens=3_974_000,
        cached_input_tokens=3_773_000,
        output_tokens=9_500,
    )

    result = asyncio.run(
        _runner(tmp_path, _chatty_body(seconds=1)).run(
            RuntimePlan(prompt="x", max_budget_usd=20.0)
        )
    )

    assert result.status == "pr_opened"
    assert result.exit_reason is ExitReason.OK
    # (201000 * $5 + 3773000 * $0.50 + 9500 * $30) / 1e6.
    assert result.cost_usd == pytest.approx(3.1765)


def test_resumed_leg_uses_its_own_usage_delta(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, input_tokens=5_000_000, output_tokens=100_000)

    result = asyncio.run(
        _runner(tmp_path, _chatty_body(seconds=1)).run(
            RuntimePlan(prompt="x", max_budget_usd=20.0, resume=SID)
        )
    )

    assert result.exit_reason is ExitReason.OK
    assert result.cost_usd == pytest.approx(0.0)


def test_a_resumed_leg_subtracts_the_baselines_cached_tokens_too(tmp_path, monkeypatch):
    """The cached counter is cumulative like the others: pricing the raw total
    would re-ledger the previous leg's cache reads, and a cached count larger
    than the leg's own input delta reads as distrusted telemetry (full rate)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(
        tmp_path, input_tokens=1_000_000, cached_input_tokens=800_000, output_tokens=10_000
    )
    grown = json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": 1},
                "total_token_usage": {
                    "input_tokens": 2_000_000,
                    "cached_input_tokens": 1_700_000,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 20_000,
                },
            },
        }
    })
    body = _chatty_body(seconds=1).replace(
        "printf '%s'",
        f"printf '%s\\n' '{grown}' >> '{_rollout_path(tmp_path)}'\nprintf '%s'",
    )

    result = asyncio.run(
        _runner(tmp_path, body).run(
            RuntimePlan(prompt="x", max_budget_usd=20.0, resume=SID)
        )
    )

    assert result.exit_reason is ExitReason.OK
    # delta: 1_000_000 in of which 900_000 cached, 10_000 out.
    # (100000 * $5 + 900000 * $0.50 + 10000 * $30) / 1e6.
    assert result.cost_usd == pytest.approx(1.25)


def test_budget_only_watchdog_samples_a_silent_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    body = _live_owned_rollout_command(tmp_path, input_tokens=5_000_000, output_tokens=100_000)
    runner = _runner(
        tmp_path,
        body + "sleep 3\n",
        idle_timeout=None,
        max_runtime=None,
    )

    started = time.monotonic()
    result = asyncio.run(
        runner.run(RuntimePlan(prompt="x", max_budget_usd=20.0))
    )

    assert time.monotonic() - started < 2
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED
