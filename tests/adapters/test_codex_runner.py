"""CodexRunner — pure argv assembly + envelope parsing, plus run() over a fake
`codex` binary. No real model call: build_codex_argv / parse_codex_envelope are
pure, and run() is exercised against a tiny shell stub that mimics `codex exec`
(writes the -o file, prints a `session id:` header).
"""

import asyncio
import json
import os
import stat
import tempfile

import pytest

from issue_agent.adapters.codex_runner import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_SANDBOX,
    CodexProvider,
    CodexRunner,
    read_rollout_turns,
    read_rollout_usage,
    usage_delta,
    _extract_retry_after_seconds,
    _is_rate_limited,
    _is_transport_drop,
    _session_id_from_lines,
    _strict_schema,
    budget_overrun_usd,
    build_codex_argv,
    codex_budget_not_enforced_warning,
    codex_cost_blindspot_warning,
    codex_cost_usd,
    rates_for_model,
    parse_codex_envelope,
    parse_codex_usage,
)
from issue_agent.app.config import from_env
from issue_agent.app.planners.schemas import AUTHOR_SCHEMA, FIX_SCHEMA, IMPLEMENT_SCHEMA
from issue_agent.adapters.journal import Journal, JournalingRuntime, read_events
from issue_agent.domain.outcome import AgentResult, ExitReason, Outcome, classify_outcome, infra_death
from issue_agent.domain.agent_task import AgentTask
from issue_agent.domain.effort import Effort


# ── build_codex_argv (pure) ─────────────────────────────────────────────────────
def test_argv_basic_shape():
    argv = build_codex_argv(AgentTask(prompt="do it"))
    assert argv[:2] == ["codex", "exec"]
    assert argv[-1] == "do it"  # prompt is last positional
    assert "--sandbox" in argv and DEFAULT_SANDBOX in argv
    assert "--dangerously-bypass-hook-trust" in argv
    # No model configured anywhere → NO -m flag: `-m ""` is not "the default",
    # and with no -m codex uses the provider's own default (in OAuth mode, the
    # subscription's current model).
    assert "-m" not in argv


def test_argv_pins_medium_reasoning_effort_by_default():
    # codex exec's own default is LOW (live session_meta 2026-07-27) — the
    # harness pins medium so an implementer leg doesn't quietly under-think.
    argv = build_codex_argv(AgentTask(prompt="x"))
    assert "model_reasoning_effort=medium" in " ".join(argv)


def test_argv_reasoning_effort_override_and_opt_out():
    assert "model_reasoning_effort=high" in " ".join(
        build_codex_argv(AgentTask(prompt="x"), reasoning_effort="high")
    )
    # "" = leave codex's own default alone — no pin at all.
    assert "model_reasoning_effort" not in " ".join(
        build_codex_argv(AgentTask(prompt="x"), reasoning_effort="")
    )


def test_argv_reasoning_effort_rides_the_resume_path_too():
    # Follow-up legs resume; -c is legal on resume and the pin must not vanish
    # exactly when the hard repair legs run.
    argv = build_codex_argv(AgentTask(prompt="x", resume="0199-abc"))
    assert "model_reasoning_effort=medium" in " ".join(argv)


def test_argv_task_effort_beats_the_process_knob():
    """The escalation ladder decides per LEG (#16); `reasoning_effort` is only
    the default for legs with no opinion of their own."""
    argv = build_codex_argv(
        AgentTask(prompt="x", effort=Effort.HIGH), reasoning_effort="low"
    )
    joined = " ".join(argv)
    assert "model_reasoning_effort=high" in joined
    assert "model_reasoning_effort=low" not in joined


def test_argv_task_effort_applies_on_the_resume_path_too():
    # The repair legs are exactly where escalation fires most; -c is legal on
    # resume, so a raised effort must not evaporate there.
    argv = build_codex_argv(AgentTask(prompt="x", resume="0199-abc", effort=Effort.HIGH))
    assert "model_reasoning_effort=high" in " ".join(argv)


def test_a_task_without_an_effort_leaves_the_process_knob_untouched():
    """Legs the ladder never touches (review, triage, the router) must build the
    same argv they did before the field existed."""
    argv = build_codex_argv(AgentTask(prompt="x"), reasoning_effort="low")
    assert "model_reasoning_effort=low" in " ".join(argv)


def test_argv_provider_overrides_inline():
    prov = CodexProvider(provider_id="acme", base_url="https://gw.example/v1")
    argv = build_codex_argv(AgentTask(prompt="x"), provider=prov)
    joined = " ".join(argv)
    assert 'model_providers.acme.base_url="https://gw.example/v1"' in joined
    assert 'model_providers.acme.wire_api="responses"' in joined
    assert 'model_provider="acme"' in joined
    # never put the secret on the argv — only the env_key NAME (codex convention:
    # OPENAI_API_KEY, which behind a proxy is aliased to the gateway key)
    assert 'env_key="OPENAI_API_KEY"' in joined
    assert "OPENAI_API_KEY=" not in joined


def test_argv_model_override_wins():
    argv = build_codex_argv(AgentTask(prompt="x", model="some-other-model"))
    assert "some-other-model" in argv
    assert DEFAULT_CODEX_MODEL not in argv


def test_argv_cwd_schema_output():
    argv = build_codex_argv(
        AgentTask(prompt="x", cwd="/wt/issue-5"),
        schema_path="/tmp/s.json", output_path="/tmp/o.json",
    )
    assert argv[argv.index("-C") + 1] == "/wt/issue-5"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert argv[argv.index("-o") + 1] == "/tmp/o.json"


def test_argv_resume_comes_first():
    argv = build_codex_argv(AgentTask(prompt="x", resume="sess-123"))
    # `exec resume <id>` must precede the -c/-m flags
    assert argv[1] == "exec" and argv[2] == "resume" and argv[3] == "sess-123"


def test_argv_resume_omits_exec_only_flags():
    # `codex exec resume` REJECTS --sandbox and -C (exit 2). The resume argv must
    # emit NEITHER — this is the bug that broke the gateway-drop retry live.
    argv = build_codex_argv(
        AgentTask(prompt="x", resume="sess-123", cwd="/wt/issue-59"),
        schema_path="/tmp/s.json", output_path="/tmp/o.json",
    )
    assert "--sandbox" not in argv          # forbidden on resume
    assert "-C" not in argv                  # forbidden on resume (cwd set on subprocess)
    # sandbox parity uses the resume-legal bypass flag instead
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    # -o / --output-schema ARE legal on resume and must survive (else no envelope)
    assert argv[argv.index("-o") + 1] == "/tmp/o.json"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert "--dangerously-bypass-hook-trust" in argv  # governor stays on


def test_argv_nonresume_keeps_sandbox_and_cd():
    # The plain-exec path is unchanged: --sandbox + -C present, no resume bypass.
    argv = build_codex_argv(AgentTask(prompt="x", cwd="/wt/issue-5"))
    assert "--sandbox" in argv and DEFAULT_SANDBOX in argv
    assert argv[argv.index("-C") + 1] == "/wt/issue-5"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_argv_prepends_system_prompt_to_prompt():
    argv = build_codex_argv(AgentTask(prompt="the task", append_system_prompt="RULES"))
    assert argv[-1].startswith("RULES")
    assert argv[-1].endswith("the task")


def test_argv_skips_system_prompt_when_inject_disabled():
    # When the contract is delivered via AGENTS.md, the prompt must NOT also be
    # prepended (no duplication).
    argv = build_codex_argv(
        AgentTask(prompt="the task", append_system_prompt="RULES"),
        inject_system_prompt=False,
    )
    assert argv[-1] == "the task"
    assert "RULES" not in argv[-1]


def test_argv_custom_provider_and_bin():
    prov = CodexProvider(provider_id="acme", name="Acme", base_url="https://x/y",
                         env_key="ACME_KEY", wire_api="chat")
    argv = build_codex_argv(AgentTask(prompt="x"), codex_bin="/opt/codex", provider=prov)
    assert argv[0] == "/opt/codex"
    joined = " ".join(argv)
    assert 'model_provider="acme"' in joined and 'env_key="ACME_KEY"' in joined


# ── _strict_schema ───────────────────────────────────────────────────────────────
def test_strict_schema_requires_all_and_closes():
    s = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    out = _strict_schema(s)
    assert set(out["required"]) == {"a", "b"}
    assert out["additionalProperties"] is False
    # original untouched (shallow-copy semantics)
    assert "required" not in s


def test_strict_schema_recurses_nested_objects():
    s = {"type": "object", "properties": {
        "outer": {"type": "object", "properties": {"inner": {"type": "string"}}}}}
    out = _strict_schema(s)
    assert out["properties"]["outer"]["required"] == ["inner"]
    assert out["properties"]["outer"]["additionalProperties"] is False


def test_strict_schema_passthrough_non_object():
    assert _strict_schema({"type": "string"}) == {"type": "string"}


# ── parse_codex_envelope (pure) ──────────────────────────────────────────────────
def test_envelope_success_maps_fields():
    msg = json.dumps({"status": "pr_opened", "summary": "opened #5", "needs": ""})
    r = parse_codex_envelope(exit_code=0, last_message=msg, session_id="s1")
    assert r.is_error is False
    assert r.status == "pr_opened"
    assert r.summary == "opened #5"
    assert r.session_id == "s1"
    assert r.structured == {"status": "pr_opened", "summary": "opened #5", "needs": ""}


def test_envelope_blocked_carries_needs():
    msg = json.dumps({"status": "blocked", "summary": "stuck", "needs": "which endpoint?"})
    r = parse_codex_envelope(exit_code=0, last_message=msg)
    assert r.status == "blocked" and r.needs == "which endpoint?"
    assert r.is_error is False


def test_envelope_status_error_is_error():
    msg = json.dumps({"status": "error", "summary": "boom", "needs": ""})
    r = parse_codex_envelope(exit_code=0, last_message=msg)
    assert r.is_error is True


def test_envelope_nonzero_exit_no_json_is_error():
    r = parse_codex_envelope(exit_code=1, last_message="", stderr="provider 401")
    assert r.is_error is True
    assert "provider 401" in r.summary


def test_envelope_nonzero_exit_freetext_only_is_error():
    r = parse_codex_envelope(exit_code=3, last_message="not json output")
    assert r.is_error is True


def test_envelope_zero_exit_freetext_not_error():
    # codex printed prose but no schema (e.g. no --output-schema) and exited 0.
    r = parse_codex_envelope(exit_code=0, last_message="PONG")
    assert r.is_error is False and r.summary == "PONG"


