from __future__ import annotations

from fastapi import APIRouter, FastAPI, Query

from services.vendor_rt_sales_ledger import list_ledger_rows

router = APIRouter(prefix="/api/vendor/rt-sales")


@router.get("/ledger")
def get_vendor_rt_sales_ledger(
    marketplace_id: str = Query(..., description="Marketplace ID"),
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    rows = list_ledger_rows(marketplace_id, limit)
    return {"ok": True, "rows": rows}


def register_vendor_rt_sales_routes(app: FastAPI) -> None:
    app.include_router(router)
