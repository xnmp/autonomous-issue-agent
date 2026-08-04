"""GitHub login identity — the ONE place login comparison lives (review C1).

Every comparison of GitHub logins (bot identities, PR/review authors) must go
through `login_eq`. The adversarial review reproduced the cost of ad-hoc `==`:
with `FIX_BOT='fix-bot[bot]'` the fixer's authorship filter matched nothing, so
address-review / fix-ci / rebase / auto-merge never dispatched — forever, with
no error (review C1).
"""

from __future__ import annotations

from enum import Enum


class IdentityMode(str, Enum):
    """How many distinct GitHub identities the harness runs under.

    THREE lets a separate reviewer approve fixer PRs; SOLO disables automated
    review because GitHub rejects self-approval. This must remain an enum: the
    reviewer and auto-merge guards have opposite polarity, so an unknown string
    must fail config validation rather than fall between both guards.
    """

    THREE = "three"
    SOLO = "solo"

    @classmethod
    def from_env(cls, mode: str) -> "IdentityMode":
        """Parse the case-sensitive shell contract without guessing a mode.

        Shell launchers compare this value literally, so accepting alternate
        casing here would make Python and shell supervision disagree.
        """
        return cls((mode or "").strip())


def normalize_login(login: str) -> str:
    """Canonical form of a GitHub login: strip surrounding whitespace, casefold,
    then strip ONE trailing `[bot]` suffix (casefold first, so a literal
    `[BOT]` still strips). GitHub logins are case-insensitive; the suffix
    asymmetry is documented on `login_eq`. Exactly one suffix is stripped — a
    hypothetical literal `x[bot][bot]` login normalizes to `x[bot]`, not `x`.

    The whitespace strip MUST happen first, before the suffix strip — a value
    like `"fix-bot[bot] "` (trailing space after the suffix, e.g. round-tripped
    through a shell env file) has to lose the space BEFORE `removesuffix`
    looks for a trailing `"[bot]"`, or the suffix never matches and the value
    is left un-normalized. Unstripped, an operator's `FIX_BOT='fix-bot '`
    reproduces the original C1 symptom (every fixer PR leg dead) and silently
    defeats both self-approval guards (review P1R-6)."""
    return (login or "").strip().casefold().removesuffix("[bot]")


def is_bot_login(login: str) -> bool:
    """True iff `login` carries the literal GitHub bot-account suffix `[bot]`
    (e.g. `dependabot[bot]`), tolerating surrounding whitespace and case. This
    is a SHAPE test, not an identity comparison — it deliberately does NOT go
    through `normalize_login`, which STRIPS that very suffix (so every bot
    login would otherwise read as "not a bot"). The one remaining raw,
    login-shaped `.endswith("[bot]")` comparison in the codebase
    (`world.Comment.is_bot`) routes through here instead of hand-rolling its
    own copy (review R2-11)."""
    return (login or "").strip().casefold().endswith("[bot]")


def login_eq(a: str, b: str) -> bool:
    """Compare two GitHub logins, tolerating the `[bot]` suffix asymmetry. A
    GitHub App configured as `my-reviewer-bot[bot]` surfaces as that literal
    in some API shapes but as the bare `my-reviewer-bot` in the review
    author field `gh pr view --json reviews` returns. A raw `==` therefore never
    matches, which would silently defeat both the self-review guard and the
    rejection ceiling. Strip the suffix on both sides before comparing.

    An UNKNOWN login (normalizes to "") never equals anything, including
    another unknown — `login_eq("", "")` is False (review P1R-7, reversing the
    prior documented contract). The prior contract's rationale was "callers
    must not rely on login_eq to reject an empty configured identity", but in
    practice a caller DID: an unresolved PR author (`author: null` from the
    adapter, e.g. a deleted/ghost account, normalizes to "") compared against
    an unset `FIX_BOT` ("") read as OURS, falsifying the fail-safe the
    authorship filter's own comment claimed. Unknown-vs-unknown must fail
    closed, not match — an absent identity must never accidentally "match" an
    absent author. Some call sites (domain/fix.py's PR filter) already guard
    both sides non-empty before calling this, so this reversal is defence in
    depth there; other call sites (domain/ci.py, domain/review.py) call this
    directly with no such guard, so the reversal is load-bearing there."""
    na, nb = normalize_login(a), normalize_login(b)
    if not na or not nb:
        return False
    return na == nb
