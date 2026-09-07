import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import BackgroundTasks
import main
from services import catalog_service as catalog

ASIN = "B0CX8VZX75"


def payload(kind="EAN", value="6976154505903"):
    return {"identifiers": [{"marketplaceId": "AE", "identifiers": [{"identifierType": kind, "identifier": value}]}], "summaries": [{"itemName": "Product"}]}


@pytest.mark.parametrize("kind", ["EAN", "GTIN", "UPC", "JAN"])
def test_identifier_types(kind):
    assert catalog.extract_catalog_barcode(payload(kind)) == "6976154505903"


@pytest.mark.parametrize("value", [None, {}, {"identifiers": None}, {"identifiers": [None, {}]}, payload("OTHER")])
def test_missing_identifiers(value):
    assert catalog.extract_catalog_barcode(value) is None


def test_priority_and_marketplace():
    data = payload("UPC", "00123")
    data["identifiers"][0]["identifiers"].append({"identifierType": "EAN", "identifier": "00456"})
    assert catalog.extract_catalog_barcode(data, "AE") == "00456"
    assert catalog.extract_catalog_barcode(data, "US") is None


def test_save_preserve_and_backfill(tmp_path):
    db = tmp_path / "catalog.db"
    catalog.upsert_spapi_catalog(ASIN, payload(), db)
    assert catalog.spapi_catalog_status(db)[ASIN]["barcode"] == "6976154505903"
    catalog.update_catalog_barcode(ASIN, "MANUAL", db)
    catalog.upsert_spapi_catalog(ASIN, payload(), db)
    assert catalog.spapi_catalog_status(db)[ASIN]["barcode"] == "MANUAL"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE spapi_catalog SET barcode = ' '")
    assert catalog.backfill_catalog_barcodes(db) == 1
    assert catalog.backfill_catalog_barcodes(db) == 0


@pytest.fixture
def state(monkeypatch):
    rows, attempts, excluded = {}, {}, set()
    monkeypatch.setattr(main, "spapi_catalog_status", lambda **_: rows)
    monkeypatch.setattr(main, "get_catalog_fetch_attempts_map", lambda *a, **k: attempts)
    monkeypatch.setattr(main, "load_catalog_fetcher_exclusions", lambda: excluded)
    monkeypatch.setattr(main, "list_universe_asins", lambda: [ASIN])
    monkeypatch.setattr(main, "_ensure_catalog_fetch_worker", lambda: None)
    monkeypatch.setattr(main, "_catalog_fetch_inflight", set())
    monkeypatch.setattr(main, "_catalog_fetch_queue", [])
    monkeypatch.setattr(main, "_catalog_fetch_options", {})
    monkeypatch.setattr(main, "_catalog_fetch_active_asin", None)
    return rows, attempts, excluded


