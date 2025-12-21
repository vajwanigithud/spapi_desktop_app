import contextlib
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import worker_status_routes as routes
from services import vendor_inventory_realtime as rt_inventory
from services import vendor_realtime_sales as rt_sales


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    return app


def test_worker_status_endpoint_returns_domains(monkeypatch):
    monkeypatch.setattr(routes, "get_db_connection", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(routes, "ensure_app_kv_table", lambda: None)
    monkeypatch.setattr(rt_inventory, "COOLDOWN_HOURS", 1, raising=False)
    monkeypatch.setattr(routes, "get_app_kv", lambda *_args, **_kwargs: "2025-12-21T15:00:00Z")
    monkeypatch.setattr(
        routes,
        "get_refresh_metadata",
        lambda *_args, **_kwargs: {
            "last_refresh_finished_at": "2025-12-21T15:00:00Z",
            "last_refresh_status": "SUCCESS",
            "last_error": None,
            "in_progress": False,
        },
    )
    monkeypatch.setattr(
        routes,
        "get_ledger_summary",
        lambda *_args, **_kwargs: {
            "missing": 0,
            "requested": 0,
            "downloaded": 0,
            "applied": 1,
            "failed": 0,
            "next_claimable_hour_utc": None,
            "last_applied_hour_utc": "2025-12-21T15:00:00+00:00",
        },
    )
    monkeypatch.setattr(routes, "get_worker_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rt_sales, "is_in_quota_cooldown", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(rt_sales, "get_quota_cooldown_until", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_vendor_po_status_payload", lambda *_args, **_kwargs: {"last_success_at": None})

    app = _build_app()
    client = TestClient(app)

    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.json()

    assert data["ok"] is True
    assert "summary" in data and "overall" in data["summary"]
    assert "domains" in data
    domains = data["domains"]
    assert "inventory" in domains and "workers" in domains["inventory"]
    assert "rt_sales" in domains and "workers" in domains["rt_sales"]
    assert "vendor_po" in domains and "workers" in domains["vendor_po"]
    assert isinstance(domains["inventory"]["workers"], list)
    assert isinstance(domains["rt_sales"]["workers"], list)
    assert isinstance(domains["vendor_po"]["workers"], list)

    first_inventory = domains["inventory"]["workers"][0]
    assert "status" in first_inventory
    assert "reason_code" in first_inventory
    assert "next_eligible_at_utc" in first_inventory
    assert data["summary"]["error_count"] == 0


def test_rt_sales_waits_on_lock(monkeypatch):
    future = datetime(2025, 12, 21, 16, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(routes, "get_db_connection", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(routes, "ensure_app_kv_table", lambda: None)
    monkeypatch.setattr(rt_inventory, "COOLDOWN_HOURS", 1, raising=False)
    monkeypatch.setattr(routes, "get_app_kv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_refresh_metadata", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        routes,
        "get_ledger_summary",
        lambda *_args, **_kwargs: {"last_applied_hour_utc": "2025-12-21T15:00:00+00:00", "failed": 0},
    )
    monkeypatch.setattr(routes, "get_worker_lock", lambda *_args, **_kwargs: {"expires_at": future.isoformat(), "owner": "test"})
    monkeypatch.setattr(rt_sales, "is_in_quota_cooldown", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(rt_sales, "get_quota_cooldown_until", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "datetime", type("dt", (), {"now": staticmethod(lambda tz=None: future), "fromisoformat": datetime.fromisoformat}))

    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    worker = next(w for w in resp.json()["domains"]["rt_sales"]["workers"] if w["key"] == "rt_sales_sync")
    assert worker["status"] == "waiting"
    assert worker["reason_code"] == "worker_lock"
    assert worker["next_eligible_at_utc"] is not None


def test_rt_sales_waits_on_cooldown(monkeypatch):
    now_utc = datetime(2025, 12, 21, 15, 0, tzinfo=timezone.utc)
    cooldown_until = now_utc + timedelta(minutes=10)

    monkeypatch.setattr(routes, "get_db_connection", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(routes, "ensure_app_kv_table", lambda: None)
    monkeypatch.setattr(rt_inventory, "COOLDOWN_HOURS", 1, raising=False)
    monkeypatch.setattr(routes, "get_app_kv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_refresh_metadata", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        routes,
        "get_ledger_summary",
        lambda *_args, **_kwargs: {"last_applied_hour_utc": "2025-12-21T14:45:00+00:00", "failed": 0},
    )
    monkeypatch.setattr(routes, "get_worker_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rt_sales, "is_in_quota_cooldown", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(rt_sales, "get_quota_cooldown_until", lambda *_args, **_kwargs: cooldown_until)
    monkeypatch.setattr(routes, "datetime", type("dt", (), {"now": staticmethod(lambda tz=None: now_utc), "fromisoformat": datetime.fromisoformat}))

    app = _build_app()
    client = TestClient(app)
    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    worker = next(w for w in resp.json()["domains"]["rt_sales"]["workers"] if w["key"] == "rt_sales_sync")
    assert worker["status"] == "waiting"
    assert worker["reason_code"] == "cooldown"
    assert worker["next_eligible_at_utc"] is not None
