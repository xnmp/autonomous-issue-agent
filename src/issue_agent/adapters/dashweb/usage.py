"""Infrastructure adapter for runtime budget headroom.

Three different meters answering one operator question — "can I start another
batch right now?". None of them is the harness's own data, so all three parse
defensively: an unknown shape becomes an `error` string beside null percentages,
never an exception and never a fabricated number.

**The Claude OAuth token never leaves this module.** It is read, put in one
`Authorization` header aimed at api.anthropic.com, and dropped. It is not
logged, not cached, not returned, and not included in any error text.
"""

from __future__ import annotations

import glob
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ..journal import default_ledger_path, sum_today
from .ghdata import TtlCache

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA_HEADER = "oauth-2025-04-20"
CLAUDE_TIMEOUT_S = 5.0
CLAUDE_TTL_S = 60.0

# Percentage field names seen across the usage endpoint's shapes. `utilization`
# is today's; the rest are the drift this parser is meant to survive.
_PCT_KEYS = ("utilization", "used_percent", "percent_used", "percent", "pct")
_RESET_KEYS = ("resets_at", "reset_at", "resets_at_iso", "reset")


def default_credentials_path() -> str:
    return os.path.expanduser("~/.claude/.credentials.json")


def default_codex_sessions_root() -> str:
    return os.path.expanduser("~/.codex/sessions")


def _iso_from_epoch(value) -> str | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _pct(block) -> float | None:
    if isinstance(block, bool):
        return None
    if isinstance(block, (int, float)):
        return float(block)
    if isinstance(block, dict):
        for key in _PCT_KEYS:
            value = block.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def _resets(block) -> str | None:
    if isinstance(block, str):
        return block or None
    if isinstance(block, (int, float)) and not isinstance(block, bool):
        return _iso_from_epoch(block)
    if not isinstance(block, dict):
        return None
    for key in _RESET_KEYS:
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
        iso = _iso_from_epoch(value)
        if iso:
            return iso
    return None


def parse_claude_usage(payload) -> dict:
    """The usage endpoint's body → the contract's claude block. Pure.

    Accepts `{"five_hour": {"utilization": 12}}`, a bare
    `{"five_hour_pct": 12}`, and anything in between; a body that yields no
    percentage at all is reported as an error rather than as 0% used, because
    "0%" is exactly the reading that would talk an operator into a batch.
    """
    if not isinstance(payload, dict):
        return _claude_error("usage endpoint returned an unexpected shape")
    windows = {}
    for name in ("five_hour", "seven_day"):
        block = payload.get(name)
        pct = _pct(block)
        if pct is None:
            pct = _pct(payload.get(f"{name}_pct"))
        windows[name] = (pct, _resets(block) or _resets(payload.get(f"{name}_resets_at")))
    five_pct, five_reset = windows["five_hour"]
    seven_pct, seven_reset = windows["seven_day"]
    if five_pct is None and seven_pct is None:
        return _claude_error("usage endpoint returned an unexpected shape")
    return {
        "five_hour_pct": five_pct,
        "five_hour_resets_at": five_reset,
        "seven_day_pct": seven_pct,
        "seven_day_resets_at": seven_reset,
        "source": "oauth",
        "error": None,
    }


def _claude_error(message: str) -> dict:
    return {
        "five_hour_pct": None,
        "five_hour_resets_at": None,
        "seven_day_pct": None,
        "seven_day_resets_at": None,
        "source": "oauth",
        "error": message,
    }


def read_oauth_token(path: str) -> str:
    """The stored OAuth access token, or "". Callers must not store or log it."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return ""
    token = oauth.get("accessToken")
    return token if isinstance(token, str) else ""


def http_fetch(token: str) -> dict:
    """GET the usage endpoint. The ONLY place the token is used."""
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": CLAUDE_BETA_HEADER,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=CLAUDE_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


_claude_cache = TtlCache(ttl_s=CLAUDE_TTL_S)
_claude_last_good: dict[str, dict] = {}


def claude_usage(
    fetch=None,
    credentials_path: str | None = None,
    cache=None,
    last_good: dict[str, dict] | None = None,
) -> dict:
    """Claude subscription headroom. `fetch(token) -> dict` is injectable so no
    test ever touches the network."""
    using_default_cache = cache is None
    cache = _claude_cache if using_default_cache else cache
    if last_good is None:
        last_good = _claude_last_good if using_default_cache else {}
    cached = cache.get("claude")
    if cached is not None:
        return cached
    token = read_oauth_token(credentials_path or default_credentials_path())
    if not token:
        return cache.put(
            "claude",
            _claude_fallback(
                "no Claude OAuth credentials on this machine", last_good
            ),
        )
    try:
        payload = (fetch or http_fetch)(token)
    except urllib.error.HTTPError as exc:
        return cache.put(
            "claude",
            _claude_fallback(
                f"usage endpoint returned HTTP {exc.code}", last_good
            ),
        )
    except Exception as exc:  # noqa: BLE001 — a meter must never break the page
        # The token is never interpolated into this message.
        return cache.put(
            "claude",
            _claude_fallback(
                f"usage endpoint unreachable ({type(exc).__name__})", last_good
            ),
        )
    parsed = parse_claude_usage(payload)
    if parsed["error"] is not None:
        return cache.put(
            "claude",
            _claude_fallback(str(parsed["error"]), last_good),
        )
    last_good["claude"] = parsed
    return cache.put("claude", parsed)


def _claude_fallback(message: str, last_good: dict[str, dict]) -> dict:
    previous = last_good.get("claude")
    if previous is None:
        return _claude_error(message)
    return {
        **previous,
        "source": "oauth · cached",
        "error": f"{message}; showing the last successful reading",
    }


# ── codex ─────────────────────────────────────────────────────────────────────
def _codex_error(message: str) -> dict:
    return {
        "weekly_pct": None,
        "window_minutes": None,
        "resets_at": None,
        "plan": None,
        "source": "rollout",
        "sampled_at": None,
        "error": message,
    }


def newest_rollout(sessions_root: str) -> str:
    """The most recently modified `rollout-*.jsonl`, or "". Codex writes the
    rate-limit snapshot into whatever session is live, so newest-by-mtime is
    the only file that can hold a current reading."""
    files = glob.glob(os.path.join(sessions_root, "**", "rollout-*.jsonl"), recursive=True)
    best, best_mtime = "", -1.0
    for path in files:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def _find_key(obj, key: str):
    """Depth-first search for `key` anywhere in a decoded JSON value. The
    rate-limit block's nesting is codex's business and has moved before."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def parse_rollout_rate_limits(lines) -> dict | None:
    """The LAST `rate_limits` object in a rollout, or None. Pure over lines.

    Scanned from the end: a session appends a fresh snapshot per turn, and only
    the final one describes the window right now.
    """
    for line in reversed(list(lines)):
        line = line.strip()
        if not line or "rate_limits" not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # a torn tail line — same tolerance the journal reader has
        limits = _find_key(obj, "rate_limits")
        if isinstance(limits, dict):
            return limits
    return None


