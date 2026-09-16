"""Durable, Amazon-free Phase 1 orchestration for Guided Parent--Child jobs."""
import hashlib
import json
import uuid
from datetime import datetime, timezone

from services.db import get_db_connection
from services.parent_child_migrations import ensure_parent_child_schema

STATES = frozenset((
    "DRAFT", "RESOLVING", "NEEDS_INPUT", "READY_FOR_CONFIRMATION", "APPROVED",
    "LOCAL_VALIDATING", "AMAZON_VALIDATING", "CREATING_PARENT", "WAITING_FOR_PARENT",
    "VALIDATING_CHILDREN", "ATTACHING_CHILDREN", "VERIFYING_FAMILY", "COMPLETED",
    "NEEDS_ATTENTION", "PARTIAL_COMPLETION", "SUBMISSION_UNKNOWN", "CANCELLED_BEFORE_WRITES",
))
TERMINAL_STATES = frozenset(("COMPLETED", "CANCELLED_BEFORE_WRITES"))
OPERATION_STATES = frozenset(("PREPARED", "SUBMITTING", "ACCEPTED", "REJECTED", "UNKNOWN", "VERIFIED"))
ALLOWED_TRANSITIONS = {
    "DRAFT": {"RESOLVING", "NEEDS_INPUT", "READY_FOR_CONFIRMATION", "CANCELLED_BEFORE_WRITES"},
    "RESOLVING": {"NEEDS_INPUT", "READY_FOR_CONFIRMATION", "CANCELLED_BEFORE_WRITES"},
    "NEEDS_INPUT": {"RESOLVING", "READY_FOR_CONFIRMATION", "CANCELLED_BEFORE_WRITES"},
    "READY_FOR_CONFIRMATION": {"APPROVED", "NEEDS_INPUT", "CANCELLED_BEFORE_WRITES"},
    "APPROVED": {"LOCAL_VALIDATING", "NEEDS_ATTENTION", "CANCELLED_BEFORE_WRITES"},
    "LOCAL_VALIDATING": {"AMAZON_VALIDATING", "NEEDS_ATTENTION", "CANCELLED_BEFORE_WRITES"},
    "AMAZON_VALIDATING": {"CREATING_PARENT", "NEEDS_ATTENTION", "CANCELLED_BEFORE_WRITES"},
    "CREATING_PARENT": {"WAITING_FOR_PARENT", "NEEDS_ATTENTION", "SUBMISSION_UNKNOWN"},
    "WAITING_FOR_PARENT": {"VALIDATING_CHILDREN", "NEEDS_ATTENTION", "SUBMISSION_UNKNOWN"},
    "VALIDATING_CHILDREN": {"ATTACHING_CHILDREN", "NEEDS_ATTENTION", "PARTIAL_COMPLETION"},
    "ATTACHING_CHILDREN": {"VERIFYING_FAMILY", "NEEDS_ATTENTION", "PARTIAL_COMPLETION", "SUBMISSION_UNKNOWN"},
    "VERIFYING_FAMILY": {"COMPLETED", "NEEDS_ATTENTION", "PARTIAL_COMPLETION", "SUBMISSION_UNKNOWN"},
    "PARTIAL_COMPLETION": {"VERIFYING_FAMILY", "NEEDS_ATTENTION"},
    "SUBMISSION_UNKNOWN": {"WAITING_FOR_PARENT", "VERIFYING_FAMILY", "NEEDS_ATTENTION", "PARTIAL_COMPLETION"},
    "NEEDS_ATTENTION": {"LOCAL_VALIDATING", "AMAZON_VALIDATING", "WAITING_FOR_PARENT", "VALIDATING_CHILDREN", "VERIFYING_FAMILY", "PARTIAL_COMPLETION", "CANCELLED_BEFORE_WRITES"},
    "COMPLETED": set(), "CANCELLED_BEFORE_WRITES": set(),
}

_SECRET_TERMS = ("credential", "token", "authorization", "header", "signed_url", "signature", "secret", "password")

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if not any(term in str(k).lower() for term in _SECRET_TERMS)}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value

