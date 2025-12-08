import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def consolidate_picklist(
    po_numbers: List[str],
    vendor_pos_cache_path: Path,
    normalize_pos_entries_fn,
    load_oos_state_fn,
    save_oos_state_fn,
    spapi_catalog_status_fn,
    upsert_oos_entry_fn,
    fetch_rejected_lines_fn: Callable[[List[str]], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    if not vendor_pos_cache_path.exists():
        return {"summary": {"numPos": 0, "totalUnits": 0, "totalLines": 0, "warning": "Cache missing"}, "items": []}
    try:
        data = json.loads(vendor_pos_cache_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read cache: {exc}")

    normalized = normalize_pos_entries_fn(data)
    selected = [po for po in normalized if po.get("purchaseOrderNumber") in po_numbers]
    if not selected:
        return {"summary": {"numPos": 0, "totalUnits": 0, "totalLines": 0, "warning": "No matching POs"}, "items": []}

    oos_state = load_oos_state_fn()
    oos_keys = set(oos_state.keys()) if isinstance(oos_state, dict) else set()
    new_oos_added = False

    catalog = spapi_catalog_status_fn()

    consolidated: Dict[Tuple[str, str], Dict[str, Any]] = {}
    total_units = 0

    rejected_line_keys: set[str] = set()
    try:
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

    def is_rejected_line(item: Dict[str, Any]) -> bool:
        def norm(val: Any) -> str:
            if val is None:
                return ""
            return str(val).strip().upper()

        ack = item.get("acknowledgementStatus")
        candidates = []
        if isinstance(ack, dict):
            candidates.extend(
                [
                    ack.get("confirmationStatus"),
                    ack.get("status"),
                    ack.get("overallStatus"),
                ]
            )
        elif isinstance(ack, str):
            candidates.append(ack)

        candidates.extend(
            [
                item.get("confirmationStatus"),
                item.get("status"),
                item.get("_inhouseStatus"),
                item.get("_internalStatus"),
            ]
        )

        return any("REJECT" in norm(c) for c in candidates if norm(c))

    for po in selected:
        po_num = po.get("purchaseOrderNumber") or ""
        d = po.get("orderDetails") or {}
        ship_to = d.get("shipToParty", {}).get("partyId") if isinstance(d.get("shipToParty"), dict) else None
        po_date = po.get("purchaseOrderDate") or d.get("purchaseOrderDate")
        items = d.get("items") or []
        for it in items:
            qty = it.get("orderedQuantity") or {}
            qty_amount = qty.get("amount")
            try:
                qty_num = float(qty_amount)
            except Exception:
                qty_num = 0

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

            if key_po_asin in oos_keys:
                continue
            if any(
                (entry.get("asin") == asin and entry.get("vendorSku") == sku)
                for entry in (oos_state.values() if isinstance(oos_state, dict) else [])
            ):
                continue

            ckey = (asin, sku)
            if ckey not in consolidated:
                info = catalog.get(asin) or {}
                master_sku = info.get("sku")
                line_sku = master_sku or sku or ""
                consolidated[ckey] = {
                    "asin": asin,
                    "externalId": sku,
                    "sku": line_sku,
                    "title": info.get("title"),
                    "image": info.get("image"),
                    "totalQty": 0,
                }
            consolidated[ckey]["totalQty"] += qty_num
            total_units += qty_num

    if new_oos_added:
        save_oos_state_fn(oos_state)

    items_out = list(consolidated.values())
    items_out.sort(key=lambda x: (0 - (x.get("totalQty") or 0)))
    summary = {
        "numPos": len(selected),
        "totalUnits": total_units,
        "totalLines": len(items_out),
        "warning": None,
    }
    return {"summary": summary, "items": items_out}


def generate_picklist_pdf(po_numbers: List[str], items: List[Dict[str, Any]], summary: Dict[str, Any]) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError:
        raise HTTPException(status_code=500, detail="reportlab is required for PDF generation")

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontSize = 9
    title_style = styles["Normal"]
    title_style.fontSize = 9
    qty_style = styles["Normal"]
    qty_style.fontSize = 9
    qty_style.alignment = 1  # center

    data = []
    header = ["ASIN", "SKU", "Image", "Title", "Total Qty"]
    data.append(header)

    col_widths = [28 * mm, 28 * mm, 40 * mm, 64 * mm, 20 * mm]

    for it in items:
        asin = it.get("asin") or ""
        sku = it.get("sku") or it.get("externalId") or it.get("vendorSku") or ""
        img_url = it.get("image") or ""
        title = it.get("title") or ""
        qty = it.get("totalQty") or ""

        img_flow = ""
        if img_url:
            try:
                img_flow = Image(img_url, width=38 * mm, height=38 * mm, kind="proportional")
            except Exception:
                img_flow = ""

        data.append([asin, sku, img_flow, Paragraph(title, normal), qty])

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), "#f0f0f0"),
                ("GRID", (0, 0), (-1, -1), 0.5, "#cccccc"),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    elements = [
        Paragraph(f"Picklist for POs: {', '.join(po_numbers)}", title_style),
        Spacer(1, 8),
        Paragraph(
            f"Summary: {summary.get('numPos')} POs, {summary.get('totalLines')} SKUs, {summary.get('totalUnits')} units",
            normal,
        ),
        Spacer(1, 12),
        table,
    ]

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
