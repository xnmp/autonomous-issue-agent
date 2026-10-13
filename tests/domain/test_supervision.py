from issue_agent.domain.supervision import ClockSample, DaemonState, ExitCode, suspension_between


def test_exit_taxonomy_is_stable() -> None:
    assert ExitCode.CLEAN == 0
    assert ExitCode.DUPLICATE_INSTANCE == 3
    assert ExitCode.RELOAD == 87
    assert tuple(DaemonState) == (DaemonState.STARTING, DaemonState.RUNNING, DaemonState.DRAINING, DaemonState.STOPPED)


def test_suspension_is_wall_time_not_explained_by_active_time() -> None:
    result = suspension_between(ClockSample(100, 20), ClockSample(180, 35))
    assert result.suspended_seconds == 65


def test_clock_adjustments_never_produce_negative_suspension() -> None:
    result = suspension_between(ClockSample(100, 20), ClockSample(90, 25))
    assert result.suspended_seconds == 0
