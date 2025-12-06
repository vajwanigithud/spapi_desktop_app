# ================================================================
#  SP-API AUTH MODULE (NO AWS REQUIRED)
#  ---------------------------------------------------------------
#  - Get LWA Access Token
#  - Get Restricted Data Token (RDT)
#  - Make API calls using only LWA + RDT
# ================================================================

import requests
import datetime

from config import (
    LWA_CLIENT_ID,
    LWA_CLIENT_SECRET,
    LWA_REFRESH_TOKEN
)

class SpApiAuth:
    def __init__(self):
        self._lwa_token = None
        self._lwa_expiry = None

    # ------------------------------------------------------------
    # LWA ACCESS TOKEN
    # ------------------------------------------------------------
    def get_lwa_access_token(self):
        if self._lwa_token and self._lwa_expiry > datetime.datetime.utcnow():
            return self._lwa_token

        url = "https://api.amazon.com/auth/o2/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": LWA_REFRESH_TOKEN,
            "client_id": LWA_CLIENT_ID,
            "client_secret": LWA_CLIENT_SECRET,
        }

        resp = requests.post(url, data=data)
        if resp.status_code != 200:
            print("❌ LWA token request failed")
            print("Status:", resp.status_code)
            print("Response:", resp.text)
        resp.raise_for_status()
        payload = resp.json()

        self._lwa_token = payload["access_token"]
        self._lwa_expiry = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=payload.get("expires_in", 3600) - 60
        )
        return self._lwa_token

    # ------------------------------------------------------------
    # RESTRICTED DATA TOKEN (RDT)
    # ------------------------------------------------------------
    def get_rdt(self, restricted_resources):
        lwa_token = self.get_lwa_access_token()

        url = "https://sellingpartnerapi-eu.amazon.com/tokens/2021-03-01/restrictedDataToken"
        body = {
            "restrictedResources": restricted_resources
        }

        headers = {
            "Content-Type": "application/json",
            "x-amz-access-token": lwa_token
        }

        resp = requests.post(url, json=body, headers=headers)
        if resp.status_code != 200:
            print("❌ RDT request failed")
            print("Status:", resp.status_code)
            print("URL:", url)
            print("Body sent:", body)
            print("Response:", resp.text)
        resp.raise_for_status()

        return resp.json()["restrictedDataToken"]
