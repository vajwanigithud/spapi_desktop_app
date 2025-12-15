"""Tests for main.py helper functions."""


def test_resolve_catalog_host_uae_returns_eu():
    """UAE marketplace (A2VIGQ35RCS4UG) must use the EU endpoint."""
    from main import resolve_catalog_host
    
    result = resolve_catalog_host("A2VIGQ35RCS4UG")
    assert result == "https://sellingpartnerapi-eu.amazon.com"


def test_resolve_catalog_host_eu_marketplaces():
    """EU marketplaces should use the EU endpoint."""
    from main import resolve_catalog_host
    
    eu_marketplaces = [
        "A2VIGQ35RCS4UG",  # UAE
        "A1PA6795UKMFR9",  # DE
        "A13V1IB3VIYZZH",  # ES
        "A1RKKUPIHCS9HS",  # ES
        "A1F83G8C2ARO7P",  # UK
    ]
    for marketplace_id in eu_marketplaces:
        result = resolve_catalog_host(marketplace_id)
        assert result == "https://sellingpartnerapi-eu.amazon.com", f"Failed for {marketplace_id}"


def test_resolve_catalog_host_jp_returns_fe():
    """JP marketplace should use the FE endpoint."""
    from main import resolve_catalog_host
    
    result = resolve_catalog_host("A1VC38T7YXB528")
    assert result == "https://sellingpartnerapi-fe.amazon.com"


def test_resolve_catalog_host_us_returns_na():
    """US marketplace should use the NA endpoint."""
    from main import resolve_catalog_host
    
    result = resolve_catalog_host("ATVPDKIKX0DER")
    assert result == "https://sellingpartnerapi-na.amazon.com"


def test_resolve_catalog_host_matches_vendor_host():
    """resolve_catalog_host should return same region as resolve_vendor_host."""
    from main import resolve_catalog_host, resolve_vendor_host
    
    test_marketplaces = [
        "A2VIGQ35RCS4UG",  # UAE
        "A1PA6795UKMFR9",  # DE
        "A1VC38T7YXB528",  # JP
        "ATVPDKIKX0DER",   # US (not in EU/FE, should default to NA)
    ]
    for marketplace_id in test_marketplaces:
        catalog_host = resolve_catalog_host(marketplace_id)
        vendor_host = resolve_vendor_host(marketplace_id)
        assert catalog_host == vendor_host, f"Mismatch for {marketplace_id}: catalog={catalog_host}, vendor={vendor_host}"
