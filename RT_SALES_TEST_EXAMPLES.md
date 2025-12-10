# Vendor Real Time Sales - Testing Examples

## Quick API Testing Guide

### Using Browser Developer Tools

1. Open the app in your browser
2. Press `F12` to open DevTools
3. Go to "Network" tab
4. Go to "Vendor Real Time Sales" tab in the app
5. Click "Refresh Now"
6. Watch the network requests to verify API calls

### Using curl (Command Line)

```bash
# Get ASIN view for last 2 hours
curl "http://localhost:8000/api/vendor-realtime-sales/summary?lookback_hours=2&view_by=asin"

# Get Time view for last 4 hours
curl "http://localhost:8000/api/vendor-realtime-sales/summary?lookback_hours=4&view_by=time"

# Get ASIN detail for specific ASIN
curl "http://localhost:8000/api/vendor-realtime-sales/asin/B0ABC123?lookback_hours=24"

# Test backward compatibility with old window param
curl "http://localhost:8000/api/vendor-realtime-sales/summary?window=last_24h"
```

### Using Postman or Thunder Client

**ASIN View (2 hours)**
```
Method: GET
URL: http://localhost:8000/api/vendor-realtime-sales/summary
Query Params:
  - lookback_hours: 2
  - view_by: asin
```

**Time View (8 hours)**
```
Method: GET
URL: http://localhost:8000/api/vendor-realtime-sales/summary
Query Params:
  - lookback_hours: 8
  - view_by: time
```

---

## Response Validation Checklist

### ASIN View Response (lookback_hours=2, view_by=asin)

```json
{
  "lookback_hours": 2,                    // ✓ Should be 2
  "view_by": "asin",                      // ✓ Should be "asin"
  "window": {
    "start_utc": "2025-12-09T18:14:51...",
    "end_utc": "2025-12-09T20:14:51...",
    "start_uae": "2025-12-09T22:14:51...",  // ✓ Should be +4 hours from UTC
    "end_uae": "2025-12-10T00:14:51..."     // ✓ Should be +4 hours from UTC
  },
  "total_units": 523,                     // ✓ Should match Amazon
  "total_revenue": 8542.75,               // ✓ Should match Amazon
  "currency_code": "AED",                 // ✓ Should be AED
  "rows": [                               // ✓ Should have 1+ items
    {
      "asin": "B0ABC123",
      "units": 45,
      "revenue": 1250.50,
      "imageUrl": "...",
      "first_hour_utc": "2025-12-09T19:00:00+00:00",
      "last_hour_utc": "2025-12-09T20:00:00+00:00"
    }
  ],
  "top_asins": [...]                      // ✓ Backward compat (same as rows)
}
```

### Time View Response (lookback_hours=4, view_by=time)

```json
{
  "lookback_hours": 4,                    // ✓ Should be 4
  "view_by": "time",                      // ✓ Should be "time"
  "window": {
    "start_utc": "...",
    "end_utc": "...",
    "start_uae": "...",
    "end_uae": "..."
  },
  "total_units": 1000,                    // ✓ Should match Amazon
  "total_revenue": 15000.00,              // ✓ Should match Amazon
  "currency_code": "AED",
  "rows": [
    {
      "bucket_start_utc": "2025-12-09T16:00:00+00:00",
      "bucket_end_utc": "2025-12-09T17:00:00+00:00",
      "bucket_start_uae": "2025-12-09T20:00:00+04:00",  // ✓ +4 hours
      "bucket_end_uae": "2025-12-09T21:00:00+04:00",    // ✓ +4 hours
      "units": 250,
      "revenue": 3750.50
    },
    {
      "bucket_start_utc": "2025-12-09T17:00:00+00:00",
      "bucket_end_utc": "2025-12-09T18:00:00+00:00",
      "bucket_start_uae": "2025-12-09T21:00:00+04:00",
      "bucket_end_uae": "2025-12-09T22:00:00+04:00",
      "units": 300,
      "revenue": 4500.75
    },
    // ... more hourly buckets
  ]
}
```

**Validation for Time View:**
- Sum of all `rows[].units` = `total_units`
- Sum of all `rows[].revenue` = `total_revenue`
- Each bucket end = next bucket start
- Number of buckets ≈ lookback_hours (might be less if no data)

---

## Manual Comparison Steps

### 1. Open Both Sides by Side

**Left side:** Amazon Vendor Central (Real Time Sales)
**Right side:** Your SP-API Desktop App (Vendor Real Time Sales tab)

### 2. Set Identical Lookback Window

Amazon:
- Go to "Lookback" dropdown
- Select "Trailing 2 hours"

