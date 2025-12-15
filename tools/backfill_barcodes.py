import json
import logging
from pathlib import Path

from main import harvest_barcodes_from_pos, normalize_pos_entries

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_barcodes")

ROOT = Path(__file__).resolve().parent.parent
VENDOR_POS_CACHE = ROOT / "vendor_pos_cache.json"


def backfill():
    if not VENDOR_POS_CACHE.exists():
        logger.error("vendor_pos_cache.json not found")
        return {"processed_pos": 0, "processed_lines": 0, "barcodes_set": 0, "barcodes_skipped_invalid": 0}

    try:
        data = json.loads(VENDOR_POS_CACHE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"Failed to read vendor_pos_cache.json: {exc}")
        return {"processed_pos": 0, "processed_lines": 0, "barcodes_set": 0, "barcodes_skipped_invalid": 0}

    pos_list = normalize_pos_entries(data)
    counts = harvest_barcodes_from_pos(pos_list, log_prefix="[BarcodeBackfill]")
    summary = {
        "processed_pos": len(pos_list),
        "processed_lines": counts.get("lines", 0),
        "barcodes_set": counts.get("set", 0),
        "barcodes_skipped_invalid": counts.get("invalid", 0),
    }
    logger.info(f"Backfill complete: {summary}")
    return summary


if __name__ == "__main__":
    backfill()
