from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.db import ensure_df_payments_tables
from services.df_payments import get_df_payments_state, refresh_df_payments

MARKETPLACE_ID = "A2TEST-DFP"


def _sample_payload(now_utc: datetime) -> dict:
    recent = (now_utc - timedelta(days=2)).isoformat()
    old = (now_utc - timedelta(days=120)).isoformat()
    return {
        "purchaseOrders": [
            {
                "purchaseOrderNumber": "PO-NEW",
                "customerOrderNumber": "CO-1",
                "orderDetails": {
                    "orderStatus": "OPEN",
                    "orderDate": recent,
                    "items": [
                        {
                            "vendorProductIdentifier": "SKU-1",
                            "orderedQuantity": {"amount": 2},
                            "netPrice": {"amount": "10", "currencyCode": "AED"},
                        },
                        {
                            "vendorProductIdentifier": "SKU-1",
                            "orderedQuantity": {"amount": 1},
                            "netPrice": {"amount": "10", "currencyCode": "AED"},
                        },
                        {
                            "vendorProductIdentifier": "SKU-2",
                            "orderedQuantity": {"amount": 1},
                            "listPrice": {"amount": "5", "currencyCode": "USD"},
                        },
                    ],
                },
            },
            {
                "purchaseOrderNumber": "PO-CANCEL",
                "orderDetails": {
                    "orderStatus": "CANCELLED",
                    "orderDate": recent,
                    "items": [],
                },
            },
            {
                "purchaseOrderNumber": "PO-OLD",
                "orderDetails": {
                    "orderStatus": "OPEN",
                    "orderDate": old,
                    "items": [
                        {
                            "vendorProductIdentifier": "SKU-OLD",
                            "orderedQuantity": {"amount": 1},
                            "netPrice": {"amount": "1", "currencyCode": "AED"},
                        }
                    ],
                },
            },
        ]
    }


def test_df_payments_materialize_and_prune(tmp_path):
    db_path = tmp_path / "df_payments.db"
    ensure_df_payments_tables(db_path)

    fixed_now = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
    payload = _sample_payload(fixed_now)

    refresh_df_payments(
        MARKETPLACE_ID,
        lookback_days=120,
        db_path=db_path,
        fetcher=lambda **kwargs: payload,
        now_utc=fixed_now,
    )

    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path)
    orders = state["orders"]

    assert len(orders) == 1
    order = orders[0]
    assert order["purchase_order_number"] == "PO-NEW"
    assert order["items_count"] == 3
    assert order["total_units"] == 4
    assert round(order["subtotal_amount"], 2) == 35.0
    assert round(order["vat_amount"], 2) == 1.75
    assert order["currency_code"] == "AED"
    assert order["sku_list"] == "SKU-1, SKU-2"

    invoices = state["dashboard"]["invoices_by_month"]
    assert invoices
    assert invoices[0]["month"] == fixed_now.strftime("%Y-%m")
    assert round(invoices[0]["total_incl_vat"], 2) == 36.75

    cashflow = state["dashboard"]["cashflow_projection"]
    assert cashflow
    assert cashflow[0]["unpaid_amount"] == 0.0
    assert "reconciliation" in (cashflow[0]["note"] or "").lower()

    assert state["state"]["rows_90d"] == 1
