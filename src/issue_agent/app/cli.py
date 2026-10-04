"""`issue-agent` — operator CLI, runnable from inside a target repo.

A thin front end over the tested bash engine. It auto-detects the repo + working
copy from the current directory, synthesizes (or reuses) the per-repo overlay
`config/repos/<name>.env`, and then delegates the heavy lifting to
`bin/point-at.sh` (env compose → doctor → base branch → labels → auto-merge →
proxy check → single-instance guard → run all three watchers under one trap).

Why a checkout-resident CLI and not a PyPI wheel: the daemon walks `__file__` up
to the harness root to load `plugin/AGENT.md`, `plugin/hooks/bash_governor.py`,
and `config/mcp/*.json` — none of which ship in the wheel. `bin/install.sh`
installs this editable so `HARNESS_ROOT` (below) still resolves those siblings.

Subcommands: init | start | stop | status | dashboard | doctor | ready |
triage | install-service | uninstall-service. The pure helpers (detect_repo,
resolve_name, synth_repo_env) are unit-tested with no git/network.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from ..adapters.env_file import source_env_file
from ..adapters.heartbeat import heartbeat_liveness, read_heartbeat_time
from ..adapters.operator_effects import build_operator_effects
from ..domain.contracts import (
    DEFAULT_TEST_FAILURE_EXIT_CODES,
    parse_test_failure_exit_codes,
)
from ..domain.dashboard import STALE_AFTER_S
from ..domain.deployment import canonical_deployment_name
from ..domain.repository import canonical_repository, repository_equal
from .codex_resolution import (
    CodexResolutionReport,
    Finding,
    codex_resolution_report,
)
from .config import (
    REPOINT_HISTORY_ENV,
    default_state_dir as config_default_state_dir,
    parse_repoint_history,
)
from .identity_pools import IdentityPoolReport, identity_pool_report
from .service_doctor import installed_service_findings

# HARNESS_ROOT: …/src/issue_agent/app/cli.py → repo root. Same walk main.py uses.
# This is what makes the CLI checkout-resident: plugin/ + config/ are siblings.
HARNESS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
REPOS_DIR = os.path.join(HARNESS_ROOT, "config", "repos")
SHARED_ENV = os.path.join(HARNESS_ROOT, "config", "shared.env")
# The dashboard's loopback port. Fixed rather than ephemeral so the operator can
# bookmark it, and so `npm run dev`'s proxy target (dashboard/vite.config.ts)
# matches the running backend without configuration.
DASHBOARD_PORT = 8321


def _active_env_path(name: str) -> str:
    """The composed env snapshot for one deployment. Per-NAME (not the old fixed
    config/.active.env) so concurrent deployments never overwrite each other's
    snapshot or trip each other's mtime hot-reload watch."""
    return os.path.join(HARNESS_ROOT, "config", f".active.{name}.env")
ROLES = ("fix", "review", "steward")


# ── ANSI helpers (match point-at.sh's palette) ────────────────────────────────
def _c(code: str, msg: str) -> str:
    return f"\033[{code}m{msg}\033[0m" if sys.stdout.isatty() else msg


def info(msg: str) -> None:
    print(_c("36", f"▸ {msg}"))


def ok(msg: str) -> None:
    print(_c("32", f"  ✓ {msg}"))


def err(msg: str) -> None:
    print(_c("31", msg), file=sys.stderr)


def warn(msg: str) -> None:
    print(_c("33", f"⚠ {msg}"), file=sys.stderr)


# ── pure helpers (unit-tested; no git/network) ────────────────────────────────
_SSH_RE = re.compile(r"^git@[^:]+:(?P<slug>.+?)(?:\.git)?$")
_HTTPS_RE = re.compile(r"^https?://[^/]+/(?P<slug>.+?)(?:\.git)?/?$")


def parse_remote_url(url: str) -> str:
    """`owner/name` from a git remote URL (ssh or https), or '' if unparseable.

    Handles:  git@github.com:owner/repo.git  →  owner/repo
              https://github.com/owner/repo   →  owner/repo
    """
    url = url.strip()
    for rx in (_SSH_RE, _HTTPS_RE):
        m = rx.match(url)
        if m:
            return canonical_repository(m.group("slug").strip("/"))
    return ""


# M12: fields read from a deployment configuration via
# read_repo_env_field/_source_env_file. Cleared from the sourcing subprocess's
# BASE environment (see _source_env_file) so a same-named var already present
# in the CLI's own process (e.g. an operator's shell that `source`d an overlay
# by hand) can never masquerade as a value the FILE itself set — only what the
# file actually assigns is ever returned. Add a name here before reading it
# elsewhere in this module.
_OVERLAY_FIELDS = (
    "REPO", "TARGET_DIR", "STATE_DIR", "REVIEW_STATE_DIR", "STEWARD_STATE_DIR",
    "TEST_CMD", "BASE_BRANCH", "DAILY_SPEND_CAP_USD", "GLOBAL_DAILY_SPEND_CAP_USD",
    "FIXER_RUNTIME", "REVIEWER_RUNTIME", "STEWARD_RUNTIME", "REVIEW_MODE",
    # Read by the dashboard's operator re-arm: which login's change requests
    # count toward the rejection ceiling, how many of them it takes, and the
    # same App → token identity the review daemon used to author them.
    "REVIEW_BOT", "REVIEW_MAX_REJECTIONS", "REVIEW_BOT_TOKEN",
    "GH_APP_ID", "GH_APP_INSTALL_ID", "GH_APP_KEY_FILE", "GH_APP_TOKEN_CACHE",
    REPOINT_HISTORY_ENV,
)

def _source_env_file(path: str) -> dict[str, str]:
    """Shell-semantics read of every var a single overlay/env file sets (M12).

    The prior `read_repo_env_field` was a regex line-scanner
    (`^FIELD=(.*)$`) that silently mis-parsed a trailing ` # comment`, `KEY =
    VALUE` spacing, or a `${VAR}` reference — so `resolve_name` could MISS an
    existing overlay's REPO/TARGET_DIR match, and `cmd_start` would then
    synthesize a brand-new overlay with fresh `*_STATE_DIR`s, orphaning every
    counter/session/dedup mark for that deployment in one command. This
    mirrors the `set -a; source; env -0` technique `app/main.py`'s
    `_load_agent_env` and `bin/compose-env.sh` already use at launch, so the
    CLI's read of an overlay agrees with what actually runs.

    Returns {} for a missing/unreadable file or a script bash rejects (e.g. a
    stray syntax error) — same "absent, not an error" contract the regex
    reader had.
    """
    env = os.environ.copy()
    for key in _OVERLAY_FIELDS:
        env.pop(key, None)
    try:
        result = source_env_file(
            path,
            env,
            assigned_only=True,
            normalize_spacing=True,
            timeout_seconds=10,
        )
    except ValueError:
        return {}
    return {key: value for key, value in result.items() if key in _OVERLAY_FIELDS}


def read_repo_env_field(path: str, field: str) -> str:
    """Read `FIELD`'s value out of a shell env file the way bash actually
    resolves it (M12) — see `_source_env_file`. "" for missing/unset."""
    return _source_env_file(path).get(field, "")


def _deployment_name_arg(value: str) -> str:
    # argparse runs `type` over a STRING default too, so the "" default of every
    # --name flag reached this and canonicalised to "target" — which is not equal
    # to "", so `issue-agent start` (and status/stop/doctor) with no --name died
    # in the parser before any command ran. "" means "no name given"; only a real
    # answer is validated.
    if not value:
        return ""
    canonical = canonical_deployment_name(value)
    if value != canonical:
        raise argparse.ArgumentTypeError(
            f"deployment name must be canonical; use {canonical!r}"
        )
    return canonical


def derive_name(repo: str) -> str:
    """Fallback overlay name from a repo slug: OWNER-NAME, lowercased, safe chars.

    P0R-2: the prior basename-only form collided across owners — `acme/app`
    and `other/app` both derived `"app"`, so `resolve_name` could reuse ONE
    owner's overlay (REPO/TARGET_DIR and all) while running against the
    OTHER's checkout, with no warning (the M12 residual-warning tier only
    fires on a TARGET_DIR match, which a same-basename-different-owner repo
    won't have either). Including the owner mirrors config.py::_repo_slug's
    `owner/name` -> `owner-name` join (so overlay names and default state-dir
    slugs stay the same shape) but ALSO lowercases + sanitizes to safe
    filename chars, since this becomes a `config/repos/<name>.env` FILENAME,
    not just a directory-component suffix appended to a fixed prefix.

    my-org/example-agent → my-org-example-agent

    Back-compat: this only changes what a NEWLY synthesized name looks like.
    `resolve_name` finds an EXISTING overlay (any name) by reading its actual
    REPO/TARGET_DIR fields, never by recomputing derive_name against it — so
    an overlay from before this change (named after the old bare basename)
    that still matches the detected repo is found and reused exactly as
    before; derive_name is only consulted on the "nothing matched" fallback.
    """
    repo = canonical_repository(repo)
    parts = [p for p in repo.split("/") if p] if repo else []
    return canonical_deployment_name("-".join(parts))


def resolve_name(
    repo: str, explicit: str = "", repos_dir: str = REPOS_DIR, target_dir: str = ""
) -> str:
    """Overlay name for this repo. Priority:
    1. explicit --name.
    2. An overlay whose REPO==repo AND TARGET_DIR==`target_dir` — the cwd-honoring
       match. When several overlays share a REPO (e.g. a primary `myapp.env` and a
       worktree `myapp-wt.env`, both my-org/example-agent), this picks the
       one for the working copy you actually launched from, instead of the
       alphabetically-first REPO match (which silently ran against the wrong dir).
    3. An overlay whose REPO==repo (first alpha), but only when target_dir is
       unknown. A known checkout is part of deployment identity and must never
       silently inherit another checkout's overlay.
    4. Derived from the repo basename.

    M12: if none of the above match by REPO, but some OTHER overlay's
    TARGET_DIR already points at this exact working copy, warn loudly (stderr)
    before falling through to (4) — proceeding silently would synthesize a
    SECOND overlay (fresh `*_STATE_DIR`s) for the same checkout, orphaning the
    first one's counters/sessions/dedup marks. This is the residual case even
    after the M12 parser fix: a genuine repo rename, or an explicit --repo
    that disagrees with what the existing overlay recorded — only the
    operator can resolve it, so this warns rather than silently reusing or
    silently duplicating.
    """
    repo = canonical_repository(repo)
    if explicit:
        canonical = canonical_deployment_name(explicit)
        if explicit != canonical:
            raise ValueError(
                f"deployment name must be canonical; use {canonical!r}"
            )
        return canonical
    if repo and os.path.isdir(repos_dir):
        envs = [fn for fn in sorted(os.listdir(repos_dir)) if fn.endswith(".env")]
        want = os.path.realpath(target_dir) if target_dir else ""
        repo_matches = [
            fn for fn in envs
            if repository_equal(
                read_repo_env_field(os.path.join(repos_dir, fn), "REPO"),
                repo,
            )
        ]
        # Tier 2: prefer the overlay whose TARGET_DIR IS the detected working copy.
        if want:
            for fn in repo_matches:
                td = read_repo_env_field(os.path.join(repos_dir, fn), "TARGET_DIR")
                if td and os.path.realpath(td) == want:
                    return fn[: -len(".env")]
        # Tier 3 is only safe when the caller genuinely has no checkout
        # context. Mutating commands always detect a target and must either
        # find its exact overlay or synthesize/select one explicitly.
        if repo_matches and not want:
            return repo_matches[0][: -len(".env")]
        if repo_matches and want:
            names = ", ".join(fn[: -len(".env")] for fn in repo_matches)
            warn(
                f"existing overlay(s) for {repo!r} ({names}) target a different "
                f"working copy than {target_dir!r}; refusing implicit reuse. "
                "Pass --name for a checkout-specific overlay (and "
                "--write-config only when intentionally repointing it)."
            )
        # M12: about to fall through to derive_name (4) — last chance to catch
        # a same-checkout duplicate that REPO-matching couldn't see.
        if want:
            for fn in envs:
                td = read_repo_env_field(os.path.join(repos_dir, fn), "TARGET_DIR")
                if td and os.path.realpath(td) == want:
                    warn(
                        f"config/repos/{fn} already targets {target_dir} but its "
                        f"REPO does not match {repo!r} — about to synthesize a "
                        f"NEW overlay for the same working copy. If this should "
                        f"reuse it, pass --name {fn[: -len('.env')]}."
                    )
                    break
    return derive_name(repo)


def _overlay_context_mismatch(overlay: str, repo: str, target_dir: str) -> str:
    """'' if `overlay` (a path) agrees with the DETECTED `(repo, target_dir)`
    pair — or the overlay doesn't record enough to compare — else a
    human-readable description naming both sides (P0R-2).

    Repository and checkout are independent parts of deployment identity.
    Matching REPO alone is insufficient: two checkouts of one repository may
    have different branches, state roots, and live daemons.
    """
    repo = (repo or "").strip()
    target_dir = (target_dir or "").strip()
    name = os.path.basename(overlay)
    overlay_repo = read_repo_env_field(overlay, "REPO").strip()
    overlay_target = read_repo_env_field(overlay, "TARGET_DIR").strip()
    if repo and not overlay_repo:
        target_detail = (
            f" (TARGET_DIR={overlay_target!r})" if overlay_target else ""
        )
        return (
            f"config/repos/{name} does not record REPO{target_detail}; "
            f"this checkout/flags detected REPO={repo!r}"
            + (f" at TARGET_DIR={target_dir!r}" if target_dir else "")
        )
    if overlay_repo and repo and not repository_equal(overlay_repo, repo):
        return (
            f"config/repos/{name} is REPO={overlay_repo!r}; this checkout/flags "
            f"detected REPO={repo!r}"
        )
    if target_dir and not overlay_target:
        return (
            f"config/repos/{name} does not record TARGET_DIR; this checkout is "
            f"{target_dir!r}"
        )
    if overlay_target and target_dir and (
        os.path.realpath(overlay_target) != os.path.realpath(target_dir)
    ):
        return (
            f"config/repos/{name} is TARGET_DIR={overlay_target!r}; "
            f"this checkout is {target_dir!r}"
        )
    return ""


def check_overlay_mismatch(
    name: str, repo: str, target_dir: str, repos_dir: str | None = None
) -> str:
    """`_overlay_context_mismatch` for the overlay `name` resolves to in
    `repos_dir` (default: the live `REPOS_DIR`, read at call time so a test's
    `monkeypatch.setattr(cli, "REPOS_DIR", ...)` is honored — unlike a
    parameter default, which would freeze the value this function was
    DEFINED with). '' when the overlay doesn't exist yet: that path is about
    to SYNTHESIZE a fresh overlay from the detected context, not reuse a
    possibly-mismatched one.
    """
    base = repos_dir if repos_dir is not None else REPOS_DIR
    overlay = os.path.join(base, f"{name}.env")
    if not os.path.isfile(overlay):
        return ""
    return _overlay_context_mismatch(overlay, repo, target_dir)


def _q(value: str) -> str:
    """Shell-single-quote a value for the synthesized env file."""
    return "'" + value.replace("'", "'\\''") + "'"


