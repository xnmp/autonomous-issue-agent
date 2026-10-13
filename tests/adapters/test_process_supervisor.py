from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.child_env import CHILD_ENV_MANIFEST
from issue_agent.adapters.control_store import (
    BudgetLimit,
    ReservationRequest,
    SQLiteControlStore,
)
from issue_agent.adapters.process_supervisor import ProcessIdentity, ProcessSupervisor
from issue_agent.domain.contracts import (
    BudgetScope,
    Money,
    RunId,
)


def _wait_dead(process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 3
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert process.poll() is not None


@pytest.mark.parametrize("state", ["R", "S", "Z", "X"])
def test_selected_process_identity_needs_no_host_census(monkeypatch, state):
    original_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: path == Path("/proc") or original_is_dir(path))
    fields = [state, "1", "123", *("0" for _ in range(16)), "9876"]

    def read_stat(path, **kwargs):
        assert path == Path("/proc/123/stat")
        return f"123 (command with ) and ( spaces) {' '.join(fields)}"

    def unavailable_census(path):
        raise PermissionError("other processes are not observable")

    monkeypatch.setattr(Path, "read_text", read_stat)
    monkeypatch.setattr(Path, "iterdir", unavailable_census)
    live = state not in {"Z", "X"}
    assert ProcessSupervisor.process_identity(123) == (ProcessIdentity(123, 9876) if live else None)
    assert ProcessSupervisor.matches_identity(123, 9876) is live
    assert not ProcessSupervisor.matches_identity(123, 9877)


@pytest.mark.parametrize("raw", [None, "malformed", "123 (short) R", "456 (replaced) R 1 123 " + "0 " * 16 + "9876"])
def test_selected_process_identity_refuses_absent_or_malformed_stat(monkeypatch, raw):
    original_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: path == Path("/proc") or original_is_dir(path))

    def read_stat(path, **kwargs):
        assert path == Path("/proc/123/stat")
        if raw is None:
            raise FileNotFoundError(path)
        return raw

    monkeypatch.setattr(Path, "read_text", read_stat)
    assert ProcessSupervisor.process_identity(123) is None
    assert not ProcessSupervisor.matches_identity(123, 9876)


def _reserve_paid_run(
    store: SQLiteControlStore,
    run_id: RunId,
) -> ReservationRequest:
    """Lease issue 1 as the daemon and reserve one paid leg bound to it."""
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scope = BudgetScope(
        "o/r", "repository", "USD", now.date().isoformat(), start, start + timedelta(days=1), ZoneInfo("UTC"),
    )
    assert store.lease("o/r", "fix", 1, "o/r:fix")
    request = ReservationRequest(
        run_id, "o/r", "fix", "RESOLVE", "o/r:fix", Money("USD", 1),
        (BudgetLimit(scope, Money("USD", 10)),), 1, issue=1,
    )
    assert store.reserve(request).admitted
    return request


def test_registered_process_group_is_killed_and_receipt_removed(tmp_path) -> None:
    process = subprocess.Popen(
        ["sleep", "30"], start_new_session=True, stdout=subprocess.DEVNULL
    )
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    run_id = RunId("run-1")
    supervisor.register(run_id, os.getpgid(process.pid))

    supervisor.kill_tree(run_id)

    _wait_dead(process)
    assert supervisor.registrations() == ()


def test_boot_recovery_kills_all_valid_registered_groups(tmp_path) -> None:
    processes = [
        subprocess.Popen(["sleep", "30"], start_new_session=True)
        for _ in range(2)
    ]
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    for index, process in enumerate(processes):
        supervisor.register(RunId(f"run-{index}"), os.getpgid(process.pid))

    assert supervisor.recover_orphans() == (RunId("run-0"), RunId("run-1"))
    for process in processes:
        _wait_dead(process)


def test_legacy_recovery_does_not_reap_a_live_sibling_owner(tmp_path) -> None:
    process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    run_id = RunId("live-sibling")
    supervisor.register(run_id, os.getpgid(process.pid))
    try:
        assert supervisor.recover_abandoned_orphans() == ()
        assert process.poll() is None
        assert supervisor.registrations()[0].run_id == run_id
    finally:
        supervisor.kill_tree(run_id)
        _wait_dead(process)


