"""Real-time vendor inventory API routes."""
# DB-FIRST: SQLite is the single source of truth.
# JSON files are debug/export only and must not be used for live state.

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from fastapi import APIRouter, FastAPI

from services.catalog_images import attach_image_urls
from services.db import get_db_connection
from services.spapi_reports import SpApiQuotaError
from services.vendor_inventory_realtime import (
    DEFAULT_LOOKBACK_HOURS,
    decorate_items_with_sales,
    get_cached_realtime_inventory_snapshot,
    load_sales_30d_map,
    refresh_realtime_inventory_snapshot,
)
from services.vendor_rt_inventory_sync import refresh_vendor_rt_inventory_singleflight

router = APIRouter(prefix="/api/vendor-inventory/realtime")
logger = logging.getLogger(__name__)

MARKETPLACE_IDS: List[str] = [
    mp.strip()
    for mp in (os.getenv("MARKETPLACE_IDS") or os.getenv("MARKETPLACE_ID", "")).split(",")
    if mp.strip()
]
DEFAULT_MARKETPLACE_ID = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"
UAE_TZ = timezone(timedelta(hours=4))
CATALOG_CHUNK_SIZE = 400


def _chunked(seq: Sequence[str], size: int = CATALOG_CHUNK_SIZE) -> Iterable[Sequence[str]]:
    for idx in range(0, len(seq), size):
        yield seq[idx : idx + size]


def _load_catalog_metadata(asins: List[str]) -> Dict[str, Dict[str, Optional[str]]]:
    normalized = sorted(
        {
            (asin or "").strip().upper()
            for asin in asins
            if isinstance(asin, str) and asin.strip()
        }
    )
    if not normalized:
        return {}

    catalog: Dict[str, Dict[str, Optional[str]]] = {}
    try:
        with get_db_connection() as conn:
            for chunk in _chunked(normalized):
                placeholders = ",".join(["?"] * len(chunk))
                query = f"""
                    SELECT asin, title, image
                    FROM spapi_catalog
                    WHERE UPPER(asin) IN ({placeholders})
                """
                rows = conn.execute(query, tuple(chunk)).fetchall()
                for row in rows:
                    asin = str(row["asin"] or "").strip().upper()
                    if not asin:
                        continue
                    catalog[asin] = {
                        "title": row["title"],
                        "image_url": row["image"],
                    }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[VendorRtInventory] Failed to load catalog metadata: %s", exc)
    return catalog


