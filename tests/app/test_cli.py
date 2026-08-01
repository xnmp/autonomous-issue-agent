"""CLI pure helpers — repo detection parsing, overlay-name resolution, and the
synthesized per-repo env. No git/network: every helper here is deterministic.

The key invariant: synth_repo_env's keys must round-trip through config.from_env
(the daemon's reader), so the CLI and the daemon can't drift on key names."""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_agent.app import cli


from issue_agent.app.config import from_env


def test_overlay_reviewer_runtime_overrides_and_scrubs_ambient_value(tmp_path, monkeypatch):
    overlay = tmp_path / "app.env"
    overlay.write_text('REPO="o/r"\nREVIEWER_RUNTIME="claude"\n')
    monkeypatch.setenv("REVIEWER_RUNTIME", "codex")

    runtime = cli.read_repo_env_field(str(overlay), "REVIEWER_RUNTIME")

    assert runtime == "claude"
    assert from_env({"REPO": "o/r", "REVIEWER_RUNTIME": runtime}).reviewer_runtime == "claude"

    overlay.write_text('REPO="o/r"\n')
    assert cli.read_repo_env_field(str(overlay), "REVIEWER_RUNTIME") == ""


# ── parse_remote_url ──────────────────────────────────────────────────────────
def test_dashboard_reports_a_stale_frontend_bundle_before_falling_back(
    monkeypatch, capsys
):
    from issue_agent.app import dashboard

    args = type(
        "Args",
        (),
        {
            "names": ["demo"],
            "html": None,
            "text": False,
            "serve": 8321,
            "details": False,
            "stale_minutes": 5.0,
        },
    )()
    monkeypatch.setattr(
        cli,
        "_prepare_dashweb",
        lambda: cli.DashwebPreparation(
            False, "automatic dashboard rebuild failed with exit 2", True
        ),
    )
    monkeypatch.setattr(
        dashboard,
        "serve",
        lambda _port, _render: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert cli.cmd_dashboard(args) == 0
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "automatic dashboard rebuild failed with exit 2" in combined
    assert "make dashboard-ui" in combined


def test_prepare_dashboard_rebuilds_from_harness_root_and_revalidates(
    tmp_path, monkeypatch
):
    from issue_agent.adapters.dashweb import bundle

    harness = str(tmp_path / "harness with spaces")
    target = tmp_path / "target-repo"
    target.mkdir()
    states = iter(
        (
            bundle.BundleStatus("stale", "src/App.tsx changed"),
            bundle.BundleStatus("ready", "validated"),
        )
    )
    monkeypatch.chdir(target)
    monkeypatch.setattr(cli, "HARNESS_ROOT", harness)
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr(bundle, "bundle_status", lambda _root: next(states))
    calls = []
    monkeypatch.setattr(cli, "_run_dashboard_build", lambda: calls.append(True) or 0)

    result = cli._prepare_dashweb()

    assert result.ready
    assert result.rebuild_attempted
    assert calls == [True]


def test_prepare_dashboard_falls_back_when_rebuild_does_not_validate(
    tmp_path, monkeypatch
):
    from issue_agent.adapters.dashweb import bundle

    states = iter(
        (
            bundle.BundleStatus("stale", "src/App.tsx changed"),
            bundle.BundleStatus("stale", "assets/app.js missing"),
        )
    )
    monkeypatch.setattr(cli, "HARNESS_ROOT", str(tmp_path))
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr(bundle, "bundle_status", lambda _root: next(states))
    monkeypatch.setattr(cli, "_run_dashboard_build", lambda: 0)

    result = cli._prepare_dashweb()

    assert not result.ready
    assert result.rebuild_attempted
    assert "produced an invalid bundle" in result.detail
    assert "assets/app.js missing" in result.detail


def test_prepare_dashboard_falls_back_when_rebuild_times_out(tmp_path, monkeypatch):
    from issue_agent.adapters.dashweb import bundle

    states = iter(
        (
            bundle.BundleStatus("stale", "src/App.tsx changed"),
            bundle.BundleStatus("stale", "still stale"),
        )
    )
    monkeypatch.setattr(cli, "HARNESS_ROOT", str(tmp_path))
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr(bundle, "bundle_status", lambda _root: next(states))
    monkeypatch.setattr(cli, "_run_dashboard_build", lambda: 124)

    result = cli._prepare_dashweb()

    assert not result.ready
    assert result.rebuild_attempted
    assert result.detail == "automatic dashboard rebuild timed out"


def test_dashboard_rebuild_timeout_terminates_the_process_group(monkeypatch):
    class HungBuild:
        pid = 4321

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("make", timeout)
            return -15

        def poll(self):
            return None

    process = HungBuild()
    popen_calls = []
    signals = []
    monkeypatch.setattr(
        cli.subprocess,
        "Popen",
        lambda argv, start_new_session: popen_calls.append((argv, start_new_session))
        or process,
    )
    monkeypatch.setattr(cli.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    assert cli._run_dashboard_build() == 124
    assert popen_calls == [
        (["make", "-C", cli.HARNESS_ROOT, "dashboard-ui"], True)
    ]
    assert signals == [(process.pid, cli.signal.SIGTERM)]


def test_dashboard_recovery_command_is_absolute_and_shell_safe():
    assert cli.dashboard_build_command("/tmp/harness with spaces") == (
        "cd '/tmp/harness with spaces' && uv sync --extra sdk --extra dash && "
        "make dashboard-ui"
    )


def test_parse_remote_url_ssh():
    assert cli.parse_remote_url("git@github.com:acme/MyApp.git") == "acme/myapp"


def test_parse_remote_url_https_and_trailing_git():
    assert cli.parse_remote_url("https://github.com/acme/MyApp.git") == "acme/myapp"
    assert cli.parse_remote_url("https://github.com/acme/MyApp") == "acme/myapp"


def test_parse_remote_url_https_trailing_slash():
    assert cli.parse_remote_url("https://github.example.com/o/r/") == "o/r"


def test_parse_remote_url_garbage_is_empty():
    assert cli.parse_remote_url("not a url") == ""
    assert cli.parse_remote_url("") == ""


# ── derive_name ───────────────────────────────────────────────────────────────
def test_derive_name_lowercases_basename():
    # P0R-2: owner is now part of the derived name (was basename-only).
    assert cli.derive_name("acme/MyApp-combined-agent") == "acme-myapp-combined-agent"


def test_derive_name_sanitizes_and_falls_back():
    assert cli.derive_name("") == "target"
    assert cli.derive_name("o/weird name!") == "o-weird-name"


@pytest.mark.parametrize(
    ("alias", "canonical"),
    (
        ("Acme App", "acme-app"),
        ("acme@app", "acme-app"),
        ("ACME-APP", "acme-app"),
    ),
)
def test_explicit_deployment_name_aliases_are_rejected(
    alias,
    canonical,
):
    with pytest.raises(ValueError, match=repr(canonical)):
        cli.resolve_name("acme/app", explicit=alias)
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(
            ["install-service", "--name", alias]
        )
    assert exc.value.code == 2


# ── P0R-2: derive_name includes the owner (collision fix) ─────────────────────
def test_derive_name_includes_owner():
    assert cli.derive_name("acme/app") == "acme-app"


def test_derive_name_no_collision_across_owners():
    """The original bug: acme/app and other/app both derived the bare
    basename "app" — resolve_name could then reuse ONE owner's overlay while
    running against the OTHER's checkout. Two different owners of the same
    repo BASENAME must now derive DIFFERENT names."""
    a = cli.derive_name("acme/app")
    b = cli.derive_name("other/app")
    assert a != b
    assert {a, b} == {"acme-app", "other-app"}


# ── read_repo_env_field + resolve_name ────────────────────────────────────────
def test_read_repo_env_field_last_assignment_wins(tmp_path):
    p = tmp_path / "r.env"
    p.write_text('REPO="a/one"\nexport REPO="a/two"\nOTHER=x\n')
    assert cli.read_repo_env_field(str(p), "REPO") == "a/two"


# ── review M12: shell-semantics reader replaces the regex line-scanner ────────
# The prior regex (`^FIELD=(.*)$`) silently mis-parsed each of these — the
# repro table from the review — so resolve_name MISSED an existing overlay
# and cmd_start synthesized a duplicate with fresh *_STATE_DIRs.
def test_read_repo_env_field_survives_trailing_comment(tmp_path):
    p = tmp_path / "r.env"
    p.write_text('REPO="owner/name"  # prod\n')
    assert cli.read_repo_env_field(str(p), "REPO") == "owner/name"


def test_read_repo_env_field_survives_spaces_around_equals(tmp_path):
    p = tmp_path / "r.env"
    p.write_text("REPO = owner/name\n")
    assert cli.read_repo_env_field(str(p), "REPO") == "owner/name"


def test_read_repo_env_field_expands_var_reference(tmp_path):
    p = tmp_path / "r.env"
    p.write_text("OWNER_REPO=owner/name\nREPO=${OWNER_REPO}\n")
    assert cli.read_repo_env_field(str(p), "REPO") == "owner/name"


# Anti-vacuity twins: a reader that always returns something regardless of
# input (or one that leaks the CLI process's own environment) would pass the
# three tests above for the wrong reason.
def test_read_repo_env_field_missing_field_is_empty(tmp_path):
    p = tmp_path / "r.env"
    p.write_text('OTHER="x"\n')
    assert cli.read_repo_env_field(str(p), "REPO") == ""


def test_read_repo_env_field_missing_file_is_empty():
    assert cli.read_repo_env_field("/no/such/file.env", "REPO") == ""


def test_read_repo_env_field_does_not_leak_ambient_env(tmp_path, monkeypatch):
    """A field the FILE does not set must read as unset even if the CLI
    process's own environment happens to carry a same-named var (e.g. an
    operator's shell that `source`d an overlay by hand) — otherwise
    resolve_name could match an overlay by accident."""
    monkeypatch.setenv("REPO", "ambient/leak")
    p = tmp_path / "r.env"
    p.write_text('OTHER="x"\n')
    assert cli.read_repo_env_field(str(p), "REPO") == ""


def test_resolve_name_matches_overlay_despite_trailing_comment(tmp_path):
    (tmp_path / "myapp.env").write_text('REPO="acme/App"  # prod\n')
    assert cli.resolve_name("ACME/app", repos_dir=str(tmp_path)) == "myapp"


def test_resolve_name_matches_overlay_with_spaced_equals(tmp_path):
    (tmp_path / "myapp.env").write_text("REPO = acme/App\n")
    assert cli.resolve_name("acme/App", repos_dir=str(tmp_path)) == "myapp"


def test_resolve_name_matches_overlay_with_var_expansion(tmp_path):
    (tmp_path / "myapp.env").write_text("OWNER_REPO=acme/App\nREPO=${OWNER_REPO}\n")
    assert cli.resolve_name("acme/App", repos_dir=str(tmp_path)) == "myapp"


def test_resolve_name_warns_before_synthesizing_duplicate(tmp_path, capsys):
    """M12: an overlay already targets this exact working copy but its REPO
    field disagrees (rename, or a still-mismatched parse) — resolve_name must
    warn loudly (stderr) before falling through to derive_name, which would
    synthesize a SECOND overlay (and a second STATE_DIR) for the same
    checkout."""
    (tmp_path / "myapp.env").write_text('REPO="acme/OldName"\nTARGET_DIR="/repos/app"\n')
    name = cli.resolve_name("acme/NewName", repos_dir=str(tmp_path), target_dir="/repos/app")
    assert name == cli.derive_name("acme/NewName")  # still falls through to derive_name
    captured = capsys.readouterr()
    assert "myapp" in captured.err
    assert "synthesize" in captured.err.lower()


def test_resolve_name_no_warning_when_no_target_dir_collision(tmp_path, capsys):
    """Anti-vacuity twin: an overlay pointed at a DIFFERENT working copy must
    not trigger the warning."""
    (tmp_path / "myapp.env").write_text('REPO="acme/OldName"\nTARGET_DIR="/repos/other"\n')
    name = cli.resolve_name("acme/NewName", repos_dir=str(tmp_path), target_dir="/repos/app")
    assert name == cli.derive_name("acme/NewName")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_resolve_name_explicit_wins(tmp_path):
    assert cli.resolve_name("o/r", explicit="custom", repos_dir=str(tmp_path)) == "custom"


def test_resolve_name_reuses_matching_overlay(tmp_path):
    (tmp_path / "myapp.env").write_text('REPO="acme/MyApp-combined-agent"\n')
    (tmp_path / "other.env").write_text('REPO="x/y"\n')
    name = cli.resolve_name("acme/MyApp-combined-agent", repos_dir=str(tmp_path))
    assert name == "myapp"


def test_resolve_name_derives_when_no_match(tmp_path):
    # P0R-2: derived name now includes the owner (was "new-repo").
    name = cli.resolve_name("acme/New-Repo", repos_dir=str(tmp_path))
    assert name == "acme-new-repo"


# ── P0R-2: backward compat — reuse is by CONTENT (REPO field), not by
# recomputing derive_name's (now-changed) shape against an existing filename ──
def test_resolve_name_backcompat_old_basename_overlay_still_reused(tmp_path):
    """An overlay named under the OLD basename-only convention (e.g. hand
    authored, or synthesized before this fix) must still be found and reused
    for the SAME repo — resolve_name matches by reading the overlay's actual
    REPO field, never by recomputing derive_name against it. Only a NEWLY
    synthesized name (no existing overlay matches) uses the new owner-
    inclusive shape."""
    (tmp_path / "app.env").write_text('REPO="acme/app"\n')
    # Sanity: the NEW derive_name would NOT reproduce this filename anymore —
    # proves the reuse below is genuinely content-based, not a coincidence.
    assert cli.derive_name("acme/app") != "app"
    assert cli.resolve_name("acme/app", repos_dir=str(tmp_path)) == "app"


def test_resolve_name_prefers_target_dir_match(tmp_path):
    """Regression: two overlays share a REPO but point at different working copies.
    The launched-from dir (target_dir) must decide — NOT the alphabetically-first
    match. `-wt` sorts before the primary name, so alpha-only picked the wrong one
    and silently ran against the wrong TARGET_DIR."""
    repo = "acme/App-combined-agent"
    (tmp_path / "app-wt.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app-harness"\n')
    (tmp_path / "app.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app"\n')
    # Launched from the primary checkout → the primary overlay, not the alpha-first -wt.
    assert cli.resolve_name(repo, repos_dir=str(tmp_path), target_dir="/repos/app") == "app"
    # Launched from the worktree copy → the -wt overlay.
    assert cli.resolve_name(repo, repos_dir=str(tmp_path), target_dir="/repos/app-harness") == "app-wt"


def test_resolve_name_target_dir_realpath_normalized(tmp_path):
    """Trailing slash / `.` segment variants of the same dir still match."""
    repo = "acme/App"
    (tmp_path / "app.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app"\n')
    assert cli.resolve_name(repo, repos_dir=str(tmp_path), target_dir="/repos/app/") == "app"
    assert cli.resolve_name(repo, repos_dir=str(tmp_path), target_dir="/repos/./app") == "app"


def test_resolve_name_does_not_reuse_same_repo_from_another_checkout(
    tmp_path, capsys
):
    """A known checkout is a deployment identity axis, not a hint."""
    repo = "acme/App-combined-agent"
    (tmp_path / "app-wt.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app-harness"\n')
    (tmp_path / "app.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app"\n')
    assert cli.resolve_name(
        repo, repos_dir=str(tmp_path), target_dir="/somewhere/else"
    ) == cli.derive_name(repo)
    assert "different working copy" in capsys.readouterr().err


# ── synth_repo_env round-trips through the daemon's reader ─────────────────────
def _source_env(text: str) -> dict:
    """Parse a synthesized overlay the way from_env consumes it: only the simple
    KEY=value lines the CLI emits (single-quoted or "$HOME/…"). We resolve the
    subset config.from_env actually reads. This mirrors compose-env.sh sourcing
    without spawning bash."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        elif val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        env[key] = val
    return env


def test_synth_repo_env_minimal_keys():
    text = cli.synth_repo_env(
        repo="acme/MyApp", target_dir="/repos/myapp", name="myapp"
    )
    env = _source_env(text)
    assert env["REPO"] == "acme/myapp"
    assert env["TARGET_DIR"] == "/repos/myapp"
    assert env["BASE_BRANCH"] == "dev"
    assert "issue-agent-myapp" in env["STATE_DIR"]
    # No optional flags → no TEST_CMD etc emitted.
    assert "TEST_CMD" not in env


def test_synth_repo_env_roundtrips_through_from_env():
    text = cli.synth_repo_env(
        repo="o/r", target_dir="/t", name="r", base_branch="main",
        test_cmd="pytest -q", review_mode="delegated", max_budget="10.00",
    )
    cfg = from_env(_source_env(text))
    assert cfg.repo == "o/r"
    assert cfg.target_dir == "/t"
    assert cfg.base_branch == "main"
    # from_env reads TEST_CMD via env, but MAX_BUDGET_USD / REVIEW_MODE land on cfg.
    assert cfg.fix_budget_usd == 10.00
    from issue_agent.domain.review import ReviewMode
    assert cfg.review_mode is ReviewMode.DELEGATED


def test_synth_repo_env_quotes_special_chars():
    # A TEST_CMD with quotes/$() must survive single-quoting intact.
    cmd = "set -e; R=$(git rev-parse --show-toplevel); (cd \"$R\" && pytest)"
    text = cli.synth_repo_env(repo="o/r", target_dir="/t", name="r", test_cmd=cmd)
    assert _source_env(text)["TEST_CMD"] == cmd


def test_synth_repo_env_records_exact_repoint_lineage():
    text = cli.synth_repo_env(
        repo="o/r",
        target_dir="/srv/current",
        name="r",
        repointed_from_deployments=(
            ("o/r", "/srv/old"),
            ("o/r", "/srv/older"),
        ),
    )

    assert json.loads(
        _source_env(text)["REPOINTED_FROM_DEPLOYMENTS"]
    ) == [
        {"repository": "o/r", "target_dir": "/srv/old"},
        {"repository": "o/r", "target_dir": "/srv/older"},
    ]


def test_repoint_history_preserves_unstarted_and_started_ancestors(tmp_path):
    overlay = tmp_path / "app.env"
    overlay.write_text(
        "REPO='o/r'\n"
        "TARGET_DIR='/srv/second'\n"
        "REPOINTED_FROM_DEPLOYMENTS='"
        '[{"repository":"o/r","target_dir":"/srv/first"}]'
        "'\n"
    )

    history = cli.repoint_history_for_overlay(
        str(overlay),
        repository="o/r",
        target_dir="/srv/third",
    )

    assert history == (
        ("o/r", "/srv/second"),
        ("o/r", "/srv/first"),
    )


def test_repoint_history_refuses_to_discard_malformed_lineage(tmp_path):
    overlay = tmp_path / "app.env"
    overlay.write_text(
        "REPO='o/r'\n"
        "TARGET_DIR='/srv/old'\n"
        "REPOINTED_FROM_DEPLOYMENTS='not-json'\n"
    )

    with pytest.raises(ValueError, match="valid JSON"):
        cli.repoint_history_for_overlay(
            str(overlay),
            repository="o/r",
            target_dir="/srv/new",
        )


def test_repoint_history_rejects_cross_repository_overlay_reuse(tmp_path):
    overlay = tmp_path / "app.env"
    overlay.write_text("REPO='old/repo'\nTARGET_DIR='/srv/old'\n")

    with pytest.raises(ValueError, match="different --name"):
        cli.repoint_history_for_overlay(
            str(overlay),
            repository="new/repo",
            target_dir="/srv/new",
        )


# ── harness worktree path ─────────────────────────────────────────────────────
def test_harness_worktree_path_default_is_sibling():
    p = cli.harness_worktree_path("/Users/me/Repos/MyApp-combined-agent")
    assert p == "/Users/me/Repos/MyApp-combined-agent-harness"


def test_harness_worktree_path_strips_trailing_slash():
    p = cli.harness_worktree_path("/Users/me/Repos/myapp/")
    assert p == "/Users/me/Repos/myapp-harness"


def test_harness_worktree_path_explicit_wins():
    p = cli.harness_worktree_path("/Users/me/Repos/myapp", explicit="/tmp/wt")
    assert p == "/tmp/wt"


# ── HARNESS_ROOT resolves to the actual checkout (siblings present) ───────────
def test_harness_root_points_at_checkout():
    assert os.path.isdir(os.path.join(cli.HARNESS_ROOT, "plugin"))
    assert os.path.isfile(os.path.join(cli.HARNESS_ROOT, "bin", "point-at.sh"))


# ── test-command discovery ────────────────────────────────────────────────────
def test_has_test_signature_true_for_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert cli._has_test_signature(str(tmp_path)) is True


def test_has_test_signature_true_for_glob_go_test(tmp_path):
    (tmp_path / "foo_test.go").write_text("package x\n")
    assert cli._has_test_signature(str(tmp_path)) is True


def test_has_test_signature_false_for_bare_repo(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n")
    assert cli._has_test_signature(str(tmp_path)) is False


def _fake_proc(stdout, returncode=0):
    class P:
        pass
    p = P()
    p.stdout = stdout
    p.returncode = returncode
    return p


def test_discover_test_cmd_skips_llm_when_no_signature(tmp_path, monkeypatch):
    # No signature file → must NOT spend an LLM call; returns '' immediately.
    # (_has_test_signature is exercised directly by its own tests above.)
    monkeypatch.setattr(cli, "_has_test_signature", lambda _d: False)
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return _fake_proc("uv run pytest\n")

    monkeypatch.setattr(cli.subprocess, "run", spy)
    assert cli.discover_test_cmd(str(tmp_path)) == ""
    assert called["n"] == 0


def test_discover_test_cmd_returns_command(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_has_test_signature", lambda _d: True)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _fake_proc("TEST_CMD: uv run pytest\n"))
    assert cli.discover_test_cmd(str(tmp_path)) == "uv run pytest"


def test_discover_test_cmd_no_tests_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_has_test_signature", lambda _d: True)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _fake_proc("TEST_CMD: NO_TESTS\n"))
    assert cli.discover_test_cmd(str(tmp_path)) == ""


def test_discover_test_cmd_fail_soft_on_missing_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_has_test_signature", lambda _d: True)

    def boom(*a, **k):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli.discover_test_cmd(str(tmp_path)) == ""


# ── _parse_test_cmd_reply: robust to weak-model prose / fences ────────────────
def test_parse_test_cmd_plain():
    assert cli._parse_test_cmd_reply("TEST_CMD: uv run pytest") == "uv run pytest"


def test_parse_test_cmd_ignores_prose_prefix():
    # The real failure mode observed: model echoes a prose line first.
    reply = "Standard test command with uv:\nTEST_CMD: uv run pytest -q"
    assert cli._parse_test_cmd_reply(reply) == "uv run pytest -q"


def test_parse_test_cmd_last_marker_wins():
    reply = "TEST_CMD: <the command>\nTEST_CMD: pnpm exec vitest run"
    assert cli._parse_test_cmd_reply(reply) == "pnpm exec vitest run"


def test_parse_test_cmd_no_tests():
    assert cli._parse_test_cmd_reply("TEST_CMD: NO_TESTS") == ""


def test_parse_test_cmd_missing_marker_is_empty():
    assert cli._parse_test_cmd_reply("i could not find any tests, sorry") == ""


def test_parse_test_cmd_monorepo_composite():
    cmd = "set -e; (cd backend && uv run pytest) && (cd frontend && pnpm exec tsc -b)"
    assert cli._parse_test_cmd_reply(f"TEST_CMD: {cmd}") == cmd


# ── review M11: status merges STATE_DIR/REVIEW_STATE_DIR/STEWARD_STATE_DIR ────
def test_journal_paths_overlay_values_win(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text(
        'STATE_DIR="/custom/fix"\n'
        'REVIEW_STATE_DIR="/custom/review"\n'
        'STEWARD_STATE_DIR="/custom/steward"\n'
    )
    assert cli.journal_paths("myapp") == [
        "/custom/fix/journal.jsonl",
        "/custom/review/journal.jsonl",
        "/custom/steward/journal.jsonl",
    ]


def test_journal_paths_falls_back_to_synth_defaults(tmp_path, monkeypatch):
    """No overlay at all → the same per-role layout `synth_repo_env` writes."""
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    fix, review, steward = cli.journal_paths("other")
    assert fix.endswith(os.path.join(".local", "state", "issue-agent-other", "journal.jsonl"))
    assert review.endswith(os.path.join(".local", "state", "review-agent-other", "journal.jsonl"))
    assert steward.endswith(os.path.join(".local", "state", "steward-agent-other", "journal.jsonl"))


def test_journal_path_backcompat_is_the_fix_journal(tmp_path, monkeypatch):
    """journal_path (singular, back-compat) stays the fix-only journal — same
    contract tests/adapters/test_journal.py (outside this leg's ownership)
    already relies on."""
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text('STATE_DIR="/custom/fix"\n')
    assert cli.journal_path("myapp") == cli.journal_paths("myapp")[0] == "/custom/fix/journal.jsonl"


def _write_jsonl(path: str, *lines: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for line in lines:
            f.write(line + "\n")


def test_status_merges_all_three_journals_in_global_ts_order(tmp_path):
    """review M11: a STATE_DIR-only read made review/steward runs, spend
    blocks, and escalations invisible to `issue-agent status`. Interleaved
    events across all three files must render as ONE global timeline."""
    fix_j = str(tmp_path / "fix" / "journal.jsonl")
    review_j = str(tmp_path / "review" / "journal.jsonl")
    steward_j = str(tmp_path / "steward" / "journal.jsonl")
    _write_jsonl(
        fix_j,
        json.dumps({"ts": "2026-07-25T10:00:00+00:00", "event": "agent_run",
                    "kind": "RESOLVE", "number": 1, "cost_usd": 1.0, "status": "ok"}),
    )
    _write_jsonl(
        review_j,
        json.dumps({"ts": "2026-07-25T10:05:00+00:00", "event": "agent_run",
                    "kind": "REVIEW", "number": 9, "cost_usd": 2.0, "status": "ok"}),
        json.dumps({"ts": "2026-07-25T10:06:00+00:00", "event": "spend_block",
                    "scope": "issue", "issue": 9}),
    )
    _write_jsonl(
        steward_j,
        json.dumps({"ts": "2026-07-25T10:07:00+00:00", "event": "escalation",
                    "target": "issue", "number": 3}),
    )
    now = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    out = "\n".join(cli.spend_status_lines(
        [fix_j, review_j, steward_j],
        10.0,
        lambda: now,
    ))
    # review/steward events reach the render — the M11 blind spot.
    assert "REVIEW" in out and "#9" in out
    assert "1 spend block(s) today" in out
    assert "1 escalation(s) to needs-human-review in 24h" in out
    # Global ts order: RESOLVE (10:00, fix) precedes REVIEW (10:05, review) in
    # the "last runs" listing, matching timestamp order across files, not
    # per-file read order.
    assert out.index("RESOLVE") < out.index("REVIEW")


def test_status_merge_anti_vacuity_single_populated_journal_unchanged(tmp_path):
    """With only the fix journal populated (review/steward never ran — their
    files don't exist), passing the full journal_paths triple must render
    IDENTICALLY to passing the fix journal alone (pre-M11 behaviour) — a role
    that never ran must not perturb the output."""
    fix_j = str(tmp_path / "fix" / "journal.jsonl")
    _write_jsonl(
        fix_j,
        json.dumps({"ts": "2026-07-25T10:00:00+00:00", "event": "agent_run",
                    "kind": "RESOLVE", "number": 1, "cost_usd": 1.0, "status": "ok"}),
    )
    missing_review = str(tmp_path / "review" / "journal.jsonl")
    missing_steward = str(tmp_path / "steward" / "journal.jsonl")
    now = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    out_single = cli.spend_status_lines(fix_j, 10.0, lambda: now)
    out_multi = cli.spend_status_lines(
        [fix_j, missing_review, missing_steward],
        10.0,
        lambda: now,
    )
    assert out_single == out_multi
    assert out_single != []  # sanity: the fixture actually produced output


def test_status_merge_agent_run_start_does_not_appear_in_last_runs(tmp_path):
    """The journal now also carries `agent_run_start` events (wave A's
    pre-await receipt). The merged 'last runs' listing must only show settled
    `agent_run` events, never the start half, or an in-flight/never-settled
    run renders as if it completed."""
    fix_j = str(tmp_path / "fix" / "journal.jsonl")
    _write_jsonl(
        fix_j,
        json.dumps({"ts": "2026-07-25T10:00:00+00:00", "event": "agent_run_start",
                    "kind": "RESOLVE", "number": 1}),
        json.dumps({"ts": "2026-07-25T10:00:05+00:00", "event": "agent_run",
                    "kind": "RESOLVE", "number": 1, "cost_usd": 1.0, "status": "ok"}),
    )
    now = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    out = "\n".join(
        cli.spend_status_lines(fix_j, 10.0, lambda: now)
    )
    assert out.count("RESOLVE") == 1


def test_status_caps_resolve_overlay_then_shared_config(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path / "repos"))
    Path(cli.REPOS_DIR).mkdir()
    monkeypatch.setattr(cli, "SHARED_ENV", str(tmp_path / "shared.env"))
    Path(cli.SHARED_ENV).write_text(
        'DAILY_SPEND_CAP_USD="99"\n'
        'GLOBAL_DAILY_SPEND_CAP_USD="1500"\n'
    )
    (Path(cli.REPOS_DIR) / "self.env").write_text(
        'REPO="o/r"\n'
        'TARGET_DIR="/srv/o-r"\n'
        'DAILY_SPEND_CAP_USD="0"\n'
    )

    assert cli.daily_cap("self") == 0
    assert cli.global_daily_cap("self") == 1500


# ── P0R-2: refuse a resolved overlay that belongs to a DIFFERENT repo/checkout
# ── check_overlay_mismatch (pure) ─────────────────────────────────────────────
def test_check_overlay_mismatch_detects_wrong_repo(tmp_path):
    """The exact repro: overlay app.env is for acme/app at /srv/acme-app; the
    detected repo/checkout is other/app at /srv/other-app. Message names both
    repos."""
    (tmp_path / "app.env").write_text(
        'REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\n'
    )
    msg = cli.check_overlay_mismatch(
        "app", "other/app", "/srv/other-app", repos_dir=str(tmp_path)
    )
    assert msg
    assert "acme/app" in msg
    assert "other/app" in msg


def test_check_overlay_mismatch_anti_vacuity_when_repo_agrees(tmp_path):
    """Anti-vacuity twin: same overlay, but the detected repo/checkout IS
    acme/app at /srv/acme-app — no mismatch."""
    (tmp_path / "app.env").write_text(
        'REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\n'
    )
    msg = cli.check_overlay_mismatch(
        "app", "acme/app", "/srv/acme-app", repos_dir=str(tmp_path)
    )
    assert msg == ""


def test_check_overlay_mismatch_no_file_is_not_a_mismatch(tmp_path):
    """No overlay at that name yet → synthesis, not reuse; nothing to compare."""
    assert cli.check_overlay_mismatch("app", "other/app", "/srv/other-app", repos_dir=str(tmp_path)) == ""


def test_check_overlay_mismatch_same_repo_different_target_dir_is_fatal(tmp_path):
    """A sibling checkout must not start or stop this deployment."""
    (tmp_path / "app-wt.env").write_text(
        'REPO="acme/app"\nTARGET_DIR="/srv/app-harness"\n'
    )
    msg = cli.check_overlay_mismatch(
        "app-wt", "acme/app", "/srv/somewhere-else", repos_dir=str(tmp_path)
    )
    assert msg
    assert "/srv/app-harness" in msg
    assert "/srv/somewhere-else" in msg


def test_check_overlay_mismatch_target_dir_fallback_when_no_repo_recorded(tmp_path):
    """A malformed/hand-edited overlay with no REPO at all: TARGET_DIR is the
    only identity signal left, so a mismatch there is still caught."""
    (tmp_path / "app.env").write_text('TARGET_DIR="/srv/acme-app"\n')
    msg = cli.check_overlay_mismatch(
        "app", "other/app", "/srv/other-app", repos_dir=str(tmp_path)
    )
    assert msg
    assert "/srv/acme-app" in msg
    assert "/srv/other-app" in msg


# ── per-command wiring: fatal (start/stop/doctor/install-service) vs warn (status) ──
def _start_args(**overrides) -> argparse.Namespace:
    base = dict(
        repo="", target_dir="", name="", roles=None, base_branch="dev",
        test_cmd="", no_init=True, edit_check_cmd="", review_mode="",
        max_budget="", review_budget="", max_ticks=None, write_config=False,
        worktree=False, worktree_path="",
        no_verify_test_cmd=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _prep_mismatch_overlay(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    (tmp_path / "app.env").write_text(
        'REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\nTEST_CMD="echo ok"\n'
    )


def test_cmd_start_refuses_wrong_repo_overlay(tmp_path, monkeypatch, capsys):
    """P0R-2 exact repro at the `cmd_start` level: resolving to the app.env
    overlay (whether via an explicit --name or a stale collision) while the
    detected repo/checkout is a DIFFERENT one must refuse — not silently
    launch all three daemons against acme/app while sitting in other/app."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    execve_calls: list = []
    monkeypatch.setattr(cli.os, "execve", lambda *a: execve_calls.append(a))

    args = _start_args(repo="other/app", target_dir="/srv/other-app", name="app")
    rc = cli.cmd_start(args)

    assert rc == 2
    assert execve_calls == []  # never delegated to point-at.sh
    captured = capsys.readouterr()
    assert "acme/app" in captured.err
    assert "other/app" in captured.err


def test_cmd_start_reuses_matching_overlay_no_warning(tmp_path, monkeypatch, capsys):
    """Anti-vacuity twin: same overlay, but the detected repo/checkout IS
    acme/app at /srv/acme-app — proceeds and reuses, no refusal, no warning."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    execve_calls: list = []
    monkeypatch.setattr(cli.os, "execve", lambda *a: execve_calls.append(a))

    args = _start_args(repo="acme/app", target_dir="/srv/acme-app", name="app")
    rc = cli.cmd_start(args)

    assert rc is None  # falls through to (stubbed) os.execve, never returns 2
    assert len(execve_calls) == 1  # DID delegate to point-at.sh
    captured = capsys.readouterr()
    assert "reusing config/repos/app.env" in captured.out
    assert captured.err == ""


def test_cmd_start_write_config_stops_existing_deployment_before_rewrite(
    tmp_path,
    monkeypatch,
) -> None:
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    overlay = tmp_path / "app.env"
    original = overlay.read_text()
    events: list[tuple[str, str]] = []

    def stop(name: str) -> int:
        events.append((f"stop:{name}", overlay.read_text()))
        return 0

    def execve(*_args) -> None:
        events.append(("exec", overlay.read_text()))

    monkeypatch.setattr(cli, "_stop_deployment", stop)
    monkeypatch.setattr(cli.os, "execve", execve)

    rc = cli.cmd_start(
        _start_args(
            repo="acme/app",
            target_dir="/srv/acme-app",
            name="app",
            write_config=True,
            test_cmd="make test",
            no_verify_test_cmd=True,
        )
    )

    assert rc is None
    assert events[0] == ("stop:app", original)
    assert events[1][0] == "exec"
    assert events[1][1] != original
    assert "TEST_CMD='make test'" in events[1][1]


def test_cmd_start_write_config_preserves_overlay_when_quiescence_fails(
    tmp_path,
    monkeypatch,
) -> None:
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    overlay = tmp_path / "app.env"
    original = overlay.read_text()
    execve_calls: list[tuple] = []
    monkeypatch.setattr(cli, "_stop_deployment", lambda _name: 1)
    monkeypatch.setattr(
        cli.os,
        "execve",
        lambda *args: execve_calls.append(args),
    )

    rc = cli.cmd_start(
        _start_args(
            repo="acme/app",
            target_dir="/srv/acme-app",
            name="app",
            write_config=True,
            test_cmd="make test",
            no_verify_test_cmd=True,
        )
    )

    assert rc == 1
    assert overlay.read_text() == original
    assert execve_calls == []


def test_cmd_stop_refuses_wrong_repo_overlay(tmp_path, monkeypatch, capsys):
    """FATAL: stop is destructive (SIGTERMs another deployment's daemons if
    it proceeded), so a mismatch here is refused, not warned."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    args = argparse.Namespace(repo="other/app", target_dir="/srv/other-app", name="app")
    rc = cli.cmd_stop(args)
    assert rc == 2
    captured = capsys.readouterr()
    assert "acme/app" in captured.err
    assert "other/app" in captured.err


def test_role_pid_discovery_is_scoped_to_requested_deployment(
    monkeypatch,
) -> None:
    by_pattern = {
        "issue_agent.app.main fix": [101, 102],
        "issue_agent.app.launchd_main fix": [],
        "issue_agent.app.main review": [201, 202],
        "issue_agent.app.launchd_main review": [],
        "issue_agent.app.main steward": [301],
        "issue_agent.app.launchd_main steward": [],
    }
    owners = {
        101: "deployment-a",
        102: "deployment-b",
        201: "deployment-a",
        202: "deployment-b",
        301: "deployment-b",
    }
    monkeypatch.setattr(cli, "_pgrep", lambda pattern: by_pattern[pattern])
    monkeypatch.setattr(
        cli, "_pid_deployment_name", lambda pid: owners[pid]
    )

    assert cli._role_pids("deployment-a") == {
        "fix": [101],
        "review": [201],
        "steward": [],
    }
    assert cli._role_pids("deployment-b") == {
        "fix": [102],
        "review": [202],
        "steward": [301],
    }


def test_launcher_pid_discovery_uses_exact_deployment_argv_token(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli, "_pgrep", lambda _pattern: [101, 102, 103, 104]
    )
    argv = {
        101: ("/bin/bash", "/opt/harness/bin/point-at.sh", "app"),
        102: ("/bin/bash", "/opt/harness/bin/point-at.sh", "app2"),
        103: ("/bin/bash", "/tmp/not-point-at.sh", "app"),
        104: (
            "python",
            "-c",
            "import time; time.sleep(60)",
            "/tmp/point-at.sh",
            "app",
        ),
    }
    monkeypatch.setattr(cli, "_pid_argv", lambda pid: argv[pid])

    assert cli._launcher_pids("app") == [101]
    assert cli._launcher_pids("app2") == [102]


def test_linux_pid_deployment_identity_comes_from_process_environment():
    if not os.path.isdir("/proc"):
        return
    process = subprocess.Popen(
        [
            cli.sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(5)",
        ],
        env={
            **os.environ,
            "ISSUE_AGENT_DEPLOYMENT_ID": "deployment-a",
        },
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        assert cli._pid_deployment_name(process.pid) == "deployment-a"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_cmd_doctor_refuses_wrong_repo_overlay(tmp_path, monkeypatch, capsys):
    """FATAL: doctor would compose env for and preflight-check the WRONG
    repo, reporting a false all-clear right before an operator runs `start`."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    args = argparse.Namespace(repo="other/app", target_dir="/srv/other-app", name="app")
    rc = cli.cmd_doctor(args)
    assert rc == 2
    captured = capsys.readouterr()
    assert "acme/app" in captured.err
    assert "other/app" in captured.err


def test_install_service_refuses_wrong_repo_overlay(tmp_path, monkeypatch, capsys):
    """FATAL: install-service is worse than a one-shot `start` against the
    wrong repo — it installs a PERSISTENT service that keeps relaunching
    against it. Refuses before ever invoking compose-env.sh."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    real_run = cli.subprocess.run
    calls: list = []

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        # check_overlay_mismatch reads the overlay via a REAL bash subshell
        # (read_repo_env_field) before any compose-env.sh/install-*.sh call —
        # let that one through untouched; only intercept the script calls.
        if cmd[0] == "bash" and cmd[1] == "-c":
            return real_run(cmd, **kwargs)
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    args = argparse.Namespace(
        repo="other/app", target_dir="/srv/other-app", name="app", roles=None
    )
    rc = cli.cmd_install_service(args)
    assert rc == 2
    assert calls == []  # refused before composing env / installing anything
    captured = capsys.readouterr()
    assert "acme/app" in captured.err
    assert "other/app" in captured.err


def test_cmd_status_warns_but_proceeds_on_wrong_repo_overlay(tmp_path, monkeypatch, capsys):
    """WARN, not fatal: status is read-only (no SIGTERM, no service install,
    no dispatch) — a mismatch is misleading, not dangerous, and refusing
    would prevent using `status` to diagnose this exact collision."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    args = argparse.Namespace(repo="other/app", target_dir="/srv/other-app", name="app", lines=5)
    rc = cli.cmd_status(args)
    assert rc == 0  # proceeded
    captured = capsys.readouterr()
    assert "acme/app" in captured.err
    assert "other/app" in captured.err
    assert "status for 'app'" in captured.out  # ... and still rendered status


# ── review M12/A4 fold-in: cmd_install_service fails loud on compose failure ──
def _install_args(tmp_path) -> argparse.Namespace:
    # Explicit repo/target_dir/name → _resolve_context never shells out to
    # git/gh; resolve_name(explicit=name) returns immediately.
    return argparse.Namespace(repo="o/r", target_dir=str(tmp_path), name="myapp", roles=None)


def test_install_service_aborts_on_compose_failure(tmp_path, monkeypatch):
    """A4's out-of-scope report: compose-env.sh ran with check=False and its
    return code was never inspected — a compose failure proceeded straight to
    installing the service against a stale/missing config/.active.env."""
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text(
        f'REPO="o/r"\nTARGET_DIR="{tmp_path}"\n'
    )
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))

    real_run = cli.subprocess.run
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        # P0R-2's check_overlay_mismatch reads the overlay via a REAL bash
        # subshell first — pass that one through; only intercept script calls.
        if cmd[0] == "bash" and cmd[1] == "-c":
            return real_run(cmd, **kwargs)
        calls.append(cmd)
        return _Result(1 if "compose-env.sh" in cmd[1] else 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.cmd_install_service(_install_args(tmp_path))
    assert rc != 0
    assert not any(
        "install-systemd.sh" in c[1] or "install-launchd.sh" in c[1] for c in calls
    )


def test_install_service_proceeds_on_compose_success(tmp_path, monkeypatch):
    """Anti-vacuity twin: a successful compose still installs the service."""
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text(
        f'REPO="o/r"\nTARGET_DIR="{tmp_path}"\n'
    )
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))

    real_run = cli.subprocess.run
    calls: list[list[str]] = []
    detailed_calls: list[tuple[list[str], dict]] = []

    class _Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        if cmd[0] == "bash" and cmd[1] == "-c":
            return real_run(cmd, **kwargs)
        calls.append(cmd)
        detailed_calls.append((cmd, kwargs))
        return _Result(0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.cmd_install_service(_install_args(tmp_path))
    assert rc == 0
    assert any(
        "install-systemd.sh" in c[1] or "install-launchd.sh" in c[1] for c in calls
    )
    install_call = next(
        kwargs
        for command, kwargs in detailed_calls
        if "install-systemd.sh" in command[1]
        or "install-launchd.sh" in command[1]
    )
    assert install_call["env"]["ISSUE_AGENT_DEPLOYMENT_ID"] == "myapp"


def test_install_service_rejects_partial_role_cutover(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text(
        f'REPO="o/r"\nTARGET_DIR="{tmp_path}"\n'
    )
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    args = _install_args(tmp_path)
    args.roles = ["fix"]

    assert cli.cmd_install_service(args) == 2
    assert "every legacy writer stops" in capsys.readouterr().err


def test_uninstall_service_defaults_to_current_deployment(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "SHARED_ENV", str(tmp_path / "shared.env"))
    Path(cli.SHARED_ENV).write_text("")
    (tmp_path / "myapp.env").write_text(
        f'REPO="o/r"\nTARGET_DIR="{tmp_path}"\n'
    )
    calls: list[tuple[list[str], dict]] = []
    real_run = cli.subprocess.run

    class _Result:
        returncode = 0

    def fake_run(command, **kwargs):
        if command[:2] == ["bash", "-c"]:
            return real_run(command, **kwargs)
        calls.append((command, kwargs))
        return _Result()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    args = argparse.Namespace(
        repo="o/r",
        target_dir=str(tmp_path),
        name="myapp",
        all=False,
    )

    assert cli.cmd_uninstall_service(args) == 0
    uninstall, kwargs = calls[-1]
    assert uninstall[-2:] == ["uninstall", "myapp"]
    assert kwargs["env"]["ISSUE_AGENT_DEPLOYMENT_ID"] == "myapp"
    assert kwargs["env"]["AGENT_ENV"].endswith(".active.myapp.env")


def test_uninstall_service_all_is_explicit_and_unscoped(monkeypatch) -> None:
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or _Result(),
    )
    args = argparse.Namespace(
        repo="",
        target_dir="",
        name="",
        all=True,
    )

    assert cli.cmd_uninstall_service(args) == 0
    assert calls == [
        [
            cli._BASH,
            cli._bin(
                "install-launchd.sh"
                if cli.sys.platform == "darwin"
                else "install-systemd.sh"
            ),
            "uninstall",
        ]
    ]


# ── TEST_CMD verification (a wrong command wedges the whole gate) ──────────────
def test_verify_reports_a_passing_suite():
    check = cli.verify_test_cmd("true", "/tmp", run=lambda c, cwd, t: 0)
    assert check.passed and not check.timed_out


def test_verify_reports_a_failing_suite_without_masking_it():
    """A red suite must be REPORTED, never silently swapped for the
    ALLOW_UNTESTED fallback — that would trade a loud problem (the gate denies
    pushes until you fix it) for a silent one (a gate with no teeth)."""
    check = cli.verify_test_cmd("false", "/tmp", run=lambda c, cwd, t: 1)
    assert not check.passed and not check.timed_out and check.exit_code == 1


def test_verify_detects_watch_mode_as_a_timeout():
    """The signature failure: `vitest` instead of `vitest run` never exits, so
    the gate hangs GATE_TEST_TIMEOUT_S on every push and then denies it."""
    def _hang(cmd, cwd, timeout):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
    check = cli.verify_test_cmd("vitest", "/tmp", timeout_s=0.1, run=_hang)
    assert check.timed_out and check.exit_code is None


def test_verify_survives_a_runner_that_explodes():
    """Verification is best-effort: it must never be the reason `start` fails."""
    def _boom(cmd, cwd, timeout):
        raise OSError("no shell")
    assert cli.verify_test_cmd("x", "/tmp", run=_boom).exit_code == -1


def test_verify_runs_the_command_the_GATE_would_run():
    """Anti-vacuity on the whole feature: verifying a DIFFERENT invocation than
    the gate uses would prove nothing. The gate does
    `subprocess.run(cmd, shell=True, cwd=<repo>)`, so the command string must
    reach the runner intact and with the repo as cwd."""
    seen = {}

    def _spy(cmd, cwd, timeout):
        seen.update(cmd=cmd, cwd=cwd, timeout=timeout)
        return 0

    cli.verify_test_cmd("set -e; (cd api && pytest)", "/repo/root", run=_spy)
    assert seen["cmd"] == "set -e; (cd api && pytest)"
    assert seen["cwd"] == "/repo/root"
    assert seen["timeout"] > 0


def test_report_names_watch_mode_and_the_slow_suite_alternative(capsys):
    cli.report_test_cmd_check("vitest", cli.TestCmdCheck(exit_code=None, seconds=180.0))
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "WATCH MODE" in combined and "GATE_TEST_TIMEOUT_S" in combined


def test_report_tells_you_a_red_suite_blocks_the_fixer(capsys):
    cli.report_test_cmd_check("pytest", cli.TestCmdCheck(exit_code=1, seconds=3.0))
    combined = "".join(capsys.readouterr())
    assert "denies every push" in combined


def test_ready_routes_operator_label_through_durable_effects(
    monkeypatch, capsys
):
    calls = []

    class Effects:
        def set_issue_labels(self, issue, *, add=(), remove=()):
            calls.append((issue, add, remove))
            return True, "ok"

    monkeypatch.setattr(cli, "detect_target_dir", lambda _cwd: "/repo")
    monkeypatch.setattr(cli, "resolve_name", lambda *_args, **_kwargs: "demo")
    monkeypatch.setattr(cli, "_operator_effects", lambda *_args: Effects())
    args = argparse.Namespace(repo="o/r", issues=["9"])

    assert cli.cmd_ready(args) == 0
    assert calls == [(9, ("ai-ready",), ())]
    assert "#9" in capsys.readouterr().out


def test_remote_bootstrap_uses_effect_commands_for_every_requested_write(
    monkeypatch,
):
    labels = []
    auto_merge = []

    class Effects:
        def ensure_label(self, label, **_kwargs):
            labels.append(label)
            return True, "ok"

        def set_repository_auto_merge(self, enabled):
            auto_merge.append(enabled)
            return True, "ok"

    monkeypatch.setattr(cli, "_operator_effects", lambda *_args: Effects())
    args = argparse.Namespace(
        repo="o/r",
        name="demo",
        skip_labels=False,
        ensure_auto_merge=True,
    )

    assert cli.cmd_prepare_remote(args) == 0
    assert tuple(labels) == cli.REMOTE_LABELS
    assert auto_merge == [True]
