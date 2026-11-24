"""Regression tests for secret-safe configuration diagnostics."""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from ximiowatch import main, telemetry
from ximiowatch.config import ConfigurationError, load_monitors
from ximiowatch.telemetry import JsonFormatter

CANARY = "SYNTHETIC-CANARY-DO-NOT-LOG-8B41E6"


def _diagnostics(exc: BaseException) -> str:
    return "\n".join(
        [
            str(exc),
            repr(exc.__cause__),
            repr(exc.__context__),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
    )


def _assert_clean(canary: str, *texts: str) -> None:
    for text in texts:
        assert canary not in text


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_invalid_monitor_field_does_not_leak_expanded_header(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_AUTH_HEADER", CANARY)
    monkeypatch.setenv("TEST_BAD_INTERVAL", CANARY)
    path = _write(
        tmp_path / "monitors.yaml",
        """monitors:
  - name: sample
    application: demo
    url: http://demo.test/check
    headers:
      X-Authorization: "${TEST_AUTH_HEADER}"
    interval_seconds: "${TEST_BAD_INTERVAL}"
""",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    output = _diagnostics(caught.value)
    _assert_clean(CANARY, output)
    assert "Invalid monitor at entry #1" in output
    assert "interval_seconds" in output


def test_invalid_assertion_value_does_not_leak_expanded_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SENSITIVE_ASSERTION", CANARY)
    path = _write(
        tmp_path / "monitors.yaml",
        """monitors:
  - name: sample
    application: demo
    url: http://demo.test/check
    assertions:
      - type: max_response_time
        value: "${TEST_SENSITIVE_ASSERTION}"
""",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    output = _diagnostics(caught.value)
    _assert_clean(CANARY, output)
    assert "assertions[0]" in output
    assert "requires a numeric 'value'" in output


def test_malformed_yaml_does_not_leak_sensitive_text(tmp_path):
    path = _write(
        tmp_path / "monitors.yaml",
        f"""monitors:
  - name: sample
    application: demo
    url: http://demo.test/check
    synthetic_header: {CANARY}
   broken: [
""",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    output = _diagnostics(caught.value)
    _assert_clean(CANARY, output)
    assert "not valid YAML" in output
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_missing_environment_does_not_leak_other_expanded_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_AUTH_HEADER", CANARY)
    monkeypatch.delenv("TEST_MISSING_SETTING", raising=False)
    path = _write(
        tmp_path / "monitors.yaml",
        """monitors:
  - name: sample
    application: demo
    url: "${TEST_MISSING_SETTING}"
    headers:
      X-Authorization: "${TEST_AUTH_HEADER}"
""",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    output = _diagnostics(caught.value)
    _assert_clean(CANARY, output)
    assert "TEST_MISSING_SETTING" in output
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_invalid_monitor_name_does_not_leak_expanded_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_EMPTY_NAME", "")
    monkeypatch.setenv("TEST_AUTH_HEADER", CANARY)
    path = _write(
        tmp_path / "monitors.yaml",
        """monitors:
  - name: "${TEST_EMPTY_NAME}"
    application: demo
    url: http://demo.test/check
    headers:
      X-Authorization: "${TEST_AUTH_HEADER}"
""",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    output = _diagnostics(caught.value)
    _assert_clean(CANARY, output)
    assert "name must not be empty" in output


def test_duplicate_monitor_names_do_not_leak_expanded_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_MONITOR_NAME", CANARY)
    path = _write(
        tmp_path / "monitors.yaml",
        """monitors:
  - name: "${TEST_MONITOR_NAME}"
    application: demo
    url: http://demo.test/first
  - name: "${TEST_MONITOR_NAME}"
    application: demo
    url: http://demo.test/second
""",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    output = _diagnostics(caught.value)
    _assert_clean(CANARY, output)
    assert "Duplicate monitor name at entry #2" in output
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_failed_startup_creates_no_spans(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_AUTH_HEADER", CANARY)
    monkeypatch.setenv("TEST_BAD_TIMEOUT", CANARY)
    config_path = _write(
        tmp_path / "monitors.yaml",
        """monitors:
  - name: sample
    application: demo
    url: http://demo.test/check
    headers:
      X-Authorization: "${TEST_AUTH_HEADER}"
    timeout_seconds: "${TEST_BAD_TIMEOUT}"
""",
    )
    monkeypatch.setattr(main, "_config_path", str(config_path))
    monkeypatch.setattr(main, "setup_tracing", lambda: None)
    monkeypatch.setattr(telemetry, "_logging_configured", True)

    tracer_requests: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def unexpected_tracer(*args, **kwargs):
        tracer_requests.append((args, kwargs))
        raise AssertionError("configuration failures must not create spans")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", unexpected_tracer)

    with pytest.raises(ConfigurationError):
        with TestClient(main.app):
            pass

    assert tracer_requests == []


def test_failed_startup_logs_metrics_and_diagnostics_stay_clean(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_AUTH_HEADER", CANARY)
    monkeypatch.setenv("TEST_BAD_TIMEOUT", CANARY)
    config_path = _write(
        tmp_path / "monitors.yaml",
        """monitors:
  - name: sample
    application: demo
    url: http://demo.test/check
    headers:
      X-Authorization: "${TEST_AUTH_HEADER}"
    timeout_seconds: "${TEST_BAD_TIMEOUT}"
""",
    )
    monkeypatch.setattr(main, "_config_path", str(config_path))
    monkeypatch.setattr(telemetry, "_logging_configured", True)

    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = CaptureHandler()
    root = logging.getLogger()
    original_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.ERROR)
    try:
        with pytest.raises(ConfigurationError) as caught:
            with TestClient(main.app):
                pass
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)

    formatted_logs = "\n".join(JsonFormatter().format(record) for record in records)
    metrics_output = generate_latest(main.metrics.registry).decode("utf-8")
    output = _diagnostics(caught.value)
    _assert_clean(CANARY, output, formatted_logs, metrics_output)
    assert any(record.msg == "configuration.load_failed" for record in records)
