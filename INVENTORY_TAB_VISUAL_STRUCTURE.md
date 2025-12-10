# Inventory Tab - Visual Structure Guide

## DOM Hierarchy

```
<div class="layout">
  <div class="tabs">
    <button class="tab-btn" data-tab="pos">Vendor POs</button>
    <button class="tab-btn" data-tab="catalog">Catalog Fetcher</button>
    <button class="tab-btn" data-tab="oos">Out-of-Stock Items</button>
    <button class="tab-btn" data-tab="vendor-rt-sales">Vendor Real Time Sales</button>
    <button class="tab-btn" data-tab="inventory">Inventory</button>        ← NEW
    <button class="tab-btn" data-tab="tester">Endpoint Tester</button>
    <button class="tab-btn" data-tab="notifications">Notifications</button>
  </div>
</div>

<div class="panel" id="inventory-tab" style="display:none;">      ← NEW
  <h2>Inventory</h2>
  <div style="padding:12px; ...">
    <div class="subtab-bar">                                    ← NEW
      <button class="subtab-btn active" onclick="showInventorySubtab('overview')">
        Overview (Latest Week)
      </button>
      <button class="subtab-btn" onclick="showInventorySubtab('asin')">
        ASIN Breakdown (Latest Week)
      </button>
    </div>

    <div id="inventory-overview-subtab" class="inventory-subtab">  ← NEW
      <!-- Content will be added in later parts -->
    </div>

    <div id="inventory-asin-subtab" class="inventory-subtab" style="display:none;">  ← NEW
      <!-- Content will be added in later parts -->
    </div>
  </div>
</div>
```

## User Interaction Flow

### 1. Click "Inventory" Tab
```
User clicks Inventory button
         ↓
showTab('inventory') is called
         ↓
- All other tabs hidden (display:none)
- inventory-tab shown (display:block)
- Overview subtab is active by default
- ASIN subtab is hidden
```

### 2. Click "Overview (Latest Week)" Subtab
```
User clicks Overview button
         ↓
showInventorySubtab('overview') is called
         ↓
- All .subtab-btn elements have active class removed
- inventory-overview-subtab shown (display:block)
- inventory-asin-subtab hidden (display:none)
- Overview button gets .active class (blue background)
```

### 3. Click "ASIN Breakdown (Latest Week)" Subtab
```
User clicks ASIN Breakdown button
         ↓
showInventorySubtab('asin') is called
         ↓
- All .subtab-btn elements have active class removed
- inventory-overview-subtab hidden (display:none)
- inventory-asin-subtab shown (display:block)
- ASIN button gets .active class (blue background)
```

## CSS Styling Reference

### Subtab Button States

#### Inactive Button
```css
.subtab-btn {
  padding: 8px 14px;
  margin-right: 10px;
  background: #f0f0f0;        /* Light gray */
  border: 1px solid #ccc;
  border-radius: 6px;
  cursor: pointer;
}
```
Visual: Gray button with dark text, clickable

#### Active Button
```css
.subtab-btn.active {
  background: #3498db;        /* Bright blue */
  color: white;
}
```
Visual: Blue button with white text (current selection)

### Spacing

```css
.subtab-bar {
  margin: 15px 0;              /* Top & bottom spacing */
}

.inventory-subtab {
  margin-top: 20px;            /* Space between subtabs and content */
}
```

## JavaScript Functions

### showTab(tab)
**Purpose**: Switch between main tabs (Vendor POs, Catalog, Inventory, etc.)

**For inventory tab**:
```javascript
const inventoryEl = document.getElementById("inventory-tab");
if (inventoryEl) {
  inventoryEl.style.display = tab === "inventory" ? "block" : "none";
}
```

### showInventorySubtab(name)
**Purpose**: Switch between inventory subtabs (Overview vs ASIN Breakdown)

**Parameters**: 
- `"overview"` → Shows Overview subtab
- `"asin"` → Shows ASIN Breakdown subtab

