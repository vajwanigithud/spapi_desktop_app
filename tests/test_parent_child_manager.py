import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest

from services import parent_child_migrations as migrations
from services.parent_child_store import get_draft, save_draft
from services.parent_child_validation import WRITE_FEATURE_FLAGS, family_product_type, parse_asins, theme_options
from services.parent_child_resolver import resolve
from services.parent_child_resolver import resolve_many
from services.spapi_catalog_relationships import thumbnail
from services.spapi_listings import verify_vendor_access
from services.spapi_product_types import ProductTypeDefinitionError, get_definition
from services.parent_child_preview import build_preview

def test_asin_parsing_unicode_duplicates_and_order():
    result=parse_asins('b000000001, B000000001\tB000000002\nＢ000000003 nope')
    assert result['asins']==['B000000001','B000000002']
    assert result['duplicate_count']==1 and result['invalid_count']==2

def test_thumbnail_main_then_first_and_missing():
    assert thumbnail({'images':[{'images':[{'variant':'PT01','link':'first'},{'variant':'MAIN','link':'main'}]}]})=='main'
    assert thumbnail({'images':[{'images':[{'link':'first'}]}]})=='first'
    assert thumbnail({}) is None

def test_product_type_theme_filtering():
    assert theme_options({'properties':{'variation_theme':{'enum':['SizeName','ColorName']}}})==['SizeName','ColorName']

def test_product_type_detection_consistent_mixed_and_missing():
    assert family_product_type([{'asin':'A','status':'Ready','product_type':'FITNESS_EQUIPMENT'}])['status']=='consistent'
    assert family_product_type([{'asin':'A','status':'Ready','product_type':'A'},{'asin':'B','status':'Ready','product_type':'B'}])['status']=='mixed'
    assert family_product_type([{'asin':'A','status':'Ready','product_type':None}])['status']=='missing'

def test_deprecated_themes_are_excluded():
    schema={'properties':{'variation_theme':{'enum':['Color','Size'],'deprecatedEnumValues':['Size']}}}
    assert theme_options(schema)==['Color']

def test_write_flags_are_all_disabled():
    assert WRITE_FEATURE_FLAGS and not any(WRITE_FEATURE_FLAGS.values())

def test_resolver_ambiguity_and_current_parent():
    with patch('services.parent_child_resolver.listings_lookup',return_value=({'B000000001':['A','B']},{'status':'Vendor Listing Found'})),patch('services.parent_child_resolver.catalog_lookup',return_value=(None,{'status':'Catalog Item Not Found'})):
        assert resolve(object(),'m','v','B000000001')['status_detail']=='Ambiguous Vendor SKU'
    catalog={'summaries':[{'itemName':'<unsafe>'}],'productTypes':[{'productType':'TOY'}],'relationships':[{'relationships':[{'parentAsins':['B000000009']}]}]}
    with patch('services.parent_child_resolver.listings_lookup',return_value=({'B000000001':['A']},{'status':'Vendor Listing Found'})),patch('services.parent_child_resolver.catalog_lookup',return_value=(catalog,{'status':'Catalog Item Found'})),patch('services.parent_child_resolver.thumbnail',return_value=None):
        result=resolve(object(),'m','v','B000000001')
    assert result['current_parent']=='B000000009' and result['status_detail']=='Existing Different Parent'

def test_catalog_evidence_survives_zero_vendor_listings_and_failed_catalog_is_unknown_parent():
    catalog={'summaries':[{'itemName':'Title'}],'images':[],'productTypes':[{'productType':'BANNER'}],'relationships':[]}
    with patch('services.parent_child_resolver.listings_lookup',return_value=({}, {'status':'Vendor Listing Not Found','result_count':0})),patch('services.parent_child_resolver.catalog_lookup',return_value=(catalog, {'status':'Catalog Item Found'})):
        item=resolve_many(object(),'M','V',['B000000001'])[0]
    assert item['title']=='Title' and item['product_type']=='BANNER' and item['status_detail']=='Vendor Listing Not Found'
    with patch('services.parent_child_resolver.listings_lookup',return_value=({}, {'status':'Listings Access Denied','result_count':0})),patch('services.parent_child_resolver.catalog_lookup',return_value=(None, {'status':'Catalog Access Denied'})):
        assert resolve_many(object(),'M','V',['B000000001'])[0]['current_parent']=='Unknown'


def test_resolver_mocked_consistent_product_type_children_are_eligible():
    catalogs = [
        {'summaries': [{'itemName': 'One'}], 'productTypes': [{'productType': 'BANNER'}], 'relationships': []},
        {'summaries': [{'itemName': 'Two'}], 'productTypes': [{'productType': 'BANNER'}], 'relationships': []},
    ]
    with patch('services.parent_child_resolver.listings_lookup', return_value=({'B000000001': ['SKU-1'], 'B000000002': ['SKU-2']}, {'status': 'Vendor Listing Found'})), patch('services.parent_child_resolver.catalog_lookup', side_effect=[(catalogs[0], {'status': 'Catalog Item Found'}), (catalogs[1], {'status': 'Catalog Item Found'})]):
        items = resolve_many(object(), 'M', 'V', ['B000000001', 'B000000002'])
    assert [item['status'] for item in items] == ['Ready', 'Ready']
    assert {item['product_type'] for item in items} == {'BANNER'}


def test_resolver_mocked_mixed_product_types_remain_distinct():
    catalogs = [
        {'summaries': [{'itemName': 'Garland'}], 'productTypes': [{'productType': 'GARLAND'}], 'relationships': []},
        {'summaries': [{'itemName': 'Banner'}], 'productTypes': [{'productType': 'BANNER'}], 'relationships': []},
    ]
    with patch('services.parent_child_resolver.listings_lookup', return_value=({'B000000001': ['SKU-1'], 'B000000002': ['SKU-2']}, {'status': 'Vendor Listing Found'})), patch('services.parent_child_resolver.catalog_lookup', side_effect=[(catalogs[0], {'status': 'Catalog Item Found'}), (catalogs[1], {'status': 'Catalog Item Found'})]):
        items = resolve_many(object(), 'M', 'V', ['B000000001', 'B000000002'])
    assert [item['product_type'] for item in items] == ['GARLAND', 'BANNER']


def test_resolver_mocked_different_existing_parents_need_review():
    def catalog(parent):
        return {'summaries': [{'itemName': 'Banner'}], 'productTypes': [{'productType': 'BANNER'}], 'relationships': [{'relationships': [{'parentAsins': [parent]}]}]}
    with patch('services.parent_child_resolver.listings_lookup', return_value=({'B000000001': ['SKU-1'], 'B000000002': ['SKU-2']}, {'status': 'Vendor Listing Found'})), patch('services.parent_child_resolver.catalog_lookup', side_effect=[(catalog('B000000009'), {'status': 'Catalog Item Found'}), (catalog('B000000010'), {'status': 'Catalog Item Found'})]):
        items = resolve_many(object(), 'M', 'V', ['B000000001', 'B000000002'])
    assert [item['status'] for item in items] == ['Needs Review', 'Needs Review']
    assert {item['current_parent'] for item in items} == {'B000000009', 'B000000010'}


def test_resolver_mocked_missing_catalog_product_type_needs_review():
    catalog = {'summaries': [{'itemName': 'No product type'}], 'productTypes': [], 'relationships': []}
    with patch('services.parent_child_resolver.listings_lookup', return_value=({'B000000001': ['SKU-1']}, {'status': 'Vendor Listing Found'})), patch('services.parent_child_resolver.catalog_lookup', return_value=(catalog, {'status': 'Catalog Item Found'})):
        item = resolve_many(object(), 'M', 'V', ['B000000001'])[0]
    assert item['status'] == 'Needs Review' and item['status_detail'] == 'Catalog Product Type Unavailable'


def test_resolver_mocked_missing_image_does_not_block_an_eligible_product():
    catalog = {'summaries': [{'itemName': 'No image'}], 'images': [], 'productTypes': [{'productType': 'BANNER'}], 'relationships': []}
    with patch('services.parent_child_resolver.listings_lookup', return_value=({'B000000001': ['SKU-1']}, {'status': 'Vendor Listing Found'})), patch('services.parent_child_resolver.catalog_lookup', return_value=(catalog, {'status': 'Catalog Item Found'})):
        item = resolve_many(object(), 'M', 'V', ['B000000001'])[0]
    assert item['status'] == 'Ready'
    assert item['image_url'] is None


def test_resolver_mocked_catalog_failure_while_listings_succeeds_is_lookup_failed():
    with patch('services.parent_child_resolver.listings_lookup', return_value=({'B000000001': ['SKU-1']}, {'status': 'Vendor Listing Found'})), patch('services.parent_child_resolver.catalog_lookup', return_value=(None, {'status': 'Catalog Access Denied'})):
        item = resolve_many(object(), 'M', 'V', ['B000000001'])[0]
    assert item['status'] == 'Lookup Failed' and item['vendor_sku'] == 'SKU-1'

