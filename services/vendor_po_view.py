# DB-FIRST: SQLite is the single source of truth.
# JSON files are debug/export only and must not be used for live state.

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        if isinstance(value, str) and not value.strip():
            return 0
        return int(float(value))
    except Exception:
        return 0


def _pick_int(*values: Any) -> int:
    for value in values:
        if value is not None:
            return _to_int(value)
    return 0


def compute_po_status(header: Dict[str, Any], totals: Optional[Dict[str, Any]] = None) -> str:
    """
    Determine PO lifecycle state (OPEN / CLOSED / CANCELLED) based on header + line totals.
    """
    totals = totals or {}
    accepted = _pick_int(
        totals.get("accepted_qty"),
        header.get("acceptedQty"),
        header.get("accepted_qty"),
    )
    received = _pick_int(
        totals.get("received_qty"),
        header.get("receivedQty"),
        header.get("received_qty"),
    )
    cancelled = _pick_int(
        totals.get("cancelled_qty"),
        header.get("cancelledQty"),
        header.get("cancelled_qty"),
    )
    pending = _pick_int(
        totals.get("pending_qty"),
        header.get("remainingQty"),
        header.get("remaining_qty"),
    )

    remaining = pending if pending else max(0, accepted - received - cancelled)

    if accepted <= 0:
        return "CANCELLED"
    if cancelled >= accepted and remaining <= 0:
        return "CANCELLED"
    if remaining > 0:
        return "OPEN"
    return "CLOSED"


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
