# Auto-Sync Status - Visual Guide & Flow Diagrams

## UI Layout

```
┌─────────────────────────────────────────────────────────────┐
│ Vendor Real Time Sales                                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Lookback: [2▼]  View By: [ASIN▼]  [Refresh Now]           │
│  🟢 Idle (Auto-sync OK — you can refresh now)              │
│                                                             │
│  ┌─────────────────┐  ┌─────────────────┐                  │
│  │ Total Units:    │  │ Total Revenue:  │                  │
│  │    150          │  │    2,500.75     │                  │
│  └─────────────────┘  └─────────────────┘                  │
│                                                             │
│  Top ASINs                                                  │
│  Trailing 2 hours (06:00 → 08:00 UAE)                      │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ ASIN          Units    Revenue    First Hour        │   │
│  ├──────────────────────────────────────────────────────┤   │
│  │ B001234567    100      1500.00    2025-12-10T06:00Z │   │
│  │ B002345678     50       800.75    2025-12-10T07:00Z │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Status Label States

### 1. Idle State (Green)
```
🟢 Idle (Auto-sync OK — you can refresh now)
[Refresh Now]  ← Button ENABLED
```

### 2. Auto-Sync Running (Amber)
```
🔵 Auto-sync running… (Real-time sales backfill in progress)
[Refresh Now]  ← Button DISABLED (grayed out)
```

### 3. Quota Cooldown (Red)
```
🟡 In quota cooldown until 20:35 UAE (Refresh temporarily disabled)
[Refresh Now]  ← Button DISABLED (grayed out)
```

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ USER INTERFACE (ui/index.html)                                  │
│                                                                 │
│  showTab('vendor-rt-sales')                                    │
│         │                                                       │
│         ├─→ initRtSalesTab()                                   │
│         ├─→ loadVendorRtSalesSummary()                         │
│         └─→ startRtSalesStatusPolling()                        │
│                  │                                              │
│                  └─→ updateRtSalesSyncStatus() [immediate]     │
│                       │                                        │
└───────────────────────┼────────────────────────────────────────┘
                        │
                        ↓ fetch()
┌─────────────────────────────────────────────────────────────────┐
│ BACKEND API (main.py)                                           │
│                                                                 │
│ GET /api/vendor-realtime-sales/status                          │
│   │                                                             │
│   ├─→ datetime.now(timezone.utc)                               │
│   │                                                             │
│   └─→ vendor_realtime_sales_service.get_rt_sales_status()      │
│       │                                                         │
│       ├─→ Read: _rt_sales_backfill_in_progress                 │
│       │                                                         │
│       ├─→ Read: _rt_sales_quota_cooldown_until_utc             │
│       │                                                         │
│       ├─→ Call: is_in_quota_cooldown(now_utc)                  │
│       │                                                         │
│       ├─→ Convert: UTC → UAE timezone (if cooldown)            │
│       │                                                         │
│       └─→ Build: {"busy": bool, "cooldown_active": bool, ...}  │
│                                                                 │
└──────────────────────────────────────────────────────────────────┘
                        ↓ JSON response
┌─────────────────────────────────────────────────────────────────┐
│ USER INTERFACE (continued)                                      │
│                                                                 │
│ updateRtSalesSyncStatus()                                       │
│   │                                                             │
│   ├─→ Parse JSON response                                      │
│   │                                                             │
│   ├─→ if (busy) {                                              │
│   │     statusEl.textContent = "🔵 Auto-sync running..."       │
│   │     statusEl.classList.add("rt-sales-status-busy")         │
│   │     refreshBtn.disabled = true                             │
│   │   }                                                         │
│   │                                                             │
│   ├─→ else if (cooldown) {                                     │
│   │     statusEl.textContent = "🟡 In quota cooldown..."       │
│   │     statusEl.classList.add("rt-sales-status-cooldown")     │
│   │     refreshBtn.disabled = true                             │
│   │   }                                                         │
│   │                                                             │
│   └─→ else {                                                    │
│       statusEl.textContent = "🟢 Idle..."                      │
│       statusEl.classList.add("rt-sales-status-idle")           │
│       refreshBtn.disabled = false                              │
│     }                                                           │
│                                                                 │
│ [Polling continues every 30 seconds]                           │
│ [Until user leaves RT Sales tab]                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## State Machine

```
                ┌──────────────┐
                │   IDLE       │  (green) "Idle..."
                │              │
                └──┬────────┬──┘
                   │        │
     [auto-sync]   │        │  [quota limit hit]
          │        │        │        │
          ↓        │        │        ↓
    ┌──────────┐   │        │    ┌───────────┐
    │ BUSY     │   │        │    │ COOLDOWN  │
    │ (amber)  │───┘        └───→│ (red)     │
    │ "Running"└─────────────────│ "Cooldown"│
    └──────────┘    [backfill     └───────────┘
                     completes,
                     enters cooldown]
                                   │
                                   │ [cooldown
                                   │  expires]
                                   │
                                   └──→ IDLE
                                        (restart)
