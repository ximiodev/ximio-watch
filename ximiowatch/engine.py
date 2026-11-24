"""Asynchronous monitor scheduling and execution."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from opentelemetry.trace import Status, StatusCode, Tracer

from .assertions import BUSINESS_ASSERTIONS, PERFORMANCE_ASSERTIONS, AssertionContext, evaluate
from .config import MonitorConfig
from .metrics import Metrics
from .result import CheckResult


def _performance_configured(monitor: MonitorConfig) -> bool:
    return any(assertion.type in PERFORMANCE_ASSERTIONS for assertion in monitor.assertions)


def _error_result(
    monitor: MonitorConfig,
    elapsed: float,
    outcome: str,
    error_type: str,
    consecutive_failures: int,
) -> CheckResult:
    """Build a result for a failed request without exposing exception details."""
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
        performance_configured=_performance_configured(monitor),
        performance_ok=None,
        consecutive_failures=consecutive_failures,
    )


def _evaluate_response(
    monitor: MonitorConfig, response: httpx.Response, elapsed: float, consecutive_failures: int
) -> CheckResult:
    payload: Any = None
    json_valid = False
    try:
        payload = response.json()
        json_valid = True
    except (ValueError, UnicodeDecodeError):
        pass

    context = AssertionContext(
        status_code=response.status_code,
        elapsed_seconds=elapsed,
        payload=payload,
        json_valid=json_valid,
    )
    results = [
        result for assertion in monitor.assertions for result in evaluate(assertion, context)
    ]
    failed_types = tuple(dict.fromkeys(result.type for result in results if not result.passed))

    status_results = [result for result in results if result.type == "http_status"]
    if status_results:
        http_ok = all(result.passed for result in status_results)
    else:
        # Default status behavior: 2xx and 3xx are HTTP-available.
        http_ok = 200 <= response.status_code < 400
        if not http_ok:
            failed_types = tuple(dict.fromkeys((*failed_types, "http_status")))

    json_configured = any(result.type in BUSINESS_ASSERTIONS for result in results)
    if not json_valid and json_configured:
        non_json_failures = tuple(
            failed_type
            for failed_type in failed_types
            if failed_type not in BUSINESS_ASSERTIONS and failed_type != "invalid_json"
        )
        failed_types = tuple(dict.fromkeys((*non_json_failures, "invalid_json")))

    business_failed_types = tuple(
        failed_type
        for failed_type in failed_types
        if failed_type in BUSINESS_ASSERTIONS or failed_type == "invalid_json"
    )
    functional_ok = http_ok and not business_failed_types

    performance_configured = _performance_configured(monitor)
    performance_results = [
        result for result in results if result.type in PERFORMANCE_ASSERTIONS
    ]
    performance_ok = (
        all(result.passed for result in performance_results) if performance_configured else None
    )

    if not http_ok:
        outcome = "http_failure"
    elif business_failed_types:
        outcome = "functional_failure"
    elif performance_configured and not performance_ok:
        outcome = "performance_failure"
    else:
        outcome = "success"

    return CheckResult(
        monitor=monitor.name,
        application=monitor.application,
        status_code=response.status_code,
        elapsed_seconds=elapsed,
        http_ok=http_ok,
        functional_ok=functional_ok,
        failed_types=failed_types,
        outcome=outcome,
        business_failed_types=business_failed_types,
        performance_configured=performance_configured,
        performance_ok=performance_ok,
        consecutive_failures=0 if functional_ok else consecutive_failures + 1,
    )


async def check_monitor(
    monitor: MonitorConfig,
    client: httpx.AsyncClient,
    *,
    metrics: Metrics | None = None,
    consecutive_failures: int = 0,
) -> CheckResult:
    """Execute one monitor check. Request errors never escape as exceptions."""
    started = time.monotonic()
    try:
        response = await client.request(
            monitor.method,
            monitor.url,
            headers=monitor.headers or None,
            timeout=monitor.timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        result = _error_result(
            monitor,
            time.monotonic() - started,
            "timeout",
            type(exc).__name__,
            consecutive_failures + 1,
        )
    except httpx.RequestError as exc:
        result = _error_result(
            monitor,
            time.monotonic() - started,
            "connection_error",
            type(exc).__name__,
            consecutive_failures + 1,
        )
    except Exception as exc:  # keep one broken monitor from affecting the others
        result = _error_result(
            monitor,
            time.monotonic() - started,
            "error",
            type(exc).__name__,
            consecutive_failures + 1,
        )
    else:
        result = _evaluate_response(
            monitor, response, time.monotonic() - started, consecutive_failures
        )

    if metrics is not None:
        metrics.record(result)
    return result


class MonitorEngine:
    """Runs every monitor in its own independent asyncio task."""

    def __init__(
        self,
        monitors: list[MonitorConfig],
        metrics: Metrics,
        *,
        client: httpx.AsyncClient | None = None,
        logger: logging.Logger | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.monitors = monitors
        self.metrics = metrics
        self.logger = logger or logging.getLogger("ximiowatch")
        self._tracer = tracer
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
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
                    self.client,
                    metrics=self.metrics,
                    consecutive_failures=self._consecutive_failures.get(monitor.name, 0),
                )
            except Exception as exc:  # unexpected: keep the loop alive, sanitize details
                result = _error_result(
                    monitor,
                    0.0,
                    "error",
                    type(exc).__name__,
                    self._consecutive_failures.get(monitor.name, 0) + 1,
                )
                self.metrics.record(result)
                span.add_event("ximiowatch.error", {"error.type": type(exc).__name__})

            self._consecutive_failures[monitor.name] = result.consecutive_failures
            self._log(result)

            span.set_attribute("http.response.status_code", result.status_code or 0)
            span.set_attribute("ximiowatch.http_up", result.http_ok)
            span.set_attribute("ximiowatch.functional_up", result.functional_ok)
            span.set_attribute("ximiowatch.performance_configured", result.performance_configured)
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
                "silent_failure": result.silent_failure,
                "performance_configured": result.performance_configured,
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
        """Close resources owned by this engine. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self.client.aclose()
