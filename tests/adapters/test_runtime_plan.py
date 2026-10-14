"""The runtime plan: git identity and rendered execution context."""

from dataclasses import replace

from issue_agent.adapters.runtime_plan import RuntimePlan, git_identity_env


def test_identity_names_harness_and_model():
    env = git_identity_env("gpt-5.6-sol")
    assert env == {"GIT_AUTHOR_NAME": "Harness - gpt-5.6-sol", "GIT_COMMITTER_NAME": "Harness - gpt-5.6-sol"}


def test_no_model_says_nothing():
    assert git_identity_env(None) == {}
    assert git_identity_env("") == {}


def test_emails_are_never_touched():
    assert not any(k.endswith("EMAIL") for k in git_identity_env("m"))


def test_replace_changes_execution_plan_and_keeps_context():
    original = RuntimePlan(
        prompt="work", cwd="/old", context_suffix="\n\nRepository context"
    )
    changed = replace(original, cwd="/new", resume="session")

    assert (original.cwd, changed.cwd, changed.resume) == ("/old", "/new", "session")
    assert changed.rendered_prompt == original.rendered_prompt == "work\n\nRepository context"


def test_replacing_the_task_preserves_repository_context():
    original = RuntimePlan(
        prompt="Old requirements", context_suffix="\n\nTrailing context"
    )
    changed = replace(original, prompt="New requirements")

    assert changed.rendered_prompt == "New requirements\n\nTrailing context"
    assert original.rendered_prompt == "Old requirements\n\nTrailing context"
    assert RuntimePlan(prompt="bare").rendered_prompt == "bare"
