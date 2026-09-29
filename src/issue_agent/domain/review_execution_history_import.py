"""Conservative, pure classification of preactivation review execution evidence.

The result describes what historical proof is missing. It never grants an
execution origin: even an empty observation needs a fresh, source-backed
activation decision at the infrastructure boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re

from .resolve_activation import LegacyPresent
from .review_execution_activation import (
    CompleteLegacyReviewSubmissions, CompleteLegacyRoundOutcomeStorage,
    LegacyReviewRunState, LegacyReviewSessionCandidate,
)
from .review_execution_counter_mapping import CompleteLegacyCounterMapping
from .review_execution_death_import import CompleteLegacyReviewDeathAttribution
from .workflow_identity import validate_workflow_integer, validate_workflow_text


class ReviewImportBlockerKind(str, Enum):
    MALFORMED_COMMAND = "malformed_command"
    UNASSIGNED_COUNTER = "unassigned_counter"
    UNATTRIBUTED_RUN = "unattributed_run"
    SESSION_PRODUCER = "session_producer"
    OPEN_ROUND = "open_round"
    PREDECESSOR_DEATH = "predecessor_death"
    NONZERO_DEBIT = "nonzero_debit"
    ORIGIN = "origin"


@dataclass(frozen=True)
class ReviewImportSource:
    kind: str
    identity: str
    digest: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.kind, "review import source kind", limit=64)
        validate_workflow_text(self.identity, "review import source identity", limit=4096)
        if not isinstance(self.digest, str) or len(self.digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.digest
        ):
            raise ValueError("review import source requires an exact identity and digest")


@dataclass(frozen=True)
class ReviewImportBlocker:
    kind: ReviewImportBlockerKind
    sources: tuple[ReviewImportSource, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.kind, ReviewImportBlockerKind)
                or not isinstance(self.sources, tuple) or not self.sources
                or any(not isinstance(item, ReviewImportSource) for item in self.sources)):
            raise ValueError("review import blocker requires a typed cause and source")


@dataclass(frozen=True)
class ReviewImportHistoryBlocked:
    pr: int
    blockers: tuple[ReviewImportBlocker, ...]

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "blocked review import PR", minimum=1)
        if (not isinstance(self.blockers, tuple) or not self.blockers
                or any(not isinstance(item, ReviewImportBlocker) for item in self.blockers)
                or len({item.kind for item in self.blockers}) != len(self.blockers)):
            raise ValueError("blocked review import requires distinct reasons")


@dataclass(frozen=True)
class ReviewImportNoHistoryObserved:
    """An empty source observation, not proof that execution may be activated."""

    pr: int
    source_digest: str

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "empty review import PR", minimum=1)
        if not isinstance(self.source_digest, str) or len(self.source_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_digest
        ):
            raise ValueError("empty review import requires the complete source digest")


ReviewImportHistory = ReviewImportHistoryBlocked | ReviewImportNoHistoryObserved


def classify_review_import_history(
    mapping: CompleteLegacyCounterMapping,
    deaths: CompleteLegacyReviewDeathAttribution,
    submissions: CompleteLegacyReviewSubmissions,
    outcomes: CompleteLegacyRoundOutcomeStorage,
) -> tuple[ReviewImportHistory, ...]:
    """Identify every open PR whose inherited execution needs further proof.

    All four inputs are complete observations, not trusted activation evidence.
    The adapter must revalidate their source sets under its held barriers.
    """
    if (not isinstance(mapping, CompleteLegacyCounterMapping)
            or not isinstance(deaths, CompleteLegacyReviewDeathAttribution)
            or not isinstance(submissions, CompleteLegacyReviewSubmissions)
            or not isinstance(outcomes, CompleteLegacyRoundOutcomeStorage)
            or deaths.mapping != mapping
            or len({mapping.links.repository, submissions.repository, outcomes.repository}) != 1):
        raise ValueError("review import history requires matching complete source sets")

    aggregate_digest = hashlib.sha256(json.dumps((
        mapping.source_digest, deaths.source_digest, submissions.source_digest,
        outcomes.source_digest,
    ), separators=(",", ":")).encode()).hexdigest()

    def ordered(values: tuple[ReviewImportSource, ...]) -> tuple[ReviewImportSource, ...]:
        return tuple(sorted(values, key=lambda item: (item.kind, item.identity, item.digest)))

    malformed_commands = ordered(tuple(
        ReviewImportSource("command", str(dict(item.command_fields)["command_id"]), item.source_digest)
        for item in submissions.submissions
        if not isinstance((target := dict(item.command_fields)["target"]), str)
        or re.fullmatch(r"pr:[1-9][0-9]{0,18}", target) is None
        or int(target[3:]) > 2**63 - 1
    ))
    unassigned_counters = ordered(tuple(
        ReviewImportSource("counter", f"key:{item.counter.source.key}",
                           item.counter.value.file_digest)
        for item in mapping.unassigned
    ))
    unattributed_runs = ordered(tuple(
        ReviewImportSource("run", item.run_id, item.source_digest)
        for item in deaths.runs.runs if item.state is not LegacyReviewRunState.ABANDONED
    ) + tuple(
        ReviewImportSource("run", item.source.run_id, item.source.source_digest)
        for item in deaths.unassigned_deaths
    ))

    def source(kind: str, identity: str, digest: str) -> ReviewImportSource:
        return ReviewImportSource(kind, identity, digest)

    results: list[ReviewImportHistory] = []
    for pr in mapping.links.listing.numbers:
        files = tuple(item for item in mapping.files.prs if item.source.pr == pr)
        debit = next(item for item in deaths.debits if item.pr == pr)
        commands = tuple(item for item in submissions.submissions
                         if dict(item.command_fields)["target"] == f"pr:{pr}")
        rounds = tuple(item for item in outcomes.rounds.rounds
                       if dict(item.fields)["target_number"] == pr)
        run_sources = tuple((*debit.current_owner_runs, *debit.predecessor_runs))

        file_refs = tuple(source("pr-file", f"{item.source.root}:pr:{pr}:{suffix}", value.digest)
                          for item in files
                          for suffix, value in (("session", item.source.session), ("round", item.source.round))
                          if isinstance(value, LegacyPresent))
        counter_refs = tuple(source("counter", f"key:{item.counter.source.key}",
                                    item.counter.value.file_digest) for item in debit.files)
        run_refs = tuple(source("run", item.source.run_id, item.source.source_digest)
                         for item in run_sources)
        command_refs = tuple(source("command", str(dict(item.command_fields)["command_id"]),
                                    item.source_digest) for item in commands)
        round_refs = tuple(source("round", str(dict(item.fields)["round_id"]), item.source_digest)
                           for item in rounds)
        history = ordered((*file_refs, *counter_refs, *run_refs, *command_refs, *round_refs))
        blockers: list[ReviewImportBlocker] = []

        if malformed_commands:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.MALFORMED_COMMAND,
                                                malformed_commands))
        if unassigned_counters:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.UNASSIGNED_COUNTER,
                                                unassigned_counters))
        if unattributed_runs:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.UNATTRIBUTED_RUN,
                                                unattributed_runs))

        sessions = tuple(source("session", f"{item.source.root}:pr:{pr}", item.session.file_digest)
                         for item in files if isinstance(item.session, LegacyReviewSessionCandidate))
        if sessions:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.SESSION_PRODUCER, ordered(sessions)))
        open_rounds = tuple(ref for item, ref in zip(rounds, round_refs, strict=True)
                            if dict(item.fields)["outcome_json"] is None)
        if open_rounds:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.OPEN_ROUND, ordered(open_rounds)))
        predecessors = tuple(source("run", item.source.run_id, item.source.source_digest)
                             for item in debit.predecessor_runs)
        if predecessors:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.PREDECESSOR_DEATH, ordered(predecessors)))
        nonzero = tuple(source("counter", f"key:{item.counter.source.key}", item.counter.value.file_digest)
                        for item in debit.files if item.counter.value.value > 0)
        nonzero += tuple(source("run", item.source.run_id, item.source.source_digest)
                         for item in debit.current_owner_runs)
        if nonzero:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.NONZERO_DEBIT, ordered(nonzero)))
        if history or malformed_commands or unassigned_counters or unattributed_runs:
            if history:
                blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.ORIGIN, history))
            results.append(ReviewImportHistoryBlocked(pr, tuple(blockers)))
        else:
            results.append(ReviewImportNoHistoryObserved(pr, aggregate_digest))
    return tuple(results)
