"""Stage 3 regression tests for explicit health states and content assertions."""

from __future__ import annotations

import json
import logging
import traceback

import httpx
import pytest
from prometheus_client import CollectorRegistry, generate_latest

from tests.conftest import json_response
from tests.helpers import make_monitor
from ximiowatch.config import Assertion, ConfigurationError, MonitorConfig, load_monitors
from ximiowatch.engine import MonitorEngine, check_monitor
from ximiowatch.metrics import KNOWN_ASSERTION_TYPES, KNOWN_OUTCOMES, Metrics

BASE_LABELS = {"monitor": "monitor-a", "application": "app-a"}
CANARY = "SYNTHETIC-ASSERTION-CANARY-DO-NOT-LOG-7D12A9"


def _response(
    status: int = 200,
    *,
    payload: object | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    body = content if content is not None else json.dumps(payload).encode("utf-8")
    return httpx.Response(
        status,
        content=body,
        headers=headers or {},
        request=httpx.Request("GET", "http://demo.test/check"),
    )


async def _run(monitor, response, metrics=None, consecutive_failures=0):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response)
    ) as client:
        return await check_monitor(
            monitor, client, metrics=metrics, consecutive_failures=consecutive_failures
        )


def _sample(registry, name: str, labels: dict[str, str] | None = None) -> float | None:
    return registry.get_sample_value(name, labels or BASE_LABELS)


async def test_http_only_monitor_has_functional_and_performance_not_configured(
    metrics, registry
):
    result = await _run(make_monitor(), json_response({"ok": True}), metrics)

    assert result.http_state == "up"
    assert result.functional_state == "not_configured"
    assert result.functional_ok is True
    assert result.performance_state == "not_configured"
    assert result.outcome == "success"
    assert result.healthy is True
    assert _sample(registry, "ximiowatch_functional_configured") == 0.0
    assert _sample(registry, "ximiowatch_functional_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_performance_configured") == 0.0
    assert _sample(registry, "ximiowatch_performance_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_functional_up") is None
    assert _sample(registry, "ximiowatch_performance_up") is None


async def test_healthy_json_business_assertion(metrics, registry):
    monitor = make_monitor(
        assertions=[Assertion(type="json_equals", path="ready", value=True)]
    )
    result = await _run(monitor, json_response({"ready": True}), metrics)

    assert result.functional_state == "up"
    assert result.functional_ok is True
    assert result.silent_failure is False
    assert result.outcome == "success"
    assert result.healthy is True
    assert _sample(registry, "ximiowatch_functional_up") == 1.0
    assert _sample(registry, "ximiowatch_functional_evaluated") == 1.0
    assert _sample(registry, "ximiowatch_last_success_timestamp_seconds") > 0


async def test_failing_json_business_assertion_with_http_200(metrics, registry):
    monitor = make_monitor(
        assertions=[Assertion(type="json_equals", path="ready", value=True)]
    )
    result = await _run(monitor, json_response({"ready": False}), metrics)

    assert result.http_state == "up"
    assert result.functional_state == "down"
    assert result.silent_failure is True
    assert result.outcome == "functional_failure"
    assert result.healthy is False
    assert _sample(registry, "ximiowatch_functional_up") == 0.0
    assert _sample(registry, "ximiowatch_silent_failures_total") == 1.0


async def test_http_failure_does_not_claim_business_evaluation(metrics, registry):
    monitor = make_monitor(
        assertions=[Assertion(type="json_equals", path="ready", value=True)]
    )
    result = await _run(monitor, json_response({"ready": True}, status_code=503), metrics)

    assert result.http_state == "down"
    assert result.functional_state == "unknown"
    assert result.functional_ok is False
    assert result.silent_failure is False
    assert result.outcome == "http_failure"
    assert result.business_failed_types == ()
    assert _sample(registry, "ximiowatch_functional_configured") == 1.0
    assert _sample(registry, "ximiowatch_functional_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_functional_up") is None


async def test_all_conditional_business_assertions_skipped(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="apply"),
        then=Assertion(type="json_exists", path="code"),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(monitor, json_response({"mode": "skip"}), metrics)

    assert result.http_state == "up"
    assert result.functional_state == "unknown"
    assert result.outcome == "not_evaluated"
    assert result.healthy is False
    assert result.silent_failure is False
    assert _sample(registry, "ximiowatch_functional_configured") == 1.0
    assert _sample(registry, "ximiowatch_functional_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_functional_up") is None
    assert _sample(registry, "ximiowatch_last_success_timestamp_seconds") is None


