// ECharts option builders. Every builder is (data, theme) -> option, so a chart can
// be redrawn with new data or after a light/dark switch without keeping state.
//
// Colour does one job per chart: a single blue hue for data (two steps where
// "connected" and "briefly gone" differ, a light-to-dark ramp for magnitude),
// neutral greys for context, red only for downtime.

import {
  DOW, DOW_LONG, cssVar, el, fmtClock, fmtDur, fmtHour, fmtHours, fmtIsoDay, fmtMonth,
  fmtPct, fmtTime, onTheme, parseDay, tipBody,
} from "./core.js";

const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

export function theme() {
  return {
    ink: cssVar("--ink"), ink2: cssVar("--ink-2"), muted: cssVar("--muted"),
    surface: cssVar("--surface"), raised: cssVar("--raised"), track: cssVar("--track"),
    grid: cssVar("--grid"), axis: cssVar("--axis"), border: cssVar("--border"),
    accent: cssVar("--accent"), soft: cssVar("--accent-soft"), critical: cssVar("--critical"),
    hatch: cssVar("--hatch"),
    ramp: [1, 2, 3, 4, 5, 6].map((i) => cssVar(`--seq-${i}`)),
    dark: document.documentElement.getAttribute("data-theme") === "dark",
  };
}

// ---- lifecycle --------------------------------------------------------------------

const live = new Set();

/** Mount a chart on `node`. `build(data, theme, node)` returns an ECharts option. */
export function chart(node, build, { onClick } = {}) {
  const inst = echarts.init(node, null, { renderer: "canvas" });
  const c = {
    inst,
    data: null,
    set(data) { this.data = data; this.draw(); },
    draw() {
      if (this.data == null) return;
      inst.setOption(build(this.data, theme(), node), { notMerge: true });
    },
    /** Change the pixel height (rows changed), then redraw to fit. */
    height(px) {
      if (node.style.height !== `${px}px`) { node.style.height = `${px}px`; inst.resize(); }
    },
  };
  if (onClick) inst.on("click", onClick);
  new ResizeObserver(() => { inst.resize(); c.draw(); }).observe(node);
  live.add(c);
  return c;
}
onTheme(() => live.forEach((c) => c.draw()));

// ---- shared option pieces -----------------------------------------------------------

const base = (t) => ({
  animation: false,
  textStyle: { fontFamily: FONT, color: t.ink2, fontSize: 12 },
  aria: { enabled: true },
});

const tooltip = (t, extra) => ({
  confine: true,
  transitionDuration: 0,
  backgroundColor: t.raised,
  borderColor: t.border,
  borderWidth: 1,
  padding: [7, 10],
  textStyle: { color: t.ink, fontSize: 12.5, fontFamily: FONT },
  extraCssText: `border-radius:8px;box-shadow:${cssVar("--tip-shadow")};`,
  ...extra,
});

const label = (t, extra) => ({ color: t.muted, fontSize: 11, fontFamily: FONT, ...extra });
const quietAxis = (t, extra) => ({
  axisLine: { show: false }, axisTick: { show: false }, splitLine: { show: false },
  axisLabel: label(t), ...extra,
});

/** A faint diagonal hatch: the "nothing is known here" fill, same as the CSS one. */
function hatch(t) {
  const size = 8;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  g.strokeStyle = t.hatch;
  g.lineWidth = 1.2;
  for (const o of [-size, 0, size]) { g.beginPath(); g.moveTo(o, size); g.lineTo(o + size, 0); g.stroke(); }
  return { image: c, repeat: "repeat" };
}

const timeLabel = (v) => {
  const d = new Date(v);
  return d.getHours() === 0 && d.getMinutes() === 0
    ? `{day|${DOW[(d.getDay() + 6) % 7]} ${d.getDate()}}` : fmtTime(v / 1000);
};

// ---- connected devices against the usual range --------------------------------------

