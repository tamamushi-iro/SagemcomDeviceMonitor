// Devices: what every device is, whose it is, and when it was here.
// A "device" may stand for several MAC addresses: phones that rotate their private
// address are merged when their addresses were never online at the same time.

import {
  KIND_LABEL, SLOW_REFRESH_MS, api, byId, el, fmtDate, fmtDay, fmtTime, fmtWhen, getJSON,
  plural, postJSON, segmented, tableView,
} from "./core.js";
import { chart, deviceMatrix, matrixHeight } from "./charts.js";

const MAX_MATRIX_ROWS = 60;
const SHOWS = ["seen", "online", "new", "unowned", "hidden", "all"];
const root = byId("devices-root");
const tbody = byId("dv-table").querySelector("tbody");
const query = new URLSearchParams(location.search);

let show = SHOWS.includes(query.get("show")) ? query.get("show") : "seen";
let days = 90;
let list = null;     // /api/devices
let history_ = null; // /api/device-days
const open = new Set();   // device keys whose MAC addresses are expanded

const matrix = chart(byId("dv-matrix-chart"), deviceMatrix, {
  onClick: (p) => {
    const row = p.componentType === "yAxis" ? matrixRows().find((r) => r.name === p.value)
      : p.componentType === "series" ? matrixRows()[p.value[1]] : null;
    if (row) location.href = `/patterns?subject=${encodeURIComponent(row.key)}`;
  },
});

const PICK = {
  seen: (d) => d.kind !== "never" && !d.hidden,
  online: (d) => d.active,
  new: (d) => d.kind === "new",
  unowned: (d) => d.kind !== "never" && !d.owner && !d.hidden,
  hidden: (d) => d.hidden,
  all: () => true,
};

function visible() {
  const q = byId("dv-filter").value.trim().toLowerCase();
  return list.devices.filter(PICK[show]).filter((d) => !q
    || [d.name, d.hostname, d.label, d.owner, ...d.macs.flatMap((m) => [m.mac, m.ip])]
      .some((v) => (v || "").toLowerCase().includes(q)));
}

/** History rows for the devices the table is showing, newest arrival first. */
function matrixAll() {
  if (!history_ || !list) return [];
  const keep = new Set(visible().map((d) => d.key));
  return history_.devices.filter((r) => keep.has(r.key));
}
const matrixRows = () => matrixAll().slice(0, MAX_MATRIX_ROWS);

function drawMatrix() {
  if (!history_ || !list) return;
  const total = matrixAll().length;
  const rows = matrixRows();
  byId("dv-matrix-empty").hidden = rows.length > 0;
  byId("dv-matrix-chart").hidden = rows.length === 0;
  const unit = history_.step_days > 1 ? "Each cell is a week (average hours a day)" : "Each cell is a day";
  byId("dv-matrix-sub").textContent = `${unit}; most recently arrived at the top. Click a row for its pattern.`
    + (total > rows.length ? ` Showing ${rows.length} of ${total}.` : "");
  if (!rows.length) return;
  matrix.height(matrixHeight(rows.length));
  matrix.set({ bins: history_.bins, rows, step: history_.step_days });
}

// ---- table ---------------------------------------------------------------------------

const macsOf = (d) => d.macs.map((m) => m.mac);

let reloadTimer = null;
/** Re-read the list once the person has stopped typing in the table. */
function reloadSoon() {
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(() => {
    if (document.activeElement?.matches?.("input.edit")) { reloadSoon(); return; }
    loadList(true).catch(() => {});
  }, 700);
}

async function save(d, field, value, input) {
  try {
    await postJSON("/api/devices", { macs: macsOf(d), [field]: value });
    d[field] = typeof value === "string" ? (value.trim() || null) : value;
    if (input) { input.classList.add("saved"); setTimeout(() => input.classList.remove("saved"), 1200); }
    reloadSoon();
  } catch (e) {
    alert(`Could not save: ${e.message}`);
  }
}

