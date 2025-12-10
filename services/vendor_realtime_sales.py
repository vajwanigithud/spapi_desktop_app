"""
Vendor Real Time Sales Report handler.

Consumes GET_VENDOR_REAL_TIME_SALES_REPORT from SP-API and provides:
- Ingestion into SQLite (vendor_realtime_sales table)
- State tracking (vendor_rt_sales_state table) to avoid gaps
- Backfill logic with safe time windows
- Aggregation and querying for UI
- Support for flexible lookback windows and view-by modes (ASIN / Time)
"""

import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from decimal import Decimal

try:
    # Python 3.9+ standard lib
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:
    # Fallback for environments without zoneinfo
    ZoneInfo = None
    class ZoneInfoNotFoundError(Exception):
        pass

from services.db import execute_write, get_db_connection, execute_many_write
from services.perf import time_block

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

# In-memory backfill lock to prevent overlapping cycles
_rt_sales_backfill_in_progress = False
_rt_sales_backfill_lock_acquired_at_utc = None  # type: Optional[datetime]


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
    logger.debug("[VendorRtSales] Backfill lock acquired")
    return True


def end_backfill() -> None:
    """Release the backfill lock."""
    global _rt_sales_backfill_in_progress, _rt_sales_backfill_lock_acquired_at_utc
    if _rt_sales_backfill_in_progress and _rt_sales_backfill_lock_acquired_at_utc:
        elapsed = (datetime.now(timezone.utc) - _rt_sales_backfill_lock_acquired_at_utc).total_seconds()
        logger.debug(f"[VendorRtSales] Backfill lock released (held for {elapsed:.1f}s)")
    _rt_sales_backfill_in_progress = False
    _rt_sales_backfill_lock_acquired_at_utc = None


def start_quota_cooldown(now_utc: datetime) -> None:
    """Start a quota cooldown period (prevents further API calls for a while)."""
    global _rt_sales_quota_cooldown_until_utc
    _rt_sales_quota_cooldown_until_utc = now_utc + timedelta(minutes=QUOTA_COOLDOWN_MINUTES)
    logger.warning(
        f"[VendorRtSales] Quota cooldown started until {_rt_sales_quota_cooldown_until_utc.isoformat()}"
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
        "message": message
    }