export function countVsUsual(d, t) {
  const pts = d.series.points.map(([ts, v]) => [ts * 1000, v]);
  const usual = d.series.usual;
  const usualAt = new Map(usual.map((u) => [u[0], u]));
  const valueAt = new Map(d.series.points.map((p) => [p[0], p[1]]));
  const values = [...pts.map((p) => p[1]), ...usual.flatMap((u) => [u[1], u[3]])].filter((v) => v != null);
  const lo = values.length ? Math.max(0, Math.floor(Math.min(...values) - 1)) : 0;
  const hi = values.length ? Math.ceil(Math.max(...values) + 1) : 10;
  return {
    ...base(t),
    grid: { left: 30, right: 16, top: 10, bottom: 28 },
    xAxis: {
      type: "time", min: d.window.start * 1000, max: d.window.end * 1000,
      axisLine: { lineStyle: { color: t.axis } }, axisTick: { show: false }, splitLine: { show: false },
      axisLabel: label(t, { formatter: timeLabel, hideOverlap: true, rich: { day: { color: t.ink2, fontWeight: 600, fontSize: 11 } } }),
    },
    yAxis: {
      type: "value", min: lo, max: hi, minInterval: 1,
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: t.grid, width: 1 } }, axisLabel: label(t),
    },
    tooltip: tooltip(t, {
      trigger: "axis",
      axisPointer: { type: "line", snap: true, lineStyle: { color: t.axis, width: 1, type: "solid" } },
      formatter: (ps) => {
        const ts = Math.round((ps[0]?.axisValue ?? 0) / 1000);
        const slot = Math.floor(ts / d.series.step) * d.series.step;
        const v = valueAt.get(slot);
        const u = usualAt.get(Math.floor(ts / 3600) * 3600);
        const when = new Date(ts * 1000);
        return tipBody(`${DOW[(when.getDay() + 6) % 7]} ${fmtTime(ts)}`, [
          ["", v == null ? "not monitored" : "connected", v == null ? null : String(Math.round(v * 10) / 10)],
          u ? ["band", "usual range", `${Math.round(u[1])}–${Math.round(u[3])}`] : null,
        ].filter(Boolean));
      },
    }),
    series: [
      // The usual range as a band: an invisible line at its low edge, with the band's
      // height stacked on top and only that part filled.
      { type: "line", stack: "usual", data: usual.map((u) => [u[0] * 1000, u[1]]), symbol: "none", lineStyle: { opacity: 0 }, silent: true, tooltip: { show: false }, z: 1 },
      { type: "line", stack: "usual", data: usual.map((u) => [u[0] * 1000, Math.max(u[3] - u[1], 0.15)]), symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: t.track, opacity: 1 }, silent: true, tooltip: { show: false }, z: 1 },
      {
        type: "line", name: "Connected", data: pts, showSymbol: false, symbol: "circle", symbolSize: 8,
        lineStyle: { width: 2, color: t.accent, join: "round", cap: "round" },
        itemStyle: { color: t.accent, borderColor: t.surface, borderWidth: 2 },
        connectNulls: false, z: 3,
        markArea: {
          silent: true, itemStyle: { color: hatch(t) },
          data: d.window.gaps.map(([a, b]) => [{ xAxis: a * 1000 }, { xAxis: b * 1000 }]),
        },
      },
    ],
  };
}

// ---- typical week: weekday x hour ---------------------------------------------------

export function weekScale(d) {
  if (d.subject.unit === "share") return { min: 0, max: 1, lo: "never", hi: "always" };
  const min = Math.floor(d.week.min), max = Math.max(Math.ceil(d.week.max), min + 1);
  return { min, max, lo: String(min), hi: String(max) };
}

export function weekHeat(d, t) {
  const s = weekScale(d);
  const cells = [];
  d.week.values.forEach((row, dow) => row.forEach((v, h) => { if (v != null) cells.push([h, dow, v]); }));
  const share = d.subject.unit === "share";
  return {
    ...base(t),
    grid: { left: 40, right: 8, top: 24, bottom: 4 },
    xAxis: {
      type: "category", data: [...Array(24).keys()], position: "top",
      ...quietAxis(t, { axisLabel: label(t, { interval: 2, formatter: (h) => fmtHour(+h) }) }),
    },
    yAxis: { type: "category", data: DOW, inverse: true, ...quietAxis(t, { axisLabel: label(t, { color: t.ink2 }) }) },
    visualMap: { show: false, min: s.min, max: s.max, dimension: 2, inRange: { color: t.ramp } },
    tooltip: tooltip(t, {
      formatter: (p) => {
        const [h, dow, v] = p.value;
        return tipBody(`${DOW_LONG[dow]}, ${fmtClock(h * 60)}–${fmtClock(h * 60 + 60)}`, [
          share ? ["", "of the time at home", fmtPct(v)]
            : ["", `${d.subject.unit} on average`, String(Math.round(v * 10) / 10)],
        ]);
      },
    }),
    series: [{
      type: "heatmap", data: cells,
      itemStyle: { borderColor: t.surface, borderWidth: 2, borderRadius: 3 },
      emphasis: { itemStyle: { borderColor: t.ink, borderWidth: 1.5 } },
    }],
  };
}

