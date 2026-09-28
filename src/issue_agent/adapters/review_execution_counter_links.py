"""Read-only, source-backed open-PR linkage for legacy review counter import."""

from __future__ import annotations

import sqlite3

from .control_store import SQLiteControlStore
from .review_activation import read_open_pull_requests
from .review_linkage import read_review_linked_issue
from ..domain.review_activation import OpenPullRequestsListed
from ..domain.review_execution_counter_mapping import (
    BlockedLegacyCounterLink, BlockedLegacyOpenCounterLinks, CompleteLegacyOpenCounterLinks,
    classify_legacy_counter_link,
)
from ..domain.workflow_identity import PullRequestRef, WorkflowKey, canonical_workflow_repository


def read_complete_legacy_open_counter_links(
    control: SQLiteControlStore, gateway: object, repository: str,
) -> CompleteLegacyOpenCounterLinks | BlockedLegacyOpenCounterLinks:
    """Read every currently open PR and its issue with complete existing adapters."""
    try:
        repo = canonical_workflow_repository(repository)
    except ValueError:
        return BlockedLegacyOpenCounterLinks("counter linkage repository is invalid")
    try:
        gateway_repo = canonical_workflow_repository(getattr(gateway, "repo", None))
    except ValueError:
        return BlockedLegacyOpenCounterLinks("counter linkage gateway repository is unavailable")
    if gateway_repo != repo:
        return BlockedLegacyOpenCounterLinks("counter linkage gateway belongs to another repository")
    listing = read_open_pull_requests(gateway, repo)
    if not isinstance(listing, OpenPullRequestsListed):
        return BlockedLegacyOpenCounterLinks("complete open PR listing is unavailable")
    reader = getattr(gateway, "get_review_pull_request", None)
    if not callable(reader):
        return BlockedLegacyOpenCounterLinks("complete review pull request reader is unavailable")
    links = []
    for number in listing.numbers:
        try:
            view = reader(number)
            linked = read_review_linked_issue(control, gateway, WorkflowKey(repo, PullRequestRef(number)), view)
            classified = classify_legacy_counter_link(view, linked)
        except sqlite3.Error:
            raise
        except Exception as exc:  # noqa: BLE001 - remote failures cannot prove counter attribution
            return BlockedLegacyOpenCounterLinks(
                f"PR #{number} linkage read failed: {type(exc).__name__}"[:512]
            )
        if isinstance(classified, BlockedLegacyCounterLink):
            return BlockedLegacyOpenCounterLinks(f"PR #{number}: {classified.reason}")
        if classified.pr != number:
            return BlockedLegacyOpenCounterLinks(f"PR #{number} linkage names another pull request")
        links.append(classified)
    if read_open_pull_requests(gateway, repo) != listing:
        return BlockedLegacyOpenCounterLinks("open PR listing changed during counter linkage read")
    return CompleteLegacyOpenCounterLinks(repo, listing, tuple(links))


def revalidate_complete_legacy_open_counter_links(
    control: SQLiteControlStore, gateway: object, repository: str,
    observed: CompleteLegacyOpenCounterLinks,
) -> CompleteLegacyOpenCounterLinks | BlockedLegacyOpenCounterLinks:
    """Require a fresh full read before the caller uses an observed mapping."""
    if not isinstance(observed, CompleteLegacyOpenCounterLinks):
        return BlockedLegacyOpenCounterLinks("counter linkage revalidation requires a complete observation")
    current = read_complete_legacy_open_counter_links(control, gateway, repository)
    if isinstance(current, BlockedLegacyOpenCounterLinks):
        return current
    if current.source_digest != observed.source_digest or current != observed:
        return BlockedLegacyOpenCounterLinks("open PR counter linkage changed or was incomplete")
    return current