def _encoded_repoint_history(
    history: tuple[tuple[str, str], ...],
) -> str:
    return json.dumps(
        [
            {"repository": repository, "target_dir": target_dir}
            for repository, target_dir in history
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def repoint_history_for_overlay(
    overlay: str,
    *,
    repository: str,
    target_dir: str,
) -> tuple[tuple[str, str], ...]:
    """Preserve exact prior deployments when an overlay is regenerated."""

    existing = _source_env_file(overlay)
    if not existing:
        raise ValueError("existing overlay could not be read safely")
    history = parse_repoint_history(existing.get(REPOINT_HISTORY_ENV, ""))
    prior_repository = canonical_repository(existing.get("REPO", ""))
    prior_target = existing.get("TARGET_DIR", "")
    if not prior_repository or not prior_target:
        raise ValueError(
            "existing overlay must record both REPO and TARGET_DIR"
        )
    if prior_repository != canonical_repository(repository):
        raise ValueError(
            "cross-repository overlay reuse is not a supported repoint; "
            "choose a different --name"
        )
    if not os.path.isabs(prior_target):
        raise ValueError(
            "existing TARGET_DIR must be absolute before a supported repoint"
        )
    history = (
        (prior_repository, os.path.realpath(prior_target)),
        *history,
    )
    current = (
        canonical_repository(repository),
        os.path.realpath(target_dir),
    )
    deduplicated = tuple(
        identity
        for identity in dict.fromkeys(history)
        if identity != current
    )
    # Reuse the config boundary's validation and bounded-history contract.
    return parse_repoint_history(_encoded_repoint_history(deduplicated))


def synth_repo_env(
    *,
    repo: str,
    target_dir: str,
    name: str,
    base_branch: str = "dev",
    base_branch_note: str = "",
    base_branch_suggestion: str = "",
    suggestions: tuple[tuple[str, str], ...] = (),
    test_cmd: str = "",
    lint_cmd: str = "",
    typecheck_cmd: str = "",
    gate_test_failure_exit_codes: str = "",
    edit_check_cmd: str = "",
    review_mode: str = "",
    max_budget: str = "",
    review_budget: str = "",
    repointed_from_deployments: tuple[tuple[str, str], ...] = (),
) -> str:
    """Render a config/repos/<name>.env overlay from flags. Keys mirror exactly
    what app/config.from_env reads, and state dirs are isolated per name (matching
    the hand-authored myapp.env / otherapp.env) so parallel deployments never collide."""
    repo = canonical_repository(repo)
    lines = [
        f"# Per-repo overlay — {repo}. AUTO-GENERATED by `issue-agent start`.",
        "# Overlaid on config/shared.env by bin/point-at.sh. Safe to edit by hand;",
        "# `issue-agent start` will NOT clobber it (pass --write-config to regenerate).",
        "",
        f"REPO={_q(repo)}",
        f"TARGET_DIR={_q(target_dir)}",
    ]
    if base_branch_note:
        lines.append(f"# BASE_BRANCH inferred from {base_branch_note}.")
    if base_branch_suggestion:
        lines.append(
            f"# detected merge target of recent PRs: {base_branch_suggestion} — set "
            "BASE_BRANCH explicitly to adopt it."
        )
    lines += [
        f"BASE_BRANCH={_q(base_branch)}",
        "",
        "# Isolated daemon state (keeps this deployment's state separate).",
        f'STATE_DIR="$HOME/.local/state/issue-agent-{name}"',
        f'REVIEW_STATE_DIR="$HOME/.local/state/review-agent-{name}"',
        f'STEWARD_STATE_DIR="$HOME/.local/state/steward-agent-{name}"',
    ]
    if repointed_from_deployments:
        lines += [
            "",
            "# Exact prior deployments authorized for one-time recovery/adoption.",
            f"{REPOINT_HISTORY_ENV}="
            f"{_q(_encoded_repoint_history(repointed_from_deployments))}",
        ]
    opt = [
        ("TEST_CMD", test_cmd),
        ("LINT_CMD", lint_cmd),
        ("TYPECHECK_CMD", typecheck_cmd),
        ("EDIT_CHECK_CMD", edit_check_cmd),
        ("REVIEW_MODE", review_mode),
        ("MAX_BUDGET_USD", max_budget),
        ("REVIEW_BUDGET_USD", review_budget),
    ]
    if any(v for _, v in opt):
        lines += ["", "# From `issue-agent start` flags."]
        for key, value in opt:
            if value:
                lines.append(f"{key}={_q(value)}")
    if gate_test_failure_exit_codes:
        lines += [
            "",
            "# Exit codes this toolchain uses for an ASSERTION failure (trusted red);"
            " any other non-zero is an infrastructure/build error.",
            f"GATE_TEST_FAILURE_EXIT_CODES={_q(gate_test_failure_exit_codes)}",
        ]
    if suggestions:
        # Detected, NOT applied. These commands block every commit once set, and
        # their author here is a model, not the operator — so they stay inert
        # until a human reads them (ADR 0012: a trusted gate input needs
        # harness-side or operator provenance, and discovery has neither).
        lines += [
            "",
            "# Detected but NOT applied — each of these BLOCKS every commit once",
            "# set. Review, then uncomment to adopt.",
        ]
        for key, value in suggestions:
            lines.append(f"# {key}={_q(value)}")
    if not test_cmd:
        # The tests gate is fail-CLOSED (check_tests): with no TEST_CMD and no
        # opt-out, every pr-create/push would be denied. A repo that genuinely
        # has no tests gets the explicit operator opt-out written into its
        # overlay — loud (the NO TESTS banner already fired) but not wedged.
        lines += [
            "",
            "# No test suite discovered. The PR/push gates fail closed without a",
            "# TEST_CMD; this opt-out keeps them passable. REMOVE once tests exist.",
            "ALLOW_UNTESTED=1",
        ]
    return "\n".join(lines) + "\n"


# ── git/gh detection (impure; thin subprocess wrappers) ───────────────────────
def _run(cmd: list[str], cwd: str | None = None) -> str:
    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def detect_target_dir(cwd: str) -> str:
    """The git working-copy root containing cwd, or ''."""
    return _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)


def detect_repo(cwd: str) -> str:
    """`owner/name` for the repo at cwd. Prefer `gh` (authoritative, handles
    renames/forks); fall back to parsing the origin remote URL."""
    slug = _run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=cwd)
    if slug:
        return canonical_repository(slug)
    return parse_remote_url(_run(["git", "remote", "get-url", "origin"], cwd=cwd))


# ── identity detection + first-line token validation ──────────────────────────
# Everything below mirrors doctor.sh's identity section (bin/doctor.sh) so `init`
# cannot write a config doctor is guaranteed to redden. doctor stays the
# authority: these checks are fail-OPEN, they only move the diagnosis earlier.
# A GitHub login is [A-Za-z0-9-]; an App's bot account additionally carries a
# literal `[bot]` suffix. Accepting that suffix (doctor.sh's charset filter does
# not) is deliberate: rejecting it would fail a legitimate App identity, and it
# still cannot admit gh's JSON error body, which is what the filter exists for.
_LOGIN_CHARSET = re.compile(r"\A[A-Za-z0-9-]+(\[bot\])?\Z")
# Ceiling on every operator re-prompt loop (ADR 0012): a wrong answer must not
# be able to hold the wizard open forever.
_PROMPT_ATTEMPTS = 3


def normalize_login(login: str) -> str:
    """doctor.sh `_normalize_login`: trim, lowercase, drop a trailing `[bot]`, so
    `fix-bot` and `fix-bot[bot]` compare as the one identity they are."""
    value = login.strip().lower()
    suffix = "[bot]"
    return value[: -len(suffix)] if value.endswith(suffix) else value


def _gh_login(env: dict[str, str] | None = None, cwd: str | None = None) -> tuple[str, bool]:
    """(login, gh_answered). gh prints its error BODY to stdout on a bad token, so
    an unguarded capture reports `{ "message":` as a login — charset-validating
    the first line (a GitHub login is [A-Za-z0-9-] only) both sanitises the value
    and detects the failure.

    `gh_answered` is False ONLY when the process could not be executed at all
    (gh absent, or it hung until the timeout). A non-zero exit means gh ran and
    GitHub said no, which is a verdict, not an outage — the distinction decides
    whether an unproven token may be kept."""
    try:
        proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return "", False
    if proc.returncode != 0:
        return "", True
    first = (proc.stdout or "").strip().splitlines()[:1]
    login = first[0].strip() if first else ""
    # `--jq .login` prints a literal `null` when the field is absent, and `null`
    # passes the charset filter — it would be compared as if it were an account.
    if login == "null" or not _LOGIN_CHARSET.match(login):
        return "", True
    return login, True


def detect_fixer_login(cwd: str | None = None) -> str:
    """The ambient `gh` account the fixer will act as, or '' if gh is unusable."""
    return _gh_login(cwd=cwd)[0]


def gh_login_for_token(token: str) -> tuple[str, bool]:
    """(login this token authenticates as, whether GitHub answered at all). The
    token is passed only through the child env and is never printed or logged."""
    if not token:
        return "", True
    env = dict(os.environ, GH_TOKEN=token, GITHUB_TOKEN=token)
    return _gh_login(env=env)


def read_token_input(raw: str) -> tuple[str, str]:
    """Interpret a token answer as either the secret itself or a file holding it:
    `@path`, or a bare path that exists. Returns (token, problem) — a problem is
    an operator-facing message and never contains token material."""
    value = raw.strip()
    from_file = value.startswith("@")
    if from_file:
        value = value[1:].strip()
    path = os.path.expanduser(value) if value else ""
    if not (from_file or (path and os.path.isfile(path))):
        return value, ""
    # The message never repeats what was typed: an operator who pastes a token
    # with a stray leading `@` would otherwise see the secret echoed back.
    if not path or not os.path.isfile(path):
        return "", "no file exists at the path you entered — re-enter the token, or @<path> to a readable file."
    try:
        with open(path) as handle:
            return handle.read().strip(), ""
    except OSError:
        return "", "that token file could not be read (permissions or I/O) — re-enter the token, or @<path> to a readable file."


def prompt_review_bot_token(review_bot: str, attempts: int = _PROMPT_ATTEMPTS) -> str:
    """Ask for REVIEW_BOT_TOKEN and prove it authenticates AS `review_bot` before
    it is written. doctor.sh only caught a mismatched/expired token post-hoc, by
    which point the operator had already been told the config was good.

    A token GitHub ANSWERED about and rejected — expired, or belonging to another
    account — is never stored, at any exit from this loop. Storing one silently
    installs whatever account it belongs to (typically the operator's own PAT) as
    the reviewer identity: three-identity mode is defeated while every check
    downstream still reads green. The only value kept unproven is one GitHub
    could not be asked about at all (gh absent), and it is kept loudly."""
    unproven = ""     # gh could not be run — keepable, doctor re-checks it
    rejected = False  # gh answered and said no — never keepable
    for attempt in range(1, attempts + 1):
        try:
            raw = getpass.getpass(
                "  Reviewer token REVIEW_BOT_TOKEN (paste, or @path to a file; "
                "Enter to skip): "
            ).strip()
        except EOFError:
            raw = ""
        if not raw:
            if rejected or unproven:
                warn(
                    "no REVIEW_BOT_TOKEN stored — `issue-agent doctor` will flag the "
                    "missing reviewer identity."
                )
            return ""
        token, problem = read_token_input(raw)
        if problem:
            warn(problem)
        else:
            login, answered = gh_login_for_token(token)
            if not answered:
                unproven = token
                warn(
                    "could not run `gh` to validate that token — it would be stored "
                    "unverified and `issue-agent doctor` must confirm it."
                )
            elif not login:
                rejected = True
                warn(
                    "GitHub rejected that token — expired, revoked, or lacking "
                    "read:user. It will NOT be stored."
                )
            elif normalize_login(login) != normalize_login(review_bot):
                rejected = True
                warn(
                    f"that token belongs to '{login}', but REVIEW_BOT is "
                    f"'{review_bot or 'unset'}' — GitHub 422s every approval when "
                    "the reviewing account is not the one you configured. It will "
                    "NOT be stored."
                )
            else:
                ok(f"reviewer token authenticates as '{login}'")
                return token
        if attempt < attempts:
            info(f"try again ({attempts - attempt} attempt(s) left).")
    if rejected:
        warn(
            f"no REVIEW_BOT_TOKEN stored after {attempts} attempts: every token "
            "offered was rejected by GitHub or belongs to another account, and "
            "storing one would silently install that account as the reviewer."
        )
        return ""
    if unproven:
        warn(
            f"keeping the last token UNVERIFIED after {attempts} attempts (gh could "
            "not be run) — `issue-agent doctor` re-checks it and is the authority."
        )
    return unproven


def prompt_fix_bot_token(attempts: int = _PROMPT_ATTEMPTS) -> str:
    """Ask for FIX_BOT_TOKEN and report which account it authenticates as.

    Unlike the reviewer's token there is no login to match against — FIX_BOT is
    documented as the ambient `gh auth` user, and a dedicated fixer PAT is
    deliberately a DIFFERENT account from that. So the contract here is weaker
    and stated rather than enforced: a token GitHub answered about and rejected
    is never stored (it would wedge every fixer call), a token gh could not be
    asked about is kept once and loudly, and skipping is allowed with the
    consequence named."""
    for attempt in range(1, attempts + 1):
        try:
            raw = getpass.getpass(
                "  Fixer token FIX_BOT_TOKEN (paste, or @path to a file; "
                "Enter to skip): "
            ).strip()
        except EOFError:
            raw = ""
        if not raw:
            warn(
                "no FIX_BOT_TOKEN stored — the fixer will act as the ambient `gh "
                "auth` account and spend that account's rate-limit pool. "
                "`issue-agent doctor` re-reports this."
            )
            return ""
        token, problem = read_token_input(raw)
        if problem:
            warn(problem)
        else:
            login, answered = gh_login_for_token(token)
            if not answered:
                # Unlike the reviewer's loop there is no login to converge ON, so
                # re-prompting would only ask the operator to paste the same
                # secret again on a box where gh simply is not installed. Keep it
                # once, loudly, and let doctor be the authority.
                warn(
                    "could not run `gh` to validate that token — storing it "
                    "unverified; `issue-agent doctor` must confirm it."
                )
                return token
            if not login:
                warn(
                    "GitHub rejected that token — expired, revoked, or lacking "
                    "read:user. It will NOT be stored."
                )
            else:
                ok(f"fixer token authenticates as '{login}'")
                return token
        if attempt < attempts:
            info(f"try again ({attempts - attempt} attempt(s) left).")
    warn(
        f"no FIX_BOT_TOKEN stored after {attempts} attempts — the fixer will act "
        "as the ambient `gh auth` account. `issue-agent doctor` re-reports this."
    )
    return ""


def _wizard_pool_summary(fields: list[tuple[str, str]]) -> IdentityPoolReport:
    """Replay doctor's identity/pool analysis over what the wizard just
    collected, so the operator learns at CONFIG time — not after an hour of
    exhausted quota — which roles share a credential and what that costs.

    POLL_SECONDS is a per-repo knob the wizard never asks about and shared.env
    never holds, so it is left absent on purpose: the estimate then says out loud
    that it assumed the default."""
    return report_identity_pools({key: value for key, value in fields if value})


def detect_claude_auth() -> bool:
    """Is a Claude credential present? Either the CLI's stored login or an API
    key/token in the environment the daemons inherit."""
    if os.path.isfile(os.path.expanduser("~/.claude/.credentials.json")):
        return True
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


def detect_codex_auth() -> str:
    """The codex auth mode: 'chatgpt', 'apikey', or '' when unknown/unreadable.
    Only the auth MODE is read out of ~/.codex/auth.json — never the token
    material it also holds.

    Presence deliberately does NOT live here: `shutil.which` answers for the
    OPERATOR'S shell, and the 2026-08-02 keifu incident was exactly a case where
    that answer did not describe the daemons. See report_codex_resolution."""
    try:
        with open(os.path.expanduser("~/.codex/auth.json")) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    mode = str(data.get("auth_mode") or "").strip().lower()
    if mode in ("chatgpt", "apikey"):
        return mode
    return "apikey" if data.get("OPENAI_API_KEY") else ""


def report_codex_resolution(codex_bin: str) -> CodexResolutionReport:
    """Print which codex binary would actually run, plus every finding about it.
    Shared by `doctor` and the `init` wizard so both speak from one resolution."""
    report = codex_resolution_report(codex_bin)
    if report.resolved_path:
        line = (f"codex: {report.resolved_path} "
                f"({report.version or 'version unknown'}, "
                f"{'pinned' if report.pinned else 'via PATH'})")
        ok(line) if report.healthy else info(line)
    for finding in report.findings:
        (err if finding.level == "error" else warn)(f"codex: {finding.message}")
    return report


def report_identity_pools(env: dict) -> IdentityPoolReport:
    """Print which credential each role runs on and whose rate-limit pool it
    spends. Shared by `doctor` and the `init` wizard so both speak from one
    resolution. Offline by construction — doctor.sh already probes the tokens it
    can see; what it cannot see is the RESOLUTION, which is config alone."""
    report = identity_pool_report(env)
    for role in report.roles:
        line = f"identity: {role.role} → {role.source}"
        (info if role.ambient else ok)(line)
    for finding in report.findings:
        emit = {"error": err, "warn": warn}.get(finding.level, info)
        emit(f"identity: {finding.message}")
    return report


def remote_branch_exists(repo: str, branch: str, cwd: str | None = None) -> bool:
    """Does `branch` exist on the remote? An inferred base that does not exist
    would send every PR at a missing ref, and point-at.sh would hard-stop on it."""
    if not repo or not branch:
        return False
    return bool(
        _run(["gh", "api", f"repos/{repo}/branches/{branch}", "--jq", ".name"], cwd=cwd)
    )


def detect_base_branch(
    target_dir: str, repo: str = "", sample: int = 20
) -> tuple[str, str]:
    """(branch, provenance). Where the repo's merged PRs actually land beats the
    harness default: a repo without a `dev` branch would otherwise have every PR
    opened against a base that does not exist.

    Candidates are tried in confidence order and each is CONFIRMED to exist on the
    remote before being offered — an inference is only useful if it names a real
    merge target. Bounded: at most one probe per candidate. A provenance of ''
    means nothing was confirmed and the caller holds the harness default; there is
    then nothing to record, because 'inferred from the default' is not an
    inference."""
    raw = _run(
        [
            "gh", "pr", "list", "--state", "merged",
            "--limit", str(sample), "--json", "baseRefName",
        ],
        cwd=target_dir,
    )
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        entries = []
    bases = [
        str(entry.get("baseRefName") or "")
        for entry in entries
        if isinstance(entry, dict)
    ] if isinstance(entries, list) else []
    bases = [base for base in bases if base]
    candidates: list[tuple[str, str]] = []
    if bases:
        branch, hits = Counter(bases).most_common(1)[0]
        candidates.append((branch, f"{hits}/{len(bases)} of the last merged PRs"))
    default = _run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        cwd=target_dir,
    )
    if default:
        candidates.append((default, "the repository default branch"))
    for branch, note in candidates:
        if remote_branch_exists(repo, branch, cwd=target_dir):
            return branch, note
    return "dev", ""


def detect_review_bot(target_dir: str, fixer_login: str, sample: int = 10) -> str:
    """The account that most often reviews merged PRs here and is NOT the fixer —
    a suggestion for REVIEW_BOT, since the fixer cannot approve its own PR (422).
    '' when gh is unreachable, the JSON is malformed, or nobody qualifies."""
    raw = _run(
        [
            "gh", "pr", "list", "--state", "merged",
            "--limit", str(sample), "--json", "reviews",
        ],
        cwd=target_dir,
    )
    try:
        entries = json.loads(raw) if raw else []
    except ValueError:
        return ""
    if not isinstance(entries, list):
        return ""
    logins: list[str] = []
    for entry in entries:
        reviews = entry.get("reviews") if isinstance(entry, dict) else None
        for review in reviews if isinstance(reviews, list) else []:
            author = review.get("author") if isinstance(review, dict) else None
            login = str((author or {}).get("login") or "") if isinstance(author, dict) else ""
            if login and normalize_login(login) != normalize_login(fixer_login):
                logins.append(login)
    return Counter(logins).most_common(1)[0][0] if logins else ""


def gate_failure_exit_codes(test_cmd: str) -> str:
    """Which exit codes count as TRUSTED red-test evidence for this toolchain, or
    '' to keep the harness default of 1. `cargo test` reports assertion failures
    as 101 and reserves 1 for build/usage errors, so the default set would accept
    a compile failure as an honest failing test."""
    try:
        tokens = shlex.split(test_cmd)
    except ValueError:
        tokens = test_cmd.split()
    return "101" if tokens and os.path.basename(tokens[0]) == "cargo" else ""


