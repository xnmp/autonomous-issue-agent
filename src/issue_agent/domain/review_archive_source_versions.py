"""Canonical mutation revisions for review archive source groups."""

from __future__ import annotations

from dataclasses import dataclass

from .workflow_identity import canonical_workflow_repository


MAX_ARCHIVE_SOURCE_VERSION = 9223372036854775806


@dataclass(frozen=True)
class ReviewArchiveSourceVersions:
    repository: str
    round_rows: int
    all_round_rows: int
    round_receipts: int
    generation_sources: int
    checkpoint_sources: int
    workflow_rows: int

    def __post_init__(self) -> None:
        if (canonical_workflow_repository(self.repository) != self.repository
                or any(type(value) is not int or not 0 <= value <= MAX_ARCHIVE_SOURCE_VERSION
                       for value in (
                           self.round_rows, self.all_round_rows, self.round_receipts,
                           self.generation_sources, self.checkpoint_sources, self.workflow_rows,
                       ))):
            raise ValueError("archive source versions require canonical bounded witnesses")
