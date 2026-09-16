"""Server-owned, sequential attachment for the fixed Princess_Plate family."""
import json, uuid
from datetime import datetime, timedelta, timezone
from services.db import get_db_connection, get_readonly_db_connection
from services.parent_child_migrations import ensure_parent_child_schema
from services.parent_child_safety import checksum, redact
from services.spapi_listings import listing_item_attributes
from services.parent_child_child_attachment import _patch as patch_child_attachment
from services.spapi_validation_preview import validation_preview_patch_listing
from services.spapi_catalog_relationships import catalog_lookup, current_parent

MARKET='A2VIGQ35RCS4UG'; VENDOR='FK6EI'; PARENT='Princess_Plate'; PARENT_ASIN='B0HJLX8X6C'
CHILDREN=(('DI-113','B0CC2JX4BL','9 INCH'),('DI-114','B0CC2JH68Q','7 INCH'))
STATES={'CHILD_READY','CHILD_SUBMITTING','CHILD_SUBMITTED','CHILD_REJECTED','CHILD_SUBMISSION_UNKNOWN','CHILD_VERIFYING','CHILD_ATTACHED','PROPAGATION_PENDING','ALREADY_CORRECT','MANUAL_REVIEW_REQUIRED'}
def _now(): return datetime.now(timezone.utc)
def _blocking(issues): return any((x.get('severity') or '').upper() in ('ERROR','FATAL') for x in issues if isinstance(x,dict))
def _attrs(l,k): return (l.get('attributes') or {}).get(k) or []
def _parent(auth):
 l=listing_item_attributes(auth,MARKET,VENDOR,PARENT); c,d=catalog_lookup(auth,MARKET,PARENT_ASIN); rel=_attrs(l,'child_parent_sku_relationship')
 ok=l.get('status')==200 and l.get('api_sku')==PARENT and PARENT_ASIN in (l.get('api_asins') or []) and l.get('product_type')=='DISHWARE_PLATE' and d.get('http_status')==200 and (c or {}).get('asin')==PARENT_ASIN and next((x.get('productType') for x in (c or {}).get('productTypes',[]) if isinstance(x,dict)),None)=='DISHWARE_PLATE' and any(x.get('value')=='parent' for x in _attrs(l,'parentage_level') if isinstance(x,dict)) and any(x.get('name')=='SIZE' for x in _attrs(l,'variation_theme') if isinstance(x,dict)) and bool(rel) and all(isinstance(x,dict) and x.get('child_relationship_type')=='variation' and not x.get('parent_sku') for x in rel) and not _blocking(l.get('issues') or [])
 return ok,redact({'listings_status':l.get('status'),'catalog_status':d.get('http_status'),'request_id':l.get('request_id')})
def _child(auth,sku,asin):
 l=listing_item_attributes(auth,MARKET,VENDOR,sku); c,d=catalog_lookup(auth,MARKET,asin); typ=next((x.get('productType') for x in (c or {}).get('productTypes',[]) if isinstance(x,dict)),None)
 return l.get('status')==200 and l.get('api_sku')==sku and asin in (l.get('api_asins') or []) and l.get('product_type')=='DISHWARE_PLATE' and d.get('http_status')==200 and (c or {}).get('asin')==asin and typ=='DISHWARE_PLATE' and not _blocking(l.get('issues') or []),current_parent(c),l
def patches(snapshot,sku,existing_size_correct=True):
 if sku not in dict((x[0],x[1:]) for x in CHILDREN): raise ValueError('Unapproved child')
 values={'/attributes/parentage_level':[{'value':'child','marketplace_id':MARKET}],'/attributes/variation_theme':[{'name':'SIZE','marketplace_id':MARKET}],'/attributes/child_parent_sku_relationship':[{'child_relationship_type':'variation','parent_sku':PARENT,'marketplace_id':MARKET}]}
 if not existing_size_correct: values['/attributes/size']=[{'value':next(x[2] for x in CHILDREN if x[0]==sku),'marketplace_id':MARKET}]
 return [{'op':'replace','path':p,'value':v} for p,v in values.items()]
