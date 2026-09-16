"""Read-only Phase 1 API for Parent–Child Manager."""
from typing import Any, Dict, List
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse
import logging
import uuid
from pydantic import BaseModel, Field
from services.parent_child_migrations import ensure_parent_child_schema
import config
from services.db import get_db_connection
from services.parent_child_store import archive_draft, delete_draft, duplicate_draft, get_draft, list_drafts, list_jobs, save_draft, set_vendor_context_active, upsert_vendor_context, vendor_contexts, verified_vendor_context_readonly
from services.parent_child_validation import WRITE_FEATURE_FLAGS, parse_asins, theme_options
import services.parent_child_validation as pc_validation
from services.parent_child_resolver import resolve_many
from services.spapi_product_types import ProductTypeDefinitionError, get_definition
from services.spapi_listings import listing_item_attributes, verify_vendor_access
from services.parent_child_preview import build_preview
from services.spapi_catalog_relationships import catalog_lookup, current_parent
from services.parent_child_safety import (create_preflight, create_snapshot, full_schema_errors,
    checksum, get_snapshot, get_snapshot_readonly, latest_preflight, latest_preflight_readonly, mark_snapshot_stale, patch_errors, ptd_metadata, redact, save_validation_evidence)
from services.spapi_validation_preview import validation_preview_patch_listing, validation_preview_put_listing
from services import parent_child_tracker as tracker
from services import parent_child_parent_submission as parent_submission
from services import parent_child_runtime_settings as runtime_settings
from services import parent_child_child_submission as child_submission
from services import parent_child_guided_orchestrator as guided
from services import parent_child_guided_worker as guided_worker

router=APIRouter(prefix='/api/parent-child',tags=['parent-child'])
logger=logging.getLogger(__name__)
class VendorContext(BaseModel): marketplace_id:str; vendor_code:str; display_name:str|None=None
class ContextStatus(BaseModel): active:bool
class ResolveRequest(BaseModel): marketplace_id:str; vendor_code:str; asins:List[str]=Field(default_factory=list)
class DraftPayload(BaseModel): data:Dict[str,Any]
class PtdRequest(BaseModel): marketplace_id:str; vendor_code:str; product_type:str; parentage_level:str='parent'; force:bool=False
class PreviewRequest(BaseModel): data:Dict[str,Any]
class PreflightRequest(BaseModel): snapshot_id:str
class ValidationPreviewRequest(BaseModel): snapshot_id:str; confirmation:str
class ParentSubmissionRequest(BaseModel): snapshot_id:str; confirmation:str
class ValidationModeEnable(BaseModel): confirmation:str
class ParentArmRequest(BaseModel): snapshot_id:str; confirmation:str
class ChildAttachmentRequest(BaseModel): snapshot_id:str; confirmation:str; parent_job_id:str
class GuidedDraftRequest(BaseModel): definition:Dict[str,Any]; draft_id:str|None=None
class GuidedApprovalRequest(BaseModel): family_hash:str; approved_by:str|None=None
class GuidedResumeRequest(BaseModel): family_hash:str
class GuidedParentReconciliationRequest(BaseModel): family_hash:str
class GuidedChildWriteConfirmation(BaseModel): family_hash:str; plan_hash:str; confirmation:str
class GuidedSelectedExecutionRequest(BaseModel): job_id:str; family_hash:str; confirmation:str
class GuidedAutomaticExecutionRequest(BaseModel): confirmation:str

def _checked_context(marketplace_id,vendor_code, context_id=None):
    found=next((x for x in vendor_contexts(marketplace_id) if x['vendor_code']==vendor_code and x['verified'] and x['active']),None)
    if not found: raise HTTPException(400,'A configured, verified vendor context is required')
    if context_id is not None and found['id'] != context_id:
        raise HTTPException(409,'Vendor context does not match the verified marketplace/vendor context')
    return found

# Guided Mode Phase 1 is intentionally local-only.  These narrow endpoints only
# persist/review durable state and never acquire an auth client or call Amazon.
@router.post('/guided/drafts')
def guided_save_draft(payload: GuidedDraftRequest):
    try: return {'draft': guided.save_draft(payload.definition, payload.draft_id)}
    except ValueError as exc: raise HTTPException(422, str(exc))

@router.get('/guided/drafts')
def guided_drafts():
    """Local draft inventory only; this endpoint never starts a worker."""
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        rows = [dict(row) for row in conn.execute("SELECT id,family_hash,approval_hash,created_at_utc,updated_at_utc,family_json FROM pc_guided_drafts ORDER BY updated_at_utc DESC")]
    return {'items': [{'id': row['id'], 'family_hash': row['family_hash'], 'approved': row['approval_hash'] == row['family_hash'], 'created_at_utc': row['created_at_utc'], 'updated_at_utc': row['updated_at_utc'], 'parent_sku': __import__('json').loads(row['family_json']).get('parent_sku'), 'parent_title': __import__('json').loads(row['family_json']).get('parent_title')} for row in rows]}

