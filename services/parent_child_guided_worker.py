"""Phase 2 bounded Guided Mode worker.

Every invocation performs one persisted action.  Write transports are injected;
there is deliberately no default normal-write implementation in this module.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from services.db import get_db_connection
from services.parent_child_guided_orchestrator import (
    get_draft, get_job, next_action, save_draft, transition, update_operation, utcnow,
)
from services.parent_child_migrations import ensure_parent_child_schema
from services.parent_child_preview import active_variation_keys, build_preview
from services.parent_child_resolver import resolve_many
from services.parent_child_safety import checksum, full_schema_errors, patch_errors, redact
from services.spapi_catalog_relationships import catalog_lookup, current_parent
from services.spapi_listings import listing_item_attributes
from services.spapi_product_types import get_definition
from services.spapi_validation_preview import validation_preview_patch_listing, validation_preview_put_listing

BLOCKING = {"ERROR", "FATAL"}
ALLOWED_RELATIONSHIP_PATHS = {"parentage_level", "variation_theme", "child_parent_sku_relationship"}

def _now(): return datetime.now(timezone.utc)
def _safe(value): return redact(value)

def _runtime(job_id):
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM pc_guided_job_runtime WHERE job_id=?", (job_id,)).fetchone()
    result = dict(row) if row else {}
    for key in ("snapshot_json", "parent_ptd_json", "child_ptd_json"):
        if result.get(key): result[key[:-5]] = json.loads(result[key])
    return result

def _save_runtime(job_id, *, snapshot=None, parent_ptd=None, child_ptd=None, next_attempt=None, action=None, increment=False):
    ensure_parent_child_schema(); stamp = utcnow()
    with get_db_connection() as conn:
        conn.execute("INSERT INTO pc_guided_job_runtime(job_id,snapshot_json,parent_ptd_json,child_ptd_json,next_attempt_at_utc,poll_attempt_count,last_action,updated_at_utc) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET snapshot_json=COALESCE(excluded.snapshot_json,pc_guided_job_runtime.snapshot_json),parent_ptd_json=COALESCE(excluded.parent_ptd_json,pc_guided_job_runtime.parent_ptd_json),child_ptd_json=COALESCE(excluded.child_ptd_json,pc_guided_job_runtime.child_ptd_json),next_attempt_at_utc=excluded.next_attempt_at_utc,poll_attempt_count=pc_guided_job_runtime.poll_attempt_count+?,last_action=excluded.last_action,updated_at_utc=excluded.updated_at_utc", (job_id, json.dumps(_safe(snapshot), sort_keys=True) if snapshot is not None else None, json.dumps(_safe(parent_ptd), sort_keys=True) if parent_ptd is not None else None, json.dumps(_safe(child_ptd), sort_keys=True) if child_ptd is not None else None, next_attempt, int(increment), action, stamp, int(increment))); conn.commit()

def _due_after(job_id, action, seconds=30):
    runtime = _runtime(job_id); attempt = int(runtime.get("poll_attempt_count") or 0) + 1
    delay = min(900, seconds * (2 ** min(attempt, 5)))
    _save_runtime(job_id, next_attempt=(_now() + timedelta(seconds=delay)).isoformat(), action=action, increment=True)

def _adapters(overrides):
    # Parent/child normal writes require explicit injection.  This is a hard
    # Phase-2 guard against accidental live writes during local operation.
    base = {"resolve_many": resolve_many, "get_definition": get_definition, "build_preview": build_preview,
            "listing": listing_item_attributes, "catalog": catalog_lookup,
            "validate_parent": validation_preview_put_listing, "validate_child": validation_preview_patch_listing}
    base.update(overrides or {})
    return base

def production_adapters():
    """The only normal-write injection point; browser input never selects it."""
    from services.parent_child_guided_transports import child_patch, parent_put
    return {"parent_put": parent_put, "child_patch": child_patch}

def _issues_blocking(result): return any((item.get("severity") or "").upper() in BLOCKING for item in (result or {}).get("issues", []) if isinstance(item, dict))
def _validated(result): return isinstance(result, dict) and result.get("http_status", result.get("status", 200)) == 200 and result.get("classification") in ("PASSED", "PASSED_WITH_WARNINGS", "VALID") and not _issues_blocking(result)

def resolve_draft(draft_id, auth_client, adapters=None):
    """Read-only draft enrichment.  It never approves or writes to Amazon."""
    draft = get_draft(draft_id)
    if not draft: raise ValueError("Guided draft not found")
    family = dict(draft["family"]); api = _adapters(adapters)
    asins = [item.get("asin") for item in family.get("children", [])]
    if not asins or len(asins) != len(set(asins)) or any(not isinstance(asin, str) or len(asin.strip()) != 10 for asin in asins): raise ValueError("Children must contain unique valid ASINs")
    resolved = api["resolve_many"](auth_client, family["marketplace_id"], family["vendor_code"], asins)
    if any(item.get("status") != "Ready" for item in resolved): raise ValueError("One or more child ASINs need resolution")
    types = {item.get("product_type") for item in resolved}
    if len(types) != 1: raise ValueError("All children must share one product type")
    family["product_type"] = next(iter(types)); prior = {item["asin"]: item for item in family["children"]}
    family["children"] = [{**prior[item["asin"]], **{key: item.get(key) for key in ("asin", "vendor_sku", "title", "image_url", "product_type", "current_parent") if item.get(key) is not None}} for item in resolved]
    parent_schema, _ = api["get_definition"](auth_client, family["marketplace_id"], family["vendor_code"], family["product_type"], "PARENT")
    child_schema, _ = api["get_definition"](auth_client, family["marketplace_id"], family["vendor_code"], family["product_type"], "CHILD")
    themes = set()
    for schema in (parent_schema, child_schema):
        values = (schema.get("properties") or {}).get("variation_theme", {}).get("items", {}).get("properties", {}).get("name", {}).get("enum", [])
        themes = set(values) if not themes else themes & set(values)
    if family.get("variation_theme") not in themes: raise ValueError("Selected variation theme is not active in both PTDs")
    return save_draft(family, draft_id)

def _schema_accepts_array(schema):
    if schema.get("type") == "array": return True
    return any(isinstance(branch, dict) and _schema_accepts_array(branch) for branch in schema.get("anyOf", []) + schema.get("oneOf", []))

def _normalize_parent_values(parent_schema, values, marketplace_id):
    """Unbox only one PTD-valid localized wrapper before preview localizes it once."""
    normalized = {}
    for name, raw in values.items():
        node = (parent_schema.get("properties") or {}).get(name)
        if not isinstance(raw, list):
            normalized[name] = raw
            continue
        item = (node or {}).get("items") or {}
        properties = item.get("properties") or {}
        value_schema = properties.get("value") or {}
        if not node or not properties or "value" not in properties or _schema_accepts_array(value_schema):
            normalized[name] = raw
            continue
        if len(raw) != 1 or not isinstance(raw[0], dict): raise ValueError(f"Parent {name} must be one PTD-compatible localized value")
        wrapper = raw[0]
        if set(wrapper) - set(properties) or "value" not in wrapper: raise ValueError(f"Parent {name} has an invalid localized value shape")
        if "marketplace_id" in properties and wrapper.get("marketplace_id") != marketplace_id: raise ValueError(f"Parent {name} has a missing or mismatched marketplace context")
        if "language_tag" in properties:
            language = ((parent_schema.get("$defs") or {}).get("language_tag") or {}).get("default")
            if not language or wrapper.get("language_tag") != language: raise ValueError(f"Parent {name} has a missing or mismatched language context")
        wrapper_schema = {key: parent_schema[key] for key in ("$schema", "$id", "$defs", "definitions") if key in parent_schema}
        wrapper_schema.update(item)
        if full_schema_errors(wrapper_schema, wrapper, "Parent", layer="Parent localized value validation", validation_object=f"parent_required_values/{name}"):
            raise ValueError(f"Parent {name} is not PTD-compatible")
        normalized[name] = wrapper["value"]
    return normalized

def _local_validate(job, api, auth_client):
    family = job["family"]
    parent_schema, parent_trace = api["get_definition"](auth_client, family["marketplace_id"], family["vendor_code"], family["product_type"], "PARENT")
    child_schema, child_trace = api["get_definition"](auth_client, family["marketplace_id"], family["vendor_code"], family["product_type"], "CHILD")
    raw_values = {key: (value.get("value") if isinstance(value, dict) and "value" in value else value) for key, value in family["parent_required_values"].items()}
    values = _normalize_parent_values(parent_schema, raw_values, family["marketplace_id"])
    items = []
    dimensions = active_variation_keys(child_schema, family["variation_theme"])
    for child in family["children"]:
        supplied = child.get("variation_values") or ({"size": child["variation_value"]} if "size" in dimensions else {})
        inactive = set(supplied) - dimensions
        if inactive: raise ValueError("Inactive variation dimension: " + ", ".join(sorted(inactive)))
        items.append({"asin": child["asin"], "vendor_sku": child["vendor_sku"], "variation_values": supplied, "current_parent": child.get("current_parent")})
    preview = api["build_preview"]({"marketplace_id": family["marketplace_id"], "parent_sku": family["parent_sku"], "parent_title": family["parent_title"], "product_type": family["product_type"], "variation_theme": family["variation_theme"], "parent_values": values, "items": items}, parent_schema, child_schema, {})
    errors = full_schema_errors(parent_schema, preview["parent"]["body"]["attributes"], "Parent")
    for child in preview["children"]:
        names = {patch["path"].rsplit("/", 1)[-1] for patch in child["body"]["patches"]}
        if not names <= ALLOWED_RELATIONSHIP_PATHS | dimensions: errors.append({"scope": child["asin"], "message": "Unsupported child PATCH attribute"})
        errors.extend(patch_errors(child_schema, child["body"]["patches"], {}, child["asin"])[0])
    if errors: raise ValueError("Local PTD validation failed")
    snapshot = {"payload": preview, "payload_hash": checksum(preview), "parent_trace": _safe(parent_trace), "child_trace": _safe(child_trace)}
    _save_runtime(job["id"], snapshot=snapshot, parent_ptd=parent_schema, child_ptd=child_schema, action="LOCAL_VALIDATE")

def _cached_definition(_auth_client, marketplace_id, vendor_code, product_type, parentage_level):
    """Resume-local validation may use only the already cached authoritative PTD."""
    with get_db_connection() as conn:
        row = conn.execute("SELECT schema_json FROM pc_ptd_cache WHERE marketplace_id=? AND vendor_code=? AND product_type=? AND parentage_level=?", (marketplace_id, vendor_code, product_type, parentage_level.lower())).fetchone()
    if not row: raise ValueError(f"Cached {parentage_level.lower()} PTD is required before resume")
    try: schema = json.loads(row["schema_json"])
    except Exception as exc: raise ValueError(f"Cached {parentage_level.lower()} PTD is malformed") from exc
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict): raise ValueError(f"Cached {parentage_level.lower()} PTD is malformed")
    return schema, {"source": "cache", "parentage_level": parentage_level.lower()}

def resume_prewrite_local_validation(job_id, expected_family_hash):
    """Resume exactly one pre-write LOCAL validation failure without a network transport."""
    job = get_job(job_id)
    if not job: raise ValueError("Guided job not found")
    if job["family_hash"] != expected_family_hash: raise ValueError("Guided resume hash does not match the immutable job")
    if job["state"] != "NEEDS_ATTENTION" or (job.get("blocking_issue") or {}).get("message") != "Local PTD validation failed": raise ValueError("Only a local-validation NEEDS_ATTENTION job may resume")
    if any(item["state"] != "PREPARED" or item["attempt_count"] or item.get("request_id") or item.get("http_status") or item.get("submitting_at_utc") or item.get("completed_at_utc") or item.get("verified_at_utc") for item in job["operations"]): raise ValueError("Guided resume requires every operation to be conclusively pre-write")
    with get_db_connection() as conn:
        runtime = conn.execute("SELECT 1 FROM pc_guided_job_runtime WHERE job_id=?", (job_id,)).fetchone()
        lease = conn.execute("SELECT 1 FROM pc_guided_worker_leases WHERE job_id=?", (job_id,)).fetchone()
        evidence = conn.execute("SELECT 1 FROM pc_guided_validation_evidence WHERE job_id=?", (job_id,)).fetchone()
    if runtime or lease or evidence: raise ValueError("Guided resume requires no runtime, lease, or validation evidence")
    _local_validate(job, {"get_definition": _cached_definition, "build_preview": build_preview}, None)
    transition(job_id, "LOCAL_VALIDATING", {"message": "Local PTD validation resumed from the immutable pre-write job"})
    return transition(job_id, "AMAZON_VALIDATING", {"message": "Cached local PTD validation passed; awaiting existing validation gates"})

def _evidence(job_id, key, payload_hash, result):
    ensure_parent_child_schema(); stamp = _now(); expires = stamp + timedelta(minutes=30)
    with get_db_connection() as conn:
        conn.execute("INSERT INTO pc_guided_validation_evidence(id,job_id,operation_key,payload_hash,classification,evidence_json,completed_at_utc,expires_at_utc) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id,operation_key,payload_hash) DO UPDATE SET classification=excluded.classification,evidence_json=excluded.evidence_json,completed_at_utc=excluded.completed_at_utc,expires_at_utc=excluded.expires_at_utc", (str(uuid.uuid4()), job_id, key, payload_hash, result.get("classification") or "UNKNOWN", json.dumps(_safe(result)), stamp.isoformat(), expires.isoformat())); conn.commit()

def _verification_auth(auth_client):
    """Fail closed before a reconciliation GET when the configured client is absent."""
    if auth_client is None:
        raise ValueError("Configured auth client is required for Guided Listings GET verification")
    return auth_client

def _verify_parent(job, api, auth_client):
    family = job["family"]
    row = api["listing"](_verification_auth(auth_client), family["marketplace_id"], family["vendor_code"], family["parent_sku"])
    attributes = row.get("attributes") or {}; parentage = attributes.get("parentage_level") or []
    theme = attributes.get("variation_theme") or []
    # requested_sku merely records what we asked for.  api_sku is the identity
    # Amazon actually returned, and must match exactly before children unlock.
    ok = row.get("status") == 200 and row.get("api_sku") == family["parent_sku"] and row.get("product_type") == family["product_type"] and (any(x.get("value") == "parent" for x in parentage if isinstance(x, dict)) or row.get("parent_verified") is True) and (any(x.get("name") == family["variation_theme"] for x in theme if isinstance(x, dict)) or row.get("theme_verified") is True) and not _issues_blocking(row)
    return ok, _safe(row)

def _verified_parent_asins(job_id):
    """Return only the parent ASINs proved by the durable parent GET audit."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT evidence_json FROM pc_guided_events WHERE job_id=? ORDER BY created_at_utc DESC", (job_id,)).fetchall()
    for row in rows:
        try: evidence = json.loads(row["evidence_json"] or "{}").get("parent_get") or {}
        except (TypeError, ValueError): continue
        asins = evidence.get("api_asins")
        if isinstance(asins, list) and asins and all(isinstance(asin, str) and asin.strip() for asin in asins):
            return set(asins)
    return set()

