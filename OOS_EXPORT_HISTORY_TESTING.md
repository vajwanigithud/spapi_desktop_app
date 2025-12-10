# Out-of-Stock Export History - Testing Guide

## Pre-Test Checklist

- [ ] Code deployed to main.py, services/db.py, ui/index.html
- [ ] App restarted (triggers table creation)
- [ ] No Python errors in console
- [ ] Database file (catalog.db) exists

## Test Scenarios

### Test 1: Initial OOS List Load

**Steps:**
1. Open browser, navigate to app
2. Click "Out-of-Stock Items" tab
3. Observe the OOS items table

**Expected Results:**
- [ ] All OOS items display in table
- [ ] Each row shows "Pending for export" in Actions column
- [ ] Text is amber colored and bold
- [ ] "Pending" indicates never been exported
- [ ] Rows are normal opacity

**If FAILS:**
- Check browser console for JS errors
- Verify /api/oos-items returns `export_status` field
- Verify database table was created: `sqlite3 catalog.db "SELECT name FROM sqlite_master WHERE type='table' AND name='vendor_oos_export_history';"`

---

### Test 2: First Export

**Precondition:** Have at least 5 OOS items (or add some)

**Steps:**
1. Note which ASINs are in the list
2. Click "Export OOS (XLS)" button
3. Observe status message
4. Wait for download (browser's download folder)
5. Check file contents

**Expected Results:**
- [ ] Status shows "Exporting..." immediately
- [ ] File downloads as "oos_items.xls" (TSV format)
- [ ] File contains ASIN column header
- [ ] File lists all pending ASINs (the ones from step 1)
- [ ] File contains NO duplicates
- [ ] No errors in console

**Check file contents:**
```bash
# On Windows
type %USERPROFILE%\Downloads\oos_items.xls
# Should show:
# asin
# B001AAAA
# B002BBBB
# ... etc
```

**If FAILS:**
- Check main.py export endpoint for exceptions
- Verify pending_asins list is being built correctly
- Check CSV writer is working

---

### Test 3: Export History Recording

**Precondition:** Just completed Test 2

**Steps:**
1. Wait 2 seconds (async processing)
2. Refresh the OOS tab (click "Reload" button)
3. Observe the Actions column

**Expected Results:**
- [ ] Previously exported ASINs now show "Exported" status
- [ ] Text is greyed out (muted color)
- [ ] Rows with "Exported" are slightly faded (opacity 0.7)
- [ ] Status message shows count: "Export complete. X total items marked as exported."

**Check database directly:**
```bash
# Connect to database
sqlite3 catalog.db

# Query export history
SELECT COUNT(*) FROM vendor_oos_export_history;
# Should show: number matching exported ASINs

SELECT DISTINCT asin FROM vendor_oos_export_history;
# Should list all exported ASINs
```

**If FAILS:**
- Check database table exists: `\.tables` in sqlite3
- Verify mark_oos_asins_exported() is being called
- Check backend logs for errors

---

### Test 4: Second Export (Only New ASINs)

**Precondition:** Just completed Test 3, add new OOS items

**Steps:**
1. Add new OOS items (or they may exist naturally)
2. Verify new items show "Pending for export"
3. Click "Export OOS (XLS)" again
4. Check file contents
5. Refresh list

**Expected Results:**
- [ ] Export file contains ONLY new pending ASINs
- [ ] Previously exported ASINs NOT in file
- [ ] New batch_id created (different from first export)
- [ ] After refresh, new ASINs now show "Exported"
- [ ] Original exported ASINs still show "Exported"

**Check database:**
```sql
SELECT DISTINCT export_batch_id FROM vendor_oos_export_history;
# Should show 2 different UUIDs
```

**If FAILS:**
- Verify is_asin_exported() is filtering correctly
- Check that new batch_id is generated
- Verify pending_asins list contains only new items

---

### Test 5: Edge Case - No Pending ASINs

**Precondition:** All OOS items are already exported

**Steps:**
1. Click "Export OOS (XLS)" button again (when everything is exported)
2. Observe status
3. Check downloaded file

**Expected Results:**
- [ ] Download still succeeds (no error)
- [ ] File downloads with just header: "asin"
- [ ] No data rows (empty CSV)
- [ ] Status message shows "Export complete. 0 total items marked as exported."
- [ ] No errors in console or logs

**If FAILS:**
- Verify empty CSV is being handled gracefully
- Check for any exception handling issues

---

### Test 6: App Restart - Persistence

**Precondition:** Export history has been created

**Steps:**
1. Note which ASINs are exported
2. Restart the app (stop and start)
3. Open browser, go to OOS tab
4. Click "Reload"

**Expected Results:**
- [ ] Export status persists after restart
- [ ] Previously exported ASINs still show "Exported"
- [ ] No status resets
- [ ] Database records still present

**Check database after restart:**
```sql
SELECT COUNT(*) FROM vendor_oos_export_history;
# Should be same as before restart
```

**If FAILS:**
- Verify database file is not being reset
- Check table initialization on startup
- Verify data persistence in SQLite

---

### Test 7: Multiple Exports - Batch Tracking

**Steps:**
1. Export set of ASINs (Batch A)
2. Add more OOS items
3. Export again (Batch B)
4. Query database for batch IDs

**Expected Results:**
- [ ] Each export has unique batch_id
- [ ] Batch A and B are different UUIDs
- [ ] Can trace which export each ASIN came from

**Check database:**
```sql
SELECT asin, export_batch_id, exported_at 
FROM vendor_oos_export_history 
ORDER BY exported_at;
```

**If FAILS:**
- Verify uuid.uuid4() is generating unique IDs
- Check batch_id is being stored correctly

---

### Test 8: UI Styling Verification

**Steps:**
1. Look at OOS table with mixed pending/exported items
2. Observe styling

**Expected Results:**
- [ ] Pending items: amber text (#d97706), bold, normal row opacity
- [ ] Exported items: greyed text (#6b7280), faded row (opacity 0.7)
- [ ] Clear visual distinction between states
- [ ] All rows visible (no hiding/filtering)

**If FAILS:**
- Check CSS classes are applied correctly
- Verify export_status values match CSS selectors
- Check HTML rendering in browser inspector

---

### Test 9: Cross-Tab Navigation

**Steps:**
1. Export OOS items
2. Navigate to different tab
3. Return to OOS tab
4. Observe list

**Expected Results:**
- [ ] Status persists when leaving/returning to tab
- [ ] No refresh artifacts or state loss
- [ ] All existing functionality still works

**If FAILS:**
- Check that tab switching doesn't reset state
- Verify API is called fresh when returning

---

### Test 10: Browser Console Check

**Throughout all tests:**
- [ ] No JavaScript errors in console
- [ ] No network errors (HTTP 5xx)
- [ ] No warnings about missing elements

**Check console (F12 → Console tab):**
- Should be clean (no red errors)
- Watch network tab during export (should see 200 responses)

---

## Rollback Procedure

If something breaks:

1. **Restore files:**
   ```bash
   git checkout main.py services/db.py ui/index.html
   ```

2. **Restart app**

3. **Check database:**
   - If export_history table exists, it won't be removed (manual cleanup may be needed)
   - Can be safely dropped if needed:
     ```sql
     DROP TABLE vendor_oos_export_history;
     ```

---

## Success Criteria

✅ **All tests pass if:**

1. ✅ OOS items load with export_status field
2. ✅ All items show "Pending" initially
3. ✅ Export file contains pending ASINs only
4. ✅ Export history recorded in database
5. ✅ After export, status updates to "Exported"
6. ✅ Second export skips already-exported ASINs
7. ✅ Empty export handled gracefully
8. ✅ History persists after app restart
9. ✅ Each export has unique batch_id
10. ✅ UI styling shows clear distinction
11. ✅ No JavaScript console errors
12. ✅ All other tabs still work

---

## Debugging Commands

### Check database
```bash
sqlite3 catalog.db

# List all tables
.tables

# Verify export_history table
SELECT sql FROM sqlite_master WHERE type='table' AND name='vendor_oos_export_history';

# Count exports
SELECT COUNT(*) FROM vendor_oos_export_history;

# List all exports
SELECT asin, marketplace_id, exported_at, export_batch_id FROM vendor_oos_export_history;
```

### Check logs
```bash
# Look for [VendorOOS] or [DB] log messages
# Errors would show here
tail -f logs/*.log
```

### Quick import test
```python
# Verify functions import
from services.db import is_asin_exported, mark_oos_asins_exported
from services.db import ensure_oos_export_history_table

# Test is_asin_exported
result = is_asin_exported("B001AAAA")
print(f"Is B001AAAA exported? {result}")
```

---

## Expected Timeline

- Table creation: < 1 second (on startup)
- First export: 1-2 seconds
- Refresh after export: 1-2 seconds
- Database record creation: immediate

---

**Testing Complete When:**
All 10 test scenarios pass and success criteria are met ✅
