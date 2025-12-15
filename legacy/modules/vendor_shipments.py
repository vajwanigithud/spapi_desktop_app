import logging
from urllib.parse import urlencode

import requests

from auth.spapi_auth import SpApiAuth
from config import MARKETPLACE_ID

logger = logging.getLogger(__name__)


class VendorShipments:
    BASE_URL = "https://sellingpartnerapi-eu.amazon.com/vendor/shipping/v1/shipments"

    def __init__(self):
        self.auth = SpApiAuth()

    def fetch_shipments(self, created_after=None, created_before=None, limit=50, max_pages=1, next_token=None):
        """
        Fetch shipments with pagination support.
        
        Args:
            created_after: ISO8601 start date
            created_before: ISO8601 end date
            limit: Results per page (max 100, default 50)
            max_pages: Maximum number of pages to fetch (default 1 for backward compatibility)
            next_token: Token from previous page for resuming pagination
            
        Returns:
            dict: {
                "shipments": [...],  # merged from all pages
                "pagination": {
                    "pages_fetched": N,
                    "next_token": "..." or None  (if more pages available)
                }
            }
        """
        lwa = self.auth.get_lwa_access_token()
        all_shipments = []
        current_next_token = next_token
        pages_fetched = 0
        
        while pages_fetched < max_pages:
            params = {}
            if created_after:
                params["createdAfter"] = created_after
            if created_before:
                params["createdBefore"] = created_before
            if limit:
                params["limit"] = limit
            if MARKETPLACE_ID:
                params["marketplaceId"] = MARKETPLACE_ID
            if current_next_token:
                params["nextToken"] = current_next_token
            
            url = f"{self.BASE_URL}?{urlencode(params)}"
            headers = {
                "Authorization": f"Bearer {lwa}",
                "x-amz-access-token": lwa,
                "accept": "application/json",
            }
            
            try:
                resp = requests.get(url, headers=headers, timeout=30)
                resp.raise_for_status()
            except requests.exceptions.Timeout:
                logger.error(f"[VendorShipments] Timeout fetching shipments after 30s (page {pages_fetched + 1})")
                break
            except requests.exceptions.RequestException as e:
                logger.error(f"[VendorShipments] Network error fetching shipments: {e}")
                raise
            
            data = resp.json()
            shipments = data.get("shipments") or []
            all_shipments.extend(shipments)
            pages_fetched += 1
            
            logger.info(f"[VendorShipments] Fetched page {pages_fetched}: {len(shipments)} shipments")
            
            # Check if there are more pages
            current_next_token = data.get("nextToken")
            if not current_next_token:
                logger.info(f"[VendorShipments] Reached last page at page {pages_fetched}")
                break
        
        return {
            "shipments": all_shipments,
            "pagination": {
                "pages_fetched": pages_fetched,
                "next_token": current_next_token,
            }
        }
