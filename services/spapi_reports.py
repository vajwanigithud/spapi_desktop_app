import logging
import logging
import os
import time
import gzip
import json
import requests
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List

class SpApiQuotaError(RuntimeError):
    """Raised when SP-API returns a QuotaExceeded / 429."""
    pass

# Recent changes:
# - Forecast reports default to sellingProgram=RETAIL (avoid Vendor Fulfilled FATAL errors).
# - Lightweight client wrapper for createReport to keep logging consistent.

from auth.spapi_auth import SpApiAuth

logger = logging.getLogger("spapi_reports")
auth_client = SpApiAuth()

REPORTS_API_HOST = os.getenv("REPORTS_API_HOST", "https://sellingpartnerapi-eu.amazon.com")


def _get_marketplace_ids() -> Any:
    env_ids = os.getenv("MARKETPLACE_IDS") or os.getenv("MARKETPLACE_ID", "")
    ids = [mp.strip() for mp in env_ids.split(",") if mp.strip()]
    return ids


class _ReportsApiClient:
    def createReport(self, body: Dict[str, Any]) -> Dict[str, Any]:
        access_token = auth_client.get_lwa_access_token()
        url = f"{REPORTS_API_HOST}/reports/2021-06-30/reports"
        headers = {
            "content-type": "application/json",
            "x-amz-access-token": access_token,
            "accept": "application/json",
        }
        resp = requests.post(url, json=body, headers=headers, timeout=30)
        if resp.status_code == 429:
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
            logger.error(
                "[spapi_reports] createReport failed 429 QuotaExceeded for body=%s resp=%s",
                body,
                payload,
            )
            raise SpApiQuotaError(f"QuotaExceeded creating report: {payload}")
        if resp.status_code >= 300:
            logger.error(
                "[spapi_reports] createReport failed %s: %s",
                resp.status_code,
                resp.text,
            )
            resp.raise_for_status()
        return resp.json()


def get_spapi_client() -> _ReportsApiClient:
    return _ReportsApiClient()


