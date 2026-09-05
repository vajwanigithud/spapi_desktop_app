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
        assert asin20["normalized_status"] == "PARTIALLY_ACCEPTED"
        assert asin20["net_amount"] == 5.0
        assert asin20["unit_net_amount"] == 5.0
        assert asin20["net_currency"] == "AED"
        assert asin20["total_amount"] == 10.0
        assert asin20["accepted_net_total"] == 10.0
        assert asin10["net_amount"] == 10.0
        assert asin10["total_amount"] == 30.0
        amounts = payload["amounts"]
        assert amounts["sum_total_amount"] == 40.0
        assert amounts["po_total_accepted_cost"] == 40.0
        assert amounts["diff"] == 0.0
        assert amounts["currency"] == "AED"


def test_po_details_preserves_explicit_zero_acceptance(monkeypatch):
    main = _setup_main(monkeypatch)

    monkeypatch.setattr(main, "store_get_vendor_po", lambda _: {
        "purchaseOrderNumber": "6QCXKJ5Q",
        "orderDetails": {},
        "requestedQty": 52,
        "acceptedQty": 0,
        "receivedQty": 0,
        "remainingQty": 0,
        "cancelledQty": 52,
    })
    monkeypatch.setattr(main, "store_get_vendor_po_lines", lambda _: [{
        "asin": "B0CX89MYGP",
        "vendor_sku": "SKU-REJECTED",
        "ordered_qty": 52,
        "accepted_qty": 0,
        "received_qty": 0,
        "cancelled_qty": 52,
        "pending_qty": 0,
        "net_cost_amount": "3.99",
        "net_cost_currency": "AED",
    }])
    monkeypatch.setattr(main, "bootstrap_headers_from_cache", lambda: None)
    monkeypatch.setattr(main, "load_po_tracker", lambda: {})
    monkeypatch.setattr(main, "get_po_notification_flags", lambda _: {})
    monkeypatch.setattr(main, "_sync_vendor_po_lines_for_po", lambda _: None)

    with TestClient(main.app) as client:
        resp = client.get("/api/vendor-pos/6QCXKJ5Q")

    assert resp.status_code == 200
    line = resp.json()["item"]["orderDetails"]["items"][0]
    assert line["accepted_qty"] == 0
    assert line["rejected_qty"] == 52
    assert line["normalized_status"] in {"REJECTED", "CANCELLED"}
    assert line["normalized_status"] != "ACCEPTED"
    assert line["unit_net_amount"] == 3.99
    assert line["net_currency"] == "AED"
    assert line["accepted_net_total"] == 0.0


def test_modal_line_status_and_money_variants(monkeypatch):
    main = _setup_main(monkeypatch)
    rows = main._aggregate_po_items_for_modal([
        {
            "amazonProductIdentifier": "FULL",
            "ordered_qty": 4,
            "accepted_qty": 4,
            "received_qty": 0,
            "cancelled_qty": 0,
            "net_cost_amount": "2.50",
            "net_cost_currency": "AED",
        },
        {
            "amazonProductIdentifier": "PARTIAL",
            "ordered_qty": 10,
            "accepted_qty": 6,
            "received_qty": 0,
            "cancelled_qty": 4,
            "net_cost_amount": "3.00",
            "net_cost_currency": "AED",
        },
        {
            "amazonProductIdentifier": "MISSING-COST",
            "ordered_qty": 2,
            "accepted_qty": 2,
            "received_qty": 0,
            "cancelled_qty": 0,
        },
        {
            "amazonProductIdentifier": "PENDING",
            "ordered_qty": 3,
        },
    ])
    by_asin = {row["amazonProductIdentifier"]: row for row in rows}

    assert by_asin["FULL"]["normalized_status"] == "ACCEPTED"
    assert by_asin["FULL"]["accepted_net_total"] == 10.0
    assert by_asin["PARTIAL"]["normalized_status"] == "PARTIALLY_ACCEPTED"
    assert by_asin["PARTIAL"]["accepted_net_total"] == 18.0
    assert by_asin["MISSING-COST"]["unit_net_amount"] is None
    assert by_asin["MISSING-COST"]["accepted_net_total"] is None
    assert by_asin["PENDING"]["normalized_status"] == "PENDING"
