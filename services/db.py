import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Optional

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
def get_db_connection_for_path(db_path: Path):
    """Return a SQLite connection for the given path, reusing the default setup when possible.

    Uses the hardened default connection (WAL, timeout, row_factory) when the caller requests
    the main catalog DB path; otherwise opens a scoped connection for the provided path.
    """
    resolved = Path(db_path).resolve()
    default = Path(CATALOG_DB_PATH).resolve()
    if resolved == default:
        with get_db_connection() as conn:
            yield conn
    else:
        conn = None
        try:
            conn = sqlite3.connect(resolved, timeout=_db_timeout)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            if conn:
                conn.close()

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


def ensure_oos_export_history_table():
    """
    Ensure the vendor_oos_export_history table exists.
    Tracks which ASINs have been exported from the Out-of-Stock list.
    """
    sql = """
    CREATE TABLE IF NOT EXISTS vendor_oos_export_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asin TEXT NOT NULL,
        marketplace_id TEXT NOT NULL DEFAULT 'A2VIGQ35RCS4UG',
        exported_at TEXT NOT NULL,
        export_batch_id TEXT NOT NULL,
        notes TEXT,
        UNIQUE(asin, marketplace_id)
    )
    """
    try:
        execute_write(sql)
        execute_write("CREATE INDEX IF NOT EXISTS idx_oos_export_asin_mkt ON vendor_oos_export_history(asin, marketplace_id)")
        logger.info("[DB] vendor_oos_export_history table ensured")
    except Exception as exc:
        logger.error(f"[DB] Failed to ensure vendor_oos_export_history table: {exc}", exc_info=True)
        raise


def mark_oos_asins_exported(asins: list[str], batch_id: str, marketplace_id: str = "A2VIGQ35RCS4UG"):
    """
    Mark a list of ASINs as exported.
    
    Args:
        asins: List of ASIN strings to mark as exported
        batch_id: UUID or ID to group exports from same batch
        marketplace_id: Marketplace ID (defaults to primary US marketplace)
    
    Returns:
        Count of successfully inserted records
    """
    if not asins:
        return 0
    
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc).isoformat()
    
    inserted = 0
    for asin in asins:
        try:
            sql = """
            INSERT OR IGNORE INTO vendor_oos_export_history
            (asin, marketplace_id, exported_at, export_batch_id)
            VALUES (?, ?, ?, ?)
            """
            execute_write(sql, (asin, marketplace_id, now_utc, batch_id))
            inserted += 1
        except Exception as exc:
            logger.warning(f"[DB] Failed to mark ASIN {asin} as exported: {exc}")
    
    return inserted


def ensure_vendor_inventory_table() -> None:
    """
    Create vendor_inventory_asin table if it does not exist.
    Stores weekly inventory snapshots per ASIN per marketplace.
    One row per ASIN per marketplace (latest seen snapshot per design).
    """
    sql = """
    CREATE TABLE IF NOT EXISTS vendor_inventory_asin (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        marketplace_id TEXT NOT NULL,
        asin TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        
        -- Core "what is Amazon holding" metrics
        sellable_onhand_units INTEGER NOT NULL,
        sellable_onhand_cost REAL NOT NULL,
        unsellable_onhand_units INTEGER,
        unsellable_onhand_cost REAL,
        
        -- Aging + unhealthy
        aged90plus_sellable_units INTEGER,
        aged90plus_sellable_cost REAL,
        unhealthy_units INTEGER,
        unhealthy_cost REAL,
        
        -- Flow-related metrics (helpful later for velocity logic)
        net_received_units INTEGER,
        net_received_cost REAL,
        open_po_units INTEGER,
        unfilled_customer_ordered_units INTEGER,
        vendor_confirmation_rate REAL,
        sell_through_rate REAL,
        
        updated_at TEXT NOT NULL
    )
    """
    try:
        execute_write(sql)
        # Recreate uniqueness on (marketplace_id, asin) only so historical ASINs persist
        execute_write("DROP INDEX IF EXISTS idx_vendor_inventory_unique")
        index_sql = """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_inventory_unique
        ON vendor_inventory_asin (marketplace_id, asin)
        """
        execute_write(index_sql)
        
        logger.info("[DB] vendor_inventory_asin table ensured")
    except Exception as exc:
        logger.error(f"[DB] Failed to ensure vendor_inventory_asin table: {exc}", exc_info=True)
        raise


