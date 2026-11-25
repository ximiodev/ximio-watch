"""Structural checks for the Stage 4 Prometheus rules.

Behavioral correctness is covered authoritatively by the `promtool test rules`
suites under prometheus/rules/tests/.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
RULES_DIR = ROOT / "prometheus" / "rules"

EXPECTED_ALERTS = {
    "ApplicationHttpFailure": ("critical", "10s", "2m"),
    "SilentApplicationFailure": ("warning", "15s", "2m"),
    "ApplicationPerformanceDegraded": ("warning", "20s", "5m"),
    "MonitorEvaluationIncomplete": ("warning", "15s", "2m"),
    "XimioWatchMonitorStale": ("critical", "10s", "1m"),
    "XimioWatchUnavailable": ("critical", "10s", "1m"),
}


def _rules(path: Path) -> dict[str, dict[str, object]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        rule["alert"]: rule
        for group in document["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }


def _recording_rules() -> dict[str, dict[str, object]]:
    document = yaml.safe_load((RULES_DIR / "recording.yml").read_text(encoding="utf-8"))
    return {
        rule["record"]: rule
        for group in document["groups"]
        for rule in group["rules"]
        if "record" in rule
    }


def test_demo_rules_define_all_alert_classes_with_fast_thresholds():
    rules = _rules(RULES_DIR / "alerts.yml")

    assert set(rules) == set(EXPECTED_ALERTS)
    for name, (severity, demo_for, _) in EXPECTED_ALERTS.items():
        assert rules[name]["labels"]["severity"] == severity
        assert rules[name]["labels"]["service"] == "ximiowatch"
        assert rules[name]["for"] == demo_for


def test_production_rules_define_all_alert_classes_with_production_thresholds():
    rules = _rules(RULES_DIR / "alerts.production.yml")

    assert set(rules) == set(EXPECTED_ALERTS)
    for name, (severity, _, production_for) in EXPECTED_ALERTS.items():
        assert rules[name]["labels"]["severity"] == severity
        assert rules[name]["labels"]["service"] == "ximiowatch"
        assert rules[name]["for"] == production_for


def test_application_alerts_require_freshness_and_bounded_matching():
    rules = {
        **_rules(RULES_DIR / "alerts.yml"),
        **_rules(RULES_DIR / "alerts.production.yml"),
    }
    for name in EXPECTED_ALERTS:
        expression = str(rules[name]["expr"])
        assert "url" not in expression
        assert "instance" not in expression
        assert "path" not in expression

    for name in (
        "ApplicationHttpFailure",
        "SilentApplicationFailure",
        "ApplicationPerformanceDegraded",
        "MonitorEvaluationIncomplete",
        "XimioWatchMonitorStale",
    ):
        expression = str(rules[name]["expr"])
        assert "ximiowatch_monitor_" in expression

    # Raw engine metrics carry scrape-added job/instance labels. Application
    # alerts must aggregate them away so only bounded labels reach Alertmanager.
    for name in (
        "ApplicationHttpFailure",
        "SilentApplicationFailure",
        "ApplicationPerformanceDegraded",
    ):
        expression = str(rules[name]["expr"])
        assert "max by (monitor, application)" in expression


def test_exporter_alert_detects_failed_and_absent_targets():
    for rules in (_rules(RULES_DIR / "alerts.yml"), _rules(RULES_DIR / "alerts.production.yml")):
        expression = str(rules["XimioWatchUnavailable"]["expr"])
        assert 'up{job="ximiowatch"} == 0' in expression
        assert 'absent(up{job="ximiowatch"})' in expression


def test_recording_rules_encapsulate_freshness_and_eligibility():
    rules = _recording_rules()

    assert set(rules) == {
        "ximiowatch_monitor_fresh",
        "ximiowatch_monitor_stale",
        "ximiowatch_monitor_incomplete",
    }
    for rule in rules.values():
        expression = str(rule["expr"])
        assert "on(monitor, application)" in expression
        assert "max by (monitor, application)" in expression
        assert "url" not in expression

    for name in ("ximiowatch_monitor_fresh", "ximiowatch_monitor_stale"):
        assert 'count(up{job="ximiowatch"} == 1) > 0' in str(rules[name]["expr"])
    assert "ximiowatch_monitor_fresh == 1" in str(rules["ximiowatch_monitor_incomplete"]["expr"])

    stale = str(rules["ximiowatch_monitor_stale"]["expr"])
    assert "ximiowatch_startup_timestamp_seconds" in stale
    assert ">= 60" in stale


def test_prometheus_configs_share_rules_and_separate_alerting():
    prometheus_dir = ROOT / "prometheus"
    standalone = yaml.safe_load((prometheus_dir / "prometheus.yml").read_text(encoding="utf-8"))
    alerting = yaml.safe_load(
        (prometheus_dir / "prometheus.alerting.yml").read_text(encoding="utf-8")
    )

    expected_rules = [
        "/etc/prometheus/rules/recording.yml",
        "/etc/prometheus/rules/alerts.yml",
    ]
    assert standalone["rule_files"] == expected_rules
    assert alerting["rule_files"] == expected_rules
    assert standalone["scrape_configs"] == alerting["scrape_configs"]

    assert "alerting" not in standalone
    assert alerting["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == [
        "alertmanager:9093"
    ]

    overlay = (ROOT / "docker-compose.alertmanager.yml").read_text(encoding="utf-8")
    assert "./prometheus/prometheus.alerting.yml:/etc/prometheus/prometheus.yml:ro" in overlay
