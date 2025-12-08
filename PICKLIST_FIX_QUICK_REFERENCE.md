# Quick Reference: Picklist Bug Fixes

## Two Bugs Fixed

### 1️⃣ PDF Download Bug
**Problem:** PDF opened in browser instead of downloading  
**Cause:** Wrong Content-Disposition header (`inline` instead of `attachment`)  
**Files:** `main.py` lines 1733, 1754  
**Status:** ✓ FIXED

### 2️⃣ Empty Picklist Preview Bug
**Problem:** Preview showed 0 items when PO items were marked OOS  
**Cause:** Code filtered out all OOS items (used `continue`)  
**Files:** `services/picklist_service.py`, `ui/index.html`  
**Status:** ✓ FIXED

---

## What Changed

| Feature | Before | After |
|---------|--------|-------|
| **PDF Download** | Opens in viewer | Downloads as file |
| **Picklist Preview** | Empty (0 items) | Shows all items |
| **OOS Items** | Hidden | Visible with [OOS] label |
| **Item Sorting** | By quantity | Available first, then OOS |
| **Visual Indicators** | None | Red text, strikethrough, opacity |

---

## Testing the Fixes

### Test PDF Download
1. Select a PO
2. Click "Export Pick List (PDF)"
3. Click "Download PDF" in modal
4. ✓ File should download as `picklist.pdf`

### Test Empty Picklist Fix
1. Select a PO that has OOS items
2. Click "Export Pick List (PDF)"
3. ✓ Preview should show items (not empty!)
4. ✓ OOS items marked with red `[OOS]` label

---

## Code Changes Summary

**Backend (picklist_service.py):**
```python
# Before: Skip OOS items
if key_po_asin in oos_keys:
    continue  # Item disappears

# After: Include OOS items with flag
is_oos = key_po_asin in oos_keys
consolidated[ckey] = {
    ...
    "isOutOfStock": is_oos,  # ← New flag
}
```

**Frontend (index.html):**
```javascript
// Before: Items disappear if OOS
items = items.filter(it => !it.isOutOfStock);

// After: Show items with visual indicators
const isOos = it.isOutOfStock;
const oosLabel = isOos ? '[OOS]' : '';
const rowStyle = isOos ? 'opacity:0.6; color:red;' : '';
```

---

## Impact

✓ **Users can now:** See all items in picklist with OOS status clearly marked  
✓ **PDF downloads:** Properly as a file, not opened in browser  
✓ **Total units:** Accurately reflects only available (non-OOS) items  
✓ **Sorting:** Non-OOS items shown first for quick reference  

---

## Files Modified

```
main.py                      (+2 lines)
├─ Line 1733: Add headers to POST endpoint
└─ Line 1754: Fix inline → attachment

services/picklist_service.py (+49 lines)
├─ Lines 128-161: Include OOS items with flag
└─ Lines 163-178: Set isOutOfStock property

ui/index.html                (+15 lines)
├─ Lines 1774-1779: Better sorting
└─ Lines 1790-1805: OOS visual indicators
```

---

## Quick Start

No configuration needed! Changes are automatic:

1. Restart the app
2. Select a PO with items
3. Click "Export Pick List (PDF)"
4. See all items with OOS status marked
5. Download PDF properly

---

## Questions?

- **Why are items showing as OOS?** They were marked OOS through the "Mark OOS" button or automatic rejection detection
- **Can I unmark items as OOS?** Yes, use the "Restock" endpoint or UI button
- **Does this affect PDF export?** No, PDF still only includes non-OOS items as intended
- **Are there API changes?** No, completely backward compatible
