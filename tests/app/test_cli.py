"""CLI helpers and commands: repo detection, overlay-name resolution, the
synthesized per-repo env, and the per-command overlay guards.

The key invariant: synth_repo_env's keys must round-trip through config.from_env
(the daemon's reader), so the CLI and the daemon can't drift on key names."""

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest
import sys

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


@pytest.mark.parametrize("args,passed", [([], []), (["--port", "9000"], ["--port", "9000"])])
def test_dashboard_replaces_itself_with_the_dashboard_module(monkeypatch, args, passed):
    calls = []
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: calls.append((path, argv)))
    with pytest.raises(SystemExit):
        cli.main(["dashboard", *args])
    assert calls == [(sys.executable, [sys.executable, "-m", "issue_agent.app.dashweb.main", *passed])]


@pytest.mark.parametrize(
    "url,repo",
    [
        ("git@github.com:acme/MyApp.git", "acme/myapp"),
        ("https://github.com/acme/MyApp.git", "acme/myapp"),
        ("https://github.com/acme/MyApp", "acme/myapp"),
        ("https://github.example.com/o/r/", "o/r"),
        ("not a url", ""),
        ("", ""),
    ],
)
def test_parse_remote_url(url, repo):
    assert cli.parse_remote_url(url) == repo


@pytest.mark.parametrize(
    "repo,name",
    [
        # P0R-2: the owner is part of the name, so acme/app and other/app cannot
        # share one overlay (the original collision).
        ("acme/MyApp-combined-agent", "acme-myapp-combined-agent"),
        ("acme/app", "acme-app"),
        ("other/app", "other-app"),
        ("o/weird name!", "o-weird-name"),
        ("", "target"),
    ],
)
def test_derive_name(repo, name):
    assert cli.derive_name(repo) == name


@pytest.mark.parametrize(
    ("alias", "canonical"), (("Acme App", "acme-app"), ("acme@app", "acme-app"), ("ACME-APP", "acme-app"))
)
def test_explicit_deployment_name_aliases_are_rejected(alias, canonical):
    with pytest.raises(ValueError, match=repr(canonical)):
        cli.resolve_name("acme/app", explicit=alias)
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["install-service", "--name", alias])
    assert exc.value.code == 2


# Review M12: the prior regex line-scanner mis-parsed the comment, spacing and
# expansion rows, so resolve_name MISSED an existing overlay and cmd_start
# synthesized a duplicate with fresh *_STATE_DIRs. The empty rows are the
# anti-vacuity twins (a reader that leaks the CLI's own env would pass the rest).
@pytest.mark.parametrize(
    "text,value",
    [
        ('REPO="a/one"\nexport REPO="a/two"\nOTHER=x\n', "a/two"),
        ('REPO="owner/name"  # prod\n', "owner/name"),
        ("REPO = owner/name\n", "owner/name"),
        ("OWNER_REPO=owner/name\nREPO=${OWNER_REPO}\n", "owner/name"),
        ('OTHER="x"\n', ""),
        (None, ""),
    ],
)
def test_read_repo_env_field(tmp_path, monkeypatch, text, value):
    monkeypatch.setenv("REPO", "ambient/leak")
    p = tmp_path / "r.env"
    if text is not None:
        p.write_text(text)
    assert cli.read_repo_env_field(str(p), "REPO") == value
    if text is not None:
        (tmp_path / "myapp.env").write_text(text)
        expected = "myapp" if value else cli.derive_name("owner/name")
        assert cli.resolve_name(value or "owner/name", repos_dir=str(tmp_path)) == expected


@pytest.mark.parametrize("overlay_dir,warns", [("/repos/app", True), ("/repos/other", False)])
def test_resolve_name_warns_before_synthesizing_a_duplicate_for_the_same_checkout(tmp_path, capsys, overlay_dir, warns):
    """M12: an overlay already targets this exact working copy but its REPO
    disagrees — warn before derive_name synthesizes a SECOND overlay (and a
    second STATE_DIR) for the same checkout; a different checkout is silent."""
    (tmp_path / "myapp.env").write_text(f'REPO="acme/OldName"\nTARGET_DIR="{overlay_dir}"\n')
    name = cli.resolve_name("acme/NewName", repos_dir=str(tmp_path), target_dir="/repos/app")
    assert name == cli.derive_name("acme/NewName")
    err = capsys.readouterr().err
    assert ("myapp" in err and "synthesize" in err.lower()) if warns else err == ""


def test_resolve_name_explicit_wins_then_content_match_then_derived(tmp_path):
    (tmp_path / "myapp.env").write_text('REPO="acme/MyApp-combined-agent"\n')
    (tmp_path / "other.env").write_text('REPO="x/y"\n')
    # P0R-2 back-compat: reuse is by the overlay's REPO content, never by
    # recomputing derive_name against an old basename-only filename.
    (tmp_path / "app.env").write_text('REPO="acme/app"\n')
    assert cli.resolve_name("o/r", explicit="custom", repos_dir=str(tmp_path)) == "custom"
    assert cli.resolve_name("acme/MyApp-combined-agent", repos_dir=str(tmp_path)) == "myapp"
    assert cli.resolve_name("acme/app", repos_dir=str(tmp_path)) == "app"
    assert cli.resolve_name("acme/New-Repo", repos_dir=str(tmp_path)) == "acme-new-repo"


