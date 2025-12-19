from services.db import get_db_connection

with get_db_connection() as conn:
    row = conn.execute("""
        SELECT asin,
               length(payload) as payload_len,
               substr(payload,1,200) as payload_head
        FROM spapi_catalog
        WHERE payload IS NOT NULL AND trim(payload) <> ''
        LIMIT 1
    """).fetchone()
    print("SAMPLE_PAYLOAD_ROW:", dict(row) if row else None)
