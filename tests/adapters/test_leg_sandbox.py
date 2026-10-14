"""The leg sandbox, run for real under bubblewrap (#413).

A leg-shaped command runs through `LegSandbox.launch` against planted fake
credentials in a temporary HOME, harness checkout and target repository. The
tests skip, naming the reason, on a host where bwrap cannot create a sandbox
(GitHub's hosted runners restrict unprivileged user namespaces).
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from issue_agent.adapters.leg_sandbox import (
    LegSandbox,
    SandboxRefused,
    claude_runtime_state,
    codex_runtime_state,
    git_link,
)
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.adapters.worktree import _subprocess_git
from issue_agent.domain.contracts import RunId

_UNAVAILABLE = LegSandbox(home="/nonexistent", repository="/nonexistent", writable=False).preflight()
needs_bwrap = pytest.mark.skipif(_UNAVAILABLE is not None, reason=f"no usable bubblewrap: {_UNAVAILABLE}")

FAKE_TOKEN = "fake-fix-token-for-tests"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


@pytest.fixture
def host(tmp_path: Path) -> dict[str, Path]:
    """A fake operator: HOME with planted credentials, a harness checkout with
    config/shared.env, and a target repository with one issue worktree."""
    home, harness, target = tmp_path / "home", tmp_path / "harness", tmp_path / "target"
    for path, text in ((home / ".config/gh/hosts.yml", "github.com:\n  oauth_token: planted-gh\n"),
                       (home / ".ssh/id_ed25519", "planted-ssh-key\n"),
                       (home / ".claude/settings.json", "{}\n"),
                       (harness / "config/shared.env", "REVIEW_BOT_TOKEN=planted-review\n"),
                       (target / "config/shared.env", "PLANTED=target-config\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git("init", "-q", "-b", "dev", cwd=target)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=target)
    worktree = target / ".claude/worktrees/issue-5"
    _git("worktree", "add", "-q", "-b", "fix/5-x", str(worktree), cwd=target)
    return {"home": home, "harness": harness, "target": target, "worktree": worktree, "tmp": tmp_path}


def sandbox(host: dict[str, Path], *, writable: bool = True, credential: str | None = FAKE_TOKEN) -> LegSandbox:
    return LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=writable,
                      credential=credential, protected=(str(host["harness"]),))


def run_leg(box: LegSandbox, host: dict[str, Path], script: str, **launch) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(host["home"]), "LANG": "C.UTF-8"}
    leg = box.launch(["/bin/sh", "-c", script], cwd=str(host["worktree"]), env=env, **launch)
    return subprocess.run(leg.argv, env={**env, **leg.env}, capture_output=True, text=True, timeout=60)


@needs_bwrap
def test_a_leg_cannot_read_planted_operator_and_harness_credentials(host):
    secrets = [host["home"] / ".config/gh/hosts.yml", host["home"] / ".ssh/id_ed25519",
               host["harness"] / "config/shared.env", host["target"] / "config/shared.env",
               host["home"] / ".claude/settings.json"]
    script = "".join(f'cat "{s}" 2>/dev/null && echo "READ {s}"; ' for s in secrets) + "echo done"

    done = run_leg(sandbox(host), host, script)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "done"
    assert "planted" not in done.stdout


@needs_bwrap
def test_a_leg_reads_and_writes_its_checkout_but_not_the_root(host):
    done = run_leg(sandbox(host), host, 'echo hello > note.txt && cat note.txt; touch /escape 2>/dev/null || echo ro-root')

    assert done.stdout.split() == ["hello", "ro-root"]
    assert (host["worktree"] / "note.txt").read_text() == "hello\n"


@needs_bwrap
def test_a_reviewer_reads_its_checkout_and_writes_nothing(host):
    (host["worktree"] / "code.py").write_text("x = 1\n")

    done = run_leg(sandbox(host, writable=False), host,
                   'cat code.py; echo y > code.py 2>/dev/null || echo read-only; echo "token=${GH_TOKEN:-none}"')

    assert done.stdout.split() == ["x", "=", "1", "read-only", "token=none"]


@needs_bwrap
def test_a_fix_leg_commits_and_git_answers_github_with_its_one_token(host):
    script = ('echo a > a && git add a && git -c user.name=t -c user.email=t@t commit -qm a && echo committed; '
              'printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill | grep "^password=" ; '
              'echo x >> .git 2>/dev/null || echo link-ro; '
              'echo x >> "$(git rev-parse --git-dir)/commondir" 2>/dev/null || echo own-links-ro; '
              'echo "[core] hooksPath = /tmp" >> "$(git rev-parse --git-dir)/config.worktree" 2>/dev/null '
              '|| echo worktree-config-ro; '
              f'printf "[core]\\n\\tfsmonitor = touch /tmp/pwned\\n" >> "{host["target"]}/.git/config"; '
              f'echo "touch /tmp/pwned" > "{host["target"]}/.git/hooks/post-checkout"; '
              f'echo /elsewhere > "{host["target"]}/.git/commondir"')
    hooks = sorted(p.name for p in (host["target"] / ".git/hooks").iterdir())

    done = run_leg(sandbox(host), host, script)

    assert done.stdout.split() == ["committed", f"password={FAKE_TOKEN}", "link-ro", "own-links-ro",
                                   "worktree-config-ro"], done.stderr
    assert "packed-refs" not in done.stderr  # git writes its lock beside config on every commit
    assert _git("log", "-1", "--format=%s", "fix/5-x", cwd=host["target"]).strip() == "a"
    # The leg's writes to what the harness's git reads outside vanished with it.
    assert "fsmonitor" not in (host["target"] / ".git/config").read_text()
    assert sorted(p.name for p in (host["target"] / ".git/hooks").iterdir()) == hooks
    assert not (host["target"] / ".git/commondir").exists()
    assert _git("rev-parse", "HEAD", cwd=host["worktree"]).strip()


@needs_bwrap
def test_git_in_a_leg_never_starts_maintenance_on_the_object_store_other_checkouts_share(host):
    commit = "echo {0} > {0} && git add {0} && GIT_TRACE=1 git -c user.name=t -c user.email=t@t commit -qm {0}"

    done = run_leg(sandbox(host), host, commit.format("inside"))
    outside = subprocess.run(["/bin/sh", "-c", commit.format("outside")], cwd=host["worktree"],
                             capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert "maintenance run" not in done.stderr
    assert "maintenance run" in outside.stderr  # the control: git's default starts it after a commit


@needs_bwrap
def test_a_scratch_worktree_works_inside_a_fix_leg_and_leaves_no_trace(host):
    before = _git("worktree", "list", "--porcelain", cwd=host["target"])

    done = run_leg(sandbox(host), host, 'git worktree add -q --detach /tmp/base HEAD && git -C /tmp/base log -1 --format=%s')

    assert done.stdout.split() == ["init"], done.stderr
    assert _git("worktree", "list", "--porcelain", cwd=host["target"]) == before


@needs_bwrap
def test_dev_shm_is_its_own_mount_inside_a_leg(host):
    # tauri's native-directory and recovery tests require it (probe, 2026-10-14).
    done = run_leg(sandbox(host), host, 'stat -c %m /dev/shm; touch /dev/shm/x && echo writable')

    assert done.stdout.split() == ["/dev/shm", "writable"], done.stderr


_XVFB = shutil.which("Xvfb")
_XVFB_RUN = shutil.which("xvfb-run")
_XPROP = shutil.which("xprop")


@needs_bwrap
@pytest.mark.skipif(not (_XVFB and _XVFB_RUN and _XPROP), reason="no Xvfb, xvfb-run and xprop on this host")
def test_two_legs_each_start_their_own_virtual_display(host):
    # Tauri's native E2E runs under xvfb-run. Xvfb aborted in the sandbox
    # without a GPU render node (NVIDIA's EGL vendor library) or a
    # /tmp/.X11-unix to listen in; legs share the abstract socket namespace,
    # so `-d` is what keeps two legs' displays apart.
    first = "xvfb-run -d -s '-screen 0 320x240x24' sh -c 'xprop -root -f LEG 8s -set LEG first && echo $DISPLAY && sleep 6'"
    second = "sleep 2; xvfb-run -d -s '-screen 0 320x240x24' sh -c 'echo $DISPLAY; xprop -root LEG'"
    box = sandbox(host)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(host["home"]), "LANG": "C.UTF-8"}
    legs = [box.launch(["/bin/sh", "-c", script], cwd=str(host["worktree"]), env=env) for script in (first, second)]
    running = [subprocess.Popen(leg.argv, env={**env, **leg.env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True) for leg in legs]
    (one, err_one), (two, err_two) = (p.communicate(timeout=60) for p in running)

    assert running[0].returncode == 0, err_one
    assert running[1].returncode == 0, err_two
    display_one, display_two = one.split()[0], two.split()[0]
    assert display_one.startswith(":") and display_two.startswith(":") and display_one != display_two
    assert "first" not in two  # the second leg's display is its own


@needs_bwrap
def test_a_leg_without_the_credential_holds_no_token_even_if_its_env_had_one(host):
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(host["home"]), "GH_TOKEN": "ambient"}
    leg = sandbox(host, writable=False).launch(["/bin/sh", "-c", 'echo "${GH_TOKEN:-none}"'],
                                               cwd=str(host["worktree"]), env=env)

    done = subprocess.run(leg.argv, env={**env, **leg.env}, capture_output=True, text=True, timeout=60)

    assert done.stdout.strip() == "none"
    assert "ambient" not in " ".join(leg.argv) and FAKE_TOKEN not in " ".join(leg.argv)


@needs_bwrap
def test_the_token_never_appears_in_the_sandbox_argv(host):
    leg = sandbox(host).launch(["/bin/true"], cwd=str(host["worktree"]), env={"PATH": "/usr/bin:/bin"})

    assert FAKE_TOKEN not in " ".join(leg.argv)
    assert FAKE_TOKEN not in leg.summary and FAKE_TOKEN not in repr(sandbox(host))
    assert leg.env["GH_TOKEN"] == FAKE_TOKEN


@needs_bwrap
def test_a_leg_cannot_see_host_processes_or_their_environment(host, monkeypatch):
    marker = f"host-secret-{uuid.uuid4().hex}"
    monkeypatch.setenv("SANDBOX_TEST_MARKER", marker)

    done = run_leg(sandbox(host), host, f'cat /proc/[0-9]*/environ 2>/dev/null | tr "\\0" "\\n" | grep -c {marker}; '
                                        'ls /proc | grep -c "^[0-9]"')

    found, processes = done.stdout.split()
    assert found == "0"
    assert int(processes) < 10


def _network_reachable() -> bool:
    try:
        socket.create_connection(("api.github.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


# A system interpreter: the test's own venv lives in the harness checkout,
# which no leg sees.
_SYSTEM_PYTHON = shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin")


@needs_bwrap
@pytest.mark.skipif(not _network_reachable(), reason="this host has no route to api.github.com")
@pytest.mark.skipif(_SYSTEM_PYTHON is None, reason="no system python3 to probe with")
def test_a_leg_reaches_the_network(host):
    probe = (f'"{_SYSTEM_PYTHON}" -c "import socket; socket.create_connection((\'api.github.com\', 443), '
             'timeout=10).close(); print(\'reached\')"')

    done = run_leg(sandbox(host), host, probe)

    assert done.stdout.strip() == "reached", done.stderr


@needs_bwrap
def test_tearing_down_the_registered_group_kills_every_sandboxed_process(host):
    marker = f"sandboxed-sleeper-{uuid.uuid4().hex}"
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(host["home"])}
    leg = sandbox(host).launch(["/bin/sh", "-c", f'sleep 300 & sleep 301 & echo {marker} > started; wait'],
                               cwd=str(host["worktree"]), env=env)
    supervisor = ProcessSupervisor(str(host["tmp"] / "supervision"), grace_seconds=0.5)
    run_id = RunId("sandboxed-leg")
    process = subprocess.Popen(supervisor.supervised_command(run_id, leg.argv), env={**env, **leg.env},
                               start_new_session=True)
    deadline = time.monotonic() + 20
    while not (host["worktree"] / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (host["worktree"] / "started").exists()
    assert len(_sleepers(host["worktree"])) >= 2  # the probe sees the sandboxed processes

    supervisor.kill_tree(run_id)
    process.wait(timeout=10)

    deadline = time.monotonic() + 5
    while _sleepers(host["worktree"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _sleepers(host["worktree"]) == []
    assert process.returncode in (-signal.SIGTERM, 128 + signal.SIGTERM, -signal.SIGKILL, 128 + signal.SIGKILL)


@needs_bwrap
def test_a_launcher_killed_outright_takes_the_sandbox_with_it(host):
    # A daemon crash or boot-time orphan reap SIGKILLs the registered leader:
    # --die-with-parent and the PID namespace leave no sandboxed orphan.
    env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(host["home"])}
    leg = sandbox(host).launch(["/bin/sh", "-c", "sleep 300 & echo up > started; wait"],
                               cwd=str(host["worktree"]), env=env)
    supervisor = ProcessSupervisor(str(host["tmp"] / "supervision"), grace_seconds=0.5)
    process = subprocess.Popen(supervisor.supervised_command(RunId("killed-leader"), leg.argv),
                               env={**env, **leg.env}, start_new_session=True)
    deadline = time.monotonic() + 20
    while not (host["worktree"] / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _sleepers(host["worktree"])

    process.kill()
    process.wait(timeout=10)

    deadline = time.monotonic() + 5
    while _sleepers(host["worktree"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _sleepers(host["worktree"]) == []


def _sleepers(cwd: Path) -> list[int]:
    """Host processes still running in the leg's checkout."""
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                if os.readlink(f"/proc/{entry}/cwd") == str(cwd):
                    found.append(int(entry))
            except OSError:
                continue
    return found


