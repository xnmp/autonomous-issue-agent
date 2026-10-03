"""Untrusted control files must not restore an exhausted attempt allowance."""

import pytest

from issue_agent.adapters.store import Store


@pytest.mark.parametrize("raw", ["oops", "", "-1", "1.5", "9" * 5000, "\ufffd"])
@pytest.mark.parametrize("suffix,reader", [
    ("infra-deaths", "get_infra_deaths"),
])
def test_malformed_counter_never_becomes_fresh_allowance(tmp_path, raw, suffix, reader):
    store = Store(str(tmp_path))
    (tmp_path / f"issue-7.{suffix}").write_text(raw)
    with pytest.raises(ValueError):
        getattr(store, reader)(7)


def test_missing_counter_is_a_new_workflow(tmp_path):
    assert Store(str(tmp_path)).get_infra_deaths(7) == 0


def test_unreadable_counter_does_not_become_absent(tmp_path):
    store = Store(str(tmp_path))
    (tmp_path / "issue-7.infra-deaths").mkdir()
    with pytest.raises((OSError, ValueError)):
        store.get_infra_deaths(7)


def test_control_delete_failure_is_reported(tmp_path):
    store = Store(str(tmp_path))
    (tmp_path / "issue-7.ci-history").mkdir()
    with pytest.raises((OSError, ValueError)):
        store.clear_ci_history(7)


@pytest.mark.parametrize("raw", ["", "garbage", "not-a-timestamp"])
def test_corrupt_consumed_reply_cannot_become_a_new_reply(tmp_path, raw):
    from issue_agent.adapters.continuation_activation import read_continuation_roots
    from issue_agent.domain.continuation_import import classify_record
    from issue_agent.domain.workflow_identity import WorkflowFamily
    (tmp_path / "issue-7.resumed-at").write_text(raw)
    records, _ = read_continuation_roots("o/r", (("fix", str(tmp_path)),), (WorkflowFamily.ISSUE_RECOVERY,))
    blockers = classify_record(records[0])
    assert isinstance(blockers, tuple) and blockers
    assert any("resumed-at" in block.reason or "timestamp" in block.reason for block in blockers)


def test_oversized_control_source_is_refused_by_stopped_import(tmp_path):
    from issue_agent.adapters.continuation_activation import read_continuation_roots
    from issue_agent.domain.resolve_activation import MAX_LEGACY_FILE_BYTES, LegacyUnreadable
    from issue_agent.domain.workflow_identity import WorkflowFamily
    (tmp_path / "issue-7.session").write_text("x" * (MAX_LEGACY_FILE_BYTES + 1))
    records, _ = read_continuation_roots("o/r", (("fix", str(tmp_path)),), (WorkflowFamily.ISSUE_RECOVERY,))
    source = dict(records[0].fields)["session"]
    assert isinstance(source, LegacyUnreadable)
