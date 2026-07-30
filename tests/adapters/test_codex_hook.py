import asyncio
import os
import stat

from issue_agent.adapters.codex_hook import (
    HookPreparingRuntime,
    write_hook_config,
)
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.outcome import AgentResult


def test_hook_and_config_live_outside_worktree(tmp_path) -> None:
    worktree = tmp_path / "worktree"
    state = tmp_path / "state"
    worktree.mkdir()

    config = write_hook_config(
        str(worktree), state_dir=str(state), agents_md="contract"
    )

    assert os.path.commonpath([config, state]) == str(state)
    assert not (worktree / ".codex").exists()
    assert not (worktree / "AGENTS.override.md").exists()
    text = open(config).read()
    assert str(state / "codex-hooks") in text
    assert 'developer_instructions = "contract' in text


def test_generated_governor_fails_closed_without_uv(tmp_path) -> None:
    worktree = tmp_path / "worktree"
    state = tmp_path / "state"
    worktree.mkdir()
    config = write_hook_config(str(worktree), state_dir=str(state))
    script = os.path.join(os.path.dirname(config), "hooks", "gov.sh")
    text = open(script).read()
    assert "uv not found; refusing" in text
    assert "exit 2" in text
    assert os.stat(script).st_mode & stat.S_IXUSR


def test_edit_check_delegates_to_the_shared_guarded_wrapper(tmp_path) -> None:
    worktree = tmp_path / "worktree"
    state = tmp_path / "state"
    worktree.mkdir()
    config = write_hook_config(
        str(worktree),
        state_dir=str(state),
        edit_check_cmd="uv run pyrefly check",
    )
    script = os.path.join(os.path.dirname(config), "hooks", "edit_check.sh")
    text = open(script).read()
    assert "plugin/hooks/edit_check.sh" in text
    assert "EDIT_CHECK_CMD=" in text
    assert "out=$(" not in text


def test_screenshot_marker_is_state_owned(tmp_path) -> None:
    worktree = tmp_path / "worktree"
    state = tmp_path / "state"
    worktree.mkdir()
    config = write_hook_config(str(worktree), state_dir=str(state))
    script = os.path.join(os.path.dirname(config), "hooks", "read_record.sh")
    text = open(script).read()
    assert str(state / "evidence") in text
    assert '.harness/screenshots-read' not in text


def test_repository_instructions_are_bounded_and_embedded(tmp_path) -> None:
    worktree = tmp_path / "worktree"
    state = tmp_path / "state"
    worktree.mkdir()
    (worktree / "CLAUDE.md").write_text("A" * 40_000)
    messages = []
    config = write_hook_config(
        str(worktree),
        state_dir=str(state),
        agents_md="contract",
        log=messages.append,
    )
    text = open(config).read()
    assert "contract" in text
    assert "truncated" in text
    assert messages


def test_missing_state_dir_refuses_containment(tmp_path) -> None:
    try:
        write_hook_config(str(tmp_path))
    except OSError as exc:
        assert "STATE_DIR" in str(exc)
    else:
        raise AssertionError("hook was written into an untrusted worktree")


def test_runtime_sets_codex_home_to_external_config(tmp_path) -> None:
    worktree = tmp_path / "worktree"
    state = tmp_path / "state"
    worktree.mkdir()

    class Runtime:
        task = None

        async def run(self, task):
            self.task = task
            return AgentResult(False, status="ok")

    inner = Runtime()
    runtime = HookPreparingRuntime(
        inner,
        lambda cwd: write_hook_config(cwd, state_dir=str(state)),
    )
    asyncio.run(runtime.run(RuntimePlan("work", cwd=str(worktree))))

    assert inner.task is not None
    assert inner.task.env["CODEX_HOME"].startswith(str(state))
