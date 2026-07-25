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

Subcommands: init | start | stop | status | doctor | ready | triage |
install-service | uninstall-service. The pure helpers (detect_repo,
resolve_name, synth_repo_env) are unit-tested with no git/network.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# HARNESS_ROOT: …/src/issue_agent/app/cli.py → repo root. Same walk main.py uses.
# This is what makes the CLI checkout-resident: plugin/ + config/ are siblings.
HARNESS_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
REPOS_DIR = os.path.join(HARNESS_ROOT, "config", "repos")
SHARED_ENV = os.path.join(HARNESS_ROOT, "config", "shared.env")
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
            return m.group("slug").strip("/")
    return ""


def read_repo_env_field(path: str, field: str) -> str:
    """Best-effort read of `FIELD="value"` from a shell env file (no sourcing).

    Only used to match an existing overlay's REPO against the detected repo, so a
    simple line scan (last assignment wins, quotes stripped) is enough — the real
    expansion happens in bin/compose-env.sh at launch.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    value = ""
    pat = re.compile(rf"^\s*(?:export\s+)?{re.escape(field)}=(.*)$")
    for line in lines:
        m = pat.match(line)
        if m:
            value = m.group(1).strip().strip('"').strip("'")
    return value


def derive_name(repo: str) -> str:
    """Fallback overlay name from a repo slug: lowercased basename, safe chars.

    my-org/example-agent → example-agent
    """
    base = repo.split("/")[-1].lower() if repo else ""
    return re.sub(r"[^a-z0-9._-]", "-", base).strip("-") or "target"


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
    3. An overlay whose REPO==repo (first alpha) — back-compat fallback when
       target_dir is unknown or no overlay's TARGET_DIR matches it.
    4. Derived from the repo basename.
    """
    if explicit:
        return explicit
    if repo and os.path.isdir(repos_dir):
        envs = [fn for fn in sorted(os.listdir(repos_dir)) if fn.endswith(".env")]
        repo_matches = [
            fn for fn in envs
            if read_repo_env_field(os.path.join(repos_dir, fn), "REPO") == repo
        ]
        # Tier 2: prefer the overlay whose TARGET_DIR IS the detected working copy.
        if target_dir:
            want = os.path.realpath(target_dir)
            for fn in repo_matches:
                td = read_repo_env_field(os.path.join(repos_dir, fn), "TARGET_DIR")
                if td and os.path.realpath(td) == want:
                    return fn[: -len(".env")]
        # Tier 3: any REPO match (first alpha) — back-compat.
        if repo_matches:
            return repo_matches[0][: -len(".env")]
    return derive_name(repo)


def _q(value: str) -> str:
    """Shell-single-quote a value for the synthesized env file."""
    return "'" + value.replace("'", "'\\''") + "'"


