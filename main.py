# ================================================================
#  IMPORTANT INSTRUCTIONS FOR CODEX — DO NOT MODIFY OR REMOVE
# ================================================================
#
# This backend is intentionally minimal. Codex must NOT refactor it,
# must NOT change endpoint paths, and must NOT reintroduce any
# Forecast/Google Sheets logic.
#
# -------------------------------
# HARD REQUIREMENTS FOR CODEX
# -------------------------------
#
# 1. DO NOT modify the following endpoint paths OR their response
#    structures in any way:
#
#       GET  /api/vendor-pos
#       GET  /api/vendor-pos/{po_number}
#       GET  /api/catalog/asins
#       POST /api/catalog/fetch/{asin}
#       POST /api/catalog/fetch-all
#       GET  /api/catalog/item/{asin}
#
# 2. /api/vendor-pos MUST:
#       - Read ONLY vendor_pos_cache.json.
#       - Normalize using normalize_pos_entries().
#       - Filter POs where purchaseOrderDate >= 2025-10-01.
#       - Sort by purchaseOrderDate DESC (newest first).
#       - Return JSON: { "items": [...], "source": "cache" }.
#
#    DO NOT add Vendor SP-API calls inside this endpoint.
#
# 3. parse_po_date(po) MUST read from:
#       po["purchaseOrderDate"]   (top-level key)
#    and may optionally fall back to:
#       po["orderDetails"]["purchaseOrderDate"]
#
#    DO NOT remove support for top-level purchaseOrderDate.
#    DO NOT break the date filtering behaviour.
#
# 4. Catalog enrichment MUST use ONLY the local SQLite DB tables:
#       spapi_catalog
#       spapi_catalog_meta
#
#    DO NOT introduce Google Sheets APIs.
#    DO NOT reintroduce Forecast_Dashboard or any Forecast logic.
#    DO NOT import googleapiclient or google.oauth libraries.
#
# 5. DO NOT modify normalize_pos_entries() or extract_asins_from_pos()
#    except when I explicitly request a targeted bug fix.
#
# 6. DO NOT flatten, restructure, or rename any JSON fields belonging
#    to POs or catalog items.
#
# 7. DO NOT introduce any new database tables without my explicit request.
#
# 8. You MAY add helper functions ONLY if they do not affect existing
#    endpoint behaviour.
#
# -------------------------------
# SUMMARY
# -------------------------------
# This backend is stable and correct. Codex must apply ONLY changes that
# I specifically request, and must NOT rewrite or "optimize" the file.
# ================================================================

# Wave 2A module split plan (helpers move, routes stay here):
# - services/json_cache.py       # JSON file I/O (vendor_pos_cache, asin cache, trackers, oos)
# - services/catalog_service.py  # Catalog DB helpers (init, upsert, status, barcode setters)
# - services/oos_service.py      # OOS helpers (upsert/seed utilities)
# - services/picklist_service.py # Picklist aggregation + PDF generation helpers
# - (future) vendor/debug helpers remain inline until further split

# =============================================
#  SP-API DESKTOP APP - MINIMAL ENTRYPOINT
# =============================================

import asyncio
import json
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from io import BytesIO, StringIO
import csv
import time
from urllib.parse import parse_qsl
from endpoint_presets import ENDPOINT_PRESETS
from services.utils_barcodes import is_asin, normalize_barcode, is_valid_ean13
from services import db_repos
from services.json_cache import (
    load_vendor_pos_cache,
    save_vendor_pos_cache,
    load_asin_cache,
    save_asin_cache,
    load_po_tracker,
    save_po_tracker,
    load_oos_state,
    save_oos_state,
)
from services.catalog_service import (
    init_catalog_db,
    upsert_spapi_catalog,
    spapi_catalog_status,
    update_catalog_barcode,
    set_catalog_barcode_if_absent,
    get_catalog_entry,
    parse_catalog_payload,
    list_catalog_indexes,
)
import services.oos_service as oos_service
import services.picklist_service as picklist_service
from services.async_utils import run_single_arg
from services.vendor_notifications import (
    get_po_notification_flags,
    mark_po_as_needing_refresh,
    clear_po_refresh_flag,
    log_vendor_notification,
    process_vendor_notification,
    get_recent_notifications,
)
from services.perf import time_block, get_recent_timings
import services.vendor_realtime_sales as vendor_realtime_sales_service
from services import spapi_reports
from services.vendor_inventory import (
    refresh_vendor_inventory_snapshot,
    get_vendor_inventory_snapshot_for_ui,
)
from routes.nicelabel_routes import register_nicelabel_routes

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

import uvicorn
import requests
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from auth.spapi_auth import SpApiAuth
from pydantic import BaseModel

# --- Logging configuration ---
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE_PATH = LOG_DIR / "spapi_backend.log"
SPAPI_TESTER_LOG_PATH = LOG_DIR / "spapi_tester.log"

log_level = os.getenv("SPAPI_LOG_LEVEL", "INFO").upper()

root_logger = logging.getLogger()
logger = root_logger
if not root_logger.handlers:
    root_logger.setLevel(log_level)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=5_000_000,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

logging.getLogger("uvicorn").propagate = True
logging.getLogger("uvicorn.error").propagate = True
logging.getLogger("uvicorn.access").propagate = True
# --- End logging configuration ---

