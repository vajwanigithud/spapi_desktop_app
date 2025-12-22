from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from config import MARKETPLACE_ID
from services.db import ensure_df_payments_tables
from services.df_payments import (
    get_df_payments_state,
    incremental_refresh_df_payments,
    refresh_df_payments,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/df-payments")
DEFAULT_MARKETPLACE_ID = MARKETPLACE_ID


class FetchRequest(BaseModel):
    lookback_days: Optional[int] = Field(90, ge=1, le=90)
    ship_from_party_id: Optional[str] = Field(None, alias="shipFromPartyId")
    limit: Optional[int] = Field(50, ge=1, le=100)

    class Config:
        allow_population_by_field_name = True


@router.get("/state")
def get_state() -> dict:
    return get_df_payments_state(DEFAULT_MARKETPLACE_ID)


@router.post("/fetch")
def fetch_orders(payload: FetchRequest = Body(default_factory=FetchRequest)) -> dict:
    try:
        result = refresh_df_payments(
            DEFAULT_MARKETPLACE_ID,
            lookback_days=payload.lookback_days,
            ship_from_party_id=payload.ship_from_party_id,
            limit=payload.limit,
        )
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[DF Payments] Fetch failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/incremental")
def incremental_scan() -> dict:
    try:
        result = incremental_refresh_df_payments(
            DEFAULT_MARKETPLACE_ID,
            triggered_by="manual",
            force=True,
        )
        return {"ok": True, **result}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[DF Payments] Incremental scan failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


def register_df_payments_routes(app: FastAPI) -> None:
    try:
        ensure_df_payments_tables()
    except Exception as exc:
        logger.warning("[DF Payments] Failed to ensure tables on startup: %s", exc)
    app.include_router(router)
