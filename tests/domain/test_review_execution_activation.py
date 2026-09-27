"""Legacy execution source records keep untrusted namespaces distinct."""

import pytest

from issue_agent.domain.resolve_activation import LegacyAbsent
from issue_agent.domain.review_execution_activation import LegacyExecutionCounterRecord, LegacyExecutionPRRecord


@pytest.mark.parametrize("pr", [0, -1, 2**63, True, "7"])
def test_pr_source_requires_a_valid_pull_request_number(pr):
    with pytest.raises(ValueError):
        LegacyExecutionPRRecord(pr, LegacyAbsent(), LegacyAbsent())


@pytest.mark.parametrize("key", [0, -(2**63), 2**63, True, "-7"])
def test_counter_source_requires_a_signed_legacy_target_key(key):
    with pytest.raises(ValueError):
        LegacyExecutionCounterRecord(key, LegacyAbsent())


@pytest.mark.parametrize("record", [
    lambda: LegacyExecutionPRRecord(7, None, LegacyAbsent()),
    lambda: LegacyExecutionCounterRecord(-7, "1"),
    lambda: LegacyExecutionPRRecord(7, LegacyAbsent(), LegacyAbsent(), ""),
])
def test_source_records_reject_untyped_files_and_missing_root(record):
    with pytest.raises(ValueError):
        record()
