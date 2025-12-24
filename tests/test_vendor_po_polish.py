from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from services import db as db_service
from services.db import get_db_connection
from services.vendor_po_store import (
    ensure_vendor_po_schema,
    replace_vendor_po_lines,
    upsert_vendor_po_headers,
)
from services.vendor_po_view import compute_amount_reconciliation, compute_po_status


def _setup_tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    monkeypatch.setattr(db_service, "CATALOG_DB_PATH", db_path)
    import services.vendor_po_store as po_store

    monkeypatch.setattr(po_store, "SCHEMA_ENSURED", False, raising=False)
    ensure_vendor_po_schema()
    return db_path


def _seed_sample_po():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    po_payload = {
        "purchaseOrderNumber": "PO-TEST-1",
        "purchaseOrderDate": "2025-12-01T00:00:00Z",
        "requestedQty": 20,
        "acceptedQty": 10,
        "receivedQty": 2,
        "cancelledQty": 0,
        "remainingQty": 8,
        "totalAcceptedCostAmount": "50.00",
        "totalAcceptedCostCurrency": "AED",
        "orderDetails": {
            "shipToParty": {"partyId": "DXB1"},
            "items": [],
        },
        "lastUpdatedDate": now,
    }
    upsert_vendor_po_headers([po_payload], source="tests", source_detail="seed", synced_at=now)
    replace_vendor_po_lines(
        "PO-TEST-1",
        [
            {
                "item_sequence_number": "1",
                "asin": "B0TEST1234",
                "vendor_sku": "SKU-1",
                "barcode": "1112223334445",
                "title": "Test Line",
                "image": "",
                "ordered_qty": 20,
                "accepted_qty": 10,
                "received_qty": 2,
                "cancelled_qty": 0,
                "pending_qty": 8,
                "shortage_qty": 0,
                "net_cost_amount": 5.0,
                "net_cost_currency": "AED",
                "list_price_amount": 0.0,
                "list_price_currency": "AED",
                "last_updated_at": now,
                "raw": {},
                "ship_to_location": "DXB1",
            }
        ],
    )


def _seed_missing_accepted_po():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    po_payload = {
        "purchaseOrderNumber": "PO-MISSING",
        "purchaseOrderDate": "2025-12-05T00:00:00Z",
        "requestedQty": 12,
        "acceptedQty": 0,
        "receivedQty": 0,
        "cancelledQty": 0,
        "remainingQty": 5,
        "orderDetails": {"items": []},
    }
    upsert_vendor_po_headers([po_payload], source="tests", source_detail="seed", synced_at=now)
    replace_vendor_po_lines(
        "PO-MISSING",
        [
            {
                "item_sequence_number": "1",
                "asin": "B0MISSING",
                "vendor_sku": "SKU-MISS",
                "barcode": "",
                "title": "Missing Accepted Line",
                "image": "",
                "ordered_qty": 12,
                "accepted_qty": 0,
                "received_qty": 0,
                "cancelled_qty": 0,
                "pending_qty": 5,
                "shortage_qty": 0,
                "net_cost_amount": 4.0,
                "net_cost_currency": "AED",
                "list_price_amount": 0.0,
                "list_price_currency": "AED",
                "last_updated_at": now,
                "raw": {},
                "ship_to_location": "DXB1",
            }
        ],
    )
    with get_db_connection() as conn:
        conn.execute("UPDATE vendor_po_header SET accepted_qty = NULL WHERE po_number = 'PO-MISSING'")
        conn.execute("UPDATE vendor_po_header SET remaining_qty = NULL WHERE po_number = 'PO-MISSING'")
        conn.commit()


def _seed_mixed_currency_po():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    po_payload = {
        "purchaseOrderNumber": "PO-MIXED",
        "purchaseOrderDate": "2025-12-07T00:00:00Z",
        "requestedQty": 10,
        "acceptedQty": 10,
        "receivedQty": 0,
        "cancelledQty": 0,
        "remainingQty": 10,
        "orderDetails": {"items": []},
    }
    upsert_vendor_po_headers([po_payload], source="tests", source_detail="seed", synced_at=now)
    replace_vendor_po_lines(
        "PO-MIXED",
        [
            {
                "item_sequence_number": "1",
                "asin": "B0MIXED1",
                "vendor_sku": "SKU-MIX-1",
                "barcode": "",
                "title": "Line AED",
                "image": "",
                "ordered_qty": 5,
                "accepted_qty": 5,
                "received_qty": 0,
                "cancelled_qty": 0,
                "pending_qty": 5,
                "shortage_qty": 0,
                "net_cost_amount": 6.0,
                "net_cost_currency": "AED",
                "list_price_amount": 0.0,
                "list_price_currency": "AED",
                "last_updated_at": now,
                "raw": {},
                "ship_to_location": "DXB1",
            },
            {
                "item_sequence_number": "2",
                "asin": "B0MIXED2",
                "vendor_sku": "SKU-MIX-2",
                "barcode": "",
                "title": "Line USD",
                "image": "",
                "ordered_qty": 5,
                "accepted_qty": 5,
                "received_qty": 0,
                "cancelled_qty": 0,
                "pending_qty": 5,
                "shortage_qty": 0,
                "net_cost_amount": 2.0,
                "net_cost_currency": "USD",
                "list_price_amount": 0.0,
                "list_price_currency": "USD",
                "last_updated_at": now,
                "raw": {},
                "ship_to_location": "DXB1",
            },
        ],
    )


