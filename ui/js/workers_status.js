(function () {
  const POLL_MS = 4000;
  const WAITING_STATUSES = new Set(["cooldown", "locked", "waiting"]);
  const STATUS_PRIORITY = {
    error: 5,
    overdue: 4,
    locked: 3,
    cooldown: 3,
    waiting: 3,
    ok: 2,
    unknown: 1,
  };

  const GROUPS = [
    {
      id: "inventory",
      containerId: "workers-section-inventory",
      label: "Inventory Refresh",
      keys: ["rt_inventory_refresh", "inventory_materializer"],
      description: "Refreshes Amazon realtime inventory and materializes the snapshot.",
      defaultMode: "auto",
    },
    {
      id: "rt",
      containerId: "workers-section-rt-sales",
      label: "Real Time Refresh",
      keys: ["rt_sales_sync"],
      description: "Keeps realtime sales ledger healthy (15m cadence).",
      defaultMode: "auto",
    },
    {
      id: "df-orders",
      containerId: "workers-section-df-payments",
      label: "DF Orders Refresh",
      keys: ["df_payments_incremental"],
      description: "Pulls DF purchase orders and payments incrementally.",
      defaultMode: "auto",
    },
    {
      id: "df-gmail",
      containerId: "workers-section-vendor-po",
      label: "DF Gmail Reconcile Refresh",
      keys: ["vendor_po_sync"],
      description: "Manual reconcile helper; run when you need a fresh remittance import.",
      defaultMode: "manual",
    },
  ];

  const statusIcon = (status) => {
    const value = (status || "").toLowerCase();
    if (value === "error") return "🔴";
    if (value === "overdue") return "🟠";
    if (WAITING_STATUSES.has(value)) return "🟡";
    if (value === "ok") return "🟢";
    return "⚪";
  };

  const normalizeWorker = (raw) => {
    if (!raw || typeof raw !== "object") {
      return null;
    }
    const status = (raw.status || "unknown").toLowerCase();
    const lastRunUae = raw.last_run_at_uae || raw.last_run_at || null;
    const nextRunUae = raw.next_eligible_at_uae || raw.next_run_at_uae || raw.next_run_at || raw.next_eligible_at || null;
    return {
      key: raw.key,
      status,
      lastRunUae,
      nextRunUae,
      lastRunUtc: raw.last_run_utc || raw.last_run_at_utc || null,
      nextRunUtc: raw.next_run_utc || raw.next_eligible_at_utc || null,
      message: raw.message || raw.details || "",
      mode: (raw.mode || (raw.expected_interval_minutes ? "auto" : "manual")).toLowerCase(),
      what: raw.what || "",
      overdueByMinutes: Number(raw.overdue_by_minutes || 0),
    };
  };

  const buildWorkerMap = (data) => {
    const map = new Map();
    if (!data || !data.domains) return map;
    Object.values(data.domains).forEach((domain) => {
      if (!domain || !Array.isArray(domain.workers)) return;
      domain.workers.forEach((worker) => {
        const normalized = normalizeWorker(worker);
        if (normalized && normalized.key) {
          map.set(normalized.key, normalized);
        }
      });
    });
    return map;
  };

  const rankStatus = (status) => STATUS_PRIORITY[status] || STATUS_PRIORITY.unknown;

  const pickWorstStatus = (workers) => {
    if (!workers || !workers.length) return "error";
    return workers.reduce((worst, worker) => (rankStatus(worker.status) > rankStatus(worst) ? worker.status : worst), workers[0].status);
  };

  const pickMostRecentByUtc = (workers, field) => {
    let best = null;
    workers.forEach((w) => {
      const candidate = w[field];
      if (!candidate) return;
      const ts = Date.parse(candidate);
      if (!Number.isFinite(ts)) return;
      if (!best || ts > best.ts) {
        best = { ts, label: w[`${field === "lastRunUtc" ? "lastRunUae" : "nextRunUae"}`] || candidate };
      }
    });
    return best ? best.label : null;
  };

  const pickEarliestNextRun = (workers) => {
    let pick = null;
    workers.forEach((w) => {
      const candidate = w.nextRunUtc;
      if (!candidate) return;
      const ts = Date.parse(candidate);
      if (!Number.isFinite(ts)) return;
      if (!pick || ts < pick.ts) {
        pick = { ts, label: w.nextRunUae || candidate };
      }
    });
    return pick ? pick.label : null;
  };

  const computeGroupState = (group, workerMap) => {
    const workers = group.keys.map((k) => workerMap.get(k)).filter(Boolean);
    const status = pickWorstStatus(workers);
    const lastRun = pickMostRecentByUtc(workers, "lastRunUtc") || workers.find((w) => w.lastRunUae)?.lastRunUae || "—";
    const nextRun = pickEarliestNextRun(workers) || workers.find((w) => w.nextRunUae)?.nextRunUae || (group.defaultMode === "manual" ? "—" : "");
    const mode = (workers.find((w) => w.mode)?.mode || group.defaultMode || "manual").toLowerCase();
    const message = (workers.find((w) => w.message)?.message || "").trim();
    const overdueMinutes = workers.reduce((max, w) => Math.max(max, w.overdueByMinutes || 0), 0);

    return {
      id: group.id,
      containerId: group.containerId,
      label: group.label,
      description: group.description,
      status,
      lastRun,
      nextRun: nextRun || "—",
      mode,
      message,
      overdueMinutes,
    };
  };

  const overallStatusFromGroups = (groups) => {
    const statuses = groups.map((g) => g.status);
    if (statuses.some((s) => s === "error")) return "error";
    if (statuses.some((s) => s === "overdue")) return "overdue";
    if (statuses.some((s) => WAITING_STATUSES.has(s) || s === "locked" || s === "cooldown")) return "waiting";
    return "ok";
  };

  const overallLabel = (status, counts) => {
    if (status === "error" || (counts && counts.error_count)) return "🛠 Workers: ERROR";
    if (status === "overdue" || (counts && counts.overdue_count)) {
      const cnt = (counts && counts.overdue_count) || 1;
      return `🛠 Workers: ${cnt} Overdue`;
    }
    if (status === "waiting" || (counts && counts.waiting_count)) {
      const cnt = (counts && counts.waiting_count) || 1;
      return `🛠 Workers: ${cnt} Waiting`;
    }
    return "🛠 Workers: OK";
  };

  const ensureCard = (card) => {
    if (!card) return null;
    if (!card.dataset || card.dataset.wired !== "1") {
      card.innerHTML = `
        <div class="worker-card-head">
          <div class="worker-card-title" data-role="title"></div>
          <div class="worker-card-mode" data-role="mode"></div>
        </div>
        <div class="worker-card-status-line" data-role="status"></div>
        <div class="worker-meta-compact">
          <div data-role="last"></div>
          <div data-role="next"></div>
        </div>
        <div class="worker-card-desc" data-role="desc"></div>
        <div class="worker-card-message" data-role="message"></div>
      `;
      card.dataset.wired = "1";
    }
    return {
      title: card.querySelector("[data-role='title']"),
      mode: card.querySelector("[data-role='mode']"),
      status: card.querySelector("[data-role='status']"),
      last: card.querySelector("[data-role='last']"),
      next: card.querySelector("[data-role='next']"),
      desc: card.querySelector("[data-role='desc']"),
      message: card.querySelector("[data-role='message']"),
    };
  };

  const renderCard = (card, state) => {
    if (!card || !state) return;
    const refs = ensureCard(card);
    if (!refs) return;

    if (refs.title) refs.title.textContent = `${statusIcon(state.status)} ${state.label}`;
    if (refs.mode) refs.mode.textContent = state.mode === "auto" ? "Automatic" : "Manual";
    if (refs.status) {
      const overdueText = state.overdueMinutes > 0 ? ` • Overdue by ${state.overdueMinutes}m` : "";
      refs.status.textContent = `Status: ${state.status.toUpperCase()}${overdueText}`;
    }
    if (refs.last) refs.last.textContent = `Last run: ${state.lastRun || "—"}`;
    if (refs.next) refs.next.textContent = `Next run: ${state.mode === "manual" ? "—" : state.nextRun || "—"}`;
    if (refs.desc) refs.desc.textContent = state.description;
    if (refs.message) {
      if (state.message) {
        refs.message.style.display = "block";
        refs.message.textContent = state.message;
      } else {
        refs.message.style.display = "none";
        refs.message.textContent = "";
      }
    }
  };

  function initWorkersStatus() {
    const button = document.getElementById("workers-status-btn");
    const backdrop = document.getElementById("workers-modal");
    const closeBtn = document.getElementById("workers-close-btn");
    const refreshBtn = document.getElementById("workers-refresh-btn");
    const heartbeatEl = document.getElementById("workers-heartbeat");
    const lastCheckedEl = document.getElementById("workers-last-checked");
    const errorEl = document.getElementById("workers-error");

    if (!button || !backdrop || !heartbeatEl || !lastCheckedEl) return;

    const sectionMap = new Map();
    GROUPS.forEach((g) => {
      const node = document.getElementById(g.containerId);
      if (node) sectionMap.set(g.id, node);
    });

    let pollHandle = null;
    let inFlight = null;

    const showError = (err) => {
      if (errorEl) {
        errorEl.style.display = "block";
        errorEl.textContent = err && err.message ? err.message : "Failed to load status";
      }
      if (heartbeatEl) heartbeatEl.textContent = "System heartbeat: Check logs";
      if (lastCheckedEl) lastCheckedEl.textContent = "Last checked: -";
      button.textContent = "🛠 Workers: ERROR";
    };

    const applyData = (data) => {
      const workerMap = buildWorkerMap(data);
      const groupStates = GROUPS.map((g) => computeGroupState(g, workerMap));
      const overall = overallStatusFromGroups(groupStates);

      groupStates.forEach((state) => {
        const card = sectionMap.get(state.id);
        if (card) {
          renderCard(card, state);
        }
      });

      if (heartbeatEl) heartbeatEl.textContent = overall === "ok" ? "System heartbeat: OK" : "System heartbeat: Check logs";
      if (lastCheckedEl) lastCheckedEl.textContent = `Last checked: ${data.checked_at_uae || "-"}`;
      if (errorEl) {
        errorEl.style.display = "none";
        errorEl.textContent = "";
      }
      button.textContent = overallLabel(overall, data.summary);
    };

    const fetchStatus = async () => {
      if (errorEl) {
        errorEl.style.display = "none";
        errorEl.textContent = "";
      }

      if (inFlight) {
        inFlight.abort();
      }
      const controller = new AbortController();
      inFlight = controller;
      try {
        const resp = await fetch("/api/workers/status", { signal: controller.signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        applyData(data);
      } catch (err) {
        if (err && err.name === "AbortError") return;
        showError(err);
      } finally {
        if (inFlight === controller) {
          inFlight = null;
        }
      }
    };

    const startPolling = () => {
      if (pollHandle) return;
      fetchStatus();
      pollHandle = window.setInterval(fetchStatus, POLL_MS);
    };

    const stopPolling = () => {
      if (pollHandle) {
        window.clearInterval(pollHandle);
        pollHandle = null;
      }
      if (inFlight) {
        inFlight.abort();
        inFlight = null;
      }
    };

    const openModal = () => {
      backdrop.style.display = "flex";
      startPolling();
    };

    const closeModal = () => {
      backdrop.style.display = "none";
      stopPolling();
    };

    button.addEventListener("click", openModal);
    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => {
        stopPolling();
        startPolling();
      });
    }

    backdrop.addEventListener("click", (evt) => {
      if (evt.target === backdrop) {
        closeModal();
      }
    });

    // Initial load for the header; polling will start when modal opens.
    fetchStatus();
  }

  document.addEventListener("DOMContentLoaded", initWorkersStatus);
})();