def test_completeness_cooldown_and_barcode_mode(state):
    rows, _, _ = state
    rows[ASIN] = {"title": "Product", "image": "image.jpg"}
    assert main.catalog_fetch_eligibility(ASIN)["reason"] == "complete"
    assert main.fetch_missing_catalog_barcodes(BackgroundTasks()) == {"queued": 1}
    assert main.fetch_missing_catalog_barcodes(BackgroundTasks()) == {"queued": 0, "skipped_already_queued": 1}
    main._catalog_fetch_inflight.clear()
    rows[ASIN]["fetched_at"] = datetime.now(timezone.utc).isoformat()
    assert main.catalog_fetch_eligibility(ASIN, "barcode")["reason"] == "cooldown"
    rows[ASIN]["image"] = " "
    assert main.catalog_fetch_eligibility(ASIN)["reason"] == "cooldown"
    assert main.catalog_fetch_eligibility(ASIN, force=True)["eligible"]
    rows[ASIN]["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    assert main.catalog_fetch_eligibility(ASIN)["eligible"]


@pytest.mark.parametrize("reason", ["terminal", "excluded", "retry_exhausted", "retry_backoff", "active"])
def test_queue_protections(state, monkeypatch, reason):
    rows, attempts, excluded = state
    if reason == "terminal": attempts[ASIN] = {"terminal_code": "NOT_FOUND"}
    if reason == "excluded": excluded.add(ASIN)
    if reason == "retry_exhausted": attempts[ASIN] = {"attempts": main.CATALOG_FETCH_MAX_ATTEMPTS}
    if reason == "retry_backoff": attempts[ASIN] = {"attempts": 1, "last_error": "503", "last_attempt_at": datetime.now(timezone.utc).isoformat()}
    if reason == "active": monkeypatch.setattr(main, "_catalog_fetch_active_asin", ASIN)
    assert main.catalog_fetch_eligibility(ASIN)["reason"] == reason
    assert not main._queue_catalog_fetch(None, ASIN)
    assert main.fetch_missing_catalog_barcodes(BackgroundTasks())["skipped_" + reason] == 1


def test_gets_read_only(state, monkeypatch):
    monkeypatch.setattr(main, "get_catalog_asin_sources_map", lambda _: {})
    monkeypatch.setattr(main, "_load_inventory_asin_set", set)
    monkeypatch.setattr(main, "_load_realtime_sales_asin_set", set)
    def forbidden(*a, **k): raise AssertionError("GET wrote state")
    for name in ("_queue_catalog_fetch", "seed_catalog_universe", "record_catalog_asin_sources", "extract_asins_from_pos"):
        monkeypatch.setattr(main, name, forbidden)
    for _ in range(3):
        assert main.list_catalog_asins(BackgroundTasks())["items"]
        assert main.get_catalog_queue_status()["queued"] == 0


def test_stale_worker_skips_complete(state, monkeypatch):
    rows, _, _ = state
    assert main._queue_catalog_fetch(None, ASIN)
    rows[ASIN] = {"title": "Product", "image": "image.jpg"}
    def forbidden(*a): raise AssertionError("network called")
    monkeypatch.setattr(main, "_perform_catalog_fetch", forbidden)
    class Stop:
        calls = 0
        def is_set(self):
            self.calls += 1
            return self.calls > 2
    monkeypatch.setattr(main, "_catalog_fetch_stop", Stop())
    main._catalog_fetch_worker_loop()
    assert not main._catalog_fetch_inflight


def test_ui_endpoint():
    ui = (Path(__file__).resolve().parents[1] / "ui/index.html").read_text(encoding="utf-8")
    assert 'onclick="fetchMissingBarcodes()"' in ui
    assert 'fetch("/api/catalog/fetch-missing-barcodes", { method: "POST" })' in ui
    assert 'Object.entries(data)' in ui


@pytest.mark.parametrize("with_identifier", [True, False])
def test_barcode_network_fetch_and_success_cooldown(state, monkeypatch, tmp_path, with_identifier):
    rows, attempts, _ = state
    db = tmp_path / "catalog.db"
    data = payload() if with_identifier else {"summaries": [{"itemName": "Product"}]}
    rows[ASIN] = {"title": "Product", "image": "image.jpg"}
    monkeypatch.setattr(main, "MARKETPLACE_IDS", ["AE"])
    monkeypatch.setattr(main.auth_client, "get_lwa_access_token", lambda: "test")
    class Response:
        status_code = 200
        def json(self): return data
    def get(url, **kwargs):
        assert kwargs["params"]["includedData"] == "summaries,images,identifiers"
        return Response()
    monkeypatch.setattr(main.requests, "get", get)
    def save(asin, value):
        catalog.upsert_spapi_catalog(asin, value, db)
        rows.update(catalog.spapi_catalog_status(db))
    monkeypatch.setattr(main, "upsert_spapi_catalog", save)
    def record(asin, ok):
        assert ok
        attempts[asin] = {"attempts": 0, "last_attempt_at": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(main, "record_catalog_fetch_attempt", record)
    main._catalog_fetch_options[ASIN] = ("barcode", False)
    assert main._perform_catalog_fetch(ASIN) == (True, False, None)
    expected = "complete" if with_identifier else "cooldown"
    assert main.catalog_fetch_eligibility(ASIN, "barcode")["reason"] == expected
    assert rows[ASIN]["barcode"] == ("6976154505903" if with_identifier else None)


def test_po_sync_and_fetch_all_respect_cooldown(state, monkeypatch):
    rows, _, _ = state
    rows[ASIN] = {"title": "Product", "fetched_at": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(main, "seed_catalog_universe", lambda *a, **k: 0)
    monkeypatch.setattr(main, "record_catalog_asin_sources", lambda *a, **k: None)
    monkeypatch.setattr(main, "should_fetch_catalog", lambda *a, **k: True)
    def forbidden(*a, **k): raise AssertionError("reset attempts")
    monkeypatch.setattr(main, "reset_catalog_fetch_attempts", forbidden)
    assert main._activate_new_po_catalog_asins([ASIN])["queued"] == 0
    assert main.fetch_catalog_for_missing(BackgroundTasks())["queued"] == 0


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (400, False)])
def test_transient_failures_only(state, monkeypatch, status, retryable):
    from fastapi import HTTPException
    def fail(asin): raise HTTPException(status, "error")
    monkeypatch.setattr(main, "fetch_spapi_catalog_item", fail)
    monkeypatch.setattr(main, "record_catalog_fetch_attempt", lambda *a, **k: None)
    monkeypatch.setattr(main, "mark_catalog_fetch_terminal", lambda *a, **k: None)
    assert main._perform_catalog_fetch(ASIN)[1] is retryable