def test_shutdown_force_kill_does_not_spend_the_boot_recovery_grace_per_group(
    tmp_path,
) -> None:
    processes = [
        subprocess.Popen(["sleep", "30"], start_new_session=True)
        for _ in range(2)
    ]
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=10)
    for index, process in enumerate(processes):
        supervisor.register(RunId(f"forced-{index}"), os.getpgid(process.pid))

    started = time.monotonic()
    reaped = supervisor.force_kill_all()

    assert reaped == (RunId("forced-0"), RunId("forced-1"))
    assert time.monotonic() - started < 1
    for process in processes:
        _wait_dead(process)


def test_malformed_registration_is_quarantined_without_signalling(tmp_path) -> None:
    directory = tmp_path / "process-groups"
    directory.mkdir()
    (directory / "bad.json").write_text('{"pgid": "not-a-pid"}')

    assert ProcessSupervisor(str(tmp_path)).recover_orphans() == ()
    assert (directory / "bad.invalid").exists()


def test_registered_pid_tree_kills_descendants_without_signalling_parent_group(
    tmp_path,
) -> None:
    process = subprocess.Popen(["sleep", "30"])
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    root = next(
        identity
        for identity in supervisor.direct_children()
        if identity.pid == process.pid
    )
    run_id = RunId("claude-run")
    supervisor.register_process_tree(run_id, (root,))

    supervisor.kill_process_tree(run_id)

    _wait_dead(process)


def test_boot_recovery_reaps_registered_pid_tree(tmp_path) -> None:
    process = subprocess.Popen(["sleep", "30"])
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    root = next(
        identity
        for identity in supervisor.direct_children()
        if identity.pid == process.pid
    )
    supervisor.register_process_tree(RunId("claude-run"), (root,))

    assert supervisor.recover_orphans() == (RunId("claude-run"),)
    _wait_dead(process)


def test_stale_group_leader_identity_is_never_signalled(
    tmp_path, monkeypatch
) -> None:
    process = subprocess.Popen(
        ["sleep", "30"], start_new_session=True, stdout=subprocess.DEVNULL
    )
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0)
    run_id = RunId("stale")
    supervisor.register(run_id, os.getpgid(process.pid))
    receipt = tmp_path / "process-groups" / "stale.json"
    raw = json.loads(receipt.read_text())
    raw["leader"]["start_ticks"] += 1
    receipt.write_text(json.dumps(raw))
    signalled = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))

    assert supervisor.recover_orphans() == (run_id,)
    assert signalled == []
    assert process.poll() is None
    process.terminate()
    _wait_dead(process)


