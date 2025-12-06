"""
Forecast data synchronization helpers.

Recent changes:
- Forecast: avoid invalid sellingProgram for GET_VENDOR_FORECASTING_REPORT and surface errorDetails as hard failures.
- Inventory: parse reportData list correctly for GET_VENDOR_REAL_TIME_INVENTORY_REPORT and warn on suspiciously low upserts.
- Overall sync: propagate forecast/inventory warnings instead of claiming full success when parts fail.
"""

import csv
import io
import json
import logging
import sqlite3
import os
import time
import threading
from datetime import datetime, timedelta, timezone, date
from typing import Iterable, Dict, Any, List
from pathlib import Path

from services.db import CATALOG_DB_PATH, get_db_connection
from services.spapi_reports import (
    request_vendor_report,
    poll_vendor_report,
    download_vendor_report_document,
    SpApiQuotaError,
)

logger = logging.getLogger("forecast_sync")
_inventory_lock = threading.Lock()
_sync_lock = threading.Lock()
# Track last inventory sync end time for diagnostics (not used for gating yet)
_last_inventory_sync_at: datetime | None = None
_inventory_cooldown_until: datetime | None = None
_forecast_cooldown_until: datetime | None = None
_STATE_PATH = Path(__file__).resolve().parent.parent / "forecast_sync_state.json"
_last_full_sync_cache: datetime | None = None


class ForecastSyncError(RuntimeError):
    """Raised when forecast report returns an error envelope."""


def parse_report_tsv(document_bytes: bytes):
    """
    Parse a TSV report downloaded from Amazon Vendor Reports.
    Returns a list of dictionaries, one per row.
    Automatically handles:
        - BOM
        - irregular quoting
        - missing columns
    """
    try:
        text = document_bytes.decode("utf-8-sig", errors="ignore")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        return [dict(row) for row in reader]
    except Exception as exc:
        logger.error(f"[Parser] TSV parsing failed: {exc}")
        return []


def parse_report_json(document_bytes: bytes):
    """
    Parse JSON report documents (Vendor Forecasting sometimes uses JSON).
    Returns Python dict or empty dict on error.
    """
    try:
        text = document_bytes.decode("utf-8-sig", errors="ignore")
        return json.loads(text)
    except Exception as exc:
        logger.error(f"[Parser] JSON parsing failed: {exc}")
        return {}


def safe_float(val):
    try:
        if val is None or val == "":
            return 0.0
        return float(val)
    except:
        return 0.0


def parse_date(date_str):
    """
    Parse Amazon Vendor date strings in multiple formats.
    Returns datetime or None.
    """
    if not date_str:
        return None

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except:
            continue
    return None


# ---------------------------------------------------------------------------
# Internal helpers (placeholders for SP-API wiring)
# ---------------------------------------------------------------------------

def _request_report(report_type: str, params: Dict[str, Any]) -> str:
    logger.info(f"[forecast_sync] Requesting report {report_type}")
    return request_vendor_report(report_type, **params)


def _poll_report(report_id: str) -> str:
    logger.info(f"[forecast_sync] Polling report {report_id}")
    return poll_vendor_report(report_id)


def _download_report_document(document_id: str) -> Iterable[Dict[str, Any]]:
    logger.info(f"[forecast_sync] Downloading document {document_id}")
    raw = download_vendor_report_document(document_id)

    # download_vendor_report_document now returns either bytes or dict/list (JSON),
    # or an error envelope (dict with errorDetails/reportRequestError).
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        return parse_report_tsv(raw)
    if isinstance(raw, str):
        return parse_report_tsv(raw.encode("utf-8", errors="ignore"))
    logger.warning("[forecast_sync] Unexpected document type: %s", type(raw))
    return []


