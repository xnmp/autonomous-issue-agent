from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from issue_agent.adapters.child_env import CHILD_ENV_MANIFEST
from issue_agent.adapters.control_store import BudgetLimit, ReservationRequest, SQLiteControlStore
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.domain.contracts import BudgetScope, Money, RunId


def _registrations(supervisor):
    """The live registry receipts, read the way recovery reads them."""
    paths = sorted(supervisor.directory.glob("*.json")) if supervisor.directory.exists() else ()
    return tuple(r for path in paths if (r := ProcessSupervisor._read(path)) is not None)


def _until(condition, seconds: float = 3) -> None:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)


def _wait_dead(process: subprocess.Popen[bytes]) -> None:
    _until(lambda: process.poll() is not None)
    assert process.poll() is not None


def _wait_gone(pid: int) -> None:
    _until(lambda: ProcessSupervisor.process_identity(pid) is None)
    assert ProcessSupervisor.process_identity(pid) is None


def _python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def _touch(marker: Path) -> tuple[str, ...]:
    """A paid target whose only effect is the marker it writes."""
    return _python(f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "malformed",
        "123 (short) R",
        "456 (replaced) R 1 123 " + "0 " * 16 + "9876",
        # An unreaped zombie or dead leader is not alive, so its lease can be reclaimed.
        *(f"123 (leader with ) and ( spaces) {state} 1 123 " + "0 " * 16 + "9876" for state in ("Z", "X")),
    ],
)
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


def _paid_run(tmp_path, run_id: RunId):
    """A supervisor beside a control store in which the daemon leased issue 1
    and reserved one paid leg bound to it; returns (supervisor, store, carrier env)."""
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    store = SQLiteControlStore(str(tmp_path / "control.db"))
    store.initialize()
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    scope = BudgetScope(
        "o/r", "repository", "USD", now.date().isoformat(), start, start + timedelta(days=1), ZoneInfo("UTC")
    )
    assert store.lease("o/r", "fix", 1, "o/r:fix")
    assert store.reserve(
        ReservationRequest(
            run_id,
            "o/r",
            "fix",
            "RESOLVE",
            "o/r:fix",
            Money("USD", 1),
            (BudgetLimit(scope, Money("USD", 10)),),
            1,
            issue=1,
        )
    ).admitted
    env = {
        **os.environ,
        "ISSUE_AGENT_CONTROL_DB": store.path,
        "ISSUE_AGENT_RUN_ID": str(run_id),
        "ISSUE_AGENT_OWNER_ID": "o/r:fix",
    }
    return supervisor, store, env


def _sleepers(supervisor, prefix: str, count: int = 2) -> list[subprocess.Popen]:
    processes = [subprocess.Popen(["sleep", "30"], start_new_session=True) for _ in range(count)]
    for index, process in enumerate(processes):
        supervisor.register(RunId(f"{prefix}-{index}"), os.getpgid(process.pid))
    return processes


def test_boot_recovery_kills_all_valid_registered_groups(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    processes = _sleepers(supervisor, "run")

    assert supervisor.recover_orphans() == (RunId("run-0"), RunId("run-1"))
    for process in processes:
        _wait_dead(process)


# Above the kernel's largest pid_max: a launcher that can never be alive.
DEAD_LAUNCHER = "4194305-1"


def _leg_tmp(supervisor, run_id: str, launcher: str = "") -> Path:
    """A leg's temp directory as its sandbox leaves it, made by this process
    or by `launcher`: written, with a link out, directories it made read-only
    or closed, and /tmp itself read-only."""
    path = (supervisor.tmp_directory / launcher / run_id) if launcher else Path(supervisor.leg_tmp(RunId(run_id)))
    (path / "tmp/archive/deep").mkdir(parents=True)
    (path / "tmp/trace.json").write_text("{}")
    (path / "tmp/archive/deep/file").write_text("x")
    (path / "tmp/closed").mkdir()
    (path / "tmp/closed/file").write_text("x")
    (path / "tmp/link").symlink_to(supervisor.state_dir.parent / "keep")
    for directory, mode in (("tmp/archive/deep", 0o500), ("tmp/archive", 0o500), ("tmp/closed", 0o000),
                            ("tmp", 0o500)):
        (path / directory).chmod(mode)
    return path


def _disk_full(monkeypatch) -> None:
    import errno

    import issue_agent.adapters.process_supervisor as module

    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(module, "atomic_write", full)


def test_a_legs_temp_directory_goes_with_its_process_group(tmp_path) -> None:
    (tmp_path / "keep").mkdir(mode=0o755)
    (tmp_path / "keep").chmod(0o755)
    (tmp_path / "keep/file").write_text("kept")
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    (process,) = _sleepers(supervisor, "leg", 1)
    tmp = _leg_tmp(supervisor, "leg-0")

    supervisor.kill_tree(RunId("leg-0"))

    _wait_dead(process)
    assert not tmp.exists()
    assert (tmp_path / "keep/file").read_text() == "kept"  # never follows the leg's link
    assert stat.S_IMODE((tmp_path / "keep").stat().st_mode) == 0o755  # nor chmods through it


def test_a_legs_temp_directory_goes_when_its_process_never_registered(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path / "state"))
    (tmp_path / "keep").mkdir()
    tmp = _leg_tmp(supervisor, "never-ran")

    supervisor.kill_tree(RunId("never-ran"))

    assert not tmp.exists()


