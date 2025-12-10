# Auto-Sync Status Feature - Code Snippets

Quick reference for the exact code changes made.

## 1. services/vendor_realtime_sales.py

**Location:** After `start_quota_cooldown()` function (around line 108)

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
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    
    global _rt_sales_backfill_in_progress, _rt_sales_quota_cooldown_until_utc
    
    busy = _rt_sales_backfill_in_progress
    cooldown_active = is_in_quota_cooldown(now_utc)
    
    cooldown_until_utc = None
    cooldown_until_uae = None
    
    if cooldown_active and _rt_sales_quota_cooldown_until_utc:
        cooldown_until_utc = _rt_sales_quota_cooldown_until_utc.isoformat()
        cooldown_until_uae = _rt_sales_quota_cooldown_until_utc.astimezone(UAE_TZ).isoformat()
    
    if busy:
        message = "busy"
    elif cooldown_active:
        message = "cooldown"
    else:
        message = "idle"
    
    return {
        "busy": busy,
        "cooldown_active": cooldown_active,
        "cooldown_until_utc": cooldown_until_utc,
        "cooldown_until_uae": cooldown_until_uae,
        "message": message
    }
```

---

## 2. main.py

**Location:** After the `/api/vendor-realtime-sales/summary` endpoint (around line 2097)

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

---

## 3. ui/index.html

### A. HTML Changes

**Update Refresh Button (line 310):**
```html
<!-- CHANGE THIS -->
<button class="btn" onclick="refreshVendorRtSales()" style="margin-left:12px;">Refresh Now</button>

<!-- TO THIS -->
<button id="rt-sales-refresh-btn" class="btn" onclick="refreshVendorRtSales()" style="margin-left:12px;">Refresh Now</button>
```

**Replace Status Display (around line 311-312):**
```html
<!-- REMOVE THIS -->
<span id="rt-sales-status" style="font-size:12px; color:#6b7280;"></span>
</div>

