"""Background startup, initial unknown state and graceful shutdown."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from prometheus_client import CollectorRegistry

from tests.helpers import make_monitor
from ximiowatch import main
from ximiowatch.engine import MonitorEngine
from ximiowatch.metrics import Metrics

LABELS = {"monitor": "monitor-a", "application": "app-a"}


async def test_startup_does_not_wait_for_first_monitor_check(monkeypatch):
    calls: list[object] = []
    gate = asyncio.Event()
    monitors = [make_monitor()]
    registry = CollectorRegistry()
    fresh_metrics = Metrics(registry=registry)

    monkeypatch.setattr(main, "metrics", fresh_metrics)
    monkeypatch.setattr(main, "load_monitors", lambda path: monitors)
    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "setup_tracing", lambda: None)

    class FakeEngine:
        def __init__(self, configured, metrics):
            calls.append("init")
            metrics.configure(configured)
            self.monitors = configured

        async def run(self, *, immediate: bool):
            calls.append(("run", immediate))
            await gate.wait()

        async def aclose(self):
            calls.append("close")

    monkeypatch.setattr(main, "MonitorEngine", FakeEngine)

    async with main.lifespan(main.app):
        await asyncio.sleep(0)
        assert calls == ["init", ("run", True)]
        assert main.app.state.monitors == monitors
        assert main._task is not None
        assert not main._task.done()

        # Configuration metadata exists, but no health observation has been recorded.
        assert registry.get_sample_value("ximiowatch_monitor_interval_seconds", LABELS) == 10.0
        assert registry.get_sample_value("ximiowatch_monitor_timeout_seconds", LABELS) == 2.0
        assert registry.get_sample_value("ximiowatch_http_up", LABELS) is None
        assert registry.get_sample_value("ximiowatch_functional_up", LABELS) is None
        assert registry.get_sample_value("ximiowatch_last_check_timestamp_seconds", LABELS) is None

    assert calls[-1] == "close"
    assert main._task is None


async def test_shutdown_during_active_stream_cancels_tasks_and_closes_resources(metrics, registry):
    stream_started = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            stream_started.set()
            try:
                await asyncio.sleep(10)
                yield b'{"ok":true}'
            finally:
                self.closed = True

        async def aclose(self) -> None:
            self.closed = True

    stream = BlockingStream()

    class StreamTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream, request=request)

        async def aclose(self) -> None:
            return None

    def factory(monitor) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=StreamTransport(), follow_redirects=False, trust_env=False
        )

    engine = MonitorEngine([make_monitor()], metrics, client_factory=factory)
    run_task = asyncio.create_task(engine.run(immediate=True))
    await asyncio.wait_for(stream_started.wait(), timeout=1)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    owned_clients = list(engine._clients.values())
    await engine.aclose()

    assert stream.closed is True
    assert all(client.is_closed for client in owned_clients)
    assert registry.get_sample_value("ximiowatch_checks_total", LABELS) is None
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and task.get_name() == "monitor-a"
    ]
