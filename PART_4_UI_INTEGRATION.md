# PART 4: UI Integration - Inventory Tab

## Summary
Successfully added a complete Inventory tab to ui/index.html with dashboard controls, three subtabs (Snapshot, Aged 90+, Unhealthy), data tables, and CSV export functionality.

**Status**: ✅ COMPLETE
**Files Modified**: 1 (ui/index.html)
**UI Components Added**: 1 main tab + 3 subtabs + table + controls

---

## What Was Built

### 1. Inventory Main Tab
Added complete tab structure that integrates with existing showTab() logic:
- Tab button in main navigation
- Tab content panel with ID `inventory-tab`
- Auto-loads snapshot when tab is opened

### 2. Tab Controls
**Refresh Button** (POST /api/vendor-inventory/refresh)
- Disabled during refresh
- Shows status messages
- Auto-reloads snapshot after successful refresh

**Download CSV Button** (current view)
- Downloads CSV filtered by current subtab
- Includes all visible columns
- Handles CSV escaping properly

**Status Labels**
- Week label: "Week: YYYY-MM-DD → YYYY-MM-DD (latest week)"
- Status label: Loading messages, errors, counts

### 3. Three Inner Subtabs

#### a) Snapshot (All ASINs)
- Shows all ASINs from latest week
- Default/active subtab on load
- Displays all inventory metrics

#### b) Aged 90+ Days
- Filters to ASINs with aged90PlusDaysSellableInventoryUnits > 0
- Shows aging inventory concerns
- Helps identify old stock

#### c) Unhealthy / Excess
- Filters to ASINs with unhealthyInventoryUnits > 0
- Shows damaged/defective stock
- Highlights quality issues

### 4. Data Table
Columns displayed (left to right):
1. **ASIN** - Product identifier
2. **Title** - Product title (escaped HTML)
3. **Sellable** - Sellable on-hand units
4. **Unsellable** - Unsellable on-hand units
5. **Total On-hand** - Sum of sellable + unsellable
6. **Open PO** - Open purchase order units
7. **Aged 90+ Units** - Units aged 90+ days
8. **Unhealthy Units** - Damaged/defective units
9. **Net Received Units** - Inbound stock
10. **Sell-through Rate** - Percentage (0-1 formatted to 2 decimals)

**Table Features**:
- Sticky header
- Hover effects
- Footer row with totals
- Right-aligned numbers
- Row class placeholders for future styling (inv-row-zero, inv-row-aged, inv-row-unhealthy)

### 5. Data Filtering & Sorting
- Pre-filtered by subtab on render
- Sorted by units DESC, ASIN ASC (from backend)
- Totals recalculated per view
- Handles both snake_case and camelCase field names from backend

---

## HTML Structure

### Main Tab Container
```html
<div class="panel" id="inventory-tab" style="display:none; margin-top:14px;">
  <h2>Vendor Inventory – Latest Week</h2>
  
  <!-- Controls -->
  <div class="inventory-controls">
    <button id="inv-refresh-btn" class="btn">Refresh Inventory Snapshot</button>
    <button id="inv-download-btn" class="btn">Download CSV (current view)</button>
    <span id="inv-week-label" class="muted-text"></span>
    <span id="inv-status-label" class="muted-text"></span>
  </div>

  <!-- Subtabs -->
  <div class="inventory-subtabs">
    <button id="inv-subtab-snapshot" class="inventory-subtab-button active">Snapshot (All ASINs)</button>
    <button id="inv-subtab-aged" class="inventory-subtab-button">Aged 90+ Days</button>
    <button id="inv-subtab-unhealthy" class="inventory-subtab-button">Unhealthy / Excess</button>
  </div>

  <!-- Table -->
  <div id="inv-table-wrapper">
    <table id="inv-table" class="data-table">
      <thead id="inv-table-head"></thead>
      <tbody id="inv-table-body"></tbody>
      <tfoot id="inv-table-foot"></tfoot>
    </table>
  </div>
</div>
```

---

## CSS Added

### Inventory Controls
```css
.inventory-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}

.inventory-controls .muted-text {
  margin-left: 8px;
  font-size: 0.9em;
  opacity: 0.8;
}
```

### Subtab Buttons
```css
.inventory-subtabs {
  margin: 8px 0 12px 0;
}

.inventory-subtab-button {
  margin-right: 6px;
  padding: 4px 8px;
  font-size: 0.9em;
  cursor: pointer;
  background: #f0f0f0;
  border: 1px solid #ccc;
  border-radius: 4px;
}

.inventory-subtab-button.active {
  font-weight: bold;
  text-decoration: underline;
  background: #3498db;
  color: white;
}
```

