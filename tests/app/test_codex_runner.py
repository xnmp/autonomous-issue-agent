"""CodexRunner — pure argv assembly + envelope parsing, plus run() over a fake
`codex` binary. No real model call: build_codex_argv / parse_codex_envelope are
pure, and run() is exercised against a tiny shell stub that mimics `codex exec`
(writes the -o file and a rollout `session_meta`).
"""

import re
import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

from issue_agent.adapters.codex_runner import (
    DEFAULT_SANDBOX,
    CodexProvider,
    CodexRunner,
    MalformedRolloutCounters,
    _is_rate_limited,
    _is_transport_drop,
    _strict_schema,
    build_codex_argv,
    parse_codex_envelope,
    read_rollout_usage,
    spend_evidence_path,
    usage_delta,
)
from issue_agent.adapters.journal import JournalingRuntime
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.app.planners.schemas import FIX_SCHEMA
from issue_agent.domain.outcome import (
    AgentResult,
    ExitReason,
    LegClass,
    Outcome,
    classify_leg,
    classify_outcome,
    infra_class_failure,
    infra_death,
    resumable_session,
)
from tests.fakes.legacy_journal import Journal, read_events
from issue_agent.domain.effort import Effort
from issue_agent.domain.pricing import ModelRates, RateTable

# $5 in / $30 out / $0.50 cached per MTok, keyed on the unpinned-leg fallback.
CARD = RateTable("card", "v1", "USD", {"<runtime-default>": ModelRates(5_000_000, 30_000_000, 500_000, 6_250_000)})


# ── build_codex_argv (pure) ─────────────────────────────────────────────────────
def test_oversize_prompt_moves_to_stdin_small_stays_on_argv():
    """Linux kills a spawn whose single argv element exceeds MAX_ARG_STRLEN
    (128 KiB) with E2BIG — observed live on RE_REVIEW keifu #173 (2026-08-02),
    whose diff-bearing prompt died before any paid work and walked the PR to
    needs-human-review. Oversize prompts must ride stdin (`codex exec -`)."""
    from issue_agent.adapters.codex_runner import _PROMPT_ARGV_LIMIT, route_oversize_prompt

    small = build_codex_argv(RuntimePlan(prompt="do it"))
    routed_small, payload_small = route_oversize_prompt(small)
    assert payload_small is None
    assert routed_small == build_codex_argv(RuntimePlan(prompt="do it"))

    big_prompt = "x" * 150_000
    big = build_codex_argv(RuntimePlan(prompt=big_prompt))
    routed, payload = route_oversize_prompt(big)
    assert routed[-1] == "-"
    assert routed[:-1] == big[:-1]
    assert payload == big_prompt.encode()
    # The routed argv is now safely under the per-argument limit everywhere.
    assert all(len(arg.encode()) < 131072 for arg in routed)

    # Boundary: exactly at the limit stays on argv; one byte over routes.
    at_limit = ["codex", "exec", "y" * _PROMPT_ARGV_LIMIT]
    assert route_oversize_prompt(at_limit) == (at_limit, None)
    over = ["codex", "exec", "y" * (_PROMPT_ARGV_LIMIT + 1)]
    assert route_oversize_prompt(over)[0][-1] == "-"

    # The limit is BYTES, not characters: a multibyte prompt under the char
    # count but over the byte count must route (the E2BIG kernel check is
    # byte-based).
    multibyte = ["codex", "exec", "€" * (_PROMPT_ARGV_LIMIT // 3 + 1)]
    routed_mb, payload_mb = route_oversize_prompt(multibyte)
    assert routed_mb[-1] == "-" and payload_mb is not None


def test_argv_shape():
    argv = build_codex_argv(
        RuntimePlan(prompt="do it", cwd="/wt/issue-5"), schema_path="/tmp/s.json", output_path="/tmp/o.json"
    )
    assert argv[:2] == ["codex", "exec"]
    assert argv[-1] == "do it"  # prompt is last positional
    assert "--sandbox" in argv and DEFAULT_SANDBOX in argv
    # A hook supplied by the target checkout must never be auto-trusted.
    assert "--dangerously-bypass-hook-trust" not in argv
    # No model configured anywhere → NO -m flag: `-m ""` is not "the default",
    # and with no -m codex uses the provider's own default (in OAuth mode, the
    # subscription's current model).
    assert "-m" not in argv
    assert argv[argv.index("-C") + 1] == "/wt/issue-5"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert argv[argv.index("-o") + 1] == "/tmp/o.json"


@pytest.mark.parametrize(
    "effort,knob,pinned",
    [
        (None, "high", "high"),
        (None, "", None),  # "" = leave codex's own default alone — no pin at all
        # The escalation ladder decides per LEG (#16); the knob is only the default.
        (Effort.HIGH, "low", "high"),
    ],
)
def test_argv_reasoning_effort(effort, knob, pinned):
    joined = " ".join(build_codex_argv(RuntimePlan(prompt="x", effort=effort), reasoning_effort=knob))
    assert joined.count("model_reasoning_effort") == (1 if pinned else 0)
    assert pinned is None or f"model_reasoning_effort={pinned}" in joined


def test_argv_resume_omits_exec_only_flags():
    # `codex exec resume` REJECTS --sandbox and -C (exit 2). The resume argv must
    # emit NEITHER — this is the bug that broke the gateway-drop retry live.
    argv = build_codex_argv(
        RuntimePlan(prompt="x", resume="sess-123", cwd="/wt/issue-59"),
        schema_path="/tmp/s.json",
        output_path="/tmp/o.json",
    )
    assert "--sandbox" not in argv  # forbidden on resume
    assert "-C" not in argv  # forbidden on resume (cwd set on subprocess)
    # sandbox parity uses the resume-legal bypass flag instead
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    # -o / --output-schema ARE legal on resume and must survive (else no envelope)
    assert argv[argv.index("-o") + 1] == "/tmp/o.json"
    assert argv[argv.index("--output-schema") + 1] == "/tmp/s.json"


def test_argv_prepends_system_prompt_to_prompt():
    argv = build_codex_argv(RuntimePlan(prompt="the task", append_system_prompt="RULES"))
    assert argv[-1].startswith("RULES")
    assert argv[-1].endswith("the task")


def test_argv_provider_block():
    prov = CodexProvider(provider_id="acme", name="Acme", base_url="https://x/y", env_key="ACME_KEY", wire_api="chat")
    argv = build_codex_argv(RuntimePlan(prompt="x"), codex_bin="/opt/codex", provider=prov)
    assert argv[0] == "/opt/codex"
    joined = " ".join(argv)
    assert 'model_provider="acme"' in joined and 'env_key="ACME_KEY"' in joined
    # Blank base_url = ChatGPT-subscription OAuth: codex's built-in provider must
    # stay untouched (defining a custom one forces API-key auth).
    oauth = " ".join(build_codex_argv(RuntimePlan(prompt="x"), provider=CodexProvider()))
    assert "model_providers" not in oauth and "model_provider=" not in oauth


def test_strict_schema_recurses_nested_objects_and_passes_non_objects_through():
    out = _strict_schema(
        {"type": "object", "properties": {"outer": {"type": "object", "properties": {"inner": {"type": "string"}}}}}
    )
    assert out["properties"]["outer"]["required"] == ["inner"]
    assert out["properties"]["outer"]["additionalProperties"] is False
    assert _strict_schema({"type": "string"}) == {"type": "string"}


def test_strict_schema_closes_objects_nested_under_array_combinators():
    schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {"anyOf": [{"type": "object", "properties": {"detail": {"type": "string"}}}]},
            }
        },
    }
    strict = _strict_schema(schema)
    assert strict["additionalProperties"] is False
    assert strict["required"] == ["findings"]
    assert strict["properties"]["findings"]["type"] == ["array", "null"]
    item = strict["properties"]["findings"]["items"]["anyOf"][0]
    assert item["additionalProperties"] is False
    assert item["required"] == ["detail"]


