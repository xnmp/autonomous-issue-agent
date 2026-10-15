"""Real Git contracts for per-issue fix clones: retained work, branch identity,
crash recovery and the repository they borrow objects from."""

import os
import subprocess

import pytest

from issue_agent.adapters.worktree import Worktrees, WorktreeUnavailable

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **IDENTITY}).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """origin, reached over a transport as GitHub is (a bare path would make
    `clone` copy objects instead of borrowing them)."""
    bare = tmp_path / "upstream.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "dev", str(bare))
    return bare


@pytest.fixture
def repo(tmp_path, upstream):
    target = tmp_path / "target"
    git(tmp_path, "init", "-q", "-b", "dev", str(target))
    git(target, "commit", "--allow-empty", "-m", "base")
    git(target, "remote", "add", "origin", f"file://{upstream}")
    git(target, "push", "-q", "origin", "dev")
    return target


def other_push(tmp_path, upstream, branch, message):
    """A commit someone else pushes to `branch` (a human, GitHub's update-branch)."""
    other = tmp_path / "other"
    if not other.exists():
        git(tmp_path, "clone", "-q", f"file://{upstream}", str(other))
    git(other, "fetch", "-q", "origin")
    git(other, "checkout", "-q", "-B", branch, f"origin/{branch}")
    git(other, "commit", "--allow-empty", "-m", message)
    git(other, "push", "-q", "origin", branch)
    return git(other, "rev-parse", "HEAD")


def test_a_fix_checkout_is_a_clone_whose_branch_and_objects_the_repository_never_holds(repo):
    path = Worktrees(str(repo)).ensure(703, "fix/703-new")

    assert git(path, "branch", "--show-current") == "fix/703-new"
    assert git(path, "log", "-1", "--format=%s") == "base"
    assert os.path.isdir(os.path.join(path, ".git"))
    assert git(path, "remote", "get-url", "origin") == git(repo, "remote", "get-url", "origin")
    # Borrowed read-only from the repository; nothing copied.
    borrowed = open(os.path.join(path, ".git/objects/info/alternates")).read().strip()
    assert os.path.realpath(borrowed) == os.path.realpath(repo / ".git/objects")
    git(path, "commit", "--allow-empty", "-m", "leg work")
    assert not git(repo, "branch", "--list", "fix/703-new")
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_a_harness_run_from_a_linked_checkout_clones_from_the_repository_itself(tmp_path, repo):
    harness = tmp_path / "target-harness"  # `issue-agent start --worktree`
    git(repo, "worktree", "add", "-q", "--detach", str(harness))

    path = Worktrees(str(harness)).ensure(703, "fix/703-new")

    borrowed = open(os.path.join(path, ".git/objects/info/alternates")).read().strip()
    assert os.path.realpath(borrowed) == os.path.realpath(repo / ".git/objects")


def test_a_restarted_daemon_reuses_the_clone_with_its_unpushed_work(repo):
    path = Worktrees(str(repo)).ensure(703, "fix/703-new")
    git(path, "commit", "--allow-empty", "-m", "unpushed")

    again = Worktrees(str(repo))  # a new process: nothing in memory

    assert again.branch_for_issue(703) == "fix/703-new"
    assert again.ensure(703, "fix/703-new") == path
    assert git(path, "log", "-1", "--format=%s") == "unpushed"


@pytest.mark.parametrize("leftover", ["empty", "half-cloned"])
def test_a_clone_killed_midway_leaves_only_scratch_the_next_attempt_replaces(repo, leftover):
    worktrees = Worktrees(str(repo))
    scratch = repo / (worktrees.path(703) + ".partial")
    if leftover == "empty":
        scratch.mkdir(parents=True)
    else:
        git(repo, "clone", "-q", "--no-checkout", str(repo), str(scratch))

    path = worktrees.ensure(703, "fix/703-new")

    assert git(path, "branch", "--show-current") == "fix/703-new"
    assert not scratch.exists()


def test_a_failed_clone_is_never_handed_to_a_leg(repo):
    git(repo, "remote", "set-url", "origin", "file:///definitely/not/a/repository.git")
    worktrees = Worktrees(str(repo))

    with pytest.raises(WorktreeUnavailable, match="cannot clone"):
        worktrees.ensure(703, "fix/703-new")
    assert not (repo / worktrees.path(703)).exists()


def test_sync_resets_the_clone_to_the_pushed_head_and_tracks_it_for_force_with_lease(tmp_path, repo, upstream):
    worktrees = Worktrees(str(repo))
    path = worktrees.ensure(703, "fix/703-new")
    git(path, "push", "-q", "origin", "fix/703-new")
    head = other_push(tmp_path, upstream, "fix/703-new", "update-branch")
    git(path, "commit", "--allow-empty", "-m", "unpushed")

    assert worktrees.sync(703, "fix/703-new", head) == path

    assert git(path, "rev-parse", "HEAD") == head
    assert git(path, "rev-parse", "refs/remotes/origin/fix/703-new") == head
    git(path, "commit", "--amend", "--allow-empty", "-m", "rebased")
    git(path, "push", "-q", "--force-with-lease", "origin", "fix/703-new")
    with pytest.raises(WorktreeUnavailable, match="decided head"):
        worktrees.sync(703, "fix/703-new", head)  # the remote moved on


