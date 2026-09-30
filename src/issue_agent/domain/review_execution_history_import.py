"""Conservative, pure classification of preactivation review execution evidence.

The result describes what historical proof is missing. It never grants an
execution origin: even an empty observation needs a fresh, source-backed
activation decision at the infrastructure boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
import re
from typing import TypeVar

from .resolve_activation import LegacyPresent
from .review_execution_activation import (
    CompleteLegacyReviewSubmissions, CompleteLegacyReviewRounds,
    LegacyReviewSessionCandidate,
    legacy_review_dispatch_identity, CompleteLegacyReviewSubmissionReceipts,
    CompleteLegacyReviewOutcomeReceipts, CompleteLegacyReviewGenerationReceipts,
)
from .review_execution_counter_mapping import CompleteLegacyCounterMapping
from .review_execution_death_import import CompleteLegacyReviewDeathAttribution
from .workflow_identity import validate_workflow_integer
from .review_execution_migration import (
    ReviewImportBlockerKind, ReviewImportSource, ReviewImportBlocker, ReviewImportHistoryBlocked,
)


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


@dataclass(frozen=True)
class ReviewImportScopeBlocked:
    blockers: tuple[ReviewImportBlocker, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.blockers, tuple) or not self.blockers
                or any(not isinstance(item, ReviewImportBlocker) for item in self.blockers)
                or len({item.kind for item in self.blockers}) != len(self.blockers)):
            raise ValueError("blocked review import scope requires distinct sourced reasons")


ReviewImportHistory = ReviewImportHistoryBlocked | ReviewImportNoHistoryObserved


def classify_review_import_history(
    mapping: CompleteLegacyCounterMapping,
    deaths: CompleteLegacyReviewDeathAttribution,
    submissions: CompleteLegacyReviewSubmissions,
    outcomes: CompleteLegacyReviewRounds,
    *, receipts: CompleteLegacyReviewSubmissionReceipts | None = None,
    outcome_receipts: CompleteLegacyReviewOutcomeReceipts | None = None,
    generation_receipts: CompleteLegacyReviewGenerationReceipts | None = None,
    retained_target_sources: tuple[tuple[int, ReviewImportSource], ...] = (),
) -> tuple[ReviewImportHistory, ...] | ReviewImportScopeBlocked:
    """Classify every open or retained target without reconstructing chronology.

    All four inputs are complete observations, not trusted activation evidence.
    The adapter must revalidate their source sets under its held barriers.
    """
    if (not isinstance(mapping, CompleteLegacyCounterMapping)
            or not isinstance(deaths, CompleteLegacyReviewDeathAttribution)
            or not isinstance(submissions, CompleteLegacyReviewSubmissions)
            or not isinstance(outcomes, CompleteLegacyReviewRounds)
            or deaths.mapping != mapping
            or len({mapping.links.repository, submissions.repository, outcomes.repository}) != 1):
        raise ValueError("review import history requires matching complete source sets")
    if (not isinstance(retained_target_sources, tuple)
            or any(not isinstance(item, tuple) or len(item) != 2 or type(item[0]) is not int
                   or not 1 <= item[0] <= 2**63 - 1 or not isinstance(item[1], ReviewImportSource)
                   for item in retained_target_sources)
            or len({item[0] for item in retained_target_sources}) != len(retained_target_sources)):
        raise ValueError("review import requires unique retained target sources")

    for observed, expected_type in ((receipts, CompleteLegacyReviewSubmissionReceipts),
                                    (outcome_receipts, CompleteLegacyReviewOutcomeReceipts),
                                    (generation_receipts, CompleteLegacyReviewGenerationReceipts)):
        if observed is not None and (not isinstance(observed, expected_type)
                                    or observed.repository != mapping.links.repository):
            raise ValueError("review import receipt sources must match the repository")

    aggregate_digest = hashlib.sha256(json.dumps((
        mapping.source_digest, deaths.source_digest, submissions.source_digest,
        outcomes.source_digest,
        tuple(item.source_digest for item in (receipts, outcome_receipts, generation_receipts)
              if item is not None),
        tuple(sorted((pr, source.kind, source.identity, source.digest)
                     for pr, source in retained_target_sources)),
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
        if item.counter.source.key > 0
    ))
    run_targets = tuple((run, legacy_review_dispatch_identity(run)) for run in deaths.runs.runs)
    unattributed_runs = ordered(tuple(
        ReviewImportSource("run", run.run_id, run.source_digest)
        for run, identity in run_targets
        if isinstance(identity, str) or identity[0] != mapping.links.repository
    ))

    command_targets = {
        dict(item.command_fields)["command_id"]: int(target[3:])
        for item in submissions.submissions
        if isinstance(target := dict(item.command_fields)["target"], str)
        and re.fullmatch(r"pr:[1-9][0-9]{0,18}", target) and int(target[3:]) <= 2**63 - 1
    }
    round_targets = {dict(item.fields)["round_id"]: dict(item.fields)["target_number"]
                     for item in outcomes.rounds}
    receipt_targets: list[tuple[int | None, ReviewImportSource]] = []
    registrations: dict[str, set[int]] = {}
    for item in (() if receipts is None else receipts.receipts):
        fields = dict(item.fields)
        if (fields["namespace"] == "review-round-submission"
                and fields["key"] in command_targets and fields["payload_json"] is not None):
            registrations.setdefault(fields["payload_json"], set()).add(command_targets[fields["key"]])
    for item in (() if receipts is None else receipts.receipts):
        fields = dict(item.fields)
        candidates = registrations.get(fields["payload_json"], frozenset())
        pr = (next(iter(candidates)) if len(candidates) == 1 else None) if fields["namespace"] == "review-round-submission-index" else command_targets.get(fields["key"])
        receipt_targets.append((pr, ReviewImportSource(
            "receipt", f"{fields['namespace']}:{fields['key']}", item.source_digest)))
    for item in (() if outcome_receipts is None else outcome_receipts.receipts):
        fields = dict(item.fields)
        receipt_targets.append((round_targets.get(fields["key"]), ReviewImportSource(
            "receipt", f"{fields['namespace']}:{fields['key']}", item.source_digest)))
    for item in (() if generation_receipts is None else generation_receipts.receipts):
        fields = dict(item.fields)
        receipt_targets.append((_generation_target(fields["payload_json"], mapping.links.repository),
                               ReviewImportSource("receipt", str(fields["key"]), item.source_digest)))
    unassigned_receipts = ordered(tuple(ref for pr, ref in receipt_targets if pr is None))

    unauthorized_commands = ordered(tuple(ReviewImportSource(
        "command", str(dict(item.command_fields)["command_id"]), item.source_digest)
        for item in submissions.submissions
        if dict(item.command_fields)["executor_identity"] not in deaths.runs.lineage.owners
        or item.receipt_fields is not None and dict(item.receipt_fields)["executor_identity"] not in deaths.runs.lineage.owners))
    conflicting = []
    for item in submissions.submissions:
        fields = dict(item.command_fields)
        pr = command_targets.get(fields["command_id"])
        if pr is not None and not _target_identity_agrees(
                mapping.links.repository, pr, fields["payload_json"], fields["workflow_binding_json"]):
            conflicting.append(ReviewImportSource("command", str(fields["command_id"]), item.source_digest))
    for item in outcomes.rounds:
        fields = dict(item.fields)
        if not _target_identity_agrees(mapping.links.repository, fields["target_number"],
                                       fields["plan_json"], fields["planning_json"],
                                       fields["closure_json"], fields["outcome_json"]):
            conflicting.append(ReviewImportSource("round", str(fields["round_id"]), item.source_digest))
    for run, identity in run_targets:
        if (not isinstance(identity, str) and identity[0] == mapping.links.repository
                and not _target_identity_agrees(mapping.links.repository, identity[1], run.workflow_binding_json)):
            conflicting.append(ReviewImportSource("run", run.run_id, run.source_digest))
    receipt_payloads = tuple(item for observed in (receipts, outcome_receipts, generation_receipts)
                             if observed is not None for item in observed.receipts)
    for (pr, ref), item in zip(receipt_targets, receipt_payloads, strict=True):
        if pr is not None and not _target_identity_agrees(mapping.links.repository, pr, dict(item.fields)["payload_json"]):
            conflicting.append(ref)

    scope_blockers = tuple(ReviewImportBlocker(kind, sources) for kind, sources in (
        (ReviewImportBlockerKind.CROSS_TARGET_SOURCE, ordered(tuple(conflicting))),
        (ReviewImportBlockerKind.MALFORMED_COMMAND, malformed_commands),
        (ReviewImportBlockerKind.UNASSIGNED_COUNTER, unassigned_counters),
        (ReviewImportBlockerKind.UNATTRIBUTED_RUN, unattributed_runs),
        (ReviewImportBlockerKind.UNATTRIBUTED_COMMAND, unauthorized_commands),
        (ReviewImportBlockerKind.UNATTRIBUTED_RECEIPT, unassigned_receipts),
    ) if sources)
    if scope_blockers:
        return ReviewImportScopeBlocked(scope_blockers)

    def source(kind: str, identity: str, digest: str) -> ReviewImportSource:
        return ReviewImportSource(kind, identity, digest)

    results: list[ReviewImportHistory] = []
    numbers = set(mapping.links.listing.numbers)
    numbers.update(item.source.pr for item in mapping.files.prs)
    numbers.update(-item.source.key for item in mapping.files.counters if item.source.key < 0)
    numbers.update(identity[1] for _, identity in run_targets
                   if not isinstance(identity, str) and identity[0] == mapping.links.repository)
    numbers.update(dict(item.fields)['target_number'] for item in outcomes.rounds)
    numbers.update(int(target[3:]) for item in submissions.submissions
                   if isinstance(target := dict(item.command_fields)['target'], str)
                   and re.fullmatch(r'pr:[1-9][0-9]{0,18}', target) and int(target[3:]) <= 2**63 - 1)
    numbers.update(pr for pr, _ in retained_target_sources)
    numbers.update(pr for pr, _ in receipt_targets if pr is not None)
    counter_targets = {item.counter: item.pr for item in mapping.assignments}
    files_by_pr = _group_by_pr((item.source.pr, item) for item in mapping.files.prs)
    counters_by_pr = _group_by_pr((pr, item) for item in mapping.files.counters
                                if (pr := -item.source.key if item.source.key < 0
                                    else counter_targets.get(item)) is not None)
    commands_by_pr = _group_by_pr((command_targets[dict(item.command_fields)["command_id"]], item)
                                for item in submissions.submissions
                                if dict(item.command_fields)["command_id"] in command_targets)
    rounds_by_pr = _group_by_pr((dict(item.fields)["target_number"], item) for item in outcomes.rounds)
    runs_by_pr = _group_by_pr((identity[1], run) for run, identity in run_targets
                            if not isinstance(identity, str) and identity[0] == mapping.links.repository)
    retained_by_pr = _group_by_pr(retained_target_sources)
    receipts_by_pr = _group_by_pr((pr, ref) for pr, ref in receipt_targets if pr is not None)
    debits_by_pr = {item.pr: item for item in deaths.debits}
    for pr in sorted(numbers):
        files = files_by_pr.get(pr, ())
        debit = debits_by_pr.get(pr)
        counters = counters_by_pr.get(pr, ())
        commands = commands_by_pr.get(pr, ())
        rounds = rounds_by_pr.get(pr, ())
        run_sources = runs_by_pr.get(pr, ())

        file_refs = tuple(source("pr-file", f"{item.source.root}:pr:{pr}:{suffix}", value.digest)
                          for item in files
                          for suffix, value in (("session", item.source.session), ("round", item.source.round))
                          if isinstance(value, LegacyPresent))
        counter_refs = tuple(source("counter", f"key:{item.source.key}",
                                    item.value.file_digest) for item in counters)
        run_refs = tuple(source("run", item.run_id, item.source_digest)
                         for item in run_sources)
        command_refs = tuple(source("command", str(dict(item.command_fields)["command_id"]),
                                    item.source_digest) for item in commands)
        round_refs = tuple(source("round", str(dict(item.fields)["round_id"]), item.source_digest)
                           for item in rounds)
        retained_refs = retained_by_pr.get(pr, ())
        receipt_refs = receipts_by_pr.get(pr, ())
        history = ordered((*file_refs, *counter_refs, *run_refs, *command_refs, *round_refs,
                           *retained_refs, *receipt_refs))
        blockers: list[ReviewImportBlocker] = []

        sessions = tuple(source("session", f"{item.source.root}:pr:{pr}", item.session.file_digest)
                         for item in files if isinstance(item.session, LegacyReviewSessionCandidate))
        if sessions:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.SESSION_PRODUCER, ordered(sessions)))
        open_rounds = tuple(ref for item, ref in zip(rounds, round_refs, strict=True)
                            if dict(item.fields)["outcome_json"] is None)
        if open_rounds:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.OPEN_ROUND, ordered(open_rounds)))
        predecessors = tuple(source("run", item.source.run_id, item.source.source_digest)
                             for item in (() if debit is None else debit.predecessor_runs))
        if predecessors:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.PREDECESSOR_DEATH, ordered(predecessors)))
        nonzero = tuple(source("counter", f"key:{item.source.key}", item.value.file_digest)
                        for item in counters if item.value.value > 0)
        nonzero += tuple(source("run", item.source.run_id, item.source.source_digest)
                         for item in (() if debit is None else debit.current_owner_runs))
        if nonzero:
            blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.NONZERO_DEBIT, ordered(nonzero)))
        if history:
            if history:
                blockers.append(ReviewImportBlocker(ReviewImportBlockerKind.ORIGIN, history))
            results.append(ReviewImportHistoryBlocked(pr, tuple(blockers)))
        else:
            results.append(ReviewImportNoHistoryObserved(pr, aggregate_digest))
    return tuple(results)


def _generation_target(payload: str, repository: str) -> int | None:
    """Read only the two recorded target identities; never reconstruct a boundary."""
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate generation source field")
            result[name] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique)
        if value["type"] == "ReviewExecutionTransitionProofSource":
            value = value["fields"]["boundary"]
        if value["type"] != "ReviewExecutionGenerationBoundary":
            return None
        keys = tuple(value["fields"][name]["fields"]["fence"]["fields"]["key"]
                     for name in ("from_revision", "to_revision"))
        if keys[0] != keys[1]:
            return None
        key = keys[0]
        target = key["fields"]["target"]
        number = target["fields"]["number"]
        if (key["type"] == "WorkflowKey" and key["fields"]["repository"] == repository
                and target["type"] == "PullRequestRef" and type(number) is int
                and 1 <= number <= 2**63 - 1):
            return number
    except (ValueError, TypeError, KeyError, RecursionError):
        pass
    return None


def _target_identity_agrees(repository: str, pr: int, *payloads: str | None) -> bool:
    """Reject contradictory recorded targets without interpreting lifecycle history."""
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate source identity field")
            result[name] = value
        return result

    def reject_nonfinite(value):
        raise ValueError("nonfinite source identity value")

    try:
        for payload in payloads:
            if payload is None:
                continue
            root = json.loads(payload, object_pairs_hook=unique,
                              parse_constant=reject_nonfinite)
            if isinstance(root, dict) and "pr" in root and (type(root["pr"]) is not int or root["pr"] != pr):
                return False
            pending = [(root, 0)]
            while pending:
                value, depth = pending.pop()
                if depth > 24:
                    return False
                if isinstance(value, dict):
                    if value.get("type") == "WorkflowKey":
                        fields = value["fields"]
                        target = fields["target"]
                        if (fields["repository"] != repository or target["type"] != "PullRequestRef"
                                or type(target["fields"]["number"]) is not int or target["fields"]["number"] != pr):
                            return False
                    pending.extend((child, depth + 1) for child in value.values()
                                   if isinstance(child, (dict, list)))
                elif isinstance(value, list):
                    pending.extend((child, depth + 1) for child in value if isinstance(child, (dict, list)))
    except (ValueError, TypeError, KeyError, RecursionError):
        return False
    return True


_T = TypeVar("_T")


def _group_by_pr(sources: Iterable[tuple[int, _T]]) -> dict[int, tuple[_T, ...]]:
    grouped: dict[int, list[_T]] = {}
    for pr, source in sources:
        grouped.setdefault(pr, []).append(source)
    return {pr: tuple(values) for pr, values in grouped.items()}
