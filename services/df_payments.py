from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from auth.spapi_auth import SpApiAuth
from config import MARKETPLACE_ID
from services.catalog_service import DEFAULT_CATALOG_DB_PATH
from services.db import ensure_df_payments_tables, get_db_connection_for_path

LOGGER = logging.getLogger(__name__)
DEFAULT_MARKETPLACE_ID = MARKETPLACE_ID
SPAPI_HOST = os.getenv("SPAPI_HOST", "https://sellingpartnerapi-eu.amazon.com")
DF_ORDERS_PATH = "/vendor/directFulfillment/orders/2021-12-28/purchaseOrders"
MAX_LOOKBACK_DAYS = 90
MIN_LOOKBACK_DAYS = 1
DEFAULT_LOOKBACK_DAYS = 90
MAX_PAGES = 50
DEFAULT_LIMIT = 50
VAT_RATE = Decimal("0.05")

FetchFunc = Callable[..., Dict[str, Any]]


@contextmanager
def _connection(db_path: Path):
    """Provide a SQLite connection, reusing the hardened default path when applicable."""
    with get_db_connection_for_path(db_path) as conn:
        yield conn


def _clamp_lookback(lookback_days: Optional[int]) -> int:
    try:
        value = int(lookback_days or DEFAULT_LOOKBACK_DAYS)
    except Exception:
        value = DEFAULT_LOOKBACK_DAYS
    return max(MIN_LOOKBACK_DAYS, min(MAX_LOOKBACK_DAYS, value))


def _normalize_iso(value: Any, *, default: Optional[datetime] = None) -> Optional[str]:
    if value is None:
        if default is None:
            return None
        return default.replace(microsecond=0, tzinfo=timezone.utc).isoformat()
    candidate = str(value).strip()
    if not candidate:
        return default.replace(microsecond=0, tzinfo=timezone.utc).isoformat() if default else None
    if candidate.endswith("Z"):
        candidate = candidate.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except Exception:
        return default.replace(microsecond=0, tzinfo=timezone.utc).isoformat() if default else None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat()


def _coerce_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _coerce_int(value: Any) -> int:
    try:
        return int(Decimal(str(value)))
    except Exception:
        return 0


def _extract_purchase_orders(payload: Any) -> List[dict]:
    if isinstance(payload, dict):
        for key in ("purchaseOrders", "orders"):
            block = payload.get(key)
            if isinstance(block, list):
                return block
        inner = payload.get("payload")
        if isinstance(inner, dict):
            for key in ("purchaseOrders", "orders"):
                block = inner.get(key)
                if isinstance(block, list):
                    return block
    return []


