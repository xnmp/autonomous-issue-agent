"""Barrier-held legacy file and PR-link sources for review execution import."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json

from ..domain.review_execution_counter_mapping import (
    BlockedLegacyCounterMapping, BlockedLegacyOpenCounterLinks, CompleteLegacyCounterMapping,
    CompleteLegacyOpenCounterLinks, map_legacy_execution_counters,
)
from ..domain.workflow_identity import canonical_workflow_repository, validate_workflow_text
from .control_store import SQLiteControlStore
from .deployment_authority import deployment_effect_barrier
from .resolve_activation import LegacyRoots
from .review_execution_activation import (
    BlockedLegacyExecutionFileValues, CompleteLegacyExecutionRootFiles,
    revalidate_complete_legacy_execution_file_values,
)
from .review_execution_counter_links import revalidate_complete_legacy_open_counter_links

_ROLES = ("fix", "review", "steward")


@dataclass(frozen=True)
class HeldLegacyExecutionCounterSources:
    """Complete file and PR-link observations rechecked under held barriers."""

    roots: CompleteLegacyExecutionRootFiles
    mapping: CompleteLegacyCounterMapping

    def __post_init__(self) -> None:
        if (not isinstance(self.roots, CompleteLegacyExecutionRootFiles)
                or not isinstance(self.mapping, CompleteLegacyCounterMapping)
                or self.roots.values != self.mapping.files):
            raise ValueError("held counter sources require the same complete file values")

    @property
    def source_digest(self) -> str:
        source = (self.roots.source_digest, self.mapping.source_digest)
        return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyExecutionCounterSources:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "blocked execution counter source")


@contextmanager
def held_legacy_execution_counter_sources(
    control: SQLiteControlStore, gateway: object, repository: str, roots: LegacyRoots,
    observed_files: CompleteLegacyExecutionRootFiles,
    observed_links: CompleteLegacyOpenCounterLinks,
) -> Iterator[HeldLegacyExecutionCounterSources | BlockedLegacyExecutionCounterSources]:
    """Recheck files and PR linkage while excluding all three role effects.

    A future import writer must keep this context open through its SQL commit.
    This result proves source identity and attribution, not historical debit or
    reset authority.
    """
    if not isinstance(control, SQLiteControlStore):
        raise ValueError("execution counter sources require a control store")
    repo = canonical_workflow_repository(repository)
    control.require_initialized()
    with ExitStack() as barriers:
        for role in _ROLES:
            barriers.enter_context(deployment_effect_barrier(control.path, repo, role))
        files = revalidate_complete_legacy_execution_file_values(roots, observed_files)
        if isinstance(files, BlockedLegacyExecutionFileValues):
            yield BlockedLegacyExecutionCounterSources(files.reason)
            return
        links = revalidate_complete_legacy_open_counter_links(control, gateway, repo, observed_links)
        if isinstance(links, BlockedLegacyOpenCounterLinks):
            yield BlockedLegacyExecutionCounterSources(links.reason)
            return
        mapping = map_legacy_execution_counters(files.values, links)
        if isinstance(mapping, BlockedLegacyCounterMapping):
            yield BlockedLegacyExecutionCounterSources(mapping.reason)
            return
        yield HeldLegacyExecutionCounterSources(files, mapping)
