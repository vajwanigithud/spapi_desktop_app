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


def _make_order(po_number: str, order_date: datetime, amount: float) -> dict:
    return {
        "purchaseOrderNumber": po_number,
        "orderDetails": {
            "orderStatus": "OPEN",
            "orderDate": order_date.isoformat(),
            "items": [
                {
                    "vendorProductIdentifier": f"SKU-{po_number}",
                    "orderedQuantity": {"amount": 1},
                    "netPrice": {"amount": str(amount), "currencyCode": "AED"},
                }
            ],
        },
    }


def _multi_month_payload(now_utc: datetime) -> dict:
    dec = datetime(2024, 12, 20, tzinfo=timezone.utc)
    jan = datetime(2025, 1, 10, tzinfo=timezone.utc)
    feb = datetime(2025, 2, 5, tzinfo=timezone.utc)
    mar = datetime(2025, 3, 1, tzinfo=timezone.utc)
    return {
        "purchaseOrders": [
            _make_order("PO-DEC", dec, 100),
            _make_order("PO-JAN", jan, 200),
            _make_order("PO-FEB", feb, 300),
            _make_order("PO-MAR", mar, 400),
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

    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path, now_utc=fixed_now)
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


def test_df_payments_dashboard_windows(tmp_path):
    db_path = tmp_path / "df_payments_windows.db"
    ensure_df_payments_tables(db_path)

    fixed_now = datetime(2025, 2, 15, 12, 0, tzinfo=timezone.utc)
    payload = _multi_month_payload(fixed_now)

    refresh_df_payments(
        MARKETPLACE_ID,
        lookback_days=120,
        db_path=db_path,
        fetcher=lambda **kwargs: payload,
        now_utc=fixed_now,
    )

    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path, now_utc=fixed_now)

    invoices = state["dashboard"]["invoices_by_month"]
    assert [row["month"] for row in invoices] == ["2025-01", "2025-02"]

    cashflow = state["dashboard"]["cashflow_projection"]
    assert [row["month"] for row in cashflow] == ["2025-02", "2025-03"]
    for row in cashflow:
        assert row["unpaid_amount"] == 0.0
