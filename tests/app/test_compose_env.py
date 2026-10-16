"""Env-compose pipeline + doctor preflight — review C5 / D3 / D7.

C5(a): compose-env.sh once emitted a hand-maintained allow-list that dropped ~18
documented knobs; it now emits every var shared.env + the overlay define.
P0R-1: a PRE/POST diff alone dropped a var assigned to the value the launching
shell already exported (`FOO="${FOO:-default}"`), so it emits the UNION of the
diff and the assignment names scanned from the files — hence the set-equality
test. C5(b): main() exports the composed config into os.environ, since systemd
units set only AGENT_ENV + TARGET_DIR and subprocesses (git, gh, codex) inherit
it. D7: doctor fails when two overlays resolve to one state dir, and compares
account logins without collapsing Users and Bots (#481).

These run the REAL scripts: compose-env.sh is copied into a scratch tree so its
self-locating root resolves to scratch config, and doctor.sh runs with
HARNESS_ROOT overridden and fake gh/claude/jq/uv stubs prepended to PATH.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from issue_agent.app import main as main_module
from issue_agent.app.main import _load_agent_env

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_ENV_SH = ROOT / "bin" / "compose-env.sh"
DOCTOR_SH = ROOT / "bin" / "doctor.sh"


# ── shared fixtures ────────────────────────────────────────────────────────────
_GH_BASE = (
    'if [ "$1" = "--version" ]; then echo "gh version 0.0.0-fake"; exit 0; fi\n'
    'if [ "$1" = "api" ] && [ "$2" = "user" ]; then echo "fake-user"; exit 0; fi\n'
    'if [ "$1" = "repo" ] && [ "$2" = "view" ]; then exit 0; fi\n'
)
_STUBS = {
    "claude": 'echo "claude 0.0.0-fake"\n',
    # Any `uv run` doctor makes is stubbed so the check stays hermetic.
    "uv": 'if [ "$1" = "--version" ]; then echo "uv 0.0.0-fake"; exit 0; fi\nexit 2\n',
}


def _tool_dir(path: Path, scripts: dict[str, str]) -> Path:
    path.mkdir()
    for name, body in scripts.items():
        (path / name).write_text("#!/usr/bin/env bash\n" + body)
        (path / name).chmod((path / name).stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_tool_path(tmp_path: Path) -> Path:
    """A PATH-prependable dir with fast, deterministic stand-ins for the
    external tools bin/doctor.sh shells out to, so tests never depend on real
    gh auth, network, or a real `uv` environment. `git` is
    deliberately NOT stubbed — the real one is already on PATH and
    `git --version` is harmless."""
    return _tool_dir(tmp_path / "fake_bin", {**_STUBS, "gh": _GH_BASE + "exit 0\n", "jq": 'echo "jq-0.0.0-fake"\n'})


def _run_doctor(env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **env}
    # doctor requires an explicit config. An empty fixture keeps these checks
    # isolated from operator config while preserving the test's passed env.
    if "AGENT_ENV" not in env:
        empty = Path(tempfile.mkdtemp(prefix="doctor-cfg-")) / "empty.env"
        empty.write_text("")
        full_env["AGENT_ENV"] = str(empty)
    return subprocess.run(
        ["bash", str(DOCTOR_SH)],
        env=full_env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _doctor_with(fake_bin: Path, **env: str) -> subprocess.CompletedProcess:
    return _run_doctor({"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}", **env})


def _install_compose_env(tmp_path: Path, shared: str, overlays: dict[str, str]) -> Path:
    """Copy the REAL compose-env.sh into <tmp_path>/bin/ so its self-locating
    root (`dirname "${BASH_SOURCE[0]}"/..`) resolves to the scratch tree, not
    the real repo's config/, beside a scratch shared.env and repos/<name>.env
    overlays. Reads the current file content each run, so the test always
    exercises the shipped script. point-at.sh rides along because compose-env
    parses the TARGET_ENV_ scrub floor from it (fail-closed)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dest = bin_dir / "compose-env.sh"
    dest.write_text(COMPOSE_ENV_SH.read_text())
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    (bin_dir / "point-at.sh").write_text((ROOT / "bin" / "point-at.sh").read_text())
    (tmp_path / "config" / "repos").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "shared.env").write_text(shared)
    for name, text in overlays.items():
        (tmp_path / "config" / "repos" / f"{name}.env").write_text(text)
    return dest


