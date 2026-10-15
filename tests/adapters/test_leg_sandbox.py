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
import stat
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
from issue_agent.adapters.worktree import Worktrees, _subprocess_git
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
    config/shared.env, and a target repository (origin a bare repository
    reached as GitHub is, over a transport) with one issue's fix clone."""
    home, harness, target = tmp_path / "home", tmp_path / "harness", tmp_path / "target"
    for path, text in ((home / ".config/gh/hosts.yml", "github.com:\n  oauth_token: planted-gh\n"),
                       (home / ".ssh/id_ed25519", "planted-ssh-key\n"),
                       (home / ".claude/settings.json", "{}\n"),
                       (harness / "config/shared.env", "REVIEW_BOT_TOKEN=planted-review\n"),
                       (target / "config/shared.env", "PLANTED=target-config\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    upstream = tmp_path / "upstream.git"
    _git("init", "-q", "--bare", "-b", "dev", str(upstream), cwd=tmp_path)
    _git("init", "-q", "-b", "dev", cwd=target)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=target)
    _git("remote", "add", "origin", f"file://{upstream}", cwd=target)
    _git("push", "-q", "origin", "dev", cwd=target)
    worktree = Path(Worktrees(str(target)).ensure(5, "fix/5-x"))
    return {"home": home, "harness": harness, "target": target, "worktree": worktree, "tmp": tmp_path,
            "upstream": upstream}


def sandbox(host: dict[str, Path], *, writable: bool = True, credential: str | None = FAKE_TOKEN) -> LegSandbox:
    return LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=writable,
                      credential=credential, protected=(str(host["harness"]),))


def leg_tmp(host: dict[str, Path]) -> str:
    """A fresh path for one leg's temp directories, as the supervisor names them."""
    return str(host["tmp"] / "state" / "leg-tmp" / uuid.uuid4().hex)


def run_leg(box: LegSandbox, host: dict[str, Path], script: str, **launch) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(host["home"]), "LANG": "C.UTF-8"}
    launch.setdefault("tmp", leg_tmp(host))
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
              'git init -q --bare /tmp/remote.git && git push -q -u /tmp/remote.git fix/5-x && '
              'git -C /tmp/remote.git log -1 --format=%s fix/5-x')

    done = run_leg(sandbox(host), host, script)

    # `push -u` publishes and succeeds although the clone's config is read-only.
    assert done.stdout.split() == ["committed", f"password={FAKE_TOKEN}", "a"], done.stderr
    # The commit and its branch live in the leg's clone, never in the repository.
    assert _git("log", "-1", "--format=%s", "fix/5-x", cwd=host["worktree"]).strip() == "a"
    assert not _git("branch", "--list", "fix/5-x", cwd=host["target"]).strip()


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


@needs_bwrap
def test_a_legs_tmp_and_var_tmp_are_private_directories_on_disk_not_memory(host):
    # #470: Chrome's traces in a tmpfs /tmp were charged to the fix unit's
    # memory and OOM-killed it. The leg's writes land in its directory on disk.
    tmp = Path(leg_tmp(host))

    done = run_leg(sandbox(host), host, 'echo a > /tmp/a && echo b > /var/tmp/b && ls -A /tmp /var/tmp; '
                                        'stat -f -c %T /dev/shm', tmp=str(tmp))

    assert done.returncode == 0, done.stderr
    assert (tmp / "tmp/a").read_text() == "a\n" and (tmp / "var-tmp/b").read_text() == "b\n"
    assert ".X11-unix" in done.stdout  # still the leg's own socket directory
    assert done.stdout.split()[-1] == "tmpfs"  # /dev/shm stays its own tmpfs
    for path in (tmp, tmp / "tmp", tmp / "var-tmp"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


@needs_bwrap
def test_one_leg_never_sees_another_legs_temp_files(host):
    first = Path(leg_tmp(host))
    run_leg(sandbox(host), host, "echo planted-secret > /tmp/s; echo planted-secret > /var/tmp/s", tmp=str(first))

    done = run_leg(sandbox(host), host, f'cat /tmp/s /var/tmp/s "{first}/tmp/s" 2>/dev/null; ls -A /var/tmp; echo done')

    assert done.stdout.split() == ["done"], done.stdout


@needs_bwrap
def test_a_temp_directory_that_already_exists_is_never_handed_to_a_leg(host):
    planted = host["tmp"] / "planted"
    (planted / "tmp").mkdir(parents=True)
    (planted / "tmp/earlier").write_text("an earlier leg's file\n")
    linked = host["tmp"] / "linked"
    linked.symlink_to(host["home"] / ".ssh")

    for tmp in (planted, linked):
        with pytest.raises(SandboxRefused, match="cannot be created fresh"):
            sandbox(host).launch(["/bin/true"], tmp=str(tmp), cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})
    assert (planted / "tmp/earlier").exists()
    assert (host["home"] / ".ssh/id_ed25519").read_text() == "planted-ssh-key\n"