def test_migration_repeatability_and_draft_persistence(monkeypatch,tmp_path):
    db=tmp_path/'parent_child_test.db'
    from contextlib import contextmanager
    @contextmanager
    def conn():
        c=sqlite3.connect(db);c.row_factory=sqlite3.Row
        try: yield c
        finally:c.close()
    monkeypatch.setattr(migrations,'get_db_connection',conn)
    import services.parent_child_store as store
    monkeypatch.setattr(store,'get_db_connection',conn)
    migrations.ensure_parent_child_schema(); migrations.ensure_parent_child_schema()
    saved=save_draft({'marketplace_id':'M','vendor_code':'V','preview':{'validation':{'passed':True}},'items':[{'asin':'B000000001','status':'Ready','confirmed':True}]})
    restored = get_draft(saved['id'])
    assert restored['items'][0]['asin']=='B000000001' and restored['preview']['validation']['passed'] is True and 'confirmed' not in restored['items'][0]
    import services.parent_child_store as store
    store.upsert_vendor_context('M', 'V', True, 'Main Vendor', region='eu', capability_result='Listings Items read access verified')
    context = store.vendor_contexts('M')[0]
    assert context['active'] == 1 and context['verified_at_utc'] and context['region'] == 'eu'

def test_tab_order_and_escaping_contract_present():
    html=Path('ui/index.html').read_text(encoding='utf-8')
    assert '"parent-child"' in html and 'DEFAULT_TAB_ORDER' in html
    assert '.replace(/</g, "&lt;")' in html


def test_image_preview_modal_is_hidden_and_css_cannot_override_it():
    html = Path("ui/index.html").read_text(encoding="utf-8")
    css = Path("ui/css/parent_child_manager.css").read_text(encoding="utf-8")
    assert 'id="pc-image-modal" hidden aria-hidden="true"' in html
    assert ".pc-image-preview-modal[hidden]{display:none!important}" in css
    assert ".pc-modal" not in css


def test_image_preview_lifecycle_has_validation_and_all_close_paths():
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "function validPreviewUrl" in js
    assert "function closePreviewModal" in js
    assert "modal.hidden = true" in js
    assert 'modal.setAttribute("aria-hidden", "true")' in js
    assert "image.removeAttribute(\"src\")" in js
    assert 'closeButton.addEventListener("click", closePreviewModal)' in js
    assert "if (event.target === modal) closePreviewModal()" in js
    assert 'event.key === "Escape"' in js
    assert "closePreviewModal();\n    bindPreviewModal();" in js


def test_marketplace_labels_use_existing_spapi_configuration(monkeypatch):
    from routes import parent_child_routes as routes
    monkeypatch.setattr(routes.config, "MARKETPLACE_IDS", ["A2VIGQ35RCS4UG"])
    assert routes.marketplaces()["items"] == [{"marketplace_id": "A2VIGQ35RCS4UG", "label": "Amazon UAE — A2VIGQ35RCS4UG", "region": "eu"}]


def test_vendor_access_verification_is_read_only_success_and_failure(monkeypatch):
    class Auth:
        def get_lwa_access_token(self): return "token"
    class Response:
        def __init__(self, status, payload): self.status_code=status; self.ok=status < 400; self.payload=payload
        def json(self): return self.payload
    monkeypatch.setattr("services.spapi_listings.requests.get", lambda *args, **kwargs: Response(200, {"items": []}))
    assert verify_vendor_access(Auth(), "M", "V")["verified"] is True
    monkeypatch.setattr("services.spapi_listings.requests.get", lambda *args, **kwargs: Response(403, {"errors": [{"message": "denied"}]}))
    assert "permission" in verify_vendor_access(Auth(), "M", "V")["last_error"].lower()


def test_vendor_setup_empty_state_and_selection_contract_present():
    html = Path("ui/index.html").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "Complete one-time setup" in html and "Add and Verify Vendor Code" in html
    assert 'api("/marketplaces")' in js
    assert "pc_last_vendor_context" in js
    assert "Vendor access verified" in js

def test_product_type_is_child_detected_and_manual_override_is_advanced():
    html = Path("ui/index.html").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "Product type will be detected from child products." in html
    assert "Manual Product Type override" in html and "<details>" in html
    assert 'parentage_level: level' in js
    assert "themeSelects(themes)" in js
    assert "if (themes.length === 1) select.value = themes[0]" in js


def test_ptd_request_uses_parentage_level_and_resolves_schema_link(monkeypatch, tmp_path):
    import services.spapi_product_types as ptd
    from contextlib import contextmanager
    db = tmp_path / "parent_child_test.db"
    @contextmanager
    def conn():
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE IF NOT EXISTS pc_ptd_cache (marketplace_id TEXT, vendor_code TEXT, product_type TEXT, parentage_level TEXT, schema_json TEXT, fetched_at_utc TEXT, PRIMARY KEY(marketplace_id,vendor_code,product_type,parentage_level))")
        try: yield c
        finally: c.close()
    calls = []
    class Response:
        ok = True; status_code = 200
        def __init__(self, body): self.body = body
        def json(self): return self.body
    def fake_get(url, **kwargs):
        calls.append((url, kwargs)); return Response({"schema": {"link": {"resource": "https://schema.example/schema"}}} if "definitions/" in url else {"properties": {"variation_theme": {"items": {"properties": {"name": {"enum": ["COLOR"], "$lifecycle": {"enumDeprecated": []}}}}}}})
    monkeypatch.setattr(ptd, "get_db_connection", conn); monkeypatch.setattr(ptd.requests, "get", fake_get)
    schema, trace = get_definition(type("Auth", (), {"get_lwa_access_token": lambda self: "token"})(), "A2VIGQ35RCS4UG", "FK6EI", "TABLETOP_GAME", "parent")
    child_schema, child_trace = get_definition(type("Auth", (), {"get_lwa_access_token": lambda self: "token"})(), "A2VIGQ35RCS4UG", "FK6EI", "TABLETOP_GAME", "child")
    assert schema["properties"] and trace["definition_status"] == trace["schema_status"] == 200
    assert child_schema["properties"] and child_trace["parentageLevel"] == "CHILD"
    assert calls[0][1]["params"] == {"marketplaceIds": "A2VIGQ35RCS4UG", "requirements": "LISTING", "requirementsEnforced": "ENFORCED", "parentageLevel": "PARENT"}
    assert calls[2][1]["params"]["parentageLevel"] == "CHILD"
    assert trace["sellerId"] is None and trace["vendor_context"] == "FK6EI"


def test_ptd_child_request_failure_is_sanitized(monkeypatch, tmp_path):
    import services.spapi_product_types as ptd
    from contextlib import contextmanager
    @contextmanager
    def conn():
        c = sqlite3.connect(tmp_path / "parent_child_test.db"); c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE IF NOT EXISTS pc_ptd_cache (marketplace_id TEXT, vendor_code TEXT, product_type TEXT, parentage_level TEXT, schema_json TEXT, fetched_at_utc TEXT, PRIMARY KEY(marketplace_id,vendor_code,product_type,parentage_level))")
        try: yield c
        finally: c.close()
    class Response:
        ok = False; status_code = 403
        def json(self): return {"errors": [{"message": "Access denied for this definition"}]}
    monkeypatch.setattr(ptd, "get_db_connection", conn); monkeypatch.setattr(ptd.requests, "get", lambda *args, **kwargs: Response())
    try: get_definition(type("Auth", (), {"get_lwa_access_token": lambda self: "token"})(), "M", "V", "TYPE", "child", force=True)
    except ProductTypeDefinitionError as exc: assert exc.status_code == 403 and "Access denied" in exc.message
    else: assert False, "Expected sanitized PTD failure"


def test_variation_workflow_ui_contracts_cover_retry_attributes_without_confirmation_gate():
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    html = Path("ui/index.html").read_text(encoding="utf-8")
    assert 'id="pc-schema-status"' in html and 'id="pc-theme-step2"' in html
    assert "Promise.allSettled(requests)" in js and "retrySchema" in js
    assert "Amazon’s ${state.familyType} schema does not currently support" in js
    assert "if (themes.length === 1) select.value = themes[0]" in js
    assert "childAttributes(theme)" in js and "enumValues(field.node)" in js
    assert "item.variation_values" in js and "function workflowValidation" in js
    assert "workflowValidation" in js and "Family can proceed" in js


