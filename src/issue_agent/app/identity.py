"""Each role's GitHub identity: resolved once at boot, verified, fail-closed (ADR 0001).

A role acts only on its own credential: the fix role on FIX_BOT_TOKEN, the
review role on its GitHub App when the key is readable, else REVIEW_BOT_TOKEN.
No credential refuses to start. There is no fallback to the operator's ambient
`gh` login (keifu 2026-08-02: the fixer ran as the human and spent that
account's GraphQL pool). The login GitHub names for the credential must be the
configured FIX_BOT/REVIEW_BOT: keyed writes are read back by that author
(#424), and the sibling role, which holds no credential to ask, reads it from
config. The role then uses GitHub's spelling of its own login.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping

from ..adapters.app_token import AppCredentials, AppTokenMinter, default_mint_fn
from ..adapters.github import Identity
from ..domain.identity import login_eq

_TOKEN = {"fix": "FIX_BOT_TOKEN", "review": "REVIEW_BOT_TOKEN"}
_BOT = {"fix": "FIX_BOT", "review": "REVIEW_BOT"}
# A user login (alphanumerics and hyphens; `_` in an Enterprise Managed User's
# `name_shortcode`) or an App's `slug[bot]`.
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?")


def _app_identity(env: Mapping[str, str]) -> Identity | None:
    """The review App, when all three settings are set and its key is readable.
    The shipped example's placeholders name no readable key, so they leave the
    token in charge (tauri-explorer 2026-07-26: they captured this branch and
    every poll died on ENOENT)."""
    if not (env.get("GH_APP_ID") and env.get("GH_APP_KEY_FILE") and env.get("GH_APP_INSTALL_ID")):
        return None
    key_file = os.path.expanduser(env["GH_APP_KEY_FILE"])
    if not os.access(key_file, os.R_OK):
        return None
    creds = AppCredentials(
        app_id=env["GH_APP_ID"], install_id=env["GH_APP_INSTALL_ID"], key_file=key_file,
        cache_path=env.get("GH_APP_TOKEN_CACHE", os.path.expanduser("~/.local/state/issue-agent-bot/app-token.json")),
    )
    return Identity(token_provider=AppTokenMinter(creds, default_mint_fn, now_fn=lambda: int(time.time())).get)


def role_credential(role: str, env: Mapping[str, str]) -> tuple[Identity, str]:
    """`role`'s own credential and its name, or ValueError. Offline, so a hot
    reload can refuse a candidate env that would leave the role without one."""
    if role not in _TOKEN:
        raise ValueError(f"unknown role: {role!r} (use fix|review)")
    app = _app_identity(env) if role == "review" else None
    if app is not None:
        return app, "GitHub App"
    source = _TOKEN[role]
    token = (env.get(source) or "").strip()
    if not token:
        raise ValueError(f"{source} is not set{' and no GitHub App key is readable' if role == 'review' else ''}: "
                         f"the {role} role acts only on its own credential, never the operator's gh login")
    return Identity(static_token=token), source


def resolve_role_identity(
    role: str, env: Mapping[str, str], expected: str, viewer: Callable[[Identity], str],
    log: Callable[[str], None],
) -> tuple[Identity, str]:
    """`role`'s credential and the login GitHub names for it (`viewer`), or
    ValueError, which `main` maps to INVALID_CONFIG. `expected` is the
    configured FIX_BOT or REVIEW_BOT."""
    identity, source = role_credential(role, env)
    answer = (viewer(identity) or "").strip()
    login = answer if LOGIN.fullmatch(answer) else ""
    if not login_eq(login, expected):
        named = repr(login) if login else f"nobody GitHub would name (answer {answer[:80]!r})"
        raise ValueError(f"{_BOT[role]} is {expected!r} but {source} acts as {named}: set {_BOT[role]} "
                         f"to the credential's login, or give the {role} role {_BOT[role]}'s credential")
    log(f"identity: acts as {login} via {source}")
    return identity, login
