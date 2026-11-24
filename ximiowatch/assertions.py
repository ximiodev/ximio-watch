"""Small, declarative assertion engine. No eval or expression execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Assertion

_MISSING = object()

BUSINESS_ASSERTIONS = frozenset(
    {
        "json_exists",
        "json_equals",
        "json_not_empty",
        "numeric_gt",
        "numeric_lt",
    }
)
PERFORMANCE_ASSERTIONS = frozenset({"max_response_time"})
JSON_ASSERTIONS = frozenset(BUSINESS_ASSERTIONS | PERFORMANCE_ASSERTIONS)


@dataclass(frozen=True)
class AssertionContext:
    status_code: int | None
    elapsed_seconds: float
    payload: Any = None
    json_valid: bool = False


@dataclass(frozen=True)
class AssertionResult:
    passed: bool
    type: str
    detail: str = ""


def resolve_path(payload: Any, path: str) -> Any:
    """Resolve a dotted JSON path. Numeric segments index into lists."""
    current = payload
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return _MISSING
            current = current[segment]
        elif isinstance(current, (list, tuple)):
            if not segment.isdigit():
                return _MISSING
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    return actual == expected


def _compare_number(actual: Any, expected: Any, *, greater: bool) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        return False
    return actual > expected if greater else actual < expected


def evaluate_one(assertion: Assertion, context: AssertionContext) -> AssertionResult:
    """Evaluate one leaf assertion."""
    kind = assertion.type
    if kind == "http_status":
        expected = assertion.value if assertion.value is not None else 200
        passed = context.status_code is not None and context.status_code == expected
    elif kind == "max_response_time":
        limit = assertion.value if assertion.value is not None else float("inf")
        passed = (
            isinstance(limit, (int, float))
            and not isinstance(limit, bool)
            and context.elapsed_seconds <= limit
        )
    elif not context.json_valid:
        passed = False
    else:
        value = resolve_path(context.payload, assertion.path or "")
        if kind == "json_exists":
            # A present field with value null satisfies "exists".
            passed = value is not _MISSING
        elif kind == "json_equals":
            passed = value is not _MISSING and _equals(value, assertion.value)
        elif kind == "json_not_empty":
            passed = (
                value is not _MISSING
                and value is not None
                and not (isinstance(value, (str, list, tuple, dict)) and len(value) == 0)
            )
        elif kind == "numeric_gt":
            passed = _compare_number(value, assertion.value, greater=True)
        elif kind == "numeric_lt":
            passed = _compare_number(value, assertion.value, greater=False)
        else:
            passed = False
    return AssertionResult(passed=passed, type=kind)


def evaluate(
    assertions: Assertion | list[Assertion], context: AssertionContext
) -> list[AssertionResult]:
    """Evaluate assertions, including conditional branches."""
    items = [assertions] if isinstance(assertions, Assertion) else assertions
    results: list[AssertionResult] = []
    for assertion in items:
        if assertion.type != "conditional":
            results.append(evaluate_one(assertion, context))
            continue

        if assertion.when is None:  # Prevented by config validation; kept defensive.
            continue
        guard_results = evaluate([assertion.when], context)
        guard_passed = all(result.passed for result in guard_results)
        branch = assertion.then if guard_passed else assertion.otherwise
        if branch is not None:
            results.extend(evaluate([branch], context))
    return results
