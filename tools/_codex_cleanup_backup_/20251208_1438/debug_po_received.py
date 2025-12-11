#!/usr/bin/env python3
"""
Debug script to check what received quantity data is available from SP-API
for a specific PO number.
"""

import json
from pathlib import Path
from main import _fetch_po_details_from_api, _fetch_vendor_shipments_for_po, MARKETPLACE_IDS

PO_NUM = "6RD2BEAD"  # Your example PO that should have 40 received

if __name__ == "__main__":
    marketplace = MARKETPLACE_IDS[0].strip() if MARKETPLACE_IDS else "A2VIGQ35RCS4UG"
    
    print(f"\n[DEBUG] Fetching PO details for {PO_NUM} from {marketplace}")
    po_details = _fetch_po_details_from_api(PO_NUM, marketplace)
    
    if po_details:
        print(f"\n[DEBUG] PO Details Retrieved:")
        print(f"  PO Number: {po_details.get('purchaseOrderNumber')}")
        print(f"  PO State: {po_details.get('purchaseOrderState')}")
        
        order_details = po_details.get('orderDetails', {})
        items = order_details.get('items', [])
        print(f"  Total Items: {len(items)}")
        
        if items:
            first_item = items[0]
            print(f"\n[DEBUG] First Item Structure:")
            print(json.dumps(first_item, indent=2, default=str)[:1500])
            
            print(f"\n[DEBUG] All keys in first item:")
            print(sorted(first_item.keys()))
            
            # Check orderItemStatus structure
            item_status = first_item.get('orderItemStatus', {})
            if item_status:
                print(f"\n[DEBUG] orderItemStatus Structure:")
                print(json.dumps(item_status, indent=2, default=str)[:500])
                print(f"  Keys: {sorted(item_status.keys())}")
    else:
        print("[DEBUG] Failed to fetch PO details")
    
    # Also check shipments
    print(f"\n[DEBUG] Fetching Shipments for {PO_NUM}")
    shipments = _fetch_vendor_shipments_for_po(PO_NUM, marketplace)
    print(f"  Shipments by ASIN: {shipments}")
