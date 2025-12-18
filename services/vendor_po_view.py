# DB-FIRST: SQLite is the single source of truth.
# JSON files are debug/export only and must not be used for live state.

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _pick_first_int(*values: Any) -> Optional[int]:
    for value in values:
        parsed = _to_int(value)
        if parsed is not None:
            return parsed
    return None


def compute_po_status(header: Dict[str, Any], totals: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """
    Determine PO lifecycle state (OPEN / CLOSED / CANCELLED) based on header + line totals.
    Returns (status, reason) where reason is one of accepted_zero|remaining_positive|remaining_zero.
    """
    totals = totals or {}
    accepted_header = _pick_first_int(
        header.get("acceptedQty"),
        header.get("accepted_qty"),
    )
    accepted_totals = _pick_first_int(totals.get("accepted_qty"))
    accepted = accepted_header if accepted_header is not None else accepted_totals

    received = _pick_first_int(
        header.get("receivedQty"),
        header.get("received_qty"),
        totals.get("received_qty"),
    ) or 0
    cancelled = _pick_first_int(
        header.get("cancelledQty"),
        header.get("cancelled_qty"),
        totals.get("cancelled_qty"),
    ) or 0
    pending = _pick_first_int(
        header.get("remainingQty"),
        header.get("remaining_qty"),
        totals.get("pending_qty"),
    )
    ordered = _pick_first_int(
        header.get("requestedQty"),
        header.get("requested_qty"),
        totals.get("requested_qty"),
    )

    if accepted_header is not None and accepted_header == 0:
        return "CANCELLED", "accepted_zero"

    if pending is not None:
        remaining = max(0, pending)
        if remaining > 0:
            return "OPEN", "remaining_positive"
        return "CLOSED", "remaining_zero"

    base = accepted if accepted is not None else ordered
    if base is None:
        return "CLOSED", "remaining_zero"

    remaining = max(0, base - received - cancelled)
    if remaining > 0:
        return "OPEN", "remaining_positive"
    return "CLOSED", "remaining_zero"


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def compute_amount_reconciliation(line_total: Any, accepted_total: Any) -> Dict[str, float]:
    """
    Return rounded amounts + delta between computed line total and header accepted total.
    """
    line = _to_decimal(line_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    accepted = _to_decimal(accepted_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    delta = (line - accepted).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "line_total": float(line),
        "accepted_total": float(accepted),
        "delta": float(delta),
    }
