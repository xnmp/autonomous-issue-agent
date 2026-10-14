"""The leg mount plan (#413): what a paid leg can and cannot see."""

from __future__ import annotations

import pytest

from issue_agent.domain.sandbox import (
    Access,
    GitLink,
    LegFacts,
    UnsafeMount,
    claude_session_store,
    leg_mount_plan,
)

HOME = "/home/op"
HARNESS = "/home/op/Repos/harness"
STATE = "/home/op/.local/state/issue-agent"
WORKTREE = "/home/op/Repos/target/.claude/worktrees/issue-5"
GIT = GitLink("/home/op/Repos/target/.git", "/home/op/Repos/target/.git/worktrees/issue-5")


def facts(**overrides) -> LegFacts:
    values = dict(
        home=HOME, cwd=WORKTREE, writable=True, git=GIT, runtime_binary="/usr/bin/claude",
        runtime_state=("/home/op/.claude/projects/x", "/home/op/.claude/.credentials.json"),
        search_path=("/home/op/.cargo/bin", "/usr/bin"), protected=(HARNESS, STATE),
    )
    values.update(overrides)
    return LegFacts(**values)


def access(plan, path: str) -> Access | None:
    return next((m.access for m in plan.mounts if m.path == path), None)


def visible(plan, path: str) -> Access | None:
    """The access the deepest mount covering `path` gives, as bwrap applies
    them in order (a later mount covers an earlier one)."""
    found = None
    for mount in plan.mounts:
        if path == mount.path or path.startswith(mount.path.rstrip("/") + "/"):
            found = mount.access
    return found


