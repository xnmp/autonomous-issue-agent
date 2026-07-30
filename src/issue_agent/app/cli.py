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
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from ..adapters.env_file import source_env_file
from ..adapters.operator_effects import build_operator_effects

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
            return m.group("slug").strip("/")
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
    "TEST_CMD", "DAILY_SPEND_CAP_USD", "FIXER_RUNTIME", "REVIEWER_RUNTIME", "REVIEW_MODE",
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
    parts = [p for p in repo.split("/") if p] if repo else []
    base = "-".join(parts).lower()
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
    if explicit:
        return explicit
    if repo and os.path.isdir(repos_dir):
        envs = [fn for fn in sorted(os.listdir(repos_dir)) if fn.endswith(".env")]
        want = os.path.realpath(target_dir) if target_dir else ""
        repo_matches = [
            fn for fn in envs
            if read_repo_env_field(os.path.join(repos_dir, fn), "REPO") == repo
        ]
        # Tier 2: prefer the overlay whose TARGET_DIR IS the detected working copy.
        if want:
            for fn in repo_matches:
                td = read_repo_env_field(os.path.join(repos_dir, fn), "TARGET_DIR")
                if td and os.path.realpath(td) == want:
                    return fn[: -len(".env")]
        # Tier 3: any REPO match (first alpha) — back-compat.
        if repo_matches:
            return repo_matches[0][: -len(".env")]
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

    REPO is the primary identity check: a same-basename collision across
    owners (`derive_name`'s fallback, or a hand-picked `--name`) can point
    `resolve_name`'s caller at an overlay for a COMPLETELY DIFFERENT repo —
    reusing it launches all three daemons against that other repo/checkout:
    it will label issues, open PRs, and merge there.

    TARGET_DIR is consulted ONLY as a fallback identity signal when the
    overlay records NO REPO at all — never as an independent axis once REPO
    already agrees. `resolve_name`'s Tier-3 fallback (same REPO, but no
    overlay's TARGET_DIR matches the detected one — e.g. two checkouts of one
    repo, or the checkout moved) is legitimate, tested, back-compat behavior;
    treating a REPO-agreeing, TARGET_DIR-differing overlay as a mismatch here
    would refuse that supported case, not the wrong-repo danger this guard
    targets.
    """
    repo = (repo or "").strip()
    target_dir = (target_dir or "").strip()
    name = os.path.basename(overlay)
    overlay_repo = read_repo_env_field(overlay, "REPO").strip()
    if overlay_repo:
        if repo and overlay_repo != repo:
            return (
                f"config/repos/{name} is REPO={overlay_repo!r}; this checkout/flags "
                f"detected REPO={repo!r}"
            )
        return ""
    overlay_target = read_repo_env_field(overlay, "TARGET_DIR").strip()
    if overlay_target and target_dir and (
        os.path.realpath(overlay_target) != os.path.realpath(target_dir)
    ):
        return (
            f"config/repos/{name} is TARGET_DIR={overlay_target!r} (no REPO recorded); "
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
        # Verify whatever we ended up with — discovered OR hand-supplied. A typo
        # in --test-cmd wedges the gate exactly as badly as a bad guess, and this
        # is the last moment before the value is written into the overlay and
        # starts denying pushes.
        if test_cmd and not getattr(args, "no_verify_test_cmd", False):
            info(f"trial-running the test command (up to {_VERIFY_TIMEOUT_S:.0f}s)…")
            report_test_cmd_check(test_cmd, verify_test_cmd(test_cmd, target_dir))
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


# M11: STATE_DIR/REVIEW_STATE_DIR/STEWARD_STATE_DIR paired with the synth
# default dirname pattern `synth_repo_env` writes for each (issue-agent-<name>
# / review-agent-<name> / steward-agent-<name>) — one place both journal_paths
# and synth_repo_env's isolated-state block agree on the naming.
_JOURNAL_ROLE_DEFAULTS = (
    ("STATE_DIR", "issue-agent"),
    ("REVIEW_STATE_DIR", "review-agent"),
    ("STEWARD_STATE_DIR", "steward-agent"),
)


def journal_paths(name: str) -> list[str]:
    """Every per-role journal overlay `name`'s daemons may have written (M11):
    fix, review, and steward each get their OWN STATE_DIR and therefore their
    own journal.jsonl. Reading STATE_DIR alone (the pre-M11 `journal_path`)
    made review/steward runs, their spend blocks, and their loops invisible to
    `issue-agent status`. Overlay value wins per role; an absent/empty one
    falls back to the same per-role default `synth_repo_env` writes into a
    freshly generated overlay. A role that never ran still gets a path here —
    `read_events` (adapters/journal.py) already treats a missing file as an
    empty history, not an error, so callers need no existence check and a
    role that never ran is silently skipped, not flagged."""
    overlay = os.path.join(REPOS_DIR, f"{name}.env")
    paths = []
    for env_key, dirname in _JOURNAL_ROLE_DEFAULTS:
        raw = read_repo_env_field(overlay, env_key)
        state = (
            os.path.expandvars(os.path.expanduser(raw)) if raw
            else os.path.expanduser(f"~/.local/state/{dirname}-{name}")
        )
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
    return _env_cap(name, "DAILY_SPEND_CAP_USD", 50.0)


def global_daily_cap(name: str) -> float:
    """GLOBAL_DAILY_SPEND_CAP_USD — today's spend across every repo. Defaults to
    0 (off), matching HarnessConfig."""
    return _env_cap(name, "GLOBAL_DAILY_SPEND_CAP_USD", 0.0)


def spend_status_lines(
    journal_file: str | list[str], ledger_file: str, cap: float = 0.0, now_fn=None,
    repo: str | None = None, global_cap: float = 0.0,
) -> list[str]:
    """The spend/health section of `issue-agent status`, as plain lines.

    Pure over the files so it is testable without daemons: reads only, and an
    absent or empty journal yields `[]` — a status command that shouts about a
    missing file on a fresh install teaches operators to ignore it.

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

    from ..adapters.journal import (
        events_within,
        parse_ts,
        read_events,
        repeated_dispatch_warning,
        sum_today,
    )

    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    paths = [journal_file] if isinstance(journal_file, str) else list(journal_file)
    events: list[dict] = []
    for path in paths:
        events.extend(read_events(path))
    # Global order across files. A missing/garbled `ts` sorts first (never
    # corrupts the ordering of the events after it) rather than raising —
    # readers tolerating garbage is the module's own contract (journal.py).
    events.sort(key=lambda e: parse_ts(e.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    spent = sum_today(ledger_file, now_fn)
    if not events and not spent:
        return []

    lines = ["", _c("36", "── spend & health ──")]
    # DAILY_SPEND_CAP_USD bounds THIS repo (#32), so pairing it with the
    # all-repos total read as "you have $640 of headroom" on a box where the
    # other deployment had spent it all. Cap and number must share a scope.
    if repo:
        repo_spent = sum_today(ledger_file, now_fn, repo=repo)
        budget = f" / ${cap:.2f} cap" if cap > 0 else " (no daily cap)"
        lines.append(f"  today, {repo}: ${repo_spent:.2f}{budget}")
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
    launchers = _launcher_pids(name)
    print(_c("36", f"▸ status for '{name}'" + (f"  ({repo})" if repo else "")))
    print(f"  launcher (point-at.sh): {'running ' + str(launchers) if launchers else 'stopped'}")
    for role in ROLES:
        pids = roles[role]
        state = _c("32", "running") + f" {pids}" if pids else _c("33", "stopped")
        print(f"  {role:8s}: {state}")
    logs = log_dir()
    for role in ROLES:
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
        from ..adapters.journal import default_ledger_path

        for line in spend_status_lines(
            journal_paths(name), default_ledger_path(), daily_cap(name),
            repo=read_repo_env_field(os.path.join(REPOS_DIR, f"{name}.env"), "REPO"),
            global_cap=global_daily_cap(name),
        ):
            print(line)
    except Exception as exc:  # noqa: BLE001
        err(f"(could not read the event journal: {exc})")
    return 0


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
    preparation = _prepare_dashweb()
    if preparation.ready:
        from .dashweb.server import serve as dashweb_serve

        info(f"serving the interactive dashboard at {url} (Ctrl-C to stop)")
        try:
            dashweb_serve(args.serve)
        except KeyboardInterrupt:
            print()
        return 0
    warn("interactive dashboard unavailable — serving the v1 snapshot instead:")
    warn(f"  {preparation.detail}")
    warn(f"  {dashboard_build_command()}")
    info(f"serving the dashboard at {url} (Ctrl-C to stop) — regenerated per request")
    try:
        dash.serve(args.serve, lambda: dash.render_html(build()))
    except KeyboardInterrupt:
        print()
    return 0


def cmd_doctor(args) -> int:
    """Compose env for this repo (if detectable) then run bin/doctor.sh, which
    reads AGENT_ENV. With no repo context it still runs tooling/identity checks."""
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
        r = subprocess.run(["/bin/bash", _bin("compose-env.sh"), name])
        if r.returncode == 0:
            env["AGENT_ENV"] = _active_env_path(name)
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
    return subprocess.run(["/bin/bash", _bin("doctor.sh")], env=env).returncode


def _label_issues(args, label: str) -> int:
    repo = args.repo or detect_repo(os.getcwd())
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
    # A4 fold-in: this used to run with check=False and never look at the
    # return code — a compose failure (bad overlay syntax, missing file
    # between check above and here, etc.) fell through silently, and the
    # service unit got installed pointing AGENT_ENV at a STALE or missing
    # the composed active env (whatever a prior successful compose left behind, or
    # nothing). Fail loud and abort instead of installing against unknown env.
    r = subprocess.run(["/bin/bash", _bin("compose-env.sh"), name])
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
    fields.append(("REVIEWER_RUNTIME", ask("Reviewer runtime (codex|claude)", "codex")))
    if any(k in ("FIXER_RUNTIME", "REVIEWER_RUNTIME") and v == "codex" for k, v in fields):
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
        "test command; a repo with no tests proceeds behind a loud warning. The "
        "resolved command is then trial-run once (--no-verify-test-cmd skips it) "
        "so watch-mode or a missing binary is caught here rather than by every "
        "push being denied later.",
    )
    add_context_flags(sp)
    sp.add_argument("--roles", nargs="+", choices=ROLES, help="subset to run (default: all three)")
    sp.add_argument("--base-branch", default="dev", help="base branch for PRs (default: dev)")
    sp.add_argument("--test-cmd", default="", help="TEST_CMD (arms the PR/red-fail gate); omit to auto-discover")
    sp.add_argument(
        "--no-verify-test-cmd", action="store_true",
        help="skip the one trial run of TEST_CMD (it catches watch-mode and "
             "missing binaries before they deny every push)",
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
    sp.add_argument("--name", required=True)
    sp.add_argument("--ensure-auto-merge", action="store_true")
    sp.add_argument("--skip-labels", action="store_true")
    sp.set_defaults(func=cmd_prepare_remote)

    sp = sub.add_parser("publish-base-branch", help=argparse.SUPPRESS)
    sp.add_argument("--repo", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--target-dir", required=True)
    sp.add_argument("--branch", required=True)
    sp.set_defaults(func=cmd_publish_base_branch)

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
