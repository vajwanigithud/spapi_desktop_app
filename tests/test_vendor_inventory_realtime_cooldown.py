from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import db as db_service
from services import vendor_inventory_realtime as rt_inventory
from services.db import ensure_app_kv_table, get_app_kv, get_db_connection, set_app_kv


@pytest.mark.parametrize("delta_minutes", [5, 30])
def test_refresh_respects_hourly_cooldown(tmp_path, monkeypatch, delta_minutes):
    db_path = tmp_path / "cooldown.db"
    monkeypatch.setattr(db_service, "CATALOG_DB_PATH", db_path)
    ensure_app_kv_table()

    recent = datetime.now(timezone.utc) - timedelta(minutes=delta_minutes)
    with get_db_connection() as conn:
        set_app_kv(conn, rt_inventory.COOLDOWN_KV_KEY, recent.isoformat())
        stored_value = get_app_kv(conn, rt_inventory.COOLDOWN_KV_KEY)
    assert stored_value
    assert rt_inventory._parse_datetime(stored_value)

    call_counter = {"count": 0}

    observed = {"kv_value": None}
    original_get_app_kv = rt_inventory.get_app_kv

    def fake_get_app_kv(conn, key):
        value = original_get_app_kv(conn, key)
        observed["kv_value"] = value
        return value

    def fake_request_vendor_report(*_args, **_kwargs):
        call_counter["count"] += 1
        return "REPORT123"

    def fake_poll_vendor_report(*_args, **_kwargs):
        return {"reportDocumentId": "DOC123"}

    def fake_download_vendor_report_document(*_args, **_kwargs):
        return ({"items": []}, {})

    monkeypatch.setattr(rt_inventory, "get_app_kv", fake_get_app_kv)
    monkeypatch.setattr(rt_inventory, "request_vendor_report", fake_request_vendor_report)
    monkeypatch.setattr(rt_inventory, "poll_vendor_report", fake_poll_vendor_report)
    monkeypatch.setattr(rt_inventory, "download_vendor_report_document", fake_download_vendor_report_document)

    result = rt_inventory.refresh_realtime_inventory_snapshot("A2TESTMKT", cache_path=tmp_path / "snapshot.json")

    assert result["cooldown_active"] is True
    assert result["refresh_in_progress"] is False
    assert result["marketplace_id"] == "A2TESTMKT"
    assert "items" in result
    assert isinstance(result["items"], list)
    assert call_counter["count"] == 0
    assert observed["kv_value"]
    assert result["cooldown_until_utc"]
    cooldown_until = result["cooldown_until_utc"].replace("Z", "+00:00")
    cooldown_dt = datetime.fromisoformat(cooldown_until)
    assert cooldown_dt > recent
