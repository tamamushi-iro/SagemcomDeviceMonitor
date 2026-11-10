// Patterns: when one person, one device or the whole household is usually here,
// at three zoom levels: every day of the history, a typical week, and day by day.

import {
  DOW, DOW_LONG, KIND_LABEL, SLOW_REFRESH_MS, api, byId, el, fmtClock, fmtDay, fmtDur, fmtHours,
  fmtIsoDay, fmtPct, fmtTime, fmtWhen, plural, segmented, tableView,
} from "./core.js";
import {
  calendarHeat, calendarHeight, calendarScale, chart, countScale, dayHeat, dayRows, dayStrips,
  trendLine, weekHeat, weekScale,
} from "./charts.js";

const WEEKS = [4, 8, 12, 0];
const root = byId("patterns-root");
const query = new URLSearchParams(location.search);
let subject = query.get("subject") || "people";
let weeks = WEEKS.includes(+query.get("weeks")) && query.get("weeks") !== null ? +query.get("weeks") : 8;
let data = null;

const isCount = () => data.subject.kind === "count";
const charts = {
  cal: chart(byId("pt-cal-chart"), calendarHeat),
  week: chart(byId("pt-week-chart"), weekHeat),
  days: chart(byId("pt-days-chart"), (d, t) => (d.subject.kind === "count" ? dayHeat(d, t) : dayStrips(d, t))),
  trend: chart(byId("pt-trend-chart"), trendLine),
};

const round1 = (v) => (v == null ? "–" : String(Math.round(v * 10) / 10));
const scale = (s, extra = []) => [
  el("span", { class: "scale" }, s.lo, el("i", { class: "ramp" }), s.hi), ...extra,
];
const key = (cls, label) => el("span", { class: "key" }, el("i", { class: `sw ${cls}` }), label);

function fillSubjects(opts) {
  const select = byId("pt-subject");
  const group = (label, items) => items.length && el("optgroup", { label },
    items.map((o) => el("option", { value: o.key, selected: o.key === data.subject.key }, o.name)));
  const byKind = (kind) => opts.devices.filter((o) => o.kind === kind);
  select.replaceChildren(...[
    group("Household", opts.counts),
    group("People", opts.people),
    ...["regular", "occasional", "new", "always_on", "gone"].map((k) => group(`Devices: ${KIND_LABEL[k].toLowerCase()}`, byKind(k))),
  ].filter(Boolean));
}

function tile(label, value, sub) {
  return el("div", { class: "tile" },
    el("span", { class: "t-label" }, label), el("span", { class: "t-value" }, value), el("span", { class: "t-sub" }, sub || " "));
}

function usualText(u) {
  if (!u) return ["–", "not enough data"];
  if (u.kind === "home") return ["Home", "most of the day"];
  if (u.kind === "away") return ["Away", "most of the day"];
  const span = `${fmtClock(u.from_min)}–${fmtClock(u.to_min)}`;
  return u.kind === "out" ? [`Out ${span}`, "home for the rest"] : [`Home ${span}`, "out for the rest"];
}

function tiles() {
  const s = data.stats, sub = data.subject;
  let items;
  if (isCount()) {
    const hour = (x) => (x ? `${DOW[x.dow]} ${fmtClock(x.hour * 60)}` : "–");
    items = [
      tile("Average", [round1(s.mean), " ", el("small", null, sub.unit)],
        sub.unit === "devices" && s.always_on ? `${s.always_on} of them are always-on gear` : ""),
      tile("Busiest hour of the week", hour(s.busiest), s.busiest ? `${round1(s.busiest.value)} on average` : ""),
      tile("Quietest hour of the week", hour(s.quietest), s.quietest ? `${round1(s.quietest.value)} on average` : ""),
      tile("Most at once", s.peak ? round1(s.peak.value) : "–", s.peak ? `${fmtDay(s.peak.t)}, ${fmtTime(s.peak.t)}` : ""),
    ];
  } else {
    const now = sub.state === "home" ? ["Home", `${sub.approx ? "since before" : "since"} ${fmtWhen(sub.since, data.now)}`]
      : sub.state === "recent" ? ["Just seen", `${fmtDur(data.now - sub.last_seen)} ago`]
        : sub.state === "away" ? ["Away", `since ${fmtWhen(sub.since, data.now)}`] : ["–", "never connected"];
    const [wd, wdSub] = usualText(s.usual_weekday);
    const [we, weSub] = usualText(s.usual_weekend);
    const away = s.longest_away;
    items = [
      tile("Right now", now[0], now[1]),
      tile("At home", fmtPct(s.share), s.weekday_h == null ? "" : `${fmtHours(s.weekday_h)} on weekdays, ${fmtHours(s.weekend_h)} at weekends`),
      tile("Typical weekday", wd, wdSub),
      tile("Typical weekend", we, weSub),
      tile("Longest absence", away ? fmtDur(away.end - away.start) : "–", !away ? ""
        : fmtDay(away.start) === fmtDay(away.end) ? `${fmtDay(away.start)}, ${fmtTime(away.start)}–${fmtTime(away.end)}`
          : `${fmtDay(away.start)} to ${fmtDay(away.end)}`),
    ];
  }
  byId("pt-tiles").replaceChildren(...items);
}