def test_resolve_name_prefers_target_dir_match(tmp_path, capsys):
    """Regression: two overlays share a REPO but point at different working copies.
    The launched-from dir (target_dir) must decide — NOT the alphabetically-first
    match (`-wt` sorts first and silently ran against the wrong TARGET_DIR). A
    known checkout is an identity axis, not a hint: an unknown one derives."""
    repo = "acme/App-combined-agent"
    (tmp_path / "app-wt.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app-harness"\n')
    (tmp_path / "app.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app"\n')
    for target, name in (
        ("/repos/app", "app"),
        ("/repos/app/", "app"),
        ("/repos/./app", "app"),
        ("/repos/app-harness", "app-wt"),
    ):
        assert cli.resolve_name(repo, repos_dir=str(tmp_path), target_dir=target) == name
    assert cli.resolve_name(repo, repos_dir=str(tmp_path), target_dir="/somewhere/else") == cli.derive_name(repo)
    assert "different working copy" in capsys.readouterr().err


def _source_env(text: str) -> dict:
    """Parse the simple KEY=value lines synth_repo_env emits, as from_env reads them."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env[key] = val.strip().strip("'\"")
    return env


def test_synth_repo_env_roundtrips_through_from_env():
    env = _source_env(cli.synth_repo_env(repo="acme/MyApp", target_dir="/repos/myapp", name="myapp"))
    assert (env["REPO"], env["BASE_BRANCH"]) == ("acme/myapp", "dev")
    assert "issue-agent-myapp" in env["STATE_DIR"]
    # The harness never writes TEST_CMD: it is only the operator's point-at probe.
    assert "TEST_CMD" not in env
    cfg = from_env(
        _source_env(cli.synth_repo_env(repo="o/r", target_dir="/t", name="r", base_branch="main", max_budget="10.00"))
    )
    assert (cfg.repo, cfg.target_dir, cfg.base_branch, cfg.fix_budget_usd) == ("o/r", "/t", "main", 10.00)


def test_harness_worktree_path():
    assert cli.harness_worktree_path("/r/MyApp-combined-agent") == "/r/MyApp-combined-agent-harness"
    assert cli.harness_worktree_path("/r/myapp/") == "/r/myapp-harness"
    assert cli.harness_worktree_path("/r/myapp", explicit="/tmp/wt") == "/tmp/wt"


def _fake_proc(stdout, returncode=0):
    return argparse.Namespace(stdout=stdout, returncode=returncode)


def test_journal_paths_fall_back_to_synth_defaults(tmp_path, monkeypatch):
    """No overlay at all → the same per-role layout `synth_repo_env` writes."""
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    fix, review = cli.journal_paths("other")
    assert fix.endswith(os.path.join(".local", "state", "issue-agent-other", "journal.jsonl"))
    assert review.endswith(os.path.join(".local", "state", "review-agent-other", "journal.jsonl"))


def _isolated_config(tmp_path, monkeypatch):
    """Point both config roots at tmp: `config/repos/<name>.env` overlays and
    the composed `config/.active.<name>.env` snapshots."""
    repos, config = tmp_path / "repos", tmp_path / "config"
    repos.mkdir()
    config.mkdir()
    monkeypatch.setattr(cli, "REPOS_DIR", str(repos))
    monkeypatch.setattr(cli, "HARNESS_ROOT", str(tmp_path))
    return repos, config


@pytest.mark.parametrize(
    "overlay,snapshot", [('REPO="Acme/Widget-App"\n', None), ('TARGET_DIR="/srv/myapp"\n', 'REPO="Acme/Widget-App"\n')]
)
def test_journal_paths_match_where_daemons_write(tmp_path, monkeypatch, overlay, snapshot):
    """A hand-written overlay with REPO but no STATE_DIR keys (keifu, 2026-08-02),
    or REPO only in the composed snapshot (it lives in shared.env): the read
    side must resolve the dirs the daemons derive, or every run is invisible
    while spend accrues."""
    repos, config = _isolated_config(tmp_path, monkeypatch)
    (repos / "myapp.env").write_text(overlay)
    if snapshot:
        (config / ".active.myapp.env").write_text(snapshot)
    daemon_cfg = from_env({"REPO": "Acme/Widget-App"})
    assert cli.journal_paths("myapp") == [
        os.path.join(daemon_cfg.state_dir, "journal.jsonl"),
        os.path.join(daemon_cfg.review_state_dir, "journal.jsonl"),
    ]


def test_a_snapshot_missing_one_key_still_falls_through_to_the_overlay(tmp_path, monkeypatch):
    """Per FIELD, not per file: a snapshot that names REPO but not
    REVIEW_STATE_DIR must not blank the overlay's explicit value."""
    repos, config = _isolated_config(tmp_path, monkeypatch)
    (repos / "myapp.env").write_text('REPO="Acme/Old"\nREVIEW_STATE_DIR="/custom/review"\n')
    (config / ".active.myapp.env").write_text('REPO="Acme/New"\nSTATE_DIR="/composed/fix"\n')
    assert cli.journal_paths("myapp") == ["/composed/fix/journal.jsonl", "/custom/review/journal.jsonl"]


def test_status_caps_resolve_overlay_then_shared_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path / "repos"))
    Path(cli.REPOS_DIR).mkdir()
    monkeypatch.setattr(cli, "SHARED_ENV", str(tmp_path / "shared.env"))
    Path(cli.SHARED_ENV).write_text('DAILY_SPEND_CAP_USD="99"\nGLOBAL_DAILY_SPEND_CAP_USD="1500"\n')
    (Path(cli.REPOS_DIR) / "self.env").write_text('REPO="o/r"\nTARGET_DIR="/srv/o-r"\nDAILY_SPEND_CAP_USD="0"\n')
    assert cli.daily_cap("self") == 0
    assert cli.global_daily_cap("self") == 1500


