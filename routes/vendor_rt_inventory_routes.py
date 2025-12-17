from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from fastapi import APIRouter, FastAPI, HTTPException, Query, Response

from services.vendor_inventory_realtime import SALES_30D_LOOKBACK_DAYS
from services.vendor_rt_inventory_state import (
    DEFAULT_CATALOG_DB_PATH,
    get_checkpoint,
    get_refresh_metadata,
    get_state_max_end_time,
    get_state_rows,
)
from services.vendor_rt_inventory_sync import refresh_vendor_rt_inventory_singleflight

router = APIRouter()
LOGGER = logging.getLogger(__name__)

DEFAULT_MARKETPLACE_ID = "A2VIGQ35RCS4UG"


def _resolve_db_path(raw: Optional[str]) -> Path:
    return Path(raw or DEFAULT_CATALOG_DB_PATH)


def _chunked(seq: Sequence[str], size: int = 400) -> Iterable[Sequence[str]]:
    for idx in range(0, len(seq), size):
        yield seq[idx : idx + size]


def _load_catalog_metadata(asins: List[str], db_path: Path) -> Dict[str, Dict[str, Optional[str]]]:
    """
    Load catalog metadata (title + image_url) for ASINs in batches.

    NOTE: This assumes a cached catalog table exists. If it doesn't, we fail safely
    and return an empty map.
    """
    if not asins:
        return {}

    catalog: Dict[str, Dict[str, Optional[str]]] = {}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Default assumption used in earlier attempts. If your actual table differs,
            # we can adjust after you confirm the schema.
            table = "spapi_catalog"
            col_asin = "asin"
            col_title = "title"
            col_image = "image"

            for chunk in _chunked(asins):
                placeholders = ",".join(["?"] * len(chunk))
                query = f"""
                    SELECT {col_asin} AS asin, {col_title} AS title, {col_image} AS image
                    FROM {table}
                    WHERE {col_asin} IN ({placeholders})
                """
                rows = conn.execute(query, tuple(chunk)).fetchall()
                for row in rows:
                    asin = (row["asin"] or "").strip().upper()
                    if not asin:
                        continue
                    catalog[asin] = {
                        "title": row["title"],
                        "image_url": row["image"],
                    }
        finally:
            conn.close()
    except Exception as exc:
        LOGGER.warning("Failed to load catalog metadata: %s", exc)

    return catalog


def _load_sales_30d_map(
    asins: List[str],
    marketplace_id: str,
    db_path: Path,
) -> Dict[str, Optional[int]]:
    """
    Load sales_30d totals from vendor_realtime_sales cache for the given ASINs.
    Safe fallback: if table/DB missing, returns empty map and endpoint still works.
    """
    if not asins:
        return {}

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=SALES_30D_LOOKBACK_DAYS)).isoformat()
    sales_map: Dict[str, Optional[int]] = {}

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            for chunk in _chunked(asins):
                placeholders = ",".join(["?"] * len(chunk))
                query = """
                    SELECT asin, SUM(ordered_units) AS total_units
                    FROM vendor_realtime_sales
                    WHERE hour_start_utc >= ?
                """
                params: List[Any] = [cutoff_iso]

                if marketplace_id:
                    query += " AND marketplace_id = ?"
                    params.append(marketplace_id)

                query += f" AND asin IN ({placeholders}) GROUP BY asin"
                params.extend(chunk)

                rows = conn.execute(query, tuple(params)).fetchall()
                for row in rows:
                    asin = (row["asin"] or "").strip().upper()
                    if not asin:
                        continue
                    try:
                        sales_map[asin] = int(row["total_units"] or 0)
                    except Exception:
                        sales_map[asin] = None
        finally:
            conn.close()
    except Exception as exc:
        LOGGER.warning("Failed to load sales_30d map: %s", exc)

    return sales_map


def _decorate_inventory_items(
    items: List[Dict[str, Any]],
    marketplace_id: str,
    db_path: Path,
) -> List[Dict[str, Any]]:
    asin_list = sorted(
        {
            (item.get("asin") or "").strip().upper()
            for item in items
            if isinstance(item, dict) and item.get("asin")
        }
    )
    catalog_map = _load_catalog_metadata(asin_list, db_path)
    sales_map = _load_sales_30d_map(asin_list, marketplace_id, db_path)
    for item in items:
        asin = (item.get("asin") or "").strip().upper()
        meta = catalog_map.get(asin) or {}
        item["title"] = meta.get("title")
        item["image_url"] = meta.get("image_url")
        item["sales_30d"] = sales_map.get(asin)
    return items


def _load_inventory_snapshot(
    marketplace_id: str,
    limit: Optional[int],
    db_path: Path,
) -> Dict[str, Any]:
    as_of = get_checkpoint(marketplace_id, db_path=db_path)
    if not as_of:
        as_of = get_state_max_end_time(marketplace_id, db_path=db_path)
    items = get_state_rows(marketplace_id, limit, db_path)
    _decorate_inventory_items(items, marketplace_id, db_path)
    return {"as_of": as_of, "items": items}


@router.get("/api/vendor/rt-inventory")
def get_vendor_rt_inventory(
    marketplace_id: str = Query(DEFAULT_MARKETPLACE_ID),
    limit: Optional[int] = Query(None, ge=1),
    db: Optional[str] = Query(default=str(DEFAULT_CATALOG_DB_PATH)),
) -> Dict[str, Any]:
    db_path = _resolve_db_path(db)

    snapshot = _load_inventory_snapshot(marketplace_id, limit, db_path)
    refresh_meta = get_refresh_metadata(marketplace_id, db_path=db_path)

    return {
        "marketplace_id": marketplace_id,
        "as_of": snapshot["as_of"],
        "items": snapshot["items"],
        "source": "cache",
        "status": "ok",
        "refresh_in_progress": bool(refresh_meta.get("in_progress")),
        "refresh": refresh_meta,
    }


@router.post("/api/vendor/rt-inventory/refresh")
def refresh_vendor_rt_inventory(
    response: Response,
    marketplace_id: str = Query(DEFAULT_MARKETPLACE_ID),
    hours: int = Query(2, ge=1, le=24),
    limit: Optional[int] = Query(None, ge=1),
    db: Optional[str] = Query(default=str(DEFAULT_CATALOG_DB_PATH)),
) -> Dict[str, Any]:
    db_path = _resolve_db_path(db)
    try:
        refresh_result = refresh_vendor_rt_inventory_singleflight(
            marketplace_id,
            db_path=db_path,
            hours=hours,
        )
    except Exception as exc:
        LOGGER.error("Failed to refresh vendor RT inventory: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to refresh vendor RT inventory") from exc

    if refresh_result["status"] == "refresh_in_progress":
        response.status_code = 202

    snapshot = _load_inventory_snapshot(marketplace_id, limit, db_path)
    return {
        "marketplace_id": marketplace_id,
        "as_of": snapshot["as_of"],
        "items": snapshot["items"],
        "source": refresh_result.get("source", "cache"),
        "status": refresh_result["status"],
        "refresh_in_progress": bool(refresh_result["refresh"].get("in_progress")),
        "refresh": refresh_result["refresh"],
    }


def register_vendor_rt_inventory_routes(app: FastAPI) -> None:
    app.include_router(router)