def _family(definition):
    data = _safe(definition or {})
    required = ("marketplace_id", "vendor_context_id", "vendor_code", "parent_sku", "parent_title", "product_type", "variation_theme", "children", "parent_required_values")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError("Missing guided family fields: " + ", ".join(missing))
    if not isinstance(data["children"], list) or not data["children"]:
        raise ValueError("At least one child is required")
    children = []
    for child in data["children"]:
        item = _safe(child)
        if not item.get("asin") or not item.get("vendor_sku") or item.get("variation_value") is None:
            raise ValueError("Every child requires asin, vendor_sku, and variation_value")
        children.append({key: item[key] for key in ("asin", "vendor_sku", "variation_value", "variation_values", "title", "image_url", "product_type", "current_parent", "provenance", "verified_attributes", "attributes_status") if key in item})
    data["children"] = sorted(children, key=lambda item: (item["asin"], item["vendor_sku"]))
    data["parent_required_values"] = _safe(data["parent_required_values"])
    return data

def _conn_row(conn, query, params):
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None

def save_draft(definition, draft_id=None):
    """Create or edit a draft; edits deliberately erase any old approval."""
    ensure_parent_child_schema(); family = _family(definition); digest = canonical_hash(family); stamp = utcnow(); draft_id = draft_id or str(uuid.uuid4())
    with get_db_connection() as conn:
        existing = _conn_row(conn, "SELECT id FROM pc_guided_drafts WHERE id=?", (draft_id,))
        if existing:
            conn.execute("UPDATE pc_guided_drafts SET family_json=?,family_hash=?,approval_hash=NULL,approval_at_utc=NULL,approval_by=NULL,updated_at_utc=? WHERE id=?", (json.dumps(family, sort_keys=True), digest, stamp, draft_id))
        else:
            conn.execute("INSERT INTO pc_guided_drafts(id,family_json,family_hash,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?)", (draft_id, json.dumps(family, sort_keys=True), digest, stamp, stamp))
        conn.commit()
    return get_draft(draft_id)

def get_draft(draft_id):
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        row = _conn_row(conn, "SELECT * FROM pc_guided_drafts WHERE id=?", (draft_id,))
    if row:
        row["family"] = json.loads(row.pop("family_json")); row["canonical_snapshot_hash"] = row["family_hash"]; row["approved"] = row["approval_hash"] == row["family_hash"]
    return row

def review_summary(draft_id):
    draft = get_draft(draft_id)
    if not draft: return None
    family = draft["family"]
    marketplace_name = {"A2VIGQ35RCS4UG": "UAE"}.get(family["marketplace_id"], family["marketplace_id"])
    return {"draft_id": draft_id, "family_hash": draft["family_hash"], "canonical_snapshot_hash": draft["family_hash"], "approved": draft["approved"], "marketplace_id": family["marketplace_id"], "marketplace": f"{marketplace_name} / {family['marketplace_id']}", "vendor_context_id": family["vendor_context_id"], "vendor_code": family["vendor_code"], "parent_sku": family["parent_sku"], "parent_title": family["parent_title"], "product_type": family["product_type"], "variation_theme": family["variation_theme"], "children": family["children"], "parent_required_values": family["parent_required_values"]}

