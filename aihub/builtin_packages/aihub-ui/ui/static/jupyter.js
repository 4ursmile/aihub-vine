// Notebook: work with Jupyter servers (JupyterHub, Jupyter Server / Lab, or one this UI installs and runs locally).
// File browser, notebook editor with a live kernel (tables, figures, rich HTML), SQL cells that can fill a pandas DataFrame, a remote terminal,
// and AI help (inline suggestions, write a cell, fix an error) where each of the three has its own model. A notebook can become a Works node.
// Data: /api/jupyter/* (server.py -> aihubui/jupyter.py). Tokens never reach the browser.
import { h, html, Component, useState, useEffect, useRef, useMemo, useCallback } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, guard, escPush, Seg, Modal, Switch, Pop, confirmBox, ask, CtxMenu, menuAt } from "./ui.js";
import { Output, serializeNotebook, parseNotebook, cellsOf, fixCR, widgetEvent, widgetSnap } from "./notebook.js";
import { Grid, drag } from "./sqlstudio.js";
import { DataView, frameFromHtml, frameFromResource } from "./dataviz.js";
import { Md } from "./blocks.js";
import { useHl, colorize, esc, langOf } from "./code.js";
import { CodeBox, IDENT, KW } from "./codebox.js";
import { sqlComplete, sqlSignature, SQL_WORDS } from "./sqlintel.js";
import { loadXterm, wireTerm, termSuggestOn, setTermSuggest, termTheme } from "./term.js";