@router.get('/guided/drafts/{draft_id}')
def guided_draft(draft_id: str):
    value = guided.get_draft(draft_id)
    if not value: raise HTTPException(404, 'Guided draft not found')
    return {'draft': value}

@router.post('/guided/drafts/{draft_id}/duplicate')
def guided_duplicate_draft(draft_id: str):
    value = guided.get_draft(draft_id)
    if not value: raise HTTPException(404, 'Guided draft not found')
    return {'draft': guided.save_draft(value['family'])}

@router.delete('/guided/drafts/{draft_id}')
def guided_delete_draft(draft_id: str):
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        if conn.execute("SELECT 1 FROM pc_guided_jobs WHERE draft_id=? LIMIT 1", (draft_id,)).fetchone(): raise HTTPException(409, 'Draft is referenced by a job and cannot be deleted')
        conn.execute("DELETE FROM pc_guided_drafts WHERE id=?", (draft_id,)); conn.commit()
    return {'ok': True}

@router.get('/guided/drafts/{draft_id}/review')
def guided_review(draft_id: str):
    value = guided.review_summary(draft_id)
    if not value: raise HTTPException(404, 'Guided draft not found')
    return {'review': value}

@router.get('/guided/drafts/{draft_id}/review-readiness')
def guided_review_readiness(draft_id: str):
    value = guided.review_readiness(draft_id)
    if not value: raise HTTPException(404, 'Guided draft not found')
    return value

@router.get('/guided/drafts/{draft_id}/current-job')
def guided_current_job(draft_id: str):
    """Only an exact approved-family job may be restored into Guided progress."""
    draft = guided.get_draft(draft_id)
    if not draft or not draft.get('approved'): return {'job': None}
    with get_db_connection() as conn:
        row = conn.execute("SELECT id FROM pc_guided_jobs WHERE draft_id=? AND family_hash=? AND state!='CANCELLED_BEFORE_WRITES' LIMIT 1", (draft_id, draft['family_hash'])).fetchone()
    return {'job': guided.get_job(row['id']) if row else None}

@router.post('/guided/drafts/{draft_id}/approve')
def guided_approve(draft_id: str, payload: GuidedApprovalRequest):
    try: return {'job': guided.approve(draft_id, payload.family_hash, payload.approved_by)}
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.get('/guided/jobs')
def guided_jobs():
    """Local Guided job inventory.  Listing jobs never runs a worker."""
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        ids = [row['id'] for row in conn.execute("SELECT id FROM pc_guided_jobs ORDER BY created_at_utc DESC")]
    items = []
    for job_id in ids:
        job = guided.get_job(job_id)
        operations = job['operations']
        items.append({'id': job['id'], 'state': job['state'], 'family_hash': job['family_hash'], 'parent_sku': job['family']['parent_sku'], 'parent_title': job['family'].get('parent_title'), 'created_at_utc': job['created_at_utc'], 'approved_at_utc': job['approved_at_utc'], 'failure_reason': job['blocking_issue'], 'write_attempted': any(item['attempt_count'] or item['state'] != 'PREPARED' for item in operations)})
    return {'items': items}

@router.get('/guided/execution-control')
def guided_execution_control():
    """Durable local scheduler control; this endpoint never starts or ticks a worker."""
    return guided_worker.execution_control()

@router.post('/guided/execution-control/pause')
def guided_pause_execution():
    return guided_worker.set_execution_control('PAUSED')

@router.post('/guided/execution-control/selected-job')
def guided_run_selected_job(payload: GuidedSelectedExecutionRequest):
    try: return guided_worker.set_execution_control('SELECTED_JOB', job_id=payload.job_id, family_hash=payload.family_hash, confirmation=payload.confirmation)
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/execution-control/automatic')
def guided_enable_automatic_execution(payload: GuidedAutomaticExecutionRequest):
    try: return guided_worker.set_execution_control('AUTOMATIC', confirmation=payload.confirmation)
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.get('/guided/jobs/{job_id}')
def guided_job(job_id: str):
    value = guided.get_job(job_id)
    if not value: raise HTTPException(404, 'Guided job not found')
    return {'job': value}

@router.get('/guided/jobs/{job_id}/next-action')
def guided_next_action(job_id: str):
    try: return {'job_id': job_id, 'next_action': guided.next_action(job_id)}
    except ValueError as exc: raise HTTPException(404, str(exc))

