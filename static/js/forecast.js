// forecast.js

function forecastDebug(msg) {
  try {
    var el = document.getElementById("forecast-debug");
    if (!el) return;
    var time = new Date().toISOString().slice(11, 19);
    el.textContent += "[" + time + "] " + msg + "\n";
  } catch (e) {
    // ignore debug errors
  }
}

function onForecastRefreshClick() {
  var statusBox = document.getElementById("forecast-refresh-status");
  if (statusBox) {
    statusBox.textContent = "Syncing forecast data...";
  }
  forecastDebug("POST /api/forecast/refresh-all ...");

  fetch("/api/forecast/refresh-all", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  })
    .then(function (res) {
      forecastDebug(
        "HTTP " + res.status + " from /api/forecast/refresh-all"
      );
      return res.json().catch(function () {
        return {};
      });
    })
    .then(function (data) {
      if (statusBox) {
        var when =
          (data && (data.refreshedAt || data.generatedAt)) || "";
        statusBox.textContent =
          "Sync complete" + (when ? " at " + when : "");
      }
      forecastDebug(
        "Refresh-all response ok=" +
          data.ok +
          ", msg=" +
          (data.message || "")
      );
      // reload dashboard after sync
      loadForecastDashboard();
    })
    .catch(function (err) {
      forecastDebug("Error in refresh-all: " + err.message);
      if (statusBox) {
        statusBox.textContent = "Sync failed: " + err.message;
      }
    });
}

// 👇 GLOBAL function, used by index.html showTab()
function loadForecastDashboard() {
  var banner = document.getElementById("forecast-meta-banner");
  if (banner) {
    banner.textContent = "Loading forecast data...";
  }
  forecastDebug("GET /api/forecast/dashboard ...");

  fetch("/api/forecast/dashboard")
    .then(function (res) {
      forecastDebug(
        "HTTP " + res.status + " from /api/forecast/dashboard"
      );
      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      return res.json();
    })
    .then(function (data) {
      var meta = data && data.meta ? data.meta : {};
      var rows = data && Array.isArray(data.rows) ? data.rows : [];

      forecastDebug(
        "Parsed dashboard: meta keys = " +
          Object.keys(meta).join(", ") +
          "; rows = " +
          rows.length
      );

      renderSourceStatus(meta);
      renderMetaBanner(meta);
      renderForecastRows(rows);
    })
    .catch(function (err) {
      forecastDebug("Error in dashboard: " + err.message);
      if (banner) {
        banner.textContent =
          "Error loading forecast data: " + err.message;
      }
    });
}

function renderSourceStatus(meta) {
  forecastDebug("renderSourceStatus called");
  var container = document.getElementById("forecast-source-status");
  if (!container) return;
  container.innerHTML = "";

  if (!meta || !meta.sourceStatus) {
    container.textContent = "No source status information yet.";
    return;
  }

  var order = ["salesHistory", "forecast", "inventory", "pos"];
  order.forEach(function (key) {
    var s = meta.sourceStatus[key];
    if (!s) return;

    var card = document.createElement("div");
    card.className =
      "forecast-status-card forecast-status-" +
      ((s.status || "unknown").toString().toLowerCase());
    card.style.display = "inline-block";
    card.style.border = "1px solid #ddd";
    card.style.borderRadius = "6px";
    card.style.padding = "6px 10px";
    card.style.marginRight = "8px";
    card.style.marginBottom = "4px";
    card.style.background = "#fafafa";

    var label = s.label || key;
    var status = s.status || "UNKNOWN";
    var message = s.message || "";

    card.innerHTML =
      '<div><b>' +
      label +
      '</b> <span style="font-size:11px; padding:2px 6px; border-radius:4px; background:#eee;">' +
      status +
      '</span></div>' +
      '<div style="font-size:12px; color:#555;">' +
      message +
      "</div>";

    container.appendChild(card);
  });
}

function renderMetaBanner(meta) {
  var el = document.getElementById("forecast-meta-banner");
  if (!el) return;

  if (!meta) {
    el.textContent = "";
    return;
  }

  var salesFrom = meta.salesDataFrom || "—";
  var salesThrough = meta.salesDataThrough || "—";
  var forecastMin = meta.forecastGenerationDateMin || "—";
  var forecastMax = meta.forecastGenerationDateMax || "—";
  var snapshot = meta.inventorySnapshotTime || "—";

  el.textContent =
    "Sales: " +
    salesFrom +
    " → " +
    salesThrough +
    " | Forecast gen: " +
    forecastMin +
    " → " +
    forecastMax +
    " | Inventory snapshot: " +
    snapshot;
}

