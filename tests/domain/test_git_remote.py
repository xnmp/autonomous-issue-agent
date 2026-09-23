"""Publication and base observation share configured repository identity."""

import pytest

from issue_agent.domain.git_remote import remote_identity, require_repository_remote, require_safe_fetch_url


@pytest.mark.parametrize("url", [
    "https://github.com/Owner/Repo.git",
    "https://github.com/Owner/Repo",
    "ssh://git@github.com/Owner/Repo.git",
    "git@github.com:Owner/Repo.git",
])
def test_supported_transport_forms_have_one_repository_identity(url):
    assert remote_identity(url) == ("github.com", "Owner/Repo")
    require_repository_remote(url, "owner/repo", "GITHUB.COM")


@pytest.mark.parametrize("url", [
    "https://elsewhere.invalid/owner/repo.git",
    "https://github.com/other/repo.git",
    "git@github.com:owner/other.git",
    "file:///tmp/owner/repo",
    "/tmp/owner/repo", "", "https://github.com/repo",
    "https://github.com/owner/repo/another",
])
def test_wrong_or_missing_remote_identity_cannot_authorize_repository_work(url):
    with pytest.raises(ValueError):
        require_repository_remote(url, "owner/repo", "github.com")


def test_configured_enterprise_host_is_not_inferred_from_repository_name():
    require_repository_remote("git@git.example.invalid:owner/repo.git", "owner/repo", "git.example.invalid")
    with pytest.raises(ValueError):
        require_repository_remote("git@github.com:owner/repo.git", "owner/repo", "git.example.invalid")


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo.git", "http://github.com/owner/repo.git",
    "ssh://git@github.com/owner/repo.git", "git@github.com:owner/repo.git",
    "git://github.com/owner/repo.git",
])
def test_captured_fetch_endpoint_permits_only_credentials_free_transports(url):
    require_safe_fetch_url(url)


@pytest.mark.parametrize("url", [
    "https://secret@github.com/owner/repo.git",
    "https://user:secret@github.com/owner/repo.git",
    "http://secret@github.com/owner/repo.git",
    "ssh://git:secret@github.com/owner/repo.git",
    "git://secret@github.com/owner/repo.git",
    "https://github.com/owner/repo.git?token=secret",
    "https://github.com/owner/repo.git#secret",
    "git@github.com:owner/repo.git?token=secret",
    "ftp://github.com/owner/repo.git",
    "ext://github.com/owner/repo.git",
    "--upload-pack=secret@github.com:owner/repo.git",
    None, "", "https://github.com/owner/repo.git\n", "x" * 4097,
])
def test_fetch_rejects_credential_and_ambiguous_endpoints_without_echoing_them(url):
    with pytest.raises(ValueError) as error:
        require_safe_fetch_url(url)
    assert "secret" not in str(error.value)
