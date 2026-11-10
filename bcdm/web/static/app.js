"use strict";

// Refresh cadence: track the poll interval, but stay responsive and not too chatty.
const POLL_S = parseFloat(document.body.dataset.pollInterval) || 30;
const REFRESH_MS = Math.min(Math.max(POLL_S, 4), 15) * 1000;
const PALETTE = ["#3ba776", "#5b7cc6", "#e0a13c", "#b97fd0", "#48b0c0", "#d8584f", "#9aa55c"];
const AXIS = "#8b97a7";
const GRID = "rgba(127,127,127,.14)";

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString() : "—");
const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

function fmtAgo(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "now";
  const m = s / 60; if (m < 60) return Math.floor(m) + "m";
  const h = m / 60; if (h < 24) return Math.floor(h) + "h";
  const d = h / 24; if (d < 30) return Math.floor(d) + "d";
  const mo = d / 30; if (mo < 12) return Math.floor(mo) + "mo";
  return Math.floor(mo / 12) + "y";
}
const fmtDayShort = (iso) => new Date(iso + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" });

// ---- theme toggle ---------------------------------------------------------

function initTheme() {
  const root = document.documentElement;
  const apply = (t) => {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("bcdm-theme", t); } catch (e) {}
    const label = document.getElementById("theme-label");
    if (label) label.textContent = t === "dark" ? "Light mode" : "Dark mode";
  };
  apply(root.getAttribute("data-theme") || "light");
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.addEventListener("click", () =>
    apply((root.getAttribute("data-theme") || "light") === "dark" ? "light" : "dark"));
}

// ---- shared topbar status (every page) ------------------------------------

let snapshotHook = null; // set by the Now page

async function refreshStatus() {
  const dot = document.getElementById("status-dot");
  const txt = document.getElementById("status-text");
  const notice = document.getElementById("status-notice");
  try {
    const s = await fetchJSON("/api/now");
    if (s.online) {
      dot.className = "dot ok";
      txt.textContent = "Online · " + fmtTime(s.updated_at);
    } else {
      dot.className = "dot bad";
      txt.textContent = s.login_disabled ? "Router login disabled" : "Router unreachable";
    }
    txt.title = s.online ? "" : (s.error || "");
    // Logins stopped after rejected passwords / a lockout: say why and how to fix it.
    if (notice) {
      notice.hidden = !s.login_disabled;
      notice.textContent = s.login_disabled ? (s.error || "") : "";
    }
    if (snapshotHook) snapshotHook(s);
  } catch (e) {
    if (dot) dot.className = "dot bad";
    if (txt) txt.textContent = "Dashboard error";
  }
}

// ---- Now page -------------------------------------------------------------

function initNow() {
  const tbody = document.querySelector("#now-table tbody");
  const empty = document.getElementById("now-empty");
  const showHidden = document.getElementById("show-hidden");

  function render(s) {
    document.getElementById("active-count").textContent = s.active_count;
    document.getElementById("total-count").textContent = s.total_count;
    document.getElementById("now-updated").textContent = s.updated_at
      ? "updated " + fmtTime(s.updated_at) : "";

    const devices = (s.devices || []).filter((d) => showHidden.checked || !d.hidden);
    empty.hidden = devices.length > 0;
    tbody.innerHTML = devices.map((d) => `
      <tr class="${d.hidden ? "offline" : ""}">
        <td>${esc(d.name)}${d.is_new ? '<span class="badge new">NEW</span>' : ""}${d.watched ? '<span class="star" title="watched">★</span>' : ""}</td>
        <td class="ip">${esc(d.ip || "—")}</td>
        <td class="mac">${esc(d.mac)}</td>
        <td>${esc(d.interface_type || "—")}</td>
        <td class="muted">${esc(d.active_since || "—")}</td>
      </tr>`).join("");
  }

  snapshotHook = render;
  showHidden.addEventListener("change", () => refreshStatus());
  refreshRecentEvents();
  setInterval(refreshRecentEvents, REFRESH_MS);
}

