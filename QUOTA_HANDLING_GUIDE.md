# Vendor Real-Time Sales Quota Handling Guide

## Overview

This document describes the quota-aware changes made to prevent hammering Amazon's SP-API and gracefully handle quota exceeded (429) errors.

## Changes Summary

### 1. **spapi_reports.py** – Unified Quota Exception Handling

#### Before
- `createReport` raised `SpApiQuotaError` on 429
- `download_vendor_report_document` raised generic `HTTPError` on 429
- Callers had to handle multiple exception types

#### After
- Both `createReport` and `download_vendor_report_document` now raise `SpApiQuotaError` on 429
- Added 429 check in `getReportDocument` call (meta endpoint)
- Added 429 check in document download call (content endpoint)
- Single exception type `SpApiQuotaError` means "stop all SP-API calls"

#### Code Changes
```python
# In download_vendor_report_document:
if meta_resp.status_code == 429:
    raise SpApiQuotaError(f"QuotaExceeded downloading report document: {payload}")

# Same for document download
if doc_resp.status_code == 429:
    raise SpApiQuotaError(f"QuotaExceeded downloading report document payload: {payload}")
```

---

### 2. **vendor_realtime_sales.py** – Quota Handling & Configuration

#### New Module-Level Configuration Flags

Easy on/off switches for expensive audit operations:
```python
ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT = False  # Disabled by default (generates many API calls)
ENABLE_VENDOR_RT_SALES_DAILY_AUDIT = True    # Enabled (24h window is manageable)
QUOTA_COOLDOWN_MINUTES = 30                  # Cooldown period after quota hit
```

**To disable weekly audits:** Set `ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT = False` in `vendor_realtime_sales.py`  
**To disable daily audits:** Set `ENABLE_VENDOR_RT_SALES_DAILY_AUDIT = False` in `vendor_realtime_sales.py`

#### New In-Memory Cooldown Mechanism

Module-level variables:
```python
_rt_sales_quota_cooldown_until_utc = None  # Tracks cooldown expiration

def is_in_quota_cooldown(now_utc: datetime) -> bool:
    """Check if quota cooldown is active."""
    
def start_quota_cooldown(now_utc: datetime) -> None:
    """Activate a 30-minute cooldown (configurable via QUOTA_COOLDOWN_MINUTES)."""
```

The cooldown is **process-level** (in-memory). When the app restarts, cooldown is reset.

#### Hard-Stop Behavior in `backfill_realtime_sales_for_gap`

**Before:** Caught all exceptions and continued to next chunk (could exhaust quota on all remaining chunks)

**After:**
- Catches `SpApiQuotaError` separately
- Logs error with chunk details
- **Immediately aborts remaining chunks** (no further API calls for that backfill call)
- **Re-raises `SpApiQuotaError`** so caller can activate cooldown
- Other exceptions still logged and skipped, but do NOT abort

```python
try:
    # ... process chunk ...
except SpApiQuotaError as e:
    # HARD STOP: Quota exceeded, abort remaining chunks and re-raise
    logger.error("[VendorRtSalesBackfill] QUOTA EXCEEDED at chunk [...]. Aborting remaining chunks.")
    raise  # Re-raise so caller can activate cooldown
except Exception as e:
    # Other errors: log and continue (do not corrupt state)
    logger.error("[VendorRtSalesBackfill] Failed to process chunk [...]: %s", e)
    # Continue with next chunk
```

#### Updated `run_realtime_sales_audit_window`

- Now **propagates** `SpApiQuotaError` to caller
- Other exceptions are logged and suppressed (returns empty result)

---

### 3. **main.py** – Auto-Sync Loop with Quota Intelligence

#### Updated `vendor_rt_sales_auto_sync_loop`

The loop now:

1. **Checks quota cooldown at start of each cycle**
   ```python
   if is_in_quota_cooldown(now_utc):
       logger.warning("[RTSalesAutoSync] In quota cooldown; skipping all SP-API calls this cycle")
       time.sleep(interval_seconds)
       continue
   ```

