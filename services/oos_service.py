import logging
from typing import Any, Dict, List

from services import json_cache, db_repos

logger = logging.getLogger(__name__)


def upsert_oos_entry(
    state: Dict[str, Any],
    *,
    po_number: str,
    asin: str,
    vendor_sku: str | None = None,
    po_date: str | None = None,
    ship_to_party: str | None = None,
    qty: Any = None,
    image: str | None = None,
) -> bool:
    if not po_number or not asin:
        return False
    try:
        if qty is not None and float(qty) <= 0:
            return False
    except Exception:
        pass
    key = f"{po_number}::{asin}"
    if key in state:
        return False
    state[key] = {
        "poNumber": po_number,
        "asin": asin,
        "vendorSku": vendor_sku,
        "purchaseOrderDate": po_date,
        "shipToPartyId": ship_to_party,
        "qty": qty,
        "image": image,
        "isOutOfStock": True,
    }
    return True


def seed_oos_from_rejected_lines(
    po_numbers: List[str],
    po_date_map: Dict[str, str] | None = None,
) -> int:
    if not po_numbers:
        return 0
    state = json_cache.load_oos_state()
    added = 0
    try:
        rows = db_repos.get_rejected_vendor_po_lines(po_numbers)
        for row in rows:
            po_num = (row.get("po_number") or "").strip()
            asin_val = (row.get("asin") or "").strip()
            if not po_num or not asin_val:
                continue
            ordered_qty = row.get("ordered_qty") or 0
            cancelled_qty = row.get("cancelled_qty") or 0
            accepted_qty = row.get("accepted_qty") or 0
            is_rejected = cancelled_qty >= ordered_qty or (accepted_qty <= 0 and cancelled_qty > 0)
            if not is_rejected:
                continue
            qty_val = ordered_qty or cancelled_qty or None
            try:
                if qty_val is not None and float(qty_val) <= 0:
                    continue
            except Exception:
                pass
            if upsert_oos_entry(
                state,
                po_number=po_num,
                asin=asin_val,
                vendor_sku=(row.get("sku") or "").strip() or None,
                po_date=(po_date_map or {}).get(po_num),
                ship_to_party=(row.get("ship_to_location") or "").strip() or None,
                qty=qty_val,
                image=None,
            ):
                added += 1
        if added:
            json_cache.save_oos_state(state)
    except Exception as exc:
        logger.warning(f"[OOS] Failed to seed rejected lines into OOS: {exc}")
    return added


def seed_oos_from_rejected_payload(purchase_orders: List[Dict[str, Any]]) -> int:
    if not purchase_orders:
        return 0
    state = json_cache.load_oos_state()
    added = 0

    for po in purchase_orders:
        po_num = po.get("purchaseOrderNumber") or ""
        d = po.get("orderDetails") or {}
        po_date = po.get("purchaseOrderDate") or d.get("purchaseOrderDate")
        ship_to = d.get("shipToParty", {}).get("partyId") if isinstance(d.get("shipToParty"), dict) else None
        items = d.get("items") or []
        for it in items:
            ack = it.get("acknowledgementStatus") or {}
            conf = (ack.get("confirmationStatus") or it.get("status") or "").upper()
            if conf != "REJECTED":
                continue
            asin = it.get("amazonProductIdentifier") or ""
            if not po_num or not asin:
                continue
            sku = it.get("vendorProductIdentifier") or ""
            qty_raw = it.get("orderedQuantity") or {}
            qty_num = None
            try:
                qty_num = float(qty_raw.get("amount"))
            except Exception:
                qty_num = None
            try:
                if qty_num is not None and qty_num <= 0:
                    continue
            except Exception:
                pass
            if upsert_oos_entry(
                state,
                po_number=po_num,
                asin=asin,
                vendor_sku=sku or None,
                po_date=po_date,
                ship_to_party=ship_to,
                qty=qty_num,
                image=None,
            ):
                added += 1
    if added:
        json_cache.save_oos_state(state)
    return added
