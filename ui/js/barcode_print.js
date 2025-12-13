function isValidEan(value) {
  const digits = (value || "").replace(/[^0-9]/g, "").trim();
  return digits.length === 12 || digits.length === 13;
}

function clampCopies(value) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed)) {
    return 1;
  }
  return Math.min(10, Math.max(1, parsed));
}

async function barcodePrintFromRow(btn) {
  if (!btn) {
    return;
  }

  const row = btn.closest("tr");
  if (!row) {
    alert("Unable to locate catalog row data for printing.");
    return;
  }

  let ean = (row.dataset.ean || "").trim();
  let sku = (row.dataset.sku || "").trim();

  if (!ean) {
    const fallbackCell = row.cells[2];
    if (fallbackCell) {
      ean = fallbackCell.textContent?.replace(/[^0-9]/g, "").trim() || "";
    }
  }
  if (!sku) {
    sku = (row.cells[1]?.textContent || "").trim();
  }

  if (!isValidEan(ean) || !sku) {
    alert("❌ Missing/invalid EAN or SKU.");
    return;
  }

  const parsedCopies = 1;

  console.log("Printing", { ean, sku, copies: parsedCopies });

  try {
    const resp = await fetch("/api/barcode/print", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ean, sku, copies: parsedCopies }),
    });

    let bodyJson = null;
    let fallbackText = "";
    try {
      bodyJson = await resp.json();
    } catch {
      fallbackText = await resp.text().catch(() => "");
    }

    if (!resp.ok) {
      const detail =
        (bodyJson && (bodyJson.detail || bodyJson.message || bodyJson.error)) ||
        fallbackText ||
        resp.statusText ||
        `HTTP ${resp.status}`;
      alert(`❌ Print failed: ${detail}`);
      return;
    }

    alert("✅ Printed");
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    alert(`❌ Print failed: ${message}`);
  }
}