### Table Styling
```css
.text-right {
  text-align: right;
}

.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.data-table thead th {
  background: #f8fafc;
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
}

.data-table tbody td {
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
}

.data-table tbody tr:hover {
  background: #f1f5f9;
}

.data-table tfoot tr {
  border-top: 2px solid #e5e7eb;
  background: #f9fafb;
  font-weight: 600;
}
```

### Row Classes (Placeholders)
```css
.inv-row-zero {
  /* placeholder for zero inventory rows */
}

.inv-row-aged {
  /* placeholder for aged rows */
}

.inv-row-unhealthy {
  /* placeholder for unhealthy rows */
}
```

---

## JavaScript Functions

### Global State
```javascript
let vendorInventorySnapshot = [];      // raw rows from API
let currentInventorySubtab = 'snapshot'; // 'snapshot' | 'aged' | 'unhealthy'
```

### loadVendorInventorySnapshotIfNeeded(forceReload)
**Purpose**: Load snapshot from API if not already cached

**Behavior**:
- Returns early if cached and forceReload=false
- Fetches GET /api/vendor-inventory/snapshot
- Updates status label
- Updates week label
- Renders current subtab
- Handles errors gracefully

### refreshVendorInventorySnapshot()
**Purpose**: Trigger API refresh and reload snapshot

**Behavior**:
- Disables refresh button during operation
- Calls POST /api/vendor-inventory/refresh
- Handles quota_error status
- Auto-reloads snapshot on success
- Shows ingested ASIN count
- Re-enables button finally

### setInventorySubtab(subtab)
**Purpose**: Switch active subtab

**Parameters**: 'snapshot' | 'aged' | 'unhealthy'

**Behavior**:
- Updates currentInventorySubtab
- Toggles button active state
- Calls renderVendorInventoryCurrentSubtab()

### renderVendorInventoryCurrentSubtab()
**Purpose**: Render table based on current subtab

**Delegates to**: renderVendorInventoryTable(mode)

### updateInventoryWeekLabelFromSnapshot(rows)
**Purpose**: Extract and display week range from snapshot

**Behavior**:
- Reads start_date and end_date from first row
- Updates inv-week-label text
- Handles missing dates gracefully

### escapeHtml(str)
**Purpose**: Prevent XSS by escaping HTML entities

**Characters escaped**:
- & → &amp;
- < → &lt;
- > → &gt;
- " → &quot;
- ' → &#39;

### renderVendorInventoryTable(mode)
**Purpose**: Main table rendering function

**Behavior**:
1. Filters vendorInventorySnapshot based on mode:
   - 'aged': aged90plus_sellable_units > 0
   - 'unhealthy': unhealthy_units > 0
   - 'snapshot': no filter (all rows)
2. Builds header row
3. Iterates through filtered rows:
   - Calculates totals
   - Handles missing fields (snake_case and camelCase)
   - Escapes HTML in title
   - Applies row classes
4. Appends tbody rows
5. Appends footer with totals

**Field Name Fallbacks**:
All fields check both snake_case and camelCase variants:
- sellable_onhand_units or sellableOnHandInventoryUnits
- unsellable_onhand_units or unsellableOnHandInventoryUnits
- aged90plus_sellable_units or aged90PlusDaysSellableInventoryUnits
- unhealthy_units or unhealthyInventoryUnits
- open_po_units or openPurchaseOrderUnits
- net_received_units or netReceivedInventoryUnits
- sell_through_rate or sellThroughRate

### downloadVendorInventorySnapshotCsv()
**Purpose**: Export current view as CSV

**Behavior**:
1. Validates data exists
2. Filters rows same as table render
3. Builds CSV header
4. Escapes quotes in titles
5. Creates Blob
6. Triggers browser download
7. Cleans up object URL

**CSV Columns**:
ASIN, Title, SellableUnits, UnsellableUnits, TotalOnHandUnits, OpenPOUnits, Aged90PlusUnits, UnhealthyUnits, NetReceivedUnits, SellThroughRate

---

## Integration Points

### showTab() Integration
When tab === 'inventory':
- Shows inventory-tab element
- Calls loadVendorInventorySnapshotIfNeeded()
- Auto-loads data on first open

### Backend Integration
**Uses two endpoints**:

