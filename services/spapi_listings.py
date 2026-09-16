"""Read-only Listings Items access. This module deliberately exposes no write method."""
import time
import requests
def _host(marketplace_id): return 'https://sellingpartnerapi-eu.amazon.com'
def listings_lookup(auth_client, marketplace_id, vendor_code, asins):
    # Vendor code is carried as an explicit request scope and result is never inferred from catalog data.
    token=auth_client.get_lwa_access_token(); headers={'x-amz-access-token':token}
    url=f'{_host(marketplace_id)}/listings/2021-08-01/items/{vendor_code}'
    started=time.monotonic(); identifiers=','.join(asins if isinstance(asins,list) else [asins])
    response=requests.get(url,params={'marketplaceIds':marketplace_id,'includedData':'summaries,attributes','identifiers':identifiers,'identifiersType':'ASIN'},headers=headers,timeout=20)
    try: payload=response.json()
    except ValueError: payload={}
    request_id=response.headers.get('x-amzn-RequestId') or response.headers.get('x-amz-request-id')
    if not response.ok:
        states={401:'Listings Access Denied',403:'Listings Access Denied',429:'Amazon Throttled'}
        return {},{'status':states.get(response.status_code,'Listings Request Failed'),'http_status':response.status_code,'request_id':request_id,'result_count':0,'error':payload.get('message') or (payload.get('errors') or [{}])[0].get('message'),'elapsed_ms':round((time.monotonic()-started)*1000)}
    found={}
    for item in payload.get('items',[]):
        for summary in item.get('summaries') or []:
            asin=summary.get('asin')
            if asin and item.get('sku'):found.setdefault(asin,[]).append(item['sku'])
    return {asin:sorted(set(skus)) for asin,skus in found.items()},{'status':'Vendor Listing Found' if found else 'Vendor Listing Not Found','http_status':response.status_code,'request_id':request_id,'result_count':len(payload.get('items') or []),'error':None,'elapsed_ms':round((time.monotonic()-started)*1000)}

def find_vendor_skus(auth_client, marketplace_id, vendor_code, asin):
    return listings_lookup(auth_client,marketplace_id,vendor_code,[asin])[0].get(asin,[])

def listing_item_attributes(auth_client, marketplace_id, vendor_code, sku):
    """Read existing item attributes for a dry-run PATCH comparison only."""
    token=auth_client.get_lwa_access_token()
    response=requests.get(f'{_host(marketplace_id)}/listings/2021-08-01/items/{vendor_code}/{sku}',params={'marketplaceIds':marketplace_id,'includedData':'attributes,summaries,issues'},headers={'x-amz-access-token':token},timeout=20)
    try: data=response.json()
    except ValueError: data={}
    request_id=response.headers.get('x-amzn-RequestId') or response.headers.get('x-amz-request-id')
    if not response.ok: return {'attributes':{},'issues':[], 'summaries':[], 'status': response.status_code, 'request_id':request_id, 'requested_sku':sku, 'api_sku':None, 'api_asins':[], 'product_type':None}
    summaries=data.get('summaries') or []
    api_asins=[row.get('asin') for row in summaries if isinstance(row,dict) and isinstance(row.get('asin'),str) and row.get('asin').strip()]
    return {'attributes':data.get('attributes') or {},'issues':data.get('issues') or [],'summaries':summaries,'status':response.status_code,'request_id':request_id,'requested_sku':sku,'api_sku':data.get('sku'),'api_asins':api_asins,'product_type':data.get('productType') or next((row.get('productType') for row in summaries if row.get('productType')),None)}

def verify_vendor_access(auth_client, marketplace_id, vendor_code):
    """Perform a read-only Listings search; response errors are never treated as verification."""
    token = auth_client.get_lwa_access_token()
    response = requests.get(
        f'{_host(marketplace_id)}/listings/2021-08-01/items/{vendor_code}',
        params={'marketplaceIds': marketplace_id, 'includedData': 'summaries', 'identifiers': '0000000000', 'identifiersType': 'ASIN'},
        headers={'x-amz-access-token': token}, timeout=20,
    )
    try: payload = response.json()
    except ValueError: payload = {}
    if response.ok and not payload.get('errors') and not payload.get('issues'):
        return {'verified': True, 'capability_result': 'Listings Items read access verified', 'last_error': None}
    status = response.status_code
    if status in (401,): reason = 'Authentication failed'
    elif status in (403,): reason = 'Access denied or Product Listing permission missing'
    elif status == 429: reason = 'Amazon throttled the request'
    elif status >= 500: reason = 'Temporary Amazon error'
    elif status == 400: reason = 'Invalid vendor code or marketplace mismatch'
    else: reason = (payload.get('message') or payload.get('errors', [{}])[0].get('message') or f'Verification failed (HTTP {status})')
    return {'verified': False, 'capability_result': None, 'last_error': reason}