async def test_conditional_branch_with_successful_business_assertion(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="apply"),
        then=Assertion(type="json_equals", path="code", value="ok"),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(monitor, json_response({"mode": "apply", "code": "ok"}), metrics)

    assert result.functional_state == "up"
    assert result.outcome == "success"
    assert result.healthy is True
    assert _sample(registry, "ximiowatch_functional_up") == 1.0


async def test_conditional_branch_with_failing_business_assertion(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="apply"),
        then=Assertion(type="json_equals", path="code", value="ok"),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(monitor, json_response({"mode": "apply", "code": "bad"}), metrics)

    assert result.functional_state == "down"
    assert result.outcome == "functional_failure"
    assert result.silent_failure is True
    assert _sample(registry, "ximiowatch_functional_up") == 0.0


async def test_conditional_otherwise_branch_is_executed(metrics):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="apply"),
        then=Assertion(type="json_equals", path="code", value="ok"),
        otherwise=Assertion(type="json_equals", path="mode", value="skip"),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(monitor, json_response({"mode": "skip"}), metrics)

    assert result.functional_state == "up"
    assert result.outcome == "success"
    assert result.failed_types == ()


async def test_nested_performance_assertion_is_discovered_and_executed(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="slow"),
        then=Assertion(type="max_response_time", value=0),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(monitor, json_response({"mode": "slow"}), metrics)

    assert result.performance_configured is True
    assert result.performance_evaluated is True
    assert result.performance_state == "down"
    assert result.outcome == "performance_failure"
    assert result.silent_failure is False
    assert _sample(registry, "ximiowatch_performance_configured") == 1.0
    assert _sample(registry, "ximiowatch_performance_evaluated") == 1.0
    assert _sample(registry, "ximiowatch_performance_up") == 0.0
    assert _sample(registry, "ximiowatch_silent_failures_total") is None


async def test_configured_performance_with_no_applicable_branch_is_unknown(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="slow"),
        then=Assertion(type="max_response_time", value=0),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(monitor, json_response({"mode": "fast"}), metrics)

    assert result.performance_configured is True
    assert result.performance_evaluated is False
    assert result.performance_state == "unknown"
    assert result.outcome == "not_evaluated"
    assert _sample(registry, "ximiowatch_performance_up") is None
    assert _sample(registry, "ximiowatch_performance_evaluated") == 0.0


async def test_invalid_json_does_not_bypass_conditional_json_requirement(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="authorized", value=True),
        then=Assertion(type="json_exists", path="code"),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(
        monitor, _response(content=b"not json", headers={"Content-Type": "text/plain"}), metrics
    )

    assert result.functional_state == "down"
    assert "invalid_json" in result.business_failed_types
    assert result.silent_failure is True
    assert result.outcome == "functional_failure"
    assert _sample(registry, "ximiowatch_functional_evaluated") == 1.0
    assert (
        _sample(
            registry,
            "ximiowatch_assertion_failures_total",
            {**BASE_LABELS, "type": "invalid_json"},
        )
        == 1.0
    )


async def test_missing_null_and_invalid_json_fields(metrics):
    monitors = [
        make_monitor(name="missing", assertions=[Assertion(type="json_exists", path="code")]),
        make_monitor(name="null", assertions=[Assertion(type="json_not_empty", path="code")]),
        make_monitor(
            name="invalid",
            assertions=[Assertion(type="numeric_gt", path="count", value=0)],
        ),
    ]
    payloads = [
        {"ready": True},
        {"code": None},
        {"count": "not-a-number"},
    ]
    results = []
    for monitor, payload in zip(monitors, payloads, strict=True):
        results.append(await _run(monitor, json_response(payload), metrics))

    assert [result.functional_state for result in results] == ["down", "down", "down"]
    assert [result.failed_types for result in results] == [
        ("json_exists",),
        ("json_not_empty",),
        ("numeric_gt",),
    ]