Your App:
- Lookback: "Trailing 2 hours" (should be default)
- View By: "ASIN"
- Click "Refresh Now"

### 3. Check Summary Cards

**Amazon shows:**
- Total Ordered Units: [NUMBER]
- Total Ordered Revenue: [NUMBER] AED

**Your App shows:**
- Total Units: [NUMBER]
- Total Revenue: [CURRENCY] [NUMBER]

Verify they **match exactly** (down to decimal places)

### 4. Check Top 5 ASINs

**Amazon view:**
1. ASIN: B0XYZ123, Units: 45, Revenue: 1250.50
2. ASIN: B0ABC456, Units: 38, Revenue: 1100.75
3. ... (etc)

**Your App view:**
1. B0XYZ123 - Units: 45 - Revenue: 1250.50
2. B0ABC456 - Units: 38 - Revenue: 1100.75
3. ... (etc)

All must **match exactly** (ASIN, units, revenue)

### 5. Check Different Windows

Repeat steps 2-4 for each window:

**2 hours:**
```
Amazon → Your App
Units: 523 → 523 ✓
Revenue: 8542.75 → 8542.75 ✓
Top ASIN: B0ABC123 (45 units) → B0ABC123 (45 units) ✓
```

**4 hours:**
```
Amazon → Your App
Units: 1050 → 1050 ✓
Revenue: 17100.00 → 17100.00 ✓
```

**8 hours:**
```
Amazon → Your App
Units: 2100 → 2100 ✓
Revenue: 34200.00 → 34200.00 ✓
```

**12 hours:**
```
Amazon → Your App
Units: 3150 → 3150 ✓
Revenue: 51300.00 → 51300.00 ✓
```

**24 hours:**
```
Amazon → Your App
Units: 6300 → 6300 ✓
Revenue: 102600.00 → 102600.00 ✓
```

**48 hours:**
```
Amazon → Your App
Units: 12600 → 12600 ✓
Revenue: 205200.00 → 205200.00 ✓
```

### 6. Test Time View

**Amazon Vendor Central:**
- Look for "View By: Time" option
- Note the hourly breakdown

**Your App:**
- View By: "Time"
- Compare hourly buckets with Amazon
- Verify sum of all hours = total at top

### 7. Verify UAE Timezone Display

**Your App Window Info Label:**
Should show something like:
```
Trailing 2 hours (22:00 → 00:00 UAE)
```

**Check:**
- Times are in 24-hour format (not 12-hour)
- Times are in **UAE timezone** (UTC+4)
- If current UTC time is 20:00, then UAE time should be 00:00 (same day or next depending on time)
- Start time is earlier than end time

**Calculation:**
- If app shows "22:00 → 00:00 UAE"
- That's 2 hours apart ✓
- If you convert to UTC: "18:00 → 20:00 UTC" ✓

---

## Automated Test Plan (for developers)

### Python Test Script

```python
import requests
import json
from datetime import datetime, timezone, timedelta

BASE_URL = "http://localhost:8000"

# Test 1: ASIN view response structure
response = requests.get(f"{BASE_URL}/api/vendor-realtime-sales/summary", 
                       params={"lookback_hours": 2, "view_by": "asin"})
data = response.json()

assert response.status_code == 200, f"Expected 200, got {response.status_code}"
assert data["lookback_hours"] == 2
assert data["view_by"] == "asin"
assert "window" in data
assert "start_utc" in data["window"]
assert "start_uae" in data["window"]
assert isinstance(data["total_units"], int)
assert isinstance(data["total_revenue"], (int, float))
assert data["currency_code"] == "AED"
assert isinstance(data["rows"], list)
print("✓ Test 1 PASSED: ASIN view structure")

# Test 2: Time view response structure
response = requests.get(f"{BASE_URL}/api/vendor-realtime-sales/summary",
                       params={"lookback_hours": 4, "view_by": "time"})
data = response.json()

assert response.status_code == 200
assert data["lookback_hours"] == 4
assert data["view_by"] == "time"
assert len(data["rows"]) > 0, "Expected at least one time bucket"
assert "bucket_start_utc" in data["rows"][0]
assert "bucket_start_uae" in data["rows"][0]
print("✓ Test 2 PASSED: Time view structure")

# Test 3: Time bucket sum equals total
total_units = sum(row["units"] for row in data["rows"])
total_revenue = sum(row["revenue"] for row in data["rows"])

assert total_units == data["total_units"], \
    f"Sum of bucket units ({total_units}) != total_units ({data['total_units']})"
assert abs(total_revenue - data["total_revenue"]) < 0.01, \
    f"Sum of bucket revenue ({total_revenue}) != total_revenue ({data['total_revenue']})"
print("✓ Test 3 PASSED: Time bucket sum validation")

# Test 4: Backward compatibility
response = requests.get(f"{BASE_URL}/api/vendor-realtime-sales/summary",
                       params={"window": "last_24h"})
data = response.json()

assert response.status_code == 200
assert "rows" in data or "top_asins" in data
print("✓ Test 4 PASSED: Backward compatibility")

# Test 5: ASIN detail
asin = data["rows"][0]["asin"] if data["rows"] else "B0TEST123"
response = requests.get(f"{BASE_URL}/api/vendor-realtime-sales/asin/{asin}",
                       params={"lookback_hours": 24})
detail = response.json()

assert response.status_code == 200
assert detail["asin"] == asin
assert isinstance(detail["data"], list)
print("✓ Test 5 PASSED: ASIN detail endpoint")

# Test 6: Invalid lookback_hours
response = requests.get(f"{BASE_URL}/api/vendor-realtime-sales/summary",
                       params={"lookback_hours": 99, "view_by": "asin"})
assert response.status_code == 400, "Expected 400 for invalid lookback_hours"
print("✓ Test 6 PASSED: Invalid lookback validation")

# Test 7: Invalid view_by
response = requests.get(f"{BASE_URL}/api/vendor-realtime-sales/summary",
                       params={"lookback_hours": 2, "view_by": "invalid"})
assert response.status_code == 400, "Expected 400 for invalid view_by"
print("✓ Test 7 PASSED: Invalid view_by validation")

print("\n✓✓✓ All tests passed! ✓✓✓")
```

