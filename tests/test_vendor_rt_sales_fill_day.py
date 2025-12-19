import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("LWA_CLIENT_ID", "test-client")
os.environ.setdefault("LWA_CLIENT_SECRET", "test-secret")
os.environ.setdefault("LWA_REFRESH_TOKEN", "test-refresh")

from services import vendor_realtime_sales as rt_sales


def _build_fake_hours(missing_hours: list[int]):
    missing_set = set(missing_hours)

    def _fake_classify(date_str: str, marketplace_id: str, latest_allowed_end=None):
        hours_detail = []
        missing_list = []
        for hour in range(24):
            start = datetime(2025, 12, 11, hour, tzinfo=timezone.utc)
            end = start + timedelta(hours=1)
            status = "missing" if hour in missing_set else "ok"
            hours_detail.append(
                {
                    "hour": hour,
                    "status": status,
                    "start_utc": rt_sales._utc_iso(start),
                    "end_utc": rt_sales._utc_iso(end),
                }
            )
            if status == "missing":
                missing_list.append(hour)
        return hours_detail, missing_list, []

    return _fake_classify


def test_fill_day_default_caps_three(monkeypatch):
    monkeypatch.setattr(
        rt_sales,
        "_classify_daily_hours",
        _build_fake_hours([0, 1, 2, 3, 4]),
    )
    plan = rt_sales.plan_fill_day_run(
        date_str="2025-12-11",
        requested_hours=None,
        marketplace_id="TEST",
    )
    assert len(plan["hours_to_request"]) == rt_sales.MAX_HOURLY_REPORTS_PER_FILL_DAY
    assert plan["burst_enabled"] is False
    assert plan["batches_run"] == 1
    assert plan["hours_applied_this_call"] == rt_sales.MAX_HOURLY_REPORTS_PER_FILL_DAY


def test_fill_day_burst_processes_multiple_batches(monkeypatch):
    missing_state = [0, 1, 2, 3, 4, 5, 6]

    def _dynamic_classify(date_str: str, marketplace_id: str, latest_allowed_end=None):
        current_missing = list(missing_state)
        hours_detail = []
        for hour in range(24):
            start = datetime(2025, 12, 11, hour, tzinfo=timezone.utc)
            end = start + timedelta(hours=1)
            status = "missing" if hour in current_missing else "ok"
            hours_detail.append(
                {
                    "hour": hour,
                    "status": status,
                    "start_utc": rt_sales._utc_iso(start),
                    "end_utc": rt_sales._utc_iso(end),
                }
            )
        return hours_detail, current_missing, []

    monkeypatch.setattr(rt_sales, "_classify_daily_hours", _dynamic_classify)
    monkeypatch.setattr(rt_sales, "enqueue_vendor_rt_sales_specific_hours", lambda *args, **kwargs: None)
    monkeypatch.setattr(rt_sales, "ledger_acquire_worker_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr(rt_sales, "ledger_release_worker_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(rt_sales, "ledger_refresh_worker_lock", lambda *args, **kwargs: None)

    process_calls = {"count": 0}

    def _fake_process(*args, **kwargs):
        if not missing_state:
            return {"ok": True, "requested": 0, "applied": 0, "message": "no work"}
        missing_state.pop(0)
        process_calls["count"] += 1
        return {"ok": True, "requested": 1, "applied": 1}

    monkeypatch.setattr(rt_sales, "process_rt_sales_hour_ledger", _fake_process)

    plan = rt_sales.plan_fill_day_run(
        date_str="2025-12-11",
        requested_hours=None,
        marketplace_id="TEST",
        max_reports=4,
        burst_enabled=True,
        max_batches=2,
    )
    rt_sales.run_fill_day_repair_cycle(
        "2025-12-11",
        plan["hours_to_request"],
        "TEST",
        plan["total_missing"],
        burst_enabled=True,
        burst_hours=4,
        max_batches=2,
    )
    assert process_calls["count"] == 7
    assert missing_state == []