def test_group_descendant_is_killed_after_registered_leader_exits(
    tmp_path,
) -> None:
    child_pid_file = tmp_path / "child.pid"
    process = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            f"sleep 30 & echo $! > {child_pid_file}; sleep 0.2",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    run_id = RunId("leader-exited")
    supervisor.register(run_id, os.getpgid(process.pid))
    process.wait(timeout=3)
    child_pid = int(child_pid_file.read_text().strip())
    assert ProcessSupervisor.process_identity(child_pid) is not None

    supervisor.kill_tree(run_id)

    deadline = time.monotonic() + 3
    while (
        ProcessSupervisor.process_identity(child_pid) is not None
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    assert ProcessSupervisor.process_identity(child_pid) is None
    assert supervisor.registrations() == ()


def test_term_grace_kills_stubborn_descendant_after_leader_exits(
    tmp_path,
) -> None:
    child_pid_file = tmp_path / "stubborn-child.pid"
    process = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            (
                "trap 'exit 0' TERM; "
                "(trap '' TERM; while :; do sleep 1; done) & "
                f"echo $! > {child_pid_file}; "
                "while :; do sleep 1; done"
            ),
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 3
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(child_pid_file.read_text().strip())
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.1)
    run_id = RunId("leader-exits-during-grace")
    supervisor.register(run_id, os.getpgid(process.pid))

    supervisor.kill_tree(run_id)

    _wait_dead(process)
    deadline = time.monotonic() + 3
    while (
        ProcessSupervisor.process_identity(child_pid) is not None
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    assert process.returncode == 0
    assert ProcessSupervisor.process_identity(child_pid) is None
    assert supervisor.registrations() == ()


def test_supervised_command_registers_before_work_executes(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    run_id = RunId("wrapped")
    command = supervisor.supervised_command(
        run_id,
        (
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ),
    )
    process = subprocess.Popen(command, start_new_session=True)
    deadline = time.monotonic() + 3
    while not supervisor.registrations() and time.monotonic() < deadline:
        time.sleep(0.02)

    registration = supervisor.registrations()[0]
    assert registration.run_id == run_id
    assert registration.pgid == process.pid
    assert registration.leader.pid == process.pid

    supervisor.kill_tree(run_id)
    _wait_dead(process)


def test_cancellation_before_wrapper_registration_prevents_paid_exec(
    tmp_path,
) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    run_id = RunId("cancel-before-register")
    marker = tmp_path / "paid-target-started"
    command = supervisor.supervised_command(
        run_id,
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
        ),
    )
    environment = {
        **os.environ,
        **supervisor.launch_guard_env(),
    }

    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            os.kill(os.getpid(), signal.SIGSTOP)
            os.execvpe(command[0], command, environment)
        finally:
            os._exit(127)
    try:
        stopped_pid, status = os.waitpid(pid, os.WUNTRACED)
        assert stopped_pid == pid
        assert os.WIFSTOPPED(status)

        supervisor.kill_tree(run_id)
        os.kill(pid, signal.SIGCONT)
        exited_pid, status = os.waitpid(pid, 0)

        assert exited_pid == pid
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) != 0
        assert not marker.exists()
        assert supervisor.registrations() == ()
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_superseded_launch_token_cannot_register_against_a_retry(
    tmp_path,
) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    run_id = RunId("superseded-launch")
    stale_marker = tmp_path / "stale-started"
    stale = supervisor.supervised_command(
        run_id,
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(stale_marker)!r}).touch()",
        ),
    )
    supervisor.kill_tree(run_id)
    current = supervisor.supervised_command(
        run_id,
        (sys.executable, "-c", "import time; time.sleep(30)"),
    )

    stale_result = subprocess.run(
        stale,
        start_new_session=True,
        env={**os.environ, **supervisor.launch_guard_env()},
        check=False,
        capture_output=True,
    )
    process = subprocess.Popen(
        current,
        start_new_session=True,
        env={**os.environ, **supervisor.launch_guard_env()},
    )
    try:
        deadline = time.monotonic() + 3
        while (
            not supervisor.registrations()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert stale_result.returncode != 0
        assert not stale_marker.exists()
        assert supervisor.registrations()[0].leader.pid == process.pid
    finally:
        supervisor.kill_tree(run_id)
        _wait_dead(process)


def test_supervised_paid_command_registers_the_sqlite_run_lease(tmp_path) -> None:
    state_dir = tmp_path / "state"
    supervisor = ProcessSupervisor(str(state_dir), grace_seconds=0.05)
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    run_id = RunId("paid-run")
    request = _reserve_paid_run(store, run_id)
    command = supervisor.supervised_command(
        run_id,
        (sys.executable, "-c", "import time; time.sleep(30)"),
    )

    process = subprocess.Popen(
        command,
        start_new_session=True,
        env={
            **os.environ,
            **supervisor.launch_guard_env(),
            "ISSUE_AGENT_CONTROL_DB": store.path,
            "ISSUE_AGENT_RUN_ID": str(run_id),
            "ISSUE_AGENT_OWNER_ID": request.owner_id,
        },
    )
    deadline = time.monotonic() + 3
    # The daemon's own lease is replaced by the child's process identity.
    while store.leases()[0].pid != process.pid and time.monotonic() < deadline:
        time.sleep(0.02)

    lease = store.leases()[0]
    assert lease.run_id == run_id
    assert (lease.repository, lease.role, lease.target) == ("o/r", "fix", 1)
    assert lease.owner_id == request.owner_id
    assert lease.pid == process.pid
    assert lease.pgid == process.pid
    assert lease.process_start_ticks > 0

    supervisor.kill_tree(run_id)
    _wait_dead(process)
    assert store.reclaim_dead_leases("o/r:fix", lambda _pid, _ticks: False) == (run_id,)
    assert store.settle_terminal_estimate(run_id, "test complete")
    assert store.leases() == ()


def test_supervised_paid_command_scrubs_authority_from_the_target_environment(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    supervisor = ProcessSupervisor(str(state_dir), grace_seconds=0.05)
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    run_id = RunId("authority-carrier")
    request = _reserve_paid_run(store, run_id)
    observed = tmp_path / "observed.json"
    program = (
        "import json, os; "
        "from pathlib import Path; "
        f"Path({str(observed)!r}).write_text(json.dumps({{"
        "'child': {k: os.environ.get(k) for k in ("
        "'ISSUE_AGENT_CONTROL_DB', 'ISSUE_AGENT_RUN_ID', 'ISSUE_AGENT_OWNER_ID')}}))"
    )
    command = supervisor.supervised_command(
        run_id,
        (sys.executable, "-c", program),
    )

    completed = subprocess.run(
        command,
        start_new_session=True,
        env={
            **os.environ,
            **supervisor.launch_guard_env(),
            "ISSUE_AGENT_CONTROL_DB": store.path,
            "ISSUE_AGENT_RUN_ID": str(run_id),
            "ISSUE_AGENT_OWNER_ID": request.owner_id,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(observed.read_text())
    assert result["child"] == {
        "ISSUE_AGENT_CONTROL_DB": None,
        "ISSUE_AGENT_RUN_ID": None,
        "ISSUE_AGENT_OWNER_ID": None,
    }

    supervisor.kill_tree(run_id)
    assert store.reclaim_dead_leases("o/r:fix", lambda _pid, _ticks: False) == (run_id,)
    assert store.settle_terminal_estimate(run_id, "test complete")
    assert store.leases() == ()


def test_carrier_forwards_direct_termination_to_the_target_group(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    run_id = RunId("carrier-signal")
    target_pid_file = tmp_path / "target.pid"
    command = supervisor.supervised_command(
        run_id,
        (
            sys.executable,
            "-c",
            (
                "import os, signal, time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(target_pid_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(30)"
            ),
        ),
    )
    process = subprocess.Popen(command, start_new_session=True)
    deadline = time.monotonic() + 3
    while not target_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert target_pid_file.exists()
    target_pid = int(target_pid_file.read_text())

    try:
        process.terminate()
        _wait_dead(process)
        deadline = time.monotonic() + 3
        while (
            ProcessSupervisor.process_identity(target_pid) is not None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        assert ProcessSupervisor.process_identity(target_pid) is None
        assert process.returncode == -signal.SIGKILL
    finally:
        supervisor.kill_tree(run_id)


def test_carrier_escalation_kills_descendants_forked_during_grace(
    tmp_path,
) -> None:
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    run_id = RunId("carrier-fork-race")
    target_pid_file = tmp_path / "targets.pid"
    target = tmp_path / "resistant_target.py"
    target.write_text(
        "import os\n"
        "import signal\n"
        "import time\n"
        "from pathlib import Path\n"
        "triggered = False\n"
        "def resist(_signum, _frame):\n"
        "    global triggered\n"
        "    triggered = True\n"
        "signal.signal(signal.SIGTERM, resist)\n"
        f"path = Path({str(target_pid_file)!r})\n"
        "path.write_text(f'{os.getpid()}\\n')\n"
        "while not triggered:\n"
        "    time.sleep(0.01)\n"
        "while True:\n"
        "    child = os.fork()\n"
        "    if child == 0:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "        time.sleep(30)\n"
        "        os._exit(0)\n"
        "    with path.open('a') as stream:\n"
        "        stream.write(f'{child}\\n')\n"
        "    time.sleep(0.05)\n"
    )
    command = supervisor.supervised_command(
        run_id,
        (sys.executable, str(target)),
    )
    process = subprocess.Popen(command, start_new_session=True)
    deadline = time.monotonic() + 3
    while not target_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert target_pid_file.exists()

    try:
        process.terminate()
        _wait_dead(process)
        target_pids = tuple(
            int(line) for line in target_pid_file.read_text().splitlines()
        )
        deadline = time.monotonic() + 3
        while (
            any(
                ProcessSupervisor.process_identity(pid) is not None
                for pid in target_pids
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        assert len(target_pids) > 2
        assert process.returncode == -signal.SIGKILL
        assert all(
            ProcessSupervisor.process_identity(pid) is None
            for pid in target_pids
        )
    finally:
        supervisor.kill_tree(run_id)


def test_supervised_paid_command_refuses_exec_after_launcher_death(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    supervisor = ProcessSupervisor(str(state_dir), grace_seconds=0.05)
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    run_id = RunId("orphaned-launch")
    request = _reserve_paid_run(store, run_id)
    marker = tmp_path / "paid-command-started"
    command = supervisor.supervised_command(
        run_id,
        (
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')",
        ),
    )

    completed = subprocess.run(
        command,
        start_new_session=True,
        env={
            **os.environ,
            "ISSUE_AGENT_CONTROL_DB": store.path,
            "ISSUE_AGENT_RUN_ID": str(run_id),
            "ISSUE_AGENT_OWNER_ID": request.owner_id,
            "ISSUE_AGENT_SUPERVISOR_PARENT_PID": "99999999",
            "ISSUE_AGENT_SUPERVISOR_PARENT_START_TICKS": "1",
        },
        check=False,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert not marker.exists()
    # Only the daemon's claim remains: no child process was registered.
    assert [lease.pid for lease in store.leases()] == [os.getpid()]
    assert supervisor.registrations() == ()


def test_supervised_sdk_cli_registers_session_before_target_executes(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    marker = tmp_path / "target-started"
    target = tmp_path / "claude"
    target.write_text(
        "#!/bin/sh\n"
        '[ -z "${ISSUE_AGENT_SUPERVISOR_RUN_ID:-}" ] || exit 8\n'
        '[ -z "${GH_TOKEN:-}" ] || exit 9\n'
        '[ "${TEST_CMD:-}" = "uv run pytest" ] || exit 10\n'
        '[ -z "${ISSUE_AGENT_AUTHORITY_BUNDLE:-}" ] || exit 11\n'
        '[ "${CARGO_INCREMENTAL:-}" = "0" ] || exit 13\n'
        '[ "${CARGO_TARGET_DIR:-}" = "/operator/shared-cargo-target" ] || exit 14\n'
        '[ "${CARGO_BUILD_JOBS:-}" = "2" ] || exit 15\n'
        '[ "${PW_WORKERS:-}" = "1" ] || exit 16\n'
        f'echo started > "{marker}"\n'
        "sleep 30\n"
    )
    target.chmod(0o755)
    supervisor = ProcessSupervisor(str(state_dir), grace_seconds=0.05)
    run_id = RunId("sdk-cli")
    wrapper, supervised_env = supervisor.supervised_cli(run_id, str(target))
    process = subprocess.Popen(
        [wrapper, "--sdk-argument"],
        env={
            **os.environ,
            **supervised_env,
            "GH_TOKEN": "must-not-reach-child",
            "TEST_CMD": "uv run pytest",
            "ISSUE_AGENT_AUTHORITY_BUNDLE": "must-stay-in-carrier",
            "CARGO_INCREMENTAL": "0",
            "CARGO_TARGET_DIR": "/operator/shared-cargo-target",
            "CARGO_BUILD_JOBS": "2",
            "PW_WORKERS": "1",
            CHILD_ENV_MANIFEST: "TEST_CMD ISSUE_AGENT_AUTHORITY_BUNDLE",
        },
    )
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    assert marker.exists()
    registration = supervisor.registrations()[0]
    assert registration.leader.pid == process.pid
    assert registration.pgid == process.pid

    supervisor.kill_tree(run_id)
    _wait_dead(process)
