from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
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
DF_PAYMENTS_TERMS_DAYS = 30
INCREMENTAL_COOLDOWN_SECONDS = 600
INCREMENTAL_FAILURE_BACKOFF_SECONDS = int(os.getenv("DF_PAYMENTS_FAILURE_BACKOFF_SECONDS", "180"))
INCREMENTAL_SCHEDULER_INTERVAL_SECONDS = int(os.getenv("DF_PAYMENTS_SCHEDULER_INTERVAL_SECONDS", "45"))

_incremental_lock = Lock()
_dfp_scheduler_thread: Optional[threading.Thread] = None
_dfp_scheduler_stop = False

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


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat()


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
    pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else None
    if pagination:
        for key in ("nextToken", "next_token"):
            token = pagination.get(key)
            if token:
                return str(token)
    for key in ("nextToken", "next_token"):
        token = payload.get(key)
        if token:
            return str(token)
    inner = payload.get("payload")
    if isinstance(inner, dict):
        pagination_inner = inner.get("pagination") if isinstance(inner.get("pagination"), dict) else None
        if pagination_inner:
            for key in ("nextToken", "next_token"):
                token = pagination_inner.get(key)
                if token:
                    return str(token)
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


def _count_orders_in_window(marketplace_id: str, *, db_path: Path, window_start: datetime, window_end: datetime) -> int:
    with _connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM df_payments_orders
            WHERE marketplace_id = ? AND order_date_utc >= ? AND order_date_utc <= ?
            """,
            (
                marketplace_id,
                window_start.replace(microsecond=0).isoformat(),
                window_end.replace(microsecond=0).isoformat(),
            ),
        ).fetchone()
        return int(row["c"] if hasattr(row, "keys") else row[0])


def _max_order_date(marketplace_id: str, *, db_path: Path) -> Optional[str]:
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(order_date_utc) AS max_date FROM df_payments_orders WHERE marketplace_id = ?",
            (marketplace_id,),
        ).fetchone()
    if not row:
        return None
    value = row["max_date"] if hasattr(row, "keys") else row[0]
    return value


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


def _terms_shift_months(payment_terms_days: int) -> int:
    try:
        days = int(payment_terms_days)
    except Exception:
        days = DF_PAYMENTS_TERMS_DAYS
    return 1 if days >= 30 else 0


def _shift_month_key(month_key: str, months: int) -> str:
    try:
        base_dt = datetime.fromisoformat(f"{month_key}-01")
    except Exception:
        return month_key
    shifted = _add_months(base_dt, months)
    return _month_key(shifted)


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
        {"month": m, "total_incl_vat": float(month_totals.get(m, 0.0))}
        for m in ordered_months
    ]


def _build_cashflow_projection(
    month_totals: Dict[str, float], *, now_utc: datetime, payment_terms_days: int = DF_PAYMENTS_TERMS_DAYS
) -> List[Dict[str, Any]]:
    current_month = _month_key(now_utc)
    next_month = _month_key(_add_months(now_utc, 1))
    ordered_months = [current_month, next_month]
    shift_months = _terms_shift_months(payment_terms_days)

    expected_totals: Dict[str, float] = {}
    for month_key, total in month_totals.items():
        target_month = _shift_month_key(month_key, shift_months)
        expected_totals[target_month] = expected_totals.get(target_month, 0.0) + float(total)

    return [
        {
            "month": m,
            "unpaid_amount": float(expected_totals.get(m, 0.0)),
            "note": "(reconciliation not enabled yet)",
        }
        for m in ordered_months
    ]


def _get_state_row(marketplace_id: str, *, db_path: Path) -> Dict[str, Any]:
    with _connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT marketplace_id, last_fetch_started_at, last_fetch_finished_at, last_fetch_status, last_error, last_lookback_days, rows_90d, pages_fetched, fetched_orders_total, unique_po_total, rows_in_db_window, limit_used, last_incremental_started_at, last_incremental_finished_at, last_incremental_status, last_incremental_error, last_seen_order_date_utc, last_incremental_orders_upserted, last_incremental_pages_fetched, incremental_last_attempt_at_utc, incremental_last_success_at_utc, incremental_cooldown_until_utc
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
            "pages_fetched": None,
            "fetched_orders_total": None,
            "unique_po_total": None,
            "rows_in_db_window": None,
            "limit_used": None,
            "last_incremental_started_at": None,
            "last_incremental_finished_at": None,
            "last_incremental_status": None,
            "last_incremental_error": None,
            "last_seen_order_date_utc": None,
            "last_incremental_orders_upserted": None,
            "last_incremental_pages_fetched": None,
            "incremental_last_attempt_at_utc": None,
            "incremental_last_success_at_utc": None,
            "incremental_cooldown_until_utc": None,
        }
    return dict(row)


def compute_incremental_eligibility(
    state: Dict[str, Any],
    now_utc: Optional[datetime] = None,
    *,
    cooldown_seconds: int = INCREMENTAL_COOLDOWN_SECONDS,
    failure_backoff_seconds: int = INCREMENTAL_FAILURE_BACKOFF_SECONDS,
) -> Dict[str, Any]:
    """Compute whether DF Payments incremental scan is eligible to run.

    Returns a dict with eligibility flag, reason, next_eligible_at, worker_status, and auto_enabled.
    """

    effective_now = now_utc or _now_utc()
    baseline_ok = (state.get("last_fetch_status") or "").upper() == "SUCCESS" and bool(state.get("last_fetch_finished_at"))
    auto_enabled = bool(baseline_ok)

    last_status = (state.get("last_incremental_status") or "").upper()
    in_progress = last_status == "IN_PROGRESS" or (state.get("last_fetch_status") or "").upper() == "IN_PROGRESS"

    last_attempt = _parse_iso_dt(state.get("incremental_last_attempt_at_utc") or state.get("last_incremental_started_at"))
    last_success = _parse_iso_dt(state.get("incremental_last_success_at_utc"))
    if not last_success and last_status == "SUCCESS":
        last_success = _parse_iso_dt(state.get("last_incremental_finished_at"))
    if not last_success and baseline_ok:
        last_success = _parse_iso_dt(state.get("last_fetch_finished_at"))

    stored_cooldown = _parse_iso_dt(state.get("incremental_cooldown_until_utc"))
    anchor_cooldown = last_success + timedelta(seconds=cooldown_seconds) if last_success else None
    failure_cooldown = (
        last_attempt + timedelta(seconds=failure_backoff_seconds)
        if last_attempt and last_status == "ERROR"
        else None
    )

    next_dt_candidates = [dt for dt in (stored_cooldown, anchor_cooldown, failure_cooldown) if dt]
    next_eligible_dt = max(next_dt_candidates) if next_dt_candidates else None
    eligible = bool(baseline_ok) and not in_progress and (not next_eligible_dt or next_eligible_dt <= effective_now)

    reason: Optional[str] = None
    worker_status = "ok"
    worker_details: Optional[str] = None

    if not baseline_ok:
        worker_status = "waiting"
        reason = "Run Fetch Orders first"
    elif in_progress:
        worker_status = "locked"
        reason = "Lock held by another scan"
    elif next_eligible_dt and next_eligible_dt > effective_now:
        worker_status = "waiting"
        reason = f"cooldown until {next_eligible_dt.isoformat()}"

    if last_status == "ERROR":
        worker_status = "error"
        worker_details = state.get("last_incremental_error") or "Last incremental scan failed"

    return {
        "eligible": eligible,
        "reason": reason,
        "next_eligible_at": next_eligible_dt,
        "auto_enabled": auto_enabled,
        "worker_status": worker_status,
        "worker_details": worker_details,
        "last_success_at": last_success,
        "last_attempt_at": last_attempt,
    }


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
    pages_fetched: Optional[int] = None,
    fetched_orders_total: Optional[int] = None,
    unique_po_total: Optional[int] = None,
    rows_in_db_window: Optional[int] = None,
    limit_used: Optional[int] = None,
    last_incremental_started_at: Optional[str] = None,
    last_incremental_finished_at: Optional[str] = None,
    last_incremental_status: Optional[str] = None,
    last_incremental_error: Optional[str] = None,
    last_seen_order_date_utc: Optional[str] = None,
    last_incremental_orders_upserted: Optional[int] = None,
    last_incremental_pages_fetched: Optional[int] = None,
    incremental_last_attempt_at_utc: Optional[str] = None,
    incremental_last_success_at_utc: Optional[str] = None,
    incremental_cooldown_until_utc: Optional[str] = None,
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
                rows_90d,
                pages_fetched,
                fetched_orders_total,
                unique_po_total,
                rows_in_db_window,
                limit_used,
                last_incremental_started_at,
                last_incremental_finished_at,
                last_incremental_status,
                last_incremental_error,
                last_seen_order_date_utc,
                last_incremental_orders_upserted,
                last_incremental_pages_fetched,
                incremental_last_attempt_at_utc,
                incremental_last_success_at_utc,
                incremental_cooldown_until_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(marketplace_id) DO UPDATE SET
                last_fetch_started_at=excluded.last_fetch_started_at,
                last_fetch_finished_at=excluded.last_fetch_finished_at,
                last_fetch_status=excluded.last_fetch_status,
                last_error=excluded.last_error,
                last_lookback_days=excluded.last_lookback_days,
                rows_90d=excluded.rows_90d,
                pages_fetched=excluded.pages_fetched,
                fetched_orders_total=excluded.fetched_orders_total,
                unique_po_total=excluded.unique_po_total,
                rows_in_db_window=excluded.rows_in_db_window,
                limit_used=excluded.limit_used,
                last_incremental_started_at=excluded.last_incremental_started_at,
                last_incremental_finished_at=excluded.last_incremental_finished_at,
                last_incremental_status=excluded.last_incremental_status,
                last_incremental_error=excluded.last_incremental_error,
                last_seen_order_date_utc=excluded.last_seen_order_date_utc,
                last_incremental_orders_upserted=excluded.last_incremental_orders_upserted,
                last_incremental_pages_fetched=excluded.last_incremental_pages_fetched,
                incremental_last_attempt_at_utc=excluded.incremental_last_attempt_at_utc,
                incremental_last_success_at_utc=excluded.incremental_last_success_at_utc,
                incremental_cooldown_until_utc=excluded.incremental_cooldown_until_utc
            """,
            (
                marketplace_id,
                last_fetch_started_at,
                last_fetch_finished_at,
                last_fetch_status,
                last_error,
                last_lookback_days,
                rows_90d,
                pages_fetched,
                fetched_orders_total,
                unique_po_total,
                rows_in_db_window,
                limit_used,
                last_incremental_started_at,
                last_incremental_finished_at,
                last_incremental_status,
                last_incremental_error,
                last_seen_order_date_utc,
                last_incremental_orders_upserted,
                last_incremental_pages_fetched,
                incremental_last_attempt_at_utc,
                incremental_last_success_at_utc,
                incremental_cooldown_until_utc,
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
        "cashflow_projection": _build_cashflow_projection(
            month_totals, now_utc=effective_now, payment_terms_days=DF_PAYMENTS_TERMS_DAYS
        ),
    }
    state_row = _get_state_row(marketplace_id, db_path=db_path)
    state_row["rows_90d"] = state_row.get("rows_90d") or len(orders)
    order_dates = [o.get("order_date_utc") for o in orders if o.get("order_date_utc")]
    eligibility = compute_incremental_eligibility(state_row, effective_now)
    state_row["incremental_next_eligible_at_utc"] = _iso(eligibility.get("next_eligible_at"))
    state_row["incremental_wait_reason"] = eligibility.get("reason")
    state_row["incremental_auto_enabled"] = eligibility.get("auto_enabled")
    state_row["incremental_worker_status"] = eligibility.get("worker_status")
    state_row["incremental_worker_details"] = eligibility.get("worker_details")
    diagnostics = {
        "orders_count": len(orders),
        "min_order_date_utc": min(order_dates) if order_dates else None,
        "max_order_date_utc": max(order_dates) if order_dates else None,
        "lookback_days_applied": state_row.get("last_lookback_days"),
        "pages_fetched": state_row.get("pages_fetched") if isinstance(state_row, dict) else None,
        "fetched_orders_total": state_row.get("fetched_orders_total"),
        "unique_po_total": state_row.get("unique_po_total"),
        "rows_in_db_window": state_row.get("rows_in_db_window"),
        "limit_used": state_row.get("limit_used"),
        "last_incremental_started_at": state_row.get("last_incremental_started_at"),
        "last_incremental_finished_at": state_row.get("last_incremental_finished_at"),
        "last_incremental_status": state_row.get("last_incremental_status"),
        "last_incremental_error": state_row.get("last_incremental_error"),
        "last_seen_order_date_utc": state_row.get("last_seen_order_date_utc"),
        "last_incremental_orders_upserted": state_row.get("last_incremental_orders_upserted"),
        "last_incremental_pages_fetched": state_row.get("last_incremental_pages_fetched"),
        "incremental_last_attempt_at_utc": state_row.get("incremental_last_attempt_at_utc"),
        "incremental_last_success_at_utc": state_row.get("incremental_last_success_at_utc"),
        "incremental_cooldown_until_utc": state_row.get("incremental_cooldown_until_utc"),
        "incremental_next_eligible_at_utc": state_row.get("incremental_next_eligible_at_utc"),
        "incremental_auto_enabled": state_row.get("incremental_auto_enabled"),
        "incremental_worker_status": state_row.get("incremental_worker_status"),
        "incremental_worker_details": state_row.get("incremental_worker_details"),
    }
    state_row["diagnostics"] = diagnostics
    return {
        "orders": orders,
        "dashboard": dashboard,
        "state": state_row,
        "diagnostics": diagnostics,
    }