def _listing_child_parent(row):
    relationships = (row.get("attributes") or {}).get("child_parent_sku_relationship") or []
    if not isinstance(relationships, list): raise ValueError("Child Listings relationship is malformed")
    parents = {item.get("parent_sku") for item in relationships if isinstance(item, dict) and isinstance(item.get("parent_sku"), str) and item.get("parent_sku").strip()}
    if len(parents) > 1: raise ValueError("Child Listings relationship is ambiguous")
    return next(iter(parents), None)

def _verify_child(job, child, api, auth_client):
    family = job["family"]
    auth = _verification_auth(auth_client)
    row = api["listing"](auth, family["marketplace_id"], family["vendor_code"], child["vendor_sku"])
    if not isinstance(row, dict) or row.get("status") != 200 or row.get("api_sku") != child["vendor_sku"] or row.get("product_type") != family["product_type"] or _issues_blocking(row):
        raise ValueError("Child Listings GET identity or issue response is ambiguous")
    listing_parent = _listing_child_parent(row) or row.get("current_parent")
    if listing_parent is not None:
        return listing_parent == family["parent_sku"] or row.get("attached_to_parent") is True, listing_parent, _safe(row)
    catalog, catalog_details = api["catalog"](auth, family["marketplace_id"], child["child_asin"])
    if not isinstance(catalog_details, dict) or catalog_details.get("http_status") != 200 or not isinstance(catalog, dict):
        raise ValueError("Child Catalog GET response is ambiguous")
    catalog_parent = current_parent(catalog)
    if catalog_parent is None:
        return False, None, _safe({"listing": row, "catalog": {"http_status": catalog_details.get("http_status"), "request_id": catalog_details.get("request_id")}})
    parent_asins = _verified_parent_asins(job["id"])
    if not parent_asins:
        raise ValueError("Verified parent ASIN is required before Catalog child reconciliation")
    return catalog_parent in parent_asins, catalog_parent, _safe({"listing": row, "catalog": {"http_status": catalog_details.get("http_status"), "request_id": catalog_details.get("request_id"), "parent_asin": catalog_parent}})