def _compose(tmp_path: Path, shared: str, overlay: str, ambient: dict | None = None) -> Path:
    """Run the REAL compose-env.sh over a shared.env + repos/demo.env pair with
    `ambient` merged into the launching shell's environment, and return the
    composed .active.env path. An `ambient` value of None DELETES that var from
    the launching environment, so a test can assert on the "operator has not
    exported it" case without depending on whatever the developer's shell
    happens to hold."""
    compose_env_sh = _install_compose_env(tmp_path, shared, {"demo": overlay})
    env = dict(os.environ)
    for key, value in (ambient or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    subprocess.run(["bash", str(compose_env_sh), "demo"], check=True, capture_output=True, text=True, env=env)
    return tmp_path / "config" / ".active.demo.env"


def _emitted(active: Path) -> dict[str, str]:
    """The KEY=%q-VALUE lines of a composed .active.env, as a dict of the RAW
    (still shell-quoted) values. `printf %q` always emits a single line, so
    line-splitting is safe even for values containing newlines. Use
    `_load_agent_env` when the unquoted value matters."""
    return dict(
        line.split("=", 1)
        for line in active.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )


# `NAME=` / `export NAME=` at the start of a line — an INDEPENDENT restatement
# (Python re, not the script's grep) of "a name these config files assign", so
# the set-equality test below cannot pass by mirroring the implementation.
_ASSIGN_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)
_UNSET_RE = re.compile(r"^[ \t]*unset[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*$", re.MULTILINE)


def test_compose_env_emits_the_files_with_overlay_precedence_and_nothing_ambient(tmp_path):
    """The overlay wins over shared.env, and only what the two files define is
    emitted: no unrelated ambient var and none of the shell's own (PATH/PWD/...)."""
    active = _compose(
        tmp_path,
        shared='MERGE_METHOD="merge"\nFIX_BOT="fix-bot"\n',
        overlay='REPO="acme/widget"\nMERGE_METHOD="squash"\n',
        ambient={"SOME_UNRELATED_AMBIENT_VAR": "should-not-leak"},
    )
    text = active.read_text()
    assert "MERGE_METHOD=squash" in text and "MERGE_METHOD=merge" not in text
    assert "SOME_UNRELATED_AMBIENT_VAR" not in text
    for shell_owned in ("PATH=", "PWD=", "SHLVL=", "\n_=", "OLDPWD="):
        assert shell_owned not in text


