import logging
import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.db import CATALOG_DB_PATH, get_db_connection

router = APIRouter(prefix="/api/forecast", tags=["forecast"])
logger = logging.getLogger("forecast_engine")


class BlacklistRequest(BaseModel):
    asin: str
    marketplaceId: str
    reason: str | None = None


def _utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@router.post("/blacklist")
def add_to_blacklist(req: BlacklistRequest):
    if not req.asin or not req.marketplaceId:
        raise HTTPException(status_code=400, detail="asin and marketplaceId are required")
    now = _utc_now()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO forecast_blacklist (asin, marketplace_id, reason, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (req.asin, req.marketplaceId, req.reason, now),
        )
        conn.commit()
    logger.info(f"[forecast_blacklist] Blacklisted {req.asin} ({req.marketplaceId})")
    return {"ok": True}


@router.get("/blacklist")
def list_blacklist():
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT asin, marketplace_id, reason, created_at
            FROM forecast_blacklist
            ORDER BY created_at DESC
            """
        ).fetchall()
    items = [
        {
            "asin": r[0],
            "marketplaceId": r[1],
            "reason": r[2],
            "createdAt": r[3],
        }
        for r in rows
    ]
    return {"items": items}
