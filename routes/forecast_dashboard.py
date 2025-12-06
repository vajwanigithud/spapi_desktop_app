import logging
from datetime import datetime

from fastapi import APIRouter

logger = logging.getLogger("uvicorn.error")

router = APIRouter(
    prefix="/api/forecast",
    tags=["forecast"],
)


@router.get("/dashboard")
def get_forecast_dashboard():
    """
    Very simple forecast dashboard endpoint.
    Returns placeholder meta + empty rows, and logs when called.
    """
    logger.info("[ForecastDashboard] HIT /api/forecast/dashboard")
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    meta = {
        "generatedAt": now_iso,
        "salesDataFrom": None,
        "salesDataThrough": None,
        "forecastGenerationDateMin": None,
        "forecastGenerationDateMax": None,
        "inventorySnapshotTime": None,
        "sourceStatus": {
            "salesHistory": {
                "key": "salesHistory",
                "label": "Sales History",
                "status": "MISSING",
                "message": "No sales data loaded",
                "dataFrom": None,
                "dataThrough": None,
                "daysBehind": None,
            },
            "forecast": {
                "key": "forecast",
                "label": "Forecast",
                "status": "MISSING",
                "message": "No forecast data loaded",
                "lastGenerationDate": None,
                "ageDays": None,
            },
            "inventory": {
                "key": "inventory",
                "label": "Inventory",
                "status": "MISSING",
                "message": "No inventory snapshot",
                "snapshotTime": None,
                "ageMinutes": None,
            },
            "pos": {
                "key": "pos",
                "label": "POs",
                "status": "INFO",
                "message": "POs maintained in Vendor POs tab",
                "dataFrom": None,
                "dataThrough": None,
            },
        },
    }

    return {
        "meta": meta,
        "rows": [],
    }


@router.post("/refresh-all")
def refresh_all_forecast_data():
    """
    Stub endpoint to refresh all forecast-related data:
    - sales history
    - inventory snapshot
    - vendor forecasting report

    For now, it just logs and returns ok=true.
    Later we will wire this to the real SP-API report jobs and ETL.
    """
    logger.info("[ForecastRefresh] Received request to refresh all forecast data")
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return {
        "ok": True,
        "message": "Stub refresh completed",
        "refreshedAt": now_iso,
    }
