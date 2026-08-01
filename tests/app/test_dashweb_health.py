"""Public health-check contract for the dashboard HTTP backend."""

import pytest

pytest.importorskip("fastapi")

from asgi_client import ASGITestClient as TestClient  # noqa: E402
from issue_agent.app.dashweb import server  # noqa: E402


def test_health_endpoint_returns_an_ok_response():
    """A loopback caller can verify that the dashboard HTTP service is available."""
    client = TestClient(server.create_app(), base_url="http://127.0.0.1:8321")

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
