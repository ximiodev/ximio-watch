"""Guard that the provisioned dashboard only references available metric names."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]

DASHBOARD_METRIC = re.compile(r"ximiowatch_[a-zA-Z0-9_]+")
METRIC_DECLARATION = re.compile(r'f"\{prefix\}_([a-zA-Z0-9_]+)"')
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")


def test_dashboard_promql_references_existing_or_recorded_metrics():
    dashboard = json.loads(
        (ROOT / "grafana" / "dashboards" / "ximiowatch.json").read_text(encoding="utf-8")
    )
    expressions = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]
    referenced = set()
    for expression in expressions:
        referenced.update(DASHBOARD_METRIC.findall(expression))

    declarations = METRIC_DECLARATION.findall(
        (ROOT / "ximiowatch" / "metrics.py").read_text(encoding="utf-8")
    )
    available = {f"ximiowatch_{name}" for name in declarations}
    for name in list(available):
        available.update(name + suffix for suffix in HISTOGRAM_SUFFIXES)

    recording = yaml.safe_load((ROOT / "prometheus" / "rules" / "recording.yml").read_text(encoding="utf-8"))
    available.update(
        rule["record"]
        for group in recording["groups"]
        for rule in group["rules"]
        if "record" in rule
    )

    assert referenced
    assert referenced <= available
