import threading
import json
from pathlib import Path

import pytest

from services import parent_child_guided_orchestrator as guided
from services.parent_child_store import upsert_vendor_context, vendor_contexts


def family(**changes):
    value = {
        "marketplace_id": "M", "vendor_context_id": 1, "vendor_code": "VENDOR",
        "parent_sku": "GUIDED-PARENT", "parent_title": "Guided parent", "product_type": "TYPE",
        "variation_theme": "SIZE", "parent_required_values": {"brand": {"value": "Acme", "provenance": "PTD"}},
        "children": [{"asin": "B000000001", "vendor_sku": "CHILD-1", "variation_value": "7 INCH"}, {"asin": "B000000002", "vendor_sku": "CHILD-2", "variation_value": "9 INCH"}],
    }
    value.update(changes); return value


def draft():
    upsert_vendor_context("M", "VENDOR", True)
    context = vendor_contexts("M")[0]
    return guided.save_draft(family(vendor_context_id=context["id"]))


def approved_job():
    value = draft()
    return guided.approve(value["id"], value["family_hash"], "tester")


def allow_selected_guided_job(job):
    from services import parent_child_guided_worker as worker
    return worker.set_execution_control("SELECTED_JOB", job_id=job["id"], family_hash=job["family_hash"], confirmation="RUN SELECTED JOB")


def test_guided_execution_control_defaults_paused_and_persists_selected_mode_across_restart():
    from services import parent_child_guided_worker as worker
    assert worker.execution_control()["mode"] == "PAUSED"
    job = approved_job(); changed = allow_selected_guided_job(job)
    assert changed["mode"] == "SELECTED_JOB" and changed["selected_job_id"] == job["id"]
    import importlib
    reloaded = importlib.reload(worker)
    persisted = reloaded.execution_control()
    assert persisted["mode"] == "SELECTED_JOB" and persisted["selected_family_hash"] == job["family_hash"]


def test_guided_execution_control_migration_defaults_existing_database_to_paused():
    from services.db import get_db_connection
    from services.parent_child_migrations import ensure_parent_child_schema
    with get_db_connection() as conn:
        conn.execute("DROP TABLE pc_guided_execution_control")
        conn.execute("DELETE FROM pc_schema_migrations WHERE version='017_guided_execution_control'")
        conn.commit()
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        row = conn.execute("SELECT mode,selected_job_id,selected_family_hash FROM pc_guided_execution_control WHERE singleton_id=1").fetchone()
    assert tuple(row) == ("PAUSED", None, None)


def test_guided_execution_control_rejects_invalid_hash_terminal_job_and_unconfirmed_automatic():
    from services import parent_child_guided_worker as worker
    job = approved_job()
    with pytest.raises(ValueError, match="confirmation"):
        worker.set_execution_control("AUTOMATIC", confirmation="RUN")
    with pytest.raises(ValueError, match="hash"):
        worker.set_execution_control("SELECTED_JOB", job_id=job["id"], family_hash="wrong", confirmation="RUN SELECTED JOB")
    worker.cancel(job["id"])
    with pytest.raises(ValueError, match="non-terminal"):
        worker.set_execution_control("SELECTED_JOB", job_id=job["id"], family_hash=job["family_hash"], confirmation="RUN SELECTED JOB")


def test_guided_execution_control_emergency_override_is_absolute(monkeypatch):
    from services import parent_child_guided_worker as worker
    job = approved_job()
    worker.set_execution_control("AUTOMATIC", confirmation="ENABLE AUTOMATIC PROCESSING")
    monkeypatch.setenv("GUIDED_SCHEDULER_DISABLED", "true")
    control = worker.execution_control()
    assert control["mode"] == "AUTOMATIC" and control["effective_mode"] == "PAUSED" and control["emergency_override"]
    assert worker.eligible_jobs() == []
    assert worker.claim_job(job["id"], "emergency-test") is False
    with pytest.raises(ValueError, match="paused"):
        worker.process_next_safe_action(job["id"], object(), {"get_definition": lambda *_: pytest.fail("worker must stop before transport")})


def test_guided_execution_control_selected_job_filters_eligibility_claims_and_unrelated_jobs():
    from services import parent_child_guided_worker as worker
    first = approved_job()
    value = draft(); second_draft = guided.save_draft(family(parent_sku="CONTROL-OTHER", vendor_context_id=value["family"]["vendor_context_id"], children=[{"asin": "B000000031", "vendor_sku": "CONTROL-1", "variation_value": "7 INCH"}, {"asin": "B000000032", "vendor_sku": "CONTROL-2", "variation_value": "9 INCH"}]), value["id"])
    second = guided.approve(second_draft["id"], second_draft["family_hash"])
    allow_selected_guided_job(first)
    assert worker.eligible_jobs() == [first["id"]]
    assert worker.claim_job(second["id"], "other") is False
    with pytest.raises(ValueError, match="not selected"):
        worker.process_next_safe_action(second["id"], object(), {"get_definition": lambda *_: pytest.fail("unrelated job must not run")})
    assert guided.get_job(second["id"])["state"] == "APPROVED"


def test_guided_execution_control_lease_race_and_pause_between_actions():
    from services import parent_child_guided_worker as worker
    job = approved_job(); allow_selected_guided_job(job); claims = []
    threads = [threading.Thread(target=lambda owner=owner: claims.append((owner, worker.claim_job(job["id"], owner)))) for owner in ("one", "two")]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert sum(result for _, result in claims) == 1
    owner = next(owner for owner, result in claims if result); worker.release_job(job["id"], owner)
    schema = {"properties": {}, "required": []}
    worker.process_next_safe_action(job["id"], object(), {"get_definition": lambda *_: (schema, {}), "build_preview": worker.build_preview})
    assert guided.get_job(job["id"])["state"] == "AMAZON_VALIDATING"
    worker.set_execution_control("PAUSED")
    with pytest.raises(ValueError, match="paused"):
        worker.process_next_safe_action(job["id"], object(), {"validate_parent": lambda *_: pytest.fail("pause must block the next action")})
    assert guided.get_job(job["id"])["state"] == "AMAZON_VALIDATING"


def test_guided_execution_control_ui_and_routes_are_server_owned():
    from routes import parent_child_routes as routes
    html = Path("ui/index.html").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_guided_mode.js").read_text(encoding="utf-8")
    assert routes.guided_execution_control()["mode"] == "PAUSED"
    job = approved_job()
    selected = routes.guided_run_selected_job(routes.GuidedSelectedExecutionRequest(job_id=job["id"], family_hash=job["family_hash"], confirmation="RUN SELECTED JOB"))
    assert selected["mode"] == "SELECTED_JOB" and selected["selected_job_id"] == job["id"]
    assert routes.guided_pause_execution()["mode"] == "PAUSED"
    assert "Guided execution control" in html and "Enable automatic processing" in html
    assert "/guided/execution-control/selected-job" in js and "RUN SELECTED JOB" in js
    assert "/guided/execution-control/automatic" in js and "ENABLE AUTOMATIC PROCESSING" in js
    assert "production_adapters" not in js and "auth_client" not in js


def test_valid_and_invalid_guided_transitions():
    job = approved_job()
    assert guided.transition(job["id"], "LOCAL_VALIDATING")["state"] == "LOCAL_VALIDATING"
    with pytest.raises(ValueError, match="Invalid guided transition"):
        guided.transition(job["id"], "COMPLETED")


def test_immutable_approval_and_edit_invalidation():
    value = draft()
    with pytest.raises(ValueError, match="does not match"):
        guided.approve(value["id"], "wrong")
    guided.approve(value["id"], value["family_hash"])
    changed = guided.save_draft(family(parent_title="Edited"), value["id"])
    assert not changed["approved"] and changed["approval_hash"] is None


