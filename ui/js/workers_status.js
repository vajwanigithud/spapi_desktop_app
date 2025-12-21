(function () {
  const WAITING_STATUSES = new Set(["cooldown", "locked"]);

  function statusIcon(status) {
    const value = (status || "").toLowerCase();
    if (value === "error") return "🔴";
    if (WAITING_STATUSES.has(value)) return "🟡";
    if (value === "ok") return "🟢";
    return "⚪";
  }

  function collectWorkers(data) {
    if (!data || !data.domains) return [];
    const list = [];
    Object.values(data.domains).forEach((domain) => {
      if (domain && Array.isArray(domain.workers)) {
        domain.workers.forEach((w) => {
          if (w && typeof w === "object") {
            list.push(w);
          }
        });
      }
    });
    return list;
  }

  function computeLabel(data) {
    const summary = data && data.summary;
    const workers = collectWorkers(data);
    const waitingCount = summary && typeof summary.waiting_count === "number"
      ? summary.waiting_count
      : workers.filter((w) => WAITING_STATUSES.has((w.status || "").toLowerCase())).length;
    const errorCount = summary && typeof summary.error_count === "number"
      ? summary.error_count
      : workers.filter((w) => (w.status || "").toLowerCase() === "error").length;
    const overall = (summary && summary.overall) || (errorCount ? "error" : waitingCount ? "waiting" : "ok");

    if (overall === "error" || errorCount > 0) {
      return "🛠 Workers: ERROR";
    }
    if (overall === "waiting" || waitingCount > 0) {
      const count = waitingCount || 1;
      return `🛠 Workers: ${count} Waiting`;
    }
    return "🛠 Workers: OK";
  }

  function renderDomain(container, domain, fallbackTitle) {
    if (!container) return;
    container.innerHTML = "";
    const title = document.createElement("div");
    title.style.fontWeight = "700";
    title.style.marginBottom = "6px";
    title.textContent = domain?.title || fallbackTitle;
    container.appendChild(title);

    const workers = (domain && Array.isArray(domain.workers)) ? domain.workers : [];
    if (!workers.length) {
      const empty = document.createElement("div");
      empty.className = "muted-text";
      empty.style.fontSize = "12px";
      empty.textContent = "No workers";
      container.appendChild(empty);
      return;
    }

    workers.forEach((worker) => {
      const row = document.createElement("div");
      row.className = "worker-row";

      const left = document.createElement("div");
      const titleEl = document.createElement("div");
      titleEl.className = "worker-title";
      titleEl.textContent = `${statusIcon(worker.status)} ${worker.name || worker.key || "Worker"}`;
      left.appendChild(titleEl);

      if (worker.what) {
        const what = document.createElement("div");
        what.className = "worker-what";
        what.textContent = worker.what;
        left.appendChild(what);
      }

      if (worker.details) {
        const details = document.createElement("div");
        details.className = "worker-details";
        details.textContent = worker.details;
        left.appendChild(details);
      }

      const meta = document.createElement("div");
      meta.className = "worker-meta";
      const last = document.createElement("div");
      last.textContent = `Last run: ${worker.last_run_at_uae || "—"}`;
      const next = document.createElement("div");
      next.textContent = `Next: ${worker.next_eligible_at_uae || "—"}`;
      meta.appendChild(last);
      meta.appendChild(next);

      row.appendChild(left);
      row.appendChild(meta);
      container.appendChild(row);
    });
  }

  function initWorkersStatus() {
    const button = document.getElementById("workers-status-btn");
    const backdrop = document.getElementById("workers-modal");
    const closeBtn = document.getElementById("workers-close-btn");
    const refreshBtn = document.getElementById("workers-refresh-btn");
    const heartbeatEl = document.getElementById("workers-heartbeat");
    const lastCheckedEl = document.getElementById("workers-last-checked");
    const errorEl = document.getElementById("workers-error");
    const sectionInventory = document.getElementById("workers-section-inventory");
    const sectionRtSales = document.getElementById("workers-section-rt-sales");
    const sectionVendorPo = document.getElementById("workers-section-vendor-po");

    if (!button || !backdrop || !heartbeatEl || !lastCheckedEl) {
      return;
    }

    function clearSections(message) {
      [sectionInventory, sectionRtSales, sectionVendorPo].forEach((section) => {
        if (section) {
          section.innerHTML = "";
          if (message) {
            const text = document.createElement("div");
            text.className = "muted-text";
            text.style.fontSize = "12px";
            text.textContent = message;
            section.appendChild(text);
          }
        }
      });
    }

    async function fetchStatus(showLoading) {
      if (showLoading && errorEl) {
        errorEl.style.display = "none";
        errorEl.textContent = "";
      }
      try {
        const resp = await fetch("/api/workers/status");
        const data = await resp.json();
        button.textContent = computeLabel(data);
        heartbeatEl.textContent = data.ok ? "System heartbeat: OK" : "System heartbeat: Check logs";
        if (lastCheckedEl) {
          lastCheckedEl.textContent = `Last checked: ${data.checked_at_uae || "-"}`;
        }
        if (errorEl) {
          errorEl.style.display = "none";
          errorEl.textContent = "";
        }
        renderDomain(sectionInventory, data.domains?.inventory, "Inventory");
        renderDomain(sectionRtSales, data.domains?.rt_sales, "REAL-TIME SALES");
        renderDomain(sectionVendorPo, data.domains?.vendor_po, "VENDOR PO");
      } catch (err) {
        button.textContent = "🛠 Workers: ERROR";
        heartbeatEl.textContent = "System heartbeat: Check logs";
        if (errorEl) {
          errorEl.style.display = "block";
          errorEl.textContent = err && err.message ? err.message : "Failed to load status";
        }
        if (lastCheckedEl) {
          lastCheckedEl.textContent = "Last checked: -";
        }
        clearSections("Unable to load status");
      }
    }

    function openModal() {
      backdrop.style.display = "flex";
      fetchStatus(true);
    }

    function closeModal() {
      backdrop.style.display = "none";
    }

    button.addEventListener("click", openModal);
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (refreshBtn) refreshBtn.addEventListener("click", () => fetchStatus(true));
    backdrop.addEventListener("click", (evt) => {
      if (evt.target === backdrop) {
        closeModal();
      }
    });

    fetchStatus(false);
  }

  document.addEventListener("DOMContentLoaded", initWorkersStatus);
})();
