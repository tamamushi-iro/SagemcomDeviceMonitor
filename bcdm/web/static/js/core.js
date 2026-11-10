// Shared by every page: fetching, safe DOM building, formatting, theme, sidebar status.
// Device names come from the network (any device chooses its own hostname), so
// nothing here builds HTML from strings: text always goes in as text nodes.

export const TZ = (() => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (e) { return "UTC"; }
})();

const POLL_S = parseFloat(document.body.dataset.pollInterval) || 30;
export const REFRESH_MS = Math.min(Math.max(POLL_S, 10), 60) * 1000;
export const SLOW_REFRESH_MS = 5 * 60 * 1000;

export async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

export const api = (path, params = {}) => getJSON(`${path}?${new URLSearchParams({ ...params, tz: TZ })}`);

export const postJSON = (url, body) => getJSON(url, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const byId = (id) => document.getElementById(id);

/** Build an element. Children that are strings become text nodes. */
export function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (k in n && k !== "list" && k !== "style") n[k] = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(3)) {
    if (kid == null || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

// ---- formatting (viewer's locale and timezone) ------------------------------

const F = {
  time: new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }),
  hour: new Intl.DateTimeFormat(undefined, { hour: "numeric" }),
  day: new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" }),
  dayNum: new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric" }),
  date: new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" }),
  dateYear: new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" }),
  month: new Intl.DateTimeFormat(undefined, { month: "short" }),
  weekday: new Intl.DateTimeFormat(undefined, { weekday: "short" }),
  weekdayLong: new Intl.DateTimeFormat(undefined, { weekday: "long" }),
};

const date = (t) => new Date(t * 1000);
/** "2026-11-09" as a local calendar day. */
export const parseDay = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };

export const fmtTime = (t) => F.time.format(date(t));
export const fmtDay = (t) => F.day.format(date(t));
export const fmtDate = (t) => F.date.format(date(t));
export const fmtIsoDay = (iso) => F.day.format(parseDay(iso));
export const fmtMonth = (d) => F.month.format(d);
/** Wall-clock minutes since midnight, e.g. 510 -> "08:30". */
export const fmtClock = (min) => (min >= 1440 ? "24:00" : F.time.format(new Date(2000, 0, 1, 0, Math.round(min))));
export const fmtHour = (h) => F.hour.format(new Date(2000, 0, 1, h));

// Monday-first, to match the server's weekday numbers (0 = Monday).
export const DOW = [...Array(7)].map((_, i) => F.weekday.format(new Date(2024, 0, 1 + i)));
export const DOW_LONG = [...Array(7)].map((_, i) => F.weekdayLong.format(new Date(2024, 0, 1 + i)));

const dayIndex = (d) => Math.round(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 86400000);

/** A time relative to now: "08:45", "yesterday 08:45", "Mon 08:45", "3 Nov 08:45". */
export function fmtWhen(t, now = Date.now() / 1000) {
  const d = date(t);
  const back = dayIndex(date(now)) - dayIndex(d);
  const time = F.time.format(d);
  if (back === 0) return time;
  if (back === 1) return `yesterday ${time}`;
  if (back > 1 && back < 7) return `${F.weekday.format(d)} ${time}`;
  return `${(back > 300 ? F.dateYear : F.date).format(d)} ${time}`;
}

