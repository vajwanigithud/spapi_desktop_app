"""The sole live transport for the deliberately narrow parent-only phase.

Nothing in this module accepts a browser payload.  It resolves the immutable
approval snapshot and sends only its parent `putListingsItem` body.
"""
import json
import uuid
from datetime import datetime, timezone

import requests

from services.db import get_db_connection, get_readonly_db_connection
from services.parent_child_migrations import ensure_parent_child_schema
from services.parent_child_safety import checksum, idempotency_key, redact, utcnow
from services.parent_child_validation import WRITE_FEATURE_FLAGS
from services import parent_child_runtime_settings as runtime_settings
from services.spapi_listings import listing_item_attributes
from services.spapi_catalog_relationships import catalog_lookup, current_parent

UAE_MARKETPLACE = "A2VIGQ35RCS4UG"
VERIFIED_VENDOR = "FK6EI"
SUCCESSFUL_PREVIEW = {"PASSED", "PASSED_WITH_WARNINGS"}
CONFLICT_ACTIVE = ('READY_TO_SUBMIT','SUBMITTING','PARENT_SUBMITTED','PARENT_VERIFYING','PARENT_ACCEPTED','PARENT_VERIFICATION_FAILED','MANUAL_REVIEW_REQUIRED','SUBMISSION_UNKNOWN','CONFLICT_REVIEW_REQUIRED')


def _stamp(): return utcnow().isoformat()

def _unexpired(snapshot):
    """Fail closed for missing, naive, malformed, or elapsed expiry values."""
    try:
        value=datetime.fromisoformat(snapshot['expires_at_utc'])
        return value.tzinfo is not None and value.utcoffset() is not None and value > utcnow()
    except (KeyError, TypeError, ValueError):
        return False

def _assert_current(snapshot):
    if snapshot.get('status') != 'FRESH' or not _unexpired(snapshot):
        raise ValueError('Approval snapshot is stale or expired; generate a new preview')

def _active_conflict(conn, snapshot):
    active=','.join('?' for _ in CONFLICT_ACTIVE)
    rows = conn.execute(f"SELECT status FROM parent_child_jobs WHERE marketplace_id=? AND vendor_context_id=? AND parent_sku=? AND status IN ({active})", (snapshot['marketplace_id'], snapshot['vendor_context_id'], snapshot['parent_sku'],*CONFLICT_ACTIVE)).fetchall()
    if rows:
        return True
    children = snapshot['payload'].get('children', [])
    if not children:
        return False
    placeholders = ','.join('?' for _ in children)
    row = conn.execute(f"SELECT 1 FROM parent_child_jobs j JOIN parent_child_job_items i ON i.job_id=j.id WHERE j.marketplace_id=? AND j.vendor_context_id=? AND j.status IN ({active}) AND (i.sku IN ({placeholders}) OR i.asin IN ({placeholders})) LIMIT 1", (snapshot['marketplace_id'], snapshot['vendor_context_id'],*CONFLICT_ACTIVE,*[x.get('sku') for x in children], *[x.get('asin') for x in children])).fetchone()
    return bool(row)


def _validation_ok(conn, snapshot):
    rows = conn.execute("SELECT operation,sku,classification,expires_at_utc,payload_hash,evidence_json FROM pc_validation_preview_evidence WHERE snapshot_id=?", (snapshot['id'],)).fetchall()
    wanted = {("PUT", snapshot['parent_sku'])}
    wanted.update(("PATCH", child['sku']) for child in snapshot['payload'].get('children', []))
    expected_hash = checksum(snapshot['payload'])
    seen = set()
    # A snapshot's entire evidence history is authoritative.  Inspect every
    # row before accepting any row: legacy/hashless or conflicting rows cannot
    # be silently filtered out as belonging to a harmless prior attempt.
    for row in rows:
        payload_hash = row['payload_hash']
        operation, sku = row['operation'], row['sku']
        if (not isinstance(payload_hash, str) or not payload_hash.strip() or
                len(payload_hash) != 64 or any(ch not in '0123456789abcdef' for ch in payload_hash.lower()) or
                payload_hash != expected_hash):
            return False
        if not isinstance(operation, str) or not operation.strip() or not isinstance(sku, str) or not sku.strip():
            return False
        key = (operation, sku)
        if key not in wanted or key in seen:
            return False
        seen.add(key)
        if row['classification'] not in SUCCESSFUL_PREVIEW:
            return False
        try:
            expiry = datetime.fromisoformat(row['expires_at_utc'])
            evidence = json.loads(row['evidence_json'])
            issues = evidence.get('issues')
            if expiry.tzinfo is None or expiry.utcoffset() is None or expiry <= utcnow() or not isinstance(issues, list):
                return False
            if any(not isinstance(item, dict) or (item.get('severity') or '').upper() in ('ERROR','FATAL') for item in issues):
                return False
        except Exception: return False
    return seen == wanted