@router.post('/guided/jobs/{job_id}/resume-local-validation')
def guided_resume_local_validation(job_id: str, payload: GuidedResumeRequest):
    try: return {'job': guided_worker.resume_prewrite_local_validation(job_id, payload.family_hash)}
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/reconcile-accepted-parent')
def guided_reconcile_accepted_parent(job_id: str, payload: GuidedParentReconciliationRequest):
    """One guarded Listings GET; this endpoint cannot submit a listing write."""
    try:
        return {'job': guided_worker.reconcile_accepted_parent_get(job_id, payload.family_hash, getattr(router, '_auth_client', None), guided_worker.production_adapters())}
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/reconcile-prepared-children')
def guided_reconcile_prepared_children(job_id: str, payload: GuidedParentReconciliationRequest):
    """Guarded child GET reconciliation only; it has no PATCH path."""
    try:
        return guided_worker.reconcile_prepared_children_get(job_id, payload.family_hash, getattr(router, '_auth_client', None), guided_worker.production_adapters())
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/reconcile-accepted-di106')
def guided_reconcile_accepted_di106(job_id: str, payload: GuidedParentReconciliationRequest):
    """Guarded DI-106 Listings/Catalog reconciliation only; no write transport is reachable."""
    try:
        return {'job': guided_worker.reconcile_accepted_di106_get(job_id, payload.family_hash, getattr(router, '_auth_client', None), guided_worker.production_adapters())}
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/prepare-child-write-authorization')
def guided_prepare_child_write_authorization(job_id: str, payload: GuidedParentReconciliationRequest):
    try: return guided_worker.prepare_child_write_authorization(job_id, payload.family_hash, getattr(router, '_auth_client', None), guided_worker.production_adapters())
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/confirm-child-write-authorization')
def guided_confirm_child_write_authorization(job_id: str, payload: GuidedChildWriteConfirmation):
    try: return guided_worker.confirm_child_write_authorization(job_id, payload.family_hash, payload.plan_hash, payload.confirmation)
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/start')
def guided_start(job_id: str):
    # No auth client is supplied in Phase 2 HTTP endpoints: deployment must wire
    # an explicit worker transport; this route remains safe in local mode.
    try: return guided_worker.start(job_id, getattr(router, '_auth_client', None), guided_worker.production_adapters())
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/process-next')
def guided_process_next(job_id: str):
    try: return guided_worker.process_next_safe_action(job_id, getattr(router, '_auth_client', None), guided_worker.production_adapters())
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/wake')
def guided_wake(job_id: str):
    try: return guided_worker.wake(job_id, getattr(router, '_auth_client', None), guided_worker.production_adapters())
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.post('/guided/jobs/{job_id}/cancel')
def guided_cancel(job_id: str):
    try: return {'job': guided_worker.cancel(job_id)}
    except ValueError as exc: raise HTTPException(409, str(exc))

@router.get('/guided/jobs/{job_id}/progress')
def guided_progress(job_id: str):
    try: return guided_worker.status(job_id)
    except ValueError as exc: raise HTTPException(404, str(exc))

@router.get('/feature-flags')
def feature_flags(): return {'write_operations_enabled':False,'flags':{**WRITE_FEATURE_FLAGS,'amazon_validation_preview':runtime_settings.enabled()}}
@router.get('/validation-mode')
def validation_mode(): return runtime_settings.status()
@router.post('/validation-mode/enable')
def enable_validation_mode(payload:ValidationModeEnable):
    try: return runtime_settings.enable(payload.confirmation)
    except ValueError as exc: raise HTTPException(409,str(exc))
@router.post('/validation-mode/disable')
def disable_validation_mode(): return runtime_settings.disable()
@router.get('/parent-creation-mode')
def parent_creation_mode(snapshot_id:str|None=None): return runtime_settings.parent_creation_status(snapshot_id)
@router.post('/parent-creation-mode/arm')
def arm_parent_creation(payload:ParentArmRequest):
    if payload.confirmation != 'ARM PARENT CREATION': raise HTTPException(409,'Confirmation must exactly equal ARM PARENT CREATION')
    if runtime_settings.enabled(): raise HTTPException(409,'Disable Validation Mode before arming Parent Creation Mode')
    snapshot=get_snapshot(payload.snapshot_id)
    if not snapshot: raise HTTPException(404,'Approval snapshot not found')
    context=_checked_context(snapshot['marketplace_id'],snapshot['vendor_code'],snapshot['vendor_context_id'])
    result=parent_submission.gates(snapshot,context)
    failed=[name for name,value in result.items() if name not in ('create_parent_enabled','ready') and not value]
    if failed: raise HTTPException(409,'Parent Creation Mode safety gates failed: '+', '.join(failed))
    auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'SP-API authentication is not configured')
    ok,evidence=parent_submission.final_preflight(auth,snapshot,'FK6EI')
    preflight=create_preflight(snapshot['id'],'passed' if ok else 'blocked',evidence)
    if not ok: raise HTTPException(409,'Fresh arm-time GET-only preflight failed')
    try: parent_submission._assert_current(snapshot)
    except ValueError as exc: raise HTTPException(409,str(exc))
    result=parent_submission.gates(snapshot,context)
    failed=[name for name,value in result.items() if name not in ('create_parent_enabled','ready') and not value]
    if failed: raise HTTPException(409,'Parent Creation Mode safety gates failed after preflight: '+', '.join(failed))
    with get_db_connection() as conn:
        rows=conn.execute("SELECT expires_at_utc FROM pc_validation_preview_evidence WHERE snapshot_id=? AND payload_hash=?",(snapshot['id'],snapshot['payload_hash'])).fetchall()
    if not rows: raise HTTPException(409,'Current validation evidence is required')
    try: return runtime_settings.arm_parent_creation(snapshot,min(row['expires_at_utc'] for row in rows),preflight['expires_at_utc'],payload.confirmation)
    except ValueError as exc: raise HTTPException(409,str(exc))
