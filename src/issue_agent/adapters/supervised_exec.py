"""Register a new process-group leader durably before executing paid work."""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Mapping

from ..domain.contracts import RunId
from .child_env import (
    CHILD_ENV_MANIFEST,
    child_process_env,
    without_supervisor_authority,
)
from .control_store import SQLiteControlStore
from .process_supervisor import ProcessSupervisor

_STATE_ENV = "ISSUE_AGENT_SUPERVISOR_STATE_DIR"
_RUN_ENV = "ISSUE_AGENT_SUPERVISOR_RUN_ID"
_TOKEN_ENV = "ISSUE_AGENT_SUPERVISOR_LAUNCH_TOKEN"
_TARGET_ENV = "ISSUE_AGENT_SUPERVISED_TARGET"
_PARENT_PID_ENV = "ISSUE_AGENT_SUPERVISOR_PARENT_PID"
_PARENT_START_ENV = "ISSUE_AGENT_SUPERVISOR_PARENT_START_TICKS"
_FORWARDED_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
_FORWARDED_SIGNAL_GRACE_SECONDS = 1.0


def _require_live_launcher(environment: Mapping[str, str]) -> None:
    control_db = environment.get("ISSUE_AGENT_CONTROL_DB", "")
    raw_pid = environment.get(_PARENT_PID_ENV, "")
    raw_start = environment.get(_PARENT_START_ENV, "")
    if not control_db and not raw_pid and not raw_start:
        return
    try:
        pid = int(raw_pid)
        start_ticks = int(raw_start)
    except ValueError as exc:
        raise RuntimeError("supervised launcher identity is malformed") from exc
    identity = ProcessSupervisor.process_identity(pid)
    if identity is None or identity.start_ticks != start_ticks:
        raise RuntimeError(
            "supervised launcher exited before process registration"
        )


def _register(
    state_dir: str,
    run_id: RunId,
    token: str,
    environment: Mapping[str, str],
) -> None:
    _require_live_launcher(environment)
    supervisor = ProcessSupervisor(state_dir)
    registration = supervisor.register_prepared_launch(
        run_id, token, os.getpgrp(), leader_pid=os.getpid()
    )
    control_db = environment.get("ISSUE_AGENT_CONTROL_DB", "")
    accounting_run_id = environment.get("ISSUE_AGENT_RUN_ID", "")
    accounting_owner_id = environment.get("ISSUE_AGENT_OWNER_ID", "")
    if not control_db and not accounting_run_id and not accounting_owner_id:
        return
    if (
        not control_db
        or accounting_run_id != str(run_id)
        or not accounting_owner_id
    ):
        raise RuntimeError(
            "supervised process identity does not match its accounting run"
        )
    SQLiteControlStore(control_db).register_process_lease(
        run_id,
        expected_owner_id=accounting_owner_id,
        pid=registration.leader.pid,
        pgid=registration.pgid,
        process_start_ticks=registration.leader.start_ticks,
    )
    _require_live_launcher(environment)
    supervisor.require_registered_launch(run_id, token)


def _wait_for_target(pid: int) -> None:
    """Retain ancestor authority until the provider exits, preserving status."""

    while True:
        try:
            _pid, status = os.waitpid(pid, 0)
            break
        except InterruptedError:
            continue
    if os.WIFEXITED(status):
        raise SystemExit(os.WEXITSTATUS(status))
    if os.WIFSIGNALED(status):
        signum = os.WTERMSIG(status)
        if signum not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        raise SystemExit(128 + signum)
    raise SystemExit(1)


def _carry_authority_and_exec(
    command: list[str], environment: Mapping[str, str]
) -> None:
    """Apply ADR 0005: retain ancestor authority and scrub the provider."""

    prior_mask = signal.pthread_sigmask(
        signal.SIG_BLOCK,
        _FORWARDED_SIGNALS,
    )
    try:
        pid = os.fork()
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
        raise
    if pid == 0:
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
            child_environment = without_supervisor_authority(environment)
            os.execvpe(command[0], command, child_environment)
        except BaseException:
            os._exit(127)

    def forward_to_group(signum: int, _frame: object) -> None:
        for forwarded in _FORWARDED_SIGNALS:
            signal.signal(forwarded, signal.SIG_IGN)
        pgid = os.getpgrp()
        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + _FORWARDED_SIGNAL_GRACE_SECONDS
        targets = tuple(
            identity
            for identity in ProcessSupervisor.live_group_identities(pgid)
            if identity.pid != os.getpid()
        )
        while targets and time.monotonic() < deadline:
            time.sleep(0.02)
            targets = tuple(
                identity
                for identity in ProcessSupervisor.live_group_identities(pgid)
                if identity.pid != os.getpid()
            )
        if targets:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os._exit(128 + signal.SIGKILL)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        raise SystemExit(128 + signum)

    for signum in _FORWARDED_SIGNALS:
        signal.signal(signum, forward_to_group)
    signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
    _wait_for_target(pid)


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 4:
        raise SystemExit(
            "usage: supervised_exec STATE_DIR RUN_ID TOKEN COMMAND [ARG ...]"
        )
    state_dir, raw_run_id, token, *command = arguments
    environment = dict(os.environ)
    _register(state_dir, RunId(raw_run_id), token, environment)
    environment.pop(_PARENT_PID_ENV, None)
    environment.pop(_PARENT_START_ENV, None)
    _carry_authority_and_exec(command, environment)


def supervised_cli_main() -> None:
    """Become an SDK CLI session leader, register, then exec the real CLI."""

    environment = dict(os.environ)
    registration_environment = dict(environment)
    try:
        state_dir = environment.pop(_STATE_ENV)
        raw_run_id = environment.pop(_RUN_ENV)
        token = environment.pop(_TOKEN_ENV)
        target = environment.pop(_TARGET_ENV)
    except KeyError as exc:
        raise SystemExit(f"missing supervised CLI environment: {exc}") from exc
    explicit_keys = environment.pop(CHILD_ENV_MANIFEST, "").split()
    environment.pop(_PARENT_PID_ENV, None)
    environment.pop(_PARENT_START_ENV, None)
    environment = child_process_env(
        environment,
        additional_keys=explicit_keys,
    )
    os.setsid()
    _register(
        state_dir,
        RunId(raw_run_id),
        token,
        registration_environment,
    )
    _carry_authority_and_exec([target, *sys.argv[1:]], environment)


if __name__ == "__main__":
    main()
