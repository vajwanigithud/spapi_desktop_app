from services.db import get_db_connection

with get_db_connection() as conn:
    print("DB_LIST:", conn.execute("PRAGMA database_list").fetchall())
    print("CATALOG_ROWS:", conn.execute("select count(*) from spapi_catalog").fetchone()[0])
    print("CATALOG_WITH_IMAGE:", conn.execute("select count(*) from spapi_catalog where image IS NOT NULL").fetchone()[0])
    print("SAMPLE_IMAGE:", conn.execute("select asin, substr(image,1,60) from spapi_catalog where image IS NOT NULL limit 1").fetchone())