# P0R-2: refuse a resolved overlay that belongs to a DIFFERENT repo/checkout.
@pytest.mark.parametrize(
    "name,overlay,repo,target,named",
    [
        (
            "app",
            'REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\n',
            "other/app",
            "/srv/other-app",
            ("acme/app", "other/app"),
        ),
        ("app", 'REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\n', "acme/app", "/srv/acme-app", None),
        ("app", None, "other/app", "/srv/other-app", None),
        # A sibling checkout must not start or stop this deployment.
        (
            "app-wt",
            'REPO="acme/app"\nTARGET_DIR="/srv/app-harness"\n',
            "acme/app",
            "/srv/somewhere-else",
            ("/srv/app-harness", "/srv/somewhere-else"),
        ),
        # A hand-edited overlay with no REPO: TARGET_DIR is the identity left.
        ("app", 'TARGET_DIR="/srv/acme-app"\n', "other/app", "/srv/other-app", ("/srv/acme-app", "/srv/other-app")),
    ],
)
def test_check_overlay_mismatch(tmp_path, name, overlay, repo, target, named):
    if overlay:
        (tmp_path / f"{name}.env").write_text(overlay)
    msg = cli.check_overlay_mismatch(name, repo, target, repos_dir=str(tmp_path))
    if named is None:
        assert msg == ""
    else:
        assert msg and all(part in msg for part in named)


# ── per-command wiring: fatal (start/stop/doctor/install-service) vs warn (status) ──
def _start_args(**overrides) -> argparse.Namespace:
    return argparse.Namespace(
        **{
            "repo": "",
            "target_dir": "",
            "name": "",
            "roles": None,
            "base_branch": "dev",
            "no_init": True,
            "max_budget": "",
            "review_budget": "",
            "max_ticks": None,
            "write_config": False,
            "worktree": False,
            "worktree_path": "",
            **overrides,
        }
    )


def _prep_mismatch_overlay(tmp_path, monkeypatch, *, isolate_entrypoint=True) -> None:
    # Overlay-only cases ignore host installation; install cases supply their
    # own PATH fixture and must retain real drift detection.
    if isolate_entrypoint:
        monkeypatch.setattr(cli, "entrypoint_drift", lambda: "")
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    (tmp_path / "app.env").write_text('REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\nTEST_CMD="echo ok"\n')


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
        return os.path.join("/opt/other-checkout/.venv/bin" if name in state["foreign"] else venv_bin, name)

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


_FOREIGN = dict(present=("issue-agent",), foreign=("issue-agent",))
_TRANSIENT = dict(present=("issue-agent",), harness_root="/home/u/repo/.claude/worktrees/agent-x")


# `said` pins each message to its stream: refusals and failures are stderr, which
# operators separate from the success line (`issue-agent start 2>…`).
@pytest.mark.parametrize(
    "state,healed,installs,said",
    [
        (dict(present=("issue-agent",)), True, 0, ()),
        # Neither script on PATH is a dev checkout, not drift — doctor stays quiet
        # there too, and `make install` must not run behind the operator's back.
        (dict(present=()), True, 0, ()),
        (dict(_FOREIGN, after=("issue-agent",), after_foreign=("issue-agent",)), False, 1, (("err", "still drifted"),)),
        # `make install` runs `uv sync` on the ONE .venv every deployment's daemons
        # execute from; pruning it mid-tick breaks the running ones.
        (dict(_FOREIGN, live=("keifu", "sol")), False, 0, (("err", "keifu, sol"), ("err", "uv sync"))),
        # Presence-only reads healthy while another checkout's shim shadows ours —
        # the daemon then execs a different tree's plugin/ and config/.
        (dict(_FOREIGN, after=("issue-agent",)), True, 1, (("out", "/opt/other-checkout"),)),
        # #187: reinstalling from the transient process would reinstall the
        # transient tree — the exact state being healed.
        (_TRANSIENT, False, 0, (("err", "canonical checkout"),)),
        # The output tail still reaches the operator.
        (dict(_FOREIGN, timeout=True), False, 1, (("err", "exceeded 300s"), ("err", "boom"))),
    ],
)
def test_start_heals_entrypoint_drift_only_when_safe(monkeypatch, capsys, state, healed, installs, said):
    calls = _install_state(monkeypatch, **state)

    assert cli.heal_entrypoint_drift() is healed
    assert len(calls) == installs
    captured = capsys.readouterr()
    assert all(text in getattr(captured, stream) for stream, text in said)


def test_drift_flags_an_install_resolving_into_a_transient_worktree(monkeypatch):
    """#187: `make install` inside an agent worktree bakes the transient venv
    into the launcher; from inside that process every other check reads
    coherent, so the path itself is the signature."""
    _install_state(monkeypatch, **_TRANSIENT)

    assert "transient agent worktree" in cli.entrypoint_drift()


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


