"""Test helpers shared across the focused test modules."""

from __future__ import annotations

from ximiowatch.config import Assertion, MonitorConfig

DEFAULT_URL = "http://demo.test/check"


def make_monitor(
    name: str = "monitor-a",
    application: str = "app-a",
    assertions: list[Assertion] | None = None,
    **overrides: object,
) -> MonitorConfig:
    values: dict[str, object] = {
        "name": name,
        "application": application,
        "url": DEFAULT_URL,
        "assertions": assertions or [Assertion(type="http_status", value=200)],
    }
    values.update(overrides)
    return MonitorConfig.model_validate(values)
