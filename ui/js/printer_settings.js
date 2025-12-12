/*
Manual Test Checklist:
1. Open the Printer Settings panel via the new header button and confirm the modal appears.
2. Verify printers load via GET /api/printers and the default dropdown reflects saved data.
3. Update the settings and POST to /api/printers/default, then refresh GET to ensure values persist.
4. Switch the Print Backend, save, and confirm the preview modal routes print requests based on the backend.
*/

(function () {
  const LABEL_FIELD_KEYS = [
    "label_width_mm",
    "label_height_mm",
    "dpi",
    "darkness",
    "speed",
  ];

  const TEXT_FIELD_KEYS = ["print_method", "media_type"];

  const DEFAULT_LABEL_VALUES = {
    label_width_mm: 40,
    label_height_mm: 30,
    dpi: 203,
    darkness: 12,
    speed: 3,
  };

  const DEFAULT_TEXT_VALUES = {
    print_method: "direct_thermal",
    media_type: "gap",
  };

  const DEFAULT_GAP = 0;
  const DEFAULT_PRINT_BACKEND = "nice_label";

  window.PRINTER_SETTINGS = window.PRINTER_SETTINGS || {
    print_backend: DEFAULT_PRINT_BACKEND,
  };

  const modal = document.getElementById("printer-settings-modal");
  const openButton = document.getElementById("printer-settings-btn");
  const selectEl = document.getElementById("printer-settings-select");
  const presetSelect = document.getElementById("printer-settings-preset");
  const backendSelect = document.getElementById("printer-settings-backend");
  const defaultPrinterField = document.getElementById("printer-settings-default-field");
  const presetField = document.getElementById("printer-settings-preset-field");
  const printMethodField = document.getElementById("printer-settings-print-method-field");
  const mediaField = document.getElementById("printer-settings-media-field");
  const gapField = document.getElementById("printer-settings-gap-field");
  const dpiField = document.getElementById("printer-settings-dpi-field");
  const niceLabelInfo = document.getElementById("printer-settings-nicelabel-info");
  const warningEl = document.getElementById("printer-settings-warning");
  const statusEl = document.getElementById("printer-settings-status");
  const saveButton = document.getElementById("printer-settings-save-btn");
  const closeButton = document.getElementById("printer-settings-close-btn");

  if (
    !modal ||
    !openButton ||
    !selectEl ||
    !saveButton ||
    !closeButton ||
    !backendSelect
  ) {
    return;
  }

  const labelInputs = LABEL_FIELD_KEYS.reduce((acc, key) => {
    const el = document.getElementById(`printer-setting-${key}`);
    if (el) {
      el.value = DEFAULT_LABEL_VALUES[key] ?? "";
    }
    acc[key] = el;
    return acc;
  }, {});
  const textInputs = TEXT_FIELD_KEYS.reduce((acc, key) => {
    const el = document.getElementById(`printer-setting-${key}`);
    if (el) {
      el.value = DEFAULT_TEXT_VALUES[key] || "";
    }
    acc[key] = el;
    return acc;
  }, {});
  const gapInput = document.getElementById("printer-setting-gap_mm");

  let isOpen = false;
  let printerPresets = {};
  let selectedPreset = "";
  let suppressPresetClear = false;
  setBackendSelection(window.PRINTER_SETTINGS.print_backend);

  function setElementVisible(element, visible) {
    if (!element) return;
    element.style.display = visible ? "" : "none";
  }

  function refreshNiceLabelFields() {
    const isNice = getBackendSelection() === "nice_label";
    const targets = [
      defaultPrinterField,
      presetField,
      printMethodField,
      mediaField,
      gapField,
      dpiField,
    ];
    targets.forEach((element) => setElementVisible(element, !isNice));
    setElementVisible(warningEl, !isNice);
    setElementVisible(niceLabelInfo, isNice);
  }

  function setBackendSelection(value) {
    const normalized = value || DEFAULT_PRINT_BACKEND;
    window.PRINTER_SETTINGS = window.PRINTER_SETTINGS || {};
    window.PRINTER_SETTINGS.print_backend = normalized;
    if (backendSelect) {
      backendSelect.value = normalized;
    }
    refreshNiceLabelFields();
    return normalized;
  }

  function getBackendSelection() {
    if (backendSelect && backendSelect.value) {
      return backendSelect.value;
    }
    return (
      window.PRINTER_SETTINGS?.print_backend || DEFAULT_PRINT_BACKEND
    );
  }

  function setStatus(message, tone = "info") {
    if (!statusEl) return;
    statusEl.textContent = message || "";
    if (tone === "error") {
      statusEl.style.color = "#b91c1c";
    } else if (tone === "success") {
      statusEl.style.color = "#059669";
    } else {
      statusEl.style.color = "#374151";
    }
  }

  function setWarning(message) {
    if (!warningEl) return;
    warningEl.textContent = message || "";
  }

  function toggleModal(show) {
    isOpen = show;
    modal.style.display = show ? "flex" : "none";
    if (show) {
      refreshPrinters();
      setStatus("Loading printer list...", "info");
    } else {
      setStatus("", "info");
      setWarning("");
    }
  }

  async function refreshPrinters() {
    if (!selectEl) return;
    selectEl.disabled = true;
    selectEl.innerHTML = '<option value="">Loading printers...</option>';
    setWarning("");

    try {
      const resp = await fetch("/api/printers");
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.json();
      const printers = Array.isArray(data?.printers) ? data.printers : [];

      selectEl.innerHTML = "";
      if (printers.length > 0) {
        printers.forEach((name) => {
          const option = document.createElement("option");
          option.value = name;
          option.textContent = name;
          selectEl.appendChild(option);
        });
        selectEl.disabled = false;
        setWarning(data?.warning || "");
      } else {
        const emptyOption = document.createElement("option");
        emptyOption.value = "";
        emptyOption.textContent = "No printers detected";
        selectEl.appendChild(emptyOption);
        selectEl.disabled = true;
        setWarning(data?.warning || "No printers available.");
      }

      await loadDefaultSettings();
      setStatus("Printer list refreshed.", "info");
    } catch (err) {
      selectEl.innerHTML = "";
      const fallbackOption = document.createElement("option");
      fallbackOption.value = "";
      fallbackOption.textContent = "Printer list unavailable";
      selectEl.appendChild(fallbackOption);
      selectEl.disabled = true;
      setWarning("Unable to fetch printers.");
      setStatus("Failed to load printers.", "error");
    }
  }

  async function loadDefaultSettings() {
    try {
      const resp = await fetch("/api/printers/default");
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const payload = await resp.json();
      const defaultName = (payload?.default_printer_name || "").trim();

      if (defaultName) {
        const optionExists = Array.from(selectEl.options).some((opt) => opt.value === defaultName);
        if (!optionExists) {
          const extra = document.createElement("option");
          extra.value = defaultName;
          extra.textContent = `${defaultName} (saved)`;
          selectEl.appendChild(extra);
        }
        selectEl.value = defaultName;
      }

      const labelValues = payload?.label_settings || {};
      LABEL_FIELD_KEYS.forEach((key) => {
        const input = labelInputs[key];
        if (!input) return;
        const value = labelValues[key];
        input.value = value != null ? value : DEFAULT_LABEL_VALUES[key] ?? "";
      });

      TEXT_FIELD_KEYS.forEach((key) => {
        const input = textInputs[key];
        if (!input) return;
        const value = labelValues[key];
        input.value = value != null ? value : DEFAULT_TEXT_VALUES[key] || "";
      });

      if (gapInput) {
        const gapValue = labelValues.gap_mm;
        gapInput.value = gapValue != null ? gapValue : DEFAULT_GAP;
      }

      const backendValue = payload?.print_backend || DEFAULT_PRINT_BACKEND;
      setBackendSelection(backendValue);

      printerPresets = payload?.presets || {};
      populatePresetOptions();
      selectedPreset = payload?.selected_preset || "";
      if (selectedPreset && presetSelect) {
        presetSelect.value = selectedPreset;
      }

      if (selectedPreset && printerPresets[selectedPreset]) {
        applyPresetValues(printerPresets[selectedPreset]);
      }

      setStatus("Loaded saved printer settings.", "info");
    } catch (err) {
      setStatus("Unable to load saved settings.", "error");
    }
  }

  async function saveSettings() {
    if (!selectEl || !saveButton) return;
    setStatus("Saving printer settings...", "info");
    saveButton.disabled = true;

    const labelPayload = LABEL_FIELD_KEYS.reduce((acc, key) => {
      const input = labelInputs[key];
      const parsed = input ? parseFloat(input.value) : NaN;
      acc[key] = Number.isFinite(parsed) ? parsed : DEFAULT_LABEL_VALUES[key];
      return acc;
    }, {});

    TEXT_FIELD_KEYS.forEach((key) => {
      const input = textInputs[key];
      if (input) {
        labelPayload[key] = input.value || DEFAULT_TEXT_VALUES[key] || "";
      }
    });

    if (gapInput) {
      const parsedGap = parseFloat(gapInput.value);
      labelPayload.gap_mm = Number.isFinite(parsedGap) ? parsedGap : DEFAULT_GAP;
    }

    const backendSelection = getBackendSelection();
    setBackendSelection(backendSelection);
    const payload = {
      default_printer_name: selectEl.value || "",
      label_settings: labelPayload,
      selected_preset: selectedPreset,
      print_backend: backendSelection,
    };

    try {
      const resp = await fetch("/api/printers/default", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        const msg = await resp.text();
        throw new Error(msg || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      setStatus("Printer settings saved.", "success");
      const savedPreset = data?.settings?.selected_preset || "";
      selectedPreset = savedPreset;
      if (presetSelect) {
        presetSelect.value = savedPreset || "";
      }
      const savedBackend = data?.settings?.print_backend || backendSelection;
      setBackendSelection(savedBackend);
    } catch (err) {
      setStatus("Failed to save printer settings.", "error");
    } finally {
      saveButton.disabled = false;
    }
  }

  function populatePresetOptions() {
    if (!presetSelect) return;
    const customOption = document.createElement("option");
    customOption.value = "";
    customOption.textContent = "Custom";
    presetSelect.innerHTML = "";
    presetSelect.appendChild(customOption);
    Object.keys(printerPresets || {}).forEach((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      presetSelect.appendChild(option);
    });
  }

  function applyPresetValues(preset) {
    suppressPresetClear = true;
    const values = preset?.label_settings || {};
    LABEL_FIELD_KEYS.forEach((key) => {
      if (!labelInputs[key]) return;
      const value = values[key];
      labelInputs[key].value =
        value != null ? value : DEFAULT_LABEL_VALUES[key] ?? "";
    });
    TEXT_FIELD_KEYS.forEach((key) => {
      const input = textInputs[key];
      if (!input) return;
      const value = values[key];
      input.value = value != null ? value : DEFAULT_TEXT_VALUES[key] || "";
    });
    if (gapInput) {
      const gapValue = values.gap_mm;
      gapInput.value = gapValue != null ? gapValue : DEFAULT_GAP;
    }
    const backendValue = preset?.print_backend || DEFAULT_PRINT_BACKEND;
    setBackendSelection(backendValue);
    suppressPresetClear = false;
  }

  function markCustomState() {
    if (suppressPresetClear) return;
    selectedPreset = "";
    if (presetSelect) {
      presetSelect.value = "";
    }
  }


  openButton.addEventListener("click", () => toggleModal(true));
  closeButton.addEventListener("click", () => toggleModal(false));
  saveButton.addEventListener("click", saveSettings);
  if (presetSelect) {
    presetSelect.addEventListener("change", (event) => {
      const value = event.target.value;
      selectedPreset = value || "";
      if (selectedPreset && printerPresets[selectedPreset]) {
        applyPresetValues(printerPresets[selectedPreset]);
      }
    });
  }

  Object.values(labelInputs).forEach((input) => {
    input?.addEventListener("input", markCustomState);
  });
  Object.values(textInputs).forEach((input) => {
    input?.addEventListener("input", markCustomState);
  });
  gapInput?.addEventListener("input", markCustomState);
  backendSelect?.addEventListener("change", (event) => {
    markCustomState();
    const value = event.target.value;
    setBackendSelection(value);
  });

  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      toggleModal(false);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isOpen) {
      toggleModal(false);
    }
  });
})();