@router.post('/parent-creation-mode/disarm')
def disarm_parent_creation(payload:ParentArmRequest):
    return runtime_settings.disarm_parent_creation(payload.snapshot_id)
@router.get('/child-attachment/readiness')
def child_attachment_readiness(snapshot_id:str):
    snapshot=get_snapshot_readonly(snapshot_id)
    if not snapshot: raise HTTPException(404,'Approval snapshot not found')
    return child_submission.readiness(snapshot)
@router.post('/child-attachment/arm')
def arm_child_attachment(payload:ChildAttachmentRequest):
    snapshot=get_snapshot(payload.snapshot_id)
    if not snapshot: raise HTTPException(404,'Approval snapshot not found')
    auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'SP-API authentication is not configured')
    try:return child_submission.arm(auth,snapshot,payload.parent_job_id,payload.confirmation)
    except ValueError as exc: raise HTTPException(409,str(exc))
@router.post('/child-attachment/validation-preview')
def child_attachment_validation(payload:ChildAttachmentRequest):
    if not runtime_settings.enabled(): raise HTTPException(403,'Validation Mode is required')
    snapshot=get_snapshot(payload.snapshot_id); auth=getattr(router,'_auth_client',None)
    if not snapshot or not auth: raise HTTPException(409,'Current snapshot and authentication are required')
    try:return {'results':child_submission.validate_children(auth,snapshot,payload.confirmation)}
    except ValueError as exc: raise HTTPException(409,str(exc))
@router.post('/child-attachment/execute')
def execute_child_attachment(payload:ChildAttachmentRequest):
    if runtime_settings.enabled(): raise HTTPException(409,'Disable Validation Mode before child attachment')
    snapshot=get_snapshot(payload.snapshot_id); auth=getattr(router,'_auth_client',None)
    if not snapshot or not auth: raise HTTPException(409,'Current snapshot and authentication are required')
    try:return {'items':child_submission.execute(auth,snapshot,payload.confirmation)}
    except ValueError as exc: raise HTTPException(409,str(exc))
@router.get('/capability-status')
def capability_status(marketplace_id:str, vendor_code:str):
    context=_checked_context(marketplace_id,vendor_code)
    return {'read_access':'verified' if context['verified'] else 'unverified', 'product_listing_role':'unconfirmed',
            'write_endpoint_authorization':'not_proven', 'persistent_writes':'disabled', 'amazon_validation_preview':runtime_settings.enabled(), 'write_operations_enabled':False}
@router.get('/vendor-contexts')
def contexts(marketplace_id:str|None=None): return {'items':vendor_contexts(marketplace_id)}
@router.get('/marketplaces')
def marketplaces():
    labels={'A2VIGQ35RCS4UG':'Amazon UAE','A1PA6795UKMFR9':'Amazon Germany','A13V1IB3VIYZZH':'Amazon Spain','A1F83G8C2ARO7P':'Amazon UK','A1VC38T7YXB528':'Amazon Japan'}
    ids=list(config.MARKETPLACE_IDS or [])
    return {'items':[{'marketplace_id':value,'label':f"{labels.get(value, 'Amazon Marketplace')} — {value}",'region':'eu' if value in {'A2VIGQ35RCS4UG','A1PA6795UKMFR9','A13V1IB3VIYZZH','A1F83G8C2ARO7P'} else 'fe' if value=='A1VC38T7YXB528' else 'na'} for value in ids]}
@router.post('/vendor-contexts')
def save_context(payload:VendorContext):
    upsert_vendor_context(payload.marketplace_id,payload.vendor_code,False,payload.display_name); return {'ok':True,'verified':False}
@router.post('/vendor-contexts/verify')
def verify_context(payload:VendorContext):
    if payload.marketplace_id not in config.MARKETPLACE_IDS: raise HTTPException(400,'Marketplace mismatch')
    vendor_code=payload.vendor_code.strip()
    if not vendor_code: raise HTTPException(400,'Invalid vendor code')
    auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'Authentication failed')
    try: result=verify_vendor_access(auth,payload.marketplace_id,vendor_code)
    except Exception as exc: result={'verified':False,'capability_result':None,'last_error':f'Temporary Amazon error: {exc}'}
    upsert_vendor_context(payload.marketplace_id,vendor_code,result['verified'],payload.display_name,region=next((x['region'] for x in marketplaces()['items'] if x['marketplace_id']==payload.marketplace_id),None),capability_result=result['capability_result'],last_error=result['last_error'])
    return result
