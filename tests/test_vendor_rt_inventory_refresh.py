from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.vendor_rt_inventory_state import (
    ensure_vendor_rt_inventory_state_table,
    get_refresh_metadata,
    set_checkpoint,
    set_refresh_metadata,
)
from services.vendor_rt_inventory_sync import refresh_vendor_rt_inventory_singleflight


MARKETPLACE_ID = "A2TEST123"


def _new_db(tmp_path) -> Path:
    path = tmp_path / "rt_inventory.db"
    path.touch()
    ensure_vendor_rt_inventory_state_table(path)
    return path


def test_refresh_dedupes_when_in_progress(tmp_path):
    db_path = _new_db(tmp_path)
    meta = {
        "in_progress": True,
        "last_refresh_started_at": datetime.now(timezone.utc).isoformat(),
        "last_refresh_status": "IN_PROGRESS",
        "last_refresh_finished_at": None,
        "last_error": None,
    }
    set_refresh_metadata(MARKETPLACE_ID, meta, db_path=db_path)

    call_counter = {"count": 0}

    def fake_sync(*args, **kwargs):
        call_counter["count"] += 1
        return {"status": "synced"}

    result = refresh_vendor_rt_inventory_singleflight(
        MARKETPLACE_ID,
        db_path=db_path,
        sync_callable=fake_sync,
    )

    assert result["status"] == "refresh_in_progress"
    assert call_counter["count"] == 0


def test_refresh_skips_when_snapshot_fresh(tmp_path):
    db_path = _new_db(tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    set_checkpoint(MARKETPLACE_ID, recent, db_path=db_path)

    call_counter = {"count": 0}

    def fake_sync(*args, **kwargs):
        call_counter["count"] += 1
        return {"status": "synced"}

    result = refresh_vendor_rt_inventory_singleflight(
        MARKETPLACE_ID,
        db_path=db_path,
        sync_callable=fake_sync,
        freshness_hours=24,
    )

    assert result["status"] == "fresh_skipped"
    assert call_counter["count"] == 0


def test_refresh_runs_once_and_marks_success(tmp_path):
    db_path = _new_db(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    set_checkpoint(MARKETPLACE_ID, stale, db_path=db_path)

    call_counter = {"count": 0}

    def fake_sync(marketplace_id, *, db_path, hours, include_items):
        call_counter["count"] += 1
        new_as_of = datetime.now(timezone.utc).isoformat()
        set_checkpoint(marketplace_id, new_as_of, db_path=db_path)
        return {"status": "synced", "as_of": new_as_of}

    result = refresh_vendor_rt_inventory_singleflight(
        MARKETPLACE_ID,
        db_path=db_path,
        sync_callable=fake_sync,
        freshness_hours=1,
    )

    assert result["status"] == "refreshed"
    assert result["source"] == "refreshed"
    assert call_counter["count"] == 1

    meta = get_refresh_metadata(MARKETPLACE_ID, db_path=db_path)
    assert meta["last_refresh_status"] == "SUCCESS"
    assert meta["last_refresh_started_at"]
    assert meta["last_refresh_finished_at"]
    assert not meta["in_progress"]
