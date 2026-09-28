"""Observational legacy death and counter attribution for execution import."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .review_execution_activation import (
    AttributedLegacyReviewDeath, CompleteLegacyReviewRuns, LegacyReviewRunNoDeath,
    MAX_SQL_ROWID, UnattributedLegacyReviewDeath, classify_legacy_review_death,
)
from .review_execution_counter_mapping import CompleteLegacyCounterMapping, LegacyCounterAssignment
from .workflow_identity import validate_workflow_integer, validate_workflow_text


@dataclass(frozen=True)
class ObservedLegacyReviewDebit:
    """Current-owner key total with predecessor deaths retained separately."""

    pr: int
    files: tuple[LegacyCounterAssignment, ...]
    current_owner_runs: tuple[AttributedLegacyReviewDeath, ...]
    predecessor_runs: tuple[AttributedLegacyReviewDeath, ...]

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "legacy debit PR", minimum=1)
        if (not isinstance(self.files, tuple) or not isinstance(self.current_owner_runs, tuple)
                or not isinstance(self.predecessor_runs, tuple)
                or any(not isinstance(item, LegacyCounterAssignment) or item.pr != self.pr
                       for item in self.files)
                or any(not isinstance(item, AttributedLegacyReviewDeath) or item.pr != self.pr
                       for item in (*self.current_owner_runs, *self.predecessor_runs))
                or self.count > MAX_SQL_ROWID):
            raise ValueError("legacy debit requires bounded matching file and run sources")

    @property
    def count(self) -> int:
        return sum(item.counter.value.value for item in self.files) + len(self.current_owner_runs)


@dataclass(frozen=True)
class BlockedLegacyReviewDeathAttribution:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "blocked legacy death attribution")


def _derive(
    runs: CompleteLegacyReviewRuns, mapping: CompleteLegacyCounterMapping,
) -> tuple[tuple[ObservedLegacyReviewDebit, ...], tuple[AttributedLegacyReviewDeath, ...]] | BlockedLegacyReviewDeathAttribution:
    repository = mapping.links.repository
    if runs.lineage.repository != repository:
        return BlockedLegacyReviewDeathAttribution("review runs and counter mapping belong to different repositories")
    by_key: dict[int, list[int]] = {}
    for link in mapping.links.links:
        by_key.setdefault(-link.pr, []).append(link.pr)
        if link.linked_issue is not None:
            by_key.setdefault(link.linked_issue, []).append(link.pr)
    current_deaths: dict[int, list[AttributedLegacyReviewDeath]] = {}
    predecessor_deaths: dict[int, list[AttributedLegacyReviewDeath]] = {}
    unassigned: list[AttributedLegacyReviewDeath] = []
    for run in sorted(runs.runs, key=lambda item: item.run_id):
        classified = classify_legacy_review_death(run)
        if isinstance(classified, LegacyReviewRunNoDeath):
            continue
        if isinstance(classified, UnattributedLegacyReviewDeath):
            return BlockedLegacyReviewDeathAttribution(
                f"abandoned run {run.run_id[:128]} is unattributed: {classified.reason[:256]}",
            )
        if classified.repository != repository:
            return BlockedLegacyReviewDeathAttribution("abandoned review run belongs to another repository")
        selected = by_key.get(classified.counter_key, ())
        if len(selected) > 1:
            return BlockedLegacyReviewDeathAttribution("abandoned issue-key death selects multiple open PRs")
        if not selected:
            unassigned.append(classified)
        elif selected[0] != classified.pr:
            return BlockedLegacyReviewDeathAttribution("abandoned review death key now selects another open PR")
        else:
            target = current_deaths if run.owner_id == runs.lineage.current_owner else predecessor_deaths
            target.setdefault(classified.pr, []).append(classified)
    files: dict[int, list[LegacyCounterAssignment]] = {}
    for assignment in mapping.assignments:
        files.setdefault(assignment.pr, []).append(assignment)
    try:
        debits = tuple(ObservedLegacyReviewDebit(
            link.pr, tuple(files.get(link.pr, ())), tuple(current_deaths.get(link.pr, ())),
            tuple(predecessor_deaths.get(link.pr, ())),
        ) for link in mapping.links.links)
    except ValueError:
        return BlockedLegacyReviewDeathAttribution("legacy review debit exceeds the supported range")
    return debits, tuple(unassigned)


@dataclass(frozen=True)
class CompleteLegacyReviewDeathAttribution:
    """Every owner run and current counter key is accounted for observationally."""

    runs: CompleteLegacyReviewRuns
    mapping: CompleteLegacyCounterMapping
    debits: tuple[ObservedLegacyReviewDebit, ...]
    unassigned_deaths: tuple[AttributedLegacyReviewDeath, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.runs, CompleteLegacyReviewRuns)
                or not isinstance(self.mapping, CompleteLegacyCounterMapping)
                or not isinstance(self.debits, tuple)
                or not isinstance(self.unassigned_deaths, tuple)
                or _derive(self.runs, self.mapping) != (self.debits, self.unassigned_deaths)):
            raise ValueError("complete legacy death attribution disagrees with its sources")

    @property
    def source_digest(self) -> str:
        source = (self.runs.source_digest, self.mapping.source_digest)
        return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()


def attribute_complete_legacy_review_deaths(
    runs: CompleteLegacyReviewRuns, mapping: CompleteLegacyCounterMapping,
) -> CompleteLegacyReviewDeathAttribution | BlockedLegacyReviewDeathAttribution:
    """Match owner-lineage abandonments and file counters to open PR keys once.

    This matches the legacy reader's key selection. It does not prove a reset
    boundary, predecessor adoption into the current reader, the chronological effect
    of a productive review, or a reusable session. Those need separate source
    joins before import authority.
    """
    if not isinstance(runs, CompleteLegacyReviewRuns) or not isinstance(mapping, CompleteLegacyCounterMapping):
        raise ValueError("death attribution requires complete typed run and counter sources")
    derived = _derive(runs, mapping)
    if isinstance(derived, BlockedLegacyReviewDeathAttribution):
        return derived
    return CompleteLegacyReviewDeathAttribution(runs, mapping, *derived)
