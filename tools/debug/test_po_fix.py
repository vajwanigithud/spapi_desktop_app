#!/usr/bin/env python3
"""Manual debug script to inspect vendor_po_lines aggregation; not part of the main app."""

import sqlite3
from pathlib import Path

CATALOG_DB_PATH = Path(__file__).resolve().parents[2] / "catalog.db"

def test_po_lines():
    if not CATALOG_DB_PATH.exists():
        print(f"[ERROR] Database not found: {CATALOG_DB_PATH}")
        return
    
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        # Check table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vendor_po_lines'"
        )
        if not cursor.fetchone():
            print("[ERROR] vendor_po_lines table does not exist")
            return
        
        # Get all POs with line counts
        cursor = conn.execute("""
            SELECT
                po_number,
                COUNT(*) as line_count,
                COALESCE(SUM(ordered_qty), 0) as total_ordered,
                COALESCE(SUM(received_qty), 0) as total_received,
                COALESCE(SUM(shortage_qty), 0) as total_shortage,
                COALESCE(SUM(pending_qty), 0) as total_pending,
                MAX(last_changed_utc) as last_change
            FROM vendor_po_lines
            GROUP BY po_number
            ORDER BY MAX(last_changed_utc) DESC
        """)
        
        rows = cursor.fetchall()
        if not rows:
            print("[INFO] No PO lines found in database")
            return
        
        print(f"\n{'='*100}")
        print(f"{'PO_NUMBER':<15} {'LINES':<8} {'ORDERED':<10} {'RECEIVED':<10} {'SHORTAGE':<10} {'PENDING':<10} {'LAST_CHANGE':<20}")
        print(f"{'-'*100}")
        
        total_pos = 0
        total_lines = 0
        for row in rows:
            po_num = row[0]
            line_count = row[1]
            ordered = row[2]
            received = row[3]
            shortage = row[4]
            pending = row[5]
            last_change = row[6]
            
            print(f"{po_num:<15} {line_count:<8} {ordered:<10} {received:<10} {shortage:<10} {pending:<10} {last_change:<20}")
            total_pos += 1
            total_lines += line_count
        
        print(f"{'-'*100}")
        print(f"Total POs with lines: {total_pos}")
        print(f"Total line items: {total_lines}")
        print(f"{'='*100}\n")

if __name__ == "__main__":
    test_po_lines()
