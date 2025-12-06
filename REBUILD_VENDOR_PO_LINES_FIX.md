# Fix for rebuild_all_vendor_po_lines() Function

## Problem
The `rebuild_all_vendor_po_lines()` function was failing with:
```
sqlite3.OperationalError: no such table: vendor_pos
```

The function was trying to query a non-existent SQL table called `vendor_pos`, when in fact the PO header data is stored in a JSON file (`vendor_pos_cache.json`), not a database table.

## Root Cause
The initial implementation assumed POs were stored in a SQL table:
```python
# ❌ WRONG - This table doesn't exist
with get_db_connection() as conn:
    cur = conn.execute(
        "SELECT po_number FROM vendor_pos ORDER BY order_date DESC"
    )
```

However, the actual architecture is:
- **PO Headers**: Stored in `vendor_pos_cache.json` (JSON file, fetched from SP-API)
- **PO Lines**: Stored in `vendor_po_lines` table (SQLite, computed from detailed PO status)

## Solution
Updated `rebuild_all_vendor_po_lines()` to:

1. **Read from the correct source** (`vendor_pos_cache.json` instead of a SQL table)
2. **Reuse existing normalization logic** that the `/api/vendor-pos` endpoint uses
3. **Process all 45 POs** with their line items, writing 2033 total vendor_po_lines rows

### Key Changes

#### Before
```python
def rebuild_all_vendor_po_lines():
    # ...initialization code...
    
    # ❌ WRONG: trying to query non-existent table
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                "SELECT po_number FROM vendor_pos ORDER BY order_date DESC"
            )
            rows = cur.fetchall()
            po_numbers = [row["po_number"] for row in rows]
    except Exception as e:
        logger.error(f"[VendorPO] Failed to query vendor_pos: {e}")
        return
```

#### After
```python
def rebuild_all_vendor_po_lines():
    # ...initialization code...
    
    # ✅ CORRECT: read from vendor_pos_cache.json
    try:
        if not VENDOR_POS_CACHE.exists():
            logger.info("[VendorPO] vendor_pos_cache.json not found")
            return
        
        # Reuse the same normalization as /api/vendor-pos endpoint
        cache_data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
        normalized = normalize_pos_entries(cache_data)
        
        # Sort by date (newest first)
        normalized.sort(key=parse_po_date, reverse=True)
        
        po_numbers = [po.get("purchaseOrderNumber") for po in normalized 
                      if po.get("purchaseOrderNumber")]
    except Exception as e:
        logger.error(f"[VendorPO] Failed to read vendor_pos_cache.json: {e}")
        return
```

## Test Results
Running the command:
```bash
python main.py --rebuild-po-lines
```

Produces successful output:
```
[VendorPO] Rebuilding all vendor PO lines from SP-API...
[VendorPO] Found 45 POs to rebuild from cache
[VendorPO] Progress: 4/45 POs (8%)
[VendorPO] Progress: 8/45 POs (17%)
...
[VendorPO] Progress: 45/45 POs (100%)
[COMPLETE] [VendorPO] Rebuild complete: 45 POs processed, 0 errors, 2033 total vendor_po_lines rows
```

## Data Impact
- **Before rebuild**: Only 4 vendor_po_lines rows (for the most recently synced PO)
- **After rebuild**: 2033 vendor_po_lines rows (all 45 POs with their line items)
- **UI Fix**: The Vendor POs grid now shows non-zero Ordered/Received/Pending/Shortage values for all 45 POs, not just the last one

## Files Modified
- `main.py` (lines 1939-2033)
  - Updated `rebuild_all_vendor_po_lines()` function
  - CLI entry point already in place (lines 2040-2044)

## How to Use
### One-time rebuild (to backfill existing POs):
```bash
python main.py --rebuild-po-lines
```

### Normal incremental sync (continues as before):
```bash
python main.py  # starts FastAPI server normally
```

The incremental sync in `/api/vendor-pos` remains unchanged and continues to work correctly for new/modified POs.

## Architecture Notes
The Vendor POs flow is now:
1. **Incremental sync** (`GET /api/vendor-pos?refresh=1`):
   - Fetches new/changed POs from SP-API
   - Writes them to `vendor_pos_cache.json`
   - Calls `sync_vendor_po_lines_batch()` for each fetched PO
   
2. **Grid aggregation** (`GET /api/vendor-pos`):
   - Reads `vendor_pos_cache.json`
   - Aggregates vendor_po_lines data via SQL
   - Returns combined view to UI

3. **Full rebuild** (maintenance, CLI):
   - Reads all POs from `vendor_pos_cache.json`
   - Calls `_sync_vendor_po_lines_for_po()` for each
   - Backfills vendor_po_lines for POs that may have been cached before line-sync was added

This separation ensures the incremental sync is fast (only touches changed POs) while the full rebuild is available for occasional maintenance.