def readiness(snapshot):
 try:
  if snapshot['marketplace_id']!=MARKET or snapshot['vendor_code']!=VENDOR or snapshot['parent_sku']!=PARENT or snapshot['product_type']!='DISHWARE_PLATE' or snapshot['variation_theme']!='SIZE' or checksum(snapshot['payload'])!=snapshot['payload_hash'] or datetime.fromisoformat(snapshot['expires_at_utc'])<=_now(): return {'ready':False}
  return {'ready':True,'children':[x[0] for x in CHILDREN]}
 except Exception:return {'ready':False}
def arm(auth,snapshot,parent_job_id,confirmation):
 if confirmation!='ARM CHILD ATTACHMENT': raise ValueError('Confirmation must exactly equal ARM CHILD ATTACHMENT')
 ensure_parent_child_schema()
 if not readiness(snapshot)['ready']: raise ValueError('Immutable child plan is not ready')
 ok,_=_parent(auth)
 if not ok: raise ValueError('Parent GET verification failed')
 for sku,asin,_ in CHILDREN:
  valid,parent,_=_child(auth,sku,asin)
  if not valid or parent is not None: raise ValueError('Child GET preflight failed')
 expiry=min(datetime.fromisoformat(snapshot['expires_at_utc']),_now()+timedelta(minutes=5)).isoformat()
 with get_db_connection() as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute('select 1 from pc_child_attachment_arms where consumed_at_utc is null and expires_at_utc>?',(_now().isoformat(),)).fetchone(): c.rollback();raise ValueError('Child attachment arm already exists')
  aid=str(uuid.uuid4());c.execute('insert into pc_child_attachment_arms values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(aid,snapshot['id'],snapshot['payload_hash'],parent_job_id,PARENT,PARENT_ASIN,MARKET,snapshot['vendor_context_id'],VENDOR,json.dumps(CHILDREN),expiry,snapshot['expires_at_utc'],_now().isoformat(),expiry,None));c.commit()
 return {'arm_id':aid,'expires_at_utc':expiry}
def validate_children(auth,snapshot,confirmation):
 if confirmation!='VALIDATE CHILDREN': raise ValueError('Confirmation must exactly equal VALIDATE CHILDREN')
 if not readiness(snapshot)['ready']: raise ValueError('Immutable child plan is not ready')
 result=[]
 for sku,_,_ in CHILDREN:
  value=validation_preview_patch_listing(auth,VENDOR,sku,MARKET,{'productType':'DISHWARE_PLATE','patches':patches(snapshot,sku)})
  if value.get('classification') not in ('PASSED','PASSED_WITH_WARNINGS') or _blocking(value.get('issues') or []): raise ValueError('Child validation preview failed')
  result.append({'operation':'PATCH','sku':sku,'payload_hash':snapshot['payload_hash'],'result':redact(value)})
 return result
def execute(auth,snapshot,confirmation):
 if confirmation!='ATTACH CHILDREN': raise ValueError('Confirmation must exactly equal ATTACH CHILDREN')
 if not readiness(snapshot)['ready']: raise ValueError('Immutable child plan is not ready')
 with get_db_connection() as c:
  arm=c.execute('select * from pc_child_attachment_arms where snapshot_id=? and consumed_at_utc is null and expires_at_utc>?',(snapshot['id'],_now().isoformat())).fetchone()
  if not arm: raise ValueError('Child attachment is not armed')
  c.execute('update pc_child_attachment_arms set consumed_at_utc=? where id=?',(_now().isoformat(),arm['id']));c.commit()
 out=[]
 for sku,asin,_ in CHILDREN:
  ok,parent,l=_child(auth,sku,asin)
  if not ok or parent not in (None,PARENT): raise ValueError('Fresh child preflight failed')
  if parent==PARENT: out.append({'sku':sku,'status':'ALREADY_CORRECT'});continue
  key=checksum({'snapshot':snapshot['id'],'hash':snapshot['payload_hash'],'sku':sku})
  status,value=patch_child_attachment(auth,sku,{'productType':'DISHWARE_PLATE','patches':patches(snapshot,sku)},key)
  status='CHILD_SUBMITTED' if status=='CHILD_SUBMITTED' else 'CHILD_REJECTED' if status=='CHILD_REJECTED' else 'CHILD_SUBMISSION_UNKNOWN'
  out.append({'sku':sku,'status':status,'evidence':redact(value)})
  if status!='CHILD_SUBMITTED': break
 return out
