"""Runtime planning keeps provider launch details outside the domain."""

from dataclasses import fields, replace

import pytest

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
    original = RuntimePlan(prompt="work", cwd="/old", context_prefix="Prior scratch notes\n\n", context_suffix="\n\nRepository context")
    changed = replace(original, cwd="/new", resume="session")

    assert changed.intent is original.intent
    assert original.cwd == "/old"
    assert changed.cwd == "/new"
    assert changed.resume == "session"
    assert changed.prompt == original.prompt == "Prior scratch notes\n\nwork\n\nRepository context"
    assert changed.context_prefix == original.context_prefix
    assert changed.context_suffix == original.context_suffix


def test_context_is_rendered_for_providers_without_entering_authoritative_intent():
    intent = RunIntent(prompt="Author the current requirements")
    plan = RuntimePlan(intent=intent, context_prefix="Scratch: previous experiment failed.\n\n")

    assert plan.prompt == "Scratch: previous experiment failed.\n\nAuthor the current requirements"
    assert plan.intent is intent
    assert intent.prompt == "Author the current requirements"
    assert RuntimePlan(intent=intent).prompt == intent.prompt


def test_replacing_authoritative_intent_preserves_scratch_context():
    original = RuntimePlan(prompt="Old requirements", context_prefix="Scratch notes\n\n", context_suffix="\n\nTrailing context")
    changed = replace(original, intent=replace(original.intent, prompt="New requirements"))

    assert changed.prompt == "Scratch notes\n\nNew requirements\n\nTrailing context"
    assert original.prompt == "Scratch notes\n\nOld requirements\n\nTrailing context"


@pytest.mark.parametrize("context", [None, False, 1, b"notes", {}, []])
@pytest.mark.parametrize("replacing", [False, True])
@pytest.mark.parametrize("field", ["context_prefix", "context_suffix"])
def test_malformed_context_is_rejected_at_runtime_plan_boundary(context, replacing, field):
    with pytest.raises(ValueError, match="context must be text"):
        if replacing:
            replace(RuntimePlan("work"), **{field: context})
        else:
            RuntimePlan("work", **{field: context})