def process_next_safe_action(job_id, auth_client=None, adapters=None):
    """Run exactly one safe stage and return; no loop and no implicit normal write."""
    job = get_job(job_id)
    if not job: raise ValueError("Guided job not found")
    if job["state"] == "CANCELLED_BEFORE_WRITES": raise ValueError("Cancelled guided jobs cannot be processed")
    api = _adapters(adapters); action = next_action(job_id)
    try:
        if action == "LOCAL_VALIDATE":
            _local_validate(job, api, auth_client); transition(job_id, "LOCAL_VALIDATING"); transition(job_id, "AMAZON_VALIDATING")
        elif action == "AMAZON_VALIDATE":
            runtime = _runtime(job_id); payload = runtime["snapshot"]["payload"]; parent = api["validate_parent"](auth_client, job["family"]["vendor_code"], job["family"]["parent_sku"], job["family"]["marketplace_id"], payload["parent"]["body"])
            _evidence(job_id, "parent", runtime["snapshot"]["payload_hash"], parent)
            if not _validated(parent): raise ValueError("Amazon validation preview rejected parent")
            for child in payload["children"]:
                result = api["validate_child"](auth_client, job["family"]["vendor_code"], child["sku"], job["family"]["marketplace_id"], child["body"]); _evidence(job_id, "child:" + child["asin"], runtime["snapshot"]["payload_hash"], result)
                if not _validated(result): raise ValueError("Amazon validation preview rejected child")
            transition(job_id, "CREATING_PARENT")
        elif action == "VALIDATE_PARENT":
            # Fresh GET-only preflight, then an explicitly injected, one-shot PUT.
            parent = api["listing"](auth_client, job["family"]["marketplace_id"], job["family"]["vendor_code"], job["family"]["parent_sku"])
            if parent.get("status") not in (404, "AVAILABLE"): raise ValueError("Parent SKU is not available")
            if "parent_put" not in api: raise ValueError("No approved parent write transport is configured")
            runtime = _runtime(job_id); update_operation(job_id, "parent", "SUBMITTING")
            result = api["parent_put"](auth_client, job["family"], runtime["snapshot"]["payload"]["parent"]["body"], next(item["idempotency_key"] for item in get_job(job_id)["operations"] if item["operation_key"] == "parent"))
            if result.get("http_status") in (200, 202) and not _issues_blocking(result): update_operation(job_id, "parent", "ACCEPTED", result.get("request_id"), result.get("http_status"), result.get("issues"))
            else: update_operation(job_id, "parent", "UNKNOWN", result.get("request_id"), result.get("http_status"), result.get("issues")); transition(job_id, "SUBMISSION_UNKNOWN"); return {"job": status(job_id), "action": action}
            transition(job_id, "WAITING_FOR_PARENT")
        elif action == "RECONCILE_PARENT_GET" or action == "VERIFY_PARENT_GET":
            verified, evidence = _verify_parent(job, api, auth_client)
            if verified: update_operation(job_id, "parent", "VERIFIED"); transition(job_id, "VALIDATING_CHILDREN", {"parent_get": evidence})
            else: _due_after(job_id, action)
        elif action == "VALIDATE_CHILDREN":
            transition(job_id, "ATTACHING_CHILDREN")
        elif action == "ATTACH_CHILD":
            current = get_job(job_id); child = next(item for item in current["operations"] if item["operation_type"] == "PATCH" and item["state"] == "PREPARED")
            attached, existing_parent, _ = _verify_child(current, child, api, auth_client)
            if attached: update_operation(job_id, child["operation_key"], "VERIFIED")
            elif existing_parent not in (None, "", "Unknown"): raise ValueError("Child is attached to a different parent")
            else:
                _child_write_authorized(current, child)
                if "child_patch" not in api: raise ValueError("No approved child write transport is configured")
                payload = _runtime(job_id)["snapshot"]["payload"]; body = next(item["body"] for item in payload["children"] if item["asin"] == child["child_asin"])
                update_operation(job_id, child["operation_key"], "SUBMITTING")
                result = api["child_patch"](auth_client, current["family"], child["vendor_sku"], body, child["idempotency_key"])
                if result.get("http_status") in (200, 202) and not _issues_blocking(result): update_operation(job_id, child["operation_key"], "ACCEPTED", result.get("request_id"), result.get("http_status"), result.get("issues"))
                elif result.get("http_status") in (400, 422): update_operation(job_id, child["operation_key"], "REJECTED", result.get("request_id"), result.get("http_status"), result.get("issues")); transition(job_id, "PARTIAL_COMPLETION")
                else: update_operation(job_id, child["operation_key"], "UNKNOWN", result.get("request_id"), result.get("http_status"), result.get("issues")); transition(job_id, "SUBMISSION_UNKNOWN")
        elif action in ("RECONCILE_CHILD_GET", "VERIFY_CHILD_GET"):
            child = next(item for item in get_job(job_id)["operations"] if item["state"] in ("SUBMITTING", "UNKNOWN", "ACCEPTED"))
            verified, _, _ = _verify_child(job, child, api, auth_client)
            if verified: update_operation(job_id, child["operation_key"], "VERIFIED")
            else: _due_after(job_id, action)
        elif action == "COMPLETE": transition(job_id, "VERIFYING_FAMILY"); transition(job_id, "COMPLETED")
        elif action in ("NEEDS_ATTENTION", "PARTIAL_COMPLETION"): return {"job": status(job_id), "action": action}
        else: return {"job": status(job_id), "action": action}
    except Exception as exc:
        current = get_job(job_id)
        if current and current["state"] not in ("NEEDS_ATTENTION", "COMPLETED", "CANCELLED_BEFORE_WRITES"):
            try: transition(job_id, "NEEDS_ATTENTION", {"message": str(exc)})
            except ValueError: pass
    return {"job": status(job_id), "action": action}

