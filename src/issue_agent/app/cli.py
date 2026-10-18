"""`issue-agent` — operator CLI, runnable from inside a target repo.

It detects the repo and working copy from the current directory, synthesizes
(or reuses) the per-repo overlay `config/repos/<name>.env`, and delegates the
launch to `bin/point-at.sh` (compose env, doctor, base branch, labels,
single-instance guard, run the role daemons).

Checkout-resident, not a wheel: the daemon walks `__file__` up to the harness
root for `plugin/` and `config/`; `bin/install.sh` installs this editable.

Subcommands: init | config | start | stop | status | dashboard | doctor | ready |
install-service | uninstall-service (plus prepare-remote and
publish-base-branch, which `bin/point-at.sh` calls).
"""

from __future__ import annotations

import argparse
import getpass
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from ..adapters.env_file import source_env_file
from ..adapters import config_files
from ..adapters.github import FixObservationIncomplete, GhUnavailable, GitHubGateway
from ..adapters.heartbeat import read_heartbeat_time
from ..adapters.operator_effects import build_operator_effects
from ..adapters.signal_log import send_signal
from ..domain.deployment import canonical_deployment_name
from ..domain.labels import Label
from ..domain.repository import canonical_repository, repository_equal
from .config import default_state_dir as config_default_state_dir
from .config import from_env
from . import settings
from .doctor import doctor_findings, identity_findings, parse_unit
from ..domain.identity import account_eq, configured_account

# …/src/issue_agent/app/cli.py → the harness checkout (plugin/ and config/).
HARNESS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
REPOS_DIR = os.path.join(HARNESS_ROOT, "config", "repos")
SHARED_ENV = os.path.join(HARNESS_ROOT, "config", "shared.env")
ROLES = ("fix", "review")
# A heartbeat older than this is a dead daemon. Generous: a long leg emits
# nothing between its start and end lines.
STALE_AFTER_S = 1800.0
# NixOS has no /bin/bash.
_BASH = shutil.which("bash") or "/bin/bash"


def _active_env_path(name: str) -> str:
    """Per-deployment composed snapshot, so concurrent deployments never share one."""
    return os.path.join(HARNESS_ROOT, "config", f".active.{name}.env")


def _bin(script: str) -> str:
    return os.path.join(HARNESS_ROOT, "bin", script)


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


# ── overlay identity (pure apart from reading overlay files) ─────────────────
_SSH_RE = re.compile(r"^git@[^:]+:(?P<slug>.+?)(?:\.git)?$")
_HTTPS_RE = re.compile(r"^https?://[^/]+/(?P<slug>.+?)(?:\.git)?/?$")


def parse_remote_url(url: str) -> str:
    """`owner/name` from an ssh or https git remote URL, or ''."""
    for rx in (_SSH_RE, _HTTPS_RE):
        m = rx.match(url.strip())
        if m:
            return canonical_repository(m.group("slug").strip("/"))
    return ""


# The fields read out of overlay/snapshot files. Cleared from the sourcing
# shell's base environment so only what the FILE assigns is returned (M12).
_OVERLAY_FIELDS = (
    "REPO", "TARGET_DIR", "STATE_DIR", "REVIEW_STATE_DIR", "BASE_BRANCH",
    "DAILY_SPEND_CAP_USD", "GLOBAL_DAILY_SPEND_CAP_USD",
    "FIXER_RUNTIME", "REVIEWER_RUNTIME", "REVIEW_BOT", "REVIEW_BOT_TOKEN",
)


def read_repo_env_field(path: str, field: str) -> str:
    """`field` from a shell env file the way bash resolves it (comments,
    spacing, `${VAR}`), the same technique the launch uses (M12). '' when the
    file or field is absent or bash rejects the file."""
    env = {k: v for k, v in os.environ.items() if k not in _OVERLAY_FIELDS}
    try:
        values = source_env_file(path, env, assigned_only=True, normalize_spacing=True,
                                 timeout_seconds=10)
    except ValueError:
        return ""
    return values.get(field, "") if field in _OVERLAY_FIELDS else ""


def _deployment_name_arg(value: str) -> str:
    # argparse also runs `type` over the "" default: "" means "no name given".
    if not value:
        return ""
    canonical = canonical_deployment_name(value)
    if value != canonical:
        raise argparse.ArgumentTypeError(f"deployment name must be canonical; use {canonical!r}")
    return canonical


def derive_name(repo: str) -> str:
    """Fallback overlay name: `owner-name`, lowercased and filename-safe, so two
    owners' same-named repos never share an overlay (P0R-2)."""
    repo = canonical_repository(repo)
    return canonical_deployment_name("-".join(p for p in repo.split("/") if p) if repo else "")


def resolve_name(repo: str, explicit: str = "", repos_dir: str = REPOS_DIR, target_dir: str = "") -> str:
    """Overlay name for this repo: an explicit name; else the overlay whose
    REPO and TARGET_DIR both match (a known checkout never inherits another
    checkout's overlay); else, with no checkout context, the first REPO match;
    else derived. Warns before synthesizing a second overlay for a checkout
    some overlay already targets."""
    repo = canonical_repository(repo)
    if explicit:
        canonical = canonical_deployment_name(explicit)
        if explicit != canonical:
            raise ValueError(f"deployment name must be canonical; use {canonical!r}")
        return canonical
    if not repo or not os.path.isdir(repos_dir):
        return derive_name(repo)
    envs = [fn for fn in sorted(os.listdir(repos_dir)) if fn.endswith(".env")]
    want = os.path.realpath(target_dir) if target_dir else ""

    def field(fn: str, key: str) -> str:
        return read_repo_env_field(os.path.join(repos_dir, fn), key)

    def targets_here(fn: str) -> bool:
        td = field(fn, "TARGET_DIR")
        return bool(td) and os.path.realpath(td) == want

    matches = [fn for fn in envs if repository_equal(field(fn, "REPO"), repo)]
    if not want and matches:
        return matches[0][: -len(".env")]
    for fn in matches:
        if targets_here(fn):
            return fn[: -len(".env")]
    if matches:
        warn(f"overlay(s) for {repo!r} ({', '.join(fn[:-4] for fn in matches)}) target a different "
             f"working copy than {target_dir!r}; refusing implicit reuse. Pass --name.")
    for fn in envs:
        if want and targets_here(fn):
            warn(f"config/repos/{fn} already targets {target_dir} but its REPO is not {repo!r}; "
                 f"about to synthesize a NEW overlay. To reuse it, pass --name {fn[:-4]}.")
            break
    return derive_name(repo)