def test_a_previously_optional_property_stays_sayable_as_null():
    """Strict mode forces every property into `required`. If that also made it
    non-nullable, a classifier could no longer express "no finding" — which is
    the DEFAULT verdict for advisory fields."""
    out = _strict_schema(
        {"type": "object", "properties": {"kept": {"type": "string"}, "opt": {"type": "integer"}}, "required": ["kept"]}
    )

    assert set(out["required"]) == {"kept", "opt"}
    assert out["properties"]["kept"]["type"] == "string", "required stays strict"
    assert out["properties"]["opt"]["type"] == ["integer", "null"]


_FAULT = "stream error: unexpected status 400 Bad Request"
_OK, _AGENT, _UNAVAILABLE = ExitReason.OK, ExitReason.AGENT_ERROR, ExitReason.RUNTIME_UNAVAILABLE


def _status(status, **kw):
    return json.dumps({"status": status, **kw})


@pytest.mark.parametrize(
    "exit_code,last_message,stderr,evidence,reason",
    [
        (0, _status("pr_opened"), "", {}, _OK),
        (0, _status("blocked", needs="?"), "", {}, _OK),  # a question is a NORMAL completion
        (0, "PONG", "", {}, _OK),  # prose without a schema, exit 0
        (0, "", _FAULT, {}, _OK),  # the fault branch may only narrow a FAILURE
        (0, _status("error"), "", {}, _AGENT),
        (1, "", "provider 401", {}, _AGENT),
        (1, _status("error", summary="boom"), _FAULT, {}, _AGENT),  # a parsed envelope is never a fault
        (1, "", _FAULT, {}, _UNAVAILABLE),  # a fault with no trace of a session
        # supervised_exec fails a bad exec with a silent os._exit(127): no stream
        # output and no session is not something a model can produce...
        (127, "", "", {}, _UNAVAILABLE),
        # ...but 127 is also a command the AGENT mistyped once anything printed,
        # and only 127 is the exec-failure code.
        (127, "", "bash: cargo: command not found", {}, _AGENT),
        (1, "", "", {}, _AGENT),
        (1, "", "boom", {}, _AGENT),  # the classification is POSITIVE
        (3, "I rewrote the test helper", "unexpected status 400 Bad Request", {}, _AGENT),
        # A leg with a session or tokens made an attempt (codex prices a
        # subscription run at $0, so tokens matter on their own).
        (1, "", _FAULT, {"session_id": "sess-1"}, _AGENT),
        (1, "", _FAULT, {"usage": {"input_tokens": 4096, "output_tokens": 12}}, _AGENT),
        # M17: only the stderr TAIL decides; a diff quoting a 400 cannot exempt a failure.
        (
            1,
            "",
            "\n".join(
                ['apply_patch: +    assert body == "unexpected status 400 Bad Request"']
                + [f"codex| trace line {i}" for i in range(40)]
                + ["Error: 2 tests failed"]
            ),
            {},
            _AGENT,
        ),
        # The signature list holds one entry because agent tools write these.
        (1, "", "bash: cargo: command not found", {}, _AGENT),
        (1, "", "ls: /nope: No such file or directory", {}, _AGENT),
        (1, "", "./tool: cannot execute binary file: Exec format error", {}, _AGENT),
    ],
)
def test_envelope_exit_reason(exit_code, last_message, stderr, evidence, reason):
    r = parse_codex_envelope(exit_code=exit_code, last_message=last_message, stderr=stderr, **evidence)
    assert r.exit_reason is reason
    assert r.is_error is (reason is not _OK)
    # None of these excuse a retry: they are attempts or a runtime that never ran.
    assert not infra_death(r.exit_reason)


def test_envelope_maps_fields():
    msg = {"status": "pr_opened", "summary": "opened #5", "needs": ""}
    r = parse_codex_envelope(exit_code=0, last_message=json.dumps(msg), session_id="s1")
    assert (r.status, r.summary, r.session_id, r.structured) == ("pr_opened", "opened #5", "s1", msg)
    blocked = parse_codex_envelope(exit_code=0, last_message=_status("blocked", summary="stuck", needs="which?"))
    assert (blocked.status, blocked.needs) == ("blocked", "which?")
    assert "provider 401" in parse_codex_envelope(exit_code=1, last_message="", stderr="provider 401").summary
    assert "400 Bad Request" in parse_codex_envelope(exit_code=1, last_message="", stderr=_FAULT).summary


# ── run() over a fake codex binary ───────────────────────────────────────────────
# The envelope a completed stub run writes to the `-o` path.
_DONE = 'printf \'%s\' \'{"status":"done","summary":"ok","needs":""}\' > "$out"\n'
_OPENED = 'printf \'%s\' \'{"status":"pr_opened","summary":"done","needs":""}\' > "$out"\n'
_STATUS_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "summary": {"type": "string"}, "needs": {"type": "string"}},
}


