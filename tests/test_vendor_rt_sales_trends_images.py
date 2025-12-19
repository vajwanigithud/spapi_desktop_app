"""Test that Sales Trends returns imageUrl when catalog images exist."""
import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services import vendor_realtime_sales as vendor_rt


def _prepare_test_db(tmp_path) -> Path:
    """Create a test database with catalog and sales data."""
    db_path = tmp_path / "trends_image_test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Create tables
    conn.execute("""
        CREATE TABLE spapi_catalog (
            asin TEXT PRIMARY KEY,
            title TEXT,
            image TEXT,
            payload TEXT,
            barcode TEXT,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.execute("""
        CREATE TABLE vendor_realtime_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL,
            hour_start_utc TEXT NOT NULL,
            hour_end_utc TEXT NOT NULL,
            ordered_units INTEGER NOT NULL,
            ordered_revenue REAL NOT NULL,
            marketplace_id TEXT NOT NULL,
            currency_code TEXT NOT NULL,
            ingested_at_utc TEXT NOT NULL
        )
    """)
    
    conn.execute("""
        CREATE TABLE vendor_rt_audit_hours (
            marketplace_id TEXT NOT NULL,
            hour_start_utc TEXT NOT NULL,
            hour_end_utc TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            PRIMARY KEY (marketplace_id, hour_start_utc)
        )
    """)
    
    conn.execute("""
        CREATE TABLE vendor_rt_sales_state (
            marketplace_id TEXT PRIMARY KEY,
            last_ingested_end_utc TEXT,
            last_daily_audit_utc TEXT,
            last_weekly_audit_utc TEXT
        )
    """)
    
    # Insert test data
    test_asin = "B001TEST123"
    test_image_url = "https://example.com/test-image.jpg"
    
    conn.execute(
        "INSERT INTO spapi_catalog (asin, title, image) VALUES (?, ?, ?)",
        (test_asin, "Test Product", test_image_url)
    )
    
    # Add sales data spanning 4 weeks
    now = datetime.now(timezone.utc)
    for week in range(4):
        for day in range(7):
            hour_start = now - timedelta(weeks=week, days=day, hours=12)
            hour_end = hour_start + timedelta(hours=1)
            conn.execute(
                """INSERT INTO vendor_realtime_sales 
                   (asin, hour_start_utc, hour_end_utc, ordered_units, ordered_revenue, 
                    marketplace_id, currency_code, ingested_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    test_asin,
                    hour_start.isoformat().replace("+00:00", "Z"),
                    hour_end.isoformat().replace("+00:00", "Z"),
                    10,
                    100.0,
                    "A2VIGQ35RCS4UG",
                    "AED",
                    now.isoformat().replace("+00:00", "Z"),
                )
            )
    
    conn.commit()
    conn.close()
    return db_path


def test_trends_returns_image_url(tmp_path, monkeypatch):
    """Verify that get_sales_trends_last_4_weeks returns imageUrl from spapi_catalog."""
    db_path = _prepare_test_db(tmp_path)
    
    @contextlib.contextmanager
    def _conn_ctx():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    # Patch the db connection
    import services.db
    monkeypatch.setattr(services.db, "get_db_connection", _conn_ctx)
    
    # Call the trends function
    with _conn_ctx() as conn:
        result = vendor_rt.get_sales_trends_last_4_weeks(
            conn,
            marketplace_id="A2VIGQ35RCS4UG",
            min_total_units=1,
        )
    
    # Verify structure
    assert "rows" in result
    assert len(result["rows"]) > 0
    
    # Verify image data is present
    first_row = result["rows"][0]
    assert "asin" in first_row
    assert "imageUrl" in first_row, "imageUrl field missing from trend row"
    assert first_row["imageUrl"] == "https://example.com/test-image.jpg", \
        f"Expected image URL, got: {first_row.get('imageUrl')}"
    
    print("✅ Trends image test passed: imageUrl correctly populated from spapi_catalog.image")
