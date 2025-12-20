import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from services.db import get_db_connection
from services.perf import time_block

from .utils_barcodes import is_asin

schema_logger = logging.getLogger("forecast_schema")
logger = logging.getLogger(__name__)
_FORECAST_DISABLED_LOGGED = False

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_DB_PATH = ROOT / "catalog.db"
VALID_ASIN_SOURCES = {
    "vendor_po",
    "realtime_inventory",
    "realtime_sales",
    "manual",
    "other",
}


def init_catalog_db(db_path: Path = DEFAULT_CATALOG_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        with time_block("catalog_init"):
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_fetch_attempts (
                    asin TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_attempt_at TEXT,
                    terminal_code TEXT,
                    terminal_message TEXT
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
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_spapi_catalog_meta_sku ON spapi_catalog_meta(sku)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_spapi_catalog_barcode ON spapi_catalog(barcode)"
                )
                conn.commit()
            except Exception as exc:
                schema_logger.warning(f"[CatalogSchema] Failed to ensure catalog indexes: {exc}")
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(catalog_fetch_attempts)").fetchall()}
                if "terminal_code" not in cols:
                    conn.execute("ALTER TABLE catalog_fetch_attempts ADD COLUMN terminal_code TEXT")
                if "terminal_message" not in cols:
                    conn.execute("ALTER TABLE catalog_fetch_attempts ADD COLUMN terminal_message TEXT")
                conn.commit()
            except Exception as exc:
                schema_logger.warning(f"[CatalogSchema] Failed to ensure fetch attempt columns: {exc}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_asin_sources (
                    asin TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_seen_at TEXT,
                    PRIMARY KEY (asin, source)
                )
                """
            )
            conn.commit()
    global _FORECAST_DISABLED_LOGGED
    if not _FORECAST_DISABLED_LOGGED:
        schema_logger.info("Forecast feature disabled: init_forecast_tables() not called")
        _FORECAST_DISABLED_LOGGED = True


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
        with time_block("catalog_upsert"):
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
        with time_block("catalog_status_fetch"):
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
        with time_block("catalog_entry_lookup"):
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


def list_catalog_indexes(db_path: Path = DEFAULT_CATALOG_DB_PATH) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    if not db_path.exists():
        return indexes
    try:
        with get_db_connection() as conn:
            for table in ("spapi_catalog", "spapi_catalog_meta"):
                rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
                for row in rows:
                    indexes.append({"table": table, "name": row["name"], "unique": bool(row["unique"])})
    except Exception as exc:
        logger.debug(f"[Catalog] Could not list indexes: {exc}")
    return indexes


def ensure_asin_in_universe(asin: str, db_path: Path = DEFAULT_CATALOG_DB_PATH) -> None:
    """
    Guarantee that an ASIN exists in spapi_catalog_meta so it appears in the catalog universe.
    SKU is optional at this stage; INSERT OR IGNORE prevents overwriting existing metadata.
    """
    asin = (asin or "").strip().upper()
    if not asin:
        return
    init_catalog_db(db_path)
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO spapi_catalog_meta (asin, sku) VALUES (?, NULL)",
                (asin,),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to ensure ASIN {asin} in universe: {exc}")


def list_universe_asins(db_path: Path = DEFAULT_CATALOG_DB_PATH) -> List[str]:
    """
    Return the sorted union of ASINs from spapi_catalog and spapi_catalog_meta.
    Skips blank/null entries.
    """
    if not db_path.exists():
        return []
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT asin FROM spapi_catalog WHERE asin IS NOT NULL AND TRIM(asin) <> ''
                UNION
                SELECT asin FROM spapi_catalog_meta WHERE asin IS NOT NULL AND TRIM(asin) <> ''
                ORDER BY asin COLLATE NOCASE
                """
            ).fetchall()
            return [row[0] for row in rows if row[0]]
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to list catalog universe: {exc}")
        return []


