import argparse
import gzip
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple

import requests

from services.spapi_reports import (
    REPORTS_API_HOST,
    auth_client,
    poll_vendor_report,
    request_vendor_report,
)

# Vendor Retail Analytics reports always reference PST (no DST).
PST = timezone(timedelta(hours=-8), name="PST")
DEFAULT_MARKETPLACE = "A2VIGQ35RCS4UG"
EXPORT_DIR = Path("exports") / "vendor_rt_inventory"
LOGGER = logging.getLogger("fetch_vendor_rt_inventory")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch GET_VENDOR_REAL_TIME_INVENTORY_REPORT and save raw/decompressed copies."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window in days (1-7). Default: 7",
    )
    parser.add_argument(
        "--start",
        type=str,
        help="Override start time (ISO8601, PST offset -08:00). Requires --end.",
    )
    parser.add_argument(
        "--end",
        type=str,
        help="Override end time (ISO8601, PST offset -08:00). Requires --start.",
    )
    parser.add_argument(
        "--marketplace",
        type=str,
        default=DEFAULT_MARKETPLACE,
        help="Marketplace ID (default: A2VIGQ35RCS4UG)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1200,
        help="Max seconds to wait for report completion (default: 1200)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="Seconds between polling attempts (default: 15)",
    )
    parser.add_argument(
        "--no-decompress",
        action="store_true",
        help="Skip writing decompressed JSON even if gzip.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress INFO logs; only warnings/errors.",
    )
    return parser.parse_args()


def parse_iso_pst(value: str) -> datetime:
    if not value:
        raise ValueError("Empty timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PST)
    return parsed.astimezone(PST)


def compute_window_from_args(args: argparse.Namespace) -> Tuple[datetime, datetime]:
    if args.start or args.end:
        if not (args.start and args.end):
            raise ValueError("--start and --end must be provided together")
        start = parse_iso_pst(args.start)
        end = parse_iso_pst(args.end)
    else:
        start, end = compute_default_window(args.days)
    if end <= start:
        raise ValueError("end must be later than start")
    span = end - start
    if span > timedelta(days=7):
        raise ValueError("Window cannot exceed 7 days")
    now_limit = datetime.now(PST)
    if now_limit - start > timedelta(days=30):
        raise ValueError("Start time must be within 30 days")
    return start, end


def compute_default_window(days: int) -> Tuple[datetime, datetime]:
    if days < 1 or days > 7:
        raise ValueError("days must be between 1 and 7")
    now_pst = datetime.now(PST)
    prev_hour = (now_pst - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    start = prev_hour - timedelta(days=days)
    return start, prev_hour


def fetch_document_raw(document_id: str) -> Tuple[bytes, str]:
    access_token = auth_client.get_lwa_access_token()
    meta_url = f"{REPORTS_API_HOST}/reports/2021-06-30/documents/{document_id}"
    headers = {
        "x-amz-access-token": access_token,
        "accept": "application/json",
    }
    meta_resp = requests.get(meta_url, headers=headers, timeout=30)
    if meta_resp.status_code >= 300:
        snippet = meta_resp.text[:500]
        LOGGER.error(
            "getReportDocument failed %s: %s",
            meta_resp.status_code,
            snippet,
        )
        meta_resp.raise_for_status()
    meta = meta_resp.json()
    LOGGER.info(
        "Document meta: id=%s compression=%s expires=%s",
        document_id,
        meta.get("compressionAlgorithm"),
        meta.get("expiresAt") or meta.get("expirationTime"),
    )
    download_url = meta.get("url")
    if not download_url:
        raise RuntimeError(f"Missing download URL for document {document_id}")
    compression = (meta.get("compressionAlgorithm") or "").upper()
    expires = meta.get("expiresAt") or meta.get("expirationTime")
    if expires:
        LOGGER.info("Document %s expires at %s", document_id, expires)
    doc_resp = requests.get(download_url, timeout=60)
    if doc_resp.status_code >= 300:
        snippet = doc_resp.text[:500]
        LOGGER.error("Document download failed %s: %s", doc_resp.status_code, snippet)
        doc_resp.raise_for_status()
    return doc_resp.content, compression


def ensure_export_dir() -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    return EXPORT_DIR


def save_outputs(
    raw_bytes: bytes,
    compression: str,
    base_name: str,
    *,
    decompress: bool,
) -> Tuple[Path, Path]:
    export_dir = ensure_export_dir()
    raw_ext = ".json.gz" if compression == "GZIP" else ".json"
    raw_path = export_dir / f"{base_name}{raw_ext}"
    raw_path.write_bytes(raw_bytes)
    LOGGER.info("Saved raw document to %s", raw_path)
    decompressed_path = None
    if compression == "GZIP" and decompress:
        try:
            decompressed = gzip.decompress(raw_bytes)
        except Exception as exc:
            LOGGER.error("Failed to decompress GZIP payload: %s", exc)
            return raw_path, decompressed_path
        try:
            json.loads(decompressed.decode("utf-8-sig"))
        except Exception:
            LOGGER.warning("Decompressed payload is not valid JSON text; saving bytes anyway")
        decompressed_path = export_dir / f"{base_name}.json"
        decompressed_path.write_bytes(decompressed)
        LOGGER.info("Saved decompressed JSON to %s", decompressed_path)
    return raw_path, decompressed_path


def build_base_name(start: datetime, end: datetime, report_id: str) -> str:
    fmt = "%Y%m%dT%H%M%S"
    return f"vendor_rt_inventory_{start.strftime(fmt)}_{end.strftime(fmt)}_{report_id}"


def run(args: argparse.Namespace) -> None:
    LOGGER.info(
        "Starting vendor realtime inventory export (days=%s, marketplace=%s)",
        args.days,
        args.marketplace,
    )
    start_pst, end_pst = compute_window_from_args(args)
    LOGGER.info("Requesting window %s to %s (PST)", start_pst.isoformat(), end_pst.isoformat())
    report_id = request_vendor_report(
        report_type="GET_VENDOR_REAL_TIME_INVENTORY_REPORT",
        params={"marketplaceIds": [args.marketplace]},
        data_start=start_pst,
        data_end=end_pst,
        selling_program="RETAIL",
    )
    LOGGER.info("Created report %s", report_id)
    report_meta = poll_vendor_report(
        report_id,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
    )
    document_id = report_meta.get("reportDocumentId")
    status = report_meta.get("processingStatus")
    if status != "DONE" or not document_id:
        raise RuntimeError(f"Report {report_id} did not complete successfully: {report_meta}")
    LOGGER.info("Report %s DONE with document %s", report_id, document_id)
    raw_bytes, compression = fetch_document_raw(document_id)
    LOGGER.info("Downloaded document (%s bytes, compression=%s)", len(raw_bytes), compression or "NONE")
    base_name = build_base_name(start_pst, end_pst, report_id)
    raw_path, decompressed_path = save_outputs(
        raw_bytes,
        compression,
        base_name,
        decompress=not args.no_decompress,
    )
    if compression == "GZIP" and args.no_decompress:
        LOGGER.info("Decompression disabled by flag; only raw archive saved.")
    LOGGER.info("Export complete.")
    print(f"reportId: {report_id}")
    print(f"reportDocumentId: {document_id}")
    print(f"raw_path: {raw_path}")
    if decompressed_path:
        print(f"json_path: {decompressed_path}")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    try:
        run(args)
    except TimeoutError as exc:
        LOGGER.error("Timed out waiting for report: %s", exc)
        return 2
    except Exception as exc:
        LOGGER.error("Fetch failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
