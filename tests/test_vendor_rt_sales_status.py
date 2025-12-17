import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routes import vendor_rt_sales_routes as routes
from services import vendor_realtime_sales as vendor_rt
from services import vendor_rt_sales_ledger as ledger


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)
    return app


def _prepare_db(tmp_path, monkeypatch) -> Path:
    db_path = tmp_path / "rt_status.db"

    @contextlib.contextmanager
    def _conn_ctx():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(ledger, "get_db_connection", _conn_ctx)
    return db_path


def _seed_ledger(conn: sqlite3.Connection, marketplace_id: str, now: datetime) -> None:
    conn.execute(
        f"""
        INSERT INTO {ledger.LEDGER_TABLE} (
            marketplace_id, hour_utc, status, report_id,
            attempt_count, last_error, next_retry_utc,
            created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?, ?)
        """,
        (marketplace_id, "2025-01-01T01:00:00+00:00", ledger.STATUS_MISSING, now.isoformat(), now.isoformat()),
    )
    conn.execute(
        f"""
        INSERT INTO {ledger.LEDGER_TABLE} (
            marketplace_id, hour_utc, status, report_id,
            attempt_count, last_error, next_retry_utc,
            created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?, ?)
        """,
        (marketplace_id, "2025-01-01T02:00:00+00:00", ledger.STATUS_REQUESTED, now.isoformat(), now.isoformat()),
    )
    conn.execute(
        f"""
        INSERT INTO {ledger.LEDGER_TABLE} (
            marketplace_id, hour_utc, status, report_id,
            attempt_count, last_error, next_retry_utc,
            created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?, ?)
        """,
        (marketplace_id, "2025-01-01T03:00:00+00:00", ledger.STATUS_DOWNLOADED, now.isoformat(), now.isoformat()),
    )
    conn.execute(
        f"""
        INSERT INTO {ledger.LEDGER_TABLE} (
            marketplace_id, hour_utc, status, report_id,
            attempt_count, last_error, next_retry_utc,
            created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?, ?)
        """,
        (marketplace_id, "2025-01-01T04:00:00+00:00", ledger.STATUS_FAILED, now.isoformat(), now.isoformat()),
    )
    conn.execute(
        f"""
        INSERT INTO {ledger.LEDGER_TABLE} (
            marketplace_id, hour_utc, status, report_id,
            attempt_count, last_error, next_retry_utc,
            created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?, ?)
        """,
        (marketplace_id, "2025-01-01T05:00:00+00:00", ledger.STATUS_APPLIED, now.isoformat(), now.isoformat()),
    )


def test_vendor_rt_sales_status_includes_worker_lock_and_counts(tmp_path, monkeypatch):
    marketplace_id = "TEST-MKT"
    db_path = _prepare_db(tmp_path, monkeypatch)
    now = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ledger.ensure_vendor_rt_sales_ledger_table(conn)
        _seed_ledger(conn, marketplace_id, now)
        conn.execute(
            f"""
            UPDATE {ledger.LEDGER_TABLE}
            SET next_retry_utc = ?
            WHERE marketplace_id = ? AND status = ?
            """,
            ((now - timedelta(minutes=5)).isoformat(), marketplace_id, ledger.STATUS_FAILED),
        )
        conn.commit()

    ledger.acquire_worker_lock(marketplace_id, "auto-sync:123", ttl_seconds=1800)

    monkeypatch.setattr(vendor_rt, "is_in_quota_cooldown", lambda *_: False)
    monkeypatch.setattr(vendor_rt, "get_quota_cooldown_until", lambda: None)

    app = _build_app()
    client = TestClient(app)
    resp = client.get(f"/api/vendor/rt-sales/status?marketplace_id={marketplace_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["marketplace_id"] == marketplace_id

    worker_lock = data["worker_lock"]
    assert worker_lock["held"] is True
    assert worker_lock["owner"] == "auto-sync:123"
    assert worker_lock["stale"] is False

    cooldown = data["cooldown"]
    assert cooldown["active"] is True
    assert cooldown["reason"] == "lock_busy"

    summary = data["ledger_summary"]
    assert summary["missing"] == 1
    assert summary["requested"] == 1
    assert summary["downloaded"] == 1
    assert summary["failed"] == 1
    assert summary["applied"] == 1
    assert summary["last_applied_hour_utc"] == "2025-01-01T05:00:00+00:00"
    assert summary["next_claimable_hour_utc"] == "2025-01-01T01:00:00+00:00"


def test_vendor_rt_sales_status_handles_quota_cooldown(tmp_path, monkeypatch):
    marketplace_id = "TEST-MKT2"
    db_path = _prepare_db(tmp_path, monkeypatch)
    now = datetime(2025, 1, 2, 0, 0, tzinfo=timezone.utc)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ledger.ensure_vendor_rt_sales_ledger_table(conn)
        _seed_ledger(conn, marketplace_id, now)
        conn.commit()

    cooldown_until = now + timedelta(minutes=30)
    monkeypatch.setattr(vendor_rt, "is_in_quota_cooldown", lambda *_: True)
    monkeypatch.setattr(vendor_rt, "get_quota_cooldown_until", lambda: cooldown_until)

    app = _build_app()
    client = TestClient(app)
    resp = client.get(f"/api/vendor/rt-sales/status?marketplace_id={marketplace_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cooldown"]["active"] is True
    assert data["cooldown"]["reason"] == "quota"
    assert data["cooldown"]["until_utc"] == cooldown_until.isoformat()
    assert data["worker_lock"]["held"] is False
