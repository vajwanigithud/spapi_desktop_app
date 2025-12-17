import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from services import db as db_service
from services.json_cache import DEFAULT_VENDOR_POS_CACHE, load_vendor_pos_cache

LOGGER = logging.getLogger(__name__)
HEADER_TABLE = "vendor_po_header"
LINE_TABLE = "vendor_po_lines"
SYNC_TABLE = "vendor_po_sync_state"
SCHEMA_ENSURED = False


def ensure_vendor_po_schema() -> None:
    """
    Ensure all vendor PO tables exist with required columns/indexes.
    Safe to call repeatedly.
    """
    global SCHEMA_ENSURED
    if SCHEMA_ENSURED:
        return

    with db_service.get_db_connection() as conn:
        _ensure_header_table(conn)
        _ensure_line_table(conn)
        _ensure_sync_state_table(conn)
    SCHEMA_ENSURED = True


def _ensure_header_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {HEADER_TABLE} (
            po_number TEXT PRIMARY KEY,
            order_date TEXT,
            ship_to TEXT,
            ship_to_code TEXT,
            ship_to_city TEXT,
            ship_to_country TEXT,
            amazon_status TEXT,
            purchase_order_state TEXT,
            requested_qty INTEGER DEFAULT 0,
            accepted_qty INTEGER DEFAULT 0,
            received_qty INTEGER DEFAULT 0,
            cancelled_qty INTEGER DEFAULT 0,
            remaining_qty INTEGER DEFAULT 0,
            total_accepted_cost_amount REAL DEFAULT 0,
            total_accepted_cost_currency TEXT,
            po_items_count INTEGER DEFAULT 0,
            last_source TEXT,
            last_synced_at TEXT,
            last_changed_at TEXT,
            last_source_detail TEXT,
            raw_json TEXT
        )
        """
    )
    conn.commit()

    # Backwards-compatible migrations (if columns were added later)
    _ensure_column(conn, HEADER_TABLE, "ship_to_code", "TEXT")
    _ensure_column(conn, HEADER_TABLE, "ship_to_city", "TEXT")
    _ensure_column(conn, HEADER_TABLE, "ship_to_country", "TEXT")
    _ensure_column(conn, HEADER_TABLE, "last_source_detail", "TEXT")
    _ensure_column(conn, HEADER_TABLE, "raw_json", "TEXT")
    conn.commit()


def _ensure_line_table(conn: sqlite3.Connection) -> None:
    columns = _list_columns(conn, LINE_TABLE)
    if not columns:
        _create_line_table(conn)
        _ensure_line_indexes(conn)
        conn.commit()
        return

    # If legacy schema (id column) exists, rebuild table to enforce PK on po_number/item_sequence_number
    needs_rebuild = "item_sequence_number" not in columns or "id" in columns
    if needs_rebuild:
        _rebuild_vendor_po_lines(conn)
        return

    required_columns = {
        "asin": "TEXT",
        "vendor_sku": "TEXT",
        "barcode": "TEXT",
        "title": "TEXT",
        "image": "TEXT",
        "ordered_qty": "INTEGER DEFAULT 0",
        "accepted_qty": "INTEGER DEFAULT 0",
        "received_qty": "INTEGER DEFAULT 0",
        "cancelled_qty": "INTEGER DEFAULT 0",
        "pending_qty": "INTEGER DEFAULT 0",
        "shortage_qty": "INTEGER DEFAULT 0",
        "net_cost_amount": "REAL",
        "net_cost_currency": "TEXT",
        "list_price_amount": "REAL",
        "list_price_currency": "TEXT",
        "last_updated_at": "TEXT",
        "raw_json": "TEXT",
        "ship_to_location": "TEXT",
    }
    for column, ddl in required_columns.items():
        _ensure_column(conn, LINE_TABLE, column, ddl)

    _ensure_line_indexes(conn)
    conn.commit()


def _ensure_sync_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SYNC_TABLE} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            sync_in_progress INTEGER DEFAULT 0,
            sync_started_at TEXT,
            sync_finished_at TEXT,
            sync_last_ok_at TEXT,
            sync_last_error TEXT,
            last_sync_window_start TEXT,
            last_sync_window_end TEXT,
            lock_owner TEXT,
            lock_expires_at TEXT
        )
        """
    )
    conn.commit()
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {SYNC_TABLE}").fetchone()
    if not row or row["c"] == 0:
        conn.execute(
            f"""
            INSERT INTO {SYNC_TABLE} (id, sync_in_progress)
            VALUES (1, 0)
            """
        )
        conn.commit()