# ── exit_reason on the codex envelope ────────────────────────────────────────
def test_envelope_exit_reason_separates_agent_error_from_normal_terminals():
    ok = parse_codex_envelope(
        exit_code=0, last_message=json.dumps({"status": "pr_opened"})
    )
    # `blocked` is a NORMAL completion — the agent finished and asked a question.
    blocked = parse_codex_envelope(
        exit_code=0, last_message=json.dumps({"status": "blocked", "needs": "?"})
    )
    err = parse_codex_envelope(exit_code=0, last_message=json.dumps({"status": "error"}))
    no_envelope = parse_codex_envelope(exit_code=1, last_message="", stderr="401")

    assert ok.exit_reason is ExitReason.OK
    assert blocked.exit_reason is ExitReason.OK
    assert err.exit_reason is ExitReason.AGENT_ERROR
    assert no_envelope.exit_reason is ExitReason.AGENT_ERROR
    # None of these excuse a retry — they are all attempts the agent actually made.
    assert not any(
        infra_death(r.exit_reason) for r in (ok, blocked, err, no_envelope)
    )


def test_session_id_from_lines():
    out = "OpenAI Codex v0.142.3\nsession id: 019f0bdd-b4df-7c03\n--------\n"
    assert _session_id_from_lines(out) == "019f0bdd-b4df-7c03"
    assert _session_id_from_lines("no header here") == ""


# ── run() over a fake codex binary ───────────────────────────────────────────────
def _fake_codex(tmp, body: str) -> str:
    """Write an executable stub at <tmp>/codex that runs `body` (bash)."""
    path = os.path.join(tmp, "codex")
    with open(path, "w") as f:
        f.write("#!/usr/bin/env bash\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def test_run_parses_output_file_and_session_stdout_fallback():
    # Session id on STDOUT exercises the FALLBACK branch only — real `codex exec`
    # emits it on stderr (covered by test_run_reads_session_id_from_stderr, the
    # primary path). Kept distinct so neither branch silently masks the other.
    tmp = tempfile.mkdtemp()
    # The stub: find the -o path in argv, write an envelope there, print a header.
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "session id: sess-xyz"
printf '%s' '{"status":"pr_opened","summary":"done","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "test-key"})
    r = asyncio.run(runner.run(AgentTask(prompt="resolve", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.is_error is False
    assert r.status == "pr_opened"
    assert r.session_id == "sess-xyz"


def test_run_nonzero_exit_no_output_is_error():
    tmp = tempfile.mkdtemp()
    body = 'echo "boom" >&2\nexit 1\n'
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "test-key"})
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error is True


def test_run_reads_session_id_from_stderr():
    # Real `codex exec` prints `session id:` to STDERR, not stdout.
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "session id: sess-from-stderr" >&2
printf '%s' '{"status":"done","summary":"ok","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "test-key"})
    r = asyncio.run(runner.run(AgentTask(prompt="x", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.session_id == "sess-from-stderr"
    assert r.status == "done"


# ── rate-limit (HTTP 429) classification + window-aligned backoff ─────────────
def test_is_rate_limited_matches_provider_429_text():
    # A representative Azure OpenAI gateway 429 body.
    azure = ('azure-openai error: Your requests to gpt-5.5 for '
             'your-deployment in eastus2 have exceeded rate limit.')
    assert _is_rate_limited(azure) is True
    assert _is_rate_limited('"code":"rate_limit_exceeded"') is True
    assert _is_rate_limited("HTTP 429 Too Many Requests") is True
    assert _is_rate_limited("quota exceeded for this deployment") is True
    # A plain network drop is NOT a rate limit (routes to the short backoff).
    assert _is_rate_limited("stream disconnected before completion") is False
    assert _is_rate_limited("response.failed event received") is False


def test_rate_limit_is_a_transport_drop_but_network_drop_is_not_rate_limited():
    err = AgentResult(is_error=True)
    # 429 counts as a retryable transport drop…
    assert _is_transport_drop(1, "azure error: exceeded rate limit", err) is True
    # …but a captured success envelope is never retried, even with 429 text.
    ok = AgentResult(is_error=False, status="pr_opened")
    assert _is_transport_drop(0, "exceeded rate limit", ok) is False


def test_extract_retry_after_seconds_parses_common_shapes():
    assert _extract_retry_after_seconds("retry-after: 23") == 23.0
    assert _extract_retry_after_seconds("please retry in 12.5s") == 12.5
    assert _extract_retry_after_seconds("x-ratelimit-reset-tokens: 19") == 19.0
    assert _extract_retry_after_seconds("retry-after: 2000ms") == 2.0
    # No hint → None (caller uses the window-aligned default).
    assert _extract_retry_after_seconds("exceeded rate limit") is None
    # Largest hint wins (the furthest-out reset is the safe wait).
    assert _extract_retry_after_seconds("reset-requests: 0 reset-tokens: 47") == 47.0


def test_run_retries_after_rate_limit_then_succeeds():
    """A 429 on the first attempt must NOT be given up on: the runner waits (short,
    in-test) and retries, then succeeds. Regression for the 345 rebase, where the
    2s network backoff retried inside Azure's 60s window and burned all attempts.
    A stub counts invocations via an appended marker file; attempt 1 emits the
    provider 429 text + exits nonzero, attempt 2 writes a real envelope."""
    tmp = tempfile.mkdtemp()
    counter = os.path.join(tmp, "n")
    # Bash stub: increment counter; first run -> 429 to stderr + exit 1; else ok.
    body = (
        'n=$(cat "' + counter + '" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "' + counter + '"\n'
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo "session id: sess-rl" >&2\n'
        'if [ "$n" -eq 1 ]; then\n'
        '  echo "azure-openai error: have exceeded rate limit. retry-after: 0" >&2\n'
        '  exit 1\n'
        'fi\n'
        'printf \'%s\' \'{"status":"pr_opened","summary":"done","needs":""}\' > "$out"\n'
        'exit 0\n'
    )
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        rate_limit_backoff_base=0.05, max_rate_limit_wait=0.1, max_rate_limit_retries=3,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="rebase", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.is_error is False, f"rate-limited run was not retried to success: {r.summary}"
    assert r.status == "pr_opened"
    with open(counter) as f:
        assert int(f.read().strip()) == 2, "expected exactly one retry after the 429"


def test_run_gives_up_after_max_rate_limit_retries():
    """A sustained 429 (every attempt) is bounded by max_rate_limit_retries and
    surfaces a retryable timeout — the daemon re-dispatches cold rather than looping
    forever. Uses a tiny backoff so the test is fast."""
    tmp = tempfile.mkdtemp()
    body = (
        'echo "session id: sess-rl2" >&2\n'
        'echo "azure-openai error: exceeded rate limit" >&2\n'
        'exit 1\n'
    )
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        rate_limit_backoff_base=0.02, max_rate_limit_wait=0.05, max_rate_limit_retries=2,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error is True
    assert r.status == "timeout"
    assert "rate limit" in r.summary.lower()
    # Upstream never gave the agent a turn → infra death, so the daemon must not
    # burn a breaker slot on it.
    assert r.exit_reason is ExitReason.TRANSPORT_DEATH


def test_run_exhausted_transport_retries_is_infra_death():
    """Every attempt died on the wire before a turn completed. The agent made no
    attempt, so the CI/review breakers must not advance (FAILURE-MODES #58/#65)."""
    tmp = tempfile.mkdtemp()
    body = (
        'echo "session id: sess-drop" >&2\n'
        'echo "stream disconnected before completion" >&2\n'
        'exit 1\n'
    )
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        transport_retries=1, backoff_base=0.01,
    )

    r = asyncio.run(runner.run(AgentTask(prompt="x")))

    assert r.is_error is True and r.status == "timeout"
    assert r.exit_reason is ExitReason.TRANSPORT_DEATH
    assert infra_death(r.exit_reason) is True


def test_run_process_killed_by_signal_is_infra_death_not_an_attempt():
    """FAILURE-MODES #58: codex died `exit -9` six times on one PR (suspected macOS
    jetsam during the Playwright RSS burst) and each death marched the breaker to
    needs-human-review. A NEGATIVE returncode means killed-by-signal and we didn't
    do it, so it is infra, not a failed attempt."""
    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, "kill -9 $$\n")  # self-SIGKILL → returncode -9
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"})

    r = asyncio.run(runner.run(AgentTask(prompt="x")))

    assert r.is_error is True
    assert r.exit_reason is ExitReason.KILLED_EXTERNALLY
    assert infra_death(r.exit_reason) is True


def test_run_ordinary_nonzero_exit_is_an_agent_error_not_infra_death():
    """The boundary of the exemption: a POSITIVE exit code is codex reporting
    failure after running. That IS an attempt and must still count."""
    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, 'echo "boom" >&2\nexit 1\n')
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"})

    r = asyncio.run(runner.run(AgentTask(prompt="x")))

    assert r.is_error is True
    assert r.exit_reason is ExitReason.AGENT_ERROR
    assert infra_death(r.exit_reason) is False


# ── total-runtime (wall) cap ─────────────────────────────────────────────────
def test_wall_cap_kills_a_run_the_idle_watchdog_never_catches():
    """A codex run that keeps printing tool traces resets the idle window forever
    (FAILURE-MODES #60). Here stderr flows steadily while the injected WALL clock
    jumps past the cap: only elapsed-time accounting can stop it, and it must use
    the same killpg teardown (the process group, not just the pid)."""
    import time as _time

    tmp = tempfile.mkdtemp()
    # One stderr line straight away (so the idle watchdog is satisfied), then a long
    # sleep the cap must cut short.
    codex = _fake_codex(tmp, 'echo "codex tool: step 1" >&2\nsleep 30\n')
    # started_at, between-lines check, last_line, then the post-line check jumps.
    ticks = iter([1000.0, 1000.0, 1000.0, 4600.0])
    held = {"t": 1000.0}

    def wall():
        try:
            held["t"] = next(ticks)
        except StopIteration:
            pass
        return held["t"]

    logged: list[str] = []
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        idle_timeout=900.0, max_runtime=900.0, poll_slice=0.1,
        wall_clock=wall, log=logged.append,
    )

    start = _time.monotonic()
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    elapsed = _time.monotonic() - start

    assert elapsed < 20, f"wall cap did not trip promptly (took {elapsed:.1f}s)"
    assert r.is_error is True and r.status == "timeout"
    assert r.exit_reason is ExitReason.WALL_TIMEOUT
    assert any("WALL" in m for m in logged)
    assert not any("IDLE" in m for m in logged)  # the idle watchdog never saw it


def test_wall_cap_alone_still_bounds_a_run_when_idle_timeout_is_disabled():
    """With only the wall cap armed the pump must NOT fall through to the unbounded
    readline branch, or the cap would silently never fire."""
    import time as _time

    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, 'echo "codex tool: step 1" >&2\nsleep 30\n')
    ticks = iter([1000.0, 1000.0, 1000.0, 4600.0])
    held = {"t": 1000.0}

    def wall():
        try:
            held["t"] = next(ticks)
        except StopIteration:
            pass
        return held["t"]

    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        idle_timeout=None, max_runtime=900.0, poll_slice=0.1, wall_clock=wall,
    )

    start = _time.monotonic()
    r = asyncio.run(runner.run(AgentTask(prompt="x")))

    assert _time.monotonic() - start < 20
    assert r.exit_reason is ExitReason.WALL_TIMEOUT