def _decorate_snapshot_items(snapshot: Dict[str, Any]) -> None:
    items = snapshot.get("items") or []
    asin_list = [
        (item.get("asin") or "").strip().upper()
        for item in items
        if isinstance(item, dict) and item.get("asin")
    ]
    catalog_map = _load_catalog_metadata(asin_list)
    marketplace_id = snapshot.get("marketplace_id")
    try:
        sales_map = load_sales_30d_map(marketplace_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[VendorRtInventory] Failed to load sales_30d map for %s: %s", marketplace_id, exc)
        sales_map = {}
    decorate_items_with_sales(items, sales_map)
    for item in items:
        asin = (item.get("asin") or "").strip().upper()
        meta = catalog_map.get(asin) or {}
        if not item.get("title") and meta.get("title"):
            item["title"] = meta["title"]
        if not item.get("image_url") and meta.get("image_url"):
            item["image_url"] = meta["image_url"]
        if item.get("sales_30d") is None:
            item["sales_30d"] = sales_map.get(asin) or 0
    snapshot["items"] = items


def _parse_iso_to_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _compute_as_of_fields(snapshot: Dict[str, Any]) -> Dict[str, Optional[Any]]:
    raw_source = snapshot.get("generated_at") or snapshot.get("report_end_time")
    as_of_dt = _parse_iso_to_utc(raw_source)
    as_of_iso = as_of_dt.isoformat() if as_of_dt else raw_source
    as_of_uae = as_of_dt.astimezone(UAE_TZ).strftime("%Y-%m-%d %H:%M UAE") if as_of_dt else None
    stale_hours = None
    if as_of_dt:
        delta = datetime.now(timezone.utc) - as_of_dt
        stale_hours = round(max(delta.total_seconds() / 3600.0, 0.0), 2)
    return {
        "as_of_raw": raw_source,
        "as_of": as_of_iso,
        "as_of_uae": as_of_uae,
        "stale_hours": stale_hours,
    }


def _normalize_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    _decorate_snapshot_items(snapshot)
    items = snapshot.get("items") or []
    snapshot.setdefault("count", len(items))
    snapshot.setdefault("unique_count", snapshot.get("count"))
    snapshot.setdefault("duplicates_dropped", 0)
    snapshot.setdefault("raw_row_count", len(items))
    snapshot.setdefault("raw_nonempty_asin_count", len(items))
    snapshot.setdefault("raw_unique_asin_count", snapshot.get("unique_count"))
    snapshot.setdefault("collapsed_unique_asin_count", snapshot.get("unique_count"))
    snapshot.setdefault("normalized_sellable_sum", sum(item.get("sellable", 0) for item in items))
    snapshot.setdefault("realtime_sellable_asins", snapshot.get("unique_count"))
    snapshot.setdefault("realtime_sellable_units", snapshot.get("normalized_sellable_sum"))
    snapshot.setdefault("catalog_asin_count", 0)
    snapshot.setdefault("coverage_ratio", 0.0)
    snapshot.setdefault("inventory_scope", "realtime_inventory_snapshot")
    snapshot.setdefault("coverage_label", snapshot.get("inventory_scope"))
    snapshot.setdefault("inventory_scope_explanation", "")
    return snapshot


def _format_snapshot_response(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = _normalize_snapshot(dict(snapshot))
    items = snapshot.get("items") or []
    for item in items:
        if isinstance(item, dict) and item.get("image_url") and not item.get("imageUrl"):
            item["imageUrl"] = item.get("image_url")
    attach_image_urls(items)
    refresh_meta = dict(snapshot.get("refresh") or {})
    refresh_in_progress = bool(refresh_meta.get("in_progress"))
    computed = _compute_as_of_fields(snapshot)
    status = snapshot.get("status")
    if not status:
        status = "skipped" if snapshot.get("refresh_skipped") else "ok"

    return {
        "ok": True,
        "status": status,
        "marketplace_id": snapshot.get("marketplace_id", DEFAULT_MARKETPLACE_ID),
        "generated_at": snapshot.get("generated_at"),
        "report_start_time": snapshot.get("report_start_time"),
        "report_end_time": snapshot.get("report_end_time"),
        "age_seconds": snapshot.get("age_seconds"),
        "age_hours": snapshot.get("age_hours"),
        "is_stale": snapshot.get("is_stale"),
        "count": snapshot.get("count"),
        "items": items,
        "refresh_skipped": snapshot.get("refresh_skipped") or False,
        "refresh_in_progress": refresh_in_progress,
        "refresh": refresh_meta,
        "as_of_raw": computed["as_of_raw"],
        "as_of": computed["as_of"],
        "as_of_utc": computed["as_of"],
        "as_of_uae": computed["as_of_uae"],
        "stale_hours": computed["stale_hours"],
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
        "coverage_label": snapshot.get("coverage_label"),
        "inventory_scope": snapshot.get("inventory_scope"),
        "inventory_scope_explanation": snapshot.get("inventory_scope_explanation"),
        "source": snapshot.get("source"),
    }


def _build_snapshot_payload() -> Dict[str, Any]:
    snapshot = get_cached_realtime_inventory_snapshot()
    snapshot.setdefault("marketplace_id", DEFAULT_MARKETPLACE_ID)
    return _format_snapshot_response(snapshot)


def _build_health_payload() -> Dict[str, Any]:
    snapshot = get_cached_realtime_inventory_snapshot()
    snapshot.setdefault("marketplace_id", DEFAULT_MARKETPLACE_ID)
    computed = _compute_as_of_fields(snapshot)
    as_of_utc = computed.get("as_of") or snapshot.get("as_of_utc") or snapshot.get("as_of")
    as_of_uae = computed["as_of_uae"]
    age_seconds = snapshot.get("age_seconds")
    if age_seconds is None and as_of_utc:
        as_of_dt = _parse_iso_to_utc(as_of_utc)
        if as_of_dt:
            age_seconds = int(max((datetime.now(timezone.utc) - as_of_dt).total_seconds(), 0))
    age_hours = snapshot.get("age_hours")
    if age_hours is None and age_seconds is not None:
        age_hours = round(age_seconds / 3600.0, 2)
    if age_hours is None:
        age_hours = computed["stale_hours"]

    unique_asins = snapshot.get("unique_count")
    if not isinstance(unique_asins, int):
        items = snapshot.get("items") or []
        unique_asins = len(items)

    has_snapshot = bool(snapshot.get("generated_at"))

    payload: Dict[str, Any] = {
        "ok": has_snapshot,
        "marketplace_id": snapshot.get("marketplace_id", DEFAULT_MARKETPLACE_ID),
        "as_of_utc": as_of_utc,
        "as_of_uae": as_of_uae,
        "age_seconds": age_seconds,
        "age_hours": age_hours,
        "is_stale": bool(snapshot.get("is_stale", True)),
        "unique_asins": unique_asins,
    }
    if not has_snapshot:
        payload["reason"] = "no_snapshot"
        payload["is_stale"] = True
        payload["age_seconds"] = None
        payload["age_hours"] = None
    return payload


def _refresh_snapshot_payload() -> Dict[str, Any]:
    marketplace_id = DEFAULT_MARKETPLACE_ID

    def _refresh_singleflight_callable(marketplace_id: str, **_kwargs: Any) -> Dict[str, Any]:
        # Preserve existing report logic while letting the single-flight guard orchestrate execution.
        return refresh_realtime_inventory_snapshot(
            marketplace_id,
            lookback_hours=DEFAULT_LOOKBACK_HOURS,
        )

    result = refresh_vendor_rt_inventory_singleflight(
        marketplace_id,
        hours=DEFAULT_LOOKBACK_HOURS,
        sync_callable=_refresh_singleflight_callable,
    )

    try:
        snapshot = get_cached_realtime_inventory_snapshot(materialize=True)
    except TypeError:
        # Test stubs may not accept the optional materialize kwarg; fall back to default behavior.
        snapshot = get_cached_realtime_inventory_snapshot()
    snapshot.setdefault("marketplace_id", marketplace_id)

    refresh_meta = result.get("refresh") or {}
    snapshot["refresh"] = refresh_meta
    if result.get("status"):
        snapshot["status"] = result["status"]
    if result.get("source"):
        snapshot["source"] = result["source"]
    if result.get("status") == "fresh_skipped":
        snapshot["refresh_skipped"] = True

    return _format_snapshot_response(snapshot)


@router.get("/snapshot")
def get_realtime_inventory_snapshot() -> Dict[str, Any]:
    """
    Return the cached GET_VENDOR_REAL_TIME_INVENTORY_REPORT snapshot + freshness metadata.
    """
    return _build_snapshot_payload()


@router.get(
    "/health",
    summary="Realtime snapshot health",
    description="Returns freshness metadata for the realtime inventory snapshot without loading the full item payload.",
)
def get_realtime_inventory_health() -> Dict[str, Any]:
    """
    Lightweight health check for realtime inventory freshness.
    """
    return _build_health_payload()


@router.post("/refresh")
def refresh_realtime_inventory() -> Dict[str, Any]:
    """
    Trigger a fresh GET_VENDOR_REAL_TIME_INVENTORY_REPORT download.
    """
    try:
        return _refresh_snapshot_payload()
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
