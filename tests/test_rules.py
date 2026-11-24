"""Structural tests for the demonstration-only Prometheus alert rules."""

from __future__ import annotations

from pathlib import Path

import yaml

RULES_PATH = Path(__file__).parents[1] / "prometheus" / "rules" / "alerts.yml"


def test_alert_rules_define_both_required_alerts():
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    rules = {
        rule["alert"]: rule
        for group in document["groups"]
        for rule in group["rules"]
    }

    assert set(rules) == {"SilentApplicationFailure", "XimioWatchUnavailable"}
    assert all(rule["for"] == "30s" for rule in rules.values())
    assert rules["XimioWatchUnavailable"]["expr"].strip() == 'up{job="ximiowatch"} == 0'

    silent = rules["SilentApplicationFailure"]["expr"]
    for required in (
        "ximiowatch_http_up == 1",
        "ximiowatch_functional_up == 0",
        "ximiowatch_last_check_timestamp_seconds",
        'up{job="ximiowatch"} == 1',
    ):
        assert required in silent
