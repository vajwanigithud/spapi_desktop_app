# Timezone Hardening - Manual Testing Checklist

## Pre-Testing Verification
- [ ] App starts without errors on startup
- [ ] No `ZoneInfoNotFoundError` or `ModuleNotFoundError: tzdata` in logs
- [ ] Console shows no import-time errors

## Module Import Tests
- [ ] Run: `python -c "from services.vendor_realtime_sales import UAE_TZ; print(UAE_TZ)"`
  - Expected output: `Asia/Dubai` (if tzdata installed) or `UTC+04:00` (fallback)

## Time Conversion Tests
- [ ] Run: `python -c "from datetime import datetime, timezone; from services.vendor_realtime_sales import utc_to_uae_str; dt = datetime.now(timezone.utc); print(f'UTC: {dt}'); print(f'UAE: {utc_to_uae_str(dt)}')"`
  - Expected: UAE time should be 4 hours ahead of UTC
  - Example: UTC 06:00 → UAE 10:00

## UI Tests (In-Browser)

### Lookback Selection
- [ ] Navigate to "Vendor Real Time Sales" tab
- [ ] Verify "Lookback" dropdown has these options:
  - [ ] Trailing 2 hours
  - [ ] Trailing 4 hours
  - [ ] Trailing 8 hours
  - [ ] Trailing 12 hours
  - [ ] Trailing 24 hours
  - [ ] Trailing 48 hours

### View By Selection
- [ ] Verify "View By" dropdown has these options:
  - [ ] ASIN
  - [ ] Time

### Time Display
- [ ] Select "Trailing 2 hours" lookback
- [ ] Check the info label above the table
  - Expected format: "Trailing 2 hours (HH:MM → HH:MM UAE)"
  - Example: "Trailing 2 hours (06:00 → 08:00 UAE)" if current time is 08:00 UTC / 12:00 UAE
- [ ] Times shown should be in UAE timezone
- [ ] Two times should be exactly 2 hours apart

## API Response Tests (Using DevTools or curl)

### Test Endpoint with Lookback
- [ ] Call: `GET /api/vendor-realtime-sales/summary?lookback_hours=2&view_by=asin`
- [ ] Verify response includes:
  - [ ] `lookback_hours: 2`
  - [ ] `view_by: "asin"`
  - [ ] `window` object with:
    - [ ] `start_utc` (ISO format with Z suffix)
    - [ ] `end_utc` (ISO format with Z suffix)
    - [ ] `start_uae` (ISO format with +04:00 offset)
    - [ ] `end_uae` (ISO format with +04:00 offset)
  - [ ] `total_units` (integer)
  - [ ] `total_revenue` (float)
  - [ ] `currency_code` (string, should be "AED")
  - [ ] `rows` array with ASIN data
- [ ] Verify times are correct:
  - [ ] `end_utc` - `start_utc` = approximately 2 hours
  - [ ] `end_uae` - `start_uae` = approximately 2 hours (same difference)
  - [ ] Each UAE time is 4 hours ahead of corresponding UTC time

### Test with Different Lookback Values
- [ ] `lookback_hours=4` → should show 4-hour window
- [ ] `lookback_hours=24` → should show 24-hour window
- [ ] `lookback_hours=48` → should show 48-hour window

### Test View By Time
- [ ] Call: `GET /api/vendor-realtime-sales/summary?lookback_hours=2&view_by=time`
- [ ] Verify response includes:
  - [ ] `view_by: "time"`
  - [ ] `rows` array with hourly buckets
  - [ ] Each row has: `bucket_start_utc`, `bucket_end_utc`, `bucket_start_uae`, `bucket_end_uae`
  - [ ] Each row has: `units`, `revenue`
- [ ] Verify times are consistent with ASIN view

### Test Invalid Lookback
- [ ] Call: `GET /api/vendor-realtime-sales/summary?lookback_hours=5` (invalid)
- [ ] Expected: HTTP 400 error with message about valid values

### Test Invalid View By
- [ ] Call: `GET /api/vendor-realtime-sales/summary?lookback_hours=2&view_by=invalid`
- [ ] Expected: HTTP 400 error

## ASIN Detail Tests
- [ ] Click on an ASIN row in the table
- [ ] Verify times displayed for that ASIN are in UTC (hour_start_utc, hour_end_utc)
- [ ] Verify data is subset of the summary window

## Data Consistency Tests
- [ ] Verify that times in the table header match the API response times
- [ ] Verify that aggregated units/revenue add up correctly
- [ ] Verify that no data is missing when changing lookback hours
- [ ] Verify that view switching (ASIN ↔ Time) shows consistent data

## Edge Case Tests
- [ ] Test at midnight UTC (00:00 UTC = 04:00 UAE)
- [ ] Test at noon UTC (12:00 UTC = 16:00 UAE)
- [ ] Test spanning midnight in UAE (23:00 UTC = 03:00 next day UAE)
- [ ] Test with empty data window (should return empty rows but valid structure)

## Browser Console Tests
- [ ] Open DevTools (F12)
- [ ] Check Console tab for any errors or warnings
- [ ] Expected: No JavaScript errors related to timezone
- [ ] No warnings about `Asia/Dubai` or `tzdata`

## Fallback Mode Testing (Optional - requires uninstalling tzdata)
If you want to test the fallback mode without installing tzdata:

1. Open a separate Python session
2. Run: `pip uninstall tzdata -y`
3. Restart the app
4. Repeat all above tests
5. Verify everything still works (times will still be correct via UTC+4)
6. Re-install tzdata: `pip install tzdata` (if desired)

## Regression Tests
- [ ] Verify other tabs still work (Vendor POs, Catalog Fetcher, OOS Items)
- [ ] Verify no performance regression in Real-Time Sales queries
- [ ] Verify no database schema changes or corruption
- [ ] Verify SP-API quota/cooldown logic unaffected

## Documentation Tests
- [ ] Verify this checklist makes sense and is comprehensive
- [ ] Check that TIMEZONE_HARDENING_SUMMARY.md accurately describes changes
- [ ] Verify no README conflicts with new timezone handling

## Test Results
- Date Tested: ___________
- Tester: ___________
- Overall Status: [ ] PASS [ ] FAIL

### Notes:
(Use this space to document any issues found during testing)
