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
