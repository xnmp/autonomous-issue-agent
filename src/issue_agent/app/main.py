"""Entry point — `python -m issue_agent.app.main <fix|review|steward>`.

Replaces `make run-fix` / `run-review` / `run-steward` (agent/watcher.sh etc.).
Builds the identity (fixer = ambient/PAT; review+steward = GitHub App token), the
adapters, the governor hook, and runs the chosen role's daemon forever.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import time
from collections.abc import Callable

from ..adapters.agent_runner import AgentRunner
from ..adapters.app_token import AppCredentials, AppTokenMinter, default_mint_fn
from ..adapters.github import GitHubGateway, Identity
from ..adapters.governor import build_hooks
from ..adapters.single_instance import AlreadyRunning, acquire
from ..adapters.store import Store
from ..adapters.worktree import Worktrees, build_gate_context
from .config import HarnessConfig, from_env, render_agent_md
from .roles import build_fix_daemon, build_review_daemon, build_steward_daemon
from .world_probe import WorldProbe


def _load_agent_env(env: dict) -> dict:
    """Return env plus variables from AGENT_ENV, if configured.

    Repo config files are intentionally shell syntax because operators compose
    values with parameter expansion (for example `${ANTHROPIC_AUTH_TOKEN:-...}`).
    Keep that compatibility at the process edge, then pass a plain dictionary to
    the Python config mapper. Variables from the file match legacy watcher
    semantics: sourcing the file overrides inherited values.
    """
    path = env.get("AGENT_ENV", "")
    if not path:
        return env
    try:
        proc = subprocess.run(
            [
                "bash",
                "-c",
                'set -a; source "$1"; set +a; env -0',
                "issue-agent-env",
                path,
            ],
            env=env,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"could not load AGENT_ENV {path!r}: {exc}") from exc

    loaded = env.copy()
    for item in proc.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        loaded[key.decode()] = value.decode()
    loaded["AGENT_ENV"] = path
    return loaded


def _app_identity(cfg_env: dict, warn: Callable[[str], None] = lambda _m: None) -> Identity:
    """review/steward identity: GitHub App installation token if configured AND
    USABLE, else a static bot token, else ambient. Mirrors gh_review/gh_steward
    priority.

    "Configured" has to mean more than "the three vars are non-empty".
    config/shared.example.env ships GH_APP_* as literal `<your-app-id>` /
    `$HOME/.config/<your-app>/private-key.pem` placeholders, and PATH A (bot
    account + REVIEW_BOT_TOKEN) is documented as leaving them alone. Selecting the
    App branch on non-emptiness alone commits to a key file that does not exist,
    and `AppTokenMinter.get` then raises ENOENT on EVERY poll pass — observed on
    the first real deployment as an endless `review: poll pass failed (continuing)`
    with the reviewer never reviewing anything. So gate the branch on the key being
    READABLE and fall through when it is not.

    Fall through LOUDLY. Demoting to ambient silently would make review == FIX_BOT,
    and under IDENTITY_MODE=three every approval is then a GitHub 422 self-approval
    — a failure that surfaces far from its cause.
    """
    if cfg_env.get("GH_APP_ID") and cfg_env.get("GH_APP_KEY_FILE") and cfg_env.get("GH_APP_INSTALL_ID"):
        key_file = os.path.expanduser(cfg_env["GH_APP_KEY_FILE"])
        if os.access(key_file, os.R_OK):
            creds = AppCredentials(
                app_id=cfg_env["GH_APP_ID"],
                install_id=cfg_env["GH_APP_INSTALL_ID"],
                key_file=key_file,
                cache_path=cfg_env.get("GH_APP_TOKEN_CACHE", os.path.expanduser("~/.local/state/issue-agent-bot/app-token.json")),
            )
            minter = AppTokenMinter(creds, default_mint_fn, now_fn=lambda: int(time.time()))
            return Identity(token_provider=minter.get)
        warn(
            f"GH_APP_KEY_FILE '{key_file}' is missing or unreadable — ignoring the "
            "GitHub App identity and falling back to "
            + ("REVIEW_BOT_TOKEN" if cfg_env.get("REVIEW_BOT_TOKEN")
               else "ambient `gh auth` (which is FIX_BOT — approvals will 422)")
        )
    if cfg_env.get("REVIEW_BOT_TOKEN"):
        return Identity(static_token=cfg_env["REVIEW_BOT_TOKEN"])
    return Identity()


def _agent_md(cfg: HarnessConfig) -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    path = os.path.join(here, "plugin", "AGENT.md")
    try:
        with open(path) as f:
            return render_agent_md(f.read(), cfg)
    except OSError:
        return ""


def _review_prompt(cfg: HarnessConfig) -> str:
    """Reviewer system prompt. An operator REVIEW_PROMPT (cfg.review_prompt, from
    the env) wins wholesale; otherwise load the built-in dimension scaffold from
    plugin/REVIEW_PROMPT.md. File I/O lives here (not in the pure from_env mapping),
    mirroring _agent_md. `$ROUND_NUMBER` is substituted per round downstream."""
    if cfg.review_prompt:
        return cfg.review_prompt
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    path = os.path.join(here, "plugin", "REVIEW_PROMPT.md")
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def build_role(role: str, env: dict | None = None):
    # review S7/D3: `main()` already loads AGENT_ENV once (via `_load_agent_env`,
    # which shells out to `bash -c ... env -0`) and passes the merged dict here —
    # only self-load when called standalone (env=None) so the documented
    # entrypoint (`python -m issue_agent.app.main <role>`) never pays for that
    # subprocess twice per boot.
    if env is None:
        env = _load_agent_env(dict(os.environ))
    cfg = from_env(env)
    cfg.validate()
    if cfg.target_dir and os.path.isdir(cfg.target_dir):
        os.chdir(cfg.target_dir)
    # Harness root (…/src/issue_agent/app/main.py → repo root): resolves relative
    # FIX_MCP_CONFIG paths. Honour an explicit HARNESS_ROOT_DIR override if set.
    harness_root = cfg.harness_root or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    # review S7: was `HarnessConfig(**{**cfg.__dict__, ...})` — a manual rebuild
    # of a frozen dataclass from its own __dict__. `dataclasses.replace` is the
    # stdlib-blessed way to derive one frozen instance from another and, unlike
    # the __dict__ spread, raises on a typo'd/renamed field instead of silently
    # constructing a HarnessConfig with the wrong shape.
    cfg = dataclasses.replace(
        cfg,
        agent_md=_agent_md(cfg),
        review_prompt=_review_prompt(cfg),
        harness_root=harness_root,
    )

    target_dir = cfg.target_dir or os.getcwd()

    # Resolved-config print (lean P5.1): every daemon start states what it
    # ACTUALLY loaded — the only structural defense against the silent-default
    # class (#16/#49/#50/#78). TEST_CMD/ALLOW_UNTESTED are env facts the gate
    # reads directly, so they're reported here alongside the config.
    for line in cfg.describe().splitlines():
        sys.stderr.write(f"{role}: config: {line}\n")
    sys.stderr.write(
        f"{role}: config: TEST_CMD={'set' if env.get('TEST_CMD') else 'EMPTY'} "
        f"ALLOW_UNTESTED={env.get('ALLOW_UNTESTED', '0') or '0'}\n"
    )
    sys.stderr.flush()

    # The governor hook needs a GateContext built from the agent's worktree cwd.
    def build_ctx(cwd: str):
        return build_gate_context(
            cwd, base_branch=cfg.base_branch,
            # Pin the open-PR probe to THIS repo (review P1R-11). Without it gh
            # resolves the repo from the worktree's remotes, and on a target with
            # more than one remote it answers about the wrong repository — where
            # the branch has no PR, so the push gate silently does not apply.
            repo=cfg.repo,
            env_test_cmd=env.get("TEST_CMD"),
            red_must_fail=env.get("RED_MUST_FAIL", "1") != "0",
            verify_red_at_commit=env.get("VERIFY_RED_AT_COMMIT", "1") != "0",
            # Operator opt-out for a test-less repo: without it the fail-closed
            # tests gate (check_tests) denies every pr-create/push. Mirrors the
            # bash checks' ALLOW_UNTESTED — an overlay fact, never agent-set.
            allow_untested=env.get("ALLOW_UNTESTED", "") == "1",
        )

    # Live per-commit breadcrumb (red:/green:/docs:) from inside the agent session,
    # tagged with the role so it interleaves with the daemon's own log lines.
    def _commit_log(msg: str) -> None:
        sys.stderr.write(f"{role}: {msg}\n")
        sys.stderr.flush()

    # ISSUE_AGENT_DEBUG=1 turns on the deep trace: every Bash command + gate
    # verdict (governor hook) AND every SDK stream message (runner). This is the
    # instrument for the "Stream closed" wedge — off by default (verbose), on when
    # diagnosing. Boot logs whether it's active so the log is self-describing.
    debug = env.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False")
    hooks = build_hooks(
        build_ctx, edit_check_cmd=cfg.edit_check_cmd or None, log=_commit_log, debug=debug
    )
    _idle = cfg.agent_idle_timeout if cfg.agent_idle_timeout > 0 else None
    # Two independent caps: silence (idle) and total elapsed (wall). The wall cap is
    # what bounds a limping post-suspend stream, which never goes silent enough to
    # trip the idle watchdog. 0 on either knob disables that one only.
    _wall = cfg.agent_max_runtime if cfg.agent_max_runtime > 0 else None
    runner = AgentRunner(
        hooks=hooks, log=_commit_log, debug=debug,
        idle_timeout=_idle, max_runtime=_wall,
    )
    worktrees = Worktrees(target_dir, cfg.worktree_root, cfg.base_branch)

    if role == "fix":
        gw = GitHubGateway(cfg.repo, Identity(static_token=env.get("FIX_BOT_TOKEN") or None))
        # runtime-tagged sessions: a FIXER_RUNTIME flip must cold-start resume
        # legs, not feed the new runtime the old runtime's session ids.
        store = Store(cfg.state_dir, runtime=cfg.fixer_runtime)
        # Arm-at-creation probe (once per boot): only a POSITIVELY verified
        # review+checks gate on the base branch activates early arming — see
        # FixConfig.arm_on_open for why no knob can force this on. Probed here
        # (not in from_env) because it needs the gateway; a flip of the repo's
        # protection therefore takes effect at the next daemon restart.
        if cfg.arm_on_open != "0":
            active = gw.merge_gate_protection(cfg.base_branch)
            cfg = dataclasses.replace(cfg, arm_on_open_active=active)
            sys.stderr.write(
                f"{role}: arm-on-open "
                + (
                    f"ACTIVE — {cfg.base_branch} protection verifiably requires "
                    "review + status checks; auto-merge arms at PR creation\n"
                    if active else
                    f"inactive — {cfg.base_branch} protection does not gate "
                    "merging; auto-merge arms after APPROVE + green CI\n"
                )
            )
        return build_fix_daemon(cfg, gw, store, worktrees, runner)
    # Change-probe gate for review/steward observes: 1 REST point per quiet tick
    # instead of a full GraphQL world build (the 2026-07-26 rate-limit exhaustion
    # — two daemons on one token at 20s polls). The FIX daemon is deliberately
    # never gated: its decide reads CI check states and mergeable/mergeStateStatus,
    # which GitHub recomputes WITHOUT bumping any updated_at, so the probe's
    # signature is blind to exactly the signals the fixer acts on. Review reads
    # neither; the steward never fetches PRs at all (Jira-side edits, when sync
    # is on, are picked up at the probe's TTL reconciliation cadence).
    def _probed(daemon, gw):
        if not cfg.world_probe:
            return daemon
        probe = WorldProbe(gw.change_signature, float(cfg.world_probe_ttl_seconds))
        return dataclasses.replace(daemon, observe=probe.gate(daemon.observe))

    if role == "review":
        gw = GitHubGateway(cfg.repo, _app_identity(env, warn=_commit_log),
                           review_pr_label=cfg.review_trigger_label,
                           review_adopt_unlabeled=cfg.review_adopt_unlabeled)
        store = Store(cfg.review_state_dir)
        return _probed(build_review_daemon(cfg, gw, store, runner), gw)
    if role == "steward":
        gw = GitHubGateway(cfg.repo, _app_identity(env, warn=_commit_log))
        store = Store(cfg.steward_state_dir)
        return _probed(build_steward_daemon(cfg, gw, store, runner), gw)
    raise SystemExit(f"unknown role: {role!r} (use fix|review|steward)")


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        raise SystemExit("usage: python -m issue_agent.app.main <fix|review|steward>")
    role = argv[0]
    if role not in ("fix", "review", "steward"):
        raise SystemExit(f"unknown role: {role!r} (use fix|review|steward)")

    # Single-instance guard: refuse to start a second daemon of the same (role,
    # repo). A duplicate would poll independently and double-dispatch (two fixers
    # both grab ai-ready), breaking the single-fixer invariant. flock auto-frees
    # on process death, so there's no stale-lock hazard. Held for the whole run
    # via `_lock` (the open fd IS the lock; GC/exit releases it).
    env = _load_agent_env(dict(os.environ))
    # review C5(b): export the composed config into THIS process's environment
    # before building anything. No adapter subprocess call passes `env=`
    # explicitly, so every shell-out downstream (bash gate checkers, git, gh,
    # codex) inherits os.environ as it stood at spawn time. Foreground
    # `point-at.sh` happens to `set -a`-source the same files into its own
    # shell first, so dev mode worked by accident; systemd units set only
    # AGENT_ENV + TARGET_DIR, so without this update the bash checkers read
    # TEST_CMD/ALLOW_UNTESTED/BASE_BRANCH from an empty environment — the
    # test-less-repo escape hatch became unreachable (fail-closed gate) and
    # operator knobs like CI_IGNORE_CHECKS were honoured in dev and silently
    # ignored under the service. `update()` (not a fresh dict) matches
    # foreground `set -a` sourcing semantics: the overlay wins over whatever
    # the service unit already set, inherited env stays for anything the
    # overlay didn't touch.
    os.environ.update(env)
    repo = env.get("REPO", "")
    try:
        _lock = acquire(role, repo)  # noqa: F841 — kept alive for process lifetime
    except AlreadyRunning as e:
        sys.stderr.write(f"{role}: refusing to start — {e}\n")
        raise SystemExit(3) from e

    # Record the proxy posture at boot so a "can't reach api.github.com" failure is
    # diagnosable from the role log alone (the daemon inherits the launching shell's
    # env; nothing proxy-related is hardcoded). One line, stderr, no network call.
    _proxy = env.get("HTTPS_PROXY") or env.get("https_proxy")
    _dbg = env.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False")
    sys.stderr.write(
        f"{role}: boot — HTTPS_PROXY={'set' if _proxy else 'unset'} "
        f"NO_PROXY={env.get('NO_PROXY') or env.get('no_proxy') or '<unset>'} "
        f"DEBUG={'on' if _dbg else 'off'}\n"
    )
    daemon = build_role(role, env)
    # ISSUE_AGENT_MAX_TICKS bounds the loop (the demo-gate / a one-shot run); unset
    # = run forever like the bash daemons.
    raw = env.get("ISSUE_AGENT_MAX_TICKS")
    max_ticks = int(raw) if raw else None
    # Hot reload (HOT_RELOAD=0 disables): a change to the harness's HEAD
    # commit (never a mid-edit save — commit-gated so partial changes cannot
    # load) or to the composed env file restarts this daemon at the next
    # between-ticks point via point-at's relaunch loop.
    reload_check = None
    if os.environ.get("HOT_RELOAD", "1") != "0":
        from ..adapters.reload import ReloadWatch

        harness_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        watch = ReloadWatch(
            harness_root,
            files=tuple(p for p in (os.environ.get("AGENT_ENV"),) if p),
        )
        reload_check = watch.changed
    daemon.run_forever(
        sleep=time.sleep, now=lambda: int(time.time()), max_ticks=max_ticks,
        reload_check=reload_check,
    )


if __name__ == "__main__":
    main()
