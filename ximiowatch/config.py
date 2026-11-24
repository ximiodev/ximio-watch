"""YAML monitor configuration with environment variable expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterator, Literal
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

AssertionType = Literal[
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
]

JSON_ASSERTIONS = frozenset(
    {
        "json_exists",
        "json_equals",
        "json_not_empty",
        "numeric_gt",
        "numeric_lt",
    }
)
TEXT_ASSERTIONS = frozenset({"text_contains", "text_not_contains"})
CONTENT_TYPE_ASSERTIONS = frozenset({"content_type_contains"})
CONTENT_ASSERTIONS = frozenset(TEXT_ASSERTIONS | CONTENT_TYPE_ASSERTIONS)
BUSINESS_ASSERTIONS = frozenset(JSON_ASSERTIONS | CONTENT_ASSERTIONS)
PERFORMANCE_ASSERTIONS = frozenset({"max_response_time"})
BODY_ASSERTIONS = frozenset(JSON_ASSERTIONS | TEXT_ASSERTIONS)

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")

_SAFE_FIELDS = frozenset(
    {
        "monitors",
        "name",
        "application",
        "url",
        "method",
        "headers",
        "interval_seconds",
        "timeout_seconds",
        "max_response_bytes",
        "assertions",
        "type",
        "path",
        "value",
        "when",
        "then",
        "otherwise",
    }
)

_ERROR_DESCRIPTIONS = {
    "extra_forbidden": "contains an unsupported field",
    "greater_than": "must be greater than zero",
    "int_parsing": "must be an integer",
    "int_type": "must be an integer",
    "bool_parsing": "must be a boolean",
    "bool_type": "must be a boolean",
    "dict_type": "must be a mapping",
    "float_parsing": "must be a number",
    "float_type": "must be a number",
    "list_type": "must be a list",
    "literal_error": "has an unsupported value",
    "missing": "is required",
    "model_type": "must be an object",
    "string_too_short": "must not be empty",
    "string_type": "must be a string",
}


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


def _contains_type(assertion: Assertion, types: frozenset[str]) -> bool:
    """Report whether an assertion tree contains one of the requested types."""
    if assertion.type in types:
        return True
    if assertion.type != "conditional":
        return False
    for child in (assertion.when, assertion.then, assertion.otherwise):
        if child is not None and _contains_type(child, types):
            return True
    return False


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
        needs_path = kind in JSON_ASSERTIONS
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
        if kind in CONTENT_ASSERTIONS:
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError(f"'{kind}' requires a non-empty string 'value'")
            if self.path is not None:
                raise ValueError(f"'{kind}' does not accept 'path'")
        if kind != "conditional" and (
            self.when is not None or self.then is not None or self.otherwise is not None
        ):
            raise ValueError("only 'conditional' may define nested branches")
        if kind == "conditional":
            if self.when is None or self.then is None:
                raise ValueError("'conditional' requires 'when' and 'then'")
            if self.path is not None or self.value is not None:
                raise ValueError("'conditional' does not accept 'path' or 'value'")
            for child in (self.when, self.then, self.otherwise):
                if child is not None and _contains_type(child, frozenset({"http_status"})):
                    raise ValueError("'http_status' is not supported inside a conditional")
        return self


Assertion.model_rebuild()


def iter_assertions(
    assertions: Assertion | list[Assertion],
) -> Iterator[Assertion]:
    """Walk every assertion, including all conditional branches and guards."""

    def walk(item: Assertion) -> Iterator[Assertion]:
        yield item
        if item.type == "conditional":
            for child in (item.when, item.then, item.otherwise):
                if child is not None:
                    yield from walk(child)

    items = [assertions] if isinstance(assertions, Assertion) else assertions
    for item in items:
        yield from walk(item)


def iter_required_assertions(assertions: list[Assertion]) -> Iterator[Assertion]:
    """Walk assertions that can produce a required observation.

    Conditional guards only select a branch. They are executed, but they are not
    required business results; only `then` and `otherwise` subtrees are required.
    """

    def walk(item: Assertion) -> Iterator[Assertion]:
        if item.type != "conditional":
            yield item
            return
        for child in (item.then, item.otherwise):
            if child is not None:
                yield from walk(child)

    for item in assertions:
        yield from walk(item)


def configured_assertion_types(assertions: list[Assertion]) -> frozenset[str]:
    """Collect every configured assertion type recursively, including guards."""
    return frozenset(item.type for item in iter_assertions(assertions))


def required_assertion_types(assertions: list[Assertion]) -> frozenset[str]:
    """Collect assertion types that can produce a required observation."""
    return frozenset(item.type for item in iter_required_assertions(assertions))


def _validate_url(value: str) -> str:
    """Validate an operator-supplied URL without retaining its value in errors."""
    candidate = value.strip()
    if not candidate:
        raise ValueError("must not be empty")
    if any(character.isspace() for character in candidate):
        raise ValueError("must not contain whitespace")

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        raise ValueError("must be a valid HTTP or HTTPS URL") from None

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("must not contain embedded credentials")
    if not parsed.hostname:
        raise ValueError("must contain a hostname")

    try:
        # Accessing port and constructing httpx.URL rejects malformed hosts,
        # ports and IPv6 literals without relying on the raw value in errors.
        _ = parsed.port
        httpx.URL(candidate)
    except (ValueError, httpx.InvalidURL):
        raise ValueError("must be a valid HTTP or HTTPS URL") from None
    return candidate


class MonitorConfig(BaseModel):
    """Definition of a single synthetic monitor."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    application: str = Field(min_length=1)
    url: str
    method: Literal["GET", "HEAD"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    interval_seconds: float = Field(default=10, gt=0)
    timeout_seconds: float = Field(default=2, gt=0)
    max_response_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    assertions: list[Assertion] = Field(default_factory=list)

    @field_validator("method", mode="before")
    @classmethod
    def normalize_method(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_url(value)

    @model_validator(mode="after")
    def validate_body_assertions(self) -> MonitorConfig:
        if self.method == "HEAD" and any(
            item.type in BODY_ASSERTIONS for item in iter_assertions(self.assertions)
        ):
            raise ValueError("HEAD monitors cannot define body assertions")
        return self


def _safe_location(location: tuple[Any, ...] | list[Any]) -> str:
    """Return a field path containing only known schema names and list indexes."""
    result = ""
    for item in location:
        if isinstance(item, int) and not isinstance(item, bool):
            token = f"[{item}]"
        elif isinstance(item, str) and item in _SAFE_FIELDS:
            token = item
        else:
            token = "<field>"
        if not result or token.startswith("["):
            result += token
        else:
            result += f".{token}"
    return result or "<configuration>"


def _safe_validation_diagnostics(validation_error: ValidationError) -> str:
    """Build diagnostics from error categories, never from submitted values."""
    diagnostics: list[str] = []
    for error in validation_error.errors():
        location = _safe_location(error.get("loc", ()))
        error_type = str(error.get("type", ""))
        if error_type == "value_error":
            # Custom validators in this module only emit static, value-free messages.
            description = str(error.get("msg", "is invalid"))
        else:
            description = _ERROR_DESCRIPTIONS.get(error_type, "is invalid")
        diagnostics.append(f"{location} {description}")
    return "; ".join(diagnostics) or "invalid definition"


def _read_configuration(config_path: Path) -> tuple[Any | None, str | None]:
    """Read YAML and return a sanitized problem description instead of exceptions."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, "Cannot read configuration file"

    try:
        return yaml.safe_load(text) or {}, None
    except yaml.YAMLError:
        return None, "Configuration file is not valid YAML"
    except Exception:
        return None, "Configuration file could not be parsed"


def _validate_monitor(item: Any, index: int) -> MonitorConfig:
    """Validate one monitor without retaining raw exception context."""
    try:
        return MonitorConfig.model_validate(item)
    except ValidationError as validation_error:
        diagnostics = _safe_validation_diagnostics(validation_error)
    raise ConfigurationError(f"Invalid monitor at entry #{index + 1}: {diagnostics}")


def load_monitors(path: str | Path) -> list[MonitorConfig]:
    """Load and validate monitor definitions from a YAML file."""
    config_path = Path(path)
    raw, problem = _read_configuration(config_path)
    if problem is not None:
        raise ConfigurationError(problem)

    if not isinstance(raw, dict) or not isinstance(raw.get("monitors"), list):
        raise ConfigurationError("Configuration must contain a 'monitors' list")
    if not raw["monitors"]:
        raise ConfigurationError("At least one monitor must be configured")

    monitors: list[MonitorConfig] = []
    names: set[str] = set()
    for index, item in enumerate(expand_environment(raw["monitors"])):
        monitor = _validate_monitor(item, index)
        if monitor.name in names:
            raise ConfigurationError(f"Duplicate monitor name at entry #{index + 1}")
        names.add(monitor.name)
        monitors.append(monitor)
    return monitors