def synth_repo_env(
    *,
    repo: str,
    target_dir: str,
    name: str,
    base_branch: str = "dev",
    test_cmd: str = "",
    edit_check_cmd: str = "",
    review_mode: str = "",
    max_budget: str = "",
    review_budget: str = "",
) -> str:
    """Render a config/repos/<name>.env overlay from flags. Keys mirror exactly
    what app/config.from_env reads, and state dirs are isolated per name (matching
    the hand-authored myapp.env / otherapp.env) so parallel deployments never collide."""
    lines = [
        f"# Per-repo overlay — {repo}. AUTO-GENERATED by `issue-agent start`.",
        "# Overlaid on config/shared.env by bin/point-at.sh. Safe to edit by hand;",
        "# `issue-agent start` will NOT clobber it (pass --write-config to regenerate).",
        "",
        f"REPO={_q(repo)}",
        f"TARGET_DIR={_q(target_dir)}",
        f"BASE_BRANCH={_q(base_branch)}",
        "",
        "# Isolated daemon state (keeps this deployment's state separate).",
        f'STATE_DIR="$HOME/.local/state/issue-agent-{name}"',
        f'REVIEW_STATE_DIR="$HOME/.local/state/review-agent-{name}"',
        f'STEWARD_STATE_DIR="$HOME/.local/state/steward-agent-{name}"',
    ]
    opt = [
        ("TEST_CMD", test_cmd),
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
        return slug
    return parse_remote_url(_run(["git", "remote", "get-url", "origin"], cwd=cwd))


# ── test-command discovery (cheap LLM probe) ──────────────────────────────────
# Signature files that tell us a test toolchain exists AT ALL. Cheap gate BEFORE
# spending an LLM call: if none of these are anywhere in the tree, there are no
# tests and we skip the probe (and the banner fires). Kept broad, not exhaustive
# — the LLM does the precise read; this only decides "worth asking?".
_TEST_SIGNS = (
    "pytest.ini", "tox.ini", "conftest.py", "pyproject.toml", "setup.cfg",
    "package.json", "vitest.config", "jest.config", "playwright.config",
    "go.mod", "Cargo.toml", "build.gradle", "pom.xml", "Makefile", "justfile",
    "phpunit.xml", "Gemfile", "*_test.go", "*.test.ts", "*.spec.ts",
)

_TEST_CMD_MARKER = "TEST_CMD:"
_TEST_DISCOVERY_PROMPT = """\
You are inspecting a git repository to find the ONE shell command that runs its \
test suite non-interactively (must EXIT, never watch-mode).

Rules:
- Inspect config files (pyproject.toml, package.json scripts, Makefile, tox.ini, \
go.mod, Cargo.toml, etc.) and the test layout.
- Return a command that runs from the repo root and exits with non-zero on failure.
- For JS/TS prefer the non-watch form (`vitest run`, `jest`, `pnpm test` only if it \
is non-watch). NEVER return a bare `vitest`/`jest --watch`.
- For a monorepo, compose the tiers with `set -e` (e.g. \
`set -e; (cd backend && uv run pytest) && (cd frontend && pnpm exec tsc -b)`).

Your ENTIRE reply must be a SINGLE line in exactly this form, nothing before or after:
  TEST_CMD: <the command>
If — and ONLY if — the repo has NO test suite at all, reply with the single line:
  TEST_CMD: NO_TESTS

Do not explain. Do not use code fences. The line must start with `TEST_CMD:`."""


def _has_test_signature(target_dir: str) -> bool:
    """Fast pre-check: does any known test-toolchain signature file exist? Uses
    git ls-files (respects .gitignore, fast) with a plain-walk fallback."""
    listing = _run(["git", "ls-files"], cwd=target_dir)
    names = listing.split("\n") if listing else []
    if not names:
        for root, _dirs, files in os.walk(target_dir):
            if ".git" in root:
                continue
            names.extend(files)
    basenames = {os.path.basename(n) for n in names}
    for sign in _TEST_SIGNS:
        if sign.startswith("*"):
            suf = sign[1:]
            if any(n.endswith(suf) for n in names):
                return True
        elif sign in basenames:
            return True
    return False


def discover_test_cmd(target_dir: str, model: str = "haiku") -> str:
    """Find the repo's test command via a cheap read-only LLM probe. Returns the
    command, or '' if the repo genuinely has no tests (or the probe is unavailable
    — fail-soft: a missing `claude` must not block `start`). Never raises.

    Cheap by construction: a signature-file pre-check skips the LLM call entirely
    for repos with no test toolchain, and the call itself is Haiku + read-only
    tools (Read/Glob/Grep) so it can't mutate anything."""
    if not target_dir or not os.path.isdir(target_dir):
        return ""
    if not _has_test_signature(target_dir):
        return ""  # no toolchain → definitely no tests; don't spend a call
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
        return ""  # claude absent / timed out — fail soft, proceed without a cmd
    if proc.returncode != 0:
        return ""
    return _parse_test_cmd_reply(proc.stdout or "")


def _parse_test_cmd_reply(text: str) -> str:
    """Extract the command from the model's `TEST_CMD: <cmd>` line. Robust to a
    stray prose line or a code fence the model adds despite instructions: we scan
    for the LAST marker line (weak models sometimes echo the instruction first)
    and take everything after the marker. NO_TESTS (or an empty command) → ''."""
    cmd = ""
    for line in text.splitlines():
        line = line.strip().strip("`").strip()
        if line.startswith(_TEST_CMD_MARKER):
            cmd = line[len(_TEST_CMD_MARKER):].strip()
    if not cmd or cmd.upper() == "NO_TESTS":
        return ""
    return cmd


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


def _launcher_pids(name: str) -> list[int]:
    return _pgrep(f"point-at.sh {name}")


def _role_pids(name: str) -> dict[str, list[int]]:
    """Map each role → daemon pids. The daemon argv is `… issue_agent.app.main
    <role>`, which carries no repo/name marker, so this matches by role only. That
    is exact under the single-launcher-per-repo invariant point-at.sh enforces
    (it refuses to start a second launcher for a name); with multiple deployments
    on one host the launcher-pid check in stop/status is the repo-scoped signal."""
    return {role: _pgrep(f"issue_agent.app.main {role}") for role in ROLES}


# ── subcommand implementations ────────────────────────────────────────────────
def _bin(script: str) -> str:
    return os.path.join(HARNESS_ROOT, "bin", script)


def _resolve_context(args) -> tuple[str, str, str]:
    """(repo, target_dir, name) from flags then cwd auto-detection."""
    cwd = os.getcwd()
    target_dir = args.target_dir or detect_target_dir(cwd)
    repo = args.repo or detect_repo(cwd)
    # Pass target_dir so a repo with several overlays resolves to the one for THIS
    # working copy (cwd wins), not the alphabetically-first REPO match.
    name = resolve_name(repo, getattr(args, "name", "") or "", target_dir=target_dir)
    return repo, target_dir, name


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
        wt = ensure_harness_worktree(target_dir, args.base_branch, getattr(args, "worktree_path", "") or "")
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
        rc = cmd_init(argparse.Namespace(force=False))
        if rc != 0 or not os.path.isfile(SHARED_ENV):
            return rc or 2
        print()

    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    if not os.path.isfile(overlay) or args.write_config:
        os.makedirs(REPOS_DIR, exist_ok=True)
        # Resolve TEST_CMD: explicit flag wins; else probe the repo with a cheap
        # read-only LLM search. Empty result → the repo has no tests: proceed, but
        # LOUDLY. Only done when synthesizing a fresh overlay (not on reuse).
        test_cmd = args.test_cmd or ""
        if not test_cmd:
            info("no --test-cmd given — searching the repo for its test command…")
            test_cmd = discover_test_cmd(target_dir)
            if test_cmd:
                ok(f"discovered TEST_CMD: {test_cmd}")
            else:
                _no_tests_banner()
        content = synth_repo_env(
            repo=repo, target_dir=target_dir, name=name,
            base_branch=args.base_branch, test_cmd=test_cmd,
            edit_check_cmd=args.edit_check_cmd or "", review_mode=args.review_mode or "",
            max_budget=args.max_budget or "", review_budget=args.review_budget or "",
        )
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
    info(f"delegating to bin/point-at.sh {name}")
    # exec — hand over the terminal (Ctrl-C trap, log tail live in point-at.sh).
    os.execve("/bin/bash", ["/bin/bash", _bin("point-at.sh"), name], env)


def cmd_stop(args) -> int:
    repo, _target, name = _resolve_context(args)
    launchers = _launcher_pids(name)
    roles = _role_pids(name)
    pids = sorted(set(launchers) | {p for ps in roles.values() for p in ps})
    if not pids:
        info(f"no watchers running for '{name}'.")
        return 0
    info(f"stopping watchers for '{name}' (pids: {' '.join(map(str, pids))})")
    # Kill launchers first (their trap tears down the group), then any strays.
    for pid in pids:
        try:
            os.kill(pid, 15)  # SIGTERM — daemons/flocks free cleanly
        except ProcessLookupError:
            pass
    ok("sent SIGTERM")
    return 0


def cmd_status(args) -> int:
    repo, _target, name = _resolve_context(args)
    roles = _role_pids(name)
    launchers = _launcher_pids(name)
    print(_c("36", f"▸ status for '{name}'" + (f"  ({repo})" if repo else "")))
    print(f"  launcher (point-at.sh): {'running ' + str(launchers) if launchers else 'stopped'}")
    for role in ROLES:
        pids = roles[role]
        state = _c("32", "running") + f" {pids}" if pids else _c("33", "stopped")
        print(f"  {role:8s}: {state}")
    for role in ROLES:
        log = f"/tmp/issue-agent-{role}-{name}.log"
        if os.path.isfile(log):
            print(_c("36", f"\n── tail {log} ──"))
            print(_run(["tail", "-n", str(args.lines), log]) or "  (empty)")
    return 0


def cmd_doctor(args) -> int:
    """Compose env for this repo (if detectable) then run bin/doctor.sh, which
    reads AGENT_ENV. With no repo context it still runs tooling/identity checks."""
    _repo, _target, name = _resolve_context(args)
    env = dict(os.environ)
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    if os.path.isfile(SHARED_ENV) and os.path.isfile(overlay):
        r = subprocess.run(["/bin/bash", _bin("compose-env.sh"), name])
        if r.returncode == 0:
            env["AGENT_ENV"] = os.path.join(HARNESS_ROOT, "config", ".active.env")
    return subprocess.run(["/bin/bash", _bin("doctor.sh")], env=env).returncode


def _label_issues(args, label: str) -> int:
    repo = args.repo or detect_repo(os.getcwd())
    if not repo:
        err("could not detect the repo. Pass --repo owner/name.")
        return 2
    rc = 0
    for n in args.issues:
        r = subprocess.run(
            ["gh", "issue", "edit", str(n), "-R", repo, "--add-label", label]
        )
        if r.returncode == 0:
            ok(f"#{n} → {label}")
        else:
            err(f"failed to label #{n}")
            rc = 1
    return rc


def cmd_ready(args) -> int:
    return _label_issues(args, "ai-ready")


def cmd_triage(args) -> int:
    return _label_issues(args, "ai-triage")


def cmd_install_service(args) -> int:
    _repo, _target, name = _resolve_context(args)
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    if not (os.path.isfile(SHARED_ENV) and os.path.isfile(overlay)):
        err(f"need config/shared.env + config/repos/{name}.env first (run `issue-agent start` once).")
        return 2
    subprocess.run(["/bin/bash", _bin("compose-env.sh"), name], check=False)
    env = dict(os.environ, AGENT_ENV=os.path.join(HARNESS_ROOT, "config", ".active.env"))
    script = "install-launchd.sh" if sys.platform == "darwin" else "install-systemd.sh"
    cmd = ["/bin/bash", _bin(script)]
    # Pass a single-role selector through (launchd installer supports --fix/etc).
    if args.roles and len(args.roles) == 1 and sys.platform == "darwin":
        cmd.append(f"--{args.roles[0]}")
    return subprocess.run(cmd, env=env).returncode


def cmd_uninstall_service(args) -> int:
    script = "install-launchd.sh" if sys.platform == "darwin" else "install-systemd.sh"
    return subprocess.run(["/bin/bash", _bin(script), "uninstall"]).returncode


def cmd_init(args) -> int:
    """Interactive first-run: populate config/shared.env (global identity +
    gateway secrets). Idempotent — refuses to clobber without --force."""
    if os.path.isfile(SHARED_ENV) and not args.force:
        err(f"config/shared.env already exists — refusing to clobber. Edit it, or re-run with --force.")
        return 2
    if not sys.stdin.isatty():
        err("`issue-agent init` is interactive; run it in a terminal (or copy config/agent.env.example → config/shared.env by hand).")
        return 2

    print(_c("36", "▸ issue-agent init — creating config/shared.env (global identity + secrets)"))
    print("  Press Enter to accept the [default]. These are repo-INDEPENDENT; per-repo")
    print("  knobs (REPO/TARGET_DIR/TEST_CMD) are written by `issue-agent start`.\n")

    def ask(prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        try:
            val = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            val = ""
        return val or default

    fields: list[tuple[str, str]] = []
    fields.append(("IDENTITY_MODE", ask("Identity mode (three|solo)", "three")))
    fields.append(("FIX_BOT", ask("Fixer GitHub login (ambient gh auth user)", "")))
    fields.append(("REVIEW_BOT", ask("Reviewer login (bot; distinct from fixer)", "")))
    fields.append(("STEWARD_BOT", ask("Steward login (may equal reviewer)", "")))
    print()
    if ask("Configure GitHub App for review+steward identity? (y/N)", "N").lower().startswith("y"):
        fields.append(("GH_APP_ID", ask("GH App ID", "")))
        fields.append(("GH_APP_INSTALL_ID", ask("GH App installation ID", "")))
        fields.append(("GH_APP_KEY_FILE", ask("GH App private-key .pem path", "")))
    print()
    if ask("Enable Jira sync? (y/N)", "N").lower().startswith("y"):
        fields.append(("JIRA_SYNC", "1"))
        fields.append(("JIRA_PROJECT_KEY", ask("Jira project key (e.g. ACME)", "")))
        fields.append(("JIRA_CLOUD_ID", ask("Jira cloud id / host", "")))
    print()
    fields.append(("FIXER_RUNTIME", ask("Fixer runtime (claude|codex)", "claude")))
    if any(k == "FIXER_RUNTIME" and v == "codex" for k, v in fields):
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
    with open(SHARED_ENV, "w") as f:
        f.write("\n".join(lines) + "\n")
    ok(f"wrote {SHARED_ENV}")
    print("  Review it, then `cd <target-repo> && issue-agent start`.")
    return 0


# ── argument parser ───────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="issue-agent",
        description="Autonomous issue-resolving harness — run it from inside a target repo.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_context_flags(sp):
        sp.add_argument("--repo", default="", help="owner/name (default: auto-detect from cwd)")
        sp.add_argument("--target-dir", default="", help="target working copy (default: git root of cwd)")
        sp.add_argument("--name", default="", help="config/repos/<name>.env overlay name (default: reuse/derive)")

    sp = sub.add_parser("init", help="interactive first-run: create config/shared.env")
    sp.add_argument("--force", action="store_true", help="overwrite an existing shared.env")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser(
        "start",
        help="start the watchers against this repo (foreground)",
        description="Start the watchers in the FOREGROUND (Ctrl-C stops all; logs "
        "tail live). First run auto-creates config/shared.env via `init`. If "
        "--test-cmd is omitted, a cheap read-only LLM probe discovers the repo's "
        "test command; a repo with no tests proceeds behind a loud warning.",
    )
    add_context_flags(sp)
    sp.add_argument("--roles", nargs="+", choices=ROLES, help="subset to run (default: all three)")
    sp.add_argument("--base-branch", default="dev", help="base branch for PRs (default: dev)")
    sp.add_argument("--test-cmd", default="", help="TEST_CMD (arms the PR/red-fail gate); omit to auto-discover")
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

    sp = sub.add_parser("install-service", help="install the OS service units (launchd/systemd)")
    add_context_flags(sp)
    sp.add_argument("--roles", nargs="+", choices=ROLES, help="single role → install just that unit (launchd)")
    sp.set_defaults(func=cmd_install_service)

    sp = sub.add_parser("uninstall-service", help="remove the OS service units")
    sp.set_defaults(func=cmd_uninstall_service)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
