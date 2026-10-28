// Usage page + price alerts. Data: GET /api/usage (local session logs + this UI's free chat, priced with the hub price table).
// Every answer turn and work run is also uploaded to the hub (server side, hubusage.py).
import { html, useState, useEffect } from "https://cdn.jsdelivr.net/npm/htm@3.1.1/preact/standalone.module.js";
import { Ic } from "./icons.js";
import { toast, guard, Seg } from "./ui.js";

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

export function UsagePage({ api, savePrefs, prefs }) {
  const [d, setD] = useState(null), [per, setPer] = useState("month"), [err, setErr] = useState(""), [days, setDays] = useState(7);
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
    <div class="row" style="margin:14px 0 8px"><${Seg} value=${per} onChange=${setPer} items=${items}/>${per === "last" && html`<label class="mute small">days <input type="number" min="1" max="365" style="width:70px" value=${days} onInput=${(e) => setDays(Math.max(1, Math.min(365, Number(e.target.value) || 7)))}/></label>`}</div>
    <div class="ukpi"><div class="card"><span class="mute small">Estimated cost</span><b>${usd(t.cost)}</b></div>
      <div class="card"><span class="mute small">Requests</span><b>${t.requests}</b><span class="mute small">${t.sessions || 0} sessions</span></div>
      <div class="card"><span class="mute small">Output tokens</span><b>${tok(t.output)}</b><span class="mute small">input ${tok(t.input)}</span></div>
      <div class="card"><span class="mute small">Cache hit</span><b>${Math.round((t.cache_hit || 0) * 100)}%</b><span class="mute small">read ${tok(t.cache_read)}</span></div></div>
    <h2>Cost per day</h2><div class="card"><${Chart} days=${p.by_day || []} limit=${0}/></div>
    <h2>By tool</h2><div class="card"><${Table} rows=${p.by_tool || []} label="Tool"/></div>
    <h2>By model</h2><div class="card"><${Table} rows=${p.by_model || []} label="Model"/>${(p.by_model || []).some((r) => r.unpriced) && html`<p class="mute small">* some requests have no price in the table.</p>`}</div></div>`;
}
