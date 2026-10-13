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

from issue_agent.app import cli, codex_resolution


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
def test_dashboard_reports_a_stale_frontend_bundle_and_exits_nonzero(
    monkeypatch, capsys
):
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
            "legacy": False,
        },
    )()
    monkeypatch.setattr(
        cli,
        "_prepare_dashweb",
        lambda: cli.DashwebPreparation(
            False, "automatic dashboard rebuild failed with exit 2", True
        ),
    )
    assert cli.cmd_dashboard(args) == 2
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "automatic dashboard rebuild failed with exit 2" in combined
    assert "uv sync --extra dash" in combined
    assert "npm run build" in combined


def test_dashboard_legacy_flag_keeps_snapshot_server_reload(monkeypatch):
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
            "legacy": True,
        },
    )()
    changed = object()
    served = []
    monkeypatch.setattr(cli, "_prepare_dashweb", lambda: pytest.fail("SPA prepared"))
    monkeypatch.setattr("issue_agent.adapters.reload.ReloadWatch.changed", changed)
    monkeypatch.setattr(
        dashboard,
        "serve",
        lambda port, render, **kwargs: served.append((port, render, kwargs)) or 87,
    )

    assert cli.cmd_dashboard(args) == 87
    assert served[0][0] == 8321
    assert served[0][2]["reload_check"] is changed


def test_dashboard_server_startup_failure_is_loud_and_nonzero(monkeypatch, capsys):
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
            "legacy": False,
        },
    )()
    monkeypatch.setattr(
        cli, "_prepare_dashweb", lambda: cli.DashwebPreparation(True, "ready")
    )
    monkeypatch.setattr(
        "issue_agent.app.dashweb.server.serve",
        lambda _port: (_ for _ in ()).throw(RuntimeError("socket unavailable")),
    )

    assert cli.cmd_dashboard(args) == 2
    assert "socket unavailable" in capsys.readouterr().err


def test_dashboard_server_nonzero_startup_exit_is_not_silent(monkeypatch, capsys):
    args = type(
        "Args",
        (),
        {
            "names": ["demo"], "html": None, "text": False, "serve": 8321,
            "details": False, "stale_minutes": 5.0, "legacy": False,
        },
    )()
    monkeypatch.setattr(
        cli, "_prepare_dashweb", lambda: cli.DashwebPreparation(True, "ready")
    )
    monkeypatch.setattr("issue_agent.app.dashweb.server.serve", lambda _port: 1)

    assert cli.cmd_dashboard(args) == 2
    assert "dashboard server exited with status 1" in capsys.readouterr().err


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("REVIEW_BOT_TOKEN", "review-secret"),
        ("GH_APP_ID", "123"),
        ("GH_APP_INSTALL_ID", "456"),
        ("GH_APP_KEY_FILE", "/keys/review.pem"),
        ("GH_APP_TOKEN_CACHE", "/state/app-token.json"),
    ],
)
def test_rearm_identity_fields_are_read_from_the_composed_file_only(
    tmp_path, monkeypatch, field, value
):
    overlay = tmp_path / "r.env"
    overlay.write_text(f'{field}="{value}"\n')
    monkeypatch.setenv(field, "ambient-secret-must-not-win")

    assert cli.read_repo_env_field(str(overlay), field) == value

    overlay.write_text('OTHER="x"\n')
    assert cli.read_repo_env_field(str(overlay), field) == ""


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
    # The harness never writes TEST_CMD: it is only the operator's point-at probe.
    assert "TEST_CMD" not in env


def test_synth_repo_env_roundtrips_through_from_env():
    text = cli.synth_repo_env(
        repo="o/r", target_dir="/t", name="r", base_branch="main",
        max_budget="10.00",
    )
    cfg = from_env(_source_env(text))
    assert cfg.repo == "o/r"
    assert cfg.target_dir == "/t"
    assert cfg.base_branch == "main"
    assert cfg.fix_budget_usd == 10.00


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


def _fake_proc(stdout, returncode=0):
    class P:
        pass
    p = P()
    p.stdout = stdout
    p.returncode = returncode
    return p


# ── review M11: status merges STATE_DIR/REVIEW_STATE_DIR ────
def test_journal_paths_overlay_values_win(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text(
        'STATE_DIR="/custom/fix"\n'
        'REVIEW_STATE_DIR="/custom/review"\n'
    )
    assert cli.journal_paths("myapp") == [
        "/custom/fix/journal.jsonl",
        "/custom/review/journal.jsonl",
    ]


def test_journal_paths_falls_back_to_synth_defaults(tmp_path, monkeypatch):
    """No overlay at all → the same per-role layout `synth_repo_env` writes."""
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    fix, review = cli.journal_paths("other")
    assert fix.endswith(os.path.join(".local", "state", "issue-agent-other", "journal.jsonl"))
    assert review.endswith(os.path.join(".local", "state", "review-agent-other", "journal.jsonl"))


def test_journal_paths_for_a_hand_written_overlay_match_where_daemons_write(
    tmp_path, monkeypatch
):
    """A reconstructed overlay with REPO but no STATE_DIR keys (keifu,
    2026-08-02): status/dashboard must read the SAME dirs the daemons derive
    via config.from_env, or every run is invisible while spend accrues."""
    from issue_agent.app import config as config_mod

    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text('REPO="Acme/Widget-App"\n')

    daemon_cfg = config_mod.from_env({"REPO": "Acme/Widget-App"})
    assert cli.journal_paths("myapp") == [
        os.path.join(daemon_cfg.state_dir, "journal.jsonl"),
        os.path.join(daemon_cfg.review_state_dir, "journal.jsonl"),
    ]


def _isolated_config(tmp_path, monkeypatch):
    """Point both config roots at tmp: `config/repos/<name>.env` overlays and
    the composed `config/.active.<name>.env` snapshots."""
    repos = tmp_path / "repos"
    config = tmp_path / "config"
    repos.mkdir()
    config.mkdir()
    monkeypatch.setattr(cli, "REPOS_DIR", str(repos))
    monkeypatch.setattr(cli, "HARNESS_ROOT", str(tmp_path))
    return repos, config


def test_journal_paths_prefer_the_composed_launch_snapshot(tmp_path, monkeypatch):
    """REPO lives in `config/shared.env` for many deployments, so it reaches the
    daemons only through the composed snapshot. Reading the overlay alone
    derives a state dir nobody writes to and reports an empty history while
    SQLite spend accrues — the read side must resolve what actually launched."""
    from issue_agent.app import config as config_mod

    repos, config = _isolated_config(tmp_path, monkeypatch)
    (repos / "myapp.env").write_text('TARGET_DIR="/srv/myapp"\n')  # no REPO
    (config / ".active.myapp.env").write_text('REPO="Acme/Widget-App"\n')

    daemon_cfg = config_mod.from_env({"REPO": "Acme/Widget-App"})
    assert cli.journal_paths("myapp") == [
        os.path.join(daemon_cfg.state_dir, "journal.jsonl"),
        os.path.join(daemon_cfg.review_state_dir, "journal.jsonl"),
    ]


def test_a_snapshot_missing_one_key_still_falls_through_to_the_overlay(
    tmp_path, monkeypatch
):
    """Per FIELD, not per file: a snapshot that names REPO but not
    REVIEW_STATE_DIR must not blank the overlay's explicit value."""
    repos, config = _isolated_config(tmp_path, monkeypatch)
    (repos / "myapp.env").write_text(
        'REPO="Acme/Old"\nREVIEW_STATE_DIR="/custom/review"\n'
    )
    (config / ".active.myapp.env").write_text(
        'REPO="Acme/New"\nSTATE_DIR="/composed/fix"\n'
    )

    fix, review = cli.journal_paths("myapp")
    assert fix == "/composed/fix/journal.jsonl", "snapshot wins where it speaks"
    assert review == "/custom/review/journal.jsonl", "overlay fills the gap"


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
        no_init=True,
        max_budget="", review_budget="", max_ticks=None, write_config=False,
        worktree=False, worktree_path="",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _prep_mismatch_overlay(tmp_path, monkeypatch, *, isolate_entrypoint=True) -> None:
    # Overlay-only cases ignore host installation; install cases supply their
    # own PATH fixture and must retain real drift detection.
    if isolate_entrypoint:
        monkeypatch.setattr(cli, "entrypoint_drift", lambda: "")
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    (tmp_path / "app.env").write_text(
        'REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\nTEST_CMD="echo ok"\n'
    )


# ── start self-heals a stale/partial install (#156 follow-up, 2026-08-08) ─────
def _install_state(
    monkeypatch,
    present,
    install_rc=0,
    after=None,
    after_foreign=(),
    live=(),
    foreign=(),
    timeout=False,
    harness_root="/opt/test-harness",
):
    """PATH resolution for the declared console scripts, plus a recording stub
    for `make install` that can flip what PATH resolves afterwards. `foreign`
    names scripts that resolve to ANOTHER checkout's shim; `live` is what the
    deployment-liveness probe reports; `after_foreign` is what still shadows
    after the install."""
    calls: list[list[str]] = []
    state = {"present": set(present), "foreign": set(foreign)}
    scripts = set(cli.declared_console_scripts())
    # Exercise installation paths, including explicit transient cases, without
    # deriving the simulated install from wherever pytest itself was launched.
    monkeypatch.setattr(cli, "HARNESS_ROOT", harness_root)
    real_which, real_run = cli.shutil.which, cli.subprocess.run
    venv_bin = os.path.join(cli.HARNESS_ROOT, ".venv", "bin")

    def which(name, *args, **kwargs):
        if name not in scripts:
            return real_which(name, *args, **kwargs)
        if name not in state["present"]:
            return None
        base = (
            "/opt/other-checkout/.venv/bin"
            if name in state["foreign"]
            else venv_bin
        )
        return os.path.join(base, name)

    def run(cmd, **kwargs):
        if list(cmd)[:2] != ["make", "install"]:
            return real_run(cmd, **kwargs)
        calls.append(list(cmd))
        if after is not None:  # a successful install rewrites every shim
            state["present"] = set(after)
            state["foreign"] = set(after_foreign)
        if timeout:
            raise subprocess.TimeoutExpired(cmd, 300, output=b"sync log\nboom")
        return subprocess.CompletedProcess(cmd, install_rc, "", "boom")

    monkeypatch.setattr(cli.shutil, "which", which)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "live_deployment_names", lambda: tuple(live))
    return calls


