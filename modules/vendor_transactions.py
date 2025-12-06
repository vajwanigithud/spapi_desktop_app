import requests
from urllib.parse import urlencode
from auth.spapi_auth import SpApiAuth
from config import MARKETPLACE_ID


class VendorTransactions:
    BASE_URL = "https://sellingpartnerapi-eu.amazon.com/vendor/transactionStatus"

    def __init__(self):
        self.auth = SpApiAuth()

    def fetch_transactions(self, transaction_type="SHIPMENT", created_after=None, created_before=None, limit=50):
        lwa = self.auth.get_lwa_access_token()
        params = {"transactionType": transaction_type}
        if created_after:
            params["createdAfter"] = created_after
        if created_before:
            params["createdBefore"] = created_before
        if limit:
            params["limit"] = limit
        if MARKETPLACE_ID:
            params["marketplaceId"] = MARKETPLACE_ID

        url = f"{self.BASE_URL}?{urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {lwa}",
            "x-amz-access-token": lwa,
            "accept": "application/json",
        }
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()
