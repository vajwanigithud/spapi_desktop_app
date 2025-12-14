"""Real-time vendor inventory API routes (Prompt 1 backend scaffolding)."""

from __future__ import annotations

import logging
import os
from typing import List

from fastapi import APIRouter, FastAPI

from services.spapi_reports import SpApiQuotaError
from services.vendor_inventory_realtime import (
    get_cached_realtime_inventory_snapshot,
    refresh_realtime_inventory_snapshot,
)

router = APIRouter(prefix="/api/vendor-inventory/realtime")
logger = logging.getLogger(__name__)

MARKETPLACE_IDS: List[str] = [
    mp.strip()
    for mp in (os.getenv("MARKETPLACE_IDS") or os.getenv("MARKETPLACE_ID", "")).split(",")
    if mp.strip()
]
DEFAULT_MARKETPLACE_ID = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"


def _format_snapshot_response(snapshot: dict) -> dict:
    return {
        "status": "ok",
        "generated_at": snapshot.get("generated_at"),
        "report_start_time": snapshot.get("report_start_time"),
        "report_end_time": snapshot.get("report_end_time"),
        "age_seconds": snapshot.get("age_seconds"),
        "age_hours": snapshot.get("age_hours"),
        "is_stale": snapshot.get("is_stale"),
        "count": snapshot.get("count"),
        "items": snapshot.get("items") or [],
        "refresh_skipped": snapshot.get("refresh_skipped") or False,
        "marketplace_id": snapshot.get("marketplace_id", DEFAULT_MARKETPLACE_ID),
        "unique_count": snapshot.get("unique_count"),
        "duplicates_dropped": snapshot.get("duplicates_dropped"),
        "raw_row_count": snapshot.get("raw_row_count"),
        "raw_nonempty_asin_count": snapshot.get("raw_nonempty_asin_count"),
        "raw_unique_asin_count": snapshot.get("raw_unique_asin_count"),
        "collapsed_unique_asin_count": snapshot.get("collapsed_unique_asin_count"),
        "raw_sellable_sum_raw": snapshot.get("raw_sellable_sum_raw"),
        "normalized_sellable_sum": snapshot.get("normalized_sellable_sum"),
        "realtime_sellable_asins": snapshot.get("realtime_sellable_asins"),
        "realtime_sellable_units": snapshot.get("realtime_sellable_units"),
        "catalog_asin_count": snapshot.get("catalog_asin_count"),
        "coverage_ratio": snapshot.get("coverage_ratio"),
    }


@router.get("/snapshot")
def get_realtime_inventory_snapshot() -> dict:
    """
    Return the cached GET_VENDOR_REAL_TIME_INVENTORY_REPORT snapshot + freshness metadata.
    """
    snapshot = get_cached_realtime_inventory_snapshot()
    snapshot.setdefault("marketplace_id", DEFAULT_MARKETPLACE_ID)
    return _format_snapshot_response(snapshot)


@router.post("/refresh")
def refresh_realtime_inventory() -> dict:
    """
    Trigger a fresh GET_VENDOR_REAL_TIME_INVENTORY_REPORT download.
    """
    marketplace_id = DEFAULT_MARKETPLACE_ID
    try:
        snapshot = refresh_realtime_inventory_snapshot(marketplace_id)
        snapshot.setdefault("marketplace_id", marketplace_id)
        return _format_snapshot_response(snapshot)
    except SpApiQuotaError as exc:
        logger.warning("[VendorRtInventory] Quota exceeded while refreshing: %s", exc)
        return {
            "status": "quota_error",
            "error": str(exc),
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("[VendorRtInventory] Refresh failed: %s", exc, exc_info=True)
        return {
            "status": "error",
            "error": str(exc),
        }


def register_vendor_inventory_realtime_routes(app: FastAPI) -> None:
    """Mount the router on the FastAPI app."""
    app.include_router(router)