const LS = { get(k, d) { try { const v = localStorage.getItem("nbj_" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }, set(k, v) { try { localStorage.setItem("nbj_" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } } };
const enc = encodeURIComponent, sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const uid = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
const join = (s) => (Array.isArray(s) ? s.join("") : typeof s === "string" ? s : "");
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g, isMacJ = /Mac/.test(navigator.platform);
let nbClip = null;                                                          // Copy / Cut of the Notebook file list: {sid, path, name, type, cut}
const fmtSize = (n) => (n == null ? "" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB" : (n / 1048576).toFixed(1) + " MB");
const ago = (t) => { const d = t ? (Date.now() - new Date(t).getTime()) / 1000 : NaN; return isNaN(d) ? "" : d < 60 ? "just now" : d < 3600 ? Math.floor(d / 60) + " min ago" : d < 86400 ? Math.floor(d / 3600) + " h ago" : d < 2592000 ? Math.floor(d / 86400) + " d ago" : new Date(t).toLocaleDateString(); };
const KSHORT = { hub: "JupyterHub", server: "Jupyter Server", local: "Local" };
const KIND = { hub: "JupyterHub", server: "Jupyter Server / Lab", local: "Local (installed by AI Hub)" };
const sqlOf = (c) => (c.type === "code" && c.raw && c.raw.metadata && c.raw.metadata.aihub && c.raw.metadata.aihub.sql) || null;
// chart / stats views of a cell's data, kept in the cell metadata so they come back with the notebook: aihub.viz = {sql | output index: spec}
const vizOf = (c, k) => ((c.raw && c.raw.metadata && c.raw.metadata.aihub && c.raw.metadata.aihub.viz) || {})[k] || null;
const withViz = (c, k, spec) => {
  const raw = { ...(c.raw || {}) }, md = { ...(raw.metadata || {}) }, ai = { ...(md.aihub || {}) }, v = { ...(ai.viz || {}) };
  if (spec && (spec.view !== "table" || (spec.charts || []).length)) v[k] = spec; else delete v[k];
  if (Object.keys(v).length) ai.viz = v; else delete ai.viz;
  if (Object.keys(ai).length) md.aihub = ai; else delete md.aihub;
  raw.metadata = md; return raw;
};
const frameOf = (o) => { if (o.output_type !== "display_data" && o.output_type !== "execute_result") return null; const d = o.data || {}; return (d["application/vnd.dataresource+json"] && frameFromResource(d["application/vnd.dataresource+json"])) || frameFromHtml(join(d["text/html"])); };
// output view per cell, in the standard nbformat fields so Jupyter shows the same: metadata.jupyter.outputs_hidden (+ collapsed) and metadata.scrolled
const metaOf = (c) => (c.raw && c.raw.metadata) || {};
const hiddenOf = (c) => !!((metaOf(c).jupyter || {}).outputs_hidden || metaOf(c).collapsed === true);
const scrollOf = (c) => metaOf(c).scrolled !== false;                       // default: a long output scrolls in its own box
const withMeta = (c, f) => { const raw = { ...(c.raw || {}) }; raw.metadata = f({ ...(raw.metadata || {}) }); return raw; };
const withHidden = (c, on) => withMeta(c, (m) => { const j = { ...(m.jupyter || {}) }; if (on) { j.outputs_hidden = true; m.collapsed = true; } else { delete j.outputs_hidden; delete m.collapsed; } if (Object.keys(j).length) m.jupyter = j; else delete m.jupyter; return m; });
const withScroll = (c, on) => withMeta(c, (m) => ({ ...m, scrolled: !!on }));
const hasOut = (c) => c.type === "code" && (c.outputs.length > 0 || !!c.sqlres);
// the output area of one cell: scrolls in a box (and follows a running cell's new output while you are at the bottom) or shows everything
function OutBox({ scroll, running, children }) {
  const r = useRef(), stick = useRef(true);
  useEffect(() => { if (running) stick.current = true; }, [running]);
  useEffect(() => { const el = r.current; if (el && scroll && running && stick.current) el.scrollTop = el.scrollHeight; });
  return html`<div ref=${r} class=${"nb-outs " + (scroll ? "scroll" : "full")} onScroll=${(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24; }}>${children}</div>`;
}
const withSql = (c, sql) => { const raw = { ...(c.raw || {}) }, md = { ...(raw.metadata || {}) }, ai = { ...(md.aihub || {}) }; if (sql) ai.sql = sql; else delete ai.sql; if (Object.keys(ai).length) md.aihub = ai; else delete md.aihub; raw.metadata = md; return raw; };

// kernel output event -> the cell's nbformat outputs (same rules as the server's Fold)
function fold(o, dids, e) {
  const t = e.t, c = e.c || {};
  if (t === "stream") { const l = o[o.length - 1]; return l && l.output_type === "stream" && l.name === c.name ? [...o.slice(0, -1), { ...l, text: fixCR(join(l.text) + (c.text || "")) }] : [...o, { output_type: "stream", name: c.name || "stdout", text: fixCR(c.text || "") }]; }
  if (t === "display_data" || t === "execute_result" || t === "update_display_data") {
    const did = c.transient && c.transient.display_id;
    if (t === "update_display_data" && did && dids[did]) { const old = dids[did], nw = { ...old, data: c.data || {}, metadata: c.metadata || {} }; dids[did] = nw; return o.map((x) => (x === old ? nw : x)); }
    const x = { output_type: t === "update_display_data" ? "display_data" : t, data: c.data || {}, metadata: c.metadata || {} };
    if (t === "execute_result") x.execution_count = c.execution_count;
    if (did) dids[did] = x;
    return [...o, x];
  }
  if (t === "error") return [...o, { output_type: "error", ename: c.ename, evalue: c.evalue, traceback: c.traceback || [] }];
  if (t === "clear_output") return [];
  return o;
}
const outText = (c, n = 300) => { const s = (c.outputs || []).map((o) => (o.output_type === "stream" ? join(o.text) : o.output_type === "error" ? o.ename + ": " + o.evalue : join((o.data || {})["text/plain"]))).join("\n").replace(ANSI, ""); return s.slice(0, n); };
const errText = (c) => { const e = (c.outputs || []).find((o) => o.output_type === "error"); return e ? (e.traceback || []).join("\n").replace(ANSI, "") || e.ename + ": " + e.evalue : c.sqlres && !c.sqlres.ok ? c.sqlres.error : c.sqlres && c.sqlres.assigned && !c.sqlres.assigned.ok ? c.sqlres.assigned.error : ""; };

// ---------- one output: rich HTML that needs scripts (plotly, bokeh) runs in a sandboxed frame served by the UI, everything else is the shared renderer
function Out({ o, post, viz, onViz }) {
  const d = o.data || {}, h = join(d["text/html"]);
  const [src, setSrc] = useState("");
  const df = useMemo(() => frameOf(o), [o]);
  const rich = (o.output_type === "display_data" || o.output_type === "execute_result") && h && /<script/i.test(h) && !d["image/png"];
  useEffect(() => { if (rich) post("jupyter/frame", { html: h }).then((r) => setSrc("/nbframe/" + r.id)).catch(() => {}); }, [rich, h]);
  if (rich) return src ? html`<iframe class="nbj-frame" sandbox="allow-scripts allow-popups" src=${src} title="Interactive output"></iframe>` : html`<div class="nb-o mute small">Loading output…</div>`;
  if (df) return html`<${DataView} res=${df} spec=${viz} onSpec=${onViz} table=${html`<${Output} o=${o}/>`} note=${"from the " + df.rows.length + " rows printed" + (df.partial ? " (output was cut: show more rows for full charts)" : "")}/>`;
  return html`<${Output} o=${o}/>`;
}

// ---------- AI model picker (same list and look as the chat's model picker)
function ModelSelect({ models, value, onChange }) {
  const [open, setOpen] = useState(false), cur = models.find((m) => m.id === value) || models[0];
  return html`<${Pop} open=${open} setOpen=${setOpen} up=${false} width=${340} button=${html`<button class="btn" onClick=${() => setOpen(!open)}>${Ic("cpu", 14)} ${cur ? cur.label : "Default"} ${Ic("down", 12)}</button>`}>
    <div class="optlist"><h5>Model</h5><div class="mlist">${models.map((m) => html`<button class=${"opt" + (m.id === value ? " on" : "")} onClick=${() => { onChange(m.id); setOpen(false); }}><span class="oi">${Ic("cpu", 16)}</span>
      <span class="grow"><b>${m.label}</b><span class="d">${m.note}</span></span><span class="ck">${Ic("check", 16)}</span></button>`)}</div></div><//>`;
}
function AiSettings({ settings, models, post, setSettings }) {
  const chat = (models || []).filter((m) => (m.kind || "chat") === "chat");
  const set = (k, patch) => { const next = { ...settings, [k]: { ...settings[k], ...patch } }; setSettings(next); post("jupyter/settings", { [k]: patch }).then((r) => setSettings(r.settings)).catch((e) => toast(e.message)); };
  const row = (k, icon, title, text, extra) => html`<div class="nbj-set"><span class="sq-logo">${Ic(icon, 16)}</span><div class="grow"><b>${title}</b><div class="mute small">${text}</div></div>${extra}<${ModelSelect} models=${chat} value=${settings[k].model} onChange=${(v) => set(k, { model: v })}/></div>`;
  return html`<div class="nbj-sets">
    ${row("suggest", "zap", "Code suggestions", "Grey ghost text while you type at the end of a cell. Tab accepts, Esc dismisses. A small fast model works best.", html`<div class="row" style="gap:8px"><label class="mute small">after <select value=${settings.suggest.delay} onChange=${(e) => set("suggest", { delay: +e.target.value })} aria-label="Delay">${[500, 900, 1500, 2500].map((n) => html`<option value=${n}>${n / 1000} s</option>`)}</select></label><${Switch} on=${settings.suggest.on} onChange=${(v) => set("suggest", { on: v })} label="Code suggestions"/></div>`)}
    ${row("write", "sparkles", "Write code", "Ask AI on a cell: describe what you want, it writes the cell using the earlier cells (and, for SQL cells, your database schema).")}
    ${row("fix", "bolt", "Fix errors", "Fix with AI on a failed cell: sends the code and the traceback; you review the fix before it replaces anything.")}
    <p class="mute small" style="margin:6px 2px 0">Models come from your Claude settings, the same list as the chat model picker. Default = your Claude default.</p></div>`;
}

// ---------- a live kernel for one notebook: session (reused after a reload), a polling loop, and run-with-callback
function useKernel(server, path, nbKernel, post, api, ready) {
  const [k, setK] = useState({ id: "", name: "", sid: "", state: "starting", err: "" }), [specs, setSpecs] = useState([]), [gen, setGen] = useState(0);
  const waiters = useRef({}), kid = useRef("");
  kid.current = k.id;
  useEffect(() => {                                                      // the Running panel shut this kernel down: drop it (Reconnect or a kernel pick starts a new one)
    const on = (e) => { const d = e.detail || {}; if (d.server !== server || !d.kid || d.kid !== kid.current) return;
      if (d.op === "shutdown") { Object.values(waiters.current).forEach((w) => w.done({ status: "error", lost: true, error: "The kernel was shut down" })); waiters.current = {}; setK((x) => ({ ...x, id: "", sid: "", state: "dead", err: "Kernel shut down" })); } };
    addEventListener("nbj-kernel", on); return () => removeEventListener("nbj-kernel", on);
  }, [server]);
  useEffect(() => {
    if (!ready) return;
    let off = false;
    setK({ id: "", name: "", sid: "", state: "starting", err: "" });
    (async () => {
      try {
        const sp = await post("jupyter/kernelspecs", { server }); if (off) return; setSpecs(sp.kernelspecs);
        const want = nbKernel && sp.kernelspecs.some((s) => s.name === nbKernel) ? nbKernel : sp.default;
        const s = await post("jupyter/session", { server, path, kernel: want }); if (off) return;
        setK({ id: s.kernel.id, name: s.kernel.name, sid: s.id, state: "idle", err: "" });
      } catch (e) { if (!off) setK((x) => ({ ...x, state: "dead", err: e.message })); }
    })();
    return () => { off = true; };
  }, [server, path, ready, gen]);
  useEffect(() => {
    if (!k.id) return;
    let stop = false, after = 0, chid = 0;
    (async () => {
      while (!stop) {
        try {
          const r = await api(`jupyter/poll?server=${enc(server)}&kind=k&id=${enc(k.id)}&after=${after}&wait=15`);
          if (stop) return;
          if (chid && r.ch !== chid) { chid = r.ch; after = 0; Object.values(waiters.current).forEach((w) => w.done({ status: "error", lost: true })); waiters.current = {}; continue; }
          chid = r.ch;
          for (const e of r.events) {
            after = e.n;
            if (e.t === "status" && e.c) setK((x) => (x.state === "dead" ? x : { ...x, state: e.c.execution_state === "busy" ? "busy" : "idle" }));
            if (widgetEvent(e)) continue;                                       // ipywidgets (tqdm.notebook bars ...): models live in notebook.js
            const w = waiters.current[e.p];
            // done = execute_reply (shell) AND status idle (iopub). They come on separate channels, so the reply often
            // arrives before the last outputs: finishing on the reply alone dropped them. Same rule as the server's exec_wait.
            if (!w) continue;
            if (e.t === "execute_reply") { w.reply = e.c; if (w.idle) w.done(e.c); else if (!w.t) w.t = setTimeout(() => w.done(w.reply), 3000); }
            else if (e.t === "status") { if (e.c && e.c.execution_state === "idle") { w.idle = true; if (w.reply) w.done(w.reply); } }
            else w.on(e);
          }
          if (r.closed) { setK((x) => ({ ...x, state: "dead", err: "Disconnected from the kernel." })); await sleep(2500); setK((x) => (x.state === "dead" && !x.err.startsWith("Disconnected") ? x : { ...x, state: "idle", err: "" })); }
        } catch (e) { if (stop) return; setK((x) => ({ ...x, state: "dead", err: e.message })); await sleep(3000); }
      }
    })();
    return () => { stop = true; };
  }, [k.id, server]);
  const exec = useCallback((code, on) => {
    const msg = uid(); let done;
    const p = new Promise((res) => { done = res; });
    const w = { on, reply: null, idle: false, t: 0, fin: false, done: (c) => { if (w.fin) return; w.fin = true; clearTimeout(w.t); if (waiters.current[msg] === w) delete waiters.current[msg]; done(c); } };
    waiters.current[msg] = w;
    post("jupyter/kernel/execute", { server, kid: kid.current, code, msg }).catch((e) => waiters.current[msg] && waiters.current[msg].done({ status: "error", lost: true, error: e.message }));
    return p;
  }, [server]);
  const op = (o) => post("jupyter/kernel/op", { server, kid: k.id, op: o });
  const switchTo = async (name) => {
    const was = k.state; setK((x) => ({ ...x, state: "starting" }));
    try { const s = await post("jupyter/session", k.sid ? { server, switch: k.sid, kernel: name } : { server, path, kernel: name }); setK({ id: s.kernel.id, name: s.kernel.name, sid: s.id, state: "idle", err: "" }); }
    catch (e) { setK((x) => ({ ...x, state: was })); toast(e.message); }
  };
  const complete = (code, pos) => post("jupyter/kernel/complete", { server, kid: kid.current, code, pos });
  const signature = (code, pos) => post("jupyter/kernel/inspect", { server, kid: kid.current, code, pos });
  const live = () => (kid.current && k.state === "idle" ? { server, kid: kid.current } : {});
  return { k, specs, exec, complete, signature, live, interrupt: () => op("interrupt"), restart: () => op("restart"), switchTo, reconnect: () => setGen((g) => g + 1), reply: (value) => post("jupyter/kernel/op", { server, kid: k.id, op: "input", value }) };
}

// ---------- AI bar under a cell: write / fix, the proposal is shown first
function AiBar({ c, sql, conns, post, ctx, lang, apply, close, failed }) {
  const [q, setQ] = useState(""), [busy, setBusy] = useState(false), [prop, setProp] = useState(null);
  const kind = sql ? "sql" : "code";
  const go = guard(async (fix) => {
    if (!fix && !q.trim()) return toast("Describe what you want");
    setBusy(true); setProp(null);
    try {
      const body = { code: c.src, lang, cells: ctx(), kind, conn: sql ? sql.conn : "" };
      const r = fix ? await post("jupyter/ai/fix", { ...body, error: errText(c) }) : await post("jupyter/ai/write", { ...body, ask: q });
      if (!r.code) toast(r.note || "No code came back"); else setProp(r);
    } finally { setBusy(false); }
  });
  return html`<div class="nbj-ai"><div class="row" style="gap:6px">${Ic("sparkles", 15)}
      <input class="grow" autofocus placeholder=${c.src.trim() ? "Ask AI to change this cell…" : "Ask AI to write this cell, e.g. plot revenue per month"} value=${q} disabled=${busy} onInput=${(e) => setQ(e.target.value)}
        onKeyDown=${(e) => { if (e.key === "Enter" && !e.shiftKey) go(false); else if (e.key === "Escape") close(); }} aria-label="Ask AI"/>
      <button class="btn acc" disabled=${busy} onClick=${() => go(false)}>${busy ? html`<i class="spin"></i>` : Ic("sparkles", 13)} Write</button>
      ${failed && html`<button class="btn" disabled=${busy} onClick=${() => go(true)}>${Ic("bolt", 13)} Fix error</button>`}
      <button class="ibtn" aria-label="Close" onClick=${close}>${Ic("x", 14)}</button></div>
    ${prop && html`<div class="nbj-prop"><pre>${prop.code}</pre>${prop.note && html`<div class="mute small">${prop.note}</div>`}
      <div class="row" style="gap:6px"><button class="btn acc sm" onClick=${() => { apply(prop, "replace"); setProp(null); setQ(""); close(); }}>${Ic("check", 12)} Replace cell</button>
        <button class="btn sm" onClick=${() => { apply(prop, "below"); setProp(null); }}>Insert below</button><button class="btn sm" onClick=${() => setProp(null)}>Discard</button><span class="grow"></span>
        <span class="mute small">${prop.model || ""}</span></div></div>`}</div>`;
}

// ---------- one cell. Memoized: typing in a cell, or output streaming into it, re-renders that cell only. Handlers come through `fx`
// (one object, refreshed by the notebook each render), so a cell never holds a stale closure and its props stay comparable.
function memo(C, same) {
  class M extends Component { shouldComponentUpdate(n) { return !same(this.props, n); } render(p) { return h(C, p); } }
  return M;
}
const sameCell = (a, b) => { for (const k in b) if (k === "marks" ? JSON.stringify(a.marks) !== JSON.stringify(b.marks) : a[k] !== b[k]) return false; return true; };
const CELL_TYPE = (x) => (sqlOf(x) ? "sql" : x.type);
const Cell = memo(function Cell({ c, i, last, sel, running, aiOpen, marks, lang, kOk, sugg, conns, fx }) {
  const F = () => fx.current, sql = sqlOf(c), failed = !!errText(c), ec = c.raw && c.raw.execution_count, out = hasOut(c);
  const connId = sql ? (conns.find((q) => q.name === sql.conn) || {}).id || "" : "";
  const adder = html`<div class="nb-add"><button type="button" onClick=${() => F().insert(i + 1, "code", true)}>${Ic("plus", 12)} Code</button><button type="button" onClick=${() => F().insert(i + 1, "markdown")}>${Ic("plus", 12)} Text</button><button type="button" onClick=${() => F().insert(i + 1, "sql", true)}>${Ic("plus", 12)} SQL</button></div>`;
  return html`<div class="nb-cw"><div id=${"nbc-" + c.key} class=${"nb-cell " + c.type + (sql ? " sql" : "") + (sel ? " sel" : "") + (running ? " run" : "")} onPointerDown=${() => F().setSel(c.key)} onContextMenu=${(e) => F().openCtx(c, e)}>
    <div class="nb-gut">${c.type === "code" ? html`<button class=${"nbj-run" + (running ? " busy" : "")} title=${running ? "Running…" : "Run cell (Shift+Enter)"} aria-label="Run cell" disabled=${running} onClick=${(e) => { e.stopPropagation(); F().runCell(c.key); }}>${running ? html`<i class="spin"></i>` : Ic("play", 12)}</button><span class="mono nbj-n">${running ? "[*]" : sql ? "sql" : ec ? "[" + ec + "]" : "[ ]"}</span>` : ""}</div>
    <div class="nb-body">
      <div class="nb-tools">
        <select aria-label="Cell type" value=${CELL_TYPE(c)} onChange=${(e) => F().retype(c.key, e.target.value)}><option value="code">Code</option><option value="sql">SQL</option><option value="markdown">Text</option><option value="raw">Raw</option></select>
        <button class="ibtn sm" title="Move up" aria-label="Move up" disabled=${!i} onClick=${() => F().move(i, -1)}>${Ic("up", 13)}</button><button class="ibtn sm" title="Move down" aria-label="Move down" disabled=${last} onClick=${() => F().move(i, 1)}>${Ic("down", 13)}</button>
        <button class="ibtn sm" title="More: run, output, AI, convert" aria-label="More cell actions" onClick=${(e) => { e.stopPropagation(); F().openCtx(c, e, e.currentTarget); }}>${Ic("more", 13)}</button>
        <button class="ibtn sm bad" title="Delete cell" aria-label="Delete cell" onClick=${() => F().remove(c.key)}>${Ic("trash", 13)}</button></div>
      ${sql && html`<div class="nbj-sqlbar"><b>SQL</b>
        <select value=${sql.conn} onChange=${(e) => F().setSql(c.key, { conn: e.target.value })} aria-label="Connection"><option value="">Connection…</option>${conns.map((q) => html`<option value=${q.name}>${q.name}</option>`)}</select>
        <label class="mute small">into <input class="nbj-var" placeholder="no variable" value=${sql.var || ""} onInput=${(e) => F().setSql(c.key, { var: e.target.value.replace(/[^A-Za-z0-9_]/g, "") })} aria-label="Python variable"/></label>
        <label class="mute small">rows <select value=${sql.limit || 10000} onChange=${(e) => F().setSql(c.key, { limit: +e.target.value })}>${[100, 1000, 10000, 50000, 200000].map((n) => html`<option value=${n}>${n.toLocaleString()}</option>`)}</select></label>
        ${!conns.length && html`<span class="mute small">Add a database in Data Studio first.</span>`}</div>`}
      ${c.type === "markdown" && !sel ? html`<div class="nb-md" title="Click to edit" onClick=${() => F().setSel(c.key)}>${c.src.trim() ? html`<${Md} text=${c.src}/>` : html`<span class="mute small">Empty text cell. Click to write.</span>`}</div>`
        : html`<${CodeBox} value=${c.src} lang=${sql ? "sql" : c.type === "code" ? lang : c.type === "markdown" ? "markdown" : ""} autofocus=${sel && !c.src}
            onChange=${(v) => F().upd(c.key, { src: v })} onKey=${(e) => F().key(c.key, e)} delay=${c.type === "code" && !sql ? sugg : 0}
            intel=${c.type === "code"} marks=${marks} onAi=${(s2, m) => F().setAi({ s: s2, m: m || "" })} words=${sql ? SQL_WORDS : F().wordsFor(c.key)}
            complete=${sql ? sqlComplete(F().post, connId) : kOk ? (code, pos) => F().kern.complete(code, pos) : null}
            signature=${c.type !== "code" ? null : sql ? sqlSignature : kOk ? (code, pos) => F().kern.signature(code, pos) : null}
            suggest=${(prefix, suffix) => F().post("jupyter/ai/suggest", { prefix, suffix, lang, cells: F().ctxFor(c.key)(), ...F().kern.live() }).then((r) => r.text || "")}/>`}
      ${aiOpen && html`<${AiBar} c=${c} sql=${sql ? { ...sql, conn: connId || sql.conn } : null} conns=${conns} post=${F().post} ctx=${F().ctxFor(c.key)} lang=${lang} apply=${F().aiApply(c, i)} close=${() => F().setAiOpen(null)} failed=${failed}/>`}
      ${out && hiddenOf(c) && html`<button class="nbj-ohid" title="Show this cell's output" onClick=${() => F().upd(c.key, (x) => ({ raw: withHidden(x, false) }))}>${Ic("eye", 12)} Output hidden${c.outputs.length > 1 ? " (" + c.outputs.length + ")" : ""}. Show</button>`}
      ${out && !hiddenOf(c) && html`<div class="nbj-ow">
      <${OutBox} scroll=${scrollOf(c)} running=${running}>
        ${c.sqlres && c.sqlres.ok && c.sqlres.columns && c.sqlres.columns.length ? html`<${DataView} res=${c.sqlres} spec=${vizOf(c, "sql")} onSpec=${(s) => F().upd(c.key, (x) => ({ raw: withViz(x, "sql", s) }))} table=${html`<${Grid} res=${c.sqlres} name=${(sql && sql.conn) || "result"}/>`}/>${c.sqlres.assigned && html`<div class=${"small " + (c.sqlres.assigned.ok ? "mute" : "nbj-bad")}>${c.sqlres.assigned.ok ? "Also in the kernel as DataFrame " : "Could not assign "}<code>${c.sqlres.assigned.var}</code>${c.sqlres.assigned.error ? ": " + c.sqlres.assigned.error : ""}</div>`}`
          : c.sqlres && !c.sqlres.ok ? html`<pre class="nb-o err">${c.sqlres.error}</pre>`
          : c.outputs.map((o, j) => html`<${Out} key=${j} o=${o} post=${F().post} viz=${vizOf(c, String(j))} onViz=${(s) => F().upd(c.key, (x) => ({ raw: withViz(x, String(j), s) }))}/>`)}
        ${failed && !aiOpen && !running && html`<div><button class="btn sm" onClick=${() => F().setAiOpen(c.key)}>Fix with AI</button></div>`}<//></div>`}
    </div></div>${adder}</div>`;
}, sameCell);

// the notebook as a .py file in the percent format (Jupytext, VS Code, Spyder): `# %%` code cells, `# %% [markdown]` text cells as comments
const toPy = (cells) => cells.map((c) => { const q = (s) => s.split("\n").map((l) => (l ? "# " + l : "#")).join("\n"), s = sqlOf(c);
  return c.type === "code" && !s ? "# %%\n" + c.src : c.type === "code" ? "# %% [sql] " + (s.conn || "") + (s.var ? " -> " + s.var : "") + "\n" + q(c.src) : "# %% [" + c.type + "]\n" + q(c.src); }).join("\n\n") + "\n";
export const saveBlob = (blob, name) => { const url = URL.createObjectURL(blob), a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000); };

// ---------- the notebook
function Notebook({ server, path, post, api, conns, settings, models, dirtyRef, onExpose, onOutline, onFileMenu, jump }) {
  const [cells, setCells] = useState(null), [dirty, setDirty] = useState(false), [err, setErr] = useState(""), [sel, setSel] = useState(null), [run, setRun] = useState({}), [aiOpen, setAiOpen] = useState(null);
  const nbRef = useRef(null), cellsRef = useRef([]), dids = useRef({}), runAll = useRef(false), fx = useRef(null);
  cellsRef.current = cells || [];
  const nbKernel = nbRef.current && ((nbRef.current.metadata || {}).kernelspec || {}).name;
  const kern = useKernel(server, path, nbKernel || "", post, api, !!cells);
  dirtyRef.current = dirty;
  useEffect(() => () => { dirtyRef.current = false; }, []);
  useEffect(() => { if (!dirty) return; const b = (e) => { e.preventDefault(); e.returnValue = ""; }; addEventListener("beforeunload", b); return () => removeEventListener("beforeunload", b); }, [dirty]);
  const load = () => post("jupyter/file/get", { server, path }).then((r) => {
    try { const nb = typeof r.content === "string" ? parseNotebook(r.content) : r.content; if (!nb || !Array.isArray(nb.cells)) throw new Error("not a notebook"); nbRef.current = nb; setCells(cellsOf(nb)); setDirty(false); setErr(""); }
    catch (e) { setErr("Cannot read this notebook: " + e.message); }
  }).catch((e) => setErr(e.message));
  useEffect(() => { setCells(null); load(); }, [server, path]);
  const spec = kern.specs.find((s) => s.name === kern.k.name), lang = ((spec && spec.language) || "python").toLowerCase();
  const upd = (key, f) => { setDirty(true); setCells((cs) => cs.map((c) => (c.key === key ? { ...c, ...(typeof f === "function" ? f(c) : f) } : c))); };
  const newSql = (x) => withSql(x, { conn: (conns[0] || {}).name || "", var: "df", limit: 10000 });
  const insert = (at, type, focus, extra = {}) => { const c = { key: uid(), type: type === "sql" ? "code" : type, src: "", outputs: [], raw: null, ...extra }; if (type === "sql") c.raw = newSql(c); setDirty(true); setCells((cs) => [...cs.slice(0, at), c, ...cs.slice(at)]); setSel(c.key); if (focus) focusCell(c.key); return c; };
  const move = (i, by) => { setDirty(true); setCells((cs) => { const j = i + by; if (j < 0 || j >= cs.length) return cs; const n = [...cs]; [n[i], n[j]] = [n[j], n[i]]; return n; }); };
  const remove = (key) => { setDirty(true); setCells((cs) => cs.filter((c) => c.key !== key)); };
  const retype = (key, t) => upd(key, (x) => (t === "sql" ? { type: "code", raw: sqlOf(x) ? x.raw : newSql(x) } : { type: t, raw: sqlOf(x) ? withSql(x, null) : x.raw }));
  const setSql = (key, patch) => upd(key, (x) => ({ raw: withSql(x, { ...sqlOf(x), ...patch }) }));
  const wordsFor = (key) => () => {                          // names used in this notebook, nearest cells first (cheap: only runs while you type a word)
    const out = new Set(KW), cs = cellsRef.current, i = cs.findIndex((c) => c.key === key);
    for (let d = 0; d < cs.length; d++) for (const j of [i - d, i + d]) { const c = cs[j]; if (c && c.type === "code") for (const w of c.src.match(IDENT) || []) out.add(w); if (out.size > 600) return out; }
    return out;
  };
  const ctxFor = (key) => () => { const i = cellsRef.current.findIndex((c) => c.key === key); return cellsRef.current.slice(Math.max(0, i - 8), i < 0 ? 0 : i).map((c) => ({ type: sqlOf(c) ? "sql" : c.type, src: c.src.slice(0, 1500), out: outText(c) })); };

  const runCell = async (key) => {
    const c = cellsRef.current.find((x) => x.key === key);
    if (!c || c.type !== "code") return true;
    if (!kern.k.id || kern.k.state === "dead") { toast(kern.k.err || "The kernel is not ready"); return false; }
    if (!c.src.trim()) return true;
    const sql = sqlOf(c);
    setRun((r) => ({ ...r, [key]: 1 }));
    upd(key, (x) => ({ outputs: [], sqlres: null, cleared: false, raw: { ...(hiddenOf(x) ? withHidden(x, false) : x.raw || {}), execution_count: null } }));   // running shows the output again
    // kernel events are folded into the cell once per frame, not once per message: a chatty loop (prints, progress bars) stays one render a frame
    // (a hidden tab gets no animation frames: a timer then, so a long run in the background does not pile up its output)
    const q = [], d = {}; let raf = 0;
    const later = () => (document.hidden ? { t: setTimeout(flush, 250) } : { r: requestAnimationFrame(flush) }), unlater = (h) => { if (h && h.r) cancelAnimationFrame(h.r); if (h && h.t) clearTimeout(h.t); };
    const flush = () => { raf = 0; if (!q.length) return; const evs = q.splice(0); upd(key, (x) => ({ outputs: evs.reduce((o, e) => fold(o, d, e), x.outputs) })); };
    try {
      if (sql) {
        if (!sql.conn) { toast("Pick a SQL connection for this cell"); return false; }
        const r = await post("jupyter/sql/run", { server, kid: kern.k.id, sql: c.src, conn: sql.conn, var: sql.var || "", limit: sql.limit || 10000 });
        const a = r.assigned, sum = r.ok ? `${(r.rows || []).length} row${(r.rows || []).length === 1 ? "" : "s"} × ${(r.columns || []).length} columns${a && a.ok ? " → DataFrame " + a.var : ""}` : "SQL failed";
        upd(key, { sqlres: r, outputs: r.ok ? [{ output_type: "display_data", data: { "text/plain": sum }, metadata: {} }] : [] });
        return !!r.ok && (!a || a.ok);
      }
      const reply = await kern.exec(c.src, (e) => {
        if (e.t === "input_request") { flush(); const v = window.prompt(e.c.prompt || "Input", ""); kern.reply(v == null ? "" : v).catch(() => {}); return; }
        q.push(e); if (!raf) raf = later();
      });
      unlater(raf); flush();
      if (reply.lost) { toast(reply.error || "Lost the connection to the kernel"); return false; }
      upd(key, (x) => ({ outputs: widgetSnap(x.outputs), raw: { ...(x.raw || {}), execution_count: reply.execution_count == null ? null : reply.execution_count } }));
      return reply.status !== "error";
    } catch (e) { toast(e.message); return false; }
    finally { unlater(raf); flush(); setRun((r) => { const n = { ...r }; delete n[key]; return n; }); }
  };
  const codeKeys = (cs) => cs.filter((c) => c.type === "code").map((c) => c.key);
  const runMany = async (keys) => { runAll.current = true; for (const k of keys) { if (!runAll.current) break; if (!(await runCell(k))) break; } runAll.current = false; };
  const interrupt = guard(async () => { runAll.current = false; await kern.interrupt(); });
  const restart = guard(async (thenAll) => {
    if (!(await confirmBox({ title: "Restart the kernel?", text: "All variables are lost.", ok: "Restart" }))) return;
    runAll.current = false; await kern.restart(); toast("Kernel restarting"); if (thenAll) setTimeout(() => runMany(codeKeys(cellsRef.current)), 1500);
  });
  const persist = async () => {
    const nb = nbRef.current, snap = cellsRef.current;
    if (kern.k.name && kern.specs.length) { const sp = kern.specs.find((x) => x.name === kern.k.name); if (sp) nb.metadata = { ...(nb.metadata || {}), kernelspec: { name: sp.name, display_name: sp.display, language: sp.language } }; }
    await post("jupyter/file/put", { server, path, type: "notebook", content: JSON.parse(serializeNotebook(nb, snap)) });
    if (cellsRef.current === snap) setDirty(false);                      // edited while saving: stay dirty, the next round saves it
  };
  const save = guard(async () => { await persist(); toast("Saved " + path.split("/").pop()); });
  const clearOut = (key) => upd(key, { outputs: [], sqlres: null, cleared: true });
  const allOut = (f) => { setDirty(true); setCells((cs) => cs.map((c) => (c.type === "code" ? { ...c, ...f(c) } : c))); };
  const clearAll = guard(async () => { if (await confirmBox({ title: "Clear all outputs?", text: "Every cell's output is removed (a smaller file, clean diffs). Variables in the kernel stay; outputs come back when cells run again.", ok: "Clear" })) allOut(() => ({ outputs: [], sqlres: null, cleared: true })); });
  const [ctx, setCtx] = useState(null);
  const openCtx = (c, e, btnEl) => {                                       // right-click on a cell (or its ⋯ button); the editor and Shift+right-click keep the browser's menu
    if (!btnEl && (e.shiftKey || e.target.closest("textarea,input,select,a,.nbj-ctx"))) return;
    e.preventDefault(); setSel(c.key); setCtx({ key: c.key, at: btnEl ? menuAt(null, btnEl) : { x: e.clientX, y: e.clientY } });
  };
  const ctxItems = (key) => {
    const cs = cellsRef.current, i = cs.findIndex((x) => x.key === key), c = cs[i]; if (!c) return [];
    const code = c.type === "code", out = hasOut(c), any = cs.some(hasOut), mod = isMacJ ? "⌘" : "Ctrl";
    return [
      code && ["Run cell", "play", mod + "+Enter", !!run[c.key], () => runCell(c.key)],
      code && ["Run this and below", "down", "", busy, () => runMany(codeKeys(cs.slice(i)))],
      code && "-",
      code && ["Clear output", "x", "", !out, () => clearOut(c.key)],
      code && [hiddenOf(c) ? "Show output" : "Hide output", "eye", "", !out, () => upd(c.key, (x) => ({ raw: withHidden(x, !hiddenOf(x)) }))],
      code && [scrollOf(c) ? "Expand output (full height)" : "Scroll output in a box", scrollOf(c) ? "expand" : "shrink", "", !out, () => upd(c.key, (x) => ({ raw: withScroll(x, !scrollOf(x)) }))],
      code && ["Clear all outputs", "trash", "", !any, clearAll],
      "-",
      ["Insert code cell above", "up", "", false, () => insert(i, "code", true)],
      ["Insert code cell below", "plus", "", false, () => insert(i + 1, "code", true)],
      ["Insert text cell below", "note", "", false, () => insert(i + 1, "markdown")],
      ["Ask AI about this cell", "sparkles", "", false, () => setAiOpen(c.key)],
      ["Copy cell source", "copy", "", !c.src, () => navigator.clipboard.writeText(c.src).then(() => toast("Copied"), () => toast("Could not copy"))],
      "-",
      c.type !== "code" || sqlOf(c) ? ["Convert to code", "code", "", false, () => retype(c.key, "code")] : ["Convert to text", "note", "", false, () => retype(c.key, "markdown")],
      code && !sqlOf(c) && ["Convert to SQL", "database", "", false, () => retype(c.key, "sql")],
      ["Move up", "up", "", !i, () => move(i, -1)],
      ["Move down", "down", "", i === cs.length - 1, () => move(i, 1)],
      "-",
      ["Delete cell", "trash", "", false, () => remove(c.key), true],
    ];
  };
  const [addMenu, setAddMenu] = useState(false), [runMenu, setRunMenu] = useState(false), [outMenu, setOutMenu] = useState(false), [fnd, setFnd] = useState(null), [ai, setAi] = useState({ s: "", m: "" }), findIn = useRef();
  const findRe = (global = true) => { if (!fnd || !fnd.q) return null; try { return new RegExp(fnd.rx ? fnd.q : fnd.q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), (global ? "g" : "") + (fnd.cs ? "" : "i") + "m"); } catch (e) { return null; } };
  const matches = useMemo(() => {
    const re = findRe(); if (!re || !cells) return [];
    const out = []; for (const c of cells) { re.lastIndex = 0; let m; while ((m = re.exec(c.src)) && out.length < 3000) { if (!m[0]) { re.lastIndex++; continue; } out.push({ key: c.key, start: m.index, end: m.index + m[0].length, text: m[0] }); } }
    return out;
  }, [fnd && fnd.q, fnd && fnd.cs, fnd && fnd.rx, cells]);
  const goto = (i) => { if (!matches.length) return; const n = (i + matches.length) % matches.length; setFnd((f) => ({ ...f, i: n })); const m = matches[n]; setSel(m.key); requestAnimationFrame(() => { const el = document.getElementById("nbc-" + m.key); if (el) el.scrollIntoView({ block: "center", behavior: "smooth" }); }); };
  useEffect(() => { if (fnd && matches.length) goto(Math.min(fnd.i || 0, matches.length - 1)); }, [fnd && fnd.q, fnd && fnd.cs, fnd && fnd.rx]);
  const replaceOne = () => { const m = matches[fnd.i]; if (!m) return; const re = findRe(false), to = fnd.rx && re ? m.text.replace(re, fnd.r) : fnd.r; upd(m.key, (x) => ({ src: x.src.slice(0, m.start) + to + x.src.slice(m.end) })); };
  const replaceAll = () => { const re = findRe(); if (!re || !matches.length) return; const keys = new Set(matches.map((m) => m.key)); setDirty(true); setCells((cs) => cs.map((c) => (keys.has(c.key) ? { ...c, src: c.src.replace(re, fnd.rx ? fnd.r : () => fnd.r) } : c))); toast(matches.length + " replaced"); };
  const marksOf = (k) => (fnd && matches.length ? matches.filter((m) => m.key === k).map((m) => [m.start, m.end, m === matches[fnd.i]]) : null);
  useEffect(() => {                                                      // the headings for the Contents panel: after a pause in typing, not on every key
    if (!onOutline || !cells) return;
    const t = setTimeout(() => onOutline(path, cells.flatMap((c) => (c.type === "markdown" ? c.src.split("\n").filter(((fence) => (l) => { if (/^\s*(```|~~~)/.test(l)) fence = !fence; return !fence && /^#{1,6}\s+\S/.test(l); })(false)).map((l) => ({ key: c.key, level: /^#+/.exec(l)[0].length, text: l.replace(/^#+\s+/, "").replace(/[*_`]/g, "").trim() })) : []))), 300);
    return () => clearTimeout(t);
  }, [cells]);
  const addAfter = (t) => { const cs = cellsRef.current, i = cs.findIndex((c) => c.key === sel), at = i < 0 ? cs.length : i + 1; const c = insert(at, t === "ai" ? "code" : t, t === "code" || t === "sql"); if (t === "ai") setAiOpen(c.key); };
  useEffect(() => { if (!jump || jump.path !== path) return; requestAnimationFrame(() => { const el = document.getElementById("nbc-" + jump.key); if (el) el.scrollIntoView({ block: "start", behavior: "smooth" }); }); }, [jump]);
  const saving = useRef(false), [auto, setAuto] = useState(() => LS.get("auto", true)), [busySave, setBusySave] = useState(false), [saveErr, setSaveErr] = useState("");
  const persistRef = useRef(persist); persistRef.current = persist;
  const flush = async () => { if (saving.current) return; saving.current = true; setBusySave(true); try { await persistRef.current(); setSaveErr(""); } catch (e) { setSaveErr(e.message); } saving.current = false; setBusySave(false); };
  useEffect(() => {                                                      // auto-save: 1.5 s after the last change, when the tab is hidden, and when you leave
    if (!dirty || !auto) return;
    const t = setTimeout(flush, 1500), hide = () => { if (document.visibilityState === "hidden") flush(); };
    document.addEventListener("visibilitychange", hide);
    return () => { clearTimeout(t); document.removeEventListener("visibilitychange", hide); };
  }, [dirty, auto, cells]);
  const openFind = () => { setFnd((f) => f || { q: "", r: "", cs: false, rx: false, i: 0 }); setTimeout(() => findIn.current && findIn.current.focus() && findIn.current.select(), 30); };
  const aiApply = (c, i) => (prop, mode) => {
    if (mode === "replace") upd(c.key, (x) => ({ src: prop.code, raw: sqlOf(x) && prop.conn ? withSql(x, { ...sqlOf(x), conn: (conns.find((q) => q.id === prop.conn) || {}).name || sqlOf(x).conn }) : x.raw }));
    else insert(i + 1, sqlOf(c) ? "sql" : "code", false, { src: prop.code });
  };
  const focusCell = (k) => setTimeout(() => { const t = document.querySelector("#nbc-" + k + " textarea"); if (t) { t.focus(); t.selectionStart = t.selectionEnd = t.value.length; } const el = document.getElementById("nbc-" + k); if (el) el.scrollIntoView({ block: "nearest" }); }, 40);
  const key = (k, e) => {
    const cs = cellsRef.current, i = cs.findIndex((x) => x.key === k), c = cs[i]; if (!c) return;
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
    else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") { e.preventDefault(); openFind(); }
    else if (e.shiftKey && e.key === "Enter") {                       // run, then land in the next cell ready to type (a new one after the last)
      e.preventDefault(); if (c.type === "code") runCell(c.key);
      if (i === cs.length - 1) insert(i + 1, "code", true); else { const nk = cs[i + 1].key; setSel(nk); focusCell(nk); }
    }
    else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); runCell(c.key); }
    else if (e.key === "Escape" && c.type === "markdown") { e.preventDefault(); setSel(null); }
  };
  const fileExtra = () => [["Save", "check", isMacJ ? "⌘S" : "Ctrl+S", !dirty, save], ["Revert to saved", "refresh", "", !dirty, () => { setCells(null); load(); }],
    ["Download as Python (.py)", "download", "", !cellsRef.current.length, () => saveBlob(new Blob([toPy(cellsRef.current)], { type: "text/x-python" }), path.split("/").pop().replace(/\.ipynb$/i, "") + ".py")], "-"];
  fx.current = { upd, insert, move, remove, retype, setSql, runCell, setSel, setAiOpen, setAi, openCtx, aiApply, ctxFor, wordsFor, key, kern, post };
  if (err) return html`<div class="empty"><div>${Ic("alert", 24)}<div>${err}</div></div></div>`;
  if (!cells) return html`<div class="empty"><div class="shine">Opening…</div></div>`;
  const st = kern.k.state, busy = Object.keys(run).length > 0, kOk = !!kern.k.id && st !== "dead", sugg = settings.suggest.on ? settings.suggest.delay : 0;
  const stateTag = st === "dead" ? html`<button class="nbj-kst bad" title=${(kern.k.err ? kern.k.err + ". " : "") + "Click to connect a kernel again"} onClick=${kern.reconnect}>${Ic("alert", 11)} Offline. Reconnect</button>`
    : html`<span class=${"nbj-kst " + st} title=${kern.k.err || ""}><i></i>${st === "starting" ? "Starting" : st === "busy" ? "Busy" : "Idle"}</span>`;
  const opts = (list, close) => html`<div class="optlist">${list.map(([n, i, d, off, f]) => html`<button class="opt" disabled=${off} onClick=${() => { close(); f(); }}><span class="oi">${Ic(i, 16)}</span><span class="grow"><b>${n}</b>${d && html`<span class="d">${d}</span>`}</span></button>`)}</div>`;
  const s = cells.find((c) => c.key === sel), so = s && hasOut(s), anyOut = cells.some(hasOut), selAt = cells.findIndex((c) => c.key === sel);
  const saveLabel = saveErr ? "Save failed" : busySave ? "Saving…" : dirty ? (auto ? "Saving soon" : "Save") : "Saved";
  return html`<div class="nb nbj" onKeyDown=${(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") { e.preventDefault(); openFind(); } }}>
    <div class="nbj-bar nbj-colab">
      <div class="nbj-split"><button class="btn sm ghost" title="Add a code cell after the selected one" onClick=${() => addAfter("code")}>${Ic("plus", 14)} Code</button>
        <${Pop} open=${addMenu} setOpen=${setAddMenu} up=${false} width=${230} button=${html`<button class="btn sm ghost nbj-caret" aria-label="More cell types" onClick=${() => setAddMenu(!addMenu)}>${Ic("down", 12)}</button>`}>
          ${opts([["Code cell", "code", "Python or the kernel's language", false, () => addAfter("code")], ["Text cell", "note", "Markdown", false, () => addAfter("markdown")], ["SQL cell", "database", "Query a database into a DataFrame", false, () => addAfter("sql")], ["Cell written by AI", "sparkles", "Describe it, review the code", false, () => addAfter("ai")]], () => setAddMenu(false))}<//></div>
      <button class="btn sm ghost" title="Add a text cell after the selected one" onClick=${() => addAfter("markdown")}>${Ic("plus", 14)} Text</button><span class="nbj-vr"></span>
      <div class="nbj-split"><button class="btn sm ghost" disabled=${busy} title="Run every code cell" onClick=${() => runMany(codeKeys(cells))}>${Ic("play", 13)} Run all</button>
        <${Pop} open=${runMenu} setOpen=${setRunMenu} up=${false} width=${250} button=${html`<button class="btn sm ghost nbj-caret" aria-label="More run options" onClick=${() => setRunMenu(!runMenu)}>${Ic("down", 12)}</button>`}>
          ${opts([["Run selected cell", "play", "Shift+Enter", !sel || busy, () => sel && runCell(sel)], ["Run selected and below", "down", "", !sel || busy, () => runMany(codeKeys(cells.slice(selAt)))], ["Run all above", "up", "", !sel || busy, () => runMany(codeKeys(cells.slice(0, selAt)))], "-", ["Restart kernel", "refresh", "Clears all variables", false, () => restart(false)], ["Restart and run all", "refresh", "", false, () => restart(true)]].filter((x) => x !== "-"), () => setRunMenu(false))}<//></div>
      ${busy && html`<button class="btn sm ghost bad" onClick=${interrupt}>${Ic("stop", 12)} Interrupt</button>`}
      <${Pop} open=${outMenu} setOpen=${setOutMenu} up=${false} width=${260} button=${html`<button class="btn sm ghost" title="Clear, hide or fold cell outputs" onClick=${() => setOutMenu(!outMenu)}>Output ${Ic("down", 11)}</button>`}>
        ${opts([["Clear selected output", "x", "", !so, () => clearOut(sel)], ["Clear all outputs", "trash", "", !anyOut, clearAll],
          [s && hiddenOf(s) ? "Show selected output" : "Hide selected output", "eye", "", !so, () => upd(sel, (x) => ({ raw: withHidden(x, !hiddenOf(x)) }))],
          ["Hide all outputs", "eye", "", !anyOut, () => allOut((c) => ({ raw: withHidden(c, true) }))], ["Show all outputs", "eye", "", !cells.some(hiddenOf), () => allOut((c) => ({ raw: withHidden(c, false) }))],
          ["Scroll long outputs", "shrink", "Each in a box with its own scroll", false, () => allOut((c) => ({ raw: withScroll(c, true) }))], ["Expand all outputs", "expand", "Full height", false, () => allOut((c) => ({ raw: withScroll(c, false) }))]], () => setOutMenu(false))}<//>
      <span class="grow"></span>
      <button class="ibtn sm" title="Find and replace (Ctrl/Cmd+F)" aria-label="Find and replace" onClick=${openFind}>${Ic("search", 15)}</button>
      <select class="nbj-ksel" value=${kern.k.name} onChange=${(e) => kern.switchTo(e.target.value)} aria-label="Kernel" disabled=${!kern.specs.length}>${!kern.specs.length && html`<option value="">Kernel…</option>`}${kern.specs.map((x) => html`<option value=${x.name}>${x.display}</option>`)}</select>${stateTag}
      <button class=${"btn sm nbj-save" + (saveErr ? " bad" : "")} disabled=${!dirty && !saveErr} title=${saveErr || (isMacJ ? "⌘S" : "Ctrl+S")} onClick=${save}>${busySave ? html`<i class="spin"></i>` : !dirty && !saveErr ? Ic("check", 12) : ""} ${saveLabel}</button>
      <button class="ibtn sm" title="File: download, copy path, rename, use as a Works node…" aria-label="File actions" onClick=${(e) => onFileMenu(e, path, fileExtra())}>${Ic("more", 16)}</button></div>
    ${fnd && html`<div class="nbj-find" role="search" onKeyDown=${(e) => { if (e.key === "Escape") { e.stopPropagation(); setFnd(null); } }}>
      <div class="nbj-ff"><input ref=${findIn} placeholder="Find" value=${fnd.q} onInput=${(e) => setFnd({ ...fnd, q: e.target.value, i: 0 })} onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); goto(fnd.i + (e.shiftKey ? -1 : 1)); } }} aria-label="Find"/>
        <button class=${"nbj-tg" + (fnd.cs ? " on" : "")} title="Match case" aria-pressed=${fnd.cs} onClick=${() => setFnd({ ...fnd, cs: !fnd.cs })}>Aa</button><button class=${"nbj-tg" + (fnd.rx ? " on" : "")} title="Regular expression" aria-pressed=${fnd.rx} onClick=${() => setFnd({ ...fnd, rx: !fnd.rx })}>.*</button></div>
      <span class=${"small " + (fnd.q && !matches.length ? "nbj-bad" : "mute")}>${!fnd.q ? "" : findRe() === null ? "Invalid pattern" : matches.length ? (fnd.i + 1) + " of " + matches.length : "No results"}</span>
      <button class="ibtn sm" title="Previous (Shift+Enter)" aria-label="Previous match" disabled=${!matches.length} onClick=${() => goto(fnd.i - 1)}>${Ic("up", 14)}</button><button class="ibtn sm" title="Next (Enter)" aria-label="Next match" disabled=${!matches.length} onClick=${() => goto(fnd.i + 1)}>${Ic("down", 14)}</button>
      <div class="nbj-ff"><input placeholder="Replace with" value=${fnd.r} onInput=${(e) => setFnd({ ...fnd, r: e.target.value })} aria-label="Replace with"/></div>
      <button class="btn sm" disabled=${!matches.length} onClick=${replaceOne}>Replace</button><button class="btn sm" disabled=${!matches.length} onClick=${replaceAll}>Replace all</button>
      <span class="grow"></span><button class="ibtn sm" title="Close (Esc)" aria-label="Close find" onClick=${() => setFnd(null)}>${Ic("x", 14)}</button></div>`}
    <div class="nb-cells">${!cells.length ? html`<div class="nbj-emptynb"><p class="mute">This notebook has no cells yet.</p><div class="nb-add"><button type="button" onClick=${() => insert(0, "code", true)}>${Ic("plus", 12)} Code</button><button type="button" onClick=${() => insert(0, "markdown")}>${Ic("plus", 12)} Text</button><button type="button" onClick=${() => insert(0, "sql", true)}>${Ic("plus", 12)} SQL</button></div></div>`
      : cells.map((c, i) => html`<${Cell} key=${c.key} c=${c} i=${i} last=${i === cells.length - 1} sel=${sel === c.key} running=${!!run[c.key]} aiOpen=${aiOpen === c.key} marks=${marksOf(c.key)} lang=${lang} kOk=${kOk} sugg=${sugg} conns=${conns} fx=${fx}/>`)}</div>
    ${ctx && html`<${CtxMenu} at=${ctx.at} items=${ctxItems(ctx.key)} close=${() => setCtx(null)}/>`}
    <div class="ebar"><span class=${saveErr ? "nbj-bad" : ""} title=${saveErr}>${saveErr ? "Auto-save failed: " + saveErr.slice(0, 80) : dirty ? "Unsaved changes" : "All changes saved"}</span><label class="nbj-auto" title="Save changes automatically"><input type="checkbox" checked=${auto} onChange=${(e) => { setAuto(e.target.checked); LS.set("auto", e.target.checked); }}/> Auto-save</label><span>${cells.length} cell${cells.length === 1 ? "" : "s"}</span><span class="grow"></span>
      ${settings.suggest.on && ai.s && ai.s !== "ok" && html`<span class=${ai.s === "err" ? "nbj-bad" : ""} title=${ai.m || "AI suggestions"}>${ai.s === "busy" ? "Suggesting…" : ai.s === "err" ? "Suggestions failed: " + ai.m.slice(0, 60) : ""}</span>`}
      <span class="hint"><kbd>Shift</kbd>+<kbd>Enter</kbd> run and next · right-click a cell for more${settings.suggest.on ? html` · <kbd>Tab</kbd> accepts a suggestion` : ""}</span></div></div>`;
}

