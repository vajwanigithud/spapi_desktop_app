from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from services.db import ensure_df_payments_tables
from services.df_payments import (
    _fetch_purchase_orders_from_api,
    compute_incremental_eligibility,
    get_df_payments_state,
    incremental_refresh_df_payments,
    refresh_df_payments,
)

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
    assert [row["month"] for row in invoices] == ["2024-12", "2025-01"]
    assert round(invoices[0]["total_incl_vat"], 2) == 0.0
    assert round(invoices[1]["total_incl_vat"], 2) == 36.75

    cashflow = state["dashboard"]["cashflow_projection"]
    assert [row["month"] for row in cashflow] == ["2025-01", "2025-02"]
    assert round(cashflow[0]["unpaid_amount"], 2) == 0.0
    assert round(cashflow[1]["unpaid_amount"], 2) == 36.75
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
    assert round(invoices[0]["total_incl_vat"], 2) == 210.0
    assert round(invoices[1]["total_incl_vat"], 2) == 315.0

    cashflow = state["dashboard"]["cashflow_projection"]
    assert [row["month"] for row in cashflow] == ["2025-02", "2025-03"]
    assert round(cashflow[0]["unpaid_amount"], 2) == 210.0
    assert round(cashflow[1]["unpaid_amount"], 2) == 315.0


def test_df_payments_lookback_respects_90_days(tmp_path):
    db_path = tmp_path / "df_payments_lookback.db"
    ensure_df_payments_tables(db_path)

    fixed_now = datetime(2025, 3, 1, 0, 0, tzinfo=timezone.utc)
    recent = fixed_now - timedelta(days=5)
    mid = fixed_now - timedelta(days=20)
    older = fixed_now - timedelta(days=60)

    payload = {
        "purchaseOrders": [
            _make_order("PO-RECENT", recent, 50),
            _make_order("PO-MID", mid, 75),
            _make_order("PO-OLDER", older, 125),
        ]
    }

    refresh_df_payments(
        MARKETPLACE_ID,
        lookback_days=90,
        db_path=db_path,
        fetcher=lambda **kwargs: payload,
        now_utc=fixed_now,
    )

    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path, now_utc=fixed_now)
    orders = state["orders"]

    assert len(orders) == 3
    order_dates = {o["purchase_order_number"]: o["order_date_utc"] for o in orders}
    assert order_dates["PO-OLDER"].startswith((older.replace(microsecond=0)).isoformat()[:10])
    assert state["state"]["rows_90d"] == 3


def test_df_payments_paginate_all_pages(monkeypatch):
    fixed_now = datetime(2025, 12, 31, 0, 0, tzinfo=timezone.utc)

    pages = [
        {
            "payload": {
                "purchaseOrders": [
                    {
                        "purchaseOrderNumber": "PO-1",
                        "orderDetails": {
                            "orderStatus": "OPEN",
                            "orderDate": fixed_now.isoformat(),
                            "items": [],
                        },
                    }
                ],
                "pagination": {"nextToken": "T2"},
            }
        },
        {
            "payload": {
                "purchaseOrders": [
                    {
                        "purchaseOrderNumber": "PO-1",
                        "orderDetails": {
                            "orderStatus": "OPEN",
                            "orderDate": fixed_now.isoformat(),
                            "items": [
                                {
                                    "vendorProductIdentifier": "SKU-1",
                                    "orderedQuantity": {"amount": 2},
                                    "netPrice": {"amount": "10", "currencyCode": "AED"},
                                }
                            ],
                        },
                    },
                    {
                        "purchaseOrderNumber": "PO-2",
                        "orderDetails": {
                            "orderStatus": "OPEN",
                            "orderDate": fixed_now.isoformat(),
                            "items": [
                                {
                                    "vendorProductIdentifier": "SKU-2",
                                    "orderedQuantity": {"amount": 1},
                                    "netPrice": {"amount": "5", "currencyCode": "AED"},
                                }
                            ],
                        },
                    },
                ],
                "nextToken": "T3",
            }
        },
        {
            "payload": {
                "purchaseOrders": [
                    {
                        "purchaseOrderNumber": "PO-3",
                        "orderDetails": {
                            "orderStatus": "OPEN",
                            "orderDate": fixed_now.isoformat(),
                            "items": [
                                {
                                    "vendorProductIdentifier": "SKU-3",
                                    "orderedQuantity": {"amount": 1},
                                    "netPrice": {"amount": "7", "currencyCode": "AED"},
                                }
                            ],
                        },
                    }
                ]
            }
        },
    ]

    call_index = {"i": 0}

    class DummyResp:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, params=None, headers=None, timeout=None):
        assert params
        assert params.get("includeDetails") == "true"
        assert "createdAfter" in params and "createdBefore" in params
        if call_index["i"] == 0:
            assert params.get("limit") == 50
            assert "nextToken" not in params
        else:
            assert params.get("limit") == 50
            assert "nextToken" in params
        if call_index["i"] >= len(pages):
            raise AssertionError("Requested more pages than provided")
        payload = pages[call_index["i"]]
        call_index["i"] += 1
        return DummyResp(payload)

    monkeypatch.setattr("services.df_payments.requests.get", fake_get)

    result = _fetch_purchase_orders_from_api(
        MARKETPLACE_ID,
        lookback_days=90,
        ship_from_party_id=None,
        limit=50,
        now_utc=fixed_now,
    )

    assert result["pages"] == 3
    assert len(result["orders"]) == 3
    po_map = {o["purchaseOrderNumber"]: o for o in result["orders"]}
    assert len(po_map["PO-1"]["orderDetails"].get("items", [])) == 1
    expected_after = (fixed_now - timedelta(days=90)).replace(microsecond=0).isoformat()
    assert result["created_after"].startswith(expected_after)


