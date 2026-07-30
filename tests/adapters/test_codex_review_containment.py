"""Regression coverage for Codex review-only containment (#113)."""

import io
import json
import os
import sys
import importlib.util

import pytest

from issue_agent.adapters import codex_runner
from issue_agent.adapters.codex_hook import write_hook_config
from issue_agent.adapters.store import Store
from issue_agent.app.config import from_env
from issue_agent.app.planners.tasks import ReviewTaskConfig
from issue_agent.app.roles import build_review_daemon
from issue_agent.app.workflows.review import ReviewWorkflow, ReviewWorkflowConfig
from issue_agent.domain.agent_task import AgentTask
from issue_agent.domain.outcome import AgentResult
from issue_agent.domain.review import ReviewMode

from test_dispatch import FakeRunner
from test_roles import FIX_BOT, _BuildableGateway


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(ROOT, "plugin", "hooks", "bash_governor.py")
spec = importlib.util.spec_from_file_location("bash_governor_containment", HOOK)
hook = importlib.util.module_from_spec(spec)
sys.modules["bash_governor_containment"] = hook
spec.loader.exec_module(hook)


class CapturingCodexRunner:
    def __init__(self, **_kwargs):
        pass

    def preflight(self):
        return None


def test_codex_review_role_cannot_persist_mode_into_nested_fixer(tmp_path, monkeypatch):
    """The review role's real hook setup must not cage nested fixer worktrees."""
    monkeypatch.setattr(codex_runner, "CodexRunner", CapturingCodexRunner)
    target = tmp_path / "target"
    cfg = from_env({
        "REPO": "o/r", "FIX_BOT": FIX_BOT, "REVIEWER_RUNTIME": "codex",
        "REVIEW_STATE_DIR": str(tmp_path / "review-state"),
        "TARGET_DIR": str(target),
    })
    daemon = build_review_daemon(
        cfg, _BuildableGateway(), Store(str(tmp_path / "review-state")),
        FakeRunner(AgentResult(is_error=False)),
    )
    dispatcher = daemon.executor._run_agent.__closure__[0].cell_contents
    dispatcher.review_workflow.cfg.prepare_cwd(str(target))

    fixer = target / ".claude" / "worktrees" / "issue-113"
    fixer.mkdir(parents=True)
    write_hook_config(str(fixer))

    review_hook = (target / ".codex" / "hooks" / "gov.sh").read_text()
    fixer_hook = (fixer / ".codex" / "hooks" / "gov.sh").read_text()
    assert "ISSUE_AGENT_REVIEW_READ_ONLY" not in review_hook
    assert "ISSUE_AGENT_REVIEW_READ_ONLY" not in fixer_hook


def test_codex_review_sets_read_only_mode_on_its_child_task():
    cfg = ReviewWorkflowConfig(
        review=ReviewTaskConfig("o/r"), review_mode=ReviewMode.SINGLE,
        max_review_rejections=1, review_bot="review-bot",
        review_trigger_label="ai-pr-open", log=lambda _message: None,
        reviewer_runtime="codex", execution_cwd="/explicit/review-checkout",
        prepare_cwd=lambda _cwd: None,
    )
    workflow = ReviewWorkflow(cfg, None, None, None, None)

    prepared = workflow._prepare_task(AgentTask(prompt="review"))

    assert prepared is not None
    assert prepared.env == {"ISSUE_AGENT_REVIEW_READ_ONLY": "1"}


def test_codex_review_environment_blocks_mutating_commands(monkeypatch):
    monkeypatch.setenv("ISSUE_AGENT_REVIEW_READ_ONLY", "1")
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps({"tool_input": {"command": "git commit -m x"}})),
    )

    with pytest.raises(SystemExit) as exit_code:
        hook.main()

    assert exit_code.value.code == 2
