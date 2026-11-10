// Health: is the gear behaving? Always-on devices that dropped out, devices that
// keep falling off Wi-Fi, and the stretches the monitor itself could not see.

import {
  SLOW_REFRESH_MS, api, byId, el, fmtDay, fmtDur, fmtPct, fmtTime, fmtWhen, plural, segmented,
  spanAt, strip, stripAxis, tableView, tipBody,
} from "./core.js";
import { barsHeight, chart, rankBars } from "./charts.js";

const root = byId("health-root");
let days = 30;
let data = null;

const dropsChart = chart(byId("hl-drops-chart"), rankBars, {
  onClick: (p) => { const d = data?.drops[p.dataIndex]; if (d) location.href = `/patterns?subject=${encodeURIComponent(d.key)}`; },
});

function tile(label, value, sub) {
  return el("div", { class: "tile" },
    el("span", { class: "t-label" }, label), el("span", { class: "t-value" }, value), el("span", { class: "t-sub" }, sub || " "));
}

function tiles() {
  const c = data.coverage;
  const off = data.always_on.filter((a) => !a.active);
  const worst = data.drops[0];
  byId("hl-tiles").replaceChildren(
    tile("Monitored", fmtPct(c.share, c.share > 0.995 && c.share < 1 ? 1 : 0),
      c.gaps.length ? `${plural(c.gaps.length, "gap")}, longest ${fmtDur(c.longest_s)}` : "No gaps"),
    tile("Always-on gear online", data.always_on.length ? `${data.always_on.length - off.length} of ${data.always_on.length}` : "–",
      off.length ? `Offline: ${off.map((a) => a.name).join(", ")}` : data.always_on.length ? "All online" : "None identified yet"),
    tile("Outages of always-on gear", String(data.always_on.reduce((n, a) => n + a.outages, 0)),
      (() => {
        const longest = [...data.always_on].sort((a, b) => b.longest_s - a.longest_s)[0];
        return longest && longest.longest_s ? `Longest: ${longest.name}, ${fmtDur(longest.longest_s)}` : "None of 5 minutes or more";
      })()),
    tile("Short Wi-Fi drops", [String(data.drops_per_day), " ", el("small", null, "a day")],
      worst ? `Most: ${worst.name}, ${worst.per_day} a day` : "Nothing notable"));
}

function describeAlways(a, t) {
  const when = (x) => fmtWhen(x, data.now);
  const gap = spanAt(data.coverage.gaps, t);
  if (gap) return tipBody("Not monitored", [], `${when(gap[0])} to ${when(gap[1])}`);
  if (t < a.from) return tipBody(a.name, [], "Not on the network yet");
  const down = spanAt(a.down, t);
  if (down) {
    return tipBody(a.name, [["box down", "offline", fmtDur(down[1] - down[0])]],
      `${when(down[0])} to ${down[1] >= data.last_sample ? "now" : when(down[1])}`);
  }
  return tipBody(a.name, [["", "online"]], `${fmtDay(t)}, ${fmtTime(t)}`);
}

function alwaysRows() {
  const { start, end } = data;
  const gaps = data.coverage.gaps;
  const kids = [];
  if (data.always_on.length) {
    kids.push(el("div", { class: "r head" }, el("span", null, ""), el("span", null, "Now"),
      el("div", { class: "strip-cell" }, stripAxis(start, end, data.stale ? "" : "now")),
      el("span", { class: "num" }, "Online"), el("span", { class: "num" }, "Outages")));
  }
  for (const a of data.always_on) {
    const state = a.active
      ? [el("span", { class: "state" }, el("i", { class: "dot ok" }), "Online")]
      : [el("span", { class: "state" }, el("i", { class: "dot bad" }), "Offline"),
        el("small", null, `since ${fmtWhen(a.last_seen, data.now)}`)];
    kids.push(el("a", { class: "r", href: `/patterns?subject=${encodeURIComponent(a.key)}` },
      el("div", { class: "who", title: a.name }, el("b", null, a.name), a.owner && el("span", null, a.owner)),
      el("div", { class: "what" }, state),
      el("div", { class: "strip-cell" }, strip({
        start, end,
        layers: [
          { cls: "down", spans: a.down },
          { cls: "gap", spans: a.from > start ? [[start, a.from], ...gaps] : gaps },
        ],
        describe: (t) => describeAlways(a, t),
      })),
      el("span", { class: "num" }, a.uptime == null ? "–" : fmtPct(a.uptime, a.uptime > 0.995 && a.uptime < 1 ? 2 : 1)),
      el("span", { class: "num" }, a.outages ? `${a.outages}, longest ${fmtDur(a.longest_s)}` : "none")));
  }
  byId("hl-rows").replaceChildren(...kids);
  byId("hl-rows-empty").hidden = kids.length > 0;
}

function drops() {
  const items = data.drops.map((d) => ({
    name: d.name,
    value: d.per_day,
    note: `${d.drops} in ${plural(data.days, "day")}, while connected about ${d.connected_h_per_day} h a day`,
  }));
  byId("hl-drops-empty").hidden = items.length > 0;
  byId("hl-drops-chart").hidden = items.length === 0;
  if (!items.length) return;
  dropsChart.height(barsHeight(items.length));
  dropsChart.set({ items, unit: "short drops a day" });
}

function monitoring() {
  const { start, end, coverage: c } = data;
  byId("hl-cover").replaceChildren(
    stripAxis(start, end, data.stale ? "" : "now"),
    strip({
      start, end,
      layers: [{ cls: "on", spans: c.spans }, { cls: "gap", spans: c.gaps }],
      describe: (t) => {
        const gap = spanAt(c.gaps, t);
        return gap
          ? tipBody("Not monitored", [["", "for", fmtDur(gap[1] - gap[0])]], `${fmtWhen(gap[0], data.now)} to ${fmtWhen(gap[1], data.now)}`)
          : tipBody(`${fmtDay(t)}, ${fmtTime(t)}`, [["box", "polling"]]);
      },
    }));
  const table = byId("hl-gaps");
  table.hidden = c.gaps.length === 0;
  table.querySelector("tbody").replaceChildren(...[...c.gaps].reverse().map(([a, b]) => el("tr", null,
    el("td", null, `${fmtDay(a)}, ${fmtTime(a)}`),
    el("td", null, b >= data.now - 1 && data.stale ? "still not polling" : `${fmtDay(b)}, ${fmtTime(b)}`),
    el("td", { class: "num" }, fmtDur(b - a)))));
}

const dropsTable = tableView(byId("hl-drops"), () => data && {
  columns: [{ label: "Device" }, { label: "Owner" }, { label: "Short drops", num: true }, { label: "Per day", num: true },
    { label: "Connected hours a day", num: true }, { label: "Drops per connected hour", num: true }],
  rows: data.drops.map((d) => [d.name, d.owner, d.drops, d.per_day, d.connected_h_per_day, d.per_connected_h]),
});

function render() {
  byId("hl-range").textContent = `${fmtDay(data.start)} to ${data.stale ? fmtDay(data.end) : "now"}`;
  tiles();
  alwaysRows();
  drops();
  monitoring();
  dropsTable.refresh();
  root.classList.remove("loading");
}

async function load(quiet = false) {
  if (!quiet) root.classList.add("loading");
  try { data = await api("/api/health", { days }); render(); } catch (e) { root.classList.remove("loading"); }
}

segmented(byId("hl-days"), days, (v) => { days = +v; load(); });
load();
setInterval(() => { if (!document.hidden) load(true); }, SLOW_REFRESH_MS);