def request_vendor_report(
    report_type: str,
    params: Optional[Dict[str, Any]] = None,
    data_start: Optional[datetime] = None,
    data_end: Optional[datetime] = None,
    report_period: Optional[str] = None,
    selling_program: Optional[str] = None,
    distributor_view: Optional[str] = None,
    extra_options: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Wrapper around createReport for vendor reports.

    - Accepts optional explicit data_start / data_end or picks them up from params.
    - Applies sensible defaults per report type.
    - Handles:
      * GET_VENDOR_FORECASTING_REPORT  -> no date range allowed
      * GET_VENDOR_REAL_TIME_INVENTORY_REPORT -> end must be <= last completed hour
    """

    params = params or {}

    logger.info(
        "[spapi_reports] request_vendor_report type=%s, params=%s",
        report_type,
        params,
    )

    # Determine marketplaces
    marketplace_ids: List[str] = (
        params.get("marketplaceIds") or _get_marketplace_ids()
    )

    # Helper to parse any date-like thing
    def _parse_dt(val: Any) -> Optional[datetime]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        if isinstance(val, str):
            s = val
            if s.endswith("Z"):
                s = s.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s)
            except Exception:
                logger.warning(
                    "[spapi_reports] Could not parse datetime value %r", val
                )
                return None
        return None

    # Allow data_start / data_end to be specified either as explicit args
    # OR inside params under common keys.
    if data_start is None:
        for key in ("dataStartTime", "data_start", "startDate"):
            if key in params:
                data_start = _parse_dt(params[key])
                break

    if data_end is None:
        for key in ("dataEndTime", "data_end", "endDate"):
            if key in params:
                data_end = _parse_dt(params[key])
                break

    # Default options by report type
    if report_type == "GET_VENDOR_SALES_REPORT":
        if report_period is None:
            report_period = "DAY"
        if selling_program is None:
            selling_program = "RETAIL"
        if distributor_view is None:
            distributor_view = "SOURCING"

    if report_type == "GET_VENDOR_FORECASTING_REPORT":
        # Forecast reports expect RETAIL (or omission). Avoid VENDOR_FULFILLED which causes FATAL.
        if selling_program is None:
            selling_program = "RETAIL"
    elif report_type == "GET_VENDOR_REAL_TIME_INVENTORY_REPORT":
        # Use RETAIL unless explicitly overridden.
        if selling_program is None:
            selling_program = "RETAIL"

    # ── Special cases for date handling ──────────────────────────────

    # 1) FORECAST: Amazon explicitly says "do NOT specify a date range"
    if report_type == "GET_VENDOR_FORECASTING_REPORT":
        if data_start or data_end:
            logger.info(
                "[spapi_reports] Dropping dataStartTime/dataEndTime for %s; "
                "report type does not support a date range.",
                report_type,
            )
        data_start = None
        data_end = None

    # 2) REAL-TIME INVENTORY: hourly data; can't ask for "now" exactly
    if report_type == "GET_VENDOR_REAL_TIME_INVENTORY_REPORT" and data_end is not None:
        now_utc = datetime.now(timezone.utc)

        # Last fully available hour (be conservative: one full hour behind)
        last_full_hour = (
            now_utc.replace(minute=0, second=0, microsecond=0)
            - timedelta(hours=1)
        )

        if data_end.tzinfo is None:
            data_end = data_end.replace(tzinfo=timezone.utc)

        if data_end > last_full_hour:
            logger.info(
                "[spapi_reports] Adjusting RT inventory data_end from %s to %s "
                "to satisfy hourly availability.",
                data_end.isoformat(),
                last_full_hour.isoformat(),
            )
            data_end = last_full_hour

        if data_start is not None:
            if data_start.tzinfo is None:
                data_start = data_start.replace(tzinfo=timezone.utc)
            if data_start >= data_end:
                # keep at least a small window (3 days back)
                new_start = data_end - timedelta(days=3)
                logger.info(
                    "[spapi_reports] Adjusting RT inventory data_start from %s to %s "
                    "to keep it before data_end.",
                    data_start.isoformat(),
                    new_start.isoformat(),
                )
                data_start = new_start

    # ── Build createReport payload ───────────────────────────────────

    body: Dict[str, Any] = {
        "reportType": report_type,
        "marketplaceIds": marketplace_ids,
    }

    if data_start is not None:
        if data_start.tzinfo is None:
            data_start = data_start.replace(tzinfo=timezone.utc)
        body["dataStartTime"] = (
            data_start.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    if data_end is not None:
        if data_end.tzinfo is None:
            data_end = data_end.replace(tzinfo=timezone.utc)
        body["dataEndTime"] = (
            data_end.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    # Report options
    report_options: Dict[str, Any] = {}
    if report_period:
        report_options["reportPeriod"] = report_period
    if selling_program:
        report_options["sellingProgram"] = selling_program
    if distributor_view:
        report_options["distributorView"] = distributor_view
    if extra_options:
        report_options.update(extra_options)

    if report_options:
        body["reportOptions"] = report_options

    logger.info("[spapi_reports] Final createReport payload: %s", body)

    client = get_spapi_client()
    resp = client.createReport(body=body)
    report_id = resp["reportId"]
    logger.info(
        "[spapi_reports] Created report %s reportId=%s",
        report_type,
        report_id,
    )
    return report_id


def poll_vendor_report(
    report_id: str, timeout_seconds: int = 600, poll_interval_seconds: int = 20
) -> Dict[str, Any]:
    logger.info(f"[spapi_reports] poll_vendor_report report_id={report_id}")
    access_token = auth_client.get_lwa_access_token()
    url = f"{REPORTS_API_HOST}/reports/2021-06-30/reports/{report_id}"
    headers = {
        "x-amz-access-token": access_token,
        "accept": "application/json",
    }
    deadline = time.time() + timeout_seconds
    last_status = None
    while True:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code >= 300:
            logger.error(f"[spapi_reports] getReport failed {resp.status_code}: {resp.text}")
            resp.raise_for_status()
        data = resp.json()
        status = data.get("processingStatus")
        document_id = data.get("reportDocumentId")
        if status != last_status:
            logger.info(f"[spapi_reports] report {report_id} status={status}")
            last_status = status
        if status == "DONE":
            return data
        if status == "FATAL":
            if document_id:
                logger.warning(
                    "[spapi_reports] report %s ended FATAL but has document_id=%s; returning data",
                    report_id,
                    document_id,
                )
                return data
            raise RuntimeError(f"Report {report_id} failed with status {status}: {data}")
        if status == "CANCELLED":
            raise RuntimeError(f"Report {report_id} failed with status {status}: {data}")
        if time.time() > deadline:
            raise TimeoutError(f"Polling timed out for report {report_id}")
        time.sleep(poll_interval_seconds)


def download_vendor_report_document(document_id: str) -> tuple:
    """
    Download and decompress vendor report document.
    
    NOTE: Report document URLs have an expiration time. Do NOT cache the URL
    beyond the expiresAt/expirationTime returned by getReportDocument. Re-call
    this function if the URL expires.
    
    Raises:
        SpApiQuotaError: If the getReportDocument or document download returns 429 QuotaExceeded
    
    Returns:
        tuple: (content, expiration_info) where:
            - content is the decompressed document (dict, list, or bytes)
            - expiration_info is dict with keys: "expiresAt", "url", or None if not provided
    """
    logger.info(f"[spapi_reports] download_vendor_report_document document_id={document_id}")
    access_token = auth_client.get_lwa_access_token()
    meta_url = f"{REPORTS_API_HOST}/reports/2021-06-30/documents/{document_id}"
    headers = {
        "x-amz-access-token": access_token,
        "accept": "application/json",
    }
    meta_resp = requests.get(meta_url, headers=headers, timeout=30)
    
    # Check for quota exceeded on getReportDocument call
    if meta_resp.status_code == 429:
        try:
            payload = meta_resp.json()
        except Exception:
            payload = meta_resp.text
        logger.error(f"[spapi_reports] getReportDocument failed 429 QuotaExceeded: {payload}")
        raise SpApiQuotaError(f"QuotaExceeded downloading report document: {payload}")
    
    if meta_resp.status_code >= 300:
        logger.error(f"[spapi_reports] getReportDocument failed {meta_resp.status_code}: {meta_resp.text}")
        meta_resp.raise_for_status()
    meta = meta_resp.json()
    download_url = meta.get("url")
    compression = meta.get("compressionAlgorithm")
    
    # Extract expiration info if present (schema may include expiresAt or expirationTime)
    expiration_info = None
    if meta.get("expiresAt"):
        expiration_info = {"expiresAt": meta.get("expiresAt"), "url": download_url}
        logger.info(f"[spapi_reports] Document {document_id} expires at {meta.get('expiresAt')}")
    elif meta.get("expirationTime"):
        expiration_info = {"expirationTime": meta.get("expirationTime"), "url": download_url}
        logger.info(f"[spapi_reports] Document {document_id} expires at {meta.get('expirationTime')}")
    
    if not download_url:
        raise RuntimeError(f"Missing download URL for document {document_id}: {meta}")

    doc_resp = requests.get(download_url, timeout=60)
    
    # Check for quota exceeded on document download
    if doc_resp.status_code == 429:
        try:
            payload = doc_resp.json()
        except Exception:
            payload = doc_resp.text
        logger.error(f"[spapi_reports] Document download failed 429 QuotaExceeded: {payload}")
        raise SpApiQuotaError(f"QuotaExceeded downloading report document payload: {payload}")
    
    if doc_resp.status_code >= 300:
        logger.error(f"[spapi_reports] Document download failed {doc_resp.status_code}")
        doc_resp.raise_for_status()
    content = doc_resp.content
    if compression and compression.upper() == "GZIP":
        try:
            content = gzip.decompress(content)
        except OSError:
            # Not actually gzip, treat as plain
            pass
        except Exception as exc:
            logger.error(f"[spapi_reports] GZIP decompress failed: {exc}")
            pass
    # ----------------------------------------------------
    # Debug logging of decoded payload (size/preview/JSON)
    # ----------------------------------------------------
    logger.info(
        "[spapi_reports] document %s raw size=%s bytes",
        document_id,
        len(content) if isinstance(content, (bytes, bytearray)) else "n/a",
    )

    decoded_text = None
    decoded_obj = None
    if isinstance(content, (bytes, bytearray)):
        try:
            decoded_text = content.decode("utf-8-sig", errors="ignore")
            try:
                decoded_obj = json.loads(decoded_text)
            except Exception:
                decoded_obj = None
        except Exception:
            decoded_text = None
            decoded_obj = None
            logger.exception(
                "[spapi_reports] Error while decoding document %s", document_id
            )

    # If JSON error envelopes, return dict so callers can skip
    if isinstance(decoded_obj, dict):
        logger.info(
            "[spapi_reports] document %s JSON top-level dict keys=%s",
            document_id,
            list(decoded_obj.keys())[:20],
        )
        if "errorDetails" in decoded_obj:
            details = decoded_obj["errorDetails"]
            try:
                details_json = json.dumps(details, ensure_ascii=False)
            except Exception:
                details_json = str(details)
            logger.error(
                "[spapi_reports] Vendor report document %s contains errorDetails: %s",
                document_id,
                details_json,
            )
            return decoded_obj, expiration_info
        if "reportRequestError" in decoded_obj:
            details = decoded_obj["reportRequestError"]
            try:
                details_json = json.dumps(details, ensure_ascii=False)
            except Exception:
                details_json = str(details)
            logger.error(
                "[spapi_reports] Vendor report document %s contains reportRequestError: %s",
                document_id,
                details_json,
            )
            return decoded_obj, expiration_info
    elif isinstance(decoded_obj, list):
        logger.info(
            "[spapi_reports] document %s JSON top-level list len=%s",
            document_id,
            len(decoded_obj),
        )
        if decoded_obj:
            sample = decoded_obj[0]
            logger.info(
                "[spapi_reports] document %s JSON first element type=%s keys=%s",
                document_id,
                type(sample).__name__,
                list(sample.keys())[:20] if isinstance(sample, dict) else None,
            )

    if decoded_obj is None and decoded_text is not None:
        preview = "\n".join(decoded_text.splitlines()[:10])
        logger.info(
            "[spapi_reports] document %s text preview (first 10 lines):\n%s",
            document_id,
            preview,
        )

    # Return dict/list for JSON, else raw bytes for TSV/CSV
    final_content = decoded_obj if decoded_obj is not None else content
    return final_content, expiration_info

