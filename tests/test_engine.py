"""Monitor isolation and consecutive-failure tracking tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import httpx

from tests.conftest import json_response
from tests.helpers import make_monitor
from ximiowatch.config import Assertion
from ximiowatch.engine import MonitorEngine


async def test_failed_monitor_does_not_interrupt_others(metrics, registry):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/broken":
            raise httpx.TimeoutException("simulated timeout", request=request)
        return json_response({"ready": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        broken = make_monitor(
            name="broken", application="app-broken", url="http://demo.test/broken"
        )
        healthy = make_monitor(
            name="healthy",
            application="app-healthy",
            url="http://demo.test/healthy",
            assertions=[Assertion(type="json_equals", path="ready", value=True)],
        )
        engine = MonitorEngine([broken, healthy], metrics, client=client)
        results = await engine.check_all()

    by_name = {result.monitor: result for result in results}
    assert set(by_name) == {"broken", "healthy"}
    assert by_name["broken"].outcome == "timeout"
    assert by_name["healthy"].outcome == "success"
    assert (
        registry.get_sample_value(
            "ximiowatch_functional_up", {"monitor": "healthy", "application": "app-healthy"}
        )
        == 1.0
    )
    assert (
        registry.get_sample_value(
            "ximiowatch_functional_up", {"monitor": "broken", "application": "app-broken"}
        )
        == 0.0
    )


async def test_one_broken_loop_does_not_stop_other_loops(metrics):
    counts = {"broken": 0, "healthy": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        name = "broken" if request.url.path == "/broken" else "healthy"
        counts[name] += 1
        if name == "broken":
            raise httpx.ConnectError("simulated connection error", request=request)
        return json_response({"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        broken = make_monitor(
            name="broken", url="http://demo.test/broken", interval_seconds=0.01
        )
        healthy = make_monitor(
            name="healthy", url="http://demo.test/healthy", interval_seconds=0.01
        )
        engine = MonitorEngine([broken, healthy], metrics, client=client)
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.15)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert counts["broken"] >= 1
    assert counts["healthy"] >= 1


async def test_consecutive_failures_reset_after_recovery(metrics, registry):
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"products": [] if state["fail"] else [{"id": "sku-1"}]})

    monitor = make_monitor(
        assertions=[
            Assertion(type="json_exists", path="products"),
            Assertion(type="json_not_empty", path="products"),
        ]
    )
    labels = {"monitor": "monitor-a", "application": "app-a"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        engine = MonitorEngine([monitor], metrics, client=client)
        await engine.check_all()
        await engine.check_all()
        assert (
            registry.get_sample_value("ximiowatch_consecutive_failures", labels) == 2.0
        )

        state["fail"] = False
        await engine.check_all()
        assert (
            registry.get_sample_value("ximiowatch_consecutive_failures", labels) == 0.0
        )