<!-- REPLACE WITH THIS -->
</div>
<div id="rt-sales-sync-status" class="rt-sales-status-label"></div>
```

### B. CSS Changes

**Add to style section (after line 77):**
```css
/* Real-Time Sales status label */
.rt-sales-status-label { font-size: 12px; margin-top: 4px; color: #666; }
.rt-sales-status-busy { color: #d97706; font-weight: 500; }
.rt-sales-status-cooldown { color: #b91c1c; font-weight: 500; }
.rt-sales-status-idle { color: #059669; font-weight: 500; }
```

### C. JavaScript Changes

**1. Add global variable (before any RT Sales functions):**
```javascript
let rtSalesStatusIntervalId = null;
```

**2. Add these new functions (after `updateRtSalesWindowInfo()`, around line 2330):**
```javascript
// ========== Sync Status Monitoring ==========
let rtSalesStatusIntervalId = null;

async function updateRtSalesSyncStatus() {
  const statusEl = document.getElementById("rt-sales-sync-status");
  const refreshBtn = document.getElementById("rt-sales-refresh-btn");
  if (!statusEl || !refreshBtn) return;

  try {
    const resp = await fetch("/api/vendor-realtime-sales/status");
    if (!resp.ok) {
      statusEl.textContent = "Status: unavailable";
      statusEl.className = "rt-sales-status-label";
      refreshBtn.disabled = false;
      return;
    }

    const data = await resp.json();

    const busy = !!data.busy;
    const cooldown = !!data.cooldown_active;
    const cooldownUntilUae = data.cooldown_until_uae || null;

    // Reset base class
    statusEl.className = "rt-sales-status-label";

    if (busy) {
      statusEl.textContent = "🔵 Auto-sync running… (Real-time sales backfill in progress)";
      statusEl.classList.add("rt-sales-status-busy");
      refreshBtn.disabled = true;
    } else if (cooldown) {
      let label = "🟡 In quota cooldown (Refresh temporarily disabled)";
      if (cooldownUntilUae) {
        try {
          const dt = new Date(cooldownUntilUae);
          const hh = dt.getHours().toString().padStart(2, "0");
          const mm = dt.getMinutes().toString().padStart(2, "0");
          label = `🟡 In quota cooldown until ${hh}:${mm} UAE (Refresh temporarily disabled)`;
        } catch (e) {
          // ignore parse errors, keep generic label
        }
      }
      statusEl.textContent = label;
      statusEl.classList.add("rt-sales-status-cooldown");
      refreshBtn.disabled = true;
    } else {
      statusEl.textContent = "🟢 Idle (Auto-sync OK — you can refresh now)";
      statusEl.classList.add("rt-sales-status-idle");
      refreshBtn.disabled = false;
    }
  } catch (err) {
    console.error("[RtSales] Failed to update sync status", err);
    statusEl.textContent = "Status: unavailable";
    statusEl.className = "rt-sales-status-label";
    refreshBtn.disabled = false;
  }
}

function startRtSalesStatusPolling() {
  // Clear any existing interval
  if (rtSalesStatusIntervalId !== null) {
    clearInterval(rtSalesStatusIntervalId);
  }
  // Poll every 30 seconds
  updateRtSalesSyncStatus(); // Initial update
  rtSalesStatusIntervalId = setInterval(updateRtSalesSyncStatus, 30000);
}

function stopRtSalesStatusPolling() {
  if (rtSalesStatusIntervalId !== null) {
    clearInterval(rtSalesStatusIntervalId);
    rtSalesStatusIntervalId = null;
  }
}
```

**3. Update `refreshVendorRtSales()` (around line 2531):**
```javascript
// REPLACE THE ENTIRE FUNCTION WITH THIS:
async function refreshVendorRtSales() {
  const btn = document.getElementById("rt-sales-refresh-btn");
  if (btn) btn.disabled = true;

  try {
    const lookbackHours = document.getElementById("rt-sales-lookback").value;
    const body = {
      window: `trailing_${lookbackHours}h`  // Use a window value for backward compat
    };

    const resp = await fetch("/api/vendor-realtime-sales/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || err.detail || `HTTP ${resp.status}`);
    }

    const result = await resp.json();
    
    if (result.status === "error") {
      if (result.error === "QuotaExceeded") {
        console.warn("[VendorRtSales] Quota exceeded; using cached data");
        await loadVendorRtSalesSummary();
      } else {
        throw new Error(result.message || result.error || "Unknown error");
      }
    } else if (result.status === "success" && result.ingest_summary) {
      await loadVendorRtSalesSummary();
    } else {
      throw new Error("Invalid response from server");
    }
  } catch (err) {
    console.error("[VendorRtSales] Refresh failed:", err.message);
  } finally {
    // Let status endpoint decide whether button can be re-enabled
    await updateRtSalesSyncStatus();
  }
}
```

**4. Update `showTab()` function (around line 1619):**
```javascript
// FIND THIS BLOCK:
const rtSalesEl = document.getElementById("vendor-rt-sales-tab");
if (rtSalesEl) {
  rtSalesEl.style.display = tab === "vendor-rt-sales" ? "block" : "none";
  if (tab === "vendor-rt-sales") {
    initRtSalesTab();
    loadVendorRtSalesSummary();
  }
}

// REPLACE WITH THIS:
const rtSalesEl = document.getElementById("vendor-rt-sales-tab");
if (rtSalesEl) {
  rtSalesEl.style.display = tab === "vendor-rt-sales" ? "block" : "none";
  if (tab === "vendor-rt-sales") {
    initRtSalesTab();
    loadVendorRtSalesSummary();
    startRtSalesStatusPolling();
  } else {
    stopRtSalesStatusPolling();
  }
}
```

---

## Summary of Changes

| File | Change Type | Impact |
|------|------------|--------|
| services/vendor_realtime_sales.py | Add 1 function | Reads existing state, no logic changes |
| main.py | Add 1 endpoint | Lightweight, no DB/API calls |
| ui/index.html | Add 1 label, CSS, 3 JS functions, update 2 functions | UI enhancement |
| **Total Lines** | ~400 lines added | All surgical, minimal footprint |

---

## Testing Commands

### Test the backend function:
```bash
python -c "
from services.vendor_realtime_sales import get_rt_sales_status
from datetime import datetime, timezone

status = get_rt_sales_status(datetime.now(timezone.utc))
print('Status:', status)
"
```

### Test with simulated cooldown:
```bash
python -c "
from services.vendor_realtime_sales import (
    get_rt_sales_status,
    start_quota_cooldown,
)
from datetime import datetime, timezone

# Simulate cooldown
now = datetime.now(timezone.utc)
start_quota_cooldown(now)

# Check status
status = get_rt_sales_status(now)
print('With cooldown:', status)
"
```

### Test the API endpoint:
```bash
curl http://localhost:8000/api/vendor-realtime-sales/status | python -m json.tool
```

---

## Expected JSON Responses

### When Idle:
```json
{
  "busy": false,
  "cooldown_active": false,
  "cooldown_until_utc": null,
  "cooldown_until_uae": null,
  "message": "idle"
}
```

### When Backfill Running:
```json
{
  "busy": true,
  "cooldown_active": false,
  "cooldown_until_utc": null,
  "cooldown_until_uae": null,
  "message": "busy"
}
```

### When In Cooldown:
```json
{
  "busy": false,
  "cooldown_active": true,
  "cooldown_until_utc": "2025-12-10T20:35:00+00:00",
  "cooldown_until_uae": "2025-12-11T00:35:00+04:00",
  "message": "cooldown"
}
```