export function fmtDur(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return "under a minute";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

export const fmtHours = (h) => (h == null ? "–" : `${h < 10 ? (Math.round(h * 10) / 10) : Math.round(h)} h`);
export const fmtPct = (x, digits = 0) => (x == null ? "–" : `${(x * 100).toFixed(digits)}%`);
export const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

export const KIND_LABEL = {
  always_on: "Always on",
  regular: "Regular",
  occasional: "Occasional",
  new: "New",
  gone: "Not seen lately",
  never: "Never connected",
};

// ---- theme --------------------------------------------------------------------

const themeSubs = new Set();
/** Run `cb` whenever the light/dark theme changes (charts re-read their colours). */
export const onTheme = (cb) => themeSubs.add(cb);
export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function initTheme() {
  const root = document.documentElement;
  const label = byId("theme-label");
  const apply = (t, announce) => {
    root.setAttribute("data-theme", t);
    try { localStorage.setItem("bcdm-theme", t); } catch (e) { /* private mode */ }
    if (label) label.textContent = t === "dark" ? "Light mode" : "Dark mode";
    if (announce) themeSubs.forEach((cb) => cb(t));
  };
  apply(root.getAttribute("data-theme") || "light", false);
  const btn = byId("theme-toggle");
  if (btn) btn.addEventListener("click", () => apply(root.getAttribute("data-theme") === "dark" ? "light" : "dark", true));
}

// ---- sidebar status (every page) ------------------------------------------------

async function refreshStatus() {
  const dot = byId("status-dot");
  const txt = byId("status-text");
  const notice = byId("status-notice");
  if (!dot || !txt) return;
  try {
    const s = await getJSON("/api/now");
    if (s.online) {
      dot.className = "dot ok";
      txt.textContent = `Live · ${F.time.format(new Date(s.updated_at))}`;
    } else {
      dot.className = s.error ? "dot bad" : "dot";
      txt.textContent = s.login_disabled ? "Router login disabled"
        : s.error ? "Router unreachable" : "Waiting for the first poll";
    }
    txt.title = s.online ? "" : (s.error || "");
    // Logins stopped after rejected passwords / a lockout: say why and how to fix it.
    if (notice) {
      notice.hidden = !s.login_disabled;
      notice.textContent = s.login_disabled ? (s.error || "") : "";
    }
  } catch (e) {
    dot.className = "dot bad";
    txt.textContent = "Dashboard unreachable";
  }
}

// ---- small UI pieces ------------------------------------------------------------

/** A button group: buttons carry data-value; `onPick(value)` fires on a change. */
export function segmented(root, value, onPick) {
  const mark = (v) => root.querySelectorAll("button").forEach((b) => {
    const on = b.dataset.value === String(v);
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  mark(value);
  root.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-value]");
    if (!btn || btn.classList.contains("on")) return;
    mark(btn.dataset.value);
    onPick(btn.dataset.value);
  });
  return { set: mark };
}

let tipEl = null;
function placeTip(ev) {
  const pad = 12;
  const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad + 4;
  if (x + w > innerWidth - 6) x = ev.clientX - w - pad;
  if (y + h > innerHeight - 6) y = ev.clientY - h - pad;
  tipEl.style.left = `${Math.max(6, x)}px`;
  tipEl.style.top = `${Math.max(6, y)}px`;
}
/** One shared tooltip for hand-built marks. `content` is a DOM node. */
export const tip = {
  show(ev, content) {
    if (!tipEl) { tipEl = el("div", { class: "tip tt", role: "tooltip" }); document.body.append(tipEl); }
    tipEl.replaceChildren(content);
    tipEl.hidden = false;
    placeTip(ev);
  },
  hide() { if (tipEl) tipEl.hidden = true; },
};

/** Tooltip body: a muted heading, then rows of [swatch class, text, strong value]. */
export function tipBody(head, rows = [], note = null) {
  return el("div", { class: "tt" },
    head && el("div", { class: "tt-head" }, head),
    rows.map(([key, text, value]) => el("div", { class: "tt-row" },
      key && el("i", { class: key }), value != null && el("b", null, value), text)),
    note && el("div", { class: "tt-note" }, note));
}

export function table(columns, rows) {
  return el("table", { class: "grid" },
    el("thead", null, el("tr", null, columns.map((c) => el("th", { class: c.num ? "num" : null }, c.label)))),
    el("tbody", null, rows.map((r) => el("tr", null,
      r.map((v, i) => el("td", { class: columns[i].num ? "num" : null }, v == null ? "–" : v))))));
}

