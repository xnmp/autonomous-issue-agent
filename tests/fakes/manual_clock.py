"""Hand-driven monotonic clock for tests that must own deadline expiry.

The clock sets a Daemon's shutdown budget, but its wait() windows are still
real time. Give the worker-unwind window seconds (shutdown timeout 30) and
close a window by advancing the clock in the hook that ends it.
"""


class ManualClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def set(self, now: float) -> None:
        self.now = now
