"""EPL RAW printing endpoint."""

import logging

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field, validator

from services.printers import get_printer_settings
from services.raw_print import send_raw_to_printer
from services.print_log import log_print_job
from services.epl_label import build_epl_ean13_label

try:
    import win32print
except ImportError:  # pragma: no cover
    win32print = None  # type: ignore[assignment]

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


class BarcodePrintRequest(BaseModel):
    ean: str
    sku: str
    copies: int = Field(1, ge=1, le=10)

    @validator("ean")
    def ensure_valid_ean(cls, value: str) -> str:
        digits = (value or "").strip()
        if not digits.isdigit() or len(digits) not in {12, 13}:
            raise ValueError("EAN must be 12 or 13 digits")
        return digits

    @validator("sku")
    def ensure_sku(cls, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("SKU is required")
        return normalized


@router.post("/barcode/print")
def print_epl_label(payload: BarcodePrintRequest):
    settings = get_printer_settings() or {}
    printer_name = (settings.get("default_printer_name") or "").strip()
    if not printer_name and win32print:
        try:
            printer_name = (win32print.GetDefaultPrinter() or "").strip()
        except Exception:
            printer_name = ""

    epl = build_epl_ean13_label(payload.ean, payload.sku, copies=payload.copies)
    logger.debug("Generated EPL payload:\n%s", epl)
    try:
        send_raw_to_printer(printer_name, epl.encode("ascii"))
    except Exception as exc:
        log_print_job(printer_name, payload.ean, payload.sku, payload.copies, ok=False, error=str(exc))
        raise HTTPException(status_code=502, detail=f"EPL print failed: {exc}")

    log_print_job(printer_name, payload.ean, payload.sku, payload.copies, ok=True)
    return {"ok": True, "printer": printer_name, "sent": payload.copies}


def register_barcode_print_routes(app: FastAPI) -> None:
    app.include_router(router)
