"""Regression tests for the process startup timestamp used by freshness rules."""

from __future__ import annotations

import time

import httpx
from prometheus_client import CollectorRegistry

from tests.conftest import json_response
from ximiowatch.engine import MonitorEngine
from ximiowatch.metrics import Metrics
from tests.helpers import make_monitor


def test_startup_metric_is_global_and_bounded(registry: CollectorRegistry):
    before = time.time()
    metrics = Metrics(registry=registry)
    after = time.time()

    value = registry.get_sample_value("ximiowatch_startup_timestamp_seconds")
    assert value is not None
    assert before <= value <= after


def test_startup_metric_has_no_monitor_labels(registry: CollectorRegistry):
    metrics = Metrics(registry=registry)
    samples = [
        sample
        for metric in registry.collect()
        for sample in metric.samples
        if sample.name == "ximiowatch_startup_timestamp_seconds"
    ]

    assert len(samples) == 1
    assert samples[0].labels == {}


async def test_startup_metric_is_not_changed_by_monitor_checks(metrics: Metrics, registry):
    engine = MonitorEngine(
        [make_monitor()],
        metrics,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: json_response({"ok": True}))
        ),
    )
    initial = registry.get_sample_value("ximiowatch_startup_timestamp_seconds")

    await engine.check_all()
    await engine.aclose()

    assert registry.get_sample_value("ximiowatch_startup_timestamp_seconds") == initial
