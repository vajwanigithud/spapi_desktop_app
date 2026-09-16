import json
import uuid
from datetime import datetime, timezone
from services.db import get_db_connection, get_readonly_db_connection
from services.parent_child_migrations import ensure_parent_child_schema

def utcnow(): return datetime.now(timezone.utc).isoformat()

def vendor_contexts(marketplace_id=None):
    ensure_parent_child_schema()
    with get_db_connection() as c:
        sql = "SELECT id,marketplace_id,vendor_code,verified,display_name,credential_context,region,active,verified_at_utc,capability_result,last_error FROM pc_vendor_contexts"
        params = () if marketplace_id is None else (marketplace_id,)
        if marketplace_id is not None: sql += " WHERE marketplace_id=?"
        return [dict(r) for r in c.execute(sql + " ORDER BY marketplace_id,display_name,vendor_code", params)]

def verified_vendor_context_readonly(marketplace_id, vendor_code, context_id):
    """Read the immutable snapshot context without schema initialization."""
    with get_readonly_db_connection() as c:
        row = c.execute("SELECT id,marketplace_id,vendor_code,verified,active FROM pc_vendor_contexts WHERE id=? AND marketplace_id=? AND vendor_code=?", (context_id, marketplace_id, vendor_code)).fetchone()
    return dict(row) if row and row['verified'] and row['active'] else None

def upsert_vendor_context(marketplace_id, vendor_code, verified, display_name=None, region=None, capability_result=None, last_error=None, active=True):
    ensure_parent_child_schema(); now=utcnow()
    with get_db_connection() as c:
        c.execute("INSERT INTO pc_vendor_contexts(marketplace_id,vendor_code,verified,display_name,credential_context,region,active,verified_at_utc,capability_result,last_error,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(marketplace_id,vendor_code) DO UPDATE SET verified=excluded.verified,display_name=excluded.display_name,region=excluded.region,active=excluded.active,verified_at_utc=excluded.verified_at_utc,capability_result=excluded.capability_result,last_error=excluded.last_error,updated_at_utc=excluded.updated_at_utc", (marketplace_id,vendor_code,int(verified),display_name,'default',region,int(active),now if verified else None,capability_result,last_error,now,now)); c.commit()

def set_vendor_context_active(context_id, active):
    ensure_parent_child_schema()
    with get_db_connection() as c:
        if not active:
            row = c.execute("SELECT marketplace_id,vendor_code FROM pc_vendor_contexts WHERE id=?", (context_id,)).fetchone()
            if row and (c.execute("SELECT 1 FROM pc_drafts WHERE marketplace_id=? AND vendor_code=? LIMIT 1", (row['marketplace_id'], row['vendor_code'])).fetchone() or c.execute("SELECT 1 FROM pc_jobs WHERE marketplace_id=? AND vendor_code=? LIMIT 1", (row['marketplace_id'], row['vendor_code'])).fetchone()):
                raise ValueError("This vendor context is referenced by a draft or tracker job and cannot be disabled")
        c.execute("UPDATE pc_vendor_contexts SET active=?,updated_at_utc=? WHERE id=?", (int(active), utcnow(), context_id)); c.commit()