def test_healthy_run_inside_both_caps_is_untouched():
    """False-positive guard: a normal run with both caps armed returns its envelope
    and carries no timeout classification."""
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "session id: sess-ok" >&2
printf '%s' '{"status":"pr_opened","summary":"done","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        idle_timeout=900.0, max_runtime=2700.0,
    )

    r = asyncio.run(runner.run(AgentTask(prompt="x", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))

    assert r.is_error is False and r.status == "pr_opened"
    assert r.exit_reason is ExitReason.OK


# ── idle watchdog measures wall-clock so it survives laptop sleep ─────────────
def test_idle_watchdog_trips_on_wall_clock_not_monotonic():
    """Codex counterpart to the AgentRunner wall-clock fix. A stub that stays silent
    for longer than a poll slice but shorter than idle_timeout in REAL time must still
    be killed once the injected WALL clock jumps past idle_timeout — simulating the
    laptop sleeping mid-fix (macOS freezes the monotonic clock during suspend, so a
    one-shot monotonic wait would not count the sleep and recovery would stall)."""
    import time as _time

    tmp = tempfile.mkdtemp()
    # Stub prints nothing to stderr and sleeps a few seconds, then exits.
    codex = _fake_codex(tmp, "sleep 3\n")
    # Wall clock jumps +3600s after the first read, while real time stays ~seconds.
    ticks = iter([1000.0, 4600.0, 4600.0, 4600.0, 4600.0])
    held = {"t": 1000.0}

    def wall():
        try:
            held["t"] = next(ticks)
        except StopIteration:
            pass
        return held["t"]

    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        idle_timeout=900.0, poll_slice=0.1, wall_clock=wall,
    )
    start = _time.monotonic()
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    elapsed = _time.monotonic() - start
    # Tripped fast on the wall-clock jump, not after 900s of real waiting.
    assert elapsed < 20, f"did not trip on wall-clock gap (took {elapsed:.1f}s)"
    assert r.is_error is True and r.status == "timeout"


# ── idle watchdog kills the whole process tree (regression: Myapp #310) ─────────
def test_run_idle_timeout_kills_background_child_and_returns():
    """A hung run whose descendant holds the stdout pipe open must NOT wedge the
    runner. The stub spawns a background child that keeps the inherited stdout fd
    open and sleeps ~long, then the stub itself sleeps ~long with NO stderr output.
    With a tiny idle_timeout the watchdog must (a) return promptly as hung, and
    (b) kill the whole process group so the background child dies too — else
    _read_stdout()/proc.wait() would block forever (the #310 daemon wedge)."""
    import time as _time

    tmp = tempfile.mkdtemp()
    marker = os.path.join(tmp, "child-alive")
    # Background child: touch a marker, hold stdout open, sleep long. Parent also
    # sleeps long and prints NOTHING to stderr → idle watchdog fires.
    body = (
        "( while true; do touch '" + marker + "'; sleep 0.2; done ) &\n"
        "sleep 30\n"
    )
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"}, idle_timeout=1.0)

    start = _time.monotonic()
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    elapsed = _time.monotonic() - start

    # Returned promptly (well under the stub's 30s sleep) — not wedged.
    assert elapsed < 20, f"run did not return promptly (took {elapsed:.1f}s)"
    assert r.is_error is True
    assert r.status == "timeout"
    # The background child was killed with the group: its marker stops updating.
    mtime0 = os.path.getmtime(marker) if os.path.exists(marker) else 0
    _time.sleep(1.0)
    mtime1 = os.path.getmtime(marker) if os.path.exists(marker) else 0
    assert mtime0 == mtime1, "background child survived the idle-timeout kill (process group not killed)"


# ── long run must NOT be killed by the post-hang drain cap (regression: a5144bc) ─
def test_run_longer_than_drain_cap_completes_when_stderr_flows():
    """A healthy resolve runs for MINUTES while emitting per-tool-call stderr. The
    stdout-drain cap (post_hang_drain_timeout) must bound only the residual drain
    AFTER stderr EOF — never the live run. Regression for a5144bc, which wrapped the
    joint stderr+stdout read in one outer timeout, SIGKILLing every run past the cap
    (exit -9, status=timeout, no IDLE line). Here the stub runs well past the cap,
    emitting stderr within the idle window throughout; it must succeed, not be killed.
    """
    tmp = tempfile.mkdtemp()
    # Emit a stderr line every 0.1s for ~1.5s (15 lines), then write the envelope and
    # exit 0. Total runtime (~1.5s) exceeds post_hang_drain_timeout (0.3s below), but
    # each stderr gap (0.1s) stays under idle_timeout (5s) → NOT a hang.
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "session id: sess-long" >&2
for i in $(seq 1 15); do echo "codex tool: step $i" >&2; sleep 0.1; done
printf '%s' '{"status":"pr_opened","summary":"done","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        idle_timeout=5.0, post_hang_drain_timeout=0.3,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="resolve", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.is_error is False, f"long healthy run was killed: {r.status} / {r.summary}"
    assert r.status == "pr_opened"
    assert r.session_id == "sess-long"


# ── preflight: helpful, actionable "not set up" logging ───────────────────────
def test_preflight_missing_binary_is_actionable():
    r = CodexRunner(codex_bin="definitely-not-a-real-codex-bin-xyz",
                    env={"OPENAI_API_KEY": "k"})
    reason = r.preflight()
    assert reason and "not found on PATH" in reason and "FIXER_RUNTIME=codex" in reason


def test_preflight_missing_key_names_the_var(monkeypatch):
    """Gateway mode (base_url set) still hard-requires the env key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, "exit 0\n")
    prov = CodexProvider(base_url="https://gw.example/v1")
    r = CodexRunner(codex_bin=codex, provider=prov)  # no env key
    reason = r.preflight()
    assert reason and "OPENAI_API_KEY is unset" in reason


def test_oauth_mode_emits_no_provider_overrides_and_no_model_flag():
    """Blank base_url = ChatGPT-subscription OAuth: codex's built-in provider
    must stay untouched (defining a custom one forces API-key auth), and no
    CODEX_MODEL means no -m (codex picks the subscription's default)."""
    argv = build_codex_argv(AgentTask(prompt="x"), provider=CodexProvider())
    joined = " ".join(argv)
    assert "model_providers" not in joined and "model_provider=" not in joined
    assert "-m" not in argv


def test_oauth_mode_preflight_accepts_codex_login_credentials(monkeypatch):
    """With no API key, the credentials `codex login` stored are sufficient —
    and with neither, the error says to run `codex login`."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, "exit 0\n")
    home = tempfile.mkdtemp()
    monkeypatch.setenv("HOME", home)

    r = CodexRunner(codex_bin=codex)  # default provider = OAuth mode
    reason = r.preflight()
    assert reason and "codex login" in reason

    os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
    with open(os.path.join(home, ".codex", "auth.json"), "w") as fh:
        fh.write("{}")
    assert r.preflight() is None


def test_preflight_ok_when_bin_and_key_present():
    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, "exit 0\n")
    r = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"})
    assert r.preflight() is None


def test_run_returns_blocked_result_when_not_ready(monkeypatch):
    """A misconfigured codex runtime fails LOUD (blocked + reason), not opaque."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    logged: list[str] = []
    r = CodexRunner(codex_bin="nope-not-real-bin", log=logged.append)
    res = asyncio.run(r.run(AgentTask(prompt="x")))
    assert res.is_error and res.status == "blocked"
    assert "not found on PATH" in res.summary
    assert any("NOT READY" in m for m in logged)


def test_debug_dumps_codex_stderr_as_tagged_lines():
    """Live stderr streaming: milestone lines (a TEST_CMD/pytest run) are tagged
    and ALWAYS surface; other lines only under debug (`codex|`). A codex run is as
    observable as the SDK path — the visibility the operator asked for."""
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "exec bash -lc pytest" >&2
echo "tokens used 1234" >&2
printf '%s' '{"status":"done","summary":"ok","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, debug=True)
    asyncio.run(runner.run(AgentTask(prompt="x")))
    joined = "\n".join(logged)
    assert "codex: exec model=" in joined
    # a pytest run is a milestone → tagged TEST_CMD, not the generic verbose tag
    assert "codex TEST_CMD: exec bash -lc pytest" in joined
    assert "codex| tokens used 1234" in joined  # non-milestone line, debug-only
    assert "codex: exit 0" in joined


def test_milestone_lines_surface_without_debug():
    """The key ask: TEST_CMD runs + commits are visible even with debug OFF."""
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "exec bash -lc 'uv run pytest'" >&2
echo "exec git commit -m 'green: impl'" >&2
echo "some chatty verbose line" >&2
printf '%s' '{"status":"done","summary":"ok","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, debug=False)  # debug OFF
    asyncio.run(runner.run(AgentTask(prompt="x")))
    joined = "\n".join(logged)
    assert "codex TEST_CMD:" in joined         # pytest run surfaced
    assert "codex commit:" in joined           # commit surfaced
    assert "some chatty verbose line" not in joined  # noise stays suppressed


# ── idle watchdog: kill a hung codex subprocess without total-runtime cap ──────
def test_codex_idle_timeout_kills_and_returns_timeout():
    """A codex subprocess that emits one line then goes silent past idle_timeout
    is killed and surfaced as a retryable timeout — not an infinite wedge."""
    tmp = tempfile.mkdtemp()
    # print a line to stderr, then sleep far longer than the idle window.
    body = 'echo "exec bash -lc pytest" >&2\nsleep 30\nexit 0\n'
    codex = _fake_codex(tmp, body)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, idle_timeout=0.4)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error and r.status == "timeout"
    assert any("IDLE" in m for m in logged)


