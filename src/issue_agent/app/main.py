"""Entry point — `python -m issue_agent.app.main <fix|review>`.

Builds the identity (fixer = ambient/PAT; review = GitHub App token), the
adapters, and runs the chosen role's daemon forever.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import sys
import time
from collections.abc import Callable

from ..adapters.agent_runner import AgentRunner
from ..adapters.control_store import (
    CorruptControlStore,
    IncompatibleSchema,
    LegacySpendMigrationError,
    RetiredOwnerError,
)
from ..adapters.env_file import source_env_file
from ..adapters.github import GitHubGateway, Identity
from ..adapters.single_instance import AlreadyRunning, UnsafeLockFile, acquire
from ..adapters.systemd_notify import SystemdNotifier
from ..adapters.store import Store
from ..adapters.worktree import Worktrees
from ..domain.supervision import ExitCode
from ..domain.contracts import RunId
from ..domain.recovery import RecoveryArm, RecoveryTrigger, recovery_plan
from ..domain.repository import canonical_repository
from .config import HarnessConfig, from_env, render_agent_md
from .roles import (
    build_fix_daemon,
    build_review_daemon,
    migrate_legacy_spend,
    prior_process_supervision_state_dirs,
    process_supervision_state_dir,
)
from .review_identity import review_identity as _app_identity


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
    loaded = source_env_file(path, env)
    loaded["AGENT_ENV"] = path
    return loaded


def _recover_pre_adapter_processes(
    trigger: RecoveryTrigger,
    cfg: HarnessConfig,
    role: str,
    state_dir: str,
) -> tuple[RunId, ...]:
    """Run ADR 0006's pre-adapter process phase for every owned scope."""

    if (
        RecoveryArm.REAP_ALL_PROCESSES
        not in recovery_plan(trigger).preflight_arms
    ):
        return ()

    from ..adapters.process_supervisor import ProcessSupervisor

    legacy = ProcessSupervisor(state_dir).recover_abandoned_orphans()
    repointed = tuple(
        run_id
        for prior_state_dir in prior_process_supervision_state_dirs(
            cfg, role, state_dir
        )
        for run_id in ProcessSupervisor(prior_state_dir).recover_orphans()
    )
    current = ProcessSupervisor(
        process_supervision_state_dir(cfg, role, state_dir)
    ).recover_orphans()
    return (*legacy, *repointed, *current)


def _validate_reload_candidate(env: dict[str, str]) -> None:
    candidate = from_env(_load_agent_env(env))
    candidate.validate()


class _TerminationHandler:
    """First SIGTERM requests orderly drain; a second forces boot recovery."""

    def __init__(self, hard_exit: Callable[[int], None] = os._exit):
        self._hard_exit = hard_exit
        self._requested = False

    def __call__(self, _signum, _frame) -> None:
        if self._requested:
            self._hard_exit(int(ExitCode.TRANSIENT))
            return
        self._requested = True
        raise SystemExit(ExitCode.CLEAN)


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


