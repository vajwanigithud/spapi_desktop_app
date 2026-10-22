from datetime import date, datetime, timezone

import pytest

import services.spapi_reports as spr


class DummyClient:
    def __init__(self):
        self.last_body = None

    def createReport(self, body):
        self.last_body = body
        return {"reportId": "RID"}


def test_daily_vendor_inventory_report_options_are_passed_through(monkeypatch):
    dummy = DummyClient()
    monkeypatch.setattr(spr, "get_spapi_client", lambda: dummy)
    report_day = date(2026, 10, 20)
    rid = spr.request_vendor_report(
        "GET_VENDOR_INVENTORY_REPORT",
        params={"marketplaceIds": ["A2VIGQ35RCS4UG"]},
        data_start=datetime(2026, 10, 20, tzinfo=timezone.utc),
        data_end=datetime(2026, 10, 20, 23, 59, 59, tzinfo=timezone.utc),
        report_period="DAY",
        selling_program="RETAIL",
        distributor_view="MANUFACTURING",
    )
    assert rid == "RID"
    assert dummy.last_body["reportType"] == "GET_VENDOR_INVENTORY_REPORT"
    assert dummy.last_body["dataStartTime"].startswith(report_day.isoformat())
    assert dummy.last_body["reportOptions"] == {
        "reportPeriod": "DAY",
        "sellingProgram": "RETAIL",
        "distributorView": "MANUFACTURING",
    }


def test_create_report_quota_raises(monkeypatch):
    class QuotaClient:
        def createReport(self, body):
            raise spr.SpApiQuotaError("quotaExceeded")

    monkeypatch.setattr(spr, "get_spapi_client", lambda: QuotaClient())
    with pytest.raises(spr.SpApiQuotaError):
        spr.request_vendor_report("GET_VENDOR_INVENTORY_REPORT")
