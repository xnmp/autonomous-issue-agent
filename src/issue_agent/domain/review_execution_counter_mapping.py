"""Pure, observational attribution of legacy review execution file counters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json

from .review_activation import OpenPullRequestsListed
from .review_execution_activation import (
    CompleteLegacyExecutionFileValues, MAX_SQL_ROWID, ParsedLegacyExecutionCounter,
)
from .review_linkage import LinkedIssueRead, LinkedIssueUnavailable, NoLinkedIssue, ObservedLinkedIssue
from .review_rejection import is_commit_sha
from .workflow_identity import canonical_workflow_repository, validate_workflow_integer, validate_workflow_text
from .world import PRView, issue_for_pr


@dataclass(frozen=True)
class LegacyOpenPRCounterLink:
    """One observed open PR's exact runtime issue-key selection."""

    pr: int
    head_sha: str
    branch: str
    closing_issue: int | None
    linked_issue: int | None

    def __post_init__(self) -> None:
        validate_workflow_integer(self.pr, "counter-link PR", minimum=1)
        if not is_commit_sha(self.head_sha):
            raise ValueError("counter-link PR requires a valid head SHA")
        validate_workflow_text(self.branch, "counter-link branch", limit=4096)
        if self.closing_issue is not None:
            validate_workflow_integer(self.closing_issue, "counter-link closing issue", minimum=1)
        if self.linked_issue is not None:
            validate_workflow_integer(self.linked_issue, "counter-link issue", minimum=1)
        expected = issue_for_pr(PRView(self.pr, head_ref_name=self.branch, closing_issue=self.closing_issue))
        if expected != self.linked_issue:
            raise ValueError("counter-link issue disagrees with runtime linkage policy")


@dataclass(frozen=True)
class BlockedLegacyCounterLink:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "blocked counter link")


def classify_legacy_counter_link(
    view: PRView, linked: LinkedIssueRead,
) -> LegacyOpenPRCounterLink | BlockedLegacyCounterLink:
    """Project one complete PR and issue read into the runtime's counter key."""
    if (not isinstance(view, PRView) or type(view.number) is not int or not 0 < view.number <= MAX_SQL_ROWID
            or view.state != "OPEN" or not is_commit_sha(view.head_ref_oid)
            or not isinstance(view.head_ref_name, str) or not view.head_ref_name
            or not isinstance(view.closing_issue, (int, type(None))) or type(view.closing_issue) is bool):
        return BlockedLegacyCounterLink("open pull request linkage is malformed or incomplete")
    try:
        number = issue_for_pr(view)
        if number is not None:
            validate_workflow_integer(number, "linked issue number", minimum=1)
        if isinstance(linked, NoLinkedIssue):
            if number is not None:
                return BlockedLegacyCounterLink("linked issue observation disagrees with the pull request")
        elif isinstance(linked, ObservedLinkedIssue):
            if linked.issue.number != number:
                return BlockedLegacyCounterLink("linked issue observation names another issue")
        elif isinstance(linked, LinkedIssueUnavailable):
            return BlockedLegacyCounterLink("linked issue observation is unavailable")
        else:
            return BlockedLegacyCounterLink("linked issue observation is untyped")
        return LegacyOpenPRCounterLink(
            view.number, view.head_ref_oid, view.head_ref_name, view.closing_issue, number,
        )
    except (ValueError, TypeError, OverflowError):
        return BlockedLegacyCounterLink("open pull request linkage is malformed or out of range")


