"""Sanitized logging and unexpected-error diagnostics."""

from __future__ import annotations

import logging
import sys

import httpx

from tests.helpers import make_monitor
from ximiowatch.engine import MonitorEngine
from ximiowatch.telemetry import JsonFormatter


def test_json_formatter_does_not_emit_raw_exception_text():
    try:
        raise ValueError("secret-token-in-exception")
    except ValueError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="ximiowatch",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="safe diagnostic message",
        args=(),
        exc_info=exc_info,
    )

    output = JsonFormatter().format(record)

    assert "secret-token-in-exception" not in output
    assert "exception" not in output
    assert "safe diagnostic message" in output


async def test_unexpected_engine_error_is_recorded_without_raw_message(metrics, monkeypatch, caplog):
    async def unexpected(*args, **kwargs):
        raise ValueError("secret-url-from-target")

    monkeypatch.setattr("ximiowatch.engine.check_monitor", unexpected)
    monitor = make_monitor()

    async with httpx.AsyncClient() as client:
        engine = MonitorEngine([monitor], metrics, client=client)
        with caplog.at_level(logging.INFO, logger="ximiowatch"):
            result = (await engine.check_all())[0]

    assert result.outcome == "error"
    assert result.error_type == "ValueError"
    assert "secret-url-from-target" not in caplog.text

    records = [record for record in caplog.records if record.name == "ximiowatch"]
    assert records[0].error_type == "ValueError"
