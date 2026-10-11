"""Env-compose pipeline + doctor preflight — review C5 / D3 / D7.

C5(a): bin/compose-env.sh used to emit a hand-maintained allow-list that
omitted ~18 documented knobs, so a systemd deployment silently dropped
operator config. It now snapshots the environment before/after sourcing
shared.env + the per-repo overlay and emits the diff — every var either file
defined, with no allow-list to fall out of sync.

P0R-1: that C5(a) fix regressed in the opposite direction — a PRE/POST diff
emits a var only when it is NEW or CHANGED, so a var the files DO assign to
the value the launching shell already exported was byte-identical in both
snapshots and silently dropped (`FOO="${FOO:-default}"`, or a TEST_CMD
matching the operator's exported value). compose-env.sh now emits the UNION of
the diff and the assignment names scanned out of the two files. The suite was
green with that bug present because every test asserted MEMBERSHIP of a
happy-path example or ABSENCE of ambient leakage; none asserted that an
assigned var is still emitted. Hence the set-equality test below.

C5(b): nothing exported the composed config into os.environ, so under systemd
(unit files set only AGENT_ENV + TARGET_DIR) every subprocess spawned
downstream (bash gate checkers, git, gh, codex — none of which pass `env=`
explicitly) inherited an empty environment: the ALLOW_UNTESTED escape hatch
became unreachable and operator knobs like CI_PENDING_MAX_SECONDS were silently
ignored. `app/main.py::main()` now calls `os.environ.update(env)` before
building anything.

D7 (doctor half): bin/doctor.sh gains a check that fails when two
config/repos/*.env overlays resolve to the same effective state dir (the
exact M5 shape — a second repo's fix daemon would resume the first repo's
issue-N.session), and its identity-distinctness check (C1) now normalizes
logins before comparing so `fix-bot` vs `fix-bot[bot]` is caught instead of
silently passing.

These tests exercise the REAL scripts via subprocess (not a description of
them) — bin/compose-env.sh is copied into a scratch tree so its
self-locating `dirname "${BASH_SOURCE[0]}"/..` root resolves to the scratch
config, never touching the real repo's (gitignored) config/. bin/doctor.sh is
run in place with HARNESS_ROOT overridden for the same reason, and PATH
prepended with fake gh/claude/jq/uv stubs so the run is hermetic (no network,
no real gh auth / tree-sitter needed) — per the wave brief's suggestion to
"test via a stub PATH with a fake gh".
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
from tests.fakes.executor import ImmediateExecutor as Executor

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_ENV_SH = ROOT / "bin" / "compose-env.sh"
DOCTOR_SH = ROOT / "bin" / "doctor.sh"


# ── shared fixtures ────────────────────────────────────────────────────────────
def _fake_tool_path(tmp_path: Path) -> Path:
    """A PATH-prependable dir with fast, deterministic stand-ins for the
    external tools bin/doctor.sh shells out to, so tests never depend on real
    gh auth, network, or a tree-sitter-capable `uv` environment. `git` is
    deliberately NOT stubbed — the real one is already on PATH and
    `git --version` is harmless."""
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    scripts = {
        "gh": (
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "--version" ]; then echo "gh version 0.0.0-fake"; exit 0; fi\n'
            'if [ "$1" = "api" ] && [ "$2" = "user" ]; then echo "fake-user"; exit 0; fi\n'
            'if [ "$1" = "repo" ] && [ "$2" = "view" ]; then exit 0; fi\n'
            'exit 0\n'
        ),
        "claude": '#!/usr/bin/env bash\necho "claude 0.0.0-fake"\n',
        "jq": '#!/usr/bin/env bash\necho "jq-0.0.0-fake"\n',
        # Governor self-test wants rc=2 (blocked) from `uv run --quiet <path>`;
        # stubbed unconditionally so the check is fast and independent of
        # whether plugin/hooks/bash_governor.py exists under the (possibly
        # scratch) HARNESS_ROOT being tested.
        "uv": (
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "--version" ]; then echo "uv 0.0.0-fake"; exit 0; fi\n'
            'exit 2\n'
        ),
    }
    for name, body in scripts.items():
        p = fake_bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin


def _run_doctor(env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    # Hermeticity: doctor.sh resolves `CONFIG="${AGENT_ENV:-$HARNESS_ROOT/config/agent.env}"`
    # and SOURCES it with `set -a`, so without an explicit AGENT_ENV it reads the
    # developer's own (gitignored) config/agent.env and that file's values
    # OVERRIDE the env this helper passes in. Tests then silently assert against
    # whatever happens to be on the machine — they passed for a while only
    # because their fixture logins coincided with the shipped example values in
    # that file. Point every run at an empty config unless the test supplies one.
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


def _install_compose_env(tmp_path: Path) -> Path:
    """Copy the REAL compose-env.sh into <tmp_path>/bin/ so its self-locating
    root (`dirname "${BASH_SOURCE[0]}"/..`) resolves to the scratch tree, not
    the real repo's config/. Reads the current file content each run, so the
    test always exercises the shipped script. point-at.sh rides along because
    compose-env parses the TARGET_ENV_ scrub floor from it (fail-closed)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dest = bin_dir / "compose-env.sh"
    dest.write_text(COMPOSE_ENV_SH.read_text())
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    point_at = bin_dir / "point-at.sh"
    point_at.write_text((ROOT / "bin" / "point-at.sh").read_text())
    return dest


