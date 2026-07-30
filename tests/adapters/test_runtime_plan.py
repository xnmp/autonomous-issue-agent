"""Runtime planning keeps provider launch details outside the domain."""

from dataclasses import fields, replace

from issue_agent.adapters.runtime_plan import RuntimePlan, git_identity_env
from issue_agent.domain.agent_task import RunIntent


def test_identity_names_harness_and_model():
    env = git_identity_env("gpt-5.6-sol")
    assert env == {
        "GIT_AUTHOR_NAME": "Harness - gpt-5.6-sol",
        "GIT_COMMITTER_NAME": "Harness - gpt-5.6-sol",
    }


def test_no_model_says_nothing():
    assert git_identity_env(None) == {}
    assert git_identity_env("") == {}


def test_emails_are_never_touched():
    assert not any(k.endswith("EMAIL") for k in git_identity_env("m"))


def test_provider_settings_cannot_enter_domain_intent():
    plan = RuntimePlan(
        prompt="work",
        cwd="/tmp/worktree",
        resume="session",
        env={"TOKEN": "redacted"},
        allowed_tools=("Read",),
    )

    assert plan.intent == RunIntent(prompt="work")
    assert {item.name for item in fields(RunIntent)} == {
        "prompt",
        "schema",
        "system_instruction",
        "model",
        "effort",
        "maximum_budget_usd",
    }
    for provider_field in ("cwd", "resume", "env", "allowed_tools", "mcp_servers"):
        assert not hasattr(plan.intent, provider_field)


def test_replace_changes_execution_plan_without_mutating_intent():
    original = RuntimePlan(prompt="work", cwd="/old")
    changed = replace(original, cwd="/new", resume="session")

    assert changed.intent is original.intent
    assert original.cwd == "/old"
    assert changed.cwd == "/new"
    assert changed.resume == "session"
