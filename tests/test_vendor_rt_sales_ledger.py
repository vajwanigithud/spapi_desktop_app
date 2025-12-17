from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import db as db_service
from services.db import get_db_connection, init_vendor_rt_sales_state_table
from services.vendor_realtime_sales import (
    LEDGER_STATUS_FAILED,
    ensure_vendor_rt_sales_hour_ledger_table,
    enqueue_vendor_rt_sales_hours,
    enqueue_vendor_rt_sales_specific_hours,
    process_rt_sales_hour_ledger,
    _ledger_mark_failed,
    _ledger_mark_requested,
    _ledger_plan_hours,
)


def _setup_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    monkeypatch.setattr(db_service, "CATALOG_DB_PATH", db_path)
    init_vendor_rt_sales_state_table()
    ensure_vendor_rt_sales_hour_ledger_table()
    return db_path


def _seed_last_ingested(marketplace: str, dt: datetime) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO vendor_rt_sales_state (marketplace_id, last_ingested_end_utc)
            VALUES (?, ?)
            """,
            (marketplace, _utc_iso(dt)),
        )
        conn.commit()


def test_ledger_inserts_unique_hours(tmp_path, monkeypatch):
    _setup_temp_db(tmp_path, monkeypatch)
    marketplace = "TEST-MKT"
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=3)

    inserted = enqueue_vendor_rt_sales_hours(marketplace, start, end)
    assert inserted == 3

    # Second enqueue should not duplicate
    inserted_again = enqueue_vendor_rt_sales_hours(marketplace, start, end)
    assert inserted_again == 0

    with get_db_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM vendor_rt_sales_hour_ledger WHERE marketplace_id = ?",
            (marketplace,),
        ).fetchone()["c"]
    assert count == 3


def test_ledger_request_once_behavior(tmp_path, monkeypatch):
    _setup_temp_db(tmp_path, monkeypatch)
    marketplace = "TEST-MKT"
    hour_start = datetime(2025, 1, 2, 5, tzinfo=timezone.utc)
    _seed_last_ingested(marketplace, hour_start)
    enqueue_vendor_rt_sales_specific_hours(marketplace, [hour_start])

    now = hour_start + timedelta(hours=2)
    hours = _ledger_plan_hours(marketplace, max_hours=5, now_utc=now)
    assert hours == [_utc_iso(hour_start)]

    claimed = _ledger_mark_requested(marketplace, hours[0])
    assert claimed

    # Once marked requested, planner should skip the hour
    hours_after = _ledger_plan_hours(marketplace, max_hours=5, now_utc=now)
    assert hours_after == []


def test_failed_hour_respects_cooldown(tmp_path, monkeypatch):
    _setup_temp_db(tmp_path, monkeypatch)
    marketplace = "TEST-MKT"
    hour_start = datetime(2025, 1, 3, tzinfo=timezone.utc)
    _seed_last_ingested(marketplace, hour_start)
    enqueue_vendor_rt_sales_specific_hours(marketplace, [hour_start])

    hour_iso = _utc_iso(hour_start)
    cooldown_until = _utc_iso(hour_start + timedelta(hours=1))
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE vendor_rt_sales_hour_ledger
            SET status = ?, cooldown_until = ?
            WHERE marketplace_id = ? AND hour_start_utc = ?
            """,
            (LEDGER_STATUS_FAILED, cooldown_until, marketplace, hour_iso),
        )
        conn.commit()

    future = hour_start + timedelta(minutes=30)
    hours = _ledger_plan_hours(marketplace, max_hours=1, now_utc=future)
    assert hours == []

    # After cooldown passes, hour becomes eligible again
    later = future + timedelta(hours=2)
    hours_later = _ledger_plan_hours(marketplace, max_hours=1, now_utc=later)
    assert hours_later == [hour_iso]


def test_planner_respects_safety_lag(tmp_path, monkeypatch):
    _setup_temp_db(tmp_path, monkeypatch)
    marketplace = "TEST-MKT"
    now = datetime(2025, 1, 4, 12, tzinfo=timezone.utc)
    eligible_hour = now - timedelta(hours=3)
    blocked_hour = now - timedelta(minutes=30)
    _seed_last_ingested(marketplace, eligible_hour)

    enqueue_vendor_rt_sales_specific_hours(marketplace, [eligible_hour, blocked_hour])

    hours = _ledger_plan_hours(marketplace, max_hours=5, now_utc=now)
    assert hours == [_utc_iso(eligible_hour)]


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
