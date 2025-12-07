# Forecast cleanup utility (manual use only)
# Drops forecast-only tables from catalog.db when run explicitly.
# Tables targeted: vendor_forecast, vendor_sales_history, vendor_rt_inventory, forecast_blacklist, report_jobs.
# Forecast data files noted: forecast_catalog.db, forecast_sync_state.json (optionally archived).

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from services.db import CATALOG_DB_PATH


TABLES = [
    "vendor_forecast",
    "vendor_sales_history",
    "vendor_rt_inventory",
    "forecast_blacklist",
    "report_jobs",
]

FORECAST_FILES = [
    "forecast_catalog.db",
    "forecast_sync_state.json",
]


def backup_catalog(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_before_forecast_cleanup_{timestamp}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def drop_forecast_tables(db_path: Path) -> dict:
    results: dict[str, str] = {}
    with sqlite3.connect(db_path) as conn:
        conn.isolation_level = None  # manual transactions
        conn.execute("BEGIN")
        try:
            for table in TABLES:
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                    results[table] = "dropped_or_absent"
                except Exception as exc:  # pragma: no cover - best-effort logging
                    results[table] = f"failed: {exc}"
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return results


def archive_files(base_dir: Path) -> list[str]:
    archive_dir = base_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    for name in FORECAST_FILES:
        src = base_dir / name
        if not src.exists():
            continue
        dest = archive_dir / name
        # Avoid overwriting existing archive
        if dest.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = archive_dir / f"{src.stem}_{timestamp}{src.suffix}"
        shutil.move(src, dest)
        archived.append(str(dest))
    return archived


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drop forecast-related tables from catalog.db (manual maintenance).",
    )
    parser.add_argument(
        "--archive-files",
        action="store_true",
        help="Move forecast_catalog.db and forecast_sync_state.json into archive/ (non-destructive).",
    )
    args = parser.parse_args()

    catalog_path = Path(CATALOG_DB_PATH)
    if not catalog_path.exists():
        print(f"[ERROR] catalog.db not found at {catalog_path}")
        return

    print("=== Forecast Cleanup (manual) ===")
    print("This will DROP forecast-related tables from catalog.db:")
    for t in TABLES:
        print(f"  - {t}")
    print("A backup will be created before any changes.")
    input("Press ENTER to continue, or Ctrl+C to abort...")

    backup_path = backup_catalog(catalog_path)
    print(f"[INFO] Backup created at: {backup_path}")

    results = drop_forecast_tables(catalog_path)
    print("[INFO] Drop results:")
    for table, status in results.items():
        print(f"  {table}: {status}")

    if args.archive_files:
        archived = archive_files(catalog_path.parent)
        if archived:
            print("[INFO] Archived forecast-specific files:")
            for path in archived:
                print(f"  - {path}")
        else:
            print("[INFO] No forecast-specific files found to archive.")
    else:
        for name in FORECAST_FILES:
            path = catalog_path.parent / name
            if path.exists():
                print(f"[NOTE] {name} exists at {path}. You can manually archive or delete this file if no longer needed.")

    print("[DONE] Forecast cleanup complete.")


if __name__ == "__main__":
    main()