```

## Function Call Chain

```
User clicks RT Sales tab
        ↓
    showTab("vendor-rt-sales")
        ↓
    startRtSalesStatusPolling()
        ↓
    updateRtSalesSyncStatus()
        ├─→ fetch("/api/vendor-realtime-sales/status")
        │       ↓
        │   get_vendor_realtime_sales_status()
        │       ↓
        │   get_rt_sales_status(now_utc)
        │       ├─→ Read _rt_sales_backfill_in_progress
        │       ├─→ Read _rt_sales_quota_cooldown_until_utc
        │       ├─→ is_in_quota_cooldown(now_utc)
        │       └─→ Convert cooldown time to UAE
        │
        └─→ Parse JSON
        └─→ Update DOM
        └─→ Set button disabled state
        └─→ Display status message + color

[Wait 30 seconds]
        ↓
    updateRtSalesSyncStatus()
        [repeat polling...]

User clicks different tab
        ↓
    stopRtSalesStatusPolling()
        └─→ clearInterval()
```

## Refresh Button State Transitions

```
START: Idle (button enabled)
   │
   │ User clicks "Refresh Now"
   ↓
DISABLE: Button is disabled during fetch
   │
   │ Response received
   │ ├─ Success + new data available?
   │ ├─ Or Quota error + cached data?
   │ └─ updateRtSalesSyncStatus() called
   │
   ↓ Status endpoint returns...
   │
   ├─→ busy=true
   │   └─→ Disabled (auto-sync running)
   │
   ├─→ cooldown=true
   │   └─→ Disabled (quota cooldown)
   │
   └─→ busy=false, cooldown=false
       └─→ Enabled (idle, ready to refresh again)
```

## Component Interactions

```
┌─────────────────────────────────────────────────────────────────┐
│ Real-Time Sales Tab                                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Tab Controls                                            │   │
│  │ ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐│   │
│  │ │ Lookback: [2]│  │ View By:[ASIN]│  │[Refresh Button]││   │
│  │ └──────────────┘  └──────────────┘  └─────────────────┘│   │
│  └─────────────────────────────────────────────────────────┘   │
│                         ↓ (communicates                        │
│                          with)                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Status Label (NEW)                                      │   │
│  │ ┌─────────────────────────────────────────────────────┐ │   │
│  │ │ 🟢 Idle (Auto-sync OK — you can refresh now)       │ │   │
│  │ │ (Color changes: green/amber/red)                   │ │   │
│  │ └─────────────────────────────────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────┘   │
│                         ↑ (updated by)                         │
│                          /\                                    │
│                         /  \                                   │
│    ┌────────────────────┐    ┌─────────────────────────┐      │
│    │ updateRtSales...   │    │ Polling every 30s       │      │
│    │ SyncStatus()       │    │ while tab visible       │      │
│    │ (JS function)      │    │ (interval handler)      │      │
│    └────────────────────┘    └─────────────────────────┘      │
│                 ↓                            ↓                 │
│           ┌─────────────────────────────────────┐              │
│           │ GET /api/vendor-realtime-sales/    │              │
│           │         status                     │              │
│           │ (backend endpoint)                 │              │
│           └─────────────────────────────────────┘              │
│                         ↓                                      │
│           ┌─────────────────────────────────────┐              │
│           │ get_rt_sales_status()               │              │
│           │ (Python function)                  │              │
│           │ Reads in-memory state              │              │
│           └─────────────────────────────────────┘              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## CSS Class Application

```
Element: <div id="rt-sales-sync-status">

Base class (always present):
  .rt-sales-status-label
    font-size: 12px
    margin-top: 4px
    color: #666

Additional class based on state:
  
  if (busy) → add .rt-sales-status-busy
    color: #d97706 (amber)
    font-weight: 500
  
  else if (cooldown) → add .rt-sales-status-cooldown
    color: #b91c1c (red)
    font-weight: 500
  
  else → add .rt-sales-status-idle
    color: #059669 (green)
    font-weight: 500

Result:
  <div id="rt-sales-sync-status" 
       class="rt-sales-status-label rt-sales-status-busy">
    🔵 Auto-sync running…
  </div>
```

## Timing Diagram

```
User navigates to RT Sales tab
│
├─ T=0ms:  showTab('vendor-rt-sales') called
├─ T=10ms: initRtSalesTab() executes
├─ T=20ms: loadVendorRtSalesSummary() starts
├─ T=50ms: startRtSalesStatusPolling() called
├─ T=55ms: updateRtSalesSyncStatus() executes (immediate)
├─ T=70ms: fetch() sent to /api/vendor-realtime-sales/status
├─ T=85ms: Response received, JSON parsed
├─ T=90ms: DOM updated, status label visible
├─ T=100ms: Refresh button state set
│
├─ T=30s: 30-second interval triggers
├─ T=30.05s: updateRtSalesSyncStatus() executes again
├─ T=30.1s: fetch() sent
├─ T=30.2s: Response received, DOM updated
│
├─ T=60s: 30-second interval triggers again
│
├─ [User clicks different tab]
├─ T=120s: showTab() clears interval
├─ T=120.05s: stopRtSalesStatusPolling() called
├─ T=120.1s: Polling stops, no more status requests
│
├─ [User returns to RT Sales tab]
├─ T=180s: showTab('vendor-rt-sales') called
├─ T=180.1s: startRtSalesStatusPolling() called (new interval)
├─ T=180.15s: updateRtSalesSyncStatus() executes (immediate)
└─ [Polling resumes every 30 seconds]
```

This visual guide shows how all components work together seamlessly.