async def test_latency_only_failure_is_not_silent(metrics, registry):
    monitor = make_monitor(assertions=[Assertion(type="max_response_time", value=0)])
    result = await _run(monitor, json_response({"ok": True}), metrics)

    assert result.http_state == "up"
    assert result.functional_state == "not_configured"
    assert result.performance_state == "down"
    assert result.outcome == "performance_failure"
    assert result.silent_failure is False
    assert _sample(registry, "ximiowatch_silent_failures_total") is None


async def test_timeout_with_performance_configured_is_not_evaluated(metrics, registry):
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=httpx.Request("GET", "http://x"))

    monitor = make_monitor(assertions=[Assertion(type="max_response_time", value=1)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "timeout"
    assert result.performance_state == "unknown"
    assert result.performance_ok is None
    assert _sample(registry, "ximiowatch_performance_configured") == 1.0
    assert _sample(registry, "ximiowatch_performance_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_performance_up") is None


async def test_valid_observation_transitions_to_unknown(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="apply"),
        then=Assertion(type="json_equals", path="ok", value=True),
    )
    monitor = make_monitor(assertions=[conditional])
    state = {"payload": {"mode": "apply", "ok": True}}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response(state["payload"]))
    ) as client:
        engine = MonitorEngine([monitor], metrics, client=client)
        first = (await engine.check_all())[0]
        assert first.functional_state == "up"
        assert _sample(registry, "ximiowatch_functional_up") == 1.0

        state["payload"] = {"mode": "skip"}
        second = (await engine.check_all())[0]

    assert second.functional_state == "unknown"
    assert second.outcome == "not_evaluated"
    assert _sample(registry, "ximiowatch_functional_up") is None
    assert _sample(registry, "ximiowatch_functional_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_last_success_timestamp_seconds") > 0


async def test_oversized_response_does_not_fabricate_business_evaluation(metrics, registry):
    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 512
            yield b"x" * 512

        async def aclose(self) -> None:
            return None

    monitor = make_monitor(
        max_response_bytes=512,
        assertions=[Assertion(type="json_exists", path="ok")],
    )
    response = httpx.Response(
        200,
        stream=OversizedStream(),
        request=httpx.Request("GET", "http://demo.test/check"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        result = await check_monitor(monitor, client, metrics=metrics)

    assert result.outcome == "response_too_large"
    assert result.http_state == "up"
    assert result.status_code == 200
    assert result.functional_state == "unknown"
    assert result.business_failed_types == ()
    assert result.silent_failure is False
    assert _sample(registry, "ximiowatch_functional_configured") == 1.0
    assert _sample(registry, "ximiowatch_functional_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_functional_up") is None
    assert _sample(registry, "ximiowatch_last_success_timestamp_seconds") is None
    assert (
        _sample(registry, "ximiowatch_request_duration_seconds_count") is None
    )


async def test_positive_and_negative_text_assertions(metrics, registry):
    positive = make_monitor(
        assertions=[Assertion(type="text_contains", value="Ready")]
    )
    negative = make_monitor(
        name="negative",
        assertions=[Assertion(type="text_not_contains", value="ERROR")],
    )
    response = _response(
        content=b"<html>Ready now</html>", headers={"Content-Type": "text/html; charset=utf-8"}
    )

    assert (await _run(positive, response, metrics)).outcome == "success"
    assert (await _run(negative, response, metrics)).outcome == "success"
    assert _sample(registry, "ximiowatch_functional_up") == 1.0


async def test_text_absence_and_undecodable_body(metrics, registry):
    missing = make_monitor(assertions=[Assertion(type="text_contains", value="ABSENT")])
    undecodable = make_monitor(
        name="undecodable",
        assertions=[Assertion(type="text_not_contains", value=CANARY)],
    )
    missing_result = await _run(
        missing,
        _response(content=b"plain body", headers={"Content-Type": "text/plain"}),
        metrics,
    )
    undecodable_result = await _run(
        undecodable,
        _response(
            content=b"\xff\xfe\xfd",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        ),
        metrics,
    )

    assert missing_result.functional_state == "down"
    assert undecodable_result.functional_state == "unknown"
    assert undecodable_result.outcome == "not_evaluated"
    assert undecodable_result.functional_ok is False
    assert "text_not_contains" not in undecodable_result.failed_types


async def test_invalid_or_missing_content_type(metrics):
    missing = make_monitor(
        assertions=[Assertion(type="content_type_contains", value="application/json")]
    )
    invalid = make_monitor(
        name="invalid",
        assertions=[Assertion(type="content_type_contains", value="application/json")],
    )

    missing_result = await _run(missing, _response(content=b'{"ok":true}'), metrics)
    invalid_result = await _run(
        invalid,
        _response(
            content=b'{"ok":true}', headers={"Content-Type": "text/plain; charset=utf-8"}
        ),
        metrics,
    )

    assert missing_result.functional_state == "down"
    assert invalid_result.functional_state == "down"
    assert missing_result.failed_types == ("content_type_contains",)

    case_insensitive = make_monitor(
        name="case-insensitive",
        assertions=[Assertion(type="content_type_contains", value="APPLICATION/JSON")],
    )
    matched = await _run(
        case_insensitive,
        _response(content=b'{"ok":true}', headers={"Content-Type": "application/json"}),
        metrics,
    )
    assert matched.functional_state == "up"


def test_head_monitor_rejects_nested_body_assertions():
    nested = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="apply"),
        then=Assertion(type="text_contains", value=CANARY),
    )
    with pytest.raises(Exception) as caught:
        MonitorConfig.model_validate(
            {
                "name": "head",
                "application": "demo",
                "url": "http://demo.test/check",
                "method": "HEAD",
                "assertions": [nested],
            }
        )
    assert "HEAD monitors cannot define body assertions" in str(caught.value)


