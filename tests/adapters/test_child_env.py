from issue_agent.adapters.child_env import child_process_env


def test_child_environment_is_allowlisted_and_has_no_publication_credentials():
    inherited = {
        "PATH": "/bin",
        "HOME": "/home/agent",
        "ANTHROPIC_AUTH_TOKEN": "runtime-secret",
        "GH_TOKEN": "publication-secret",
        "GITHUB_TOKEN": "publication-secret-2",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
        "UNRELATED": "ambient",
    }

    selected = child_process_env(
        inherited,
        {"ISSUE_AGENT_RUN_ID": "run-1", "GH_TOKEN": "still-denied"},
    )

    assert selected == {
        "PATH": "/bin",
        "HOME": "/home/agent",
        "ANTHROPIC_AUTH_TOKEN": "runtime-secret",
        "ISSUE_AGENT_RUN_ID": "run-1",
    }


def test_provider_specific_auth_key_can_be_admitted_explicitly():
    selected = child_process_env(
        {"CUSTOM_PROVIDER_KEY": "secret", "NOPE": "discarded"},
        additional_keys=("CUSTOM_PROVIDER_KEY",),
    )

    assert selected == {"CUSTOM_PROVIDER_KEY": "secret"}
