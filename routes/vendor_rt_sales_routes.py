from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, FastAPI, Query

from config import MARKETPLACE_IDS
from services import vendor_realtime_sales as vendor_rt_sales
from services.vendor_rt_sales_ledger import get_ledger_summary, get_worker_lock, list_ledger_rows

router = APIRouter(prefix="/api/vendor/rt-sales")
DEFAULT_MARKETPLACE_ID = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"


@router.get("/ledger")
def get_vendor_rt_sales_ledger(
    marketplace_id: str = Query(..., description="Marketplace ID"),
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    rows = list_ledger_rows(marketplace_id, limit)
    return {"ok": True, "rows": rows}


def _parse_iso_or_none(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


@router.get("/status")
def get_vendor_rt_sales_status(
    marketplace_id: Optional[str] = Query(None, description="Marketplace ID (defaults to primary)")
) -> dict:
    resolved_marketplace = marketplace_id or DEFAULT_MARKETPLACE_ID
    now_utc = datetime.now(timezone.utc)
    ledger_summary = get_ledger_summary(resolved_marketplace, now_utc=now_utc)
    lock_row = get_worker_lock(resolved_marketplace)
    expires_iso = lock_row.get("expires_at") if lock_row else None
    expires_dt = _parse_iso_or_none(expires_iso)
    worker_lock = {
        "held": bool(lock_row),
        "owner": lock_row.get("owner") if lock_row else None,
        "expires_utc": expires_iso,
        "stale": bool(lock_row) and (expires_dt is None or expires_dt <= now_utc),
    }

    cooldown_active = vendor_rt_sales.is_in_quota_cooldown(now_utc)
    cooldown_reason = "quota" if cooldown_active else "none"
    cooldown_until = None
    cooldown_until_dt = vendor_rt_sales.get_quota_cooldown_until()
    if cooldown_active and cooldown_until_dt:
        cooldown_until = cooldown_until_dt.isoformat()

    if not cooldown_active and worker_lock["held"] and not worker_lock["stale"]:
        cooldown_active = True
        cooldown_reason = "lock_busy"
        cooldown_until = worker_lock["expires_utc"]

    cooldown = {
        "active": cooldown_active,
        "reason": cooldown_reason if cooldown_active else "none",
        "until_utc": cooldown_until,
    }

    return {
        "ok": True,
        "marketplace_id": resolved_marketplace,
        "now_utc": now_utc.replace(microsecond=0).isoformat(),
        "cooldown": cooldown,
        "worker_lock": worker_lock,
        "ledger_summary": ledger_summary,
    }


def register_vendor_rt_sales_routes(app: FastAPI) -> None:
    app.include_router(router)
