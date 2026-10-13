import os
import stat

import pytest

from issue_agent.adapters import atomic_file
from issue_agent.adapters.atomic_file import atomic_write


def test_atomic_write_replaces_content_privately(tmp_path):
    target = tmp_path / "snapshot.env"
    target.write_text("old")
    target.chmod(0o644)

    atomic_write(target, "new")

    assert target.read_text() == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert os.listdir(tmp_path) == ["snapshot.env"]


def test_a_failed_atomic_write_keeps_the_old_content_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "snapshot.env"
    target.write_text("old")

    def full_disk(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(atomic_file.os, "fsync", full_disk)

    with pytest.raises(OSError):
        atomic_write(target, b"new")

    assert target.read_text() == "old"
    assert os.listdir(tmp_path) == ["snapshot.env"]
