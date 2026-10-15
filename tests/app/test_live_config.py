"""Live knobs (ADR 0007 amendment): re-read each tick from the composed env
file, validated, applied as a new snapshot; anything else needs a restart."""

from __future__ import annotations

import os
import shlex
import subprocess

import pytest

from issue_agent.adapters.reload import ReloadWatch
from issue_agent.app.config import LIVE_ENV_NAMES, from_env, reload_fingerprint
from issue_agent.app.live_config import LiveConfig, read_candidate
from issue_agent.domain.agent_task import RuntimeName
from issue_agent.domain.effort import Effort
from issue_agent.domain.routing import Tier
from issue_agent.domain.world import Pickup

BASE = {
    "REPO": "o/r", "FIX_BOT": "fix-bot", "REVIEW_BOT": "review-bot[bot]", "FIXER_RUNTIME": "claude",
    "REVIEWER_RUNTIME": "claude", "REVIEW_MODEL": "opus", "FIX_MODEL_LOW": "claude:sonnet:low",
    "FIX_DISPATCH_CONCURRENCY": "2", "ISSUE_SPEND_CAP_USD": "25", "DAILY_SPEND_CAP_USD": "100",
}


def _write(path, values: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))


def _live(tmp_path, logs, **boot_overrides):
    env = tmp_path / "active.env"
    _write(env, {**BASE, **boot_overrides})
    boot = from_env({"AGENT_ENV": str(env)})
    assert boot.validation_errors() == ()
    return env, boot, LiveConfig(boot, str(env), role="fix", log=logs.append)


def test_a_live_knob_change_applies_at_the_next_refresh_as_a_new_snapshot(tmp_path):
    logs: list[str] = []
    env, boot, live = _live(tmp_path, logs)
    _write(env, {**BASE, "FIX_MODEL_LOW": "claude:opus:high", "FIX_DISPATCH_CONCURRENCY": "4",
                 "ISSUE_SPEND_CAP_USD": "40", "DAILY_SPEND_CAP_USD": "0", "ISSUE_PICKUP": "open"})

    current = live.refresh()

    assert current is live.current and current is not boot
    route = current.fix_route(Tier.LOW)
    assert (route.runtime, route.model, route.effort) == (RuntimeName.CLAUDE, "opus", Effort.HIGH)
    assert (current.fix_dispatch_concurrency, current.issue_spend_cap_usd, current.daily_spend_cap_usd) == (4, 40.0, 0.0)
    assert current.pickup is Pickup.OPEN
    assert current.config_version != boot.config_version  # a leg's receipt names the effective snapshot
    assert boot.fix_dispatch_concurrency == 2  # the boot snapshot is never mutated
    assert len(logs) == 1 and "FIX_DISPATCH_CONCURRENCY=4" in logs[0]


def test_an_unchanged_file_is_not_read_again(tmp_path):
    reads: list[str] = []
    env = tmp_path / "active.env"
    _write(env, BASE)
    boot = from_env({"AGENT_ENV": str(env)})
    live = LiveConfig(boot, str(env), read=lambda path: reads.append(path) or boot)

    for _ in range(3):
        assert live.refresh() is boot
    assert reads == []


@pytest.mark.parametrize("bad", [
    {"FIX_DISPATCH_CONCURRENCY": "0"},
    {"FIX_DISPATCH_CONCURRENCY": "lots"},
    {"FIX_DISPATCH_CONCURRENCY": "17"},  # past the dispatch pool's bound
    {"ISSUE_SPEND_CAP_USD": "-1"},
    {"DAILY_SPEND_CAP_USD": "nan"},
    {"FIX_MODEL_LOW": "gemini:pro"},
    {"FIX_MODEL_LOW": "claude:opus:sideways"},
    {"ISSUE_PICKUP": "everything"},
    {"FIX_BOT": ""},  # a non-live knob broken in the same file: the whole file is invalid
])
def test_an_invalid_candidate_keeps_the_previous_values_and_logs_once_per_hash(tmp_path, bad):
    logs: list[str] = []
    env, boot, live = _live(tmp_path, logs)
    _write(env, {**BASE, "FIX_MODEL_LOW": "claude:opus", **bad})

    assert live.refresh() is boot
    assert live.refresh() is boot
    assert len(logs) == 1 and "refused" in logs[0]
    assert live.current.fix_route(Tier.LOW).model == "sonnet"


def test_a_non_live_change_is_not_applied_live(tmp_path):
    logs: list[str] = []
    env, boot, live = _live(tmp_path, logs)
    _write(env, {**BASE, "POLL_SECONDS": "5", "BASE_BRANCH": "main", "ISSUE_PICKUP": "open"})

    current = live.refresh()

    assert (current.poll_seconds, current.base_branch) == (boot.poll_seconds, boot.base_branch)
    assert current.pickup is Pickup.OPEN


