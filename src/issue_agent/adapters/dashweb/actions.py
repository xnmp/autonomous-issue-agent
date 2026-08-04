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
# The three labels that hold an issue on a human. Unblock removes them and
# atomically restores the selectable issue/PR trigger when the hold replaced
# one; advisory budget holds leave an existing lifecycle stage unchanged.
UNBLOCK_LABELS = ("needs-human-review", "needs-budget-review", "ai-blocked")
SELECTABLE_ISSUE_LABELS = ("ai-ready", "ai-working", "ai-pr-open")
# Park: the operator hands the issue's PR(s) to a human and frees the queue.
# ai-pr-open is what keeps a PR inside the fixer's overlap serializer, so
# removing it is the whole mechanism; ai-parked records the decision.
PARKED_LABEL = "ai-parked"
PARKED_COLOR = "6a737d"
# Labels a park strips from issue and PRs: the lifecycle/attention set that
# would otherwise keep the target rendered as harness-owned or alarmed.
PARK_REMOVES = ("ai-pr-open", "ai-working", *UNBLOCK_LABELS)


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


def issue_unblock_change(
    held: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Re-arm an issue whose lifecycle hold is being removed.

    Fix worlds contain only labelled pipeline issues.  Removing ``ai-blocked``
    or ``needs-human-review`` without restoring a selectable label therefore
    makes the issue invisible; there is no later decision that can infer its
    stage.  Budget review is advisory and may coexist with a healthy lifecycle
    label, so clearing that label alone adds nothing.  The infrastructure
    adapter conditionally suppresses this fallback when its revision-bound
    read sees another selectable lifecycle label.
    """
    lifecycle_hold = {"ai-blocked", "needs-human-review"}
    add = (
        ("ai-ready",)
        if lifecycle_hold.intersection(held)
        else ()
    )
    return add, UNBLOCK_LABELS


def pr_unblock_change(
    held: tuple[str, ...], trigger_label: str, hold_role: str = ""
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Re-arm a held PR without relying on rogue-PR adoption.

    ``ai-blocked`` is a fixer hold, so it returns to ``ai-pr-open``.  A terminal
    human escalation can come from either role; restore the trigger removed by
    the durable escalation event.  On a custom-trigger deployment, missing
    provenance is unsafe to guess because the wrong choice leaves one role
    blind.  ``needs-budget-review`` is advisory and does not itself replace a
    trigger.
    """
    if "needs-human-review" in held:
        if hold_role == "fix":
            add = ("ai-pr-open",)
        elif hold_role == "review":
            add = (trigger_label,)
        elif trigger_label == "ai-pr-open":
            # Both roles share the same arm, so provenance cannot affect the
            # result and an older deployment without events is recoverable.
            add = ("ai-pr-open",)
        else:
            raise ValueError(
                "needs-human-review source is unknown; cannot choose between "
                "fixer label 'ai-pr-open' and reviewer label "
                f"'{trigger_label}'"
            )
    elif "ai-blocked" in held:
        add = ("ai-pr-open",)
    else:
        add = ()
    return add, tuple(held)


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
