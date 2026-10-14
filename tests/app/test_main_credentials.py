import pytest

from issue_agent.app.main import _load_agent_env


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
