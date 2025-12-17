from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from services import db as db_service
from services.db import get_db_connection
from services.vendor_po_lock import LOCK_TTL_SECONDS
from services.vendor_po_status_store import (
    get_vendor_po_status_payload,
    record_vendor_po_run_failure,
    record_vendor_po_run_start,
    record_vendor_po_run_success,
)
from services.vendor_po_store import ensure_vendor_po_schema


def _setup_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    monkeypatch.setattr(db_service, "CATALOG_DB_PATH", db_path)
    import services.vendor_po_store as po_store

    monkeypatch.setattr(po_store, "SCHEMA_ENSURED", False, raising=False)
    ensure_vendor_po_schema()


@contextmanager
def _vendor_po_test_client(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("LWA_CLIENT_ID", "dummy")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "dummy")
    monkeypatch.setenv("LWA_REFRESH_TOKEN", "dummy")
    import main

    monkeypatch.setattr(main, "start_vendor_rt_sales_startup_backfill_thread", lambda: None)
    monkeypatch.setattr(main, "start_vendor_rt_sales_auto_sync", lambda: None)

    def _fake_fetch(*args, **kwargs):
        return {"fetched": 0}

    monkeypatch.setattr(main, "_fetch_and_persist_vendor_pos", _fake_fetch)
    with TestClient(main.app) as client:
        yield client


def test_status_empty_db(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    payload = get_vendor_po_status_payload()
    assert payload["state"] == "idle"
    assert payload["lock"]["held"] is False
    assert payload["counts"]["headers"] == 0
    assert payload["counts"]["lines"] == 0
    assert payload["source"] == "DB"


def test_status_running_when_lock_held(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    future = now + timedelta(seconds=LOCK_TTL_SECONDS // 2)
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE vendor_po_sync_state
            SET sync_in_progress = 1,
                sync_started_at = ?,
                lock_owner = ?,
                lock_expires_at = ?
            WHERE id = 1
            """,
            (now.isoformat().replace("+00:00", "Z"), "worker", future.isoformat().replace("+00:00", "Z")),
        )
        conn.commit()
    record_vendor_po_run_start("sync", started_at=now.isoformat().replace("+00:00", "Z"))
    payload = get_vendor_po_status_payload()
    assert payload["state"] == "running"
    assert payload["lock"]["held"] is True
    assert payload["lock"]["stale"] is False


def test_status_stale_detected(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=LOCK_TTL_SECONDS + 120)
    expired = start + timedelta(seconds=LOCK_TTL_SECONDS)
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE vendor_po_sync_state
            SET sync_in_progress = 1,
                sync_started_at = ?,
                lock_owner = ?,
                lock_expires_at = ?
            WHERE id = 1
            """,
            (start.isoformat().replace("+00:00", "Z"), "worker", expired.isoformat().replace("+00:00", "Z")),
        )
        conn.commit()
    record_vendor_po_run_start("sync", started_at=start.isoformat().replace("+00:00", "Z"))
    payload = get_vendor_po_status_payload()
    assert payload["state"] == "error"
    assert payload["lock"]["stale"] is True
    assert payload["lock"]["stale_seconds"] is not None


def test_status_error_meta(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    started = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    finished = now.isoformat().replace("+00:00", "Z")
    record_vendor_po_run_start("sync", started_at=started)
    record_vendor_po_run_failure("boom", finished_at=finished)
    payload = get_vendor_po_status_payload()
    assert payload["state"] == "error"
    assert payload["last_error"] == "boom"
    assert payload["last_run_finished_at"] == finished


def test_status_duration_calculated(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    started = now.isoformat().replace("+00:00", "Z")
    finished = (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    record_vendor_po_run_start("sync", started_at=started)
    record_vendor_po_run_success(finished_at=finished)
    payload = get_vendor_po_status_payload()
    assert payload["state"] == "idle"
    assert payload["last_run_duration_s"] == 600


def test_sync_and_rebuild_allow_missing_body(tmp_path, monkeypatch):
    with _vendor_po_test_client(tmp_path, monkeypatch) as client:
        resp_sync = client.post("/api/vendor-pos/sync")
        assert resp_sync.status_code == 200
        data_sync = resp_sync.json()
        assert data_sync["status"] == "ok"

        resp_rebuild = client.post("/api/vendor-pos/rebuild")
        assert resp_rebuild.status_code == 200
        data_rebuild = resp_rebuild.json()
        assert data_rebuild["status"] == "ok"


def test_sync_and_rebuild_allow_empty_body(tmp_path, monkeypatch):
    with _vendor_po_test_client(tmp_path, monkeypatch) as client:
        resp_sync = client.post("/api/vendor-pos/sync", json={})
        assert resp_sync.status_code == 200
        data_sync = resp_sync.json()
        assert data_sync["status"] == "ok"

        resp_rebuild = client.post("/api/vendor-pos/rebuild", json={})
        assert resp_rebuild.status_code == 200
        data_rebuild = resp_rebuild.json()
        assert data_rebuild["status"] == "ok"


def test_sync_accepts_valid_created_after(tmp_path, monkeypatch):
    with _vendor_po_test_client(tmp_path, monkeypatch) as client:
        resp_sync = client.post("/api/vendor-pos/sync", json={"createdAfter": "2025-12-01T00:00:00Z"})
        assert resp_sync.status_code == 200
        data_sync = resp_sync.json()
        assert data_sync["status"] == "ok"


def test_sync_rejects_invalid_created_after(tmp_path, monkeypatch):
    with _vendor_po_test_client(tmp_path, monkeypatch) as client:
        resp_sync = client.post("/api/vendor-pos/sync", json={"createdAfter": "not-a-date"})
        assert resp_sync.status_code == 422
