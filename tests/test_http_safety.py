"""HTTP execution safety: deadlines, bounded streams, isolation and redirects."""

from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from tests.helpers import make_monitor
from ximiowatch.config import Assertion, MonitorConfig
from ximiowatch.engine import MonitorEngine, check_monitor
from ximiowatch.metrics import KNOWN_OUTCOMES

BASE_LABELS = {"monitor": "monitor-a", "application": "app-a"}


class TrackingStream(httpx.AsyncByteStream):
    """Byte stream that records whether HTTPX released it."""

    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self._chunks = chunks
        self._delay = delay
        self.closed = False

    async def __aiter__(self):
        try:
            for chunk in self._chunks:
                if self._delay:
                    await asyncio.sleep(self._delay)
                yield chunk
        finally:
            self.closed = True

    async def aclose(self) -> None:
        self.closed = True


class StreamTransport(httpx.AsyncBaseTransport):
    """Transport that returns controlled response streams."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self.streams: list[TrackingStream] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response, stream = self._factory(request)
        self.streams.append(stream)
        return response

    async def aclose(self) -> None:
        return None


async def test_successful_http_and_json_check_records_once(metrics, registry):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ready": True}, request=request
        )

    monitor = make_monitor(
        assertions=[
            Assertion(type="http_status", value=200),
            Assertion(type="json_equals", path="ready", value=True),
        ]
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "success"
    assert result.http_ok is True
    assert result.functional_ok is True
    assert (
        registry.get_sample_value(
            "ximiowatch_checks_total", {**BASE_LABELS, "outcome": "success"}
        )
        == 1.0
    )


async def test_absolute_timeout_during_slow_streaming_closes_response(metrics):
    stream = TrackingStream([b'{"ready":true}'] * 20, delay=0.03)
    transport = StreamTransport(
        lambda request: (
            httpx.Response(200, stream=stream, request=request),
            stream,
        )
    )
    monitor = make_monitor(timeout_seconds=0.1)

    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "timeout"
    assert result.error_type == "TimeoutError"
    assert result.status_code is None
    assert stream.closed is True


async def test_timeout_during_header_acquisition_is_not_connection_error(metrics):
    class DelayedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200, json={"ready": True}, request=request)

        async def aclose(self) -> None:
            return None

    monitor = make_monitor(timeout_seconds=0.05)

    async with httpx.AsyncClient(transport=DelayedTransport()) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "timeout"
    assert result.error_type == "TimeoutError"
    assert result.outcome != "connection_error"


async def test_response_larger_than_limit_is_bounded_and_closes_response(metrics, registry):
    stream = TrackingStream([b"x" * 2048])
    transport = StreamTransport(
        lambda request: (
            httpx.Response(200, stream=stream, request=request),
            stream,
        )
    )
    monitor = make_monitor(max_response_bytes=1024)

    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_monitor(
            monitor, client, metrics=metrics, consecutive_failures=2
        )

    assert result.outcome == "response_too_large"
    assert result.status_code == 200
    assert result.http_ok is True
    assert result.functional_ok is False
    assert result.failed_types == ("response_too_large",)
    assert result.consecutive_failures == 3
    assert stream.closed is True
    assert (
        registry.get_sample_value(
            "ximiowatch_checks_total",
            {**BASE_LABELS, "outcome": "response_too_large"},
        )
        == 1.0
    )
    assert registry.get_sample_value("ximiowatch_functional_up", BASE_LABELS) == 0.0


async def test_oversized_response_without_content_length_is_rejected(metrics):
    stream = TrackingStream([b"abcd", b"efgh"])
    transport = StreamTransport(
        lambda request: (
            httpx.Response(200, stream=stream, request=request),
            stream,
        )
    )
    monitor = make_monitor(max_response_bytes=4)

    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "response_too_large"
    assert stream.closed is True


async def test_decoded_compressed_response_is_subject_to_limit(metrics):
    payload = b'{"value":"' + (b"x" * 4096) + b'"}'
    compressed = gzip.compress(payload)
    stream = TrackingStream([compressed])
    transport = StreamTransport(
        lambda request: (
            httpx.Response(
                200,
                stream=stream,
                headers={
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(compressed)),
                    "Content-Type": "application/json",
                },
                request=request,
            ),
            stream,
        )
    )
    monitor = make_monitor(max_response_bytes=1024)

    async with httpx.AsyncClient(transport=transport) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "response_too_large"
    assert result.status_code == 200
    assert stream.closed is True


async def test_cookies_are_isolated_between_monitors_and_executions(metrics):
    records: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie")
        records.append((request.url.path, cookie))
        response = httpx.Response(200, json={"ok": True}, request=request)
        if request.url.path == "/set":
            response.headers["Set-Cookie"] = "shared=stale; Path=/"
        return response

    def factory(monitor: MonitorConfig) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
        )

    setter = make_monitor(
        name="setter", application="app-a", url="http://demo.test/set"
    )
    reader = make_monitor(
        name="reader", application="app-a", url="http://demo.test/read"
    )
    engine = MonitorEngine([setter, reader], metrics, client_factory=factory)

    try:
        await engine.check_all()
        first_round = list(records)
        records.clear()
        await engine.check_all()
    finally:
        await engine.aclose()

    assert first_round == [("/set", None), ("/read", None)] or first_round == [
        ("/read", None),
        ("/set", None),
    ]
    assert all(cookie is None for _, cookie in records)


async def test_plain_client_would_persist_cookies_without_isolation():
    """Control case: the mock transport really exercises HTTPX cookie storage."""
    records: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        records.append(request.headers.get("cookie"))
        response = httpx.Response(200, json={"ok": True}, request=request)
        response.headers["Set-Cookie"] = "shared=stale; Path=/"
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await client.get("http://demo.test/set")
        await client.get("http://demo.test/read")

    assert records == [None, "shared=stale"]


async def test_external_client_is_not_closed_by_engine(metrics):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    ) as client:
        engine = MonitorEngine([make_monitor()], metrics, client=client)
        await engine.check_all()
        await engine.aclose()

        assert client.is_closed is False
        assert all(owned.is_closed for owned in engine._clients.values())


async def test_engine_records_each_completed_check_exactly_once(metrics, registry):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    monitor = make_monitor(
        assertions=[Assertion(type="json_equals", path="ok", value=True)]
    )
    engine = MonitorEngine(
        [monitor],
        metrics,
        client_factory=lambda _: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
        ),
    )
    try:
        await engine.check_all()
        await engine.check_all()
    finally:
        await engine.aclose()

    recorded = sum(
        registry.get_sample_value(
            "ximiowatch_checks_total", {**BASE_LABELS, "outcome": outcome}
        )
        or 0.0
        for outcome in KNOWN_OUTCOMES
    )
    assert recorded == 2.0


async def test_unexpected_redirect_is_not_healthy_and_is_not_followed(metrics):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={"Location": "http://demo.test/destination"},
                request=request,
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    monitor = make_monitor(url="http://demo.test/redirect")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.status_code == 302
    assert result.http_ok is False
    assert result.outcome == "http_failure"
    assert hits == ["/redirect"]


async def test_explicit_status_assertion_accepts_redirect_response(metrics):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://demo.test/next"}, request=request)

    monitor = make_monitor(
        url="http://demo.test/redirect",
        assertions=[Assertion(type="http_status", value=302)],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.status_code == 302
    assert result.http_ok is True
    assert result.outcome == "success"


async def test_default_status_policy_accepts_only_2xx(metrics):
    def handler(request: httpx.Request) -> httpx.Response:
        status = int(request.url.path.strip("/"))
        return httpx.Response(status, json={"ok": True}, request=request)

    def monitor_for(status: int):
        return make_monitor(
            url=f"http://demo.test/{status}",
            assertions=[Assertion(type="json_equals", path="ok", value=True)],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await check_monitor(monitor_for(201), client)
        not_ok = await check_monitor(monitor_for(301), client)
        failed = await check_monitor(monitor_for(500), client)

    assert ok.outcome == "success"
    assert not_ok.outcome == "http_failure"
    assert failed.outcome == "http_failure"


async def test_external_cancellation_is_not_swallowed_or_recorded(metrics, registry):
    started = asyncio.Event()

    class BlockingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.sleep(10)
            return httpx.Response(200, json={"ok": True}, request=request)

        async def aclose(self) -> None:
            return None

    monitor = make_monitor(timeout_seconds=10)

    async with httpx.AsyncClient(transport=BlockingTransport()) as client:
        task = asyncio.create_task(check_monitor(monitor, client, metrics=metrics))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert registry.get_sample_value("ximiowatch_checks_total", BASE_LABELS) is None
