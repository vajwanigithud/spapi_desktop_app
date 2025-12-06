#!/usr/bin/env python3
"""Query vendor_po_lines table to verify data population."""
import sqlite3
import sys

def main():
    db_path = r'C:\spapi_desktop_app\catalog.db'
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row  # Enable column access by name
        cursor = conn.cursor()
        
        # Check if table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='vendor_po_lines'
        """)
        if not cursor.fetchone():
            print('[ERROR] vendor_po_lines table does not exist')
            return 1
        
        # Get table info
        cursor.execute('PRAGMA table_info(vendor_po_lines)')
        print('[INFO] Table schema:')
        for col in cursor.fetchall():
            print(f'  {col[1]}: {col[2]}')
        print()
        
        # Get row count
        cursor.execute('SELECT COUNT(*) as cnt FROM vendor_po_lines')
        total_rows = cursor.fetchone()['cnt']
        print(f'[INFO] Total rows in vendor_po_lines: {total_rows}\n')
        
        if total_rows == 0:
            print('[WARN] No data found in vendor_po_lines table')
            print('[INFO] Table exists but is empty. Run sync to populate.')
            return 0
        
        # Run the requested query
        print('[QUERY] Group by PO number with line count and sum of ordered_qty:')
        print('=' * 85)
        cursor.execute('''
            SELECT
                po_number,
                COUNT(*) AS line_count,
                SUM(ordered_qty) AS ordered_sum,
                SUM(received_qty) AS received_sum,
                SUM(shortage_qty) AS shortage_sum
            FROM vendor_po_lines
            GROUP BY po_number
            ORDER BY line_count DESC
            LIMIT 25
        ''')
        
        print(f'{"PO_NUMBER":<35} {"LINES":<8} {"ORDERED":<10} {"RECEIVED":<10} {"SHORTAGE"}')
        print('-' * 85)
        
        rows = cursor.fetchall()
        for row in rows:
            po = row['po_number'] or '(null)'
            lines = row['line_count']
            ordered = row['ordered_sum'] or 0
            received = row['received_sum'] or 0
            shortage = row['shortage_sum'] or 0
            print(f'{po:<35} {lines:<8} {ordered:<10} {received:<10} {shortage}')
        
        print(f'\n[INFO] Total POs with data: {len(rows)}')
        conn.close()
        return 0
        
    except Exception as e:
        print(f'[ERROR] {e}', file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
