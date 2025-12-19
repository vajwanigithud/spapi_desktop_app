"""Catalog image enrichment helpers.

Provides lightweight, read-only helpers to fetch image URLs for ASINs
and attach them to result rows without mutating existing values.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Set

from services.db import get_db_connection


def get_image_url_map(asins: Set[str]) -> Dict[str, str]:
    """Return a mapping of ASIN -> image_url for the provided ASINs.

    Prefers the `image` column when non-empty; otherwise attempts to extract
    an image URL from the JSON `payload`, favoring MAIN variants. Returns an
    empty dict when no ASINs are provided or no images are found.
    """
    normalized = {asin.strip() for asin in asins if isinstance(asin, str) and asin.strip()}
    if not normalized:
        return {}

    placeholders = ",".join(["?"] * len(normalized))
    query = f"""
        SELECT asin, image, payload
        FROM spapi_catalog
        WHERE asin IN ({placeholders})
    """

    with get_db_connection() as conn:
        rows = conn.execute(query, tuple(normalized)).fetchall()

    result: Dict[str, str] = {}
    for row in rows:
        asin = row["asin"]
        if not asin:
            continue
        image_url = _extract_image_from_row(row)
        if image_url:
            result[asin] = image_url

    return result


def _extract_image_from_row(row) -> Optional[str]:
    """Return best image URL from a catalog row.

    Prefers non-empty `image`; otherwise attempts to parse payload JSON and
    select MAIN variant, falling back to first available link.
    """
    image = _safe_get(row, "image")
    if isinstance(image, str) and image.strip():
        return image.strip()

    payload = _safe_get(row, "payload")
    if not isinstance(payload, str) or not payload.strip():
        return None

    try:
        data = json.loads(payload)
    except Exception:
        return None

    images_root = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images_root, list):
        return None

    first_link: Optional[str] = None
    for marketplace_entry in images_root:
        images_list = marketplace_entry.get("images") if isinstance(marketplace_entry, dict) else None
        if not isinstance(images_list, list):
            continue
        for image_entry in images_list:
            if not isinstance(image_entry, dict):
                continue
            link = image_entry.get("link")
            if isinstance(link, str) and link.strip():
                link = link.strip()
                if first_link is None:
                    first_link = link
                variant = image_entry.get("variant")
                if isinstance(variant, str) and variant.upper() == "MAIN":
                    return link

    return first_link


def _safe_get(row, key: str):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def attach_image_urls(rows: List[dict], asin_key: str = "asin") -> List[dict]:
    """Attach imageUrl to rows using catalog lookup.

    - Collects ASINs from rows[asin_key]
    - Fetches a map via get_image_url_map
    - Sets row["imageUrl"] only when missing/falsey and a catalog image exists
    - Does not overwrite existing imageUrl
    - Safe on empty input or rows without the ASIN key
    """
    if not rows:
        return rows

    asins = {str(row.get(asin_key)).strip() for row in rows if row.get(asin_key)}
    image_map = get_image_url_map(asins)

    for row in rows:
        asin = row.get(asin_key)
        if not asin:
            continue
        if row.get("imageUrl"):
            continue
        image_url = image_map.get(str(asin).strip())
        if image_url:
            row["imageUrl"] = image_url

    return rows
