"""ADR 0010 wake-only webhook forwarder.

Runs `gh webhook forward` as a child process and turns every delivery into a
WAKE for the three role daemons: touch each role's wake file, nothing else.

The payload is deliberately never parsed. Acting on webhook contents would make
the harness event-driven, and an event-driven harness inherits every dropped,
duplicated and out-of-order delivery GitHub documents as possible. A wake only
shortens a sleep; the daemon then does exactly what it does on a poll — build
the whole world and reconcile it. That keeps the stateless reconcile the single
source of truth, and makes a lost delivery cost latency (up to
RECONCILE_SECONDS) rather than a missed issue.

Failure posture: if `gh webhook forward` is missing (it is a gh extension and
needs admin:repo_hook) or dies for any reason, we say so LOUDLY on stderr and
exit non-zero. `bin/point-at.sh` supervises the restart with a backoff, and the
daemons keep working at the reconcile cadence — i.e. today's behaviour, slower.
"""

from __future__ import annotations

import os
import subprocess
import sys

from ..adapters.operator_effects import build_operator_effects
from ..adapters.wake import touch_wake, wake_path
from ..adapters.single_instance import AlreadyRunning, UnsafeLockFile, acquire
from .config import from_env

# The events that can change any role's decision: issue/PR lifecycle and
# labels, review verdicts, comments, and CI outcomes. Overridable
# (WEBHOOK_EVENTS, comma-separated) but not per-role — one forwarder feeds all
# three, and a wake is cheap.
DEFAULT_EVENTS = (
    "issues,pull_request,issue_comment,pull_request_review,check_suite,status"
)


def _log(msg: str) -> None:
    sys.stderr.write(f"webhook: {msg}\n")
    sys.stderr.flush()


def run(
    env: dict | None = None,
    spawn=subprocess.Popen,
    gh_run=subprocess.run,
    effects_factory=build_operator_effects,
) -> int:
    """Forward deliveries into wake touches until the child exits. Returns the
    exit status to propagate (never 0 — the forwarder is meant to run forever,
    so any return is a death the supervisor should restart)."""
    env = dict(os.environ) if env is None else env
    if env.get("AGENT_ENV"):
        # Same composed-overlay source as the daemons (a systemd unit sets only
        # AGENT_ENV, so the state dirs are not otherwise in the environment).
        # Imported lazily: the loader lives with the daemon entrypoint, and the
        # forwarder must still start if that import graph is unavailable.
        try:
            from .main import _load_agent_env

            env = _load_agent_env(env)
        except Exception as exc:  # noqa: BLE001 - degrade to the inherited env
            _log(f"could not load AGENT_ENV ({exc}) — using the inherited environment")
    cfg = from_env(env)
    if not (cfg.repo or "").strip():
        _log("REPO is unset — refusing to start (is the repo overlay composed?)")
        return 2
    try:
        instance_lock = acquire(
            "webhook",
            cfg.repo,
            target_dir=cfg.target_dir,
            state_dir=cfg.state_dir,
        )
    except (AlreadyRunning, UnsafeLockFile, OSError) as exc:
        _log(str(exc))
        return 3
    events = env.get("WEBHOOK_EVENTS") or DEFAULT_EVENTS
    wakes = [
        wake_path(d)
        for d in (cfg.state_dir, cfg.review_state_dir, cfg.steward_state_dir)
        if d
    ]
    try:
        state_dirs = tuple(
            path
            for path in (
                cfg.state_dir,
                cfg.review_state_dir,
                cfg.steward_state_dir,
            )
            if path
        )
        _log(f"forwarding {events} for {cfg.repo} → {len(wakes)} role wake files")
        try:
            effects = effects_factory(
                cfg.repo,
                state_dirs,
                identity=f"{cfg.repo}:webhook",
                run=gh_run,
            )
            proc = effects.start_relay_forward(
                events,
                spawn=spawn,
            )
        except Exception as exc:  # noqa: BLE001 - poll reconciliation stays live
            _log(
                f"could not start the durable relay forwarder: {exc} — is the gh webhook "
                "extension installed (`gh extension install cli/gh-webhook`) and "
                "does the token carry admin:repo_hook? Daemons continue at the "
                "reconcile cadence."
            )
            return 127

        deliveries = 0
        if proc.stdout is not None:
            for line in proc.stdout:
                if not line.strip():
                    continue
                # PAYLOAD IGNORED BY DESIGN — a line is an edge, not data.
                deliveries += 1
                for path in wakes:
                    touch_wake(path)
        rc = proc.wait()
        _log(
            f"`gh webhook forward` EXITED (status {rc}) after {deliveries} deliveries "
            "— push wake is DOWN; daemons fall back to the reconcile cadence until "
            "the supervisor restarts it"
        )
        return rc if rc != 0 else 1
    finally:
        instance_lock.release()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
