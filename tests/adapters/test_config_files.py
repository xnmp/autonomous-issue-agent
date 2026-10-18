"""Editing one assignment in an operator env file: comments, order and
`export` survive; what cannot be edited safely is refused, never guessed."""

from __future__ import annotations

import os
import stat
import select
import subprocess
import sys

import pytest

from issue_agent.adapters.config_files import (
    EditRefused, assigns, publish, read_text, service_snapshots, set_assignment, settings_lock, sourced, write_text,
)

SHARED = """# Shared defaults.
REPO="o/r"   # the repository
export FIX_MODEL_LOW=claude:sonnet:low  # cheap tier
# FIX_DISPATCH_CONCURRENCY=9  (a commented example is not an assignment)
DAILY_SPEND_CAP_USD=100
DAILY_SPEND_CAP_USD="${DAILY_SPEND_CAP_USD:-200}"
"""


def test_settings_saves_serialize_across_processes_and_release_on_close(tmp_path):
    (tmp_path / "config").mkdir()
    script = """
import sys
from issue_agent.adapters.config_files import settings_lock
print('ready', flush=True)
with settings_lock(sys.argv[1]):
    print('entered', flush=True)
"""
    child = None
    try:
        with settings_lock(str(tmp_path)):
            child = subprocess.Popen([sys.executable, "-u", "-c", script, str(tmp_path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
            assert not select.select([child.stdout], [], [], 0.1)[0]
        output, error = child.communicate(timeout=5)
        assert child.returncode == 0, error
        assert output.strip() == "entered"
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()


def test_a_settings_lock_refuses_a_symlink_without_touching_its_target(tmp_path):
    (tmp_path / "config").mkdir()
    target = tmp_path / "untouched"
    target.write_text("preserved")
    (tmp_path / "config" / ".settings.lock").symlink_to(target)
    with pytest.raises(OSError):
        with settings_lock(str(tmp_path)):
            pytest.fail("followed the symlink")
    assert target.read_text() == "preserved"


def test_a_replaced_value_keeps_comments_order_export_and_the_inline_comment():
    out = set_assignment(SHARED, "FIX_MODEL_LOW", "claude:opus:high")
    assert out.splitlines() == [
        "# Shared defaults.", 'REPO="o/r"   # the repository', "export FIX_MODEL_LOW=claude:opus:high  # cheap tier",
        "# FIX_DISPATCH_CONCURRENCY=9  (a commented example is not an assignment)",
        "DAILY_SPEND_CAP_USD=100", 'DAILY_SPEND_CAP_USD="${DAILY_SPEND_CAP_USD:-200}"',
    ]


def test_the_last_assignment_is_the_one_replaced_since_it_is_the_one_that_wins():
    out = set_assignment(SHARED, "DAILY_SPEND_CAP_USD", "50")
    assert out.splitlines()[-2:] == ["DAILY_SPEND_CAP_USD=100", "DAILY_SPEND_CAP_USD=50"]


def test_a_missing_key_is_appended_even_without_a_final_newline():
    assert set_assignment("A=1", "FIX_DISPATCH_CONCURRENCY", "3") == "A=1\nFIX_DISPATCH_CONCURRENCY=3\n"
    assert set_assignment("", "B", "x") == "B=x\n"
    # a commented example does not count as setting it
    assert set_assignment(SHARED, "FIX_DISPATCH_CONCURRENCY", "3").endswith("FIX_DISPATCH_CONCURRENCY=3\n")


def test_values_are_shell_quoted_and_round_trip_through_bash(tmp_path):
    path = tmp_path / "x.env"
    tricky = "it's $HOME `id` \"q\" ;rm"
    path.write_text(set_assignment(SHARED, "FIX_MODEL", tricky))
    env = sourced((str(path),), {"PATH": os.environ["PATH"]})
    assert env["FIX_MODEL"] == tricky
    assert env["FIX_MODEL_LOW"] == "claude:sonnet:low"


def test_unset_removes_every_top_level_assignment_and_nothing_else():
    out = set_assignment(SHARED, "DAILY_SPEND_CAP_USD", None)
    assert "DAILY_SPEND_CAP_USD" not in out
    assert out.count("\n") == SHARED.count("\n") - 2
    assert set_assignment(SHARED, "NOT_THERE", None) == SHARED


def test_crlf_line_endings_are_kept():
    assert set_assignment("A=1\r\nB=2\r\n", "A", "3") == "A=3\r\nB=2\r\n"


@pytest.mark.parametrize("text", [
    "if true; then\n  FIX_MODEL=x\nfi\n",          # inside a block: which branch wins is not the editor's call
    "FIX_MODEL=x; echo hi\n",                      # a second command on the line
    "FIX_MODEL=$(whoami)\n",                       # a substitution
    "FIX_MODEL='a\nb'\n",                          # a quoted value continuing onto the next line
    'FIX_MODEL="a\nb"\n',
    "FIX_MODEL=a \\\n  b\n",                       # a line continuation
    "FIX_MODEL=a trailing words\n",
])
def test_an_assignment_it_cannot_edit_safely_is_refused(text):
    with pytest.raises(EditRefused):
        set_assignment(text, "FIX_MODEL", "y")


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\0b", "x" * 1001])
def test_a_value_that_is_not_one_short_line_is_refused(value):
    with pytest.raises(EditRefused):
        set_assignment(SHARED, "FIX_MODEL", value)


@pytest.mark.parametrize("key", ["", "1A", "A-B", "A B", "A=B"])
def test_a_key_that_is_not_a_variable_name_is_refused(key):
    with pytest.raises(EditRefused):
        set_assignment(SHARED, key, "x")


def test_a_malformed_file_is_refused(tmp_path):
    with pytest.raises(EditRefused):
        set_assignment("A=1\0\n", "A", "2")
    binary = tmp_path / "bin.env"
    binary.write_bytes(b"A=\xff\xfe\n")
    with pytest.raises(EditRefused):
        read_text(str(binary))


def test_assigns_ignores_comments():
    assert assigns(SHARED, "REPO") and assigns(SHARED, "FIX_MODEL_LOW")
    assert not assigns(SHARED, "FIX_DISPATCH_CONCURRENCY")


def test_a_write_is_atomic_keeps_the_mode_and_goes_through_a_symlink(tmp_path):
    real = tmp_path / "real.env"
    real.write_text("A=1\n")
    real.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(real)
    write_text(str(link), "A=2\n", expected="A=1\n")
    assert link.is_symlink() and real.read_text() == "A=2\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_a_write_over_a_file_changed_since_it_was_read_is_refused(tmp_path):
    path = tmp_path / "x.env"
    path.write_text("A=1\n# a hand edit\n")
    with pytest.raises(EditRefused):
        write_text(str(path), "A=2\n", expected="A=1\n")
    assert path.read_text() == "A=1\n# a hand edit\n"


def test_publish_copies_to_matching_service_snapshots_only(tmp_path):
    env = {"PATH": os.environ["PATH"]}
    active = tmp_path / "active.env"
    active.write_text("REPO=o/r\nTARGET_DIR=/t\nFIX_DISPATCH_CONCURRENCY=3\n")
    home = tmp_path / "home"
    mine = home / ".config" / "issue-agent" / "demo.env"
    mine.parent.mkdir(parents=True)
    mine.write_text("REPO=o/r\nTARGET_DIR=/t\nFIX_DISPATCH_CONCURRENCY=1\n")
    assert service_snapshots("demo", str(home)) == (str(mine),)
    assert publish(str(active), (str(mine),), env) == []
    assert mine.read_text() == active.read_text() and stat.S_IMODE(mine.stat().st_mode) == 0o600

    mine.write_text("REPO=o/other\nTARGET_DIR=/t\n")
    problems = publish(str(active), (str(mine),), env)
    assert problems and "another REPO" in problems[0]
    assert mine.read_text() == "REPO=o/other\nTARGET_DIR=/t\n"


def test_no_service_snapshot_means_nothing_to_publish(tmp_path):
    assert service_snapshots("demo", str(tmp_path)) == ()
