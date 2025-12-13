import random
import win32print

# ----------------- SETTINGS -----------------
PRINTER_NAME = "ZDesigner TLP 2844 (Copy 1)"   # change if needed
DPI = 203                                     # most TLP 2844 are 203dpi

LABEL_W_MM = 38.0
LABEL_H_MM = 25.4

# ✅ tuned margins (more breathing space + lower print)
LEFT_MARGIN_MM = 3.0     # was 1.0  -> ~24 dots
TOP_MARGIN_MM  = 3.0     # was 1.0  -> ~24 dots

# ✅ barcode look (slightly bigger/taller)
BARCODE_HEIGHT_DOTS = 105   # was 90
NARROW_BAR_DOTS = 2         # thickness
WIDE_BAR_RATIO  = 6         # wide:narrow ratio

SKU_TEXT = "SKU-TEST-001"
# -------------------------------------------

def mm_to_dots(mm: float, dpi: int = DPI) -> int:
    return int(round(mm * dpi / 25.4))

def ean13_checksum(d12: str) -> str:
    # EAN-13 checksum from first 12 digits
    s = 0
    for i, ch in enumerate(d12):
        n = int(ch)
        s += n if (i % 2 == 0) else 3 * n
    check = (10 - (s % 10)) % 10
    return str(check)

def random_ean13(prefix: str = "629") -> str:
    # Make 12 digits then add checksum.
    body_len = 12 - len(prefix)
    d12 = prefix + "".join(str(random.randint(0, 9)) for _ in range(body_len))
    return d12 + ean13_checksum(d12)

def send_raw(printer_name: str, raw: str) -> None:
    hPrinter = win32print.OpenPrinter(printer_name)
    try:
        job = win32print.StartDocPrinter(hPrinter, 1, ("EPL EAN13 Test", None, "RAW"))
        win32print.StartPagePrinter(hPrinter)
        win32print.WritePrinter(hPrinter, raw.encode("ascii"))
        win32print.EndPagePrinter(hPrinter)
        win32print.EndDocPrinter(hPrinter)
    finally:
        win32print.ClosePrinter(hPrinter)

def build_epl(ean: str, sku: str) -> str:
    w = mm_to_dots(LABEL_W_MM)
    h = mm_to_dots(LABEL_H_MM)

    left = mm_to_dots(LEFT_MARGIN_MM)
    top  = mm_to_dots(TOP_MARGIN_MM)

    # Layout (tight + clean)
    barcode_x = left
    barcode_y = top

    # ✅ human readable DIRECTLY under barcode (tiny gap)
    HR_GAP_DOTS = 3
    text_y = barcode_y + BARCODE_HEIGHT_DOTS + HR_GAP_DOTS

    # ✅ SKU below human readable
    SKU_GAP_DOTS = 18
    sku_y = text_y + SKU_GAP_DOTS

    epl = ""
    epl += "N\n"
    epl += f"q{w}\n"
    epl += f"Q{h},{mm_to_dots(2.0)}\n"  # 2mm gap

    # Barcode (EAN-13 as E30 on many EPL firmwares)
    epl += (
        f'B{barcode_x},{barcode_y},0,E30,'
        f'{NARROW_BAR_DOTS},{WIDE_BAR_RATIO},{BARCODE_HEIGHT_DOTS},N,"{ean}"\n'
    )

    # Human readable number (font 2)
    epl += f'A{barcode_x},{text_y},0,2,1,1,N,"{ean}"\n'

    # SKU line (font 2)
    epl += f'A{barcode_x},{sku_y},0,2,1,1,N,"{sku}"\n'

    epl += "P1\n"
    return epl

if __name__ == "__main__":
    ean = random_ean13("629")
    epl = build_epl(ean, SKU_TEXT)
    print("Printing EAN:", ean)
    send_raw(PRINTER_NAME, epl)
    print("✅ Sent EPL job")
