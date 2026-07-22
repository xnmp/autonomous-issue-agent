"""CLI pure helpers — repo detection parsing, overlay-name resolution, and the
synthesized per-repo env. No git/network: every helper here is deterministic.

The key invariant: synth_repo_env's keys must round-trip through config.from_env
(the daemon's reader), so the CLI and the daemon can't drift on key names."""

import os

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
