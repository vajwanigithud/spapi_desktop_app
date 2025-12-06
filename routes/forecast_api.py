import logging
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any

from fastapi import APIRouter

from services.db import CATALOG_DB_PATH
from services.forecast_sync import sync_all_forecast_sources

router = APIRouter(prefix="/api/forecast", tags=["forecast"])
logger = logging.getLogger("uvicorn.error")
forecast_logger = logging.getLogger("forecast_dashboard")


@router.get("/dashboard")
def get_forecast_dashboard():
    """
    Real-ish Forecast Dashboard endpoint using local SQLite data.
    READ-ONLY: aggregates sales history, inventory, forecast, and inbound POs to build a simple view.
    """
    now = datetime.utcnow().replace(microsecond=0)
    now_iso = now.isoformat() + "Z"
    thirty_days_ago = (now - timedelta(days=30)).date().isoformat()

    asins = set()
    sales_totals: Dict[str, float] = {}
    sales_dates_min = None
    sales_dates_max = None
    sales_row_count = 0

    inventory: Dict[str, Dict[str, Any]] = {}
    forecast_totals: Dict[str, float] = {}
    fg_dates_min = None
    fg_dates_max = None
    forecast_row_count = 0
    inventory_row_count = 0

    try:
        with sqlite3.connect(CATALOG_DB_PATH) as conn:
            # sales
            cur = conn.execute(
                """
                SELECT asin, sales_date, units
                FROM vendor_sales_history
                """
            )
            for asin, sales_date, units in cur.fetchall():
                if not asin:
                    continue
                sales_row_count += 1
                asins.add(asin)
                if sales_date:
                    if sales_dates_min is None or sales_date < sales_dates_min:
                        sales_dates_min = sales_date
                    if sales_dates_max is None or sales_date > sales_dates_max:
                        sales_dates_max = sales_date
                try:
                    if sales_date and sales_date >= thirty_days_ago:
                        sales_totals[asin] = sales_totals.get(asin, 0) + float(units or 0)
                except Exception:
                    pass

            # inventory
            cur = conn.execute(
                """
                SELECT asin, highly_available_inventory
                FROM vendor_rt_inventory
                """
            )
            for asin, qty in cur.fetchall():
                if not asin:
                    continue
                inventory_row_count += 1
                asins.add(asin)
                inventory[asin] = {"quantity": qty or 0}

            # forecast
            cur = conn.execute(
                """
                SELECT asin, p70_units, forecast_generation_date
                FROM vendor_forecast
                """
            )
            for asin, p70_units, fg_date in cur.fetchall():
                if not asin:
                    continue
                forecast_row_count += 1
                asins.add(asin)
                forecast_totals[asin] = forecast_totals.get(asin, 0) + float(p70_units or 0)
                if fg_date:
                    if fg_dates_min is None or fg_date < fg_dates_min:
                        fg_dates_min = fg_date
                    if fg_dates_max is None or fg_date > fg_dates_max:
                        fg_dates_max = fg_date

    except Exception as exc:
        logger.error(f"[ForecastDashboard] DB error: {exc}")
        # fallback to empty data but still respond

    forecast_data = forecast_totals
    inventory_data = inventory
    # Load catalog images for ASINs
    image_map: Dict[str, str] = {}
    asin_list = list(asins)
    if asin_list:
        try:
            with sqlite3.connect(CATALOG_DB_PATH) as conn:
                qmarks = ",".join(["?"] * len(asin_list))
                cur = conn.execute(
                    f"SELECT asin, image FROM spapi_catalog WHERE asin IN ({qmarks})",
                    asin_list,
                )
                for asin_val, img in cur.fetchall():
                    if asin_val:
                        image_map[asin_val] = img
        except Exception as exc:
            logger.error(f"[ForecastDashboard] Error loading catalog images: {exc}")

    forecast_logger.info(
        f"[Load] Loaded {sales_row_count} sales rows, "
        f"{forecast_row_count} forecast rows, "
        f"{inventory_row_count} inventory records"
    )

    # ----------------------------------------------
    # Load inbound POs from vendor_pos_cache.json (assume Pending/Acknowledged arrive in next 30d)
    # ----------------------------------------------
    inbound_30d: Dict[str, float] = {}
    try:
        pos_cache_path = Path(__file__).resolve().parent.parent / "vendor_pos_cache.json"
        tracker_path = Path(__file__).resolve().parent.parent / "po_tracker.json"
        tracker: Dict[str, Any] = {}
        if tracker_path.exists():
            try:
                tracker = json.load(open(tracker_path, "r", encoding="utf-8"))
                if not isinstance(tracker, dict):
                    tracker = {}
            except Exception:
                tracker = {}
        if pos_cache_path.exists():
            with open(pos_cache_path, "r", encoding="utf-8") as f:
                po_data = json.load(f)

            po_entries = po_data.get("items", [])

            for po in po_entries:
                po_num = po.get("purchaseOrderNumber")
                status = None
                if po_num and tracker:
                    entry = tracker.get(po_num)
                    if isinstance(entry, dict):
                        status = entry.get("status")
                status = status or po.get("_internalStatus") or po.get("_inhouseStatus") or po.get("inhouseStatus") or "Pending"
                status_l = status.lower() if isinstance(status, str) else ""
                # Include only Pending or Acknowledged; exclude Delivered/Closed/etc.
                if status_l not in {"pending", "acknowledged"}:
                    continue

                order_details = po.get("orderDetails", {}) or {}
                for item in order_details.get("items", []) or []:
                    asin = item.get("amazonProductIdentifier")
                    try:
                        qty = float(item.get("orderedQuantity", {}).get("amount", 0) or 0)
                    except Exception:
                        qty = 0
                    if asin and qty:
                        inbound_30d[asin] = inbound_30d.get(asin, 0) + qty
        else:
            logger.info("[Forecast] No vendor_pos_cache.json found")
    except Exception as exc:
        logger.error(f"[Forecast] Error loading inbound PO cache: {exc}")

    rows = []
    for asin in sorted(asins):
        last30 = sales_totals.get(asin, 0.0)
        avg_weekly = last30 / 4.0 if last30 else 0.0
        current_inv = inventory.get(asin, {}).get("quantity", 0) or 0
        p70 = forecast_totals.get(asin, 0.0)
        weeks_cover = None
        if avg_weekly > 0:
            weeks_cover = current_inv / avg_weekly if avg_weekly else None

        # risk rules
        if weeks_cover is None or weeks_cover == 0:
            risk = "OOS"
        elif weeks_cover < 2:
            risk = "HIGH"
        elif weeks_cover < 5:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        if risk == "HIGH":
            suggestion = "Urgent replenishment required"
        elif risk == "MEDIUM":
            suggestion = "Consider replenishment"
        elif risk == "LOW":
            suggestion = "Monitor - no action needed"
        else:
            suggestion = "Immediate PO required"

        next_inbound = inbound_30d.get(asin, 0)
        rows.append(
            {
                "asin": asin,
                "image": image_map.get(asin),
                "currentInventory": current_inv,
                "last30dUnits": last30,
                "avgWeeklyUnits": avg_weekly,
                "next30dInbound": next_inbound,
                "p70Demand": p70,
                "weeksOfCover": weeks_cover,
                "risk": risk,
                "suggestedAction": suggestion,
            }
        )

    meta = {
        "sourceStatus": {
            "salesHistory": {"status": "OK", "message": "Loaded"},
            "forecast": {"status": "OK", "message": "Loaded"},
            "inventory": {"status": "OK", "message": "Loaded"},
            "pos": (
                {"status": "OK", "message": "Loaded from vendor_pos_cache.json"}
                if inbound_30d
                else {"status": "INFO", "message": "No inbound PO data"}
            ),
        },
        "salesDataFrom": sales_dates_min,
        "salesDataThrough": sales_dates_max,
        "forecastGenerationDateMin": fg_dates_min,
        "forecastGenerationDateMax": fg_dates_max,
        "inventorySnapshotTime": now_iso,
    }

    logger.info(f"[ForecastDashboard] Served {len(rows)} rows")
    # ----------------------------------------------
    # Diagnostic logging (row counts, inbound totals)
    # ----------------------------------------------
    forecast_logger.info(
        f"[Dashboard] Sales history rows={sales_row_count}, "
        f"Forecast rows={forecast_row_count}, Inventory rows={inventory_row_count}, "
        f"Inbound ASINs={len(inbound_30d)}"
    )

    if inbound_30d:
        inbound_summary = ", ".join([f"{asin}:{qty}" for asin, qty in inbound_30d.items()])
        forecast_logger.info(f"[Dashboard] Inbound 30d totals \u2192 {inbound_summary}")
    else:
        forecast_logger.info("[Dashboard] No inbound POs found within 30 days window")

    return {"meta": meta, "rows": rows}


