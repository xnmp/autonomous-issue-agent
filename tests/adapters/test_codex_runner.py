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
    _extract_retry_after_seconds,
    _is_rate_limited,
    _is_transport_drop,
    _session_id_from_lines,
    _strict_schema,
    build_codex_argv,
    codex_cost_blindspot_warning,
    codex_cost_usd,
    parse_codex_envelope,
    parse_codex_usage,
)
from issue_agent.app.config import from_env
from issue_agent.app.planners.schemas import AUTHOR_SCHEMA, FIX_SCHEMA, IMPLEMENT_SCHEMA
from issue_agent.domain.outcome import AgentResult, ExitReason, Outcome, classify_outcome, infra_death
from issue_agent.domain.agent_task import AgentTask


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
    # D12's second sentence: codex has no per-session budget analogue, so the
    # wall cap is the only bound on one run.
    assert "no per-session budget" in w and "2700s" in w


def test_blindspot_warning_names_the_missing_wall_cap_when_it_is_unset():
    w = codex_cost_blindspot_warning(
        cost_per_mtok_in=0.0, cost_per_mtok_out=0.0,
        issue_spend_cap_usd=25.0, daily_spend_cap_usd=0.0, max_runtime=0.0,
    )
    assert w is not None and "CODEX_MAX_RUNTIME_SECONDS is unset" in w
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