def check_overlay_mismatch(name: str, repo: str, target_dir: str, repos_dir: str | None = None) -> str:
    """'' when overlay `name` agrees with the detected repo and checkout (or does
    not exist yet), else both sides named. Repository and checkout are both
    deployment identity (P0R-2)."""
    overlay = os.path.join(repos_dir if repos_dir is not None else REPOS_DIR, f"{name}.env")
    if not os.path.isfile(overlay):
        return ""
    base = os.path.basename(overlay)
    o_repo = read_repo_env_field(overlay, "REPO").strip()
    o_target = read_repo_env_field(overlay, "TARGET_DIR").strip()
    repo, target_dir = (repo or "").strip(), (target_dir or "").strip()
    if repo and not o_repo:
        return (f"config/repos/{base} does not record REPO" + (f" (TARGET_DIR={o_target!r})" if o_target else "")
                + f"; this checkout/flags detected REPO={repo!r}" + (f" at {target_dir!r}" if target_dir else ""))
    if o_repo and repo and not repository_equal(o_repo, repo):
        return f"config/repos/{base} is REPO={o_repo!r}; this checkout/flags detected REPO={repo!r}"
    if target_dir and not o_target:
        return f"config/repos/{base} does not record TARGET_DIR; this checkout is {target_dir!r}"
    if o_target and target_dir and os.path.realpath(o_target) != os.path.realpath(target_dir):
        return f"config/repos/{base} is TARGET_DIR={o_target!r}; this checkout is {target_dir!r}"
    return ""


def _q(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def synth_repo_env(
    *, repo: str, target_dir: str, name: str, base_branch: str = "dev",
    base_branch_note: str = "", base_branch_suggestion: str = "",
    max_budget: str = "", review_budget: str = "",
) -> str:
    """A config/repos/<name>.env overlay with per-name state dirs."""
    lines = [
        f"# Per-repo overlay — {canonical_repository(repo)}. AUTO-GENERATED by `issue-agent start`.",
        "# Overlaid on config/shared.env. Safe to edit; --write-config regenerates it.",
        "",
        f"REPO={_q(canonical_repository(repo))}",
        f"TARGET_DIR={_q(target_dir)}",
    ]
    if base_branch_note:
        lines.append(f"# BASE_BRANCH inferred from {base_branch_note}.")
    if base_branch_suggestion:
        lines.append(f"# detected merge target of recent PRs: {base_branch_suggestion} — set "
                     "BASE_BRANCH explicitly to adopt it.")
    lines += [
        f"BASE_BRANCH={_q(base_branch)}",
        "",
        f'STATE_DIR="$HOME/.local/state/issue-agent-{name}"',
        f'REVIEW_STATE_DIR="$HOME/.local/state/review-agent-{name}"',
    ]
    flags = [(k, v) for k, v in (("MAX_BUDGET_USD", max_budget), ("REVIEW_BUDGET_USD", review_budget)) if v]
    if flags:
        lines += ["", "# From `issue-agent start` flags."] + [f"{k}={_q(v)}" for k, v in flags]
    return "\n".join(lines) + "\n"


# ── git/gh detection ─────────────────────────────────────────────────────────
def _run(cmd: list[str], cwd: str | None = None) -> str:
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def detect_target_dir(cwd: str) -> str:
    return _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)


def detect_repo(cwd: str) -> str:
    """`owner/name` via gh (handles renames), else the origin remote URL."""
    slug = _run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], cwd=cwd)
    return canonical_repository(slug) if slug else parse_remote_url(
        _run(["git", "remote", "get-url", "origin"], cwd=cwd))


