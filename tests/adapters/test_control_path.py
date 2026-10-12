from __future__ import annotations

import os

import pytest

from issue_agent.adapters.control_path import (
    ControlAuthorityConflict,
    canonical_control_store_path,
    control_store_path,
)


def test_role_state_directories_resolve_machine_control_database(tmp_path) -> None:
    authority = tmp_path / "machine-state"
    path = control_store_path(
        (
            "/deployment/state/fix",
            "/deployment/state/review",
        ),
        authority_root=str(authority),
    )

    assert path == str(authority / "issue-agent-control" / "control.db")


def test_custom_repository_roots_cannot_split_global_accounting(tmp_path) -> None:
    authority = tmp_path / "machine-state"
    first = control_store_path(
        ("/srv/repo-a/fix", "/srv/repo-a/review"),
        authority_root=str(authority),
    )
    second = control_store_path(
        ("/var/lib/repo-b/fix", "/var/lib/repo-b/review"),
        authority_root=str(authority),
    )

    assert first == second


def test_canonical_control_authority_rejects_empty_root() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        canonical_control_store_path(authority_root="")


def test_programmatic_legacy_fallback_rejects_disjoint_role_roots() -> None:
    with pytest.raises(ValueError, match="non-root parent"):
        control_store_path(
            ("/srv/issue-agent/fix", "/var/lib/issue-agent/review"),
            authority_root="",
        )


def test_single_role_fallback_is_used_only_when_no_role_paths_are_configured(
    tmp_path,
) -> None:
    state_dir = tmp_path / "fix"

    path = control_store_path(
        (),
        fallback_state_dir=str(state_dir),
        authority_root="",
    )

    assert path == os.path.join(
        str(tmp_path), "issue-agent-control", "control.db"
    )


def test_operator_effects_require_a_configured_deployment_path() -> None:
    with pytest.raises(ValueError, match="state directory"):
        control_store_path((), temporary_on_missing=False)


def test_existing_deployment_local_authority_fails_closed(tmp_path) -> None:
    deployment = tmp_path / "custom"
    legacy = deployment / "issue-agent-control" / "control.db"
    legacy.parent.mkdir(parents=True)
    legacy.touch()

    with pytest.raises(ControlAuthorityConflict, match="migrate it"):
        control_store_path(
            (
                str(deployment / "fix"),
                str(deployment / "review"),
            ),
            authority_root=str(tmp_path / "machine-state"),
        )
