# RT Sales Hour Ledger Primer

The vendor RT-sales refresh loop is now completely ledger-driven. Every hour we want to ingest is represented in SQLite (`vendor_rt_sales_hour_ledger`) and drives the worker through a deterministic lifecycle. This document is a quick reference for the team when the UI shows “not syncing” warnings.

## Ledger States

Each `(marketplace_id, hour_utc)` row moves through these statuses:

- **MISSING** – The hour exists in the ledger but has not been claimed yet. Seeding logic keeps a rolling lookback window filled with MISSING rows.
- **REQUESTED** – A worker claimed the hour and is waiting for an SP-API report to be generated.
- **DOWNLOADED** – The report document was downloaded and is ready to apply.
- **APPLIED** – Hourly rows were written to `vendor_realtime_sales`. This is the terminal “good” state.
- **FAILED** – The worker hit an error (quota, parsing, etc.) and stored `last_error` plus `next_retry_utc`. It will be claimable again when the retry time passes.

Use the ledger dashboard in the RT Sales tab to quickly see how many hours are in each state, when the last APPLIED hour landed, and which hour will be claimed next.

## Worker Lock

The **worker lock** (`vendor_rt_sales_worker_lock`) ensures that only one RT-sales worker (startup backfill, auto-sync loop, or fill-day repair) runs at a time per marketplace. Key points:

- Lock rows contain `marketplace_id`, `owner`, and `expires_at` (UTC). Owners include the task name and PID, e.g. `auto-sync:12345`.
- TTL defaults to 15 minutes (or 2× the auto-sync interval). Workers refresh the lock before/after every long SP-API call.
- If a worker crashes without releasing the lock the TTL eventually expires; the next run “steals” the lock and logs that it replaced a stale owner.
- The status endpoint marks a lock as **stale** if `expires_at <= now`. Investigate stale locks – they usually mean a worker died mid-flight.

## Cooldown

SP-API quota exhaustion triggers a **quota cooldown** (default 30 minutes). During a cooldown:

- No new report requests are made. The UI shows `Cooldown due to SP-API quota (until HH:MM)`.
- Manual refresh is disabled; use the Audit / Fill Day tools after cooldown expires.
- The status endpoint exposes `cooldown.reason` (`quota`, `lock_busy`, `none`) and `cooldown.until_utc` when known.

## Troubleshooting Checklist

1. **Lock Busy** – If “RT Sales: Locked by …” stays stuck for >15 minutes, check logs for the owner. You can force a restart only after confirming the lock is stale.
2. **Quota Cooldown** – When quota is the reason, wait until the `until` timestamp and avoid triggering more SP-API calls.
3. **Missing Hours Never Drop** – Look at the ledger dashboard: if MISSING stays high but `next_claimable_hour` is far in the past, ensure the worker lock isn’t held and that `next_retry_utc` for FAILED rows isn’t in the future.
4. **Applied Never Advances** – Inspect `last_error` for FAILED hours. Use Fill Day repair to reschedule failing hours once the root cause is resolved.
5. **UI Still Shows Old Data** – Verify the status endpoint returns `ok: true` and the ledger counts update every 30 seconds while the RT Sales tab is active. If not, check browser console/network logs.

Understanding these moving parts should make it easier to explain “why it’s not syncing” without tailing server logs. Use the new `/api/vendor/rt-sales/status` endpoint (or the UI strip) whenever you need to debug RT-sales freshness.