def test_assertion_failure_labels_are_bounded_and_secret_safe(metrics, registry):
    monitor = make_monitor(assertions=[Assertion(type="text_contains", value=CANARY)])
    assert "text_contains" in KNOWN_ASSERTION_TYPES

    async def scenario() -> None:
        result = await _run(
            monitor,
            _response(content=b"safe body", headers={"Content-Type": "text/plain"}),
            metrics,
        )
        assert result.failed_types == ("text_contains",)

    import asyncio

    asyncio.run(scenario())
    output = generate_latest(registry).decode("utf-8")
    assert CANARY not in output
    assert 'type="text_contains"' in output
    assert all(outcome in KNOWN_OUTCOMES for outcome in KNOWN_OUTCOMES)


async def test_exact_once_metric_recording_with_new_assertions(metrics, registry):
    monitor = make_monitor(assertions=[Assertion(type="text_contains", value="OK")])
    response = _response(content=b"OK", headers={"Content-Type": "text/plain"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response)
    ) as client:
        await check_monitor(monitor, client, metrics=metrics)
        await check_monitor(monitor, client, metrics=metrics)

    total = sum(
        sample.value
        for metric in registry.collect()
        for sample in metric.samples
        if sample.name == "ximiowatch_checks_total"
    )
    assert total == 2.0
    assert _sample(registry, "ximiowatch_functional_up") == 1.0