# ====================================================================
# TIME CONSTANTS FOR SAFE BACKFILLING
# ====================================================================
SAFE_MINUTES_LAG = 10       # Buffer to avoid future/not-yet-ready hours
MAX_HISTORY_DAYS = 3        # How far back we backfill on startup
CHUNK_HOURS = 6             # Window size per report request


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
            logger.warning(f"[VendorRtSales] Failed to parse last_ingested_end_utc {utc_str}: {e}")
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
                f"[VendorRtSales] Updated last_ingested_end_utc for {marketplace_id} to {end_utc_str}"
            )
    except Exception as exc:
        logger.error(
            f"[VendorRtSales] Failed to update state for {marketplace_id}: {exc}",
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
        logger.info("[VendorRtSales] vendor_realtime_sales table ensured")
    except Exception as exc:
        logger.error(f"[VendorRtSales] Failed to ensure table: {exc}", exc_info=True)
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
        logger.warning(f"[VendorRtSales] Failed to get last_ingested_end_utc: {e}")
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
        logger.debug(f"[VendorRtSales] Updated state for {marketplace_id}: {end_str}")
    except Exception as e:
        logger.error(f"[VendorRtSales] Failed to update state: {e}", exc_info=True)
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
        logger.warning(f"[VendorRtSales] Failed to get audit state: {e}")
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
        
        logger.debug(f"[VendorRtSales] Updated daily audit state for {marketplace_id}: {ts_str}")
    except Exception as e:
        logger.error(f"[VendorRtSales] Failed to update daily audit state: {e}", exc_info=True)
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
        
        logger.debug(f"[VendorRtSales] Updated weekly audit state for {marketplace_id}: {ts_str}")
    except Exception as e:
        logger.error(f"[VendorRtSales] Failed to update weekly audit state: {e}", exc_info=True)
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
        logger.info("[VendorRtSales] Empty report data; returning empty summary")
        return {"rows": 0, "asins": 0, "hours": 0}
    
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
                logger.warning("[VendorRtSales] Skipping line with missing asin/time: %s", line)
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
                logger.warning(f"[VendorRtSales] Failed to parse endTime {hour_end}: {e}")
        except Exception as e:
            logger.warning("[VendorRtSales] Error processing line %s: %s", line, e)
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
                "[VendorRtSales] Ingested %d rows, %d ASINs, %d hours",
                len(rows_to_insert),
                len(seen_asins),
                len(seen_hours)
            )
        except Exception as exc:
            logger.error(f"[VendorRtSales] Failed to insert rows: {exc}", exc_info=True)
            raise
    
    return {
        "rows": len(rows_to_insert),
        "asins": len(seen_asins),
        "hours": len(seen_hours)
    }


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
        # Parse input times
        start_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
        
        # Calculate lookback_hours for metadata
        lookback_hours = round((end_dt - start_dt).total_seconds() / 3600)
        
        with get_db_connection() as conn:
            # Get total units and revenue
            query = """
            SELECT
                SUM(ordered_units) as total_units,
                SUM(ordered_revenue) as total_revenue,
                MAX(currency_code) as currency_code
            FROM vendor_realtime_sales
            WHERE hour_start_utc >= ? AND hour_start_utc < ?
            """
            params = [start_utc, end_utc]
            if marketplace_id:
                query += " AND marketplace_id = ?"
                params.append(marketplace_id)
            
            totals_row = conn.execute(query, params).fetchone()
            total_units = totals_row["total_units"] or 0
            total_revenue = totals_row["total_revenue"] or 0.0
            currency_code = totals_row["currency_code"] or "AED"
            
            # Build window metadata
            window_data = {
                "start_utc": start_utc,
                "end_utc": end_utc,
                "start_uae": utc_to_uae_str(start_dt),
                "end_uae": utc_to_uae_str(end_dt)
            }
            
            # Aggregate by ASIN or by time bucket
            if view_by == "time":
                rows = _get_realtime_sales_by_time(
                    conn, start_utc, end_utc, marketplace_id
                )
            else:
                # Default: view_by="asin"
                rows = _get_realtime_sales_by_asin(
                    conn, start_utc, end_utc, marketplace_id
                )
            
            return {
                "lookback_hours": lookback_hours,
                "view_by": view_by,
                "window": window_data,
                "total_units": total_units,
                "total_revenue": round(float(total_revenue), 2),
                "currency_code": currency_code,
                "rows": rows
            }
    except Exception as exc:
        logger.error(
            f"[VendorRtSales] Failed to get summary [{start_utc}, {end_utc}): {exc}",
            exc_info=True
        )
        raise


