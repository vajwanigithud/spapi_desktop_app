import logging
from io import BytesIO
from typing import Any, Callable, Dict, List, Tuple

from fastapi import HTTPException

logger = logging.getLogger(__name__)


def generate_picklist_xlsx(
    po_numbers: List[str],
    consolidate_fn: Callable[[List[str]], Dict[str, Any]],
) -> Tuple[bytes, List[Dict[str, Any]], Dict[str, Any]]:
    """
    Build a picklist XLSX using the same consolidation function as preview/PDF.
    """
    if not consolidate_fn:
        raise HTTPException(status_code=500, detail="Picklist generator unavailable")

    result = consolidate_fn(po_numbers)
    items = result.get("items") or []
    summary_raw = result.get("summary") or {}

    # Maintain the same ordering as PDF/preview.
    items.sort(key=lambda x: (0 - (x.get("totalQty") or 0)))

    summary = dict(summary_raw) if isinstance(summary_raw, dict) else {}
    line_count = summary.get("totalLines")
    if line_count is None:
        line_count = len(items)
        summary["totalLines"] = line_count

    if not items:
        raise HTTPException(status_code=400, detail="No picklist lines found for the given PO numbers")

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except (ImportError, ModuleNotFoundError) as exc:
        raise HTTPException(status_code=500, detail="openpyxl is required for XLSX generation") from exc

    buffer = BytesIO()
    try:
        wb = Workbook()
        ws = wb.active
        ws.title = "Pick List"

        headers = ["#", "ASIN", "Qty"]
        ws.append(headers)
        header_font = Font(bold=True)
        for cell in ws[1]:
            cell.font = header_font

        for idx, it in enumerate(items, start=1):
            qty_raw = it.get("totalQty") or 0
            try:
                qty_val = int(qty_raw)
            except Exception:
                qty_val = qty_raw or 0
            ws.append([idx, it.get("asin") or "", qty_val])

        ws.freeze_panes = "A2"
        ws.column_dimensions["A"].width = 6
        ws.column_dimensions["B"].width = 24
        ws.column_dimensions["C"].width = 10

        wb.save(buffer)
        xlsx_bytes = buffer.getvalue()
    except Exception as exc:
        logger.error("Picklist XLSX generation failed: %s", exc, exc_info=True)
        detail = f"Picklist XLSX failed: {exc.__class__.__name__}: {exc}"
        raise HTTPException(status_code=500, detail=detail) from exc
    finally:
        buffer.close()

    return xlsx_bytes, items, summary
