"""single_instance — the (role, repo) flock guard that stops duplicate daemons.

The real bug it fixes: two `fix` daemons on the same repo both poll ai-ready and
dispatch concurrently. These tests prove: a second acquire of the same (role,
repo) fails; releasing (or the fd closing) frees it; different roles/repos never
collide; the lock path is stable + filesystem-safe.
"""

import os

import pytest

from issue_agent.adapters.single_instance import (
    AlreadyRunning,
    InstanceLock,
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
