#!/usr/bin/env python
"""Manual maintenance script to migrate vendor_po_lines schema; not part of the main app."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "catalog.db"

def migrate_vendor_po_lines_schema():
    """Migrate vendor_po_lines table from old to new schema."""
    if not DB_PATH.exists():
        print(f"[Migration] {DB_PATH} does not exist yet. Schema will be created on first sync.")
        return
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    try:
        # Check if table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vendor_po_lines'")
        if not cursor.fetchone():
            print("[Migration] vendor_po_lines table does not exist. Will be created on first sync.")
            return
        
        # Check columns
        cursor.execute("PRAGMA table_info(vendor_po_lines)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}  # name -> type
        
        if 'acknowledged_qty' in columns and 'accepted_qty' not in columns:
            print("[Migration] Found old schema with acknowledged_qty. Migrating...")
            
            # Rename old table
            cursor.execute("ALTER TABLE vendor_po_lines RENAME TO vendor_po_lines_old")
            print("[Migration] Renamed old table to vendor_po_lines_old")
            
            # Create new table with correct schema
            cursor.execute("""
            CREATE TABLE vendor_po_lines (
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
            """)
            print("[Migration] Created new vendor_po_lines table")
            
            # Copy data, mapping acknowledged_qty -> accepted_qty
            cursor.execute("""
            INSERT INTO vendor_po_lines
            (id, po_number, ship_to_location, asin, sku, ordered_qty, accepted_qty,
             cancelled_qty, shipped_qty, received_qty, shortage_qty, pending_qty, last_changed_utc)
            SELECT 
                id, po_number, ship_to_location, asin, sku, ordered_qty, acknowledged_qty,
                cancelled_qty, shipped_qty, received_qty, shortage_qty, pending_qty, last_changed_utc
            FROM vendor_po_lines_old
            """)
            print("[Migration] Copied data from old table")
            
            # Drop old table
            cursor.execute("DROP TABLE vendor_po_lines_old")
            print("[Migration] Dropped old table")
            
            conn.commit()
            print("[Migration] ✓ Migration completed successfully")
        
        elif 'accepted_qty' in columns:
            print("[Migration] Schema is already up to date with accepted_qty")
        
        else:
            print("[Migration] WARNING: Unexpected schema. Columns:", list(columns.keys()))
    
    except Exception as e:
        print(f"[Migration] ERROR: {e}")
        conn.rollback()
        raise
    
    finally:
        conn.close()

if __name__ == "__main__":
    migrate_vendor_po_lines_schema()
