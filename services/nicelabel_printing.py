"""Helpers for sending payloads to a NiceLabel TCP integration."""

import logging
import socket
from typing import Dict

logger = logging.getLogger(__name__)


def _ensure_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def send_print_job(
    host: str, port: int, payload_text: str, timeout: float = 5
) -> Dict[str, int]:
    """Send the provided payload over TCP to a NiceLabel listener."""
    payload = _ensure_newline(payload_text)
    encoded = payload.encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(encoded)
    except OSError as exc:
        msg = f"Unable to send NiceLabel payload to {host}:{port}: {exc}"
        logger.error(msg, exc_info=exc)
        raise RuntimeError(msg) from exc
    return {"ok": True, "bytes_sent": len(encoded)}


def _sanitize_value(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ").strip()


def build_payload(fields: Dict[str, str], copies: int = 1) -> str:
    """Construct key=value payload lines for NiceLabel."""
    sanitized = {key.upper(): _sanitize_value(str(value or "")) for key, value in fields.items()}
    lines = [f"COPIES={copies}"] + [f"{key}={value}" for key, value in sanitized.items()]
    return "\n".join(lines) + "\n"
