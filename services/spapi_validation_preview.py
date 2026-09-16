"""Amazon Listings validation preview transport only; never a persistent write API."""
import random
import time

import requests

UAE_MARKETPLACE = "A2VIGQ35RCS4UG"
EU_HOST = "https://sellingpartnerapi-eu.amazon.com"
MODE = "VALIDATION_PREVIEW"
RETRYABLE = {429, 500, 503}


def _params(marketplace_id):
    if marketplace_id != UAE_MARKETPLACE:
        raise ValueError("Validation preview is bound to the configured UAE marketplace")
    # The mode is intentionally private and cannot be overridden by callers.
    return {"marketplaceIds": UAE_MARKETPLACE, "mode": MODE, "issueLocale": "en_AE", "includedData": "issues,identifiers"}


def _safe_issues(payload):
    issues = payload.get("issues") or [] if isinstance(payload, dict) else []
    return [{"severity": item.get("severity"), "code": item.get("code"), "message": item.get("message"),
             "attribute_names": item.get("attributeNames") or item.get("attribute_names") or []} for item in issues]


def _classify(status, payload):
    issues = _safe_issues(payload)
    if status == 403: return "AUTHORIZATION_DENIED", issues
    if status == 429: return "RATE_LIMITED", issues
    if status in (500, 503): return "TEMPORARY_FAILURE", issues
    if status < 200 or status >= 300: return "UNKNOWN", issues
    if any((x.get("severity") or "").upper() in ("ERROR", "FATAL") for x in issues): return "BLOCKED_BY_AMAZON_ISSUES", issues
    if issues: return "PASSED_WITH_WARNINGS", issues
    # Amazon may return a submission status that is not accepted despite HTTP 200.
    submission = payload.get("status") or payload.get("submissionStatus") if isinstance(payload, dict) else None
    if submission and str(submission).upper() not in ("ACCEPTED", "VALID", "SUCCESS", "SUCCESSFUL"): return "UNKNOWN", issues
    return "PASSED", issues


def _call(method, auth_client, seller_id, sku, marketplace_id, body):
    if not seller_id: raise ValueError("A server-resolved verified vendor code is required")
    url = f"{EU_HOST}/listings/2021-08-01/items/{seller_id}/{sku}"
    attempted = time.time(); last_error = None
    for attempt in range(3):
        try:
            response = method(url, params=_params(marketplace_id), json=body,
                              headers={"x-amz-access-token": auth_client.get_lwa_access_token()}, timeout=20)
            try: payload = response.json()
            except ValueError: payload = {}
            if response.status_code not in RETRYABLE or attempt == 2: break
            time.sleep(min(2, .2 * (2 ** attempt) + random.random() / 10))
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 2: return {"http_status": None, "classification": "TEMPORARY_FAILURE", "issues": [], "request_id": None, "rate_limit": None, "submission_status": None, "attempted_at": attempted, "completed_at": time.time()}
            time.sleep(min(2, .2 * (2 ** attempt) + random.random() / 10))
    classification, issues = _classify(response.status_code, payload)
    return {"http_status": response.status_code, "classification": classification, "issues": issues,
            "request_id": response.headers.get("x-amzn-RequestId") or response.headers.get("x-amz-request-id"),
            "rate_limit": response.headers.get("x-amzn-RateLimit-Limit"), "submission_status": payload.get("status") or payload.get("submissionStatus"),
            "attempted_at": attempted, "completed_at": time.time()}


def validation_preview_put_listing(auth_client, seller_id, sku, marketplace_id, body):
    return _call(requests.put, auth_client, seller_id, sku, marketplace_id, body)


def validation_preview_patch_listing(auth_client, seller_id, sku, marketplace_id, body):
    return _call(requests.patch, auth_client, seller_id, sku, marketplace_id, body)