def start(job_id, auth_client=None, adapters=None): return process_next_safe_action(job_id, auth_client, adapters)
def wake(job_id, auth_client=None, adapters=None): return process_next_safe_action(job_id, auth_client, adapters)

def reconcile_accepted_parent_get(job_id, expected_family_hash, auth_client, adapters=None):
    """Perform one guarded Listings GET for an already-accepted parent only.

    This route intentionally has no planner, validation-preview, or write path.
    It exists solely to reconcile a prior accepted parent submission after an
    infrastructure failure at the GET verification boundary.
    """
    job = get_job(job_id)
    if not job: raise ValueError("Guided job not found")
    if job["family_hash"] != expected_family_hash: raise ValueError("Guided reconciliation hash does not match the immutable job")
    if job["state"] != "NEEDS_ATTENTION": raise ValueError("Only a blocked guided job may use guarded parent reconciliation")
    if (job.get("blocking_issue") or {}).get("message") != "'NoneType' object has no attribute 'get_lwa_access_token'":
        raise ValueError("Guarded parent reconciliation is limited to the missing-auth verification failure")
    parent = next(item for item in job["operations"] if item["operation_key"] == "parent")
    children = [item for item in job["operations"] if item["operation_type"] == "PATCH"]
    if parent["state"] != "ACCEPTED" or parent["attempt_count"] != 1:
        raise ValueError("Guarded reconciliation requires one accepted parent submission")
    if any(item["state"] != "PREPARED" or item["attempt_count"] for item in children):
        raise ValueError("Guarded reconciliation requires every child to remain conclusively pre-write")
    verified, evidence = _verify_parent(job, _adapters(adapters), auth_client)
    if not verified:
        raise ValueError("Parent Listings GET verification is ambiguous; no child is eligible")
    update_operation(job_id, "parent", "VERIFIED")
    return transition(job_id, "VALIDATING_CHILDREN", {"parent_get": evidence, "message": "Accepted parent verified by guarded Listings GET"})

