from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from services import db as db_service
from services.db import ensure_vendor_inventory_table, get_db_connection, get_vendor_inventory_snapshot

os.environ.setdefault("LWA_CLIENT_ID", "dummy")
os.environ.setdefault("LWA_CLIENT_SECRET", "dummy")
os.environ.setdefault("LWA_REFRESH_TOKEN", "dummy")

from services.vendor_inventory_realtime import materialize_vendor_inventory_snapshot


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "inventory.db"
    monkeypatch.setattr(db_service, "CATALOG_DB_PATH", db_path)
    ensure_vendor_inventory_table()
    yield db_path


def _sample_snapshot():
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "generated_at": now_iso,
        "marketplace_id": "A2VIGQ35RCS4UG",
        "report_start_time": now_iso,
        "report_end_time": now_iso,
        "items": [
            {
                "asin": "B0TEST1234",
                "sellable": 7,
                "startTime": now_iso,
                "endTime": now_iso,
            }
        ],
    }


def test_materialize_snapshot_writes_vendor_inventory_rows(temp_db):
    snapshot = _sample_snapshot()
    written = materialize_vendor_inventory_snapshot(snapshot, source="test")
    assert written == 1
    with get_db_connection() as conn:
        rows = get_vendor_inventory_snapshot(conn, snapshot["marketplace_id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["asin"] == "B0TEST1234"
    assert row["sellable_onhand_units"] == 7
