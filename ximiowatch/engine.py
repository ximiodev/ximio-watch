"""Asynchronous monitor scheduling and execution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from opentelemetry.trace import Status, StatusCode, Tracer

from .assertions import AssertionContext, evaluate_detailed
from .config import (
    BUSINESS_ASSERTIONS,
    JSON_ASSERTIONS,
    PERFORMANCE_ASSERTIONS,
    TEXT_ASSERTIONS,
    MonitorConfig,
    configured_assertion_types,
    required_assertion_types,
)
from .metrics import Metrics
from .result import CheckResult, HealthState

ClientFactory = Callable[[MonitorConfig], httpx.AsyncClient]

_RESPONSE_TOO_LARGE = "response_too_large"
_NOT_EVALUATED = "not_evaluated"
_CHARSET = re.compile(
    r"charset\s*=\s*(?:\"([^\";\s]+)\"|'([^';\s]+)'|([^;\s]+))", re.IGNORECASE
)


class ResponseTooLarge(Exception):
    """Raised when a decoded response body exceeds the configured limit."""

    def __init__(self, status_code: int) -> None:
        super().__init__("response body exceeded the configured limit")
        self.status_code = status_code


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """Use an injected test transport without taking ownership of it."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        return None


def _dimension_state(configured: bool, evaluated: bool, passed: bool | None) -> HealthState:
    if not configured:
        return "not_configured"
    if not evaluated:
        return "unknown"
    return "up" if passed is True else "down"


def _next_consecutive_failures(outcome: str, previous: int) -> int:
    """Count confirmed failures, not performance-only or skipped checks."""
    if outcome == "success":
        return 0
    if outcome in {"performance_failure", _NOT_EVALUATED}:
        return previous
    return previous + 1


def _error_result(
    monitor: MonitorConfig,
    elapsed: float,
    outcome: str,
    error_type: str,
    consecutive_failures: int,
) -> CheckResult:
    """Build a result for an incomplete request without leaking exception details."""
    all_types = configured_assertion_types(monitor.assertions)
    required_types = required_assertion_types(monitor.assertions)
    functional_configured = bool(required_types & BUSINESS_ASSERTIONS)
    performance_configured = bool(all_types & PERFORMANCE_ASSERTIONS)
    return CheckResult(
        monitor=monitor.name,
        application=monitor.application,
        status_code=None,
        elapsed_seconds=elapsed,
        http_ok=False,
        functional_ok=False,
        failed_types=(outcome,),
        outcome=outcome,
        error_type=error_type,
        business_failed_types=(),
        performance_configured=performance_configured,
        performance_ok=None,
        consecutive_failures=_next_consecutive_failures(outcome, consecutive_failures),
        functional_configured=functional_configured,
        functional_evaluated=False,
        functional_state=_dimension_state(functional_configured, False, None),
        performance_evaluated=False,
        performance_state=_dimension_state(performance_configured, False, None),
    )


def _status_ok(monitor: MonitorConfig, status_code: int) -> bool:
    """Apply explicit status assertions or the default 2xx-only status policy."""
    status_assertions = [
        assertion for assertion in monitor.assertions if assertion.type == "http_status"
    ]
    if status_assertions:
        return all(assertion.value == status_code for assertion in status_assertions)
    return 200 <= status_code < 300


def _oversized_result(
    monitor: MonitorConfig,
    status_code: int,
    elapsed: float,
    consecutive_failures: int,
) -> CheckResult:
    """Report an oversized response without fabricating business evaluation."""
    all_types = configured_assertion_types(monitor.assertions)
    required_types = required_assertion_types(monitor.assertions)
    functional_configured = bool(required_types & BUSINESS_ASSERTIONS)
    performance_configured = bool(all_types & PERFORMANCE_ASSERTIONS)
    http_ok = _status_ok(monitor, status_code)
    failed_types = (_RESPONSE_TOO_LARGE,)
    if not http_ok:
        failed_types = (_RESPONSE_TOO_LARGE, "http_status")
    return CheckResult(
        monitor=monitor.name,
        application=monitor.application,
        status_code=status_code,
        elapsed_seconds=elapsed,
        http_ok=http_ok,
        functional_ok=False,
        failed_types=failed_types,
        outcome=_RESPONSE_TOO_LARGE,
        error_type="ResponseTooLarge",
        business_failed_types=(),
        performance_configured=performance_configured,
        performance_ok=None,
        consecutive_failures=_next_consecutive_failures(
            _RESPONSE_TOO_LARGE, consecutive_failures
        ),
        functional_configured=functional_configured,
        functional_evaluated=False,
        functional_state=_dimension_state(functional_configured, False, None),
        performance_evaluated=False,
        performance_state=_dimension_state(performance_configured, False, None),
    )


