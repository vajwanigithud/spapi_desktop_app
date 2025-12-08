# Export PickList (PDF) - Bug Report

## Summary
The "Export Pick List (PDF)" feature in the Vendor PO tab has **2 critical bugs** that prevent proper PDF generation and download.

---

## Bug #1: Missing Content-Disposition Header in GET Endpoint
**Location:** `main.py`, line 1753 in `picklist_pdf_get()` function

**Problem:**
The GET endpoint returns the PDF with `Content-Disposition: inline` instead of `attachment`. This tells the browser to display the PDF inline rather than prompt for download.

**Current Code (WRONG):**
```python
headers = {"Content-Disposition": 'inline; filename="picklist.pdf"'}
return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
```

**Issue:**
- `inline` = browser tries to display the PDF in a new tab
- User expects a download dialog, not an inline viewer
- Some browsers may fail to handle this properly

**Fix:**
Change `inline` to `attachment`:
```python
headers = {"Content-Disposition": 'attachment; filename="picklist.pdf"'}
return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
```

---

## Bug #2: Missing Content-Disposition Header in POST Endpoint
**Location:** `main.py`, line 1733 in `picklist_pdf()` function (POST endpoint)

**Problem:**
The POST endpoint doesn't include a `Content-Disposition` header at all, so the PDF won't download properly.

**Current Code (WRONG):**
```python
pdf_bytes = generate_picklist_pdf(po_numbers, items, summary)
return Response(content=pdf_bytes, media_type="application/pdf")
```

**Issue:**
- No filename suggestion for the download
- Browser behavior is undefined (may try to display inline or not save at all)
- Inconsistent with the GET endpoint

**Fix:**
Add the Content-Disposition header:
```python
pdf_bytes = generate_picklist_pdf(po_numbers, items, summary)
headers = {"Content-Disposition": 'attachment; filename="picklist.pdf"'}
return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
```

---

## How the Feature Currently Works

1. **UI Flow** (`ui/index.html`):
   - User clicks "Export Pick List (PDF)" button → calls `openPicklistPreview()`
   - Modal opens with preview of consolidated items from selected POs
   - User clicks "Download PDF" button → calls `downloadPicklistPdf()`
   - Calls GET endpoint: `/api/picklist/pdf?poNumbers=PO1,PO2,...`

2. **Backend Flow** (`main.py`):
   - GET endpoint `picklist_pdf_get()` (line 1736):
     - Receives comma-separated PO numbers from query string
     - Calls `consolidate_picklist()` to aggregate items
     - Generates PDF using `generate_picklist_pdf()`
     - Returns Response with PDF bytes
   
   - POST endpoint `picklist_pdf()` (line 1714):
     - Receives JSON payload with `purchaseOrderNumbers` array
     - Does the same consolidation and PDF generation
     - Returns Response with PDF bytes (but missing headers)

3. **Affected Code**:
   - `picklist_service.py` (lines 190-266): `generate_picklist_pdf()` function works correctly
   - Rendering logic is sound, only the HTTP response headers are wrong

---

## Required Dependencies
- ✅ `reportlab` 4.4.5 is installed and available
- `REPORTLAB_AVAILABLE` flag is set correctly
- No missing imports or dependencies

---

## Testing Notes

**To reproduce the bug:**
1. Open the desktop app
2. Go to Vendor PO tab
3. Select one or more POs
4. Click "Export Pick List (PDF)" button
5. Review preview and click "Download PDF"
6. Expected: PDF file downloads with filename `picklist.pdf`
7. Actual: PDF opens in browser tab or fails to download

**After fix:**
- PDF should prompt download with filename `picklist.pdf`
- Both GET and POST endpoints should work identically
- Browser should treat it as a file download, not inline display

---

## Files That Need Changes

| File | Lines | Change |
|------|-------|--------|
| `main.py` | 1753 | Change `inline` to `attachment` |
| `main.py` | 1732-1733 | Add `Content-Disposition` header to POST endpoint |

---

## Impact
- **Severity**: HIGH - Core feature is broken
- **Affected Users**: Anyone using the "Export Pick List (PDF)" feature
- **Fix Complexity**: LOW - Simple header change (2 lines)