@needs_bwrap
def test_runtime_state_is_shared_with_the_host_at_the_same_path(host):
    env = {"HOME": str(host["home"]), "CLAUDE_CODE_OAUTH_TOKEN": "t",
           "CODEX_HOME": str(host["tmp"] / "state/codex-hooks/k")}
    os.makedirs(env["CODEX_HOME"])
    (host["home"] / ".codex").mkdir()
    (host["home"] / ".codex/auth.json").write_text("{}")
    os.symlink(host["home"] / ".codex/auth.json", Path(env["CODEX_HOME"]) / "auth.json")
    claude = claude_runtime_state(env, str(host["worktree"]))
    codex = codex_runtime_state(env, str(host["worktree"]))
    store = claude[0]

    done = run_leg(sandbox(host), host, f'echo s > "{store}/s.jsonl"; cat "{env["CODEX_HOME"]}/auth.json"; '
                                        f'echo r > "{env["CODEX_HOME"]}/rollout"',
                   runtime_state=(*claude, *codex))

    assert done.stdout.strip() == "{}"
    assert Path(store, "s.jsonl").read_text() == "s\n"
    assert (Path(env["CODEX_HOME"]) / "rollout").read_text() == "r\n"


def _fix_leg(host: dict[str, Path], script: str) -> subprocess.Popen:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(host["home"]), "LANG": "C.UTF-8",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    leg = sandbox(host).launch(["/bin/sh", "-c", script], cwd=str(host["worktree"]), env=env)
    return subprocess.Popen(leg.argv, env={**env, **leg.env}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


@needs_bwrap
def test_a_host_ref_update_reaches_a_running_fix_leg(host):
    leg = _fix_leg(host, 'git rev-parse -q --verify refs/heads/other || echo none; read _; '
                         'git rev-parse refs/heads/other; git rev-parse HEAD')
    try:
        assert leg.stdout.readline().strip() == "none"
        _git("commit", "-q", "--allow-empty", "-m", "host", cwd=host["worktree"])
        _git("update-ref", "refs/heads/other", "HEAD", cwd=host["target"])
        out, err = leg.communicate("go\n", timeout=30)
    finally:
        leg.kill()
    assert out.split() == [_git("rev-parse", "dev", cwd=host["target"]).strip(),
                           _git("rev-parse", "fix/5-x", cwd=host["target"]).strip()], err


@needs_bwrap
def test_no_host_fetch_packs_a_running_legs_branch_away(host):
    upstream = host["tmp"] / "upstream.git"
    _git("init", "-q", "--bare", "-b", "dev", str(upstream), cwd=host["tmp"])
    _git("remote", "add", "origin", str(upstream), cwd=host["target"])
    _git("push", "-q", "origin", "dev", cwd=host["target"])
    for index in range(20):  # past the 16 loose refs at which auto-maintenance packs
        _git("branch", f"b{index}", cwd=host["target"])
    leg = _fix_leg(host, 'read _; git rev-parse --abbrev-ref HEAD; git log -1 --format=%s')
    try:
        for fetch in (lambda: _subprocess_git(["-c", "maintenance.autoDetach=false", "fetch", "origin", "dev"],
                                              str(host["target"])).code,  # the harness's
                      lambda: len(_git("-c", "maintenance.autoDetach=false", "fetch", "-q", "origin", "dev",
                                       cwd=host["target"])) * 0):  # the operator's
            _git("commit", "-q", "--allow-empty", "-m", "upstream", cwd=host["target"])
            _git("push", "-q", "origin", "HEAD:dev", cwd=host["target"])
            _git("reset", "-q", "--hard", "HEAD~1", cwd=host["target"])
            assert fetch() == 0
        out, err = leg.communicate("go\n", timeout=30)
    finally:
        leg.kill()
    assert out.split() == ["fix/5-x", "init"], err
    assert (host["target"] / ".git/refs/heads/fix/5-x").is_file()


@needs_bwrap
def test_git_works_quietly_in_a_fix_leg_and_its_pack_refs_cannot_prune_the_hosts_refs(host):
    refs = _git("show-ref", cwd=host["target"])
    script = ('set -e; echo a > a; git add a; git commit -qm a; echo b > a; git stash -q; git stash pop -q; '
              'git commit -qam b; git branch scratch; git branch -qD scratch; git tag t; git tag -d t >/dev/null; '
              'git rebase -q HEAD~1; git fetch -q "$(git rev-parse --git-common-dir)" dev:refs/remotes/o/dev; '
              'git worktree add -q /tmp/side HEAD; git -C /tmp/side log -1 --format=%s; set +e; '
              'git pack-refs --all 2>/dev/null || echo pack-refused; echo done')

    out, err = _fix_leg(host, script).communicate(timeout=60)

    assert out.split() == ["b", "pack-refused", "done"], err
    assert "error" not in err.lower() and "fatal" not in err.lower(), err
    after = _git("show-ref", cwd=host["target"])
    assert _git("log", "-1", "--format=%s", "fix/5-x", cwd=host["target"]).strip() == "b"
    # Every ref the host had is still there, loose; the leg's fetch reached the host.
    assert set(refs.split("\n")[:-1]) - set(after.split("\n")) <= {
        line for line in refs.split("\n") if line.endswith("refs/heads/fix/5-x")}
    assert "refs/remotes/o/dev" in after
    assert (host["target"] / ".git/refs/heads/dev").is_file()
    assert not (host["target"] / ".git/worktrees/side").exists()


@needs_bwrap
def test_a_fix_leg_cannot_plant_what_the_harnesss_git_would_obey_in_the_repository(host):
    common = host["target"] / ".git"
    script = (f'echo /elsewhere > "{common}/commondir"; '
              f'printf "[core]\\n\\thooksPath = /tmp\\n" > "{common}/config.worktree"; '
              f'mkdir -p "{common}/modules/x" "{common}/remotes" && echo evil > "{common}/modules/x/config"; '
              f'echo "URL: /evil" > "{common}/remotes/origin"; echo evil > "{common}/hooks/pre-commit"; '
              f'echo evil >> "{common}/HEAD"; echo "[x]" >> "{common}/config"; echo done')
    _fix_leg(host, "true").communicate(timeout=30)  # a launch's own preconditions (packed-refs, loose refs)
    before = {p.name: p.read_bytes() for p in common.iterdir() if p.is_file()}

    out, err = _fix_leg(host, script).communicate(timeout=30)

    assert out.split() == ["done"], err
    assert {p.name: p.read_bytes() for p in common.iterdir() if p.is_file()} == before
    assert not {"commondir", "config.worktree", "modules", "remotes"} & {p.name for p in common.iterdir()}
    assert not (common / "hooks/pre-commit").exists()


@needs_bwrap
def test_a_fix_leg_never_prunes_on_fetch(host):
    _git("config", "fetch.prune", "true", cwd=host["target"])
    _git("config", "remote.origin.prune", "true", cwd=host["target"])

    out, err = _fix_leg(host, "git config fetch.prune; git config remote.origin.prune").communicate(timeout=30)

    assert out.split() == ["false", "false"], err


@needs_bwrap
def test_a_repository_whose_include_would_pack_refs_again_refuses_the_fix_leg(host):
    (host["tmp"] / "packs.gitconfig").write_text("[gc]\n\tpackRefs = true\n")
    _fix_leg(host, "true").communicate(timeout=30)  # the launch writes the keys first
    with open(host["target"] / ".git/config", "a") as config:
        config.write(f"[include]\n\tpath = {host['tmp'] / 'packs.gitconfig'}\n")

    with pytest.raises(SandboxRefused, match="gc.packRefs"):
        _fix_leg(host, "true")


def test_a_checkout_whose_git_link_points_elsewhere_is_refused(host):
    evil = host["worktree"] / "evil"
    (evil / "worktrees").mkdir(parents=True)
    (host["worktree"] / ".git").write_text(f"gitdir: {evil}\n")

    with pytest.raises(SandboxRefused):
        git_link(str(host["worktree"]), str(host["target"]))


def test_a_checkout_link_into_another_worktree_is_refused(host):
    other = host["target"] / ".claude/worktrees/issue-6"
    _git("worktree", "add", "-q", "-b", "fix/6-y", str(other), cwd=host["target"])
    (host["worktree"] / ".git").write_text((other / ".git").read_text())

    with pytest.raises(SandboxRefused):
        git_link(str(host["worktree"]), str(host["target"]))


def test_a_valid_worktree_resolves_to_its_own_git_metadata(host):
    link = git_link(str(host["worktree"]), str(host["target"]))

    assert link is not None
    assert link.common_dir == os.path.realpath(host["target"] / ".git")
    assert Path(link.worktree_dir).parent == Path(link.common_dir) / "worktrees"
    assert git_link(str(host["tmp"]), str(host["target"])) is None


def test_a_missing_bwrap_is_named_and_refuses_every_launch(host):
    box = LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=True,
                     bwrap="definitely-not-bwrap")

    assert "not installed" in (box.preflight() or "")
    assert box.posture().startswith("unavailable:")
    with pytest.raises(SandboxRefused):
        box.launch(["/bin/true"], cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})


