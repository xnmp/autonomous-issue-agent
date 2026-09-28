"""Legacy file counter attribution follows complete open-PR linkage evidence."""

from dataclasses import replace
import hashlib

import pytest

from issue_agent.domain.resolve_activation import LegacyPresent
from issue_agent.domain.review_activation import OpenPullRequestsListed
from issue_agent.domain.review_execution_activation import (
    CompleteLegacyExecutionFileValues, LegacyExecutionCounterRecord,
    classify_legacy_execution_file_values,
)
from issue_agent.domain.review_execution_counter_mapping import (
    BlockedLegacyCounterLink, BlockedLegacyCounterMapping, CompleteLegacyCounterMapping,
    CompleteLegacyOpenCounterLinks, LegacyOpenPRCounterLink, LegacyUnassignedCounterReason,
    classify_legacy_counter_link, map_legacy_execution_counters,
)
from issue_agent.domain.review_linkage import LinkedIssueUnavailable, NoLinkedIssue, ObservedLinkedIssue
from issue_agent.domain.world import IssueView, PRView

HEAD = "a" * 40


def _counter_files(values: dict[int, str]) -> CompleteLegacyExecutionFileValues:
    entries = tuple(LegacyExecutionCounterRecord(
        key, LegacyPresent(hashlib.sha256(text.encode()).hexdigest(), text),
    ) for key, text in values.items())
    result = classify_legacy_execution_file_values(entries)
    assert isinstance(result, CompleteLegacyExecutionFileValues)
    return result


def _links(*items: tuple[int, int | None]) -> CompleteLegacyOpenCounterLinks:
    links = tuple(LegacyOpenPRCounterLink(
        pr, HEAD, f"fix/{issue}-work" if issue is not None else "work/topic", None, issue,
    ) for pr, issue in items)
    return CompleteLegacyOpenCounterLinks("o/r", OpenPullRequestsListed(tuple(pr for pr, _ in items)), links)


def test_positive_issue_and_negative_pr_counters_map_once_and_preserve_unused_sources():
    files = _counter_files({7: "2", -9: "3", -10: "4", 8: "5", -11: "6"})
    links = _links((9, 7), (10, None))

    mapped = map_legacy_execution_counters(files, links)

    assert isinstance(mapped, CompleteLegacyCounterMapping)
    assert [(item.counter.source.key, item.pr, item.counter.value.value) for item in mapped.assignments] == [
        (-10, 10, 4), (-9, 9, 3), (7, 9, 2),
    ]
    assert [(item.counter.source.key, item.reason) for item in mapped.unassigned] == [
        (-11, LegacyUnassignedCounterReason.NO_OPEN_PR),
        (8, LegacyUnassignedCounterReason.NO_LINKED_OPEN_PR),
    ]


def test_shared_issue_counter_blocks_instead_of_copying_to_two_open_prs():
    files = _counter_files({7: "2"})
    links = _links((9, 7), (10, 7))

    result = map_legacy_execution_counters(files, links)

    assert isinstance(result, BlockedLegacyCounterMapping)
    assert "multiple open PRs" in result.reason
    assert isinstance(map_legacy_execution_counters(_counter_files({-9: "1"}), links),
                      CompleteLegacyCounterMapping)


def test_closing_reference_takes_precedence_over_branch_and_no_link_keeps_negative_key():
    closing = classify_legacy_counter_link(
        PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD, closing_issue=8),
        ObservedLinkedIssue(IssueView(8)),
    )
    unlinked = classify_legacy_counter_link(
        PRView(10, head_ref_name="work/topic", head_ref_oid=HEAD), NoLinkedIssue(),
    )

    assert isinstance(closing, LegacyOpenPRCounterLink) and closing.linked_issue == 8
    assert isinstance(unlinked, LegacyOpenPRCounterLink) and unlinked.linked_issue is None
    links = CompleteLegacyOpenCounterLinks("o/r", OpenPullRequestsListed((9, 10)), (closing, unlinked))
    mapped = map_legacy_execution_counters(_counter_files({7: "1", 8: "2", -10: "3"}), links)
    assert isinstance(mapped, CompleteLegacyCounterMapping)
    assert [(item.counter.source.key, item.pr) for item in mapped.assignments] == [(-10, 10), (8, 9)]
    assert [item.counter.source.key for item in mapped.unassigned] == [7]


@pytest.mark.parametrize("view,linked", [
    (PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD), NoLinkedIssue()),
    (PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD), ObservedLinkedIssue(IssueView(8))),
    (PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD), LinkedIssueUnavailable("offline")),
    (PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD, state="CLOSED"), ObservedLinkedIssue(IssueView(7))),
    (PRView(9, head_ref_name="fix/7-work", head_ref_oid="bad"), ObservedLinkedIssue(IssueView(7))),
    (PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD, closing_issue=True), ObservedLinkedIssue(IssueView(7))),
    (PRView(9, head_ref_name="fix/7-work", head_ref_oid=HEAD, closing_issue=0), ObservedLinkedIssue(IssueView(7))),
    (PRView(9, head_ref_name="fix/" + "9" * 4097 + "-work", head_ref_oid=HEAD), NoLinkedIssue()),
])
def test_malformed_or_unavailable_linkage_cannot_map_counter(view, linked):
    assert isinstance(classify_legacy_counter_link(view, linked), BlockedLegacyCounterLink)


def test_mapping_refuses_overflow_and_forged_or_partial_results():
    files = _counter_files({7: str(2**63 - 1), -9: "1"})
    links = _links((9, 7))
    assert isinstance(map_legacy_execution_counters(files, links), BlockedLegacyCounterMapping)

    valid = map_legacy_execution_counters(_counter_files({7: "2", -9: "1"}), links)
    assert isinstance(valid, CompleteLegacyCounterMapping)
    with pytest.raises(ValueError):
        replace(valid, assignments=valid.assignments[:1])
    with pytest.raises(ValueError):
        replace(valid, assignments=(replace(valid.assignments[0], pr=10), *valid.assignments[1:]))
    with pytest.raises(ValueError):
        CompleteLegacyOpenCounterLinks("o/r", OpenPullRequestsListed((9, 10)), links.links)


def test_mapping_digest_changes_with_file_or_link_source():
    base = map_legacy_execution_counters(_counter_files({7: "2"}), _links((9, 7)))
    changed_value = map_legacy_execution_counters(_counter_files({7: "3"}), _links((9, 7)))
    changed_link = map_legacy_execution_counters(_counter_files({7: "2"}),
                                                 CompleteLegacyOpenCounterLinks("o/r",
                                                     OpenPullRequestsListed((9,)),
                                                     (LegacyOpenPRCounterLink(9, "b" * 40,
                                                                              "fix/7-work", None, 7),)))
    assert isinstance(base, CompleteLegacyCounterMapping)
    assert isinstance(changed_value, CompleteLegacyCounterMapping)
    assert isinstance(changed_link, CompleteLegacyCounterMapping)
    assert len({base.source_digest, changed_value.source_digest, changed_link.source_digest}) == 3


def test_complete_link_source_binds_repository_even_for_identical_numbers():
    links = _links((9, 7))
    other = replace(links, repository="another/repo")

    assert other.source_digest != links.source_digest
    with pytest.raises(ValueError):
        replace(links, repository="not-a-repository")