def _decode_text(body: bytes, content_type: str | None) -> tuple[str | None, bool]:
    """Decode body text once. Unsupported or invalid encodings are not decodable."""
    match = _CHARSET.search(content_type or "")
    encoding = next((group for group in match.groups() if group), "utf-8") if match else "utf-8"
    try:
        return body.decode(encoding), True
    except (LookupError, UnicodeDecodeError):
        return None, False


def _evaluate_response(
    monitor: MonitorConfig,
    status_code: int,
    body: bytes,
    elapsed: float,
    content_type: str | None,
    consecutive_failures: int,
) -> CheckResult:
    all_types = configured_assertion_types(monitor.assertions)
    required_types = required_assertion_types(monitor.assertions)
    functional_configured = bool(required_types & BUSINESS_ASSERTIONS)
    performance_configured = bool(all_types & PERFORMANCE_ASSERTIONS)

    payload: Any = None
    json_valid = False
    try:
        payload = json.loads(body)
        json_valid = True
    except (ValueError, UnicodeDecodeError):
        pass

    text: str | None = None
    text_decodable = False
    if all_types & TEXT_ASSERTIONS:
        text, text_decodable = _decode_text(body, content_type)

    context = AssertionContext(
        status_code=status_code,
        elapsed_seconds=elapsed,
        payload=payload,
        json_valid=json_valid,
        text=text,
        text_decodable=text_decodable,
        content_type=content_type,
    )
    evaluation = evaluate_detailed(monitor.assertions, context)
    results = evaluation.results
    # Required failures count; latency guards also count as performance failures.
    # Business guards only select branches and never count as business failures.
    failed_types = tuple(
        dict.fromkeys(
            result.type
            for result in evaluation.all_results
            if not result.passed
            and (result.role == "required" or result.type in PERFORMANCE_ASSERTIONS)
        )
    )

    status_results = [result for result in results if result.type == "http_status"]
    if status_results:
        http_ok = all(result.passed for result in status_results)
    else:
        # Default status behavior: only 2xx responses are considered available.
        http_ok = 200 <= status_code < 300
        if not http_ok:
            failed_types = tuple(dict.fromkeys((*failed_types, "http_status")))

    business_results = tuple(result for result in results if result.type in BUSINESS_ASSERTIONS)
    performance_results = tuple(
        result for result in evaluation.all_results if result.type in PERFORMANCE_ASSERTIONS
    )

    business_evaluated = False
    business_failed_types: tuple[str, ...] = ()
    if http_ok:
        # Only an attempted JSON assertion (guard or selected branch) establishes a
        # JSON contract. A JSON assertion in a skipped branch does not.
        if (
            functional_configured
            and evaluation.incomplete_types & JSON_ASSERTIONS
            and not json_valid
        ):
            # Invalid applicable JSON is a confirmed failure, never a silent skip.
            non_json_failures = tuple(
                failed_type
                for failed_type in failed_types
                if failed_type not in JSON_ASSERTIONS and failed_type != "invalid_json"
            )
            failed_types = tuple(dict.fromkeys((*non_json_failures, "invalid_json")))
            business_failed_types = tuple(
                failed_type
                for failed_type in failed_types
                if failed_type in BUSINESS_ASSERTIONS or failed_type == "invalid_json"
            )
            business_evaluated = True
        else:
            business_failed_types = tuple(
                dict.fromkeys(result.type for result in business_results if not result.passed)
            )
            business_evaluated = bool(business_results) and not (
                evaluation.incomplete and not business_failed_types
            )
    else:
        # Business assertions are not claimed as evaluated when HTTP is down.
        failed_types = tuple(
            failed_type for failed_type in failed_types if failed_type not in BUSINESS_ASSERTIONS
        )

    performance_ok = (
        all(result.passed for result in performance_results) if performance_results else None
    )
    performance_evaluated = performance_configured and bool(performance_results)

    functional_state = _dimension_state(
        functional_configured, business_evaluated, not business_failed_types
    )
    performance_state = _dimension_state(
        performance_configured, performance_evaluated, performance_ok
    )
    # Preserve the Stage 2 boolean contract: HTTP-only monitors remain functionally
    # OK when HTTP succeeds; UNKNOWN and DOWN are not OK.
    functional_ok = http_ok and functional_state in {"up", "not_configured"}

    if not http_ok:
        outcome = "http_failure"
    elif functional_state == "down":
        outcome = "functional_failure"
    elif performance_state == "down":
        outcome = "performance_failure"
    elif functional_state == "unknown" or performance_state == "unknown":
        outcome = _NOT_EVALUATED
    else:
        outcome = "success"

    return CheckResult(
        monitor=monitor.name,
        application=monitor.application,
        status_code=status_code,
        elapsed_seconds=elapsed,
        http_ok=http_ok,
        functional_ok=functional_ok,
        failed_types=failed_types,
        outcome=outcome,
        business_failed_types=business_failed_types,
        performance_configured=performance_configured,
        performance_ok=performance_ok,
        consecutive_failures=_next_consecutive_failures(outcome, consecutive_failures),
        functional_configured=functional_configured,
        functional_evaluated=business_evaluated,
        functional_state=functional_state,
        performance_evaluated=performance_evaluated,
        performance_state=performance_state,
    )


