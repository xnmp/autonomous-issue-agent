"""Entry point — `python -m issue_agent.app.main <fix|review|steward>`.

Builds the identity (fixer = ambient/PAT; review+steward = GitHub App token), the
adapters, the governor hook, and runs the chosen role's daemon forever.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
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
from ..adapters.app_token import AppCredentials, AppTokenMinter, default_mint_fn
from ..adapters.codex_hook import evidence_receipt_path
from ..adapters.github import GhUnavailable, GitHubGateway, Identity
from ..adapters.governor import build_hooks
from ..adapters.single_instance import AlreadyRunning, UnsafeLockFile, acquire
from ..adapters.systemd_notify import SystemdNotifier
from ..adapters.store import Store
from ..adapters.worktree import Worktrees, build_gate_context
from ..domain.world import WorldState, fix_probe_safe
from ..domain.supervision import ExitCode
from ..domain.contracts import RunId
from ..domain.repository import canonical_repository
from .config import HarnessConfig, describe_command, from_env, render_agent_md
from .roles import (
    build_fix_daemon,
    build_review_daemon,
    build_steward_daemon,
    migrate_legacy_spend,
    prior_process_supervision_state_dirs,
    process_supervision_state_dir,
)
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
    loaded = source_env_file(path, env)
    loaded["AGENT_ENV"] = path
    return loaded


def _ignore_log(_message: str) -> None:
    pass


def _recover_supervised_processes(
    cfg: HarnessConfig,
    role: str,
    state_dir: str,
) -> tuple[RunId, ...]:
    """Reap legacy, explicitly repointed, then current deployment registries."""

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


def _app_identity(
    cfg_env: dict, warn: Callable[[str], None] = _ignore_log
) -> Identity:
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

    Fall through AUDIBLY, and proportionately. Demoting to ambient silently would
    make review == FIX_BOT, and under IDENTITY_MODE=three every approval is then a
    GitHub 422 self-approval — a failure that surfaces far from its cause, so that
    case stays a warning. Falling through to a configured REVIEW_BOT_TOKEN is a
    working identity, not a fault; it states which identity is live instead (#46).
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
        # Two very different situations, and printing the SAME alarm for both
        # (#46) trains operators to ignore it. With a bot token configured the
        # static-token path is a supported, complete identity — the missing key
        # file is then almost always the shipped GH_APP_* placeholder, and the
        # line should confirm which identity is live, not read as a fault on
        # every boot of all three daemons. With no bot token the fallback is
        # ambient == FIX_BOT and every approval 422s: that stays loud.
        if cfg_env.get("REVIEW_BOT_TOKEN"):
            warn(
                "identity: static token (REVIEW_BOT_TOKEN) — GH_APP_KEY_FILE "
                f"'{key_file}' is not present, so the GitHub App identity is "
                "not in use"
            )
        else:
            warn(
                f"GH_APP_KEY_FILE '{key_file}' is missing or unreadable — ignoring "
                "the GitHub App identity and falling back to ambient `gh auth` "
                "(which is FIX_BOT — approvals will 422)"
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
        "steward": cfg.steward_state_dir,
    }[role]
    state_dir = runner_state_dir
    if state_dir:
        recovered = _recover_supervised_processes(cfg, role, state_dir)
        if recovered:
            sys.stderr.write(
                f"{role}: recovered {len(recovered)} orphaned process group(s) "
                "before adapter initialization\n"
            )

    # Resolved-config print (lean P5.1): every daemon start states what it
    # ACTUALLY loaded — the only structural defense against the silent-default
    # class (#16/#49/#50/#78). TEST_CMD/ALLOW_UNTESTED are env facts the gate
    # reads directly, so they're reported here alongside the config.
    for line in cfg.describe().splitlines():
        sys.stderr.write(f"{role}: config: {line}\n")
    sys.stderr.write(
        f"{role}: config: TEST_CMD={describe_command(env.get('TEST_CMD'))} "
        f"ALLOW_UNTESTED={env.get('ALLOW_UNTESTED', '0') or '0'}\n"
    )
    sys.stderr.flush()

    # The governor hook needs a GateContext built from the agent's worktree cwd.
    screenshot_receipt_key = secrets.token_bytes(32)

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
            screenshots_read_path=evidence_receipt_path(
                runner_state_dir, cwd
            ),
            screenshots_receipt_key=screenshot_receipt_key,
            durable_publish=True,
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
        build_ctx,
        edit_check_cmd=cfg.edit_check_cmd or None,
        log=_commit_log,
        debug=debug,
        trusted_state_dir=runner_state_dir,
        screenshot_receipt_key=screenshot_receipt_key,
    )
    _idle = cfg.agent_idle_timeout if cfg.agent_idle_timeout > 0 else None
    # Two independent caps: silence (idle) and total elapsed (wall). The wall cap is
    # what bounds a limping post-suspend stream, which never goes silent enough to
    # trip the idle watchdog. 0 on either knob disables that one only.
    _wall = cfg.agent_max_runtime if cfg.agent_max_runtime > 0 else None
    from ..adapters.process_supervisor import ProcessSupervisor

    runner = AgentRunner(
        hooks=hooks, log=_commit_log, debug=debug,
        idle_timeout=_idle, max_runtime=_wall,
        max_buffer_size=cfg.sdk_max_buffer_bytes or None,
        process_supervisor=ProcessSupervisor(
            process_supervision_state_dir(cfg, role, runner_state_dir)
        ),
    )
    worktrees = Worktrees(target_dir, cfg.worktree_root, cfg.base_branch)

    # Change-probe gate for the per-tick observe: 1 REST point on a quiet tick
    # instead of a full ~14-point GraphQL world build (the 2026-07-26 rate-limit
    # exhaustion — two daemons on one token at 20s polls). Review and steward are
    # gated unconditionally: their decide functions read nothing the signature is
    # blind to (the reviewer ignores checks; the steward never fetches PRs at
    # all). The FIX daemon is gated CONDITIONALLY, via `probe_when`: its decide
    # reads CI check states and mergeable/mergeStateStatus, which GitHub
    # recomputes WITHOUT bumping any updated_at — but only an in-flight PR or a
    # held issue produces those signals, so `fix_probe_safe` re-enables full
    # builds for exactly the worlds where the blind spot bites, and lets an idle
    # fixer coast on the cheap signature (new work always bumps updated_at).
    # Anything no signature can see (Jira-side edits the steward mirrors, when
    # sync is on) is picked up at the probe's TTL reconciliation cadence.
    def _probed(daemon, gw, probe_when: Callable[[WorldState], bool] | None = None):
        if not cfg.world_probe:
            return daemon
        probe = WorldProbe(
            gw.change_signature,
            float(cfg.world_probe_ttl_seconds),
            probe_when=probe_when,
        )
        # Daemon owns live pool state. The fix role's decision closure also
        # retains this exact instance to read capacity and in-flight paths, so
        # cloning it here would split observation from dispatch accounting.
        daemon.observe = probe.gate(daemon.observe)
        return daemon

    if role == "fix":
        gw = GitHubGateway(
            cfg.repo,
            Identity(static_token=env.get("FIX_BOT_TOKEN") or None),
            progress=scheduler_progress,
        )
        # runtime-tagged sessions: a FIXER_RUNTIME flip must cold-start resume
        # legs, not feed the new runtime the old runtime's session ids.
        store = Store(cfg.state_dir, runtime=cfg.fixer_runtime)
        # Server merge-gate probe (once per boot): only a positively verified
        # strict review+checks gate permits automated merging. Probed here
        # (not in from_env) because it needs the gateway; a flip of the repo's
        # protection therefore takes effect at the next daemon restart.
        if cfg.arm_on_open != "0":
            try:
                active = gw.merge_gate_protection(cfg.base_branch)
            except GhUnavailable as exc:
                active = False
                sys.stderr.write(
                    f"{role}: arm-on-open inactive — branch protection could "
                    f"not be verified ({exc}); automated merge is disabled\n"
                )
            cfg = dataclasses.replace(cfg, arm_on_open_active=active)
            sys.stderr.write(
                f"{role}: arm-on-open "
                + (
                    f"ACTIVE — {cfg.base_branch} protection verifiably requires "
                    "review + status checks + an up-to-date head\n"
                    if active else
                    f"inactive — {cfg.base_branch} protection does not gate "
                    "review, checks, and base freshness; automated merge disabled\n"
                )
            )
        return _probed(
            build_fix_daemon(cfg, gw, store, worktrees, runner), gw,
            probe_when=fix_probe_safe,
        )
    if role == "review":
        gw = GitHubGateway(
            cfg.repo,
            _app_identity(env, warn=_commit_log),
            review_pr_label=cfg.review_trigger_label,
            review_adopt_unlabeled=cfg.review_adopt_unlabeled,
            progress=scheduler_progress,
        )
        # A runtime flip must cold-start review legs too.  In particular, Codex
        # resume cannot preserve its read-only sandbox, and legacy Claude
        # session ids must never reach `codex exec resume`.
        store = Store(cfg.review_state_dir, runtime=cfg.reviewer_runtime)
        return _probed(build_review_daemon(cfg, gw, store, runner), gw)
    if role == "steward":
        gw = GitHubGateway(
            cfg.repo,
            _app_identity(env, warn=_commit_log),
            progress=scheduler_progress,
        )
        store = Store(cfg.steward_state_dir)
        return _probed(build_steward_daemon(cfg, gw, store, runner), gw)
    raise SystemExit(f"unknown role: {role!r} (use fix|review|steward)")


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        raise SystemExit(ExitCode.INVALID_CONFIG)
    role = argv[0]
    if role not in ("fix", "review", "steward"):
        raise SystemExit(ExitCode.INVALID_CONFIG)

    signal.signal(signal.SIGTERM, _TerminationHandler())

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
    repo = canonical_repository(env.get("REPO", ""))
    state_dir_key = {
        "fix": "STATE_DIR",
        "review": "REVIEW_STATE_DIR",
        "steward": "STEWARD_STATE_DIR",
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
        daemon = build_role(role, env, scheduler_progress)
        cfg = from_env(env)
        cfg.validate()
        state_dir = {
            "fix": cfg.state_dir,
            "review": cfg.review_state_dir,
            "steward": cfg.steward_state_dir,
        }[role]
        imported = migrate_legacy_spend(cfg, state_dir)
        if imported:
            sys.stderr.write(
                f"{role}: imported {imported} legacy spend row(s) "
                "before paid admission\n"
            )
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
    main()
