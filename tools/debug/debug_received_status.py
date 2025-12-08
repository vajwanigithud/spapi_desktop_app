#!/usr/bin/env python3
"""Manual debug script to inspect vendor_po_lines for a PO; not part of the main app."""
import sqlite3
from pathlib import Path

db_path = Path("C:\\spapi_desktop_app\\catalog.db")

if db_path.exists():
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("""
            SELECT po_number, asin, sku, ordered_qty, acknowledged_qty, cancelled_qty, 
                   shipped_qty, received_qty, shortage_qty, pending_qty
            FROM vendor_po_lines
            WHERE po_number = '6RD2BEAD'
            ORDER BY asin
        """)
        
        rows = cursor.fetchall()
        print(f"Found {len(rows)} lines for PO 6RD2BEAD\n")
        
        total_ordered = 0
        total_received = 0
        total_shipped = 0
        
        for row in rows:
            po, asin, sku, ordered, ack, canc, shipped, received, shortage, pending = row
            print(f"ASIN: {asin:15s} | Ordered: {ordered:4d} | Received: {received:4d} | Shipped: {shipped:4d} | Shortage: {shortage:4d}")
            total_ordered += ordered
            total_received += received
            total_shipped += shipped
        
        print(f"\nTotals:")
        print(f"  Total Ordered:  {total_ordered}")
        print(f"  Total Received: {total_received}")
        print(f"  Total Shipped:  {total_shipped}")
        print(f"\nExpected (from Amazon dashboard):")
        print(f"  Ordered: 975")
        print(f"  Received: 40")
else:
    print(f"Database not found at {db_path}")
