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

# =============================================
#  SP-API DESKTOP APP - MINIMAL ENTRYPOINT
# =============================================

import json
import os
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from io import BytesIO
import threading
import time
from urllib.parse import parse_qsl
from endpoint_presets import ENDPOINT_PRESETS

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
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from auth.spapi_auth import SpApiAuth
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from routes import forecast_blacklist, forecast_api

# --- Logging configuration ---
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_FILE_PATH = LOG_DIR / "spapi_backend.log"
SPAPI_TESTER_LOG_PATH = LOG_DIR / "spapi_tester.log"
ACK_LOG_PATH = LOG_DIR / "vendor_ack_log.jsonl"

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

schema_logger = logging.getLogger("forecast_schema")
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------------
# UI
# -------------------------------
UI_DIR = Path(__file__).parent / "ui"
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=TEMPLATE_DIR)

app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(forecast_blacklist.router)
app.include_router(forecast_api.router)

FORECAST_AUTO_SYNC_INTERVAL_MINUTES = 120  # user can change

@app.get("/")
def home():  # simple root
    return {"status": "running", "message": "Fresh start - add your endpoints here"}


@app.get("/ui/index.html")
def ui_index():
    index_path = UI_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(index_path)


@app.get("/forecast", response_class=HTMLResponse)
async def forecast_page(request: Request):
    """
    Render the Forecast dashboard tab.
    """
    return templates.TemplateResponse(
        "forecast.html",
        {
            "request": request,
            "active_tab": "forecast",
        },
    )


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
SAFETY_BUFFER_DAYS = 7
FORECAST_HORIZON_WEEKS = 8
INBOUND_WINDOW_DAYS = 30
SALES_MIN_WINDOW_DAYS = 7


def resolve_catalog_host(marketplace_id: str) -> str:
    """
    Resolve the correct SP-API host for Catalog API calls based on marketplace.
    Reuses resolve_vendor_host to ensure consistency across all SP-API calls.
    """
    return resolve_vendor_host(marketplace_id)


