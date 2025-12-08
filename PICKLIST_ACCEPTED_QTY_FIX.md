# Picklist Empty with Accepted Quantity > 0 - Bug Fix

## Issue
When PO items had a rejection status but with `accepted_qty > 0`, the picklist showed them as 0 items. These items should appear in the picklist since they were partially accepted.

## Root Cause
The picklist service was marking ALL rejected lines as Out-of-Stock (OOS) regardless of their accepted quantity. The logic did not check if items had `accepted_qty > 0`.

**Problematic Logic:**
```python
# Old code - marks ALL rejected items as OOS
if asin and (is_rejected_line(it) or key_po_asin in rejected_line_keys):
    added = upsert_oos_entry_fn(...)  # ← Always added to OOS
```

This meant:
- Item ordered: 10 units
- Item rejected: 5 units  
- Item accepted: 5 units ← **Should show in picklist!**
- But was marked OOS anyway ← **Bug**

## Fix Applied
**File:** `services/picklist_service.py` (Lines 47-82, 133-162)

### Part 1: Rejected Lines Lookup (Lines 47-82)
Check `accepted_qty` before marking as OOS:

```python
if accepted_qty <= 0:
    # Only mark as OOS if no accepted quantity
    added = upsert_oos_entry_fn(...)
else:
    # Skip OOS marking if accepted_qty > 0
    # Item will be processed normally below
    pass
```

### Part 2: Item Processing (Lines 133-162)
When processing items from cache, check if they have accepted quantity before marking as OOS:

```python
if asin and key_po_asin in rejected_line_keys:
    accepted_qty = 0
    ack = it.get("acknowledgementStatus") or {}
    if isinstance(ack, dict):
        accepted_qty = float(ack.get("acceptedQuantity") or 0)
    
    if accepted_qty > 0:
        pass  # Don't mark as OOS, process normally
    else:
        # Mark as OOS only if no accepted quantity
        added = upsert_oos_entry_fn(...)
        continue
```

## Before vs After

### Before Fix
```
PO: 6HTP1VPO
Status: Has rejected items but with accepted quantities

Picklist Preview:
POs: 1
Lines: 0          ← Empty!
Total Units: 0    ← Shows 0

[No items displayed]
```

### After Fix
```
PO: 6HTP1VPO  
Status: Rejected but 5 units accepted

Picklist Preview:
POs: 1
Lines: 1
Total Units: 5    ← Shows accepted quantity!

[Items table]
| ASIN        | SKU   | Qty |
|-------------|-------|-----|
| B0DKBMW4DZ  | 6976  | 5   | ← Now appears!
```

## Logic Flow

### Item Handling
```
For each item in PO:
  1. Is it in rejected_line_keys?
     ├─ YES: Check accepted_qty
     │   ├─ accepted_qty > 0? → Include in picklist ✓
     │   └─ accepted_qty = 0? → Mark as OOS
     └─ NO: Include in picklist normally ✓

  2. Is it marked as OOS?
     ├─ YES: Show with [OOS] indicator
     └─ NO: Show normally
```

## Database Fields Used

From `vendor_po_lines` table:
- `po_number` - PO identifier
- `asin` - Product ASIN
- `sku` - Vendor SKU
- `ordered_qty` - Original order quantity
- `accepted_qty` - **Quantity accepted by Amazon** ← Key field
- `cancelled_qty` - Cancelled quantity
- `shortage_qty` - Short quantity

## Key Logic Changes

| Aspect | Before | After |
|--------|--------|-------|
| Check accepted_qty | No | Yes |
| Mark rejected items with accepted_qty > 0 as OOS | Yes ❌ | No ✓ |
| Rejected items in picklist | Never | When accepted_qty > 0 |
| Item visibility | Missing | Complete |

## Test Cases

### Test Case 1: Fully Rejected Item
- Ordered: 10 units
- Accepted: 0 units
- Result: **NOT in picklist** (correctly marked OOS)

### Test Case 2: Partially Accepted Item
- Ordered: 10 units
- Accepted: 5 units
- Result: **IN picklist with qty=5** ✓ (new behavior)

### Test Case 3: Fully Accepted Item
- Ordered: 10 units
- Accepted: 10 units
- Result: **IN picklist with qty=10** ✓ (unchanged)

## Impact

✓ Picklist now shows all items with partial acceptance  
✓ Users can see what was actually accepted vs rejected  
✓ Empty picklist issue resolved  
✓ Accurate inventory representation  
✓ No breaking changes  

## Files Modified

`services/picklist_service.py`:
- Lines 47-82: Check accepted_qty in rejected lines lookup
- Lines 133-162: Check accepted_qty in item processing

## Edge Cases Handled

1. **Missing accepted_qty field** → Defaults to 0 (treated as rejected)
2. **Non-numeric accepted_qty** → Defaults to 0 via exception handling
3. **Missing acknowledgementStatus** → Defaults to 0 (treated as not accepted)
4. **NULL values in database** → Converted to 0 via COALESCE

## Backward Compatibility

✓ No API changes  
✓ No schema changes  
✓ Existing functionality preserved  
✓ Only changes behavior for rejected items with accepted_qty > 0  
