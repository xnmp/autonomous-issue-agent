"""Infrastructure adapter for the dashboard's local start action.

`start` goes through point-at (which composes env, runs doctor and traps all
three watchers). GitHub label writes live in ``operator_effects``.

Every action is reversible from the target repo's own UI. Nothing here writes
harness state, edits config, or touches a worktree.
"""

from __future__ import annotations

import os
import subprocess

def start_command(harness_root: str, name: str) -> list[str]:
    """Detached launcher. `setsid` matters: the daemons must outlive the HTTP
    request that started them, and `point-at.sh` runs them in the foreground
    under one trap.

    `bin/point-at.sh` directly, NOT `make point-at`: make expands a command-line
    variable into the recipe TEXT, which it then hands to /bin/sh, so a `"` in
    `name` escapes into a shell command. Callers must still validate `name`
    against the known deployments — this is defence in depth, not the gate.
    """
    return ["setsid", os.path.join(harness_root, "bin", "point-at.sh"), name]


def start_log_path(log_dir: str, name: str) -> str:
    return os.path.join(log_dir, f"dashboard-start-{name}.log")


def launch_detached(argv: list[str], log_path: str) -> tuple[bool, str]:
    """Start the launcher with its output appended to `log_path`. The process is
    deliberately not waited on — `make point-at` runs until Ctrl-C."""
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            subprocess.Popen(
                argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except (OSError, ValueError) as exc:
        return False, f"could not launch: {exc}"
    return True, log_path
