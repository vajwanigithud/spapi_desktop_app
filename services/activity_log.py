from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Deque, Dict, List

from services.env_manager import SECRET_KEYS, read_managed_values

MAX_ACTIVITY_EVENTS = 500
VALID_LEVELS = {"INFO", "OK", "WARN", "ERROR", "API", "DB", "BG"}

_EVENTS: Deque[Dict[str, str]] = deque(maxlen=MAX_ACTIVITY_EVENTS)
_LOCK = Lock()


def add_activity(level: str, message: str) -> Dict[str, str]:
    normalized_level = (level or "INFO").upper()
    if normalized_level not in VALID_LEVELS:
        normalized_level = "INFO"
    event = {
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "level": normalized_level,
        "message": redact_activity_text(message),
    }
    with _LOCK:
        _EVENTS.append(event)
    return event


def get_activity_events(limit: int = MAX_ACTIVITY_EVENTS) -> List[Dict[str, str]]:
    try:
        clean_limit = max(1, min(int(limit), MAX_ACTIVITY_EVENTS))
    except Exception:
        clean_limit = MAX_ACTIVITY_EVENTS
    with _LOCK:
        return list(_EVENTS)[-clean_limit:]


def redact_activity_text(text: object) -> str:
    redacted = str(text or "")
    try:
        values = read_managed_values()
    except Exception:
        values = {}
    for key in SECRET_KEYS:
        value = values.get(key)
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    redacted = re.sub(r"amzn1\.[A-Za-z0-9._\-]+", "[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{20,}", r"\1[REDACTED]", redacted)
    redacted = re.sub(
        r'(?i)("?(?:access_token|refresh_token|client_secret|secret|password)"?\s*[:=]\s*")([^"]+)(")',
        r"\1[REDACTED]\3",
        redacted,
    )
    redacted = re.sub(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40,}(?![A-Za-z0-9/+=])", "[REDACTED]", redacted)
    return redacted