@dataclass(frozen=True)
class CompleteLegacyOpenCounterLinks:
    """Every listed open PR has one observed, internally consistent issue link."""

    repository: str
    listing: OpenPullRequestsListed
    links: tuple[LegacyOpenPRCounterLink, ...]

    def __post_init__(self) -> None:
        if (canonical_workflow_repository(self.repository) != self.repository
                or not isinstance(self.listing, OpenPullRequestsListed) or not isinstance(self.links, tuple)
                or any(not isinstance(link, LegacyOpenPRCounterLink) for link in self.links)
                or tuple(link.pr for link in self.links) != self.listing.numbers):
            raise ValueError("complete counter links require one ordered link for every open PR")

    @property
    def source_digest(self) -> str:
        source = tuple((link.pr, link.head_sha, link.branch, link.closing_issue, link.linked_issue)
                       for link in self.links)
        return hashlib.sha256(json.dumps((self.repository, self.listing.numbers, source), separators=(",", ":"),
                                         ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class BlockedLegacyOpenCounterLinks:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "blocked open counter links")


@dataclass(frozen=True)
class LegacyCounterAssignment:
    counter: ParsedLegacyExecutionCounter
    pr: int

    def __post_init__(self) -> None:
        if not isinstance(self.counter, ParsedLegacyExecutionCounter):
            raise ValueError("counter assignment requires an exact parsed file source")
        validate_workflow_integer(self.pr, "counter target PR", minimum=1)


class LegacyUnassignedCounterReason(str, Enum):
    NO_OPEN_PR = "no_open_pr"
    NO_LINKED_OPEN_PR = "no_linked_open_pr"


@dataclass(frozen=True)
class LegacyUnassignedCounter:
    counter: ParsedLegacyExecutionCounter
    reason: LegacyUnassignedCounterReason

    def __post_init__(self) -> None:
        if (not isinstance(self.counter, ParsedLegacyExecutionCounter)
                or not isinstance(self.reason, LegacyUnassignedCounterReason)):
            raise ValueError("unassigned counter requires its source and typed reason")


@dataclass(frozen=True)
class BlockedLegacyCounterMapping:
    reason: str

    def __post_init__(self) -> None:
        validate_workflow_text(self.reason, "blocked counter mapping")


def _expected_mapping(
    files: CompleteLegacyExecutionFileValues, links: CompleteLegacyOpenCounterLinks,
) -> tuple[tuple[LegacyCounterAssignment, ...], tuple[LegacyUnassignedCounter, ...]] | BlockedLegacyCounterMapping:
    open_prs = set(links.listing.numbers)
    by_issue: dict[int, list[int]] = {}
    for link in links.links:
        if link.linked_issue is not None:
            by_issue.setdefault(link.linked_issue, []).append(link.pr)
    assigned: list[LegacyCounterAssignment] = []
    unassigned: list[LegacyUnassignedCounter] = []
    total_by_pr: dict[int, int] = {}
    seen_keys: set[int] = set()
    for counter in files.counters:
        key = counter.source.key
        if key in seen_keys:
            return BlockedLegacyCounterMapping(f"counter key {key} occurs in multiple source roots")
        seen_keys.add(key)
        if key < 0:
            target = -key if -key in open_prs else None
            reason = LegacyUnassignedCounterReason.NO_OPEN_PR
        else:
            targets = by_issue.get(key, ())
            if len(targets) > 1:
                return BlockedLegacyCounterMapping(f"issue counter #{key} belongs to multiple open PRs")
            target = targets[0] if targets else None
            reason = LegacyUnassignedCounterReason.NO_LINKED_OPEN_PR
        if target is None:
            unassigned.append(LegacyUnassignedCounter(counter, reason))
            continue
        total_by_pr[target] = total_by_pr.get(target, 0) + counter.value.value
        if total_by_pr[target] > MAX_SQL_ROWID:
            return BlockedLegacyCounterMapping(f"review counter total for PR #{target} exceeds the supported range")
        assigned.append(LegacyCounterAssignment(counter, target))
    return tuple(assigned), tuple(unassigned)


@dataclass(frozen=True)
class CompleteLegacyCounterMapping:
    """Every parsed counter is assigned once or retained as historical evidence."""

    files: CompleteLegacyExecutionFileValues
    links: CompleteLegacyOpenCounterLinks
    assignments: tuple[LegacyCounterAssignment, ...]
    unassigned: tuple[LegacyUnassignedCounter, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.files, CompleteLegacyExecutionFileValues)
                or not isinstance(self.links, CompleteLegacyOpenCounterLinks)
                or not isinstance(self.assignments, tuple) or not isinstance(self.unassigned, tuple)
                or (expected := _expected_mapping(self.files, self.links))
                != (self.assignments, self.unassigned)):
            raise ValueError("complete counter mapping disagrees with its file or link sources")

    @property
    def source_digest(self) -> str:
        return hashlib.sha256(json.dumps((self.files.source_digest, self.links.source_digest),
                                         separators=(",", ":")).encode()).hexdigest()


def map_legacy_execution_counters(
    files: CompleteLegacyExecutionFileValues, links: CompleteLegacyOpenCounterLinks,
) -> CompleteLegacyCounterMapping | BlockedLegacyCounterMapping:
    """Attribute positive issue and negative PR keys without copying shared values."""
    if not isinstance(files, CompleteLegacyExecutionFileValues) or not isinstance(links, CompleteLegacyOpenCounterLinks):
        raise ValueError("counter mapping requires complete typed file and open-PR observations")
    expected = _expected_mapping(files, links)
    if isinstance(expected, BlockedLegacyCounterMapping):
        return expected
    return CompleteLegacyCounterMapping(files, links, *expected)
