"""Printer settings API consumed by the frontend."""

from typing import Dict, Optional

import logging

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field, root_validator

from services.printers import get_printer_settings, list_printers, save_printer_settings

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


class PrinterDefaultsLabelPayload(BaseModel):
    label_width_mm: float = Field(38.0, ge=0, description="Label width in millimeters")
    label_height_mm: float = Field(25.4, ge=0, description="Label height in millimeters")
    gap_mm: float = Field(2.0, ge=0, description="Gap between labels in millimeters")


class PrinterDefaultsResponse(BaseModel):
    default_printer_name: str = Field("", description="Saved default printer name")
    label_width_mm: float = Field(38.0, ge=0)
    label_height_mm: float = Field(25.4, ge=0)
    gap_mm: float = Field(2.0, ge=0)


class PrinterSettingsPayload(BaseModel):
    default_printer_name: str = Field("", description="Printer name to persist")
    label_settings: Optional[PrinterDefaultsLabelPayload] = None
    label_width_mm: Optional[float] = Field(None, ge=0)
    label_height_mm: Optional[float] = Field(None, ge=0)
    gap_mm: Optional[float] = Field(None, ge=0)

    @root_validator(pre=True)
    def merge_label_fields(cls, values):
        label_settings = values.get("label_settings") or {}
        for key in ("label_width_mm", "label_height_mm", "gap_mm"):
            if values.get(key) is not None:
                label_settings[key] = values[key]
        values["label_settings"] = label_settings
        return values


@router.get("/printers")
def list_available_printers() -> Dict[str, object]:
    """Return the printer list data formatted for the UI."""
    return list_printers()


def _to_flat_settings(settings: Dict[str, object]) -> PrinterDefaultsResponse:
    label_settings = settings.get("label_settings", {})
    return PrinterDefaultsResponse(
        default_printer_name=(settings.get("default_printer_name") or "").strip(),
        label_width_mm=float(label_settings.get("label_width_mm", 38.0)),
        label_height_mm=float(label_settings.get("label_height_mm", 25.4)),
        gap_mm=float(label_settings.get("gap_mm", 2.0)),
    )


@router.get("/printers/default")
def read_printer_defaults() -> Dict[str, object]:
    """Provide the saved printer settings with safe defaults."""
    try:
        settings = get_printer_settings() or {}
        response = _to_flat_settings(settings)
        return response.dict()
    except Exception:
        logger.exception("Failed to read printer defaults")
        return {
            "default_printer_name": "",
            "label_width_mm": 38.0,
            "label_height_mm": 25.4,
            "gap_mm": 2.0,
        }


@router.post("/printers/default")
def save_printer_defaults(payload: PrinterSettingsPayload) -> Dict[str, object]:
    """Persist updated printer settings."""
    label_settings = {}
    if payload.label_settings:
        label_settings.update(payload.label_settings.dict())

    label_settings.setdefault("label_width_mm", 38.0)
    label_settings.setdefault("label_height_mm", 25.4)
    label_settings.setdefault("gap_mm", 2.0)

    save_printer_settings(
        default_printer_name=payload.default_printer_name,
        label_settings=label_settings,
    )
    response = _to_flat_settings(get_printer_settings())
    return {"ok": True, "settings": response.dict()}


def register_printer_routes(app: FastAPI) -> None:
    """Mount the printer settings router on the FastAPI app."""
    app.include_router(router)
