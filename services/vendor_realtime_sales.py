"""
Vendor Real Time Sales Report handler.

Consumes GET_VENDOR_REAL_TIME_SALES_REPORT from SP-API and provides:
- Ingestion into SQLite (vendor_realtime_sales table)
- State tracking (vendor_rt_sales_state table) to avoid gaps
- Backfill logic with safe time windows
- Aggregation and querying for UI
- Support for flexible lookback windows and view-by modes (ASIN / Time)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

try:
    # Python 3.9+ standard lib
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:
    # Fallback for environments without zoneinfo
    ZoneInfo = None
    class ZoneInfoNotFoundError(Exception):
        pass

from services.catalog_service import (
    record_catalog_asin_sources,
    seed_catalog_universe,
)
from services.db import execute_many_write, execute_write, get_db_connection
from services.perf import time_block
from services.spapi_reports import (
    SpApiQuotaError,
    download_vendor_report_document,
    poll_vendor_report,
    request_vendor_report,
)

logger = logging.getLogger(__name__)

# UAE timezone: prefer real IANA zone, fall back to fixed UTC+4
try:
    if ZoneInfo is None:
        raise ZoneInfoNotFoundError("zoneinfo not available")
    UAE_TZ = ZoneInfo("Asia/Dubai")
except ZoneInfoNotFoundError:
    # Fallback: fixed UTC+4, good enough for UAE (no DST)
    UAE_TZ = timezone(timedelta(hours=4))

# ====================================================================
# QUOTA AND AUDIT CONFIGURATION
# ====================================================================
# Set to False to disable weekly audits (which can generate many API calls)
ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT = False

# Set to False to disable daily audits
ENABLE_VENDOR_RT_SALES_DAILY_AUDIT = True

# In-memory quota cooldown tracking
_rt_sales_quota_cooldown_until_utc = None  # type: Optional[datetime]
QUOTA_COOLDOWN_MINUTES = 30

# Logging prefixes (SP-API entry points share the same sales tag for easier filtering)
LOG_PREFIX_API = "[VendorRtSales]"
LOG_PREFIX_INGEST = "[VendorRtSales]"
LOG_PREFIX_TRENDS = "[VendorRtTrends]"
LOG_PREFIX_AUDIT = "[VendorRtAudit]"
LOG_PREFIX_FILL_DAY = "[VendorRtSales]"
LOG_PREFIX_COOLDOWN = "[VendorRtCooldown]"
LOG_PREFIX_SUMMARY = "[VendorRtSummary]"
LOG_PREFIX_ADMIN = "[VendorRtAdmin]"


class VendorRtCooldownBlock(SpApiQuotaError):
    """Raised when we skip an SP-API call because the global cooldown is already active."""

    def __init__(self, cooldown_until: Optional[datetime]) -> None:
        message = (
            f"Cooldown active until {cooldown_until.isoformat()}"
            if cooldown_until
            else "Cooldown active (until unknown)"
        )
        super().__init__(message)
        self.cooldown_until = cooldown_until


def cooldown_remaining_seconds() -> int:
    """Return how many seconds remain in the active quota cooldown (0 if none)."""
    if _rt_sales_quota_cooldown_until_utc is None:
        return 0
    now = datetime.now(timezone.utc)
    remaining = int((_rt_sales_quota_cooldown_until_utc - now).total_seconds())
    return max(0, remaining)


def _format_cooldown_until() -> Optional[str]:
    if _rt_sales_quota_cooldown_until_utc is None:
        return None
    return _rt_sales_quota_cooldown_until_utc.isoformat()


def _ensure_spapi_call_allowed(action_label: str) -> None:
    """Raise VendorRtCooldownBlock if a cooldown is currently active."""
    now = datetime.now(timezone.utc)
    if is_in_quota_cooldown(now):
        until = _rt_sales_quota_cooldown_until_utc
        until_str = until.isoformat() if until else "unknown"
        logger.warning(
            f"{LOG_PREFIX_API} Request blocked due to cooldown active until {until_str} ({action_label})"
        )
        raise VendorRtCooldownBlock(until)

# In-memory backfill lock to prevent overlapping cycles
_rt_sales_backfill_in_progress = False
_rt_sales_backfill_lock_acquired_at_utc = None  # type: Optional[datetime]

# One-time 4-week backfill gate (stored in app_kv_store)
SALES_TRENDS_4W_BACKFILL_KEY = "rt_sales_4week_backfill_done"


def is_in_quota_cooldown(now_utc: datetime) -> bool:
    """Check if we're currently in a quota cooldown period."""
    global _rt_sales_quota_cooldown_until_utc
    return (
        _rt_sales_quota_cooldown_until_utc is not None
        and now_utc < _rt_sales_quota_cooldown_until_utc
    )


def is_backfill_in_progress() -> bool:
    """Check if a backfill is currently running."""
    global _rt_sales_backfill_in_progress
    return _rt_sales_backfill_in_progress


def start_backfill() -> bool:
    """
    Attempt to acquire the backfill lock.
    Returns True if acquired, False if already in progress.
    """
    global _rt_sales_backfill_in_progress, _rt_sales_backfill_lock_acquired_at_utc
    if _rt_sales_backfill_in_progress:
        return False
    _rt_sales_backfill_in_progress = True
    _rt_sales_backfill_lock_acquired_at_utc = datetime.now(timezone.utc)
    logger.debug(f"{LOG_PREFIX_INGEST} Backfill lock acquired")
    return True


def end_backfill() -> None:
    """Release the backfill lock."""
    global _rt_sales_backfill_in_progress, _rt_sales_backfill_lock_acquired_at_utc
    if _rt_sales_backfill_in_progress and _rt_sales_backfill_lock_acquired_at_utc:
        elapsed = (datetime.now(timezone.utc) - _rt_sales_backfill_lock_acquired_at_utc).total_seconds()
        logger.debug(
            f"{LOG_PREFIX_INGEST} Backfill lock released (held for {elapsed:.1f}s)"
        )
    _rt_sales_backfill_in_progress = False
    _rt_sales_backfill_lock_acquired_at_utc = None


def start_quota_cooldown(now_utc: datetime) -> None:
    """Start a quota cooldown period (prevents further API calls for a while)."""
    global _rt_sales_quota_cooldown_until_utc
    new_until = now_utc + timedelta(minutes=QUOTA_COOLDOWN_MINUTES)
    if _rt_sales_quota_cooldown_until_utc is None or new_until > _rt_sales_quota_cooldown_until_utc:
        _rt_sales_quota_cooldown_until_utc = new_until
        logger.warning(
            f"{LOG_PREFIX_COOLDOWN} Quota cooldown started until {_rt_sales_quota_cooldown_until_utc.isoformat()}"
        )
    else:
        logger.debug(
            f"{LOG_PREFIX_COOLDOWN} Cooldown already active until {_rt_sales_quota_cooldown_until_utc.isoformat()}; keeping existing window"
        )


# ====================================================================
# DAILY AUDIT GATING (UAE CALENDAR DATE)
# ====================================================================
LAST_AUDIT_KEY = "rt_sales_last_audit_date_uae"


def should_run_rt_sales_daily_audit(conn) -> Tuple[bool, str]:
    """
    Check if daily audit should run based on UAE calendar date.
    Returns (should_run, today_str) where today_str is the UAE date in ISO format.
    """
    from services import db as db_service
    
    uae_today = datetime.now(UAE_TZ).date()
    today_str = uae_today.isoformat()
    last = db_service.get_app_kv(conn, LAST_AUDIT_KEY)
    if last == today_str:
        return False, today_str
    return True, today_str


def mark_rt_sales_daily_audit_ran(conn, today_str: str) -> None:
    """Mark that the daily audit ran for the given UAE date."""
    from services import db as db_service
    db_service.set_app_kv(conn, LAST_AUDIT_KEY, today_str)


def get_rt_sales_status(now_utc: Optional[datetime] = None) -> dict:
    """
    Return status of the Real-Time Sales auto-sync/backfill system.
    
    Returns:
        {
            "busy": bool,  # True if backfill/auto-sync is actively running
            "cooldown_active": bool,  # True if quota cooldown is active
            "cooldown_until_utc": Optional[str],  # ISO8601, or None
            "cooldown_until_uae": Optional[str],  # ISO8601 in UAE time, or None
            "message": str  # "busy", "cooldown", or "idle"
        }
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    
    global _rt_sales_backfill_in_progress, _rt_sales_quota_cooldown_until_utc
    
    busy = _rt_sales_backfill_in_progress
    cooldown_active = is_in_quota_cooldown(now_utc)

    cooldown_until_utc = None
    cooldown_until_uae = None
    
    if cooldown_active and _rt_sales_quota_cooldown_until_utc:
        cooldown_until_utc = _rt_sales_quota_cooldown_until_utc.isoformat()
        cooldown_until_uae = _rt_sales_quota_cooldown_until_utc.astimezone(UAE_TZ).isoformat()
    
    if busy:
        message = "busy"
    elif cooldown_active:
        message = "cooldown"
    else:
        message = "idle"

    return {
        "busy": busy,
        "cooldown_active": cooldown_active,
        "cooldown_until_utc": cooldown_until_utc,
        "cooldown_until_uae": cooldown_until_uae,
        "cooldown_remaining_seconds": cooldown_remaining_seconds(),
        "message": message
    }


# ====================================================================
# TIME CONSTANTS FOR SAFE BACKFILLING
# ====================================================================
SAFE_MINUTES_LAG = 10       # Buffer to avoid future/not-yet-ready hours
SAFETY_LOOKBACK_MINUTES = 30  # Extra buffer to catch late-arriving hours in lookback windows
MAX_HISTORY_DAYS = 3        # How far back we backfill on startup
CHUNK_HOURS = 6             # Window size per report request

# Limit how many hourly reports the Master Auditor can request per fill-day run
# Max missing hours to repair per Fill Day click
MAX_HOURLY_REPORTS_PER_FILL_DAY = int(
    os.getenv("VENDOR_RT_MAX_HOURS_PER_FILL_DAY", "3")
)

# Hour ledger configuration
LEDGER_SAFETY_LAG_MINUTES = int(os.getenv("VENDOR_RT_LEDGER_SAFETY_LAG_MINUTES", "90"))
LEDGER_COOLDOWN_MINUTES = int(os.getenv("VENDOR_RT_LEDGER_COOLDOWN_MINUTES", "45"))
LEDGER_MAX_HOURS_PER_CYCLE = int(os.getenv("VENDOR_RT_LEDGER_MAX_HOURS", "4"))
LEDGER_DEFAULT_BACKFILL_HOURS = int(os.getenv("VENDOR_RT_LEDGER_DEFAULT_BACKFILL_HOURS", "24"))
LEDGER_TABLE_NAME = "vendor_rt_sales_hour_ledger"
LEDGER_STATUS_MISSING = "MISSING"
LEDGER_STATUS_REQUESTED = "REQUESTED"
LEDGER_STATUS_DOWNLOADED = "DOWNLOADED"
LEDGER_STATUS_APPLIED = "APPLIED"
LEDGER_STATUS_FAILED = "FAILED"

# ====================================================================
# AUDIT CONFIGURATION
# ====================================================================
AUDIT_CALENDAR_DEFAULT_DAYS = 30
AUDIT_CALENDAR_MAX_DAYS = 30

def get_safe_now_utc() -> datetime:
    """
    Return the current UTC time minus SAFE_MINUTES_LAG.
    Used for dataEndTime to avoid requesting future/not-yet-ready hours.
    """
    return datetime.now(timezone.utc) - timedelta(minutes=SAFE_MINUTES_LAG)


def get_last_ingested_end_utc(conn, marketplace_id: str) -> Optional[datetime]:
    """
    Get the last fully ingested hour end time for a marketplace.
    
    Args:
        conn: SQLite connection (from get_db_connection context)
        marketplace_id: The marketplace ID
    
    Returns:
        Timezone-aware datetime (UTC) or None if not found.
    """
    from services.db import get_last_ingested_end_utc_db
    
    utc_str = get_last_ingested_end_utc_db(conn, marketplace_id)
    if utc_str:
        try:
            return datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        except Exception as e:
            logger.warning(f"{LOG_PREFIX_INGEST} Failed to parse last_ingested_end_utc {utc_str}: {e}")
    return None


def update_last_ingested_end_utc(marketplace_id: str, new_end: datetime) -> None:
    """
    Update the last ingested hour end time for a marketplace.
    
    Args:
        marketplace_id: The marketplace ID
        new_end: Timezone-aware datetime (UTC)
    """
    from services.db import get_db_connection, update_last_ingested_end_utc_db
    
    # Normalize to ISO8601 with Z suffix
    end_utc_str = new_end.isoformat().replace("+00:00", "Z")
    
    try:
        with get_db_connection() as conn:
            update_last_ingested_end_utc_db(conn, marketplace_id, end_utc_str)
            logger.debug(
                f"{LOG_PREFIX_INGEST} Updated last_ingested_end_utc for {marketplace_id} to {end_utc_str}"
            )
    except Exception as exc:
        logger.error(
            f"{LOG_PREFIX_INGEST} Failed to update state for {marketplace_id}: {exc}",
            exc_info=True
        )


def init_vendor_realtime_sales_table() -> None:
    """Create vendor_realtime_sales table if it does not exist."""
    sql = """
    CREATE TABLE IF NOT EXISTS vendor_realtime_sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT NOT NULL,
        hour_start_utc TEXT NOT NULL,
        hour_end_utc TEXT NOT NULL,
        ordered_units INTEGER NOT NULL,
        ordered_revenue REAL NOT NULL,
        marketplace_id TEXT NOT NULL,
        currency_code TEXT NOT NULL,
        ingested_at_utc TEXT NOT NULL
    )
    """
    try:
        execute_write(sql)
        
        # Create unique index
        with get_db_connection() as conn:
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_rt_sales_unique
                ON vendor_realtime_sales (asin, hour_start_utc, marketplace_id)
                """
            )
            conn.commit()
        logger.info(f"{LOG_PREFIX_INGEST} vendor_realtime_sales table ensured")
    except Exception as exc:
        logger.error(f"{LOG_PREFIX_INGEST} Failed to ensure table: {exc}", exc_info=True)
        raise