def _compose(tmp_path: Path, shared: str, overlay: str, ambient: dict | None = None) -> Path:
    """Write a shared.env + repos/demo.env fixture pair, run the REAL
    compose-env.sh over them with `ambient` merged into the launching shell's
    environment, and return the composed .active.env path. An `ambient` value
    of None DELETES that var from the launching environment, so a test can
    assert on the "operator has not exported it" case without depending on
    whatever the developer's shell happens to hold."""
    compose_env_sh = _install_compose_env(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "shared.env").write_text(shared)
    repos_dir = tmp_path / "config" / "repos"
    repos_dir.mkdir(exist_ok=True)
    (repos_dir / "demo.env").write_text(overlay)

    env = dict(os.environ)
    for key, value in (ambient or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    subprocess.run(
        ["bash", str(compose_env_sh), "demo"],
        check=True, capture_output=True, text=True, env=env,
    )
    return tmp_path / "config" / ".active.demo.env"


def _emitted(active: Path) -> dict[str, str]:
    """The KEY=%q-VALUE lines of a composed .active.env, as a dict of the RAW
    (still shell-quoted) values. `printf %q` always emits a single line, so
    line-splitting is safe even for values containing newlines. Use
    `_load_agent_env` when the unquoted value matters."""
    out = {}
    for line in active.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key] = value
    return out


# `NAME=` / `export NAME=` at the start of a line — an INDEPENDENT restatement
# (Python re, not the script's grep) of "a name these config files assign", so
# the set-equality test below cannot pass by mirroring the implementation.
_ASSIGN_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)
_UNSET_RE = re.compile(r"^[ \t]*unset[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*$", re.MULTILINE)


# ── C5(a) + acceptance pipeline (wave centerpiece) ─────────────────────────────
def test_compose_env_pipeline_carries_knobs_outside_old_allow_list_to_os_environ(
    tmp_path, monkeypatch
):
    # review C5: end-to-end — shared.env + overlay -> compose-env.sh ->
    # .active.env -> _load_agent_env -> (simulated main.py) os.environ. Uses
    # CI_PENDING_MAX_SECONDS (documented, omitted from the old allow-list) and
    # MAX_INFRA_DEATHS (a brand-new knob no allow-list edit ever added), plus
    # a value with spaces/quotes to prove %q quoting survived the rewrite.
    compose_env_sh = _install_compose_env(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "shared.env").write_text(
        'IDENTITY_MODE="three"\nFIX_BOT="fix-bot"\n'
    )
    repos_dir = tmp_path / "config" / "repos"
    repos_dir.mkdir()
    (repos_dir / "demo.env").write_text(
        'REPO="acme/widget"\n'
        'CI_PENDING_MAX_SECONDS="3600"\n'
        'MAX_INFRA_DEATHS="7"\n'
        # Escaped \$ — a LITERAL dollar sign in the value, not a shell
        # parameter expansion (compose-env.sh sources this as real bash, so
        # an unescaped $var would be interpreted, per the documented
        # ${ANTHROPIC_AUTH_TOKEN:-...}-style feature).
        'WEIRD_VALUE="has spaces and \\"quotes\\" and \\$dollar signs"\n'
    )

    subprocess.run(
        ["bash", str(compose_env_sh), "demo"], check=True, capture_output=True, text=True
    )

    active = tmp_path / "config" / ".active.demo.env"
    active_text = active.read_text()
    assert "CI_PENDING_MAX_SECONDS=" in active_text
    assert "MAX_INFRA_DEATHS=7" in active_text
    assert "REPO=acme/widget" in active_text

    env = _load_agent_env({"AGENT_ENV": str(active)})
    assert env["CI_PENDING_MAX_SECONDS"] == "3600"
    assert env["MAX_INFRA_DEATHS"] == "7"
    assert env["WEIRD_VALUE"] == 'has spaces and "quotes" and $dollar signs'
    assert env["REPO"] == "acme/widget"

    # review C5(b), simulated: main.py applies this exact dict with
    # os.environ.update(env) before building anything. monkeypatch.setenv
    # undoes this automatically at teardown.
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert os.environ["CI_PENDING_MAX_SECONDS"] == "3600"
    assert os.environ["MAX_INFRA_DEATHS"] == "7"
    assert os.environ["WEIRD_VALUE"] == 'has spaces and "quotes" and $dollar signs'


