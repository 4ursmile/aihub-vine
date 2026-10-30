// Usage page + price alerts. Data: GET /api/usage (local session logs + this UI's free chat, priced with the hub price table).
// Every answer turn and work run is also uploaded to the hub (server side, hubusage.py).
import { html, useState, useEffect } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, guard, Seg, Modal } from "./ui.js";

const usd = (v) => "$" + (v >= 100 ? v.toFixed(0) : v >= 0.01 || !v ? v.toFixed(2) : v.toFixed(3));
const tok = (n) => (n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(Math.round(n || 0)));
const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago"; };

export function useAlerts(api) {
  const [al, setAl] = useState([]);
  useEffect(() => {
    let live = true;
    const load = () => api("usage").then((d) => live && setAl(d.alerts || [])).catch(() => {});
    load(); const t = setInterval(load, 300000);
    const on = () => load(); window.addEventListener("aihub-usage", on);
    return () => { live = false; clearInterval(t); window.removeEventListener("aihub-usage", on); };
  }, []);
  return al;
}

export function AlertBar({ alerts }) {
  const key = (a) => a.id + ":" + a.level, [gone, setGone] = useState(() => { try { return JSON.parse(sessionStorage.getItem("usage_dismissed") || "[]"); } catch (e) { return []; } });
  const show = alerts.filter((a) => !gone.includes(key(a)));
  if (!show.length) return null;
  const hide = () => { const n = [...gone, ...show.map(key)]; setGone(n); try { sessionStorage.setItem("usage_dismissed", JSON.stringify(n)); } catch (e) {} };
  const bad = show.some((a) => a.level === "over");
  return html`<div class=${"ubanner" + (bad ? " over" : "")} role="alert">${Ic("alert", 16)}<span class="grow"><b>${show[0].title}.</b> ${show[0].text}${show.length > 1 ? " (+" + (show.length - 1) + " more)" : ""}</span>
    <a class="btn" href="#/usage">Open usage</a><a class="ibtn" title="Dismiss for this session" onClick=${hide}>${Ic("x", 14)}</a></div>`;
}

function Meter({ r }) {
  return html`<div class=${"umeter " + r.level}><div class="row"><b>${r.label}</b><span class="grow"></span><span class="mute small">${usd(r.spent)} / ${usd(r.limit)} (${Math.round(r.pct)}%)</span></div>
    <div class="ubar"><i style=${"width:" + Math.min(100, r.pct) + "%"}></i>${r.at.filter((t) => t <= 100).map((t) => html`<u style=${"left:" + t + "%"} title=${"alert at " + t + "%"}></u>`)}</div>
    <div class="mute small">Alerts at ${r.at.join("%, ")}%${r.projected ? " · pace: " + usd(r.projected) : ""}</div></div>`;
}

function Chart({ days, limit }) {
  const d = [...days].sort((x, y) => (x.name < y.name ? -1 : 1)).slice(-62);
  if (!d.length) return html`<div class="empty small">No usage in this period.</div>`;
  const W = 640, H = 150, P = 24, mx = Math.max(...d.map((x) => x.cost), limit || 0, 0.01), bw = (W - P) / d.length;
  return html`<div class="chart"><svg viewBox=${`0 0 ${W} ${H + 22}`} role="img" aria-label="Estimated cost per day">
    <line class="cg" x1=${P} x2=${W} y1=${H} y2=${H}/>
    ${d.map((x, i) => { const h = Math.max(1, (x.cost / mx) * (H - 8)); return html`<rect class="bar" x=${P + i * bw + 1} y=${H - h} width=${Math.max(1, bw - 2)} height=${h} rx="2" fill="var(--accent)"><title>${x.name}: ${usd(x.cost)}, ${x.requests} requests</title></rect>`; })}
    ${limit > 0 && html`<line x1=${P} x2=${W} y1=${H - (limit / mx) * (H - 8)} y2=${H - (limit / mx) * (H - 8)} stroke="var(--bad)" stroke-dasharray="4 3"/>`}
    <text class="ct" x=${P} y=${H + 15}>${d[0].name}</text><text class="ct" x=${W} y=${H + 15} text-anchor="end">${d[d.length - 1].name}</text><text class="ct" x="0" y="10">${usd(mx)}</text></svg></div>`;
}