def get_df_payments_worker_metadata(
    marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    *,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> Dict[str, Any]:
    ensure_df_payments_tables(db_path)
    state = _get_state_row(marketplace_id, db_path=db_path)
    eligibility = compute_incremental_eligibility(state, _now_utc())
    state["incremental_next_eligible_at_utc"] = _iso(eligibility.get("next_eligible_at"))
    state["incremental_wait_reason"] = eligibility.get("reason")
    state["incremental_auto_enabled"] = eligibility.get("auto_enabled")
    state["incremental_worker_status"] = eligibility.get("worker_status")
    state["incremental_worker_details"] = eligibility.get("worker_details")
    return state


def _fetch_purchase_orders_from_api(
    marketplace_id: str,
    *,
    lookback_days: int,
    ship_from_party_id: Optional[str],
    limit: int,
    now_utc: datetime,
    created_after_override: Optional[datetime] = None,
    created_before_override: Optional[datetime] = None,
) -> Dict[str, Any]:
    created_before = (created_before_override or now_utc).astimezone(timezone.utc).replace(microsecond=0)
    created_after = (
        created_after_override.astimezone(timezone.utc).replace(microsecond=0)
        if created_after_override
        else created_before - timedelta(days=lookback_days)
    )

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

    effective_limit = max(1, min(int(limit or DEFAULT_LIMIT), 100))
    base_params = {
        "includeDetails": "true",
        "createdAfter": created_after.isoformat(),
        "createdBefore": created_before.isoformat(),
        "limit": effective_limit,
    }
    if ship_from_party_id:
        base_params["shipFromPartyId"] = ship_from_party_id

    orders_by_po: Dict[str, Dict[str, Any]] = {}
    next_token = None
    page = 0
    while True:
        call_params = dict(base_params)
        if next_token:
            call_params["nextToken"] = next_token

        resp = requests.get(
            f"{SPAPI_HOST}{DF_ORDERS_PATH}", params=call_params, headers=headers, timeout=30
        )
        if resp.status_code >= 400:
            try:
                body_snip = resp.text[:800]
            except Exception:
                body_snip = "<unavailable>"
            LOGGER.error(
                "[DF Payments] Page %s error status=%s params_keys=%s body_snip=%s",
                page + 1,
                resp.status_code,
                sorted(call_params.keys()),
                body_snip,
            )
            resp.raise_for_status()
        resp.raise_for_status()
        data = resp.json()
        page_orders = _extract_purchase_orders(data)
        if page_orders:
            for order in page_orders:
                po_number = (order.get("purchaseOrderNumber") or order.get("poNumber") or "").strip()
                if not po_number:
                    continue
                existing = orders_by_po.get(po_number)
                if existing:
                    def _item_count(payload: Dict[str, Any]) -> int:
                        details = payload.get("orderDetails") or payload.get("order_details") or {}
                        items = details.get("items") or details.get("orderItems") or []
                        return len(items) if isinstance(items, list) else 0

                    current_items = _item_count(order)
                    existing_items = _item_count(existing)
                    if current_items > existing_items:
                        orders_by_po[po_number] = order
                else:
                    orders_by_po[po_number] = order
        next_token = _extract_next_token(data)
        page += 1
        LOGGER.info(
            "[DF Payments] Page %s fetched | sent_createdAfter=%s sent_createdBefore=%s sent_limit=%s sent_shipFromPartyId=%s sent_includeDetails=%s sent_nextToken=%s | received_orders=%s | unique_po_so_far=%s | nextToken_present=%s | nextToken_length=%s",
            page + 1,
            "createdAfter" in call_params,
            "createdBefore" in call_params,
            "limit" in call_params,
            "shipFromPartyId" in call_params,
            call_params.get("includeDetails") == "true",
            bool(call_params.get("nextToken")),
            len(page_orders) if page_orders else 0,
            len(orders_by_po),
            bool(next_token),
            len(next_token) if next_token else 0,
        )
        if not next_token:
            break
        if page >= MAX_PAGES:
            LOGGER.warning("[DF Payments] Pagination stopped after %s pages", page)
            break

    orders = list(orders_by_po.values())

    return {
        "orders": orders,
        "created_after": base_params["createdAfter"],
        "created_before": base_params["createdBefore"],
        "pages": page,
        "limit_used": effective_limit,
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
        fetch_limit = limit or DEFAULT_LIMIT
        fetched = fetch_func(
            marketplace_id=marketplace_id,
            lookback_days=lookback,
            ship_from_party_id=ship_from_party_id,
            limit=fetch_limit,
            now_utc=effective_now,
        )
        fetched_meta = fetched if isinstance(fetched, dict) else {}
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
        max_order_iso = _max_order_date(marketplace_id, db_path=db_path)
        window_start = effective_now - timedelta(days=lookback)
        rows_in_window = _count_orders_in_window(
            marketplace_id,
            db_path=db_path,
            window_start=window_start,
            window_end=effective_now,
        )
        LOGGER.info(
            "[DF Payments] Fetch summary | fetched_orders_total=%s | unique_po_total=%s | rows_in_db_window=%s | pages_fetched=%s",
            len(orders_payload),
            len(summaries),
            rows_in_window,
            fetched_meta.get("pages"),
        )
        finished_iso = _now_utc().isoformat()
        _update_state(
            marketplace_id,
            db_path=db_path,
            last_fetch_started_at=started_iso,
            last_fetch_finished_at=finished_iso,
            last_fetch_status="SUCCESS",
            last_error=None,
            last_lookback_days=lookback,
            rows_90d=rows_90d,
            pages_fetched=fetched_meta.get("pages"),
            fetched_orders_total=len(orders_payload),
            unique_po_total=len(summaries),
            rows_in_db_window=rows_in_window,
            limit_used=fetched_meta.get("limit_used") if fetched_meta.get("limit_used") else fetch_limit,
            last_seen_order_date_utc=max_order_iso,
        )
        return {
            "status": "refreshed",
            "orders_upserted": upserted,
            "orders_seen": len(orders_payload),
            "pruned": pruned,
            "rows_90d": rows_90d,
            "lookback_days": lookback,
            "created_after": fetched_meta.get("created_after"),
            "created_before": fetched_meta.get("created_before"),
            "pages_fetched": fetched_meta.get("pages"),
            "fetched_orders_total": len(orders_payload),
            "unique_po_total": len(summaries),
            "rows_in_db_window": rows_in_window,
            "limit_used": fetched_meta.get("limit_used") if fetched_meta.get("limit_used") else fetch_limit,
            "last_seen_order_date_utc": max_order_iso,
        }
    except Exception as exc:
        finished_iso = _now_utc().isoformat()
        _update_state(
            marketplace_id,
            db_path=db_path,
            last_fetch_started_at=started_iso,
            last_fetch_finished_at=finished_iso,
            last_fetch_status="ERROR",
            last_error=str(exc),
            last_lookback_days=lookback,
            pages_fetched=fetched_meta.get("pages") if "fetched_meta" in locals() else None,
            fetched_orders_total=len(orders_payload) if "orders_payload" in locals() else None,
            unique_po_total=len(summaries) if "summaries" in locals() else None,
            rows_in_db_window=None,
            limit_used=fetched_meta.get("limit_used") if "fetched_meta" in locals() and fetched_meta.get("limit_used") else (limit or DEFAULT_LIMIT),
        )
        LOGGER.error("[DF Payments] Refresh failed: %s", exc, exc_info=True)
        raise


def incremental_refresh_df_payments(
    marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    *,
    ship_from_party_id: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
    fetcher: Optional[FetchFunc] = None,
    now_utc: Optional[datetime] = None,
    triggered_by: str = "manual",
    force: bool = False,
    cooldown_seconds: int = INCREMENTAL_COOLDOWN_SECONDS,
    failure_backoff_seconds: int = INCREMENTAL_FAILURE_BACKOFF_SECONDS,
) -> Dict[str, Any]:
    ensure_df_payments_tables(db_path)
    effective_now = now_utc or _now_utc()
    state_row = _get_state_row(marketplace_id, db_path=db_path)
    baseline_ok = (state_row.get("last_fetch_status") or "").upper() == "SUCCESS" and bool(state_row.get("last_fetch_finished_at"))

    if not baseline_ok and not force:
        return {"status": "waiting", "reason": "baseline_required"}

    if not _incremental_lock.acquire(blocking=False):
        return {"status": "locked", "reason": "lock_held"}

    last_seen_dt = _parse_iso_dt(state_row.get("last_seen_order_date_utc"))
    fallback_start = effective_now - timedelta(days=7)
    buffered = last_seen_dt - timedelta(hours=2) if last_seen_dt else None
    candidates = [dt for dt in (buffered, fallback_start) if dt]
    created_after_dt = max(candidates) if candidates else fallback_start
    created_before_dt = effective_now

    started_iso = effective_now.replace(microsecond=0).isoformat()
    _update_state(
        marketplace_id,
        db_path=db_path,
        last_incremental_started_at=started_iso,
        last_incremental_status="IN_PROGRESS",
        last_incremental_error=None,
        incremental_last_attempt_at_utc=started_iso,
        incremental_cooldown_until_utc=None,
    )

    fetch_limit = limit or DEFAULT_LIMIT
    lookback_days = max(1, (created_before_dt - created_after_dt).days + 1)
    fetch_func = fetcher or _fetch_purchase_orders_from_api

    try:
        fetched = fetch_func(
            marketplace_id=marketplace_id,
            lookback_days=lookback_days,
            ship_from_party_id=ship_from_party_id,
            limit=fetch_limit,
            now_utc=effective_now,
            created_after_override=created_after_dt,
            created_before_override=created_before_dt,
        )
        fetched_meta = fetched if isinstance(fetched, dict) else {}
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
        window_start = effective_now - timedelta(days=MAX_LOOKBACK_DAYS)
        rows_in_window = _count_orders_in_window(
            marketplace_id,
            db_path=db_path,
            window_start=window_start,
            window_end=effective_now,
        )

        fetched_order_dates = [s.get("order_date_utc") for s in summaries if s.get("order_date_utc")]
        fetched_max_dt = max((_parse_iso_dt(v) for v in fetched_order_dates if v), default=None)
        db_max_iso = _max_order_date(marketplace_id, db_path=db_path)
        db_max_dt = _parse_iso_dt(db_max_iso) if db_max_iso else None
        chosen_max_dt = max([dt for dt in (fetched_max_dt, db_max_dt) if dt], default=None)
        chosen_max_iso = chosen_max_dt.replace(microsecond=0).isoformat() if chosen_max_dt else state_row.get("last_seen_order_date_utc")

        LOGGER.info(
            "[DF Payments] Incremental summary | trigger=%s | fetched_orders_total=%s | unique_po_total=%s | rows_in_db_window=%s | pages_fetched=%s",
            triggered_by,
            len(orders_payload),
            len(summaries),
            rows_in_window,
            fetched_meta.get("pages"),
        )

        finished_dt = now_utc or _now_utc()
        finished_iso = finished_dt.replace(microsecond=0).isoformat()
        cooldown_until_dt = finished_dt + timedelta(seconds=cooldown_seconds)
        cooldown_until_iso = cooldown_until_dt.replace(microsecond=0).isoformat()

        _update_state(
            marketplace_id,
            db_path=db_path,
            last_incremental_started_at=started_iso,
            last_incremental_finished_at=finished_iso,
            last_incremental_status="SUCCESS",
            last_incremental_error=None,
            last_seen_order_date_utc=chosen_max_iso,
            last_incremental_orders_upserted=upserted,
            last_incremental_pages_fetched=fetched_meta.get("pages"),
            rows_90d=rows_90d,
            pages_fetched=fetched_meta.get("pages"),
            fetched_orders_total=len(orders_payload),
            unique_po_total=len(summaries),
            rows_in_db_window=rows_in_window,
            limit_used=fetched_meta.get("limit_used") if fetched_meta.get("limit_used") else fetch_limit,
            incremental_last_attempt_at_utc=started_iso,
            incremental_last_success_at_utc=finished_iso,
            incremental_cooldown_until_utc=cooldown_until_iso,
        )

        return {
            "status": "incremental_refreshed",
            "orders_upserted": upserted,
            "orders_seen": len(orders_payload),
            "pruned": pruned,
            "rows_90d": rows_90d,
            "lookback_days": lookback_days,
            "created_after": fetched_meta.get("created_after"),
            "created_before": fetched_meta.get("created_before"),
            "pages_fetched": fetched_meta.get("pages"),
            "fetched_orders_total": len(orders_payload),
            "unique_po_total": len(summaries),
            "rows_in_db_window": rows_in_window,
            "limit_used": fetched_meta.get("limit_used") if fetched_meta.get("limit_used") else fetch_limit,
            "last_seen_order_date_utc": chosen_max_iso,
            "next_eligible_utc": cooldown_until_iso,
            "triggered_by": triggered_by,
        }
    except Exception as exc:
        finished_dt = now_utc or _now_utc()
        finished_iso = finished_dt.replace(microsecond=0).isoformat()
        last_success_dt = _parse_iso_dt(state_row.get("incremental_last_success_at_utc") or state_row.get("last_incremental_finished_at"))
        if not last_success_dt and baseline_ok:
            last_success_dt = _parse_iso_dt(state_row.get("last_fetch_finished_at"))
        cooldown_candidates = [
            last_success_dt + timedelta(seconds=cooldown_seconds) if last_success_dt else None,
            finished_dt + timedelta(seconds=failure_backoff_seconds),
        ]
        cooldown_candidates = [dt for dt in cooldown_candidates if dt]
        cooldown_until_dt = max(cooldown_candidates) if cooldown_candidates else None
        _update_state(
            marketplace_id,
            db_path=db_path,
            last_incremental_started_at=started_iso,
            last_incremental_finished_at=finished_iso,
            last_incremental_status="ERROR",
            last_incremental_error=str(exc),
            last_seen_order_date_utc=state_row.get("last_seen_order_date_utc"),
            last_incremental_orders_upserted=len(summaries) if "summaries" in locals() else None,
            last_incremental_pages_fetched=fetched_meta.get("pages") if "fetched_meta" in locals() else None,
            incremental_last_attempt_at_utc=started_iso,
            incremental_last_success_at_utc=state_row.get("incremental_last_success_at_utc"),
            incremental_cooldown_until_utc=_iso(cooldown_until_dt),
        )
        LOGGER.error("[DF Payments] Incremental refresh failed (%s): %s", triggered_by, exc, exc_info=True)
        raise
    finally:
        _incremental_lock.release()


def maybe_run_df_payments_incremental_auto(
    marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    *,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
    now_utc: Optional[datetime] = None,
    fetcher: Optional[FetchFunc] = None,
) -> Dict[str, Any]:
    """Run DF Payments incremental scan automatically if eligible.

    Returns a dict with status of the attempt (ran / waiting / locked / error).
    """

    effective_now = now_utc or _now_utc()
    state_row = _get_state_row(marketplace_id, db_path=db_path)
    eligibility = compute_incremental_eligibility(state_row, effective_now)

    if not eligibility.get("auto_enabled"):
        return {
            "status": "waiting",
            "reason": eligibility.get("reason") or "auto_disabled",
            "next_eligible_at": eligibility.get("next_eligible_at"),
        }

    if not eligibility.get("eligible"):
        return {
            "status": "waiting",
            "reason": eligibility.get("reason") or "cooldown",
            "next_eligible_at": eligibility.get("next_eligible_at"),
        }

    result = incremental_refresh_df_payments(
        marketplace_id,
        db_path=db_path,
        fetcher=fetcher,
        now_utc=effective_now,
        triggered_by="auto",
        force=False,
    )
    return {"status": result.get("status") or "ran", **result}


def _dfp_scheduler_loop(
    marketplace_id: str,
    *,
    db_path: Path,
    interval_seconds: int,
):
    global _dfp_scheduler_stop
    LOGGER.info(
        "[DF Payments] Incremental scheduler started (interval=%ss, marketplace=%s)",
        interval_seconds,
        marketplace_id,
    )
    while not _dfp_scheduler_stop:
        try:
            outcome = maybe_run_df_payments_incremental_auto(
                marketplace_id,
                db_path=db_path,
            )
            status = (outcome.get("status") or "waiting").lower()
            if status == "waiting":
                reason = outcome.get("reason") or "cooldown"
                LOGGER.debug("[DF Payments] Auto-scan waiting: %s", reason)
            elif status == "locked":
                LOGGER.debug("[DF Payments] Auto-scan skipped (lock held)")
            elif status == "error":
                LOGGER.warning("[DF Payments] Auto-scan error: %s", outcome.get("error"))
            else:
                LOGGER.info(
                    "[DF Payments] Auto-scan completed | status=%s | orders_upserted=%s",
                    status,
                    outcome.get("orders_upserted"),
                )
        except Exception as exc:  # pragma: no cover - scheduler safety
            LOGGER.error("[DF Payments] Auto-scheduler tick failed: %s", exc, exc_info=True)
        finally:
            time.sleep(interval_seconds)
    LOGGER.info("[DF Payments] Incremental scheduler stopped")


def start_df_payments_incremental_scheduler(
    marketplace_id: str = DEFAULT_MARKETPLACE_ID,
    *,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
    interval_seconds: int = INCREMENTAL_SCHEDULER_INTERVAL_SECONDS,
):
    """Start the background scheduler that triggers incremental scans when eligible."""
    global _dfp_scheduler_thread, _dfp_scheduler_stop

    if _dfp_scheduler_thread and _dfp_scheduler_thread.is_alive():
        LOGGER.debug("[DF Payments] Scheduler already running; skipping start")
        return

    _dfp_scheduler_stop = False
    thread = threading.Thread(
        target=_dfp_scheduler_loop,
        name="DfPaymentsIncrementalScheduler",
        kwargs={
            "marketplace_id": marketplace_id,
            "db_path": db_path,
            "interval_seconds": max(10, interval_seconds),
        },
        daemon=True,
    )
    thread.start()
    _dfp_scheduler_thread = thread
    LOGGER.info("[DF Payments] Scheduler thread started")


def stop_df_payments_incremental_scheduler(timeout: float = 2.0) -> None:
    """Signal the scheduler to stop and wait briefly for shutdown."""
    global _dfp_scheduler_stop, _dfp_scheduler_thread
    _dfp_scheduler_stop = True
    thread = _dfp_scheduler_thread
    if thread and thread.is_alive():
        thread.join(timeout=timeout)
    _dfp_scheduler_thread = None
