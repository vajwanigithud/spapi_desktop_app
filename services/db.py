import sqlite3
from pathlib import Path

CATALOG_DB_PATH = Path(__file__).resolve().parent.parent / "catalog.db"


def get_db_connection():
    conn = sqlite3.connect(CATALOG_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
