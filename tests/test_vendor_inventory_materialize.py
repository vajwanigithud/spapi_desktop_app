from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from services import db as db_service
from services.db import (
    ensure_vendor_inventory_table,
    get_db_connection,
    get_vendor_inventory_snapshot,
)

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
            },
            {
                "asin": "B0TEST5678",
                "sellable": 5,
                "startTime": now_iso,
                "endTime": now_iso,
            },
        ],
    }


def test_materialize_snapshot_writes_vendor_inventory_rows(temp_db, monkeypatch):
    monkeypatch.delenv("INVENTORY_RT_PRUNE_MIN_KEEP", raising=False)
    snapshot = _sample_snapshot()
    prune_meta = materialize_vendor_inventory_snapshot(snapshot, source="test")
    assert isinstance(prune_meta, dict)
    for key in (
        "prune_attempted",
        "prune_skipped_reason",
        "prune_min_keep_count",
        "pruned_rows",
        "prune_kept_count",
        "prune_before_count",
    ):
        assert key in prune_meta

    assert prune_meta["prune_kept_count"] == 2
    assert prune_meta["prune_min_keep_count"] == 20
    assert prune_meta["prune_attempted"] is False
    assert prune_meta["prune_skipped_reason"] == "below_threshold"
    assert prune_meta["pruned_rows"] == 0

    refresh_meta = snapshot.get("refresh") or {}
    assert refresh_meta.get("prune_kept_count") == prune_meta["prune_kept_count"]
    assert refresh_meta.get("prune_min_keep_count") == prune_meta["prune_min_keep_count"]
    assert refresh_meta.get("prune_before_count") == prune_meta["prune_before_count"]

    with get_db_connection() as conn:
        rows = get_vendor_inventory_snapshot(conn, snapshot["marketplace_id"])
    assert len(rows) == 2
    asins = {row["asin"] for row in rows}
    assert {"B0TEST1234", "B0TEST5678"} == asins
    for row in rows:
        assert row["sellable_onhand_units"] in (7, 5)
