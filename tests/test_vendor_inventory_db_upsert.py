from __future__ import annotations

import os

from services import db as db_service
from services.db import (
    ensure_vendor_inventory_table,
    get_db_connection,
    get_vendor_inventory_snapshot,
    replace_vendor_inventory_snapshot,
)

os.environ.setdefault("LWA_CLIENT_ID", "dummy")
os.environ.setdefault("LWA_CLIENT_SECRET", "dummy")
os.environ.setdefault("LWA_REFRESH_TOKEN", "dummy")


def _inventory_row(marketplace_id: str, asin: str, sellable_units: int, start: str, end: str, updated_at: str):
    return {
        "marketplace_id": marketplace_id,
        "asin": asin,
        "start_date": start,
        "end_date": end,
        "sellable_onhand_units": sellable_units,
        "sellable_onhand_cost": float(sellable_units),
        "unsellable_onhand_units": 0,
        "unsellable_onhand_cost": 0.0,
        "aged90plus_sellable_units": 0,
        "aged90plus_sellable_cost": 0.0,
        "unhealthy_units": 0,
        "unhealthy_cost": 0.0,
        "net_received_units": 0,
        "net_received_cost": 0.0,
        "open_po_units": 0,
        "unfilled_customer_ordered_units": 0,
        "vendor_confirmation_rate": 0.0,
        "sell_through_rate": 0.0,
        "updated_at": updated_at,
    }


def test_upsert_preserves_existing_asins(tmp_path, monkeypatch):
    db_path = tmp_path / "inventory.db"
    monkeypatch.setattr(db_service, "CATALOG_DB_PATH", db_path)
    ensure_vendor_inventory_table()

    marketplace_id = "TEST-MKT"
    start_a = "2024-01-01T00:00:00Z"
    end_a = "2024-01-07T00:00:00Z"
    start_b = "2024-02-01T00:00:00Z"
    end_b = "2024-02-07T00:00:00Z"

    initial_rows = [
        _inventory_row(marketplace_id, "ASIN-A", 5, start_a, end_a, "2024-01-07T00:00:00Z"),
        _inventory_row(marketplace_id, "ASIN-B", 2, start_b, end_b, "2024-02-07T00:00:00Z"),
    ]

    with get_db_connection() as conn:
        replace_vendor_inventory_snapshot(conn, marketplace_id, initial_rows)
        first_snapshot = get_vendor_inventory_snapshot(conn, marketplace_id)
    assert {row["asin"] for row in first_snapshot} == {"ASIN-A", "ASIN-B"}

    updated_start = "2024-01-08T00:00:00Z"
    updated_end = "2024-01-14T00:00:00Z"
    updated_rows = [
        _inventory_row(marketplace_id, "ASIN-A", 9, updated_start, updated_end, "2024-01-14T00:00:00Z"),
    ]

    with get_db_connection() as conn:
        replace_vendor_inventory_snapshot(conn, marketplace_id, updated_rows)
        final_snapshot = get_vendor_inventory_snapshot(conn, marketplace_id)

    data_by_asin = {row["asin"]: row for row in final_snapshot}
    assert set(data_by_asin) == {"ASIN-A", "ASIN-B"}
    assert data_by_asin["ASIN-A"]["sellable_onhand_units"] == 9
    assert data_by_asin["ASIN-A"]["start_date"] == updated_start
    assert data_by_asin["ASIN-A"]["end_date"] == updated_end
    assert data_by_asin["ASIN-B"]["sellable_onhand_units"] == 2
    assert data_by_asin["ASIN-B"]["start_date"] == start_b


def test_partial_payload_accumulates_asins(tmp_path, monkeypatch):
    db_path = tmp_path / "inventory.db"
    monkeypatch.setattr(db_service, "CATALOG_DB_PATH", db_path)
    ensure_vendor_inventory_table()

    marketplace_id = "TEST-MKT"
    now = "2024-03-01T00:00:00Z"

    initial_rows = [
        _inventory_row(marketplace_id, "ASIN-A", 5, now, now, now),
        _inventory_row(marketplace_id, "ASIN-B", 2, now, now, now),
    ]

    with get_db_connection() as conn:
        replace_vendor_inventory_snapshot(conn, marketplace_id, initial_rows)
        first = get_vendor_inventory_snapshot(conn, marketplace_id)

    assert len(first) == 2

    # Second payload omits ASIN-A and introduces ASIN-C (partial/windowed report)
    later = "2024-03-02T00:00:00Z"
    second_rows = [
        _inventory_row(marketplace_id, "ASIN-B", 7, later, later, later),
        _inventory_row(marketplace_id, "ASIN-C", 3, later, later, later),
    ]

    with get_db_connection() as conn:
        replace_vendor_inventory_snapshot(conn, marketplace_id, second_rows)
        final = get_vendor_inventory_snapshot(conn, marketplace_id)

    asins = {row["asin"] for row in final}
    assert asins == {"ASIN-A", "ASIN-B", "ASIN-C"}
    # Count is monotonic non-decreasing
    assert len(final) >= len(first)
    # ASIN-B quantity updated, ASIN-A retained
    by_asin = {row["asin"]: row for row in final}
    assert by_asin["ASIN-B"]["sellable_onhand_units"] == 7
    assert by_asin["ASIN-A"]["sellable_onhand_units"] == 5
