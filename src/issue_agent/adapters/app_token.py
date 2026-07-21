"""GitHub App installation-token mint — port of bin/gh-app-token.sh.

Installation tokens expire ~hourly, so a daemon must re-mint. The mint flow is
inherently impure (RS256 sign + HTTPS POST), but the cache/expiry DECISION — "is
the cached token still good for >5 min?" — is pure and is the part with the
lesson (a UTC-parsing bug once made the cache look permanently expired). So that
decision is a pure function tested with an injected clock; the crypto/HTTP live
behind injected callables.

Used by the GitHubGateway for the review + steward identities (the App acts as
its own `<app>[bot]`, distinct from the fixer → satisfies no-self-approval).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass

# Re-mint when fewer than this many seconds of life remain (bash: 5 min).
REFRESH_MARGIN_S = 300


@dataclass(frozen=True)
class CachedToken:
    token: str
    expires_epoch: int


def token_is_fresh(cached: CachedToken | None, now_epoch: int) -> bool:
    """Pure: may we reuse the cached token? True iff >REFRESH_MARGIN_S of life
    remains. Port of the bash `exp > now+300` guard."""
    if cached is None or not cached.token:
        return False
    return cached.expires_epoch > now_epoch + REFRESH_MARGIN_S


def load_cache(path: str) -> CachedToken | None:
    """Read the token cache file (the bash JSON shape). None if absent/garbage."""
    try:
        with open(path) as f:
            data = json.load(f)
        return CachedToken(
            token=data.get("token", ""),
            expires_epoch=int(data.get("expires_epoch", 0) or 0),
        )
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return None


def write_cache(path: str, token: str, expires_at: str, expires_epoch: int) -> None:
    """Persist the cache (0600), matching the bash file shape."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps(
        {"token": token, "expires_at": expires_at, "expires_epoch": expires_epoch}
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload + "\n")


@dataclass(frozen=True)
class AppCredentials:
    app_id: str
    install_id: str
    key_file: str
    cache_path: str


class AppTokenMinter:
    """Mints + caches installation tokens. The crypto+HTTP exchange is injected
    (`mint_fn`) so the cache logic is unit-tested without network or a real key;
    the production wiring passes a `mint_fn` that signs a JWT and POSTs to GitHub
    (see `default_mint_fn`)."""

    def __init__(
        self,
        creds: AppCredentials,
        mint_fn: Callable[[AppCredentials], tuple[str, str, int]],
        now_fn: Callable[[], int],
    ):
        # mint_fn(creds) -> (token, expires_at_iso, expires_epoch)
        self._creds = creds
        self._mint = mint_fn
        self._now = now_fn

    def get(self) -> str:
        cached = load_cache(self._creds.cache_path)
        if token_is_fresh(cached, self._now()):
            return cached.token  # type: ignore[union-attr]
        token, expires_at, expires_epoch = self._mint(self._creds)
        if not token:
            raise RuntimeError("gh-app-token: mint returned empty token")
        write_cache(self._creds.cache_path, token, expires_at, expires_epoch)
        return token


def default_mint_fn(creds: AppCredentials) -> tuple[str, str, int]:
    """Production mint: RS256-sign a short-lived App JWT and exchange it for an
    installation token (port of the bash JWT + curl). Pure-logic parts (cache,
    freshness) are tested separately; this is the thin live shell.

    Uses PyJWT + urllib (stdlib) to avoid a curl/openssl dependency. Returns
    (token, expires_at_iso, expires_epoch).
    """
    import datetime as _dt
    import json as _json
    import urllib.request

    import jwt as _jwt  # PyJWT (from the [sdk] extra deps)

    now = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())
    with open(creds.key_file) as f:
        private_key = f.read()
    assertion = _jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": creds.app_id},
        private_key,
        algorithm="RS256",
    )
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{creds.install_id}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {assertion}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - fixed https host
        data = _json.loads(resp.read().decode())
    token = data.get("token", "")
    expires_at = data.get("expires_at", "")
    exp_epoch = now + 3300  # default ~55 min if parse fails
    if expires_at:
        try:
            exp_epoch = int(
                _dt.datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=_dt.timezone.utc)
                .timestamp()
            )
        except ValueError:
            pass
    return token, expires_at, exp_epoch
