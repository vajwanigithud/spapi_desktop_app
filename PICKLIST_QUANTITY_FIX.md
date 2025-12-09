# Picklist Quantity Fix - Complete Implementation

## Problem Summary

The picklist (both preview and PDF) had three critical issues:

1. **Wrong quantity used**: Picklist was showing `orderedQuantity` instead of `acceptedQuantity`
2. **All items marked OOS**: Rejected/partial lines were being marked as out-of-stock, causing all rows to show [OOS] and strike-through styling
3. **Incorrect total units**: Summary showed 0 units because OOS items weren't counted and quantity math was wrong

## Root Cause Analysis

### Backend (services/picklist_service.py)

**Issue 1: Rejected Lines Treated as OOS**
- Old code: Added ALL lines with `accepted_qty < ordered_qty` to the OOS state
- This included partial rejections and cancelled items
- Result: Legitimate items shown as out-of-stock with red strike-through

**Issue 2: Using Ordered Instead of Accepted Quantity**
- Line 122-127: Retrieved `orderedQuantity` 
- Line 191: Added `qty_num` (which was ordered) to consolidated total
- Should use `acknowledgementStatus.acceptedQuantity` instead

**Issue 3: Total Units Calculation**
- Only counted non-OOS items when all items should be counted
- Since almost everything was marked OOS, total was effectively 0

### Frontend (ui/index.html)

The preview modal:
- Did client-side filtering of rejected lines (redundant)
- Sorted to group OOS items first (unnecessary overhead)
- Applied strike-through to all items marked `isOutOfStock`

## Solution Implementation

### 1. Backend Changes - services/picklist_service.py

#### Change 1: Smart Rejection Detection
```python
# OLD: Mark everything with accepted < ordered as rejected
rejected_line_keys: set[str] = set()
for row in rows:
    rejected_line_keys.add(key)  # Too broad!
    
# NEW: Only mark fully rejected lines (accepted = 0, ordered > 0)
fully_rejected_lines: set[str] = set()
for row in rows:
    accepted_qty = float(row.get("accepted_qty") or 0)
    ordered_qty = float(row.get("ordered_qty") or 0)
    if accepted_qty == 0 and ordered_qty > 0:
        fully_rejected_lines.add(key)
```

**Impact**: Fully rejected lines are completely excluded from picklist; partial acceptances are included with accepted quantity

#### Change 2: Use Accepted Quantity
```python
# OLD: Used orderedQuantity for all items
qty = it.get("orderedQuantity") or {}
qty_num = float(qty.get("amount"))
consolidated[ckey]["totalQty"] += qty_num

# NEW: Try accepted first, fall back to ordered
accepted_qty = 0
ack = it.get("acknowledgementStatus") or {}
if isinstance(ack, dict):
    acc_qty_obj = ack.get("acceptedQuantity") or {}
    accepted_qty = float(acc_qty_obj.get("amount") or 0)

if accepted_qty == 0:  # Fresh PO, no status yet
    qty = it.get("orderedQuantity") or {}
    accepted_qty = float(qty.get("amount") or 0)

consolidated[ckey]["totalQty"] += int(accepted_qty)
```

**Impact**: Picklist now shows what we're actually shipping (accepted), not what was originally ordered

#### Change 3: Fix Total Units Calculation
```python
# OLD: Only count non-OOS items
if not is_oos:
    total_units += qty_num

# NEW: Count all accepted items regardless of OOS status
total_units += int(accepted_qty)
```

**Impact**: Total units now shows true count of accepted items to pick

#### Change 4: Removed OOS Pollution
- Deleted entire `is_rejected_line()` function (30+ lines)
- Removed `upsert_oos_entry_fn` calls that were marking rejected lines as OOS
- Removed `new_oos_added` tracking and `save_oos_state_fn` call
- Result: OOS state only contains actual inventory shortages, not rejection artifacts

### 2. Frontend Changes - ui/index.html

#### Change 1: Remove Redundant Filtering
```javascript
// OLD: Backend already excluded, this was redundant
const items = (data.items || []).filter(it => {
  const ack = it.acknowledgementStatus || {};
  const conf = (ack.confirmationStatus || "").toUpperCase();
  return conf !== "REJECTED";  // Backend already did this!
});

// NEW: Trust backend filtering
const items = (data.items || []);
```

#### Change 2: Simplify Sorting
```javascript
// OLD: Grouped OOS items first (unnecessary)
items.sort((a, b) => {
  const aOos = a.isOutOfStock ? 1 : 0;
  const bOos = b.isOutOfStock ? 1 : 0;
  if (aOos !== bOos) return aOos - bOos;
  return (b.totalQty || 0) - (a.totalQty || 0);
});

// NEW: Just sort by quantity
items.sort((a, b) => (b.totalQty || 0) - (a.totalQty || 0));
```

**Impact**: Cleaner code, performance gain, correct display order

## Business Rules Implemented

1. **Rejected lines are excluded entirely**: If accepted_qty = 0 and ordered_qty > 0, the line is not on the picklist
2. **Partial acceptances are included**: If accepted_qty > 0, that item appears with accepted quantity
3. **Only real OOS shows [OOS]**: Only items in the actual OOS inventory get the [OOS] badge
4. **Summary totals are accurate**: 
   - Total Units = sum of all accepted quantities across all items
   - Total Lines = count of all accepted items
   - POs = count of selected purchase orders

## Validation & Testing

### Edge Cases Handled

1. **Fully rejected PO**
   - All lines have accepted_qty = 0
   - Picklist is empty (0 lines, 0 units)
   - Summary shows correctly

2. **Mixed acceptance**
   - Some lines fully accepted, some partial, some fully rejected
   - Picklist shows only accepted items with their accepted quantities
   - Total units = sum of accepted only

3. **Partial acceptance** (e.g., ordered 100, accepted 75, cancelled 25)
   - Line appears in picklist with quantity 75
   - Total = 75 (not 100, not 50)

4. **Fresh PO with no status**
   - Falls back to orderedQuantity
   - Shows correct quantity

5. **OOS inventory**
   - Items in real OOS state show [OOS] badge (red, strike-through)
   - But still counted in total (you know what you're picking despite shortage)

## Files Changed

1. **services/picklist_service.py** (118 lines of core logic)
   - Removed redundant OOS pollution code
   - Fixed quantity source (accepted not ordered)
   - Fixed total units calculation
   - Simplified rejection detection

2. **ui/index.html** (2 locations)
   - Removed redundant client-side filtering
   - Simplified sort logic

## Backward Compatibility

✅ **Fully backward compatible**
- No API endpoint changes
- No request/response structure changes
- Same `/api/picklist/preview` and `/api/picklist/pdf` endpoints
- Same `isOutOfStock`, `totalQty`, `asin`, `sku` fields
- Just with correct values now

## Performance Impact

✅ **Slight performance improvement**
- Removed redundant filtering, sorting, and OOS state mutations
- Cleaner logic flow
- No additional database queries

## Summary

The picklist now correctly:
- Shows **accepted quantities** (what we're shipping) instead of ordered
- Excludes **fully rejected lines** entirely (not even counted)
- Shows **real OOS only** (not rejection artifacts)
- Computes **accurate totals** matching the Vendor POs tab

Both preview modal and PDF export now have consistent, correct data.
