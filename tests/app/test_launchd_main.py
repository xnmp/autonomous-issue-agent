import pytest

from issue_agent.app.launchd_main import run
from issue_agent.domain.supervision import ExitCode


@pytest.mark.parametrize(
    "code",
    (
        ExitCode.INVALID_CONFIG,
        ExitCode.DUPLICATE_INSTANCE,
        ExitCode.INCOMPATIBLE_SCHEMA,
        ExitCode.STORE_CORRUPTION,
        ExitCode.OPERATOR_REPAIR_REQUIRED,
    ),
)
def test_launchd_maps_permanent_failures_to_success(code):
    run(lambda: (_ for _ in ()).throw(SystemExit(code)))


@pytest.mark.parametrize(
    "code", (ExitCode.TRANSIENT, ExitCode.RELOAD)
)
def test_launchd_preserves_restartable_failures(code):
    with pytest.raises(SystemExit) as stopped:
        run(lambda: (_ for _ in ()).throw(SystemExit(code)))
    assert stopped.value.code is code