@needs_bwrap
@pytest.mark.parametrize("tmp", ["", "relative/leg-tmp", "/", "bad\0path", "/huge/" + "a" * 5000])
def test_an_absent_or_malformed_temp_path_refuses_the_leg(host, tmp):
    with pytest.raises(SandboxRefused):
        sandbox(host).launch(["/bin/true"], tmp=tmp, cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})


@needs_bwrap
def test_a_temp_path_inside_home_or_the_harness_refuses_the_leg(host):
    for tmp in (host["home"], host["harness"], host["tmp"]):
        with pytest.raises(SandboxRefused, match="would expose"):
            sandbox(host).launch(["/bin/true"], tmp=str(tmp), cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})


@needs_bwrap
def test_a_launch_refused_after_planning_leaves_no_temp_directory(host):
    tmp = Path(leg_tmp(host))

    with pytest.raises(SandboxRefused, match="missing"):
        sandbox(host).launch(["/bin/true"], tmp=str(tmp), cwd=str(host["worktree"]), env={"PATH": "/usr/bin"},
                             runtime_state=(str(host["tmp"] / "absent-session-store"),))
    assert not tmp.exists()


@needs_bwrap
def test_a_supervised_legs_temp_directory_goes_when_the_leg_ends(host):
    supervisor = ProcessSupervisor(str(host["tmp"] / "supervision"))
    run = RunId("leg-1")
    tmp = Path(supervisor.leg_tmp(run))

    outside = host["harness"] / "config"  # a host path the leg names but cannot see
    done = run_leg(sandbox(host), host, f'head -c 1048576 /dev/zero > /tmp/trace && ln -s "{outside}" /tmp/link && '
                                        'echo wrote', tmp=str(tmp))
    assert done.stdout.strip() == "wrote", done.stderr
    supervisor.kill_tree(run)

    assert not tmp.exists()
    assert (outside / "shared.env").exists()  # the removal never follows a leg's link


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
    legs = [box.launch(["/bin/sh", "-c", script], tmp=leg_tmp(host), cwd=str(host["worktree"]), env=env) for script in (first, second)]
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
                                               tmp=leg_tmp(host), cwd=str(host["worktree"]), env=env)

    done = subprocess.run(leg.argv, env={**env, **leg.env}, capture_output=True, text=True, timeout=60)

    assert done.stdout.strip() == "none"
    assert "ambient" not in " ".join(leg.argv) and FAKE_TOKEN not in " ".join(leg.argv)