def init_vendor_rt_audit_hours_table() -> None:
    """Create vendor_rt_audit_hours table for tracking audited hour status."""
    sql = """
    CREATE TABLE IF NOT EXISTS vendor_rt_audit_hours (
        marketplace_id TEXT NOT NULL,
        hour_start_utc TEXT NOT NULL,
        hour_end_utc TEXT NOT NULL,
        status TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL,
        PRIMARY KEY (marketplace_id, hour_start_utc)
    )
    """
    try:
        execute_write(sql)
        logger.info(f"{LOG_PREFIX_AUDIT} vendor_rt_audit_hours table ensured")
    except Exception as exc:
        logger.error(f"{LOG_PREFIX_AUDIT} Failed to ensure audit hours table: %s", exc, exc_info=True)
        raise


# ====================================================================
# STATE TRACKING HELPERS
# ====================================================================

def get_last_ingested_end_utc(conn, marketplace_id: str) -> Optional[datetime]:
    """
    Retrieve the last fully ingested hour end time for a marketplace.
    Returns timezone-aware UTC datetime or None if never ingested.
    """
    try:
        row = conn.execute(
            "SELECT last_ingested_end_utc FROM vendor_rt_sales_state WHERE marketplace_id = ?",
            (marketplace_id,)
        ).fetchone()
        if row and row["last_ingested_end_utc"]:
            return datetime.fromisoformat(row["last_ingested_end_utc"]).replace(tzinfo=timezone.utc)
        return None
    except Exception as e:
        logger.warning(f"{LOG_PREFIX_INGEST} Failed to get last_ingested_end_utc: {e}")
        return None


def update_last_ingested_end_utc(marketplace_id: str, new_end: datetime) -> None:
    """
    Update the last fully ingested hour end time for a marketplace.
    """
    try:
        # Ensure it's UTC
        if new_end.tzinfo is None:
            new_end = new_end.replace(tzinfo=timezone.utc)
        
        end_str = new_end.isoformat()
        sql = """
        INSERT OR REPLACE INTO vendor_rt_sales_state (marketplace_id, last_ingested_end_utc)
        VALUES (?, ?)
        """
        execute_write(sql, (marketplace_id, end_str))
        logger.debug(f"{LOG_PREFIX_INGEST} Updated state for {marketplace_id}: {end_str}")
    except Exception as e:
        logger.error(f"{LOG_PREFIX_INGEST} Failed to update state: {e}", exc_info=True)
        raise


def get_audit_state(marketplace_id: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Get the last daily and weekly audit timestamps for a marketplace.
    
    Returns:
        (last_daily_audit_utc, last_weekly_audit_utc) as timezone-aware UTC datetimes or None.
    """
    from services.db import get_db_connection, get_vendor_rt_sales_state_db
    
    try:
        with get_db_connection() as conn:
            state = get_vendor_rt_sales_state_db(conn, marketplace_id)
            
            daily = None
            if state.get("last_daily_audit_utc"):
                try:
                    daily = datetime.fromisoformat(state["last_daily_audit_utc"].replace("Z", "+00:00"))
                except Exception:
                    pass
            
            weekly = None
            if state.get("last_weekly_audit_utc"):
                try:
                    weekly = datetime.fromisoformat(state["last_weekly_audit_utc"].replace("Z", "+00:00"))
                except Exception:
                    pass
            
            return (daily, weekly)
    except Exception as e:
        logger.warning(f"{LOG_PREFIX_AUDIT} Failed to get audit state: {e}")
        return (None, None)


def update_daily_audit_state(marketplace_id: str, ts: datetime) -> None:
    """Persist the last daily audit timestamp."""
    from services.db import get_db_connection, update_last_daily_audit_utc_db
    
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        
        ts_str = ts.isoformat().replace("+00:00", "Z")
        
        with get_db_connection() as conn:
            update_last_daily_audit_utc_db(conn, marketplace_id, ts_str)
        
        logger.debug(f"{LOG_PREFIX_AUDIT} Updated daily audit state for {marketplace_id}: {ts_str}")
    except Exception as e:
        logger.error(f"{LOG_PREFIX_AUDIT} Failed to update daily audit state: {e}", exc_info=True)
        raise


def update_weekly_audit_state(marketplace_id: str, ts: datetime) -> None:
    """Persist the last weekly audit timestamp."""
    from services.db import get_db_connection, update_last_weekly_audit_utc_db
    
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        
        ts_str = ts.isoformat().replace("+00:00", "Z")
        
        with get_db_connection() as conn:
            update_last_weekly_audit_utc_db(conn, marketplace_id, ts_str)
        
        logger.debug(f"{LOG_PREFIX_AUDIT} Updated weekly audit state for {marketplace_id}: {ts_str}")
    except Exception as e:
        logger.error(f"{LOG_PREFIX_AUDIT} Failed to update weekly audit state: {e}", exc_info=True)
        raise


def get_vendor_rt_sales_state(conn, marketplace_id: str) -> dict:
    """
    Get the audit state for a marketplace (wrapper around DB helper).
    
    Args:
        conn: SQLite connection
        marketplace_id: The marketplace ID
    
    Returns:
        A dict with keys: marketplace_id, last_ingested_end_utc, last_daily_audit_utc, last_weekly_audit_utc
        All timestamp values are ISO8601 strings or None.
    """
    from services.db import get_vendor_rt_sales_state_db
    
    return get_vendor_rt_sales_state_db(conn, marketplace_id)


def _upsert_vendor_rt_audit_hour(
    marketplace_id: str,
    hour_start: datetime,
    hour_end: datetime,
    status: str,
) -> None:
    """
    Record or update an audited hour entry.
    """
    status = status.upper()
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sql = """
    INSERT INTO vendor_rt_audit_hours
    (marketplace_id, hour_start_utc, hour_end_utc, status, updated_at_utc)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(marketplace_id, hour_start_utc) DO UPDATE SET
        hour_end_utc=excluded.hour_end_utc,
        status=excluded.status,
        updated_at_utc=excluded.updated_at_utc
    """
    execute_write(
        sql,
        (
            marketplace_id,
            _utc_iso(hour_start),
            _utc_iso(hour_end),
            status,
            now_iso,
        ),
    )


def _fetch_vendor_rt_audit_hours(
    conn,
    marketplace_id: str,
    start_iso: str,
    end_iso: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Return a mapping of hour_start_utc -> audit row for the specified range.
    """
    rows = conn.execute(
        """
        SELECT hour_start_utc, hour_end_utc, status
        FROM vendor_rt_audit_hours
        WHERE marketplace_id = ?
          AND hour_start_utc >= ?
          AND hour_start_utc < ?
        """,
        (marketplace_id, start_iso, end_iso),
    ).fetchall()
    return {row["hour_start_utc"]: dict(row) for row in rows}


def _fetch_realtime_sales_hour_starts(
    conn,
    marketplace_id: str,
    start_iso: str,
    end_iso: str,
) -> set[str]:
    """
    Return the set of normalized hour_start_utc values that already exist in vendor_realtime_sales.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT hour_start_utc
        FROM vendor_realtime_sales
        WHERE marketplace_id = ?
          AND hour_start_utc >= ?
          AND hour_start_utc < ?
        """,
        (marketplace_id, start_iso, end_iso),
    ).fetchall()

    hour_keys: set[str] = set()
    for row in rows:
        raw_value = row["hour_start_utc"]
        if not raw_value:
            continue
        try:
            hour_dt = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            hour_keys.add(_utc_iso(hour_dt))
        except Exception:
            hour_keys.add(raw_value)
    return hour_keys


 # Read-only coverage helper for audit calendars and trends coverage metadata.
