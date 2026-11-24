"""Prometheus metrics. Labels are limited to configured names and fixed value sets."""

from __future__ import annotations

import time

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

from .config import MonitorConfig
from .result import CheckResult

MONITOR_LABELS = ("monitor", "application")
KNOWN_ASSERTION_TYPES = frozenset(
    {
        "http_status",
        "max_response_time",
        "json_exists",
        "json_equals",
        "json_not_empty",
        "numeric_gt",
        "numeric_lt",
        "conditional",
        "invalid_json",
        "timeout",
        "connection_error",
        "error",
        "response_too_large",
        "unknown",
    }
)
KNOWN_OUTCOMES = frozenset(
    {
        "success",
        "functional_failure",
        "performance_failure",
        "http_failure",
        "timeout",
        "connection_error",
        "error",
        "response_too_large",
    }
)


class Metrics:
    """Owns all XimioWatch collectors, allowing isolated registries in tests."""

    def __init__(self, registry: CollectorRegistry | None = None, prefix: str = "ximiowatch"):
        self.registry: CollectorRegistry = registry if registry is not None else REGISTRY
        self.http_up = Gauge(
            f"{prefix}_http_up",
            "1 when the HTTP availability check passed, 0 otherwise",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.functional_up = Gauge(
            f"{prefix}_functional_up",
            "1 when HTTP and business/content assertions passed, 0 otherwise",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.performance_up = Gauge(
            f"{prefix}_performance_up",
            "1 when configured response-time thresholds passed, 0 when breached",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.performance_configured = Gauge(
            f"{prefix}_performance_configured",
            "1 when the monitor defines a response-time threshold, 0 otherwise",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.monitor_interval_seconds = Gauge(
            f"{prefix}_monitor_interval_seconds",
            "Configured delay between completed checks, in seconds",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.monitor_timeout_seconds = Gauge(
            f"{prefix}_monitor_timeout_seconds",
            "Configured absolute request deadline, in seconds",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.checks_total = Counter(
            f"{prefix}_checks_total",
            "Monitor executions by outcome",
            (*MONITOR_LABELS, "outcome"),
            registry=self.registry,
        )
        self.assertion_failures_total = Counter(
            f"{prefix}_assertion_failures_total",
            "Failed assertions by type",
            (*MONITOR_LABELS, "type"),
            registry=self.registry,
        )
        self.silent_failures_total = Counter(
            f"{prefix}_silent_failures_total",
            "Checks where HTTP was available but business/content assertions failed",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.request_duration_seconds = Histogram(
            f"{prefix}_request_duration_seconds",
            "Duration of monitored HTTP requests in seconds",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.consecutive_failures = Gauge(
            f"{prefix}_consecutive_failures",
            "Number of consecutive HTTP or functional failures",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.last_success_timestamp_seconds = Gauge(
            f"{prefix}_last_success_timestamp_seconds",
            "Unix timestamp of the last fully healthy check",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.last_check_timestamp_seconds = Gauge(
            f"{prefix}_last_check_timestamp_seconds",
            "Unix timestamp of the last completed check, including failed checks",
            MONITOR_LABELS,
            registry=self.registry,
        )

    def configure(self, monitors: list[MonitorConfig]) -> None:
        """Expose bounded configuration facts for freshness evaluation."""
        for monitor in monitors:
            labels = {"monitor": monitor.name, "application": monitor.application}
            self.monitor_interval_seconds.labels(**labels).set(monitor.interval_seconds)
            self.monitor_timeout_seconds.labels(**labels).set(monitor.timeout_seconds)
            self.performance_configured.labels(**labels).set(
                1 if any(a.type == "max_response_time" for a in monitor.assertions) else 0
            )

    def record(self, result: CheckResult) -> None:
        """Update every metric from one monitor execution (called exactly once)."""
        labels = {"monitor": result.monitor, "application": result.application}
        now = time.time()
        self.http_up.labels(**labels).set(1 if result.http_ok else 0)
        self.functional_up.labels(**labels).set(1 if result.functional_ok else 0)
        if result.performance_configured:
            self.performance_up.labels(**labels).set(
                0 if result.performance_ok is not True else 1
            )
        outcome = result.outcome if result.outcome in KNOWN_OUTCOMES else "error"
        self.checks_total.labels(**labels, outcome=outcome).inc()
        if result.status_code is not None:
            self.request_duration_seconds.labels(**labels).observe(result.elapsed_seconds)

        for failed_type in result.failed_types:
            safe_type = failed_type if failed_type in KNOWN_ASSERTION_TYPES else "unknown"
            self.assertion_failures_total.labels(**labels, type=safe_type).inc()

        if result.silent_failure:
            self.silent_failures_total.labels(**labels).inc()

        self.consecutive_failures.labels(**labels).set(result.consecutive_failures)
        self.last_check_timestamp_seconds.labels(**labels).set(now)
        if result.healthy:
            self.last_success_timestamp_seconds.labels(**labels).set(now)
