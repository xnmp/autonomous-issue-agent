from scripts.benchmark_control_plane import run


def test_control_plane_benchmark_is_executable_and_reports_capacity() -> None:
    result = run(3)

    assert result["iterations"] == 3
    assert result["admission_release_p95_ms"] >= 0
    assert result["outbox_publish_p95_ms"] >= 0
    assert result["projection_rebuild_ms"] >= 0
    assert result["projection_event_count"] == 6
    assert result["exhausted_admission_p95_ms"] >= 0
    assert result["unavailable_admission_p95_ms"] >= 0
    assert result["exhausted_tick_p95_ms"] >= 0
    assert result["unavailable_tick_p95_ms"] >= 0
    assert result["active_child_shutdown_ms"] >= 0
    assert result["control_db_bytes"] > 0
    assert result["control_db_growth_bytes_per_transition"] >= 0
    assert result["event_log_bytes"] > 0
    assert result["event_log_bytes_per_transition"] > 0
