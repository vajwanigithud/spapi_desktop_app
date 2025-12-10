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


def fetch_latest_vendor_inventory_report_json(marketplace_id: str) -> dict:
    """
    Fetch the latest available weekly vendor inventory report as raw JSON.
    
    We request the latest completed week with a lag buffer so we don't hit
    "report data not yet available" errors.
    
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
    try:
        logger.info(f"[VendorInventory] Requesting GET_VENDOR_INVENTORY_REPORT for {marketplace_id}")
        
        today_utc = datetime.now(timezone.utc).date()
        
        # Amazon needs some time to prepare the weekly inventory dataset.
        # Use a 3-day lag buffer so we only ask for weeks that are fully processed.
        LAG_DAYS = 3
        
        # Latest end date we will ask for (today - lag)
        candidate_end = today_utc - timedelta(days=LAG_DAYS)
        
        # Our window is 7 days: [start, end]
        start_date = candidate_end - timedelta(days=6)
        end_date = candidate_end
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        logger.info(
            f"[VendorInventory] Using date range: {start_str} to {end_str} (lag={LAG_DAYS} days)"
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
            
            # Helper to safely get nested amount
            def get_amount(obj: Optional[Dict]) -> Optional[float]:
                if obj is None:
                    return None
                amt = obj.get("amount")
                return float(amt) if amt is not None else None
            
            row = {
                "marketplace_id": marketplace_id,
                "asin": item.get("asin", ""),
                "start_date": item.get("startDate", ""),
                "end_date": item.get("endDate", ""),
                
                # Core metrics
                "sellable_onhand_units": int(item.get("sellableOnHandInventoryUnits", 0) or 0),
                "sellable_onhand_cost": float(item.get("sellableOnHandInventoryCost", {}).get("amount", 0.0) or 0.0),
                "unsellable_onhand_units": item.get("unsellableOnHandInventoryUnits"),
                "unsellable_onhand_cost": get_amount(item.get("unsellableOnHandInventoryCost")),
                
                # Aging + unhealthy
                "aged90plus_sellable_units": item.get("aged90PlusDaysSellableInventoryUnits"),
                "aged90plus_sellable_cost": get_amount(item.get("aged90PlusDaysSellableInventoryCost")),
                "unhealthy_units": item.get("unhealthyInventoryUnits"),
                "unhealthy_cost": get_amount(item.get("unhealthyInventoryCost")),
                
                # Flow metrics
                "net_received_units": item.get("netReceivedInventoryUnits"),
                "net_received_cost": get_amount(item.get("netReceivedInventoryCost")),
                "open_po_units": item.get("openPurchaseOrderUnits"),
                "unfilled_customer_ordered_units": item.get("unfilledCustomerOrderedUnits"),
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
    
    If report data is not yet available, leaves existing snapshot untouched.
    Single call; no retries or loops (higher-level auto-sync decides when to call).
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
    
    Returns:
        Number of rows stored (0 if data not available yet, but existing snapshot kept)
    
    Raises:
        spapi_reports.SpApiQuotaError: Propagated from fetch step
        Exception: For other failures
    """
    try:
        logger.info(f"[VendorInventory] Starting refresh for {marketplace_id}")
        
        # Fetch report JSON
        report_json = fetch_latest_vendor_inventory_report_json(marketplace_id)
        
        # If Amazon says the data for this range isn't ready, keep existing snapshot.
        error_details = report_json.get("errorDetails")
        if error_details:
            msg = str(error_details)
            if "not yet available" in msg:
                logger.warning(
                    f"[VendorInventory] Report data not yet available for requested range; "
                    f"keeping existing snapshot. errorDetails={msg}"
                )
                # Do NOT call replace_vendor_inventory_snapshot()
                return 0
        
        # Extract latest week rows
        rows = extract_latest_week_inventory_by_asin(report_json, marketplace_id)
        
        # Store in DB (replaces all existing rows for this marketplace)
        db.replace_vendor_inventory_snapshot(conn, marketplace_id, rows)
        
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
    
    This will be used later by an API endpoint in Part 3.
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
    
    Returns:
        List of inventory snapshot dicts, sorted for UI display
    """
    try:
        rows = db.get_vendor_inventory_snapshot(conn, marketplace_id)
        
        # Sort by sellable units DESC, then ASIN ASC
        rows.sort(key=lambda r: (-r.get("sellable_onhand_units", 0), r.get("asin", "")))
        
        return rows
    except Exception as e:
        logger.error(f"[VendorInventory] Failed to get inventory snapshot for UI: {e}", exc_info=True)
        raise