def review_readiness(draft_id):
    """Fail-closed, local-only readiness based on the cached authoritative Parent PTD."""
    draft = get_draft(draft_id)
    if not draft: return None
    family = draft["family"]
    with get_db_connection() as conn:
        row = conn.execute("SELECT schema_json FROM pc_ptd_cache WHERE marketplace_id=? AND vendor_code=? AND product_type=? AND parentage_level='parent'", (family["marketplace_id"], family["vendor_code"], family["product_type"])).fetchone()
    if not row:
        return {"ready": False, "status": "requirements_unavailable", "message": "Requirements not loaded — try Find Products again.", "missing": []}
    try:
        schema = json.loads(row["schema_json"]); raw_required = schema.get("required")
        if not isinstance(raw_required, list) or any(not isinstance(key, str) or not key for key in raw_required): raise ValueError
    except Exception:
        return {"ready": False, "status": "requirements_unavailable", "message": "Requirements not loaded — try Find Products again.", "missing": []}
    system = {"parentage_level", "variation_theme", "child_parent_sku_relationship"}
    applicable = [key for key in raw_required if key not in system]
    if not applicable and raw_required:
        return {"ready": False, "status": "requirements_unavailable", "message": "Requirements not loaded — try Find Products again.", "missing": []}
    values = family.get("parent_required_values") or {}
    def present(value): return value is not None and value != "" and value != [] and value != {}
    missing = [key for key in applicable if not isinstance(values.get(key), dict) or not present(values[key].get("value")) or not values[key].get("provenance")]
    if missing:
        return {"ready": False, "status": "missing_requirements", "message": "Complete every required parent field.", "missing": missing, "applicable_required_fields": applicable}
    return {"ready": True, "status": "ready", "message": "All required parent fields are complete.", "missing": [], "applicable_required_fields": applicable, "genuinely_empty": raw_required == []}

def _verified_context(conn, family):
    return conn.execute("SELECT 1 FROM pc_vendor_contexts WHERE id=? AND marketplace_id=? AND vendor_code=? AND verified=1 AND active=1", (family["vendor_context_id"], family["marketplace_id"], family["vendor_code"])).fetchone()

def _operation_identity(family_hash, operation_key):
    return hashlib.sha256((family_hash + ":" + operation_key).encode()).hexdigest()

