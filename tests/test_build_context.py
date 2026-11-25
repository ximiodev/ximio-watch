"""Static checks for the public Docker build context and ignore rules."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_dockerfile_copies_only_the_public_demo_configuration():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY config/monitors.yaml ./config/monitors.yaml" in dockerfile
    assert "COPY config/ ./config/" not in dockerfile
    assert "COPY . ." not in dockerfile


def test_ignore_rules_protect_private_monitor_configuration():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    required_patterns = (
        "config/private/",
        "config/local/",
        "config/production/",
        "config/monitors.*.yaml",
        "secrets/",
        "alertmanager/private/",
        "alertmanager/secrets/",
        "alertmanager/*.local.yml",
        "*.telegram_token",
        "*.chat_id",
        "*.pem",
        "*.key",
        ".env",
        ".claude/",
        ".cursor/",
        ".opencode/",
        "*transcript*",
        "!config/monitors.example.yaml",
    )
    for pattern in required_patterns:
        assert pattern in gitignore
        assert pattern in dockerignore

    for lines in (gitignore, dockerignore):
        assert "config/" not in lines
        assert "!config/monitors.yaml" not in lines


def test_verification_assets_are_versioned_but_excluded_from_images():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    # The overlay mounts these files read-only; they must be reproducible from
    # the repository but must not be copied into production runtime images.
    assert "verification/" in dockerignore
    assert not any(line.strip() == "verification/" for line in gitignore)
    for name in ("fixtures.py", "mock_receiver.py", "monitors.yaml"):
        assert (ROOT / "verification" / name).is_file()

    overlay = (ROOT / "docker-compose.alertmanager.yml").read_text(encoding="utf-8")
    for name in ("fixtures.py", "mock_receiver.py", "monitors.yaml"):
        assert f"./verification/{name}:" in overlay