def test_compose_env_missing_overlay_fails_loud(tmp_path):
    # Anti-vacuity twin for the usage/error paths (unchanged by the rewrite,
    # but now exercised through the real script instead of assumed).
    compose_env_sh = _install_compose_env(tmp_path, 'FIX_BOT="fix-bot"\n', {})

    result = subprocess.run(["bash", str(compose_env_sh), "nonexistent"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "nonexistent.env" in result.stderr


# P0R-1: a var the files assign must be emitted even when its value equals the
# launching shell's. Pre-fix, a PRE/POST diff saw no change and omitted it; under
# systemd the daemon then read TEST_CMD=None and every gate failed closed.
def test_compose_env_emitted_key_set_equals_the_names_the_files_assign(tmp_path):
    # review §5's structural lesson: for anything that emits a SET, assert set
    # equality against an independently-derived expectation, never membership
    # of the happy-path examples — membership is exactly what let P0R-1 ship
    # green. The expectation is re-derived here from the fixture text with a
    # Python regex, so omitting ANY var (not just the ones this test happens to
    # name) fails, and so does leaking any extra one.
    shared = (
        # unchanged vs. ambient — the P0R-1 shape
        'TEST_CMD="uv run pytest -q"\n'
        'ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-fallback}"\n'
        # overridden by the overlay
        'MERGE_METHOD="merge"\n'
        # assignment-shaped lines that must NOT count
        '# COMMENTED_KNOB="nope"\n'
        '#TIGHT_COMMENT="nope"\n'
        '\t# INDENTED_COMMENT="nope"\n'
        # leading whitespace + export prefix, which MUST count
        '  export INDENTED_EXPORT="yes"\n'
        # assigned then unset: a name the scan sees but POST does not have, so
        # it must not be emitted as an empty string
        'GONE_LATER="temp"\n'
        "unset GONE_LATER\n"
        # set INDIRECTLY, via a nested `source` (no assignment-shaped line in
        # either config file names it) — proves the PRE/POST diff arm of the
        # union is still load-bearing and was not replaced by the file scan.
        '. "$(dirname "${BASH_SOURCE[0]}")/nested.env"\n'
    )
    overlay = 'REPO="acme/widget"\nMERGE_METHOD="squash"\nCI_PENDING_MAX_SECONDS="3600"\nEMPTY_ON_PURPOSE=\n'
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "nested.env").write_text('INDIRECT_FROM_NESTED_SOURCE="via-nested"\n')
    active = _compose(
        tmp_path,
        shared,
        overlay,
        ambient={
            "TEST_CMD": "uv run pytest -q",
            "ANTHROPIC_AUTH_TOKEN": "sk-ambient-real",
            "SOME_UNRELATED_AMBIENT_VAR": "should-not-leak",
            # Commented names exported ambiently: a scan that matched a comment
            # would emit them with these values.
            "COMMENTED_KNOB": "leaked",
            "TIGHT_COMMENT": "leaked",
        },
    )

    expected = set(_ASSIGN_RE.findall(shared)) | set(_ASSIGN_RE.findall(overlay))
    expected -= set(_UNSET_RE.findall(shared)) | set(_UNSET_RE.findall(overlay))
    expected |= {"INDIRECT_FROM_NESTED_SOURCE"}
    # compose-env appends the #63 scrub manifest naming every emitted knob.
    emitted = _emitted(active)
    # raw _emitted values keep %q escaping (backslash-space) — unescape first
    assert set(emitted.pop("HARNESS_DEPLOY_KNOBS").replace("\\", "").split()) == expected
    assert set(emitted) == expected

    env = _load_agent_env({"AGENT_ENV": str(active)})
    assert {
        key: env[key]
        for key in (
            "TEST_CMD",
            "ANTHROPIC_AUTH_TOKEN",
            "MERGE_METHOD",
            "INDENTED_EXPORT",
            "EMPTY_ON_PURPOSE",
            "INDIRECT_FROM_NESTED_SOURCE",
        )
    } == {
        "TEST_CMD": "uv run pytest -q",
        "ANTHROPIC_AUTH_TOKEN": "sk-ambient-real",
        "MERGE_METHOD": "squash",
        "INDENTED_EXPORT": "yes",
        "EMPTY_ON_PURPOSE": "",
        "INDIRECT_FROM_NESTED_SOURCE": "via-nested",
    }


def test_compose_env_emits_only_sourceable_identifier_names(tmp_path):
    # `set -a` exports FUNCTIONS as well as variables, so a helper defined in a
    # config file appears in `env -0` as `BASH_FUNC_name%%=() {...}`. Emitted
    # verbatim it made .active.env unsourceable — the line parses as a command
    # ("BASH_FUNC_name%%=...: command not found"), which is exactly the file
    # both `_load_agent_env` and the bash tools `source`. Assert the composed
    # file is clean AND that sourcing it is silent.
    active = _compose(
        tmp_path, shared='_helper() { HELPED="yes"; }\n_helper\nFIX_BOT="fix-bot"\n', overlay='REPO="acme/widget"\n'
    )
    assert not [k for k in _emitted(active) if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)]
    proc = subprocess.run(
        ["bash", "-c", 'set -a; source "$1"; set +a', "check", str(active)], capture_output=True, text=True
    )
    assert (proc.returncode, proc.stderr) == (0, "")
    # The variable the function set is still config and still arrives.
    assert _load_agent_env({"AGENT_ENV": str(active)})["HELPED"] == "yes"