def replace_vendor_inventory_snapshot(conn, marketplace_id: str, rows: list[dict]) -> dict[str, Any]:
    """
    Upsert vendor_inventory_asin rows for the given marketplace_id and prune stale ASINs
    when safe. Returns prune metadata with keys:
    - prune_attempted: bool
    - prune_skipped_reason: "" | "empty_snapshot" | "below_threshold"
    - prune_min_keep_count: int
    - pruned_rows: int
    - prune_kept_count: int
    - prune_before_count: int | None
    """
    try:
        try:
            raw_min_keep = os.getenv("INVENTORY_RT_PRUNE_MIN_KEEP")
            raw_min_keep = raw_min_keep.strip() if isinstance(raw_min_keep, str) else raw_min_keep
            env_min_keep = int(raw_min_keep) if raw_min_keep not in (None, "") else 20
        except Exception:
            env_min_keep = 20
        prune_min_keep_count = max(env_min_keep, 0)

        columns = [
            "marketplace_id", "asin", "start_date", "end_date",
            "sellable_onhand_units", "sellable_onhand_cost",
            "unsellable_onhand_units", "unsellable_onhand_cost",
            "aged90plus_sellable_units", "aged90plus_sellable_cost",
            "unhealthy_units", "unhealthy_cost",
            "net_received_units", "net_received_cost",
            "open_po_units", "unfilled_customer_ordered_units",
            "vendor_confirmation_rate", "sell_through_rate",
            "updated_at"
        ]

        if rows:
            placeholders = ", ".join(["?" for _ in columns])
            update_columns = [col for col in columns if col not in ("marketplace_id", "asin")]
            update_clause = ", ".join([f"{col} = excluded.{col}" for col in update_columns])
            insert_sql = f"""
            INSERT INTO vendor_inventory_asin ({', '.join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(marketplace_id, asin) DO UPDATE SET {update_clause}
            """

            values = [tuple(row.get(col) for col in columns) for row in rows]
            conn.executemany(insert_sql, values)
        else:
            logger.info(f"[DB] No vendor_inventory_asin rows to upsert for {marketplace_id}")

        normalized_asins = {
            (row.get("asin") or "").strip().upper()
            for row in rows or []
            if (row.get("asin") or "").strip()
        }
        asins_in_snapshot = sorted(normalized_asins)
        prune_kept_count = len(asins_in_snapshot)

        prune_attempted = False
        prune_skipped_reason = ""
        pruned_rows = 0

        prune_before_count = conn.execute(
            "SELECT COUNT(*) FROM vendor_inventory_asin WHERE marketplace_id = ?",
            (marketplace_id,),
        ).fetchone()[0]

        if prune_kept_count == 0:
            prune_skipped_reason = "empty_snapshot"
            logger.warning(
                f"[DB] Skipping prune for vendor_inventory_asin {marketplace_id}: empty snapshot ASIN set (preventing destructive delete). kept={prune_kept_count} existing_before={prune_before_count} min_keep={prune_min_keep_count}"
            )
        elif prune_kept_count < prune_min_keep_count:
            prune_skipped_reason = "below_threshold"
            logger.warning(
                f"[DB] Skipping prune for vendor_inventory_asin {marketplace_id}: kept={prune_kept_count} below min_keep={prune_min_keep_count}; existing_before={prune_before_count}; prune skipped (threshold)"
            )
        else:
            prune_attempted = True
            chunk_size = 500

            rowcounts: list[int] = []
            if len(asins_in_snapshot) <= chunk_size:
                placeholders = ", ".join("?" for _ in asins_in_snapshot)
                delete_sql = (
                    "DELETE FROM vendor_inventory_asin "
                    "WHERE marketplace_id = ? AND asin NOT IN (" + placeholders + ")"
                )
                cur = conn.execute(delete_sql, [marketplace_id, *asins_in_snapshot])
                rowcounts.append(cur.rowcount)
            else:
                rows_existing = conn.execute(
                    "SELECT asin FROM vendor_inventory_asin WHERE marketplace_id = ?",
                    (marketplace_id,),
                ).fetchall()
                existing_asins = []
                for r in rows_existing:
                    asin_val = r["asin"] if hasattr(r, "keys") else r[0]
                    if asin_val:
                        existing_asins.append(asin_val)
                keep_set = set(asins_in_snapshot)
                stale_asins = [
                    asin for asin in existing_asins if asin and asin.strip().upper() not in keep_set
                ]

                for idx in range(0, len(stale_asins), chunk_size):
                    chunk = stale_asins[idx : idx + chunk_size]
                    placeholders = ", ".join("?" for _ in chunk)
                    delete_sql = (
                        "DELETE FROM vendor_inventory_asin "
                        "WHERE marketplace_id = ? AND asin IN (" + placeholders + ")"
                    )
                    cur = conn.execute(delete_sql, [marketplace_id, *chunk])
                    rowcounts.append(cur.rowcount)

            if rowcounts and all(rc is not None and rc >= 0 for rc in rowcounts):
                pruned_rows = sum(rowcounts)
            else:
                after_count = conn.execute(
                    "SELECT COUNT(*) FROM vendor_inventory_asin WHERE marketplace_id = ?",
                    (marketplace_id,),
                ).fetchone()[0]
                pruned_rows = max(prune_before_count - after_count, 0)

            if pruned_rows > 0:
                logger.info(
                    f"[DB] Pruned stale vendor_inventory_asin rows for {marketplace_id}: {pruned_rows} removed (kept {prune_kept_count})"
                )
            else:
                logger.info(
                    f"[DB] No stale vendor_inventory_asin rows to prune for {marketplace_id} (kept {prune_kept_count})"
                )

        conn.commit()

        logger.info(f"[DB] Upserted vendor_inventory_asin rows for {marketplace_id}: {len(rows)} rows")

        return {
            "prune_attempted": bool(prune_attempted),
            "prune_skipped_reason": str(prune_skipped_reason or ""),
            "prune_min_keep_count": int(prune_min_keep_count or 0),
            "pruned_rows": int(pruned_rows or 0),
            "prune_kept_count": int(prune_kept_count or 0),
            "prune_before_count": int(prune_before_count or 0),
        }
    except Exception as exc:
        logger.error(f"[DB] Failed to upsert vendor_inventory_asin snapshot: {exc}", exc_info=True)
        raise


