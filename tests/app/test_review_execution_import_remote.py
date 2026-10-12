"""Configured remote import reads preserve complete reviews and trusted holds."""

from tests.domain.builders import observed_pr

from dataclasses import replace

from issue_agent.adapters.control_store import SQLiteControlStore
from issue_agent.adapters.store import Store
from issue_agent.app.config import HarnessConfig
from issue_agent.app.review_execution_import_remote import (
    BlockedReviewImportRemote, read_configured_review_import_remote,
)
from issue_agent.domain.labels import Label
from issue_agent.domain.review_activation import OpenPullRequestsListed
from issue_agent.domain.review_workflow import CompleteReviews, PartialReviews, RecordedPullRequest
from issue_agent.domain.world import PRView


class ReviewGateway:
    repo = "o/r"

    def __init__(self) -> None:
        self.view = observed_pr(9, head_ref_name="work/topic", head_ref_oid="a" * 40)
        self.reviews = CompleteReviews(())
        self.listed = (9,)

    def _gh_json(self, _args):
        return [[{"number": number, "state": "open"} for number in self.listed]]

    def get_pr(self, number, *, mergeability=False, checks=False):
        assert number == 9
        return self.view

    def get_review_pull_request(self, number):
        return self.get_pr(number)

    def pull_request_reviews(self, number):
        assert number == 9
        return self.reviews


def _sources(tmp_path):
    checkout = tmp_path / "checkout"
    review = tmp_path / "review"
    checkout.mkdir()
    review.mkdir()
    cfg = HarnessConfig(
        repo="o/r", target_dir=str(checkout), state_dir=str(tmp_path / "fix"),
        review_state_dir=str(review),
        fix_bot="fixer",
    )
    control = SQLiteControlStore(str(tmp_path / "control.db"))
    control.initialize()
    return cfg, control, ReviewGateway(), Store(str(review))


def test_configured_remote_read_uses_complete_reviews_and_current_hold_labels(tmp_path):
    cfg, control, gateway, store = _sources(tmp_path)
    gateway.view = replace(gateway.view, labels=frozenset({Label.AI_PR_OPEN, Label.NEEDS_BUDGET_REVIEW}))

    result = read_configured_review_import_remote(
        cfg, control, gateway, store, OpenPullRequestsListed((9,)),
    )

    assert isinstance(result, tuple)
    assert len(result) == 1
    assert isinstance(result[0], RecordedPullRequest)
    assert result[0].observation.reviews == CompleteReviews(())
    assert result[0].external_holds == (Label.NEEDS_BUDGET_REVIEW.value,)


def test_configured_remote_read_refuses_partial_reviews_and_repointed_store(tmp_path):
    cfg, control, gateway, store = _sources(tmp_path)
    gateway.reviews = PartialReviews("read failed")

    partial = read_configured_review_import_remote(
        cfg, control, gateway, store, OpenPullRequestsListed((9,)),
    )
    wrong_store = read_configured_review_import_remote(
        cfg, control, gateway, Store(str(tmp_path / "other")), OpenPullRequestsListed((9,)),
    )

    assert isinstance(partial, BlockedReviewImportRemote)
    assert "complete current review" in partial.reason
    assert isinstance(wrong_store, BlockedReviewImportRemote)
    assert "deployment configuration" in wrong_store.reason


def test_configured_remote_read_refuses_changed_open_listing(tmp_path):
    cfg, control, gateway, store = _sources(tmp_path)
    gateway.listed = ()

    result = read_configured_review_import_remote(
        cfg, control, gateway, store, OpenPullRequestsListed((9,)),
    )

    assert isinstance(result, BlockedReviewImportRemote)
    assert "listing changed" in result.reason