def _iter_date_chunks(start: date, end: date, max_days: int = 15):
    """
    Yield (chunk_start, chunk_end) date ranges inclusive, each up to max_days.
    """
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def parse_vendor_sales_json(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse the JSON payload from GET_VENDOR_SALES_REPORT.

    Expected structure (simplified):
    {
        "reportSpecification": {...},
        "salesAggregate": [...],
        "salesByAsin": [
            {
                "asin": "B0C4135GKJ",
                "startDate": "2025-11-15",
                "endDate": "2025-11-15",
                "customerReturns": 0,
                "shippedCogs": {"amount": 6.0, "currencyCode": "AED"},
                "shippedRevenue": {"amount": 8.57, "currencyCode": "AED"},
                "shippedUnits": 1
            },
            ...
        ]
    }
    """
    rows: List[Dict[str, Any]] = []
    skipped_missing_keys = 0

    if isinstance(doc, list):
        entries = doc
    elif isinstance(doc, dict):
        entries = doc.get("salesByAsin") or []
    else:
        return rows

    if not isinstance(entries, list):
        logger.warning(
            "[forecast_sync] salesByAsin payload is not a list (type=%s)", type(entries)
        )
        return rows

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        asin = (entry.get("asin") or "").strip()
        date_str = (
            (entry.get("startDate") or "").strip()
            or (entry.get("periodStartDate") or "").strip()
            or (entry.get("date") or "").strip()
        )

        if not asin or not date_str:
            skipped_missing_keys += 1
            logger.warning(
                "[forecast_sync] Skipping sales entry missing asin/date: %r",
                entry,
            )
            continue

        parsed_dt = parse_date(date_str.replace("Z", "+00:00"))
        if not parsed_dt:
            logger.warning(
                "[forecast_sync] Invalid date %r in entry for asin %s",
                date_str,
                asin,
            )
            continue
        sales_date = parsed_dt.date().isoformat()

        shipped_units = int(entry.get("shippedUnits") or 0)
        shipped_cogs = float((entry.get("shippedCogs") or {}).get("amount") or 0.0)
        shipped_revenue = float((entry.get("shippedRevenue") or {}).get("amount") or 0.0)
        returns = int(entry.get("customerReturns") or 0)
        marketplace_id = (
            (entry.get("marketplaceId") or "").strip()
            or (entry.get("marketplace_id") or "").strip()
            or "UNKNOWN"
        )

        rows.append(
            {
                "asin": asin,
                "marketplace_id": marketplace_id,
                "sales_date": sales_date,
                "shipped_units": shipped_units,
                "shipped_cogs": shipped_cogs,
                "shipped_revenue": shipped_revenue,
                "customer_returns": returns,
            }
        )

    if skipped_missing_keys:
        logger.warning(
            "[forecast_sync] Skipped %d sales entries with missing asin/startDate.",
            skipped_missing_keys,
        )

    logger.info(
        "[forecast_sync] Parsed %d sales rows from JSON document.",
        len(rows),
    )

    return rows


def _is_error_envelope(payload: Any) -> bool:
    """
    True if the payload looks like a SP-API error envelope.
    """
    if not isinstance(payload, dict):
        return False
    if "errorDetails" in payload or "reportRequestError" in payload:
        return True
    if "errors" in payload and isinstance(payload["errors"], (list, dict)):
        return True
    return False


def _load_last_full_sync() -> datetime | None:
    global _last_full_sync_cache
    if _last_full_sync_cache:
        return _last_full_sync_cache
    if not _STATE_PATH.exists():
        logger.info("[forecast_sync] No last_full_sync_at state file found")
        return None
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        ts = data.get("last_full_sync_at")
        if ts:
            ts = ts.replace("Z", "+00:00")
            _last_full_sync_cache = datetime.fromisoformat(ts)
            logger.info(
                "[forecast_sync] Loaded last_full_sync_at=%s from state file",
                _last_full_sync_cache.isoformat(),
            )
            return _last_full_sync_cache
    except Exception as exc:
        logger.warning("[forecast_sync] Failed to read last_full_sync_at: %s", exc)
    # Fallback to file mtime so we still respect the cooldown even if JSON is missing
    try:
        stat_ts = datetime.fromtimestamp(_STATE_PATH.stat().st_mtime, tz=timezone.utc)
        _last_full_sync_cache = stat_ts
        logger.info(
            "[forecast_sync] Using state file mtime for last_full_sync_at=%s",
            stat_ts.isoformat(),
        )
        return _last_full_sync_cache
    except Exception:
        logger.warning("[forecast_sync] Failed to derive last_full_sync_at from state file mtime", exc_info=True)
        return None
    return None


def _save_last_full_sync(dt: datetime) -> None:
    global _last_full_sync_cache
    try:
        _STATE_PATH.write_text(
            json.dumps({"last_full_sync_at": dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")}),
            encoding="utf-8",
        )
        _last_full_sync_cache = dt
        logger.info("[forecast_sync] Saved last_full_sync_at=%s", dt.isoformat())
    except Exception:
        logger.warning("[forecast_sync] Failed to persist last_full_sync_at", exc_info=True)


def _upsert_sales_history(rows: List[Dict[str, Any]]) -> int:
    """
    Upsert rows into vendor_sales_history using flattened daily fields.
    """
    inserted = 0
    with get_db_connection() as conn:
        cur = conn.cursor()
        for r in rows:
            if not hasattr(r, "get"):
                raise TypeError(f"Expected row dict, got {type(r)}: {r!r}")

            asin = (r.get("asin") or "").strip()
            sales_date = (r.get("sales_date") or r.get("date") or "").strip()
            if not asin or not sales_date:
                logger.warning(
                    "[forecast_sync] Skipping sales_history row without asin/date: %r",
                    r,
                )
                continue

            marketplace_id = (
                (r.get("marketplace_id") or "").strip()
                or (r.get("marketplaceId") or "").strip()
                or "UNKNOWN"
            )

            units = float(r.get("shipped_units") or r.get("units") or 0)
            revenue = float(r.get("shipped_revenue") or r.get("revenue") or 0.0)
            created_at = r.get("created_at") or datetime.utcnow().isoformat() + "Z"

            cur.execute(
                """
                INSERT OR REPLACE INTO vendor_sales_history
                (asin, marketplace_id, sales_date, units, revenue, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (asin, marketplace_id, sales_date, units, revenue, created_at),
            )
            inserted += 1
        conn.commit()
    logger.info("[forecast_sync] Upserted %d sales rows into vendor_sales_history", inserted)
    return inserted


def _upsert_forecast(rows: Iterable[Dict[str, Any]]) -> None:
    """
    Upsert rows into vendor_forecast.
    Expected fields per row:
      asin, marketplace_id, forecast_generation_date, start_date, end_date,
      mean_units, p70_units, p80_units, p90_units
    """
    with get_db_connection() as conn:
        cur = conn.cursor()
        for r in rows:
            if not hasattr(r, "get"):
                raise TypeError(f"Expected row dict, got {type(r)}: {r!r}")

            asin = (
                (r.get("asin") or "").strip()
                or (r.get("ASIN") or "").strip()
                or (r.get("amazonProductIdentifier") or "").strip()
                or (r.get("amazon_product_identifier") or "").strip()
            )
            if not asin:
                logger.warning(
                    "[forecast_sync] Skipping forecast row without ASIN: %r",
                    r,
                )
                continue

            marketplace_id = (
                (r.get("marketplace_id") or "").strip()
                or (r.get("marketplaceId") or "").strip()
            )
            if not marketplace_id:
                marketplace_id = "UNKNOWN"

            forecast_generation_date = (
                r.get("forecast_generation_date")
                or r.get("forecastGenerationDate")
                or r.get("generationDate")
            )
            start_date = (
                r.get("start_date")
                or r.get("startDate")
                or r.get("periodStart")
            )
            end_date = (
                r.get("end_date")
                or r.get("endDate")
                or r.get("periodEnd")
            )

            mean_units = float(
                r.get("mean_units", r.get("meanUnits", r.get("meanForecastUnits", 0))) or 0
            )
            p70_units = float(
                r.get("p70_units", r.get("p70Units", r.get("p70ForecastUnits", 0))) or 0
            )
            p80_units = float(
                r.get("p80_units", r.get("p80Units", r.get("p80ForecastUnits", 0))) or 0
            )
            p90_units = float(
                r.get("p90_units", r.get("p90Units", r.get("p90ForecastUnits", 0))) or 0
            )

            cur.execute(
                """
                INSERT OR REPLACE INTO vendor_forecast
                (asin, marketplace_id, forecast_generation_date, start_date, end_date,
                 mean_units, p70_units, p80_units, p90_units)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def _upsert_inventory(rows: Iterable[Dict[str, Any]]) -> None:
    """
    Upsert rows into vendor_rt_inventory.
    Expected fields per row:
      asin, marketplace_id, snapshot_time, highly_available_inventory
    """
    with get_db_connection() as conn:
        cur = conn.cursor()
        for r in rows:
            if not hasattr(r, "get"):
                raise TypeError(f"Expected row dict, got {type(r)}: {r!r}")

            asin = (
                (r.get("asin") or "").strip()
                or (r.get("ASIN") or "").strip()
                or (r.get("amazonProductIdentifier") or "").strip()
                or (r.get("amazon_product_identifier") or "").strip()
            )

            if not asin:
                logger.warning(
                    "[forecast_sync] Skipping inventory row without ASIN: %r",
                    r,
                )
                continue

            marketplace_id = (
                (r.get("marketplace_id") or "").strip()
                or (r.get("marketplaceId") or "").strip()
            )
            if not marketplace_id:
                marketplace_id = "UNKNOWN"

            snapshot_time = (
                r.get("snapshot_time")
                or r.get("snapshotTime")
                or r.get("lastUpdatedTime")
                or r.get("startTime")
                or r.get("endTime")
            )
            if not snapshot_time:
                snapshot_time = datetime.utcnow().isoformat() + "Z"

            cur.execute(
                """
                INSERT OR REPLACE INTO vendor_rt_inventory
                (asin, marketplace_id, snapshot_time, highly_available_inventory)
                VALUES (?, ?, ?, ?)
                """,
                (
                    asin,
                    marketplace_id,
                    snapshot_time,
                    int(r.get("highly_available_inventory", 0) or 0),
                ),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Public sync functions (with TODOs for real SP-API wiring)
# ---------------------------------------------------------------------------

def sync_vendor_sales_history(start_date: date | None = None, end_date: date | None = None) -> Dict[str, Any]:
    """
    Populate vendor_sales_history from GET_VENDOR_SALES_REPORT.
    """
    logger.info("[forecast_sync] Starting sync_vendor_sales_history")
    # Determine safe window end (at least 7 days behind now)
    now_utc = datetime.now(timezone.utc)
    default_end = now_utc - timedelta(days=8)
    if end_date:
        safe_end = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)
        if safe_end > default_end:
            safe_end = default_end
    else:
        safe_end = default_end

    tracking_start_str = os.getenv("TRACKING_START")
    if start_date:
        window_start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    elif tracking_start_str:
        ts = tracking_start_str.replace("Z", "+00:00")
        try:
            window_start_dt = datetime.fromisoformat(ts)
        except Exception:
            window_start_dt = safe_end - timedelta(days=30)
    else:
        window_start_dt = safe_end - timedelta(days=30)

    if window_start_dt > safe_end:
        window_start_dt = safe_end - timedelta(days=1)

    inserted_total = 0
    parsed_total = 0
    chunks_parsed = 0

    for chunk_start_date, chunk_end_date in _iter_date_chunks(
        window_start_dt.date(), safe_end.date(), max_days=15
    ):
        chunk_start_dt = datetime.combine(chunk_start_date, datetime.min.time(), tzinfo=timezone.utc)
        chunk_end_dt = datetime.combine(chunk_end_date, datetime.max.time(), tzinfo=timezone.utc)
        chunks_parsed += 1

        logger.info(
            "[forecast_sync] Requesting sales chunk %s -> %s",
            chunk_start_dt.isoformat(),
            chunk_end_dt.isoformat(),
        )

        report_id = request_vendor_report(
            "GET_VENDOR_SALES_REPORT",
            data_start=chunk_start_dt,
            data_end=chunk_end_dt,
            report_period="DAY",
        )
        report_status = poll_vendor_report(report_id)
        document_id = report_status.get("reportDocumentId")
        if not document_id:
            logger.warning("[forecast_sync] No documentId for report %s", report_id)
            continue

        report_rows_raw = download_vendor_report_document(document_id)

        # Handle error envelopes
        if _is_error_envelope(report_rows_raw):
            logger.error(
                "[forecast_sync] Sales report chunk %s -> %s returned error envelope: %s",
                chunk_start_dt.date(),
                chunk_end_dt.date(),
                report_rows_raw,
            )
            continue

        # Normalize payload into a list of row dicts
        chunk_rows: List[Dict[str, Any]] = []
        if isinstance(report_rows_raw, dict):
            chunk_rows = parse_vendor_sales_json(report_rows_raw)
        elif isinstance(report_rows_raw, list):
            chunk_rows = parse_vendor_sales_json(report_rows_raw)
        elif isinstance(report_rows_raw, (bytes, bytearray)):
            # TSV fallback -> map to flattened fields
            for row in parse_report_tsv(report_rows_raw):
                if not isinstance(row, dict):
                    continue
                asin = row.get("asin") or row.get("ASIN")
                sales_date = (
                    row.get("reportingDate")
                    or row.get("date")
                    or row.get("period")
                    or row.get("startDate")
                    or row.get("endDate")
                )
                if not asin or not sales_date:
                    continue
                chunk_rows.append(
                    {
                        "asin": asin,
                        "sales_date": sales_date,
                        "shipped_units": int(row.get("shippedUnits") or row.get("orderedUnits") or row.get("units") or 0),
                        "customer_returns": int(row.get("customerReturns") or row.get("returnsUnits") or 0),
                        "shipped_revenue": safe_float(row.get("shippedRevenue") or row.get("orderedRevenue") or row.get("revenue") or 0),
                        "shipped_cogs": safe_float(row.get("shippedCogs") or 0),
                    }
                )
        elif isinstance(report_rows_raw, str):
            for row in parse_report_tsv(report_rows_raw.encode("utf-8", errors="ignore")):
                if not isinstance(row, dict):
                    continue
                asin = row.get("asin") or row.get("ASIN")
                sales_date = (
                    row.get("reportingDate")
                    or row.get("date")
                    or row.get("period")
                    or row.get("startDate")
                    or row.get("endDate")
                )
                if not asin or not sales_date:
                    continue
                chunk_rows.append(
                    {
                        "asin": asin,
                        "sales_date": sales_date,
                        "shipped_units": int(row.get("shippedUnits") or row.get("orderedUnits") or row.get("units") or 0),
                        "customer_returns": int(row.get("customerReturns") or row.get("returnsUnits") or 0),
                        "shipped_revenue": safe_float(row.get("shippedRevenue") or row.get("orderedRevenue") or row.get("revenue") or 0),
                        "shipped_cogs": safe_float(row.get("shippedCogs") or 0),
                    }
                )
        else:
            logger.warning(
                "[forecast_sync] Unexpected sales history payload type: %s",
                type(report_rows_raw),
            )

        if chunk_rows:
            parsed_total += len(chunk_rows)
            inserted_total += _upsert_sales_history(chunk_rows)
        logger.info(
            "[forecast_sync] Parsed %d sales rows from chunk %s -> %s",
            len(chunk_rows),
            chunk_start_dt.date(),
            chunk_end_dt.date(),
        )

    logger.info(
        "[forecast_sync] Completed sync_vendor_sales_history (rows=%d, chunks=%d)",
        inserted_total,
        chunks_parsed,
    )
    status = "ok" if inserted_total > 0 else "warning"
    return {
        "sales_rows": inserted_total,
        "parsed_rows": parsed_total,
        "chunks": chunks_parsed,
        "start": window_start_dt.date().isoformat(),
        "end": safe_end.date().isoformat(),
        "status": status,
    }


def sync_vendor_forecast(start_date: datetime | None = None, end_date: datetime | None = None) -> Dict[str, Any]:
    """
    Populate vendor_forecast from GET_VENDOR_FORECASTING_REPORT.
    """
    global _forecast_cooldown_until
    logger.info("[forecast_sync] Starting sync_vendor_forecast")
    now_utc = end_date or datetime.now(timezone.utc)
    window_start = start_date or (now_utc - timedelta(days=90))

    if _forecast_cooldown_until and datetime.now(timezone.utc) < _forecast_cooldown_until:
        wait_minutes = int((_forecast_cooldown_until - datetime.now(timezone.utc)).total_seconds() // 60)
        logger.warning(
            "[forecast_sync] Forecast sync in cooldown for %s more minutes; skipping forecast run",
            wait_minutes,
        )
        return {
            "forecast_rows": 0,
            "start": window_start.isoformat(),
            "end": now_utc.isoformat(),
            "status": "warning",
            "error": f"forecast cooldown {wait_minutes}m remaining",
        }

    try:
        report_id = request_vendor_report(
            "GET_VENDOR_FORECASTING_REPORT",
            data_start=window_start,
            data_end=now_utc,
            # Selling program defaults handled in request_vendor_report (RETAIL for forecast)
            selling_program=None,
        )
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code == 429:
            cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            _forecast_cooldown_until = cooldown_until
            logger.warning(
                "[forecast_sync] Forecast report quota exceeded (429); entering cooldown until %s",
                cooldown_until.isoformat(),
            )
            return {
                "forecast_rows": 0,
                "start": window_start.isoformat(),
                "end": now_utc.isoformat(),
                "status": "warning",
                "error": f"quota exceeded; cooldown until {cooldown_until.isoformat()}",
            }
        raise
    report_status = poll_vendor_report(report_id)
    document_id = report_status.get("reportDocumentId")
    if not document_id:
        logger.warning("[forecast_sync] No documentId for forecast report %s", report_id)
        return {"forecast_rows": 0, "start": window_start.isoformat(), "end": now_utc.isoformat(), "status": "error", "error": "no documentId"}

    logger.info("[forecast_sync] Downloading document %s", document_id)
    raw_payload = download_vendor_report_document(document_id)

    # Skip error envelopes
    if _is_error_envelope(raw_payload):
        details = raw_payload.get("errorDetails") or raw_payload.get("reportRequestError") if isinstance(raw_payload, dict) else raw_payload
        logger.error("[forecast_sync] Forecast report returned error envelope: %s", details)
        raise ForecastSyncError(f"Forecast report error: {details}")

    forecast_rows: List[Dict[str, Any]] = []

    if isinstance(raw_payload, dict):
        # JSON structure: look for forecastByAsin/forecasts…
        entries = (
            raw_payload.get("forecastByAsin")
            or raw_payload.get("forecastsByAsin")
            or raw_payload.get("forecasts")
            or raw_payload
        )
        if isinstance(entries, list):
            forecast_rows = [e for e in entries if isinstance(e, dict)]
        elif isinstance(entries, dict):
            forecast_rows = [entries]
        else:
            logger.warning(
                "[forecast_sync] Unexpected forecast JSON structure: %s",
                type(entries),
            )
    elif isinstance(raw_payload, list):
        forecast_rows = [r for r in raw_payload if isinstance(r, dict)]
    elif isinstance(raw_payload, (bytes, bytearray)):
        # TSV/CSV style
        forecast_rows = list(parse_report_tsv(raw_payload))
    elif isinstance(raw_payload, str):
        forecast_rows = list(parse_report_tsv(raw_payload.encode("utf-8", errors="ignore")))
    else:
        logger.warning(
            "[forecast_sync] Unexpected forecast payload type: %s",
            type(raw_payload),
        )

    if not forecast_rows:
        logger.info("[forecast_sync] No forecast rows to upsert")
        return {"forecast_rows": 0, "start": window_start.isoformat(), "end": now_utc.isoformat(), "status": "warning"}

    _upsert_forecast(forecast_rows)
    logger.info(
        "[forecast_sync] Completed sync_vendor_forecast (rows=%d)",
        len(forecast_rows),
    )
    return {
        "forecast_rows": len(forecast_rows),
        "start": window_start.isoformat(),
        "end": now_utc.isoformat(),
        "status": "ok",
    }

# ---------------------------------------------------------------------------


def sync_vendor_rt_inventory() -> Dict[str, Any]:
    """
    Populate vendor_rt_inventory from GET_VENDOR_REAL_TIME_INVENTORY_REPORT.
    """
    global _last_inventory_sync_at, _inventory_cooldown_until
    logger.info("[forecast_sync] Starting sync_vendor_rt_inventory")
    if _inventory_lock.locked():
        logger.warning("[forecast_sync] Inventory sync already in progress; skipping this run")
        return {"inventory_rows": 0, "status": "warning", "error": "inventory sync already running"}

    now_utc = datetime.now(timezone.utc)
    if _inventory_cooldown_until and now_utc < _inventory_cooldown_until:
        wait_minutes = int((_inventory_cooldown_until - now_utc).total_seconds() // 60)
        logger.warning(
            "[forecast_sync] Inventory sync in cooldown for %s more minutes; skipping this run",
            wait_minutes,
        )
        return {
            "inventory_rows": 0,
            "status": "warning",
            "error": f"inventory cooldown {wait_minutes}m remaining",
        }

    with _inventory_lock:
        start_dt = now_utc - timedelta(days=1)

        # Retry/backoff for 429 quota exceeded
        backoff_minutes = 30
        attempts = 1
        report_id = None
        for attempt in range(attempts):
            try:
                report_id = request_vendor_report(
                    "GET_VENDOR_REAL_TIME_INVENTORY_REPORT",
                    data_start=start_dt,
                    data_end=now_utc,
                    selling_program=None,
                )
                break
            except Exception as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code == 429:
                    cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes)
                    _inventory_cooldown_until = cooldown_until
                    logger.warning(
                        "[forecast_sync] Inventory report quota exceeded (429); entering cooldown until %s",
                        cooldown_until.isoformat(),
                    )
                    return {
                        "inventory_rows": 0,
                        "status": "warning",
                        "error": f"quota exceeded; cooldown until {cooldown_until.isoformat()}",
                    }
                raise

        logger.info(
            "[forecast_sync] Inventory window %s -> %s",
            start_dt.isoformat(),
            now_utc.isoformat(),
        )
        report_status = poll_vendor_report(report_id)
        document_id = report_status.get("reportDocumentId")
        if not document_id:
            logger.warning("[forecast_sync] No documentId for inventory report %s", report_id)
            return {"inventory_rows": 0, "status": "error", "error": "no documentId"}

        logger.info("[forecast_sync] Downloading document %s", document_id)
        raw_payload = download_vendor_report_document(document_id)

        # Skip error envelopes
        if _is_error_envelope(raw_payload):
            logger.error("[forecast_sync] Inventory report returned error envelope: %s", raw_payload)
            return {"inventory_rows": 0, "status": "error", "error": "error envelope"}

        inventory_rows: List[Dict[str, Any]] = []
        expected_rows = 0

        if isinstance(raw_payload, dict):
            # JSON structure: reportData preferred
            entries = (
                raw_payload.get("reportData")
                or raw_payload.get("inventoryByAsin")
                or raw_payload.get("inventory")
                or raw_payload
            )
            if isinstance(entries, list):
                expected_rows = len(entries)
                inventory_rows = [e for e in entries if isinstance(e, dict)]
            elif isinstance(entries, dict):
                inventory_rows = [entries]
            else:
                logger.warning(
                    "[forecast_sync] Unexpected inventory JSON structure: %s",
                    type(entries),
                )
        elif isinstance(raw_payload, list):
            expected_rows = len(raw_payload)
            inventory_rows = [r for r in raw_payload if isinstance(r, dict)]
        elif isinstance(raw_payload, (bytes, bytearray)):
            inventory_rows = list(parse_report_tsv(raw_payload))
            expected_rows = len(inventory_rows)
        elif isinstance(raw_payload, str):
            inventory_rows = list(parse_report_tsv(raw_payload.encode("utf-8", errors="ignore")))
            expected_rows = len(inventory_rows)
        else:
            logger.warning(
                "[forecast_sync] Unexpected inventory payload type: %s",
                type(raw_payload),
            )

        if inventory_rows and isinstance(inventory_rows, str):
            logger.error("[forecast_sync] BUG: inventory_rows is a string, not list of dicts")
            raise TypeError("inventory_rows must be Iterable[dict], not str")

        if expected_rows == 0:
            expected_rows = len(inventory_rows)
        logger.info(
            "[forecast_sync] Parsed %d inventory rows from payload (expected=%s)",
            len(inventory_rows),
            expected_rows,
        )

        normalized_rows: List[Dict[str, Any]] = []
        for row in inventory_rows:
            asin = (row.get("asin") or row.get("ASIN") or "").strip()
            if not asin:
                logger.warning("[forecast_sync] Skipping inventory row without ASIN: %r", row)
                continue
            snapshot = (
                row.get("snapshot_time")
                or row.get("snapshotTime")
                or row.get("startTime")
                or row.get("endTime")
                or row.get("lastUpdatedTime")
            )
            normalized_rows.append(
                {
                    **row,
                    "asin": asin,
                    "snapshot_time": snapshot,
                    "marketplace_id": (row.get("marketplaceId") or row.get("marketplace_id") or "UNKNOWN"),
                    "highly_available_inventory": row.get("highlyAvailableInventory", row.get("available")),
                }
            )

        _upsert_inventory(normalized_rows)
        logger.info(
            "[forecast_sync] Completed sync_vendor_rt_inventory (rows=%d)",
            len(normalized_rows),
        )
        status = "ok"
        if expected_rows and len(normalized_rows) < max(10, expected_rows // 2):
            logger.warning(
                "[forecast_sync] Inventory upserts low vs expected (expected=%s, upserted=%s)",
                expected_rows,
                len(normalized_rows),
            )
            status = "warning"
        _last_inventory_sync_at = datetime.now(timezone.utc)
        return {"inventory_rows": len(normalized_rows), "expected_rows": expected_rows, "status": status}


def sync_all_forecast_sources(start_date: date | None = None, end_date: date | None = None) -> Dict[str, Any]:
    """
    Convenience helper that runs all three syncs in sequence.
    Raises an exception if any individual sync fails.
    """
    if not _sync_lock.acquire(blocking=False):
        raise ForecastSyncError("sync already running")
    try:
        # Enforce once-per-24h guard
        now = datetime.now(timezone.utc)
        last_sync = _load_last_full_sync()
        if last_sync and (now - last_sync) < timedelta(hours=24):
            next_allowed = last_sync + timedelta(hours=24)
            logger.warning(
                "[forecast_sync] Last full sync at %s; skipping until %s",
                last_sync.isoformat(),
                next_allowed.isoformat(),
            )
            # Refresh the persisted timestamp to avoid repeated attempts in tight loops
            _save_last_full_sync(last_sync)
            statuses = {"sales": "warning", "forecast": "warning", "inventory": "warning"}
            return {
                "status": "warning",
                "statuses": statuses,
                "error": "sync_recent",
                "last_sync": last_sync.isoformat(),
                "next_allowed": next_allowed.isoformat(),
            }

        logger.info("Running sync_vendor_sales_history()")
        try:
            sales_result = sync_vendor_sales_history(start_date=start_date, end_date=end_date)
        except SpApiQuotaError as exc:
            logger.error("[forecast_sync] Sales sync quota exceeded: %s", exc)
            raise ForecastSyncError("quota_exceeded_sales") from exc

        logger.info("Running sync_vendor_forecast()")
        try:
            forecast_result = sync_vendor_forecast(
                start_date=datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
                if start_date
                else None,
                end_date=datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)
                if end_date
                else None,
            )
        except SpApiQuotaError as exc:
            logger.error("[forecast_sync] Forecast sync quota exceeded: %s", exc)
            raise ForecastSyncError("quota_exceeded_forecast") from exc

        logger.info("Running sync_vendor_rt_inventory()")
        try:
            inventory_result = sync_vendor_rt_inventory()
        except SpApiQuotaError as exc:
            logger.error("[forecast_sync] Inventory sync quota exceeded: %s", exc)
            raise ForecastSyncError("quota_exceeded_inventory") from exc
        results = {
            "sales": sales_result,
            "forecast": forecast_result,
            "inventory": inventory_result,
        }
        statuses = {
            "sales": sales_result.get("status", "ok"),
            "forecast": forecast_result.get("status", "ok"),
            "inventory": inventory_result.get("status", "ok"),
        }
        overall_status = "ok"
        if "error" in statuses.values():
            overall_status = "error"
        elif "warning" in statuses.values():
            overall_status = "warning"
        logger.info("[forecast_sync] sync_all_forecast_sources status=%s details=%s", overall_status, results)
        if overall_status == "error":
            raise ForecastSyncError(f"One or more syncs failed: {statuses}")
        # Persist last sync time for ok or warning to avoid repeated runs within 24h
        if overall_status in ("ok", "warning"):
            _save_last_full_sync(now)
        return {"status": overall_status, "statuses": statuses, **results}
    finally:
        if _sync_lock.locked():
            _sync_lock.release()


