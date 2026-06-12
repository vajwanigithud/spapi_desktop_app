from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import requests

from services.activity_log import add_activity
from services.env_manager import SECRET_KEYS, read_managed_values

LOGGER = logging.getLogger(__name__)

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
_HEALTH = {
    "status": "Not Tested",
    "last_token_test_time": None,
    "last_vendor_po_test_time": None,
    "last_redacted_error": "",
}


def get_token_health() -> Dict[str, object]:
    return dict(_HEALTH)


def test_lwa_token(values: Dict[str, str]) -> Dict[str, object]:
    tested_at = _now_iso()
    add_activity("API", "Credentials: LWA token test started")
    try:
        token_payload = request_lwa_access_token(values)
        expires_in = int(token_payload.get("expires_in") or 0)
        _update_health(status="OK", token_time=tested_at, error="")
        LOGGER.info("[Credentials] LWA token test succeeded")
        add_activity("OK", "Credentials: LWA token test completed")
        return {"ok": True, "status": "OK", "tested_at": tested_at, "expires_in": expires_in}
    except Exception as exc:
        error = redact_text(str(exc), values)
        _update_health(status="Error", token_time=tested_at, error=error)
        LOGGER.warning("[Credentials] LWA token test failed: %s", error)
        add_activity("ERROR", f"Credentials: LWA token test failed: {error}")
        return {"ok": False, "status": "Error", "tested_at": tested_at, "error": error}


def test_vendor_po_access(fetcher: Callable[[], object]) -> Dict[str, object]:
    tested_at = _now_iso()
    add_activity("API", "Credentials: Vendor PO access test started")
    try:
        fetcher()
        _update_health(status="OK", vendor_time=tested_at, error="")
        LOGGER.info("[Credentials] Vendor PO access test succeeded")
        add_activity("OK", "Credentials: Vendor PO access test completed")
        return {
            "ok": True,
            "status": "OK",
            "tested_at": tested_at,
        }
    except Exception as exc:
        error = redact_text(str(exc))
        _update_health(status="Error", vendor_time=tested_at, error=error)
        LOGGER.warning("[Credentials] Vendor PO access test failed: %s", error)
        add_activity("ERROR", f"Credentials: Vendor PO access test failed: {error}")
        return {"ok": False, "status": "Error", "tested_at": tested_at, "error": error}


def request_lwa_access_token(values: Dict[str, str]) -> Dict[str, Any]:
    missing = [
        key
        for key in ("LWA_CLIENT_ID", "LWA_CLIENT_SECRET", "LWA_REFRESH_TOKEN")
        if not values.get(key)
    ]
    if missing:
        raise RuntimeError(f"Missing required LWA settings: {', '.join(missing)}")

    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": values["LWA_REFRESH_TOKEN"],
            "client_id": values["LWA_CLIENT_ID"],
            "client_secret": values["LWA_CLIENT_SECRET"],
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"http_{resp.status_code} LWA token request failed {safe_response_text(resp, values)}")
    return resp.json()


def safe_response_text(resp: requests.Response, values: Optional[Dict[str, str]] = None) -> str:
    text = (resp.text or "").strip()
    if len(text) > 500:
        text = text[:500] + "..."
    return redact_text(text, values)


def redact_text(text: str, values: Optional[Dict[str, str]] = None) -> str:
    redacted = str(text or "")
    candidate_values = values if values is not None else read_managed_values()
    candidates = []
    for key in SECRET_KEYS:
        value = candidate_values.get(key)
        if value:
            candidates.append(value)
    for value in candidates:
        redacted = redacted.replace(value, "[REDACTED]")
    redacted = re.sub(r"amzn1\.[A-Za-z0-9._\-]+", "[REDACTED]", redacted)
    redacted = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{20,}",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r'(?i)("?(?:access_token|refresh_token|client_secret|secret|password)"?\s*[:=]\s*")([^"]+)(")',
        r"\1[REDACTED]\3",
        redacted,
    )
    redacted = re.sub(
        r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40,}(?![A-Za-z0-9/+=])",
        "[REDACTED]",
        redacted,
    )
    return redacted


def _update_health(
    *,
    status: str,
    token_time: Optional[str] = None,
    vendor_time: Optional[str] = None,
    error: str = "",
) -> None:
    _HEALTH["status"] = status
    if token_time:
        _HEALTH["last_token_test_time"] = token_time
    if vendor_time:
        _HEALTH["last_vendor_po_test_time"] = vendor_time
    _HEALTH["last_redacted_error"] = error


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
