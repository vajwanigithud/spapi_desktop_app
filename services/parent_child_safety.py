"""Server-authoritative, read-only approval safety primitives.

This module deliberately has no Amazon write transport.  It creates immutable
local snapshots and GET-only preflight evidence for a later, separately
approved submission phase.
"""
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from jsonschema import validators

from services.db import get_db_connection, get_readonly_db_connection
from services.parent_child_migrations import ensure_parent_child_schema

SENSITIVE_WORDS = ("token", "authorization", "credential", "secret", "signature", "x-amz")
ACTIVE_JOB_STATUSES = (
    "PREPARED", "PARENT_SUBMITTING", "PARENT_ACCEPTED_FOR_PROCESSING", "VERIFYING_PARENT",
    "PARENT_VERIFIED", "CHILDREN_SUBMITTING", "CHILD_ACCEPTED_FOR_PROCESSING",
    "VERIFYING_RELATIONSHIPS", "PARTIALLY_LIVE",
)


def utcnow():
    return datetime.now(timezone.utc)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checksum(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def redact(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if any(word in key.lower() for word in SENSITIVE_WORDS) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def ptd_metadata(schema, trace, level):
    """Persist validation schema and non-secret binding metadata only."""
    return redact({
        "parentage_level": level.upper(),
        "marketplace_id": trace.get("marketplaceIds"),
        "product_type": trace.get("product_type"),
        "requirements": trace.get("requirements"),
        "requirements_enforced": trace.get("requirementsEnforced"),
        "locale": trace.get("locale"),
        "retrieved_at_utc": utcnow().isoformat(),
        "schema_checksum": checksum(schema),
        "schema": schema,
    })


def _friendly(schema, path):
    head = next(iter(path), "") if path else ""
    node = (schema.get("properties") or {}).get(head) or {}
    return node.get("title") or str(head or "payload").replace("_", " ").title()


def _condition_path(error):
    """Return the exact conditional/combinator path that produced an error."""
    path = list(error.absolute_schema_path)
    for index, segment in enumerate(path):
        if segment in ("if", "then", "else", "allOf", "anyOf", "oneOf"):
            return "/" + "/".join(str(part) for part in path[:index + 1])
    return None


def full_schema_errors(schema, instance, scope, layer="Parent full PUT validation", validation_object="proposed attributes"):
    """Validate with the dialect declared by Amazon's PTD, including conditionals."""
    Validator = validators.validator_for(schema)
    Validator.check_schema(schema)
    errors = sorted(Validator(schema).iter_errors(instance), key=lambda err: list(err.absolute_path))
    result = []
    for error in errors:
        absolute_path = list(error.absolute_path)
        # JSON Schema reports a missing required property at its containing
        # object.  Recover the exact missing property for a useful UI field.
        missing = None
        if error.validator == "required":
            for name in error.validator_value or []:
                if name not in error.instance:
                    missing = name
                    break
        display_path = absolute_path + ([missing] if missing else [])
        path = "/" + "/".join(str(part) for part in display_path)
        schema_path = "/" + "/".join(str(part) for part in error.absolute_schema_path)
        result.append({"scope": scope, "layer": layer, "validation_object": validation_object,
                       "field": _friendly(schema, display_path),
                       "message": "is required" if missing else error.message,
                       "path": path or "/", "instance_path": path or "/",
                       "schema_path": schema_path or "/", "condition_path": _condition_path(error)})
    return result


def _property_value_errors(root_schema, name, value, scope):
    """Validate one proposed PATCH value while retaining the PTD's local definitions."""
    property_schema = {key: root_schema[key] for key in ("$schema", "$id", "$defs", "definitions") if key in root_schema}
    property_schema.update((root_schema.get("properties") or {}).get(name) or {})
    errors = full_schema_errors(
        property_schema,
        value,
        scope,
        layer="Child individual PTD property validation",
        validation_object=f"proposed PATCH value /attributes/{name}",
    )
    for error in errors:
        suffix = "" if error["path"] == "/" else error["path"]
        error["path"] = f"/attributes/{name}{suffix}"
        error["instance_path"] = error["path"]
        if error["schema_path"].startswith("/$defs") or error["schema_path"].startswith("/definitions"):
            continue
        error["schema_path"] = f"/properties/{name}{error['schema_path']}"
        error["field"] = _friendly(root_schema, [name])
    return errors


def patch_errors(child_schema, patches, existing_attributes, asin_or_sku):
    """Validate only the proposed PATCH document and its proposed PTD values.

    Existing Listings attributes are evidence for add/replace decisions only; they
    are never merged into or root-validated as a replacement listing.
    """
    errors, candidate = [], {}
    properties = child_schema.get("properties") or {}
    for patch in patches:
        path, operation = patch.get("path"), patch.get("op")
        if operation not in ("add", "replace"):
            errors.append({"scope": asin_or_sku, "layer": "Child PATCH document validation", "validation_object": "proposed patchListingsItem body", "field": "PATCH operation", "message": "must be add or replace", "path": path or "/", "instance_path": path or "/", "schema_path": "/"})
            continue
        if not isinstance(path, str) or not path.startswith("/attributes/") or path.count("/") != 2:
            errors.append({"scope": asin_or_sku, "layer": "Child PATCH document validation", "validation_object": "proposed patchListingsItem body", "field": "PATCH path", "message": "must target one top-level attribute", "path": path or "/", "instance_path": path or "/", "schema_path": "/properties"})
            continue
        name = path.split("/", 2)[2]
        if name not in properties:
            errors.append({"scope": asin_or_sku, "layer": "Child PATCH document validation", "validation_object": "proposed patchListingsItem body", "field": name, "message": "is not in the Child PTD", "path": path, "instance_path": path, "schema_path": "/properties"})
            continue
        errors.extend(_property_value_errors(child_schema, name, patch.get("value"), asin_or_sku))
        candidate[name] = patch.get("value")
    return errors, candidate


def _snapshot_row(row):
    result = dict(row)
    for name in ("parent_ptd_json", "child_ptd_json", "payload_json"):
        result[name.removesuffix("_json")] = json.loads(result.pop(name))
    return result


def create_snapshot(draft_id, context, product_type, variation_theme, payload, parent_ptd, child_ptd):
    """Insert a new immutable version. Duplicate payload hashes are rejected."""
    ensure_parent_child_schema()
    parent_meta, child_meta = parent_ptd, child_ptd
    safe_payload = redact(payload)
    payload_hash = checksum(safe_payload)
    stamp, expiry = utcnow(), utcnow() + timedelta(minutes=15)
    with get_db_connection() as conn:
        version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM pc_approval_snapshots WHERE draft_id=?", (draft_id,)).fetchone()[0]
        duplicate = conn.execute("SELECT 1 FROM pc_approval_snapshots WHERE draft_id=? AND payload_hash=? AND status='FRESH'", (draft_id, payload_hash)).fetchone()
        if duplicate:
            raise ValueError("An identical fresh approval snapshot already exists for this draft")
        snapshot_id = str(uuid.uuid4())
        try:
            conn.execute("INSERT INTO pc_approval_snapshots(id,draft_id,version,marketplace_id,vendor_context_id,vendor_code,parent_sku,product_type,variation_theme,parent_ptd_json,child_ptd_json,payload_json,payload_hash,status,created_at_utc,expires_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (snapshot_id, draft_id, version, context["marketplace_id"], context["id"], context["vendor_code"],
                          payload["parent"]["sku"], product_type, variation_theme, canonical_json(parent_meta), canonical_json(child_meta),
                          canonical_json(safe_payload), payload_hash, "FRESH", stamp.isoformat(), expiry.isoformat()))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create immutable approval snapshot") from exc
    return get_snapshot(snapshot_id)


def get_snapshot(snapshot_id):
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM pc_approval_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    return _snapshot_row(row) if row else None


def get_snapshot_readonly(snapshot_id):
    """Lookup only; never initializes or repairs Parent--Child schema."""
    with get_readonly_db_connection() as conn:
        row = conn.execute("SELECT * FROM pc_approval_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    return _snapshot_row(row) if row else None


def mark_draft_snapshots_stale(draft_id):
    if not draft_id:
        return 0
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        cursor = conn.execute("UPDATE pc_approval_snapshots SET status='STALE' WHERE draft_id=? AND status='FRESH'", (draft_id,))
        conn.commit()
        return cursor.rowcount


def mark_snapshot_stale(snapshot_id):
    """Explicit edit invalidation; immutable contents are never overwritten."""
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        cursor = conn.execute("UPDATE pc_approval_snapshots SET status='STALE' WHERE id=? AND status='FRESH'", (snapshot_id,))
        conn.commit()
        return cursor.rowcount


def create_preflight(snapshot_id, status, evidence):
    ensure_parent_child_schema()
    checked, expires = utcnow(), utcnow() + timedelta(minutes=15)
    record = {"id": str(uuid.uuid4()), "snapshot_id": snapshot_id, "status": status,
              "evidence": redact(evidence), "checked_at_utc": checked.isoformat(), "expires_at_utc": expires.isoformat()}
    with get_db_connection() as conn:
        conn.execute("INSERT INTO pc_snapshot_preflights(id,snapshot_id,status,evidence_json,checked_at_utc,expires_at_utc) VALUES(?,?,?,?,?,?)",
                     (record["id"], snapshot_id, status, canonical_json(record["evidence"]), record["checked_at_utc"], record["expires_at_utc"]))
        conn.commit()
    return record


def latest_preflight(snapshot_id):
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM pc_snapshot_preflights WHERE snapshot_id=? ORDER BY checked_at_utc DESC LIMIT 1", (snapshot_id,)).fetchone()
    if not row:
        return None
    result = dict(row); result["evidence"] = json.loads(result.pop("evidence_json"));
    result["expired"] = datetime.fromisoformat(result["expires_at_utc"]) <= utcnow()
    return result


def latest_preflight_readonly(snapshot_id):
    with get_readonly_db_connection() as conn:
        row = conn.execute("SELECT * FROM pc_snapshot_preflights WHERE snapshot_id=? ORDER BY checked_at_utc DESC LIMIT 1", (snapshot_id,)).fetchone()
    if not row:
        return None
    result = dict(row); result["evidence"] = json.loads(result.pop("evidence_json"))
    result["expired"] = datetime.fromisoformat(result["expires_at_utc"]) <= utcnow()
    return result


def save_validation_evidence(snapshot_id, payload_hash, operation, sku, result):
    """Persist only sanitized validation-preview evidence; it expires in 30 minutes."""
    ensure_parent_child_schema()
    stamp = utcnow(); expires = stamp + timedelta(minutes=30)
    evidence = redact(result)
    with get_db_connection() as conn:
        conn.execute("INSERT INTO pc_validation_preview_evidence(id,snapshot_id,payload_hash,operation,sku,classification,evidence_json,attempted_at_utc,completed_at_utc,expires_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), snapshot_id, payload_hash, operation, sku, result["classification"], canonical_json(evidence),
                      stamp.isoformat(), stamp.isoformat(), expires.isoformat()))
        conn.commit()
    return {"operation": operation, "sku": sku, "classification": result["classification"], "evidence": evidence, "expires_at_utc": expires.isoformat()}


def idempotency_key(marketplace_id, vendor_context_id, operation, sku, payload_hash):
    return checksum({"marketplace_id": marketplace_id, "vendor_context_id": vendor_context_id,
                     "operation": operation, "sku": sku, "payload_hash": payload_hash})


def reserve_future_submission(snapshot_id):
    """Transaction-safe, local-only conflict reservation; never creates a tracker job."""
    snapshot = get_snapshot(snapshot_id)
    if not snapshot:
        raise ValueError("Unknown approval snapshot")
    if snapshot["status"] != "FRESH":
        raise ValueError("Approval snapshot is stale")
    payload = snapshot["payload"]
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        conflict = conn.execute(f"SELECT 1 FROM parent_child_jobs WHERE marketplace_id=? AND vendor_context_id=? AND parent_sku=? AND status IN ({active}) LIMIT 1", (snapshot["marketplace_id"], snapshot["vendor_context_id"], snapshot["parent_sku"], *ACTIVE_JOB_STATUSES)).fetchone()
        if conflict:
            raise ValueError("An active job already owns this parent SKU")
        now = utcnow().isoformat()
        try:
            conn.execute("INSERT INTO pc_submission_reservations(id,snapshot_id,idempotency_key,marketplace_id,vendor_context_id,reservation_type,parent_sku,active,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                         (str(uuid.uuid4()), snapshot_id, idempotency_key(snapshot["marketplace_id"], snapshot["vendor_context_id"], "PARENT", snapshot["parent_sku"], snapshot["payload_hash"]), snapshot["marketplace_id"], snapshot["vendor_context_id"], "PARENT", snapshot["parent_sku"], 1, now))
            for child in payload.get("children", []):
                conn.execute("INSERT INTO pc_submission_reservations(id,snapshot_id,idempotency_key,marketplace_id,vendor_context_id,reservation_type,child_asin,child_sku,active,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (str(uuid.uuid4()), snapshot_id, idempotency_key(snapshot["marketplace_id"], snapshot["vendor_context_id"], "CHILD", child.get("sku"), snapshot["payload_hash"]), snapshot["marketplace_id"], snapshot["vendor_context_id"], "CHILD", child.get("asin"), child.get("sku"), 1, now))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ValueError("Active parent or child conflict, or duplicate approval reservation") from exc
    return {"reserved": True, "snapshot_id": snapshot_id, "write_operations_enabled": False}
