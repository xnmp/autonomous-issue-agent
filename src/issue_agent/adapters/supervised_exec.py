"""Register a new process-group leader durably before executing paid work."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from ..domain.contracts import RunId
from .child_env import CHILD_ENV_MANIFEST, child_process_env
from .control_store import SQLiteControlStore
from .process_supervisor import ProcessSupervisor

_STATE_ENV = "ISSUE_AGENT_SUPERVISOR_STATE_DIR"
_RUN_ENV = "ISSUE_AGENT_SUPERVISOR_RUN_ID"
_TARGET_ENV = "ISSUE_AGENT_SUPERVISED_TARGET"
_PARENT_PID_ENV = "ISSUE_AGENT_SUPERVISOR_PARENT_PID"
_PARENT_START_ENV = "ISSUE_AGENT_SUPERVISOR_PARENT_START_TICKS"


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
    environment: Mapping[str, str],
) -> None:
    _require_live_launcher(environment)
    registration = ProcessSupervisor(state_dir).register(
        run_id,
        os.getpgrp(),
        leader_pid=os.getpid(),
    )
    control_db = environment.get("ISSUE_AGENT_CONTROL_DB", "")
    accounting_run_id = environment.get("ISSUE_AGENT_RUN_ID", "")
    if not control_db and not accounting_run_id:
        return
    if not control_db or accounting_run_id != str(run_id):
        raise RuntimeError(
            "supervised process identity does not match its accounting run"
        )
    SQLiteControlStore(control_db).register_process_lease(
        run_id,
        pid=registration.leader.pid,
        pgid=registration.pgid,
        process_start_ticks=registration.leader.start_ticks,
    )
    _require_live_launcher(environment)


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 3:
        raise SystemExit(
            "usage: supervised_exec STATE_DIR RUN_ID COMMAND [ARG ...]"
        )
    state_dir, raw_run_id, *command = arguments
    environment = dict(os.environ)
    _register(state_dir, RunId(raw_run_id), environment)
    environment.pop(_PARENT_PID_ENV, None)
    environment.pop(_PARENT_START_ENV, None)
    os.execvpe(command[0], command, environment)


def supervised_cli_main() -> None:
    """Become an SDK CLI session leader, register, then exec the real CLI."""

    environment = dict(os.environ)
    registration_environment = dict(environment)
    try:
        state_dir = environment.pop(_STATE_ENV)
        raw_run_id = environment.pop(_RUN_ENV)
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
    _register(state_dir, RunId(raw_run_id), registration_environment)
    os.execvpe(target, [target, *sys.argv[1:]], environment)


if __name__ == "__main__":
    main()