def test_compose_env_repo_overlay_wins_over_shared(tmp_path):
    # Anti-vacuity twin: precedence still holds under the new diff-based
    # emission (shared sets a default, the per-repo overlay wins).
    compose_env_sh = _install_compose_env(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "shared.env").write_text('MERGE_METHOD="merge"\n')
    repos_dir = tmp_path / "config" / "repos"
    repos_dir.mkdir()
    (repos_dir / "demo.env").write_text('REPO="acme/widget"\nMERGE_METHOD="squash"\n')

    subprocess.run(
        ["bash", str(compose_env_sh), "demo"], check=True, capture_output=True, text=True
    )
    active_text = (tmp_path / "config" / ".active.demo.env").read_text()
    assert "MERGE_METHOD=squash" in active_text
    assert "MERGE_METHOD=merge" not in active_text


def test_compose_env_does_not_leak_ambient_or_shell_owned_vars(tmp_path):
    # Anti-vacuity twin of the pipeline test: the diff mechanism must emit
    # ONLY what the two files defined/changed — not a raw env dump. An
    # ambient var the files never touch (however it got into the composing
    # shell's environment) must not appear, and neither should PATH/PWD/HOME/
    # SHLVL/_ (the "vars the shell itself owns" the review explicitly calls
    # out).
    compose_env_sh = _install_compose_env(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "shared.env").write_text('FIX_BOT="fix-bot"\n')
    repos_dir = tmp_path / "config" / "repos"
    repos_dir.mkdir()
    (repos_dir / "demo.env").write_text('REPO="acme/widget"\n')

    env = dict(os.environ)
    env["SOME_UNRELATED_AMBIENT_VAR"] = "should-not-leak"
    subprocess.run(
        ["bash", str(compose_env_sh), "demo"],
        check=True, capture_output=True, text=True, env=env,
    )
    active_text = (tmp_path / "config" / ".active.demo.env").read_text()
    assert "SOME_UNRELATED_AMBIENT_VAR" not in active_text
    for shell_owned in ("PATH=", "PWD=", "SHLVL=", "\n_=", "OLDPWD="):
        assert shell_owned not in active_text