def _extract_next_token(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("nextToken", "next_token"):
        token = payload.get(key)
        if token:
            return str(token)
    inner = payload.get("payload")
    if isinstance(inner, dict):
        for key in ("nextToken", "next_token"):
            token = inner.get(key)
            if token:
                return str(token)
    return None


def _build_order_summary(order: Dict[str, Any], *, now_utc: datetime) -> Optional[Dict[str, Any]]:
    details = order.get("orderDetails") or {}
    order_status = (details.get("orderStatus") or order.get("orderStatus") or "").strip().upper()
    if order_status == "CANCELLED":
        return None

    po_number = (order.get("purchaseOrderNumber") or order.get("poNumber") or "").strip()
    if not po_number:
        return None

    items = details.get("items") or details.get("orderItems") or []
    subtotal = Decimal("0")
    items_count = len(items)
    total_units = 0
    currency_code = None
    seen_skus: list[str] = []

    for item in items:
        qty = _coerce_int((item.get("orderedQuantity") or {}).get("amount"))
        price_block = item.get("netPrice") or {}
        list_price_block = item.get("listPrice") or {}
        price_amount = _coerce_decimal(price_block.get("amount") or list_price_block.get("amount"))
        if not currency_code:
            currency_code = (
                price_block.get("currencyCode")
                or list_price_block.get("currencyCode")
                or "AED"
            )
        subtotal += price_amount * qty
        total_units += qty

        sku = item.get("vendorProductIdentifier") or item.get("sku") or ""
        sku_normalized = str(sku).strip()
        if sku_normalized and sku_normalized not in seen_skus:
            seen_skus.append(sku_normalized)

    vat_amount = (subtotal * VAT_RATE)
    order_date_utc = _normalize_iso(
        details.get("orderDate") or order.get("orderDate"), default=now_utc
    )

    return {
        "purchase_order_number": po_number,
        "customer_order_number": order.get("customerOrderNumber") or "",
        "order_date_utc": order_date_utc,
        "order_status": order_status or "UNKNOWN",
        "items_count": items_count,
        "total_units": total_units,
        "subtotal_amount": float(subtotal),
        "vat_amount": float(vat_amount),
        "currency_code": currency_code or "AED",
        "sku_list": ", ".join(seen_skus),
        "last_updated_utc": now_utc.replace(microsecond=0).isoformat(),
    }


def _upsert_orders(
    marketplace_id: str,
    summaries: List[Dict[str, Any]],
    *,
    db_path: Path,
) -> int:
    if not summaries:
        return 0

    with _connection(db_path) as conn:
        placeholders = (
            "INSERT INTO df_payments_orders (marketplace_id, purchase_order_number, customer_order_number, order_date_utc, order_status, items_count, total_units, subtotal_amount, vat_amount, currency_code, sku_list, last_updated_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(marketplace_id, purchase_order_number) DO UPDATE SET "
            "customer_order_number=excluded.customer_order_number, "
            "order_date_utc=excluded.order_date_utc, "
            "order_status=excluded.order_status, "
            "items_count=excluded.items_count, "
            "total_units=excluded.total_units, "
            "subtotal_amount=excluded.subtotal_amount, "
            "vat_amount=excluded.vat_amount, "
            "currency_code=excluded.currency_code, "
            "sku_list=excluded.sku_list, "
            "last_updated_utc=excluded.last_updated_utc"
        )
        params = [
            (
                marketplace_id,
                row["purchase_order_number"],
                row.get("customer_order_number"),
                row.get("order_date_utc"),
                row.get("order_status"),
                row.get("items_count"),
                row.get("total_units"),
                row.get("subtotal_amount"),
                row.get("vat_amount"),
                row.get("currency_code"),
                row.get("sku_list"),
                row.get("last_updated_utc"),
            )
            for row in summaries
        ]
        conn.executemany(placeholders, params)
        conn.commit()
        return len(params)


def _prune_old_orders(marketplace_id: str, *, now_utc: datetime, db_path: Path) -> int:
    cutoff_iso = (now_utc - timedelta(days=MAX_LOOKBACK_DAYS)).replace(microsecond=0).isoformat()
    with _connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM df_payments_orders WHERE marketplace_id = ? AND order_date_utc < ?",
            (marketplace_id, cutoff_iso),
        )
        conn.commit()
        pruned = cur.rowcount or 0
    LOGGER.info("[DF Payments] Pruned %s rows before %s", pruned, cutoff_iso)
    return pruned


def _count_orders(marketplace_id: str, *, db_path: Path) -> int:
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM df_payments_orders WHERE marketplace_id = ?",
            (marketplace_id,),
        ).fetchone()
        return int(row["c"] if hasattr(row, "keys") else row[0])


