"""Persistent read-only tracker foundation. It deliberately cannot submit jobs."""
import hashlib, json, random, uuid
from datetime import datetime, timedelta, timezone
from services.db import get_db_connection, get_readonly_db_connection

STATES={"READY_TO_SUBMIT","PREPARED","SUBMITTING","SUBMISSION_UNKNOWN","PARENT_SUBMITTED","PARENT_VERIFYING","PARENT_ACCEPTED","PARENT_VERIFICATION_FAILED","MANUAL_REVIEW_REQUIRED","AUTHORIZATION_DENIED","CONFLICT_REVIEW_REQUIRED","PARENT_SUBMITTING","PARENT_ACCEPTED_FOR_PROCESSING","PARENT_REJECTED","VERIFYING_PARENT","PARENT_VERIFIED","CHILDREN_SUBMITTING","CHILD_ACCEPTED_FOR_PROCESSING","CHILD_REJECTED","VERIFYING_RELATIONSHIPS","PARTIALLY_LIVE","COMPLETED","FAILED","TIMED_OUT","CANCELLED"}
TERMINAL={"COMPLETED","FAILED","TIMED_OUT","CANCELLED","PARENT_REJECTED","AUTHORIZATION_DENIED","CONFLICT_REVIEW_REQUIRED","PARENT_VERIFICATION_FAILED"}
TRANSITIONS={
 "READY_TO_SUBMIT":{"SUBMITTING"},"SUBMITTING":{"PARENT_SUBMITTED","PARENT_REJECTED","AUTHORIZATION_DENIED","CONFLICT_REVIEW_REQUIRED","SUBMISSION_UNKNOWN"},
 "PARENT_SUBMITTED":{"PARENT_VERIFYING"},"SUBMISSION_UNKNOWN":{"PARENT_VERIFYING","MANUAL_REVIEW_REQUIRED"},"PARENT_VERIFYING":{"PARENT_ACCEPTED","PARENT_VERIFICATION_FAILED","MANUAL_REVIEW_REQUIRED"},
 "PREPARED":{"PARENT_SUBMITTING","CANCELLED"},"PARENT_SUBMITTING":{"PARENT_ACCEPTED_FOR_PROCESSING","PARENT_REJECTED","FAILED"},
 "PARENT_ACCEPTED_FOR_PROCESSING":{"VERIFYING_PARENT","PARENT_REJECTED","TIMED_OUT"},"VERIFYING_PARENT":{"PARENT_VERIFIED","PARENT_REJECTED","TIMED_OUT"},
 "PARENT_VERIFIED":{"CHILDREN_SUBMITTING","VERIFYING_RELATIONSHIPS"},"CHILDREN_SUBMITTING":{"CHILD_ACCEPTED_FOR_PROCESSING","CHILD_REJECTED","PARTIALLY_LIVE"},
 "CHILD_ACCEPTED_FOR_PROCESSING":{"VERIFYING_RELATIONSHIPS","PARTIALLY_LIVE"},"CHILD_REJECTED":{"PARTIALLY_LIVE","FAILED"},
 "VERIFYING_RELATIONSHIPS":{"COMPLETED","PARTIALLY_LIVE","TIMED_OUT"},"PARTIALLY_LIVE":{"VERIFYING_RELATIONSHIPS","COMPLETED","TIMED_OUT"}}

def now(): return datetime.now(timezone.utc)
def _safe(value):
    if isinstance(value,dict): return {k:"[redacted]" if any(x in k.lower() for x in ("token","authorization","credential","signature")) else _safe(v) for k,v in value.items()}
    if isinstance(value,list): return [_safe(v) for v in value]
    return value
def next_check(attempt): return (now()+timedelta(seconds=min(72*3600,60*(2**min(attempt,10))+random.randint(0,30)))).isoformat()

def jobs(status=None):
    with get_readonly_db_connection() as c:
        sql="SELECT * FROM parent_child_jobs"; params=[]
        if status: sql+=" WHERE status=?";params=[status]
        return [dict(row) for row in c.execute(sql+" ORDER BY created_at_utc DESC",params)]
def detail(job_id):
    with get_readonly_db_connection() as c:
        job=c.execute("SELECT * FROM parent_child_jobs WHERE id=?",(job_id,)).fetchone()
        if not job:return None
        result=dict(job);result['snapshot']=json.loads(result.pop('snapshot_json'));result['items']=[dict(x) for x in c.execute("SELECT * FROM parent_child_job_items WHERE job_id=?",(job_id,))];result['events']=[dict(x) for x in c.execute("SELECT * FROM parent_child_job_events WHERE job_id=? ORDER BY id",(job_id,))];return result