---

## Expected Behavior Summary

| Setting | Expected Behavior |
|---------|-------------------|
| Lookback: 2h, View: ASIN | Shows top ASINs for last 2 hours, units descending |
| Lookback: 2h, View: Time | Shows 2 hourly buckets, sorted by time ascending |
| Lookback: 24h, View: ASIN | Shows top ASINs for last 24 hours |
| Lookback: 24h, View: Time | Shows ~24 hourly buckets |
| Lookback: 48h, View: Time | Shows ~48 hourly buckets |
| Window info label | "Trailing N hours (HH:MM → HH:MM UAE)" |
| Total Units/Revenue | Matches Amazon Vendor Central exactly |
| Time display | In UAE timezone (Asia/Dubai, UTC+4) |
| Refresh button | Makes API call, ingests data |
| Summary load | No API calls (DB-only) |
| localStorage | Lookback, ViewBy, and sort state persist |

---

## Debugging Tips

### If numbers don't match Amazon:

1. **Check timestamp alignment:**
   - Amazon: Uses trailing window (now-N to now)
   - Your App: Also uses trailing window
   - Both should use same logic ✓

2. **Check currency:**
   - Ensure "AED" is set in both places
   - Revenue values should match exactly

3. **Check data freshness:**
   - Click "Refresh Now" to get latest data
   - Wait for ingestion to complete
   - Check logs for any errors

4. **Check time matching:**
   - Both windows should be for same lookback period
   - Verify no timezone confusion

### If times are wrong in UI:

1. **Check UAE timezone:**
   ```javascript
   // In browser console:
   const formatter = new Intl.DateTimeFormat('en-US', {
     timeZone: 'Asia/Dubai',
     hour: '2-digit',
     minute: '2-digit'
   });
   console.log(formatter.format(new Date())); // Should show Dubai time
   ```

2. **Check backend response:**
   - Get the API response JSON
   - Look at `window.start_uae` field
   - Should be ISO format with +04:00 offset

### If sorting doesn't work:

1. **Clear localStorage:**
   ```javascript
   localStorage.clear()
   ```

2. **Reload page**

3. **Try clicking column header again**

### If view doesn't switch:

1. **Check View By dropdown:**
   - Verify it shows both "ASIN" and "Time" options
   - Verify selection changes

2. **Check table display:**
   - Right-click → Inspect Element on the tables
   - Should see `display:none` on hidden table
   - Should see `display:table` on visible table

3. **Check browser console:**
   - Press F12
   - Look at Console tab
   - Should not show errors

---

## Common Issues & Fixes

| Issue | Fix |
|-------|-----|
| Numbers are slightly different by rounding | Normal; use rounded values or 2 decimals max |
| Times are 4 hours off | Likely UTC vs UAE timezone confusion; check backend |
| Blank table on first load | Click "Refresh Now" to fetch data |
| Table shows ASIN when Time selected | Check View By value in dropdown; reload page |
| Sorting doesn't persist | Clear browser localStorage, reload |
| API returns 400 error | Check lookback_hours is 2, 4, 8, 12, 24, or 48 |
| API returns 400 for view_by | Check view_by is exactly "asin" or "time" (lowercase) |

