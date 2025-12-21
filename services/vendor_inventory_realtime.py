"""
Vendor Real-Time Inventory snapshot helper.

This module fetches GET_VENDOR_REAL_TIME_INVENTORY_REPORT on demand,
stores the raw rows + metadata on disk, and exposes helpers to read the
cached snapshot along with freshness info. It is intentionally isolated
from the weekly inventory logic so Prompt 1 can stand alone.
"""
# DB-FIRST: SQLite is the single source of truth.
# JSON files are debug/export only and must not be used for live state.

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.catalog_service import (
    record_catalog_asin_sources,
    seed_catalog_universe,
    spapi_catalog_status,
)
from services.db import (
    ensure_app_kv_table,
    ensure_vendor_inventory_table,
    get_app_kv,
    get_db_connection,
    replace_vendor_inventory_snapshot,
    set_app_kv,
)
from services.spapi_reports import (
    download_vendor_report_document,
    poll_vendor_report,
    request_vendor_report,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = ROOT / "vendor_realtime_inventory_snapshot.json"
SNAPSHOT_DB_KEY = "vendor_rt_inventory_snapshot"
DEFAULT_LOOKBACK_HOURS = 24
STALE_THRESHOLD_HOURS = 24
MIN_REFRESH_INTERVAL_MINUTES = 30
SALES_30D_LOOKBACK_DAYS = 30
COOLDOWN_HOURS = 1
COOLDOWN_KV_KEY = "vendor_rt_inventory_last_refresh_utc"
_PRUNE_META_DEFAULTS = {
    "prune_attempted": False,
    "prune_skipped_reason": "",
    "prune_min_keep_count": 0,
    "pruned_rows": 0,
    "prune_kept_count": 0,
    "prune_before_count": 0,
}

_CANDIDATE_ROW_KEYS = [
    "reportData",
    "realtimeInventoryByAsin",
    "inventoryByAsin",
    "inventory",
    "items",
    "data",
]


def _blank_snapshot() -> Dict[str, Any]:
    return {
        "generated_at": None,
        "marketplace_id": None,
        "report_start_time": None,
        "report_end_time": None,
        "items": [],
        "report_id": None,
        "document_id": None,
        "unique_count": 0,
        "duplicates_dropped": 0,
        "raw_row_count": 0,
        "raw_nonempty_asin_count": 0,
        "raw_unique_asin_count": 0,
        "collapsed_unique_asin_count": 0,
        "raw_sellable_sum_raw": 0,
        "normalized_sellable_sum": 0,
        "realtime_sellable_asins": 0,
        "realtime_sellable_units": 0,
        "catalog_asin_count": 0,
        "coverage_ratio": 0.0,
        "inventory_scope": "realtime_inventory_snapshot",
        "coverage_label": "Realtime coverage (ASINs present in snapshot vs catalog baseline)",
        "inventory_scope_explanation": "",
    }


def _coerce_snapshot_dict(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return _blank_snapshot()
    snapshot = dict(data)
    snapshot.setdefault("items", [])
    snapshot.setdefault("unique_count", len(snapshot["items"]))
    snapshot.setdefault("duplicates_dropped", 0)
    snapshot.setdefault("raw_row_count", 0)
    snapshot.setdefault("raw_nonempty_asin_count", 0)
    snapshot.setdefault("raw_unique_asin_count", 0)
    snapshot.setdefault("collapsed_unique_asin_count", snapshot.get("unique_count", len(snapshot["items"])))
    snapshot.setdefault("raw_sellable_sum_raw", 0)
    snapshot.setdefault("normalized_sellable_sum", 0)
    snapshot.setdefault("realtime_sellable_asins", len(snapshot["items"]))
    snapshot.setdefault("realtime_sellable_units", snapshot.get("normalized_sellable_sum", 0))
    snapshot.setdefault("catalog_asin_count", 0)
    snapshot.setdefault("coverage_ratio", 0.0)
    snapshot.setdefault("inventory_scope", "realtime_inventory_snapshot")
    snapshot.setdefault("coverage_label", "Realtime coverage (ASINs present in snapshot vs catalog baseline)")
    snapshot.setdefault("inventory_scope_explanation", "")
    return snapshot


def _read_snapshot(cache_path: Path) -> Dict[str, Any]:
    if not cache_path.exists():
        return _blank_snapshot()
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return _coerce_snapshot_dict(data)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[VendorRtInventory] Failed to read cache %s: %s", cache_path, exc)
        return _blank_snapshot()


def _write_snapshot(cache_path: Path, payload: Dict[str, Any]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[VendorRtInventory] Failed to write cache %s: %s", cache_path, exc)


def _load_snapshot_from_db() -> Dict[str, Any]:
    ensure_app_kv_table()
    try:
        with get_db_connection() as conn:
            raw = get_app_kv(conn, SNAPSHOT_DB_KEY)
    except Exception as exc:
        logger.error("[VendorRtInventory] Failed to read snapshot from SQLite: %s", exc)
        return _blank_snapshot()
    if not raw:
        return _blank_snapshot()
    try:
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("[VendorRtInventory] Invalid snapshot payload from SQLite: %s", exc)
        return _blank_snapshot()
    return _coerce_snapshot_dict(data)


def _persist_snapshot_to_db(payload: Dict[str, Any]) -> None:
    ensure_app_kv_table()
    serialized = json.dumps(payload, ensure_ascii=False)
    with get_db_connection() as conn:
        set_app_kv(conn, SNAPSHOT_DB_KEY, serialized)


def _load_sales_30d_map(marketplace_id: Optional[str]) -> Dict[str, int]:
    """
    Read the cached vendor_realtime_sales rows and roll up the trailing 30 days per ASIN.
    Falls back to an empty map if sqlite is unavailable so inventory never breaks.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=SALES_30D_LOOKBACK_DAYS)
    cutoff_iso = _iso(cutoff_dt)
    if not cutoff_iso:
        return {}

    query = """
        SELECT asin, SUM(ordered_units) AS total_units
        FROM vendor_realtime_sales
        WHERE hour_start_utc >= ?
    """
    params: List[Any] = [cutoff_iso]
    if marketplace_id:
        query += " AND marketplace_id = ?"
        params.append(marketplace_id)
    query += " GROUP BY asin"

    try:
        with get_db_connection() as conn:
            rows = conn.execute(query, params).fetchall()
    except Exception as exc:
        logger.warning(
            "[VendorRtInventory] Failed to load sales_30d map for %s: %s",
            marketplace_id or "default",
            exc,
        )
        return {}

    sales_map: Dict[str, int] = {}
    for row in rows:
        asin = str(row["asin"] or "").strip()
        if not asin:
            continue
        total_units = row["total_units"]
        try:
            sales_map[asin] = int(total_units or 0)
        except Exception:
            try:
                sales_map[asin] = int(float(total_units or 0))
            except Exception:
                sales_map[asin] = 0
    return sales_map


def load_sales_30d_map(marketplace_id: Optional[str]) -> Dict[str, int]:
    """
    Public helper so API routes can reuse the cached 30d sales map.
    """
    return _load_sales_30d_map(marketplace_id)


def decorate_items_with_sales(
    items: List[Dict[str, Any]],
    sales_map: Dict[str, int],
) -> None:
    for item in items or []:
        asin = str(item.get("asin") or "").strip()
        if not asin:
            item["sales_30d"] = 0
            continue
        value = sales_map.get(asin) or sales_map.get(asin.upper())
        try:
            item["sales_30d"] = int(value)
        except Exception:
            item["sales_30d"] = 0


def _get_catalog_asin_count() -> int:
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(DISTINCT asin) AS total FROM spapi_catalog WHERE asin IS NOT NULL AND asin <> ''"
            ).fetchone()
            if row and row["total"] is not None:
                return int(row["total"])
    except Exception as exc:
        logger.warning("[VendorRtInventory] Failed to query catalog DB: %s", exc)
    try:
        catalog = spapi_catalog_status()
        if isinstance(catalog, dict):
            return len(catalog)
    except Exception as exc:
        logger.warning("[VendorRtInventory] Failed to read catalog cache fallback: %s", exc)
    return 0


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = candidate.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo:
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return dt.replace(tzinfo=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _extract_rows(report_content: Any) -> List[Dict[str, Any]]:
    if isinstance(report_content, dict):
        if "errorDetails" in report_content:
            raise RuntimeError(f"SP-API returned errorDetails: {report_content['errorDetails']}")
        if "reportRequestError" in report_content:
            raise RuntimeError(f"SP-API returned reportRequestError: {report_content['reportRequestError']}")
        for key in _CANDIDATE_ROW_KEYS:
            value = report_content.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [report_content]
    if isinstance(report_content, list):
        return [row for row in report_content if isinstance(row, dict)]
    return []


def _normalize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        asin = str(row.get("asin") or "").strip()
        sellable = row.get("highlyAvailableInventory")
        try:
            sellable_int = int(sellable)
        except Exception:
            sellable_int = 0
        start_time = (
            row.get("startTime")
            or row.get("startDateTime")
            or row.get("startDate")
            or row.get("intervalStartTime")
            or None
        )
        end_time = (
            row.get("endTime")
            or row.get("endDateTime")
            or row.get("endDate")
            or row.get("intervalEndTime")
            or None
        )
        normalized.append(
            {
                "asin": asin,
                "sellable": sellable_int,
                "startTime": start_time,
                "endTime": end_time,
            }
        )
    return normalized


def _resolve_row_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
    end_fields = [
        "endTime",
        "endDateTime",
        "endDate",
        "intervalEndTime",
    ]
    start_fields = [
        "startTime",
        "startDateTime",
        "startDate",
        "intervalStartTime",
    ]
    for field in end_fields:
        dt = _parse_datetime(row.get(field))
        if dt:
            return dt
    for field in start_fields:
        dt = _parse_datetime(row.get(field))
        if dt:
            return dt
    return None


def _collapse_latest_per_asin(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    timestamps: Dict[str, Optional[datetime]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        asin = str(row.get("asin") or "").strip()
        if not asin:
            continue
        row_ts = _resolve_row_timestamp(row)
        existing = latest.get(asin)
        if not existing:
            latest[asin] = row
            timestamps[asin] = row_ts
            continue
        existing_ts = timestamps.get(asin)
        if row_ts and (not existing_ts or row_ts >= existing_ts):
            latest[asin] = row
            timestamps[asin] = row_ts
        elif not row_ts and not existing_ts:
            # keep existing if both missing timestamps
            continue
    return list(latest.values())


def _derive_report_window(
    rows: List[Dict[str, Any]],
    fallback_start: datetime,
    fallback_end: datetime,
) -> Tuple[Optional[str], Optional[str]]:
    start_candidates: List[datetime] = []
    end_candidates: List[datetime] = []
    for row in rows:
        start_dt = (
            _parse_datetime(row.get("startTime"))
            or _parse_datetime(row.get("startDateTime"))
            or _parse_datetime(row.get("startDate"))
            or _parse_datetime(row.get("intervalStartTime"))
        )
        end_dt = (
            _parse_datetime(row.get("endTime"))
            or _parse_datetime(row.get("endDateTime"))
            or _parse_datetime(row.get("endDate"))
            or _parse_datetime(row.get("intervalEndTime"))
        )
        if start_dt:
            start_candidates.append(start_dt)
        if end_dt:
            end_candidates.append(end_dt)

    start_dt = min(start_candidates) if start_candidates else fallback_start
    end_dt = max(end_candidates) if end_candidates else fallback_end
    return _iso(start_dt), _iso(end_dt)


def _decorate_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    items = snapshot.get("items") or []
    snapshot["items"] = items
    sales_map = _load_sales_30d_map(snapshot.get("marketplace_id"))
    for item in items:
        asin = str(item.get("asin") or "").strip()
        item["sales_30d"] = sales_map.get(asin) if asin else None
    unique_asins = {
        str(item.get("asin") or "").strip().upper()
        for item in items
        if isinstance(item, dict) and item.get("asin")
    }
    seeded = seed_catalog_universe(unique_asins)
    if seeded:
        logger.info(f"[CatalogUniverse] seeded {seeded} asins from realtime_inventory_snapshot")
    record_catalog_asin_sources(unique_asins, "realtime_inventory")
    normalized_sellable_sum = snapshot.get("normalized_sellable_sum")
    if not isinstance(normalized_sellable_sum, (int, float)):
        normalized_sellable_sum = sum(item.get("sellable", 0) for item in items)
        snapshot["normalized_sellable_sum"] = normalized_sellable_sum
    realtime_sellable_asins = snapshot.get("realtime_sellable_asins")
    if not isinstance(realtime_sellable_asins, int) or realtime_sellable_asins <= 0:
        realtime_sellable_asins = len(unique_asins)
        snapshot["realtime_sellable_asins"] = realtime_sellable_asins
    snapshot["realtime_sellable_units"] = snapshot.get("realtime_sellable_units") or normalized_sellable_sum
    unique_count = snapshot.get("unique_count")
    if not isinstance(unique_count, int) or unique_count <= 0:
        unique_count = len(items)
        snapshot["unique_count"] = unique_count
    snapshot["count"] = snapshot.get("count") or unique_count
    catalog_count = snapshot.get("catalog_asin_count")
    if not isinstance(catalog_count, int) or catalog_count < 0:
        catalog_count = 0
    snapshot["catalog_asin_count"] = catalog_count
    snapshot["coverage_ratio"] = round(realtime_sellable_asins / catalog_count, 3) if catalog_count > 0 else 0.0
    snapshot["inventory_scope"] = snapshot.get("inventory_scope") or "realtime_inventory_snapshot"
    if catalog_count > 0:
        pct = (realtime_sellable_asins / catalog_count) * 100
        snapshot["coverage_label"] = snapshot.get("coverage_label") or (
            f"Realtime coverage: {realtime_sellable_asins}/{catalog_count} ASINs ({pct:.1f}%)"
        )
    else:
        snapshot["coverage_label"] = snapshot.get("coverage_label") or "Realtime coverage: no catalog baseline"
    catalog_desc = (
        f"{catalog_count} catalog ASINs" if catalog_count > 0 else "no catalog baseline"
    )
    explanation = (snapshot.get("inventory_scope_explanation") or "").strip()
    if not explanation:
        snapshot["inventory_scope_explanation"] = (
            "Realtime snapshot is derived from GET_VENDOR_REAL_TIME_INVENTORY_REPORT and reflects the ASINs "
            "present in that report window. Vendor Central exports may include additional ASINs not present "
            "in this realtime report. Coverage compares "
            f"{realtime_sellable_asins} snapshot ASINs against {catalog_desc}."
        )
    generated_at_dt = _parse_datetime(snapshot.get("generated_at"))
    if generated_at_dt:
        age_seconds = int((datetime.now(timezone.utc) - generated_at_dt).total_seconds())
        snapshot["age_seconds"] = age_seconds
        snapshot["age_hours"] = round(age_seconds / 3600, 2)
    else:
        snapshot["age_seconds"] = None
        snapshot["age_hours"] = None
    snapshot["is_stale"] = is_snapshot_stale(snapshot)
    return snapshot


def _merge_prune_meta(refresh_meta: Dict[str, Any], prune_meta: Dict[str, Any]) -> Dict[str, Any]:
    for key, default in _PRUNE_META_DEFAULTS.items():
        if key in prune_meta and prune_meta.get(key) is not None:
            refresh_meta[key] = prune_meta[key]
        elif key not in refresh_meta or refresh_meta.get(key) is None:
            refresh_meta[key] = default
    return refresh_meta


def _materialize_rows_for_vendor_inventory(
    snapshot: Dict[str, Any],
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
) -> List[Dict[str, Any]]:
    marketplace_id = (snapshot.get("marketplace_id") or "").strip()
    report_start = _parse_datetime(snapshot.get("report_start_time"))
    report_end = _parse_datetime(snapshot.get("report_end_time"))
    generated_at = snapshot.get("generated_at") or _iso(datetime.now(timezone.utc))
    rows: List[Dict[str, Any]] = []
    for item in snapshot.get("items") or []:
        if not isinstance(item, dict):
            continue
        asin = (item.get("asin") or "").strip()
        if not asin:
            continue
        start_dt = _parse_datetime(
            item.get("startTime")
            or item.get("startDateTime")
            or item.get("startDate")
            or report_start
        )
        end_dt = _parse_datetime(
            item.get("endTime")
            or item.get("endDateTime")
            or item.get("endDate")
            or report_end
        )
        sellable_units = item.get("sellable")
        try:
            sellable_units = int(sellable_units or 0)
        except Exception:
            sellable_units = 0
        rows.append(
            {
                "marketplace_id": marketplace_id,
                "asin": asin,
                "start_date": window_start or _iso(start_dt) or snapshot.get("report_start_time") or generated_at,
                "end_date": window_end or _iso(end_dt) or snapshot.get("report_end_time") or generated_at,
                "sellable_onhand_units": sellable_units,
                "sellable_onhand_cost": 0.0,
                "unsellable_onhand_units": 0,
                "unsellable_onhand_cost": 0.0,
                "aged90plus_sellable_units": 0,
                "aged90plus_sellable_cost": 0.0,
                "unhealthy_units": 0,
                "unhealthy_cost": 0.0,
                "net_received_units": 0,
                "net_received_cost": 0.0,
                "open_po_units": 0,
                "unfilled_customer_ordered_units": 0,
                "vendor_confirmation_rate": None,
                "sell_through_rate": None,
                "updated_at": generated_at,
            }
        )
    return rows


def materialize_vendor_inventory_snapshot(snapshot: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    """
    Replace vendor_inventory_asin rows using realtime snapshot items.
    Returns prune metadata (dict) including row counts.
    """
    marketplace_id = (snapshot.get("marketplace_id") or "").strip()
    if not marketplace_id:
        logger.warning("[VendorRtInventory] Materialization skipped (%s): missing marketplace_id", source)
        return {}
    window_start_raw = (
        snapshot.get("report_start_time")
        or snapshot.get("start_date")
        or snapshot.get("window_start_utc")
    )
    window_end_raw = (
        snapshot.get("report_end_time")
        or snapshot.get("end_date")
        or snapshot.get("window_end_utc")
    )
    window_start_dt = _parse_datetime(window_start_raw)
    window_end_dt = _parse_datetime(window_end_raw)
    window_start = _iso(window_start_dt) if window_start_dt else None
    window_end = _iso(window_end_dt) if window_end_dt else None
    if not (window_start and window_end):
        logger.warning(
            "[VendorRtInventory] Snapshot window missing for %s (%s); using item timestamps",
            marketplace_id,
            source,
        )
        window_start = None
        window_end = None
    rows = _materialize_rows_for_vendor_inventory(snapshot, window_start=window_start, window_end=window_end)
    try:
        ensure_vendor_inventory_table()
        with get_db_connection() as conn:
            prune_meta = replace_vendor_inventory_snapshot(conn, marketplace_id, rows) or {}
        refresh_meta = _merge_prune_meta(snapshot.get("refresh") or {}, prune_meta)
        snapshot["refresh"] = refresh_meta
        logger.info(
            "[VendorRtInventory] Materialized realtime snapshot (%s) into vendor_inventory_asin: %s rows",
            source,
            len(rows),
        )
        return prune_meta
    except Exception as exc:
        logger.error(
            "[VendorRtInventory] Failed to materialize snapshot (%s) for %s: %s",
            source,
            marketplace_id,
            exc,
            exc_info=True,
        )
        return {}


def is_snapshot_stale(snapshot: Dict[str, Any], threshold_hours: int = STALE_THRESHOLD_HOURS) -> bool:
    generated_at = _parse_datetime(snapshot.get("generated_at"))
    if not generated_at:
        return True
    age = datetime.now(timezone.utc) - generated_at
    return age > timedelta(hours=threshold_hours)


def get_cached_realtime_inventory_snapshot(cache_path: Optional[Path] = None) -> Dict[str, Any]:
    snapshot = _load_snapshot_from_db()
    if snapshot.get("generated_at"):
        logger.info(
            "[VendorRtInventory] Loaded realtime snapshot from SQLite (generated_at=%s)",
            snapshot.get("generated_at"),
        )
        decorated = _decorate_snapshot(snapshot)
        materialize_vendor_inventory_snapshot(decorated, source="db_snapshot_load")
        return decorated

    path = cache_path or DEFAULT_CACHE_PATH
    if path.exists():
        logger.warning(
            "[VendorRtInventory][DB-FIRST] JSON snapshot read from %s to backfill empty SQLite state",
            path,
        )
        json_snapshot = _read_snapshot(path)
        if json_snapshot.get("generated_at"):
            try:
                _persist_snapshot_to_db(json_snapshot)
            except Exception as exc:
                logger.error("[VendorRtInventory] Failed to persist JSON snapshot to SQLite: %s", exc)
            decorated = _decorate_snapshot(json_snapshot)
            materialize_vendor_inventory_snapshot(decorated, source="json_bootstrap")
            return decorated

    logger.info("[VendorRtInventory] No cached realtime snapshot found; returning blank snapshot")
    return _decorate_snapshot(_blank_snapshot())


def refresh_realtime_inventory_snapshot(
    marketplace_id: str,
    *,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    cache_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Fetch the latest GET_VENDOR_REAL_TIME_INVENTORY_REPORT data for the given marketplace_id
    and persist it to disk so UI layers can switch over in a later prompt.
    """

    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be greater than zero")

    now = datetime.now(timezone.utc)
    cached_snapshot = _decorate_snapshot(_load_snapshot_from_db())
    ensure_app_kv_table()
    last_refresh_raw: Optional[str] = None
    try:
        with get_db_connection() as conn:
            last_refresh_raw = get_app_kv(conn, COOLDOWN_KV_KEY)
    except Exception as exc:
        logger.warning("[VendorRtInventory] Failed to read cooldown marker: %s", exc)
    last_refresh_dt = _parse_datetime(last_refresh_raw)
    if last_refresh_dt and now - last_refresh_dt < timedelta(hours=COOLDOWN_HOURS):
        cooldown_until = last_refresh_dt + timedelta(hours=COOLDOWN_HOURS)
        cooldown_snapshot = dict(cached_snapshot)
        cooldown_snapshot.setdefault("items", [])
        cooldown_snapshot.setdefault("marketplace_id", marketplace_id)
        cooldown_snapshot.update(
            {
                "cooldown_active": True,
                "cooldown_until_utc": _iso(cooldown_until),
                "refresh_in_progress": False,
                "status": "cooldown_active",
                "refresh_skipped": True,
                "marketplace_id": cooldown_snapshot.get("marketplace_id") or marketplace_id,
            }
        )
        return cooldown_snapshot

    path = cache_path or DEFAULT_CACHE_PATH

    # Throttle SP-API report creation if we fetched within the last window.
    generated_at = _parse_datetime(cached_snapshot.get("generated_at"))
    if (
        generated_at
        and now - generated_at < timedelta(minutes=MIN_REFRESH_INTERVAL_MINUTES)
        and cached_snapshot.get("items")
    ):
        throttled = dict(cached_snapshot)
        throttled["refresh_skipped"] = True
        return throttled
    data_end = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    data_start = data_end - timedelta(hours=lookback_hours)

    logger.info(
        "[VendorRtInventory] Requesting report %s from %s to %s",
        marketplace_id,
        data_start.isoformat(),
        data_end.isoformat(),
    )

    report_id = request_vendor_report(
        report_type="GET_VENDOR_REAL_TIME_INVENTORY_REPORT",
        params={"marketplaceIds": [marketplace_id]},
        data_start=data_start,
        data_end=data_end,
        selling_program="RETAIL",
    )
    report_meta = poll_vendor_report(report_id, timeout_seconds=600)
    document_id = report_meta.get("reportDocumentId")
    if not document_id:
        raise RuntimeError(f"Report {report_id} completed without reportDocumentId")

    report_content, expiration_info = download_vendor_report_document(document_id)
    raw_rows = _extract_rows(report_content)
    total_raw_rows = len(raw_rows)
    nonempty_asins: List[str] = []
    raw_sellable_sum_raw = 0
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        asin = str(row.get("asin") or "").strip()
        if asin:
            nonempty_asins.append(asin)
        value = row.get("highlyAvailableInventory")
        try:
            raw_sellable_sum_raw += int(value or 0)
        except Exception:
            continue
    raw_nonempty_asin_count = len(nonempty_asins)
    raw_unique_asin_count = len(set(nonempty_asins))
    logger.info(
        "[VendorRtInventory] raw_rows=%s nonempty_asin=%s unique_asin=%s",
        total_raw_rows,
        raw_nonempty_asin_count,
        raw_unique_asin_count,
    )
    report_start_time, report_end_time = _derive_report_window(raw_rows, data_start, data_end)
    collapsed_raw = _collapse_latest_per_asin(raw_rows)
    collapsed_unique_asin_count = len(collapsed_raw)
    rows = _normalize_rows(collapsed_raw)
    expires_at: Optional[str] = None
    if isinstance(expiration_info, dict):
        expires_at = (
            expiration_info.get("expiresAt")
            or expiration_info.get("expirationTime")
            or None
        )
    unique_count = len(rows)
    duplicates_dropped = max(0, raw_nonempty_asin_count - unique_count)
    normalized_sellable_sum = sum(item.get("sellable", 0) for item in rows)
    catalog_asin_count = _get_catalog_asin_count()
    coverage_ratio = round(unique_count / catalog_asin_count, 3) if catalog_asin_count > 0 else 0.0

    snapshot = {
        "generated_at": _iso(now),
        "marketplace_id": marketplace_id,
        "report_start_time": report_start_time,
        "report_end_time": report_end_time,
        "items": rows,
        "report_id": report_id,
        "document_id": document_id,
        "expires_at": expires_at,
        "count": unique_count,
        "unique_count": unique_count,
        "collapsed_unique_asin_count": unique_count,
        "duplicates_dropped": duplicates_dropped,
        "raw_row_count": total_raw_rows,
        "raw_nonempty_asin_count": raw_nonempty_asin_count,
        "raw_unique_asin_count": raw_unique_asin_count,
        "collapsed_unique_asin_count": collapsed_unique_asin_count,
        "raw_sellable_sum_raw": raw_sellable_sum_raw,
        "normalized_sellable_sum": normalized_sellable_sum,
        "realtime_sellable_asins": unique_count,
        "realtime_sellable_units": normalized_sellable_sum,
        "catalog_asin_count": catalog_asin_count,
        "coverage_ratio": coverage_ratio,
        "is_stale": False,
    }

    snapshot = _decorate_snapshot(snapshot)
    snapshot["refresh_skipped"] = False

    _persist_snapshot_to_db(snapshot)
    _write_snapshot(path, snapshot)
    prune_meta = materialize_vendor_inventory_snapshot(snapshot, source="refresh") or {}
    refresh_meta = _merge_prune_meta(snapshot.get("refresh") or {}, prune_meta)
    snapshot["refresh"] = refresh_meta
    try:
        ensure_app_kv_table()
        with get_db_connection() as conn:
            set_app_kv(conn, COOLDOWN_KV_KEY, _iso(now))
    except Exception as exc:
        logger.warning("[VendorRtInventory] Failed to persist cooldown marker: %s", exc)
    return snapshot


__all__ = [
    "DEFAULT_CACHE_PATH",
    "get_cached_realtime_inventory_snapshot",
    "refresh_realtime_inventory_snapshot",
    "is_snapshot_stale",
    "load_sales_30d_map",
    "decorate_items_with_sales",
    "materialize_vendor_inventory_snapshot",
]