def seed_catalog_universe(asins: Iterable[str], db_path: Path = DEFAULT_CATALOG_DB_PATH) -> int:
    """
    Batch-insert ASINs into the catalog universe (spapi_catalog_meta) if missing.
    Returns the number of new ASINs inserted.
    """
    normalized = {
        (asin or "").strip().upper()
        for asin in (asins or [])
        if asin and is_asin((asin or "").strip().upper())
    }
    if not normalized:
        return 0
    init_catalog_db(db_path)
    before = 0
    after = 0
    try:
        with get_db_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM spapi_catalog_meta").fetchone()
            before = int(row[0]) if row and row[0] is not None else 0
            rows = [(asin,) for asin in normalized]
            conn.executemany(
                "INSERT OR IGNORE INTO spapi_catalog_meta (asin, sku) VALUES (?, NULL)",
                rows,
            )
            conn.commit()
            row = conn.execute("SELECT COUNT(*) FROM spapi_catalog_meta").fetchone()
            after = int(row[0]) if row and row[0] is not None else before
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to seed catalog universe: {exc}")
        return 0
    return max(0, after - before)


def record_catalog_asin_source(
    asin: str,
    source: str,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> None:
    asin_norm = (asin or "").strip().upper()
    source_norm = (source or "").strip()
    if not asin_norm or source_norm not in VALID_ASIN_SOURCES:
        return
    init_catalog_db(db_path)
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO catalog_asin_sources (asin, source, first_seen_at)
                VALUES (?, ?, ?)
                """,
                (asin_norm, source_norm, timestamp),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to record asin source for {asin_norm}: {exc}")


def record_catalog_asin_sources(
    asins: Iterable[str],
    source: str,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> None:
    source_norm = (source or "").strip()
    if source_norm not in VALID_ASIN_SOURCES:
        return
    normalized = {
        (asin or "").strip().upper()
        for asin in (asins or [])
        if asin and is_asin((asin or "").strip().upper())
    }
    if not normalized:
        return
    init_catalog_db(db_path)
    timestamp = datetime.now(timezone.utc).isoformat()
    rows = [(asin, source_norm, timestamp) for asin in normalized]
    try:
        with get_db_connection() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO catalog_asin_sources (asin, source, first_seen_at)
                VALUES (?, ?, ?)
                """,
                rows,
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to record asin sources ({source_norm}): {exc}")


def get_catalog_fetch_attempts(asin: str, db_path: Path = DEFAULT_CATALOG_DB_PATH) -> int:
    if not asin:
        return 0
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT attempts FROM catalog_fetch_attempts WHERE asin = ?",
                (asin,),
            ).fetchone()
            return int(row["attempts"]) if row and row["attempts"] is not None else 0
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to read fetch attempts for {asin}: {exc}")
        return 0