// ---------- other files
const previewKind = (p) => { let e = (/\.([A-Za-z0-9]+)$/.exec(p) || [])[1]; e = (e || "").toLowerCase(); return e === "md" || e === "markdown" ? "md" : e === "html" || e === "htm" ? "html" : e === "csv" || e === "tsv" ? "csv" : e === "svg" ? "svg" : ""; };
function parseCsv(t, sep, max) {
  const rows = []; let r = [], f = "", q = false;
  for (let i = 0; i < t.length && rows.length <= max; i++) {
    const c = t[i];
    if (q) { if (c === '"') { if (t[i + 1] === '"') { f += '"'; i++; } else q = false; } else f += c; }
    else if (c === '"') q = true; else if (c === sep) { r.push(f); f = ""; } else if (c === "\n") { r.push(f.replace(/\r$/, "")); rows.push(r); r = []; f = ""; } else f += c;
  }
  if (f || r.length) { r.push(f); rows.push(r); }
  return rows;
}
function CsvView({ text, sep }) {
  const rows = useMemo(() => parseCsv(text, sep, 501), [text, sep]);
  if (!rows.length) return html`<p class="mute" style="padding:16px">This file is empty.</p>`;
  const more = rows.length > 501, head = rows[0], body = rows.slice(1, 501);
  return html`<div class="nb-o html nbj-csv"><table><thead><tr>${head.map((c, i) => html`<th key=${i}>${c}</th>`)}</tr></thead><tbody>${body.map((r, i) => html`<tr key=${i}>${r.map((c, j) => html`<td key=${j}>${c}</td>`)}</tr>`)}</tbody></table>${more && html`<p class="mute small">Showing the first 500 rows. Edit to see the whole file.</p>`}</div>`;
}
function FileView({ server, path, post, dirtyRef, onFileMenu, settings }) {
  const [d, setD] = useState(null), [text, setText] = useState(""), [base, setBase] = useState(""), [mode, setMode] = useState(""), [frame, setFrame] = useState(""), h = useHl(), kind = previewKind(path);
  useEffect(() => { setD(null); post("jupyter/file/get", { server, path }).then((r) => { setD(r); setText(r.format === "text" ? r.content || "" : ""); setBase(r.format === "text" ? r.content || "" : ""); }).catch((e) => setD({ err: e.message })); }, [server, path]);
  const dirty = !!d && d.format === "text" && text !== base;
  const textRef = useRef(text); textRef.current = text;
  // completion without a kernel: the file's own names at once, then the UI's completer (jedi / stdlib members for .py, words otherwise)
  const fileWords = useCallback(() => new Set([...(/\.py$/i.test(path) ? KW : []), ...(textRef.current.length < 400000 ? textRef.current.match(IDENT) || [] : [])]), [path]);
  const fileComplete = useCallback((code, pos) => post("code/complete", { code, pos, path, lang: /\.py$/i.test(path) ? "python" : /\.(sh|bash|zsh)$/i.test(path) ? "sh" : "" }), [path]);
  dirtyRef.current = dirty;
  useEffect(() => () => { dirtyRef.current = false; }, []);
  const wr = (t) => post("jupyter/file/put", { server, path, type: "file", format: "text", content: t }).then(() => setBase(t));
  useEffect(() => { if (!dirty || !LS.get("auto", true)) return; const t = setTimeout(() => { wr(text).catch((e) => toast("Auto-save failed: " + e.message)); }, 1500); return () => clearTimeout(t); }, [text, dirty]);
  useEffect(() => { setMode(""); setFrame(""); }, [path]);
  const view = mode || (kind ? "preview" : "edit");
  useEffect(() => { if (kind === "html" && view === "preview" && d && d.format === "text") post("jupyter/frame", { html: text }).then((r) => setFrame("/nbframe/" + r.id)).catch(() => {}); }, [kind, view, d, base]);
  if (!d) return html`<div class="empty"><div class="shine">Opening…</div></div>`;
  if (d.err) return html`<div class="empty"><div>${Ic("alert", 24)}<div>${d.err}</div></div></div>`;
  const save = guard(async () => { await wr(text); toast("Saved " + path.split("/").pop()); });
  const more = html`<button class="ibtn sm" title="File: download, copy path, rename…" aria-label="File actions" onClick=${(e) => onFileMenu(e, path, d.format === "text" ? [["Save", "check", isMacJ ? "⌘S" : "Ctrl+S", !dirty, save], ["Revert to saved", "refresh", "", !dirty, () => setText(base)], ["Copy contents", "copy", "", !text, () => navigator.clipboard.writeText(text).then(() => toast("Copied"), () => toast("Could not copy"))], "-"] : [])}>${Ic("more", 16)}</button>`;
  const flang = langOf(h, path) || "";
  const bar = (extra) => html`<div class="nbj-pvbar">${extra}<span class="mute small grow sq-ell" title=${path}>${path}</span>${more}</div>`;
  if (d.format === "base64") return html`<div class="nb nbj">${bar("")}${/^image\//.test(d.mimetype || "") ? html`<div class="nbj-img"><img alt=${path} src=${"data:" + d.mimetype + ";base64," + d.content}/></div>` : html`<div class="empty"><div>${Ic("file", 24)}<div>${path.split("/").pop()} is a binary file (${d.mimetype || "unknown type"}). Use the file menu to download it.</div></div></div>`}</div>`;
  const seg = kind ? html`<span class="nbj-pv"><button class=${view === "preview" ? "on" : ""} onClick=${() => setMode("preview")}>${Ic("eye", 13)} Preview</button><button class=${view === "edit" ? "on" : ""} onClick=${() => setMode("edit")}>${Ic("edit", 13)} Edit</button></span>` : "";
  if (kind && view === "preview") return html`<div class="nb nbj">${bar(seg)}<div class="nbj-pvbody">${kind === "md" ? html`<div class="mdfile"><${Md} text=${text}/></div>`
      : kind === "html" ? (frame ? html`<iframe class="nbj-pvframe" sandbox="allow-scripts allow-popups" src=${frame} title=${path}></iframe>` : html`<div class="empty"><div class="shine">Rendering…</div></div>`)
      : kind === "svg" ? html`<div class="nbj-img"><img alt=${path} src=${"data:image/svg+xml;charset=utf-8," + encodeURIComponent(text)}/></div>`
      : html`<${CsvView} text=${text} sep=${/\.tsv$/i.test(path) ? "\t" : ","}/>`}</div></div>`;
  return html`<div class="nb nbj">${bar(seg)}<div class="nb-cells" style="padding:10px 14px"><${CodeBox} value=${text} lang=${flang} onChange=${setText} intel=${true} words=${fileWords} complete=${fileComplete} delay=${settings && settings.suggest.on ? settings.suggest.delay : 0}
    suggest=${(prefix, suffix) => post("jupyter/ai/suggest", { prefix, suffix, lang: flang, cells: [] }).then((r) => r.text || "")} onKey=${(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } }} label="File text"/></div>
    <div class="ebar"><span class="grow">${dirty ? (LS.get("auto", true) ? "Saving soon" : "Unsaved changes") : "All changes saved"}</span><button class="btn sm" disabled=${!dirty} onClick=${() => setText(base)}>Revert</button><button class="btn sm" disabled=${!dirty} onClick=${save}>Save</button></div></div>`;
}