def reconcile_prepared_children_get(job_id, expected_family_hash, auth_client, adapters=None):
    """Guarded child reconciliation only; it never has a PATCH path.

    All child GET responses are evaluated before any ledger change, so a wrong
    parent or malformed identity cannot partially reconcile the family.
    """
    job = get_job(job_id)
    if not job: raise ValueError("Guided job not found")
    if job["family_hash"] != expected_family_hash: raise ValueError("Guided child reconciliation hash does not match the immutable job")
    if job["state"] != "NEEDS_ATTENTION" or (job.get("blocking_issue") or {}).get("message") != "'NoneType' object has no attribute 'get_lwa_access_token'":
        raise ValueError("Guarded child reconciliation is limited to the missing-auth failure")
    parent = next(item for item in job["operations"] if item["operation_key"] == "parent")
    children = [item for item in job["operations"] if item["operation_type"] == "PATCH"]
    if parent["state"] != "VERIFIED" or parent["attempt_count"] != 1:
        raise ValueError("Guarded child reconciliation requires the original verified one-attempt parent")
    if any(item["state"] != "PREPARED" or item["attempt_count"] or item.get("request_id") or item.get("http_status") for item in children):
        raise ValueError("Guarded child reconciliation requires every child to be conclusively pre-write")
    with get_db_connection() as conn:
        if conn.execute("SELECT 1 FROM pc_guided_worker_leases WHERE job_id=?", (job_id,)).fetchone():
            raise ValueError("Guarded child reconciliation requires no runtime lease")
    api = _adapters(adapters); checked = []
    for child in children:
        attached, observed_parent, evidence = _verify_child(job, child, api, auth_client)
        if observed_parent not in (None, "", "Unknown") and not attached:
            raise ValueError("Child is attached to a different parent")
        checked.append((child, attached, evidence))
    for child, attached, _ in checked:
        if attached: update_operation(job_id, child["operation_key"], "VERIFIED")
    return {"job": get_job(job_id), "children": [{"vendor_sku": child["vendor_sku"], "attached": attached, "evidence": evidence} for child, attached, evidence in checked], "next_safe_action": "EXPLICIT_CHILD_WRITE_AUTHORIZATION_REQUIRED"}