def test_new_assertion_secret_diagnostics_are_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ASSERTION_SECRET", CANARY)
    path = tmp_path / "monitors.yaml"
    path.write_text(
        f"""monitors:
  - name: sample
    application: demo
    url: http://demo.test/check
    method: HEAD
    assertions:
      - type: conditional
        when:
          type: content_type_contains
          value: "${{TEST_ASSERTION_SECRET}}"
        then:
          type: text_contains
          value: "${{TEST_ASSERTION_SECRET}}"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    output = "\n".join(
        [
            str(caught.value),
            repr(caught.value.__cause__),
            repr(caught.value.__context__),
            "".join(
                traceback.format_exception(
                    type(caught.value), caught.value, caught.value.__traceback__
                )
            ),
        ]
    )
    assert CANARY not in output
    assert "HEAD monitors cannot define body assertions" in output


async def test_new_assertion_failures_do_not_leak_expected_values(metrics, registry, caplog):
    monitor = make_monitor(assertions=[Assertion(type="text_contains", value=CANARY)])
    engine = MonitorEngine(
        [monitor],
        metrics,
        client_factory=lambda _: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: _response(content=b"safe", headers={"Content-Type": "text/plain"})
            )
        ),
    )
    with caplog.at_level(logging.INFO, logger="ximiowatch"):
        await engine.check_all()

    metrics_output = generate_latest(registry).decode("utf-8")
    assert CANARY not in caplog.text
    assert CANARY not in metrics_output


def test_conditional_status_assertion_is_rejected(tmp_path):
    path = tmp_path / "monitors.yaml"
    path.write_text(
        """monitors:
  - name: sample
    application: demo
    url: http://demo.test/check
    assertions:
      - type: conditional
        when:
          type: http_status
          value: 200
        then:
          type: json_exists
          path: ok
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as caught:
        load_monitors(path)

    assert "'http_status' is not supported inside a conditional" in str(caught.value)


async def test_guard_only_business_assertion_is_not_configured(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="ok", value=True),
        then=Assertion(type="max_response_time", value=1),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(monitor, json_response({"ok": True}), metrics)

    assert result.functional_state == "not_configured"
    assert result.functional_ok is True
    assert result.performance_state == "up"
    assert result.outcome == "success"
    assert result.healthy is True
    assert _sample(registry, "ximiowatch_functional_configured") == 0.0
    assert _sample(registry, "ximiowatch_functional_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_performance_evaluated") == 1.0


async def test_latency_guard_is_evaluated(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="max_response_time", value=0),
        then=Assertion(type="json_exists", path="missing"),
        otherwise=Assertion(type="json_exists", path="ok"),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(monitor, json_response({"ok": True}), metrics)

    assert result.performance_configured is True
    assert result.performance_evaluated is True
    assert result.performance_state == "down"
    assert result.functional_state == "up"
    assert result.outcome == "performance_failure"
    assert result.silent_failure is False
    assert _sample(registry, "ximiowatch_performance_up") == 0.0
    assert (
        _sample(
            registry,
            "ximiowatch_assertion_failures_total",
            {**BASE_LABELS, "type": "max_response_time"},
        )
        == 1.0
    )


async def test_invalid_json_guard_is_a_confirmed_failure(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="apply"),
        then=Assertion(type="content_type_contains", value="application/json"),
        otherwise=Assertion(type="content_type_contains", value="application/json"),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(
        monitor, _response(content=b"not json", headers={"Content-Type": "text/plain"}), metrics
    )

    assert result.functional_state == "down"
    assert result.functional_evaluated is True
    assert result.silent_failure is True
    assert result.outcome == "functional_failure"
    assert "invalid_json" in result.business_failed_types
    assert _sample(registry, "ximiowatch_functional_up") == 0.0


async def test_json_in_skipped_branch_does_not_create_confirmed_failure(metrics, registry):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="content_type_contains", value="application/json"),
        then=Assertion(type="json_exists", path="code"),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(
        monitor, _response(content=b"not json", headers={"Content-Type": "text/plain"}), metrics
    )

    assert result.functional_state == "unknown"
    assert result.functional_evaluated is False
    assert result.silent_failure is False
    assert result.outcome == "not_evaluated"
    assert _sample(registry, "ximiowatch_functional_up") is None


async def test_single_quoted_charset_decodes_text(metrics):
    monitor = make_monitor(assertions=[Assertion(type="text_contains", value="Ready")])
    result = await _run(
        monitor,
        _response(
            content=b"Ready now",
            headers={"Content-Type": "text/plain; charset='utf-8'"},
        ),
        metrics,
    )

    assert result.functional_state == "up"
    assert result.outcome == "success"


async def test_invalid_json_guard_without_required_business_check_is_not_configured(
    metrics, registry
):
    conditional = Assertion(
        type="conditional",
        when=Assertion(type="json_equals", path="mode", value="apply"),
        then=Assertion(type="max_response_time", value=1),
    )
    monitor = make_monitor(assertions=[conditional])
    result = await _run(
        monitor, _response(content=b"not json", headers={"Content-Type": "text/plain"}), metrics
    )

    assert result.functional_configured is False
    assert result.functional_evaluated is False
    assert result.functional_state == "not_configured"
    assert result.performance_state == "unknown"
    assert result.business_failed_types == ()
    assert result.outcome == "not_evaluated"
    assert result.silent_failure is False
    assert _sample(registry, "ximiowatch_functional_configured") == 0.0
    assert _sample(registry, "ximiowatch_functional_evaluated") == 0.0
    assert _sample(registry, "ximiowatch_functional_up") is None
