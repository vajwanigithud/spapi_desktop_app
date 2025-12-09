# Vendor Real Time Sales Audits – Testing Checklist

## Pre-Test
- [ ] `python -m py_compile main.py services/db.py services/vendor_realtime_sales.py` passes
- [ ] No import errors
- [ ] All three files syntax-check clean

## Startup Test
- [ ] Start app
- [ ] Check logs for `[RTSalesStartupBackfill] Starting startup backfill...`
- [ ] Confirm app doesn't hang on startup (should return immediately)
- [ ] Confirm Electron window connects quickly (no "refused to connect" errors)
- [ ] Watch for `[RTSalesStartupBackfill] Finished startup backfill: rows=..., asins=..., hours=...`

## First 15-Minute Cycle
- [ ] App runs for ~15 min
- [ ] Check logs for normal cycle operations:
  - `[RTSalesAutoSync] Cycle complete: ...` (gap detection + recent tail)
  - `[RTSalesAutoSync] Daily audit done: rows=..., asins=..., hours=24`
  - `[RTSalesAutoSync] Weekly audit done: rows=..., asins=..., hours=168`

## Database State
- [ ] Open SQLite CLI: `sqlite3 catalog.db`
- [ ] Run: `SELECT * FROM vendor_rt_sales_state;`
- [ ] Confirm columns exist:
  - `marketplace_id` (should have value)
  - `last_ingested_end_utc` (should have ISO timestamp)
  - `last_daily_audit_utc` (should have ISO timestamp)
  - `last_weekly_audit_utc` (should have ISO timestamp)

## Audit Re-run Prevention
- [ ] Wait 15 minutes for next cycle
- [ ] In logs, confirm cycle runs but NO new daily/weekly audit messages
- [ ] This is correct: audits only run once per day/week
- [ ] Log should show: `[RTSalesAutoSync] Cycle complete: ...` but NOT the audit lines

## Manual Verification
- [ ] Run: `SELECT COUNT(*) FROM vendor_realtime_sales;` (should have rows)
- [ ] Run: `SELECT COUNT(DISTINCT asin) FROM vendor_realtime_sales;` (multiple ASINs)
- [ ] Run: `SELECT COUNT(DISTINCT hour_start_utc) FROM vendor_realtime_sales;` (multiple hours)
- [ ] Sample query to verify UPSERT idempotency:
  ```sql
  SELECT asin, hour_start_utc, COUNT(*) as cnt
  FROM vendor_realtime_sales
  GROUP BY asin, hour_start_utc
  HAVING cnt > 1;
  ```
  Should return 0 rows (no true duplicates)

## UI Test
- [ ] Open Vendor Real Time Sales tab
- [ ] Click Refresh Now (manual refresh still works)
- [ ] Verify table shows data with images, units, revenue
- [ ] Confirm no errors in DevTools console

## Long-Running Test (optional)
- [ ] Let app run for >24 hours and check if daily audit re-runs
- [ ] Wait >7 days and check if weekly audit re-runs
- [ ] Confirm only ONE daily and ONE weekly audit per day/week respectively

## Error Handling Test
- [ ] Artificially break spapi_client call in audit (comment out some SP-API logic)
- [ ] Confirm loop catches exception and logs error
- [ ] Confirm app doesn't crash
- [ ] Next cycle should retry

## Logging Verification
Look for these prefixes in logs:
- [ ] `[RTSalesStartupBackfill]` – startup phase
- [ ] `[RTSalesAutoSync]` – normal 15-min cycles
- [ ] `[VendorRtSalesBackfill]` – gap detection/backfill (if gaps detected)
- [ ] `[VendorRtSalesAudit]` – audit operations (daily, weekly)

## Final Checks
- [ ] Vendor POs tab still works
- [ ] Catalog Fetcher still works
- [ ] Pick List / PDF export still works
- [ ] Notifications tab unaffected
- [ ] No performance degradation
- [ ] API response shape unchanged

## Sign-Off
- [ ] All tests passed ✓
- [ ] App is production-ready
- [ ] Audits running reliably
- [ ] Logging is clear and informative
