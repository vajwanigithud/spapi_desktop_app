import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from services.spapi_reports import (
    REPORTS_API_HOST,
    auth_client,
    poll_vendor_report,
    request_vendor_report,
)
from services.vendor_rt_inventory_state import (
    DEFAULT_CATALOG_DB_PATH,
    apply_incremental_rows,
    get_checkpoint,
    get_state_rows,
    parse_end_time,
    set_checkpoint,
)

LOGGER = logging.getLogger(__name__)
PST = ZoneInfo("America/Los_Angeles")


def compute_window(hours: int) -> Tuple[datetime, datetime]:
    if hours < 1:
        raise ValueError("hours must be >= 1")
    if hours > 24:
        raise ValueError("hours must be <= 24")
    now_pst = datetime.now(PST)
    end = (now_pst - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    return start, end


def _iso_to_datetime(value: str) -> datetime:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("ISO datetime value is required")
    if candidate.endswith("Z"):
        candidate = candidate.replace("Z", "+00:00")
    dt = datetime.fromisoformat(candidate)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def download_report_document(document_id: str) -> Any:
    access_token = auth_client.get_lwa_access_token()
    meta_url = f"{REPORTS_API_HOST}/reports/2021-06-30/documents/{document_id}"
    headers = {
        "x-amz-access-token": access_token,
        "accept": "application/json",
    }
    meta_resp = requests.get(meta_url, headers=headers, timeout=30)
    meta_resp.raise_for_status()
    meta = meta_resp.json()
    download_url = meta.get("url")
    if not download_url:
        raise RuntimeError(f"Missing download URL for document {document_id}")
    compression = (meta.get("compressionAlgorithm") or "").upper()

    doc_resp = requests.get(download_url, timeout=60)
    doc_resp.raise_for_status()
    content = doc_resp.content

    if compression == "GZIP":
        try:
            content = gzip.decompress(content)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Failed to decompress GZIP payload: %s", exc)

    try:
        return json.loads(content.decode("utf-8-sig"))
    except Exception:
        return json.loads(content)


def extract_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("reportData", "data"):
            block = payload.get(key)
            if isinstance(block, dict):
                items = block.get("items")
                if isinstance(items, list):
                    return items
            elif isinstance(block, list):
                return block
        items = payload.get("items")
        if isinstance(items, list):
            return items
    raise ValueError("Could not extract items from payload")


def request_report_window(
    start: datetime,
    end: datetime,
    marketplace: str,
    timeout: int,
    poll_interval: int,
) -> List[Dict[str, Any]]:
    report_id = request_vendor_report(
        report_type="GET_VENDOR_REAL_TIME_INVENTORY_REPORT",
        params={"marketplaceIds": [marketplace]},
        data_start=start,
        data_end=end,
        selling_program="RETAIL",
    )
    LOGGER.info("Created report %s", report_id)
    meta = poll_vendor_report(
        report_id,
        timeout_seconds=timeout,
        poll_interval_seconds=poll_interval,
    )
    document_id = meta.get("reportDocumentId")
    if meta.get("processingStatus") != "DONE" or not document_id:
        raise RuntimeError(f"Report {report_id} did not complete successfully: {meta}")
    LOGGER.info("Report %s DONE with document %s", report_id, document_id)
    payload = download_report_document(document_id)
    return extract_rows(payload)


def _plan_sync_window(
    marketplace_id: str,
    db_path: Path,
    hours: int,
) -> Dict[str, Any]:
    checkpoint_iso = get_checkpoint(marketplace_id, db_path=db_path)
    use_checkpoint = False
    start_pst: Optional[datetime] = None
    end_pst: Optional[datetime] = None
    _prev_hour_start, prev_hour_end = compute_window(1)
    if checkpoint_iso:
        try:
            checkpoint_dt_utc = _iso_to_datetime(checkpoint_iso)
            checkpoint_dt_pst = checkpoint_dt_utc.astimezone(PST)
            if checkpoint_dt_pst < prev_hour_end:
                start_pst = checkpoint_dt_pst
                end_pst = prev_hour_end
                use_checkpoint = True
            else:
                return {
                    "status": "up_to_date",
                    "as_of": checkpoint_iso,
                }
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "Invalid checkpoint %s (%s); falling back to --hours",
                checkpoint_iso,
                exc,
            )
    if not start_pst or not end_pst:
        start_pst, end_pst = compute_window(hours)
    if use_checkpoint:
        start_pst = start_pst + timedelta(seconds=1)
    return {
        "status": "run",
        "start_pst": start_pst,
        "end_pst": end_pst,
        "checkpoint": checkpoint_iso,
    }


