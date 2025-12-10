# Out-of-Stock Export History Feature - Implementation Complete

**Date:** 2025-12-10  
**Status:** ✅ Complete and Ready for Testing  
**Feature:** Export history tracking for OOS items with "Pending" vs "Exported" status

---

## Overview

Implemented a persistent export history system for the Out-of-Stock (OOS) tab that:

- **Tracks exported ASINs** in SQLite database (`vendor_oos_export_history` table)
- **Shows status** in UI: "Pending for export" (never exported) vs "Exported" (previously exported)
- **Exports only pending** ASINs on each download
- **Records history** automatically after successful export
- **All rows visible** - no filtering, just status indicators

---

## Files Modified

### 1. services/db.py
**Added 4 new functions:**

```python
def ensure_oos_export_history_table()
    # Creates vendor_oos_export_history table with UNIQUE(asin, marketplace_id)
    # Runs on app startup
    
def mark_oos_asins_exported(asins, batch_id, marketplace_id)
    # Records ASINs as exported after successful export
    # Args: List of ASINs, batch UUID, marketplace ID
    
def get_exported_asins(marketplace_id)
    # Returns set of all exported ASINs for marketplace
    
def is_asin_exported(asin, marketplace_id)
    # Checks if single ASIN has been exported
```

**Database Schema:**
```sql
CREATE TABLE vendor_oos_export_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT NOT NULL,
    marketplace_id TEXT NOT NULL DEFAULT 'A2VIGQ35RCS4UG',
    exported_at TEXT NOT NULL,
    export_batch_id TEXT NOT NULL,
    notes TEXT,
    UNIQUE(asin, marketplace_id)
)
```

### 2. main.py

**Modified 2 endpoints:**

#### GET /api/oos-items
- **Added:** `export_status` field to each item
- **Values:** `"pending"` or `"exported"`
- **Logic:** Checks `is_asin_exported()` for each ASIN
- **Returns:** All OOS items with status (no filtering)

#### GET /api/oos-items/export
- **Changed:** Only exports pending ASINs (not previously exported)
- **New logic:**
  1. Identify pending ASINs (where `export_status == "pending"`)
  2. Generate UUID `batch_id`
  3. Build CSV from pending ASINs only
  4. Call `mark_oos_asins_exported()` to record them
  5. Return CSV to client
- **Edge cases:**
  - Empty pending list → returns empty CSV (no error)
  - Duplicates in history → handled by `UNIQUE` constraint

**Added initialization:**
- Calls `ensure_oos_export_history_table()` on app startup (line ~340)

### 3. ui/index.html

**CSS (lines ~84-86):**
```css
.export-status-pending { font-weight: 600; color: #d97706; }  /* Amber, bold */
.export-status-exported { opacity: 0.6; color: #6b7280; }     /* Greyed, muted */
.oos-row-exported { opacity: 0.7; }                           /* Row slightly faded */
```

**Updated `renderOosTable()` (lines ~1908-1950):**
- Reads `it.export_status` from API response
- Shows text instead of Restock button:
  - `"Pending for export"` (amber, bold)
  - `"Exported"` (greyed, muted)
- Applies `oos-row-exported` class for styling

**Updated `downloadOosXls()` (lines ~1904-1925):**
- Shows "Exporting..." status while downloading
- After 1 second, calls `loadOosItems()` to refresh
- Shows message: "Export complete. X total items marked as exported."
- Shows error message if export fails

---

## How It Works

### User Flow

```
1. User navigates to OOS tab
   ↓
2. API returns all OOS items with export_status field
   ↓
3. UI renders table with "Pending for export" or "Exported" in Actions column
   ↓
4. User clicks "Export OOS (XLS)" button
   ↓
5. Export endpoint:
   - Identifies pending ASINs only
   - Creates batch_id UUID
   - Builds CSV from pending ASINs
   - Records them as exported in DB
   - Returns CSV to client
   ↓
6. Frontend refreshes OOS list after export
   ↓
7. Those ASINs now show "Exported" status
   ↓
8. Next export will skip those ASINs automatically
```

### Database Behavior

**First export with 5 pending ASINs:**
```
CSV contains: 5 rows (all ASINs)
After export: 5 rows inserted into export_history
Next API call: export_status shows "exported" for those 5
Next export: Only new/pending ASINs included
```

**Unique constraint enforces:**
- Same ASIN + marketplace can only have 1 record
- Prevents duplicates if export function called twice

---

## API Response Example

