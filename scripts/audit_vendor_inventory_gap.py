#!/usr/bin/env python3
"""
Quick audit comparing the local realtime inventory snapshot JSON with a Vendor Central CSV export.

Usage:
    python scripts/audit_vendor_inventory_gap.py path/to/vendor_central.csv

Notes:
- Vendor Central exports often start with metadata rows (e.g. Program=[Retail]...).
  We scan the file and locate the *real* header row where one cell is exactly "ASIN".
- Units are read from the best-matching sellable/on-hand units column.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ROOT / "vendor_realtime_inventory_snapshot.json"

UNIT_HEADER_PREFERENCE = [
    "sellable on hand units",
    "sellable on hand unit",
    "sellable units",
    "sellable",
    "available units",
    "available",
    "on hand units",
    "on hand",
    "total available",
    "inventory units",
    "inventory",
    "units",
]


def normalize_asin(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().upper()
    return s or None


def parse_int(value: object) -> int:
    if value is None:
        return 0
    s = str(value).strip()
    if not s:
        return 0
    s = s.replace(",", "")
    try:
        return int(float(s))
    except Exception:
        return 0


def _cell_is_exact_asin(cell: object) -> bool:
    if cell is None:
        return False
    s = str(cell).strip().strip('"').strip("'").lower()
    return s == "asin"


def find_real_header(path: Path, max_scan_rows: int = 200) -> Tuple[List[str], int]:
    """
    Returns (headers, header_row_index).
    We only accept a header row where a cell is exactly 'ASIN' (case-insensitive).
    This avoids matching metadata like 'View By=[ASIN]'.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for idx, row in enumerate(reader):
            if idx >= max_scan_rows:
                break
            if not row:
                continue

            # Must be a multi-column row (real headers typically are)
            if len(row) < 2:
                continue

            if any(_cell_is_exact_asin(c) for c in row):
                headers = [str(c).strip() for c in row]
                return headers, idx

    return [], -1


def detect_columns(headers: List[str]) -> Tuple[Optional[str], Optional[str]]:
    asin_col: Optional[str] = None
    unit_col: Optional[str] = None

    lowered = [h.strip().lower() for h in headers]

    # Find ASIN column (exact match preferred; otherwise contains 'asin')
    for i, h in enumerate(lowered):
        if h == "asin":
            asin_col = headers[i]
            break
    if asin_col is None:
        for i, h in enumerate(lowered):
            if "asin" in h:
                asin_col = headers[i]
                break

    if asin_col is None:
        return None, None

    header_map = {h.strip().lower(): h for h in headers}

    # Preference list match
    for candidate in UNIT_HEADER_PREFERENCE:
        if candidate in header_map:
            unit_col = header_map[candidate]
            break

    # Smarter fallback: "sellable" + ("unit" or "inventory" or "on hand")
    if unit_col is None:
        for h_lc, original in header_map.items():
            if "sellable" in h_lc and ("unit" in h_lc or "inventory" in h_lc or "on hand" in h_lc):
                unit_col = original
                break

    return asin_col, unit_col


def read_snapshot() -> Tuple[Dict[str, int], int, int]:
    if not SNAPSHOT_PATH.exists():
        print(f"[ERROR] Snapshot file not found: {SNAPSHOT_PATH}")
        sys.exit(1)

    data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    items = data.get("items") or []

    result: Dict[str, int] = {}
    total_units = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        asin = normalize_asin(item.get("asin"))
        if not asin:
            continue
        sellable = parse_int(item.get("sellable"))
        result[asin] = sellable
        total_units += sellable

    return result, len(result), total_units


def read_vendor_csv(path: Path) -> Tuple[Dict[str, int], int, int, str, str]:
    if not path.exists():
        print(f"[ERROR] CSV file not found: {path}")
        sys.exit(1)

    headers, header_idx = find_real_header(path)
    if not headers or header_idx < 0:
        print("[ERROR] Could not find a real header row with a column exactly named 'ASIN'.")
        sys.exit(1)

    asin_col, unit_col = detect_columns(headers)
    if not asin_col:
        print("[ERROR] Unable to find an ASIN column. Headers:", headers)
        sys.exit(1)
    if not unit_col:
        print("[ERROR] Unable to find a sellable/on-hand UNITS column. Headers:", headers)
        print("Tip: Your file usually has something like 'Sellable On Hand Units'.")
        sys.exit(1)

    counts: Dict[str, int] = {}
    total_units = 0

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)

        # Skip up to and including the header row
        for _ in range(header_idx + 1):
            next(reader, None)

        dict_reader = csv.DictReader(fh, fieldnames=headers)
        for row in dict_reader:
            asin = normalize_asin(row.get(asin_col))
            if not asin:
                continue
            units = parse_int(row.get(unit_col))
            total_units += units
            counts[asin] = units

    return counts, len(counts), total_units, asin_col, unit_col


def top_entries(counter: Dict[str, int], limit: int = 50) -> List[Tuple[str, int]]:
    return Counter(counter).most_common(limit)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    csv_path = Path(sys.argv[1]).resolve()

    snapshot_map, snapshot_count, snapshot_units = read_snapshot()
    csv_map, csv_count, csv_units, asin_col, unit_col = read_vendor_csv(csv_path)

    missing_in_snapshot = {asin: units for asin, units in csv_map.items() if asin not in snapshot_map and units > 0}
    missing_in_csv = {asin: units for asin, units in snapshot_map.items() if asin not in csv_map}

    print("=== Inventory Snapshot vs Vendor Central CSV ===")
    print(f"Snapshot file: {SNAPSHOT_PATH}")
    print(f"Vendor CSV:    {csv_path}")
    print(f"Detected CSV columns: ASIN='{asin_col}', Units='{unit_col}'")
    print()
    print(f"Snapshot unique ASINs : {snapshot_count}")
    print(f"CSV unique ASINs      : {csv_count}")
    print(f"Snapshot sellable sum : {snapshot_units}")
    print(f"CSV sellable sum      : {csv_units}")
    print()

    print("Top ASINs present in CSV but missing in snapshot (units > 0):")
    if missing_in_snapshot:
        for asin, units in top_entries(missing_in_snapshot, limit=50):
            print(f"  {asin:<15} {units}")
    else:
        print("  None")

    print()
    print("Top ASINs present in snapshot but missing in CSV (by sellable):")
    if missing_in_csv:
        for asin, units in top_entries(missing_in_csv, limit=50):
            print(f"  {asin:<15} {units}")
    else:
        print("  None")


if __name__ == "__main__":
    main()
