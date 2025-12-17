from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routes import vendor_rt_inventory_routes as routes


def _build_app():
    app = FastAPI()
    app.include_router(routes.router)
    return app


def test_rt_inventory_endpoint_includes_as_of_fields(monkeypatch):
    app = _build_app()
    fixed_snapshot = {
        "as_of": "2025-12-17T10:00:00+00:00",
        "items": [
            {"asin": "ASIN1", "sellable": 7},
        ],
    }

    monkeypatch.setattr(
        routes,
        "_load_inventory_snapshot",
        lambda *args, **kwargs: fixed_snapshot,
    )
    monkeypatch.setattr(
        routes,
        "get_refresh_metadata",
        lambda *args, **kwargs: {"in_progress": False},
    )
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
    assert data["as_of"] == "2025-12-17T10:00:00+00:00"
    assert data["as_of_utc"] == "2025-12-17T10:00:00+00:00"
    assert data["as_of_uae"].startswith("2025-12-17")
    assert pytest.approx(data["stale_hours"], rel=1e-3) == 2.0
    assert data["items"] == fixed_snapshot["items"]
