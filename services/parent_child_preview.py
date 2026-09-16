"""Pure, read-only payload preview for Parent–Child Manager."""
import re
import json

SENSITIVE = re.compile(r"(token|authorization|credential|secret|signature|x-amz)", re.I)
UNSAFE_SHARED = re.compile(r"(cost|price|external.*id|dimension|weight|compliance|battery|hazmat|dangerous|brand|manufacturer)", re.I)

def _normalized(value):
    """Comparison-only normalization; emitted PATCH values are never changed."""
    if isinstance(value, str): return re.sub(r"\s+", " ", value.strip()).casefold()
    if isinstance(value, list):
        normalized = [_normalized(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str))
    if isinstance(value, dict): return {key: _normalized(item) for key, item in value.items() if key not in ("marketplace_id", "language_tag")}
    return value

def _equivalent(left, right): return _normalized(left) == _normalized(right)

def _property(schema, words):
    for name, node in (schema.get("properties") or {}).items():
        title = str(node.get("title") or name).lower()
        if all(word in title for word in words): return name, node
    return None, {}

def _enum_value(node, desired):
    values = node.get("enum") or node.get("items", {}).get("enum") or []
    return next((value for value in values if str(value).lower() == desired), desired)

def _attribute(schema, title_words, value):
    name, node = _property(schema, title_words)
    if not name: return {}
    if node.get("type") == "array" or node.get("items"):
        return {name: [{"value": value}]}
    return {name: value}

def _localized_attribute(schema, name, value, marketplace_id=None):
    node = (schema.get("properties") or {}).get(name, {})
    if not node.get("items"): return {name: value}
    item = node.get("items") or {}; entry = {"value": value}; required = set(item.get("required") or [])
    if "language_tag" in required:
        language = ((schema.get("$defs") or {}).get("language_tag") or {}).get("default")
        if language: entry["language_tag"] = language
    if "marketplace_id" in required and marketplace_id: entry["marketplace_id"] = marketplace_id
    return {name: [entry]}

def _title_attribute(schema, title, marketplace_id):
    for name, node in (schema.get("properties") or {}).items():
        label = str(node.get("title") or "").casefold()
        if name == "item_name" or label == "title": return _localized_attribute(schema, name, title, marketplace_id)
    return {}

def _theme(schema, theme):
    name, node = _property(schema, ["variation", "theme"])
    if not name: return {}
    if node.get("type") == "array" or node.get("items"):
        return {name: [{"name": theme}]}
    return {name: theme}

def _relationship(schema, parent_sku):
    name, node = _property(schema, ["parent", "sku", "relationship"])
    if not name: return {}
    fields = (node.get("items", {}).get("properties") or {})
    parent_key = next((key for key, value in fields.items() if "parent" in str(value.get("title") or key).lower() and "sku" in str(value.get("title") or key).lower()), None)
    type_key = next((key for key, value in fields.items() if "relationship" in str(value.get("title") or key).lower() and "type" in str(value.get("title") or key).lower()), None)
    entry = {}
    if parent_key and parent_sku: entry[parent_key] = parent_sku
    if type_key: entry[type_key] = _enum_value(fields[type_key], "variation")
    return {name: [entry]} if entry else {}

def _attribute_value(schema, name, value, existing=None, marketplace_id=None):
    """Use the PTD's own array/object shape and preserve existing context."""
    node = (schema.get("properties") or {}).get(name, {})
    if not node.get("items"): return value
    entry = dict((existing or [{}])[0]) if isinstance(existing, list) else {}
    entry["value"] = value
    required = set((node.get("items") or {}).get("required") or [])
    if "language_tag" in required and not entry.get("language_tag"):
        language = ((schema.get("$defs") or {}).get("language_tag") or {}).get("default")
        if language: entry["language_tag"] = language
    if "marketplace_id" in required and not entry.get("marketplace_id") and marketplace_id:
        entry["marketplace_id"] = marketplace_id
    return [entry]

