(function () {
  const WORKERS_STATUS_UI_VERSION = "workers-ui-v3";
  const POLL_MS = 4000;
  const WAITING_STATUSES = new Set(["cooldown", "locked", "waiting", "sleeping", "idle"]);
  const STATUS_PRIORITY = {
    error: 5,
    overdue: 4,
    active: 4,
    locked: 3,
    cooldown: 3,
    waiting: 3,
    sleeping: 2,
    idle: 2,
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
      overdueReason: raw.overdue_reason || null,
      mode: (raw.mode || (raw.expected_interval_minutes ? "auto" : "manual")).toLowerCase(),
      what: raw.what || "",
      overdueByMinutes: Number(raw.overdue_by_minutes || 0),
      runtimeStatus: raw.runtime_status || null,
      runtimeLastMessage: raw.runtime_last_message || null,
      runtimeCurrentTask: raw.runtime_current_task || null,
      runtimeQueueCount: raw.queue_count ?? raw.runtime_queue_count,
      actionableQueueCount: raw.actionable_queue_count,
      waitingQueueCount: raw.waiting_queue_count,
      requestedCount: raw.requested_count,
      downloadedCount: raw.downloaded_count,
      failedCount: raw.failed_count,
      missingCount: raw.missing_count,
      staleRequestedCount: raw.stale_requested_count,
      staleDownloadedCount: raw.stale_downloaded_count,
      runtimeActiveWindow: raw.runtime_active_window || null,
      runtimeNextWakeUtc: raw.runtime_next_wake_utc || null,
      runtimeLastStartedUtc: raw.runtime_last_loop_started_utc || null,
      runtimeLastFinishedUtc: raw.runtime_last_loop_finished_utc || null,
      lockOwner: raw.lock_owner || null,
      lockState: raw.lock_state || null,
      lockExpiresAt: raw.lock_expires_at || null,
      cooldownUntil: raw.cooldown_until || null,
      explanation: raw.explanation || null,
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
    const overdueReason = workers.find((w) => w.overdueReason && w.status === "overdue")?.overdueReason || null;
    const message = (overdueReason || workers.find((w) => w.message)?.message || "").trim();
    const overdueMinutes = workers.reduce((max, w) => Math.max(max, w.overdueByMinutes || 0), 0);
    const runtimeWorker = workers.find((w) =>
      w.runtimeStatus ||
      w.runtimeLastMessage ||
      w.runtimeCurrentTask ||
      w.runtimeQueueCount !== undefined ||
      w.actionableQueueCount !== undefined ||
      w.runtimeNextWakeUtc ||
      w.explanation
    );
    const runtimeStatus = String(runtimeWorker?.runtimeStatus || "").toLowerCase();
    const displayStatus = ["active", "sleeping", "idle", "locked", "cooldown", "error"].includes(runtimeStatus)
      ? runtimeStatus
      : status;

    return {
      id: group.id,
      containerId: group.containerId,
      label: group.label,
      description: group.description,
      status,
      displayStatus,
      lastRun,
      nextRun: nextRun || "—",
      mode,
      message,
      overdueReason,
      overdueMinutes,
      runtime: runtimeWorker ? {
        status: runtimeWorker.runtimeStatus,
        lastMessage: runtimeWorker.runtimeLastMessage,
        currentTask: runtimeWorker.runtimeCurrentTask,
        queueCount: runtimeWorker.runtimeQueueCount,
        actionableQueueCount: runtimeWorker.actionableQueueCount,
        waitingQueueCount: runtimeWorker.waitingQueueCount,
        requestedCount: runtimeWorker.requestedCount,
        downloadedCount: runtimeWorker.downloadedCount,
        failedCount: runtimeWorker.failedCount,
        missingCount: runtimeWorker.missingCount,
        staleRequestedCount: runtimeWorker.staleRequestedCount,
        staleDownloadedCount: runtimeWorker.staleDownloadedCount,
        activeWindow: runtimeWorker.runtimeActiveWindow,
        nextWakeUtc: runtimeWorker.runtimeNextWakeUtc,
        lastStartedUtc: runtimeWorker.runtimeLastStartedUtc,
        lastFinishedUtc: runtimeWorker.runtimeLastFinishedUtc,
        lockOwner: runtimeWorker.lockOwner,
        lockState: runtimeWorker.lockState,
        lockExpiresAt: runtimeWorker.lockExpiresAt,
        cooldownUntil: runtimeWorker.cooldownUntil,
        explanation: runtimeWorker.explanation,
      } : null,
    };
  };

  const formatRuntimeTime = (value) => {
    if (!value) return "";
    const ts = Date.parse(value);
    if (!Number.isFinite(ts)) return value;
    return new Date(ts).toLocaleString();
  };

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const formatStatusLabel = (value) => String(value || "unknown")
    .replace(/[_-]+/g, " ")
    .toUpperCase();

  const isPositiveNumber = (value) => Number(value || 0) > 0;

  const renderKvRows = (rows) => {
    if (!rows.length) return "";
    return `
      <div class="worker-kv-grid">
        ${rows.map((row) => `
          <div class="worker-kv-item">
            <div class="worker-kv-key">${escapeHtml(row.label)}</div>
            <div class="worker-kv-value">${escapeHtml(row.value)}</div>
          </div>
        `).join("")}
      </div>
    `;
  };

  const overallStatusFromGroups = (groups) => {
    const statuses = groups.map((g) => g.status);
    if (statuses.some((s) => s === "error")) return "error";
    if (statuses.some((s) => s === "overdue")) return "overdue";
    return "ok";
  };

  const overallLabel = (status, counts) => {
    const errors = (counts && counts.error_count) || 0;
    const overdue = (counts && counts.overdue_count) || 0;

    if (status === "error" || errors) {
      const suffix = errors === 1 ? " Error" : " Errors";
      return `🛠 Workers: ${errors || 1}${suffix}`;
    }
    if (status === "overdue" || overdue) {
      return `🛠 Workers: ${overdue || 1} Overdue`;
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
        <div class="worker-card-group">
          <div class="worker-group-label">Main status</div>
          <div class="worker-card-status-line" data-role="status"></div>
          <div class="worker-card-desc" data-role="desc"></div>
        </div>
        <div class="worker-card-group">
          <div class="worker-group-label">Timing</div>
          <div class="worker-meta-compact">
            <div data-role="last"></div>
            <div data-role="next"></div>
          </div>
        </div>
        <div class="worker-card-group" data-role="runtime-wrap">
          <div class="worker-group-label">Runtime</div>
          <div class="worker-card-message" data-role="runtime"></div>
        </div>
        <div class="worker-card-group" data-role="message-wrap">
          <div class="worker-group-label">Reason / message</div>
          <div class="worker-card-message" data-role="message"></div>
        </div>
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
      runtimeWrap: card.querySelector("[data-role='runtime-wrap']"),
      runtime: card.querySelector("[data-role='runtime']"),
      messageWrap: card.querySelector("[data-role='message-wrap']"),
      message: card.querySelector("[data-role='message']"),
    };
  };

  const renderCard = (card, state) => {
    if (!card || !state) return;
    const refs = ensureCard(card);
    if (!refs) return;

    const statusValue = (state.status || "unknown").toLowerCase();
    const displayStatus = (state.displayStatus || statusValue || "unknown").toLowerCase();
    card.dataset.status = displayStatus;

    if (refs.title) refs.title.textContent = `${statusIcon(displayStatus)} ${state.label}`;
    if (refs.mode) refs.mode.textContent = state.mode === "auto" ? "Automatic" : "Manual";
    if (refs.status) {
      const ledgerText = state.overdueMinutes > 0
        ? `Ledger overdue by ${state.overdueMinutes}m`
        : (displayStatus !== statusValue ? `Ledger: ${formatStatusLabel(statusValue)}` : "");
      refs.status.innerHTML = `
        <span class="worker-status-badge" data-status="${escapeHtml(displayStatus)}">${escapeHtml(formatStatusLabel(displayStatus))}</span>
        ${ledgerText ? `<span class="worker-ledger-note">${escapeHtml(ledgerText)}</span>` : ""}
      `;
    }
    if (refs.last) refs.last.textContent = `Last run: ${state.lastRun || "—"}`;
    if (refs.next) {
      const runtime = state.runtime;
      const nextLabel = runtime && runtime.cooldownUntil ? "Cooldown until" : (runtime && runtime.nextWakeUtc ? "Next wake" : "Next run");
      const nextValue = runtime && runtime.cooldownUntil
        ? formatRuntimeTime(runtime.cooldownUntil)
        : runtime && runtime.nextWakeUtc
          ? formatRuntimeTime(runtime.nextWakeUtc)
          : (state.mode === "manual" ? "—" : state.nextRun || "—");
      refs.next.textContent = `${nextLabel}: ${nextValue}`;
    }
    if (refs.desc) refs.desc.textContent = state.description;
    if (refs.runtime) {
      const runtime = state.runtime;
      if (runtime) {
        const rows = [];
        const isRtSales = state.id === "rt";
        const queueCount = Number(runtime.queueCount || 0);
        const addRow = (label, value, options = {}) => {
          if (value === undefined || value === null || value === "") return;
          if (options.hideZero && Number(value || 0) === 0) return;
          rows.push({ label, value });
        };

        addRow("Status", formatStatusLabel(runtime.status || displayStatus));
        if (runtime.nextWakeUtc) addRow("Next wake", formatRuntimeTime(runtime.nextWakeUtc));
        if (runtime.currentTask) addRow("Task", runtime.currentTask);
        if (runtime.activeWindow) addRow("Window", runtime.activeWindow);
        if (runtime.queueCount !== undefined && runtime.queueCount !== null) addRow("Queue", runtime.queueCount);
        if (runtime.actionableQueueCount !== undefined && runtime.actionableQueueCount !== null) {
          addRow("Claimable", runtime.actionableQueueCount, { hideZero: !isRtSales && !queueCount });
        }
        if (runtime.waitingQueueCount !== undefined && runtime.waitingQueueCount !== null) {
          addRow("Waiting", runtime.waitingQueueCount, { hideZero: !isRtSales && !queueCount });
        }
        if (runtime.failedCount !== undefined && runtime.failedCount !== null) addRow("Failed", runtime.failedCount, { hideZero: true });
        if (runtime.requestedCount !== undefined && runtime.requestedCount !== null) addRow("Requested", runtime.requestedCount, { hideZero: true });
        if (runtime.downloadedCount !== undefined && runtime.downloadedCount !== null) addRow("Downloaded", runtime.downloadedCount, { hideZero: true });
        if (runtime.staleRequestedCount !== undefined && runtime.staleRequestedCount !== null) addRow("Stale requested", runtime.staleRequestedCount, { hideZero: true });
        if (runtime.staleDownloadedCount !== undefined && runtime.staleDownloadedCount !== null) addRow("Stale downloaded", runtime.staleDownloadedCount, { hideZero: true });
        if (isPositiveNumber(runtime.missingCount)) addRow("Missing", runtime.missingCount);
        if (runtime.lastStartedUtc) addRow("Loop started", formatRuntimeTime(runtime.lastStartedUtc));
        if (runtime.lastFinishedUtc) addRow("Loop finished", formatRuntimeTime(runtime.lastFinishedUtc));
        if (runtime.lockState && runtime.lockState !== "none") {
          const ownerText = runtime.lockOwner ? ` (${runtime.lockOwner})` : "";
          const expiryText = runtime.lockExpiresAt ? ` until ${formatRuntimeTime(runtime.lockExpiresAt)}` : "";
          addRow("Lock", `${runtime.lockState}${ownerText}${expiryText}`);
        }
        if (refs.runtimeWrap) refs.runtimeWrap.style.display = rows.length ? "flex" : "none";
        refs.runtime.style.display = rows.length ? "block" : "none";
        refs.runtime.innerHTML = renderKvRows(rows);
      } else {
        if (refs.runtimeWrap) refs.runtimeWrap.style.display = "none";
        refs.runtime.style.display = "none";
        refs.runtime.innerHTML = "";
      }
    }
    if (refs.message) {
      const trimmed = (state.message || "").trim();
      const overdueReason = (state.overdueReason || "").trim();
      const runtime = state.runtime || {};
      const explanation = (runtime.explanation || "").trim();
      const showReason = statusValue === "error" && trimmed;
      const showOverdueReason = statusValue === "overdue" && overdueReason;
      const showWaitingMessage = (statusValue === "waiting" || displayStatus === "sleeping" || displayStatus === "idle") && trimmed;
      if (explanation) {
        if (refs.messageWrap) refs.messageWrap.style.display = "flex";
        refs.message.style.display = "block";
        refs.message.textContent = explanation;
      } else if (showReason) {
        if (refs.messageWrap) refs.messageWrap.style.display = "flex";
        refs.message.style.display = "block";
        refs.message.textContent = `Reason: ${trimmed}`;
      } else if (showOverdueReason) {
        if (refs.messageWrap) refs.messageWrap.style.display = "flex";
        refs.message.style.display = "block";
        refs.message.textContent = `Reason: ${overdueReason}`;
      } else if (showWaitingMessage) {
        if (refs.messageWrap) refs.messageWrap.style.display = "flex";
        refs.message.style.display = "block";
        refs.message.textContent = trimmed;
      } else if (runtime.lastMessage) {
        if (refs.messageWrap) refs.messageWrap.style.display = "flex";
        refs.message.style.display = "block";
        refs.message.textContent = runtime.lastMessage;
      } else {
        if (refs.messageWrap) refs.messageWrap.style.display = "none";
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
    const versionEl = document.getElementById("workers-ui-version");
    const refreshNoteEl = document.getElementById("workers-refresh-note");
    const errorEl = document.getElementById("workers-error");

    if (!button || !backdrop || !heartbeatEl || !lastCheckedEl) return;
    if (versionEl) versionEl.textContent = `UI: ${WORKERS_STATUS_UI_VERSION}`;

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
      const overall = (data && data.summary && data.summary.overall) || overallStatusFromGroups(groupStates);

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

    const fetchStatus = async (manual = false) => {
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
        const resp = await fetch(`/api/workers/status${manual ? "?manual=true" : ""}`, { signal: controller.signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        applyData(data);
        if (manual && refreshNoteEl) {
          refreshNoteEl.textContent = `Status refreshed at ${new Date().toLocaleTimeString()}.`;
        }
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
      if (pollHandle) {
        window.clearInterval(pollHandle);
        pollHandle = null;
      }
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
      if (versionEl) versionEl.textContent = `UI: ${WORKERS_STATUS_UI_VERSION}`;
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
        fetchStatus(true);
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
