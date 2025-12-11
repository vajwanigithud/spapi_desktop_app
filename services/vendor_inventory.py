"""
Vendor Inventory Report Service
================================

Fetches GET_VENDOR_INVENTORY_REPORT from SP-API, extracts the latest week,
and stores a per-ASIN snapshot in SQLite.

Pattern: Reuses existing spapi_reports helpers (request_vendor_report, poll_vendor_report, download_vendor_report_document)
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from services import db
from services import spapi_reports

logger = logging.getLogger(__name__)


def _safe_amount_from_cost(cost_obj) -> float:
    """
    Safely extract the 'amount' field from a cost-like dict.
    Handles None, missing key, wrong type, etc.
    Returns 0.0 if anything is off.
    
    Example objects:
      {"amount": 123.45, "currencyCode": "AED"}
      None
    """
    if not cost_obj:
        return 0.0
    try:
        # Some APIs return {"amount": "..."} as string, some as number.
        return float(cost_obj.get("amount") or 0.0)
    except Exception:
        return 0.0


def fetch_latest_vendor_inventory_report_json(marketplace_id: str) -> dict:
    """
    Fetch the latest available weekly vendor inventory report as raw JSON.
    
    Requests the latest completed week (Sunday through Saturday) with a lag buffer
    so we don't hit "report data not yet available" errors.
    
    Uses the existing spapi_reports helpers to maintain consistency
    with other vendor report patterns.
    
    Args:
        marketplace_id: The marketplace ID (e.g., "A2VIGQ35RCS4UG" for UAE)
    
    Returns:
        The parsed JSON report as a dict
    
    Raises:
        spapi_reports.SpApiQuotaError: If SP-API returns quota exceeded
        Exception: For other failures
    """
    LAG_DAYS = 3  # Days to lag behind today (Amazon needs time to process data)
    
    try:
        logger.info(f"[VendorInventory] Requesting GET_VENDOR_INVENTORY_REPORT for {marketplace_id}")
        
        today_utc = datetime.now(timezone.utc).date()
        
        # Step 1: Latest candidate end date that is safely in the past
        candidate_end = today_utc - timedelta(days=LAG_DAYS)
        
        # Step 2: Move back to the most recent SATURDAY <= candidate_end
        # In Python: Monday=0, Tuesday=1, ..., Sunday=6
        # We want Saturday which is weekday=5
        offset_to_saturday = (candidate_end.weekday() - 5) % 7
        end_date = candidate_end - timedelta(days=offset_to_saturday)
        
        # Step 3: Start date is the SUNDAY of that same week (6 days before Saturday)
        start_date = end_date - timedelta(days=6)
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        # Verify we got Sunday-Saturday alignment
        assert start_date.weekday() == 6, f"start_date should be Sunday (weekday=6), got {start_date.weekday()}"
        assert end_date.weekday() == 5, f"end_date should be Saturday (weekday=5), got {end_date.weekday()}"
        
        logger.info(
            f"[VendorInventory] Using weekly date range: {start_str} (Sun) → {end_str} (Sat), lag={LAG_DAYS}d"
        )
        
        # Use existing report helper with WEEK period and date range
        report_id = spapi_reports.request_vendor_report(
            report_type="GET_VENDOR_INVENTORY_REPORT",
            params={"marketplaceIds": [marketplace_id]},
            data_start=datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc),
            data_end=datetime.combine(end_date, datetime.min.time(), tzinfo=timezone.utc),
            report_period="WEEK",
            selling_program="RETAIL",
            distributor_view="MANUFACTURING"
        )
        logger.info(f"[VendorInventory] Created report {report_id}")
        
        # Poll until done
        report_data = spapi_reports.poll_vendor_report(report_id, timeout_seconds=600)
        document_id = report_data.get("reportDocumentId")
        
        if not document_id:
            logger.warning(f"[VendorInventory] Report {report_id} has no document_id, returning empty dict")
            return {}
        
        logger.info(f"[VendorInventory] Poll completed, document_id={document_id}")
        
        # Download and decompress document (already parsed as JSON if applicable)
        report_content, expiration_info = spapi_reports.download_vendor_report_document(document_id)
        
        # The download function returns parsed JSON if it's valid JSON, otherwise bytes
        if isinstance(report_content, dict):
            report_json = report_content
        elif isinstance(report_content, list):
            # In case the top level is a list (shouldn't be for GET_VENDOR_INVENTORY_REPORT, but handle it)
            report_json = {"inventoryByAsin": report_content}
        else:
            logger.warning(f"[VendorInventory] Report content is not JSON-like, returning empty dict")
            return {}
        
        logger.info(f"[VendorInventory] Successfully downloaded and parsed report {report_id}")
        return report_json
    except spapi_reports.SpApiQuotaError as e:
        logger.error(f"[VendorInventory] QuotaExceeded fetching report: {e}")
        raise
    except Exception as e:
        logger.error(f"[VendorInventory] Failed to fetch inventory report for {marketplace_id}: {e}", exc_info=True)
        raise


def extract_latest_week_inventory_by_asin(report_json: dict, marketplace_id: str) -> List[Dict[str, Any]]:
    """
    Given the full GET_VENDOR_INVENTORY_REPORT JSON,
    return a list of rows (dict) for ONLY the latest week (max endDate)
    from inventoryByAsin.
    
    Args:
        report_json: The full report JSON from fetch_latest_vendor_inventory_report_json
        marketplace_id: The marketplace ID to include in each row
    
    Returns:
        List of dicts, each compatible with vendor_inventory_asin table schema
    """
    try:
        # Extract inventoryByAsin array
        inventory_by_asin = report_json.get("inventoryByAsin", [])
        
        if not inventory_by_asin:
            logger.info("[VendorInventory] No inventoryByAsin data in report")
            return []
        
        # Find latest end_date
        end_dates = [item.get("endDate") for item in inventory_by_asin if item.get("endDate")]
        if not end_dates:
            logger.warning("[VendorInventory] No endDate found in any inventory record")
            return []
        
        latest_end_date = max(end_dates)
        logger.info(f"[VendorInventory] Latest week endDate: {latest_end_date}")
        
        # Filter to latest week and build row dicts
        rows = []
        for item in inventory_by_asin:
            if item.get("endDate") != latest_end_date:
                continue
            
            asin = item.get("asin", "")
            
            # Log debug info if cost objects are null (helps troubleshoot data issues)
            if item.get("sellableOnHandInventoryCost") is None and asin:
                logger.debug(
                    f"[VendorInventory] ASIN {asin} has null sellableOnHandInventoryCost; treating as 0.0"
                )
            
            row = {
                "marketplace_id": marketplace_id,
                "asin": asin,
                "start_date": item.get("startDate", ""),
                "end_date": item.get("endDate", ""),
                
                # Core metrics
                "sellable_onhand_units": int(item.get("sellableOnHandInventoryUnits") or 0),
                "sellable_onhand_cost": _safe_amount_from_cost(item.get("sellableOnHandInventoryCost")),
                "unsellable_onhand_units": int(item.get("unsellableOnHandInventoryUnits") or 0),
                "unsellable_onhand_cost": _safe_amount_from_cost(item.get("unsellableOnHandInventoryCost")),
                
                # Aging + unhealthy
                "aged90plus_sellable_units": int(item.get("aged90PlusDaysSellableInventoryUnits") or 0),
                "aged90plus_sellable_cost": _safe_amount_from_cost(item.get("aged90PlusDaysSellableInventoryCost")),
                "unhealthy_units": int(item.get("unhealthyInventoryUnits") or 0),
                "unhealthy_cost": _safe_amount_from_cost(item.get("unhealthyInventoryCost")),
                
                # Flow metrics
                "net_received_units": int(item.get("netReceivedInventoryUnits") or 0),
                "net_received_cost": _safe_amount_from_cost(item.get("netReceivedInventoryCost")),
                "open_po_units": int(item.get("openPurchaseOrderUnits") or 0),
                "unfilled_customer_ordered_units": int(item.get("unfilledCustomerOrderedUnits") or 0),
                "vendor_confirmation_rate": item.get("vendorConfirmationRate"),
                "sell_through_rate": item.get("sellThroughRate"),
                
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            rows.append(row)
        
        logger.info(f"[VendorInventory] Extracted {len(rows)} ASINs for latest week")
        return rows
    except Exception as e:
        logger.error(f"[VendorInventory] Failed to extract latest week inventory: {e}", exc_info=True)
        raise


def refresh_vendor_inventory_snapshot(conn, marketplace_id: str) -> int:
    """
    Downloads GET_VENDOR_INVENTORY_REPORT, extracts latest week,
    stores snapshot into vendor_inventory_asin table for given marketplace.
    
    Only replaces the snapshot if we have actual data. If report has errorDetails
    or no inventoryByAsin, preserves the existing snapshot and returns 0.
    
    Uses caching to avoid re-downloading the same week if already cached.
    
    Single call; no retries or loops (higher-level auto-sync decides when to call).
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
    
    Returns:
        Number of rows stored (0 if data not available, cached, or empty; existing snapshot preserved)
    
    Raises:
        spapi_reports.SpApiQuotaError: Propagated from fetch step
        Exception: For other failures
    """
    LAST_INV_WEEK_KEY = "vendor_inventory_last_week_end"
    
    try:
        logger.info(f"[VendorInventory] Starting refresh for {marketplace_id}")
        
        # Compute the week we're about to request
        today_utc = datetime.now(timezone.utc).date()
        LAG_DAYS = 3
        candidate_end = today_utc - timedelta(days=LAG_DAYS)
        offset_to_saturday = (candidate_end.weekday() - 5) % 7
        end_date = candidate_end - timedelta(days=offset_to_saturday)
        week_end_str = end_date.isoformat()
        
        # Check if we already have this week cached
        try:
            last_week = db.get_app_kv(conn, LAST_INV_WEEK_KEY)
            if last_week == week_end_str:
                logger.info(
                    f"[VendorInventory] Skipping refresh for {marketplace_id}; "
                    f"snapshot for week_end={week_end_str} already stored (cached)"
                )
                # Return count of existing rows
                existing_rows = db.get_vendor_inventory_snapshot(conn, marketplace_id)
                return len(existing_rows)
        except Exception as e:
            logger.debug(f"[VendorInventory] Could not check cache: {e}; proceeding with fetch")
        
        # Fetch report JSON
        report_json = fetch_latest_vendor_inventory_report_json(marketplace_id)
        
        # Check for API-level errors (errorDetails in response)
        error_details = report_json.get("errorDetails")
        if error_details:
            msg = str(error_details)
            logger.warning(
                f"[VendorInventory] Report returned error; preserving existing snapshot. "
                f"errorDetails={msg}"
            )
            # Do NOT call replace_vendor_inventory_snapshot(); keep existing data
            # Do NOT update cache key when there's an error
            return 0
        
        # Extract latest week rows (returns [] if no inventoryByAsin)
        rows = extract_latest_week_inventory_by_asin(report_json, marketplace_id)
        
        # Only replace snapshot if we actually have rows
        if not rows:
            logger.warning(
                f"[VendorInventory] No usable inventory data for {marketplace_id}; "
                f"keeping previous snapshot"
            )
            # Do NOT call replace_vendor_inventory_snapshot(); keep existing data
            # Do NOT update cache key when there's no data
            return 0
        
        # Store in DB (replaces all existing rows for this marketplace)
        db.replace_vendor_inventory_snapshot(conn, marketplace_id, rows)
        
        # Update cache key to mark that we've successfully fetched this week
        try:
            db.set_app_kv(conn, LAST_INV_WEEK_KEY, week_end_str)
        except Exception as e:
            logger.warning(f"[VendorInventory] Could not update cache key: {e}")
        
        logger.info(f"[VendorInventory] Refresh complete for {marketplace_id}: {len(rows)} rows stored")
        return len(rows)
    except spapi_reports.SpApiQuotaError as e:
        logger.error(f"[VendorInventory] QuotaExceeded during refresh: {e}")
        raise
    except Exception as e:
        logger.error(f"[VendorInventory] Failed to refresh inventory snapshot for {marketplace_id}: {e}", exc_info=True)
        raise


def get_vendor_inventory_snapshot_for_ui(conn, marketplace_id: str) -> List[Dict[str, Any]]:
    """
    Thin wrapper that returns rows from db.get_vendor_inventory_snapshot()
    sorted by sellable_onhand_units DESC then ASIN ASC.
    Enriches each row with title and image_url from the catalog.
    
    This will be used by the API endpoint to return complete data to the UI.
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
    
    Returns:
        List of inventory snapshot dicts with title and image_url, sorted for UI display
    """
    try:
        rows = db.get_vendor_inventory_snapshot(conn, marketplace_id)
        
        if not rows:
            return []
        
        # Extract all ASINs and build enrichment map
        asins = [r.get("asin") for r in rows if r.get("asin")]
        
        if asins:
            try:
                # Query catalog for title and image_url
                placeholders = ", ".join(["?" for _ in asins])
                catalog_rows = conn.execute(
                    f"SELECT asin, title, image FROM spapi_catalog WHERE asin IN ({placeholders})",
                    asins
                ).fetchall()
                
                # Build lookup map
                # Note: catalog_rows are sqlite3.Row objects, so we convert to dict
                catalog_map = {}
                for catalog_row in catalog_rows:
                    catalog_dict = dict(catalog_row)
                    asin = catalog_dict.get("asin")
                    if asin:
                        catalog_map[asin] = {
                            "title": catalog_dict.get("title") or "",
                            "image_url": catalog_dict.get("image") or "",
                        }
            except Exception as e:
                logger.warning(f"[VendorInventory] Failed to enrich from catalog: {e}; continuing without enrichment")
                catalog_map = {}
        else:
            catalog_map = {}
        
        # Enrich each row with catalog data
        for row in rows:
            asin = row.get("asin")
            if asin and asin in catalog_map:
                row["title"] = catalog_map[asin].get("title", "")
                row["image_url"] = catalog_map[asin].get("image_url", "")
            else:
                row["title"] = row.get("title", "")
                row["image_url"] = ""
        
        # Sort by sellable units DESC, then ASIN ASC
        rows.sort(key=lambda r: (-r.get("sellable_onhand_units", 0), r.get("asin", "")))
        
        return rows
    except Exception as e:
        logger.error(f"[VendorInventory] Failed to get inventory snapshot for UI: {e}", exc_info=True)
        raise
