"""Narrow, server-owned two-child attachment boundary.  No browser payload is accepted."""
import json, uuid, requests
from datetime import datetime, timedelta, timezone
from services.db import get_db_connection
from services.parent_child_migrations import ensure_parent_child_schema
from services.parent_child_safety import checksum, idempotency_key, redact
from services.spapi_listings import listing_item_attributes
from services.spapi_catalog_relationships import catalog_lookup, current_parent

MARKET='A2VIGQ35RCS4UG'; VENDOR='FK6EI'; PARENT='Princess_Plate'; PARENT_ASIN='B0HJLX8X6C'
CHILDREN={'DI-113':('B0CC2JX4BL','9 INCH'),'DI-114':('B0CC2JH68Q','7 INCH')}
ALLOWED={'/attributes/parentage_level','/attributes/variation_theme','/attributes/child_parent_sku_relationship','/attributes/size'}
def now(): return datetime.now(timezone.utc)
def _issue_blocking(items): return any((x.get('severity') or '').upper() in ('ERROR','FATAL') for x in items if isinstance(x,dict))
def _values(attrs,key): return (attrs or {}).get(key) or []
def _parent_ok(auth):
    l=listing_item_attributes(auth,MARKET,VENDOR,PARENT); c,d=catalog_lookup(auth,MARKET,PARENT_ASIN); a=l.get('attributes') or {}
    rel=_values(a,'child_parent_sku_relationship')
    ok=l.get('status')==200 and l.get('api_sku')==PARENT and PARENT_ASIN in (l.get('api_asins') or []) and l.get('product_type')=='DISHWARE_PLATE' and d.get('http_status')==200 and (c or {}).get('asin')==PARENT_ASIN and next((x.get('productType') for x in (c or {}).get('productTypes',[]) if isinstance(x,dict)),None)=='DISHWARE_PLATE' and any(x.get('value')=='parent' for x in _values(a,'parentage_level') if isinstance(x,dict)) and any(x.get('name')=='SIZE' for x in _values(a,'variation_theme') if isinstance(x,dict)) and bool(rel) and all(isinstance(x,dict) and x.get('child_relationship_type')=='variation' and not x.get('parent_sku') for x in rel) and not _issue_blocking(l.get('issues') or [])
    return ok,redact({'listings_status':l.get('status'),'catalog_status':d.get('http_status'),'request_id':l.get('request_id')})
def _child_ok(auth,sku):
    asin,size=CHILDREN[sku]; l=listing_item_attributes(auth,MARKET,VENDOR,sku); c,d=catalog_lookup(auth,MARKET,asin)
    parent=current_parent(c) if c else 'Unknown'; typ=next((x.get('productType') for x in (c or {}).get('productTypes',[]) if isinstance(x,dict)),None)
    exact=l.get('status')==200 and l.get('api_sku')==sku and (l.get('api_asins') or [])==[asin] and l.get('product_type')=='DISHWARE_PLATE' and d.get('http_status')==200 and (c or {}).get('asin')==asin and typ=='DISHWARE_PLATE'
    return exact,parent,l,c
def validate_patch(sku,patches,existing):
    if sku not in CHILDREN or not isinstance(patches,list) or not patches: raise ValueError('Exact authorized child patch is required')
    paths=[x.get('path') for x in patches]
    if len(paths)!=len(set(paths)) or any(x not in ALLOWED for x in paths): raise ValueError('Only relationship fields and a necessary size patch are allowed')
    required={'/attributes/parentage_level','/attributes/variation_theme','/attributes/child_parent_sku_relationship'}
    if not required.issubset(paths): raise ValueError('All relationship fields are required')
    if '/attributes/size' in paths and existing: raise ValueError('Size is already correct and must be omitted')
    return True
def arm(auth,snapshot,parent_job_id,confirmation):
    if confirmation!='ARM CHILD ATTACHMENT': raise ValueError('Confirmation must exactly equal ARM CHILD ATTACHMENT')
    ensure_parent_child_schema()
    if snapshot['marketplace_id']!=MARKET or snapshot['vendor_code']!=VENDOR or snapshot['parent_sku']!=PARENT or snapshot['product_type']!='DISHWARE_PLATE' or snapshot['variation_theme']!='SIZE': raise ValueError('Immutable snapshot scope mismatch')
    if checksum(snapshot['payload'])!=snapshot['payload_hash'] or datetime.fromisoformat(snapshot['expires_at_utc'])<=now(): raise ValueError('Snapshot is stale')
    ok,_=_parent_ok(auth)
    if not ok: raise ValueError('Parent GET verification failed')
    for sku in CHILDREN:
        exact,parent,l,_=_child_ok(auth,sku)
        if not exact or parent is not None: raise ValueError('Child GET verification failed')
    expiry=min(datetime.fromisoformat(snapshot['expires_at_utc']),now()+timedelta(minutes=5)).isoformat(); aid=str(uuid.uuid4())
    with get_db_connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('select 1 from pc_child_attachment_arms where consumed_at_utc is null and expires_at_utc>?',(now().isoformat(),)).fetchone(): c.rollback();raise ValueError('Child attachment already armed')
        c.execute('insert into pc_child_attachment_arms values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(aid,snapshot['id'],snapshot['payload_hash'],parent_job_id,PARENT,PARENT_ASIN,MARKET,snapshot['vendor_context_id'],VENDOR,json.dumps(CHILDREN),expiry,snapshot['expires_at_utc'],now().isoformat(),expiry,None));c.commit()
    return {'id':aid,'expires_at_utc':expiry}
def _patch(auth,sku,body,key):
    r=requests.patch(f'https://sellingpartnerapi-eu.amazon.com/listings/2021-08-01/items/{VENDOR}/{sku}',params={'marketplaceIds':MARKET,'issueLocale':'en_AE'},json=body,headers={'x-amz-access-token':auth.get_lwa_access_token(),'x-amz-client-request-id':key},timeout=20)
    try: payload=r.json()
    except ValueError: payload=None
    issues=payload.get('issues',[]) if isinstance(payload,dict) else []
    state='CHILD_SUBMITTED' if r.status_code==200 and isinstance(payload,dict) and isinstance(payload.get('submissionId'),str) and not _issue_blocking(issues) else ('CHILD_REJECTED' if r.status_code in (400,422) else 'AUTHORIZATION_DENIED' if r.status_code in (401,403) else 'CONFLICT_REVIEW_REQUIRED' if r.status_code==409 else 'SUBMISSION_UNKNOWN')
    return state,redact({'http_status':r.status_code,'request_id':r.headers.get('x-amzn-RequestId') or r.headers.get('x-amz-request-id'),'issues':issues})
