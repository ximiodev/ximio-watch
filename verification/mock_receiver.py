"""Controlled local webhook receiver used only by Stage 4 integration verification.

It records Alertmanager payloads in a local JSON Lines file for inspection and
never forwards them anywhere. Payloads are not printed to stdout or stderr.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

EVENTS_PATH = Path(
    os.environ.get("MOCK_RECEIVER_EVENTS", "/tmp/ximiowatch-stage4-events.jsonl")
)
MAX_BODY_BYTES = 1_000_000


def _reply(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class MockReceiverHandler(BaseHTTPRequestHandler):
    """Accepts firing/resolved notifications and exposes recorded events locally."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/reset":
            EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            EVENTS_PATH.write_text("", encoding="utf-8")
            _reply(self, 200, {"status": "reset"})
            return
        if path != "/notify":
            _reply(self, 404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            _reply(self, 400, {"error": "invalid content length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            _reply(self, 413, {"error": "invalid payload size"})
            return

        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _reply(self, 400, {"error": "invalid json"})
            return

        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
        _reply(self, 202, {"status": "accepted"})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/healthz":
            _reply(self, 200, {"status": "ok", "service": "mock-receiver"})
            return
        if path == "/events":
            events: list[object] = []
            if EVENTS_PATH.exists():
                for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
                    if line:
                        events.append(json.loads(line))
            _reply(self, 200, events)
            return
        _reply(self, 404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        """Suppress access logs, which could otherwise contain notification data."""
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8080), MockReceiverHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