def codex_block(limits: dict | None, sampled_at: str | None) -> dict:
    """Rate-limit object → the contract's codex block. Pure."""
    if not isinstance(limits, dict):
        return _codex_error("no rate_limits snapshot in the newest codex rollout")
    primary = limits.get("primary")
    if not isinstance(primary, dict):
        return _codex_error("codex rate_limits carried no `primary` window")
    pct = primary.get("used_percent")
    window = primary.get("window_minutes")
    return {
        "weekly_pct": float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None,
        "window_minutes": int(window) if isinstance(window, (int, float)) and not isinstance(window, bool) else None,
        "resets_at": _iso_from_epoch(primary.get("resets_at")),
        "plan": limits.get("plan_type") or None,
        "source": "rollout",
        "sampled_at": sampled_at,
        "error": None,
    }


TAIL_BYTES = 256 * 1024
_CODEX_CACHE = TtlCache(ttl_s=60.0)


def tail_lines(path: str, tail_bytes: int = TAIL_BYTES) -> list[str]:
    """The last `tail_bytes` of a file as whole lines.

    Rollouts reach tens of megabytes and the snapshot we want is the last one,
    so reading the whole file to find it costs a full re-read every poll. The
    leading partial line is dropped unless the whole file fit.
    """
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - tail_bytes)
        fh.seek(start)
        chunk = fh.read()
    text = chunk.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[1:] if start and lines else lines


def codex_usage(sessions_root: str | None = None) -> dict:
    root = sessions_root or default_codex_sessions_root()
    cached = _CODEX_CACHE.get(root)
    if cached is not None:
        return cached
    path = newest_rollout(root)
    if not path:
        return _CODEX_CACHE.put(root, _codex_error("no codex rollout files found"))
    try:
        lines = tail_lines(path)
        sampled_at = _iso_from_epoch(os.path.getmtime(path))
    except OSError as exc:
        return _CODEX_CACHE.put(
            root, _codex_error(f"codex rollout unreadable ({exc.strerror or 'error'})")
        )
    return _CODEX_CACHE.put(
        root, codex_block(parse_rollout_rate_limits(lines), sampled_at)
    )


# ── harness caps ──────────────────────────────────────────────────────────────
def harness_usage(names, config_for, ledger_path: str | None = None, now_fn=None) -> dict:
    """Today's ledgered spend against the configured caps, per deployment and
    in total. Reuses the CLI's cap resolution (overlay wins over shared) so the
    dashboard and `issue-agent status` cannot report different caps."""
    ledger = ledger_path or default_ledger_path()
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    per_repo = []
    global_cap = 0.0
    for name in names:
        repo, daily_cap, configured_global_cap = config_for(name)
        per_repo.append(
            {
                "name": name,
                "repo": repo,
                "today_usd": round(sum_today(ledger, now_fn, repo=repo or None), 4),
                "daily_cap_usd": daily_cap or None,
            }
        )
        global_cap = max(global_cap, configured_global_cap)
    return {
        "global_daily_cap_usd": global_cap or None,
        "today_all_repos_usd": round(sum_today(ledger, now_fn), 4),
        "per_repo": per_repo,
    }


def usage_payload(names, config_for, fetch=None) -> dict:
    """`GET /api/usage`. Each meter's own error is carried on its block; the
    top-level `warnings` list summarises them for the UI chip."""
    claude = claude_usage(fetch=fetch)
    codex = codex_usage()
    try:
        harness = harness_usage(names, config_for)
        harness_error = ""
    except Exception as exc:  # noqa: BLE001
        harness = {"global_daily_cap_usd": None, "today_all_repos_usd": 0.0, "per_repo": []}
        harness_error = f"harness caps unreadable ({exc})"
    warnings = [
        message
        for message in (claude.get("error"), codex.get("error"), harness_error)
        if message
    ]
    return {"claude": claude, "codex": codex, "harness": harness, "warnings": warnings}


__all__ = [
    "CLAUDE_USAGE_URL",
    "claude_usage",
    "codex_block",
    "codex_usage",
    "harness_usage",
    "newest_rollout",
    "parse_claude_usage",
    "parse_rollout_rate_limits",
    "read_oauth_token",
    "usage_payload",
]
