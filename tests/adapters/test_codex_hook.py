"""codex_hook.write_hook_config — emits the governor as a Codex PreToolUse hook."""

import os
import stat
import tempfile

from issue_agent.adapters.codex_hook import GOVERNOR_PATH, write_hook_config


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


def test_custom_governor_path():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd, governor_path="/custom/gov.py")
    script = open(os.path.join(cwd, ".codex", "hooks", "gov.sh")).read()
    assert "/custom/gov.py" in script


def test_writes_agents_md_when_given():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd, agents_md="# Fixer contract\nred before green.")
    agents = os.path.join(cwd, "AGENTS.md")
    assert os.path.exists(agents)
    assert "red before green" in open(agents).read()


def test_no_agents_md_when_empty():
    cwd = tempfile.mkdtemp()
    write_hook_config(cwd)  # agents_md="" default
    assert not os.path.exists(os.path.join(cwd, "AGENTS.md"))


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
