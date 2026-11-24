"""Regression tests for separated HTTP, functional and performance health."""

from __future__ import annotations

import httpx

from tests.conftest import json_response
from tests.helpers import make_monitor
from ximiowatch.config import Assertion, load_monitors
from ximiowatch.engine import MonitorEngine, check_monitor


def total_checks(registry) -> float:
    return sum(
        sample.value
        for metric in registry.collect()
        for sample in metric.samples
        if sample.name == "ximiowatch_checks_total"
    )


async def test_fiscal_monitor_rejects_missing_authorized_field(metrics, registry):
    monitor = next(
        item
        for item in load_monitors("config/monitors.yaml")
        if item.name == "fiscal_authorization"
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: json_response({"invoice_id": "INV-DEMO-2041"})
        )
    ) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.http_ok is True
    assert result.functional_ok is False
    assert "json_equals" in result.business_failed_types
    assert result.silent_failure is True
    assert (
        registry.get_sample_value(
            "ximiowatch_silent_failures_total",
            {"monitor": monitor.name, "application": monitor.application},
        )
        == 1.0
    )


async def test_http_500_with_valid_json_is_not_functionally_healthy(metrics, registry):
    monitor = make_monitor(assertions=[Assertion(type="json_equals", path="ok", value=True)])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response({"ok": True}, status_code=500))
    ) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.http_ok is False
    assert result.functional_ok is False
    assert result.silent_failure is False
    assert result.outcome == "http_failure"
    assert (
        registry.get_sample_value(
            "ximiowatch_functional_up", {"monitor": "monitor-a", "application": "app-a"}
        )
        == 0.0
    )


async def test_latency_only_failure_does_not_increment_silent_failures(metrics, registry):
    monitor = make_monitor(
        assertions=[
            Assertion(type="http_status", value=200),
            Assertion(type="json_equals", path="ok", value=True),
            Assertion(type="max_response_time", value=0.0),
        ]
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response({"ok": True}))
    ) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.http_ok is True
    assert result.functional_ok is True
    assert result.performance_ok is False
    assert result.silent_failure is False
    assert result.outcome == "performance_failure"
    assert (
        registry.get_sample_value(
            "ximiowatch_performance_up", {"monitor": "monitor-a", "application": "app-a"}
        )
        == 0.0
    )
    assert (
        registry.get_sample_value(
            "ximiowatch_functional_up", {"monitor": "monitor-a", "application": "app-a"}
        )
        == 1.0
    )
    assert (
        registry.get_sample_value(
            "ximiowatch_silent_failures_total",
            {"monitor": "monitor-a", "application": "app-a"},
        )
        is None
    )


async def test_combined_failure_counts_http_and_functional_separately(metrics, registry):
    monitor = make_monitor(
        assertions=[
            Assertion(type="http_status", value=200),
            Assertion(type="json_equals", path="ok", value=True),
            Assertion(type="max_response_time", value=0.0),
        ]
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response({"ok": False}, status_code=500))
    ) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.http_ok is False
    assert result.functional_ok is False
    assert result.performance_ok is False
    assert result.silent_failure is False
    assert result.outcome == "http_failure"
    assert (
        registry.get_sample_value(
            "ximiowatch_silent_failures_total",
            {"monitor": "monitor-a", "application": "app-a"},
        )
        is None
    )


async def test_each_engine_check_records_metrics_exactly_once(metrics, registry):
    monitor = make_monitor(assertions=[Assertion(type="json_equals", path="ok", value=True)])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response({"ok": True}))
    ) as client:
        engine = MonitorEngine([monitor], metrics, client=client)
        await engine.check_all()
        assert total_checks(registry) == 1.0
        await engine.check_all()
        assert total_checks(registry) == 2.0


async def test_last_check_timestamp_advances_on_failed_check(metrics, registry):
    monitor = make_monitor(timeout_seconds=0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = MonitorEngine([monitor], metrics, client=client)
        await engine.check_all()

    assert (
        registry.get_sample_value(
            "ximiowatch_last_check_timestamp_seconds",
            {"monitor": "monitor-a", "application": "app-a"},
        )
        > 0
    )


async def test_performance_is_unknown_without_threshold_or_valid_measurement(metrics, registry):
    no_threshold = make_monitor()
    with_threshold = make_monitor(
        name="slow",
        application="app-slow",
        url="http://demo.test/slow",
        assertions=[Assertion(type="max_response_time", value=1)],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/slow"):
            raise httpx.ConnectError("simulated connection error", request=request)
        return json_response({"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = MonitorEngine([no_threshold, with_threshold], metrics, client=client)
        await engine.check_all()

    assert (
        registry.get_sample_value(
            "ximiowatch_performance_configured",
            {"monitor": "monitor-a", "application": "app-a"},
        )
        == 0.0
    )
    assert (
        registry.get_sample_value(
            "ximiowatch_performance_up", {"monitor": "monitor-a", "application": "app-a"}
        )
        is None
    )
    assert (
        registry.get_sample_value(
            "ximiowatch_performance_configured",
            {"monitor": "slow", "application": "app-slow"},
        )
        == 1.0
    )
    assert (
        registry.get_sample_value(
            "ximiowatch_performance_up", {"monitor": "slow", "application": "app-slow"}
        )
        == 0.0
    )