function statusDot(d) {
  return d.active ? el("i", { class: "dot ok", title: "Online" }) : el("i", { class: "dot hollow", title: "Offline" });
}

function deviceCell(d) {
  const hint = [];
  if (d.macs.length > 1) {
    hint.push(el("button", { class: "linkish", type: "button", "aria-expanded": String(open.has(d.key)),
      onclick: () => { if (open.has(d.key)) open.delete(d.key); else open.add(d.key); renderTable(); } },
    `${d.macs.length} MAC addresses`));
  } else {
    hint.push(el("button", { class: "linkish mono", type: "button", title: "Show connection log",
      onclick: () => { if (open.has(d.key)) open.delete(d.key); else open.add(d.key); renderTable(); } }, d.macs[0].mac));
  }
  if (d.ip) hint.push(" · ", el("span", { class: "mono" }, d.ip));
  if (d.randomized) hint.push(" · private address");
  return el("td", { class: "cell-name" },
    el("input", {
      class: "edit", type: "text", value: d.label || "", placeholder: d.hostname || d.macs[0].mac,
      "aria-label": `Name for ${d.name}`, maxLength: 80,
      onchange: (ev) => save(d, "label", ev.target.value, ev.target),
    }),
    el("div", { class: "hint" }, hint));
}

function row(d) {
  const tr = el("tr", { class: d.active ? null : "dim" },
    el("td", { class: "ctr" }, statusDot(d)),
    deviceCell(d),
    el("td", null, el("input", {
      class: "edit", type: "text", list: "dv-owners", value: d.owner || "",
      placeholder: d.suggestion ? `${d.suggestion}?` : "", maxLength: 40, "aria-label": `Owner of ${d.name}`,
      onchange: (ev) => save(d, "owner", ev.target.value, ev.target),
    })),
    el("td", { class: "ctr" }, d.owner && el("input", {
      type: "checkbox", checked: d.carried, "aria-label": `${d.name} goes where ${d.owner} goes`,
      title: d.carried_auto ? "Guessed from its name. Change it if that is wrong." : "",
      onchange: (ev) => save(d, "carried", ev.target.checked),
    })),
    el("td", null, d.kind === "never" ? KIND_LABEL[d.kind]
      : el("a", { href: `/patterns?subject=${encodeURIComponent(d.key)}`, title: "See when it is usually here" }, KIND_LABEL[d.kind])),
    el("td", { title: d.last_seen ? `${fmtDay(d.last_seen)}, ${fmtTime(d.last_seen)}` : "" },
      d.active ? "online" : d.last_seen ? fmtWhen(d.last_seen, list.now) : "–"),
    el("td", { title: `${fmtDay(d.first_seen)}, ${fmtTime(d.first_seen)}` }, fmtDate(d.first_seen)),
    el("td", { class: "ctr" }, el("input", { type: "checkbox", checked: d.watched, "aria-label": `Watch ${d.name}`,
      onchange: (ev) => save(d, "watched", ev.target.checked) })),
    el("td", { class: "ctr" }, el("input", { type: "checkbox", checked: d.hidden, "aria-label": `Hide ${d.name}`,
      onchange: (ev) => save(d, "hidden", ev.target.checked) })));
  if (!open.has(d.key)) return [tr];
  return [tr, ...d.macs.map((m) => macRow(m))];
}

