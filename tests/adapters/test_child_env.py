from issue_agent.adapters.child_env import child_process_env, without_supervisor_authority


def test_child_environment_is_allowlisted_and_inherits_no_publication_credentials():
    inherited = {
        "PATH": "/bin",
        "HOME": "/home/agent",
        "ANTHROPIC_AUTH_TOKEN": "runtime-secret",
        "GH_TOKEN": "publication-secret",
        "GITHUB_TOKEN": "publication-secret-2",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
        "UNRELATED": "ambient",
    }

    selected = child_process_env(inherited, {"ISSUE_AGENT_RUN_ID": "run-1"})

    assert selected == {
        "PATH": "/bin",
        "HOME": "/home/agent",
        "ANTHROPIC_AUTH_TOKEN": "runtime-secret",
        "ISSUE_AGENT_RUN_ID": "run-1",
    }


def test_an_explicit_leg_credential_wins_over_an_inherited_one():
    # A fix leg's one token is named by the harness (the leg sandbox, #413);
    # the daemon's ambient token never reaches it.
    selected = child_process_env({"GH_TOKEN": "ambient", "PATH": "/bin"}, {"GH_TOKEN": "leg-token"})

    assert selected == {"PATH": "/bin", "GH_TOKEN": "leg-token"}


def test_provider_specific_auth_key_can_be_admitted_explicitly():
    selected = child_process_env(
        {"CUSTOM_PROVIDER_KEY": "secret", "NOPE": "discarded"}, additional_keys=("CUSTOM_PROVIDER_KEY",)
    )

    assert selected == {"CUSTOM_PROVIDER_KEY": "secret"}


def test_paid_child_receives_no_control_plane_values():
    selected = without_supervisor_authority(
        {
            "PATH": "/bin",
            "ISSUE_AGENT_AUTHORITY_BUNDLE": "authority",
            "ISSUE_AGENT_CONTROL_DB": "/state/control.db",
            "ISSUE_AGENT_RUN_ID": "run-1",
            "ISSUE_AGENT_OWNER_ID": "owner-1",
            "ISSUE_AGENT_BASE_BRANCH": "dev",
        }
    )

    assert selected == {"PATH": "/bin"}