// ---- day by day ---------------------------------------------------------------------

/** Row height and total chart height for `n` day rows. */
export function dayRows(n) {
  const rowH = n <= 31 ? 18 : n <= 62 ? 13 : n <= 93 ? 10 : 7;
  return { rowH, height: 30 + n * rowH + 8 };
}

function dayAxis(days, t, nowIso) {
  const { rowH } = dayRows(days.length);
  const every = rowH >= 15;
  return {
    type: "category", data: days.map((x) => x.date), inverse: true,
    axisLine: { show: false }, axisTick: { show: false },
    splitLine: { show: true, interval: (i) => days[i].dow === 6, lineStyle: { color: t.grid } },
    axisLabel: label(t, {
      interval: 0, margin: 10,
      formatter: (iso, i) => {
        const day = days[i];
        if (iso === nowIso) return "{b|Today}";
        if (!every && day.dow !== 0) return "";
        const dt = parseDay(iso);
        const text = `${DOW[day.dow]} ${dt.getDate()} ${fmtMonth(dt)}`;
        return day.dow >= 5 ? `{w|${text}}` : text;
      },
      rich: { b: { color: t.ink, fontWeight: 600, fontSize: 11 }, w: { color: t.ink2, fontSize: 11 } },
    }),
  };
}

const clockAxis = (t, extra) => ({
  position: "top", axisLine: { show: false }, axisTick: { show: false },
  splitLine: { show: true, lineStyle: { color: t.grid } }, ...extra,
});

/** A person or device: one row per day, marks where they were home. */
export function dayStrips(d, t) {
  const days = d.days;
  const flat = (key) => days.flatMap((day, i) => day[key].map(([a, b]) => [i, a, b]));
  const bar = (fill, frac, minW, opacity = 1) => (params, api) => {
    const y = api.value(0);
    const p0 = api.coord([api.value(1), y]), p1 = api.coord([api.value(2), y]);
    const h = Math.max(2, api.size([0, 1])[1] * frac);
    return {
      type: "rect",
      shape: { x: p0[0], y: p0[1] - h / 2, width: Math.max(p1[0] - p0[0], minW), height: h, r: frac < 1 ? 1.5 : 0 },
      style: { fill, opacity },
    };
  };
  const layer = (name, data, render, z) => ({
    type: "custom", name, data, renderItem: render, encode: { x: [1, 2], y: 0 }, silent: true, clip: true, z,
  });
  const today = days[0]?.date === d.end ? [[0, d.now_min, d.now_min]] : [];
  return {
    ...base(t),
    grid: { left: 86, right: 12, top: 26, bottom: 6 },
    xAxis: {
      type: "value", min: 0, max: 1440, interval: 180,
      ...clockAxis(t, { axisLabel: label(t, { formatter: (m) => fmtClock(m), showMaxLabel: true, hideOverlap: true }) }),
    },
    yAxis: dayAxis(days, t, d.end),
    tooltip: tooltip(t, {
      trigger: "axis",
      axisPointer: { type: "shadow", axis: "y", shadowStyle: { color: t.ink, opacity: 0.05 } },
      formatter: (ps) => {
        const row = ps.find((p) => p.seriesType === "bar");
        const day = row && days[row.dataIndex];
        if (!day) return "";
        const spans = day.home.slice(0, 6).map(([a, b]) => `${fmtClock(a)}–${fmtClock(b)}`);
        return tipBody(fmtIsoDay(day.date), [
          ["box", "at home", day.covered_h ? fmtHours(day.value) : null],
          ...spans.map((s) => ["", s, null]),
          day.home.length > 6 ? ["", `and ${day.home.length - 6} more`, null] : null,
        ].filter(Boolean), day.covered_h < 23.5 && day.date !== d.end ? `monitored for ${fmtHours(day.covered_h)}` : null);
      },
    }),
    series: [
      // An empty bar per day, so every row answers the pointer even when nobody was home.
      { type: "bar", data: days.map(() => 0), silent: true, itemStyle: { opacity: 0 }, barWidth: 1 },
      layer("weekend", days.map((day, i) => [i, 0, 1440, day.dow]).filter((r) => r[3] >= 5), bar(t.track, 1, 0, 0.5), 1),
      layer("gap", flat("gaps"), bar(hatch(t), 0.72, 0), 2),
      layer("home", flat("home"), bar(t.soft, 0.72, 1), 3),
      layer("on", flat("on"), bar(t.accent, 0.72, 1.5), 4),
      layer("now", today, (params, api) => {
        const p = api.coord([api.value(1), api.value(0)]);
        const h = api.size([0, 1])[1];
        return { type: "rect", shape: { x: p[0] - 0.75, y: p[1] - h / 2, width: 1.5, height: h }, style: { fill: t.ink } };
      }, 5),
    ],
  };
}