def _get_realtime_sales_by_asin(
    conn,
    start_utc: str,
    end_utc: str,
    marketplace_id: Optional[str] = None
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
    marketplace_id: Optional[str] = None
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
            f"[VendorRtSales] Failed to get ASIN detail for {asin} [{start_utc}, {end_utc}): {exc}",
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
            logger.info(f"[VendorRtSales] Deleted {deleted} old records")
            return deleted
    except Exception as exc:
        logger.error(f"[VendorRtSales] Failed to clear old data: {exc}", exc_info=True)
        raise


# ====================================================================
# BACKFILL LOGIC FOR GAP DETECTION
# ====================================================================

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
    import time
    from services import spapi_reports
    from services.spapi_reports import SpApiQuotaError

    # Clamp end to safe_now
    safe_now = get_safe_now_utc()
    if start_utc >= safe_now:
        logger.debug("[VendorRtSalesBackfill] Start time already >= safe_now; nothing to backfill")
        return (0, 0, 0)

    end_utc_clamped = min(end_utc, safe_now)
    if end_utc_clamped <= start_utc:
        logger.debug("[VendorRtSalesBackfill] Clamped end <= start; nothing to backfill")
        return (0, 0, 0)

    total_rows = 0
    total_asins = set()
    total_hours = set()

    current_start = start_utc
    while current_start < end_utc_clamped:
        current_end = min(current_start + timedelta(hours=CHUNK_HOURS), end_utc_clamped)

        logger.info(
            "[VendorRtSalesBackfill] Requesting chunk [%s, %s)",
            current_start.isoformat(),
            current_end.isoformat()
        )

        try:
            # Request report
            report_id = spapi_reports.request_vendor_report(
                report_type="GET_VENDOR_REAL_TIME_SALES_REPORT",
                data_start=current_start,
                data_end=current_end,
                extra_options={"currencyCode": "AED"}
            )
            logger.debug(f"[VendorRtSalesBackfill] Report requested: {report_id}")

            # Poll until DONE
            report_data = spapi_reports.poll_vendor_report(report_id)
            processing_status = report_data.get("processingStatus", "UNKNOWN")
            logger.debug(f"[VendorRtSalesBackfill] Report status: {processing_status}")

            if processing_status == "DONE":
                document_id = report_data.get("reportDocumentId")
                if document_id:
                    # Download and parse
                    content, _ = spapi_reports.download_vendor_report_document(document_id)
                    if isinstance(content, bytes):
                        report_json = json.loads(content.decode("utf-8"))
                    elif isinstance(content, str):
                        report_json = json.loads(content)
                    else:
                        report_json = content

                    # Ingest
                    summary = ingest_realtime_sales_report(
                        report_json,
                        marketplace_id=marketplace_id,
                        currency_code="AED"
                    )

                    total_rows += summary.get("rows", 0)
                    total_asins.update(summary.get("asins", []) if isinstance(summary.get("asins"), (list, set)) else [])

                    # Track unique hours
                    if summary.get("rows", 0) > 0:
                        total_hours.add(current_start.isoformat())

                    logger.info(
                        "[VendorRtSalesBackfill] Chunk done: %d rows, cumulative total: %d rows",
                        summary.get("rows", 0),
                        total_rows
                    )
                else:
                    logger.warning("[VendorRtSalesBackfill] No document ID in DONE report")
            else:
                logger.warning(
                    "[VendorRtSalesBackfill] Report not DONE: status=%s",
                    processing_status
                )
                # Still consider it an attempt; move on

        except SpApiQuotaError as e:
            # HARD STOP: Quota exceeded, abort remaining chunks and re-raise
            logger.error(
                "[VendorRtSalesBackfill] QUOTA EXCEEDED at chunk [%s, %s): %s. "
                "Aborting remaining chunks.",
                current_start.isoformat(),
                current_end.isoformat(),
                e,
            )
            raise  # Re-raise so caller can activate cooldown

        except Exception as e:
            # Other errors: log and continue (do not corrupt state)
            logger.error(
                "[VendorRtSalesBackfill] Failed to process chunk [%s, %s): %s",
                current_start.isoformat(),
                current_end.isoformat(),
                e,
                exc_info=True
            )
            # Continue with next chunk despite error

        current_start = current_end
        time.sleep(1)  # Small delay between requests

    logger.info(
        "[VendorRtSalesBackfill] Backfill complete: %d total rows, %d unique hours",
        total_rows,
        len(total_hours)
    )
    return (total_rows, len(total_asins), len(total_hours))


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
        "[VendorRtSalesAudit] Starting %s audit for [%s, %s)",
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
            "[VendorRtSalesAudit] %s audit complete: %d rows, %d ASINs, %d hours",
            label.capitalize(),
            rows,
            asins,
            hours
        )
        
        return (rows, asins, hours)
    except Exception as e:
        # Re-raise quota errors; log and suppress others
        from services.spapi_reports import SpApiQuotaError
        if isinstance(e, SpApiQuotaError):
            logger.error(
                "[VendorRtSalesAudit] %s audit hit quota: %s",
                label.capitalize(),
                e
            )
            raise  # Propagate quota error to caller
        else:
            logger.error(
                "[VendorRtSalesAudit] %s audit failed: %s",
                label.capitalize(),
                e,
                exc_info=True
            )
            return (0, 0, 0)  # Suppress other errors