def _fake_codex(tmp, body: str) -> str:
    """Write an executable stub at <tmp>/codex that runs `body` (bash), with
    `$out` bound to the `-o` envelope path from argv."""
    path = os.path.join(str(tmp), "codex")
    with open(path, "w") as f:
        f.write(
            '#!/usr/bin/env bash\nout=""; prev=""; for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
            + body
        )
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


# Shell that records a fresh root `codex exec` session the way codex >= 0.146
# does: a rollout `session_meta` in $CODEX_HOME (there is no stderr header).
def _meta(sid: str) -> str:
    code = (
        "import json,os,sys,datetime;d=os.path.join(os.environ['CODEX_HOME'],'sessions','2026','10','13');"
        "os.makedirs(d,exist_ok=True);open(os.path.join(d,'rollout-now-'+sys.argv[1]+'.jsonl'),'a').write("
        "json.dumps({'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'type':'session_meta',"
        "'payload':{'id':sys.argv[1],'originator':'codex_exec','source':'exec','cwd':os.getcwd()}})+'\\n')"
    )
    return f'{sys.executable} -c "{code}" "{sid}"\n'


@pytest.mark.parametrize(
    "streams", ("", "exec 2>&-", "exec 1>&- 2>&-"), ids=("streaming", "stdout-drain", "process-wait")
)
def test_a_cancelled_run_terminates_its_process_group(tmp_path, streams):
    tmp = str(tmp_path)
    pid_file, ready_file = os.path.join(tmp, "pid"), os.path.join(tmp, "ready")
    body = f'echo $$ > "{pid_file}"\n{streams}\necho ready > "{ready_file}"\n'
    body += 'while true; do echo "working" >&2; sleep 0.05; done\n' if not streams else "sleep 30\n"

    async def scenario():
        runner = CodexRunner(codex_bin=_fake_codex(tmp, body), env={"OPENAI_API_KEY": "test-key"})
        task = asyncio.create_task(runner.run(RuntimePlan(prompt="x")))
        for _ in range(100):
            if os.path.exists(ready_file):
                break
            await asyncio.sleep(0.01)
        assert os.path.exists(ready_file)
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    with pytest.raises(ProcessLookupError):
        os.kill(int(open(pid_file).read()), 0)


def test_native_codex_child_receives_operator_build_settings(tmp_path, monkeypatch):
    observed = tmp_path / "environment.txt"
    monkeypatch.setenv("CARGO_TARGET_DIR", "/operator/shared-cargo-target")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")
    monkeypatch.setenv("CARGO_BUILD_JOBS", "2")
    monkeypatch.setenv("PW_WORKERS", "1")
    monkeypatch.setenv("GH_TOKEN", "publication-secret")
    body = (
        r"""
printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n' "${CARGO_TARGET_DIR-unset}" "${CARGO_INCREMENTAL-unset}" "${CARGO_BUILD_JOBS-unset}" "${PW_WORKERS-unset}" "${GH_TOKEN-unset}" "${GIT_AUTHOR_NAME-unset}" "${GIT_COMMITTER_NAME-unset}" > "$OBSERVED_ENV"
"""
        + _DONE
    )
    result = asyncio.run(
        CodexRunner(
            codex_bin=_fake_codex(tmp_path, body), env={"OPENAI_API_KEY": "test-key", "OBSERVED_ENV": str(observed)}
        ).run(RuntimePlan(prompt="x", model="m", env={"GIT_AUTHOR_NAME": "fix-bot"}))
    )

    assert result.status == "done"
    # The task's identity (fix.py's fix-bot) beats the model's git identity.
    assert observed.read_text().splitlines() == [
        "/operator/shared-cargo-target",
        "0",
        "2",
        "1",
        "unset",
        "fix-bot",
        "Harness - m",
    ]


def test_run_registers_process_group_before_codex_executes(tmp_path):
    state_dir = tmp_path / "state"
    body = (
        r"""
receipt_found=""
for receipt in "$RECEIPT_DIR"/*.json; do
  [ ! -e "$receipt" ] || receipt_found=1
done
[ -n "$receipt_found" ] || exit 9
"""
        + _DONE
    )
    supervisor = ProcessSupervisor(str(state_dir))
    runner = CodexRunner(
        codex_bin=_fake_codex(tmp_path, body),
        process_supervisor=supervisor,
        env={"OPENAI_API_KEY": "test-key", "RECEIPT_DIR": str(state_dir / "process-groups")},
    )

    result = asyncio.run(runner.run(RuntimePlan(prompt="x", schema=_STATUS_SCHEMA)))

    assert result.status == "done"
    assert not any(supervisor.directory.glob("*.json"))


def test_run_drains_a_line_beyond_the_stream_limit_without_losing_the_next_line(tmp_path):
    """Fallback draining preserves the stderr record boundary after a huge line."""
    body = (
        r"""
head -c 5242880 /dev/zero | tr '\0' x >&2
echo >&2
echo "stderr after fallback drain" >&2
"""
        + _DONE
    )
    logged: list[str] = []
    result = asyncio.run(
        CodexRunner(
            codex_bin=_fake_codex(tmp_path, body), env={"OPENAI_API_KEY": "test-key"}, log=logged.append, debug=True
        ).run(RuntimePlan(prompt="x"))
    )

    assert result.status == "done"
    assert any("oversized stderr line" in line and "stream limit" in line for line in logged)
    assert any("stderr after fallback drain" in line for line in logged)


def _write_session_rollout(
    codex_home,
    *,
    sid: str,
    cwd: str,
    timestamp: datetime,
    originator: str = "codex_exec",
    source="exec",
    turns: int = 2,
):
    day = codex_home / "sessions" / "2026" / "07" / "29"
    day.mkdir(parents=True, exist_ok=True)
    usage = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 50_000,
        "cache_write_input_tokens": 0,
        "output_tokens": 100_000,
    }
    meta = {
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "type": "session_meta",
        "payload": {"id": sid, "originator": originator, "cwd": cwd, "source": source},
    }
    count = {
        "timestamp": timestamp.isoformat(),
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}},
    }
    (day / f"rollout-2026-07-29T00-00-00-{sid}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in [meta, *[count] * turns])
    )


