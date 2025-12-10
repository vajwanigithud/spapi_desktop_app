# PART 3: API Endpoints - Quick Start Guide

## Three New Endpoints

### 1️⃣ POST /api/vendor-inventory/refresh
Trigger inventory snapshot refresh from SP-API

**Request**:
```bash
curl -X POST http://localhost:8000/api/vendor-inventory/refresh
```

**Success Response**:
```json
{
  "status": "ok",
  "ingested_asins": 150,
  "marketplace_id": "A2VIGQ35RCS4UG"
}
```

**Quota Error**:
```json
{
  "status": "quota_error",
  "error": "..."
}
```

---

### 2️⃣ GET /api/vendor-inventory/snapshot
Get stored inventory snapshot (latest week only)

**Request**:
```bash
curl http://localhost:8000/api/vendor-inventory/snapshot
```

**Response**:
```json
{
  "status": "ok",
  "count": 150,
  "items": [
    {
      "id": 1,
      "asin": "B001ABC123",
      "start_date": "2025-01-08",
      "end_date": "2025-01-14",
      "sellable_onhand_units": 500,
      "sellable_onhand_cost": 12500.50,
      ...
    },
    ...
  ]
}
```

**Data is sorted by**:
- Units DESC (highest first)
- ASIN ASC (alphabetical for ties)

---

### 3️⃣ GET /api/vendor-inventory/debug
Developer-only: Raw API JSON

**Request**:
```bash
curl http://localhost:8000/api/vendor-inventory/debug
```

**Response**: Raw JSON from Amazon API
```json
{
  "status": "ok",
  "marketplace_id": "A2VIGQ35RCS4UG",
  "report_data": {
    "inventoryByAsin": [...]
  }
}
```

⚠️ **For debugging only — do NOT use in UI**

---

## Python Usage

```python
import requests
import json

# 1. Trigger refresh
response = requests.post("http://localhost:8000/api/vendor-inventory/refresh")
result = response.json()

if result["status"] == "ok":
    print(f"✓ Stored {result['ingested_asins']} ASINs")
elif result["status"] == "quota_error":
    print("⚠ Quota exceeded, retry later")
else:
    print(f"✗ Error: {result['error']}")

# 2. Get snapshot
response = requests.get("http://localhost:8000/api/vendor-inventory/snapshot")
data = response.json()

if data["status"] == "ok":
    for item in data["items"][:5]:  # Top 5
        print(f"{item['asin']}: {item['sellable_onhand_units']} units")
else:
    print(f"✗ Error: {data['error']}")
```

---

## JavaScript Usage (for UI)

```javascript
// Refresh inventory
async function refreshInventory() {
  const response = await fetch('/api/vendor-inventory/refresh', {
    method: 'POST'
  });
  const data = await response.json();
  
  if (data.status === 'ok') {
    console.log(`✓ Ingested ${data.ingested_asins} ASINs`);
    loadSnapshot();  // Reload
  } else {
    console.error('Error:', data.error);
  }
}

// Load snapshot for UI
async function loadSnapshot() {
  const response = await fetch('/api/vendor-inventory/snapshot');
  const data = await response.json();
  
  if (data.status === 'ok') {
    renderTable(data.items);  // Show in table
    updateMetrics(data);       // Update cards
  }
}

// Load on tab open
function showInventoryTab() {
  loadSnapshot();
}
```

---

## API Features

✅ **Error Handling**
- Quota errors: status="quota_error" (caller retries)
- Other errors: status="error" with message
- All responses are HTTP 200 (check status field)

✅ **Logging**
- All requests logged with [VendorInventory] prefix
- Full error stack traces on failure
- Quota errors logged as warnings

✅ **Thread-Safe**
- DB connections use context managers
- Connection pooling via existing db.py
- Safe for concurrent requests

✅ **Pattern Match**
- Follows /api/vendor-realtime-sales pattern
- Same error handling style
- Same marketplace logic
- Same response structure

---

## Files Changed

**main.py**:
- Added imports from services/vendor_inventory
- Added 3 new endpoint functions
- ~120 lines added

**No other files modified**

---

## Integration with PART 2

✅ Uses `refresh_vendor_inventory_snapshot()` from services/vendor_inventory.py  
✅ Uses `get_vendor_inventory_snapshot_for_ui()` from services/vendor_inventory.py  
✅ Uses DB functions from services/db.py  
✅ Uses `fetch_latest_vendor_inventory_report_json()` for debug endpoint  

---

## Ready for PART 4

✅ All endpoints functional and tested  
✅ Data ready for UI consumption  
✅ Sorting applied for UI display  
✅ Error handling in place  

**Next**: PART 4 will add UI components to render this data

---

**Date**: 2025-12-10  
**Phase**: PART 3 of 5  
**Status**: ✅ COMPLETE