def gates(snapshot, context, *, readonly=False):
    """Return fail-closed gate results without doing a write or accepting input."""
    flags = WRITE_FEATURE_FLAGS
    result = {
        'create_parent_enabled': bool(runtime_settings.parent_arm(snapshot['id'])),
        'other_write_flags_disabled': all(flags[x] is False for x in ('attach_child','detach_child','move_child','bulk_feed')),
        'validation_preview_disabled': not runtime_settings.enabled(),
        'uae_vendor_snapshot': snapshot['marketplace_id'] == UAE_MARKETPLACE and snapshot['vendor_code'] == VERIFIED_VENDOR and context['vendor_code'] == VERIFIED_VENDOR and context['id'] == snapshot['vendor_context_id'],
        'snapshot_fresh': snapshot['status'] == 'FRESH' and _unexpired(snapshot),
        'snapshot_hash_matches': checksum(snapshot['payload']) == snapshot['payload_hash'],
        'ptd_checksums_match': checksum(snapshot['parent_ptd']['schema']) == snapshot['parent_ptd']['schema_checksum'] and checksum(snapshot['child_ptd']['schema']) == snapshot['child_ptd']['schema_checksum'],
        'validation_evidence_current': False,
        'no_active_job_or_reservation': False,
    }
    if not readonly:
        ensure_parent_child_schema()
    connection = get_readonly_db_connection if readonly else get_db_connection
    with connection() as conn:
        result['validation_evidence_current'] = _validation_ok(conn, snapshot)
        reservation = conn.execute("SELECT 1 FROM pc_submission_reservations WHERE marketplace_id=? AND vendor_context_id=? AND active=1 AND (parent_sku=? OR child_asin IN (%s) OR child_sku IN (%s)) LIMIT 1" % (','.join('?' for _ in snapshot['payload'].get('children', [])) or "NULL", ','.join('?' for _ in snapshot['payload'].get('children', [])) or "NULL"), (snapshot['marketplace_id'], snapshot['vendor_context_id'], snapshot['parent_sku'], *[x.get('asin') for x in snapshot['payload'].get('children', [])], *[x.get('sku') for x in snapshot['payload'].get('children', [])])).fetchone()
        result['no_active_job_or_reservation'] = not reservation and not _active_conflict(conn, snapshot)
    result['ready'] = all(result.values())
    return result


def final_preflight(auth, snapshot, vendor_code):
    """A new GET-only check made in the same request immediately before job creation."""
    _assert_current(snapshot)
    parent = listing_item_attributes(auth, snapshot['marketplace_id'], vendor_code, snapshot['parent_sku'])
    ok = parent.get('status') == 404
    children = []
    for child in snapshot['payload'].get('children', []):
        listing = listing_item_attributes(auth, snapshot['marketplace_id'], vendor_code, child['sku'])
        catalog, details = catalog_lookup(auth, snapshot['marketplace_id'], child['asin'])
        types = (catalog or {}).get('productTypes') or []
        product_type = types[0].get('productType') if types else None
        summaries=listing.get('summaries') or []
        # GetListingsItem returns the requested seller SKU at the response top
        # level and its catalog ASIN in summary objects.  A summary does not
        # necessarily repeat ``sku``.  Both are API-returned fields: never use
        # the requested values as a substitute for either identity component.
        listing_identity=(isinstance(listing.get('api_sku'),str) and listing['api_sku'] == child['sku'] and
                          isinstance(listing.get('api_asins'),list) and child['asin'] in listing['api_asins'])
        listing_type=listing.get('product_type') or next((row.get('productType') for row in summaries if row.get('asin') == child['asin']),None)
        catalog_identity=(catalog or {}).get('asin') == child['asin']
        blocking_issues=any((issue.get('severity') or '').upper() in ('ERROR','FATAL') for issue in (listing.get('issues') or []) if isinstance(issue,dict))
        child_ok = (listing.get('status') == 200 and listing_identity and listing_type == 'DISHWARE_PLATE' and
                    details.get('http_status') == 200 and catalog_identity and product_type == 'DISHWARE_PLATE' and
                    snapshot['product_type'] == 'DISHWARE_PLATE' and current_parent(catalog) is None and not blocking_issues)
        ok = ok and child_ok
        children.append({'asin': child['asin'], 'sku': child['sku'], 'listings_status': listing.get('status'), 'listings_identity':listing_identity, 'listings_product_type':listing_type, 'catalog_status': details.get('http_status'), 'catalog_identity':catalog_identity, 'product_type': product_type, 'current_parent': current_parent(catalog) if catalog else 'Unknown','blocking_issues':blocking_issues})
    return ok, redact({'parent_sku_status': parent.get('status'), 'parent_request_id': parent.get('request_id'), 'children': children})