@router.post('/vendor-contexts/{context_id}/status')
def context_status(context_id:int,payload:ContextStatus):
    try: set_vendor_context_active(context_id,payload.active)
    except ValueError as exc: raise HTTPException(409,str(exc))
    return {'ok':True}
@router.get('/vendor-code-suggestions')
def vendor_code_suggestions(marketplace_id:str):
    # PO data is intentionally not trusted as an identity source; no candidate is verified here.
    return {'items':[],'message':'Suggested from Vendor POs'}
@router.post('/parse-asins')
def parse(payload:Dict[str,str]): return parse_asins(payload.get('text',''))
@router.post('/resolve')
def resolve_products(payload:ResolveRequest):
    _checked_context(payload.marketplace_id,payload.vendor_code)
    auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'SP-API authentication is not configured')
    parsed=parse_asins('\n'.join(payload.asins)); items=[]
    try: items=resolve_many(auth,payload.marketplace_id,payload.vendor_code,parsed['asins'])
    except Exception as exc: items=[{'asin':asin,'status':'Blocked','status_detail':'Listings Request Failed','error':str(exc),'current_parent':'Unknown'} for asin in parsed['asins']]
    return {**parsed,'items':items}
@router.post('/product-type-definition')
def ptd(payload:PtdRequest):
    _checked_context(payload.marketplace_id,payload.vendor_code); auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'SP-API authentication is not configured')
    try:
        schema, trace=get_definition(auth,payload.marketplace_id,payload.vendor_code,payload.product_type,payload.parentage_level,payload.force)
        return {'product_type':payload.product_type,'parentage_level':payload.parentage_level,'variation_themes':theme_options(schema),'schema':schema,'trace':trace}
    except ProductTypeDefinitionError as exc: raise HTTPException(exc.status_code,f'Product Type Definition unavailable (Amazon HTTP {exc.status_code}): {exc.message}')
    except Exception as exc: raise HTTPException(502,f'Product Type Definition unavailable: {exc}')
@router.post('/drafts')
def create_draft(payload:DraftPayload):
    d=payload.data; _checked_context(d.get('marketplace_id',''),d.get('vendor_code',''))
    return {'draft':save_draft(d)}
@router.get('/drafts')
def drafts(include_archived:bool=False): return {'items':list_drafts(include_archived)}
@router.post('/drafts/{draft_id}/duplicate')
def duplicate_saved_draft(draft_id:str):
    value=duplicate_draft(draft_id)
    if not value: raise HTTPException(404,'Draft not found')
    return {'draft':value}
@router.post('/drafts/{draft_id}/archive')
def archive_saved_draft(draft_id:str): archive_draft(draft_id); return {'ok':True}
@router.delete('/drafts/{draft_id}')
def delete_saved_draft(draft_id:str):
    try: delete_draft(draft_id)
    except ValueError as exc: raise HTTPException(409,str(exc))
    return {'ok':True}