def _load_orders(marketplace_id: str, *, db_path: Path) -> List[Dict[str, Any]]:
    with _connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM df_payments_orders WHERE marketplace_id = ? ORDER BY order_date_utc DESC",
            (marketplace_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _add_months(dt: datetime, months: int) -> datetime:
    year = dt.year + ((dt.month - 1 + months) // 12)
    month = ((dt.month - 1 + months) % 12) + 1
    return dt.replace(year=year, month=month, day=1)


def _aggregate_invoice_totals(marketplace_id: str, *, db_path: Path) -> Dict[str, float]:
    with _connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT substr(order_date_utc, 1, 7) AS month, SUM(subtotal_amount + vat_amount) AS total
            FROM df_payments_orders
            WHERE marketplace_id = ?
            GROUP BY substr(order_date_utc, 1, 7)
            """,
            (marketplace_id,),
        ).fetchall()
    return {
        row["month"]: float(row["total"] or 0)
        for row in rows
        if row["month"]
    }


def _slice_invoices_window(month_totals: Dict[str, float], *, now_utc: datetime) -> List[Dict[str, Any]]:
    current_month = _month_key(now_utc)
    prev_month = _month_key(_add_months(now_utc, -1))
    ordered_months = [prev_month, current_month]
    return [
        {"month": m, "total_incl_vat": month_totals[m]}
        for m in ordered_months
        if m in month_totals
    ]


def _build_cashflow_projection(month_totals: Dict[str, float], *, now_utc: datetime) -> List[Dict[str, Any]]:
    current_month = _month_key(now_utc)
    next_month = _month_key(_add_months(now_utc, 1))
    ordered_months = [current_month, next_month]
    return [
        {
            "month": m,
            "unpaid_amount": 0.0,
            "note": "(reconciliation not enabled yet)",
        }
        for m in ordered_months
        if m in month_totals
    ]


def _get_state_row(marketplace_id: str, *, db_path: Path) -> Dict[str, Any]:
    with _connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT marketplace_id, last_fetch_started_at, last_fetch_finished_at, last_fetch_status, last_error, last_lookback_days, rows_90d
            FROM df_payments_state
            WHERE marketplace_id = ?
            """,
            (marketplace_id,),
        ).fetchone()
    if not row:
        return {
            "marketplace_id": marketplace_id,
            "last_fetch_started_at": None,
            "last_fetch_finished_at": None,
            "last_fetch_status": None,
            "last_error": None,
            "last_lookback_days": None,
            "rows_90d": 0,
        }
    return dict(row)


def _update_state(
    marketplace_id: str,
    *,
    db_path: Path,
    last_fetch_started_at: Optional[str] = None,
    last_fetch_finished_at: Optional[str] = None,
    last_fetch_status: Optional[str] = None,
    last_error: Optional[str] = None,
    last_lookback_days: Optional[int] = None,
    rows_90d: Optional[int] = None,
) -> None:
    with _connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO df_payments_state (
                marketplace_id,
                last_fetch_started_at,
                last_fetch_finished_at,
                last_fetch_status,
                last_error,
                last_lookback_days,
                rows_90d
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(marketplace_id) DO UPDATE SET
                last_fetch_started_at=excluded.last_fetch_started_at,
                last_fetch_finished_at=excluded.last_fetch_finished_at,
                last_fetch_status=excluded.last_fetch_status,
                last_error=excluded.last_error,
                last_lookback_days=excluded.last_lookback_days,
                rows_90d=excluded.rows_90d
            """,
            (
                marketplace_id,
                last_fetch_started_at,
                last_fetch_finished_at,
                last_fetch_status,
                last_error,
                last_lookback_days,
                rows_90d,
            ),
        )
        conn.commit()


def get_df_payments_state(
    marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    *,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    effective_now = now_utc or datetime.now(timezone.utc)
    ensure_df_payments_tables(db_path)
    orders = _load_orders(marketplace_id, db_path=db_path)
    month_totals = _aggregate_invoice_totals(marketplace_id, db_path=db_path)
    invoices_by_month = _slice_invoices_window(month_totals, now_utc=effective_now)
    dashboard = {
        "invoices_by_month": invoices_by_month,
        "cashflow_projection": _build_cashflow_projection(month_totals, now_utc=effective_now),
    }
    state_row = _get_state_row(marketplace_id, db_path=db_path)
    state_row["rows_90d"] = state_row.get("rows_90d") or len(orders)
    return {
        "orders": orders,
        "dashboard": dashboard,
        "state": state_row,
    }


def _fetch_purchase_orders_from_api(
    marketplace_id: str,
    *,
    lookback_days: int,
    ship_from_party_id: Optional[str],
    limit: int,
    now_utc: datetime,
) -> Dict[str, Any]:
    created_before = now_utc.replace(microsecond=0)
    created_after = created_before - timedelta(days=lookback_days)

    auth_client = SpApiAuth()
    rdt = auth_client.get_rdt(
        [
            {
                "method": "GET",
                "path": DF_ORDERS_PATH,
                # dataElements intentionally omitted; DF Orders rejects purchaseOrders as a data element
            }
        ]
    )
    if not rdt:
        raise RuntimeError("Failed to obtain restricted data token for DF payments")

    headers = {
        "accept": "application/json",
        "x-amz-access-token": rdt,
    }

    params = {
        "includeDetails": "true",
        "createdAfter": created_after.isoformat(),
        "createdBefore": created_before.isoformat(),
        "limit": max(1, min(int(limit or DEFAULT_LIMIT), 100)),
    }
    if ship_from_party_id:
        params["shipFromPartyId"] = ship_from_party_id

    orders: List[Dict[str, Any]] = []
    next_token = None
    page = 0
    while True:
        call_params = {"includeDetails": "true"}
        if next_token:
            call_params["nextToken"] = next_token
        else:
            call_params.update(params)

        resp = requests.get(f"{SPAPI_HOST}{DF_ORDERS_PATH}", params=call_params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        page_orders = _extract_purchase_orders(data)
        if page_orders:
            orders.extend(page_orders)
        next_token = _extract_next_token(data)
        page += 1
        if not next_token:
            break
        if page >= MAX_PAGES:
            LOGGER.warning("[DF Payments] Pagination stopped after %s pages", page)
            break

    return {
        "orders": orders,
        "created_after": params["createdAfter"],
        "created_before": params["createdBefore"],
        "pages": page,
    }


def refresh_df_payments(
    marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    *,
    lookback_days: Optional[int] = None,
    ship_from_party_id: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
    fetcher: Optional[FetchFunc] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    ensure_df_payments_tables(db_path)
    effective_now = now_utc or datetime.now(timezone.utc)
    lookback = _clamp_lookback(lookback_days)
    fetch_func = fetcher or _fetch_purchase_orders_from_api

    started_iso = effective_now.replace(microsecond=0).isoformat()
    _update_state(
        marketplace_id,
        db_path=db_path,
        last_fetch_started_at=started_iso,
        last_fetch_status="IN_PROGRESS",
        last_error=None,
        last_lookback_days=lookback,
    )

    try:
        fetched = fetch_func(
            marketplace_id=marketplace_id,
            lookback_days=lookback,
            ship_from_party_id=ship_from_party_id,
            limit=limit or DEFAULT_LIMIT,
            now_utc=effective_now,
        )
        if isinstance(fetched, dict):
            orders_payload = _extract_purchase_orders(fetched)
        elif isinstance(fetched, list):
            orders_payload = fetched
        else:
            orders_payload = []
        summaries: List[Dict[str, Any]] = []
        for order in orders_payload:
            summary = _build_order_summary(order, now_utc=effective_now)
            if summary:
                summary["marketplace_id"] = marketplace_id
                summaries.append(summary)

        upserted = _upsert_orders(marketplace_id, summaries, db_path=db_path)
        pruned = _prune_old_orders(marketplace_id, now_utc=effective_now, db_path=db_path)
        rows_90d = _count_orders(marketplace_id, db_path=db_path)
        finished_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        _update_state(
            marketplace_id,
            db_path=db_path,
            last_fetch_started_at=started_iso,
            last_fetch_finished_at=finished_iso,
            last_fetch_status="SUCCESS",
            last_error=None,
            last_lookback_days=lookback,
            rows_90d=rows_90d,
        )
        return {
            "status": "refreshed",
            "orders_upserted": upserted,
            "orders_seen": len(orders_payload),
            "pruned": pruned,
            "rows_90d": rows_90d,
            "lookback_days": lookback,
        }
    except Exception as exc:
        finished_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        _update_state(
            marketplace_id,
            db_path=db_path,
            last_fetch_started_at=started_iso,
            last_fetch_finished_at=finished_iso,
            last_fetch_status="ERROR",
            last_error=str(exc),
            last_lookback_days=lookback,
        )
        LOGGER.error("[DF Payments] Refresh failed: %s", exc, exc_info=True)
        raise