def test_step_two_polish_contract_has_summary_warning_groups_and_status_badges():
    html = Path("ui/index.html").read_text(encoding="utf-8")
    css = Path("ui/css/parent_child_manager.css").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert 'id="pc-detection-summary"' in html and 'id="pc-family-warning"' in html
    assert "Products cannot form one family" in js and "Keep this group only" in js
    assert "Product Types detected" in js and "Existing parents detected" in js
    assert all(name in css for name in ("pc-status-eligible", "pc-status-needs-review", "pc-status-blocked", "pc-status-lookup-failed"))
    assert "Technical lookup details" in js and "pc-thumbnail-button" in js


def test_review_button_remains_html_disabled_and_has_disabled_style_contract():
    html = Path("ui/index.html").read_text(encoding="utf-8")
    css = Path("ui/css/parent_child_manager.css").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert 'id="pc-review-button" onclick="PC.go(3)" disabled' in html
    assert '#pc-review-button:disabled' in css and 'cursor:not-allowed' in css
    assert '#pc-review-button:disabled:hover' in css and '#pc-review-button:disabled:active' in css
    assert '$("pc-review-button").disabled=!canReview' in js


def test_step_two_validation_has_one_source_for_blocked_partial_and_valid_states():
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "function workflowValidation()" in js
    assert "Parent Product Type Definition has not loaded." in js
    assert "Complete valid variation attributes for:" in js
    assert "Visually confirm:" not in js
    assert "validation.canProceed ? \"Yes\" : \"No\"" in js
    assert "const canReview=validation.canProceed" in js
    assert "if (step === 3 && !state.validation?.canProceed) { cards(); return; }" in js
    assert "warning.innerHTML = \"\"" in js


def test_optional_images_remain_but_confirmation_checkbox_and_draft_field_are_ignored():
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    store = Path("services/parent_child_store.py").read_text(encoding="utf-8")
    assert "pc-thumbnail-button" in js and "openPreviewModal" in js
    assert "return { init, go: reviewGo" in js and "confirm:" not in js[js.rfind("return { init, go: reviewGo"):]
    assert "x.pop('confirmed',None)" in store and "int(item.get('confirmed'" not in store


def test_read_only_preview_generates_parent_child_size_relationship_and_redacts_sensitive_data():
    relationship = {"title": "Child Parent SKU Relationship", "items": {"properties": {"parent_sku": {"title": "Parent SKU"}, "child_relationship_type": {"title": "Child Relationship Type", "enum": ["variation"]}}}}
    parent_schema = {"properties": {"parentage_level": {"title": "Parentage Level", "items": {"properties": {"value": {"enum": ["parent"]}}}}, "variation_theme": {"title": "Variation Theme", "items": {}}, "item_name": {"title": "Item Name"}}}
    child_schema = {"properties": {"parentage_level": {"title": "Parentage Level", "items": {"properties": {"value": {"enum": ["child"]}}}}, "variation_theme": {"title": "Variation Theme", "items": {}}, "child_parent_sku_relationship": relationship, "size": {"title": "Size"}}, "required": ["parentage_level", "variation_theme", "child_parent_sku_relationship", "size"]}
    data = {"parent_sku": "PARENT-1", "parent_title": "Plate family", "product_type": "DISHWARE_PLATE", "variation_theme": "SIZE", "items": [{"asin": "B0CC2JX4BL", "vendor_sku": "DI-113", "current_parent": None, "variation_values": {"size": "9 Inch"}}]}
    preview = build_preview(data, parent_schema, child_schema)
    patches = preview["children"][0]["body"]["patches"]
    assert preview["parent"]["operation"] == "putListingsItem" and preview["parent"]["body"]["productType"] == "DISHWARE_PLATE"
    assert next(patch for patch in patches if patch["path"] == "/attributes/size")["value"] == "9 Inch"
    assert next(patch for patch in patches if patch["path"] == "/attributes/child_parent_sku_relationship")["value"][0]["parent_sku"] == "PARENT-1"
    assert preview["validation"]["passed"] is True and "authorization" not in str(preview)


def test_preview_validation_errors_have_friendly_and_technical_paths():
    schema = {"properties": {"size": {"title": "Size"}}, "required": ["size"]}
    preview = build_preview({"parent_sku": "P", "product_type": "TYPE", "variation_theme": "SIZE", "items": [{"asin": "A", "vendor_sku": "S", "variation_values": {"size": ""}}]}, {}, schema)
    error = preview["validation"]["errors"]["A"][0]
    assert error == {"path": "/attributes/size", "message": "has no value", "field": "Size"}


def test_preview_ui_is_read_only_and_has_summary_json_plan_and_return_to_edit():
    html = Path("ui/index.html").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "Preview Amazon payload" in html and "Read-only: Amazon submission is disabled." in html
    assert "Technical JSON preview" in js and "Submission plan" in js and "Return to edit" in js
    assert "Dry run passed" in js and "Ready for controlled submission" in js
    assert 'api("/preview"' in js and "putListingsItem(" not in js and "patchListingsItem(" not in js and "createFeed" not in js


def test_parent_branch_uses_schema_derived_variation_declaration_and_child_patch_uses_replace_from_existing():
    parent = {"properties": {"child_parent_sku_relationship": {"title": "Child Parent SKU Relationship", "items": {"properties": {"child_relationship_type": {"title": "Child Relationship Type"}}}}}, "required": ["child_parent_sku_relationship"]}
    child = {"properties": {"size": {"title": "Size"}}}
    data = {"parent_sku": "P", "product_type": "TYPE", "variation_theme": "SIZE", "items": [{"asin": "A", "vendor_sku": "S", "variation_values": {"size": "9 Inch"}}]}
    preview = build_preview(data, parent, child, {"S": {"attributes": {"size": [{"value": "7 Inch"}], "item_name": [{"value": "Do not touch"}]}}})
    parent_relationship = preview["parent"]["body"]["attributes"]["child_parent_sku_relationship"]
    assert parent_relationship == [{"child_relationship_type": "variation"}] and "parent_sku" not in parent_relationship[0]
    size_patch = next(patch for patch in preview["children"][0]["body"]["patches"] if patch["path"] == "/attributes/size")
    assert size_patch["op"] == "replace" and "item_name" not in str(preview["children"][0]["body"]["patches"])


def test_child_attribute_reader_is_read_only_and_requests_attributes_summaries_issues():
    source = Path("services/spapi_listings.py").read_text(encoding="utf-8")
    assert "def listing_item_attributes" in source and "includedData':'attributes,summaries,issues" in source
    assert all(method not in source for method in ("requests.put", "requests.patch", "requests.delete"))


def test_semantic_child_patch_diff_ignores_size_case_and_whitespace_but_keeps_relationship():
    relationship = {"title": "Child Parent SKU Relationship", "items": {"properties": {"parent_sku": {"title": "Parent SKU"}, "child_relationship_type": {"title": "Child Relationship Type", "enum": ["variation"]}}}}
    schema = {"properties": {"size": {"title": "Size", "items": {"properties": {"value": {}}}}, "child_parent_sku_relationship": relationship}}
    data = {"parent_sku": "P", "product_type": "TYPE", "variation_theme": "SIZE", "items": [{"asin": "A", "vendor_sku": "S", "variation_values": {"size": " 9 Inch "}}]}
    preview = build_preview(data, {}, schema, {"S": {"status": 200, "attributes": {"size": [{"value": "9 INCH", "language_tag": "en_AE"}]}}})
    changes = {change["attribute"]: change for change in preview["children"][0]["changes"]}
    assert changes["size"]["status"] == "Already correct — no change"
    assert all(patch["path"] != "/attributes/size" for patch in preview["children"][0]["body"]["patches"])
    assert any(patch["path"] == "/attributes/child_parent_sku_relationship" for patch in preview["children"][0]["body"]["patches"])


def test_semantic_child_patch_diff_replaces_different_and_adds_missing_size():
    schema = {"properties": {"size": {"title": "Size", "items": {"properties": {"value": {}}}}}}
    data = {"parent_sku": "P", "product_type": "TYPE", "variation_theme": "SIZE", "items": [{"asin": "A", "vendor_sku": "S", "variation_values": {"size": "9 Inch"}}]}
    assert build_preview(data, {}, schema, {"S": {"status": 200, "attributes": {"size": [{"value": "7 Inch"}]}}})["children"][0]["changes"][0]["status"] == "Replace"
    assert build_preview(data, {}, schema, {"S": {"status": 200, "attributes": {}}})["children"][0]["changes"][0]["status"] == "Add"


def test_preview_polish_ui_contract_has_responsive_parent_form_and_collapsed_existing_data():
    css = Path("ui/css/parent_child_manager.css").read_text(encoding="utf-8")
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "pc-parent-required .pc-preview-card" in css and "grid-template-columns:repeat(2" in css
    assert "Technical existing attributes" in js and "pc-existing-table" in js
    assert "Available from all selected children" in js and "Use this value" in js
    assert "UNSAFE_SHARED" not in js and "cost_price" not in js


