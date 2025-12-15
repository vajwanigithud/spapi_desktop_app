from typing import Optional


def is_asin(value: str) -> bool:
    """
    Returns True if the string looks like an ASIN (10 alphanumeric chars).
    """
    if not value or not isinstance(value, str):
        return False
    v = value.strip()
    return len(v) == 10 and v.isalnum()


def is_valid_ean13(ean: str) -> bool:
    """
    Validate EAN-13 checksum.
    """
    if not ean or len(ean) != 13 or not ean.isdigit():
        return False
    digits = [int(c) for c in ean]
    checksum = digits[-1]
    total = sum(digits[i] * (1 if i % 2 == 0 else 3) for i in range(12))
    calc = (10 - (total % 10)) % 10
    return calc == checksum


def normalize_barcode(value: str) -> Optional[str]:
    """
    Normalize and validate a barcode string.

    Rules:
    - Strip whitespace.
    - Must be all digits.
    - If 13 digits: treat as EAN-13 (validate checksum if possible).
    - If 12 digits: treat as UPC-A and normalize by prefixing '0' to get 13-digit EAN.
    - Otherwise: return None.

    Returns 13-digit numeric EAN string if valid, otherwise None.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw.isdigit():
        return None

    if len(raw) == 12:
        raw = "0" + raw  # UPC-A → EAN-13
    elif len(raw) != 13:
        return None

    if not is_valid_ean13(raw):
        # If checksum fails, reject
        return None
    return raw
