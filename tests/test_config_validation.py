"""Configuration validation for read-only monitoring targets."""

from __future__ import annotations

import traceback

import pytest
import yaml
from pydantic import ValidationError

from ximiowatch.config import ConfigurationError, MonitorConfig, load_monitors

CANARY = "SYNTHETIC-URL-CANARY-DO-NOT-LOG-4F39A1"


def _config(url: str, method: str = "GET") -> dict[str, object]:
    return {
        "name": "sample",
        "application": "demo",
        "url": url,
        "method": method,
    }


def _diagnostics(exc: BaseException) -> str:
    return "\n".join(
        [
            str(exc),
            repr(exc.__cause__),
            repr(exc.__context__),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_unsupported_methods_are_rejected(method: str):
    with pytest.raises(ValidationError):
        MonitorConfig.model_validate(_config("http://demo.test/check", method))


def test_unsupported_method_diagnostics_are_sanitized(tmp_path):
    path = tmp_path / "monitors.yaml"
    path.write_text(
        """monitors:
  - name: sample
    application: demo
    url: http://demo.test/check
    method: POST
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    assert "method has an unsupported value" in str(caught.value)


@pytest.mark.parametrize("method, expected", [("get", "GET"), (" head ", "HEAD")])
def test_supported_methods_are_normalized(method: str, expected: str):
    monitor = MonitorConfig.model_validate(_config("http://demo.test/check", method))

    assert monitor.method == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://demo.test/check",
        "//demo.test/check",
        "not-a-url",
        "http:///check",
        "http://demo.test:not-a-port/check",
        "http://demo.test/path with space",
        "http://[invalid-ipv6/check",
    ],
)
def test_invalid_urls_are_rejected(url: str):
    with pytest.raises(ValidationError):
        MonitorConfig.model_validate(_config(url))


@pytest.mark.parametrize("url", [
    "http://demo-api:8001/check",
    "https://192.168.1.10/healthz",
    "http://[::1]:8000/check",
    "https://localhost/check",
])
def test_private_and_service_targets_remain_valid(url: str):
    monitor = MonitorConfig.model_validate(_config(url))

    assert monitor.url == url


def test_embedded_credentials_are_rejected_without_leaking_them(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_URL_SECRET", CANARY)
    path = tmp_path / "monitors.yaml"
    path.write_text(
        """monitors:
  - name: sample
    application: demo
    url: "http://user:${TEST_URL_SECRET}@demo.test/check"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    output = _diagnostics(caught.value)
    assert CANARY not in output
    assert "embedded credentials" in output
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_response_size_limit_is_configurable_and_bounded():
    default = MonitorConfig.model_validate(_config("http://demo.test/check"))
    custom = MonitorConfig.model_validate(
        {**_config("http://demo.test/check"), "max_response_bytes": 4096}
    )

    assert default.max_response_bytes == 2 * 1024 * 1024
    assert custom.max_response_bytes == 4096
    with pytest.raises(ValidationError):
        MonitorConfig.model_validate(
            {**_config("http://demo.test/check"), "max_response_bytes": 0}
        )


def test_unknown_tls_option_is_rejected(tmp_path):
    path = tmp_path / "monitors.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "monitors": [
                    {
                        **_config("https://demo.test/check"),
                        "verify_tls": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    assert "unsupported field" in str(caught.value)