def test_missing_batteries_required_returns_structured_not_ready_response(monkeypatch):
    from routes import parent_child_routes as routes
    context={"id":1,"marketplace_id":"M","vendor_code":"V","verified":1,"active":1}
    parent={"properties":{"variation_theme":{"title":"Variation Theme","items":{"properties":{"name":{"enum":["SIZE"]}}}},"batteries_required":{"title":"Are Batteries Required"}},"required":["batteries_required"]}
    child={"properties":{"variation_theme":{"title":"Variation Theme","items":{"properties":{"name":{"enum":["SIZE"]}}}}}}
    monkeypatch.setattr(routes, "_checked_context", lambda *args: context)
    monkeypatch.setattr(routes.router, "_auth_client", object(), raising=False)
    monkeypatch.setattr(routes, "get_definition", lambda *args: (parent if args[-1] == "PARENT" else child, {"marketplaceIds":"M","product_type":"TYPE"}))
    request = routes.PreviewRequest(data={"marketplace_id": "M", "vendor_code": "V", "vendor_context_id":1, "parent_sku": "P", "product_type": "TYPE", "variation_theme":"SIZE", "items": []})
    response = routes.preview(request)
    assert response.status_code == 422
    assert b'"status":"not_ready"' in response.body and b'Are Batteries Required is required' in response.body


def test_parent_field_serialization_handles_enum_boolean_category_and_subcategory():
    schema = {"properties": {"product_category": {"title": "Product Category", "items": {"properties": {"value": {}}}}, "product_subcategory": {"title": "Product Subcategory", "items": {"properties": {"value": {}}}}, "batteries_required": {"title": "Are Batteries Required", "items": {"properties": {"value": {"enum": [True, False]}}}}}}
    preview = build_preview({"parent_sku": "P", "product_type": "TYPE", "parent_values": {"product_category": "7503500", "product_subcategory": "7503560", "batteries_required": False}, "items": []}, schema, {})
    attrs = preview["parent"]["body"]["attributes"]
    assert attrs["product_category"] == [{"value": "7503500"}] and attrs["product_subcategory"] == [{"value": "7503560"}] and attrs["batteries_required"] == [{"value": False}]


def test_preview_api_client_contract_handles_json_and_non_json_failures_safely():
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert 'response.headers.get("content-type")' in js and "await response.text()" in js
    assert "error.payload?.status === \"not_ready\"" in js
    assert "Preview service returned an unexpected server response. Check application logs and retry." in js


def test_preview_unexpected_exception_returns_sanitized_json(monkeypatch):
    from routes import parent_child_routes as routes
    context={"id":1,"marketplace_id":"M","vendor_code":"V","verified":1,"active":1}
    schema={"properties":{"variation_theme":{"title":"Variation Theme","items":{"properties":{"name":{"enum":["SIZE"]}}}}}}
    monkeypatch.setattr(routes, "_checked_context", lambda *args: context)
    monkeypatch.setattr(routes.router, "_auth_client", object(), raising=False)
    monkeypatch.setattr(routes, "get_definition", lambda *args: (schema, {"marketplaceIds":"M","product_type":"TYPE"}))
    monkeypatch.setattr(routes, "build_preview", lambda *args: (_ for _ in ()).throw(RuntimeError("token=do-not-show")))
    response = routes.preview(routes.PreviewRequest(data={"marketplace_id": "M", "vendor_code": "V", "vendor_context_id":1, "product_type":"TYPE", "variation_theme":"SIZE", "items": []}))
    assert response.status_code == 500 and b"token" not in response.body and b"unexpected error" in response.body


def test_step_one_title_maps_to_localized_parent_item_name_and_is_authoritative():
    schema = {"$defs": {"language_tag": {"default": "en_AE"}}, "properties": {"item_name": {"title": "Title", "items": {"required": ["value", "language_tag"], "properties": {"value": {}, "language_tag": {}}}}}, "required": ["item_name"]}
    preview = build_preview({"marketplace_id": "A2VIGQ35RCS4UG", "parent_sku": "P", "parent_title": "Goldedge Plates", "product_type": "TYPE", "items": []}, schema, {})
    assert preview["parent"]["body"]["attributes"]["item_name"] == [{"value": "Goldedge Plates", "language_tag": "en_AE"}]


def test_batteries_false_is_not_missing_but_unselected_is_actionable():
    schema = {"properties": {"batteries_required": {"title": "Are Batteries Required", "items": {"properties": {"value": {"enum": [True, False]}}}}}, "required": ["batteries_required"]}
    missing = build_preview({"parent_sku": "P", "product_type": "TYPE", "items": []}, schema, {})
    false_value = build_preview({"parent_sku": "P", "product_type": "TYPE", "parent_values": {"batteries_required": False}, "items": []}, schema, {})
    assert missing["validation"]["errors"]["Parent"][0]["field"] == "Are Batteries Required"
    assert false_value["validation"]["passed"] is True and false_value["parent"]["body"]["attributes"]["batteries_required"] == [{"value": False}]


def test_parent_title_ui_uses_one_authoritative_step_one_value_and_shows_required_errors():
    js = Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert 'name === "item_name" ? $("pc-parent-title").value' in js
    assert 'data-pc-parent-title' in js and 'PC.parentTitle(this.value)' in js
    assert 'parentRequiredFormV3(); output.innerHTML' in js


