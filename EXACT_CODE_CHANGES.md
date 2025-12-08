# Code Changes - Exact Fixes Applied

## Fix #1: PDF Download Headers

### File: main.py, Line 1733 (POST Endpoint)

**BEFORE:**
```python
pdf_bytes = generate_picklist_pdf(po_numbers, items, summary)
return Response(content=pdf_bytes, media_type="application/pdf")
```

**AFTER:**
```python
pdf_bytes = generate_picklist_pdf(po_numbers, items, summary)
headers = {"Content-Disposition": 'attachment; filename="picklist.pdf"'}
return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
```

---

### File: main.py, Line 1754 (GET Endpoint)

**BEFORE:**
```python
headers = {"Content-Disposition": 'inline; filename="picklist.pdf"'}
return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
```

**AFTER:**
```python
headers = {"Content-Disposition": 'attachment; filename="picklist.pdf"'}
return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
```

---

## Fix #2: Rejected Items Lookup

### File: services/picklist_service.py, Lines 47-82

**BEFORE:**
```python
rejected_line_keys: set[str] = set()
try:
    with time_block("picklist_rejected_lookup"):
        rows = fetch_rejected_lines_fn(po_numbers) or []
    for row in rows:
        po_num = (row.get("po_number") or "").strip()
        asin = (row.get("asin") or "").strip()
        sku = (row.get("sku") or "").strip()
        key = f"{po_num}::{asin}"
        if po_num and asin:
            rejected_line_keys.add(key)
            if key in oos_keys:
                continue
            try:
                qty_num = float(row.get("ordered_qty") or 0)
            except Exception:
                qty_num = 0
            added = upsert_oos_entry_fn(
                oos_state,
                po_number=po_num,
                asin=asin,
                vendor_sku=sku or None,
                po_date=None,
                ship_to_party=None,
                qty=qty_num,
                image=(catalog.get(asin) or {}).get("image"),
            )
            if added:
                new_oos_added = True
                oos_keys.add(key)
except Exception as exc:
    logger.warning(f"[Picklist] Failed to load vendor_po_lines for rejection filter: {exc}")
```

**AFTER:**
```python
rejected_line_keys: set[str] = set()
try:
    with time_block("picklist_rejected_lookup"):
        rows = fetch_rejected_lines_fn(po_numbers) or []
    for row in rows:
        po_num = (row.get("po_number") or "").strip()
        asin = (row.get("asin") or "").strip()
        sku = (row.get("sku") or "").strip()
        key = f"{po_num}::{asin}"
        if po_num and asin:
            rejected_line_keys.add(key)
            if key in oos_keys:
                continue
            try:
                accepted_qty = float(row.get("accepted_qty") or 0)  # ← NEW
            except Exception:
                accepted_qty = 0  # ← NEW
            if accepted_qty <= 0:  # ← NEW
                try:
                    qty_num = float(row.get("ordered_qty") or 0)
                except Exception:
                    qty_num = 0
                added = upsert_oos_entry_fn(
                    oos_state,
                    po_number=po_num,
                    asin=asin,
                    vendor_sku=sku or None,
                    po_date=None,
                    ship_to_party=None,
                    qty=qty_num,
                    image=(catalog.get(asin) or {}).get("image"),
                )
                if added:
                    new_oos_added = True
                    oos_keys.add(key)
except Exception as exc:
    logger.warning(f"[Picklist] Failed to load vendor_po_lines for rejection filter: {exc}")
```

**Key changes:**
- Check `accepted_qty` from database
- Only mark as OOS if `accepted_qty <= 0`
- Skip OOS marking if `accepted_qty > 0`

---

## Fix #3: Item Processing with Accepted Quantity

### File: services/picklist_service.py, Lines 129-165

**BEFORE:**
```python
asin = it.get("amazonProductIdentifier") or ""
sku = it.get("vendorProductIdentifier") or ""
key_po_asin = f"{po_num}::{asin}" if asin else ""

if asin and (is_rejected_line(it) or key_po_asin in rejected_line_keys):
    try:
        if qty_num <= 0:
            qty_num = None
    except Exception:
        qty_num = None
    added = upsert_oos_entry_fn(
        oos_state,
        po_number=po_num,
        asin=asin,
        vendor_sku=sku or None,
        po_date=po_date,
        ship_to_party=ship_to,
        qty=qty_num,
        image=(catalog.get(asin) or {}).get("image"),
    )
    if added:
        new_oos_added = True
        oos_keys.add(key_po_asin)
    continue

if not asin:
    continue
```

**AFTER:**
```python
asin = it.get("amazonProductIdentifier") or ""
sku = it.get("vendorProductIdentifier") or ""
key_po_asin = f"{po_num}::{asin}" if asin else ""

if asin and key_po_asin in rejected_line_keys:  # ← CHANGED
    accepted_qty = 0  # ← NEW
    ack = it.get("acknowledgementStatus") or {}  # ← NEW
    if isinstance(ack, dict):  # ← NEW
        try:  # ← NEW
            accepted_qty = float(ack.get("acceptedQuantity") or 0)  # ← NEW
        except Exception:  # ← NEW
            accepted_qty = 0  # ← NEW
    if accepted_qty > 0:  # ← NEW
        pass  # ← NEW - don't mark as OOS
    else:  # ← NEW
        try:
            if qty_num <= 0:
                qty_num = None
        except Exception:
            qty_num = None
        added = upsert_oos_entry_fn(
            oos_state,
            po_number=po_num,
            asin=asin,
            vendor_sku=sku or None,
            po_date=po_date,
            ship_to_party=ship_to,
            qty=qty_num,
            image=(catalog.get(asin) or {}).get("image"),
        )
        if added:
            new_oos_added = True
            oos_keys.add(key_po_asin)
        continue

if not asin:
    continue
```

**Key changes:**
- Check `acknowledgementStatus.acceptedQuantity`
- Only mark as OOS if `acceptedQuantity <= 0`
- Continue to normal processing if `acceptedQuantity > 0`

---

## Fix #4: UI Sorting (Optional Enhancement)

### File: ui/index.html, Lines 1774-1779

**BEFORE:**
```javascript
items.sort((a, b) => (b.totalQty || 0) - (a.totalQty || 0));
```

**AFTER:**
```javascript
items.sort((a, b) => {
  const aOos = a.isOutOfStock ? 1 : 0;
  const bOos = b.isOutOfStock ? 1 : 0;
  if (aOos !== bOos) return aOos - bOos;
  return (b.totalQty || 0) - (a.totalQty || 0);
});
```

**Effect:** Non-OOS items shown first, then OOS items

---

## Summary of Changes

| Component | Changes | Type |
|-----------|---------|------|
| PDF Headers | 2 lines | Bug fix |
| Rejected Items Lookup | 6 lines added | Bug fix |
| Item Processing | 18 lines added | Bug fix |
| UI Sorting | 5 lines improved | Enhancement |
| **Total** | **31+ lines** | |

All changes are backward compatible and validated.
