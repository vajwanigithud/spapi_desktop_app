from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI

from config import MARKETPLACE_IDS
from services import vendor_inventory_realtime as rt_inventory
from services import vendor_realtime_sales as rt_sales
from services.db import ensure_app_kv_table, get_app_kv, get_db_connection
from services.df_payments import get_df_payments_worker_metadata
from services.vendor_po_status_store import get_vendor_po_status_payload
from services.vendor_rt_inventory_state import get_refresh_metadata
from services.vendor_rt_sales_ledger import get_ledger_summary, get_worker_lock

router = APIRouter()
UAE_TZ = timezone(timedelta(hours=4))
RT_SALES_EXPECTED_INTERVAL_MINUTES = 15
RT_SALES_GRACE_MINUTES = 5
WAITING_STATUSES = {"cooldown", "locked", "waiting"}
OVERDUE_STATUSES = {"overdue"}
MARKETPLACE_IDS_ENV = [
    mp.strip() for mp in (os.getenv("MARKETPLACE_IDS") or os.getenv("MARKETPLACE_ID", "")).split(",") if mp.strip()
]
DEFAULT_MARKETPLACE_ID = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else (MARKETPLACE_IDS_ENV[0] if MARKETPLACE_IDS_ENV else "A2VIGQ35RCS4UG")


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    candidate = value.strip()
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


