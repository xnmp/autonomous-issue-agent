"""Observable codex per-leg budget enforcement and telemetry (#29)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time

import pytest

from issue_agent.adapters.codex_runner import (
    CodexRunner,
    budget_overrun_usd,
    read_rollout_turns,
)
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


def _write_rollout(
    codex_home,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    sid: str = SID,
) -> None:
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
    }
    event = {
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": usage, "total_token_usage": usage},
        }
    }
    _rollout_path(codex_home, sid).write_text(json.dumps(event) + "\n")


def _runner(tmp_path, body: str, **overrides) -> CodexRunner:
    values = {
        "codex_bin": _fake_codex(tmp_path, body),
        "env": {"OPENAI_API_KEY": "k"},
        "cost_per_mtok_in": 5.0,
        "cost_per_mtok_out": 30.0,
        "poll_slice": 0.05,
    }
    values.update(overrides)
    return CodexRunner(**values)


def _chatty_body(sid: str = SID, seconds: int = 40) -> str:
    return (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        f'echo "session id: {sid}" >&2\n'
        f'for i in $(seq 1 {seconds}); do echo "exec step $i" >&2; sleep 0.05; done\n'
        "printf '%s' '{\"status\":\"pr_opened\",\"summary\":\"done\",\"needs\":\"\"}' > \"$out\"\n"
    )


def test_priced_spend_reports_only_when_the_threshold_is_reached():
    usage = {"input_tokens": 5_000_000, "output_tokens": 100_000}
    assert budget_overrun_usd(
        usage,
        cost_per_mtok_in=5.0,
        cost_per_mtok_out=30.0,
        max_budget_usd=20.0,
    ) == pytest.approx(28.0)
    assert budget_overrun_usd(
        usage,
        cost_per_mtok_in=5.0,
        cost_per_mtok_out=30.0,
        max_budget_usd=100.0,
    ) is None


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
    assert event["turns"] == 1
    assert event["duration_s"] > 0


def test_final_rollout_crossing_cannot_escape_on_clean_exit(tmp_path, monkeypatch):
    """The final token update can land immediately before EOF, inside one poll
    slice. Completion must re-check it instead of accepting an over-budget OK."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout = _rollout_path(tmp_path)
    usage = {
        "input_tokens": 5_000_000,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 100_000,
    }
    event = json.dumps({
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": usage, "total_token_usage": usage},
        }
    })
    body = (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        f'echo "session id: {SID}" >&2\n'
        f"printf '%s\\n' '{event}' > '{rollout}'\n"
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


def test_final_stderr_usage_crossing_cannot_escape_without_rollout(
    tmp_path, monkeypatch
):
    """Older codex versions report final usage only on stderr. That fallback
    must receive the same final-crossing check as rollout-backed accounting."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    body = (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        f'echo "session id: {SID}" >&2\n'
        "echo 'Token usage: total=5,100,000 input=5,000,000 "
        "(+ 0 cached) output=100,000' >&2\n"
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
    assert result.num_turns == 1


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


def test_implausible_early_rollout_usage_is_rejected_not_budget_killed(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 5_100_000,
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
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": usage, "total_token_usage": usage},
        }
    }
    _rollout_path(tmp_path).write_text(
        json.dumps(session_meta) + "\n" + json.dumps(token_count) + "\n"
    )
    logs = []
    body = (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo "exec finished" >&2\n'
        "printf '%s' '{\"status\":\"pr_opened\",\"summary\":\"done\","
        "\"needs\":\"\"}' > \"$out\"\n"
    )

    result = asyncio.run(
        _runner(tmp_path, body, log=logs.append).run(
            RuntimePlan(prompt="x", cwd=str(worktree), max_budget_usd=20.0)
        )
    )

    assert result.status == "pr_opened"
    assert result.exit_reason is ExitReason.OK
    assert result.cost_usd == 0.0
    assert any("IMPLAUSIBLE" in line for line in logs)


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


def test_resumed_leg_uses_its_own_usage_and_turn_delta(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, input_tokens=5_000_000, output_tokens=100_000)

    result = asyncio.run(
        _runner(tmp_path, _chatty_body(seconds=1)).run(
            RuntimePlan(prompt="x", max_budget_usd=20.0, resume=SID)
        )
    )

    assert result.exit_reason is ExitReason.OK
    assert result.cost_usd == pytest.approx(0.0)
    assert result.num_turns == 0


def test_a_resumed_leg_subtracts_the_baselines_cached_tokens_too(tmp_path, monkeypatch):
    """The cached counter is cumulative like the others: pricing the raw total
    would re-ledger the previous leg's cache reads, and a cached count larger
    than the leg's own input delta reads as distrusted telemetry (full rate)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(
        tmp_path, input_tokens=1_000_000, cached_input_tokens=800_000, output_tokens=10_000
    )
    grown = json.dumps({
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
    _write_rollout(tmp_path, input_tokens=5_000_000, output_tokens=100_000)
    runner = _runner(
        tmp_path,
        (
            f'echo "session id: {SID}" >&2\n'
            f"touch '{_rollout_path(tmp_path)}'\n"
            "sleep 3\n"
        ),
        idle_timeout=None,
        max_runtime=None,
    )

    started = time.monotonic()
    result = asyncio.run(
        runner.run(RuntimePlan(prompt="x", max_budget_usd=20.0))
    )

    assert time.monotonic() - started < 2
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED


def test_enforcement_switch_off_leaves_the_same_run_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, input_tokens=5_000_000, output_tokens=100_000)

    result = asyncio.run(
        _runner(
            tmp_path, _chatty_body(seconds=1), enforce_budget=False
        ).run(RuntimePlan(prompt="x", max_budget_usd=20.0))
    )

    assert result.exit_reason is ExitReason.OK
    assert read_rollout_turns(SID) == 1
