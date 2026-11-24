"""XimioWatch service: monitoring engine plus Prometheus /metrics endpoint."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import ConfigurationError, load_monitors
from .engine import MonitorEngine
from .metrics import Metrics
from .telemetry import setup_logging, setup_tracing

metrics = Metrics()
_config_path = os.getenv("XIMIOWATCH_CONFIG", "config/monitors.yaml")
_task: asyncio.Task[None] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _task
    setup_logging()
    setup_tracing()
    engine: MonitorEngine | None = None
    try:
        monitors = load_monitors(_config_path)
    except ConfigurationError as exc:
        logging.getLogger(__name__).error(
            "configuration.load_failed",
            extra={"error_type": type(exc).__name__, "detail": str(exc)},
        )
        raise
    try:
        engine = MonitorEngine(monitors, metrics)
        app.state.engine = engine
        app.state.monitors = monitors

        _task = asyncio.create_task(engine.run(immediate=True))
        yield
    finally:
        if _task is not None:
            _task.cancel()
            with suppress(asyncio.CancelledError):
                await _task
            _task = None
        if engine is not None:
            await engine.aclose()


app = FastAPI(title="XimioWatch", version="0.1.0", lifespan=lifespan)


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Response:
    return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {"status": "ok", "service": "ximiowatch"}


@app.get("/monitors")
async def monitors() -> list[dict[str, object]]:
    definitions = getattr(app.state, "monitors", [])
    return [
        {
            "name": monitor.name,
            "application": monitor.application,
            "interval_seconds": monitor.interval_seconds,
        }
        for monitor in definitions
    ]
