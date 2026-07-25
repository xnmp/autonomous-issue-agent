"""CLI pure helpers — repo detection parsing, overlay-name resolution, and the
synthesized per-repo env. No git/network: every helper here is deterministic.

The key invariant: synth_repo_env's keys must round-trip through config.from_env
(the daemon's reader), so the CLI and the daemon can't drift on key names."""

import argparse
import json
import os
from datetime import datetime, timezone

from issue_agent.app import cli
from issue_agent.app.config import from_env


# ── parse_remote_url ──────────────────────────────────────────────────────────
def test_parse_remote_url_ssh():
    assert cli.parse_remote_url("git@github.com:acme/MyApp.git") == "acme/MyApp"


def test_parse_remote_url_https_and_trailing_git():
    assert cli.parse_remote_url("https://github.com/acme/MyApp.git") == "acme/MyApp"
    assert cli.parse_remote_url("https://github.com/acme/MyApp") == "acme/MyApp"


def test_parse_remote_url_https_trailing_slash():
    assert cli.parse_remote_url("https://github.example.com/o/r/") == "o/r"


def test_parse_remote_url_garbage_is_empty():
    assert cli.parse_remote_url("not a url") == ""
    assert cli.parse_remote_url("") == ""


# ── derive_name ───────────────────────────────────────────────────────────────
def test_derive_name_lowercases_basename():
    assert cli.derive_name("acme/MyApp-combined-agent") == "myapp-combined-agent"


def test_derive_name_sanitizes_and_falls_back():
    assert cli.derive_name("") == "target"
    assert cli.derive_name("o/weird name!") == "weird-name"


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
    assert cli.resolve_name("acme/App", repos_dir=str(tmp_path)) == "myapp"


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
    name = cli.resolve_name("acme/New-Repo", repos_dir=str(tmp_path))
    assert name == "new-repo"


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


def test_resolve_name_falls_back_to_repo_only_when_no_target_match(tmp_path):
    """target_dir given but no overlay's TARGET_DIR matches → existing REPO-first
    (alpha) behavior, preserving back-compat."""
    repo = "acme/App-combined-agent"
    (tmp_path / "app-wt.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app-harness"\n')
    (tmp_path / "app.env").write_text(f'REPO="{repo}"\nTARGET_DIR="/repos/app"\n')
    # Detected dir matches neither overlay → first alpha REPO match (app-wt).
    assert cli.resolve_name(repo, repos_dir=str(tmp_path), target_dir="/somewhere/else") == "app-wt"


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
    assert env["REPO"] == "acme/MyApp"
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
        [fix_j, review_j, steward_j], str(tmp_path / "ledger.jsonl"), 10.0, lambda: now
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
    ledger = str(tmp_path / "ledger.jsonl")
    now = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    out_single = cli.spend_status_lines(fix_j, ledger, 10.0, lambda: now)
    out_multi = cli.spend_status_lines(
        [fix_j, missing_review, missing_steward], ledger, 10.0, lambda: now
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
    out = "\n".join(cli.spend_status_lines(fix_j, str(tmp_path / "ledger.jsonl"), 10.0, lambda: now))
    assert out.count("RESOLVE") == 1


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
    (tmp_path / "myapp.env").write_text('REPO="o/r"\n')
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))

    calls: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
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
    (tmp_path / "myapp.env").write_text('REPO="o/r"\n')
    shared = tmp_path / "shared.env"
    shared.write_text("")
    monkeypatch.setattr(cli, "SHARED_ENV", str(shared))

    calls: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result(0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    rc = cli.cmd_install_service(_install_args(tmp_path))
    assert rc == 0
    assert any(
        "install-systemd.sh" in c[1] or "install-launchd.sh" in c[1] for c in calls
    )
