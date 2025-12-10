# Auto-Sync Status + Disable Refresh While Busy

## Overview
Added a lightweight status monitoring feature that allows the UI to:
- Display real-time status of the auto-sync/backfill system (busy, cooldown, or idle)
- Automatically disable the Refresh button when the backend is busy
- Poll the backend every 30 seconds (only when the RT Sales tab is active)
- Show human-readable status messages with UAE timestamps

## Changes Made

### 1. Backend: services/vendor_realtime_sales.py

**New Function: `get_rt_sales_status()`**

Added after the `start_quota_cooldown()` function (lines 108-155):

```python
def get_rt_sales_status(now_utc: Optional[datetime] = None) -> dict:
    """
    Return status of the Real-Time Sales auto-sync/backfill system.
    
    Returns:
        {
            "busy": bool,  # True if backfill/auto-sync is actively running
            "cooldown_active": bool,  # True if quota cooldown is active
            "cooldown_until_utc": Optional[str],  # ISO8601, or None
            "cooldown_until_uae": Optional[str],  # ISO8601 in UAE time, or None
            "message": str  # "busy", "cooldown", or "idle"
        }
    """
```

**Implementation Details:**
- Reads existing global state: `_rt_sales_backfill_in_progress` and `_rt_sales_quota_cooldown_until_utc`
- Uses existing helper: `is_in_quota_cooldown(now_utc)`
- Converts cooldown timestamp to UAE timezone using existing `UAE_TZ`
- Returns 5 fields for UI consumption

**No changes to:**
- Cooldown logic itself
- Backfill logic
- Database schema
- Quota handling

### 2. Backend: main.py

**New Endpoint: `GET /api/vendor-realtime-sales/status`**

Added after the `/api/vendor-realtime-sales/summary` endpoint (lines 2097-2117):

```python
@app.get("/api/vendor-realtime-sales/status")
def get_vendor_realtime_sales_status():
    """
    Lightweight status endpoint so the UI knows whether
    auto-sync/backfill or quota cooldown is active.
    
    Returns JSON with status fields for UI polling.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        status = vendor_realtime_sales_service.get_rt_sales_status(now_utc=now_utc)
        return status
    except Exception as e:
        logger.error(f"[VendorRtSales] Failed to get status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
```

**Characteristics:**
- No database queries
- No SP-API calls
- Just reads in-memory state
- Very lightweight, safe to poll frequently (every ~30s)
- Returns JSON like:
  ```json
  {
    "busy": false,
    "cooldown_active": true,
    "cooldown_until_utc": "2025-12-10T20:30:00+00:00",
    "cooldown_until_uae": "2025-12-11T00:30:00+04:00",
    "message": "cooldown"
  }
  ```

### 3. Frontend: ui/index.html

#### A. HTML Structure Changes

**Updated Refresh button** (line 310):
```html
<!-- BEFORE -->
<button class="btn" onclick="refreshVendorRtSales()" style="margin-left:12px;">Refresh Now</button>

<!-- AFTER -->
<button id="rt-sales-refresh-btn" class="btn" onclick="refreshVendorRtSales()" style="margin-left:12px;">Refresh Now</button>
```

**Removed old status span, added new status label** (lines 310-312):
```html
<!-- BEFORE -->
<span id="rt-sales-status" style="font-size:12px; color:#6b7280;"></span>

<!-- AFTER -->
<div id="rt-sales-sync-status" class="rt-sales-status-label"></div>
```

#### B. CSS Styles Added (lines 78-82)

```css
/* Real-Time Sales status label */
.rt-sales-status-label { font-size: 12px; margin-top: 4px; color: #666; }
.rt-sales-status-busy { color: #d97706; font-weight: 500; }
.rt-sales-status-cooldown { color: #b91c1c; font-weight: 500; }
.rt-sales-status-idle { color: #059669; font-weight: 500; }
```

