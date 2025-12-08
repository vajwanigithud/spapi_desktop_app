# Total Accepted Cost Fix - Quick Summary

## The Bug
**Old formula (WRONG):** Sum of unit prices only
```
Total = 25 + 4 + 55 + 5 + ... = INCORRECT
```

**New formula (CORRECT):** Sum of (accepted_quantity × unit_price)
```
Total = (2 × 25) + (13 × 4) + (5 × 55) + (2 × 5) + ... = 1,204.36 AED ✅
```

**PO 6HTP1VPO:** Now shows 1,204.36 AED (matches Vendor Central!)

---

## What Changed

### Backend (main.py)
1. **New function** `_compute_total_accepted_cost()` – Correctly calculates cost from accepted_qty × unit_price
2. **Modified** `_compute_vendor_central_columns()` – Uses the new function instead of buggy inline code
3. **Modified** `_aggregate_vendor_po_lines()` – Fetches per-line accepted quantities and passes them to cost calculator

### Database (services/db_repos.py)
- **New function** `get_vendor_po_line_details()` – Fetches ASIN and accepted_qty from vendor_po_lines table

### Frontend (ui/index.html)
- Updated cost rendering to use `po.total_accepted_cost` (with fallback to legacy field)
- Added proper number formatting with 2 decimal places

---

## Data Flow

```
PO Cache + vendor_po_lines DB
          ↓
_aggregate_vendor_po_lines()
          ↓
_compute_total_accepted_cost(po, accepted_line_map)
  For each item:
    accepted_qty × netCost.amount
          ↓
po["total_accepted_cost"] = 1204.36
po["total_accepted_cost_currency"] = "AED"
          ↓
UI displays: "AED 1,204.36"
```

---

## Files Changed

| File | What |
|------|------|
| main.py | Added/modified cost calculation functions |
| services/db_repos.py | Added `get_vendor_po_line_details()` |
| ui/index.html | Updated cost field rendering |

---

## Verification

✅ Compilation: PASSED
✅ No schema changes
✅ Backward compatible (legacy fields still present)
✅ Now matches Vendor Central values 1:1
