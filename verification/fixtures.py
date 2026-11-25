"""Controlled local HTTP fixtures used only by Stage 4 integration verification.

All responses are fictional and deterministic. No production endpoint is
contacted and no response contains private data. Each failure mode can be
enabled and disabled at runtime so firing and resolved notifications can be
verified without touching external services.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SCENARIOS = ("http", "silent", "performance", "incomplete", "stale")
PROBES = SCENARIOS + ("healthy",)
HEALTHY_PAYLOAD = {
    "status": "ready",
    "products": [{"id": "demo-1", "available": True}],
}
# The stale probe starts held open so it never completes a first check; the
# other failure scenarios start healthy.
_state = {scenario: (scenario == "stale") for scenario in SCENARIOS}
_state_lock = threading.Lock()


def _reply(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _enabled(scenario: str) -> bool:
    with _state_lock:
        return _state[scenario]


def _set(scenario: str, enabled: bool) -> None:
    with _state_lock:
        _state[scenario] = enabled


class FixtureHandler(BaseHTTPRequestHandler):
    """Exposes one runtime-switchable endpoint per alert eligibility scenario."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/healthz":
            _reply(self, 200, {"status": "ok", "service": "stage4-fixtures"})
            return
        if path == "/control":
            with _state_lock:
                snapshot = dict(_state)
            _reply(self, 200, snapshot)
            return
        if path == "/reset":
            with _state_lock:
                for scenario in SCENARIOS:
                    _state[scenario] = (scenario == "stale")
            _reply(self, 200, {"status": "reset"})
            return

        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "control" and parts[2] in ("enable", "disable"):
            scenario = parts[1]
            if scenario not in _state:
                _reply(self, 404, {"error": "unknown scenario"})
                return
            _set(scenario, parts[2] == "enable")
            _reply(self, 200, {"scenario": scenario, "enabled": _enabled(scenario)})
            return

        if len(parts) == 2 and parts[0] == "probe":
            scenario = parts[1]
            if scenario not in PROBES:
                _reply(self, 404, {"error": "unknown scenario"})
                return
            self._probe(scenario)
            return

        _reply(self, 404, {"error": "not found"})

    def _probe(self, scenario: str) -> None:
        if scenario == "healthy":
            _reply(self, 200, HEALTHY_PAYLOAD)
            return

        if scenario == "http":
            if _enabled(scenario):
                _reply(self, 503, HEALTHY_PAYLOAD)
            else:
                _reply(self, 200, HEALTHY_PAYLOAD)
            return

        if scenario == "silent":
            payload = (
                {"status": "ready", "products": []}
                if _enabled(scenario)
                else HEALTHY_PAYLOAD
            )
            _reply(self, 200, payload)
            return

        if scenario == "performance":
            if _enabled(scenario):
                # Sleep in short slices so disabling the scenario releases the
                # in-flight check and produces a clean recovery observation.
                for _ in range(10):
                    if not _enabled(scenario):
                        break
                    time.sleep(0.1)
            _reply(self, 200, HEALTHY_PAYLOAD)
            return

        if scenario == "incomplete":
            payload = {"strict": False} if _enabled(scenario) else {"strict": True, "code": "ok"}
            _reply(self, 200, payload)
            return

        if scenario == "stale":
            # Hold the first check open until explicitly disabled. This creates
            # a monitor with no completed observation after the startup grace.
            while _enabled(scenario):
                time.sleep(1)
            _reply(self, 200, HEALTHY_PAYLOAD)
            return

    def log_message(self, format: str, *args: object) -> None:
        """Keep verification output free of request details."""
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8002), FixtureHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
