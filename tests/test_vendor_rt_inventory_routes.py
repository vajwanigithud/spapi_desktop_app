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


def _mock_inventory_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    items: list[dict],
    as_of: str | None,
    sales_map: dict | Exception | None = None,
) -> None:
    monkeypatch.setattr(routes, "get_state_rows", lambda *_, **__: items)
    monkeypatch.setattr(routes, "get_checkpoint", lambda *_, **__: as_of)
    monkeypatch.setattr(routes, "get_state_max_end_time", lambda *_, **__: None)
    monkeypatch.setattr(routes, "_load_catalog_metadata", lambda *_, **__: {})
    if isinstance(sales_map, Exception):
        def _boom(*args, **kwargs):
            raise sales_map
        monkeypatch.setattr(routes, "load_sales_30d_map", _boom)
    else:
        monkeypatch.setattr(routes, "load_sales_30d_map", lambda *_, **__: sales_map or {})


def test_rt_inventory_endpoint_includes_as_of_fields(monkeypatch):
    app = _build_app()
    _mock_inventory_state(
        monkeypatch,
        items=[{"asin": "ASIN1", "sellable": 7}],
        as_of="2025-12-17T10:00:00+00:00",
    )
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
    assert data["as_of_raw"] == "2025-12-17T10:00:00+00:00"
    assert data["as_of"] == "2025-12-17T10:00:00+00:00"
    assert data["as_of_utc"] == "2025-12-17T10:00:00+00:00"
    assert data["as_of_uae"].startswith("2025-12-17")
    assert pytest.approx(data["stale_hours"], rel=1e-3) == 2.0
    assert data["items"][0]["asin"] == "ASIN1"


def test_rt_inventory_handles_missing_as_of(monkeypatch):
    app = _build_app()
    _mock_inventory_state(monkeypatch, items=[], as_of=None)
    monkeypatch.setattr(routes, "get_refresh_metadata", lambda *_, **__: {"in_progress": False})

    client = TestClient(app)
    resp = client.get("/api/vendor/rt-inventory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["as_of_raw"] is None
    assert data["as_of"] is None
    assert data["as_of_utc"] is None
    assert data["as_of_uae"] is None
    assert data["stale_hours"] is None


def test_rt_inventory_includes_sales_30d(monkeypatch):
    app = _build_app()
    _mock_inventory_state(
        monkeypatch,
        items=[
            {"asin": "ASIN_A", "sellable": 5},
            {"asin": "ASIN_B", "sellable": 1},
        ],
        as_of="2025-12-17T10:00:00+00:00",
        sales_map={"ASIN_A": 12},
    )
    monkeypatch.setattr(routes, "get_refresh_metadata", lambda *_, **__: {"in_progress": False})

    client = TestClient(app)
    resp = client.get("/api/vendor/rt-inventory")
    assert resp.status_code == 200
    items = resp.json()["items"]
    sales = {row["asin"]: row.get("sales_30d") for row in items}
    assert sales["ASIN_A"] == 12
    assert sales["ASIN_B"] == 0


def test_rt_inventory_sales_30d_loader_failure(monkeypatch):
    app = _build_app()
    _mock_inventory_state(
        monkeypatch,
        items=[{"asin": "ASIN_X", "sellable": 3}],
        as_of="2025-12-17T10:00:00+00:00",
        sales_map=RuntimeError("boom"),
    )
    monkeypatch.setattr(routes, "get_refresh_metadata", lambda *_, **__: {"in_progress": False})

    client = TestClient(app)
    resp = client.get("/api/vendor/rt-inventory")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["sales_30d"] == 0
