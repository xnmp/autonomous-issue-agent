"""Entry point — `python -m issue_agent.app.main <fix|review|steward>`.

Replaces `make run-fix` / `run-review` / `run-steward` (agent/watcher.sh etc.).
Builds the identity (fixer = ambient/PAT; review+steward = GitHub App token), the
adapters, the governor hook, and runs the chosen role's daemon forever.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from ..adapters.agent_runner import AgentRunner
from ..adapters.app_token import AppCredentials, AppTokenMinter, default_mint_fn
from ..adapters.github import GitHubGateway, Identity
from ..adapters.governor import build_hooks
from ..adapters.single_instance import AlreadyRunning, acquire
from ..adapters.store import Store
from ..adapters.worktree import Worktrees, build_gate_context
from .config import HarnessConfig, from_env, render_agent_md
from .roles import build_fix_daemon, build_review_daemon, build_steward_daemon


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


def _app_identity(cfg_env: dict) -> Identity:
    """review/steward identity: GitHub App installation token if configured, else
    a static bot token, else ambient. Mirrors gh_review/gh_steward priority."""
    if cfg_env.get("GH_APP_ID") and cfg_env.get("GH_APP_KEY_FILE") and cfg_env.get("GH_APP_INSTALL_ID"):
        creds = AppCredentials(
            app_id=cfg_env["GH_APP_ID"],
            install_id=cfg_env["GH_APP_INSTALL_ID"],
            key_file=cfg_env["GH_APP_KEY_FILE"],
            cache_path=cfg_env.get("GH_APP_TOKEN_CACHE", os.path.expanduser("~/.local/state/issue-agent-bot/app-token.json")),
        )
        minter = AppTokenMinter(creds, default_mint_fn, now_fn=lambda: int(time.time()))
        return Identity(token_provider=minter.get)
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
    env = _load_agent_env(env if env is not None else dict(os.environ))
    cfg = from_env(env)
    cfg.validate()
    if cfg.target_dir and os.path.isdir(cfg.target_dir):
        os.chdir(cfg.target_dir)
    # Harness root (…/src/issue_agent/app/main.py → repo root): resolves relative
    # FIX_MCP_CONFIG paths. Honour an explicit HARNESS_ROOT_DIR override if set.
    harness_root = cfg.harness_root or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    cfg = HarnessConfig(**{
        **cfg.__dict__,
        "agent_md": _agent_md(cfg),
        "review_prompt": _review_prompt(cfg),
        "harness_root": harness_root,
    })

    target_dir = cfg.target_dir or os.getcwd()

    # The governor hook needs a GateContext built from the agent's worktree cwd.
    def build_ctx(cwd: str):
        return build_gate_context(
            cwd, base_branch=cfg.base_branch,
            env_test_cmd=env.get("TEST_CMD"),
            red_must_fail=env.get("RED_MUST_FAIL", "1") != "0",
            verify_red_at_commit=env.get("VERIFY_RED_AT_COMMIT", "1") != "0",
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
    runner = AgentRunner(hooks=hooks, log=_commit_log, debug=debug, idle_timeout=_idle)
    worktrees = Worktrees(target_dir, cfg.worktree_root, cfg.base_branch)

    if role == "fix":
        gw = GitHubGateway(cfg.repo, Identity(static_token=env.get("FIX_BOT_TOKEN") or None))
        store = Store(cfg.state_dir)
        return build_fix_daemon(cfg, gw, store, worktrees, runner)
    if role == "review":
        gw = GitHubGateway(cfg.repo, _app_identity(env),
                           review_pr_label=cfg.review_trigger_label,
                           review_adopt_unlabeled=cfg.review_adopt_unlabeled)
        store = Store(cfg.review_state_dir)
        return build_review_daemon(cfg, gw, store, runner)
    if role == "steward":
        gw = GitHubGateway(cfg.repo, _app_identity(env))
        store = Store(cfg.steward_state_dir)
        return build_steward_daemon(cfg, gw, store, runner)
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
    daemon.run_forever(sleep=time.sleep, now=lambda: int(time.time()), max_ticks=max_ticks)


if __name__ == "__main__":
    main()
