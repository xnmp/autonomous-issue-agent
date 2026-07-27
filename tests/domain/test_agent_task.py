"""git_identity_env — commit attribution carries the leg's model."""

from issue_agent.domain.agent_task import git_identity_env


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