**Color scheme:**
- Busy: Amber (#d97706)
- Cooldown: Red (#b91c1c)
- Idle: Green (#059669)

#### C. JavaScript Functions Added

**1. `updateRtSalesSyncStatus()` (lines 2331-2384)**
- Fetches `/api/vendor-realtime-sales/status`
- Updates status label with icon and message
- Disables/enables Refresh button based on state
- Parses cooldown timestamp and shows friendly time format

**2. `startRtSalesStatusPolling()` (lines 2386-2394)**
- Starts 30-second polling interval
- Only called when RT Sales tab is visible

**3. `stopRtSalesStatusPolling()` (lines 2396-2402)**
- Clears polling interval
- Called when leaving RT Sales tab

**4. Module-level variable:**
```javascript
let rtSalesStatusIntervalId = null;
```

#### D. Updated Existing Functions

**`refreshVendorRtSales()`** (lines 2533-2570):
- Simplified to remove old status element updates
- Now calls `await updateRtSalesSyncStatus()` in finally block
- Lets status endpoint decide button state after refresh

**`showTab()` function** (lines 1619-1628):
- Calls `startRtSalesStatusPolling()` when RT Sales tab is shown
- Calls `stopRtSalesStatusPolling()` when RT Sales tab is hidden
- Prevents polling on hidden tabs (saves bandwidth)

## User Experience

### Status Display Examples

**Idle (Green):**
```
🟢 Idle (Auto-sync OK — you can refresh now)
```
- Refresh button: **Enabled**
- Auto-sync not running
- No quota cooldown active

**Auto-Sync Running (Amber):**
```
🔵 Auto-sync running… (Real-time sales backfill in progress)
```
- Refresh button: **Disabled**
- Backfill/auto-sync actively running
- User cannot manually refresh

**Quota Cooldown (Red):**
```
🟡 In quota cooldown until 20:35 UAE (Refresh temporarily disabled)
```
- Refresh button: **Disabled**
- Quota cooldown active
- Shows time when cooldown expires
- User should wait before refreshing

## API Behavior

### GET /api/vendor-realtime-sales/status

**Request:**
```
GET /api/vendor-realtime-sales/status
```

**Response (200 OK):**
```json
{
  "busy": false,
  "cooldown_active": true,
  "cooldown_until_utc": "2025-12-10T20:35:00+00:00",
  "cooldown_until_uae": "2025-12-11T00:35:00+04:00",
  "message": "cooldown"
}
```

**Response Fields:**
- `busy`: true = backfill in progress, false = idle/cooldown
- `cooldown_active`: true = quota cooldown active
- `cooldown_until_utc`: Cooldown expiration in UTC (or null)
- `cooldown_until_uae`: Same time in UAE timezone (or null)
- `message`: "busy", "cooldown", or "idle"

**Possible States:**
```
busy=true, cooldown=false   → message="busy"
busy=false, cooldown=true   → message="cooldown"
busy=false, cooldown=false  → message="idle"
busy=true, cooldown=true    → message="busy" (busy takes precedence)
```

## Technical Details

### Polling Strategy
- **Interval:** 30 seconds
- **Only active:** When RT Sales tab is visible
- **Stops:** When user switches to different tab
- **Restarts:** When user returns to RT Sales tab
- **Lightweight:** No database, no SP-API calls

### Error Handling
- If status fetch fails: Show "Status: unavailable" and enable button
- If status parsing fails: Gracefully degrade to safe defaults
- Network errors logged to console but don't break UI

### Button Behavior
- **Manual refresh disabled:** While `busy=true` OR `cooldown_active=true`
- **Manual refresh enabled:** Only when `busy=false` AND `cooldown_active=false`
- **After manual refresh:** Status updates automatically, button toggles based on response

### Integration with Existing Systems
- Does NOT change backfill logic
- Does NOT change quota cooldown logic
- Does NOT change audit scheduling
- Does NOT change SP-API integration
- Only reads existing state, never writes to it

## Backward Compatibility

✅ **No breaking changes:**
- Old endpoints unchanged
- New endpoint is additive only
- UI functionality identical for non-RT-Sales tabs
- No database schema changes
- No API contract changes
- Works with existing cooldown/backfill logic

## Testing Checklist

### Backend
- [ ] `get_rt_sales_status()` returns correct JSON structure
- [ ] Function reads `_rt_sales_backfill_in_progress` correctly
- [ ] Function reads `_rt_sales_quota_cooldown_until_utc` correctly
- [ ] Converts UTC to UAE timestamp correctly
- [ ] Handles None/missing cooldown time gracefully
- [ ] Returns correct message based on state

### API Endpoint
- [ ] GET /api/vendor-realtime-sales/status returns 200
- [ ] Response includes all 5 required fields
- [ ] Safe to call every 30 seconds (no performance impact)
- [ ] No database locks or slowdowns

### Frontend
- [ ] Status label appears below Lookback/View By controls
- [ ] Status updates when tab is first shown (initial fetch)
- [ ] Status updates every 30 seconds while tab is active
- [ ] Status polling stops when tab is hidden
- [ ] Refresh button disabled when status shows "busy"
- [ ] Refresh button disabled when status shows "cooldown"
- [ ] Refresh button enabled when status shows "idle"
- [ ] Colors match: amber (busy), red (cooldown), green (idle)
- [ ] UAE time format shows correctly when available
- [ ] Works with no cooldown time (generic message)

### Integration
- [ ] Manual refresh still works
- [ ] Summary data still loads correctly
- [ ] Tab switching works properly
- [ ] No JavaScript errors in console
- [ ] No race conditions with parallel requests

## Files Modified
1. `services/vendor_realtime_sales.py` - Added `get_rt_sales_status()` function
2. `main.py` - Added `/api/vendor-realtime-sales/status` endpoint
3. `ui/index.html` - Added status label, CSS, JS functions, polling logic

## Performance Impact
- **Negligible:** 30-second polling with tiny JSON response
- **No database queries**
- **No SP-API calls**
- **Stops when tab is hidden**
- **~50 bytes per response**

## Future Enhancements
- Could add estimated time remaining for backfill
- Could show last successful sync timestamp
- Could integrate with browser notifications
- Could add historical status/activity log