@router.post('/preview')
def preview(payload:PreviewRequest):
    data=payload.data
    context=_checked_context(data.get('marketplace_id',''),data.get('vendor_code',''),data.get('vendor_context_id'))
    request_id=str(uuid.uuid4())
    try:
        auth=getattr(router,'_auth_client',None)
        if not auth: raise HTTPException(503,'SP-API authentication is not configured')
        product_type=data.get('product_type')
        if not product_type: raise HTTPException(422,'A detected Product Type is required')
        parent_schema,parent_trace=get_definition(auth,data['marketplace_id'],context['vendor_code'],product_type,'PARENT')
        child_schema,child_trace=get_definition(auth,data['marketplace_id'],context['vendor_code'],product_type,'CHILD')
        themes=set(theme_options(parent_schema)) & set(theme_options(child_schema))
        if data.get('variation_theme') not in themes:
            raise HTTPException(422,'The selected Variation Theme is not supported by both server-resolved PTDs')
        mismatched=[item.get('asin') for item in data.get('items',[]) if item.get('product_type') and item.get('product_type') != product_type]
        if mismatched: raise HTTPException(422,'Child Product Type does not match the server-bound Product Type')
        existing={}
        for item in data.get('items',[]):
            if item.get('vendor_sku'): existing[item['vendor_sku']]=listing_item_attributes(auth,data['marketplace_id'],context['vendor_code'],item['vendor_sku'])
        result=build_preview(data,parent_schema,child_schema,existing)
        parent_attributes=result['parent']['body']['attributes']
        result['validation_trace']={
            'parent_ptd_request':parent_trace,
            'child_ptd_request':child_trace,
            'parent':{'layer':'Parent full PUT validation','object':'proposed Parent putListingsItem attributes','parentage_level':parent_attributes.get('parentage_level'),'variation_theme':parent_attributes.get('variation_theme')},
            'children':[{'scope':child.get('asin') or child.get('sku'),'layer':'Child PATCH document validation','object':'proposed patchListingsItem body','patch_paths':[patch.get('path') for patch in child['body'].get('patches',[])]} for child in result['children']],
        }
        errors=full_schema_errors(parent_schema,parent_attributes,'Parent',layer='Parent full PUT validation',validation_object='proposed Parent putListingsItem attributes')
        for error in errors:
            if error['path'].split('/')[1:2] == ['child_parent_sku_relationship']:
                error['ptd_inconsistency']='Parent PTD requires a child relationship after parent selectors were applied'
        for child in result['children']:
            child_errors,_=patch_errors(child_schema,child['body']['patches'],(existing.get(child['sku']) or {}).get('attributes') or {},child.get('asin') or child.get('sku'))
            errors.extend(child_errors)
            for name in child.get('inactive_variation_keys',[]):
                errors.append({'scope':child.get('asin') or child.get('sku'),'layer':'Child PATCH document validation','validation_object':'proposed patchListingsItem body','field':name,'message':'is not active for the selected variation theme','path':f'/attributes/{name}','instance_path':f'/attributes/{name}','schema_path':'/properties'})
        result['validation']={'passed':not errors,'errors':{'Parent':[x for x in errors if x['scope']=='Parent'], **{str(item.get('scope')):[x for x in errors if x['scope']==item.get('scope')] for item in errors if item['scope']!='Parent'}}}
        if not result['validation']['passed']:
            errors=[{'scope':scope.lower(),'field':error['path'].lstrip('/').replace('attributes/',''),'message':f"{error['field']} {error['message']}",'technical':{'layer':error.get('layer'),'object':error.get('validation_object'),'instance_path':error.get('instance_path'),'schema_path':error.get('schema_path'),'condition_path':error.get('condition_path'),'ptd_inconsistency':error.get('ptd_inconsistency')}} for scope,values in result['validation']['errors'].items() for error in values]
            return JSONResponse(status_code=422,content={'ok':False,'status':'not_ready','errors':errors,'preview':result,'request_id':request_id})
        draft=save_draft(data)
        parent_meta=ptd_metadata(parent_schema,parent_trace,'PARENT'); child_meta=ptd_metadata(child_schema,child_trace,'CHILD')
        snapshot=create_snapshot(draft['id'],context,product_type,data['variation_theme'],result,parent_meta,child_meta)
        result['approval_snapshot']={'id':snapshot['id'],'draft_id':draft['id'],'payload_hash':snapshot['payload_hash'],'expires_at_utc':snapshot['expires_at_utc']}
        return {'ok':True,'status':'structurally_ready','preview':result,'snapshot':result['approval_snapshot'],'write_operations_enabled':False,'request_id':request_id}
    except HTTPException: raise
    except Exception:
        logger.exception('Parent-child preview failed request_id=%s draft_id=%s',request_id,data.get('id') or 'unsaved')
        return JSONResponse(status_code=500,content={'ok':False,'status':'error','message':'Preview service encountered an unexpected error. Check application logs and retry.','request_id':request_id})
