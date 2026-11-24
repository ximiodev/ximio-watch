"""YAML monitor configuration with environment variable expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssertionType = Literal[
    "http_status",
    "max_response_time",
    "json_exists",
    "json_equals",
    "json_not_empty",
    "numeric_gt",
    "numeric_lt",
    "conditional",
]

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


class ConfigurationError(ValueError):
    """Raised when the monitor configuration cannot be loaded."""


def _replace_env_reference(match: re.Match[str]) -> str:
    name, default = match.group(1), match.group(2)
    if name in os.environ:
        return os.environ[name]
    if default is not None:
        return default
    raise ConfigurationError(f"Environment variable '{name}' is not set and has no default")


def expand_environment(value: Any) -> Any:
    """Recursively expand ${NAME} and ${NAME:default} references in YAML data."""
    if isinstance(value, dict):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if isinstance(value, str):
        return _ENV_REFERENCE.sub(_replace_env_reference, value)
    return value


class Assertion(BaseModel):
    """One assertion. Conditional assertions contain nested assertions."""

    model_config = ConfigDict(extra="forbid")

    type: AssertionType
    path: str | None = None
    value: Any = None
    when: Assertion | None = None
    then: Assertion | None = None
    otherwise: Assertion | None = None

    @model_validator(mode="after")
    def validate_for_type(self) -> Assertion:
        kind = self.type
        needs_path = kind in {
            "json_exists",
            "json_equals",
            "json_not_empty",
            "numeric_gt",
            "numeric_lt",
        }
        needs_value = kind in {
            "http_status",
            "max_response_time",
            "json_equals",
            "numeric_gt",
            "numeric_lt",
        }

        if needs_path and not self.path:
            raise ValueError(f"'{kind}' requires a 'path'")
        if needs_value and self.value is None:
            raise ValueError(f"'{kind}' requires a 'value'")
        if kind == "http_status" and (
            isinstance(self.value, bool) or not isinstance(self.value, int)
        ):
            raise ValueError("'http_status' requires an integer 'value'")
        if kind in {"max_response_time", "numeric_gt", "numeric_lt"} and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise ValueError(f"'{kind}' requires a numeric 'value'")
        if kind == "conditional" and (self.when is None or self.then is None):
            raise ValueError("'conditional' requires 'when' and 'then'")
        return self


Assertion.model_rebuild()


class MonitorConfig(BaseModel):
    """Definition of a single synthetic monitor."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    application: str = Field(min_length=1)
    url: str = Field(min_length=1)
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    interval_seconds: float = Field(default=10, gt=0)
    timeout_seconds: float = Field(default=2, gt=0)
    assertions: list[Assertion] = Field(default_factory=list)

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.strip().upper()


def load_monitors(path: str | Path) -> list[MonitorConfig]:
    """Load and validate monitor definitions from a YAML file."""
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ConfigurationError(f"Cannot read configuration file: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError("Configuration file is not valid YAML") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("monitors"), list):
        raise ConfigurationError("Configuration must contain a 'monitors' list")
    if not raw["monitors"]:
        raise ConfigurationError("At least one monitor must be configured")

    monitors: list[MonitorConfig] = []
    names: set[str] = set()
    for index, item in enumerate(expand_environment(raw["monitors"])):
        label = item.get("name", f"#{index + 1}") if isinstance(item, dict) else f"#{index + 1}"
        try:
            monitor = MonitorConfig.model_validate(item)
        except Exception as exc:
            raise ConfigurationError(f"Invalid monitor '{label}': {exc}") from exc
        if monitor.name in names:
            raise ConfigurationError(f"Duplicate monitor name '{monitor.name}'")
        names.add(monitor.name)
        monitors.append(monitor)
    return monitors