def test_codex_no_idle_trip_on_steady_output():
    """Steady stderr output (gaps < idle_timeout) must complete, never trip."""
    tmp = tempfile.mkdtemp()
    body = r'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
for i in 1 2 3; do echo "exec step $i" >&2; sleep 0.05; done
printf '%s' '{"status":"done","summary":"ok","needs":""}' > "$out"
exit 0
'''
    codex = _fake_codex(tmp, body)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"}, idle_timeout=0.5)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error is False and r.status == "done"


# ── _is_transport_drop (pure classifier) ──────────────────────────────────────
def _res(status="error", is_error=True, sid=""):
    return AgentResult(is_error=is_error, status=status, summary="", session_id=sid)


def test_transport_drop_detects_response_failed():
    assert _is_transport_drop(1, "ERROR: stream disconnected before completion: "
                              "response.failed event received", _res()) is True


def test_transport_drop_ignores_clean_exit():
    # exit 0 is never a transport drop, even if stderr mentions the phrase.
    assert _is_transport_drop(0, "response.failed event received", _res()) is False


def test_transport_drop_ignores_real_agent_error():
    # non-zero exit but stderr is a normal test failure, not a stream drop.
    assert _is_transport_drop(1, "2 failed, 10 passed\nTypeError: ...", _res()) is False


def test_transport_drop_never_retries_captured_envelope():
    # A completed turn (blocked/pr_opened) must never be retried, even on nonzero exit.
    assert _is_transport_drop(1, "stream disconnected before completion",
                              _res(status="blocked", is_error=False)) is False
    assert _is_transport_drop(1, "response.failed event received",
                              _res(status="pr_opened", is_error=False)) is False


# ── gateway transport-drop retry loop (run over a fake codex) ─────────────────
def _flaky_codex(tmp, fail_times: int) -> str:
    """A codex stub that emits a gateway stream-drop + exits 1 for the first
    `fail_times` invocations (tracked via a counter file), then succeeds. Also
    prints a session id header so the retry can resume."""
    counter = os.path.join(tmp, "count")
    with open(counter, "w") as f:
        f.write("0")
    body = f'''
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
n=$(cat "{counter}")
echo "session id: sess-$n" >&2
if [ "$n" -lt "{fail_times}" ]; then
  echo $((n+1)) > "{counter}"
  echo "ERROR: stream disconnected before completion: response.failed event received" >&2
  exit 1
