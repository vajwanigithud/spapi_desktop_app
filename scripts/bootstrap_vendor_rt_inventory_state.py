import argparse
import gzip
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure repo root is on sys.path so "services.*" imports work when running scripts directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.vendor_rt_inventory_state import (  # noqa: E402
    DEFAULT_CATALOG_DB_PATH,
    bootstrap_state_from_rows,
    get_state_snapshot,
)

LOGGER = logging.getLogger("bootstrap_vendor_rt_inventory_state")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap vendor_rt_inventory_state from a 7-day report JSON."
    )
    parser.add_argument("--input", required=True, help="Path to report JSON (.json or .json.gz)")
    parser.add_argument(
        "--marketplace",
        default="A2VIGQ35RCS4UG",
        help="Marketplace ID (default: A2VIGQ35RCS4UG)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_CATALOG_DB_PATH),
        help="Path to catalog.db (default: repo catalog.db)",
    )
    return parser.parse_args()


def load_rows(inp: Path) -> List[Dict[str, Any]]:
    if not inp.exists():
        raise FileNotFoundError(f"Input file not found: {inp}")

    LOGGER.info("Loading report %s", inp)
    raw_bytes = inp.read_bytes()

    if inp.suffix.lower() == ".gz":
        LOGGER.info("Detected gzipped file; decompressing")
        raw_bytes = gzip.decompress(raw_bytes)

    # Decode safely (utf-8-sig handles BOM)
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except Exception:
        # Fallback: json.loads supports bytes in some cases, but keep this as a last resort
        payload = json.loads(raw_bytes)

    rows: Any = None

    if isinstance(payload, list):
        rows = payload

    elif isinstance(payload, dict):
        # Common SP-API wrapper shapes
        container = payload.get("reportData") or payload.get("data")

        if isinstance(container, list):
            rows = container
        elif isinstance(container, dict):
            candidate = container.get("items")
            if isinstance(candidate, list):
                rows = candidate

        # Fallback
        if rows is None:
            candidate = payload.get("items")
            if isinstance(candidate, list):
                rows = candidate

    if not isinstance(rows, list):
        raise ValueError("Could not extract rows from payload (expected list)")

    # Ensure list elements are dicts (filter anything weird)
    clean_rows: List[Dict[str, Any]] = [r for r in rows if isinstance(r, dict)]

    LOGGER.info("Loaded %s rows from payload (%s valid dict rows)", len(rows), len(clean_rows))
    return clean_rows


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    db_path = Path(args.db).resolve()
    rows = load_rows(Path(args.input))

    stats = bootstrap_state_from_rows(rows, marketplace_id=args.marketplace, db_path=db_path)
    LOGGER.info("Bootstrap stats: %s", stats)

    snapshot = get_state_snapshot(db_path=db_path)
    total_sellable = sum(int(row.get("sellable") or 0) for row in snapshot)

    print(f"Bootstrap complete. Stats: {stats}. Total sellable: {total_sellable}")
    print("Top 20 ASINs (sellable, last_end_time):")
    for row in snapshot[:20]:
        asin = row.get("asin")
        sellable = row.get("sellable")
        end_time = row.get("last_end_time")
        print(f"  {asin}: sellable={sellable}, last_end_time={end_time}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
