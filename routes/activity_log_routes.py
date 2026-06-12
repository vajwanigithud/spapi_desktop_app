from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, FastAPI, Query

from services.activity_log import get_activity_events

router = APIRouter(prefix="/api/activity-logs")


@router.get("")
def read_activity_logs(limit: int = Query(500, ge=1, le=500)) -> Dict[str, object]:
    return {"ok": True, "items": get_activity_events(limit)}


def register_activity_log_routes(app: FastAPI) -> None:
    app.include_router(router)
