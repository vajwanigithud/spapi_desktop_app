from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from services.catalog_service import delete_catalog_asin_data, init_catalog_db


ASIN = "B012345678"


def _seed_catalog_owned_rows(db_path):
    init_catalog_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO spapi_catalog (asin, title, image, payload, barcode) VALUES (?, ?, ?, ?, ?)",
            (ASIN, "Bad title", "bad.jpg", "{}", "1234567890123"),
        )
        conn.execute("INSERT INTO spapi_catalog_meta (asin, sku) VALUES (?, ?)", (ASIN, "SKU-BAD"))
        conn.execute(
            "INSERT INTO catalog_fetch_attempts (asin, attempts, terminal_code) VALUES (?, ?, ?)",
            (ASIN, 5, "NOT_FOUND"),
        )
        conn.execute(
            "INSERT INTO catalog_asin_sources (asin, source, first_seen_at) VALUES (?, ?, ?)",
            (ASIN, "vendor_po", "2025-01-01T00:00:00+00:00"),
        )
        conn.execute("CREATE TABLE IF NOT EXISTS vendor_po_lines (po_number TEXT, asin TEXT)")
        conn.execute("INSERT INTO vendor_po_lines (po_number, asin) VALUES (?, ?)", ("PO-KEEP", ASIN))
        conn.commit()


def test_delete_catalog_asin_data_is_transactional_and_scoped(tmp_path):
    db_path = tmp_path / "catalog-delete.db"
    _seed_catalog_owned_rows(db_path)

    result = delete_catalog_asin_data(ASIN.lower(), db_path=db_path)

    assert result == {
        "asin": ASIN,
        "deleted": True,
        "rows_removed_by_table": {
            "spapi_catalog": 1,
            "spapi_catalog_meta": 1,
            "catalog_fetch_attempts": 1,
            "catalog_asin_sources": 1,
        },
    }
    with sqlite3.connect(db_path) as conn:
        for table in result["rows_removed_by_table"]:
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE asin = ?", (ASIN,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM vendor_po_lines WHERE asin = ?", (ASIN,)).fetchone()[0] == 1


def test_delete_missing_catalog_asin_is_safe(tmp_path):
    db_path = tmp_path / "catalog-delete-missing.db"
    init_catalog_db(db_path)
    result = delete_catalog_asin_data(ASIN, db_path=db_path)
    assert result["deleted"] is True
    assert result["rows_removed_by_table"] == {
        "spapi_catalog": 0,
        "spapi_catalog_meta": 0,
        "catalog_fetch_attempts": 0,
        "catalog_asin_sources": 0,
    }


def test_catalog_delete_route_tombstones_and_manual_add_restores(monkeypatch, tmp_path):
    monkeypatch.setenv("LWA_CLIENT_ID", "dummy")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "dummy")
    monkeypatch.setenv("LWA_REFRESH_TOKEN", "dummy")
    import main

    db_path = tmp_path / "catalog-route-delete.db"
    exclusions_path = tmp_path / "catalog-exclusions.json"
    _seed_catalog_owned_rows(db_path)
    monkeypatch.setattr(main, "CATALOG_DB_PATH", db_path)
    monkeypatch.setattr(main, "CATALOG_FETCHER_EXCLUSIONS_PATH", exclusions_path)
    monkeypatch.setattr(main, "start_vendor_rt_sales_startup_backfill_thread", lambda: None)
    monkeypatch.setattr(main, "start_vendor_rt_sales_auto_sync", lambda: None)
    monkeypatch.setattr(main, "start_df_payments_incremental_scheduler", lambda: None)

    with TestClient(main.app) as client:
        deleted = client.delete(f"/api/catalog/asins/{ASIN.lower()}")
        assert deleted.status_code == 200
        payload = deleted.json()
        assert payload["asin"] == ASIN
        assert payload["deleted"] is True
        assert payload["excluded"] is True
        assert json.loads(exclusions_path.read_text(encoding="utf-8")) == [ASIN]

        added = client.post("/api/catalog/add-asin", json={"asin": ASIN.lower()})
        assert added.status_code == 200
        assert ASIN not in main.load_catalog_fetcher_exclusions()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM spapi_catalog_meta WHERE asin = ?", (ASIN,)).fetchone()[0] == 1


def test_catalog_delete_rejects_active_fetch(monkeypatch, tmp_path):
    monkeypatch.setenv("LWA_CLIENT_ID", "dummy")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "dummy")
    monkeypatch.setenv("LWA_REFRESH_TOKEN", "dummy")
    import main

    db_path = tmp_path / "catalog-active.db"
    init_catalog_db(db_path)
    monkeypatch.setattr(main, "CATALOG_DB_PATH", db_path)
    monkeypatch.setattr(main, "CATALOG_FETCHER_EXCLUSIONS_PATH", tmp_path / "active-exclusions.json")
    monkeypatch.setattr(main, "start_vendor_rt_sales_startup_backfill_thread", lambda: None)
    monkeypatch.setattr(main, "start_vendor_rt_sales_auto_sync", lambda: None)
    monkeypatch.setattr(main, "start_df_payments_incremental_scheduler", lambda: None)
    main._catalog_fetch_inflight.add(ASIN)
    try:
        with TestClient(main.app) as client:
            response = client.delete(f"/api/catalog/asins/{ASIN}")
        assert response.status_code == 409
        assert "fetch is active" in response.json()["detail"]
    finally:
        main._catalog_fetch_inflight.discard(ASIN)


def test_catalog_list_does_not_reseed_tombstoned_historical_po(monkeypatch):
    monkeypatch.setenv("LWA_CLIENT_ID", "dummy")
    monkeypatch.setenv("LWA_CLIENT_SECRET", "dummy")
    monkeypatch.setenv("LWA_REFRESH_TOKEN", "dummy")
    import main

    seeded = []
    sourced = []
    monkeypatch.setattr(main, "extract_asins_from_pos", lambda: ([ASIN], {ASIN: "SKU-OLD"}))
    monkeypatch.setattr(main, "load_catalog_fetcher_exclusions", lambda: {ASIN})
    monkeypatch.setattr(main, "seed_catalog_universe", lambda values: seeded.extend(values) or 0)
    monkeypatch.setattr(main, "record_catalog_asin_sources", lambda values, source: sourced.extend(values))
    monkeypatch.setattr(main, "list_universe_asins", lambda: [])
    monkeypatch.setattr(main, "spapi_catalog_status", lambda: {})
    monkeypatch.setattr(main, "get_catalog_fetch_attempts_map", lambda _: {})
    monkeypatch.setattr(main, "get_catalog_asin_sources_map", lambda _: {})
    monkeypatch.setattr(main, "_load_inventory_asin_set", lambda: set())
    monkeypatch.setattr(main, "_load_realtime_sales_asin_set", lambda: set())
    monkeypatch.setattr(main, "start_vendor_rt_sales_startup_backfill_thread", lambda: None)
    monkeypatch.setattr(main, "start_vendor_rt_sales_auto_sync", lambda: None)
    monkeypatch.setattr(main, "start_df_payments_incremental_scheduler", lambda: None)

    with TestClient(main.app) as client:
        response = client.get("/api/catalog/asins")

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert seeded == []
    assert sourced == []
