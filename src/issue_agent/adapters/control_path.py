"""One filesystem location rule for the deployment-wide control authority."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable


def control_store_path(
    state_dirs: Iterable[str],
    *,
    fallback_state_dir: str = "",
    temporary_on_missing: bool = True,
) -> str:
    configured = tuple(
        os.path.abspath(path)
        for path in state_dirs
        if path
    )
    common = os.path.commonpath(configured) if configured else ""
    if not common or common == os.path.sep:
        common = (
            os.path.dirname(os.path.abspath(fallback_state_dir))
            if fallback_state_dir
            else ""
        )
    if not common:
        if not temporary_on_missing:
            raise ValueError("at least one deployment state directory is required")
        common = tempfile.mkdtemp(prefix="issue-agent-control-")
    return os.path.join(common, "issue-agent-control", "control.db")
