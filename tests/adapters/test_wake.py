"""Wake signal — the cross-process edge a webhook delivery turns into."""

import os

from issue_agent.adapters.wake import WakeSignal, touch_wake, wake_path


def test_a_touch_wakes_exactly_once(tmp_path):
    """Consuming semantics: the delivery that arrived is worth one early tick,
    and the tick after it must sleep normally rather than spin."""
    p = str(tmp_path / "wake")
    sig = WakeSignal(p)
    assert sig.pending() is False
    touch_wake(p)
    assert sig.pending() is True
    assert sig.pending() is False


def test_every_touch_is_its_own_edge(tmp_path):
    """Two deliveries inside one clock granule must not collapse into one wake —
    the second would otherwise wait a full reconcile interval."""
    p = str(tmp_path / "wake")
    sig = WakeSignal(p)
    for _ in range(5):
        touch_wake(p)
        assert sig.pending() is True


def test_a_missing_wake_file_is_never_pending(tmp_path):
    sig = WakeSignal(str(tmp_path / "never-written"))
    assert sig.pending() is False
    assert sig.pending() is False


def test_a_wake_left_over_from_a_previous_run_does_not_fire(tmp_path):
    """The file outlives the daemon; a restart must not inherit a phantom
    delivery (harmless but it would mask a genuinely dead forwarder)."""
    p = str(tmp_path / "wake")
    touch_wake(p)
    assert WakeSignal(p).pending() is False


def test_touch_creates_missing_parent_dirs(tmp_path):
    """The forwarder can start before a role daemon has made its STATE_DIR."""
    p = wake_path(str(tmp_path / "state" / "fix"))
    touch_wake(p)
    assert os.path.exists(p)
    assert WakeSignal(str(tmp_path / "state" / "fix" / "wake")).path == p


def test_an_unwritable_path_does_not_raise(tmp_path):
    """A wake that cannot be delivered degrades to the reconcile cadence — it
    must never take the forwarder down."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    touch_wake(str(blocker / "sub" / "wake"))  # must not raise
    assert WakeSignal(str(blocker / "sub" / "wake")).pending() is False
