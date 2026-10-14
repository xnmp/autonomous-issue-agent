"""Secrets delivered as systemd encrypted credentials (ADR 0001, #413).

`LoadCredentialEncrypted=NAME:path` in a service unit decrypts the file into
`$CREDENTIALS_DIRECTORY/NAME` for that unit only; nothing lands in the env
snapshot, the unit file, or the journal. The three names below are the whole
set; macOS (launchd) and foreground runs keep reading them from the env file.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

SECRET_NAMES = ("FIX_BOT_TOKEN", "REVIEW_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
MAX_SECRET_BYTES = 8192  # PATs and OAuth tokens are well under 1 KiB


class CredentialError(ValueError):
    """A systemd credential is unusable, or competes with an env value."""


def credentials_home(home: str) -> str:
    """Where the operator keeps the encrypted `<NAME>.cred` files."""
    return os.path.join(home, ".config", "issue-agent", "credentials")


def stored_names(home: str) -> tuple[str, ...]:
    """The secrets with an encrypted file on this host (existence only: the
    contents cannot be decrypted outside a unit, and are never read here)."""
    base = credentials_home(home)
    return tuple(n for n in SECRET_NAMES if os.path.isfile(os.path.join(base, f"{n}.cred")))


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
    value = raw.decode("utf-8", errors="replace").strip()
    if not value:
        raise CredentialError(f"{name}: systemd credential is empty")
    return value


def with_credentials(
    env: Mapping[str, str],
    read: Callable[[str, str], str | None] = _read_delivered,
) -> dict[str, str]:
    """`env` with each secret filled from `$CREDENTIALS_DIRECTORY/<NAME>`.

    A name with no delivered credential keeps its env value. A name with both
    is refused: two sources of truth for one secret drift apart. The same value
    in both is not a conflict: a hot reload re-resolves an environment this
    process already filled from the credential."""
    resolved = dict(env)
    directory = env.get("CREDENTIALS_DIRECTORY", "")
    if not directory:
        return resolved
    for name in SECRET_NAMES:
        delivered = read(directory, name)
        if delivered is None:
            continue
        inline = (env.get(name) or "").strip()
        if inline and inline != delivered:
            raise CredentialError(f"{name} is set in both the environment and a systemd credential; "
                                  "remove the plaintext line from the env file")
        resolved[name] = delivered
    return resolved
