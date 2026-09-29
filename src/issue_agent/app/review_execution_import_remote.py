"""Read complete current review facts for an offline execution import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os

from ..adapters.control_store import SQLiteControlStore
from ..adapters.review_activation import read_open_pull_requests
from ..adapters.review_effects import read_pull_request
from ..adapters.review_linkage import review_linked_issue_holds
from ..adapters.store import Store
from ..domain.review_activation import OpenPullRequestsListed
from ..domain.review_workflow import CompleteReviews, ObservedPullRequest, RecordedPullRequest
from ..domain.workflow_identity import PullRequestRef, WorkflowKey, canonical_workflow_repository
from .config import HarnessConfig, owner_identity_for_target
from .review_runtime import review_external_holds, unproductive_rounds_reader


@dataclass(frozen=True)
class BlockedReviewImportRemote:
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason or len(self.reason) > 512:
            raise ValueError("blocked import remote read requires a bounded reason")


def read_configured_review_import_remote(
    cfg: HarnessConfig, control: SQLiteControlStore, gateway: object,
    store: Store, listing: OpenPullRequestsListed,
) -> tuple[RecordedPullRequest, ...] | BlockedReviewImportRemote:
    """Reread every listed PR, complete reviews and trusted holds from each view.

    This is observational. The caller holds all three deployment effect
    barriers and later rechecks the SQL/file sources in its commit transaction.
    """
    if (not isinstance(cfg, HarnessConfig) or not isinstance(control, SQLiteControlStore)
            or not isinstance(store, Store) or not isinstance(listing, OpenPullRequestsListed)):
        raise ValueError("review import remote read requires configured typed sources")
    cfg.validate()
    repository = canonical_workflow_repository(cfg.repo)
    try:
        gateway_repository = canonical_workflow_repository(getattr(gateway, "repo", None))
    except ValueError:
        return BlockedReviewImportRemote("review import gateway repository is unavailable")
    if (cfg.repo != repository
            or not cfg.review_state_dir
            or os.path.realpath(store.state_dir) != os.path.realpath(cfg.review_state_dir)
            or gateway_repository != repository):
        return BlockedReviewImportRemote("review import remote reader differs from deployment configuration")
    if read_open_pull_requests(gateway, repository) != listing:
        return BlockedReviewImportRemote("open pull request listing changed before remote review reads")
    owner_id = owner_identity_for_target(repository, cfg.target_dir, "review")
    external_holds = review_external_holds(
        unproductive_rounds_reader(store, lambda: control.abandoned_dispatch_counts(owner_id)),
        cfg.review_runtime.max_unproductive_rounds,
        linked_holds=lambda observed, view: review_linked_issue_holds(control, gateway, observed.key, view),
    )
    facts: list[RecordedPullRequest] = []
    for number in listing.numbers:
        key = WorkflowKey(repository, PullRequestRef(number))
        observed, view = read_pull_request(gateway, key, datetime.now(timezone.utc), reviews=True)
        if not isinstance(observed, ObservedPullRequest) or not isinstance(observed.reviews, CompleteReviews):
            return BlockedReviewImportRemote(f"PR #{number} lacks a complete current review read")
        try:
            facts.append(RecordedPullRequest(observed, tuple(external_holds(observed, view))))
        except Exception as exc:  # noqa: BLE001 - missing hold evidence must block the whole import
            return BlockedReviewImportRemote(
                f"PR #{number} external hold read failed: {type(exc).__name__}"[:512]
            )
    if read_open_pull_requests(gateway, repository) != listing:
        return BlockedReviewImportRemote("open pull request listing changed during remote review reads")
    return tuple(facts)