@needs_bwrap
def test_the_token_never_appears_in_the_sandbox_argv(host):
    leg = sandbox(host).launch(["/bin/true"], tmp=leg_tmp(host), cwd=str(host["worktree"]), env={"PATH": "/usr/bin:/bin"})

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
                               tmp=leg_tmp(host), cwd=str(host["worktree"]), env=env)
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
                               tmp=leg_tmp(host), cwd=str(host["worktree"]), env=env)
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
    leg = sandbox(host).launch(["/bin/sh", "-c", script], tmp=leg_tmp(host), cwd=str(host["worktree"]), env=env)
    return subprocess.Popen(leg.argv, env={**env, **leg.env}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


@needs_bwrap
def test_no_host_ref_pack_or_gc_hides_a_running_legs_branch(host):
    # tauri #1106: the host packed refs while a fix leg ran, and the leg found
    # itself on an unborn branch. A clone's refs are its own.
    for index in range(20):  # past the 16 loose refs at which auto-maintenance packs
        _git("branch", f"b{index}", cwd=host["target"])
    leg = _fix_leg(host, 'read _; git rev-parse --abbrev-ref HEAD; git log -1 --format=%s')
    try:
        _git("pack-refs", "--all", "--prune", cwd=host["target"])
        _git("-c", "maintenance.autoDetach=false", "fetch", "-q", "origin", "dev", cwd=host["target"])
        _git("gc", "-q", "--prune=now", cwd=host["target"])
        out, err = leg.communicate("go\n", timeout=30)
    finally:
        leg.kill()
    assert out.split() == ["fix/5-x", "init"], err


@needs_bwrap
def test_git_works_quietly_in_a_fix_leg_and_never_reaches_the_repositorys_refs(host):
    refs = _git("show-ref", cwd=host["target"])
    script = ('set -e; echo a > a; git add a; git commit -qm a; echo b > a; git stash -q; git stash pop -q; '
              'git commit -qam b; git branch scratch; git tag t; git pack-refs --all; git branch -qD scratch; '
              'git tag -d t >/dev/null; git rebase -q HEAD~1; git worktree add -q /tmp/side HEAD; '
              'git -C /tmp/side log -1 --format=%s; git gc -q; echo done')

    out, err = _fix_leg(host, script).communicate(timeout=60)

    assert out.split() == ["b", "done"], err
    # Only the read-only config speaks up (`branch -D` drops its section), and git carries on.
    assert all("config" in line for line in err.splitlines()), err
    assert _git("show-ref", cwd=host["target"]) == refs
    assert _git("log", "-1", "--format=%s", "fix/5-x", cwd=host["worktree"]).strip() == "b"


@needs_bwrap
def test_a_fix_leg_cannot_plant_what_the_harnesss_git_obeys_in_its_clone(host):
    dot, marker = host["worktree"] / ".git", host["tmp"] / "pwned"
    evil = f'printf "[core]\\n\\tfsmonitor = touch {marker}; echo\\n"'
    script = (f'{evil} >> .git/config 2>/dev/null || echo config-ro; '
              f'printf "#!/bin/sh\\ntouch {marker}\\n" > .git/hooks/reference-transaction 2>/dev/null '
              '|| echo hooks-ro; '
              'echo /tmp > .git/commondir 2>/dev/null || echo commondir-ro; '
              'mv .git .git-old 2>/dev/null || echo dot-git-pinned; '
              f'echo x > "{host["target"]}/.git/objects/planted" 2>/dev/null || echo objects-ro')
    _fix_leg(host, "true").communicate(timeout=30)  # a launch's own precondition: an empty commondir
    before = {p.name: p.read_bytes() for p in dot.iterdir() if p.is_file()}

    out, err = _fix_leg(host, script).communicate(timeout=30)

    assert out.split() == ["config-ro", "hooks-ro", "commondir-ro", "dot-git-pinned", "objects-ro"], err
    assert {p.name: p.read_bytes() for p in dot.iterdir() if p.is_file()} == before
    for harness_git in (["status"], ["reset", "--hard", "HEAD"]):  # what sync and the guidance read run
        assert _subprocess_git(harness_git, str(host["worktree"])).code == 0
    assert not marker.exists()


@needs_bwrap
def test_a_merged_branch_the_repository_pruned_never_breaks_a_fix_legs_fetch(host):
    # A clone borrows the objects of every branch it tracks from the
    # repository. Once a merged branch is deleted and the repository prunes
    # its objects, the clone's ref names objects that exist nowhere it can
    # read; a pruning fetch drops that ref before walking it (probe, 2026-10-15).
    _git("checkout", "-q", "-b", "other", cwd=host["target"])
    _git("commit", "-q", "--allow-empty", "-m", "other", cwd=host["target"])
    _git("push", "-q", "origin", "other", cwd=host["target"])
    _git("checkout", "-q", "dev", cwd=host["target"])
    clone = Worktrees(str(host["target"])).ensure(6, "fix/6-y")
    for step in (["push", "-q", "origin", ":other"], ["branch", "-q", "-D", "other"], ["fetch", "-q", "--prune"],
                 ["reflog", "expire", "--expire=now", "--all"], ["gc", "-q", "--prune=now"]):
        _git(*step, cwd=host["target"])
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(host["home"])}
    leg_env = sandbox(host).launch(["/bin/true"], tmp=leg_tmp(host), cwd=clone, env=env).env

    def fetch(extra: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "fetch", "-q", "origin"], cwd=clone, env={**os.environ, **extra},
                              capture_output=True, text=True)

    assert fetch({}).returncode != 0  # the control: git's default fetch fails on the stale ref
    assert fetch(leg_env).returncode == 0
    assert fetch(leg_env).returncode == 0


def test_a_valid_fix_clone_borrows_the_repositorys_objects(host):
    link = git_link(str(host["worktree"]), str(host["target"]))

    assert link is not None and link.shared == os.path.realpath(host["target"] / ".git/objects")
    assert git_link(str(host["tmp"]), str(host["target"])) is None


@pytest.mark.parametrize("alternates", [None, "fifo", "/elsewhere/objects", "{objects}\n/elsewhere/objects"])
def test_a_clone_that_borrows_anything_but_the_repositorys_objects_is_refused(host, alternates):
    path = host["worktree"] / ".git/objects/info/alternates"
    if alternates is None:
        path.unlink()
    elif alternates == "fifo":  # a read would wait forever and wedge the daemon
        path.unlink()
        os.mkfifo(path)
    else:
        path.write_text(alternates.format(objects=host["target"] / ".git/objects"))

    with pytest.raises(SandboxRefused):
        git_link(str(host["worktree"]), str(host["target"]))


