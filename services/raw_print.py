"""Windows RAW printer helper."""

import logging

try:
    import win32print
except ImportError:  # pragma: no cover
    win32print = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def send_raw_to_printer(printer_name: str, raw_bytes: bytes, job_name: str = "EPL Print Job") -> None:
    """Send raw EPL bytes directly to the Windows printer queue."""
    if not win32print:
        raise RuntimeError("win32print is required on Windows to send RAW jobs")

    target = (printer_name or "").strip()
    if not target:
        target = win32print.GetDefaultPrinter()

    if not target:
        raise RuntimeError("No printer specified or available as default")

    logger.info("Sending RAW job %s to printer %s", job_name, target)

    handle = win32print.OpenPrinter(target)
    try:
        doc_info = (job_name, None, "RAW")
        win32print.StartDocPrinter(handle, 1, doc_info)
        page_started = False
        try:
            win32print.StartPagePrinter(handle)
            page_started = True
            win32print.WritePrinter(handle, raw_bytes)
        finally:
            if page_started:
                win32print.EndPagePrinter(handle)
            win32print.EndDocPrinter(handle)
    finally:
        win32print.ClosePrinter(handle)
