// Overview: who is here now, the last two days, and whether that is normal.

import {
  DOW_LONG, REFRESH_MS, api, byId, el, fmtClock, fmtDay, fmtDur, fmtTime, fmtWhen, plural,
  spanAt, strip, stripAxis, tableView, tipBody,
} from "./core.js";
import { chart, countVsUsual } from "./charts.js";

const root = byId("overview-root");
const countChart = chart(byId("ov-count-chart"), countVsUsual);
let data = null;

const text = (id, value) => { byId(id).textContent = value; };
const subjectUrl = (key) => `/patterns?subject=${encodeURIComponent(key)}`;

function tiles(d) {
  const c = d.counts;
  const now = new Date(d.now * 1000);

  text("t-connected-label", d.stale ? "Connected at the last poll" : "Connected now");
  text("t-connected", d.has_data ? c.connected : "–");
  let sub = " ";
  if (c.usual && !d.stale) {
    const [lo, , hi] = c.usual;
    const a = Math.round(lo), b = Math.round(hi);
    const verdict = c.connected > hi + 0.5 ? `${Math.round(c.connected - hi)} more than usual`
      : c.connected < lo - 0.5 ? `${Math.round(lo - c.connected)} fewer than usual` : "As usual";
    sub = `${verdict}: ${a === b ? a : `${a} to ${b}`} on ${DOW_LONG[(now.getDay() + 6) % 7]}s around ${fmtClock(now.getHours() * 60)}`;
  }
  text("t-connected-sub", sub);

  if (c.people_total) {
    const here = d.people.filter((p) => p.state !== "away").map((p) => p.name);
    text("t-people", `${c.people_home} of ${c.people_total}`);
    text("t-people-sub", here.length ? here.join(", ") : "Nobody home");
    byId("tile-people").href = "/patterns?subject=people";
  } else {
    text("t-people", "–");
    text("t-people-sub", "Assign owners on the Devices page");
    byId("tile-people").href = "/devices";
  }

  text("t-new", c.new_7d);
  text("t-new-sub", c.new_7d ? `${c.new_online} connected now` : "Nothing unfamiliar joined");

  text("t-always", c.always_on ? `${c.always_on - c.always_on_offline} of ${c.always_on}` : "–");
  text("t-always-sub", !c.always_on ? "None identified yet"
    : c.always_on_offline ? `${plural(c.always_on_offline, "device")} offline` : "All online");

  const host = document.body.dataset.host;
  text("ov-asof", !d.last_sample ? `Waiting for the first poll of ${host}`
    : d.stale ? `As of ${fmtWhen(d.last_sample, d.now)}`
      : `Updated ${fmtWhen(d.last_sample, d.now)}, polling ${host} every ${Math.round(d.monitor.poll_s)} s`);
}

function banners(d) {
  const stale = byId("ov-stale");
  stale.hidden = !(d.stale && d.has_data);
  if (!stale.hidden) {
    const why = d.monitor.login_disabled ? "" : (d.monitor.error || "").split("\n")[0];
    stale.textContent = `No successful poll since ${fmtWhen(d.last_sample, d.now)} (${fmtDur(d.now - d.last_sample)} ago). `
      + `Everything below is the last known state.${why ? ` Latest error: ${why}` : ""}`;
  }
  const owners = byId("ov-owners");
  const names = d.owner_suggestions.map((s) => s.owner);
  owners.hidden = d.counts.people_total > 0 || !names.length;
  if (!owners.hidden) {
    owners.replaceChildren(
      el("span", { class: "grow" },
        "This page can show who is home instead of which devices are. ",
        `${plural(names.length, "name")} already ${names.length === 1 ? "points" : "point"} to a person: ${names.slice(0, 5).join(", ")}${names.length > 5 ? "…" : ""}.`),
      el("a", { class: "btn primary", href: "/devices" }, "Assign owners"));
  }
}

// A person is home or away; a device is only ever known to be connected or not
// (a laptop asleep on the desk is "not connected", not "away").
const isPerson = (r) => r.key.startsWith("owner:");

function stateNode(r, d) {
  const since = r.since ? fmtWhen(r.since, d.now) : "";
  const person = isPerson(r);
  if (r.state === "home") {
    return [el("span", { class: "state" }, el("i", { class: "dot ok" }), person ? "Home" : "Here"),
      el("small", null, `${r.approx ? "since before" : "since"} ${since}`)];
  }
  if (r.state === "recent") {
    return [el("span", { class: "state" }, el("i", { class: "dot" }), "Just seen"),
      el("small", null, `${fmtDur(d.last_sample - r.last_seen)} ago, may still be here`)];
  }
  return [el("span", { class: "state" }, el("i", { class: "dot hollow" }), person ? "Away" : "Not connected"),
    el("small", null, r.approx ? `left after ${since}` : person ? `since ${since}` : `last seen ${since}`)];
}

function describe(r, t, d) {
  const { start, end, gaps } = d.window;
  const when = (x) => fmtWhen(x, d.now);
  const here = isPerson(r) ? "Home" : "Here";
  const gone = isPerson(r) ? "Away" : "Not connected";
  const gap = spanAt(gaps, t);
  if (gap) return tipBody("Not monitored", [], `${when(gap[0])} to ${when(gap[1])}`);
  if (t > d.last_sample) return null;
  const home = spanAt(r.home, t);
  if (home) {
    const early = home[0] <= start, open = home[1] >= Math.min(end, d.last_sample);
    const span = early && open ? "the whole 48 hours" : early ? `until ${when(home[1])}`
      : `${when(home[0])} to ${open ? "now" : when(home[1])} (${fmtDur(home[1] - home[0])})`;
    return tipBody(r.name, [[spanAt(r.on, t) ? "box" : "box soft", spanAt(r.on, t) ? "connected" : "briefly gone, came back"]],
      `${here} ${span}`);
  }
  const before = [...r.home].reverse().find(([, b]) => b <= t);
  const after = r.home.find(([a]) => a > t);
  return tipBody(r.name, [],
    before && after ? `${gone} ${when(before[1])} to ${when(after[0])} (${fmtDur(after[0] - before[1])})`
      : before ? `${gone} since ${when(before[1])}` : after ? `${gone} until ${when(after[0])}` : gone);
}