fi
printf '%s' '{{"status":"pr_opened","summary":"done","needs":""}}' > "$out"
exit 0
'''
    return _fake_codex(tmp, body)


def test_transport_drop_retries_then_succeeds():
    """Two gateway drops, then success → run() recovers via the retry loop."""
    tmp = tempfile.mkdtemp()
    codex = _flaky_codex(tmp, fail_times=2)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, transport_retries=4, backoff_base=0.01)
    r = asyncio.run(runner.run(AgentTask(prompt="x", schema={
        "type": "object", "properties": {"status": {"type": "string"},
        "summary": {"type": "string"}, "needs": {"type": "string"}}})))
    assert r.is_error is False and r.status == "pr_opened"
    joined = "\n".join(logged)
    assert "gateway stream-drop (attempt 1/5)" in joined
    assert "resuming sess-" in joined  # retried with resume, not cold


def test_transport_drop_exhausts_to_timeout():
    """Every attempt drops → run() gives up as a retryable timeout, not error."""
    tmp = tempfile.mkdtemp()
    codex = _flaky_codex(tmp, fail_times=99)  # never succeeds
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, transport_retries=2, backoff_base=0.01)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error and r.status == "timeout"
    assert "survived 3 attempts" in r.summary  # 1 + 2 retries
    assert any("giving up" in m for m in logged)


def test_real_error_is_not_retried():
    """A non-transport nonzero exit (real failure) must NOT enter the retry loop —
    it returns immediately as the parsed error envelope."""
    tmp = tempfile.mkdtemp()
    body = 'echo "boom: some real error" >&2\nexit 1\n'
    codex = _fake_codex(tmp, body)
    logged: list[str] = []
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         log=logged.append, transport_retries=4, backoff_base=0.01)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error is True
    assert not any("gateway stream-drop" in m for m in logged)  # loop never engaged


def test_transport_retries_zero_disables_loop():
    """transport_retries=0 → a single drop returns timeout immediately (no retry)."""
    tmp = tempfile.mkdtemp()
    codex = _flaky_codex(tmp, fail_times=99)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         transport_retries=0, backoff_base=0.01)
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.is_error and r.status == "timeout"
    assert "survived 1 attempts" in r.summary


# ══ review M1 / plan D12 — codex token accounting ═══════════════════════════
# Without a price table `cost_usd` was hardcoded 0.0, the ledger write is gated
# on `cost > 0`, and BOTH spend caps were blind to every codex run.

# Real `codex exec` usage lines (the two shapes across codex-cli versions).
_MODERN_USAGE_LINE = (
    "[2026-06-28T12:00:04] Token usage: total=52,341 input=48,000 "
    "(+ 12,000 cached) output=4,341"
)
_LEGACY_USAGE_LINE = "[2026-06-28T12:00:04] tokens used: 52341"


def test_usage_read_from_the_modern_split_token_line():
    # review M1: tokens are the ONLY cost substrate codex exposes.
    u = parse_codex_usage(f"exec bash -lc pytest\n{_MODERN_USAGE_LINE}\n")
    assert u == {
        "input_tokens": 48000,
        "output_tokens": 4341,
        "cache_read_input_tokens": 12000,
        "cache_creation_input_tokens": 0,
    }


def test_usage_read_from_the_legacy_total_only_line():
    # Older exec builds print only a total — no input/output split exists to invent.
    u = parse_codex_usage(_LEGACY_USAGE_LINE)
    assert u["total_tokens"] == 52341
    assert u["input_tokens"] == 0 and u["output_tokens"] == 0


def test_usage_is_none_when_codex_printed_no_token_line():
    # Anti-vacuity: 'no usage reported' stays distinct from 'zero tokens', so the
    # journal never records a fabricated $0 receipt as a measured one.
    assert parse_codex_usage("exec bash -lc pytest\n2 passed\n") is None
    assert parse_codex_usage("") is None


def test_usage_takes_the_last_line_because_codex_totals_are_cumulative():
    stderr = "Token usage: input=10 output=5\nmore work\nToken usage: input=90 output=40"
    assert parse_codex_usage(stderr)["input_tokens"] == 90


# codex-cli 0.145.x split the total-only report across two lines (observed live
# 2026-07-27 — every run journaled usage=None/cost $0 and the caps went blind):
#   "tokens used\n1,820"
_TWO_LINE_USAGE = "codex\nok\ntokens used\n1,820\n"


def test_usage_read_from_the_two_line_total_shape():
    u = parse_codex_usage(_TWO_LINE_USAGE)
    assert u["total_tokens"] == 1820
    assert u["input_tokens"] == 0 and u["output_tokens"] == 0


def test_two_line_shape_requires_adjacency_and_a_bare_count():
    # A stray number further from the header must NOT be stitched into a usage
    # report — only the immediately-following digits-only line pairs.
    assert parse_codex_usage("tokens used\nexit code 0\n1,820\n") is None
    # A count line with prose around the digits is not a count.
    assert parse_codex_usage("tokens used\nabout 1,820 or so\n") is None


def test_read_rollout_usage_prefers_last_cumulative_token_count(tmp_path):
    sid = "019fa244-56c7-7de0-8e18-e56f423bbbcc"
    day = tmp_path / "sessions" / "2026" / "07" / "27"
    day.mkdir(parents=True)
    lines = [
        {"type": "session_meta", "payload": {"type": "session_meta"}},
        {"payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 100, "cached_input_tokens": 50,
            "cache_write_input_tokens": 0, "output_tokens": 10,
            "total_tokens": 110}}}},
        {"payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 54019, "cached_input_tokens": 35328,
            "cache_write_input_tokens": 7, "output_tokens": 604,
            "total_tokens": 54623}}}},
    ]
    (day / f"rollout-2026-07-27T16-30-15-{sid}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n"
    )
    u = read_rollout_usage(sid, codex_home=str(tmp_path))
    assert u == {
        "input_tokens": 54019,
        "output_tokens": 604,
        "cache_read_input_tokens": 35328,
        "cache_creation_input_tokens": 7,
    }


def test_read_rollout_usage_is_none_when_file_or_session_missing(tmp_path):
    assert read_rollout_usage("", codex_home=str(tmp_path)) is None
    assert read_rollout_usage("no-such-session", codex_home=str(tmp_path)) is None
    # Corrupt rollout (unparseable lines, no token_count) reads as None, not a raise.
    day = tmp_path / "sessions" / "2026" / "07" / "27"
    day.mkdir(parents=True)
    (day / "rollout-2026-07-27T00-00-00-badf00d.jsonl").write_text(
        '{"payload": {"type": "token_count"\nnot json at all\n'
    )
    assert read_rollout_usage("badf00d", codex_home=str(tmp_path)) is None


def test_envelope_prefers_caller_supplied_rollout_usage_over_stderr():
    # The rollout split prices a warm cached session at the input rate; the
    # stderr total-only shape would price the same tokens at max(rate) and
    # spuriously trip the issue cap.
    r = parse_codex_envelope(
        exit_code=0,
        last_message='{"status": "pr_opened", "summary": "done"}',
        stderr=_TWO_LINE_USAGE,
        cost_per_mtok_in=5.0,
        cost_per_mtok_out=30.0,
        usage={
            "input_tokens": 1_000_000, "output_tokens": 10_000,
            "cache_read_input_tokens": 900_000, "cache_creation_input_tokens": 0,
        },
    )
    assert r.usage["input_tokens"] == 1_000_000
    assert r.cost_usd == (1_000_000 * 5.0 + 10_000 * 30.0) / 1_000_000.0


def test_usage_delta_subtracts_the_already_ledgered_baseline():
    # A follow-up leg resumes the resolve's session; the rollout counter is
    # cumulative, so without the delta the leg re-ledgers the resolve's tokens
    # (observed live: a 114s fix-ci ledgered $39.57 carrying 6M resolve tokens).
    end = {"input_tokens": 7_809_575, "output_tokens": 17_467,
           "cache_read_input_tokens": 7_432_192, "cache_creation_input_tokens": 0}
    start = {"input_tokens": 6_069_388, "output_tokens": 15_541,
             "cache_read_input_tokens": 5_837_056, "cache_creation_input_tokens": 0}
    d = usage_delta(end, start)
    assert d["input_tokens"] == 1_740_187
    assert d["output_tokens"] == 1_926
    assert d["cache_read_input_tokens"] == 1_595_136


def test_usage_delta_edges():
    # No end usage → None (no fabricated receipt). No baseline → end unchanged.
    assert usage_delta(None, {"input_tokens": 5}) is None
    end = {"input_tokens": 10, "output_tokens": 2}
    assert usage_delta(end, None) == end
    # A counter that shrank (rollout replaced/rotated) floors at zero rather
    # than ledgering negative spend.
    assert usage_delta({"input_tokens": 3}, {"input_tokens": 9})["input_tokens"] == 0


def test_envelope_falls_back_to_stderr_usage_when_no_rollout():
    r = parse_codex_envelope(
        exit_code=0,
        last_message='{"status": "pr_opened", "summary": "done"}',
        stderr=_TWO_LINE_USAGE,
        cost_per_mtok_in=5.0,
        cost_per_mtok_out=30.0,
    )
    assert r.usage["total_tokens"] == 1820
    # Total-only prices at the HIGHER rate — over-estimation is the safe direction.
    assert r.cost_usd == 1820 * 30.0 / 1_000_000.0


def test_cost_prices_input_and_output_at_their_own_rates():
    usage = {"input_tokens": 48000, "output_tokens": 4341, "cache_read_input_tokens": 12000}
    cost = codex_cost_usd(usage, cost_per_mtok_in=1.25, cost_per_mtok_out=10.0)
    # (48000 * 1.25 + 4341 * 10) / 1e6 — cached reads are already inside input.
    assert cost == pytest.approx(0.10341)


def test_cost_is_zero_without_rates():
    # Anti-vacuity twin: the same usage, no price table → nothing is invented.
    usage = {"input_tokens": 48000, "output_tokens": 4341}
    assert codex_cost_usd(usage, cost_per_mtok_in=0.0, cost_per_mtok_out=0.0) == 0.0
    assert codex_cost_usd(None, cost_per_mtok_in=1.25, cost_per_mtok_out=10.0) == 0.0


def test_total_only_usage_is_priced_at_the_higher_rate():
    # A cap that under-counts is a cap that never fires, so an unsplit total
    # rounds toward over-estimation.
    cost = codex_cost_usd(
        {"total_tokens": 1_000_000}, cost_per_mtok_in=1.25, cost_per_mtok_out=10.0
    )
    assert cost == pytest.approx(10.0)


def test_envelope_carries_cost_and_usage_when_rates_are_set():
    msg = json.dumps({"status": "pr_opened", "summary": "opened #5"})
    r = parse_codex_envelope(
        exit_code=0, last_message=msg, stderr=_MODERN_USAGE_LINE,
        cost_per_mtok_in=1.25, cost_per_mtok_out=10.0,
    )
    assert r.cost_usd == pytest.approx(0.10341)
    assert r.usage == {
        "input_tokens": 48000, "output_tokens": 4341,
        "cache_read_input_tokens": 12000, "cache_creation_input_tokens": 0,
    }


def test_envelope_cost_stays_zero_when_rates_are_unset():
    # Anti-vacuity twin of the above: identical envelope + usage line, no rates.
    msg = json.dumps({"status": "pr_opened", "summary": "opened #5"})
    r = parse_codex_envelope(exit_code=0, last_message=msg, stderr=_MODERN_USAGE_LINE)
    assert r.cost_usd == 0.0
    assert r.usage is not None  # tokens still captured for the journal


def test_a_failed_codex_run_still_reports_the_tokens_it_burned():
    # A run that died mid-way still spent money; charging it 0 is exactly the
    # blind spot M1 describes, one attempt smaller.
    r = parse_codex_envelope(
        exit_code=1, last_message="", stderr=f"boom\n{_MODERN_USAGE_LINE}",
        cost_per_mtok_in=1.25, cost_per_mtok_out=10.0,
    )
    assert r.is_error is True
    assert r.cost_usd == pytest.approx(0.10341)


def _costed_codex(tmp) -> str:
    """A stub that writes an envelope AND prints a codex token-usage line."""
    body = (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo "session id: sess-cost" >&2\n'
        'echo "' + _MODERN_USAGE_LINE + '" >&2\n'
        'printf \'%s\' \'{"status":"pr_opened","summary":"done","needs":""}\' > "$out"\n'
        'exit 0\n'
    )
    return _fake_codex(tmp, body)


def test_run_flows_codex_cost_into_the_result():
    """End to end: rates configured → the AgentResult the dispatcher journals
    carries a non-zero cost, which is what lets the ledger (cost > 0) and both
    spend caps see codex at all."""
    tmp = tempfile.mkdtemp()
    runner = CodexRunner(
        codex_bin=_costed_codex(tmp), env={"OPENAI_API_KEY": "k"},
        cost_per_mtok_in=1.25, cost_per_mtok_out=10.0,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.status == "pr_opened"
    assert r.cost_usd == pytest.approx(0.10341)


def test_run_reports_zero_cost_without_rates():
    """Anti-vacuity twin: same stub, no price table → 0.0 (and the startup
    warning below is the operator's only signal)."""
    tmp = tempfile.mkdtemp()
    runner = CodexRunner(codex_bin=_costed_codex(tmp), env={"OPENAI_API_KEY": "k"})
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.status == "pr_opened"
    assert r.cost_usd == 0.0


# ── harness #4: the SHIPPED config must price codex, not just be able to ──────
def test_a_codex_run_on_shipped_defaults_ledgers_a_nonzero_cost():
    """The operator configures nothing; a codex run still arrives at the ledger
    carrying money. Codex bills $0 on a subscription, so these dollars are
    synthetic — but the token volume behind them is real and exhaustible, and
    an un-priced run is one the spend caps can never see."""
    cfg = from_env({"REPO": "x"})
    tmp = tempfile.mkdtemp()
    runner = CodexRunner(
        codex_bin=_costed_codex(tmp), env={"OPENAI_API_KEY": "k"},
        cost_per_mtok_in=cfg.codex_runtime.cost_per_mtok_in,
        cost_per_mtok_out=cfg.codex_runtime.cost_per_mtok_out,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.status == "pr_opened"
    assert r.cost_usd > 0


def test_the_issue_cap_is_reachable_on_codex_only_load():
    """The cap predicate dispatch uses is `sum(ledger) >= cap`. Priced at 0 that
    sum stays 0 forever, so codex-only work could run unbounded under a cap that
    looked armed. On the shipped defaults a bounded number of ordinary runs
    trips it."""
    cfg = from_env({"REPO": "x"})
    per_run = codex_cost_usd(
        {"input_tokens": 48000, "output_tokens": 4341},
        cost_per_mtok_in=cfg.codex_cost_per_mtok_in,
        cost_per_mtok_out=cfg.codex_cost_per_mtok_out,
    )
    spent, runs = 0.0, 0
    while spent < cfg.issue_spend_cap_usd and runs < 10_000:
        spent += per_run
        runs += 1
    assert spent >= cfg.issue_spend_cap_usd
    assert runs < 10_000  # i.e. the loop exited on the cap, not the guard


def test_zeroing_the_rates_makes_the_issue_cap_unreachable_again():
    """Anti-vacuity twin of the above, and the reason zeroing still earns the
    blindspot warning: no price table → no ledger line → no cap, ever."""
    cfg = from_env({
        "REPO": "x", "CODEX_COST_PER_MTOK_IN": "0", "CODEX_COST_PER_MTOK_OUT": "0",
    })
    per_run = codex_cost_usd(
        {"input_tokens": 48000, "output_tokens": 4341},
        cost_per_mtok_in=cfg.codex_cost_per_mtok_in,
        cost_per_mtok_out=cfg.codex_cost_per_mtok_out,
    )
    assert per_run * 10_000 < cfg.issue_spend_cap_usd
    assert codex_cost_blindspot_warning(
        cost_per_mtok_in=cfg.codex_cost_per_mtok_in,
        cost_per_mtok_out=cfg.codex_cost_per_mtok_out,
        issue_spend_cap_usd=cfg.issue_spend_cap_usd,
        daily_spend_cap_usd=cfg.daily_spend_cap_usd,
    ) is not None


# ── D12: the ONE startup warning when caps are armed but pricing is not ───────
def test_blindspot_warning_fires_when_caps_are_armed_without_a_price_table():
    w = codex_cost_blindspot_warning(
        cost_per_mtok_in=0.0, cost_per_mtok_out=0.0,
        issue_spend_cap_usd=25.0, daily_spend_cap_usd=50.0, max_runtime=2700.0,
    )
    assert w is not None
    assert "CODEX_COST_PER_MTOK_IN/OUT" in w
    assert "$0.00" in w and "NEITHER cap can ever fire" in w
    assert "MAX_BUDGET_USD" not in w
    assert "per-session budget" not in w


def test_blindspot_warning_only_reports_the_cost_ledger_blindspot():
    w = codex_cost_blindspot_warning(
        cost_per_mtok_in=0.0, cost_per_mtok_out=0.0,
        issue_spend_cap_usd=25.0, daily_spend_cap_usd=0.0, max_runtime=0.0,
    )
    assert w is not None and "CODEX_MAX_RUNTIME_SECONDS" not in w
    assert "issue=$25.00" in w and "daily=" not in w  # only the armed cap is named


def test_no_blindspot_warning_once_the_rates_are_configured():
    # Anti-vacuity 1: pricing set → caps CAN see codex spend → silence.
    assert codex_cost_blindspot_warning(
        cost_per_mtok_in=1.25, cost_per_mtok_out=10.0,
        issue_spend_cap_usd=25.0, daily_spend_cap_usd=50.0,
    ) is None


def test_no_blindspot_warning_when_no_cap_is_armed():
    # Anti-vacuity 2: no cap → nothing is being misled about.
    assert codex_cost_blindspot_warning(
        cost_per_mtok_in=0.0, cost_per_mtok_out=0.0,
        issue_spend_cap_usd=0.0, daily_spend_cap_usd=0.0,
    ) is None


def test_blindspot_warning_still_fires_on_a_half_configured_price_table():
    # Pricing only the input side still under-counts every run — the same blind
    # spot in weaker form, so it must not buy silence.
    assert codex_cost_blindspot_warning(
        cost_per_mtok_in=1.25, cost_per_mtok_out=0.0,
        issue_spend_cap_usd=25.0, daily_spend_cap_usd=0.0,
    ) is not None


# ── #29: "MAX_BUDGET_USD bounds nothing here" must not hide behind pricing ────
# The clause used to live inside the blindspot warning, which returns early once
# rates are set — so the operators who had configured pricing CORRECTLY were the
# only ones who never saw it. Observed on keifu 2026-07-28: a $28.74 resolve
# against MAX_BUDGET_USD=20, exit_reason=ok, and zero grep matches in the logs.


def test_the_budget_warning_fires_even_when_pricing_is_fully_configured():
    # Pricing configured but enforcement off: the operator has a price table and
    # NO ceiling, which is precisely the regime the original bug hid.
    w = codex_budget_not_enforced_warning(
        fix_budget_usd=20.0, max_runtime=3600.0, enforced=False, priced=True
    )
    assert w is not None
    assert "MAX_BUDGET_USD=$20.00" in w and "NOT ENFORCED" in w
    assert "CODEX_ENFORCE_BUDGET=0" in w
    assert "3600s" in w


def test_the_budget_warning_is_independent_of_the_price_table():
    """The two surfaces answer different questions; configuring one must not
    silence the other."""
    priced = dict(cost_per_mtok_in=1.25, cost_per_mtok_out=10.0,
                  issue_spend_cap_usd=25.0, daily_spend_cap_usd=50.0)
    assert codex_cost_blindspot_warning(**priced) is None
    assert codex_budget_not_enforced_warning(
        fix_budget_usd=20.0, max_runtime=3600.0, enforced=False, priced=True
    ) is not None


def test_the_budget_warning_names_a_zeroed_price_table_as_the_reason():
    """Enforcement armed but unable to fire: every run prices at $0.00, so no
    threshold is ever crossed. It LOOKS enforced, which is why the reason has to
    be named rather than the warning merely appearing."""
    w = codex_budget_not_enforced_warning(
        fix_budget_usd=20.0, max_runtime=3600.0, enforced=True, priced=False
    )
    assert w is not None and "NOT ENFORCED" in w
    assert "cannot measure a reliable dollar ceiling" in w


def test_the_budget_warning_treats_half_pricing_as_unmeasurable():
    """A dollar ceiling is only honest when both token classes are priced.
    Input-only pricing leaves output growth unbounded, and vice versa."""
    w = codex_budget_not_enforced_warning(
        fix_budget_usd=20.0, max_runtime=3600.0,
        enforced=True, priced=False,
    )
    assert w is not None and "NOT ENFORCED" in w


def test_the_budget_warning_goes_silent_once_the_budget_is_really_enforced():
    """The #29 fix: with the check live AND a price table to measure against,
    MAX_BUDGET_USD is a real per-run ceiling. Warning about it then would be a
    misreport in the opposite direction."""
    assert codex_budget_not_enforced_warning(
        fix_budget_usd=20.0, max_runtime=3600.0, enforced=True, priced=True
    ) is None


def test_the_budget_warning_names_the_missing_wall_cap():
    # AGENT_MAX_RUNTIME_SECONDS, not CODEX_: the value handed in is
    # `codex_runtime.max_runtime`, whose env knob is the AGENT_ one. Naming a
    # variable that does not exist is the same class of operator misreport #29
    # is about, so the text was corrected with the rest of it.
    w = codex_budget_not_enforced_warning(
        fix_budget_usd=20.0, max_runtime=0.0, enforced=False, priced=True
    )
    assert w is not None and "AGENT_MAX_RUNTIME_SECONDS is unset" in w


def test_no_budget_warning_when_no_budget_is_configured():
    # Anti-vacuity: with no budget set there is no number being misreported.
    assert codex_budget_not_enforced_warning(
        fix_budget_usd=0.0, max_runtime=3600.0, enforced=False, priced=False
    ) is None


# ── #29 (enforcement half): MAX_BUDGET_USD actually bounds a codex run ────────
# codex has no per-session budget FLAG, so the only possible bound is the
# harness watching the session's streamed counters and killing it. Before this,
# `AGENT_MAX_RUNTIME_SECONDS` was the sole per-run limit and issue #128's
# resolve completed at $28.74 against a configured $20 with exit_reason=ok.


def test_budget_overrun_reports_the_spend_that_crossed_the_line():
    # 5M in @ $5 + 100k out @ $30 = $28.00 — the shape of the live overspend.
    over = budget_overrun_usd(
        {"input_tokens": 5_000_000, "output_tokens": 100_000},
        cost_per_mtok_in=5.0, cost_per_mtok_out=30.0, max_budget_usd=20.0,
    )
    assert over == pytest.approx(28.0)


def test_budget_overrun_is_none_below_the_line():
    assert budget_overrun_usd(
        {"input_tokens": 1_000_000, "output_tokens": 10_000},
        cost_per_mtok_in=5.0, cost_per_mtok_out=30.0, max_budget_usd=20.0,
    ) is None


def test_budget_overrun_is_none_when_the_runtime_is_unpriced():
    """A zeroed price table makes every run read as free. Killing on a dollar
    threshold nobody can measure would be worse than not killing: the failure
    mode stays 'unbounded, and loudly warned about at boot'."""
    assert budget_overrun_usd(
        {"input_tokens": 500_000_000, "output_tokens": 9_000_000},
        cost_per_mtok_in=0.0, cost_per_mtok_out=0.0, max_budget_usd=20.0,
    ) is None


def test_budget_overrun_is_none_without_a_budget_or_usage():
    # No bound configured → nothing to cross. No usage reported yet → nothing
    # measured, so no fabricated verdict either.
    assert budget_overrun_usd(
        {"input_tokens": 5_000_000, "output_tokens": 100_000},
        cost_per_mtok_in=5.0, cost_per_mtok_out=30.0, max_budget_usd=0.0,
    ) is None
    assert budget_overrun_usd(
        None, cost_per_mtok_in=5.0, cost_per_mtok_out=30.0, max_budget_usd=20.0
    ) is None


_OVERSPENT_SID = "019fa244-0000-7de0-8e18-e56f423bbbcc"


def _write_rollout(codex_home, sid, *, input_tokens, output_tokens):
    """A codex session rollout whose cumulative counters price above $20 at the
    shipped rates: 5M in @ $5 + 100k out @ $30 = $28.00."""
    day = codex_home / "sessions" / "2026" / "07" / "28"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-2026-07-28T00-00-00-{sid}.jsonl").write_text(
        json.dumps({"payload": {"type": "token_count", "info": {
            "last_token_usage": {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
            },
            "total_token_usage": {
                "input_tokens": input_tokens, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": output_tokens,
            }}}}) + "\n"
    )


def _spendy_codex(tmp, sid):
    """A stub that announces `sid` and then keeps working (chatty stderr, ~2s).
    Chatty on purpose: a run that keeps printing tool traces never trips the
    IDLE watchdog, which is exactly the runaway the budget check has to stop."""
    body = (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        f'echo "session id: {sid}" >&2\n'
        'for i in $(seq 1 40); do echo "exec step $i" >&2; sleep 0.05; done\n'
        'printf \'%s\' \'{"status":"pr_opened","summary":"done","needs":""}\' > "$out"\n'
        'exit 0\n'
    )
    return _fake_codex(tmp, body)


def _budget_runner(tmp, sid, **kw):
    return CodexRunner(
        codex_bin=_spendy_codex(tmp, sid), env={"OPENAI_API_KEY": "k"},
        cost_per_mtok_in=5.0, cost_per_mtok_out=30.0,
        # Sample promptly so the test observes the kill rather than the stub
        # finishing on its own.
        poll_slice=0.05,
        **kw,
    )


def test_a_run_that_crosses_the_budget_is_killed_with_budget_exhausted(
    tmp_path, monkeypatch
):
    """The headline #29 observable. The same run today finishes and reports
    exit_reason=ok at 44% over budget; it must instead be stopped and report the
    SDK's own budget verdict, so the existing retry/escalation handling (which
    keys off BUDGET_EXHAUSTED, and treats it as a real attempt rather than an
    infra death) needs no codex special case."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, _OVERSPENT_SID, input_tokens=5_000_000,
                   output_tokens=100_000)
    tmp = tempfile.mkdtemp()

    r = asyncio.run(
        _budget_runner(tmp, _OVERSPENT_SID).run(
            AgentTask(prompt="x", max_budget_usd=20.0)
        )
    )

    assert r.exit_reason is ExitReason.BUDGET_EXHAUSTED
    assert r.status == "error_max_budget_usd"
    assert r.envelope_subtype == "error_max_budget_usd"
    assert r.is_error is True
    assert "MAX_BUDGET_USD=$20.00" in r.summary
    # A budget abort is an ATTEMPT, not an infra death — it must march the
    # breakers forward exactly like the SDK path's `error_max_budget_usd`.
    assert infra_death(r.exit_reason) is False


def test_the_killed_run_still_ledgers_what_it_spent(tmp_path, monkeypatch):
    """A kill that reported $0.00 would hide precisely the money it exists to
    bound — the issue/daily caps read the ledger, and the ledger only records a
    leg when cost > 0."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, _OVERSPENT_SID, input_tokens=5_000_000,
                   output_tokens=100_000)
    tmp = tempfile.mkdtemp()

    r = asyncio.run(
        _budget_runner(tmp, _OVERSPENT_SID).run(
            AgentTask(prompt="x", max_budget_usd=20.0)
        )
    )

    assert r.cost_usd == pytest.approx(28.0)
    assert r.usage["input_tokens"] == 5_000_000
    assert r.session_id == _OVERSPENT_SID
    assert r.num_turns == 1
    assert read_rollout_turns(_OVERSPENT_SID) == 1


def test_budget_kill_reaches_the_settled_agent_run_receipt(tmp_path, monkeypatch):
    """The journal is the operator seam: the raw runner result is insufficient
    if the settled receipt drops the measured spend or budget classification."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(
        tmp_path, _OVERSPENT_SID, input_tokens=5_000_000, output_tokens=100_000
    )
    journal = Journal(str(tmp_path / "journal.jsonl"))
    runtime = JournalingRuntime(
        _budget_runner(tempfile.mkdtemp(), _OVERSPENT_SID),
        journal,
        repo="o/r",
        role="fix",
    )
    runtime.set_context("fix", "RESOLVE", 29, 29)

    asyncio.run(runtime.run(AgentTask(prompt="x", max_budget_usd=20.0)))

    event = [e for e in read_events(journal.path) if e["event"] == "agent_run"][0]
    assert event["cost_usd"] == pytest.approx(28.0)
    assert event["usage"]["input_tokens"] == 5_000_000
    assert event["exit_reason"] == "budget_exhausted"
    assert event["envelope_subtype"] == "error_max_budget_usd"
    assert event["turns"] == 1
    assert event["duration_s"] > 0


def test_a_run_under_the_budget_is_left_alone(tmp_path, monkeypatch):
    """Anti-vacuity twin: identical stub and identical spend, a budget above it
    → the run completes normally. Without this the kill test would pass just as
    well against a runner that killed everything."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, _OVERSPENT_SID, input_tokens=5_000_000,
                   output_tokens=100_000)
    tmp = tempfile.mkdtemp()

    r = asyncio.run(
        _budget_runner(tmp, _OVERSPENT_SID).run(
            AgentTask(prompt="x", max_budget_usd=100.0)
        )
    )

    assert r.exit_reason is ExitReason.OK
    assert r.status == "pr_opened"


def test_budget_only_watchdog_samples_during_a_silent_stderr_gap(
    tmp_path, monkeypatch
):
    """With idle and wall caps disabled, budget enforcement must still keep the
    stderr read bounded. Codex announces its session and can then spend during a
    long generation without another trace line."""
    import time as _time

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(
        tmp_path, _OVERSPENT_SID, input_tokens=5_000_000, output_tokens=100_000
    )
    tmp = tempfile.mkdtemp()
    body = (
        f'echo "session id: {_OVERSPENT_SID}" >&2\n'
        "sleep 3\n"
    )
    runner = CodexRunner(
        codex_bin=_fake_codex(tmp, body),
        env={"OPENAI_API_KEY": "k"},
        cost_per_mtok_in=5.0,
        cost_per_mtok_out=30.0,
        idle_timeout=None,
        max_runtime=None,
        poll_slice=0.05,
    )

    start = _time.monotonic()
    result = asyncio.run(runner.run(AgentTask(prompt="x", max_budget_usd=20.0)))

    assert _time.monotonic() - start < 2
    assert result.exit_reason is ExitReason.BUDGET_EXHAUSTED


def test_enforcement_can_be_switched_off(tmp_path, monkeypatch):
    """CODEX_ENFORCE_BUDGET=0 → the old wall-cap-only regime. Same over-budget
    session as the kill test, so this pins the KNOB rather than the threshold."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, _OVERSPENT_SID, input_tokens=5_000_000,
                   output_tokens=100_000)
    tmp = tempfile.mkdtemp()

    r = asyncio.run(
        _budget_runner(tmp, _OVERSPENT_SID, enforce_budget=False).run(
            AgentTask(prompt="x", max_budget_usd=20.0)
        )
    )

    assert r.exit_reason is ExitReason.OK
    assert r.status == "pr_opened"


def test_a_resumed_leg_is_judged_on_its_own_spend_not_the_session_total(
    tmp_path, monkeypatch
):
    """The load-bearing detail. A fix-ci / address-review / rebase leg RESUMES
    the resolve's codex session, whose rollout counter is cumulative and already
    ledgered by the previous leg. Comparing the raw counter against the budget
    would kill every follow-up leg on its predecessor's tokens the first time it
    sampled — so the check runs on the same per-leg delta the ledger uses
    (usage_delta). Here the session has burned $28 against a $20 budget and this
    leg has burned nothing: it must run to completion."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_rollout(tmp_path, _OVERSPENT_SID, input_tokens=5_000_000,
                   output_tokens=100_000)
    tmp = tempfile.mkdtemp()

    r = asyncio.run(
        _budget_runner(tmp, _OVERSPENT_SID).run(
            AgentTask(prompt="x", max_budget_usd=20.0, resume=_OVERSPENT_SID)
        )
    )

    assert r.exit_reason is ExitReason.OK
    assert r.status == "pr_opened"
    # …and the leg is ledgered at its own (zero) delta, not the session's $28.
    assert r.cost_usd == pytest.approx(0.0)


# ── #43: each leg is priced with the model it actually ran ───────────────────
_LUNA_TERRA = (("gpt-5.6-luna", 1.25, 10.0), ("gpt-5.6-terra", 10.0, 60.0))


def test_the_table_prices_a_named_model_instead_of_the_fallback():
    assert rates_for_model(
        "gpt-5.6-terra", _LUNA_TERRA, fallback_in=5.0, fallback_out=30.0
    ) == (10.0, 60.0)


def test_a_model_the_table_does_not_name_falls_back_to_the_scalars():
    assert rates_for_model(
        "gpt-5.6-sol", _LUNA_TERRA, fallback_in=5.0, fallback_out=30.0
    ) == (5.0, 30.0)


def test_an_empty_table_or_an_unknown_model_falls_back():
    """Zero behaviour change for a deployment that never sets the knob."""
    assert rates_for_model("anything", (), fallback_in=5.0, fallback_out=30.0) == (5.0, 30.0)
    assert rates_for_model(None, _LUNA_TERRA, fallback_in=5.0, fallback_out=30.0) == (5.0, 30.0)
    assert rates_for_model("  ", _LUNA_TERRA, fallback_in=5.0, fallback_out=30.0) == (5.0, 30.0)


def test_model_lookup_is_case_insensitive():
    assert rates_for_model(
        "GPT-5.6-Terra", _LUNA_TERRA, fallback_in=5.0, fallback_out=30.0
    ) == (10.0, 60.0)


def test_two_routed_tiers_ledger_different_dollars_for_identical_tokens():
    """The whole point of #43: routing moves PRICE, and with one flat rate a
    cheaper tier could only ever show up as fewer tokens. Same stub, same token
    counts, two models → two costs."""
    tmp = tempfile.mkdtemp()
    codex = _costed_codex(tmp)

    def _cost(model):
        runner = CodexRunner(
            codex_bin=codex, env={"OPENAI_API_KEY": "k"},
            cost_per_mtok_in=5.0, cost_per_mtok_out=30.0,
            cost_per_mtok=_LUNA_TERRA,
        )
        return asyncio.run(runner.run(AgentTask(prompt="x", model=model))).cost_usd

    cheap, dear = _cost("gpt-5.6-luna"), _cost("gpt-5.6-terra")
    assert cheap == pytest.approx(0.10341)   # 48000*1.25 + 4341*10, per MTok
    assert dear == pytest.approx(0.740460)   # 48000*10   + 4341*60
    assert cheap < dear


def test_an_unpinned_leg_is_priced_with_the_runners_default_model():
    """A resolve that pins no model still runs on SOMETHING — the runner's
    default — and must be priced as that, not at the scalar fallback."""
    tmp = tempfile.mkdtemp()
    runner = CodexRunner(
        codex_bin=_costed_codex(tmp), env={"OPENAI_API_KEY": "k"},
        default_model="gpt-5.6-terra",
        cost_per_mtok_in=5.0, cost_per_mtok_out=30.0,
        cost_per_mtok=_LUNA_TERRA,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.cost_usd == pytest.approx(0.740460)


def test_a_model_outside_the_table_still_prices_at_the_scalars():
    """Anti-vacuity twin: the table is an override, not a replacement."""
    tmp = tempfile.mkdtemp()
    runner = CodexRunner(
        codex_bin=_costed_codex(tmp), env={"OPENAI_API_KEY": "k"},
        cost_per_mtok_in=1.25, cost_per_mtok_out=10.0,
        cost_per_mtok=(("gpt-5.6-terra", 10.0, 60.0),),
    )
    r = asyncio.run(runner.run(AgentTask(prompt="x", model="gpt-5.6-luna")))
    assert r.cost_usd == pytest.approx(0.10341)


def test_the_result_reports_the_model_that_actually_ran():
    """What the journal needs to split cost by routed tier — an unpinned task
    carries model=None, so the run has to say what it used."""
    tmp = tempfile.mkdtemp()
    runner = CodexRunner(
        codex_bin=_costed_codex(tmp), env={"OPENAI_API_KEY": "k"},
        default_model="gpt-5.6-terra",
    )
    assert asyncio.run(runner.run(AgentTask(prompt="x"))).model == "gpt-5.6-terra"
    assert asyncio.run(
        runner.run(AgentTask(prompt="x", model="gpt-5.6-luna"))
    ).model == "gpt-5.6-luna"


def test_an_aborted_run_still_reports_its_model():
    """The runs whose cost is ESTIMATED rather than measured are exactly the
    ones a tier split must not lose."""
    tmp = tempfile.mkdtemp()
    codex = _fake_codex(tmp, 'echo "session id: s" >&2\nsleep 30\n')
    runner = CodexRunner(
        codex_bin=codex, env={"OPENAI_API_KEY": "k"},
        default_model="gpt-5.6-terra", idle_timeout=0.5,
        transport_retries=0, post_hang_drain_timeout=0.5,
    )
    r = asyncio.run(runner.run(AgentTask(prompt="x")))
    assert r.status == "timeout"
    assert r.model == "gpt-5.6-terra"


# ══ review M17a — the "real output" guard must use the real status vocabulary ═
# The old guard was a hand-copied tuple ("pr_opened", "blocked", "done") that had
# drifted from every schema: `done` exists in none of them, and `pushed` — the
# status of a COMPLETED FORCE-PUSH — was missing, so that leg could be re-run on
# resume. Pin the vocabulary here so a schema change breaks loudly.
_FIXER_TERMINAL_STATUSES = frozenset(
    {"pr_opened", "pushed", "blocked", "failed", "tests_authored", "contract_change"}
)
# `done` is in no schema; agent/lib.sh and classify_outcome still accept it, so a
# codex run that emits it must still count as real output.
_LEGACY_OK_ALIASES = frozenset({"done"})


def test_fixer_status_vocabulary_is_exactly_the_schemas_union():
    """Drift alarm: if a leg's schema gains or loses a terminal status, this
    fails and whoever changed it must re-check the retry guard below."""
    union = set()
    for schema in (FIX_SCHEMA, AUTHOR_SCHEMA, IMPLEMENT_SCHEMA):
        union |= set(schema["properties"]["status"]["enum"])
    assert union == set(_FIXER_TERMINAL_STATUSES)


@pytest.mark.parametrize("status", sorted(_FIXER_TERMINAL_STATUSES | _LEGACY_OK_ALIASES))
def test_a_captured_envelope_is_never_retried_whatever_its_terminal_status(status):
    """review M17a: every terminal status in the schemas means the turn COMPLETED.
    Retrying over one re-runs paid, side-effecting work (`pushed` = a force-push
    that already landed on the branch)."""
    result = parse_codex_envelope(
        exit_code=1,
        last_message=json.dumps({"status": status, "summary": "s"}),
        stderr="ERROR: stream disconnected before completion",
    )
    assert classify_outcome(result) is not Outcome.ERROR
    assert _is_transport_drop(1, "ERROR: stream disconnected before completion",
                              result) is False


def test_an_error_envelope_with_a_drop_signature_is_still_retried():
    """Anti-vacuity for the guard: it must not swallow everything. An `error`
    envelope classifies as ERROR, so a transport signature still buys a retry."""
    result = parse_codex_envelope(
        exit_code=1, last_message=json.dumps({"status": "error", "summary": "s"}),
        stderr="ERROR: stream disconnected before completion",
    )
    assert _is_transport_drop(1, "ERROR: stream disconnected before completion",
                              result) is True


def _pushed_then_dropped_codex(tmp, counter: str) -> str:
    """A stub whose turn COMPLETES (writes a `pushed` envelope) but whose
    connection then dies on the way out: drop signature + nonzero exit."""
    body = (
        'n=$(cat "' + counter + '" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "' + counter + '"\n'
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo "session id: sess-push" >&2\n'
        'printf \'%s\' \'{"status":"pushed","summary":"force-pushed the rebase","needs":""}\' > "$out"\n'
        'echo "ERROR: stream disconnected before completion" >&2\n'
        'exit 1\n'
    )
    return _fake_codex(tmp, body)


def test_a_completed_force_push_is_not_re_run(tmp_path):
    """review M17a, the concrete harm: `pushed` was missing from the old guard, so
    a completed force-push whose stream died on the way out got RE-RUN on resume —
    duplicate spend on the one branch-mutating leg."""
    tmp = tempfile.mkdtemp()
    counter = str(tmp_path / "n")
    runner = CodexRunner(
        codex_bin=_pushed_then_dropped_codex(tmp, counter),
        env={"OPENAI_API_KEY": "k"}, transport_retries=4, backoff_base=0.01,
    )

    r = asyncio.run(runner.run(AgentTask(prompt="rebase")))

    with open(counter) as f:
        assert int(f.read().strip()) == 1, "the completed push leg was run twice"
    assert r.status == "pushed" and r.is_error is False


def test_an_envelope_less_drop_still_retries(tmp_path):
    """Anti-vacuity twin: the guard only suppresses retries over REAL output. A
    drop that produced no envelope must still get its retry."""
    tmp = tempfile.mkdtemp()
    codex = _flaky_codex(tmp, fail_times=1)
    runner = CodexRunner(codex_bin=codex, env={"OPENAI_API_KEY": "k"},
                         transport_retries=4, backoff_base=0.01)

    r = asyncio.run(runner.run(AgentTask(prompt="x")))

    assert r.is_error is False and r.status == "pr_opened"
    with open(os.path.join(tmp, "count")) as f:
        assert int(f.read().strip()) == 1  # one failure consumed → it retried


# ══ review M17b — classify on the stderr TAIL, not the whole agent trace ══════
# Codex streams the agent's whole tool trace (patches, test output, greps) to
# stderr. A fixer working on ANY throttling code prints provider-pushback wording
# verbatim as it patches — which is the wording the retry classifier matches on.
_POISON_PATCH_LINE = (
    "apply_patch: +    raise HTTPException(429, Too Many Requests)  "
    "# rate limit exceeded"
)


def _trace_with(tail: str, poison: str = _POISON_PATCH_LINE) -> str:
    """A realistic accumulated codex trace: the agent's own output carries the
    poison wording early, then 30 lines of unrelated trace, then `tail`."""
    return "\n".join([poison] + [f"codex| trace line {i}" for i in range(30)] + [tail])


def test_rate_limit_text_in_the_agent_trace_is_not_provider_pushback():
    """review M17b: matching the whole accumulated trace let the AGENT'S OWN TEXT
    buy 15 minutes of 60s retries and then launder a plain agent error into
    TRANSPORT_DEATH, the one exit reason the breakers exempt."""
    trace = _trace_with("Error: 2 tests failed")
    assert _is_rate_limited(trace) is False
    assert _is_transport_drop(1, trace, AgentResult(is_error=True)) is False


def test_a_real_429_in_the_stderr_tail_is_still_detected():
    """Anti-vacuity twin: same trace, but the provider's 429 is where a real one
    lands — the last thing printed before exit."""
    trace = _trace_with("azure-openai error: have exceeded rate limit.")
    assert _is_rate_limited(trace) is True
    assert _is_transport_drop(1, trace, AgentResult(is_error=True)) is True


def test_a_transport_signature_in_the_agent_trace_is_not_a_drop():
    """Same narrowing for the network-drop signatures: the agent quoting
    'stream disconnected before completion' (a log it read, a test it wrote) must
    not buy a retry."""
    trace = _trace_with(
        "Error: 2 tests failed",
        poison='apply_patch: +    assert e.msg == stream disconnected before completion',
    )
    assert _is_transport_drop(1, trace, AgentResult(is_error=True)) is False


def test_retry_after_hint_is_read_from_the_tail_not_the_agent_trace():
    """A number the AGENT printed must never decide how long we sleep (the
    parser takes the LARGEST hint, so one bogus big number in the trace would
    otherwise park the daemon)."""
    trace = _trace_with(
        "azure-openai error: exceeded rate limit. retry-after: 3",
        poison="apply_patch: +    # honour Retry-After: 99999 from the provider",
    )
    assert _extract_retry_after_seconds(trace) == 3.0


def test_run_does_not_retry_when_only_the_agent_trace_mentions_a_rate_limit(tmp_path):
    """The behavioural half of M17b: a genuine agent error whose trace happens to
    contain 'rate limit' must return AGENT_ERROR on the FIRST attempt — no waits,
    and no TRANSPORT_DEATH laundering (which would exempt the breaker)."""
    tmp = tempfile.mkdtemp()
    counter = str(tmp_path / "n")
    body = (
        'n=$(cat "' + counter + '" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "' + counter + '"\n'
        "echo '" + _POISON_PATCH_LINE + "' >&2\n"
        'for i in $(seq 1 30); do echo "codex| trace line $i" >&2; done\n'
        'echo "Error: 2 tests failed" >&2\n'
        'exit 1\n'
    )
    runner = CodexRunner(
        codex_bin=_fake_codex(tmp, body), env={"OPENAI_API_KEY": "k"},
        rate_limit_backoff_base=0.01, max_rate_limit_wait=0.02,
        transport_retries=4, backoff_base=0.01,
    )

    r = asyncio.run(runner.run(AgentTask(prompt="x")))

    with open(counter) as f:
        assert int(f.read().strip()) == 1, "the agent's own text triggered a retry"
    assert r.is_error is True
    assert r.exit_reason is ExitReason.AGENT_ERROR
    assert infra_death(r.exit_reason) is False


def test_run_still_retries_a_real_429_in_the_tail(tmp_path):
    """Anti-vacuity twin of the above, through run(): identical trace, but the
    provider's 429 is the last line → the rate-limit path engages and recovers."""
    tmp = tempfile.mkdtemp()
    counter = str(tmp_path / "n")
    body = (
        'n=$(cat "' + counter + '" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "' + counter + '"\n'
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo "session id: sess-rl3" >&2\n'
        "echo '" + _POISON_PATCH_LINE + "' >&2\n"
        'for i in $(seq 1 30); do echo "codex| trace line $i" >&2; done\n'
        'if [ "$n" -eq 1 ]; then\n'
        '  echo "azure-openai error: have exceeded rate limit. retry-after: 0" >&2\n'
        '  exit 1\n'
        'fi\n'
        'printf \'%s\' \'{"status":"pr_opened","summary":"done","needs":""}\' > "$out"\n'
        'exit 0\n'
    )
    runner = CodexRunner(
        codex_bin=_fake_codex(tmp, body), env={"OPENAI_API_KEY": "k"},
        rate_limit_backoff_base=0.01, max_rate_limit_wait=0.02, max_rate_limit_retries=3,
    )

    r = asyncio.run(runner.run(AgentTask(prompt="x")))

    assert r.is_error is False and r.status == "pr_opened"
    with open(counter) as f:
        assert int(f.read().strip()) == 2


# ══ review M17c — per-run scratch dir is removed; last.json truncated per attempt ═
def test_a_stale_envelope_is_not_attributed_to_the_next_attempt(tmp_path):
    """review M17c: codex writes `-o` only when a turn completes, so a retry that
    produced NOTHING used to re-read the previous attempt's envelope and report
    it as its own result. Attempt 1 leaves an `error` envelope and drops; attempt 2
    writes no envelope and exits 0 — its result must be attempt 2's."""
    tmp = tempfile.mkdtemp()
    counter = str(tmp_path / "n")
    body = (
        'n=$(cat "' + counter + '" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "' + counter + '"\n'
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo "session id: sess-stale" >&2\n'
        'if [ "$n" -eq 1 ]; then\n'
        '  printf \'%s\' \'{"status":"error","summary":"ATTEMPT ONE ENVELOPE"}\' > "$out"\n'
        '  echo "ERROR: stream disconnected before completion" >&2\n'
        '  exit 1\n'
        'fi\n'
        'echo "SECOND ATTEMPT PROSE"\n'
        'exit 0\n'
    )
    runner = CodexRunner(
        codex_bin=_fake_codex(tmp, body), env={"OPENAI_API_KEY": "k"},
        transport_retries=4, backoff_base=0.01,
    )

    r = asyncio.run(runner.run(AgentTask(prompt="x")))

    with open(counter) as f:
        assert int(f.read().strip()) == 2  # it did retry (guard: the test is live)
    assert "ATTEMPT ONE ENVELOPE" not in r.summary
    assert r.is_error is False and "SECOND ATTEMPT PROSE" in r.summary


def test_the_per_run_scratch_dir_is_removed(tmp_path):
    """review M17c: the run dir used to be an un-removed mkdtemp, so a long-lived
    daemon leaked one `codex-run-*` directory per dispatch. The stub records the
    -o path it was given; that whole directory must be gone once run() returns."""
    tmp = tempfile.mkdtemp()
    marker = str(tmp_path / "outpath")
    body = (
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done\n'
        'echo "$out" > "' + marker + '"\n'
        'printf \'%s\' \'{"status":"pr_opened","summary":"done"}\' > "$out"\n'
        'exit 0\n'
    )
    runner = CodexRunner(codex_bin=_fake_codex(tmp, body), env={"OPENAI_API_KEY": "k"})

    r = asyncio.run(runner.run(AgentTask(prompt="x", schema=FIX_SCHEMA)))

    assert r.status == "pr_opened"
    with open(marker) as f:
        out_path = f.read().strip()
    assert out_path, "stub never saw an -o path"
    run_dir = os.path.dirname(out_path)
    assert os.path.basename(run_dir).startswith("codex-run-")
    assert not os.path.exists(run_dir), f"scratch dir leaked: {run_dir}"
