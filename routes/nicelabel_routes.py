"""Simple NiceLabel TCP printing entrypoint."""

import logging

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field, validator

from services.nicelabel_printing import build_payload, send_print_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/nicelabel")


class NiceLabelPrintRequest(BaseModel):
    ean: str
    sku: str
    title: str | None = None
    copies: int = Field(1, ge=1, le=10)
    host: str = Field("127.0.0.1")
    port: int = Field(9101, gt=0, le=65535)

    @validator("ean")
    def validate_ean(cls, value: str) -> str:
        digits = (value or "").strip()
        if not digits.isdigit() or len(digits) not in {12, 13}:
            raise ValueError("EAN must be 12 or 13 digits")
        return digits

    @validator("sku")
    def validate_sku(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("SKU is required")
        return value.strip()


@router.post("/print")
def nicelabel_print(payload: NiceLabelPrintRequest):
    fields = {
        "EAN": payload.ean,
        "SKU": payload.sku,
        "TITLE": payload.title or "",
    }
    payload_text = build_payload(fields, copies=payload.copies)
    logger.info(
        "[NiceLabel] Printing to %s:%s copies=%s keys=%s",
        payload.host,
        payload.port,
        payload.copies,
        list(fields.keys()),
    )

    try:
        result = send_print_job(payload.host, payload.port, payload_text)
    except RuntimeError as exc:
        detail = f"NiceLabel TCP print failed: {exc}"
        logger.error(detail)
        raise HTTPException(status_code=502, detail=detail)

    return {
        "ok": True,
        "bytes_sent": result.get("bytes_sent", 0),
        "host": payload.host,
        "port": payload.port,
    }


def register_nicelabel_routes(app: FastAPI) -> None:
    """Mount NiceLabel routes on the FastAPI app."""
    app.include_router(router)