# ── declared vs. required red-test exit codes (doctor) ───────────────────────
# tauri, 2026-08-08: a TEST_CMD ending in `cargo test` under the default trusted
# set {1} makes every honest assertion failure (101) read as INFRA_ERROR, so the
# red-fail gate can never be satisfied and every seam issue deadlocks. Caught by
# the operator; doctor now says it. `cargo test` reserves 1 for build/usage
# errors and reports assertion failures as 101; GNU make exits 2 when a recipe
# fails (1 only under --question), so a `make test` deployment — this harness's
# own — is in the same trap; the rest use the portable 1.
_RUNNER_FAILURE_EXIT_CODES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("cargo", "test"), 101),
    (("go", "test"), 1),
    (("npm", "test"), 1),
    (("bun", "test"), 1),
    (("bunx", "vitest"), 1),
    (("pytest",), 1),
    (("vitest",), 1),
    (("jest",), 1),
    (("make",), 2),
)


def _test_cmd_tokens(test_cmd: str) -> tuple[str, ...]:
    """TEST_CMD as bare command words. An unbalanced quote still has to be read
    (an operator's half-written command is exactly when this check matters)."""
    try:
        tokens = shlex.split(test_cmd)
    except ValueError:
        tokens = test_cmd.split()
    return tuple(os.path.basename(token) for token in tokens)


def test_cmd_runners(test_cmd: str) -> tuple[tuple[str, int], ...]:
    """Every known runner in TEST_CMD with the exit code it reports an asserted
    failure as, in command order and without duplicates. A sequenced command
    (`npm test && cargo test`) runs several; each one's contract must hold."""
    tokens = _test_cmd_tokens(test_cmd)
    found: list[tuple[str, int]] = []
    for index in range(len(tokens)):
        for words, code in _RUNNER_FAILURE_EXIT_CODES:
            if tuple(tokens[index:index + len(words)]) != words:
                continue
            entry = (" ".join(words), code)
            if entry not in found:
                found.append(entry)
            break
    return tuple(found)


def test_failure_exit_code_findings(
    test_cmd: str, declared_raw: str | None, name: str
) -> tuple[Finding, ...]:
    """Doctor's findings about GATE_TEST_FAILURE_EXIT_CODES vs TEST_CMD.

    Empty when TEST_CMD is unset or every runner's failure code is trusted.
    `declared_raw` is the deployment's raw declaration and None means the key is
    ABSENT: only then does the harness default apply. A key that is PRESENT and
    empty is a declaration the gate parses to the empty set (adapters/worktree.py
    reads it the same way), so it must reach the parser rather than be papered
    over with the default here.

    An empty trusted set is an error, not a warning: nothing at all can count as
    red-test evidence, so every red-fail check deadlocks regardless of which
    runner TEST_CMD drives — reported even when no runner is recognised.
    """
    if not test_cmd.strip():
        return ()
    raw = DEFAULT_TEST_FAILURE_EXIT_CODES if declared_raw is None else declared_raw
    declared = parse_test_failure_exit_codes(raw)
    runners = test_cmd_runners(test_cmd)
    missing = tuple(
        (runner, code) for runner, code in runners if code not in declared
    )
    if declared and not missing:
        return ()
    wanted = sorted(declared | {code for _, code in missing}) or [
        int(DEFAULT_TEST_FAILURE_EXIT_CODES)
    ]
    remedy = (
        f"set GATE_TEST_FAILURE_EXIT_CODES=\"{' '.join(str(c) for c in wanted)}\" "
        f"in config/repos/{name}.env"
    )
    findings: list[Finding] = []
    if not declared:
        findings.append(Finding(
            "error",
            f"GATE_TEST_FAILURE_EXIT_CODES={raw!r} is empty, malformed, or names "
            "a reserved infrastructure code, so the gate fails closed: NO exit "
            "code counts as trusted red-test evidence and every red-fail check "
            f"deadlocks — {remedy}",
        ))
    for runner, code in missing:
        findings.append(Finding(
            "warn",
            f"TEST_CMD runs '{runner}', which reports an asserted failure as "
            f"exit {code}, but GATE_TEST_FAILURE_EXIT_CODES trusts "
            f"{{{', '.join(str(c) for c in sorted(declared)) or 'nothing'}}} — "
            "the red-fail gate reads a genuine test failure as INFRA_ERROR and "
            f"never converges; {remedy}",
        ))
    return tuple(findings)


def _ask(prompt: str, default: str = "") -> str:
    """One operator question. EOF (piped stdin, Ctrl-D) takes the default rather
    than exploding mid-wizard."""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        value = ""
    return value or default


# ── repo-command discovery (cheap LLM probe) ──────────────────────────────────
# Signature files that tell us a checkable toolchain exists AT ALL — test, lint,
# or typecheck. Cheap gate BEFORE spending an LLM call: if none of these are
# anywhere in the tree the probe is skipped entirely (and, with no test command,
# the banner fires). The list covers lint/format/typecheck configs too, because
# the probe now answers for those as well and a repo can carry a linter without
# carrying a test suite. Kept broad, not exhaustive — the LLM does the precise
# read; this only decides "worth asking?".
_TOOLCHAIN_SIGNS = (
    "pytest.ini", "tox.ini", "conftest.py", "pyproject.toml", "setup.cfg",
    "package.json", "vitest.config", "jest.config", "playwright.config",
    "go.mod", "Cargo.toml", "build.gradle", "pom.xml", "Makefile", "justfile",
    "phpunit.xml", "Gemfile", "*_test.go", "*.test.ts", "*.spec.ts",
    "ruff.toml", ".ruff.toml", "mypy.ini", "pyrefly.toml", ".flake8",
    ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
    "eslint.config.js", "eslint.config.mjs", "biome.json", ".golangci.yml",
    "clippy.toml", "rustfmt.toml", ".rustfmt.toml", "tsconfig.json",
    ".pre-commit-config.yaml",
)

_TEST_CMD_MARKER = "TEST_CMD:"
_LINT_CMD_MARKER = "LINT_CMD:"
_TYPECHECK_CMD_MARKER = "TYPECHECK_CMD:"
_TEST_DISCOVERY_PROMPT = """\
You are inspecting a git repository to find the shell command an autonomous \
agent's PR gate will run as the repo's test suite (non-interactive: it must \
EXIT, never watch-mode).

The gate uses this command as a REVERT DETECTOR: it reverts implementation \
files and requires the command to go red. So the command must be able to fail \
for a change in ANY language/tier of source the repo contains — a repo with a \
JS frontend and a Rust/Go/Python backend needs BOTH suites composed, or changes \
on the uncovered side can never be gated (e.g. a Tauri app needs its `vitest`/\
`jest` suite AND `cd src-tauri && cargo test`).

Rules:
- Inspect config files (pyproject.toml, package.json scripts, Makefile, tox.ini, \
go.mod, Cargo.toml, etc.) and the test layout.
- Return a command that runs from the repo root and exits with non-zero if ANY \
composed sub-suite fails.
- Compose multiple suites/tiers with `set -e` (e.g. \
`set -e; (cd backend && uv run pytest) && (cd frontend && pnpm exec vitest run)`).
- INCLUDE every unit/integration suite whose source the repo contains.
- EXCLUDE tiers that are slow or flaky by construction: performance/benchmark \
assertions, load tests, and e2e suites that need a live app/display/server — \
they flake under load and stall the gate. Beware package.json `test` scripts \
that chain a perf tier onto the unit run (e.g. `vitest run && vitest run -c \
vitest.perf.config.ts`); in that case invoke the unit runner directly instead \
of the script.
- For JS/TS prefer the non-watch form (`vitest run`, `jest`, `pnpm test` only if it \
is non-watch). NEVER return a bare `vitest`/`jest --watch`.

Also report the repo's LINT and TYPECHECK commands, if it has them. Those two \
run on every commit the agent makes, so they must be CHECK-MODE: terminating, \
non-mutating, and non-zero on a violation. NEVER a formatter that rewrites files \
(`cargo fmt` alone, `black .`, `prettier --write`) — use the check form \
(`cargo fmt --check`, `ruff format --check`). Sources: CI workflow yaml, \
package.json scripts, Makefile/justfile targets, pyproject/Cargo config. \
Compose a language's lint and format check with `&&` (e.g. \
`cargo fmt --check && cargo clippy -- -D warnings`).

Your ENTIRE reply must be EXACTLY three lines, nothing before or after:
  TEST_CMD: <the command>
  LINT_CMD: <the command>
  TYPECHECK_CMD: <the command>
Use the sentinel when — and only when — the repo has nothing of that kind:
  TEST_CMD: NO_TESTS
  LINT_CMD: NO_LINT
  TYPECHECK_CMD: NO_TYPECHECK
A repo whose typechecking is inseparable from its build or its test run (Rust, \
Go) has no separate typecheck command: reply NO_TYPECHECK rather than duplicating \
the test command.

Do not explain. Do not use code fences. Each line must start with its marker."""


def _has_toolchain_signature(target_dir: str) -> bool:
    """Fast pre-check: does any known test/lint/typecheck signature file exist?
    Uses git ls-files (respects .gitignore, fast) with a plain-walk fallback."""
    listing = _run(["git", "ls-files"], cwd=target_dir)
    names = listing.split("\n") if listing else []
    if not names:
        for root, _dirs, files in os.walk(target_dir):
            if ".git" in root:
                continue
            names.extend(files)
    basenames = {os.path.basename(n) for n in names}
    for sign in _TOOLCHAIN_SIGNS:
        if sign.startswith("*"):
            suf = sign[1:]
            if any(n.endswith(suf) for n in names):
                return True
        elif sign in basenames:
            return True
    return False


@dataclass(frozen=True)
class DiscoveredCommands:
    """What one discovery probe found. Absent commands are ''."""

    test_cmd: str = ""
    lint_cmd: str = ""
    typecheck_cmd: str = ""


def discover_repo_commands(target_dir: str, model: str = "haiku") -> DiscoveredCommands:
    """Find the repo's test/lint/typecheck commands via a cheap read-only LLM
    probe. Empty fields mean the repo genuinely has none (or the probe is
    unavailable — fail-soft: a missing `claude` must not block `start`). Never
    raises.

    Cheap by construction: a signature-file pre-check skips the LLM call entirely
    for repos with no test toolchain, and the call itself is Haiku + read-only
    tools (Read/Glob/Grep) so it can't mutate anything. All three commands come
    from ONE call — lint and typecheck are read off the same repo inspection the
    test command already pays for."""
    if not target_dir or not os.path.isdir(target_dir):
        return DiscoveredCommands()
    if not _has_toolchain_signature(target_dir):
        return DiscoveredCommands()  # no toolchain → don't spend a call
    try:
        proc = subprocess.run(
            [
                "claude", "-p", _TEST_DISCOVERY_PROMPT,
                "--model", model,
                "--allowed-tools", "Read", "Glob", "Grep",
                "--permission-mode", "bypassPermissions",
            ],
            cwd=target_dir, capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return DiscoveredCommands()  # claude absent/timed out — proceed without
    if proc.returncode != 0:
        return DiscoveredCommands()
    return parse_discovery_reply(proc.stdout or "")


def discover_test_cmd(target_dir: str, model: str = "haiku") -> str:
    """The test command alone (`discover_repo_commands().test_cmd`)."""
    return discover_repo_commands(target_dir, model).test_cmd


def _parse_marked_line(text: str, marker: str, sentinel: str) -> str:
    """Extract one `MARKER: <cmd>` line. Robust to a stray prose line or a code
    fence the model adds despite instructions: we scan for the LAST marker line
    (weak models sometimes echo the instruction first) and take everything after
    the marker. The sentinel (or an empty command) → ''."""
    cmd = ""
    for line in text.splitlines():
        line = line.strip().strip("`").strip()
        if line.startswith(marker):
            cmd = line[len(marker):].strip()
    if not cmd or cmd.upper() == sentinel:
        return ""
    return cmd


def parse_discovery_reply(text: str) -> DiscoveredCommands:
    """Read the probe's three marker lines. A reply missing a line simply yields
    '' for it — an old/weak model that answers only TEST_CMD still works."""
    return DiscoveredCommands(
        test_cmd=_parse_marked_line(text, _TEST_CMD_MARKER, "NO_TESTS"),
        lint_cmd=_parse_marked_line(text, _LINT_CMD_MARKER, "NO_LINT"),
        typecheck_cmd=_parse_marked_line(text, _TYPECHECK_CMD_MARKER, "NO_TYPECHECK"),
    )


def _parse_test_cmd_reply(text: str) -> str:
    return _parse_marked_line(text, _TEST_CMD_MARKER, "NO_TESTS")


# ── test-command VERIFICATION (does the discovered command actually work?) ────
# Discovery is an LLM guess, and until now it was written into the overlay
# unverified — the one config value whose wrongness wedges the whole harness.
# The three realistic failures all present identically to the operator (every
# PR denied) but only after a paid session has already run:
#   * watch-mode (`vitest` not `vitest run`) — hangs until GATE_TEST_TIMEOUT_S
#     (600s) and is then reported as a FAILING run;
#   * a binary that is not installed (`pnpm` on a machine without it) — 127;
#   * a command that assumes a different cwd or a missing service.
# Running it ONCE at setup turns the guess into a fact, and costs the same as
# the run the gate will do on the very first push anyway.
_VERIFY_TIMEOUT_S = 180.0


@dataclass(frozen=True)
class TestCmdCheck:
    """Outcome of trial-running a candidate TEST_CMD. `exit_code is None` means
    it never finished — the watch-mode signature."""

    exit_code: int | None
    seconds: float

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def verify_test_cmd(
    cmd: str, target_dir: str, timeout_s: float = _VERIFY_TIMEOUT_S, run=None
) -> TestCmdCheck:
    """Trial-run `cmd` the way the GATE will run it — via
    `governor.run_target_command` (env scrub + #63 re-entrancy bound +
    process-group containment), in the repo root, bounded by a timeout.
    Anything else would verify a command the gate never issues. `run` is
    injectable so tests never execute a real suite. Never raises: a
    verification that blows up must not block `start`. A recursion refusal
    surfaces as exit -1 (failed)."""
    started = time.monotonic()

    def _gate_runner(c: str, cwd: str, t: float) -> int:
        from ..adapters.governor import run_target_command

        rc, _ = run_target_command(c, cwd, t, capture=True)
        if rc is None:
            return -1
        if rc == 124:
            raise subprocess.TimeoutExpired(c, t)
        return rc

    runner = run or _gate_runner
    try:
        code = runner(cmd, target_dir, timeout_s)
    except subprocess.TimeoutExpired:
        return TestCmdCheck(exit_code=None, seconds=time.monotonic() - started)
    except Exception:  # noqa: BLE001 — verification is best-effort, never fatal
        return TestCmdCheck(exit_code=-1, seconds=time.monotonic() - started)
    return TestCmdCheck(exit_code=code, seconds=time.monotonic() - started)


def report_test_cmd_check(cmd: str, check: TestCmdCheck) -> None:
    """Tell the operator what the trial run proved. A FAILING suite is reported
    but NOT treated as a reason to drop the command: the alternative is the
    ALLOW_UNTESTED fallback, which trades a loud problem for a silent one (a
    gate with no teeth). The operator is told exactly what to fix."""
    if check.passed:
        ok(f"verified TEST_CMD ({check.seconds:.0f}s): the suite runs and passes")
        return
    if check.timed_out:
        warn(
            f"TEST_CMD did not finish within {_VERIFY_TIMEOUT_S:.0f}s: {cmd}\n"
            "    If this is WATCH MODE the gate will hang on every push and then "
            "deny it — use the non-watch form (`vitest run`, `jest --ci`).\n"
            "    If your suite is just slow, that is fine: raise "
            "GATE_TEST_TIMEOUT_S above its real runtime."
        )
        return
    warn(
        f"TEST_CMD exited {check.exit_code} on a clean checkout: {cmd}\n"
        "    The tests gate denies every push and `gh pr create` while this "
        "fails, so the fixer would burn a session and then be blocked.\n"
        "    Fix the suite, or correct TEST_CMD in the overlay before starting."
    )


# A DENY-LIST of command shapes that REWRITE the tree, not an allow-list of safe
# ones: anything not named here is still trialed, so this cannot certify a
# command as harmless. It exists because the mutating and check-mode spellings of
# a formatter differ by one flag, a model asked for "the lint command" reaches
# for the mutating one, and a formatter that rewrites the tree EXITS 0 — so the
# trial run cannot catch it and it would be promoted live on a green result.
# Each entry: (label, does this command appear?, flags that make it check-mode,
# flags that make it mutating).
_MUTATING_CHECK_SHAPES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("cargo fmt", r"\bcargo\s+fmt\b", (r"--check",), ()),
    ("ruff format", r"\bruff\s+format\b", (r"--check", r"--diff"), ()),
    ("black", r"\bblack\b", (r"--check", r"--diff"), ()),
    ("isort", r"\bisort\b", (r"--check", r"--check-only", r"--diff"), ()),
    ("dotnet format", r"\bdotnet\s+format\b", (r"--verify-no-changes",), ()),
    ("prettier", r"\bprettier\b", (), (r"--write", r"(?<![\w-])-w(?![\w-])")),
    ("gofmt", r"\bgofmt\b", (), (r"(?<![\w-])-w(?![\w-])",)),
    ("goimports", r"\bgoimports\b", (), (r"(?<![\w-])-w(?![\w-])",)),
)


def mutating_check_command(cmd: str) -> str:
    """Why `cmd` must not be run or promoted as a check command, or '' if no known
    mutating shape matches. A gate command has to be a QUESTION about the tree; one
    that rewrites it would edit the operator's checkout on every commit the gate
    fires — and would pass its own trial run while doing so."""
    for label, trigger, check_flags, write_flags in _MUTATING_CHECK_SHAPES:
        if not re.search(trigger, cmd):
            continue
        if check_flags and not any(re.search(flag, cmd) for flag in check_flags):
            return f"`{label}` REWRITES files without {' or '.join(check_flags)}"
        if any(re.search(flag, cmd) for flag in write_flags):
            return f"`{label}` was given a write flag, so it REWRITES files"
    return ""


