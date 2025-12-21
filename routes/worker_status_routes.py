from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI

from config import MARKETPLACE_IDS
from services import vendor_inventory_realtime as rt_inventory
from services import vendor_realtime_sales as rt_sales
from services.db import ensure_app_kv_table, get_app_kv, get_db_connection
from services.vendor_po_status_store import get_vendor_po_status_payload
from services.vendor_rt_inventory_state import get_refresh_metadata
from services.vendor_rt_sales_ledger import get_ledger_summary, get_worker_lock

router = APIRouter()
UAE_TZ = timezone(timedelta(hours=4))
WAITING_STATUSES = {"cooldown", "locked"}
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


def _inventory_domain(now_utc: datetime, marketplace_id: str) -> Dict[str, Any]:
    workers: List[Dict[str, Any]] = []
    status = "ok"
    details: Optional[str] = None
    cooldown_until_dt: Optional[datetime] = None
    last_run_iso: Optional[str] = None
    refresh_meta: Dict[str, Any] = {}

    try:
        ensure_app_kv_table()
        with get_db_connection() as conn:
            last_refresh_raw = get_app_kv(conn, rt_inventory.COOLDOWN_KV_KEY)
    except Exception as exc:
        last_refresh_raw = None
        details = f"Cooldown read failed: {exc}"

    last_refresh_dt = _parse_iso_datetime(last_refresh_raw) if last_refresh_raw else None
    if last_refresh_dt:
        last_run_iso = last_refresh_dt.isoformat()
        cooldown_until_dt = last_refresh_dt + timedelta(hours=getattr(rt_inventory, "COOLDOWN_HOURS", 1))
        if cooldown_until_dt > now_utc:
            status = "cooldown"

    try:
        refresh_meta = get_refresh_metadata(marketplace_id)
    except Exception as exc:  # pragma: no cover - defensive
        refresh_meta = {}
        details = details or f"Refresh metadata unavailable: {exc}"

    refresh_last_finished = refresh_meta.get("last_refresh_finished_at") if isinstance(refresh_meta, dict) else None
    refresh_status = (refresh_meta or {}).get("last_refresh_status") if isinstance(refresh_meta, dict) else None
    refresh_error = (refresh_meta or {}).get("last_error") if isinstance(refresh_meta, dict) else None
    refresh_in_progress = bool((refresh_meta or {}).get("in_progress"))

    if refresh_last_finished:
        last_run_iso = refresh_last_finished

    if refresh_status == "FAILED":
        status = "error"
        details = refresh_error or "Last refresh failed"

    if refresh_in_progress and status == "ok":
        status = "locked"
        details = "Refresh in progress"

    if status == "cooldown" and cooldown_until_dt:
        details = details or f"Cooldown until {_fmt_uae(cooldown_until_dt)}"

    workers.append(
        {
            "key": "rt_inventory_refresh",
            "name": "Realtime Inventory Refresh",
            "status": status,
            "last_run_at_uae": _fmt_uae(last_run_iso),
            "next_eligible_at_uae": _fmt_uae(cooldown_until_dt) if status == "cooldown" else None,
            "details": details,
            "what": "Fetches Amazon RT inventory and stores snapshot",
        }
    )

    materializer_status = "ok" if status != "error" else "error"
    materializer_details = None
    if refresh_status == "FAILED":
        materializer_details = refresh_error or "Last materialization failed"
    workers.append(
        {
            "key": "inventory_materializer",
            "name": "Inventory Materializer",
            "status": materializer_status,
            "last_run_at_uae": _fmt_uae(last_run_iso),
            "next_eligible_at_uae": None,
            "details": materializer_details,
            "what": "Writes inventory snapshot into SQLite safely",
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
    except Exception:
        cooldown_active = False
        cooldown_until_dt = None

    status = "ok"
    details: Optional[str] = None
    next_eligible_dt: Optional[datetime] = None

    if cooldown_active:
        status = "cooldown"
        next_eligible_dt = cooldown_until_dt
        details = "Quota cooldown active"
    elif lock_row:
        if lock_stale:
            status = "error"
            details = "Worker lock stale"
        else:
            status = "locked"
            next_eligible_dt = lock_expires_dt
            owner = lock_row.get("owner")
            if owner:
                details = f"Lock owner: {owner}"
    elif ledger_summary.get("failed"):
        status = "error"
        details = "Failed ledger hours present"

    last_run_iso = ledger_summary.get("last_applied_hour_utc")
    if not next_eligible_dt and last_run_iso:
        last_dt = _parse_iso_datetime(last_run_iso)
        if last_dt:
            next_eligible_dt = last_dt + timedelta(minutes=15)

    workers.append(
        {
            "key": "rt_sales_sync",
            "name": "RT Sales Sync",
            "status": status,
            "last_run_at_uae": _fmt_uae(last_run_iso),
            "next_eligible_at_uae": _fmt_uae(next_eligible_dt),
            "details": details,
            "what": "Ingests real-time sales and maintains hourly ledger",
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
            "next_eligible_at_uae": None,
            "details": "Manual / on-demand",
            "what": "Refreshes Vendor POs when run manually",
        }
    )

    return {"title": "VENDOR PO", "workers": workers}


def _collect_workers(domains: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_workers: List[Dict[str, Any]] = []
    for domain in domains.values():
        workers = domain.get("workers") if isinstance(domain, dict) else None
        if workers:
            all_workers.extend([w for w in workers if isinstance(w, dict)])
    return all_workers


@router.get("/api/workers/status")
def get_worker_status() -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
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
                    "next_eligible_at_uae": None,
                    "details": str(exc),
                    "what": "Fetches Amazon RT inventory and stores snapshot",
                },
                {
                    "key": "inventory_materializer",
                    "name": "Inventory Materializer",
                    "status": "error",
                    "last_run_at_uae": None,
                    "next_eligible_at_uae": None,
                    "details": str(exc),
                    "what": "Writes inventory snapshot into SQLite safely",
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
                    "next_eligible_at_uae": None,
                    "details": str(exc),
                    "what": "Ingests real-time sales and maintains hourly ledger",
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
                    "next_eligible_at_uae": None,
                    "details": str(exc),
                    "what": "Refreshes Vendor POs when run manually",
                }
            ],
        }

    all_workers = _collect_workers(domains)
    waiting_count = sum(1 for w in all_workers if (w.get("status") or "").lower() in WAITING_STATUSES)
    error_count = sum(1 for w in all_workers if (w.get("status") or "").lower() == "error")
    overall = "error" if error_count else ("waiting" if waiting_count else "ok")

    return {
        "ok": error_count == 0,
        "checked_at_utc": now_utc.replace(microsecond=0).isoformat(),
        "checked_at_uae": _fmt_uae(now_utc),
        "summary": {
            "overall": overall,
            "waiting_count": waiting_count,
            "error_count": error_count,
        },
        "domains": domains,
    }


def register_worker_status_routes(app: FastAPI) -> None:
    app.include_router(router)
