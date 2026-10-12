"""Worktree management, driven by a fake git runner."""

import hashlib
import hmac
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from issue_agent.adapters import worktree as wt_mod
from issue_agent.adapters.worktree import (
    GitResult,
    WorktreeUnavailable,
    Worktrees,
)


class FakeGit:
    """Records git argv and answers from a scripted table keyed by a prefix of
    the joined args. Default: rc=0, empty out."""

    def __init__(self, table=None):
        self.calls = []
        self.table = table or {}

    def __call__(self, args, cwd):
        self.calls.append((args, cwd))
        key = " ".join(args)
        for prefix, result in self.table.items():
            if key.startswith(prefix):
                return result
        # `cat-file -e` on an unscripted path means "absent" → non-zero, like
        # real git (an unmatched probe must NOT read as "file exists at base").
        if key.startswith("cat-file -e"):
            return GitResult(1, "")
        return GitResult(0, "")


def test_ensure_reuses_existing_worktree():
    fg = FakeGit({
        "worktree list": GitResult(0, "worktree /repo/.claude/worktrees/issue-5\nbranch refs/heads/fix/5-x\n"),
        "branch --show-current": GitResult(0, "fix/5-x\n"),
    })
    # /repo isn't a real directory in the test environment — the honesty check
    # (M20, below) is exercised separately; these git-wiring tests fake it away.
    wt = Worktrees("/repo", run=fg, path_exists=lambda p: True)
    path = wt.ensure(5, "fix/5-x")
    assert path.endswith("issue-5")
    # existing → no `worktree add`
    assert not any(a[0][:2] == ["worktree", "add"] for a in fg.calls)


