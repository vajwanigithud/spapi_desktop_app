import sqlite3

db = "catalog.db"
con = sqlite3.connect(db)
cur = con.cursor()

print("DB:", db)
print("\nTables:")
for (n,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(" -", n)

print("\nSchema vendor_realtime_sales:")
try:
    for row in cur.execute("PRAGMA table_info(vendor_realtime_sales)"):
        print(row)
except Exception as e:
    print("ERROR:", e)

con.close()
