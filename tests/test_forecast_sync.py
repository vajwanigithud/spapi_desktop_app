import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone

import pytest

import services.forecast_sync as fs
from services.spapi_reports import SpApiQuotaError


def _make_temp_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.execute(
        """
        CREATE TABLE vendor_sales_history (
            asin TEXT NOT NULL,
            marketplace_id TEXT NOT NULL,
            sales_date TEXT NOT NULL,
            units REAL NOT NULL,
            revenue REAL NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (asin, marketplace_id, sales_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE vendor_forecast (
            id INTEGER PRIMARY KEY,
            asin TEXT NOT NULL,
            marketplace_id TEXT NOT NULL,
            forecast_generation_date TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            mean_units REAL NOT NULL,
            p70_units REAL NOT NULL,
            p80_units REAL NOT NULL,
            p90_units REAL NOT NULL,
            UNIQUE (asin, marketplace_id, start_date, end_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE vendor_rt_inventory (
            asin TEXT PRIMARY KEY,
            marketplace_id TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            highly_available_inventory INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    return tmp.name


@pytest.fixture()
def temp_db(monkeypatch):
    db_path = _make_temp_db()

    def _conn():
        conn = sqlite3.connect(db_path)
        return conn

    monkeypatch.setattr(fs, "get_db_connection", _conn)
    yield db_path


def test_is_error_envelope():
    assert fs._is_error_envelope({"errorDetails": {}})
    assert fs._is_error_envelope({"reportRequestError": {}})
    assert fs._is_error_envelope({"errors": []})
    assert not fs._is_error_envelope({"ok": True})
    assert not fs._is_error_envelope([])


def test_sales_sync_ok(monkeypatch, temp_db):
    monkeypatch.setattr(fs, "_iter_date_chunks", lambda *_, **__: [(date(2025, 1, 1), date(2025, 1, 1))])
    monkeypatch.setattr(fs, "request_vendor_report", lambda *_, **__: "RID")
    monkeypatch.setattr(fs, "poll_vendor_report", lambda *_: {"reportDocumentId": "DOC"})
    monkeypatch.setattr(
        fs,
        "download_vendor_report_document",
        lambda *_: {"salesByAsin": [{"asin": "B001", "startDate": "2025-01-01", "shippedUnits": 2, "shippedRevenue": {"amount": 10}}]},
    )

    result = fs.sync_vendor_sales_history(start_date=date(2025, 1, 1), end_date=date(2025, 1, 1))
    assert result["status"] == "ok"
    assert result["sales_rows"] == 1

    conn = sqlite3.connect(temp_db)
    rows = conn.execute("SELECT asin, units FROM vendor_sales_history").fetchall()
    assert rows == [("B001", 2.0)]


def test_forecast_error_envelope(monkeypatch, temp_db):
    monkeypatch.setattr(fs, "request_vendor_report", lambda *_, **__: "RID")
    monkeypatch.setattr(fs, "poll_vendor_report", lambda *_: {"reportDocumentId": "DOC"})
    monkeypatch.setattr(fs, "download_vendor_report_document", lambda *_: {"errorDetails": {"message": "boom"}})
    with pytest.raises(fs.ForecastSyncError):
        fs.sync_vendor_forecast()


def test_forecast_empty_warning(monkeypatch, temp_db):
    monkeypatch.setattr(fs, "request_vendor_report", lambda *_, **__: "RID")
    monkeypatch.setattr(fs, "poll_vendor_report", lambda *_: {"reportDocumentId": "DOC"})
    monkeypatch.setattr(fs, "download_vendor_report_document", lambda *_: {"forecastByAsin": []})

    result = fs.sync_vendor_forecast()
    assert result["status"] == "warning"
    assert result["forecast_rows"] == 0


def test_forecast_ok(monkeypatch, temp_db):
    monkeypatch.setattr(fs, "request_vendor_report", lambda *_, **__: "RID")
    monkeypatch.setattr(fs, "poll_vendor_report", lambda *_: {"reportDocumentId": "DOC"})
    monkeypatch.setattr(
        fs,
        "download_vendor_report_document",
        lambda *_: {
            "forecastByAsin": [
                {
                    "asin": "B001",
                    "forecastGenerationDate": "2025-01-01",
                    "startDate": "2025-01-02",
                    "endDate": "2025-01-03",
                    "p70ForecastUnits": 5,
                    "meanForecastUnits": 7,
                }
            ]
        },
    )

    result = fs.sync_vendor_forecast()
    assert result["status"] == "ok"
    assert result["forecast_rows"] == 1


def test_inventory_reportdata_ok(monkeypatch, temp_db):
    monkeypatch.setattr(fs, "request_vendor_report", lambda *_, **__: "RID")
    monkeypatch.setattr(fs, "poll_vendor_report", lambda *_: {"reportDocumentId": "DOC"})
    payload = {
        "reportData": [
            {"asin": "B001", "highlyAvailableInventory": 10, "startTime": "2025-01-01T00:00:00Z"},
            {"asin": "B002", "highlyAvailableInventory": 5, "startTime": "2025-01-01T00:00:00Z"},
        ]
    }
    monkeypatch.setattr(fs, "download_vendor_report_document", lambda *_: payload)

    result = fs.sync_vendor_rt_inventory()
    assert result["status"] == "ok"
    assert result["inventory_rows"] == 2


def test_inventory_warning_low_upserts(monkeypatch, temp_db):
    monkeypatch.setattr(fs, "request_vendor_report", lambda *_, **__: "RID")
    monkeypatch.setattr(fs, "poll_vendor_report", lambda *_: {"reportDocumentId": "DOC"})
    payload = {"reportData": [{"asin": "B001", "highlyAvailableInventory": 1}, {"foo": "bar"}]}
    monkeypatch.setattr(fs, "download_vendor_report_document", lambda *_: payload)

    result = fs.sync_vendor_rt_inventory()
    assert result["status"] == "warning"
    assert result["inventory_rows"] == 1
    assert result["expected_rows"] == 2


def test_sync_all_statuses(monkeypatch, temp_db):
    monkeypatch.setattr(fs, "sync_vendor_sales_history", lambda **_: {"status": "ok"})
    monkeypatch.setattr(fs, "sync_vendor_forecast", lambda **_: {"status": "warning"})
    monkeypatch.setattr(fs, "sync_vendor_rt_inventory", lambda **_: {"status": "ok"})
    summary = fs.sync_all_forecast_sources()
    assert summary["status"] == "warning"
    assert summary["statuses"]["forecast"] == "warning"

    monkeypatch.setattr(fs, "sync_vendor_forecast", lambda **_: {"status": "error"})
    with pytest.raises(fs.ForecastSyncError):
        fs.sync_all_forecast_sources()


def test_sync_all_quota_error(monkeypatch, temp_db):
    def raise_quota(**kwargs):
        raise SpApiQuotaError("quota")

    monkeypatch.setattr(fs, "sync_vendor_sales_history", raise_quota)
    with pytest.raises(fs.ForecastSyncError):
        fs.sync_all_forecast_sources()


def test_sync_all_lock(monkeypatch, temp_db):
    # Manually acquire the lock to simulate in-progress run
    fs._sync_lock.acquire()
    try:
        with pytest.raises(fs.ForecastSyncError):
            fs.sync_all_forecast_sources()
    finally:
        fs._sync_lock.release()
