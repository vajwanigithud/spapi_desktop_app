# ================================================================
#  SP-API AUTH MODULE (NO AWS REQUIRED)
#  ---------------------------------------------------------------
#  - Get LWA Access Token
#  - Get Restricted Data Token (RDT)
#  - Make API calls using only LWA + RDT
# ================================================================

import requests
import datetime
import time
import logging

from config import (
    LWA_CLIENT_ID,
    LWA_CLIENT_SECRET,
    LWA_REFRESH_TOKEN
)

logger = logging.getLogger("spapi_auth")

class SpApiAuth:
    def __init__(self):
        self._lwa_token = None
        self._lwa_expiry = None

    # ====================================================================
    # FIX #1: AUTH TOKEN RETRY + TIMEOUT
    # - Retries 3 times with exponential backoff (1s, 2s, 4s) on transient errors
    # - Timeout of 15s prevents infinite hang on network failure
    # - Logs clear error messages for debugging
    # ====================================================================
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

        # Retry logic with exponential backoff
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                # HARDENING: Add 15s timeout to prevent infinite hang
                resp = requests.post(url, data=data, timeout=15)
                
                if resp.status_code == 200:
                    payload = resp.json()
                    self._lwa_token = payload["access_token"]
                    self._lwa_expiry = datetime.datetime.utcnow() + datetime.timedelta(
                        seconds=payload.get("expires_in", 3600) - 60
                    )
                    logger.info("[Auth] Successfully obtained LWA token")
                    return self._lwa_token
                elif resp.status_code == 429:
                    # Rate limited, wait before retry
                    wait_time = 2 ** (attempt - 1)
                    logger.warning(f"[Auth] Token request rate limited (429), waiting {wait_time}s before retry {attempt}/{max_attempts}")
                    if attempt < max_attempts:
                        time.sleep(wait_time)
                        continue
                    # If last attempt, raise
                    resp.raise_for_status()
                else:
                    # Other error
                    logger.error(f"[Auth] Token request failed {resp.status_code}: {resp.text}")
                    resp.raise_for_status()
            
            except requests.exceptions.Timeout:
                logger.warning(f"[Auth] Token request timeout (15s), attempt {attempt}/{max_attempts}")
                if attempt < max_attempts:
                    wait_time = 2 ** (attempt - 1)
                    time.sleep(wait_time)
                    continue
                logger.error(f"[Auth] Token request failed after {max_attempts} attempts (timeout)")
                raise
            
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"[Auth] Connection error, attempt {attempt}/{max_attempts}: {e}")
                if attempt < max_attempts:
                    wait_time = 2 ** (attempt - 1)
                    time.sleep(wait_time)
                    continue
                logger.error(f"[Auth] Connection failed after {max_attempts} attempts")
                raise
            
            except Exception as e:
                logger.error(f"[Auth] Unexpected error, attempt {attempt}/{max_attempts}: {e}")
                if attempt < max_attempts:
                    wait_time = 2 ** (attempt - 1)
                    time.sleep(wait_time)
                    continue
                raise
        
        raise RuntimeError("[Auth] Failed to obtain LWA token after 3 attempts")

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
