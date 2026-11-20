// Data view: one result set as a Table, quick Charts or column Stats. Used by Data Studio results, notebook SQL cells and the pandas /
// polars DataFrames a notebook cell prints. Charts (bar, stacked, horizontal, line, area, scatter / bubble with trend, pie, histogram,
// box plot, heatmap, treemap; several side by side) are built from column pickers with the vendored ECharts; Stats profiles every column
// (type, missing, distinct, min / max / mean / median / std, quartiles, top values, distribution) plus a correlation matrix.
// Everything runs in the browser on the rows already fetched. The spec {view, charts: [...]} is plain JSON the host may keep.
import { html, useState, useEffect, useRef, useMemo } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { Seg, Pop, toast } from "./ui.js";
import { loadEcharts, isDark } from "./diagram.js";

const uid = () => Math.random().toString(36).slice(2, 9);
const PALETTE = ["#d97757", "#5b8def", "#3fb68b", "#e0a35a", "#9b7bd4", "#e56b8f", "#4bb3c4", "#8f9779", "#c9824a", "#6c7ae0", "#58a55c", "#b06ab3"];
const MAX_SERIES = 12, MAX_POINTS = 20000;
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// ---------- values
const NUM_RE = /^[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?$/i;
const DATE_RE = /^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
export const toNum = (v) => (typeof v === "number" ? (isFinite(v) ? v : NaN) : typeof v === "string" && NUM_RE.test(v.trim()) ? parseFloat(v) : NaN);
const toTime = (v) => {
  if (typeof v !== "string" || !DATE_RE.test(v.trim())) return NaN;
  const s = v.trim().replace(" ", "T");
  return Date.parse(s.length === 10 ? s + "T00:00:00" : s);        // a bare date is local midnight, like a time without a zone
};
const isBool = (v) => typeof v === "boolean" || v === "true" || v === "false" || v === "True" || v === "False";
const pad = (n) => String(n).padStart(2, "0");
const isoDay = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
const fmtTime = (t) => { const d = new Date(t); return isoDay(d) + (d.getHours() || d.getMinutes() || d.getSeconds() ? " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) : ""); };

export function fmt(v) {                                        // compact, for axes and labels
  if (v == null || typeof v !== "number" || !isFinite(v)) return v == null ? "" : String(v);
  const a = Math.abs(v), cut = (x, s) => (Math.abs(x) >= 100 ? x.toFixed(0) : x.toFixed(1)).replace(/\.0$/, "") + s;
  if (a >= 1e12) return cut(v / 1e12, "T"); if (a >= 1e9) return cut(v / 1e9, "B"); if (a >= 1e6) return cut(v / 1e6, "M"); if (a >= 1e4) return cut(v / 1e3, "k");
  if (Number.isInteger(v)) return v.toLocaleString();
  return a >= 100 ? v.toFixed(1) : a >= 1 ? v.toFixed(2).replace(/0$/, "") : Number(v.toPrecision(3)).toString();
}
const full = (v) => (v == null || !isFinite(v) ? "" : Number.isInteger(v) ? v.toLocaleString() : Number(v.toPrecision(7)).toLocaleString(undefined, { maximumFractionDigits: 6 }));

// ---------- column types: num, date, bool or text (by the values, not the database type: Decimal and dates arrive as strings)
export function profile(columns, rows) {
  return columns.map((name, i) => {
    let seen = 0, nums = 0, dates = 0, bools = 0;
    const ds = new Set();
    for (let r = 0; r < rows.length && seen < 3000; r++) {
      const v = rows[r][i];
      if (v == null || v === "") continue;
      seen++;
      if (ds.size < 1000) ds.add(String(v));
      if (isBool(v)) bools++;
      else if (!isNaN(toNum(v))) nums++;
      else if (!isNaN(toTime(v))) dates++;
    }
    const kind = !seen ? "text" : bools === seen ? "bool" : nums >= seen * 0.95 ? "num" : dates >= seen * 0.95 ? "date" : "text";
    return { name: String(name), i, kind, card: ds.size, idlike: kind === "num" && /(^|_|\b)(id|key|pk|code|zip)$/i.test(String(name)) };
  });
}
const col = (prof, n) => (n == null || n === "" ? null : prof.find((c) => c.name === n) || null);

// ---------- chart types
export const TYPES = {
  bar: { label: "Bar", fields: ["x", "y", "agg", "group", "bucket", "sort", "top"] },
  stack: { label: "Stacked bar", fields: ["x", "y", "agg", "group", "bucket", "sort", "top"] },
  hbar: { label: "Horizontal bar", fields: ["x", "y", "agg", "group", "bucket", "sort", "top"] },
  line: { label: "Line", fields: ["x", "y", "agg", "group", "bucket", "sort", "top"] },
  area: { label: "Area", fields: ["x", "y", "agg", "group", "bucket", "sort", "top"] },
  scatter: { label: "Scatter / bubble", fields: ["x", "y", "group", "size", "trend"] },
  pie: { label: "Pie / donut", fields: ["x", "y1", "agg", "bucket", "top"] },
  hist: { label: "Histogram", fields: ["y", "group", "bins"] },
  box: { label: "Box plot", fields: ["y", "group"] },
  heatmap: { label: "Heatmap", fields: ["x", "group", "y1", "agg", "bucket"] },
  treemap: { label: "Treemap", fields: ["x", "group", "y1", "agg"] },
};
const AGGS = [["sum", "Sum"], ["avg", "Average"], ["count", "Count"], ["min", "Min"], ["max", "Max"], ["first", "As is"]];
const BUCKETS = [["auto", "Auto"], ["none", "Exact"], ["hour", "Hour"], ["day", "Day"], ["week", "Week"], ["month", "Month"], ["quarter", "Quarter"], ["year", "Year"]];
const XY = new Set(["bar", "stack", "hbar", "line", "area"]);

function bucketKey(t, b) {
  const d = new Date(t), y = d.getFullYear(), m = d.getMonth();
  if (b === "year") return String(y);
  if (b === "quarter") return y + "-Q" + (Math.floor(m / 3) + 1);
  if (b === "month") return y + "-" + pad(m + 1);
  if (b === "week") return isoDay(new Date(y, m, d.getDate() - ((d.getDay() + 6) % 7)));
  if (b === "hour") return isoDay(d) + " " + pad(d.getHours()) + ":00";
  return isoDay(d);
}
function autoBucket(rows, i) {
  let lo = Infinity, hi = -Infinity;
  const ds = new Set();
  for (const r of rows) { const t = toTime(r[i]); if (isNaN(t)) continue; if (t < lo) lo = t; if (t > hi) hi = t; if (ds.size <= 200) ds.add(t); }
  if (ds.size <= 200) return "none";
  const days = (hi - lo) / 864e5;
  return days > 1100 ? "month" : days > 120 ? "week" : days > 3 ? "day" : "hour";
}

// the best few charts for these columns, most telling first
export function suggest(prof, n = 4) {
  const nums = prof.filter((c) => c.kind === "num"), vals = nums.filter((c) => !c.idlike).length ? nums.filter((c) => !c.idlike) : nums;
  const dates = prof.filter((c) => c.kind === "date");
  const cats = prof.filter((c) => (c.kind === "text" || c.kind === "bool") && c.card > 1).sort((a, b) => (b.card <= 60) - (a.card <= 60));
  const small = cats.filter((c) => c.card <= 12), out = [], add = (c) => out.push({ id: uid(), ...c });
  if (dates.length) add(vals.length ? { type: "line", x: dates[0].name, y: vals.slice(0, 2).map((c) => c.name), agg: "sum", bucket: "auto" } : { type: "bar", x: dates[0].name, y: [], agg: "count", bucket: "auto" });
  if (cats.length) add(vals.length ? { type: "bar", x: cats[0].name, y: [vals[0].name], agg: "sum", sort: "desc", top: 30 } : { type: "bar", x: cats[0].name, y: [], agg: "count", sort: "desc", top: 30 });
  if (vals.length >= 2) add({ type: "scatter", x: vals[0].name, y: [vals[1].name], group: small[0] ? small[0].name : "" });
  if (vals.length) add({ type: "hist", y: [vals[0].name] });
  if (small.length) add({ type: "pie", x: small[0].name, y: vals[0] ? [vals[0].name] : [], agg: vals[0] ? "sum" : "count", top: 10 });
  if (vals.length && small.length) add({ type: "box", y: [vals[0].name], group: small[0].name });
  if (cats.length >= 2) add({ type: "heatmap", x: cats[0].name, group: cats[1].name, y: vals[0] ? [vals[0].name] : [], agg: vals[0] ? "sum" : "count" });
  if (!out.length && prof.length) add({ type: "bar", x: prof[0].name, y: [], agg: "count", sort: "desc", top: 30 });
  return out.slice(0, n);
}
// a chart that shows one column on its own (from Stats)
const forColumn = (c) => ({ id: uid(), ...(c.kind === "num" ? { type: "hist", y: [c.name] } : c.kind === "date" ? { type: "bar", x: c.name, y: [], agg: "count", bucket: "auto" } : { type: "bar", x: c.name, y: [], agg: "count", sort: "desc", top: 30 }) });

// ---------- aggregation
const acc0 = () => ({ n: 0, s: 0, mn: Infinity, mx: -Infinity, f: null });
const fin = (a, agg) => (!a ? null : agg === "count" ? a.n : !a.n ? null : agg === "sum" ? a.s : agg === "avg" ? a.s / a.n : agg === "min" ? a.mn : agg === "max" ? a.mx : a.f);
const cmp = (a, b) => { const na = typeof a === "number" && !isNaN(a), nb = typeof b === "number" && !isNaN(b); if (na && nb) return a - b; if (na !== nb) return na ? -1 : 1; return String(a).localeCompare(String(b), undefined, { numeric: true }); };

function keyer(X, rows, bucketPref) {
  let bucket = X.kind === "date" ? bucketPref || "auto" : "none";
  if (bucket === "auto") bucket = autoBucket(rows, X.i);
  const key = (v) => {
    if (v == null || v === "") return "(null)";
    if (X.kind === "date") { const t = toTime(v); return isNaN(t) ? String(v) : bucket === "none" ? String(v) : bucketKey(t, bucket); }
    if (X.kind === "num") { const n = toNum(v); return isNaN(n) ? String(v) : String(n); }
    return String(v);
  };
  const sortv = (v, k) => (k === "(null)" ? NaN : X.kind === "date" ? (bucket === "none" ? toTime(v) : k) : X.kind === "num" ? toNum(v) : k);
  return { key, sortv, bucket };
}
// top groups of a split column (by rows), the rest folded into "Other"
function groupKeys(rows, G, limit) {
  const cnt = new Map();
  for (const r of rows) { const g = r[G.i] == null ? "(null)" : String(r[G.i]); cnt.set(g, (cnt.get(g) || 0) + 1); }
  const top = [...cnt.entries()].sort((a, b) => b[1] - a[1]).slice(0, limit).map((e) => e[0]);
  const keep = new Set(top), other = cnt.size > top.length;
  return { list: other ? [...top.sort(cmp), "Other"] : top.sort(cmp), of: (v) => { const g = v == null ? "(null)" : String(v); return keep.has(g) ? g : "Other"; }, hidden: cnt.size - top.length };
}

function cube(c, prof, rows) {
  const X = col(prof, c.x), Ys = (c.y || []).map((n) => col(prof, n)).filter((y) => y && y.kind === "num"), G = col(prof, c.group);
  if (!X) return { error: "Pick a column for the " + (c.type === "hbar" ? "categories" : "X axis") + "." };
  const agg = Ys.length ? c.agg || "sum" : "count", kx = keyer(X, rows, c.bucket), gk = G && G !== X ? groupKeys(rows, G, MAX_SERIES) : null;
  const cells = new Map(), xs = new Map(), nY = Math.max(1, Ys.length);
  for (const r of rows) {
    const v = r[X.i], k = kx.key(v), g = gk ? gk.of(r[G.i]) : "";
    if (!xs.has(k)) xs.set(k, kx.sortv(v, k));
    let gm = cells.get(k); if (!gm) cells.set(k, (gm = new Map()));
    let a = gm.get(g); if (!a) gm.set(g, (a = Array.from({ length: nY }, acc0)));
    if (!Ys.length) { a[0].n++; continue; }
    for (let j = 0; j < Ys.length; j++) {
      const y = toNum(r[Ys[j].i]); if (isNaN(y)) continue;
      const q = a[j]; q.n++; q.s += y; if (y < q.mn) q.mn = y; if (y > q.mx) q.mx = y; if (q.f === null) q.f = y;
    }
  }
  const series = [];
  for (const g of gk ? gk.list : [""]) {
    for (let j = 0; j < nY; j++) {
      const vals = new Map();
      for (const [k, gm] of cells) { const a = gm.get(g); if (a) vals.set(k, fin(a[j], agg)); }
      const yname = Ys.length ? Ys[j].name : "count";
      series.push({ name: gk ? (Ys.length > 1 ? yname + " · " + g : g) : agg === "count" || agg === "sum" || agg === "first" ? yname : agg + " " + yname, vals });
    }
  }
  return { X, Ys, G: gk ? G : null, agg, xs, series, bucket: kx.bucket, hidden: gk ? gk.hidden : 0 };
}
function orderKeys(cb, sort, top, defTop) {
  let keys = [...cb.xs.keys()];
  if (sort === "desc" || sort === "asc") {
    const tot = new Map(keys.map((k) => [k, cb.series.reduce((s, x) => s + (Number(x.vals.get(k)) || 0), 0)]));
    keys.sort((a, b) => (sort === "desc" ? tot.get(b) - tot.get(a) : tot.get(a) - tot.get(b)));
  } else keys.sort((a, b) => cmp(cb.xs.get(a), cb.xs.get(b)));
  const n = keys.length, lim = top === 0 || top === "0" ? 0 : +top || defTop || 0;
  return { keys: lim && n > lim ? keys.slice(0, lim) : keys, cut: lim && n > lim ? n : 0 };
}

// ---------- ECharts options
function frame(nSeries, extra) {
  return { backgroundColor: "transparent", color: PALETTE, animationDuration: 350, textStyle: { fontFamily: "ui-sans-serif, -apple-system, Segoe UI, Inter, sans-serif" },
    legend: nSeries > 1 ? { type: "scroll", top: 0, left: "center", itemWidth: 14, itemHeight: 9, textStyle: { fontSize: 11 } } : { show: false },
    toolbox: { right: 2, top: 0, itemSize: 12, itemGap: 8, feature: { restore: { title: "Reset zoom" }, saveAsImage: { title: "Save as image", pixelRatio: 2 } }, tooltip: { show: true } },
    grid: { left: 10, right: 18, top: nSeries > 1 ? 36 : 26, bottom: 10, containLabel: true }, tooltip: { trigger: "axis", confine: true, valueFormatter: (v) => (typeof v === "number" ? full(v) : v) }, ...extra };
}
const catAxis = (keys, extra) => ({ type: "category", data: keys, axisTick: { alignWithLabel: true }, axisLabel: { hideOverlap: true, fontSize: 11, width: 130, overflow: "truncate", rotate: keys.length > 14 && keys.some((k) => String(k).length > 6) ? 35 : 0 }, ...extra });
const valAxis = (extra) => ({ type: "value", axisLabel: { formatter: (v) => fmt(v), fontSize: 11 }, splitLine: { lineStyle: { opacity: 0.35 } }, ...extra });
const WHEEL = { zoomOnMouseWheel: "ctrl", moveOnMouseWheel: false };                  // the page keeps the wheel; Ctrl + wheel zooms
const zoom = (horizontal) => (horizontal ? [{ type: "inside", yAxisIndex: 0, ...WHEEL }, { type: "slider", yAxisIndex: 0, width: 14, right: 4 }] : [{ type: "inside", ...WHEEL }, { type: "slider", height: 14, bottom: 4 }]);

function xyOption(c, prof, rows) {
  const cb = cube(c, prof, rows); if (cb.error) return cb;
  const text = cb.X.kind === "text" || cb.X.kind === "bool", bars = c.type !== "line" && c.type !== "area";
  const { keys, cut } = orderKeys(cb, c.sort || (text && bars ? "desc" : "x"), c.top, text ? 50 : 0);
  const time = !bars && cb.X.kind === "date" && cb.bucket === "none", h = c.type === "hbar", many = keys.length > (h ? 40 : 60);
  const series = cb.series.map((s) => ({ name: s.name, type: bars ? "bar" : "line", data: time ? keys.map((k) => [cb.xs.get(k), s.vals.get(k) ?? null]) : keys.map((k) => s.vals.get(k) ?? null),
    stack: c.type === "stack" ? "total" : undefined, areaStyle: c.type === "area" ? { opacity: cb.series.length > 1 ? 0.18 : 0.28 } : undefined, connectNulls: true,
    showSymbol: keys.length <= 50, symbolSize: 5, large: keys.length > 3000, barMaxWidth: 46, emphasis: { focus: cb.series.length > 1 ? "series" : "none" },
    label: bars && cb.series.length === 1 && keys.length <= 14 ? { show: true, position: h ? "right" : "top", fontSize: 10, formatter: (p) => fmt(p.value) } : undefined }));
  const cat = time ? { type: "time", axisLabel: { hideOverlap: true, fontSize: 11 } } : catAxis(keys, h ? { inverse: true, axisLabel: { fontSize: 11, width: 140, overflow: "truncate" } } : {});
  const opt = frame(series.length, { xAxis: h ? valAxis() : cat, yAxis: h ? cat : valAxis({ scale: !bars }), series });
  if (many || time) { opt.dataZoom = zoom(h); if (h) opt.grid.right = 26; else opt.grid.bottom = 26; }
  const notes = [];
  if (cut) notes.push("top " + keys.length + " of " + cut + " " + cb.X.name + " values");
  if (cb.hidden) notes.push(cb.hidden + " smaller groups folded into Other");
  if (cb.X.kind === "date" && cb.bucket !== "none") notes.push("by " + cb.bucket);
  return { option: opt, note: notes.join(" · ") };
}

function linreg(pts) {
  let n = 0, sx = 0, sy = 0, sxx = 0, sxy = 0, syy = 0;
  for (const [x, y] of pts) { n++; sx += x; sy += y; sxx += x * x; sxy += x * y; syy += y * y; }
  const d = n * sxx - sx * sx; if (n < 3 || !d) return null;
  const b = (n * sxy - sx * sy) / d, a = (sy - b * sx) / n, r = (n * sxy - sx * sy) / Math.sqrt(d * (n * syy - sy * sy) || 1);
  return { a, b, r2: r * r };
}
function scatterOption(c, prof, rows) {
  const X = col(prof, c.x), Ys = (c.y || []).map((n) => col(prof, n)).filter((y) => y && y.kind === "num"), G = col(prof, c.group), S = col(prof, c.size);
  if (!X || (X.kind !== "num" && X.kind !== "date")) return { error: "Scatter needs a number or date column on the X axis." };
  if (!Ys.length) return { error: "Pick at least one number column for Y." };
  const gk = G ? groupKeys(rows, G, MAX_SERIES) : null, step = Math.max(1, Math.ceil(rows.length / MAX_POINTS)), xv = X.kind === "date" ? toTime : toNum;
  const buckets = new Map();
  let smin = Infinity, smax = -Infinity;
  for (let r = 0; r < rows.length; r += step) {
    const row = rows[r], x = xv(row[X.i]); if (isNaN(x)) continue;
    const s = S ? toNum(row[S.i]) : NaN; if (!isNaN(s)) { if (s < smin) smin = s; if (s > smax) smax = s; }
    for (const Y of Ys) {
      const y = toNum(row[Y.i]); if (isNaN(y)) continue;
      const name = gk ? (Ys.length > 1 ? Y.name + " · " + gk.of(row[G.i]) : gk.of(row[G.i])) : Y.name;
      let b = buckets.get(name); if (!b) buckets.set(name, (b = []));
      b.push(S ? [x, y, s] : [x, y]);
    }
  }
  const size = S && smax > smin ? (v) => 5 + 25 * Math.sqrt(Math.max(0, (v[2] - smin) / (smax - smin))) : S ? 10 : rows.length > 5000 ? 4 : 7;
  const names = gk ? [...buckets.keys()].sort((a, b) => (a === "Other") - (b === "Other") || cmp(a, b)) : [...buckets.keys()];
  const series = [], notes = [];
  names.forEach((name, i) => {
    const data = buckets.get(name);
    series.push({ name, type: "scatter", data, symbolSize: size, large: data.length > 4000, itemStyle: { opacity: data.length > 2000 ? 0.55 : 0.8 }, emphasis: { focus: "series" } });
    if (c.trend) {
      const fit = linreg(data); if (!fit) return;
      let lo = Infinity, hi = -Infinity; for (const p of data) { if (p[0] < lo) lo = p[0]; if (p[0] > hi) hi = p[0]; }
      series.push({ name: name + " trend", type: "line", data: [[lo, fit.a + fit.b * lo], [hi, fit.a + fit.b * hi]], showSymbol: false, silent: true, tooltip: { show: false },
        lineStyle: { type: "dashed", width: 1.5, color: PALETTE[i % PALETTE.length] }, color: PALETTE[i % PALETTE.length] });
      notes.push(name + ": y = " + fmt(fit.b) + "·x " + (fit.a < 0 ? "− " : "+ ") + fmt(Math.abs(fit.a)) + ", R² " + fit.r2.toFixed(3));
    }
  });
  const xl = X.kind === "date" ? fmtTime : full;
  const opt = frame(names.length, { xAxis: X.kind === "date" ? { type: "time", axisLabel: { hideOverlap: true, fontSize: 11 } } : valAxis({ scale: true, name: X.name, nameLocation: "middle", nameGap: 26, nameTextStyle: { fontSize: 11 } }),
    yAxis: valAxis({ scale: true, name: Ys.length === 1 ? Ys[0].name : "", nameTextStyle: { fontSize: 11, align: "left" } }), series,
    tooltip: { trigger: "item", confine: true, formatter: (p) => esc(p.seriesName) + "<br>" + esc(X.name) + ": <b>" + esc(xl(p.value[0])) + "</b><br>y: <b>" + esc(full(p.value[1])) + "</b>" + (S && p.value.length > 2 ? "<br>" + esc(S.name) + ": <b>" + esc(full(p.value[2])) + "</b>" : "") } });
  if (opt.legend.show !== false) opt.legend.data = names;            // trend lines stay out of the legend
  opt.grid.bottom = 22;
  if (step > 1) notes.unshift("every " + step + "th row drawn (" + MAX_POINTS.toLocaleString() + " point cap)");
  return { option: opt, note: notes.join(" · ") };
}

function pieOption(c, prof, rows) {
  const cb = cube({ ...c, y: (c.y || []).slice(0, 1), group: "" }, prof, rows); if (cb.error) return cb;
  const s = cb.series[0], lim = +c.top || 10;
  const all = [...s.vals.entries()].filter((e) => e[1] != null && e[1] > 0).sort((a, b) => b[1] - a[1]);
  const data = all.slice(0, lim).map(([name, value]) => ({ name, value }));
  const rest = all.slice(lim).reduce((t, e) => t + e[1], 0);
  if (rest > 0) data.push({ name: "Other (" + (all.length - lim) + ")", value: rest, itemStyle: { color: "#9a9aa0" } });
  if (!data.length) return { error: "Nothing positive to show for this column." };
  return { option: frame(2, { tooltip: { trigger: "item", confine: true, valueFormatter: full }, legend: { type: "scroll", orient: "vertical", left: 0, top: "middle", textStyle: { fontSize: 11 } },
    series: [{ type: "pie", radius: ["38%", "70%"], center: ["60%", "52%"], data, avoidLabelOverlap: true, itemStyle: { borderColor: isDark() ? "#1c1c1e" : "#fff", borderWidth: 2, borderRadius: 4 },
      label: { formatter: "{b}\n{d}%", fontSize: 11 }, labelLine: { length: 8, length2: 8 } }] }), note: all.length > lim ? all.length + " slices, " + lim + " largest shown" : "" };
}

function numbersBy(c, prof, rows) {
  const Ys = (c.y || []).map((n) => col(prof, n)).filter((y) => y && y.kind === "num"), G = col(prof, c.group);
  if (!Ys.length) return { error: "Pick a number column." };
  const sets = new Map();
  if (G) {
    const gk = groupKeys(rows, G, c.type === "box" ? 30 : MAX_SERIES);
    for (const g of gk.list) sets.set(g, []);
    for (const r of rows) { const y = toNum(r[Ys[0].i]); if (!isNaN(y)) sets.get(gk.of(r[G.i])).push(y); }
    for (const [k, v] of sets) if (!v.length) sets.delete(k);
    return { sets, label: Ys[0].name, hidden: gk.hidden };
  }
  for (const Y of Ys) { const v = []; for (const r of rows) { const y = toNum(r[Y.i]); if (!isNaN(y)) v.push(y); } sets.set(Y.name, v); }
  return { sets, label: Ys.map((y) => y.name).join(", "), hidden: 0 };
}
function histOption(c, prof, rows) {
  const nb = numbersBy(c, prof, rows); if (nb.error) return nb;
  let lo = Infinity, hi = -Infinity, n = 0;
  for (const v of nb.sets.values()) for (const x of v) { if (x < lo) lo = x; if (x > hi) hi = x; n++; }
  if (!n) return { error: "No numbers in this column." };
  const bins = +c.bins || Math.max(8, Math.min(50, Math.ceil(Math.log2(n) + 1))), w = hi > lo ? (hi - lo) / bins : 1;
  const labels = Array.from({ length: bins }, (_, i) => fmt(lo + i * w) + " – " + fmt(lo + (i + 1) * w));
  const series = [...nb.sets.entries()].map(([name, v]) => {
    const cnt = new Array(bins).fill(0);
    for (const x of v) cnt[Math.min(bins - 1, Math.floor((x - lo) / w))]++;
    return { name, type: "bar", data: cnt, barCategoryGap: "3%", barGap: nb.sets.size > 1 ? "-100%" : undefined, itemStyle: { opacity: nb.sets.size > 1 ? 0.6 : 0.9 } };
  });
  const one = nb.sets.size === 1 ? [...nb.sets.values()][0].slice().sort((a, b) => a - b) : null;
  const note = one ? "n " + one.length.toLocaleString() + " · mean " + fmt(one.reduce((s, x) => s + x, 0) / one.length) + " · median " + fmt(quant(one, 0.5)) + " · bin width " + fmt(w) : (nb.hidden ? nb.hidden + " smaller groups in Other" : "");
  return { option: frame(series.length, { xAxis: catAxis(labels, { name: nb.label, nameLocation: "middle", nameGap: 28, nameTextStyle: { fontSize: 11 }, axisLabel: { hideOverlap: true, fontSize: 10 } }), yAxis: valAxis({ name: "rows", nameTextStyle: { fontSize: 11 } }), series }), note };
}
function quant(sorted, p) { if (!sorted.length) return NaN; const pos = (sorted.length - 1) * p, lo = Math.floor(pos), hi = Math.ceil(pos); return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo); }
function boxOption(c, prof, rows) {
  const nb = numbersBy(c, prof, rows); if (nb.error) return nb;
  const cats = [], boxes = [], outl = [];
  [...nb.sets.entries()].forEach(([name, v]) => {
    const s = v.slice().sort((a, b) => a - b); if (!s.length) return;
    const q1 = quant(s, 0.25), q2 = quant(s, 0.5), q3 = quant(s, 0.75), iq = q3 - q1, lo = q1 - 1.5 * iq, hi = q3 + 1.5 * iq;
    const wl = s.find((x) => x >= lo), wh = [...s].reverse().find((x) => x <= hi), i = cats.length;
    cats.push(name); boxes.push([wl, q1, q2, q3, wh]);
    for (const x of s) if ((x < lo || x > hi) && outl.length < 4000) outl.push([i, x]);
  });
  if (!cats.length) return { error: "No numbers in this column." };
  return { option: frame(1, { tooltip: { trigger: "item", confine: true }, xAxis: catAxis(cats), yAxis: valAxis({ scale: true, name: nb.label, nameTextStyle: { fontSize: 11, align: "left" } }),
    series: [{ name: nb.label, type: "boxplot", data: boxes, itemStyle: { color: "rgba(217,119,87,0.18)", borderColor: PALETTE[0] } },
    { name: "outliers", type: "scatter", data: outl, symbolSize: 5, itemStyle: { color: PALETTE[1], opacity: 0.6 } }] }), note: outl.length ? outl.length + " outliers (beyond 1.5 × IQR)" : "" };
}
function heatOption(c, prof, rows) {
  const G = col(prof, c.group); if (!G) return { error: "Pick a column for the rows." };
  const cb = cube({ ...c, y: (c.y || []).slice(0, 1), group: "" }, prof, rows); if (cb.error) return cb;
  const X = cb.X, kx = keyer(X, rows, c.bucket), gk = groupKeys(rows, G, 40), agg = cb.agg, Y = cb.Ys[0];
  const xk = orderKeys(cb, "desc", 0, 0).keys.slice(0, 50).sort((a, b) => cmp(cb.xs.get(a), cb.xs.get(b))), xi = new Map(xk.map((k, i) => [k, i]));
  const ys = gk.list, yi = new Map(ys.map((k, i) => [k, i])), cells = new Map();
  for (const r of rows) {
    const a = xi.get(kx.key(r[X.i])); if (a === undefined) continue;
    const b = yi.get(gk.of(r[G.i])), k = a * 1000 + b;
    let q = cells.get(k); if (!q) cells.set(k, (q = acc0()));
    if (!Y) { q.n++; continue; }
    const y = toNum(r[Y.i]); if (isNaN(y)) continue;
    q.n++; q.s += y; if (y < q.mn) q.mn = y; if (y > q.mx) q.mx = y; if (q.f === null) q.f = y;
  }
  const data = []; let mn = Infinity, mx = -Infinity;
  for (const [k, q] of cells) { const v = fin(q, agg); if (v == null) continue; data.push([Math.floor(k / 1000), k % 1000, v]); if (v < mn) mn = v; if (v > mx) mx = v; }
  if (!data.length) return { error: "No values for these columns." };
  const opt = frame(1, { tooltip: { trigger: "item", confine: true, formatter: (p) => esc(xk[p.value[0]]) + " · " + esc(ys[p.value[1]]) + "<br><b>" + esc(full(p.value[2])) + "</b>" },
    xAxis: catAxis(xk, { splitArea: { show: true } }), yAxis: { type: "category", data: ys, splitArea: { show: true }, axisLabel: { fontSize: 11, width: 120, overflow: "truncate" } },
    visualMap: { min: mn, max: mx === mn ? mn + 1 : mx, calculable: true, orient: "horizontal", left: "center", bottom: 0, itemHeight: 140, itemWidth: 10, textStyle: { fontSize: 10 }, formatter: (v) => fmt(v), inRange: { color: isDark() ? ["#2b2523", "#d97757", "#ffd2b8"] : ["#fdf3ee", "#e9a184", "#b9573a"] } },
    series: [{ type: "heatmap", data, label: { show: xk.length * ys.length <= 150, fontSize: 10, formatter: (p) => fmt(p.value[2]) }, emphasis: { itemStyle: { borderColor: "#000", borderWidth: 1 } } }] });
  opt.grid.bottom = 44;
  return { option: opt, note: (cb.xs.size > 50 ? "50 of " + cb.xs.size + " " + X.name + " values · " : "") + (gk.hidden ? gk.hidden + " smaller " + G.name + " values in Other" : "") };
}
function treeOption(c, prof, rows) {
  const cb = cube({ ...c, y: (c.y || []).slice(0, 1) }, prof, rows); if (cb.error) return cb;
  const kids = (k) => cb.series.map((s) => ({ name: s.name, value: s.vals.get(k) })).filter((d) => d.value > 0);
  const data = orderKeys(cb, "desc", 0, 0).keys.slice(0, 300).map((k) => cb.G ? { name: k, children: kids(k), value: kids(k).reduce((t, d) => t + d.value, 0) } : { name: k, value: cb.series[0].vals.get(k) }).filter((d) => d.value > 0);
  if (!data.length) return { error: "Nothing positive to show." };
  return { option: frame(1, { tooltip: { trigger: "item", confine: true, valueFormatter: full },
    series: [{ type: "treemap", data, roam: false, nodeClick: false, breadcrumb: { show: false }, top: 4, left: 4, right: 4, bottom: 4, label: { show: true, fontSize: 11, formatter: (p) => p.name + "\n" + fmt(p.value) },
      upperLabel: cb.G ? { show: true, height: 18, fontSize: 11 } : undefined, levels: cb.G ? [{ itemStyle: { gapWidth: 3, borderWidth: 0 } }, { colorSaturation: [0.3, 0.6], itemStyle: { gapWidth: 1, borderColorSaturation: 0.7 } }] : [{ itemStyle: { gapWidth: 2 } }] }] }), note: cb.xs.size > 300 ? "300 largest of " + cb.xs.size : "" };
}
export function chartOption(c, prof, rows) {
  try {
    if (!rows.length) return { error: "No rows to draw." };
    if (XY.has(c.type)) return xyOption(c, prof, rows);
    return ({ scatter: scatterOption, pie: pieOption, hist: histOption, box: boxOption, heatmap: heatOption, treemap: treeOption })[c.type](c, prof, rows);
  } catch (e) { return { error: "Could not draw: " + e.message }; }
}

// ---------- ECharts host
function EChart({ option, apiRef }) {
  const host = useRef(), inst = useRef(null), opt = useRef(option), [err, setErr] = useState("");
  opt.current = option;
  useEffect(() => {
    let dead = false, ro = null;
    loadEcharts().then((E) => {
      if (dead || !host.current) return;
      const ch = E.init(host.current, isDark() ? "dark" : null, { renderer: "canvas" });
      inst.current = ch; if (apiRef) apiRef.current = ch;
      ch.setOption(opt.current, true);
      ro = new ResizeObserver(() => ch.resize()); ro.observe(host.current);
    }).catch((e) => !dead && setErr(e.message));
    return () => { dead = true; if (ro) ro.disconnect(); if (inst.current) inst.current.dispose(); inst.current = null; if (apiRef) apiRef.current = null; };
  }, []);
  useEffect(() => { if (inst.current) try { inst.current.setOption(option, true); setErr(""); } catch (e) { setErr(e.message); } }, [option]);
  return html`<div class="dv-host" ref=${host}>${err && html`<div class="dv-msg">${err}</div>`}</div>`;
}

// ---------- one chart card: pickers + chart
function Field({ label, children }) { return html`<label class="dv-f"><span>${label}</span>${children}</label>`; }
function ChartCard({ c, prof, rows, set, remove, dup }) {
  const out = useMemo(() => chartOption(c, prof, rows), [c, prof, rows]), api = useRef(null);
  const f = new Set(TYPES[c.type].fields), X = col(prof, c.x), nums = prof.filter((p) => p.kind === "num");
  const opt = (list, cur, none) => html`${none != null && html`<option value="">${none}</option>`}${list.map((p) => html`<option value=${p.name} selected=${p.name === cur}>${p.name}</option>`)}`;
  const ys = (c.y || []).filter((n) => nums.some((p) => p.name === n));
  const png = () => { const ch = api.current; if (!ch) return; const a = document.createElement("a"); a.href = ch.getDataURL({ pixelRatio: 2, backgroundColor: isDark() ? "#1c1c1e" : "#fff" }); a.download = (c.title || c.type) + ".png"; a.click(); };
  const copy = () => { if (!out.option) return; try { navigator.clipboard.writeText("```echarts\n" + JSON.stringify(out.option) + "\n```"); toast("Chart copied: paste it into a chat or a Markdown cell"); } catch (e) { /* no clipboard */ } };
  const lblX = c.type === "hbar" || c.type === "pie" || c.type === "treemap" ? "Category" : c.type === "heatmap" ? "Columns" : "X axis";
  const lblG = c.type === "heatmap" ? "Rows" : c.type === "treemap" ? "Inside" : c.type === "box" || c.type === "hist" ? "Per" : c.type === "scatter" ? "Color" : "Split by";
  return html`<div class=${"dv-card" + (c.wide ? " wide" : "")}>
    <div class="dv-ch">
      <select class="dv-type" value=${c.type} onChange=${(e) => set({ type: e.target.value })} aria-label="Chart type">${Object.entries(TYPES).map(([k, t]) => html`<option value=${k}>${t.label}</option>`)}</select>
      <span class="grow"></span>
      <button class="ibtn sm" title=${c.wide ? "Half width" : "Full width"} aria-label="Toggle width" onClick=${() => set({ wide: !c.wide })}>${Ic(c.wide ? "shrink" : "expand", 13)}</button>
      <button class="ibtn sm" title="Download PNG" aria-label="Download PNG" disabled=${!out.option} onClick=${png}>${Ic("download", 13)}</button>
      <button class="ibtn sm" title="Copy as an echarts block (chat, Markdown)" aria-label="Copy chart" disabled=${!out.option} onClick=${copy}>${Ic("copy", 13)}</button>
      <button class="ibtn sm" title="Duplicate" aria-label="Duplicate" onClick=${dup}>${Ic("plus", 13)}</button>
      <button class="ibtn sm" title="Remove" aria-label="Remove chart" onClick=${remove}>${Ic("x", 13)}</button></div>
    <div class="dv-fields">
      ${f.has("x") && html`<${Field} label=${lblX}><select onChange=${(e) => set({ x: e.target.value })}>${opt(c.type === "scatter" ? prof.filter((p) => p.kind === "num" || p.kind === "date") : prof, c.x, "…")}</select><//>`}
      ${f.has("y") && html`<${Field} label=${c.type === "hist" || c.type === "box" ? "Values" : "Y values"}><span class="dv-ys">${ys.map((n) => html`<span class="dv-chip" key=${n}>${n}<button aria-label=${"Remove " + n} onClick=${() => set({ y: ys.filter((x) => x !== n) })}>×</button></span>`)}
        <select class="dv-add" value="" onChange=${(e) => { if (e.target.value) set({ y: [...ys, e.target.value] }); e.target.value = ""; }} aria-label="Add a value column"><option value="">${ys.length ? "+" : XY.has(c.type) ? "count rows · + column" : "+ column"}</option>${nums.filter((p) => !ys.includes(p.name)).map((p) => html`<option value=${p.name}>${p.name}</option>`)}</select></span><//>`}
      ${f.has("y1") && html`<${Field} label="Value"><select onChange=${(e) => set({ y: e.target.value ? [e.target.value] : [] })}>${opt(nums, ys[0], "count rows")}</select><//>`}
      ${f.has("agg") && ys.length > 0 && html`<${Field} label="Aggregate"><select value=${c.agg || "sum"} onChange=${(e) => set({ agg: e.target.value })}>${AGGS.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select><//>`}
      ${f.has("bucket") && X && X.kind === "date" && html`<${Field} label="Group dates"><select value=${c.bucket || "auto"} onChange=${(e) => set({ bucket: e.target.value })}>${BUCKETS.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select><//>`}
      ${f.has("group") && html`<${Field} label=${lblG}><select onChange=${(e) => set({ group: e.target.value })}>${opt(prof.filter((p) => p.name !== c.x && p.kind !== "num" || (c.type === "heatmap" || c.type === "treemap") && p.name !== c.x), c.group, c.type === "heatmap" ? "…" : "none")}</select><//>`}
      ${f.has("size") && html`<${Field} label="Size"><select onChange=${(e) => set({ size: e.target.value })}>${opt(nums, c.size, "none")}</select><//>`}
      ${f.has("sort") && html`<${Field} label="Sort"><select value=${c.sort || ""} onChange=${(e) => set({ sort: e.target.value })}><option value="">Auto</option><option value="x">By ${lblX.toLowerCase()}</option><option value="desc">Largest first</option><option value="asc">Smallest first</option></select><//>`}
      ${f.has("top") && html`<${Field} label="Show"><select value=${String(c.top ?? "")} onChange=${(e) => set({ top: e.target.value === "" ? undefined : +e.target.value })}><option value="">Auto</option>${[5, 10, 20, 30, 50, 100, 500].map((n) => html`<option value=${n}>Top ${n}</option>`)}<option value="0">All</option></select><//>`}
      ${f.has("bins") && html`<${Field} label="Bins"><select value=${String(c.bins || "")} onChange=${(e) => set({ bins: +e.target.value || undefined })}><option value="">Auto</option>${[5, 10, 20, 30, 50, 100].map((n) => html`<option value=${n}>${n}</option>`)}</select><//>`}
      ${f.has("trend") && html`<label class="dv-f dv-ck"><input type="checkbox" checked=${!!c.trend} onChange=${(e) => set({ trend: e.target.checked })}/><span>Trend line</span></label>`}</div>
    ${out.error ? html`<div class="dv-host dv-empty">${Ic("chart", 22)}<span>${out.error}</span></div>` : html`<${EChart} option=${out.option} apiRef=${api}/>`}
    ${out.note && html`<div class="dv-note">${out.note}</div>`}</div>`;
}

// ---------- stats
function colStats(P, rows) {
  const n = rows.length; let miss = 0;
  const ds = new Set(), freq = new Map(), nums = [], times = [];
  for (const r of rows) {
    const v = r[P.i];
    if (v == null || v === "") { miss++; continue; }
    const s = String(v);
    if (ds.size < 100000) ds.add(s);
    if (P.kind === "num") { const x = toNum(v); if (!isNaN(x)) nums.push(x); }
    else if (P.kind === "date") { const t = toTime(v); if (!isNaN(t)) times.push(t); }
    if (P.kind !== "num" && freq.size < 50000) freq.set(s, (freq.get(s) || 0) + 1);
  }
  const st = { ...P, n, miss, distinct: ds.size, distinctMore: ds.size >= 100000 };
  const hist = (arr, lo, hi) => { const b = new Array(24).fill(0), w = (hi - lo) / 24 || 1; for (const x of arr) b[Math.min(23, Math.floor((x - lo) / w))]++; return b; };
  if (P.kind === "num" && nums.length) {
    nums.sort((a, b) => a - b);
    const sum = nums.reduce((s, x) => s + x, 0), mean = sum / nums.length;
    const sd = nums.length > 1 ? Math.sqrt(nums.reduce((s, x) => s + (x - mean) * (x - mean), 0) / (nums.length - 1)) : 0;
    Object.assign(st, { min: nums[0], max: nums[nums.length - 1], mean, sum, sd, q1: quant(nums, 0.25), med: quant(nums, 0.5), q3: quant(nums, 0.75), zeros: nums.filter((x) => x === 0).length, hist: hist(nums, nums[0], nums[nums.length - 1]) });
  } else if (P.kind === "date" && times.length) {
    let lo = Infinity, hi = -Infinity; for (const t of times) { if (t < lo) lo = t; if (t > hi) hi = t; }
    Object.assign(st, { tmin: lo, tmax: hi, span: (hi - lo) / 864e5, hist: hist(times, lo, hi) });
  }
  if (P.kind !== "num") st.top = [...freq.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
  return st;
}
function corr(prof, rows) {
  const cols = prof.filter((p) => p.kind === "num" && !p.idlike).slice(0, 12);
  if (cols.length < 2) return null;
  const v = cols.map((p) => Float64Array.from(rows, (r) => toNum(r[p.i])));
  const m = cols.map(() => cols.map(() => null));
  for (let a = 0; a < cols.length; a++) for (let b = a; b < cols.length; b++) {
    let n = 0, sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0; const X = v[a], Y = v[b];
    for (let i = 0; i < X.length; i++) { const x = X[i], y = Y[i]; if (x !== x || y !== y) continue; n++; sx += x; sy += y; sxx += x * x; syy += y * y; sxy += x * y; }
    const d = Math.sqrt((n * sxx - sx * sx) * (n * syy - sy * sy));
    m[a][b] = m[b][a] = n > 2 && d ? (n * sxy - sx * sy) / d : null;
  }
  return { cols, m };
}
function Spark({ bins }) {
  const mx = Math.max(1, ...bins), W = 120, H = 24, bw = W / bins.length;
  return html`<svg class="dv-spark" width=${W} height=${H} viewBox=${"0 0 " + W + " " + H} aria-hidden="true">${bins.map((b, i) => html`<rect x=${i * bw + 0.5} y=${H - Math.max(b ? 1 : 0, (b / mx) * H)} width=${Math.max(1, bw - 1)} height=${Math.max(b ? 1 : 0, (b / mx) * H)}/>`)}</svg>`;
}
const KIND = { num: "number", date: "date", bool: "yes / no", text: "text" };
function Stats({ rows, truncated, prof, onChart }) {
  const st = useMemo(() => prof.map((p) => colStats(p, rows)), [rows, prof]);
  const cr = useMemo(() => corr(prof, rows), [rows, prof]);
  const cells = rows.length * prof.length, miss = st.reduce((s, x) => s + x.miss, 0);
  const pct = (a, b) => (b ? (a / b * 100).toFixed(a && a < b / 1000 ? 2 : a === b ? 0 : 1) + "%" : "");
  const csv = () => { const H = ["column", "type", "rows", "missing", "distinct", "min", "max", "mean", "median", "std", "q1", "q3", "sum"];
    const L = st.map((s) => [s.name, s.kind, s.n, s.miss, s.distinct, s.min ?? (s.tmin != null ? fmtTime(s.tmin) : ""), s.max ?? (s.tmax != null ? fmtTime(s.tmax) : ""), s.mean ?? "", s.med ?? "", s.sd ?? "", s.q1 ?? "", s.q3 ?? "", s.sum ?? ""]);
    try { navigator.clipboard.writeText([H, ...L].map((r) => r.map((v) => (/[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : v)).join(",")).join("\n")); toast("Stats copied as CSV"); } catch (e) { /* no clipboard */ } };
  return html`<div class="dv-stats">
    <div class="dv-sum"><span class="dv-kpi"><b>${rows.length.toLocaleString()}</b><span>rows${truncated ? " (limited)" : ""}</span></span><span class="dv-kpi"><b>${prof.length}</b><span>columns</span></span>
      ${Object.keys(KIND).map((k) => { const n = prof.filter((p) => p.kind === k).length; return n ? html`<span class="dv-kpi"><b>${n}</b><span>${KIND[k]}</span></span>` : null; })}
      <span class="dv-kpi"><b>${pct(miss, cells) || "0%"}</b><span>cells missing</span></span><span class="grow"></span><button class="btn sm" onClick=${csv}>${Ic("copy", 12)} Copy as CSV</button></div>
    <div class="dv-scroll"><table class="dv-st"><thead><tr><th>Column</th><th>Type</th><th>Missing</th><th>Distinct</th><th>Min</th><th>Max</th><th>Mean</th><th>Median</th><th>Std dev</th><th>Distribution / top values</th><th></th></tr></thead>
      <tbody>${st.map((s) => html`<tr key=${s.name}><td class="dv-cn" title=${s.name}>${s.name}</td><td><span class=${"dv-k k-" + s.kind}>${KIND[s.kind]}</span></td>
        <td class=${"num" + (s.miss ? " warn" : "")}>${s.miss ? s.miss.toLocaleString() + " · " + pct(s.miss, s.n) : "0"}</td>
        <td class="num">${s.distinct.toLocaleString()}${s.distinctMore ? "+" : ""}${s.distinct === s.n - s.miss && s.n > 1 ? html` <span class="dv-uniq" title="Every value is different">unique</span>` : ""}</td>
        ${s.kind === "num" ? html`<td class="num">${full(s.min)}</td><td class="num">${full(s.max)}</td><td class="num">${full(s.mean)}</td><td class="num" title=${"Q1 " + full(s.q1) + " · Q3 " + full(s.q3)}>${full(s.med)}</td><td class="num">${full(s.sd)}</td>`
          : s.kind === "date" && s.tmin != null ? html`<td class="num">${fmtTime(s.tmin)}</td><td class="num">${fmtTime(s.tmax)}</td><td class="mute" colspan="3">${s.span >= 1 ? "spans " + fmt(s.span) + " days" : "within a day"}</td>`
          : html`<td colspan="5" class="mute"></td>`}
        <td>${s.hist ? html`<${Spark} bins=${s.hist}/>` : html`<div class="dv-top">${(s.top || []).map(([v, n]) => html`<div><span class="sq-ell" title=${v}>${v}</span><i style=${"width:" + Math.max(2, (n / (s.n - s.miss || 1)) * 100) + "%"}></i><em>${n.toLocaleString()}</em></div>`)}</div>`}</td>
        <td><button class="ibtn sm" title="Chart this column" aria-label=${"Chart " + s.name} onClick=${() => onChart(forColumn(s))}>${Ic("chart", 13)}</button></td></tr>`)}</tbody></table></div>
    ${cr && html`<div class="dv-corr"><b>Correlation</b> <span class="mute small">Pearson r between number columns (−1 … 1)</span>
      <div class="dv-scroll"><table class="dv-ct"><thead><tr><th></th>${cr.cols.map((p) => html`<th title=${p.name}>${p.name}</th>`)}</tr></thead>
        <tbody>${cr.cols.map((p, a) => html`<tr><th title=${p.name}>${p.name}</th>${cr.m[a].map((r, b) => html`<td title=${r == null ? "" : p.name + " × " + cr.cols[b].name}
          style=${r == null ? "" : "background:color-mix(in srgb," + (r >= 0 ? "#d97757" : "#5b8def") + " " + Math.round(Math.abs(r) * 75) + "%,transparent)"}
          onClick=${() => a !== b && onChart({ id: uid(), type: "scatter", x: p.name, y: [cr.cols[b].name], trend: true })}>${r == null ? "–" : r.toFixed(2)}</td>`)}</tr>`)}</tbody></table></div>
      <div class="mute small">Click a cell to plot the pair with a trend line.</div></div>`}</div>`;
}

// ---------- the view
export function DataView({ res, table, spec, onSpec, note, extra }) {
  const [own, setOwn] = useState(null), [menu, setMenu] = useState(false);
  const cur = spec && typeof spec === "object" ? spec : own || { view: "table", charts: [] };
  const rows = useMemo(() => res.rows || [], [res]), prof = useMemo(() => profile(res.columns || [], rows), [res]);
  const view = cur.view === "chart" || cur.view === "stats" ? cur.view : "table";
  const charts = (Array.isArray(cur.charts) ? cur.charts : []).filter((c) => c && TYPES[c.type]);
  const set = (s) => { const n = { ...cur, ...s }; setOwn(n); if (onSpec) onSpec(n); };
  const setCharts = (cs) => set({ charts: cs, view: "chart" });
  const go = (v) => set({ view: v, charts: v === "chart" && !charts.length ? suggest(prof, 2) : charts });
  const add = (c) => setCharts([...charts, { id: uid(), ...c }]);
  const addType = (t) => { setMenu(false); const s = suggest(prof, 8).find((x) => x.type === t) || { type: t, ...(t === "hist" || t === "box" ? {} : { x: (prof.find((p) => p.kind !== "num") || prof[0] || {}).name }), y: prof.filter((p) => p.kind === "num" && !p.idlike).slice(0, 1).map((p) => p.name) }; add({ ...s, type: t }); };
  const more = () => { const have = new Set(charts.map((c) => c.type + c.x + (c.y || []).join())); const s = suggest(prof, 8).filter((c) => !have.has(c.type + c.x + (c.y || []).join())); if (!s.length) return toast("No more suggestions: add one by type"); setCharts([...charts, ...s.slice(0, 4)]); };
  return html`<div class="dv">
    <div class="dv-bar"><${Seg} small=${true} value=${view} onChange=${go} items=${[{ id: "table", label: "Table", icon: "table" }, { id: "chart", label: "Charts" + (charts.length ? " " + charts.length : ""), icon: "chart" }, { id: "stats", label: "Stats", icon: "gauge" }]}/>
      ${view === "chart" && html`<${Pop} open=${menu} setOpen=${setMenu} up=${false} width=${230} button=${html`<button class="btn sm" onClick=${() => setMenu(!menu)}>${Ic("plus", 12)} Chart ${Ic("down", 11)}</button>`}>
          <div class="optlist dv-menu">${Object.entries(TYPES).map(([k, t]) => html`<button class="opt" onClick=${() => addType(k)}><span class="grow"><b>${t.label}</b></span></button>`)}</div><//>
        <button class="btn sm" title="Add the charts that fit these columns best" onClick=${more}>${Ic("sparkles", 12)} Suggest</button>
        ${charts.length > 0 && html`<button class="btn sm" onClick=${() => setCharts([])}>Clear</button>`}`}
      <span class="grow"></span>${(view !== "table" || note) && html`<span class="mute small">${view !== "table" ? rows.length.toLocaleString() + " rows" + (res.truncated ? " (limited)" : "") : ""}${note ? (view !== "table" ? " · " : "") + note : ""}</span>`}${extra || ""}</div>
    <div class=${"dv-body dv-" + view}>${view === "table" ? table
      : view === "stats" ? html`<${Stats} rows=${rows} truncated=${res.truncated} prof=${prof} onChart=${(c) => setCharts([...charts, c])}/>`
      : charts.length ? html`<div class="dv-charts">${charts.map((c, i) => html`<${ChartCard} key=${c.id || i} c=${c} prof=${prof} rows=${rows}
            set=${(p) => setCharts(charts.map((x, j) => (j === i ? { ...x, ...p } : x)))} remove=${() => setCharts(charts.filter((_, j) => j !== i))} dup=${() => setCharts([...charts.slice(0, i + 1), { ...c, id: uid() }, ...charts.slice(i + 1)])}/>`)}</div>`
      : html`<div class="dv-host dv-empty">${Ic("chart", 22)}<span>No chart yet: press Suggest or add one by type.</span></div>`}</div></div>`;
}

// ---------- DataFrames printed by a notebook cell -> {columns, rows, partial}
const CELL_NULL = new Set(["NaN", "nan", "None", "NaT", "<NA>", "null"]);
const cellVal = (s) => (CELL_NULL.has(s) ? null : s);
export function frameFromHtml(h) {
  if (!h || !/<table[^>]*class=["'][^"']*dataframe/i.test(h)) return null;
  const doc = new DOMParser().parseFromString(h, "text/html"), t = doc.querySelector("table.dataframe");
  if (!t) return null;
  const carry = [];                                              // a MultiIndex label spans rows (rowspan): repeat it in the rows below
  const body = [...t.querySelectorAll("tbody > tr")].map((tr) => {
    const out = [], cells = [...tr.children];
    for (let k = 0, c = 0; k < cells.length || (carry[c] && carry[c].n > 0);) {
      if (carry[c] && carry[c].n > 0) { out.push(carry[c].v); carry[c].n--; c++; continue; }
      const td = cells[k++], v = td.textContent.trim(), rs = Math.max(1, +td.getAttribute("rowspan") || 1), cs = Math.max(1, +td.getAttribute("colspan") || 1);
      for (let j = 0; j < cs; j++, c++) { out.push(v); carry[c] = rs > 1 ? { v, n: rs - 1 } : null; }
    }
    return out;
  });
  if (!body.length) return null;
  const W = Math.max(...body.map((r) => r.length)), nIdx = t.querySelector("tbody > tr") ? [...t.querySelector("tbody > tr").children].filter((x) => x.tagName === "TH").length : 0;
  const hdr = [...t.querySelectorAll("thead > tr")].filter((tr) => [...tr.children].every((x) => x.tagName === "TH")).map((tr) => {
    const out = []; for (const th of tr.children) { const span = Math.max(1, +th.getAttribute("colspan") || 1); for (let k = 0; k < span; k++) out.push(th.textContent.trim()); } return out; });
  const columns = Array.from({ length: W }, (_, i) => { const parts = hdr.map((r) => (r.length === W ? r[i] : r.length === W - nIdx && i >= nIdx ? r[i - nIdx] : "")).filter(Boolean); return [...new Set(parts)].join(" / ") || (i < nIdx ? (nIdx > 1 ? "index " + (i + 1) : "index") : "column " + (i + 1)); });
  const seen = new Map(), cols = columns.map((c) => { const k = seen.get(c) || 0; seen.set(c, k + 1); return k ? c + " (" + (k + 1) + ")" : c; });
  let partial = false;
  const rows = body.filter((r) => { const dots = r.every((v) => v === "..." || v === "…" || v === ""); if (dots) partial = true; return !dots; }).map((r) => {
    const o = r.map(cellVal); while (o.length < W) o.push(null); return o.slice(0, W); });
  return rows.length && cols.length ? { columns: cols, rows, partial } : null;
}
export function frameFromResource(d) {
  try {
    const o = typeof d === "string" ? JSON.parse(d) : d, f = ((o.schema || {}).fields || []).map((x) => String(x.name));
    if (!f.length || !Array.isArray(o.data)) return null;
    return { columns: f, rows: o.data.map((r) => f.map((k) => (r[k] === undefined ? null : r[k]))), partial: false };
  } catch (e) { return null; }
}