def test_tracker_migration_state_machine_idempotency_and_restart_safe_polling(monkeypatch, tmp_path):
    import services.parent_child_tracker as tracker
    from contextlib import contextmanager
    db = tmp_path / "parent_child_test.db"
    @contextmanager
    def conn():
        c=sqlite3.connect(db);c.row_factory=sqlite3.Row
        try: yield c
        finally: c.close()
    monkeypatch.setattr(migrations, "get_db_connection", conn); migrations.ensure_parent_child_schema()
    monkeypatch.setattr(tracker, "get_db_connection", conn)
    with conn() as c:
        c.execute("INSERT INTO parent_child_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("j","d","M",1,"P","T","SIZE","PREPARED","{}","hash",None,None,None,None,"now","now")); c.commit()
    assert tracker.transition("j", "PARENT_SUBMITTING", "e1") is True
    assert tracker.transition("j", "PARENT_SUBMITTING", "e1") is False
    try: tracker.transition("j", "COMPLETED", "bad")
    except ValueError: pass
    else: assert False, "invalid transition must fail"
    try: tracker.record_poll("j", 1, 200, "request-id")
    except ValueError: pass
    else: assert False, "unsubmitted jobs must not poll"


def test_tracker_ui_is_empty_by_default_and_has_no_submission_control():
    html=Path("ui/index.html").read_text(encoding="utf-8"); js=Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "No Parent–Child submissions have been made yet." in js
    assert "monitoring can continue up to 72 hours" in html
    assert "Refresh status" in html and "submission endpoint" not in js


def test_preview_suggestions_show_value_and_stale_after_payload_edits():
    js=Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    service=Path("services/parent_child_preview.py").read_text(encoding="utf-8")
    assert "JSON.stringify(s.value)" in js and "Use this value" in js
    assert "state.preview = null" in js and "UNSAFE_SHARED" in service
    assert all(term in service for term in ("battery", "hazmat", "cost", "manufacturer"))


def test_authoritative_schema_validation_checks_nested_conditionals_and_patch_paths():
    from services.parent_child_safety import full_schema_errors, patch_errors
    schema={"type":"object","properties":{"mode":{"const":"child"},"size":{"type":"array","items":{"type":"object","required":["value"],"properties":{"value":{"type":"string","minLength":2}}}}},"required":["mode"],"if":{"properties":{"mode":{"const":"child"}}},"then":{"required":["size"]}}
    assert full_schema_errors(schema,{"mode":"child"},"Parent")
    errors,_=patch_errors(schema,[{"op":"replace","path":"/attributes/size","value":[{"value":"x"}]}],{"mode":"child"},"A")
    assert any(error["schema_path"] for error in errors) and any("too short" in error["message"] for error in errors)


def test_safety_snapshot_hash_versioning_stale_and_deterministic_idempotency(monkeypatch,tmp_path):
    import services.parent_child_safety as safety
    from contextlib import contextmanager
    db=tmp_path/"parent_child_test.db"
    @contextmanager
    def conn():
        c=sqlite3.connect(db);c.row_factory=sqlite3.Row
        try: yield c
        finally:c.close()
    monkeypatch.setattr(migrations,"get_db_connection",conn); migrations.ensure_parent_child_schema()
    monkeypatch.setattr(safety,"get_db_connection",conn); monkeypatch.setattr(safety,"ensure_parent_child_schema",lambda:None)
    payload={"parent":{"sku":"P","body":{}},"children":[{"asin":"A","sku":"S","body":{}}]}; context={"id":1,"marketplace_id":"M","vendor_code":"V"}; ptd={"schema_checksum":"one"}
    first=safety.create_snapshot("d",context,"TYPE","SIZE",payload,ptd,ptd)
    assert len(first["payload_hash"])==64 and safety.idempotency_key("M",1,"CHILD","S",first["payload_hash"]) == safety.idempotency_key("M",1,"CHILD","S",first["payload_hash"])
    assert safety.mark_draft_snapshots_stale("d")==1 and safety.get_snapshot(first["id"])["status"]=="STALE"
    second=safety.create_snapshot("d",context,"TYPE","SIZE",payload,ptd,ptd)
    assert second["version"]==2


def test_browser_ptds_are_not_sent_to_authoritative_preview_and_safety_ui_is_visible():
    js=Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    route=Path("routes/parent_child_routes.py").read_text(encoding="utf-8")
    assert 'JSON.stringify({ data: payload() })' in js
    assert 'class PreviewRequest(BaseModel): data:Dict[str,Any]' in route
    assert "get_definition(auth,data['marketplace_id']" in route
    assert "Final safety checklist" in js and "Structurally ready" in js and "Run final preflight" in js


def test_preflight_expiry_and_active_parent_child_reservations_are_enforced(monkeypatch,tmp_path):
    import services.parent_child_safety as safety
    from contextlib import contextmanager
    db=tmp_path/"parent_child_test.db"
    @contextmanager
    def conn():
        c=sqlite3.connect(db);c.row_factory=sqlite3.Row
        try: yield c
        finally:c.close()
    monkeypatch.setattr(migrations,"get_db_connection",conn); migrations.ensure_parent_child_schema()
    monkeypatch.setattr(safety,"get_db_connection",conn); monkeypatch.setattr(safety,"ensure_parent_child_schema",lambda:None)
    context={"id":1,"marketplace_id":"M","vendor_code":"V"}; ptd={"schema_checksum":"one"}
    payload={"parent":{"sku":"P","body":{}},"children":[{"asin":"A","sku":"S","body":{}}]}
    one=safety.create_snapshot("d",context,"TYPE","SIZE",payload,ptd,ptd)
    preflight=safety.create_preflight(one["id"],"passed",{"authorization":"hidden"})
    with conn() as c: c.execute("UPDATE pc_snapshot_preflights SET expires_at_utc='2000-01-01T00:00:00+00:00'");c.commit()
    assert safety.latest_preflight(one["id"])["expired"] is True
    assert safety.reserve_future_submission(one["id"])["reserved"] is True
    safety.mark_draft_snapshots_stale("d")
    two=safety.create_snapshot("d",context,"TYPE","SIZE",{**payload,"parent":{"sku":"P","body":{"v":2}}},ptd,ptd)
    try: safety.reserve_future_submission(two["id"])
    except ValueError as exc: assert "conflict" in str(exc).lower()
    else: assert False, "active parent/child reservations must conflict"


def test_validation_preview_transport_locks_mode_uses_eu_and_classifies_issues(monkeypatch):
    import services.spapi_validation_preview as preview
    calls=[]
    class Auth:
        def get_lwa_access_token(self): return "secret-token"
    class Response:
        status_code=200; headers={"x-amzn-RequestId":"r","x-amzn-RateLimit-Limit":"5"}
        def json(self): return {"status":"ACCEPTED","issues":[{"severity":"WARNING","code":"W","message":"review"}]}
    def fake_put(*args,**kwargs): calls.append((args,kwargs)); return Response()
    monkeypatch.setattr(preview.requests,"put",fake_put)
    result=preview.validation_preview_put_listing(Auth(),"FK6EI","P","A2VIGQ35RCS4UG",{"productType":"TYPE"})
    assert result["classification"]=="PASSED_WITH_WARNINGS"
    assert calls[0][0][0].startswith("https://sellingpartnerapi-eu.amazon.com/")
    assert calls[0][1]["params"]["mode"]=="VALIDATION_PREVIEW"
    try: preview._params("NOT-UAE")
    except ValueError: pass
    else: assert False, "marketplace must be server-bound"
    assert all(value is False for value in WRITE_FEATURE_FLAGS.values())


def test_validation_preview_403_is_authorization_denied_and_error_200_is_blocked():
    from services.spapi_validation_preview import _classify
    assert _classify(403,{})[0]=="AUTHORIZATION_DENIED"
    assert _classify(200,{"status":"ACCEPTED","issues":[{"severity":"ERROR","code":"E"}]})[0]=="BLOCKED_BY_AMAZON_ISSUES"


def test_parent_selectors_are_present_and_activate_only_the_parent_conditional_branch():
    from services.parent_child_safety import full_schema_errors
    selector_parent = [{"value": "parent"}]
    selector_child = [{"value": "child"}]
    theme = [{"name": "SIZE"}]
    schema = {
        "type": "object",
        "properties": {
            "parentage_level": {"type": "array"},
            "variation_theme": {"type": "array"},
            "item_name": {"type": "array"},
            "child_parent_sku_relationship": {"type": "array"},
        },
        "allOf": [
            {"if": {"required": ["parentage_level", "variation_theme"], "properties": {"parentage_level": {"const": selector_parent}, "variation_theme": {"const": theme}}}, "then": {"required": ["item_name"]}},
            {"if": {"required": ["parentage_level"], "properties": {"parentage_level": {"const": selector_child}}}, "then": {"required": ["child_parent_sku_relationship"]}},
        ],
    }
    preview = build_preview({"parent_sku": "P", "parent_title": "Parent", "product_type": "TYPE", "variation_theme": "SIZE"}, schema, {})
    attributes = preview["parent"]["body"]["attributes"]
    assert attributes["parentage_level"] == selector_parent and attributes["variation_theme"] == theme
    errors = full_schema_errors(schema, attributes, "Parent")
    assert not any(error["path"] == "/child_parent_sku_relationship" for error in errors)


def test_parent_conditional_error_reports_exact_branch_path():
    from services.parent_child_safety import full_schema_errors
    schema = {"type": "object", "properties": {"kind": {"const": "parent"}}, "if": {"properties": {"kind": {"const": "parent"}}, "required": ["kind"]}, "then": {"required": ["item_name"]}}
    error = full_schema_errors(schema, {"kind": "parent"}, "Parent")[0]
    assert error["schema_path"] == "/then/required" and error["condition_path"] == "/then"


def test_child_patch_never_root_validates_existing_legacy_attributes_or_full_requirements():
    from services.parent_child_safety import patch_errors
    schema = {"type": "object", "additionalProperties": False, "properties": {"size": {"type": "array", "items": {"type": "object", "required": ["value"], "properties": {"value": {"type": "string", "minLength": 2}}}}}, "required": ["manufacturer"]}
    patches = [{"op": "replace", "path": "/attributes/size", "value": [{"value": "9 Inch"}]}]
    legacy = {"description": [{"value": "legacy"}], "occasion_type": [{"value": "party"}], "manufacturer": []}
    errors, candidate = patch_errors(schema, patches, legacy, "CHILD-1")
    assert not errors and candidate == {"size": [{"value": "9 Inch"}]}


def test_child_patch_property_validation_uses_ptd_definitions_and_blocks_invalid_values():
    from services.parent_child_safety import patch_errors
    schema = {"$defs": {"size_value": {"type": "string", "minLength": 2}}, "properties": {"size": {"type": "array", "items": {"type": "object", "required": ["value"], "properties": {"value": {"$ref": "#/$defs/size_value"}}}}, "variation_theme": {"type": "array", "items": {"type": "object", "required": ["name"], "properties": {"name": {"const": "SIZE"}}}}, "child_parent_sku_relationship": {"type": "array", "items": {"type": "object", "required": ["parent_sku"], "properties": {"parent_sku": {"type": "string", "minLength": 1}}}}}}
    invalid = [
        {"op": "replace", "path": "/attributes/size", "value": [{"value": "x"}]},
        {"op": "replace", "path": "/attributes/variation_theme", "value": [{"name": "COLOR"}]},
        {"op": "replace", "path": "/attributes/child_parent_sku_relationship", "value": [{}]},
    ]
    errors, _ = patch_errors(schema, invalid, {"manufacturer": [{"value": "legacy"}]}, "CHILD-1")
    assert len(errors) == 3 and all(error["layer"] == "Child individual PTD property validation" for error in errors)
    assert all(error["path"].startswith("/attributes/") for error in errors)


def test_parent_put_is_still_fully_validated_and_amazon_preview_remains_disabled(monkeypatch):
    from services.parent_child_safety import full_schema_errors
    from services import parent_child_validation as validation
    from routes import parent_child_routes as routes
    schema = {"type": "object", "properties": {"item_name": {"type": "array"}}, "required": ["item_name"]}
    assert full_schema_errors(schema, {}, "Parent", layer="Parent full PUT validation")
    assert validation.AMAZON_VALIDATION_PREVIEW is False and len(validation.WRITE_FEATURE_FLAGS) == 6 and not any(validation.WRITE_FEATURE_FLAGS.values())
    monkeypatch.setattr(routes, "get_snapshot", lambda _: (_ for _ in ()).throw(AssertionError("transport prerequisites must not run")))
    try:
        routes.amazon_validation_preview(routes.ValidationPreviewRequest(snapshot_id="snapshot", confirmation="VALIDATE"))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        assert False, "Amazon validation-preview transport must remain disabled"


def test_parent_submission_expired_snapshot_and_flags_fail_closed_without_transport():
    from services.parent_child_parent_submission import _unexpired
    snapshot={"id":"x","marketplace_id":"A2VIGQ35RCS4UG","vendor_code":"FK6EI","vendor_context_id":1,"status":"FRESH","expires_at_utc":"2000-01-01T00:00:00+00:00","payload":{},"payload_hash":"x","parent_ptd":{"schema":{},"schema_checksum":"x"},"child_ptd":{"schema":{},"schema_checksum":"x"}}
    # The expiry check is evaluated before any job/transport work; flags remain fail-closed.
    assert _unexpired(snapshot) is False
    assert WRITE_FEATURE_FLAGS["create_parent"] is False and not any(WRITE_FEATURE_FLAGS.values())


def test_parent_submission_http_200_malformed_or_missing_status_is_unknown(monkeypatch):
    import services.parent_child_parent_submission as submission
    class Auth:
        def get_lwa_access_token(self): return "test-token"
    class Response:
        status_code=200; headers={"content-type":"application/json","x-amzn-RequestId":"r"}
        def json(self): return {}
    monkeypatch.setattr(submission.requests,"put",lambda *a,**k:Response())
    snapshot={"parent_sku":"P","payload":{"parent":{"body":{}}}}
    status,evidence=submission._put(Auth(),snapshot,"key")
    assert status == "SUBMISSION_UNKNOWN" and evidence["request_id"] == "r"


def test_parent_submission_ui_panel_is_rendered_by_preview_handler():
    js=Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "renderParentSubmissionPanel" in js and "parentSubmissionPanel()" in js and "Create parent on Amazon" in js


def test_parent_submission_opens_only_exact_validated_job_detail_without_retry():
    js=Path("ui/js/parent_child_manager.js").read_text(encoding="utf-8")
    assert "function validJobId" in js and "async function openJobDetail(jobId)" in js
    assert 'api(`/jobs/${encodeURIComponent(jobId)}`)' in js
    assert "if (!validJobId(r.job_id))" in js and "do not retry" in js
    assert "await openJobDetail(r.job_id)" in js


def test_validation_evidence_requires_exact_single_successful_current_hash_set():
    import json
    from datetime import datetime, timedelta, timezone
    from services.parent_child_parent_submission import _validation_ok
    from services.parent_child_safety import checksum
    payload={'children':[{'sku':'DI-113'},{'sku':'DI-114'}]}
    snapshot={'id':'evidence-snapshot','parent_sku':'Princess_Plate','payload':payload}
    expiry=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE pc_validation_preview_evidence (snapshot_id,payload_hash,operation,sku,classification,expires_at_utc,evidence_json)')
        def add(operation, sku, classification='PASSED', issues=[]):
            conn.execute('INSERT INTO pc_validation_preview_evidence VALUES(?,?,?,?,?,?,?)',(snapshot['id'],checksum(payload),operation,sku,classification,expiry,json.dumps({'issues':issues})))
        add('PUT','Princess_Plate'); add('PATCH','DI-113'); add('PATCH','DI-114')
        assert _validation_ok(conn,snapshot)
        add('PATCH','DI-114','FAILED')
        assert not _validation_ok(conn,snapshot)


def test_validation_evidence_rejects_error_duplicate_extra_and_unrelated_rows():
    import json
    from datetime import datetime, timedelta, timezone
    from services.parent_child_parent_submission import _validation_ok
    from services.parent_child_safety import checksum
    payload={'children':[{'sku':'DI-113'},{'sku':'DI-114'}]}; snapshot={'id':'s','parent_sku':'Princess_Plate','payload':payload}
    expiry=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()
    def result(extra):
        with sqlite3.connect(':memory:') as conn:
            conn.row_factory=sqlite3.Row; conn.execute('CREATE TABLE pc_validation_preview_evidence (snapshot_id,payload_hash,operation,sku,classification,expires_at_utc,evidence_json)')
            for op,sku in [('PUT','Princess_Plate'),('PATCH','DI-113'),('PATCH','DI-114')]: conn.execute('INSERT INTO pc_validation_preview_evidence VALUES(?,?,?,?,?,?,?)',('s',checksum(payload),op,sku,'PASSED',expiry,'{"issues":[]}'))
            if extra: conn.execute('INSERT INTO pc_validation_preview_evidence VALUES(?,?,?,?,?,?,?)',extra)
            return _validation_ok(conn,snapshot)
    assert not result(('s',checksum(payload),'PATCH','DI-114','PASSED',expiry,json.dumps({'issues':[{'severity':'ERROR'}]})))
    assert not result(('s',checksum(payload),'PATCH','OTHER','PASSED',expiry,'{"issues":[]}'))
    assert result(('other','other-hash','PATCH','OTHER','FAILED',expiry,'{}'))


def test_validation_evidence_rejects_hashless_wrong_hash_and_expired_rows_without_mutation():
    import json
    from datetime import datetime, timedelta, timezone
    from services.parent_child_parent_submission import _validation_ok
    from services.parent_child_safety import checksum
    payload={'children':[{'sku':'DI-113'},{'sku':'DI-114'}]}; snapshot={'id':'s','parent_sku':'Princess_Plate','payload':payload}
    future=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(); expired=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    def result(extra):
        with sqlite3.connect(':memory:') as conn:
            conn.row_factory=sqlite3.Row; conn.execute('CREATE TABLE pc_validation_preview_evidence (snapshot_id,payload_hash,operation,sku,classification,expires_at_utc,evidence_json)')
            for op,sku in [('PUT','Princess_Plate'),('PATCH','DI-113'),('PATCH','DI-114')]: conn.execute('INSERT INTO pc_validation_preview_evidence VALUES(?,?,?,?,?,?,?)',('s',checksum(payload),op,sku,'PASSED',future,'{"issues":[]}'))
            conn.execute('INSERT INTO pc_validation_preview_evidence VALUES(?,?,?,?,?,?,?)',extra)
            before=list(conn.execute('SELECT * FROM pc_validation_preview_evidence'))
            assert not _validation_ok(conn,snapshot)
            assert [tuple(row) for row in conn.execute('SELECT * FROM pc_validation_preview_evidence')] == [tuple(row) for row in before]
    result(('s',None,'PATCH','DI-114','PASSED',future,'{"issues":[]}'))
    result(('s','f'*64,'PATCH','DI-114','PASSED',future,'{"issues":[]}'))
    result(('s',checksum(payload),'PATCH','DI-114','PASSED',expired,'{"issues":[]}'))


def test_failed_validation_gate_cannot_create_job_or_reach_transport(monkeypatch):
    import services.parent_child_parent_submission as submission
    monkeypatch.setattr(submission, 'gates', lambda *args, **kwargs: {'ready':False})
    monkeypatch.setattr(submission, '_create_ready_job', lambda *args: (_ for _ in ()).throw(AssertionError('job creation reached')))
    monkeypatch.setattr(submission, '_put', lambda *args: (_ for _ in ()).throw(AssertionError('transport reached')))
    try:
        submission.execute(object(), {}, {}, 'CREATE PARENT', lambda *args: None)
    except ValueError as exc:
        assert 'safety gates failed' in str(exc)
    else:
        assert False, 'a failed evidence gate must stop before job creation or transport'


def test_readonly_connector_and_get_paths_have_no_wal_or_write_sql(monkeypatch):
    import contextlib
    import services.parent_child_runtime_settings as settings
    statements=[]
    @contextlib.contextmanager
    def readonly():
        conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE pc_runtime_settings (name TEXT, value TEXT)')
        conn.set_trace_callback(statements.append)
        try: yield conn
        finally: conn.close()
    monkeypatch.setattr(settings,'get_readonly_db_connection',readonly)
    assert settings.enabled() is False
    assert statements == ["SELECT value FROM pc_runtime_settings WHERE name='amazon_validation_preview_enabled'"]


def test_runtime_mode_get_fails_closed_for_absent_and_malformed_settings(monkeypatch):
    import contextlib
    import services.parent_child_runtime_settings as settings
    @contextlib.contextmanager
    def absent():
        conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row
        try: yield conn
        finally: conn.close()
    monkeypatch.setattr(settings,'get_readonly_db_connection',absent)
    assert settings.enabled() is False
    @contextlib.contextmanager
    def malformed():
        conn=sqlite3.connect(':memory:'); conn.row_factory=sqlite3.Row
        conn.execute('CREATE TABLE pc_runtime_settings (name TEXT, value TEXT)')
        conn.execute("INSERT INTO pc_runtime_settings VALUES ('amazon_validation_preview_enabled','malformed')")
        try: yield conn
        finally: conn.close()
    monkeypatch.setattr(settings,'get_readonly_db_connection',malformed)
    assert settings.enabled() is False


def test_readiness_old_schema_fails_closed_without_migration(monkeypatch):
    from routes import parent_child_routes as routes
    snapshot={'id':'s','marketplace_id':'A2VIGQ35RCS4UG','vendor_code':'FK6EI','vendor_context_id':1,'payload_hash':'h'}
    monkeypatch.setattr(routes,'get_snapshot_readonly',lambda _: snapshot)
    monkeypatch.setattr(routes,'verified_vendor_context_readonly',lambda *args: {'id':1,'vendor_code':'FK6EI'})
    monkeypatch.setattr(routes.parent_submission,'gates',lambda *args,**kwargs: (_ for _ in ()).throw(sqlite3.OperationalError('missing migration')))
    result=routes.parent_submission_readiness('s')
    assert result['gates'] == {'ready':False,'reason':'Application restart/migration required'}
    assert 'ensure_parent_child_schema' not in __import__('inspect').getsource(routes.parent_submission_readiness)


def test_ui_parent_readiness_has_fail_closed_metadata_contracts():
    js=Path('ui/js/parent_child_manager.js').read_text(encoding='utf-8')
    for required in ('validTime(x.completed_at_utc)','future(x.expires_at_utc)','r.final_preflight_status === "passed"','validTime(r.final_preflight_at_utc)','future(r.snapshot_expires_at_utc)','typeof r.payload_hash === "string"','b.disabled=!safe','"Invalid"','"Not available"','"Expired — run again."'):
        assert required in js


def test_inactive_variation_values_are_rejected_before_patch_or_snapshot_work():
    from services.parent_child_preview import build_preview
    schema={"$defs":{"language_tag":{"default":"en_AE"}},"properties":{"size":{"items":{"properties":{"value":{}},"required":["value"]}},"collection_item":{"items":{"properties":{"value":{},"language_tag":{}},"required":["value","language_tag"]}},"parentage_level":{},"variation_theme":{},"child_parent_sku_relationship":{}},"allOf":[{"if":{"properties":{"variation_theme":{"contains":{"properties":{"name":{"enum":["SIZE"]}}}}}},"then":{"required":["size"]}},{"if":{"properties":{"variation_theme":{"contains":{"properties":{"name":{"enum":["COLLECTION_ITEM","SIZE/COLLECTION_ITEM"]}}}}}},"then":{"required":["collection_item"]}}]}
    data={"parent_sku":"P","product_type":"TYPE","variation_theme":"SIZE","items":[{"asin":"A","vendor_sku":"S","variation_values":{"size":"9 Inch","collection_item":"Dinner Plate"}}]}
    preview=build_preview(data,{},schema)
    child=preview['children'][0]
    assert child['inactive_variation_keys']==['collection_item']
    assert '/attributes/collection_item' not in [patch['path'] for patch in child['body']['patches']]
    assert preview['validation']['passed'] is False


def test_active_collection_item_uses_ptd_localized_wrapper_and_unchanged_value_is_not_patched():
    from services.parent_child_preview import build_preview
    schema={"$defs":{"language_tag":{"default":"en_AE"}},"properties":{"collection_item":{"items":{"properties":{"value":{},"language_tag":{}},"required":["value","language_tag"]}}},"allOf":[{"if":{"properties":{"variation_theme":{"contains":{"properties":{"name":{"enum":["COLLECTION_ITEM"]}}}}}},"then":{"required":["collection_item"]}}]}
    data={"parent_sku":"P","product_type":"TYPE","variation_theme":"COLLECTION_ITEM","items":[{"asin":"A","vendor_sku":"S","variation_values":{"collection_item":"Dinner Plate"}}]}
    preview=build_preview(data,{},schema)
    value=preview['children'][0]['body']['patches'][0]['value'][0]
    assert value == {'value':'Dinner Plate','language_tag':'en_AE'}
    unchanged=build_preview(data,{},schema,{'S':{'status':200,'attributes':{'collection_item':[value]}}})
    assert unchanged['children'][0]['body']['patches']==[]


def test_client_variation_payload_prunes_inactive_values_contract():
    js=Path('ui/js/parent_child_manager.js').read_text(encoding='utf-8')
    assert 'function pruneVariationValues(theme)' in js
    assert 'filter(([name])=>allowed.has(name))' in js
    assert 'pruneVariationValues(draft.variation_theme)' in js
    assert 'if (!childAttributes($("pc-theme").value).some(field=>field.name===name)) return;' in js


def test_validation_preview_click_uses_effective_runtime_mode_and_single_request_contract():
    js=Path('ui/js/parent_child_manager.js').read_text(encoding='utf-8')
    assert 'state.validationPreviewEnabled=mode.enabled === true' in js
    assert 'function validationPreviewReady()' in js
    assert 'state.validationPreflightPassed' in js and 'state.validationPreviewRunning' in js
    assert 'await api("/validation-mode")' in js
    assert 'if (answer !== "VALIDATE")' in js
    assert 'state.validationPreviewRunning=true' in js and 'state.validationPreviewRunning=false' in js
    assert 'api("/validation-preview",{method:"POST",body:JSON.stringify({snapshot_id:state.snapshot.id,confirmation:"VALIDATE"})})' in js
    assert 'if (state.validationPreviewRunning) return;' in js
    assert 'id="pc-amazon-validation"' in js


def test_validation_preview_route_keeps_runtime_mode_authoritative_and_browser_payload_narrow():
    route=Path('routes/parent_child_routes.py').read_text(encoding='utf-8')
    start=route.index('def amazon_validation_preview')
    body=route[start:route.index("@router.get('/parent-submission/readiness')",start)]
    for required in ("runtime_settings.enabled()", "payload.confirmation != 'VALIDATE'", "snapshot=get_snapshot(payload.snapshot_id)", "A2VIGQ35RCS4UG", "FK6EI", "latest_preflight(snapshot['id'])"):
        assert required in body
    assert 'payload.vendor' not in body and 'payload.marketplace' not in body


def _arm_snapshot():
    return {'id':'arm-snapshot','draft_id':'draft','marketplace_id':'A2VIGQ35RCS4UG','vendor_context_id':2,'vendor_code':'FK6EI','parent_sku':'Princess_Plate','product_type':'DISHWARE_PLATE','variation_theme':'SIZE','status':'FRESH','expires_at_utc':'2999-01-01T00:00:00+00:00','payload_hash':'a'*64,'payload':{'children':[{'sku':'DI-113','asin':'B0CC2JX4BL'},{'sku':'DI-114','asin':'B0CC2JH68Q'}]},'parent_ptd':{'schema':{},'schema_checksum':'x'},'child_ptd':{'schema':{},'schema_checksum':'x'}}


def _arm_evidence(parent=404, child_parent=None, bad=None):
    children=[]
    for sku,asin in (('DI-113','B0CC2JX4BL'),('DI-114','B0CC2JH68Q')):
        failed=bad == sku
        children.append({'sku':sku,'asin':asin,'listings_status':429 if failed else 200,'listings_identity':not failed,'listings_product_type':'DISHWARE_PLATE','catalog_status':200,'catalog_identity':not failed,'product_type':'DISHWARE_PLATE','current_parent':child_parent if bad == sku else None,'blocking_issues':False})
    return parent == 404 and not child_parent and not bad, {'parent_sku_status':parent,'children':children}


def test_arm_route_ignores_stored_preflight_and_calls_fresh_service(monkeypatch):
    import contextlib
    from routes import parent_child_routes as routes
    snap=_arm_snapshot(); calls=[]
    monkeypatch.setattr(routes.runtime_settings,'enabled',lambda:False)
    monkeypatch.setattr(routes,'get_snapshot',lambda _:snap)
    monkeypatch.setattr(routes,'_checked_context',lambda *args:{'id':2,'vendor_code':'FK6EI'})
    monkeypatch.setattr(routes.parent_submission,'gates',lambda *args,**kwargs:{'snapshot_fresh':True,'validation_evidence_current':True,'no_active_job_or_reservation':True,'ready':False})
    monkeypatch.setattr(routes,'latest_preflight',lambda *_: (_ for _ in ()).throw(AssertionError('stored preflight used')))
    monkeypatch.setattr(routes,'create_preflight',lambda *args: {'id':'fresh','expires_at_utc':'2999-01-01T00:00:00+00:00'})
    monkeypatch.setattr(routes.parent_submission,'_assert_current',lambda _:None)
    @contextlib.contextmanager
    def conn():
        db=sqlite3.connect(':memory:'); db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE pc_validation_preview_evidence (snapshot_id,payload_hash,expires_at_utc)')
        db.execute('INSERT INTO pc_validation_preview_evidence VALUES(?,?,?)',(snap['id'],snap['payload_hash'],'2999-01-01T00:00:00+00:00'))
        try: yield db
        finally: db.close()
    monkeypatch.setattr(routes,'get_db_connection',conn)
    monkeypatch.setattr(routes.parent_submission,'final_preflight',lambda *args: (calls.append('fresh') or _arm_evidence()))
    monkeypatch.setattr(routes.runtime_settings,'arm_parent_creation',lambda *args:{'armed':True})
    monkeypatch.setattr(routes.router,'_auth_client',object(),raising=False)
    assert routes.arm_parent_creation(routes.ParentArmRequest(snapshot_id=snap['id'],confirmation='ARM PARENT CREATION')) == {'armed':True}
    assert calls == ['fresh']


@pytest.mark.parametrize('parent,child,bad',[(200,None,None),(404,'OTHER-PARENT','DI-113'),(404,'OTHER-PARENT','DI-114'),(404,None,'DI-113'),(404,None,'DI-114')])
def test_fresh_arm_preflight_failures_are_get_only_and_never_put(monkeypatch,parent,child,bad):
    import services.parent_child_parent_submission as submission
    snap=_arm_snapshot(); transport=[]
    class Auth: pass
    monkeypatch.setattr(submission,'_assert_current',lambda _:None)
    monkeypatch.setattr(submission,'listing_item_attributes',lambda *args,**kwargs: transport.append(('GET-listing',args[-1])) or ({'status':parent} if args[-1]=='Princess_Plate' else {'status':429 if bad==args[-1] else 200,'api_sku':args[-1],'api_asins':[next(x['asin'] for x in snap['payload']['children'] if x['sku']==args[-1])],'summaries':[{'asin':next(x['asin'] for x in snap['payload']['children'] if x['sku']==args[-1]),'productType':'DISHWARE_PLATE'}],'product_type':'DISHWARE_PLATE','issues':[]}))
    monkeypatch.setattr(submission,'catalog_lookup',lambda *args,**kwargs: (transport.append(('GET-catalog',args[-1])) or ({'asin':args[-1],'productTypes':[{'productType':'DISHWARE_PLATE'}]}, {'http_status':200})))
    monkeypatch.setattr(submission,'current_parent',lambda _:child)
    ok,_=submission.final_preflight(Auth(),snap,'FK6EI')
    assert not ok and transport and all(call[0].startswith('GET-') for call in transport)


def test_parent_arm_persists_exact_scope_once_and_expired_inputs_fail_closed():
    import services.parent_child_runtime_settings as runtime
    from datetime import datetime, timedelta, timezone
    snap=_arm_snapshot(); future=(datetime.now(timezone.utc)+timedelta(minutes=4)).isoformat()
    armed=runtime.arm_parent_creation(snap,future,future,'ARM PARENT CREATION')
    assert armed['armed'] and armed['arm']['snapshot_id']==snap['id'] and armed['arm']['payload_hash']==snap['payload_hash'] and armed['arm']['parent_sku']=='Princess_Plate'
    try: runtime.arm_parent_creation(snap,future,future,'ARM PARENT CREATION')
    except ValueError: pass
    else: assert False, 'second arm must fail'
    runtime.disarm_parent_creation(snap['id'])
    expired=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError,match='expired'): runtime.arm_parent_creation(snap,expired,future,'ARM PARENT CREATION')


