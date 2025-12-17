import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from services import db as db_service
from services.vendor_po_store import (
    count_vendor_po_headers,
    count_vendor_po_lines,
    get_vendor_po_sync_state,
)

LOGGER = logging.getLogger(__name__)
STATUS_KEY = "vendor_po_status_meta"
LOCK_STALE_THRESHOLD_SECONDS = 30 * 60


def _ensure_app_kv_table() -> None:
    with db_service.get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_kv_store (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _default_meta() -> Dict[str, Any]:
    return {
        "last_success_at": None,
        "last_error_at": None,
        "last_error": None,
        "last_run_started_at": None,
        "last_run_finished_at": None,
        "last_run_duration_s": None,
        "last_operation": None,
    }


def load_vendor_po_status_meta() -> Dict[str, Any]:
    _ensure_app_kv_table()
    with db_service.get_db_connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_kv_store WHERE key = ?",
            (STATUS_KEY,),
        ).fetchone()
    meta = _default_meta()
    if not row or not row["value"]:
        return meta
    try:
        payload = json.loads(row["value"])
        if isinstance(payload, dict):
            meta.update({k: payload.get(k) for k in meta})
    except Exception:
        LOGGER.warning("[VendorPOStatus] Failed to parse stored metadata; resetting")
    return meta


def _write_meta(meta: Dict[str, Any]) -> None:
    _ensure_app_kv_table()
    serialized = json.dumps(meta)
    with db_service.get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO app_kv_store (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (STATUS_KEY, serialized),
        )
        conn.commit()


def _update_meta(updates: Dict[str, Any]) -> Dict[str, Any]:
    current = load_vendor_po_status_meta()
    current.update(updates)
    _write_meta(current)
    return current


def record_vendor_po_run_start(operation: str, started_at: Optional[str] = None) -> None:
    started_iso = started_at or _utc_now()
    _update_meta(
        {
            "last_operation": operation,
            "last_run_started_at": started_iso,
            "last_run_finished_at": None,
            "last_run_duration_s": None,
        }
    )


def record_vendor_po_run_success(finished_at: Optional[str] = None) -> None:
    finished_iso = finished_at or _utc_now()
    meta = load_vendor_po_status_meta()
    duration = _compute_duration(meta.get("last_run_started_at"), finished_iso)
    _update_meta(
        {
            "last_success_at": finished_iso,
            "last_error": None,
            "last_error_at": None,
            "last_run_finished_at": finished_iso,
            "last_run_duration_s": duration,
        }
    )


def record_vendor_po_run_failure(error: str, finished_at: Optional[str] = None) -> None:
    finished_iso = finished_at or _utc_now()
    meta = load_vendor_po_status_meta()
    duration = _compute_duration(meta.get("last_run_started_at"), finished_iso)
    msg = (error or "").strip()
    if len(msg) > 180:
        msg = msg[:177] + "..."
    _update_meta(
        {
            "last_error": msg or "unknown failure",
            "last_error_at": finished_iso,
            "last_run_finished_at": finished_iso,
            "last_run_duration_s": duration,
        }
    )


def get_vendor_po_status_payload() -> Dict[str, Any]:
    sync_state = get_vendor_po_sync_state()
    meta = load_vendor_po_status_meta()
    now = datetime.now(timezone.utc)
    lock_owner = sync_state.get("lock_owner")
    lock_acquired = sync_state.get("sync_started_at")
    lock_expires = sync_state.get("lock_expires_at")
    in_progress = bool(sync_state.get("sync_in_progress"))
    acquired_dt = _parse_iso(lock_acquired)
    expires_dt = _parse_iso(lock_expires)
    lock_stale = False
    stale_seconds = None
    if in_progress:
        threshold = expires_dt or (
            acquired_dt + timedelta(seconds=LOCK_STALE_THRESHOLD_SECONDS)
            if acquired_dt
            else None
        )
        if threshold and threshold < now:
            lock_stale = True
            stale_seconds = int((now - threshold).total_seconds())

    if lock_stale and stale_seconds is None and acquired_dt:
        stale_seconds = int(max(0, (now - acquired_dt).total_seconds()))

    state = "idle"
    last_error = meta.get("last_error")
    if in_progress:
        state = "running"
        if lock_stale:
            state = "error"
    elif last_error:
        state = "error"

    lock_info = {
        "held": in_progress,
        "owner": lock_owner,
        "acquired_at": lock_acquired,
        "stale": lock_stale,
        "stale_seconds": stale_seconds,
    }

    payload = {
        "state": state,
        "operation": meta.get("last_operation"),
        "lock": lock_info,
        "last_success_at": meta.get("last_success_at"),
        "last_error_at": meta.get("last_error_at"),
        "last_error": last_error,
        "last_run_started_at": meta.get("last_run_started_at"),
        "last_run_finished_at": meta.get("last_run_finished_at"),
        "last_run_duration_s": meta.get("last_run_duration_s"),
        "source": "DB",
        "counts": {
            "headers": count_vendor_po_headers(),
            "lines": count_vendor_po_lines(),
        },
        "sync_state": sync_state,
    }
    return payload


def _compute_duration(start_iso: Optional[str], end_iso: Optional[str]) -> Optional[float]:
    start_dt = _parse_iso(start_iso)
    end_dt = _parse_iso(end_iso)
    if not start_dt or not end_dt:
        return None
    duration = (end_dt - start_dt).total_seconds()
    if duration < 0:
        return None
    return round(duration, 3)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt
