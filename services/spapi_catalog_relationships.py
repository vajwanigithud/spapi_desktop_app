import time
import requests

HOST='https://sellingpartnerapi-eu.amazon.com'
INCLUDED='summaries,images,relationships,productTypes,attributes,vendorDetails'

def _details(response, started, status, error=None):
    return {'status':status,'http_status':response.status_code if response else None,'request_id':(response.headers.get('x-amzn-RequestId') or response.headers.get('x-amz-request-id')) if response else None,'error':error,'elapsed_ms':round((time.monotonic()-started)*1000)}

def catalog_lookup(auth_client, marketplace_id, asin):
    token=auth_client.get_lwa_access_token(); url=f'{HOST}/catalog/2022-04-01/items/{asin}'
    def call(included):
        started=time.monotonic(); response=requests.get(url,params={'marketplaceIds':marketplace_id,'includedData':included},headers={'x-amz-access-token':token},timeout=20)
        try: payload=response.json()
        except ValueError: payload={}
        return response,payload,started
    response,payload,started=call(INCLUDED)
    if response.status_code in (401,403): response,payload,started=call(INCLUDED.replace(',vendorDetails',''))
    if response.ok:return payload,_details(response,started,'Catalog Item Found')
    states={404:'Catalog Item Not Found',401:'Catalog Access Denied',403:'Catalog Access Denied',429:'Amazon Throttled'}
    return None,_details(response,started,states.get(response.status_code,'Catalog Request Failed'),payload.get('message') or (payload.get('errors') or [{}])[0].get('message'))

def get_catalog_item(auth_client, marketplace_id, asin):
    data,details=catalog_lookup(auth_client,marketplace_id,asin)
    if data is None:raise requests.HTTPError(details['error'] or details['status'])
    return data

def thumbnail(payload):
    images=[image for group in payload.get('images',[]) for image in group.get('images',[])]
    pick=next((image for image in images if image.get('variant')=='MAIN'),None) or (images[0] if images else {})
    return pick.get('link') or pick.get('url')

def current_parent(payload):
    for group in payload.get('relationships',[]):
        for relation in group.get('relationships',[]):
            if relation.get('parentAsins'):return relation['parentAsins'][0]
    return None
