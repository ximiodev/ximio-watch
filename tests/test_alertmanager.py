"""Structural validation of Stage 4 Alertmanager configuration and examples."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
ALERTMANAGER_DIR = ROOT / "alertmanager"


def _load(name: str) -> dict[str, object]:
    return yaml.safe_load((ALERTMANAGER_DIR / name).read_text(encoding="utf-8"))


def test_demo_config_uses_local_webhook_with_resolved_notifications():
    config = _load("alertmanager.yml")
    route = config["route"]
    receivers = config["receivers"]

    assert route["receiver"] == "demo-webhook"
    assert route["group_by"] == ["alertname", "application", "monitor"]
    assert route["group_wait"] == "2s"
    assert route["group_interval"] == "10s"
    assert route["repeat_interval"] == "1m"

    demo = next(receiver for receiver in receivers if receiver["name"] == "demo-webhook")
    webhook = demo["webhook_configs"][0]
    assert webhook["url"] == "http://mock-receiver:8080/notify"
    assert webhook["send_resolved"] is True


def test_application_inhibition_is_scoped_to_application_and_monitor():
    config = _load("alertmanager.yml")
    by_name = {rule["name"]: rule for rule in config["inhibit_rules"]}

    http_rule = by_name["http-failure-suppresses-secondary-symptoms"]
    assert http_rule["source_matchers"] == ['alertname="ApplicationHttpFailure"']
    assert http_rule["equal"] == ["application", "monitor"]
    assert "SilentApplicationFailure" in http_rule["target_matchers"][0]

    functional_rule = by_name["functional-failure-suppresses-secondary-symptoms"]
    assert functional_rule["source_matchers"] == ['alertname="SilentApplicationFailure"']
    assert functional_rule["equal"] == ["application", "monitor"]

    exporter_rule = by_name["exporter-unavailable-suppresses-per-monitor-alerts"]
    assert exporter_rule["source_matchers"] == ['alertname="XimioWatchUnavailable"']
    # Exporter unavailability invalidates all per-monitor observations, so this
    # suppression is intentionally global.
    assert "equal" not in exporter_rule


def test_telegram_example_uses_secret_files_and_safe_template():
    config = _load("alertmanager.telegram.example.yml")
    telegram = config["receivers"][0]["telegram_configs"][0]

    assert telegram["bot_token_file"] == "/etc/alertmanager/private/telegram_bot_token"
    assert telegram["chat_id_file"] == "/etc/alertmanager/private/telegram_chat_id"
    assert telegram["parse_mode"] == "HTML"
    assert telegram["send_resolved"] is True
    assert 'template "ximiowatch.telegram.message"' in telegram["message"]

    assert "bot_token" not in telegram
    assert "chat_id" not in telegram

    template = (ALERTMANAGER_DIR / "templates" / "ximiowatch.tmpl").read_text(
        encoding="utf-8"
    )
    assert "define \"ximiowatch.telegram.message\"" in template
    assert "| html" in template
    assert "range .Alerts" in template
    assert "does not prove application recovery" in template


def test_mock_receiver_is_local_only_and_inspection_oriented():
    source = (ROOT / "verification" / "mock_receiver.py").read_text(encoding="utf-8")

    assert "ThreadingHTTPServer((\"0.0.0.0\", 8080)" in source
    assert "/notify" in source
    assert "/events" in source
    for forbidden in ("httpx", "requests", "urlopen", "api.telegram.org"):
        assert forbidden not in source
