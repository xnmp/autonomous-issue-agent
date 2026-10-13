"""review identity resolution (`app.main._app_identity`).

This had NO tests, and that is exactly how the bug below shipped. The first real
deployment (xnmp/tauri-explorer, 2026-07-26) ran with PATH A from
config/shared.example.env — a bot account + REVIEW_BOT_TOKEN, with the GH_APP_*
placeholders left in place, as that path documents. `_app_identity` selected the
GitHub App branch because the three placeholder strings are non-empty, then
`AppTokenMinter.get` raised ENOENT on the literal
`$HOME/.config/<your-app>/private-key.pem`. Every poll pass died:

    review: poll pass failed (continuing): [Errno 2] No such file or directory:
      '/home/chong/.config/<your-app>/private-key.pem'

The reviewer never reviewed anything, and the daemon reported no error beyond a
line that reads like a transient network blip.

The assertions below pin the CONTRACT ("which identity does the resolver hand
back, and does it say so out loud"), not the mechanism — they observe the
returned Identity and the warnings emitted, never how the branch is written.
"""

from __future__ import annotations

import os

import pytest

from issue_agent.app.main import _app_identity


@pytest.fixture
def key_file(tmp_path):
    """A readable private key, i.e. a GitHub App that is actually set up."""
    p = tmp_path / "private-key.pem"
    p.write_text("-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n")
    return str(p)


def _warnings() -> tuple[list[str], object]:
    seen: list[str] = []
    return seen, seen.append


# ── the regression: placeholders must not capture the App branch ──────────────
def test_placeholder_app_config_falls_back_to_the_bot_token():
    """The shipped-example GH_APP_* placeholders + a real REVIEW_BOT_TOKEN must
    resolve to the TOKEN, not to an App identity that cannot mint."""
    seen, warn = _warnings()
    ident = _app_identity(
        {
            "GH_APP_ID": "<your-app-id>",
            "GH_APP_INSTALL_ID": "<your-installation-id>",
            "GH_APP_KEY_FILE": "/home/nobody/.config/<your-app>/private-key.pem",
            "REVIEW_BOT_TOKEN": "ghp_realtoken",
        },
        warn=warn,
    )
    assert ident.env_token() == "ghp_realtoken"
    assert ident.token_provider is None


def test_unusable_app_key_is_reported_not_swallowed():
    """Falling back silently would leave an operator who MEANT to use the App
    wondering why reviews run as someone else."""
    seen, warn = _warnings()
    _app_identity(
        {
            "GH_APP_ID": "123",
            "GH_APP_INSTALL_ID": "456",
            "GH_APP_KEY_FILE": "/nonexistent/private-key.pem",
            "REVIEW_BOT_TOKEN": "ghp_realtoken",
        },
        warn=warn,
    )
    assert len(seen) == 1
    assert "/nonexistent/private-key.pem" in seen[0]
    assert "REVIEW_BOT_TOKEN" in seen[0]


def test_fallback_to_ambient_warns_that_approvals_will_422():
    """With no bot token the fallback is ambient == FIX_BOT, so GitHub 422s every
    approval as a self-approval. That consequence belongs in the warning."""
    seen, warn = _warnings()
    ident = _app_identity(
        {"GH_APP_ID": "123", "GH_APP_INSTALL_ID": "456", "GH_APP_KEY_FILE": "/nonexistent/private-key.pem"}, warn=warn
    )
    assert ident.static_token is None and ident.token_provider is None  # ambient
    assert len(seen) == 1
    assert "422" in seen[0]


# ── the App path still works when it is genuinely configured ─────────────────
def test_readable_key_selects_the_app_identity(key_file):
    seen, warn = _warnings()
    ident = _app_identity(
        {
            "GH_APP_ID": "123",
            "GH_APP_INSTALL_ID": "456",
            "GH_APP_KEY_FILE": key_file,
            "REVIEW_BOT_TOKEN": "ghp_realtoken",  # present but MUST NOT win
        },
        warn=warn,
    )
    assert ident.token_provider is not None
    assert ident.static_token is None
    assert seen == []


def test_app_key_path_is_tilde_expanded(tmp_path, monkeypatch, key_file):
    """`GH_APP_KEY_FILE="~/…"` reaches us unexpanded when it is quoted in the env
    file; the readability probe and the minter must agree on the same path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    seen, warn = _warnings()
    ident = _app_identity(
        {"GH_APP_ID": "123", "GH_APP_INSTALL_ID": "456", "GH_APP_KEY_FILE": "~/" + os.path.basename(key_file)},
        warn=warn,
    )
    assert ident.token_provider is not None
    assert seen == []


# ── the branch is never entered when the App is simply not configured ────────
@pytest.mark.parametrize("missing", ["GH_APP_ID", "GH_APP_INSTALL_ID", "GH_APP_KEY_FILE"])
def test_partial_app_config_uses_the_token_without_warning(key_file, missing):
    """A half-set App block is 'not configured', not 'broken' — no warning, and
    certainly no crash."""
    env = {
        "GH_APP_ID": "123",
        "GH_APP_INSTALL_ID": "456",
        "GH_APP_KEY_FILE": key_file,
        "REVIEW_BOT_TOKEN": "ghp_realtoken",
    }
    env.pop(missing)
    seen, warn = _warnings()
    ident = _app_identity(env, warn=warn)
    assert ident.env_token() == "ghp_realtoken"
    assert seen == []


def test_no_identity_configured_at_all_is_ambient_and_quiet():
    seen, warn = _warnings()
    ident = _app_identity({}, warn=warn)
    assert ident.static_token is None and ident.token_provider is None
    assert seen == []


def test_warn_is_optional_so_callers_that_do_not_log_still_fall_back():
    """The default warn must be a no-op, not a missing argument — a caller that
    omits it must still get the fallback rather than a TypeError."""
    ident = _app_identity(
        {
            "GH_APP_ID": "123",
            "GH_APP_INSTALL_ID": "456",
            "GH_APP_KEY_FILE": "/nonexistent/private-key.pem",
            "REVIEW_BOT_TOKEN": "ghp_realtoken",
        }
    )
    assert ident.env_token() == "ghp_realtoken"
