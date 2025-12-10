# PART 3: REST API Endpoints for Inventory Snapshot

## Summary
Added three REST API endpoints to expose inventory snapshot data and allow manual refresh from SP-API.

**Status**: ✅ COMPLETE
**Files Modified**: 1 (main.py)
**Endpoints Added**: 3 (1 POST, 2 GET)

---

## Endpoints Added

### 1. POST /api/vendor-inventory/refresh

**Purpose**: Download latest inventory report from SP-API and store snapshot

**Parameters**: None (uses configured marketplace)

**Request**: 
```json
POST /api/vendor-inventory/refresh
```

**Success Response** (HTTP 200):
```json
{
  "status": "ok",
  "ingested_asins": 150,
  "marketplace_id": "A2VIGQ35RCS4UG"
}
```

**Quota Error Response** (HTTP 200):
```json
{
  "status": "quota_error",
  "error": "QuotaExceeded creating report: ..."
}
```

**General Error Response** (HTTP 200):
```json
{
  "status": "error",
  "error": "Connection timeout or other error message"
}
```

**Process**:
1. Calls `refresh_vendor_inventory_snapshot()` from services/vendor_inventory.py
2. Downloads GET_VENDOR_INVENTORY_REPORT (weekly period)
3. Extracts latest week ASIN data
4. Stores into vendor_inventory_asin table
5. Returns count of ASINs stored

**Error Handling**:
- SpApiQuotaError: Returns 200 with status="quota_error" (caller handles retry)
- Other errors: Returns 200 with status="error"
- All errors logged with [VendorInventory] prefix

---

### 2. GET /api/vendor-inventory/snapshot

**Purpose**: Retrieve stored inventory snapshot for UI rendering

**Parameters**: None

**Request**:
```
GET /api/vendor-inventory/snapshot
```

**Success Response** (HTTP 200):
```json
{
  "status": "ok",
  "count": 150,
  "items": [
    {
      "id": 1,
      "marketplace_id": "A2VIGQ35RCS4UG",
      "asin": "B001ABC123",
      "start_date": "2025-01-08",
      "end_date": "2025-01-14",
      "sellable_onhand_units": 500,
      "sellable_onhand_cost": 12500.50,
      "unsellable_onhand_units": 5,
      "unsellable_onhand_cost": 50.00,
      "aged90plus_sellable_units": 10,
      "aged90plus_sellable_cost": 250.00,
      "unhealthy_units": 2,
      "unhealthy_cost": 20.00,
      "net_received_units": 100,
      "net_received_cost": 2500.00,
      "open_po_units": 50,
      "unfilled_customer_ordered_units": 5,
      "vendor_confirmation_rate": 0.95,
      "sell_through_rate": 0.85,
      "updated_at": "2025-01-10T14:23:45.123456+00:00"
    },
    ...
  ]
}
```

**Error Response** (HTTP 200):
```json
{
  "status": "error",
  "error": "Database connection failed",
  "count": 0,
  "items": []
}
```

**Data Sorting**:
- Primary: `sellable_onhand_units` DESC (highest inventory first)
- Secondary: `asin` ASC (alphabetical for ties)

**Use Cases**:
- UI dashboard rendering
- Inventory overview table
- ASIN breakdown sorting

---

### 3. GET /api/vendor-inventory/debug

**Purpose**: Developer-only endpoint for debugging raw report JSON

**Parameters**: None

**Request**:
```
GET /api/vendor-inventory/debug
```

**Success Response** (HTTP 200):
```json
{
  "status": "ok",
  "marketplace_id": "A2VIGQ35RCS4UG",
  "report_data": {
    "inventoryByAsin": [
      {
        "asin": "B001ABC123",
        "startDate": "2025-01-08",
        "endDate": "2025-01-14",
        "sellableOnHandInventoryUnits": 500,
        "sellableOnHandInventoryCost": {
          "amount": 12500.50,
          "currencyCode": "AED"
        },
        ...
      },
      ...
    ]
  }
}
```

**Error Response** (HTTP 200):
```json
{
  "status": "error",
  "error": "API request failed"
}
```

**Important**: 
- ⚠️ DO NOT consume this endpoint in production UI
- For development/debugging only
- Returns raw API JSON (not database rows)
- Useful for verifying API data structure
- Shows exactly what Amazon's API returns

---

## Implementation Details

### Pattern Compliance

**✅ Follows existing /api/vendor-* patterns**:
- Same error handling (quotas return 200, not 5xx)
- Same response structure (status, error, data fields)
- Same marketplace handling (uses MARKETPLACE_IDS[0])
- Same logging prefixes ([VendorInventory])

**✅ Error Handling**:
- SpApiQuotaError: Gracefully handled, returns status="quota_error"
- Connection errors: Caught and logged with full traceback
- All responses are HTTP 200 (even errors)
- Caller determines success by checking status field

**✅ Connection Management**:
- Uses `get_db_connection()` context managers
- Connections properly closed even on error
- Thread-safe via existing db.py locking

---

## Code Changes

### main.py

**Imports Added**:
```python
from services.vendor_inventory import (
    refresh_vendor_inventory_snapshot,
    get_vendor_inventory_snapshot_for_ui,
)
```

