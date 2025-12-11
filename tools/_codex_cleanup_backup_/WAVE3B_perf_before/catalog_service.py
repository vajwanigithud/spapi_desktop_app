import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from services.db import get_db_connection

schema_logger = logging.getLogger("forecast_schema")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_DB_PATH = ROOT / "catalog.db"


def init_catalog_db(db_path: Path = DEFAULT_CATALOG_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spapi_catalog (
                asin TEXT PRIMARY KEY,
                title TEXT,
                image TEXT,
                payload TEXT,
                barcode TEXT,
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
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(spapi_catalog)").fetchall()}
            if "barcode" not in cols:
                conn.execute("ALTER TABLE spapi_catalog ADD COLUMN barcode TEXT")
                conn.commit()
                schema_logger.info("[CatalogSchema] Added barcode column to spapi_catalog")
        except Exception as exc:
            schema_logger.warning(f"[CatalogSchema] Failed to add barcode column: {exc}")
    schema_logger.info("Forecast feature disabled: init_forecast_tables() not called")


def upsert_spapi_catalog(asin: str, payload: Dict[str, Any], db_path: Path = DEFAULT_CATALOG_DB_PATH) -> None:
    if not asin:
        return
    init_catalog_db(db_path)
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
    with get_db_connection() as conn:
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


def spapi_catalog_status(db_path: Path = DEFAULT_CATALOG_DB_PATH) -> Dict[str, Dict[str, Any]]:
    if not db_path.exists():
        return {}
    updates = []
    results = {}
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.asin, c.title, c.image, c.payload, c.barcode, m.sku
            FROM spapi_catalog c
            LEFT JOIN spapi_catalog_meta m ON c.asin = m.asin
            """
        ).fetchall()
        for asin, title, image, payload_raw, barcode, sku in rows:
            parsed: Optional[Dict[str, Any]] = None
            model_number = None
            if (not title or not image) and payload_raw:
                try:
                    parsed = json.loads(payload_raw)
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
                            model_number = model_number or attrs.get("modelNumber")
                            title = title or attrs.get("title")
                except Exception as exc:
                    logger.warning(f"[Catalog] Failed to parse payload for {asin}: {exc}")
            results[asin] = {
                "title": title,
                "image": image,
                "payload": parsed or (json.loads(payload_raw) if payload_raw else None),
                "barcode": barcode,
                "sku": sku or model_number,
            }
            if not image or not title:
                updates.append((asin, title, image, parsed))
        for asin, title, image, parsed in updates:
            if not parsed:
                continue
            try:
                conn.execute(
                    "UPDATE spapi_catalog SET title = ?, image = ? WHERE asin = ?",
                    (title, image, asin),
                )
                conn.commit()
            except Exception as exc:
                logger.warning(f"[Catalog] Failed to backfill title/image for {asin}: {exc}")
    return results


def update_catalog_barcode(asin: str, barcode: str, db_path: Path = DEFAULT_CATALOG_DB_PATH) -> bool:
    if not asin or not barcode:
        return False
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT barcode FROM spapi_catalog WHERE asin = ?", (asin,))
            row = cur.fetchone()
            if not row:
                return False
            existing = row[0]
            if existing == barcode:
                return True
            conn.execute("UPDATE spapi_catalog SET barcode = ? WHERE asin = ?", (barcode, asin))
            conn.commit()
            return True
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to update barcode for {asin}: {exc}")
        return False


def set_catalog_barcode_if_absent(asin: str, barcode: str, db_path: Path = DEFAULT_CATALOG_DB_PATH) -> bool:
    if not asin or not barcode:
        return False
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT barcode FROM spapi_catalog WHERE asin = ?", (asin,))
            row = cur.fetchone()
            if not row:
                return False
            existing = row[0]
            if existing:
                if existing != barcode:
                    logger.info(f"[Catalog] Existing barcode retained for {asin} (existing={existing}, new={barcode})")
                return False
            conn.execute("UPDATE spapi_catalog SET barcode = ? WHERE asin = ?", (barcode, asin))
            conn.commit()
            return True
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to set barcode for {asin}: {exc}")
        return False


def get_catalog_entry(asin: str, db_path: Path = DEFAULT_CATALOG_DB_PATH) -> Optional[Dict[str, Any]]:
    """
    Fetch a catalog row (title, image, payload) for an ASIN.
    Returns None if the DB is missing or the ASIN is not found.
    """
    if not asin:
        return None
    if not db_path.exists():
        return None
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT title, image, payload FROM spapi_catalog WHERE asin = ?",
                (asin,),
            ).fetchone()
            return dict(row) if row else None
    except Exception as exc:
        logger.error(f"[Catalog] Failed to fetch catalog entry for {asin}: {exc}", exc_info=True)
        raise


def parse_catalog_payload(payload_raw: Any, *, include_raw: bool = True) -> Dict[str, Any]:
    """
    Parse stored catalog payload JSON with safe fallback.
    If include_raw is True, returns {"raw": payload_raw} on parse errors.
    """
    if not payload_raw:
        return {}
    try:
        if isinstance(payload_raw, str):
            return json.loads(payload_raw)
        if isinstance(payload_raw, dict):
            return payload_raw
        return {}
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to parse catalog payload: {exc}")
        return {"raw": payload_raw} if include_raw else {}
