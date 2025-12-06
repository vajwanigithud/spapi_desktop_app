import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from services.db import CATALOG_DB_PATH
from services.forecast_sync import (
    parse_report_tsv,
    parse_report_json,
    safe_float,
    parse_date,
)

logger = logging.getLogger("forecast_engine")


def _utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def create_or_get_report_job(report_type: str, date_start: str, date_end: str, params: dict) -> int:
    """
    Create or reuse a report job. SP-API createReport is stubbed.
    """
    params_json = json.dumps(params or {}, ensure_ascii=False)
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        cur = conn.execute(
            """
            SELECT id, status FROM report_jobs
            WHERE report_type = ? AND date_start = ? AND date_end = ? AND status != 'FAILED'
            """,
            (report_type, date_start, date_end),
        )
        row = cur.fetchone()
        if row:
            job_id = row[0]
            logger.debug(f"[report_jobs] Reusing existing job {job_id} for {report_type} {date_start}->{date_end}")
            return job_id

        # TODO: integrate SP-API createReport here
        report_id = "PENDING_SP_API_WIREUP"

        conn.execute(
            """
            INSERT INTO report_jobs (report_type, date_start, date_end, report_id, document_id, status, params_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (report_type, date_start, date_end, report_id, None, "CREATED", params_json, _utc_now()),
        )
        conn.commit()
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        logger.info(f"[report_jobs] Created job {job_id} for {report_type} {date_start}->{date_end}")
        return job_id


def poll_report_jobs() -> None:
    """
    Poll jobs in CREATED/IN_PROGRESS. SP-API getReport is stubbed.
    """
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        jobs = conn.execute(
            "SELECT id, report_id, status FROM report_jobs WHERE status IN ('CREATED','IN_PROGRESS')"
        ).fetchall()
        for job in jobs:
            job_id = job["id"]
            report_id = job["report_id"]
            status = job["status"]

            # TODO: replace stub with SP-API getReport(report_id)
            # For now, just flip CREATED -> IN_PROGRESS on first poll, then leave for manual testing.
            new_status = "IN_PROGRESS" if status == "CREATED" else status

            conn.execute(
                "UPDATE report_jobs SET status=?, last_checked_at=? WHERE id=?",
                (new_status, _utc_now(), job_id),
            )
            logger.debug(f"[report_jobs] Polled job {job_id} ({report_id}) status={new_status}")
        conn.commit()


def download_ready_reports() -> None:
    """
    Download and process jobs marked READY. SP-API getReportDocument is stubbed.
    """
    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        jobs = conn.execute("SELECT * FROM report_jobs WHERE status='READY'").fetchall()
        for job in jobs:
            job_id = job["id"]
            report_type = job["report_type"]
            document_id = job["document_id"]

            # TODO: replace with SP-API getReportDocument(document_id)
            dummy_data: Any = {}

            if report_type == "VENDOR_FORECAST":
                handle_vendor_forecast_report(job, dummy_data)
            elif report_type == "VENDOR_SALES_HISTORY":
                handle_sales_history_report(job, dummy_data)
            elif report_type == "VENDOR_RT_INVENTORY":
                handle_inventory_report(job, dummy_data)
            else:
                logger.warning(f"[report_jobs] Unknown report_type {report_type} for job {job_id}")

            conn.execute(
                "UPDATE report_jobs SET status='DOWNLOADED', last_checked_at=? WHERE id=?",
                (_utc_now(), job_id),
            )
            logger.info(f"[report_jobs] Marked job {job_id} as DOWNLOADED")
        conn.commit()


def handle_vendor_forecast_report(job: sqlite3.Row, data: Any) -> None:
    """
    ETL for GET_VENDOR_FORECASTING_REPORT → vendor_forecast.
    Tries JSON first (some accounts get JSON), otherwise TSV.
    """
    logger.info(f"[forecast_etl] handle_vendor_forecast_report job={job['id']}")

    if data is None:
        logger.warning("[forecast_etl] Forecast ETL skipped: data is None")
        return

    raw_rows = None
    if isinstance(data, (bytes, bytearray)):
        doc = parse_report_json(data)
        if isinstance(doc, dict) and doc:
            if "forecasts" in doc and isinstance(doc["forecasts"], list):
                raw_rows = doc["forecasts"]
        if raw_rows is None:
            raw_rows = parse_report_tsv(data)
    elif isinstance(data, list):
        raw_rows = data
    else:
        logger.warning(f"[forecast_etl] Unexpected forecast data type: {type(data)}")
        return

    if not raw_rows:
        logger.info("[forecast_etl] No rows parsed from forecast report")
        return

    logger.info(f"[forecast_etl] Parsed {len(raw_rows)} forecast rows")

    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for r in raw_rows:
            asin = (
                r.get("asin")
                or r.get("ASIN")
                or r.get("amazonProductIdentifier")
            )
            if not asin:
                continue

            marketplace_id = (
                r.get("marketplaceId")
                or r.get("marketplace_id")
                or (job["marketplace_id"] if "marketplace_id" in job.keys() else "UNKNOWN")
            )

            gen_str = r.get("forecastGenerationDate") or r.get("generationDate")
            gen_dt = parse_date(gen_str) or datetime.utcnow()
            forecast_generation_date = gen_dt.strftime("%Y-%m-%d")

            start_dt = parse_date(r.get("startDate")) or gen_dt
            end_dt = parse_date(r.get("endDate")) or gen_dt

            start_date = start_dt.strftime("%Y-%m-%d")
            end_date = end_dt.strftime("%Y-%m-%d")

            mean_units = safe_float(r.get("mean") or r.get("meanUnits"))
            p70_units = safe_float(r.get("p70") or r.get("p70Units"))
            p80_units = safe_float(r.get("p80") or r.get("p80Units"))
            p90_units = safe_float(r.get("p90") or r.get("p90Units"))

            conn.execute(
                """
                INSERT INTO vendor_forecast
                    (asin, marketplace_id, forecast_generation_date,
                     start_date, end_date, mean_units, p70_units, p80_units, p90_units)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin, marketplace_id, start_date, end_date)
                DO UPDATE SET
                    forecast_generation_date=excluded.forecast_generation_date,
                    mean_units=excluded.mean_units,
                    p70_units=excluded.p70_units,
                    p80_units=excluded.p80_units,
                    p90_units=excluded.p90_units
                """,
                (
                    asin,
                    marketplace_id,
                    forecast_generation_date,
                    start_date,
                    end_date,
                    mean_units,
                    p70_units,
                    p80_units,
                    p90_units,
                ),
            )

        conn.commit()
        logger.info("[forecast_etl] Forecast ETL committed")


def handle_sales_history_report(job: sqlite3.Row, data: Any) -> None:
    """
    ETL for GET_VENDOR_SALES_REPORT → vendor_sales_history.
    `data` is expected to be the raw bytes of the report document.
    """
    logger.info(f"[forecast_etl] handle_sales_history_report job={job['id']}")

    if data is None:
        logger.warning("[forecast_etl] Sales history ETL skipped: data is None")
        return

    # Try to parse as TSV first
    if isinstance(data, (bytes, bytearray)):
        rows = parse_report_tsv(data)
    elif isinstance(data, list):
        rows = data  # already parsed
    else:
        logger.warning(f"[forecast_etl] Unexpected sales history data type: {type(data)}")
        return

    if not rows:
        logger.info("[forecast_etl] No rows parsed from sales history report")
        return

    logger.info(f"[forecast_etl] Parsed {len(rows)} sales history rows")

    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        for r in rows:
            asin = (
                r.get("ASIN")
                or r.get("asin")
                or r.get("amazonProductIdentifier")
                or r.get("itemId")
            )
            if not asin:
                continue

            marketplace_id = (
                r.get("marketplaceId")
                or r.get("marketplace_id")
                or (job["marketplace_id"] if "marketplace_id" in job.keys() else "UNKNOWN")
            )

            date_str = (
                r.get("shipDate")
                or r.get("shippedDate")
                or r.get("eventDate")
                or r.get("date")
            )
            dt = parse_date(date_str)
            if not dt:
                continue
            sales_date = dt.strftime("%Y-%m-%d")

            units = safe_float(
                r.get("shippedUnits")
                or r.get("orderedUnits")
                or r.get("unitsShipped")
                or r.get("units")
            )
            revenue = safe_float(
                r.get("shippedRevenue")
                or r.get("netOrderedRevenue")
                or r.get("revenue")
            )

            created_at = _utc_now()

            conn.execute(
                """
                INSERT INTO vendor_sales_history
                    (asin, marketplace_id, sales_date, units, revenue, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin, marketplace_id, sales_date)
                DO UPDATE SET
                    units=excluded.units,
                    revenue=excluded.revenue,
                    created_at=excluded.created_at
                """,
                (asin, marketplace_id, sales_date, units, revenue, created_at),
            )

        conn.commit()
        logger.info("[forecast_etl] Sales history ETL committed")


def handle_inventory_report(job: sqlite3.Row, data: Any) -> None:
    """
    ETL for GET_VENDOR_REAL_TIME_INVENTORY_REPORT → vendor_rt_inventory.
    """
    logger.info(f"[forecast_etl] handle_inventory_report job={job['id']}")

    if data is None:
        logger.warning("[forecast_etl] Inventory ETL skipped: data is None")
        return

    if isinstance(data, (bytes, bytearray)):
        rows = parse_report_tsv(data)
    elif isinstance(data, list):
        rows = data
    else:
        logger.warning(f"[forecast_etl] Unexpected inventory data type: {type(data)}")
        return

    if not rows:
        logger.info("[forecast_etl] No rows parsed from inventory report")
        return

    logger.info(f"[forecast_etl] Parsed {len(rows)} inventory rows")

    with sqlite3.connect(CATALOG_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        for r in rows:
            asin = (
                r.get("ASIN")
                or r.get("asin")
                or r.get("amazonProductIdentifier")
            )
            if not asin:
                continue

            marketplace_id = (
                r.get("marketplaceId")
                or r.get("marketplace_id")
                or (job["marketplace_id"] if "marketplace_id" in job.keys() else "UNKNOWN")
            )

            snapshot_dt = parse_date(r.get("snapshotDate") or r.get("date")) or datetime.utcnow()
            snapshot_time = snapshot_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            highly_available_inventory = int(
                safe_float(
                    r.get("availableQuantity")
                    or r.get("sellableOnHandUnits")
                    or r.get("onHandUnits")
                )
            )

            conn.execute(
                """
                INSERT INTO vendor_rt_inventory
                    (asin, marketplace_id, snapshot_time, highly_available_inventory)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asin)
                DO UPDATE SET
                    marketplace_id=excluded.marketplace_id,
                    snapshot_time=excluded.snapshot_time,
                    highly_available_inventory=excluded.highly_available_inventory
                """,
                (asin, marketplace_id, snapshot_time, highly_available_inventory),
            )

        conn.commit()
        logger.info("[forecast_etl] Inventory ETL committed")
