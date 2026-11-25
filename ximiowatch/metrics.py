"""Prometheus metrics. Labels are limited to configured names and fixed value sets."""

from __future__ import annotations

import time

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

from .config import (
    BUSINESS_ASSERTIONS,
    PERFORMANCE_ASSERTIONS,
    configured_assertion_types,
    required_assertion_types,
)
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
        "text_contains",
        "text_not_contains",
        "content_type_contains",
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
        "not_evaluated",
    }
)


class Metrics:
    """Owns all XimioWatch collectors, allowing isolated registries in tests."""

    def __init__(self, registry: CollectorRegistry | None = None, prefix: str = "ximiowatch"):
        self.registry: CollectorRegistry = registry if registry is not None else REGISTRY
        self.startup_timestamp_seconds = Gauge(
            f"{prefix}_startup_timestamp_seconds",
            "Unix timestamp when the monitor process started",
            registry=self.registry,
        )
        self.startup_timestamp_seconds.set(time.time())
        self.http_up = Gauge(
            f"{prefix}_http_up",
            "1 when the HTTP availability check passed, 0 otherwise",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.functional_up = Gauge(
            f"{prefix}_functional_up",
            "1 when evaluated business/content assertions passed, 0 when failed",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.functional_configured = Gauge(
            f"{prefix}_functional_configured",
            "1 when the monitor defines business/content assertions, 0 otherwise",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.functional_evaluated = Gauge(
            f"{prefix}_functional_evaluated",
            "1 when the latest check evaluated configured business/content assertions",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.performance_up = Gauge(
            f"{prefix}_performance_up",
            "1 when evaluated response-time thresholds passed, 0 when breached",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.performance_configured = Gauge(
            f"{prefix}_performance_configured",
            "1 when the monitor defines a response-time threshold, 0 otherwise",
            MONITOR_LABELS,
            registry=self.registry,
        )
        self.performance_evaluated = Gauge(
            f"{prefix}_performance_evaluated",
            "1 when the latest check evaluated a configured response-time threshold",
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
            "Checks where HTTP was available and a required content assertion failed",
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
            "Number of consecutive HTTP, functional or transport failures",
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
        """Expose bounded configuration facts for freshness and state evaluation."""
        for monitor in monitors:
            labels = {"monitor": monitor.name, "application": monitor.application}
            all_types = configured_assertion_types(monitor.assertions)
            required_types = required_assertion_types(monitor.assertions)
            self.monitor_interval_seconds.labels(**labels).set(monitor.interval_seconds)
            self.monitor_timeout_seconds.labels(**labels).set(monitor.timeout_seconds)
            self.functional_configured.labels(**labels).set(
                1 if required_types & BUSINESS_ASSERTIONS else 0
            )
            self.performance_configured.labels(**labels).set(
                1 if all_types & PERFORMANCE_ASSERTIONS else 0
            )

    def record(self, result: CheckResult) -> None:
        """Update every metric from one monitor execution (called exactly once)."""
        labels = {"monitor": result.monitor, "application": result.application}
        label_values = (labels["monitor"], labels["application"])
        now = time.time()
        self.http_up.labels(**labels).set(1 if result.http_ok else 0)
        self.functional_configured.labels(**labels).set(1 if result.functional_configured else 0)
        self.functional_evaluated.labels(**labels).set(1 if result.functional_evaluated else 0)
        self.performance_configured.labels(**labels).set(1 if result.performance_configured else 0)
        self.performance_evaluated.labels(**labels).set(1 if result.performance_evaluated else 0)

        for gauge, state in (
            (self.functional_up, result.functional_state),
            (self.performance_up, result.performance_state),
        ):
            if state in {"up", "down"}:
                gauge.labels(**labels).set(1 if state == "up" else 0)
            else:
                # Remove stale healthy/failing values; unknown is not a binary result.
                gauge.remove(*label_values)

        outcome = result.outcome if result.outcome in KNOWN_OUTCOMES else "error"
        self.checks_total.labels(**labels, outcome=outcome).inc()
        if result.status_code is not None and result.outcome != "response_too_large":
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