@router.post('/preflight')
def preflight(payload:PreflightRequest):
    snapshot=get_snapshot(payload.snapshot_id)
    if not snapshot: raise HTTPException(404,'Approval snapshot not found')
    if snapshot['status']!='FRESH': raise HTTPException(409,'Approval snapshot is stale; regenerate the preview')
    if snapshot['expires_at_utc'] <= __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(): raise HTTPException(409,'Approval snapshot expired; regenerate the preview')
    context=_checked_context(snapshot['marketplace_id'],snapshot['vendor_code'],snapshot['vendor_context_id'])
    auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'SP-API authentication is not configured')
    parent=listing_item_attributes(auth,snapshot['marketplace_id'],context['vendor_code'],snapshot['parent_sku'])
    parent_status='Available' if parent.get('status')==404 else 'Already exists' if parent.get('status')==200 else 'Unknown'
    children=[]; passed=parent_status=='Available'
    for child in snapshot['payload'].get('children',[]):
        listing=listing_item_attributes(auth,snapshot['marketplace_id'],context['vendor_code'],child['sku'])
        catalog,details=catalog_lookup(auth,snapshot['marketplace_id'],child['asin'])
        types=(catalog or {}).get('productTypes') or []; product_type=types[0].get('productType') if types else None
        actual_parent=current_parent(catalog or {}) if catalog else 'Unknown'
        summaries=listing.get('summaries') or []; asin_matches=any(row.get('asin')==child['asin'] for row in summaries)
        issues=listing.get('issues') or []; blocking=[{'code':x.get('code'),'message':x.get('message')} for x in issues if x.get('severity','ERROR').upper() in ('ERROR','FATAL')]
        variation_ok=True
        for patch in child['body'].get('patches',[]):
            name=patch.get('path','').rsplit('/',1)[-1]
            if name not in ('parentage_level','variation_theme','child_parent_sku_relationship') and (listing.get('attributes') or {}).get(name) != patch.get('value'): variation_ok=False
        conclusion='Confirmed None' if listing.get('status')==200 and catalog and asin_matches and product_type==snapshot['product_type'] and actual_parent is None and variation_ok and not blocking else 'Blocked'
        passed &= conclusion=='Confirmed None'
        children.append({'asin':child['asin'],'sku':child['sku'],'conclusion':conclusion,'listings_http_status':listing.get('status'),'listings_request_id':listing.get('request_id'),'catalog_http_status':details.get('http_status'),'catalog_request_id':details.get('request_id'),'product_type':product_type,'current_parent':actual_parent,'issues':blocking})
    evidence=redact({'parent_sku':snapshot['parent_sku'],'parent_sku_status':parent_status,'parent_lookup_status':parent.get('status'),'parent_request_id':parent.get('request_id'),'children':children})
    record=create_preflight(snapshot['id'],'passed' if passed else 'blocked',evidence)
    return {'ok':passed,'status':record['status'],'preflight':record,'write_operations_enabled':False}
@router.post('/snapshots/{snapshot_id}/stale')
def stale_snapshot(snapshot_id:str):
    snapshot=get_snapshot(snapshot_id)
    if not snapshot: raise HTTPException(404,'Approval snapshot not found')
    mark_snapshot_stale(snapshot_id)
    runtime_settings.disarm_parent_creation(snapshot_id)
    return {'ok':True,'status':'stale','write_operations_enabled':False}
@router.post('/validation-preview')
def amazon_validation_preview(payload:ValidationPreviewRequest):
    """The only non-GET Amazon operation, permanently locked to VALIDATION_PREVIEW."""
    if not runtime_settings.enabled(): raise HTTPException(403,'Amazon validation preview is disabled')
    if payload.confirmation != 'VALIDATE': raise HTTPException(400,'Type VALIDATE to confirm Amazon validation preview')
    snapshot=get_snapshot(payload.snapshot_id)
    if not snapshot or snapshot['status']!='FRESH': raise HTTPException(409,'A current immutable snapshot is required')
    if checksum(snapshot['payload']) != snapshot['payload_hash']: raise HTTPException(409,'Immutable snapshot hash mismatch')
    if checksum(snapshot['parent_ptd']['schema']) != snapshot['parent_ptd']['schema_checksum'] or checksum(snapshot['child_ptd']['schema']) != snapshot['child_ptd']['schema_checksum']: raise HTTPException(409,'PTD checksum changed; regenerate preview')
    preflight=latest_preflight(snapshot['id'])
    if not preflight or preflight['expired'] or preflight['status']!='passed': raise HTTPException(409,'A passed final GET-only preflight from the last 15 minutes is required')
    context=_checked_context(snapshot['marketplace_id'],snapshot['vendor_code'],snapshot['vendor_context_id'])
    if snapshot['marketplace_id']!='A2VIGQ35RCS4UG': raise HTTPException(409,'Validation preview is restricted to Amazon UAE')
    auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'SP-API authentication is not configured')
    if context.get('vendor_code') != 'FK6EI' or snapshot.get('vendor_code') != 'FK6EI' or context.get('id') != snapshot.get('vendor_context_id'): raise HTTPException(409,'Validation Preview is restricted to verified FK6EI snapshot context')
    parent=validation_preview_put_listing(auth,context['vendor_code'],snapshot['parent_sku'],snapshot['marketplace_id'],snapshot['payload']['parent']['body'])
    results=[save_validation_evidence(snapshot['id'],snapshot['payload_hash'],'PUT',snapshot['parent_sku'],parent)]
    if parent['classification'] not in ('PASSED','PASSED_WITH_WARNINGS'):
        return {'ok':False,'status':parent['classification'],'results':results,'write_operations_enabled':False}
    for child in snapshot['payload'].get('children',[]):
        result=validation_preview_patch_listing(auth,context['vendor_code'],child['sku'],snapshot['marketplace_id'],child['body'])
        results.append(save_validation_evidence(snapshot['id'],snapshot['payload_hash'],'PATCH',child['sku'],result))
        if result['classification'] not in ('PASSED','PASSED_WITH_WARNINGS'): break
    passed=all(item['classification'] in ('PASSED','PASSED_WITH_WARNINGS') for item in results)
    return {'ok':passed,'status':'PASSED' if passed else results[-1]['classification'],'results':results,'message':'No listing was created or modified. This was Amazon VALIDATION_PREVIEW mode.','write_operations_enabled':False}