def _child_write_authorized(job, child):
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE"); row=conn.execute("SELECT * FROM pc_guided_child_write_authorizations WHERE job_id=?", (job["id"],)).fetchone()
        consumed=json.loads(row["consumed_operations_json"]) if row else []
        if not row or not row["confirmed_by"] or row["family_hash"] != job["family_hash"] or row["expires_at_utc"] <= utcnow() or child["operation_key"] in consumed: conn.rollback(); raise ValueError("Explicit unexpired child write authorization is required")
        consumed.append(child["operation_key"]); conn.execute("UPDATE pc_guided_child_write_authorizations SET consumed_operations_json=? WHERE id=?",(json.dumps(consumed),row["id"])); conn.commit()
    return row

def prepare_child_write_authorization(job_id, family_hash, auth_client, adapters=None):
    """GET-only preparation. Browser receives this exact immutable plan."""
    result = reconcile_prepared_children_get(job_id, family_hash, auth_client, adapters)
    job = get_job(job_id); plan = [{"operation_key": x["operation_key"], "asin": x["child_asin"], "vendor_sku": x["vendor_sku"], "payload_hash": x["payload_hash"]} for x in job["operations"] if x["operation_type"] == "PATCH"]
    plan_hash = checksum(plan); expiry = (_now() + timedelta(minutes=5)).isoformat()
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO pc_guided_child_write_authorizations(id,job_id,family_hash,plan_hash,child_evidence_json,expires_at_utc,confirmed_at_utc,confirmed_by,consumed_operations_json) VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()),job_id,family_hash,plan_hash,json.dumps(_safe(result["children"])),expiry,utcnow(),None,"[]")); conn.commit()
    return {"job_id":job_id,"family_hash":family_hash,"plan":plan,"plan_hash":plan_hash,"expires_at_utc":expiry}

