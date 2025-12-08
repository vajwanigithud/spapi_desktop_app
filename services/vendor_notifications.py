import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from services.db import get_db_connection

logger = logging.getLogger(__name__)

VENDOR_NOTIFICATION_TYPES = [
    "ORDER_CHANGE",
    "ORDER_STATUS_CHANGE",
]

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
NOTIFICATIONS_LOG_PATH = LOG_DIR / "vendor_notifications.jsonl"


def _ensure_log_dir():
    try:
        LOG_DIR.mkdir(exist_ok=True)
    except Exception as exc:
        logger.warning(f"[VendorNotifications] Failed creating log dir: {exc}")


def _flags_path() -> Path:
    return LOG_DIR / "vendor_po_flags.json"


def _load_flags() -> Dict[str, Any]:
    path = _flags_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[VendorNotifications] Failed to read flags file: {exc}")
        return {}


def _save_flags(data: Dict[str, Any]) -> None:
    path = _flags_path()
    _ensure_log_dir()
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"[VendorNotifications] Failed to write flags file: {exc}")


def log_vendor_notification(event: Dict[str, Any]) -> None:
    """
    Append a single JSON object to logs/vendor_notifications.jsonl.
    """
    try:
        _ensure_log_dir()
        code = (
            event.get("notificationTypeCode")
            or event.get("code")
            or event.get("payload", {}).get("notificationTypeCode")
            or ""
        )
        summary = event.get("notificationType") or event.get("type") or ""
        if code:
            summary = f"{summary}: {code}" if summary else code
        entry = {
            "ts": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "notificationType": event.get("notificationType"),
            "po": event.get("purchaseOrderNumber") or event.get("po") or None,
            "summary": summary,
            "raw": event,
        }
        with NOTIFICATIONS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning(f"[VendorNotifications] Failed to log notification: {exc}")


def mark_po_as_needing_refresh(po_number: str, reason: str) -> None:
    if not po_number:
        return
    try:
        data = _load_flags()
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        entry = data.get(po_number) or {}
        entry.update(
            {
                "needs_refresh": True,
                "has_recent_notifications": True,
                "last_notification_ts": now,
                "last_notification_summary": reason,
            }
        )
        data[po_number] = entry
        _save_flags(data)
    except Exception as exc:
        logger.warning(f"[VendorNotifications] Failed to mark PO {po_number}: {exc}")


def clear_po_refresh_flag(po_number: str) -> None:
    if not po_number:
        return
    try:
        data = _load_flags()
        entry = data.get(po_number) or {}
        if entry.get("needs_refresh"):
            entry["needs_refresh"] = False
            data[po_number] = entry
            _save_flags(data)
    except Exception as exc:
        logger.warning(f"[VendorNotifications] Failed to clear refresh flag for {po_number}: {exc}")


def get_po_notification_flags(po_number: str) -> Dict[str, Any]:
    data = _load_flags()
    entry = data.get(po_number) or {}
    return {
        "needs_refresh": bool(entry.get("needs_refresh")),
        "has_recent_notifications": bool(entry.get("has_recent_notifications")),
        "last_notification_ts": entry.get("last_notification_ts"),
        "last_notification_summary": entry.get("last_notification_summary"),
    }


def process_vendor_notification(event: Dict[str, Any]) -> None:
    """
    High-level handler with full safety.
    """
    try:
        notification_type = event.get("notificationType") or event.get("type") or ""
        po_num = (
            event.get("purchaseOrderNumber")
            or event.get("poNumber")
            or event.get("po")
            or event.get("payload", {}).get("purchaseOrderNumber")
            or event.get("payload", {}).get("poNumber")
        )
        log_vendor_notification(event)

        if notification_type in VENDOR_NOTIFICATION_TYPES and po_num:
            code = (
                event.get("notificationTypeCode")
                or event.get("code")
                or event.get("payload", {}).get("notificationTypeCode")
                or ""
            )
            reason = f"{notification_type}{': ' + code if code else ''}"
            mark_po_as_needing_refresh(str(po_num), reason)
    except Exception as exc:
        logger.warning(f"[VendorNotifications] Failed processing notification: {exc}")


def get_recent_notifications(limit: int = 100):
    if not NOTIFICATIONS_LOG_PATH.exists():
        return []
    try:
        with NOTIFICATIONS_LOG_PATH.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        recent = lines[-limit:]
        out = []
        for line in recent:
            try:
                obj = json.loads(line.strip())
                out.append(
                    {
                        "ts": obj.get("ts"),
                        "notificationType": obj.get("notificationType"),
                        "po": obj.get("po"),
                        "summary": obj.get("summary") or obj.get("notificationType") or "",
                        "has_po": bool(obj.get("po")),
                    }
                )
            except Exception:
                continue
        return out
    except Exception as exc:
        logger.warning(f"[VendorNotifications] Failed reading recent notifications: {exc}")
        return []