/** Give a chart card a "Table" button showing the same numbers as rows. */
export function tableView(card, build) {
  const holder = el("div", { class: "table-view", hidden: true });
  const render = () => { const t = build(); holder.replaceChildren(t ? table(t.columns, t.rows) : el("p", { class: "empty" }, "No data.")); };
  const btn = el("button", { class: "btn quiet", type: "button", "aria-pressed": "false", title: "Show the numbers behind this chart" }, "Table");
  btn.addEventListener("click", () => {
    const open = holder.hidden;
    holder.hidden = !open;
    btn.classList.toggle("on", open);
    btn.setAttribute("aria-pressed", String(open));
    if (open) render();
  });
  card.querySelector(".card-head").append(btn);
  card.append(holder);
  return { refresh() { if (!holder.hidden) render(); } };
}

/**
 * Reference lines for a strip over [start, end], as epoch seconds: local midnights,
 * plus noons (minor) on short spans; on spans of weeks only the Mondays.
 */
export function dayTicks(start, end) {
  const spanDays = (end - start) / 86400;
  const out = [];
  const d = date(start);
  d.setHours(12, 0, 0, 0);
  if (d.getTime() / 1000 < start) d.setHours(24, 0, 0, 0);
  while (d.getTime() / 1000 < end) {
    const noon = d.getHours() !== 0;
    const keep = noon ? spanDays <= 4 : spanDays <= 16 || d.getDay() === 1;
    if (keep) out.push({ t: d.getTime() / 1000, minor: noon });
    d.setHours(d.getHours() + 12);
  }
  return out;
}

/**
 * A horizontal presence strip over [start, end]. `layers` is a list of
 * { cls, spans: [[from, to], ...] } drawn in order; `describe(t)` returns the
 * tooltip node for the instant under the pointer.
 */
export function strip({ start, end, layers, describe, thin = false, ticks = true }) {
  const span = Math.max(end - start, 1);
  const pct = (t) => `${(100 * (Math.min(Math.max(t, start), end) - start)) / span}%`;
  const node = el("div", { class: thin ? "strip thin" : "strip" });
  for (const { cls, spans } of layers) {
    for (const [a, b] of spans) {
      if (b <= start || a >= end) continue;
      const lo = Math.max(a, start), hi = Math.min(b, end);
      node.append(el("i", { class: cls, style: { left: pct(lo), width: `${(100 * (hi - lo)) / span}%` } }));
    }
  }
  if (ticks) for (const k of dayTicks(start, end)) node.append(el("i", { class: k.minor ? "tick minor" : "tick", style: { left: pct(k.t) } }));
  if (describe) {
    const at = (ev) => {
      const r = node.getBoundingClientRect();
      return start + (span * Math.min(Math.max(ev.clientX - r.left, 0), r.width)) / Math.max(r.width, 1);
    };
    node.addEventListener("pointermove", (ev) => { const c = describe(at(ev)); if (c) tip.show(ev, c); else tip.hide(); });
    node.addEventListener("pointerleave", () => tip.hide());
  }
  return node;
}

/** Day labels to sit above strips spanning [start, end]. */
export function stripAxis(start, end, endLabel = "now") {
  const span = Math.max(end - start, 1);
  const node = el("div", { class: "axis" });
  const majors = dayTicks(start, end).filter((k) => !k.minor);
  const fmt = span > 12 * 86400 ? F.date : F.dayNum;
  const step = Math.max(1, Math.ceil(majors.length / 6));
  let n = 0;
  for (const k of majors) {
    if (n++ % step) continue;
    const left = (100 * (k.t - start)) / span;
    if (left > 90) continue;  // would collide with the end label
    node.append(el("span", { class: left < 4 ? "first" : null, style: { left: `${left}%` } }, fmt.format(date(k.t))));
  }
  node.append(el("span", { class: "last", style: { left: "100%" } }, endLabel));
  return node;
}

/** The span of `spans` containing instant t, or null. */
export const spanAt = (spans, t) => spans.find(([a, b]) => t >= a && t < b) || null;

initTheme();
refreshStatus();
setInterval(refreshStatus, Math.min(REFRESH_MS, 20000));