def confirm_child_write_authorization(job_id, family_hash, plan_hash, confirmed_by=None):
    with get_db_connection() as conn:
        row=conn.execute("SELECT * FROM pc_guided_child_write_authorizations WHERE job_id=?",(job_id,)).fetchone()
        if not row or row["family_hash"] != family_hash or row["plan_hash"] != plan_hash or row["expires_at_utc"] <= utcnow(): raise ValueError("Fresh exact child write evidence is required")
        if row["confirmed_by"] is not None: raise ValueError("Child write authorization was already confirmed")
        conn.execute("UPDATE pc_guided_child_write_authorizations SET confirmed_by=?,confirmed_at_utc=? WHERE id=?",(confirmed_by or "user",utcnow(),row["id"])); conn.commit()
    return {"job_id":job_id,"family_hash":family_hash,"plan_hash":plan_hash,"confirmed":True}

def cancel(job_id, reason="Cancelled before writes"):
    job = get_job(job_id)
    if not job: raise ValueError("Guided job not found")
    if any(item["state"] != "PREPARED" or item["attempt_count"] or item.get("request_id") or item.get("http_status") or item.get("submitting_at_utc") or item.get("completed_at_utc") or item.get("verified_at_utc") for item in job["operations"]): raise ValueError("A guided job cannot be cancelled unless every operation is conclusively pre-write")
    with get_db_connection() as conn:
        runtime = conn.execute("SELECT 1 FROM pc_guided_job_runtime WHERE job_id=?", (job_id,)).fetchone()
        lease = conn.execute("SELECT 1 FROM pc_guided_worker_leases WHERE job_id=?", (job_id,)).fetchone()
        evidence = conn.execute("SELECT 1 FROM pc_guided_validation_evidence WHERE job_id=?", (job_id,)).fetchone()
    if runtime or lease or evidence: raise ValueError("A guided job cannot be cancelled while runtime, lease, or validation evidence makes its write history ambiguous")
    return transition(job_id, "CANCELLED_BEFORE_WRITES", {"reason": reason})

