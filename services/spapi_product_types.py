"""Read-only Product Type Definition retrieval and schema-link resolution."""
import json
import requests
from services.db import get_db_connection
from services.parent_child_store import utcnow

class ProductTypeDefinitionError(Exception):
    def __init__(self, status_code, message):
        self.status_code, self.message = status_code, message
        super().__init__(message)

def _sanitized_error(response):
    try:
        errors = response.json().get("errors", [])
        message = "; ".join(str(item.get("message") or item.get("code") or "Amazon rejected the request") for item in errors)
    except (ValueError, AttributeError, TypeError):
        message = "Amazon rejected the request"
    return message[:300] or "Amazon rejected the request"

def _request_json(url, *, params=None, headers=None):
    response = requests.get(url, params=params, headers=headers, timeout=20)
    if not response.ok: raise ProductTypeDefinitionError(response.status_code, _sanitized_error(response))
    try: return response.json(), response.status_code
    except ValueError: raise ProductTypeDefinitionError(response.status_code, "Amazon returned an invalid schema response")

def _is_schema(value): return isinstance(value, dict) and isinstance(value.get("properties"), dict)

def get_definition(auth_client, marketplace_id, vendor_code, product_type, parentage_level, force=False):
    """Return the resolved JSON Schema for exactly one parentage level.

    Vendor code scopes the verified local context/cache, but is not sent as
    sellerId: a vendor code is not an Amazon seller identifier.
    """
    level = parentage_level.upper()
    params = {"marketplaceIds": marketplace_id, "requirements": "LISTING", "requirementsEnforced": "ENFORCED", "parentageLevel": level}
    trace = {"parentageLevel": level, "marketplaceIds": marketplace_id, "vendor_context": vendor_code, "sellerId": None, "product_type": product_type, "locale": None, "requirements": "LISTING", "requirementsEnforced": "ENFORCED", "definition_status": None, "schema_status": None}
    if not force:
        with get_db_connection() as c:
            row = c.execute("SELECT schema_json FROM pc_ptd_cache WHERE marketplace_id=? AND vendor_code=? AND product_type=? AND parentage_level=?", (marketplace_id, vendor_code, product_type, parentage_level.lower())).fetchone()
        if row:
            cached = json.loads(row["schema_json"])
            if _is_schema(cached):
                trace.update(definition_status=200, schema_status=200, source="cache")
                return cached, trace
    token = auth_client.get_lwa_access_token()
    definition, status = _request_json(f"https://sellingpartnerapi-eu.amazon.com/definitions/2020-09-01/productTypes/{product_type}", params=params, headers={"x-amz-access-token": token})
    trace["definition_status"] = status
    trace["locale"] = definition.get("locale")
    resource = ((definition.get("schema") or {}).get("link") or {}).get("resource")
    if not resource: raise ProductTypeDefinitionError(status, "Amazon did not provide a schema resource")
    schema, schema_status = _request_json(resource)
    trace["schema_status"] = schema_status
    if not _is_schema(schema): raise ProductTypeDefinitionError(schema_status, "Amazon returned a schema without properties")
    with get_db_connection() as c:
        c.execute("INSERT OR REPLACE INTO pc_ptd_cache VALUES(?,?,?,?,?,?)", (marketplace_id, vendor_code, product_type, parentage_level.lower(), json.dumps(schema), utcnow()))
        c.commit()
    return schema, trace