async function refreshRecentEvents() {
  const ul = document.getElementById("recent-events");
  if (!ul) return;
  try {
    const events = await fetchJSON("/api/events?limit=20");
    ul.innerHTML = events.length
      ? events.map((e) => `
        <li><span class="ev-tag ev-${e.event}">${e.event}</span>
        <span>${esc(e.name)}</span><time>${fmtDateTime(e.ts)}</time></li>`).join("")
      : '<li class="muted">No activity recorded yet.</li>';
  } catch (e) { /* keep previous content */ }
}

// ---- History page ---------------------------------------------------------

function lineChart(id, datasets, stackedY) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { labels: [], datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8, color: AXIS }, grid: { color: GRID } },
        y: { stacked: !!stackedY, beginAtZero: true, ticks: { precision: 0, color: AXIS }, grid: { color: GRID } },
      },
      plugins: { legend: { labels: { color: AXIS } } },
    },
  });
}

function initHistory() {
  let range = "24h", scope = "watched", days = 14;

  const countChart = lineChart("history-chart", [
    { label: "Active", data: [], borderColor: PALETTE[0], backgroundColor: "rgba(59,167,118,.15)", fill: true, tension: .25, pointRadius: 0, borderWidth: 2 },
    { label: "Known", data: [], borderColor: PALETTE[1], backgroundColor: "transparent", fill: false, tension: .25, pointRadius: 0, borderWidth: 1, borderDash: [4, 4] },
  ]);
  const interfaceChart = lineChart("interface-chart", [], true);
  const dailyChart = new Chart(document.getElementById("daily-chart"), {
    type: "bar",
    data: { labels: [], datasets: [
      { label: "New", data: [], backgroundColor: PALETTE[2] },
      { label: "Came online", data: [], backgroundColor: PALETTE[0] },
      { label: "Went offline", data: [], backgroundColor: PALETTE[5] },
    ] },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { stacked: true, ticks: { maxTicksLimit: 12, color: AXIS }, grid: { color: GRID } },
        y: { stacked: true, beginAtZero: true, ticks: { precision: 0, color: AXIS }, grid: { color: GRID } },
      },
      plugins: { legend: { labels: { color: AXIS } } },
    },
  });

  const labelFor = (iso) => {
    const d = new Date(iso);
    if (range === "1h" || range === "24h") return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  };

  async function loadCount() {
    try {
      const { points } = await fetchJSON("/api/history?range=" + range);
      document.getElementById("history-empty").hidden = points.length > 0;
      countChart.data.labels = points.map((p) => labelFor(p.ts));
      countChart.data.datasets[0].data = points.map((p) => p.active);
      countChart.data.datasets[1].data = points.map((p) => p.total);
      countChart.update();
    } catch (e) {}
  }

  async function loadInterface() {
    try {
      const { labels, series } = await fetchJSON("/api/interfaces?range=" + range);
      document.getElementById("interface-empty").hidden = series.length > 0;
      interfaceChart.data.labels = labels.map(labelFor);
      interfaceChart.data.datasets = series.map((s, i) => ({
        label: s.interface, data: s.data,
        borderColor: PALETTE[i % PALETTE.length],
        backgroundColor: PALETTE[i % PALETTE.length] + "55",
        fill: i === 0 ? "origin" : "-1", tension: .25, pointRadius: 0, borderWidth: 1.5,
      }));
      interfaceChart.update();
    } catch (e) {}
  }

  async function loadTimeline() {
    try {
      const data = await fetchJSON(`/api/timeline?range=${range}&scope=${scope}`);
      renderTimeline(data);
      renderLeaderboard(data);
      document.getElementById("lb-range").textContent = "· last " + range;
    } catch (e) {}
  }

  async function loadHeatmap() {
    try {
      renderHeatmap(await fetchJSON(`/api/heatmap?days=${days}&scope=${scope}`));
    } catch (e) {}
  }

  async function loadDaily() {
    try {
      const d = await fetchJSON(`/api/daily?days=${days}`);
      dailyChart.data.labels = d.days.map(fmtDayShort);
      dailyChart.data.datasets[0].data = d.new;
      dailyChart.data.datasets[1].data = d.online;
      dailyChart.data.datasets[2].data = d.offline;
      dailyChart.update();
    } catch (e) {}
  }

  const onRange = () => { loadCount(); loadInterface(); loadTimeline(); };
  const onScope = () => { loadTimeline(); loadHeatmap(); };
  const onDays = () => { loadHeatmap(); loadDaily(); };

  bindButtons("ranges", "range", (v) => { range = v; onRange(); });
  bindButtons("tl-scope", "scope", (v) => { scope = v; onScope(); });
  bindButtons("days", "days", (v) => { days = parseInt(v, 10); onDays(); });

  const reloadAll = () => { loadCount(); loadInterface(); loadTimeline(); loadHeatmap(); loadDaily(); };
  reloadAll();
  setInterval(reloadAll, REFRESH_MS * 2);
}

