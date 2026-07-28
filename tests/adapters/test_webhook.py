"""Webhook forwarder — deliveries become wakes, and nothing else."""

import io

from issue_agent.adapters.wake import WakeSignal, wake_path
from issue_agent.app.webhook import DEFAULT_EVENTS, forward_command, run


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = io.StringIO("".join(lines))
        self._rc = rc

    def wait(self):
        return self._rc


def _env(tmp_path, **extra):
    env = {
        "REPO": "o/r",
        "STATE_DIR": str(tmp_path / "fix"),
        "REVIEW_STATE_DIR": str(tmp_path / "review"),
        "STEWARD_STATE_DIR": str(tmp_path / "steward"),
    }
    env.update(extra)
    return env


def test_every_delivery_wakes_every_role(tmp_path):
    env = _env(tmp_path)
    sigs = [
        WakeSignal(wake_path(env["STATE_DIR"])),
        WakeSignal(wake_path(env["REVIEW_STATE_DIR"])),
        WakeSignal(wake_path(env["STEWARD_STATE_DIR"])),
    ]
    run(env, spawn=lambda *a, **k: FakeProc(['{"action":"opened"}\n']))
    assert [s.pending() for s in sigs] == [True, True, True]


def test_the_payload_is_never_parsed(tmp_path):
    """A wake is an edge, not data: garbage that no JSON parser would accept
    must still wake the daemons, because the daemon rebuilds the world anyway."""
    env = _env(tmp_path)
    sig = WakeSignal(wake_path(env["STATE_DIR"]))
    run(env, spawn=lambda *a, **k: FakeProc(["not json at all\n"]))
    assert sig.pending() is True


def test_blank_lines_are_not_deliveries(tmp_path):
    env = _env(tmp_path)
    sig = WakeSignal(wake_path(env["STATE_DIR"]))
    run(env, spawn=lambda *a, **k: FakeProc(["\n", "   \n"]))
    assert sig.pending() is False


def test_a_forwarder_death_is_reported_as_a_failure(tmp_path, capsys):
    """point-at restarts on any exit, so a clean-looking 0 must not read as
    success — and the operator needs the reason in the log."""
    rc = run(_env(tmp_path), spawn=lambda *a, **k: FakeProc([], rc=0))
    assert rc != 0
    assert "EXITED" in capsys.readouterr().err


def test_a_missing_gh_extension_says_how_to_fix_it(tmp_path, capsys):
    def boom(*a, **k):
        raise OSError("No such file or directory: 'gh'")

    rc = run(_env(tmp_path), spawn=boom)
    assert rc == 127
    err = capsys.readouterr().err
    assert "gh extension install" in err and "admin:repo_hook" in err


def test_an_unset_repo_refuses_to_start(tmp_path, capsys):
    started = []
    rc = run({"REPO": ""}, spawn=lambda *a, **k: started.append(a) or FakeProc([]))
    assert rc == 2 and started == []
    assert "REPO is unset" in capsys.readouterr().err


def test_forward_command_subscribes_to_the_events_that_move_decisions():
    cmd = forward_command("o/r", DEFAULT_EVENTS)
    assert cmd[:3] == ["gh", "webhook", "forward"]
    assert "--repo=o/r" in cmd
    for ev in ("issues", "pull_request", "issue_comment",
               "pull_request_review", "check_suite", "status"):
        assert ev in DEFAULT_EVENTS