def build_role(
    role: str,
    env: dict | None = None,
    scheduler_progress: Callable[[], None] | None = None,
):
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
    runner_state_dir = {
        "fix": cfg.state_dir,
        "review": cfg.review_state_dir,
    }[role]

    # Resolved-config print (lean P5.1): every daemon start states what it
    # ACTUALLY loaded — the only structural defense against the silent-default
    # class (#16/#49/#50/#78).
    for line in cfg.describe().splitlines():
        sys.stderr.write(f"{role}: config: {line}\n")
    sys.stderr.flush()

    # Runner diagnostics, tagged with the role so they interleave with the
    # daemon's own log lines.
    def _commit_log(msg: str) -> None:
        sys.stderr.write(f"{role}: {msg}\n")
        sys.stderr.flush()

    # ISSUE_AGENT_DEBUG=1 turns on the deep trace: every SDK stream message
    # (runner). This is the
    # instrument for the "Stream closed" wedge — off by default (verbose), on when
    # diagnosing. Boot logs whether it's active so the log is self-describing.
    debug = env.get("ISSUE_AGENT_DEBUG", "0") not in ("", "0", "false", "False")
    _idle = cfg.agent_idle_timeout if cfg.agent_idle_timeout > 0 else None
    # Two independent caps: silence (idle) and total elapsed (wall). The wall cap is
    # what bounds a limping post-suspend stream, which never goes silent enough to
    # trip the idle watchdog. 0 on either knob disables that one only.
    _wall = cfg.agent_max_runtime if cfg.agent_max_runtime > 0 else None
    from ..adapters.process_supervisor import ProcessSupervisor

    runner = AgentRunner(
        log=_commit_log, debug=debug,
        idle_timeout=_idle, max_runtime=_wall,
        max_buffer_size=cfg.sdk_max_buffer_bytes or None,
        process_supervisor=ProcessSupervisor(
            process_supervision_state_dir(cfg, role, runner_state_dir)
        ),
    )
    worktrees = Worktrees(target_dir, cfg.worktree_root, cfg.base_branch)

    if role == "fix":
        gw = GitHubGateway(
            cfg.repo,
            Identity(static_token=env.get("FIX_BOT_TOKEN") or None),
            progress=scheduler_progress,
        )
        store = Store(cfg.state_dir)
        return build_fix_daemon(cfg, gw, store, worktrees, runner)
    if role == "review":
        gw = GitHubGateway(
            cfg.repo,
            _app_identity(env, warn=_commit_log),
            progress=scheduler_progress,
        )
        store = Store(cfg.review_state_dir)
        return build_review_daemon(cfg, gw, store, runner)
    raise SystemExit(f"unknown role: {role!r} (use fix|review)")


def _role(argv: list[str] | None) -> str:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in ("fix", "review"):
        raise SystemExit(ExitCode.INVALID_CONFIG)
    return argv[0]


def main(argv: list[str] | None = None) -> None:
    """In-process entry: the drain handler is scoped to this call.

    Callers such as test workers must not inherit it; a leaked handler turned
    a stray SIGTERM into SystemExit(0) instead of a visible crash (#249).
    """
    role = _role(argv)
    previous = signal.signal(signal.SIGTERM, _TerminationHandler())
    try:
        _serve(role)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _process_main() -> None:
    # The daemon process keeps the handler through interpreter teardown, so a
    # second SIGTERM while executor threads are joined still hard-exits with
    # TRANSIENT rather than dying as signal 15.
    role = _role(None)
    signal.signal(signal.SIGTERM, _TerminationHandler())
    _serve(role)


