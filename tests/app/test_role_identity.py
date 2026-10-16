"""Each role's GitHub identity (`app.identity.resolve_role_identity`, #443).

A role acts only on its own credential and refuses to start without one; no
role ever runs on the operator's ambient `gh` login (keifu 2026-08-02: the
fixer ran as the human and exhausted that account's GraphQL pool). The login
GitHub names for the credential must be the configured FIX_BOT / REVIEW_BOT,
since keyed writes are read back by that author (#424).

Regression kept from the review resolver: the shipped example's GH_APP_*
placeholders plus a real REVIEW_BOT_TOKEN must resolve to the token
(tauri-explorer 2026-07-26: the placeholders captured the App branch and every
poll died on ENOENT).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_agent.adapters.github import Identity
from issue_agent.app import main as main_module
from issue_agent.app.identity import resolve_role_identity

PLACEHOLDER_APP = {
    "GH_APP_ID": "<your-app-id>",
    "GH_APP_INSTALL_ID": "<your-installation-id>",
    "GH_APP_KEY_FILE": "/home/nobody/.config/<your-app>/private-key.pem",
}


@pytest.fixture
def app(tmp_path) -> dict[str, str]:
    """A GitHub App that is actually set up: all three settings, readable key."""
    key = tmp_path / "private-key.pem"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n")
    return {"GH_APP_ID": "123", "GH_APP_INSTALL_ID": "456", "GH_APP_KEY_FILE": str(key),
            "GH_APP_TOKEN_CACHE": str(tmp_path / "cache.json")}


class Viewer:
    """GitHub's `viewer { login }` answer for whichever credential asks."""

    def __init__(self, answer: str | None):
        self.answer = answer
        self.asked: list[Identity] = []

    def __call__(self, identity: Identity) -> str:
        self.asked.append(identity)
        return self.answer  # type: ignore[return-value]


def resolve(role: str, env: dict[str, str], expected: str, answer: str | None):
    viewer, lines = Viewer(answer), []
    identity, login = resolve_role_identity(role, env, expected, viewer, lines.append)
    return identity, login, viewer, lines


# ── fix role ──────────────────────────────────────────────────────────────────
def test_the_fix_role_acts_on_fix_bot_token_and_names_its_login():
    identity, login, viewer, lines = resolve("fix", {"FIX_BOT_TOKEN": " ghp_fix\n"}, "fix-bot", "fix-bot")

    assert identity.env_token() == "ghp_fix"
    assert viewer.asked == [identity]  # GitHub was asked about THIS credential
    assert login == "fix-bot"
    assert lines == ["identity: acts as fix-bot via FIX_BOT_TOKEN"]


@pytest.mark.parametrize("env", [{}, {"FIX_BOT_TOKEN": ""}, {"FIX_BOT_TOKEN": "  \n"}, {"REVIEW_BOT_TOKEN": "ghp_r"}])
def test_the_fix_role_without_its_token_refuses_and_never_asks_github(env):
    viewer = Viewer("operator")
    with pytest.raises(ValueError, match="FIX_BOT_TOKEN is not set"):
        resolve_role_identity("fix", env, "fix-bot", viewer, lambda _line: None)
    assert viewer.asked == []  # no ambient call, not even to check


def test_the_github_app_is_review_only_so_the_fix_role_still_refuses_without_a_token(app):
    with pytest.raises(ValueError, match="FIX_BOT_TOKEN is not set"):
        resolve("fix", app, "fix-bot", "my-reviewer-bot[bot]")


# ── review role ───────────────────────────────────────────────────────────────
def test_the_review_role_acts_on_review_bot_token():
    identity, login, _viewer, lines = resolve("review", {"REVIEW_BOT_TOKEN": "ghp_r"}, "review-bot", "review-bot")

    assert identity.env_token() == "ghp_r" and identity.token_provider is None
    assert login == "review-bot"
    assert lines == ["identity: acts as review-bot via REVIEW_BOT_TOKEN"]


def test_a_readable_app_key_wins_over_review_bot_token(app):
    identity, login, _viewer, lines = resolve(
        "review", {**app, "REVIEW_BOT_TOKEN": "ghp_r"}, "my-reviewer-bot[bot]", "my-reviewer-bot[bot]")

    assert identity.token_provider is not None and identity.static_token is None
    assert login == "my-reviewer-bot[bot]"
    assert lines == ["identity: acts as my-reviewer-bot[bot] via GitHub App"]