def test_start_does_not_reinstall_when_the_entry_points_are_intact(monkeypatch):
    calls = _install_state(monkeypatch, present=("issue-agent",))

    assert cli.heal_entrypoint_drift() is True
    assert calls == []


def test_start_does_not_reinstall_a_checkout_that_never_installed(monkeypatch):
    """Neither script on PATH is a dev checkout, not drift — doctor stays quiet
    there too, and `make install` must not be run behind the operator's back."""
    calls = _install_state(monkeypatch, present=())

    assert cli.heal_entrypoint_drift() is True
    assert calls == []


def test_start_gives_up_after_one_failed_reinstall(monkeypatch, capsys):
    calls = _install_state(
        monkeypatch, present=("issue-agent",), foreign=("issue-agent",), install_rc=2
    )

    assert cli.heal_entrypoint_drift() is False
    assert len(calls) == 1  # one attempt, never a retry loop
    assert "auto-repair failed" in capsys.readouterr().err


def test_start_gives_up_when_the_reinstall_does_not_clear_the_drift(
    monkeypatch, capsys
):
    calls = _install_state(
        monkeypatch, present=("issue-agent",), foreign=("issue-agent",),
        after=("issue-agent",), after_foreign=("issue-agent",),
    )

    assert cli.heal_entrypoint_drift() is False
    assert len(calls) == 1
    assert "still drifted" in capsys.readouterr().err


def test_start_refuses_to_reinstall_under_a_live_deployment(monkeypatch, capsys):
    """`make install` runs `uv sync` on the ONE .venv every deployment's daemons
    execute from; pruning it mid-tick breaks the running ones."""
    calls = _install_state(
        monkeypatch, present=("issue-agent",), foreign=("issue-agent",), live=("keifu", "sol")
    )

    assert cli.heal_entrypoint_drift() is False
    assert calls == []  # never touched the shared .venv
    captured = capsys.readouterr().err
    assert "keifu, sol" in captured
    assert "uv sync" in captured


def test_start_treats_a_shim_from_another_checkout_as_drift(monkeypatch, capsys):
    """Presence-only reads healthy while another checkout's shim shadows ours —
    the daemon then execs a different tree's plugin/ and config/."""
    calls = _install_state(
        monkeypatch,
        present=("issue-agent",),
        foreign=("issue-agent",),
        after=("issue-agent",),
    )

    assert cli.heal_entrypoint_drift() is True
    assert len(calls) == 1
    assert "/opt/other-checkout" in capsys.readouterr().out


def test_drift_flags_an_install_resolving_into_a_transient_worktree(monkeypatch):
    """#187: `make install` inside an agent worktree bakes the transient venv
    into the launcher; from inside that process every other check reads
    coherent, so the path itself is the signature."""
    _install_state(
        monkeypatch, present=("issue-agent",),
        harness_root="/home/u/repo/.claude/worktrees/agent-x",
    )

    assert "transient agent worktree" in cli.entrypoint_drift()


def test_heal_refuses_to_reinstall_from_a_transient_worktree(monkeypatch, capsys):
    """`make install` from the transient process would reinstall the transient
    tree — the exact state being healed."""
    calls = _install_state(
        monkeypatch, present=("issue-agent",),
        harness_root="/home/u/repo/.claude/worktrees/agent-x",
    )

    assert cli.heal_entrypoint_drift() is False
    assert calls == []
    assert "canonical checkout" in capsys.readouterr().err


def test_doctor_refuses_to_render_from_a_transient_worktree(monkeypatch, capsys):
    """#187: doctor from a stale worktree venv reported four confidently wrong
    errors about the deployment. Wrong-checkout red is worse than no output —
    but ONLY the transient case refuses; other drift renders with the drift
    leading, because doctor still runs the code the operator invoked."""
    monkeypatch.setattr(cli, "entrypoint_drift", lambda: "runs a transient tree")
    monkeypatch.setattr(cli, "running_from_transient_checkout", lambda: True)

    assert cli.cmd_doctor(object()) == 2
    captured = capsys.readouterr().err
    assert "REFUSING" in captured and "runs a transient tree" in captured


def test_start_gives_up_when_the_reinstall_times_out(monkeypatch, capsys):
    calls = _install_state(monkeypatch, present=("issue-agent",), foreign=("issue-agent",), timeout=True)

    assert cli.heal_entrypoint_drift() is False
    assert len(calls) == 1
    captured = capsys.readouterr().err
    assert "exceeded 300s" in captured
    assert "boom" in captured  # output tail still reaches the operator


def test_entrypoint_drift_covers_every_declared_console_script():
    """Derived from pyproject's [project.scripts], not a second hardcoded list
    that can disagree with the one bin/install.sh materialises."""
    assert set(cli.declared_console_scripts()) == {"issue-agent"}


def test_live_deployment_names_ignores_stopped_deployments(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "keifu.env").write_text("")
    (tmp_path / "notes.txt").write_text("")
    monkeypatch.setattr(cli, "_launcher_pids", lambda _n: [])
    monkeypatch.setattr(cli, "_role_pids", lambda _n: {"fix": []})
    monkeypatch.setattr(cli, "_fresh_role_heartbeats", lambda _n: {})

    assert cli.live_deployment_names() == ()

    monkeypatch.setattr(cli, "_launcher_pids", lambda n: [7] if n == "keifu" else [])

    assert cli.live_deployment_names() == ("keifu",)


