import os
import sqlite3

DB_PATH = "spapi_desktop.sqlite"

def init_database():
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
        conn.close()
