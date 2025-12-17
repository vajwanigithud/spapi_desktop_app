from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routes import vendor_rt_inventory_routes as routes


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    return app


def test_rt_inventory_response_contains_as_of_raw(monkeypatch):
    app = _build_app()
    snapshot_payload = {
        "as_of": "2025-12-17T10:00:00+00:00",
        "items": [{"asin": "ASIN1", "sellable": 5}],
    }

    monkeypatch.setattr(routes, "_load_inventory_snapshot", lambda *_, **__: snapshot_payload)
    monkeypatch.setattr(routes, "get_refresh_metadata", lambda *_, **__: {"in_progress": False})
    monkeypatch.setattr(
        routes,
        "_now_utc",
        lambda: datetime(2025, 12, 17, 12, 0, tzinfo=timezone.utc),
    )

    client = TestClient(app)
    resp = client.get("/api/vendor/rt-inventory")
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is True
    assert data["as_of_raw"] == snapshot_payload["as_of"]
    assert data["as_of"] == "2025-12-17T10:00:00+00:00"
    assert data["as_of_utc"] == "2025-12-17T10:00:00+00:00"
    assert data["items"] == snapshot_payload["items"]
    assert pytest.approx(data["stale_hours"], rel=1e-3) == 2.0


def test_rt_inventory_response_handles_missing_as_of(monkeypatch):
    app = _build_app()
    snapshot_payload = {
        "as_of": None,
        "items": [],
    }

    monkeypatch.setattr(routes, "_load_inventory_snapshot", lambda *_, **__: snapshot_payload)
    monkeypatch.setattr(routes, "get_refresh_metadata", lambda *_, **__: {"in_progress": False})
    monkeypatch.setattr(routes, "_now_utc", lambda: datetime(2025, 12, 17, tzinfo=timezone.utc))

    client = TestClient(app)
    resp = client.get("/api/vendor/rt-inventory")
    assert resp.status_code == 200
    data = resp.json()

    assert data["as_of_raw"] is None
    assert data["as_of"] is None
    assert data["as_of_utc"] is None
    assert data["as_of_uae"] is None
    assert data["stale_hours"] is None
