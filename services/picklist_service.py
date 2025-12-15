import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from fastapi import HTTPException

from services.perf import time_block

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
        with time_block("picklist_cache_read"):
            data = json.loads(vendor_pos_cache_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read cache: {exc}")

    normalized = normalize_pos_entries_fn(data)
    selected = [po for po in normalized if po.get("purchaseOrderNumber") in po_numbers]
    if not selected:
        return {"summary": {"numPos": 0, "totalUnits": 0, "totalLines": 0, "warning": "No matching POs"}, "items": []}

    oos_state = load_oos_state_fn()
    oos_keys = set(oos_state.keys()) if isinstance(oos_state, dict) else set()

    catalog = spapi_catalog_status_fn()

    consolidated: Dict[Tuple[str, str], Dict[str, Any]] = {}
    total_units = 0

    # Build a map of fully rejected lines (accepted_qty == 0 AND ordered_qty > 0)
    # These should be excluded from picklist entirely
    fully_rejected_lines: set[str] = set()
    try:
        with time_block("picklist_rejected_lookup"):
            rows = fetch_rejected_lines_fn(po_numbers) or []
        for row in rows:
            po_num = (row.get("po_number") or "").strip()
            asin = (row.get("asin") or "").strip()
            if po_num and asin:
                try:
                    accepted_qty = float(row.get("accepted_qty") or 0)
                    ordered_qty = float(row.get("ordered_qty") or 0)
                except Exception:
                    continue
                # Only mark as fully rejected if accepted is 0 and ordered is > 0
                if accepted_qty == 0 and ordered_qty > 0:
                    key = f"{po_num}::{asin}"
                    fully_rejected_lines.add(key)
    except Exception as exc:
        logger.warning(f"[Picklist] Failed to load vendor_po_lines for rejection filter: {exc}")

    with time_block("picklist_consolidate_items"):
        for po in selected:
            po_num = po.get("purchaseOrderNumber") or ""
            d = po.get("orderDetails") or {}
            items = d.get("items") or []
            for it in items:
                asin = it.get("amazonProductIdentifier") or ""
                sku = it.get("vendorProductIdentifier") or ""
                key_po_asin = f"{po_num}::{asin}" if asin else ""

                # SKIP fully rejected lines entirely (not on picklist)
                if key_po_asin in fully_rejected_lines:
                    continue

                if not asin:
                    continue

                # Use ACCEPTED quantity, not ordered
                # First try acknowledgementStatus.acceptedQuantity (for items with status)
                accepted_qty = 0
                ack = it.get("acknowledgementStatus") or {}
                if isinstance(ack, dict):
                    try:
                        acc_qty_obj = ack.get("acceptedQuantity") or {}
                        accepted_qty = float(acc_qty_obj.get("amount") or 0)
                    except (TypeError, ValueError):
                        accepted_qty = 0
                
                # If no accepted quantity, fall back to ordered quantity (for fresh POs)
                if accepted_qty == 0:
                    qty = it.get("orderedQuantity") or {}
                    qty_amount = qty.get("amount")
                    try:
                        accepted_qty = float(qty_amount or 0)
                    except (TypeError, ValueError):
                        accepted_qty = 0

                # Skip lines with 0 accepted quantity
                if accepted_qty == 0:
                    continue

                ckey = (asin, sku)
                is_oos = False
                
                # Only mark as OOS if the item has 0 accepted quantity
                # Do NOT check the oos_state dictionary as it accumulates stale data
                if accepted_qty == 0:
                    is_oos = True

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
                        "isOutOfStock": is_oos,
                    }
                # Add accepted quantity (not ordered)
                consolidated[ckey]["totalQty"] += int(accepted_qty)
                # Count all accepted items in total
                total_units += int(accepted_qty)

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
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Image,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
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

    with time_block("picklist_pdf_generate"):
        doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