def _fmt_iso_utc(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = _parse_iso_datetime(str(value))
    if not dt:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_uae(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    dt: Optional[datetime]
    if isinstance(value, datetime):
        dt = value
    else:
        dt = _parse_iso_datetime(str(value))
    if not dt:
        return None
    return dt.astimezone(UAE_TZ).strftime("%Y-%m-%d %H:%M UAE")


def _compute_overdue_status(
    now_utc: datetime, next_eligible_dt: Optional[datetime], grace_minutes: Optional[int]
) -> tuple[Optional[str], int]:
    if next_eligible_dt is None:
        return None, 0

    grace_delta = timedelta(minutes=grace_minutes or 0)
    deadline = next_eligible_dt + grace_delta

    if now_utc > deadline:
        overdue_delta = now_utc - deadline
        overdue_by_minutes = max(0, int(overdue_delta.total_seconds() // 60))
        return "overdue", overdue_by_minutes

    if now_utc <= next_eligible_dt:
        return "waiting", 0

    return None, 0


def _classify_worker_state(
    now_utc: datetime,
    *,
    cooldown_until_dt: Optional[datetime],
    last_error: Optional[str],
    next_eligible_dt: Optional[datetime],
    grace_minutes: Optional[int] = None,
) -> tuple[str, Optional[str], int]:
    """Classify worker status with cooldown and error awareness."""

    if cooldown_until_dt and cooldown_until_dt > now_utc:
        normalized_error = (last_error or "").strip() or None
        cooldown_msg = f"Cooldown until {_fmt_uae(cooldown_until_dt)}"
        if normalized_error:
            cooldown_msg = f"{cooldown_msg} (last error: {normalized_error})"
        return "waiting", cooldown_msg, 0

    normalized_error = (last_error or "").strip() or None
    if normalized_error:
        return "error", normalized_error, 0

    overdue_status, overdue_delta = _compute_overdue_status(now_utc, next_eligible_dt, grace_minutes)
    if overdue_status:
        details = None
        if overdue_delta:
            details = f"Overdue by {overdue_delta} minutes"
        return overdue_status, details, overdue_delta

    return "ok", None, 0


def _inventory_domain(now_utc: datetime, marketplace_id: str) -> Dict[str, Any]:
    workers: List[Dict[str, Any]] = []
    status = "ok"
    details: Optional[str] = None
    base_details: Optional[str] = None
    cooldown_until_dt: Optional[datetime] = None
    next_eligible_dt: Optional[datetime] = None
    last_run_iso: Optional[str] = None
    last_run_dt: Optional[datetime] = None
    refresh_meta: Dict[str, Any] = {}
    overdue_by_minutes = 0

    try:
        ensure_app_kv_table()
        with get_db_connection() as conn:
            last_refresh_raw = get_app_kv(conn, rt_inventory.COOLDOWN_KV_KEY)
    except Exception as exc:
        last_refresh_raw = None
        base_details = f"Cooldown read failed: {exc}"

    last_refresh_dt = _parse_iso_datetime(last_refresh_raw) if last_refresh_raw else None
    if last_refresh_dt:
        last_run_dt = last_refresh_dt
        last_run_iso = last_refresh_dt.isoformat()
        cooldown_until_dt = last_refresh_dt + timedelta(hours=getattr(rt_inventory, "COOLDOWN_HOURS", 1))
        next_eligible_dt = cooldown_until_dt

    try:
        refresh_meta = get_refresh_metadata(marketplace_id)
    except Exception as exc:  # pragma: no cover - defensive
        refresh_meta = {}
        base_details = base_details or f"Refresh metadata unavailable: {exc}"

    refresh_last_finished = refresh_meta.get("last_refresh_finished_at") if isinstance(refresh_meta, dict) else None
    refresh_status = (refresh_meta or {}).get("last_refresh_status") if isinstance(refresh_meta, dict) else None
    refresh_error = (refresh_meta or {}).get("last_error") if isinstance(refresh_meta, dict) else None
    refresh_in_progress = bool((refresh_meta or {}).get("in_progress"))
    refresh_failed = refresh_status == "FAILED"
    error_reason = refresh_error or ("Last refresh failed" if refresh_failed else None)

    if refresh_last_finished:
        last_run_iso = refresh_last_finished
        last_run_dt = _parse_iso_datetime(refresh_last_finished) or last_run_dt

    if refresh_in_progress:
        status = "locked"
        details = "Refresh in progress"
        overdue_by_minutes = 0
    else:
        status, details, overdue_by_minutes = _classify_worker_state(
            now_utc,
            cooldown_until_dt=cooldown_until_dt,
            last_error=error_reason,
            next_eligible_dt=next_eligible_dt,
            grace_minutes=None,
        )
        if not details:
            details = base_details

    next_display_dt = cooldown_until_dt or next_eligible_dt

    workers.append(
        {
            "key": "rt_inventory_refresh",
            "name": "Realtime Inventory Refresh",
            "status": status,
            "last_run_at_uae": _fmt_uae(last_run_iso),
            "last_run_utc": _fmt_iso_utc(last_run_dt or last_run_iso),
            "next_eligible_at_uae": _fmt_uae(next_display_dt),
            "next_run_utc": _fmt_iso_utc(next_display_dt),
            "details": details,
            "message": details,
            "expected_interval_minutes": None,
            "grace_minutes": None,
            "overdue_by_minutes": overdue_by_minutes,
            "what": "Fetches Amazon RT inventory and stores snapshot",
            "mode": "auto",
        }
    )

    materializer_status = status
    materializer_details = details
    workers.append(
        {
            "key": "inventory_materializer",
            "name": "Inventory Materializer",
            "status": materializer_status,
            "last_run_at_uae": _fmt_uae(last_run_iso),
            "last_run_utc": _fmt_iso_utc(last_run_dt or last_run_iso),
            "next_eligible_at_uae": None,
            "next_run_utc": None,
            "details": materializer_details,
            "message": materializer_details,
            "expected_interval_minutes": None,
            "grace_minutes": None,
            "overdue_by_minutes": overdue_by_minutes,
            "what": "Writes inventory snapshot into SQLite safely",
            "mode": "auto",
        }
    )

    return {"title": "Inventory", "workers": workers}


def _rt_sales_domain(now_utc: datetime, marketplace_id: str) -> Dict[str, Any]:
    workers: List[Dict[str, Any]] = []
    try:
        ledger_summary = get_ledger_summary(marketplace_id, now_utc=now_utc)
    except Exception:  # pragma: no cover - defensive
        ledger_summary = {
            "missing": 0,
            "requested": 0,
            "downloaded": 0,
            "applied": 0,
            "failed": 0,
            "next_claimable_hour_utc": None,
            "last_applied_hour_utc": None,
        }

    try:
        lock_row = get_worker_lock(marketplace_id)
    except Exception:  # pragma: no cover - defensive
        lock_row = None

    lock_expires_dt = _parse_iso_datetime(lock_row.get("expires_at")) if lock_row else None
    lock_stale = bool(lock_row) and (lock_expires_dt is None or lock_expires_dt <= now_utc)

    cooldown_active = False
    cooldown_until_dt: Optional[datetime] = None
    try:
        cooldown_active = rt_sales.is_in_quota_cooldown(now_utc)
        if cooldown_active:
            cooldown_until_dt = rt_sales.get_quota_cooldown_until()
            if cooldown_until_dt and not isinstance(cooldown_until_dt, datetime):
                cooldown_until_dt = _parse_iso_datetime(str(cooldown_until_dt))
    except Exception:
        cooldown_active = False
        cooldown_until_dt = None

    status = "ok"
    details: Optional[str] = None
    last_run_dt = _parse_iso_datetime(ledger_summary.get("last_applied_hour_utc"))
    next_eligible_dt: Optional[datetime] = None
    if last_run_dt:
        next_eligible_dt = last_run_dt + timedelta(minutes=RT_SALES_EXPECTED_INTERVAL_MINUTES)

    if cooldown_active:
        status = "cooldown"
        if cooldown_until_dt:
            if next_eligible_dt:
                next_eligible_dt = max(next_eligible_dt, cooldown_until_dt)
            else:
                next_eligible_dt = cooldown_until_dt
        if cooldown_until_dt:
            details = f"Cooldown until {_fmt_uae(cooldown_until_dt)}"
        else:
            details = "Quota cooldown active"
    elif lock_row:
        if lock_stale:
            status = "error"
            details = "Worker lock stale"
        else:
            status = "locked"
            if lock_expires_dt:
                next_eligible_dt = max(next_eligible_dt, lock_expires_dt) if next_eligible_dt else lock_expires_dt
            owner = lock_row.get("owner")
            if owner:
                details = f"Lock owner: {owner}"
    elif ledger_summary.get("failed"):
        status = "error"
        details = "Failed ledger hours present"

    overdue_by_minutes = 0
    overdue_status, overdue_delta = _compute_overdue_status(now_utc, next_eligible_dt, RT_SALES_GRACE_MINUTES)
    if status not in {"error", "cooldown", "locked"} and overdue_status:
        status = overdue_status
        overdue_by_minutes = overdue_delta
        if overdue_by_minutes and not details:
            details = f"Overdue by {overdue_by_minutes} minutes"

    if status == "ok" and next_eligible_dt is None:
        status = "error"
        details = details or "No schedule available"

    last_run_iso = ledger_summary.get("last_applied_hour_utc")
    if not next_eligible_dt and last_run_dt:
        next_eligible_dt = last_run_dt + timedelta(minutes=RT_SALES_EXPECTED_INTERVAL_MINUTES)

    last_run_display = _fmt_uae(last_run_dt or last_run_iso)
    workers.append(
        {
            "key": "rt_sales_sync",
            "name": "RT Sales Sync",
            "status": status,
            "last_run_at_uae": last_run_display,
            "last_run_utc": _fmt_iso_utc(last_run_dt or last_run_iso),
            "next_eligible_at_uae": _fmt_uae(next_eligible_dt),
            "next_run_utc": _fmt_iso_utc(next_eligible_dt),
            "details": details,
            "message": details,
            "expected_interval_minutes": RT_SALES_EXPECTED_INTERVAL_MINUTES,
            "grace_minutes": RT_SALES_GRACE_MINUTES,
            "overdue_by_minutes": overdue_by_minutes,
            "what": "Ingests real-time sales and maintains hourly ledger",
            "mode": "auto",
        }
    )

    return {"title": "REAL-TIME SALES", "workers": workers}


def _vendor_po_domain() -> Dict[str, Any]:
    workers: List[Dict[str, Any]] = []
    payload: Dict[str, Any]
    try:
        payload = get_vendor_po_status_payload()
    except Exception:  # pragma: no cover - defensive
        payload = {}

    last_success = payload.get("last_success_at") if isinstance(payload, dict) else None
    workers.append(
        {
            "key": "vendor_po_sync",
            "name": "Vendor PO Sync",
            "status": "ok",
            "last_run_at_uae": _fmt_uae(last_success),
            "last_run_utc": _fmt_iso_utc(last_success),
            "next_eligible_at_uae": None,
            "next_run_utc": None,
            "details": "Manual / on-demand",
            "message": "Manual / on-demand",
            "expected_interval_minutes": None,
            "grace_minutes": None,
            "overdue_by_minutes": 0,
            "what": "Refreshes Vendor POs when run manually",
            "mode": "manual",
        }
    )

    return {"title": "VENDOR PO", "workers": workers}


def _df_payments_domain(now_utc: datetime, marketplace_id: str) -> Dict[str, Any]:
    workers: List[Dict[str, Any]] = []
    status = "ok"
    details: Optional[str] = None
    next_eligible_dt: Optional[datetime] = None
    auto_enabled = False

    try:
        meta = get_df_payments_worker_metadata(marketplace_id)
    except Exception as exc:  # pragma: no cover - defensive
        meta = None
        status = "error"
        details = f"Metadata unavailable: {exc}"

    last_finished = None
    last_status = None
    last_error = None
    last_started = None
    last_success = None

    if isinstance(meta, dict):
        last_finished = meta.get("last_incremental_finished_at")
        last_started = meta.get("last_incremental_started_at")
        last_status = meta.get("last_incremental_status")
        last_error = meta.get("last_incremental_error")
        next_eligible_dt = _parse_iso_datetime(meta.get("incremental_next_eligible_at_utc"))
        details = meta.get("incremental_worker_details") or meta.get("incremental_wait_reason") or details
        status = meta.get("incremental_worker_status") or status
        last_success = meta.get("incremental_last_success_at_utc")
        auto_enabled = bool(meta.get("incremental_auto_enabled"))

    last_finished_dt = _parse_iso_datetime(last_finished)
    last_started_dt = _parse_iso_datetime(last_started)
    last_success_dt = _parse_iso_datetime(last_success)

    if (last_status or "").upper() == "ERROR" and status == "ok":
        status = "error"
        details = details or last_error or "Last incremental scan failed"

    last_run_display = _fmt_uae(last_success_dt or last_finished_dt or last_finished or last_started_dt)
    workers.append(
        {
            "key": "df_payments_incremental",
            "name": "DF Payments Incremental Scan",
            "status": status,
            "last_run_at_uae": last_run_display,
            "last_run_utc": _fmt_iso_utc(last_success_dt or last_finished_dt or last_finished or last_started_dt),
            "next_eligible_at_uae": _fmt_uae(next_eligible_dt) if next_eligible_dt else None,
            "next_run_utc": _fmt_iso_utc(next_eligible_dt) if next_eligible_dt else None,
            "details": details,
            "message": details,
            "what": "Incrementally fetches new DF orders since last successful scan (10-min cadence)",
            "mode": "auto" if auto_enabled else "manual",
        }
    )

    return {"title": "DF PAYMENTS", "workers": workers}


def _collect_workers(domains: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_workers: List[Dict[str, Any]] = []
    for domain in domains.values():
        workers = domain.get("workers") if isinstance(domain, dict) else None
        if workers:
            all_workers.extend([w for w in workers if isinstance(w, dict)])
    return all_workers


@router.get("/api/workers/status")
def get_worker_status() -> Dict[str, Any]:
    now_utc = _utcnow()
    marketplace_id = DEFAULT_MARKETPLACE_ID

    domains: Dict[str, Dict[str, Any]] = {}
    try:
        domains["inventory"] = _inventory_domain(now_utc, marketplace_id)
    except Exception as exc:  # pragma: no cover - defensive
        domains["inventory"] = {
            "title": "Inventory",
            "workers": [
                {
                    "key": "rt_inventory_refresh",
                    "name": "Realtime Inventory Refresh",
                    "status": "error",
                    "last_run_at_uae": None,
                    "last_run_utc": None,
                    "next_eligible_at_uae": None,
                    "next_run_utc": None,
                    "details": str(exc),
                    "message": str(exc),
                    "expected_interval_minutes": None,
                    "grace_minutes": None,
                    "overdue_by_minutes": 0,
                    "what": "Fetches Amazon RT inventory and stores snapshot",
                    "mode": "auto",
                },
                {
                    "key": "inventory_materializer",
                    "name": "Inventory Materializer",
                    "status": "error",
                    "last_run_at_uae": None,
                    "last_run_utc": None,
                    "next_eligible_at_uae": None,
                    "next_run_utc": None,
                    "details": str(exc),
                    "message": str(exc),
                    "expected_interval_minutes": None,
                    "grace_minutes": None,
                    "overdue_by_minutes": 0,
                    "what": "Writes inventory snapshot into SQLite safely",
                    "mode": "auto",
                },
            ],
        }

    try:
        domains["rt_sales"] = _rt_sales_domain(now_utc, marketplace_id)
    except Exception as exc:  # pragma: no cover - defensive
        domains["rt_sales"] = {
            "title": "REAL-TIME SALES",
            "workers": [
                {
                    "key": "rt_sales_sync",
                    "name": "RT Sales Sync",
                    "status": "error",
                    "last_run_at_uae": None,
                    "last_run_utc": None,
                    "next_eligible_at_uae": None,
                    "next_run_utc": None,
                    "details": str(exc),
                    "message": str(exc),
                    "expected_interval_minutes": RT_SALES_EXPECTED_INTERVAL_MINUTES,
                    "grace_minutes": RT_SALES_GRACE_MINUTES,
                    "overdue_by_minutes": 0,
                    "what": "Ingests real-time sales and maintains hourly ledger",
                    "mode": "auto",
                }
            ],
        }

    try:
        domains["vendor_po"] = _vendor_po_domain()
    except Exception as exc:  # pragma: no cover - defensive
        domains["vendor_po"] = {
            "title": "VENDOR PO",
            "workers": [
                {
                    "key": "vendor_po_sync",
                    "name": "Vendor PO Sync",
                    "status": "error",
                    "last_run_at_uae": None,
                    "last_run_utc": None,
                    "next_eligible_at_uae": None,
                    "next_run_utc": None,
                    "details": str(exc),
                    "message": str(exc),
                    "expected_interval_minutes": None,
                    "grace_minutes": None,
                    "overdue_by_minutes": 0,
                    "what": "Refreshes Vendor POs when run manually",
                    "mode": "manual",
                }
            ],
        }

    try:
        domains["df_payments"] = _df_payments_domain(now_utc, marketplace_id)
    except Exception as exc:  # pragma: no cover - defensive
        domains["df_payments"] = {
            "title": "DF PAYMENTS",
            "workers": [
                {
                    "key": "df_payments_incremental",
                    "name": "DF Payments Incremental Scan",
                    "status": "error",
                    "last_run_at_uae": None,
                    "last_run_utc": None,
                    "next_eligible_at_uae": None,
                    "next_run_utc": None,
                    "details": str(exc),
                    "message": str(exc),
                    "what": "Fetches new DF orders (10m cooldown, DB-first)",
                    "mode": "auto",
                }
            ],
        }

    all_workers = _collect_workers(domains)
    waiting_count = sum(1 for w in all_workers if (w.get("status") or "").lower() in WAITING_STATUSES)
    error_count = sum(1 for w in all_workers if (w.get("status") or "").lower() == "error")
    overdue_count = sum(1 for w in all_workers if (w.get("status") or "").lower() in OVERDUE_STATUSES)

    if error_count:
        overall = "error"
    elif overdue_count:
        overall = "overdue"
    else:
        overall = "ok"

    return {
        "ok": error_count == 0 and overdue_count == 0,
        "checked_at_utc": now_utc.replace(microsecond=0).isoformat(),
        "checked_at_uae": _fmt_uae(now_utc),
        "summary": {
            "overall": overall,
            "waiting_count": waiting_count,
            "error_count": error_count,
            "overdue_count": overdue_count,
        },
        "domains": domains,
    }


def register_worker_status_routes(app: FastAPI) -> None:
    app.include_router(router)