def test_start_still_launches_the_preflight_after_a_failed_reinstall(
    tmp_path, monkeypatch, capsys
):
    """Fallback is today's behaviour: the preflight runs and reports the
    stale/partial install verbatim, rather than a second Python-side error."""
    _prep_mismatch_overlay(tmp_path, monkeypatch, isolate_entrypoint=False)
    execve_calls: list = []
    monkeypatch.setattr(cli.os, "execve", lambda *a: execve_calls.append(a))
    install_calls = _install_state(monkeypatch, present=("issue-agent",), foreign=("issue-agent",), install_rc=1)

    cli.cmd_start(
        _start_args(repo="acme/app", target_dir="/srv/acme-app", name="app")
    )

    assert install_calls == [["make", "install"]]
    assert len(execve_calls) == 1
    assert "point-at.sh" in " ".join(execve_calls[0][1])


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
    _install_state(monkeypatch, present=())
    execve_calls: list = []
    monkeypatch.setattr(cli.os, "execve", lambda *a: execve_calls.append(a))

    args = _start_args(repo="acme/app", target_dir="/srv/acme-app", name="app")
    rc = cli.cmd_start(args)

    assert rc is None  # falls through to (stubbed) os.execve, never returns 2
    assert len(execve_calls) == 1  # DID delegate to point-at.sh
    captured = capsys.readouterr()
    assert "reusing config/repos/app.env" in captured.out
    assert captured.err == ""


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
    }
    owners = {
        101: "deployment-a",
        102: "deployment-b",
        201: "deployment-a",
        202: "deployment-b",
    }
    monkeypatch.setattr(cli, "_pgrep", lambda pattern: by_pattern[pattern])
    monkeypatch.setattr(
        cli, "_pid_deployment_name", lambda pid: owners[pid]
    )

    assert cli._role_pids("deployment-a") == {
        "fix": [101],
        "review": [201],
    }
    assert cli._role_pids("deployment-b") == {
        "fix": [102],
        "review": [202],
    }


def test_fresh_role_heartbeats_are_bounded_and_ignore_terminal_state(
    tmp_path,
    monkeypatch,
) -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 3, 22, 30, tzinfo=timezone.utc)
    state_dirs = [tmp_path / role for role in cli.ROLES]
    for state_dir in state_dirs:
        state_dir.mkdir()
    (state_dirs[0] / "heartbeat.json").write_text(
        json.dumps({
            "state": "running",
            "updated_at": (now - timedelta(seconds=10)).isoformat(),
        })
    )
    (state_dirs[1] / "heartbeat.json").write_text(
        json.dumps({
            "state": "running",
            "updated_at": (now - timedelta(seconds=31)).isoformat(),
        })
    )
    monkeypatch.setattr(
        cli,
        "journal_paths",
        lambda _name: [str(path / "journal.jsonl") for path in state_dirs],
    )

    assert cli._fresh_role_heartbeats(
        "app", now_fn=lambda: now, stale_after_s=30
    ) == {"fix": now - timedelta(seconds=10)}

    (state_dirs[1] / "heartbeat.json").write_text(
        json.dumps({
            "state": "running",
            "updated_at": (now + timedelta(seconds=1)).isoformat(),
        })
    )
    assert "review" not in cli._fresh_role_heartbeats(
        "app", now_fn=lambda: now, stale_after_s=30
    )

    (state_dirs[1] / "heartbeat.json").write_text(
        json.dumps({"state": "stopped", "updated_at": now.isoformat()})
    )
    assert "review" not in cli._fresh_role_heartbeats(
        "app", now_fn=lambda: now, stale_after_s=30
    )