@pytest.mark.parametrize('changed',('parent','DI-113','DI-114'))
def test_submission_preflight_is_independent_and_blocks_before_put(monkeypatch,changed):
    import services.parent_child_parent_submission as submission
    snap=_arm_snapshot(); calls=[]
    monkeypatch.setattr(submission,'gates',lambda *a,**k:{'ready':True})
    monkeypatch.setattr(submission,'final_preflight',lambda *a,**k:(calls.append('GET-preflight') or (False,{'changed':changed})))
    monkeypatch.setattr(submission,'_put',lambda *a,**k: (_ for _ in ()).throw(AssertionError('PUT reached')))
    with pytest.raises(ValueError,match='preflight failed'):
        submission.execute(object(),snap,{},'CREATE PARENT',lambda *a:{'id':'preflight'})
    assert calls==['GET-preflight']


def test_listings_adapter_preserves_api_top_level_sku_and_summary_asin_shape(monkeypatch):
    """Fixture mirrors the sanitized live GetListingsItem shape for DI-113."""
    import services.spapi_listings as listings
    class Response:
        ok=True; status_code=200; headers={'x-amzn-RequestId':'test-request'}
        def json(self): return {'sku':'DI-113','productType':'DISHWARE_PLATE','summaries':[{'asin':'B0CC2JX4BL','productType':'DISHWARE_PLATE'}]}
    monkeypatch.setattr(listings.requests,'get',lambda *a,**k:Response())
    class Auth:
        def get_lwa_access_token(self): return 'test-token'
    result=listings.listing_item_attributes(Auth(),'A2VIGQ35RCS4UG','FK6EI','DI-113')
    assert result['requested_sku']=='DI-113'
    assert result['api_sku']=='DI-113' and result['api_asins']==['B0CC2JX4BL']
    assert result['summaries'][0].get('sku') is None