def _record_scripts(monkeypatch, rc=lambda cmd: 0) -> list:
    """Record every script call; the overlay reads (a real `bash -c` subshell,
    read_repo_env_field) pass through untouched."""
    calls: list = []
    real_run = cli.subprocess.run

    def run(cmd, **kwargs):
        if list(cmd[:2]) == ["bash", "-c"]:
            return real_run(cmd, **kwargs)
        calls.append((cmd, kwargs))
        return argparse.Namespace(returncode=rc(cmd), stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


def test_start_still_launches_the_preflight_after_a_failed_reinstall(tmp_path, monkeypatch):
    """Fallback is today's behaviour: the preflight runs and reports the
    stale/partial install verbatim, rather than a second Python-side error."""
    _prep_mismatch_overlay(tmp_path, monkeypatch, isolate_entrypoint=False)
    execve_calls: list = []
    monkeypatch.setattr(cli.os, "execve", lambda *a: execve_calls.append(a))
    install_calls = _install_state(monkeypatch, **_FOREIGN, install_rc=1)

    cli.cmd_start(_start_args(repo="acme/app", target_dir="/srv/acme-app", name="app"))

    assert install_calls == [["make", "install"]]
    assert len(execve_calls) == 1
    assert "point-at.sh" in " ".join(execve_calls[0][1])


# P0R-2 exact repro at the command level: resolving to the app.env overlay
# (via an explicit --name or a stale collision) while the detected repo/checkout
# is a DIFFERENT one. FATAL for start (would launch the daemons against acme/app
# from other/app), stop (SIGTERMs another deployment's daemons), doctor (a false
# all-clear for the wrong repo right before `start`) and install-service (a
# PERSISTENT service relaunching against it, refused before compose-env.sh).
# WARN for status: it is read-only, and refusing would prevent using `status`
# to diagnose this exact collision.
@pytest.mark.parametrize(
    "command,rc", [("start", 2), ("stop", 2), ("doctor", 2), ("install_service", 2), ("status", 0)]
)
def test_a_wrong_repo_overlay_is_fatal_except_for_read_only_status(tmp_path, monkeypatch, capsys, command, rc):
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    launched = _record_scripts(monkeypatch)
    monkeypatch.setattr(cli.os, "execve", lambda *a: launched.append(a))

    assert (
        getattr(cli, f"cmd_{command}")(_start_args(repo="other/app", target_dir="/srv/other-app", name="app", lines=5))
        == rc
    )

    captured = capsys.readouterr()
    assert "acme/app" in captured.err and "other/app" in captured.err
    if command == "status":
        assert "status for 'app'" in captured.out  # ... and still rendered status
    else:
        assert launched == []  # never delegated to point-at.sh, compose-env.sh or an installer


def test_role_pid_discovery_is_scoped_to_requested_deployment(monkeypatch) -> None:
    by_pattern = {
        "issue_agent.app.main fix": [101, 102],
        "issue_agent.app.launchd_main fix": [],
        "issue_agent.app.main review": [201, 202],
        "issue_agent.app.launchd_main review": [],
    }
    owners = {101: "deployment-a", 102: "deployment-b", 201: "deployment-a", 202: "deployment-b"}
    monkeypatch.setattr(cli, "_pgrep", lambda pattern: by_pattern[pattern])
    monkeypatch.setattr(cli, "_pid_deployment_name", lambda pid: owners[pid])

    assert cli._role_pids("deployment-a") == {"fix": [101], "review": [201]}
    assert cli._role_pids("deployment-b") == {"fix": [102], "review": [202]}


def test_fresh_role_heartbeats_are_bounded_and_ignore_terminal_state(tmp_path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 3, 22, 30, tzinfo=timezone.utc)
    state_dirs = [tmp_path / role for role in cli.ROLES]
    for state_dir in state_dirs:
        state_dir.mkdir()
    monkeypatch.setattr(cli, "journal_paths", lambda _name: [str(path / "journal.jsonl") for path in state_dirs])

    def beat(index: int, state: str, age_s: float) -> None:
        updated = (now - timedelta(seconds=age_s)).isoformat()
        (state_dirs[index] / "heartbeat.json").write_text(json.dumps({"state": state, "updated_at": updated}))

    def fresh():
        return cli._fresh_role_heartbeats("app", now_fn=lambda: now, stale_after_s=30)

    beat(0, "running", 10)
    beat(1, "running", 31)
    assert fresh() == {"fix": now - timedelta(seconds=10)}
    beat(1, "running", -1)  # from the future
    assert "review" not in fresh()
    beat(1, "stopped", 0)
    assert "review" not in fresh()


def test_launcher_pid_discovery_uses_exact_deployment_argv_token(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_pgrep", lambda _pattern: [101, 102, 103, 104])
    argv = {
        101: ("/bin/bash", "/opt/harness/bin/point-at.sh", "app"),
        102: ("/bin/bash", "/opt/harness/bin/point-at.sh", "app2"),
        103: ("/bin/bash", "/tmp/not-point-at.sh", "app"),
        104: ("python", "-c", "import time; time.sleep(60)", "/tmp/point-at.sh", "app"),
    }
    monkeypatch.setattr(cli, "_pid_argv", lambda pid: argv[pid])

    assert cli._launcher_pids("app") == [101]
    assert cli._launcher_pids("app2") == [102]


def test_linux_pid_deployment_identity_comes_from_process_environment():
    if not os.path.isdir("/proc"):
        return
    process = subprocess.Popen(
        [cli.sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(5)"],
        env={**os.environ, "ISSUE_AGENT_DEPLOYMENT_ID": "deployment-a"},
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


def test_explicit_target_dir_is_the_repository_detection_authority(tmp_path, monkeypatch) -> None:
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    detected_from: list[str] = []
    monkeypatch.setattr(cli, "detect_target_dir", lambda _cwd: "/wrong/cwd")
    monkeypatch.setattr(cli, "detect_repo", lambda path: detected_from.append(path) or "acme/target")
    monkeypatch.setattr(cli, "resolve_name", lambda repo, explicit, target_dir: "target-deployment")

    context = cli._resolve_context(argparse.Namespace(repo="", target_dir=str(target), name=""))

    assert context == ("acme/target", str(target.resolve()), "target-deployment")
    assert detected_from == [str(target.resolve())]


# ── review M12/A4 fold-in: cmd_install_service fails loud on compose failure ──
def _install_args(tmp_path, **changes) -> argparse.Namespace:
    # Explicit repo/target_dir/name → _resolve_context never shells out to
    # git/gh; resolve_name(explicit=name) returns immediately.
    return argparse.Namespace(**{"repo": "o/r", "target_dir": str(tmp_path), "name": "myapp", "roles": None, **changes})


def _myapp_overlay(tmp_path, monkeypatch, text: str | None = None) -> None:
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path))
    (tmp_path / "myapp.env").write_text(text if text is not None else f'REPO="o/r"\nTARGET_DIR="{tmp_path}"\n')
    (tmp_path / "shared.env").write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(tmp_path / "shared.env"))


def test_install_service_aborts_on_compose_failure(tmp_path, monkeypatch):
    """A4's out-of-scope report: compose-env.sh ran with check=False and its
    return code was never inspected — a compose failure proceeded straight to
    installing the service against a stale/missing config/.active.env."""
    _myapp_overlay(tmp_path, monkeypatch)
    calls = _record_scripts(monkeypatch, rc=lambda cmd: 1 if "compose-env.sh" in cmd[1] else 0)

    assert cli.cmd_install_service(_install_args(tmp_path)) != 0
    assert not any("install-systemd.sh" in c[1] or "install-launchd.sh" in c[1] for c, _kw in calls)


def test_doctor_aborts_on_compose_failure_without_checking_inherited_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "entrypoint_drift", lambda: "")
    monkeypatch.setattr(cli, "check_overlay_mismatch", lambda *_args: "")
    _myapp_overlay(tmp_path, monkeypatch, "REPO=o/r\n")
    monkeypatch.setenv("AGENT_ENV", str(tmp_path / "another-deployment.env"))

    def run(cmd, **kwargs):
        assert Path(cmd[1]).name == "compose-env.sh", "preflight ran after failed compose"
        return argparse.Namespace(returncode=7)

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.cmd_doctor(_install_args(tmp_path)) == 7
    assert "refusing to check a stale or missing" in capsys.readouterr().err


