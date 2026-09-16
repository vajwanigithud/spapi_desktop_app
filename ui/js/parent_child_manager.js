/* Parent–Child Manager: read-only by design. No write endpoints exist. */
window.PC = (() => {
  const state = { items: [], contexts: [], marketplaces: [], schemas: {}, familyType: null, step: 1, inited: false, modalBound: false, vendorBound: false, validationPreviewEnabled: false, validationPreflightPassed: false, validationPreviewRunning: false };
  const $ = id => document.getElementById(id);
  const esc = value => window.escapeHtml ? window.escapeHtml(value) : String(value || "");

  function closePreviewModal() {
    const modal = $("pc-image-modal");
    const image = $("pc-image-large");
    if (!modal || !image) return;
    modal.hidden = true;
    modal.classList.remove("open", "active");
    modal.setAttribute("aria-hidden", "true");
    image.removeAttribute("src");
    image.alt = "";
    image.removeAttribute("title");
    document.body.style.overflow = "";
  }

  function validPreviewUrl(value) {
    if (typeof value !== "string" || !value.trim()) return false;
    try {
      const url = new URL(value.trim(), window.location.href);
      return url.protocol === "http:" || url.protocol === "https:";
    } catch (_) {
      return false;
    }
  }

  function openPreviewModal(url, title = "Product image preview") {
    const modal = $("pc-image-modal");
    const image = $("pc-image-large");
    if (!modal || !image || !validPreviewUrl(url)) {
      closePreviewModal();
      return false;
    }
    image.src = url.trim();
    image.alt = title;
    image.title = title;
    modal.hidden = false;
    modal.classList.add("open");
    modal.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    return true;
  }

  function bindPreviewModal() {
    if (state.modalBound) return;
    const modal = $("pc-image-modal");
    const closeButton = $("pc-image-modal-close");
    const image = $("pc-image-large");
    if (!modal || !closeButton || !image) return;
    state.modalBound = true;
    closeButton.addEventListener("click", closePreviewModal);
    modal.addEventListener("click", event => {
      if (event.target === modal) closePreviewModal();
    });
    image.addEventListener("error", closePreviewModal);
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && !modal.hidden) closePreviewModal();
    });
  }

  async function api(path, options = {}) {
    const response = await fetch("/api/parent-child" + path, {
      headers: { "Content-Type": "application/json" }, ...options,
    });
    const contentType = response.headers.get("content-type") || "";
    const body = await response.text(); let data = null;
    if (contentType.includes("application/json")) { try { data = JSON.parse(body); } catch (_) {} }
    if (!response.ok) { const error = Error(data?.detail || data?.message || (data?.errors || []).map(item => item.message).join(" ") || (data ? "Request failed" : "Preview service returned an unexpected server response. Check application logs and retry.")); error.payload = data; error.unexpectedResponse = !data; throw error; }
    if (!data) throw Error("Preview service returned an unexpected server response. Check application logs and retry.");
    return data;
  }

  function scope() {
    const vendor_code = $("pc-vendor").value;
    const context = state.contexts.find(row => row.marketplace_id === $("pc-marketplace").value && row.vendor_code === vendor_code);
    return { marketplace_id: $("pc-marketplace").value, vendor_code, vendor_context_id: context?.id };
  }

  function syncVendors() {
    const marketplace = $("pc-marketplace").value;
    const rows = state.contexts.filter(row => row.marketplace_id === marketplace && row.verified && row.active);
    $("pc-vendor").innerHTML = rows.map(row => `<option value="${esc(row.vendor_code)}">${esc(row.display_name || row.vendor_code)} — ${esc(row.vendor_code)} — Verified</option>`).join("") || '<option value="">No verified vendor code</option>';
    const preferred = localStorage.getItem("pc_last_vendor_context");
    if (preferred && rows.some(row => row.vendor_code === preferred)) $("pc-vendor").value = preferred;
    if (rows.length === 1) $("pc-vendor").value = rows[0].vendor_code;
    $("pc-vendor").onchange = () => localStorage.setItem("pc_last_vendor_context", $("pc-vendor").value);
    $("pc-parent-form").hidden = !rows.length;
    $("pc-setup-card").hidden = !!rows.length;
    $("pc-setup-marketplace").textContent = state.marketplaces.find(item => item.marketplace_id === marketplace)?.label || "Not configured";
  }

  function showSetupError(message, detail = "") {
    const error = $("pc-setup-error");
    error.hidden = false;
    error.innerHTML = `<strong>${esc(message)}</strong><button class="btn btn-outline-secondary" type="button" id="pc-setup-retry">Retry</button><button class="btn btn-outline-secondary" type="button" id="pc-setup-credentials">Open Credentials</button><details><summary>Advanced Details</summary><pre>${esc(detail)}</pre></details>`;
    $("pc-setup-retry").onclick = () => { state.inited = false; init(); };
    $("pc-setup-credentials").onclick = openCredentials;
  }

  function openCredentials() {
    const button = document.getElementById("credentials-btn");
    if (button) button.click();
  }

  function closeVendorModal() {
    const modal = $("pc-vendor-modal"); if (!modal) return;
    modal.hidden = true; modal.setAttribute("aria-hidden", "true"); modal.classList.remove("open", "active");
  }

  function openVendorModal(context = null) {
    const modal = $("pc-vendor-modal"); if (!modal) return;
    $("pc-vendor-marketplace").innerHTML = state.marketplaces.map(item => `<option value="${esc(item.marketplace_id)}">${esc(item.label)}</option>`).join("");
    $("pc-vendor-marketplace").value = context?.marketplace_id || $("pc-marketplace").value;
    $("pc-vendor-code").value = context?.vendor_code || ""; $("pc-vendor-label").value = context?.display_name || ""; $("pc-vendor-verify-result").textContent = "";
    modal.hidden = false; modal.setAttribute("aria-hidden", "false");
  }

  async function verifyVendor() {
    const marketplace_id = $("pc-vendor-marketplace").value;
    const vendor_code = $("pc-vendor-code").value.trim();
    const display_name = $("pc-vendor-label").value.trim() || null;
    const output = $("pc-vendor-verify-result");
    if (!vendor_code) { output.textContent = "Enter a vendor code."; output.className = "pc-verify-result error"; return; }
    $("pc-vendor-verify").disabled = true; output.className = "pc-verify-result"; output.textContent = "Verifying read-only access…";
    try {
      const result = await api("/vendor-contexts/verify", { method: "POST", body: JSON.stringify({ marketplace_id, vendor_code, display_name }) });
      if (!result.verified) throw Error(result.last_error || "Verification did not return a supported capability.");
      output.className = "pc-verify-result success"; output.textContent = "Vendor access verified";
      state.contexts = (await api(`/vendor-contexts?marketplace_id=${encodeURIComponent(marketplace_id)}`)).items;
      $("pc-marketplace").value = marketplace_id; syncVendors(); localStorage.setItem("pc_last_vendor_context", vendor_code);
      closeVendorModal();
    } catch (error) { output.className = "pc-verify-result error"; output.textContent = error.message; }
    finally { $("pc-vendor-verify").disabled = false; }
  }

  function bindVendorSetup() {
    if (state.vendorBound) return;
    state.vendorBound = true;
    $("pc-add-vendor").onclick = openVendorModal; $("pc-setup-add").onclick = openVendorModal;
    $("pc-open-credentials").onclick = openCredentials; $("pc-vendor-cancel").onclick = closeVendorModal; $("pc-vendor-verify").onclick = verifyVendor;
    $("pc-reverify-vendor").onclick = () => openVendorModal(state.contexts.find(row => row.vendor_code === $("pc-vendor").value));
    $("pc-edit-vendor").onclick = () => openVendorModal(state.contexts.find(row => row.vendor_code === $("pc-vendor").value));
    $("pc-disable-vendor").onclick = async () => {
      const context = state.contexts.find(row => row.vendor_code === $("pc-vendor").value);
      if (!context) return;
      try { await api(`/vendor-contexts/${context.id}/status`, { method: "POST", body: JSON.stringify({ active: false }) }); state.contexts = (await api(`/vendor-contexts?marketplace_id=${encodeURIComponent($("pc-marketplace").value)}`)).items; syncVendors(); }
      catch (error) { showSetupError("Could not disable Vendor Code.", error.message); }
    };
    $("pc-vendor-modal").addEventListener("click", event => { if (event.target === $("pc-vendor-modal")) closeVendorModal(); });
  }

  function bindThumbnailHandlers() {
    document.querySelectorAll("[data-pc-preview-url]").forEach(preview => {
      preview.addEventListener("click", () => openPreviewModal(preview.dataset.pcPreviewUrl, preview.dataset.pcPreviewTitle || "Product image preview"));
      const image = preview.matches("img") ? preview : preview.querySelector("img");
      image?.addEventListener("error", () => {
        preview.replaceWith(Object.assign(document.createElement("div"), { className: "pc-placeholder", textContent: "No image" }));
      }, { once: true });
    });
  }

  async function init() {
    closePreviewModal();
    bindPreviewModal();
    bindVendorSetup();
    if (state.inited) return;
    state.inited = true;
    try {
      $("pc-setup-error").hidden = true;
      state.marketplaces = (await api("/marketplaces")).items;
      if (!state.marketplaces.length) {
        $("pc-parent-form").hidden = true; $("pc-setup-card").hidden = false;
        $("pc-setup-marketplace").textContent = "No SP-API marketplace is configured. Open Credentials to configure one.";
        return;
      }
      $("pc-marketplace").innerHTML = state.marketplaces.map(item => `<option value="${esc(item.marketplace_id)}">${esc(item.label)}</option>`).join("");
      const storedMarketplace = localStorage.getItem("pc_last_marketplace");
      if (storedMarketplace && state.marketplaces.some(item => item.marketplace_id === storedMarketplace)) $("pc-marketplace").value = storedMarketplace;
      if (state.marketplaces.length === 1) $("pc-marketplace").value = state.marketplaces[0].marketplace_id;
      state.contexts = (await api(`/vendor-contexts?marketplace_id=${encodeURIComponent($("pc-marketplace").value)}`)).items;
      $("pc-marketplace").onchange = async () => { localStorage.setItem("pc_last_marketplace", $("pc-marketplace").value); state.contexts = (await api(`/vendor-contexts?marketplace_id=${encodeURIComponent($("pc-marketplace").value)}`)).items; syncVendors(); };
      syncVendors();
      if ($("pc-vendor").value) { const capability=await api(`/capability-status?marketplace_id=${encodeURIComponent($("pc-marketplace").value)}&vendor_code=${encodeURIComponent($("pc-vendor").value)}`); state.validationPreviewEnabled=capability.amazon_validation_preview === true; }
      $("pc-asins").oninput = parse;
      $("pc-find").onclick = find;
      $("pc-discover").onclick = discover;
      $("pc-theme").onchange = () => { pruneVariationValues($("pc-theme").value); if ($("pc-theme-step2")) $("pc-theme-step2").value = $("pc-theme").value; cards(); };
      $("pc-theme-step2").onchange = () => { $("pc-theme").value = $("pc-theme-step2").value; pruneVariationValues($("pc-theme").value); cards(); };
      await jobs();
    } catch (error) {
      console.error("Parent–Child Manager initialization failed", error);
      closePreviewModal();
      state.inited = false;
      showSetupError("Could not load Parent–Child setup.", error.message || String(error));
    }
  }

  async function parse() {
    try {
      const data = await api("/parse-asins", { method: "POST", body: JSON.stringify({ text: $("pc-asins").value }) });
      $("pc-counts").textContent = `${data.total_input_count} entered · ${data.valid_unique_count} valid unique · ${data.duplicate_count} duplicates · ${data.invalid_count} invalid`;
      return data;
    } catch (_) { return null; }
  }

  function friendlyTheme(value) {
    const labels = { Color: "Colour", Colour: "Colour", Size: "Size", ColorSize: "Colour and Size", ColourSize: "Colour and Size", Style: "Style", NumberOfItems: "Number of Items" };
    return labels[value] || String(value || "").replace(/([a-z])([A-Z])/g, "$1 $2");
  }

  function schemaStatusCard(status) {
    const row = (label, value) => `<div><b>${label}</b><span>${esc(value.state)}${value.error ? ` — ${esc(value.error)}` : ""}</span>${value.state === "Failed" ? `<button type="button" class="btn btn-outline-secondary" onclick="PC.retrySchema('${value.level}')">Retry</button>` : ""}</div>`;
    $("pc-schema-status").innerHTML = `<div class="pc-schema-card"><h3>Schema status</h3><p>Product Type: <b>${esc(state.familyType || "—")}</b></p>${row("Parent schema", status.parent)}${row("Child schema", status.child)}</div>`;
  }

  function themeSelects(themes) {
    [$("pc-theme"), $("pc-theme-step2")].filter(Boolean).forEach(select => {
      select.innerHTML = themes.map(value => `<option value="${esc(value)}">${esc(friendlyTheme(value))}</option>`).join("") || "<option>No schema-supported themes</option>";
      select.disabled = !themes.length;
      if (themes.length === 1) select.value = themes[0];
    });
  }

  async function loadSchemas(force = false) {
    const status = { parent: { level: "parent", state: "Loading" }, child: { level: "child", state: "Loading" } };
    schemaStatusCard(status);
    const requests = ["parent", "child"].map(level => api("/product-type-definition", { method: "POST", body: JSON.stringify({ ...scope(), product_type: state.familyType, parentage_level: level, force }) }));
    const results = await Promise.allSettled(requests);
    ["parent", "child"].forEach((level, index) => {
      const result = results[index];
      if (result.status === "fulfilled") { state.schemas[level] = result.value.schema; status[level] = { level, state: `Loaded (HTTP ${result.value.trace.schema_status})`, trace: result.value.trace }; }
      else status[level] = { level, state: "Failed", error: result.reason.message || "Product Type Definition unavailable" };
    });
    state.schemaStatus = status; schemaStatusCard(status);
    if (!state.schemas.parent || !state.schemas.child) { themeSelects([]); return; }
    const themes = results[0].value.variation_themes || [];
    themeSelects(themes);
    pruneVariationValues($("pc-theme").value);
    if (!themes.length) $("pc-product-type-status").textContent = `Amazon’s ${state.familyType} schema does not currently support a variation family for this marketplace/vendor context.`;
    $("pc-schema").textContent = JSON.stringify({ parent: state.schemas.parent, child: state.schemas.child, trace: { parent: status.parent.trace, child: status.child.trace } }, null, 2);
  }

  function familyAnalysis() {
    const resolved = state.items.filter(item => item.status !== "Lookup Failed" && item.status !== "Blocked");
    const eligible = state.items.filter(item => item.status === "Ready" || item.status === "Already Connected");
    const groups = {};
    resolved.forEach(item => {
      const type = item.product_type || "Product Type unavailable";
      (groups[type] ||= []).push(item);
    });
    const types = Object.keys(groups).filter(type => type !== "Product Type unavailable");
    const parents = [...new Set(resolved.map(item => item.current_parent).filter(parent => parent && parent !== "Unknown"))];
    const missing = resolved.filter(item => !item.product_type);
    const mismatch = types.length > 1 || parents.length > 1 || missing.length > 0;
    return { resolved, eligible, groups, types, parents, missing, mismatch, canProceed: !mismatch && eligible.length > 0 && eligible.length === state.items.length };
  }

  async function detectFamilyType() {
    const analysis = familyAnalysis();
    const eligible = analysis.eligible;
    const types = analysis.types;
    const missing = analysis.missing;
    state.familyType = null; state.schemas = {};
    const theme = $("pc-theme"); theme.disabled = true; theme.innerHTML = "<option>Waiting for a consistent product type</option>";
    if (missing.length) { $("pc-product-type-status").textContent = `Needs Review: product type unavailable for ${missing.map(item => item.asin).join(", ")}.`; $("pc-product-type-value").textContent = ""; return; }
    if (types.length !== 1 || analysis.parents.length > 1) { $("pc-product-type-status").textContent = types.length > 1 ? `Product type mismatch: ${types.join(", ")}. Keep matching products only to continue.` : "Existing parent mismatch. Keep one compatible group only to continue."; $("pc-product-type-value").textContent = ""; return; }
    state.familyType = types[0]; $("pc-product-type").value = state.familyType; $("pc-product-type-value").textContent = state.familyType; $("pc-product-type-status").textContent = `Detected from ${eligible.length} child products`;
    await loadSchemas();
  }

  function imageMarkup(item) {
    if (!validPreviewUrl(item.image_url)) return '<div class="pc-placeholder">No image</div>';
    return `<button type="button" class="pc-thumbnail-button" data-pc-preview-url="${esc(item.image_url)}" data-pc-preview-title="${esc(item.title || item.asin || "Product image preview")}" aria-label="Preview image for ${esc(item.title || item.asin)}"><img src="${esc(item.image_url)}" alt="Product thumbnail"></button>`;
  }

  function themeNames(node) {
    if (!node || typeof node !== "object") return [];
    if (Array.isArray(node)) return node.flatMap(themeNames);
    const direct = node.properties?.variation_theme?.contains?.properties?.name?.enum || [];
    return [...direct, ...Object.values(node).flatMap(themeNames)];
  }
  function childAttributes(theme) {
    const schema = state.schemas.child || {}; const names = new Set();
    const visit = node => { if (!node || typeof node !== "object") return; if (node.if && themeNames(node.if).includes(theme)) (node.then?.required || []).forEach(name => names.add(name)); Object.values(node).forEach(value => { if (value && typeof value === "object") visit(value); }); };
    visit(schema.allOf || []);
    return [...names].map(name => ({ name, node: (schema.properties || {})[name] || {}, label: ((schema.properties || {})[name] || {}).title || friendlyTheme(name) }));
  }
  function pruneVariationValues(theme) { const allowed=new Set(childAttributes(theme).map(field=>field.name)); state.items=(state.items || []).map(item=>({ ...item, variation_values:Object.fromEntries(Object.entries(item.variation_values || {}).filter(([name])=>allowed.has(name)))})); }
  function enumValues(node) {
    return node?.enum || node?.items?.enum || node?.items?.properties?.value?.enum || node?.items?.properties?.value?.items?.enum || [];
  }
  function valueRules(node) { return node?.items?.properties?.value || node?.items || node || {}; }
  function validAttribute(node, value) { const rules = valueRules(node); const text = String(value || "").trim(); if (!text || (rules.minLength && text.length < rules.minLength) || (rules.maxLength && text.length > rules.maxLength)) return false; try { return !rules.pattern || new RegExp(rules.pattern).test(text); } catch (_) { return true; } }
  function variationFields(item, index) {
    const selected = $("pc-theme").value;
    if (!selected || $("pc-theme").disabled) return "";
    const fields = childAttributes(selected);
    if (!fields.length) return '<div class="pc-meta">Amazon did not mark differentiating child attributes as required for this theme.</div>';
    return `<div class="pc-variation-fields"><b>Required variation attributes</b>${fields.map(field => { const values = enumValues(field.node); const value = (item.variation_values || {})[field.name] || ""; const rules = valueRules(field.node); const control = values.length ? `<select onchange="PC.variation(${index},'${esc(field.name)}',this.value)"><option value="">Select ${esc(field.label)}</option>${values.map(option => `<option value="${esc(option)}" ${option === value ? "selected" : ""}>${esc(option)}</option>`).join("")}</select>` : `<input required ${rules.minLength ? `minlength="${rules.minLength}"` : ""} ${rules.maxLength ? `maxlength="${rules.maxLength}"` : ""} ${rules.pattern ? `pattern="${esc(rules.pattern)}"` : ""} value="${esc(value)}" onchange="PC.variation(${index},'${esc(field.name)}',this.value)" aria-label="${esc(field.label)}">`; return `<label>${esc(field.label)}${control}</label>`; }).join("")}</div>`;
  }

  function card(item, index) {
    const lookup=item.lookup_details || {}; const catalog=lookup.catalog || {}; const listing=lookup.vendor_listing || {};
    return `<article class="pc-card">${imageMarkup(item)}<div><h4>${esc(item.title || item.asin)}</h4><div class="pc-meta">ASIN: ${esc(item.asin)} · Vendor SKU: ${esc(item.vendor_sku || "—")}</div><div class="pc-meta">Vendor: ${esc(scope().vendor_code)} · Type: ${esc(item.product_type || "—")}</div><div class="pc-meta">Current parent: ${esc(item.current_parent)}</div>${variationFields(item,index)}<div class="pc-status">${esc(item.status)}${item.status_detail ? " — " + esc(item.status_detail) : ""}</div><details class="pc-meta"><summary>Lookup Details</summary>Catalog: ${esc(catalog.status)} · Request ${esc(catalog.request_id)} · ${esc(catalog.error || "") }<br>Vendor listing: ${esc(listing.status)} · ${esc(listing.result_count)} result(s) · Request ${esc(listing.request_id)} · ${esc(listing.error || "")}</details><label class="pc-meta"><input type="checkbox" ${item.confirmed ? "checked" : ""} onchange="PC.confirm(${index},this.checked)"> visually confirmed</label><button class="btn btn-outline-secondary" onclick="PC.remove(${index})">Remove</button></div></article>`;
  }

  function renderCard(item, index) {
    const lookup=item.lookup_details || {}; const catalog=lookup.catalog || {}; const listing=lookup.vendor_listing || {};
    const label = item.status === "Ready" || item.status === "Already Connected" ? "Eligible" : item.status || "Lookup Failed";
    const badge = label.toLowerCase().replace(/\s+/g, "-");
    const field = (name, value) => `<div class="pc-field"><b>${name}</b>${esc(value || "—")}</div>`;
    return `<article class="pc-card">${imageMarkup(item)}<div><h4>${esc(item.title || item.asin)}</h4><div class="pc-fields">${field("ASIN", item.asin)}${field("Vendor SKU", item.vendor_sku)}${field("Product Type", item.product_type)}${field("Current Parent", item.current_parent)}</div>${variationFields(item,index)}<div class="pc-status pc-status-${badge}">${esc(label)}${item.status_detail ? " — " + esc(item.status_detail) : ""}</div><details class="pc-meta"><summary>Technical lookup details</summary>Catalog: ${esc(catalog.status)} · Request ${esc(catalog.request_id)} · ${esc(catalog.error || "")}<br>Vendor listing: ${esc(listing.status)} · ${esc(listing.result_count)} result(s) · Request ${esc(listing.request_id)} · ${esc(listing.error || "")}</details><label class="pc-meta"><input type="checkbox" ${item.confirmed ? "checked" : ""} onchange="PC.confirm(${index},this.checked)"> visually confirmed</label><button class="btn btn-outline-secondary" onclick="PC.remove(${index})">Remove</button></div></article>`;
  }
  function keepGroup(type) { state.items = state.items.filter(item => (item.product_type || "Product Type unavailable") === type); detectFamilyType().then(cards); }
  function workflowValidation() {
    const analysis = familyAnalysis(); const selectedTheme = $("pc-theme").value;
    const attributes = childAttributes(selectedTheme); const eligibleItems = state.items.filter(item => item.status === "Ready" || item.status === "Already Connected");
    const incomplete = eligibleItems.filter(item => !attributes.every(field => validAttribute(field.node, (item.variation_values || {})[field.name])));
    const reasons = [];
    if (analysis.mismatch) reasons.push("Selected products do not form one compatible Product Type/parent group.");
    if (!analysis.canProceed) reasons.push("Every selected product must be eligible before the family can proceed.");
    if (!state.schemas.parent) reasons.push("Parent Product Type Definition has not loaded.");
    if (!state.schemas.child) reasons.push("Child Product Type Definition has not loaded.");
    if (!selectedTheme) reasons.push("Select an Amazon-supported Variation Theme.");
    if (selectedTheme && !attributes.length) reasons.push("Amazon did not provide required child differentiating attributes for the selected theme.");
    if (incomplete.length) reasons.push(`Complete valid variation attributes for: ${incomplete.map(item => item.asin).join(", ")}.`);
    return { analysis, selectedTheme, attributes, eligibleItems, incomplete, reasons, canProceed: reasons.length === 0 };
  }
  function cards() {
    const validation = workflowValidation(); const { analysis } = validation; state.validation = validation;
    $("pc-products").innerHTML = state.items.map(renderCard).join("");
    $("pc-detection-summary").innerHTML = [["Resolved products", analysis.resolved.length], ["Eligible products", analysis.eligible.length], ["Product Types detected", analysis.types.length], ["Existing parents detected", analysis.parents.length], ["Family can proceed", validation.canProceed ? "Yes" : "No"]].map(([label, value]) => `<div><b>${esc(label)}</b><strong>${esc(value)}</strong></div>`).join("");
    const warning = $("pc-family-warning");
    if (!validation.canProceed) {
      const groups = Object.entries(analysis.groups).map(([type, items]) => {
        const parents = new Set(items.map(item => item.current_parent).filter(parent => parent && parent !== "Unknown"));
        const compatible = type !== "Product Type unavailable" && parents.size <= 1;
        return `<div class="pc-type-group"><b>${esc(type)}</b><br>${items.length} product${items.length === 1 ? "" : "s"}${parents.size ? ` · ${parents.size} existing parent${parents.size === 1 ? "" : "s"}` : ""}${compatible ? `<button type="button" class="btn btn-outline-secondary" onclick="PC.keepGroup(${JSON.stringify(type)})">Keep this group only</button>` : ""}</div>`;
      }).join("");
      warning.innerHTML = `<div class="pc-family-warning"><h3>${analysis.mismatch ? "Products cannot form one family" : "Family needs attention"}</h3><ul>${validation.reasons.map(reason => `<li>${esc(reason)}</li>`).join("")}</ul>${analysis.mismatch ? `<div class="pc-type-groups">${groups}</div>` : ""}</div>`;
    } else warning.innerHTML = "";
    bindThumbnailHandlers();
    const canReview=validation.canProceed;
    $("pc-review-button").disabled=!canReview;
    $("pc-review-reason").textContent=canReview?'':validation.reasons.join(" ");
  }

  async function find() {
    const parsed = await parse(); if (!parsed?.asins?.length) return;
    $("pc-find").disabled = true; $("pc-find").textContent = "Finding…";
    try {
      const data = await api("/resolve", { method: "POST", body: JSON.stringify({ ...scope(), asins: parsed.asins }) });
      state.items = data.items;
      await detectFamilyType(); populateFetchedParentValues(); parentRequiredFormV3(); cards();
    } catch (error) { $("pc-products").textContent = error.message; }
    finally { $("pc-find").disabled = false; $("pc-find").textContent = "Find Products"; }
  }

  function populateFetchedParentValues() { const schema = state.schemas.parent || {}; const items = state.items || []; const props = schema.properties || {}; if (!items.length || items.some(item => item.attributes_status !== 200 || !item.verified_attributes)) return; const blocked = /(?:price|cost|identifier|dimension|weight|manufacturer|compliance|hazmat|dangerous|battery|feature)/i; const values = state.parentValues || {}; const provenance = state.parentValueProvenance || {}; state.possibleFetched = []; let count = 0; Object.entries(props).forEach(([name, node]) => { const publicationLabel = /country.*publication/i.test(`${name} ${node.title || ""}`); if ((publicationLabel && name !== "country_of_publication") || blocked.test(name) || blocked.test(node.title || "") || values[name] !== undefined || provenance[name]?.source === "manual") return; const childValues = items.map(item => { const value = item.verified_attributes[name]; return Array.isArray(value) && value.length === 1 && value[0] && Object.prototype.hasOwnProperty.call(value[0], "value") ? value[0].value : undefined; }); if (childValues.some(value => value === undefined || value === null || value === "") || !validAttribute(node, childValues[0])) return; const choices = enumValues(node); if (choices.length && !choices.includes(childValues[0])) return; const normalized = childValues.map(value => JSON.stringify(value).toLowerCase().replace(/\s+/g, " ")); if (!normalized.every(value => value === normalized[0])) return; values[name] = childValues[0]; provenance[name] = { source: "fetched", source_asins: items.map(item => item.asin), source_attribute_key: name, target_ptd_key: name, source_value: childValues[0], retrieved_at_utc: items[0].attributes_retrieved_at_utc }; count++; }); ["batteries_required", "supplier_declared_dg_hz_regulation"].forEach(name => { if (!props[name] || values[name] !== undefined || provenance[name]?.source === "manual") return; const childValues = items.map(item => { const value=item.verified_attributes[name]; return Array.isArray(value) && value.length === 1 ? value[0]?.value : undefined; }); const unanimous = childValues.length && childValues.every(value => value === childValues[0]) && (childValues[0] === false || childValues[0] === "not_applicable"); if (unanimous) { values[name] = childValues[0]; provenance[name] = { source: "fetched", source_asins: items.map(item => item.asin), source_attribute_key: name, target_ptd_key: name, source_value: childValues[0], retrieved_at_utc: items[0].attributes_retrieved_at_utc, compliance_confirmation_required: true, confirmed: false }; count++; } }); const childKeys = [...new Set(items.flatMap(item => Object.keys(item.verified_attributes || {})))]; Object.entries(props).forEach(([target, node]) => { if (values[target] !== undefined) return; const similarlyNamed = childKeys.filter(source => source !== target && (source.includes("country") && target.includes("country") || source.includes("dg") && target.includes("dg"))); if (similarlyNamed.length) state.possibleFetched.push({ target, source: similarlyNamed.join(", "), label: node.title || target }); }); state.parentValues = values; state.parentValueProvenance = provenance; if (count) { state.preview = null; invalidateApproval(); $("pc-detection-summary").insertAdjacentHTML("beforeend", `<div><b>Fetched values</b><strong>${count} fields populated from matching Amazon child data.</strong></div>`); } }

  async function discover() { const override = $("pc-product-type").value.trim(); if (!override) { $("pc-schema").textContent = "Enter a manual Product Type override first."; return; } state.items = state.items.map(item => ({ ...item, product_type: override })); await detectFamilyType(); }
  function review() { const groups = ["Ready", "Already Connected", "Needs Review", "Blocked"]; $("pc-summary").textContent = `${state.items.length} products entered · ${groups.map(group => `${state.items.filter(item => item.status === group).length} ${group.toLowerCase()}`).join(" · ")}`; $("pc-family").innerHTML = `<h3>${esc($("pc-parent-title").value || "New parent")} <small>${esc($("pc-parent-sku").value)}</small></h3><p>Variation theme: ${esc($("pc-theme").value)}</p>` + state.items.map(card).join(""); bindThumbnailHandlers(); }
  function payload() { const allowed=new Set(childAttributes($("pc-theme").value).map(field=>field.name)); const items=(state.items || []).map(item=>({ ...item, variation_values:Object.fromEntries(Object.entries(item.variation_values || {}).filter(([name])=>allowed.has(name)))})); return { ...scope(), id: state.draft?.id, parent_mode: $("pc-parent-mode").value, parent_sku: $("pc-parent-sku").value, parent_title: $("pc-parent-title").value, product_type: $("pc-product-type").value, variation_theme: $("pc-theme").value, parent_values: state.parentValues || {}, parent_value_provenance: state.parentValueProvenance || {}, confirmed_suggestions: state.confirmedSuggestions || [], items }; }
  function parentRequiredForm() { const schema = state.schemas.parent || {}; const relationship = Object.entries(schema.properties || {}).find(([name, node]) => /parent.*sku.*relationship/i.test(node.title || name)); const exclude = relationship && !(relationship[1].items?.properties || {}).parent_sku ? relationship[0] : null; const fields = (schema.required || []).filter(name => name !== exclude && !["parentage_level", "variation_theme", "item_name"].includes(name)); const output = $("pc-parent-required"); if (!fields.length) { output.innerHTML = ""; return; } output.innerHTML = `<section class="pc-preview-card"><h3>Parent required information</h3><p>Fields are from the active Parent PTD branch. No values are inferred from children.</p>${fields.map(name => { const node = schema.properties?.[name] || {}; const values = enumValues(node); const value = (state.parentValues || {})[name] || ""; const control = values.length ? `<select onchange="PC.parentValue('${esc(name)}',this.value)"><option value="">Select ${esc(node.title || friendlyTheme(name))}</option>${values.map(option => `<option value="${esc(option)}" ${option === value ? "selected" : ""}>${esc(option)}</option>`).join("")}</select>` : `<input value="${esc(value)}" onchange="PC.parentValue('${esc(name)}',this.value)">`; return `<label>${esc(node.title || friendlyTheme(name))}${control}</label>`; }).join("")}</section>`; }
  function previewMarkup(preview) {
    const attrs = value => Object.entries(value || {}).filter(([key]) => !["parentage_level", "variation_theme", "child_parent_sku_relationship"].includes(key)).map(([key, item]) => `${friendlyTheme(key)}: ${Array.isArray(item) ? JSON.stringify(item) : item}`).join(" · ") || "—";
    const errors = Object.entries(preview.validation.errors || {}).map(([group, values]) => `<div><b>${esc(group)}</b><ul>${values.map(error => `<li>${esc(error.field)} ${esc(error.message)} <details><summary>Technical path</summary><code>${esc(error.path)}</code></details></li>`).join("")}</ul></div>`).join("");
    const parent = preview.parent; const children = preview.children || [];
    return `<section class="pc-preview-card"><h3>${preview.validation.passed ? "Dry run passed" : "Not ready"}</h3><p>${preview.validation.passed ? "Ready for controlled submission. Amazon writes remain disabled." : "Complete the fields below, then return to edit. No job or submission was created."}</p>${errors}<h4>Parent</h4><p>SKU: ${esc(parent.sku)} · Title: ${esc($("pc-parent-title").value)} · Product Type: ${esc(parent.productType)} · Variation theme: ${esc($("pc-theme").value)} · Parentage level: parent</p><details><summary>Technical JSON preview</summary><pre>${esc(JSON.stringify(parent, null, 2))}</pre></details><h4>Children</h4>${children.map(child => `<div class="pc-preview-child">ASIN: ${esc(child.asin)} · Vendor SKU: ${esc(child.sku)} · ${esc(attrs(child.attributes))} · Intended parent SKU: ${esc(parent.sku)} · Current parent: ${esc(child.current_parent || "None")}<details><summary>Technical JSON preview</summary><pre>${esc(JSON.stringify(child, null, 2))}</pre></details></div>`).join("")}<h4>Submission plan</h4><ol>${preview.plan.map((step, index) => `<li>Step ${index + 1}: ${esc(step)}</li>`).join("")}</ol>${preview.validation.passed ? '<p class="pc-readonly-notice">Ready for controlled submission — read-only preview only.</p>' : '<button type="button" class="btn btn-outline-secondary" onclick="PC.go(2)">Return to edit</button>'}</section>`;
  }
  function previewMarkupV2(preview) { const errors = Object.entries(preview.validation.errors || {}).map(([group, values]) => `<div><b>${esc(group)}</b><ul>${values.map(error => `<li>${esc(error.field)} ${esc(error.message)} <details><summary>Technical details</summary><code>${esc(error.path)}</code></details></li>`).join("")}</ul></div>`).join(""); const parent = preview.parent; return `<section class="pc-preview-card"><h3>${preview.validation.passed ? "Dry run passed" : "Not ready"}</h3><p>${preview.validation.passed ? "Ready for controlled submission. Amazon writes remain disabled." : "Complete the listed parent information or return to edit. No job or submission was created."}</p>${errors}<h4>Parent — proposed putListingsItem</h4><p>SKU: ${esc(parent.sku)} · Product Type: ${esc(parent.body.productType)} · Variation theme: ${esc($("pc-theme").value)} · Parentage level: parent</p><details><summary>Technical JSON preview</summary><pre>${esc(JSON.stringify(parent.body, null, 2))}</pre></details><h4>Children — PATCH CHILD — existing listing attributes will not be replaced.</h4>${preview.children.map(child => `<div class="pc-preview-child"><b>${esc(child.sku)} / ${esc(child.asin)}</b><p>Existing on Amazon: ${esc(JSON.stringify(child.existing || {}))}</p><p>Proposed changes: ${child.body.patches.map(patch => `${esc(patch.op)} ${esc(patch.path)}`).join(" · ")}</p><p>Not touched: title, brand, identifiers, images, dimensions, compliance, cost, and price.</p><details><summary>Technical JSON preview</summary><pre>${esc(JSON.stringify(child.body, null, 2))}</pre></details></div>`).join("")}<h4>Submission plan</h4><ol>${preview.plan.map((step, index) => `<li>Step ${index + 1}: ${esc(step)}</li>`).join("")}</ol><p>Dry-run validation does not guarantee Amazon acceptance.</p>${preview.validation.passed ? '<span class="pc-readonly-notice">Ready for controlled submission — read-only preview only.</span>' : '<button type="button" class="btn btn-outline-secondary" onclick="PC.go(2)">Return to edit</button>'}</section>`; }
  async function previewPayload() { const output = $("pc-payload-preview"); output.innerHTML = '<div class="pc-preview-card">Building read-only preview…</div>'; try { const result = await api("/preview", { method: "POST", body: JSON.stringify({ data: payload(), parent_schema: state.schemas.parent, child_schema: state.schemas.child }) }); state.preview = result.preview; output.innerHTML = previewMarkupV2(state.preview); } catch (error) { output.innerHTML = `<div class="pc-inline-error">Could not build payload preview: ${esc(error.message)}</div>`; } }
  async function save() { try { state.draft = (await api("/drafts", { method: "POST", body: JSON.stringify({ data: payload() }) })).draft; alert("Draft saved."); } catch (error) { alert(error.message); } }
  async function jobs() { const data = await api("/jobs"); $("pc-jobs").innerHTML = data.items.length ? data.items.map(item => `<div class="pc-job"><b>${esc(item.status)}</b> · ${esc(item.marketplace_id)} · ${esc(item.vendor_code)}<br>Expected children: not submitted (writes disabled)</div>`).join("") : '<div class="pc-job">No submissions. Save a verified draft to preserve the workflow.</div>'; }
  function go(step) { const chosen = $("pc-theme").value; const names = chosen === "ColorSize" || chosen === "ColourSize" ? ["Colour", "Size"] : [friendlyTheme(chosen)]; const variationsComplete = state.items.filter(item => item.status === "Ready" || item.status === "Already Connected").every(item => names.every(name => (item.variation_values || {})[name])); if (step === 3 && (!state.familyType || !state.schemas.parent || !state.schemas.child || !chosen || !variationsComplete)) { $("pc-products").insertAdjacentHTML("afterbegin", '<div class="pc-inline-error">Resolve a consistent Product Type, both schemas, a Variation Theme, and required child variation values before Review.</div>'); return; } closePreviewModal(); state.step = step; document.querySelectorAll(".pc-step").forEach(element => element.hidden = +element.dataset.step !== step); document.querySelectorAll("[data-pc-step]").forEach(element => element.classList.toggle("active", +element.dataset.pcStep === step)); if (step === 3) review(); if (step === 4) jobs().catch(error => console.error("Parent–Child job refresh failed", error)); }

  function safeGo(step) { if (step === 3 && !state.validation?.canProceed) { cards(); return; } closePreviewModal(); state.step = step; document.querySelectorAll(".pc-step").forEach(element => element.hidden = +element.dataset.step !== step); document.querySelectorAll("[data-pc-step]").forEach(element => element.classList.toggle("active", +element.dataset.pcStep === step)); if (step === 3) review(); if (step === 4) jobs().catch(error => console.error("Parent–Child job refresh failed", error)); }
  function previewMarkupV2(preview) { const childRows = preview.children.map(item => `<section class="pc-preview-child"><h4>PATCH CHILD — ${esc(item.sku)} / ${esc(item.asin)}</h4><p>Existing parent: ${esc(item.current_parent || "None")}</p><table class="pc-existing-table"><tbody>${item.changes.map(change => `<tr><th>${esc(change.field)}</th><td>${esc(change.status)}</td></tr>`).join("")}</tbody></table><p>Total patch operations: ${item.body.patches.length}</p><details><summary>Technical existing attributes</summary><pre>${esc(JSON.stringify(item.existing || {}, null, 2))}</pre></details><details><summary>Technical JSON preview</summary><pre>${esc(JSON.stringify(item.body, null, 2))}</pre></details></section>`).join(""); const suggestions = (preview.shared_suggestions || []).length ? `<section class="pc-shared-values"><h4>Available from all selected children</h4>${preview.shared_suggestions.map(s => `<div><b>${esc(s.field)}</b> · ${esc(s.source_asins.join(", "))} · ${s.confirmed ? "Confirmed" : `<button type="button" class="btn btn-outline-secondary" onclick="PC.useShared('${esc(s.attribute)}',${JSON.stringify(s.value)})">Use this value</button>`}</div>`).join("")}</section>` : ""; return `<section class="pc-preview-card"><h3>${preview.validation.passed ? "Dry run passed" : "Not ready"}</h3>${suggestions}<h4>Parent — proposed putListingsItem</h4><details><summary>Technical JSON preview</summary><pre>${esc(JSON.stringify(preview.parent.body, null, 2))}</pre></details>${childRows}<p>Dry-run validation does not guarantee Amazon acceptance.</p></section>`; }
  function parentRequiredFormV2() { parentRequiredForm(); const form = $("pc-parent-required"); form.querySelectorAll("label").forEach(label => { const input = label.querySelector("input,select"); if (!input) return; const name = Object.keys(state.schemas.parent?.properties || {}).find(key => (state.schemas.parent.properties[key].title || friendlyTheme(key)) === label.firstChild?.textContent) || ""; const node = state.schemas.parent?.properties?.[name] || {}; const help = node.description ? document.createElement("small") : null; if (help) { help.className = "pc-meta"; help.textContent = node.description; label.append(help); } if (node.type === "boolean") { const select = document.createElement("select"); select.innerHTML = '<option value="">Select</option><option value="true">Yes</option><option value="false">No</option>'; select.value = String((state.parentValues || {})[name] ?? ""); select.onchange = () => PC.parentValue(name, select.value === "true"); input.replaceWith(select); } }); }
  async function previewPayloadSafe() { const output = $("pc-payload-preview"); output.innerHTML = '<div class="pc-preview-card">Building read-only preview…</div>'; try { const result = await api("/preview", { method: "POST", body: JSON.stringify({ data: payload(), parent_schema: state.schemas.parent, child_schema: state.schemas.child }) }); state.preview = result.preview; output.innerHTML = previewMarkupV2(state.preview); } catch (error) { if (error.payload?.status === "not_ready") { const messages = error.payload.errors.map(item => `<li>${esc(item.scope)}: ${esc(item.message)}</li>`).join(""); state.preview = error.payload.preview; output.innerHTML = `<div class="pc-inline-error"><b>Not ready</b><ul>${messages}</ul></div>`; } else output.innerHTML = `<div class="pc-inline-error">${esc(error.unexpectedResponse ? "Preview service returned an unexpected server response. Check application logs and retry." : error.message)}</div>`; } }
  function parentRequiredFormV3() { const schema = state.schemas.parent || {}; const props = schema.properties || {}; const relationship = Object.entries(props).find(([name, node]) => /parent.*sku.*relationship/i.test(node.title || name)); const exclude = relationship && !(relationship[1].items?.properties || {}).parent_sku ? relationship[0] : null; const names = [...new Set(["item_name", ...(schema.required || [])])].filter(name => props[name] && name !== exclude && !["parentage_level", "variation_theme"].includes(name)); const errors = new Map(((state.preview?.validation?.errors?.Parent) || []).map(error => [error.path.replace(/^\//, ""), error.message])); $("pc-parent-required").innerHTML = `<section class="pc-preview-card"><h3>Parent required information</h3><p>Fields are from the active Parent PTD branch. Values marked required must be completed before preview can be ready.</p>${names.map(name => { const node=props[name], value=name === "item_name" ? $("pc-parent-title").value : ((state.parentValues || {})[name] ?? ""), nested=node.items?.properties?.value || node, enumValuesForField=nested.enum || enumValues(node), bool=enumValuesForField.includes(true) || enumValuesForField.includes(false) || nested.type === "boolean", help=node.description || nested.description || "", error=errors.get(name) || ""; let control; if (name === "item_name") control=`<input data-pc-parent-title value="${esc(value)}" onchange="PC.parentTitle(this.value)">`; else if (bool) control=`<select onchange="PC.parentValue('${esc(name)}',this.value === '' ? '' : this.value === 'true')"><option value="">Select</option><option value="true" ${value === true ? "selected" : ""}>Yes</option><option value="false" ${value === false ? "selected" : ""}>No</option></select>`; else if (enumValuesForField.length) control=`<select onchange="PC.parentValue('${esc(name)}',this.value)"><option value="">Select</option>${enumValuesForField.map(option => `<option value="${esc(option)}" ${option === value ? "selected" : ""}>${esc(option)}</option>`).join("")}</select>`; else control=`<input value="${esc(value)}" onchange="PC.parentValue('${esc(name)}',this.value)">`; return `<label><span>${esc(node.title || friendlyTheme(name))} <em>*</em></span>${control}${help ? `<small>${esc(help)}</small>` : ""}${error ? `<small class="pc-field-error">${esc(error)}</small>` : ""}</label>`; }).join("")}</section>`; }
  async function previewPayloadSafe() { const output = $("pc-payload-preview"); output.innerHTML = '<div class="pc-preview-card">Building server-authoritative read-only preview…</div>'; try { const result = await api("/preview", { method: "POST", body: JSON.stringify({ data: payload() }) }); state.preview = result.preview; state.snapshot = result.snapshot; state.validationPreflightPassed=false; state.draft = { ...(state.draft || {}), id: result.snapshot?.draft_id || payload().id }; output.innerHTML = `${previewMarkupV2(state.preview)}${safetyChecklist(result.snapshot)}`; } catch (error) { state.snapshot = null; state.validationPreflightPassed=false; if (error.payload?.status === "not_ready") { state.preview = error.payload.preview; parentRequiredFormV3(); output.innerHTML = `<div class="pc-inline-error"><b>Not ready</b><ul>${error.payload.errors.map(item => `<li>${esc(item.scope)}: ${esc(item.message)}</li>`).join("")}</ul></div>`; } else output.innerHTML = `<div class="pc-inline-error">${esc(error.unexpectedResponse ? "Preview service returned an unexpected server response. Check application logs and retry." : error.message)}</div>`; } }
  function validationPreviewReady() { return Boolean(state.validationPreviewEnabled && state.snapshot?.id && state.validationPreflightPassed && !state.validationPreviewRunning); }
  function updateValidationPreviewButton() { const button=$("pc-amazon-validation"); if (!button) return; button.disabled=!validationPreviewReady(); button.title=state.validationPreviewRunning ? "Validation preview is already running" : !state.validationPreviewEnabled ? "Validation Mode is Off" : !state.validationPreflightPassed ? "Run a passed final preflight first" : "Run Amazon validation preview"; }
  function safetyChecklist(snapshot) { return `<section class="pc-preview-card"><h3>Final safety checklist</h3><ul><li>Server PTDs bound: ${snapshot ? "Yes" : "Pending"}</li><li>Full PTD validation passed: ${snapshot ? "Yes" : "Pending"}</li><li>Immutable payload hash: ${esc(snapshot?.payload_hash || "Pending")}</li><li>Parent SKU available: Run final preflight</li><li>Every child current parent confirmed None: Run final preflight</li><li>Listings and Catalog evidence fresh: Run final preflight</li><li>No active-job conflicts: writes remain disabled</li><li>Write authorization: unproven</li><li>Persistent Amazon writes: disabled</li></ul><p><b>Structurally ready — live write authorization not yet proven.</b></p><button type="button" class="btn btn-outline-secondary" ${snapshot ? "" : "disabled"} onclick="PC.preflight()">Run final preflight</button><button id="pc-amazon-validation" type="button" class="btn btn-outline-secondary" ${validationPreviewReady() ? "" : "disabled"} title="Validation Mode and a passed final preflight are required" onclick="PC.amazonValidate()">Run Amazon validation preview</button><div id="pc-preflight-result"></div><div id="pc-amazon-validation-result"></div></section>`; }
  async function runPreflight() { const target=$("pc-preflight-result"); if (!state.snapshot?.id) { if(target) target.textContent="A current snapshot is required."; return; } state.validationPreflightPassed=false; updateValidationPreviewButton(); target.textContent="Running GET-only final preflight…"; try { const result=await api("/preflight", {method:"POST",body:JSON.stringify({snapshot_id:state.snapshot.id})}); state.validationPreflightPassed=result.status === "passed"; target.innerHTML=`<b>${esc(result.status)}</b><pre>${esc(JSON.stringify(result.preflight.evidence,null,2))}</pre>`; } catch (error) { target.textContent=error.message || "Final preflight failed."; } finally { updateValidationPreviewButton(); } }
  function invalidateApproval() { if (!state.snapshot) return; const snapshotId=state.snapshot.id; state.snapshot=null; api(`/snapshots/${encodeURIComponent(snapshotId)}/stale`, {method:"POST",body:"{}"}).catch(() => {}); const output=$("pc-payload-preview"); if (output) output.innerHTML='<div class="pc-inline-error">Safety snapshot is stale. Regenerate the server-authoritative preview and final preflight.</div>'; }
  async function amazonValidate() { const output=$("pc-amazon-validation-result"); if (state.validationPreviewRunning) return; if (!state.snapshot?.id || !state.validationPreflightPassed) { if(output) output.textContent="A current snapshot and passed final preflight are required."; updateValidationPreviewButton(); return; } try { const mode=await api("/validation-mode"); state.validationPreviewEnabled=mode.enabled === true; if (!state.validationPreviewEnabled) { if(output) output.textContent="Validation Mode is Off. Enable it in the in-app mode card."; updateValidationPreviewButton(); return; } } catch(error) { if(output) output.textContent=`Could not verify Validation Mode: ${error.message}`; updateValidationPreviewButton(); return; } const answer=window.prompt("This sends the proposed requests to Amazon in VALIDATION_PREVIEW mode. Amazon should validate them without creating or changing listings. Type VALIDATE to continue."); if (answer !== "VALIDATE") { if(output) output.textContent="Validation preview was not requested."; return; } state.validationPreviewRunning=true; updateValidationPreviewButton(); if(output) output.textContent="Running Amazon VALIDATION_PREVIEW…"; try { const data=await api("/validation-preview",{method:"POST",body:JSON.stringify({snapshot_id:state.snapshot.id,confirmation:"VALIDATE"})}); if (!data || !data.status) throw Error("Validation preview returned an invalid response."); if(output) output.innerHTML=`<b>${esc(data.status)}</b><pre>${esc(JSON.stringify(data.results || [],null,2))}</pre><p>No listing was created or modified. This was Amazon VALIDATION_PREVIEW mode.</p>`; } catch(error) { if(output) output.textContent=error.message || "Validation preview failed safely."; } finally { state.validationPreviewRunning=false; updateValidationPreviewButton(); } }
  function card(item, index) { const lookup=item.lookup_details || {}, catalog=lookup.catalog || {}, listing=lookup.vendor_listing || {}; return `<article class="pc-card">${imageMarkup(item)}<div><h4>${esc(item.title || item.asin)}</h4><div class="pc-fields"><div class="pc-field"><b>ASIN</b>${esc(item.asin)}</div><div class="pc-field"><b>Vendor SKU</b>${esc(item.vendor_sku || "—")}</div><div class="pc-field"><b>Product Type</b>${esc(item.product_type || "—")}</div><div class="pc-field"><b>Current Parent</b>${esc(item.current_parent || "—")}</div></div>${variationFields(item,index)}<div class="pc-status">${esc(item.status)}</div><details class="pc-meta"><summary>Technical lookup details</summary>Catalog: ${esc(catalog.status)} · Vendor listing: ${esc(listing.status)}</details><button class="btn btn-outline-secondary" onclick="PC.remove(${index})">Remove</button></div></article>`; }
  function renderCard(item,index) { return card(item,index); }
  async function jobs() { const filter=$("pc-job-filter")?.value || ""; const data=await api(`/jobs${filter ? `?status=${encodeURIComponent(filter)}` : ""}`); $("pc-jobs").innerHTML=data.items.length ? data.items.map(item => `<article class="pc-job"><b>${esc(item.status)}</b> · Parent SKU: ${esc(item.parent_sku)}<br>Submitted: ${esc(item.submitted_at_utc || "Not submitted")} · Last checked: ${esc(item.last_checked_at_utc || "Never")} · Next check: ${esc(item.next_check_at_utc || "—")}<br>24–48 hour expectation; monitoring up to 72 hours.</article>`).join("") : '<div class="pc-job">No Parent–Child submissions have been made yet.</div>'; }
  function reviewGo(step) { safeGo(step); if (step === 3 && state.validation?.canProceed) parentRequiredFormV3(); }
  async function drafts(action, draftId) { const output = $("pc-saved-drafts"); if (action === "open") { const draft = (await api(`/drafts/${encodeURIComponent(draftId)}`)).draft; state.draft = draft; state.parentValues = draft.parent_values || {}; state.parentValueProvenance = draft.parent_value_provenance || {}; state.confirmedSuggestions = draft.confirmed_suggestions || []; state.items = draft.items || []; $("pc-marketplace").value = draft.marketplace_id; state.contexts = (await api(`/vendor-contexts?marketplace_id=${encodeURIComponent(draft.marketplace_id)}`)).items; syncVendors(); $("pc-vendor").value = draft.vendor_code; $("pc-parent-mode").value = draft.parent_mode || "new"; $("pc-parent-sku").value = draft.parent_sku || ""; $("pc-parent-title").value = draft.parent_title || ""; $("pc-product-type").value = draft.product_type || ""; $("pc-asins").value = state.items.map(item => item.asin).join("\n"); state.familyType = draft.product_type || null; state.preview = draft.preview || null; output.hidden = true; $("pc-product-type-status").textContent = "Cached child evidence restored; refresh is required before preview or preflight."; if (state.familyType) await loadSchemas(); if (draft.variation_theme) { $("pc-theme").value = draft.variation_theme; $("pc-theme-step2").value = draft.variation_theme; pruneVariationValues(draft.variation_theme); } cards(); return; } if (action === "duplicate") { await api(`/drafts/${encodeURIComponent(draftId)}/duplicate`, { method: "POST" }); return drafts(); } if (action === "archive") { await api(`/drafts/${encodeURIComponent(draftId)}/archive`, { method: "POST" }); return drafts(); } if (action === "delete") { if (!window.confirm("Permanently delete this saved draft? This cannot be undone.")) return; await api(`/drafts/${encodeURIComponent(draftId)}`, { method: "DELETE" }); return drafts(); } const result = await api("/drafts"); output.hidden = false; output.innerHTML = `<h3>Saved drafts</h3><table><thead><tr><th>Parent</th><th>Marketplace / Vendor</th><th>Type / Theme</th><th>Children</th><th>Status</th><th>Last edited</th><th>Actions</th></tr></thead><tbody>${result.items.map(draft => `<tr><td>${esc(draft.parent_sku)}<br>${esc(draft.parent_title)}</td><td>${esc(draft.marketplace_id)}<br>${esc(draft.vendor_code)}</td><td>${esc(draft.product_type)}<br>${esc(draft.variation_theme)}</td><td>${esc(draft.child_count)}</td><td>${esc(draft.status)}</td><td>${esc(draft.updated_at_utc)}</td><td><button onclick="PC.drafts('open','${esc(draft.id)}')">Open and continue</button> <button onclick="PC.drafts('duplicate','${esc(draft.id)}')">Duplicate</button> <button onclick="PC.drafts('archive','${esc(draft.id)}')">Archive</button> <button onclick="PC.drafts('delete','${esc(draft.id)}')">Delete</button></td></tr>`).join("") || '<tr><td colspan="7">No saved drafts</td></tr>'}</tbody></table>`; }
  function parentSubmissionPanel() { if (!state.snapshot) return ""; const p=state.preview?.parent || {}; return `<section class="pc-parent-submit"><h3>Create parent on Amazon</h3><p>Parent SKU: ${esc(p.sku || "")}</p><p>Marketplace: UAE · Vendor code: FK6EI<br>Product type: ${esc(p.body?.productType || "")} · Variation theme: ${esc($("pc-theme").value)}<br>Snapshot hash: ${esc(state.snapshot.payload_hash)}</p><p>Amazon validation status and expiry: server verified. Final-preflight timestamp: server verified.</p><p>Children will not be attached in this step.</p><p>This action creates the parent listing on Amazon.</p><label>Type CREATE PARENT <input id="pc-create-parent-confirmation" oninput="PC.parentReadiness()"></label><button id="pc-create-parent" class="btn" disabled onclick="PC.createParent()">Create parent on Amazon</button><div id="pc-parent-submit-result"></div></section>`; }
  function parentSubmissionPanel() { if (!state.snapshot) return ""; const p=state.preview?.parent || {}; return `<section class="pc-parent-submit"><h3>Create parent on Amazon</h3><p>Parent SKU: ${esc(p.sku || "")}</p><p>Marketplace: UAE / A2VIGQ35RCS4UG · Vendor code: FK6EI<br>Product type: ${esc(p.body?.productType || "")} · Variation theme: ${esc($("pc-theme").value)}<br>Snapshot hash: ${esc(state.snapshot.payload_hash)}</p><div id="pc-parent-evidence">Amazon validation result: Not available<br>Amazon validation completed: Not available<br>Amazon validation expiry: Not available<br>Final preflight result: Not available<br>Final preflight completed: Not available<br>Snapshot expiry: Not available</div><p>Children will not be attached in this step.</p><p>This action creates the parent listing on Amazon.</p><label>Type CREATE PARENT <input id="pc-create-parent-confirmation" oninput="PC.parentReadiness()"></label><button id="pc-create-parent" class="btn" disabled onclick="PC.createParent()">Create parent on Amazon</button><div id="pc-parent-submit-result"></div></section>`; }
  async function parentReadiness() { if (!state.snapshot) return; const b=$("pc-create-parent"); if (!b) return; try { const r=await api(`/parent-submission/readiness?snapshot_id=${encodeURIComponent(state.snapshot.id)}`); const evidence=r.validation_evidence || []; const validTime=value=>typeof value === "string" && Number.isFinite(Date.parse(value)); const future=value=>validTime(value) && Date.parse(value)>Date.now(); const successful=evidence.length>0 && evidence.every(x=>["PASSED","PASSED_WITH_WARNINGS"].includes(x.classification)); const validationComplete=evidence.length>0 && evidence.every(x=>validTime(x.completed_at_utc)); const validationFresh=evidence.length>0 && evidence.every(x=>future(x.expires_at_utc)); const preflightPassed=r.final_preflight_status === "passed"; const preflightComplete=validTime(r.final_preflight_at_utc); const snapshotFresh=future(r.snapshot_expires_at_utc); const hash=typeof r.payload_hash === "string" && r.payload_hash.length>0; const safe=Boolean(r.gates?.ready) && successful && validationComplete && validationFresh && preflightPassed && preflightComplete && snapshotFresh && hash && $("pc-create-parent-confirmation").value === "CREATE PARENT"; b.disabled=!safe; const expiryText=!evidence.length ? "Not available" : !evidence.every(x=>validTime(x.expires_at_utc)) ? "Invalid" : !validationFresh ? "Expired — run again." : evidence.map(x=>x.expires_at_utc).join(", "); const completeText=!evidence.length ? "Not available" : !validationComplete ? "Invalid" : evidence.map(x=>x.completed_at_utc).join(", "); $("pc-parent-evidence").innerHTML=`Amazon validation result: ${esc(evidence.map(x=>x.classification).join(", ") || "Not available")}<br>Amazon validation completed: ${esc(completeText)}<br>Amazon validation expiry: ${esc(expiryText)}<br>Final preflight result: ${esc(r.final_preflight_status || "Not available")}<br>Final preflight completed: ${esc(!r.final_preflight_at_utc ? "Not available" : preflightComplete ? r.final_preflight_at_utc : "Invalid")}<br>Snapshot expiry: ${esc(!r.snapshot_expires_at_utc ? "Not available" : validTime(r.snapshot_expires_at_utc) ? snapshotFresh ? r.snapshot_expires_at_utc : "Expired" : "Invalid")}`; const out=$("pc-parent-submit-result"); if(out) out.textContent=safe ? "" : "Unavailable until all server evidence is current and complete."; } catch (_) { b.disabled=true; } }
  function validJobId(value) { return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value); }
  async function openJobDetail(jobId) { if (!validJobId(jobId)) throw new Error("Submission status is uncertain. Open the tracker to locate the job; do not retry."); go(4); const detail=await api(`/jobs/${encodeURIComponent(jobId)}`); const job=detail.job; $("pc-jobs").innerHTML=`<article class="pc-job"><h4>Submitted parent job</h4><b>${esc(job.status)}</b><br>Job ID: ${esc(job.id)}<br>Parent SKU: ${esc(job.parent_sku)}<br>Submitted: ${esc(job.submitted_at_utc || "pending")}</article>`; }
  async function createParent() { if (!state.snapshot) return; const b=$("pc-create-parent"); b.disabled=true; const snapshotId=state.snapshot.id; try { const r=await api("/parent-submission",{method:"POST",body:JSON.stringify({snapshot_id:snapshotId,confirmation:$("pc-create-parent-confirmation").value})}); state.snapshot=null; if (!validJobId(r.job_id)) { $("pc-parent-submit-result").textContent="Submission status is uncertain. Open the tracker to locate the job; do not retry."; go(4); return; } await openJobDetail(r.job_id); } catch(e) { const out=$("pc-parent-submit-result"); if(out) out.textContent=e.message; } }
  async function validationMode(action) { if (action === "enable") { const confirmation=window.prompt("This allows Amazon VALIDATION_PREVIEW requests only. It does not create or modify listings. Type ENABLE VALIDATION to continue."); if (confirmation !== "ENABLE VALIDATION") return; await api("/validation-mode/enable",{method:"POST",body:JSON.stringify({confirmation})}); } else if (action === "disable") await api("/validation-mode/disable",{method:"POST",body:"{}"}); const mode=await api("/validation-mode"); state.validationPreviewEnabled=mode.enabled === true; updateValidationPreviewButton(); const card=$("pc-validation-mode"); if(card) card.innerHTML=mode.enabled ? `<b>Status: On</b> · Amazon schema validation is available. Persistent writes remain disabled. <button class="btn btn-outline-secondary" onclick="PC.validationMode('disable')">Disable validation mode</button>` : `<b>Status: Off</b> · Validation requests cannot be sent. <button class="btn btn-outline-secondary" onclick="PC.validationMode('enable')">Enable validation mode</button>`; }
  async function parentCreationMode(action) { const output=$("pc-parent-creation-mode"); if (!state.snapshot?.id) return; try { if(action === 'arm') { const confirmation=window.prompt('Type ARM PARENT CREATION to arm one parent PUT for this exact validated snapshot.'); if(confirmation !== 'ARM PARENT CREATION') { if(output) output.textContent='Parent Creation Mode remains disarmed.'; return; } await api('/parent-creation-mode/arm',{method:'POST',body:JSON.stringify({snapshot_id:state.snapshot.id,confirmation})}); } else if(action === 'disarm') await api('/parent-creation-mode/disarm',{method:'POST',body:JSON.stringify({snapshot_id:state.snapshot.id,confirmation:''})}); const mode=await api(`/parent-creation-mode?snapshot_id=${encodeURIComponent(state.snapshot.id)}`); if(output) output.innerHTML=mode.armed ? `<b>Parent Creation Mode: Armed</b><br>Parent SKU: ${esc(mode.arm.parent_sku)}<br>Snapshot hash: ${esc(mode.arm.payload_hash)}<br>Arm expiry: ${esc(mode.arm.expires_at_utc)}<br>One parent PUT only. Children will not be attached. <button class="btn btn-outline-secondary" onclick="PC.parentCreationMode('disarm')">Disarm</button>` : `<b>Parent Creation Mode: Disarmed</b><br>Disable Validation Mode, then arm Parent Creation for this validated snapshot. <button class="btn btn-outline-secondary" onclick="PC.parentCreationMode('arm')">Arm parent creation</button>`; await parentReadiness(); } catch(error) { if(output) output.textContent=error.message || 'Parent Creation Mode request failed safely.'; } }
  function renderParentSubmissionPanel() { const target=$("pc-payload-preview"); if (target && state.snapshot && !$("pc-create-parent")) { target.insertAdjacentHTML("beforeend", `<section id="pc-validation-mode" class="pc-preview-card">Amazon validation mode: loading…</section><section id="pc-parent-creation-mode" class="pc-preview-card">Parent Creation Mode: loading…</section>${parentSubmissionPanel()}`); const result=$("pc-parent-submit-result"); if (result) result.textContent="Unavailable until a fresh preview, final preflight, validation evidence, and all server safety gates pass."; parentReadiness(); validationMode(); parentCreationMode(); } }
  setTimeout(() => { if (window.PC) { window.PC.validationMode=validationMode; window.PC.parentCreationMode=parentCreationMode; } }, 0);
  return { init, go: reviewGo, save, drafts, refreshJobs: () => jobs(), previewPayload: async () => { await previewPayloadSafe(); renderParentSubmissionPanel(); }, preflight: runPreflight, amazonValidate, parentReadiness, createParent, parentTitle: value => { $("pc-parent-title").value = value; state.preview = null; invalidateApproval(); parentRequiredFormV3(); }, parentValue: (name, value) => { state.parentValues = { ...(state.parentValues || {}), [name]: value }; state.preview = null; invalidateApproval(); parentRequiredFormV3(); }, useShared: (name, value) => { state.parentValues = { ...(state.parentValues || {}), [name]: value }; state.preview = null; invalidateApproval(); parentRequiredFormV3(); }, keepGroup, retrySchema: async () => { await loadSchemas(true); pruneVariationValues($("pc-theme").value); cards(); }, remove: index => { state.items.splice(index, 1); invalidateApproval(); cards(); }, variation: (index, name, value) => { if (!childAttributes($("pc-theme").value).some(field=>field.name===name)) return; state.items[index].variation_values = { ...(state.items[index].variation_values || {}), [name]: value }; state.preview = null; invalidateApproval(); cards(); }, image: openPreviewModal, closeImage: closePreviewModal };
  function previewValidationErrors(items) { return `<div class="pc-inline-error"><b>Not ready</b><ul>${items.map(item => { const detail = item.technical || {}; return `<li>${esc(item.scope)}: ${esc(item.message)}<details><summary>Technical details</summary><div>Layer: ${esc(detail.layer || "Unknown")}</div><div>Object: ${esc(detail.object || "Unknown")}</div><div>Instance path: ${esc(detail.instance_path || "/")}</div><div>Schema path: ${esc(detail.schema_path || "/")}</div>${detail.condition_path ? `<div>Conditional branch: ${esc(detail.condition_path)}</div>` : ""}${detail.ptd_inconsistency ? `<div>${esc(detail.ptd_inconsistency)}</div>` : ""}</details></li>`; }).join("")}</ul></div>`; }
  async function previewPayloadSafe() { const output = $("pc-payload-preview"); output.innerHTML = '<div class="pc-preview-card">Building server-authoritative read-only previewâ€¦</div>'; try { const result = await api("/preview", { method: "POST", body: JSON.stringify({ data: payload() }) }); state.preview = result.preview; state.snapshot = result.snapshot; state.draft = { ...(state.draft || {}), id: result.snapshot?.draft_id || payload().id }; output.innerHTML = `${previewMarkupV2(state.preview)}${safetyChecklist(result.snapshot)}`; } catch (error) { state.snapshot = null; if (error.payload?.status === "not_ready") { state.preview = error.payload.preview; parentRequiredFormV3(); output.innerHTML = previewValidationErrors(error.payload.errors || []); } else output.innerHTML = `<div class="pc-inline-error">${esc(error.unexpectedResponse ? "Preview service returned an unexpected server response. Check application logs and retry." : error.message)}</div>`; } }
  function parentRequiredFormV3() { const schema=state.schemas.parent || {}, props=schema.properties || {}, values=state.parentValues || {}, provenance=state.parentValueProvenance || {}; const names=[...new Set(["item_name", ...(schema.required || [])])].filter(name => props[name] && !["parentage_level","variation_theme","child_parent_sku_relationship"].includes(name)); const field = name => { const node=props[name], value=name === "item_name" ? $("pc-parent-title").value : (values[name] ?? ""), choices=enumValues(node), bool=choices.includes(true) || choices.includes(false), info=provenance[name], control=name === "item_name" ? `<input value="${esc(value)}" onchange="PC.parentTitle(this.value)">` : bool ? `<select onchange="PC.parentValue('${esc(name)}',this.value === 'true' ? true : this.value === 'false' ? false : '')"><option value="">Select</option><option value="true" ${value === true ? "selected" : ""}>Yes</option><option value="false" ${value === false ? "selected" : ""}>No</option></select>` : choices.length ? `<select onchange="PC.parentValue('${esc(name)}',this.value)"><option value="">Select</option>${choices.map(choice => `<option value="${esc(choice)}" ${choice === value ? "selected" : ""}>${esc(choice)}</option>`).join("")}</select>` : `<input value="${esc(value)}" onchange="PC.parentValue('${esc(name)}',this.value)">`; const fetched=info?.source === "fetched" ? `<small>Fetched from ${esc((info.source_asins || []).join(" and "))}</small><details><summary>Source details</summary><div>Child attribute key: ${esc(info.source_attribute_key)}</div><div>Parent PTD key: ${esc(info.target_ptd_key)}</div><div>Value: ${esc(JSON.stringify(info.source_value))}</div><div>Retrieved: ${esc(info.retrieved_at_utc)}</div></details>${info.compliance_confirmation_required ? `<label><input type="checkbox" onchange="PC.parentValue('__compliance_confirmation',this.checked)"> I confirm these parent-level answers match all selected child products.</label>` : ""}` : ""; return `<label><span>${esc(node.title || friendlyTheme(name))}${name !== "item_name" ? " *" : ""}</span>${control}${fetched}</label>`; }; $("pc-parent-required").innerHTML=`<section class="pc-preview-card"><h3>Parent required information</h3>${names.map(field).join("")}${(state.possibleFetched || []).map(item => `<p>Possible matching child value available for ${esc(item.label)}: child key ${esc(item.source)}; Parent PTD key ${esc(item.target)}. Select it explicitly to use it.</p>`).join("")}</section>`; }
  function populateFetchedParentValues() { const props=state.schemas.parent?.properties || {}, items=state.items || {}, values=state.parentValues || {}, provenance=state.parentValueProvenance || {}; if (!Array.isArray(items) || !items.length || items.some(item => item.attributes_status !== 200 || !item.verified_attributes)) return; let count=0; Object.entries(props).forEach(([key,node]) => { if (values[key] !== undefined || provenance[key]?.source === "manual" || /price|cost|identifier|dimension|weight|manufacturer|feature/i.test(key)) return; const source=items.map(item => item.verified_attributes[key]); if (source.some(value => !Array.isArray(value) || value.length !== 1 || !Object.prototype.hasOwnProperty.call(value[0] || {},"value"))) return; const valuesFromChildren=source.map(value => value[0].value); if (!valuesFromChildren.every(value => JSON.stringify(value).toLowerCase().replace(/\s+/g," ") === JSON.stringify(valuesFromChildren[0]).toLowerCase().replace(/\s+/g," "))) return; if (!validAttribute(node, valuesFromChildren[0]) || (enumValues(node).length && !enumValues(node).includes(valuesFromChildren[0]))) return; if ((key === "batteries_required" && valuesFromChildren[0] !== false) || (key === "supplier_declared_dg_hz_regulation" && valuesFromChildren[0] !== "not_applicable")) return; values[key]=valuesFromChildren[0]; provenance[key]={source:"fetched",source_asins:items.map(item=>item.asin),source_attribute_key:key,target_ptd_key:key,source_value:valuesFromChildren[0],retrieved_at_utc:items[0].attributes_retrieved_at_utc,compliance_confirmation_required:key === "batteries_required" || key === "supplier_declared_dg_hz_regulation",confirmed:false}; count++; }); state.parentValues=values; state.parentValueProvenance=provenance; if(count){state.preview=null;invalidateApproval();} }
})();
