"""Env-compose pipeline + doctor preflight — review C5 / D3 / D7.

C5(a): bin/compose-env.sh used to emit a hand-maintained allow-list that
omitted ~18 documented knobs, so a systemd deployment silently dropped
operator config. It now snapshots the environment before/after sourcing
shared.env + the per-repo overlay and emits the diff — every var either file
defined, with no allow-list to fall out of sync.

C5(b): nothing exported the composed config into os.environ, so under systemd
(unit files set only AGENT_ENV + TARGET_DIR) every subprocess spawned
downstream (bash gate checkers, git, gh, codex — none of which pass `env=`
explicitly) inherited an empty environment: the ALLOW_UNTESTED escape hatch
became unreachable and operator knobs like CI_IGNORE_CHECKS were silently
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

import os
import stat
import subprocess
from pathlib import Path

import pytest

from issue_agent.app import main as main_module
from issue_agent.app.main import _load_agent_env

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
    test always exercises the shipped script."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dest = bin_dir / "compose-env.sh"
    dest.write_text(COMPOSE_ENV_SH.read_text())
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    return dest


# ── C5(a) + acceptance pipeline (wave centerpiece) ─────────────────────────────
def test_compose_env_pipeline_carries_knobs_outside_old_allow_list_to_os_environ(
    tmp_path, monkeypatch
):
    # review C5: end-to-end — shared.env + overlay -> compose-env.sh ->
    # .active.env -> _load_agent_env -> (simulated main.py) os.environ. Uses
    # CI_IGNORE_CHECKS (documented, omitted from the old allow-list) and
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
        'CI_IGNORE_CHECKS="Format.*"\n'
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

    active = tmp_path / "config" / ".active.env"
    active_text = active.read_text()
    assert "CI_IGNORE_CHECKS=" in active_text
    assert "MAX_INFRA_DEATHS=7" in active_text
    assert "REPO=acme/widget" in active_text

    env = _load_agent_env({"AGENT_ENV": str(active)})
    assert env["CI_IGNORE_CHECKS"] == "Format.*"
    assert env["MAX_INFRA_DEATHS"] == "7"
    assert env["WEIRD_VALUE"] == 'has spaces and "quotes" and $dollar signs'
    assert env["REPO"] == "acme/widget"

    # review C5(b), simulated: main.py applies this exact dict with
    # os.environ.update(env) before building anything. monkeypatch.setenv
    # undoes this automatically at teardown.
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert os.environ["CI_IGNORE_CHECKS"] == "Format.*"
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
    active_text = (tmp_path / "config" / ".active.env").read_text()
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
    active_text = (tmp_path / "config" / ".active.env").read_text()
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


# ── S7/D3: main.py delivers env to subprocesses + loads it once ───────────────
def test_main_exports_composed_env_into_os_environ_before_building(tmp_path, monkeypatch):
    # review C5(b), driven through the REAL main() code path (not simulated):
    # build_role is stubbed to raise immediately so nothing past the export
    # runs (no real GitHub/worktree/adapter construction), but os.environ.
    # update(env) — the line under test — executes for real.
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO="o/r"\nCI_IGNORE_CHECKS="Format.*"\nMAX_INFRA_DEATHS="9"\n')
    monkeypatch.setenv("AGENT_ENV", str(env_file))
    monkeypatch.delenv("CI_IGNORE_CHECKS", raising=False)
    monkeypatch.delenv("MAX_INFRA_DEATHS", raising=False)
    # Avoid a real flock/tempfile side effect — the single-instance guard is
    # not what this test is about.
    monkeypatch.setattr(main_module, "acquire", lambda role, repo: object())

    stop = RuntimeError("stop-before-build-role")

    def _boom(role, env):
        raise stop

    monkeypatch.setattr(main_module, "build_role", _boom)

    with pytest.raises(RuntimeError):
        main_module.main(["fix"])

    assert os.environ["CI_IGNORE_CHECKS"] == "Format.*"
    assert os.environ["MAX_INFRA_DEATHS"] == "9"


def test_build_role_does_not_reload_env_when_already_provided(monkeypatch):
    # review S7: `_load_agent_env` shells out to `bash -c ... env -0`.
    # build_role used to call it unconditionally
    # (`_load_agent_env(env if env is not None else dict(os.environ))`), so
    # main()'s already-loaded dict was reloaded a second time on every boot.
    # Drive the REAL build_role (not mocked) with an env dict provided, and
    # fail loudly if it reaches _load_agent_env at all.
    def _must_not_be_called(env):  # pragma: no cover - only runs on regression
        raise AssertionError("build_role reloaded env via _load_agent_env")

    monkeypatch.setattr(main_module, "_load_agent_env", _must_not_be_called)

    daemon = main_module.build_role("fix", {"REPO": "o/r"})
    assert daemon is not None


def test_build_role_self_loads_env_when_called_standalone_without_one(monkeypatch):
    # Anti-vacuity twin: a standalone caller that does NOT already have a
    # loaded env (env=None, the documented default) must still get AGENT_ENV
    # applied — the "load once" fix must not silently drop loading entirely.
    calls = []
    real = main_module._load_agent_env

    def _counting(env):
        calls.append(1)
        return real(env)

    monkeypatch.setattr(main_module, "_load_agent_env", _counting)
    monkeypatch.setenv("REPO", "o/r")
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
    # review C1: fix-bot and fix-bot[bot] are the SAME GitHub identity in two
    # API surface forms — a raw string compare (the old check) passed this,
    # and GitHub then 422s the self-approval on every PR, forever.
    fake_bin = _fake_tool_path(tmp_path)
    result = _run_doctor({
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "IDENTITY_MODE": "three",
        "FIX_BOT": "fix-bot",
        "REVIEW_BOT": "fix-bot[bot]",
        "STEWARD_BOT": "steward-bot",
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
        "FIX_BOT": "fix-bot",
        "REVIEW_BOT": "review-bot",
        "STEWARD_BOT": "steward-bot",
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
        "FIX_BOT": "Fix-Bot",
        "REVIEW_BOT": "fix-bot[BOT]",
        "STEWARD_BOT": "steward-bot",
        "REPO": "o/r",
    })
    assert result.returncode != 0
    assert "IDENTITY_MODE=three needs" in result.stdout
