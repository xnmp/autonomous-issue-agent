"""Infrastructure adapter for the dashboard's local start action.

`start` goes through point-at (which composes env, runs doctor and traps all
three watchers). GitHub mutations live in ``operator_effects`` so they cannot
bypass durable lifecycle commands and receipts.

Every action is reversible from the target repo's own UI. Nothing here writes
harness state, edits config, or touches a worktree.
"""

from __future__ import annotations

import os
import subprocess

PRIORITY_LABELS = ("P1", "P2", "P3")
# Neutral-to-hot, so P1 reads as the urgent one in the GitHub UI too.
PRIORITY_COLORS = {"P1": "b60205", "P2": "d93f0b", "P3": "fbca04"}


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


def priority_change(priority: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the atomic add/remove intent for one priority selection."""

    if priority is not None and priority not in PRIORITY_LABELS:
        raise ValueError(f"priority must be one of {PRIORITY_LABELS} or null")
    added = (priority,) if priority else ()
    removed = tuple(label for label in PRIORITY_LABELS if label != priority)
    return added, removed


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