**Endpoints Added**: 3 new functions
- `api_vendor_inventory_refresh()` - POST endpoint
- `api_vendor_inventory_snapshot()` - GET endpoint
- `api_vendor_inventory_debug()` - GET endpoint (debug-only)

**Lines Added**: ~120

**Breaking Changes**: None

---

## Testing

### Test Refresh Endpoint
```bash
curl -X POST http://localhost:8000/api/vendor-inventory/refresh
# Expected: {"status": "ok", "ingested_asins": <count>, "marketplace_id": "..."}
```

### Test Snapshot Endpoint
```bash
curl http://localhost:8000/api/vendor-inventory/snapshot
# Expected: {"status": "ok", "count": <count>, "items": [...]}
```

### Test Debug Endpoint
```bash
curl http://localhost:8000/api/vendor-inventory/debug
# Expected: {"status": "ok", "marketplace_id": "...", "report_data": {...}}
```

### Python Testing
```python
import requests

# Test refresh
response = requests.post("http://localhost:8000/api/vendor-inventory/refresh")
assert response.json()["status"] in ("ok", "quota_error", "error")

# Test snapshot
response = requests.get("http://localhost:8000/api/vendor-inventory/snapshot")
data = response.json()
assert data["status"] == "ok"
assert isinstance(data["items"], list)
print(f"Fetched {data['count']} ASINs")

# Test debug
response = requests.get("http://localhost:8000/api/vendor-inventory/debug")
report = response.json()["report_data"]
assert "inventoryByAsin" in report
```

---

## Usage in UI (PART 4)

### Trigger Refresh
```javascript
// From Vue/React component
async function refreshInventory() {
  try {
    const response = await fetch('/api/vendor-inventory/refresh', {
      method: 'POST'
    });
    const data = await response.json();
    
    if (data.status === 'ok') {
      console.log(`Ingested ${data.ingested_asins} ASINs`);
      // Reload snapshot
      loadInventorySnapshot();
    } else if (data.status === 'quota_error') {
      // Show quota message, use cached data
      showMessage('API quota exceeded, showing cached data');
    } else {
      showError(data.error);
    }
  } catch (error) {
    showError(error.message);
  }
}
```

### Load Snapshot
```javascript
async function loadInventorySnapshot() {
  const response = await fetch('/api/vendor-inventory/snapshot');
  const data = await response.json();
  
  if (data.status === 'ok') {
    displayTable(data.items);
    updateMetrics(data.items);
  } else {
    showError(data.error);
  }
}

// Auto-load on tab open
showInventorySubtab('overview').then(() => {
  loadInventorySnapshot();
});
```

---

## Response Field Reference

### Snapshot Row Fields

| Field | Type | Description |
|-------|------|-------------|
| id | int | Database row ID |
| marketplace_id | string | "A2VIGQ35RCS4UG" |
| asin | string | Product ASIN |
| start_date | string | Week start (YYYY-MM-DD) |
| end_date | string | Week end (YYYY-MM-DD) |
| sellable_onhand_units | int | Primary metric (units) |
| sellable_onhand_cost | float | Valued inventory |
| unsellable_onhand_units | int | Defective, etc. |
| unsellable_onhand_cost | float | Cost of unsellable |
| aged90plus_sellable_units | int | Over 90 days old |
| aged90plus_sellable_cost | float | Cost of aged |
| unhealthy_units | int | Various issues |
| unhealthy_cost | float | Cost of unhealthy |
| net_received_units | int | Inbound stock |
| net_received_cost | float | Inbound value |
| open_po_units | int | Committed/in-flight |
| unfilled_customer_ordered_units | int | Back-orders |
| vendor_confirmation_rate | float | 0.0-1.0 |
| sell_through_rate | float | 0.0-1.0 |
| updated_at | string | ISO8601 UTC timestamp |

---

## Integration Points

### With Backend (PART 2)
- ✅ refresh_vendor_inventory_snapshot() - Service function
- ✅ get_vendor_inventory_snapshot_for_ui() - Service function
- ✅ fetch_latest_vendor_inventory_report_json() - For debug endpoint
- ✅ Database layer (db.py functions)

### With UI (PART 4)
- ⏳ Inventory overview dashboard
- ⏳ ASIN breakdown table
- ⏳ Refresh button
- ⏳ Filters and sorting

### Error Handling
- ✅ QuotaExceeded from SP-API (propagated)
- ✅ Connection errors (logged, returned as status="error")
- ✅ Database errors (logged, returned as status="error")
- ⏳ User-friendly error messages (PART 4 UI)

---

## Quality Checklist

- [x] Code compiles without errors
- [x] Follows existing /api/vendor-* patterns
- [x] Error handling matches real-time sales pattern
- [x] Marketplace ID handling matches other endpoints
- [x] Context managers properly used for DB connections
- [x] All functions have proper logging
- [x] No breaking changes to existing endpoints
- [x] Debug endpoint marked as developer-only
- [x] Response structures consistent (status, error, data)
- [x] Ready for PART 4 UI integration

---

## Summary

**What's Added**: 3 REST endpoints for inventory management
**What's Ready**: API is complete and tested
**What's Next**: PART 4 will add UI components and integration

---

**Date**: 2025-12-10  
**Phase**: PART 3 of 5  
**Status**: ✅ COMPLETE
