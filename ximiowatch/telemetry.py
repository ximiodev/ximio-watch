"""JSON structured logging and opt-in OpenTelemetry tracing."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_logging_configured = False
_tracing_configured = False


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line, including structured 'extra' fields."""

    _reserved = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._reserved and not key.startswith("_"):
                payload[key] = value
        # Raw exception text and stack traces may contain target URLs or field
        # values. Unexpected errors are logged by the engine with a class name only.
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    """Configure stdout JSON logging once."""
    global _logging_configured
    if _logging_configured:
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # HTTPX logs full request URLs at INFO; monitored URLs may contain query secrets.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _logging_configured = True


def setup_tracing() -> None:
    """Install a tracer provider. Exporters are disabled unless explicitly requested."""
    global _tracing_configured
    if _tracing_configured:
        return

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "ximiowatch")}
        )
    )
    exporter_setting = os.getenv("OTEL_TRACES_EXPORTER", "none").strip().lower()
    if exporter_setting in {"otlp", "otlphttp"}:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except Exception as exc:
            logging.getLogger(__name__).error(
                "otel.exporter.setup_failed", extra={"error_type": type(exc).__name__}
            )
    elif exporter_setting not in {"", "none", "false"}:
        logging.getLogger(__name__).warning(
            "otel.exporter.unknown", extra={"exporter": exporter_setting}
        )

    trace.set_tracer_provider(provider)
    _tracing_configured = True
