import argparse
import logging
import sys
from pathlib import Path
from typing import Tuple

# Ensure repo root is on PYTHONPATH so `import services.*` works when running from /scripts
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.vendor_rt_inventory_state import (
    DEFAULT_CATALOG_DB_PATH,
    ensure_vendor_rt_inventory_state_table,
)
from services.vendor_rt_inventory_sync import sync_vendor_rt_inventory

LOGGER = logging.getLogger("apply_vendor_rt_inventory_incremental")


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


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    db_path = Path(args.db)
    result = sync_vendor_rt_inventory(
        args.marketplace,
        db_path=db_path,
        hours=args.hours,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        include_items=False,
    )
    if result["status"] == "up_to_date":
        print("Checkpoint up-to-date; skipping report request.")
        return 0

    row_count = result.get("row_count") or 0
    distinct_asins = result.get("asin_count")
    min_end = result.get("min_end")
    max_end = result.get("max_end")
    print(
        f"Fetched rows: {row_count} | DISTINCT ASINs: {distinct_asins} | endTime range: {min_end} -> {max_end}"
    )
    stats = result.get("stats") or {}
    LOGGER.info("Incremental apply stats: %s", stats)

    asin_count, sellable_total = fetch_state_totals(db_path)
    print(f"Incremental apply stats: {stats}")
    print(f"State ASINs: {asin_count}, State Sellable: {sellable_total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
