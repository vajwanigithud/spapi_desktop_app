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


def test_picklist_preview_returns_lines(monkeypatch):
    main = _setup_main(monkeypatch)

    def fake_get_pos(po_numbers):
        return [{"purchaseOrderNumber": po_numbers[0], "orderDetails": {}}]

    def fake_lines(po_number):
        return [
            {
                "asin": "ASIN1",
                "vendor_sku": "SKU1",
                "ordered_qty": 12,
                "accepted_qty": 8,
                "received_qty": 3,
                "pending_qty": 5,
                "title": "Sample Item",
                "image": "",
                "item_sequence_number": "001",
            }
        ]

    monkeypatch.setattr(main, "get_vendor_pos_by_numbers", fake_get_pos)
    monkeypatch.setattr(main, "store_get_vendor_po_lines", fake_lines)
    monkeypatch.setattr(main, "load_oos_state", lambda: {})
    monkeypatch.setattr(main, "save_oos_state", lambda payload: None)
    monkeypatch.setattr(main, "spapi_catalog_status", lambda: {})
    monkeypatch.setattr(main, "get_rejected_vendor_po_lines", lambda _: [])
    monkeypatch.setattr(main.oos_service, "upsert_oos_entry", lambda *args, **kwargs: None)

    with TestClient(main.app) as client:
        resp = client.post("/api/picklist/preview", json={"purchaseOrderNumbers": ["PO-123"]})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["po_count"] == 1
        assert payload["line_count"] == 1
        assert payload["summary"]["totalLines"] == 1
        assert payload["items"]
        assert payload["items"][0]["asin"] == "ASIN1"