def _build_hourly_coverage_map(
    conn,
    marketplace_id: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    safe_now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Build a per-hour coverage map for the provided UTC window so UI helpers can decide
    which hours are GOOD, INFERRED, MISSING, or FUTURE_BLOCKED.

    Steps:
      1. Read explicit audit rows stored in vendor_rt_audit_hours.
      2. Infer OK hours from vendor_realtime_sales when no audit row exists.
      3. Label future hours beyond safe_now as FUTURE_BLOCKED, and everything else as MISSING.

    Each entry maps the normalized hour_start ISO string to:
      {
          "hour_start": datetime,
          "hour_end": datetime,
          "status": str,            # one of OK, OK_INFERRED, EMPTY, MISSING, FUTURE_BLOCKED
          "audited_status": str | None,
          "has_sales": bool
      }
    """
    normalized_start = _normalize_utc_datetime(start_utc)
    normalized_end = _normalize_utc_datetime(end_utc)
    if safe_now is None:
        safe_now = get_safe_now_utc()
    safe_now = _normalize_utc_datetime(safe_now)

    # Step 1: load persisted audit rows for the window.
    audit_map = _fetch_vendor_rt_audit_hours(
        conn,
        marketplace_id,
        _utc_iso(normalized_start),
        _utc_iso(normalized_end),
    )
    # Step 2: capture any hours that already exist in vendor_realtime_sales so they can be inferred.
    sales_hour_keys = _fetch_realtime_sales_hour_starts(
        conn,
        marketplace_id,
        _utc_iso(normalized_start),
        _utc_iso(normalized_end),
    )

    coverage: Dict[str, Dict[str, Any]] = {}
    current = normalized_start
    while current < normalized_end:
        next_hour = min(current + timedelta(hours=1), normalized_end)
        hour_key = _utc_iso(current)
        audit_row = audit_map.get(hour_key)
        if audit_row:
            status = (audit_row.get("status") or "").upper()
            has_sales = hour_key in sales_hour_keys
        elif hour_key in sales_hour_keys:
            status = "OK_INFERRED"
            has_sales = True
        elif next_hour > safe_now:
            status = "FUTURE_BLOCKED"
            has_sales = False
        else:
            status = "MISSING"
            has_sales = False

        coverage[hour_key] = {
            "hour_start": current,
            "hour_end": next_hour,
            "status": status,
            "audited_status": audit_row.get("status") if audit_row else None,
            "has_sales": has_sales,
        }
        current = next_hour

    return coverage


# ====================================================================
# SAFE TIME WINDOW HELPERS
# ====================================================================

def get_safe_now_utc() -> datetime:
    """
    Get current time in UTC, minus SAFE_MINUTES_LAG buffer.
    Real-time sales data is only available for fully completed hours.
    """
    return datetime.now(timezone.utc) - timedelta(minutes=SAFE_MINUTES_LAG)


def ingest_realtime_sales_report(
    report_json: dict,
    marketplace_id: str,
    currency_code: str
) -> dict:
    """
    Consume a GET_VENDOR_REAL_TIME_SALES_REPORT JSON and upsert into DB.
    
    Args:
        report_json: Full report JSON from SP-API
        marketplace_id: The marketplace ID for this report
        currency_code: Currency code (e.g. "AED")
    
    Returns:
        Summary dict: { "rows": int, "asins": int, "hours": int }
    """
    report_data = report_json.get("reportData", [])
    
    if not report_data:
        logger.info(f"{LOG_PREFIX_INGEST} Empty report data; returning empty summary")
        return {"rows": 0, "asins": 0, "hours": 0, "hour_starts": []}
    
    now_utc = datetime.now(timezone.utc).isoformat()
    
    rows_to_insert = []
    seen_asins = set()
    seen_hours = set()
    max_end_time_seen: Optional[datetime] = None
    
    for line in report_data:
        try:
            asin = line.get("asin")
            hour_start = line.get("startTime")
            hour_end = line.get("endTime")
            units = int(line.get("orderedUnits", 0))
            revenue = float(line.get("orderedRevenue", 0.0))
            
            if not asin or not hour_start or not hour_end:
                logger.warning(
                    f"{LOG_PREFIX_INGEST} Skipping line with missing asin/time: %s",
                    line,
                )
                continue
            
            rows_to_insert.append((
                asin,
                hour_start,
                hour_end,
                units,
                revenue,
                marketplace_id,
                currency_code,
                now_utc
            ))
            
            seen_asins.add(asin)
            seen_hours.add(hour_start)
            
            # Track the maximum endTime we see
            try:
                end_dt = datetime.fromisoformat(hour_end.replace("Z", "+00:00"))
                if max_end_time_seen is None or end_dt > max_end_time_seen:
                    max_end_time_seen = end_dt
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX_INGEST} Failed to parse endTime {hour_end}: {e}"
                )
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX_INGEST} Error processing line %s: %s",
                line,
                e,
            )
            continue
    
    if rows_to_insert:
        sql = """
        INSERT OR REPLACE INTO vendor_realtime_sales
        (asin, hour_start_utc, hour_end_utc, ordered_units, ordered_revenue,
         marketplace_id, currency_code, ingested_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            with time_block(f"vendor_rt_sales_upsert:{len(rows_to_insert)}"):
                execute_many_write(sql, rows_to_insert)
            
            # Update state to track furthest ingested hour
            if max_end_time_seen:
                update_last_ingested_end_utc(marketplace_id, max_end_time_seen)
            
            logger.info(
                f"{LOG_PREFIX_INGEST} Ingested %d rows, %d ASINs, %d hours",
                len(rows_to_insert),
                len(seen_asins),
                len(seen_hours)
            )
            seeded = seed_catalog_universe(seen_asins)
            if seeded:
                logger.info(f"[CatalogUniverse] seeded {seeded} asins from vendor_realtime_sales")
            record_catalog_asin_sources(seen_asins, "realtime_sales")
        except Exception as exc:
            logger.error(
                f"{LOG_PREFIX_INGEST} Failed to insert rows: {exc}",
                exc_info=True,
            )
            raise
    
    return {
        "rows": len(rows_to_insert),
        "asins": len(seen_asins),
        "hours": len(seen_hours),
        "hour_starts": list(seen_hours)
    }


def _normalize_utc_datetime(dt: datetime) -> datetime:
    """Ensure the datetime is timezone-aware UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    """Return a normalized ISO string (with trailing Z) for a UTC datetime."""
    normalized = _normalize_utc_datetime(dt)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_iso_to_utc(value: str) -> datetime:
    """Convert an ISO string (with optional Z) to a timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ====================================================================
# HOURLY LEDGER HELPERS
# ====================================================================
def _floor_to_hour(dt: datetime) -> datetime:
    normalized = _normalize_utc_datetime(dt)
    return normalized.replace(minute=0, second=0, microsecond=0)


def _ledger_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_vendor_rt_sales_hour_ledger_table() -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {LEDGER_TABLE_NAME} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        marketplace_id TEXT NOT NULL,
        hour_start_utc TEXT NOT NULL,
        status TEXT NOT NULL,
        report_id TEXT,
        requested_at TEXT,
        downloaded_at TEXT,
        applied_at TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        cooldown_until TEXT,
        UNIQUE(marketplace_id, hour_start_utc)
    )
    """
    execute_write(sql)
    execute_write(
        f"""
        CREATE INDEX IF NOT EXISTS idx_rt_sales_hour_status
        ON {LEDGER_TABLE_NAME} (marketplace_id, status, hour_start_utc)
        """
    )


def _ledger_insert_range(conn, marketplace_id: str, start_hour: datetime, end_hour: datetime) -> int:
    if end_hour <= start_hour:
        return 0
    inserted = 0
    current = _floor_to_hour(start_hour)
    end_hour = _floor_to_hour(end_hour)
    while current < end_hour:
        hour_iso = _utc_iso(current)
        cur = conn.execute(
            f"""
            INSERT INTO {LEDGER_TABLE_NAME} (marketplace_id, hour_start_utc, status, attempt_count)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(marketplace_id, hour_start_utc) DO NOTHING
            """,
            (marketplace_id, hour_iso, LEDGER_STATUS_MISSING),
        )
        if cur.rowcount:
            inserted += 1
        current += timedelta(hours=1)
    return inserted


def enqueue_vendor_rt_sales_hours(marketplace_id: str, start_utc: datetime, end_utc: datetime) -> int:
    """
    Insert ledger rows for the provided UTC range (hour granularity).
    """
    ensure_vendor_rt_sales_hour_ledger_table()
    normalized_start = _floor_to_hour(start_utc)
    normalized_end = _floor_to_hour(end_utc)
    if normalized_end <= normalized_start:
        return 0
    with get_db_connection() as conn:
        inserted = _ledger_insert_range(conn, marketplace_id, normalized_start, normalized_end)
        conn.commit()
    if inserted:
        logger.info(
            "[RtSalesLedger] plan marketplace=%s hours=%d window=[%s -> %s)",
            marketplace_id,
            inserted,
            _utc_iso(normalized_start),
            _utc_iso(normalized_end),
        )
    return inserted


def enqueue_vendor_rt_sales_specific_hours(marketplace_id: str, hour_starts_utc: List[datetime]) -> int:
    """
    Insert ledger rows for specific hour start timestamps.
    """
    ensure_vendor_rt_sales_hour_ledger_table()
    if not hour_starts_utc:
        return 0
    with get_db_connection() as conn:
        inserted = 0
        for dt in hour_starts_utc:
            cur = conn.execute(
                f"""
                INSERT INTO {LEDGER_TABLE_NAME} (marketplace_id, hour_start_utc, status, attempt_count)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(marketplace_id, hour_start_utc) DO NOTHING
                """,
                (marketplace_id, _utc_iso(dt), LEDGER_STATUS_MISSING),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    if inserted:
        logger.info(
            "[RtSalesLedger] plan marketplace=%s hours=%d (manual enqueue)",
            marketplace_id,
            inserted,
        )
    return inserted


def _ledger_seed_until(marketplace_id: str, target_end: datetime) -> None:
    """
    Ensure ledger rows exist up to target_end.
    """
    ensure_vendor_rt_sales_hour_ledger_table()
    normalized_end = _floor_to_hour(target_end)
    with get_db_connection() as conn:
        last_row = conn.execute(
            f"""
            SELECT hour_start_utc FROM {LEDGER_TABLE_NAME}
            WHERE marketplace_id = ?
            ORDER BY hour_start_utc DESC
            LIMIT 1
            """,
            (marketplace_id,),
        ).fetchone()
        if last_row and last_row["hour_start_utc"]:
            start_hour = _parse_iso_to_utc(last_row["hour_start_utc"]) + timedelta(hours=1)
        else:
            state_row = conn.execute(
                """
                SELECT last_ingested_end_utc FROM vendor_rt_sales_state
                WHERE marketplace_id = ?
                """,
                (marketplace_id,),
            ).fetchone()
            if state_row and state_row["last_ingested_end_utc"]:
                start_hour = _parse_iso_to_utc(state_row["last_ingested_end_utc"])
            else:
                start_hour = normalized_end - timedelta(hours=LEDGER_DEFAULT_BACKFILL_HOURS)
        inserted = _ledger_insert_range(conn, marketplace_id, start_hour, normalized_end)
        if inserted:
            logger.info(
                "[RtSalesLedger] plan marketplace=%s hours=%d seed-start=%s seed-end=%s",
                marketplace_id,
                inserted,
                _utc_iso(_floor_to_hour(start_hour)),
                _utc_iso(normalized_end),
            )
        conn.commit()


def _ledger_mark_requested(marketplace_id: str, hour_iso: str) -> bool:
    ensure_vendor_rt_sales_hour_ledger_table()
    now_iso = _utc_iso(_ledger_now())
    with get_db_connection() as conn:
        cur = conn.execute(
            f"""
            UPDATE {LEDGER_TABLE_NAME}
            SET status = ?, requested_at = ?, attempt_count = attempt_count + 1,
                last_error = NULL, cooldown_until = NULL, report_id = NULL
            WHERE marketplace_id = ?
              AND hour_start_utc = ?
              AND status IN (?, ?)
            """,
            (
                LEDGER_STATUS_REQUESTED,
                now_iso,
                marketplace_id,
                hour_iso,
                LEDGER_STATUS_MISSING,
                LEDGER_STATUS_FAILED,
            ),
        )
        conn.commit()
        return cur.rowcount == 1


def _ledger_set_report_id(marketplace_id: str, hour_iso: str, report_id: str) -> None:
    with get_db_connection() as conn:
        conn.execute(
            f"""
            UPDATE {LEDGER_TABLE_NAME}
            SET report_id = ?
            WHERE marketplace_id = ? AND hour_start_utc = ?
            """,
            (report_id, marketplace_id, hour_iso),
        )
        conn.commit()


def _ledger_mark_downloaded(marketplace_id: str, hour_iso: str) -> None:
    now_iso = _utc_iso(_ledger_now())
    with get_db_connection() as conn:
        conn.execute(
            f"""
            UPDATE {LEDGER_TABLE_NAME}
            SET status = ?, downloaded_at = ?
            WHERE marketplace_id = ? AND hour_start_utc = ?
            """,
            (LEDGER_STATUS_DOWNLOADED, now_iso, marketplace_id, hour_iso),
        )
        conn.commit()


def _ledger_mark_applied(marketplace_id: str, hour_iso: str) -> None:
    now_iso = _utc_iso(_ledger_now())
    with get_db_connection() as conn:
        conn.execute(
            f"""
            UPDATE {LEDGER_TABLE_NAME}
            SET status = ?, applied_at = ?, last_error = NULL, cooldown_until = NULL
            WHERE marketplace_id = ? AND hour_start_utc = ?
            """,
            (LEDGER_STATUS_APPLIED, now_iso, marketplace_id, hour_iso),
        )
        conn.commit()


def _ledger_mark_failed(
    marketplace_id: str,
    hour_iso: str,
    error_message: str,
    *,
    cooldown_minutes: int = LEDGER_COOLDOWN_MINUTES,
) -> None:
    cooldown_until = _ledger_now() + timedelta(minutes=cooldown_minutes)
    with get_db_connection() as conn:
        conn.execute(
            f"""
            UPDATE {LEDGER_TABLE_NAME}
            SET status = ?, last_error = ?, cooldown_until = ?
            WHERE marketplace_id = ? AND hour_start_utc = ?
            """,
            (
                LEDGER_STATUS_FAILED,
                (error_message or "")[:500],
                _utc_iso(cooldown_until),
                marketplace_id,
                hour_iso,
            ),
        )
        conn.commit()
    logger.warning(
        "[RtSalesLedger] fail hour=%s err=%s cooldown_until=%s",
        hour_iso,
        error_message,
        _utc_iso(cooldown_until),
    )


def _ledger_safe_cutoff(now_utc: Optional[datetime] = None) -> datetime:
    now_utc = now_utc or _ledger_now()
    return _normalize_utc_datetime(now_utc - timedelta(minutes=LEDGER_SAFETY_LAG_MINUTES))


def _ledger_plan_hours(
    marketplace_id: str,
    *,
    max_hours: int,
    now_utc: Optional[datetime] = None,
) -> List[str]:
    ensure_vendor_rt_sales_hour_ledger_table()
    now_utc = now_utc or _ledger_now()
    safe_cutoff = _ledger_safe_cutoff(now_utc)
    _ledger_seed_until(marketplace_id, safe_cutoff)
    cutoff_iso = _utc_iso(safe_cutoff)
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT hour_start_utc, status, cooldown_until
            FROM {LEDGER_TABLE_NAME}
            WHERE marketplace_id = ?
              AND hour_start_utc < ?
              AND status IN (?, ?)
            ORDER BY hour_start_utc ASC
            """,
            (
                marketplace_id,
                cutoff_iso,
                LEDGER_STATUS_MISSING,
                LEDGER_STATUS_FAILED,
            ),
        ).fetchall()
    planned: List[str] = []
    for row in rows:
        if len(planned) >= max_hours:
            break
        status = row["status"]
        cooldown_until = row["cooldown_until"]
        if status == LEDGER_STATUS_FAILED and cooldown_until:
            try:
                cooldown_dt = _parse_iso_to_utc(cooldown_until)
            except Exception:
                cooldown_dt = None
            if cooldown_dt and cooldown_dt > now_utc:
                logger.info(
                    "[RtSalesLedger] skip hour=%s status=%s cooldown_until=%s",
                    row["hour_start_utc"],
                    status,
                    cooldown_until,
                )
                continue
        planned.append(row["hour_start_utc"])
    return planned