@router.get('/parent-submission/readiness')
def parent_submission_readiness(snapshot_id:str):
    """Server-resolved safety status; no browser payload is accepted."""
    try:
        snapshot=get_snapshot_readonly(snapshot_id)
        if not snapshot: raise HTTPException(404,'Approval snapshot not found')
        context=verified_vendor_context_readonly(snapshot['marketplace_id'],snapshot['vendor_code'],snapshot['vendor_context_id'])
        if not context: raise RuntimeError('required context unavailable')
        gates=parent_submission.gates(snapshot,context,readonly=True)
        preflight=latest_preflight_readonly(snapshot['id'])
        from services.db import get_readonly_db_connection
        with get_readonly_db_connection() as conn:
            validation=[dict(row) for row in conn.execute("SELECT operation,sku,classification,completed_at_utc,expires_at_utc FROM pc_validation_preview_evidence WHERE snapshot_id=? AND payload_hash=? ORDER BY completed_at_utc",(snapshot['id'],snapshot['payload_hash']))]
    except HTTPException:
        raise
    except Exception:
        return {'snapshot_id':snapshot_id,'gates':{'ready':False,'reason':'Application restart/migration required'},'validation_evidence':[],'final_preflight_status':None,'final_preflight_at_utc':None,'write_operations_enabled':False}
    return {'snapshot_id':snapshot['id'],'parent_sku':snapshot['parent_sku'],'marketplace':'UAE','marketplace_id':snapshot['marketplace_id'],'vendor_code':'FK6EI','product_type':snapshot['product_type'],'variation_theme':snapshot['variation_theme'],'payload_hash':snapshot['payload_hash'],'snapshot_expires_at_utc':snapshot['expires_at_utc'],'validation_evidence':validation,'final_preflight_status':preflight.get('status') if preflight else None,'final_preflight_at_utc':preflight.get('checked_at_utc') if preflight else None,'gates':gates,'write_operations_enabled':WRITE_FEATURE_FLAGS['create_parent'] is True}

@router.post('/parent-submission')
def submit_parent(payload:ParentSubmissionRequest):
    """One parent PUT only.  Children are never passed to any transport here."""
    if runtime_settings.enabled(): raise HTTPException(409,'Disable Amazon Validation Preview mode before parent submission')
    snapshot=get_snapshot(payload.snapshot_id)
    if not snapshot: raise HTTPException(404,'Approval snapshot not found')
    context=_checked_context(snapshot['marketplace_id'],snapshot['vendor_code'],snapshot['vendor_context_id'])
    auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'SP-API authentication is not configured')
    try:
        result=parent_submission.execute(auth,snapshot,context,payload.confirmation,create_preflight)
    except ValueError as exc:
        raise HTTPException(409,str(exc))
    return {**result,'message':'Parent submitted; awaiting GET-only verification. Children were not attached.'}
@router.get('/drafts/{draft_id}')
def draft(draft_id:str):
    value=get_draft(draft_id)
    if not value: raise HTTPException(404,'Draft not found')
    return {'draft':value}
@router.get('/jobs')
def jobs(status:str|None=None): return {'items':tracker.jobs(status),'write_operations_enabled':False}
@router.get('/jobs/{job_id}')
def job_detail(job_id:str):
    value=tracker.detail(job_id)
    if not value: raise HTTPException(404,'Tracker job not found')
    return {'job':value,'write_operations_enabled':False}
@router.get('/jobs/{job_id}/events')
def job_events(job_id:str):
    value=tracker.detail(job_id)
    if not value: raise HTTPException(404,'Tracker job not found')
    return {'items':value['events'],'write_operations_enabled':False}
@router.post('/jobs/{job_id}/refresh')
def refresh_job(job_id:str):
    # The only post-submission operation is Listings/Catalog GET verification.
    try: tracker.record_poll(job_id,1,None,None,[])
    except ValueError as exc: raise HTTPException(409,str(exc))
    job=tracker.detail(job_id)
    if not job or job.get('status') not in ('PARENT_SUBMITTED','SUBMISSION_UNKNOWN','PARENT_VERIFYING'):
        raise HTTPException(409,'Only submitted parent jobs may be refreshed')
    auth=getattr(router,'_auth_client',None)
    if not auth: raise HTTPException(503,'SP-API authentication is not configured')
    try:
        evidence=tracker.verify_submitted_job(auth,job_id,2,job['marketplace_id'],'FK6EI')
    except ValueError as exc: raise HTTPException(409,str(exc))
    return {'ok':True,'mode':'GET-only verification','evidence':evidence,'write_operations_enabled':False}

def register_parent_child_routes(app:FastAPI,auth_client):
    ensure_parent_child_schema(); setattr(router,'_auth_client',auth_client); app.include_router(router)
