"""HTML for the lightweight, auto-refreshing live dashboard page.

Kept separate from `fastapi.py` (which only wires this string onto a
route) the same way `summary.py` is: `render_dashboard_html` is a pure
function, directly testable without a FastAPI test client. No build
step, no external JS/CSS dependency — one self-contained HTML document
that polls the existing JSON `{prefix}/performance` endpoint with
`fetch()` and re-renders in place.
"""
# ruff: noqa: E501 — mostly a large embedded HTML/JS template; wrapping
# markup/JS lines at 88 chars would hurt readability far more than it helps.

from __future__ import annotations

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Performance dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0f1115; color: #e6e6e6; margin: 0; padding: 24px;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .subtitle { color: #8a8f98; font-size: 13px; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: #171a21; border: 1px solid #262b36; border-radius: 8px; padding: 14px 16px; }
  .card .label { color: #8a8f98; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  .card .value { font-size: 26px; font-weight: 600; margin-top: 4px; }
  .card .sub { color: #8a8f98; font-size: 12px; margin-top: 2px; }
  section { margin-bottom: 28px; }
  section > h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.04em; color: #8a8f98; margin-bottom: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #262b36; }
  th { color: #8a8f98; font-weight: 500; }
  tr:hover { background: #171a21; }
  .mono { font-family: ui-monospace, "SF Mono", Consolas, monospace; }
  .status-ok { color: #4ade80; }
  .status-error { color: #f87171; }
  a { color: #7dd3fc; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .empty { color: #8a8f98; font-style: italic; padding: 8px 0; }
  #error-banner {
    display: none; background: #3a1d1d; border: 1px solid #7f1d1d; color: #fca5a5;
    padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 13px;
  }
  #auth-bar {
    display: flex; align-items: center; gap: 8px; background: #171a21;
    border: 1px solid #262b36; border-radius: 8px; padding: 10px 14px; margin-bottom: 16px;
  }
  #auth-bar label { font-size: 12px; color: #8a8f98; white-space: nowrap; }
  #auth-bar input {
    flex: 1; min-width: 160px; background: #0f1115; border: 1px solid #262b36;
    border-radius: 6px; color: #e6e6e6; padding: 6px 10px; font-size: 13px;
    font-family: ui-monospace, "SF Mono", Consolas, monospace;
  }
  #auth-bar button {
    background: #2563eb; border: none; border-radius: 6px; color: white;
    padding: 6px 14px; font-size: 13px; cursor: pointer;
  }
  #auth-bar button:hover { background: #1d4ed8; }
  #auth-status { font-size: 12px; color: #8a8f98; white-space: nowrap; }
</style>
</head>
<body>
  <h1>Performance dashboard</h1>
  <div class="subtitle">
    Auto-refreshing every <span id="interval-label">__INTERVAL_SECONDS__</span>s &middot;
    reading <code class="mono">__PREFIX__/performance</code> &middot;
    last updated <span id="last-updated">never</span>
  </div>
  <div id="auth-bar">
    <label for="api-key-input">x-api-key</label>
    <input id="api-key-input" type="password" placeholder="Paste your API key, then Save" autocomplete="off">
    <button id="api-key-save" type="button">Save</button>
    <span id="auth-status"></span>
  </div>
  <div id="error-banner"></div>

  <div class="grid">
    <div class="card"><div class="label">Throughput</div><div class="value" id="m-throughput">&ndash;</div><div class="sub">req/s (recent window)</div></div>
    <div class="card"><div class="label">Requests recorded</div><div class="value" id="m-total">&ndash;</div><div class="sub"><span id="m-retained">&ndash;</span> retained in memory</div></div>
    <div class="card"><div class="label">p50 latency</div><div class="value" id="m-p50">&ndash;</div><div class="sub">whole request</div></div>
    <div class="card"><div class="label">p95 / p99 latency</div><div class="value" id="m-p95">&ndash;</div><div class="sub" id="m-p99">&ndash;</div></div>
    <div class="card"><div class="label">Pool connections</div><div class="value" id="m-pool">&ndash;</div><div class="sub">active / idle</div></div>
    <div class="card"><div class="label">CPU (cumulative)</div><div class="value" id="m-cpu">&ndash;</div><div class="sub">user+system seconds</div></div>
    <div class="card"><div class="label">Memory (RSS)</div><div class="value" id="m-mem">&ndash;</div><div class="sub">current process</div></div>
  </div>

  <section>
    <h2>Trace-stage timing (mean / p95, ms)</h2>
    <table id="stages-table">
      <thead><tr><th>Stage / metric</th><th>Count</th><th>Mean</th><th>p50</th><th>p90</th><th>p95</th><th>p99</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="empty" id="stages-empty" style="display:none;">No histogram data yet — send some traffic.</div>
  </section>

  <section>
    <h2>Gauges</h2>
    <table id="gauges-table">
      <thead><tr><th>Name</th><th>Tags</th><th>Value</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="empty" id="gauges-empty" style="display:none;">No gauges reported yet.</div>
  </section>

  <section>
    <h2>Recent requests</h2>
    <table id="requests-table">
      <thead><tr><th>Request</th><th>Route</th><th>Status</th><th>Duration</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="empty" id="requests-empty" style="display:none;">No requests recorded yet.</div>
  </section>

<script>
(function () {
  var PREFIX = "__PREFIX__";
  var INTERVAL_MS = __INTERVAL_SECONDS__ * 1000;
  var prevTotal = null;
  var prevTime = null;
  var API_KEY_STORAGE_KEY = "perf_dashboard_api_key";

  var apiKeyInput = document.getElementById("api-key-input");
  var apiKeySave = document.getElementById("api-key-save");
  var authStatus = document.getElementById("auth-status");

  function loadApiKey() {
    try {
      return sessionStorage.getItem(API_KEY_STORAGE_KEY) || "";
    } catch (e) {
      return "";
    }
  }
  function saveApiKey(key) {
    try {
      if (key) { sessionStorage.setItem(API_KEY_STORAGE_KEY, key); }
      else { sessionStorage.removeItem(API_KEY_STORAGE_KEY); }
    } catch (e) { /* sessionStorage unavailable (e.g. file://); key just won't persist */ }
  }

  var savedKey = loadApiKey();
  if (savedKey) {
    apiKeyInput.value = savedKey;
    authStatus.textContent = "using saved key";
  } else {
    authStatus.textContent = "no key set";
  }
  apiKeySave.addEventListener("click", function () {
    saveApiKey(apiKeyInput.value.trim());
    authStatus.textContent = apiKeyInput.value.trim() ? "saved" : "cleared";
    poll();
  });
  apiKeyInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { apiKeySave.click(); }
  });

  function fmtMs(ns) {
    if (ns === null || ns === undefined) return "\u2013";
    return (ns / 1e6).toFixed(2) + "ms";
  }
  function fmtBytes(n) {
    if (n === null || n === undefined) return "\u2013";
    if (n > 1e9) return (n / 1e9).toFixed(2) + " GB";
    if (n > 1e6) return (n / 1e6).toFixed(2) + " MB";
    if (n > 1e3) return (n / 1e3).toFixed(1) + " KB";
    return n.toFixed(0) + " B";
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function findByName(list, name) {
    for (var i = 0; i < list.length; i++) { if (list[i].name === name) return list[i]; }
    return null;
  }
  function setText(id, text) { document.getElementById(id).textContent = text; }

  function render(data) {
    var now = Date.now() / 1000;
    setText("m-total", data.total_requests_recorded);
    setText("m-retained", data.request_count + " / " + data.max_request_history);

    if (prevTotal !== null && prevTime !== null && now > prevTime) {
      var rate = (data.total_requests_recorded - prevTotal) / (now - prevTime);
      setText("m-throughput", Math.max(0, rate).toFixed(1));
    }
    prevTotal = data.total_requests_recorded;
    prevTime = now;

    var request = findByName(data.histograms, "request") || findByName(data.histograms, "dispatch");
    setText("m-p50", request ? fmtMs(request.p50) : "\u2013");
    setText("m-p95", request ? fmtMs(request.p95) : "\u2013");
    setText("m-p99", request ? "p99 " + fmtMs(request.p99) : "\u2013");

    var poolActive = findByName(data.gauges, "pool_active_connections");
    var poolIdle = findByName(data.gauges, "pool_idle_connections");
    setText("m-pool", (poolActive ? poolActive.value : "\u2013") + " / " + (poolIdle ? poolIdle.value : "\u2013"));

    var cpuUser = findByName(data.gauges, "process_cpu_user_seconds_total");
    var cpuSys = findByName(data.gauges, "process_cpu_system_seconds_total");
    if (cpuUser || cpuSys) {
      setText("m-cpu", ((cpuUser ? cpuUser.value : 0) + (cpuSys ? cpuSys.value : 0)).toFixed(2) + "s");
    } else {
      setText("m-cpu", "collector off");
    }

    var mem = findByName(data.gauges, "process_memory_current_rss_bytes") ||
              findByName(data.gauges, "process_memory_peak_rss_bytes");
    setText("m-mem", mem ? fmtBytes(mem.value) : "collector off");

    var stageBody = document.querySelector("#stages-table tbody");
    stageBody.innerHTML = "";
    data.histograms.forEach(function (h) {
      var tr = document.createElement("tr");
      tr.innerHTML = "<td class='mono'>" + escapeHtml(h.name) + "</td>" +
        "<td>" + h.count + "</td>" +
        "<td>" + fmtMs(h.mean) + "</td>" +
        "<td>" + fmtMs(h.p50) + "</td>" +
        "<td>" + fmtMs(h.p90) + "</td>" +
        "<td>" + fmtMs(h.p95) + "</td>" +
        "<td>" + fmtMs(h.p99) + "</td>";
      stageBody.appendChild(tr);
    });
    document.getElementById("stages-empty").style.display = data.histograms.length ? "none" : "block";

    var gaugeBody = document.querySelector("#gauges-table tbody");
    gaugeBody.innerHTML = "";
    data.gauges.forEach(function (g) {
      var tr = document.createElement("tr");
      tr.innerHTML = "<td class='mono'>" + escapeHtml(g.name) + "</td>" +
        "<td class='mono'>" + escapeHtml(JSON.stringify(g.tags || {})) + "</td>" +
        "<td>" + g.value + "</td>";
      gaugeBody.appendChild(tr);
    });
    document.getElementById("gauges-empty").style.display = data.gauges.length ? "none" : "block";

    var reqBody = document.querySelector("#requests-table tbody");
    reqBody.innerHTML = "";
    data.recent_requests.forEach(function (r) {
      var tr = document.createElement("tr");
      var route = (r.tags && (r.tags.route || r.tags.method)) ?
        escapeHtml((r.tags.method || "") + " " + (r.tags.route || "")) : "\u2013";
      var statusClass = r.status === "ok" ? "status-ok" : "status-error";
      tr.innerHTML = "<td class='mono'><a href='" + PREFIX + "/request/" +
        encodeURIComponent(r.request_id) + "' target='_blank'>" +
        escapeHtml(r.request_id.slice(0, 12)) + "\u2026</a></td>" +
        "<td>" + route + "</td>" +
        "<td class='" + statusClass + "'>" + escapeHtml(r.status) + "</td>" +
        "<td>" + fmtMs(r.duration_ns) + "</td>";
      reqBody.appendChild(tr);
    });
    document.getElementById("requests-empty").style.display = data.recent_requests.length ? "none" : "block";

    setText("last-updated", new Date().toLocaleTimeString());
    document.getElementById("error-banner").style.display = "none";
  }

  function poll() {
    var headers = { "Accept": "application/json" };
    var key = apiKeyInput.value.trim();
    if (key) { headers["x-api-key"] = key; }

    fetch(PREFIX + "/performance?recent_limit=25", { headers: headers })
      .then(function (resp) {
        if (resp.status === 401 || resp.status === 403) {
          throw new Error(
            "HTTP " + resp.status +
            (key ? " (check the API key is correct and has admin privileges)"
                 : " (enter an admin API key above and click Save)")
          );
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(render)
      .catch(function (err) {
        var banner = document.getElementById("error-banner");
        banner.textContent = "Failed to refresh: " + err.message;
        banner.style.display = "block";
      });
  }

  poll();
  setInterval(poll, INTERVAL_MS);
})();
</script>
</body>
</html>
"""


def render_dashboard_html(
    prefix: str = "/debug", refresh_interval_seconds: float = 2.0
) -> str:
    """Render the live dashboard page's HTML for a dashboard mounted at `prefix`.

    Pure string templating, no request/registry access — the page
    itself does all data-fetching client-side via `fetch(prefix +
    "/performance")`, so this only needs to know where that endpoint
    lives and how often to poll it.
    """
    return _HTML_TEMPLATE.replace("__PREFIX__", prefix).replace(
        "__INTERVAL_SECONDS__", str(refresh_interval_seconds)
    )
