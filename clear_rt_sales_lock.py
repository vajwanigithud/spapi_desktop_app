import sqlite3
from datetime import datetime, timezone

DB="catalog.db"
MP="A2VIGQ35RCS4UG"

def main():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 1) Show current lock row
    row = cur.execute(
        "select marketplace_id, owner, expires_at, created_at_utc, updated_at_utc "
        "from vendor_rt_sales_worker_lock where marketplace_id=?",
        (MP,),
    ).fetchone()
    print("NOW_UTC:", now)
    print("LOCK_BEFORE:", dict(row) if row else None)

    # 2) Delete the lock row ONLY if it is expired (safety)
    if row and row["expires_at"] and row["expires_at"] < now:
        cur.execute(
            "delete from vendor_rt_sales_worker_lock where marketplace_id=?",
            (MP,),
        )
        print("Deleted expired worker lock row.")
    else:
        print("Lock not expired (or missing); no deletion performed.")

    # 3) Clear any RT-sales autosync/repair pause keys (best-effort)
    # (We don't assume exact key names; we remove only pause-ish keys.)
    keys = [r["key"] for r in cur.execute("select key from app_kv_store").fetchall()]
    pause_keys = [k for k in keys if ("rt" in k.lower() and "sales" in k.lower() and ("pause" in k.lower() or "repair" in k.lower()))]
    for k in pause_keys:
        cur.execute("delete from app_kv_store where key=?", (k,))
    if pause_keys:
        print("Cleared app_kv_store keys:", pause_keys)
    else:
        print("No rt-sales pause/repair keys found in app_kv_store.")

    con.commit()

    # 4) Show lock row after
    row2 = cur.execute(
        "select marketplace_id, owner, expires_at, created_at_utc, updated_at_utc "
        "from vendor_rt_sales_worker_lock where marketplace_id=?",
        (MP,),
    ).fetchone()
    print("LOCK_AFTER:", dict(row2) if row2 else None)

    con.close()

if __name__ == "__main__":
    main()
