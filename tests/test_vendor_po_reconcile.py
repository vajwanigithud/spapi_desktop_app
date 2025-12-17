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


def test_reconcile_endpoint_uses_pending_fallback(monkeypatch):
    main = _setup_main(monkeypatch)

    header_payload = {
        "purchaseOrderNumber": "2FLBUJAO",
        "poItemsCount": 0,
        "requestedQty": 74,
        "acceptedQty": 51,
        "receivedQty": 0,
        "remainingQty": 0,
        "cancelledQty": 10,
        "total_accepted_cost": 0,
    }

    def fake_get_po(po_number):
        return dict(header_payload)

    def fake_get_lines(po_number):
        lines = []
        for idx in range(39):
            entry = {
                "asin": f"ASIN{idx}",
                "vendor_sku": f"SKU{idx}",
                "ordered_qty": 0,
                "accepted_qty": 0,
                "received_qty": 0,
                "cancelled_qty": 0,
                "pending_qty": 0,
                "shortage_qty": 0,
            }
            if idx == 0:
                entry["ordered_qty"] = 74
                entry["accepted_qty"] = 51
            if idx == 1:
                entry["ordered_qty"] = 10
                entry["accepted_qty"] = 0
            lines.append(entry)
        return lines

    monkeypatch.setattr(main, "store_get_vendor_po", fake_get_po)
    monkeypatch.setattr(main, "store_get_vendor_po_lines", fake_get_lines)

    with TestClient(main.app) as client:
        resp = client.get("/api/vendor-pos/reconcile/2FLBUJAO")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["vc_target_hint"]["remaining_units"] == 51
        assert payload["vc_target_hint"]["cancelled_units"] == 0
        assert payload["header"]["po_items_count"] == 39
        assert payload["header"]["requested_units"] == 74
        zero_line = next(line for line in payload["lines"] if line["accepted"] == 0)
        assert zero_line["ordered"] == 10