@pytest.fixture
def review(host: dict[str, Path]) -> Path:
    checkout = host["target"] / ".claude/worktrees/review-pr-7"
    _git("worktree", "add", "-q", "--detach", str(checkout), cwd=host["target"])
    return checkout


def test_a_review_worktree_reads_the_repositorys_git_dir(host, review):
    link = git_link(str(review), str(host["target"]))

    assert link is not None and link.shared == os.path.realpath(host["target"] / ".git")


def test_a_checkout_whose_git_link_points_elsewhere_is_refused(host, review):
    evil = review / "evil"
    (evil / "worktrees").mkdir(parents=True)
    (review / ".git").write_text(f"gitdir: {evil}\n")

    with pytest.raises(SandboxRefused):
        git_link(str(review), str(host["target"]))


def test_a_checkout_link_into_another_worktree_is_refused(host, review):
    other = host["target"] / ".claude/worktrees/review-pr-8"
    _git("worktree", "add", "-q", "--detach", str(other), cwd=host["target"])
    (review / ".git").write_text((other / ".git").read_text())

    with pytest.raises(SandboxRefused):
        git_link(str(review), str(host["target"]))


@needs_bwrap
def test_a_fix_leg_never_writes_a_shared_worktree(host, review):
    with pytest.raises(SandboxRefused):
        sandbox(host).launch(["/bin/true"], tmp=leg_tmp(host), cwd=str(review), env={"PATH": "/usr/bin:/bin"})


def test_a_missing_bwrap_is_named_and_refuses_every_launch(host):
    box = LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=True,
                     bwrap="definitely-not-bwrap")

    assert "not installed" in (box.preflight() or "")
    assert box.posture().startswith("unavailable:")
    with pytest.raises(SandboxRefused):
        box.launch(["/bin/true"], tmp=leg_tmp(host), cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})


@needs_bwrap
def test_a_commondir_planted_before_the_mount_is_reset_to_the_clone_at_launch(host):
    commondir = host["worktree"] / ".git/commondir"
    commondir.write_text(str(host["tmp"]))

    done = run_leg(sandbox(host), host, "echo ran")

    assert done.stdout.split() == ["ran"], done.stderr
    common = _subprocess_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], str(host["worktree"]))
    assert Path(common.out.strip()).resolve() == (host["worktree"] / ".git").resolve()


@needs_bwrap
@pytest.mark.parametrize("plant", ["fifo", "hardlink", "symlink"])
def test_a_commondir_that_is_not_a_plain_file_refuses_the_leg(host, plant):
    commondir = host["worktree"] / ".git/commondir"
    if plant == "fifo":
        os.mkfifo(commondir)
    elif plant == "hardlink":
        (host["tmp"] / "shared").write_text("")
        os.link(host["tmp"] / "shared", commondir)
    else:
        commondir.symlink_to(host["tmp"] / "elsewhere")

    with pytest.raises(SandboxRefused, match="commondir"):
        sandbox(host).launch(["/bin/true"], tmp=leg_tmp(host), cwd=str(host["worktree"]), env={"PATH": "/usr/bin:/bin"})
    assert not (host["tmp"] / "elsewhere").exists()


def test_a_bwrap_too_old_for_overlays_is_named_and_refuses_every_launch(host):
    def old(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "bwrap: Unknown option --overlay-src\n")

    box = LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=True,
                     which=lambda name, **_: f"/usr/bin/{name}", run=old)

    assert "0.10 or newer" in (box.preflight() or "")
    with pytest.raises(SandboxRefused):
        box.launch(["/bin/true"], tmp=leg_tmp(host), cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})


def test_a_bwrap_that_cannot_create_namespaces_refuses_every_launch(host):
    def denied(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "bwrap: setting up uid map: Permission denied\n")

    box = LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=True,
                     which=lambda name, **_: f"/usr/bin/{name}", run=denied)

    assert "uid map" in (box.preflight() or "")
    with pytest.raises(SandboxRefused):
        box.launch(["/bin/true"], tmp=leg_tmp(host), cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})


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
    leg = sandbox(host, writable=False).launch(["/bin/sh", "-c", script], tmp=leg_tmp(host), cwd=str(host["worktree"]), env=env,
                                               runtime_state=store)

    out = subprocess.run(leg.argv, env={**env, **leg.env}, capture_output=True, text=True, timeout=60).stdout

    assert out.split() == [f"{config}", f"{config}/projects", store[0], "gh=none", "cred=leg-login"]
    assert (config / ".credentials.json").read_text() == '{"fake": "v1"}'
