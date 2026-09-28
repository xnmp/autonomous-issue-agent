"""Complete legacy death attribution preserves every SQL run and file source."""

from dataclasses import replace
import hashlib
import json

import pytest

from issue_agent.domain.resolve_activation import LegacyPresent
from issue_agent.domain.review_activation import OpenPullRequestsListed
from issue_agent.domain.review_execution_activation import (
    CompleteLegacyExecutionFileValues, CompleteLegacyReviewRuns, LegacyExecutionCounterRecord,
    LegacyExecutionRunRecord, LegacyReviewOwnerLineage, LegacyReviewRunState, MAX_SQL_ROWID,
    classify_legacy_execution_file_values,
)
from issue_agent.domain.review_execution_counter_mapping import (
    CompleteLegacyCounterMapping, CompleteLegacyOpenCounterLinks, LegacyOpenPRCounterLink,
    map_legacy_execution_counters,
)
from issue_agent.domain.review_execution_death_import import (
    BlockedLegacyReviewDeathAttribution, CompleteLegacyReviewDeathAttribution,
    attribute_complete_legacy_review_deaths,
)

HEAD = "a" * 40
OWNER = "o/r:review:current"


def _mapping(values: dict[int, str], *links: tuple[int, int | None], repository: str = "o/r") -> CompleteLegacyCounterMapping:
    records = tuple(LegacyExecutionCounterRecord(
        key, LegacyPresent(hashlib.sha256(text.encode()).hexdigest(), text),
    ) for key, text in values.items())
    files = classify_legacy_execution_file_values(records)
    assert isinstance(files, CompleteLegacyExecutionFileValues)
    source = CompleteLegacyOpenCounterLinks(
        repository, OpenPullRequestsListed(tuple(pr for pr, _ in links)),
        tuple(LegacyOpenPRCounterLink(
            pr, HEAD, f"fix/{issue}-work" if issue is not None else "work/topic", None, issue,
        ) for pr, issue in links),
    )
    mapped = map_legacy_execution_counters(files, source)
    assert isinstance(mapped, CompleteLegacyCounterMapping)
    return mapped


def _run(rowid: int, pr: int, key: int, *, repository: str = "o/r",
         state: LegacyReviewRunState = LegacyReviewRunState.ABANDONED,
         dispatch: str | None = None, owner: str = OWNER) -> LegacyExecutionRunRecord:
    identity = json.dumps({
        "repository": repository, "role": "review", "kind": "REVIEW", "number": pr,
        "breaker_key": key, "issue": key if key > 0 else None,
    })
    return LegacyExecutionRunRecord(
        rowid, owner, f"run-{rowid}", identity if dispatch is None else dispatch,
        "REVIEW", state, "cfg", "2026-09-28", None, None, None, None, None, None, None,
    )


def _runs(*items: LegacyExecutionRunRecord, repository: str = "o/r") -> CompleteLegacyReviewRuns:
    lineage = LegacyReviewOwnerLineage(
        repository, OWNER if repository == "o/r" else f"{repository}:review:current", (),
        (OWNER, "o/r:review") if repository == "o/r" else
        (f"{repository}:review:current", f"{repository}:review"), (),
    )
    return CompleteLegacyReviewRuns(lineage, items)


def test_whole_lineage_accounts_for_files_and_abandoned_runs_from_both_keys():
    runs = _runs(_run(1, 9, -9), _run(2, 9, 7),
                 _run(3, 9, -9, state=LegacyReviewRunState.COMPLETED), _run(4, 10, -10))
    mapping = _mapping({7: "2", -9: "3", -10: "4"}, (9, 7), (10, None))

    result = attribute_complete_legacy_review_deaths(runs, mapping)

    assert isinstance(result, CompleteLegacyReviewDeathAttribution)
    assert [(item.pr, item.count, [run.source.run_id for run in item.current_owner_runs])
            for item in result.debits] == [(9, 7, ["run-1", "run-2"]), (10, 5, ["run-4"])]
    assert result.unassigned_deaths == ()
    assert result.runs == runs and result.mapping == mapping


def test_closed_pr_death_stays_visible_without_an_open_counter_key():
    result = attribute_complete_legacy_review_deaths(
        _runs(_run(1, 11, -11)), _mapping({-11: "2"}, (9, None)),
    )

    assert isinstance(result, CompleteLegacyReviewDeathAttribution)
    assert [(item.pr, item.count) for item in result.debits] == [(9, 0)]
    assert [(item.source.run_id, item.counter_key) for item in result.unassigned_deaths] == [("run-1", -11)]
    assert [item.counter.source.key for item in result.mapping.unassigned] == [-11]