def _disposable_worktree(target_dir: str) -> str:
    """A throwaway detached worktree of the target's HEAD, or '' if one cannot be
    made. Callers must pass it to `_discard_worktree`."""
    try:
        root = tempfile.mkdtemp(prefix="issue-agent-trial-")
    except OSError:
        return ""
    path = os.path.join(root, "wt")
    try:
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", path, "HEAD"],
            cwd=target_dir, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(root, ignore_errors=True)
        return ""
    if result.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        return ""
    return path


def _discard_worktree(target_dir: str, path: str) -> None:
    """Remove a disposable worktree and its temp root. Best-effort: a leftover
    directory must not fail `start`."""
    for argv in (["worktree", "remove", "--force", path], ["worktree", "prune"]):
        try:
            subprocess.run(
                ["git", *argv], cwd=target_dir, capture_output=True, text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def verify_check_cmd(
    cmd: str, target_dir: str, timeout_s: float = _VERIFY_TIMEOUT_S, run=None
) -> TestCmdCheck | None:
    """Trial-run a DISCOVERED check command in a disposable worktree of the
    target's HEAD, never in the operator's working copy. None means it was not
    run at all (no worktree could be made) — which is a refusal, not a pass:
    the command is model-authored, and executing it against a tree a human may be
    mid-edit on is not a risk this step is allowed to take to answer a question.

    TEST_CMD keeps verifying in the real checkout: it is the command the gate
    itself will run there, so a trial elsewhere would verify something else."""
    worktree = _disposable_worktree(target_dir)
    if not worktree:
        return None
    try:
        return verify_test_cmd(cmd, worktree, timeout_s, run=run)
    finally:
        _discard_worktree(target_dir, worktree)


def resolve_check_command(
    key: str,
    flag_value: str,
    discovered: str,
    target_dir: str,
    *,
    interactive: bool,
    verify: bool = True,
) -> tuple[str, str]:
    """Decide what LINT_CMD/TYPECHECK_CMD the overlay may carry LIVE, and what it
    may only suggest. Returns (live, suggestion) — at most one is non-empty.

    These are trusted gate inputs: once set, each one BLOCKS every commit the
    fixer makes. A discovered value's author is a model, so it carries no
    operator or harness provenance (ADR 0012) and cannot be promoted on its own.
    It becomes live only after the operator confirms it AND it survives the same
    bounded trial run TEST_CMD gets; where that confirmation cannot happen
    (non-TTY) it is written as an inert comment instead. An explicit flag is
    already an operator decision and is written live."""
    if flag_value:
        return flag_value, ""
    if not discovered:
        return "", ""
    mutating = mutating_check_command(discovered)
    if mutating:
        warn(
            f"discovered {key} is not check-mode: {mutating}.\n"
            f"    A gate command must ASK about the tree, not rewrite it — and a "
            "formatter that rewrites it still exits 0, so no trial run would "
            "catch this.\n"
            f"    Recorded as a commented suggestion only: {discovered}"
        )
        return "", discovered
    if not interactive:
        info(
            f"detected a candidate {key} but there is no terminal to confirm it — "
            "recording it as a commented suggestion only."
        )
        return "", discovered
    answer = _ask(f"{key} (Enter to accept, type a replacement, or '-' to skip)", discovered)
    if answer == "-":
        info(f"{key} skipped — the commit gate will not run it.")
        return "", discovered
    if not verify:
        return answer, ""
    info(f"trial-running {key} in a throwaway worktree (up to {_VERIFY_TIMEOUT_S:.0f}s)…")
    check = verify_check_cmd(answer, target_dir)
    if check is None:
        warn(
            f"could not create a disposable worktree to trial {key} safely, so it "
            "was NOT run.\n"
            "    Recorded as a commented suggestion instead — running a "
            "model-authored command in your working copy is not an acceptable way "
            "to answer this."
        )
        return "", answer
    if check.passed:
        ok(f"verified {key} ({check.seconds:.0f}s)")
        return answer, ""
    outcome = (
        f"did not finish within {_VERIFY_TIMEOUT_S:.0f}s"
        if check.timed_out else f"exited {check.exit_code}"
    )
    warn(
        f"{key} {outcome} on a clean checkout: {answer}\n"
        "    Setting it would block EVERY commit the fixer makes, so it is "
        "recorded as a commented suggestion instead.\n"
        f"    Fix it (or correct the command), then set {key} in the overlay."
    )
    return "", answer


def _no_tests_banner() -> None:
    """Loud, impossible-to-miss warning that the target repo has no test suite —
    the PR gate, the red-must-fail gate, and the reviewer's test run all lose
    their teeth without one."""
    W = 64
    bar = "!" * W

    def row(text: str, code: str) -> None:
        body = f"!!  {text}".ljust(W - 2) + "!!"
        print(_c(code, body))

    print(_c("31", bar))
    row("THIS REPO HAS NO TESTS", "31")
    row("The PR gate, red-must-fail gate, and reviewer test run", "33")
    row("all have NO teeth — merged code is unverified by tests.", "33")
    row("Pass --test-cmd '<cmd>' if discovery missed your suite.", "33")
    print(_c("31", bar))


# ── harness worktree (isolate from the primary checkout) ─────────────────────
def harness_worktree_path(target_dir: str, explicit: str = "") -> str:
    """Where the isolated harness worktree lives. Explicit path wins; else a
    sibling of the primary checkout named `<basename>-harness` (outside the repo,
    so it's never swept into a diff)."""
    if explicit:
        return os.path.abspath(explicit)
    tgt = os.path.abspath(target_dir)
    return os.path.join(os.path.dirname(tgt), f"{os.path.basename(tgt)}-harness")


def _is_worktree_of(primary: str, candidate: str) -> bool:
    """True if `candidate` is already a registered worktree of `primary`'s repo."""
    listing = _run(["git", "worktree", "list", "--porcelain"], cwd=primary)
    want = os.path.realpath(candidate)
    for line in listing.splitlines():
        if line.startswith("worktree "):
            if os.path.realpath(line[len("worktree "):].strip()) == want:
                return True
    return False


def ensure_harness_worktree(target_dir: str, base_branch: str, explicit: str = "") -> str:
    """Create (or reuse) a git worktree of the target repo, DETACHED at the base
    branch tip, so the harness runs without touching the primary checkout (which a
    human or another agent may be mid-edit on). Detached HEAD sidesteps the
    "branch already checked out elsewhere" conflict entirely. Returns the worktree
    path, or '' on failure. Idempotent — reuses an existing worktree at the path.

    The fixer's own per-issue worktrees nest under this one (verified: nested
    `git worktree add` from a linked worktree works)."""
    wt = harness_worktree_path(target_dir, explicit)
    if os.path.isdir(wt):
        if _is_worktree_of(target_dir, wt):
            return wt
        if os.listdir(wt):
            err(f"{wt} exists and is not a worktree of this repo — pass --worktree-path.")
            return ""
    # Pick a ref to detach at: prefer origin/<base>; else the repo default; else HEAD.
    _run(["git", "fetch", "origin", base_branch], cwd=target_dir)
    ref = f"origin/{base_branch}"
    if _run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=target_dir) == "":
        default = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=target_dir)
        ref = default or "HEAD"
    r = subprocess.run(
        ["git", "worktree", "add", "--detach", wt, ref],
        cwd=target_dir, capture_output=True, text=True,
    )
    if r.returncode != 0:
        err(f"git worktree add failed: {(r.stderr or '').strip()}")
        return ""
    return wt


# ── process detection for stop/status (reuse point-at.sh's pgrep pattern) ─────
def _pgrep(pattern: str) -> list[int]:
    out = _run(["pgrep", "-f", pattern])
    return [int(p) for p in out.split() if p.isdigit()]