export function countScale(d) {
  const vals = d.days.flatMap((day) => day.cells).filter((v) => v != null);
  if (!vals.length) return { min: 0, max: 1 };
  const min = Math.floor(Math.min(...vals)), max = Math.max(Math.ceil(Math.max(...vals)), min + 1);
  return { min, max, lo: String(min), hi: String(max) };
}

/** A count (devices or people): one row per day, one cell per half hour. */
export function dayHeat(d, t) {
  const days = d.days;
  const s = countScale(d);
  const { rowH } = dayRows(days.length);
  const cells = days.flatMap((day, y) => day.cells.map((v, x) => (v == null ? null : [x, y, v])).filter(Boolean));
  return {
    ...base(t),
    grid: { left: 86, right: 12, top: 26, bottom: 6 },
    xAxis: {
      type: "category", data: [...Array(48).keys()],
      ...clockAxis(t, { splitLine: { show: false }, axisLabel: label(t, { interval: 5, formatter: (i) => fmtClock(i * 30), hideOverlap: true }) }),
    },
    yAxis: { ...dayAxis(days, t, d.end), splitLine: { show: false } },
    visualMap: { show: false, min: s.min, max: s.max, dimension: 2, inRange: { color: t.ramp } },
    tooltip: tooltip(t, {
      formatter: (p) => {
        const [x, y, v] = p.value;
        return tipBody(`${fmtIsoDay(days[y].date)}, ${fmtClock(x * 30)}–${fmtClock(x * 30 + 30)}`,
          [["", d.subject.unit, String(Math.round(v * 10) / 10)]]);
      },
    }),
    series: [{
      type: "heatmap", data: cells, progressive: 0,
      itemStyle: { borderColor: t.surface, borderWidth: rowH >= 10 ? 1 : 0 },
      emphasis: { itemStyle: { borderColor: t.ink, borderWidth: 1 } },
    }],
  };
}

// ---- calendar: one cell per day, whole history ----------------------------------------

export function calendarScale(d) {
  if (d.subject.unit === "share") return { min: 0, max: 1, lo: "away all day", hi: "home all day" };
  const vals = d.calendar.map((c) => c[1]).filter((v) => v != null);
  if (!vals.length) return { min: 0, max: 1, lo: "0", hi: "1" };
  const min = Math.floor(Math.min(...vals)), max = Math.max(Math.ceil(Math.max(...vals)), min + 1);
  return { min, max, lo: String(min), hi: String(max) };
}