@needs_bwrap
def test_worktree_config_a_leg_planted_before_the_mount_is_emptied_at_launch(host):
    admin = Path(git_link(str(host["worktree"]), str(host["target"])).worktree_dir)
    (admin / "config.worktree").write_text("[core]\n\tfsmonitor = touch /tmp/pwned\n")

    done = run_leg(sandbox(host), host, "echo ran")

    assert done.stdout.split() == ["ran"], done.stderr
    assert (admin / "config.worktree").read_text() == ""


@needs_bwrap
@pytest.mark.parametrize("plant", ["fifo", "hardlink"])
def test_a_worktree_config_that_is_not_a_plain_file_refuses_the_leg(host, plant):
    admin = Path(git_link(str(host["worktree"]), str(host["target"])).worktree_dir)
    if plant == "fifo":
        os.mkfifo(admin / "config.worktree")
    else:
        (host["tmp"] / "shared").write_text("")
        os.link(host["tmp"] / "shared", admin / "config.worktree")

    with pytest.raises(SandboxRefused, match="config.worktree"):
        sandbox(host).launch(["/bin/true"], cwd=str(host["worktree"]), env={"PATH": "/usr/bin:/bin"})


@needs_bwrap
def test_a_linked_worktree_config_is_tampering_and_refuses_the_leg(host):
    admin = Path(git_link(str(host["worktree"]), str(host["target"])).worktree_dir)
    (admin / "config.worktree").symlink_to(host["tmp"] / "elsewhere")

    with pytest.raises(SandboxRefused, match="config.worktree"):
        sandbox(host).launch(["/bin/true"], cwd=str(host["worktree"]), env={"PATH": "/usr/bin:/bin"})
    assert not (host["tmp"] / "elsewhere").exists()