def test_uninstall_service_defaults_to_current_deployment(tmp_path, monkeypatch) -> None:
    _myapp_overlay(tmp_path, monkeypatch)
    calls = _record_scripts(monkeypatch)

    assert cli.cmd_uninstall_service(_install_args(tmp_path, all=False)) == 0
    uninstall, kwargs = calls[-1]
    assert uninstall[-2:] == ["uninstall", "myapp"]
    assert kwargs["env"]["ISSUE_AGENT_DEPLOYMENT_ID"] == "myapp"
    assert kwargs["env"]["AGENT_ENV"].endswith(".active.myapp.env")


def test_uninstall_service_all_is_explicit_and_unscoped(monkeypatch) -> None:
    calls = _record_scripts(monkeypatch)

    assert cli.cmd_uninstall_service(argparse.Namespace(repo="", target_dir="", name="", all=True)) == 0
    script = "install-launchd.sh" if cli.sys.platform == "darwin" else "install-systemd.sh"
    assert [cmd for cmd, _kw in calls] == [[cli._BASH, cli._bin(script), "uninstall"]]


def test_ready_routes_operator_label_through_operator_effects(monkeypatch, capsys):
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


def test_remote_bootstrap_ensures_exactly_the_two_lifecycle_labels(monkeypatch, capsys):
    labels = []

    class Effects:
        def ensure_label(self, label, **_kwargs):
            labels.append(label)
            return (label != "needs-human"), "forbidden"

    monkeypatch.setattr(cli, "_operator_effects", lambda *_args: Effects())

    assert cli.cmd_prepare_remote(argparse.Namespace(repo="o/r")) == 1
    assert labels == ["ai-ready", "needs-human"]
    assert "label needs-human: forbidden" in capsys.readouterr().err


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
            return _fake_proc((login or '{"message":"Bad credentials"}') + "\n", 0 if login else 1)
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


def _init_env(tmp_path, monkeypatch, *, answers, secrets=(), gh=None, codex=None):
    shared = tmp_path / "config" / "shared.env"
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    monkeypatch.setattr(cli.subprocess, "run", gh or _gh_stub())
    monkeypatch.setattr(cli, "detect_claude_auth", lambda: True)
    monkeypatch.setattr(cli, "detect_codex_auth", lambda: "apikey")
    real_which = cli.shutil.which
    codex_path = "/usr/bin/codex" if codex is None else codex
    monkeypatch.setattr(
        cli.shutil, "which", lambda name, *a, **k: codex_path if name == "codex" else real_which(name, *a, **k)
    )
    asked = _Answers(answers)
    secret_asked = _Answers(secrets)
    monkeypatch.setattr("builtins.input", asked)
    monkeypatch.setattr(cli.getpass, "getpass", secret_asked)
    return shared, asked, secret_asked


@pytest.mark.parametrize("pin_answer,pinned", [(None, True), ("n", False)])
def test_cmd_init_pins_codex_bin_unless_the_operator_declines(tmp_path, monkeypatch, capsys, pin_answer, pinned):
    """2026-08-02 keifu: the wizard's shell found 0.147.0 while the daemons'
    systemd PATH found 0.142.3, which the provider refuses to serve. Answering
    nothing (or piped stdin) must still write the absolute pin."""
    answers = ["fix-bot", "review-bot", "N", "claude", "codex"]
    if pin_answer is not None:
        answers += [pin_answer, "https://api.openai.com/v1"]
    shared, _asked, _s = _init_env(
        tmp_path, monkeypatch, answers=answers, secrets=["", ""], codex="/home/o/.local/bin/codex"
    )

    assert cli.cmd_init(argparse.Namespace(force=False)) == 0
    assert _shared_values(shared.read_text()).get("CODEX_BIN") == ("/home/o/.local/bin/codex" if pinned else None)
    assert ("unpinned" in capsys.readouterr().err) is not pinned


def _wizard_answers(**over) -> list[str]:
    base = {
        "fix": "fix-bot",
        "review": "review-bot",
        "app": "N",
        "fixer_runtime": "claude",
        "reviewer_runtime": "claude",
    }
    base.update(over)
    return list(base.values())


def _shared_values(text: str) -> dict:
    return _source_env(text)


def _init(tmp_path, monkeypatch, *, secrets, answers=None, gh=None, force=False):
    """Run the wizard to completion; returns (written text, prompts, secret prompts)."""
    shared, asked, secret_asked = _init_env(
        tmp_path, monkeypatch, answers=_wizard_answers() if answers is None else answers, secrets=secrets, gh=gh
    )
    assert cli.cmd_init(argparse.Namespace(force=force)) == 0
    return shared.read_text(), asked, secret_asked