def test_headerless_01460_recovers_root_rollout_usage_cost_turns_and_ledger(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    codex_home = tmp_path / "codex-home"
    # CodexHomeRuntime sets CODEX_HOME on the task, not on the daemon.
    # The runner must read the same isolated profile its child writes even when
    # the ambient operator profile points elsewhere.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "wrong-ambient-home"))
    now = datetime.now(timezone.utc)
    good_sid = "019fa000-good-root"
    for sid, cwd, minutes, extra in (
        ("019fa000-before-start", worktree, -1, {}),
        ("019fa000-wrong-cwd", tmp_path / "other-worktree", 5, {}),
        ("019fa000-wrong-origin", worktree, 6, {"originator": "codex_tui"}),
        (good_sid, worktree, 2, {}),
        ("019fa000-newer-subagent", worktree, 3, {"source": {"subagent": "explore_local"}}),
    ):
        _write_session_rollout(codex_home, sid=sid, cwd=str(cwd), timestamp=now + timedelta(minutes=minutes), **extra)
    transcript = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "fixtures", "codex-0.146.0-no-session-header.stderr")
    )
    codex = _fake_codex(tmp_path, f"cat '{transcript}' >&2\n" + _OPENED)
    journal = Journal(str(tmp_path / "journal.jsonl"))
    ledger_path = str(tmp_path / "spend.jsonl")
    runtime = JournalingRuntime(
        CodexRunner(
            codex_bin=codex,
            env={"OPENAI_API_KEY": "test-key"},
            rate_table=CARD,
            spend_evidence_dir=str(tmp_path / "spend-evidence"),
        ),
        journal,
        repo="xnmp/autonomous-issue-agent",
        role="fix",
    )
    runtime.set_context("fix", "RESOLVE", 75, 75)

    result = asyncio.run(
        runtime.run(
            RuntimePlan(
                prompt="resolve",
                cwd=str(worktree),
                env={
                    "ISSUE_AGENT_RUN_ID": "reserved-run",
                    "ISSUE_AGENT_CONFIG_VERSION": "cfg",
                    "CODEX_HOME": str(codex_home),
                },
            )
        )
    )

    assert result.session_id == good_sid
    assert result.usage == {
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cache_read_input_tokens": 50_000,
        "cache_creation_input_tokens": 0,
        "source": "rollout",
    }
    # (950000 * $5 + 50000 * $0.50 + 100000 * $30) / 1e6.
    assert result.cost_usd == pytest.approx(7.775)
    agent_run = [event for event in read_events(journal.path) if event["event"] == "agent_run"][0]
    assert agent_run["session_id"] == good_sid
    # The journal records the same cache-aware leg cost as the result.
    assert agent_run["cost_usd"] == pytest.approx(7.775)
    assert read_events(ledger_path) == [], "legacy spend JSONL is read-only"
    with open(spend_evidence_path(str(tmp_path / "spend-evidence"), "reserved-run")) as f:
        evidence = json.load(f)
    assert evidence["session_id"] == good_sid
    assert evidence["cwd"] == os.path.realpath(worktree)


# ── rate-limit (HTTP 429) classification + window-aligned backoff ─────────────
def _run(tmp, body: str, *, plan: RuntimePlan | None = None, **runner) -> AgentResult:
    """One run of a stub codex; `CODEX_HOME` is the stub's dir so `_meta` rollouts land there."""
    env = {"OPENAI_API_KEY": "k", "CODEX_HOME": str(tmp), **runner.pop("env", {})}
    return asyncio.run(
        CodexRunner(codex_bin=_fake_codex(tmp, body), env=env, **runner).run(plan or RuntimePlan(prompt="x"))
    )


@pytest.mark.parametrize(
    "stderr_line",
    [
        "azure-openai error: have exceeded rate limit. retry-after: 0",
        "ERROR: stream disconnected before completion: response.failed event received",
    ],
)
def test_a_gateway_failure_ends_the_leg_after_one_launch(tmp_path, stderr_line):
    """A leg is one launch: a 429 or a stream drop is a machine end the item's
    backoff retries in a new leg with its own reservation. The in-leg retry
    loop this replaces settled only its last attempt's session."""
    counter = tmp_path / "n"
    r = _run(
        tmp_path,
        f'n=$(cat "{counter}" 2>/dev/null || echo 0); echo $((n+1)) > "{counter}"\n'
        + _meta("sess-drop")
        + f'echo "{stderr_line}" >&2\nexit 1\n',
    )

    assert counter.read_text().strip() == "1"
    assert r.is_error is True
    assert r.exit_reason is ExitReason.TRANSPORT_DEATH and infra_death(r.exit_reason)
    # The provider ended the turn: the session stays resumable for the next leg.
    assert resumable_session(r) == "sess-drop"


def test_a_hung_run_whose_tail_reads_like_a_drop_keeps_its_timeout_class(tmp_path):
    """An idle-tripped run must stay IDLE_TIMEOUT, never become a transport
    death: the wall-cap resume-once rule counts reliably hanging tasks."""
    r = _run(
        tmp_path,
        _meta("sess-hang") + 'echo "stream disconnected before completion" >&2\nsleep 30\n',
        idle_timeout=0.3,
        poll_slice=0.05,
    )

    assert r.exit_reason is ExitReason.IDLE_TIMEOUT


def test_run_process_killed_by_signal_is_infra_death_not_an_attempt(tmp_path):
    """FAILURE-MODES #58: codex died `exit -9` six times on one PR (suspected macOS
    jetsam during the Playwright RSS burst) and each death marched the breaker to
    needs-human-review. A NEGATIVE returncode means killed-by-signal and we didn't
    do it, so it is infra, not a failed attempt."""
    r = _run(tmp_path, "kill -9 $$\n")  # self-SIGKILL → returncode -9

    assert r.is_error is True
    assert r.exit_reason is ExitReason.KILLED_EXTERNALLY
    assert infra_death(r.exit_reason) is True


# ── total-runtime (wall) cap ─────────────────────────────────────────────────
def _jumping_wall_clock(*ticks: float):
    """An injected WALL clock that replays `ticks`, then holds the last one."""
    remaining, held = iter(ticks), [ticks[0]]

    def wall():
        held[0] = next(remaining, held[0])
        return held[0]

    return wall