def test_a_fix_leg_writes_its_checkout_and_publishes():
    plan = leg_mount_plan(facts())

    assert visible(plan, f"{WORKTREE}/src/app.py") is Access.READ_WRITE
    assert visible(plan, f"{GIT.common_dir}/objects/ab/cdef") is Access.READ_WRITE
    assert visible(plan, f"{GIT.common_dir}/refs/heads/fix/5") is Access.READ_WRITE
    assert visible(plan, f"{GIT.worktree_dir}/index") is Access.READ_WRITE
    assert plan.publishes and plan.cwd == WORKTREE
    # Its one token arrives from the launcher; every other GitHub token is unset.
    unset = {name for name, value in plan.env if value is None}
    assert {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"} <= unset
    assert not {"GH_TOKEN", "GITHUB_TOKEN"} & unset


def test_the_harness_runs_git_there_so_a_fix_leg_never_changes_git_config_hooks_or_links():
    plan = leg_mount_plan(facts())

    # Writes beside them (packed-refs.lock, a scratch worktree) vanish with the leg.
    for path in (f"{GIT.common_dir}/config", f"{GIT.common_dir}/hooks/post-checkout",
                 f"{GIT.common_dir}/packed-refs", f"{GIT.common_dir}/worktrees/issue-9/commondir"):
        assert visible(plan, path) is Access.THROWAWAY, path
    # Its own checkout's links sit in a directory it writes, so they are read-only.
    for path in (f"{GIT.worktree_dir}/commondir", f"{GIT.worktree_dir}/gitdir", f"{GIT.worktree_dir}/config.worktree",
                 f"{WORKTREE}/.git"):
        assert visible(plan, path) is Access.READ_ONLY, path


def test_a_reviewer_sees_the_whole_repository_read_only():
    plan = leg_mount_plan(facts(writable=False))

    for path in (f"{GIT.common_dir}/config", f"{GIT.common_dir}/packed-refs", f"{GIT.worktree_dir}/HEAD"):
        assert visible(plan, path) is Access.READ_ONLY, path


def test_a_reviewer_reads_its_checkout_and_holds_no_credential():
    plan = leg_mount_plan(facts(writable=False))

    assert visible(plan, f"{WORKTREE}/src/app.py") is Access.READ_ONLY
    assert visible(plan, f"{GIT.common_dir}/objects/ab/cdef") is Access.READ_ONLY
    assert visible(plan, f"{GIT.worktree_dir}/index") is Access.READ_ONLY
    assert not plan.publishes
    unset = {name for name, value in plan.env if value is None}
    assert {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"} <= unset


def test_home_is_private_so_operator_credentials_are_absent():
    plan = leg_mount_plan(facts())

    assert access(plan, HOME) is Access.PRIVATE
    for secret in ("/home/op/.config/gh/hosts.yml", "/home/op/.ssh/id_ed25519", f"{HARNESS}/config/shared.env",
                   f"{STATE}/control.db", "/home/op/.claude.json", "/home/op/.claude/settings.json",
                   "/home/op/Repos/target/config/shared.env", "/home/op/Repos/other/README.md"):
        assert visible(plan, secret) is Access.PRIVATE or visible(plan, secret) is None, secret
    # Only the runtime's own session store and login, read-write.
    assert visible(plan, "/home/op/.claude/projects/x/s.jsonl") is Access.READ_WRITE
    assert visible(plan, "/home/op/.claude/.credentials.json") is Access.READ_WRITE


def test_toolchains_are_read_only_and_download_caches_are_thrown_away():
    plan = leg_mount_plan(facts())

    assert visible(plan, "/home/op/.cargo/bin/cargo") is Access.READ_ONLY
    assert visible(plan, "/home/op/.rustup/toolchains/x/bin/rustc") is Access.READ_ONLY
    assert visible(plan, "/home/op/.local/share/uv/python/cpython/bin/python3") is Access.READ_ONLY
    assert visible(plan, "/home/op/.cargo/registry/src/crate/lib.rs") is Access.THROWAWAY
    assert visible(plan, "/home/op/.cache/uv/wheels/x.whl") is Access.THROWAWAY
    assert visible(plan, "/home/op/.cargo/config.toml") is Access.READ_ONLY  # linker and rustflags
    assert visible(plan, "/home/op/.cargo/credentials.toml") is Access.PRIVATE
    assert visible(plan, "/usr/lib/libc.so") is Access.READ_ONLY
    assert visible(plan, "/tmp/scratch") is Access.PRIVATE
    assert visible(plan, "/run/user/1000/bus") is None


def test_a_leg_gets_gpu_render_nodes_and_its_own_x_socket_directory_but_no_host_display():
    plan = leg_mount_plan(facts(devices=("/dev/dri/renderD128", "/dev/dri/card1", "/dev/dri", "/dev/mem",
                                         "/dev/dri/renderD128/../card1")))

    assert visible(plan, "/dev/dri/renderD128") is Access.DEVICE
    assert {m.path for m in plan.mounts if m.access is Access.DEVICE} == {"/dev/dri/renderD128"}
    assert access(plan, "/tmp/.X11-unix") is Access.PRIVATE
    assert visible(plan, "/tmp/.X11-unix/X0") is Access.PRIVATE  # never the host's socket
    assert access(plan, "/dev/shm") is Access.PRIVATE  # its own tmpfs, as on a host


def test_parents_are_mounted_before_what_they_contain():
    plan = leg_mount_plan(facts())
    paths = [m.path for m in plan.mounts]

    for index, path in enumerate(paths):
        for later in paths[index + 1:]:
            assert not (path.startswith(later.rstrip("/") + "/")), f"{later} would cover {path}"


def test_a_path_entry_inside_the_checkout_does_not_narrow_it():
    plan = leg_mount_plan(facts(search_path=(f"{WORKTREE}/node_modules/.bin",)))

    assert visible(plan, f"{WORKTREE}/node_modules/.bin/vitest") is Access.READ_WRITE


@pytest.mark.parametrize("entry", ["/", "/home", HOME, "/home/op/.local", "/home/op/Repos", HARNESS,
                                   f"{HARNESS}/..", "/home/op/.config/x/..", "relative/bin", "", ".", "bad\0"])
def test_a_path_entry_that_would_expose_a_protected_path_is_not_mounted(entry):
    plan = leg_mount_plan(facts(search_path=(entry,)))

    for secret in (f"{HOME}/.ssh/id_rsa", f"{HARNESS}/config/shared.env", f"{STATE}/control.db"):
        assert visible(plan, secret) in (Access.PRIVATE, None)


@pytest.mark.parametrize("override", [
    {"cwd": HOME}, {"cwd": "/"}, {"cwd": "/home"}, {"cwd": HARNESS}, {"runtime_state": (STATE,)},
    {"scratch": ("/home/op/.local",)}, {"build_cache": ("/home/op/Repos",)}, {"runtime_binary": "/home"},
])
def test_a_required_mount_that_would_expose_a_protected_path_refuses_the_leg(override):
    with pytest.raises(UnsafeMount):
        leg_mount_plan(facts(**override))


@pytest.mark.parametrize("override", [{"cwd": "relative"}, {"cwd": ""}, {"home": "~"},
                                      {"runtime_state": ("x/y",)}, {"cwd": "/a\0b"}])
def test_relative_or_malformed_paths_are_refused(override):
    with pytest.raises(UnsafeMount):
        leg_mount_plan(facts(**override))


def test_a_mount_strictly_inside_a_protected_dir_is_allowed():
    triage = f"{STATE}/triage"
    plan = leg_mount_plan(facts(cwd=triage, git=None, writable=False,
                                runtime_state=(f"{STATE}/codex-hooks/triage-abc",)))

    assert visible(plan, f"{triage}/x") is Access.READ_ONLY
    assert visible(plan, f"{STATE}/codex-hooks/triage-abc/sessions/r.jsonl") is Access.READ_WRITE
    assert visible(plan, f"{STATE}/control.db") is Access.PRIVATE  # the empty private HOME


def test_a_huge_path_list_still_plans_each_entry_once():
    entries = tuple(f"/opt/tool{i}/bin" for i in range(5_000)) * 2
    plan = leg_mount_plan(facts(search_path=entries))

    paths = [m.path for m in plan.mounts]
    assert len(paths) == len(set(paths))
    # /opt is mounted read-only, so entries under it add nothing.
    assert not any(p.startswith("/opt/tool") for p in paths)


def test_claude_session_store_names_the_cli_project_directory():
    assert claude_session_store("/home/op/.claude", "/home/op/Repos/x/.claude/worktrees/issue-240") == (
        "/home/op/.claude/projects/-home-op-Repos-x--claude-worktrees-issue-240")


def test_claude_session_store_refuses_a_path_whose_name_the_cli_would_hash():
    with pytest.raises(UnsafeMount):
        claude_session_store("/home/op/.claude", "/" + "a" * 200)
    assert claude_session_store("/c", "/" + "a" * 199).endswith("-" + "a" * 199)


def test_runtime_helpers_beside_the_binary_are_read_only_unless_they_expose_a_protected_path():
    plan = leg_mount_plan(facts(runtime_binary="/opt2/codex/bin/codex", runtime_support=("/opt2/codex/bin", HOME)))

    assert visible(plan, "/opt2/codex/bin/codex-code-mode-host") is Access.READ_ONLY
    assert access(plan, HOME) is Access.PRIVATE