def eligible_jobs():
    ensure_parent_child_schema(); now = utcnow()
    with get_db_connection() as conn:
        return [row[0] for row in conn.execute("SELECT j.id FROM pc_guided_jobs j LEFT JOIN pc_guided_job_runtime r ON r.job_id=j.id WHERE j.state NOT IN ('COMPLETED','CANCELLED_BEFORE_WRITES') AND (r.next_attempt_at_utc IS NULL OR r.next_attempt_at_utc<=?)", (now,))]

def claim_job(job_id, owner_id, lease_seconds=60):
    """Database lease prevents two desktop instances from processing one job."""
    ensure_parent_child_schema(); stamp = _now(); expiry = stamp + timedelta(seconds=lease_seconds)
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT owner_id,expires_at_utc FROM pc_guided_worker_leases WHERE job_id=?", (job_id,)).fetchone()
        if row and row["owner_id"] != owner_id and row["expires_at_utc"] > stamp.isoformat(): conn.rollback(); return False
        conn.execute("INSERT INTO pc_guided_worker_leases(job_id,owner_id,expires_at_utc,updated_at_utc) VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET owner_id=excluded.owner_id,expires_at_utc=excluded.expires_at_utc,updated_at_utc=excluded.updated_at_utc", (job_id, owner_id, expiry.isoformat(), stamp.isoformat())); conn.commit()
    return True

def release_job(job_id, owner_id):
    ensure_parent_child_schema()
    with get_db_connection() as conn: conn.execute("DELETE FROM pc_guided_worker_leases WHERE job_id=? AND owner_id=?", (job_id, owner_id)); conn.commit()

def scheduler_tick(auth_client=None, adapters=None, owner_id=None):
    """One bounded pass: each eligible job receives at most one action."""
    owner_id = owner_id or "guided-" + str(uuid.uuid4()); results = []
    for job_id in eligible_jobs():
        if not claim_job(job_id, owner_id): continue
        try: results.append(process_next_safe_action(job_id, auth_client, adapters))
        finally: release_job(job_id, owner_id)
    return results

def status(job_id):
    job = get_job(job_id)
    if not job: raise ValueError("Guided job not found")
    runtime = _runtime(job_id)
    return {"job_id": job_id, "draft_id": job["draft_id"], "family_hash": job["family_hash"], "stage": job["state"], "next_action": next_action(job_id), "parent_sku": job["family"]["parent_sku"], "product_type": job["family"]["product_type"], "variation_theme": job["family"]["variation_theme"], "children": [{"asin": x["asin"], "vendor_sku": x["vendor_sku"], "title": x.get("title"), "image_url": x.get("image_url")} for x in job["family"]["children"]], "operations": [{key: item.get(key) for key in ("operation_key", "operation_type", "child_asin", "vendor_sku", "state", "attempt_count", "request_id", "http_status", "prepared_at_utc", "submitting_at_utc", "completed_at_utc", "verified_at_utc")} for item in job["operations"]], "next_attempt_at_utc": runtime.get("next_attempt_at_utc"), "message": "Guided job requires attention" if job["state"] == "NEEDS_ATTENTION" else None}