@pytest.mark.parametrize("idle_timeout", [900.0, None], ids=["idle-armed", "idle-disabled"])
def test_wall_cap_kills_a_run_the_idle_watchdog_never_catches(tmp_path, idle_timeout):
    """A codex run that keeps printing tool traces resets the idle window forever
    (FAILURE-MODES #60). Here stderr flows while the injected WALL clock jumps
    past the cap: only elapsed-time accounting can stop it, with the same killpg
    teardown. With only the wall cap armed the pump must NOT fall through to
    the unbounded readline branch."""
    logged: list[str] = []
    start = time.monotonic()
    # One stderr line straight away (so the idle watchdog is satisfied), then a
    # long sleep the cap must cut short. The clock reads: started_at,
    # between-lines check, last_line, then the post-line check jumps.
    r = _run(
        tmp_path,
        'echo "codex tool: step 1" >&2\nsleep 30\n',
        idle_timeout=idle_timeout,
        max_runtime=900.0,
        poll_slice=0.1,
        wall_clock=_jumping_wall_clock(1000.0, 1000.0, 1000.0, 4600.0),
        log=logged.append,
    )

    assert time.monotonic() - start < 20, "wall cap did not trip promptly"
    assert r.is_error is True and r.status == "timeout"
    assert r.exit_reason is ExitReason.WALL_TIMEOUT
    assert any("WALL" in m for m in logged)
    assert not any("IDLE" in m for m in logged)  # the idle watchdog never saw it


def test_healthy_run_inside_both_caps_is_untouched(tmp_path):
    """False-positive guard: a normal run with both caps armed returns its envelope
    and carries no timeout classification."""
    r = _run(
        tmp_path,
        'echo "session id: sess-ok" >&2\n' + _OPENED,
        idle_timeout=900.0,
        max_runtime=2700.0,
        plan=RuntimePlan(prompt="x", schema=_STATUS_SCHEMA),
    )

    assert r.is_error is False and r.status == "pr_opened"
    assert r.exit_reason is ExitReason.OK


def test_the_per_run_scratch_dir_is_removed(tmp_path):
    """review M17c: the run dir used to be an un-removed mkdtemp, so a long-lived
    daemon leaked one `codex-run-*` directory per dispatch."""
    marker = tmp_path / "outpath"
    r = _run(tmp_path, f'echo "$out" > "{marker}"\n' + _OPENED, plan=RuntimePlan(prompt="x", schema=FIX_SCHEMA))

    assert r.status == "pr_opened"
    run_dir = os.path.dirname(marker.read_text().strip())
    assert os.path.basename(run_dir).startswith("codex-run-")
    assert not os.path.exists(run_dir), f"scratch dir leaked: {run_dir}"


# ── idle watchdog measures wall-clock so it survives laptop sleep ─────────────
def test_idle_watchdog_trips_on_wall_clock_not_monotonic(tmp_path):
    """Codex counterpart to the AgentRunner wall-clock fix. A stub that stays silent
    for longer than a poll slice but shorter than idle_timeout in REAL time must still
    be killed once the injected WALL clock jumps past idle_timeout — simulating the
    laptop sleeping mid-fix (macOS freezes the monotonic clock during suspend, so a
    one-shot monotonic wait would not count the sleep and recovery would stall)."""
    start = time.monotonic()
    # Silent for a few seconds; the wall clock jumps +3600s after its first read.
    r = _run(tmp_path, "sleep 3\n", idle_timeout=900.0, poll_slice=0.1, wall_clock=_jumping_wall_clock(1000.0, 4600.0))

    elapsed = time.monotonic() - start
    # Tripped fast on the wall-clock jump, not after 900s of real waiting.
    assert elapsed < 20, f"did not trip on wall-clock gap (took {elapsed:.1f}s)"
    assert r.is_error is True and r.status == "timeout"


# ── idle watchdog kills the whole process tree (regression: Myapp #310) ─────────
def test_run_idle_timeout_kills_background_child_and_returns(tmp_path):
    """A hung run whose descendant holds the stdout pipe open must NOT wedge the
    runner. The stub spawns a background child that keeps the inherited stdout fd
    open and sleeps ~long, then the stub itself sleeps ~long with NO stderr output.
    With a tiny idle_timeout the watchdog must (a) return promptly as hung, and
    (b) kill the whole process group so the background child dies too — else
    _read_stdout()/proc.wait() would block forever (the #310 daemon wedge)."""
    marker = str(tmp_path / "child-alive")
    start = time.monotonic()
    r = _run(tmp_path, f"( while true; do touch '{marker}'; sleep 0.2; done ) &\nsleep 30\n", idle_timeout=1.0)

    elapsed = time.monotonic() - start
    # Returned promptly (well under the stub's 30s sleep) — not wedged.
    assert elapsed < 20, f"run did not return promptly (took {elapsed:.1f}s)"
    assert r.is_error is True and r.status == "timeout"
    # The background child was killed with the group: its marker stops updating.
    mtime0 = os.path.getmtime(marker) if os.path.exists(marker) else 0
    time.sleep(1.0)
    mtime1 = os.path.getmtime(marker) if os.path.exists(marker) else 0
    assert mtime0 == mtime1, "background child survived the idle-timeout kill (process group not killed)"


# ── long run must NOT be killed by the post-hang drain cap (regression: a5144bc) ─
def test_run_longer_than_drain_cap_completes_when_stderr_flows(tmp_path):
    """A healthy resolve runs for MINUTES while emitting per-tool-call stderr. The
    stdout-drain cap (post_hang_drain_timeout) must bound only the residual drain
    AFTER stderr EOF — never the live run. Regression for a5144bc, which wrapped the
    joint stderr+stdout read in one outer timeout, SIGKILLing every run past the cap
    (exit -9, status=timeout, no IDLE line). Here the stub runs well past the cap,
    emitting stderr within the idle window throughout; it must succeed, not be killed.
    """
    # A stderr line every 0.1s for ~1.5s, then the envelope: the total runtime
    # exceeds post_hang_drain_timeout (0.3s), each gap stays under idle_timeout (5s).
    body = _meta("sess-long") + 'for i in $(seq 1 15); do echo "codex tool: step $i" >&2; sleep 0.1; done\n' + _OPENED
    r = _run(
        tmp_path,
        body,
        idle_timeout=5.0,
        post_hang_drain_timeout=0.3,
        plan=RuntimePlan(prompt="resolve", schema=_STATUS_SCHEMA),
    )

    assert r.is_error is False, f"long healthy run was killed: {r.status} / {r.summary}"
    assert r.status == "pr_opened"
    assert r.session_id == "sess-long"


# ── preflight: helpful, actionable "not set up" logging ───────────────────────
def test_preflight_missing_binary_is_actionable():
    reason = CodexRunner(codex_bin="definitely-not-a-real-codex-bin-xyz", env={"OPENAI_API_KEY": "k"}).preflight()
    assert reason and "not found on PATH" in reason and "CODEX_BIN" in reason