def test_cmd_init_degrades_when_gh_is_unreachable(tmp_path, monkeypatch, capsys):
    """gh missing/unauthenticated must warn and keep going, not crash the wizard."""

    def boom(cmd, **kwargs):
        raise OSError("gh: not found")

    text, _asked, _s = _init(tmp_path, monkeypatch, secrets=["", ""], gh=boom)

    assert _shared_values(text)["FIX_BOT"] == "fix-bot"
    assert "gh api user" in capsys.readouterr().err


def test_cmd_init_reprompt_loop_is_bounded_and_keeps_the_last_answer(tmp_path, monkeypatch, capsys):
    """A stubborn operator must not be able to hold the wizard open forever."""
    text, asked, _s = _init(
        tmp_path,
        monkeypatch,
        secrets=["", ""],
        answers=["fix-bot", "fix-bot", "fix-bot", "fix-bot", "N", "claude", "claude"],
    )

    assert _shared_values(text)["REVIEW_BOT"] == "fix-bot"
    assert "doctor" in capsys.readouterr().err
    assert len([p for p in asked.prompts if "Reviewer login" in p]) == 3  # bounded, then the answer stands


def test_cmd_init_discards_a_rejected_token_at_the_attempt_ceiling(tmp_path, monkeypatch, capsys):
    """A token GitHub answered about and rejected is NEVER stored. Persisting one
    silently installs whatever account it belongs to (typically the operator's
    own PAT) as the reviewer identity, defeating three-identity mode."""
    text, _asked, secrets = _init(tmp_path, monkeypatch, secrets=["t1", "t2", "t3", "t4"], gh=_gh_stub(token_logins={}))

    assert "REVIEW_BOT_TOKEN" not in _shared_values(text)
    assert "t3" not in text
    assert secrets.items == []  # reviewer bounded at three, then the fixer prompt
    # The fixer prompt has the same contract: t4 is rejected by the same stub, so
    # neither identity may end up holding a token GitHub refused.
    assert "FIX_BOT_TOKEN" not in _shared_values(text)
    assert "t4" not in text
    assert "no REVIEW_BOT_TOKEN stored" in capsys.readouterr().err


def test_cmd_init_discards_a_token_belonging_to_another_account(tmp_path, monkeypatch, capsys):
    """The review's scenario: an operator's personal PAT offered three times. It
    authenticates fine — for the WRONG identity — so it must not be written."""
    text, _asked, _s = _init(
        tmp_path, monkeypatch, secrets=["personal-pat"] * 3, gh=_gh_stub(token_logins={"personal-pat": "the-operator"})
    )

    assert "personal-pat" not in text
    assert "the-operator" in capsys.readouterr().err


def test_cmd_init_keeps_an_unverifiable_token_when_gh_cannot_run(tmp_path, monkeypatch, capsys):
    """gh absent is an outage, not a verdict: keep the token, loudly, and let
    doctor adjudicate. This is the ONLY path that stores an unproven token."""

    def unusable(cmd, **kwargs):
        if (kwargs.get("env") or {}).get("GH_TOKEN"):
            raise OSError("gh: not found")
        return _fake_proc("fix-bot\n")

    text, _asked, _s = _init(tmp_path, monkeypatch, secrets=["offline-token"] * 3, gh=unusable)

    assert _shared_values(text)["REVIEW_BOT_TOKEN"] == "offline-token"
    assert "UNVERIFIED" in capsys.readouterr().err


def test_cmd_init_reports_a_missing_token_file_and_reprompts(tmp_path, monkeypatch, capsys):
    token_file = tmp_path / "pat.txt"
    token_file.write_text("file-token\n")
    text, _asked, _s = _init(
        tmp_path,
        monkeypatch,
        secrets=[f"@{tmp_path / 'nope.txt'}", f"@{token_file}"],
        gh=_gh_stub(token_logins={"file-token": "review-bot"}),
    )

    assert _shared_values(text)["REVIEW_BOT_TOKEN"] == "file-token"
    message = capsys.readouterr().err
    assert "no file exists at the path you entered" in message
    assert "nope.txt" not in message  # never echo what was typed


def test_cmd_init_stores_a_verified_token_privately_and_never_echoes_it(tmp_path, monkeypatch, capsys):
    """shared.env can hold a long-lived PAT: it must never be group/world readable."""
    text, _asked, _s = _init(
        tmp_path, monkeypatch, secrets=["s3cret-pat"], gh=_gh_stub(token_logins={"s3cret-pat": "review-bot"})
    )

    captured = capsys.readouterr()
    assert "s3cret-pat" not in captured.out + captured.err
    assert "s3cret-pat" in _shared_values(text)["REVIEW_BOT_TOKEN"]
    assert oct(Path(cli.SHARED_ENV).stat().st_mode & 0o777) == "0o600"


def test_cmd_init_skips_the_reviewer_token_prompt_when_a_github_app_is_configured(tmp_path, monkeypatch):
    """The App supplies the review identity, so REVIEW_BOT_TOKEN is not
    asked for. The FIXER's is: the App covers review only, and
    `main` builds the fix gateway from FIX_BOT_TOKEN or nothing (2026-08-02
    keifu — an App-configured deployment still ran its fixer on the operator's
    ambient credential)."""
    text, _asked, secrets = _init(
        tmp_path,
        monkeypatch,
        secrets=["fixer-pat"],
        gh=_gh_stub(token_logins={"fixer-pat": "fix-bot"}),
        answers=["fix-bot", "review-bot", "y", "123", "456", "/keys/app.pem", "claude", "claude"],
    )

    values = _shared_values(text)
    assert values["GH_APP_ID"] == "123"
    assert "REVIEW_BOT_TOKEN" not in values
    assert values["FIX_BOT_TOKEN"] == "fixer-pat"
    assert [p for p in secrets.prompts if "REVIEW_BOT_TOKEN" in p] == []


