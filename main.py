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
# Vendor POs (raw JSON)
# -------------------------------
VENDOR_POS_CACHE = Path(__file__).parent / "vendor_pos_cache.json"
ASIN_CACHE_PATH = Path(__file__).parent / "asin_image_cache.json"
MARKETPLACE_IDS: List[str] = [
    mp for mp in (os.getenv("MARKETPLACE_IDS") or os.getenv("MARKETPLACE_ID", "")).split(",") if mp.strip()
]
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


def extract_purchase_orders(obj: Any) -> List[Dict[str, Any]] | None:
    """
    Recursively search the JSON response for a key 'purchaseOrders' whose value is a list,
    and return that list. If not found, also look for 'orders'. If still not found, return None.
    """
    if isinstance(obj, dict):
        if "purchaseOrders" in obj and isinstance(obj["purchaseOrders"], list):
            return obj["purchaseOrders"]
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
    
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            # Response is wrapped: {"payload": {...}}
            return data.get("payload")
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


def _aggregate_vendor_po_lines(pos_list: List[Dict[str, Any]]) -> None:
    """
    Aggregate vendor_po_lines data for each PO and add computed totals to the PO dict.
    
    For each PO, this adds:
    - total_ordered_qty: SUM(ordered_qty) from all lines
    - total_received_qty: SUM(received_qty) from all lines
    - total_pending_qty: SUM(pending_qty) from all lines
    - total_shortage_qty: SUM(shortage_qty) from all lines
    - last_line_change: MAX(last_changed_utc) from all lines
    
    Modifies pos_list in-place.
    """
    from services.db import get_db_connection
    
    if not pos_list:
        return
    
    # Get all unique PO numbers
    po_numbers = [po.get("purchaseOrderNumber") for po in pos_list if po.get("purchaseOrderNumber")]
    if not po_numbers:
        return
    
    try:
        with get_db_connection() as conn:
            # Query vendor_po_lines aggregated by po_number
            placeholders = ",".join(["?" for _ in po_numbers])
            sql = f"""
            SELECT 
                po_number,
                COALESCE(SUM(ordered_qty), 0) AS total_ordered,
                COALESCE(SUM(received_qty), 0) AS total_received,
                COALESCE(SUM(pending_qty), 0) AS total_pending,
                COALESCE(SUM(shortage_qty), 0) AS total_shortage,
                MAX(last_changed_utc) AS last_change
            FROM vendor_po_lines
            WHERE po_number IN ({placeholders})
            GROUP BY po_number
            """
            cur = conn.execute(sql, po_numbers)
            rows = cur.fetchall()
            
            # Build a map of po_number -> aggregates
            agg_map = {}
            for row in rows:
                agg_map[row["po_number"]] = {
                    "total_ordered_qty": row["total_ordered"],
                    "total_received_qty": row["total_received"],
                    "total_pending_qty": row["total_pending"],
                    "total_shortage_qty": row["total_shortage"],
                    "last_line_change": row["last_change"],
                }
            
            # Add aggregates to each PO
            for po in pos_list:
                po_num = po.get("purchaseOrderNumber")
                if po_num in agg_map:
                    po.update(agg_map[po_num])
                else:
                    # No lines found for this PO (shouldn't happen, but be safe)
                    po["total_ordered_qty"] = 0
                    po["total_received_qty"] = 0
                    po["total_pending_qty"] = 0
                    po["total_shortage_qty"] = 0
                    po["last_line_change"] = None
    
    except Exception as e:
        logger.error(f"[VendorPO] Error aggregating vendor_po_lines: {e}")
        # On error, set all to 0 to avoid crashes
        for po in pos_list:
            po.setdefault("total_ordered_qty", 0)
            po.setdefault("total_received_qty", 0)
            po.setdefault("total_pending_qty", 0)
            po.setdefault("total_shortage_qty", 0)
            po.setdefault("last_line_change", None)


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

    if enrich:
        try:
            enrich_items_with_catalog([po])
        except Exception as exc:
            print(f"Error enriching PO {po_number}: {exc}")

    return {"item": po}


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
        acknowledged_qty INTEGER DEFAULT 0,
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


