"""Simple print job logging."""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from services.db import execute_write, get_db_connection

TABLE_NAME = "print_jobs"
logger = logging.getLogger(__name__)


def ensure_print_log_table() -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        printer TEXT NOT NULL,
        ean TEXT NOT NULL,
        sku TEXT NOT NULL,
        copies INTEGER NOT NULL,
        ok INTEGER NOT NULL,
        error TEXT
    )
    """
    execute_write(sql)


ensure_print_log_table()


def log_print_job(printer: str, ean: str, sku: str, copies: int, ok: bool, error: Optional[str] = None) -> Optional[int]:
    with get_db_connection() as conn:
        cur = conn.execute(
            f"INSERT INTO {TABLE_NAME} (created_at, printer, ean, sku, copies, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.utcnow().isoformat(),
                printer or "",
                ean or "",
                sku or "",
                copies,
                1 if ok else 0,
                error,
            ),
        )
        conn.commit()
        inserted_id = cur.lastrowid
        if inserted_id is None:
            alt = conn.execute("SELECT last_insert_rowid()").fetchone()
            inserted_id = alt[0] if alt and alt[0] is not None else None
    logger.info(
        "[print_log] inserted id=%s ok=%s printer=%s ean=%s sku=%s copies=%s",
        inserted_id,
        ok,
        printer,
        ean,
        sku,
        copies,
    )
    return inserted_id


def get_recent_print_jobs(limit: int = 20) -> List[Dict[str, object]]:
    limit = max(1, min(limit, 100))
    with get_db_connection() as conn:
        cursor = conn.execute(
            f"SELECT id, created_at, printer, ean, sku, copies, ok, error FROM {TABLE_NAME} ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
    result: List[Dict[str, object]] = []
    for row in rows:
        job = dict(row)
        ok_value = job.get("ok")
        job["ok"] = bool(ok_value) if ok_value is not None else False
        result.append(job)
    return result