def test_a_full_disk_still_kills_the_group_and_frees_its_temp_directory(tmp_path, monkeypatch) -> None:
    # The cancellation receipt cannot be written; the group must not survive
    # to keep filling the disk with its /tmp (review of #474).
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    (tmp_path / "keep").mkdir()
    (process,) = _sleepers(supervisor, "leg", 1)
    tmp = _leg_tmp(supervisor, "leg-0")
    _disk_full(monkeypatch)

    with pytest.raises(OSError):
        supervisor.kill_tree(RunId("leg-0"))

    _wait_dead(process)
    assert not tmp.exists()


def test_boot_recovery_on_a_full_disk_frees_leftovers_and_kills_orphans(tmp_path, monkeypatch) -> None:
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    (tmp_path / "keep").mkdir()
    (process,) = _sleepers(supervisor, "orphan", 1)
    left = [_leg_tmp(supervisor, "orphan-0"), _leg_tmp(supervisor, "crashed", launcher=DEAD_LAUNCHER)]
    _disk_full(monkeypatch)

    with pytest.raises(OSError):
        supervisor.recover_orphans()

    _wait_dead(process)
    assert not any(path.exists() for path in left)


def test_boot_recovery_removes_temp_directories_a_crash_left(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    (tmp_path / "keep").mkdir()
    (process,) = _sleepers(supervisor, "orphan", 1)
    left = [_leg_tmp(supervisor, "orphan-0"),
            _leg_tmp(supervisor, "crashed-before-registering", launcher=DEAD_LAUNCHER),
            _leg_tmp(supervisor, "unidentified-host", launcher="unidentified")]

    supervisor.recover_orphans()

    _wait_dead(process)
    assert not any(path.exists() for path in left)
    assert not (supervisor.tmp_directory / DEAD_LAUNCHER).exists()
    assert ProcessSupervisor(str(tmp_path / "absent")).recover_orphans() == ()  # nothing to sweep


def test_any_recovery_removes_a_dead_daemons_unregistered_temp_directory_but_no_live_one(tmp_path) -> None:
    # A retired registry (an old checkout, the sibling role) never boots again.
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    (tmp_path / "keep").mkdir()
    (process,) = _sleepers(supervisor, "live", 1)  # a legacy receipt its live parent keeps
    live_launch = _leg_tmp(supervisor, "launching")  # this daemon is alive: a launch in progress
    leftover = _leg_tmp(supervisor, "crashed", launcher=DEAD_LAUNCHER)
    still_registered = _leg_tmp(supervisor, "live-0", launcher=DEAD_LAUNCHER)
    try:
        assert supervisor.recover_abandoned_orphans() == ()

        assert not leftover.exists()
        assert (live_launch / "tmp/trace.json").exists() and (still_registered / "tmp/trace.json").exists()
    finally:
        supervisor.kill_tree(RunId("live-0"))
        supervisor.kill_tree(RunId("launching"))
        _wait_dead(process)
    assert not still_registered.exists() and not live_launch.exists()


@pytest.mark.parametrize("raise_limit", [True, False])
def test_a_temp_tree_deeper_than_the_open_file_limit(tmp_path, raise_limit) -> None:
    # Removal holds a descriptor per level; units start at a soft limit of
    # 1024. The daemon raises it at start, and a tree still too deep is logged.
    script = f"""
import os, resource
from pathlib import Path
from issue_agent.adapters.process_supervisor import ProcessSupervisor
from issue_agent.app.main import _raise_open_file_limit
from issue_agent.domain.contracts import RunId
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
if {raise_limit!r}:
    _raise_open_file_limit()
supervisor = ProcessSupervisor({str(tmp_path / "state")!r})
root = Path(supervisor.leg_tmp(RunId("deep"))) / "tmp"
root.mkdir(parents=True)
fd = os.open(root, os.O_RDONLY)
for _ in range(300):
    os.mkdir("a", dir_fd=fd)
    deeper = os.open("a", os.O_RDONLY, dir_fd=fd)
    os.close(fd)
    fd = deeper
os.close(fd)
supervisor.kill_tree(RunId("deep"))
print("present" if root.exists() else "removed")
"""
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    if raise_limit:
        assert done.stdout.strip() == "removed"
    else:
        assert done.stdout.strip() == "present"
        assert "could not remove leg temp directory" in done.stderr


def test_wake_recovery_keeps_a_live_legs_temp_directory(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    (tmp_path / "keep").mkdir()
    (process,) = _sleepers(supervisor, "live", 1)
    tmp = _leg_tmp(supervisor, "live-0")
    try:
        assert supervisor.recover_abandoned_orphans() == ()
        assert (tmp / "tmp/trace.json").exists()
    finally:
        supervisor.kill_tree(RunId("live-0"))
        _wait_dead(process)


@pytest.mark.parametrize("run_id", ["", ".", "..", "../escape", "a/b", "a\0b"])
def test_a_run_id_that_is_not_a_plain_name_has_no_temp_directory(tmp_path, run_id) -> None:
    with pytest.raises(ValueError):
        ProcessSupervisor(str(tmp_path)).leg_tmp(RunId(run_id))


def test_legacy_recovery_does_not_reap_a_live_sibling_owner(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    (process,) = _sleepers(supervisor, "live-sibling", 1)
    run_id = RunId("live-sibling-0")
    try:
        assert supervisor.recover_abandoned_orphans() == ()
        assert process.poll() is None
        assert _registrations(supervisor)[0].run_id == run_id
    finally:
        supervisor.kill_tree(run_id)
        _wait_dead(process)


def test_shutdown_force_kill_does_not_spend_the_boot_recovery_grace_per_group(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=10)
    processes = _sleepers(supervisor, "forced")

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


def test_stale_group_leader_identity_is_never_signalled(tmp_path, monkeypatch) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0)
    (process,) = _sleepers(supervisor, "stale", 1)
    receipt = tmp_path / "process-groups" / "stale-0.json"
    raw = json.loads(receipt.read_text())
    raw["leader"]["start_ticks"] += 1
    receipt.write_text(json.dumps(raw))
    signalled = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))

    assert supervisor.recover_orphans() == (RunId("stale-0"),)
    assert signalled == []
    assert process.poll() is None
    process.terminate()
    _wait_dead(process)


