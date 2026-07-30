from __future__ import annotations

import issue_agent.adapters.systemd_notify as systemd_notify


def test_notify_sends_expected_datagram(monkeypatch):
    calls = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def connect(self, address):
            calls.append(("connect", address))

        def sendall(self, payload):
            calls.append(("sendall", payload))

    monkeypatch.setattr(systemd_notify.socket, "socket", lambda *_args: FakeSocket())
    assert systemd_notify.notify(
        "READY=1\nSTATUS=ready", {"NOTIFY_SOCKET": "/run/notify.sock"}
    )
    assert calls == [
        ("connect", "/run/notify.sock"),
        ("sendall", b"READY=1\nSTATUS=ready"),
    ]


def test_notify_is_optional_and_fail_soft(tmp_path):
    assert systemd_notify.notify("READY=1", {}) is False
    assert systemd_notify.notify(
        "READY=1", {"NOTIFY_SOCKET": str(tmp_path / "missing.sock")}
    ) is False


def test_watchdog_is_rate_limited_and_only_sent_on_scheduler_progress(
    monkeypatch,
):
    sent = []
    current = [10.0]
    monkeypatch.setattr(
        systemd_notify,
        "notify",
        lambda message, _env: sent.append(message) or True,
    )
    notifier = systemd_notify.SystemdNotifier(
        {"NOTIFY_SOCKET": "/run/notify.sock", "WATCHDOG_USEC": "4000000"},
        monotonic=lambda: current[0],
    )

    notifier.ready("ready")
    current[0] = 11.9
    notifier.progress("tick")
    current[0] = 12.0
    notifier.progress("tick")
    notifier.stopping("done")

    assert sent == [
        "READY=1\nSTATUS=ready",
        "WATCHDOG=1\nSTATUS=tick",
        "STOPPING=1\nSTATUS=done",
    ]