def test_a_route_onto_a_runtime_the_process_did_not_bind_waits_for_the_restart(tmp_path):
    logs: list[str] = []
    env, boot, live = _live(tmp_path, logs)
    _write(env, {**BASE, "FIX_MODEL_LOW": "codex:gpt-6-luna:low"})

    assert live.refresh() is boot
    assert "did not bind" in logs[0]


def test_an_unreadable_or_vanished_file_keeps_the_running_settings(tmp_path):
    logs: list[str] = []
    env, boot, live = _live(tmp_path, logs)
    env.write_text("FIX_MODEL_LOW='unterminated\n")
    assert live.refresh() is boot
    env.unlink()
    assert live.refresh() is boot


def test_a_file_that_fails_to_source_never_falls_back_to_the_launch_environment(tmp_path, monkeypatch):
    """point-at.sh exports every launch-time knob; a sourcing failure must not
    read them back as the file's values (a lowered cap would silently revert)."""
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "999")
    env = tmp_path / "active.env"
    _write(env, {**BASE, "DAILY_SPEND_CAP_USD": "10"})
    assert read_candidate(str(env)).daily_spend_cap_usd == 10.0
    env.write_text(env.read_text() + "exit 3\n")
    with pytest.raises(ValueError):
        read_candidate(str(env))


def test_a_transient_read_failure_is_retried_without_the_file_changing(tmp_path):
    logs: list[str] = []
    env, boot, _ = _live(tmp_path, logs)
    outcomes = [OSError("bash timed out"), OSError("bash timed out")]

    def flaky(path):
        if outcomes:
            raise outcomes.pop()
        return read_candidate(path)

    live = LiveConfig(boot, str(env), read=flaky, log=logs.append)
    _write(env, {**BASE, "DAILY_SPEND_CAP_USD": "5"})

    assert live.refresh() is boot and live.refresh() is boot
    assert live.refresh().daily_spend_cap_usd == 5.0
    assert sum("unreadable" in line for line in logs) == 1


def test_a_static_config_never_reads(tmp_path):
    boot = from_env(BASE)
    live = LiveConfig(boot, read=lambda _path: pytest.fail("a static config reads no file"))
    assert live.refresh() is boot


# ── the reload watch fingerprints only what needs a restart ──────────────────

def _repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _watch(tmp_path, env, role="fix"):
    return ReloadWatch(str(_repo(tmp_path / "repo")), files=(str(env),),
                       fingerprint=lambda p: reload_fingerprint(p, dict(os.environ), role))


def test_a_live_only_change_does_not_reload(tmp_path):
    env = tmp_path / "active.env"
    _write(env, {**BASE, "HARNESS_DEPLOY_KNOBS": "REPO FIX_BOT"})
    watch = _watch(tmp_path, env)

    _write(env, {**BASE, "FIX_MODEL_LOW": "claude:opus:high", "ISSUE_PICKUP": "open", "FIX_DISPATCH_CONCURRENCY": "3",
                 "HARNESS_DEPLOY_KNOBS": "REPO FIX_BOT ISSUE_PICKUP"})  # a newly set live knob joins the manifest

    assert watch.changed() is False


@pytest.mark.parametrize("change", [
    {"POLL_SECONDS": "5"},
    {"REPO": "o/other"},
    {"FIX_MODEL_LOW": "codex:gpt-6-luna:low"},  # a runtime this role does not bind yet
])
def test_a_non_live_change_reloads(tmp_path, change):
    env = tmp_path / "active.env"
    _write(env, BASE)
    watch = _watch(tmp_path, env)

    _write(env, {**BASE, **change})

    assert watch.changed() is True


def test_removing_a_non_live_knob_reloads_and_reordering_does_not(tmp_path):
    env = tmp_path / "active.env"
    _write(env, {**BASE, "POLL_SECONDS": "30"})
    watch = _watch(tmp_path, env)

    _write(env, dict(reversed(list({**BASE, "POLL_SECONDS": "30"}.items()))))
    assert watch.changed() is False
    _write(env, BASE)
    assert watch.changed() is True


def test_every_live_knob_is_dispatch_only():
    """The exception stays narrow: a knob joins only with an ADR 0007 amendment."""
    assert LIVE_ENV_NAMES == {
        "FIX_MODEL", "REVIEW_MODEL", "FIX_MODEL_LOW", "FIX_MODEL_MED", "FIX_MODEL_HIGH", "FIX_MODEL_XHIGH",
        "TRIAGE_MODEL", "STEWARD_MODEL", "FIX_DISPATCH_CONCURRENCY", "ISSUE_SPEND_CAP_USD", "DAILY_SPEND_CAP_USD",
        "ISSUE_PICKUP",
    }