def test_preflight_missing_key_names_the_var(tmp_path, monkeypatch):
    """Gateway mode (base_url set) still hard-requires the env key."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runner = CodexRunner(
        codex_bin=_fake_codex(tmp_path, "exit 0\n"), provider=CodexProvider(base_url="https://gw.example/v1")
    )
    reason = runner.preflight()
    assert reason and "OPENAI_API_KEY is unset" in reason


def test_oauth_mode_preflight_accepts_codex_login_credentials(tmp_path, monkeypatch):
    """With no API key, the credentials `codex login` stored are sufficient —
    and with neither, the error says to run `codex login`."""
    for inherited_auth in ("OPENAI_API_KEY", "CODEX_HOME"):
        monkeypatch.delenv(inherited_auth, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    r = CodexRunner(codex_bin=_fake_codex(tmp_path, "exit 0\n"))  # default provider = OAuth mode
    reason = r.preflight()
    assert reason and "codex login" in reason

    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}")
    assert r.preflight() is None
    assert r.oauth_auth_file() == str(home / ".codex" / "auth.json")


def test_oauth_auth_file_honors_explicit_codex_home(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auth = tmp_path / "codex-home" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(auth.parent))

    runner = CodexRunner(codex_bin=_fake_codex(tmp_path, "exit 0\n"))

    assert runner.preflight() is None
    assert runner.oauth_auth_file() == str(auth)


def test_run_returns_blocked_result_when_not_ready(monkeypatch):
    """A misconfigured codex runtime fails LOUD (blocked + reason), not opaque.

    is_error stays True — a non-error result reads as a completed leg to the
    review gate and could synthesize APPROVE on an unreviewed PR — but the
    result is INFRA-class via RUNTIME_UNAVAILABLE, so a spawn precondition
    bills the infra ceiling instead of burning three resolve retries into a
    reasonless escalation (tauri #650, harness #188)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    logged: list[str] = []
    res = asyncio.run(CodexRunner(codex_bin="nope-not-real-bin", log=logged.append).run(RuntimePlan(prompt="x")))
    assert res.is_error and res.status == "blocked"
    assert classify_outcome(res) == Outcome.ERROR
    assert res.exit_reason is ExitReason.RUNTIME_UNAVAILABLE
    assert "not found on PATH" in res.summary
    assert any("NOT READY" in m for m in logged)


def test_milestone_lines_surface_without_debug(tmp_path):
    """The key ask: TEST_CMD runs + commits are visible even with debug OFF."""
    body = (
        r"""
echo "exec bash -lc 'uv run pytest'" >&2
echo "exec git commit -m 'green: impl'" >&2
echo "some chatty verbose line" >&2
"""
        + _DONE
    )
    logged: list[str] = []
    _run(tmp_path, body, log=logged.append, debug=False)
    joined = "\n".join(logged)
    assert "codex TEST_CMD:" in joined  # pytest run surfaced
    assert "codex commit:" in joined  # commit surfaced
    assert "some chatty verbose line" not in joined  # noise stays suppressed


# ── _is_transport_drop (pure classifier) ──────────────────────────────────────
def _res(status="error", is_error=True, sid=""):
    return AgentResult(is_error=is_error, status=status, summary="", session_id=sid)


# ══ review M1 / plan D12 — codex token accounting ═══════════════════════════
# Without a price table `cost_usd` was hardcoded 0.0, the ledger write is gated
# on `cost > 0`, and BOTH spend caps were blind to every codex run.


def _rollout_with(tmp_path, sid: str, *lines: str, tail: str = "") -> None:
    day = tmp_path / "sessions" / "2026" / "10" / "13"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"rollout-now-{sid}.jsonl").write_text("".join(line + "\n" for line in lines) + tail)


def _token_count(info) -> str:
    return json.dumps(
        {"timestamp": datetime.now(timezone.utc).isoformat(), "payload": {"type": "token_count", "info": info}}
    )


def _totals(input_tokens: int, cached: int, cache_write: int, output: int) -> str:
    return _token_count(
        {
            "total_token_usage": {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached,
                "cache_write_input_tokens": cache_write,
                "output_tokens": output,
                "total_tokens": input_tokens + output,
            }
        }
    )


def test_read_rollout_usage_prefers_last_cumulative_token_count(tmp_path):
    sid = "019fa244-56c7-7de0-8e18-e56f423bbbcc"
    _rollout_with(
        tmp_path,
        sid,
        json.dumps({"type": "session_meta", "payload": {"type": "session_meta"}}),
        _totals(100, 50, 0, 10),
        _totals(54019, 35328, 7, 604),
    )
    assert read_rollout_usage(sid, codex_home=str(tmp_path)) == {
        "input_tokens": 54019,
        "output_tokens": 604,
        "cache_read_input_tokens": 35328,
        "cache_creation_input_tokens": 7,
        "source": "rollout",
    }


def test_read_rollout_usage_is_none_when_file_or_session_missing(tmp_path):
    assert read_rollout_usage("", codex_home=str(tmp_path)) is None
    assert read_rollout_usage("no-such-session", codex_home=str(tmp_path)) is None
    # Unparseable lines that carry no counters, and a counter record codex is
    # still writing (no newline yet), are no evidence rather than a raise.
    _rollout_with(tmp_path, "badf00d", "not json at all", tail='{"payload": {"type": "token_count"')
    assert read_rollout_usage("badf00d", codex_home=str(tmp_path)) is None


@pytest.mark.parametrize(
    "record",
    [
        '{"payload": {"type": "token_count"',
        _token_count({"total_token_usage": {"input_tokens": "12", "output_tokens": 3}}),
        _token_count({"total_token_usage": None}),
        _token_count(["not", "a", "dict"]),
    ],
    ids=["unparseable", "string-counter", "null-totals", "non-dict-info"],
)
def test_a_complete_counter_record_that_does_not_parse_fails_closed(tmp_path, record):
    _rollout_with(tmp_path, "bad", record)
    with pytest.raises(MalformedRolloutCounters):
        read_rollout_usage("bad", codex_home=str(tmp_path))


def test_a_counter_record_without_counters_yet_is_skipped(tmp_path):
    """codex emits `info: null` token_count records (rate-limit snapshots)."""
    _rollout_with(
        tmp_path,
        "early",
        _token_count(None),
        _token_count({"total_token_usage": {"input_tokens": 10, "output_tokens": 2}}),
        _token_count(None),
    )
    assert read_rollout_usage("early", codex_home=str(tmp_path))["input_tokens"] == 10


