import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.catalog_service import DEFAULT_CATALOG_DB_PATH
from services.db import get_db_connection
from services.utils_barcodes import is_asin

LOGGER = logging.getLogger(__name__)


@contextmanager
def _connection(db_path: Path):
    resolved = Path(db_path).resolve()
    default = Path(DEFAULT_CATALOG_DB_PATH).resolve()
    if resolved == default:
        with get_db_connection() as conn:
            yield conn
    else:
        conn = sqlite3.connect(resolved)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def ensure_vendor_rt_inventory_state_table(
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> None:
    with _connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_rt_inventory_state (
                asin TEXT PRIMARY KEY,
                sellable INTEGER NOT NULL DEFAULT 0,
                last_end_time TEXT,
                updated_at TEXT,
                marketplace_id TEXT,
                source TEXT
            )
            """
        )
        conn.commit()


def ensure_vendor_rt_inventory_checkpoint_table(
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> None:
    with _connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_rt_inventory_checkpoint (
                marketplace_id TEXT PRIMARY KEY,
                last_end_time TEXT,
                updated_at TEXT
            )
            """
        )
        conn.commit()


def get_checkpoint(
    marketplace_id: str,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> Optional[str]:
    if not marketplace_id:
        return None
    ensure_vendor_rt_inventory_checkpoint_table(db_path)
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT last_end_time FROM vendor_rt_inventory_checkpoint WHERE marketplace_id = ?",
            (marketplace_id,),
        ).fetchone()
    if not row:
        return None
    return parse_end_time(row["last_end_time"])


