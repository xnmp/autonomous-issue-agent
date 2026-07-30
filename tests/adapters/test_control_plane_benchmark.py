from scripts.benchmark_control_plane import run


def test_control_plane_benchmark_is_executable_and_reports_capacity() -> None:
    result = run(3)

    assert result["iterations"] == 3
    assert result["admission_release_p95_ms"] >= 0
    assert result["control_db_bytes"] > 0
    assert result["event_log_bytes"] > 0
