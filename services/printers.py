# Manual Test Checklist:
# 1. Call GET /api/printers and verify the response includes a printers array and any warning text.
# 2. POST to /api/printers/default with a printer name and label settings, then GET default to confirm persistence.

import json
import logging
import platform
import sqlite3
from typing import Any, Dict, List, Optional

from services.db import get_app_kv, get_db_connection, set_app_kv

try:
    import win32print
except ImportError:  # pragma: no cover
    win32print = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
LOG_PREFIX = "[Printers]"
SETTINGS_KEY = "barcode_printer_settings"
DEFAULT_PRINT_BACKEND = "nice_label"
DEFAULT_LABEL_SETTINGS: Dict[str, Any] = {
    "label_width_mm": 40,
    "label_height_mm": 30,
    "dpi": 203,
    "darkness": 12,
    "speed": 3,
    "print_method": "direct_thermal",
    "media_type": "gap",
    "gap_mm": 0,
}
PRESETS: Dict[str, Dict[str, Any]] = {
    "Zebra TLP 2844 (38x25 DT, Gap)": {
        "label_settings": {
            "label_width_mm": 38,
            "label_height_mm": 25,
            "dpi": 203,
            "darkness": 11,
            "speed": 3,
            "print_method": "direct_thermal",
            "media_type": "gap",
            "gap_mm": 3,
        },
        "print_backend": DEFAULT_PRINT_BACKEND,
    }
}


def list_printers() -> Dict[str, Any]:
    """Enumerate locally-installed printers when Windows + pywin32 are available."""
    printers: List[str] = []
    warning: Optional[str] = None
    if platform.system().lower() == "windows":
        if not win32print:
            warning = "pywin32 not installed"
            logger.warning(f"{LOG_PREFIX} Unable to enumerate printers: module missing")
        else:
            try:
                flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
                printers = [entry[2] for entry in win32print.EnumPrinters(flags)]
                if not printers:
                    warning = "No printers detected on this machine"
            except Exception as exc:  # pragma: no cover
                warning = "Failed to enumerate printers"
                logger.error(f"{LOG_PREFIX} EnumPrinters failed: {exc}", exc_info=True)
    else:
        warning = "Printer enumeration not supported on this OS"
    payload: Dict[str, Any] = {"printers": printers}
    if warning:
        payload["warning"] = warning
    return payload


def get_printer_settings() -> Dict[str, Any]:
    """Return the stored default printer name, label settings, preset metadata, and backend."""
    with get_db_connection() as conn:
        stored = _load_settings(conn)
    return {
        "default_printer_name": stored.get("default_printer_name", ""),
        "label_settings": stored.get("label_settings", DEFAULT_LABEL_SETTINGS.copy()),
        "selected_preset": stored.get("selected_preset", ""),
        "presets": PRESETS,
        "print_backend": stored.get("print_backend", DEFAULT_PRINT_BACKEND),
    }


def save_printer_settings(
    default_printer_name: Optional[str],
    label_settings: Optional[Dict[str, Any]],
    selected_preset: Optional[str] = None,
    print_backend: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Persist the default printer name and label defaults.
    If a parameter is None, the existing value is preserved.
    """
    with get_db_connection() as conn:
        current = _load_settings(conn)
        printer_name = default_printer_name
        if printer_name is None:
            printer_name = current.get("default_printer_name", "")
        else:
            printer_name = str(printer_name).strip()

        merged_labels = _merge_label_settings(label_settings, current.get("label_settings", {}))
        payload: Dict[str, Any] = {
            "default_printer_name": printer_name,
            "label_settings": merged_labels,
            "selected_preset": selected_preset or "",
            "print_backend": print_backend or current.get("print_backend", DEFAULT_PRINT_BACKEND),
        }
        _persist_settings(conn, payload)
    logger.info(f"{LOG_PREFIX} Saved default printer: {printer_name or '<unset>'}")
    return payload


def _load_settings(conn) -> Dict[str, Any]:
    raw = _read_raw_settings(conn)
    if not raw:
        return {
            "default_printer_name": "",
            "label_settings": DEFAULT_LABEL_SETTINGS.copy(),
            "selected_preset": "",
            "print_backend": DEFAULT_PRINT_BACKEND,
        }

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        logger.warning(f"{LOG_PREFIX} Failed to parse printer settings JSON: {exc}")
        parsed = {}

    label_settings = _merge_label_settings(
        parsed.get("label_settings"), DEFAULT_LABEL_SETTINGS.copy()
    )
    default_name = parsed.get("default_printer_name", "")
    if default_name is None:
        default_name = ""

    selected_preset = parsed.get("selected_preset", "")

    return {
        "default_printer_name": str(default_name),
        "label_settings": label_settings,
        "selected_preset": selected_preset or "",
        "print_backend": parsed.get("print_backend", DEFAULT_PRINT_BACKEND) or DEFAULT_PRINT_BACKEND,
    }


def _merge_label_settings(
    updates: Optional[Dict[str, Any]],
    base: Dict[str, Any],
) -> Dict[str, Any]:
    normalized = base.copy()
    if not isinstance(updates, dict):
        return normalized

    for key in normalized.keys():
        value = updates.get(key)
        if value is None:
            continue
        if key in {"label_width_mm", "label_height_mm", "dpi", "darkness", "speed", "gap_mm"}:
            if isinstance(value, (int, float)):
                normalized[key] = value
        else:
            normalized[key] = str(value)
    return normalized


def _persist_settings(conn, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload)
    try:
        set_app_kv(conn, SETTINGS_KEY, data)
        return
    except sqlite3.Error as exc:
        logger.warning(f"{LOG_PREFIX} app_kv_store unavailable: {exc}")

    _ensure_barcode_settings_table(conn)
    conn.execute(
        """
        INSERT INTO barcode_settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (SETTINGS_KEY, data),
    )
    conn.commit()


def _read_raw_settings(conn) -> Optional[str]:
    try:
        return get_app_kv(conn, SETTINGS_KEY)
    except sqlite3.Error:
        pass

    _ensure_barcode_settings_table(conn)
    row = conn.execute(
        "SELECT value FROM barcode_settings WHERE key = ? LIMIT 1", (SETTINGS_KEY,)
    ).fetchone()
    if row:
        return row[0]
    return None


def _ensure_barcode_settings_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS barcode_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.commit()
