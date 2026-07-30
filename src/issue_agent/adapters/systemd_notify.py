"""Minimal sd_notify client with bounded watchdog heartbeats."""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable


def notify(message: str, env: dict[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    address = values.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.connect(address)
            client.sendall(message.encode())
        return True
    except OSError:
        return False


class SystemdNotifier:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._env = dict(os.environ if env is None else env)
        self._monotonic = monotonic
        raw = self._env.get("WATCHDOG_USEC", "")
        try:
            usec = int(raw)
        except ValueError:
            usec = 0
        self._interval = max(1.0, usec / 2_000_000) if usec > 0 else 0.0
        self._last_watchdog = self._monotonic()

    def ready(self, status: str) -> None:
        notify(f"READY=1\nSTATUS={status}", self._env)

    def status(self, status: str) -> None:
        notify(f"STATUS={status}", self._env)

    def progress(self, status: str = "") -> None:
        """Rate-limit scheduler liveness, including bounded inter-tick waits."""

        if not self._interval:
            return
        current = self._monotonic()
        if current - self._last_watchdog < self._interval:
            return
        message = "WATCHDOG=1"
        if status:
            message += f"\nSTATUS={status}"
        notify(message, self._env)
        self._last_watchdog = current

    def stopping(self, status: str) -> None:
        notify(f"STOPPING=1\nSTATUS={status}", self._env)
