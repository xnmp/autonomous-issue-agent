from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "bin/service-terminal-evidence.sh"
MANAGER = {
    "INVOCATION_ID": "7e9203c8",
    "SERVICE_RESULT": "oom-kill",
    "EXIT_CODE": "killed",
    "EXIT_STATUS": "KILL",
}


def record(cgroup: Path, env: dict[str, str]) -> dict[str, str]:
    result = subprocess.run(
        ["/bin/sh", str(HOOK), str(cgroup)],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith("issue_agent_service_terminal ")
    return dict(field.split("=", 1) for field in lines[0].split()[1:])


def cgroup(tmp_path: Path, events: str | None, peak: str | None) -> Path:
    if events is not None:
        (tmp_path / "memory.events").write_text(events)
    if peak is not None:
        (tmp_path / "memory.peak").write_text(peak)
    return tmp_path


def test_record_joins_the_manager_result_with_the_units_oom_counters(tmp_path):
    unit = cgroup(
        tmp_path,
        "low 0\nhigh 0\nmax 12\noom 1\noom_kill 1\noom_group_kill 0\n",
        "4294967296\n",
    )

    assert record(unit, MANAGER) == {
        "invocation": "7e9203c8",
        "result": "oom-kill",
        "exit_code": "killed",
        "exit_status": "KILL",
        "oom": "1",
        "oom_kill": "1",
        "memory_peak": "4294967296",
    }


def test_unreadable_cgroup_evidence_is_unknown_never_zero(tmp_path):
    fields = record(tmp_path / "gone", MANAGER)

    assert (fields["oom"], fields["oom_kill"], fields["memory_peak"]) == (
        "unknown",
        "unknown",
        "unknown",
    )
    assert fields["result"] == "oom-kill"


@pytest.mark.parametrize(
    ("events", "peak"),
    [
        ("oom x\noom_kill -1\n", "12k\n"),
        ("oom 1 2\noom_kill\n", ""),
        ("", "max\n"),
        ("\x00\xff garbage", "9" * 40 + "\n"),
    ],
)
def test_malformed_cgroup_evidence_is_unknown(tmp_path, events, peak):
    fields = record(cgroup(tmp_path, events, peak), MANAGER)

    assert (fields["oom"], fields["oom_kill"], fields["memory_peak"]) == (
        "unknown",
        "unknown",
        "unknown",
    )


def test_absent_manager_fields_stay_empty_rather_than_invented(tmp_path):
    fields = record(cgroup(tmp_path, "oom 0\noom_kill 0\n", "1\n"), {})

    assert (
        fields["invocation"],
        fields["result"],
        fields["exit_code"],
        fields["exit_status"],
    ) == ("", "", "", "")
    assert (fields["oom"], fields["oom_kill"]) == ("0", "0")


@pytest.mark.parametrize(
    ("value", "reported"),
    [("9" * 20, "9" * 20), ("1" + "0" * 20, "unknown")],
)
def test_counters_beyond_u64_width_are_unknown_not_truncated(
    tmp_path, value, reported
):
    fields = record(
        cgroup(tmp_path, f"oom {value}\noom_kill {value}\n", f"{value}\n"),
        MANAGER,
    )

    assert (fields["oom"], fields["oom_kill"], fields["memory_peak"]) == (
        reported,
        reported,
        reported,
    )


def test_without_an_argument_the_hook_reads_its_own_cgroup():
    # Production path: systemd starts the hook inside the unit's cgroup.
    result = subprocess.run(
        ["/bin/sh", str(HOOK)],
        env={"PATH": "/usr/bin:/bin", **MANAGER},
        capture_output=True,
        text=True,
        check=True,
    )
    fields = dict(field.split("=", 1) for field in result.stdout.split()[1:])
    expected = {"oom": "unknown", "oom_kill": "unknown"}
    # The hook is a child of this process, so it starts in the same cgroup.
    unified = [
        line[3:]
        for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    ] if Path("/proc/self/cgroup").exists() else []
    events = Path(f"/sys/fs/cgroup{unified[0]}/memory.events") if unified else None
    if events is not None and events.exists():
        for line in events.read_text().splitlines():
            key, _, value = line.partition(" ")
            if key in expected:
                expected[key] = value

    assert (fields["oom"], fields["oom_kill"]) == (
        expected["oom"],
        expected["oom_kill"],
    )
    assert fields["result"] == "oom-kill"