def save_draft(data):
    ensure_parent_child_schema(); now=utcnow(); did=data.get('id') or str(uuid.uuid4())
    # A saved payload is an edit boundary.  Approval snapshots are immutable and
    # must be regenerated after any draft save, never silently reused.
    if data.get('id'):
        from services.parent_child_safety import mark_draft_snapshots_stale
        mark_draft_snapshots_stale(data['id'])
    fields=(did,data['marketplace_id'],data['vendor_code'],data.get('parent_mode','new'),data.get('parent_sku'),data.get('parent_title'),data.get('product_type'),data.get('variation_theme'),data.get('status','Incomplete'),json.dumps(data.get('preview')) if data.get('preview') else None,json.dumps(data),now,now)
    with get_db_connection() as c:
        c.execute("INSERT INTO pc_drafts(id,marketplace_id,vendor_code,parent_mode,parent_sku,parent_title,product_type,variation_theme,status,preview_json,data_json,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET marketplace_id=excluded.marketplace_id,vendor_code=excluded.vendor_code,parent_mode=excluded.parent_mode,parent_sku=excluded.parent_sku,parent_title=excluded.parent_title,product_type=excluded.product_type,variation_theme=excluded.variation_theme,status=excluded.status,preview_json=excluded.preview_json,data_json=excluded.data_json,archived=0,updated_at_utc=excluded.updated_at_utc",fields)
        c.execute("DELETE FROM pc_draft_items WHERE draft_id=?", (did,))
        for position,item in enumerate(data.get('items',[])):
            c.execute("INSERT INTO pc_draft_items(draft_id,asin,vendor_sku,title,image_url,product_type,current_parent,variation_values_json,status,status_detail,confirmed,evidence_json,position,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(did,item['asin'],item.get('vendor_sku'),item.get('title'),item.get('image_url'),item.get('product_type'),item.get('current_parent'),json.dumps(item.get('variation_values',{})),item.get('status','Needs Review'),item.get('status_detail'),0,json.dumps(item.get('evidence',{})),position,now,now))
        c.commit()
    return get_draft(did)

def get_draft(draft_id):
    ensure_parent_child_schema()
    with get_db_connection() as c:
        d=c.execute("SELECT * FROM pc_drafts WHERE id=?",(draft_id,)).fetchone()
        if not d:return None
        out=dict(d); saved=json.loads(out.pop('data_json') or '{}'); out.update(saved); out['preview']=json.loads(out.pop('preview_json') or 'null'); out['items']=[]
        for r in c.execute("SELECT * FROM pc_draft_items WHERE draft_id=? ORDER BY position,id",(draft_id,)):
            x=dict(r); x['variation_values']=json.loads(x.pop('variation_values_json') or '{}'); x['evidence']=json.loads(x.pop('evidence_json') or '{}'); x.pop('confirmed',None); out['items'].append(x)
        return out

def list_drafts(include_archived=False):
    ensure_parent_child_schema()
    with get_db_connection() as c:
        where="" if include_archived else " WHERE archived=0"
        rows=c.execute("SELECT d.*,COUNT(i.id) AS child_count FROM pc_drafts d LEFT JOIN pc_draft_items i ON i.draft_id=d.id"+where+" GROUP BY d.id ORDER BY d.updated_at_utc DESC").fetchall()
    return [dict(row) for row in rows]

def archive_draft(draft_id):
    ensure_parent_child_schema()
    with get_db_connection() as c: c.execute("UPDATE pc_drafts SET archived=1,updated_at_utc=? WHERE id=?",(utcnow(),draft_id)); c.commit()

def duplicate_draft(draft_id):
    source=get_draft(draft_id)
    if not source: return None
    source.pop('id',None); source['parent_sku']=(source.get('parent_sku') or '') + '-COPY'; source['status']='Incomplete'; source.pop('preview',None)
    return save_draft(source)

def delete_draft(draft_id):
    ensure_parent_child_schema()
    with get_db_connection() as c:
        if c.execute("SELECT 1 FROM parent_child_jobs WHERE draft_id=? LIMIT 1",(draft_id,)).fetchone() or c.execute("SELECT 1 FROM pc_jobs WHERE draft_id=? LIMIT 1",(draft_id,)).fetchone(): raise ValueError("Draft is referenced by a tracker job and can only be archived")
        c.execute("DELETE FROM pc_draft_items WHERE draft_id=?",(draft_id,)); c.execute("DELETE FROM pc_drafts WHERE id=?",(draft_id,)); c.commit()

def list_jobs():
    ensure_parent_child_schema()
    with get_db_connection() as c:return [dict(r) for r in c.execute("SELECT * FROM pc_jobs ORDER BY created_at_utc DESC")]
