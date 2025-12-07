"""
Preset metadata for SP-API Endpoint Tester (GET/POST focused on vendor-related APIs).
"""

ENDPOINT_PRESETS = [
    # -------------------------------
    # Vendor – Retail Procurement
    # -------------------------------
    {
        "id": "vendor_orders_list",
        "group": "Vendor – Retail Procurement",
        "label": "Vendor Orders – list",
        "method": "GET",
        "path": "/vendor/orders/v1/purchaseOrders",
        "default_query": "marketplaceIds=A2VIGQ35RCS4UG&limit=10",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "vendor_orders_get",
        "group": "Vendor – Retail Procurement",
        "label": "Vendor Orders – by PO",
        "method": "GET",
        "path": "/vendor/orders/v1/purchaseOrders/{purchaseOrderNumber}",
        "default_query": "marketplaceIds=A2VIGQ35RCS4UG",
        "default_body": None,
        "notes": "Replace {purchaseOrderNumber}",
    },
    {
        "id": "vendor_orders_ack",
        "group": "Vendor – Retail Procurement",
        "label": "Vendor Orders – acknowledge",
        "method": "POST",
        "path": "/vendor/orders/v1/acknowledgements",
        "default_query": "",
        "default_body": {
            "acknowledgements": [
                {
                    "purchaseOrderNumber": "{purchaseOrderNumber}",
                    "acknowledgementDate": "2025-01-01T00:00:00Z",
                    "acknowledgementStatus": "Accepted",
                    "sellingParty": {"partyId": "VENDORID"},
                    "shipFromParty": {"partyId": "WAREHOUSEID"},
                }
            ]
        },
        "notes": "Replace placeholders with real PO/party IDs",
    },
    {
        "id": "vendor_orders_status",
        "group": "Vendor – Retail Procurement",
        "label": "Vendor Orders – status list",
        "method": "GET",
        "path": "/vendor/orders/v1/purchaseOrdersStatus",
        "default_query": "marketplaceIds=A2VIGQ35RCS4UG&limit=10",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "vendor_shipments_confirm",
        "group": "Vendor – Retail Procurement",
        "label": "Vendor Shipments – confirmations",
        "method": "POST",
        "path": "/vendor/shipping/v1/shipmentConfirmations",
        "default_query": "",
        "default_body": {
            "shipmentConfirmations": [
                {
                    "shipmentIdentifier": "TEST-SHIPMENT",
                    "transactionType": "New",
                    "purchaseOrders": [
                        {
                            "purchaseOrderNumber": "{purchaseOrderNumber}",
                            "items": [
                                {
                                    "itemSequenceNumber": "1",
                                    "buyerProductIdentifier": "{asin}",
                                    "shippedQuantity": {"amount": 1, "unitOfMeasure": "Each"},
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        "notes": "Replace purchaseOrderNumber and identifiers",
    },
    {
        "id": "vendor_invoices_submit",
        "group": "Vendor – Retail Procurement",
        "label": "Vendor Invoices – submit",
        "method": "POST",
        "path": "/vendor/payments/v1/invoices",
        "default_query": "",
        "default_body": {
            "invoices": [
                {
                    "invoiceType": "Invoice",
                    "invoiceNumber": "INV-TEST",
                    "invoiceDate": "2025-01-01T00:00:00Z",
                    "remitToParty": {"partyId": "VENDORID"},
                    "sellingParty": {"partyId": "VENDORID"},
                    "shipmentIdentifiers": ["TEST-SHIPMENT"],
                }
            ]
        },
        "notes": "Replace IDs with valid values",
    },
    {
        "id": "vendor_transactions_get",
        "group": "Vendor – Retail Procurement",
        "label": "Vendor Transactions – status",
        "method": "GET",
        "path": "/vendor/transactions/v1/transactions/{transactionId}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {transactionId}",
    },
    # -------------------------------
    # Vendor – Direct Fulfillment
    # -------------------------------
    {
        "id": "df_orders_list",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Orders – list",
        "method": "GET",
        "path": "/vendor/directFulfillment/orders/v1/purchaseOrders",
        "default_query": "limit=10",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "df_orders_get",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Order – single",
        "method": "GET",
        "path": "/vendor/directFulfillment/orders/v1/purchaseOrders/{purchaseOrderNumber}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {purchaseOrderNumber}",
    },
    {
        "id": "df_orders_ack",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Orders – acknowledge",
        "method": "POST",
        "path": "/vendor/directFulfillment/orders/v1/acknowledgements",
        "default_query": "",
        "default_body": {
            "acknowledgementStatusDetails": [
                {
                    "purchaseOrderNumber": "{purchaseOrderNumber}",
                    "acknowledgementStatus": "Accepted",
                }
            ]
        },
        "notes": "Replace {purchaseOrderNumber}",
    },
    {
        "id": "df_shipping_labels_list",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – shippingLabels list",
        "method": "GET",
        "path": "/vendor/directFulfillment/shipping/v1/shippingLabels",
        "default_query": "limit=10",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "df_shipping_labels_request",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – request shippingLabel",
        "method": "POST",
        "path": "/vendor/directFulfillment/shipping/v1/shippingLabels",
        "default_query": "",
        "default_body": {
            "purchaseOrderNumber": "{purchaseOrderNumber}",
            "sellingParty": {"partyId": "VENDORID"},
            "shipFromParty": {"partyId": "WAREHOUSEID"},
        },
        "notes": "Replace IDs/placeholders",
    },
    {
        "id": "df_shipping_label_by_po",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – shippingLabel by PO",
        "method": "GET",
        "path": "/vendor/directFulfillment/shipping/v1/shippingLabels/{purchaseOrderNumber}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {purchaseOrderNumber}",
    },
    {
        "id": "df_ship_confirms",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – shipmentConfirmations",
        "method": "POST",
        "path": "/vendor/directFulfillment/shipping/v1/shipmentConfirmations",
        "default_query": "",
        "default_body": {
            "shipmentConfirmations": [
                {
                    "purchaseOrderNumber": "{purchaseOrderNumber}",
                    "shipFromParty": {"partyId": "WAREHOUSEID"},
                    "packages": [],
                }
            ]
        },
        "notes": "Replace {purchaseOrderNumber}",
    },
    {
        "id": "df_ship_status_updates",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – shipmentStatusUpdates",
        "method": "POST",
        "path": "/vendor/directFulfillment/shipping/v1/shipmentStatusUpdates",
        "default_query": "",
        "default_body": {
            "shipmentStatusUpdates": [
                {
                    "purchaseOrderNumber": "{purchaseOrderNumber}",
                    "sellingParty": {"partyId": "VENDORID"},
                    "shipFromParty": {"partyId": "WAREHOUSEID"},
                    "packages": [],
                }
            ]
        },
        "notes": "Replace placeholders",
    },
    {
        "id": "df_customer_invoices",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – customerInvoices list",
        "method": "GET",
        "path": "/vendor/directFulfillment/shipping/v1/customerInvoices",
        "default_query": "limit=10",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "df_customer_invoice_by_po",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – customerInvoice by PO",
        "method": "GET",
        "path": "/vendor/directFulfillment/shipping/v1/customerInvoices/{purchaseOrderNumber}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {purchaseOrderNumber}",
    },
    {
        "id": "df_packing_slips",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – packingSlips list",
        "method": "GET",
        "path": "/vendor/directFulfillment/shipping/v1/packingSlips",
        "default_query": "limit=10",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "df_packing_slip_by_po",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Shipping – packingSlip by PO",
        "method": "GET",
        "path": "/vendor/directFulfillment/shipping/v1/packingSlips/{purchaseOrderNumber}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {purchaseOrderNumber}",
    },
    {
        "id": "df_inventory_submit",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Inventory – submit inventory update",
        "method": "POST",
        "path": "/vendor/directFulfillment/inventory/v1/warehouses/{warehouseId}/items",
        "default_query": "",
        "default_body": {"items": []},
        "notes": "Replace {warehouseId} and items",
    },
    {
        "id": "df_payments_invoice",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Payments – submit invoice",
        "method": "POST",
        "path": "/vendor/directFulfillment/payments/v1/invoices",
        "default_query": "",
        "default_body": {"invoices": []},
        "notes": "Supply valid invoices payload",
    },
    {
        "id": "df_transactions_get",
        "group": "Vendor – Direct Fulfillment",
        "label": "DF Transactions – status",
        "method": "GET",
        "path": "/vendor/directFulfillment/transactions/v1/transactions/{transactionId}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {transactionId}",
    },
    # -------------------------------
    # Catalog
    # -------------------------------
    {
        "id": "catalog_search_latest",
        "group": "Catalog",
        "label": "Catalog search (2022-04-01)",
        "method": "GET",
        "path": "/catalog/2022-04-01/items",
        "default_query": "marketplaceIds=A2VIGQ35RCS4UG&keywords=candle&includedData=attributes,images,summaries",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "catalog_by_asin_latest",
        "group": "Catalog",
        "label": "Catalog by ASIN (2022-04-01)",
        "method": "GET",
        "path": "/catalog/2022-04-01/items/{asin}",
        "default_query": "marketplaceIds=A2VIGQ35RCS4UG",
        "default_body": None,
        "notes": "Replace {asin}",
    },
    {
        "id": "catalog_by_sku_latest",
        "group": "Catalog",
        "label": "Catalog search by SKU (2022-04-01)",
        "method": "GET",
        "path": "/catalog/2022-04-01/items",
        "default_query": "marketplaceIds=A2VIGQ35RCS4UG&identifiers=YOUR_SKU_HERE&identifiersType=SKU",
        "default_body": None,
        "notes": "Replace YOUR_SKU_HERE",
    },
    # -------------------------------
    # Reports
    # -------------------------------
    {
        "id": "reports_create_vendor_invoice",
        "group": "Reports",
        "label": "Create Vendor Invoice Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_INVOICE_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    {
        "id": "reports_create_vendor_sales",
        "group": "Reports",
        "label": "Create Vendor Sales Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_SALES_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    {
        "id": "reports_get_status",
        "group": "Reports",
        "label": "Report status by ID",
        "method": "GET",
        "path": "/reports/2021-06-30/reports/{reportId}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {reportId}",
    },
    {
        "id": "reports_get_document",
        "group": "Reports",
        "label": "Report document by ID",
        "method": "GET",
        "path": "/reports/2021-06-30/documents/{documentId}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {documentId}",
    },
    {
        "id": "reports_vendor_sales_diagnostics",
        "group": "Reports",
        "label": "Create Vendor Sales Diagnostics Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_SALES_DIAGNOSTICS_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    {
        "id": "reports_vendor_traffic",
        "group": "Reports",
        "label": "Create Vendor Traffic Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_TRAFFIC_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    {
        "id": "reports_vendor_margin",
        "group": "Reports",
        "label": "Create Vendor Net Pure Product Margin Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_NET_PURE_PRODUCT_MARGIN_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    {
        "id": "reports_vendor_inventory",
        "group": "Reports",
        "label": "Create Vendor Inventory Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_INVENTORY_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    {
        "id": "reports_vendor_df_shipments",
        "group": "Reports",
        "label": "Create DF Shipments Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_DIRECT_FULFILLMENT_SHIPMENTS_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    {
        "id": "reports_vendor_df_orders",
        "group": "Reports",
        "label": "Create DF Orders Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_DIRECT_FULFILLMENT_ORDERS_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    {
        "id": "reports_vendor_df_invoices",
        "group": "Reports",
        "label": "Create DF Invoices Report",
        "method": "POST",
        "path": "/reports/2021-06-30/reports",
        "default_query": "",
        "default_body": {"reportType": "GET_VENDOR_DIRECT_FULFILLMENT_INVOICES_REPORT", "marketplaceIds": ["A2VIGQ35RCS4UG"]},
        "notes": "",
    },
    # -------------------------------
    # Notifications, Tokens, Sellers, Uploads
    # -------------------------------
    {
        "id": "notifications_destinations_get",
        "group": "Notifications",
        "label": "Get destinations",
        "method": "GET",
        "path": "/notifications/v1/destinations",
        "default_query": "",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "notifications_destinations_post",
        "group": "Notifications",
        "label": "Create destination",
        "method": "POST",
        "path": "/notifications/v1/destinations",
        "default_query": "",
        "default_body": {
            "name": "MyDestination",
            "resourceSpecification": {"sqs": {"arn": "arn:aws:sqs:region:acct:queue"}},
        },
        "notes": "Replace ARN with valid value",
    },
    {
        "id": "notifications_subscriptions_get",
        "group": "Notifications",
        "label": "Get subscription",
        "method": "GET",
        "path": "/notifications/v1/subscriptions/{notificationType}",
        "default_query": "",
        "default_body": None,
        "notes": "Replace {notificationType}",
    },
    {
        "id": "notifications_subscriptions_post",
        "group": "Notifications",
        "label": "Create subscription",
        "method": "POST",
        "path": "/notifications/v1/subscriptions/{notificationType}",
        "default_query": "",
        "default_body": {
            "payloadVersion": "1.0",
            "destinationId": "{destinationId}",
        },
        "notes": "Replace {notificationType} and destinationId",
    },
    {
        "id": "tokens_rdt",
        "group": "Tokens",
        "label": "Create Restricted Data Token",
        "method": "POST",
        "path": "/tokens/2021-03-01/restrictedDataToken",
        "default_query": "",
        "default_body": {
            "restrictedResources": [
                {
                    "method": "GET",
                    "path": "/vendor/directFulfillment/orders/v1/purchaseOrders",
                    "dataElements": [],
                }
            ]
        },
        "notes": "Adjust restrictedResources as needed",
    },
    {
        "id": "sellers_marketplace_participations",
        "group": "Sellers",
        "label": "Marketplace participations",
        "method": "GET",
        "path": "/sellers/v1/marketplaceParticipations",
        "default_query": "",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "uploads_destination",
        "group": "Uploads",
        "label": "Create upload destination",
        "method": "POST",
        "path": "/uploads/2020-11-01/uploadDestinations/{resource}",
        "default_query": "",
        "default_body": {
            "contentType": "text/plain; charset=UTF-8"
        },
        "notes": "Replace {resource}",
    },
    # -------------------------------
    # Optional – Advanced
    # -------------------------------
    {
        "id": "definitions_product_types",
        "group": "Optional – Advanced",
        "label": "Definitions – product types",
        "method": "GET",
        "path": "/definitions/2020-09-01/productTypes",
        "default_query": "marketplaceIds=A2VIGQ35RCS4UG",
        "default_body": None,
        "notes": "",
    },
    {
        "id": "definitions_product_type",
        "group": "Optional – Advanced",
        "label": "Definitions – product type by name",
        "method": "GET",
        "path": "/definitions/2020-09-01/productTypes/{productType}",
        "default_query": "marketplaceIds=A2VIGQ35RCS4UG&productTypeVersion=LATEST",
        "default_body": None,
        "notes": "Replace {productType}",
    },
    {
        "id": "listings_restrictions",
        "group": "Optional – Advanced",
        "label": "Listings restrictions",
        "method": "GET",
        "path": "/listings/2021-08-01/restrictions",
        "default_query": "asin={asin}&marketplaceIds=A2VIGQ35RCS4UG",
        "default_body": None,
        "notes": "Replace {asin}",
    },
    {
        "id": "fees_estimate",
        "group": "Optional – Advanced",
        "label": "Products fees estimate",
        "method": "POST",
        "path": "/products/fees/v0/listings/feesEstimate",
        "default_query": "",
        "default_body": {
            "FeesEstimateRequest": {
                "MarketplaceId": "A2VIGQ35RCS4UG",
                "IsAmazonFulfilled": False,
                "Identifier": "Request-1",
                "PriceToEstimateFees": {
                    "ListingPrice": {"CurrencyCode": "GBP", "Amount": 10.0},
                    "Shipping": {"CurrencyCode": "GBP", "Amount": 0.0},
                },
                "IdentifierType": "ASIN",
                "IdType": "ASIN",
                "IdValue": "{asin}",
            }
        },
        "notes": "Replace {asin}",
    },
]
