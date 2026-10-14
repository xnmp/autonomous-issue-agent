import pytest

from issue_agent.adapters.credentials import MAX_SECRET_BYTES, CredentialError, require_stored_secrets, with_credentials


def _delivered(tmp_path, **files):
    directory = tmp_path / "creds"
    directory.mkdir(exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content)
    return {"CREDENTIALS_DIRECTORY": str(directory)}


def test_a_delivered_credential_fills_its_secret_without_the_trailing_newline(tmp_path):
    env = {**_delivered(tmp_path, FIX_BOT_TOKEN="ghp_fix\n"), "REPO": "o/r"}

    assert with_credentials(env)["FIX_BOT_TOKEN"] == "ghp_fix"
    assert with_credentials(env)["REPO"] == "o/r"


def test_the_env_value_is_used_when_nothing_is_delivered(tmp_path):
    assert with_credentials({"FIX_BOT_TOKEN": "from-env"})["FIX_BOT_TOKEN"] == "from-env"
    assert with_credentials({**_delivered(tmp_path), "FIX_BOT_TOKEN": "from-env"})["FIX_BOT_TOKEN"] == "from-env"


def test_a_secret_in_both_sources_is_refused_naming_the_variable_but_not_its_value(tmp_path):
    env = {**_delivered(tmp_path, REVIEW_BOT_TOKEN="cred-secret"), "REVIEW_BOT_TOKEN": "env-secret"}

    with pytest.raises(CredentialError, match="REVIEW_BOT_TOKEN") as raised:
        with_credentials(env)
    assert "cred-secret" not in str(raised.value) and "env-secret" not in str(raised.value)


def test_a_blank_env_value_does_not_compete_with_a_credential(tmp_path):
    env = {**_delivered(tmp_path, FIX_BOT_TOKEN="tok"), "FIX_BOT_TOKEN": "  "}

    assert with_credentials(env)["FIX_BOT_TOKEN"] == "tok"


def test_re_resolving_an_environment_already_filled_from_the_credential_is_stable(tmp_path):
    # Hot reload validates os.environ, which the first start filled in.
    once = with_credentials(_delivered(tmp_path, FIX_BOT_TOKEN="tok"))

    assert with_credentials(once) == once


@pytest.mark.parametrize("content", ["", "\n", "  \r\n"])
def test_an_empty_credential_is_a_config_error(tmp_path, content):
    with pytest.raises(CredentialError, match="FIX_BOT_TOKEN"):
        with_credentials(_delivered(tmp_path, FIX_BOT_TOKEN=content))


def test_an_oversized_credential_is_a_config_error(tmp_path):
    with pytest.raises(CredentialError, match="exceeds"):
        with_credentials(_delivered(tmp_path, CLAUDE_CODE_OAUTH_TOKEN="x" * (MAX_SECRET_BYTES + 1)))


def test_an_unreadable_credential_entry_is_a_config_error(tmp_path):
    env = _delivered(tmp_path)
    (tmp_path / "creds" / "FIX_BOT_TOKEN").mkdir()  # a directory where a file belongs

    with pytest.raises(CredentialError, match="unreadable"):
        with_credentials(env)


def test_only_the_three_known_secrets_are_read(tmp_path):
    resolved = with_credentials(_delivered(tmp_path, SOMETHING_ELSE="x", CLAUDE_CODE_OAUTH_TOKEN="oauth"))

    assert resolved["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth" and "SOMETHING_ELSE" not in resolved


def test_a_non_utf8_credential_is_a_config_error(tmp_path):
    env = _delivered(tmp_path)
    (tmp_path / "creds" / "FIX_BOT_TOKEN").write_bytes(b"ghp_\xff\xfe")

    with pytest.raises(CredentialError, match="UTF-8"):
        with_credentials(env)


def _credstore(home, *names):
    base = home / ".config" / "credstore.encrypted"
    base.mkdir(parents=True)
    for name in names:
        (base / name).write_text("encrypted")
    return {"HOME": str(home)}


def test_a_stored_secret_that_never_arrived_refuses_to_start_naming_the_variable(tmp_path):
    # Foreground start, or a reload after the plaintext was removed: the
    # daemon would otherwise fall back to the operator's ambient gh login.
    env = _credstore(tmp_path, "FIX_BOT_TOKEN")

    with pytest.raises(CredentialError, match="FIX_BOT_TOKEN"):
        require_stored_secrets(env, "fix")


def test_a_stored_secret_exported_in_the_environment_starts_in_the_foreground(tmp_path):
    env = {**_credstore(tmp_path, "FIX_BOT_TOKEN"), "FIX_BOT_TOKEN": "ghp_exported"}

    require_stored_secrets(env, "fix")  # does not raise


def test_a_stored_secret_delivered_to_the_unit_starts(tmp_path):
    env = {**_credstore(tmp_path / "h", "FIX_BOT_TOKEN"), **_delivered(tmp_path, FIX_BOT_TOKEN="ghp")}

    require_stored_secrets(with_credentials(env), "fix")  # does not raise


def test_a_role_is_not_refused_over_a_secret_its_unit_never_imports(tmp_path):
    env = _credstore(tmp_path, "REVIEW_BOT_TOKEN")

    require_stored_secrets(env, "fix")  # does not raise
    with pytest.raises(CredentialError, match="REVIEW_BOT_TOKEN"):
        require_stored_secrets(env, "review")


def test_the_oauth_token_in_the_credstore_is_required_by_both_roles(tmp_path):
    env = _credstore(tmp_path, "CLAUDE_CODE_OAUTH_TOKEN")

    for role in ("fix", "review"):
        with pytest.raises(CredentialError, match="CLAUDE_CODE_OAUTH_TOKEN"):
            require_stored_secrets(env, role)

