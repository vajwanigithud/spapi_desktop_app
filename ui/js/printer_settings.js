(function () {
  const DEFAULT_LABEL_VALUES = {
    label_width_mm: 38,
    label_height_mm: 25.4,
    gap_mm: 2,
  };

  const IDS = {
    modal: "printer-settings-modal",
    openButton: "printer-settings-btn",
    closeButton: "printer-settings-close-btn",
    select: "printer-settings-select",
    width: "printer-setting-label_width_mm",
    height: "printer-setting-label_height_mm",
    gap: "printer-setting-gap_mm",
    warning: "printer-settings-warning",
    status: "printer-settings-status",
    save: "printer-settings-save-btn",
    test: "printer-test-print-btn",
    health: "printer-health-indicator",
    recentList: "recent-prints-list",
    recentRefresh: "recent-prints-refresh-btn",
  };

  function getElement(id) {
    const el = document.getElementById(id);
    if (!el) {
      console.error(`[printer_settings] missing element: #${id}`);
    }
    return el;
  }

  function setStatusText(el, text, tone = "info") {
    if (!el) return;
    el.textContent = text || "";
    switch (tone) {
      case "success":
        el.style.color = "#059669";
        break;
      case "error":
        el.style.color = "#b91c1c";
        break;
      default:
        el.style.color = "#374151";
    }
  }

  async function fetchJson(path) {
    const response = await fetch(path);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  }

  async function loadPrinterDefaults(selectEl, widthEl, heightEl, gapEl, statusEl) {
    try {
      const defaults = await fetchJson("/api/printers/default");
      widthEl.value = defaults.label_width_mm ?? DEFAULT_LABEL_VALUES.label_width_mm;
      heightEl.value = defaults.label_height_mm ?? DEFAULT_LABEL_VALUES.label_height_mm;
      gapEl.value = defaults.gap_mm ?? DEFAULT_LABEL_VALUES.gap_mm;
      setStatusText(statusEl, "Printer settings loaded.", "success");
      const savedPrinter = (
        defaults.default_printer_name ||
        defaults.selected_printer ||
        ""
      ).trim();
      return savedPrinter;
    } catch (err) {
      console.error("[printer_settings] load defaults failed", err);
      setStatusText(statusEl, "Unable to load printer defaults.", "error");
      return "";
    }
  }

  async function loadPrinters(selectEl, preferred) {
    if (!selectEl) return "";
    selectEl.disabled = true;
    selectEl.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "-- Select printer --";
    selectEl.appendChild(placeholder);
    try {
      const payload = await fetchJson("/api/printers");
      const printers = Array.isArray(payload.printers) ? payload.printers : [];
      const virtualPattern = /(OneNote|PDF|XPS|Fax)/i;
      const physical = printers.filter((n) => !virtualPattern.test(n));
      const virtual = printers.filter((n) => virtualPattern.test(n));
      const ordered = physical.concat(virtual);
      ordered.forEach((printer) => {
        if (!printer) return;
        const option = document.createElement("option");
        option.value = printer;
        option.textContent = printer;
        selectEl.appendChild(option);
      });
      if (preferred && preferred.length && !ordered.includes(preferred)) {
        const missing = document.createElement("option");
        missing.value = preferred;
        missing.textContent = `${preferred} (saved)`;
        selectEl.appendChild(missing);
      }
      selectEl.value = preferred || "";
      selectEl.disabled = false;
      return selectEl.value;
    } catch (err) {
      console.error("[printer_settings] load printers failed", err);
      selectEl.disabled = true;
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "Printers unavailable";
      selectEl.appendChild(option);
      return "";
    }
  }

  async function refreshHealth(healthEl) {
    if (!healthEl) return;
    healthEl.textContent = "Checking printer health...";
    try {
      const payload = await fetchJson("/api/printers/health");
      const printerName = (payload.printer || "").trim();
      if (!printerName) {
        healthEl.textContent = "❌ No default printer selected";
        return;
      }
      if (payload.ready) {
        healthEl.textContent = `✅ Printer Ready — ${printerName}`;
      } else {
        healthEl.textContent = `⚠️ Printer Not Ready — ${payload.reason || "Unknown"}`;
      }
    } catch (err) {
      console.error("[printer_settings] health fetch failed", err);
      healthEl.textContent = "⚠️ Printer health unavailable";
    }
  }

  async function loadRecentPrints(listEl) {
    if (!listEl) return;
    listEl.textContent = "Loading recent prints...";
    try {
      const payload = await fetchJson("/api/prints/recent?limit=5");
      const jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
      if (!jobs.length) {
        listEl.textContent = "No recent prints";
        return;
      }
      listEl.innerHTML = "";
      jobs.forEach((job) => {
        const line = document.createElement("div");
        line.style.marginBottom = "4px";
        line.textContent = `${job.ok ? "✅" : "⚠️"} ${job.created_at || ""} x${job.copies} SKU:${job.sku || ""}`;
        listEl.appendChild(line);
        if (job.error) {
          const errLine = document.createElement("div");
          errLine.style.fontSize = "11px";
          errLine.style.color = "#b91c1c";
          errLine.style.marginLeft = "10px";
          errLine.textContent = job.error;
          listEl.appendChild(errLine);
        }
      });
    } catch (err) {
      console.error("[printer_settings] recent prints failed", err);
      listEl.textContent = "Unable to load recent prints";
    }
  }

  async function saveSettings(selectEl, widthEl, heightEl, gapEl, statusEl, warningEl) {
    const printerName = (selectEl?.value || "").trim();
    if (!selectEl) return;
    setStatusText(statusEl, "Saving printer settings...", "info");
    if (warningEl) warningEl.textContent = "";
    const payload = {
      default_printer_name: printerName,
      label_settings: {
        label_width_mm: Number.parseFloat(widthEl?.value) || DEFAULT_LABEL_VALUES.label_width_mm,
        label_height_mm: Number.parseFloat(heightEl?.value) || DEFAULT_LABEL_VALUES.label_height_mm,
        gap_mm: Number.parseFloat(gapEl?.value) || DEFAULT_LABEL_VALUES.gap_mm,
      },
    };
    try {
      const resp = await fetch("/api/printers/default", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      setStatusText(statusEl, "Printer settings saved.", "success");
      await loadPrinterDefaults(selectEl, widthEl, heightEl, gapEl, statusEl);
      await loadPrinters(selectEl, selectEl.value);
      await refreshHealth(getElement(IDS.health));
    } catch (err) {
      console.error("[printer_settings] save failed", err);
      setStatusText(statusEl, "Saving failed.", "error");
      if (warningEl) {
        warningEl.textContent = err.message || "Unable to save settings.";
      }
    }
  }

  async function runTestPrint(buttonEl, recentListEl) {
    if (!buttonEl) return;
    const payload = { ean: "6292526066910", sku: "TEST-PRINT", copies: 1 };
    const originalText = buttonEl.textContent;
    buttonEl.disabled = true;
    buttonEl.textContent = "Printing…";
    try {
      const resp = await fetch("/api/barcode/print", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      alert("✅ Test print sent");
      await loadRecentPrints(recentListEl);
    } catch (err) {
      console.error("[printer_settings] test print failed", err);
      alert(`❌ Test print failed: ${err.message}`);
    } finally {
      buttonEl.disabled = false;
      buttonEl.textContent = originalText;
    }
  }

  function openModal(modal, statusEl, healthEl, selectEl, widthEl, heightEl, gapEl, recentListEl) {
    if (!modal) return;
    if (modal.dataset.open === "1") return;
    modal.dataset.open = "1";
    modal.style.display = "flex";
    setStatusText(statusEl, "Loading printer settings...", "info");
    refreshHealth(healthEl);
    loadPrinterDefaults(selectEl, widthEl, heightEl, gapEl, statusEl).then((preferred) => {
      loadPrinters(selectEl, preferred);
    });
    loadRecentPrints(recentListEl);
  }

  function closeModal(modal) {
    if (!modal || modal.dataset.open !== "1") return;
    modal.dataset.open = "0";
    modal.style.display = "none";
  }

  function init() {
    const modal = getElement(IDS.modal);
    const openButton = getElement(IDS.openButton);
    const closeButton = getElement(IDS.closeButton);
    const selectEl = getElement(IDS.select);
    const widthEl = getElement(IDS.width);
    const heightEl = getElement(IDS.height);
    const gapEl = getElement(IDS.gap);
    const warningEl = getElement(IDS.warning);
    const statusEl = getElement(IDS.status);
    const saveButton = getElement(IDS.save);
    const testButton = getElement(IDS.test);
    const healthEl = getElement(IDS.health);
    const recentListEl = getElement(IDS.recentList);
    const recentRefresh = getElement(IDS.recentRefresh);

    if (!modal || !openButton) return;

    openButton.addEventListener("click", () => {
      openModal(modal, statusEl, healthEl, selectEl, widthEl, heightEl, gapEl, recentListEl);
    });

    document.addEventListener(
      "click",
      (event) => {
        if (event.target.closest("#" + IDS.openButton)) {
          event.preventDefault();
          openModal(modal, statusEl, healthEl, selectEl, widthEl, heightEl, gapEl, recentListEl);
        }
      },
      true
    );

    closeButton && closeButton.addEventListener("click", () => closeModal(modal));
    modal && modal.addEventListener("click", (event) => {
      if (event.target === modal) {
        closeModal(modal);
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeModal(modal);
      }
    });

    saveButton && saveButton.addEventListener("click", () => {
      saveSettings(selectEl, widthEl, heightEl, gapEl, statusEl, warningEl);
    });

    testButton && testButton.addEventListener("click", () => {
      runTestPrint(testButton, recentListEl);
    });

    recentRefresh && recentRefresh.addEventListener("click", () => {
      loadRecentPrints(recentListEl);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