def _create_ready_job(snapshot, preflight_id):
    """Atomically reserve scope and commit the READY_TO_SUBMIT record before PUT."""
    ensure_parent_child_schema(); _assert_current(snapshot); job_id = str(uuid.uuid4()); stamp = _stamp()
    key = idempotency_key(snapshot['marketplace_id'], snapshot['vendor_context_id'], 'PARENT_PUT', snapshot['parent_sku'], snapshot['payload_hash'])
    with get_db_connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        duplicate = conn.execute("SELECT job_id FROM pc_parent_submission_details WHERE snapshot_id=? OR idempotency_key=?", (snapshot['id'], key)).fetchone()
        if duplicate: conn.rollback(); raise ValueError('This snapshot already has a parent submission job')
        if _active_conflict(conn, snapshot): conn.rollback(); raise ValueError('An active job already owns this parent SKU')
        try:
            conn.execute("INSERT INTO pc_submission_reservations(id,snapshot_id,idempotency_key,marketplace_id,vendor_context_id,reservation_type,parent_sku,active,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()),snapshot['id'],key,snapshot['marketplace_id'],snapshot['vendor_context_id'],'PARENT',snapshot['parent_sku'],1,stamp))
            for child in snapshot['payload'].get('children',[]):
                child_key=idempotency_key(snapshot['marketplace_id'],snapshot['vendor_context_id'],'CHILD',child['sku'],snapshot['payload_hash'])
                conn.execute("INSERT INTO pc_submission_reservations(id,snapshot_id,idempotency_key,marketplace_id,vendor_context_id,reservation_type,child_asin,child_sku,active,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),snapshot['id'],child_key,snapshot['marketplace_id'],snapshot['vendor_context_id'],'CHILD',child['asin'],child['sku'],1,stamp))
                conn.execute("INSERT INTO parent_child_job_items(job_id,asin,sku,operation_type,status,issues_json) VALUES(?,?,?,?,?,?)",(job_id,child['asin'],child['sku'],'REFERENCE','RESERVED','[]'))
            validations=[{'id':row['id'],'operation':row['operation'],'sku':row['sku'],'expires_at_utc':row['expires_at_utc'],'evidence_checksum':checksum(json.loads(row['evidence_json']))} for row in conn.execute("SELECT id,operation,sku,expires_at_utc,evidence_json FROM pc_validation_preview_evidence WHERE snapshot_id=?",(snapshot['id'],))]
            manifest={'snapshot_id':snapshot['id'],'payload_hash':snapshot['payload_hash'],'parent_ptd_checksum':snapshot['parent_ptd']['schema_checksum'],'child_ptd_checksum':snapshot['child_ptd']['schema_checksum'],'parent_sku':snapshot['parent_sku'],'marketplace_id':snapshot['marketplace_id'],'vendor_context_id':snapshot['vendor_context_id'],'product_type':snapshot['product_type'],'variation_theme':snapshot['variation_theme'],'children':[{'asin':x['asin'],'sku':x['sku']} for x in snapshot['payload'].get('children',[])],'validation_evidence':validations,'created_at_utc':stamp,'idempotency_key':key}
            conn.execute("INSERT INTO parent_child_jobs(id,draft_id,marketplace_id,vendor_context_id,parent_sku,product_type,variation_theme,status,snapshot_json,payload_hash,submitted_at_utc,last_checked_at_utc,next_check_at_utc,deadline_at_utc,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id,snapshot['draft_id'],snapshot['marketplace_id'],snapshot['vendor_context_id'],snapshot['parent_sku'],snapshot['product_type'],snapshot['variation_theme'],'READY_TO_SUBMIT',json.dumps(manifest),snapshot['payload_hash'],None,None,None,None,stamp,stamp))
            conn.execute("INSERT INTO parent_child_job_items(job_id,asin,sku,operation_type,status,issues_json) VALUES(?,?,?,?,?,?)", (job_id,None,snapshot['parent_sku'],'PUT','READY_TO_SUBMIT','[]'))
            conn.execute("INSERT INTO pc_parent_submission_details(job_id,snapshot_id,idempotency_key,operation_type,vendor_code,final_preflight_id,created_at_utc) VALUES(?,?,?,?,?,?,?)", (job_id,snapshot['id'],key,'putListingsItem',VERIFIED_VENDOR,preflight_id,stamp))
            conn.execute("INSERT INTO parent_child_job_events(job_id,event_key,from_status,to_status,message,evidence_json,created_at_utc) VALUES(?,?,?,?,?,?,?)", (job_id,'ready','', 'READY_TO_SUBMIT','Durably committed before parent PUT','{}',stamp)); conn.commit()
        except Exception:
            conn.rollback(); raise
    return job_id, key


def _transition(job_id, target, event, evidence):
    with get_db_connection() as conn:
        row=conn.execute("SELECT status FROM parent_child_jobs WHERE id=?",(job_id,)).fetchone()
        if not row or row['status'] != ('READY_TO_SUBMIT' if target == 'SUBMITTING' else 'SUBMITTING'): raise ValueError('Job is not eligible for this transition')
        stamp=_stamp(); conn.execute("UPDATE parent_child_jobs SET status=?, submitted_at_utc=CASE WHEN ?='SUBMITTING' THEN ? ELSE submitted_at_utc END, updated_at_utc=? WHERE id=?",(target,target,stamp,stamp,job_id)); conn.execute("INSERT INTO parent_child_job_events(job_id,event_key,from_status,to_status,message,evidence_json,created_at_utc) VALUES(?,?,?,?,?,?,?)",(job_id,event,row['status'],target,'Parent-only submission state change',json.dumps(redact(evidence)),stamp)); conn.commit()


def _put(auth, snapshot, key):
    url=f"https://sellingpartnerapi-eu.amazon.com/listings/2021-08-01/items/{VERIFIED_VENDOR}/{snapshot['parent_sku']}"
    # No mode parameter and no retry: an ambiguous request is never replayed.
    response=requests.put(url, params={'marketplaceIds': UAE_MARKETPLACE, 'issueLocale':'en_AE'}, json=snapshot['payload']['parent']['body'], headers={'x-amz-access-token':auth.get_lwa_access_token(),'x-amz-client-request-id':key}, timeout=20)
    content_type=response.headers.get('content-type','').lower()
    try: body=response.json() if 'json' in content_type else None
    except ValueError: body=None
    if not isinstance(body,dict):
        return 'SUBMISSION_UNKNOWN', redact({'http_status':response.status_code,'request_id':response.headers.get('x-amzn-RequestId') or response.headers.get('x-amz-request-id'),'outcome':'malformed_response'})
    if not isinstance(body.get('submissionId'),str) or not body['submissionId'].strip() or not isinstance(body.get('issues',[]),list):
        return 'SUBMISSION_UNKNOWN', redact({'http_status':response.status_code,'request_id':response.headers.get('x-amzn-RequestId') or response.headers.get('x-amz-request-id'),'outcome':'unexpected_response_shape'})
    issues=[{'severity':x.get('severity'),'code':x.get('code'),'message':x.get('message')} for x in body.get('issues',[])]
    submission=body.get('status') or body.get('submissionStatus')
    recognized={'ACCEPTED','VALID','SUCCESS','SUCCESSFUL'}
    if response.status_code == 200: status='PARENT_REJECTED' if any((x.get('severity') or '').upper() in ('ERROR','FATAL') for x in issues) else ('PARENT_SUBMITTED' if str(submission).upper() in recognized else 'SUBMISSION_UNKNOWN')
    elif response.status_code in (400,422): status='PARENT_REJECTED'
    elif response.status_code in (401,403): status='AUTHORIZATION_DENIED'
    elif response.status_code == 409: status='CONFLICT_REVIEW_REQUIRED'
    else: status='SUBMISSION_UNKNOWN'
    return status, redact({'http_status':response.status_code,'request_id':response.headers.get('x-amzn-RequestId') or response.headers.get('x-amz-request-id'),'issues':issues,'submission_status':submission})


def execute(auth, snapshot, context, confirmation, create_preflight):
    if confirmation != 'CREATE PARENT': raise ValueError('Confirmation must exactly equal CREATE PARENT')
    result=gates(snapshot, context)
    if not result['ready']: raise ValueError('Parent submission safety gates failed')
    ok,evidence=final_preflight(auth,snapshot,VERIFIED_VENDOR)
    preflight=create_preflight(snapshot['id'],'passed' if ok else 'blocked',evidence)
    if not ok: raise ValueError('Brand-new final GET-only preflight failed')
    _assert_current(snapshot)
    runtime_settings.consume_parent_arm(snapshot)
    job_id,key=_create_ready_job(snapshot,preflight['id'])
    _assert_current(snapshot)
    _transition(job_id,'SUBMITTING','submitting',{'payload_hash':snapshot['payload_hash']})
    try: status,evidence=_put(auth,snapshot,key)
    except Exception:
        status,evidence='SUBMISSION_UNKNOWN',{'outcome':'ambiguous_exception'}
    with get_db_connection() as conn:
        conn.execute("UPDATE pc_parent_submission_details SET request_id=?,http_status=?,submission_status=?,issues_json=? WHERE job_id=?",(evidence.get('request_id'),evidence.get('http_status'),evidence.get('submission_status'),json.dumps(evidence.get('issues',[])),job_id)); conn.commit()
    _transition(job_id,status,'result',evidence)
    return {'job_id':job_id,'status':status,'payload_hash':snapshot['payload_hash']}
