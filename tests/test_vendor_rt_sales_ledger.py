import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from services import vendor_rt_sales_ledger as ledger


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger.db"

    @contextlib.contextmanager
    def _conn_ctx():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(ledger, "get_db_connection", _conn_ctx)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ledger.ensure_vendor_rt_sales_ledger_table(conn)
    return db_path


def test_ensure_hours_exist_idempotent(ledger_db):
    marketplace = "A1"
    hours = [
        "2025-12-17T04:00:00+00:00",
        "2025-12-17T05:00:00+00:00",
    ]
    inserted_first = ledger.ensure_hours_exist(marketplace, hours)
    inserted_second = ledger.ensure_hours_exist(marketplace, hours)

    rows = ledger.list_ledger_rows(marketplace, 10)

    assert inserted_first == len(hours)
    assert inserted_second == 0
    assert [row["status"] for row in rows] == [ledger.STATUS_MISSING] * len(hours)


def test_claim_next_missing_hour_transitions_to_requested(ledger_db):
    marketplace = "A1"
    hour = "2025-12-17T04:00:00+00:00"
    ledger.ensure_hours_exist(marketplace, [hour])

    claimed = ledger.claim_next_missing_hour(marketplace, datetime(2025, 12, 17, 5, tzinfo=timezone.utc))

    assert claimed is not None
    assert claimed["hour_utc"] == hour
    assert claimed["status"] == ledger.STATUS_REQUESTED
    assert claimed["attempt_count"] == 1

    stored = ledger.list_ledger_rows(marketplace, 1)[0]
    assert stored["status"] == ledger.STATUS_REQUESTED


def test_mark_failed_sets_cooldown(ledger_db):
    marketplace = "A1"
    hour = "2025-12-17T04:00:00+00:00"
    ledger.ensure_hours_exist(marketplace, [hour])

    ledger.mark_failed(marketplace, hour, "boom", cooldown_minutes=15)

    row = ledger.list_ledger_rows(marketplace, 1)[0]
    assert row["status"] == ledger.STATUS_FAILED
    assert row["last_error"] == "boom"
    assert row["next_retry_utc"] is not None

    retry_dt = datetime.fromisoformat(row["next_retry_utc"])
    updated_dt = datetime.fromisoformat(row["updated_at_utc"])
    assert retry_dt >= updated_dt + timedelta(minutes=15) - timedelta(seconds=1)


def test_claim_sequence_advances_after_apply(ledger_db):
    marketplace = "A1"
    base_hour = datetime(2025, 12, 17, 4, tzinfo=timezone.utc)
    hours = [(base_hour + timedelta(hours=offset)).isoformat() for offset in range(3)]
    ledger.ensure_hours_exist(marketplace, hours)

    first = ledger.claim_next_missing_hour(marketplace, base_hour + timedelta(hours=5))
    assert first is not None
    ledger.mark_applied(marketplace, first["hour_utc"])

    second = ledger.claim_next_missing_hour(marketplace, base_hour + timedelta(hours=6))
    assert second is not None
    assert second["hour_utc"] != first["hour_utc"]


def test_set_report_id_persists_without_status_change(ledger_db):
    marketplace = "A1"
    hour = "2025-12-17T04:00:00+00:00"
    ledger.ensure_hours_exist(marketplace, [hour])
    claimed = ledger.claim_next_missing_hour(marketplace, datetime(2025, 12, 17, 5, tzinfo=timezone.utc))
    assert claimed is not None
    assert claimed["status"] == ledger.STATUS_REQUESTED

    ledger.set_report_id(marketplace, hour, "RPT-123")
    row = ledger.list_ledger_rows(marketplace, 1)[0]
    assert row["status"] == ledger.STATUS_REQUESTED
    assert row["report_id"] == "RPT-123"


def test_ensure_table_migrates_legacy_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE vendor_rt_sales_hour_ledger (
            marketplace_id TEXT NOT NULL,
            hour TEXT NOT NULL,
            status TEXT NOT NULL,
            report_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            next_retry_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO vendor_rt_sales_hour_ledger (
            marketplace_id, hour, status, report_id,
            attempt_count, last_error, next_retry_utc,
            created_at_utc, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "A1",
            "2025-12-17T04:00:00+00:00",
            ledger.STATUS_MISSING,
            None,
            0,
            None,
            None,
            "2025-12-17T04:00:00+00:00",
            "2025-12-17T04:00:00+00:00",
        ),
    )
    conn.commit()
    ledger.ensure_vendor_rt_sales_ledger_table(conn)
    info = conn.execute("PRAGMA table_info(vendor_rt_sales_hour_ledger)").fetchall()
    assert any(col["name"] == "hour_utc" for col in info)
    legacy_exists = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='vendor_rt_sales_hour_ledger_old'
        """
    ).fetchone()
    conn.close()
    assert legacy_exists is None

    @contextlib.contextmanager
    def _conn_ctx():
        db_conn = sqlite3.connect(db_path)
        db_conn.row_factory = sqlite3.Row
        try:
            yield db_conn
        finally:
            db_conn.close()

    monkeypatch.setattr(ledger, "get_db_connection", _conn_ctx)
    inserted = ledger.ensure_hours_exist("A1", ["2025-12-17T05:00:00+00:00"])
    assert inserted == 1