def test_cmd_init_survives_eof_midway_and_still_writes_a_config(tmp_path, monkeypatch):
    """Ctrl-D partway through takes the remaining defaults instead of exploding."""
    values = _shared_values(_init(tmp_path, monkeypatch, answers=["fix-bot"], secrets=[])[0])
    assert values["FIX_BOT"] == "fix-bot"
    assert "REVIEW_BOT_TOKEN" not in values


# ── runtime auth detection ────────────────────────────────────────────────────
def test_detect_claude_auth_accepts_an_environment_key(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    assert cli.detect_claude_auth() is True

    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    assert cli.detect_claude_auth() is False


@pytest.mark.parametrize(
    "content,mode",
    [
        (
            json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "secret"}}),
            "chatgpt",
        ),  # the mode, not the secret
        ("{not json", ""),
    ],
)
def test_detect_codex_auth_reads_the_mode_and_tolerates_a_malformed_file(monkeypatch, tmp_path, content, mode):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(content)
    assert cli.detect_codex_auth() == mode


def test_read_token_input_distinguishes_files_from_opaque_tokens(tmp_path):
    path = tmp_path / "pat"
    path.write_text("from-file\n\n")
    assert cli.read_token_input(str(path)) == ("from-file", "")
    # A PAT is opaque text; only an EXISTING path (or an explicit @) is a file.
    assert cli.read_token_input("/not/a/real/path") == ("/not/a/real/path", "")
    # A stray leading @ on a pasted PAT must not put the secret in a message.
    token, problem = cli.read_token_input(f"@{tmp_path / 'absent'}")
    assert token == "" and problem
    assert "absent" not in problem and str(tmp_path) not in problem


def _gh_raises(*_a, **_k):
    raise OSError("gh: not found")


@pytest.mark.parametrize(
    "run,answer",
    [
        # gh writes its error body to stdout: never a login, yet ANSWERED (a verdict).
        (lambda *a, **k: _fake_proc('{"message":"Bad credentials"}\n', 0), ("", True)),
        # `gh api user --jq .login` prints `null` for an absent field, and `null`
        # passes the login charset — it must not be compared as an account.
        (lambda *a, **k: _fake_proc("null\n", 0), ("", True)),
        # gh absent is an OUTAGE, not a verdict — the caller may keep the token.
        (_gh_raises, ("", False)),
    ],
)
def test_gh_login_for_token_separates_a_verdict_from_an_outage(monkeypatch, run, answer):
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.gh_login_for_token("t") == answer
    assert cli.detect_fixer_login() == ""


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


_MERGED = json.dumps([{"baseRefName": "main"}, {"baseRefName": "main"}, {"baseRefName": "dev"}])
_TRUNK = ("trunk", "the repository default branch")


@pytest.mark.parametrize(
    "stub,repo,expected",
    [
        (dict(pr_list=_MERGED, existing_branches=("main",)), "acme/app", ("main", "2/3")),
        # An inferred base that does not exist would point every PR at a missing
        # ref (and trip point-at.sh's hard stop): fall through to the next candidate.
        (
            dict(pr_list=json.dumps([{"baseRefName": "gone"}]), default_branch="trunk", existing_branches=("trunk",)),
            "acme/app",
            _TRUNK,
        ),
        (dict(pr_list="[]", default_branch="trunk", existing_branches=("trunk",)), "acme/app", _TRUNK),
        (dict(pr_list="not json at all", default_branch="trunk", existing_branches=("trunk",)), "acme/app", _TRUNK),
        # Nothing confirmed: the harness default and NO provenance comment.
        (dict(), "acme/app", ("dev", "")),
        (dict(pr_list=_MERGED, existing_branches=("main",)), "", ("dev", "")),
    ],
)
def test_detect_base_branch(monkeypatch, stub, repo, expected):
    monkeypatch.setattr(cli, "_run", _gh_run_stub(**stub))
    branch, note = cli.detect_base_branch("/t", repo)
    assert branch == expected[0]
    assert expected[1] in note if expected[1] else note == ""


# ── cmd_start first-run base-branch inference ────────────────────────────────────────────
def _fresh_start_env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPOS_DIR", str(tmp_path / "repos"))
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))
    monkeypatch.setattr(cli.os, "execve", lambda *a: None)
    # The real probe reads the host PATH; drift there would run `make install`
    # on the checkout venv from inside the test.
    monkeypatch.setattr(cli, "entrypoint_drift", lambda: "")
    return Path(cli.REPOS_DIR) / "app.env"


def test_cmd_start_write_config_preserves_overlay_when_quiescence_fails(tmp_path, monkeypatch) -> None:
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    overlay = tmp_path / "app.env"
    original = overlay.read_text()
    execve_calls: list[tuple] = []
    monkeypatch.setattr(cli, "_stop_deployment", lambda _name: 1)
    monkeypatch.setattr(cli.os, "execve", lambda *args: execve_calls.append(args))

    assert cli.cmd_start(_start_args(repo="acme/app", target_dir="/srv/acme-app", name="app", write_config=True)) == 1
    assert overlay.read_text() == original
    assert execve_calls == []


_INFERRING = dict(repo="acme/app", target_dir="/srv/acme-app", name="app", base_branch=None)


def test_cmd_start_never_adopts_an_inferred_base_branch_unattended(tmp_path, monkeypatch, capsys):
    """point-at.sh hard-stops on a BASE_BRANCH that does not exist — that stop is
    the operator's conscious-choice gate on where this harness MERGES. Inference
    with nobody at the terminal must not walk past it: keep the default, record
    the finding as an inert comment."""
    overlay = _fresh_start_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "detect_base_branch", lambda _t, _r: ("main", "9/10 of the last merged PRs"))

    cli.cmd_start(_start_args(**_INFERRING))

    text = overlay.read_text()
    assert _source_env(text)["BASE_BRANCH"] == "dev"
    assert "# detected merge target of recent PRs: main" in text
    assert "main" in capsys.readouterr().out


