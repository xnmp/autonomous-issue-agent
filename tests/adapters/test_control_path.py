from __future__ import annotations

import os

import pytest

from issue_agent.adapters.control_path import control_store_path


def test_role_state_directories_resolve_one_shared_control_database() -> None:
    path = control_store_path(
        (
            "/deployment/state/fix",
            "/deployment/state/review",
            "/deployment/state/steward",
        )
    )

    assert path == "/deployment/state/issue-agent-control/control.db"


def test_disjoint_role_state_directories_are_rejected_instead_of_split() -> None:
    with pytest.raises(ValueError, match="same control database"):
        control_store_path(
            ("/srv/issue-agent/fix", "/var/lib/issue-agent/review"),
            fallback_state_dir="/srv/issue-agent/fix",
        )


def test_single_role_fallback_is_used_only_when_no_role_paths_are_configured(
    tmp_path,
) -> None:
    state_dir = tmp_path / "fix"

    path = control_store_path((), fallback_state_dir=str(state_dir))

    assert path == os.path.join(
        str(tmp_path), "issue-agent-control", "control.db"
    )


def test_operator_effects_require_a_configured_deployment_path() -> None:
    with pytest.raises(ValueError, match="state directory"):
        control_store_path((), temporary_on_missing=False)
