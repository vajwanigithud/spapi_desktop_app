# ============================================================================
# WARNING: UNDOCUMENTED ENDPOINT
# ============================================================================
# The /vendor/transactionStatus endpoint used in this module is NOT present in
# the official SP-API schemas (spapi_models/models/**). This is a legacy or
# undocumented endpoint that may:
#   - Be deprecated without notice
#   - Have behavior changes not reflected in official documentation
#   - Be removed from Amazon's infrastructure
#
# OFFICIAL REPLACEMENT (for transaction status):
#   - /vendor/transactions/v1/transactions/{transactionId}
#   - See: spapi_models/models/vendor-transaction-status-api-model/
#   - This endpoint requires a transactionId (returned from async POST operations)
#   - It provides structured transaction status (Processing, Success, Failure)
#
# TO MIGRATE:
#   - Define a fetch_transactions_by_id(transaction_id) function (TODO: scaffold below)
#   - Update callers to track transaction IDs from their POST requests
# ============================================================================

import requests
import logging
from urllib.parse import urlencode
from auth.spapi_auth import SpApiAuth
from config import MARKETPLACE_ID

logger = logging.getLogger(__name__)

# FEATURE FLAG: Disable undocumented /vendor/transactionStatus endpoint by default.
# Set to True ONLY if you have confirmed with Amazon that this endpoint is stable
# in your marketplace and have documented the expected response structure.
ENABLE_VENDOR_TRANSACTION_STATUS_LEGACY = False


class VendorTransactions:
    """
    Vendor Transactions API client.
    
    ⚠️ WARNING: Uses undocumented /vendor/transactionStatus endpoint
    
    This endpoint is NOT in official SP-API schemas and is disabled by default.
    To enable, set ENABLE_VENDOR_TRANSACTION_STATUS_LEGACY = True and verify
    with Amazon that it is supported in your marketplace.
    
    See module-level comment for migration path to official endpoint.
    """
    BASE_URL = "https://sellingpartnerapi-eu.amazon.com/vendor/transactionStatus"

    def __init__(self):
        self.auth = SpApiAuth()

    def fetch_transactions(self, transaction_type="SHIPMENT", created_after=None, created_before=None, limit=50):
        """
        Fetch transactions using the undocumented /vendor/transactionStatus endpoint.
        
        ⚠️ UNDOCUMENTED ENDPOINT - DISABLED BY DEFAULT
        
        This endpoint is NOT in official SP-API schemas. It may be:
        - Deprecated without warning
        - Removed from Amazon's infrastructure
        - Changed without documentation
        
        If disabled (default), raises NotImplementedError.
        
        Args:
            transaction_type: Type of transaction (SHIPMENT, PURCHASE_ORDER, RFQLINE)
            created_after: ISO8601 start date
            created_before: ISO8601 end date
            limit: Max results per page
            
        Returns:
            dict: Response from API (if enabled)
            
        Raises:
            NotImplementedError: If feature flag ENABLE_VENDOR_TRANSACTION_STATUS_LEGACY is False
            HTTPError: If API call fails
        """
        # SAFEGUARD: Check feature flag
        if not ENABLE_VENDOR_TRANSACTION_STATUS_LEGACY:
            raise NotImplementedError(
                "The /vendor/transactionStatus endpoint is undocumented and disabled by default. "
                "Set ENABLE_VENDOR_TRANSACTION_STATUS_LEGACY=True in modules/vendor_transactions.py "
                "only after confirming with Amazon that this endpoint is supported. "
                "For transaction status queries, use the official endpoint: "
                "/vendor/transactions/v1/transactions/{transactionId} instead."
            )
        
        logger.warning(
            "[VendorTransactions] Using undocumented /vendor/transactionStatus endpoint. "
            "This may be deprecated or removed without notice. "
            "Consider migrating to /vendor/transactions/v1/transactions/{transactionId}."
        )
        
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
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # TODO: Scaffold official transaction status endpoint
    # =====================================================================
    # @staticmethod
    # def fetch_transaction_by_id(transaction_id: str) -> dict:
    #     """
    #     Fetch transaction status using OFFICIAL endpoint.
    #
    #     Endpoint: GET /vendor/transactions/v1/transactions/{transactionId}
    #     Schema: spapi_models/models/vendor-transaction-status-api-model/
    #     Operation ID: getTransaction
    #
    #     Args:
    #         transaction_id: Transaction GUID from async POST response
    #
    #     Returns:
    #         dict: {
    #             "payload": {
    #                 "transactionStatus": {
    #                     "transactionId": "...",
    #                     "status": "Processing|Success|Failure",
    #                     "errors": [...]  # if Failure
    #                 }
    #             }
    #         }
    #
    #     Raises:
    #         HTTPError: If API call fails
    #
    #     NOTE: Requires transactionId from the POST operation that created
    #           the transaction. Add tracking to your async POST handlers.
    #     """
    #     # Implementation here
    #     pass
    # =====================================================================