2. **Catches `SpApiQuotaError` from normal backfill**
   ```python
   try:
       rows, asins, hours = backfill_realtime_sales_for_gap(...)
   except SpApiQuotaError as e:
       logger.error("[RTSalesAutoSync] QuotaExceeded; aborting remaining backfills/audits this cycle: {e}")
       start_quota_cooldown(now_utc)
       time.sleep(interval_seconds)
       continue  # Skip remaining audits this cycle
   ```

3. **Respects audit configuration flags**
   ```python
   if ENABLE_VENDOR_RT_SALES_DAILY_AUDIT:
       # Run daily audit
   
   if ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT:
       # Run weekly audit
   ```

4. **Catches `SpApiQuotaError` from each audit and stops further audits**
   ```python
   try:
       run_realtime_sales_audit_window(...)
   except SpApiQuotaError as e:
       logger.error("[RTSalesAutoSync] QuotaExceeded during daily audit; aborting remaining audits")
       start_quota_cooldown(now_utc)
       break  # Stop further audits this cycle
   ```

#### Result
- Once quota is hit, the app stops calling SP-API for 30 minutes
- No more chunks are processed in that cycle or remaining cycles until cooldown expires
- Existing data in the database is still queryable via summary endpoints
- Time windows are NOT marked as complete, so they can be retried when quota recovers

---

### 4. **POST /api/vendor-realtime-sales/refresh** Endpoint

#### Before
- Crashed if `createReport` or download returned 429
- Exception propagated to frontend as 500 error

#### After
- Catches `SpApiQuotaError` specifically
- Returns 200 OK with clear error message:
  ```json
  {
    "status": "error",
    "error": "QuotaExceeded",
    "message": "Amazon Vendor real-time sales quota has been exceeded for now. Showing last known data.",
    "window": "last_24h"
  }
  ```
- Frontend can display this message and show cached data from `GET /api/vendor-realtime-sales/summary`

#### Example Flow
1. User clicks "Refresh RT Sales" in UI
2. Frontend calls `POST /api/vendor-realtime-sales/refresh`
3. Backend hits quota → returns 200 OK with `"error": "QuotaExceeded"`
4. Frontend displays "Quota hit" message and calls `GET /api/vendor-realtime-sales/summary`
5. Summary endpoint returns data from the database (no SP-API call needed)
6. UI shows whatever data is already available

---

## Configuration & Tuning

### Disable Weekly Audits (Recommended to Start)

Edit `services/vendor_realtime_sales.py`:
```python
ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT = False  # ← Set to False
ENABLE_VENDOR_RT_SALES_DAILY_AUDIT = True
```

Weekly audits over 7 days with 6-hour chunks = 28 API calls per week. Disabling this is the easiest way to reduce pressure.

### Disable Daily Audits (If Needed)

```python
ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT = False
ENABLE_VENDOR_RT_SALES_DAILY_AUDIT = False  # ← Set to False
```

With both disabled, only the 15-minute normal backfill runs (2-4 calls per cycle).

### Adjust Cooldown Period

Edit `services/vendor_realtime_sales.py`:
```python
QUOTA_COOLDOWN_MINUTES = 30  # ← Increase if quota recovers slowly
```

Longer cooldown = longer wait before retrying, but less API spam.

---

## Behavior Flowchart

### Auto-Sync Cycle Flow

```
Start cycle (every 15 min)
    ↓
Check quota cooldown
    ├─ YES → Sleep until next cycle
    └─ NO ↓
    
Do normal backfill (last 3h or gap)
    ├─ SUCCESS → Continue
    ├─ QUOTA ERROR → Activate 30-min cooldown, skip remaining audits, sleep
    └─ OTHER ERROR → Log, continue
    
    ↓
Run daily audit (if enabled and overdue)
    ├─ SUCCESS → Update audit timestamp
    ├─ QUOTA ERROR → Activate 30-min cooldown, skip weekly, stop
    └─ OTHER ERROR → Log, continue
    
    ↓
Run weekly audit (if enabled and overdue)
    ├─ SUCCESS → Update audit timestamp
    ├─ QUOTA ERROR → Activate 30-min cooldown, stop
    └─ OTHER ERROR → Log, continue
    
    ↓
Sleep until next cycle
```

### Backfill Chunk Processing