def bootstrap_headers_from_cache(cache_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Optional helper to import vendor_pos_cache.json into the DB header table.
    Invoked when header table is empty and cache exists.
    """
    ensure_vendor_po_schema()
    cache_path = cache_path or DEFAULT_VENDOR_POS_CACHE
    stats = {"imported": 0, "skipped": 0, "source": str(cache_path)}

    if not cache_path or not cache_path.exists():
        return stats

    try:
        with db_service.get_db_connection() as conn:
            row = conn.execute(f"SELECT COUNT(*) as c FROM {HEADER_TABLE}").fetchone()
            if row and row["c"]:
                stats["skipped"] = row["c"]
                return stats
    except Exception as exc:
        LOGGER.warning("[VendorPOStore] Failed to check header count: %s", exc)
        return stats

    try:
        data = load_vendor_pos_cache(cache_path, raise_on_error=True)
    except Exception as exc:
        LOGGER.warning("[VendorPOStore] Failed to load cache for bootstrap: %s", exc)
        return stats

    normalized = _normalize_pos_entries(data)
    if not normalized:
        return stats

    now_iso = _utc_now()
    upsert_rows: List[Tuple[Any, ...]] = []
    for po in normalized:
        po_number = (po.get("purchaseOrderNumber") or "").strip()
        if not po_number:
            continue
        header = _prepare_header_row(po, source="cache_bootstrap", synced_at=now_iso)
        upsert_rows.append(header)

    if not upsert_rows:
        return stats

    insert_sql = f"""
        INSERT INTO {HEADER_TABLE} (
            po_number, order_date, ship_to, ship_to_code, ship_to_city, ship_to_country,
            amazon_status, purchase_order_state,
            requested_qty, accepted_qty, received_qty, cancelled_qty, remaining_qty,
            total_accepted_cost_amount, total_accepted_cost_currency,
            po_items_count, last_source, last_source_detail, last_synced_at, last_changed_at, raw_json
        )
        VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(po_number) DO UPDATE SET
            order_date=excluded.order_date,
            ship_to=excluded.ship_to,
            ship_to_code=excluded.ship_to_code,
            ship_to_city=excluded.ship_to_city,
            ship_to_country=excluded.ship_to_country,
            amazon_status=excluded.amazon_status,
            purchase_order_state=excluded.purchase_order_state,
            requested_qty=excluded.requested_qty,
            accepted_qty=excluded.accepted_qty,
            received_qty=excluded.received_qty,
            cancelled_qty=excluded.cancelled_qty,
            remaining_qty=excluded.remaining_qty,
            total_accepted_cost_amount=excluded.total_accepted_cost_amount,
            total_accepted_cost_currency=excluded.total_accepted_cost_currency,
            po_items_count=excluded.po_items_count,
            last_source=excluded.last_source,
            last_source_detail=excluded.last_source_detail,
            last_synced_at=excluded.last_synced_at,
            last_changed_at=excluded.last_changed_at,
            raw_json=excluded.raw_json
    """

    try:
        db_service.execute_many_write(insert_sql, upsert_rows)
        stats["imported"] = len(upsert_rows)
    except Exception as exc:
        LOGGER.error("[VendorPOStore] Failed to bootstrap headers: %s", exc, exc_info=True)
    return stats


def upsert_vendor_po_headers(
    purchase_orders: Sequence[Dict[str, Any]],
    *,
    source: str,
    source_detail: Optional[str] = None,
    synced_at: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Upsert vendor PO headers into SQLite.
    """
    ensure_vendor_po_schema()
    synced_at = synced_at or _utc_now()
    upsert_rows: List[Tuple[Any, ...]] = []
    for po in purchase_orders or []:
        po_number = (po.get("purchaseOrderNumber") or "").strip()
        if not po_number:
            continue
        header = _prepare_header_row(po, source=source, synced_at=synced_at, source_detail=source_detail)
        upsert_rows.append(header)

    if not upsert_rows:
        return {"upserted": 0}

    insert_sql = f"""
        INSERT INTO {HEADER_TABLE} (
            po_number, order_date, ship_to, ship_to_code, ship_to_city, ship_to_country,
            amazon_status, purchase_order_state,
            requested_qty, accepted_qty, received_qty, cancelled_qty, remaining_qty,
            total_accepted_cost_amount, total_accepted_cost_currency,
            po_items_count, last_source, last_source_detail, last_synced_at, last_changed_at, raw_json
        )
        VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(po_number) DO UPDATE SET
            order_date=excluded.order_date,
            ship_to=excluded.ship_to,
            ship_to_code=excluded.ship_to_code,
            ship_to_city=excluded.ship_to_city,
            ship_to_country=excluded.ship_to_country,
            amazon_status=excluded.amazon_status,
            purchase_order_state=excluded.purchase_order_state,
            requested_qty=excluded.requested_qty,
            accepted_qty=excluded.accepted_qty,
            received_qty=excluded.received_qty,
            cancelled_qty=excluded.cancelled_qty,
            remaining_qty=excluded.remaining_qty,
            total_accepted_cost_amount=excluded.total_accepted_cost_amount,
            total_accepted_cost_currency=excluded.total_accepted_cost_currency,
            po_items_count=excluded.po_items_count,
            last_source=excluded.last_source,
            last_source_detail=excluded.last_source_detail,
            last_synced_at=excluded.last_synced_at,
            last_changed_at=excluded.last_changed_at,
            raw_json=excluded.raw_json
    """
    db_service.execute_many_write(insert_sql, upsert_rows)
    return {"upserted": len(upsert_rows)}


def replace_vendor_po_lines(po_number: str, lines: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Replace all line items for a PO inside the vendor_po_lines table.
    """
    ensure_vendor_po_schema()
    if not po_number:
        return {"lines": 0}

    delete_sql = f"DELETE FROM {LINE_TABLE} WHERE po_number = ?"
    db_service.execute_write(delete_sql, (po_number,))

    if not lines:
        return {"lines": 0}

    insert_sql = f"""
        INSERT INTO {LINE_TABLE} (
            po_number,
            item_sequence_number,
            asin,
            vendor_sku,
            barcode,
            title,
            image,
            ordered_qty,
            accepted_qty,
            received_qty,
            cancelled_qty,
            pending_qty,
            shortage_qty,
            net_cost_amount,
            net_cost_currency,
            list_price_amount,
            list_price_currency,
            last_updated_at,
            raw_json,
            ship_to_location
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """
    rows: List[Tuple[Any, ...]] = []
    for line in lines:
        rows.append(
            (
                po_number,
                line.get("item_sequence_number") or "",
                line.get("asin") or "",
                line.get("vendor_sku") or "",
                line.get("barcode") or "",
                line.get("title") or "",
                line.get("image") or "",
                _to_int(line.get("ordered_qty")),
                _to_int(line.get("accepted_qty")),
                _to_int(line.get("received_qty")),
                _to_int(line.get("cancelled_qty")),
                _to_int(line.get("pending_qty")),
                _to_int(line.get("shortage_qty")),
                _to_float(line.get("net_cost_amount")),
                line.get("net_cost_currency") or "",
                _to_float(line.get("list_price_amount")),
                line.get("list_price_currency") or "",
                line.get("last_updated_at") or _utc_now(),
                json.dumps(line.get("raw") or line, ensure_ascii=False),
                line.get("ship_to_location") or "",
            )
        )

    db_service.execute_many_write(insert_sql, rows)
    return {"lines": len(rows)}


def get_vendor_po_list(
    *,
    created_after: Optional[str] = None,
    order_desc: bool = True,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return vendor PO headers hydrated with stored raw JSON payloads.
    """
    ensure_vendor_po_schema()
    clauses: List[str] = []
    params: List[Any] = []
    if created_after:
        clauses.append("order_date >= ?")
        params.append(created_after)

    order_clause = "ORDER BY order_date DESC" if order_desc else "ORDER BY order_date ASC"
    limit_clause = ""
    if limit is not None and limit > 0:
        limit_clause = " LIMIT ?"
        params.append(int(limit))

    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    sql = f"""
        SELECT *
        FROM {HEADER_TABLE}
        {where_clause}
        {order_clause}
        {limit_clause}
    """

    with db_service.get_db_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    result: List[Dict[str, Any]] = []
    for row in rows:
        payload = _hydrate_po_row(row)
        result.append(payload)
    return result


def get_vendor_po(po_number: str) -> Optional[Dict[str, Any]]:
    ensure_vendor_po_schema()
    if not po_number:
        return None
    with db_service.get_db_connection() as conn:
        row = conn.execute(f"SELECT * FROM {HEADER_TABLE} WHERE po_number = ?", (po_number,)).fetchone()
    if not row:
        return None
    return _hydrate_po_row(row)


def get_vendor_po_lines(po_number: str) -> List[Dict[str, Any]]:
    ensure_vendor_po_schema()
    if not po_number:
        return []
    with db_service.get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM {LINE_TABLE}
            WHERE po_number = ?
            ORDER BY item_sequence_number
            """,
            (po_number,),
        ).fetchall()
    results: List[Dict[str, Any]] = []
    for row in rows:
        results.append(dict(row))
    return results


def aggregate_line_totals(po_numbers: Iterable[str]) -> Dict[str, Dict[str, int]]:
    ensure_vendor_po_schema()
    po_numbers = [po for po in po_numbers if po]
    if not po_numbers:
        return {}
    placeholders = ",".join(["?"] * len(po_numbers))
    sql = f"""
        SELECT
            po_number,
            COALESCE(SUM(ordered_qty), 0) AS requested_qty,
            COALESCE(SUM(accepted_qty), 0) AS accepted_qty,
            COALESCE(SUM(received_qty), 0) AS received_qty,
            COALESCE(SUM(cancelled_qty), 0) AS cancelled_qty,
            COALESCE(SUM(pending_qty), 0) AS pending_qty
        FROM {LINE_TABLE}
        WHERE po_number IN ({placeholders})
        GROUP BY po_number
    """
    with db_service.get_db_connection() as conn:
        rows = conn.execute(sql, tuple(po_numbers)).fetchall()
    return {row["po_number"]: dict(row) for row in rows}


def get_vendor_po_line_totals_for_po(po_number: str) -> Dict[str, int]:
    totals = aggregate_line_totals([po_number])
    return totals.get(po_number, {})


def count_vendor_po_lines() -> int:
    ensure_vendor_po_schema()
    with db_service.get_db_connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {LINE_TABLE}").fetchone()
    return row["c"] if row else 0


def get_rejected_vendor_po_lines(po_numbers: Sequence[str]) -> List[Dict[str, Any]]:
    ensure_vendor_po_schema()
    po_numbers = [po for po in po_numbers if po]
    if not po_numbers:
        return []
    placeholders = ",".join(["?"] * len(po_numbers))
    sql = f"""
        SELECT po_number, asin, vendor_sku AS sku, ship_to_location, ordered_qty, cancelled_qty, accepted_qty
        FROM {LINE_TABLE}
        WHERE po_number IN ({placeholders})
          AND (
              COALESCE(cancelled_qty, 0) > 0
              OR COALESCE(accepted_qty, 0) < COALESCE(ordered_qty, 0)
          )
    """
    with db_service.get_db_connection() as conn:
        rows = conn.execute(sql, tuple(po_numbers)).fetchall()
    return [dict(row) for row in rows]


def update_header_totals_from_lines(
    po_number: str,
    totals: Dict[str, Any],
    *,
    last_changed_at: Optional[str] = None,
    total_cost: Optional[float] = None,
    cost_currency: Optional[str] = None,
) -> None:
    ensure_vendor_po_schema()
    if not po_number:
        return
    remaining_qty = max(0, _to_int(totals.get("accepted_qty")) - _to_int(totals.get("received_qty")) - _to_int(totals.get("cancelled_qty")))
    sql = f"""
        UPDATE {HEADER_TABLE}
        SET requested_qty = ?,
            accepted_qty = ?,
            received_qty = ?,
            cancelled_qty = ?,
            remaining_qty = ?,
            last_changed_at = COALESCE(?, last_changed_at),
            total_accepted_cost_amount = COALESCE(?, total_accepted_cost_amount),
            total_accepted_cost_currency = COALESCE(?, total_accepted_cost_currency)
        WHERE po_number = ?
    """
    params = (
        _to_int(totals.get("requested_qty")),
        _to_int(totals.get("accepted_qty")),
        _to_int(totals.get("received_qty")),
        _to_int(totals.get("cancelled_qty")),
        remaining_qty,
        last_changed_at,
        total_cost,
        cost_currency,
        po_number,
    )
    db_service.execute_write(sql, params)



def update_header_raw_payload(
    po_number: str,
    payload: Dict[str, Any],
    *,
    source: str,
    source_detail: Optional[str] = None,
    synced_at: Optional[str] = None,
) -> None:
    """
    Update stored raw JSON payload without overwriting totals.
    """
    ensure_vendor_po_schema()
    if not po_number:
        return
    synced_at = synced_at or _utc_now()
    po_items = len(payload.get("orderDetails", {}).get("items") or [])
    order_date = _extract_order_date(payload)
    ship_to, code, city, country = _extract_ship_to(payload)
    amazon_status = payload.get("purchaseOrderState") or payload.get("amazonStatus") or ""
    raw_json = json.dumps(payload, ensure_ascii=False)
    sql = f"""
        UPDATE {HEADER_TABLE}
        SET raw_json = ?,
            po_items_count = ?,
            last_source = ?,
            last_source_detail = ?,
            last_synced_at = ?,
            order_date = COALESCE(?, order_date),
            ship_to = COALESCE(?, ship_to),
            ship_to_code = COALESCE(?, ship_to_code),
            ship_to_city = COALESCE(?, ship_to_city),
            ship_to_country = COALESCE(?, ship_to_country),
            amazon_status = COALESCE(?, amazon_status),
            purchase_order_state = COALESCE(?, purchase_order_state)
        WHERE po_number = ?
    """
    params = (
        raw_json,
        po_items,
        source,
        source_detail or "",
        synced_at,
        order_date,
        ship_to,
        code,
        city,
        country,
        amazon_status,
        amazon_status,
        po_number,
    )
    db_service.execute_write(sql, params)


def get_vendor_pos_by_numbers(po_numbers: Sequence[str]) -> List[Dict[str, Any]]:
    ensure_vendor_po_schema()
    po_numbers = [po for po in po_numbers if po]
    if not po_numbers:
        return []
    placeholders = ",".join(["?"] * len(po_numbers))
    sql = f"SELECT * FROM {HEADER_TABLE} WHERE po_number IN ({placeholders})"
    with db_service.get_db_connection() as conn:
        rows = conn.execute(sql, tuple(po_numbers)).fetchall()
    return [_hydrate_po_row(row) for row in rows]


def export_vendor_pos_snapshot() -> Dict[str, Any]:
    """
    Return a JSON-serializable snapshot of all stored POs.
    """
    ensure_vendor_po_schema()
    with db_service.get_db_connection() as conn:
        rows = conn.execute(f"SELECT po_number, raw_json FROM {HEADER_TABLE} ORDER BY order_date DESC").fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        if row["raw_json"]:
            try:
                payload = json.loads(row["raw_json"])
            except Exception:
                payload = {"purchaseOrderNumber": row["po_number"]}
        else:
            payload = {"purchaseOrderNumber": row["po_number"]}
        items.append(payload)
    return {"items": items, "exported_at": _utc_now(), "source": "db"}


def get_vendor_po_sync_state() -> Dict[str, Any]:
    ensure_vendor_po_schema()
    with db_service.get_db_connection() as conn:
        row = conn.execute(f"SELECT * FROM {SYNC_TABLE} WHERE id = 1").fetchone()
    if not row:
        return {
            "sync_in_progress": False,
            "sync_started_at": None,
            "sync_finished_at": None,
            "sync_last_ok_at": None,
            "sync_last_error": None,
            "last_sync_window_start": None,
            "last_sync_window_end": None,
            "lock_owner": None,
            "lock_expires_at": None,
        }
    return dict(row)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _hydrate_po_row(row: sqlite3.Row) -> Dict[str, Any]:
    payload_raw = row["raw_json"]
    if payload_raw:
        try:
            po = json.loads(payload_raw)
        except Exception:
            po = {"purchaseOrderNumber": row["po_number"]}
    else:
        po = {"purchaseOrderNumber": row["po_number"]}

    po["purchaseOrderNumber"] = row["po_number"]
    po["purchaseOrderDate"] = po.get("purchaseOrderDate") or row["order_date"]
    po["requestedQty"] = row["requested_qty"]
    po["acceptedQty"] = row["accepted_qty"]
    po["receivedQty"] = row["received_qty"]
    po["remainingQty"] = row["remaining_qty"]
    po["cancelledQty"] = row["cancelled_qty"]
    po["total_accepted_cost"] = row["total_accepted_cost_amount"]
    po["total_accepted_cost_currency"] = row["total_accepted_cost_currency"]
    po["totalAcceptedCostAmount"] = str(row["total_accepted_cost_amount"])
    po["totalAcceptedCostCurrency"] = row["total_accepted_cost_currency"] or "AED"
    po["poItemsCount"] = row["po_items_count"]
    po["amazonStatus"] = row["amazon_status"]
    po["_shipToCode"] = row["ship_to_code"]
    po["_shipToText"] = row["ship_to"]
    po["shipToCode"] = row["ship_to_code"]
    po["shipToText"] = row["ship_to"]
    po["_source"] = row["last_source"]
    po["_syncAt"] = row["last_synced_at"]
    po["_shipTo"] = {
        "code": row["ship_to_code"],
        "city": row["ship_to_city"],
        "country": row["ship_to_country"],
        "text": row["ship_to"],
    }
    return po


def _prepare_header_row(
    po: Dict[str, Any],
    *,
    source: str,
    synced_at: str,
    source_detail: Optional[str] = None,
) -> Tuple[Any, ...]:
    po_number = (po.get("purchaseOrderNumber") or "").strip()
    order_date = _extract_order_date(po)
    ship_to, ship_to_code, ship_to_city, ship_to_country = _extract_ship_to(po)
    amazon_status = po.get("purchaseOrderState") or po.get("amazonStatus") or ""

    requested = _to_int(po.get("requestedQty") or po.get("total_ordered_qty"))
    accepted = _to_int(po.get("acceptedQty") or po.get("total_accepted_qty"))
    received = _to_int(po.get("receivedQty") or po.get("total_received_qty"))
    cancelled = _to_int(po.get("cancelledQty") or po.get("total_cancelled_qty"))
    remaining = _to_int(po.get("remainingQty") or po.get("total_pending_qty"))
    if remaining <= 0 and accepted:
        remaining = max(0, accepted - received - cancelled)

    total_cost = _to_float(
        po.get("total_accepted_cost")
        or po.get("totalAcceptedCostAmount")
        or po.get("totalAcceptedCost")
    )
    total_currency = (
        po.get("total_accepted_cost_currency")
        or po.get("totalAcceptedCostCurrency")
        or "AED"
    )
    po_items = len(po.get("orderDetails", {}).get("items") or [])
    last_changed_at = (
        po.get("lastUpdatedDate")
        or po.get("orderDetails", {}).get("lastUpdatedDate")
        or order_date
    )
    raw_json = json.dumps(po, ensure_ascii=False)

    return (
        po_number,
        order_date,
        ship_to,
        ship_to_code,
        ship_to_city,
        ship_to_country,
        amazon_status,
        po.get("purchaseOrderState") or amazon_status,
        requested,
        accepted,
        received,
        cancelled,
        remaining,
        total_cost,
        total_currency,
        po_items,
        source,
        source_detail or "",
        synced_at,
        last_changed_at,
        raw_json,
    )


def _create_line_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LINE_TABLE} (
            po_number TEXT NOT NULL,
            item_sequence_number TEXT NOT NULL,
            asin TEXT,
            vendor_sku TEXT,
            barcode TEXT,
            title TEXT,
            image TEXT,
            ordered_qty INTEGER DEFAULT 0,
            accepted_qty INTEGER DEFAULT 0,
            received_qty INTEGER DEFAULT 0,
            cancelled_qty INTEGER DEFAULT 0,
            pending_qty INTEGER DEFAULT 0,
            shortage_qty INTEGER DEFAULT 0,
            net_cost_amount REAL,
            net_cost_currency TEXT,
            list_price_amount REAL,
            list_price_currency TEXT,
            last_updated_at TEXT,
            raw_json TEXT,
            ship_to_location TEXT,
            PRIMARY KEY (po_number, item_sequence_number)
        )
        """
    )


def _rebuild_vendor_po_lines(conn: sqlite3.Connection) -> None:
    legacy_table = f"{LINE_TABLE}_legacy_{int(time.time())}"
    conn.execute(f"ALTER TABLE {LINE_TABLE} RENAME TO {legacy_table}")
    _create_line_table(conn)
    # Attempt to copy overlapping columns
    columns = {
        "po_number": "po_number",
        "item_sequence_number": "item_sequence_number",
        "asin": "asin",
        "vendor_sku": "vendor_sku",
        "ordered_qty": "ordered_qty",
        "accepted_qty": "accepted_qty",
        "cancelled_qty": "cancelled_qty",
        "received_qty": "received_qty",
        "pending_qty": "pending_qty",
        "shortage_qty": "shortage_qty",
        "last_updated_at": "last_updated_at",
        "ship_to_location": "ship_to_location",
    }

    select_cols = []
    for new_col, legacy_col in columns.items():
        if _column_exists(conn, legacy_table, legacy_col):
            select_cols.append(legacy_col)
        else:
            if new_col == "item_sequence_number":
                select_cols.append("'' AS item_sequence_number")
            else:
                select_cols.append(f"NULL AS {new_col}")

    insert_sql = f"""
        INSERT INTO {LINE_TABLE} (
            po_number,
            item_sequence_number,
            asin,
            vendor_sku,
            barcode,
            title,
            image,
            ordered_qty,
            accepted_qty,
            received_qty,
            cancelled_qty,
            pending_qty,
            shortage_qty,
            net_cost_amount,
            net_cost_currency,
            list_price_amount,
            list_price_currency,
            last_updated_at,
            raw_json,
            ship_to_location
        )
        SELECT
            po_number,
            COALESCE(item_sequence_number, ''),
            asin,
            COALESCE(vendor_sku, sku, ''),
            '' AS barcode,
            '' AS title,
            '' AS image,
            ordered_qty,
            accepted_qty,
            received_qty,
            cancelled_qty,
            pending_qty,
            shortage_qty,
            NULL AS net_cost_amount,
            NULL AS net_cost_currency,
            NULL AS list_price_amount,
            NULL AS list_price_currency,
            last_changed_utc AS last_updated_at,
            NULL AS raw_json,
            ship_to_location
        FROM {legacy_table}
    """
    conn.execute(insert_sql)
    conn.execute(f"DROP TABLE {legacy_table}")
    _ensure_line_indexes(conn)
    conn.commit()


def _ensure_line_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{LINE_TABLE}_po_number ON {LINE_TABLE}(po_number)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{LINE_TABLE}_asin ON {LINE_TABLE}(asin)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{LINE_TABLE}_vendor_sku ON {LINE_TABLE}(vendor_sku)"
    )


def _list_columns(conn: sqlite3.Connection, table: str) -> Dict[str, sqlite3.Row]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["name"]: row for row in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = _list_columns(conn, table)
    if column in columns:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as exc:
        LOGGER.debug("[VendorPOStore] Could not add column %s.%s: %s", table, column, exc)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in _list_columns(conn, table)


def _normalize_pos_entries(data: Any) -> List[Dict[str, Any]]:
    items_raw: List[Any] = []
    if isinstance(data, dict) and "items" in data:
        items_raw = data.get("items") or []
    elif isinstance(data, list):
        items_raw = data

    normalized: List[Dict[str, Any]] = []
    for entry in items_raw:
        if isinstance(entry, dict) and "raw" in entry and isinstance(entry["raw"], dict):
            normalized.append(entry["raw"])
        elif isinstance(entry, dict):
            normalized.append(entry)
    return normalized


def _extract_order_date(po: Dict[str, Any]) -> Optional[str]:
    date_str = po.get("purchaseOrderDate") or po.get("orderDetails", {}).get("purchaseOrderDate")
    if not date_str:
        return None
    if isinstance(date_str, str) and date_str.endswith("Z"):
        return date_str
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return date_str


def _extract_ship_to(po: Dict[str, Any]) -> Tuple[str, str, str, str]:
    details = po.get("orderDetails", {})
    ship_to_party = details.get("shipToParty", {}) if isinstance(details, dict) else {}
    if not isinstance(ship_to_party, dict):
        return "", "", "", ""
    code = (ship_to_party.get("partyId") or "").strip()
    address = ship_to_party.get("address") or {}
    if not isinstance(address, dict):
        address = {}
    city = (address.get("city") or "").strip()
    country = (address.get("country") or "").strip()
    text = code
    if city or country:
        text = f"{code} – {city}, {country}".strip(" –,")
    return text, code, city, country


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(value))
    except Exception:
        return 0


def _to_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0
