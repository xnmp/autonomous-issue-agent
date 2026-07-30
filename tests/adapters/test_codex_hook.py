"""codex_hook.write_hook_config — emits the governor as a Codex PreToolUse hook."""

import os
import stat
import subprocess
import tempfile
import tomllib

from issue_agent.adapters.codex_hook import (
    GOVERNOR_PATH,
    REPOSITORY_INSTRUCTIONS_MAX_BYTES,
    write_hook_config,
)

END_MARKER = "<!-- issue-agent generated instructions: end -->"


def test_writes_config_and_executable_script():
    cwd = tempfile.mkdtemp()
    config_path = write_hook_config(cwd)

    assert config_path == os.path.join(cwd, ".codex", "config.toml")
    assert os.path.exists(config_path)

    gov = os.path.join(cwd, ".codex", "hooks", "gov.sh")
    assert os.path.exists(gov)
    assert os.stat(gov).st_mode & stat.S_IXUSR  # executable


def test_config_declares_pretooluse_bash_hook():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd)
    toml = open(os.path.join(cwd, ".codex", "config.toml")).read()
    assert "[[hooks.PreToolUse]]" in toml
    assert 'matcher = "^Bash$"' in toml
    assert "[[hooks.PreToolUse.hooks]]" in toml
    assert 'type = "command"' in toml
    # points at the gov.sh we wrote
    assert os.path.join(cwd, ".codex", "hooks", "gov.sh") in toml


def test_gov_script_invokes_the_shared_governor():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd)
    script = open(os.path.join(cwd, ".codex", "hooks", "gov.sh")).read()
    # delegates to the single source-of-truth governor, fails OPEN without uv
    assert GOVERNOR_PATH in script
    assert "uv run" in script
    assert "exit 0" in script  # the uv-missing fail-open path


def test_review_hook_cannot_leave_read_only_state_for_nested_fixers(tmp_path):
    """Read-only policy belongs to a dispatched leg, never its hook artifact."""
    reviewer = tmp_path / "target"
    fixer = reviewer / ".claude" / "worktrees" / "issue-113"
    fixer.mkdir(parents=True)

    write_hook_config(str(reviewer), review_only=True)
    write_hook_config(str(fixer))

    review_hook = (reviewer / ".codex" / "hooks" / "gov.sh").read_text()
    fixer_hook = (fixer / ".codex" / "hooks" / "gov.sh").read_text()
    assert "ISSUE_AGENT_REVIEW_READ_ONLY" not in review_hook
    assert "ISSUE_AGENT_REVIEW_READ_ONLY" not in fixer_hook


def test_custom_governor_path():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd, governor_path="/custom/gov.py")
    script = open(os.path.join(cwd, ".codex", "hooks", "gov.sh")).read()
    assert "/custom/gov.py" in script


def test_writes_agents_md_when_given():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd, agents_md="# Fixer contract\nred before green.")
    agents = os.path.join(cwd, "AGENTS.override.md")
    assert os.path.exists(agents)
    assert "red before green" in open(agents).read()


def test_no_agents_md_when_empty():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd)  # agents_md="" default
    assert not os.path.exists(os.path.join(cwd, "AGENTS.override.md"))