def test_a_bwrap_too_old_for_overlays_is_named_and_refuses_every_launch(host):
    def old(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "bwrap: Unknown option --overlay-src\n")

    box = LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=True,
                     which=lambda name, **_: f"/usr/bin/{name}", run=old)

    assert "0.10 or newer" in (box.preflight() or "")
    with pytest.raises(SandboxRefused):
        box.launch(["/bin/true"], cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})


def test_a_bwrap_that_cannot_create_namespaces_refuses_every_launch(host):
    def denied(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "bwrap: setting up uid map: Permission denied\n")

    box = LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=True,
                     which=lambda name, **_: f"/usr/bin/{name}", run=denied)

    assert "uid map" in (box.preflight() or "")
    with pytest.raises(SandboxRefused):
        box.launch(["/bin/true"], cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})


def _plant_claude_config(host: dict[str, Path]) -> Path:
    """A fake operator ~/.claude: a login, settings with a hook, history,
    another project's transcript. No real credential."""
    config = host["home"] / ".claude"
    for name, text in ((".credentials.json", '{"fake": "v1"}'), ("history.jsonl", "planted-history\n"),
                       ("projects/-home-op-other/s.jsonl", "planted-transcript\n"),
                       ("hooks/pre.sh", "planted-hook\n"), ("CLAUDE.md", "planted-memory\n"),
                       ("settings.json", '{"hooks": "planted-settings"}')):
        (config / name).parent.mkdir(parents=True, exist_ok=True)
        (config / name).write_text(text)
    return config


