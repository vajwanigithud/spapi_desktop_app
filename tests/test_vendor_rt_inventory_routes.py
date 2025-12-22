from __future__ import annotations

import contextlib
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import vendor_inventory_realtime_routes as realtime_routes
from routes import vendor_rt_inventory_routes as legacy_routes
from services.spapi_reports import SpApiQuotaError
from services import vendor_inventory_realtime as inventory_service


def _build_app(include_legacy: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(realtime_routes.router)
    if include_legacy:
        app.include_router(legacy_routes.router)
    return app


def _sample_snapshot():
    now_iso = datetime(2025, 12, 17, 10, 0, tzinfo=timezone.utc).isoformat()
    return {
        "generated_at": now_iso,
        "report_start_time": now_iso,
        "report_end_time": now_iso,
        "items": [
            {"asin": "B0TEST001", "sellable": 5},
            {"asin": "B0TEST002", "sellable": 3},
        ],
        "age_seconds": 120,
        "age_hours": 0.03,
        "is_stale": False,
        "unique_count": 2,
        "raw_row_count": 2,
        "raw_nonempty_asin_count": 2,
        "raw_unique_asin_count": 2,
        "collapsed_unique_asin_count": 2,
        "normalized_sellable_sum": 8,
        "realtime_sellable_asins": 2,
        "realtime_sellable_units": 8,
        "catalog_asin_count": 0,
        "coverage_ratio": 0.0,
    }


def test_realtime_snapshot_endpoint_includes_catalog_and_sales(monkeypatch: pytest.MonkeyPatch):
    app = _build_app()
    snapshot = _sample_snapshot()
    # Pre-populate one row with an existing imageUrl to ensure we don't overwrite it
    snapshot["items"][1]["imageUrl"] = "https://existing/B0TEST002.jpg"

    monkeypatch.setattr(
        realtime_routes,
        "get_cached_realtime_inventory_snapshot",
        lambda: dict(snapshot),
    )
    monkeypatch.setattr(
        realtime_routes,
        "_load_catalog_metadata",
        lambda asins: {"B0TEST001": {"title": "Widget", "image_url": "https://img/1"}},
    )
    # Ensure catalog_images.attach_image_urls runs predictably without hitting SQLite.
    monkeypatch.setattr(
        realtime_routes,
        "attach_image_urls",
        lambda rows: rows,
    )
    monkeypatch.setattr(
        realtime_routes,
        "load_sales_30d_map",
        lambda *_: {"B0TEST001": 12, "B0TEST002": 0},
    )

    client = TestClient(app)
    resp = client.get("/api/vendor-inventory/realtime/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "ok"
    assert data["as_of"]
    assert data["as_of_uae"]
    assert data["items"][0]["title"] == "Widget"
    assert data["items"][0]["image_url"] == "https://img/1"
    assert data["items"][0]["imageUrl"] == "https://img/1"
    # Existing imageUrl is not overwritten
    assert data["items"][1]["imageUrl"] == "https://existing/B0TEST002.jpg"
    sales_map = {item["asin"]: item["sales_30d"] for item in data["items"]}
    assert sales_map["B0TEST001"] == 12
    assert sales_map["B0TEST002"] == 0


def test_accumulated_endpoint_includes_sales_map(monkeypatch: pytest.MonkeyPatch):
    app = _build_app()

    as_of = "2025-12-20T00:00:00Z"
    rows = [
        {
            "asin": "B0SALES",
            "sellable_onhand_units": 5,
            "updated_at": as_of,
        }
    ]

    monkeypatch.setattr(inventory_service, "ensure_vendor_inventory_table", lambda: None)
    monkeypatch.setattr(inventory_service, "get_db_connection", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(inventory_service, "get_app_kv", lambda *_args, **_kwargs: as_of)
    monkeypatch.setattr(inventory_service, "get_vendor_inventory_snapshot", lambda _conn, _mp: list(rows))
    monkeypatch.setattr(inventory_service, "load_sales_30d_map", lambda _mp: {"B0SALES": 9})

    client = TestClient(app)
    resp = client.get("/api/vendor-inventory/realtime/accumulated")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["rows"][0]["sales_30d"] == 9
    assert data["count"] == 1
    assert data["as_of_utc"] == as_of


def test_refresh_endpoint_handles_quota_error(monkeypatch: pytest.MonkeyPatch):
    app = _build_app()

    def _boom(*_args, **_kwargs):
        raise SpApiQuotaError("cooldown")

    monkeypatch.setattr(
        realtime_routes,
        "refresh_realtime_inventory_snapshot",
        _boom,
    )

    client = TestClient(app)
    resp = client.post("/api/vendor-inventory/realtime/refresh")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "quota_error"
    assert "cooldown" in data["error"]


def test_refresh_endpoint_respects_singleflight(monkeypatch: pytest.MonkeyPatch):
    app = _build_app()
    refresh_called = {"invocations": 0}

    def _refresh_callable(*_args, **_kwargs):
        refresh_called["invocations"] += 1
        return {}

    def _singleflight_stub(*_args, **_kwargs):
        return {
            "status": "refresh_in_progress",
            "source": "cache",
            "refresh": {"in_progress": True},
        }

    monkeypatch.setattr(realtime_routes, "refresh_realtime_inventory_snapshot", _refresh_callable)
    monkeypatch.setattr(realtime_routes, "refresh_vendor_rt_inventory_singleflight", _singleflight_stub)
    monkeypatch.setattr(realtime_routes, "get_cached_realtime_inventory_snapshot", lambda: {"items": []})

    client = TestClient(app)
    resp = client.post("/api/vendor-inventory/realtime/refresh")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "refresh_in_progress"
    assert data["source"] == "cache"
    assert data["refresh"]["in_progress"] is True
    assert refresh_called["invocations"] == 0


def test_realtime_health_endpoint(monkeypatch: pytest.MonkeyPatch):
    app = _build_app()
    snapshot = _sample_snapshot()
    snapshot["age_seconds"] = 180
    snapshot["age_hours"] = 0.05
    snapshot["is_stale"] = False

    monkeypatch.setattr(
        realtime_routes,
        "get_cached_realtime_inventory_snapshot",
        lambda: dict(snapshot),
    )

    client = TestClient(app)
    resp = client.get("/api/vendor-inventory/realtime/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "is_stale" in data
    assert "age_hours" in data
    assert data["unique_asins"] == 2


def test_realtime_health_endpoint_handles_missing_snapshot(monkeypatch: pytest.MonkeyPatch):
    app = _build_app()

    monkeypatch.setattr(
        realtime_routes,
        "get_cached_realtime_inventory_snapshot",
        lambda: {"items": []},
    )

    client = TestClient(app)
    resp = client.get("/api/vendor-inventory/realtime/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["reason"] == "no_snapshot"
    assert data["is_stale"] is True


def test_legacy_endpoint_delegates_to_realtime_snapshot(monkeypatch: pytest.MonkeyPatch):
    app = _build_app(include_legacy=True)
    snapshot = _sample_snapshot()
    snapshot["items"][0]["title"] = "LegacyWidget"

    monkeypatch.setattr(
        realtime_routes,
        "get_cached_realtime_inventory_snapshot",
        lambda: dict(snapshot),
    )
    monkeypatch.setattr(
        realtime_routes,
        "_load_catalog_metadata",
        lambda *_: {},
    )
    monkeypatch.setattr(
        realtime_routes,
        "load_sales_30d_map",
        lambda *_: {},
    )

    client = TestClient(app)
    resp = client.get("/api/vendor/rt-inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["items"][0]["title"] == "LegacyWidget"
