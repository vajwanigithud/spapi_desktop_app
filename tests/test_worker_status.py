import contextlib
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import worker_status_routes as routes
from services import vendor_inventory_realtime as rt_inventory
from services import vendor_realtime_sales as rt_sales


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    return app


def _stub_worker_status_dependencies(monkeypatch, *, now_utc: datetime, last_applied_iso: str) -> None:
    monkeypatch.setattr(routes, "_utcnow", lambda: now_utc)
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
            "last_applied_hour_utc": last_applied_iso,
        },
    )
    monkeypatch.setattr(routes, "get_worker_lock", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rt_sales, "is_in_quota_cooldown", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(rt_sales, "get_quota_cooldown_until", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "get_vendor_po_status_payload", lambda *_args, **_kwargs: {"last_success_at": None})


def test_worker_status_endpoint_returns_domains(monkeypatch):
    now_utc = datetime(2025, 12, 21, 15, 5, tzinfo=timezone.utc)
    _stub_worker_status_dependencies(monkeypatch, now_utc=now_utc, last_applied_iso="2025-12-21T15:00:00+00:00")

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
    assert "last_run_utc" in first_inventory
    assert "next_run_utc" in first_inventory
    assert "mode" in first_inventory
    assert "message" in first_inventory
    assert data["summary"]["error_count"] == 0


def test_rt_sales_worker_marks_overdue(monkeypatch):
    now_utc = datetime(2025, 12, 21, 16, 40, tzinfo=timezone.utc)
    _stub_worker_status_dependencies(
        monkeypatch,
        now_utc=now_utc,
        last_applied_iso="2025-12-21T15:15:00+00:00",
    )

    app = _build_app()
    client = TestClient(app)

    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.json()

    worker = next(w for w in data["domains"]["rt_sales"]["workers"] if w["key"] == "rt_sales_sync")
    assert worker["status"] == "overdue"
    assert worker["overdue_by_minutes"] > 0
    assert worker["expected_interval_minutes"] == routes.RT_SALES_EXPECTED_INTERVAL_MINUTES
    assert worker["grace_minutes"] == routes.RT_SALES_GRACE_MINUTES
    assert data["summary"]["overall"] == "overdue"


def test_rt_sales_worker_waiting_before_next(monkeypatch):
    now_utc = datetime(2025, 12, 21, 15, 5, tzinfo=timezone.utc)
    _stub_worker_status_dependencies(
        monkeypatch,
        now_utc=now_utc,
        last_applied_iso="2025-12-21T15:00:00+00:00",
    )

    app = _build_app()
    client = TestClient(app)

    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.json()

    worker = next(w for w in data["domains"]["rt_sales"]["workers"] if w["key"] == "rt_sales_sync")
    assert worker["status"] == "waiting"
    assert worker["overdue_by_minutes"] == 0
    assert data["summary"]["overall"] == "waiting"
