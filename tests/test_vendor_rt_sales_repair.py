import contextlib
from datetime import datetime, timedelta, timezone

from services import vendor_realtime_sales as rt_sales


def _fake_coverage_map(hours: int = 6):
    base = datetime(2025, 12, 1, tzinfo=timezone.utc)
    coverage = {}
    for idx in range(hours):
        start = base + timedelta(hours=idx)
        coverage[rt_sales._utc_iso(start)] = {
            "hour_start": start,
            "hour_end": start + timedelta(hours=1),
            "status": "MISSING",
        }
    return coverage


def test_repair_30d_dry_run_counts(monkeypatch):
    monkeypatch.setattr(
        rt_sales,
        "_build_hourly_coverage_map",
        lambda *args, **kwargs: _fake_coverage_map(),
    )
    monkeypatch.setattr(
        rt_sales,
        "get_db_connection",
        lambda: contextlib.nullcontext(object()),
    )

    result = rt_sales.repair_missing_hours_last_30_days(
        marketplace_id="TEST",
        report_window_hours=3,
        max_runtime_seconds=120,
        max_reports=10,
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["hours_targeted"] == 6
    assert result["reports_created"] == 2
    assert result["remaining_missing"] == 6


def test_rt_sales_autosync_pause_state_expires():
    rt_sales.rt_sales_set_autosync_paused(False, None, None)
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    rt_sales.rt_sales_set_autosync_paused(True, rt_sales.RT_SALES_REPAIR_PAUSE_REASON, future)
    pause_state = rt_sales.rt_sales_get_autosync_pause(now_utc=datetime.now(timezone.utc), auto_clear=False)
    assert pause_state["paused"] is True
    rt_sales.rt_sales_set_autosync_paused(True, rt_sales.RT_SALES_REPAIR_PAUSE_REASON, datetime.now(timezone.utc) - timedelta(seconds=1))
    pause_state = rt_sales.rt_sales_get_autosync_pause(now_utc=datetime.now(timezone.utc))
    assert pause_state["paused"] is False
    rt_sales.rt_sales_set_autosync_paused(False, None, None)


def test_repair_30d_lock_busy(monkeypatch):
    monkeypatch.setattr(
        rt_sales,
        "_build_hourly_coverage_map",
        lambda *args, **kwargs: _fake_coverage_map(),
    )
    monkeypatch.setattr(
        rt_sales,
        "get_db_connection",
        lambda: contextlib.nullcontext(object()),
    )
    monkeypatch.setattr(rt_sales, "ledger_acquire_worker_lock", lambda *args, **kwargs: False)
    monkeypatch.setattr(rt_sales, "ledger_release_worker_lock", lambda *args, **kwargs: None)
    monkeypatch.setattr(rt_sales, "rt_sales_set_autosync_paused", lambda *args, **kwargs: None)

    result = rt_sales.repair_missing_hours_last_30_days(
        marketplace_id="TEST",
        report_window_hours=2,
        max_runtime_seconds=120,
        max_reports=5,
        dry_run=False,
    )
    assert result["ok"] is False
    assert result["stopped_reason"] == "lock_busy"