export function calendarHeat(d, t, node) {
  const s = calendarScale(d);
  const share = d.subject.unit === "share";
  const first = d.calendar[0][0], last = d.calendar[d.calendar.length - 1][0];
  const weeks = Math.ceil((d.calendar.length + 6) / 7) + 1;
  const cell = Math.max(10, Math.min(20, Math.floor((node.clientWidth - 48) / weeks)));
  const inPeriod = (iso) => iso >= d.start && iso <= d.end;
  return {
    ...base(t),
    calendar: {
      range: [first, last], left: 30, top: 22, cellSize: [cell, cell], orient: "horizontal",
      dayLabel: { firstDay: 1, nameMap: ["S", "M", "T", "W", "T", "F", "S"], color: t.muted, fontSize: 10, margin: 6 },
      monthLabel: { color: t.muted, fontSize: 11, margin: 6 },
      yearLabel: { show: false },
      splitLine: { show: false },
      itemStyle: { color: t.track, borderColor: t.surface, borderWidth: 2 },
    },
    visualMap: { show: false, min: s.min, max: s.max, dimension: 1, inRange: { color: t.ramp } },
    tooltip: tooltip(t, {
      formatter: (p) => {
        const [iso, v, extra, cov] = p.value;
        return tipBody(fmtIsoDay(iso), [
          share ? ["", "at home", fmtHours(extra)] : ["", `${d.subject.unit} on average`, String(Math.round(v * 10) / 10)],
          !share && extra != null ? ["", "at the busiest moment", String(Math.round(extra))] : null,
        ].filter(Boolean), cov < 23.5 && iso !== d.end ? `monitored for ${fmtHours(cov)}` : null);
      },
    }),
    series: [{
      type: "heatmap", coordinateSystem: "calendar",
      data: d.calendar.filter((c) => c[1] != null).map((c) => ({
        value: c,
        // Days outside the chosen period stay visible but step back.
        itemStyle: { opacity: inPeriod(c[0]) ? 1 : 0.45, borderRadius: 2 },
      })),
      emphasis: { itemStyle: { borderColor: t.ink, borderWidth: 1 } },
    }],
  };
}

export const calendarHeight = (node, days) => {
  const weeks = Math.ceil((days + 6) / 7) + 1;
  const cell = Math.max(10, Math.min(20, Math.floor((node.clientWidth - 48) / weeks)));
  return 22 + 7 * cell + 8;
};

// ---- long-range line (counts) ---------------------------------------------------------

/** Daily average as a line, inside a band from each day's lowest to its highest. */
export function trendLine(d, t) {
  const days = [...d.days].reverse();  // oldest first
  const noon = (day) => parseDay(day.date).getTime() + 12 * 3600 * 1000;
  const known = days.filter((day) => day.value != null);
  const lo = known.length ? Math.max(0, Math.floor(Math.min(...known.map((x) => x.low)) - 1)) : 0;
  const hi = known.length ? Math.ceil(Math.max(...known.map((x) => x.peak)) + 1) : 1;
  const byX = new Map(days.map((day) => [noon(day), day]));
  const hidden = { symbol: "none", lineStyle: { opacity: 0 }, silent: true, tooltip: { show: false }, connectNulls: false };
  return {
    ...base(t),
    grid: { left: 30, right: 16, top: 10, bottom: 28 },
    xAxis: {
      type: "time", min: parseDay(d.start).getTime(), max: parseDay(d.end).getTime() + 86400000,
      axisLine: { lineStyle: { color: t.axis } }, axisTick: { show: false }, splitLine: { show: false },
      axisLabel: label(t, { hideOverlap: true }),
    },
    yAxis: {
      type: "value", min: lo, max: hi, minInterval: 1, axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: t.grid } }, axisLabel: label(t),
    },
    tooltip: tooltip(t, {
      trigger: "axis",
      axisPointer: { type: "line", snap: true, lineStyle: { color: t.axis, width: 1, type: "solid" } },
      formatter: (ps) => {
        const day = byX.get(ps[0]?.axisValue);
        if (!day) return "";
        if (day.value == null) return tipBody(fmtIsoDay(day.date), [["", "not monitored"]]);
        return tipBody(fmtIsoDay(day.date), [
          ["", `${d.subject.unit} on average`, String(Math.round(day.value * 10) / 10)],
          ["box wash", "lowest to highest", `${Math.round(day.low)}–${Math.round(day.peak)}`],
        ]);
      },
    }),
    series: [
      { type: "line", stack: "range", data: days.map((x) => [noon(x), x.low]), ...hidden },
      {
        type: "line", stack: "range", ...hidden, areaStyle: { color: t.accent, opacity: 0.14 },
        data: days.map((x) => [noon(x), x.low == null ? null : Math.max(x.peak - x.low, 0.1)]),
      },
      {
        type: "line", data: days.map((x) => [noon(x), x.value]), showSymbol: days.length <= 31, symbol: "circle", symbolSize: 6,
        connectNulls: false, z: 3,
        lineStyle: { width: 2, color: t.accent, join: "round", cap: "round" },
        itemStyle: { color: t.accent, borderColor: t.surface, borderWidth: 1.5 },
      },
    ],
  };
}