def test_appends_root_and_nested_claude_md_in_path_order(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("root rules")
    nested = tmp_path / "packages" / "api"
    nested.mkdir(parents=True)
    (nested / "CLAUDE.md").write_text("api rules")

    write_hook_config(str(tmp_path), agents_md="# Harness contract")

    agents = (tmp_path / "AGENTS.override.md").read_text()
    assert "# Repository instructions (CLAUDE.md)" in agents
    assert agents.index("## CLAUDE.md") < agents.index("## packages/api/CLAUDE.md")
    assert "root rules" in agents and "api rules" in agents


def test_preserves_target_agents_md_and_regeneration_is_idempotent(tmp_path):
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("# Target rules\nkeep this")
    (tmp_path / "CLAUDE.md").write_text("claude rules 😀")

    write_hook_config(str(tmp_path), agents_md="# Harness contract")
    first = (tmp_path / "AGENTS.override.md").read_text()
    write_hook_config(str(tmp_path), agents_md="# Harness contract")

    assert agents_path.read_text() == "# Target rules\nkeep this"
    assert "# Repository instructions (AGENTS.md)" in first
    assert "# Target rules\nkeep this" in first
    assert first == (tmp_path / "AGENTS.override.md").read_text()
    assert first.count("# Harness contract") == 1
    assert first.count("claude rules") == 1


def test_repository_content_may_document_the_generated_end_marker(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(f"documented marker: {END_MARKER}")

    write_hook_config(str(tmp_path), agents_md="contract")
    first = (tmp_path / "AGENTS.override.md").read_text()
    write_hook_config(str(tmp_path), agents_md="contract")

    assert (tmp_path / "AGENTS.override.md").read_text() == first
    assert first.count(END_MARKER) == 2


def test_git_worktree_ignores_untracked_claude_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "CLAUDE.md").write_text("tracked rules")
    ignored = tmp_path / "scratch"
    ignored.mkdir()
    (ignored / "CLAUDE.md").write_text("untracked injection")
    subprocess.run(["git", "-C", str(tmp_path), "add", "CLAUDE.md"], check=True)

    write_hook_config(str(tmp_path), agents_md="contract")

    instructions = (tmp_path / "AGENTS.override.md").read_text()
    assert "tracked rules" in instructions
    assert "untracked injection" not in instructions


def test_tracked_agents_override_is_untouched_and_uses_config_fallback(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    override = tmp_path / "AGENTS.override.md"
    override.write_text("# Target override")
    (tmp_path / "CLAUDE.md").write_text("claude rules 😀")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "AGENTS.override.md", "CLAUDE.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "target instructions"],
        check=True,
    )

    write_hook_config(str(tmp_path), agents_md="# Harness contract")

    assert override.read_text() == "# Target override"
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--short", "AGENTS.override.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""
    config = (tmp_path / ".codex" / "config.toml").read_text()
    assert "developer_instructions" in config
    assert "# Harness contract" in config
    assert "claude rules" in config
    assert "😀" in tomllib.loads(config)["developer_instructions"]


def test_repository_instructions_are_capped_and_truncation_is_logged(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x" * (REPOSITORY_INSTRUCTIONS_MAX_BYTES * 2))
    logs: list[str] = []

    write_hook_config(str(tmp_path), agents_md="contract", log=logs.append)

    agents = (tmp_path / "AGENTS.override.md").read_text()
    section = agents[
        agents.index("# Repository instructions (CLAUDE.md)"):
        agents.index("<!-- issue-agent generated instructions: end -->")
    ]
    assert len(section.encode()) <= REPOSITORY_INSTRUCTIONS_MAX_BYTES
    assert any("truncated" in message for message in logs)


def test_truncation_cuts_at_a_paragraph_and_points_at_the_full_file(tmp_path):
    # Paragraph-structured content well past the budget: the cut must land on a
    # paragraph boundary (never mid-sentence) and be followed by a pointer that
    # names the file, so the agent can recover the dropped guidance on demand.
    (tmp_path / "CLAUDE.md").write_text(
        "intact-paragraph\n\n" * (REPOSITORY_INSTRUCTIONS_MAX_BYTES // 8)
    )

    write_hook_config(str(tmp_path), agents_md="contract")

    agents = (tmp_path / "AGENTS.override.md").read_text()
    pointer_at = agents.index("[Repository instructions truncated")
    assert "read CLAUDE.md (and any later CLAUDE.md files)" in agents
    assert agents[:pointer_at].rstrip().endswith("intact-paragraph")
    section = agents[agents.index("# Repository instructions (CLAUDE.md)"):agents.index(END_MARKER)]
    assert len(section.encode()) <= REPOSITORY_INSTRUCTIONS_MAX_BYTES


def test_truncation_pointer_names_the_first_file_that_did_not_fit(tmp_path):
    # First file fits whole; the second overflows — the pointer must name the
    # second file, not the first.
    (tmp_path / "CLAUDE.md").write_text("root rules\n\n")
    sub = tmp_path / "backend"
    sub.mkdir()
    (sub / "CLAUDE.md").write_text("b\n\n" * REPOSITORY_INSTRUCTIONS_MAX_BYTES)

    write_hook_config(str(tmp_path), agents_md="contract")

    agents = (tmp_path / "AGENTS.override.md").read_text()
    assert "root rules" in agents
    assert "read backend/CLAUDE.md" in agents


# ── PostToolUse edit-check (only when edit_check_cmd given) ────────────────────
def test_no_edit_check_block_without_edit_check_cmd():
    # The read-record PostToolUse hook is always present (observe-only); only the
    # EDIT-CHECK block is gated on edit_check_cmd.
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd)  # edit_check_cmd="" default
    toml = open(os.path.join(cwd, ".codex", "config.toml")).read()
    assert "apply_patch" not in toml  # no edit-check matcher
    assert not os.path.exists(os.path.join(cwd, ".codex", "hooks", "edit_check.sh"))


def test_posttooluse_block_and_script_when_edit_check_cmd_given():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd, edit_check_cmd="npx tsc --noEmit")
    toml = open(os.path.join(cwd, ".codex", "config.toml")).read()
    assert "[[hooks.PostToolUse]]" in toml
    assert "apply_patch" in toml  # Codex edits via apply_patch
    script_path = os.path.join(cwd, ".codex", "hooks", "edit_check.sh")
    assert script_path in toml
    assert os.path.exists(script_path)
    assert os.stat(script_path).st_mode & stat.S_IXUSR  # executable
    script = open(script_path).read()
    assert "npx tsc --noEmit" in script
    assert "exit 2" in script  # block contract on failure


def test_read_record_hook_always_written():
    # The screenshot-read recorder is observe-only and free → always installed,
    # independent of edit_check_cmd.
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd)  # no edit_check_cmd
    toml = open(os.path.join(cwd, ".codex", "config.toml")).read()
    assert 'matcher = "^(Read|view_image)$"' in toml
    script_path = os.path.join(cwd, ".codex", "hooks", "read_record.sh")
    assert script_path in toml
    assert os.path.exists(script_path)
    assert os.stat(script_path).st_mode & stat.S_IXUSR
    assert "screenshots-read" in open(script_path).read()
