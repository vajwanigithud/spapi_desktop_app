import sqlite3

from services import vendor_realtime_sales as vendor_rt


def test_asin_summary_returns_every_product_not_only_first_fifty():
    """The ASIN grid must be able to reconcile exactly to the all-product headline."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE vendor_realtime_sales (
            asin TEXT NOT NULL,
            hour_start_utc TEXT NOT NULL,
            hour_end_utc TEXT NOT NULL,
            ordered_units INTEGER NOT NULL,
            ordered_revenue REAL NOT NULL,
            marketplace_id TEXT NOT NULL,
            currency_code TEXT NOT NULL
        );
        CREATE TABLE spapi_catalog (asin TEXT PRIMARY KEY, image TEXT);
        CREATE TABLE spapi_catalog_meta (asin TEXT, sku TEXT);
        """
    )

    for index in range(51):
        conn.execute(
            """
            INSERT INTO vendor_realtime_sales (
                asin, hour_start_utc, hour_end_utc, ordered_units,
                ordered_revenue, marketplace_id, currency_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"ASIN-{index:03d}",
                "2026-10-20T00:00:00+00:00",
                "2026-10-20T01:00:00+00:00",
                1,
                10.0 + index,
                "MKT",
                "AED",
            ),
        )

    rows = vendor_rt._get_realtime_sales_by_asin(
        conn,
        "2026-10-20T00:00:00+00:00",
        "2026-10-20T02:00:00+00:00",
        "MKT",
    )

    assert len(rows) == 51
    assert sum(row["units"] for row in rows) == 51
    assert sum(row["revenue"] for row in rows) == sum(10.0 + index for index in range(51))