@pytest.fixture
def vendor_po_client(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    _seed_sample_po()
    monkeypatch.setenv("LWA_CLIENT_ID", "dummy")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "dummy")
    monkeypatch.setenv("LWA_REFRESH_TOKEN", "dummy")
    import main

    monkeypatch.setattr(main, "start_vendor_rt_sales_startup_backfill_thread", lambda: None)
    monkeypatch.setattr(main, "start_vendor_rt_sales_auto_sync", lambda: None)
    with TestClient(main.app) as client:
        yield client


def test_compute_po_status_variants():
    header = {"acceptedQty": 0, "receivedQty": 0, "cancelledQty": 0, "remainingQty": 0}
    status, reason = compute_po_status(header, {})
    assert status == "CANCELLED"
    assert reason == "accepted_zero"

    header_open = {"acceptedQty": 20, "receivedQty": 10, "cancelledQty": 0, "remainingQty": 10}
    status, reason = compute_po_status(header_open, {})
    assert status == "OPEN"
    assert reason == "remaining_positive"

    header_closed = {"acceptedQty": 15, "receivedQty": 15, "cancelledQty": 0, "remainingQty": 0}
    status, reason = compute_po_status(header_closed, {})
    assert status == "CLOSED"
    assert reason == "remaining_zero"


def test_amount_reconciliation_delta_rounds():
    result = compute_amount_reconciliation("12.345", "10.00")
    assert result["line_total"] == pytest.approx(12.35)
    assert result["accepted_total"] == pytest.approx(10.00)
    assert result["delta"] == pytest.approx(2.35)


def test_amount_reconciliation_handles_string_header_value(vendor_po_client):
    import main as main_module

    accepted_total_value = main_module._pick_money_amount(None, "75.5")
    recon = compute_amount_reconciliation(50.0, accepted_total_value)
    assert recon["delta"] == pytest.approx(-25.5)


def test_po_status_missing_accepted_infers_open():
    header = {"requestedQty": 20, "acceptedQty": None, "receivedQty": 0, "cancelledQty": 0}
    totals = {"pending_qty": 5}
    status, reason = compute_po_status(header, totals)
    assert status == "OPEN"
    assert reason == "remaining_positive"


def test_vendor_po_detail_and_ledger(vendor_po_client):
    detail_resp = vendor_po_client.get("/api/vendor-pos/PO-TEST-1")
    assert detail_resp.status_code == 200
    item = detail_resp.json()["item"]
    assert item["po_status"] == "OPEN"
    assert item["po_status_reason"] == "remaining_positive"
    recon = item["amount_reconciliation"]
    assert recon["line_total"] == pytest.approx(50.0)
    assert recon["delta"] == pytest.approx(0.0)
    assert recon["ok"] is True

    ledger_resp = vendor_po_client.get("/api/vendor-pos/PO-TEST-1/ledger")
    assert ledger_resp.status_code == 200
    payload = ledger_resp.json()
    assert payload["ok"] is True
    assert payload["po_number"] == "PO-TEST-1"
    assert payload["ledger_type"] == "snapshot_synth"
    assert len(payload["rows"]) >= 1


def test_missing_accepted_status_reason_field(vendor_po_client):
    _seed_missing_accepted_po()
    resp = vendor_po_client.get("/api/vendor-pos/PO-MISSING")
    assert resp.status_code == 200
    payload = resp.json()["item"]
    assert payload["po_status"] == "OPEN"
    assert payload["po_status_reason"] == "remaining_positive"


def test_amount_reconciliation_mixed_currency_warning(vendor_po_client):
    _seed_mixed_currency_po()
    resp = vendor_po_client.get("/api/vendor-pos/PO-MIXED")
    assert resp.status_code == 200
    recon = resp.json()["item"]["amount_reconciliation"]
    assert recon["ok"] is False
    assert recon["error"] == "mixed_currencies"
    assert recon["delta"] is None


def test_vendor_po_table_status_marks_new(vendor_po_client):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    upsert_vendor_po_headers(
        [
            {
                "purchaseOrderNumber": "PO-NEW-0",
                "purchaseOrderDate": "2025-12-20T00:00:00Z",
                "requestedQty": 10,
                "acceptedQty": 0,
                "receivedQty": 0,
                "cancelledQty": 0,
                "remainingQty": 0,
                "amazonStatus": "OPEN",
                "orderDetails": {"items": []},
            }
        ],
        source="tests",
        source_detail="table-status",
        synced_at=now,
    )

    resp = vendor_po_client.get("/api/vendor-pos", params={"createdAfter": "2025-01-01T00:00:00"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    po = next((it for it in items if it.get("purchaseOrderNumber") == "PO-NEW-0"), None)
    assert po is not None
    assert po["po_status"] == "NEW"
    assert po["po_status_reason"] == "all_zero_uncancelled"


def test_vendor_po_table_status_respects_explicit_cancel(vendor_po_client):
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    upsert_vendor_po_headers(
        [
            {
                "purchaseOrderNumber": "PO-CANCELLED-1",
                "purchaseOrderDate": "2025-12-19T00:00:00Z",
                "requestedQty": 5,
                "acceptedQty": 0,
                "receivedQty": 0,
                "cancelledQty": 0,
                "remainingQty": 0,
                "amazonStatus": "CANCELLED",
                "purchaseOrderState": "CANCELLED",
                "orderDetails": {"items": []},
            }
        ],
        source="tests",
        source_detail="table-status",
        synced_at=now,
    )

    resp = vendor_po_client.get("/api/vendor-pos", params={"createdAfter": "2025-01-01T00:00:00"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    po = next((it for it in items if it.get("purchaseOrderNumber") == "PO-CANCELLED-1"), None)
    assert po is not None
    assert po["po_status"] == "CANCELLED"
    assert po["po_status_reason"] == "amazon_status_cancelled"