tester_logger = logging.getLogger("spapi_tester")
if not tester_logger.handlers:
    tester_handler = RotatingFileHandler(
        SPAPI_TESTER_LOG_PATH,
        maxBytes=2_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    tester_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    tester_handler.setFormatter(tester_formatter)
    tester_logger.setLevel(log_level)
    tester_logger.addHandler(tester_handler)

app = FastAPI(title="SP-API Desktop App (Minimal)", version="1.0.0")

register_nicelabel_routes(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event():
    """Initialize background tasks on app startup."""
    try:
        # Spawn startup backfill in background thread (non-blocking)
        start_vendor_rt_sales_startup_backfill_thread()
        # Start auto-sync loop in background thread
        start_vendor_rt_sales_auto_sync()
        logger.info("[Startup] Background tasks initialized successfully")
    except Exception as e:
        logger.warning(f"[Startup] Failed to initialize background tasks: {e}")


# -------------------------------
# UI
# -------------------------------
UI_DIR = Path(__file__).parent / "ui"
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATE_DIR)


app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
@app.get("/")
def home():  # simple root
    return {"status": "running", "message": "Fresh start - add your endpoints here"}


@app.get("/ui/index.html")
def ui_index():
    index_path = UI_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(index_path)

# -------------------------------
# ====================================================================
# VENDOR POs SYNC ARCHITECTURE
# ====================================================================
# Main entry point:  sync_vendor_pos() @ line 912
# Called by:         POST /api/vendor-pos/sync (triggered by UI button)
#
# Flow:
# 1. sync_vendor_pos()
#    - Calls fetch_vendor_pos_from_api() to get list of POs
#    - Merges with cached POs and writes to vendor_pos_cache.json
#    - Calls sync_vendor_po_lines_batch() with fetched PO numbers
#
# 2. sync_vendor_po_lines_batch(po_numbers)
#    - For EACH PO in the list:
#    - Calls _sync_vendor_po_lines_for_po(po_number)
#
# 3. _sync_vendor_po_lines_for_po(po_number)
#    - Fetches detailed PO with item status from SP-API
#    - Parses ordered/received/pending/shortage quantities per line item
#    - SCOPED DELETE: DELETE FROM vendor_po_lines WHERE po_number = ?
#      (Only deletes lines for THIS PO, never a global wipe)
#    - Inserts new line records into vendor_po_lines
#
# Aggregation:
# 4. _aggregate_vendor_po_lines(pos_list)
#    - Called by GET /api/vendor-pos to compute totals
#    - SUM(ordered_qty), SUM(received_qty), etc. grouped by po_number
#    - Adds computed fields to each PO for UI display
#
# Rebuild operation:
# 5. rebuild_all_vendor_po_lines()
#    - Queries ALL POs from vendor_pos table
#    - Resyncs line data for each using _sync_vendor_po_lines_for_po
#    - Used as one-time backfill or after data corruption
#    - Run via: python main.py --rebuild-po-lines
#
# ====================================================================

# Vendor POs (raw JSON)

# -------------------------------
VENDOR_POS_CACHE = Path(__file__).parent / "vendor_pos_cache.json"
ASIN_CACHE_PATH = Path(__file__).parent / "asin_image_cache.json"
MARKETPLACE_IDS: List[str] = [
    mp for mp in (os.getenv("MARKETPLACE_IDS") or os.getenv("MARKETPLACE_ID", "")).split(",") if mp.strip()
]
SHIP_FROM_PARTY_ID = os.getenv("SHIP_FROM_PARTY_ID", "")
auth_client = SpApiAuth()

# Catalog DB
CATALOG_DB_PATH = Path(__file__).parent / "catalog.db"
CATALOG_API_HOST = os.getenv("CATALOG_API_HOST", "https://sellingpartnerapi-na.amazon.com")

# Marketplace region mappings for SP-API endpoints
# UAE (A2VIGQ35RCS4UG) belongs to EU region along with DE, ES, and UK marketplaces
EU_MARKETPLACE_IDS = {"A2VIGQ35RCS4UG", "A1PA6795UKMFR9", "A13V1IB3VIYZZH", "A1RKKUPIHCS9HS", "A1F83G8C2ARO7P"}
FE_MARKETPLACE_IDS = {"A1VC38T7YXB528"}  # JP
PO_TRACKER_PATH = Path(__file__).parent / "po_tracker.json"
OOS_STATE_PATH = Path(__file__).parent / "oos_state.json"


def resolve_catalog_host(marketplace_id: str) -> str:
    """
    Resolve the correct SP-API host for Catalog API calls based on marketplace.
    Reuses resolve_vendor_host to ensure consistency across all SP-API calls.
    """
    return resolve_vendor_host(marketplace_id)


def default_created_after(days: int = 60) -> str:
    dt = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    return dt.isoformat() + "Z"


# Ensure DB exists at import time
init_catalog_db()

# Migrate vendor_po_lines schema if needed
try:
    from tools.debug.migrate_vendor_po_schema import migrate_vendor_po_lines_schema
    migrate_vendor_po_lines_schema()
except Exception as e:
    logger.warning(f"[Startup] Schema migration skipped or failed (non-critical): {e}")

# Initialize vendor_realtime_sales table & state
try:
    vendor_realtime_sales_service.init_vendor_realtime_sales_table()
    vendor_realtime_sales_service.init_vendor_rt_audit_hours_table()
    from services.db import init_vendor_rt_sales_state_table, ensure_oos_export_history_table, ensure_vendor_inventory_table, ensure_app_kv_table
    init_vendor_rt_sales_state_table()
    ensure_oos_export_history_table()
    ensure_vendor_inventory_table()
    ensure_app_kv_table()
except Exception as e:
    logger.warning(f"[Startup] Failed to init vendor_realtime_sales tables (non-critical): {e}")


# ========================================
# Vendor Real Time Sales Auto-Sync
# ========================================
VENDOR_RT_SALES_AUTO_SYNC_INTERVAL_MINUTES = 15  # Now 15 minutes instead of 60
_rt_sales_auto_sync_thread = None
_rt_sales_auto_sync_stop = False


def start_vendor_rt_sales_startup_backfill_thread():
    """
    Spawn a daemon thread that runs the vendor real-time sales startup backfill
    in the background so the FastAPI startup event returns quickly.
    """
    import threading
    t = threading.Thread(
        target=run_vendor_rt_sales_startup_backfill,
        name="VendorRtSalesStartupBackfill",
        daemon=True,
    )
    t.start()
    logger.debug("[RTSalesStartupBackfill] Daemon thread spawned")


def run_vendor_rt_sales_startup_backfill():
    """
    On app startup, ensure vendor_realtime_sales has no gaps up to safe_now.
    Uses vendor_rt_sales_state to determine last_ingested_end_utc and backfills
    up to MAX_HISTORY_DAYS in the past.
    
    This function is intended to run in a background daemon thread so startup is non-blocking.
    """
    try:
        from services.vendor_realtime_sales import (
            get_safe_now_utc,
            get_last_ingested_end_utc,
            backfill_realtime_sales_for_gap,
            MAX_HISTORY_DAYS,
        )
        from services.db import get_db_connection
        
        safe_now = get_safe_now_utc()
        earliest_allowed = safe_now - timedelta(days=MAX_HISTORY_DAYS)
        
        marketplace_ids = MARKETPLACE_IDS if MARKETPLACE_IDS else ["A2VIGQ35RCS4UG"]
        marketplace_id = marketplace_ids[0]
        
        logger.info(f"[RTSalesStartupBackfill] Starting startup backfill for {marketplace_id}")
        
        # Get last ingested end time from state
        with get_db_connection() as conn:
            last_end = get_last_ingested_end_utc(conn, marketplace_id)
        
        if last_end is None:
            # First time: backfill last 24h
            start_window = safe_now - timedelta(hours=24)
            logger.info(f"[RTSalesStartupBackfill] First time setup, backfilling from {start_window}")
        else:
            if last_end < earliest_allowed:
                # Too old, backfill from earliest_allowed
                start_window = earliest_allowed
                logger.info(f"[RTSalesStartupBackfill] Last ingested {last_end} is too old, starting from {start_window}")
            else:
                # Normal gap backfill
                start_window = last_end
                logger.info(f"[RTSalesStartupBackfill] Backfilling gap from {last_end}")
        
        if start_window < safe_now:
            logger.info(f"[RTSalesStartupBackfill] Backfilling [{start_window}, {safe_now})")
            rows, asins, hours = backfill_realtime_sales_for_gap(
                spapi_client=None,  # Will use global spapi_client
                marketplace_id=marketplace_id,
                start_utc=start_window,
                end_utc=safe_now,
            )
            logger.info(f"[RTSalesStartupBackfill] Completed: {rows} rows, {asins} ASINs, {hours} hours")
        else:
            logger.info("[RTSalesStartupBackfill] Already up-to-date, no backfill needed")
    
    except Exception as e:
        logger.error(f"[RTSalesStartupBackfill] Failed (non-critical): {e}", exc_info=True)
        # Do not crash the app on startup backfill failure


def vendor_rt_sales_auto_sync_loop():
    """
    Background loop that periodically syncs Vendor Real Time Sales data.
    Runs every VENDOR_RT_SALES_AUTO_SYNC_INTERVAL_MINUTES minutes.
    
    Logic:
    - Checks for gaps in vendor_rt_sales_state.
    - If no state: backfill last 24h.
    - If gap > 2h: backfill the gap.
    - Otherwise: sync overlapping last 3h (for late adjustments).
    - If quota is exceeded: activate cooldown and skip remaining work this cycle.
    - Optionally runs daily/weekly audits (controlled by ENABLE_* flags).
    """
    global _rt_sales_auto_sync_stop
    import time
    
    logger.info(f"[RTSalesAutoSync] Started, will sync every {VENDOR_RT_SALES_AUTO_SYNC_INTERVAL_MINUTES} minutes")
    
    interval_seconds = VENDOR_RT_SALES_AUTO_SYNC_INTERVAL_MINUTES * 60
    
    marketplace_ids = MARKETPLACE_IDS if MARKETPLACE_IDS else ["A2VIGQ35RCS4UG"]
    marketplace_id = marketplace_ids[0]
    
    while not _rt_sales_auto_sync_stop:
        try:
            from services.vendor_realtime_sales import (
                get_safe_now_utc,
                get_last_ingested_end_utc,
                backfill_realtime_sales_for_gap,
                is_in_quota_cooldown,
                start_quota_cooldown,
                is_backfill_in_progress,
                start_backfill,
                end_backfill,
                ENABLE_VENDOR_RT_SALES_DAILY_AUDIT,
                ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT,
            )
            from services.spapi_reports import SpApiQuotaError
            from services.db import get_db_connection
            
            now_utc = get_safe_now_utc()
            
            # Check if in quota cooldown
            if is_in_quota_cooldown(now_utc):
                logger.warning("[RTSalesAutoSync] In quota cooldown; skipping all SP-API calls this cycle")
                time.sleep(interval_seconds)
                continue
            
            # Check if a backfill is already in progress
            if is_backfill_in_progress():
                logger.warning("[RTSalesAutoSync] Previous cycle still in progress; skipping this cycle")
                time.sleep(interval_seconds)
                continue
            
            # Acquire backfill lock
            if not start_backfill():
                logger.warning("[RTSalesAutoSync] Failed to acquire backfill lock; another cycle is active")
                time.sleep(interval_seconds)
                continue
            
            # Get last ingested end time
            with get_db_connection() as conn:
                last_end = get_last_ingested_end_utc(conn, marketplace_id)
            
            if last_end is None:
                # First time in this cycle: backfill last 24h
                start_window = now_utc - timedelta(hours=24)
                logger.info(
                    f"[RTSalesAutoSync] No state found, backfilling last 24h "
                    f"[{start_window.isoformat()}, {now_utc.isoformat()})"
                )
            elif now_utc - last_end > timedelta(hours=2):
                # Gap detected (app was sleeping)
                start_window = last_end
                logger.info(
                    f"[RTSalesAutoSync] Gap detected ({(now_utc - last_end).total_seconds() / 3600:.1f}h), "
                    f"backfilling [{start_window.isoformat()}, {now_utc.isoformat()})"
                )
            else:
                # Normal sync: overlap last 3h to catch late adjustments
                start_window = now_utc - timedelta(hours=3)
                logger.info(
                    f"[RTSalesAutoSync] Normal sync, refreshing last 3h "
                    f"[{start_window.isoformat()}, {now_utc.isoformat()})"
                )
            
            # Perform backfill/sync
            try:
                rows, asins, hours = backfill_realtime_sales_for_gap(
                    spapi_client=None,  # Will use global
                    marketplace_id=marketplace_id,
                    start_utc=start_window,
                    end_utc=now_utc,
                )
                
                logger.info(
                    f"[RTSalesAutoSync] Cycle complete: "
                    f"{rows} rows, {asins} unique ASINs, {hours} hours processed"
                )
            except SpApiQuotaError as e:
                logger.error(f"[RTSalesAutoSync] QuotaExceeded; aborting remaining backfills/audits this cycle: {e}")
                # ↓ FIX #2: Use CURRENT time, not stale now_utc
                start_quota_cooldown(datetime.now(timezone.utc))
                end_backfill()
                time.sleep(interval_seconds)
                continue
            
            # =====================================================================
            # DAILY AUDIT: re-download last 24 hours once per day
            # =====================================================================
            if ENABLE_VENDOR_RT_SALES_DAILY_AUDIT:
                try:
                    from services.vendor_realtime_sales import (
                        get_vendor_rt_sales_state,
                        update_daily_audit_state,
                        run_realtime_sales_audit_window,
                        should_run_rt_sales_daily_audit,
                        mark_rt_sales_daily_audit_ran,
                    )
                    
                    with get_db_connection() as conn:
                        state = get_vendor_rt_sales_state(conn, marketplace_id)
                        should_run, today_str = should_run_rt_sales_daily_audit(conn)
                    
                    if should_run:
                        # Define audit window: last 24 full hours
                        audit_end = now_utc.replace(minute=0, second=0, microsecond=0)
                        audit_start = audit_end - timedelta(hours=24)
                        
                        logger.info(f"[RTSalesAutoSync] Running daily audit [{audit_start.isoformat()}, {audit_end.isoformat()}) (uae_date={today_str})")
                        try:
                            audit_rows, audit_asins, audit_hours = run_realtime_sales_audit_window(
                                spapi_client=None,
                                start_utc=audit_start,
                                end_utc=audit_end,
                                marketplace_id=marketplace_id,
                                label="daily"
                            )
                            with get_db_connection() as conn:
                                update_daily_audit_state(marketplace_id, audit_end)
                                mark_rt_sales_daily_audit_ran(conn, today_str)
                            logger.info(f"[RTSalesAutoSync] Daily audit done: {audit_rows} rows, {audit_asins} ASINs, {audit_hours} hours")
                        except SpApiQuotaError as e:
                            logger.error(f"[RTSalesAutoSync] QuotaExceeded during daily audit; aborting remaining audits this cycle: {e}")
                            # ↓ FIX #2: Use CURRENT time
                            start_quota_cooldown(datetime.now(timezone.utc))
                            end_backfill()
                            break  # Stop further audits this cycle
                    else:
                        logger.info(f"[RTSalesAutoSync] Skipping daily audit for uae_date={today_str} (already ran today)")
                
                except Exception as e:
                    logger.error(f"[RTSalesAutoSync] Daily audit error: {e}", exc_info=True)
            
            # =====================================================================
            # WEEKLY AUDIT: re-download last 7 days once per week
            # =====================================================================
            if ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT:
                try:
                    from services.vendor_realtime_sales import (
                        get_vendor_rt_sales_state,
                        update_weekly_audit_state,
                        run_realtime_sales_audit_window,
                    )
                    
                    with get_db_connection() as conn:
                        state = get_vendor_rt_sales_state(conn, marketplace_id)
                    
                    last_weekly_audit = state.get("last_weekly_audit_utc")
                    
                    # Define audit window: last 7 full days
                    audit_end = now_utc.replace(minute=0, second=0, microsecond=0)
                    audit_start = audit_end - timedelta(days=7)
                    
                    # Check if we need to run weekly audit
                    should_run_weekly = False
                    if last_weekly_audit is None:
                        should_run_weekly = True
                    else:
                        try:
                            from datetime import datetime as dt_type
                            last_audit_dt = dt_type.fromisoformat(last_weekly_audit.replace("Z", "+00:00"))
                            # Audit window is [audit_start, audit_end)
                            # We want to run if the window has advanced since last audit
                            if audit_start > last_audit_dt:
                                should_run_weekly = True
                        except Exception as e:
                            logger.warning(f"[RTSalesAutoSync] Failed to parse last_weekly_audit_utc: {e}")
                            should_run_weekly = True
                    
                    if should_run_weekly:
                        logger.info(f"[RTSalesAutoSync] Running weekly audit [{audit_start.isoformat()}, {audit_end.isoformat()})")
                        try:
                            audit_rows, audit_asins, audit_hours = run_realtime_sales_audit_window(
                                spapi_client=None,
                                start_utc=audit_start,
                                end_utc=audit_end,
                                marketplace_id=marketplace_id,
                                label="weekly"
                            )
                            update_weekly_audit_state(marketplace_id, audit_end)
                            logger.info(f"[RTSalesAutoSync] Weekly audit done: {audit_rows} rows, {audit_asins} ASINs, {audit_hours} hours")
                        except SpApiQuotaError as e:
                            logger.error(f"[RTSalesAutoSync] QuotaExceeded during weekly audit; aborting remaining audits this cycle: {e}")
                            # ↓ FIX #2: Use CURRENT time
                            start_quota_cooldown(datetime.now(timezone.utc))
                            end_backfill()
                            break  # Stop further audits this cycle
                
                except Exception as e:
                    logger.error(f"[RTSalesAutoSync] Weekly audit error: {e}", exc_info=True)
        
        except Exception as e:
            logger.error(f"[RTSalesAutoSync] Cycle failed: {e}", exc_info=True)
        
        finally:
            # Always release backfill lock, even if exceptions occurred
            end_backfill()
        
        # Sleep until next sync
        logger.debug(f"[RTSalesAutoSync] Next sync in {VENDOR_RT_SALES_AUTO_SYNC_INTERVAL_MINUTES} minutes")
        time.sleep(interval_seconds)


def start_vendor_rt_sales_auto_sync():
    """Start the vendor real-time sales auto-sync background thread."""
    global _rt_sales_auto_sync_thread, _rt_sales_auto_sync_stop
    
    if _rt_sales_auto_sync_thread is not None and _rt_sales_auto_sync_thread.is_alive():
        logger.warning("[RTSalesAutoSync] Already running; skipping duplicate start")
        return
    
    _rt_sales_auto_sync_stop = False
    import threading
    _rt_sales_auto_sync_thread = threading.Thread(
        target=vendor_rt_sales_auto_sync_loop,
        daemon=True,
        name="VendorRtSalesAutoSync"
    )
    _rt_sales_auto_sync_thread.start()
    logger.info("[RTSalesAutoSync] Background thread started")



def fetch_spapi_catalog_item(asin: str) -> Dict[str, Any]:
    """
    Single call to SP-API Catalog Items for a given ASIN.
    Stores title/image into local catalog DB.
    
    FIX #3A: Added 30s timeout to prevent infinite hangs on network failure.
    FIX #3D: Optimized includedData parameter to request only necessary attributes.
    """
    if not asin:
        raise HTTPException(status_code=400, detail="Missing ASIN")
    existing = spapi_catalog_status().get(asin)
    if existing:
        return {"asin": asin, "source": "db", "title": existing.get("title"), "image": existing.get("image")}

    if not MARKETPLACE_IDS:
        raise HTTPException(status_code=400, detail="No marketplace IDs configured")
    marketplace = MARKETPLACE_IDS[0].strip()
    api_host = resolve_catalog_host(marketplace)
    
    # FIX #3D: Use includedData to request only what we need:
    # - summaries: Gets title, description, and basic product info
    # - images: Gets product images (needed for UI display)
    # This reduces response payload and improves performance.
    params = {
        "marketplaceIds": marketplace,
        "includedData": "summaries,images",
    }
    access_token = auth_client.get_lwa_access_token()
    url = f"{api_host}/catalog/2022-04-01/items/{asin}"
    headers = {
        "x-amz-access-token": access_token,
        "user-agent": "sp-api-desktop-app/1.0",
        "accept": "application/json",
    }
    # HARDENING: Add 30s timeout to prevent infinite hang
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    except requests.exceptions.Timeout:
        logger.error(f"[Catalog] Timeout fetching {asin} after 30s")
        raise HTTPException(status_code=504, detail=f"Catalog fetch timeout for {asin}")
    except requests.exceptions.RequestException as e:
        logger.error(f"[Catalog] Network error fetching {asin}: {e}")
        raise HTTPException(status_code=503, detail=f"Catalog fetch network error: {str(e)}")
    
    if resp.status_code == 429:
        raise HTTPException(status_code=429, detail="Catalog rate limit hit. Try again later.")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Catalog fetch failed: {resp.text}")
    data = resp.json()
    payload = data.get("item") or data  # accommodate raw item or wrapped
    if not isinstance(payload, dict):
        payload = {"raw": data}
    upsert_spapi_catalog(asin, payload)
    return {"asin": asin, "source": "spapi", "title": payload.get("title"), "image": payload.get("image"), "payload": payload}


def extract_asins_from_pos() -> Tuple[List[str], Dict[str, str]]:
    """
    Collect unique ASINs from vendor_pos_cache.json.
    """
    data = load_vendor_pos_cache(VENDOR_POS_CACHE)
    if not data:
        return [], {}
    items_raw = []
    if isinstance(data, dict) and "items" in data:
        items_raw = data.get("items") or []
    elif isinstance(data, list):
        items_raw = data
    normalized = []
    for entry in items_raw:
        if isinstance(entry, dict) and "raw" in entry and isinstance(entry["raw"], dict):
            normalized.append(entry["raw"])
        else:
            normalized.append(entry)

    asins = set()
    sku_map: Dict[str, str] = {}
    for entry in normalized:
        details = entry.get("orderDetails") or {}
        for item in details.get("items") or []:
            asin = item.get("amazonProductIdentifier")
            if asin:
                asins.add(asin)
                if asin not in sku_map and item.get("vendorProductIdentifier"):
                    sku_map[asin] = item.get("vendorProductIdentifier")
    return sorted(asins), sku_map


def normalize_pos_entries(data: Any) -> List[Dict[str, Any]]:
    items_raw = []
    if isinstance(data, dict) and "items" in data:
        items_raw = data.get("items") or []
    elif isinstance(data, list):
        items_raw = data
    normalized = []
    for entry in items_raw:
        if isinstance(entry, dict) and "raw" in entry and isinstance(entry["raw"], dict):
            normalized.append(entry["raw"])
        else:
            normalized.append(entry)
    return normalized


def parse_po_date(po: Dict[str, Any]) -> datetime:
    date_str = po.get("purchaseOrderDate") or po.get("orderDetails", {}).get("purchaseOrderDate") or ""
    try:
        if date_str.endswith("Z"):
            date_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return datetime.min


def enrich_items_with_catalog(po_list):
    looked_up = set()
    updated = False
    spapi_cache = spapi_catalog_status()
    for po in po_list:
        details = po.get("orderDetails") or {}
        for item in details.get("items") or []:
            asin = item.get("amazonProductIdentifier")
            if not asin:
                continue
            if asin in looked_up:
                continue
            # Prefer master DB (SP-API catalog)
            master = spapi_cache.get(asin)
            if master:
                if master.get("title"):
                    item.setdefault("title", master.get("title"))
                if master.get("image"):
                    item.setdefault("image", master.get("image"))
                if master.get("barcode"):
                    item.setdefault("barcode", master.get("barcode"))
                looked_up.add(asin)
                continue
            looked_up.add(asin)


def harvest_barcodes_from_pos(pos_list: List[Dict[str, Any]], log_prefix: str = "[BarcodeHarvest]") -> Dict[str, int]:
    """
    Scan PO lines for barcode-like external IDs and upsert into catalog if missing.
    Returns counters: set, invalid, lines.
    """
    counts = {"set": 0, "invalid": 0, "lines": 0}
    if not pos_list:
        return counts
    for po in pos_list:
        po_num = po.get("purchaseOrderNumber") or ""
        details = po.get("orderDetails") or {}
        for item in details.get("items") or []:
            counts["lines"] += 1
            asin = item.get("amazonProductIdentifier") or ""
            # Align with PO modal: prefer vendorProductIdentifier as externalId surrogate
            candidate = (
                item.get("vendorProductIdentifier")
                or item.get("externalId")
                or item.get("buyerProductIdentifier")
                or ""
            ).strip()
            if not asin or not candidate or is_asin(candidate):
                continue
            barcode = normalize_barcode(candidate)
            if not barcode:
                counts["invalid"] += 1
                logger.info(f"{log_prefix} Skipped invalid barcode candidate '{candidate}' for asin={asin} sku={item.get('vendorProductIdentifier')}")
                continue
            if set_catalog_barcode_if_absent(asin, barcode):
                counts["set"] += 1
                logger.info(
                    f"{log_prefix} Set barcode {barcode} for catalog asin={asin} "
                    f"sku={item.get('vendorProductIdentifier')} from PO {po_num} "
                    f"line {item.get('itemSequenceNumber')}"
                )
    return counts


@app.get("/api/catalog-cache-stats")
async def catalog_cache_stats():
    cache = load_asin_cache()
    if not isinstance(cache, dict):
        cache = {}
    return {"asinCount": len(cache)}


def resolve_vendor_host(marketplace_id: str) -> str:
    if marketplace_id in EU_MARKETPLACE_IDS:
        return "https://sellingpartnerapi-eu.amazon.com"
    if marketplace_id in FE_MARKETPLACE_IDS:
        return "https://sellingpartnerapi-fe.amazon.com"
    return "https://sellingpartnerapi-na.amazon.com"


class PoStatusUpdate(BaseModel):
    status: str
    appointmentDate: str | None = None





def extract_purchase_orders(obj: Any) -> List[Dict[str, Any]] | None:
    """
    Recursively search the JSON response for a key 'purchaseOrders' whose value is a list,
    and return that list. If not found, also look for 'orders' or 'ordersStatus'. If still not found, return None.
    """
    if isinstance(obj, dict):
        if "purchaseOrders" in obj and isinstance(obj["purchaseOrders"], list):
            return obj["purchaseOrders"]
        if "ordersStatus" in obj and isinstance(obj["ordersStatus"], list):
            return obj["ordersStatus"]
        if "orders" in obj and isinstance(obj["orders"], list):
            return obj["orders"]
        for v in obj.values():
            found = extract_purchase_orders(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = extract_purchase_orders(v)
            if found is not None:
                return found
    return None


def fetch_vendor_pos_from_api(created_after: str, created_before: str, max_pages: int = 5):
    """
    Fetch Vendor POs from SP-API.
    
    FIX #3D: Added 20s timeout to prevent infinite hangs on network failure.
    """
    if not MARKETPLACE_IDS:
        raise HTTPException(status_code=400, detail="MARKETPLACE_IDS not configured")
    marketplace = MARKETPLACE_IDS[0].strip()
    host = resolve_vendor_host(marketplace)
    url = f"{host}/vendor/orders/v1/purchaseOrders"
    token = auth_client.get_lwa_access_token()
    all_pos = []
    next_token = None
    page = 0
    while page < max_pages:
        params = {
            "createdAfter": created_after,
            "createdBefore": created_before,
            "marketplaceIds": marketplace,
            "limit": 100,
        }
        if next_token:
            params["nextToken"] = next_token
        headers = {
            "Authorization": f"Bearer {token}",
            "x-amz-access-token": token,
            "accept": "application/json",
            "user-agent": "sp-api-desktop-app/1.0",
        }
        # HARDENING: Add 20s timeout to prevent infinite hang
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.exceptions.Timeout:
            logger.error(f"[VendorPO] Timeout fetching POs after 20s on page {page}")
            raise HTTPException(status_code=504, detail=f"Vendor PO fetch timeout on page {page}")
        except requests.exceptions.RequestException as e:
            logger.error(f"[VendorPO] Network error fetching POs: {e}")
            raise HTTPException(status_code=503, detail=f"Vendor PO fetch network error: {str(e)}")
        
        if resp.status_code >= 400:
            print(f"Vendor PO fetch failed {resp.status_code}: {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=f"Vendor PO fetch failed: {resp.text}")
        data = resp.json()
        items = extract_purchase_orders(data) or []
        if not items:
            if isinstance(data, dict) and "payload" in data:
                try:
                    import json as _json
                    payload_preview = _json.dumps(data.get("payload"), ensure_ascii=False)[:500]
                except Exception:
                    payload_preview = str(data.get("payload"))[:500]
                print(f"Vendor PO fetch returned empty page: status {resp.status_code}, payload preview: {payload_preview}")
            else:
                print(
                    f"Vendor PO fetch returned empty page: status {resp.status_code}, "
                    f"top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                )
        all_pos.extend(items)
        next_token = data.get("nextToken") if isinstance(data, dict) else None
        if not next_token:
            break
        page += 1
    print(f"Fetched {len(all_pos)} POs from {created_after} to {created_before}")
    return all_pos


def _parse_qty(val: Any) -> int:
    try:
        if isinstance(val, dict):
            return int(val.get("amount") or 0)
        return int(val or 0)
    except Exception:
        return 0


def fetch_po_status_totals(po_number: str) -> Dict[str, int]:
    """
    Call /vendor/orders/v1/purchaseOrdersStatus for a single PO and derive total_received_qty and total_pending_qty.
    """
    if not po_number:
        return {"total_received_qty": 0, "total_pending_qty": 0}
    if not MARKETPLACE_IDS:
        logger.warning("[VendorPO] MARKETPLACE_IDS not configured, skipping status fetch")
        return {"total_received_qty": 0, "total_pending_qty": 0}

    marketplace = MARKETPLACE_IDS[0].strip()
    host = resolve_vendor_host(marketplace)
    url = f"{host}/vendor/orders/v1/purchaseOrdersStatus"
    token = auth_client.get_lwa_access_token()

    params = {
        "marketplaceIds": marketplace,
        "purchaseOrderNumber": po_number,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "x-amz-access-token": token,
        "accept": "application/json",
        "user-agent": "sp-api-desktop-app/1.0",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"[VendorPO] Status fetch failed for PO {po_number}: {e}")
        return {"total_received_qty": 0, "total_pending_qty": 0}

    try:
        data = resp.json()
    except Exception:
        logger.warning(f"[VendorPO] Non-JSON status response for PO {po_number}")
        return {"total_received_qty": 0, "total_pending_qty": 0}

    purchase_orders = extract_purchase_orders(data) or []
    total_received = 0
    total_pending = 0

    for po in purchase_orders:
        items = po.get("itemStatus") or po.get("items") or []
        for item in items:
            # Normalize quantities
            ordered_amt = 0
            oq_wrapper = item.get("orderedQuantity", {})
            if isinstance(oq_wrapper, dict):
                if "amount" in oq_wrapper:
                    ordered_amt = _parse_qty(oq_wrapper)
                elif isinstance(oq_wrapper.get("orderedQuantity"), dict):
                    ordered_amt = _parse_qty(oq_wrapper.get("orderedQuantity"))

            ack_obj = item.get("acknowledgementStatus") or {}
            accepted_amt = _parse_qty(ack_obj.get("acceptedQuantity"))

            recv_info = item.get("receivingStatus") or {}
            received_qty = _parse_qty(recv_info.get("receivedQuantity"))
            pending_qty = _parse_qty(recv_info.get("pendingQuantity"))

            if pending_qty == 0:
                # Default to accepted - received (business definition)
                pending_qty = max(0, accepted_amt - received_qty)

            total_received += received_qty
            total_pending += pending_qty

    return {"total_received_qty": total_received, "total_pending_qty": total_pending}


def fetch_detailed_po_with_status(po_number: str):
    """
    FIX: Fetch detailed PO using GET /vendor/orders/v1/purchaseOrders/{po_number}
    to get itemStatus with acknowledgedQuantity, receivedQuantity, cancelledQuantity, etc.
    
    This is necessary because the list endpoint only returns orderedQuantity.
    """
    if not MARKETPLACE_IDS:
        return None
    
    marketplace = MARKETPLACE_IDS[0].strip()
    host = resolve_vendor_host(marketplace)
    url = f"{host}/vendor/orders/v1/purchaseOrders/{po_number}"
    token = auth_client.get_lwa_access_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "x-amz-access-token": token,
        "accept": "application/json",
        "user-agent": "sp-api-desktop-app/1.0",
    }

    # Prefer purchaseOrdersStatus because it carries itemStatus/receivingStatus
    status_url = f"{host}/vendor/orders/v1/purchaseOrdersStatus"
    status_params = {
        "marketplaceIds": marketplace,
        "purchaseOrderNumber": po_number,
    }
    try:
        status_resp = requests.get(status_url, headers=headers, params=status_params, timeout=20)
        if status_resp.status_code == 200:
            status_data = status_resp.json()
            status_pos = extract_purchase_orders(status_data) or []
            if status_pos:
                po_match = next((po for po in status_pos if po.get("purchaseOrderNumber") == po_number), status_pos[0])
                # Ensure ship_to is available in legacy location
                if "orderDetails" not in po_match:
                    od: Dict[str, Any] = {}
                    if po_match.get("shipToParty"):
                        od["shipToParty"] = po_match.get("shipToParty")
                    if po_match.get("purchaseOrderDate"):
                        od["purchaseOrderDate"] = po_match.get("purchaseOrderDate")
                    if od:
                        po_match["orderDetails"] = od
                logger.info(f"[VendorPO] Using purchaseOrdersStatus payload for PO {po_number}")
                return po_match
    except Exception as e:
        logger.warning(f"[VendorPO] Failed purchaseOrdersStatus lookup for PO {po_number}: {e}")
    
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            payload = data.get("payload") if isinstance(data, dict) else None
            if isinstance(payload, dict):
                # Unwrap purchaseOrders array if present
                if isinstance(payload.get("purchaseOrders"), list) and payload["purchaseOrders"]:
                    return payload["purchaseOrders"][0]
                return payload
            return None
        elif resp.status_code == 404:
            logger.warning(f"[VendorPO] PO {po_number} not found (404)")
            return None
        else:
            logger.warning(f"[VendorPO] Failed to fetch detailed PO {po_number}: {resp.status_code}")
            return None
    except requests.exceptions.Timeout:
        logger.warning(f"[VendorPO] Timeout fetching detailed PO {po_number}")
        return None
    except Exception as e:
        logger.warning(f"[VendorPO] Error fetching detailed PO {po_number}: {e}")
        return None


def _compute_accepted_line_amounts(items: List[Dict[str, Any]]) -> tuple:
    """
    For each item in items (from itemStatus), compute accepted_line_amount = accepted_qty * netCost.amount.
    Also accumulates PO-level accepted total.
    Additionally extract received_qty from receivingStatus.receivedQuantity.
    
    Returns:
        (items_with_amounts, po_total_amount, currency_code)
        where items_with_amounts is the list with accepted_line_amount and received_qty added to each item
    """
    from decimal import Decimal, InvalidOperation
    
    po_total = Decimal("0")
    currency_code = "AED"
    
    for item in items:
        try:
            # Get accepted quantity from acknowledgementStatus
            ack_status = item.get("acknowledgementStatus", {}) or {}
            accepted_qty_obj = ack_status.get("acceptedQuantity", {}) or {}
            accepted_qty = 0
            if isinstance(accepted_qty_obj, dict):
                accepted_qty = _parse_qty(accepted_qty_obj)
            
            # Extract received quantity from receivingStatus
            recv_status = item.get("receivingStatus", {}) or {}
            received_qty_obj = recv_status.get("receivedQuantity", {}) or {}
            received_qty = 0
            if isinstance(received_qty_obj, dict):
                received_qty = _parse_qty(received_qty_obj)
            item["received_qty"] = received_qty
            
            # If no acknowledgement yet, use 0 for accepted
            if accepted_qty <= 0:
                item["accepted_line_amount"] = 0.0
                continue
            
            # Get netCost
            net_cost_obj = item.get("netCost", {}) or {}
            if not isinstance(net_cost_obj, dict):
                item["accepted_line_amount"] = 0.0
                continue
            
            cost_amount_str = net_cost_obj.get("amount", "")
            if not cost_amount_str:
                item["accepted_line_amount"] = 0.0
                continue
            
            # Update currency from this item if present
            if net_cost_obj.get("currencyCode"):
                currency_code = net_cost_obj.get("currencyCode")
            
            # Parse unit price as Decimal
            try:
                unit_price = Decimal(str(cost_amount_str))
            except (InvalidOperation, ValueError, TypeError):
                asin = item.get("amazonProductIdentifier", "?")
                logger.warning(f"[VendorPO] Could not parse netCost.amount '{cost_amount_str}' for ASIN {asin}")
                item["accepted_line_amount"] = 0.0
                continue
            
            # Compute line cost = accepted_qty * unit_price
            line_cost = Decimal(accepted_qty) * unit_price
            item["accepted_line_amount"] = float(line_cost)
            po_total += line_cost
            
        except Exception as e:
            logger.error(f"[VendorPO] Error processing item for accepted amount: {e}", exc_info=True)
            item["accepted_line_amount"] = 0.0
            item.setdefault("received_qty", 0)
            continue
    
    return items, po_total, currency_code


def _compute_total_accepted_cost(po: Dict[str, Any], accepted_line_map: Dict[str, int]) -> tuple:
    """
    Compute total accepted cost = sum(accepted_qty * netCost.amount) for all items in the PO.
    
    BUGFIX: Previous implementation only summed unit costs without multiplying by accepted quantities.
    This function correctly computes: for each item, accepted_qty (from vendor_po_lines) * unit_price (from netCost).
    
    Args:
        po: PO dict with orderDetails.items[]
        accepted_line_map: dict mapping ASIN (amazonProductIdentifier) -> accepted_qty from vendor_po_lines
    
    Returns:
        (total_cost: Decimal, currency_code: str)
    """
    from decimal import Decimal, InvalidOperation
    
    total_cost = Decimal("0")
    currency_code = "AED"
    po_num = po.get("purchaseOrderNumber", "?")
    
    order_details = po.get("orderDetails", {}) or {}
    items = order_details.get("items", []) or []
    
    for item in items:
        try:
            asin = item.get("amazonProductIdentifier", "")
            if not asin:
                continue
            
            # Get accepted quantity for this ASIN from vendor_po_lines map
            accepted_qty = accepted_line_map.get(asin, 0)
            if accepted_qty <= 0:
                # Skip items with no accepted quantity
                continue
            
            # Get netCost
            net_cost_obj = item.get("netCost", {})
            if not isinstance(net_cost_obj, dict):
                continue
            
            cost_amount_str = net_cost_obj.get("amount", "")
            if not cost_amount_str:
                continue
            
            # Update currency from this item if present
            if net_cost_obj.get("currencyCode"):
                currency_code = net_cost_obj.get("currencyCode")
            
            # Parse unit price as Decimal (safe handling)
            try:
                unit_price = Decimal(str(cost_amount_str))
            except (InvalidOperation, ValueError, TypeError):
                logger.warning(f"[VendorPO] Could not parse netCost.amount '{cost_amount_str}' for ASIN {asin} in PO {po_num}")
                continue
            
            # Compute line cost = accepted_qty * unit_price
            line_cost = Decimal(accepted_qty) * unit_price
            total_cost += line_cost
            logger.debug(f"[VendorPO] PO {po_num} ASIN {asin}: accepted_qty={accepted_qty} * unit_price={unit_price} = line_cost={line_cost}")
            
        except Exception as e:
            logger.error(f"[VendorPO] Error processing item in PO {po_num}: {e}", exc_info=True)
            continue
    
    logger.info(f"[VendorPO] PO {po_num}: total_accepted_cost = {total_cost} {currency_code}")
    return total_cost, currency_code


def _compute_vendor_central_columns(po: Dict[str, Any], line_totals: Dict[str, int], accepted_line_map: Dict[str, int] = None) -> None:
    """
    Compute Vendor Central-style display columns from line totals and PO data.
    Adds the following derived fields to the PO dict:
    - poItemsCount: number of distinct items in the PO
    - requestedQty: total ordered quantity (units)
    - acceptedQty: total accepted quantity (units)
    - asnQty: ASN (shipment announced) quantity (units) [set to 0 for now; future: integrate Vendor Shipments API]
    - receivedQty: total received quantity (units)
    - remainingQty: accepted - received - cancelled (units)
    - cancelledQty: total cancelled quantity (units)
    - total_accepted_cost: sum(accepted_qty * netCost.amount) for all items (FIXED: was just summing unit prices)
    - total_accepted_cost_currency: currency code for the cost
    - amazonStatus: raw purchaseOrderState from SP-API
    """
    from decimal import Decimal
    
    po_num = po.get("purchaseOrderNumber") or ""
    
    requested = line_totals.get("total_ordered", 0)
    accepted = line_totals.get("total_accepted", 0)
    received = line_totals.get("total_received", 0)
    cancelled = line_totals.get("total_cancelled", 0)
    
    remaining = max(0, accepted - received - cancelled)
    
    po["poItemsCount"] = po.get("orderDetails", {}).get("items", []) if isinstance(po.get("orderDetails", {}), dict) else 0
    if isinstance(po["poItemsCount"], list):
        po["poItemsCount"] = len(po["poItemsCount"])
    
    po["requestedQty"] = requested
    po["acceptedQty"] = accepted
    po["asnQty"] = 0
    po["receivedQty"] = received
    po["remainingQty"] = remaining
    po["cancelledQty"] = cancelled
    
    # Compute total accepted cost from order items and accepted quantities
    # FIXED: Now correctly multiplies accepted_qty * unit_price instead of just summing unit prices
    if accepted_line_map is None:
        accepted_line_map = {}
    
    total_cost, currency_code = _compute_total_accepted_cost(po, accepted_line_map)
    
    po["total_accepted_cost"] = float(total_cost)
    po["total_accepted_cost_currency"] = currency_code
    
    # Also set legacy field names for backward compatibility
    po["totalAcceptedCostAmount"] = str(total_cost)
    po["totalAcceptedCostCurrency"] = currency_code
    
    # Add Amazon status (raw purchaseOrderState)
    po["amazonStatus"] = po.get("purchaseOrderState", "")
    
    ship_to_party = po.get("orderDetails", {}).get("shipToParty", {}) if isinstance(po.get("orderDetails"), dict) else {}
    if isinstance(ship_to_party, dict):
        po["shipToCode"] = ship_to_party.get("partyId", "")
        address = ship_to_party.get("address", {}) if isinstance(ship_to_party.get("address"), dict) else {}
        city = address.get("city", "")
        country = address.get("country", "")
        po["shipToText"] = f"{po['shipToCode']} – {city}, {country}".replace(" – , ", "") if city or country else po["shipToCode"]
    else:
        po["shipToCode"] = ""
        po["shipToText"] = ""


def _aggregate_vendor_po_lines(pos_list: List[Dict[str, Any]]) -> None:
    """
    Attach aggregated quantities from vendor_po_lines to each PO in pos_list.
    Exposes total_ordered_qty, total_accepted_qty, total_received_qty, total_pending_qty,
    total_cancelled_qty, total_shortage_qty, and Vendor Central-style columns for display.
    
    Also builds per-line accepted_qty map (keyed by ASIN) for cost calculation.
    """
    if not pos_list:
        return

    po_numbers = [po.get("purchaseOrderNumber") for po in pos_list if po.get("purchaseOrderNumber")]
    if not po_numbers:
        return

    try:
        with time_block(f"vendor_po_lines.aggregate:{len(po_numbers)}"):
            agg_map = db_repos.get_vendor_po_line_totals(po_numbers)
            # Also fetch per-line accepted quantities for cost calculation
            line_details_map = db_repos.get_vendor_po_line_details(po_numbers)
    except Exception as e:
        logger.error(f"[VendorPO] Error aggregating vendor_po_lines: {e}", exc_info=True)
        for po in pos_list:
            po.setdefault("total_ordered_qty", 0)
            po.setdefault("total_received_qty", 0)
            po.setdefault("total_pending_qty", 0)
            po.setdefault("total_accepted_qty", 0)
            po.setdefault("total_cancelled_qty", 0)
            po.setdefault("total_shortage_qty", 0)
            _compute_vendor_central_columns(po, {})
        return

    for po in pos_list:
        po_num = po.get("purchaseOrderNumber")
        totals = agg_map.get(po_num, {})
        if totals:
            po.update(
                {
                    "total_ordered_qty": totals.get("total_ordered", 0),
                    "total_accepted_qty": totals.get("total_accepted", 0),
                    "total_received_qty": totals.get("total_received", 0),
                    "total_pending_qty": totals.get("total_pending", 0),
                    "total_cancelled_qty": totals.get("total_cancelled", 0),
                    "total_shortage_qty": totals.get("total_shortage", 0),
                }
            )
        else:
            po.setdefault("total_ordered_qty", 0)
            po.setdefault("total_accepted_qty", 0)
            po.setdefault("total_received_qty", 0)
            po.setdefault("total_pending_qty", 0)
            po.setdefault("total_cancelled_qty", 0)
            po.setdefault("total_shortage_qty", 0)
        
        # Build ASIN-keyed accepted_qty map for this PO
        accepted_line_map = {}
        po_lines = line_details_map.get(po_num, [])
        for line in po_lines:
            asin = line.get("asin", "")
            accepted_qty = line.get("accepted_qty", 0)
            if asin:
                accepted_line_map[asin] = accepted_qty
        
        _compute_vendor_central_columns(po, totals if totals else {}, accepted_line_map)
        po.setdefault("total_received_qty", 0)
        po.setdefault("total_pending_qty", 0)


def _attach_po_status_totals(pos_list: List[Dict[str, Any]]) -> None:
    """
    Enrich each PO with total_received_qty and total_pending_qty from purchaseOrdersStatus endpoint.
    """
    if not pos_list:
        return
    for po in pos_list:
        po_num = po.get("purchaseOrderNumber") or ""
        try:
            totals = fetch_po_status_totals(po_num)
            po.update(totals)
        except Exception as e:
            logger.warning(f"[VendorPO] Failed to attach status totals for PO {po_num}: {e}")
            po.setdefault("total_received_qty", 0)
            po.setdefault("total_pending_qty", 0)


def _refresh_po_in_cache(po_number: str) -> None:
    """
    Best-effort refresh of a single PO inside vendor_pos_cache.json by fetching live details.
    Does not raise; logs on failure.
    """
    if not po_number:
        return
    try:
        data = load_vendor_pos_cache(VENDOR_POS_CACHE)
    except Exception as exc:
        logger.warning(f"[VendorPO] Failed to read cache for refresh: {exc}")
        return

    try:
        detailed = fetch_detailed_po_with_status(po_number)
    except Exception as exc:
        logger.warning(f"[VendorPO] Failed to fetch detailed PO during refresh {po_number}: {exc}")
        return

    if not isinstance(detailed, dict):
        return

    normalized = normalize_pos_entries(data)
    updated = False
    for idx, po in enumerate(normalized):
        if po.get("purchaseOrderNumber") == po_number:
            normalized[idx] = detailed
            updated = True
            break
    if not updated:
        normalized.append(detailed)

    try:
        payload = {"items": normalized}
        save_vendor_pos_cache(payload, VENDOR_POS_CACHE)
    except Exception as exc:
        logger.warning(f"[VendorPO] Failed to write refreshed cache for {po_number}: {exc}")


def seed_oos_from_rejected_lines(po_numbers: List[str], po_date_map: Dict[str, str] | None = None) -> int:
    return oos_service.seed_oos_from_rejected_lines(po_numbers, po_date_map)


def seed_oos_from_rejected_payload(purchase_orders: List[Dict[str, Any]]) -> int:
    return oos_service.seed_oos_from_rejected_payload(purchase_orders)


def consolidate_picklist(po_numbers: List[str]) -> Dict[str, Any]:
    return picklist_service.consolidate_picklist(
        po_numbers,
        VENDOR_POS_CACHE,
        normalize_pos_entries,
        load_oos_state,
        save_oos_state,
        spapi_catalog_status,
        oos_service.upsert_oos_entry,
        db_repos.get_rejected_vendor_po_lines,
    )


def generate_picklist_pdf(po_numbers: List[str], items: List[Dict[str, Any]], summary: Dict[str, Any]) -> bytes:
    return picklist_service.generate_picklist_pdf(po_numbers, items, summary)


@app.post("/api/vendor-pos/sync")
def sync_vendor_pos(createdAfter: Optional[str] = Body(None)):
    """
    Fetch Vendor POs from SP-API for a window and persist to vendor_pos_cache.json.
    """
    created_after = createdAfter or default_created_after()
    created_before = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    try:
        pos = fetch_vendor_pos_from_api(created_after, created_before, max_pages=5)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sync failed: {exc}")

        try:
            harvested = harvest_barcodes_from_pos(pos)
            if harvested.get("set"):
                logger.info(f"[VendorPO] Harvested {harvested['set']} barcodes from PO sync (lines={harvested['lines']}, invalid={harvested['invalid']})")
        except Exception as exc:
            logger.warning(f"[VendorPO] Barcode harvest failed during sync: {exc}")

    if not pos:
        print(f"[vendor-pos-sync] fetched 0 POs from {created_after} to {created_before} - leaving vendor_pos_cache.json unchanged")
        return {
            "status": "no_update",
            "source": "spapi",
            "fetched": 0,
            "createdAfter": created_after,
            "createdBefore": created_before,
        }

    # Attach status totals (received/pending) from purchaseOrdersStatus
    try:
        _attach_po_status_totals(pos)
    except Exception as e:
        logger.warning(f"[VendorPO] Failed to attach status totals during sync: {e}")

    merged_items = []
    try:
        old_data = load_vendor_pos_cache(VENDOR_POS_CACHE)
        old_normalized = normalize_pos_entries(old_data)
    except Exception:
        old_normalized = []

    by_po = {}
    for po in old_normalized:
        po_num = po.get("purchaseOrderNumber")
        if po_num:
            by_po[po_num] = po
    for po in pos:
        po_num = po.get("purchaseOrderNumber")
        if po_num:
            by_po[po_num] = po
    merged_items = list(by_po.values())

    payload = {"items": merged_items}
    try:
        save_vendor_pos_cache(payload, VENDOR_POS_CACHE)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write vendor_pos_cache.json: {exc}")

    # FIX: Sync vendor_po_lines for all fetched POs with detailed status
    po_numbers = [po.get("purchaseOrderNumber") for po in pos if po.get("purchaseOrderNumber")]
    if po_numbers:
        try:
            sync_vendor_po_lines_batch(po_numbers)
            logger.info(f"[VendorPO] Synced {len(po_numbers)} POs with detailed status")
        except Exception as e:
            logger.error(f"[VendorPO] Error syncing vendor_po_lines: {e}")
            # Don't fail the main sync, just log the error
    
    # DEBUG: Log vendor_po_lines count after sync
    try:
        line_count = db_repos.count_vendor_po_lines()
        logger.info(f"[VendorPO] vendor_po_lines row count after sync: {line_count}")
    except Exception as e:
        logger.warning(f"[VendorPO] Could not log vendor_po_lines count: {e}")

    return {
        "status": "ok",
        "source": "spapi",
        "fetched": len(pos),
        "createdAfter": created_after,
        "createdBefore": created_before,
    }


@app.post("/api/vendor-pos/rebuild")
def rebuild_vendor_pos_full():
    """
    Full rebuild: fetch all POs since 2025-10-01, attach status totals, overwrite cache, and sync vendor_po_lines.
    """
    created_after = default_created_after()
    created_before = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    try:
        pos = fetch_vendor_pos_from_api(created_after, created_before, max_pages=10)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rebuild failed: {exc}")

    try:
        harvested = harvest_barcodes_from_pos(pos)
        if harvested.get("set"):
            logger.info(f"[VendorPO] Harvested {harvested['set']} barcodes from rebuild (lines={harvested['lines']}, invalid={harvested['invalid']})")
    except Exception as exc:
        logger.warning(f"[VendorPO] Barcode harvest failed during rebuild: {exc}")

    if not pos:
        print(f"[vendor-pos-rebuild] fetched 0 POs from {created_after} to {created_before} - leaving vendor_pos_cache.json unchanged")
        return {
            "status": "no_update",
            "source": "spapi",
            "fetched": 0,
            "createdAfter": created_after,
            "createdBefore": created_before,
        }

    # Attach status totals (received/pending) from purchaseOrdersStatus
    try:
        _attach_po_status_totals(pos)
    except Exception as e:
        logger.warning(f"[VendorPO] Failed to attach status totals during full rebuild: {e}")

    payload = {"items": pos}
    try:
        save_vendor_pos_cache(payload, VENDOR_POS_CACHE)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write vendor_pos_cache.json: {exc}")

    # Sync vendor_po_lines for all fetched POs
    po_numbers = [po.get("purchaseOrderNumber") for po in pos if po.get("purchaseOrderNumber")]
    if po_numbers:
        try:
            sync_vendor_po_lines_batch(po_numbers)
            logger.info(f"[VendorPO] Rebuild synced {len(po_numbers)} POs with detailed status")
        except Exception as e:
            logger.error(f"[VendorPO] Error syncing vendor_po_lines during rebuild: {e}")

    return {
        "status": "ok",
        "source": "spapi",
        "fetched": len(pos),
        "createdAfter": created_after,
        "createdBefore": created_before,
    }



@app.post("/api/vendor-notifications/test-ingest")
def ingest_vendor_notification(event: Dict[str, Any]):
    """
    Test endpoint to ingest vendor notifications (simulated).
    """
    process_vendor_notification(event or {})
    return {"ok": True}


@app.get("/api/vendor-notifications/recent")
def recent_vendor_notifications(limit: int = 100):
    """
    Return last N vendor notifications from log.
    """
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 100
    return {"items": get_recent_notifications(limit)}


@app.get("/api/vendor-pos")
def get_vendor_pos(
    refresh: int = Query(0, description="If 1, refresh POs from SP-API before reading cache"),
    enrich: bool = Query(False, description="Enrich ASINs with Catalog data"),
    createdAfter: Optional[str] = Query(None, description="ISO start date; defaults to 60d ago"),
):
    source = "cache"
    created_after_param = createdAfter or default_created_after()
    if refresh == 1:
        created_after = created_after_param
        created_before = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        try:
            pos = fetch_vendor_pos_from_api(created_after, created_before, max_pages=5)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Sync failed: {exc}")
        try:
            save_vendor_pos_cache({"items": pos}, VENDOR_POS_CACHE)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write vendor_pos_cache.json: {exc}")
        
        try:
            harvested = harvest_barcodes_from_pos(pos)
            if harvested.get("set"):
                logger.info(f"[VendorPO] Harvested {harvested['set']} barcodes from GET refresh (lines={harvested['lines']}, invalid={harvested['invalid']})")
        except Exception as exc:
            logger.warning(f"[VendorPO] Barcode harvest failed during GET refresh: {exc}")

        po_numbers = [po.get("purchaseOrderNumber") for po in pos if po.get("purchaseOrderNumber")]
        try:
            _attach_po_status_totals(pos)
        except Exception as e:
            logger.warning(f"[VendorPO] Failed to attach status totals during GET refresh: {e}")
        if po_numbers:
            try:
                sync_vendor_po_lines_batch(po_numbers)
                logger.info(f"[VendorPO] Synced {len(po_numbers)} POs with detailed status from GET refresh")
            except Exception as e:
                logger.error(f"[VendorPO] Error syncing vendor_po_lines from GET refresh: {e}")
        
        source = "spapi"

    try:
        data = load_vendor_pos_cache(VENDOR_POS_CACHE, raise_on_error=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read cache: {exc}")
    if not data:
        return {"items": [], "source": source}

    normalized = normalize_pos_entries(data)
    try:
        cutoff_dt = datetime.fromisoformat(created_after_param.replace("Z", "+00:00"))
    except Exception:
        cutoff_dt = None
    if cutoff_dt:
        filtered = []
        for po in normalized:
            po_dt = parse_po_date(po)
            if po_dt and po_dt < cutoff_dt:
                continue
            filtered.append(po)
        normalized = filtered
    cutoff = datetime(2025, 10, 1)
    print(f"[vendor-pos] normalized POs: {len(normalized)}")
    filtered = []
    for po in normalized:
        dt = parse_po_date(po)
        if dt == datetime.min or dt >= cutoff:
            filtered.append(po)
    filtered.sort(key=parse_po_date, reverse=True)
    tracker = load_po_tracker()
    for po in filtered:
        po_num = po.get("purchaseOrderNumber")
        internal_status = "Pending"
        appointment_date = None
        if po_num and isinstance(tracker, dict):
            entry = tracker.get(po_num) or {}
            if isinstance(entry, dict):
                if entry.get("status"):
                    internal_status = entry["status"]
                if entry.get("appointmentDate"):
                    appointment_date = entry["appointmentDate"]
        po["_internalStatus"] = internal_status
        if appointment_date:
            po["_appointmentDate"] = appointment_date

        try:
            flags = get_po_notification_flags(po_num)
            po["notificationFlags"] = flags
        except Exception as exc:
            logger.warning(f"[VendorPO] Failed to attach notification flags for {po_num}: {exc}")
    print(f"[vendor-pos] filtered POs (>= 2025-10-01): {len(filtered)}")

    _aggregate_vendor_po_lines(filtered)

    if enrich:
        enrich_items_with_catalog(filtered)

    return {"items": filtered, "source": source}


@app.get("/api/vendor-pos/{po_number}")
async def get_single_vendor_po(po_number: str, enrich: int = 0):
    """
    Return a single vendor PO by purchaseOrderNumber.
    If enrich=1, run enrich_items_with_catalog on just this PO before returning.
    """
    try:
        data = load_vendor_pos_cache(VENDOR_POS_CACHE, raise_on_error=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read cache: {exc}")
    if not data:
        return JSONResponse({"error": "PO not found"}, status_code=404)

    normalized = normalize_pos_entries(data)

    po = next((p for p in normalized if p.get("purchaseOrderNumber") == po_number), None)
    if not po:
        return JSONResponse({"error": "PO not found"}, status_code=404)

    flags = get_po_notification_flags(po_number)
    if flags.get("needs_refresh"):
        try:
            _refresh_po_in_cache(po_number)
            clear_po_refresh_flag(po_number)
            data = load_vendor_pos_cache(VENDOR_POS_CACHE, raise_on_error=True)
            normalized = normalize_pos_entries(data)
            po = next((p for p in normalized if p.get("purchaseOrderNumber") == po_number), po)
        except Exception as exc:
            logger.warning(f"[VendorPO] Refresh on open failed for {po_number}: {exc}")

    try:
        detailed = fetch_detailed_po_with_status(po_number)
        if isinstance(detailed, dict):
            status_items = detailed.get("itemStatus") or detailed.get("items") or []
            if status_items:
                po.setdefault("orderDetails", {})
                po["orderDetails"]["items"] = status_items
    except Exception as exc:
        logger.warning(f"[VendorPO] Could not attach status items for PO {po_number}: {exc}")

    # Compute accepted line amounts for modal display
    try:
        items = po.get("orderDetails", {}).get("items", []) or []
        if items:
            items_with_amounts, po_total, currency = _compute_accepted_line_amounts(items)
            po["orderDetails"]["items"] = items_with_amounts
            po["accepted_total_amount"] = float(po_total)
            po["accepted_total_currency"] = currency
            logger.info(f"[VendorPO] PO {po_number} modal: accepted_total = {po_total} {currency}")
        else:
            po["accepted_total_amount"] = 0.0
            po["accepted_total_currency"] = "AED"
    except Exception as exc:
        logger.warning(f"[VendorPO] Failed to compute accepted amounts for PO {po_number}: {exc}")
        po["accepted_total_amount"] = 0.0
        po["accepted_total_currency"] = "AED"

    if enrich:
        try:
            enrich_items_with_catalog([po])
        except Exception as exc:
            print(f"Error enriching PO {po_number}: {exc}")

    po["notificationFlags"] = flags
    return {"item": po}


@app.get("/api/vendor-po-lines")
def get_vendor_po_lines(po_number: str):
    """
    Return line-item details for a PO from vendor_po_lines table.
    Used by the "Line Items Inventory Breakdown" modal in the UI.
    """
    if not po_number:
        raise HTTPException(status_code=400, detail="po_number parameter required")
    
    try:
        with time_block("vendor_po_lines.endpoint_fetch"):
            rows = db_repos.get_vendor_po_lines(po_number)
        lines = []
        if rows:
            for row in rows:
                lines.append(
                    {
                        "asin": row.get("asin") or "",
                        "sku": row.get("sku") or "",
                        "ordered_qty": row.get("ordered_qty") or 0,
                        "received_qty": row.get("received_qty") or 0,
                        "pending_qty": row.get("pending_qty") or 0,
                        "shortage_qty": row.get("shortage_qty") or 0,
                        "last_changed_utc": row.get("last_changed_utc") or "",
                    }
                )
            logger.info(f"[VendorPO] Retrieved {len(lines)} lines for PO {po_number}")
        else:
            logger.warning(f"[VendorPO] No vendor_po_lines found for PO {po_number}")
        
        return {
            "po_number": po_number,
            "items": lines,
            "message": "No line items found for this PO." if not lines else None
        }

    except Exception as e:
        logger.error(f"[VendorPO] Error fetching lines for PO {po_number}: {e}", exc_info=True)
        return {
            "po_number": po_number,
            "items": [],
            "message": "Failed to fetch line details. Please try again."
        }


# ====================================================================
# VENDOR REAL TIME SALES ENDPOINTS
# ====================================================================

@app.post("/api/vendor-realtime-sales/refresh")
async def refresh_vendor_realtime_sales():
    """
    Disabled legacy endpoint; use Audit → Fill Day for any SP-API repair work.
    """
    logger.warning(
        "[VendorRtAPI] Legacy refresh endpoint hit; SP-API refresh is disabled in favor of Audit → Fill Day."
    )
    return JSONResponse(
        {
            "status": "disabled",
            "message": "Ad-hoc refreshes are disabled. Use the Audit tab and Fill Day workflow to request data."
        },
        status_code=410,
    )


@app.get("/api/vendor-realtime-sales/summary")
def get_vendor_realtime_sales_summary(
    lookback_hours: Optional[int] = None,
    view_by: str = "asin",
    window: Optional[str] = None,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None
):
    """
    Get aggregated real-time sales summary for a window.
    
    Query parameters:
    - lookback_hours: int (2, 4, 8, 12, 24, 48) - trailing window in hours
    - view_by: "asin" (default) or "time" - aggregation view
    - window: (deprecated) "last_1h", "last_3h", "last_24h", "today", "yesterday", "custom"
    - start_utc, end_utc: for custom window
    
    If lookback_hours is provided, it takes precedence. Otherwise falls back to window param.

    The payload is built entirely from the local DB and audit tables (read-only); no SP-API
    refresh or backfill logic runs inside this route.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        marketplace_id = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"
        
        # NEW: Use lookback_hours if provided
        if lookback_hours is not None:
            if lookback_hours not in (2, 4, 8, 12, 24, 48):
                raise HTTPException(status_code=400, detail="lookback_hours must be one of: 2, 4, 8, 12, 24, 48")
            
            resolved_end = now_utc
            resolved_start = now_utc - timedelta(hours=lookback_hours)
        # BACKWARDS COMPATIBILITY: Fall back to window param
        elif window:
            if window == "last_1h":
                resolved_end = now_utc
                resolved_start = now_utc - timedelta(hours=1)
            elif window == "last_3h":
                resolved_end = now_utc
                resolved_start = now_utc - timedelta(hours=3)
            elif window == "last_24h":
                resolved_end = now_utc
                resolved_start = now_utc - timedelta(hours=24)
            elif window == "today":
                resolved_end = now_utc.replace(hour=23, minute=59, second=59)
                resolved_start = now_utc.replace(hour=0, minute=0, second=0)
            elif window == "yesterday":
                yesterday = now_utc - timedelta(days=1)
                resolved_end = yesterday.replace(hour=23, minute=59, second=59)
                resolved_start = yesterday.replace(hour=0, minute=0, second=0)
            elif window == "custom" and start_utc and end_utc:
                resolved_start = datetime.fromisoformat(start_utc)
                resolved_end = datetime.fromisoformat(end_utc)
                if resolved_start.tzinfo is None:
                    resolved_start = resolved_start.replace(tzinfo=timezone.utc)
                if resolved_end.tzinfo is None:
                    resolved_end = resolved_end.replace(tzinfo=timezone.utc)
            else:
                resolved_end = now_utc
                resolved_start = now_utc - timedelta(hours=24)
        else:
            # Default: last 24 hours
            resolved_end = now_utc
            resolved_start = now_utc - timedelta(hours=24)
        
        start_str = resolved_start.isoformat()
        end_str = resolved_end.isoformat()
        
        # Validate view_by
        if view_by not in ("asin", "time"):
            raise HTTPException(status_code=400, detail="view_by must be 'asin' or 'time'")
        
        summary = vendor_realtime_sales_service.get_realtime_sales_summary(
            start_utc=start_str,
            end_utc=end_str,
            marketplace_id=marketplace_id,
            view_by=view_by
        )
        
        # BACKWARDS COMPATIBILITY: Return with "top_asins" key if view_by="asin"
        # (old clients may expect this)
        if view_by == "asin":
            summary["top_asins"] = summary.get("rows", [])
        
        return summary
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[VendorRtSummary] Failed to get summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vendor-realtime-sales/status")
def get_vendor_realtime_sales_status():
    """
    Lightweight status endpoint so the UI knows whether
    auto-sync/backfill or quota cooldown is active.
    
    Returns JSON with status fields for UI polling.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        status = vendor_realtime_sales_service.get_rt_sales_status(now_utc=now_utc)
        return status
    except Exception as e:
        logger.error(f"[VendorRtSummary] Failed to get status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vendor-realtime-sales/asin/{asin}")
def get_vendor_realtime_sales_for_asin(
    asin: str,
    lookback_hours: Optional[int] = None,
    window: Optional[str] = None,
    start_utc: Optional[str] = None,
    end_utc: Optional[str] = None
):
    """
    Get hourly sales detail for a specific ASIN.
    
    Query parameters:
    - lookback_hours: int (2, 4, 8, 12, 24, 48) - trailing window in hours
    - window: (deprecated) "last_1h", "last_3h", "last_24h", etc.
    - start_utc, end_utc: for custom window
    """
    try:
        now_utc = datetime.now(timezone.utc)
        marketplace_id = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"
        
        # NEW: Use lookback_hours if provided
        if lookback_hours is not None:
            if lookback_hours not in (2, 4, 8, 12, 24, 48):
                raise HTTPException(status_code=400, detail="lookback_hours must be one of: 2, 4, 8, 12, 24, 48")
            
            resolved_end = now_utc
            resolved_start = now_utc - timedelta(hours=lookback_hours)
        # BACKWARDS COMPATIBILITY: Fall back to window param
        elif window:
            if window == "last_1h":
                resolved_end = now_utc
                resolved_start = now_utc - timedelta(hours=1)
            elif window == "last_3h":
                resolved_end = now_utc
                resolved_start = now_utc - timedelta(hours=3)
            elif window == "last_24h":
                resolved_end = now_utc
                resolved_start = now_utc - timedelta(hours=24)
            elif window == "today":
                resolved_end = now_utc.replace(hour=23, minute=59, second=59)
                resolved_start = now_utc.replace(hour=0, minute=0, second=0)
            elif window == "yesterday":
                yesterday = now_utc - timedelta(days=1)
                resolved_end = yesterday.replace(hour=23, minute=59, second=59)
                resolved_start = yesterday.replace(hour=0, minute=0, second=0)
            elif window == "custom" and start_utc and end_utc:
                resolved_start = datetime.fromisoformat(start_utc)
                resolved_end = datetime.fromisoformat(end_utc)
                if resolved_start.tzinfo is None:
                    resolved_start = resolved_start.replace(tzinfo=timezone.utc)
                if resolved_end.tzinfo is None:
                    resolved_end = resolved_end.replace(tzinfo=timezone.utc)
            else:
                resolved_end = now_utc
                resolved_start = now_utc - timedelta(hours=24)
        else:
            # Default: last 24 hours
            resolved_end = now_utc
            resolved_start = now_utc - timedelta(hours=24)
        
        start_str = resolved_start.isoformat()
        end_str = resolved_end.isoformat()
        
        detail = vendor_realtime_sales_service.get_realtime_sales_for_asin(
            asin=asin,
            start_utc=start_str,
            end_utc=end_str,
            marketplace_id=marketplace_id
        )
        
        return {
            "asin": asin,
            "data": detail
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[VendorRtSummary] Failed to get ASIN detail: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vendor-realtime-sales/backfill-4weeks")
async def api_vendor_rt_sales_backfill_4weeks(request: Request):
    """
    Legacy 4-week backfill endpoint (disabled).
    Use the Audit -> Fill Day workflow for repairing missing hours instead.
    """
    logger.warning("[VendorRtAPI] Legacy 4-week backfill endpoint hit; disabled in favor of Audit + Fill Day flow.")
    return JSONResponse({
        "status": "disabled",
        "message": "Legacy 4-week backfill is disabled. Use the audit calendar and Fill Day flow instead."
    }, status_code=410)


@app.get("/api/vendor-rt-sales/audit-4weeks")
def api_vendor_rt_sales_audit_4weeks():
    """
    Returns audit stats (rows, hours, per-day units) for the same 4-week window used by Sales Trends.
    """
    try:
        from services.db import get_db_connection

        marketplace_id = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"
        with get_db_connection() as conn:
            data = vendor_realtime_sales_service.audit_vendor_rt_sales_last_4_weeks(
                conn,
                marketplace_id
            )
        return data
    except Exception as e:
        logger.error(f"[VendorRtAudit] Failed to get 4-week audit: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vendor-realtime-sales/audit-calendar")
def api_vendor_rt_sales_audit_calendar(days: Optional[int] = None):
    """
    Returns the daily ingestion coverage for the last `days` days in UAE time.
    """
    try:
        marketplace_id = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"
        max_days = vendor_realtime_sales_service.AUDIT_CALENDAR_MAX_DAYS
        default_days = vendor_realtime_sales_service.AUDIT_CALENDAR_DEFAULT_DAYS
        requested_days = days if isinstance(days, int) and days >= 1 else default_days
        # Clamp the runtime window to the 1-30 day retention window Amazon guarantees.
        bounded_days = max(1, min(requested_days, max_days))
        data = vendor_realtime_sales_service.get_rt_sales_audit_calendar(
            marketplace_id=marketplace_id,
            days=bounded_days,
        )
        return data
    except Exception as e:
        logger.error(f"[VendorRtAudit] Failed to compute audit calendar: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vendor-realtime-sales/audit-day")
def api_vendor_rt_sales_audit_day(date: str):
    """
    Returns hour-level coverage for a single UAE date.
    """
    try:
        marketplace_id = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"
        data = vendor_realtime_sales_service.get_rt_sales_audit_day(
            marketplace_id=marketplace_id,
            date_str=date,
        )
        return data
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as e:
        logger.error(f"[VendorRtAudit] Failed to compute audit day for {date}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vendor-realtime-sales/fill-day")
async def api_vendor_rt_sales_fill_day(
    background_tasks: BackgroundTasks,
    request: Request,
):
    """
    Schedule SP-API requests for the missing hours of a UAE day.
    """
    try:
        body = await request.json() if await request.body() else {}
    except:
        body = {}

    date_str = body.get("date")
    missing_hours = body.get("missing_hours", [])

    if not date_str:
        raise HTTPException(status_code=400, detail="date parameter required")
    if not isinstance(missing_hours, list):
        raise HTTPException(status_code=400, detail="missing_hours must be a list")

    marketplace_id = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"

    cleaned_hours = []
    for entry in missing_hours:
        try:
            hour_int = int(entry)
        except (TypeError, ValueError):
            continue
        if 0 <= hour_int <= 23:
            cleaned_hours.append(hour_int)

    try:
        plan = vendor_realtime_sales_service.plan_fill_day_run(
            date_str=date_str,
            requested_hours=cleaned_hours,
            marketplace_id=marketplace_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    scheduled = [
        {
            "hour": hour_info["hour"],
            "start_utc": hour_info["start_utc"],
            "end_utc": hour_info["end_utc"],
            "status": "scheduled",
        }
        for hour_info in plan["hours_to_request"]
    ]

    if plan["hours_to_request"]:
        background_tasks.add_task(
            vendor_realtime_sales_service.run_fill_day_repair_cycle,
            date_str,
            plan["hours_to_request"],
            marketplace_id,
            plan["total_missing"],
        )

    logger.info(
        "[VendorRtSales] Fill-day run %s: scheduled %d task(s) (remaining %d, pending %d, cooldown=%s)",
        date_str,
        len(scheduled),
        plan["remaining_missing"],
        len(plan["pending_hours"]),
        plan["cooldown_active"],
    )

    return {
        "scheduled_tasks": scheduled,
        "total_missing": plan["total_missing"],
        "remaining_missing": plan["remaining_missing"],
        "pending_hours": plan["pending_hours"],
        "cooldown_active": plan["cooldown_active"],
        "cooldown_until": plan["cooldown_until"],
    }


@app.post("/api/vendor-realtime-sales/audit-and-repair")
async def api_vendor_rt_sales_audit_and_repair():
    """
    Legacy endpoint (disabled). Use the audit calendar + Fill Day workflow instead.
    """
    logger.warning(
        "[VendorRtAudit] Legacy audit-and-repair endpoint called; disabled in favor of the audit calendar and Fill Day flow."
    )
    return {
        "status": "disabled",
        "message": "Master audit-and-repair is disabled. Use the Audit tab to inspect coverage and the Fill Day flow to request specific hours."
    }


# ========================================
# Sales Trends Endpoints
# ========================================

@app.get("/api/vendor-sales-trends")
def api_vendor_sales_trends(
    lookback_weeks: int = 4,
    min_total_units: int = 1,
):
    """
    Returns 4-week rolling sales trends per ASIN using vendor_realtime_sales.
    
    For now, only support lookback_weeks=4. If another value is passed, clamp to 4.
    
    Query parameters:
    - lookback_weeks: int (default 4, clamped to 4)
    - min_total_units: int (default 1, minimum total units across 4 weeks to include)
    
    Returns:
    {
      "window": {
        "start_utc": "...",
        "end_utc": "...",
        "start_uae": "...",
        "end_uae": "..."
      },
      "bucket_size_days": 7,
      "bucket_labels": ["W4", "W3", "W2", "W1"],
      "week_ranges_uae": [
        { "label": "W4", "start_uae": "...", "end_uae": "..." },
        ...
      ],
      "this_week": {
        "start_utc": "...",
        "end_utc": "...",
        "start_uae": "...",
        "end_uae": "..."
      },
      "rows": [
        {
          "asin": "...",
          "title": "...",
          "imageUrl": "...",
          "w4_units": int,
          "w3_units": int,
          "w2_units": int,
          "w1_units": int,
          "this_week_units": int,
          "this_week_progress": float,
          "total_units_4w": int,
          "delta_units": int,
          "pct_change": float or None,
          "trend": "rising" | "falling" | "flat" | "new" | "dead"
        }
      ]
    }
    """
    try:
        from services.db import get_db_connection
        
        # Clamp lookback_weeks to 4
        if lookback_weeks != 4:
            lookback_weeks = 4
        
        marketplace_id = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"
        
        with get_db_connection() as conn:
            data = vendor_realtime_sales_service.get_sales_trends_last_4_weeks(
                conn,
                marketplace_id,
                min_total_units=min_total_units,
            )
        
        return data
    
    except Exception as e:
        logger.error(f"[VendorRtTrends] Failed to get sales trends: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vendor-realtime-sales/synthesize-precutover-hours")
def synthesize_precutover_hours(max_days: int = 3):
    """
    One-time admin endpoint to fake pre-30-day coverage for Trends.
    This should be called manually once after deployment.
    """
    try:
        marketplace_id = MARKETPLACE_IDS[0] if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"

        logger.info(
            "[VendorRtAdmin] Running synthetic pre-cutover audit patch for %s (max_days=%s)",
            marketplace_id,
            max_days,
        )

        result = vendor_realtime_sales_service.synthesize_pre_cutover_audit_hours(
            max_days=max_days,
            marketplace_id=marketplace_id,
        )
        return result

    except Exception as e:
        logger.error(
            f"[VendorRtAdmin] Failed to synthesize pre-cutover hours: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


# ========================================
# Vendor Inventory Endpoints
# ========================================

@app.post("/api/vendor-inventory/refresh")
def api_vendor_inventory_refresh():
    """
    Downloads GET_VENDOR_INVENTORY_REPORT (weekly),
    extracts latest week's per-ASIN snapshot,
    stores into vendor_inventory_asin table.
    Returns number of ASINs ingested.
    """
    try:
        from services.db import get_db_connection
        from services.spapi_reports import SpApiQuotaError
        
        marketplace_ids = MARKETPLACE_IDS if MARKETPLACE_IDS else ["A2VIGQ35RCS4UG"]
        marketplace_id = marketplace_ids[0]
        
        logger.info(f"[VendorInventory] Refresh requested for {marketplace_id}")
        
        with get_db_connection() as conn:
            count = refresh_vendor_inventory_snapshot(conn, marketplace_id)
        
        logger.info(f"[VendorInventory] Refresh complete: {count} ASINs stored")
        
        return {
            "status": "ok",
            "ingested_asins": count,
            "marketplace_id": marketplace_id,
        }
    
    except spapi_reports.SpApiQuotaError as e:
        logger.warning(f"[VendorInventory] QuotaExceeded during refresh: {e}")
        return {
            "status": "quota_error",
            "error": str(e),
        }
    
    except Exception as e:
        logger.error(f"[VendorInventory] Failed to refresh inventory: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
        }


@app.get("/api/vendor-inventory/snapshot")
def api_vendor_inventory_snapshot():
    """
    Returns stored snapshot (latest week only)
    for UI rendering.
    
    Sorted by sellable_onhand_units DESC, then ASIN ASC.
    """
    try:
        from services.db import get_db_connection
        
        marketplace_ids = MARKETPLACE_IDS if MARKETPLACE_IDS else ["A2VIGQ35RCS4UG"]
        marketplace_id = marketplace_ids[0]
        
        with get_db_connection() as conn:
            rows = get_vendor_inventory_snapshot_for_ui(conn, marketplace_id)
        
        # Convert Row objects to dicts if needed
        items = [dict(row) if hasattr(row, 'keys') else row for row in rows]
        
        logger.info(f"[VendorInventory] Returned snapshot with {len(items)} ASINs")
        
        return {
            "status": "ok",
            "count": len(items),
            "items": items,
        }
    
    except Exception as e:
        logger.error(f"[VendorInventory] Failed to get snapshot: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "count": 0,
            "items": [],
        }


@app.get("/api/vendor-inventory/debug")
def api_vendor_inventory_debug():
    """
    Developer-only debug route:
    Shows latest raw JSON returned from the GET_VENDOR_INVENTORY_REPORT call.
    
    DO NOT consume this in UI — for debugging only.
    """
    try:
        from services.vendor_inventory import fetch_latest_vendor_inventory_report_json
        
        marketplace_ids = MARKETPLACE_IDS if MARKETPLACE_IDS else ["A2VIGQ35RCS4UG"]
        marketplace_id = marketplace_ids[0]
        
        logger.info(f"[VendorInventory] Debug: Fetching raw report for {marketplace_id}")
        
        data = fetch_latest_vendor_inventory_report_json(marketplace_id)
        
        return {
            "status": "ok",
            "marketplace_id": marketplace_id,
            "report_data": data,
        }
    
    except Exception as e:
        logger.error(f"[VendorInventory] Debug request failed: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
        }


@app.get("/api/spapi-tester/meta")
def spapi_tester_meta():
    """
    Return preset endpoints for the tester tab.
    """
    host = ""
    if MARKETPLACE_IDS:
        marketplace = MARKETPLACE_IDS[0].strip()
        host = resolve_vendor_host(marketplace)
    else:
        host = "https://sellingpartnerapi-eu.amazon.com"
    return {
        "host": host,
        "presets": ENDPOINT_PRESETS,
    }


@app.get("/api/perf-stats")
def perf_stats():
    """
    Return the last few timing blocks and current index list for debugging performance.
    """
    return {
        "build": "perf_wave_3B",
        "db_indexes": list_catalog_indexes(),
        "timing_last": get_recent_timings(),
        "status": "ok",
    }


class TesterRequest(BaseModel):
    method: str
    path: str
    query_string: Optional[str] = None
    body_json: Optional[Dict[str, Any]] = None


@app.post("/api/spapi-tester/run")
def spapi_tester_run(req: TesterRequest):
    """
    Proxy a SP-API call (GET/POST) using existing auth. Logs only Amazon's response.
    """
    if not req.path:
        raise HTTPException(status_code=400, detail="path is required")

    method = (req.method or "GET").upper()
    path = req.path if req.path.startswith("/") else f"/{req.path}"
    params = dict(parse_qsl(req.query_string or "", keep_blank_values=True)) if req.query_string else {}

    if not MARKETPLACE_IDS:
        raise HTTPException(status_code=400, detail="MARKETPLACE_IDS not configured")

    marketplace = MARKETPLACE_IDS[0].strip()
    host = resolve_vendor_host(marketplace)
    url = host.rstrip("/") + path

    token = auth_client.get_lwa_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "x-amz-access-token": token,
        "accept": "application/json",
        "user-agent": "spapi-desktop-app/endpoint-tester",
    }

    try:
        resp = requests.request(method, url, headers=headers, params=params, json=req.body_json, timeout=30)
    except Exception as e:
        tester_logger.error(f"[Tester] Error calling {url}: {e}")
        raise HTTPException(status_code=502, detail=f"Request failed: {e}")

    try:
        body = resp.json()
    except ValueError:
        body = resp.text

    try:
        tester_logger.info(
            json.dumps(
                {
                    "method": method,
                    "path": path,
                    "params": params,
                    "status": resp.status_code,
                    "body": body,
                },
                ensure_ascii=False,
            )
        )
    except Exception:
        pass

    return {
        "request": {
            "method": method,
            "path": path,
            "params": params,
            "url": resp.url,
            "body": req.body_json,
        },
        "response": {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": body,
        },
    }


@app.post("/api/po-status/{po_number}")
def update_po_status(po_number: str, payload: PoStatusUpdate):
    """
    Update internal in-house status for a PO, and optionally its Appointment Date.
    This does NOT affect Amazon data; it's only stored locally in po_tracker.json.
    Allowed statuses:
      - Pending
      - Preparing
      - Appointment Scheduled
      - Delivered
    
    Logic:
    - For Appointment Scheduled: always set statusDate (provided value or today)
    - For Delivered: keep existing appointment date if present, else set to provided or today
    - For Pending/Preparing: CLEAR statusDate (set to None)
    """
    allowed = {
        "Pending",
        "Preparing",
        "Appointment Scheduled",
        "Delivered",
    }
    status = (payload.status or "").strip()
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid status")

    appointment_date = payload.appointmentDate

    tracker = load_po_tracker()
    if not isinstance(tracker, dict):
        tracker = {}

    existing = tracker.get(po_number) or {}
    if not isinstance(existing, dict):
        existing = {}

    prev_date = existing.get("appointmentDate")

    existing["status"] = status

    # Handle statusDate logic
    if status == "Appointment Scheduled":
        # For appointment: use provided date or default to today
        if appointment_date:
            existing["appointmentDate"] = appointment_date
        else:
            today = datetime.utcnow().date().isoformat()
            existing["appointmentDate"] = today
    elif status == "Delivered":
        # For delivered: keep existing appointment date if present, else use provided or today
        if prev_date:
            existing["appointmentDate"] = prev_date
        elif appointment_date:
            existing["appointmentDate"] = appointment_date
        else:
            today = datetime.utcnow().date().isoformat()
            existing["appointmentDate"] = today
    else:
        # For Pending/Preparing: clear the statusDate
        existing.pop("appointmentDate", None)

    existing["updatedAt"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    tracker[po_number] = existing
    save_po_tracker(tracker)

    return {
        "ok": True,
        "poNumber": po_number,
        "status": status,
        "appointmentDate": existing.get("appointmentDate"),
    }


@app.get("/api/oos-items")
def get_oos_items():
    """
    Return consolidated Out-of-Stock items (one per ASIN) for the OOS tab.
    Includes export_status field indicating if ASIN was previously exported.
    """
    from services.db import is_asin_exported
    
    state = load_oos_state()
    items = list(state.values())
    catalog = spapi_catalog_status()

    agg: Dict[str, Dict[str, Any]] = {}
    for it in items:
        asin = (it or {}).get("asin")
        if not asin:
            continue
        qty_raw = (it or {}).get("qty")
        try:
            qty_val = float(qty_raw)
        except Exception:
            qty_val = 0
        if qty_val <= 0:
            continue
        entry = agg.get(asin) or {
            "asin": asin,
            "vendorSku": (it or {}).get("vendorSku"),
            "poNumbers": set(),
            "purchaseOrderDate": (it or {}).get("purchaseOrderDate"),
            "shipToPartyId": (it or {}).get("shipToPartyId"),
            "qty": 0,
            "image": (it or {}).get("image"),
            "isOutOfStock": True,
            "export_status": "pending",  # Default to pending
        }
        entry["qty"] = (entry.get("qty") or 0) + qty_val
        if (it or {}).get("poNumber"):
            entry["poNumbers"].add(it.get("poNumber"))
        if not entry.get("image"):
            entry["image"] = (catalog.get(asin) or {}).get("image")
        agg[asin] = entry

    consolidated = []
    for asin, entry in agg.items():
        entry["poNumbers"] = sorted(list(entry.get("poNumbers") or []))
        # Set export_status based on export history
        entry["export_status"] = "exported" if is_asin_exported(asin) else "pending"
        consolidated.append(entry)

    return {"items": consolidated}


@app.get("/api/oos-items/export")
def export_oos_items():
    """
    Export OOS items as a simple XLS-friendly TSV (ASINs only).
    Only includes ASINs with export_status="pending" (not previously exported).
    Records exported ASINs in export history for future reference.
    """
    import uuid
    from services.db import is_asin_exported, mark_oos_asins_exported
    
    state = load_oos_state()
    items = list(state.values())
    pending_asins: list[str] = []
    
    for it in items:
        asin = (it or {}).get("asin")
        if not asin:
            continue
        qty_raw = (it or {}).get("qty")
        try:
            qty_val = float(qty_raw)
        except Exception:
            qty_val = 0
        if qty_val <= 0:
            continue
        # Only include pending ASINs (not yet exported)
        if not is_asin_exported(asin):
            pending_asins.append(asin)

    # Generate export batch ID
    batch_id = str(uuid.uuid4())
    
    # If there are pending ASINs, record them as exported
    if pending_asins:
        mark_oos_asins_exported(pending_asins, batch_id)
    
    # Build CSV with pending ASINs
    output = StringIO()
    writer = csv.writer(output, delimiter="\t")
    writer.writerow(["asin"])
    for asin in sorted(pending_asins):
        writer.writerow([asin])

    data = output.getvalue().encode("utf-8-sig")
    headers_resp = {"Content-Disposition": 'attachment; filename="oos_items.xls"'}
    
    return Response(
        content=data,
        media_type="application/vnd.ms-excel",
        headers=headers_resp
    )


@app.post("/api/oos-items/mark")
def mark_oos_item(payload: Dict[str, Any]):
    """
    Mark a single PO item as out-of-stock.
    Payload:
      {
        "poNumber": "...",
        "asin": "...",
        "vendorSku": "...",
        "purchaseOrderDate": "...",
        "shipToPartyId": "...",
        "qty": <number>,
        "image": "https://..."   # optional
      }
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    po = payload.get("poNumber")
    asin = payload.get("asin")
    if not po or not asin:
        raise HTTPException(status_code=400, detail="poNumber and asin required")

    key = f"{po}::{asin}"
    state = load_oos_state()
    state[key] = {
        "poNumber": po,
        "asin": asin,
        "vendorSku": payload.get("vendorSku"),
        "purchaseOrderDate": payload.get("purchaseOrderDate"),
        "shipToPartyId": payload.get("shipToPartyId"),
        "qty": payload.get("qty"),
        "image": payload.get("image"),
        "isOutOfStock": True,
    }
    save_oos_state(state)
    return {"status": "ok", "key": key}


@app.post("/api/oos-items/restock")
def restock_oos_item(payload: Dict[str, Any]):
    """
    Clear OOS flag (or remove entry) for a single PO item.
    Payload:
      {
        "poNumber": "...",
        "asin": "..."
      }
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    po = payload.get("poNumber")
    asin = payload.get("asin")
    if not asin:
        raise HTTPException(status_code=400, detail="asin required")

    state = load_oos_state()
    removed = 0
    if po:
        key = f"{po}::{asin}"
        if key in state:
            del state[key]
            removed = 1
    else:
        to_delete = [k for k, v in state.items() if (v or {}).get("asin") == asin]
        for k in to_delete:
            del state[k]
        removed = len(to_delete)

    if removed:
        save_oos_state(state)

    return {"status": "ok", "asin": asin, "removed": removed}


@app.get("/api/catalog/asins")
def list_catalog_asins():
    """
    Return unique ASINs from vendor POs with fetched flag from local SP-API catalog DB.
    """
    try:
        asins, sku_map = extract_asins_from_pos()
        fetched = spapi_catalog_status()
    except Exception as exc:
        return JSONResponse({"error": f"Failed to load ASINs: {exc}"}, status_code=500)

    return {
        "items": [
            {
                "asin": asin,
                "fetched": asin in fetched,
                "title": fetched.get(asin, {}).get("title"),
                "image": fetched.get(asin, {}).get("image"),
                "sku": fetched.get(asin, {}).get("sku") or sku_map.get(asin),
                "model": fetched.get(asin, {}).get("model"),
                "barcode": fetched.get(asin, {}).get("barcode"),
            }
            for asin in asins
        ]
    }


@app.post("/api/catalog/fetch/{asin}")
def fetch_catalog_for_asin(asin: str, background_tasks: BackgroundTasks):
    """
    Queue catalog fetch in background and return immediately.
    """
    try:
        fetched = spapi_catalog_status().get(asin)
        if fetched and (fetched.get("title") or fetched.get("image")):
            return {"asin": asin, "status": "cached", "title": fetched.get("title"), "image": fetched.get("image")}
    except Exception as e:
        logger.warning(f"[Catalog] Error checking cache for {asin}: {e}")
    
    background_tasks.add_task(_fetch_catalog_background, asin)
    return {"asin": asin, "status": "queued"}


def _fetch_catalog_background(asin: str):
    """Helper function to fetch catalog in background thread."""
    try:
        fetch_spapi_catalog_item(asin)
        logger.info(f"[Catalog] Background fetch completed for {asin}")
    except HTTPException as e:
        logger.warning(f"[Catalog] Background fetch failed for {asin}: {e.detail}")
    except Exception as e:
        logger.error(f"[Catalog] Unexpected error fetching {asin}: {e}", exc_info=True)


@app.post("/api/catalog/fetch-all")
def fetch_catalog_for_missing(background_tasks: BackgroundTasks):
    """
    Queue catalog fetch for all missing ASINs in background.
    """
    try:
        asins, _ = extract_asins_from_pos()
        fetched = spapi_catalog_status()
        missing = [a for a in asins if a not in fetched]
    except Exception as exc:
        logger.error(f"[Catalog] Error listing missing ASINs: {exc}")
        return {"fetched": 0, "queued": 0, "errors": [{"error": str(exc)}]}
    
    if not missing:
        return {"fetched": 0, "queued": 0, "message": "All ASINs already fetched"}
    
    for asin in missing:
        background_tasks.add_task(_fetch_catalog_background, asin)
    
    logger.info(f"[Catalog] Queued {len(missing)} ASINs for background fetch")
    return {"fetched": 0, "queued": len(missing), "missingTotal": len(missing)}


@app.get("/api/catalog/item/{asin}")
def get_catalog_payload(asin: str):
    """
    Return stored SP-API catalog payload for an ASIN.
    """
    if not CATALOG_DB_PATH.exists():
        return JSONResponse({"error": "Catalog DB missing"}, status_code=404)
    try:
        entry = get_catalog_entry(asin, db_path=CATALOG_DB_PATH)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    if not entry:
        return JSONResponse({"error": "Catalog not found"}, status_code=404)
    payload = parse_catalog_payload(entry.get("payload"))
    return {"asin": asin, "title": entry.get("title"), "image": entry.get("image"), "payload": payload}


@app.post("/api/catalog/update-barcode")
def update_catalog_barcode_endpoint(payload: Dict[str, Any]):
    """
    Manually update barcode for a catalog item (identified by ASIN).
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    asin = (payload.get("asin") or "").strip()
    raw_barcode = (payload.get("barcode") or "").strip()
    if not asin:
        raise HTTPException(status_code=400, detail="asin is required")
    normalized = normalize_barcode(raw_barcode)
    if not normalized:
        raise HTTPException(status_code=400, detail="Invalid barcode. Expect 12-digit UPC or 13-digit EAN numeric value.")
    if not update_catalog_barcode(asin, normalized):
        raise HTTPException(status_code=404, detail="Catalog item not found")
    item = spapi_catalog_status().get(asin) or {}
    item["barcode"] = normalized
    return {"status": "ok", "asin": asin, "barcode": normalized, "item": item}


@app.post("/api/picklist/preview")
def picklist_preview(payload: Dict[str, Any]):
    """
    Consolidate items across selected POs (excluding OOS) for preview.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    po_numbers = payload.get("purchaseOrderNumbers") or []
    if not isinstance(po_numbers, list) or not all(isinstance(p, str) for p in po_numbers):
        raise HTTPException(status_code=400, detail="purchaseOrderNumbers must be a list of strings")
    result = consolidate_picklist(po_numbers)
    return result


@app.post("/api/picklist/pdf")
def picklist_pdf(payload: Dict[str, Any]):
    """
    Consolidate items and return a simple PDF pick list.
    """
    if not REPORTLAB_AVAILABLE:
        raise HTTPException(status_code=500, detail="reportlab is required for PDF generation")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    po_numbers = payload.get("purchaseOrderNumbers") or []
    if not isinstance(po_numbers, list) or not all(isinstance(p, str) for p in po_numbers):
        raise HTTPException(status_code=400, detail="purchaseOrderNumbers must be a list of strings")

    result = consolidate_picklist(po_numbers)
    items = result.get("items") or []
    items.sort(key=lambda x: (0 - (x.get("totalQty") or 0)))
    summary = result.get("summary") or {}

    pdf_bytes = generate_picklist_pdf(po_numbers, items, summary)
    headers = {"Content-Disposition": 'attachment; filename="picklist.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/api/picklist/pdf")
def picklist_pdf_get(poNumbers: str = Query("", description="Comma-separated PO numbers")):
    """
    GET variant to generate pick list PDF via query string (e.g., ?poNumbers=PO1,PO2).
    """
    if not REPORTLAB_AVAILABLE:
        raise HTTPException(status_code=500, detail="reportlab is required for PDF generation")
    po_numbers = [p.strip() for p in (poNumbers or "").split(",") if p.strip()]
    if not po_numbers:
        raise HTTPException(status_code=400, detail="poNumbers query parameter is required")
    # Reuse existing consolidation and PDF generation logic
    result = consolidate_picklist(po_numbers)
    items = result.get("items") or []
    items.sort(key=lambda x: (0 - (x.get("totalQty") or 0)))
    summary = result.get("summary") or {}

    pdf_bytes = generate_picklist_pdf(po_numbers, items, summary)
    headers = {"Content-Disposition": 'attachment; filename="picklist.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/api/debug/sample-po")
def debug_sample_po():
    """
    Return the first PO item from cache for debugging purposes.
    """
    try:
        data = load_vendor_pos_cache(VENDOR_POS_CACHE, raise_on_error=True)
    except Exception:
        return {"message": "no items in cache"}
    if not data:
        return {"message": "no items in cache"}
    normalized = normalize_pos_entries(data)
    for po in normalized:
        details = po.get("orderDetails") or {}
        items = details.get("items") or []
        if items:
            raw_item = items[0]
            po_number = po.get("purchaseOrderNumber")
            keys_preview = list(raw_item.keys())[:8]
            print(f"[debug sample-po] po={po_number}, item keys={keys_preview}")
            return {"poNumber": po_number, "rawItem": raw_item}
    return {"message": "no items in cache"}


@app.get("/api/debug/catalog-sample/{asin}")
def debug_catalog_sample(asin: str):
    """
    Return selected fields from stored catalog payload for debugging.
    """
    if not CATALOG_DB_PATH.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        entry = get_catalog_entry(asin, db_path=CATALOG_DB_PATH)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    if not entry:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload = parse_catalog_payload(entry.get("payload"), include_raw=False)
    result = {
        "asin": asin,
        "attributeSets": payload.get("attributeSets"),
        "summaries": payload.get("summaries"),
        "rawKeys": list(payload.keys()) if isinstance(payload, dict) else [],
    }
    print(f"[debug catalog-sample] asin={asin}, keys={result['rawKeys'][:8]}")
    return result


def debug_dump_vendor_po(po_number: str, output_path: str = None):
    """
    Fetch raw JSON for a single vendor PO from SP-API and dump to file.
    
    Args:
        po_number: PO number to fetch
        output_path: File path to write JSON. Defaults to debug_po_{po_number}.json
    
    Usage:
        python main.py --debug-po 8768LE6D
    """
    if not output_path:
        output_path = f"debug_po_{po_number}.json"
    
    detailed_po = fetch_detailed_po_with_status(po_number)
    if not detailed_po:
        print(f"[DebugDump] Failed to fetch PO {po_number}")
        return
    
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(detailed_po, f, indent=2, ensure_ascii=False)
        print(f"[DebugDump] Dumped PO {po_number} to {output_path}")
        
        # Also print item count and structure preview
        items = detailed_po.get("itemStatus", [])
        print(f"[DebugDump] PO has {len(items)} items")
        if items and len(items) > 0:
            first_item = items[0]
            print(f"[DebugDump] First item keys: {list(first_item.keys())}")
            if 'orderedQuantity' in first_item:
                oq = first_item['orderedQuantity']
                print(f"[DebugDump] orderedQuantity structure: {json.dumps(oq, ensure_ascii=False, indent=2)[:300]}")
            if 'receivingStatus' in first_item:
                rs = first_item['receivingStatus']
                print(f"[DebugDump] receivingStatus structure: {json.dumps(rs, ensure_ascii=False, indent=2)[:300]}")
            if 'acknowledgementStatus' in first_item:
                acks = first_item['acknowledgementStatus']
                print(f"[DebugDump] acknowledgementStatus structure: {json.dumps(acks, ensure_ascii=False, indent=2)[:300]}")
    except Exception as e:
        logger.error(f"[DebugDump] Error dumping PO {po_number}: {e}", exc_info=True)
        print(f"[DebugDump] Error: {e}")


def init_vendor_po_lines_table():
    """Create vendor_po_lines table if it doesn't exist."""
    db_repos.init_vendor_po_lines_table()


def verify_vendor_po_mapping(po_number: str):
    """
    Verify vendor PO quantity mapping by comparing SP-API raw JSON totals
    against database aggregates.
    
    Steps:
    1. Fetch raw PO JSON from SP-API
    2. Extract and aggregate line quantities using CORRECT schema fields
    3. Query database aggregates for the same PO
    4. Log comparison report to console
    
    SP-API Schema (Vendor Orders API):
    CASE 1: If itemStatus[] available (full status):
    - itemStatus[].orderedQuantity.orderedQuantity.amount = Original ordered qty
    - itemStatus[].orderedQuantity.cancelledQuantity.amount = Cancelled qty
    - itemStatus[].acknowledgementStatus.acceptedQuantity.amount = Accepted/confirmed qty
    - itemStatus[].receivingStatus.receivedQuantity.amount = Received qty
    
    CASE 2: If itemStatus[] NOT available (fallback to items):
    - orderDetails.items[].orderedQuantity.amount = Ordered qty
    - No other status data available; accepted = ordered, received = 0
    - pending = 0, shortage = 0
    
    Calculations:
    - pending = accepted - received (qty awaiting delivery)
    - shortage = ordered - accepted - cancelled (qty not confirmed)
    """
    # Fetch raw PO from SP-API
    detailed_po = fetch_detailed_po_with_status(po_number)
    if not detailed_po:
        print(f"[VerifyPO {po_number}] ERROR: Could not fetch PO from SP-API")
        return

    item_status_list = detailed_po.get("itemStatus", [])
    use_item_status = bool(item_status_list)

    if not use_item_status:
        item_status_list = detailed_po.get("orderDetails", {}).get("items", [])
        if not item_status_list:
            print(f"[VerifyPO {po_number}] ERROR: No itemStatus or items in response")
            return

    # Extract quantities from raw JSON
    api_ordered_total = 0
    api_accepted_total = 0
    api_cancelled_total = 0
    api_received_total = 0
    api_pending_total = 0
    api_shortage_total = 0

    data_source = "itemStatus" if use_item_status else "orderDetails.items (fallback)"
    print(f"\\n[VerifyPO {po_number}] ===== SP-API LINE DETAILS (from {data_source}) =====")

    for idx, item in enumerate(item_status_list, 1):
        item_seq = item.get("itemSequenceNumber", "?")
        asin = item.get("amazonProductIdentifier", "?")

        if use_item_status:
            ordered = 0
            oq_obj = item.get("orderedQuantity", {})
            if isinstance(oq_obj, dict):
                oq_inner = oq_obj.get("orderedQuantity", {})
                if isinstance(oq_inner, dict):
                    ordered = int(oq_inner.get("amount", 0) or 0)

            cancelled = 0
            if isinstance(oq_obj, dict):
                can_inner = oq_obj.get("cancelledQuantity", {})
                if isinstance(can_inner, dict):
                    cancelled = int(can_inner.get("amount", 0) or 0)

            accepted = 0
            ack_obj = item.get("acknowledgementStatus", {})
            if isinstance(ack_obj, dict):
                acc_qty = ack_obj.get("acceptedQuantity", {})
                if isinstance(acc_qty, dict):
                    accepted = int(acc_qty.get("amount", 0) or 0)

            received = 0
            recv_obj = item.get("receivingStatus", {})
            if isinstance(recv_obj, dict):
                recv_qty = recv_obj.get("receivedQuantity", {})
                if isinstance(recv_qty, dict):
                    received = int(recv_qty.get("amount", 0) or 0)
        else:
            ordered = 0
            oq = item.get("orderedQuantity", {})
            if isinstance(oq, dict):
                ordered = int(oq.get("amount", 0) or 0)

            cancelled = 0
            accepted = ordered
            received = 0

        pending = max(0, accepted - received)
        shortage = max(0, ordered - accepted - cancelled)

        print(
            f"  [Item {idx} seq={item_seq} asin={asin}] "
            f"ordered={ordered} accepted={accepted} cancelled={cancelled} "
            f"received={received} pending={pending} shortage={shortage}"
        )

        api_ordered_total += ordered
        api_accepted_total += accepted
        api_cancelled_total += cancelled
        api_received_total += received
        api_pending_total += pending
        api_shortage_total += shortage

    print(
        f"[VerifyPO {po_number}] SP-API TOTALS: "
        f"ordered={api_ordered_total} accepted={api_accepted_total} "
        f"cancelled={api_cancelled_total} received={api_received_total} "
        f"pending={api_pending_total} shortage={api_shortage_total}"
    )

    try:
        totals = db_repos.get_vendor_po_line_totals_for_po(po_number)
    except Exception as exc:
        logger.error(f"[VerifyPO {po_number}] Error querying database: {exc}", exc_info=True)
        print(f"[VerifyPO {po_number}] ERROR: {exc}")
        return

    if not totals:
        print(f"[VerifyPO {po_number}] ERROR: No rows found in database for this PO")
        return

    db_ordered = totals.get("total_ordered", 0)
    db_accepted = totals.get("total_accepted", 0)
    db_cancelled = totals.get("total_cancelled", 0)
    db_received = totals.get("total_received", 0)
    db_pending = totals.get("total_pending", 0)
    db_shortage = totals.get("total_shortage", 0)

    print(
        f"[VerifyPO {po_number}] DB TOTALS: "
        f"ordered={db_ordered} accepted={db_accepted} "
        f"cancelled={db_cancelled} received={db_received} "
        f"pending={db_pending} shortage={db_shortage}"
    )

    print(f"\\n[VerifyPO {po_number}] ===== COMPARISON =====")
    ordered_match = "OK" if api_ordered_total == db_ordered else f"? (api={api_ordered_total} vs db={db_ordered})"
    accepted_match = "OK" if api_accepted_total == db_accepted else f"? (api={api_accepted_total} vs db={db_accepted})"
    cancelled_match = "OK" if api_cancelled_total == db_cancelled else f"? (api={api_cancelled_total} vs db={db_cancelled})"
    received_match = "OK" if api_received_total == db_received else f"? (api={api_received_total} vs db={db_received})"
    pending_match = "OK" if api_pending_total == db_pending else f"? (api={api_pending_total} vs db={db_pending})"
    shortage_match = "OK" if api_shortage_total == db_shortage else f"? (api={api_shortage_total} vs db={db_shortage})"

    print(f"  ordered:   {ordered_match}")
    print(f"  accepted:  {accepted_match}")
    print(f"  cancelled: {cancelled_match}")
    print(f"  received:  {received_match}")
    print(f"  pending:   {pending_match}")
    print(f"  shortage:  {shortage_match}")



def _sync_vendor_po_lines_for_po(po_number: str):
    """
    Sync vendor_po_lines for a single PO using correct SP-API schema mapping.
    
    IMPORTANT: Quantity Mapping (from Vendor Orders API schema)
    =========================================================
    CASE 1: If itemStatus[] is available (PO has been acknowledged):
    - orderedQuantity.orderedQuantity.amount = Original ordered quantity
    - orderedQuantity.cancelledQuantity.amount = Cancelled quantity
    - acknowledgementStatus.acceptedQuantity.amount = Accepted/confirmed quantity
    - receivingStatus.receivedQuantity.amount = Received quantity
    
    CASE 2: If itemStatus[] is NOT available (fallback to orderDetails.items):
    - Use orderDetails.items[].orderedQuantity.amount as ordered quantity
    - No acknowledgement/receiving data available yet
    - Set accepted_qty = ordered_qty, received_qty = 0, pending = 0, shortage = 0
    
    Derived Calculations:
    - pending_qty = accepted_qty - received_qty (awaiting delivery)
    - shortage_qty = ordered_qty - accepted_qty - cancelled_qty (not confirmed by vendor)
    
    This matches Amazon Vendor Central terminology:
    - Quantity Submitted = orderedQuantity (what was ordered)
    - Accepted quantity = acknowledgementStatus.acceptedQuantity (what vendor confirmed)
    - Quantity received = receivingStatus.receivedQuantity (what was received)
    - Quantity outstanding = pending_qty (confirmed but not yet received)
    """
    # Fetch detailed PO from SP-API
    detailed_po = fetch_detailed_po_with_status(po_number)
    if not detailed_po:
        logger.warning(f"[VendorPO] Could not fetch detailed PO {po_number}")
        return
    
    ship_to_party = (
        detailed_po.get("orderDetails", {}).get("shipToParty")
        or detailed_po.get("shipToParty", {})
        or {}
    )
    ship_to_location = ship_to_party.get("partyId", "")
    
    # FIX: Clear only this PO's lines (scoped delete)
    try:
        with time_block("vendor_po_lines.clear_po"):
            db_repos.delete_vendor_po_lines_for_po(po_number)
    except Exception as e:
        logger.error(f"[VendorPO] Failed to clear lines for PO {po_number}: {e}")
        return
    
    # Try itemStatus first, then items (full status often lives under items in purchaseOrders payload)
    item_status_list = detailed_po.get("itemStatus") or detailed_po.get("items") or []
    use_item_status = bool(item_status_list)

    # Fallback to orderDetails.items if neither present
    if not use_item_status:
        item_status_list = detailed_po.get("orderDetails", {}).get("items", [])
        if not item_status_list:
            logger.warning(f"[VendorPO] PO {po_number} has neither itemStatus nor items")
            return
        logger.info(f"[VendorPO] PO {po_number} using fallback orderDetails.items (no itemStatus available)")
    else:
        logger.info(f"[VendorPO] PO {po_number} has detailed items ({len(item_status_list)} items)")
    
    # Process each item
    now_utc = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    rows_to_insert: List[Tuple[Any, ...]] = []
    for item in item_status_list:
        try:
            item_seq = item.get("itemSequenceNumber", "")
            asin = item.get("amazonProductIdentifier") or item.get("buyerProductIdentifier") or ""
            sku = item.get("vendorProductIdentifier", "")
            
            if use_item_status:
                # ============================================================
                # CASE 1: Using itemStatus/items with full acknowledgement/receiving data
                # ============================================================
                # Extract ORDERED quantity (from orderedQuantity.orderedQuantity)
                ordered_qty = 0
                oq_wrapper = item.get("orderedQuantity", {})
                if isinstance(oq_wrapper, dict):
                    oq_inner = oq_wrapper.get("orderedQuantity", {})
                    if isinstance(oq_inner, dict):
                        ordered_qty = int(oq_inner.get("amount", 0) or 0)

                # Extract CANCELLED quantity (from orderedQuantity.cancelledQuantity)
                cancelled_qty = 0
                if isinstance(oq_wrapper, dict):
                    can_inner = oq_wrapper.get("cancelledQuantity", {})
                    if isinstance(can_inner, dict):
                        cancelled_qty = int(can_inner.get("amount", 0) or 0)

                # Extract ACCEPTED quantity (from acknowledgementStatus.acceptedQuantity or rejectedQuantity)
                accepted_qty = 0
                ack_obj = item.get("acknowledgementStatus", {})
                if isinstance(ack_obj, dict):
                    acc_qty = ack_obj.get("acceptedQuantity", {})
                    rej_qty = ack_obj.get("rejectedQuantity", {})
                    if isinstance(acc_qty, dict):
                        accepted_qty = int(acc_qty.get("amount", 0) or 0)
                    if isinstance(rej_qty, dict):
                        cancelled_qty += int(rej_qty.get("amount", 0) or 0)

                # Extract RECEIVED quantity (from receivingStatus.receivedQuantity)
                received_qty = 0
                pending_qty = 0
                recv_obj = item.get("receivingStatus", {}) or {}
                if isinstance(recv_obj, dict):
                    recv_qty = recv_obj.get("receivedQuantity", {})
                    if isinstance(recv_qty, dict):
                        received_qty = int(recv_qty.get("amount", 0) or 0)
                    pending_obj = recv_obj.get("pendingQuantity", {})
                    if isinstance(pending_obj, dict):
                        pending_qty = int(pending_obj.get("amount", 0) or 0)

                    # If API pending not provided, derive from accepted - received
                    if pending_qty == 0:
                        pending_qty = max(0, accepted_qty - received_qty)

            else:
                # ============================================================
                # CASE 2: Fallback using orderDetails.items (minimal data)
                # ============================================================
                ordered_qty = 0
                oq = item.get("orderedQuantity", {})
                if isinstance(oq, dict):
                    ordered_qty = int(oq.get("amount", 0) or 0)
                cancelled_qty = 0
                accepted_qty = ordered_qty
                received_qty = 0
                pending_qty = max(0, accepted_qty - received_qty)

            # ============================================================
            # Calculate DERIVED quantities
            # ============================================================
            # pending_qty already derived above; ensure non-negative
            pending_qty = max(0, pending_qty)

            shortage_qty = max(0, ordered_qty - accepted_qty - cancelled_qty)

            rows_to_insert.append(
                (
                    po_number,
                    ship_to_location,
                    asin,
                    sku,
                    ordered_qty,
                    accepted_qty,
                    cancelled_qty,
                    0,  # shipped_qty not provided by vendor orders API
                    received_qty,
                    shortage_qty,
                    pending_qty,
                    now_utc,
                )
            )

        except Exception as e:
            logger.error(f"[VendorPO] Error processing item {item_seq} in PO {po_number}: {e}", exc_info=True)
            continue

    if rows_to_insert:
        with time_block(f"vendor_po_lines.bulk_insert:{len(rows_to_insert)}"):
            db_repos.bulk_insert_vendor_po_lines(rows_to_insert)
    logger.info(f"[VendorPO] Synced {len(rows_to_insert)} lines for PO {po_number}")


def get_shipments_for_po(po_number: str) -> List[Dict[str, Any]]:
    """
    Fetch all vendor shipments related to a single PO number from Vendor Shipments API.
    
    Schema Reference (Vendor Shipments API):
    - Filter by: buyerReferenceNumber (PO number)
    - Shipment has: purchaseOrders[].purchaseOrderNumber, purchaseOrders[].items[]
    - Per-item: vendorProductIdentifier, buyerProductIdentifier, shippedQuantity.amount
    - Response pagination: nextToken
    
    Returns normalized list of line records:
        {
            "po_number": str,
            "shipment_id": str,
            "asin": str,
            "vendor_sku": str,
            "shipped_qty": int,
            "received_qty": int,
        }
    """
    if not MARKETPLACE_IDS:
        logger.warning("[Shipments] No MARKETPLACE_IDS configured")
        return []
    
    try:
        marketplace = MARKETPLACE_IDS[0].strip()
        host = resolve_vendor_host(marketplace)
        url = f"{host}/vendor/shipping/v1/shipments"
        token = auth_client.get_lwa_access_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "x-amz-access-token": token,
            "accept": "application/json",
            "user-agent": "sp-api-desktop-app/1.0",
        }
        all_lines: List[Dict[str, Any]] = []
        next_token: Optional[str] = None
        
        while True:
            params = {
                "buyerReferenceNumber": po_number,
                "limit": 50,
            }
            if next_token:
                params["nextToken"] = next_token
            
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=20)
            except requests.exceptions.Timeout:
                logger.warning(f"[Shipments] Timeout fetching shipments for PO {po_number}")
                break
            except requests.exceptions.RequestException as e:
                logger.warning(f"[Shipments] Error fetching shipments for PO {po_number}: {e}")
                break
            
            if resp.status_code == 200:
                data = resp.json()
                payload = data.get("payload") or {}
                shipments = payload.get("shipments") or []
                
                # Vendor Shipments fields: filter with buyerReferenceNumber, then pull
                # purchaseOrders[].items[].{buyerProductIdentifier, vendorProductIdentifier, shippedQuantity.amount}
                for shipment in shipments:
                    shipment_id = shipment.get("vendorShipmentIdentifier", "")
                    purchase_orders = shipment.get("purchaseOrders") or []
                    
                    for po_info in purchase_orders:
                        po_num = po_info.get("purchaseOrderNumber") or ""
                        if po_num != po_number:
                            continue
                        
                        items = po_info.get("items") or []
                        for item in items:
                            asin = item.get("buyerProductIdentifier") or ""
                            sku = item.get("vendorProductIdentifier") or ""
                            
                            shipped_qty = 0
                            sq = item.get("shippedQuantity") or {}
                            if isinstance(sq, dict):
                                shipped_qty = int(sq.get("amount") or 0)
                            
                            # Shipments payload does not carry a separate received quantity, so use shippedQuantity.
                            received_qty = shipped_qty
                            
                            all_lines.append({
                                "po_number": po_number,
                                "shipment_id": shipment_id,
                                "asin": asin,
                                "vendor_sku": sku,
                                "shipped_qty": shipped_qty,
                                "received_qty": received_qty,
                            })
                
                pagination = payload.get("pagination") or {}
                next_token = pagination.get("nextToken")
                if not next_token:
                    break
            elif resp.status_code == 404:
                logger.info(f"[Shipments] No shipments found for PO {po_number} (404)")
                break
            else:
                logger.warning(f"[Shipments] Failed to fetch shipments for PO {po_number}: {resp.status_code}")
                break
        
        return all_lines
    
    except Exception as e:
        logger.warning(f"[Shipments] Error fetching shipments for PO {po_number}: {e}", exc_info=True)
        return []


def aggregate_received_for_po(po_number: str) -> Dict[str, Any]:
    """
    For a given PO, aggregate shipment data by ASIN/vendor_sku.
    
    Returns:
        {
            "po_number": str,
            "lines": [
                {
                    "asin": str,
                    "vendor_sku": str,
                    "total_shipped": int,
                    "total_received": int,
                },
                ...
            ],
            "totals": {
                "shipped": int,
                "received": int,
            }
        }
    """
    shipment_lines = get_shipments_for_po(po_number)
    
    # Group by (asin, vendor_sku)
    grouped: Dict[tuple, Dict[str, Any]] = {}
    total_shipped = 0
    total_received = 0
    
    for line in shipment_lines:
        key = (line.get("asin") or "", line.get("vendor_sku") or "")
        shipped = int(line.get("shipped_qty", 0) or 0)
        received = int(line.get("received_qty", 0) or 0)
        
        if key not in grouped:
            grouped[key] = {
                "asin": line.get("asin") or "",
                "vendor_sku": line.get("vendor_sku") or "",
                "total_shipped": 0,
                "total_received": 0,
            }
        
        grouped[key]["total_shipped"] += shipped
        grouped[key]["total_received"] += received
        total_shipped += shipped
        total_received += received
    
    # Convert to list
    lines_list = list(grouped.values())
    lines_list.sort(key=lambda x: (x.get("vendor_sku") or "", x.get("asin") or ""))
    
    return {
        "po_number": po_number,
        "lines": lines_list,
        "totals": {
            "shipped": total_shipped,
            "received": total_received,
        }
    }


def verify_po_receipts_against_shipments(po_number: str) -> None:
    """
    Compare vendor_po_lines (DB) against Vendor Shipments API for one PO.
    
    Shows per-line and totals comparison:
    - DB: ordered_qty, received_qty from vendor_po_lines table
    - Shipments: shipped/received quantities from Vendor Shipments API
    
    Logs detailed comparison to console.
    """
    print(f"\n[VerifyPOReceipts {po_number}] ===== COMPARING DB vs SHIPMENTS =====")
    print(f"[VerifyPOReceipts {po_number}] Data sources:")
    print(f"  DB (vendor_po_lines): Vendor Orders API -> Ordered/Received from itemStatus")
    print(f"  Shipments API: /vendor/shipping/v1/shipments filtered by buyerReferenceNumber={po_number}")
    
    # Get DB data
    db_lines: Dict[Tuple[str, str], Dict[str, Any]] = {}
    db_ordered_total = 0
    db_received_total = 0
    
    try:
        rows = db_repos.get_vendor_po_lines(po_number)
        for row in rows:
            asin = (row.get("asin") or "").strip()
            sku = (row.get("sku") or "").strip()
            key = (asin, sku)
            ordered_qty = int(row.get("ordered_qty") or 0)
            received_qty = int(row.get("received_qty") or 0)
            db_lines[key] = {
                "asin": asin,
                "sku": sku,
                "ordered_qty": ordered_qty,
                "received_qty": received_qty,
            }
            db_ordered_total += ordered_qty
            db_received_total += received_qty
    except Exception as e:
        logger.error(f"[VerifyPOReceipts {po_number}] Error querying DB: {e}", exc_info=True)
        print(f"[VerifyPOReceipts {po_number}] ERROR querying DB: {e}")
        return
    
    # Get Shipments data
    shipments_agg = aggregate_received_for_po(po_number)
    shipments_lines: Dict[Tuple[str, str], Dict[str, Any]] = {}
    shipments_totals = shipments_agg.get("totals", {})
    shipments_total_shipped = int(shipments_totals.get("shipped", 0) or 0)
    shipments_total_received = int(shipments_totals.get("received", 0) or 0)
    
    for line in shipments_agg["lines"]:
        key = (line.get("asin") or "", line.get("vendor_sku") or "")
        shipments_lines[key] = {
            "asin": line.get("asin") or "",
            "vendor_sku": line.get("vendor_sku") or "",
            "shipped_qty": int(line.get("total_shipped", 0) or 0),
            "received_qty": int(line.get("total_received", 0) or 0),
        }
    
    # Merge and compare (join by ASIN or vendor_sku)
    def _match_key(key: Tuple[str, str], other_keys: List[Tuple[str, str]]):
        asin, sku = key
        if key in other_keys:
            return key
        if asin:
            for ok in other_keys:
                if ok[0] == asin:
                    return ok
        if sku:
            for ok in other_keys:
                if ok[1] == sku:
                    return ok
        return None
    
    def _find_line(line_map: Dict[Tuple[str, str], Dict[str, Any]], lookup: Tuple[str, str]) -> Dict[str, Any]:
        if lookup in line_map:
            return line_map[lookup]
        asin, sku = lookup
        if asin:
            for (a, _), payload in line_map.items():
                if a == asin:
                    return payload
        if sku:
            for (_, s), payload in line_map.items():
                if s == sku:
                    return payload
        return {}
    
    all_keys: set = set()
    
    shipment_keys_list = list(shipments_lines.keys())
    for db_key in db_lines.keys():
        matched = _match_key(db_key, shipment_keys_list)
        all_keys.add(matched or db_key)
    
    db_keys_list = list(db_lines.keys())
    for ship_key in shipments_lines.keys():
        matched = _match_key(ship_key, db_keys_list)
        all_keys.add(matched or ship_key)
    
    print(f"\n[VerifyPOReceipts {po_number}] ===== PER-LINE COMPARISON =====")
    print(f"{'ASIN':<15} {'SKU':<20} {'DB_Ordered':<12} {'DB_Rcvd':<10} {'Ship_Rcvd':<11} {'Delta_R':<8}")
    print("-" * 90)
    
    comparison_rows: List[Dict[str, Any]] = []
    for key in sorted(all_keys):
        db_line = _find_line(db_lines, key)
        ship_line = _find_line(shipments_lines, key)
        
        asin = db_line.get("asin") or ship_line.get("asin", "")
        sku = db_line.get("sku") or ship_line.get("vendor_sku", "")
        
        db_ordered = db_line.get("ordered_qty", 0)
        db_received = db_line.get("received_qty", 0)
        ship_received = ship_line.get("received_qty", 0)
        delta = ship_received - db_received
        
        comparison_rows.append({
            "asin": asin,
            "sku": sku,
            "db_ordered": db_ordered,
            "db_received": db_received,
            "ship_received": ship_received,
            "delta": delta,
        })
        
        delta_str = f"{delta:+d}" if delta != 0 else "0"
        print(f"{asin:<15} {sku:<20} {db_ordered:<12} {db_received:<10} {ship_received:<11} {delta_str:<8}")
    
    print("-" * 90)
    print(f"\n[VerifyPOReceipts {po_number}] ===== TOTALS =====")
    print(f"[VerifyPOReceipts {po_number}] DB (vendor_po_lines):")
    print(f"  total_ordered  = {db_ordered_total}")
    print(f"  total_received = {db_received_total}")
    print(f"[VerifyPOReceipts {po_number}] Shipments API:")
    print(f"  total_shipped  = {shipments_total_shipped}")
    print(f"  total_received = {shipments_total_received}")
    
    delta_received = shipments_total_received - db_received_total
    print(f"[VerifyPOReceipts {po_number}] Delta received (Shipments - DB) = {delta_received:+d}")
    
    if delta_received == 0:
        print(f"[VerifyPOReceipts {po_number}] Received quantities match.")
    else:
        print(f"[VerifyPOReceipts {po_number}] Discrepancy detected: {delta_received:+d} units difference")


def sync_vendor_po_lines_batch(po_numbers: List[str]):
    """
    Sync vendor_po_lines for multiple POs.
    Called after fetching POs from SP-API.
    """
    if not po_numbers:
        return

    init_vendor_po_lines_table()

    def _sync_safe(po_num: str) -> Tuple[str, Optional[Exception]]:
        try:
            _sync_vendor_po_lines_for_po(po_num)
            return po_num, None
        except Exception as exc:
            logger.error(f"[VendorPO] Error syncing lines for PO {po_num}: {exc}")
            return po_num, exc

    async def _run_batch():
        with time_block(f"vendor_po_sync_concurrent:{len(po_numbers)}"):
            return await run_single_arg(_sync_safe, po_numbers, max_concurrency=4)

    try:
        results = asyncio.run(_run_batch())
        errors = [r for _, r in results if r]
        if errors:
            logger.warning(f"[VendorPO] vendor_po_lines sync completed with {len(errors)} errors out of {len(po_numbers)} POs")
    except RuntimeError:
        # Fallback if already in an event loop (should be rare for sync endpoints)
        for po_num in po_numbers:
            _sync_safe(po_num)


def rebuild_all_vendor_po_lines():
    """
    Rebuild vendor_po_lines for ALL existing POs in vendor_pos_cache.json.
    
    This is a maintenance operation to backfill line quantities for POs that may have been
    created before the line-syncing logic was fixed, or to refresh all data.
    
    Steps:
    1. Read vendor_pos_cache.json and normalize PO entries
    2. For each PO:
       - Fetch detailed PO info from SP-API
       - Call _sync_vendor_po_lines_for_po to refresh line data
       - Log progress every ~10% of completion
    3. Report final counts
    
    Does NOT modify vendor_pos_cache.json, only refreshes vendor_po_lines table.
    
    Typical usage:
        python main.py --rebuild-po-lines
    """
    logger.info("[VendorPO] Starting rebuild of vendor_po_lines for ALL POs...")
    print("\n[VendorPO] Rebuilding all vendor PO lines from SP-API...")
    
    # Initialize vendor_po_lines table
    init_vendor_po_lines_table()
    
    # Get all PO numbers from vendor_pos_cache.json
    try:
        if not VENDOR_POS_CACHE.exists():
            logger.info("[VendorPO] vendor_pos_cache.json not found")
            print("[VendorPO] vendor_pos_cache.json not found - no POs to rebuild")
            return
        
        cache_data = load_vendor_pos_cache(VENDOR_POS_CACHE, raise_on_error=True)
        normalized = normalize_pos_entries(cache_data)
        
        # Sort by date (newest first, matching the grid behavior)
        normalized.sort(key=parse_po_date, reverse=True)
        
        po_numbers = [po.get("purchaseOrderNumber") for po in normalized if po.get("purchaseOrderNumber")]
        po_date_map = {
            po.get("purchaseOrderNumber"): (
                po.get("purchaseOrderDate")
                or po.get("orderDetails", {}).get("purchaseOrderDate")
            )
            for po in normalized
            if po.get("purchaseOrderNumber")
        }
        
    except Exception as e:
        logger.error(f"[VendorPO] Failed to read vendor_pos_cache.json: {e}")
        print(f"[ERROR] Failed to read vendor_pos_cache.json: {e}")
        return
    
    if not po_numbers:
        logger.info("[VendorPO] No POs found in vendor_pos_cache.json")
        print("[VendorPO] No POs found in vendor_pos_cache.json")
        return
    
    logger.info(f"[VendorPO] Found {len(po_numbers)} POs to rebuild from cache")
    print(f"[VendorPO] Found {len(po_numbers)} POs to rebuild from cache")
    
    # Rebuild lines for each PO concurrently (bounded)
    def _rebuild_safe(po_num: str) -> Tuple[str, Optional[Exception]]:
        try:
            _sync_vendor_po_lines_for_po(po_num)
            return po_num, None
        except Exception as exc:
            logger.error(f"[VendorPO] Error rebuilding lines for PO {po_num}: {exc}")
            return po_num, exc

    async def _run_rebuild():
        with time_block(f"vendor_po_rebuild_concurrent:{len(po_numbers)}"):
            return await run_single_arg(_rebuild_safe, po_numbers, max_concurrency=4)

    try:
        results = asyncio.run(_run_rebuild())
    except RuntimeError:
        # Fallback if already in an event loop
        results = [_rebuild_safe(po_num) for po_num in po_numbers]

    success_count = sum(1 for _, err in results if err is None)
    error_count = len([1 for _, err in results if err is not None])
    
    try:
        added_oos = seed_oos_from_rejected_lines(po_numbers, po_date_map)
        if added_oos:
            logger.info(f"[VendorPO] Seeded {added_oos} rejected lines into OOS after rebuild")
        added_payload = seed_oos_from_rejected_payload(normalized)
        if added_payload:
            logger.info(f"[VendorPO] Seeded {added_payload} rejected payload lines into OOS after rebuild")
    except Exception as e:
        logger.warning(f"[VendorPO] Could not seed OOS from rejected lines: {e}")

    # Final summary
    try:
        line_count = db_repos.count_vendor_po_lines()
    except Exception as e:
        logger.warning(f"[VendorPO] Could not query final line count: {e}")
        line_count = 0
    
    summary = (
        f"[VendorPO] Rebuild complete: {success_count} POs processed, "
        f"{error_count} errors, {line_count} total vendor_po_lines rows"
    )
    logger.info(summary)
    print(f"[COMPLETE] {summary}")
    if error_count > 0:
        print(f"[WARNING] {error_count} errors encountered (see logs for details)")


if __name__ == "__main__":
    import sys
    
    # Check for CLI arguments for maintenance operations
    if "--rebuild-po-lines" in sys.argv:
        # Maintenance: rebuild all PO lines from existing POs in vendor_pos
        # Useful after schema changes or to backfill older POs
        rebuild_all_vendor_po_lines()
        sys.exit(0)
    
    # Debug: dump raw JSON for a specific PO
    if "--debug-po" in sys.argv:
        try:
            idx = sys.argv.index("--debug-po")
            po_number = sys.argv[idx + 1]
            debug_dump_vendor_po(po_number)
            sys.exit(0)
        except (IndexError, ValueError) as e:
            print(f"Usage: python main.py --debug-po <PO_NUMBER>")
            sys.exit(1)
    
    # Verify: check mapping against SP-API
    if "--verify-po" in sys.argv:
        try:
            idx = sys.argv.index("--verify-po")
            po_number = sys.argv[idx + 1]
            verify_vendor_po_mapping(po_number)
            sys.exit(0)
        except (IndexError, ValueError) as e:
            print(f"Usage: python main.py --verify-po <PO_NUMBER>")
            sys.exit(1)
    
    # Verify receipts: compare vendor_po_lines (DB) against Vendor Shipments API
    if "--verify-po-receipts" in sys.argv:
        try:
            idx = sys.argv.index("--verify-po-receipts")
            po_number = sys.argv[idx + 1]
            verify_po_receipts_against_shipments(po_number)
            sys.exit(0)
        except (IndexError, ValueError) as e:
            print(f"Usage: python main.py --verify-po-receipts <PO_NUMBER>")
            sys.exit(1)
    
    # Normal mode: start the FastAPI server
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
