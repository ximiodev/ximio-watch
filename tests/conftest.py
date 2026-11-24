"""Pytest fixtures with isolated Prometheus registries and no network access."""

from __future__ import annotations

import httpx
import pytest
from prometheus_client import CollectorRegistry

from ximiowatch.metrics import Metrics


@pytest.fixture
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry) -> Metrics:
    return Metrics(registry=registry)


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "http://demo.test/check"),
    )