def test_cmd_status_reports_heartbeat_when_pid_namespace_hides_daemon(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    heartbeat = datetime(2026, 8, 3, 22, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        cli,
        "_role_pids",
        lambda _name: {role: [] for role in cli.ROLES},
    )
    monkeypatch.setattr(cli, "_launcher_pids", lambda _name: [])
    monkeypatch.setattr(
        cli,
        "_fresh_role_heartbeats",
        lambda _name: {"fix": heartbeat, "review": heartbeat},
    )
    monkeypatch.setattr(cli, "log_dir", lambda: str(tmp_path / "logs"))
    monkeypatch.setattr(
        cli,
        "interventions_status_line",
        lambda _name, _repo: "  interventions: 0",
    )

    rc = cli.cmd_status(
        argparse.Namespace(
            repo="acme/app", target_dir="/srv/acme-app", name="app", lines=5
        )
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "fix     : running (fresh heartbeat; PID not visible)" in output
    assert "review  : running (fresh heartbeat; PID not visible)" in output


def test_systemd_status_log_reads_the_installed_unit_journal(monkeypatch) -> None:
    calls: list[tuple[list[str], float]] = []

    class Result:
        def __init__(self, stdout: str):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        if command[0] == "systemctl":
            return Result("ActiveState=active\nLoadState=loaded\n")
        if command[0] == "journalctl":
            return Result("fix: live service output\n")
        raise AssertionError(command)

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli._systemd_status_log("app", "fix", 7) == (
        "systemd journal: issue-agent-fix-watcher-app.service",
        "fix: live service output",
    )
    assert calls == [
        ([
            "systemctl",
            "--user",
            "show",
            "issue-agent-fix-watcher-app.service",
            "--property=LoadState",
            "--property=ActiveState",
        ], cli._STATUS_COMMAND_TIMEOUT_S),
        ([
            "journalctl",
            "--user",
            "--unit",
            "issue-agent-fix-watcher-app.service",
            "--lines",
            "7",
            "--no-pager",
            "--output=cat",
        ], cli._STATUS_COMMAND_TIMEOUT_S),
    ]


def test_systemd_status_log_reports_query_failure_instead_of_falling_back(
    monkeypatch,
) -> None:
    class Result:
        returncode = 1
        stdout = ""
        stderr = "Failed to connect to user bus"

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda _command, **_kwargs: Result(),
    )

    source, output = cli._systemd_status_log("app", "fix", 7) or ("", "")
    assert source == (
        "systemd status unavailable: issue-agent-fix-watcher-app.service"
    )
    assert output == "  Failed to connect to user bus"


def test_cmd_status_prefers_service_journal_over_stale_foreground_log(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fix-app.log").write_text("stale canary incident\n")
    monkeypatch.setattr(cli, "log_dir", lambda: str(logs))
    monkeypatch.setattr(
        cli,
        "_role_pids",
        lambda _name: {"fix": [101], "review": []},
    )
    monkeypatch.setattr(cli, "_launcher_pids", lambda _name: [])
    monkeypatch.setattr(cli, "_fresh_role_heartbeats", lambda _name: {})
    monkeypatch.setattr(
        cli,
        "_systemd_status_log",
        lambda _name, role, _lines: (
            ("systemd journal: fix.service", "live service output")
            if role == "fix"
            else None
        ),
    )
    monkeypatch.setattr(
        cli,
        "interventions_status_line",
        lambda _name, _repo: "  interventions: 0",
    )

    assert cli.cmd_status(
        argparse.Namespace(
            repo="acme/app",
            target_dir="/srv/acme-app",
            name="app",
            lines=5,
        )
    ) == 0

    output = capsys.readouterr().out
    assert "live service output" in output
    assert "stale canary incident" not in output


def test_cmd_status_prefers_live_foreground_log_over_inactive_installed_unit(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fix-app.log").write_text("live foreground output\n")
    monkeypatch.setattr(cli, "log_dir", lambda: str(logs))
    monkeypatch.setattr(
        cli,
        "_role_pids",
        lambda _name: {"fix": [101], "review": []},
    )
    monkeypatch.setattr(cli, "_launcher_pids", lambda _name: [99])
    monkeypatch.setattr(cli, "_fresh_role_heartbeats", lambda _name: {})
    service_queries: list[str] = []

    def inactive_service(_name, role, _lines):
        service_queries.append(role)
        return None

    monkeypatch.setattr(cli, "_systemd_status_log", inactive_service)
    monkeypatch.setattr(
        cli,
        "interventions_status_line",
        lambda _name, _repo: "  interventions: 0",
    )

    assert cli.cmd_status(
        argparse.Namespace(
            repo="acme/app",
            target_dir="/srv/acme-app",
            name="app",
            lines=5,
        )
    ) == 0
    assert "live foreground output" in capsys.readouterr().out
    assert service_queries == ["fix", "review"]


def test_cmd_status_shows_foreground_log_alongside_unavailable_systemd(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fix-app.log").write_text("live foreground output\n")
    monkeypatch.setattr(cli, "log_dir", lambda: str(logs))
    monkeypatch.setattr(
        cli,
        "_role_pids",
        lambda _name: {"fix": [101], "review": []},
    )
    monkeypatch.setattr(cli, "_launcher_pids", lambda _name: [99])
    monkeypatch.setattr(cli, "_fresh_role_heartbeats", lambda _name: {})
    monkeypatch.setattr(
        cli,
        "_systemd_status_log",
        lambda _name, role, _lines: (
            ("systemd status unavailable: fix.service", "user bus failed")
            if role == "fix"
            else None
        ),
    )
    monkeypatch.setattr(
        cli,
        "interventions_status_line",
        lambda _name, _repo: "  interventions: 0",
    )

    assert cli.cmd_status(
        argparse.Namespace(
            repo="acme/app",
            target_dir="/srv/acme-app",
            name="app",
            lines=5,
        )
    ) == 0

    output = capsys.readouterr().out
    assert "user bus failed" in output
    assert "live foreground output" in output


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


def test_explicit_target_dir_is_the_repository_detection_authority(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    detected_from: list[str] = []
    monkeypatch.setattr(cli, "detect_target_dir", lambda _cwd: "/wrong/cwd")

    def detect_repo(path: str) -> str:
        detected_from.append(path)
        return "acme/target"

    monkeypatch.setattr(cli, "detect_repo", detect_repo)
    monkeypatch.setattr(
        cli,
        "resolve_name",
        lambda repo, explicit, target_dir: "target-deployment",
    )

    context = cli._resolve_context(
        argparse.Namespace(repo="", target_dir=str(target), name="")
    )

    assert context == (
        "acme/target",
        str(target.resolve()),
        "target-deployment",
    )
    assert detected_from == [str(target.resolve())]


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


def test_doctor_aborts_on_compose_failure_without_checking_inherited_snapshot(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "entrypoint_drift", lambda: "")
    monkeypatch.setattr(cli, "check_overlay_mismatch", lambda *_args: "")
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text('REPO=o/r\n')
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    monkeypatch.setenv("AGENT_ENV", str(tmp_path / "another-deployment.env"))

    def run(cmd, **kwargs):
        assert Path(cmd[1]).name == "compose-env.sh", "preflight ran after failed compose"
        return argparse.Namespace(returncode=7)

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.cmd_doctor(_install_args(tmp_path)) == 7
    assert "refusing to check a stale or missing" in capsys.readouterr().err


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


# ── setup wizard: identity detection + first-line token validation ────────────
# doctor.sh validated REVIEW_BOT_TOKEN only after the fact, so `init` could write
# a config doctor was guaranteed to redden. These cover the wizard's own checks.
def _gh_stub(fixer: str = "fix-bot", token_logins: dict | None = None):
    """Stand in for `gh api user`: the ambient call answers `fixer`; a call
    carrying GH_TOKEN answers whichever login that token belongs to (and mimics
    gh's real failure shape — error body on stdout, non-zero exit)."""
    tokens = token_logins or {}

    def run(cmd, **kwargs):
        token = (kwargs.get("env") or {}).get("GH_TOKEN", "")
        if token:
            login = tokens.get(token, "")
            return _fake_proc(
                (login or '{"message":"Bad credentials"}') + "\n", 0 if login else 1
            )
        return _fake_proc(fixer + "\n", 0 if fixer else 1)

    return run


class _Answers:
    """Scripted operator. Exhaustion is EOF — the same thing a piped stdin or a
    Ctrl-D mid-wizard does."""

    def __init__(self, items):
        self.items = list(items)
        self.prompts: list[str] = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self.items:
            raise EOFError
        return self.items.pop(0)


def _codex_report(*, path="/usr/bin/codex", version="0.147.0", findings=()):
    return codex_resolution.analyse_codex_resolution(
        path if path.startswith("/") else "codex",
        path, version,
        tuple(codex_resolution.CodexCandidate(p, v) for p, v in findings),
    )


def _init_env(tmp_path, monkeypatch, *, answers, secrets=(), gh=None, codex=None):
    shared = tmp_path / "config" / "shared.env"
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    monkeypatch.setattr(cli.subprocess, "run", gh or _gh_stub())
    monkeypatch.setattr(cli, "detect_claude_auth", lambda: True)
    monkeypatch.setattr(cli, "detect_codex_auth", lambda: "apikey")
    report = codex if codex is not None else _codex_report()
    monkeypatch.setattr(cli, "report_codex_resolution", lambda _bin: report)
    asked = _Answers(answers)
    secret_asked = _Answers(secrets)
    monkeypatch.setattr("builtins.input", asked)
    monkeypatch.setattr(cli.getpass, "getpass", secret_asked)
    return shared, asked, secret_asked


def _wizard_answers(**over) -> list[str]:
    base = {
        "fix": "fix-bot", "review": "review-bot",
        "app": "N",
        "fixer_runtime": "claude", "reviewer_runtime": "claude",
    }
    base.update(over)
    return list(base.values())


def _shared_values(text: str) -> dict:
    return _source_env(text)


def test_cmd_init_prefills_fixer_login_from_gh(tmp_path, monkeypatch, capsys):
    """Enter on the FIX_BOT prompt accepts the account gh is authenticated as."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch,
        answers=_wizard_answers(fix=""), secrets=["", ""],
        gh=_gh_stub(fixer="ambient-dev"),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["FIX_BOT"] == "ambient-dev"
    assert "ambient-dev" in capsys.readouterr().out


def test_cmd_init_degrades_when_gh_is_unreachable(tmp_path, monkeypatch, capsys):
    """gh missing/unauthenticated must warn and keep going, not crash the wizard."""
    def boom(cmd, **kwargs):
        raise OSError("gh: not found")

    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(), secrets=["", ""], gh=boom,
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["FIX_BOT"] == "fix-bot"
    assert "gh api user" in capsys.readouterr().err


def test_cmd_init_reprompts_when_reviewer_is_the_fixer(tmp_path, monkeypatch, capsys):
    """A reviewer that IS the fixer 422s every approval — including when the two
    differ only by case or a [bot] suffix."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch,
        answers=["fix-bot", "Fix-Bot[bot]", "review-bot",
                 "N", "claude", "claude"],
        secrets=["", ""],
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["REVIEW_BOT"] == "review-bot"
    assert "422" in capsys.readouterr().err


def test_cmd_init_reprompt_loop_is_bounded_and_keeps_the_last_answer(
    tmp_path, monkeypatch, capsys
):
    """A stubborn operator must not be able to hold the wizard open forever."""
    shared, asked, _s = _init_env(
        tmp_path, monkeypatch,
        answers=["fix-bot", "fix-bot", "fix-bot", "fix-bot",
                 "N", "claude", "claude"],
        secrets=["", ""],
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["REVIEW_BOT"] == "fix-bot"
    assert "doctor" in capsys.readouterr().err
    reviewer_prompts = [p for p in asked.prompts if "Reviewer login" in p]
    assert len(reviewer_prompts) == 3  # bounded, then the answer stands


def test_cmd_init_writes_a_token_that_authenticates_as_the_reviewer(
    tmp_path, monkeypatch
):
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(), secrets=["good-token"],
        gh=_gh_stub(token_logins={"good-token": "Review-Bot[bot]"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["REVIEW_BOT_TOKEN"] == "good-token"


def test_cmd_init_reprompts_a_token_belonging_to_another_account(
    tmp_path, monkeypatch, capsys
):
    """The doctor-red config this wizard used to produce: a valid token for the
    WRONG account. Caught at input time now."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=["someone-elses", "good-token"],
        gh=_gh_stub(token_logins={"someone-elses": "other-human",
                                  "good-token": "review-bot"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["REVIEW_BOT_TOKEN"] == "good-token"
    captured = capsys.readouterr()
    assert "other-human" in captured.err
    assert "someone-elses" not in captured.err + captured.out


def test_cmd_init_discards_a_rejected_token_at_the_attempt_ceiling(
    tmp_path, monkeypatch, capsys
):
    """A token GitHub answered about and rejected is NEVER stored. Persisting one
    silently installs whatever account it belongs to (typically the operator's
    own PAT) as the reviewer identity, defeating three-identity mode."""
    shared, _asked, secrets = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=["t1", "t2", "t3", "t4"], gh=_gh_stub(token_logins={}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    text = shared.read_text()
    assert "REVIEW_BOT_TOKEN" not in _shared_values(text)
    assert "t3" not in text
    assert secrets.items == []  # reviewer bounded at three, then the fixer prompt
    # The fixer prompt has the same contract: t4 is rejected by the same stub, so
    # neither identity may end up holding a token GitHub refused.
    assert "FIX_BOT_TOKEN" not in _shared_values(text)
    assert "t4" not in text
    assert "no REVIEW_BOT_TOKEN stored" in capsys.readouterr().err


def test_cmd_init_discards_a_token_belonging_to_another_account(
    tmp_path, monkeypatch, capsys
):
    """The review's scenario: an operator's personal PAT offered three times. It
    authenticates fine — for the WRONG identity — so it must not be written."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=["personal-pat"] * 3,
        gh=_gh_stub(token_logins={"personal-pat": "the-operator"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert "personal-pat" not in shared.read_text()
    assert "the-operator" in capsys.readouterr().err


def test_cmd_init_skip_after_a_rejected_token_stores_nothing(
    tmp_path, monkeypatch, capsys
):
    """Enter (or EOF) after a rejection is a SKIP, not consent to the rejected
    value."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=["wrong-token", ""],
        gh=_gh_stub(token_logins={"wrong-token": "someone-else"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    text = shared.read_text()
    assert "wrong-token" not in text
    assert "REVIEW_BOT_TOKEN" not in _shared_values(text)
    assert "no REVIEW_BOT_TOKEN stored" in capsys.readouterr().err


def test_cmd_init_keeps_an_unverifiable_token_when_gh_cannot_run(
    tmp_path, monkeypatch, capsys
):
    """gh absent is an outage, not a verdict: keep the token, loudly, and let
    doctor adjudicate. This is the ONLY path that stores an unproven token."""
    def unusable(cmd, **kwargs):
        if (kwargs.get("env") or {}).get("GH_TOKEN"):
            raise OSError("gh: not found")
        return _fake_proc("fix-bot\n")

    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=["offline-token"] * 3, gh=unusable,
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["REVIEW_BOT_TOKEN"] == "offline-token"
    assert "UNVERIFIED" in capsys.readouterr().err


def test_cmd_init_reads_a_token_out_of_a_file(tmp_path, monkeypatch):
    token_file = tmp_path / "pat.txt"
    token_file.write_text("file-token\n")
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=[f"@{token_file}"],
        gh=_gh_stub(token_logins={"file-token": "review-bot"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["REVIEW_BOT_TOKEN"] == "file-token"


def test_cmd_init_reports_a_missing_token_file_and_reprompts(
    tmp_path, monkeypatch, capsys
):
    token_file = tmp_path / "pat.txt"
    token_file.write_text("file-token\n")
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=[f"@{tmp_path / 'nope.txt'}", f"@{token_file}"],
        gh=_gh_stub(token_logins={"file-token": "review-bot"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["REVIEW_BOT_TOKEN"] == "file-token"
    message = capsys.readouterr().err
    assert "no file exists at the path you entered" in message
    assert "nope.txt" not in message  # never echo what was typed


def test_cmd_init_never_echoes_token_material(tmp_path, monkeypatch, capsys):
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(), secrets=["s3cret-pat"],
        gh=_gh_stub(token_logins={"s3cret-pat": "review-bot"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    captured = capsys.readouterr()
    assert "s3cret-pat" not in captured.out + captured.err
    assert "s3cret-pat" in _shared_values(shared.read_text())["REVIEW_BOT_TOKEN"]


def test_cmd_init_collects_a_fixer_token_and_reports_the_account_it_belongs_to(
    tmp_path, monkeypatch, capsys
):
    """2026-08-02 keifu: with FIX_BOT_TOKEN unset the fix daemon fell through to
    the ambient `gh auth` credential, acted as the human operator, and burned
    that account's personal GraphQL pool. The wizard now asks."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=["review-pat", "fixer-pat"],
        gh=_gh_stub(token_logins={
            "review-pat": "review-bot", "fixer-pat": "fix-bot-account",
        }),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    values = _shared_values(shared.read_text())
    assert values["FIX_BOT_TOKEN"] == "fixer-pat"
    out = capsys.readouterr()
    assert "fixer token authenticates as 'fix-bot-account'" in out.out
    # …and with both identities distinct, the attribution finding is gone.
    assert "FIX_BOT_TOKEN is not set" not in out.err


def test_cmd_init_names_the_consequence_when_the_fixer_token_is_skipped(
    tmp_path, monkeypatch, capsys
):
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(),
        secrets=["review-pat"],
        gh=_gh_stub(token_logins={"review-pat": "review-bot"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert "FIX_BOT_TOKEN" not in _shared_values(shared.read_text())
    stderr = capsys.readouterr().err
    assert "no FIX_BOT_TOKEN stored" in stderr
    assert "FIX_BOT_TOKEN is not set" in stderr  # the doctor finding, at config time


def test_cmd_init_skips_the_reviewer_token_prompt_when_a_github_app_is_configured(
    tmp_path, monkeypatch
):
    """The App supplies the review identity, so REVIEW_BOT_TOKEN is not
    asked for. The FIXER's is: the App covers review only, and
    `main` builds the fix gateway from FIX_BOT_TOKEN or nothing (2026-08-02
    keifu — an App-configured deployment still ran its fixer on the operator's
    ambient credential)."""
    shared, _asked, secrets = _init_env(
        tmp_path, monkeypatch,
        answers=["fix-bot", "review-bot", "y",
                 "123", "456", "/keys/app.pem", "claude", "claude"],
        secrets=["fixer-pat"],
        gh=_gh_stub(token_logins={"fixer-pat": "fix-bot"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    values = _shared_values(shared.read_text())
    assert values["GH_APP_ID"] == "123"
    assert "REVIEW_BOT_TOKEN" not in values
    assert values["FIX_BOT_TOKEN"] == "fixer-pat"
    assert [p for p in secrets.prompts if "REVIEW_BOT_TOKEN" in p] == []


def test_cmd_init_pins_codex_bin_when_the_path_hides_a_newer_install(
    tmp_path, monkeypatch, capsys
):
    """2026-08-02 keifu: the wizard's shell found 0.147.0 while the daemons'
    systemd PATH found 0.142.3, which the provider refuses to serve. Answering
    nothing (or piped stdin) must still write the absolute pin."""
    shadowed = codex_resolution.analyse_codex_resolution(
        "codex", "/run/current-system/sw/bin/codex", "0.142.3",
        (codex_resolution.CodexCandidate("/run/current-system/sw/bin/codex", "0.142.3"),
         codex_resolution.CodexCandidate("/home/o/.local/bin/codex", "0.147.0")),
    )
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch,
        answers=["fix-bot", "review-bot", "N",
                 "claude", "codex"],
        secrets=["", ""], codex=shadowed,
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["CODEX_BIN"] == "/home/o/.local/bin/codex"


def test_cmd_init_leaves_codex_bin_unpinned_when_resolution_is_clean(
    tmp_path, monkeypatch
):
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch,
        answers=["fix-bot", "review-bot", "N",
                 "claude", "codex", "https://api.openai.com/v1"],
        secrets=["", ""],
        codex=_codex_report(path="/opt/codex", version="0.147.0"),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert "CODEX_BIN" not in _shared_values(shared.read_text())


def test_cmd_init_never_asks_about_codex_for_a_claude_only_deployment(
    tmp_path, monkeypatch
):
    """A deployment that never spawns codex has no reason to answer for a binary
    it will not run — and no reason to carry a CODEX_BIN into its config."""
    shadowed = codex_resolution.analyse_codex_resolution(
        "codex", "/usr/bin/codex", "0.142.3",
        (codex_resolution.CodexCandidate("/usr/bin/codex", "0.142.3"),
         codex_resolution.CodexCandidate("/opt/codex", "0.147.0")),
    )
    shared, asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(), secrets=["", ""],
        codex=shadowed,
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert "CODEX_BIN" not in _shared_values(shared.read_text())
    assert not any("CODEX_BIN" in prompt for prompt in asked.prompts)


def test_cmd_init_keeps_codex_unpinned_when_the_operator_declines(
    tmp_path, monkeypatch, capsys
):
    shadowed = codex_resolution.analyse_codex_resolution(
        "codex", "/usr/bin/codex", "0.142.3",
        (codex_resolution.CodexCandidate("/usr/bin/codex", "0.142.3"),
         codex_resolution.CodexCandidate("/opt/codex", "0.147.0")),
    )
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch,
        answers=["fix-bot", "review-bot", "N",
                 "claude", "codex", "n", "https://api.openai.com/v1"],
        secrets=["", ""], codex=shadowed,
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert "CODEX_BIN" not in _shared_values(shared.read_text())
    assert "unpinned" in capsys.readouterr().err


def test_cmd_init_writes_shared_env_private(tmp_path, monkeypatch):
    """shared.env can hold a long-lived PAT: it must never be group/world readable."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(), secrets=["good"],
        gh=_gh_stub(token_logins={"good": "review-bot"}),
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert oct(shared.stat().st_mode & 0o777) == "0o600"


def test_cmd_init_survives_eof_midway_and_still_writes_a_config(
    tmp_path, monkeypatch
):
    """Ctrl-D partway through takes the remaining defaults instead of exploding."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=["fix-bot"], secrets=[],
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    values = _shared_values(shared.read_text())
    assert values["FIX_BOT"] == "fix-bot"
    assert "REVIEW_BOT_TOKEN" not in values


def test_cmd_init_non_tty_names_the_real_shared_template(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SHARED_ENV", str(tmp_path / "shared.env"))
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: False))

    assert cli.cmd_init(argparse.Namespace(force=False)) == 2
    message = capsys.readouterr().err
    assert "config/shared.example.env" in message
    assert "agent.env.example" not in message
    assert (Path(cli.HARNESS_ROOT) / "config" / "shared.example.env").is_file()


def test_cmd_init_codex_on_chatgpt_auth_explains_the_synthetic_rate_card(
    tmp_path, monkeypatch, capsys
):
    """ChatGPT-auth codex reports no billed cost, so spend is priced from the
    rate card and there is no gateway URL to ask for."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch,
        answers=_wizard_answers(reviewer_runtime="codex"), secrets=["", ""],
    )
    monkeypatch.setattr(cli, "detect_codex_auth", lambda: "chatgpt")

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert "OPENAI_BASE_URL" not in _shared_values(shared.read_text())
    assert "CODEX_COST_PER_MTOK" in capsys.readouterr().out


def test_cmd_init_codex_on_api_key_still_asks_for_the_gateway(tmp_path, monkeypatch):
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch,
        answers=_wizard_answers(reviewer_runtime="codex") + ["https://gw.local/v1"],
        secrets=["", ""],
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text())["OPENAI_BASE_URL"] == "https://gw.local/v1"


def test_cmd_init_uses_the_suggested_reviewer_login_as_the_default(
    tmp_path, monkeypatch
):
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(review=""), secrets=["", ""],
    )

    assert cli.cmd_init(
        argparse.Namespace(force=False, review_bot_default="suggested-bot")
    ) == 0
    assert _shared_values(shared.read_text())["REVIEW_BOT"] == "suggested-bot"


# ── runtime auth detection ────────────────────────────────────────────────────
def test_detect_claude_auth_accepts_an_environment_key(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    assert cli.detect_claude_auth() is True

    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    assert cli.detect_claude_auth() is False


def test_detect_claude_auth_accepts_stored_cli_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    assert cli.detect_claude_auth() is True


def test_detect_codex_auth_reads_the_mode_not_the_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "secret"}})
    )
    assert cli.detect_codex_auth() == "chatgpt"


def test_detect_codex_auth_tolerates_a_malformed_auth_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{not json")
    assert cli.detect_codex_auth() == ""


def test_detect_codex_auth_does_not_answer_for_binary_presence(monkeypatch, tmp_path):
    """Presence belongs to the resolution report, which asks about the DAEMONS'
    PATH; `shutil.which` here would only describe the operator's shell."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda _b: None)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    assert cli.detect_codex_auth() == "apikey"


# ── token input handling (pure) ───────────────────────────────────────────────
def test_read_token_input_strips_a_pasted_secret():
    assert cli.read_token_input("  ghp_abc  ") == ("ghp_abc", "")


def test_read_token_input_reads_a_bare_existing_path(tmp_path):
    path = tmp_path / "pat"
    path.write_text("from-file\n\n")
    assert cli.read_token_input(str(path)) == ("from-file", "")


def test_read_token_input_reports_a_missing_at_path_without_echoing_it(tmp_path):
    """A stray leading @ on a pasted PAT must not put the secret in a message."""
    token, problem = cli.read_token_input(f"@{tmp_path / 'absent'}")
    assert token == ""
    assert problem
    assert "absent" not in problem
    assert str(tmp_path) not in problem


def test_read_token_input_treats_a_bare_nonexistent_path_as_the_token():
    """A PAT is opaque text; only an EXISTING path (or an explicit @) is a file."""
    assert cli.read_token_input("/not/a/real/path") == ("/not/a/real/path", "")


def test_normalize_login_matches_doctor_semantics():
    assert cli.normalize_login(" Review-Bot[bot] ") == "review-bot"
    assert cli.normalize_login("review-bot") == "review-bot"


def test_gh_login_for_token_rejects_an_error_body_as_a_login(monkeypatch):
    """gh writes its error body to stdout: it must never be read as a login, and
    the call still counts as ANSWERED (a verdict, not an outage)."""
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda *a, **k: _fake_proc('{"message":"Bad credentials"}\n', 0),
    )
    assert cli.gh_login_for_token("bad") == ("", True)


def test_gh_login_for_token_reports_an_unrunnable_gh_as_unanswered(monkeypatch):
    """gh absent is an OUTAGE, not a verdict — the caller may keep the token."""
    def boom(*a, **k):
        raise OSError("gh: not found")

    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli.gh_login_for_token("whatever") == ("", False)


def test_gh_login_for_token_reports_a_github_refusal_as_answered(monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: _fake_proc("gh: HTTP 401\n", 1)
    )
    assert cli.gh_login_for_token("expired") == ("", True)


# ── base-branch / reviewer inference from repo history ────────────────────────
def _gh_run_stub(pr_list="", default_branch="", existing_branches=()):
    """Stand in for the three gh reads base-branch inference makes, dispatching on
    the REAL argv so a renamed --json field cannot silently pass."""
    def run(cmd, cwd=None):
        if cmd[:4] == ["gh", "pr", "list", "--state"]:
            assert cmd[4] == "merged"
            assert cmd[cmd.index("--json") + 1] == "baseRefName"
            return pr_list
        if cmd[:3] == ["gh", "repo", "view"]:
            return default_branch
        if cmd[:2] == ["gh", "api"]:
            branch = cmd[2].rsplit("/", 1)[-1]
            return branch if branch in existing_branches else ""
        raise AssertionError(f"unexpected gh call: {cmd}")

    return run


def test_detect_base_branch_takes_the_majority_of_merged_prs(monkeypatch):
    monkeypatch.setattr(cli, "_run", _gh_run_stub(
        pr_list=json.dumps(
            [{"baseRefName": "main"}, {"baseRefName": "main"}, {"baseRefName": "dev"}]
        ),
        existing_branches=("main",),
    ))
    branch, note = cli.detect_base_branch("/t", "acme/app")
    assert branch == "main"
    assert "2/3" in note


def test_detect_base_branch_skips_a_branch_that_is_not_on_the_remote(monkeypatch):
    """An inferred base that does not exist would point every PR at a missing ref
    (and trip point-at.sh's hard stop) — fall through to the next candidate."""
    monkeypatch.setattr(cli, "_run", _gh_run_stub(
        pr_list=json.dumps([{"baseRefName": "deleted-release"}]),
        default_branch="trunk",
        existing_branches=("trunk",),
    ))
    assert cli.detect_base_branch("/t", "acme/app") == (
        "trunk", "the repository default branch"
    )


def test_detect_base_branch_falls_back_to_the_default_branch(monkeypatch):
    monkeypatch.setattr(cli, "_run", _gh_run_stub(
        pr_list="[]", default_branch="trunk", existing_branches=("trunk",),
    ))
    assert cli.detect_base_branch("/t", "acme/app") == (
        "trunk", "the repository default branch"
    )


def test_detect_base_branch_tolerates_malformed_gh_json(monkeypatch):
    monkeypatch.setattr(cli, "_run", _gh_run_stub(
        pr_list="not json at all", default_branch="trunk",
        existing_branches=("trunk",),
    ))
    assert cli.detect_base_branch("/t", "acme/app")[0] == "trunk"


def test_detect_base_branch_last_resort_is_dev_with_no_provenance(monkeypatch):
    """Nothing confirmed → the harness default, and NO provenance comment:
    'inferred from the harness default' is not an inference."""
    monkeypatch.setattr(cli, "_run", _gh_run_stub())
    assert cli.detect_base_branch("/t", "acme/app") == ("dev", "")


def test_detect_base_branch_without_a_repo_cannot_confirm_anything(monkeypatch):
    monkeypatch.setattr(cli, "_run", _gh_run_stub(
        pr_list=json.dumps([{"baseRefName": "main"}]), existing_branches=("main",),
    ))
    assert cli.detect_base_branch("/t", "") == ("dev", "")


def test_detect_review_bot_picks_the_most_frequent_non_fixer_reviewer(monkeypatch):
    payload = json.dumps([
        {"reviews": [{"author": {"login": "fix-bot"}},
                     {"author": {"login": "review-bot"}}]},
        {"reviews": [{"author": {"login": "review-bot"}}]},
        {"reviews": [{"author": {"login": "someone"}}]},
    ])
    seen: list[list[str]] = []

    def run(cmd, cwd=None):
        seen.append(cmd)
        return payload

    monkeypatch.setattr(cli, "_run", run)
    assert cli.detect_review_bot("/t", "Fix-Bot") == "review-bot"
    # Real argv: a renamed field would return nothing and silently no-op.
    assert seen[0][:4] == ["gh", "pr", "list", "--state"]
    assert seen[0][4] == "merged"
    assert seen[0][seen[0].index("--json") + 1] == "reviews"


def test_detect_review_bot_is_empty_when_only_the_fixer_reviews(monkeypatch):
    payload = json.dumps([{"reviews": [{"author": {"login": "fix-bot"}}]}])
    monkeypatch.setattr(cli, "_run", lambda cmd, cwd=None: payload)
    assert cli.detect_review_bot("/t", "fix-bot") == ""


def test_detect_review_bot_tolerates_gh_failure_and_malformed_json(monkeypatch):
    monkeypatch.setattr(cli, "_run", lambda cmd, cwd=None: "")
    assert cli.detect_review_bot("/t", "fix-bot") == ""
    monkeypatch.setattr(cli, "_run", lambda cmd, cwd=None: "{oops")
    assert cli.detect_review_bot("/t", "fix-bot") == ""


# ── overlay: inferred keys ────────────────────────────────────────────────────
def test_synth_repo_env_records_base_branch_provenance():
    text = cli.synth_repo_env(
        repo="o/r", target_dir="/t", name="r", base_branch="main",
        base_branch_note="7/10 of the last merged PRs",
    )
    assert "# BASE_BRANCH inferred from 7/10 of the last merged PRs." in text
    assert _source_env(text)["BASE_BRANCH"] == "main"


# ── cmd_start first-run base-branch inference ────────────────────────────────────────────
def _fresh_start_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path / "repos"))
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    monkeypatch.setattr(cli.os, "execve", lambda *a: None)
    return Path(cli.REPOS_DIR) / "app.env"


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
        )
    )

    assert rc is None
    assert events[0] == ("stop:app", original)
    assert events[1][0] == "exec"
    assert events[1][1] != original
    assert _source_env(events[1][1])["REPO"] == "acme/app"


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
        )
    )

    assert rc == 1
    assert overlay.read_text() == original
    assert execve_calls == []


def test_cmd_start_never_adopts_an_inferred_base_branch_unattended(
    tmp_path, monkeypatch, capsys
):
    """point-at.sh hard-stops on a BASE_BRANCH that does not exist — that stop is
    the operator's conscious-choice gate on where this harness MERGES. Inference
    with nobody at the terminal must not walk past it: keep the default, record
    the finding as an inert comment."""
    overlay = _fresh_start_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "detect_base_branch", lambda _t, _r: ("main", "9/10 of the last merged PRs"))

    cli.cmd_start(_start_args(
        repo="acme/app", target_dir="/srv/acme-app", name="app",
        base_branch=None,
    ))

    text = overlay.read_text()
    assert _source_env(text)["BASE_BRANCH"] == "dev"
    assert "# detected merge target of recent PRs: main" in text
    assert "main" in capsys.readouterr().out


def test_cmd_start_records_no_suggestion_when_nothing_was_inferred(
    tmp_path, monkeypatch
):
    """Anti-vacuity twin: with no confirmed candidate there is no suggestion and
    no provenance comment — 'inferred from the harness default' is not a fact."""
    overlay = _fresh_start_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "detect_base_branch", lambda _t, _r: ("dev", ""))

    cli.cmd_start(_start_args(
        repo="acme/app", target_dir="/srv/acme-app", name="app",
        base_branch=None,
    ))

    text = overlay.read_text()
    assert _source_env(text)["BASE_BRANCH"] == "dev"
    assert "detected merge target" not in text
    assert "inferred from" not in text


def test_cmd_start_never_infers_over_an_explicit_base_branch(tmp_path, monkeypatch):
    overlay = _fresh_start_env(tmp_path, monkeypatch)

    def refuse(*a, **k):
        raise AssertionError("must not infer when --base-branch was passed")

    monkeypatch.setattr(cli, "detect_base_branch", refuse)

    cli.cmd_start(_start_args(
        repo="acme/app", target_dir="/srv/acme-app", name="app",
        base_branch="release",
    ))

    assert _source_env(overlay.read_text())["BASE_BRANCH"] == "release"


def test_cmd_start_write_config_preserves_the_existing_base_branch(
    tmp_path, monkeypatch
):
    """A regenerate must not silently re-point a live deployment's merge target:
    the previously written BASE_BRANCH stands unless --base-branch says otherwise."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    overlay = tmp_path / "app.env"
    overlay.write_text(
        'REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\nBASE_BRANCH="release-2"\n'
    )
    monkeypatch.setattr(cli.os, "execve", lambda *a: None)
    monkeypatch.setattr(cli, "_stop_deployment", lambda _n: 0)

    def refuse(*a, **k):
        raise AssertionError("must not re-infer over a written BASE_BRANCH")

    monkeypatch.setattr(cli, "detect_base_branch", refuse)

    cli.cmd_start(_start_args(
        repo="acme/app", target_dir="/srv/acme-app", name="app",
        base_branch=None, write_config=True,
    ))

    assert _source_env(overlay.read_text())["BASE_BRANCH"] == "release-2"


def test_cmd_start_write_config_honours_an_explicit_base_branch_override(
    tmp_path, monkeypatch
):
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    overlay = tmp_path / "app.env"
    overlay.write_text(
        'REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\nBASE_BRANCH="release-2"\n'
    )
    monkeypatch.setattr(cli.os, "execve", lambda *a: None)
    monkeypatch.setattr(cli, "_stop_deployment", lambda _n: 0)

    cli.cmd_start(_start_args(
        repo="acme/app", target_dir="/srv/acme-app", name="app",
        base_branch="main", write_config=True,
    ))

    assert _source_env(overlay.read_text())["BASE_BRANCH"] == "main"


def test_cmd_start_confirms_the_inferred_base_branch_on_a_tty(tmp_path, monkeypatch):
    overlay = _fresh_start_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "detect_base_branch", lambda _t, _r: ("main", "history"))
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", _Answers(["trunk"]))

    cli.cmd_start(_start_args(
        repo="acme/app", target_dir="/srv/acme-app", name="app",
        base_branch=None,
    ))

    assert _source_env(overlay.read_text())["BASE_BRANCH"] == "trunk"


def test_cmd_start_adopts_a_confirmed_base_branch_on_a_tty(tmp_path, monkeypatch):
    overlay = _fresh_start_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli, "detect_base_branch", lambda _t, _r: ("main", "9/10 of the last merged PRs")
    )
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", _Answers([""]))

    cli.cmd_start(_start_args(
        repo="acme/app", target_dir="/srv/acme-app", name="app",
        base_branch=None,
    ))

    text = overlay.read_text()
    assert _source_env(text)["BASE_BRANCH"] == "main"
    assert "# BASE_BRANCH inferred from 9/10 of the last merged PRs." in text


def test_cmd_start_does_not_rediscover_when_reusing_an_overlay(
    tmp_path, monkeypatch, capsys
):
    """Base-branch inference is first-run only: a reused overlay is authority."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.os, "execve", lambda *a: None)

    def refuse(*a, **k):
        raise AssertionError("must not probe on overlay reuse")

    monkeypatch.setattr(cli, "detect_base_branch", refuse)

    cli.cmd_start(_start_args(repo="acme/app", target_dir="/srv/acme-app", name="app"))

    assert "reusing config/repos/app.env" in capsys.readouterr().out


def test_cmd_init_tightens_an_existing_shared_env_before_writing_secrets(
    tmp_path, monkeypatch
):
    """--force over a 0644 file: O_CREAT's mode applies only to a NEW file, so
    without a chmod FIRST the PAT would land in a world-readable file and only be
    protected afterwards. Asserted at open time, not just at the end."""
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers(), secrets=["good"],
        gh=_gh_stub(token_logins={"good": "review-bot"}),
    )
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text("stale\n")
    os.chmod(shared, 0o644)
    modes: list[str] = []
    real_open = os.open

    def spy(path, flags, mode=0o777):
        if str(path) == str(shared):
            modes.append(oct(os.stat(path).st_mode & 0o777))
        return real_open(path, flags, mode)

    monkeypatch.setattr(cli.os, "open", spy)

    assert cli.cmd_init(argparse.Namespace(force=True)) == 0
    assert modes == ["0o600"]  # already private when the descriptor was taken
    assert oct(shared.stat().st_mode & 0o777) == "0o600"
    assert "good" in shared.read_text()


def test_main_exits_130_on_keyboard_interrupt(monkeypatch, capsys):
    """Ctrl-C at a wizard prompt is an ordinary exit, not a crash."""
    def interrupted(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_status", interrupted)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["status"])
    assert excinfo.value.code == 130
    assert "aborted" in capsys.readouterr().err


def test_start_parses_with_no_name_flag():
    """argparse runs `type` over the string default too: this used to abort every
    `issue-agent start`/`status`/`stop` that did not pass --name."""
    args = cli.build_parser().parse_args(["start"])
    assert args.name == ""
    assert cli.build_parser().parse_args(["status"]).name == ""


def test_start_still_rejects_a_non_canonical_name():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["start", "--name", "Weird Name"])


# ── discovered check commands must be check-mode, and trialed off the tree ────
def _init_git_repo(path):
    """A real one-commit repo — the trial-run tests need genuine git worktrees."""
    path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (path / "tracked.txt").write_text("pristine\n")
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["commit", "-qm", "seed"]):
        subprocess.run(["git", *argv], cwd=path, env=env, check=True,
                       capture_output=True)
    return path


def test_gh_login_rejects_a_literal_null_from_jq(monkeypatch):
    """`gh api user --jq .login` prints `null` for an absent field, and `null`
    passes the login charset — it must not be compared as an account."""
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: _fake_proc("null\n", 0))
    assert cli.gh_login_for_token("t") == ("", True)
    assert cli.detect_fixer_login() == ""


# ── `issue-agent status` reports operator interventions (unattended metric) ───
def _operator_unblock_journal(state_dir: Path, issue: int, seq: int) -> None:
    """One journalled dashboard unblock, written the way the daemons write it."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "journal.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "ts": f"2026-07-25T10:0{seq}:00+00:00",
            "event": "action",
            "desc": f"operator unblock issue #{issue}: -ai-blocked",
            "command_id": f"operator-unblock:{seq:x}",
            "issue": issue,
            "action": {
                "type": "operator_unblock",
                "add": [],
                "remove": ["ai-blocked"],
                "operator": "acme/app:dashboard",
            },
        }) + "\n")


def test_cmd_status_reports_operator_interventions(tmp_path, monkeypatch, capsys):
    """The unattended-operation metric must be readable without the web
    dashboard: `status` is what an operator runs over ssh mid-incident."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    state = tmp_path / "state"
    _operator_unblock_journal(state, 7, 1)
    _operator_unblock_journal(state, 8, 2)
    monkeypatch.setattr(
        cli, "journal_paths", lambda name: [str(state / "journal.jsonl")]
    )
    monkeypatch.setattr(cli, "log_dir", lambda: str(tmp_path / "logs"))

    rc = cli.cmd_status(
        argparse.Namespace(
            repo="acme/app", target_dir="/srv/acme-app", name="app", lines=5
        )
    )

    assert rc == 0
    assert (
        "interventions: 2 (rearm 2, policy 0, other 0) — no merged PRs yet"
        in capsys.readouterr().out
    )
