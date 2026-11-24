"""Configuration loading and environment expansion tests."""

from __future__ import annotations

import pytest

from ximiowatch.config import ConfigurationError, load_monitors

CONFIG_TEMPLATE = """
monitors:
  - name: sample
    application: demo
    url: "${DEMO_API_URL:http://localhost:8001}/check"
    method: get
    headers:
      X-Demo-Token: "${DEMO_TOKEN:default-token}"
    interval_seconds: 5
    timeout_seconds: 1
    assertions:
      - type: http_status
        value: 200
"""


def test_environment_references_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_API_URL", "http://fake.test:1234")
    monkeypatch.setenv("DEMO_TOKEN", "secret-from-env")
    path = tmp_path / "monitors.yaml"
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")

    monitors = load_monitors(path)

    assert len(monitors) == 1
    monitor = monitors[0]
    assert monitor.url == "http://fake.test:1234/check"
    assert monitor.headers == {"X-Demo-Token": "secret-from-env"}
    assert monitor.method == "GET"


def test_missing_environment_variable_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_API_URL", raising=False)
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    path = tmp_path / "monitors.yaml"
    path.write_text(
        'monitors:\n  - name: x\n    application: y\n    url: "${MISSING_SETTING}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="MISSING_SETTING"):
        load_monitors(path)


def test_invalid_assertion_is_rejected(tmp_path):
    path = tmp_path / "monitors.yaml"
    path.write_text(
        """monitors:
  - name: x
    application: y
    url: http://fake.test/check
    assertions:
      - type: json_exists
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="requires a 'path'"):
        load_monitors(path)