def test_df_payments_paginate_and_persist(monkeypatch, tmp_path):
    db_path = tmp_path / "df_payments_paginate.db"
    ensure_df_payments_tables(db_path)

    fixed_now = datetime(2025, 12, 31, 0, 0, tzinfo=timezone.utc)

    responses = [
        {
            "purchaseOrders": [
                _make_order("PO-A", fixed_now, 10),
                _make_order("PO-B", fixed_now, 20),
            ],
            "pagination": {"nextToken": "NX2"},
        },
        {
            "payload": {
                "purchaseOrders": [
                    _make_order("PO-C", fixed_now, 30),
                ],
                "nextToken": "NX3",
            }
        },
        {
            "purchaseOrders": [
                _make_order("PO-D", fixed_now, 40),
            ],
        },
    ]

    idx = {"i": 0}

    class DummyResp2:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, params=None, headers=None, timeout=None):
        assert params
        assert params.get("includeDetails") == "true"
        assert "createdAfter" in params and "createdBefore" in params
        if idx["i"] == 0:
            assert params.get("limit") == 2
            assert "nextToken" not in params
        else:
            assert params.get("nextToken")
            assert params.get("limit") == 2
        if idx["i"] >= len(responses):
            raise AssertionError("Unexpected extra page")
        payload = responses[idx["i"]]
        idx["i"] += 1
        return DummyResp2(payload)

    monkeypatch.setattr("services.df_payments.requests.get", fake_get)

    result = refresh_df_payments(
        MARKETPLACE_ID,
        lookback_days=90,
        limit=2,
        db_path=db_path,
        fetcher=_fetch_purchase_orders_from_api,
        now_utc=fixed_now,
    )

    assert result["pages_fetched"] == 3
    assert result["rows_90d"] == 4
    assert result["fetched_orders_total"] == 4
    assert result["unique_po_total"] == 4
    assert result["rows_in_db_window"] == 4
    assert result["limit_used"] == 2

    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path, now_utc=fixed_now)
    assert state["state"]["pages_fetched"] == 3
    assert state["state"]["rows_90d"] == 4
    assert state["state"]["diagnostics"]["rows_in_db_window"] == 4


def test_df_payments_incremental_uses_buffer(monkeypatch, tmp_path):
    db_path = tmp_path / "df_payments_incremental.db"
    ensure_df_payments_tables(db_path)

    fixed_now = datetime(2025, 3, 10, 12, 0, tzinfo=timezone.utc)
    first_order_date = fixed_now - timedelta(days=1)

    initial_payload = {"purchaseOrders": [_make_order("PO-INIT", first_order_date, 50)]}
    refresh_df_payments(
        MARKETPLACE_ID,
        lookback_days=10,
        db_path=db_path,
        fetcher=lambda **kwargs: initial_payload,
        now_utc=fixed_now,
    )

    captured = {}

    def fake_incremental_fetch(**kwargs):
        captured["created_after_override"] = kwargs.get("created_after_override")
        captured["created_before_override"] = kwargs.get("created_before_override")
        return {
            "purchaseOrders": [
                _make_order("PO-NEW", fixed_now - timedelta(hours=1), 75),
            ],
            "pagination": {"nextToken": None},
        }

    result = incremental_refresh_df_payments(
        MARKETPLACE_ID,
        db_path=db_path,
        fetcher=fake_incremental_fetch,
        now_utc=fixed_now,
    )

    assert result["status"] == "incremental_refreshed"
    assert result["orders_upserted"] == 1
    assert captured["created_after_override"]
    assert captured["created_before_override"] == fixed_now
    # created_after should be within 2 hours before last_seen (buffer) and not earlier than 7d fallback
    buffer_expected = first_order_date - timedelta(hours=2)
    assert captured["created_after_override"] >= buffer_expected

    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path, now_utc=fixed_now)
    assert state["state"]["rows_90d"] == 2
    assert state["state"]["last_seen_order_date_utc"]
    po_numbers = {o["purchase_order_number"] for o in state["orders"]}
    assert po_numbers == {"PO-INIT", "PO-NEW"}