def test_duplicate_and_concurrent_confirmations_create_one_job():
    value = draft(); results = []
    def confirm(): results.append(guided.approve(value["id"], value["family_hash"])["id"])
    threads = [threading.Thread(target=confirm) for _ in range(2)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert len(set(results)) == 1


def test_operations_are_deterministic_and_parent_gates_children():
    first = approved_job(); second = guided.approve(guided.get_draft(first["draft_id"])["id"], first["family_hash"])
    assert first["operations"][0]["idempotency_key"] == second["operations"][0]["idempotency_key"]
    with pytest.raises(ValueError, match="Children require"):
        guided.update_operation(first["id"], "child:B000000001", "SUBMITTING")
    guided.update_operation(first["id"], "parent", "SUBMITTING")
    guided.update_operation(first["id"], "parent", "ACCEPTED")
    guided.update_operation(first["id"], "parent", "VERIFIED")
    guided.update_operation(first["id"], "child:B000000001", "SUBMITTING")
    with pytest.raises(ValueError, match="Children require"):
        guided.update_operation(first["id"], "child:B000000002", "SUBMITTING")


def test_submitting_unknown_never_retries_and_resume_planner_is_pure():
    job = approved_job()
    assert guided.next_action(job["id"]) == "LOCAL_VALIDATE"
    guided.transition(job["id"], "LOCAL_VALIDATING"); assert guided.next_action(job["id"]) == "AMAZON_VALIDATE"
    guided.transition(job["id"], "AMAZON_VALIDATING"); assert guided.next_action(job["id"]) == "AMAZON_VALIDATE"
    guided.transition(job["id"], "CREATING_PARENT"); assert guided.next_action(job["id"]) == "VALIDATE_PARENT"
    guided.update_operation(job["id"], "parent", "SUBMITTING")
    assert guided.next_action(job["id"]) == "RECONCILE_PARENT_GET"
    with pytest.raises(ValueError, match="one write attempt"):
        guided.update_operation(job["id"], "parent", "SUBMITTING")
    guided.update_operation(job["id"], "parent", "UNKNOWN")
    assert guided.next_action(job["id"]) == "RECONCILE_PARENT_GET"
    with pytest.raises(ValueError, match="never retry"):
        guided.update_operation(job["id"], "parent", "SUBMITTING")


def test_partial_completion_and_sanitized_storage():
    job = approved_job()
    guided.update_operation(job["id"], "parent", "VERIFIED")
    guided.update_operation(job["id"], "child:B000000001", "SUBMITTING", request_id="request", issues=[{"token": "never-store", "message": "safe"}])
    guided.update_operation(job["id"], "child:B000000001", "VERIFIED")
    guided.update_operation(job["id"], "child:B000000002", "REJECTED")
    assert guided.next_action(job["id"]) == "PARTIAL_COMPLETION"
    stored = guided.get_job(job["id"])
    assert "never-store" not in stored["operations"][1]["issues_json"]


def test_phase_one_endpoints_are_local_only_and_production_family_is_absent(monkeypatch):
    from routes import parent_child_routes as routes
    calls = []
    monkeypatch.setattr(routes, "_checked_context", lambda *a, **k: calls.append("amazon") )
    value = draft()
    saved = routes.guided_save_draft(routes.GuidedDraftRequest(definition=family(vendor_context_id=value["family"]["vendor_context_id"])))
    review = routes.guided_review(saved["draft"]["id"])["review"]
    job = routes.guided_approve(saved["draft"]["id"], routes.GuidedApprovalRequest(family_hash=review["family_hash"]))["job"]
    assert routes.guided_next_action(job["id"])["next_action"] == "LOCAL_VALIDATE"
    assert not calls
    assert all("Princess_Plate" not in str(x) and "DI-113" not in str(x) for x in (saved, review, job))


def test_phase_two_mocked_worker_completes_sequential_family_without_real_transport():
    from services import parent_child_guided_worker as worker
    job = approved_job(); allow_selected_guided_job(job); attached = set(); calls = []
    schema = {"properties": {"parentage_level": {}, "variation_theme": {}, "child_parent_sku_relationship": {}, "size": {}}, "required": []}
    def definition(*_args): return schema, {"marketplaceIds": ["M"]}
    def listing(_auth, _market, _vendor, sku):
        if sku == "GUIDED-PARENT":
            if "parent" not in attached: return {"status": 404}
            return {"status": 200, "api_sku": "GUIDED-PARENT", "product_type": "TYPE", "parent_verified": True, "theme_verified": True, "issues": []}
        asin = "B000000001" if sku == "CHILD-1" else "B000000002"
        return {"status": 200, "api_sku": sku, "api_asins": [asin], "product_type": "TYPE", "current_parent": "GUIDED-PARENT" if sku in attached else None, "attached_to_parent": sku in attached, "issues": []}
    def parent_put(_auth, _family, _body, _key): attached.add("parent"); calls.append("parent"); return {"http_status": 202, "request_id": "safe"}
    def child_patch(_auth, _family, sku, _body, _key): attached.add(sku); calls.append(sku); return {"http_status": 202, "request_id": "safe"}
    api = {"get_definition": definition, "listing": listing, "catalog": lambda _a,_m,asin: ({"asin": asin, "relationships": []}, {"http_status": 200, "request_id": "catalog-safe"}), "parent_put": parent_put, "child_patch": child_patch,
           "validate_parent": lambda *_: {"classification": "PASSED", "http_status": 200, "issues": []},
           "validate_child": lambda *_: {"classification": "PASSED", "http_status": 200, "issues": []}}
    actions = [worker.process_next_safe_action(job["id"], object(), api)["action"] for _ in range(12)]
    final = guided.get_job(job["id"])
    assert final["state"] == "NEEDS_ATTENTION", (actions, final["blocking_issue"], final["operations"])
    assert "authorization" in final["blocking_issue"]["message"].lower()
    assert calls == ["parent"]


def test_phase_two_unknown_is_get_only_and_cancel_is_prewrite_only():
    from services import parent_child_guided_worker as worker
    second = draft()
    changed = guided.save_draft(family(parent_sku="GUIDED-PARENT-2", vendor_context_id=second["family"]["vendor_context_id"]), second["id"])
    job = guided.approve(changed["id"], changed["family_hash"])
    assert worker.cancel(job["id"])["state"] == "CANCELLED_BEFORE_WRITES"
    job = approved_job()
    guided.update_operation(job["id"], "parent", "SUBMITTING")
    with pytest.raises(ValueError, match="cannot be cancelled"):
        worker.cancel(job["id"])
    guided.update_operation(job["id"], "parent", "UNKNOWN")
    assert guided.next_action(job["id"]) == "RECONCILE_PARENT_GET"


def _persisted_accepted_parent_job(parent_sku="GUIDED-PARENT"):
    if parent_sku == "GUIDED-PARENT":
        job = approved_job()
    else:
        value = draft()
        saved = guided.save_draft(family(parent_sku=parent_sku, vendor_context_id=value["family"]["vendor_context_id"], children=[{"asin": "B000000011", "vendor_sku": "CHILD-11", "variation_value": "7 INCH"}, {"asin": "B000000012", "vendor_sku": "CHILD-12", "variation_value": "9 INCH"}]), value["id"])
        job = guided.approve(saved["id"], saved["family_hash"])
    guided.transition(job["id"], "LOCAL_VALIDATING")
    guided.transition(job["id"], "AMAZON_VALIDATING")
    guided.transition(job["id"], "CREATING_PARENT")
    guided.update_operation(job["id"], "parent", "SUBMITTING")
    guided.update_operation(job["id"], "parent", "ACCEPTED", request_id="parent-put", http_status=200)
    result = guided.transition(job["id"], "WAITING_FOR_PARENT")
    allow_selected_guided_job(result)
    return result


def test_persisted_accepted_parent_get_uses_configured_auth_and_never_writes():
    from services import parent_child_guided_worker as worker
    job = _persisted_accepted_parent_job(); configured_auth = object(); auths = []
    def listing(auth, _marketplace, _vendor, sku):
        auths.append(auth)
        return {"status": 200, "request_id": "parent-get", "requested_sku": sku, "api_sku": "GUIDED-PARENT", "product_type": "TYPE", "parent_verified": True, "theme_verified": True, "issues": []}
    result = worker.process_next_safe_action(job["id"], configured_auth, {"listing": listing, "parent_put": lambda *_: pytest.fail("PUT must not run"), "child_patch": lambda *_: pytest.fail("PATCH must not run")})
    stored = guided.get_job(job["id"])
    assert result["action"] == "VERIFY_PARENT_GET"
    assert auths == [configured_auth]
    assert stored["state"] == "VALIDATING_CHILDREN"
    parent = stored["operations"][0]
    assert parent["state"] == "VERIFIED" and parent["attempt_count"] == 1
    assert parent["request_id"] == "parent-put" and parent["http_status"] == 200


def test_parent_reconciliation_missing_auth_and_ambiguous_listing_fail_closed():
    from services import parent_child_guided_worker as worker
    job = _persisted_accepted_parent_job(); calls = []
    def listing(*args):
        calls.append(args)
        return {"status": 200, "api_sku": "GUIDED-PARENT", "product_type": "TYPE", "parent_verified": True, "theme_verified": True, "issues": []}
    worker.process_next_safe_action(job["id"], None, {"listing": listing})
    assert not calls
    blocked = guided.get_job(job["id"])
    assert blocked["state"] == "NEEDS_ATTENTION"
    assert "Configured auth client" in blocked["blocking_issue"]["message"]

    ambiguous = _persisted_accepted_parent_job("GUIDED-PARENT-AMBIGUOUS")
    response = worker.reconcile_accepted_parent_get
    guided.transition(ambiguous["id"], "NEEDS_ATTENTION", {"message": "'NoneType' object has no attribute 'get_lwa_access_token'"})
    with pytest.raises(ValueError, match="ambiguous"):
        response(ambiguous["id"], ambiguous["family_hash"], object(), {"listing": lambda *_: {"status": 200, "api_sku": "GUIDED-PARENT", "product_type": "TYPE", "parent_verified": False, "theme_verified": False, "issues": [], "catalog": {"relationships": [{"parent_sku": "GUIDED-PARENT"}]}}})
    after = guided.get_job(ambiguous["id"])
    assert after["state"] == "NEEDS_ATTENTION"
    assert after["operations"][0]["state"] == "ACCEPTED" and after["operations"][0]["attempt_count"] == 1


def test_guarded_reconcile_of_persisted_accepted_parent_is_get_only():
    from services import parent_child_guided_worker as worker
    job = _persisted_accepted_parent_job()
    guided.transition(job["id"], "NEEDS_ATTENTION", {"message": "'NoneType' object has no attribute 'get_lwa_access_token'"})
    calls = []
    def listing(auth, _marketplace, _vendor, sku):
        calls.append((auth, sku))
        return {"status": 200, "request_id": "verified-by-get", "requested_sku": sku, "api_sku": sku, "product_type": "TYPE", "parent_verified": True, "theme_verified": True, "issues": []}
    result = worker.reconcile_accepted_parent_get(job["id"], job["family_hash"], object(), {"listing": listing, "parent_put": lambda *_: pytest.fail("PUT must not run"), "child_patch": lambda *_: pytest.fail("PATCH must not run")})
    assert result["state"] == "VALIDATING_CHILDREN"
    assert len(calls) == 1
    stored = guided.get_job(job["id"])
    assert stored["operations"][0]["state"] == "VERIFIED"
    assert all(item["state"] == "PREPARED" and item["attempt_count"] == 0 for item in stored["operations"][1:])


def _persisted_verified_parent_children_prepared_job():
    job = _persisted_accepted_parent_job()
    guided.update_operation(job["id"], "parent", "VERIFIED")
    guided.transition(job["id"], "VALIDATING_CHILDREN", {"parent_get": {"status": 200, "api_sku": "GUIDED-PARENT", "api_asins": ["B0PARENT01"], "product_type": "TYPE"}})
    guided.transition(job["id"], "ATTACHING_CHILDREN")
    return guided.transition(job["id"], "NEEDS_ATTENTION", {"message": "'NoneType' object has no attribute 'get_lwa_access_token'"})


def _persisted_child_authorization_job():
    """A real migrated job/runtime at the child-write boundary; no live transport."""
    from services import parent_child_guided_worker as worker
    job = approved_job()
    schema = {"properties": {"parentage_level": {}, "variation_theme": {}, "child_parent_sku_relationship": {}, "size": {}}, "required": []}
    worker._local_validate(job, {"get_definition": lambda *_: (schema, {"source": "isolated"}), "build_preview": worker.build_preview}, object())
    guided.transition(job["id"], "LOCAL_VALIDATING")
    guided.transition(job["id"], "AMAZON_VALIDATING")
    guided.transition(job["id"], "CREATING_PARENT")
    guided.update_operation(job["id"], "parent", "SUBMITTING")
    guided.update_operation(job["id"], "parent", "ACCEPTED", request_id="parent-put", http_status=200)
    guided.update_operation(job["id"], "parent", "VERIFIED")
    guided.transition(job["id"], "WAITING_FOR_PARENT")
    guided.transition(job["id"], "VALIDATING_CHILDREN", {"parent_get": {"status": 200, "request_id": "parent-get", "api_sku": "GUIDED-PARENT", "api_asins": ["B0PARENT01"], "product_type": "TYPE"}})
    result = guided.transition(job["id"], "ATTACHING_CHILDREN")
    allow_selected_guided_job(result)
    return result


def _persisted_accepted_di106_ambiguity_job():
    """Real persisted state matching the narrow production DI-106 reconciliation guard."""
    context = draft()
    saved = guided.save_draft(family(parent_sku="Prince_Plate", parent_title="Prince Party Plate Pack of 10", product_type="DISHWARE_PLATE", vendor_context_id=context["family"]["vendor_context_id"], children=[{"asin": "B0CC2HZNFH", "vendor_sku": "DI-106", "variation_value": "7 Inch"}, {"asin": "B0CC2JL6F6", "vendor_sku": "DI-105", "variation_value": "9 Inch"}]), context["id"])
    job = guided.approve(saved["id"], saved["family_hash"])
    guided.transition(job["id"], "LOCAL_VALIDATING"); guided.transition(job["id"], "AMAZON_VALIDATING"); guided.transition(job["id"], "CREATING_PARENT")
    guided.update_operation(job["id"], "parent", "SUBMITTING")
    guided.update_operation(job["id"], "parent", "ACCEPTED", request_id="parent-put", http_status=200)
    guided.update_operation(job["id"], "parent", "VERIFIED")
    guided.transition(job["id"], "WAITING_FOR_PARENT")
    guided.transition(job["id"], "VALIDATING_CHILDREN", {"parent_get": {"status": 200, "request_id": "parent-get", "api_sku": "Prince_Plate", "api_asins": ["B0HJW82XKN"], "product_type": "DISHWARE_PLATE"}})
    guided.transition(job["id"], "ATTACHING_CHILDREN")
    guided.update_operation(job["id"], "child:B0CC2HZNFH", "SUBMITTING")
    guided.update_operation(job["id"], "child:B0CC2HZNFH", "ACCEPTED", request_id="accepted-di106", http_status=200)
    return guided.transition(job["id"], "NEEDS_ATTENTION", {"message": "Child Listings/Catalog parent identity is ambiguous"})


def test_guarded_accepted_di106_reconciliation_is_get_only_and_preserves_patch_metadata():
    from services import parent_child_guided_worker as worker
    from services.db import get_db_connection
    job = _persisted_accepted_di106_ambiguity_job(); calls = []; auth = object()
    def listing(actual_auth, _market, _vendor, sku):
        calls.append(("listing", actual_auth, sku))
        return {"status": 200, "request_id": "listing-di106", "api_sku": "DI-106", "api_asins": ["B0CC2HZNFH"], "product_type": "DISHWARE_PLATE", "attributes": {"child_parent_sku_relationship": [{"parent_sku": "Prince_Plate"}]}, "issues": []}
    def catalog(actual_auth, _market, asin):
        calls.append(("catalog", actual_auth, asin))
        return {"asin": "B0CC2HZNFH", "relationships": [{"relationships": [{"parentAsins": ["B0HJW82XKN"]}]}]}, {"http_status": 200, "request_id": "catalog-di106"}
    result = worker.reconcile_accepted_di106_get(job["id"], job["family_hash"], auth, {"listing": listing, "catalog": catalog, "parent_put": lambda *_: pytest.fail("PUT must never run"), "child_patch": lambda *_: pytest.fail("PATCH must never run")})
    assert [item[0] for item in calls] == ["listing", "catalog"] and all(item[1] is auth for item in calls)
    assert result["state"] == "VERIFIED"
    assert result["next_action"] == "VALIDATE_CHILDREN"
    stored = guided.get_job(job["id"]); di106, di105 = stored["operations"][1:]
    assert (di106["state"], di106["attempt_count"], di106["request_id"], di106["http_status"]) == ("VERIFIED", 1, "accepted-di106", 200)
    assert (di105["state"], di105["attempt_count"], di105["request_id"], di105["http_status"]) == ("PREPARED", 0, None, None)
    with get_db_connection() as conn:
        evidence = conn.execute("SELECT evidence_json FROM pc_guided_events WHERE job_id=? ORDER BY id DESC LIMIT 1", (job["id"],)).fetchone()[0]
    assert "accepted_child_get" in evidence and "listing-di106" in evidence and "PATCH" not in evidence


@pytest.mark.parametrize("failure", ["catalog_parent", "listing_identity", "lease", "hash"])
def test_guarded_accepted_di106_reconciliation_fails_closed_without_any_write(failure):
    from services import parent_child_guided_worker as worker
    from services.db import get_db_connection
    job = _persisted_accepted_di106_ambiguity_job(); calls = []
    if failure == "lease":
        with get_db_connection() as conn:
            conn.execute("INSERT INTO pc_guided_worker_leases(job_id,owner_id,expires_at_utc,updated_at_utc) VALUES(?,?,?,?)", (job["id"], "other", "2999-01-01T00:00:00+00:00", "now")); conn.commit()
    def listing(*_args):
        calls.append("listing")
        return {"status": 200, "request_id": "listing", "api_sku": "WRONG" if failure == "listing_identity" else "DI-106", "api_asins": ["B0CC2HZNFH"], "product_type": "DISHWARE_PLATE", "attributes": {"child_parent_sku_relationship": [{"parent_sku": "Prince_Plate"}]}, "issues": []}
    def catalog(*_args):
        calls.append("catalog")
        parent = "B0OTHER01" if failure == "catalog_parent" else "B0HJW82XKN"
        return {"asin": "B0CC2HZNFH", "relationships": [{"relationships": [{"parentAsins": [parent]}]}]}, {"http_status": 200, "request_id": "catalog"}
    with pytest.raises(ValueError, match="hash|lease|ambiguous|identity"):
        worker.reconcile_accepted_di106_get(job["id"], "wrong" if failure == "hash" else job["family_hash"], object(), {"listing": listing, "catalog": catalog, "parent_put": lambda *_: pytest.fail("PUT must never run"), "child_patch": lambda *_: pytest.fail("PATCH must never run")})
    if failure in ("lease", "hash"): assert calls == []
    stored = guided.get_job(job["id"])
    assert stored["state"] == "NEEDS_ATTENTION"
    assert stored["operations"][1]["state"] == "ACCEPTED" and stored["operations"][1]["attempt_count"] == 1
    assert stored["operations"][2]["state"] == "PREPARED" and stored["operations"][2]["attempt_count"] == 0


def _persisted_accepted_di105_ambiguity_job():
    job = _persisted_accepted_di106_ambiguity_job()
    guided.update_operation(job["id"], "child:B0CC2HZNFH", "VERIFIED")
    guided.update_operation(job["id"], "child:B0CC2JL6F6", "SUBMITTING")
    guided.update_operation(job["id"], "child:B0CC2JL6F6", "ACCEPTED", request_id="accepted-di105", http_status=200)
    return guided.get_job(job["id"])


def test_guarded_accepted_di105_reconciliation_is_get_only_and_returns_sanitized_operation():
    from services import parent_child_guided_worker as worker
    job = _persisted_accepted_di105_ambiguity_job(); calls = []; auth = object()
    def listing(actual_auth, _market, _vendor, sku):
        calls.append(("listing", actual_auth, sku))
        return {"status": 200, "request_id": "listing-di105", "api_sku": "DI-105", "api_asins": ["B0CC2JL6F6"], "product_type": "DISHWARE_PLATE", "attributes": {"child_parent_sku_relationship": [{"parent_sku": "Prince_Plate"}]}, "issues": []}
    def catalog(actual_auth, _market, asin):
        calls.append(("catalog", actual_auth, asin))
        return {"asin": "B0CC2JL6F6", "relationships": [{"relationships": [{"parentAsins": ["B0HJW82XKN"]}]}]}, {"http_status": 200, "request_id": "catalog-di105"}
    result = worker.reconcile_accepted_child_get(job["id"], job["family_hash"], "child:B0CC2JL6F6", auth, {"listing": listing, "catalog": catalog, "parent_put": lambda *_: pytest.fail("PUT must never run"), "child_patch": lambda *_: pytest.fail("PATCH must never run")})
    assert [item[0] for item in calls] == ["listing", "catalog"] and all(item[1] is auth for item in calls)
    assert result == {"job_id": job["id"], "operation_key": "child:B0CC2JL6F6", "vendor_sku": "DI-105", "asin": "B0CC2JL6F6", "state": "VERIFIED", "attempt_count": 1, "request_id": "accepted-di105", "http_status": 200, "next_action": "VALIDATE_CHILDREN"}
    assert "family" not in result and "operations" not in result
    stored = guided.get_job(job["id"])
    assert stored["operations"][1]["state"] == "VERIFIED" and stored["operations"][2]["state"] == "VERIFIED"


@pytest.mark.parametrize("failure", ["wrong_operation", "wrong_hash", "wrong_sku", "wrong_asin", "wrong_product_type", "wrong_parent", "not_paused"])
def test_guarded_accepted_di105_reconciliation_fails_closed_without_writes(failure):
    from services import parent_child_guided_worker as worker
    job = _persisted_accepted_di105_ambiguity_job(); calls = []
    if failure == "not_paused": worker.set_execution_control("AUTOMATIC", confirmation="ENABLE AUTOMATIC PROCESSING")
    def listing(*_args):
        calls.append("listing")
        return {"status": 200, "request_id": "listing", "api_sku": "WRONG" if failure == "wrong_sku" else "DI-105", "api_asins": ["B0WRONG" if failure == "wrong_asin" else "B0CC2JL6F6"], "product_type": "WRONG" if failure == "wrong_product_type" else "DISHWARE_PLATE", "attributes": {"child_parent_sku_relationship": [{"parent_sku": "Prince_Plate"}]}, "issues": []}
    def catalog(*_args):
        calls.append("catalog")
        return {"asin": "B0CC2JL6F6", "relationships": [{"relationships": [{"parentAsins": ["B0OTHER" if failure == "wrong_parent" else "B0HJW82XKN"]}]}]}, {"http_status": 200, "request_id": "catalog"}
    with pytest.raises(ValueError, match="operation|hash|ambiguous|PAUSED|accepted PATCH"):
        worker.reconcile_accepted_child_get(job["id"], "wrong" if failure == "wrong_hash" else job["family_hash"], "child:B0CC2HZNFH" if failure == "wrong_operation" else "child:B0CC2JL6F6", object(), {"listing": listing, "catalog": catalog, "parent_put": lambda *_: pytest.fail("PUT must never run"), "child_patch": lambda *_: pytest.fail("PATCH must never run")})
    if failure in ("wrong_operation", "wrong_hash", "not_paused"): assert calls == []
    stored = guided.get_job(job["id"])
    assert stored["state"] == "NEEDS_ATTENTION" and stored["operations"][2]["state"] == "ACCEPTED" and stored["operations"][2]["attempt_count"] == 1


def _child_authorization_adapters(parents=None, listing_mutation=None, catalog_mutation=None):
    """Exact sanitized GET fixtures, with write transports that prove preparation is GET-only."""
    parents = parents or {}
    calls = []
    asins = {"CHILD-1": "B000000001", "CHILD-2": "B000000002"}
    def listing(auth, _marketplace, _vendor, sku):
        calls.append(("listing", auth, sku))
        row = {"status": 200, "request_id": "listing-" + sku, "api_sku": sku,
               "api_asins": [asins[sku]], "product_type": "TYPE", "attributes": {}, "issues": []}
        if listing_mutation: listing_mutation(row, sku)
        return row
    def catalog(auth, _marketplace, asin):
        calls.append(("catalog", auth, asin))
        relation = {"relationships": [{"relationships": [{"parentAsins": parents.get(asin, [])}]}]}
        value = {"asin": asin, **relation}
        details = {"http_status": 200, "request_id": "catalog-" + asin}
        if catalog_mutation: catalog_mutation(value, details, asin)
        return value, details
    return {"listing": listing, "catalog": catalog,
            "parent_put": lambda *_: pytest.fail("Child authorization preparation must never PUT"),
            "child_patch": lambda *_: pytest.fail("Child authorization preparation must never PATCH")}, calls


@pytest.mark.parametrize("case", ["wrong_sku", "wrong_asin", "wrong_type", "different_parent", "multiple_parents", "malformed_catalog", "missing_metadata"])
def test_persisted_child_authorization_preparation_fails_closed_for_identity_and_evidence(case):
    from services import parent_child_guided_worker as worker
    job = _persisted_child_authorization_job()
    def list_mutation(row, _sku):
        if case == "wrong_sku": row["api_sku"] = "OTHER"
        if case == "wrong_asin": row["api_asins"] = ["B000000099"]
        if case == "wrong_type": row["product_type"] = "OTHER"
        if case == "missing_metadata": row.pop("request_id")
    def catalog_mutation(value, details, asin):
        if case == "different_parent": value["relationships"][0]["relationships"][0]["parentAsins"] = ["B0OTHER01"]
        if case == "multiple_parents": value["relationships"][0]["relationships"][0]["parentAsins"] = ["B0OTHER01", "B0OTHER02"]
        if case == "malformed_catalog": value["relationships"] = {"not": "a list"}
        if case == "missing_metadata": details.pop("request_id")
    api, calls = _child_authorization_adapters(listing_mutation=list_mutation, catalog_mutation=catalog_mutation)
    with pytest.raises(ValueError, match="ambiguous|malformed|different parent|exact fresh GET eligibility"):
        worker.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    assert calls
    stored = guided.get_job(job["id"])
    assert all(item["attempt_count"] == 0 and item["state"] == "PREPARED" for item in stored["operations"][1:])


def test_persisted_child_authorization_preparation_is_get_only_and_reconciles_already_correct_child():
    from services import parent_child_guided_worker as worker
    job = _persisted_child_authorization_job()
    # Listings is authoritative for an already-correct child; it gets reconciled, never authorized for PATCH.
    def list_mutation(row, sku):
        if sku == "CHILD-1": row["attributes"] = {"child_parent_sku_relationship": [{"parent_sku": "GUIDED-PARENT"}]}
    api, calls = _child_authorization_adapters(parents={"B000000001": ["B0PARENT01"]}, listing_mutation=list_mutation)
    prepared = worker.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    assert [entry["vendor_sku"] for entry in prepared["plan"]] == ["CHILD-2"]
    assert all(name in ("listing", "catalog") for name, *_ in calls)
    stored = guided.get_job(job["id"])
    assert stored["operations"][1]["state"] == "VERIFIED" and stored["operations"][1]["attempt_count"] == 0
    assert stored["operations"][2]["state"] == "PREPARED" and stored["operations"][2]["attempt_count"] == 0


def test_persisted_child_authorization_confirmation_binds_exact_hash_plan_expiry_lease_and_audit():
    from services import parent_child_guided_worker as worker
    from services.db import get_db_connection
    job = _persisted_child_authorization_job(); api, _ = _child_authorization_adapters()
    prepared = worker.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    with pytest.raises(ValueError, match="not eligible"):
        worker.confirm_child_write_authorization(job["id"], "wrong", prepared["plan_hash"], "AUTHORIZE CHILDREN")
    with pytest.raises(ValueError, match="Fresh unconfirmed"):
        worker.confirm_child_write_authorization(job["id"], job["family_hash"], "wrong", "AUTHORIZE CHILDREN")
    with pytest.raises(ValueError, match="Exact AUTHORIZE"):
        worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "authorize children")
    with get_db_connection() as conn:
        conn.execute("INSERT INTO pc_guided_worker_leases(job_id,owner_id,expires_at_utc,updated_at_utc) VALUES(?,?,?,?)", (job["id"], "other", "2999-01-01T00:00:00+00:00", "now")); conn.commit()
    with pytest.raises(ValueError, match="unleased"):
        worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
    with get_db_connection() as conn:
        conn.execute("DELETE FROM pc_guided_worker_leases WHERE job_id=?", (job["id"],))
        conn.execute("UPDATE pc_guided_child_write_authorizations SET expires_at_utc='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["id"],)); conn.commit()
    with pytest.raises(ValueError, match="Fresh unconfirmed"):
        worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
    prepared = worker.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    with get_db_connection() as conn:
        conn.execute("UPDATE pc_guided_operations SET payload_hash='changed' WHERE job_id=? AND operation_key='child:B000000001'", (job["id"],)); conn.commit()
    with pytest.raises(ValueError, match="ledger changed|plan has changed"):
        worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
    # Restore the exact ledger value, then verify the durable sanitized confirmation audit.
    original_hash = next(entry["operation_payload_hash"] for entry in prepared["plan"] if entry["operation_key"] == "child:B000000001")
    with get_db_connection() as conn:
        conn.execute("UPDATE pc_guided_operations SET payload_hash=? WHERE job_id=? AND operation_key='child:B000000001'", (original_hash, job["id"])); conn.commit()
    prepared = worker.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
    with get_db_connection() as conn:
        audit = conn.execute("SELECT action,evidence_json FROM pc_guided_child_write_authorization_events WHERE authorization_id=(SELECT id FROM pc_guided_child_write_authorizations WHERE job_id=?) ORDER BY id", (job["id"],)).fetchall()
    assert audit[-1]["action"] == "EXACT_PLAN_CONFIRMED"
    assert any(row["action"] == "GET_PREPARED" for row in audit)
    assert "local_user" not in audit[-1]["evidence_json"]


def test_persisted_child_authorization_consumption_is_atomic_restart_safe_and_renews_only_after_verified_sibling():
    from services import parent_child_guided_worker as worker
    from services.db import get_db_connection
    job = _persisted_child_authorization_job(); api, _ = _child_authorization_adapters(); prepared = worker.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
    first = guided.get_job(job["id"])["operations"][1]
    body = next(item["body"] for item in worker._runtime(job["id"])["snapshot"]["payload"]["children"] if item["asin"] == first["child_asin"])
    changed_body = json.loads(json.dumps(body)); changed_body["patches"][-1]["value"] = [{"value": "wrong"}]
    with pytest.raises(ValueError, match="changed or already consumed"):
        worker._child_write_authorized(guided.get_job(job["id"]), first, changed_body)
    consumed = []
    def consume():
        try:
            worker._child_write_authorized(guided.get_job(job["id"]), first, body)
            consumed.append("consumed")
        except ValueError:
            consumed.append("rejected")
    threads = [threading.Thread(target=consume), threading.Thread(target=consume)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert sorted(consumed) == ["consumed", "rejected"]
    with pytest.raises(ValueError, match="changed or already consumed"):
        worker._child_write_authorized(guided.get_job(job["id"]), first, body)
    # Reloading the real module cannot turn a durable consumed authorization back into a usable one.
    import importlib
    reloaded = importlib.reload(worker)
    with pytest.raises(ValueError, match="changed or already consumed"):
        reloaded._child_write_authorized(guided.get_job(job["id"]), first, body)
    guided.update_operation(job["id"], first["operation_key"], "SUBMITTING")
    guided.update_operation(job["id"], first["operation_key"], "ACCEPTED", request_id="one", http_status=202)
    guided.update_operation(job["id"], first["operation_key"], "VERIFIED")
    with get_db_connection() as conn:
        conn.execute("UPDATE pc_guided_child_write_authorizations SET expires_at_utc='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["id"],)); conn.commit()
    renewed = reloaded.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    assert [entry["operation_key"] for entry in renewed["plan"]] == ["child:B000000002"]
    reloaded.confirm_child_write_authorization(job["id"], job["family_hash"], renewed["plan_hash"], "AUTHORIZE CHILDREN")
    with get_db_connection() as conn:
        consumed = json.loads(conn.execute("SELECT consumed_operations_json FROM pc_guided_child_write_authorizations WHERE job_id=?", (job["id"],)).fetchone()[0])
    assert consumed == ["child:B000000001"]


def test_persisted_child_authorization_rejects_malformed_evidence_and_double_confirmation():
    from services import parent_child_guided_worker as worker
    from services.db import get_db_connection
    job = _persisted_child_authorization_job(); api, _ = _child_authorization_adapters(); prepared = worker.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    with get_db_connection() as conn:
        original = conn.execute("SELECT child_evidence_json FROM pc_guided_child_write_authorizations WHERE job_id=?", (job["id"],)).fetchone()[0]
        conn.execute("UPDATE pc_guided_child_write_authorizations SET child_evidence_json='not-json' WHERE job_id=?", (job["id"],)); conn.commit()
    with pytest.raises(ValueError, match="malformed"):
        worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
    with get_db_connection() as conn:
        conn.execute("UPDATE pc_guided_child_write_authorizations SET child_evidence_json=? WHERE job_id=?", (original, job["id"])); conn.commit()
    results = []
    def confirm():
        try:
            worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
            results.append("confirmed")
        except ValueError:
            results.append("rejected")
    threads = [threading.Thread(target=confirm), threading.Thread(target=confirm)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert sorted(results) == ["confirmed", "rejected"]


@pytest.mark.parametrize("http_status,outcome", [(202, "ACCEPTED"), (422, "REJECTED"), (500, "UNKNOWN")])
def test_persisted_child_authorization_records_one_shot_patch_outcomes_without_retry(http_status, outcome):
    from services import parent_child_guided_worker as worker
    from services.db import get_db_connection
    job = _persisted_child_authorization_job(); api, _ = _child_authorization_adapters(); auth = object()
    prepared = worker.prepare_child_write_authorization(job["id"], job["family_hash"], auth, api)
    worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
    attempts = []
    api["child_patch"] = lambda *_: (attempts.append(1) or {"http_status": http_status, "request_id": "patch-safe", "issues": []})
    worker.process_next_safe_action(job["id"], auth, api)
    stored = guided.get_job(job["id"]); first = stored["operations"][1]
    assert attempts == [1] and first["attempt_count"] == 1 and first["state"] == outcome
    with get_db_connection() as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM pc_guided_child_write_authorization_events WHERE operation_key=?", (first["operation_key"],))]
    assert "CONSUMED_BEFORE_PATCH" in actions and "PATCH_" + outcome in actions
    worker.process_next_safe_action(job["id"], auth, api)
    assert attempts == [1]


def test_persisted_child_authorization_scheduler_fails_closed_and_selected_preparation_leaves_unrelated_job_untouched():
    from services import parent_child_guided_worker as worker
    job = _persisted_child_authorization_job(); api, _ = _child_authorization_adapters(); writes = []
    api["child_patch"] = lambda *_: writes.append("patch")
    # Scheduler sees a real eligible job, but cannot cross the explicit authorization boundary.
    worker.scheduler_tick(object(), api, "isolated-owner")
    assert writes == []
    assert guided.get_job(job["id"])["operations"][1]["attempt_count"] == 0
    value = draft()
    other_saved = guided.save_draft(family(parent_sku="UNRELATED-AUTH-PARENT", vendor_context_id=value["family"]["vendor_context_id"], children=[{"asin": "B000000031", "vendor_sku": "UNRELATED-1", "variation_value": "7 INCH"}, {"asin": "B000000032", "vendor_sku": "UNRELATED-2", "variation_value": "9 INCH"}]), value["id"])
    other = guided.approve(other_saved["id"], other_saved["family_hash"])
    worker.prepare_child_write_authorization(job["id"], job["family_hash"], object(), api)
    assert guided.get_job(other["id"])["state"] == "APPROVED"


def test_child_authorization_migration_applies_on_fresh_and_prior_migration_databases():
    """The isolation fixture starts fresh; replaying 016 models a database at migration 015."""
    from services.db import get_db_connection
    from services.parent_child_migrations import ensure_parent_child_schema
    with get_db_connection() as conn:
        assert conn.execute("SELECT 1 FROM pc_schema_migrations WHERE version='016_guided_child_write_authorizations'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pc_guided_child_write_authorizations'").fetchone()
        conn.execute("DROP TABLE pc_guided_child_write_authorization_events")
        conn.execute("DROP TABLE pc_guided_child_write_authorizations")
        conn.execute("DELETE FROM pc_schema_migrations WHERE version='016_guided_child_write_authorizations'")
        conn.commit()
    ensure_parent_child_schema()
    with get_db_connection() as conn:
        assert conn.execute("SELECT 1 FROM pc_schema_migrations WHERE version='016_guided_child_write_authorizations'").fetchone()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pc_guided_child_write_authorizations)")}
    assert {"family_hash", "plan_hash", "child_evidence_json", "expires_at_utc", "confirmed_at_utc", "consumed_operations_json"} <= columns


def test_guarded_persisted_child_reconciliation_uses_auth_and_is_get_only():
    from services import parent_child_guided_worker as worker
    job = _persisted_verified_parent_children_prepared_job(); configured_auth = object(); calls = []
    def listing(auth, _marketplace, _vendor, sku):
        calls.append(("listing", auth, sku))
        relationship = [{"parent_sku": "GUIDED-PARENT"}] if sku == "CHILD-1" else []
        asin = "B000000001" if sku == "CHILD-1" else "B000000002"
        return {"status": 200, "request_id": "listing-" + sku, "api_sku": sku, "api_asins": [asin], "product_type": "TYPE", "attributes": {"child_parent_sku_relationship": relationship}, "issues": []}
    def catalog(auth, _marketplace, asin):
        calls.append(("catalog", auth, asin))
        return {"asin": asin, "relationships": [{"relationships": [{"parentAsins": ["B0PARENT01"]}]}]}, {"http_status": 200, "request_id": "catalog-" + asin}
    result = worker.reconcile_prepared_children_get(job["id"], job["family_hash"], configured_auth, {"listing": listing, "catalog": catalog, "parent_put": lambda *_: pytest.fail("PUT must not run"), "child_patch": lambda *_: pytest.fail("PATCH must not run")})
    assert [item[0] for item in calls] == ["listing", "catalog", "listing", "catalog"]
    assert all(item[1] is configured_auth for item in calls)
    assert all(child["attached"] for child in result["children"])
    stored = guided.get_job(job["id"])
    assert stored["operations"][0]["state"] == "VERIFIED" and stored["operations"][0]["attempt_count"] == 1
    assert all(item["state"] == "VERIFIED" and item["attempt_count"] == 0 for item in stored["operations"][1:])
    assert stored["state"] == "NEEDS_ATTENTION"


@pytest.mark.parametrize("kind", ["missing_auth", "malformed_identity", "wrong_parent"])
def test_guarded_child_reconciliation_fails_closed_without_child_write(kind):
    from services import parent_child_guided_worker as worker
    job = _persisted_verified_parent_children_prepared_job(); calls = []
    def listing(auth, _marketplace, _vendor, sku):
        calls.append(("listing", auth, sku))
        if kind == "malformed_identity": return {"status": 200, "api_sku": "WRONG-SKU", "product_type": "TYPE", "attributes": {}, "issues": []}
        return {"status": 200, "api_sku": sku, "product_type": "TYPE", "attributes": {"child_parent_sku_relationship": [{"parent_sku": "OTHER-PARENT"}]}, "issues": []}
    if kind == "missing_auth":
        with pytest.raises(ValueError, match="Configured auth client"):
            worker.reconcile_prepared_children_get(job["id"], job["family_hash"], None, {"listing": listing, "catalog": lambda *_: pytest.fail("Catalog must not run")})
        assert not calls
    else:
        with pytest.raises(ValueError, match="ambiguous|different parent"):
            worker.reconcile_prepared_children_get(job["id"], job["family_hash"], object(), {"listing": listing, "catalog": lambda *_: pytest.fail("Catalog must not run"), "parent_put": lambda *_: pytest.fail("PUT must not run"), "child_patch": lambda *_: pytest.fail("PATCH must not run")})
        assert calls
    stored = guided.get_job(job["id"])
    assert stored["state"] == "NEEDS_ATTENTION"
    assert stored["operations"][0]["state"] == "VERIFIED" and stored["operations"][0]["attempt_count"] == 1
    assert all(item["state"] == "PREPARED" and item["attempt_count"] == 0 for item in stored["operations"][1:])


def test_persisted_guided_end_to_end_rehearsal_and_unknown_acknowledgement():
    """Scheduler-free, mocked transport rehearsal of the exact durable worker path."""
    from services import parent_child_guided_worker as worker
    import importlib
    job = approved_job(); allow_selected_guided_job(job); auth = object(); present = set(); writes = []
    schema = {"properties": {"parentage_level": {}, "variation_theme": {}, "child_parent_sku_relationship": {}, "size": {}}, "required": []}
    def listing(actual_auth, _m, _v, sku):
        assert actual_auth is auth
        if sku == "GUIDED-PARENT":
            return {"status": 200 if "parent" in present else 404, "api_sku": sku if "parent" in present else None, "api_asins": ["B0PARENT01"] if "parent" in present else [], "product_type": "TYPE", "parent_verified": "parent" in present, "theme_verified": "parent" in present, "issues": []}
        asin = "B000000001" if sku == "CHILD-1" else "B000000002"
        return {"status": 200, "request_id": "listing-" + sku, "api_sku": sku, "api_asins": [asin], "product_type": "TYPE", "attributes": {}, "issues": []}
    def parent_put(_a, _f, _b, _k): present.add("parent"); writes.append("parent"); return {"http_status": 200, "request_id": "put-parent", "issues": []}
    def child_patch(_a, _f, sku, _b, _k): present.add(sku); writes.append(sku); return {"http_status": 202, "request_id": "patch-" + sku, "issues": []}
    def catalog(_auth, _market, asin):
        sku = "CHILD-1" if asin == "B000000001" else "CHILD-2"
        parents = ["B0PARENT01"] if sku in present else []
        return {"asin": asin, "relationships": [{"relationships": [{"parentAsins": parents}]}]}, {"http_status": 200, "request_id": "catalog-" + asin}
    api = {"get_definition": lambda *_: (schema, {}), "listing": listing, "catalog": catalog, "validate_parent": lambda *_: {"classification": "PASSED", "http_status": 200, "issues": []}, "validate_child": lambda *_: {"classification": "PASSED", "http_status": 200, "issues": []}, "parent_put": parent_put, "child_patch": child_patch}
    for _ in range(3): worker.process_next_safe_action(job["id"], auth, api)
    worker = importlib.reload(worker)  # persisted state, not in-memory recovery
    for _ in range(2): worker.process_next_safe_action(job["id"], auth, api)
    prepared = worker.prepare_child_write_authorization(job["id"], job["family_hash"], auth, api)
    assert writes == ["parent"]
    worker.confirm_child_write_authorization(job["id"], job["family_hash"], prepared["plan_hash"], "AUTHORIZE CHILDREN")
    for _ in range(6): worker.process_next_safe_action(job["id"], auth, api)
    stored = guided.get_job(job["id"])
    assert stored["state"] == "COMPLETED" and writes == ["parent", "CHILD-1", "CHILD-2"]
    assert [item["attempt_count"] for item in stored["operations"]] == [1, 1, 1]
    value = draft()
    saved = guided.save_draft(family(parent_sku="UNKNOWN-PARENT", vendor_context_id=value["family"]["vendor_context_id"], children=[{"asin": "B000000021", "vendor_sku": "UNKNOWN-1", "variation_value": "7 INCH"}, {"asin": "B000000022", "vendor_sku": "UNKNOWN-2", "variation_value": "9 INCH"}]), value["id"])
    unknown = guided.approve(saved["id"], saved["family_hash"])
    allow_selected_guided_job(unknown)
    unknown_api = dict(api, listing=lambda actual_auth, _m, _v, sku: {"status": 404, "api_sku": None, "issues": []} if sku == "UNKNOWN-PARENT" else listing(actual_auth, _m, _v, sku), parent_put=lambda *_: {"http_status": 500, "request_id": "unknown", "issues": []})
    for _ in range(3): worker.process_next_safe_action(unknown["id"], auth, unknown_api)
    blocked = guided.get_job(unknown["id"])
    assert blocked["state"] == "SUBMISSION_UNKNOWN" and blocked["operations"][0]["state"] == "UNKNOWN" and blocked["operations"][0]["attempt_count"] == 1


def test_scheduler_free_route_loads_guarded_child_reconciliation_without_unrelated_work(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import parent_child_routes as routes
    from services import parent_child_guided_worker as worker
    job = _persisted_verified_parent_children_prepared_job(); value = draft(); saved = guided.save_draft(family(parent_sku="UNRELATED-PARENT", vendor_context_id=value["family"]["vendor_context_id"], children=[{"asin": "B000000031", "vendor_sku": "UNRELATED-1", "variation_value": "7 INCH"}, {"asin": "B000000032", "vendor_sku": "UNRELATED-2", "variation_value": "9 INCH"}]), value["id"]); unrelated = guided.approve(saved["id"], saved["family_hash"]); auth = object()
    def listing(_a, _m, _v, sku):
        asin = "B000000001" if sku == "CHILD-1" else "B000000002"
        return {"status": 200, "request_id": "listing-" + sku, "api_sku": sku, "api_asins": [asin], "product_type": "TYPE", "attributes": {}, "issues": []}
    monkeypatch.setattr(worker, "production_adapters", lambda: {"listing": listing, "catalog": lambda _a,_m,asin: ({"asin": asin, "relationships": [{"relationships": [{"parentAsins": []}]}]}, {"http_status": 200, "request_id": "catalog-" + asin})})
    app = FastAPI(); routes.register_parent_child_routes(app, auth)
    assert not hasattr(app.state, "guided_scheduler")
    response = TestClient(app).post(f"/api/parent-child/guided/jobs/{job['id']}/reconcile-prepared-children", json={"family_hash": job["family_hash"]})
    assert response.status_code == 200
    assert guided.get_job(unrelated["id"])["state"] == "APPROVED"


def test_guided_scheduler_disabled_startup_setting(monkeypatch):
    source = Path("main.py").read_text(encoding="utf-8")
    assert "def guided_scheduler_enabled()" in source
    assert 'os.getenv("GUIDED_SCHEDULER_DISABLED"' in source
    assert "if guided_scheduler_enabled():" in source


def test_needs_attention_prewrite_cancellation_retains_audit_and_releases_ownership():
    from services import parent_child_guided_worker as worker
    from services.db import get_db_connection
    first = approved_job()
    guided.transition(first["id"], "NEEDS_ATTENTION", {"message": "Local PTD validation failed"})
    cancelled = worker.cancel(first["id"], "Superseded by corrected draft before any write attempt")
    assert cancelled["state"] == "CANCELLED_BEFORE_WRITES"
    assert all(item["state"] == "PREPARED" and item["attempt_count"] == 0 for item in cancelled["operations"])
    with get_db_connection() as conn:
        events = [row[0] for row in conn.execute("SELECT evidence_json FROM pc_guided_events WHERE job_id=?", (first["id"],))]
    assert any("Local PTD validation failed" in value for value in events)
    replacement = draft()
    replacement = guided.save_draft(family(parent_title="Replacement title", vendor_context_id=replacement["family"]["vendor_context_id"]), replacement["id"])
    replacement_job = guided.approve(replacement["id"], replacement["family_hash"])
    assert replacement_job["id"] != first["id"]
    assert first["id"] not in worker.eligible_jobs()
    with pytest.raises(ValueError, match="cannot be processed"):
        worker.process_next_safe_action(first["id"])


def test_cancellation_refuses_uncertain_submission_history():
    from services import parent_child_guided_worker as worker
    job = approved_job()
    guided.update_operation(job["id"], "parent", "SUBMITTING")
    with pytest.raises(ValueError, match="conclusively pre-write"):
        worker.cancel(job["id"])


def test_saved_jobs_inventory_and_exact_job_navigation_are_local_only(monkeypatch):
    from routes import parent_child_routes as routes
    calls = []
    monkeypatch.setattr(routes, "_checked_context", lambda *args, **kwargs: calls.append("amazon"))
    job = approved_job()
    guided.transition(job["id"], "NEEDS_ATTENTION", {"message": "Local PTD validation failed"})
    listing = routes.guided_jobs()["items"]
    item = next(value for value in listing if value["id"] == job["id"])
    assert item["state"] == "NEEDS_ATTENTION"
    assert item["failure_reason"] == {"message": "Local PTD validation failed"}
    assert item["write_attempted"] is False
    assert routes.guided_job(job["id"])["job"]["id"] == job["id"]
    assert not calls
    ui = Path("ui/js/parent_child_guided_mode.js").read_text(encoding="utf-8")
    assert 'api("/guided/jobs")' in ui and 'api(`/guided/jobs/${id}`)' in ui
    assert "Failure reason:" in ui and "Write attempted:" in ui


def test_open_job_polling_is_local_non_overlapping_and_stops_with_the_panel():
    ui = Path("ui/js/parent_child_guided_mode.js").read_text(encoding="utf-8")
    assert "jobPollTimer:null,jobPollBusy:false" in ui
    assert "window.setInterval(()=>refreshOpenJob().catch(e=>error(e.message)),30000)" in ui
    assert "if(!id||state.jobPollBusy)return" in ui
    assert "api(`/guided/jobs/${id}`),api(`/guided/jobs/${id}/progress`)" in ui
    assert "Last checked:" in ui and "Refresh status" in ui
    assert "function stopJobPolling()" in ui
    assert "stopJobPolling();state.selectedJob=null;return showJobsList();" in ui
    assert "async function openDraft(id){stopJobPolling();state.selectedJob=null;" in ui
    assert "parent_put" not in ui and "child_patch" not in ui


def test_phase_three_worker_lease_and_guided_ui_contract():
    from services import parent_child_guided_worker as worker
    from pathlib import Path
    job = approved_job(); allow_selected_guided_job(job)
    assert worker.claim_job(job["id"], "one") is True
    assert worker.claim_job(job["id"], "two") is False
    worker.release_job(job["id"], "one")
    assert worker.claim_job(job["id"], "two") is True
    html = Path("ui/index.html").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_guided_mode.js").read_text(encoding="utf-8")
    assert "Guided Mode" in html and "Advanced Mode" in html
    assert "Confirm and create family" in html and "I confirm immutable family hash" in html
    assert "typed" not in js.lower() and "VALIDATE" not in js
    assert "parent_put" not in js and "child_patch" not in js


def test_guided_ui_encoding_and_saved_drafts_are_local_only():
    from pathlib import Path
    from routes import parent_child_routes as routes
    source = Path("ui/index.html").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_guided_mode.js").read_text(encoding="utf-8")
    assert "Advanced Mode — troubleshooting and technical manager" in source
    assert not any(value in source + js for value in ("â€", "Â", "�"))
    assert 'id="pcg-drafts"' in source and "Saved drafts" in source
    value = draft()
    items = routes.guided_drafts()["items"]
    assert [item["id"] for item in items] == [value["id"]]
    duplicate = routes.guided_duplicate_draft(value["id"])["draft"]
    assert duplicate["id"] != value["id"]
    assert not duplicate["approved"]
    routes.guided_delete_draft(duplicate["id"])
    assert routes.guided_draft(value["id"])["draft"]["family"]["children"][0]["asin"] == "B000000001"
    assert "showDrafts" in js and "openDraft" in js and "queueSave" in js


def test_guided_readiness_preserves_parent_provenance_without_job():
    value = draft()
    definition = family(
        parent_title="Prince Party Plate Pack of 10",
        parent_required_values={"brand": {"value": "Goldedge", "provenance": {"source": "unanimous verified child attributes", "asins": ["B000000001", "B000000002"]}}},
        children=[
            {"asin": "B000000001", "vendor_sku": "CHILD-1", "variation_value": "7 Inch", "variation_values": {"size": "7 Inch"}, "verified_attributes": {"brand": "Goldedge"}},
            {"asin": "B000000002", "vendor_sku": "CHILD-2", "variation_value": "9 Inch", "variation_values": {"size": "9 Inch"}, "verified_attributes": {"brand": "Goldedge"}},
        ],
        vendor_context_id=value["family"]["vendor_context_id"],
    )
    saved = guided.save_draft(definition, value["id"])
    assert saved["family"]["parent_title"] == "Prince Party Plate Pack of 10"
    assert saved["family"]["parent_required_values"]["brand"]["provenance"]["source"] == "unanimous verified child attributes"
    assert saved["family"]["children"][0]["verified_attributes"]["brand"] == "Goldedge"
    assert not saved["approved"]
    ui = Path("ui/js/parent_child_guided_mode.js").read_text(encoding="utf-8")
    assert "Requirements not loaded" in ui
    """
    assert "Requirements not loaded — try Find Products again." in ui
    """
    assert "Complete every required parent field." in ui
    assert "Family is ready for review." in ui
    assert "await progress(d.job.id,latest.id,state.review.family_hash)" in ui
    assert "function openDraft" in ui and "/current-job" in ui and "d.family_hash" in ui


def test_cancelled_prepared_job_is_permanently_rejected_by_worker():
    from services import parent_child_guided_worker as worker
    job = approved_job()
    guided.transition(job["id"], "NEEDS_ATTENTION", {"message": "obsolete draft"})
    cancelled = worker.cancel(job["id"])
    assert cancelled["state"] == "CANCELLED_BEFORE_WRITES"
    assert guided.next_action(job["id"]) == "NONE"
    with pytest.raises(ValueError, match="cannot be processed"):
        worker.process_next_safe_action(job["id"])
    assert job["id"] not in worker.eligible_jobs()


def test_review_readiness_fails_closed_for_unloaded_empty_and_missing_requirements():
    import json
    from services.db import get_db_connection
    value = draft()
    assert guided.review_readiness(value["id"])["status"] == "requirements_unavailable"
    with get_db_connection() as conn:
        conn.execute("INSERT INTO pc_ptd_cache(marketplace_id,vendor_code,product_type,parentage_level,schema_json,fetched_at_utc) VALUES(?,?,?,?,?,?)", ("M", "VENDOR", "TYPE", "parent", json.dumps({"required": []}), "now")); conn.commit()
    assert guided.review_readiness(value["id"])["status"] == "ready"
    with get_db_connection() as conn:
        conn.execute("UPDATE pc_ptd_cache SET schema_json=? WHERE marketplace_id='M' AND vendor_code='VENDOR' AND product_type='TYPE' AND parentage_level='parent'", (json.dumps({"required": ["bullet_point"]}),)); conn.commit()
    assert guided.review_readiness(value["id"])["status"] == "missing_requirements"
    complete = guided.save_draft(family(parent_required_values={"bullet_point": {"value": "Goldedge", "provenance": {"source": "verified"}}}, vendor_context_id=value["family"]["vendor_context_id"]), value["id"])
    assert guided.review_readiness(complete["id"])["ready"]


def test_current_job_endpoint_rejects_cancelled_or_hash_mismatch_job():
    from routes import parent_child_routes as routes
    job = approved_job(); guided.transition(job["id"], "NEEDS_ATTENTION", {"message": "obsolete"})
    from services import parent_child_guided_worker as worker
    worker.cancel(job["id"])
    assert routes.guided_current_job(job["draft_id"])["job"] is None


def test_confirmation_summary_renders_complete_immutable_review_scope():
    value = draft()
    definition = family(
        parent_sku="Prince_Plate",
        parent_title="Prince Party Plate Pack of 10",
        product_type="DISHWARE_PLATE",
        marketplace_id="A2VIGQ35RCS4UG",
        vendor_code="FK6EI",
        parent_required_values={"bullet_point": {"value": "Blue Prince Party Plates, Pack of 10", "provenance": {"source": "manual"}}},
        children=[
            {"asin": "B0CC2HZNFH", "vendor_sku": "DI-106", "variation_value": "7 Inch"},
            {"asin": "B0CC2JL6F6", "vendor_sku": "DI-105", "variation_value": "9 Inch"},
        ],
        vendor_context_id=value["family"]["vendor_context_id"],
    )
    saved = guided.save_draft(definition, value["id"])
    review = guided.review_summary(saved["id"])
    assert review["family_hash"] == review["canonical_snapshot_hash"] == saved["family_hash"]
    assert review["marketplace"] == "UAE / A2VIGQ35RCS4UG"
    assert [child["asin"] for child in review["children"]] == ["B0CC2HZNFH", "B0CC2JL6F6"]
    assert [(child["vendor_sku"], child["variation_value"]) for child in review["children"]] == [("DI-106", "7 Inch"), ("DI-105", "9 Inch")]
    assert review["parent_required_values"]["bullet_point"]["value"] == "Blue Prince Party Plates, Pack of 10"


def test_confirmation_client_fails_closed_for_scope_hash_and_browser_tampering():
    ui = Path("ui/js/parent_child_guided_mode.js").read_text(encoding="utf-8")
    page = Path("ui/index.html").read_text(encoding="utf-8")
    assert "function renderConfirmation(review)" in ui
    assert "review.children.map" in ui and "Object.entries(review.parent_required_values)" in ui
    assert "Immutable approval hash:" in ui and 'id="pcg-confirm-hash"' in page
    assert '["asin","vendor_sku","variation_value"]' in ui
    assert "review.family_hash!==draft.family_hash" in ui
    assert "same(values,family.parent_required_values)" in ui
    assert "await api(`/guided/drafts/${state.draft.id}`)" in ui
    assert 'body:JSON.stringify({family_hash:state.review.family_hash})' in ui
    assert "Return to edit and review it again." in ui


def test_guided_parent_localized_values_are_unboxed_once_and_size_patches_stay_unchanged():
    from services import parent_child_guided_worker as worker
    from services.parent_child_preview import build_preview
    from services.parent_child_safety import full_schema_errors, patch_errors
    scalar = {"batteries_required": "boolean", "brand": "string", "country_of_origin": "string", "product_category": "string", "product_subcategory": "string", "supplier_declared_dg_hz_regulation": "string"}
    def localized(kind, language=False):
        properties = {"value": {"type": kind}, "marketplace_id": {"type": "string"}}
        required = ["value"]
        if language: properties["language_tag"] = {"type": "string"}; required.append("language_tag")
        return {"type": "array", "items": {"type": "object", "properties": properties, "required": required}}
    parent = {"$defs": {"language_tag": {"default": "en_AE"}}, "properties": {name: localized(kind, name == "brand") for name, kind in scalar.items()}}
    wrapped = {
        "batteries_required": [{"marketplace_id": "A2VIGQ35RCS4UG", "value": False}],
        "brand": [{"marketplace_id": "A2VIGQ35RCS4UG", "language_tag": "en_AE", "value": "Goldedge"}],
        "country_of_origin": [{"marketplace_id": "A2VIGQ35RCS4UG", "value": "CN"}],
        "product_category": [{"marketplace_id": "A2VIGQ35RCS4UG", "value": "7503500"}],
        "product_subcategory": [{"marketplace_id": "A2VIGQ35RCS4UG", "value": "7503560"}],
        "supplier_declared_dg_hz_regulation": [{"marketplace_id": "A2VIGQ35RCS4UG", "value": "not_applicable"}],
        "bullet_point": "Blue Prince Party Plates, Pack of 10",
    }
    parent["properties"]["bullet_point"] = localized("string", True)
    normalized = worker._normalize_parent_values(parent, wrapped, "A2VIGQ35RCS4UG")
    assert {name: normalized[name] for name in scalar} == {"batteries_required": False, "brand": "Goldedge", "country_of_origin": "CN", "product_category": "7503500", "product_subcategory": "7503560", "supplier_declared_dg_hz_regulation": "not_applicable"}
    assert normalized["bullet_point"] == wrapped["bullet_point"]
    child = {"properties": {"size": localized("string")}, "required": ["size"]}
    preview = build_preview({"marketplace_id": "A2VIGQ35RCS4UG", "parent_sku": "Prince_Plate", "parent_title": "Prince Party Plate Pack of 10", "product_type": "DISHWARE_PLATE", "variation_theme": "SIZE", "parent_values": normalized, "items": [{"asin": "B0CC2HZNFH", "vendor_sku": "DI-106", "variation_values": {"size": "7 INCH"}}]}, parent, child, {})
    assert not full_schema_errors(parent, preview["parent"]["body"]["attributes"], "Parent")
    patches = preview["children"][0]["body"]["patches"]
    assert any(patch["path"] == "/attributes/size" and patch["value"] == [{"value": "7 INCH"}] for patch in patches)
    assert not patch_errors(child, patches, {}, "B0CC2HZNFH")[0]


@pytest.mark.parametrize("value, message", [([], "one PTD-compatible"), ([{"marketplace_id": "M", "value": "a"}, {"marketplace_id": "M", "value": "b"}], "one PTD-compatible"), ([{"marketplace_id": "M"}], "invalid localized"), ([{"marketplace_id": "OTHER", "value": "a"}], "mismatched marketplace"), ([{"marketplace_id": "M", "language_tag": "fr_FR", "value": "a"}], "mismatched language")])
def test_guided_parent_localized_value_normalization_fails_closed(value, message):
    from services import parent_child_guided_worker as worker
    schema = {"$defs": {"language_tag": {"default": "en_AE"}}, "properties": {"brand": {"type": "array", "items": {"type": "object", "properties": {"value": {"type": "string"}, "marketplace_id": {"type": "string"}, "language_tag": {"type": "string"}}, "required": ["value", "language_tag"]}}}}
    with pytest.raises(ValueError, match=message):
        worker._normalize_parent_values(schema, {"brand": value}, "M")


def test_persisted_approved_job_local_validation_unboxes_wrappers_without_transport(monkeypatch):
    from services import parent_child_guided_worker as worker
    scalar = {"batteries_required": "boolean", "brand": "string", "country_of_origin": "string", "product_category": "string", "product_subcategory": "string", "supplier_declared_dg_hz_regulation": "string"}
    def localized(kind, language=False):
        props = {"value": {"type": kind}, "marketplace_id": {"type": "string"}}; required = ["value"]
        if language: props["language_tag"] = {"type": "string"}; required.append("language_tag")
        return {"type": "array", "items": {"type": "object", "properties": props, "required": required}}
    parent = {"$defs": {"language_tag": {"default": "en_AE"}}, "required": list(scalar), "properties": {name: localized(kind, name == "brand") for name, kind in scalar.items()}}
    child = {"properties": {"size": localized("string")}, "required": ["size"]}
    context = draft()
    values = {
        "batteries_required": {"value": [{"marketplace_id": "M", "value": False}], "provenance": {"source": "cache"}},
        "brand": {"value": [{"marketplace_id": "M", "language_tag": "en_AE", "value": "Goldedge"}], "provenance": {"source": "cache"}},
        "country_of_origin": {"value": [{"marketplace_id": "M", "value": "CN"}], "provenance": {"source": "cache"}},
        "product_category": {"value": [{"marketplace_id": "M", "value": "7503500"}], "provenance": {"source": "cache"}},
        "product_subcategory": {"value": [{"marketplace_id": "M", "value": "7503560"}], "provenance": {"source": "cache"}},
        "supplier_declared_dg_hz_regulation": {"value": [{"marketplace_id": "M", "value": "not_applicable"}], "provenance": {"source": "cache"}},
    }
    saved = guided.save_draft(family(vendor_context_id=context["family"]["vendor_context_id"], parent_required_values=values, children=[{"asin": "B000000001", "vendor_sku": "CHILD-1", "variation_value": "7 INCH", "variation_values": {"size": "7 INCH"}}, {"asin": "B000000002", "vendor_sku": "CHILD-2", "variation_value": "9 INCH", "variation_values": {"size": "9 INCH"}}]))
    persisted = guided.get_job(guided.approve(saved["id"], saved["family_hash"])["id"])
    calls = []
    api = {"get_definition": lambda _auth, _market, _vendor, _type, level: (parent if level == "PARENT" else child, {"source": "test-cache"}), "build_preview": worker.build_preview}
    monkeypatch.setattr(worker, "_save_runtime", lambda *args, **kwargs: calls.append("runtime"))
    worker._local_validate(persisted, api, None)
    assert calls == ["runtime"]
    assert all(operation["attempt_count"] == 0 and operation["state"] == "PREPARED" for operation in guided.get_job(persisted["id"])["operations"])


def test_persisted_prewrite_job_can_resume_only_through_cached_local_validation(monkeypatch):
    from services import parent_child_guided_worker as worker
    from services.db import get_db_connection
    scalar = {"batteries_required": "boolean", "brand": "string", "country_of_origin": "string", "product_category": "string", "product_subcategory": "string", "supplier_declared_dg_hz_regulation": "string"}
    def localized(kind, language=False):
        props = {"value": {"type": kind}, "marketplace_id": {"type": "string"}}; required = ["value"]
        if language: props["language_tag"] = {"type": "string"}; required.append("language_tag")
        return {"type": "array", "items": {"type": "object", "properties": props, "required": required}}
    parent = {"$defs": {"language_tag": {"default": "en_AE"}}, "required": list(scalar), "properties": {name: localized(kind, name == "brand") for name, kind in scalar.items()}}
    child = {"properties": {"size": localized("string")}, "required": ["size"]}
    context = draft()
    values = {name: {"value": [{"marketplace_id": "M", "value": False if name == "batteries_required" else "Goldedge" if name == "brand" else "CN" if name == "country_of_origin" else "7503500" if name == "product_category" else "7503560" if name == "product_subcategory" else "not_applicable"}], "provenance": {"source": "cache"}} for name in scalar}
    values["brand"]["value"][0]["language_tag"] = "en_AE"
    saved = guided.save_draft(family(parent_sku="RESUME-PARENT", vendor_context_id=context["family"]["vendor_context_id"], parent_required_values=values, children=[{"asin": "B000000011", "vendor_sku": "RESUME-1", "variation_value": "7 INCH", "variation_values": {"size": "7 INCH"}}, {"asin": "B000000012", "vendor_sku": "RESUME-2", "variation_value": "9 INCH", "variation_values": {"size": "9 INCH"}}]))
    job = guided.approve(saved["id"], saved["family_hash"])
    guided.transition(job["id"], "NEEDS_ATTENTION", {"message": "Local PTD validation failed"})
    with get_db_connection() as conn:
        for level, schema in (("parent", parent), ("child", child)):
            conn.execute("INSERT INTO pc_ptd_cache(marketplace_id,vendor_code,product_type,parentage_level,schema_json,fetched_at_utc) VALUES(?,?,?,?,?,?)", ("M", "VENDOR", "TYPE", level, __import__("json").dumps(schema), "now"))
        conn.commit()
    monkeypatch.setattr(worker, "get_definition", lambda *_: (_ for _ in ()).throw(AssertionError("Amazon definition transport must not run")))
    with pytest.raises(ValueError, match="hash does not match"):
        worker.resume_prewrite_local_validation(job["id"], "wrong")
    resumed = worker.resume_prewrite_local_validation(job["id"], job["family_hash"])
    assert resumed["state"] == "AMAZON_VALIDATING"
    assert all(operation["state"] == "PREPARED" and operation["attempt_count"] == 0 for operation in resumed["operations"])
    with get_db_connection() as conn:
        assert conn.execute("SELECT count(*) FROM pc_guided_job_runtime WHERE job_id=?", (job["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM pc_guided_validation_evidence WHERE job_id=?", (job["id"],)).fetchone()[0] == 0