def test_compose_env_scans_a_final_line_with_no_trailing_newline(tmp_path):
    # A hand-edited overlay whose last line has no "\n" is the shape most
    # likely to be silently half-scanned. The last knob must still be emitted
    # even though its value matches the ambient shell's (the P0R-1 condition).
    active = _compose(
        tmp_path,
        shared='A_KNOB="one"\nB_KNOB="two"',  # deliberately unterminated
        overlay='REPO="acme/widget"',  # deliberately unterminated
        ambient={"A_KNOB": "one", "B_KNOB": "two", "REPO": "acme/widget"},
    )
    assert set(_emitted(active)) == {"A_KNOB", "B_KNOB", "REPO", "HARNESS_DEPLOY_KNOBS"}


def test_compose_env_omits_a_conditional_assignment_that_never_fired(tmp_path):
    # A name the file scan sees inside a branch that did not execute, and that
    # nothing else set, must NOT be emitted as an empty string — an empty
    # TEST_CMD reads as "no test command" and is as bad as a missing one.
    active = _compose(
        tmp_path,
        shared='if [ -n "${TRIGGER:-}" ]; then\n  MAYBE_KNOB="fired"\nfi\n',
        overlay='REPO="acme/widget"\n',
        ambient={"MAYBE_KNOB": None, "TRIGGER": None},
    )
    assert "MAYBE_KNOB" not in _emitted(active)


def test_compose_env_roundtrips_awkward_values_byte_exactly(tmp_path):
    # %q → `set -a; source` must be lossless for the values that break naive
    # KEY=VALUE writers: spaces, both quote flavours, a literal newline, a
    # trailing space, and a literal `$`. These values all also equal what the
    # shell exports ambiently, so this doubles as a P0R-1 case at the awkward
    # end of the value space.
    values = {
        "TEST_CMD": 'set -e; (cd backend && uv run pytest -q) && echo "done"',
        "MULTILINE_KNOB": "line one\nline two\ttabbed",
        "QUOTED_KNOB": "it's got 'single' and \"double\" quotes",
        "TRAILING_SPACE_KNOB": "value with trailing space ",
        "DOLLAR_KNOB": "$NOT_EXPANDED and ${ALSO_NOT}",
    }
    # shlex.quote writes each value as an unambiguous POSIX single-quoted bash
    # token, so the fixture encodes exactly these bytes; the assertion is on
    # the values that come back out, not on the encoding.
    shared = "".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items())
    active = _compose(tmp_path, shared, 'REPO="acme/widget"\n', ambient=values)
    env = _load_agent_env({"AGENT_ENV": str(active)})
    for k, v in values.items():
        assert env[k] == v, f"{k} did not round-trip"


# ── S7/D3: main.py delivers env to subprocesses + loads it once ───────────────
def test_main_exports_composed_env_into_os_environ_before_building(tmp_path, monkeypatch):
    # review C5(b), driven through the REAL main() code path (not simulated):
    # build_role is stubbed to raise immediately so nothing past the export
    # runs (no real GitHub/worktree/adapter construction), but os.environ.
    # update(env) — the line under test — executes for real.
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\nFIX_BOT="fixer"\nCI_PENDING_MAX_SECONDS="3600"\nMAX_RESOLVE_RETRIES="9"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.delenv("CI_PENDING_MAX_SECONDS", raising=False)
    monkeypatch.delenv("MAX_RESOLVE_RETRIES", raising=False)
    # Avoid a real flock/tempfile side effect — the single-instance guard is
    # not what this test is about.
    monkeypatch.setattr(main_module, "acquire", lambda role, repo, **_scope: object())

    def _boom(role, env, _scheduler_progress=None):
        raise RuntimeError("stop-before-build-role")

    monkeypatch.setattr(main_module, "build_role", _boom)
    monkeypatch.setattr(main_module, "_recover_pre_adapter_processes", lambda *_args: ())

    with pytest.raises(RuntimeError):
        main_module.main(["fix"])

    assert os.environ["CI_PENDING_MAX_SECONDS"] == "3600"
    assert os.environ["MAX_RESOLVE_RETRIES"] == "9"


