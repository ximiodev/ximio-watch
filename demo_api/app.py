"""FastAPI demo service with independently switchable simulated business failures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

SCENARIOS = ("restaurant_catalog", "fiscal_authorization", "device_data_freshness")

app = FastAPI(title="XimioWatch Demo API", version="0.1.0")
_simulated_failures: dict[str, bool] = {scenario: False for scenario in SCENARIOS}


class SimulationState(BaseModel):
    enabled: bool


def _failure_enabled(scenario: str) -> bool:
    return _simulated_failures[scenario]


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    """Infrastructure health stays green even while business failures are enabled."""
    return {
        "status": "ok",
        "service": "demo-api",
        "simulated_failures": dict(_simulated_failures),
    }


@app.get("/simulation")
async def get_simulation() -> dict[str, bool]:
    return dict(_simulated_failures)


@app.post("/simulation/{scenario}/failure")
async def set_simulation(scenario: str, state: SimulationState) -> dict[str, object]:
    if scenario not in _simulated_failures:
        raise HTTPException(status_code=404, detail="unknown scenario")
    _simulated_failures[scenario] = state.enabled
    return {"scenario": scenario, "failure_enabled": state.enabled}


@app.get("/scenarios/restaurant-catalog")
async def restaurant_catalog() -> dict[str, object]:
    """Scenario A: healthy catalog vs. HTTP 200 with an empty catalog."""
    products = []
    if not _failure_enabled("restaurant_catalog"):
        products = [
            {"id": "sku-camp-mug", "name": "Camp Mug", "available": True},
            {"id": "sku-trail-mix", "name": "Trail Mix", "available": True},
            {"id": "sku-bloom-filter", "name": "Bloom Filter", "available": True},
        ]
    return {"catalog_id": "catalog-demo-01", "status": "ready", "products": products}


@app.get("/scenarios/fiscal-authorization")
async def fiscal_authorization() -> dict[str, object]:
    """Scenario B: authorized invoice with or without its authorization code."""
    invoice: dict[str, object] = {
        "invoice_id": "INV-DEMO-2041",
        "authorized": True,
        "amount": 149.50,
        "currency": "EUR",
    }
    if not _failure_enabled("fiscal_authorization"):
        invoice["authorization_code"] = "AUTH-DEMO-7F3C"
    return invoice


@app.get("/scenarios/device-data-freshness")
async def device_data_freshness() -> dict[str, object]:
    """Scenario C: online device reporting stale event data during failure."""
    age_seconds = 7200 if _failure_enabled("device_data_freshness") else 2
    last_event_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "device_id": "device-demo-042",
        "status": "online",
        "last_event_at": last_event_at.isoformat(),
        "last_event_age_seconds": age_seconds,
        "event": {"type": "temperature_c", "value": 21.5},
    }
