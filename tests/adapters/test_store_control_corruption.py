"""Untrusted control files must not silently become absent state."""

import pytest

from issue_agent.adapters.store import Store


def test_control_delete_failure_is_reported(tmp_path):
    store = Store(str(tmp_path))
    (tmp_path / "issue-7.ci-history").mkdir()
    with pytest.raises((OSError, ValueError)):
        store.clear_ci_history(7)
