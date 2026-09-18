"""Offline safety smoke tests for the uploaded Guided child boundary.

These do not replace the application's full isolated pytest suite.
"""
import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_worker():
    services = types.ModuleType("services")
    services.__path__ = []
    sys.modules["services"] = services
    exports = {
        "db": {"get_db_connection": lambda: None},
        "parent_child_guided_orchestrator": {key: lambda *args, **kwargs: None for key in
            ("get_draft", "get_job", "next_action", "save_draft", "status", "transition", "update_operation", "utcnow")},
        "parent_child_migrations": {"ensure_parent_child_schema": lambda: None},
        "parent_child_preview": {"active_variation_keys": lambda *args: {"size"}, "build_preview": lambda *args: None},
        "parent_child_resolver": {"resolve_many": lambda *args: None},
        "parent_child_safety": {"checksum": lambda x: hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest(),
                                "full_schema_errors": lambda *args, **kwargs: [], "patch_errors": lambda *args: ([], []), "redact": lambda x: x},
        "spapi_catalog_relationships": {"catalog_lookup": lambda *args: None, "current_parent": lambda x: None},
        "spapi_listings": {"listing_item_attributes": lambda *args: None},
        "spapi_product_types": {"get_definition": lambda *args: None},
        "spapi_validation_preview": {"validation_preview_patch_listing": lambda *args: None,
                                     "validation_preview_put_listing": lambda *args: None},
    }
    for name, attributes in exports.items():
        module = types.ModuleType("services." + name)
        module.__dict__.update(attributes)
        sys.modules[module.__name__] = module
    spec = importlib.util.spec_from_file_location("offline_guided_worker", ROOT / "services/parent_child_guided_worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ChildAuthorizationUnit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "guided-test.sqlite"
        self.worker = load_worker()
        self.worker.get_db_connection = self.connect
        self.worker.ensure_parent_child_schema = lambda: None
        self.worker.utcnow = lambda: self.worker._now().isoformat()
        self.body = lambda size: {"productType": "DISHWARE_PLATE", "patches": [
            {"op": "add", "path": "/attributes/parentage_level", "value": [{"value": "child"}]},
            {"op": "add", "path": "/attributes/variation_theme", "value": [{"name": "SIZE"}]},
            {"op": "add", "path": "/attributes/child_parent_sku_relationship", "value": [{"parent_sku": "Prince_Plate"}]},
            {"op": "add", "path": "/attributes/size", "value": [{"value": size, "language_tag": "en_AE"}]},
        ]}
        self.job = {"id": "test-job", "family_hash": "a" * 64, "state": "NEEDS_ATTENTION",
                    "family": {"parent_sku": "Prince_Plate", "product_type": "DISHWARE_PLATE", "variation_theme": "SIZE",
                               "marketplace_id": "A2VIGQ35RCS4UG", "vendor_code": "FK6EI",
                               "children": [{"asin": "B0CC2HZNFH"}, {"asin": "B0CC2JL6F6"}]},
                    "operations": [{"operation_key": "parent", "operation_type": "PUT", "state": "VERIFIED", "attempt_count": 1},
                        {"operation_key": "child:B0CC2HZNFH", "operation_type": "PATCH", "state": "PREPARED", "attempt_count": 0,
                         "child_asin": "B0CC2HZNFH", "vendor_sku": "DI-106", "payload_hash": "b" * 64},
                        {"operation_key": "child:B0CC2JL6F6", "operation_type": "PATCH", "state": "PREPARED", "attempt_count": 0,
                         "child_asin": "B0CC2JL6F6", "vendor_sku": "DI-105", "payload_hash": "c" * 64}]}
        self.worker.get_job = lambda _: self.job
        self.worker._runtime = lambda _: {"child_ptd": {}, "snapshot": {"payload": {"children": [
            {"asin": "B0CC2HZNFH", "body": self.body("7 Inch")},
            {"asin": "B0CC2JL6F6", "body": self.body("9 Inch")}]}}}
        self.worker.transition = lambda *args, **kwargs: {"state": "VALIDATING_CHILDREN"}
        self.worker.reconcile_prepared_children_get = lambda *args: {"children": [
            {"vendor_sku": "DI-106", "attached": False, "observed_parent": None, "listing_status": 200,
             "listing_request_id": "get-106", "catalog_status": 200, "catalog_request_id": "catalog-106"},
            {"vendor_sku": "DI-105", "attached": False, "observed_parent": None, "listing_status": 200,
             "listing_request_id": "get-105", "catalog_status": 200, "catalog_request_id": "catalog-105"}]}
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE pc_guided_child_write_authorizations(id TEXT PRIMARY KEY,job_id TEXT UNIQUE,family_hash TEXT,
                    plan_hash TEXT,child_evidence_json TEXT,expires_at_utc TEXT,confirmed_at_utc TEXT,confirmed_by TEXT,
                    consumed_operations_json TEXT);
                CREATE TABLE pc_guided_child_write_authorization_events(id INTEGER PRIMARY KEY,authorization_id TEXT,
                    operation_key TEXT,action TEXT,evidence_json TEXT,created_at_utc TEXT);
                CREATE TABLE pc_guided_jobs(id TEXT PRIMARY KEY,state TEXT,family_hash TEXT);
                CREATE TABLE pc_guided_worker_leases(job_id TEXT PRIMARY KEY);
                CREATE TABLE pc_guided_operations(job_id TEXT,operation_key TEXT,operation_type TEXT,state TEXT,attempt_count INTEGER,payload_hash TEXT);
            """)
            conn.execute("INSERT INTO pc_guided_jobs VALUES(?,?,?)", (self.job["id"], self.job["state"], self.job["family_hash"]))
            conn.executemany("INSERT INTO pc_guided_operations VALUES(?,?,?,?,?,?)", [(self.job["id"], op["operation_key"],
                op["operation_type"], op["state"], op["attempt_count"], op.get("payload_hash")) for op in self.job["operations"]])

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def prepare(self):
        return self.worker.prepare_child_write_authorization(self.job["id"], self.job["family_hash"], object())

    def test_prepare_confirm_consume_each_once_and_audit(self):
        proof = self.prepare()
        self.assertEqual(len(proof["plan"]), 2)
        self.assertEqual(len(proof["children"]), 2)
        with self.assertRaisesRegex(ValueError, "Exact AUTHORIZE"):
            self.worker.confirm_child_write_authorization(self.job["id"], self.job["family_hash"], proof["plan_hash"], "YES")
        self.worker.confirm_child_write_authorization(self.job["id"], self.job["family_hash"], proof["plan_hash"], "AUTHORIZE CHILDREN")
        with self.assertRaisesRegex(ValueError, "cannot be replaced before expiry"):
            self.prepare()
        first = self.job["operations"][1]
        self.worker._child_write_authorized(self.job, first, self.body("7 Inch"))
        with self.assertRaisesRegex(ValueError, "already consumed"):
            self.worker._child_write_authorized(self.job, first, self.body("7 Inch"))
        self.job["operations"][1]["state"] = "VERIFIED"
        second = self.job["operations"][2]
        self.worker._child_write_authorized(self.job, second, self.body("9 Inch"))
        self.worker._child_write_audit(self.job, second, "UNKNOWN", None)
        with self.connect() as conn:
            row = conn.execute("SELECT consumed_operations_json FROM pc_guided_child_write_authorizations").fetchone()
            actions = [x[0] for x in conn.execute("SELECT action FROM pc_guided_child_write_authorization_events")]
        self.assertEqual(len(json.loads(row[0])), 2)
        self.assertIn("PATCH_UNKNOWN", actions)

    def test_missing_expired_hash_and_body_mismatch_fail_closed(self):
        first = self.job["operations"][1]
        with self.assertRaisesRegex(ValueError, "Explicit unexpired"):
            self.worker._child_write_authorized(self.job, first, self.body("7 Inch"))
        proof = self.prepare()
        with self.assertRaisesRegex(ValueError, "not eligible"):
            self.worker.confirm_child_write_authorization(self.job["id"], "wrong", proof["plan_hash"], "AUTHORIZE CHILDREN")
        self.worker.confirm_child_write_authorization(self.job["id"], self.job["family_hash"], proof["plan_hash"], "AUTHORIZE CHILDREN")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.worker._child_write_authorized(self.job, first, self.body("8 Inch"))
        with self.connect() as conn:
            conn.execute("UPDATE pc_guided_child_write_authorizations SET expires_at_utc='2000-01-01T00:00:00+00:00'")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "Explicit unexpired"):
            self.worker._child_write_authorized(self.job, first, self.body("7 Inch"))

    def test_wrong_identity_or_existing_parent_blocks_child_get(self):
        child = self.job["operations"][1]
        listing = {"status": 200, "api_sku": "DI-106", "api_asins": ["B0CC2HZNFH"],
                   "product_type": "DISHWARE_PLATE", "attributes": {}, "issues": [], "request_id": "list-106"}
        catalog = {"asin": "B0CC2HZNFH", "relationships": []}
        api = {"listing": lambda *args: listing, "catalog": lambda *args: (catalog, {"http_status": 200, "request_id": "cat-106"})}
        self.worker._verified_parent_asins = lambda _: {"B0HJW82XKN"}
        attached, parent, proof = self.worker._verify_child(self.job, child, api, object())
        self.assertFalse(attached)
        self.assertIsNone(parent)
        self.assertEqual(proof["listing"]["api_sku"], "DI-106")
        listing["api_asins"] = ["B0CC2JL6F6"]
        with self.assertRaisesRegex(ValueError, "identity"):
            self.worker._verify_child(self.job, child, api, object())
        listing["api_asins"] = ["B0CC2HZNFH"]
        catalog["relationships"] = [{"relationships": [{"parentAsins": ["B0OTHER01"]}]}]
        attached, parent, _ = self.worker._verify_child(self.job, child, api, object())
        self.assertFalse(attached)
        self.assertEqual(parent, "B0OTHER01")
        catalog["relationships"][0]["relationships"][0]["parentAsins"].append("B0HJW82XKN")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.worker._verify_child(self.job, child, api, object())
        with self.assertRaisesRegex(ValueError, "Configured auth"):
            self.worker._verify_child(self.job, child, api, None)

    def test_scheduler_cannot_patch_without_confirmation_and_unknown_ack_is_one_shot(self):
        self.job["state"] = "ATTACHING_CHILDREN"
        self.worker.next_action = lambda _: "ATTACH_CHILD"
        self.worker._verify_child = lambda *args: (False, None, {})
        self.worker.status = lambda _: {"state": self.job["state"]}
        outcomes = []
        self.worker.update_operation = lambda *args, **kwargs: outcomes.append(args[2])
        self.worker.transition = lambda _, state, *args: self.job.update(state=state)
        api = {"child_patch": lambda *args: (_ for _ in ()).throw(TimeoutError("uncertain transport"))}
        before = self.worker.process_next_safe_action(self.job["id"], object(), api)
        self.assertEqual(self.job["state"], "NEEDS_ATTENTION")
        self.assertEqual(outcomes, [])
        self.job["state"] = "ATTACHING_CHILDREN"
        proof = self.prepare()
        self.job["state"] = "NEEDS_ATTENTION"
        self.worker.confirm_child_write_authorization(self.job["id"], self.job["family_hash"], proof["plan_hash"], "AUTHORIZE CHILDREN")
        self.job["state"] = "ATTACHING_CHILDREN"
        after = self.worker.process_next_safe_action(self.job["id"], object(), api)
        self.assertEqual(outcomes, ["SUBMITTING", "UNKNOWN"])
        self.assertEqual(self.job["state"], "SUBMISSION_UNKNOWN")
        with self.connect() as conn:
            actions = [row[0] for row in conn.execute("SELECT action FROM pc_guided_child_write_authorization_events")]
        self.assertIn("PATCH_UNKNOWN", actions)

    def test_migration_accepts_unconfirmed_preparation_record(self):
        other = Path(self.temp.name) / "migration.sqlite"
        module = types.ModuleType("services.db")
        def open_other():
            conn = sqlite3.connect(other)
            conn.row_factory = sqlite3.Row
            return conn
        module.get_db_connection = open_other
        sys.modules["services.db"] = module
        spec = importlib.util.spec_from_file_location("offline_guided_migrations", ROOT / "services/parent_child_migrations.py")
        migrations = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migrations)
        migrations.ensure_parent_child_schema()
        with open_other() as conn:
            conn.execute("INSERT INTO pc_guided_child_write_authorizations(id,job_id,family_hash,plan_hash,child_evidence_json,expires_at_utc,confirmed_at_utc,confirmed_by,consumed_operations_json) VALUES(?,?,?,?,?,?,?,?,?)",
                         ("migration-proof", "test-job", "a" * 64, "b" * 64, "{}", "2030-01-01T00:00:00+00:00", None, None, "[]"))
            self.assertIsNone(conn.execute("SELECT confirmed_at_utc FROM pc_guided_child_write_authorizations").fetchone()[0])

    def test_expired_partial_completion_renews_only_untouched_child(self):
        proof = self.prepare()
        self.worker.confirm_child_write_authorization(self.job["id"], self.job["family_hash"], proof["plan_hash"], "AUTHORIZE CHILDREN")
        first = self.job["operations"][1]
        self.worker._child_write_authorized(self.job, first, self.body("7 Inch"))
        first.update(state="VERIFIED", attempt_count=1)
        with self.connect() as conn:
            conn.execute("UPDATE pc_guided_operations SET state='VERIFIED',attempt_count=1 WHERE operation_key=?", (first["operation_key"],))
            conn.execute("UPDATE pc_guided_child_write_authorizations SET expires_at_utc='2000-01-01T00:00:00+00:00'")
            conn.commit()
        renewed = self.prepare()
        self.assertEqual([item["vendor_sku"] for item in renewed["plan"]], ["DI-105"])
        self.worker.confirm_child_write_authorization(self.job["id"], self.job["family_hash"], renewed["plan_hash"], "AUTHORIZE CHILDREN")
        second = self.job["operations"][2]
        self.worker._child_write_authorized(self.job, second, self.body("9 Inch"))
        with self.connect() as conn:
            consumed = json.loads(conn.execute("SELECT consumed_operations_json FROM pc_guided_child_write_authorizations").fetchone()[0])
        self.assertEqual(consumed, [first["operation_key"], second["operation_key"]])


if __name__ == "__main__":
    unittest.main()