def test_a_head_only_a_review_fetched_survives_the_repositorys_gc_once_synced(tmp_path, repo, upstream):
    # A review fetches a PR head under no ref; a clone that borrowed it from
    # there would break when the repository's gc deletes it.
    worktrees = Worktrees(str(repo))
    path = worktrees.ensure(703, "fix/703-new")
    git(path, "push", "-q", "origin", "fix/703-new")
    head = other_push(tmp_path, upstream, "fix/703-new", "update-branch")
    git(repo, "fetch", "-q", "origin", f"{head}")
    worktrees.sync(703, "fix/703-new", head)
    (repo / ".git/FETCH_HEAD").unlink()  # the next fetch overwrites it anyway

    git(repo, "gc", "-q", "--prune=now")

    assert git(path, "log", "-1", "--format=%s") == "update-branch"
    git(path, "fsck", "--connectivity-only", "--no-dangling")


@pytest.mark.parametrize("planted", ["logs/HEAD", "refs/remotes"])
def test_sync_never_writes_through_a_link_a_leg_left_in_its_clone(tmp_path, repo, planted):
    # A reflog append or a ref under a linked directory would land in a host file.
    worktrees = Worktrees(str(repo))
    path = worktrees.ensure(703, "fix/703-new")
    git(path, "push", "-q", "origin", "fix/703-new")
    victim = tmp_path / "operator-file"
    victim.mkdir() if planted == "refs/remotes" else victim.write_text("operator\n")
    link = os.path.join(path, ".git", planted)
    subprocess.run(["rm", "-rf", link], check=True)
    os.symlink(victim, link)

    with pytest.raises(WorktreeUnavailable, match="link"):
        worktrees.sync(703, "fix/703-new", git(path, "rev-parse", "HEAD"))

    assert (list(victim.iterdir()) == [] if victim.is_dir() else victim.read_text() == "operator\n")


def test_a_clone_has_the_hooks_directory_a_leg_mounts_even_from_a_bare_template(tmp_path, repo, monkeypatch):
    (tmp_path / "template").mkdir()
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(tmp_path / "template"))

    path = Worktrees(str(repo)).ensure(703, "fix/703-new")

    assert os.path.isdir(os.path.join(path, ".git/hooks"))


def test_remove_deletes_the_clone(repo):
    worktrees = Worktrees(str(repo))
    path = worktrees.ensure(703, "fix/703-new")

    worktrees.remove(703)
    worktrees.remove(703)  # already gone: a no-op

    assert not os.path.exists(path)
    assert not os.path.exists(path + ".partial")
    assert git(repo, "rev-parse", "dev")


def test_a_removal_killed_midway_never_blocks_the_issue_coming_back(repo):
    worktrees = Worktrees(str(repo))
    path = worktrees.ensure(703, "fix/703-new")
    os.rename(path, path + ".partial")  # the rename landed; the delete did not

    assert worktrees.ensure(703, "fix/703-new") == path
    assert not os.path.exists(path + ".partial")


def test_a_foreign_clone_is_not_adopted_or_removed(repo):
    worktrees = Worktrees(str(repo))
    path = worktrees.ensure(703, "fix/703-new")
    git(path, "checkout", "-q", "-b", "fix/704-other")  # a leg moved its clone to another issue's branch

    with pytest.raises(WorktreeUnavailable):
        worktrees.branch_for_issue(703)
    with pytest.raises(WorktreeUnavailable, match="requires branch"):
        worktrees.ensure(703, "fix/703-new")
    with pytest.raises(WorktreeUnavailable, match="unowned"):
        worktrees.remove(703)
    assert git(path, "branch", "--show-current") == "fix/704-other"


def test_same_directory_basename_outside_configured_root_is_not_reused(repo):
    worktrees = Worktrees(str(repo))
    foreign = repo / "unrelated" / "issue-703"
    git(repo, "worktree", "add", "-b", "fix/704-other", str(foreign))

    prepared = worktrees.ensure(703, "fix/703-new")

    assert prepared == str(repo / worktrees.path(703))
    assert git(prepared, "branch", "--show-current") == "fix/703-new"
    assert git(foreign, "branch", "--show-current") == "fix/704-other"


def test_a_lost_clone_resumes_on_the_commits_already_pushed_to_its_branch(tmp_path, repo, upstream):
    git(repo, "checkout", "-q", "-b", "fix/703-new")
    git(repo, "commit", "--allow-empty", "-m", "pushed before the PR opened")
    git(repo, "push", "-q", "origin", "fix/703-new")
    git(repo, "checkout", "-q", "dev")
    git(repo, "branch", "-D", "fix/703-new")

    path = Worktrees(str(repo)).ensure(703, "fix/703-new")

    assert git(path, "branch", "--show-current") == "fix/703-new"
    assert git(path, "log", "-1", "--format=%s") == "pushed before the PR opened"


def test_a_legacy_worktree_is_refused_with_its_cleanup_command_and_left_untouched(repo):
    worktrees = Worktrees(str(repo))
    legacy = repo / worktrees.path(703)
    git(repo, "worktree", "add", "-q", "-b", "fix/703-old", str(legacy))

    with pytest.raises(WorktreeUnavailable, match=r"legacy git worktree.*worktree remove --force"):
        worktrees.ensure(703, "fix/703-old")

    assert (legacy / ".git").is_file()
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