def _sync_vendor_po_lines_for_po(po_number: str):
    """
    FIX: Sync vendor_po_lines for a single PO.
    
    Steps:
    1. Fetch detailed PO using getPurchaseOrder endpoint
    2. Parse items and their status (ordered, acknowledged, received, cancelled)
    3. Clear and re-populate vendor_po_lines for this PO
    """
    from services.db import execute_write
    
    # Fetch detailed PO with itemStatus
    detailed_po = fetch_detailed_po_with_status(po_number)
    if not detailed_po:
        logger.warning(f"[VendorPO] Could not fetch detailed PO {po_number}")
        return
    
    order_details = detailed_po.get("orderDetails", {})
    items = order_details.get("items", [])
    
    if not items:
        logger.warning(f"[VendorPO] PO {po_number} has no items")
        return
    
    ship_to_party = order_details.get("shipToParty", {})
    ship_to_location = ship_to_party.get("partyId", "")
    
    # Get itemStatus map if available
    item_status_map = {}
    item_status_list = detailed_po.get("itemStatus", [])
    if item_status_list:
        for status in item_status_list:
            item_seq = status.get("itemSequenceNumber", "")
            if item_seq:
                item_status_map[item_seq] = status
    
    # FIX: Clear only this PO's lines (scoped delete)
    try:
        execute_write("DELETE FROM vendor_po_lines WHERE po_number = ?", (po_number,))
    except Exception as e:
        logger.error(f"[VendorPO] Failed to clear lines for PO {po_number}: {e}")
        return
    
    # Process each item
    now_utc = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    for item in items:
        try:
            item_seq = item.get("itemSequenceNumber", "")
            asin = item.get("amazonProductIdentifier", "")
            sku = item.get("vendorProductIdentifier", "")
            
            # Parse ordered quantity (is a dict with 'amount' key)
            ordered_qty = 0
            oq = item.get("orderedQuantity")
            if isinstance(oq, dict):
                ordered_qty = int(oq.get("amount", 0) or 0)
            elif isinstance(oq, (int, float)):
                ordered_qty = int(oq)
            
            # Get status quantities from itemStatus
            status_info = item_status_map.get(item_seq, {})
            acknowledged_qty = 0
            cancelled_qty = 0
            received_qty = 0
            
            # Parse acknowledgedQuantity
            ack_qty = status_info.get("acknowledgedQuantity")
            if isinstance(ack_qty, dict):
                acknowledged_qty = int(ack_qty.get("amount", 0) or 0)
            elif isinstance(ack_qty, (int, float)):
                acknowledged_qty = int(ack_qty)
            
            # Parse cancelledQuantity
            can_qty = status_info.get("cancelledQuantity")
            if isinstance(can_qty, dict):
                cancelled_qty = int(can_qty.get("amount", 0) or 0)
            elif isinstance(can_qty, (int, float)):
                cancelled_qty = int(can_qty)
            
            # Parse receivedQuantity
            recv_qty = status_info.get("receivedQuantity")
            if isinstance(recv_qty, dict):
                received_qty = int(recv_qty.get("amount", 0) or 0)
            elif isinstance(recv_qty, (int, float)):
                received_qty = int(recv_qty)
            
            # Calculate derived quantities
            shortage_qty = max(0, ordered_qty - (received_qty + cancelled_qty))
            pending_qty = ordered_qty - received_qty
            
            # Insert into vendor_po_lines
            sql = """
            INSERT INTO vendor_po_lines 
            (po_number, ship_to_location, asin, sku, ordered_qty, acknowledged_qty, 
             cancelled_qty, shipped_qty, received_qty, shortage_qty, pending_qty, last_changed_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                po_number, ship_to_location, asin, sku,
                ordered_qty, acknowledged_qty, cancelled_qty, 0,  # shipped_qty = 0 for now
                received_qty, shortage_qty, pending_qty, now_utc
            )
            execute_write(sql, params)
            
        except Exception as e:
            logger.error(f"[VendorPO] Error processing item {item_seq} in PO {po_number}: {e}")
            continue
    
    logger.info(f"[VendorPO] Synced {len(items)} lines for PO {po_number}")


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


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)