function renderForecastRows(rows) {
  var tbody = document.getElementById("forecast-table-body");
  if (!tbody) return;
  tbody.innerHTML = "";

  // Default sort by P70 Demand (Horizon) desc
  var sortKey = window.__forecastSortKey || "p70Demand";
  var sortDir = window.__forecastSortDir || "desc";
  if (rows && rows.sort) {
    rows = rows.slice().sort(function (a, b) {
      function num(val) {
        var n = parseFloat(val);
        return isNaN(n) ? 0 : n;
      }
      var av = num(
        a[sortKey] ||
          a.p70DemandHorizon ||
          a.coverWeeksAtP70 ||
          a.next30dInboundUnits
      );
      var bv = num(
        b[sortKey] ||
          b.p70DemandHorizon ||
          b.coverWeeksAtP70 ||
          b.next30dInboundUnits
      );
      return sortDir === "desc" ? bv - av : av - bv;
    });
  }

  if (!rows || rows.length === 0) {
    var tr = document.createElement("tr");
    var td = document.createElement("td");
    td.colSpan = 9;
    td.textContent = "No forecast rows to display yet.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  rows.forEach(function (row) {
    var tr = document.createElement("tr");

    // ASIN cell shows image if available
    var asinTd = document.createElement("td");
    var img = row.image || row.asinImage || "";
    if (img) {
      var i = document.createElement("img");
      i.src = img;
      i.alt = row.asin || "";
      i.style.width = "48px";
      i.style.height = "48px";
      i.style.objectFit = "cover";
      i.style.borderRadius = "6px";
      asinTd.appendChild(i);
    } else {
      asinTd.textContent = row.asin || "";
    }
    tr.appendChild(asinTd);

    function fmtNum(val) {
      var n = parseFloat(val);
      if (isNaN(n)) return "";
      return n.toFixed(2);
    }

    function cell(value) {
      var td = document.createElement("td");
      td.textContent = value != null ? value : "";
      return td;
    }

    tr.appendChild(cell(row.currentInventory));
    tr.appendChild(cell(row.last30dUnits));
    tr.appendChild(cell(row.avgWeeklyUnits));
    tr.appendChild(cell(row.next30dInbound || row.next30dInboundUnits));
    tr.appendChild(cell(fmtNum(row.p70Demand || row.p70DemandHorizon)));
    tr.appendChild(cell(fmtNum(row.weeksOfCover || row.coverWeeksAtP70)));
    tr.appendChild(cell(row.risk || row.riskLevel));
    tr.appendChild(cell(row.suggestedAction));

    tbody.appendChild(tr);
  });

  // Collect ASINs without images and queue them for catalog fetch
  try {
    var missing = [];
    rows.forEach(function (row) {
      var hasImg = row.image || row.asinImage;
      if (!hasImg && row.asin) {
        missing.push(row.asin);
      }
    });
    if (missing.length) {
      queueCatalogFetchForMissing(missing);
    }
  } catch (err) {
    console.warn("Failed queuing missing catalog fetches", err);
  }
}

// Gentle background fetch for ASINs missing images so future loads have catalog data.
var _queuedCatalogFetch = new Set();
var _catalogFetchBlocked = false;
async function queueCatalogFetchForMissing(asins) {
  if (_catalogFetchBlocked) return;
  if (!asins || !asins.length) return;
  for (const asin of asins) {
    if (_catalogFetchBlocked) break;
    if (_queuedCatalogFetch.has(asin)) continue;
    _queuedCatalogFetch.add(asin);
    try {
      const resp = await fetch("/api/catalog/fetch/" + encodeURIComponent(asin), { method: "POST" });
      if (resp.status === 403) {
        _catalogFetchBlocked = true;
        console.warn("Catalog fetch forbidden (403). Halting further auto-fetch attempts until refresh.");
        break;
      }
    } catch (err) {
      console.warn("Catalog fetch failed for", asin, err);
    }
  }
}

// Initialise once script is loaded
(function initForecast() {
  forecastDebug("forecast.js loaded; running initForecast()");

  function doInit() {
    forecastDebug("Initializing forecast UI");
    var sortKeySel = document.getElementById("forecast-sort-key");
    var sortDirSel = document.getElementById("forecast-sort-dir");
    if (sortKeySel) {
      window.__forecastSortKey = sortKeySel.value || "p70Demand";
      sortKeySel.addEventListener("change", function () {
        window.__forecastSortKey = this.value;
        loadForecastDashboard();
      });
    }
    if (sortDirSel) {
      window.__forecastSortDir = sortDirSel.value || "desc";
      sortDirSel.addEventListener("change", function () {
        window.__forecastSortDir = this.value;
        loadForecastDashboard();
      });
    }

    // Clickable sortable headers
    var headerCells = document.querySelectorAll("#forecast-table thead th.sortable");
    headerCells.forEach(function (th) {
      th.style.cursor = "pointer";
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-sort");
        if (!key) return;
        if (window.__forecastSortKey === key) {
          window.__forecastSortDir = window.__forecastSortDir === "asc" ? "desc" : "asc";
        } else {
          window.__forecastSortKey = key;
          window.__forecastSortDir = "desc";
        }
        // Keep selects in sync if present
        if (sortKeySel) sortKeySel.value = window.__forecastSortKey;
        if (sortDirSel) sortDirSel.value = window.__forecastSortDir;
        loadForecastDashboard();
      });
    });

    var btn = document.getElementById("btn-forecast-refresh");
    if (btn) {
      btn.addEventListener("click", onForecastRefreshClick);
    } else {
      forecastDebug("btn-forecast-refresh not found in DOM.");
    }

    // Auto-load once
    loadForecastDashboard();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", doInit);
  } else {
    doInit();
  }
})();