def transition(job_id,to_status,event_key,message="",evidence=None):
    if to_status not in STATES: raise ValueError("Unknown tracker state")
    with get_db_connection() as c:
        row=c.execute("SELECT status FROM parent_child_jobs WHERE id=?",(job_id,)).fetchone()
        if not row: raise ValueError("Unknown job")
        current=row['status']
        exists=c.execute("SELECT 1 FROM parent_child_job_events WHERE job_id=? AND event_key=?",(job_id,event_key)).fetchone()
        if exists:return False
        if to_status not in TRANSITIONS.get(current,set()): raise ValueError(f"Invalid transition {current} -> {to_status}")
        stamp=now().isoformat();c.execute("UPDATE parent_child_jobs SET status=?,updated_at_utc=? WHERE id=?",(to_status,stamp,job_id));c.execute("INSERT INTO parent_child_job_events(job_id,event_key,from_status,to_status,message,evidence_json,created_at_utc) VALUES(?,?,?,?,?,?,?)",(job_id,event_key,current,to_status,message,json.dumps(_safe(evidence or {})),stamp));c.commit();return True
def record_poll(job_id,attempt,http_status,request_id,issues=None):
    with get_db_connection() as c:
        row=c.execute("SELECT status,submitted_at_utc FROM parent_child_jobs WHERE id=?",(job_id,)).fetchone()
        if not row or not row['submitted_at_utc']: raise ValueError("Polling requires a legitimately submitted job")
        if row['status'] in TERMINAL:return False
        stamp=now().isoformat();due=next_check(attempt);c.execute("INSERT OR IGNORE INTO parent_child_poll_attempts(job_id,attempt_number,checked_at_utc,next_check_at_utc,http_status,request_id,result,issues_json) VALUES(?,?,?,?,?,?,?,?)",(job_id,attempt,stamp,due,http_status,request_id,"GET-only verification",json.dumps(_safe(issues or []))));c.execute("UPDATE parent_child_jobs SET last_checked_at_utc=?,next_check_at_utc=?,updated_at_utc=? WHERE id=?",(stamp,due,stamp,job_id));c.commit();return True

def verify_submitted_job(auth_client, job_id, attempt, marketplace_id, vendor_code):
    """GET-only adapter for a legitimately submitted future job.

    This intentionally refuses PREPARED drafts/snapshots and has no submission
    behavior.  A later approved submission service may call it after recording
    immutable submission evidence.
    """
    detail_row=detail(job_id)
    if not detail_row or not detail_row.get("submitted_at_utc"):
        raise ValueError("Polling requires a legitimately submitted job")
    from services.spapi_listings import listing_item_attributes
    from services.spapi_catalog_relationships import catalog_lookup, current_parent
    if detail_row['status'] not in ('PARENT_SUBMITTED','SUBMISSION_UNKNOWN','PARENT_VERIFYING'):
        raise ValueError('Only submitted or unknown parent jobs can be verified')
    if detail_row['status'] != 'PARENT_VERIFYING':
        transition(job_id,'PARENT_VERIFYING',f'verifying-{attempt}','GET-only parent verification started')
    evidence=[]
    for item in [x for x in detail_row.get("items",[]) if x.get('operation_type') == 'PUT']:
        listing=listing_item_attributes(auth_client,marketplace_id,vendor_code,item["sku"])
        asin=next((x.get('asin') for x in listing.get('summaries',[]) if x.get('asin')),None)
        catalog, catalog_details=catalog_lookup(auth_client,marketplace_id,asin) if asin else (None,{})
        attrs=listing.get('attributes') or {}
        value=lambda key: attrs.get(key) or []
        parentage=any(x.get('value') == 'parent' for x in value('parentage_level') if isinstance(x,dict))
        size=any(x.get('name') == 'SIZE' for x in value('variation_theme') if isinstance(x,dict))
        relationships=value('child_parent_sku_relationship')
        relation_ok=bool(relationships) and all(isinstance(x,dict) and x.get('child_relationship_type') == 'variation' and not x.get('parent_sku') for x in relationships)
        product_type=listing.get('product_type') or next((x.get('productType') for x in listing.get('summaries',[]) if x.get('productType')),None)
        catalog_type=next((x.get('productType') for x in ((catalog or {}).get('productTypes') or [])),None)
        blocking=any((x.get('severity') or '').upper() in ('ERROR','FATAL') for x in listing.get('issues',[]))
        ok=listing.get('status') == 200 and listing.get('api_sku') == item['sku'] and catalog_details.get('http_status') == 200 and (catalog or {}).get('asin') == asin and product_type == 'DISHWARE_PLATE' and catalog_type == 'DISHWARE_PLATE' and parentage and size and relation_ok and not blocking
        evidence.append({'sku':item['sku'],'api_sku':listing.get('api_sku'),'asin':asin,'listings_status':listing.get('status'),'catalog_status':catalog_details.get('http_status'),'product_type':product_type,'catalog_product_type':catalog_type,'parentage_parent':parentage,'variation_theme_size':size,'parent_relationship_valid':relation_ok,'blocking_issues':blocking,'verified':ok})
    first=evidence[0] if evidence else {}
    record_poll(job_id,attempt,first.get("listings_status"),None,evidence)
    transition(job_id,'PARENT_ACCEPTED' if first.get('verified') else 'PARENT_VERIFICATION_FAILED',f'verification-result-{attempt}','GET-only parent verification result',evidence)
    return _safe(evidence)
