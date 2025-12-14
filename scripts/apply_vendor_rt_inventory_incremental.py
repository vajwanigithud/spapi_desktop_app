import argparse
import gzip
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

# Ensure repo root is on PYTHONPATH so `import services.*` works when running from /scripts
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
    ensure_vendor_rt_inventory_state_table,
    get_checkpoint,
    parse_end_time,
    set_checkpoint,
)

LOGGER = logging.getLogger("apply_vendor_rt_inventory_incremental")

# US Pacific time with DST handling
PST = ZoneInfo("America/Los_Angeles")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply incremental vendor RT inventory rows to vendor_rt_inventory_state."
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=2,
        help="Number of trailing hours to request (1-24). Default: 2",
    )
    parser.add_argument(
        "--marketplace",
        default="A2VIGQ35RCS4UG",
        help="Marketplace ID (default: A2VIGQ35RCS4UG)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_CATALOG_DB_PATH),
        help="Path to catalog.db",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1200,
        help="Seconds to wait for report completion (default: 1200)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="Seconds between polling attempts (default: 15)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress INFO logs; only warnings/errors.",
    )
    return parser.parse_args()


def compute_window(hours: int) -> Tuple[datetime, datetime]:
    """
    Returns (start, end) in Pacific time (America/Los_Angeles),
    where end is the previous full hour boundary (minute=0).
    """
    if hours < 1:
        raise ValueError("hours must be >= 1")
    if hours > 24:
        raise ValueError("hours must be <= 24")
    now_pst = datetime.now(PST)
    end = (now_pst - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    return start, end


def request_report(
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
        except Exception as exc:
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


def sqlite_connection(db_path: Path):
    # Reuse private helper in vendor_rt_inventory_state
    from services.vendor_rt_inventory_state import _connection

    return _connection(db_path)


def fetch_state_totals(db_path: Path) -> Tuple[int, int]:
    ensure_vendor_rt_inventory_state_table(db_path)
    with sqlite_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS asin_count, "
            "COALESCE(SUM(sellable), 0) AS total_sellable "
            "FROM vendor_rt_inventory_state"
        ).fetchone()
        return int(row["asin_count"]), int(row["total_sellable"])


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


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    db_path = Path(args.db)
    checkpoint_iso = get_checkpoint(args.marketplace, db_path=db_path)

    use_checkpoint = False
    _prev_hour_start, prev_hour_end = compute_window(1)

    # Decide request window
    if checkpoint_iso:
        try:
            checkpoint_dt_utc = _iso_to_datetime(checkpoint_iso)
            checkpoint_dt_pst = checkpoint_dt_utc.astimezone(PST)
            if checkpoint_dt_pst < prev_hour_end:
                start_pst = checkpoint_dt_pst
                end_pst = prev_hour_end
                use_checkpoint = True
            else:
                LOGGER.info(
                    "Checkpoint %s is up to date with prev hour %s; no new window needed",
                    checkpoint_iso,
                    prev_hour_end.isoformat(),
                )
                print("Checkpoint up-to-date; skipping report request.")
                return 0
        except Exception as exc:
            LOGGER.warning(
                "Invalid checkpoint %s (%s); falling back to --hours",
                checkpoint_iso,
                exc,
            )

    if use_checkpoint:
        # Avoid refetching boundary rows at exactly the checkpoint time
        start_pst = start_pst + timedelta(seconds=1)
    else:
        start_pst, end_pst = compute_window(args.hours)

    LOGGER.info("Requesting window %s to %s (PST)", start_pst.isoformat(), end_pst.isoformat())

    rows = request_report(start_pst, end_pst, args.marketplace, args.timeout, args.poll_interval)
    LOGGER.info("Fetched %s rows from report", len(rows))

    # Diagnostics from report content
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
    print(
        f"Fetched rows: {len(rows)} | DISTINCT ASINs: {len(asin_set)} | "
        f"endTime range: {min_end} -> {max_end_rows}"
    )

    stats = apply_incremental_rows(
        rows,
        marketplace_id=args.marketplace,
        db_path=db_path,
    )
    LOGGER.info("Incremental apply stats: %s", stats)

    max_end_stats = stats.get("max_end_time") if isinstance(stats, dict) else None
    max_end = max_end_stats or max_end_rows

    # Update checkpoint monotonically
    if max_end:
        try:
            new_checkpoint_dt = _iso_to_datetime(max_end)
            existing_checkpoint_dt = _iso_to_datetime(checkpoint_iso) if checkpoint_iso else None
            if not existing_checkpoint_dt or new_checkpoint_dt > existing_checkpoint_dt:
                set_checkpoint(args.marketplace, max_end, db_path=db_path)
                LOGGER.info("Updated checkpoint for %s to %s", args.marketplace, max_end)
        except Exception as exc:
            LOGGER.warning("Failed to update checkpoint for %s: %s", args.marketplace, exc)

    asin_count, sellable_total = fetch_state_totals(db_path)
    print(f"Incremental apply stats: {stats}")
    print(f"State ASINs: {asin_count}, State Sellable: {sellable_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