def test_df_payments_month_totals_across_boundary(tmp_path):
    db_path = tmp_path / "df_payments_month_totals.db"
    ensure_df_payments_tables(db_path)

    fixed_now = datetime(2025, 12, 15, tzinfo=timezone.utc)
    nov_order = datetime(2025, 11, 20, tzinfo=timezone.utc)
    dec_order = datetime(2025, 12, 5, tzinfo=timezone.utc)

    payload = {
        "purchaseOrders": [
            _make_order("PO-NOV", nov_order, 100),
            _make_order("PO-DEC", dec_order, 200),
        ]
    }

    refresh_df_payments(
        MARKETPLACE_ID,
        lookback_days=90,
        db_path=db_path,
        fetcher=lambda **kwargs: payload,
        now_utc=fixed_now,
    )

    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path, now_utc=fixed_now)
    invoices = state["dashboard"]["invoices_by_month"]

    assert [row["month"] for row in invoices] == ["2025-11", "2025-12"]
    assert round(invoices[0]["total_incl_vat"], 2) == 105.0
    assert round(invoices[1]["total_incl_vat"], 2) == 210.0
    assert state["state"]["rows_90d"] == 2
    cashflow = state["dashboard"]["cashflow_projection"]
    assert [row["month"] for row in cashflow] == ["2025-12", "2026-01"]
    assert round(cashflow[0]["unpaid_amount"], 2) == 105.0
    assert round(cashflow[1]["unpaid_amount"], 2) == 210.0


def test_manual_success_resets_auto_timer(tmp_path):
    db_path = tmp_path / "df_payments_auto_timer.db"
    ensure_df_payments_tables(db_path)

    base_now = datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc)
    refresh_df_payments(
        MARKETPLACE_ID,
        lookback_days=5,
        db_path=db_path,
        fetcher=lambda **kwargs: {"purchaseOrders": []},
        now_utc=base_now,
    )

    manual_now = base_now + timedelta(minutes=2)
    incremental_refresh_df_payments(
        MARKETPLACE_ID,
        db_path=db_path,
        fetcher=lambda **kwargs: {"purchaseOrders": []},
        now_utc=manual_now,
        triggered_by="manual",
        force=True,
    )

    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path, now_utc=manual_now)
    last_success_iso = state["state"].get("incremental_last_success_at_utc")
    assert last_success_iso and last_success_iso.startswith(manual_now.replace(microsecond=0).isoformat())
    next_auto_iso = state["state"].get("incremental_next_eligible_at_utc")
    assert next_auto_iso
    next_auto = datetime.fromisoformat(next_auto_iso)
    assert next_auto == manual_now.replace(microsecond=0) + timedelta(minutes=10)


def test_auto_waits_for_baseline(tmp_path):
    db_path = tmp_path / "df_payments_auto_baseline.db"
    ensure_df_payments_tables(db_path)

    fixed_now = datetime(2025, 6, 1, tzinfo=timezone.utc)
    state = get_df_payments_state(MARKETPLACE_ID, db_path=db_path, now_utc=fixed_now)
    eligibility = compute_incremental_eligibility(state["state"], fixed_now)
    assert eligibility["eligible"] is False
    assert "Fetch Orders" in (eligibility.get("reason") or "")


def test_incremental_lock_prevents_overlap(tmp_path):
    db_path = tmp_path / "df_payments_lock.db"
    ensure_df_payments_tables(db_path)

    base_now = datetime(2025, 7, 1, 9, 0, tzinfo=timezone.utc)
    refresh_df_payments(
        MARKETPLACE_ID,
        lookback_days=3,
        db_path=db_path,
        fetcher=lambda **kwargs: {"purchaseOrders": []},
        now_utc=base_now,
    )

    start_evt = threading.Event()
    release_evt = threading.Event()

    def slow_fetch(**kwargs):
        start_evt.set()
        release_evt.wait(timeout=2)
        return {"purchaseOrders": [_make_order("PO-SLOW", base_now, 10)]}

    thread = threading.Thread(
        target=incremental_refresh_df_payments,
        kwargs={
            "marketplace_id": MARKETPLACE_ID,
            "db_path": db_path,
            "fetcher": slow_fetch,
            "now_utc": base_now + timedelta(minutes=1),
            "force": True,
            "triggered_by": "manual",
        },
        daemon=True,
    )
    thread.start()
    start_evt.wait(timeout=1)

    blocked = incremental_refresh_df_payments(
        MARKETPLACE_ID,
        db_path=db_path,
        fetcher=lambda **kwargs: {"purchaseOrders": []},
        now_utc=base_now + timedelta(minutes=2),
        force=True,
        triggered_by="manual",
    )
    assert blocked["status"] == "locked"

    release_evt.set()
    thread.join(timeout=2)
