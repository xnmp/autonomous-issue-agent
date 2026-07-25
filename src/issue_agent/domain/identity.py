"""GitHub login identity — the ONE place login comparison lives (review C1).

Every comparison of GitHub logins (bot identities, PR/review authors) must go
through `login_eq`. The adversarial review reproduced the cost of ad-hoc `==`:
with `FIX_BOT='fix-bot[bot]'` the fixer's authorship filter matched nothing, so
address-review / fix-ci / rebase / auto-merge never dispatched — forever, with
no error (review C1).
"""

from __future__ import annotations


def normalize_login(login: str) -> str:
    """Canonical form of a GitHub login: casefold, then strip ONE trailing
    `[bot]` suffix (casefold first, so a literal `[BOT]` still strips). GitHub
    logins are case-insensitive; the suffix asymmetry is documented on
    `login_eq`. Exactly one suffix is stripped — a hypothetical literal
    `x[bot][bot]` login normalizes to `x[bot]`, not `x`."""
    return (login or "").casefold().removesuffix("[bot]")


def login_eq(a: str, b: str) -> bool:
    """Compare two GitHub logins, tolerating the `[bot]` suffix asymmetry. A
    GitHub App configured as `my-reviewer-bot[bot]` surfaces as that literal
    in some API shapes but as the bare `my-reviewer-bot` in the review
    author field `gh pr view --json reviews` returns. A raw `==` therefore never
    matches, which would silently defeat both the self-review guard and the
    rejection ceiling. Strip the suffix on both sides before comparing."""
    return normalize_login(a) == normalize_login(b)