def record_catalog_fetch_attempt(
    asin: str,
    ok: bool,
    error: Optional[str] = None,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> None:
    asin = (asin or "").strip().upper()
    if not asin:
        return
    init_catalog_db(db_path)
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_connection() as conn:
            if ok:
                conn.execute(
                    """
                    INSERT INTO catalog_fetch_attempts (asin, attempts, last_error, last_attempt_at)
                    VALUES (?, 0, NULL, ?)
                    ON CONFLICT(asin) DO UPDATE SET
                        attempts = 0,
                        last_error = NULL,
                        last_attempt_at = excluded.last_attempt_at
                    """,
                    (asin, timestamp),
                )
            else:
                truncated_error = (error or "")[:500]
                conn.execute(
                    """
                    INSERT INTO catalog_fetch_attempts (asin, attempts, last_error, last_attempt_at)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(asin) DO UPDATE SET
                        attempts = COALESCE(catalog_fetch_attempts.attempts, 0) + 1,
                        last_error = excluded.last_error,
                        last_attempt_at = excluded.last_attempt_at
                    """,
                    (asin, truncated_error, timestamp),
                )
            conn.commit()
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to record fetch attempt for {asin}: {exc}")


def should_fetch_catalog(
    asin: str,
    fetched: bool,
    max_attempts: int = 5,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> bool:
    asin_norm = (asin or "").strip().upper()
    if fetched or not asin_norm:
        return False
    attempts = 0
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT attempts, terminal_code FROM catalog_fetch_attempts WHERE asin = ?",
                (asin_norm,),
            ).fetchone()
            if row and row["terminal_code"]:
                return False
            attempts = int(row["attempts"] or 0) if row and row["attempts"] is not None else 0
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to check fetch attempts for {asin_norm}: {exc}")
        attempts = get_catalog_fetch_attempts(asin_norm, db_path=db_path)
    return attempts < max_attempts


def get_catalog_fetch_attempts_map(
    asins: Iterable[str],
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> Dict[str, Dict[str, Any]]:
    """
    Bulk read catalog_fetch_attempts rows for provided ASINs.
    """
    normalized: Set[str] = {
        (asin or "").strip().upper()
        for asin in (asins or [])
        if asin and is_asin((asin or "").strip().upper())
    }
    if not normalized:
        return {}
    try:
        with get_db_connection() as conn:
            placeholders = ", ".join(["?"] * len(normalized))
            query = (
                "SELECT asin, attempts, last_error, last_attempt_at, terminal_code, terminal_message "
                f"FROM catalog_fetch_attempts WHERE asin IN ({placeholders})"
            )
            rows = conn.execute(query, tuple(normalized)).fetchall()
            attempt_map: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                asin = (row["asin"] or "").strip().upper()
                if not asin:
                    continue
                attempt_map[asin] = {
                    "attempts": int(row["attempts"] or 0),
                    "last_error": row["last_error"],
                    "last_attempt_at": row["last_attempt_at"],
                    "terminal_code": row["terminal_code"],
                    "terminal_message": row["terminal_message"],
                }
            return attempt_map
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to read fetch attempts map: {exc}")
        return {}


def get_catalog_asin_sources_map(
    asins: Iterable[str],
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> Dict[str, List[str]]:
    normalized = {
        (asin or "").strip().upper()
        for asin in (asins or [])
        if asin and is_asin((asin or "").strip().upper())
    }
    if not normalized:
        return {}
    try:
        with get_db_connection() as conn:
            placeholders = ", ".join(["?"] * len(normalized))
            query = (
                f"SELECT asin, source FROM catalog_asin_sources "
                f"WHERE asin IN ({placeholders}) ORDER BY source"
            )
            rows = conn.execute(query, tuple(normalized)).fetchall()
            result: Dict[str, List[str]] = {}
            for row in rows:
                asin = (row["asin"] or "").strip().upper()
                source = row["source"]
                if not asin or not source:
                    continue
                result.setdefault(asin, [])
                if source not in result[asin]:
                    result[asin].append(source)
            return result
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to read asin sources: {exc}")
        return {}


def reset_catalog_fetch_attempts(asin: str, db_path: Path = DEFAULT_CATALOG_DB_PATH) -> bool:
    asin_norm = (asin or "").strip().upper()
    if not asin_norm or not is_asin(asin_norm):
        return False
    init_catalog_db(db_path)
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM catalog_fetch_attempts WHERE asin = ?", (asin_norm,))
            conn.commit()
        return True
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to reset fetch attempts for {asin_norm}: {exc}")
        return False


def reset_all_catalog_fetch_attempts(db_path: Path = DEFAULT_CATALOG_DB_PATH) -> int:
    init_catalog_db(db_path)
    try:
        with get_db_connection() as conn:
            cur = conn.execute("DELETE FROM catalog_fetch_attempts")
            conn.commit()
            return cur.rowcount or 0
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to reset all fetch attempts: {exc}")
        return 0

def mark_catalog_fetch_terminal(
    asin: str,
    code: str,
    message: Optional[str] = None,
    *,
    max_attempts: int = 999,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> None:
    asin_norm = (asin or "").strip().upper()
    if not asin_norm:
        return
    init_catalog_db(db_path)
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO catalog_fetch_attempts (asin, attempts, last_error, last_attempt_at, terminal_code, terminal_message)
                VALUES (?, ?, NULL, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    attempts = excluded.attempts,
                    last_error = NULL,
                    last_attempt_at = excluded.last_attempt_at,
                    terminal_code = excluded.terminal_code,
                    terminal_message = excluded.terminal_message
                """,
                (
                    asin_norm,
                    max_attempts,
                    timestamp,
                    code,
                    (message or "")[:500] if message else None,
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.warning(f"[Catalog] Failed to mark terminal for {asin_norm}: {exc}")
