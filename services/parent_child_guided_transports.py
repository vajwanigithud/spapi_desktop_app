"""Production composition for Guided Mode's exact ledger-owned operations only."""
import requests
from services.parent_child_safety import redact

_BLOCKED_SKUS = {"Princess_Plate", "DI-113", "DI-114"}

def _send(auth, family, sku, body, key, method):
    if sku in _BLOCKED_SKUS: raise ValueError("Completed production family is permanently out of Guided Mode scope")
    response = requests.request(method, f"https://sellingpartnerapi-eu.amazon.com/listings/2021-08-01/items/{family['vendor_code']}/{sku}", params={"marketplaceIds": family["marketplace_id"], "issueLocale": "en_AE"}, json=body, headers={"x-amz-access-token": auth.get_lwa_access_token(), "x-amz-client-request-id": key}, timeout=20)
    try: payload = response.json()
    except ValueError: payload = {}
    return redact({"http_status": response.status_code, "request_id": response.headers.get("x-amzn-RequestId") or response.headers.get("x-amz-request-id"), "issues": payload.get("issues", []) if isinstance(payload, dict) else [], "submission_status": payload.get("status") if isinstance(payload, dict) else None})

def parent_put(auth, family, body, key): return _send(auth, family, family["parent_sku"], body, key, "PUT")
def child_patch(auth, family, sku, body, key): return _send(auth, family, sku, body, key, "PATCH")