def _machine_state(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr(
        "issue_agent.app.config.machine_control_authority_root", lambda: str(tmp_path / "machine-state")
    )
    return tmp_path / "state"


def test_build_role_does_not_reload_env_when_already_provided(tmp_path, monkeypatch):
    monkeypatch.setattr("issue_agent.adapters.github.GitHubGateway.viewer_login", lambda self: "fix-bot")
    # review S7: `_load_agent_env` shells out to `bash -c ... env -0`.
    # build_role used to call it unconditionally
    # (`_load_agent_env(env if env is not None else dict(os.environ))`), so
    # main()'s already-loaded dict was reloaded a second time on every boot.
    # Drive the REAL build_role (not mocked) with an env dict provided, and
    # fail loudly if it reaches _load_agent_env at all.
    def _must_not_be_called(env):  # pragma: no cover - only runs on regression
        raise AssertionError("build_role reloaded env via _load_agent_env")

    monkeypatch.setattr(main_module, "_load_agent_env", _must_not_be_called)
    state = _machine_state(monkeypatch, tmp_path)

    # FIX_BOT is required as of review P1R-7 (an unset one made every
    # unresolvable PR author read as ours), so a config built here must carry it.
    assert (
        main_module.build_role(
            "fix",
            {
                "REPO": "o/r",
                "FIX_BOT": "fix-bot",
                "FIX_BOT_TOKEN": "ghp_fix",  # the fix role refuses to start without it (#443)
                "STATE_DIR": str(state / "fix"),
                "REVIEW_STATE_DIR": str(state / "review"),
            },
        )
        is not None
    )


def test_build_role_self_loads_env_when_called_standalone_without_one(tmp_path, monkeypatch):
    monkeypatch.setattr("issue_agent.adapters.github.GitHubGateway.viewer_login", lambda self: "fix-bot")
    # Anti-vacuity twin: a standalone caller that does NOT already have a
    # loaded env (env=None, the documented default) must still get AGENT_ENV
    # applied — the "load once" fix must not silently drop loading entirely.
    calls = []
    real = main_module._load_agent_env

    def _counting(env):
        calls.append(1)
        return real(env)

    monkeypatch.setattr(main_module, "_load_agent_env", _counting)
    state = _machine_state(monkeypatch, tmp_path)
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("FIX_BOT", "fix-bot")  # required since review P1R-7
    monkeypatch.setenv("FIX_BOT_TOKEN", "ghp_fix")  # the fix role refuses to start without it (#443)
    monkeypatch.setenv("STATE_DIR", str(state / "fix"))
    monkeypatch.setenv("REVIEW_STATE_DIR", str(state / "review"))
    monkeypatch.delenv("AGENT_ENV", raising=False)

    assert main_module.build_role("fix") is not None
    assert len(calls) == 1


# ── D7: doctor state-dir collision check ───────────────────────────────────────
def _overlays(tmp_path: Path, overlays: dict[str, str]) -> Path:
    harness = tmp_path / "harness"
    (harness / "config" / "repos").mkdir(parents=True)
    for name, text in overlays.items():
        (harness / "config" / "repos" / f"{name}.env").write_text(text)
    return harness


def test_doctor_fails_on_two_overlays_sharing_a_state_dir(tmp_path):
    harness = _overlays(
        tmp_path,
        {
            "repo-a": 'REPO="acme/widget"\n',
            "repo-b": 'REPO="other/thing"\nSTATE_DIR="/custom/shared-state"\n',
            "repo-c": 'REPO="third/repo"\nSTATE_DIR="/custom/shared-state"\n',
        },
    )

    result = _doctor_with(_fake_tool_path(tmp_path), HARNESS_ROOT=str(harness))
    assert result.returncode != 0
    # "state-dir collision:" (with the colon) is the bad() finding line —
    # distinct from the "== per-repo state-dir collisions (...) ==" section
    # header, which contains the same words but is not itself a failure.
    assert "state-dir collision:" in result.stdout
    assert "repo-b" in result.stdout and "repo-c" in result.stdout


def test_doctor_state_dir_scan_ignores_active_overlay_environment(tmp_path):
    """The scan must compose each overlay from files, not the active config.

    ``doctor.sh`` is normally invoked after point-at has sourced the active
    overlay. An older overlay that omits explicit state directories must still
    receive its repository-derived defaults rather than inherit the active
    deployment's exported values.
    """
    active = {"REPO": "other/thing", "STATE_DIR": "/active/fix", "REVIEW_STATE_DIR": "/active/review"}
    harness = _overlays(
        tmp_path, {"older": 'REPO="acme/widget"\n', "active": "".join(f'{k}="{v}"\n' for k, v in active.items())}
    )

    result = _doctor_with(_fake_tool_path(tmp_path), HARNESS_ROOT=str(harness), **active)

    assert "state-dir collision:" not in result.stdout
    assert "no state-dir collisions across 2 overlay(s)" in result.stdout


def test_doctor_case_insensitive_bot_suffix_mismatch(tmp_path):
    result = _doctor_with(_fake_tool_path(tmp_path), FIX_BOT="Acme-Fixer[bot]", REVIEW_BOT="acme-fixer[BOT]", REPO="o/r")
    assert result.returncode != 0
    assert "FIX_BOT distinct from it" in result.stdout


def test_two_deployments_compose_to_separate_files(tmp_path):
    """Concurrent multi-repo deployments: composing repo B must not overwrite
    repo A's snapshot (the old fixed config/.active.env did exactly that, and
    the mtime hot-reload watch then re-pointed A's live daemons at B)."""
    compose_env_sh = _install_compose_env(
        tmp_path, "SHARED_KNOB=x\n", {"alpha": "REPO=acme/alpha\n", "beta": "REPO=acme/beta\n"}
    )

    for name in ("alpha", "beta"):
        subprocess.run(["bash", str(compose_env_sh), name], check=True, capture_output=True, text=True)

    assert "REPO=acme/alpha" in (tmp_path / "config" / ".active.alpha.env").read_text()
    assert "REPO=acme/beta" in (tmp_path / "config" / ".active.beta.env").read_text()
    assert not (tmp_path / "config" / ".active.env").exists()


# ── CI-on-base-branch preflight (keifu deployment, 2026-07-28) ─────────────────
# Three failures found by hand on the xnmp/keifu preflight, none of which any
# existing check could see:
#
#   * CI triggered on `main` while every PR targeted `chong-dev`, so no PR had
#     ever received a check. `ci_state` returns CIState.NONE for a PR with no
#     checks and domain/ci.py (reduce_ci) treats NONE identically to OK — approved
#     PRs would have auto-merged with zero CI verification, silently.
#   * The repo's `Security Audit` job is `continue-on-error: true`. GitHub still
#     reports its conclusion as FAILURE, and domain/ci.py:45 makes any fail
#     bucket a CIState.FAILURE, so the harness would have burned fix-ci legs
#     forever on a check no change can clear.
#   * A MERGE_METHOD the repo forbids fails at the last step of an already
#     fully-paid-for issue.
#
# These drive the REAL bin/doctor.sh with a gh stub that answers the three calls
# the section makes. `jq` is deliberately NOT stubbed here (unlike
# _fake_tool_path) — the section's detection lives in its jq programs, so a
# stubbed jq would test nothing.
def _ci_doctor(
    tmp_path: Path, extra_env: dict, *, workflows: int = 1, prs: list | None = None, allow_squash: bool = True
) -> subprocess.CompletedProcess:
    """Run doctor with a `gh` that answers the CI section's three queries from
    fixture data; everything else behaves like _fake_tool_path's stubs."""
    gh = (
        _GH_BASE
        + f"""if [ "$1" = "pr" ] && [ "$2" = "list" ]; then cat <<'JSON'
{json.dumps(prs if prs is not None else [])}
JSON
exit 0; fi
if [ "$1" = "api" ]; then
  case "$2" in
    */actions/workflows) echo '{workflows}'; exit 0 ;;
    repos/*)             echo '{str(allow_squash).lower()}'; exit 0 ;;
  esac
fi
exit 0
"""
    )
    fake_bin = _tool_dir(tmp_path / "fake_ci_bin", {**_STUBS, "gh": gh})
    harness = tmp_path / "harness"
    (harness / "config" / "repos").mkdir(parents=True, exist_ok=True)
    return _doctor_with(
        fake_bin,
        **{
            "HARNESS_ROOT": str(harness),
            "REPO": "acme/widget",
            "BASE_BRANCH": "trunk",
            "FIX_BOT": "acme-fixer",
            "REVIEW_BOT": "acme-review",
            **extra_env,
        },
    )


def test_doctor_checks_explicit_composed_snapshot(tmp_path):
    active = _compose(
        tmp_path, "FIX_BOT=acme-fixer\n", 'REPO=acme/widget\nBASE_BRANCH=trunk\nTEST_CMD="uv run pytest -q"\n'
    )
    result = _ci_doctor(tmp_path, {"AGENT_ENV": str(active)}, workflows=0)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"config: {active}" in result.stdout
    assert "REPO=acme/widget" in result.stdout
    assert "TEST_CMD=uv run pytest -q" in result.stdout


def _pr(number: int, checks: list | None) -> dict:
    return {"number": number, "statusCheckRollup": checks}


def test_doctor_errors_when_no_pr_into_base_ever_got_a_check(tmp_path):
    """The keifu shape: active workflows exist, but they trigger on a different
    branch, so every PR into the base reads CIState.NONE — which the harness
    merges on. Must be an ERROR, not a warning: it silently removes CI from the
    merge gate entirely."""
    result = _ci_doctor(tmp_path, {}, workflows=2, prs=[_pr(9, []), _pr(8, []), _pr(7, None)])
    assert result.returncode != 0
    assert "NOT ONE of the last 3 PR(s) into 'trunk' received a single check" in result.stdout
    # It must name the consequence, not just the symptom — an operator who does
    # not know NONE is treated as OK will read a checkless PR as harmless.
    assert "CIState.NONE" in result.stdout


def test_doctor_errors_when_the_repo_forbids_the_configured_merge_method(tmp_path):
    result = _ci_doctor(
        tmp_path,
        {"MERGE_METHOD": "squash"},
        prs=[_pr(9, [{"name": "Test", "conclusion": "SUCCESS"}])],
        allow_squash=False,
    )
    assert result.returncode != 0
    assert "MERGE_METHOD=squash is DISABLED" in result.stdout


def test_doctor_says_so_when_the_repo_has_no_workflows_at_all(tmp_path):
    """Not an error — a repo may legitimately have no CI. But the operator must
    be told that this makes the reviewer and TEST_CMD the entire merge gate."""
    result = _ci_doctor(tmp_path, {}, workflows=0)
    assert "no active Actions workflows" in result.stdout
    assert "NONE exactly like OK" in result.stdout


def test_doctor_warns_when_base_pushes_cancel_each_others_runs(tmp_path):
    """#402: a cancelled base run is no verdict, so a red base files nothing until a run completes."""
    for name, workflow in {"cancels": "on: [push, pull_request]\nconcurrency:\n  cancel-in-progress: true\n",
                           "prs-only": "on:\n  push:\nconcurrency:\n  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n",
                           "no-push": "on: pull_request\nconcurrency:\n  cancel-in-progress: true\n"}.items():
        target = tmp_path / name / "target"
        (target / ".github" / "workflows").mkdir(parents=True)
        (target / ".github" / "workflows" / "ci.yml").write_text(workflow)
        result = _ci_doctor(tmp_path / name, {"TARGET_DIR": str(target)})
        assert ("cancel-in-progress: true on a push-triggered workflow ( ci.yml )" in result.stdout) == (name == "cancels")


def test_doctor_distinguishes_a_user_from_the_same_name_app_bot(tmp_path):
    result = _doctor_with(_fake_tool_path(tmp_path), FIX_BOT="Acme-Fixer", REVIEW_BOT="acme-fixer[bot]", REPO="o/r")
    assert "two distinct identities (fix=Acme-Fixer, review=acme-fixer[bot])" in result.stdout
    assert "FIX_BOT distinct from it" not in result.stdout
