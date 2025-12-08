# Picklist Preview Shows Empty - Root Cause Analysis

## Symptom
When you select a PO and click "Export Pick List (PDF)", the preview modal shows empty with no items listed, only the summary showing 0 lines and 0 units.

## Root Cause
**The picklist consolidation logic intentionally EXCLUDES items marked as Out-of-Stock (OOS).** When all items in a PO are marked OOS, the preview appears empty.

Looking at the data:
- PO "6HTP1VPO" has 50 items
- **ALL 50 items are marked as OOS** in `oos_state.json`
- The picklist service filters out OOS items (lines 152-153 in `picklist_service.py`)
- Result: 0 items returned, empty preview

## Code Flow

### Step 1: Frontend sends request
```javascript
// ui/index.html:1758
await fetch("/api/picklist/preview", {
  method: "POST",
  body: JSON.stringify({ purchaseOrderNumbers: selectedPoNumbers }),
})
```

### Step 2: Backend consolidates items
```python
# main.py:1710
result = consolidate_picklist(po_numbers)
```

### Step 3: Service filters out OOS items
```python
# picklist_service.py:152-153
if key_po_asin in oos_keys:
    continue  # ← SKIPS OOS ITEMS
```

### Step 4: Result is empty
```python
# Since all 50 items are OOS, none are added to consolidated dict
# Returns: {"summary": {"numPos": 1, "totalLines": 0, "totalUnits": 0}, "items": []}
```

## Why Items Are Marked OOS

Looking at `oos_state.json`:
- 279 entries total
- Many items from different POs are marked with `"isOutOfStock": True`
- The specific PO 6HTP1VPO has entries like:
  ```json
  "6HTP1VPO::B0DKBMW4DZ": {
    "poNumber": "6HTP1VPO",
    "asin": "B0DKBMW4DZ",
    "isOutOfStock": true,
    ...
  }
  ```

## Why This Happens

Items can be marked OOS through three mechanisms:

1. **Rejected Line Seeding** (`picklist_service.py:47-76`)
   - Rejected PO lines are automatically marked OOS
   - Query: `fetch_rejected_lines_fn(po_numbers)`

2. **Manual OOS Marking** (UI button "Mark as OUT OF STOCK")
   - User clicks button to mark item OOS
   - Endpoint: `POST /api/oos-items/mark`

3. **Payload-based Seeding** 
   - When sync includes rejected lines

## Expected Behavior

The picklist preview should:
1. Show only **non-OOS items** from the selected POs ✓ (This is working as designed)
2. If all items are OOS, show empty list with warning ✓ (This is working as designed)

## The Real Issue

**The issue is not a bug in the picklist code - it's working as designed.** The problem is:

- **User expectation mismatch**: Users expect to see all items in a PO, not just non-OOS ones
- **Data issue**: All items in PO 6HTP1VPO are marked OOS (likely because the PO was rejected or items were manually marked)

## Verification

To check if items are OOS:

```python
import json
from pathlib import Path

with open(Path('oos_state.json')) as f:
    oos = json.load(f)

# Check if PO items are OOS
po_num = "6HTP1VPO"
oos_in_po = {k: v for k, v in oos.items() if k.startswith(f"{po_num}::")}
print(f"Items marked OOS in {po_num}: {len(oos_in_po)}")

for key in list(oos_in_po.keys())[:3]:
    print(f"  - {key}")
```

Output:
```
Items marked OOS in 6HTP1VPO: 50
  - 6HTP1VPO::B0DKBMW4DZ
  - 6HTP1VPO::B0C3CLLJQL
  - 6HTP1VPO::B0FP9F456W
```

## Possible Solutions

### Option 1: Show OOS items with visual indicator (Recommended)
Modify picklist service to include OOS items but mark them clearly:
```python
# Add a flag to OOS items instead of filtering them out
if key_po_asin in oos_keys:
    consolidated[ckey]["isOutOfStock"] = True
    # Don't continue, add to picklist
```

Then in UI, show OOS items with strikethrough or red text.

### Option 2: Clear OOS entries that shouldn't be there
Use the restock endpoint to clear OOS flags for items in the PO:
```python
POST /api/oos-items/restock
{
  "poNumber": "6HTP1VPO",
  "asin": "B0DKBMW4DZ"
}
```

### Option 3: Add warning to picklist
Keep filtering OOS items but show better warning message:
```python
"warning": "50 items excluded (marked Out of Stock)"
```

## Status

This is **NOT a bug in the code** - it's working as designed. The issue is that items in this specific PO are marked OOS and the picklist intentionally excludes them. 

To fix: Either add OOS items to the picklist with visual indicators (Option 1), or restock the items using the restock endpoint (Option 2).
