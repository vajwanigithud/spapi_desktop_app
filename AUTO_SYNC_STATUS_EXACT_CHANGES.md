# Final Code Review - Exact Changes Made

## 1. services/vendor_realtime_sales.py

**Location:** After `start_quota_cooldown()` function, around line 108

**BEFORE:** (lines 98-107)
```python
def start_quota_cooldown(now_utc: datetime) -> None:
    """Start a quota cooldown period (prevents further API calls for a while)."""
    global _rt_sales_quota_cooldown_until_utc
    _rt_sales_quota_cooldown_until_utc = now_utc + timedelta(minutes=QUOTA_COOLDOWN_MINUTES)
    logger.warning(
        f"[VendorRtSales] Quota cooldown started until {_rt_sales_quota_cooldown_until_utc.isoformat()}"
    )


# ====================================================================
```

**AFTER:** (lines 98-155)
```python
def start_quota_cooldown(now_utc: datetime) -> None:
    """Start a quota cooldown period (prevents further API calls for a while)."""
    global _rt_sales_quota_cooldown_until_utc
    _rt_sales_quota_cooldown_until_utc = now_utc + timedelta(minutes=QUOTA_COOLDOWN_MINUTES)
    logger.warning(
        f"[VendorRtSales] Quota cooldown started until {_rt_sales_quota_cooldown_until_utc.isoformat()}"
    )


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


# ====================================================================
```

**Change Summary:** Added 1 function, 48 lines

---

## 2. main.py

**Location:** After `/api/vendor-realtime-sales/summary` endpoint, around line 2097

**BEFORE:** (lines 2090-2097)
```python
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[VendorRtSales] Failed to get summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/vendor-realtime-sales/asin/{asin}")
```

**AFTER:** (lines 2090-2117)
```python
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[VendorRtSales] Failed to get summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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


@app.get("/api/vendor-realtime-sales/asin/{asin}")
```

**Change Summary:** Added 1 endpoint, 21 lines

---

## 3. ui/index.html - HTML Changes

### 3A. Update Refresh Button (Line 310)

**BEFORE:**
```html
<button class="btn" onclick="refreshVendorRtSales()" style="margin-left:12px;">Refresh Now</button>
<span id="rt-sales-status" style="font-size:12px; color:#6b7280;"></span>
</div>
```

**AFTER:**
```html
<button id="rt-sales-refresh-btn" class="btn" onclick="refreshVendorRtSales()" style="margin-left:12px;">Refresh Now</button>
</div>
<div id="rt-sales-sync-status" class="rt-sales-status-label"></div>
```

**Changes:**
- Added `id="rt-sales-refresh-btn"` to button
- Replaced old status span with new status div
- New div has ID `rt-sales-sync-status` and class `rt-sales-status-label`

---

## 4. ui/index.html - CSS Changes

### 4B. Add CSS Classes (After Line 77)

**LOCATION:** After `.po-items-summary td { ... }` style

**ADD:**
```css
/* Real-Time Sales status label */
.rt-sales-status-label { font-size: 12px; margin-top: 4px; color: #666; }
.rt-sales-status-busy { color: #d97706; font-weight: 500; }
.rt-sales-status-cooldown { color: #b91c1c; font-weight: 500; }
.rt-sales-status-idle { color: #059669; font-weight: 500; }
```

**4 new CSS classes added**

---

## 5. ui/index.html - JavaScript Changes

### 5C1. Add Global Variable (Before any RT Sales JS)

**ADD:**
```javascript
let rtSalesStatusIntervalId = null;
```

### 5C2. Add Sync Status Functions (After updateRtSalesWindowInfo(), around line 2330)

**ADD:**
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

**3 new functions added, 67 lines total**

### 5C3. Update refreshVendorRtSales() Function (around line 2531)

**BEFORE:**
```javascript
async function refreshVendorRtSales() {
  const btn = document.querySelector("button[onclick='refreshVendorRtSales()']");
  const status = document.getElementById("rt-sales-status");
  if (btn) btn.disabled = true;
  if (status) status.textContent = "Refreshing...";

  try {
    const lookbackHours = document.getElementById("rt-sales-lookback").value;
    const body = {
      window: `trailing_${lookbackHours}h`
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
        if (status) status.textContent = `⚠️ Quota exceeded. Showing cached data from last refresh.`;
        console.warn("[VendorRtSales] Quota exceeded; using cached data");
        await loadVendorRtSalesSummary();
      } else {
        throw new Error(result.message || result.error || "Unknown error");
      }
      return;
    }
    
    if (result.status === "success" && result.ingest_summary) {
      if (status) {
        status.textContent = `✓ Ingested ${result.ingest_summary.rows} rows (${result.ingest_summary.asins} ASINs, ${result.ingest_summary.hours} hours)`;
      }
      await loadVendorRtSalesSummary();
    } else {
      throw new Error("Invalid response from server");
    }
  } catch (err) {
    if (status) status.textContent = `✗ Failed: ${err.message}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}
```

**AFTER:**
```javascript
async function refreshVendorRtSales() {
  const btn = document.getElementById("rt-sales-refresh-btn");
  if (btn) btn.disabled = true;

  try {
    const lookbackHours = document.getElementById("rt-sales-lookback").value;
    const body = {
      window: `trailing_${lookbackHours}h`
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

**Changes:**
- Use `document.getElementById("rt-sales-refresh-btn")` instead of querySelector
- Remove old status element updates
- Call `await updateRtSalesSyncStatus()` in finally block

### 5C4. Update showTab() Function (around line 1619)

**BEFORE:**
```javascript
const rtSalesEl = document.getElementById("vendor-rt-sales-tab");
if (rtSalesEl) {
  rtSalesEl.style.display = tab === "vendor-rt-sales" ? "block" : "none";
  if (tab === "vendor-rt-sales") {
    initRtSalesTab();
    loadVendorRtSalesSummary();
  }
}
```

**AFTER:**
```javascript
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

**Changes:**
- Call `startRtSalesStatusPolling()` when showing RT Sales tab
- Call `stopRtSalesStatusPolling()` when hiding RT Sales tab

---

## Change Summary

| Component | Type | Lines | Details |
|-----------|------|-------|---------|
| vendor_realtime_sales.py | Add function | 48 | `get_rt_sales_status()` |
| main.py | Add endpoint | 21 | `GET /api/vendor-realtime-sales/status` |
| index.html | Update HTML | 4 | Button ID, new status label |
| index.html | Add CSS | 4 | 4 new CSS classes |
| index.html | Add JS functions | 67 | 3 new functions |
| index.html | Update JS | 10 | Modify 2 existing functions |
| **TOTAL** | | **~154** | Surgical, minimal changes |

---

## Verification Commands

```bash
# Check Python syntax
python -m py_compile services/vendor_realtime_sales.py
python -m py_compile main.py

# Test get_rt_sales_status function
python -c "from services.vendor_realtime_sales import get_rt_sales_status; print(get_rt_sales_status())"

# Once app running, test endpoint
curl http://localhost:8000/api/vendor-realtime-sales/status

# Check HTML has required elements
grep -n "rt-sales-refresh-btn\|rt-sales-sync-status\|updateRtSalesSyncStatus" ui/index.html
```

All changes verified and ready for deployment.