@router.post("/refresh-all")
def refresh_all_forecast_data():
    """
    Refresh forecast-related data:
    - Vendor sales history
    - Vendor forecasting report
    - Vendor real-time inventory

    Uses services.forecast_sync.sync_all_forecast_sources()
    to call the SP-API reports and update catalog.db.
    """
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    try:
        logger.info("[ForecastRefresh] Starting full forecast data sync")
        summary = sync_all_forecast_sources()
        status = summary.get("status", "ok")
        if status == "ok":
            logger.info("[ForecastRefresh] Forecast data sync completed successfully: %s", summary)
        elif status == "warning":
            logger.warning("[ForecastRefresh] Forecast data sync completed with warnings: %s", summary)
        else:
            logger.error("[ForecastRefresh] Forecast data sync completed with errors: %s", summary)
        return {
            "status": status,
            "summary": summary,
            "refreshedAt": now_iso,
        }
    except Exception as exc:
        msg = str(exc)
        logger.error(f"[ForecastRefresh] Failed: {msg}")
        if "quota" in msg.lower():
            code = 429
            reason = "quota_exceeded"
        elif "sync already running" in msg.lower():
            code = 409
            reason = "already_running"
        else:
            code = 500
            reason = "sync_failed"
        return {
            "status": "error",
            "error": reason,
            "message": msg,
            "refreshedAt": now_iso,
        }, code

