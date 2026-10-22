"""Daily Vendor Inventory report service.

The Vendor Inventory Report is the app's single inventory baseline. Unlike the
hourly real-time feed, it contains one consistent reporting date for every ASIN.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from services import db, spapi_reports

logger = logging.getLogger(__name__)

REPORT_TYPE = "GET_VENDOR_INVENTORY_REPORT"
REPORT_PERIOD = "DAY"
SELLING_PROGRAM = "RETAIL"
DISTRIBUTOR_VIEW = "MANUFACTURING"
LAST_SUCCESSFUL_DATE_KEY = "vendor_inventory_last_successful_date"
LAST_REFRESH_AT_KEY = "vendor_inventory_last_refresh_utc"
INITIAL_REPORT_LOOKBACK_DAYS = 7


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_amount(value: Any) -> float:
    try:
        return float((value or {}).get("amount") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _report_date_range(report_date: date) -> Tuple[datetime, datetime]:
    return (
        datetime.combine(report_date, time.min, tzinfo=timezone.utc),
        datetime.combine(report_date, time.max.replace(microsecond=0), tzinfo=timezone.utc),
    )


def request_daily_inventory_report(marketplace_id: str, report_date: date) -> Dict[str, Any]:
    """Download one daily report without changing local inventory data."""
    start, end = _report_date_range(report_date)
    report_id = spapi_reports.request_vendor_report(
        report_type=REPORT_TYPE,
        params={"marketplaceIds": [marketplace_id]},
        data_start=start,
        data_end=end,
        report_period=REPORT_PERIOD,
        selling_program=SELLING_PROGRAM,
        distributor_view=DISTRIBUTOR_VIEW,
    )
    report_meta = spapi_reports.poll_vendor_report(report_id, timeout_seconds=600)
    document_id = report_meta.get("reportDocumentId")
    if report_meta.get("processingStatus") != "DONE" or not document_id:
        raise RuntimeError(f"Daily Vendor Inventory report was not ready: {report_meta.get('processingStatus') or 'unknown'}")
    content, _expiration = spapi_reports.download_vendor_report_document(document_id)
    if not isinstance(content, dict):
        raise RuntimeError("Vendor Inventory report was not JSON data")
    if content.get("errorDetails") or content.get("reportRequestError"):
        raise RuntimeError(f"Vendor Inventory report error: {content.get('errorDetails') or content.get('reportRequestError')}")
    return content


def _inventory_rows(report: Dict[str, Any], marketplace_id: str) -> List[Dict[str, Any]]:
    raw_rows = report.get("inventoryByAsin") or []
    specification = report.get("reportSpecification") or {}
    updated_at = specification.get("lastUpdatedDate") or datetime.now(timezone.utc).isoformat()
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        asin = str(item.get("asin") or "").strip().upper()
        if not asin or asin in seen:
            continue
        seen.add(asin)
        rows.append({
            "marketplace_id": marketplace_id,
            "asin": asin,
            "start_date": item.get("startDate") or specification.get("dataStartTime") or "",
            "end_date": item.get("endDate") or specification.get("dataEndTime") or "",
            "sellable_onhand_units": _safe_int(item.get("sellableOnHandInventoryUnits")),
            "sellable_onhand_cost": _safe_amount(item.get("sellableOnHandInventoryCost")),
            "unsellable_onhand_units": _safe_int(item.get("unsellableOnHandInventoryUnits")),
            "unsellable_onhand_cost": _safe_amount(item.get("unsellableOnHandInventoryCost")),
            "aged90plus_sellable_units": _safe_int(item.get("aged90PlusDaysSellableInventoryUnits")),
            "aged90plus_sellable_cost": _safe_amount(item.get("aged90PlusDaysSellableInventoryCost")),
            "unhealthy_units": _safe_int(item.get("unhealthyInventoryUnits")),
            "unhealthy_cost": _safe_amount(item.get("unhealthyInventoryCost")),
            "net_received_units": _safe_int(item.get("netReceivedInventoryUnits")),
            "net_received_cost": _safe_amount(item.get("netReceivedInventoryCost")),
            "open_po_units": _safe_int(item.get("openPurchaseOrderUnits")),
            "unfilled_customer_ordered_units": _safe_int(item.get("unfilledCustomerOrderedUnits")),
            "vendor_confirmation_rate": item.get("vendorConfirmationRate"),
            "sell_through_rate": item.get("sellThroughRate"),
            "updated_at": updated_at,
        })
    return rows


def _sales_30d_map(marketplace_id: str) -> Dict[str, int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    query = """
        SELECT asin, SUM(ordered_units) AS units
        FROM vendor_realtime_sales
        WHERE marketplace_id = ? AND hour_start_utc >= ?
        GROUP BY asin
    """
    with db.get_db_connection() as conn:
        source_rows = conn.execute(query, (marketplace_id, cutoff)).fetchall()
    return {str(row["asin"]).strip().upper(): _safe_int(row["units"]) for row in source_rows if row["asin"]}


def _decorate_with_catalog_and_sales(conn: Any, rows: List[Dict[str, Any]], marketplace_id: str) -> List[Dict[str, Any]]:
    sales = _sales_30d_map(marketplace_id)
    catalog: Dict[str, Dict[str, Any]] = {}
    asins = [row["asin"] for row in rows]
    for offset in range(0, len(asins), 400):
        chunk = asins[offset : offset + 400]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        for item in conn.execute(f"SELECT asin, title, image FROM spapi_catalog WHERE UPPER(asin) IN ({placeholders})", chunk).fetchall():
            catalog[str(item["asin"]).strip().upper()] = dict(item)
    for row in rows:
        meta = catalog.get(row["asin"], {})
        row["title"] = meta.get("title") or ""
        row["image_url"] = meta.get("image") or ""
        row["sales_30d"] = sales.get(row["asin"], 0)
    return rows


def refresh_vendor_inventory_snapshot(conn: Any, marketplace_id: str) -> Dict[str, Any]:
    """Fetch the newest available daily report and atomically replace the baseline."""
    today = datetime.now(timezone.utc).date()
    last_successful_value = db.get_app_kv(conn, LAST_SUCCESSFUL_DATE_KEY)

    # Once the first full baseline exists, use a small bounded fallback range
    # rather than repeatedly requesting a long historical window.
    try:
        last_successful_date = date.fromisoformat(last_successful_value) if last_successful_value else None
    except ValueError:
        last_successful_date = None

    if last_successful_date:
        # Prefer the freshest expected report and make at most three bounded
        # fallbacks. This catches up after downtime without an hourly burst.
        candidates = [today - timedelta(days=days_ago) for days_ago in range(1, 4)]
        candidates = [candidate for candidate in candidates if candidate > last_successful_date]
        if not candidates:
            return {
                "status": "up_to_date",
                "report_date": last_successful_date.isoformat(),
                "rows": len(db.get_vendor_inventory_snapshot(conn, marketplace_id)),
            }
    else:
        # Initial setup may encounter Amazon's normal reporting delay, so try a
        # small bounded range.
        candidates = [today - timedelta(days=days_ago) for days_ago in range(3, INITIAL_REPORT_LOOKBACK_DAYS + 1)]

    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            report = request_daily_inventory_report(marketplace_id, candidate)
            rows = _inventory_rows(report, marketplace_id)
            if not rows:
                raise RuntimeError("Vendor Inventory report contained no ASIN rows")
            db.replace_vendor_inventory_snapshot(conn, marketplace_id, rows)
            db.set_app_kv(conn, LAST_SUCCESSFUL_DATE_KEY, candidate.isoformat())
            db.set_app_kv(conn, LAST_REFRESH_AT_KEY, datetime.now(timezone.utc).isoformat())
            return {
                "status": "refreshed",
                "report_date": candidate.isoformat(),
                "last_updated_date": (report.get("reportSpecification") or {}).get("lastUpdatedDate"),
                "rows": len(rows),
            }
        except Exception as exc:
            last_error = exc
            logger.info("[VendorInventory] Daily report unavailable for %s: %s", candidate, exc)
    raise RuntimeError(f"No usable daily Vendor Inventory report found: {last_error}")


def get_vendor_inventory_snapshot_for_ui(conn: Any, marketplace_id: str) -> List[Dict[str, Any]]:
    # Data written by the retired hourly feed has no daily-report identity.
    # Never present it as the new daily baseline while the first full report is
    # still being obtained.
    if not db.get_app_kv(conn, LAST_SUCCESSFUL_DATE_KEY):
        return []
    rows = [dict(row) for row in db.get_vendor_inventory_snapshot(conn, marketplace_id)]
    rows.sort(key=lambda row: (-_safe_int(row.get("sellable_onhand_units")), row.get("asin") or ""))
    return _decorate_with_catalog_and_sales(conn, rows, marketplace_id)


def get_inventory_snapshot_metadata(conn: Any) -> Dict[str, Optional[str]]:
    return {
        "report_date": db.get_app_kv(conn, LAST_SUCCESSFUL_DATE_KEY),
        "refreshed_at": db.get_app_kv(conn, LAST_REFRESH_AT_KEY),
    }