// ---------- remote terminal (xterm.js on one of the server's terminals). A tab attaches to a terminal by name; closing the tab leaves it running
// (like JupyterLab), the Running panel shuts it down. Re-attaching replays what the UI still buffers.
const TERM = "term:", termOf = (p) => (p.startsWith(TERM) ? p.slice(TERM.length) : "");
function RemoteTerm({ server, name, post, api, active, gone }) {
  const host = useRef(), tr = useRef(), wt = useRef(null), [msg, setMsg] = useState("Connecting…"), [dead, setDead] = useState(false), [hint, setHint] = useState(""), [sug, setSug] = useState(termSuggestOn), hintT = useRef(0);
  const note = (m) => { setHint(m); clearTimeout(hintT.current); hintT.current = setTimeout(() => setHint(""), 3000); };
  useEffect(() => { if (active && tr.current) setTimeout(() => tr.current && tr.current.focus(), 30); }, [active]);
  useEffect(() => {
    let stop = false, t, ro;
    (async () => {
      try {
        const [{ Terminal }, { FitAddon }] = await loadXterm();
        if (stop) return;
        t = new Terminal({ fontFamily: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace", fontSize: 12.5, cursorBlink: true, scrollback: 5000, theme: termTheme() });
        tr.current = t;
        const fit = new FitAddon(); t.loadAddon(fit); t.open(host.current);
        let q = Promise.resolve();
        const send = (d) => { for (let i = 0; i < d.length; i += 4000) { const part = d.slice(i, i + 4000); q = q.then(() => post("jupyter/term/write", { server, name, data: part })).catch(() => {}); } };
        // copy / paste keys and the command assist (this terminal's history, AI); the server's PATH is not ours, so no command list
        const W = wireTerm(t, host.current, { send, kind: "jupyter:" + server, note, suggest: (prefix) => post("jupyter/ai/suggest", { prefix, suffix: "", lang: "bash", cells: [] }).then((x) => x.text || "") });
        wt.current = W;
        t.onData((d) => { W.onData(d); send(d); });
        const size = () => { try { if (!host.current || !host.current.offsetWidth) return; fit.fit(); post("jupyter/term/write", { server, name, rows: t.rows, cols: t.cols }).catch(() => {}); } catch (e) { /* hidden */ } };
        ro = new ResizeObserver(size); ro.observe(host.current); size();
        let after = 0, chid = 0, first = true;
        while (!stop) {
          const p = await api(`jupyter/poll?server=${enc(server)}&kind=t&id=${enc(name)}&after=${after}&wait=15`);
          if (stop) break;
          if (first) { setMsg(""); first = false; if (active) t.focus(); }
          if (chid && p.ch !== chid) after = 0;
          chid = p.ch;
          for (const ev of p.events) { after = ev.n; try { const m = JSON.parse(ev.d); if (m[0] === "stdout") t.write(m[1]); } catch (e) { /* not a terminal frame */ } }
          if (p.closed) { t.write("\r\n[terminal closed]\r\n"); setDead(true); setMsg("This terminal has ended."); break; }
        }
      } catch (e) { if (!stop) { setDead(true); setMsg(e.message); } }
    })();
    return () => { stop = true; clearTimeout(hintT.current); if (ro) ro.disconnect(); if (wt.current) wt.current.dispose(); wt.current = null; if (t) t.dispose(); tr.current = null; };
  }, [server, name]);
  const tool = (label, title, f) => html`<button type="button" class="btn sm ghost" title=${title} onMouseDown=${(e) => e.preventDefault()} onClick=${f}>${label}</button>`;
  return html`<div class="nbj-termw">${msg && html`<div class=${"row nbj-termh" + (dead ? " bad" : "")}>${dead ? Ic("alert", 13) : html`<i class="spin"></i>`}<span class="small grow">${msg}</span>${dead && html`<button class="btn sm" onClick=${gone}>Close tab</button>`}</div>`}
    <div class="nbj-termbar"><span class="small mute grow">${hint || "→ accepts grey text · Ctrl+Space history · # describe a command, AI writes it"}</span>
      ${tool("Copy", "Copy the selection (" + (isMacJ ? "⌘C" : "Ctrl+Shift+C") + ")", () => { if (wt.current && !wt.current.copy()) note("Select text first"); else note("Copied"); })}
      ${tool("Paste", "Paste from the clipboard (" + (isMacJ ? "⌘V" : "Ctrl+V") + ")", () => wt.current && wt.current.paste())}
      ${tool("Clear", "Clear the screen" + (isMacJ ? " (⌘K)" : ""), () => { if (tr.current) { tr.current.clear(); tr.current.focus(); } })}
      <button type="button" class=${"btn sm ghost" + (sug ? " on" : "")} aria-pressed=${sug} title="AI suggestions as grey text after a pause" onMouseDown=${(e) => e.preventDefault()} onClick=${() => { setTermSuggest(!sug); setSug(!sug); }}>Suggest ${sug ? "on" : "off"}</button></div>
    <div class="nbj-term" ref=${host}></div></div>`;
}

// ---------- Running panel: kernels and terminals on the server (JupyterLab's "Running Terminals and Kernels")
function Running({ server, post, tabs, cur, open, closeTab, newTerm, onTermGone }) {
  const [d, setD] = useState(null), [err, setErr] = useState(""), [busy, setBusy] = useState({}), [specs, setSpecs] = useState({}), [fold, setFold] = useState(() => LS.get("runfold", {}));
  const load = useCallback(() => post("jupyter/running", { server }).then((r) => { setD(r); setErr(""); }).catch((e) => setErr(e.message)), [server]);
  useEffect(() => { load(); post("jupyter/kernelspecs", { server }).then((r) => setSpecs(Object.fromEntries(r.kernelspecs.map((x) => [x.name, x.display])))).catch(() => {});
    const vis = () => document.visibilityState === "visible" && load(), t = setInterval(vis, 5000);
    document.addEventListener("visibilitychange", vis); return () => { clearInterval(t); document.removeEventListener("visibilitychange", vis); }; }, [server]);
  useEffect(() => { LS.set("runfold", fold); }, [fold]);
  const act = (key, f) => guard(async () => { setBusy((b) => ({ ...b, [key]: 1 })); try { await f(); } finally { setBusy((b) => { const n = { ...b }; delete n[key]; return n; }); load(); } })();
  const tell = (kid, op) => dispatchEvent(new CustomEvent("nbj-kernel", { detail: { server, kid, op } }));
  const shut = (k) => act(k.id, async () => {
    const used = k.sessions.filter((s) => tabs.includes(s.path)).map((s) => s.path.split("/").pop());
    if (used.length && !(await confirmBox({ title: "Shut down this kernel?", text: used.join(", ") + " use" + (used.length === 1 ? "s" : "") + " it: its variables are lost. Reconnect in the notebook starts a new kernel.", ok: "Shut down", danger: true }))) return;
    await post("jupyter/kernel/shutdown", { server, kid: k.id }); tell(k.id, "shutdown");
  });
  const restart = (k) => act(k.id, async () => { if (!(await confirmBox({ title: "Restart this kernel?", text: "All its variables are lost.", ok: "Restart" }))) return; await post("jupyter/kernel/op", { server, kid: k.id, op: "restart" }); toast("Kernel restarting"); });
  const interrupt = (k) => act(k.id, () => post("jupyter/kernel/op", { server, kid: k.id, op: "interrupt" }));
  const killTerm = (t) => act("t" + t.name, async () => { await post("jupyter/term/close", { server, name: t.name }); onTermGone(t.name); });
  const allK = () => act("allk", async () => {
    if (!(await confirmBox({ title: "Shut down all " + d.kernels.length + " kernels?", text: "Every notebook on this server loses its variables.", ok: "Shut down all", danger: true }))) return;
    for (const k of d.kernels) { await post("jupyter/kernel/shutdown", { server, kid: k.id }).catch((e) => toast(e.message)); tell(k.id, "shutdown"); }
  });
  const allT = () => act("allt", async () => {
    if (!(await confirmBox({ title: "Shut down all " + d.terminals.length + " terminals?", text: "Programs running in them are stopped.", ok: "Shut down all", danger: true }))) return;
    for (const t of d.terminals) { await post("jupyter/term/close", { server, name: t.name }).catch((e) => toast(e.message)); onTermGone(t.name); }
  });
  const sec = (id, title, n, extra) => html`<div class="nbj-rsec"><button class="nbj-rsh" aria-expanded=${!fold[id]} onClick=${() => setFold((f) => ({ ...f, [id]: !f[id] }))}><span class=${"nbj-chev" + (fold[id] ? "" : " o")}>${Ic("right", 12)}</span><b>${title}</b><span class="nbj-ct">${n}</span></button><span class="grow"></span>${extra}</div>`;
  const spin = (k) => (busy[k] ? html`<i class="spin"></i>` : null);
  const kdot = (st) => "nbj-dot " + (st === "busy" ? "busy" : st === "idle" ? "ok" : st === "dead" ? "bad" : "off");
  const ks = (d && d.kernels) || [], ts = d && d.terminals, paths = tabs.filter((p) => !termOf(p));
  return html`<div class="nbj-list nbj-procs">
    ${err && html`<div class="sq-test bad small">${Ic("alert", 13)} <span>${err}</span></div>`}
    ${!d && !err && html`<p class="mute small" style="padding:6px 8px"><i class="spin"></i> Loading…</p>`}
    ${paths.length > 0 && html`${sec("tabs", "Open tabs", paths.length)}${!fold.tabs && paths.map((p) => html`<div key=${p} class=${"nbj-f nbj-ri" + (p === cur ? " on" : "")} role="button" tabindex="0" title=${p} onClick=${() => open(p)} onKeyDown=${(e) => e.key === "Enter" && open(p)}>
      <span class=${"nbj-fi " + (p.endsWith(".ipynb") ? "notebook" : "file")}>${Ic(p.endsWith(".ipynb") ? "book" : "file", 14)}</span><span class="nbj-fn"><span class="sq-ell">${p.split("/").pop()}</span></span>
      <span class="nbj-fa"><button class="ibtn sm" title="Close tab" aria-label="Close tab" onClick=${(e) => { e.stopPropagation(); closeTab(p); }}>${Ic("x", 12)}</button></span></div>`)}`}
    ${d && html`${sec("k", "Kernels", ks.length, ks.length > 1 && html`<button class="btn sm ghost bad" disabled=${!!busy.allk} onClick=${allK}>${spin("allk")} Shut down all</button>`)}
      ${!fold.k && (ks.length ? ks.map((k) => html`<div key=${k.id} class="nbj-ri nbj-kr">
        <div class="row" style="gap:8px"><span class=${kdot(k.state)} title=${k.state || "unknown"}></span><span class="nbj-fn"><span class="sq-ell"><b>${specs[k.name] || k.name || "Kernel"}</b></span><small>${k.state || "unknown"}${k.activity ? " · " + ago(k.activity) : ""}${k.connections ? " · " + k.connections + " connected" : ""}</small></span>${spin(k.id)}</div>
        ${k.sessions.length ? k.sessions.map((s) => html`<button key=${s.id} class="nbj-ks" title=${"Open " + s.path} onClick=${() => s.path && open(s.path)}>${Ic(s.path.endsWith(".ipynb") ? "book" : "file", 12)}<span class="sq-ell">${s.path || s.name || "(no file)"}</span></button>`) : html`<span class="mute small nbj-ks0">No notebook attached</span>`}
        <div class="nbj-kact"><button class="btn sm ghost" disabled=${!!busy[k.id]} title="Interrupt what runs" onClick=${() => interrupt(k)}>${Ic("stop", 12)} Interrupt</button><button class="btn sm ghost" disabled=${!!busy[k.id]} title="Restart: clears all variables" onClick=${() => restart(k)}>${Ic("refresh", 12)} Restart</button><button class="btn sm ghost bad" disabled=${!!busy[k.id]} onClick=${() => shut(k)}>${Ic("x", 12)} Shut down</button></div></div>`)
        : html`<p class="mute small nbj-r0">No kernel is running. Opening a notebook starts one.</p>`)}
      ${sec("t", "Terminals", ts ? ts.length : 0, html`${ts && ts.length > 1 && html`<button class="btn sm ghost bad" disabled=${!!busy.allt} onClick=${allT}>${spin("allt")} Shut down all</button>`}${ts && html`<button class="ibtn sm" title="New terminal" aria-label="New terminal" onClick=${newTerm}>${Ic("plus", 14)}</button>`}`)}
      ${!fold.t && (ts === null ? html`<p class="mute small nbj-r0">Terminals are turned off on this server.</p>` : ts.length ? ts.map((t) => html`<div key=${t.name} class=${"nbj-f nbj-ri" + (cur === TERM + t.name ? " on" : "")} role="button" tabindex="0" title="Open terminal" onClick=${() => open(TERM + t.name)} onKeyDown=${(e) => e.key === "Enter" && open(TERM + t.name)}>
        <span class="nbj-fi">${Ic("terminal", 14)}</span><span class="nbj-fn"><span class="sq-ell">Terminal ${t.name}</span><small>${tabs.includes(TERM + t.name) ? "open" : "running"}${t.activity ? " · " + ago(t.activity) : ""}</small></span>${spin("t" + t.name)}
        <span class="nbj-fa"><button class="ibtn sm bad" title="Shut down" aria-label="Shut down terminal" disabled=${!!busy["t" + t.name]} onClick=${(e) => { e.stopPropagation(); killTerm(t); }}>${Ic("x", 12)}</button></span></div>`)
        : html`<p class="mute small nbj-r0">No terminal. <button class="linkbtn" onClick=${newTerm}>Open one</button></p>`)}`}</div>`;
}

// ---------- workspace: file panel + tabs
function Workspace({ server, servers, conns, settings, models, post, api, go, use, setSettings, onManage, onLive }) {
  const sid = server.id;
  const [dir, setDir] = useState(LS.get("dir_" + sid, "")), [kids, setKids] = useState({}), [exp, setExp] = useState(() => LS.get("exp_" + sid, [])), [err, setErr] = useState(""), [loading, setLoading] = useState({});
  const [tabs, setTabs] = useState(() => LS.get("tabs_" + sid, [])), [cur, setCur] = useState(() => LS.get("cur_" + sid, "")), [panel, setPanel] = useState(() => LS.get("panel", "files")), [outline, setOutline] = useState({}), [jump, setJump] = useState(null);
  const [q, setQ] = useState(""), [newMenu, setNewMenu] = useState(false), dirties = useRef({}), fileIn = useRef();
  const [drop, setDrop] = useState(null), [upl, setUpl] = useState(null), uplStop = useRef(false), dragSrc = useRef("");
  const loadDir = (d) => { setLoading((l) => ({ ...l, [d]: 1 })); return post("jupyter/ls", { server: sid, path: d }).then((r) => { setKids((k) => ({ ...k, [r.path || ""]: r.items })); setErr(""); if (!d && server.kind === "local" && !server.running && onLive) onLive(); }).catch((e) => { setErr(e.message); setKids((k) => ({ ...k, [d]: k[d] || [] })); }).finally(() => setLoading((l) => { const n = { ...l }; delete n[d]; return n; })); };
  const refresh = () => { const ds = new Set(["", ...exp]); ds.forEach((d) => loadDir(d)); };
  useEffect(() => { setKids({}); setErr(""); setDir(LS.get("dir_" + sid, "")); const e = LS.get("exp_" + sid, []); setExp(e); ["", ...e].forEach((d) => loadDir(d)); setTabs(LS.get("tabs_" + sid, [])); setCur(LS.get("cur_" + sid, ""));
    post("jupyter/running", { server: sid }).then((r) => { const live = new Set((r.terminals || []).map((t) => TERM + t.name)); setTabs((t) => t.filter((p) => !termOf(p) || live.has(p))); }).catch(() => {});   // terminal tabs whose terminal ended while away
  }, [sid]);
  useEffect(() => { LS.set("tabs_" + sid, tabs); LS.set("cur_" + sid, cur); }, [tabs, cur, sid]);
  useEffect(() => { LS.set("exp_" + sid, exp); LS.set("dir_" + sid, dir); }, [exp, dir, sid]);
  useEffect(() => { LS.set("panel", panel); }, [panel]);
  const up = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
  const toggle = (x) => { setDir(x.path); if (exp.includes(x.path)) setExp(exp.filter((e) => e !== x.path)); else { setExp([...exp, x.path]); if (!kids[x.path]) loadDir(x.path); } };
  const open = (p) => { setTabs((t) => (t.includes(p) ? t : [...t, p])); setCur(p); if (!termOf(p)) setDir(up(p)); };
  const newTerm = guard(async () => { const r = await post("jupyter/term/new", { server: sid }); open(TERM + r.name); });
  const dropTab = (p) => setTabs((t) => t.filter((x) => x !== p));
  useEffect(() => { if (cur && !tabs.includes(cur)) setCur(tabs[tabs.length - 1] || ""); }, [tabs]);       // the current tab went away (terminal shut down ...)
  const closeTab = async (p) => {
    if (dirties.current[p] && dirties.current[p].current && !(await confirmBox({ title: "Close without saving?", text: p.split("/").pop() + " has unsaved changes.", ok: "Close", danger: true }))) return;
    delete dirties.current[p]; const rest = tabs.filter((x) => x !== p); setTabs(rest); if (cur === p) setCur(rest[rest.length - 1] || "");
    if (termOf(p)) toast("Terminal " + termOf(p) + " keeps running: shut it down in Running");
  };
  const dref = (p) => (dirties.current[p] ||= { current: false });
  const reveal = (d) => { if (d && !exp.includes(d)) setExp((e) => [...e, d]); };
  const mk = guard(async (type, at) => {
    const d = at == null ? dir : at;
    const v = await ask({ title: (type === "notebook" ? "New notebook" : type === "file" ? "New file" : "New folder") + (d ? " in " + d : ""), submit: "Create", fields: [{ name: "name", label: "Name", type: "text", value: type === "notebook" ? "Untitled.ipynb" : type === "file" ? "untitled.txt" : "new-folder" }] });
    if (!v || !v.name.trim()) return;
    const r = await post("jupyter/file/new", { server: sid, dir: d, type, name: v.name.trim() }); reveal(d); await loadDir(d); if (type !== "directory") open(r.path);
  });
  const rename = guard(async (it) => {
    const v = await ask({ title: "Rename", submit: "Rename", fields: [{ name: "name", label: "New name", type: "text", value: it.name }] });
    if (!v || !v.name.trim() || v.name === it.name) return;
    const to = (up(it.path) ? up(it.path) + "/" : "") + v.name.trim(); await post("jupyter/file/rename", { server: sid, path: it.path, to });
    moved(it.path, to); loadDir(up(it.path));
  });
  const del = guard(async (it) => {
    if (!(await confirmBox({ title: "Delete " + it.name + "?", text: "It is removed from the Jupyter server.", ok: "Delete", danger: true }))) return;
    await post("jupyter/file/delete", { server: sid, path: it.path }); setTabs((t) => t.filter((x) => x !== it.path && !x.startsWith(it.path + "/"))); if (cur === it.path) setCur(""); loadDir(up(it.path));
  });
  // tabs (and the clipboard) follow a file or folder that was renamed / moved from A to B
  const moved = (a, b) => { const m = (x) => (x === a ? b : x.startsWith(a + "/") ? b + x.slice(a.length) : x); setTabs((t) => t.map(m)); setCur((c) => m(c)); if (nbClip && nbClip.sid === sid) nbClip = { ...nbClip, path: m(nbClip.path) }; };
  // ---- right-click menu of a file, a folder, an open tab or the empty list (paths are the server's; "" = its top folder)
  const [fctx, setFctx] = useState(null), [, setClipV] = useState(0);
  const openF = (e, it, extra) => { e.preventDefault(); e.stopPropagation(); setFctx({ at: menuAt(e), it, extra }); };
  const fileMenu = useCallback((e, p, extra) => openF(e, { path: p, name: p.split("/").pop(), type: p.endsWith(".ipynb") ? "notebook" : "file", tab: true }, extra), []);
  const copyText = async (t) => { try { await navigator.clipboard.writeText(t); toast("Copied " + t); } catch (e) { toast("Could not copy: " + t); } };
  const fClip = (it, cut) => { nbClip = { sid, path: it.path, name: it.name, type: it.type, cut }; setClipV((v) => v + 1); toast((cut ? "Cut " : "Copied ") + it.name + ": right-click a folder and Paste"); };
  const fPaste = guard(async (to) => {
    const c = nbClip; if (!c) return;
    if (c.sid !== sid) return toast("Paste works inside one server: download the file and upload it here");
    if (c.cut) {
      if (up(c.path) === to) { nbClip = null; setClipV((v) => v + 1); return; }
      if (c.type === "directory" && (to === c.path || to.startsWith(c.path + "/"))) return toast("Cannot move a folder into itself");
      const dest = (to ? to + "/" : "") + c.name;
      if ((kids[to] || []).some((x) => x.name === c.name)) return toast(c.name + " already exists in " + (to || "the top folder"));
      await post("jupyter/file/rename", { server: sid, path: c.path, to: dest }); moved(c.path, dest); nbClip = null; setClipV((v) => v + 1);
      reveal(to); loadDir(up(c.path)); loadDir(to); toast("Moved to " + dest); return;
    }
    const r = await post("jupyter/file/copy", { server: sid, path: c.path, to }); reveal(to); loadDir(to); toast("Pasted " + (r.path || c.name));
  });
  const fDup = guard(async (it) => { const r = await post("jupyter/file/copy", { server: sid, path: it.path, to: up(it.path) }); loadDir(up(it.path)); toast("Made " + (r.path || "a copy")); });
  const fDownload = guard(async (it) => {
    const dirty = dirties.current[it.path] && dirties.current[it.path].current; if (dirty) toast("Downloading the saved version (save first to include your changes)");
    const r = await post("jupyter/file/get", { server: sid, path: it.path });
    let blob;
    if (r.format === "base64") { const bin = atob(String(r.content || "").replace(/\s+/g, "")), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); blob = new Blob([u], { type: r.mimetype || "application/octet-stream" }); }
    else blob = new Blob([r.format === "json" || typeof r.content !== "string" ? JSON.stringify(r.content, null, 1) + "\n" : r.content], { type: r.mimetype || (it.type === "notebook" ? "application/x-ipynb+json" : "text/plain") });
    saveBlob(blob, it.name);
  });
  const uploadTo = (d) => { setDir(d); fileIn.current && fileIn.current.click(); };
  const fullPath = (p) => (server.abs_root ? server.abs_root.replace(/\/+$/, "") + "/" + p : "");
  const fItems = (it, tab, extra) => {
    const c = nbClip, mod = /Mac/.test(navigator.platform) ? "⌘" : "Ctrl+";
    if (!it) return [["New notebook", "book", "", false, () => mk("notebook", "")], ["New file", "file", "", false, () => mk("file", "")], ["New folder", "folder", "", false, () => mk("directory", "")], ["Upload files…", "upload", "", !!upl, () => uploadTo("")], "-",
      [c ? (c.cut ? "Paste (move " : "Paste (") + c.name + ")" : "Paste", "clip", mod + "V", !c, () => fPaste("")], "-", ["Refresh", "refresh", "", false, refresh]];
    const isDir = it.type === "directory", here = isDir ? it.path : up(it.path);
    return [...(extra || []),
      tab ? ["Close tab", "x", "", false, () => closeTab(it.path)] : isDir ? [exp.includes(it.path) ? "Collapse" : "Expand", "folder", "", false, () => toggle(it)] : ["Open", it.type === "notebook" ? "book" : "file", "", false, () => open(it.path)],
      "-",
      isDir && ["New notebook here", "book", "", false, () => mk("notebook", it.path)], isDir && ["New file here", "file", "", false, () => mk("file", it.path)], isDir && ["New folder here", "folder", "", false, () => mk("directory", it.path)], isDir && ["Upload files here…", "upload", "", !!upl, () => uploadTo(it.path)],
      "-",
      ["Cut", "shrink", mod + "X", false, () => fClip(it, true)], ["Copy", "copy", mod + "C", isDir, () => fClip(it, false)], [(c ? (c.cut ? "Paste (move " : "Paste (") + c.name + ")" : "Paste") + (isDir ? " into " + it.name : " here"), "clip", mod + "V", !c, () => fPaste(here)], ["Duplicate", "layers", mod + "D", isDir, () => fDup(it)],
      "-",
      ["Rename…", "edit", "F2", false, () => rename(it)],
      "-",
      ["Copy path", "link", "", false, () => copyText(it.path)], fullPath(it.path) && ["Copy full path", "link", "", false, () => copyText(fullPath(it.path))], ["Copy name", "copy", "", false, () => copyText(it.name)],
      ["Download", "download", "", isDir, () => fDownload(it)],
      it.type === "notebook" && ["Use as a Works node…", "flow", "", false, () => expose(it.path)],
      "-",
      ["Delete", "trash", "Del", false, () => del(it), true]];
  };
  const rowKey = (x) => (e) => {
    const m = /Mac/.test(navigator.platform) ? e.metaKey : e.ctrlKey, k = e.key.toLowerCase(), go = (f) => { e.preventDefault(); e.stopPropagation(); f(); };
    if (e.key === "F2") return go(() => rename(x));
    if (e.key === "Delete" || (m && e.key === "Backspace")) return go(() => del(x));
    if (m && k === "c" && x.type !== "directory") return go(() => fClip(x, false));
    if (m && k === "x") return go(() => fClip(x, true));
    if (m && k === "v" && nbClip) return go(() => fPaste(x.type === "directory" ? x.path : up(x.path)));
    if (m && k === "d" && x.type !== "directory") return go(() => fDup(x));
    if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) return go(() => setFctx({ at: menuAt(null, e.currentTarget), it: x }));
  };
  const CHUNK = 6 * 1024 * 1024, b64 = async (blob) => { const b = new Uint8Array(await blob.arrayBuffer()); let x = ""; for (let i = 0; i < b.length; i += 32768) x += String.fromCharCode.apply(null, b.subarray(i, i + 32768)); return btoa(x); };
  // items = [{rel: "a/b.txt", file}] plus folders to create (dropped folders); into folder TO
  const upload = guard(async (items, to = dir, dirs = []) => {
    if (!items.length && !dirs.length) return;
    const big = items.find((x) => x.file.size > 2e9); if (big) return toast(big.rel + " is over 2 GB");
    const there = new Set((kids[to] || []).map((x) => x.name)), clash = [...new Set([...items.map((x) => x.rel), ...dirs].map((r) => r.split("/")[0]))].filter((n) => there.has(n));
    if (clash.length && !(await confirmBox({ title: "Replace " + (clash.length === 1 ? clash[0] : clash.length + " items") + "?", text: (clash.length === 1 ? "It already exists" : clash.slice(0, 6).join(", ") + (clash.length > 6 ? " …" : "") + " already exist") + " in " + (to || "the top folder") + ". Files with the same name are overwritten; folders are merged.", ok: "Replace", danger: true }))) return;
    const at = (r) => (to ? to + "/" : "") + r, total = items.reduce((n, x) => n + x.file.size, 0);
    uplStop.current = false; let sent = 0, done = 0;
    setUpl({ n: 0, of: items.length, name: "", pct: 0 });
    try {
      for (const d of [...new Set(dirs)].sort((x, y) => x.split("/").length - y.split("/").length)) await post("jupyter/file/put", { server: sid, path: at(d), type: "directory" });
      for (const { rel, file } of items) {
        if (uplStop.current) break;
        setUpl({ n: done, of: items.length, name: rel, pct: total ? sent / total : 0 });
        if (file.size <= CHUNK) { await post("jupyter/file/put", { server: sid, path: at(rel), type: "file", format: "base64", content: await b64(file) }); sent += file.size; }
        else {                                                           // Jupyter appends numbered base64 parts (1, 2, ..., -1 = last)
          let r;
          for (let i = 0, n = 1; i < file.size; i += CHUNK, n++) {
            if (uplStop.current) throw new Error("Upload stopped: " + rel + " is incomplete");
            const last = i + CHUNK >= file.size;
            r = await post("jupyter/file/put", { server: sid, path: at(rel), type: "file", format: "base64", chunk: last ? -1 : n, content: await b64(file.slice(i, i + CHUNK)) });
            sent += Math.min(CHUNK, file.size - i); setUpl({ n: done, of: items.length, name: rel, pct: total ? sent / total : 1 });
          }
          if (r && r.size != null && r.size !== file.size) throw new Error("The server does not take files in parts: " + rel + " arrived broken (" + fmtSize(r.size) + " of " + fmtSize(file.size) + "). Upload files up to " + fmtSize(CHUNK) + " here.");
        }
        done++;
      }
      toast(uplStop.current ? "Upload stopped after " + done + " of " + items.length : done === 1 ? "Uploaded " + items[0].rel.split("/").pop() : "Uploaded " + done + " files" + (dirs.length ? " in " + new Set(dirs.map((d) => d.split("/")[0])).size + " folder(s)" : ""));
    } finally { setUpl(null); reveal(to); loadDir(to); dirs.filter((d) => !d.includes("/")).forEach((d) => kids[at(d)] && loadDir(at(d))); }
  });
  // what was dropped -> files + folders (folders are walked with the File System entries API; must be read inside the drop event)
  const collect = async (dt) => {
    const ents = [...(dt.items || [])].map((it) => (it.kind === "file" && it.webkitGetAsEntry ? it.webkitGetAsEntry() : null));
    if (!ents.some((e) => e && e.isDirectory)) return { items: [...dt.files].map((f) => ({ rel: f.name, file: f })), dirs: [] };
    const items = [], dirs = [];
    const all = (rd) => new Promise((res, rej) => { const out = []; const more = () => rd.readEntries((b) => { if (!b.length) return res(out); out.push(...b); more(); }, rej); more(); });
    const walk = async (e, base) => {
      if (!e) return;
      if (e.isFile) { items.push({ rel: base + e.name, file: await new Promise((res, rej) => e.file(res, rej)) }); return; }
      if (/^(\.git|node_modules|__pycache__|\.DS_Store)$/.test(e.name)) return;
      dirs.push(base + e.name); for (const c of await all(e.createReader())) await walk(c, base + e.name + "/");
    };
    for (const e of ents) await walk(e, "");
    return { items: items.filter((x) => x.file.name !== ".DS_Store"), dirs };
  };
  const NBJ = "application/x-nbj-path";
  const dropDir = (e) => { const r = e.target.closest && e.target.closest("[data-drop]"); return r ? r.getAttribute("data-drop") : dir; };
  const canMove = (src, to) => src && up(src) !== to && to !== src && !to.startsWith(src + "/");
  const onDragOver = (e) => {
    const types = [...(e.dataTransfer.types || [])], files = types.includes("Files"), mine = types.includes(NBJ);
    if (!files && !mine) return;
    const to = dropDir(e);
    if (mine && !canMove(dragSrc.current, to)) { e.dataTransfer.dropEffect = "none"; setDrop(null); e.preventDefault(); return; }
    e.preventDefault(); e.dataTransfer.dropEffect = mine ? "move" : "copy";
    if (!drop || drop.to !== to || drop.move !== mine) setDrop({ to, move: mine });
  };
  const onDrop = guard(async (e) => {
    e.preventDefault(); const to = dropDir(e), src = e.dataTransfer.getData(NBJ); setDrop(null);
    if (src) { dragSrc.current = ""; if (!canMove(src, to)) return; const dest = (to ? to + "/" : "") + src.split("/").pop();
      if ((kids[to] || []).some((x) => x.name === src.split("/").pop())) return toast(src.split("/").pop() + " already exists in " + (to || "the top folder"));
      await post("jupyter/file/rename", { server: sid, path: src, to: dest });
      setTabs((t) => t.map((x) => (x === src ? dest : x.startsWith(src + "/") ? dest + x.slice(src.length) : x))); setCur((c) => (c === src ? dest : c.startsWith(src + "/") ? dest + c.slice(src.length) : c));
      reveal(to); loadDir(up(src)); loadDir(to); return; }
    if (upl) return toast("An upload is already running");
    const { items, dirs } = await collect(e.dataTransfer);
    upload(items, to, dirs);
  });
  const expose = guard(async (path) => {
    if (!(await confirmBox({ title: "Use " + path.split("/").pop() + " as a node?", text: "A work is created (switched off) with a Jupyter notebook node. Parameters are read from the cell tagged `parameters` (or the first cell) and become the work's inputs. In chat you can also ask: run " + path + " on " + server.name + ".", ok: "Create work" }))) return;
    const r = await post("jupyter/expose", { server: sid, path }); toast("Work created"); location.hash = "#/works/" + r.id;
  });
  const ic = (x) => (x.type === "directory" ? "folder" : x.type === "notebook" ? "book" : "file");
  const rowOf = (x, depth) => {
    const isDir = x.type === "directory", on = isDir ? exp.includes(x.path) : false;
    return html`<div class=${"nbj-f" + (cur === x.path ? " on" : "") + (isDir && dir === x.path ? " tgt" : "") + (drop && isDir && drop.to === x.path ? " drop" : "")} key=${x.path} style=${{ paddingLeft: 6 + depth * 14 + "px", "--d": depth }} role="treeitem" aria-expanded=${isDir ? on : undefined} tabindex="0" title=${x.path}
      data-drop=${isDir ? x.path : up(x.path)} draggable="true" onDragStart=${(e) => { dragSrc.current = x.path; e.dataTransfer.setData(NBJ, x.path); e.dataTransfer.effectAllowed = "move"; }} onDragEnd=${() => { dragSrc.current = ""; setDrop(null); }}
      onClick=${() => (isDir ? toggle(x) : open(x.path))} onContextMenu=${(e) => openF(e, x)} onKeyDown=${(e) => { if (e.key === "Enter") (isDir ? toggle(x) : open(x.path)); else rowKey(x)(e); }}>
      <span class=${"nbj-chev" + (on ? " o" : "") + (isDir ? "" : " none")}>${isDir ? Ic("right", 12) : ""}</span><span class=${"nbj-fi " + x.type}>${Ic(ic(x), 14)}</span>
      <span class=${"nbj-fn" + (isDir ? " dir" : "")}><span class="sq-ell">${x.name}</span></span>${isDir ? (kids[x.path] ? html`<span class="nbj-ct">${kids[x.path].length}</span>` : null) : (x.size != null ? html`<span class="nbj-ct">${x.size < 1024 ? x.size + "B" : x.size < 1048576 ? Math.round(x.size / 1024) + "K" : (x.size / 1048576).toFixed(1) + "M"}</span>` : null)}
      <span class="nbj-fa"><button class="ibtn sm" title="Download, copy path, rename, delete…" aria-label=${"Actions for " + x.name} onClick=${(e) => { e.stopPropagation(); setFctx({ at: menuAt(null, e.currentTarget), it: x }); }}>${Ic("more", 14)}</button></span></div>`;
  };
  const branch = (d, depth) => [...(kids[d] || [])].sort((a, b) => (a.type === "directory") !== (b.type === "directory") ? (a.type === "directory" ? -1 : 1) : String(a.name).localeCompare(String(b.name))).flatMap((x) => [rowOf(x, depth), x.type === "directory" && exp.includes(x.path) ? (kids[x.path] ? (kids[x.path].length ? branch(x.path, depth + 1) : html`<div class="nbj-none" style=${{ paddingLeft: 36 + (depth + 1) * 14 + "px" }}>Empty</div>`) : html`<div class="nbj-none" style=${{ paddingLeft: 36 + (depth + 1) * 14 + "px" }}><i class="spin"></i></div>`) : null]);
  const flat = q.trim() ? Object.values(kids).flat().filter((x) => x.name.toLowerCase().includes(q.toLowerCase())) : null;
  const rail = (id, icon, label, on, f) => html`<button class=${"nbj-rb" + (on ? " on" : "")} title=${label} aria-label=${label} aria-pressed=${on} onClick=${f}>${Ic(icon, 20)}</button>`;
  const filesOpen = panel === "files", outOpen = panel === "outline", setOpenP = panel === "settings", runOpen = panel === "running", anyPanel = filesOpen || outOpen || setOpenP || runOpen, heads = outline[cur] || [];
  const onOutline = useCallback((p, l) => setOutline((o) => (JSON.stringify(o[p]) === JSON.stringify(l) ? o : { ...o, [p]: l })), []);
  const [pw, setPw] = useState(() => LS.get("pw", 280));                // side panel width (drag the gap next to it)
  const dragPanel = (e) => { const w0 = pw; let w = w0; drag(e, "x", (dx) => setPw((w = Math.max(200, Math.min(Math.min(720, innerWidth * 0.55), w0 + dx)))), () => LS.set("pw", Math.round(w))); };
  return html`<div class=${"nbj-ws2 rsz" + (anyPanel ? "" : " nopanel")} style=${"--sq-pw:" + pw + "px"}>
    ${anyPanel && html`<div class="sq-vgrip" style=${"left:" + (77 + pw) + "px"} role="separator" aria-orientation="vertical" title="Drag to resize the panel (double-click to reset)" onPointerDown=${dragPanel} onDblClick=${() => { setPw(280); LS.set("pw", 280); }}></div>`}
    <div class="nbj-rail">${rail("files", "folder", "Files", filesOpen, () => setPanel(filesOpen ? "" : "files"))}${rail("outline", "list", "Table of contents", outOpen, () => setPanel(outOpen ? "" : "outline"))}${rail("running", "cpu", "Running kernels and terminals", runOpen, () => setPanel(runOpen ? "" : "running"))}${rail("term", "terminal", "New terminal on " + server.name, !!termOf(cur), newTerm)}<span class="nbj-rsep"></span>${rail("set", "sliders", "Settings: server and AI help", setOpenP, () => setPanel(setOpenP ? "" : "settings"))}</div>
    ${filesOpen && html`<aside class=${"nbj-panel" + (drop ? " dropping" : "")} aria-label="Files" onDragOver=${onDragOver} onDragLeave=${(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDrop(null); }} onDrop=${onDrop}>
      <div class="nbj-ph"><b>Files</b><span class="nbj-srvname sq-ell" title=${server.name}>${server.name}</span><span class="grow"></span><button class="ibtn sm" title="Close panel" aria-label="Close panel" onClick=${() => setPanel("")}>${Ic("x", 14)}</button></div>
      <div class="nbj-pill">
        <${Pop} open=${newMenu} setOpen=${setNewMenu} up=${false} width=${220} button=${html`<button class="ibtn sm" title=${"New in " + (dir || "top folder")} aria-label="New" onClick=${() => setNewMenu(!newMenu)}>${Ic("plus", 16)}</button>`}>
          <div class="optlist"><h5>New in ${dir || "top folder"}</h5>${[["notebook", "book", "Notebook", ".ipynb with a live kernel"], ["file", "file", "Text file", "Python, CSV, Markdown …"], ["directory", "folder", "Folder", "Group files together"]].map(([t, i, n, d]) => html`<button class="opt" onClick=${() => { setNewMenu(false); mk(t); }}><span class="oi">${Ic(i, 16)}</span><span class="grow"><b>${n}</b><span class="d">${d}</span></span></button>`)}</div><//>
        <button class="ibtn sm" title=${"Upload files to " + (dir || "the top folder") + " (or drop files and folders on the list)"} aria-label="Upload" disabled=${!!upl} onClick=${() => fileIn.current.click()}>${Ic("upload", 16)}</button>
        <button class="ibtn sm" title="Refresh" aria-label="Refresh" onClick=${refresh}>${Ic("refresh", 16)}</button>
        <button class="ibtn sm" title="Top folder" aria-label="Top folder" onClick=${() => setDir("")}>${Ic("home", 16)}</button>
        <input class="nbj-q" placeholder="Filter" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Filter files"/><input type="file" multiple hidden ref=${fileIn} onChange=${(e) => { upload([...e.target.files].map((f) => ({ rel: f.name, file: f }))); e.target.value = ""; }}/></div>
      ${upl && html`<div class="nbj-upl" role="status"><div class="row" style="gap:6px"><i class="spin"></i><span class="small grow sq-ell">Uploading ${upl.of > 1 ? upl.n + 1 + " of " + upl.of + " · " : ""}${upl.name.split("/").pop()}</span><button class="ibtn sm" title="Stop the upload" aria-label="Stop the upload" onClick=${() => { uplStop.current = true; }}>${Ic("x", 12)}</button></div><div class="nbj-uplbar"><i style=${{ width: Math.round(upl.pct * 100) + "%" }}></i></div></div>`}
      ${drop && html`<div class="nbj-droph">${Ic(drop.move ? "folder" : "upload", 13)} ${drop.move ? "Move to " : "Drop to upload to "}<b>${drop.to || "top folder"}</b></div>`}
      <div class="nbj-list" role="tree" data-drop=${dir} onContextMenu=${(e) => { if (e.target.closest("input,a")) return; openF(e, null); }}>
        ${err && html`<div class="sq-test bad small">${Ic("alert", 13)} <span>${err}</span></div>`}
        ${!kids[""] && !err && html`<p class="mute small" style="padding:6px 8px"><i class="spin"></i> Connecting to ${server.name}…</p>`}
        ${flat ? (flat.length ? flat.map((x) => rowOf(x, 0)) : html`<p class="mute small" style="padding:10px 8px">No file matches.</p>`) : html`${dir && html`<div class="nbj-f up" role="button" tabindex="0" data-drop=${up(dir)} onClick=${() => setDir(up(dir))}><span class="nbj-chev none"></span><span class="nbj-fi">${Ic("folder", 17)}</span><span class="nbj-fn">..</span></div>`}${branch("", 0)}`}
        ${kids[""] && !kids[""].length && !err && !q && html`<p class="mute small" style="padding:10px 8px">This server has no files yet. Use +, or drop files here to upload.</p>`}</div></aside>`}
    ${fctx && html`<${CtxMenu} at=${fctx.at} items=${fItems(fctx.it, fctx.it && fctx.it.tab, fctx.extra)} close=${() => setFctx(null)}/>`}
    ${runOpen && html`<aside class="nbj-panel" aria-label="Running kernels and terminals">
      <div class="nbj-ph"><b>Running</b><span class="nbj-srvname sq-ell" title=${server.name}>${server.name}</span><span class="grow"></span><button class="ibtn sm" title="Close panel" aria-label="Close panel" onClick=${() => setPanel("")}>${Ic("x", 14)}</button></div>
      <${Running} server=${sid} post=${post} tabs=${tabs} cur=${cur} open=${open} closeTab=${closeTab} newTerm=${newTerm} onTermGone=${(n) => dropTab(TERM + n)}/></aside>`}
    ${outOpen && html`<aside class="nbj-panel" aria-label="Table of contents">
      <div class="nbj-ph"><b>Contents</b><span class="nbj-srvname sq-ell" title=${cur}>${cur ? cur.split("/").pop() : ""}</span><span class="grow"></span><button class="ibtn sm" title="Close panel" aria-label="Close panel" onClick=${() => setPanel("")}>${Ic("x", 14)}</button></div>
      <nav class="nbj-list nbj-toc">${!cur.endsWith(".ipynb") ? html`<p class="mute small" style="padding:10px 8px">Open a notebook to see its headings.</p>`
        : heads.length ? heads.map((h, i) => html`<button key=${i} class="nbj-th2" style=${{ paddingLeft: 8 + (h.level - 1) * 14 + "px" }} onClick=${() => setJump({ path: cur, key: h.key, n: Date.now() })}><span class=${"sq-ell l" + Math.min(h.level, 3)}>${h.text}</span></button>`)
        : html`<p class="mute small" style="padding:10px 8px">No headings yet. Start a text cell with <code>#</code> or <code>##</code> and it appears here.</p>`}</nav></aside>`}
    ${setOpenP && html`<aside class="nbj-panel" aria-label="Settings">
      <div class="nbj-ph"><b>Settings</b><span class="grow"></span><button class="ibtn sm" title="Close panel" aria-label="Close panel" onClick=${() => setPanel("")}>${Ic("x", 14)}</button></div>
      <div class="nbj-list nbj-setbody">
        <h6 class="nbj-sec">Working server <span>${servers.length}</span></h6>
        ${servers.map((x) => html`<button key=${x.id} class=${"nbj-srow" + (x.id === server.id ? " on" : "")} onClick=${() => x.id !== server.id && use(x.id, true)} aria-pressed=${x.id === server.id}>
          <span class=${"nbj-dot" + (x.kind === "local" ? (x.running ? " ok" : " off") : " ok")}></span><span class="nbj-fn"><span class="sq-ell">${x.name}</span><small>${KIND[x.kind] || x.kind}${x.kind === "local" ? (x.running ? " · running" : " · stopped") : ""}</small></span>${x.id === server.id ? Ic("check", 14) : ""}</button>`)}
        <button class="btn sm nbj-manage" onClick=${onManage}>${Ic("plus", 12)} Add or edit servers</button>
        <h6 class="nbj-sec">AI help</h6>
        <${AiSettings} settings=${settings} models=${models} post=${post} setSettings=${setSettings}/>
      </div></aside>`}
    <section class="nbj-main"><div class="sq-tabs" role="tablist">${tabs.map((p) => html`<div key=${p} role="tab" aria-selected=${p === cur} class=${"sq-tab" + (p === cur ? " on" : "")} title=${termOf(p) ? "Terminal " + termOf(p) + " on " + server.name : p} onClick=${() => setCur(p)} onContextMenu=${termOf(p) ? undefined : (e) => openF(e, { path: p, name: p.split("/").pop(), type: p.endsWith(".ipynb") ? "notebook" : "file", tab: true })}>${Ic(termOf(p) ? "terminal" : p.endsWith(".ipynb") ? "book" : "file", 12)}<span class="sq-ell">${termOf(p) ? "Terminal " + termOf(p) : p.split("/").pop()}</span>
          <button class="ibtn sm" aria-label="Close tab" title=${termOf(p) ? "Close the tab (the terminal keeps running)" : "Close"} onClick=${(e) => { e.stopPropagation(); closeTab(p); }}>${Ic("x", 11)}</button></div>`)}${tabs.length > 0 && html`<button class="ibtn sm nbj-newterm" title="New terminal" aria-label="New terminal" onClick=${newTerm}>${Ic("plus", 12)}</button>`}</div>
      <div class="nbj-stage">${!tabs.length && html`<div class="sq-empty big">${Ic("book", 28)}<div>Open a notebook from the file list</div><div class="mute small">Or create one with + — cells run on ${server.name}'s kernel. Terminals and kernels are under Running.</div></div>`}
        ${tabs.map((p) => html`<div key=${sid + p} class="nbj-tab" hidden=${p !== cur}>${termOf(p) ? html`<${RemoteTerm} server=${sid} name=${termOf(p)} post=${post} api=${api} active=${p === cur} gone=${() => dropTab(p)}/>` : p.endsWith(".ipynb") ? html`<${Notebook} server=${sid} path=${p} post=${post} api=${api} conns=${conns} settings=${settings} models=${models} dirtyRef=${dref(p)} onExpose=${expose} onOutline=${onOutline} onFileMenu=${fileMenu} jump=${jump}/>` : html`<${FileView} server=${sid} path=${p} post=${post} dirtyRef=${dref(p)} onFileMenu=${fileMenu} settings=${settings}/>`}</div>`)}</div>
</section></div>`;
}

// ---------- servers
function useJob(api, onDone) {
  const [job, setJob] = useState(null), t = useRef();
  useEffect(() => () => clearTimeout(t.current), []);
  const poll = (id, after = 0, lines = []) => api("works/env/job?id=" + id + "&after=" + after).then((r) => {
    const j = r.job, all = lines.concat(j.lines || []).slice(-300); setJob({ ...j, lines: all });
    if (j.state === "running") t.current = setTimeout(() => poll(id, j.next, all), 800); else onDone && onDone(j);
  }).catch((e) => setJob((x) => x && { ...x, state: "error", error: e.message }));
  return { job, watch: (j) => { setJob({ ...j, lines: [] }); poll(j.id); }, clear: () => setJob(null) };
}

function ServerForm({ init, post, api, close, saved, onOpen, onDelete, reload }) {
  const [f, setF] = useState(() => ({ kind: "hub", name: "", url: "", user: "", token: "", verify: true, kernel: "", env: "jupyter", root: "", ...init })), [busy, setBusy] = useState(false), [test, setTest] = useState(null);
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target ? e.target.value : e })), [note, setNote] = useState(null);
  // Whatever was pasted from the browser bar (…/user/name/lab?token=…) becomes the real server address, user and token
  const tidy = async (raw) => {
    if (!raw || !raw.trim() || f.kind === "local") return;
    try {
      const n = await post("jupyter/normalize", { url: raw, kind: f.kind });
      setF((x) => ({ ...x, url: n.url, user: x.user || n.user, token: x.token || n.token }));
      setNote(n.url !== raw.trim() || n.token || n.user ? n : null);
    } catch (e) { /* keep what was typed */ }
  };
  const save = guard(async () => { setBusy(true); try { saved((await post("jupyter/save", { server: f })).server); } finally { setBusy(false); } });
  const check = guard(async () => { setBusy(true); setTest(null); try { const s = await post("jupyter/save", { server: f }); setF((x) => ({ ...x, id: s.server.id, token: "", has_secret: s.server.has_secret })); setTest(await post("jupyter/test", { server: s.server.id }).catch((e) => ({ ok: false, error: e.message }))); } finally { setBusy(false); } });
  const field = (label, k, ph, type = "text", help, extra = {}) => html`<label class="sq-f"><span>${label}</span><input type=${type} value=${f[k]} placeholder=${ph || ""} onInput=${set(k)} autocomplete="off" ...${extra}/>${help && html`<small class="mute">${help}</small>`}</label>`;
  const addr = (label, ph) => field(label, "url", ph, "text", "Paste the address from your browser as it is (…/user/you/lab is fine): it is cleaned up for you.", { onBlur: (e) => tidy(e.target.value), onPaste: (e) => { const t = (e.clipboardData || window.clipboardData).getData("text"); if (t) { e.preventDefault(); setF((x) => ({ ...x, url: t })); tidy(t); } } });
  const tokenUrl = note && note.hub ? note.hub + "/hub/token" : "";
  const local = f.kind === "local";
  return html`<div class="ms-edit"><div class="ms-hd"><h3 class="grow">${f.id ? f.name || "Edit server" : "New server"}</h3>${f.id && html`<button class="btn sm acc" onClick=${() => onOpen(f.id)}>${Ic("play", 12)} Open</button>`}</div>
    <div class="ms-sec"><h4>Type</h4><div class="sq-tiles">${Object.keys(KIND).map((k) => html`<button type="button" class=${"sq-tile" + (f.kind === k ? " on" : "")} onClick=${() => setF((x) => ({ ...x, kind: k }))}><span>${KSHORT[k]}</span></button>`)}</div></div>
    <div class="ms-sec"><h4>${local ? "Local server" : "Connection"}</h4>
      ${field("Name", "name", local ? "Local Jupyter" : "Team hub")}
      ${f.kind === "hub" && html`${addr("JupyterHub address", "https://hub.example.com")}<div class="sq-grid2">${field("Your user name", "user", "alice")}
        ${field("API token" + (f.has_secret && !f.token ? " (saved: leave blank to keep)" : ""), "token", "", "password", "Hub > Token > Request new API token.")}</div>`}
      ${f.kind === "server" && html`${addr("Server address", "https://host/user/you/lab   or   http://localhost:8888")}${field("Token" + (f.has_secret && !f.token ? " (saved: leave blank to keep)" : ""), "token", "", "password", "From the URL Jupyter prints on start (?token=…), or leave blank when the server has none.")}`}
      ${local && html`<div class="sq-grid2">${field("Python environment", "env", "jupyter", "text", "Created and filled by Set up.")}${field("Notebook folder (optional)", "root", "~/notebooks")}</div>`}
      ${note && !local && html`<div class="sq-test ok">${Ic("check", 15)} <span>Using <code>${note.url}</code>${note.user ? html` · user <b>${note.user}</b>` : ""}${note.token ? " · token taken from the address" : ""}.${tokenUrl && !note.token && !f.token && !f.has_secret ? html` A JupyterHub server needs an API token: <a href=${tokenUrl} target="_blank" rel="noopener">create one</a>.` : ""}</span></div>`}</div>
    ${!local && html`<div class="ms-sec"><h4>Security</h4><div class="row sq-ro"><${Switch} on=${f.verify} onChange=${(v) => setF((x) => ({ ...x, verify: v }))} label="Verify TLS"/><div><b>Verify the TLS certificate</b><div class="mute small">Turn off only for a self-signed server you trust.</div></div></div></div>`}
    ${local && f.id && html`<div class="ms-sec"><h4>Install and run</h4><${LocalCard} s=${f} post=${post} api=${api} reload=${reload}/></div>`}
    ${test && html`<div class=${"sq-test " + (test.ok ? "ok" : "bad")}>${test.ok ? html`${Ic("check", 15)} Connected · Jupyter ${test.version} · kernels: ${(test.kernelspecs || []).map((s) => s.display).join(", ") || "none"}` : html`${Ic("alert", 15)} <span>${test.error}</span>`}</div>`}
    <div class="ms-bar">${!local && html`<button class="btn" disabled=${busy || !f.name.trim()} onClick=${check}>${busy ? html`<i class="spin"></i>` : Ic("bolt", 14)} Save and test</button>`}<span class="grow"></span>
      ${f.id && html`<button class="btn bad" onClick=${() => onDelete(f)}>${Ic("trash", 13)} Remove</button>`}<button class="btn" onClick=${close}>Revert</button><button class="btn acc" disabled=${busy || !f.name.trim()} onClick=${save}>Save</button></div></div>`;
}

function LocalCard({ s, post, api, reload }) {
  const [st, setSt] = useState(null), [extras, setExtras] = useState(["pandas", "matplotlib", "numpy"]), [busy, setBusy] = useState(false);
  const job = useJob(api, () => { refresh(); });
  const refresh = () => post("jupyter/local", { server: s.id, action: "status" }).then(setSt).catch((e) => setSt({ error: e.message, mods: {} }));
  useEffect(() => { refresh(); }, [s.id]);
  const act = guard(async (a) => { setBusy(true); try { const r = await post("jupyter/local", { server: s.id, action: a, extras }); if (r.job) job.watch(r.job); else setSt(r); reload(); } finally { setBusy(false); } });
  const m = (st && st.mods) || {}, ready = m.jupyter_server != null && m.ipykernel != null, running = job.job && job.job.state === "running";
  return html`<div class="nbj-local"><div class="row" style="gap:6px;flex-wrap:wrap">${["jupyter_server", "ipykernel", "pandas", "matplotlib", "numpy"].map((k) => html`<span class=${"tag " + (m[k] != null ? "good" : "")}>${m[k] != null ? Ic("check", 11) : ""} ${k}${m[k] ? " " + m[k] : ""}</span>`)}</div>
    ${st && st.error && html`<div class="sq-test bad small">${Ic("alert", 13)} ${st.error}</div>`}
    ${!ready && html`<div class="row" style="gap:10px;flex-wrap:wrap;margin-top:8px">${["pandas", "matplotlib", "numpy"].map((k) => html`<label class="small"><input type="checkbox" checked=${extras.includes(k)} onChange=${() => setExtras((x) => (x.includes(k) ? x.filter((y) => y !== k) : [...x, k]))}/> ${k}</label>`)}</div>`}
    <div class="row" style="gap:6px;margin-top:8px">${!ready ? html`<button class="btn acc" disabled=${busy || running} onClick=${() => act("setup")}>${running ? html`<i class="spin"></i>` : Ic("download", 14)} Set up (installs into ${(st && st.env) || s.env})</button>`
      : st && st.running ? html`<span class="tag good">${Ic("check", 11)} running on port ${st.port}</span><button class="btn sm" disabled=${busy} onClick=${() => act("stop")}>${Ic("stop", 12)} Stop</button>`
      : html`<button class="btn acc" disabled=${busy} onClick=${() => act("start")}>${busy ? html`<i class="spin"></i>` : Ic("play", 13)} Start</button>`}
      ${ready && html`<button class="btn sm" disabled=${busy || running} title="Install the chosen extra packages" onClick=${() => act("setup")}>Install extras</button>`}</div>
    ${job.job && html`<div class="sq-job"><div class="row"><span class=${"tag " + (running ? "acc" : job.job.state === "done" ? "good" : "bad")}>${running ? html`<i class="spin"></i>` : ""} ${running ? "Installing" : job.job.state === "done" ? "Installed" : "Failed"}</span><span class="grow"></span>${!running && html`<button class="ibtn" aria-label="Dismiss" onClick=${job.clear}>${Ic("x", 14)}</button>`}</div>
      <pre class="sq-log">${job.job.lines.join("\n") || "Starting…"}${job.job.error ? "\n" + job.job.error : ""}</pre></div>`}</div>`;
}

function Servers({ servers, post, api, reload, use, edit, setEdit }) {
  const [one, setOne] = useState(false);
  const oneClick = guard(async () => {
    setOne(true);
    try {
      let s = servers.find((x) => x.kind === "local");
      if (!s) s = (await post("jupyter/save", { server: { name: "Local Jupyter", kind: "local", env: "jupyter" } })).server;
      await reload();
      toast("Local server added: press Install, then Start.");
      setEdit(s);
    } finally { setOne(false); }
  });
  const remove = guard(async (s) => { if (await confirmBox({ title: "Remove " + s.name + "?", text: "Only the saved connection is removed; files on the server stay.", ok: "Remove", danger: true })) { await post("jupyter/delete", { id: s.id }); setEdit(null); reload(); } });
  const cur = edit || servers[0] || null, isNew = cur && !cur.id;
  return html`<div class="sq-pane"><div class="ms"><div class="ms-list ms-panel"><div class="ms-ph"><b>Servers</b><span>${servers.length}</span></div>
      ${servers.map((x) => html`<button key=${x.id} class=${"nbj-srow" + (cur && cur.id === x.id ? " on" : "")} onClick=${() => setEdit(x)}>
        <span class=${"nbj-dot" + (x.kind === "local" ? (x.running ? " ok" : " off") : " ok")}></span><span class="nbj-fn"><span class="sq-ell">${x.name}</span><small>${KIND[x.kind] || x.kind}${x.kind === "local" ? (x.running ? " · running" : " · stopped") : x.url ? " · " + x.url.replace(/^https?:\/\//, "") : ""}</small></span></button>`)}
      <button class=${"btn ms-add" + (isNew ? " on" : "")} onClick=${() => setEdit({})}>${Ic("plus", 14)} Add server</button>
      ${!servers.some((x) => x.kind === "local") && html`<button class="btn ms-add" disabled=${one} onClick=${oneClick}>${one ? html`<i class="spin"></i>` : Ic("zap", 14)} Set up a local server</button>`}<p class="mute small ms-note">Tokens are stored encrypted on this machine and never sent to the browser.</p></div>
      ${cur ? html`<${ServerForm} key=${cur.id || "new"} init=${cur} post=${post} api=${api} reload=${reload} onOpen=${(id) => use(id)} onDelete=${remove} close=${() => setEdit(null)} saved=${(s) => { setEdit(s); reload(); toast("Saved " + s.name); }}/>`
      : html`<div class="ms-empty">${Ic("book", 34)}<h3>Connect a Jupyter server</h3><p style="margin:0;max-width:46ch">Use your team's JupyterHub or any Jupyter server, or let AI Hub install and run one on this computer in one step.</p>
        <div class="row" style="gap:8px"><button class="btn acc" disabled=${one} onClick=${oneClick}>${one ? html`<i class="spin"></i>` : Ic("zap", 14)} Set up a local server</button><button class="btn" onClick=${() => setEdit({})}>${Ic("plus", 14)} Add a server</button></div></div>`}</div></div>`;
}

export function JupyterPage({ api, post, boot, arg }) {
  const [view, setView] = useState(() => (arg && ["work", "servers", "ai"].includes(arg.split("/")[0]) ? arg.split("/")[0] : LS.get("view", "work")));
  const [d, setD] = useState({ servers: [], settings: null }), [conns, setConns] = useState([]), [loaded, setLoaded] = useState(false), [sel, setSel] = useState(LS.get("server", "")), [edit, setEdit] = useState(null), [err, setErr] = useState("");
  const models = (boot && boot.models && boot.models.models) || [{ id: "", label: "Default", note: "" }];
  const reload = useCallback(() => Promise.all([api("jupyter/servers"), api("sql/connections").catch(() => ({ connections: [] }))]).then(([x, s]) => { setD(x); setConns(s.connections || []); setLoaded(true); setErr(""); }).catch((e) => { setErr(e.message); setLoaded(true); }), []);
  useEffect(() => { reload(); document.title = "Notebook"; }, []);
  const go = (v) => { setView(v); LS.set("view", v); };
  const server = d.servers.find((s) => s.id === sel) || d.servers[0] || null;
  const use = (id, stay) => { setSel(id); LS.set("server", id); if (!stay) go("work"); };
  const items = [{ id: "work", label: "Workspace", icon: "book" }, { id: "servers", label: "Servers", icon: "server" }, { id: "ai", label: "AI models", icon: "sparkles" }];
  const settingsOn = view === "servers" || view === "ai", noServer = loaded && view === "work" && !server;
  return html`<div class="sqs nbjp"><div class="sq-head">${(settingsOn || noServer) && server && html`<button class="btn sm ghost" onClick=${() => go("work")}>${Ic("left", 13)} Notebooks</button><span class="sq-sep"></span>`}<span class="sq-logo">${Ic("book", 18)}</span><h1>${settingsOn || noServer ? "Servers" : "Notebook"}</h1><span class="grow"></span></div>
    ${err && html`<div class="sq-test bad" style="margin:12px 20px">${Ic("alert", 15)} ${err}</div>`}
    ${!loaded ? html`<div class="sq-pane"><p class="mute">Loading…</p></div>`
    : view === "servers" || (view === "work" && !server) ? html`<${Servers} servers=${d.servers} post=${post} api=${api} reload=${reload} use=${use} edit=${edit} setEdit=${setEdit}/>`
    : view === "ai" ? html`<div class="sq-pane"><h2 style="margin:0 0 4px">AI models</h2><p class="mute small" style="margin:0 0 14px">Pick a model for each kind of help. They are used for every notebook.</p><${AiSettings} settings=${d.settings} models=${models} post=${post} setSettings=${(s) => setD((x) => ({ ...x, settings: s }))}/></div>`
    : html`<${Workspace} key=${server.id} server=${server} conns=${conns} settings=${d.settings} models=${models} post=${post} api=${api} go=${go} servers=${d.servers} use=${use} setSettings=${(x) => setD((y) => ({ ...y, settings: x }))} onManage=${() => go("servers")} onLive=${reload}/>`}</div>`;
}
