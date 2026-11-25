"""Static contract checks for the persistent shared-server demo Compose stack."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
COMPOSE = ROOT / "docker-compose.shared-demo.yml"
PROMETHEUS = ROOT / "prometheus" / "prometheus.shared-demo.yml"
ALERTMANAGER = ROOT / "alertmanager" / "alertmanager.shared-demo.yml"
ENV_EXAMPLE = ROOT / "docs" / "shared-demo.env.example"

EXPECTED_SERVICES = {
    "ximiowatch",
    "demo-api",
    "prometheus",
    "grafana",
    "alertmanager",
}
EXPECTED_MEMORY_MIB = {
    "ximiowatch": 192,
    "demo-api": 96,
    "prometheus": 320,
    "grafana": 320,
    "alertmanager": 96,
}
EXPECTED_CPUS = {
    "ximiowatch": 0.20,
    "demo-api": 0.10,
    "prometheus": 0.25,
    "grafana": 0.30,
    "alertmanager": 0.10,
}
PYTHON_IMAGE = (
    "python:3.12.16-slim@sha256:"
    "191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b"
)
PROMETHEUS_IMAGE = (
    "prom/prometheus:v3.16.0@sha256:"
    "e04987641b57704f408e31f6373ba4138ecf27cb4a165d24aabb6c38fddd0f06"
)
GRAFANA_IMAGE = (
    "grafana/grafana:12.4.16@sha256:"
    "982fa6bf18cc38280a30bf08c3b673486067d4805b25a9d4756688a689a993c7"
)
ALERTMANAGER_IMAGE = (
    "prom/alertmanager:v0.35.1@sha256:"
    "4e3d029e69c7f61dd53a2b31a3dc427a2b81736cffca185c0854075e07969e36"
)
PINNED_IMAGE_REFERENCES = {
    "Dockerfile": (PYTHON_IMAGE,),
    "Dockerfile.demo": (PYTHON_IMAGE,),
    "docker-compose.yml": (PROMETHEUS_IMAGE, GRAFANA_IMAGE),
    "docker-compose.alertmanager.yml": (PYTHON_IMAGE, ALERTMANAGER_IMAGE),
    "docker-compose.shared-demo.yml": (
        PROMETHEUS_IMAGE,
        GRAFANA_IMAGE,
        ALERTMANAGER_IMAGE,
    ),
    ".github/workflows/alerting.yml": (PROMETHEUS_IMAGE, ALERTMANAGER_IMAGE),
    "README.md": (PROMETHEUS_IMAGE, ALERTMANAGER_IMAGE),
    "docs/shared-vps-deployment.md": (PYTHON_IMAGE,),
    "prometheus/rules/tests/demo_alerts_test.yml": (PROMETHEUS_IMAGE,),
}


def _compose() -> dict[str, object]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _default_number(value: str, suffix: str = "") -> float:
    text = str(value).lower()
    marker = ":-"
    if marker in text:
        text = text.split(marker, 1)[1].split("}", 1)[0]
    if suffix and text.endswith(suffix.lower()):
        text = text[: -len(suffix)]
    return float(text)


def test_shared_demo_defines_only_the_five_isolated_services():
    document = _compose()

    assert document["name"] == "ximiowatch-demo"
    assert set(document["services"]) == EXPECTED_SERVICES
    assert "fixtures" not in document["services"]
    assert "mock-receiver" not in document["services"]

    networks = document["networks"]
    assert set(networks) == {"shared-demo"}
    assert networks["shared-demo"]["driver"] == "bridge"
    for service in document["services"].values():
        assert service["networks"] == ["shared-demo"]
        assert "container_name" not in service
        assert "network_mode" not in service
        assert service.get("privileged") is not True


def test_shared_demo_resource_defaults_fit_the_declared_budget():
    document = _compose()
    services = document["services"]

    memory_total = 0.0
    cpu_total = 0.0
    for name, expected_memory in EXPECTED_MEMORY_MIB.items():
        service = services[name]
        memory = _default_number(service["mem_limit"], "m")
        cpus = _default_number(service["cpus"])
        assert memory == expected_memory
        assert cpus == EXPECTED_CPUS[name]

        limits = service["deploy"]["resources"]["limits"]
        assert _default_number(limits["memory"], "m") == expected_memory
        assert _default_number(limits["cpus"]) == EXPECTED_CPUS[name]
        assert service["deploy"]["resources"]["reservations"]["memory"]

        pids_limit = int(service["pids_limit"].split(":-")[-1].rstrip("}"))
        assert pids_limit > 0
        assert int(limits["pids"].split(":-")[-1].rstrip("}")) == pids_limit
        memory_total += memory
        cpu_total += cpus

    assert memory_total == 1024
    assert round(cpu_total, 2) == 0.95


def test_shared_demo_publishes_loopback_ports_and_bounds_logs():
    document = _compose()

    for service in document["services"].values():
        for published in service["ports"]:
            assert published.startswith("127.0.0.1:")
        assert service["restart"] == "unless-stopped"
        assert service["logging"]["driver"] == "json-file"
        assert service["logging"]["options"]["max-size"] == "10m"
        assert service["logging"]["options"]["max-file"] == "3"
        assert "healthcheck" in service


def test_shared_demo_uses_project_volumes_and_no_host_mounts():
    document = _compose()

    assert set(document["volumes"]) == {
        "prometheus-data",
        "grafana-data",
        "alertmanager-data",
    }
    serialized = COMPOSE.read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in serialized
    assert "network_mode: host" not in serialized
    assert "external: true" not in serialized


def test_shared_demo_prometheus_config_uses_demo_rules_and_slow_scrape():
    config = yaml.safe_load(PROMETHEUS.read_text(encoding="utf-8"))

    assert config["global"]["scrape_interval"] == "15s"
    assert config["global"]["evaluation_interval"] == "5s"
    assert config["rule_files"] == [
        "/etc/prometheus/rules/recording.yml",
        "/etc/prometheus/rules/alerts.yml",
    ]
    assert config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == [
        "alertmanager:9093"
    ]
    assert "alerts.production.yml" not in COMPOSE.read_text(encoding="utf-8")


def test_shared_demo_alertmanager_has_no_external_delivery():
    config = yaml.safe_load(ALERTMANAGER.read_text(encoding="utf-8"))

    receiver = config["receivers"][0]
    assert receiver["name"] == "local-only-no-destination"
    assert set(receiver) == {"name"}
    serialized = ALERTMANAGER.read_text(encoding="utf-8")
    for forbidden in ("webhook_configs", "telegram_configs", "email_configs", "slack_configs"):
        assert forbidden not in serialized
    assert len(config["inhibit_rules"]) == 3


def test_shared_demo_grafana_requires_non_default_file_secret():
    document = _compose()
    environment = document["services"]["grafana"]["environment"]

    assert "GF_SECURITY_ADMIN_USER" in environment
    assert "GF_SECURITY_ADMIN_PASSWORD" not in environment
    assert environment["GF_SECURITY_ADMIN_PASSWORD__FILE"] == "/run/secrets/grafana_admin_password"
    assert environment["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert environment["GF_AUTH_ANONYMOUS_ENABLED"] == "false"

    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "SHARED_GRAFANA_ADMIN_USER=" in example
    assert "SHARED_GRAFANA_PASSWORD_FILE=" in example
    assert "admin/admin" not in example


def test_runtime_images_are_pinned_consistently():
    for relative_path, references in PINNED_IMAGE_REFERENCES.items():
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        for reference in references:
            assert reference in content, f"{reference} missing from {relative_path}"
