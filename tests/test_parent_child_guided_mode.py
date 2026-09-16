import threading
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
    job = approved_job(); attached = set(); calls = []
    schema = {"properties": {"parentage_level": {}, "variation_theme": {}, "child_parent_sku_relationship": {}, "size": {}}, "required": []}
    def definition(*_args): return schema, {"marketplaceIds": ["M"]}
    def listing(_auth, _market, _vendor, sku):
        if sku == "GUIDED-PARENT":
            if "parent" not in attached: return {"status": 404}
            return {"status": 200, "api_sku": "GUIDED-PARENT", "product_type": "TYPE", "parent_verified": True, "theme_verified": True, "issues": []}
        return {"status": 200, "api_sku": sku, "product_type": "TYPE", "current_parent": "GUIDED-PARENT" if sku in attached else None, "attached_to_parent": sku in attached, "issues": []}
    def parent_put(_auth, _family, _body, _key): attached.add("parent"); calls.append("parent"); return {"http_status": 202, "request_id": "safe"}
    def child_patch(_auth, _family, sku, _body, _key): attached.add(sku); calls.append(sku); return {"http_status": 202, "request_id": "safe"}
    api = {"get_definition": definition, "listing": listing, "catalog": lambda *_: ({"relationships": []}, {"http_status": 200, "request_id": "catalog-safe"}), "parent_put": parent_put, "child_patch": child_patch,
           "validate_parent": lambda *_: {"classification": "PASSED", "http_status": 200, "issues": []},
           "validate_child": lambda *_: {"classification": "PASSED", "http_status": 200, "issues": []}}
    actions = [worker.process_next_safe_action(job["id"], object(), api)["action"] for _ in range(12)]
    final = guided.get_job(job["id"])
    assert final["state"] == "COMPLETED", (actions, final["blocking_issue"], final["operations"])
    assert calls == ["parent", "CHILD-1", "CHILD-2"]


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
    return guided.transition(job["id"], "WAITING_FOR_PARENT")


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


def test_guarded_persisted_child_reconciliation_uses_auth_and_is_get_only():
    from services import parent_child_guided_worker as worker
    job = _persisted_verified_parent_children_prepared_job(); configured_auth = object(); calls = []
    def listing(auth, _marketplace, _vendor, sku):
        calls.append(("listing", auth, sku))
        relationship = [{"parent_sku": "GUIDED-PARENT"}] if sku == "CHILD-1" else []
        return {"status": 200, "request_id": "listing-" + sku, "api_sku": sku, "product_type": "TYPE", "attributes": {"child_parent_sku_relationship": relationship}, "issues": []}
    def catalog(auth, _marketplace, asin):
        calls.append(("catalog", auth, asin))
        return {"relationships": [{"relationships": [{"parentAsins": ["B0PARENT01"]}]}]}, {"http_status": 200, "request_id": "catalog-" + asin}
    result = worker.reconcile_prepared_children_get(job["id"], job["family_hash"], configured_auth, {"listing": listing, "catalog": catalog, "parent_put": lambda *_: pytest.fail("PUT must not run"), "child_patch": lambda *_: pytest.fail("PATCH must not run")})
    assert [item[0] for item in calls] == ["listing", "listing", "catalog"]
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
    job = approved_job(); auth = object(); present = set(); writes = []
    schema = {"properties": {"parentage_level": {}, "variation_theme": {}, "child_parent_sku_relationship": {}, "size": {}}, "required": []}
    def listing(actual_auth, _m, _v, sku):
        assert actual_auth is auth
        if sku == "GUIDED-PARENT":
            return {"status": 200 if "parent" in present else 404, "api_sku": sku if "parent" in present else None, "product_type": "TYPE", "parent_verified": "parent" in present, "theme_verified": "parent" in present, "issues": []}
        return {"status": 200, "api_sku": sku, "product_type": "TYPE", "current_parent": "GUIDED-PARENT" if sku in present else None, "attributes": {}, "issues": []}
    def parent_put(_a, _f, _b, _k): present.add("parent"); writes.append("parent"); return {"http_status": 200, "request_id": "put-parent", "issues": []}
    def child_patch(_a, _f, sku, _b, _k): present.add(sku); writes.append(sku); return {"http_status": 202, "request_id": "patch-" + sku, "issues": []}
    api = {"get_definition": lambda *_: (schema, {}), "listing": listing, "catalog": lambda *_: ({"relationships": []}, {"http_status": 200, "request_id": "catalog"}), "validate_parent": lambda *_: {"classification": "PASSED", "http_status": 200, "issues": []}, "validate_child": lambda *_: {"classification": "PASSED", "http_status": 200, "issues": []}, "parent_put": parent_put, "child_patch": child_patch}
    for _ in range(3): worker.process_next_safe_action(job["id"], auth, api)
    worker = importlib.reload(worker)  # persisted state, not in-memory recovery
    for _ in range(8): worker.process_next_safe_action(job["id"], auth, api)
    stored = guided.get_job(job["id"])
    assert stored["state"] == "COMPLETED" and writes == ["parent", "CHILD-1", "CHILD-2"]
    assert [item["attempt_count"] for item in stored["operations"]] == [1, 1, 1]
    value = draft()
    saved = guided.save_draft(family(parent_sku="UNKNOWN-PARENT", vendor_context_id=value["family"]["vendor_context_id"], children=[{"asin": "B000000021", "vendor_sku": "UNKNOWN-1", "variation_value": "7 INCH"}, {"asin": "B000000022", "vendor_sku": "UNKNOWN-2", "variation_value": "9 INCH"}]), value["id"])
    unknown = guided.approve(saved["id"], saved["family_hash"])
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
    monkeypatch.setattr(worker, "production_adapters", lambda: {"listing": lambda _a, _m, _v, sku: {"status": 200, "api_sku": sku, "product_type": "TYPE", "current_parent": "GUIDED-PARENT", "attributes": {}, "issues": []}, "catalog": lambda *_: ({"relationships": []}, {"http_status": 200})})
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


def test_phase_three_worker_lease_and_guided_ui_contract():
    from services import parent_child_guided_worker as worker
    from pathlib import Path
    job = approved_job()
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
