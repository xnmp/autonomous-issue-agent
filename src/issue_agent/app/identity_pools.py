"""Which GitHub credential does each daemon actually use, and whose rate-limit
pool does it spend?

2026-08-02 (keifu), config-class gaps doctor could not see, each of which
cost an operator surgery:

  1. IDENTITY_MODE=three with FIX_BOT_TOKEN unset. `main._daemon` builds the fix
     gateway as `Identity(static_token=env.get("FIX_BOT_TOKEN") or None)`, and an
     Identity with no token is AMBIENT — the credential `gh auth login` stored
     for the human at the keyboard. So the fixer branched, commented and opened
     PRs as that person, and its GraphQL calls came out of that person's
     personal 5,000-point/hour pool. The pool was exhausted twice and the
     harness was blind for roughly 50 minutes of each hour.

  2. Two roles on one credential (the fixer and reviewer both falling through
     to the ambient credential). At POLL_SECONDS=20 two daemons rebuilding the
     world every tick exceed one 5,000-point/hour pool outright (issue #163).

Both are decidable offline from the composed env, which is why they live here
and not behind a probe: the resolution rules are the harness's own, and reading
GitHub would only tell us what the pool looks like right now, not which roles
are pointed at it.

Nothing in this module ever stores or prints token material. A pool identity is
a per-process SALTED digest prefix, used only to answer "same credential or
not" within one report.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Callable

from issue_agent.app.codex_resolution import Finding
from issue_agent.domain.identity import IdentityMode

# GitHub's per-account GraphQL budget. Not configurable and not probed: it is
# GitHub's published figure for a user-owned token, and the check exists to be
# answerable with no network at all.
GRAPHQL_HOURLY_POINTS = 5000

# One daemon tick rebuilds the whole world through `gh` list/view calls. ~14
# points measured on the keifu deployment (see app/world_probe.py, ADR 0010,
# issue #163). This is the WORST case per tick by construction: the change probe
# answers a 1-REST-point question first and skips the rebuild when nothing moved,
# so a quiet repo burns far less. A busy repo has no quiet ticks.
WORLD_BUILD_GRAPHQL_POINTS = 14

# Above this share of the pool the arrangement is reported as a warning rather
# than as information. Headroom below 20% leaves nothing for the operator's own
# `gh` usage, for retry ladders, or for a burst of harness writes.
BURN_WARN_FRACTION = 0.8

# `HarnessConfig.poll_seconds` default — what the estimate assumes when
# POLL_SECONDS is absent or unparseable.
DEFAULT_POLL_SECONDS = 20

SECONDS_PER_HOUR = 3600

ROLES = ("fix", "review")

AMBIENT_POOL = "ambient"

AMBIENT_SOURCE = "ambient `gh auth` credential"


@dataclass(frozen=True)
class RoleIdentity:
    """The credential one role will run on.

    `pool` is an opaque equality key: two roles whose `pool` matches spend from
    ONE GitHub rate-limit budget. `ambient` marks the fall-through case, where
    the credential is not the harness's at all but whatever `gh auth login`
    left behind."""

    role: str
    source: str
    """Operator-facing description of where the credential comes from — an env
    var name, the GitHub App, or the ambient fall-through."""
    pool: str
    ambient: bool = False
    blank_secret: bool = False
    """The var is set to whitespace. `main` tests it with `or None`, so a blank
    string is CONFIGURED as far as the daemon is concerned and goes out as a
    bearer token GitHub will reject."""


@dataclass(frozen=True)
class SharedPool:
    """Two or more roles resolved to one credential."""

    roles: tuple[str, ...]
    source: str
    ambient: bool
    points_per_hour: int

    @property
    def fraction(self) -> float:
        return self.points_per_hour / GRAPHQL_HOURLY_POINTS


@dataclass(frozen=True)
class IdentityPoolReport:
    identity_mode: IdentityMode
    roles: tuple[RoleIdentity, ...]
    poll_seconds: int
    """The cadence the estimate used — the configured value, or
    DEFAULT_POLL_SECONDS when it was absent or unparseable."""
    poll_seconds_configured: str
    """POLL_SECONDS exactly as configured, '' when absent. Kept verbatim so the
    report can say which value it could not use."""
    poll_seconds_valid: bool
    shared_pools: tuple[SharedPool, ...]
    findings: tuple[Finding, ...]

    @property
    def healthy(self) -> bool:
        return not any(f.level in ("warn", "error") for f in self.findings)


def resolve_identity_mode(raw: object) -> IdentityMode:
    """IDENTITY_MODE as `config._coerce_identity_mode` resolves it: exact match
    after trimming, and ANY other value — absent, empty, `Solo`, `SOLO`, junk —
    lands on the THREE default.

    Case must NOT be folded here. `IdentityMode.from_env` is deliberately
    case-SENSITIVE because the bash half compares `[ "$IDENTITY_MODE" = three ]`
    literally, so `IDENTITY_MODE=Solo` really does run the harness in
    three-identity mode. Folding it in this check would report a clean bill of
    health on precisely the config that reproduces the keifu incident."""
    try:
        return IdentityMode.from_env(str(raw or ""))
    except ValueError:
        return IdentityMode.THREE


def parse_poll_seconds(raw: object) -> tuple[int, bool]:
    """(cadence to estimate with, whether the configured value was usable).

    Mirrors `config._coerce_int`'s posture: operator input never raises here. A
    non-integer, a negative, or a 0 (which is a busy loop, not "disabled", and
    which `HarnessConfig.validate` rejects outright) all fall back to the
    default so the burn estimate still gets made — an unparseable cadence must
    not silently delete the check that depends on it. `str()` is load-bearing:
    a programmatic caller can hand this an int, and `doctor` may never traceback
    on a config value."""
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_POLL_SECONDS, False
    try:
        value = int(text)
    except ValueError:
        return DEFAULT_POLL_SECONDS, False
    if value <= 0:
        return DEFAULT_POLL_SECONDS, False
    return value, True


def estimated_points_per_hour(role_count: int, poll_seconds: int) -> int:
    """Worst-case GraphQL points one credential spends in an hour when
    `role_count` daemons poll it every `poll_seconds` and every tick rebuilds.

    A cadence longer than an hour still means one build per hour, not zero: a
    daemon always builds its first world."""
    if role_count <= 0:
        return 0
    ticks = max(1, SECONDS_PER_HOUR // max(1, poll_seconds))
    return role_count * ticks * WORLD_BUILD_GRAPHQL_POINTS


# Fresh per process. Only EQUALITY WITHIN ONE REPORT is ever needed, so the key
# does not have to be stable across runs — and an unsalted digest would be a
# confirmation oracle: anyone holding a candidate PAT could match it against a
# key that leaked through a dataclass repr, and the same credential could be
# correlated across logs and deployments. The salt costs nothing and removes
# both properties.
_POOL_SALT = os.urandom(16)


def _token_pool(token: str) -> str:
    """A per-process equality key for a secret that is not the secret, and from
    which the secret cannot be confirmed."""
    digest = hashlib.sha256(_POOL_SALT + token.encode("utf-8", "replace"))
    return "token:" + digest.hexdigest()[:12]


def resolve_role_identities(
    env: dict,
    *,
    key_readable: Callable[[str], bool] | None = None,
) -> tuple[RoleIdentity, ...]:
    """Mirror of `app.main`'s identity construction, one entry per role.

    Faithfulness matters more than tidiness here: the point of the check is to
    say what the daemons WILL do, so the conditions are the same conditions
    (`or None` truthiness for FIX_BOT_TOKEN, all three GH_APP_* set AND the key
    file readable for the App branch). `key_readable` is injected so the App
    branch is testable without a real key on disk; it defaults to expanding `~`
    against the env's OWN `HOME`, because `GH_APP_KEY_FILE="$HOME/..."` under a
    systemd unit expands against the SERVICE user, not against whoever happened
    to run `doctor`."""
    readable = (
        key_readable if key_readable is not None
        else lambda path: _key_is_readable(path, env.get("HOME") or "")
    )

    fix_token = env.get("FIX_BOT_TOKEN") or ""
    if fix_token:
        fix = RoleIdentity("fix", "FIX_BOT_TOKEN", _token_pool(fix_token),
                           blank_secret=not fix_token.strip())
    else:
        fix = RoleIdentity("fix", AMBIENT_SOURCE, AMBIENT_POOL, ambient=True)

    app_id = env.get("GH_APP_ID") or ""
    key_file = env.get("GH_APP_KEY_FILE") or ""
    install_id = env.get("GH_APP_INSTALL_ID") or ""
    review_token = env.get("REVIEW_BOT_TOKEN") or ""
    if app_id and key_file and install_id and readable(key_file):
        shared = RoleIdentity("", f"GitHub App {app_id} (install {install_id})",
                              f"app:{app_id}/{install_id}")
    elif review_token:
        shared = RoleIdentity("", "REVIEW_BOT_TOKEN", _token_pool(review_token),
                              blank_secret=not review_token.strip())
    else:
        shared = RoleIdentity("", AMBIENT_SOURCE, AMBIENT_POOL, ambient=True)

    return (
        fix,
        RoleIdentity("review", shared.source, shared.pool, shared.ambient,
                     shared.blank_secret),
    )


def _key_is_readable(key_file: str, home: str = "") -> bool:
    if home and key_file.startswith("~"):
        key_file = home + key_file[1:]
    return os.access(os.path.expanduser(key_file), os.R_OK)


def _unique(values) -> list[str]:
    """Order-preserving dedup — findings must read in role order, not set order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def analyse_identity_pools(
    identity_mode: IdentityMode,
    roles: tuple[RoleIdentity, ...],
    poll_seconds: int,
    poll_seconds_configured: str,
    poll_seconds_valid: bool,
    fixer_account: str = "",
) -> IdentityPoolReport:
    """Pure: the findings implied by an already-resolved set of role identities."""
    findings: list[Finding] = []
    mode = identity_mode

    fix = next((r for r in roles if r.role == "fix"), None)
    if fix is not None and fix.ambient and mode is IdentityMode.THREE:
        who = (
            f"'{fixer_account}'" if fixer_account
            else "whichever account ran `gh auth login`"
        )
        findings.append(Finding(
            "warn",
            "IDENTITY_MODE=three but FIX_BOT_TOKEN is not set, so the fix daemon "
            f"falls through to the ambient `gh auth` credential and acts as {who} "
            "— a human account, not a bot. Every branch, comment and pull request "
            "is attributed to that person, and the fixer's GraphQL calls are "
            f"charged to that person's {GRAPHQL_HOURLY_POINTS:,}-point/hour pool, "
            "shared with their own `gh` usage (keifu exhausted it twice on "
            "2026-08-02 and the harness was blind ~50 minutes of every hour). "
            "Fix: mint a PAT for the fixer bot account and set FIX_BOT_TOKEN in "
            "config/shared.env.",
        ))

    for source in _unique(r.source for r in roles if r.blank_secret):
        findings.append(Finding(
            "error",
            f"{source} is set to whitespace. It is non-empty, so the daemon "
            "treats it as configured and sends it as a bearer token — GitHub 401s "
            "every call, and the role never falls back to anything. Unset it or "
            "give it a real token.",
        ))

    shared_pools = _shared_pools(roles, poll_seconds)
    for pool in shared_pools:
        findings.append(_burn_finding(
            pool, poll_seconds, poll_seconds_configured, poll_seconds_valid
        ))

    return IdentityPoolReport(
        identity_mode=mode,
        roles=roles,
        poll_seconds=poll_seconds,
        poll_seconds_configured=poll_seconds_configured,
        poll_seconds_valid=poll_seconds_valid,
        shared_pools=shared_pools,
        findings=tuple(findings),
    )