def _serve(role: str) -> None:
    # Single-instance guard: refuse to start a second daemon of the same (role,
    # repo). A duplicate would poll independently and double-dispatch (two fixers
    # both grab ai-ready), breaking the single-fixer invariant. flock auto-frees
    # on process death, so there's no stale-lock hazard. Held for the whole run
    # via `_lock` (the open fd IS the lock; GC/exit releases it).
    try:
        env = _load_agent_env(dict(os.environ))
    except ValueError as exc:
        sys.stderr.write(f"{role}: invalid configuration — {exc}\n")
        raise SystemExit(ExitCode.INVALID_CONFIG) from exc
    # review C5(b): export the composed config into THIS process's environment
    # before building anything. No adapter subprocess call passes `env=`
    # explicitly, so every shell-out downstream (git, gh,
    # codex) inherits os.environ as it stood at spawn time. Foreground
    # `point-at.sh` happens to `set -a`-source the same files into its own
    # shell first, so dev mode worked by accident; systemd units set only
    # AGENT_ENV + TARGET_DIR, so without this update subprocesses read an
    # empty environment and operator configuration was honoured in dev and silently
    # ignored under the service. `update()` (not a fresh dict) matches
    # foreground `set -a` sourcing semantics: the overlay wins over whatever
    # the service unit already set, inherited env stays for anything the
    # overlay didn't touch.
    os.environ.update(env)
    repo = canonical_repository(env.get("REPO", ""))
    state_dir_key = {
        "fix": "STATE_DIR",
        "review": "REVIEW_STATE_DIR",
    }[role]
    try:
        _lock = acquire(  # noqa: F841 — kept alive for process lifetime
            role,
            repo,
            target_dir=env.get("TARGET_DIR", ""),
            state_dir=env.get(state_dir_key, ""),
        )
    except AlreadyRunning as e:
        sys.stderr.write(f"{role}: refusing to start — {e}\n")
        raise SystemExit(ExitCode.DUPLICATE_INSTANCE) from e
    except (ValueError, UnsafeLockFile) as e:
        sys.stderr.write(f"{role}: invalid deployment lock — {e}\n")
        raise SystemExit(ExitCode.INVALID_CONFIG) from e

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
    notifier = SystemdNotifier(env)

    def scheduler_progress() -> None:
        notifier.progress(f"{role} scheduler progressed")

    try:
        cfg = from_env(env)
        cfg.validate()
        state_dir = {
            "fix": cfg.state_dir,
            "review": cfg.review_state_dir,
        }[role]
        if state_dir:
            recovered = _recover_pre_adapter_processes(
                RecoveryTrigger.BOOT, cfg, role, state_dir
            )
            if recovered:
                sys.stderr.write(
                    f"{role}: recovered {len(recovered)} orphaned process "
                    "group(s) before adapter initialization\n"
                )
        daemon = build_role(role, env, scheduler_progress)
        imported = migrate_legacy_spend(cfg, state_dir)
        if imported:
            sys.stderr.write(
                f"{role}: imported {imported} legacy spend row(s) "
                "before paid admission\n"
            )
        if daemon.reconcile is None:
            raise ValueError(
                f"{role} daemon recovery trigger is not configured"
            )
        daemon.reconcile(RecoveryTrigger.BOOT)
    except IncompatibleSchema as exc:
        sys.stderr.write(f"{role}: incompatible control-store schema — {exc}\n")
        raise SystemExit(ExitCode.INCOMPATIBLE_SCHEMA) from exc
    except CorruptControlStore as exc:
        sys.stderr.write(f"{role}: corrupt control store — {exc}\n")
        raise SystemExit(ExitCode.STORE_CORRUPTION) from exc
    except LegacySpendMigrationError as exc:
        sys.stderr.write(f"{role}: operator repair required — {exc}\n")
        raise SystemExit(ExitCode.OPERATOR_REPAIR_REQUIRED) from exc
    except RetiredOwnerError as exc:
        sys.stderr.write(f"{role}: deployment owner retired — {exc}\n")
        raise SystemExit(ExitCode.CLEAN) from exc
    except ValueError as exc:
        sys.stderr.write(f"{role}: invalid configuration — {exc}\n")
        raise SystemExit(ExitCode.INVALID_CONFIG) from exc
    # ISSUE_AGENT_MAX_TICKS bounds the loop (the demo-gate / a one-shot run); unset
    # = run forever like the bash daemons.
    raw = env.get("ISSUE_AGENT_MAX_TICKS")
    try:
        max_ticks = int(raw) if raw else None
    except ValueError as exc:
        sys.stderr.write(
            f"{role}: invalid ISSUE_AGENT_MAX_TICKS={raw!r}\n"
        )
        raise SystemExit(ExitCode.INVALID_CONFIG) from exc
    # Hot reload (HOT_RELOAD=0 disables): a change to the harness's HEAD
    # commit (never a mid-edit save — commit-gated so partial changes cannot
    # load) or to the composed env file restarts this daemon through point-at's
    # relaunch loop. The concurrent fixer stops admitting work, keeps observing,
    # and restarts after its paid dispatches drain.
    reload_check = None
    if os.environ.get("HOT_RELOAD", "1") != "0":
        from ..adapters.reload import ReloadWatch

        harness_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        watch = ReloadWatch(
            harness_root,
            files=tuple(p for p in (os.environ.get("AGENT_ENV"),) if p),
            validate=lambda: _validate_reload_candidate(dict(os.environ)),
        )
        reload_check = watch.changed
    notifier.ready(f"{role} daemon initialized")
    hard_exit_code: int | None = None
    try:
        daemon.run_forever(
            sleep=time.sleep, now=lambda: int(time.time()), max_ticks=max_ticks,
            reload_check=reload_check,
            scheduler_progress=scheduler_progress,
        )
    except SystemExit:
        if daemon.requires_hard_exit:
            hard_exit_code = int(ExitCode.TRANSIENT)
        else:
            raise
    finally:
        notifier.stopping(f"{role} daemon stopped")
    if hard_exit_code is not None:
        # ThreadPoolExecutor workers are intentionally non-daemon. If a runtime
        # ignored cancellation after the application deadline, normal Python
        # shutdown would wait for it forever and cross TimeoutStopSec.
        os._exit(hard_exit_code)


if __name__ == "__main__":
    _process_main()
