"""Locked EPL label builder for hardcoded units."""

from typing import List

DPI = 203
LABEL_WIDTH_MM = 38.0
LABEL_HEIGHT_MM = 25.4
X_START = 24
Y_START = 24
BARCODE_HEIGHT_DOTS = 105
HR_Y = 132
SKU_Y = 150
GAP_DOTS = 16
NARROW_BAR_DOTS = 2
WIDE_BAR_RATIO = 6


def _mm_to_dots(value: float) -> int:
    """Convert millimeters to printer dots at the assumed DPI."""
    return int(round(value / 25.4 * DPI))


def build_epl_ean13_label(ean: str, sku: str, copies: int = 1) -> str:
    """
    Build a single-print EPL template for a 38x25.4mm label with fixed text placement.
    """
    width_dots = _mm_to_dots(LABEL_WIDTH_MM)
    height_dots = _mm_to_dots(LABEL_HEIGHT_MM)

    lines: List[str] = [
        "N",
        f"q{width_dots}",
        f"Q{height_dots},{GAP_DOTS}",
        f'B{X_START},{Y_START},0,E30,{NARROW_BAR_DOTS},{WIDE_BAR_RATIO},{BARCODE_HEIGHT_DOTS},N,"{ean}"',
        f'A{X_START},{HR_Y},0,2,1,1,N,"{ean}"',
        f'A{X_START},{SKU_Y},0,2,1,1,N,"{sku}"',
        f"P{copies}",
    ]

    return "\n".join(lines) + "\n"
