"""Shared review GitHub identity composition."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from ..adapters.app_token import AppCredentials, AppTokenMinter, default_mint_fn
from ..adapters.github import Identity


def _ignore_log(_message: str) -> None:
    pass


def review_identity(cfg_env: dict[str, str], warn: Callable[[str], None] = _ignore_log) -> Identity:
    """Resolve the daemon's App → static token → ambient identity contract."""
    if cfg_env.get("GH_APP_ID") and cfg_env.get("GH_APP_KEY_FILE") and cfg_env.get("GH_APP_INSTALL_ID"):
        key_file = os.path.expanduser(cfg_env["GH_APP_KEY_FILE"])
        if os.access(key_file, os.R_OK):
            creds = AppCredentials(
                app_id=cfg_env["GH_APP_ID"],
                install_id=cfg_env["GH_APP_INSTALL_ID"],
                key_file=key_file,
                cache_path=cfg_env.get(
                    "GH_APP_TOKEN_CACHE", os.path.expanduser("~/.local/state/issue-agent-bot/app-token.json")
                ),
            )
            minter = AppTokenMinter(creds, default_mint_fn, now_fn=lambda: int(time.time()))
            return Identity(token_provider=minter.get)
        if cfg_env.get("REVIEW_BOT_TOKEN"):
            warn(
                "identity: static token (REVIEW_BOT_TOKEN) — GH_APP_KEY_FILE "
                f"'{key_file}' is not present, so the GitHub App identity is "
                "not in use"
            )
        else:
            warn(
                f"GH_APP_KEY_FILE '{key_file}' is missing or unreadable — "
                "ignoring the GitHub App identity and falling back to ambient "
                "`gh auth` (which is FIX_BOT — approvals will 422)"
            )
    if cfg_env.get("REVIEW_BOT_TOKEN"):
        return Identity(static_token=cfg_env["REVIEW_BOT_TOKEN"])
    return Identity()
