from datetime import datetime, timezone
from services.spapi_listings import listing_item_attributes, listings_lookup
from services.spapi_catalog_relationships import catalog_lookup, current_parent, thumbnail

def _catalog_fields(data):
    summary=(data.get('summaries') or [{}])[0]; types=data.get('productTypes') or []
    return {'title':summary.get('itemName'),'image_url':thumbnail(data),'product_type':(types[0].get('productType') if types else None),'current_parent':current_parent(data)}

def resolve_many(auth_client, marketplace_id, vendor_code, asins):
    listings,listing_details=listings_lookup(auth_client,marketplace_id,vendor_code,asins)
    results=[]
    for asin in asins:
        catalog,catalog_details=catalog_lookup(auth_client,marketplace_id,asin)
        base={'asin':asin,'vendor_sku':None,'current_parent':'Unknown','status':'Blocked','status_detail':listing_details['status'],'lookup_details':{'catalog':catalog_details,'vendor_listing':listing_details},'evidence':{'catalog':catalog or {},'listings':listing_details}}
        catalog_found = bool(catalog)
        if catalog_found:
            base.update(_catalog_fields(catalog)); base['current_parent']=base['current_parent'] or None
        skus=listings.get(asin,[])
        if len(skus)>1:
            base.update(status='Needs Review',status_detail='Ambiguous Vendor SKU',vendor_skus=skus)
        elif not catalog_found:
            # Catalog is authoritative for product types and relationships; do not
            # treat a Listings-only match as safe for a family.
            base.update(status='Lookup Failed', status_detail=catalog_details['status'])
            if len(skus) == 1: base['vendor_sku'] = skus[0]
        elif not base.get('product_type'):
            base.update(status='Needs Review', status_detail='Catalog Product Type Unavailable')
            if len(skus) == 1: base['vendor_sku'] = skus[0]
        elif len(skus)==1:
            attributes=listing_item_attributes(auth_client,marketplace_id,vendor_code,skus[0]) if hasattr(auth_client, 'get_lwa_access_token') else {'attributes':{},'status':None}
            base.update(status='Needs Review' if base['current_parent'] else 'Ready',status_detail='Existing Different Parent' if base['current_parent'] else None,vendor_sku=skus[0],verified_attributes=attributes.get('attributes') or {},attributes_status=attributes.get('status'),attributes_retrieved_at_utc=datetime.now(timezone.utc).isoformat())
            base['evidence']['listing_attributes']=attributes
        elif listing_details['status']!='Vendor Listing Not Found': base['status_detail']=listing_details['status']
        results.append(base)
    return results

def resolve(auth_client, marketplace_id, vendor_code, asin):
    return resolve_many(auth_client,marketplace_id,vendor_code,[asin])[0]
