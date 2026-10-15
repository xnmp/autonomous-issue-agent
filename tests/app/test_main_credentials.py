import pytest

from issue_agent.app.main import _load_agent_env, _validate_reload_candidate


def _delivered(tmp_path, **files):
    directory = tmp_path / "creds"
    directory.mkdir(exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content)
    return {"CREDENTIALS_DIRECTORY": str(directory)}


def test_the_daemon_environment_takes_the_secret_from_the_credential_not_the_env_file(tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text("REPO=o/r\n")
    env = {**_delivered(tmp_path, FIX_BOT_TOKEN="ghp_fix"), "AGENT_ENV": str(env_file), "PATH": "/usr/bin:/bin"}

    assert _load_agent_env(env)["FIX_BOT_TOKEN"] == "ghp_fix"


def test_the_daemon_refuses_to_start_when_the_env_file_also_carries_the_secret(tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text("REPO=o/r\nFIX_BOT_TOKEN=plaintext\n")
    env = {**_delivered(tmp_path, FIX_BOT_TOKEN="ghp_fix"), "AGENT_ENV": str(env_file), "PATH": "/usr/bin:/bin"}

    with pytest.raises(ValueError, match="FIX_BOT_TOKEN"):  # _serve maps ValueError to INVALID_CONFIG
        _load_agent_env(env)


def test_a_reload_after_the_plaintext_was_removed_refuses_when_the_credential_is_not_delivered(tmp_path):
    store = tmp_path / "home" / ".config" / "credstore.encrypted"
    store.mkdir(parents=True)
    (store / "FIX_BOT_TOKEN").write_text("encrypted")
    env_file = tmp_path / "agent.env"
    env_file.write_text("REPO=o/r\n")
    env = {"AGENT_ENV": str(env_file), "HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}

    with pytest.raises(ValueError, match="FIX_BOT_TOKEN"):
        _validate_reload_candidate(env, "fix")


@pytest.mark.parametrize(("role", "name"), [("fix", "FIX_BOT_TOKEN"), ("review", "REVIEW_BOT_TOKEN")])
def test_a_reload_candidate_without_the_roles_credential_is_refused_offline(tmp_path, role, name):
    """#443: the restarted daemon would refuse to start; refusing the reload keeps the running one."""
    env_file = tmp_path / "agent.env"
    env_file.write_text('REPO=o/r\nFIX_BOT=fix-bot\nREVIEW_BOT=review-bot\n')
    env = {"AGENT_ENV": str(env_file), "HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}

    with pytest.raises(ValueError, match=f"{name} is not set"):
        _validate_reload_candidate(env, role)
    env_file.write_text(f'REPO=o/r\nFIX_BOT=fix-bot\nREVIEW_BOT=review-bot\n{name}=ghp_x\n')
    _validate_reload_candidate(env, role)
