import re
ASIN_RE=re.compile(r'^[A-Z0-9]{10}$')
"""Explicit, fail-closed Parent--Child feature flags.

These are deliberately literals rather than browser/configurable values.  The
first live parent creation requires a reviewed source change and restart.
"""
WRITE_FEATURE_FLAGS={'create_parent':False,'attach_child':False,'detach_child':False,'move_child':False,'bulk_feed':False,'amazon_validation_preview':False}
# Amazon transport remains disabled while server-authoritative local validation is repaired.
AMAZON_VALIDATION_PREVIEW=WRITE_FEATURE_FLAGS['amazon_validation_preview']
def parse_asins(text):
    raw=re.split(r'[\s,]+', text or '')
    raw=[x for x in raw if x]
    invalid=[]; unique=[]; seen=set(); duplicates=0
    for value in raw:
        if not value.isascii() or not ASIN_RE.fullmatch(value.upper()): invalid.append(value); continue
        value=value.upper()
        if value in seen: duplicates+=1
        else: seen.add(value); unique.append(value)
    return {'total_input_count':len(raw),'valid_unique_count':len(unique),'duplicate_count':duplicates,'invalid_count':len(invalid),'asins':unique,'invalid':invalid}
def theme_options(schema):
    attrs=(schema or {}).get('properties',{})
    node=attrs.get('variation_theme') or attrs.get('variationTheme') or {}
    item=node.get('items',{})
    name_node=(item.get('properties') or {}).get('name',{})
    values=node.get('enum') or item.get('enum') or name_node.get('enum') or []
    lifecycle=name_node.get('$lifecycle') or node.get('$lifecycle') or {}
    deprecated=set(node.get('deprecatedEnumValues') or node.get('deprecated_values') or lifecycle.get('enumDeprecated') or [])
    return [value for value in values if value not in deprecated]

def family_product_type(items):
    """Classify catalog product types without guessing from an incomplete child."""
    eligible=[item for item in items if item.get('status') not in ('Blocked','Needs Review')]
    missing=[item for item in eligible if not item.get('product_type')]
    types={item['product_type'] for item in eligible if item.get('product_type')}
    if missing:
        return {'status':'missing','product_type':None,'missing_asins':[item.get('asin') for item in missing],'groups':{}}
    if len(types)==1:
        product_type=next(iter(types)); return {'status':'consistent','product_type':product_type,'missing_asins':[],'groups':{product_type:[item.get('asin') for item in eligible]}}
    return {'status':'mixed','product_type':None,'missing_asins':[],'groups':{value:[item.get('asin') for item in eligible if item.get('product_type')==value] for value in sorted(types)}}