@pytest.mark.parametrize("env_extra", [{}, {"ANTHROPIC_API_KEY": ""}, {"CLAUDE_CODE_OAUTH_TOKEN": ""}],
                         ids=["absent", "blank-key", "blank-token"])
def test_a_claude_leg_without_its_own_credential_is_refused_naming_the_fix_even_with_an_operator_login(host, env_extra):
    _plant_claude_config(host)

    with pytest.raises(SandboxRefused, match=r"claude setup-token.*credstore\.encrypted/CLAUDE_CODE_OAUTH_TOKEN"):
        claude_runtime_state({"HOME": str(host["home"]), **env_extra}, str(host["worktree"]))


@needs_bwrap
@pytest.mark.parametrize("name", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_a_claude_leg_with_its_own_credential_sees_only_its_session_store_of_the_operators_claude_dir(host, name):
    config = _plant_claude_config(host)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(host["home"]), "LANG": "C.UTF-8",
           name: "leg-login"}
    store = claude_runtime_state(env, str(host["worktree"]))
    script = 'find "$HOME/.claude" | sort; echo "gh=${GH_TOKEN:-none}${GITHUB_TOKEN:-}"; echo "cred=$' + name + '"'
    leg = sandbox(host, writable=False).launch(["/bin/sh", "-c", script], cwd=str(host["worktree"]), env=env,
                                               runtime_state=store)

    out = subprocess.run(leg.argv, env={**env, **leg.env}, capture_output=True, text=True, timeout=60).stdout

    assert out.split() == [f"{config}", f"{config}/projects", store[0], "gh=none", "cred=leg-login"]
    assert (config / ".credentials.json").read_text() == '{"fake": "v1"}'