def _execute_vendor_rt_sales_report(
    start_utc: datetime,
    end_utc: datetime,
    marketplace_id: str,
    *,
    currency_code: str = "AED",
    ledger_hour_iso: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_start = _normalize_utc_datetime(start_utc)
    normalized_end = _normalize_utc_datetime(end_utc)
    hour_label = ledger_hour_iso or f"{normalized_start.isoformat()}->{normalized_end.isoformat()}"

    try:
        _ensure_spapi_call_allowed(f"ledger_hour {hour_label}")
        logger.info(
            "%s Requesting RT report for [%s, %s) (%s)",
            LOG_PREFIX_API,
            normalized_start.isoformat(),
            normalized_end.isoformat(),
            marketplace_id,
        )
        report_id = request_vendor_report(
            report_type="GET_VENDOR_REAL_TIME_SALES_REPORT",
            data_start=normalized_start,
            data_end=normalized_end,
            extra_options={"currencyCode": currency_code},
        )
        if ledger_hour_iso:
            _ledger_set_report_id(marketplace_id, ledger_hour_iso, report_id)
            logger.info(
                "[RtSalesLedger] request hour=%s status=%s report_id=%s",
                ledger_hour_iso,
                LEDGER_STATUS_REQUESTED,
                report_id,
            )
        report_data = poll_vendor_report(report_id)
        document_id = report_data.get("reportDocumentId")
        if not document_id:
            raise RuntimeError(f"No reportDocumentId returned for RT report {report_id}")
        if ledger_hour_iso:
            _ledger_mark_downloaded(marketplace_id, ledger_hour_iso)
        content, _ = download_vendor_report_document(document_id)
        if isinstance(content, bytes):
            payload = json.loads(content.decode("utf-8"))
        elif isinstance(content, str):
            payload = json.loads(content)
        else:
            payload = content

        summary = ingest_realtime_sales_report(
            payload,
            marketplace_id=marketplace_id,
            currency_code=currency_code,
        )
        _record_audit_hours_for_window(
            normalized_start,
            normalized_end,
            marketplace_id,
            summary.get("hour_starts", []),
        )
        if ledger_hour_iso:
            _ledger_mark_applied(marketplace_id, ledger_hour_iso)
            logger.info(
                "[RtSalesLedger] apply hour=%s rows=%s",
                ledger_hour_iso,
                summary.get("rows", 0),
            )
        return summary
    except VendorRtCooldownBlock as exc:
        if ledger_hour_iso:
            _ledger_mark_failed(marketplace_id, ledger_hour_iso, str(exc))
        raise
    except SpApiQuotaError as exc:
        if ledger_hour_iso:
            _ledger_mark_failed(marketplace_id, ledger_hour_iso, str(exc))
        raise
    except Exception as exc:
        if ledger_hour_iso:
            _ledger_mark_failed(marketplace_id, ledger_hour_iso, str(exc))
        raise


def process_rt_sales_hour_ledger(
    marketplace_id: str,
    *,
    max_hours: Optional[int] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Process queued ledger hours by requesting SP-API reports sequentially.
    """
    if max_hours is None:
        max_hours = LEDGER_MAX_HOURS_PER_CYCLE
    now_utc = now_utc or _ledger_now()
    hours = _ledger_plan_hours(
        marketplace_id,
        max_hours=max_hours,
        now_utc=now_utc,
    )
    if not hours:
        return {"requested": 0, "applied": 0, "rows": 0, "hours": []}

    requested = 0
    applied = 0
    total_rows = 0
    total_asins = 0
    total_summary_hours = 0
    processed_hours: List[str] = []

    for hour_iso in hours:
        hour_start = _parse_iso_to_utc(hour_iso)
        if not _ledger_mark_requested(marketplace_id, hour_iso):
            logger.info(
                "[RtSalesLedger] skip hour=%s status changed before request",
                hour_iso,
            )
            continue
        try:
            summary = _execute_vendor_rt_sales_report(
                hour_start,
                hour_start + timedelta(hours=1),
                marketplace_id,
                ledger_hour_iso=hour_iso,
            )
            requested += 1
            applied += 1
            processed_hours.append(hour_iso)
            if summary:
                total_rows += summary.get("rows", 0)
                total_asins += summary.get("asins", 0)
                total_summary_hours += summary.get("hours", 0)
        except VendorRtCooldownBlock:
            break
        except SpApiQuotaError as exc:
            start_quota_cooldown(_ledger_now())
            logger.error(
                "[RtSalesLedger] quota stop hour=%s error=%s",
                hour_iso,
                exc,
            )
            raise
        except Exception as exc:
            logger.error(
                "[RtSalesLedger] fail hour=%s err=%s",
                hour_iso,
                exc,
            )
            continue

    return {
        "requested": requested,
        "applied": applied,
        "rows": total_rows,
        "asins": total_asins,
        "report_hours": total_summary_hours,
        "hours": processed_hours,
    }


def build_local_hour_window(date_str: str, hour: int) -> Tuple[datetime, datetime]:
    """Return the UTC bounds for a given UAE local date and hour."""
    if not (0 <= hour <= 23):
        raise ValueError("hour must be between 0 and 23")

    try:
        parsed = datetime.fromisoformat(date_str)
    except Exception as exc:
        raise ValueError(f"Invalid date format: {date_str}") from exc

    local_date = parsed.date()
    local_start = datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        hour,
        tzinfo=UAE_TZ
    )
    local_end = local_start + timedelta(hours=1)

    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)

# Requests a single SP-API RT report and ingests it; used by fill-day repairs and manual/maintenance helpers.
def request_vendor_rt_report(
    start_utc: datetime,
    end_utc: datetime,
    marketplace_id: str,
    currency_code: str = "AED"
) -> Dict[str, Any]:
    """
    Request a GET_VENDOR_REAL_TIME_SALES_REPORT for the provided window and ingest it.
    """
    result = _execute_vendor_rt_sales_report(
        start_utc,
        end_utc,
        marketplace_id,
        currency_code=currency_code,
    )
    return {
        "report_id": None,
        "start_utc": _utc_iso(_normalize_utc_datetime(start_utc)),
        "end_utc": _utc_iso(_normalize_utc_datetime(end_utc)),
        "marketplace_id": marketplace_id,
        "ingest_summary": result,
    }


def _record_audit_hours_for_window(
    start_utc: datetime,
    end_utc: datetime,
    marketplace_id: str,
    seen_hour_starts: Optional[List[str]] = None,
) -> None:
    """
    Ensure every hour in [start_utc, end_utc) is recorded in the audit table.
    Hours with report rows are marked as OK; the rest as EMPTY.
    """
    normalized_start = _normalize_utc_datetime(start_utc)
    normalized_end = _normalize_utc_datetime(end_utc)

    seen_keys: set[str] = set()
    for hour_start in seen_hour_starts or []:
        try:
            dt = datetime.fromisoformat(hour_start.replace("Z", "+00:00"))
            seen_keys.add(_utc_iso(dt))
        except Exception:
            continue

    if not seen_keys:
        logger.info(
            f"{LOG_PREFIX_AUDIT} Empty RT data for window [%s, %s); marking hours as EMPTY",
            _utc_iso(normalized_start),
            _utc_iso(normalized_end),
        )

    current = normalized_start
    while current < normalized_end:
        next_hour = min(current + timedelta(hours=1), normalized_end)
        hour_key = _utc_iso(current)
        status = "OK" if hour_key in seen_keys else "EMPTY"
        _upsert_vendor_rt_audit_hour(marketplace_id, current, next_hour, status)
        current = next_hour


def _classify_daily_hours(
    date_str: str,
    marketplace_id: str,
    latest_allowed_end: Optional[datetime] = None,
) -> Tuple[List[dict], List[int], List[int]]:
    # Audit-day classifier that derives hourly statuses purely from coverage metadata (no SP-API).
    """
    Return hour-by-hour audit status for a UAE date.
    The hours_detail output is safe for UI display and drives the audit-day response.
    """
    try:
        parsed_date = datetime.fromisoformat(date_str).date()
    except Exception as exc:
        raise ValueError(f"Invalid date format: {date_str}") from exc

    day_start_local = datetime(
        parsed_date.year,
        parsed_date.month,
        parsed_date.day,
        tzinfo=UAE_TZ
    )
    day_end_local = day_start_local + timedelta(days=1)
    start_utc = day_start_local.astimezone(timezone.utc)
    end_utc = day_end_local.astimezone(timezone.utc)
    safe_window = latest_allowed_end or get_safe_now_utc()

    hours_detail: List[dict] = []
    missing_hours: List[int] = []
    pending_hours: List[int] = []

    with get_db_connection() as conn:
        coverage_map = _build_hourly_coverage_map(
            conn,
            marketplace_id,
            start_utc,
            end_utc,
            safe_now=safe_window,
        )

    for hour in range(24):
        hour_start, hour_end = build_local_hour_window(date_str, hour)
        hour_key = _utc_iso(hour_start)
        coverage_row = coverage_map.get(hour_key)
        audited_status = None
        if coverage_row:
            audited_status = coverage_row.get("audited_status")
            coverage_status = coverage_row["status"]
            if coverage_status in {"OK", "OK_INFERRED"}:
                status_label = "ok"
            elif coverage_status == "EMPTY":
                status_label = "empty"
            elif coverage_status == "FUTURE_BLOCKED":
                status_label = "pending"
                pending_hours.append(hour)
            else:
                status_label = "missing"
                missing_hours.append(hour)
        else:
            status_label = "missing"
            missing_hours.append(hour)

        hours_detail.append({
            "hour": hour,
            "status": status_label,
            "start_utc": hour_key,
            "end_utc": _utc_iso(hour_end),
            "audited_status": audited_status,
        })

    return hours_detail, missing_hours, pending_hours


# Called by the /api/vendor-realtime-sales/fill-day endpoint to determine which hours can be repaired in a quota-safe way.
def plan_fill_day_run(
    date_str: str,
    requested_hours: Optional[List[int]],
    marketplace_id: str,
    max_reports: int = MAX_HOURLY_REPORTS_PER_FILL_DAY,
) -> dict:
    """
    Determine which missing hours can be repaired in this fill-day run.
    """
    safe_now = get_safe_now_utc()
    hours_detail, missing_hours, pending_hours = _classify_daily_hours(
        date_str,
        marketplace_id,
        latest_allowed_end=safe_now,
    )

    total_missing = len(missing_hours)
    missing_candidates = [info for info in hours_detail if info["status"] == "missing"]

    if requested_hours:
        requested_set = set(requested_hours)
        missing_candidates = [
            info for info in missing_candidates if info["hour"] in requested_set
        ]

    missing_candidates.sort(key=lambda item: item["hour"])
    cooldown_active = is_in_quota_cooldown(datetime.now(timezone.utc))
    cooldown_until = _format_cooldown_until()

    if cooldown_active:
        logger.warning(
            f"{LOG_PREFIX_FILL_DAY} Quota cooldown active until {cooldown_until}; skipping SP-API calls"
        )

    hours_to_request = (
        missing_candidates[:max_reports] if not cooldown_active else missing_candidates[:0]
    )

    if not cooldown_active and len(missing_candidates) > max_reports:
        logger.info(
            f"{LOG_PREFIX_FILL_DAY} Capping repair run to %d hours for %s (of %d missing)",
            max_reports,
            date_str,
            len(missing_candidates),
        )

    remaining_missing = max(0, total_missing - len(hours_to_request))

    return {
        "date": date_str,
        "total_missing": total_missing,
        "remaining_missing": remaining_missing,
        "pending_hours": pending_hours,
        "hours_to_request": hours_to_request,
        "cooldown_active": cooldown_active,
        "cooldown_until": cooldown_until,
        "cooldown_remaining_seconds": cooldown_remaining_seconds(),
    }


# Executes the hourly repair tasks planned by plan_fill_day_run; respects cooldowns and is called via BackgroundTasks.
def run_fill_day_repair_cycle(
    date_str: str,
    hours_to_request: List[dict],
    marketplace_id: str,
    total_missing: int,
) -> None:
    """
    Sequentially request and ingest the missing hours returned by plan_fill_day_run.
    """
    if not hours_to_request:
        logger.info(f"{LOG_PREFIX_FILL_DAY} No hours to repair for %s", date_str)
        return

    hour_starts: List[datetime] = []
    for entry in hours_to_request:
        try:
            hour_starts.append(_parse_iso_to_utc(entry["start_utc"]))
        except Exception:
            continue

    if not hour_starts:
        logger.info(f"{LOG_PREFIX_FILL_DAY} No valid hours to enqueue for %s", date_str)
        return

    enqueue_vendor_rt_sales_specific_hours(marketplace_id, hour_starts)
    try:
        summary = process_rt_sales_hour_ledger(
            marketplace_id,
            max_hours=len(hour_starts),
        )
    except VendorRtCooldownBlock as exc:
        logger.warning(
            f"{LOG_PREFIX_FILL_DAY} Cooldown active during Fill Day for %s: %s",
            date_str,
            exc,
        )
        return
    except SpApiQuotaError as exc:
        logger.warning(
            f"{LOG_PREFIX_FILL_DAY} Quota exceeded during Fill Day for %s: %s",
            date_str,
            exc,
        )
        start_quota_cooldown(datetime.now(timezone.utc))
        return
    logger.info(
        f"{LOG_PREFIX_FILL_DAY} Fill Day run %s -> requested=%d applied=%d remaining_missing=%d",
        date_str,
        summary.get("requested", 0),
        summary.get("applied", 0),
        max(0, total_missing - summary.get("applied", 0)),
    )
def repair_missing_hour(
    start_utc: datetime,
    end_utc: datetime,
    marketplace_id: str,
    currency_code: str = "AED"
) -> Dict[str, Any]:
    """
    Helper that wraps `request_vendor_rt_report` so it can be scheduled in BackgroundTasks.
    """
    try:
        return request_vendor_rt_report(start_utc, end_utc, marketplace_id, currency_code)
    except SpApiQuotaError as exc:
        logger.warning(
            f"{LOG_PREFIX_FILL_DAY} Quota hit while repairing [%s, %s): %s",
            start_utc.isoformat(),
            end_utc.isoformat(),
            exc,
        )
        return {"status": "quota_error", "error": str(exc)}
    except Exception as exc:
        logger.error(
            f"{LOG_PREFIX_FILL_DAY} Failed to repair [%s, %s): %s",
            start_utc.isoformat(),
            end_utc.isoformat(),
            exc,
            exc_info=True,
        )
        return {"status": "error", "error": str(exc)}


def utc_to_uae_str(dt_utc: datetime) -> str:
    """
    Convert UTC datetime to UAE timezone and return ISO format string.
    
    Args:
        dt_utc: timezone-aware UTC datetime
    
    Returns:
        ISO format string of the time in UAE (Asia/Dubai) timezone
    """
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    
    dt_uae = dt_utc.astimezone(UAE_TZ)
    return dt_uae.isoformat()


def _get_last_ingested_hour(conn, marketplace_id: Optional[str]) -> Optional[str]:
    """
    Return the most recent hour_start_utc visible in vendor_realtime_sales.
    """
    sql = "SELECT MAX(hour_start_utc) AS last_hour FROM vendor_realtime_sales"
    params: List[str] = []
    if marketplace_id:
        sql += " WHERE marketplace_id = ?"
        params.append(marketplace_id)
    row = conn.execute(sql, params).fetchone()
    return row["last_hour"] if row and row["last_hour"] else None


def _get_last_audited_hour(conn, marketplace_id: Optional[str]) -> Optional[str]:
    """
    Return the most recent audited hour marked OK or EMPTY.
    """
    sql = """
    SELECT MAX(hour_start_utc) AS last_hour
    FROM vendor_rt_audit_hours
    WHERE status IN ('OK', 'EMPTY')
    """
    params: List[str] = []
    if marketplace_id:
        sql += " AND marketplace_id = ?"
        params.append(marketplace_id)
    row = conn.execute(sql, params).fetchone()
    return row["last_hour"] if row and row["last_hour"] else None


def get_realtime_sales_summary(
    start_utc: str,
    end_utc: str,
    marketplace_id: Optional[str] = None,
    view_by: str = "asin"
) -> dict:
    """
    Aggregate real-time sales data for a window.
    
    Args:
        start_utc: ISO8601 string (UTC) - start of window
        end_utc: ISO8601 string (UTC) - end of window
        marketplace_id: Optional marketplace filter
        view_by: "asin" (default) or "time" for different aggregation
    
    Returns for view_by="asin":
    {
        "lookback_hours": int,
        "view_by": "asin",
        "window": {
            "start_utc": "...",
            "end_utc": "...",
            "start_uae": "...",
            "end_uae": "..."
        },
        "total_units": int,
        "total_revenue": float,
        "currency_code": str,
        "rows": [
            {
                "asin": "...",
                "units": int,
                "revenue": float,
                "imageUrl": "...",
                "first_hour_utc": "...",
                "last_hour_utc": "..."
            },
            ...
        ]
    }
    
    Returns for view_by="time":
    {
        "lookback_hours": int,
        "view_by": "time",
        "window": { ... },
        "total_units": int,
        "total_revenue": float,
        "currency_code": str,
        "rows": [
            {
                "bucket_start_utc": "...",
                "bucket_end_utc": "...",
                "bucket_start_uae": "...",
                "bucket_end_uae": "...",
                "units": int,
                "revenue": float
            },
            ...
        ]
    }
    """
    try:
        requested_start_dt = _normalize_utc_datetime(_parse_iso_to_utc(start_utc))
        requested_end_dt = _normalize_utc_datetime(_parse_iso_to_utc(end_utc))
        duration_secs = max(0, (requested_end_dt - requested_start_dt).total_seconds())
        requested_hours = max(1, int(round(duration_secs / 3600)))
        lookback_hours = requested_hours

        total_units = 0
        total_revenue = 0.0
        currency_code = "AED"
        rows: List[dict] = []
        coverage_summary = {
            "total_hours": 0,
            "ok_hours": 0,
            "missing_hours": 0,
            "pending_hours": 0,
            "coverage_percent": 0.0,
        }

        window_start_dt: Optional[datetime] = None
        window_end_dt: Optional[datetime] = None
        window_start_iso: Optional[str] = None
        window_end_iso: Optional[str] = None

        now_utc = datetime.now(timezone.utc)
        with get_db_connection() as conn:
            last_ingested_iso = _get_last_ingested_hour(conn, marketplace_id)
            last_audited_iso = _get_last_audited_hour(conn, marketplace_id)

            def _safe_parse(value: Optional[str]) -> Optional[datetime]:
                if not value:
                    return None
                try:
                    return _normalize_utc_datetime(_parse_iso_to_utc(value))
                except Exception as exc:
                    logger.warning(
                        f"{LOG_PREFIX_SUMMARY} Failed to parse audit boundary [{value}]: {exc}"
                    )
                    return None

            candidates = [
                _normalize_utc_datetime(now_utc),
                requested_end_dt,
            ]
            for candidate in (_safe_parse(last_ingested_iso), _safe_parse(last_audited_iso)):
                if candidate:
                    candidates.append(candidate)

            candidates = [candidate for candidate in candidates if candidate]
            window_end_dt = min(candidates) if candidates else requested_end_dt
            window_end_dt = _normalize_utc_datetime(window_end_dt)
            window_start_dt = _normalize_utc_datetime(
                window_end_dt - timedelta(hours=requested_hours) - timedelta(minutes=SAFETY_LOOKBACK_MINUTES)
            )
            window_start_iso = _utc_iso(window_start_dt)
            window_end_iso = _utc_iso(window_end_dt)

            coverage_map = _build_hourly_coverage_map(
                conn,
                marketplace_id,
                window_start_dt,
                window_end_dt,
            )

            ok_hours = sum(
                1
                for info in coverage_map.values()
                if (info.get("status") or "").upper() in {"OK", "EMPTY", "OK_INFERRED"}
            )
            missing_hours = sum(
                1
                for info in coverage_map.values()
                if (info.get("status") or "").upper() == "MISSING"
            )
            pending_hours = sum(
                1
                for info in coverage_map.values()
                if (info.get("status") or "").upper() in {"FUTURE_BLOCKED", "PENDING"}
            )
            total_hours = len(coverage_map)
            coverage_percent = (ok_hours / total_hours) * 100 if total_hours else 0.0
            coverage_summary = {
                "total_hours": total_hours,
                "ok_hours": ok_hours,
                "missing_hours": missing_hours,
                "pending_hours": pending_hours,
                "coverage_percent": round(coverage_percent, 2),
            }

            logger.info(
                f"{LOG_PREFIX_SUMMARY} Read-only summary window {window_start_iso} -> {window_end_iso} (UTC); "
                f"coverage {coverage_summary['coverage_percent']}% | OK {ok_hours}/{total_hours} | "
                f"missing {missing_hours} | pending {pending_hours}"
            )

            totals_query = """
            SELECT
                SUM(ordered_units) AS total_units,
                SUM(ordered_revenue) AS total_revenue,
                MAX(currency_code) AS currency_code
            FROM vendor_realtime_sales
            WHERE hour_start_utc >= ? AND hour_start_utc < ?
            """
            totals_params = [window_start_iso, window_end_iso]
            if marketplace_id:
                totals_query += " AND marketplace_id = ?"
                totals_params.append(marketplace_id)

            totals_row = conn.execute(totals_query, totals_params).fetchone()
            if totals_row:
                total_units = totals_row["total_units"] or 0
                total_revenue = totals_row["total_revenue"] or 0.0
                currency_code = totals_row["currency_code"] or "AED"

            if view_by == "time":
                rows = _get_realtime_sales_by_time(
                    conn,
                    window_start_iso,
                    window_end_iso,
                    marketplace_id,
                )
            else:
                rows = _get_realtime_sales_by_asin(
                    conn,
                    window_start_iso,
                    window_end_iso,
                    marketplace_id,
                )

        final_start_dt = window_start_dt or requested_start_dt
        final_end_dt = window_end_dt or requested_end_dt
        final_start_iso = window_start_iso or _utc_iso(final_start_dt)
        final_end_iso = window_end_iso or _utc_iso(final_end_dt)
        window_data = {
            "start_utc": final_start_iso,
            "end_utc": final_end_iso,
            "start_uae": utc_to_uae_str(final_start_dt),
            "end_uae": utc_to_uae_str(final_end_dt),
        }

        return {
            "lookback_hours": lookback_hours,
            "view_by": view_by,
            "window": window_data,
            "total_units": total_units,
            "total_revenue": round(float(total_revenue), 2),
            "currency_code": currency_code,
            "rows": rows,
            "coverage": coverage_summary,
        }
    except Exception as exc:
        logger.error(
            f"{LOG_PREFIX_SUMMARY} Failed to get summary [{start_utc}, {end_utc}): {exc}",
            exc_info=True
        )
        raise


def _get_realtime_sales_by_asin(
    conn,
    start_utc: str,
    end_utc: str,
    marketplace_id: Optional[str] = None,
) -> List[dict]:
    """
    Aggregate real-time sales by ASIN for a time window.
    
    Returns list of dicts with: asin, units, revenue, imageUrl, first_hour_utc, last_hour_utc
    """
    query = """
    SELECT
        vrs.asin,
        SUM(vrs.ordered_units) as units,
        SUM(vrs.ordered_revenue) as revenue,
        MIN(vrs.hour_start_utc) as first_hour_utc,
        MAX(vrs.hour_start_utc) as last_hour_utc,
        sc.image AS image_url
    FROM vendor_realtime_sales vrs
    LEFT JOIN spapi_catalog sc ON vrs.asin = sc.asin
    WHERE vrs.hour_start_utc >= ? AND vrs.hour_start_utc < ?
    """
    params = [start_utc, end_utc]
    if marketplace_id:
        query += " AND vrs.marketplace_id = ?"
        params.append(marketplace_id)
    
    query += """
    GROUP BY vrs.asin
    ORDER BY units DESC
    LIMIT 50
    """
    
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "asin": row["asin"],
            "units": row["units"] or 0,
            "revenue": round(float(row["revenue"] or 0.0), 2),
            "imageUrl": row["image_url"],
            "first_hour_utc": row["first_hour_utc"],
            "last_hour_utc": row["last_hour_utc"]
        }
        for row in rows
    ]


def _get_realtime_sales_by_time(
    conn,
    start_utc: str,
    end_utc: str,
    marketplace_id: Optional[str] = None,
) -> List[dict]:
    """
    Aggregate real-time sales by hourly time buckets for a time window.
    
    Returns list of dicts with: bucket_start_utc, bucket_end_utc, bucket_start_uae, 
                                bucket_end_uae, units, revenue
    """
    query = """
    SELECT
        vrs.hour_start_utc as bucket_start_utc,
        vrs.hour_end_utc as bucket_end_utc,
        SUM(vrs.ordered_units) as units,
        SUM(vrs.ordered_revenue) as revenue
    FROM vendor_realtime_sales vrs
    WHERE vrs.hour_start_utc >= ? AND vrs.hour_start_utc < ?
    """
    params = [start_utc, end_utc]
    if marketplace_id:
        query += " AND vrs.marketplace_id = ?"
        params.append(marketplace_id)
    
    query += """
    GROUP BY vrs.hour_start_utc, vrs.hour_end_utc
    ORDER BY vrs.hour_start_utc ASC
    """
    
    rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        bucket_start_dt = datetime.fromisoformat(
            row["bucket_start_utc"].replace("Z", "+00:00")
        )
        bucket_end_dt = datetime.fromisoformat(
            row["bucket_end_utc"].replace("Z", "+00:00")
        )
        
        result.append({
            "bucket_start_utc": row["bucket_start_utc"],
            "bucket_end_utc": row["bucket_end_utc"],
            "bucket_start_uae": utc_to_uae_str(bucket_start_dt),
            "bucket_end_uae": utc_to_uae_str(bucket_end_dt),
            "units": row["units"] or 0,
            "revenue": round(float(row["revenue"] or 0.0), 2)
        })
    
    return result


def get_realtime_sales_for_asin(
    asin: str,
    start_utc: str,
    end_utc: str,
    marketplace_id: Optional[str] = None,
) -> List[dict]:
    """
    Get hourly sales data for a specific ASIN.
    
    Returns:
    [
        {
            "hour_start_utc": "...",
            "hour_end_utc": "...",
            "ordered_units": int,
            "ordered_revenue": float
        },
        ...
    ]
    """
    try:
        with get_db_connection() as conn:
            query = """
            SELECT
                hour_start_utc,
                hour_end_utc,
                ordered_units,
                ordered_revenue
            FROM vendor_realtime_sales
            WHERE asin = ? AND hour_start_utc >= ? AND hour_start_utc < ?
            """
            params = [asin, start_utc, end_utc]
            if marketplace_id:
                query += " AND marketplace_id = ?"
                params.append(marketplace_id)
            
            query += " ORDER BY hour_start_utc ASC"
            
            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "hour_start_utc": row["hour_start_utc"],
                    "hour_end_utc": row["hour_end_utc"],
                    "ordered_units": row["ordered_units"],
                    "ordered_revenue": round(float(row["ordered_revenue"]), 2)
                }
                for row in rows
            ]
    except Exception as exc:
        logger.error(
            f"{LOG_PREFIX_SUMMARY} Failed to get ASIN detail for {asin} [{start_utc}, {end_utc}): {exc}",
            exc_info=True
        )
        raise


def clear_realtime_sales(before_utc: Optional[str] = None) -> int:
    """
    Delete old records (optional cleanup).
    
    If before_utc is provided, deletes all records older than that timestamp.
    Otherwise does nothing.
    
    Returns:
        Number of rows deleted.
    """
    if not before_utc:
        return 0
    
    try:
        with get_db_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM vendor_realtime_sales WHERE hour_start_utc < ?",
                (before_utc,)
            )
            conn.commit()
            deleted = cursor.rowcount
            logger.info(f"{LOG_PREFIX_INGEST} Deleted {deleted} old records")
            return deleted
    except Exception as exc:
        logger.error(f"{LOG_PREFIX_INGEST} Failed to clear old data: {exc}", exc_info=True)
        raise

# ====================================================================
# BACKFILL LOGIC FOR GAP DETECTION
# ====================================================================
# Called by startup sync, auto-sync loop, and audit runners to request chunked RT reports when gaps are detected.
def backfill_realtime_sales_for_gap(
    spapi_client: Any,
    marketplace_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> Tuple[int, int, int]:
    """
    Backfill vendor_realtime_sales data for [start_utc, end_utc) in CHUNK_HOURS increments.
    Uses GET_VENDOR_REAL_TIME_SALES_REPORT from SP-API.
    
    On SpApiQuotaError:
    - Logs the error with the chunk details
    - Immediately stops processing further chunks (hard-stop)
    - Re-raises SpApiQuotaError so caller can handle quota cooldown
    
    For other exceptions:
    - Logs and continues to next chunk (does not corrupt state)
    
    Returns:
        (total_rows_ingested, total_asins, total_hours_processed)
    
    Raises:
        SpApiQuotaError: If quota is exceeded (caller should activate cooldown)
    """
    safe_now = get_safe_now_utc()
    if start_utc >= safe_now:
        logger.debug(
            f"{LOG_PREFIX_API} Start time already >= safe_now; nothing to backfill"
        )
        return (0, 0, 0)

    end_utc_clamped = min(end_utc, safe_now)
    if end_utc_clamped <= start_utc:
        logger.debug(
            f"{LOG_PREFIX_API} Clamped end <= start; nothing to backfill"
        )
        return (0, 0, 0)

    enqueue_vendor_rt_sales_hours(marketplace_id, start_utc, end_utc_clamped)
    summary = process_rt_sales_hour_ledger(marketplace_id)
    return (
        summary.get("rows", 0),
        summary.get("asins", 0),
        summary.get("report_hours", 0),
    )


def run_realtime_sales_audit_window(
    spapi_client: Any,
    start_utc: datetime,
    end_utc: datetime,
    marketplace_id: str,
    label: str
) -> Tuple[int, int, int]:
    """
    Run an audit window for real-time sales (daily/weekly).
    Wrapper around backfill_realtime_sales_for_gap with special logging.
    
    Propagates SpApiQuotaError to allow caller to activate cooldown.
    
    Args:
        spapi_client: SP-API client
        start_utc: Start of audit window (should already be clamped to safe_now)
        end_utc: End of audit window (should already be clamped to safe_now)
        marketplace_id: The marketplace ID
        label: 'daily' or 'weekly' for logging
    
    Returns:
        (rows_ingested, unique_asins, unique_hours)
    
    Raises:
        SpApiQuotaError: Propagated from backfill if quota exceeded
    """
    logger.info(
        f"{LOG_PREFIX_AUDIT} Starting %s audit for [%s, %s)",
        label,
        start_utc.isoformat(),
        end_utc.isoformat()
    )
    
    try:
        rows, asins, hours = backfill_realtime_sales_for_gap(
            spapi_client,
            marketplace_id,
            start_utc,
            end_utc
        )
        
        logger.info(
            f"{LOG_PREFIX_AUDIT} %s audit complete: %d rows, %d ASINs, %d hours",
            label.capitalize(),
            rows,
            asins,
            hours
        )
        
        return (rows, asins, hours)
    except Exception as e:
        # Re-raise quota errors; log and suppress others
        from services.spapi_reports import SpApiQuotaError
        if isinstance(e, VendorRtCooldownBlock):
            logger.warning(
                f"{LOG_PREFIX_COOLDOWN} {label.capitalize()} audit blocked; cooldown active until {e.cooldown_until}"
            )
            raise
        elif isinstance(e, SpApiQuotaError):
            logger.error(
                f"{LOG_PREFIX_AUDIT} %s audit hit quota: %s",
                label.capitalize(),
                e
            )
            raise  # Propagate quota error to caller
        else:
            logger.error(
                f"{LOG_PREFIX_AUDIT} %s audit failed: %s",
                label.capitalize(),
                e,
                exc_info=True
            )
            return (0, 0, 0)  # Suppress other errors


# ====================================================================
# SALES TRENDS QUERY (4-WEEK ROLLING WINDOW)
# ====================================================================

# Helper used by get_sales_trends_last_4_weeks to determine the aligned UAE week ranges for W4-W1.
def _compute_sales_trend_week_buckets_uae(now_uae: datetime, weeks: int = 4) -> Dict[str, Dict[str, datetime]]:
    """Compute the recent four full UAE weeks used by Sales Trends."""
    if weeks < 1:
        return {}

    today_uae = now_uae.date()
    current_week_monday = today_uae - timedelta(days=today_uae.weekday())
    last_completed_w1_start = datetime.combine(current_week_monday - timedelta(days=7), time(0, 0, 0), tzinfo=UAE_TZ)

    buckets = {}
    week_keys = ["w4", "w3", "w2", "w1"]
    for index, week_key in enumerate(week_keys):
        offset_weeks = 3 - index
        start_uae = last_completed_w1_start - timedelta(days=offset_weeks * 7)
        end_uae = start_uae + timedelta(days=7) - timedelta(seconds=1)
        buckets[week_key] = {
            "start_uae": start_uae,
            "end_uae": end_uae,
        }
    return buckets

def _parse_hour_start_to_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO8601 UTC timestamp from vendor_realtime_sales."""
    if not value:
        return None
    try:
        normalized = value.replace('Z', '+00:00')
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as exc:
        logger.warning(
            "[SalesTrends] Failed to parse hour_start_utc '%s': %s",
            value,
            exc
        )
        return None

 # Read-only helper powering /api/vendor-sales-trends; it only reads RT rows and audit coverage, never SP-API.
def get_sales_trends_last_4_weeks(
    conn,
    marketplace_id: str,
    min_total_units: int = 1,
) -> dict:
    """Compute 4-week sales trends per ASIN using vendor_realtime_sales."""
    # Trends computation is resilient to missing hourly data; audit handles repairs.
    try:
        safe_now_utc = get_safe_now_utc()
        safe_now_uae = safe_now_utc.astimezone(UAE_TZ)
        cooldown_active = is_in_quota_cooldown(datetime.now(timezone.utc))
        if cooldown_active:
            logger.info(
                f"{LOG_PREFIX_TRENDS} RT cooldown active; trends will only read cached data."
            )
        bucket_defs = _compute_sales_trend_week_buckets_uae(safe_now_uae, weeks=4)

        buckets = {}
        for week_key in ["w4", "w3", "w2", "w1"]:
            week_def = bucket_defs[week_key]
            start_uae = week_def["start_uae"]
            end_uae = week_def["end_uae"]
            buckets[week_key] = {
                "label": week_key.upper(),
                "start_uae": start_uae,
                "end_uae": end_uae,
                "start_utc": start_uae.astimezone(timezone.utc),
                "end_utc": end_uae.astimezone(timezone.utc),
            }

        logger.info(
            f"{LOG_PREFIX_TRENDS} Buckets (UAE): W4=%s-%s, W3=%s-%s, W2=%s-%s, W1=%s-%s",
            buckets["w4"]["start_uae"].isoformat(), buckets["w4"]["end_uae"].isoformat(),
            buckets["w3"]["start_uae"].isoformat(), buckets["w3"]["end_uae"].isoformat(),
            buckets["w2"]["start_uae"].isoformat(), buckets["w2"]["end_uae"].isoformat(),
            buckets["w1"]["start_uae"].isoformat(), buckets["w1"]["end_uae"].isoformat(),
        )

        window_start_utc = buckets["w4"]["start_utc"]
        window_end_utc = buckets["w1"]["end_utc"]
        window_end_exclusive = window_end_utc + timedelta(seconds=1)

        query = """
        SELECT
            asin,
            hour_start_utc,
            ordered_units
        FROM vendor_realtime_sales
        WHERE
            marketplace_id = ?
            AND hour_start_utc >= ?
            AND hour_start_utc < ?
        ORDER BY asin, hour_start_utc ASC
        """

        rows = conn.execute(
            query,
            (
                marketplace_id,
                window_start_utc.isoformat(),
                window_end_exclusive.isoformat(),
            )
        ).fetchall()
        if not rows:
            logger.info(
                f"{LOG_PREFIX_TRENDS} No RT sales rows found for window "
                f"{window_start_utc.isoformat()} -> {window_end_exclusive.isoformat()}"
            )

        coverage_map = _build_hourly_coverage_map(
            conn,
            marketplace_id,
            window_start_utc,
            window_end_exclusive,
            safe_now=safe_now_utc,
        )

        total_hours = len(coverage_map)
        coverage_statuses = {"OK", "OK_INFERRED", "EMPTY"}
        covered_hours = sum(
            1 for info in coverage_map.values() if info["status"] in coverage_statuses
        )
        missing_hours = sum(
            1 for info in coverage_map.values() if info["status"] == "MISSING"
        )
        pending_hours = sum(
            1 for info in coverage_map.values() if info["status"] == "FUTURE_BLOCKED"
        )
        coverage_percent = (
            round((covered_hours / total_hours) * 100, 1) if total_hours else 0.0
        )
        # Coverage summary derived from the coverage_map statuses (OK, OK_INFERRED, EMPTY, etc.).
        coverage_summary = {
            "total_hours": total_hours,
            "covered_hours": covered_hours,
            "missing_hours": missing_hours,
            "pending_hours": pending_hours,
            "coverage_percent": coverage_percent,
        }

        weekly_stats: Dict[str, Dict[str, int]] = {}
        for row in rows:
            asin = row["asin"]
            dt = _parse_hour_start_to_utc(row["hour_start_utc"] or "")
            if not dt:
                continue

            try:
                units = int(row["ordered_units"] or 0)
            except Exception:
                try:
                    units = int(Decimal(row["ordered_units"] or 0))
                except Exception:
                    units = 0

            bucket_key = None
            for week_key in ["w4", "w3", "w2", "w1"]:
                bounds = buckets[week_key]
                if bounds["start_utc"] <= dt <= bounds["end_utc"]:
                    bucket_key = week_key
                    break
            if not bucket_key:
                continue

            if asin not in weekly_stats:
                weekly_stats[asin] = {k: 0 for k in ["w4", "w3", "w2", "w1"]}
            weekly_stats[asin][bucket_key] += units

        trend_rows = []
        for asin, week_counts in weekly_stats.items():
            w4_u = week_counts["w4"]
            w3_u = week_counts["w3"]
            w2_u = week_counts["w2"]
            w1_u = week_counts["w1"]
            total_4w = w4_u + w3_u + w2_u + w1_u

            if total_4w < min_total_units:
                continue

            delta_units: int = w1_u - w2_u
            pct_change: Optional[float] = None
            trend = "flat"

            if w1_u == 0 and w2_u == 0:
                delta_units = 0
                pct_change = 0.0
                trend = "flat"
            elif w2_u == 0 and w1_u > 0:
                delta_units = w1_u
                pct_change = None
                trend = "new"
            elif w1_u == 0 and w2_u > 0:
                delta_units = -w2_u
                pct_change = None
                trend = "dead"
            else:
                baseline = float(w2_u)
                change = float(w1_u - w2_u)
                pct_change = change / baseline if baseline != 0 else 0.0
                if pct_change >= 0.25:
                    trend = "rising"
                elif pct_change <= -0.25:
                    trend = "falling"
                else:
                    trend = "flat"

            trend_rows.append({
                "asin": asin,
                "title": asin,
                "imageUrl": None,
                "w4_units": w4_u,
                "w3_units": w3_u,
                "w2_units": w2_u,
                "w1_units": w1_u,
                "this_week_units": 0,
                "total_units_4w": total_4w,
                "delta_units": delta_units,
                "pct_change": pct_change,
                "trend": trend,
            })

        if trend_rows:
            asins = [row["asin"] for row in trend_rows]
            placeholders = ",".join(["?" for _ in asins])
            catalog_query = f"""
            SELECT asin, title, image AS imageUrl
            FROM spapi_catalog
            WHERE asin IN ({placeholders})
            """
            catalog_rows = conn.execute(catalog_query, asins).fetchall()
            catalog_map = {row["asin"]: dict(row) for row in catalog_rows}
            for row in trend_rows:
                catalog_entry = catalog_map.get(row["asin"])
                if not catalog_entry:
                    continue
                row["title"] = catalog_entry.get("title", row.get("title"))
                row["imageUrl"] = catalog_entry.get("imageUrl", row.get("imageUrl"))

        trend_rows.sort(key=lambda r: r["total_units_4w"], reverse=True)

        current_week_monday_uae = safe_now_uae.date() - timedelta(days=safe_now_uae.weekday())
        current_week_start_uae = datetime.combine(current_week_monday_uae, time(0, 0, 0), tzinfo=UAE_TZ)
        trailing_start_utc = current_week_start_uae.astimezone(timezone.utc)
        trailing_end_utc = safe_now_utc

        trailing_units_by_asin: Dict[str, int] = {}
        if trend_rows:
            trailing_query = """
            SELECT asin, SUM(ordered_units) AS trailing_units
            FROM vendor_realtime_sales
            WHERE marketplace_id = ?
              AND hour_start_utc >= ?
              AND hour_start_utc < ?
            GROUP BY asin
            """
            trailing_rows = conn.execute(
                trailing_query,
                (marketplace_id, trailing_start_utc.isoformat(), trailing_end_utc.isoformat())
            ).fetchall()
            for row in trailing_rows:
                try:
                    trailing_units = int(row["trailing_units"] or 0)
                except Exception:
                    trailing_units = int(Decimal(row["trailing_units"] or 0)) if row["trailing_units"] is not None else 0
                trailing_units_by_asin[row["asin"]] = trailing_units

        window_start_uae = buckets["w4"]["start_uae"]
        window_end_uae = buckets["w1"]["end_uae"]
        this_week_start_uae = current_week_start_uae
        this_week_end_uae = safe_now_utc.astimezone(UAE_TZ)

        for row in trend_rows:
            this_week_units = trailing_units_by_asin.get(row["asin"], 0)
            baseline = float(max(row["w1_units"], 1))
            progress = this_week_units / baseline if baseline else 0.0
            progress = max(0.0, min(1.0, progress))

            row["this_week_units"] = this_week_units
            row["this_week_progress"] = progress
            row["trailing_units"] = this_week_units
            row["trailing_vs_w1_ratio"] = progress

        week_ranges_response = []
        for week_key in ["w4", "w3", "w2", "w1"]:
            bucket = buckets[week_key]
            week_ranges_response.append({
                "label": bucket["label"],
                "start_uae": bucket["start_uae"].isoformat(),
                "end_uae": bucket["end_uae"].isoformat(),
            })

        window_data = {
            "start_utc": window_start_utc.isoformat(),
            "end_utc": window_end_utc.isoformat(),
            "start_uae": window_start_uae.isoformat(),
            "end_uae": window_end_uae.isoformat(),
        }

        this_week_data = {
            "start_utc": trailing_start_utc.isoformat(),
            "end_utc": trailing_end_utc.isoformat(),
            "start_uae": this_week_start_uae.isoformat(),
            "end_uae": this_week_end_uae.isoformat(),
        }

        logger.info(
            f"{LOG_PREFIX_TRENDS} Window %s -> %s (UTC) with %d rows, coverage %.1f%% (%d/%d hours OK)",
            window_data["start_utc"],
            window_data["end_utc"],
            len(trend_rows),
            coverage_summary["coverage_percent"],
            coverage_summary["covered_hours"],
            coverage_summary["total_hours"],
        )

        return {
            "window": window_data,
            "bucket_size_days": 7,
            "bucket_labels": ["W4", "W3", "W2", "W1"],
            "week_ranges_uae": week_ranges_response,
            "this_week": this_week_data,
            "coverage": coverage_summary,
            "rows": trend_rows,
        }

    except Exception as exc:
        logger.error(
            f"{LOG_PREFIX_TRENDS} Failed to get sales trends: {exc}",
            exc_info=True
        )
        raise

def synthesize_pre_cutover_audit_hours(
    max_days: int = 3,
    *,
    marketplace_id: Optional[str] = None,
) -> dict:
    """
    One-time helper that inserts synthetic vendor_rt_audit_hours rows marked OK
    for the oldest hours in the current 4-week trends window that fall before
    the 30-day RT cutoff.
    """
    if not marketplace_id:
        raise ValueError("marketplace_id is required to synthesize coverage")

    safe_now_utc = get_safe_now_utc()
    cutoff_utc = safe_now_utc - timedelta(days=30)
    safe_now_uae = safe_now_utc.astimezone(UAE_TZ)
    buckets = _compute_sales_trend_week_buckets_uae(safe_now_uae, weeks=4)
    w4_def = buckets.get("w4")
    if not w4_def:
        logger.warning(f"{LOG_PREFIX_ADMIN} Unable to determine W4 bucket for synthetic coverage")
        return {
            "patched_hours": 0,
            "patched_days": 0,
            "cutoff_utc": _utc_iso(cutoff_utc),
            "w4_start_utc": None,
            "max_days": 0,
            "marketplace_id": marketplace_id,
        }

    clamped_days = max(1, min(int(max_days), 3))
    w4_start_utc = _normalize_utc_datetime(w4_def["start_uae"].astimezone(timezone.utc))
    normalized_cutoff = _normalize_utc_datetime(cutoff_utc)
    window_end = min(w4_start_utc + timedelta(days=clamped_days), normalized_cutoff)

    if window_end <= w4_start_utc:
        logger.info(
            f"{LOG_PREFIX_ADMIN} No pre-cutover hours before {window_end.isoformat()} "
            f"for {marketplace_id}"
        )
        return {
            "patched_hours": 0,
            "patched_days": 0,
            "cutoff_utc": _utc_iso(normalized_cutoff),
            "w4_start_utc": _utc_iso(w4_start_utc),
            "max_days": clamped_days,
            "marketplace_id": marketplace_id,
        }

    start_iso = _utc_iso(w4_start_utc)
    end_iso = _utc_iso(window_end)
    inserted_hours: List[datetime] = []
    existing_hours: set[str] = set()

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT hour_start_utc
            FROM vendor_rt_audit_hours
            WHERE marketplace_id = ?
              AND hour_start_utc >= ?
              AND hour_start_utc < ?
            """,
            (marketplace_id, start_iso, end_iso),
        ).fetchall()
        existing_hours = {row["hour_start_utc"] for row in rows if row["hour_start_utc"]}

        current = w4_start_utc
        while current < window_end:
            hour_key = _utc_iso(current)
            if hour_key not in existing_hours:
                hour_end = current + timedelta(hours=1)
                conn.execute(
                    """
                    INSERT INTO vendor_rt_audit_hours
                    (marketplace_id, hour_start_utc, hour_end_utc, status, updated_at_utc)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        marketplace_id,
                        hour_key,
                        _utc_iso(hour_end),
                        "OK",
                        now_iso,
                    ),
                )
                inserted_hours.append(current)
            current += timedelta(hours=1)

        if inserted_hours:
            conn.commit()

    patched_days = {
        hour.astimezone(UAE_TZ).date().isoformat() for hour in inserted_hours
    }

    logger.info(
        f"{LOG_PREFIX_ADMIN} Patched {len(inserted_hours)} synthetic hours "
        f"({len(patched_days)} days) for {marketplace_id} "
        f"from {start_iso} -> {end_iso} (cutoff {normalized_cutoff.isoformat()})"
    )

    return {
        "patched_hours": len(inserted_hours),
        "patched_days": len(patched_days),
        "cutoff_utc": _utc_iso(normalized_cutoff),
        "w4_start_utc": _utc_iso(w4_start_utc),
        "max_days": clamped_days,
        "marketplace_id": marketplace_id,
        "synthesized_until": end_iso,
    }


