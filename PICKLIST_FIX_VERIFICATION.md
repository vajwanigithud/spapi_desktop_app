# Picklist Fix - Verification Checklist

## Changes Summary
- ✅ **services/picklist_service.py**: Fixed quantity source and rejection handling
- ✅ **ui/index.html**: Removed redundant filtering and simplified sorting
- ✅ **Syntax validation**: Python compile passes

## What Was Fixed

### Problem 1: All Lines Showing [OOS]
**Before**: Every line showed red strike-through with [OOS] badge
**After**: Only real out-of-stock items show [OOS]

**Root Cause**: Backend was adding ALL lines with `accepted < ordered` to OOS state
**Fix**: Only fully rejected lines (accepted=0, ordered>0) are excluded entirely

### Problem 2: Total Units Showing 0
**Before**: Summary header showed "Total Units: 0"
**After**: Summary shows correct accepted quantity total

**Root Cause**: Only counting non-OOS items, but almost everything was marked OOS
**Fix**: Count all accepted items in total, separate from OOS flag

### Problem 3: Wrong Quantities Used
**Before**: Picklist showed ordered quantities
**After**: Picklist shows accepted quantities (what we're actually shipping)

**Root Cause**: Code was pulling `orderedQuantity` instead of `acceptedQuantity`
**Fix**: Primary source is now `acknowledgementStatus.acceptedQuantity` with fallback

## Business Rules Now Enforced

| Scenario | Result | Qty on Picklist |
|----------|--------|-----------------|
| Ordered: 100, Accepted: 75, Cancelled: 25 | Line included | 75 units |
| Ordered: 100, Accepted: 0, Cancelled: 100 | Line excluded | — |
| Ordered: 100, Accepted: 100 | Line included | 100 units |
| Ordered: 50, Accepted: 0 (fresh PO) | Line included | 50 units |
| Fully OOS item (accepted: 75) | Line included + [OOS] badge | 75 units (counted) |

## Testing Checklist

### 1. Preview Modal
- [ ] Open Vendor POs tab
- [ ] Select one or more POs with mixed acceptance
- [ ] Click "Export Pick List" button
- [ ] Preview modal opens
- [ ] Check that:
  - **Total Units** matches sum of "Total Qty" column
  - **[OOS] badges** only appear on real OOS items
  - **No strike-through** on accepted items
  - **Fully rejected lines** do not appear at all

### 2. PDF Export
- [ ] From preview, click "Download PDF"
- [ ] Open PDF
- [ ] Check that:
  - Summary line shows correct unit count (matches preview)
  - No fully rejected lines appear
  - Quantities match preview

### 3. Specific Test Cases

#### Test Case 1: PO with Accepted & Rejected
1. Find a PO with some accepted and some rejected lines
2. Expected: Only accepted lines in picklist
3. Verify: Total = sum of accepted only

#### Test Case 2: All Rejected PO
1. Find a PO where all lines are rejected
2. Expected: Empty picklist (0 lines, 0 units)
3. Verify: Summary shows "No items" or empty table

#### Test Case 3: Partially Accepted Line
1. Find a line ordered 100, accepted 75, cancelled 25
2. Expected: Shows 75 units (not 100, not 50)
3. Verify: Correct math applied

#### Test Case 4: Real OOS Items
1. If available, add an item that's in OOS inventory
2. Expected: Shows [OOS] badge, strike-through styling
3. Expected: Still counted in total units
4. Verify: Visually distinct but included in pick count

### 4. Regression Tests (Should Still Work)
- [ ] Regular picklist with all accepted items works
- [ ] Multiple PO selection works
- [ ] PDF generation completes
- [ ] No console errors in browser DevTools
- [ ] Vendor POs tab quantities unchanged
- [ ] OOS tab unchanged
- [ ] Notifications tab unchanged
- [ ] Barcode lookup unchanged

## Implementation Details

### Backend Logic (services/picklist_service.py)

```python
# Key change: Rejection detection
if accepted_qty == 0 and ordered_qty > 0:
    fully_rejected_lines.add(key)

# Key change: Quantity selection
if has_accepted_qty:
    use_accepted_qty
else:
    use_ordered_qty  # Fresh PO

# Key change: Total units
total_units += int(accepted_qty)  # Always add accepted
```

### Frontend Logic (ui/index.html)

```javascript
// No client-side filtering of rejected lines
const items = (data.items || []);

// Simple sort by quantity
items.sort((a, b) => (b.totalQty || 0) - (a.totalQty || 0));

// OOS flag only triggers visual styling
const isOos = it.isOutOfStock;
const rowStyle = isOos ? 'style="opacity:0.6; background:#fff5f5;"' : '';
```

## Backward Compatibility
✅ No endpoint changes
✅ No response structure changes  
✅ Same field names (`totalQty`, `isOutOfStock`, etc.)
✅ Just with correct values

## Files Modified
1. `services/picklist_service.py` - Core logic fix
2. `ui/index.html` - UI simplification

## Expected Outcome
- Picklist preview modal shows correct quantities
- PDF export matches preview exactly
- All counts match main Vendor POs tab
- Rejected lines do not appear
- Real OOS items are visually distinct but still counted
- No errors in browser console or server logs
