"""Legacy vendor RT inventory routes (compatibility wrappers)."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from routes.vendor_inventory_realtime_routes import (
    get_realtime_inventory_snapshot as realtime_snapshot_handler,
    refresh_realtime_inventory as realtime_refresh_handler,
)

router = APIRouter()


@router.get("/api/vendor/rt-inventory")
def get_vendor_rt_inventory():
    """
    Legacy endpoint that now delegates to the DB-first realtime snapshot handler.
    """
    return realtime_snapshot_handler()


@router.post("/api/vendor/rt-inventory/refresh")
def refresh_vendor_rt_inventory():
    """
    Legacy refresh endpoint. Delegates to the DB-first realtime refresh handler.
    """
    return realtime_refresh_handler()


def register_vendor_rt_inventory_routes(app: FastAPI) -> None:
    app.include_router(router)
