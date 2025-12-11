from datetime import datetime, timedelta, timezone

import pytest

import services.spapi_reports as spr


class DummyClient:
    def __init__(self):
        self.last_body = None

    def createReport(self, body):
        self.last_body = body
        return {"reportId": "RID"}


def test_inventory_defaults_vendor_fulfilled_and_caps_end(monkeypatch):
    dummy = DummyClient()
    monkeypatch.setattr(spr, "get_spapi_client", lambda: dummy)
    now = datetime.now(timezone.utc)
    rid = spr.request_vendor_report(
        "GET_VENDOR_REAL_TIME_INVENTORY_REPORT",
        data_start=now - timedelta(days=1),
        data_end=now,
    )
    assert rid == "RID"
    opts = dummy.last_body.get("reportOptions", {})
    assert opts.get("sellingProgram") == "RETAIL"
    end_dt = datetime.fromisoformat(dummy.last_body["dataEndTime"].replace("Z", "+00:00"))
    assert end_dt <= now.replace(minute=0, second=0, microsecond=0)


def test_explicit_selling_program_respected(monkeypatch):
    dummy = DummyClient()
    monkeypatch.setattr(spr, "get_spapi_client", lambda: dummy)
    spr.request_vendor_report("GET_VENDOR_REAL_TIME_INVENTORY_REPORT", selling_program="RETAIL_OVERRIDE")
    opts = dummy.last_body.get("reportOptions", {})
    assert opts.get("sellingProgram") == "RETAIL_OVERRIDE"


def test_create_report_quota_raises(monkeypatch):
    class QuotaClient:
        def createReport(self, body):
            raise spr.SpApiQuotaError("quotaExceeded")

    monkeypatch.setattr(spr, "get_spapi_client", lambda: QuotaClient())
    with pytest.raises(spr.SpApiQuotaError):
        spr.request_vendor_report("GET_VENDOR_REAL_TIME_INVENTORY_REPORT")