def test_concurrent_worktree_setup_serializes_shared_git_metadata():
    active = 0
    maximum = 0
    guard = threading.Lock()

    def git(_args, _cwd):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.002)
        with guard:
            active -= 1
        if _args == ["branch", "--show-current"]:
            return GitResult(0, "fix/5-x" if _cwd.endswith("issue-5") else "fix/6-y")
        return GitResult(0, "")

    worktrees = Worktrees("/repo", run=git, path_exists=lambda _path: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = tuple(pool.map(
            lambda spec: worktrees.ensure(*spec),
            ((5, "fix/5-x"), (6, "fix/6-y")),
        ))

    assert paths[0].endswith("issue-5")
    assert paths[1].endswith("issue-6")
    assert maximum == 1


def test_ensure_does_not_reuse_issue_with_prefix_collision():
    fg = FakeGit({
        "worktree list": GitResult(0, "worktree /repo/.claude/worktrees/issue-50\n"),
        "show-ref": GitResult(1, ""),
        "branch --show-current": GitResult(0, "fix/5-x"),
    })
    Worktrees("/repo", base_branch="dev", run=fg, path_exists=lambda p: True).ensure(5, "fix/5-x")

    assert any(a[0][:2] == ["worktree", "add"] for a in fg.calls)


def test_ensure_creates_from_base_when_no_branch():
    fg = FakeGit({
        "worktree list": GitResult(0, ""),                 # none yet
        "branch --show-current": GitResult(0, "fix/5-x"),
        "show-ref": GitResult(1, ""),                       # branch doesn't exist
    })
    wt = Worktrees("/repo", base_branch="dev", run=fg, path_exists=lambda p: True)
    wt.ensure(5, "fix/5-x")
    add = [a for a in fg.calls if a[0][:2] == ["worktree", "add"]]
    assert add and "-b" in add[0][0] and "origin/dev" in add[0][0]


# ── M20: `ensure` must be HONEST about readiness ──────────────────────────────
def test_ensure_raises_when_every_worktree_add_attempt_fails():
    """Before this, `ensure` returned the path regardless of whether any
    `worktree add` actually succeeded — the caller logged "ready" and
    dispatched the agent into a directory that was never created. With the
    REAL default `path_exists` (os.path.isdir) and a repo dir that plainly
    doesn't exist on disk, `ensure` must raise rather than lie."""
    fg = FakeGit({
        "worktree list": GitResult(0, ""),                       # none yet
        "show-ref": GitResult(1, ""),                             # branch doesn't exist
        "worktree add": GitResult(1, "fatal: boom"),              # every add attempt fails
    })
    wt = Worktrees("/definitely/not/a/real/repo-dir", base_branch="dev", run=fg)

    with pytest.raises(WorktreeUnavailable):
        wt.ensure(5, "fix/5-x")


def test_ensure_returns_real_path_when_worktree_add_actually_creates_it(tmp_path):
    """Anti-vacuity twin: when a 'worktree add' genuinely produces the
    directory, `ensure` must still succeed — the honesty check is a real
    verification, not a trap that also denies the success path. Uses the REAL
    default `path_exists` against a real tmp_path directory."""
    repo_dir = str(tmp_path)
    expected_rel = Worktrees(repo_dir).path(7)
    expected_abs = os.path.join(repo_dir, expected_rel)

    def git(args, cwd):
        if args[:2] == ["worktree", "add"]:
            os.makedirs(expected_abs, exist_ok=True)
            return GitResult(0, "")
        if args[0] == "show-ref":
            return GitResult(1, "")
        if args == ["branch", "--show-current"]:
            return GitResult(0, "fix/7-x")
        return GitResult(0, "")

    wt = Worktrees(repo_dir, base_branch="dev", run=git)

    path = wt.ensure(7, "fix/7-x")

    assert path == expected_abs
    assert os.path.isdir(path)


# ── M20: the git runner must be bounded and must surface failures ────────────
def test_default_git_runner_times_out_and_reports_rather_than_hanging(tmp_path, monkeypatch, capsys):
    """A hanging `git` subprocess must be killed and reported as a failure, not
    hang the daemon or the agent's session — same failure class as the 14-min
    `gh pr view` hang (github.py's `_GH_TIMEOUT_S`). Uses a REAL subprocess: a
    fake `git` on PATH that sleeps, with `_GIT_TIMEOUT_S` monkeypatched tiny."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nsleep 5\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr(wt_mod, "_GIT_TIMEOUT_S", 0.2)

    result = wt_mod._subprocess_git(["status"], str(tmp_path))

    assert result.code != 0            # reported as failure, never a silent pass
    err = capsys.readouterr().err
    assert "timed out" in err.lower()


def test_default_git_runner_surfaces_stderr_on_failure(tmp_path, monkeypatch, capsys):
    """A non-zero `git` exit must be surfaced (not silently swallowed) so a
    wedged worktree op is visible in the daemon log — the other half of M20."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\necho 'fatal: kaboom' >&2\nexit 1\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    result = wt_mod._subprocess_git(["worktree", "add", "x", "y"], str(tmp_path))

    assert result.code == 1
    err = capsys.readouterr().err
    assert "kaboom" in err


def test_default_git_runner_real_success_path_unaffected(tmp_path):
    """Anti-vacuity twin: the timeout/stderr-surfacing changes must not disturb
    the ordinary (fast, well-behaved) case — a real `git` invocation still
    returns its stdout untouched."""
    result = wt_mod._subprocess_git(["--version"], str(tmp_path))

    assert result.code == 0
    assert "git version" in result.out


class RemovalGit:
    def __init__(self, branch="fix/5-small-win", remove_code=0):
        self.branch = branch
        self.remove_code = remove_code
        self.present = True
        self.branch_present = bool(branch)
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((args, cwd))
        if args[:2] == ["worktree", "list"]:
            return GitResult(
                0,
                (
                    "worktree /repo/.claude/worktrees/issue-5\n"
                    "HEAD deadbeef\n"
                    f"branch refs/heads/{self.branch}\n\n"
                    if self.present
                    else ""
                ),
            )
        if args[:2] == ["worktree", "remove"]:
            if self.remove_code == 0:
                self.present = False
            return GitResult(self.remove_code, "")
        if args[:2] == ["branch", "-D"]:
            self.branch_present = False
            return GitResult(0, "")
        if args[:2] == ["show-ref", "--verify"]:
            return GitResult(0 if self.branch_present else 1, "")
        return GitResult(0, "")


def test_remove_calls_remove_prune_and_deletes_the_issue_branch():
    git = RemovalGit()
    Worktrees("/repo", run=git).remove(5)
    kinds = [a[0][:2] for a in git.calls]
    assert ["worktree", "remove"] in kinds and ["worktree", "prune"] in kinds
    assert (["branch", "-D", "fix/5-small-win"], "/repo") in git.calls


def test_remove_failure_is_retriable_and_preserves_branch():
    git = RemovalGit(remove_code=1)

    with pytest.raises(WorktreeUnavailable, match="refused to remove"):
        Worktrees("/repo", run=git).remove(5)

    assert git.present
    assert git.branch_present
    assert not any(args[:2] == ["branch", "-D"] for args, _cwd in git.calls)


def test_remove_never_deletes_a_branch_when_the_issue_worktree_is_absent():
    fg = FakeGit({"worktree list": GitResult(0, "")})

    Worktrees("/repo", run=fg).remove(5)

    assert not any(args[:2] == ["branch", "-D"] for args, _cwd in fg.calls)


def test_remove_never_deletes_an_arbitrary_branch_from_the_issue_worktree():
    git = RemovalGit(branch="dev")

    with pytest.raises(WorktreeUnavailable, match="unowned"):
        Worktrees("/repo", run=git).remove(5)

    assert git.present
    assert not any(args[:2] == ["branch", "-D"] for args, _cwd in git.calls)


# ── ReviewCheckouts: pinned per-PR review worktrees (keifu #165) ─────────────
SHA = "2250d0f56c91d536b375ff1fc7e228d44f357470"
RC_PATH = "/repo/.claude/worktrees/review-pr-165"
# A directory on the path is only trustworthy when git agrees its toplevel IS
# that path — anything else is a plain dir whose git commands would hit the
# PARENT repo (0d2e295 H1).
TOPLEVEL_OK = {"rev-parse --show-toplevel": GitResult(0, RC_PATH + "\n")}


def _review_checkouts(table, exists=lambda p: False, remove_tree=None):
    fg = FakeGit(table)
    rc = wt_mod.ReviewCheckouts(
        "/repo", run=fg, path_exists=exists,
        remove_tree=remove_tree or (lambda p: None),
    )
    return rc, fg


def test_review_checkout_creates_detached_worktree_at_head():
    rc, fg = _review_checkouts(
        {
            f"cat-file -e {SHA}": GitResult(0, ""),
            "rev-parse HEAD": GitResult(0, SHA + "\n"),
        },
        exists=lambda p: p.endswith("review-pr-165"),
    )
    # First call: path does not exist yet → worktree add. Simulate by making
    # exists() False before add: use a stateful exists.
    seen = {"added": False}

    def exists(path):
        return seen["added"]

    rc._path_exists = exists
    original = fg.table
    real_call = fg.__call__

    def call(args, cwd):
        if args[:2] == ["worktree", "add"]:
            seen["added"] = True
        return real_call(args, cwd)

    rc._git = call
    path = rc.ensure(165, SHA)
    assert path == "/repo/.claude/worktrees/review-pr-165"
    joined = [" ".join(a) for a, _ in fg.calls]
    assert any(j.startswith("fetch origin refs/pull/165/head") for j in joined)
    assert any(
        j.startswith(f"worktree add --detach .claude/worktrees/review-pr-165 {SHA}")
        for j in joined
    )


def test_review_checkout_refreshes_existing_worktree_and_verifies_head():
    rc, fg = _review_checkouts(
        {
            **TOPLEVEL_OK,
            f"cat-file -e {SHA}": GitResult(0, ""),
            "rev-parse HEAD": GitResult(0, SHA + "\n"),
        },
        exists=lambda p: True,
    )
    path = rc.ensure(165, SHA)
    joined = [" ".join(a) for a, _ in fg.calls]
    assert any(j.startswith(f"checkout --detach {SHA}") for j in joined)
    assert any(j.startswith(f"reset --hard {SHA}") for j in joined)
    assert any(j.startswith("clean -fd") for j in joined)
    # Memoized second call for the same head revalidates cheaply (toplevel +
    # HEAD) but pays no fetch/refresh work.
    n = len(fg.calls)
    assert rc.ensure(165, SHA) == path
    later = [" ".join(a) for a, _ in fg.calls[n:]]
    assert not any(
        j.startswith(("fetch", "checkout", "reset", "clean", "worktree"))
        for j in later
    )


def test_review_checkout_raises_when_head_is_unreachable():
    rc, _fg = _review_checkouts(
        {**TOPLEVEL_OK, f"cat-file -e {SHA}": GitResult(1, "")},
        exists=lambda p: True,
    )
    with pytest.raises(WorktreeUnavailable):
        rc.ensure(165, SHA)


def test_review_checkout_raises_when_verified_head_differs():
    rc, _fg = _review_checkouts(
        {
            **TOPLEVEL_OK,
            f"cat-file -e {SHA}": GitResult(0, ""),
            "rev-parse HEAD": GitResult(0, "deadbeef\n"),
        },
        exists=lambda p: True,
    )
    with pytest.raises(WorktreeUnavailable):
        rc.ensure(165, SHA)


def test_review_checkout_remove_forgets_memo():
    rc, fg = _review_checkouts(
        {
            **TOPLEVEL_OK,
            f"cat-file -e {SHA}": GitResult(0, ""),
            "rev-parse HEAD": GitResult(0, SHA + "\n"),
        },
        exists=lambda p: True,
    )
    rc.ensure(165, SHA)
    rc.remove(165)
    joined = [" ".join(a) for a, _ in fg.calls]
    assert any(
        j.startswith("worktree remove --force .claude/worktrees/review-pr-165")
        for j in joined
    )
    n = len(fg.calls)
    rc.ensure(165, SHA)  # re-ensures after removal (no stale memo)
    assert len(fg.calls) > n


def test_review_checkout_never_mutates_a_plain_dir_on_the_worktree_path():
    """0d2e295 H1 (data-loss hazard): a plain UNREGISTERED directory on the
    worktree path (partial remove; a prune racing an add) makes git discover
    the PARENT repo, so a reset/clean 'refresh' would destroy the operator's
    live checkout — and the rev-parse confirmation would pass because the
    parent's HEAD had just been moved. The stale dir must be deleted and the
    worktree recreated; no checkout/reset/clean may ever run inside it."""
    removed: list[str] = []
    rc, fg = _review_checkouts(
        {
            # git inside the plain dir answers with the PARENT's toplevel.
            "rev-parse --show-toplevel": GitResult(0, "/repo\n"),
            f"cat-file -e {SHA}": GitResult(0, ""),
            "rev-parse HEAD": GitResult(0, SHA + "\n"),
        },
        exists=lambda p: True,
        remove_tree=removed.append,
    )
    path = rc.ensure(165, SHA)
    assert path == RC_PATH
    assert removed == [RC_PATH]
    joined = [(" ".join(a), cwd) for a, cwd in fg.calls]
    # The dir was recreated as a real worktree, and never mutated in place.
    assert any(j.startswith("worktree add --detach") for j, _ in joined)
    assert not any(
        j.startswith(("checkout --detach", "reset --hard", "clean -fd"))
        for j, _ in joined
    )


def test_review_checkout_defers_when_the_stale_dir_cannot_be_removed():
    def explode(_path):
        raise OSError("permission denied")

    rc, _fg = _review_checkouts(
        {"rev-parse --show-toplevel": GitResult(0, "/repo\n")},
        exists=lambda p: True,
        remove_tree=explode,
    )
    with pytest.raises(WorktreeUnavailable):
        rc.ensure(165, SHA)


def test_review_checkout_prunes_before_add_so_a_lost_dir_self_heals():
    """0d2e295 H2: a worktree registration whose directory is gone makes
    `worktree add` fail forever. ensure() must prune the dead registration
    before adding, so one deferral never becomes a permanent one."""
    added = {"done": False}

    def exists(_p):
        return added["done"]

    fg = FakeGit(
        {
            f"cat-file -e {SHA}": GitResult(0, ""),
            "rev-parse HEAD": GitResult(0, SHA + "\n"),
        }
    )

    def call(args, cwd):
        if args[:2] == ["worktree", "add"]:
            added["done"] = True
        return fg(args, cwd)

    rc = wt_mod.ReviewCheckouts(
        "/repo", run=call, path_exists=exists, remove_tree=lambda p: None
    )
    rc.ensure(165, SHA)
    joined = [" ".join(a) for a, _ in fg.calls]
    assert joined.index("worktree prune") < joined.index(
        f"worktree add --detach .claude/worktrees/review-pr-165 {SHA}"
    )


def test_review_checkout_defers_when_the_dir_cannot_be_classified():
    """Adversarial F2: a rev-parse that FAILS (timeout, spawn failure under
    process pressure) means 'could not classify' — the dir must be left alone
    and the round deferred, never rmtree'd as garbage."""
    removed: list[str] = []
    rc, _fg = _review_checkouts(
        {"rev-parse --show-toplevel": GitResult(1, "")},
        exists=lambda p: True,
        remove_tree=removed.append,
    )
    with pytest.raises(WorktreeUnavailable):
        rc.ensure(165, SHA)
    assert removed == []


def test_review_checkout_reverifies_the_worktree_after_the_fetch():
    """Adversarial F3: classification and mutation are separated by a network
    fetch; if the .git link vanishes in that window the refresh must defer,
    not run against whatever git now discovers."""
    calls = {"n": 0}

    def flapping_git(args, cwd):
        if args == ["rev-parse", "--show-toplevel"]:
            calls["n"] += 1
            # First classification: a real worktree. Re-check after fetch:
            # git now discovers the PARENT repo.
            out = RC_PATH if calls["n"] == 1 else "/repo"
            return GitResult(0, out + "\n")
        if args[:2] == ["cat-file", "-e"]:
            return GitResult(0, "")
        return GitResult(0, "")

    rc = wt_mod.ReviewCheckouts(
        "/repo", run=flapping_git, path_exists=lambda p: True,
        remove_tree=lambda p: None,
    )
    with pytest.raises(WorktreeUnavailable):
        rc.ensure(165, SHA)


def test_review_checkout_unlinks_a_symlink_without_touching_its_pointee(tmp_path):
    """Adversarial F4: a symlink squatting on the worktree path must be
    unlinked itself — deleting its POINTEE could destroy a sibling PR's
    checkout while the link kept squatting forever."""
    repo = tmp_path / "repo"
    wt_root = repo / ".claude" / "worktrees"
    wt_root.mkdir(parents=True)
    victim = wt_root / "review-pr-99"
    victim.mkdir()
    (victim / "keep.txt").write_text("sibling checkout")
    link = wt_root / "review-pr-165"
    link.symlink_to(victim)

    def git(args, cwd):
        if args == ["rev-parse", "--show-toplevel"]:
            return GitResult(0, "/somewhere/else\n")  # not this worktree
        if args[:2] == ["worktree", "add"]:
            link.mkdir()  # a real dir replaces the unlinked squatter
            return GitResult(0, "")
        if args == ["rev-parse", "HEAD"]:
            return GitResult(0, SHA + "\n")
        if args[:2] == ["cat-file", "-e"]:
            return GitResult(0, "")
        return GitResult(0, "")

    exploded: list[str] = []
    rc = wt_mod.ReviewCheckouts(
        str(repo), run=git, remove_tree=exploded.append
    )
    rc.ensure(165, SHA)

    assert not link.is_symlink()  # the squatting link itself is gone…
    assert (victim / "keep.txt").read_text() == "sibling checkout"  # …pointee intact
    assert exploded == []  # and rmtree was never reached


def test_a_symlink_to_the_parent_repo_is_never_classified_as_the_worktree(tmp_path):
    """Adversarial N1: a link pointing at the PARENT repo makes both realpaths
    resolve to the parent, so the toplevel comparison alone would classify it
    as a valid worktree and reset --hard the operator's live checkout. A link
    must always take the unlink path, whatever it points at."""
    repo = tmp_path / "repo"
    wt_root = repo / ".claude" / "worktrees"
    wt_root.mkdir(parents=True)
    (repo / "precious.txt").write_text("operator work")
    link = wt_root / "review-pr-165"
    link.symlink_to(repo)

    mutated: list[str] = []

    def git(args, cwd):
        if args == ["rev-parse", "--show-toplevel"]:
            return GitResult(0, str(repo) + "\n")  # the parent answers
        if args[0] in ("checkout", "reset", "clean"):
            mutated.append(" ".join(args))
            return GitResult(0, "")
        if args[:2] == ["worktree", "add"]:
            link.mkdir()
            return GitResult(0, "")
        if args == ["rev-parse", "HEAD"]:
            return GitResult(0, SHA + "\n")
        if args[:2] == ["cat-file", "-e"]:
            return GitResult(0, "")
        return GitResult(0, "")

    rc = wt_mod.ReviewCheckouts(
        str(repo), run=git, remove_tree=lambda p: None
    )
    rc.ensure(165, SHA)

    assert mutated == []                       # nothing ran inside the link
    assert not link.is_symlink()               # squatter replaced by a real dir
    assert (repo / "precious.txt").read_text() == "operator work"


def test_remove_stale_dir_refuses_paths_outside_the_worktree_root():
    """The single most safety-critical line in the class: nothing outside
    `<repo>/<worktree_root>/` may ever be deleted. (Seam pin — ensure() only
    constructs in-root paths, so no public path reaches this today.)"""
    removed: list[str] = []
    rc, _fg = _review_checkouts({}, remove_tree=removed.append)
    with pytest.raises(WorktreeUnavailable):
        rc._remove_stale_dir(165, "/repo/src")
    assert removed == []


def test_review_checkout_add_only_succeeds_because_prune_ran_first():
    """H2 efficacy (not just ordering): with a dead registration, `worktree
    add` FAILS until prune has run. A regression that reorders or drops the
    prune must fail this test, not just an ordering assertion."""
    state = {"pruned": False, "added": False}

    def git(args, cwd):
        if args == ["worktree", "prune"]:
            state["pruned"] = True
            return GitResult(0, "")
        if args[:2] == ["worktree", "add"]:
            if not state["pruned"]:
                return GitResult(128, "already registered worktree")
            state["added"] = True
            return GitResult(0, "")
        if args[:2] == ["cat-file", "-e"]:
            return GitResult(0, "")
        if args == ["rev-parse", "HEAD"]:
            return GitResult(0, SHA + "\n")
        return GitResult(0, "")

    rc = wt_mod.ReviewCheckouts(
        "/repo", run=git, path_exists=lambda p: state["added"],
        remove_tree=lambda p: None,
    )
    assert rc.ensure(165, SHA) == RC_PATH


def test_review_checkout_defers_when_a_refresh_step_fails():
    """0d2e295 H1 (exit codes): a failed checkout/reset must defer the round,
    never fall through to the rev-parse confirmation."""
    rc, _fg = _review_checkouts(
        {
            **TOPLEVEL_OK,
            f"cat-file -e {SHA}": GitResult(0, ""),
            f"checkout --detach {SHA}": GitResult(1, "index.lock held"),
            "rev-parse HEAD": GitResult(0, SHA + "\n"),
        },
        exists=lambda p: True,
    )
    with pytest.raises(WorktreeUnavailable):
        rc.ensure(165, SHA)
