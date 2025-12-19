import os
import sqlite3

import pytest

from services import vendor_rt_sales_ledger as ledger


# Ensure env defaults used by other modules do not break imports in isolation.
os.environ.setdefault("MARKETPLACE_ID", "A2VIGQ35RCS4UG")


def _init_conn(tmp_path):
    db_path = tmp_path / "ledger_norm.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ledger.ensure_vendor_rt_sales_ledger_table(conn)
    return conn


def test_normalize_existing_rows_merges_and_preserves_metadata(tmp_path):
    conn = _init_conn(tmp_path)
    marketplace_id = "A1"

    rows = [
        (
            marketplace_id,
            "2025-01-01T12:10:00+00:00",
            ledger.STATUS_REQUESTED,
            "RPT-1",
            1,
            None,
            "2025-01-01T12:20:00+00:00",
            "2025-01-01T12:05:00+00:00",
            "2025-01-01T12:10:00+00:00",
        ),
        (
            marketplace_id,
            "2025-01-01T12:24:00+00:00",
            ledger.STATUS_APPLIED,
            None,
            3,
            None,
            None,
            "2025-01-01T12:00:00+00:00",
            "2025-01-01T12:30:00+00:00",
        ),
        (
            marketplace_id,
            "2025-01-01T13:00:00+00:00",
            ledger.STATUS_MISSING,
            None,
            0,
            None,
            None,
            "2025-01-01T13:00:00+00:00",
            "2025-01-01T13:00:00+00:00",
        ),
    ]
    conn.executemany(
        f"""
        INSERT INTO {ledger.LEDGER_TABLE} (
            marketplace_id,
            hour_utc,
            status,
            report_id,
            attempt_count,
            last_error,
            next_retry_utc,
            created_at_utc,
            updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    stats = ledger.normalize_existing_ledger_rows(conn, marketplaces=[marketplace_id])
    assert stats["rows_changed"] >= 2
    assert stats["collisions_merged"] == 1

    normalized_rows = conn.execute(
        f"""
        SELECT *
        FROM {ledger.LEDGER_TABLE}
        WHERE marketplace_id = ?
        ORDER BY hour_utc
        """,
        (marketplace_id,),
    ).fetchall()

    assert len(normalized_rows) == 2
    first_hour = normalized_rows[0]
    assert first_hour["hour_utc"] == "2025-01-01T12:00:00+00:00"
    assert first_hour["status"] == ledger.STATUS_APPLIED
    # Winner lacked report_id, so merged value should pull from REQUESTED entry
    assert first_hour["report_id"] == "RPT-1"
    # attempt_count should be the max across merged rows
    assert first_hour["attempt_count"] == 3
    # The untouched hour should remain in the table
    assert normalized_rows[1]["hour_utc"] == "2025-01-01T13:00:00+00:00"
    conn.close()


def test_normalize_existing_rows_is_idempotent(tmp_path):
    conn = _init_conn(tmp_path)
    marketplace_id = "A1"
    conn.execute(
        f"""
        INSERT INTO {ledger.LEDGER_TABLE} (
            marketplace_id,
            hour_utc,
            status,
            report_id,
            attempt_count,
            last_error,
            next_retry_utc,
            created_at_utc,
            updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            marketplace_id,
            "2025-01-01T01:30:00+00:00",
            ledger.STATUS_FAILED,
            "RPT-2",
            2,
            "boom",
            "2025-01-01T02:00:00+00:00",
            "2025-01-01T01:00:00+00:00",
            "2025-01-01T01:30:00+00:00",
        ),
    )
    conn.commit()

    first = ledger.normalize_existing_ledger_rows(conn, marketplaces=[marketplace_id])
    second = ledger.normalize_existing_ledger_rows(conn, marketplaces=[marketplace_id])

    assert first["rows_changed"] >= 1
    assert second["rows_changed"] == 0
    assert second["collisions_merged"] == 0

    remaining = conn.execute(
        f"SELECT hour_utc, status FROM {ledger.LEDGER_TABLE} WHERE marketplace_id = ?",
        (marketplace_id,),
    ).fetchone()
    assert remaining["hour_utc"] == "2025-01-01T01:00:00+00:00"
    assert remaining["status"] == ledger.STATUS_FAILED

    conn.close()