**Logic**:
1. Hide both subtabs
2. Show selected subtab based on `name`
3. Remove active class from all buttons
4. Add active class to clicked button

**Code**:
```javascript
function showInventorySubtab(name) {
  // Step 1: Hide both
  document.getElementById("inventory-overview-subtab").style.display = "none";
  document.getElementById("inventory-asin-subtab").style.display = "none";

  // Step 2: Show selected
  if (name === "overview") {
    document.getElementById("inventory-overview-subtab").style.display = "block";
  } else if (name === "asin") {
    document.getElementById("inventory-asin-subtab").style.display = "block";
  }

  // Step 3: Update button styling
  document.querySelectorAll(".subtab-btn").forEach(btn => {
    btn.classList.remove("active");
  });
  
  // Step 4: Mark clicked button as active
  document.querySelector(
    `.subtab-btn[onclick="showInventorySubtab('${name}')"]`
  ).classList.add("active");
}
```

## Content Placeholder Locations

These empty divs will be populated with actual content in later phases:

```
<div id="inventory-overview-subtab" class="inventory-subtab">
  <!-- Phase 2: Add overview dashboard -->
  <!-- - Summary cards (total items, warehouses, etc.) -->
  <!-- - Recent activity -->
  <!-- - Key metrics -->
</div>

<div id="inventory-asin-subtab" class="inventory-subtab" style="display:none;">
  <!-- Phase 2: Add ASIN breakdown table -->
  <!-- - ASIN column -->
  <!-- - Quantity column -->
  <!-- - Warehouse distribution -->
  <!-- - Status column -->
</div>
```

## Example: Adding Content to Subtabs

When ready for Phase 2, you can populate these divs like this:

```javascript
// Populate Overview subtab
document.getElementById("inventory-overview-subtab").innerHTML = `
  <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px;">
    <div style="background: #e3f2fd; padding: 12px; border-radius: 8px;">
      <div style="font-size: 12px; color: #666;">Total Items</div>
      <div style="font-size: 24px; font-weight: bold;">1,234</div>
    </div>
    <!-- More cards here -->
  </div>
`;

// Populate ASIN subtab
document.getElementById("inventory-asin-subtab").innerHTML = `
  <div style="overflow: auto;">
    <table>
      <thead>
        <tr>
          <th>ASIN</th>
          <th>Qty</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody id="asin-breakdown-rows">
        <!-- Rows will be added here -->
      </tbody>
    </table>
  </div>
`;
```

## Integration Points for Future Phases

### Phase 2 - Backend Data Fetching
```javascript
// Add these functions when backend is ready
async function loadInventoryOverview() {
  // Fetch /api/inventory/overview?window=latest_week
  // Populate inventory-overview-subtab
}

async function loadAsinBreakdown() {
  // Fetch /api/inventory/asin-breakdown?window=latest_week
  // Populate inventory-asin-subtab with table data
}

// Call these when switching tabs
function showInventorySubtab(name) {
  // ... existing code ...
  if (name === "overview") {
    loadInventoryOverview();  // ← NEW
  } else if (name === "asin") {
    loadAsinBreakdown();      // ← NEW
  }
}
```

### Phase 3 - Real-time Updates
```javascript
// Add refresh button and auto-update
let inventoryAutoRefreshInterval = null;

function startInventoryAutoRefresh() {
  inventoryAutoRefreshInterval = setInterval(async () => {
    const currentView = ???  // Track which subtab is active
    if (currentView === "overview") {
      await loadInventoryOverview();
    } else if (currentView === "asin") {
      await loadAsinBreakdown();
    }
  }, 30000);  // Refresh every 30 seconds
}
```

---

**Visual Ready**: ✅ Complete  
**Interactive**: ✅ Buttons fully functional  
**Ready for Backend**: ✅ Content divs ready  
**Documentation**: ✅ All placeholders documented