### GET /api/oos-items
```json
{
  "items": [
    {
      "asin": "B001AAAA",
      "vendorSku": "SKU-001",
      "qty": 10,
      "export_status": "pending",  // NEW FIELD
      "isOutOfStock": true,
      ...
    },
    {
      "asin": "B002BBBB",
      "vendorSku": "SKU-002",
      "qty": 5,
      "export_status": "exported",  // NEW FIELD
      "isOutOfStock": true,
      ...
    }
  ]
}
```

### GET /api/oos-items/export
- Response: TSV file with pending ASINs only
- Header: `asin`
- Rows: Sorted list of pending ASINs
- After download: Export history updated in DB

---

## Testing Checklist

### Backend Tests

- [ ] App starts without errors (table created)
- [ ] `ensure_oos_export_history_table()` runs on startup
- [ ] `/api/oos-items` returns all items with `export_status` field
- [ ] `export_status` is "pending" for new ASINs
- [ ] `export_status` is "exported" for previously exported ASINs
- [ ] Export file contains only pending ASINs
- [ ] After export, DB records match exported batch

### Frontend Tests

- [ ] OOS tab loads and shows all items
- [ ] Actions column shows "Pending for export" text (amber, bold)
- [ ] Actions column shows "Exported" text (greyed) for exported items
- [ ] Exported rows slightly faded in appearance
- [ ] Export button shows "Exporting..." message
- [ ] After export, list refreshes automatically
- [ ] Status message shows count of exported items
- [ ] Exported items persist (don't re-export after refresh)

### Integration Tests

- [ ] Export ASINs once
  - File contains those ASINs
  - DB records created
  - Status updates to "Exported"

- [ ] Export again with new OOS items
  - File contains ONLY new pending ASINs
  - Old exported ASINs not included
  - New batch_id for new export

- [ ] Multiple exports
  - Each creates separate batch_id
  - All ASINs tracked individually
  - No duplicates in history table

### Edge Cases

- [ ] Export with 0 pending ASINs
  - Returns empty CSV (valid)
  - No error or exception
  - Status message displays correctly

- [ ] Restart app
  - Export history persists
  - Status shows correctly on reload

- [ ] Manual DB check
  ```sql
  SELECT * FROM vendor_oos_export_history;
  SELECT COUNT(DISTINCT asin) FROM vendor_oos_export_history;
  ```

---

## Code Changes Summary

| Component | Change | Lines |
|-----------|--------|-------|
| services/db.py | Add 4 functions + helpers | +120 |
| main.py startup | Add table init | +2 |
| main.py /api/oos-items | Add export_status field | +5 |
| main.py /api/oos-items/export | Filter pending, record exports | +20 |
| ui/index.html CSS | Add 3 CSS classes | +3 |
| ui/index.html renderOosTable | Display status | +15 |
| ui/index.html downloadOosXls | Refresh + message | +15 |
| **TOTAL** | | **~180** |

---

## Backward Compatibility

✅ **No breaking changes:**
- OOS list API returns all existing fields + new `export_status`
- Export endpoint still returns CSV format
- UI still shows all rows (nothing hidden)
- Other tabs unaffected

✅ **Safe:**
- Uses existing SQLite connection pool
- Follows project patterns (execute_write, context managers)
- Error handling graceful (logs warnings, continues)

---

## Future Enhancements

- Reset export status button (mark ASINs as pending again)
- Export history view (see all past exports)
- Batch re-export (export previously exported ASINs again)
- Export countdown (time until ASINs can be re-exported)

---

## Implementation Notes

1. **UNIQUE constraint:** Ensures same ASIN+marketplace recorded only once
   - INSERT OR IGNORE handles graceful duplicates

2. **Default marketplace:** "A2VIGQ35RCS4UG" (Amazon US)
   - Can be extended for multi-marketplace in future

3. **Batch tracking:** UUID tracks which ASINs in each export
   - Useful for future audit/reporting

4. **Time tracking:** exported_at stores UTC timestamp
   - Timezone aware for consistency

---

## Database Initialization

Table created automatically on app startup via:
```python
ensure_oos_export_history_table()  # Called in main.py
```

No manual migration needed. First run will create table.

---

**Status: Ready for Testing and Deployment ✅**

All requirements met:
- ✅ Export history persisted in DB
- ✅ "Pending" vs "Exported" status shown
- ✅ Only pending ASINs exported
- ✅ History recorded automatically
- ✅ All rows visible (no filtering)
- ✅ Backward compatible
- ✅ No new SP-API calls
- ✅ Uses existing local data only
