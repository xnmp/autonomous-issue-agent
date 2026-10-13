"""Hot reload — commit-gated (partial edits cannot load), fires only between
units of work, and a broken watch costs hot reload, never the process."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from issue_agent.adapters.reload import RELOAD_EXIT_CODE, ReloadWatch
from issue_agent.app import dashboard
from issue_agent.app.dashweb import server as dashweb_server
from issue_agent.app.dashweb.server import _run_server_until_reload
from tests.fakes.executor import ImmediateExecutor as Executor
from issue_agent.app.tick import Daemon
from issue_agent.domain.world import WorldState


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "one")
    return tmp_path


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return False


def test_uncommitted_edits_do_not_trigger_but_a_commit_does(tmp_path):
    """The whole point: a mid-edit save (half of a multi-file change) must NOT
    reload the daemon; the commit — the 'this tree is coherent' signal — must."""
    repo = _repo(tmp_path)
    w = ReloadWatch(str(repo))
    assert w.changed() is False

    (repo / "a.py").write_text("x = 2\n")          # dirty tree, no commit
    (repo / "b.py").write_text("y = 1\n")
    assert w.changed() is False

    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two")
    assert w.changed() is True


def test_env_reload_uses_content_not_mtime(tmp_path):
    """AGENT_ENV is gitignored by design, so commits can never carry it; an
    operator edit to it is a single atomic save and reloads directly."""
    repo = _repo(tmp_path)
    env = tmp_path / "active.env"
    env.write_text("A=1\n")
    w = ReloadWatch(str(repo), files=(str(env),))
    assert w.changed() is False
    os.utime(env, (time.time() + 5, time.time() + 5))
    assert w.changed() is False
    env.write_text("A=2\n")
    assert w.changed() is True


def test_invalid_candidate_config_does_not_reload(tmp_path):
    repo = _repo(tmp_path)
    env = tmp_path / "active.env"
    env.write_text("A=1\n")
    w = ReloadWatch(
        str(repo),
        files=(str(env),),
        validate=lambda: (_ for _ in ()).throw(ValueError("invalid")),
    )
    env.write_text("A=bad\n")
    assert w.changed() is False


def test_a_non_repo_or_missing_env_file_never_flaps(tmp_path):
    w = ReloadWatch(str(tmp_path / "not-a-repo"), files=(str(tmp_path / "nope"),))
    assert w.changed() is False
    assert w.changed() is False


class _Gw:
    def relabel(self, *a): ...


def test_daemon_exits_with_reload_code_between_ticks():
    """The change is noticed only AFTER a completed tick (no session can be in
    flight), the tick count still advanced, and the exit carries the reserved
    code point-at's relaunch loop matches on."""
    ticked = []

    d = Daemon(
        name="fix",
        observe=lambda: WorldState(),
        decide=lambda w: ticked.append(1) or [],
        executor=Executor(_Gw()),
        poll_seconds=1,
    )
    logs = []
    d.log = logs.append
    with pytest.raises(SystemExit) as exc:
        d.run_forever(
            sleep=lambda s: None, now=lambda: 0,
            max_ticks=5, reload_check=lambda: len(ticked) >= 2,
        )
    assert exc.value.code == RELOAD_EXIT_CODE
    assert len(ticked) == 2                       # ran ticks, then left between them
    assert any("hot-reload" in m for m in logs)


def test_daemon_without_reload_check_never_exits_early():
    ticked = []
    d = Daemon(
        name="fix", observe=lambda: WorldState(),
        decide=lambda w: ticked.append(1) or [],
        executor=Executor(_Gw()), poll_seconds=1,
    )
    d.run_forever(sleep=lambda s: None, now=lambda: 0, max_ticks=3)
    assert len(ticked) == 3


def test_dashboard_source_change_gracefully_stops_server_with_reload_code():
    class GracefulServer:
        should_exit = False

        def __init__(self):
            self.request_started = threading.Event()
            self.request_drained = False

        def run(self):
            self.request_started.set()
            assert _wait_until(lambda: self.should_exit)
            # uvicorn's Server.run has the same contract: should_exit closes
            # listeners, then the call returns only after active requests drain.
            self.request_drained = True

    server = GracefulServer()
    logs = []

    code = _run_server_until_reload(
        server,
        lambda: server.request_started.is_set(),
        poll_seconds=0.001,
        log=logs.append,
    )

    assert code == RELOAD_EXIT_CODE
    assert server.should_exit
    assert server.request_drained
    assert logs == [
        "dashboard: source changed — exiting for supervisor restart; "
        "re-run `issue-agent dashboard` if unsupervised"
    ]


def test_dashboard_uvicorn_config_bounds_graceful_shutdown(monkeypatch):
    captured = {}

    class Config:
        def __init__(self, _app, **kwargs):
            captured.update(kwargs)

    class Server:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=Config, Server=Server),
    )
    monkeypatch.setenv("HOT_RELOAD", "0")
    monkeypatch.setattr(dashweb_server, "create_app", lambda backend=None: backend)

    assert dashweb_server.serve(8321, backend=object()) == 0
    assert captured["timeout_graceful_shutdown"] == 30


def test_fallback_dashboard_exits_for_supervisor_reload():
    class Server:
        def __init__(self):
            self.stopped = threading.Event()
            self.closed = False

        def serve_forever(self):
            assert self.stopped.wait(timeout=1)

        def shutdown(self):
            self.stopped.set()

        def server_close(self):
            self.closed = True

    server = Server()

    assert dashboard._serve_until_reload(
        server,
        reload_check=lambda: True,
        poll_seconds=0.001,
    ) == RELOAD_EXIT_CODE
    assert server.closed