function bindButtons(containerId, dataKey, onPick) {
  const el = document.getElementById(containerId);
  el.addEventListener("click", (ev) => {
    const btn = ev.target.closest(`button[data-${dataKey}]`);
    if (!btn) return;
    el.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    onPick(btn.dataset[dataKey]);
  });
}

function renderTimeline(data) {
  const tl = document.getElementById("timeline");
  const start = new Date(data.start).getTime();
  const end = new Date(data.end).getTime();
  const span = Math.max(end - start, 1);
  document.getElementById("tl-start").textContent = fmtDateTime(data.start);
  document.getElementById("tl-end").textContent = "now";
  document.getElementById("timeline-empty").hidden = data.devices.length > 0;

  tl.innerHTML = data.devices.map((d) => {
    const segs = d.intervals.map((iv) => {
      const s = Math.max(new Date(iv.start).getTime(), start);
      const e = Math.min(new Date(iv.end).getTime(), end);
      const left = (100 * (s - start)) / span;
      const width = Math.max((100 * (e - s)) / span, 0.4);
      return `<span class="seg" style="left:${left}%;width:${width}%" title="${fmtDateTime(iv.start)} → ${fmtDateTime(iv.end)}"></span>`;
    }).join("");
    const dot = d.is_active ? '<span class="dot ok"></span>' : '<span class="dot"></span>';
    return `<div class="tl-row">
      <div class="tl-label" title="${esc(d.name)}">${dot}${esc(d.name)}</div>
      <div class="tl-track">${segs}</div>
      <div class="tl-uptime">${d.uptime_pct}%</div>
    </div>`;
  }).join("");
}

function renderLeaderboard(data) {
  const el = document.getElementById("leaderboard");
  const devs = [...data.devices].sort((a, b) => b.uptime_pct - a.uptime_pct);
  document.getElementById("leaderboard-empty").hidden = devs.length > 0;
  el.innerHTML = devs.map((d) => `
    <div class="lb-row">
      <div class="lb-label" title="${esc(d.name)}">${esc(d.name)}</div>
      <div class="lb-bar"><span style="width:${d.uptime_pct}%"></span></div>
      <div class="lb-val">${d.uptime_pct}%</div>
    </div>`).join("");
}

function renderHeatmap(data) {
  const el = document.getElementById("heatmap");
  document.getElementById("heatmap-empty").hidden = data.devices.length > 0;
  const cols = `170px repeat(${data.days.length}, minmax(26px, 1fr))`;
  const head = `<div class="hm-row" style="grid-template-columns:${cols}">
    <div class="hm-label"></div>
    ${data.days.map((d) => `<div class="hm-daylabel" title="${d}">${parseInt(d.slice(8, 10), 10)}</div>`).join("")}
  </div>`;
  const rows = data.devices.map((dev) => `
    <div class="hm-row" style="grid-template-columns:${cols}">
      <div class="hm-label" title="${esc(dev.name)}">${esc(dev.name)}</div>
      ${dev.cells.map((p, i) => `<div class="hm-cell" title="${data.days[i]}: ${p}% online" style="background:color-mix(in srgb, var(--accent) ${p}%, var(--panel-2))"></div>`).join("")}
    </div>`).join("");
  el.innerHTML = head + rows;
}