def test_compose_env_missing_overlay_fails_loud(tmp_path):
    # Anti-vacuity twin for the usage/error paths (unchanged by the rewrite,
    # but now exercised through the real script instead of assumed).
    compose_env_sh = _install_compose_env(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "shared.env").write_text('FIX_BOT="fix-bot"\n')
    (tmp_path / "config" / "repos").mkdir()

    result = subprocess.run(
        ["bash", str(compose_env_sh), "nonexistent"], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "nonexistent.env" in result.stderr


# ── P0R-1: a var the files assign must be emitted even when it is unchanged ───
def test_compose_env_emits_assigned_var_whose_value_equals_the_ambient_shells(tmp_path):
    # THE regression test. shared.env assigns TEST_CMD; the launching shell
    # already exports the byte-identical value, so the PRE/POST diff sees no
    # change and (pre-fix) omitted it. Under systemd — units pass only
    # AGENT_ENV + TARGET_DIR — the daemon then read TEST_CMD=None, the bash
    # gate checkers failed closed, and every push / `gh pr create` was denied
    # forever. Asserted end-to-end through the real loader, because "present in
    # the file text" is not the property that matters; "reaches the daemon" is.
    active = _compose(
        tmp_path,
        shared='TEST_CMD="uv run pytest -q"\n',
        overlay='REPO="acme/widget"\n',
        ambient={"TEST_CMD": "uv run pytest -q"},
    )
    assert "TEST_CMD" in _emitted(active)
    assert _load_agent_env({"AGENT_ENV": str(active)})["TEST_CMD"] == "uv run pytest -q"


def test_compose_env_file_value_wins_when_it_differs_from_the_ambient_shells(tmp_path):
    # Anti-vacuity twin: the fix must not degrade into "emit whatever the
    # ambient shell had". When the file disagrees with the environment, the
    # FILE wins — that is the whole point of an overlay.
    active = _compose(
        tmp_path,
        shared='TEST_CMD="uv run pytest -q"\n',
        overlay='REPO="acme/widget"\n',
        ambient={"TEST_CMD": "make check"},
    )
    assert _load_agent_env({"AGENT_ENV": str(active)})["TEST_CMD"] == "uv run pytest -q"


def test_compose_env_emits_parameter_expansion_default_with_the_ambient_value(tmp_path):
    # The `${VAR:-default}` idiom main.py's `_load_agent_env` docstring names as
    # the reason these files are shell syntax. With the var already exported the
    # assignment is a no-op — but the var IS config and must be emitted, with
    # the ambient value (not the fallback).
    active = _compose(
        tmp_path,
        shared='ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-fallback}"\n',
        overlay='REPO="acme/widget"\n',
        ambient={"ANTHROPIC_AUTH_TOKEN": "sk-ambient-real"},
    )
    env = _load_agent_env({"AGENT_ENV": str(active)})
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-ambient-real"


def test_compose_env_falls_back_when_the_expansion_default_actually_fires(tmp_path):
    # Anti-vacuity twin of the above: with nothing ambient, the documented
    # fallback is what lands.
    active = _compose(
        tmp_path,
        shared='ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-fallback}"\n',
        overlay='REPO="acme/widget"\n',
        ambient={"ANTHROPIC_AUTH_TOKEN": None},
    )
    assert _load_agent_env({"AGENT_ENV": str(active)})["ANTHROPIC_AUTH_TOKEN"] == "fallback"


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
        'unset GONE_LATER\n'
        # set INDIRECTLY, via a nested `source` (no assignment-shaped line in
        # either config file names it) — proves the PRE/POST diff arm of the
        # union is still load-bearing and was not replaced by the file scan.
        '. "$(dirname "${BASH_SOURCE[0]}")/nested.env"\n'
    )
    overlay = (
        'REPO="acme/widget"\n'
        'MERGE_METHOD="squash"\n'
        'CI_PENDING_MAX_SECONDS="3600"\n'
        'EMPTY_ON_PURPOSE=\n'
    )
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "nested.env").write_text('INDIRECT_FROM_NESTED_SOURCE="via-nested"\n')
    active = _compose(
        tmp_path, shared, overlay,
        ambient={
            "TEST_CMD": "uv run pytest -q",
            "ANTHROPIC_AUTH_TOKEN": "sk-ambient-real",
            "SOME_UNRELATED_AMBIENT_VAR": "should-not-leak",
        },
    )

    expected = set(_ASSIGN_RE.findall(shared)) | set(_ASSIGN_RE.findall(overlay))
    expected -= set(_UNSET_RE.findall(shared)) | set(_UNSET_RE.findall(overlay))
    expected |= {"INDIRECT_FROM_NESTED_SOURCE"}
    # compose-env appends the #63 scrub manifest naming every emitted knob.
    expected |= {"HARNESS_DEPLOY_KNOBS"}
    assert set(_emitted(active)) == expected

    env = _load_agent_env({"AGENT_ENV": str(active)})
    assert env["TEST_CMD"] == "uv run pytest -q"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-ambient-real"
    assert env["MERGE_METHOD"] == "squash"
    assert env["INDENTED_EXPORT"] == "yes"
    assert env["EMPTY_ON_PURPOSE"] == ""
    assert env["INDIRECT_FROM_NESTED_SOURCE"] == "via-nested"