def _pid_argv(pid: int) -> tuple[str, ...]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return tuple(
                item.decode(errors="replace")
                for item in handle.read().split(b"\0")
                if item
            )
    except OSError:
        if sys.platform != "darwin":
            return ()
        completed = subprocess.run(
            ("ps", "-p", str(pid), "-o", "command="),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            return ()
        try:
            return tuple(shlex.split(completed.stdout))
        except ValueError:
            return ()


def _launcher_pids(name: str) -> list[int]:
    result: list[int] = []
    for pid in _pgrep("point-at.sh"):
        argv = _pid_argv(pid)
        direct = (
            len(argv) >= 2
            and os.path.basename(argv[0]) == "point-at.sh"
            and argv[1] == name
        )
        interpreted = (
            len(argv) >= 3
            and os.path.basename(argv[0]) in {"bash", "sh"}
            and os.path.basename(argv[1]) == "point-at.sh"
            and argv[2] == name
        )
        if direct or interpreted:
            result.append(pid)
    return result


def _pid_deployment_name(pid: int) -> str:
    """Deployment identity exported by foreground and installed daemons."""

    variables: dict[str, str] = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            items = handle.read().split(b"\0")
        variables = {
            key.decode(errors="replace"): value.decode(errors="replace")
            for item in items
            if b"=" in item
            for key, value in (item.split(b"=", 1),)
        }
    except OSError:
        if sys.platform != "darwin":
            return ""
        completed = subprocess.run(
            ("ps", "eww", "-p", str(pid), "-o", "command="),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0:
            return ""
        for variable in ("ISSUE_AGENT_DEPLOYMENT_ID", "AGENT_ENV"):
            match = re.search(
                rf"(?:^|\s){variable}=([^\s]+)(?:\s|$)",
                completed.stdout,
            )
            if match:
                variables[variable] = match.group(1)
    explicit = variables.get("ISSUE_AGENT_DEPLOYMENT_ID", "")
    if explicit:
        return explicit
    active = os.path.basename(variables.get("AGENT_ENV", ""))
    if active.startswith(".active.") and active.endswith(".env"):
        return active[len(".active.") : -len(".env")]
    return ""


def _role_pids(name: str) -> dict[str, list[int]]:
    """Map each role to PIDs owned by exactly one deployment."""

    return {
        role: [
            pid
            for pid in sorted(
                set(_pgrep(f"issue_agent.app.main {role}"))
                | set(_pgrep(f"issue_agent.app.launchd_main {role}"))
            )
            if _pid_deployment_name(pid) == name
        ]
        for role in ROLES
    }


def _fresh_role_heartbeats(
    name: str,
    *,
    now_fn=None,
    stale_after_s: float = STALE_AFTER_S,
) -> dict[str, datetime]:
    """Fresh scheduler heartbeats for roles hidden from this PID namespace.

    Installed services and sandboxed operator shells may occupy different PID
    namespaces, so an empty ``pgrep`` result is not evidence that a daemon is
    stopped. The heartbeat is daemon-authored, moves once per scheduler tick,
    and records terminal states; bounding its age keeps a dead daemon from
    looking alive forever. This is the same liveness authority and ceiling the
    dashboard uses.
    """
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    result: dict[str, datetime] = {}
    for role, journal in zip(ROLES, journal_paths(name)):
        stamp = read_heartbeat_time(
            os.path.join(os.path.dirname(journal), "heartbeat.json")
        )
        age_s = (now - stamp).total_seconds() if stamp is not None else -1.0
        if stamp is not None and 0 <= age_s <= stale_after_s:
            result[role] = stamp
    return result


def _same_repository_deployments(name: str, repository: str) -> tuple[str, ...]:
    """Every deployment on this machine configured for `repository`."""
    candidates = {name}
    for directory, prefix in ((REPOS_DIR, ""), (os.path.join(HARNESS_ROOT, "config"), ".active.")):
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        candidates |= {
            entry[len(prefix):-len(".env")] for entry in entries
            if entry.startswith(prefix) and entry.endswith(".env") and len(entry) > len(prefix) + len(".env")
        }
    matches = []
    for candidate in sorted(candidates):
        try:
            configured = canonical_repository(_deployment_env_field(candidate, "REPO"))
        except ValueError:
            configured = ""
        if candidate == name or configured == repository:
            matches.append(candidate)
    return tuple(matches)


def _activation_liveness(name: str, repository: str) -> list[str]:
    """Fail-closed old-writer evidence for a family activation: processes
    visible here, plus every same-repository deployment's heartbeats, where
    an unreadable heartbeat counts as running."""
    now = datetime.now(timezone.utc)
    reasons: list[str] = []
    for deployment in _same_repository_deployments(name, repository):
        pids = sorted({*_launcher_pids(deployment), *(p for group in _role_pids(deployment).values() for p in group)})
        if pids:
            reasons.append(f"{deployment}: running pids {' '.join(map(str, pids))}")
        for journal in journal_paths(deployment):
            reason = heartbeat_liveness(os.path.join(os.path.dirname(journal), "heartbeat.json"),
                                        now=now, stale_after_s=STALE_AFTER_S)
            if reason:
                reasons.append(f"{deployment}: {reason}")
    return reasons


# ── subcommand implementations ────────────────────────────────────────────────
# NixOS has no /bin/bash: resolve the interpreter from PATH once.
_BASH = shutil.which("bash") or "/bin/bash"


def _bin(script: str) -> str:
    return os.path.join(HARNESS_ROOT, "bin", script)


def _resolve_context(args) -> tuple[str, str, str]:
    """(repo, target_dir, name) from flags then cwd auto-detection."""
    cwd = os.getcwd()
    target_dir = args.target_dir or detect_target_dir(cwd)
    if target_dir:
        target_dir = os.path.realpath(target_dir)
    # An explicit target is the repository authority. Operators commonly run
    # status/install commands from the harness checkout; detecting REPO from
    # that cwd reports a false overlay mismatch and can select the wrong name.
    repo = canonical_repository(args.repo or detect_repo(target_dir or cwd))
    # Pass target_dir so a repo with several overlays resolves to the one for THIS
    # working copy (cwd wins), not the alphabetically-first REPO match.
    name = resolve_name(repo, getattr(args, "name", "") or "", target_dir=target_dir)
    return repo, target_dir, name


def _stop_deployment(name: str) -> int:
    """Stop and verify every foreground or service-owned process for a name."""

    launchers = _launcher_pids(name)
    roles = _role_pids(name)
    pids = sorted(
        set(launchers)
        | {pid for group in roles.values() for pid in group}
    )
    if not pids:
        info(f"no watchers running for '{name}'.")
        return 0
    info(f"stopping watchers for '{name}' (pids: {' '.join(map(str, pids))})")
    for pid in pids:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 90
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
            except PermissionError:
                pass
        if remaining:
            time.sleep(0.1)
    if remaining:
        err(
            "watchers did not stop within 90s "
            f"(still alive: {' '.join(map(str, sorted(remaining)))})"
        )
        return 1
    ok("watchers stopped and verified")
    return 0


# #156 follow-up (live incident 2026-08-08): doctor's stale/partial-install
# check aborted a launch that one `make install` fixes, and the operator ran
# exactly the command the message named and relaunched. `start` now performs
# that remediation itself — once, never in a loop — and leaves the preflight to
# report the drift verbatim if it does not take. Bare `issue-agent doctor` stays
# report-only: it diagnoses, it does not mutate the install.
_PRIMARY_ENTRYPOINT = "issue-agent"
# `make install` runs `uv sync`, which PRUNES packages from the shared .venv the
# daemons of every deployment on this box are executing out of. Bounded so a
# wedged resolver cannot hold a launch open forever.
_INSTALL_TIMEOUT_S = 300.0


def declared_console_scripts() -> tuple[str, ...]:
    """The console scripts `make install` materialises, read from the ONE
    declaration bin/install.sh also reads (pyproject's `[project.scripts]`), so
    adding an entry point stays a single edit. Falls back to this distribution's
    installed metadata when the checkout's pyproject cannot be parsed (Python
    3.10 has no tomllib), and to nothing when neither can answer — an unknown
    contract must not be reported as drift."""
    path = os.path.join(HARNESS_ROOT, "pyproject.toml")
    try:
        import tomllib

        with open(path, "rb") as stream:
            table = (tomllib.load(stream).get("project") or {}).get("scripts")
    except (ImportError, OSError, ValueError):
        table = None
    names = tuple(str(k) for k in (table or {}) if str(k).strip())
    return names or _installed_console_scripts()


def _installed_console_scripts() -> tuple[str, ...]:
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        entries = distribution("issue-agent").entry_points
    except PackageNotFoundError:
        return ()
    return tuple(e.name for e in entries if e.group == "console_scripts")


_TRANSIENT_CHECKOUT_MARKER = f"{os.sep}.claude{os.sep}worktrees{os.sep}"


def running_from_transient_checkout() -> bool:
    """Whether this process's code lives in a `.claude/worktrees/` transient."""
    return _TRANSIENT_CHECKOUT_MARKER in HARNESS_ROOT + os.sep


def _resolves_into_this_checkout(script: str) -> bool:
    """Whether `script` on PATH actually runs THIS checkout's venv entry point.

    Presence alone reads healthy while another checkout's shim shadows ours —
    the daemon then execs a different tree's plugin/ and config/. The generated
    shim (bin/install.sh) bakes the absolute venv path in, so either PATH
    resolves straight to that binary or the shim names it."""
    found = shutil.which(script)
    if not found:
        return False
    entry = os.path.join(HARNESS_ROOT, ".venv", "bin", script)
    if os.path.realpath(found) == os.path.realpath(entry):
        return True
    try:
        with open(found, encoding="utf-8", errors="replace") as stream:
            return entry in stream.read(8192)
    except OSError:
        return False


def entrypoint_drift() -> str:
    """Why this box's install is stale/partial, '' when it is coherent.

    doctor.sh's signature generalised: gated on `issue-agent` resolving, which
    (and only which) means this box runs the INSTALLED flavour — a dev checkout
    that never ran `make install` has none of the scripts and is not drift."""
    scripts = declared_console_scripts()
    if _PRIMARY_ENTRYPOINT not in scripts or not shutil.which(_PRIMARY_ENTRYPOINT):
        return ""
    # #187: a `make install` run inside an agent worktree bakes that transient
    # tree's venv into the launcher. From INSIDE that stale process the checks
    # below read coherent — the launcher resolves into "this" checkout, which
    # is exactly the wrong one — so the transient path itself is the signature.
    if running_from_transient_checkout():
        return (
            f"the installed {_PRIMARY_ENTRYPOINT} runs {HARNESS_ROOT} — a "
            "transient agent worktree (`make install` ran inside it); run "
            "`make install` from the canonical checkout"
        )
    for script in scripts:
        if not shutil.which(script):
            return (
                f"{script} is not on PATH though {_PRIMARY_ENTRYPOINT} is "
                "— stale/partial install"
            )
        if not _resolves_into_this_checkout(script):
            return (
                f"{script} on PATH resolves to {shutil.which(script)}, which is "
                f"not this checkout's .venv/bin/{script} — a stale shim from "
                "another checkout shadows this deployment"
            )
    return ""


def live_deployment_names() -> tuple[str, ...]:
    """Deployments with a running launcher, role process, or fresh heartbeat.

    `make install` runs `uv sync` against the ONE .venv every deployment's
    daemons execute from, and a sync prunes packages mid-tick. Same liveness
    authority `status`/`stop` use, heartbeats included so daemons in another PID
    namespace (installed services) still count."""
    try:
        entries = sorted(os.listdir(REPOS_DIR))
    except OSError:
        return ()
    live: list[str] = []
    for entry in entries:
        if not entry.endswith(".env"):
            continue
        other = entry[: -len(".env")]
        if (
            _launcher_pids(other)
            or any(_role_pids(other).values())
            or _fresh_role_heartbeats(other)
        ):
            live.append(other)
    return tuple(live)


def heal_entrypoint_drift() -> bool:
    """Re-run the installer once when the entry points have drifted. True when
    there was nothing to fix or the fix worked; False leaves the drift for the
    preflight to report as today."""
    drift = entrypoint_drift()
    if not drift:
        return True
    if running_from_transient_checkout():
        # `make install` here would reinstall the transient tree — the exact
        # state being healed. Only a human in the canonical checkout can fix it.
        err(f"stale install detected ({drift}); NOT auto-repairing — `make "
            "install` from this process would reinstall the transient worktree "
            "itself. Run `make install` from the canonical checkout.")
        return False
    live = live_deployment_names()
    if live:
        err(f"stale/partial install detected ({drift}), but NOT auto-repairing: "
            f"deployment(s) {', '.join(live)} are live and `make install` runs "
            "`uv sync` against the .venv their daemons execute from. Stop them "
            "(`issue-agent stop --name <n>`) and re-run `make install`.")
        return False
    info(f"stale/partial install detected ({drift}) — running `make install` "
         "once before launch")
    try:
        result = subprocess.run(
            ["make", "install"],
            cwd=HARNESS_ROOT,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as expired:
        err(f"auto-repair failed: `make install` exceeded {_INSTALL_TIMEOUT_S:.0f}s")
        _echo_tail(_decoded(expired.stderr) or _decoded(expired.stdout))
        return False
    if result.returncode != 0:
        err(f"auto-repair failed: `make install` exited {result.returncode}")
        _echo_tail(result.stderr or result.stdout or "")
        return False
    if entrypoint_drift():
        err("auto-repair failed: `make install` succeeded but the install is "
            f"still drifted ({entrypoint_drift()})")
        return False
    ok(f"auto-fixed stale/partial install: re-ran `make install` ({drift})")
    return True


def _decoded(stream: str | bytes | None) -> str:
    if isinstance(stream, bytes):
        return stream.decode(errors="replace")
    return stream or ""


def _echo_tail(output: str, lines: int = 5) -> None:
    for line in output.strip().splitlines()[-lines:]:
        err(f"  {line}")


def cmd_start(args) -> int:
    repo, target_dir, name = _resolve_context(args)
    if not repo:
        err("could not detect the repo (not in a git repo, or no gh/origin remote).")
        err("Pass --repo owner/name (and --target-dir) explicitly.")
        return 2
    if not target_dir:
        err(f"could not detect a git working copy for {repo}. Pass --target-dir.")
        return 2

    # --worktree: run against an isolated git worktree instead of the detected
    # checkout, so a human/another agent mid-edit on the primary is untouched. Get
    # its own overlay name + single-instance lock (…-wt) so it never clobbers a
    # hand-authored overlay (e.g. myapp.env) or collides with a primary-checkout run.
    if getattr(args, "worktree", False) or getattr(args, "worktree_path", ""):
        info("--worktree: provisioning an isolated worktree (primary checkout untouched)")
        # BASE_BRANCH may still be unresolved here (inference happens with the
        # overlay); the detach ref falls back to the repo default anyway.
        wt = ensure_harness_worktree(
            target_dir,
            getattr(args, "base_branch", None) or "dev",
            getattr(args, "worktree_path", "") or "",
        )
        if not wt:
            return 2
        target_dir = wt
        if not (getattr(args, "name", "") or ""):
            name = f"{name}-wt"
        ok(f"worktree ready: {wt}  (overlay '{name}')")

    # First run: no global secrets yet → init interactively (unless --no-init or
    # not a TTY, in which case say how). So the whole first-time flow is just
    # `issue-agent start`.
    if not os.path.isfile(SHARED_ENV):
        if args.no_init or not sys.stdin.isatty():
            err("config/shared.env is missing (global identity/secrets).")
            err("Run `issue-agent init` once to create it, then retry.")
            return 2
        info("first run — no config/shared.env yet; running `init`.")
        # Suggest the account that already reviews merged PRs here: the fixer
        # cannot approve its own PR (422), so REVIEW_BOT must be someone else and
        # the operator rarely knows which of their accounts the harness expects.
        rc = cmd_init(
            argparse.Namespace(
                force=False,
                review_bot_default=detect_review_bot(target_dir, detect_fixer_login(target_dir)),
            )
        )
        if rc != 0 or not os.path.isfile(SHARED_ENV):
            return rc or 2
        print()

    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    # P0R-2: about to REUSE (not `--write-config` regenerate) an overlay found
    # under `name` — refuse if it was written for a different repo/checkout.
    # Silently proceeding would label issues, open PRs, and merge in the
    # WRONG place; silently synthesizing a second overlay would orphan
    # whichever deployment is real. Only the operator can tell which, so this
    # is fatal, not a warning.
    if os.path.isfile(overlay) and not args.write_config:
        mismatch = check_overlay_mismatch(name, repo, target_dir)
        if mismatch:
            err(f"REFUSING to reuse config/repos/{name}.env — wrong repo/checkout:")
            err(f"  {mismatch}")
            err(
                "Reusing it would run fix/review/steward against a DIFFERENT repo "
                "and checkout: labeling issues, opening PRs, and merging there. Pass "
                f"--name <other> to pick the overlay that matches {repo!r} at "
                f"{target_dir!r}, or --write-config to (re)synthesize "
                f"config/repos/{name}.env for THIS checkout."
            )
            return 2
    replacing_overlay = os.path.isfile(overlay)
    if not replacing_overlay or args.write_config:
        os.makedirs(REPOS_DIR, exist_ok=True)
        repoint_history: tuple[tuple[str, str], ...] = ()
        if replacing_overlay:
            try:
                repoint_history = repoint_history_for_overlay(
                    overlay,
                    repository=repo,
                    target_dir=target_dir,
                )
            except ValueError as exc:
                err(
                    "REFUSING to overwrite deployment lineage in "
                    f"config/repos/{name}.env: {exc}"
                )
                return 2
            if repoint_history:
                info(
                    "recording explicit repoint lineage for "
                    f"{len(repoint_history)} prior deployment(s)"
                )
        # Resolve TEST_CMD: explicit flag wins; else probe the repo with a cheap
        # read-only LLM search. Empty result → the repo has no tests: proceed, but
        # LOUDLY. Only done when synthesizing a fresh overlay (not on reuse).
        interactive = sys.stdin.isatty()
        test_cmd = args.test_cmd or ""
        lint_flag = getattr(args, "lint_cmd", "") or ""
        typecheck_flag = getattr(args, "typecheck_cmd", "") or ""
        discovered = DiscoveredCommands()
        if not (test_cmd and lint_flag and typecheck_flag):
            wanted = [
                label for label, supplied in (
                    ("test", test_cmd), ("lint", lint_flag), ("typecheck", typecheck_flag)
                ) if not supplied
            ]
            info(f"searching the repo for its {'/'.join(wanted)} command(s)…")
            discovered = discover_repo_commands(target_dir)
            if not test_cmd:
                test_cmd = discovered.test_cmd
                if test_cmd:
                    ok(f"discovered TEST_CMD: {test_cmd}")
                else:
                    _no_tests_banner()
        suggestions: list[tuple[str, str]] = []
        # LINT_CMD/TYPECHECK_CMD are commit-blocking gate inputs, so a DISCOVERED
        # one is confirmed and trial-run before it can go live; anything that
        # fails either step is written as an inert comment (see
        # resolve_check_command).
        verify_checks = not getattr(args, "no_verify_test_cmd", False)
        lint_cmd, lint_suggestion = resolve_check_command(
            "LINT_CMD", lint_flag, discovered.lint_cmd, target_dir,
            interactive=interactive, verify=verify_checks,
        )
        typecheck_cmd, typecheck_suggestion = resolve_check_command(
            "TYPECHECK_CMD", typecheck_flag, discovered.typecheck_cmd, target_dir,
            interactive=interactive, verify=verify_checks,
        )
        if lint_suggestion:
            suggestions.append(("LINT_CMD", lint_suggestion))
        if typecheck_suggestion:
            suggestions.append(("TYPECHECK_CMD", typecheck_suggestion))
        # BASE_BRANCH: an explicit --base-branch wins; else a previously written
        # value is preserved (a regenerate must not silently re-point a
        # deployment's merge target); else infer from where this repo's merged PRs
        # actually land.
        base_branch = args.base_branch or ""
        base_note = ""
        base_suggestion = ""
        if not base_branch and replacing_overlay:
            base_branch = read_repo_env_field(overlay, "BASE_BRANCH")
            if base_branch:
                info(
                    f"keeping BASE_BRANCH={base_branch} from the existing overlay "
                    "(pass --base-branch to change it)"
                )
        if not base_branch:
            inferred, base_note = detect_base_branch(target_dir, repo)
            if interactive:
                base_branch = _ask(
                    "Base branch for PRs (the harness auto-merges approved PRs "
                    "into this branch)",
                    inferred,
                )
                if base_branch != inferred:
                    base_note = "an operator answer at `issue-agent start`"
            else:
                # Adopting an inferred merge target unattended would walk straight
                # past point-at.sh's base-branch hard stop — the interlock that
                # makes choosing where this harness MERGES a conscious operator
                # act. Keep the default, record the finding, let the stop fire.
                detected_note = base_note
                base_branch = "dev"
                base_suggestion = inferred if inferred != "dev" else ""
                base_note = ""
                if base_suggestion:
                    info(
                        f"recent PRs merge into '{base_suggestion}' ({detected_note})"
                        " — keeping BASE_BRANCH=dev; pass --base-branch to adopt it."
                    )
        # Verify whatever we ended up with — discovered OR hand-supplied. A typo
        # in --test-cmd wedges the gate exactly as badly as a bad guess, and this
        # is the last moment before the value is written into the overlay and
        # starts denying pushes.
        if test_cmd and not getattr(args, "no_verify_test_cmd", False):
            info(f"trial-running the test command (up to {_VERIFY_TIMEOUT_S:.0f}s)…")
            report_test_cmd_check(test_cmd, verify_test_cmd(test_cmd, target_dir))
        content = synth_repo_env(
            repo=repo, target_dir=target_dir, name=name,
            base_branch=base_branch, base_branch_note=base_note,
            base_branch_suggestion=base_suggestion, suggestions=tuple(suggestions),
            test_cmd=test_cmd, lint_cmd=lint_cmd, typecheck_cmd=typecheck_cmd,
            gate_test_failure_exit_codes=gate_failure_exit_codes(test_cmd),
            edit_check_cmd=args.edit_check_cmd or "", review_mode=args.review_mode or "",
            max_budget=args.max_budget or "", review_budget=args.review_budget or "",
            repointed_from_deployments=repoint_history,
        )
        if replacing_overlay:
            info(
                f"quiescing '{name}' before replacing its immutable "
                "deployment snapshot"
            )
            stopped = _stop_deployment(name)
            if stopped != 0:
                err(
                    f"REFUSING to rewrite config/repos/{name}.env while its "
                    "existing deployment is still running"
                )
                return stopped
        with open(overlay, "w") as f:
            f.write(content)
        ok(f"wrote config/repos/{name}.env  (repo={repo}, target={target_dir})")
    else:
        ok(f"reusing config/repos/{name}.env  (pass --write-config to regenerate)")
        # Reused overlay with no TEST_CMD → still warn loudly (teeth matter).
        if not read_repo_env_field(overlay, "TEST_CMD"):
            _no_tests_banner()

    env = dict(os.environ)
    if args.roles:
        env["ROLES"] = " ".join(args.roles)
    if args.max_ticks is not None:
        env["ISSUE_AGENT_MAX_TICKS"] = str(args.max_ticks)
    if not heal_entrypoint_drift() and running_from_transient_checkout():
        # Launching from here would run every daemon out of the transient
        # tree; other heal failures keep today's behavior (the preflight
        # reports them) because the canonical checkout is still what executes.
        err("refusing to launch from a transient worktree checkout — run "
            "`make install` from the canonical checkout, then `issue-agent "
            "start` there.")
        return 1
    info(f"delegating to bin/point-at.sh {name}")
    # exec — hand over the terminal (Ctrl-C trap, log tail live in point-at.sh).
    os.execve(_BASH, [_BASH, _bin("point-at.sh"), name], env)


def cmd_stop(args) -> int:
    repo, target_dir, name = _resolve_context(args)
    # P0R-2: refuse rather than SIGTERM another deployment's daemons. Fatal
    # (not a warning) because this command is destructive — unlike `status`,
    # there is no read-only-so-warn-and-proceed option here.
    mismatch = check_overlay_mismatch(name, repo, target_dir)
    if mismatch:
        err(f"REFUSING to stop '{name}' — wrong repo/checkout:")
        err(f"  {mismatch}")
        err("This would SIGTERM another deployment's daemons. Pass --name <other> "
            "for the overlay that matches this checkout.")
        return 2
    return _stop_deployment(name)


# M11: STATE_DIR/REVIEW_STATE_DIR/STEWARD_STATE_DIR paired with the synth
# default dirname pattern `synth_repo_env` writes for each (issue-agent-<name>
# / review-agent-<name> / steward-agent-<name>) — one place both journal_paths
# and synth_repo_env's isolated-state block agree on the naming.
_JOURNAL_ROLE_DEFAULTS = (
    ("STATE_DIR", "issue-agent"),
    ("REVIEW_STATE_DIR", "review-agent"),
    ("STEWARD_STATE_DIR", "steward-agent"),
)


def _deployment_env_field(name: str, field: str) -> str:
    """One composed config field, read the way the DAEMONS resolved it.

    `config/.active.<name>.env` is literally the snapshot a launch composed and
    the daemons loaded — shared.env plus the repo overlay, already merged. The
    overlay ALONE goes blind the moment REPO or a `*_STATE_DIR` is set in
    `config/shared.env` (or only in the snapshot): the read side then derives a
    state dir nobody writes to, and reports an empty history while SQLite spend
    accrues. So: snapshot first, overlay second, per field — per field rather
    than per file, because a snapshot missing one key must still fall through
    to the overlay rather than blanking it.

    "" for every path that does not exist or does not assign the field, which
    is the same "absent, not an error" contract `read_repo_env_field` has.
    """
    for path in (
        _active_env_path(name),
        os.path.join(REPOS_DIR, f"{name}.env"),
    ):
        value = read_repo_env_field(path, field)
        if value:
            return value
    return ""


def deployment_env_field(name: str, field: str) -> str:
    """Public read of one composed config field (snapshot, then overlay).

    `read_repo_env_field` alone goes blind to anything set only in
    `config/shared.env` or only in the launch snapshot, which is where a
    deployment's REVIEW_BOT normally lives.
    """
    return _deployment_env_field(name, field)


def journal_paths(name: str) -> list[str]:
    """Every per-role journal overlay `name`'s daemons may have written (M11):
    fix, review, and steward each get their OWN STATE_DIR and therefore their
    own journal.jsonl. Reading STATE_DIR alone (the pre-M11 `journal_path`)
    made review/steward runs, their spend blocks, and their loops invisible to
    `issue-agent status`. Each role's state dir comes from the composed launch
    snapshot, else the overlay (`_deployment_env_field`); an absent/empty one
    falls back to the daemon's own derivation (config.default_state_dir keyed
    on that same REPO), because that is where the daemons actually write when
    the key is missing; only a deployment with no REPO anywhere falls back to
    the `synth_repo_env` `<role>-<name>` layout. A role that never ran still
    gets a path here —
    `read_events` (adapters/journal.py) already treats a missing file as an
    empty history, not an error, so callers need no existence check and a
    role that never ran is silently skipped, not flagged."""
    repo = _deployment_env_field(name, "REPO")
    paths = []
    for env_key, dirname in _JOURNAL_ROLE_DEFAULTS:
        raw = _deployment_env_field(name, env_key)
        if raw:
            state = os.path.expandvars(os.path.expanduser(raw))
        elif repo:
            # A hand-written overlay with REPO but no explicit state-dir keys:
            # the daemons fall back to config.default_state_dir, NOT to the
            # synth `<role>-<name>` layout — deriving anything else here reads
            # empty journals forever while SQLite spend accrues (keifu
            # 2026-08-02: dashboard showed $0.57 settled with "no runs").
            state = config_default_state_dir(repo, dirname)
        else:
            state = os.path.expanduser(f"~/.local/state/{dirname}-{name}")
        paths.append(os.path.join(state, "journal.jsonl"))
    return paths


def journal_path(name: str) -> str:
    """The fix-role journal for overlay `name` (STATE_DIR only). Kept for
    back-compat (tests/adapters/test_journal.py, outside this leg's file
    ownership, calls this directly) — prefer `journal_paths` (M11), which also
    covers review/steward and is what `cmd_status` now reads."""
    return journal_paths(name)[0]


def _env_cap(name: str, key: str, default: float) -> float:
    """A cap knob as status should report it: overlay wins over shared, and an
    unset/garbled value falls back to the HarnessConfig default rather than
    silently reporting a $0 (which reads as 'disabled')."""
    for path in (os.path.join(REPOS_DIR, f"{name}.env"), SHARED_ENV):
        raw = read_repo_env_field(path, key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                return 0.0
    return default


def daily_cap(name: str) -> float:
    """DAILY_SPEND_CAP_USD — today's spend on THIS overlay's repo (#32)."""
    return _env_cap(name, "DAILY_SPEND_CAP_USD", 700.0)


def global_daily_cap(name: str) -> float:
    """GLOBAL_DAILY_SPEND_CAP_USD — today's spend across every repo. Defaults to
    0 (off), matching HarnessConfig."""
    return _env_cap(name, "GLOBAL_DAILY_SPEND_CAP_USD", 0.0)


def spend_status_lines(
    journal_file: str | list[str],
    cap: float = 0.0,
    now_fn=None,
    repo: str | None = None,
    global_cap: float = 0.0,
    *,
    repository_spent: float = 0.0,
    all_repositories_spent: float = 0.0,
) -> list[str]:
    """The spend/health section of `issue-agent status`, as plain lines.

    Reads only the event files; authoritative SQLite spend totals are injected
    by the caller so rendering stays deterministic and testable without a
    daemon. An absent history and zero spend yield `[]`.

    M11: `journal_file` may be a single path (back-compat — pre-M11 callers,
    e.g. tests/adapters/test_journal.py outside this leg's file ownership,
    pass the fix journal alone) or a list of per-role journals. `cmd_status`
    now passes `journal_paths(name)`, the STATE_DIR/REVIEW_STATE_DIR/
    STEWARD_STATE_DIR triple — reading STATE_DIR alone made review/steward
    runs, their spend blocks, and their loops invisible here. Every file's
    events are read then merge-sorted into ONE global `ts` timeline before
    anything below is computed, so a review run interleaves correctly with a
    fix run instead of rendering as three disjoint, file-ordered lists. A role
    that never ran contributes no events (`read_events` tolerates a missing
    file), so it is silently absent, not an error.

    The three warnings are the failure shapes that cost real money and are
    otherwise invisible until the bill: a leg that re-dispatches without
    converging, a cap that actually fired, and work that quietly stopped because
    it escalated to a human who has not looked yet.
    """
    from datetime import datetime, timezone

    from ..adapters.event_log import read_event_log
    from ..adapters.journal import (
        events_within,
        legacy_event_view,
        parse_ts,
        read_events,
        repeated_dispatch_warning,
    )

    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    paths = [journal_file] if isinstance(journal_file, str) else list(journal_file)
    events: list[dict] = []
    malformed_typed_lines = 0
    for path in paths:
        typed_path = os.path.join(os.path.dirname(path), "events-v2.jsonl")
        typed = read_event_log(typed_path)
        if typed.source_files:
            malformed_typed_lines += typed.malformed_lines
            events.extend(legacy_event_view(event) for event in typed.events)
        else:
            events.extend(read_events(path))
    # Global order across files. A missing/garbled `ts` sorts first (never
    # corrupts the ordering of the events after it) rather than raising —
    # readers tolerating garbage is the module's own contract (journal.py).
    events.sort(key=lambda e: parse_ts(e.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    spent = all_repositories_spent
    if not events and not spent and not malformed_typed_lines:
        return []

    lines = ["", _c("36", "── spend & health ──")]
    # DAILY_SPEND_CAP_USD bounds THIS repo (#32), so pairing it with the
    # all-repos total read as "you have $640 of headroom" on a box where the
    # other deployment had spent it all. Cap and number must share a scope.
    if repo:
        budget = f" / ${cap:.2f} cap" if cap > 0 else " (no daily cap)"
        lines.append(f"  today, {repo}: ${repository_spent:.2f}{budget}")
        global_budget = (
            f" / ${global_cap:.2f} cap" if global_cap > 0 else " (no global cap)"
        )
        lines.append(f"  today, all repos: ${spent:.2f}{global_budget}")
    else:
        budget = f" / ${cap:.2f} cap" if cap > 0 else " (no daily cap)"
        lines.append(f"  today, all repos: ${spent:.2f}{budget}")

    # `agent_run` only, not `agent_run_start` — the latter (wave-A
    # JournalingRuntime pre-await receipt) is the other half of the same
    # dispatch and would double-list a run still in flight, or one that never
    # settled, as if it were a completed one.
    runs = [e for e in events if e.get("event") == "agent_run"][-5:]
    if runs:
        lines.append("  last runs:")
        for r in runs:
            why = r.get("exit_reason") or ("error" if r.get("is_error") else r.get("status") or "ok")
            lines.append(
                f"    {str(r.get('ts', ''))[:19]}  {r.get('kind', '?'):<14} "
                f"#{r.get('number', '?'):<6} ${float(r.get('cost_usd', 0) or 0):>6.2f}  {why}"
            )

    warnings: list[str] = []
    if malformed_typed_lines:
        warnings.append(
            f"skipped {malformed_typed_lines} malformed schema-v2 event line(s)"
        )
    hour = events_within(events, 3600, now_fn)
    loop = repeated_dispatch_warning(hour, 3600, 4)
    if loop:
        warnings.append(loop)
    today = now_fn().strftime("%Y-%m-%d")
    blocks = [e for e in events
              if e.get("event") == "spend_block" and str(e.get("ts", ""))[:10] == today]
    if blocks:
        scopes = ", ".join(sorted({str(b.get("scope", "?")) for b in blocks}))
        warnings.append(f"{len(blocks)} spend block(s) today (scope: {scopes}) — dispatch was skipped")
    escalations = [e for e in events_within(events, 86400, now_fn)
                   if e.get("event") == "escalation"]
    if escalations:
        where = ", ".join(f"{e.get('target', '?')} #{e.get('number', '?')}" for e in escalations[:5])
        warnings.append(f"{len(escalations)} escalation(s) to needs-human-review in 24h: {where}")
    if warnings:
        lines.append(_c("33", "  WARNINGS:"))
        lines.extend(f"    ! {w}" for w in warnings)
    return lines


_SYSTEMD_ROLE_UNITS = {
    "fix": "issue-agent-fix-watcher-{name}.service",
    "review": "issue-agent-review-watcher-{name}.service",
    "steward": "issue-agent-steward-{name}.service",
}
_STATUS_COMMAND_TIMEOUT_S = 2.0


def _systemd_status_log(
    name: str,
    role: str,
    lines: int,
) -> tuple[str, str] | None:
    """The authoritative log tail for an installed Linux user service.

    Foreground point-at deployments write files, but systemd units explicitly
    send stdout/stderr to the journal. Falling through to an old foreground log
    while a unit is live makes `status` show stale incidents as current ones.
    """

    if not sys.platform.startswith("linux"):
        return None
    unit = _SYSTEMD_ROLE_UNITS[role].format(name=name)
    state_command = [
        "systemctl",
        "--user",
        "show",
        unit,
        "--property=LoadState",
        "--property=ActiveState",
    ]
    try:
        state = subprocess.run(
            state_command,
            capture_output=True,
            text=True,
            timeout=_STATUS_COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            f"systemd status unavailable: {unit}",
            f"  systemctl failed: {exc}",
        )
    if state.returncode != 0:
        detail = (state.stderr or state.stdout).strip() or (
            f"systemctl exited {state.returncode}"
        )
        return f"systemd status unavailable: {unit}", f"  {detail}"
    properties = dict(
        line.split("=", 1)
        for line in state.stdout.splitlines()
        if "=" in line
    )
    if properties.get("LoadState") != "loaded":
        return None
    if properties.get("ActiveState") not in {
        "active",
        "activating",
        "reloading",
        "deactivating",
        "failed",
    }:
        return None
    journal_command = [
        "journalctl",
        "--user",
        "--unit",
        unit,
        "--lines",
        str(lines),
        "--no-pager",
        "--output=cat",
    ]
    try:
        journal = subprocess.run(
            journal_command,
            capture_output=True,
            text=True,
            timeout=_STATUS_COMMAND_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            f"systemd journal unavailable: {unit}",
            f"  journalctl failed: {exc}",
        )
    if journal.returncode != 0:
        detail = (journal.stderr or journal.stdout).strip() or (
            f"journalctl exited {journal.returncode}"
        )
        return f"systemd journal unavailable: {unit}", f"  {detail}"
    return f"systemd journal: {unit}", journal.stdout.strip()


def cmd_status(args) -> int:
    repo, target_dir, name = _resolve_context(args)
    # P0R-2: warn, don't refuse — status is read-only (no SIGTERM, no service
    # install, no dispatch), so a mismatched overlay is misleading, not
    # dangerous. Showing it anyway keeps `status` usable to *diagnose* exactly
    # this collision from either checkout, which refusing would prevent.
    mismatch = check_overlay_mismatch(name, repo, target_dir)
    if mismatch:
        warn(f"config/repos/{name}.env does not match this checkout — showing it anyway (status is read-only):")
        warn(f"  {mismatch}")
    roles = _role_pids(name)
    heartbeats = _fresh_role_heartbeats(name)
    launchers = _launcher_pids(name)
    print(_c("36", f"▸ status for '{name}'" + (f"  ({repo})" if repo else "")))
    print(f"  launcher (point-at.sh): {'running ' + str(launchers) if launchers else 'stopped'}")
    for role in ROLES:
        pids = roles[role]
        if pids:
            state = _c("32", "running") + f" {pids}"
        elif role in heartbeats:
            state = _c("32", "running") + " (fresh heartbeat; PID not visible)"
        else:
            state = _c("33", "stopped")
        print(f"  {role:8s}: {state}")
    logs = log_dir()
    for role in ROLES:
        # A foreground launcher may own only a subset of roles. Query each
        # installed unit independently; inactive units return None and fall
        # through to that role's foreground file.
        service_log = _systemd_status_log(name, role, args.lines)
        if service_log is not None:
            source, output = service_log
            print(_c("36", f"\n── {source} ──"))
            print(output or "  (empty)")
            # A foreground launcher can replace one failed role while other
            # roles stay service-owned. Ownership is ambiguous per role, so
            # show both labeled sources instead of hiding the live file behind
            # a failed/unavailable unit diagnostic.
            if not launchers:
                continue
        # Same default as point-at.sh's LOG_DIR; the old /tmp path is checked as
        # a fallback so status still tails logs of a daemon started pre-move.
        log = os.path.join(logs, f"{role}-{name}.log")
        if not os.path.isfile(log):
            log = f"/tmp/issue-agent-{role}-{name}.log"
        if os.path.isfile(log):
            print(_c("36", f"\n── tail {log} ──"))
            print(_run(["tail", "-n", str(args.lines), log]) or "  (empty)")
    # Journal-backed summary. Wrapped: status must still report daemon liveness
    # even if the journal is unreadable — the diagnostic must not be the thing
    # that fails when things are going wrong.
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        from ..adapters.control_path import canonical_control_store_path
        from ..adapters.control_store import read_settled_spend
        from ..domain.contracts import daily_budget_scope

        now = datetime.now(timezone.utc)
        period = daily_budget_scope(
            "*", "global", "USD", now, ZoneInfo("UTC")
        )
        spend = read_settled_spend(
            canonical_control_store_path(),
            period.period_start,
            period.period_end,
        )
        configured_repo = read_repo_env_field(
            os.path.join(REPOS_DIR, f"{name}.env"),
            "REPO",
        )
        repository_spend = (
            spend.for_repository(configured_repo)
            if configured_repo
            else None
        )
        if spend.total is None or (
            configured_repo and repository_spend is None
        ):
            raise RuntimeError(
                spend.detail or "settled spend is unavailable"
            )
        for line in spend_status_lines(
            journal_paths(name),
            daily_cap(name),
            now_fn=lambda: now,
            repo=configured_repo,
            global_cap=global_daily_cap(name),
            repository_spent=(
                repository_spend.micros / 1_000_000
                if repository_spend is not None
                else 0.0
            ),
            all_repositories_spent=spend.total.micros / 1_000_000,
        ):
            print(line)
    except Exception as exc:  # noqa: BLE001
        err(f"(could not read the event journal: {exc})")
    # Interventions come from the schema-v2 fold, not from `spend_status_lines`:
    # operator actions are only ever written as typed `lifecycle.action.applied`
    # events, and the legacy journal.jsonl those lines read no longer exists on
    # a v2 deployment. Its own try/except for the same reason as the block
    # above — a diagnostic must not be what fails.
    try:
        print(interventions_status_line(name, repo))
    except Exception as exc:  # noqa: BLE001
        err(f"(could not read operator interventions: {exc})")
    return 0


def interventions_status_line(name: str, repo: str) -> str:
    """The `interventions:` line of `issue-agent status`, indented to match."""
    from ..adapters.control_path import canonical_control_store_path
    from ..adapters.journal import default_ledger_path
    from ..domain.dashboard import interventions_line
    from . import dashboard as dash

    deployment = dash.collect(
        name,
        repo,
        journal_paths(name),
        log_dir(),
        default_ledger_path(),
        control_db_path=canonical_control_store_path(),
    )
    return "  " + interventions_line(deployment.metrics)


def log_dir() -> str:
    """Where point-at.sh writes `<role>-<name>.log`. Same resolution `cmd_status`
    uses (LOG_DIR wins, else $XDG_STATE_HOME/issue-agent/logs)."""
    return os.environ.get("LOG_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
        "issue-agent", "logs",
    )


@dataclass(frozen=True)
class DashwebPreparation:
    ready: bool
    detail: str
    rebuild_attempted: bool = False


DASHBOARD_REBUILD_TIMEOUT_S = 330.0


def _run_dashboard_build() -> int:
    process = subprocess.Popen(
        ["make", "-C", HARNESS_ROOT, "dashboard-ui"],
        start_new_session=True,
    )
    try:
        return process.wait(timeout=DASHBOARD_REBUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _terminate_dashboard_build(process)
        return 124
    except KeyboardInterrupt:
        _terminate_dashboard_build(process)
        raise


def _terminate_dashboard_build(process) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def dashboard_build_command(harness_root: str | None = None) -> str:
    """A shell-safe recovery command that works from any target checkout."""
    root = shlex.quote(harness_root or HARNESS_ROOT)
    return f"cd {root} && uv sync --extra sdk --extra dash && make dashboard-ui"


def dashboard_frontend_build_command(harness_root: str | None = None) -> str:
    """The direct recovery command for an absent generated SPA bundle."""
    root = shlex.quote(os.path.join(harness_root or HARNESS_ROOT, "dashboard"))
    return f"cd {root} && npm run build"


def _dashboard_startup_error(detail: str) -> int:
    err("interactive dashboard startup failed:")
    err(f"  {detail}")
    err("  install server dependencies: uv sync --extra dash (or make install)")
    err(f"  build a missing frontend bundle: {dashboard_frontend_build_command()}")
    err("  from the harness checkout, that build is: cd dashboard && npm run build")
    return 2


def _prepare_dashweb() -> DashwebPreparation:
    """Ensure the interactive dashboard can serve the current checkout.

    A missing optional server dependency remains an explicit install step. Once
    FastAPI is present, missing/stale generated assets are rebuilt in the
    harness checkout and revalidated before they can be served.
    """
    import importlib.util

    if importlib.util.find_spec("fastapi") is None:
        return DashwebPreparation(False, "the optional dashboard server is not installed")
    from ..adapters.dashweb.bundle import bundle_status

    before = bundle_status(HARNESS_ROOT)
    if before.ready:
        return DashwebPreparation(True, before.detail)

    info(f"dashboard frontend is {before.state} ({before.detail}); rebuilding…")
    try:
        returncode = _run_dashboard_build()
    except OSError as exc:
        return DashwebPreparation(
            False,
            f"automatic dashboard rebuild could not start ({exc})",
            rebuild_attempted=True,
        )

    after = bundle_status(HARNESS_ROOT)
    if returncode == 0 and after.ready:
        return DashwebPreparation(True, "dashboard frontend rebuilt and validated", True)
    if returncode == 124:
        detail = "automatic dashboard rebuild timed out"
    elif returncode != 0:
        detail = f"automatic dashboard rebuild failed with exit {returncode}"
    else:
        detail = f"automatic dashboard rebuild produced an invalid bundle ({after.detail})"
    return DashwebPreparation(False, detail, rebuild_attempted=True)


def cmd_dashboard(args) -> int:
    """Every deployment, every issue, one read-only view (see app/dashboard.py).

    Strictly read-only: it reads the journals + the global ledger and writes
    nothing but the optional --html file. No `gh` calls — journal-only truth.

    Serving is the default mode: the bare `issue-agent dashboard` an operator
    types mid-incident should open the thing they can click through, not print
    a wall of text. --text and --html keep the one-shot renders (scripts, CI,
    and `| less` all want those) and take precedence over --serve.
    """
    from ..adapters.journal import default_ledger_path
    from ..adapters.control_path import canonical_control_store_path
    from . import dashboard as dash

    names = list(args.names) or list(dash.deployment_names(REPOS_DIR))
    if not names:
        err(f"no deployments found in {REPOS_DIR} — run `issue-agent start` in a "
            f"target repo first, or pass --name <overlay>.")
        return 2
    ledger = default_ledger_path()
    logs = log_dir()

    def build() -> list:
        return [
            dash.collect(
                name,
                read_repo_env_field(os.path.join(REPOS_DIR, f"{name}.env"), "REPO"),
                journal_paths(name),
                logs,
                ledger,
                stale_after_s=args.stale_minutes * 60.0,
                control_db_path=canonical_control_store_path(),
            )
            for name in names
        ]

    # One-shot modes win over serving: an operator who asked for a file or a
    # report wants it on stdout, not a server they have to Ctrl-C.
    if args.html:
        path = os.path.abspath(os.path.expanduser(args.html))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(dash.render_html(build()))
        ok(f"wrote {path}")
        return 0
    if args.text or not args.serve:
        print(dash.render_text(build(), details=args.details))
        return 0

    url = f"http://127.0.0.1:{args.serve}/"
    if getattr(args, "legacy", False):
        info(f"serving the legacy dashboard at {url} (Ctrl-C to stop)")
        reload_check = None
        if os.environ.get("HOT_RELOAD", "1") != "0":
            from ..adapters.reload import ReloadWatch

            reload_check = ReloadWatch(HARNESS_ROOT).changed
        try:
            return dash.serve(
                args.serve,
                lambda: dash.render_html(build()),
                reload_check=reload_check,
            )
        except KeyboardInterrupt:
            print()
        return 0

    try:
        preparation = _prepare_dashweb()
    except Exception as exc:
        return _dashboard_startup_error(str(exc))
    if not preparation.ready:
        return _dashboard_startup_error(preparation.detail)

    try:
        from .dashweb.server import serve as dashweb_serve
        from ..adapters.reload import RELOAD_EXIT_CODE

        info(f"serving the interactive dashboard at {url} (Ctrl-C to stop)")
        result = dashweb_serve(args.serve)
        if result in {0, RELOAD_EXIT_CODE}:
            return result
        return _dashboard_startup_error(
            f"dashboard server exited with status {result}"
        )
    except KeyboardInterrupt:
        print()
        return 0
    except SystemExit as exc:
        return _dashboard_startup_error(
            f"dashboard server exited during startup with status {exc.code}"
        )
    except Exception as exc:
        return _dashboard_startup_error(str(exc))


def cmd_doctor(args) -> int:
    """Compose env for this repo (if detectable) then run bin/doctor.sh, which
    reads AGENT_ENV. With no repo context it still runs tooling/identity checks."""
    # #187: doctor once ran from a stale worktree venv and reported four
    # confidently wrong errors about the deployment's config. Only the
    # transient-worktree case refuses outright — there every path below IS the
    # wrong checkout's. Other drift shapes (a foreign shim shadowing this
    # checkout) still render: doctor runs from the code the operator invoked,
    # and the drift leads as its first, exit-reddening finding.
    drift = entrypoint_drift()
    if drift and running_from_transient_checkout():
        err(f"REFUSING to run doctor — stale/partial install: {drift}")
        err("Doctor would validate the wrong checkout's config (#187). Run "
            "`make install` from the canonical checkout, then re-run doctor.")
        return 2
    if drift:
        err(f"install drift: {drift} — heal before trusting anything below "
            "(`make install`, or `issue-agent start` with no live deployment).")
    repo, target_dir, name = _resolve_context(args)
    # P0R-2: fatal, not a warning. doctor.sh's own per-repo state-dir collision
    # check (bin/doctor.sh) can only compare MULTIPLE overlays against each
    # other — it is blind to exactly this case (one overlay, wrong repo).
    # Composing env for the wrong repo and reporting "✓" gives false
    # confidence right before an operator runs `start` for real.
    mismatch = check_overlay_mismatch(name, repo, target_dir)
    if mismatch:
        err(f"REFUSING to run doctor for '{name}' — wrong repo/checkout:")
        err(f"  {mismatch}")
        err("Preflight would check identities/access for the WRONG repo and could "
            "report a false ✓. Pass --name <other> for the overlay that matches "
            "this checkout.")
        return 2
    env = dict(os.environ)
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    if os.path.isfile(SHARED_ENV) and os.path.isfile(overlay):
        r = subprocess.run([_BASH, _bin("compose-env.sh"), name])
        if r.returncode == 0:
            env["AGENT_ENV"] = _active_env_path(name)
        else:
            # P0R-2 adjacency: a FAILED compose used to leave any AGENT_ENV
            # inherited from the operator's shell in place, so doctor would
            # preflight whatever snapshot that pointed at — possibly another
            # deployment's — while reporting on this one. Dropping it makes the
            # failure visible instead (doctor.sh falls back and complains).
            env.pop("AGENT_ENV", None)
            err(f"compose-env.sh failed for '{name}' — checks below run WITHOUT a "
                f"composed env snapshot.")
    elif os.path.isfile(SHARED_ENV):
        # shared.env exists but this repo has no overlay yet — the normal state
        # before the first `issue-agent start`. compose-env.sh needs both files,
        # so without this branch AGENT_ENV stayed unset and doctor.sh fell back
        # to its `${AGENT_ENV:-config/agent.env}` default: the LEGACY
        # single-repo file, which is a gitignored copy of agent.env.example full
        # of dummy values. Doctor then validated a config the operator never
        # wrote and printed "✓ three distinct identities (fix-bot / review-bot /
        # steward-bot)" — a false green on the exact thing being preflighted.
        # Point it at shared.env instead, and inject the DETECTED repo/dir the
        # overlay would have supplied (shared.env deliberately holds neither),
        # so the identity and access checks are about the real target.
        env["AGENT_ENV"] = SHARED_ENV
        if repo:
            env["REPO"] = repo
        if target_dir:
            env["TARGET_DIR"] = target_dir
        warn(f"no overlay config/repos/{name}.env yet — checking config/shared.env "
             f"against the detected repo. `issue-agent start` writes the overlay "
             f"(TEST_CMD, BASE_BRANCH, state dirs); re-run doctor after that.")
    composed = _composed_env(env)
    _doctor_codex_binary(composed)
    exit_codes = _doctor_test_failure_exit_codes(composed, name)
    identity = _doctor_identity_pools(composed)
    services = _doctor_installed_services(composed, name)
    rc = subprocess.run([_BASH, _bin("doctor.sh")], env=env).returncode
    # An error-level identity finding must redden the EXIT CODE, not only the
    # terminal. doctor is used as a gate (bin/point-at.sh, CI); a config whose
    # fixer token is present-but-unusable 401s every call with no fallback, and
    # returning doctor.sh's 0 for it is the false green this check replaces.
    if rc == 0 and (drift or any(
        f.level == "error"
        for f in tuple(identity.findings) + exit_codes + services
    )):
        return 1
    return rc


def _doctor_installed_services(composed: dict, name: str) -> tuple[Finding, ...]:
    """Findings about the INSTALLED services' env, not this shell's. The 2026-08-10
    relaunch proved the divergence class: doctor green interactively while the
    units ran a PATH with no codex (#650) and an install-time config copy that
    re-composition never reaches."""
    findings = installed_service_findings(
        name, composed, _active_env_path(name)
    )
    if findings:
        info("installed services (systemd/launchd)")
        for finding in findings:
            (err if finding.level == "error" else warn)(finding.message)
    return findings


CODEX_RUNTIME_KEYS = ("FIXER_RUNTIME", "REVIEWER_RUNTIME", "STEWARD_RUNTIME")


def _composed_env(env: dict) -> dict:
    """The env the DAEMONS will see: the composed snapshot AGENT_ENV names, or
    the inherited environment when there is none.

    An unreadable or unsourceable snapshot is doctor.sh's error to report; the
    Python-side checks fall back to the inherited environment rather than
    pre-empting it with a second, less informative failure."""
    agent_env = env.get("AGENT_ENV", "")
    if not agent_env:
        return dict(env)
    try:
        return source_env_file(agent_env, env)
    except ValueError:
        return dict(env)


def _doctor_identity_pools(composed: dict) -> IdentityPoolReport:
    """Report which credential each role resolves to and whose GitHub rate-limit
    pool it spends. doctor.sh checks the tokens it can SEE — it cannot see the
    fixer falling through to ambient `gh auth`, nor that two roles pointed at one
    token share one 5,000-point/hour pool (both keifu, 2026-08-02)."""
    info("identity and rate-limit pools")
    return report_identity_pools(composed)


def _doctor_test_failure_exit_codes(composed: dict, name: str) -> tuple[Finding, ...]:
    """Report runners in TEST_CMD whose assertion-failure exit code the gate does
    not trust. doctor.sh only echoes TEST_CMD; it cannot parse the declaration
    the Python gate enforces. The raw value is passed through verbatim — absent
    (None) and present-but-empty mean different things to the gate."""
    declared = composed.get("GATE_TEST_FAILURE_EXIT_CODES")
    findings = test_failure_exit_code_findings(
        composed.get("TEST_CMD") or "",
        declared if declared is None else str(declared),
        name,
    )
    if findings:
        info("project commands: red-test exit codes")
        for finding in findings:
            (err if finding.level == "error" else warn)(finding.message)
    return findings


def _doctor_codex_binary(composed: dict) -> CodexResolutionReport | None:
    """Report codex binary resolution when the composed env actually selects a
    codex runtime (or pins CODEX_BIN). doctor.sh cannot do this: it checks the
    codex CONTRACT, not which of several installed codex binaries the daemons
    will exec, which is what broke keifu on 2026-08-02."""
    selects_codex = any(
        (composed.get(key) or "").strip().lower() == "codex"
        for key in CODEX_RUNTIME_KEYS
    )
    codex_bin = (composed.get("CODEX_BIN") or "").strip()
    if not selects_codex and not codex_bin:
        return None
    info("codex binary resolution")
    return report_codex_resolution(codex_bin or "codex")


def _label_issues(args, label: str) -> int:
    repo = canonical_repository(args.repo or detect_repo(os.getcwd()))
    if not repo:
        err("could not detect the repo. Pass --repo owner/name.")
        return 2
    target_dir = detect_target_dir(os.getcwd())
    name = resolve_name(repo, target_dir=target_dir)
    effects = _operator_effects(repo, name, "cli")
    rc = 0
    for n in args.issues:
        try:
            number = int(n)
        except ValueError:
            err(f"invalid issue number {n!r}")
            rc = 1
            continue
        applied, detail = effects.set_issue_labels(number, add=(label,))
        if applied:
            ok(f"#{n} → {label}")
        else:
            err(f"failed to label #{n}: {detail}")
            rc = 1
    return rc


def cmd_ready(args) -> int:
    return _label_issues(args, "ai-ready")


def cmd_triage(args) -> int:
    return _label_issues(args, "ai-triage")


REMOTE_LABELS = (
    "ai-triage",
    "ai-ready",
    "ai-working",
    "ai-blocked",
    "ai-pr-open",
    "ai-done",
    "ai-closed",
    "ai-suggestion",
    "maybe-resolved",
    "needs-budget-review",
    "trivial",
    "minor",
    "major",
    "needs-human-review",
    "needs-design",
    "invariant",
    "recurrence",
    "P1",
    "P2",
    "P3",
    "harness",
)


def _operator_effects(repo: str, name: str, channel: str):
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    fields = _source_env_file(overlay)
    state_dirs = tuple(
        fields.get(key, "")
        for key in ("STATE_DIR", "REVIEW_STATE_DIR", "STEWARD_STATE_DIR")
    )
    if not any(state_dirs):
        state_dirs = (
            os.path.expanduser(f"~/.local/state/issue-agent-{name}"),
            os.path.expanduser(f"~/.local/state/review-agent-{name}"),
            os.path.expanduser(f"~/.local/state/steward-agent-{name}"),
        )
    return build_operator_effects(
        repo,
        state_dirs,
        identity=f"{repo}:operator:{channel}",
    )


def cmd_prepare_remote(args) -> int:
    """Durably provision the remote prerequisites used by point-at."""

    effects = _operator_effects(args.repo, args.name, "bootstrap")
    failures = []
    if not args.skip_labels:
        for label in REMOTE_LABELS:
            applied, detail = effects.ensure_label(
                label,
                color={"P1": "b60205", "P2": "d93f0b", "P3": "fbca04"}.get(
                    label, "ededed"
                ),
                description=(
                    f"harness priority {label}"
                    if label in {"P1", "P2", "P3"}
                    else ""
                ),
            )
            if not applied:
                failures.append(f"label {label}: {detail}")
    if args.ensure_auto_merge:
        applied, detail = effects.set_repository_auto_merge(True)
        if not applied:
            failures.append(f"allow_auto_merge: {detail}")
    if failures:
        for failure in failures:
            err(f"remote preparation failed — {failure}")
        return 1
    ok("remote lifecycle labels and repository settings verified")
    return 0


def cmd_publish_base_branch(args) -> int:
    effects = _operator_effects(args.repo, args.name, "bootstrap")
    applied, detail = effects.publish_base_branch(args.target_dir, args.branch)
    if not applied:
        err(f"failed to publish base branch: {detail}")
        return 1
    ok(f"published origin/{args.branch} with a durable effect receipt")
    return 0


def cmd_install_service(args) -> int:
    repo, target_dir, name = _resolve_context(args)
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    if not (os.path.isfile(SHARED_ENV) and os.path.isfile(overlay)):
        err(f"need config/shared.env + config/repos/{name}.env first (run `issue-agent start` once).")
        return 2
    # P0R-2: fatal. This installs a PERSISTENT OS service (systemd/launchd) —
    # worse than a one-shot `start` against the wrong repo, since it keeps
    # relaunching against it after this process exits.
    mismatch = check_overlay_mismatch(name, repo, target_dir)
    if mismatch:
        err(f"REFUSING to install a service for '{name}' — wrong repo/checkout:")
        err(f"  {mismatch}")
        err("This would install a PERSISTENT service running against a DIFFERENT "
            "repo. Pass --name <other> for the overlay that matches this checkout.")
        return 2
    if args.roles:
        err(
            "partial service upgrades are unsafe — install all roles so every "
            "legacy writer stops before SQLite migration"
        )
        return 2
    # A4 fold-in: this used to run with check=False and never look at the
    # return code — a compose failure (bad overlay syntax, missing file
    # between check above and here, etc.) fell through silently, and the
    # service unit got installed pointing AGENT_ENV at a STALE or missing
    # the composed active env (whatever a prior successful compose left behind, or
    # nothing). Fail loud and abort instead of installing against unknown env.
    r = subprocess.run([_BASH, _bin("compose-env.sh"), name])
    if r.returncode != 0:
        err(f"compose-env.sh failed (exit {r.returncode}) — not installing the "
            f"service against a stale/missing config/.active.{name}.env.")
        return r.returncode
    env = dict(
        os.environ,
        AGENT_ENV=_active_env_path(name),
        ISSUE_AGENT_DEPLOYMENT_ID=name,
    )
    script = "install-launchd.sh" if sys.platform == "darwin" else "install-systemd.sh"
    cmd = [_BASH, _bin(script)]
    return subprocess.run(cmd, env=env).returncode


def cmd_uninstall_service(args) -> int:
    script = "install-launchd.sh" if sys.platform == "darwin" else "install-systemd.sh"
    if args.all:
        return subprocess.run([_BASH, _bin(script), "uninstall"]).returncode

    repo, target_dir, name = _resolve_context(args)
    mismatch = check_overlay_mismatch(name, repo, target_dir)
    if mismatch:
        err(f"REFUSING to uninstall the service for '{name}' — wrong repo/checkout:")
        err(f"  {mismatch}")
        err("This could remove another deployment's persistent services. Pass "
            "--name for the overlay matching this checkout, or --all to "
            "explicitly remove every deployment.")
        return 2
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    if not (os.path.isfile(SHARED_ENV) and os.path.isfile(overlay)):
        err(
            f"need config/shared.env + config/repos/{name}.env to identify the "
            "deployment; use --all only when removing every deployment"
        )
        return 2
    composed = subprocess.run([_BASH, _bin("compose-env.sh"), name])
    if composed.returncode != 0:
        err(
            f"compose-env.sh failed (exit {composed.returncode}) — refusing "
            "an uninstall with unresolved deployment identity."
        )
        return composed.returncode
    env = dict(
        os.environ,
        AGENT_ENV=_active_env_path(name),
        ISSUE_AGENT_DEPLOYMENT_ID=name,
    )
    return subprocess.run(
        [_BASH, _bin(script), "uninstall", name],
        env=env,
    ).returncode


def cmd_init(args) -> int:
    """Interactive first-run: populate config/shared.env (global identity +
    gateway secrets). Idempotent — refuses to clobber without --force."""
    if os.path.isfile(SHARED_ENV) and not args.force:
        err(f"config/shared.env already exists — refusing to clobber. Edit it, or re-run with --force.")
        return 2
    if not sys.stdin.isatty():
        err("`issue-agent init` is interactive; run it in a terminal (or copy config/shared.example.env → config/shared.env by hand).")
        return 2

    print(_c("36", "▸ issue-agent init — creating config/shared.env (global identity + secrets)"))
    print("  Press Enter to accept the [default]. These are repo-INDEPENDENT; per-repo")
    print("  knobs (REPO/TARGET_DIR/TEST_CMD) are written by `issue-agent start`.\n")

    ask = _ask

    def ask_distinct_login(prompt: str, default: str, other_label: str, other: str) -> str:
        """A login that must not be the fixer's. GitHub 422s a self-approval, so a
        collision here is not a preference — it wedges every merge. Bounded
        re-prompt (ADR 0012): after the ceiling the answer stands and doctor is
        the authority."""
        value = ""
        for attempt in range(1, _PROMPT_ATTEMPTS + 1):
            value = ask(prompt, default)
            if not value or normalize_login(value) != normalize_login(other):
                return value
            warn(
                f"'{value}' is also {other_label} — GitHub 422s a review submitted "
                "by the PR's own author, so the fixer's account can never approve "
                "or merge its own work. Use a separate account."
            )
            if attempt < _PROMPT_ATTEMPTS:
                info(f"try again ({_PROMPT_ATTEMPTS - attempt} attempt(s) left).")
        warn(f"keeping '{value}' — `issue-agent doctor` will flag it.")
        return value

    fields: list[tuple[str, str]] = []
    # Normalized once, here: every downstream comparison (and the reviewer-token
    # prompt that hangs off it) must see the same value, whatever case was typed.
    identity_mode = ask("Identity mode (three|solo)", "three").lower()
    fields.append(("IDENTITY_MODE", identity_mode))
    gh_login = detect_fixer_login()
    if gh_login:
        ok(f"gh is authenticated as '{gh_login}'")
    else:
        warn("`gh api user` did not answer — gh is unauthenticated or unreachable; "
             "the fixer login cannot be pre-filled.")
    fix_bot = ask("Fixer GitHub login (ambient gh auth user)", gh_login)
    fields.append(("FIX_BOT", fix_bot))
    review_bot = ask_distinct_login(
        "Reviewer login (bot; distinct from fixer)",
        getattr(args, "review_bot_default", "") or "",
        "the fixer", fix_bot,
    )
    fields.append(("REVIEW_BOT", review_bot))
    fields.append((
        "STEWARD_BOT",
        ask_distinct_login(
            "Steward login (may equal reviewer)", review_bot, "the fixer", fix_bot
        ),
    ))
    print()
    gh_app = ask("Configure GitHub App for review+steward identity? (y/N)", "N").lower().startswith("y")
    if gh_app:
        fields.append(("GH_APP_ID", ask("GH App ID", "")))
        fields.append(("GH_APP_INSTALL_ID", ask("GH App installation ID", "")))
        fields.append(("GH_APP_KEY_FILE", ask("GH App private-key .pem path", "")))
    elif identity_mode == "three":
        # Without an App, approvals are posted with this PAT. Validating it here
        # (rather than only in doctor) is the difference between a wizard that
        # cannot produce a doctor-red config and one that reliably does.
        print("  The reviewer needs its OWN token to post approvals as that account.")
        token = prompt_review_bot_token(review_bot)
        if token:
            fields.append(("REVIEW_BOT_TOKEN", token))
        else:
            warn("no REVIEW_BOT_TOKEN set — the reviewer will act as the ambient gh "
                 "account and GitHub will 422 its approvals.")
    if identity_mode == "three":
        # Asked regardless of the App answer: the App covers review+steward only,
        # and the fixer's credential is FIX_BOT_TOKEN or nothing. Skipping this
        # is what put keifu's fixer on a human account and its personal quota.
        print()
        print("  The fixer needs its OWN token too. Without FIX_BOT_TOKEN the fix")
        print("  daemon falls through to the ambient `gh auth` credential: its")
        print("  branches, comments and PRs are attributed to that human account,")
        print("  and its GraphQL calls come out of that account's personal")
        print("  5,000-point/hour pool.")
        fix_token = prompt_fix_bot_token()
        if fix_token:
            fields.append(("FIX_BOT_TOKEN", fix_token))
    _wizard_pool_summary(fields)
    print()
    if ask("Enable Jira sync? (y/N)", "N").lower().startswith("y"):
        fields.append(("JIRA_SYNC", "1"))
        fields.append(("JIRA_PROJECT_KEY", ask("Jira project key (e.g. ACME)", "")))
        fields.append(("JIRA_CLOUD_ID", ask("Jira cloud id / host", "")))
    print()
    claude_ready = detect_claude_auth()
    codex_mode = detect_codex_auth()
    if claude_ready:
        ok("claude: credentials found (~/.claude or ANTHROPIC_API_KEY/AUTH_TOKEN)")
    else:
        warn("claude: no credentials found — run `claude` once to log in, or export "
             "ANTHROPIC_API_KEY, before selecting it as a runtime.")
    codex = report_codex_resolution("codex")
    codex_present = bool(codex.resolved_path)
    if not codex_present:
        warn("codex: install it before selecting it as a runtime.")
    elif codex_mode == "chatgpt":
        ok("codex: ChatGPT subscription auth")
    elif codex_mode == "apikey":
        ok("codex: API-key auth")
    else:
        ok("codex: auth mode unknown — `codex login` if it is not signed in")
    fields.append((
        "FIXER_RUNTIME",
        ask("Fixer runtime (claude|codex)", "claude" if claude_ready or not codex_present else "codex"),
    ))
    fields.append((
        "REVIEWER_RUNTIME",
        ask("Reviewer runtime (codex|claude)", "codex" if codex_present else "claude"),
    ))
    if any(k in ("FIXER_RUNTIME", "REVIEWER_RUNTIME") and v == "codex" for k, v in fields):
        # Only once codex is actually selected: a claude-only deployment has no
        # reason to answer a question about a binary it never spawns. An unpinned
        # or shadowed codex is then written out as an absolute CODEX_BIN by
        # default, because the wizard runs in the operator's shell while the
        # daemons run under systemd's minimal PATH, and on 2026-08-02 that gap
        # silently downgraded keifu's codex to a version the provider refuses.
        if not codex.healthy and codex.recommended_bin:
            if ask(f"Pin CODEX_BIN={codex.recommended_bin}? (Y/n)", "Y").lower().startswith("y"):
                fields.append(("CODEX_BIN", codex.recommended_bin))
            else:
                warn("CODEX_BIN left unpinned — the daemons' PATH decides which "
                     "codex runs, and `issue-agent doctor` re-checks it.")
        if codex_mode == "chatgpt":
            info("codex is on ChatGPT auth: the provider reports NO billed cost per "
                 "run, so spend for those legs is priced from the synthetic "
                 "CODEX_COST_PER_MTOK rate card, not from an invoice. No gateway "
                 "base URL is needed.")
        else:
            fields.append(("OPENAI_BASE_URL", ask("Gateway base URL (OpenAI-compatible)", "https://api.openai.com/v1")))
            print("  (OPENAI_API_KEY is read from the launching shell env — not stored here.)")

    lines = [
        "# Autonomous issue agent — SHARED config (global identity/secrets).",
        "# AUTO-GENERATED by `issue-agent init`. gitignored. Repo-independent knobs only.",
        "",
    ]
    for key, value in fields:
        if value:
            lines.append(f"{key}={_q(value)}")
    os.makedirs(os.path.dirname(SHARED_ENV), exist_ok=True)
    # This file may hold a long-lived PAT, so no secret byte may exist in it at
    # laxer permissions — not even briefly. O_CREAT's mode applies only when the
    # file is NEW, so a pre-existing (e.g. 0644 --force) file keeps its old mode
    # through the write unless it is tightened FIRST; hence chmod-before-open,
    # then chmod again after in case the create path raced an inherited umask.
    if os.path.exists(SHARED_ENV):
        os.chmod(SHARED_ENV, 0o600)
    handle = os.fdopen(
        os.open(SHARED_ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
    )
    with handle as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(SHARED_ENV, 0o600)
    ok(f"wrote {SHARED_ENV}")
    print("  Review it, then `cd <target-repo> && issue-agent start`.")
    return 0


def cmd_resolve_activation(args) -> int:
    """Offline resolve-family activation for this deployment (ADR 0015)."""
    return _cmd_workflow_activation(args, family="resolve")


def cmd_review_activation(args) -> int:
    """Offline review-family activation for this deployment (ADR 0015)."""
    return _cmd_workflow_activation(args, family="review")


def cmd_review_execution_activation(args) -> int:
    return _cmd_workflow_activation(args, family="review-execution")


def cmd_fix_activation(args) -> int:
    return _cmd_workflow_activation(args, family="fix")


def cmd_steward_activation(args) -> int:
    return _cmd_workflow_activation(args, family="steward")


def _cmd_workflow_activation(args, *, family: str) -> int:
    import sqlite3

    from ..adapters.control_store import ControlStoreError
    from . import resolve_activation, review_activation, review_execution_activation, continuation_activation

    activation = resolve_activation if family == "resolve" else review_activation

    name = args.name or _resolve_context(args)[2]
    try:
        transport = continuation_activation if family in {"fix", "steward"} else review_execution_activation if family == "review-execution" else activation
        scope = transport.activation_scope(name, harness_root=HARNESS_ROOT, extra_roots=tuple(args.legacy_state_dir),
                                           **({"family": family} if family in {"fix", "steward"} else {}))
        if args.action in ("inventory", "shadow"):
            result = transport.inspect(scope, shadow=args.action == "shadow")
        elif args.action == "rehearse":
            if not args.copy_to:
                err("rehearse requires --copy-to <new empty directory>")
                return 2
            result = transport.rehearse(scope, copy_to=args.copy_to)
        else:
            if not args.confirm_plan_digest:
                err("activate requires --confirm-plan-digest from a reviewed `shadow` report")
                return 2
            # Old writers cannot be fenced by a table they never read. An empty
            # pgrep is not evidence across PID namespaces, so heartbeats of
            # every deployment of this repository refuse too, failing closed.
            if isinstance(scope, continuation_activation.ContinuationActivationScope):
                if not args.plan_file:
                    err("continuation activate requires --plan-file with the original complete shadow report")
                    return 2
                result = continuation_activation.activate(scope, confirm_plan_digest=args.confirm_plan_digest,
                            plan_file=args.plan_file, liveness=lambda: tuple(_activation_liveness(name, scope.repository)))
            elif isinstance(scope, review_execution_activation.ReviewExecutionActivationScope):
                from dataclasses import replace
                if not args.plan_file:
                    err("execution activate requires --plan-file with the original complete shadow report")
                    return 2
                scope = replace(scope, liveness=lambda: tuple(_activation_liveness(name, scope.repository)))
                result = review_execution_activation.activate(
                    scope, confirm_plan_digest=args.confirm_plan_digest, plan_file=args.plan_file)
            else:
                live = _activation_liveness(name, scope.repository)
                if live:
                    err(f"REFUSING to activate '{name}': watchers may be running: {'; '.join(live)}")
                    return 2
                result = activation.activate(scope, confirm_plan_digest=args.confirm_plan_digest)
    except (ValueError, OSError, ControlStoreError, sqlite3.Error) as exc:
        err(f"{family} activation refused: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    succeeded = {"activate": {"activated", "already_active_with_this_plan"}, "rehearse": {"rehearsed"}}
    return 0 if args.action not in succeeded or result.get("status") in succeeded[args.action] else 1


# ── argument parser ───────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="issue-agent",
        description="Autonomous issue-resolving harness — run it from inside a target repo.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    from .measurement import add_parser
    add_parser(sub)

    def add_context_flags(sp):
        sp.add_argument("--repo", default="", help="owner/name (default: auto-detect from cwd)")
        sp.add_argument("--target-dir", default="", help="target working copy (default: git root of cwd)")
        sp.add_argument(
            "--name",
            default="",
            type=_deployment_name_arg,
            help="canonical config/repos/<name>.env overlay name (default: reuse/derive)",
        )

    sp = sub.add_parser("init", help="interactive first-run: create config/shared.env")
    sp.add_argument("--force", action="store_true", help="overwrite an existing shared.env")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser(
        "start",
        help="start the watchers against this repo (foreground)",
        description="Start the watchers in the FOREGROUND (Ctrl-C stops all; logs "
        "tail live). First run auto-creates config/shared.env via `init`. If "
        "--test-cmd is omitted, a cheap read-only LLM probe discovers the repo's "
        "test command; a repo with no tests proceeds behind a loud warning. The "
        "resolved command is then trial-run once (--no-verify-test-cmd skips it) "
        "so watch-mode or a missing binary is caught here rather than by every "
        "push being denied later.",
    )
    add_context_flags(sp)
    sp.add_argument("--roles", nargs="+", choices=ROLES, help="subset to run (default: all three)")
    # Default None, not "dev": only an ABSENT flag may be inferred from the repo.
    sp.add_argument(
        "--base-branch", default=None,
        help="base branch for PRs (default: inferred from merged PRs, then the "
             "repo default branch, then 'dev')",
    )
    sp.add_argument("--test-cmd", default="", help="TEST_CMD (arms the PR/red-fail gate); omit to auto-discover")
    sp.add_argument(
        "--lint-cmd", default="",
        help="LINT_CMD (blocks a commit on failure); omit to auto-discover, which "
             "requires confirmation and a passing trial run before it is written",
    )
    sp.add_argument(
        "--typecheck-cmd", default="",
        help="TYPECHECK_CMD (blocks a commit on failure); omit to auto-discover, "
             "which requires confirmation and a passing trial run before it is written",
    )
    sp.add_argument(
        "--no-verify-test-cmd", action="store_true",
        help="skip the one trial run of TEST_CMD and of any confirmed "
             "LINT_CMD/TYPECHECK_CMD (those runs catch watch-mode and missing "
             "binaries before they deny every push or block every commit)",
    )
    sp.add_argument("--no-init", action="store_true", help="don't auto-run init on first start; error instead")
    sp.add_argument("--edit-check-cmd", default="", help="EDIT_CHECK_CMD (post-edit typecheck)")
    sp.add_argument("--review-mode", default="", choices=["", "single", "multi", "delegated"], help="reviewer strategy")
    sp.add_argument("--max-budget", default="", help="MAX_BUDGET_USD per fix session")
    sp.add_argument("--review-budget", default="", help="REVIEW_BUDGET_USD per review")
    sp.add_argument("--max-ticks", type=int, default=None, help="bound each daemon's loop (dry run/CI)")
    sp.add_argument("--write-config", action="store_true", help="regenerate the overlay from flags (clobber)")
    sp.add_argument("--worktree", action="store_true",
                    help="run against an isolated git worktree (detached at base), leaving the primary checkout untouched")
    sp.add_argument("--worktree-path", default="",
                    help="explicit path for the harness worktree (default: sibling <repo>-harness); implies --worktree")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="stop the watchers for this repo")
    add_context_flags(sp)
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("status", help="show watcher state + recent logs for this repo")
    add_context_flags(sp)
    sp.add_argument("-n", "--lines", type=int, default=15, help="log tail length (default: 15)")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "dashboard",
        help="all repos, all issues: done / in flight / daemon health (read-only)",
        description="Aggregate every deployment's journals into one operator "
        "view — per-issue stage, spend, legs and drill-down, per-(role,repo) "
        "daemon health, and the efficiency/guardrail metrics. Journal-only "
        "truth: no GitHub calls, so a label or merge applied by a human outside "
        "the harness is not visible here. Serves the interactive dashboard by "
        f"default on 127.0.0.1:{DASHBOARD_PORT}; --text prints the one-shot "
        "report; --html writes a self-contained snapshot.",
    )
    sp.add_argument("--name", dest="names", action="append", default=[],
                    help="restrict to this overlay (repeatable; default: every config/repos/*.env)")
    sp.add_argument("--html", default="", help="write a self-contained HTML snapshot to PATH and exit")
    sp.add_argument("--serve", type=int, nargs="?", default=DASHBOARD_PORT, const=DASHBOARD_PORT,
                    metavar="PORT",
                    help=f"serve on 127.0.0.1:PORT (default: {DASHBOARD_PORT}; this is the "
                         "default mode — pass --text for the one-shot text report)")
    sp.add_argument("--text", action="store_true",
                    help="print the text report and exit instead of serving")
    sp.add_argument("--legacy", action="store_true",
                    help="explicitly serve the legacy snapshot renderer instead of the SPA")
    sp.add_argument("--details", action="store_true", help="text mode: also dump each issue's raw journal events")
    sp.add_argument("--stale-minutes", type=int, default=30,
                    help="flag a daemon STALE after this long with no journal event (default: 30)")
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("doctor", help="preflight checks (tooling, identities, repo access)")
    add_context_flags(sp)
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("ready", help="label issue(s) ai-ready (trigger the fixer)")
    sp.add_argument("issues", nargs="+", help="issue number(s)")
    sp.add_argument("--repo", default="", help="owner/name (default: auto-detect)")
    sp.set_defaults(func=cmd_ready)

    sp = sub.add_parser("triage", help="label issue(s) ai-triage (queue for the steward)")
    sp.add_argument("issues", nargs="+", help="issue number(s)")
    sp.add_argument("--repo", default="", help="owner/name (default: auto-detect)")
    sp.set_defaults(func=cmd_triage)

    sp = sub.add_parser("prepare-remote", help=argparse.SUPPRESS)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--name", required=True, type=_deployment_name_arg)
    sp.add_argument("--ensure-auto-merge", action="store_true")
    sp.add_argument("--skip-labels", action="store_true")
    sp.set_defaults(func=cmd_prepare_remote)

    sp = sub.add_parser("publish-base-branch", help=argparse.SUPPRESS)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--name", required=True, type=_deployment_name_arg)
    sp.add_argument("--target-dir", required=True)
    sp.add_argument("--branch", required=True)
    sp.set_defaults(func=cmd_publish_base_branch)

    for family, handler in (("resolve", cmd_resolve_activation), ("review", cmd_review_activation),
                            ("review-execution", cmd_review_execution_activation),
                            ("fix", cmd_fix_activation), ("steward", cmd_steward_activation)):
        sp = sub.add_parser(
            f"{family}-activation",
            help=f"inventory, shadow-compare, rehearse or activate the {family} workflow family",
            description=f"Offline ADR 0015 {family}-family activation for this deployment's "
            "active launch snapshot. inventory/shadow are read-only JSON reports; "
            "rehearse activates an isolated copy; activate requires the reviewed "
            "plan digest and stopped watchers.",
        )
        sp.add_argument("action", choices=("inventory", "shadow", "activate", "rehearse"))
        add_context_flags(sp)
        sp.add_argument("--confirm-plan-digest", default="",
                        help="activate: the plan digest printed by the reviewed shadow report")
        sp.add_argument("--legacy-state-dir", action="append", default=[], metavar="DIR",
                        help="additional older/custom state root to import from (repeatable)")
        sp.add_argument("--copy-to", default="", help="rehearse: new empty directory for the isolated copy")
        if family in {"review-execution", "fix", "steward"}:
            sp.add_argument("--plan-file", default="", help="activate: saved original complete shadow JSON report")
        sp.set_defaults(func=handler)

    sp = sub.add_parser("install-service", help="install the OS service units (launchd/systemd)")
    add_context_flags(sp)
    sp.add_argument(
        "--roles",
        nargs="+",
        choices=ROLES,
        help="deprecated: partial service upgrades are rejected for migration safety",
    )
    sp.set_defaults(func=cmd_install_service)

    sp = sub.add_parser("uninstall-service", help="remove the OS service units")
    add_context_flags(sp)
    sp.add_argument(
        "--all",
        action="store_true",
        help="remove every issue-agent deployment (default: current deployment only)",
    )
    sp.set_defaults(func=cmd_uninstall_service)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        raise SystemExit(args.func(args))
    except KeyboardInterrupt:
        # Ctrl-C during a wizard prompt is an ordinary way to leave; a traceback
        # would read as a crash. 130 is the conventional SIGINT exit.
        print()
        err("aborted.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