def _shared_pools(
    roles: tuple[RoleIdentity, ...], poll_seconds: int
) -> tuple[SharedPool, ...]:
    """Groups of two or more roles on one credential, in role order.

    Grouping is on the RESOLVED pool, never on the config text. Two roles whose
    token vars are both unset are not "the same unset var" — they are two roles
    that both fell through to the one ambient credential, which is a real shared
    pool and is reported as such."""
    order: list[str] = []
    groups: dict[str, list[RoleIdentity]] = {}
    for role in roles:
        if role.pool not in groups:
            groups[role.pool] = []
            order.append(role.pool)
        groups[role.pool].append(role)
    pools: list[SharedPool] = []
    for pool in order:
        members = groups[pool]
        if len(members) < 2:
            continue
        # Distinct var names on one digest means the SAME secret was pasted into
        # two vars — worth naming, because the config reads as two identities.
        sources = _unique(m.source for m in members)
        pools.append(SharedPool(
            roles=tuple(m.role for m in members),
            source=(
                sources[0] if len(sources) == 1
                else " = ".join(sources) + ", one and the same secret"
            ),
            ambient=members[0].ambient,
            points_per_hour=estimated_points_per_hour(len(members), poll_seconds),
        ))
    return tuple(pools)


def _burn_finding(
    pool: SharedPool,
    poll_seconds: int,
    poll_seconds_configured: str,
    poll_seconds_valid: bool,
) -> Finding:
    names = " + ".join(pool.roles)
    cadence = f"POLL_SECONDS={poll_seconds}"
    if not poll_seconds_valid:
        why = (
            f"the configured {poll_seconds_configured!r} is not a positive integer"
            if poll_seconds_configured else "POLL_SECONDS is not set"
        )
        cadence = f"POLL_SECONDS={poll_seconds} (the default — {why})"
    ticks = max(1, SECONDS_PER_HOUR // max(1, poll_seconds))
    how = (
        f"all fall through to the {AMBIENT_SOURCE}" if pool.ambient
        else f"resolve to the same credential ({pool.source})"
    )
    level = "warn" if pool.fraction > BURN_WARN_FRACTION else "info"
    # POLL_SECONDS first, deliberately: the cadence is the knob that always
    # applies.
    remedy = (
        " Raise POLL_SECONDS, or give each role its own credential."
        if level == "warn" else ""
    )
    return Finding(
        level,
        f"{names} {how}, so they share ONE GitHub rate-limit pool of "
        f"{GRAPHQL_HOURLY_POINTS:,} GraphQL points/hour. Upper bound at {cadence}: "
        f"{len(pool.roles)} roles x {ticks} ticks/hour x "
        f"{WORLD_BUILD_GRAPHQL_POINTS} points per world build = "
        f"~{pool.points_per_hour:,} points/hour, {_percent(pool.fraction)} of the "
        f"pool (warn above {BURN_WARN_FRACTION:.0%}). It is an upper bound twice "
        f"over: {WORLD_BUILD_GRAPHQL_POINTS} points is the FIXER's world build "
        f"applied to every role, and the ADR 0010 change probe answers an unchanged tick from "
        f"the REST quota — a separate bucket — instead of rebuilding. A busy repo "
        f"has no unchanged ticks.{remedy}",
    )


def _percent(fraction: float) -> str:
    """Below 10% a whole-number percentage rounds a real number to '0%' next to a
    non-zero points/hour figure, which reads as a bug in the estimate."""
    return f"{fraction:.0%}" if fraction >= 0.1 else f"{fraction:.1%}"


def identity_pool_report(
    env: dict,
    *,
    key_readable: Callable[[str], bool] | None = None,
) -> IdentityPoolReport:
    """Resolve every role's credential out of a composed env and analyse it."""
    poll_seconds, poll_valid = parse_poll_seconds(env.get("POLL_SECONDS"))
    return analyse_identity_pools(
        identity_mode=resolve_identity_mode(env.get("IDENTITY_MODE")),
        roles=resolve_role_identities(env, key_readable=key_readable),
        poll_seconds=poll_seconds,
        poll_seconds_configured=str(env.get("POLL_SECONDS") or "").strip(),
        poll_seconds_valid=poll_valid,
        fixer_account=(env.get("FIX_BOT") or "").strip(),
    )