function macRow(m) {
  const log = el("ul", { class: "events", hidden: true });
  const toggle = async (ev) => {
    if (!log.hidden) { log.hidden = true; ev.target.textContent = "Show log"; return; }
    ev.target.textContent = "Hide log";
    log.hidden = false;
    log.replaceChildren(el("li", null, "Loading…"));
    try {
      const events = await getJSON(`/api/devices/${encodeURIComponent(m.mac)}/events?limit=60`);
      log.replaceChildren(...(events.length
        ? events.map((e) => el("li", null, el("time", null, new Date(e.ts).toLocaleString()), e.event))
        : [el("li", null, "Nothing recorded: it has not changed state since monitoring began.")]));
    } catch (e) { log.replaceChildren(el("li", null, "Could not load the log.")); }
  };
  return el("tr", { class: "sub" },
    el("td", { class: "ctr" }, m.active ? el("i", { class: "dot ok" }) : null),
    el("td", { colspan: 8 },
      el("span", { class: "mono" }, m.mac), "  ",
      [m.ip && `${m.ip}`, m.interface, m.randomized && "private address",
        `first seen ${fmtDate(m.first_seen)}`, `last listed ${fmtWhen(m.last_seen, list.now)}`].filter(Boolean).join(" · "),
      "  ", el("button", { class: "linkish", type: "button", onclick: toggle }, "Show log"),
      log));
}

function renderTable() {
  const rows = visible();
  tbody.replaceChildren(...rows.flatMap(row));
  byId("dv-empty").hidden = rows.length > 0;
  byId("dv-owners").replaceChildren(...list.owners.map((o) => el("option", { value: o })));
  const s = list.summary;
  byId("dv-summary").textContent = `${plural(s.devices, "device")} seen online since ${fmtDate(s.since)}, `
    + `from ${plural(s.addresses, "MAC address", "MAC addresses")}; ${s.online} online now.`;
}

function renderSuggestions() {
  const box = byId("dv-suggest");
  const sug = list.suggestions;
  box.hidden = !sug.length;
  if (!sug.length) return;
  const apply = async (ev) => {
    ev.target.disabled = true;
    try {
      for (const s of sug) await postJSON("/api/devices", { macs: s.macs, owner: s.owner });
      await loadList(true);
    } catch (e) { alert(`Could not assign owners: ${e.message}`); ev.target.disabled = false; }
  };
  box.replaceChildren(
    el("span", { class: "grow" },
      "Some device names say whose they are: ",
      sug.map((s, i) => [i ? ", " : "", el("b", null, s.owner), ` (${plural(s.devices, "device")})`]), "."),
    el("button", { class: "btn primary", type: "button", onclick: apply }, "Assign these owners"));
}

async function loadList(keepFocus = false) {
  const focus = keepFocus && document.activeElement?.getAttribute("aria-label");
  list = await api("/api/devices");
  renderSuggestions();
  renderTable();
  drawMatrix();
  if (focus) tbody.querySelector(`[aria-label="${CSS.escape(focus)}"]`)?.focus();
  root.classList.remove("loading");
}

async function loadHistory() {
  history_ = await api("/api/device-days", { days });
  drawMatrix();
}

const refresh = () => { renderTable(); drawMatrix(); };
segmented(byId("dv-show"), show, (v) => { show = v; refresh(); });
segmented(byId("dv-days"), days, (v) => { days = +v; loadHistory().catch(() => {}); });
byId("dv-filter").addEventListener("input", refresh);

tableView(byId("dv-matrix"), () => history_ && {
  columns: [{ label: "Device" }, { label: "Owner" }, { label: "First online" }, { label: "Days present", num: true }, { label: "Hours a day when present", num: true }],
  rows: matrixRows().map((r) => {
    const total = r.cells.reduce((a, c) => a + c[1], 0);
    return [r.name, r.owner, fmtDay(r.first_on),
      history_.step_days > 1 ? null : r.cells.length, r.cells.length ? Math.round((total / r.cells.length) * 10) / 10 : null];
  }),
});

Promise.all([loadList(), loadHistory()]).catch(() => root.classList.remove("loading"));
setInterval(() => {
  // Don't yank the table out from under someone who is typing in it.
  if (document.hidden || tbody.contains(document.activeElement)) return;
  loadList().catch(() => {});
  loadHistory().catch(() => {});
}, SLOW_REFRESH_MS);
