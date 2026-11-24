"""Small, declarative assertion engine. No eval or expression execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from .config import (
    Assertion,
    BUSINESS_ASSERTIONS,
    CONTENT_ASSERTIONS,
    JSON_ASSERTIONS,
    PERFORMANCE_ASSERTIONS,
    TEXT_ASSERTIONS,
)

CONTENT_ASSERTIONS = frozenset(CONTENT_ASSERTIONS)
BUSINESS_ASSERTIONS = frozenset(BUSINESS_ASSERTIONS)
PERFORMANCE_ASSERTIONS = frozenset(PERFORMANCE_ASSERTIONS)
TEXT_ASSERTIONS = frozenset(TEXT_ASSERTIONS)
JSON_ASSERTIONS = frozenset(JSON_ASSERTIONS)

_MISSING = object()


@dataclass(frozen=True)
class AssertionContext:
    status_code: int | None
    elapsed_seconds: float
    payload: Any = None
    json_valid: bool = False
    text: str | None = None
    text_decodable: bool = False
    content_type: str | None = None


@dataclass(frozen=True)
class AssertionResult:
    passed: bool
    type: str
    role: Literal["guard", "required"] = "required"


@dataclass(frozen=True)
class Evaluation:
    """Executed assertions and whether any required check was incomplete.

    `results` contains only required assertions. `all_results` also contains
    executed guards so latency observations used for branching are not lost.
    """

    results: tuple[AssertionResult, ...] = ()
    all_results: tuple[AssertionResult, ...] = ()
    incomplete: bool = False
    incomplete_types: frozenset[str] = frozenset()


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
    elif kind == "content_type_contains":
        expected = assertion.value
        actual = context.content_type
        passed = (
            isinstance(expected, str)
            and bool(expected)
            and actual is not None
            and expected.casefold() in actual.casefold()
        )
    elif kind in TEXT_ASSERTIONS:
        expected = assertion.value
        passed = (
            isinstance(expected, str)
            and context.text is not None
            and (expected in context.text if kind == "text_contains" else expected not in context.text)
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


def evaluate_detailed(
    assertions: Assertion | list[Assertion], context: AssertionContext
) -> Evaluation:
    """Evaluate assertions recursively, separating guards from required checks."""

    def walk(item: Assertion, role: Literal["guard", "required"]) -> Evaluation:
        if item.type != "conditional":
            # An unevaluable required body check is not a pass and not a failure.
            if item.type in JSON_ASSERTIONS and not context.json_valid:
                return Evaluation(incomplete=True, incomplete_types=frozenset({item.type}))
            if item.type in TEXT_ASSERTIONS and not context.text_decodable:
                return Evaluation(incomplete=True, incomplete_types=frozenset({item.type}))
            result = replace(evaluate_one(item, context), role=role)
            if role == "guard":
                return Evaluation(all_results=(result,))
            return Evaluation(results=(result,), all_results=(result,))

        guard = walk(item.when, "guard") if item.when is not None else Evaluation()
        # An empty guard is not a successful guard; all([]) must never enable then.
        guard_passed = (
            bool(guard.all_results)
            and not guard.incomplete
            and all(result.passed for result in guard.all_results)
        )
        if guard.incomplete:
            return Evaluation(
                all_results=guard.all_results,
                incomplete=True,
                incomplete_types=guard.incomplete_types,
            )
        branch = item.then if guard_passed else item.otherwise
        if branch is None:
            return Evaluation(all_results=guard.all_results)
        selected = walk(branch, role)
        return Evaluation(
            results=selected.results,
            all_results=(*guard.all_results, *selected.all_results),
            incomplete=selected.incomplete,
            incomplete_types=selected.incomplete_types,
        )

    items = [assertions] if isinstance(assertions, Assertion) else assertions
    required: list[AssertionResult] = []
    executed: list[AssertionResult] = []
    incomplete = False
    incomplete_types: set[str] = set()
    for item in items:
        evaluation = walk(item, "required")
        required.extend(evaluation.results)
        executed.extend(evaluation.all_results)
        incomplete = incomplete or evaluation.incomplete
        incomplete_types.update(evaluation.incomplete_types)
    return Evaluation(
        results=tuple(required),
        all_results=tuple(executed),
        incomplete=incomplete,
        incomplete_types=frozenset(incomplete_types),
    )


def evaluate(
    assertions: Assertion | list[Assertion], context: AssertionContext
) -> list[AssertionResult]:
    """Evaluate required assertions, including selected conditional branches."""
    return list(evaluate_detailed(assertions, context).results)
