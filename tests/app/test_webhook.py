"""Webhook forwarder — deliveries become wakes, and nothing else."""

import io

from issue_agent.adapters.single_instance import acquire
from issue_agent.adapters.wake import WakeSignal, wake_path
from issue_agent.app.webhook import DEFAULT_EVENTS, forward_command, gc_dangling_hooks, run


class FakeGhRun:
    """Records `gh api` argvs; serves a canned relay-hook id list. Unit tests
    must never reach the real gh — run() GCs relay hooks before spawning."""

    def __init__(self, hook_ids=()):
        self.calls = []
        self._ids = hook_ids

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = "".join(f"{i}\n" for i in self._ids)

        class R:
            stdout = out
        return R


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
    run(env, spawn=lambda *a, **k: FakeProc(['{"action":"opened"}\n']),
        gh_run=FakeGhRun())
    assert [s.pending() for s in sigs] == [True, True, True]


def test_the_payload_is_never_parsed(tmp_path):
    """A wake is an edge, not data: garbage that no JSON parser would accept
    must still wake the daemons, because the daemon rebuilds the world anyway."""
    env = _env(tmp_path)
    sig = WakeSignal(wake_path(env["STATE_DIR"]))
    run(env, spawn=lambda *a, **k: FakeProc(["not json at all\n"]),
        gh_run=FakeGhRun())
    assert sig.pending() is True


def test_blank_lines_are_not_deliveries(tmp_path):
    env = _env(tmp_path)
    sig = WakeSignal(wake_path(env["STATE_DIR"]))
    run(env, spawn=lambda *a, **k: FakeProc(["\n", "   \n"]), gh_run=FakeGhRun())
    assert sig.pending() is False


def test_a_forwarder_death_is_reported_as_a_failure(tmp_path, capsys):
    """point-at restarts on any exit, so a clean-looking 0 must not read as
    success — and the operator needs the reason in the log."""
    rc = run(_env(tmp_path), spawn=lambda *a, **k: FakeProc([], rc=0),
             gh_run=FakeGhRun())
    assert rc != 0
    assert "EXITED" in capsys.readouterr().err


def test_a_missing_gh_extension_says_how_to_fix_it(tmp_path, capsys):
    def boom(*a, **k):
        raise OSError("No such file or directory: 'gh'")

    rc = run(_env(tmp_path), spawn=boom, gh_run=FakeGhRun())
    assert rc == 127
    err = capsys.readouterr().err
    assert "gh extension install" in err and "admin:repo_hook" in err


def test_an_unset_repo_refuses_to_start(tmp_path, capsys):
    started = []
    rc = run({"REPO": ""}, spawn=lambda *a, **k: started.append(a) or FakeProc([]),
             gh_run=FakeGhRun())
    assert rc == 2 and started == []
    assert "REPO is unset" in capsys.readouterr().err


def test_forward_command_subscribes_to_the_events_that_move_decisions():
    cmd = forward_command("o/r", DEFAULT_EVENTS)
    assert cmd[:3] == ["gh", "webhook", "forward"]
    assert "--repo=o/r" in cmd
    for ev in ("issues", "pull_request", "issue_comment",
               "pull_request_review", "check_suite", "status"):
        assert ev in DEFAULT_EVENTS


def test_dangling_relay_hooks_are_deleted_before_spawning(tmp_path):
    """`gh webhook forward` creates a NEW `name:"cli"` hook on the target repo
    every invocation and never deletes it — the supervisor's restart loop would
    accrete active hooks until GitHub's per-repo cap kills push wake for good
    (review F4). Every start therefore GCs the relics first."""
    gh = FakeGhRun(hook_ids=(111, 222))
    run(_env(tmp_path), spawn=lambda *a, **k: FakeProc([]), gh_run=gh)
    deletes = [c for c in gh.calls if "-X" in c and "DELETE" in c]
    assert [c[-1] for c in deletes] == ["repos/o/r/hooks/111", "repos/o/r/hooks/222"]
    # the list call selects ONLY relay hooks — a real webhook (name "web") is
    # never in scope
    assert any('select(.name == "cli")' in " ".join(c) for c in gh.calls)


def test_hook_gc_failure_never_stops_the_forwarder(tmp_path, capsys):
    def boom(argv, **kw):
        raise OSError("network down")

    sig = WakeSignal(wake_path(_env(tmp_path)["STATE_DIR"]))
    run(_env(tmp_path), spawn=lambda *a, **k: FakeProc(["x\n"]), gh_run=boom)
    assert sig.pending() is True
    assert "GC failed" in capsys.readouterr().err


def test_gc_deletes_nothing_when_no_relics_exist():
    gh = FakeGhRun(hook_ids=())
    gc_dangling_hooks("o/r", gh)
    assert not any("DELETE" in c for c in gh.calls)


def test_second_forwarder_cannot_gc_or_spawn(tmp_path):
    env = _env(tmp_path, TARGET_DIR=str(tmp_path / "checkout"))
    held = acquire(
        "webhook",
        "o/r",
        target_dir=env["TARGET_DIR"],
        state_dir=env["STATE_DIR"],
    )
    started = []
    gh = FakeGhRun(hook_ids=(111,))
    try:
        rc = run(
            env,
            spawn=lambda *a, **k: started.append(a) or FakeProc([]),
            gh_run=gh,
        )
    finally:
        held.release()
    assert rc == 3
    assert started == []
    assert gh.calls == []
