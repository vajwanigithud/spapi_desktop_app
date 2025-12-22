(() => {
  let dfpInitialized = false;
  let dfpLoading = false;

  const numberFormatter = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  function safe(str) {
    if (str === undefined || str === null) return "";
    return String(str);
  }

  function escapeHtmlLocal(str) {
    return safe(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function truncateText(str, max = 80) {
    const s = safe(str);
    if (s.length <= max) return s;
    return `${s.slice(0, max - 3)}...`;
  }

  function formatAmount(amount) {
    const value = Number(amount || 0);
    return numberFormatter.format(value);
  }

  function formatUaeLabel(ts) {
    if (!ts) return "—";
    if (typeof formatUaeTime === "function") {
      return `${formatUaeTime(ts)} UAE`;
    }
    return `${ts} UAE`;
  }

  function setStatus(message, isError = false) {
    const el = document.getElementById("dfp-status");
    if (!el) return;
    el.textContent = message;
    el.style.color = isError ? "#b91c1c" : "var(--muted)";
  }

  function renderStatus(state, rowsCount) {
    const lastFetch = state?.last_fetch_finished_at || state?.last_fetch_started_at;
    const lastLabel = lastFetch ? formatUaeLabel(lastFetch) : "—";
    const rowsLabel = rowsCount != null ? rowsCount : state?.rows_90d || 0;
    const errorLabel = state?.last_error ? safe(state.last_error) : "None";
    setStatus(`Last fetch: ${lastLabel} | Rows (90d): ${rowsLabel} | Error: ${errorLabel}`);

    const autoEl = document.getElementById("dfp-auto-status");
    if (autoEl) {
      const lastInc = state?.incremental_last_success_at_utc
        || state?.last_incremental_finished_at
        || state?.last_incremental_started_at;
      const nextAuto = state?.incremental_next_eligible_at_utc;
      const nextLabel = nextAuto ? formatUaeLabel(nextAuto) : "—";
      const statusLabel = state?.incremental_worker_status || state?.last_incremental_status || "—";
      const reason = state?.incremental_wait_reason || state?.incremental_worker_details || "";
      const enabled = state?.incremental_auto_enabled !== false;
      autoEl.textContent = enabled
        ? `Auto incremental: ${statusLabel} | last: ${lastInc ? formatUaeLabel(lastInc) : "—"} | next: ${nextLabel}${reason ? ` (${reason})` : ""}`
        : "Auto incremental: disabled until baseline Fetch Orders runs";
    }
  }

  function renderDiagnostics(diagnostics) {
    const el = document.getElementById("dfp-range");
    if (!el) return;
    if (!diagnostics) {
      el.textContent = "Loaded: — orders | Range: — → — (UTC)";
      return;
    }
    const count = diagnostics.orders_count != null ? diagnostics.orders_count : "—";
    const minDate = diagnostics.min_order_date_utc || "—";
    const maxDate = diagnostics.max_order_date_utc || "—";
    const pages = diagnostics.pages_fetched != null ? diagnostics.pages_fetched : "—";
    const lookback = diagnostics.lookback_days_applied != null ? diagnostics.lookback_days_applied : "—";
    el.textContent = `Loaded: ${count} orders | Range: ${minDate} → ${maxDate} (UTC) | Pages: ${pages} | Lookback applied: ${lookback}d`;
  }

  function renderInvoices(invoices) {
    const tbody = document.getElementById("dfp-invoices-body");
    if (!tbody) return;
    if (!invoices || !invoices.length) {
      tbody.innerHTML = '<tr><td colspan="2" class="empty">No invoices yet</td></tr>';
      return;
    }
    tbody.innerHTML = invoices
      .map(row => {
        const month = escapeHtmlLocal(row.month || "—");
        const total = formatAmount(row.total_incl_vat);
        return `<tr><td>${month}</td><td style="text-align:right;">${total}</td></tr>`;
      })
      .join("");
  }

  function renderCashflow(rows) {
    const tbody = document.getElementById("dfp-cashflow-body");
    if (!tbody) return;
    if (!rows || !rows.length) {
      tbody.innerHTML = '<tr><td colspan="2" class="empty">Projection unavailable</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map(row => {
        const month = escapeHtmlLocal(row.month || "—");
        const amountRaw = row.expected_payment_amount != null ? row.expected_payment_amount : row.unpaid_amount;
        const amount = formatAmount(amountRaw);
        return `<tr><td>${month}</td><td style="text-align:right;">${amount}</td></tr>`;
      })
      .join("");
  }

  function renderOrders(orders) {
    const tbody = document.getElementById("dfp-orders-body");
    if (!tbody) return;
    if (!orders || !orders.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="empty">No DF orders yet</td></tr>';
      return;
    }

    const rows = orders.map(order => {
      const po = escapeHtmlLocal(order.purchase_order_number || "—");
      const date = escapeHtmlLocal(order.order_date_utc || "—");
      const status = escapeHtmlLocal(order.order_status || "—");
      const units = Number(order.total_units || 0);
      const subtotal = formatAmount(order.subtotal_amount);
      const vat = formatAmount(order.vat_amount);
      const currency = escapeHtmlLocal(order.currency_code || "AED");
      const skuList = safe(order.sku_list || "");
      const truncated = truncateText(skuList, 80);

      return `
        <tr>
          <td>${po}</td>
          <td>${date}</td>
          <td>${status}</td>
          <td style="text-align:right;">${units}</td>
          <td style="text-align:right;">${subtotal}</td>
          <td style="text-align:right;">${vat}</td>
          <td>${currency}</td>
          <td title="${escapeHtmlLocal(skuList)}"><span class="dfp-sku">${escapeHtmlLocal(truncated)}</span></td>
        </tr>
      `;
    });

    tbody.innerHTML = rows.join("");
  }

  function renderState(data) {
    const orders = data?.orders || [];
    const dashboard = data?.dashboard || {};
    const state = data?.state || {};
    const diagnostics = state?.diagnostics;

    renderStatus(state, orders.length);
    renderDiagnostics(diagnostics);
    renderInvoices(dashboard.invoices_by_month || []);
    renderCashflow(dashboard.cashflow_projection || []);
    renderOrders(orders);
  }

  async function loadDfPaymentsState() {
    if (dfpLoading) return;
    dfpLoading = true;
    setStatus("Loading DF Payments…");
    try {
      const resp = await fetch("/api/df-payments/state");
      if (!resp.ok) {
        const errText = await resp.text().catch(() => "");
        throw new Error(errText || `HTTP ${resp.status}`);
      }
      const data = await resp.json();
      renderState(data);
    } catch (err) {
      setStatus(`Error: ${err.message}`, true);
    } finally {
      dfpLoading = false;
    }
  }

  async function triggerFetch() {
    const btn = document.getElementById("dfp-fetch-btn");
    const incBtn = document.getElementById("dfp-incremental-btn");
    const lookbackSel = document.getElementById("dfp-lookback");
    const lookback = lookbackSel ? Number(lookbackSel.value || 90) : 90;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Fetching…";
    }
    if (incBtn) incBtn.disabled = true;
    setStatus("Fetching DF orders…");
    try {
      const resp = await fetch("/api/df-payments/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lookback_days: lookback }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data?.ok === false) {
        const detail = data?.detail || data?.error || resp.statusText;
        throw new Error(detail || `HTTP ${resp.status}`);
      }
      await loadDfPaymentsState();
      setStatus("DF Payments refreshed");
    } catch (err) {
      setStatus(`Error: ${err.message}`, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Fetch Orders";
      }
      if (incBtn) incBtn.disabled = false;
    }
  }

  async function triggerIncremental() {
    const btn = document.getElementById("dfp-incremental-btn");
    const fetchBtn = document.getElementById("dfp-fetch-btn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Scanning…";
    }
    if (fetchBtn) fetchBtn.disabled = true;
    setStatus("Incremental scan in progress…");
    try {
      const resp = await fetch("/api/df-payments/incremental", { method: "POST" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data?.ok === false) {
        const detail = data?.detail || data?.error || resp.statusText;
        throw new Error(detail || `HTTP ${resp.status}`);
      }

      const status = (data.status || "").toLowerCase();
      if (status === "incremental_refreshed") {
        setStatus(`Incremental scan: +${data.orders_upserted ?? 0} orders`);
      } else if (status === "cooldown") {
        const next = data.next_eligible_utc ? formatUaeLabel(data.next_eligible_utc) : "later";
        setStatus(`Incremental scan: cooldown until ${next}`);
      } else if (status === "waiting" && (data.reason || "") === "baseline_required") {
        setStatus("Incremental scan: Run Fetch Orders once to enable auto scans");
      } else if (status === "locked") {
        setStatus("Incremental scan already running");
      } else {
        setStatus(`Incremental scan status: ${status || "unknown"}`);
      }
      await loadDfPaymentsState();
    } catch (err) {
      setStatus(`Error: ${err.message}`, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Incremental Scan";
      }
      if (fetchBtn) fetchBtn.disabled = false;
    }
  }

  function initDfPaymentsTab() {
    if (dfpInitialized) return;
    const btn = document.getElementById("dfp-fetch-btn");
    if (btn) {
      btn.addEventListener("click", triggerFetch);
    }
    const incBtn = document.getElementById("dfp-incremental-btn");
    if (incBtn) {
      incBtn.addEventListener("click", triggerIncremental);
    }
    dfpInitialized = true;
  }

  window.initDfPaymentsTab = initDfPaymentsTab;
  window.loadDfPaymentsState = loadDfPaymentsState;
})();