def _rollout_append(sid: str, event: str) -> str:
    """Shell that appends one rollout event, as codex does mid-run."""
    return (
        f'mkdir -p "$CODEX_HOME/sessions/2026/10/13"\n'
        f"printf '%s\\n' '{event}' >> \"$CODEX_HOME/sessions/2026/10/13/rollout-now-{sid}.jsonl\"\n"
    )


def test_a_run_whose_counters_are_malformed_ends_as_an_agent_attempt(tmp_path):
    """Nothing can price it, so the live budget cannot hold: the leg ends as an
    agent attempt (bounded by the attempt cap), never a free machine retry."""
    bad = _token_count({"total_token_usage": {"input_tokens": "x"}})
    r = _run(
        tmp_path,
        _meta("sess-bad") + _rollout_append("sess-bad", bad) + _OPENED,
        rate_table=CARD,
        plan=RuntimePlan(prompt="x", max_budget_usd=3),
    )

    assert r.is_error is True and r.exit_reason is ExitReason.AGENT_ERROR
    assert classify_leg(r, resumed=False) is LegClass.AGENT
    assert "malformed" in r.summary
    assert r.cost_usd == 3.0  # the reservation's maximum, never zero


def test_usage_delta_subtracts_the_already_ledgered_baseline():
    # A follow-up leg resumes the resolve's session; the rollout counter is
    # cumulative, so without the delta the leg re-ledgers the resolve's tokens
    # (observed live: a 114s fix-ci ledgered $39.57 carrying 6M resolve tokens).
    end = {
        "input_tokens": 7_809_575,
        "output_tokens": 17_467,
        "cache_read_input_tokens": 7_432_192,
        "cache_creation_input_tokens": 0,
    }
    start = {
        "input_tokens": 6_069_388,
        "output_tokens": 15_541,
        "cache_read_input_tokens": 5_837_056,
        "cache_creation_input_tokens": 0,
    }
    d = usage_delta(end, start)
    assert (d["input_tokens"], d["output_tokens"], d["cache_read_input_tokens"]) == (1_740_187, 1_926, 1_595_136)


def test_usage_delta_edges():
    # No end usage → None (no fabricated receipt). No baseline → end unchanged.
    assert usage_delta(None, {"input_tokens": 5}) is None
    end = {"input_tokens": 10, "output_tokens": 2}
    assert usage_delta(end, None) == end
    # A counter that shrank (rollout replaced/rotated) floors at zero rather
    # than ledgering negative spend.
    assert usage_delta({"input_tokens": 3}, {"input_tokens": 9})["input_tokens"] == 0


def _costed_codex(tmp) -> str:
    """A stub that writes provider-owned rollout usage and an envelope."""
    usage = json.dumps(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 48000,
                        "cached_input_tokens": 12000,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 4341,
                    }
                },
            },
        },
        separators=(",", ":"),
    )
    return _fake_codex(tmp, _rollout_append("sess-cost", usage) + _meta("sess-cost") + _OPENED)


def test_run_reports_zero_cost_without_rates(tmp_path):
    """Anti-vacuity twin: same stub, no price table → 0.0 (and the startup
    warning below is the operator's only signal)."""
    r = asyncio.run(
        CodexRunner(codex_bin=_costed_codex(tmp_path), env={"OPENAI_API_KEY": "k", "CODEX_HOME": str(tmp_path)}).run(
            RuntimePlan(prompt="x")
        )
    )
    assert r.status == "pr_opened"
    assert r.cost_usd == 0.0


def _refused_codex(codex_error_info: str, *, at: datetime | None = None) -> str:
    """A stub whose only turn the provider refused: codex records the refusal
    on the rollout's terminal `task_complete`, writes no envelope, exits 1."""
    event = json.dumps(
        {
            # Written before the run starts, so stamped just after it would begin.
            "timestamp": (at or datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "last_agent_message": None,
                "error": {"message": "provider refused", "codex_error_info": codex_error_info},
            },
        },
        separators=(",", ":"),
    )
    return _rollout_append("sess-refused", event) + _meta("sess-refused") + 'echo "turn failed" >&2\nexit 1\n'


def _run_refused(codex_error_info, **kwargs):
    result = _run(tempfile.mkdtemp(), _refused_codex(codex_error_info, **kwargs))
    return result, infra_class_failure(result)


@pytest.mark.parametrize("codex_error_info", ["server_overloaded", "usage_limit_exceeded"])
def test_provider_overload_terminal_is_a_machine_failure_that_keeps_its_session(codex_error_info):
    result, machine = _run_refused(codex_error_info)
    # RESUME and review classify on exit_reason, so it must say transport death.
    assert result.is_error and machine and result.exit_reason is ExitReason.TRANSPORT_DEATH
    assert result.session_id and resumable_session(result) == result.session_id


@pytest.mark.parametrize("codex_error_info", ["other", "", "x" * 10_000])
def test_unrecognised_provider_error_stays_an_agent_error(codex_error_info):
    result, machine = _run_refused(codex_error_info)
    assert result.is_error and not machine and result.exit_reason is not ExitReason.TRANSPORT_DEATH


def test_an_earlier_turns_overload_does_not_classify_this_run():
    result, machine = _run_refused("server_overloaded", at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert result.is_error and not machine


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json\n",
        '{"payload":{"type":"task_complete","error":"overloaded"}}\n',
        '{"timestamp":"bad","payload":{"type":"task_complete","error":{"codex_error_info":"server_overloaded"}}}\n',
    ],
)
def test_malformed_rollout_evidence_never_claims_a_provider_failure(tmp_path, content):
    from issue_agent.adapters.codex_runner import read_rollout_provider_failure

    _rollout_with(tmp_path, "sess-bad", tail=content)
    assert read_rollout_provider_failure("sess-bad", str(tmp_path), since=0.0) is None
    assert read_rollout_provider_failure("", str(tmp_path)) is None
    assert read_rollout_provider_failure("missing", str(tmp_path)) is None


# ── #43: each leg is priced with the model it actually ran ───────────────────