function Table({ rows, label }) {
  if (!rows.length) return null;
  return html`<table class="utab"><thead><tr><th>${label}</th><th>Requests</th><th>Output</th><th>Cache read</th><th>Cost</th></tr></thead><tbody>
    ${[...rows].sort((a, b) => b.cost - a.cost).map((r) => html`<tr><td>${Array.isArray(r.name) ? r.name.join(" / ") : r.name}</td><td>${r.requests}</td><td>${tok(r.output)}</td><td>${tok(r.cache_read)}</td><td><b>${usd(r.cost)}</b>${r.unpriced ? "*" : ""}</td></tr>`)}</tbody></table>`;
}

const PER = [["today", "Today"], ["week", "This week"], ["month", "This month"], ["quarter", "This quarter"], ["last", "Last N days"]];
function Rules({ rules, save }) {
  const mk = (r) => ({ id: r.id, period: r.period, n: String(r.n || 7), limit: String(r.limit || ""), at: (r.at || [80, 100]).join(", ") });
  const [rows, setRows] = useState(rules.map(mk)), [open, setOpen] = useState(!rules.length);
  useEffect(() => setRows(rules.map(mk)), [JSON.stringify(rules.map((r) => [r.id, r.period, r.n, r.limit, r.at]))]);
  const set = (i, k, v) => setRows(rows.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  const go = (e) => { e.preventDefault(); save(rows.filter((r) => String(r.limit).trim() !== "").map((r) => ({ id: r.id, period: r.period, n: Number(r.n || 7), limit: Number(r.limit), at: r.at }))); };
  return html`<div class="card"><div class="row"><b>Price alerts</b><span class="grow"></span><button class="btn" onClick=${() => setOpen(!open)}>${Ic("sliders", 13)} ${open ? "Hide" : "Edit"}</button></div>
    ${rules.map((r) => html`<${Meter} r=${r}/>`)}${!rules.length && !open && html`<p class="mute small">No alert yet. Add one: pick a period, a limit and the percents that should warn you.</p>`}
    ${open && html`<form onSubmit=${go}>${rows.map((r, i) => html`<div class="urule">
        <select value=${r.period} onChange=${(e) => set(i, "period", e.target.value)}>${PER.map(([v, l]) => html`<option value=${v}>${l}</option>`)}</select>
        ${r.period === "last" ? html`<label>days<input type="number" min="1" max="365" value=${r.n} onInput=${(e) => set(i, "n", e.target.value)}/></label>` : html`<span></span>`}
        <label>limit USD<input type="number" min="0" step="1" value=${r.limit} placeholder="e.g. 100" onInput=${(e) => set(i, "limit", e.target.value)}/></label>
        <label>alert at %<input type="text" value=${r.at} placeholder="50, 80, 100" onInput=${(e) => set(i, "at", e.target.value)}/></label>
        <a class="ibtn" title="Remove" onClick=${() => setRows(rows.filter((_, j) => j !== i))}>${Ic("trash", 14)}</a></div>`)}
      <div class="row" style="margin-top:10px"><button type="button" class="btn" onClick=${() => setRows([...rows, { id: "", period: "month", n: "7", limit: "", at: "50, 80, 100" }])}>${Ic("plus", 13)} Add alert</button><span class="grow"></span><span class="mute small">Several percents per alert are fine. A row without a limit is dropped.</span><button class="btn acc" type="submit">Save</button></div></form>`}</div>`;
}

// ---------- By session: one row per session, peak context against the window, hint flags; click for the context growth chart.
const BIG = 150000;                                                   // same threshold as the CLI tips (usage.BIG_CTX)
const clock = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const span = (s) => { s = Math.max(0, Math.round(s)); return s < 60 ? "<1m" : s < 3600 ? Math.floor(s / 60) + "m" : s < 86400 ? Math.floor(s / 3600) + "h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "m" : Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h"; };
const SORT = [{ id: "recent", label: "Recent" }, { id: "cost", label: "Cost" }, { id: "tokens", label: "Tokens" }, { id: "ctx", label: "Peak context" }];
const FLAGS = [["cold", "Cold cache", "Prompt cache rebuilt after an idle gap"], ["compact", "Compacted", "Context fell by half or more"], ["big", "Big context", "Requests over 150k tokens of context"]];

function Flags({ f }) {
  const on = FLAGS.filter(([k]) => f && f[k] > 0);
  if (!on.length) return html`<span class="mute small">-</span>`;
  return html`<span class="sflags">${on.map(([k, l, t]) => html`<span class=${"sflag " + k} title=${t + " (" + f[k] + ")"}>${l}${f[k] > 1 ? " ×" + f[k] : ""}</span>`)}</span>`;
}

function PeakBar({ peak, win, wide }) {
  const pct = Math.min(100, (peak / win) * 100), hot = peak > BIG;
  return html`<div class=${"peak" + (hot ? " hot" : "") + (wide ? " wide" : "")} title=${tok(peak) + " tokens of " + tok(win) + " window (" + pct.toFixed(1) + "%)"}><b>${tok(peak)}</b><div class="ubar"><i style=${"width:" + Math.max(peak ? 2 : 0, pct) + "%"}></i><u style=${"left:" + Math.min(100, (BIG / win) * 100) + "%"} title="150k tokens"></u></div></div>`;
}

function CtxChart({ series, win }) {
  if (!series.length) return html`<div class="empty small">No requests.</div>`;
  const W = 640, H = 170, L = 44, B = 22, n = series.length, mx = Math.max(...series.map((r) => r[1]), BIG * 1.15, 1), X = (i) => L + (n < 2 ? (W - L) / 2 : (i / (n - 1)) * (W - L - 6)), Y = (v) => H - (v / mx) * (H - 10);
  const pts = series.map((r, i) => X(i).toFixed(1) + "," + Y(r[1]).toFixed(1)).join(" "), area = "M" + X(0) + "," + H + " L" + pts.replace(/ /g, " L") + " L" + X(n - 1) + "," + H + "Z";
  return html`<div class="chart"><svg viewBox=${`0 0 ${W} ${H + B}`} role="img" aria-label="Context size per request">
    <line class="cg" x1=${L} x2=${W} y1=${H} y2=${H}/>
    <line x1=${L} x2=${W} y1=${Y(BIG)} y2=${Y(BIG)} stroke="var(--warn,#d9a21b)" stroke-dasharray="4 3"/><text class="ct" x=${L - 4} y=${Y(BIG) + 3} text-anchor="end">150k</text>
    ${mx >= win * 0.98 && html`<text class="ct" x=${L - 4} y="10" text-anchor="end">${tok(win)}</text>`}
    <path d=${area} fill="var(--accent)" opacity=".13"/><polyline points=${pts} fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>
    ${series.map((r, i) => r[4].length ? html`<circle cx=${X(i)} cy=${Y(r[1])} r="3.5" class=${"pt " + r[4][r[4].length - 1]}><title>${"#" + (i + 1) + " " + tok(r[1]) + " context · " + r[4].join(", ")}</title></circle>` : n <= 80 && html`<circle cx=${X(i)} cy=${Y(r[1])} r="2" fill="var(--accent)"><title>${"#" + (i + 1) + " " + tok(r[1]) + " context"}</title></circle>`)}
    <text class="ct" x=${L - 4} y=${H + 3} text-anchor="end">0</text><text class="ct" x=${L} y=${H + 16}>${clock(series[0][0])}</text><text class="ct" x=${W} y=${H + 16} text-anchor="end">${clock(series[n - 1][0])}</text></svg></div>`;
}

function SessionDetail({ api, row, days, win, close }) {
  const [d, setD] = useState(null), [err, setErr] = useState("");
  useEffect(() => { let live = true; api("usage/session?id=" + encodeURIComponent(row.session) + "&days=" + days).then((x) => live && setD(x)).catch((e) => live && setErr(e.message)); return () => { live = false; }; }, [row.session]);
  const s = d || row, all = s.input + s.output + s.cache_write + s.cache_read || 1, parts = [["Input", s.input, "var(--accent)"], ["Output", s.output, "var(--ok,#4a9d6b)"], ["Cache write", s.cache_write, "var(--warn,#d9a21b)"], ["Cache read", s.cache_read, "var(--mute)"]];
  return html`<${Modal} close=${close} wide=${true}><div class="sdet"><div class="row"><h2 class="grow" style="margin:0">${row.title || "Session " + row.id}</h2><a class="ibtn" title="Close" onClick=${close}>${Ic("x", 16)}</a></div>
    <p class="mute small">${row.tool} · ${row.cwd || row.project} · ${(Object.keys(row.models || {})).join(", ")}<br/>${clock(row.start)} to ${clock(row.end)} (${span(row.end - row.start)}) · ${row.requests} requests · <b>${usd(row.cost)}</b>${row.unpriced ? "*" : ""} · cache hit ${Math.round((row.cache_hit || 0) * 100)}%</p>
    <h3>Context per request</h3><div class="row small mute" style="gap:14px">Peak <${PeakBar} peak=${row.peak} win=${win} wide=${true}/></div>
    ${err ? html`<p class="tag bad">${err}</p>` : !d ? html`<p class="mute small">Reading the session…</p>` : html`<${CtxChart} series=${d.series || []} win=${win}/>`}
    <h3>Token split</h3><div class="tsplit">${parts.map(([l, v, c]) => html`<i style=${"width:" + (v / all) * 100 + "%;background:" + c} title=${l + ": " + tok(v)}></i>`)}</div>
    <div class="tleg">${parts.map(([l, v, c]) => html`<span><i style=${"background:" + c}></i>${l} <b>${tok(v)}</b></span>`)}</div>
    <h3>Hints</h3><p><${Flags} f=${s.flags}/></p>
    ${d && (d.tips || []).length ? d.tips.map((t) => html`<div class=${"stip " + t.level}><b>${t.title}</b><div class="mute small">${t.detail}</div></div>`) : d && html`<p class="mute small">Nothing to improve in this session.</p>`}</div><//>`;
}

function SessionsView({ api, days, win }) {
  const [q, setQ] = useState(""), [qq, setQq] = useState(""), [sort, setSort] = useState("recent"), [rows, setRows] = useState([]), [tot, setTot] = useState(0), [busy, setBusy] = useState(true), [err, setErr] = useState(""), [open, setOpen] = useState(null);
  useEffect(() => { const t = setTimeout(() => setQq(q), 250); return () => clearTimeout(t); }, [q]);
  const url = (off) => "usage/sessions?days=" + days + "&sort=" + sort + "&offset=" + off + "&limit=50&q=" + encodeURIComponent(qq);
  useEffect(() => { let live = true; setBusy(true); api(url(0)).then((x) => { if (live) { setRows(x.rows); setTot(x.total); setErr(""); setBusy(false); } }).catch((e) => { if (live) { setErr(e.message); setBusy(false); } }); return () => { live = false; }; }, [days, sort, qq]);
  const more = guard(async () => { setBusy(true); try { const x = await api(url(rows.length)); setRows((r) => r.concat(x.rows)); setTot(x.total); } finally { setBusy(false); } });
  return html`<div class="sess"><div class="row" style="margin:6px 0 10px;gap:10px;flex-wrap:wrap"><div class="ssearch">${Ic("search", 14)}<input type="search" placeholder="Search title, folder, tool, model" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Search sessions"/></div>
      <span class="grow"></span><span class="mute small">Sort</span><${Seg} value=${sort} onChange=${setSort} items=${SORT} small=${true}/></div>
    ${err && html`<p class="tag bad">${err}</p>`}
    <div class="card sscroll"><table class="utab stab"><thead><tr><th>Session</th><th>Tool / model</th><th>Last activity</th><th>Req</th><th>In</th><th>Out</th><th>Cache wr.</th><th>Cache rd.</th><th>Cost</th><th class="pk">Peak context</th><th>Hints</th></tr></thead><tbody>
      ${rows.map((r) => html`<tr class="sr" tabindex="0" key=${r.tool + r.session} onClick=${() => setOpen(r)} onKeyDown=${(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setOpen(r))}>
        <td class="st"><b title=${r.title || ""}>${r.title || "Session " + r.id}</b><span class="mute small" title=${r.cwd}>${r.project}</span></td>
        <td class="sm"><span>${r.tool}</span><span class="mute small" title=${Object.keys(r.models || {}).join(", ")}>${Object.keys(r.models || {}).join(", ")}</span></td>
        <td class="sm"><span>${clock(r.end)}</span><span class="mute small">${clock(r.start)} · ${span(r.end - r.start)}</span></td>
        <td>${r.requests}</td><td>${tok(r.input)}</td><td>${tok(r.output)}</td><td>${tok(r.cache_write)}</td><td>${tok(r.cache_read)}</td><td><b>${usd(r.cost)}</b>${r.unpriced ? "*" : ""}</td>
        <td class="pk"><${PeakBar} peak=${r.peak} win=${win}/></td><td class="fl"><${Flags} f=${r.flags}/></td></tr>`)}</tbody></table>
      ${!rows.length && !busy && html`<div class="empty small">No sessions in this period${qq ? " match “" + qq + "”" : ""}.</div>`}
      <div class="row" style="padding:8px 4px 0"><span class="mute small">${busy ? "Loading…" : rows.length + " of " + tot + " sessions"}</span><span class="grow"></span>${rows.length < tot && html`<button class="btn" disabled=${busy} onClick=${more}>Show 50 more</button>`}</div></div>
    ${open && html`<${SessionDetail} api=${api} row=${open} days=${days} win=${win} close=${() => setOpen(null)}/>`}</div>`;
}

export function UsagePage({ api, savePrefs, prefs }) {
  const [view, setView] = useState("summary"), [d, setD] = useState(null), [per, setPer] = useState("month"), [err, setErr] = useState(""), [days, setDays] = useState(7);
  const load = (fresh) => api("usage?days=" + days + (fresh ? "&fresh=1" : "")).then((x) => { setD(x); setErr(""); }).catch((e) => setErr(e.message));
  useEffect(() => { const t = setTimeout(load, 250); return () => clearTimeout(t); }, [days, JSON.stringify(prefs.alerts || [])]);
  if (err && !d) return html`<div class="page"><h1>Usage</h1><p class="tag bad">${err}</p></div>`;
  if (!d) return html`<div class="page"><h1>Usage</h1><p class="mute">Reading session logs…</p></div>`;
  const p = d.periods[per] || d.periods.month || {}, t = p.total || { cost: 0, requests: 0, output: 0, cache_hit: 0, input: 0, cache_read: 0, cache_write: 0 };
  const s = d.sync, items = [{ id: "today", label: "Today" }, { id: "month", label: "This month" }, { id: "last", label: "Last " + days + " days" }, { id: "all", label: "All time" }];
  const saveR = guard(async (alerts) => { if (!(await savePrefs({ alerts }))) return; await load(true); window.dispatchEvent(new Event("aihub-usage")); toast("Alerts saved"); });
  return html`<div class="page"><div class="row"><h1 class="grow">Usage</h1><button class="btn" onClick=${() => load(true)}>${Ic("refresh", 14)} Refresh</button></div>
    <p class="mute small" style="margin-top:-6px">Estimated from list prices (a subscription or enterprise bill differs). ${s.ok ? html`Sent to the hub ${ago(s.ok)}.` : "Not sent to the hub yet."}${s.fails ? " The last upload failed and is retried." : ""} Every chat answer and work run is counted.</p>
    <${Rules} rules=${d.rules} save=${saveR}/>
    <div class="row" style="margin:14px 0 0"><${Seg} value=${view} onChange=${setView} items=${[{ id: "summary", label: "Summary", icon: "chart" }, { id: "sessions", label: "By session", icon: "list" }]}/></div>
    <div class="row" style="margin:14px 0 8px"><${Seg} value=${per} onChange=${setPer} items=${items}/>${per === "last" && html`<label class="mute small">days <input type="number" min="1" max="365" style="width:70px" value=${days} onInput=${(e) => setDays(Math.max(1, Math.min(365, Number(e.target.value) || 7)))}/></label>`}</div>
    ${view === "sessions" ? html`<${SessionsView} api=${api} days=${per === "today" ? 1 : per === "month" ? new Date().getDate() : per === "all" ? 0 : days} win=${prefs.window || 1000000}/>` : html`<div class="ukpi"><div class="card"><span class="mute small">Estimated cost</span><b>${usd(t.cost)}</b></div>
      <div class="card"><span class="mute small">Requests</span><b>${t.requests}</b><span class="mute small">${t.sessions || 0} sessions</span></div>
      <div class="card"><span class="mute small">Output tokens</span><b>${tok(t.output)}</b><span class="mute small">input ${tok(t.input)}</span></div>
      <div class="card"><span class="mute small">Cache hit</span><b>${Math.round((t.cache_hit || 0) * 100)}%</b><span class="mute small">read ${tok(t.cache_read)}</span></div></div>
    <h2>Cost per day</h2><div class="card"><${Chart} days=${p.by_day || []} limit=${0}/></div>
    <h2>By tool</h2><div class="card"><${Table} rows=${p.by_tool || []} label="Tool"/></div>
    <h2>By model</h2><div class="card"><${Table} rows=${p.by_model || []} label="Model"/>${(p.by_model || []).some((r) => r.unpriced) && html`<p class="mute small">* some requests have no price in the table.</p>`}</div>`}</div>`;
}
