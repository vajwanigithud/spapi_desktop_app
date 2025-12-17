import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.db import get_db_connection

logger = logging.getLogger(__name__)

LEDGER_TABLE = "vendor_rt_sales_hour_ledger"
LEDGER_INDEX = "idx_vendor_rt_sales_hour_ledger_status"
STATUS_MISSING = "MISSING"
STATUS_REQUESTED = "REQUESTED"
STATUS_DOWNLOADED = "DOWNLOADED"
STATUS_APPLIED = "APPLIED"
STATUS_FAILED = "FAILED"
CLAIMABLE_STATUSES: Tuple[str, str] = (STATUS_MISSING, STATUS_FAILED)


def ensure_vendor_rt_sales_ledger_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
            marketplace_id TEXT NOT NULL,
            hour_utc TEXT NOT NULL,
            status TEXT NOT NULL,
            report_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            next_retry_utc TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (marketplace_id, hour_utc)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {LEDGER_INDEX}
        ON {LEDGER_TABLE} (marketplace_id, status)
        """
    )
    conn.commit()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _floor_to_hour(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def iso_hour_floor(dt: datetime) -> str:
    return _floor_to_hour(dt).isoformat()


def compute_required_hours(end_utc: datetime, lookback_hours: int) -> List[str]:
    if lookback_hours <= 0:
        return []
    end_floor = _floor_to_hour(end_utc)
    start_floor = end_floor - timedelta(hours=lookback_hours - 1)
    count = int((end_floor - start_floor).total_seconds() // 3600) + 1
    return [(start_floor + timedelta(hours=i)).isoformat() for i in range(count)]


def ensure_hours_exist(marketplace_id: str, hours: Iterable[str]) -> int:
    hours = [h for h in hours if h]
    if not marketplace_id or not hours:
        return 0
    now_iso = _utc_now_iso()
    inserted = 0
    with get_db_connection() as conn:
        ensure_vendor_rt_sales_ledger_table(conn)
        for hour in hours:
            try:
                cursor = conn.execute(
                    f"""
                    INSERT INTO {LEDGER_TABLE} (
                        marketplace_id, hour_utc, status,
                        report_id, attempt_count, last_error,
                        next_retry_utc, created_at_utc, updated_at_utc
                    ) VALUES (?, ?, ?, NULL, 0, NULL, NULL, ?, ?)
                    ON CONFLICT(marketplace_id, hour_utc) DO NOTHING
                    """,
                    (marketplace_id, hour, STATUS_MISSING, now_iso, now_iso),
                )
                if cursor.rowcount:
                    inserted += 1
            except sqlite3.Error as exc:
                logger.error("[RtSalesLedger] ensure_hours_exist failed: %s", exc)
                raise
        conn.commit()
    return inserted


def _fetch_row(conn: sqlite3.Connection, marketplace_id: str, hour_utc: str) -> Optional[Dict]:
    row = conn.execute(
        f"""
        SELECT *
        FROM {LEDGER_TABLE}
        WHERE marketplace_id = ? AND hour_utc = ?
        """,
        (marketplace_id, hour_utc),
    ).fetchone()
    return dict(row) if row else None


def claim_next_missing_hour(marketplace_id: str, now_utc: datetime) -> Optional[Dict[str, Any]]:
    if not marketplace_id:
        return None
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    now_iso = now_utc.replace(microsecond=0).isoformat()
    with get_db_connection() as conn:
        ensure_vendor_rt_sales_ledger_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""
            SELECT hour_utc
            FROM {LEDGER_TABLE}
            WHERE marketplace_id = ?
              AND status IN (?, ?)
              AND (next_retry_utc IS NULL OR next_retry_utc <= ?)
            ORDER BY hour_utc ASC
            LIMIT 1
            """,
            (marketplace_id, *CLAIMABLE_STATUSES, now_iso),
        ).fetchone()
        if not row:
            conn.commit()
            return None
        hour_utc = row["hour_utc"]
        conn.execute(
            f"""
            UPDATE {LEDGER_TABLE}
            SET status = ?, attempt_count = attempt_count + 1,
                updated_at_utc = ?, last_error = NULL, next_retry_utc = NULL
            WHERE marketplace_id = ? AND hour_utc = ?
            """,
            (STATUS_REQUESTED, now_iso, marketplace_id, hour_utc),
        )
        conn.commit()
        return _fetch_row(conn, marketplace_id, hour_utc)


def mark_downloaded(marketplace_id: str, hour_utc: str, report_id: Optional[str]) -> None:
    if not marketplace_id or not hour_utc:
        return
    now_iso = _utc_now_iso()
    with get_db_connection() as conn:
        ensure_vendor_rt_sales_ledger_table(conn)
        conn.execute(
            f"""
            UPDATE {LEDGER_TABLE}
            SET status = ?, report_id = ?, updated_at_utc = ?
            WHERE marketplace_id = ? AND hour_utc = ?
            """,
            (STATUS_DOWNLOADED, report_id, now_iso, marketplace_id, hour_utc),
        )
        conn.commit()


def mark_applied(marketplace_id: str, hour_utc: str) -> None:
    if not marketplace_id or not hour_utc:
        return
    now_iso = _utc_now_iso()
    with get_db_connection() as conn:
        ensure_vendor_rt_sales_ledger_table(conn)
        conn.execute(
            f"""
            UPDATE {LEDGER_TABLE}
            SET status = ?, updated_at_utc = ?
            WHERE marketplace_id = ? AND hour_utc = ?
            """,
            (STATUS_APPLIED, now_iso, marketplace_id, hour_utc),
        )
        conn.commit()


def mark_failed(
    marketplace_id: str,
    hour_utc: str,
    error: str,
    cooldown_minutes: int,
) -> None:
    if not marketplace_id or not hour_utc:
        return
    now = datetime.now(timezone.utc)
    retry_at = now + timedelta(minutes=max(cooldown_minutes, 0))
    now_iso = now.replace(microsecond=0).isoformat()
    retry_iso = retry_at.replace(microsecond=0).isoformat()
    with get_db_connection() as conn:
        ensure_vendor_rt_sales_ledger_table(conn)
        conn.execute(
            f"""
            UPDATE {LEDGER_TABLE}
            SET status = ?, last_error = ?, next_retry_utc = ?, updated_at_utc = ?
            WHERE marketplace_id = ? AND hour_utc = ?
            """,
            (STATUS_FAILED, (error or "")[:500], retry_iso, now_iso, marketplace_id, hour_utc),
        )
        conn.commit()


def list_ledger_rows(marketplace_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    with get_db_connection() as conn:
        ensure_vendor_rt_sales_ledger_table(conn)
        rows = conn.execute(
            f"""
            SELECT *
            FROM {LEDGER_TABLE}
            WHERE marketplace_id = ?
            ORDER BY hour_utc ASC
            LIMIT ?
            """,
            (marketplace_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]
