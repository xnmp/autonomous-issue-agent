"""Pristine first-entry verification composed with SQLite paid admission."""

from __future__ import annotations

import json
import os
import sqlite3

from ..domain.contracts import Money
from .control_store import (
    AccountingDisposition, CorruptControlStore, ReservationRequest, ReservationResult,
    RetiredOwnerError, SQLiteControlStore,
)
from .worktree import WorktreeUnavailable, Worktrees


class WorkspaceAdmission:
    """Verify the completed handoff exactly once before its first admitted run."""

    def __init__(
        self,
        control: SQLiteControlStore,
        worktrees: Worktrees,
        approved_host: str = "github.com",
    ) -> None:
        if not isinstance(approved_host, str) or not approved_host:
            raise ValueError("workspace admission requires an approved Git host")
        self._control = control
        self._worktrees = worktrees
        self._approved_host = approved_host

    @staticmethod
    def _unavailable(request: ReservationRequest, error: Exception) -> ReservationResult:
        detail = (str(error) or type(error).__name__).replace("\x00", "\\0")[:16384]
        return ReservationResult(
            AccountingDisposition.CORRUPT if isinstance(error, CorruptControlStore) else AccountingDisposition.UNAVAILABLE,
            None,
            Money.zero(request.scope.currency),
            request.quote.maximum_charge,
            request.cap,
            detail,
        )

    def __call__(self, request: ReservationRequest) -> ReservationResult:
        if not isinstance(request, ReservationRequest):
            raise ValueError("workspace admission requires a reservation request")
        if request.workflow is None:
            return self._control.reserve(request)
        try:
            # Ordinary and consumed episodes retain fail-fast shared admission.
            # The reservation transaction revalidates this optimistic read.
            if not self._control.requires_workspace_entry(request):
                return self._control.reserve(request)
            with self._control.workspace_entry_authority(request) as acknowledgement:
                if acknowledgement is None:
                    return self._control._reserve(request)
                with self._worktrees.verified_episode_entry(
                    acknowledgement, approved_host=self._approved_host,
                ) as cwd:
                    metadata = json.loads(request.dispatch_id)
                    if (
                        not isinstance(metadata, dict)
                        or not isinstance(metadata.get("cwd"), str)
                        or os.path.realpath(metadata["cwd"]) != os.path.realpath(cwd)
                        or metadata.get("publication_branch") != acknowledgement.handoff.branch
                    ):
                        raise ValueError(
                            "paid dispatch disagrees with its verified episode workspace"
                        )
                    return self._control._reserve(
                        request, workspace_verified=acknowledgement,
                    )
        except (TypeError, ValueError, WorktreeUnavailable, OSError, RetiredOwnerError, CorruptControlStore, sqlite3.DatabaseError) as exc:
            return self._unavailable(request, exc)
