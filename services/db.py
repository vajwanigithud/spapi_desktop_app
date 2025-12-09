import sqlite3
from pathlib import Path
from contextlib import contextmanager
from threading import Lock
import logging

logger = logging.getLogger(__name__)
CATALOG_DB_PATH = Path(__file__).resolve().parent.parent / "catalog.db"

# ====================================================================
# FIX #2: SQLITE HARDENING WITH WAL MODE + TIMEOUT + WRITE LOCK
# - WAL mode allows concurrent reads while serializing writes
# - 10s timeout prevents infinite hangs on database locks
# - _db_write_lock serializes all INSERT/UPDATE/DELETE to prevent SQLITE_BUSY
# - Context manager ensures proper cleanup even on exceptions
# ====================================================================

_db_write_lock = Lock()
_db_timeout = 10  # seconds

@contextmanager
def get_db_connection():
    """
    Context manager for safe SQLite connection.
    - Enforces timeout to prevent infinite waits
    - Enables WAL mode for better concurrency
    - Ensures cleanup even on exception
    """
    conn = None
    try:
        conn = sqlite3.connect(CATALOG_DB_PATH, timeout=_db_timeout)
        conn.row_factory = sqlite3.Row
        # Enable WAL (Write-Ahead Logging) for better concurrency
        # Allows multiple readers while one writer is active
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    except sqlite3.DatabaseError as e:
        logger.error(f"[DB] Database error: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"[DB] Unexpected error: {e}", exc_info=True)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception as e:
                logger.warning(f"[DB] Error closing connection: {e}")


def execute_write(sql: str, params: tuple = (), commit: bool = True):
    """
    Serialize all write operations to prevent SQLITE_BUSY errors.
    
    Args:
        sql: SQL statement to execute
        params: Tuple of parameters for the statement
        commit: Whether to auto-commit (default True)
    """
    with _db_write_lock:
        with get_db_connection() as conn:
            try:
                conn.execute(sql, params)
                if commit:
                    conn.commit()
                return conn.cursor().lastrowid
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    logger.error(f"[DB] Database locked after {_db_timeout}s timeout: {e}")
                raise
            except Exception as exc:
                logger.error(f"[DB] Write failed for SQL: {sql} params={params}: {exc}", exc_info=True)
                raise


def execute_many_write(sql: str, seq_of_params: list[tuple], commit: bool = True) -> None:
    """
    Batched write helper with the same write lock/timeout safety.
    """
    with _db_write_lock:
        with get_db_connection() as conn:
            try:
                conn.executemany(sql, seq_of_params)
                if commit:
                    conn.commit()
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    logger.error(f"[DB] Batch write locked after {_db_timeout}s timeout: {e}")
                raise
            except Exception as exc:
                logger.error(f"[DB] Batch write failed for SQL: {sql} params_count={len(seq_of_params)}: {exc}", exc_info=True)
                raise


def init_vendor_rt_sales_state_table() -> None:
    """
    Create vendor_rt_sales_state table if it does not exist.
    Tracks the last ingested hour end time per marketplace to avoid gaps.
    Also tracks daily and weekly audit timestamps.
    """
    sql = """
    CREATE TABLE IF NOT EXISTS vendor_rt_sales_state (
        marketplace_id TEXT PRIMARY KEY,
        last_ingested_end_utc TEXT,
        last_daily_audit_utc TEXT,
        last_weekly_audit_utc TEXT
    )
    """
    try:
        execute_write(sql)
        logger.info("[DB] vendor_rt_sales_state table ensured")
        
        # Lightweight migration for older DBs: add audit columns if missing
        with get_db_connection() as conn:
            for col in ("last_daily_audit_utc", "last_weekly_audit_utc"):
                try:
                    conn.execute(f"ALTER TABLE vendor_rt_sales_state ADD COLUMN {col} TEXT")
                    conn.commit()
                    logger.info(f"[DB] Added column {col} to vendor_rt_sales_state")
                except sqlite3.OperationalError:
                    # Column already exists – ignore
                    pass
    except Exception as exc:
        logger.error(f"[DB] Failed to ensure vendor_rt_sales_state table: {exc}", exc_info=True)
        raise


def get_last_ingested_end_utc_db(conn, marketplace_id: str):
    """
    Query the last ingested end time for a marketplace from the DB connection.
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
    
    Returns:
        The ISO8601 timestamp string or None if not found.
    """
    try:
        row = conn.execute(
            "SELECT last_ingested_end_utc FROM vendor_rt_sales_state WHERE marketplace_id = ?",
            (marketplace_id,)
        ).fetchone()
        return row["last_ingested_end_utc"] if row else None
    except Exception as exc:
        logger.error(f"[DB] Failed to get last_ingested_end_utc for {marketplace_id}: {exc}")
        raise


def update_last_ingested_end_utc_db(conn, marketplace_id: str, end_utc_str: str) -> None:
    """
    Update or insert the last ingested end time for a marketplace.
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
        end_utc_str: ISO8601 timestamp string
    """
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO vendor_rt_sales_state
            (marketplace_id, last_ingested_end_utc)
            VALUES (?, ?)
            """,
            (marketplace_id, end_utc_str)
        )
        conn.commit()
    except Exception as exc:
        logger.error(
            f"[DB] Failed to update last_ingested_end_utc for {marketplace_id} to {end_utc_str}: {exc}"
        )
        raise


def update_last_daily_audit_utc_db(conn, marketplace_id: str, audit_utc_str: str) -> None:
    """
    Update or insert the last daily audit timestamp for a marketplace.
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
        audit_utc_str: ISO8601 timestamp string
    """
    try:
        # Get current row or create with NULLs
        current = conn.execute(
            "SELECT * FROM vendor_rt_sales_state WHERE marketplace_id = ?",
            (marketplace_id,)
        ).fetchone()
        
        if current:
            conn.execute(
                "UPDATE vendor_rt_sales_state SET last_daily_audit_utc = ? WHERE marketplace_id = ?",
                (audit_utc_str, marketplace_id)
            )
        else:
            conn.execute(
                """
                INSERT INTO vendor_rt_sales_state
                (marketplace_id, last_daily_audit_utc)
                VALUES (?, ?)
                """,
                (marketplace_id, audit_utc_str)
            )
        conn.commit()
    except Exception as exc:
        logger.error(
            f"[DB] Failed to update last_daily_audit_utc for {marketplace_id} to {audit_utc_str}: {exc}"
        )
        raise


def update_last_weekly_audit_utc_db(conn, marketplace_id: str, audit_utc_str: str) -> None:
    """
    Update or insert the last weekly audit timestamp for a marketplace.
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
        audit_utc_str: ISO8601 timestamp string
    """
    try:
        # Get current row or create with NULLs
        current = conn.execute(
            "SELECT * FROM vendor_rt_sales_state WHERE marketplace_id = ?",
            (marketplace_id,)
        ).fetchone()
        
        if current:
            conn.execute(
                "UPDATE vendor_rt_sales_state SET last_weekly_audit_utc = ? WHERE marketplace_id = ?",
                (audit_utc_str, marketplace_id)
            )
        else:
            conn.execute(
                """
                INSERT INTO vendor_rt_sales_state
                (marketplace_id, last_weekly_audit_utc)
                VALUES (?, ?)
                """,
                (marketplace_id, audit_utc_str)
            )
        conn.commit()
    except Exception as exc:
        logger.error(
            f"[DB] Failed to update last_weekly_audit_utc for {marketplace_id} to {audit_utc_str}: {exc}"
        )
        raise


def get_vendor_rt_sales_state_db(conn, marketplace_id: str) -> dict:
    """
    Get the full audit state for a marketplace.
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
    
    Returns:
        A dict with keys: marketplace_id, last_ingested_end_utc, last_daily_audit_utc, last_weekly_audit_utc
        All timestamp values are ISO8601 strings or None.
    """
    try:
        row = conn.execute(
            """
            SELECT marketplace_id, last_ingested_end_utc, last_daily_audit_utc, last_weekly_audit_utc
            FROM vendor_rt_sales_state
            WHERE marketplace_id = ?
            """,
            (marketplace_id,)
        ).fetchone()
        
        if row:
            return {
                "marketplace_id": row["marketplace_id"],
                "last_ingested_end_utc": row["last_ingested_end_utc"],
                "last_daily_audit_utc": row["last_daily_audit_utc"],
                "last_weekly_audit_utc": row["last_weekly_audit_utc"],
            }
        return {
            "marketplace_id": marketplace_id,
            "last_ingested_end_utc": None,
            "last_daily_audit_utc": None,
            "last_weekly_audit_utc": None,
        }
    except Exception as exc:
        logger.error(f"[DB] Failed to get vendor_rt_sales_state for {marketplace_id}: {exc}")
        raise
