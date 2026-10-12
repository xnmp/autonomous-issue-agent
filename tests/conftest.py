"""Suite-wide environment sanitization (the #63 class, python tier).

The bash suites unset the deployment knobs at their top; the pytest tiers had
no equivalent, so a developer's (or a self-hosted fixer's) exported deployment
knobs flipped adapter-test verdicts — verified live on 2026-07-29. The
composed deployment also names every knob it set in HARNESS_DEPLOY_KNOBS.
"""

import os
import signal

import pytest

_DEPLOYMENT_KNOBS = (
    "BASE_BRANCH", "GH_APP_ID", "GH_APP_KEY_FILE", "GH_APP_INSTALL_ID",
    "TARGET_DIR", "AGENT_ENV", "ISSUE_AGENT_DEPLOYMENT_ID",
)
for _knob in (*_DEPLOYMENT_KNOBS, *os.environ.get("HARNESS_DEPLOY_KNOBS", "").split(), "HARNESS_DEPLOY_KNOBS"):
    os.environ.pop(_knob, None)


_GUARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory):
    # Runtime fakes must not discover operator rollouts or login credentials.
    home = tmp_path_factory.mktemp("home")
    # Sharing the monkeypatch fixture moves its teardown after signal guards.
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HOME", str(home))
        patch.setenv("CODEX_HOME", str(home / ".codex"))
        yield


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