1. **GET /api/vendor-inventory/snapshot**
   - Returns: {status: "ok", count: int, items: [{...}]}
   - Called on tab open or refresh

2. **POST /api/vendor-inventory/refresh**
   - Returns: {status: "ok"/"quota_error"/"error", ingested_asins: int}
   - Called by refresh button

### Data Flow
1. User opens Inventory tab
2. showTab('inventory') called
3. loadVendorInventorySnapshotIfNeeded() fetches API
4. vendorInventorySnapshot populated
5. renderVendorInventoryCurrentSubtab() renders default view
6. User switches subtabs → re-render with filter
7. User clicks refresh → POST endpoint → force reload
8. User clicks download → CSV export of current view

---

## Error Handling

### Loading Errors
- API errors show in status label
- Table still renders (empty state)
- User can retry with refresh button

### Refresh Errors
- Quota errors show specific message
- Other errors show generic message
- Button re-enabled even on error
- Previous snapshot remains visible

### CSV Export
- Validates data exists before export
- Escapes quotes properly
- Handles missing fields gracefully

---

## State Management

### Global State
- `vendorInventorySnapshot`: Array of ASIN rows (cached after first load)
- `currentInventorySubtab`: Current active subtab name

### Element State
- Button active classes (inv-subtab-*-button.active)
- Button disabled state (inv-refresh-btn)

### No Database/LocalStorage
- Pure stateless UI (reload resets state)
- State lives only in memory
- API calls fetch fresh data

---

## User Flows

### First Open
1. User clicks Inventory tab
2. Status: "Loading inventory snapshot…"
3. API call to GET /api/vendor-inventory/snapshot
4. Week label populated
5. Data displayed in Snapshot subtab
6. Status: "Loaded X ASINs"

### Refresh Data
1. User clicks Refresh button
2. Button disabled, shows "Refreshing…"
3. Status: "Refreshing inventory via SP-API…"
4. POST /api/vendor-inventory/refresh
5. If quota_error → status shows error
6. If success → reloads from DB
7. Button re-enabled
8. Status shows "Inventory refreshed. Ingested X ASINs"

### Switch Subtab
1. User clicks Aged/Unhealthy button
2. Button becomes active (bold, underline, blue)
3. Table filters and re-renders
4. Totals recalculated
5. CSV export uses new filtered data

### Export to CSV
1. User clicks Download CSV
2. Filters data by current subtab
3. Creates CSV with header + rows
4. Triggers browser download
5. File named: vendor_inventory_snapshot.csv

---

## Browser Compatibility

- Uses fetch() API (modern browsers)
- ES6 arrow functions
- Template literals
- const/let variables
- classList API
- URL.createObjectURL()

Requires ES6+ support (all modern browsers).

---

## Performance Notes

- Snapshot loaded once and cached in memory
- Table re-rendered (not modified in-place)
- CSV generation in-memory
- No pagination (assumes < 5000 ASINs per week)
- Lazy load on tab open (not on page load)

---

## Files Changed

**ui/index.html** (+500 lines):
- Tab button in navigation
- Inventory panel HTML structure
- CSS for controls, subtabs, table
- JavaScript functions (load, refresh, render, export)
- Global state variables
- showTab() integration

**No other files modified**

---

## Quality Checklist

- [x] HTML structure follows existing patterns
- [x] CSS classes consistent with existing styles
- [x] JavaScript uses existing event patterns
- [x] No breaking changes to other tabs
- [x] showTab() properly integrates new tab
- [x] Both endpoints (GET, POST) implemented
- [x] Error handling for API failures
- [x] Quota error handling
- [x] CSV export working
- [x] HTML escaping prevents XSS
- [x] Field name fallbacks handle both formats
- [x] Responsive layout (flex-wrap on controls)
- [x] Accessible button states
- [x] No console errors on load

---

## Summary

**What's Delivered**:
✅ Complete Inventory tab with 3 subtabs  
✅ Data table with 10 columns  
✅ Refresh button (POST /api/vendor-inventory/refresh)  
✅ CSV export (current view)  
✅ Status and week labels  
✅ Error handling and quota support  

**What's Ready**:
✅ Tab fully functional  
✅ All endpoints integrated  
✅ Data filtering working  
✅ CSV export ready  

**What's Next**:
⏳ PART 5: Advanced cosmetics, heatmaps, filters

---

**Date**: 2025-12-10  
**Phase**: PART 4 of 5  
**Status**: ✅ COMPLETE