def _run_weekly_report_backfill(
    spapi_client: Any,
    marketplace_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> dict:
    """
    Request and ingest a single weekly GET_VENDOR_REAL_TIME_SALES_REPORT.
    """
    from services import spapi_reports
    from services.spapi_reports import SpApiQuotaError

    logger.info(
        f"{LOG_PREFIX_API} Weekly report request [%s, %s)",
        start_utc.isoformat(),
        end_utc.isoformat()
    )

    _ensure_spapi_call_allowed("weekly_report_backfill")
    try:
        report_id = spapi_reports.request_vendor_report(
            report_type="GET_VENDOR_REAL_TIME_SALES_REPORT",
            data_start=start_utc,
            data_end=end_utc,
            extra_options={"currencyCode": "AED"}
        )
        logger.debug(
            f"{LOG_PREFIX_API} Weekly report requested: {report_id}"
        )

        report_data = spapi_reports.poll_vendor_report(report_id)
        processing_status = report_data.get("processingStatus", "UNKNOWN")
        logger.debug(
            f"{LOG_PREFIX_API} Weekly report status: {processing_status}"
        )

        if processing_status == "FATAL":
            logger.warning(
                f"{LOG_PREFIX_API} Weekly report ended FATAL but has document_id, ingesting data anyway for [%s, %s)",
                start_utc.isoformat(),
                end_utc.isoformat()
            )

        document_id = report_data.get("reportDocumentId")
        if not document_id:
            logger.warning(
                f"{LOG_PREFIX_API} No document ID in weekly report [%s, %s)",
                start_utc,
                end_utc
            )
            return {"rows": 0, "asins": 0, "hours": 0}

        content, _ = spapi_reports.download_vendor_report_document(document_id)
        if isinstance(content, bytes):
            report_json = json.loads(content.decode("utf-8"))
        elif isinstance(content, str):
            report_json = json.loads(content)
        else:
            report_json = content

        summary = ingest_realtime_sales_report(
            report_json,
            marketplace_id=marketplace_id,
            currency_code="AED"
        )

        _record_audit_hours_for_window(
            start_utc,
            end_utc,
            marketplace_id,
            summary.get("hour_starts", []),
        )

        logger.info(
            f"{LOG_PREFIX_INGEST} Weekly chunk done: %d rows, window [%s, %s)",
            summary.get("rows", 0),
            start_utc.isoformat(),
            end_utc.isoformat()
        )

        return summary
    except SpApiQuotaError as exc:
        start_quota_cooldown(datetime.now(timezone.utc))
        logger.error(
            f"{LOG_PREFIX_API} Weekly chunk [%s, %s) hit quota: %s",
            start_utc.isoformat(),
            end_utc.isoformat(),
            exc,
        )
        raise


def run_one_time_four_week_backfill(
    spapi_client,
    marketplace_id: str,
) -> Dict[str, Any]:
    """
    One-time heavy backfill: ingest RT-sales for the last 4 full calendar weeks
    used by Sales Trends (W4..W1), aligned to Monday–Sunday in UAE time.

    Uses app_kv_store[SALES_TRENDS_4W_BACKFILL_KEY] as a gate so this
    cannot be accidentally run multiple times. On quota error, we DO NOT
    set the 'done' flag.
    
    Returns:
    {
        "status": "success" | "skipped" | "error",
        "rows": int,  # (for success)
        "asins": int,  # (for success)
        "hours": int,  # (for success)
        "start_utc": str,  # (for success)
        "end_utc": str,  # (for success)
        "completed_utc": str,  # (for success)
        "message": str,  # (for skipped)
        "ran_at_utc": str,  # (for skipped)
        "error": str,  # (for error: "QuotaExceeded" | "UnexpectedError")
        "message": str,  # (for error)
    }
    """
    from services import db as db_service
    from services.spapi_reports import SpApiQuotaError
    
    with db_service.get_db_connection() as conn:
        already = db_service.get_app_kv(conn, SALES_TRENDS_4W_BACKFILL_KEY)
        if already:
            # Already ran in the past; return a summary without doing anything
            return {
                "status": "skipped",
                "message": "4-week RT-sales backfill already completed",
                "ran_at_utc": already,
            }

    # Compute the exact same W4..W1 ranges the Sales Trends tab uses
    now_uae = datetime.now(UAE_TZ)
    buckets = _compute_sales_trend_week_buckets_uae(now_uae, weeks=4)

    # Determine overall backfill window in UTC:
    # from the START of W4 to the END of W1.
    w4 = buckets["w4"]
    w1 = buckets["w1"]
    start_uae = w4["start_uae"]
    end_uae = w1["end_uae"]

    # Convert UAE datetimes to UTC for the backfill function
    start_utc = start_uae.astimezone(timezone.utc)
    end_utc = (end_uae + timedelta(seconds=1)).astimezone(timezone.utc)

    total_rows = 0
    total_asins = 0
    total_hours = 0

    try:
        logger.info(
            f"{LOG_PREFIX_INGEST} Starting weekly 4-week backfill"
        )

        for week_key in ["w4", "w3", "w2", "w1"]:
            bucket = buckets[week_key]
            week_start_utc = bucket["start_uae"].astimezone(timezone.utc)
            week_end_utc = (bucket["end_uae"] + timedelta(seconds=1)).astimezone(timezone.utc)

            summary = _run_weekly_report_backfill(
                spapi_client=spapi_client,
                marketplace_id=marketplace_id,
                start_utc=week_start_utc,
                end_utc=week_end_utc,
            )

            total_rows += summary.get("rows", 0)
            total_asins += summary.get("asins", 0)
            total_hours += summary.get("hours", 0)

        rows, asins, hours = total_rows, total_asins, total_hours

        logger.info(
            f"{LOG_PREFIX_INGEST} Weekly backfill successful: %d rows, %d asins, %d hours",
            rows, asins, hours
        )

    except SpApiQuotaError:
        start_quota_cooldown(datetime.now(timezone.utc))
        logger.error(
            f"{LOG_PREFIX_API} Weekly backfill stopped early due to quota; already ingested %d rows.",
            total_rows,
            exc_info=True
        )
        return {
            "status": "error",
            "error": "QuotaExceeded",
            "message": "Amazon Vendor real-time sales quota was exceeded during weekly backfill. Already ingested partial data.",
            "rows": total_rows,
            "asins": total_asins,
            "hours": total_hours,
        }
    except Exception:
        logger.exception(f"{LOG_PREFIX_INGEST} Unexpected error during weekly backfill")
        return {
            "status": "error",
            "error": "UnexpectedError",
            "message": "Unexpected error during weekly backfill; check logs for details.",
        }

    audit_total_rows = 0
    audit_distinct_hours = 0
    with db_service.get_db_connection() as audit_conn:
        audit_stats = audit_conn.execute(
            """
            SELECT
                COUNT(*) AS total_rows,
                COUNT(DISTINCT hour_start_utc) AS distinct_hours
            FROM vendor_realtime_sales
            WHERE marketplace_id = ?
              AND hour_start_utc >= ?
              AND hour_start_utc < ?
            """,
            (marketplace_id, start_utc.isoformat(), end_utc.isoformat())
        ).fetchone()
        audit_total_rows = audit_stats["total_rows"] or 0
        audit_distinct_hours = audit_stats["distinct_hours"] or 0

    logger.info(
        f"{LOG_PREFIX_AUDIT} Audit 4-weeks window: %s rows, %s distinct hours in DB",
        audit_total_rows,
        audit_distinct_hours
    )

    # Only here, after successful ingestion, we mark it as done.
    completed_utc = datetime.now(timezone.utc).isoformat()
    with db_service.get_db_connection() as conn:
        db_service.set_app_kv(conn, SALES_TRENDS_4W_BACKFILL_KEY, completed_utc)

    return {
        "status": "success",
        "rows": rows,
        "asins": asins,
        "hours": hours,
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "completed_utc": completed_utc,
        "audit": {
            "rows": audit_total_rows,
            "distinct_hours": audit_distinct_hours,
        },
    }


def _build_empty_calendar(days: int, today: datetime) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Create placeholder buckets for the audit calendar."""
    bucket_dates = []
    buckets: Dict[str, Dict[str, Any]] = {}
    start_date = (today.date() - timedelta(days=days - 1))
    for i in range(days):
        current_date = start_date + timedelta(days=i)
        iso = current_date.isoformat()
        bucket_dates.append(iso)
        buckets[iso] = {
            "date": iso,
            "distinct_hours": 0,
            "total_rows": 0,
            "ok_hours": 0,
            "missing_hours": 0,
            "pending_hours": 0,
        }
    return bucket_dates, buckets


 # Audit calendar DB-only computation; this only uses stored vendor_rt tables and honors the 30-day retention window.
def get_rt_sales_audit_calendar(
    marketplace_id: str,
    days: int = AUDIT_CALENDAR_DEFAULT_DAYS,
) -> List[Dict[str, Any]]:
    """
    Compute the last `days` days of UAE calendar coverage solely from stored audit + sales rows.
    """
    if days is None:
        days = AUDIT_CALENDAR_DEFAULT_DAYS
    # Clamp to at most 30 days to align with Amazon's retention limits.
    days = max(1, min(days, AUDIT_CALENDAR_MAX_DAYS))

    now_uae = datetime.now(UAE_TZ)
    bucket_dates, buckets = _build_empty_calendar(days, now_uae)

    start_uae = datetime.combine(datetime.fromisoformat(bucket_dates[0]).date(), time(0, 0), tzinfo=UAE_TZ)
    end_uae = datetime.combine(datetime.fromisoformat(bucket_dates[-1]).date() + timedelta(days=1), time(0, 0), tzinfo=UAE_TZ)

    start_utc = start_uae.astimezone(timezone.utc)
    end_utc = end_uae.astimezone(timezone.utc)

    safe_now = get_safe_now_utc()
    rows: List[Dict[str, Any]] = []
    coverage_map: Dict[str, Dict[str, Any]] = {}
    # Query only up to the safe window so future hours are marked FUTURE_BLOCKED rather than missing.
    query_end_utc = min(end_utc, safe_now)

    try:
        with get_db_connection() as conn:
            if query_end_utc > start_utc:
                rows = conn.execute(
                    """
                    SELECT hour_start_utc, COUNT(*) AS total_rows
                    FROM vendor_realtime_sales
                    WHERE marketplace_id = ?
                      AND hour_start_utc >= ?
                      AND hour_start_utc < ?
                    GROUP BY hour_start_utc
                    ORDER BY hour_start_utc ASC
                    """,
                    (marketplace_id, start_utc.isoformat(), query_end_utc.isoformat())
                ).fetchall()
                if not rows:
                    logger.info(
                        f"{LOG_PREFIX_AUDIT} No RT sales rows found for audit window {start_utc.isoformat()} -> {query_end_utc.isoformat()}"
                    )
            else:
                logger.info(
                    f"{LOG_PREFIX_AUDIT} Audit window {start_utc.isoformat()} -> {end_utc.isoformat()} is fully in the future; skipping row query."
                )

            coverage_map = _build_hourly_coverage_map(
                conn,
                marketplace_id,
                start_utc,
                end_utc,
                safe_now=safe_now,
            )
    except sqlite3.OperationalError as exc:
        logger.warning(
            f"{LOG_PREFIX_AUDIT} Audit calendar query failed due to missing table: {exc}"
        )
    except Exception as exc:
        logger.error(
            f"{LOG_PREFIX_AUDIT} Unexpected error building audit calendar: {exc}",
            exc_info=True
        )

    for row in rows:
        hour_start = row["hour_start_utc"]
        total_rows = row["total_rows"] or 0
        try:
            hour_dt = datetime.fromisoformat(hour_start.replace("Z", "+00:00"))
        except Exception:
            logger.warning(f"{LOG_PREFIX_AUDIT} Invalid hour_start: %s", hour_start)
            continue

        local_dt = hour_dt.astimezone(UAE_TZ)
        date_key = local_dt.date().isoformat()
        bucket = buckets.get(date_key)
        if not bucket:
            continue

        bucket["total_rows"] += total_rows
    for hour_start, coverage_row in coverage_map.items():
        try:
            hour_dt = datetime.fromisoformat(hour_start.replace("Z", "+00:00"))
        except Exception:
            logger.warning(f"{LOG_PREFIX_AUDIT} Invalid coverage hour_start: %s", hour_start)
            continue

        local_dt = hour_dt.astimezone(UAE_TZ)
        date_key = local_dt.date().isoformat()
        bucket = buckets.get(date_key)
        if not bucket:
            continue

        coverage_status = coverage_row["status"]
        if coverage_status in {"OK", "OK_INFERRED", "EMPTY"}:
            bucket["ok_hours"] += 1
        elif coverage_status == "MISSING":
            bucket["missing_hours"] += 1
        elif coverage_status == "FUTURE_BLOCKED":
            bucket["pending_hours"] += 1

    results = []
    for iso in bucket_dates:
        bucket = buckets[iso]
        ok_hours = bucket["ok_hours"]
        if bucket["total_rows"] == 0:
            status = "no-data"
        elif ok_hours >= 24:
            status = "full"
        elif ok_hours > 0:
            status = "partial"
        else:
            status = "missing"

        results.append({
            "date": iso,
            "distinct_hours": ok_hours,
            "total_rows": bucket["total_rows"],
            "status": status,
            "ok_hours": bucket["ok_hours"],
            "missing_hours": bucket["missing_hours"],
            "pending_hours": bucket["pending_hours"],
            "total_hours": 24,
        })

    return results


 # Daily audit helper derived from the coverage map; used by /api/vendor-realtime-sales/audit-day.
def get_rt_sales_audit_day(
    marketplace_id: str,
    date_str: str
) -> Dict[str, Any]:
    """
    Return hour-level coverage for a single UAE date.
    The response contains the raw hourly statuses plus lists of missing/pending hours.
    """
    try:
        target_date = datetime.fromisoformat(date_str).date()
    except Exception as exc:
        raise ValueError(f"Invalid date format: {date_str}") from exc

    hours_detail, missing_hours, pending_hours = _classify_daily_hours(
        date_str,
        marketplace_id,
        latest_allowed_end=get_safe_now_utc(),
    )

    return {
        "date": target_date.isoformat(),
        "hours": [{"hour": info["hour"], "status": info["status"]} for info in hours_detail],
        "missing_hours": missing_hours,
        "pending_hours": pending_hours,
    }