def set_checkpoint(
    marketplace_id: str,
    last_end_time_iso: Any,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> None:
    """
    Persist checkpoint monotonically:
      checkpoint_time = max(incoming_time, MAX(state.last_end_time for marketplace))
    """
    if not marketplace_id:
        raise ValueError("marketplace_id is required for checkpoint")

    normalized = parse_end_time(last_end_time_iso)
    if not normalized:
        raise ValueError("last_end_time_iso must be a valid ISO timestamp")

    ensure_vendor_rt_inventory_checkpoint_table(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    with _connection(db_path) as conn:
        state_max_row = conn.execute(
            "SELECT MAX(last_end_time) AS max_end FROM vendor_rt_inventory_state WHERE marketplace_id = ?",
            (marketplace_id,),
        ).fetchone()
        state_max = (
            parse_end_time(state_max_row["max_end"])
            if state_max_row and state_max_row["max_end"]
            else None
        )

        effective = state_max if state_max and state_max > normalized else normalized

        conn.execute(
            """
            INSERT INTO vendor_rt_inventory_checkpoint (marketplace_id, last_end_time, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(marketplace_id) DO UPDATE SET
                last_end_time=excluded.last_end_time,
                updated_at=excluded.updated_at
            """,
            (marketplace_id, effective, now_iso),
        )
        conn.commit()


def parse_end_time(value: Any) -> Optional[str]:
    """
    Normalize timestamps to strict UTC ISO format:
    YYYY-MM-DDTHH:MM:SS+00:00

    Accepts:
      - datetime objects
      - ISO strings ending with 'Z', '+00', '+00:00'
      - naive ISO strings (assumed UTC)

    Returns:
      - canonical UTC ISO string, or None if invalid/empty
    """
    if value is None:
        return None

    # Pass-through for datetime objects
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None

        # Normalize common suffix variants
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        elif s.endswith("+00") and not s.endswith("+00:00"):
            s = s + ":00"

        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            LOGGER.debug("Could not parse end time %r", value)
            return None

    # Normalize timezone to UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.isoformat()


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None

    # Accept common variants
    if candidate.endswith("Z"):
        candidate = candidate.replace("Z", "+00:00")
    elif candidate.endswith("+00") and not candidate.endswith("+00:00"):
        candidate = candidate + ":00"

    try:
        dt = datetime.fromisoformat(candidate)
    except Exception:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _prepare_row(row: Dict[str, Any]) -> Optional[Tuple[str, str, int]]:
    asin = (row.get("asin") or "").strip().upper()
    if not asin or not is_asin(asin):
        LOGGER.debug("Skipping invalid ASIN row: %s", row)
        return None

    end_time_raw = row.get("endTime") or row.get("end_time")
    end_time = parse_end_time(end_time_raw)
    if not end_time:
        LOGGER.debug("Skipping ASIN %s due to invalid end time %r", asin, end_time_raw)
        return None

    sellable_raw = row.get("highlyAvailableInventory") or row.get("sellable") or 0
    try:
        sellable = int(sellable_raw)
    except Exception:
        LOGGER.debug("Skipping ASIN %s due to invalid sellable %r", asin, sellable_raw)
        return None

    return asin, end_time, sellable


def bootstrap_state_from_rows(
    rows: List[Dict[str, Any]],
    marketplace_id: str = "",
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> Dict[str, Any]:
    ensure_vendor_rt_inventory_state_table(db_path)
    latest: Dict[str, Tuple[str, datetime, int]] = {}
    max_end_time: Optional[str] = None

    for row in rows or []:
        prepared = _prepare_row(row)
        if not prepared:
            continue
        asin, end_time_iso, sellable = prepared
        end_dt = _parse_iso_datetime(end_time_iso)
        if not end_dt:
            continue

        if max_end_time is None or end_time_iso > max_end_time:
            max_end_time = end_time_iso

        existing = latest.get(asin)
        if existing is None or end_dt > existing[1]:
            latest[asin] = (end_time_iso, end_dt, sellable)

    upserted = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connection(db_path) as conn:
        for asin, (end_iso, _end_dt, sellable) in latest.items():
            conn.execute(
                """
                INSERT INTO vendor_rt_inventory_state
                    (asin, sellable, last_end_time, updated_at, marketplace_id, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    sellable=excluded.sellable,
                    last_end_time=excluded.last_end_time,
                    updated_at=excluded.updated_at,
                    marketplace_id=excluded.marketplace_id,
                    source=excluded.source
                """,
                (asin, sellable, end_iso, now_iso, marketplace_id, "bootstrap"),
            )
            upserted += 1
        conn.commit()

    stats: Dict[str, Any] = {
        "asins_seen": len(latest),
        "asins_upserted": upserted,
        "max_end_time": max_end_time,
    }
    LOGGER.info("[VendorRtState] bootstrap complete: %s", stats)
    return stats


def apply_incremental_rows(
    rows: List[Dict[str, Any]],
    marketplace_id: str = "",
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> Dict[str, Any]:
    ensure_vendor_rt_inventory_state_table(db_path)

    rows_seen = 0
    rows_applied = 0
    rows_ignored_invalid = 0
    rows_ignored_older = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    max_end_time: Optional[str] = None

    with _connection(db_path) as conn:
        for row in rows or []:
            rows_seen += 1
            prepared = _prepare_row(row)
            if not prepared:
                rows_ignored_invalid += 1
                continue

            asin, end_time_iso, sellable = prepared

            if max_end_time is None or end_time_iso > max_end_time:
                max_end_time = end_time_iso

            new_dt = _parse_iso_datetime(end_time_iso)
            if not new_dt:
                rows_ignored_invalid += 1
                continue

            existing = conn.execute(
                "SELECT last_end_time FROM vendor_rt_inventory_state WHERE asin = ?",
                (asin,),
            ).fetchone()

            apply_row = False
            if not existing or not existing["last_end_time"]:
                apply_row = True
            else:
                existing_dt = _parse_iso_datetime(existing["last_end_time"])
                if existing_dt is None or new_dt > existing_dt:
                    apply_row = True

            if not apply_row:
                rows_ignored_older += 1
                continue

            conn.execute(
                """
                INSERT INTO vendor_rt_inventory_state
                    (asin, sellable, last_end_time, updated_at, marketplace_id, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    sellable=excluded.sellable,
                    last_end_time=excluded.last_end_time,
                    updated_at=excluded.updated_at,
                    marketplace_id=excluded.marketplace_id,
                    source=excluded.source
                """,
                (asin, sellable, end_time_iso, now_iso, marketplace_id, "incremental"),
            )
            rows_applied += 1

        conn.commit()

    rows_ignored = rows_ignored_invalid + rows_ignored_older
    stats: Dict[str, Any] = {
        "rows_seen": rows_seen,
        "rows_applied": rows_applied,
        "rows_ignored": rows_ignored,
        "ignored_invalid": rows_ignored_invalid,
        "ignored_older_or_equal_end_time": rows_ignored_older,
        "max_end_time": max_end_time,
    }
    LOGGER.info(
        "[VendorRtState] incremental: seen=%s applied=%s ignored=%s (invalid=%s older=%s) max_end=%s",
        rows_seen,
        rows_applied,
        rows_ignored,
        rows_ignored_invalid,
        rows_ignored_older,
        max_end_time,
    )
    return stats


def get_state_snapshot(
    limit: Optional[int] = None,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
) -> List[Dict[str, Any]]:
    ensure_vendor_rt_inventory_state_table(db_path)
    query = """
        SELECT asin, sellable, last_end_time, updated_at, marketplace_id, source
        FROM vendor_rt_inventory_state
        ORDER BY sellable DESC, asin ASC
    """
    params: Tuple[Any, ...] = ()
    if limit is not None and limit > 0:
        query += " LIMIT ?"
        params = (int(limit),)
    with _connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]
