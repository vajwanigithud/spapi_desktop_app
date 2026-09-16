"""Narrow, fail-closed local runtime control for Amazon validation preview."""
from datetime import datetime, timezone
import json, uuid
from services.db import get_db_connection, get_readonly_db_connection
from services.parent_child_validation import WRITE_FEATURE_FLAGS

SETTING='amazon_validation_preview_enabled'
PARENT_ARM='parent_creation_armed'
PERSISTENT=('create_parent','attach_child','detach_child','move_child','bulk_feed')
def _now(): return datetime.now(timezone.utc).isoformat()
def _writes_off(): return all(WRITE_FEATURE_FLAGS[name] is False for name in PERSISTENT)
def enabled():
    if not _writes_off(): return False
    try:
        with get_readonly_db_connection() as c:
            row=c.execute('SELECT value FROM pc_runtime_settings WHERE name=?',(SETTING,)).fetchone()
    except Exception: return False
    return bool(row and row['value'] == 'true')
def status(): return {'enabled':enabled(),'persistent_writes_disabled':_writes_off()}
def parent_arm(snapshot_id=None):
    try:
        with get_readonly_db_connection() as c:
            sql="SELECT * FROM pc_parent_creation_arms WHERE consumed_at_utc IS NULL AND expires_at_utc>?"; args=[_now()]
            if snapshot_id: sql += " AND snapshot_id=?"; args.append(snapshot_id)
            row=c.execute(sql,args).fetchone()
    except Exception: return None
    return dict(row) if row else None
def parent_creation_status(snapshot_id=None):
    arm=parent_arm(snapshot_id); return {'armed':bool(arm),'arm':{key:arm[key] for key in ('id','snapshot_id','parent_sku','payload_hash','expires_at_utc')} if arm else None}
def arm_parent_creation(snapshot, validation_expiry, preflight_expiry, confirmation):
    if confirmation != 'ARM PARENT CREATION': raise ValueError('Confirmation must exactly equal ARM PARENT CREATION')
    if enabled(): raise ValueError('Disable Validation Mode before arming Parent Creation Mode')
    expiry=min(datetime.fromisoformat(snapshot['expires_at_utc']),datetime.fromisoformat(validation_expiry),datetime.fromisoformat(preflight_expiry),datetime.now(timezone.utc).replace(microsecond=0)+__import__('datetime').timedelta(minutes=5)).isoformat()
    if datetime.fromisoformat(expiry) <= datetime.now(timezone.utc): raise ValueError('Parent Creation Mode evidence or snapshot expired during preflight')
    arm_id=str(uuid.uuid4()); children=[{'asin':x['asin'],'sku':x['sku']} for x in snapshot['payload'].get('children',[])]
    with get_db_connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM pc_parent_creation_arms WHERE consumed_at_utc IS NULL AND expires_at_utc>?",(_now(),)).fetchone(): c.rollback(); raise ValueError('A Parent Creation Mode arm already exists; disarm it first')
        c.execute("INSERT INTO pc_parent_creation_arms(id,snapshot_id,payload_hash,marketplace_id,vendor_context_id,vendor_code,parent_sku,children_json,validation_expires_at_utc,snapshot_expires_at_utc,preflight_expires_at_utc,armed_at_utc,expires_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(arm_id,snapshot['id'],snapshot['payload_hash'],snapshot['marketplace_id'],snapshot['vendor_context_id'],snapshot['vendor_code'],snapshot['parent_sku'],json.dumps(children),validation_expiry,snapshot['expires_at_utc'],preflight_expiry,_now(),expiry))
        c.execute("INSERT INTO pc_parent_creation_arm_events(arm_id,action,created_at_utc) VALUES(?,?,?)",(arm_id,'armed',_now())); c.commit()
    return parent_creation_status(snapshot['id'])
def disarm_parent_creation(snapshot_id=None):
    with get_db_connection() as c:
        c.execute("UPDATE pc_parent_creation_arms SET consumed_at_utc=? WHERE consumed_at_utc IS NULL AND (? IS NULL OR snapshot_id=?)",(_now(),snapshot_id,snapshot_id)); c.commit()
    return parent_creation_status(snapshot_id)
def consume_parent_arm(snapshot):
    with get_db_connection() as c:
        c.execute('BEGIN IMMEDIATE'); row=c.execute("SELECT * FROM pc_parent_creation_arms WHERE snapshot_id=? AND consumed_at_utc IS NULL AND expires_at_utc>?",(snapshot['id'],_now())).fetchone()
        if not row or row['payload_hash'] != snapshot['payload_hash'] or row['parent_sku'] != snapshot['parent_sku'] or row['vendor_context_id'] != snapshot['vendor_context_id']: c.rollback(); raise ValueError('Parent Creation Mode is not armed for this exact snapshot')
        c.execute("UPDATE pc_parent_creation_arms SET consumed_at_utc=? WHERE id=?",(_now(),row['id'])); c.execute("INSERT INTO pc_parent_creation_arm_events(arm_id,action,created_at_utc) VALUES(?,?,?)",(row['id'],'consumed',_now())); c.commit()
    return dict(row)
def enable(confirmation):
    if confirmation != 'ENABLE VALIDATION': raise ValueError('Confirmation must exactly equal ENABLE VALIDATION')
    if not _writes_off(): raise ValueError('Validation Preview cannot be enabled while persistent writes are enabled')
    if parent_arm(): raise ValueError('Disarm Parent Creation Mode before enabling Validation Mode')
    try:
        with get_db_connection() as c:
            c.execute("INSERT INTO pc_runtime_settings(name,value,updated_at_utc) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at_utc=excluded.updated_at_utc",(SETTING,'true',_now()))
            c.execute('INSERT INTO pc_runtime_setting_events(name,action,created_at_utc) VALUES(?,?,?)',(SETTING,'enabled',_now()));c.commit()
    except Exception as exc: raise ValueError('Runtime settings are unavailable; restart so application migrations can run') from exc
    return status()
def disable():
    try:
        with get_db_connection() as c:
            c.execute("INSERT INTO pc_runtime_settings(name,value,updated_at_utc) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at_utc=excluded.updated_at_utc",(SETTING,'false',_now()))
            c.execute('INSERT INTO pc_runtime_setting_events(name,action,created_at_utc) VALUES(?,?,?)',(SETTING,'disabled',_now()));c.commit()
    except Exception as exc: raise ValueError('Runtime settings are unavailable; restart so application migrations can run') from exc
    return status()
