"""Additive schema for the read-only Parent–Child Manager."""
from services.db import get_db_connection

MIGRATIONS = [
    ("001_initial", """
    CREATE TABLE IF NOT EXISTS pc_vendor_contexts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, marketplace_id TEXT NOT NULL, vendor_code TEXT NOT NULL,
      verified INTEGER NOT NULL DEFAULT 0, display_name TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
      UNIQUE(marketplace_id, vendor_code));
    CREATE TABLE IF NOT EXISTS pc_drafts (
      id TEXT PRIMARY KEY, marketplace_id TEXT NOT NULL, vendor_code TEXT NOT NULL, parent_mode TEXT NOT NULL,
      parent_sku TEXT, parent_title TEXT, product_type TEXT, variation_theme TEXT, status TEXT NOT NULL DEFAULT 'Draft',
      created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pc_draft_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT NOT NULL, asin TEXT NOT NULL, vendor_sku TEXT,
      title TEXT, image_url TEXT, product_type TEXT, current_parent TEXT, variation_values_json TEXT,
      status TEXT NOT NULL, status_detail TEXT, confirmed INTEGER NOT NULL DEFAULT 0, evidence_json TEXT,
      created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL, UNIQUE(draft_id, asin),
      FOREIGN KEY(draft_id) REFERENCES pc_drafts(id));
    CREATE TABLE IF NOT EXISTS pc_jobs (
      id TEXT PRIMARY KEY, draft_id TEXT, marketplace_id TEXT NOT NULL, vendor_code TEXT NOT NULL, status TEXT NOT NULL,
      submitted_at_utc TEXT, last_checked_at_utc TEXT, next_check_at_utc TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pc_job_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT,
      payload_json TEXT, created_at_utc TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES pc_jobs(id));
    CREATE TABLE IF NOT EXISTS pc_ptd_cache (
      marketplace_id TEXT NOT NULL, vendor_code TEXT NOT NULL, product_type TEXT NOT NULL, parentage_level TEXT NOT NULL,
      schema_json TEXT NOT NULL, fetched_at_utc TEXT NOT NULL, PRIMARY KEY(marketplace_id,vendor_code,product_type,parentage_level));
    CREATE INDEX IF NOT EXISTS idx_pc_drafts_scope ON pc_drafts(marketplace_id,vendor_code);
    CREATE INDEX IF NOT EXISTS idx_pc_items_draft ON pc_draft_items(draft_id);
    """),
    ("002_vendor_context_capabilities", """
    ALTER TABLE pc_vendor_contexts ADD COLUMN credential_context TEXT NOT NULL DEFAULT 'default';
    ALTER TABLE pc_vendor_contexts ADD COLUMN region TEXT;
    ALTER TABLE pc_vendor_contexts ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE pc_vendor_contexts ADD COLUMN verified_at_utc TEXT;
    ALTER TABLE pc_vendor_contexts ADD COLUMN capability_result TEXT;
    ALTER TABLE pc_vendor_contexts ADD COLUMN last_error TEXT;
    """),
    ("003_draft_preview", "ALTER TABLE pc_drafts ADD COLUMN preview_json TEXT;"),
    ("004_submission_tracker_foundation", """
    CREATE TABLE IF NOT EXISTS parent_child_jobs (
      id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, marketplace_id TEXT NOT NULL, vendor_context_id INTEGER NOT NULL,
      parent_sku TEXT NOT NULL, product_type TEXT, variation_theme TEXT, status TEXT NOT NULL,
      snapshot_json TEXT NOT NULL, payload_hash TEXT NOT NULL, submitted_at_utc TEXT, last_checked_at_utc TEXT,
      next_check_at_utc TEXT, deadline_at_utc TEXT, created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS parent_child_job_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, asin TEXT, sku TEXT NOT NULL, operation_type TEXT NOT NULL,
      status TEXT NOT NULL, listings_status TEXT, catalog_relationship_status TEXT, issues_json TEXT, UNIQUE(job_id,sku));
    CREATE TABLE IF NOT EXISTS parent_child_job_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, event_key TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
      message TEXT, evidence_json TEXT, created_at_utc TEXT NOT NULL, UNIQUE(job_id,event_key));
    CREATE TABLE IF NOT EXISTS parent_child_poll_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, attempt_number INTEGER NOT NULL, checked_at_utc TEXT NOT NULL,
      next_check_at_utc TEXT, http_status INTEGER, request_id TEXT, result TEXT, issues_json TEXT, UNIQUE(job_id,attempt_number));
    CREATE INDEX IF NOT EXISTS idx_pc_tracker_scope ON parent_child_jobs(marketplace_id,vendor_context_id,status);
    """),
    ("005_authoritative_safety_gate", """
    CREATE TABLE IF NOT EXISTS pc_approval_snapshots (
      id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, version INTEGER NOT NULL,
      marketplace_id TEXT NOT NULL, vendor_context_id INTEGER NOT NULL, vendor_code TEXT NOT NULL,
      parent_sku TEXT NOT NULL, product_type TEXT NOT NULL, variation_theme TEXT NOT NULL,
      parent_ptd_json TEXT NOT NULL, child_ptd_json TEXT NOT NULL, payload_json TEXT NOT NULL,
      payload_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'FRESH',
      created_at_utc TEXT NOT NULL, expires_at_utc TEXT NOT NULL,
      UNIQUE(draft_id, version)
    );
    CREATE TABLE IF NOT EXISTS pc_snapshot_preflights (
      id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, status TEXT NOT NULL,
      evidence_json TEXT NOT NULL, checked_at_utc TEXT NOT NULL, expires_at_utc TEXT NOT NULL,
      FOREIGN KEY(snapshot_id) REFERENCES pc_approval_snapshots(id)
    );
    CREATE INDEX IF NOT EXISTS idx_pc_snapshot_preflights ON pc_snapshot_preflights(snapshot_id, checked_at_utc DESC);
    CREATE TABLE IF NOT EXISTS pc_submission_reservations (
      id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
      marketplace_id TEXT NOT NULL, vendor_context_id INTEGER NOT NULL,
      reservation_type TEXT NOT NULL, parent_sku TEXT, child_asin TEXT, child_sku TEXT,
      active INTEGER NOT NULL DEFAULT 1, created_at_utc TEXT NOT NULL,
      FOREIGN KEY(snapshot_id) REFERENCES pc_approval_snapshots(id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pc_active_parent_reservation
      ON pc_submission_reservations(marketplace_id,vendor_context_id,parent_sku)
      WHERE active=1 AND reservation_type='PARENT';
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pc_active_child_asin_reservation
      ON pc_submission_reservations(marketplace_id,vendor_context_id,child_asin)
      WHERE active=1 AND reservation_type='CHILD' AND child_asin IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_pc_active_child_sku_reservation
      ON pc_submission_reservations(marketplace_id,vendor_context_id,child_sku)
      WHERE active=1 AND reservation_type='CHILD' AND child_sku IS NOT NULL;
    """),
    ("006_validation_preview_evidence", """
    CREATE TABLE IF NOT EXISTS pc_validation_preview_evidence (
      id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, operation TEXT NOT NULL, sku TEXT NOT NULL,
      classification TEXT NOT NULL, evidence_json TEXT NOT NULL,
      attempted_at_utc TEXT NOT NULL, completed_at_utc TEXT NOT NULL, expires_at_utc TEXT NOT NULL,
      FOREIGN KEY(snapshot_id) REFERENCES pc_approval_snapshots(id)
    );
    CREATE INDEX IF NOT EXISTS idx_pc_validation_snapshot ON pc_validation_preview_evidence(snapshot_id, completed_at_utc DESC);
    """),
    ("007_saved_draft_management", """
    ALTER TABLE pc_drafts ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE pc_drafts ADD COLUMN data_json TEXT;
    ALTER TABLE pc_draft_items ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX IF NOT EXISTS idx_pc_drafts_archived ON pc_drafts(archived, updated_at_utc DESC);
    """),
    ("008_parent_only_submission", """
    CREATE TABLE IF NOT EXISTS pc_parent_submission_details (
      job_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL UNIQUE, idempotency_key TEXT NOT NULL UNIQUE,
      operation_type TEXT NOT NULL, vendor_code TEXT NOT NULL, request_id TEXT, http_status INTEGER,
      submission_status TEXT, issues_json TEXT, final_preflight_id TEXT, created_at_utc TEXT NOT NULL,
      FOREIGN KEY(job_id) REFERENCES parent_child_jobs(id),
      FOREIGN KEY(snapshot_id) REFERENCES pc_approval_snapshots(id)
    );
    CREATE INDEX IF NOT EXISTS idx_pc_parent_submission_snapshot ON pc_parent_submission_details(snapshot_id);
    """),
    ("009_validation_preview_runtime_setting", """
    CREATE TABLE IF NOT EXISTS pc_runtime_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pc_runtime_setting_events (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, action TEXT NOT NULL, created_at_utc TEXT NOT NULL);
    """),
    ("010_validation_evidence_payload_hash", """
    ALTER TABLE pc_validation_preview_evidence ADD COLUMN payload_hash TEXT;
    CREATE INDEX IF NOT EXISTS idx_pc_validation_hash ON pc_validation_preview_evidence(snapshot_id,payload_hash,completed_at_utc DESC);
    """),
    ("011_parent_creation_arms", """
    CREATE TABLE IF NOT EXISTS pc_parent_creation_arms (
      id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
      marketplace_id TEXT NOT NULL, vendor_context_id INTEGER NOT NULL, vendor_code TEXT NOT NULL,
      parent_sku TEXT NOT NULL, children_json TEXT NOT NULL, validation_expires_at_utc TEXT NOT NULL,
      snapshot_expires_at_utc TEXT NOT NULL, preflight_expires_at_utc TEXT NOT NULL,
      armed_at_utc TEXT NOT NULL, expires_at_utc TEXT NOT NULL, consumed_at_utc TEXT,
      FOREIGN KEY(snapshot_id) REFERENCES pc_approval_snapshots(id));
    CREATE TABLE IF NOT EXISTS pc_parent_creation_arm_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, arm_id TEXT, action TEXT NOT NULL, created_at_utc TEXT NOT NULL);
    """),
    ("012_child_attachment_arms", """
    CREATE TABLE IF NOT EXISTS pc_child_attachment_arms (
      id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
      parent_job_id TEXT NOT NULL, parent_sku TEXT NOT NULL, parent_asin TEXT NOT NULL,
      marketplace_id TEXT NOT NULL, vendor_context_id INTEGER NOT NULL, vendor_code TEXT NOT NULL,
      children_json TEXT NOT NULL, validation_expires_at_utc TEXT NOT NULL,
      snapshot_expires_at_utc TEXT NOT NULL, armed_at_utc TEXT NOT NULL,
      expires_at_utc TEXT NOT NULL, consumed_at_utc TEXT);
    CREATE TABLE IF NOT EXISTS pc_child_attachment_details (
      job_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, child_sku TEXT NOT NULL,
      child_asin TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, patch_json TEXT NOT NULL,
      request_id TEXT, http_status INTEGER, submission_status TEXT, issues_json TEXT,
      PRIMARY KEY(job_id,child_sku));
    """),
    ("013_guided_mode_orchestration", """
    CREATE TABLE IF NOT EXISTS pc_guided_drafts (
      id TEXT PRIMARY KEY, family_json TEXT NOT NULL, family_hash TEXT NOT NULL,
      approval_hash TEXT, approval_at_utc TEXT, approval_by TEXT,
      created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pc_guided_jobs (
      id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, family_hash TEXT NOT NULL UNIQUE,
      family_json TEXT NOT NULL, state TEXT NOT NULL, blocking_issue_json TEXT,
      approved_at_utc TEXT NOT NULL, approved_by TEXT, created_at_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL, FOREIGN KEY(draft_id) REFERENCES pc_guided_drafts(id));
    CREATE TABLE IF NOT EXISTS pc_guided_operations (
      id TEXT PRIMARY KEY, job_id TEXT NOT NULL, operation_key TEXT NOT NULL,
      operation_type TEXT NOT NULL, child_asin TEXT, vendor_sku TEXT NOT NULL,
      sequence_number INTEGER NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
      payload_hash TEXT NOT NULL, state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
      request_id TEXT, http_status INTEGER, issues_json TEXT, prepared_at_utc TEXT NOT NULL,
      submitting_at_utc TEXT, completed_at_utc TEXT, verified_at_utc TEXT,
      updated_at_utc TEXT NOT NULL, UNIQUE(job_id, operation_key),
      FOREIGN KEY(job_id) REFERENCES pc_guided_jobs(id));
    CREATE TABLE IF NOT EXISTS pc_guided_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, event_key TEXT NOT NULL,
      from_state TEXT, to_state TEXT NOT NULL, evidence_json TEXT, created_at_utc TEXT NOT NULL,
      UNIQUE(job_id,event_key), FOREIGN KEY(job_id) REFERENCES pc_guided_jobs(id));
    CREATE INDEX IF NOT EXISTS idx_pc_guided_jobs_state ON pc_guided_jobs(state,updated_at_utc DESC);
    CREATE INDEX IF NOT EXISTS idx_pc_guided_operations_job ON pc_guided_operations(job_id,sequence_number);
    """),
    ("014_guided_mode_worker_runtime", """
    CREATE TABLE IF NOT EXISTS pc_guided_job_runtime (
      job_id TEXT PRIMARY KEY, snapshot_json TEXT, parent_ptd_json TEXT, child_ptd_json TEXT,
      next_attempt_at_utc TEXT, poll_attempt_count INTEGER NOT NULL DEFAULT 0,
      last_action TEXT, updated_at_utc TEXT NOT NULL,
      FOREIGN KEY(job_id) REFERENCES pc_guided_jobs(id));
    CREATE TABLE IF NOT EXISTS pc_guided_validation_evidence (
      id TEXT PRIMARY KEY, job_id TEXT NOT NULL, operation_key TEXT NOT NULL,
      payload_hash TEXT NOT NULL, classification TEXT NOT NULL, evidence_json TEXT NOT NULL,
      completed_at_utc TEXT NOT NULL, expires_at_utc TEXT NOT NULL,
      UNIQUE(job_id,operation_key,payload_hash), FOREIGN KEY(job_id) REFERENCES pc_guided_jobs(id));
    CREATE INDEX IF NOT EXISTS idx_pc_guided_runtime_due ON pc_guided_job_runtime(next_attempt_at_utc);
    """),
    ("015_guided_worker_leases", """
    CREATE TABLE IF NOT EXISTS pc_guided_worker_leases (
      job_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, expires_at_utc TEXT NOT NULL,
      updated_at_utc TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES pc_guided_jobs(id));
    """),
    ("016_guided_child_write_authorizations", """
    CREATE TABLE IF NOT EXISTS pc_guided_child_write_authorizations (
      id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, family_hash TEXT NOT NULL,
      plan_hash TEXT NOT NULL, child_evidence_json TEXT NOT NULL, expires_at_utc TEXT NOT NULL,
      confirmed_at_utc TEXT NOT NULL, confirmed_by TEXT, consumed_operations_json TEXT NOT NULL DEFAULT '[]',
      FOREIGN KEY(job_id) REFERENCES pc_guided_jobs(id));
    CREATE TABLE IF NOT EXISTS pc_guided_child_write_authorization_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, authorization_id TEXT NOT NULL, operation_key TEXT,
      action TEXT NOT NULL, evidence_json TEXT, created_at_utc TEXT NOT NULL);
    """),
]

def ensure_parent_child_schema():
    with get_db_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS pc_schema_migrations (version TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for version, sql in MIGRATIONS:
            if not conn.execute("SELECT 1 FROM pc_schema_migrations WHERE version=?", (version,)).fetchone():
                conn.executescript(sql)
                conn.execute("INSERT INTO pc_schema_migrations(version, applied_at_utc) VALUES(?,?)", (version, now))
        conn.commit()
