# Out-of-Stock Export History - Code Changes Summary

## Quick Reference: What Changed

### 1. services/db.py (END OF FILE - ~120 lines added)

**New functions for export history:**

```python
def ensure_oos_export_history_table():
    """Create vendor_oos_export_history table on app startup"""
    # Creates table with UNIQUE(asin, marketplace_id)
    # Creates index for fast lookups

def mark_oos_asins_exported(asins, batch_id, marketplace_id):
    """Record ASINs as exported after successful export"""
    # Takes list of ASINs and marks them exported
    # Inserts records with batch_id for grouping

def get_exported_asins(marketplace_id):
    """Get all ASINs ever exported for a marketplace"""
    # Returns set of ASIN strings

def is_asin_exported(asin, marketplace_id):
    """Check if single ASIN has been exported"""
    # Returns True/False
```

---

### 2. main.py

#### A. Startup initialization (line ~340)

**BEFORE:**
```python
try:
    vendor_realtime_sales_service.init_vendor_realtime_sales_table()
    from services.db import init_vendor_rt_sales_state_table
    init_vendor_rt_sales_state_table()
except Exception as e:
    logger.warning(f"[Startup] Failed to init vendor_realtime_sales tables (non-critical): {e}")
```

**AFTER:**
```python
try:
    vendor_realtime_sales_service.init_vendor_realtime_sales_table()
    from services.db import init_vendor_rt_sales_state_table, ensure_oos_export_history_table
    init_vendor_rt_sales_state_table()
    ensure_oos_export_history_table()  # <- NEW
except Exception as e:
    logger.warning(f"[Startup] Failed to init vendor_realtime_sales tables (non-critical): {e}")
```

#### B. GET /api/oos-items endpoint (line ~2376)

**ADDED IMPORT AND LOGIC:**
```python
def get_oos_items():
    """Return consolidated Out-of-Stock items with export_status field"""
    from services.db import is_asin_exported  # <- NEW
    
    # ... existing logic to build agg ...
    
    consolidated = []
    for asin, entry in agg.items():
        entry["poNumbers"] = sorted(list(entry.get("poNumbers") or []))
        entry["export_status"] = "exported" if is_asin_exported(asin) else "pending"  # <- NEW
        consolidated.append(entry)
    
    return {"items": consolidated}
```

#### C. GET /api/oos-items/export endpoint (line ~2422)

**CHANGED FROM: Export all OOS items**
```python
# OLD: Exported all ASINs
asins: set[str] = set()
for it in items:
    # ... build asins set ...
    asins.add(asin)

output = StringIO()
writer = csv.writer(output, delimiter="\t")
writer.writerow(["asin"])
for asin in sorted(asins):
    writer.writerow([asin])
```

**TO: Export only pending, record in DB**
```python
import uuid  # <- NEW
from services.db import is_asin_exported, mark_oos_asins_exported  # <- NEW

# Only include pending (never exported before)
pending_asins: list[str] = []
for it in items:
    # ... existing checks ...
    if not is_asin_exported(asin):  # <- NEW CHECK
        pending_asins.append(asin)

batch_id = str(uuid.uuid4())  # <- NEW

if pending_asins:
    mark_oos_asins_exported(pending_asins, batch_id)  # <- NEW: Record exports

output = StringIO()
writer = csv.writer(output, delimiter="\t")
writer.writerow(["asin"])
for asin in sorted(pending_asins):  # <- Use pending only
    writer.writerow([asin])
```

---

### 3. ui/index.html

#### A. CSS Styles (lines ~84-86)

**ADDED:**
```css
/* Out-of-Stock export status */
.export-status-pending { font-weight: 600; color: #d97706; }  /* Amber, bold */
.export-status-exported { opacity: 0.6; color: #6b7280; }     /* Greyed */
.oos-row-exported { opacity: 0.7; }                           /* Row faded */
```

#### B. renderOosTable() function (lines ~1908-1950)

**REMOVED:**
```javascript
// OLD: Restock button in Actions column
<td>
  <button class="btn" style="background:#16a34a"
          onclick="restockOosItem('${poNumber}','${asin}')">
    Restocked
  </button>
</td>
```

**ADDED:**
```javascript
const exportStatus = it.export_status || "pending";  // <- NEW: Read from API

// Row styling for exported items
const rowClass = exportStatus === "exported" ? ' class="oos-row-exported"' : '';

// Status display instead of button
const statusHTML = exportStatus === "exported"
  ? '<span class="export-status-exported">Exported</span>'
  : '<span class="export-status-pending">Pending for export</span>';

return `
  <tr${rowClass}>  <!-- Apply row styling -->
    ...
    <td>${statusHTML}</td>  <!-- Show status instead of button -->
  </tr>
`;
```

#### C. downloadOosXls() function (lines ~1904-1925)

**CHANGED FROM:**
```javascript
function downloadOosXls() {
  window.open("/api/oos-items/export", "_blank");
}
```

**TO:**
```javascript
function downloadOosXls() {
  const statusEl = document.getElementById("oos-status");
  if (statusEl) statusEl.textContent = "Exporting...";
  
  try {
    window.open("/api/oos-items/export", "_blank");
    
    // Refresh list after export completes
    setTimeout(async () => {
      await loadOosItems();
      const exportedCount = oosItems.filter(it => it.export_status === "exported").length;
      if (statusEl) {
        statusEl.textContent = `Export complete. ${exportedCount} total items marked as exported.`;
      }
    }, 1000);
  } catch (err) {
    if (statusEl) statusEl.textContent = "Export failed: " + err.message;
  }
}
```

---

## Summary of Behavior Changes

| Aspect | Before | After |
|--------|--------|-------|
| **What's exported** | All OOS ASINs | Only pending (new) ASINs |
| **What's shown** | "Restocked" button | "Pending" or "Exported" status |
| **History tracking** | None | DB table tracks all exports |
| **After export** | Nothing | List auto-refreshes, shows exported status |
| **Re-export** | Includes all again | Skips previously exported |
| **Row visibility** | All shown | All shown (no filtering) |

---

## Key Implementation Details

### Export History Table
```sql
vendor_oos_export_history (
  id INTEGER PRIMARY KEY,
  asin TEXT,
  marketplace_id TEXT,
  exported_at TEXT (UTC),
  export_batch_id TEXT (UUID),
  notes TEXT,
  UNIQUE(asin, marketplace_id)
)
```

### Default Marketplace
All functions default to "A2VIGQ35RCS4UG" (Amazon US)

### No New Dependencies
- Uses `uuid` (Python stdlib)
- Uses existing SQLite connection pool
- Uses existing CSV writer

---

## Testing Points

1. **First export:** File contains all pending ASINs
2. **DB after export:** Records created for each exported ASIN
3. **List after export:** API returns export_status="exported" for those ASINs
4. **UI after export:** Shows "Exported" status in Actions column
5. **Second export:** File contains only new ASINs (not previous ones)
6. **Empty pending:** Empty CSV returned (no error)

---

## Files to Deploy

1. `services/db.py` (modified)
2. `main.py` (modified)  
3. `ui/index.html` (modified)

No schema migrations needed. Table created automatically on first run.
