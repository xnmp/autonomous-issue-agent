"""Suite-wide environment sanitization (the #63 class, python tier).

The bash suites unset the deployment knobs at their top; the pytest tiers had
no equivalent, so a developer's (or a self-hosted fixer's) exported LINT_CMD /
TYPECHECK_CMD / GH_APP_ID flipped adapter-test verdicts — verified live on
2026-07-29. One source of truth for the list: the scrub the gate itself applies.

HARNESS_GATE_DEPTH is deliberately NOT cleared: it is the fork-bomb
re-entrancy counter, and clearing it inside a nested (gate-launched) suite run
would re-arm the bomb.
"""

import os
import signal

import pytest

from issue_agent.adapters.governor import target_cmd_scrub_set

for _knob in target_cmd_scrub_set():
    os.environ.pop(_knob, None)


_GUARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@pytest.fixture(autouse=True)
def _process_signal_handlers_are_restored(request):
    """A handler leaked into an xdist worker hid a stray SIGTERM as SystemExit(0)
    on the native CI runner (#249); fail the leaking test, not a later victim."""
    before = {signum: signal.getsignal(signum) for signum in _GUARDED_SIGNALS}
    yield
    leaked = [
        signal.Signals(signum).name
        for signum, handler in before.items()
        if signal.getsignal(signum) is not handler
    ]
    for signum, handler in before.items():
        if handler is not None:
            signal.signal(signum, handler)
    if leaked:
        pytest.fail(f"{request.node.nodeid} left process handlers changed: {', '.join(leaked)}")
