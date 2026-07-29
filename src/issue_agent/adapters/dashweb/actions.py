"""Infrastructure adapter for the dashboard's three POST actions.

Each one shells out exactly as the operator would, so nothing here reimplements
harness logic: `start` goes through `make point-at` (which composes env, runs
doctor and traps all three watchers), and the label actions go through `gh`.
The argv is built by a pure function and executed by a thin runner, so the tests
assert on the command without ever running it.

Every action is reversible from the target repo's own UI. Nothing here writes
harness state, edits config, or touches a worktree.
"""

from __future__ import annotations

import os
import subprocess

PRIORITY_LABELS = ("P1", "P2", "P3")
# Neutral-to-hot, so P1 reads as the urgent one in the GitHub UI too.
PRIORITY_COLORS = {"P1": "b60205", "P2": "d93f0b", "P3": "fbca04"}
ACTION_TIMEOUT_S = 60.0


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


def promote_command(repo: str, issue: int) -> list[str]:
    return ["gh", "issue", "edit", str(issue), "-R", repo, "--add-label", "ai-ready"]


def label_create_command(repo: str, label: str) -> list[str]:
    return [
        "gh", "label", "create", label, "-R", repo,
        "--color", PRIORITY_COLORS.get(label, "ededed"),
        "--description", f"harness priority {label}",
    ]


def priority_commands(repo: str, issue: int, priority: str | None) -> list[list[str]]:
    """Create-on-miss then one edit that swaps the labels.

    One edit rather than a remove-then-add pair: two calls can leave an issue
    with no priority at all if the second fails, and the swap is the state the
    operator asked for, not the intermediate.
    """
    if priority is not None and priority not in PRIORITY_LABELS:
        raise ValueError(f"priority must be one of {PRIORITY_LABELS} or null")
    commands = []
    if priority:
        commands.append(label_create_command(repo, priority))
    edit = ["gh", "issue", "edit", str(issue), "-R", repo]
    for label in PRIORITY_LABELS:
        if label != priority:
            edit += ["--remove-label", label]
    if priority:
        edit += ["--add-label", priority]
    commands.append(edit)
    return commands


# ── runners ───────────────────────────────────────────────────────────────────
def run_command(argv: list[str]) -> tuple[int, str]:
    """(returncode, message). Never raises."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=ACTION_TIMEOUT_S
        )
    except FileNotFoundError:
        return 127, f"{argv[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} timed out"
    except OSError as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stderr or proc.stdout or "").strip()[:400]


def run_label_commands(commands, run=None) -> tuple[bool, str]:
    """Run a create-on-miss sequence: only the LAST command's failure is fatal.

    `gh label create` fails when the label already exists, which is the normal
    case and not an error — the edit that follows is the action.
    """
    runner = run or run_command
    last_index = len(commands) - 1
    for index, argv in enumerate(commands):
        code, message = runner(argv)
        if code != 0 and index == last_index:
            return False, message or f"command failed (exit {code})"
    return True, "ok"


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
