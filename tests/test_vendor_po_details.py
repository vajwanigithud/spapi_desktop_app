from __future__ import annotations

from fastapi.testclient import TestClient


def _setup_main(monkeypatch):
    monkeypatch.setenv("LWA_CLIENT_ID", "dummy")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "dummy")
    monkeypatch.setenv("LWA_REFRESH_TOKEN", "dummy")
    import main  # type: ignore

    monkeypatch.setattr(main, "start_vendor_rt_sales_startup_backfill_thread", lambda: None)
    monkeypatch.setattr(main, "start_vendor_rt_sales_auto_sync", lambda: None)
    return main


def test_po_details_hydrates_lines_from_db(monkeypatch):
    main = _setup_main(monkeypatch)

    base_po = {
        "purchaseOrderNumber": "PO-ABC",
        "orderDetails": {},
        "requestedQty": 0,
        "acceptedQty": 0,
        "receivedQty": 0,
        "remainingQty": 0,
        "cancelledQty": 0,
    }

    def fake_get_po(po_number):
        return dict(base_po)

    def fake_get_lines(po_number):
        return [
            {
                "asin": "ASIN1",
                "vendor_sku": "SKU1",
                "ordered_qty": 10,
                "accepted_qty": 5,
                "received_qty": 2,
                "cancelled_qty": 0,
                "pending_qty": 3,
                "shortage_qty": 0,
                "title": "Line One",
            },
            {
                "asin": "ASIN2",
                "vendor_sku": "SKU2",
                "ordered_qty": 20,
                "accepted_qty": None,
                "received_qty": 0,
                "cancelled_qty": 0,
                "pending_qty": None,
                "shortage_qty": 0,
            },
            {
                "asin": "ASIN2",
                "vendor_sku": "SKU2",
                "ordered_qty": 5,
                "accepted_qty": 5,
                "received_qty": 1,
                "cancelled_qty": 0,
                "pending_qty": None,
                "shortage_qty": 0,
            },
        ]

    monkeypatch.setattr(main, "store_get_vendor_po", fake_get_po)
    monkeypatch.setattr(main, "store_get_vendor_po_lines", fake_get_lines)
    monkeypatch.setattr(main, "bootstrap_headers_from_cache", lambda: None)
    monkeypatch.setattr(main, "load_po_tracker", lambda: {})
    monkeypatch.setattr(main, "get_po_notification_flags", lambda _: {})
    monkeypatch.setattr(main, "_sync_vendor_po_lines_for_po", lambda _: (_ for _ in ()).throw(RuntimeError("should not sync")))

    with TestClient(main.app) as client:
        resp = client.get("/api/vendor-pos/PO-ABC")
        assert resp.status_code == 200
        payload = resp.json()
        items = payload["item"]["orderDetails"]["items"]
        assert payload["item"]["poItemsCount"] == 2
        assert len(items) == 2
        asin1 = next(line for line in items if line["amazonProductIdentifier"] == "ASIN1")
        asin2 = next(line for line in items if line["amazonProductIdentifier"] == "ASIN2")
        assert asin1["orderedQuantity"]["amount"] == 10
        assert asin2["orderedQuantity"]["amount"] == 25
        assert asin2["acknowledgementStatus"]["acceptedQuantity"]["amount"] == 25
        assert asin2["receivingStatus"]["receivedQuantity"]["amount"] == 1


def test_po_details_amounts_and_rejected(monkeypatch):
    main = _setup_main(monkeypatch)

    base_po = {
        "purchaseOrderNumber": "PO-XYZ",
        "orderDetails": {},
        "requestedQty": 0,
        "acceptedQty": 0,
        "receivedQty": 0,
        "remainingQty": 0,
        "cancelledQty": 0,
        "total_accepted_cost": 40,
        "total_accepted_cost_currency": "AED",
        "totalAcceptedCostAmount": "40",
        "totalAcceptedCostCurrency": "AED",
    }

    def fake_get_po(po_number):
        return dict(base_po)

    def fake_get_lines(po_number):
        return [
            {
                "asin": "ASIN10",
                "vendor_sku": "SKU10",
                "ordered_qty": 3,
                "accepted_qty": 3,
                "received_qty": 1,
                "cancelled_qty": 0,
                "pending_qty": None,
                "net_cost_amount": "10",
                "net_cost_currency": "AED",
            },
            {
                "asin": "ASIN20",
                "vendor_sku": "SKU20",
                "ordered_qty": 2,
                "accepted_qty": 2,
                "received_qty": 0,
                "cancelled_qty": 0,
                "pending_qty": None,
                "net_cost_amount": "5",
                "net_cost_currency": "AED",
            },
            {
                "asin": "ASIN20",
                "vendor_sku": "SKU20",
                "ordered_qty": 1,
                "accepted_qty": 0,
                "received_qty": 0,
                "cancelled_qty": 1,
                "pending_qty": None,
                "net_cost_amount": "5",
                "net_cost_currency": "AED",
            },
        ]

    monkeypatch.setattr(main, "store_get_vendor_po", fake_get_po)
    monkeypatch.setattr(main, "store_get_vendor_po_lines", fake_get_lines)
    monkeypatch.setattr(main, "bootstrap_headers_from_cache", lambda: None)
    monkeypatch.setattr(main, "load_po_tracker", lambda: {})
    monkeypatch.setattr(main, "get_po_notification_flags", lambda _: {})
    monkeypatch.setattr(main, "_sync_vendor_po_lines_for_po", lambda _: (_ for _ in ()).throw(RuntimeError("should not sync")))

    with TestClient(main.app) as client:
        resp = client.get("/api/vendor-pos/PO-XYZ?enrich=1")
        assert resp.status_code == 200
        payload = resp.json()
        items = payload["item"]["orderDetails"]["items"]
        assert len(items) == 2
        asin20 = next(line for line in items if line["amazonProductIdentifier"] == "ASIN20")
        asin10 = next(line for line in items if line["amazonProductIdentifier"] == "ASIN10")
        assert asin20["rejected_qty"] == 1
        assert asin20["accepted_qty"] == 2
        assert asin20["net_amount"] == 5.0
        assert asin20["total_amount"] == 10.0
        assert asin10["net_amount"] == 10.0
        assert asin10["total_amount"] == 30.0
        amounts = payload["amounts"]
        assert amounts["sum_total_amount"] == 40.0
        assert amounts["po_total_accepted_cost"] == 40.0
        assert amounts["diff"] == 0.0
        assert amounts["currency"] == "AED"
