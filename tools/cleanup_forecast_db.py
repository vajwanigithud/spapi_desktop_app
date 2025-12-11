# NOTE: Forecast is currently disabled. This tool is kept only for manual maintenance of old data.
import os
import sys
import sqlite3
import shutil
import datetime
import argparse

# Forecast-only tables we want to drop from catalog.db
FORECAST_TABLES = [
    "vendor_forecast",
    "vendor_sales_history",
    "vendor_rt_inventory",
    "forecast_blacklist",
    "report_jobs",
]


def find_catalog_db() -> str:
    """
    Locate catalog.db starting from project root (parent of tools/).
    First tries <root>/catalog.db, then scans subdirectories.
    """
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidate = os.path.join(base_dir, "catalog.db")
    if os.path.exists(candidate):
        return candidate

    # Fallback: walk a few levels under base_dir to find catalog.db
    for root, dirs, files in os.walk(base_dir):
        if "catalog.db" in files:
            return os.path.join(root, "catalog.db")

    # Return default even if missing; caller will handle error
    return candidate


def backup_db(db_path: str) -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.backup_before_forecast_cleanup_{ts}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def drop_forecast_tables(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.isolation_level = None  # manual transaction control
    cur = conn.cursor()
    dropped = []
    try:
        cur.execute("BEGIN")
        for table in FORECAST_TABLES:
            try:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
                dropped.append(table)
            except sqlite3.Error as e:
                print(f"[WARN] Error dropping table {table}: {e}")
        cur.execute("COMMIT")
    except sqlite3.Error as e:
        print(f"[ERROR] Transaction failed: {e}")
        try:
            cur.execute("ROLLBACK")
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return dropped


def archive_files(base_dir: str):
    archive_dir = os.path.join(base_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    moved = []
    for name in ("forecast_catalog.db", "forecast_sync_state.json"):
        src = os.path.join(base_dir, name)
        if os.path.exists(src):
            dest = os.path.join(archive_dir, name)
            shutil.move(src, dest)
            moved.append(dest)
    return moved


def main():
    parser = argparse.ArgumentParser(
        description="Cleanup forecast tables from catalog.db (manual tool)."
    )
    parser.add_argument(
        "--archive-files",
        action="store_true",
        help="Move forecast_catalog.db and forecast_sync_state.json into ./archive/",
    )
    args = parser.parse_args()

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    db_path = find_catalog_db()

    print("This script will DROP the following forecast-related tables from catalog.db:")
    for t in FORECAST_TABLES:
        print(f"  - {t}")
    print()
    print(f"Detected catalog.db path: {db_path}")
    if not os.path.exists(db_path):
        print("[ERROR] catalog.db not found at detected path.")
        sys.exit(1)

    print("A timestamped backup will be created in the same directory before any changes.")
    print()
    input("Press ENTER to continue, or Ctrl+C to abort...")

    backup_path = backup_db(db_path)
    print(f"[INFO] Backup created at: {backup_path}")

    dropped = drop_forecast_tables(db_path)
    print()
    print("[INFO] DROP TABLE attempts complete.")
    print("Tables processed:")
    for t in FORECAST_TABLES:
        status = "dropped (if existed)" if t in dropped else "not processed"
        print(f"  - {t}: {status}")
    print()

    if args.archive_files:
        moved = archive_files(base_dir)
        if moved:
            print("[INFO] Archived files:")
            for p in moved:
                print(f"  - {p}")
        else:
            print("[INFO] No forecast files found to archive.")
    else:
        # Non-destructive: just tell the user if these files exist
        for name in ("forecast_catalog.db", "forecast_sync_state.json"):
            p = os.path.join(base_dir, name)
            if os.path.exists(p):
                print(f"[NOTE] {name} exists at {p}. You may archive or delete it manually.")

    print()
    print("[DONE] Forecast DB cleanup script finished.")
    print("If anything looks wrong, you can restore from the backup:")
    print(f"  {backup_path}")


if __name__ == "__main__":
    main()