```
For each 6-hour chunk:
    
    Request report (createReport)
        ├─ QUOTA ERROR → ABORT remaining chunks, re-raise
        └─ SUCCESS ↓
    
    Poll report (getReport)
        └─ SUCCESS ↓
    
    Download document (getReportDocument + download)
        ├─ QUOTA ERROR → ABORT remaining chunks, re-raise
        └─ SUCCESS ↓
    
    Ingest data
        └─ SUCCESS → Move to next chunk
    
    OTHER ERRORS → Log, move to next chunk
```

---

## Logging Examples

### Quota Cooldown Activated (Main Loop)
```
[RTSalesAutoSync] QuotaExceeded; aborting remaining backfills/audits this cycle
[VendorRtSales] Quota cooldown started until 2025-12-09T20:18:00+00:00
[RTSalesAutoSync] In quota cooldown; skipping all SP-API calls this cycle
```

### Hard-Stop in Backfill
```
[VendorRtSalesBackfill] Requesting chunk [2025-12-09T00:00:00, 2025-12-09T06:00:00)
[spapi_reports] getReportDocument failed 429 QuotaExceeded: {...}
[VendorRtSalesBackfill] QUOTA EXCEEDED at chunk [2025-12-09T00:00:00, 2025-12-09T06:00:00): QuotaExceeded downloading report document. Aborting remaining chunks.
```

### Refresh Endpoint Quota Hit
```
[VendorRtSales] Requesting report window=last_24h start=... end=...
[spapi_reports] createReport failed 429 QuotaExceeded: {...}
[VendorRtSales] QuotaExceeded during refresh: QuotaExceeded creating report
→ Returns: {"status": "error", "error": "QuotaExceeded", "message": "..."}
```

---

## Testing the Changes

### Test 1: Normal Backfill (No Quota)
```bash
# Wait for normal 15-minute cycle to run
# Check logs for: [RTSalesAutoSync] Cycle complete: X rows, Y ASINs, Z hours processed
# Check database: SELECT COUNT(*) FROM vendor_realtime_sales;
```

### Test 2: Manual Refresh (No Quota)
```bash
# Call POST /api/vendor-realtime-sales/refresh with body: {"window": "last_3h"}
# Expect: {"status": "success", "window": "last_3h", "ingest_summary": {...}}
```

### Test 3: Simulate Quota Hit (Requires Amazon Rate Limiting)
```bash
# Make many refresh calls rapidly to trigger 429
# Expect: {"status": "error", "error": "QuotaExceeded", "message": "..."}
# Check logs: [RTSalesAutoSync] In quota cooldown; skipping all SP-API calls this cycle
# Wait 30 minutes (or reduce QUOTA_COOLDOWN_MINUTES for testing)
# Check that backfills resume after cooldown expires
```

### Test 4: Disable Weekly Audits
```python
# In vendor_realtime_sales.py, set ENABLE_VENDOR_RT_SALES_WEEKLY_AUDIT = False
# Restart app
# Wait for a cycle to complete
# Check logs: Should NOT see "Running weekly audit" message
# Logs should only show: backfill + daily audit (if enabled)
```

---

## Summary of Benefits

| Aspect | Before | After |
|--------|--------|-------|
| **Quota Handling** | Exception types vary (HTTPError, SpApiQuotaError) | Single `SpApiQuotaError` type |
| **Backfill on 429** | Continues to all remaining chunks (wastes quota) | Hard-stop, re-raises immediately |
| **Auto-Sync on 429** | No cooldown; retries immediately → infinite loop | 30-min cooldown, then resumes |
| **Audit Control** | Always runs (7 days = 28 API calls/week) | Configurable, can disable weekly |
| **Refresh Endpoint** | Crashes (500) on quota | Returns 200 OK with clear error |
| **User Experience** | App appears broken; no cached data fallback | "Quota hit" message, shows cached data |

---

## References

- **Time Window Handling:** `services/vendor_realtime_sales.py` (SAFE_MINUTES_LAG, CHUNK_HOURS)
- **Quota Exception:** `services/spapi_reports.py` (SpApiQuotaError class)
- **Auto-Sync Loop:** `main.py` (vendor_rt_sales_auto_sync_loop function)
- **Refresh Endpoint:** `main.py` (refresh_vendor_realtime_sales function)
