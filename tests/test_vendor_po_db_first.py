from __future__ import annotations

from datetime import datetime, timezone

from services import db as db_service
from services import vendor_po_store as store_module
from services.vendor_po_lock import acquire_vendor_po_lock, release_vendor_po_lock
from services.vendor_po_store import (
    ensure_vendor_po_schema,
    export_vendor_pos_snapshot,
    get_vendor_po_list,
    replace_vendor_po_lines,
    update_header_totals_from_lines,
    upsert_vendor_po_headers,
)
from services.vendor_po_store import (
    get_vendor_po as store_get_vendor_po,
)


def _setup_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    monkeypatch.setattr(db_service, "CATALOG_DB_PATH", db_path)
    monkeypatch.setattr(store_module, "SCHEMA_ENSURED", False, raising=False)
    ensure_vendor_po_schema()
    return db_path


def _sample_po(po_number: str = "PO-TEST") -> dict:
    return {
        "purchaseOrderNumber": po_number,
        "purchaseOrderDate": "2025-12-01T00:00:00Z",
        "purchaseOrderState": "OPEN",
        "orderDetails": {
            "purchaseOrderDate": "2025-12-01T00:00:00Z",
            "items": [],
        },
    }


def test_db_first_read_without_cache(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    upsert_vendor_po_headers([_sample_po("PO-1")], source="test", source_detail="unit")

    rows = get_vendor_po_list()
    assert len(rows) == 1
    assert rows[0]["purchaseOrderNumber"] == "PO-1"
    assert rows[0]["_source"] == "test"


def test_upsert_idempotent(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    upsert_vendor_po_headers([_sample_po("PO-123")], source="test", source_detail="first")
    upsert_vendor_po_headers([_sample_po("PO-123")], source="test", source_detail="second")

    rows = get_vendor_po_list()
    assert len(rows) == 1
    assert rows[0]["_source"] == "test"


def test_lock_enforces_single_owner(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    acquired, state = acquire_vendor_po_lock("owner-1")
    assert acquired
    assert state["sync_in_progress"]

    acquired_second, state_second = acquire_vendor_po_lock("owner-2")
    assert not acquired_second
    assert state_second["sync_in_progress"]

    release = release_vendor_po_lock("owner-1", status="SUCCESS")
    assert not release["sync_in_progress"]


def test_totals_update_from_lines(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    po_number = "PO-TOTALS"
    upsert_vendor_po_headers([_sample_po(po_number)], source="test", source_detail="totals")

    lines = [
        {
            "item_sequence_number": "1",
            "asin": "ASIN1",
            "vendor_sku": "SKU1",
            "barcode": "",
            "title": "Sample",
            "image": "",
            "ordered_qty": 10,
            "accepted_qty": 8,
            "cancelled_qty": 1,
            "received_qty": 3,
            "pending_qty": 5,
            "shortage_qty": 1,
            "net_cost_amount": 5.0,
            "net_cost_currency": "AED",
            "list_price_amount": None,
            "list_price_currency": None,
            "last_updated_at": datetime.now(timezone.utc).isoformat(),
            "raw": {},
            "ship_to_location": "DXB",
        },
        {
            "item_sequence_number": "2",
            "asin": "ASIN2",
            "vendor_sku": "SKU2",
            "barcode": "",
            "title": "Sample 2",
            "image": "",
            "ordered_qty": 5,
            "accepted_qty": 5,
            "cancelled_qty": 0,
            "received_qty": 2,
            "pending_qty": 3,
            "shortage_qty": 0,
            "net_cost_amount": 3.0,
            "net_cost_currency": "AED",
            "list_price_amount": None,
            "list_price_currency": None,
            "last_updated_at": datetime.now(timezone.utc).isoformat(),
            "raw": {},
            "ship_to_location": "DXB",
        },
    ]

    replace_vendor_po_lines(po_number, lines)
    totals_payload = {
        "requested_qty": 15,
        "accepted_qty": 13,
        "received_qty": 5,
        "cancelled_qty": 1,
    }
    update_header_totals_from_lines(
        po_number,
        totals_payload,
        total_cost=49.0,
        cost_currency="AED",
    )

    po = store_get_vendor_po(po_number)
    assert po["requestedQty"] == 15
    assert po["acceptedQty"] == 13
    assert po["receivedQty"] == 5
    assert po["cancelledQty"] == 1
    assert po["total_accepted_cost"] == 49.0
    assert po["totalAcceptedCostCurrency"] == "AED"


def test_export_snapshot_lists_raw_json(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    upsert_vendor_po_headers([_sample_po("PO-EXP")], source="test", source_detail="export")

    snapshot = export_vendor_pos_snapshot()
    assert snapshot["items"]
    assert snapshot["items"][0]["purchaseOrderNumber"] == "PO-EXP"