def active_variation_keys(child_schema, theme):
    """Return only the Child PTD dimensions activated by this exact theme."""
    keys = set(); properties = child_schema.get("properties") or {}; has_theme_condition = False
    def theme_names(value):
        if isinstance(value, dict):
            direct = value.get("properties", {}).get("variation_theme", {}).get("contains", {}).get("properties", {}).get("name", {}).get("enum", [])
            return set(direct) | set().union(*(theme_names(item) for item in value.values()))
        if isinstance(value, list): return set().union(*(theme_names(item) for item in value))
        return set()
    def visit(node):
        nonlocal has_theme_condition
        if isinstance(node, dict):
            if node.get("if"):
                names = theme_names(node["if"])
                has_theme_condition = has_theme_condition or bool(names)
                if theme in names:
                    keys.update(name for name in (node.get("then", {}).get("required") or []) if name in properties)
            for value in node.values(): visit(value)
        elif isinstance(node, list):
            for value in node: visit(value)
    visit(child_schema.get("allOf") or [])
    # Compact schemas used by older callers have no theme conditional at all;
    # their non-system required fields are the only authoritative dimensions.
    if not has_theme_condition:
        keys.update(name for name in (child_schema.get("required") or []) if name in properties and name not in {"parentage_level", "variation_theme", "child_parent_sku_relationship"})
        if not keys and "size" in properties and theme == "SIZE": keys.add("size")
    return keys

def _parent_required(schema):
    # A Parent PTD can intentionally require a variation-family declaration.
    # It is system-derived, but still an authoritative Parent requirement.
    return list(schema.get("required") or [])

def _parentage(schema, level):
    name, node = _property(schema, ["parentage", "level"])
    if not name: return {}
    value = _enum_value(node.get("items", {}).get("properties", {}).get("value", node), level)
    return {name: [{"value": value}]} if node.get("type") == "array" or node.get("items") else {name: value}

def _friendly(schema, path):
    name = path.lstrip("/").split("/")[0]
    return (schema.get("properties", {}).get(name, {}).get("title") or name.replace("_", " ").title())

def validate(schema, attributes):
    errors = []
    for name in schema.get("required") or []:
        if name not in attributes or attributes[name] in (None, "", [], {}): errors.append({"path": f"/{name}", "message": "is required", "field": _friendly(schema, f"/{name}")})
    for name, value in attributes.items():
        node = (schema.get("properties") or {}).get(name, {})
        values = node.get("enum") or []
        if values and value not in values: errors.append({"path": f"/{name}", "message": "is not an allowed value", "field": _friendly(schema, f"/{name}")})
    return errors

def validate_parent(schema, attributes):
    clone = dict(schema); clone["required"] = _parent_required(schema)
    return validate(clone, attributes)

def validate_patch(schema, patches):
    errors = []
    properties = schema.get("properties") or {}
    for patch in patches:
        path = patch.get("path", ""); name = path.removeprefix("/attributes/")
        if patch.get("op") not in ("add", "replace"): errors.append({"path": path, "message": "must use add or replace", "field": name})
        elif name not in properties: errors.append({"path": path, "message": "is not a PTD attribute", "field": name})
        elif patch.get("value") in (None, "", [], {}): errors.append({"path": path, "message": "has no value", "field": _friendly(schema, f"/{name}")})
    return errors

def _redact(value):
    if isinstance(value, dict): return {key: "[redacted]" if SENSITIVE.search(key) else _redact(item) for key, item in value.items()}
    if isinstance(value, list): return [_redact(item) for item in value]
    return value

def _shared_suggestions(parent_schema, children, existing_by_sku, confirmed):
    suggestions = []; parent_props = parent_schema.get("properties") or {}
    for name, node in parent_props.items():
        if UNSAFE_SHARED.search(name) or UNSAFE_SHARED.search(str(node.get("title") or "")): continue
        values = [((existing_by_sku.get(child.get("vendor_sku"), {}).get("attributes") or {}).get(name)) for child in children]
        if not values or any(value in (None, [], {}) for value in values) or not all(_equivalent(values[0], value) for value in values[1:]): continue
        suggestions.append({"attribute": name, "field": node.get("title") or name.replace("_", " ").title(), "value": values[0], "source_asins": [child.get("asin") for child in children], "confirmed": name in confirmed})
    return suggestions

