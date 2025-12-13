"""Printer health helpers."""

import logging
from typing import Dict

from services.printers import get_printer_settings

try:
    import win32print
except ImportError:  # pragma: no cover
    win32print = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def get_default_printer_health() -> Dict[str, object]:
    """Return a lightweight readiness report for the saved default printer."""
    settings = get_printer_settings()
    printer_name = (settings.get("default_printer_name") or "").strip()
    if not printer_name:
        return {
            "ok": False,
            "ready": False,
            "reason": "No default printer selected",
            "printer": "",
            "raw_status": 0,
        }

    if not win32print:
        return {
            "ok": False,
            "ready": False,
            "reason": "win32print unavailable",
            "printer": printer_name,
            "raw_status": 0,
        }

    handle = None
    try:
        handle = win32print.OpenPrinter(printer_name)
        info = win32print.GetPrinter(handle, 2)
        status = info.get("Status", 0)
        ready = status == 0
        reasons = []
        flag_map = {
            getattr(win32print, "PRINTER_STATUS_OFFLINE", 0): "Printer is offline",
            getattr(win32print, "PRINTER_STATUS_PAUSED", 0): "Printer is paused",
            getattr(win32print, "PRINTER_STATUS_ERROR", 0): "Printer error",
            getattr(win32print, "PRINTER_STATUS_PAPER_OUT", 0): "Paper is out",
            getattr(win32print, "PRINTER_STATUS_DOOR_OPEN", 0): "Door is open",
            getattr(win32print, "PRINTER_STATUS_TONER_LOW", 0): "Toner is low",
            getattr(win32print, "PRINTER_STATUS_OUT_OF_MEMORY", 0): "Out of memory",
            getattr(win32print, "PRINTER_STATUS_NOT_AVAILABLE", 0): "Printer not available",
        }
        for flag, message in flag_map.items():
            if flag and (status & flag):
                reasons.append(message)
                ready = False

        reason_text = "; ".join(reasons) or ("Ready" if ready else "Unknown status")
        return {
            "ok": True,
            "ready": ready,
            "reason": reason_text,
            "printer": printer_name,
            "raw_status": status,
        }
    except Exception as exc:
        logger.warning("Failed to determine printer health for %s: %s", printer_name, exc)
        return {
            "ok": False,
            "ready": False,
            "reason": str(exc),
            "printer": printer_name,
            "raw_status": 0,
        }
    finally:
        if handle:
            try:
                win32print.ClosePrinter(handle)
            except Exception:
                pass
