"""The daemon's open-file limit (#470): removing a leg's temp directory holds
a descriptor per level of its tree, and units start at a soft limit of 1024."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(("soft", "hard", "expected"), [
    (64, 2**31, 65536), (64, 4096, 4096), (100_000, 2**31, 100_000), (64, 65536, 65536)])
def test_the_daemon_raises_its_open_file_limit_only_as_far_as_legs_tolerate(monkeypatch, soft, hard, expected):
    import resource

    from issue_agent.app.main import _raise_open_file_limit

    limits = {resource.RLIMIT_NOFILE: (soft, hard)}
    monkeypatch.setattr(resource, "getrlimit", lambda which: limits[which])
    monkeypatch.setattr(resource, "setrlimit", lambda which, value: limits.__setitem__(which, value))

    _raise_open_file_limit()

    assert limits[resource.RLIMIT_NOFILE] == (expected, hard)


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