def load_asin_cache() -> Dict[str, Any]:
    if not ASIN_CACHE_PATH.exists():
        return {}
    try:
        cache = json.loads(ASIN_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            return {}
        # Drop empty entries that lack both title and image.
        pruned = {k: v for k, v in cache.items() if isinstance(v, dict) and (v.get("title") or v.get("image"))}
        return pruned
    except Exception:
        return {}


def save_asin_cache(cache: Dict[str, Any]):
    try:
        ASIN_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    except Exception:
        pass


def load_po_tracker() -> Dict[str, Any]:
    """
    Load internal PO status tracker from po_tracker.json.
    Structure: { "<po_number>": { "status": "...", "updatedAt": "..." }, ... }
    """
    if not PO_TRACKER_PATH.exists():
        return {}
    try:
        data = json.loads(PO_TRACKER_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_po_tracker(tracker: Dict[str, Any]) -> None:
    """
    Persist internal PO status tracker to po_tracker.json.
    """
    try:
        PO_TRACKER_PATH.write_text(
            json.dumps(tracker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_oos_state() -> Dict[str, Any]:
    if not OOS_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(OOS_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_oos_state(state: Dict[str, Any]) -> None:
    try:
        OOS_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def get_latest_po_date_from_cache() -> str | None:
    if not VENDOR_POS_CACHE.exists():
        return None
    try:
        data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
    normalized = normalize_pos_entries(data)
    if not normalized:
        return None
    latest = max((parse_po_date(po) for po in normalized), default=datetime.min)
    if latest == datetime.min:
        return None
    return latest.replace(microsecond=0).isoformat() + "Z"


def init_catalog_db():
    CATALOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spapi_catalog (
                asin TEXT PRIMARY KEY,
                title TEXT,
                image TEXT,
                payload TEXT,
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spapi_catalog_meta (
                asin TEXT PRIMARY KEY,
                sku TEXT
            )
            """
        )
        conn.commit()
    init_forecast_tables()


def init_forecast_tables():
    """
    Ensure all forecast-related tables and useful indexes exist in catalog.db.

    Tables:
      - vendor_forecast
      - vendor_sales_history
      - vendor_rt_inventory
      - forecast_blacklist
      - report_jobs

    This function is SAFE to call repeatedly; all CREATE statements use
    IF NOT EXISTS.
    """
    schema_logger.info("[ForecastSchema] Ensuring forecast tables and indexes exist")
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        # -------------------------
        # Forecast table
        # -------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_forecast (
                id INTEGER PRIMARY KEY,
                asin TEXT NOT NULL,
                marketplace_id TEXT NOT NULL,
                forecast_generation_date TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                mean_units REAL NOT NULL,
                p70_units REAL NOT NULL,
                p80_units REAL NOT NULL,
                p90_units REAL NOT NULL,
                UNIQUE (asin, marketplace_id, start_date, end_date)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vendor_forecast_asin_window
            ON vendor_forecast (asin, marketplace_id, start_date, end_date)
            """
        )

        # -------------------------
        # Sales history table
        # -------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_sales_history (
                id INTEGER PRIMARY KEY,
                asin TEXT NOT NULL,
                marketplace_id TEXT NOT NULL,
                sales_date TEXT NOT NULL,
                units REAL NOT NULL,
                revenue REAL NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (asin, marketplace_id, sales_date)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vendor_sales_history_asin_date
            ON vendor_sales_history (asin, marketplace_id, sales_date)
            """
        )

        # -------------------------
        # Real-time inventory table
        # -------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_rt_inventory (
                asin TEXT PRIMARY KEY,
                marketplace_id TEXT NOT NULL,
                snapshot_time TEXT NOT NULL,
                highly_available_inventory INTEGER NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vendor_rt_inventory_asin
            ON vendor_rt_inventory (asin)
            """
        )

        # -------------------------
        # Forecast blacklist
        # -------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forecast_blacklist (
                asin TEXT NOT NULL,
                marketplace_id TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (asin, marketplace_id)
            )
            """
        )

        # -------------------------
        # Report jobs for SP-API reports
        # -------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS report_jobs (
                id INTEGER PRIMARY KEY,
                report_type TEXT NOT NULL,
                date_start TEXT NOT NULL,
                date_end TEXT NOT NULL,
                report_id TEXT,
                document_id TEXT,
                status TEXT NOT NULL,
                params_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_checked_at TEXT,
                error_message TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_report_jobs_unique
            ON report_jobs (report_type, date_start, date_end)
            """
        )

        conn.commit()
    schema_logger.info("[ForecastSchema] Forecast tables and indexes are up to date")


def upsert_spapi_catalog(asin: str, payload: Dict[str, Any]):
    if not asin:
        return
    init_catalog_db()
    summaries = payload.get("summaries") or []
    title = None
    image = None
    sku = None
    if summaries and isinstance(summaries, list):
        first = summaries[0] or {}
        title = first.get("itemName") or first.get("displayName") or first.get("title")
        sku = first.get("manufacturerPartNumber") or first.get("modelNumber")
    images = payload.get("images") or []
    if images and isinstance(images, list):
        first_img = images[0] or {}
        variants = first_img.get("variants") or []
        if variants and isinstance(variants, list):
            image = (variants[0] or {}).get("link")
    vendor_details = payload.get("vendorDetails") or []
    if vendor_details and isinstance(vendor_details, list):
        sku = vendor_details[0].get("vendorSKU") or sku
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO spapi_catalog (asin, title, image, payload, fetched_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (asin, title, image, json.dumps(payload, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO spapi_catalog_meta (asin, sku) VALUES (?, ?)",
            (asin, sku),
        )
        conn.commit()


def upsert_spapi_catalog_minimal(asin: str, title: str = None, image: str = None, sku: str = None, payload: Dict[str, Any] = None):
    """
    Store minimal record for an ASIN if we don't have a full SP-API payload yet.
    Preserves existing payload/title/image if already stored.
    """
    if not asin:
        return
    init_catalog_db()
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        cur = conn.execute("SELECT title, image, payload FROM spapi_catalog WHERE asin = ?", (asin,))
        row = cur.fetchone()
        existing_title, existing_image, existing_payload = (row or (None, None, None))
        new_title = title or existing_title
        new_image = image or existing_image
        payload_to_store = payload if payload is not None else (json.loads(existing_payload) if existing_payload else {})
        conn.execute(
            """
            INSERT OR REPLACE INTO spapi_catalog (asin, title, image, payload, fetched_at)
            VALUES (?, ?, ?, ?, COALESCE((SELECT fetched_at FROM spapi_catalog WHERE asin = ?), CURRENT_TIMESTAMP))
            """,
            (asin, new_title, new_image, json.dumps(payload_to_store, ensure_ascii=False), asin),
        )
        if sku:
            conn.execute(
                "INSERT OR REPLACE INTO spapi_catalog_meta (asin, sku) VALUES (?, ?)",
                (asin, sku),
            )
        conn.commit()


def spapi_catalog_status() -> Dict[str, Dict[str, Any]]:
    if not CATALOG_DB_PATH.exists():
        return {}
    updates = []
    results = {}
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT c.asin, c.title, c.image, c.payload, m.sku
            FROM spapi_catalog c
            LEFT JOIN spapi_catalog_meta m ON c.asin = m.asin
            """
        ).fetchall()
        for asin, title, image, payload_raw, sku in rows:
            parsed = None
            model_number = None
            if (not title or not image) and payload_raw:
                try:
                    parsed = json.loads(payload_raw)
                    # Reuse parser from upsert
                    sums = parsed.get("summaries") or []
                    for s in sums:
                        if not isinstance(s, dict):
                            continue
                        title = title or s.get("itemName") or s.get("displayName") or s.get("title")
                        main_img = s.get("mainImage") or {}
                        if isinstance(main_img, dict):
                            image = image or main_img.get("link")
                    imgs = parsed.get("images") or []
                    for img in imgs:
                        if not isinstance(img, dict):
                            continue
                        image = image or img.get("link")
                        variants = img.get("variants") or []
                        if variants and isinstance(variants, list):
                            image = image or (variants[0] or {}).get("link")
                        nested = img.get("images") or []
                        if nested and isinstance(nested, list):
                            image = image or (nested[0] or {}).get("link")
                    attr_sets = parsed.get("attributeSets") or []
                    for attrs in attr_sets:
                        if isinstance(attrs, dict):
                            title = title or attrs.get("title")
                            model_number = model_number or attrs.get("modelNumber")
                except Exception:
                    parsed = None
            results[asin] = {"title": title, "image": image, "sku": sku, "model": model_number or sku}
            if parsed is not None:
                updates.append((title, image, asin))
        if updates:
            conn.executemany("UPDATE spapi_catalog SET title = ?, image = ? WHERE asin = ?", updates)
            conn.commit()
    return results

# Ensure DB exists at import time
init_catalog_db()

# Migrate vendor_po_lines schema if needed
try:
    from migrate_vendor_po_schema import migrate_vendor_po_lines_schema
    migrate_vendor_po_lines_schema()
except Exception as e:
    logger.warning(f"[Startup] Schema migration skipped or failed (non-critical): {e}")




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


def fetch_catalog_info(asin: str):
    """
    Return {"title": ..., "image": ...} for an ASIN using local catalog DB only.
    """
    info = spapi_catalog_status().get(asin)
    if not info:
        return None
    return {"title": info.get("title"), "image": info.get("image")}


def extract_asins_from_pos() -> Tuple[List[str], Dict[str, str]]:
    """
    Collect unique ASINs from vendor_pos_cache.json.
    """
    if not VENDOR_POS_CACHE.exists():
        return [], {}
    try:
        data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
    except Exception:
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
                looked_up.add(asin)
                continue
            looked_up.add(asin)


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


class AckLine(BaseModel):
    itemSequenceNumber: str
    buyerProductIdentifier: Optional[str] = None
    vendorProductIdentifier: Optional[str] = None
    confirmationStatus: str  # ACCEPTED or REJECTED
    acceptedQuantity: int = 0
    rejectedQuantity: int = 0
    rejectionReason: Optional[str] = None


class AckRequest(BaseModel):
    purchaseOrderNumber: str
    shipFromPartyId: Optional[str] = None
    items: List[AckLine]


def _quant(amount: int) -> Dict[str, Any]:
    return {"amount": int(amount), "unitOfMeasure": "Eaches", "unitSize": 1}


def build_ack_payload(req: AckRequest) -> Dict[str, Any]:
    if not MARKETPLACE_IDS:
        raise HTTPException(status_code=400, detail="MARKETPLACE_IDS not configured")

    po = fetch_detailed_po_with_status(req.purchaseOrderNumber)
    if not po:
        raise HTTPException(status_code=404, detail=f"PO {req.purchaseOrderNumber} not found")

    selling_party = po.get("sellingParty") or {}
    if not selling_party.get("partyId"):
        raise HTTPException(status_code=400, detail="Missing sellingParty.partyId for acknowledgement payload")

    # Build lookup for identifiers by itemSequenceNumber
    seq_lookup: Dict[str, Dict[str, Any]] = {}
    for it in po.get("itemStatus") or po.get("items") or []:
        seq = it.get("itemSequenceNumber")
        if not seq:
            continue
        seq_lookup[seq] = {
            "asin": it.get("amazonProductIdentifier") or it.get("buyerProductIdentifier"),
            "sku": it.get("vendorProductIdentifier"),
            "ordered": it.get("orderedQuantity") or {},
            "netCost": it.get("netCost"),
            "listPrice": it.get("listPrice"),
        }

    def _parse_item_qty(q: Any) -> int:
        """
        Safely parse ItemQuantity objects from the PO/status.
        Expected shapes:
          { "amount": 10, "unitOfMeasure": "...", ... }
        or nested like { "orderedQuantity": { ... } }
        """
        if not isinstance(q, dict):
            return 0
        if "amount" in q:
            try:
                return int(q.get("amount") or 0)
            except Exception:
                return 0
        inner = q.get("orderedQuantity")
        if isinstance(inner, dict) and "amount" in inner:
            try:
                return int(inner.get("amount") or 0)
            except Exception:
                return 0
        return 0

    ack_date = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    items_payload: List[Dict[str, Any]] = []

    for line in req.items:
        conf = (line.confirmationStatus or "").upper()
        if conf not in {"ACCEPTED", "REJECTED"}:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid confirmationStatus for item {line.itemSequenceNumber}",
            )

        acc_qty = max(0, int(line.acceptedQuantity or 0))
        rej_qty = max(0, int(line.rejectedQuantity or 0))

        lookup = seq_lookup.get(line.itemSequenceNumber or "") or {}
        ordered_raw = lookup.get("ordered") or {}
        ordered_amt = _parse_item_qty(ordered_raw)

        if conf == "ACCEPTED" and acc_qty == 0 and rej_qty == 0:
            acc_qty = max(1, ordered_amt or 1)
        if conf == "REJECTED" and acc_qty == 0 and rej_qty == 0:
            rej_qty = max(1, ordered_amt or 1)

        if ordered_amt and (acc_qty + rej_qty) != ordered_amt:
            acc_qty = max(0, ordered_amt - rej_qty)

        item_ack_list: List[Dict[str, Any]] = []

        if acc_qty > 0:
            item_ack_list.append(
                {
                    "acknowledgementCode": "Accepted",
                    "acknowledgedQuantity": _quant(acc_qty),
                }
            )

        if rej_qty > 0:
            rej_obj: Dict[str, Any] = {
                "acknowledgementCode": "Rejected",
                "acknowledgedQuantity": _quant(rej_qty),
            }
            if line.rejectionReason:
                rej_obj["rejectionReason"] = line.rejectionReason
            item_ack_list.append(rej_obj)

        if not item_ack_list:
            qty = ordered_amt or 1
            item_ack_list.append(
                {
                    "acknowledgementCode": "Accepted",
                    "acknowledgedQuantity": _quant(qty),
                }
            )
            ordered_amt = qty

        asin = line.buyerProductIdentifier or lookup.get("asin") or ""
        sku = line.vendorProductIdentifier or lookup.get("sku") or ""

        ordered_qty_obj = _quant(ordered_amt or (acc_qty + rej_qty) or 1)

        item_obj: Dict[str, Any] = {
            "itemSequenceNumber": line.itemSequenceNumber,
            "orderedQuantity": ordered_qty_obj,
            "itemAcknowledgements": item_ack_list,
        }

        if asin:
            item_obj["amazonProductIdentifier"] = asin
        if sku:
            item_obj["vendorProductIdentifier"] = sku

        if lookup.get("netCost"):
            item_obj["netCost"] = lookup["netCost"]
        if lookup.get("listPrice"):
            item_obj["listPrice"] = lookup["listPrice"]

        items_payload.append(item_obj)

    ack_obj: Dict[str, Any] = {
        "purchaseOrderNumber": req.purchaseOrderNumber,
        "acknowledgementDate": ack_date,
        "sellingParty": selling_party,
        "items": items_payload,
    }

    # ship_from_id = (
    #     req.shipFromPartyId
    #     or SHIP_FROM_PARTY_ID
    #     or po.get("shipFromParty", {}).get("partyId")
    #     or po.get("shipToParty", {}).get("partyId")
    # )
    # if ship_from_id:
    #     ack_obj["shipFromParty"] = {"partyId": ship_from_id}

    return {"acknowledgements": [ack_obj]}


def submit_po_acknowledgement(req: AckRequest) -> Dict[str, Any]:
    marketplace = MARKETPLACE_IDS[0].strip()
    host = resolve_vendor_host(marketplace)
    url = f"{host}/vendor/orders/v1/acknowledgements"
    token = auth_client.get_lwa_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "x-amz-access-token": token,
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "sp-api-desktop-app/1.0",
    }

    payload = build_ack_payload(req)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to send acknowledgement: {e}")

    status_code = resp.status_code
    try:
        resp_data = resp.json()
    except Exception:
        resp_data = {"raw": resp.text}
    if status_code >= 400:
        # Log the failed attempt
        log_entry = {
            "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "po": req.purchaseOrderNumber,
            "transactionId": None,
            "status": status_code,
            "payload": payload,
            "response": resp_data,
        }
        try:
            ACK_LOG_PATH.parent.mkdir(exist_ok=True)
            with ACK_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[Ack] Failed to write ack log: {e}")
        raise HTTPException(status_code=status_code, detail={"transactionId": None, "response": resp_data})

    transaction_id = None
    if isinstance(resp_data, dict):
        transaction_id = (
            resp_data.get("transactionId")
            or resp_data.get("payload", {}).get("transactionId")
            or resp_data.get("payload", {}).get("transactionID")
        )

    # Persist log entry
    log_entry = {
        "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "po": req.purchaseOrderNumber,
        "transactionId": transaction_id,
        "status": status_code,
        "payload": payload,
        "response": resp_data,
    }
    try:
        ACK_LOG_PATH.parent.mkdir(exist_ok=True)
        with ACK_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[Ack] Failed to write ack log: {e}")

    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail={"transactionId": transaction_id, "response": resp_data})

    return {"transactionId": transaction_id, "response": resp_data}


def get_vendor_transaction_status(transaction_id: str) -> dict | None:
    """
    Best-effort lookup of a vendor transaction's status using Vendor Transactions API.
    Returns a normalized dict with status/errors/raw or None on failure.
    """
    if not transaction_id:
        return None
    if not MARKETPLACE_IDS:
        return None

    marketplace = MARKETPLACE_IDS[0].strip()
    host = resolve_vendor_host(marketplace)
    url = f"{host}/vendor/transactions/v1/transactions/{transaction_id}"
    token = auth_client.get_lwa_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "x-amz-access-token": token,
        "accept": "application/json",
        "user-agent": "sp-api-desktop-app/1.0",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=20)
    except requests.exceptions.Timeout:
        logger.warning(f"[VendorTx] Timeout fetching transaction {transaction_id}")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"[VendorTx] Error fetching transaction {transaction_id}: {e}")
        return None

    if resp.status_code == 404:
        logger.warning(f"[VendorTx] Transaction {transaction_id} not found (404)")
        return None
    if resp.status_code == 429:
        try:
            raw_data = resp.json()
        except Exception:
            raw_data = {"raw": resp.text}
        logger.warning(f"[VendorTx] Rate limited fetching transaction {transaction_id}")
        return {
            "transactionId": transaction_id,
            "status": None,
            "errors": [],
            "raw": raw_data,
            "rateLimited": True,
        }
    if resp.status_code >= 400:
        logger.warning(f"[VendorTx] Failed transaction lookup {transaction_id}: HTTP {resp.status_code}")
        return None

    try:
        raw_data = resp.json()
    except Exception:
        raw_data = {"raw": resp.text}

    payload = raw_data.get("payload") if isinstance(raw_data, dict) else None
    if not isinstance(payload, dict):
        payload = raw_data if isinstance(raw_data, dict) else {}

    tx_status_obj = payload.get("transactionStatus") if isinstance(payload, dict) else {}
    status = None
    errors: List[Any] = []
    if isinstance(tx_status_obj, dict):
        status = tx_status_obj.get("status")
        errors = tx_status_obj.get("errors") or []
    elif isinstance(payload, dict):
        status = payload.get("status")
        errors = payload.get("errors") or []

    tx_id = payload.get("transactionId") if isinstance(payload, dict) else None
    if not tx_id and isinstance(raw_data, dict):
        tx_id = raw_data.get("transactionId")

    return {
        "transactionId": tx_id or transaction_id,
        "status": status,
        "errors": errors if isinstance(errors, list) else [],
        "raw": raw_data,
    }


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


@app.post("/api/vendor-pos/sync")
def sync_vendor_pos():
    """
    Fetch Vendor POs from SP-API for a fixed window and persist to vendor_pos_cache.json.
    """
    created_after = get_latest_po_date_from_cache() or "2025-10-01T00:00:00Z"
    created_before = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    try:
        pos = fetch_vendor_pos_from_api(created_after, created_before, max_pages=5)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sync failed: {exc}")

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
        old_data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8")) if VENDOR_POS_CACHE.exists() else {}
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
        VENDOR_POS_CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
        from services.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM vendor_po_lines")
            row = cur.fetchone()
            line_count = row["cnt"] if row else 0
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
    created_after = "2025-10-01T00:00:00Z"
    created_before = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    try:
        pos = fetch_vendor_pos_from_api(created_after, created_before, max_pages=10)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rebuild failed: {exc}")

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
        VENDOR_POS_CACHE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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


@app.post("/api/vendor-pos/acknowledge")
def acknowledge_vendor_po(req: AckRequest):
    """
    Submit acknowledgement for a PO to SP-API. Returns transactionId on success.
    Body example:
    {
      "purchaseOrderNumber": "ABC123",
      "shipFromPartyId": "DXB5",
      "items": [
        {
          "itemSequenceNumber": "1",
          "buyerProductIdentifier": "ASIN",
          "vendorProductIdentifier": "SKU",
          "confirmationStatus": "ACCEPTED",
          "acceptedQuantity": 10,
          "rejectedQuantity": 0,
          "rejectionReason": "OUT_OF_STOCK"  // optional
        }
      ]
    }
    """
    result = submit_po_acknowledgement(req)
    return {"status": "ok", **result}


@app.get("/api/vendor-pos/ack-log/{po_number}")
def get_vendor_po_ack_log(po_number: str):
    if not po_number:
        raise HTTPException(status_code=400, detail="po_number required")

    if not ACK_LOG_PATH.exists():
        return {"po_number": po_number, "entries": []}

    entries: List[Dict[str, Any]] = []

    def _derive_totals_from_ack(payload: Dict[str, Any]) -> Tuple[int, int, int]:
        ack_list = payload.get("acknowledgements") or []
        ack_obj = ack_list[0] if ack_list else {}
        items = ack_obj.get("items") or []
        line_count = len(items)
        accepted_total = 0
        rejected_total = 0

        for it in items:
            # Legacy payload shape support
            ack_status = it.get("acknowledgementStatus") or {}
            accepted_total += _parse_qty(ack_status.get("acceptedQuantity"))
            rejected_total += _parse_qty(ack_status.get("rejectedQuantity"))

            for ack_item in it.get("itemAcknowledgements") or []:
                amt = _parse_qty(ack_item.get("acknowledgedQuantity"))
                code = (ack_item.get("acknowledgementCode") or "").lower()
                if code == "accepted":
                    accepted_total += amt
                elif code == "rejected":
                    rejected_total += amt

        return line_count, accepted_total, rejected_total

    try:
        with ACK_LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                except Exception:
                    continue
                if entry.get("po") != po_number:
                    continue

                ts = entry.get("ts") or ""
                transaction_id = entry.get("transactionId") or ""
                http_status = entry.get("status")
                payload = entry.get("payload") or {}

                line_count, accepted_total, rejected_total = _derive_totals_from_ack(payload if isinstance(payload, dict) else {})

                tx_status = None
                error_summary = None

                if (
                    transaction_id
                    and isinstance(http_status, int)
                    and 200 <= http_status < 300
                ):
                    tx_info = get_vendor_transaction_status(transaction_id)
                    if tx_info:
                        if tx_info.get("rateLimited"):
                            tx_status = "RateLimited"
                            error_summary = "Vendor transaction status rate-limited"
                        else:
                            tx_status = tx_info.get("status")
                            errors = tx_info.get("errors") or []
                            if errors and isinstance(errors, list):
                                first = errors[0] or {}
                                code = first.get("code") or first.get("errorCode")
                                msg = first.get("message") or first.get("errorMessage")
                                if code or msg:
                                    error_summary = f"{code}: {msg}" if code and msg else (code or msg)

                entries.append(
                    {
                        "ts": ts,
                        "transactionId": transaction_id,
                        "httpStatus": http_status,
                        "transactionStatus": tx_status or None,
                        "errorSummary": error_summary,
                        "lineCount": line_count,
                        "acceptedTotal": accepted_total,
                        "rejectedTotal": rejected_total,
                    }
                )
    except Exception as e:
        logger.warning(f"[AckLog] Failed to read ack log: {e}")

    entries.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return {"po_number": po_number, "entries": entries}


def _aggregate_vendor_po_lines(pos_list: List[Dict[str, Any]]) -> None:
    """
    Attach aggregated quantities from vendor_po_lines to each PO in pos_list.
    Exposes total_ordered_qty, total_accepted_qty, total_received_qty, total_pending_qty,
    total_cancelled_qty, and total_shortage_qty for display.
    """
    from services.db import get_db_connection

    if not pos_list:
        return

    po_numbers = [po.get("purchaseOrderNumber") for po in pos_list if po.get("purchaseOrderNumber")]
    if not po_numbers:
        return

    try:
        with get_db_connection() as conn:
            placeholders = ",".join(["?" for _ in po_numbers])
            sql = f"""
            SELECT 
                po_number,
                COALESCE(SUM(ordered_qty), 0) AS total_ordered,
                COALESCE(SUM(accepted_qty), 0) AS total_accepted,
                COALESCE(SUM(received_qty), 0) AS total_received,
                COALESCE(SUM(pending_qty), 0) AS total_pending,
                COALESCE(SUM(cancelled_qty), 0) AS total_cancelled,
                COALESCE(SUM(shortage_qty), 0) AS total_shortage
            FROM vendor_po_lines
            WHERE po_number IN ({placeholders})
            GROUP BY po_number
            """
            cur = conn.execute(sql, po_numbers)
            rows = cur.fetchall()

            agg_map: dict[str, dict] = {}
            for row in rows:
                agg_map[row["po_number"]] = {
                    "total_ordered_qty": row["total_ordered"],
                    "total_accepted_qty": row["total_accepted"],
                    "total_received_qty": row["total_received"],
                    "total_pending_qty": row["total_pending"],
                    "total_cancelled_qty": row["total_cancelled"],
                    "total_shortage_qty": row["total_shortage"],
                }

            # Attach totals to each PO; default to 0 if no lines found
            for po in pos_list:
                po_num = po.get("purchaseOrderNumber")
                if po_num in agg_map:
                    po.update(agg_map[po_num])
                else:
                    po.setdefault("total_ordered_qty", 0)
                    po.setdefault("total_accepted_qty", 0)
                    po.setdefault("total_received_qty", 0)
                    po.setdefault("total_pending_qty", 0)
                    po.setdefault("total_cancelled_qty", 0)
                    po.setdefault("total_shortage_qty", 0)
                po.setdefault("total_received_qty", 0)
                po.setdefault("total_pending_qty", 0)

    except Exception as e:
        logger.error(f"[VendorPO] Error aggregating vendor_po_lines: {e}")
        # On error, make sure we at least have total_ordered_qty
        for po in pos_list:
            po.setdefault("total_ordered_qty", 0)
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


@app.get("/api/vendor-pos")
def get_vendor_pos(
    refresh: int = Query(0, description="If 1, refresh POs from SP-API before reading cache"),
    enrich: bool = Query(False, description="Enrich ASINs with Catalog data"),
):
    source = "cache"
    if refresh == 1:
        created_after = "2025-10-01T00:00:00Z"
        created_before = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        try:
            pos = fetch_vendor_pos_from_api(created_after, created_before, max_pages=5)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Sync failed: {exc}")
        try:
            VENDOR_POS_CACHE.write_text(
                json.dumps({"items": pos}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write vendor_pos_cache.json: {exc}")
        
        # FIX #1: Sync vendor_po_lines for all fetched POs (was missing!)
        po_numbers = [po.get("purchaseOrderNumber") for po in pos if po.get("purchaseOrderNumber")]
        # Attach status totals (received/pending) from purchaseOrdersStatus for freshly fetched POs
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

    if not VENDOR_POS_CACHE.exists():
        return {"items": [], "source": source}
    try:
        data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read cache: {exc}")

    normalized = normalize_pos_entries(data)
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
    print(f"[vendor-pos] filtered POs (>= 2025-10-01): {len(filtered)}")

    # FIX #2: Aggregate vendor_po_lines data for each PO
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
    # Reuse the cache loader to stay consistent with /api/vendor-pos
    if not VENDOR_POS_CACHE.exists():
        return JSONResponse({"error": "PO not found"}, status_code=404)
    try:
        data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read cache: {exc}")

    normalized = normalize_pos_entries(data)

    po = next((p for p in normalized if p.get("purchaseOrderNumber") == po_number), None)
    if not po:
        return JSONResponse({"error": "PO not found"}, status_code=404)

    # Attach live itemStatus/items from status endpoint so modal reflects latest accept/reject
    try:
        detailed = fetch_detailed_po_with_status(po_number)
        if isinstance(detailed, dict):
            status_items = detailed.get("itemStatus") or detailed.get("items") or []
            if status_items:
                po.setdefault("orderDetails", {})
                po["orderDetails"]["items"] = status_items
    except Exception as exc:
        logger.warning(f"[VendorPO] Could not attach status items for PO {po_number}: {exc}")

    if enrich:
        try:
            enrich_items_with_catalog([po])
        except Exception as exc:
            print(f"Error enriching PO {po_number}: {exc}")

    return {"item": po}


@app.get("/api/vendor-po-lines")
def get_vendor_po_lines(po_number: str):
    """
    Return line-item details for a PO from vendor_po_lines table.
    Used by the "Line Items Inventory Breakdown" modal in the UI.
    
    Response format:
    {
        "po_number": "...",
        "lines": [
            {
                "asin": "...",
                "sku": "...",
                "ordered_qty": N,
                "received_qty": N,
                "pending_qty": N,
                "shortage_qty": N,
                "last_changed_utc": "..."
            },
            ...
        ],
        "message": "..." (optional, if no lines found)
    }
    
    Returns HTTP 200 with empty lines list if no data found (never 404).
    """
    from services.db import get_db_connection
    
    if not po_number:
        raise HTTPException(status_code=400, detail="po_number parameter required")
    
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                """SELECT po_number, asin, sku, ordered_qty, received_qty, 
                          pending_qty, shortage_qty, last_changed_utc
                   FROM vendor_po_lines 
                   WHERE po_number = ?
                   ORDER BY asin""",
                (po_number,)
            )
            rows = cur.fetchall()
            
            lines = []
            if rows:
                for row in rows:
                    lines.append({
                        "asin": row["asin"] or "",
                        "sku": row["sku"] or "",
                        "ordered_qty": row["ordered_qty"] or 0,
                        "received_qty": row["received_qty"] or 0,
                        "pending_qty": row["pending_qty"] or 0,
                        "shortage_qty": row["shortage_qty"] or 0,
                        "last_changed_utc": row["last_changed_utc"] or ""
                    })
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

    existing["status"] = status

    if appointment_date:
        existing["appointmentDate"] = appointment_date
    elif status != "Appointment Scheduled":
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
    Return all saved Out-of-Stock items as a flat list for the OOS tab.
    """
    state = load_oos_state()
    items = list(state.values())
    return {"items": items}


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
    if not po or not asin:
        raise HTTPException(status_code=400, detail="poNumber and asin required")

    key = f"{po}::{asin}"
    state = load_oos_state()
    if key in state:
        del state[key]
    save_oos_state(state)

    return {"status": "ok", "key": key}


def consolidate_picklist(po_numbers: List[str]) -> Dict[str, Any]:
    if not VENDOR_POS_CACHE.exists():
        return {"summary": {"numPos": 0, "totalUnits": 0, "totalLines": 0, "warning": "Cache missing"}, "items": []}
    try:
        data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read cache: {exc}")

    normalized = normalize_pos_entries(data)
    selected = [po for po in normalized if po.get("purchaseOrderNumber") in po_numbers]
    if not selected:
        return {"summary": {"numPos": 0, "totalUnits": 0, "totalLines": 0, "warning": "No matching POs"}, "items": []}

    oos_state = load_oos_state()
    oos_keys = set(oos_state.keys()) if isinstance(oos_state, dict) else set()

    catalog = spapi_catalog_status()

    consolidated: Dict[Tuple[str, str], Dict[str, Any]] = {}
    total_units = 0

    for po in selected:
        po_num = po.get("purchaseOrderNumber") or ""
        d = po.get("orderDetails") or {}
        items = d.get("items") or []
        for it in items:
            ack_status = it.get("acknowledgementStatus") or {}
            if isinstance(ack_status, dict):
                conf = (ack_status.get("confirmationStatus") or "").upper()
                if conf == "REJECTED":
                    continue
            asin = it.get("amazonProductIdentifier") or ""
            sku = it.get("vendorProductIdentifier") or ""
            qty = it.get("orderedQuantity") or {}
            qty_amount = qty.get("amount")
            try:
                qty_num = float(qty_amount)
            except Exception:
                qty_num = 0

            if not asin:
                continue

            key_po_asin = f"{po_num}::{asin}"
            if key_po_asin in oos_keys:
                continue
            # Also skip if an OOS entry matches asin+sku regardless of PO if stored
            if any(
                (entry.get("asin") == asin and entry.get("vendorSku") == sku)
                for entry in (oos_state.values() if isinstance(oos_state, dict) else [])
            ):
                continue

            ckey = (asin, sku)
            if ckey not in consolidated:
                info = catalog.get(asin) or {}
                master_sku = info.get("sku")
                line_sku = master_sku or sku or ""
                consolidated[ckey] = {
                    "asin": asin,
                    "externalId": sku,
                    "sku": line_sku,
                    "title": info.get("title"),
                    "image": info.get("image"),
                    "totalQty": 0,
                }
            consolidated[ckey]["totalQty"] += qty_num
            total_units += qty_num

    items_out = list(consolidated.values())
    items_out.sort(key=lambda x: (0 - (x.get("totalQty") or 0)))
    summary = {
        "numPos": len(selected),
        "totalUnits": total_units,
        "totalLines": len(items_out),
        "warning": None,
    }
    return {"summary": summary, "items": items_out}


def generate_picklist_pdf(po_numbers: List[str], items: List[Dict[str, Any]], summary: Dict[str, Any]) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise HTTPException(status_code=500, detail="reportlab is required for PDF generation")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontSize = 9
    title_style = styles["Normal"]
    title_style.fontSize = 9
    qty_style = styles["Normal"]
    qty_style.fontSize = 9
    qty_style.alignment = 1  # center

    data = []
    header = ["ASIN", "SKU", "Image", "Title", "Total Qty"]
    data.append(header)

    col_widths = [28 * mm, 28 * mm, 40 * mm, 64 * mm, 20 * mm]

    for it in items:
        asin = it.get("asin") or ""
        sku = it.get("sku") or it.get("externalId") or it.get("vendorSku") or ""
        img_url = it.get("image") or ""
        title = it.get("title") or ""
        qty = it.get("totalQty") or ""

        # Image flowable
        img_flow = ""
        if img_url:
            try:
                img_flow = Image(img_url, width=38 * mm, height=38 * mm, kind="proportional")
            except Exception:
                img_flow = ""

        data.append(
            [
                asin,
                sku,
                img_flow,
                Paragraph(title, title_style),
                Paragraph(f"<b>{qty}</b>", qty_style),
            ]
        )

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, "black"),
                ("BACKGROUND", (0, 0), (-1, 0), "#f3f4f6"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (-1, 1), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    story = []

    def first_page_header(canvas_obj, doc_obj):
        canvas_obj.saveState()
        header_text = f"POs: {', '.join(po_numbers)}"
        canvas_obj.setFont("Helvetica-Bold", 11)
        canvas_obj.drawString(doc_obj.leftMargin, doc_obj.height + doc_obj.topMargin - 5, header_text)
        canvas_obj.restoreState()

    def later_pages(canvas_obj, doc_obj):
        pass

    story.append(Spacer(1, 6 * mm))
    story.append(table)

    doc.build(story, onFirstPage=first_page_header, onLaterPages=later_pages)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


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
            }
            for asin in asins
        ]
    }


@app.post("/api/catalog/fetch/{asin}")
def fetch_catalog_for_asin(asin: str, background_tasks: BackgroundTasks):
    """
    Queue catalog fetch in background and return immediately.
    
    FIX #3B: Convert long-running catalog fetch to background task.
    Returns immediately with status="queued" instead of blocking request.
    Client can poll /api/catalog/asins to check if fetch completed.
    """
    try:
        fetched = spapi_catalog_status().get(asin)
        if fetched and (fetched.get("title") or fetched.get("image")):
            return {"asin": asin, "status": "cached", "title": fetched.get("title"), "image": fetched.get("image")}
    except Exception as e:
        logger.warning(f"[Catalog] Error checking cache for {asin}: {e}")
    
    # Queue in background to avoid blocking request
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
    
    FIX #3C: Convert batch catalog fetch to background task.
    Returns immediately with count of queued ASINs instead of blocking.
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
    
    # Queue all missing ASINs in background
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
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT title, image, payload FROM spapi_catalog WHERE asin = ?
            """,
            (asin,),
        ).fetchone()
    if not row:
        return JSONResponse({"error": "Catalog not found"}, status_code=404)
    title, image, payload_raw = row
    payload = {}
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
    except Exception:
        payload = {"raw": payload_raw}
    return {"asin": asin, "title": title, "image": image, "payload": payload}


def forecast_auto_sync_loop():
    """Background thread: periodically refreshes forecast data."""
    logger.info("[ForecastAutoSync] Background auto-sync thread started")

    interval_seconds = FORECAST_AUTO_SYNC_INTERVAL_MINUTES * 60

    while True:
        try:
            # Skip if we already synced within the last 24h
            try:
                from services.forecast_sync import _load_last_full_sync

                last_sync = _load_last_full_sync()
                if last_sync:
                    now = datetime.now(timezone.utc)
                    if (now - last_sync) < timedelta(hours=24):
                        next_allowed = last_sync + timedelta(hours=24)
                        logger.info(
                            "[ForecastAutoSync] Last sync at %s; skipping until %s",
                            last_sync.isoformat(),
                            next_allowed.isoformat(),
                        )
                        time.sleep(interval_seconds)
                        continue
            except Exception:
                # If we cannot read the state, proceed and let sync guard handle it
                pass

            logger.info("[ForecastAutoSync] Starting scheduled forecast sync...")
            from services.forecast_sync import sync_all_forecast_sources
            try:
                summary = sync_all_forecast_sources()
                status = summary.get("status", "ok")
                if status == "ok":
                    logger.info("[ForecastAutoSync] Scheduled sync completed successfully: %s", summary)
                elif status == "warning":
                    logger.warning("[ForecastAutoSync] Scheduled sync completed with warnings: %s", summary)
                else:
                    logger.error("[ForecastAutoSync] Scheduled sync completed with errors: %s", summary)
            except Exception as exc:
                if "sync already running" in str(exc):
                    logger.warning("[ForecastAutoSync] Sync already in progress, skipping this run")
                elif "sync_recent" in str(exc):
                    logger.info("[ForecastAutoSync] Last sync was recent; skipping this run")
                else:
                    raise
        except Exception as exc:
            logger.error(f"[ForecastAutoSync] Error during scheduled sync: {exc}")

        # Sleep until next scheduled sync
        time.sleep(interval_seconds)


@app.on_event("startup")
def start_forecast_scheduler():
    """Start background thread for forecast auto-sync."""
    t = threading.Thread(target=forecast_auto_sync_loop, daemon=True)
    t.start()
    logger.info("[ForecastAutoSync] Auto-sync scheduler initialized")


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
    return Response(content=pdf_bytes, media_type="application/pdf")


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
    headers = {"Content-Disposition": 'inline; filename="picklist.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/api/debug/sample-po")
def debug_sample_po():
    """
    Return the first PO item from cache for debugging purposes.
    """
    if not VENDOR_POS_CACHE.exists():
        return {"message": "no items in cache"}
    try:
        data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
    except Exception:
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
        with sqlite3.connect(CATALOG_DB_PATH) as conn:
            row = conn.execute(
                "SELECT payload FROM spapi_catalog WHERE asin = ?",
                (asin,),
            ).fetchone()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DB error: {exc}")
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    payload_raw = row[0]
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
    except Exception:
        payload = {}
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
    from services.db import execute_write
    sql = """
    CREATE TABLE IF NOT EXISTS vendor_po_lines (
        id INTEGER PRIMARY KEY,
        po_number TEXT NOT NULL,
        ship_to_location TEXT,
        asin TEXT,
        sku TEXT,
        ordered_qty INTEGER DEFAULT 0,
        accepted_qty INTEGER DEFAULT 0,
        cancelled_qty INTEGER DEFAULT 0,
        shipped_qty INTEGER DEFAULT 0,
        received_qty INTEGER DEFAULT 0,
        shortage_qty INTEGER DEFAULT 0,
        pending_qty INTEGER DEFAULT 0,
        last_changed_utc TEXT
    )
    """
    try:
        execute_write(sql)
        logger.info("[VendorPO] vendor_po_lines table initialized")
    except Exception as e:
        logger.error(f"[VendorPO] Failed to initialize vendor_po_lines table: {e}")


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
    from services.db import get_db_connection
    
    # Fetch raw PO from SP-API
    detailed_po = fetch_detailed_po_with_status(po_number)
    if not detailed_po:
        print(f"[VerifyPO {po_number}] ERROR: Could not fetch PO from SP-API")
        return
    
    # Try itemStatus first, fallback to items
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
    print(f"\n[VerifyPO {po_number}] ===== SP-API LINE DETAILS (from {data_source}) =====")
    
    for idx, item in enumerate(item_status_list, 1):
        item_seq = item.get("itemSequenceNumber", "?")
        asin = item.get("amazonProductIdentifier", "?")
        
        if use_item_status:
            # Extract from itemStatus structure
            # Extract ordered quantity
            ordered = 0
            oq_obj = item.get("orderedQuantity", {})
            if isinstance(oq_obj, dict):
                oq_inner = oq_obj.get("orderedQuantity", {})
                if isinstance(oq_inner, dict):
                    ordered = int(oq_inner.get("amount", 0) or 0)
            
            # Extract cancelled quantity
            cancelled = 0
            oq_obj = item.get("orderedQuantity", {})
            if isinstance(oq_obj, dict):
                can_inner = oq_obj.get("cancelledQuantity", {})
                if isinstance(can_inner, dict):
                    cancelled = int(can_inner.get("amount", 0) or 0)
            
            # Extract accepted quantity
            accepted = 0
            ack_obj = item.get("acknowledgementStatus", {})
            if isinstance(ack_obj, dict):
                acc_qty = ack_obj.get("acceptedQuantity", {})
                if isinstance(acc_qty, dict):
                    accepted = int(acc_qty.get("amount", 0) or 0)
            
            # Extract received quantity
            received = 0
            recv_obj = item.get("receivingStatus", {})
            if isinstance(recv_obj, dict):
                recv_qty = recv_obj.get("receivedQuantity", {})
                if isinstance(recv_qty, dict):
                    received = int(recv_qty.get("amount", 0) or 0)
        else:
            # Extract from items structure (fallback)
            ordered = 0
            oq = item.get("orderedQuantity", {})
            if isinstance(oq, dict):
                ordered = int(oq.get("amount", 0) or 0)
            
            cancelled = 0
            accepted = ordered  # Assume all ordered is accepted if no status
            received = 0
        
        # Calculate derived
        pending = max(0, accepted - received)
        shortage = max(0, ordered - accepted - cancelled)
        
        print(f"  [Item {idx} seq={item_seq} asin={asin}] " 
              f"ordered={ordered} accepted={accepted} cancelled={cancelled} "
              f"received={received} pending={pending} shortage={shortage}")
        
        api_ordered_total += ordered
        api_accepted_total += accepted
        api_cancelled_total += cancelled
        api_received_total += received
        api_pending_total += pending
        api_shortage_total += shortage
    
    print(f"[VerifyPO {po_number}] SP-API TOTALS: "
          f"ordered={api_ordered_total} accepted={api_accepted_total} "
          f"cancelled={api_cancelled_total} received={api_received_total} "
          f"pending={api_pending_total} shortage={api_shortage_total}")
    
    # Query database aggregates
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                """SELECT 
                   COALESCE(SUM(ordered_qty), 0) as total_ordered,
                   COALESCE(SUM(accepted_qty), 0) as total_accepted,
                   COALESCE(SUM(cancelled_qty), 0) as total_cancelled,
                   COALESCE(SUM(received_qty), 0) as total_received,
                   COALESCE(SUM(pending_qty), 0) as total_pending,
                   COALESCE(SUM(shortage_qty), 0) as total_shortage
                FROM vendor_po_lines
                WHERE po_number = ?""",
                (po_number,)
            )
            row = cur.fetchone()
            if row:
                db_ordered = row["total_ordered"]
                db_accepted = row["total_accepted"]
                db_cancelled = row["total_cancelled"]
                db_received = row["total_received"]
                db_pending = row["total_pending"]
                db_shortage = row["total_shortage"]
                
                print(f"[VerifyPO {po_number}] DB TOTALS: "
                      f"ordered={db_ordered} accepted={db_accepted} "
                      f"cancelled={db_cancelled} received={db_received} "
                      f"pending={db_pending} shortage={db_shortage}")
                
                # Compare
                print(f"\n[VerifyPO {po_number}] ===== COMPARISON =====")
                ordered_match = "✓" if api_ordered_total == db_ordered else f"✗ (api={api_ordered_total} vs db={db_ordered})"
                accepted_match = "✓" if api_accepted_total == db_accepted else f"✗ (api={api_accepted_total} vs db={db_accepted})"
                cancelled_match = "✓" if api_cancelled_total == db_cancelled else f"✗ (api={api_cancelled_total} vs db={db_cancelled})"
                received_match = "✓" if api_received_total == db_received else f"✗ (api={api_received_total} vs db={db_received})"
                pending_match = "✓" if api_pending_total == db_pending else f"✗ (api={api_pending_total} vs db={db_pending})"
                shortage_match = "✓" if api_shortage_total == db_shortage else f"✗ (api={api_shortage_total} vs db={db_shortage})"
                
                print(f"  ordered:   {ordered_match}")
                print(f"  accepted:  {accepted_match}")
                print(f"  cancelled: {cancelled_match}")
                print(f"  received:  {received_match}")
                print(f"  pending:   {pending_match}")
                print(f"  shortage:  {shortage_match}")
            else:
                print(f"[VerifyPO {po_number}] ERROR: No rows found in database for this PO")
    except Exception as e:
        logger.error(f"[VerifyPO {po_number}] Error querying database: {e}", exc_info=True)
        print(f"[VerifyPO {po_number}] ERROR: {e}")


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
    from services.db import execute_write
    
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
        execute_write("DELETE FROM vendor_po_lines WHERE po_number = ?", (po_number,))
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

            # Insert into vendor_po_lines
            sql = """
            INSERT INTO vendor_po_lines
            (po_number, ship_to_location, asin, sku, ordered_qty, accepted_qty,
             cancelled_qty, shipped_qty, received_qty, shortage_qty, pending_qty, last_changed_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                po_number, ship_to_location, asin, sku,
                ordered_qty, accepted_qty, cancelled_qty, 0,  # shipped_qty = 0 (not in schema)
                received_qty, shortage_qty, pending_qty, now_utc
            )
            execute_write(sql, params)

        except Exception as e:
            logger.error(f"[VendorPO] Error processing item {item_seq} in PO {po_number}: {e}", exc_info=True)
            continue

    logger.info(f"[VendorPO] Synced {len(item_status_list)} lines for PO {po_number}")


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
    from services.db import get_db_connection
    
    print(f"\n[VerifyPOReceipts {po_number}] ===== COMPARING DB vs SHIPMENTS =====")
    print(f"[VerifyPOReceipts {po_number}] Data sources:")
    print(f"  DB (vendor_po_lines): Vendor Orders API -> Ordered/Received from itemStatus")
    print(f"  Shipments API: /vendor/shipping/v1/shipments filtered by buyerReferenceNumber={po_number}")
    
    # Get DB data
    db_lines: Dict[Tuple[str, str], Dict[str, Any]] = {}
    db_ordered_total = 0
    db_received_total = 0
    
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                """SELECT asin, sku, ordered_qty, received_qty
                   FROM vendor_po_lines
                   WHERE po_number = ?
                   ORDER BY asin""",
                (po_number,)
            )
            rows = cur.fetchall()
            for row in rows:
                asin = row["asin"] or ""
                sku = row["sku"] or ""
                key = (asin, sku)
                ordered_qty = int(row["ordered_qty"] or 0)
                received_qty = int(row["received_qty"] or 0)
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
    
    for po_num in po_numbers:
        try:
            _sync_vendor_po_lines_for_po(po_num)
        except Exception as e:
            logger.error(f"[VendorPO] Error syncing lines for PO {po_num}: {e}")
            continue


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
    from services.db import get_db_connection
    
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
        
        cache_data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
        normalized = normalize_pos_entries(cache_data)
        
        # Sort by date (newest first, matching the grid behavior)
        normalized.sort(key=parse_po_date, reverse=True)
        
        po_numbers = [po.get("purchaseOrderNumber") for po in normalized if po.get("purchaseOrderNumber")]
        
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
    
    # Rebuild lines for each PO
    success_count = 0
    error_count = 0
    progress_interval = max(1, len(po_numbers) // 10)  # Log every ~10% progress
    
    for idx, po_num in enumerate(po_numbers, 1):
        try:
            _sync_vendor_po_lines_for_po(po_num)
            success_count += 1
            
            # Log progress periodically
            if idx % progress_interval == 0 or idx == len(po_numbers):
                pct = (idx * 100) // len(po_numbers)
                logger.info(f"[VendorPO] Rebuild progress: {idx}/{len(po_numbers)} POs ({pct}%)")
                print(f"[VendorPO] Progress: {idx}/{len(po_numbers)} POs ({pct}%)")
        
        except Exception as e:
            logger.error(f"[VendorPO] Error rebuilding lines for PO {po_num}: {e}")
            error_count += 1
            continue
    
    # Final summary
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM vendor_po_lines")
            row = cur.fetchone()
            line_count = row["cnt"] if row else 0
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


