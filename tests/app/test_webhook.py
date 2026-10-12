"""Webhook forwarder — deliveries become wakes, and nothing else."""

import io

from issue_agent.adapters.single_instance import acquire
from issue_agent.adapters.wake import WakeSignal, wake_path
from issue_agent.app.webhook import DEFAULT_EVENTS, run


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = io.StringIO("".join(lines))
        self._rc = rc

    def wait(self):
        return self._rc


class FakeEffects:
    def __init__(self, fail=None):
        self.fail = fail
        self.events = []

    def start_relay_forward(self, events, *, spawn):
        self.events.append(events)
        if self.fail is not None:
            raise self.fail
        return spawn(
            ["durably-recorded-forwarder"],
            stdout=None,
            stderr=None,
            text=True,
            bufsize=1,
        )


def _effects_factory(effects):
    return lambda *_args, **_kwargs: effects


def _env(tmp_path, **extra):
    env = {
        "REPO": "o/r",
        "TARGET_DIR": str(tmp_path / "checkout"),
        "STATE_DIR": str(tmp_path / "fix"),
        "REVIEW_STATE_DIR": str(tmp_path / "review"),
    }
    env.update(extra)
    return env


def test_every_delivery_wakes_every_role(tmp_path):
    env = _env(tmp_path)
    sigs = [
        WakeSignal(wake_path(env["STATE_DIR"])),
        WakeSignal(wake_path(env["REVIEW_STATE_DIR"])),
    ]
    effects = FakeEffects()
    run(
        env,
        spawn=lambda *a, **k: FakeProc(['{"action":"opened"}\n']),
        effects_factory=_effects_factory(effects),
    )
    assert [s.pending() for s in sigs] == [True, True]
    assert effects.events == [DEFAULT_EVENTS]


def test_the_payload_is_never_parsed(tmp_path):
    """A wake is an edge, not data: garbage that no JSON parser would accept
    must still wake the daemons, because the daemon rebuilds the world anyway."""
    env = _env(tmp_path)
    sig = WakeSignal(wake_path(env["STATE_DIR"]))
    run(
        env,
        spawn=lambda *a, **k: FakeProc(["not json at all\n"]),
        effects_factory=_effects_factory(FakeEffects()),
    )
    assert sig.pending() is True


def test_blank_lines_are_not_deliveries(tmp_path):
    env = _env(tmp_path)
    sig = WakeSignal(wake_path(env["STATE_DIR"]))
    run(
        env,
        spawn=lambda *a, **k: FakeProc(["\n", "   \n"]),
        effects_factory=_effects_factory(FakeEffects()),
    )
    assert sig.pending() is False


def test_a_forwarder_death_is_reported_as_a_failure(tmp_path, capsys):
    """point-at restarts on any exit, so a clean-looking 0 must not read as
    success — and the operator needs the reason in the log."""
    rc = run(
        _env(tmp_path),
        spawn=lambda *a, **k: FakeProc([], rc=0),
        effects_factory=_effects_factory(FakeEffects()),
    )
    assert rc != 0
    assert "EXITED" in capsys.readouterr().err


def test_a_missing_gh_extension_says_how_to_fix_it(tmp_path, capsys):
    def boom(*a, **k):
        raise OSError("No such file or directory: 'gh'")

    rc = run(
        _env(tmp_path),
        spawn=boom,
        effects_factory=_effects_factory(FakeEffects()),
    )
    assert rc == 127
    err = capsys.readouterr().err
    assert "gh extension install" in err and "admin:repo_hook" in err


def test_an_unset_repo_refuses_to_start(tmp_path, capsys):
    started = []
    rc = run(
        {"REPO": ""},
        spawn=lambda *a, **k: started.append(a) or FakeProc([]),
        effects_factory=_effects_factory(FakeEffects()),
    )
    assert rc == 2 and started == []
    assert "REPO is unset" in capsys.readouterr().err


def test_forwarder_subscribes_to_the_events_that_move_decisions():
    for ev in ("issues", "pull_request", "issue_comment",
               "pull_request_review", "check_suite", "status"):
        assert ev in DEFAULT_EVENTS


def test_unreconciled_relay_effect_refuses_to_spawn(tmp_path, capsys):
    started = []
    rc = run(
        _env(tmp_path),
        spawn=lambda *a, **k: started.append(a) or FakeProc(["x\n"]),
        effects_factory=_effects_factory(FakeEffects(OSError("network down"))),
    )
    assert rc == 127
    assert started == []
    assert "durable relay forwarder" in capsys.readouterr().err


def test_second_forwarder_cannot_gc_or_spawn(tmp_path):
    env = _env(tmp_path, TARGET_DIR=str(tmp_path / "checkout"))
    held = acquire(
        "webhook",
        "o/r",
        target_dir=env["TARGET_DIR"],
        state_dir=env["STATE_DIR"],
    )
    started = []
    try:
        rc = run(
            env,
            spawn=lambda *a, **k: started.append(a) or FakeProc([]),
            effects_factory=_effects_factory(FakeEffects()),
        )
    finally:
        held.release()
    assert rc == 3
    assert started == []
