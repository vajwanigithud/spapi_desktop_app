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
    assert plan["report_window_hours"] == 1
    assert plan["reports_created_this_call"] == rt_sales.MAX_HOURLY_REPORTS_PER_FILL_DAY
    assert plan["hours_applied_this_call"] == rt_sales.MAX_HOURLY_REPORTS_PER_FILL_DAY


def test_fill_day_burst_multi_hour_windows(monkeypatch):
    date_str = "2025-12-11"
    start_end_pairs = [
        rt_sales.build_local_hour_window(date_str, hour) for hour in range(24)
    ]
    hour_isos = [rt_sales._utc_iso(start) for start, _ in start_end_pairs]
    missing_isos = set(hour_isos[:18])

    def _dynamic_classify(date_str: str, marketplace_id: str, latest_allowed_end=None):
        hours_detail = []
        missing_list = []
        for idx, (start, end) in enumerate(start_end_pairs):
            start_iso = rt_sales._utc_iso(start)
            status = "missing" if start_iso in missing_isos else "ok"
            hours_detail.append(
                {
                    "hour": idx,
                    "status": status,
                    "start_utc": start_iso,
                    "end_utc": rt_sales._utc_iso(end),
                }
            )
            if status == "missing":
                missing_list.append(idx)
        return hours_detail, missing_list, []

    monkeypatch.setattr(rt_sales, "_classify_daily_hours", _dynamic_classify)
    monkeypatch.setattr(rt_sales, "enqueue_vendor_rt_sales_specific_hours", lambda *args, **kwargs: None)
    monkeypatch.setattr(rt_sales, "ledger_acquire_worker_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr(rt_sales, "ledger_release_worker_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(rt_sales, "ledger_refresh_worker_lock", lambda *args, **kwargs: None)

    requested_attempts = []

    def _fake_mark_requested(marketplace_id, hour_iso):
        requested_attempts.append(hour_iso)
        return 1

    applied_hours = []

    def _fake_mark_applied(marketplace_id, hour_iso):
        applied_hours.append(hour_iso)
        missing_isos.discard(hour_iso)

    downloaded_hours = []

    def _fake_mark_downloaded(marketplace_id, hour_iso, report_id):
        downloaded_hours.append((hour_iso, report_id))

    audit_calls = []

    monkeypatch.setattr(rt_sales, "ledger_mark_requested_explicit", _fake_mark_requested)
    monkeypatch.setattr(rt_sales, "ledger_mark_applied", _fake_mark_applied)
    monkeypatch.setattr(rt_sales, "ledger_mark_downloaded", _fake_mark_downloaded)
    monkeypatch.setattr(rt_sales, "ledger_mark_failed", lambda *args, **kwargs: None)

    def _fake_record(start, end, marketplace_id, seen):
        audit_calls.append((rt_sales._utc_iso(start), rt_sales._utc_iso(end), list(seen or [])))

    monkeypatch.setattr(rt_sales, "_record_audit_hours_for_window", _fake_record)

    report_calls = []

    def _fake_execute(start_utc, end_utc, marketplace_id, *, ledger_hour_isos=None, **kwargs):
        hours = list(ledger_hour_isos or [])
        report_calls.append((start_utc, end_utc, tuple(hours)))
        summary_hours = hours[:-1] if len(report_calls) == 2 and hours else hours
        report_id = f"REPORT-{len(report_calls)}"
        for hour in hours:
            rt_sales.ledger_mark_downloaded(marketplace_id, hour, report_id)
        rt_sales._record_audit_hours_for_window(start_utc, end_utc, marketplace_id, summary_hours)
        for hour in hours:
            rt_sales.ledger_mark_applied(marketplace_id, hour)
        return {
            "report_id": report_id,
            "start_utc": rt_sales._utc_iso(start_utc),
            "end_utc": rt_sales._utc_iso(end_utc),
            "marketplace_id": marketplace_id,
            "summary": {
                "rows": len(summary_hours),
                "hours": len(summary_hours),
                "hour_starts": summary_hours,
            },
        }

    monkeypatch.setattr(rt_sales, "_execute_vendor_rt_sales_report", _fake_execute)

    plan = rt_sales.plan_fill_day_run(
        date_str=date_str,
        requested_hours=None,
        marketplace_id="TEST",
        max_reports=6,
        burst_enabled=True,
        max_batches=3,
        report_window_hours=6,
    )
    assert plan["reports_created_this_call"] == 3

    rt_sales.run_fill_day_repair_cycle(
        date_str,
        plan["hours_to_request"],
        "TEST",
        plan["total_missing"],
        burst_enabled=True,
        burst_hours=6,
        max_batches=3,
        report_window_hours=6,
    )

    assert len(report_calls) == 3
    assert len(audit_calls) == len(report_calls)
    assert len(applied_hours) == 18
    assert len(requested_attempts) == 18
    assert any(len(seen) < len(call[2]) for call, (_, _, seen) in zip(report_calls, audit_calls))
    assert missing_isos == set()
