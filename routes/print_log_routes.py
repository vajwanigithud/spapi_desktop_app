"""Print job log endpoints."""

import logging

from fastapi import APIRouter, FastAPI, Query

from services.print_log import get_recent_print_jobs

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


@router.get("/prints/recent")
def recent_prints(limit: int = Query(20, ge=1, le=100)):
    jobs = get_recent_print_jobs(limit=limit)
    logger.info("[print_log] recent requested limit=%s returning=%s", limit, len(jobs))
    return {"jobs": jobs}


def register_print_log_routes(app: FastAPI) -> None:
    app.include_router(router)
