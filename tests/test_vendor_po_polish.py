from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from services import db as db_service
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
    assert compute_po_status(header, {}) == "CANCELLED"

    header_open = {"acceptedQty": 20, "receivedQty": 10, "cancelledQty": 0, "remainingQty": 10}
    assert compute_po_status(header_open, {}) == "OPEN"

    header_closed = {"acceptedQty": 15, "receivedQty": 15, "cancelledQty": 0, "remainingQty": 0}
    assert compute_po_status(header_closed, {}) == "CLOSED"


def test_amount_reconciliation_delta_rounds():
    result = compute_amount_reconciliation("12.345", "10.00")
    assert result["line_total"] == pytest.approx(12.35)
    assert result["accepted_total"] == pytest.approx(10.00)
    assert result["delta"] == pytest.approx(2.35)


def test_vendor_po_detail_and_ledger(vendor_po_client):
    detail_resp = vendor_po_client.get("/api/vendor-pos/PO-TEST-1")
    assert detail_resp.status_code == 200
    item = detail_resp.json()["item"]
    assert item["po_status"] == "OPEN"
    recon = item["amount_reconciliation"]
    assert recon["line_total"] == pytest.approx(50.0)
    assert recon["delta"] == pytest.approx(0.0)

    ledger_resp = vendor_po_client.get("/api/vendor-pos/PO-TEST-1/ledger")
    assert ledger_resp.status_code == 200
    payload = ledger_resp.json()
    assert payload["ok"] is True
    assert payload["po_number"] == "PO-TEST-1"
    assert len(payload["rows"]) >= 1
