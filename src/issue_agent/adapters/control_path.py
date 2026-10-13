"""ADR 0008 filesystem scope for the shared control authority."""

from __future__ import annotations

import os
import pwd
import tempfile
from collections.abc import Iterable


class ControlAuthorityConflict(ValueError):
    """A deployment-local database would split the per-user authority."""


def machine_control_authority_root(*, user_home: str | None = None) -> str:
    """Return the stable per-user root shared by every repository deployment."""

    home = user_home
    if home is None:
        home = pwd.getpwuid(os.getuid()).pw_dir
    root = os.path.abspath(home)
    if not home or root == os.path.sep:
        raise ValueError("a non-root operating-system user home is required")
    return os.path.join(root, ".local", "state")


def canonical_control_store_path(*, authority_root: str | None = None) -> str:
    if authority_root == "":
        raise ValueError("control authority root cannot be empty")
    root = machine_control_authority_root() if authority_root is None else os.path.abspath(authority_root)
    return os.path.join(root, "issue-agent-control", "control.db")


def control_store_path(
    state_dirs: Iterable[str],
    *,
    fallback_state_dir: str = "",
    temporary_on_missing: bool = True,
    authority_root: str | None = None,
) -> str:
    configured = tuple(os.path.abspath(path) for path in state_dirs if path)
    if configured:
        deployment_root = os.path.commonpath(configured)
    else:
        deployment_root = os.path.dirname(os.path.abspath(fallback_state_dir)) if fallback_state_dir else ""
    if not deployment_root:
        if not temporary_on_missing:
            raise ValueError("at least one deployment state directory is required")
        deployment_root = tempfile.mkdtemp(prefix="issue-agent-control-")
        authority_root = deployment_root
    if authority_root == "" and deployment_root == os.path.sep:
        raise ValueError(
            "deployment state directories must share a non-root parent when no machine control authority is configured"
        )

    selected_root = machine_control_authority_root() if authority_root is None else authority_root or deployment_root
    selected = canonical_control_store_path(authority_root=selected_root)
    legacy = os.path.join(deployment_root, "issue-agent-control", "control.db")
    if (
        deployment_root != os.path.sep
        and os.path.realpath(legacy) != os.path.realpath(selected)
        and os.path.exists(legacy)
    ):
        raise ControlAuthorityConflict(
            "legacy deployment-local control database would split the "
            f"machine-wide accounting authority: {legacy}; migrate it into "
            f"{selected} before restarting"
        )
    return selected