def test_an_app_key_path_is_tilde_expanded(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    identity, *_ = resolve("review", {**app, "GH_APP_KEY_FILE": "~/private-key.pem"}, "app[bot]", "app[bot]")
    assert identity.token_provider is not None


def test_placeholder_app_settings_leave_review_bot_token_in_charge():
    identity, *_ = resolve("review", {**PLACEHOLDER_APP, "REVIEW_BOT_TOKEN": "ghp_r"}, "review-bot", "review-bot")
    assert identity.env_token() == "ghp_r" and identity.token_provider is None


@pytest.mark.parametrize("missing", ["GH_APP_ID", "GH_APP_INSTALL_ID", "GH_APP_KEY_FILE"])
def test_a_half_set_app_is_not_configured_and_the_token_is_used(app, missing):
    env = {**app, "REVIEW_BOT_TOKEN": "ghp_r"}
    env.pop(missing)
    identity, *_ = resolve("review", env, "review-bot", "review-bot")
    assert identity.env_token() == "ghp_r"


@pytest.mark.parametrize("env", [{}, PLACEHOLDER_APP, {"REVIEW_BOT_TOKEN": " "}, {"FIX_BOT_TOKEN": "ghp_fix"}])
def test_the_review_role_without_a_credential_refuses_and_never_asks_github(env):
    """No ambient fallback: that was FIX_BOT's login, and GitHub 422s a review by the PR's author."""
    viewer = Viewer("fix-bot")
    with pytest.raises(ValueError, match="REVIEW_BOT_TOKEN is not set and no GitHub App key is readable"):
        resolve_role_identity("review", env, "review-bot", viewer, lambda _line: None)
    assert viewer.asked == []


# ── the credential's login must be the configured one ─────────────────────────
@pytest.mark.parametrize(("role", "env", "bot"), [
    ("fix", {"FIX_BOT_TOKEN": "ghp_fix"}, "FIX_BOT"),
    ("review", {"REVIEW_BOT_TOKEN": "ghp_r"}, "REVIEW_BOT"),
])
def test_a_credential_acting_as_someone_else_refuses_naming_both_logins(role, env, bot):
    with pytest.raises(ValueError, match=f"{bot} is 'configured-bot' but .* acts as 'someone-else'") as refused:
        resolve(role, env, "configured-bot", "someone-else")
    assert "ghp_" not in str(refused.value)  # token material never reaches the log


def test_an_enterprise_managed_user_login_is_a_login():
    _identity, login, *_ = resolve("fix", {"FIX_BOT_TOKEN": "t"}, "fixer_acme", "fixer_acme")
    assert login == "fixer_acme"


def test_the_login_matches_up_to_case_and_github_spelling_is_kept():
    _identity, login, *_ = resolve("fix", {"FIX_BOT_TOKEN": "t"}, "Fix-Bot[bot]", "fix-bot[bot]")
    assert login == "fix-bot[bot]"


@pytest.mark.parametrize("answer", [
    None, "", "   ", "null-but\nsecond-line", '{"message":"Bad credentials"}', "-leading-hyphen",
    "fix bot", "fix-bot[bot][bot]", "a" * 40, "fix-bot" + "x" * 100_000,
])
def test_a_malformed_or_empty_viewer_answer_refuses(answer):
    expected = "fix-bot"
    with pytest.raises(ValueError, match="acts as nobody GitHub would name") as refused:
        resolve("fix", {"FIX_BOT_TOKEN": "t"}, expected, answer)
    assert len(str(refused.value)) < 400  # a huge answer is not echoed whole


def test_an_unset_configured_login_never_matches():
    with pytest.raises(ValueError, match="FIX_BOT is ''"):
        resolve("fix", {"FIX_BOT_TOKEN": "t"}, "", "fix-bot")


# ── composition: `build_role` resolves before building anything ───────────────
def _env(tmp_path: Path, monkeypatch, **extra: str) -> dict[str, str]:
    monkeypatch.setattr("issue_agent.app.config.machine_control_authority_root", lambda: str(tmp_path / "machine"))
    monkeypatch.chdir(tmp_path)  # build_role enters TARGET_DIR; restore the worker's cwd afterwards
    return {"REPO": "o/r", "FIX_BOT": "fix-bot", "REVIEW_BOT": "review-bot", "TARGET_DIR": str(tmp_path),
            "STATE_DIR": str(tmp_path / "state" / "fix"), "REVIEW_STATE_DIR": str(tmp_path / "state" / "review"),
            "HOME": str(tmp_path / "home"), **extra}


@pytest.mark.parametrize("role", ["fix", "review"])
def test_build_role_without_a_credential_refuses_before_any_github_call(tmp_path, monkeypatch, role):
    asked: list[str] = []
    monkeypatch.setattr("issue_agent.adapters.github.GitHubGateway.viewer_login",
                        lambda self: asked.append(self.repo) or "operator")
    with pytest.raises(ValueError, match="is not set"):  # `_serve` maps it to INVALID_CONFIG
        main_module.build_role(role, _env(tmp_path, monkeypatch))
    assert asked == []


@pytest.mark.parametrize(("role", "token", "login"), [("fix", "ghp_fix", "fix-bot"), ("review", "ghp_review", "review-bot")])
def test_build_role_asks_github_with_the_roles_own_token(tmp_path, monkeypatch, role, token, login):
    tokens: list[str | None] = []

    def viewer_login(self):
        tokens.append(self.identity.env_token())
        return login

    monkeypatch.setattr("issue_agent.adapters.github.GitHubGateway.viewer_login", viewer_login)
    env = _env(tmp_path, monkeypatch, FIX_BOT_TOKEN="ghp_fix", REVIEW_BOT_TOKEN="ghp_review")

    assert main_module.build_role(role, env) is not None
    assert tokens == [token]


@pytest.mark.parametrize("role", ["fix", "review"])
def test_build_role_refuses_when_github_names_another_login(tmp_path, monkeypatch, role):
    monkeypatch.setattr("issue_agent.adapters.github.GitHubGateway.viewer_login", lambda self: "operator")
    env = _env(tmp_path, monkeypatch, FIX_BOT_TOKEN="ghp_fix", REVIEW_BOT_TOKEN="ghp_review")
    with pytest.raises(ValueError, match="acts as 'operator'"):
        main_module.build_role(role, env)


@pytest.mark.parametrize("expected,answer", [("steward[bot]", "steward"), ("steward", "steward[bot]")])
def test_same_name_different_account_type_refuses_at_boot(expected, answer):
    with pytest.raises(ValueError, match="acts as"):
        resolve("fix", {"FIX_BOT_TOKEN": "t"}, expected, answer)
