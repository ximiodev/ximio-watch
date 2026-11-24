"""Focused assertion and check-result tests. No external network services."""

from __future__ import annotations

import httpx

from tests.conftest import json_response
from tests.helpers import make_monitor
from ximiowatch.assertions import AssertionContext, evaluate
from ximiowatch.config import Assertion
from ximiowatch.engine import check_monitor


def sample(registry, name: str, labels: dict[str, str]) -> float | None:
    return registry.get_sample_value(name, labels)


BASE_LABELS = {"monitor": "monitor-a", "application": "app-a"}


async def test_successful_assertions(metrics, registry):
    monitor = make_monitor(
        assertions=[
            Assertion(type="http_status", value=200),
            Assertion(type="json_equals", path="products.0.available", value=True),
            Assertion(type="max_response_time", value=1.0),
        ]
    )
    payload = {"products": [{"available": True}]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: json_response(payload))) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "success"
    assert result.http_ok is True
    assert result.functional_ok is True
    assert result.silent_failure is False
    assert sample(registry, "ximiowatch_functional_up", BASE_LABELS) == 1.0
    assert sample(registry, "ximiowatch_checks_total", {**BASE_LABELS, "outcome": "success"}) == 1.0
    assert sample(registry, "ximiowatch_last_success_timestamp_seconds", BASE_LABELS) > 0


async def test_failed_assertion_on_http_200_is_a_silent_failure(metrics, registry):
    monitor = make_monitor(
        assertions=[
            Assertion(type="http_status", value=200),
            Assertion(type="json_equals", path="authorized", value=True),
        ]
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response({"authorized": False}))
    ) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.status_code == 200
    assert result.http_ok is True
    assert result.functional_ok is False
    assert result.outcome == "functional_failure"
    assert result.silent_failure is True
    assert result.failed_types == ("json_equals",)
    assert sample(registry, "ximiowatch_http_up", BASE_LABELS) == 1.0
    assert sample(registry, "ximiowatch_functional_up", BASE_LABELS) == 0.0
    assert sample(registry, "ximiowatch_silent_failures_total", BASE_LABELS) == 1.0
    assert (
        sample(
            registry,
            "ximiowatch_assertion_failures_total",
            {**BASE_LABELS, "type": "json_equals"},
        )
        == 1.0
    )


async def test_missing_json_field(metrics):
    monitor = make_monitor(assertions=[Assertion(type="json_exists", path="products")])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response({"status": "ready"}))
    ) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.functional_ok is False
    assert result.failed_types == ("json_exists",)


async def test_invalid_json_response(metrics, registry):
    monitor = make_monitor(
        assertions=[
            Assertion(type="http_status", value=200),
            Assertion(type="json_exists", path="products"),
        ]
    )
    response = httpx.Response(
        200, text="this is not json", request=httpx.Request("GET", "http://demo.test/check")
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.http_ok is True
    assert result.functional_ok is False
    assert result.failed_types == ("invalid_json",)
    assert result.silent_failure is True
    assert (
        sample(
            registry,
            "ximiowatch_assertion_failures_total",
            {**BASE_LABELS, "type": "invalid_json"},
        )
        == 1.0
    )


async def test_http_failure_is_not_counted_as_silent(metrics, registry):
    monitor = make_monitor()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response({"error": "down"}, status_code=503))
    ) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "http_failure"
    assert result.http_ok is False
    assert result.silent_failure is False
    assert sample(registry, "ximiowatch_http_up", BASE_LABELS) == 0.0


async def test_timeout_handling(metrics, registry):
    monitor = make_monitor(timeout_seconds=0.05)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "timeout"
    assert result.error_type == "TimeoutException"
    assert result.http_ok is False
    assert result.functional_ok is False
    assert result.silent_failure is False
    assert (
        sample(registry, "ximiowatch_checks_total", {**BASE_LABELS, "outcome": "timeout"}) == 1.0
    )


async def test_conditional_assertions(metrics):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="authorized", value=True),
        then=Assertion(type="json_exists", path="authorization_code"),
    )
    monitor = make_monitor(assertions=[conditional])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: json_response({"authorized": False})
        )
    ) as client:
        skipped = await check_monitor(monitor, client, metrics=metrics)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: json_response({"authorized": True})
        )
    ) as client:
        triggered = await check_monitor(monitor, client, metrics=metrics)

    assert skipped.outcome == "not_evaluated"
    assert skipped.functional_state == "unknown"
    assert skipped.failed_types == ()
    assert triggered.outcome == "functional_failure"
    assert triggered.functional_state == "down"
    assert triggered.failed_types == ("json_exists",)


def test_conditional_otherwise_branch_and_nested_paths():
    assertion = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="device.status", value="offline"),
        then=Assertion(type="json_exists", path="device.codes.0"),
        otherwise=Assertion(type="json_equals", path="device.status", value="online"),
    )
    context = AssertionContext(
        status_code=200,
        elapsed_seconds=0.1,
        payload={"device": {"status": "online"}},
        json_valid=True,
    )

    results = evaluate(assertion, context)

    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].type == "json_equals"


def test_numeric_assertions_use_nested_paths():
    context = AssertionContext(
        status_code=200,
        elapsed_seconds=0.1,
        payload={"device": {"readings": [{"value": 12}]}},
        json_valid=True,
    )

    passed = evaluate(
        Assertion(type="numeric_gt", path="device.readings.0.value", value=10), context
    )
    failed = evaluate(
        Assertion(type="numeric_lt", path="device.readings.0.value", value=10), context
    )

    assert passed[0].passed is True
    assert failed[0].passed is False
