import logging
from typing import Any, Dict, List, Optional, Tuple

from services.db import get_db_connection, execute_write

logger = logging.getLogger(__name__)


def init_vendor_po_lines_table() -> None:
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
        logger.info("[DBRepo] vendor_po_lines table ensured")
    except Exception as exc:
        logger.error(f"[DBRepo] Failed to ensure vendor_po_lines table: {exc}", exc_info=True)
        raise


def delete_vendor_po_lines_for_po(po_number: str) -> None:
    try:
        execute_write("DELETE FROM vendor_po_lines WHERE po_number = ?", (po_number,))
    except Exception as exc:
        logger.error(f"[DBRepo] Failed to delete vendor_po_lines for PO {po_number}: {exc}", exc_info=True)
        raise


def insert_vendor_po_line(
    po_number: str,
    ship_to_location: str,
    asin: str,
    sku: str,
    ordered_qty: int,
    accepted_qty: int,
    cancelled_qty: int,
    shipped_qty: int,
    received_qty: int,
    shortage_qty: int,
    pending_qty: int,
    last_changed_utc: str,
) -> None:
    sql = """
    INSERT INTO vendor_po_lines
    (po_number, ship_to_location, asin, sku, ordered_qty, accepted_qty,
     cancelled_qty, shipped_qty, received_qty, shortage_qty, pending_qty, last_changed_utc)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        po_number,
        ship_to_location,
        asin,
        sku,
        ordered_qty,
        accepted_qty,
        cancelled_qty,
        shipped_qty,
        received_qty,
        shortage_qty,
        pending_qty,
        last_changed_utc,
    )
    try:
        execute_write(sql, params)
    except Exception as exc:
        logger.error(f"[DBRepo] Failed to insert vendor_po_lines row for PO {po_number}, ASIN {asin}: {exc}", exc_info=True)
        raise


def get_vendor_po_lines(po_number: str) -> List[Dict[str, Any]]:
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT po_number, ship_to_location, asin, sku, ordered_qty, accepted_qty,
                       cancelled_qty, shipped_qty, received_qty, shortage_qty, pending_qty, last_changed_utc
                FROM vendor_po_lines
                WHERE po_number = ?
                ORDER BY asin
                """,
                (po_number,),
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception as exc:
        logger.error(f"[DBRepo] Failed to fetch vendor_po_lines for PO {po_number}: {exc}", exc_info=True)
        raise


def get_vendor_po_line_totals(po_numbers: List[str]) -> Dict[str, Dict[str, int]]:
    if not po_numbers:
        return {}
    qmarks = ",".join(["?"] * len(po_numbers))
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    po_number,
                    COALESCE(SUM(ordered_qty), 0) AS total_ordered,
                    COALESCE(SUM(accepted_qty), 0) AS total_accepted,
                    COALESCE(SUM(cancelled_qty), 0) AS total_cancelled,
                    COALESCE(SUM(received_qty), 0) AS total_received,
                    COALESCE(SUM(pending_qty), 0) AS total_pending,
                    COALESCE(SUM(shortage_qty), 0) AS total_shortage
                FROM vendor_po_lines
                WHERE po_number IN ({qmarks})
                GROUP BY po_number
                """,
                po_numbers,
            ).fetchall()
            return {row["po_number"]: dict(row) for row in rows}
    except Exception as exc:
        logger.error(f"[DBRepo] Failed to aggregate vendor_po_lines for {len(po_numbers)} POs: {exc}", exc_info=True)
        raise


def get_vendor_po_line_totals_for_po(po_number: str) -> Dict[str, int]:
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(ordered_qty), 0) AS total_ordered,
                    COALESCE(SUM(accepted_qty), 0) AS total_accepted,
                    COALESCE(SUM(cancelled_qty), 0) AS total_cancelled,
                    COALESCE(SUM(received_qty), 0) AS total_received,
                    COALESCE(SUM(pending_qty), 0) AS total_pending,
                    COALESCE(SUM(shortage_qty), 0) AS total_shortage
                FROM vendor_po_lines
                WHERE po_number = ?
                """,
                (po_number,),
            ).fetchone()
            return dict(row) if row else {}
    except Exception as exc:
        logger.error(f"[DBRepo] Failed to aggregate vendor_po_lines for PO {po_number}: {exc}", exc_info=True)
        raise


def count_vendor_po_lines() -> int:
    try:
        with get_db_connection() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM vendor_po_lines").fetchone()
            return row["cnt"] if row else 0
    except Exception as exc:
        logger.warning(f"[DBRepo] Failed to count vendor_po_lines: {exc}")
        return 0


def get_rejected_vendor_po_lines(po_numbers: List[str]) -> List[Dict[str, Any]]:
    if not po_numbers:
        return []
    qmarks = ",".join(["?"] * len(po_numbers))
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT po_number, asin, sku, ship_to_location, ordered_qty, cancelled_qty, accepted_qty
                FROM vendor_po_lines
                WHERE po_number IN ({qmarks})
                """,
                po_numbers,
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception as exc:
        logger.warning(f"[DBRepo] Failed to fetch rejected vendor_po_lines for {len(po_numbers)} POs: {exc}")
        return []
