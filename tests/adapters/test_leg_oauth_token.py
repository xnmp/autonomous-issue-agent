"""A Claude leg's own long-lived login (`claude setup-token`), not the operator's."""

from __future__ import annotations

from issue_agent.adapters.child_env import child_process_env


def test_a_claude_leg_receives_its_own_oauth_token_but_not_the_unit_credential_directory():
    selected = child_process_env(
        {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "leg-login", "CREDENTIALS_DIRECTORY": "/run/credentials/x"})

    assert selected == {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "leg-login"}


def test_a_codex_leg_does_not_receive_the_claude_oauth_token(monkeypatch):
    from issue_agent.adapters.codex_runner import CodexRunner

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "leg-login")
    runner = CodexRunner.__new__(CodexRunner)
    runner.env = {}
    runner.provider = type("Provider", (), {"env_key": "OPENAI_API_KEY"})()
    runner._resolved_model = lambda _task: "m"

    environment = runner._child_environment(type("Task", (), {"env": {}})())

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in environment