// ---- Devices page ---------------------------------------------------------

function initDevices() {
  const tbody = document.querySelector("#devices-table tbody");
  const empty = document.getElementById("devices-empty");
  const filter = document.getElementById("device-filter");
  let all = [];

  async function load() {
    all = await fetchJSON("/api/devices");
    render();
  }

  function render() {
    const q = filter.value.trim().toLowerCase();
    const rows = all.filter((d) =>
      !q || [d.name, d.router_name, d.last_ip, d.mac].some((v) => (v || "").toLowerCase().includes(q)));
    empty.hidden = rows.length > 0;
    tbody.innerHTML = rows.map(rowHtml).join("");
  }

  function rowHtml(d) {
    const status = d.is_active ? '<span class="dot ok" title="online"></span>'
                               : '<span class="dot" title="offline"></span>';
    return `
      <tr data-mac="${esc(d.mac)}" class="${d.is_active ? "" : "offline"}">
        <td><button class="mini-btn" data-events>Events</button></td>
        <td style="text-align:center">${status}</td>
        <td class="name-cell"><input class="label-edit" value="${esc(d.label || "")}" placeholder="${esc(d.router_name || d.mac)}" data-field="label"></td>
        <td class="ip">${esc(d.last_ip || "—")}</td>
        <td class="mac">${esc(d.mac)}</td>
        <td>${esc(d.interface_type || "—")}</td>
        <td class="muted" title="${fmtDateTime(d.first_seen)}">${fmtAgo(d.first_seen)}</td>
        <td class="muted" title="${fmtDateTime(d.last_seen)}">${fmtAgo(d.last_seen)}</td>
        <td style="text-align:center"><input type="checkbox" data-field="watched" ${d.watched ? "checked" : ""}></td>
        <td style="text-align:center"><input type="checkbox" data-field="hidden" ${d.hidden ? "checked" : ""}></td>
      </tr>`;
  }

  async function save(mac, field, value) {
    try {
      await fetchJSON("/api/devices/" + encodeURIComponent(mac), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: value }),
      });
      const dev = all.find((d) => d.mac === mac);
      if (dev) dev[field] = value;
    } catch (e) { alert("Failed to save: " + e.message); }
  }

  tbody.addEventListener("change", (ev) => {
    const el = ev.target;
    const tr = el.closest("tr[data-mac]");
    if (!tr || !el.dataset.field) return;
    save(tr.dataset.mac, el.dataset.field, el.type === "checkbox" ? el.checked : el.value);
  });

  tbody.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-events]");
    if (!btn) return;
    const tr = btn.closest("tr[data-mac]");
    const existing = tr.nextElementSibling;
    if (existing && existing.classList.contains("subrow")) { existing.remove(); return; }
    const events = await fetchJSON("/api/devices/" + encodeURIComponent(tr.dataset.mac) + "/events?limit=50");
    const sub = document.createElement("tr");
    sub.className = "subrow";
    const inner = events.length
      ? events.map((e) => `<li><span class="ev-tag ev-${e.event}">${e.event}</span><time>${fmtDateTime(e.ts)}</time></li>`).join("")
      : '<li class="muted">No events recorded.</li>';
    sub.innerHTML = `<td colspan="10"><ul class="events">${inner}</ul></td>`;
    tr.after(sub);
  });

  filter.addEventListener("input", render);
  load();
  setInterval(load, REFRESH_MS * 2);
}

// ---- boot -----------------------------------------------------------------

initTheme();
if (document.getElementById("now-root")) initNow();
if (document.getElementById("history-root")) initHistory();
if (document.getElementById("devices-root")) initDevices();

refreshStatus();
setInterval(refreshStatus, REFRESH_MS);