def test_compose_env_emits_only_sourceable_identifier_names(tmp_path):
    # `set -a` exports FUNCTIONS as well as variables, so a helper defined in a
    # config file appears in `env -0` as `BASH_FUNC_name%%=() {...}`. Emitted
    # verbatim it made .active.env unsourceable — the line parses as a command
    # ("BASH_FUNC_name%%=...: command not found"), which is exactly the file
    # both `_load_agent_env` and the bash tools `source`. Assert the composed
    # file is clean AND that sourcing it is silent.
    active = _compose(
        tmp_path,
        shared='_helper() { HELPED="yes"; }\n_helper\nFIX_BOT="fix-bot"\n',
        overlay='REPO="acme/widget"\n',
    )
    assert not [k for k in _emitted(active) if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)]
    proc = subprocess.run(
        ["bash", "-c", 'set -a; source "$1"; set +a', "check", str(active)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""
    # The variable the function set is still config and still arrives.
    assert _load_agent_env({"AGENT_ENV": str(active)})["HELPED"] == "yes"


def test_compose_env_skips_commented_assignments_and_names_unset_after_assignment(tmp_path):
    # Focused twin of the set-equality test's negative half, so a failure here
    # points straight at the file-scan's anchor rather than at the whole set.
    active = _compose(
        tmp_path,
        shared=(
            '# COMMENTED_KNOB="nope"\n'
            '#TIGHT_COMMENT="nope"\n'
            '  # INDENTED_COMMENT="nope"\n'
            'GONE_LATER="temp"\n'
            'unset GONE_LATER\n'
            '  export INDENTED_EXPORT="yes"\n'
        ),
        overlay='REPO="acme/widget"\n',
        # The commented names are ALSO exported ambiently: if the scan ever
        # matched a comment, they would be emitted with these values, which is
        # the leak this asserts against.
        ambient={"COMMENTED_KNOB": "leaked", "TIGHT_COMMENT": "leaked"},
    )
    emitted = _emitted(active)
    assert "COMMENTED_KNOB" not in emitted
    assert "TIGHT_COMMENT" not in emitted
    assert "INDENTED_COMMENT" not in emitted
    assert "GONE_LATER" not in emitted
    assert emitted.keys() == {"INDENTED_EXPORT", "REPO", "HARNESS_DEPLOY_KNOBS"}
    # raw _emitted values keep %q escaping (backslash-space) — unescape first
    assert set(emitted["HARNESS_DEPLOY_KNOBS"].replace("\\", "").split()) == {"INDENTED_EXPORT", "REPO"}


def test_compose_env_scans_a_final_line_with_no_trailing_newline(tmp_path):
    # A hand-edited overlay whose last line has no "\n" is the shape most
    # likely to be silently half-scanned. The last knob must still be emitted
    # even though its value matches the ambient shell's (the P0R-1 condition).
    active = _compose(
        tmp_path,
        shared='A_KNOB="one"\nB_KNOB="two"',  # deliberately unterminated
        overlay='REPO="acme/widget"',          # deliberately unterminated
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
    env_file.write_text(
        'REPO="o/r"\nFIX_BOT="fixer"\n'
        'CI_PENDING_MAX_SECONDS="3600"\nMAX_INFRA_DEATHS="9"\n'
    )
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.delenv("CI_PENDING_MAX_SECONDS", raising=False)
    monkeypatch.delenv("MAX_INFRA_DEATHS", raising=False)
    # Avoid a real flock/tempfile side effect — the single-instance guard is
    # not what this test is about.
    monkeypatch.setattr(
        main_module, "acquire", lambda role, repo, **_scope: object()
    )

    stop = RuntimeError("stop-before-build-role")

    def _boom(role, env, _scheduler_progress=None):
        raise stop

    monkeypatch.setattr(main_module, "build_role", _boom)
    monkeypatch.setattr(
        main_module,
        "_recover_pre_adapter_processes",
        lambda *_args: (),
    )

    with pytest.raises(RuntimeError):
        main_module.main(["fix"])

    assert os.environ["CI_PENDING_MAX_SECONDS"] == "3600"
    assert os.environ["MAX_INFRA_DEATHS"] == "9"


def test_build_role_does_not_reload_env_when_already_provided(
    tmp_path, monkeypatch
):
    # review S7: `_load_agent_env` shells out to `bash -c ... env -0`.
    # build_role used to call it unconditionally
    # (`_load_agent_env(env if env is not None else dict(os.environ))`), so
    # main()'s already-loaded dict was reloaded a second time on every boot.
    # Drive the REAL build_role (not mocked) with an env dict provided, and
    # fail loudly if it reaches _load_agent_env at all.
    def _must_not_be_called(env):  # pragma: no cover - only runs on regression
        raise AssertionError("build_role reloaded env via _load_agent_env")

    monkeypatch.setattr(main_module, "_load_agent_env", _must_not_be_called)
    monkeypatch.setattr(
        "issue_agent.app.config.machine_control_authority_root",
        lambda: str(tmp_path / "machine-state"),
    )

    # FIX_BOT is required as of review P1R-7 (an unset one made every
    # unresolvable PR author read as ours), so a config built here must carry it.
    state = tmp_path / "state"
    daemon = main_module.build_role(
        "fix",
        {
            "REPO": "o/r",
            "FIX_BOT": "fix-bot",

            "STATE_DIR": str(state / "fix"),
            "REVIEW_STATE_DIR": str(state / "review"),
            "STEWARD_STATE_DIR": str(state / "steward"),
        },
    )
    assert daemon is not None


def test_world_probe_preserves_fix_daemon_dispatch_identity(monkeypatch, tmp_path):
    from issue_agent.app.loop import Daemon
    from issue_agent.domain.world import WorldState

    built = {}

    def build_fix(_cfg, gateway, _store, _worktrees, _runner):
        daemon = Daemon(
            "fix",
            lambda: WorldState(),
            lambda _world: [],
            Executor(gateway),
            dispatch_capacity=2,
        )
        built["daemon"] = daemon
        return daemon

    monkeypatch.setattr(main_module, "build_fix_daemon", build_fix)

    daemon = main_module.build_role("fix", {
        "REPO": "o/r",
        "FIX_BOT": "fix-bot",

        "WORLD_PROBE": "1",
        "STATE_DIR": str(tmp_path),
    })

    assert daemon is built["daemon"]
    assert daemon.available_dispatch_slots == 2


def test_review_role_runtime_tags_persisted_sessions(monkeypatch, tmp_path):
    """A Codex upgrade must not resume a legacy Claude review session."""
    captured = {}

    class RecordingStore:
        def __init__(self, state_dir, runtime="claude"):
            captured.update(state_dir=state_dir, runtime=runtime)

    monkeypatch.setattr(main_module, "Store", RecordingStore)
    monkeypatch.setattr(
        main_module, "build_review_daemon", lambda _cfg, _gw, store, _runner: store
    )

    store = main_module.build_role("review", {
        "REPO": "o/r",
        "FIX_BOT": "fix-bot",
        "REVIEWER_RUNTIME": "codex",
        "REVIEW_STATE_DIR": str(tmp_path),
        "WORLD_PROBE": "0",
    })

    assert store is not None
    assert captured == {"state_dir": str(tmp_path), "runtime": "codex"}


def test_build_role_self_loads_env_when_called_standalone_without_one(
    tmp_path, monkeypatch
):
    # Anti-vacuity twin: a standalone caller that does NOT already have a
    # loaded env (env=None, the documented default) must still get AGENT_ENV
    # applied — the "load once" fix must not silently drop loading entirely.
    calls = []
    real = main_module._load_agent_env

    def _counting(env):
        calls.append(1)
        return real(env)

    monkeypatch.setattr(main_module, "_load_agent_env", _counting)
    monkeypatch.setattr(
        "issue_agent.app.config.machine_control_authority_root",
        lambda: str(tmp_path / "machine-state"),
    )
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("FIX_BOT", "fix-bot")  # required since review P1R-7
    state = tmp_path / "state"
    monkeypatch.setenv("STATE_DIR", str(state / "fix"))
    monkeypatch.setenv("REVIEW_STATE_DIR", str(state / "review"))
    monkeypatch.setenv("STEWARD_STATE_DIR", str(state / "steward"))
    monkeypatch.delenv("AGENT_ENV", raising=False)

    daemon = main_module.build_role("fix")
    assert daemon is not None
    assert len(calls) == 1


# ── D7: doctor state-dir collision check ───────────────────────────────────────
def test_doctor_fails_on_two_overlays_sharing_a_state_dir(tmp_path):
    fake_bin = _fake_tool_path(tmp_path)
    harness = tmp_path / "harness"
    repos_dir = harness / "config" / "repos"
    repos_dir.mkdir(parents=True)
    (repos_dir / "repo-a.env").write_text('REPO="acme/widget"\n')
    (repos_dir / "repo-b.env").write_text(
        'REPO="other/thing"\nSTATE_DIR="/custom/shared-state"\n'
    )
    (repos_dir / "repo-c.env").write_text(
        'REPO="third/repo"\nSTATE_DIR="/custom/shared-state"\n'
    )

    result = _run_doctor({
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HARNESS_ROOT": str(harness),
    })
    assert result.returncode != 0
    # "state-dir collision:" (with the colon) is the bad() finding line —
    # distinct from the "== per-repo state-dir collisions (...) ==" section
    # header, which contains the same words but is not itself a failure.
    assert "state-dir collision:" in result.stdout
    assert "repo-b" in result.stdout and "repo-c" in result.stdout


def test_doctor_passes_when_overlays_have_distinct_state_dirs(tmp_path):
    # Anti-vacuity twin: distinct overlays (default per-repo dirs, which
    # differ because REPO differs) must not be flagged.
    fake_bin = _fake_tool_path(tmp_path)
    harness = tmp_path / "harness"
    repos_dir = harness / "config" / "repos"
    repos_dir.mkdir(parents=True)
    (repos_dir / "repo-a.env").write_text('REPO="acme/widget"\n')
    (repos_dir / "repo-b.env").write_text('REPO="other/thing"\n')

    result = _run_doctor({
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HARNESS_ROOT": str(harness),
    })
    assert "state-dir collision:" not in result.stdout
    assert "no state-dir collisions across 2 overlay(s)" in result.stdout


def test_doctor_state_dir_scan_ignores_active_overlay_environment(tmp_path):
    """The scan must compose each overlay from files, not the active config.

    ``doctor.sh`` is normally invoked after point-at has sourced the active
    overlay. An older overlay that omits explicit state directories must still
    receive its repository-derived defaults rather than inherit the active
    deployment's exported values.
    """
    fake_bin = _fake_tool_path(tmp_path)
    harness = tmp_path / "harness"
    repos_dir = harness / "config" / "repos"
    repos_dir.mkdir(parents=True)
    (repos_dir / "older.env").write_text('REPO="acme/widget"\n')
    (repos_dir / "active.env").write_text(
        'REPO="other/thing"\n'
        'STATE_DIR="/active/fix"\n'
        'REVIEW_STATE_DIR="/active/review"\n'
        'STEWARD_STATE_DIR="/active/steward"\n'
    )

    result = _run_doctor({
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HARNESS_ROOT": str(harness),
        "REPO": "other/thing",
        "STATE_DIR": "/active/fix",
        "REVIEW_STATE_DIR": "/active/review",
        "STEWARD_STATE_DIR": "/active/steward",
    })

    assert "state-dir collision:" not in result.stdout
    assert "no state-dir collisions across 2 overlay(s)" in result.stdout


def test_doctor_skips_state_dir_check_with_no_overlays(tmp_path):
    fake_bin = _fake_tool_path(tmp_path)
    harness = tmp_path / "harness"
    (harness / "config" / "repos").mkdir(parents=True)

    result = _run_doctor({
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HARNESS_ROOT": str(harness),
    })
    assert "nothing to check" in result.stdout


# ── D7 + C1: doctor identity-distinctness normalization ────────────────────────
def test_doctor_rejects_bot_suffix_mismatch_under_identity_mode_three(tmp_path):
    # review C1: acme-fixer and acme-fixer[bot] are the SAME GitHub identity in
    # two
    # API surface forms — a raw string compare (the old check) passed this,
    # and GitHub then 422s the self-approval on every PR, forever.
    fake_bin = _fake_tool_path(tmp_path)
    result = _run_doctor({
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "IDENTITY_MODE": "three",
        "FIX_BOT": "acme-fixer",
        "REVIEW_BOT": "acme-fixer[bot]",
        "STEWARD_BOT": "acme-steward",
        "REPO": "o/r",
    })
    assert result.returncode != 0
    assert "IDENTITY_MODE=three needs" in result.stdout


def test_doctor_accepts_distinct_identities_under_identity_mode_three(tmp_path):
    # Anti-vacuity twin: genuinely distinct identities must still pass.
    fake_bin = _fake_tool_path(tmp_path)
    result = _run_doctor({
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "IDENTITY_MODE": "three",
        "FIX_BOT": "acme-fixer",
        "REVIEW_BOT": "acme-reviewer",
        "STEWARD_BOT": "acme-steward",
        "REPO": "o/r",
    })
    assert "three distinct identities" in result.stdout


def test_doctor_case_insensitive_bot_suffix_mismatch(tmp_path):
    # normalize_login casefolds first, then strips — a differently-cased
    # [BOT] suffix must be caught too.
    fake_bin = _fake_tool_path(tmp_path)
    result = _run_doctor({
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "IDENTITY_MODE": "three",
        "FIX_BOT": "Acme-Fixer",
        "REVIEW_BOT": "acme-fixer[BOT]",
        "STEWARD_BOT": "acme-steward",
        "REPO": "o/r",
    })
    assert result.returncode != 0
    assert "IDENTITY_MODE=three needs" in result.stdout


def test_two_deployments_compose_to_separate_files(tmp_path):
    """Concurrent multi-repo deployments: composing repo B must not overwrite
    repo A's snapshot (the old fixed config/.active.env did exactly that, and
    the mtime hot-reload watch then re-pointed A's live daemons at B)."""
    compose_env_sh = _install_compose_env(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "shared.env").write_text("SHARED_KNOB=x\n")
    repos_dir = tmp_path / "config" / "repos"
    repos_dir.mkdir(exist_ok=True)
    (repos_dir / "alpha.env").write_text("REPO=acme/alpha\n")
    (repos_dir / "beta.env").write_text("REPO=acme/beta\n")

    for name in ("alpha", "beta"):
        subprocess.run(
            ["bash", str(compose_env_sh), name],
            check=True, capture_output=True, text=True,
        )

    alpha = tmp_path / "config" / ".active.alpha.env"
    beta = tmp_path / "config" / ".active.beta.env"
    assert "REPO=acme/alpha" in alpha.read_text()
    assert "REPO=acme/beta" in beta.read_text()
    assert not (tmp_path / "config" / ".active.env").exists()


# ── CI-on-base-branch preflight (keifu deployment, 2026-07-28) ─────────────────
# Three failures found by hand on the xnmp/keifu preflight, none of which any
# existing check could see:
#
#   * CI triggered on `main` while every PR targeted `chong-dev`, so no PR had
#     ever received a check. `ci_state` returns CIState.NONE for a PR with no
#     checks and domain/fix.py:315/:340 treat NONE identically to OK — approved
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
def _fake_ci_tools(
    tmp_path: Path,
    *,
    workflows: int = 1,
    prs: list | None = None,
    allow_squash: bool = True,
) -> Path:
    """PATH dir whose `gh` answers the CI section's three queries from fixture
    data. Everything else behaves like _fake_tool_path's stub."""
    fake_bin = tmp_path / "fake_ci_bin"
    fake_bin.mkdir()
    pr_json = json.dumps(prs if prs is not None else [])
    gh = f"""#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "gh version 0.0.0-fake"; exit 0; fi
if [ "$1" = "api" ] && [ "$2" = "user" ]; then echo "fake-user"; exit 0; fi
if [ "$1" = "repo" ] && [ "$2" = "view" ]; then exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "list" ]; then cat <<'JSON'
{pr_json}
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
    (fake_bin / "gh").write_text(gh)
    for name, body in {
        "claude": '#!/usr/bin/env bash\necho "claude 0.0.0-fake"\n',
        "uv": (
            '#!/usr/bin/env bash\n'
            'if [ "$1" = "--version" ]; then echo "uv 0.0.0-fake"; exit 0; fi\n'
            'exit 2\n'
        ),
    }.items():
        (fake_bin / name).write_text(body)
    for p in fake_bin.iterdir():
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake_bin


def _ci_doctor(tmp_path: Path, extra_env: dict, **fixture) -> subprocess.CompletedProcess:
    fake_bin = _fake_ci_tools(tmp_path, **fixture)
    harness = tmp_path / "harness"
    (harness / "config" / "repos").mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "HARNESS_ROOT": str(harness),
        "REPO": "acme/widget",
        "BASE_BRANCH": "trunk",
        "FIX_BOT": "acme-fixer",
        "REVIEW_BOT": "acme-review",
        "STEWARD_BOT": "acme-review",
    }
    env.update(extra_env)
    return _run_doctor(env)


def _pr(number: int, checks: list | None) -> dict:
    return {"number": number, "statusCheckRollup": checks}


def test_doctor_errors_when_no_pr_into_base_ever_got_a_check(tmp_path):
    """The keifu shape: active workflows exist, but they trigger on a different
    branch, so every PR into the base reads CIState.NONE — which the harness
    merges on. Must be an ERROR, not a warning: it silently removes CI from the
    merge gate entirely."""
    result = _ci_doctor(
        tmp_path, {},
        workflows=2,
        prs=[_pr(9, []), _pr(8, []), _pr(7, None)],
    )
    assert result.returncode != 0
    assert "NOT ONE of the last 3 PR(s) into 'trunk' received a single check" in result.stdout
    # It must name the consequence, not just the symptom — an operator who does
    # not know NONE is treated as OK will read a checkless PR as harmless.
    assert "CIState.NONE" in result.stdout


def test_doctor_accepts_a_base_branch_whose_prs_do_get_checks(tmp_path):
    """Anti-vacuity twin: the same code path must stay quiet on a healthy repo."""
    result = _ci_doctor(
        tmp_path, {},
        workflows=1,
        prs=[_pr(9, [{"name": "Test", "conclusion": "SUCCESS"}])],
    )
    assert "NOT ONE of the last" not in result.stdout
    assert "PRs into 'trunk' receive checks (1 of the last 1 sampled)" in result.stdout








def test_doctor_errors_when_the_repo_forbids_the_configured_merge_method(tmp_path):
    result = _ci_doctor(
        tmp_path, {"MERGE_METHOD": "squash"},
        workflows=1,
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
