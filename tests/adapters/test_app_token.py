"""App-token mint — pure cache/expiry decision + mint-on-miss, injected clock."""

import json
import os
import tempfile

from issue_agent.adapters.app_token import (
    AppCredentials,
    AppTokenMinter,
    CachedToken,
    MINT_ATTEMPTS,
    MINT_BACKOFF_S,
    REFRESH_MARGIN_S,
    load_cache,
    token_is_fresh,
    write_cache,
)


def test_fresh_when_far_from_expiry():
    assert token_is_fresh(CachedToken("t", expires_epoch=10_000), now_epoch=5_000) is True


def test_stale_within_margin():
    # <5 min of life → must re-mint (the bash >now+300 guard).
    now = 10_000
    assert token_is_fresh(CachedToken("t", now + REFRESH_MARGIN_S - 1), now) is False


def test_stale_when_none_or_empty():
    assert token_is_fresh(None, 0) is False
    assert token_is_fresh(CachedToken("", 10_000), 0) is False


def test_cache_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sub", "token.json")
        write_cache(p, "tok", "2026-01-01T00:00:00Z", 1735689600)
        c = load_cache(p)
        assert c.token == "tok" and c.expires_epoch == 1735689600
        # 0600 perms
        assert oct(os.stat(p).st_mode)[-3:] == "600"


def test_load_missing_is_none():
    assert load_cache("/nonexistent/xyz.json") is None


def test_minter_reuses_fresh_cache():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "token.json")
        write_cache(p, "cached-tok", "z", 10_000)
        creds = AppCredentials("1", "2", "key.pem", p)
        calls = []

        def mint(c):
            calls.append(c)
            return ("new", "z", 99_999)

        m = AppTokenMinter(creds, mint_fn=mint, now_fn=lambda: 5_000)  # fresh
        assert m.get() == "cached-tok"
        assert calls == []  # no mint


def test_minter_remints_when_stale_and_writes_cache():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "token.json")
        write_cache(p, "old", "z", 10_000)
        creds = AppCredentials("1", "2", "key.pem", p)

        def mint(c):
            return ("fresh-tok", "2026-06-01T00:00:00Z", 9_999_999)

        m = AppTokenMinter(creds, mint_fn=mint, now_fn=lambda: 9_999)  # stale
        assert m.get() == "fresh-tok"
        # cache rewritten with the new token
        assert load_cache(p).token == "fresh-tok"


def test_minter_raises_on_empty_mint():
    with tempfile.TemporaryDirectory() as d:
        creds = AppCredentials("1", "2", "key.pem", os.path.join(d, "t.json"))
        sleeps = []
        m = AppTokenMinter(
            creds, mint_fn=lambda c: ("", "", 0), now_fn=lambda: 0, sleep_fn=sleeps.append
        )
        try:
            m.get()
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass
        # empty token is retried like any other mint failure — all 3 attempts spent
        assert sleeps == list(MINT_BACKOFF_S)


def test_minter_retries_then_succeeds_on_transient_mint_failure():
    # A GitHub hiccup (network blip, 5xx) on the first N-1 attempts must not kill
    # the daemon's identity — the mint should be retried, not raised immediately.
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "token.json")
        creds = AppCredentials("1", "2", "key.pem", p)
        sleeps = []
        calls = {"n": 0}

        def flaky_mint(c):
            calls["n"] += 1
            if calls["n"] < MINT_ATTEMPTS:  # fail all but the last attempt
                raise RuntimeError("transient GitHub error")
            return ("fresh-tok", "2026-06-01T00:00:00Z", 9_999_999)

        m = AppTokenMinter(
            creds, mint_fn=flaky_mint, now_fn=lambda: 0, sleep_fn=sleeps.append
        )
        assert m.get() == "fresh-tok"
        assert calls["n"] == MINT_ATTEMPTS
        # backoff recorded between attempts (not after the final, successful one)
        assert sleeps == list(MINT_BACKOFF_S)
        assert load_cache(p).token == "fresh-tok"


def test_minter_raises_last_error_when_all_attempts_fail():
    with tempfile.TemporaryDirectory() as d:
        creds = AppCredentials("1", "2", "key.pem", os.path.join(d, "t.json"))
        sleeps = []
        calls = {"n": 0}

        def always_fails(c):
            calls["n"] += 1
            raise ValueError(f"boom {calls['n']}")

        m = AppTokenMinter(
            creds, mint_fn=always_fails, now_fn=lambda: 0, sleep_fn=sleeps.append
        )
        try:
            m.get()
            assert False, "expected ValueError"
        except ValueError as exc:
            assert str(exc) == f"boom {MINT_ATTEMPTS}"  # the LAST attempt's error
        assert calls["n"] == MINT_ATTEMPTS
        assert sleeps == list(MINT_BACKOFF_S)