def sync_vendor_rt_inventory(
    marketplace_id: str,
    *,
    db_path: Path = DEFAULT_CATALOG_DB_PATH,
    hours: int = 2,
    timeout: int = 1200,
    poll_interval: int = 15,
    include_items: bool = True,
) -> Dict[str, Any]:
    """
    Run the Vendor RT inventory incremental sync and return snapshot info.
    """
    plan = _plan_sync_window(marketplace_id, db_path, hours)
    if plan["status"] == "up_to_date":
        items = get_state_rows(marketplace_id, db_path=db_path) if include_items else None
        return {
            "status": "up_to_date",
            "marketplace_id": marketplace_id,
            "as_of": plan.get("as_of"),
            "items": items,
            "stats": None,
            "row_count": 0,
            "min_end": None,
            "max_end": None,
            "asin_count": 0,
        }

    start_pst: datetime = plan["start_pst"]
    end_pst: datetime = plan["end_pst"]
    LOGGER.info(
        "Requesting window %s to %s (PST) for incremental sync",
        start_pst.isoformat(),
        end_pst.isoformat(),
    )
    rows = request_report_window(start_pst, end_pst, marketplace_id, timeout, poll_interval)
    LOGGER.info("Fetched %s rows from report", len(rows))

    asin_set = set()
    end_times: List[str] = []
    for row in rows:
        asin = (row.get("asin") or "").strip().upper()
        if asin:
            asin_set.add(asin)
        end_iso = parse_end_time(row.get("endTime") or row.get("end_time"))
        if end_iso:
            end_times.append(end_iso)
    min_end = min(end_times) if end_times else None
    max_end_rows = max(end_times) if end_times else None
    LOGGER.info(
        "Fetched rows summary: rows=%s distinct_asins=%s endTime range=%s -> %s",
        len(rows),
        len(asin_set),
        min_end,
        max_end_rows,
    )

    stats = apply_incremental_rows(
        rows,
        marketplace_id=marketplace_id,
        db_path=db_path,
    )
    LOGGER.info("Incremental apply stats: %s", stats)
    max_end_stats = stats.get("max_end_time") if isinstance(stats, dict) else None
    max_end = max_end_stats or max_end_rows
    if max_end:
        try:
            new_checkpoint_dt = _iso_to_datetime(max_end)
            existing_checkpoint_dt = (
                _iso_to_datetime(plan.get("checkpoint")) if plan.get("checkpoint") else None
            )
            if not existing_checkpoint_dt or new_checkpoint_dt > existing_checkpoint_dt:
                set_checkpoint(marketplace_id, max_end, db_path=db_path)
                LOGGER.info("Updated checkpoint for %s to %s", marketplace_id, max_end)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Failed to update checkpoint for %s: %s", marketplace_id, exc)

    as_of = get_checkpoint(marketplace_id, db_path=db_path)
    items = get_state_rows(marketplace_id, db_path=db_path) if include_items else None
    return {
        "status": "synced",
        "marketplace_id": marketplace_id,
        "as_of": as_of,
        "items": items,
        "stats": stats,
        "row_count": len(rows),
        "min_end": min_end,
        "max_end": max_end_rows,
        "asin_count": len(asin_set),
    }
