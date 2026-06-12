from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from services.credential_test_service import (
    get_token_health,
    test_lwa_token,
    test_vendor_po_access,
)
from services.env_manager import MANAGED_ENV_KEYS, read_managed_values, reload_into_environ, save_managed_values

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/credentials")


class CredentialSettingsPayload(BaseModel):
    values: Dict[str, str] = Field(default_factory=dict)


def _managed_payload() -> Dict[str, object]:
    # This intentionally returns raw .env values to the browser because the app is a
    # localhost-only desktop UI. Do not expose these routes on a public interface.
    values = read_managed_values()
    return {
        "ok": True,
        "values": values,
        "managed_keys": MANAGED_ENV_KEYS,
        "health": get_token_health(),
    }


@router.get("")
def read_credentials() -> Dict[str, object]:
    return _managed_payload()


@router.post("")
def save_credentials(payload: CredentialSettingsPayload) -> Dict[str, object]:
    clean_values = {key: str(payload.values.get(key, "") or "") for key in MANAGED_ENV_KEYS}
    try:
        result = save_managed_values(clean_values)
        reload_into_environ()
        runtime_reload = getattr(router, "_runtime_reload", None)
        if callable(runtime_reload):
            runtime_reload()
        return {
            **_managed_payload(),
            "backup_path": str(result.backup_path),
            "env_path": str(result.env_path),
        }
    except Exception as exc:
        logger.warning("[Credentials] Failed to save .env: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save .env") from exc


@router.post("/reload")
def reload_credentials() -> Dict[str, object]:
    try:
        reload_into_environ()
        runtime_reload = getattr(router, "_runtime_reload", None)
        if callable(runtime_reload):
            runtime_reload()
        return _managed_payload()
    except Exception as exc:
        logger.warning("[Credentials] Failed to reload .env: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to reload .env") from exc


@router.post("/test-lwa")
def test_lwa() -> Dict[str, object]:
    return test_lwa_token(read_managed_values())


@router.post("/test-vendor-po")
def test_vendor_po() -> Dict[str, object]:
    vendor_po_tester = getattr(router, "_vendor_po_tester", None)
    if not callable(vendor_po_tester):
        raise HTTPException(status_code=500, detail="Vendor PO tester is not configured")
    return test_vendor_po_access(vendor_po_tester)


def register_credentials_routes(
    app: FastAPI,
    runtime_reload: Callable[[], None],
    vendor_po_tester: Optional[Callable[[], object]] = None,
) -> None:
    setattr(router, "_runtime_reload", runtime_reload)
    setattr(router, "_vendor_po_tester", vendor_po_tester)
    app.include_router(router)
