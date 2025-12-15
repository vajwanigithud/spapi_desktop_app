"""Manual debug script for verifying PO status totals; not part of the main app."""

import json
from typing import Dict, List

from main import VENDOR_POS_CACHE, fetch_po_status_totals
from services.db import get_db_connection


def load_po_numbers() -> List[str]:
    if not VENDOR_POS_CACHE.exists():
        print("vendor_pos_cache.json not found; nothing to verify")
        return []
    try:
        data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read vendor_pos_cache.json: {e}")
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        print("vendor_pos_cache.json missing items array")
        return []
    po_numbers = []
    for po in items:
        if isinstance(po, dict) and po.get("purchaseOrderNumber"):
            po_numbers.append(po["purchaseOrderNumber"])
    return po_numbers


def get_db_totals(po_number: str) -> Dict[str, int]:
    sql = """
    SELECT
        COALESCE(SUM(ordered_qty), 0) AS ordered,
        COALESCE(SUM(accepted_qty), 0) AS accepted,
        COALESCE(SUM(cancelled_qty), 0) AS cancelled,
        COALESCE(SUM(received_qty), 0) AS received,
        COALESCE(SUM(pending_qty), 0) AS pending,
        COALESCE(SUM(shortage_qty), 0) AS shortage
    FROM vendor_po_lines
    WHERE po_number = ?
    """
    with get_db_connection() as conn:
        row = conn.execute(sql, (po_number,)).fetchone()
        if not row:
            return {"ordered": 0, "accepted": 0, "cancelled": 0, "received": 0, "pending": 0, "shortage": 0}
        return {k: row[k] for k in row.keys()}


def verify_po(po_number: str) -> Dict[str, str]:
    api_totals = fetch_po_status_totals(po_number)
    db_totals = get_db_totals(po_number)

    mismatches = []
    api_received = api_totals.get("total_received_qty", 0)
    api_pending = api_totals.get("total_pending_qty", 0)
    if api_received != db_totals.get("received"):
        mismatches.append(f"received api={api_received} db={db_totals.get('received')}")
    if api_pending != db_totals.get("pending"):
        mismatches.append(f"pending api={api_pending} db={db_totals.get('pending')}")
    return {
        "po": po_number,
        "status": "OK" if not mismatches else "; ".join(mismatches),
    }


def main():
    po_numbers = load_po_numbers()
    if not po_numbers:
        return
    results = []
    for po in po_numbers:
        try:
            results.append(verify_po(po))
        except Exception as e:
            results.append({"po": po, "status": f"error: {e}"})
    failures = [r for r in results if r["status"] != "OK"]
    for r in results:
        print(f"{r['po']}: {r['status']}")
    print("\nSummary: {} OK, {} mismatches".format(len(results) - len(failures), len(failures)))


if __name__ == "__main__":
    main()