def get_vendor_inventory_snapshot(conn, marketplace_id: str) -> list[dict]:
    """
    Returns all rows from vendor_inventory_asin for the given marketplace_id.
    
    Args:
        conn: SQLite connection object
        marketplace_id: The marketplace ID
    
    Returns:
        List of dicts representing inventory snapshot rows
    """
    try:
        rows = conn.execute(
            "SELECT * FROM vendor_inventory_asin WHERE marketplace_id = ? ORDER BY asin ASC",
            (marketplace_id,)
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:
        logger.error(f"[DB] Failed to get vendor_inventory_asin snapshot for {marketplace_id}: {exc}", exc_info=True)
        raise


def ensure_app_kv_table() -> None:
    """
    Ensure the app_kv_store table exists.
    Simple key/value store for app-wide settings like last daily audit date.
    """
    sql = """
    CREATE TABLE IF NOT EXISTS app_kv_store (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """
    try:
        execute_write(sql)
        logger.info("[DB] app_kv_store table ensured")
    except Exception as exc:
        logger.error(f"[DB] Failed to ensure app_kv_store table: {exc}", exc_info=True)
        raise


def get_app_kv(conn, key: str) -> Optional[str]:
    """
    Get a value from app_kv_store by key.
    
    Args:
        conn: SQLite connection object
        key: The key to retrieve
    
    Returns:
        The value as a string, or None if key not found
    """
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_kv_store WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.error(f"[DB] Failed to get_app_kv for key '{key}': {exc}")
        raise


def set_app_kv(conn, key: str, value: str) -> None:
    """
    Set a value in app_kv_store by key (insert or update).
    
    Args:
        conn: SQLite connection object
        key: The key to set
        value: The value to store
    """
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO app_kv_store (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
    except Exception as exc:
        logger.error(f"[DB] Failed to set_app_kv for key '{key}': {exc}")
        raise


def ensure_df_payments_tables(db_path: Path = CATALOG_DB_PATH) -> None:
    """Ensure DF Payments tables exist for the given database path."""
    orders_sql = """
    CREATE TABLE IF NOT EXISTS df_payments_orders (
        marketplace_id TEXT NOT NULL,
        purchase_order_number TEXT NOT NULL,
        customer_order_number TEXT,
        order_date_utc TEXT,
        order_status TEXT,
        items_count INTEGER,
        total_units INTEGER,
        subtotal_amount REAL,
        vat_amount REAL,
        currency_code TEXT,
        sku_list TEXT,
        last_updated_utc TEXT,
        PRIMARY KEY (marketplace_id, purchase_order_number)
    )
    """

    state_sql = """
    CREATE TABLE IF NOT EXISTS df_payments_state (
        marketplace_id TEXT PRIMARY KEY,
        last_fetch_started_at TEXT,
        last_fetch_finished_at TEXT,
        last_fetch_status TEXT,
        last_error TEXT,
        last_lookback_days INTEGER,
        rows_90d INTEGER,
        pages_fetched INTEGER,
        fetched_orders_total INTEGER,
        unique_po_total INTEGER,
        rows_in_db_window INTEGER,
        limit_used INTEGER,
        last_incremental_started_at TEXT,
        last_incremental_finished_at TEXT,
        last_incremental_status TEXT,
        last_incremental_error TEXT,
        last_seen_order_date_utc TEXT,
        last_incremental_orders_upserted INTEGER,
        last_incremental_pages_fetched INTEGER
    )
    """

    index_sql = """
    CREATE INDEX IF NOT EXISTS idx_df_payments_order_date
    ON df_payments_orders (marketplace_id, order_date_utc)
    """

    with _db_write_lock:
        with get_db_connection_for_path(db_path) as conn:
            conn.execute(orders_sql)
            conn.execute(state_sql)
            conn.execute(index_sql)
            try:
                conn.execute("ALTER TABLE df_payments_state ADD COLUMN pages_fetched INTEGER")
            except Exception:
                pass
            for col in (
                "fetched_orders_total INTEGER",
                "unique_po_total INTEGER",
                "rows_in_db_window INTEGER",
                "limit_used INTEGER",
                "last_incremental_started_at TEXT",
                "last_incremental_finished_at TEXT",
                "last_incremental_status TEXT",
                "last_incremental_error TEXT",
                "last_seen_order_date_utc TEXT",
                "last_incremental_orders_upserted INTEGER",
                "last_incremental_pages_fetched INTEGER",
            ):
                try:
                    conn.execute(f"ALTER TABLE df_payments_state ADD COLUMN {col}")
                except Exception:
                    pass
            conn.commit()


def get_exported_asins(marketplace_id: str = "A2VIGQ35RCS4UG") -> set[str]:
    """
    Get all ASINs that have been exported for a marketplace.
    
    Args:
        marketplace_id: Marketplace ID
    
    Returns:
        Set of ASIN strings
    """
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT asin FROM vendor_oos_export_history WHERE marketplace_id = ?",
                (marketplace_id,)
            ).fetchall()
            return {row["asin"] for row in rows}
    except Exception as exc:
        logger.error(f"[DB] Failed to get exported ASINs for {marketplace_id}: {exc}")
        return set()


def is_asin_exported(asin: str, marketplace_id: str = "A2VIGQ35RCS4UG") -> bool:
    """
    Check if a single ASIN has been exported.
    
    Args:
        asin: ASIN string
        marketplace_id: Marketplace ID
    
    Returns:
        True if exported, False otherwise
    """
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT id FROM vendor_oos_export_history WHERE asin = ? AND marketplace_id = ? LIMIT 1",
                (asin, marketplace_id)
            ).fetchone()
            return row is not None
    except Exception as exc:
        logger.error(f"[DB] Failed to check export status for {asin}: {exc}")
        return False
