import sqlite3

con = sqlite3.connect("catalog.db")
con.row_factory = sqlite3.Row
cur = con.cursor()

rows = cur.execute(
    "select name from sqlite_master where type='table' and name like '%lock%'"
).fetchall()

print("Lock-ish tables:", [r["name"] for r in rows])

candidates = [
    "app_worker_locks",
    "worker_locks",
    "app_locks",
    "locks",
    "app_kv_store",
    "app_kv"
]

for tbl in candidates:
    try:
        info = cur.execute(f"pragma table_info({tbl})").fetchall()
        if info:
            print("\nTable:", tbl)
            print("Cols:", [r[1] for r in info])
    except Exception:
        pass

con.close()