// ---- device x day matrix ----------------------------------------------------------------

export const matrixHeight = (rows) => 24 + rows * 17 + 8;

export function deviceMatrix(d, t) {
  const { bins, rows, step } = d;
  const cells = rows.flatMap((r, y) => r.cells.map(([x, h]) => [x, y, h]));
  const blind = [];  // runs of days that were not monitored at all
  bins.forEach((b, i) => {
    if (b.covered_h >= 1) return;
    if (blind.length && blind[blind.length - 1][1] === i - 1) blind[blind.length - 1][1] = i;
    else blind.push([i, i]);
  });
  const firstOfMonth = (i) => i === 0 || parseDay(bins[i].date).getMonth() !== parseDay(bins[i - 1].date).getMonth();
  return {
    ...base(t),
    grid: { left: 180, right: 12, top: 22, bottom: 6 },
    xAxis: {
      type: "category", data: bins.map((b) => b.date), position: "top",
      ...quietAxis(t, { axisLabel: label(t, { interval: (i) => firstOfMonth(i), formatter: (iso) => fmtMonth(parseDay(iso)) }) }),
    },
    yAxis: {
      type: "category", data: rows.map((r) => r.name), inverse: true, triggerEvent: true,
      ...quietAxis(t, { axisLabel: label(t, { color: t.ink2, fontSize: 12, width: 164, overflow: "truncate", interval: 0 }) }),
    },
    visualMap: { show: false, min: 0, max: 24, dimension: 2, inRange: { color: t.ramp } },
    tooltip: tooltip(t, {
      formatter: (p) => {
        if (p.componentType !== "series") return "";
        const [x, y, h] = p.value;
        const b = bins[x];
        const head = step > 1 ? `Week of ${fmtIsoDay(b.date)}` : fmtIsoDay(b.date);
        return tipBody(head, [["", step > 1 ? "at home per day" : "at home", fmtHours(h)]], rows[y].name);
      },
    }),
    series: [{
      type: "heatmap", data: cells, progressive: 0,
      itemStyle: { borderColor: t.surface, borderWidth: 1, borderRadius: 1 },
      emphasis: { itemStyle: { borderColor: t.ink, borderWidth: 1 } },
      markArea: {
        silent: true, itemStyle: { color: hatch(t) },
        data: blind.map(([a, b]) => [{ xAxis: bins[a].date }, { xAxis: bins[b].date }]),
      },
    }],
  };
}

// ---- ranked bars ----------------------------------------------------------------------------

export const barsHeight = (n) => Math.max(n, 1) * 26 + 10;

/** `d.items`: [{ name, value, note }]; `d.unit` follows the value. */
export function rankBars(d, t) {
  const max = Math.max(...d.items.map((i) => i.value), 1);
  return {
    ...base(t),
    grid: { left: 180, right: 64, top: 2, bottom: 2 },
    xAxis: { type: "value", show: false, max: max * 1.02 },
    yAxis: {
      type: "category", data: d.items.map((i) => i.name), inverse: true,
      ...quietAxis(t, { axisLabel: label(t, { color: t.ink2, fontSize: 12, width: 164, overflow: "truncate" }) }),
    },
    tooltip: tooltip(t, {
      formatter: (p) => {
        const it = d.items[p.dataIndex];
        return tipBody(it.name, [["", d.unit, String(it.value)]], it.note);
      },
    }),
    series: [{
      type: "bar", data: d.items.map((i) => i.value), barMaxWidth: 14,
      itemStyle: { color: t.accent, borderRadius: [0, 4, 4, 0] },
      emphasis: { itemStyle: { color: t.accent, opacity: 0.85 } },
      label: { show: true, position: "right", color: t.ink2, fontSize: 12, fontFamily: FONT, formatter: (p) => String(p.value) },
    }],
  };
}

export { el, fmtDur };