def test_group_descendant_is_killed_after_registered_leader_exits(tmp_path) -> None:
    child_pid_file = tmp_path / "child.pid"
    process = subprocess.Popen(
        ["/bin/sh", "-c", f"sleep 30 & echo $! > {child_pid_file}; sleep 0.2"],
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

    _wait_gone(child_pid)
    assert _registrations(supervisor) == ()


def test_term_grace_kills_stubborn_descendant_after_leader_exits(tmp_path) -> None:
    child_pid_file = tmp_path / "stubborn-child.pid"
    process = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            "trap 'exit 0' TERM; (trap '' TERM; while :; do sleep 1; done) & "
            f"echo $! > {child_pid_file}; while :; do sleep 1; done",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _until(child_pid_file.exists)
    child_pid = int(child_pid_file.read_text().strip())
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.1)
    run_id = RunId("leader-exits-during-grace")
    supervisor.register(run_id, os.getpgid(process.pid))

    supervisor.kill_tree(run_id)

    _wait_dead(process)
    _wait_gone(child_pid)
    assert process.returncode == 0
    assert _registrations(supervisor) == ()


def test_cancellation_before_wrapper_registration_prevents_paid_exec(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    run_id = RunId("cancel-before-register")
    marker = tmp_path / "paid-target-started"
    command = supervisor.supervised_command(run_id, _touch(marker))
    environment = {**os.environ, **supervisor.launch_guard_env()}

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
        assert _registrations(supervisor) == ()
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_superseded_launch_token_cannot_register_against_a_retry(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path), grace_seconds=0.05)
    run_id = RunId("superseded-launch")
    stale_marker = tmp_path / "stale-started"
    stale = supervisor.supervised_command(run_id, _touch(stale_marker))
    supervisor.kill_tree(run_id)
    current = supervisor.supervised_command(run_id, _python("import time; time.sleep(30)"))
    env = {**os.environ, **supervisor.launch_guard_env()}

    stale_result = subprocess.run(stale, start_new_session=True, env=env, check=False, capture_output=True)
    process = subprocess.Popen(current, start_new_session=True, env=env)
    try:
        _until(lambda: _registrations(supervisor))
        assert stale_result.returncode != 0
        assert not stale_marker.exists()
        assert _registrations(supervisor)[0].leader.pid == process.pid
    finally:
        supervisor.kill_tree(run_id)
        _wait_dead(process)