function homeRows(d) {
  const { start, end, gaps } = d.window;
  const row = (r) => el("a", { class: "r", href: subjectUrl(r.key) },
    el("div", { class: "who", title: r.name }, el("b", null, r.name), r.sub && el("span", null, r.sub)),
    el("div", { class: "what" }, stateNode(r, d)),
    el("div", { class: "strip-cell" }, strip({
      start, end,
      layers: [{ cls: "home", spans: r.home }, { cls: "on", spans: r.on }, { cls: "gap", spans: gaps }],
      describe: (t) => describe(r, t, d),
    })),
    el("span", { class: "num" }, r.today_s >= 60 ? fmtDur(r.today_s) : "–"));

  const kids = [];
  if (d.people.length || d.others.length) {
    kids.push(el("div", { class: "r head" }, el("span", null, ""), el("span", null, "Now"),
      el("div", { class: "strip-cell" }, stripAxis(start, end, d.stale ? "" : "now")), el("span", { class: "num" }, "Home today")));
  }
  d.people.forEach((r) => kids.push(row(r)));
  if (d.others.length) {
    kids.push(el("div", { class: "r sub-head" }, d.people.length ? "Devices without an owner" : "Devices that come and go"));
    d.others.forEach((r) => kids.push(row(r)));
  }
  if (d.others_more) kids.push(el("a", { class: "r", href: "/devices" }, el("span", { class: "who" }, `and ${d.others_more} more`)));
  byId("ov-rows").replaceChildren(...kids);
  byId("ov-rows-empty").hidden = kids.length > 0;
}

function connected(d) {
  const showHidden = byId("ov-hidden").checked;
  const rows = d.connected.filter((c) => showHidden || !c.hidden);
  const hidden = d.connected.length - d.connected.filter((c) => !c.hidden).length;
  byId("ov-table").querySelector("tbody").replaceChildren(...rows.map((c) => {
    // The router knows how long a device has really been connected; the monitor
    // only knows since when it has been watching.
    const routerKnows = c.since_approx && c.router_since && c.router_since < c.since;
    const since = routerKnows ? c.router_since : c.since;
    return el("tr", { class: c.hidden ? "dim" : null },
      el("td", null, el("a", { href: subjectUrl(c.key) }, c.name), c.is_new && [" ", el("span", { class: "tag new" }, "new")]),
      el("td", null, c.owner || ""),
      el("td", { class: "mono", title: c.randomized ? "Private (randomized) MAC address" : "" }, c.ip || "–", el("br"), c.mac),
      el("td", null, c.interface || "–"),
      el("td", { class: "num", title: `since ${fmtDay(since)}, ${fmtTime(since)}` },
        `${c.since_approx && !routerKnows ? "over " : ""}${fmtDur(d.last_sample - since)}`));
  }));
  byId("ov-table-empty").hidden = rows.length > 0;
  text("ov-connected-sub", `${plural(rows.length, "device")}, most recent arrival first.${hidden && !showHidden ? ` ${hidden} hidden.` : ""}`);
}

function feed(d) {
  const today = new Date(d.now * 1000).toDateString();
  const yesterday = new Date((d.now - 86400) * 1000).toDateString();
  const items = [];
  let last = null;
  for (const a of d.activity) {
    const key = new Date(a.t * 1000).toDateString();
    if (key !== last) {
      items.push(el("li", { class: "day" }, key === today ? "Today" : key === yesterday ? "Yesterday" : fmtDay(a.t)));
      last = key;
    }
    const note = a.type === "arrived" && a.after_s ? `after ${fmtDur(a.after_s)} away`
      : a.type === "left" ? `after ${fmtDur(a.after_s)}` : a.approx ? "while not monitored" : "";
    items.push(el("li", null,
      el("time", null, fmtTime(a.t)),
      el("span", { class: "name" }, el("a", { href: subjectUrl(a.key) }, a.name), " ", el("small", null, note)),
      el("span", { class: `kind ${a.type}` }, a.type === "new" ? "new device" : a.type)));
  }
  byId("ov-feed").replaceChildren(...items);
  byId("ov-feed-empty").hidden = items.length > 0;
}

const countTable = tableView(byId("ov-count"), () => {
  if (!data) return null;
  const usual = new Map(data.series.usual.map((u) => [u[0], u]));
  const rows = data.series.points.filter(([t]) => t % 3600 === 0).reverse().map(([t, v]) => {
    const u = usual.get(t);
    return [`${fmtDay(t)}, ${fmtTime(t)}`, v == null ? "not monitored" : v, u ? `${u[1]} to ${u[3]}` : null];
  });
  return { columns: [{ label: "Hour" }, { label: "Connected", num: true }, { label: "Usual range", num: true }], rows };
});

function render() {
  tiles(data);
  banners(data);
  homeRows(data);
  countChart.set(data);
  connected(data);
  feed(data);
  countTable.refresh();
  root.classList.remove("loading");
}

async function load() {
  try {
    data = await api("/api/overview");
    render();
  } catch (e) { /* keep the previous render; the sidebar shows the dashboard is unreachable */ }
}

byId("ov-hidden").addEventListener("change", () => data && connected(data));
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
load();
setInterval(load, REFRESH_MS);
