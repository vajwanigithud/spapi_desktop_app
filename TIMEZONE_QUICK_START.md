# Timezone Hardening - Quick Start Guide

## What Was Fixed
App was crashing on Windows Python 3.13 with:
```
ZoneInfoNotFoundError: 'No time zone found with key Asia/Dubai'
ModuleNotFoundError: No module named 'tzdata'
```

## What Changed
ONE file: `services/vendor_realtime_sales.py` (lines 12-39)

**The fix:** Wrapped timezone initialization in try/except with fallback to fixed UTC+4

## Does It Affect My Features?
✅ **NO** - Everything still works exactly the same:
- Real-Time Sales dashboard
- Lookback windows (2, 4, 8, 12, 24, 48 hours)
- View modes (ASIN, Time)
- UAE timezone display
- All aggregations and reports

## Quick Test
```bash
# Verify it imports without errors
python -c "from services.vendor_realtime_sales import UAE_TZ; print(UAE_TZ)"
```

Expected output: `Asia/Dubai` or `UTC+04:00` (both are correct)

## Manual Testing Checklist
See: `TIMEZONE_TESTING_CHECKLIST.md`

## Detailed Documentation
- **What changed & why**: `TIMEZONE_HARDENING_SUMMARY.md`
- **Code before/after**: `TIMEZONE_CODE_COMPARISON.md`
- **Verification results**: `TIMEZONE_CHANGES_COMPLETE.txt`

## The Pattern (For Reference)
```python
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:
    ZoneInfo = None
    class ZoneInfoNotFoundError(Exception):
        pass

# UAE timezone: prefer IANA zone, fall back to UTC+4
try:
    if ZoneInfo is None:
        raise ZoneInfoNotFoundError("zoneinfo not available")
    UAE_TZ = ZoneInfo("Asia/Dubai")
except ZoneInfoNotFoundError:
    UAE_TZ = timezone(timedelta(hours=4))  # Fixed UTC+4 offset
```

## Why This Works
- **With tzdata**: Uses proper IANA timezone database (Asia/Dubai)
- **Without tzdata**: Falls back to fixed UTC+4 offset
- Both produce identical results (UAE has no DST)
- App never crashes due to missing timezone data

## Deployment
✅ Safe to deploy immediately
- No breaking changes
- No new dependencies
- No database changes
- Fully backward compatible

## Questions?
See the detailed docs in the root directory:
- TIMEZONE_HARDENING_SUMMARY.md
- TIMEZONE_TESTING_CHECKLIST.md
- TIMEZONE_CODE_COMPARISON.md
- TIMEZONE_CHANGES_COMPLETE.txt
