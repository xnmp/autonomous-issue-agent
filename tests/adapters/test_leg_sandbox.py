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
              f'echo x >> "{host["target"]}/.git/config" 2>/dev/null || echo config-ro; '
              f'touch "{host["target"]}/.git/hooks/post-checkout" 2>/dev/null || echo hooks-ro; '
              f'touch "{host["target"]}/.git/commondir" 2>/dev/null || echo common-root-ro')

    done = run_leg(sandbox(host), host, script)

    assert done.stdout.split() == ["committed", f"password={FAKE_TOKEN}", "link-ro", "config-ro", "hooks-ro",
                                   "common-root-ro"]
    assert _git("log", "-1", "--format=%s", "fix/5-x", cwd=host["target"]).strip() == "a"
    assert "fsmonitor" not in (host["target"] / ".git/config").read_text()


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
    env = {"HOME": str(host["home"]), "CODEX_HOME": str(host["tmp"] / "state/codex-hooks/k")}
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


def test_a_bwrap_that_cannot_create_namespaces_refuses_every_launch(host):
    def denied(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "bwrap: setting up uid map: Permission denied\n")

    box = LegSandbox(home=str(host["home"]), repository=str(host["target"]), writable=True,
                     which=lambda name, **_: f"/usr/bin/{name}", run=denied)

    assert "uid map" in (box.preflight() or "")
    with pytest.raises(SandboxRefused):
        box.launch(["/bin/true"], cwd=str(host["worktree"]), env={"PATH": "/usr/bin"})
