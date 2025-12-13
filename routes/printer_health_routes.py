"""Printer health endpoint."""

from fastapi import APIRouter, FastAPI

from services.printer_health import get_default_printer_health

router = APIRouter(prefix="/api")


@router.get("/printers/health")
def printer_health():
    return get_default_printer_health()


def register_printer_health_routes(app: FastAPI) -> None:
    app.include_router(router)
