"""Register a new process-group leader durably before executing paid work."""

from __future__ import annotations

import os
import sys

from ..domain.contracts import RunId
from .process_supervisor import ProcessSupervisor

_STATE_ENV = "ISSUE_AGENT_SUPERVISOR_STATE_DIR"
_RUN_ENV = "ISSUE_AGENT_SUPERVISOR_RUN_ID"
_TARGET_ENV = "ISSUE_AGENT_SUPERVISED_TARGET"


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 3:
        raise SystemExit(
            "usage: supervised_exec STATE_DIR RUN_ID COMMAND [ARG ...]"
        )
    state_dir, raw_run_id, *command = arguments
    supervisor = ProcessSupervisor(state_dir)
    supervisor.register(
        RunId(raw_run_id),
        os.getpgrp(),
        leader_pid=os.getpid(),
    )
    os.execvpe(command[0], command, os.environ)


def supervised_cli_main() -> None:
    """Become an SDK CLI session leader, register, then exec the real CLI."""

    environment = dict(os.environ)
    try:
        state_dir = environment.pop(_STATE_ENV)
        raw_run_id = environment.pop(_RUN_ENV)
        target = environment.pop(_TARGET_ENV)
    except KeyError as exc:
        raise SystemExit(f"missing supervised CLI environment: {exc}") from exc
    os.setsid()
    ProcessSupervisor(state_dir).register(
        RunId(raw_run_id),
        os.getpgrp(),
        leader_pid=os.getpid(),
    )
    os.execvpe(target, [target, *sys.argv[1:]], environment)


if __name__ == "__main__":
    main()