def build_preview(data, parent_schema, child_schema, existing_by_sku=None):
    existing_by_sku = existing_by_sku or {}
    parent_sku, title, theme = data.get("parent_sku"), data.get("parent_title"), data.get("variation_theme")
    parent_attributes = {}; parent_attributes.update(_parentage(parent_schema, "parent")); parent_attributes.update(_theme(parent_schema, theme)); parent_attributes.update(_relationship(parent_schema, None)); parent_attributes.update(_title_attribute(parent_schema, title, data.get("marketplace_id")))
    for name, value in (data.get("parent_values") or {}).items():
        if name in (parent_schema.get("properties") or {}): parent_attributes.update(_localized_attribute(parent_schema, name, value, data.get("marketplace_id")))
    parent = {"operation": "putListingsItem", "sku": parent_sku, "body": {"productType": data.get("product_type"), "requirements": "LISTING", "attributes": parent_attributes}}
    children = []; confirmed_suggestions = set(data.get("confirmed_suggestions") or [])
    active_keys = active_variation_keys(child_schema, theme)
    for item in data.get("items", []):
        existing = existing_by_sku.get(item.get("vendor_sku"), {})
        existing_attributes = existing.get("attributes") or {}
        attributes = {}; attributes.update(_parentage(child_schema, "child")); attributes.update(_theme(child_schema, theme)); attributes.update(_relationship(child_schema, parent_sku))
        supplied = item.get("variation_values") or {}
        inactive = sorted(set(supplied) - active_keys)
        for name in sorted(active_keys):
            if name in supplied:
                attributes[name] = _attribute_value(child_schema, name, supplied[name], existing_attributes.get(name), data.get("marketplace_id"))
        changes = []; patches = []
        for name, value in attributes.items():
            existing_value = existing_attributes.get(name); verified = existing.get("status") in (None, 200)
            if name in existing_attributes and _equivalent(existing_value, value): status = "Already correct — no change"
            elif not verified: status = "Cannot verify"
            else: status = "Replace" if name in existing_attributes else "Add"
            changes.append({"attribute": name, "field": _friendly(child_schema, f"/{name}"), "status": status, "existing": existing_value, "proposed": value})
            if status in ("Add", "Replace", "Cannot verify"): patches.append({"op": "replace" if status == "Replace" else "add", "path": f"/attributes/{name}", "value": value})
        children.append({"asin": item.get("asin"), "sku": item.get("vendor_sku"), "current_parent": item.get("current_parent"), "existing": existing_attributes, "changes": changes, "inactive_variation_keys": inactive, "operation": "patchListingsItem", "body": {"productType": data.get("product_type"), "patches": patches}})
    parent_errors = validate_parent(parent_schema, parent["body"]["attributes"])
    child_errors = {child["asin"] or child["sku"]: validate_patch(child_schema, child["body"]["patches"]) + [{"path": f"/attributes/{name}", "field": _friendly(child_schema, f"/{name}"), "message": "is not active for the selected variation theme"} for name in child["inactive_variation_keys"]] for child in children}
    errors = {**({"Parent": parent_errors} if parent_errors else {}), **{key: value for key, value in child_errors.items() if value}}
    return _redact({"parent": parent, "children": children, "parent_required_fields": _parent_required(parent_schema), "shared_suggestions": _shared_suggestions(parent_schema, data.get("items", []), existing_by_sku, confirmed_suggestions), "plan": ["Create/update proposed parent", "Wait for parent acceptance", "Attach children individually", "Verify Listings Item issues", "Verify Catalog relationships over 24–48 hours"], "validation": {"passed": not errors, "errors": errors}})