function members() {
  const sub = data.subject;
  const link = (key, name) => el("a", { href: `?subject=${encodeURIComponent(key)}&weeks=${weeks}` }, name);
  const set = (...kids) => byId("pt-members").replaceChildren(el("span", null, ...kids));  // el() flattens
  if (sub.kind === "person") {
    const list = (items) => items.map((m, i) => [i ? ", " : "", link(m.key, m.name)]);
    const counted = sub.members.filter((m) => m.counted);
    const rest = sub.members.filter((m) => !m.counted);
    set("Seen through ", list(counted), ".", rest.length > 0 && [" Not counted (not carried): ", list(rest), "."]);
  } else if (sub.kind === "device") {
    const m = sub.members[0];
    set(KIND_LABEL[m.kind],
      m.owner && [", belongs to ", link(`owner:${m.owner}`, m.owner)],
      m.macs > 1 && `, ${m.macs} MAC addresses that were never online together`);
  } else {
    set(sub.unit === "people" ? "People are counted as home while any of their devices is." : "Every connected device, always-on gear included.");
  }
}

function captions() {
  const sub = data.subject, n = data.days.length;
  const period = weeks ? `the last ${weeks} weeks` : `all ${n} days`;
  const who = sub.name;
  byId("pt-period").textContent = `${fmtIsoDay(data.start)} to ${fmtIsoDay(data.end)}`;
  byId("pt-cal-title").textContent = `Every day since ${fmtIsoDay(data.first_day)}`;
  const faded = weeks ? " Days outside the period below are faded." : "";
  if (isCount()) {
    byId("pt-cal-sub").textContent = `Average ${sub.unit === "people" ? "people at home" : "devices connected"} per day.${faded}`;
    byId("pt-week-sub").textContent = `Average for each hour of the week over ${period}.`;
    byId("pt-days-sub").textContent = "Each row is a day, newest first; each cell is half an hour.";
    byId("pt-days-legend").replaceChildren(...scale(countScale(data)));
  } else {
    byId("pt-cal-sub").textContent = `How much of each day ${who} was home.${faded}`;
    byId("pt-week-sub").textContent = `How often ${who} was home in each hour of the week, over ${period}.`;
    byId("pt-days-sub").textContent = "Each row is a day, newest first. Weekends are shaded.";
    byId("pt-days-legend").replaceChildren(key("on", "Connected"), key("home", "Brief gap (under 90 min)"), key("gap", "Not monitored"));
  }
  byId("pt-cal-legend").replaceChildren(...scale(calendarScale(data), [key("off", "Not monitored")]));
  byId("pt-week-legend").replaceChildren(...scale(weekScale(data)));
}

const tables = [
  tableView(byId("pt-cal"), () => data && {
    columns: [{ label: "Day" }, { label: isCount() ? "Average" : "Share at home", num: true },
      { label: isCount() ? "Peak" : "Hours at home", num: true }, { label: "Hours monitored", num: true }],
    rows: [...data.calendar].reverse().map(([iso, v, extra, cov]) =>
      [fmtIsoDay(iso), v == null ? null : (isCount() ? round1(v) : fmtPct(v)), extra, cov]),
  }),
  tableView(byId("pt-week"), () => data && {
    columns: [{ label: "Hour" }, ...DOW_LONG.map((label) => ({ label, num: true }))],
    rows: [...Array(24).keys()].map((h) => [fmtClock(h * 60),
      ...data.week.values.map((row) => (row[h] == null ? null : (isCount() ? round1(row[h]) : fmtPct(row[h]))))]),
  }),
  tableView(byId("pt-days"), () => data && (isCount() ? {
    columns: [{ label: "Day" }, { label: "Average", num: true }, { label: "Peak", num: true }, { label: "Hours monitored", num: true }],
    rows: data.days.map((d) => [fmtIsoDay(d.date), d.value, d.peak, d.covered_h]),
  } : {
    columns: [{ label: "Day" }, { label: "Hours at home", num: true }, { label: "At home" }, { label: "Hours monitored", num: true }],
    rows: data.days.map((d) => [fmtIsoDay(d.date), d.value,
      d.home.map(([a, b]) => `${fmtClock(a)}–${fmtClock(b)}`).join(", ") || "away", d.covered_h]),
  })),
];

function render() {
  fillSubjects(data.options);
  tiles();
  members();
  captions();
  const cal = byId("pt-cal-chart");
  charts.cal.height(calendarHeight(cal, data.calendar.length));
  charts.cal.set(data);
  charts.week.set(data);
  charts.days.height(dayRows(data.days.length).height);
  charts.days.set(data);
  byId("pt-trend").hidden = !isCount();
  if (isCount()) charts.trend.set(data);
  tables.forEach((t) => t.refresh());
  document.title = `${data.subject.name} · Patterns`;
  root.classList.remove("loading");
}

async function load(quiet = false) {
  if (!quiet) root.classList.add("loading");
  try {
    data = await api("/api/patterns", { subject, weeks });
  } catch (e) {
    // The subject is gone (an owner was renamed, a device merged): fall back once.
    if (String(e.message).startsWith("404") && subject !== "devices") { subject = "devices"; return load(quiet); }
    root.classList.remove("loading");
    return;
  }
  subject = data.subject.key;
  history.replaceState(null, "", `?subject=${encodeURIComponent(subject)}&weeks=${weeks}`);
  render();
}

byId("pt-subject").addEventListener("change", (ev) => { subject = ev.target.value; load(); });
segmented(byId("pt-weeks"), weeks, (v) => { weeks = +v; load(); });
load();
setInterval(() => { if (!document.hidden) load(true); }, SLOW_REFRESH_MS);
