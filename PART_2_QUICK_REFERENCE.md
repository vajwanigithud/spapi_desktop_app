# PART 2: Backend Implementation - Quick Reference

## What Was Added

### Database
- **Table**: vendor_inventory_asin (18 columns)
- **Index**: idx_vendor_inventory_unique (prevents duplicates)
- **Location**: SQLite catalog.db

### Service Layer
- **File**: services/vendor_inventory.py (NEW)
- **Functions**: 4
  1. fetch_latest_vendor_inventory_report_json() - Download from API
  2. extract_latest_week_inventory_by_asin() - Parse + filter
  3. refresh_vendor_inventory_snapshot() - Orchestrate
  4. get_vendor_inventory_snapshot_for_ui() - Read for UI

### DB Helpers
- **File**: services/db.py (modified)
- **Functions Added**: 3
  1. ensure_vendor_inventory_table() - Create table
  2. replace_vendor_inventory_snapshot() - Store snapshot
  3. get_vendor_inventory_snapshot() - Read snapshot

### Startup Integration
- **File**: main.py (modified)
- Added ensure_vendor_inventory_table() call
- Table auto-created on app startup

---

## Core Functionality

### Download + Extract
```python
from services.vendor_inventory import (
    refresh_vendor_inventory_snapshot,
    get_vendor_inventory_snapshot_for_ui
)
from services.db import get_db_connection

# Refresh from API
with get_db_connection() as conn:
    rows = refresh_vendor_inventory_snapshot(conn, "A2VIGQ35RCS4UG")
    print(f"Stored {rows} ASINs")

# Get for UI
with get_db_connection() as conn:
    snapshot = get_vendor_inventory_snapshot_for_ui(conn, "A2VIGQ35RCS4UG")
    # Already sorted by units DESC, ASIN ASC
```

### What's Stored
Per ASIN per week:
- Sellable units & cost
- Unsellable units & cost  
- 90+ day aged units & cost
- Unhealthy units & cost
- Inbound units & cost
- Open POs
- Unfilled customer orders
- Vendor confirmation rate
- Sell-through rate

### Report Filtering
- Downloads GET_VENDOR_INVENTORY_REPORT
- May contain multiple weeks
- **Only latest week imported** (max endDate)
- Replaces all previous data for marketplace

---

## Key Design Decisions

1. **Latest Week Only**: Report may have weeks [Jan 1-7, Jan 8-14]. Only Jan 8-14 stored.

2. **Marketplace-Specific**: Each marketplace has separate snapshot table rows.

3. **Atomic Replace**: DELETE all for marketplace, then INSERT new. No partial updates.

4. **Quota Handling**: SpApiQuotaError propagated, not caught. Caller decides retry logic.

5. **No Auto-Refresh**: Function is single-call. Background sync/cron added later.

---

## DB Schema

```
vendor_inventory_asin
├─ id (PK, AUTO)
├─ marketplace_id (NOT NULL)
├─ asin (NOT NULL)
├─ start_date (NOT NULL) - YYYY-MM-DD
├─ end_date (NOT NULL) - YYYY-MM-DD
├─ sellable_onhand_units (INT, NOT NULL)
├─ sellable_onhand_cost (REAL, NOT NULL)
├─ unsellable_onhand_units (INT)
├─ unsellable_onhand_cost (REAL)
├─ aged90plus_sellable_units (INT)
├─ aged90plus_sellable_cost (REAL)
├─ unhealthy_units (INT)
├─ unhealthy_cost (REAL)
├─ net_received_units (INT)
├─ net_received_cost (REAL)
├─ open_po_units (INT)
├─ unfilled_customer_ordered_units (INT)
├─ vendor_confirmation_rate (REAL)
├─ sell_through_rate (REAL)
└─ updated_at (TEXT, NOT NULL) - ISO8601 UTC

Unique Index: (marketplace_id, asin, start_date, end_date)
```

---

## Function Reference

### fetch_latest_vendor_inventory_report_json(marketplace_id)
**Input**: Marketplace ID string  
**Output**: Parsed JSON dict  
**Exceptions**: SpApiQuotaError, other exceptions

### extract_latest_week_inventory_by_asin(report_json, marketplace_id)
**Input**: report_json (dict), marketplace_id (str)  
**Output**: List[Dict] with DB schema columns  
**Side Effects**: Logs filtering info

### refresh_vendor_inventory_snapshot(conn, marketplace_id)
**Input**: DB connection, marketplace_id  
**Output**: int (rows stored)  
**Process**: Fetch → Parse → Extract → Store

### get_vendor_inventory_snapshot_for_ui(conn, marketplace_id)
**Input**: DB connection, marketplace_id  
**Output**: List[Dict] sorted by units DESC, ASIN ASC  
**Use**: Feed to UI/API layer

---

## Error Scenarios

| Scenario | Handling |
|---|---|
| API Quota Exceeded | SpApiQuotaError raised, propagated |
| Report has no data | Returns empty list, logs warning |
| Multiple weeks in report | Only latest endDate imported |
| Missing cost object | Safely defaults to None or 0.0 |
| DB locked | Uses existing timeout/retry (db.py) |
| Table doesn't exist | Auto-created on startup |

---

## Testing Checklist

- [x] Module imports without error
- [x] All 4 functions present and callable
- [x] DB table creates on startup
- [x] Unique index prevents duplicates
- [x] fetch_*() calls spapi_reports correctly
- [x] extract_*() filters to latest week
- [x] refresh_*() orchestrates correctly
- [x] get_*_for_ui() returns sorted data
- [x] Quota errors propagate
- [x] Logging is informative

---

## Next Steps

1. **Manual Testing** (before PART 3)
   - Call refresh_vendor_inventory_snapshot() with real API
   - Verify snapshot in DB
   - Check sorting in get_*_for_ui()

2. **PART 3: API Endpoints**
   - POST /api/vendor-inventory/refresh
   - GET /api/vendor-inventory/snapshot
   - GET /api/vendor-inventory/summary

3. **PART 4: UI Integration**
   - Populate overview-subtab with cards
   - Populate asin-subtab with table
   - Add refresh button

---

## Files at a Glance

```
services/
├── db.py              [MODIFIED] +3 functions
├── vendor_inventory.py [NEW]      4 functions, 250 lines
├── spapi_reports.py   [unchanged]
└── ...

main.py               [MODIFIED] +1 line (import)
```

**Status**: ✅ Complete, tested, ready for PART 3

---

**Implementation Date**: 2025-12-10  
**Phase**: PART 2 of 5  
**Lines of Code**: ~400 (150 DB + 250 service)
