"""Secrets delivered as systemd encrypted credentials (ADR 0001, #413).

The service units carry `ImportCredential=NAME`: at every unit start systemd
imports `~/.config/credstore.encrypted/NAME` (when it exists) and decrypts it
into `$CREDENTIALS_DIRECTORY/NAME` for that unit alone. Nothing lands in the
env snapshot, the unit file, or the journal. launchd and foreground runs have
no credential directory and keep reading the env file.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

SECRET_NAMES = ("FIX_BOT_TOKEN", "REVIEW_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
# What each role's unit imports (ops/systemd/*.service): least privilege.
ROLE_SECRETS = {
    "fix": ("FIX_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
    "review": ("REVIEW_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
}
MAX_SECRET_BYTES = 8192  # PATs and OAuth tokens are well under 1 KiB


class CredentialError(ValueError):
    """A systemd credential is unusable, competes with an env value, or exists
    on this host but did not reach the process."""


def credstore_file(home: str, name: str) -> str:
    """Where the operator keeps the encrypted credential for `name`."""
    return os.path.join(home, ".config", "credstore.encrypted", name)


def in_credstore(home: str, name: str) -> bool:
    """Existence only: the contents cannot be decrypted outside a unit."""
    return os.path.isfile(credstore_file(home, name))


def _read_delivered(directory: str, name: str) -> str | None:
    try:
        with open(os.path.join(directory, name), "rb") as stream:
            raw = stream.read(MAX_SECRET_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError(f"{name}: systemd credential is unreadable ({type(exc).__name__})") from exc
    if len(raw) > MAX_SECRET_BYTES:
        raise CredentialError(f"{name}: systemd credential exceeds {MAX_SECRET_BYTES} bytes")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CredentialError(f"{name}: systemd credential is not valid UTF-8") from exc
    if not value:
        raise CredentialError(f"{name}: systemd credential is empty")
    return value


def with_credentials(
    env: Mapping[str, str],
    read: Callable[[str, str], str | None] = _read_delivered,
) -> dict[str, str]:
    """`env` with each secret filled from `$CREDENTIALS_DIRECTORY/<NAME>`.

    A secret in both places is refused (two sources of truth drift apart); the
    same value in both is not, since a hot reload re-resolves an environment
    this process already filled."""
    resolved = dict(env)
    directory = env.get("CREDENTIALS_DIRECTORY", "")
    for name in SECRET_NAMES if directory else ():
        delivered = read(directory, name)
        if delivered is None:
            continue
        inline = (env.get(name) or "").strip()
        if inline and inline != delivered:
            raise CredentialError(f"{name} is set in both the environment and a systemd credential; "
                                  "remove the plaintext line from the env file")
        resolved[name] = delivered
    return resolved


def require_stored_secrets(env: Mapping[str, str], role: str) -> None:
    """Fail closed: when `role`'s unit would have imported a credstore file
    that never arrived (foreground run, or a reload after the plaintext was
    removed) and the env has no value, refuse, naming where the secret is."""
    home = env.get("HOME") or os.path.expanduser("~")
    for name in ROLE_SECRETS.get(role, ()):
        if not (env.get(name) or "").strip() and in_credstore(home, name):
            raise CredentialError(f"{name} is stored in ~/.config/credstore.encrypted but was not delivered "
                                  "and is not in the environment: run the installed service, or export it")
