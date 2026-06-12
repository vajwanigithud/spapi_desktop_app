(function () {
  // This dialog intentionally receives raw .env values from the local API because
  // the app is designed to run as a localhost-only desktop tool.
  const spapiFields = [
    "LWA_CLIENT_ID",
    "LWA_CLIENT_SECRET",
    "LWA_REFRESH_TOKEN",
    "MARKETPLACE_IDS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ROLE_ARN",
    "AWS_REGION",
    "SPAPI_REGION",
  ];
  const dfFields = [
    "DF_REMITTANCE_IMAP_USER",
    "DF_REMITTANCE_IMAP_PASS",
    "DF_REMITTANCE_GMAIL_LABEL",
    "DF_REMITTANCE_IMAP_MAILBOX",
    "DF_REMITTANCE_MAX_MESSAGES",
  ];
  const secretFields = new Set([
    "LWA_CLIENT_SECRET",
    "LWA_REFRESH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "DF_REMITTANCE_IMAP_PASS",
  ]);

  let secretsVisible = false;
  let currentValues = {};

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function fieldHtml(key) {
    const type = secretFields.has(key) && !secretsVisible ? "password" : "text";
    return `
      <div class="credentials-field">
        <label for="credential-field-${escapeHtml(key)}">${escapeHtml(key)}</label>
        <input id="credential-field-${escapeHtml(key)}" data-env-key="${escapeHtml(key)}" type="${type}" autocomplete="off" spellcheck="false" />
      </div>
    `;
  }

  function renderFields() {
    const spapiTarget = $("credentials-spapi-fields");
    const dfTarget = $("credentials-df-fields");
    if (spapiTarget) spapiTarget.innerHTML = spapiFields.map(fieldHtml).join("");
    if (dfTarget) dfTarget.innerHTML = dfFields.map(fieldHtml).join("");
    setFieldValues(currentValues);
  }

  function setFieldValues(values) {
    document.querySelectorAll("[data-env-key]").forEach((input) => {
      const key = input.getAttribute("data-env-key");
      input.value = values?.[key] ?? "";
    });
  }

  function collectValues() {
    const values = {};
    document.querySelectorAll("[data-env-key]").forEach((input) => {
      values[input.getAttribute("data-env-key")] = input.value;
    });
    return values;
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      button.dataset.originalText = button.textContent;
      button.textContent = label;
      button.disabled = true;
    } else {
      button.textContent = button.dataset.originalText || button.textContent;
      button.disabled = false;
    }
  }

  function statusClass(status) {
    const normalized = String(status || "Not Tested").toLowerCase().replace(/\s+/g, "-");
    if (normalized === "ok") return "credentials-status-ok";
    if (normalized === "warning") return "credentials-status-warning";
    if (normalized === "error") return "credentials-status-error";
    return "credentials-status-not-tested";
  }

  function formatTime(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString();
  }

  function renderHealth(health) {
    const status = health?.status || "Not Tested";
    const statusEl = $("credentials-health-status");
    if (statusEl) {
      statusEl.textContent = status;
      statusEl.className = statusClass(status);
    }
    const tokenTimeEl = $("credentials-health-token-time");
    if (tokenTimeEl) tokenTimeEl.textContent = formatTime(health?.last_token_test_time);
    const vendorTimeEl = $("credentials-health-vendor-time");
    if (vendorTimeEl) vendorTimeEl.textContent = formatTime(health?.last_vendor_po_test_time);
    const errorEl = $("credentials-health-error");
    if (errorEl) errorEl.textContent = health?.last_redacted_error || "-";
  }

  async function loadCredentials() {
    const resp = await fetch("/api/credentials");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    currentValues = data.values || {};
    renderFields();
    renderHealth(data.health);
    return data;
  }

  async function openDialog() {
    const modal = $("credentials-modal");
    if (modal) modal.style.display = "flex";
    const message = $("credentials-test-message");
    if (message) message.textContent = "Loading .env values...";
    try {
      await loadCredentials();
      if (message) message.textContent = "";
    } catch (err) {
      if (message) message.textContent = `Failed to load credentials: ${err.message}`;
    }
  }

  function closeDialog() {
    const modal = $("credentials-modal");
    if (modal) modal.style.display = "none";
  }

  function toggleSecrets() {
    secretsVisible = !secretsVisible;
    const btn = $("credentials-toggle-secrets-btn");
    if (btn) btn.textContent = secretsVisible ? "Hide Secrets" : "Show Secrets";
    currentValues = collectValues();
    renderFields();
  }

  async function saveCredentials() {
    const btn = $("credentials-save-btn");
    const message = $("credentials-save-message");
    setBusy(btn, true, "Saving...");
    if (message) message.textContent = "";
    try {
      const resp = await fetch("/api/credentials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: collectValues() }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      currentValues = data.values || {};
      setFieldValues(currentValues);
      renderHealth(data.health);
      if (message) message.textContent = `Saved. Backup created: ${data.backup_path || "-"}`;
    } catch (err) {
      if (message) message.textContent = `Save failed: ${err.message}`;
    } finally {
      setBusy(btn, false);
    }
  }

  async function reloadCredentials() {
    const btn = $("credentials-reload-btn");
    const message = $("credentials-test-message");
    setBusy(btn, true, "Reloading...");
    try {
      const resp = await fetch("/api/credentials/reload", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      currentValues = data.values || {};
      setFieldValues(currentValues);
      renderHealth(data.health);
      if (message) message.textContent = ".env reloaded.";
    } catch (err) {
      if (message) message.textContent = `Reload failed: ${err.message}`;
    } finally {
      setBusy(btn, false);
    }
  }

  async function runTest(path, buttonId, busyLabel) {
    const btn = $(buttonId);
    const message = $("credentials-test-message");
    setBusy(btn, true, busyLabel);
    if (message) message.textContent = "";
    try {
      const resp = await fetch(path, { method: "POST" });
      const data = await resp.json();
      renderHealth({
        status: data.status || (data.ok ? "OK" : "Error"),
        last_token_test_time: path.includes("test-lwa") ? data.tested_at : undefined,
        last_vendor_po_test_time: path.includes("test-vendor-po") ? data.tested_at : undefined,
        last_redacted_error: data.error || "",
      });
      await loadCredentials();
      if (message) {
        message.textContent = data.ok
          ? `${data.status || "OK"} at ${formatTime(data.tested_at)}`
          : `Failed: ${data.error || "Unknown error"}`;
      }
    } catch (err) {
      if (message) message.textContent = `Test failed: ${err.message}`;
    } finally {
      setBusy(btn, false);
    }
  }

  function init() {
    const openBtn = $("credentials-btn");
    if (openBtn) openBtn.addEventListener("click", openDialog);
    const closeBtn = $("credentials-close-btn");
    if (closeBtn) closeBtn.addEventListener("click", closeDialog);
    const toggleBtn = $("credentials-toggle-secrets-btn");
    if (toggleBtn) toggleBtn.addEventListener("click", toggleSecrets);
    const saveBtn = $("credentials-save-btn");
    if (saveBtn) saveBtn.addEventListener("click", saveCredentials);
    const reloadBtn = $("credentials-reload-btn");
    if (reloadBtn) reloadBtn.addEventListener("click", reloadCredentials);
    const lwaBtn = $("credentials-test-lwa-btn");
    if (lwaBtn) lwaBtn.addEventListener("click", () => runTest("/api/credentials/test-lwa", "credentials-test-lwa-btn", "Testing..."));
    const vendorBtn = $("credentials-test-vendor-po-btn");
    if (vendorBtn) vendorBtn.addEventListener("click", () => runTest("/api/credentials/test-vendor-po", "credentials-test-vendor-po-btn", "Testing..."));
    renderFields();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