@pytest.mark.parametrize('failure',(
    None, 'missing_sku', 'wrong_sku', 'missing_asin', 'wrong_asin', 'swapped_asin',
    'missing_listing_type', 'wrong_listing_type', 'missing_catalog_type', 'wrong_catalog_type',
    'listing_non_200', 'catalog_non_200', 'parented', 'ambiguous_parent', 'requested_substitution',
))
def test_final_preflight_requires_explicit_returned_child_identity_and_exact_types(monkeypatch,failure):
    import services.parent_child_parent_submission as submission
    snap=_arm_snapshot()
    monkeypatch.setattr(submission,'_assert_current',lambda _:None)
    def listing(_auth,_market,_vendor,sku):
        if sku=='Princess_Plate': return {'status':404}
        child=next(x for x in snap['payload']['children'] if x['sku']==sku)
        asin=child['asin']
        if failure in ('wrong_asin','swapped_asin'):
            asin=next(x['asin'] for x in snap['payload']['children'] if x['sku']!=sku)
        api_sku=None if failure=='missing_sku' else ('WRONG-SKU' if failure=='wrong_sku' else sku)
        api_asins=[] if failure=='missing_asin' else [asin]
        if failure=='requested_substitution': api_sku=None
        listing_type=None if failure=='missing_listing_type' else ('TOY' if failure=='wrong_listing_type' else 'DISHWARE_PLATE')
        summary_type=None if failure=='missing_listing_type' else 'DISHWARE_PLATE'
        return {'status':500 if failure=='listing_non_200' else 200,'requested_sku':sku,'api_sku':api_sku,'api_asins':api_asins,'summaries':[{'asin':asin,'productType':summary_type}],'product_type':listing_type,'issues':[]}
    def catalog(_auth,_market,asin):
        product_type=None if failure=='missing_catalog_type' else ('TOY' if failure=='wrong_catalog_type' else 'DISHWARE_PLATE')
        parent=[]
        if failure=='parented': parent=[{'relationships':[{'parentAsins':['B0PARENT']}]}]
        if failure=='ambiguous_parent': parent=[{'relationships':[{'parentAsins':['B0PARENT','B0OTHER']}]}]
        return {'asin':asin,'productTypes':([] if product_type is None else [{'productType':product_type}]),'relationships':parent},{'http_status':500 if failure=='catalog_non_200' else 200}
    monkeypatch.setattr(submission,'listing_item_attributes',listing)
    monkeypatch.setattr(submission,'catalog_lookup',catalog)
    ok,evidence=submission.final_preflight(object(),snap,'FK6EI')
    assert ok is (failure is None)
    if failure:
        assert all(not child['listings_identity'] or failure not in ('missing_sku','wrong_sku','missing_asin','wrong_asin','swapped_asin','requested_substitution') for child in evidence['children'])
