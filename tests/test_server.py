"""The metrics endpoint must serve Prometheus text at the exact /metrics path."""

from __future__ import annotations

from fastapi.testclient import TestClient

from ximiowatch.main import app


def test_metrics_endpoint_returns_prometheus_text_without_redirect():
    client = TestClient(app)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "ximiowatch_http_up" in response.text
    assert "ximiowatch_functional_up" in response.text
