"""Configuration-bound composition for offline review execution import sources."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json

from ..adapters.review_execution_import_sources import (
    BlockedLegacyExecutionSqlImportSources, HeldLegacyExecutionCounterSources,
    ObservedLegacyExecutionSqlImportSources, RevalidatedLegacyExecutionImportSources,
    verify_legacy_execution_import_sources,
)
from ..adapters.review_execution_workflow_census import (
    BlockedRetainedReviewWorkflows, CompleteRetainedReviewWorkflows,
    read_complete_retained_review_workflows,
)
from ..adapters.resolve_activation import distinct_roots
from ..adapters.workflow_codec import encode_workflow
from ..domain.review_activation import MAX_IMPORTED_PULL_REQUESTS
from ..domain.review_execution_history_import import (
    ReviewImportHistory, ReviewImportHistoryBlocked, ReviewImportNoHistoryObserved,
    classify_review_import_history, ReviewImportScopeBlocked,
    ReviewImportSource, ReviewImportBlocker, ReviewImportBlockerKind,
)
from ..domain.review_execution_import_plan import (
    BlockedZeroHistoryReviewImport, EligibleZeroHistoryReviewImport,
    ReviewExecutionImportPlan, ZeroHistoryImportBlockerKind,
    screen_zero_history_review_import, review_import_candidate_digest,
)
from ..domain.review_workflow import RecordedPullRequest
from ..domain.resolve_activation import DeploymentInventory, LegacyPresent
from ..domain.workflow_identity import WorkflowWriter, canonical_workflow_repository
from .config import HarnessConfig, configured_prior_owner_ids, owner_identity_for_target


def verify_configured_legacy_execution_import_sources(
    cfg: HarnessConfig, db: sqlite3.Connection,
    counters: HeldLegacyExecutionCounterSources,
    observed: ObservedLegacyExecutionSqlImportSources,
    *, now: datetime | None = None, extra_roots: tuple[str, ...] = (),
) -> RevalidatedLegacyExecutionImportSources | BlockedLegacyExecutionSqlImportSources:
    """Bind SQL source revalidation to the same owner lineage daemon boot adopts.

    The result remains observational. The import writer must bind declared
    historical roots to its reviewed plan, hold the source context through
    commit, and separately prove inherited debit, reset and session authority.
    """
    if not isinstance(cfg, HarnessConfig):
        raise ValueError("execution import requires deployment configuration")
    if not isinstance(counters, HeldLegacyExecutionCounterSources):
        raise ValueError("execution import requires held typed file sources")
    cfg.validate()
    repository = canonical_workflow_repository(cfg.repo)
    if cfg.repo != repository or not os.path.isabs(cfg.target_dir):
        raise ValueError("execution import requires a canonical repository and absolute checkout")
    configured_roots = tuple((role, path) for role, path in (
        ("fix", cfg.state_dir), ("review", cfg.review_state_dir),
    ) if path)
    if not cfg.review_state_dir or any(not os.path.isabs(path) for _, path in configured_roots):
        raise ValueError("execution import requires absolute configured state directories")
    if (not isinstance(extra_roots, tuple)
            or any(not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path)
                   for path in extra_roots)):
        raise ValueError("execution import historical roots require existing absolute directories")
    configured_roots += tuple((f"extra:{os.path.realpath(path)}", path) for path in extra_roots)
    expected_roots = tuple((label, os.path.abspath(path), os.path.realpath(path))
                           for label, path in configured_roots)
    distinct_expected_roots = tuple((label, os.path.abspath(path), os.path.realpath(path))
                                    for label, path in distinct_roots(configured_roots))
    observed_roots = tuple((root.label, root.configured_path, root.resolved_path)
                           for root in counters.roots.roots)
    expected_other_role_dirs = tuple(os.path.abspath(path) for path in
                                     (cfg.state_dir,) if path)
    if (counters.review_state_dir != os.path.abspath(cfg.review_state_dir)
            or counters.other_role_state_dirs != expected_other_role_dirs
            or observed_roots not in (expected_roots, distinct_expected_roots)):
        return BlockedLegacyExecutionSqlImportSources("execution import roots differ from deployment configuration")
    writer = WorkflowWriter(
        repository, "review", owner_identity_for_target(repository, cfg.target_dir, "review"),
    )
    implicit_owner = f"{repository}:review"
    predecessors = tuple(dict.fromkeys(
        owner for owner in configured_prior_owner_ids(cfg, "review")
        if owner not in {writer.owner_id, implicit_owner}
    ))
    return verify_legacy_execution_import_sources(
        db, counters, observed, writer=writer, configured_prior_owners=predecessors,
        now=now,
    )


def _historical_pr_sources(counters: HeldLegacyExecutionCounterSources) -> tuple[tuple[int, ReviewImportSource], ...]:
    current_paths = {os.path.realpath(path) for path in (counters.review_state_dir, *counters.other_role_state_dirs)}
    historical_labels = {root.label for root in counters.roots.roots if root.resolved_path not in current_paths}
    # Archival files prove history, never the producer or credit of current state.
    return tuple((item.source.pr, ReviewImportSource('pr-file', f'{item.source.root}:pr:{item.source.pr}:{suffix}', value.digest))
                 for item in counters.roots.values.prs if item.source.root in historical_labels
                 for suffix, value in (('session', item.source.session), ('round', item.source.round))
                 if isinstance(value, LegacyPresent))


def classify_configured_legacy_review_import_history(
    cfg: HarnessConfig, db: sqlite3.Connection,
    counters: HeldLegacyExecutionCounterSources,
    observed: ObservedLegacyExecutionSqlImportSources,
    *, now: datetime | None = None, extra_roots: tuple[str, ...] = (),
) -> tuple[ReviewImportHistory, ...] | ReviewImportScopeBlocked | BlockedLegacyExecutionSqlImportSources:
    """Classify every open PR only after the configured source set is rechecked.

    The result is observational. Even an empty history requires independent
    first-observation, workflow, hold and origin evidence before activation.
    The caller must retain the held file scope and SQL transaction through use.
    """
    verified = verify_configured_legacy_execution_import_sources(
        cfg, db, counters, observed, now=now, extra_roots=extra_roots,
    )
    if isinstance(verified, BlockedLegacyExecutionSqlImportSources):
        return verified
    histories = classify_review_import_history(
        verified.counters.mapping, verified.deaths,
        verified.sql.commands, verified.sql.rounds,
        receipts=verified.sql.receipts, outcome_receipts=verified.sql.outcomes,
        generation_receipts=verified.sql.generation,
        retained_target_sources=_historical_pr_sources(verified.counters),
    )
    if isinstance(histories, ReviewImportScopeBlocked):
        return histories
    return tuple(
        replace(history, source_digest=verified.source_digest)
        if isinstance(history, ReviewImportNoHistoryObserved) else history
        for history in histories
    )


ReviewImportCandidate = (
    ReviewImportHistoryBlocked | EligibleZeroHistoryReviewImport
)


@dataclass(frozen=True)
class BlockedReviewImportObservationSet:
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason or len(self.reason) > 512:
            raise ValueError("blocked review import observation requires a bounded reason")


@dataclass(frozen=True)
class ObservedReviewExecutionImportCandidates:
    """A complete observational set; the activation writer must rederive it."""

    sources: RevalidatedLegacyExecutionImportSources
    retained: CompleteRetainedReviewWorkflows
    candidates: tuple[ReviewImportCandidate, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.sources, RevalidatedLegacyExecutionImportSources)
                or not isinstance(self.retained, CompleteRetainedReviewWorkflows)
                or not isinstance(self.candidates, tuple)
                or any(not isinstance(item, (
                    ReviewImportHistoryBlocked, EligibleZeroHistoryReviewImport,
                )) for item in self.candidates)
                or (numbers := tuple(item.pr for item in self.candidates)) != tuple(sorted(set(numbers)))
                or not set(self.retained.listing.numbers) <= set(numbers)
                or not {item.snapshot.revision.fence.key.target.number for item in self.retained.sources} <= set(numbers)
                or self.sources.sql.repository != self.retained.repository):
            raise ValueError("review import candidates require complete matching source sets")

    @property
    def source_digest(self) -> str:
        source = (
            self.sources.source_digest, self.retained.source_digest,
            tuple(review_import_candidate_digest(item) for item in self.candidates),
        )
        return hashlib.sha256(json.dumps(source, separators=(",", ":")).encode()).hexdigest()


def assess_configured_review_execution_import_candidates(
    cfg: HarnessConfig, db: sqlite3.Connection,
    counters: HeldLegacyExecutionCounterSources,
    observed: ObservedLegacyExecutionSqlImportSources,
    remote: tuple[RecordedPullRequest, ...],
    *, now: datetime | None = None, extra_roots: tuple[str, ...] = (),
) -> (ObservedReviewExecutionImportCandidates | BlockedLegacyExecutionSqlImportSources
      | BlockedRetainedReviewWorkflows | BlockedReviewImportObservationSet):
    """Join complete held sources, retained rows and current PR observations.

    Remote observations are caller supplied and remain untrusted. A production
    writer must reread them under the effect barriers before its SQL commit.
    """
    if not isinstance(remote, tuple) or any(not isinstance(item, RecordedPullRequest) for item in remote):
        raise ValueError("review import candidate assessment requires typed remote observations")
    verified = verify_configured_legacy_execution_import_sources(
        cfg, db, counters, observed, now=now, extra_roots=extra_roots,
    )
    if isinstance(verified, BlockedLegacyExecutionSqlImportSources):
        return verified
    retained = read_complete_retained_review_workflows(
        db, verified.sql.repository, verified.counters.mapping.links.listing,
    )
    if isinstance(retained, BlockedRetainedReviewWorkflows):
        return retained
    numbers = tuple(item.observation.key.target.number for item in remote)
    if len(numbers) != len(set(numbers)) or not set(numbers) <= set(retained.listing.numbers):
        return BlockedReviewImportObservationSet("remote review observations repeat or name an unlisted PR")
    by_pr = dict(zip(numbers, remote, strict=True))
    links = {item.pr: item for item in verified.counters.mapping.links.links}
    retained_by_pr = {item.snapshot.revision.fence.key.target.number: item
                      for item in retained.sources}
    histories = classify_review_import_history(
        verified.counters.mapping, verified.deaths, verified.sql.commands, verified.sql.rounds,
        receipts=verified.sql.receipts, outcome_receipts=verified.sql.outcomes,
        generation_receipts=verified.sql.generation,
        retained_target_sources=tuple((pr, ReviewImportSource("workflow", f"pr:{pr}", source.row_digest))
                                      for pr, source in retained_by_pr.items()
                                      if pr not in retained.listing.numbers) + _historical_pr_sources(verified.counters),
    )
    if isinstance(histories, ReviewImportScopeBlocked):
        return BlockedReviewImportObservationSet(
            "review import scope contains unassigned " + ", ".join(item.kind.value for item in histories.blockers)
        )
    audit_time = datetime.now(timezone.utc)
    if now is not None and now > audit_time:
        audit_time = now
    candidates: list[ReviewImportCandidate] = []
    for history in histories:
        if isinstance(history, ReviewImportHistoryBlocked):
            candidates.append(history)
            continue
        recorded = by_pr.get(history.pr)
        if recorded is None:
            candidates.append(ReviewImportHistoryBlocked(history.pr, (ReviewImportBlocker(
                ReviewImportBlockerKind.CURRENT_OBSERVATION, (ReviewImportSource(
                    ZeroHistoryImportBlockerKind.REMOTE_UNAVAILABLE.value, f"pr:{history.pr}",
                    retained_by_pr[history.pr].row_digest),),
            ),)))
            continue
        bound_history = replace(history, source_digest=verified.source_digest)
        candidate = screen_zero_history_review_import(
            bound_history, retained_by_pr[history.pr].snapshot, recorded, links[history.pr],
            cfg.review_runtime.bot, now=audit_time,
        )
        if isinstance(candidate, BlockedZeroHistoryReviewImport):
            # A fresh read time is not a change to the held source. Semantic
            # review timestamps and every other observed fact remain bound.
            source = replace(recorded, observation=replace(
                recorded.observation, observed_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
            ))
            digest = hashlib.sha256(encode_workflow(source).encode()).hexdigest()
            candidate = ReviewImportHistoryBlocked(history.pr, (ReviewImportBlocker(
                ReviewImportBlockerKind.CURRENT_OBSERVATION, (ReviewImportSource(
                    candidate.kind.value, f"pr:{history.pr}", digest),),
            ),))
        candidates.append(candidate)
    return ObservedReviewExecutionImportCandidates(verified, retained, tuple(candidates))


def plan_configured_review_execution_import(
    assessment: ObservedReviewExecutionImportCandidates,
    inventory: DeploymentInventory, execution_epoch: int,
    *, now: datetime | None = None,
) -> ReviewExecutionImportPlan | BlockedReviewImportObservationSet:
    """Produce fixed mixed decisions from a complete stopped source scope."""
    if not isinstance(assessment, ObservedReviewExecutionImportCandidates) or not isinstance(
        inventory, DeploymentInventory,
    ):
        raise ValueError("execution import planning requires complete typed observations")
    if now is not None and (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("execution import planning requires an aware time")
    planned_at = datetime.now(timezone.utc)
    if now is not None and now > planned_at:
        planned_at = now
    if inventory.repository != assessment.retained.repository:
        return BlockedReviewImportObservationSet("execution import inventory belongs to another repository")
    if inventory.activation is not None or not inventory.quiescent:
        return BlockedReviewImportObservationSet("execution import deployment is active or not quiescent")
    candidates = assessment.candidates
    if len(candidates) > MAX_IMPORTED_PULL_REQUESTS:
        return BlockedReviewImportObservationSet("execution import target scope exceeds the retained decision bound")
    plan = ReviewExecutionImportPlan(
        assessment.retained.repository, assessment.retained.listing, execution_epoch,
        assessment.sources.source_digest, assessment.retained.source_digest,
        inventory.digest, candidates, planned_at,
    )
    if plan.source_digest != assessment.source_digest:
        raise ValueError("execution import plan disagrees with its complete source digest")
    return plan