def test_predecessor_death_is_retained_without_inflating_the_current_owner_count():
    result = attribute_complete_legacy_review_deaths(
        _runs(_run(1, 9, -9, owner="o/r:review"), _run(2, 9, -9)),
        _mapping({-9: "2"}, (9, None)),
    )

    assert isinstance(result, CompleteLegacyReviewDeathAttribution)
    debit = result.debits[0]
    assert debit.count == 3
    assert [run.source.run_id for run in debit.current_owner_runs] == ["run-2"]
    assert [run.source.run_id for run in debit.predecessor_runs] == ["run-1"]


def test_relinked_issue_death_cannot_be_credited_to_another_open_pr():
    result = attribute_complete_legacy_review_deaths(
        _runs(_run(1, 9, 7)), _mapping({}, (9, None), (10, 7)),
    )

    assert isinstance(result, BlockedLegacyReviewDeathAttribution)
    assert "another open PR" in result.reason


def test_shared_issue_death_blocks_even_without_an_issue_file():
    result = attribute_complete_legacy_review_deaths(
        _runs(_run(1, 9, 7)), _mapping({}, (9, 7), (10, 7)),
    )

    assert isinstance(result, BlockedLegacyReviewDeathAttribution)
    assert "multiple open PRs" in result.reason


@pytest.mark.parametrize("run", [
    _run(1, 9, -9, dispatch="opaque"),
    _run(1, 9, -9, repository="other/r"),
])
def test_ambiguous_or_foreign_abandoned_run_blocks_whole_set(run):
    result = attribute_complete_legacy_review_deaths(_runs(run), _mapping({}, (9, None)))

    assert isinstance(result, BlockedLegacyReviewDeathAttribution)


def test_file_plus_run_overflow_blocks_attribution():
    result = attribute_complete_legacy_review_deaths(
        _runs(_run(1, 9, -9)), _mapping({-9: str(MAX_SQL_ROWID)}, (9, None)),
    )

    assert isinstance(result, BlockedLegacyReviewDeathAttribution)
    assert "exceeds" in result.reason


def test_complete_variant_rejects_omitted_or_forged_attribution():
    complete = attribute_complete_legacy_review_deaths(
        _runs(_run(1, 9, -9)), _mapping({-9: "2"}, (9, None)),
    )
    assert isinstance(complete, CompleteLegacyReviewDeathAttribution)

    with pytest.raises(ValueError):
        replace(complete, debits=())
    with pytest.raises(ValueError):
        replace(complete, unassigned_deaths=complete.debits[0].current_owner_runs)
    with pytest.raises(ValueError):
        replace(complete, debits=(replace(complete.debits[0], current_owner_runs=()),))
    with pytest.raises(ValueError):
        replace(complete, debits=(replace(complete.debits[0], predecessor_runs=complete.debits[0].current_owner_runs),))
    with pytest.raises(ValueError):
        attribute_complete_legacy_review_deaths(None, complete.mapping)  # type: ignore[arg-type]


def test_source_digest_changes_with_death_and_mapping_evidence():
    base = attribute_complete_legacy_review_deaths(_runs(_run(1, 9, -9)), _mapping({}, (9, None)))
    changed_run = attribute_complete_legacy_review_deaths(
        _runs(replace(_run(1, 9, -9), completed_at="later")), _mapping({}, (9, None)),
    )
    changed_mapping = attribute_complete_legacy_review_deaths(_runs(_run(1, 9, -9)),
                                                               _mapping({-9: "1"}, (9, None)))

    assert isinstance(base, CompleteLegacyReviewDeathAttribution)
    assert isinstance(changed_run, CompleteLegacyReviewDeathAttribution)
    assert isinstance(changed_mapping, CompleteLegacyReviewDeathAttribution)
    assert len({base.source_digest, changed_run.source_digest, changed_mapping.source_digest}) == 3


def test_attribution_is_independent_of_sql_page_order():
    first = _run(1, 9, -9)
    second = _run(2, 9, -9)
    mapping = _mapping({}, (9, None))

    forward = attribute_complete_legacy_review_deaths(_runs(first, second), mapping)
    reversed_result = attribute_complete_legacy_review_deaths(_runs(second, first), mapping)

    assert isinstance(forward, CompleteLegacyReviewDeathAttribution)
    assert isinstance(reversed_result, CompleteLegacyReviewDeathAttribution)
    assert forward.source_digest == reversed_result.source_digest
    assert forward.debits == reversed_result.debits


def test_repository_mismatch_blocks_even_without_a_death():
    result = attribute_complete_legacy_review_deaths(_runs(), _mapping({}, (9, None), repository="other/r"))

    assert isinstance(result, BlockedLegacyReviewDeathAttribution)
    assert "different repositories" in result.reason
