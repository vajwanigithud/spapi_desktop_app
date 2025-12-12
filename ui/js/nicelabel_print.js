async function nicelabelPrint({ ean, sku, copies = 1 }) {
  const payload = {
    ean: (ean || "").trim(),
    sku: (sku || "").trim(),
    copies: copies || 1,
  };
  if (!payload.ean) {
    throw new Error("Missing EAN");
  }
  try {
    const resp = await fetch("/nicelabel/print", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(text || `HTTP ${resp.status}`);
    }
    if (typeof showToast === "function") {
      showToast("Sent to NiceLabel ✅");
    } else {
      alert("Sent to NiceLabel ✅");
    }
    return resp.json().catch(() => ({}));
  } catch (err) {
    throw err instanceof Error ? err : new Error(err || "NiceLabel print failed");
  }
}

async function nicelabelPrintFromRow(el) {
  if (!el) return;
  const row = el.closest("tr");
  if (!row) {
    alert("Unable to locate row data for NiceLabel print.");
    return;
  }
  let ean = (row.dataset.ean || "").trim();
  const sku = (row.dataset.sku || "").trim();
  const promptIfMissing = row.dataset.promptIfMissing === "true";
  if (!ean && promptIfMissing) {
    const entered = prompt("EAN missing for this line. Enter EAN to print:");
    if (!entered) return;
    const cleaned = (entered || "").replace(/[^0-9]/g, "");
    if (!/^\d{12,13}$/.test(cleaned)) {
      alert("EAN must be 12 or 13 digits.");
      return;
    }
    ean = cleaned;
  }
  if (!ean) {
    alert("Missing EAN for this item.");
    return;
  }
  try {
    await nicelabelPrint({ ean, sku, copies: 1 });
  } catch (err) {
    const message = err instanceof Error ? err.message : err || "NiceLabel print failed";
    alert(`NiceLabel print failed: ${message}`);
  }
}
