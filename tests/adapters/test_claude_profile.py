"""Claude Code profile → injected environment (FIXER_RUNTIME=glm)."""

from __future__ import annotations

import asyncio
import json

import pytest

from issue_agent.adapters.claude_profile import (
    EnvInjectingRuntime,
    claude_profile_env,
    endpoint_host,
    parse_dotenv,
)
from issue_agent.adapters.runtime_plan import RuntimePlan
from issue_agent.domain.outcome import AgentResult

SETTINGS_ENV = {
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.2[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4.7",
    "API_TIMEOUT_MS": "3000000",
}


def _profile(tmp_path, *, settings=None, dotenv: str | None = "ZAI_API_KEY=sk-secret-123\n"):
    d = tmp_path / ".cc-glm"
    d.mkdir()
    f = d / "settings.json"
    f.write_text(json.dumps({"env": dict(SETTINGS_ENV)} if settings is None else settings))
    if dotenv is not None:
        (d / ".env").write_text(dotenv)
    return d, str(f)


def test_profile_env_carries_the_settings_block_config_dir_and_auth_token(tmp_path):
    d, settings_file = _profile(tmp_path)

    env = claude_profile_env(settings_file)

    assert {k: env[k] for k in SETTINGS_ENV} == SETTINGS_ENV
    assert env["CLAUDE_CONFIG_DIR"] == str(d)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-secret-123"


def test_anthropic_api_key_is_blanked_not_omitted(tmp_path):
    """SDK options.env merges onto os.environ, so an omitted key would let an
    inherited Anthropic credential survive and bill the wrong account."""
    _, settings_file = _profile(tmp_path)

    env = claude_profile_env(settings_file)

    assert env["ANTHROPIC_API_KEY"] == ""


def test_a_profile_without_a_dotenv_still_yields_a_usable_env(tmp_path):
    """The key may be exported in the launching shell instead."""
    d, settings_file = _profile(tmp_path, dotenv=None)

    env = claude_profile_env(settings_file)

    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert env["CLAUDE_CONFIG_DIR"] == str(d)


def test_non_string_settings_values_are_skipped_rather_than_coerced(tmp_path):
    _, settings_file = _profile(
        tmp_path, settings={"env": {"OK": "yes", "TIMEOUT": 3000, "NESTED": {"a": 1}}}
    )

    env = claude_profile_env(settings_file)

    assert env["OK"] == "yes"
    assert "TIMEOUT" not in env and "NESTED" not in env


def test_a_settings_file_without_an_env_block_is_not_an_error(tmp_path):
    _, settings_file = _profile(tmp_path, settings={"hooks": {"Stop": []}})

    env = claude_profile_env(settings_file)

    assert env["ANTHROPIC_API_KEY"] == ""
    assert "ANTHROPIC_BASE_URL" not in env


def test_a_missing_settings_file_raises_naming_the_path(tmp_path):
    with pytest.raises(ValueError) as exc:
        claude_profile_env(str(tmp_path / "nope" / "settings.json"))

    assert "settings.json" in str(exc.value)


def test_malformed_json_raises_naming_the_path_and_never_a_secret(tmp_path):
    d, settings_file = _profile(tmp_path)
    (d / "settings.json").write_text('{"env": {"A": "b",,}')

    with pytest.raises(ValueError) as exc:
        claude_profile_env(settings_file)

    message = str(exc.value)
    assert str(settings_file) in message
    assert "sk-secret-123" not in message


def test_a_settings_file_that_is_not_an_object_raises(tmp_path):
    _, settings_file = _profile(tmp_path, settings=["not", "an", "object"])

    with pytest.raises(ValueError):
        claude_profile_env(settings_file)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("A=1\nB=2\n", {"A": "1", "B": "2"}),
        ('Q="quoted"\nS=\'single\'\n', {"Q": "quoted", "S": "single"}),
        ("# comment\n\n  \nA=1\n", {"A": "1"}),
        ("export A=1\n", {"A": "1"}),
        ("A=has=equals\n", {"A": "has=equals"}),
        ("garbage line\nA=1\n", {"A": "1"}),
        ("A=\n", {"A": ""}),
    ],
)
def test_dotenv_parsing_is_textual_not_shell(text, expected):
    assert parse_dotenv(text) == expected


def test_dotenv_values_are_never_shell_expanded():
    """A `.env` is READ, never sourced: no interpolation, no substitution."""
    assert parse_dotenv("A=$HOME/x\nB=$(whoami)\n") == {"A": "$HOME/x", "B": "$(whoami)"}


def test_endpoint_host_reports_the_host_only():
    assert endpoint_host({"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}) == "api.z.ai"
    assert endpoint_host({}) == "(default)"


# ── EnvInjectingRuntime ──────────────────────────────────────────────────────


class _RecordingRuntime:
    def __init__(self):
        self.tasks: list[RuntimePlan] = []

    async def run(self, task: RuntimePlan) -> AgentResult:
        self.tasks.append(task)
        return AgentResult(is_error=False, status="done")


def _run(runtime, task):
    return asyncio.run(runtime.run(task))


def test_the_runtime_injects_its_env_into_every_task():
    inner = _RecordingRuntime()
    runtime = EnvInjectingRuntime(inner, env={"ANTHROPIC_BASE_URL": "https://api.z.ai"})

    result = _run(runtime, RuntimePlan(prompt="fix it"))

    assert result.status == "done"
    assert inner.tasks[0].env == {"ANTHROPIC_BASE_URL": "https://api.z.ai"}
    assert inner.tasks[0].prompt == "fix it"


def test_the_tasks_own_env_wins_over_the_injected_one():
    """TEST_CMD and friends are per-dispatch facts a profile must not shadow."""
    inner = _RecordingRuntime()
    runtime = EnvInjectingRuntime(inner, env={"TEST_CMD": "profile", "BASE": "z"})

    _run(runtime, RuntimePlan(prompt="p", env={"TEST_CMD": "uv run pytest"}))

    assert inner.tasks[0].env == {"TEST_CMD": "uv run pytest", "BASE": "z"}


def test_injection_does_not_mutate_the_caller_or_the_configured_env():
    inner = _RecordingRuntime()
    env = {"BASE": "z"}
    runtime = EnvInjectingRuntime(inner, env=env)
    task = RuntimePlan(prompt="p", env={"TEST_CMD": "t"})

    _run(runtime, task)

    assert env == {"BASE": "z"}
    assert task.env == {"TEST_CMD": "t"}
