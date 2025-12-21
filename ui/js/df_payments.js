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
        const unpaid = formatAmount(row.unpaid_amount);
        return `<tr><td>${month}</td><td style="text-align:right;">${unpaid}</td></tr>`;
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

    renderStatus(state, orders.length);
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
    const lookbackSel = document.getElementById("dfp-lookback");
    const lookback = lookbackSel ? Number(lookbackSel.value || 90) : 90;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Fetching…";
    }
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
    }
  }

  function initDfPaymentsTab() {
    if (dfpInitialized) return;
    const btn = document.getElementById("dfp-fetch-btn");
    if (btn) {
      btn.addEventListener("click", triggerFetch);
    }
    dfpInitialized = true;
  }

  window.initDfPaymentsTab = initDfPaymentsTab;
  window.loadDfPaymentsState = loadDfPaymentsState;
})();
