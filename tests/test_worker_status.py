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


def _stub_worker_status_dependencies(
    monkeypatch,
    *,
    now_utc: datetime,
    last_applied_iso: str | None,
    ledger_failed: int = 0,
    cooldown: bool = False,
    cooldown_until_iso: str | None = None,
    lock_row: dict | None = None,
    inventory_last_refresh_iso: str | None = "2025-12-21T15:00:00Z",
    inventory_refresh_status: str = "SUCCESS",
    inventory_last_error: str | None = None,
    inventory_in_progress: bool = False,
) -> None:
    monkeypatch.setattr(routes, "_utcnow", lambda: now_utc)
    monkeypatch.setattr(routes, "get_db_connection", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(routes, "ensure_app_kv_table", lambda: None)
    monkeypatch.setattr(rt_inventory, "COOLDOWN_HOURS", 1, raising=False)
    monkeypatch.setattr(routes, "get_app_kv", lambda *_args, **_kwargs: inventory_last_refresh_iso)
    monkeypatch.setattr(
        routes,
        "get_refresh_metadata",
        lambda *_args, **_kwargs: {
            "last_refresh_finished_at": inventory_last_refresh_iso,
            "last_refresh_status": inventory_refresh_status,
            "last_error": inventory_last_error,
            "in_progress": inventory_in_progress,
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
            "failed": ledger_failed,
            "next_claimable_hour_utc": None,
            "last_applied_hour_utc": last_applied_iso,
        },
    )
    monkeypatch.setattr(routes, "get_worker_lock", lambda *_args, **_kwargs: lock_row)
    monkeypatch.setattr(rt_sales, "is_in_quota_cooldown", lambda *_args, **_kwargs: cooldown)
    monkeypatch.setattr(rt_sales, "get_quota_cooldown_until", lambda *_args, **_kwargs: cooldown_until_iso)
    monkeypatch.setattr(routes, "get_vendor_po_status_payload", lambda *_args, **_kwargs: {"last_success_at": None})
    monkeypatch.setattr(
        routes,
        "get_df_payments_worker_metadata",
        lambda *_args, **_kwargs: {
            "last_incremental_finished_at": None,
            "last_incremental_started_at": None,
            "last_incremental_status": "OK",
            "last_incremental_error": None,
            "incremental_next_eligible_at_utc": None,
            "incremental_worker_details": None,
            "incremental_worker_status": "ok",
            "incremental_last_success_at_utc": None,
            "incremental_auto_enabled": True,
        },
    )


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
    assert data["summary"]["overall"] == "ok"


def test_rt_sales_cooldown_not_error(monkeypatch):
    now_utc = datetime(2025, 12, 21, 15, 5, tzinfo=timezone.utc)
    cooldown_until = "2025-12-21T15:20:00+00:00"
    _stub_worker_status_dependencies(
        monkeypatch,
        now_utc=now_utc,
        last_applied_iso="2025-12-21T15:00:00+00:00",
        cooldown=True,
        cooldown_until_iso=cooldown_until,
    )

    app = _build_app()
    client = TestClient(app)

    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.json()

    worker = next(w for w in data["domains"]["rt_sales"]["workers"] if w["key"] == "rt_sales_sync")
    assert worker["status"] == "cooldown"
    assert worker["message"].startswith("Cooldown until")
    assert worker["next_run_utc"] is not None
    assert data["summary"]["error_count"] == 0
    assert data["summary"]["overall"] == "ok"


def test_inventory_cooldown_marked_waiting(monkeypatch):
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

    worker = next(w for w in data["domains"]["inventory"]["workers"] if w["key"] == "rt_inventory_refresh")
    assert worker["status"] == "waiting"
    assert worker["next_run_utc"] is not None
    assert "cooldown" in (worker["message"] or "").lower()
    assert data["summary"]["error_count"] == 0
    assert data["summary"]["overall"] == "ok"


def test_inventory_error_shows_reason(monkeypatch):
    now_utc = datetime(2025, 12, 21, 15, 5, tzinfo=timezone.utc)
    error_reason = "API failure"
    _stub_worker_status_dependencies(
        monkeypatch,
        now_utc=now_utc,
        last_applied_iso="2025-12-21T15:00:00+00:00",
        inventory_refresh_status="FAILED",
        inventory_last_error=error_reason,
        inventory_last_refresh_iso=None,
    )

    app = _build_app()
    client = TestClient(app)

    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.json()

    worker = next(w for w in data["domains"]["inventory"]["workers"] if w["key"] == "rt_inventory_refresh")
    assert worker["status"] == "error"
    assert worker["message"] == error_reason
    assert worker["next_run_utc"] is None
    assert data["summary"]["error_count"] >= 1
    assert data["summary"]["overall"] == "error"


def test_inventory_cooldown_with_error_shows_waiting(monkeypatch):
    now_utc = datetime(2025, 12, 21, 15, 5, tzinfo=timezone.utc)
    error_reason = "API failure"
    _stub_worker_status_dependencies(
        monkeypatch,
        now_utc=now_utc,
        last_applied_iso="2025-12-21T15:00:00+00:00",
        inventory_refresh_status="FAILED",
        inventory_last_error=error_reason,
    )

    app = _build_app()
    client = TestClient(app)

    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.json()

    refresh_worker = next(w for w in data["domains"]["inventory"]["workers"] if w["key"] == "rt_inventory_refresh")
    materializer_worker = next(w for w in data["domains"]["inventory"]["workers"] if w["key"] == "inventory_materializer")

    for worker in (refresh_worker, materializer_worker):
        assert worker["status"] == "waiting"
        msg = (worker.get("message") or "").lower()
        assert "cooldown" in msg
        assert "last error" in msg

    assert data["summary"]["error_count"] == 0
    assert data["summary"]["overall"] == "ok"


def test_overall_prioritizes_error(monkeypatch):
    now_utc = datetime(2025, 12, 21, 16, 40, tzinfo=timezone.utc)
    _stub_worker_status_dependencies(
        monkeypatch,
        now_utc=now_utc,
        last_applied_iso="2025-12-21T15:15:00+00:00",
        ledger_failed=1,
    )

    app = _build_app()
    client = TestClient(app)

    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.json()

    assert data["summary"]["error_count"] == 1
    assert data["summary"]["overall"] == "error"


def test_missing_schedule_marks_error(monkeypatch):
    now_utc = datetime(2025, 12, 21, 15, 5, tzinfo=timezone.utc)
    _stub_worker_status_dependencies(
        monkeypatch,
        now_utc=now_utc,
        last_applied_iso=None,
    )

    app = _build_app()
    client = TestClient(app)

    resp = client.get("/api/workers/status")
    assert resp.status_code == 200
    data = resp.json()

    worker = next(w for w in data["domains"]["rt_sales"]["workers"] if w["key"] == "rt_sales_sync")
    assert worker["status"] == "error"
    assert "schedule" in (worker["message"] or "").lower()
    assert data["summary"]["overall"] == "error"
