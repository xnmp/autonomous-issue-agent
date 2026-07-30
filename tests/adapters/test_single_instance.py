"""single_instance — the (role, repo) flock guard that stops duplicate daemons.

The real bug it fixes: two `fix` daemons on the same repo both poll ai-ready and
dispatch concurrently. These tests prove: a second acquire of the same (role,
repo) fails; releasing (or the fd closing) frees it; different roles/repos never
collide; the lock path is stable + filesystem-safe.
"""

import os

import pytest

from issue_agent.adapters import single_instance
from issue_agent.adapters.single_instance import (
    AlreadyRunning,
    InstanceLock,
    UnsafeLockFile,
    _lock_path,
    acquire,
)


def test_second_acquire_same_role_repo_raises():
    lock = acquire("fix", "owner/repo-A")
    try:
        with pytest.raises(AlreadyRunning):
            acquire("fix", "owner/repo-A")
    finally:
        lock.release()


def test_release_frees_the_lock():
    lock = acquire("fix", "owner/repo-B")
    lock.release()
    # Now a fresh acquire must succeed.
    lock2 = acquire("fix", "owner/repo-B")
    lock2.release()


def test_different_roles_do_not_collide():
    a = acquire("fix", "owner/repo-C")
    b = acquire("review", "owner/repo-C")  # same repo, different role → OK
    try:
        assert isinstance(a, InstanceLock) and isinstance(b, InstanceLock)
    finally:
        a.release()
        b.release()


def test_different_repos_do_not_collide():
    a = acquire("fix", "owner/repo-D")
    b = acquire("fix", "owner/repo-E")  # same role, different repo → OK
    try:
        assert a.path != b.path
    finally:
        a.release()
        b.release()


def test_lock_path_is_stable_and_safe():
    p1 = _lock_path("fix", "owner/name")
    p2 = _lock_path("fix", "owner/name")
    assert p1 == p2  # deterministic
    assert "/" not in os.path.basename(p1)  # slash-bearing repo didn't leak
    assert os.path.basename(p1).startswith("issue-agent-fix-")


def test_release_is_idempotent():
    lock = acquire("steward", "owner/repo-F")
    lock.release()
    lock.release()  # must not raise on a double release


def test_same_repo_different_checkouts_do_not_collide(tmp_path):
    a = acquire("fix", "owner/repo-G", target_dir=str(tmp_path / "one"))
    b = acquire("fix", "owner/repo-G", target_dir=str(tmp_path / "two"))
    try:
        assert a.path != b.path
    finally:
        a.release()
        b.release()


def test_state_root_does_not_change_singleton_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        single_instance.tempfile, "gettempdir", lambda: str(tmp_path)
    )
    lock = acquire(
        "fix",
        "owner/repo-H",
        target_dir="/srv/repo-H",
        state_dir=str(tmp_path / "state-a"),
    )
    try:
        with pytest.raises(AlreadyRunning):
            acquire(
                "fix",
                "owner/repo-H",
                target_dir="/srv/repo-H",
                state_dir=str(tmp_path / "state-b"),
            )
        assert os.path.dirname(lock.path) == str(
            tmp_path / f"issue-agent-{os.getuid()}-locks"
        )
    finally:
        lock.release()


def test_empty_repo_is_rejected():
    with pytest.raises(ValueError, match="REPO"):
        acquire("fix", "")


def test_foreign_symlink_lock_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        single_instance.tempfile, "gettempdir", lambda: str(tmp_path)
    )
    path = _lock_path("fix", "owner/repo-I", state_dir=str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(tmp_path / "somewhere-else", path)
    with pytest.raises(UnsafeLockFile):
        acquire("fix", "owner/repo-I", state_dir=str(tmp_path))
