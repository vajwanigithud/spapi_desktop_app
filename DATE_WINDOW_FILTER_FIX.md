# Date Window Filter Fix for Vendor POs Tab

## Problem
After the recent Vendor Central-style refactor, the "60 Days / 30 Days / 15 Days" dropdown in the Vendor POs toolbar no longer filtered the table. Changing the dropdown had no effect on the displayed POs.

## Root Cause
The `filterPOs()` function in ui/index.html was not including the date window filter logic. Although the dropdown element existed with ID `po-start-days`, there was:
1. No event listener attached to it
2. No date filtering logic in the `filterPOs()` function itself

## Solution
Made two surgical changes to ui/index.html:

### 1. Updated `filterPOs()` function (lines 1026-1091)
- Added retrieval of the `po-start-days` dropdown element
- Extract the days value from the dropdown (defaults to 60)
- Calculate a cutoff date: `now - daysWindow`
- Added date window filter logic in the main filter loop:
  - Extract orderDate from PO (from `purchaseOrderDate` or `orderDetails.purchaseOrderDate`)
  - Parse the date and compare against cutoff
  - Exclude any PO with an orderDate before the cutoff
  - Safely handle invalid or missing dates

### 2. Added event listener (line 2105)
- Attached the `filterPOs` function to the `po-start-days` dropdown's `change` event
- Now changing the dropdown immediately re-filters the table

## Behavior After Fix
- **60 Days** (default): Shows POs ordered in the last 60 days
- **30 Days**: Shows POs ordered in the last 30 days  
- **15 Days**: Shows POs ordered in the last 15 days
- The date filter **works together with** existing filters:
  - Search text (PO / SKU / F/C / Status)
  - F/C filter dropdown
  - Status filter dropdown
- Status counters at the top (Pending / Preparing / Appointment Scheduled / Delivered) are recalculated based on the filtered list

## Implementation Details
- Uses `new Date()` for date calculations (JavaScript Date object handles timezone correctly in browser context)
- Date comparison checks: `orderDate < cutoff` (strict less-than)
- Gracefully handles missing/invalid dates: POs without orderDate are included (not filtered out)
- Cutoff calculation respects calendar days (not hours): `new Date(year, month, date - days)`

## Testing
To verify the fix works:
1. Reload the app
2. Note the count of POs on the "60 Days" view
3. Change dropdown to "30 Days" → count should decrease or stay same
4. Change to "15 Days" → count should decrease or stay same
5. Change back to "60 Days" → count should return to original
6. Verify other filters (search, F/C, Status) still work in combination with date window
7. Verify status summary counts respect the date window filter

## Files Changed
- **ui/index.html**: 
  - Line 1030: Added `dateWindowSelect` variable
  - Line 1035: Extract days value from dropdown
  - Lines 1037-1039: Calculate cutoff date
  - Lines 1049-1056: Add date window filter logic
  - Line 2105: Add event listener for `po-start-days` change