def approve(draft_id, exact_family_hash, approved_by=None):
    """Atomically binds one approval to one hash and creates at most one job."""
    ensure_parent_child_schema(); stamp = utcnow()
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        draft = _conn_row(conn, "SELECT * FROM pc_guided_drafts WHERE id=?", (draft_id,))
        if not draft: raise ValueError("Guided draft not found")
        if draft["family_hash"] != exact_family_hash: raise ValueError("Approval hash does not match the current immutable family")
        family = json.loads(draft["family_json"])
        if not _verified_context(conn, family): raise ValueError("A matching verified active vendor context is required")
        conn.execute("UPDATE pc_guided_drafts SET approval_hash=?,approval_at_utc=?,approval_by=?,updated_at_utc=? WHERE id=?", (exact_family_hash, stamp, approved_by, stamp, draft_id))
        job = _conn_row(conn, "SELECT * FROM pc_guided_jobs WHERE family_hash=?", (exact_family_hash,))
        if not job:
            active = "('COMPLETED','CANCELLED_BEFORE_WRITES')"
            parent_conflict = conn.execute(f"SELECT 1 FROM pc_guided_jobs WHERE state NOT IN {active} AND json_extract(family_json,'$.marketplace_id')=? AND json_extract(family_json,'$.vendor_context_id')=? AND json_extract(family_json,'$.parent_sku')=? LIMIT 1", (family["marketplace_id"], family["vendor_context_id"], family["parent_sku"])).fetchone()
            child_conflict = conn.execute(f"SELECT 1 FROM pc_guided_operations o JOIN pc_guided_jobs j ON j.id=o.job_id WHERE j.state NOT IN {active} AND o.operation_type='PATCH' AND (o.child_asin IN ({','.join('?' for _ in family['children'])}) OR o.vendor_sku IN ({','.join('?' for _ in family['children'])})) LIMIT 1", ( *[child["asin"] for child in family["children"]], *[child["vendor_sku"] for child in family["children"]])) .fetchone()
            if parent_conflict or child_conflict: raise ValueError("An active guided job already owns this parent or child")
            job_id = str(uuid.uuid4())
            conn.execute("INSERT INTO pc_guided_jobs(id,draft_id,family_hash,family_json,state,approved_at_utc,approved_by,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, draft_id, exact_family_hash, json.dumps(family, sort_keys=True), "APPROVED", stamp, approved_by, stamp, stamp))
            operations = [("parent", "PUT", None, family["parent_sku"], 0)] + [("child:" + child["asin"], "PATCH", child["asin"], child["vendor_sku"], index + 1) for index, child in enumerate(family["children"])]
            for key, kind, asin, sku, sequence in operations:
                payload_hash = canonical_hash({"family_hash": exact_family_hash, "operation": key})
                conn.execute("INSERT INTO pc_guided_operations(id,job_id,operation_key,operation_type,child_asin,vendor_sku,sequence_number,idempotency_key,payload_hash,state,prepared_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (_operation_identity(exact_family_hash, "record:" + key), job_id, key, kind, asin, sku, sequence, _operation_identity(exact_family_hash, key), payload_hash, "PREPARED", stamp, stamp))
            conn.execute("INSERT INTO pc_guided_events(job_id,event_key,from_state,to_state,evidence_json,created_at_utc) VALUES(?,?,?,?,?,?)", (job_id, "approved", "READY_FOR_CONFIRMATION", "APPROVED", "{}", stamp))
            job = _conn_row(conn, "SELECT * FROM pc_guided_jobs WHERE id=?", (job_id,))
        # The exact final-family confirmation is also the only normal-mode
        # execution selection.  It cannot broaden to any other job.
        conn.execute("UPDATE pc_guided_execution_control SET mode='SELECTED_JOB',selected_job_id=?,selected_family_hash=?,updated_at_utc=? WHERE singleton_id=1", (job["id"], exact_family_hash, stamp))
        conn.commit()
    return get_job(job["id"])

def get_job(job_id):
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        job = _conn_row(conn, "SELECT * FROM pc_guided_jobs WHERE id=?", (job_id,))
        if job:
            job["family"] = json.loads(job.pop("family_json")); job["canonical_snapshot_hash"] = job["family_hash"]; job["blocking_issue"] = json.loads(job.pop("blocking_issue_json") or "null")
            job["operations"] = [dict(row) for row in conn.execute("SELECT * FROM pc_guided_operations WHERE job_id=? ORDER BY sequence_number", (job_id,))]
    return job

def transition(job_id, target, evidence=None):
    if target not in STATES: raise ValueError("Unknown guided state")
    ensure_parent_child_schema(); stamp = utcnow()
    with get_db_connection() as conn:
        job = _conn_row(conn, "SELECT state FROM pc_guided_jobs WHERE id=?", (job_id,))
        if not job: raise ValueError("Guided job not found")
        if target not in ALLOWED_TRANSITIONS[job["state"]]: raise ValueError(f"Invalid guided transition: {job['state']} -> {target}")
        conn.execute("UPDATE pc_guided_jobs SET state=?,blocking_issue_json=?,updated_at_utc=? WHERE id=?", (target, json.dumps(_safe(evidence)) if target == "NEEDS_ATTENTION" else None, stamp, job_id))
        conn.execute("INSERT INTO pc_guided_events(job_id,event_key,from_state,to_state,evidence_json,created_at_utc) VALUES(?,?,?,?,?,?)", (job_id, str(uuid.uuid4()), job["state"], target, json.dumps(_safe(evidence or {})), stamp)); conn.commit()
    return get_job(job_id)

def update_operation(job_id, operation_key, target, request_id=None, http_status=None, issues=None):
    """Persist operation state only; actual transport remains intentionally absent in Phase 1."""
    if target not in OPERATION_STATES: raise ValueError("Unknown operation state")
    ensure_parent_child_schema(); stamp = utcnow()
    with get_db_connection() as conn:
        operation = _conn_row(conn, "SELECT * FROM pc_guided_operations WHERE job_id=? AND operation_key=?", (job_id, operation_key))
        if not operation: raise ValueError("Guided operation not found")
        if operation["state"] in ("ACCEPTED", "UNKNOWN", "VERIFIED") and target == "SUBMITTING": raise ValueError("Accepted, unknown, or verified operations never retry")
        if target == "SUBMITTING":
            if operation["attempt_count"] >= 1: raise ValueError("A guided operation allows one write attempt")
            if operation["operation_type"] == "PATCH":
                parent = _conn_row(conn, "SELECT state FROM pc_guided_operations WHERE job_id=? AND operation_key='parent'", (job_id,))
                earlier = conn.execute("SELECT 1 FROM pc_guided_operations WHERE job_id=? AND operation_type='PATCH' AND sequence_number<? AND state!='VERIFIED'", (job_id, operation["sequence_number"])).fetchone()
                if not parent or parent["state"] != "VERIFIED" or earlier: raise ValueError("Children require a verified parent and verified earlier children")
        attempts = operation["attempt_count"] + (1 if target == "SUBMITTING" else 0)
        conn.execute("UPDATE pc_guided_operations SET state=?,attempt_count=?,request_id=COALESCE(?,request_id),http_status=COALESCE(?,http_status),issues_json=COALESCE(?,issues_json),submitting_at_utc=CASE WHEN ?='SUBMITTING' THEN ? ELSE submitting_at_utc END,completed_at_utc=CASE WHEN ? IN ('ACCEPTED','REJECTED','UNKNOWN') THEN ? ELSE completed_at_utc END,verified_at_utc=CASE WHEN ?='VERIFIED' THEN ? ELSE verified_at_utc END,updated_at_utc=? WHERE id=?", (target, attempts, _safe(request_id), http_status, json.dumps(_safe(issues)) if issues is not None else None, target, stamp, target, stamp, target, stamp, stamp, operation["id"])); conn.commit()
    return get_job(job_id)

def next_action(job_id):
    """Pure persisted-state planner. It neither imports nor calls Amazon clients."""
    job = get_job(job_id)
    if not job: raise ValueError("Guided job not found")
    if job["blocking_issue"] or job["state"] == "NEEDS_ATTENTION": return "NEEDS_ATTENTION"
    if job["state"] in TERMINAL_STATES: return "NONE"
    if job["state"] in ("DRAFT", "RESOLVING", "NEEDS_INPUT", "READY_FOR_CONFIRMATION"): return "WAIT_FOR_APPROVAL"
    if job["state"] == "APPROVED" and all(item["state"] == "PREPARED" for item in job["operations"]): return "LOCAL_VALIDATE"
    if job["state"] == "LOCAL_VALIDATING": return "AMAZON_VALIDATE"
    if job["state"] == "AMAZON_VALIDATING": return "AMAZON_VALIDATE"
    operations = job["operations"]; parent = next(item for item in operations if item["operation_key"] == "parent")
    if parent["state"] == "PREPARED": return "VALIDATE_PARENT"
    if parent["state"] in ("SUBMITTING", "UNKNOWN"): return "RECONCILE_PARENT_GET"
    if parent["state"] == "ACCEPTED": return "VERIFY_PARENT_GET"
    if parent["state"] == "REJECTED": return "NEEDS_ATTENTION"
    if parent["state"] != "VERIFIED": return "WAIT_FOR_PARENT"
    children = [item for item in operations if item["operation_type"] == "PATCH"]
    if not children: return "COMPLETE"
    if job["state"] == "VALIDATING_CHILDREN": return "VALIDATE_CHILDREN"
    for child in children:
        if child["state"] in ("SUBMITTING", "UNKNOWN"): return "RECONCILE_CHILD_GET"
        if child["state"] == "ACCEPTED": return "VERIFY_CHILD_GET"
        if child["state"] == "REJECTED": return "PARTIAL_COMPLETION" if any(x["state"] == "VERIFIED" for x in children) else "NEEDS_ATTENTION"
    if job["state"] == "ATTACHING_CHILDREN" and any(child["state"] == "PREPARED" for child in children): return "ATTACH_CHILD"
    for child in children:
        if child["state"] == "PREPARED": return "VALIDATE_CHILDREN"
    return "COMPLETE" if all(child["state"] == "VERIFIED" for child in children) else "NEEDS_ATTENTION"