def test_supervised_paid_command_registers_the_sqlite_run_lease(tmp_path) -> None:
    run_id = RunId("paid-run")
    supervisor, store, env = _paid_run(tmp_path, run_id)
    command = supervisor.supervised_command(run_id, _python("import time; time.sleep(30)"))

    process = subprocess.Popen(command, start_new_session=True, env={**env, **supervisor.launch_guard_env()})
    # The daemon's own lease is replaced by the child's process identity.
    _until(lambda: store.leases()[0].pid == process.pid)

    lease = store.leases()[0]
    assert (lease.run_id, lease.repository, lease.role, lease.target, lease.owner_id) == (
        run_id,
        "o/r",
        "fix",
        1,
        "o/r:fix",
    )
    assert (lease.pid, lease.pgid) == (process.pid, process.pid)
    assert lease.process_start_ticks > 0

    supervisor.kill_tree(run_id)
    _wait_dead(process)
    assert store.reclaim_dead_leases("o/r:fix", lambda _pid, _ticks: False) == (run_id,)
    assert store.settle_terminal_estimate(run_id, "test complete")
    assert store.leases() == ()


def test_supervised_paid_command_scrubs_authority_from_the_target_environment(tmp_path) -> None:
    run_id = RunId("authority-carrier")
    supervisor, store, env = _paid_run(tmp_path, run_id)
    observed = tmp_path / "observed.json"
    command = supervisor.supervised_command(
        run_id,
        _python(
            "import json, os; from pathlib import Path; "
            f"Path({str(observed)!r}).write_text(json.dumps({{k: os.environ.get(k) for k in ("
            "'ISSUE_AGENT_CONTROL_DB', 'ISSUE_AGENT_RUN_ID', 'ISSUE_AGENT_OWNER_ID')}))"
        ),
    )

    completed = subprocess.run(
        command,
        start_new_session=True,
        env={**env, **supervisor.launch_guard_env()},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(observed.read_text()) == {
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
        _python(
            "import os, signal, time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(target_pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
        ),
    )
    process = subprocess.Popen(command, start_new_session=True)
    _until(target_pid_file.exists)
    assert target_pid_file.exists()
    target_pid = int(target_pid_file.read_text())

    try:
        process.terminate()
        _wait_dead(process)
        _wait_gone(target_pid)
        assert process.returncode == -signal.SIGKILL
    finally:
        supervisor.kill_tree(run_id)


def test_supervised_paid_command_refuses_exec_after_launcher_death(tmp_path) -> None:
    run_id = RunId("orphaned-launch")
    supervisor, store, env = _paid_run(tmp_path, run_id)
    marker = tmp_path / "paid-command-started"
    command = supervisor.supervised_command(run_id, _touch(marker))

    completed = subprocess.run(
        command,
        start_new_session=True,
        check=False,
        capture_output=True,
        env={**env, "ISSUE_AGENT_SUPERVISOR_PARENT_PID": "99999999", "ISSUE_AGENT_SUPERVISOR_PARENT_START_TICKS": "1"},
    )

    assert completed.returncode != 0
    assert not marker.exists()
    # Only the daemon's claim remains: no child process was registered.
    assert [lease.pid for lease in store.leases()] == [os.getpid()]
    assert _registrations(supervisor) == ()


def test_supervised_sdk_cli_execs_its_command_with_the_sdk_arguments_appended(tmp_path) -> None:
    # The command is the sandbox wrapping the CLI (#413): its own arguments
    # come first, then the SDK's.
    seen = tmp_path / "argv"
    script = tmp_path / "wrapper.sh"
    script.write_text(f'printf "%s\\n" "$@" > "{seen}"\n')
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    wrapper, supervised_env = supervisor.supervised_cli(RunId("sdk-cmd"), ("/bin/sh", str(script), "--sandboxed"))

    completed = subprocess.run([wrapper, "--sdk-argument"], env={**os.environ, **supervised_env}, timeout=30)

    assert completed.returncode == 0
    assert seen.read_text().splitlines() == ["--sandboxed", "--sdk-argument"]


def test_supervised_sdk_cli_refuses_an_unexecutable_command(tmp_path) -> None:
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)

    with pytest.raises(ValueError):
        supervisor.supervised_cli(RunId("sdk-none"), (str(tmp_path / "missing"),))
    with pytest.raises(ValueError):
        supervisor.supervised_cli(RunId("sdk-empty"), ())


def test_supervised_sdk_cli_registers_session_before_target_executes(tmp_path) -> None:
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
    supervisor = ProcessSupervisor(str(tmp_path / "state"), grace_seconds=0.05)
    run_id = RunId("sdk-cli")
    wrapper, supervised_env = supervisor.supervised_cli(run_id, (str(target),))
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
    _until(marker.exists)

    assert marker.exists()
    registration = _registrations(supervisor)[0]
    assert (registration.leader.pid, registration.pgid) == (process.pid, process.pid)

    supervisor.kill_tree(run_id)
    _wait_dead(process)