def detect_base_branch(target_dir: str, repo: str = "", sample: int = 20) -> tuple[str, str]:
    """(branch, provenance): the majority base of recent merged PRs, else the
    repository default, each confirmed to exist on the remote; else ('dev', '')."""
    try:
        entries = json.loads(_run(["gh", "pr", "list", "--state", "merged", "--limit", str(sample),
                                   "--json", "baseRefName"], cwd=target_dir) or "[]")
    except ValueError:
        entries = []
    bases = [str(e.get("baseRefName") or "") for e in entries if isinstance(e, dict)] \
        if isinstance(entries, list) else []
    bases = [b for b in bases if b]
    candidates = []
    if bases:
        branch, hits = Counter(bases).most_common(1)[0]
        candidates.append((branch, f"{hits}/{len(bases)} of the last merged PRs"))
    default = _run(["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
                   cwd=target_dir)
    if default:
        candidates.append((default, "the repository default branch"))
    for branch, note in candidates:
        if repo and _run(["gh", "api", f"repos/{repo}/branches/{branch}", "--jq", ".name"], cwd=target_dir):
            return branch, note
    return "dev", ""


# ── identity prompts (init) ──────────────────────────────────────────────────
# Ceiling on every operator re-prompt loop (ADR 0012).
_PROMPT_ATTEMPTS = 3


def _gh_login(env: dict[str, str] | None = None) -> tuple[str, bool]:
    """(login, gh_answered). Not answered only when gh could not run at all;
    a non-zero exit is GitHub's verdict."""
    try:
        proc = subprocess.run(["gh", "api", "user", "--jq", ".login"], env=env,
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "", False
    first = (proc.stdout or "").strip().splitlines()[:1]
    login = first[0].strip() if first else ""
    valid = proc.returncode == 0 and login != "null" and configured_account(login) is not None
    return (login if valid else ""), True


def detect_fixer_login() -> str:
    return _gh_login()[0]


def gh_login_for_token(token: str) -> tuple[str, bool]:
    """The login `token` authenticates as; the token travels only in the child env."""
    return _gh_login(dict(os.environ, GH_TOKEN=token, GITHUB_TOKEN=token)) if token else ("", True)


def read_token_input(raw: str) -> tuple[str, str]:
    """A pasted secret, or `@path`/an existing path holding it. (token, problem);
    a problem never repeats what was typed."""
    value = raw.strip()
    from_file = value.startswith("@")
    value = value[1:].strip() if from_file else value
    path = os.path.expanduser(value) if value else ""
    if not (from_file or (path and os.path.isfile(path))):
        return value, ""
    if not path or not os.path.isfile(path):
        return "", "no file exists at the path you entered — re-enter the token, or @<path> to a readable file."
    try:
        with open(path) as handle:
            return handle.read().strip(), ""
    except OSError:
        return "", "that token file could not be read — re-enter the token, or @<path> to a readable file."


def prompt_token(var: str, expected_login: str = "") -> str:
    """Ask for `var` and prove it before storing it. A token GitHub rejected,
    or one belonging to an account other than `expected_login`, is never
    stored: it would silently install that account as the identity. A token
    gh could not be asked about is kept unverified, loudly; doctor re-checks."""
    for attempt in range(1, _PROMPT_ATTEMPTS + 1):
        try:
            raw = getpass.getpass(f"  {var} (paste, or @path to a file; Enter to skip): ").strip()
        except EOFError:
            raw = ""
        if not raw:
            warn(f"no {var} stored — `issue-agent doctor` reports what that means.")
            return ""
        token, problem = read_token_input(raw)
        if problem:
            warn(problem)
            continue
        login, answered = gh_login_for_token(token)
        if not answered:
            warn(f"could not run `gh` to validate {var} — storing it UNVERIFIED; doctor must confirm it.")
            return token
        if not login:
            warn("GitHub rejected that token (expired, revoked, or lacking read:user). It will NOT be stored.")
        elif expected_login and not account_eq(login, expected_login):
            warn(f"that token belongs to '{login}', not '{expected_login}' — the role refuses to start "
                 "on another account's credential. It will NOT be stored.")
        else:
            ok(f"{var} authenticates as '{login}'")
            return token
        if attempt < _PROMPT_ATTEMPTS:
            info(f"try again ({_PROMPT_ATTEMPTS - attempt} attempt(s) left).")
    warn(f"no {var} stored after {_PROMPT_ATTEMPTS} attempts.")
    return ""


def detect_claude_auth() -> bool:
    return os.path.isfile(os.path.expanduser("~/.claude/.credentials.json")) or bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def detect_codex_auth() -> str:
    """'chatgpt', 'apikey' or '' — only the auth MODE is read from auth.json."""
    try:
        with open(os.path.expanduser("~/.codex/auth.json")) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    mode = str(data.get("auth_mode") or "").strip().lower()
    return mode if mode in ("chatgpt", "apikey") else ("apikey" if data.get("OPENAI_API_KEY") else "")


def _ask(prompt: str, default: str = "") -> str:
    """One question; EOF takes the default."""
    try:
        value = input(f"  {prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        value = ""
    return value or default


def cmd_init(args) -> int:
    """Interactive first run: write config/shared.env (identities, runtimes)."""
    if os.path.isfile(SHARED_ENV) and not args.force:
        err("config/shared.env already exists — refusing to clobber. Edit it, or re-run with --force.")
        return 2
    if not sys.stdin.isatty():
        err("`issue-agent init` is interactive; run it in a terminal (or copy "
            "config/shared.example.env → config/shared.env by hand).")
        return 2
    print(_c("36", "▸ issue-agent init — creating config/shared.env (global identity + secrets)"))
    print("  Per-repo knobs (REPO/TARGET_DIR/BASE_BRANCH) are written by `issue-agent start`.\n")
    fields: list[tuple[str, str]] = []
    gh_login = detect_fixer_login()
    if gh_login:
        ok(f"gh is authenticated as '{gh_login}'")
    else:
        warn("`gh api user` did not answer; the fixer login cannot be pre-filled.")
    fix_bot = _ask("Fixer GitHub login", gh_login)
    review_bot = ""
    for attempt in range(1, _PROMPT_ATTEMPTS + 1):
        review_bot = _ask("Reviewer login (distinct from the fixer)")
        if not review_bot or not account_eq(review_bot, fix_bot):
            break
        warn(f"'{review_bot}' is also the fixer — GitHub 422s a review by the PR's author."
             + (" Keeping it; `issue-agent doctor` will flag it." if attempt == _PROMPT_ATTEMPTS else ""))
    fields += [("FIX_BOT", fix_bot), ("REVIEW_BOT", review_bot)]
    if _ask("Configure a GitHub App for the reviewer? (y/N)", "N").lower().startswith("y"):
        fields += [("GH_APP_ID", _ask("GH App ID")), ("GH_APP_INSTALL_ID", _ask("GH App installation ID")),
                   ("GH_APP_KEY_FILE", _ask("GH App private-key .pem path"))]
    else:
        fields.append(("REVIEW_BOT_TOKEN", prompt_token("REVIEW_BOT_TOKEN", review_bot)))
    # The App covers review only. The fix role refuses to start without
    # FIX_BOT_TOKEN or when it is not FIX_BOT's (app/identity.py).
    fields.append(("FIX_BOT_TOKEN", prompt_token("FIX_BOT_TOKEN", fix_bot)))
    # The same identity findings doctor renders, at config time.
    for finding in identity_findings({k: v for k, v in fields if v}):
        (err if finding.level == "error" else warn)(finding.message)
    claude_ready, codex_mode = detect_claude_auth(), detect_codex_auth()
    codex_path = shutil.which("codex") or ""
    if not claude_ready:
        warn("claude: no credentials — run `claude` once, or export ANTHROPIC_API_KEY, before selecting it.")
    fields.append(("FIXER_RUNTIME", _ask("Fixer runtime (claude|codex)",
                                         "claude" if claude_ready or not codex_path else "codex")))
    fields.append(("REVIEWER_RUNTIME", _ask("Reviewer runtime (codex|claude)", "codex" if codex_path else "claude")))
    if any(k.endswith("_RUNTIME") and v == "codex" for k, v in fields):
        # Services run a minimal PATH; an unpinned codex may resolve elsewhere.
        if codex_path and _ask(f"Pin CODEX_BIN={codex_path}? (Y/n)", "Y").lower().startswith("y"):
            fields.append(("CODEX_BIN", codex_path))
        elif codex_path:
            warn("CODEX_BIN left unpinned — the daemons' PATH decides which codex runs; doctor re-checks it.")
        if codex_mode == "chatgpt":
            info("codex on ChatGPT auth reports no billed cost; its legs are priced from the "
                 "CODEX_COST_PER_MTOK rate card.")
        else:
            fields.append(("OPENAI_BASE_URL", _ask("Gateway base URL (OpenAI-compatible)",
                                                   "https://api.openai.com/v1")))
    lines = ["# Autonomous issue agent — SHARED config. AUTO-GENERATED by `issue-agent init`.", ""]
    lines += [f"{k}={_q(v)}" for k, v in fields if v]
    os.makedirs(os.path.dirname(SHARED_ENV), exist_ok=True)
    # May hold a PAT: tighten an existing file BEFORE writing (O_CREAT's mode
    # applies only to a new file).
    if os.path.exists(SHARED_ENV):
        os.chmod(SHARED_ENV, 0o600)
    with os.fdopen(os.open(SHARED_ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(SHARED_ENV, 0o600)
    ok(f"wrote {SHARED_ENV}")
    print("  Review it, then `cd <target-repo> && issue-agent start`.")
    return 0


# ── isolated harness worktree (start --worktree) ─────────────────────────────
def harness_worktree_path(target_dir: str, explicit: str = "") -> str:
    """Explicit path, else a sibling `<basename>-harness` outside the repo."""
    if explicit:
        return os.path.abspath(explicit)
    tgt = os.path.abspath(target_dir)
    return os.path.join(os.path.dirname(tgt), f"{os.path.basename(tgt)}-harness")


def ensure_harness_worktree(target_dir: str, base_branch: str, explicit: str = "") -> str:
    """A worktree of the target DETACHED at the base tip, so the primary
    checkout is untouched; reused when already registered. '' on failure."""
    wt = harness_worktree_path(target_dir, explicit)
    listing = _run(["git", "worktree", "list", "--porcelain"], cwd=target_dir)
    registered = {os.path.realpath(line[9:].strip()) for line in listing.splitlines()
                  if line.startswith("worktree ")}
    if os.path.isdir(wt):
        if os.path.realpath(wt) in registered:
            return wt
        if os.listdir(wt):
            err(f"{wt} exists and is not a worktree of this repo — pass --worktree-path.")
            return ""
    _run(["git", "fetch", "origin", base_branch], cwd=target_dir)
    ref = f"origin/{base_branch}"
    if not _run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=target_dir):
        ref = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=target_dir) or "HEAD"
    r = subprocess.run(["git", "worktree", "add", "--detach", wt, ref], cwd=target_dir,
                       capture_output=True, text=True)
    if r.returncode != 0:
        err(f"git worktree add failed: {(r.stderr or '').strip()}")
        return ""
    return wt


# ── process and heartbeat liveness (stop/status) ─────────────────────────────
def _pgrep(pattern: str) -> list[int]:
    return [int(p) for p in _run(["pgrep", "-f", pattern]).split() if p.isdigit()]


def _ps(pid: int, fmt: tuple[str, ...]) -> str:
    """macOS has no /proc."""
    if sys.platform != "darwin":
        return ""
    done = subprocess.run(("ps", *fmt, "-p", str(pid), "-o", "command="), capture_output=True,
                          text=True, timeout=5)
    return done.stdout if done.returncode == 0 else ""


def _pid_argv(pid: int) -> tuple[str, ...]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return tuple(i.decode(errors="replace") for i in handle.read().split(b"\0") if i)
    except OSError:
        try:
            return tuple(shlex.split(_ps(pid, ())))
        except ValueError:
            return ()


def _launcher_pids(name: str) -> list[int]:
    """point-at.sh launchers whose deployment argument is exactly `name`."""
    out = []
    for pid in _pgrep("point-at.sh"):
        argv = _pid_argv(pid)
        if argv[:1] and os.path.basename(argv[0]) in {"bash", "sh"}:
            argv = argv[1:]
        if len(argv) >= 2 and os.path.basename(argv[0]) == "point-at.sh" and argv[1] == name:
            out.append(pid)
    return out


def _pid_deployment_name(pid: int) -> str:
    """The deployment a daemon exports (ISSUE_AGENT_DEPLOYMENT_ID, else AGENT_ENV)."""
    variables: dict[str, str] = {}
    try:
        with open(f"/proc/{pid}/environ", "rb") as handle:
            variables = {k.decode(errors="replace"): v.decode(errors="replace")
                         for item in handle.read().split(b"\0") if b"=" in item
                         for k, v in (item.split(b"=", 1),)}
    except OSError:
        text = _ps(pid, ("eww",))
        for var in ("ISSUE_AGENT_DEPLOYMENT_ID", "AGENT_ENV"):
            m = re.search(rf"(?:^|\s){var}=([^\s]+)(?:\s|$)", text)
            if m:
                variables[var] = m.group(1)
    if variables.get("ISSUE_AGENT_DEPLOYMENT_ID"):
        return variables["ISSUE_AGENT_DEPLOYMENT_ID"]
    active = os.path.basename(variables.get("AGENT_ENV", ""))
    return active[len(".active."):-len(".env")] if active.startswith(".active.") and active.endswith(".env") else ""


def _role_pids(name: str) -> dict[str, list[int]]:
    return {
        role: [pid for pid in sorted(set(_pgrep(f"issue_agent.app.main {role}"))
                                     | set(_pgrep(f"issue_agent.app.launchd_main {role}")))
               if _pid_deployment_name(pid) == name]
        for role in ROLES
    }


def _fresh_role_heartbeats(name: str, *, now_fn=None, stale_after_s: float = STALE_AFTER_S) -> dict[str, datetime]:
    """Roles with a fresh daemon heartbeat. Installed services may live in
    another PID namespace, so an empty pgrep is not proof a role is stopped;
    the bounded age keeps a dead daemon from looking alive."""
    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    result = {}
    for role, journal in zip(ROLES, journal_paths(name)):
        stamp = read_heartbeat_time(os.path.join(os.path.dirname(journal), "heartbeat.json"))
        if stamp is not None and 0 <= (now - stamp).total_seconds() <= stale_after_s:
            result[role] = stamp
    return result


def _resolve_context(args) -> tuple[str, str, str]:
    """(repo, target_dir, name) from flags, then cwd detection. An explicit
    target is the repository authority."""
    target_dir = args.target_dir or detect_target_dir(os.getcwd())
    target_dir = os.path.realpath(target_dir) if target_dir else ""
    repo = canonical_repository(args.repo or detect_repo(target_dir or os.getcwd()))
    return repo, target_dir, resolve_name(repo, getattr(args, "name", "") or "", target_dir=target_dir)


def _refuse_mismatch(verb: str, name: str, repo: str, target_dir: str) -> bool:
    """P0R-2: a mutating command never acts on another deployment's overlay."""
    mismatch = check_overlay_mismatch(name, repo, target_dir)
    if mismatch:
        err(f"REFUSING to {verb} '{name}' — wrong repo/checkout:")
        err(f"  {mismatch}")
        err("Pass --name <other> for the overlay that matches this checkout.")
    return bool(mismatch)


def _systemd_predecessor_matches(path: str, env: dict[str, str], name: str) -> bool:
    """Mirror install-systemd.sh's legacy names and target ownership guard."""
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    target = read_repo_env_field(overlay, "TARGET_DIR")
    if not target or (env.get("TARGET_DIR") and env["TARGET_DIR"] != target):
        return False
    repo = read_repo_env_field(overlay, "REPO")
    suffixes = ("", f"-{canonical_deployment_name(os.path.basename(target))}",
                f"-{derive_name(repo)}" if repo else "")
    return os.path.basename(path) in {
        f"issue-agent-{role}-watcher{suffix}.service" for role in ROLES for suffix in suffixes
    }


def _stop_installed_services(name: str) -> bool:
    """Discover installed writers as the installers do; supervisor stops own ADR 0005."""
    if sys.platform == "darwin":
        directory, suffix = os.path.expanduser("~/Library/LaunchAgents"), ".plist"
        prefixes = [f"dev.issue-agent.{role}-watcher" for role in ROLES]
        current_suffix = f".{name}{suffix}"
    elif sys.platform.startswith("linux"):
        directory, suffix = os.path.expanduser("~/.config/systemd/user"), ".service"
        prefixes = [f"issue-agent-{role}-watcher" for role in ROLES]
        current_suffix = f"-{name}{suffix}"
    else:
        return False
    installed = []
    for prefix in prefixes:
        for path in sorted(glob.glob(os.path.join(directory, f"{prefix}*{suffix}"))):
            if not os.path.isfile(path):
                continue
            parsed = parse_unit(path)
            env = parsed[0] if parsed is not None else {}
            identity = env.get("ISSUE_AGENT_DEPLOYMENT_ID")
            if (os.path.basename(path) == f"{prefix}{current_suffix}" or identity == name
                or (suffix == ".service" and parsed is not None and not identity
                    and _systemd_predecessor_matches(path, env, name))):
                installed.append(path)
    if sys.platform == "darwin":
        for path in installed:
            label = os.path.basename(path).removesuffix(".plist")
            if subprocess.run(["launchctl", "list", label], capture_output=True,
                              timeout=90).returncode == 0:
                subprocess.run(["launchctl", "unload", path], check=True, timeout=90)
    elif installed:
        subprocess.run(["systemctl", "--user", "stop",
                        *(os.path.basename(path) for path in installed)], check=True, timeout=90)
    return bool(installed)


def _stop_deployment(name: str) -> int:
    """Stop installed services and foreground processes, verified within 90s."""
    launchers, roles = _launcher_pids(name), _role_pids(name)
    pids = sorted(set(launchers) | {p for g in roles.values() for p in g})
    try:
        installed = _stop_installed_services(name)
    except (OSError, subprocess.SubprocessError) as exc:
        err(f"failed to stop services for '{name}': {exc}")
        return 1
    if not pids and not installed:
        info(f"no watchers running for '{name}'.")
        return 0
    # Installed definitions can coexist with a foreground run after a service stop.
    foreground = (sorted(set(_launcher_pids(name)) | {p for g in _role_pids(name).values() for p in g})
                  if installed else pids)
    pids = sorted(set(pids) | set(foreground))
    info(f"stopping watchers for '{name}' (pids: {' '.join(map(str, pids))})")
    for pid in foreground:
        try:
            send_signal(signal.SIGTERM, pid=pid, reason=f"operator_stop:{name}")
        except ProcessLookupError:
            pass
    deadline, remaining = time.monotonic() + 90, set(pids)
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
        err(f"watchers did not stop within 90s (still alive: {' '.join(map(str, sorted(remaining)))})")
        return 1
    ok("watchers stopped and verified")
    return 0


# ── install drift (#156, #187) ───────────────────────────────────────────────
_TRANSIENT_CHECKOUT_MARKER = f"{os.sep}.claude{os.sep}worktrees{os.sep}"


def declared_console_scripts() -> tuple[str, ...]:
    """`[project.scripts]` — the one declaration bin/install.sh also reads —
    else the installed distribution's console scripts."""
    try:
        import tomllib

        with open(os.path.join(HARNESS_ROOT, "pyproject.toml"), "rb") as stream:
            table = (tomllib.load(stream).get("project") or {}).get("scripts") or {}
    except (ImportError, OSError, ValueError):
        table = {}
    names = tuple(str(k) for k in table if str(k).strip())
    if names:
        return names
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        return tuple(e.name for e in distribution("issue-agent").entry_points if e.group == "console_scripts")
    except PackageNotFoundError:
        return ()


def running_from_transient_checkout() -> bool:
    return _TRANSIENT_CHECKOUT_MARKER in HARNESS_ROOT + os.sep


def entrypoint_drift() -> str:
    """Why the installed entry points do not run this checkout, '' when coherent
    (or never installed). A `make install` inside an agent worktree bakes that
    transient tree into the launcher (#187)."""
    scripts = declared_console_scripts()
    if "issue-agent" not in scripts or not shutil.which("issue-agent"):
        return ""
    if running_from_transient_checkout():
        return (f"the installed issue-agent runs {HARNESS_ROOT}, a transient agent worktree; "
                "run `make install` from the canonical checkout")
    for script in scripts:
        found = shutil.which(script)
        entry = os.path.join(HARNESS_ROOT, ".venv", "bin", script)
        if not found:
            return f"{script} is not on PATH though issue-agent is — stale/partial install"
        ours = os.path.realpath(found) == os.path.realpath(entry)
        try:
            if not ours:  # the generated shim (bin/install.sh) names the venv binary
                with open(found, encoding="utf-8", errors="replace") as stream:
                    ours = entry in stream.read(8192)
        except OSError:
            pass
        if not ours:
            return f"{script} on PATH resolves to {found}, not this checkout's .venv/bin/{script}"
    return ""


def live_deployment_names() -> tuple[str, ...]:
    """Deployments with a launcher, role process or fresh heartbeat."""
    try:
        names = [e[:-4] for e in sorted(os.listdir(REPOS_DIR)) if e.endswith(".env")]
    except OSError:
        return ()
    return tuple(n for n in names if _launcher_pids(n) or any(_role_pids(n).values()) or _fresh_role_heartbeats(n))


def heal_entrypoint_drift() -> bool:
    """Run `make install` once when the entry points drifted and nothing live
    executes from the shared .venv (`uv sync` prunes it mid-tick)."""
    drift = entrypoint_drift()
    if not drift:
        return True
    live = () if running_from_transient_checkout() else live_deployment_names()
    if running_from_transient_checkout() or live:
        why = (f"deployment(s) {', '.join(live)} are live and `make install` runs `uv sync` on their .venv"
               if live
               else "`make install` here would reinstall the transient worktree")
        err(f"stale install detected ({drift}); NOT auto-repairing: {why}.")
        return False
    info(f"stale/partial install detected ({drift}) — running `make install` once")
    try:
        result = subprocess.run(["make", "install"], cwd=HARNESS_ROOT, capture_output=True,
                                text=True, timeout=300)
        detail = result.stderr or result.stdout or ""
        failed = f"exited {result.returncode}" if result.returncode else ""
    except subprocess.TimeoutExpired as expired:
        out = expired.stderr or expired.stdout or ""
        detail, failed = out.decode(errors="replace") if isinstance(out, bytes) else out, "exceeded 300s"
    if not failed and entrypoint_drift():
        failed = f"succeeded but the install is still drifted ({entrypoint_drift()})"
    if failed:
        err(f"auto-repair failed: `make install` {failed}")
        for line in detail.strip().splitlines()[-5:]:
            err(f"  {line}")
        return False
    ok(f"auto-fixed stale/partial install ({drift})")
    return True


# ── start / stop ─────────────────────────────────────────────────────────────
def cmd_start(args) -> int:
    repo, target_dir, name = _resolve_context(args)
    if not repo:
        err("could not detect the repo (no git repo, or no gh/origin remote). Pass --repo owner/name.")
        return 2
    if not target_dir:
        err(f"could not detect a git working copy for {repo}. Pass --target-dir.")
        return 2
    if args.worktree or args.worktree_path:
        wt = ensure_harness_worktree(target_dir, args.base_branch or "dev", args.worktree_path)
        if not wt:
            return 2
        target_dir = wt
        name = name if args.name else f"{name}-wt"
        ok(f"worktree ready: {wt}  (overlay '{name}')")
    if not os.path.isfile(SHARED_ENV):
        if args.no_init or not sys.stdin.isatty():
            err("config/shared.env is missing. Run `issue-agent init` once, then retry.")
            return 2
        info("first run — no config/shared.env yet; running `init`.")
        rc = cmd_init(argparse.Namespace(force=False))
        if rc != 0 or not os.path.isfile(SHARED_ENV):
            return rc or 2
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    exists = os.path.isfile(overlay)
    if exists and not args.write_config:
        if _refuse_mismatch("reuse the overlay of", name, repo, target_dir):
            err(f"(or pass --write-config to re-synthesize config/repos/{name}.env for this checkout)")
            return 2
        ok(f"reusing config/repos/{name}.env  (pass --write-config to regenerate)")
    else:
        # An explicit flag wins; a regenerate keeps the written merge target;
        # else infer, but adopt an inference only on an operator's TTY answer:
        # unattended, point-at.sh's base-branch hard stop must still fire.
        base, note, suggestion = args.base_branch or "", "", ""
        if not base and exists:
            base = read_repo_env_field(overlay, "BASE_BRANCH")
        if not base:
            inferred, note = detect_base_branch(target_dir, repo)
            if sys.stdin.isatty():
                base = _ask("Base branch for PRs (approved PRs merge into it)", inferred)
                note = note if base == inferred else "an operator answer at `issue-agent start`"
            else:
                base, suggestion, note = "dev", (inferred if inferred != "dev" else ""), ""
                if suggestion:
                    info(f"recent PRs merge into '{suggestion}' — keeping BASE_BRANCH=dev; "
                         "pass --base-branch to adopt it.")
        content = synth_repo_env(repo=repo, target_dir=target_dir, name=name, base_branch=base,
                                 base_branch_note=note, base_branch_suggestion=suggestion,
                                 max_budget=args.max_budget, review_budget=args.review_budget)
        if exists and _stop_deployment(name) != 0:
            err(f"REFUSING to rewrite config/repos/{name}.env while its deployment is still running")
            return 1
        os.makedirs(REPOS_DIR, exist_ok=True)
        with open(overlay, "w") as f:
            f.write(content)
        ok(f"wrote config/repos/{name}.env  (repo={repo}, target={target_dir})")
    env = dict(os.environ)
    if args.roles:
        env["ROLES"] = " ".join(args.roles)
    if args.max_ticks is not None:
        env["ISSUE_AGENT_MAX_TICKS"] = str(args.max_ticks)
    if not heal_entrypoint_drift() and running_from_transient_checkout():
        err("refusing to launch from a transient worktree checkout — run `make install` and "
            "`issue-agent start` from the canonical checkout.")
        return 1
    info(f"delegating to bin/point-at.sh {name}")
    os.execve(_BASH, [_BASH, _bin("point-at.sh"), name], env)


def cmd_stop(args) -> int:
    repo, target_dir, name = _resolve_context(args)
    if _refuse_mismatch("stop", name, repo, target_dir):
        return 2
    return _stop_deployment(name)


# ── status ───────────────────────────────────────────────────────────────────
def deployment_env_field(name: str, field: str) -> str:
    """One composed field the way the daemons resolved it: the launch snapshot
    first, then the overlay, per field."""
    for path in (_active_env_path(name), os.path.join(REPOS_DIR, f"{name}.env")):
        value = read_repo_env_field(path, field)
        if value:
            return value
    return ""


def journal_paths(name: str) -> list[str]:
    """The fix and review journals of `name`: each role's composed state dir,
    else the daemon's own default for REPO, else the synthesized layout."""
    repo = deployment_env_field(name, "REPO")
    paths = []
    for key, dirname in (("STATE_DIR", "issue-agent"), ("REVIEW_STATE_DIR", "review-agent")):
        raw = deployment_env_field(name, key)
        state = (os.path.expandvars(os.path.expanduser(raw)) if raw
                 else config_default_state_dir(repo, dirname) if repo
                 else os.path.expanduser(f"~/.local/state/{dirname}-{name}"))
        paths.append(os.path.join(state, "journal.jsonl"))
    return paths


def _env_cap(name: str, key: str, default: float) -> float:
    """A cap as status reports it: overlay over shared; garbled reads as 0."""
    for path in (os.path.join(REPOS_DIR, f"{name}.env"), SHARED_ENV):
        raw = read_repo_env_field(path, key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                return 0.0
    return default


def daily_cap(name: str) -> float:
    return _env_cap(name, "DAILY_SPEND_CAP_USD", 700.0)


def global_daily_cap(name: str) -> float:
    return _env_cap(name, "GLOBAL_DAILY_SPEND_CAP_USD", 0.0)


def log_dir() -> str:
    """Where point-at.sh writes `<role>-<name>.log`."""
    return os.environ.get("LOG_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "issue-agent", "logs")


_STATUS_COMMAND_TIMEOUT_S = 2.0


def _systemd_status_log(name: str, role: str, lines: int) -> tuple[str, str] | None:
    """(source, tail) from an installed, live Linux user unit's journal; None
    when no such unit is active. A failed query is reported, never replaced
    by a stale foreground log."""
    if not sys.platform.startswith("linux"):
        return None
    unit = f"issue-agent-{role}-watcher-{name}.service"

    def query(label: str, command: list[str]) -> tuple[str, tuple[str, str] | None]:
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=_STATUS_COMMAND_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            return "", (f"{label} unavailable: {unit}", f"  {command[0]} failed: {exc}")
        if done.returncode != 0:
            detail = (done.stderr or done.stdout).strip() or f"{command[0]} exited {done.returncode}"
            return "", (f"{label} unavailable: {unit}", f"  {detail}")
        return done.stdout, None

    state, failure = query("systemd status", ["systemctl", "--user", "show", unit,
                                              "--property=LoadState", "--property=ActiveState"])
    if failure:
        return failure
    props = dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
    if props.get("LoadState") != "loaded" or props.get("ActiveState") not in {
            "active", "activating", "reloading", "deactivating", "failed"}:
        return None
    journal, failure = query("systemd journal", ["journalctl", "--user", "--unit", unit, "--lines",
                                                 str(lines), "--no-pager", "--output=cat"])
    return failure or (f"systemd journal: {unit}", journal.strip())


def spend_status_lines(cap: float = 0.0, repo: str | None = None, global_cap: float = 0.0, *,
                       repository_spent: float = 0.0, all_repositories_spent: float = 0.0) -> list[str]:
    """Today's settled spend against the caps; [] when nothing was spent."""
    if not all_repositories_spent:
        return []

    def bound(value: float, what: str) -> str:
        return f" / ${value:.2f} cap" if value > 0 else f" (no {what} cap)"

    lines = ["", _c("36", "── spend ──")]
    if repo:
        lines.append(f"  today, {repo}: ${repository_spent:.2f}{bound(cap, 'daily')}")
        lines.append(f"  today, all repos: ${all_repositories_spent:.2f}{bound(global_cap, 'global')}")
    else:
        lines.append(f"  today, all repos: ${all_repositories_spent:.2f}{bound(cap, 'daily')}")
    return lines


def cmd_status(args) -> int:
    repo, target_dir, name = _resolve_context(args)
    # Read-only: a mismatched overlay is shown with a warning, so status can
    # diagnose the collision from either checkout.
    mismatch = check_overlay_mismatch(name, repo, target_dir)
    if mismatch:
        warn(f"config/repos/{name}.env does not match this checkout — showing it anyway: {mismatch}")
    roles, heartbeats, launchers = _role_pids(name), _fresh_role_heartbeats(name), _launcher_pids(name)
    print(_c("36", f"▸ status for '{name}'" + (f"  ({repo})" if repo else "")))
    print(f"  launcher (point-at.sh): {'running ' + str(launchers) if launchers else 'stopped'}")
    for role in ROLES:
        state = (_c("32", "running") + f" {roles[role]}" if roles[role]
                 else _c("32", "running") + " (fresh heartbeat; PID not visible)" if role in heartbeats
                 else _c("33", "stopped"))
        print(f"  {role:8s}: {state}")
    for role in ROLES:
        service = _systemd_status_log(name, role, args.lines)
        if service is not None:
            print(_c("36", f"\n── {service[0]} ──"))
            print(service[1] or "  (empty)")
            # Ownership is per role: a foreground launcher may own this one.
            if not launchers:
                continue
        log = os.path.join(log_dir(), f"{role}-{name}.log")
        if os.path.isfile(log):
            print(_c("36", f"\n── tail {log} ──"))
            print(_run(["tail", "-n", str(args.lines), log]) or "  (empty)")
    # A diagnostic must not be what fails: liveness above stands regardless.
    try:
        from zoneinfo import ZoneInfo

        from ..adapters.control_path import canonical_control_store_path
        from ..adapters.control_store import read_settled_spend
        from ..domain.contracts import daily_budget_scope

        period = daily_budget_scope("*", "global", "USD", datetime.now(timezone.utc), ZoneInfo("UTC"))
        spend = read_settled_spend(canonical_control_store_path(), period.period_start, period.period_end)
        configured = read_repo_env_field(os.path.join(REPOS_DIR, f"{name}.env"), "REPO")
        mine = spend.for_repository(configured) if configured else None
        if spend.total is None or (configured and mine is None):
            raise RuntimeError(spend.detail or "settled spend is unavailable")
        for line in spend_status_lines(daily_cap(name), repo=configured, global_cap=global_daily_cap(name),
                                       repository_spent=mine.micros / 1e6 if mine is not None else 0.0,
                                       all_repositories_spent=spend.total.micros / 1e6):
            print(line)
    except Exception as exc:  # noqa: BLE001
        err(f"(could not read settled spend: {exc})")
    return 0


# ── dashboard ────────────────────────────────────────────────────────────────
def cmd_dashboard(args) -> int:
    """The dashboard is a separate component the core never imports: replace
    this process with its module entry."""
    port = ["--port", str(args.port)] if args.port is not None else []
    argv = [sys.executable, "-m", "issue_agent.app.dashweb.main", *port]
    os.execv(sys.executable, argv)
    return 0  # unreachable: execv replaces the process or raises


def _composed_env(env: dict) -> dict:
    """The env the daemons will see: the snapshot AGENT_ENV names, else the
    inherited one (an unsourceable snapshot is doctor.sh's to report)."""
    if not env.get("AGENT_ENV"):
        return dict(env)
    try:
        return source_env_file(env["AGENT_ENV"], env)
    except ValueError:
        return dict(env)


def cmd_doctor(args) -> int:
    """Compose this repo's env (if detectable), render the offline checks, then
    run bin/doctor.sh. An error finding reddens the exit code: doctor is a gate."""
    drift = entrypoint_drift()
    if drift and running_from_transient_checkout():
        err(f"REFUSING to run doctor — stale/partial install: {drift} (#187)")
        return 2
    if drift:
        err(f"install drift: {drift} — `make install` before trusting anything below.")
    repo, target_dir, name = _resolve_context(args)
    if _refuse_mismatch("run doctor for", name, repo, target_dir):
        return 2
    env = dict(os.environ)
    if os.path.isfile(SHARED_ENV) and os.path.isfile(os.path.join(REPOS_DIR, f"{name}.env")):
        r = subprocess.run([_BASH, _bin("compose-env.sh"), name])
        if r.returncode != 0:
            err(f"compose-env.sh failed for '{name}' — refusing to check a stale or missing snapshot.")
            return r.returncode
        env["AGENT_ENV"] = _active_env_path(name)
    elif os.path.isfile(SHARED_ENV):
        # Before the first start: check shared.env against the detected target.
        env["AGENT_ENV"] = SHARED_ENV
        env.update({k: v for k, v in (("REPO", repo), ("TARGET_DIR", target_dir)) if v})
        warn(f"no overlay config/repos/{name}.env yet — checking config/shared.env; re-run after `start`.")
    errors = bool(drift)
    for section, findings in doctor_findings(name, _composed_env(env), _active_env_path(name)):
        info(section)
        for finding in findings:
            {"error": err, "warn": warn}.get(finding.level, ok)(f"  {finding.message}")
        errors |= any(f.level == "error" for f in findings)
    rc = subprocess.run([_BASH, _bin("doctor.sh")], env=env).returncode
    return 1 if rc == 0 and errors else rc


# ── labels and remote bootstrap ──────────────────────────────────────────────
# The minimal design's two labels: an operator adds ai-ready, policy writes
# needs-human (docs/open/2026-10-12-minimal-harness-design.md).
REMOTE_LABELS = (Label.AI_READY.value, Label.NEEDS_HUMAN.value)


def _operator_effects(repo: str, channel: str):
    return build_operator_effects(repo, identity=f"{repo}:operator:{channel}")


def cmd_ready(args) -> int:
    repo = canonical_repository(args.repo or detect_repo(os.getcwd()))
    if not repo:
        err("could not detect the repo. Pass --repo owner/name.")
        return 2
    effects, rc = _operator_effects(repo, "cli"), 0
    for n in args.issues:
        if not str(n).isdigit():
            err(f"invalid issue number {n!r}")
            rc = 1
            continue
        applied, detail = effects.set_issue_labels(int(n), add=(Label.AI_READY.value,))
        if applied:
            ok(f"#{n} → {Label.AI_READY.value}")
        else:
            err(f"failed to label #{n}: {detail}")
            rc = 1
    return rc


def cmd_prepare_remote(args) -> int:
    """Ensure the two lifecycle labels exist (each read back)."""
    effects = _operator_effects(args.repo, "bootstrap")
    failures = [f"label {label}: {detail}" for label in REMOTE_LABELS
                for applied, detail in (effects.ensure_label(label),) if not applied]
    for failure in failures:
        err(f"remote preparation failed — {failure}")
    if not failures:
        ok("lifecycle labels verified")
    return 1 if failures else 0


def cmd_publish_base_branch(args) -> int:
    applied, detail = _operator_effects(args.repo, "bootstrap").publish_base_branch(args.target_dir, args.branch)
    if not applied:
        err(f"failed to publish base branch: {detail}")
        return 1
    ok(f"published origin/{args.branch}")
    return 0


# ── settings ─────────────────────────────────────────────────────────────────
def cmd_config(args) -> int:
    """Use one settings transaction for either explicit scope, independent of cwd."""
    if args.operation == "get":
        try:
            print(settings.read_setting(HARNESS_ROOT, args.name, args.key, os.environ))
            return 0
        except (OSError, ValueError) as exc:
            err(str(exc))
            return 1
    def open_issues(name: str) -> int | None:
        cfg = from_env(config_files.deployment_env(HARNESS_ROOT, name, os.environ))
        try:
            return GitHubGateway(cfg.repo).open_issue_count()
        except (GhUnavailable, FixObservationIncomplete, OSError, ValueError):
            return None

    result = settings.write_setting(
        HARNESS_ROOT, args.name, args.key, args.value if args.operation == "set" else None,
        environ=os.environ, open_issues=open_issues, operator=f"{getpass.getuser()} (cli)")
    (ok if result.ok else err)(result.detail)
    for warning in result.warnings:
        warn(warning)
    return 0 if result.ok else 1


# ── OS services ──────────────────────────────────────────────────────────────
def _service_env(name: str) -> dict[str, str] | None:
    """Compose the snapshot a service will bake; None (reported) on failure, so
    a unit never points at a stale or missing snapshot."""
    r = subprocess.run([_BASH, _bin("compose-env.sh"), name])
    if r.returncode != 0:
        err(f"compose-env.sh failed (exit {r.returncode}) — refusing to act on an unresolved "
            f"config/.active.{name}.env.")
        return None
    return dict(os.environ, AGENT_ENV=_active_env_path(name), ISSUE_AGENT_DEPLOYMENT_ID=name)


def _service_script() -> str:
    return _bin("install-launchd.sh" if sys.platform == "darwin" else "install-systemd.sh")


def cmd_install_service(args) -> int:
    repo, target_dir, name = _resolve_context(args)
    if not (os.path.isfile(SHARED_ENV) and os.path.isfile(os.path.join(REPOS_DIR, f"{name}.env"))):
        err(f"need config/shared.env + config/repos/{name}.env first (run `issue-agent start` once).")
        return 2
    if _refuse_mismatch("install a service for", name, repo, target_dir):
        return 2
    env = _service_env(name)
    return 1 if env is None else subprocess.run([_BASH, _service_script()], env=env).returncode


def cmd_uninstall_service(args) -> int:
    if args.all:
        return subprocess.run([_BASH, _service_script(), "uninstall"]).returncode
    repo, target_dir, name = _resolve_context(args)
    if _refuse_mismatch("uninstall the service for", name, repo, target_dir):
        return 2
    if not (os.path.isfile(SHARED_ENV) and os.path.isfile(os.path.join(REPOS_DIR, f"{name}.env"))):
        err(f"need config/shared.env + config/repos/{name}.env to identify the deployment; "
            "use --all only when removing every deployment")
        return 2
    env = _service_env(name)
    return 1 if env is None else subprocess.run([_BASH, _service_script(), "uninstall", name], env=env).returncode


# ── argument parser ──────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="issue-agent",
                                description="Autonomous issue-resolving harness and shared configuration.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def command(name: str, func, help_text: str | None, context: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text if help_text is not None else argparse.SUPPRESS)
        if context:
            sp.add_argument("--repo", default="", help="owner/name (default: auto-detect from cwd)")
            sp.add_argument("--target-dir", default="", help="target working copy (default: git root of cwd)")
            sp.add_argument("--name", default="", type=_deployment_name_arg,
                            help="canonical config/repos/<name>.env overlay name (default: reuse/derive)")
        sp.set_defaults(func=func)
        return sp

    sp = command("init", cmd_init, "interactive first-run: create config/shared.env", context=False)
    sp.add_argument("--force", action="store_true", help="overwrite an existing shared.env")

    sp = command("config", cmd_config, "read or edit live settings without a target checkout", context=False)
    actions = sp.add_subparsers(dest="operation", required=True)
    for operation in ("get", "set", "unset"):
        action = actions.add_parser(operation)
        scope = action.add_mutually_exclusive_group(required=True)
        scope.add_argument("--shared", dest="name", action="store_const", const=None,
                           help="harness-wide defaults; no deployment required")
        scope.add_argument("--name", type=_deployment_name_arg, help="one deployment's overlay")
        action.add_argument("key", choices=[spec.key for spec in settings.SPECS])
        if operation == "set":
            action.add_argument("value")

    sp = command("start", cmd_start, "start the watchers against this repo (foreground)")
    sp.add_argument("--roles", nargs="+", choices=ROLES, help="subset to run (default: both)")
    sp.add_argument("--base-branch", default=None,
                    help="base branch for PRs (default: inferred from merged PRs, then the repo default, then 'dev')")
    sp.add_argument("--no-init", action="store_true", help="don't auto-run init on first start; error instead")
    sp.add_argument("--max-budget", default="", help="MAX_BUDGET_USD per fix session")
    sp.add_argument("--review-budget", default="", help="REVIEW_BUDGET_USD per review")
    sp.add_argument("--max-ticks", type=int, default=None, help="bound each daemon's loop (dry run/CI)")
    sp.add_argument("--write-config", action="store_true", help="regenerate the overlay from flags (clobber)")
    sp.add_argument("--worktree", action="store_true",
                    help="run against an isolated git worktree (detached at base), leaving the primary checkout untouched")
    sp.add_argument("--worktree-path", default="",
                    help="explicit path for the harness worktree (default: sibling <repo>-harness); implies --worktree")

    command("stop", cmd_stop, "stop the watchers for this repo")
    sp = command("status", cmd_status, "show watcher state + recent logs for this repo")
    sp.add_argument("-n", "--lines", type=int, default=15, help="log tail length (default: 15)")

    sp = command("dashboard", cmd_dashboard, "serve the operator dashboard (python -m issue_agent.app.dashweb.main)",
                 context=False)
    sp.add_argument("--port", type=int, default=None, help="loopback port (default: the dashboard's own)")

    command("doctor", cmd_doctor, "preflight checks (tooling, identities, repo access)")
    sp = command("ready", cmd_ready, "label issue(s) ai-ready (trigger the fixer)", context=False)
    sp.add_argument("issues", nargs="+", help="issue number(s)")
    sp.add_argument("--repo", default="", help="owner/name (default: auto-detect)")

    sp = command("prepare-remote", cmd_prepare_remote, None, context=False)
    sp.add_argument("--repo", required=True)
    sp = command("publish-base-branch", cmd_publish_base_branch, None, context=False)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--target-dir", required=True)
    sp.add_argument("--branch", required=True)

    command("install-service", cmd_install_service, "install the OS service units (launchd/systemd)")
    sp = command("uninstall-service", cmd_uninstall_service, "remove the OS service units")
    sp.add_argument("--all", action="store_true",
                    help="remove every issue-agent deployment (default: current deployment only)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    try:
        raise SystemExit(args.func(args))
    except KeyboardInterrupt:
        # Ctrl-C at a wizard prompt is an ordinary exit, not a crash.
        print()
        err("aborted.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
