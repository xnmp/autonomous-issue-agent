"""The service wrapper delivers one orderly signal to the daemon."""

import os
import selectors
import shutil
import signal
import subprocess
import sys
import time

import pytest

from issue_agent.adapters.process_supervisor import ProcessSupervisor


CHILD = r'''
import os, signal, subprocess, sys, time
from issue_agent.app.main import _TerminationHandler
from issue_agent.app.loop import Daemon
from issue_agent.domain.actions import RunAgent, AgentKind
from issue_agent.domain.world import WorldState
from tests.fakes.executor import ImmediateExecutor
from tests.fakes.in_memory_github import InMemoryGitHub
from issue_agent.adapters.process_supervisor import ProcessSupervisor
active = sys.argv[1] == "active"
workers = []
ready = False
def emit(line):
    os.write(1, (line + "\n").encode())
handler = _TerminationHandler()
def terminate(s, frame):
    emit("TERM")
    handler(s, frame)
signal.signal(signal.SIGTERM, terminate)
def run(action):
    p = subprocess.Popen([sys.executable, "-S", "-c", "import time; time.sleep(30)"])
    workers.append(p)
    emit(f"OWNED {p.pid} {ProcessSupervisor.process_identity(p.pid).start_ticks}")
    emit(f"READY {os.getpid()}")
    p.wait()
    print("REAPED", p.pid, p.returncode, flush=True)
def cancel():
    for p in workers:
        if p.poll() is None:
            p.terminate()
def sleep(seconds):
    global ready
    if not active and not ready:
        ready = True
        emit(f"READY {os.getpid()}")
    time.sleep(.05)
daemon = Daemon("private", lambda: WorldState(),
    lambda w: [RunAgent(AgentKind.RESOLVE, 1)] if active else [],
    ImmediateExecutor(InMemoryGitHub(), run_agent=run), poll_seconds=1,
    dispatch_capacity=1, shutdown_timeout_seconds=2, cancel_active_dispatches=cancel,
    finalize=lambda: print("FINALIZED", flush=True), log=lambda s: print(s, flush=True))
daemon.run_forever(sleep=sleep, now=lambda: 0)
'''


@pytest.mark.skipif(sys.platform != "linux", reason="Linux uv service signal routing")
@pytest.mark.parametrize("active", [False, True])
def test_uv_main_signal_finalizes_daemon_and_reaps_owned_workers(tmp_path, active):
    uv = shutil.which("uv")
    assert uv is not None
    process = subprocess.Popen(
        [uv, "run", "--no-sync", "python", "-u", "-c", CHILD, "active" if active else "idle"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        env={**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache")},
    )
    owned = []
    output = b""
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 10
            while not any(line.startswith(b"READY ") and line[6:].isdigit()
                          for line in output.split(b"\n")[:-1]):
                assert time.monotonic() < deadline, output.decode()
                if selector.select(.1):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    assert chunk, output.decode()
                    output += chunk
        for line in output.decode().splitlines():
            if line.startswith("OWNED "):
                owned.append(tuple(map(int, line.split()[1:])))
        process.send_signal(signal.SIGTERM)
        rest, stderr = process.communicate(timeout=5)
        output += rest
        assert process.returncode == 0, (output.decode(), stderr.decode())
        assert "draining" in output.decode()
        assert "FINALIZED" in output.decode()
        assert "stopped" in output.decode()
        assert output.decode().splitlines().count("TERM") == 1
        if active:
            assert owned and "REAPED " in output.decode()
        assert all(not ProcessSupervisor.matches_identity(pid, ticks) for pid, ticks in owned)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        for pid, ticks in owned:
            if ProcessSupervisor.matches_identity(pid, ticks):
                os.kill(pid, signal.SIGKILL)