def test_the_result_reports_the_model_that_actually_ran(tmp_path):
    """What the journal needs to split cost by routed tier — an unpinned task
    carries model=None, so the run has to say what it used."""
    runner = CodexRunner(codex_bin=_costed_codex(tmp_path), env={"OPENAI_API_KEY": "k"}, default_model="gpt-5.6-terra")
    assert asyncio.run(runner.run(RuntimePlan(prompt="x"))).model == "gpt-5.6-terra"
    assert asyncio.run(runner.run(RuntimePlan(prompt="x", model="gpt-5.6-luna"))).model == "gpt-5.6-luna"


# ══ review M17a — the "real output" guard must use the real status vocabulary ═
# The old guard was a hand-copied tuple ("pr_opened", "blocked", "done") that had
# drifted from every schema: `done` exists in none of them, and `pushed` — the
# status of a COMPLETED FORCE-PUSH — was missing, so that leg could be re-run on
# resume. Pin the vocabulary here so a schema change breaks loudly.
_FIXER_TERMINAL_STATUSES = frozenset({"pushed", "blocked", "failed"})
# `done` is in no schema and `pr_opened` left it in slice 5b (the harness opens
# the PR now); classify_outcome still accepts both, so a codex run (or a
# resumed pre-5b session) that emits one must still count as real output.
_LEGACY_OK_ALIASES = frozenset({"done", "pr_opened"})


def test_fixer_status_vocabulary_is_exactly_the_schemas_union():
    """Drift alarm: if a leg's schema gains or loses a terminal status, this
    fails and whoever changed it must re-check the retry guard below."""
    union = set(FIX_SCHEMA["properties"]["status"]["enum"])
    assert union == set(_FIXER_TERMINAL_STATUSES)


@pytest.mark.parametrize("status", sorted(_FIXER_TERMINAL_STATUSES | _LEGACY_OK_ALIASES))
def test_a_captured_envelope_is_never_a_transport_drop_whatever_its_terminal_status(status):
    """review M17a: every terminal status in the schemas means the turn COMPLETED.
    Retrying over one re-runs paid, side-effecting work (`pushed` = a force-push
    that already landed on the branch)."""
    result = parse_codex_envelope(
        exit_code=1,
        last_message=json.dumps({"status": status, "summary": "s"}),
        stderr="ERROR: stream disconnected before completion",
    )
    assert classify_outcome(result) is not Outcome.ERROR
    assert _is_transport_drop(1, "ERROR: stream disconnected before completion", result) is False


# ══ review M17b — classify on the stderr TAIL, not the whole agent trace ══════
# Codex streams the agent's whole tool trace (patches, test output, greps) to
# stderr. A fixer working on ANY throttling code prints provider-pushback wording
# verbatim as it patches — which is the wording the retry classifier matches on.
_POISON_PATCH_LINE = "apply_patch: +    raise HTTPException(429, Too Many Requests)  # rate limit exceeded"


def _trace_with(tail: str, poison: str = _POISON_PATCH_LINE) -> str:
    """A realistic accumulated codex trace: the agent's own output carries the
    poison wording early, then 30 lines of unrelated trace, then `tail`."""
    return "\n".join([poison] + [f"codex| trace line {i}" for i in range(30)] + [tail])


_DROP = "ERROR: stream disconnected before completion"


@pytest.mark.parametrize(
    "exit_code,stderr,result,drop",
    [
        (1, _DROP + ": response.failed event received", _res(), True),
        (1, "azure error: exceeded rate limit", _res(), True),  # a 429 is a transport drop
        (0, "response.failed event received", _res(), False),  # exit 0 never is
        (1, "2 failed, 10 passed\nTypeError: ...", _res(), False),  # a real agent error
        # A completed turn is never retried, even on nonzero exit or 429 text.
        (1, _DROP, _res(status="blocked", is_error=False), False),
        (1, "response.failed event received", _res(status="pr_opened", is_error=False), False),
        (0, "exceeded rate limit", _res(status="pr_opened", is_error=False), False),
    ],
)
def test_transport_drop(exit_code, stderr, result, drop):
    assert _is_transport_drop(exit_code, stderr, result) is drop


@pytest.mark.parametrize(
    "text,limited",
    [
        ("azure-openai error: Your requests to gpt-5.5 for your-deployment in eastus2 have exceeded rate limit.", True),
        ('"code":"rate_limit_exceeded"', True),
        ("HTTP 429 Too Many Requests", True),
        ("quota exceeded for this deployment", True),
        # A plain network drop is NOT a rate limit (routes to the short backoff).
        ("stream disconnected before completion", False),
        ("response.failed event received", False),
    ],
)
def test_is_rate_limited(text, limited):
    assert _is_rate_limited(text) is limited


@pytest.mark.parametrize(
    "tail,poison,signal",
    [
        ("Error: 2 tests failed", _POISON_PATCH_LINE, False),
        ("Error: 2 tests failed", "apply_patch: +    assert e.msg == stream disconnected before completion", False),
        # Anti-vacuity twin: the provider's 429 lands as the last thing printed.
        ("azure-openai error: have exceeded rate limit.", _POISON_PATCH_LINE, True),
    ],
)
def test_only_the_stderr_tail_classifies_provider_pushback(tail, poison, signal):
    """review M17b: matching the whole accumulated trace let the AGENT'S OWN TEXT
    (a log it read, a test it wrote) buy 15 minutes of 60s retries and launder a
    plain agent error into TRANSPORT_DEATH, the one exit reason the breakers exempt."""
    trace = _trace_with(tail, poison=poison)
    assert _is_transport_drop(1, trace, AgentResult(is_error=True)) is signal
    assert _is_rate_limited(trace) is (signal and "rate limit" in tail)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (dict(idle_timeout=0.3, poll_slice=0.05), "idle_timeout"),
        (
            dict(
                idle_timeout=900.0,
                max_runtime=900.0,
                poll_slice=0.1,
                wall_clock=_jumping_wall_clock(1000.0, 1000.0, 1000.0, 4600.0),
            ),
            "wall_timeout",
        ),
    ],
)
def test_270_watchdog_kill_logs_signal_target_reason_and_run(tmp_path, kwargs, reason):
    """#270: the harness's own SIGKILL of a codex group must be attributable."""
    logged: list[str] = []

    _run(tmp_path, 'echo "codex tool: step 1" >&2\nsleep 30\n', log=logged.append, **kwargs)

    (line,) = [m for m in logged if m.startswith("signal-send:")]
    assert "SIGKILL" in line and f"reason={reason}" in line
    assert re.search(r"pgid=\d+", line) and "run_id=" in line
