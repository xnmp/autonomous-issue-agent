"""Untrusted control files must not restore an exhausted attempt allowance."""

import pytest

from issue_agent.adapters.store import Store


@pytest.mark.parametrize("raw", ["oops", "", "-1", "1.5", "9" * 5000, "\ufffd"])
@pytest.mark.parametrize("suffix,reader", [
    ("ci-retries", "get_ci_retries"),
    ("rebase-retries", "get_rebase_retries"),
    ("infra-deaths", "get_infra_deaths"),
])
def test_malformed_counter_never_becomes_fresh_allowance(tmp_path, raw, suffix, reader):
    store = Store(str(tmp_path))
    (tmp_path / f"issue-7.{suffix}").write_text(raw)
    with pytest.raises(ValueError):
        getattr(store, reader)(7)


def test_missing_counter_is_a_new_workflow(tmp_path):
    assert Store(str(tmp_path)).get_ci_retries(7) == 0


def test_unreadable_counter_does_not_become_absent(tmp_path):
    store = Store(str(tmp_path))
    (tmp_path / "issue-7.ci-retries").mkdir()
    with pytest.raises((OSError, ValueError)):
        store.get_ci_retries(7)


def test_control_delete_failure_is_reported(tmp_path):
    store = Store(str(tmp_path))
    (tmp_path / "issue-7.infra-deaths").mkdir()
    with pytest.raises((OSError, ValueError)):
        store.clear_infra_deaths(7)


@pytest.mark.parametrize("raw", ["", "garbage", "not-a-timestamp"])
def test_corrupt_consumed_reply_cannot_become_a_new_reply(tmp_path, raw):
    (tmp_path / "issue-7.resumed-at").write_text(raw)
    with pytest.raises(ValueError, match="timestamp"):
        Store(str(tmp_path)).get_resumed_at(7)


def test_corrupt_review_episode_cannot_reset_its_allowance(tmp_path):
    (tmp_path / "issue-7.review-retries").write_text("garbage 3")
    with pytest.raises(ValueError, match="timestamp"):
        Store(str(tmp_path)).get_review_retry(7)


def test_oversized_control_record_is_rejected(tmp_path):
    (tmp_path / "issue-7.session").write_text("x" * 65537)
    with pytest.raises(ValueError, match="limit"):
        Store(str(tmp_path)).get_session(7)
