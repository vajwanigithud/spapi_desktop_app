#!/usr/bin/env python
"""Manual debug script for host resolution tests; not part of the main app."""

import sys

sys.path.insert(0, '.')

from main import EU_MARKETPLACE_IDS, resolve_catalog_host, resolve_vendor_host


def test_resolve_catalog_host():
    """Test that resolve_catalog_host correctly routes UAE to EU endpoint."""
    
    # Test UAE marketplace
    uae_catalog_host = resolve_catalog_host('A2VIGQ35RCS4UG')
    uae_vendor_host = resolve_vendor_host('A2VIGQ35RCS4UG')
    
    print("=" * 60)
    print("TEST: resolve_catalog_host Implementation")
    print("=" * 60)
    
    print("\n1. UAE Marketplace (A2VIGQ35RCS4UG):")
    print(f"   Catalog Host: {uae_catalog_host}")
    print(f"   Vendor Host:  {uae_vendor_host}")
    
    assert uae_catalog_host == uae_vendor_host, \
        f"Mismatch: catalog={uae_catalog_host}, vendor={uae_vendor_host}"
    print("   ✓ Hosts match")
    
    assert uae_catalog_host == "https://sellingpartnerapi-eu.amazon.com", \
        f"Expected EU endpoint, got {uae_catalog_host}"
    print("   ✓ Correctly routes to EU endpoint")
    
    # Verify UAE is in EU_MARKETPLACE_IDS
    assert 'A2VIGQ35RCS4UG' in EU_MARKETPLACE_IDS, \
        "UAE not in EU_MARKETPLACE_IDS"
    print("   ✓ UAE in EU_MARKETPLACE_IDS")
    
    # Test a different EU marketplace (UK)
    uk_host = resolve_catalog_host('A1PA6795UKMFR9')
    print("\n2. UK Marketplace (A1PA6795UKMFR9):")
    print(f"   Catalog Host: {uk_host}")
    assert uk_host == "https://sellingpartnerapi-eu.amazon.com", \
        f"Expected EU endpoint for UK, got {uk_host}"
    print("   ✓ Correctly routes to EU endpoint")
    
    # Test NA marketplace
    na_host = resolve_catalog_host('A1AM78C64UHY11')
    print("\n3. NA Marketplace (A1AM78C64UHY11):")
    print(f"   Catalog Host: {na_host}")
    assert na_host == "https://sellingpartnerapi-na.amazon.com", \
        f"Expected NA endpoint, got {na_host}"
    print("   ✓ Correctly routes to NA endpoint")
    
    # Test FE marketplace (JP)
    fe_host = resolve_catalog_host('A1VC38T7YXB528')
    print("\n4. FE Marketplace (A1VC38T7YXB528):")
    print(f"   Catalog Host: {fe_host}")
    assert fe_host == "https://sellingpartnerapi-fe.amazon.com", \
        f"Expected FE endpoint, got {fe_host}"
    print("   ✓ Correctly routes to FE endpoint")
    
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✓")
    print("=" * 60)

if __name__ == "__main__":
    test_resolve_catalog_host()
