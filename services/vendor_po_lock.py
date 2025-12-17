import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from services import db as db_service
from services.vendor_po_store import (
    SYNC_TABLE,
    ensure_vendor_po_schema,
    get_vendor_po_sync_state,
)

LOGGER = logging.getLogger(__name__)
LOCK_TTL_SECONDS = 30 * 60


def acquire_vendor_po_lock(
    owner: str,
    *,
    ttl_seconds: int = LOCK_TTL_SECONDS,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Attempt to acquire the Vendor PO sync lock.

    Returns (acquired: bool, state: dict).
    """
    ensure_vendor_po_schema()
    owner = owner or "unknown"
    now = _now()
    expires_at = now + timedelta(seconds=max(LOCK_TTL_SECONDS, ttl_seconds))
    now_iso = _iso(now)
    expires_iso = _iso(expires_at)

    with db_service.get_db_connection() as conn:
        row = conn.execute(f"SELECT * FROM {SYNC_TABLE} WHERE id = 1").fetchone()
        state = dict(row) if row else {}
        stale = False
        if row and row["sync_in_progress"]:
            lock_exp = _parse_iso(row["lock_expires_at"])
            lock_start = _parse_iso(row["sync_started_at"])
            if lock_exp and lock_exp > now:
                LOGGER.info("[VendorPOLock] Lock already held by %s until %s", row["lock_owner"], row["lock_expires_at"])
                return False, state
            if not lock_exp and lock_start and (now - lock_start).total_seconds() <= LOCK_TTL_SECONDS:
                LOGGER.info("[VendorPOLock] Lock already held by %s", row["lock_owner"])
                return False, state
            stale = True

        if stale:
            LOGGER.warning(
                "[VendorPOLock] Detected stale Vendor PO lock held by %s; reclaiming",
                state.get("lock_owner") or "unknown",
            )
            conn.execute(
                f"""
                UPDATE {SYNC_TABLE}
                SET sync_in_progress = 0,
                    lock_owner = NULL,
                    lock_expires_at = NULL
                WHERE id = 1
                """
            )
            conn.commit()

        conn.execute(
            f"""
            UPDATE {SYNC_TABLE}
            SET sync_in_progress = 1,
                sync_started_at = ?,
                sync_finished_at = NULL,
                lock_owner = ?,
                lock_expires_at = ?,
                sync_last_error = NULL
            WHERE id = 1
            """,
            (now_iso, owner, expires_iso),
        )
        conn.commit()

    new_state = get_vendor_po_sync_state()
    return True, new_state


def heartbeat_vendor_po_lock(owner: str, *, ttl_seconds: int = LOCK_TTL_SECONDS) -> None:
    """
    Extend lock expiry while a sync is still running.
    """
    ensure_vendor_po_schema()
    now = _now()
    new_expiry = _iso(now + timedelta(seconds=max(LOCK_TTL_SECONDS, ttl_seconds)))
    with db_service.get_db_connection() as conn:
        conn.execute(
            f"""
            UPDATE {SYNC_TABLE}
            SET lock_expires_at = ?
            WHERE id = 1 AND lock_owner = ?
            """,
            (new_expiry, owner),
        )
        conn.commit()


def release_vendor_po_lock(
    owner: str,
    *,
    status: str,
    error: Optional[str] = None,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Release the vendor PO lock and persist sync metadata.
    """
    ensure_vendor_po_schema()
    now_iso = _iso(_now())
    success = (status or "").upper() == "SUCCESS"
    error_text = (error or "").strip() if not success else None

    with db_service.get_db_connection() as conn:
        conn.execute(
            f"""
            UPDATE {SYNC_TABLE}
            SET sync_in_progress = 0,
                sync_finished_at = ?,
                sync_last_ok_at = CASE WHEN ? THEN ? ELSE sync_last_ok_at END,
                sync_last_error = ?,
                lock_owner = NULL,
                lock_expires_at = NULL,
                last_sync_window_start = COALESCE(?, last_sync_window_start),
                last_sync_window_end = COALESCE(?, last_sync_window_end)
            WHERE id = 1
            """,
            (
                now_iso,
                1 if success else 0,
                now_iso if success else None,
                error_text,
                window_start,
                window_end,
            ),
        )
        conn.commit()
    return get_vendor_po_sync_state()


def fail_with_error(owner: str, error: str) -> Dict[str, Any]:
    """
    Convenience helper to release the lock with failure status.
    """
    return release_vendor_po_lock(owner, status="FAILED", error=error)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


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