async def _read_bounded_body(response: httpx.Response, limit: int) -> bytes:
    """Consume a decoded response in bounded chunks, stopping at the first breach."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(response.status_code)
        chunks.append(chunk)
    return b"".join(chunks)


async def _execute_check(
    monitor: MonitorConfig, client: httpx.AsyncClient, consecutive_failures: int
) -> CheckResult:
    started = time.monotonic()
    try:
        # Each engine client belongs to one monitor. Clearing its jar here prevents
        # cookies received during one check from affecting the next independent one.
        client.cookies.clear()
        async with asyncio.timeout(monitor.timeout_seconds):
            async with client.stream(
                monitor.method,
                monitor.url,
                headers=monitor.headers or None,
                timeout=httpx.Timeout(monitor.timeout_seconds),
                follow_redirects=False,
            ) as response:
                body = await _read_bounded_body(response, monitor.max_response_bytes)
                elapsed = time.monotonic() - started
                return _evaluate_response(
                    monitor,
                    response.status_code,
                    body,
                    elapsed,
                    response.headers.get("content-type"),
                    consecutive_failures,
                )
    except asyncio.TimeoutError:
        return _error_result(
            monitor,
            time.monotonic() - started,
            "timeout",
            "TimeoutError",
            consecutive_failures,
        )
    except ResponseTooLarge as exc:
        return _oversized_result(
            monitor,
            exc.status_code,
            time.monotonic() - started,
            consecutive_failures,
        )
    except httpx.TimeoutException as exc:
        return _error_result(
            monitor,
            time.monotonic() - started,
            "timeout",
            type(exc).__name__,
            consecutive_failures,
        )
    except httpx.RequestError as exc:
        return _error_result(
            monitor,
            time.monotonic() - started,
            "connection_error",
            type(exc).__name__,
            consecutive_failures,
        )
    except Exception as exc:  # keep one broken monitor from affecting the others
        return _error_result(
            monitor,
            time.monotonic() - started,
            "error",
            type(exc).__name__,
            consecutive_failures,
        )


async def check_monitor(
    monitor: MonitorConfig,
    client: httpx.AsyncClient,
    *,
    metrics: Metrics | None = None,
    consecutive_failures: int = 0,
) -> CheckResult:
    """Execute one monitor check. Cancellation is never converted into a result."""
    result = await _execute_check(monitor, client, consecutive_failures)
    if metrics is not None:
        metrics.record(result)
    return result


class MonitorEngine:
    """Runs every monitor in its own task with an independently owned client."""

    def __init__(
        self,
        monitors: list[MonitorConfig],
        metrics: Metrics,
        *,
        client: httpx.AsyncClient | None = None,
        client_factory: ClientFactory | None = None,
        logger: logging.Logger | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.monitors = monitors
        self.metrics = metrics
        self.logger = logger or logging.getLogger("ximiowatch")
        self._tracer = tracer

        if client_factory is not None:
            factory = client_factory
        elif client is not None:
            # Existing mock-transport injection stays useful, but each monitor still
            # gets its own cookie jar and the injected client keeps its ownership.
            template = client
            transport = _BorrowedTransport(template._transport)

            def factory(monitor: MonitorConfig) -> httpx.AsyncClient:
                return httpx.AsyncClient(
                    headers=template.headers,
                    transport=transport,
                    follow_redirects=False,
                    trust_env=False,
                )

        else:

            def factory(monitor: MonitorConfig) -> httpx.AsyncClient:
                return httpx.AsyncClient(follow_redirects=False, trust_env=False)

        self._clients: dict[str, httpx.AsyncClient] = {
            monitor.name: factory(monitor) for monitor in monitors
        }

        self._consecutive_failures: dict[str, int] = {}
        self._closed = False
        self.metrics.configure(monitors)

    def _tracer_instance(self) -> Tracer:
        if self._tracer is None:
            from opentelemetry import trace

            self._tracer = trace.get_tracer("ximiowatch")
        return self._tracer

    async def _check(self, monitor: MonitorConfig) -> CheckResult:
        tracer = self._tracer_instance()
        with tracer.start_as_current_span("ximiowatch.monitor.check") as span:
            span.set_attribute("monitor.name", monitor.name)
            span.set_attribute("application.name", monitor.application)
            try:
                result = await check_monitor(
                    monitor,
                    self._clients[monitor.name],
                    metrics=self.metrics,
                    consecutive_failures=self._consecutive_failures.get(monitor.name, 0),
                )
            except Exception as exc:  # unexpected: keep the loop alive, sanitize details
                result = _error_result(
                    monitor,
                    0.0,
                    "error",
                    type(exc).__name__,
                    self._consecutive_failures.get(monitor.name, 0),
                )
                self.metrics.record(result)
                span.add_event("ximiowatch.error", {"error.type": type(exc).__name__})

            self._consecutive_failures[monitor.name] = result.consecutive_failures
            self._log(result)

            span.set_attribute("http.response.status_code", result.status_code or 0)
            span.set_attribute("ximiowatch.http_state", result.http_state)
            span.set_attribute("ximiowatch.functional_state", result.functional_state)
            span.set_attribute("ximiowatch.performance_state", result.performance_state)
            span.set_attribute("ximiowatch.functional_configured", result.functional_configured)
            span.set_attribute("ximiowatch.functional_evaluated", result.functional_evaluated)
            span.set_attribute("ximiowatch.performance_configured", result.performance_configured)
            span.set_attribute("ximiowatch.performance_evaluated", result.performance_evaluated)
            if result.performance_ok is not None:
                span.set_attribute("ximiowatch.performance_up", result.performance_ok)
            span.set_attribute("ximiowatch.outcome", result.outcome)
            span.set_attribute("ximiowatch.duration_seconds", round(result.elapsed_seconds, 6))
            span.set_attribute(
                "ximiowatch.failed_assertions", ",".join(result.failed_types) or "none"
            )
            if result.error_type:
                span.set_attribute("error.type", result.error_type)
            if not result.healthy:
                span.set_status(Status(StatusCode.ERROR, result.outcome))
            return result

    def _log(self, result: CheckResult) -> None:
        self.logger.info(
            "monitor.check.completed",
            extra={
                "monitor": result.monitor,
                "application": result.application,
                "outcome": result.outcome,
                "http_ok": result.http_ok,
                "functional_ok": result.functional_ok,
                "http_state": result.http_state,
                "functional_state": result.functional_state,
                "performance_state": result.performance_state,
                "functional_configured": result.functional_configured,
                "functional_evaluated": result.functional_evaluated,
                "performance_configured": result.performance_configured,
                "performance_evaluated": result.performance_evaluated,
                "silent_failure": result.silent_failure,
                "performance_ok": "none" if result.performance_ok is None else result.performance_ok,
                "status_code": result.status_code or 0,
                "duration_seconds": round(result.elapsed_seconds, 6),
                "failed_assertions": ",".join(result.failed_types) or "none",
                "consecutive_failures": result.consecutive_failures,
                "error_type": result.error_type or "none",
            },
        )

    async def check_all(self) -> list[CheckResult]:
        """Run all monitors once, concurrently."""
        return await asyncio.gather(*(self._check(monitor) for monitor in self.monitors))

    async def _monitor_loop(self, monitor: MonitorConfig, immediate: bool) -> None:
        if not immediate:
            await asyncio.sleep(monitor.interval_seconds)
        while True:
            try:
                await self._check(monitor)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Sanitized: no raw exception text, which could contain target data.
                self.logger.error(
                    "monitor.loop.error",
                    extra={"monitor": monitor.name, "error_type": type(exc).__name__},
                )
            await asyncio.sleep(monitor.interval_seconds)

    async def run(self, *, immediate: bool = True) -> None:
        """Start the perpetual loops and run until cancelled."""
        tasks = [
            asyncio.create_task(self._monitor_loop(monitor, immediate), name=monitor.name)
            for monitor in self.monitors
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        """Close every client owned by this engine. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(client.aclose() for client in self._clients.values()), return_exceptions=True
        )