def test_cmd_start_write_config_preserves_the_existing_base_branch(tmp_path, monkeypatch):
    """A regenerate must not silently re-point a live deployment's merge target:
    the previously written BASE_BRANCH stands unless --base-branch says otherwise."""
    _prep_mismatch_overlay(tmp_path, monkeypatch)
    overlay = tmp_path / "app.env"
    overlay.write_text('REPO="acme/app"\nTARGET_DIR="/srv/acme-app"\nBASE_BRANCH="release-2"\n')
    monkeypatch.setattr(cli.os, "execve", lambda *a: None)
    monkeypatch.setattr(cli, "_stop_deployment", lambda _n: 0)

    def refuse(*a, **k):
        raise AssertionError("must not re-infer over a written BASE_BRANCH")

    monkeypatch.setattr(cli, "detect_base_branch", refuse)

    cli.cmd_start(_start_args(**_INFERRING, write_config=True))

    assert _source_env(overlay.read_text())["BASE_BRANCH"] == "release-2"


def test_cmd_start_confirms_the_inferred_base_branch_on_a_tty(tmp_path, monkeypatch):
    overlay = _fresh_start_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "detect_base_branch", lambda _t, _r: ("main", "history"))
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", _Answers(["trunk"]))

    cli.cmd_start(_start_args(**_INFERRING))

    assert _source_env(overlay.read_text())["BASE_BRANCH"] == "trunk"


def test_cmd_init_tightens_an_existing_shared_env_before_writing_secrets(tmp_path, monkeypatch):
    """--force over a 0644 file: O_CREAT's mode applies only to a NEW file, so
    without a chmod FIRST the PAT would land in a world-readable file and only be
    protected afterwards. Asserted at open time, not just at the end."""
    shared, _asked, _s = _init_env(
        tmp_path,
        monkeypatch,
        answers=_wizard_answers(),
        secrets=["good"],
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


def test_status_tails_only_a_live_units_journal_and_reports_a_failed_query(monkeypatch):
    """`systemctl show` is parsed: a loaded, live unit's journal is the log;
    an inactive one is not; a failed query is reported, not replaced."""
    show = {"returncode": 0, "stdout": "LoadState=loaded\nActiveState=active\n", "stderr": ""}
    calls: list = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[0] == "systemctl":
            return argparse.Namespace(**show)
        return argparse.Namespace(returncode=0, stdout="line 1\nline 2\n", stderr="")

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.subprocess, "run", run)
    unit = "issue-agent-fix-watcher-app.service"
    assert cli._systemd_status_log("app", "fix", 5) == (f"systemd journal: {unit}", "line 1\nline 2")
    assert calls[-1][:6] == ["journalctl", "--user", "--unit", unit, "--lines", "5"]
    show["stdout"] = "LoadState=loaded\nActiveState=inactive\n"
    assert cli._systemd_status_log("app", "fix", 5) is None
    show.update(returncode=1, stdout="", stderr="Failed to connect to bus\n")
    assert cli._systemd_status_log("app", "fix", 5) == (
        f"systemd status unavailable: {unit}",
        "  Failed to connect to bus",
    )


def test_start_still_rejects_a_non_canonical_name():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["start", "--name", "Weird Name"])


# ── doctor: the offline checks gate the exit code ─────────────────────────────
def _doctor_rc(tmp_path, monkeypatch, composed: dict, *, sh_rc: int) -> int:
    monkeypatch.setattr(cli, "SHARED_ENV", str(tmp_path / "absent.env"))
    for independent_check in ("entrypoint_drift", "check_overlay_mismatch"):
        monkeypatch.setattr(cli, independent_check, lambda *a, **k: "")
    monkeypatch.setattr(cli, "_resolve_context", lambda _a: ("o/r", str(tmp_path), "r"))
    monkeypatch.setattr(cli, "_composed_env", lambda _env: dict(composed))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: argparse.Namespace(returncode=sh_rc))
    from issue_agent.app import doctor

    monkeypatch.setattr(doctor, "sandbox_findings", lambda: ())  # the host's bwrap is not under test
    return cli.cmd_doctor(argparse.Namespace(name=None, repo=None))


_SEPARATE = {"FIX_BOT_TOKEN": "fix", "REVIEW_BOT_TOKEN": "review"}


@pytest.mark.parametrize(
    "composed,sh_rc,rc",
    [
        # doctor is a gate (point-at.sh, CI): a whitespace token 401s every call.
        ({**_SEPARATE, "FIX_BOT_TOKEN": "   "}, 0, 1),
        (dict(_SEPARATE), 0, 0),  # no error finding stays green
        ({**_SEPARATE, "FIX_BOT_TOKEN": "   "}, 2, 2),  # a failing doctor.sh keeps its exit code
    ],
)
def test_doctor_exit_code_gates_on_error_findings_and_doctor_sh(tmp_path, monkeypatch, capsys, composed, sh_rc, rc):
    assert _doctor_rc(tmp_path, monkeypatch, composed, sh_rc=sh_rc) == rc
    assert ("whitespace" in capsys.readouterr().err) is (rc == 1 or sh_rc == 2)


def test_doctor_reads_the_composed_snapshot_the_daemons_will_see(tmp_path):
    snapshot = tmp_path / "active.env"
    snapshot.write_text('FIX_BOT="from-snapshot"\n')
    composed = cli._composed_env({"AGENT_ENV": str(snapshot), "FIX_BOT": "from-shell"})
    assert composed["FIX_BOT"] == "from-snapshot"


def test_an_unsourceable_snapshot_falls_back_to_the_inherited_environment():
    env = {"AGENT_ENV": "/nonexistent/active.env", "FIX_BOT": "f"}
    assert cli._composed_env(env) == env
