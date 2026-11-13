// Notebook: work with Jupyter servers (JupyterHub, Jupyter Server / Lab, or one this UI installs and runs locally).
// File browser, notebook editor with a live kernel (tables, figures, rich HTML), SQL cells that can fill a pandas DataFrame, a remote terminal,
// and AI help (inline suggestions, write a cell, fix an error) where each of the three has its own model. A notebook can become a Works node.
// Data: /api/jupyter/* (server.py -> aihubui/jupyter.py). Tokens never reach the browser.
import { html, useState, useEffect, useRef, useMemo, useCallback } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, guard, Seg, Modal, Switch, Pop, confirmBox, ask } from "./ui.js";
import { Output, serializeNotebook, parseNotebook, cellsOf } from "./notebook.js";
import { Grid } from "./sqlstudio.js";
import { Md } from "./blocks.js";
import { useHl, colorize, esc, langOf } from "./code.js";
import { CodeBox, IDENT, KW } from "./codebox.js";
import { sqlComplete, sqlSignature, SQL_WORDS } from "./sqlintel.js";
import { loadXterm } from "./term.js";

const LS = { get(k, d) { try { const v = localStorage.getItem("nbj_" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }, set(k, v) { try { localStorage.setItem("nbj_" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } } };
const enc = encodeURIComponent, sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const uid = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
const join = (s) => (Array.isArray(s) ? s.join("") : typeof s === "string" ? s : "");
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
const fmtSize = (n) => (n == null ? "" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB" : (n / 1048576).toFixed(1) + " MB");
const ago = (t) => { const d = t ? (Date.now() - new Date(t).getTime()) / 1000 : NaN; return isNaN(d) ? "" : d < 60 ? "just now" : d < 3600 ? Math.floor(d / 60) + " min ago" : d < 86400 ? Math.floor(d / 3600) + " h ago" : d < 2592000 ? Math.floor(d / 86400) + " d ago" : new Date(t).toLocaleDateString(); };
const KSHORT = { hub: "JupyterHub", server: "Jupyter Server", local: "Local" };
const KIND = { hub: "JupyterHub", server: "Jupyter Server / Lab", local: "Local (installed by AI Hub)" };
const sqlOf = (c) => (c.type === "code" && c.raw && c.raw.metadata && c.raw.metadata.aihub && c.raw.metadata.aihub.sql) || null;
const withSql = (c, sql) => { const raw = { ...(c.raw || {}) }, md = { ...(raw.metadata || {}) }, ai = { ...(md.aihub || {}) }; if (sql) ai.sql = sql; else delete ai.sql; if (Object.keys(ai).length) md.aihub = ai; else delete md.aihub; raw.metadata = md; return raw; };

// kernel output event -> the cell's nbformat outputs (same rules as the server's Fold)
function fold(o, dids, e) {
  const t = e.t, c = e.c || {};
  if (t === "stream") { const l = o[o.length - 1]; return l && l.output_type === "stream" && l.name === c.name ? [...o.slice(0, -1), { ...l, text: join(l.text) + (c.text || "") }] : [...o, { output_type: "stream", name: c.name || "stdout", text: c.text || "" }]; }
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
function Out({ o, post }) {
  const d = o.data || {}, h = join(d["text/html"]);
  const [src, setSrc] = useState("");
  const rich = (o.output_type === "display_data" || o.output_type === "execute_result") && h && /<script/i.test(h) && !d["image/png"];
  useEffect(() => { if (rich) post("jupyter/frame", { html: h }).then((r) => setSrc("/nbframe/" + r.id)).catch(() => {}); }, [rich, h]);
  if (rich) return src ? html`<iframe class="nbj-frame" sandbox="allow-scripts allow-popups" src=${src} title="Interactive output"></iframe>` : html`<div class="nb-o mute small">Loading output…</div>`;
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
  const [k, setK] = useState({ id: "", name: "", sid: "", state: "starting", err: "" }), [specs, setSpecs] = useState([]);
  const waiters = useRef({}), kid = useRef("");
  kid.current = k.id;
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
  }, [server, path, ready]);
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
            const w = waiters.current[e.p];
            if (w) { if (e.t === "execute_reply") w.done(e.c); else w.on(e); }
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
    waiters.current[msg] = { on, done: (c) => { delete waiters.current[msg]; done(c); } };
    post("jupyter/kernel/execute", { server, kid: kid.current, code, msg }).catch((e) => waiters.current[msg] && waiters.current[msg].done({ status: "error", lost: true, error: e.message }));
    return p;
  }, [server]);
  const op = (o) => post("jupyter/kernel/op", { server, kid: k.id, op: o });
  const switchTo = async (name) => { setK((x) => ({ ...x, state: "starting" })); try { const s = await post("jupyter/session", { server, switch: k.sid, kernel: name }); setK({ id: s.kernel.id, name: s.kernel.name, sid: s.id, state: "idle", err: "" }); } catch (e) { setK((x) => ({ ...x, state: "idle" })); toast(e.message); } };
  const complete = (code, pos) => post("jupyter/kernel/complete", { server, kid: kid.current, code, pos });
  const signature = (code, pos) => post("jupyter/kernel/inspect", { server, kid: kid.current, code, pos });
  const live = () => (kid.current && k.state === "idle" ? { server, kid: kid.current } : {});
  return { k, specs, exec, complete, signature, live, interrupt: () => op("interrupt"), restart: () => op("restart"), switchTo, reply: (value) => post("jupyter/kernel/op", { server, kid: k.id, op: "input", value }) };
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

// ---------- the notebook
function Notebook({ server, path, post, api, conns, settings, models, dirtyRef, onExpose, onOutline, jump }) {
  const [cells, setCells] = useState(null), [dirty, setDirty] = useState(false), [err, setErr] = useState(""), [sel, setSel] = useState(null), [run, setRun] = useState({}), [aiOpen, setAiOpen] = useState(null);
  const nbRef = useRef(null), cellsRef = useRef([]), dids = useRef({}), runAll = useRef(false);
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
  const insert = (at, type, extra = {}) => { const c = { key: uid(), type: type === "sql" ? "code" : type, src: "", outputs: [], raw: null, ...extra }; if (type === "sql") c.raw = withSql(c, { conn: (conns[0] || {}).name || "", var: "df", limit: 10000 }); setDirty(true); setCells((cs) => [...cs.slice(0, at), c, ...cs.slice(at)]); setSel(c.key); return c; };
  const move = (i, by) => { setDirty(true); setCells((cs) => { const j = i + by; if (j < 0 || j >= cs.length) return cs; const n = [...cs]; [n[i], n[j]] = [n[j], n[i]]; return n; }); };
  const remove = (key) => { setDirty(true); setCells((cs) => cs.filter((c) => c.key !== key)); };
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
    upd(key, (x) => ({ outputs: [], sqlres: null, cleared: false, raw: { ...(x.raw || {}), execution_count: null } }));
    try {
      if (sql) {
        if (!sql.conn) { toast("Pick a SQL connection for this cell"); return false; }
        const r = await post("jupyter/sql/run", { server, kid: kern.k.id, sql: c.src, conn: sql.conn, var: sql.var || "", limit: sql.limit || 10000 });
        const a = r.assigned, sum = r.ok ? `${(r.rows || []).length} row${(r.rows || []).length === 1 ? "" : "s"} × ${(r.columns || []).length} columns${a && a.ok ? " → DataFrame " + a.var : ""}` : "SQL failed";
        upd(key, { sqlres: r, outputs: r.ok ? [{ output_type: "display_data", data: { "text/plain": sum }, metadata: {} }] : [] });
        return !!r.ok && (!a || a.ok);
      }
      const d = {};
      const reply = await kern.exec(c.src, (e) => {
        if (e.t === "input_request") { const v = window.prompt(e.c.prompt || "Input", ""); kern.reply(v == null ? "" : v).catch(() => {}); return; }
        upd(key, (x) => ({ outputs: fold(x.outputs, d, e) }));
      });
      if (reply.lost) { toast(reply.error || "Lost the connection to the kernel"); return false; }
      upd(key, (x) => ({ raw: { ...(x.raw || {}), execution_count: reply.execution_count == null ? null : reply.execution_count } }));
      return reply.status !== "error";
    } catch (e) { toast(e.message); return false; }
    finally { setRun((r) => { const n = { ...r }; delete n[key]; return n; }); }
  };
  const runMany = async (keys) => { runAll.current = true; for (const k of keys) { if (!runAll.current) break; if (!(await runCell(k))) break; } runAll.current = false; };
  const interrupt = guard(async () => { runAll.current = false; await kern.interrupt(); });
  const restart = guard(async (thenAll) => {
    if (!(await confirmBox({ title: "Restart the kernel?", text: "All variables are lost.", ok: "Restart" }))) return;
    runAll.current = false; await kern.restart(); toast("Kernel restarting"); if (thenAll) setTimeout(() => runMany(cellsRef.current.filter((c) => c.type === "code").map((c) => c.key)), 1500);
  });
  const persist = async () => {
    const nb = nbRef.current, snap = cellsRef.current;
    if (kern.k.name && kern.specs.length) { const sp = kern.specs.find((x) => x.name === kern.k.name); if (sp) nb.metadata = { ...(nb.metadata || {}), kernelspec: { name: sp.name, display_name: sp.display, language: sp.language } }; }
    await post("jupyter/file/put", { server, path, type: "notebook", content: JSON.parse(serializeNotebook(nb, snap)) });
    if (cellsRef.current === snap) setDirty(false);                      // edited while saving: stay dirty, the next round saves it
  };
  const save = guard(async () => { await persist(); toast("Saved " + path.split("/").pop()); });
  const [addMenu, setAddMenu] = useState(false), [runMenu, setRunMenu] = useState(false), [fnd, setFnd] = useState(null), [ai, setAi] = useState({ s: "", m: "" }), findIn = useRef();
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
  useEffect(() => { if (onOutline && cells) onOutline(path, cells.flatMap((c, n) => (c.type === "markdown" ? c.src.split("\n").filter((l) => /^#{1,6}\s+\S/.test(l)).map((l) => ({ key: c.key, level: /^#+/.exec(l)[0].length, text: l.replace(/^#+\s+/, "").replace(/[*_`]/g, "").trim() })) : []))); }, [cells]);
  const addAfter = (t) => { const cs = cellsRef.current, i = cs.findIndex((c) => c.key === sel), at = i < 0 ? cs.length : i + 1; const c = insert(at, t === "ai" ? "code" : t); if (t === "ai") setAiOpen(c.key); };
  useEffect(() => { if (!jump || jump.path !== path) return; setSel(jump.key); requestAnimationFrame(() => { const el = document.getElementById("nbc-" + jump.key); if (el) el.scrollIntoView({ block: "start", behavior: "smooth" }); }); }, [jump]);
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
    else insert(i + 1, sqlOf(c) ? "sql" : "code", { src: prop.code });
  };
  const focusCell = (k) => setTimeout(() => { const t = document.querySelector("#nbc-" + k + " textarea"); if (t) { t.focus(); t.selectionStart = t.selectionEnd = t.value.length; } const el = document.getElementById("nbc-" + k); if (el) el.scrollIntoView({ block: "nearest" }); }, 40);
  const key = (c, i) => (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
    else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") { e.preventDefault(); openFind(); }
    else if (e.shiftKey && e.key === "Enter") {                       // run, then land in the next cell ready to type (a new one after the last)
      e.preventDefault(); if (c.type === "code") runCell(c.key);
      if (i === cellsRef.current.length - 1) { const nc = insert(i + 1, "code"); focusCell(nc.key); } else { const nk = cellsRef.current[i + 1].key; setSel(nk); focusCell(nk); }
    }
    else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); runCell(c.key); }
  };
  if (err) return html`<div class="empty"><div>${Ic("alert", 24)}<div>${err}</div></div></div>`;
  if (!cells) return html`<div class="empty"><div class="shine">Opening…</div></div>`;
  const adder = (at) => html`<div class="nb-add"><button type="button" onClick=${() => insert(at, "code")}>${Ic("plus", 12)} Code</button><button type="button" onClick=${() => insert(at, "markdown")}>${Ic("plus", 12)} Text</button>
    <button type="button" onClick=${() => insert(at, "sql")}>${Ic("database", 12)} SQL</button><button type="button" onClick=${() => { const c = insert(at, "code"); setAiOpen(c.key); }}>${Ic("sparkles", 12)} AI</button></div>`;
  const st = kern.k.state, busy = Object.keys(run).length > 0;
  const stateTag = html`<span class=${"tag " + (st === "idle" ? "good" : st === "busy" ? "acc" : st === "dead" ? "bad" : "")} title=${kern.k.err || ""}>${st === "busy" || st === "starting" ? html`<i class="spin"></i>` : Ic(st === "dead" ? "alert" : "check", 11)} ${st === "starting" ? "Starting kernel" : st === "dead" ? "Kernel offline" : st === "busy" ? "Busy" : "Idle"}</span>`;
  return html`<div class="nb nbj" onKeyDown=${(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") { e.preventDefault(); openFind(); } }}>
    <div class="nbj-bar nbj-colab">
      <div class="nbj-split"><button class="btn sm ghost" title="Add a code cell after the selected one" onClick=${() => addAfter("code")}>${Ic("plus", 14)} Code</button>
        <${Pop} open=${addMenu} setOpen=${setAddMenu} up=${false} width=${230} button=${html`<button class="btn sm ghost nbj-caret" aria-label="More cell types" onClick=${() => setAddMenu(!addMenu)}>${Ic("down", 12)}</button>`}>
          <div class="optlist">${[["code", "code", "Code cell", "Python or the kernel's language"], ["sql", "database", "SQL cell", "Query a database into a DataFrame"], ["ai", "sparkles", "Cell written by AI", "Describe it, review the code"]].map(([t, i, n, d]) => html`<button class="opt" onClick=${() => { setAddMenu(false); addAfter(t); }}><span class="oi">${Ic(i, 16)}</span><span class="grow"><b>${n}</b><span class="d">${d}</span></span></button>`)}</div><//></div>
      <button class="btn sm ghost" title="Add a text cell after the selected one" onClick=${() => addAfter("markdown")}>${Ic("plus", 14)} Text</button><span class="nbj-vr"></span>
      <div class="nbj-split"><button class="btn sm ghost" disabled=${busy} title="Run every code cell" onClick=${() => runMany(cells.filter((c) => c.type === "code").map((c) => c.key))}>${Ic("play", 13)} Run all</button>
        <${Pop} open=${runMenu} setOpen=${setRunMenu} up=${false} width=${250} button=${html`<button class="btn sm ghost nbj-caret" aria-label="More run options" onClick=${() => setRunMenu(!runMenu)}>${Ic("down", 12)}</button>`}>
          <div class="optlist">${[["Run selected cell", "play", "Shift+Enter", !sel || busy, () => sel && runCell(sel)], ["Run selected and below", "down", "From the selected cell to the end", !sel || busy, () => runMany(cells.slice(cells.findIndex((c) => c.key === sel)).filter((c) => c.type === "code").map((c) => c.key))], ["Run all above", "up", "Every cell before the selected one", !sel || busy, () => runMany(cells.slice(0, cells.findIndex((c) => c.key === sel)).filter((c) => c.type === "code").map((c) => c.key))], ["Interrupt execution", "stop", "", !busy, interrupt], ["Restart session", "refresh", "Clears all variables", false, () => restart(false)], ["Restart and run all", "refresh", "Fresh kernel, every cell", false, () => restart(true)]].map(([n, i, d, off, f]) => html`<button class="opt" disabled=${off} onClick=${() => { setRunMenu(false); f(); }}><span class="oi">${Ic(i, 16)}</span><span class="grow"><b>${n}</b><span class="d">${d}</span></span></button>`)}</div><//></div>
      ${busy && html`<button class="btn sm bad" onClick=${interrupt}>${Ic("stop", 12)} Interrupt</button>`}<span class="grow"></span>
      <select class="sq-conn" value=${kern.k.name} onChange=${(e) => kern.switchTo(e.target.value)} aria-label="Kernel" disabled=${!kern.specs.length}>${!kern.specs.length && html`<option value="">Kernel…</option>`}${kern.specs.map((s) => html`<option value=${s.name}>${s.display}</option>`)}</select>${stateTag}
      <button class="btn sm ghost" title="Find and replace (Ctrl/Cmd+F)" onClick=${openFind}>${Ic("search", 13)} Find</button><button class="btn sm ghost" title="Create a work that runs this notebook; chat can run it too" onClick=${() => onExpose(path)}>${Ic("flow", 13)} Use as node</button>
      <button class="btn sm ghost" disabled=${!dirty} onClick=${() => { setCells(null); load(); }}>Revert</button><button class="btn sm acc" disabled=${!dirty} onClick=${save}>${Ic("check", 12)} Save</button></div>
    ${fnd && html`<div class="nbj-find" role="search" onKeyDown=${(e) => { if (e.key === "Escape") { e.stopPropagation(); setFnd(null); } }}>
      <div class="nbj-ff"><input ref=${findIn} placeholder="Find" value=${fnd.q} onInput=${(e) => setFnd({ ...fnd, q: e.target.value, i: 0 })} onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); goto(fnd.i + (e.shiftKey ? -1 : 1)); } }} aria-label="Find"/>
        <button class=${"nbj-tg" + (fnd.cs ? " on" : "")} title="Match case" aria-pressed=${fnd.cs} onClick=${() => setFnd({ ...fnd, cs: !fnd.cs })}>Aa</button><button class=${"nbj-tg" + (fnd.rx ? " on" : "")} title="Regular expression" aria-pressed=${fnd.rx} onClick=${() => setFnd({ ...fnd, rx: !fnd.rx })}>.*</button></div>
      <span class=${"small " + (fnd.q && !matches.length ? "nbj-bad" : "mute")}>${!fnd.q ? "" : findRe() === null ? "Invalid pattern" : matches.length ? (fnd.i + 1) + " of " + matches.length : "No results"}</span>
      <button class="ibtn sm" title="Previous (Shift+Enter)" aria-label="Previous match" disabled=${!matches.length} onClick=${() => goto(fnd.i - 1)}>${Ic("up", 14)}</button><button class="ibtn sm" title="Next (Enter)" aria-label="Next match" disabled=${!matches.length} onClick=${() => goto(fnd.i + 1)}>${Ic("down", 14)}</button>
      <div class="nbj-ff"><input placeholder="Replace with" value=${fnd.r} onInput=${(e) => setFnd({ ...fnd, r: e.target.value })} aria-label="Replace with"/></div>
      <button class="btn sm" disabled=${!matches.length} onClick=${replaceOne}>Replace</button><button class="btn sm" disabled=${!matches.length} onClick=${replaceAll}>Replace all</button>
      <span class="grow"></span><button class="ibtn sm" title="Close (Esc)" aria-label="Close find" onClick=${() => setFnd(null)}>${Ic("x", 14)}</button></div>`}
    <div class="nb-cells">${!cells.length ? html`<div class="nbj-emptynb"><p class="mute">This notebook has no cells yet.</p>${adder(0)}</div>` : adder(0)}${cells.map((c, i) => {
      const sql = sqlOf(c), failed = !!errText(c), running = !!run[c.key], ec = c.raw && c.raw.execution_count;
      return html`<div key=${c.key} id=${"nbc-" + c.key} class=${"nb-cell " + c.type + (sql ? " sql" : "") + (sel === c.key ? " sel" : "")} onPointerDown=${() => setSel(c.key)}>
        <div class="nb-gut">${c.type === "code" ? html`<button class=${"nbj-run" + (running ? " busy" : "")} title=${running ? "Running…" : "Run cell (Shift+Enter)"} aria-label="Run cell" disabled=${running} onClick=${(e) => { e.stopPropagation(); runCell(c.key); }}>${running ? html`<i class="spin"></i>` : Ic("play", 13)}</button><span class="mono nbj-n">${running ? "*" : sql ? "sql" : ec ? "[" + ec + "]" : ""}</span>` : ""}</div>
        <div class="nb-body">
          <div class="nb-tools">            <button class="ibtn sm" title="Add a code cell below" aria-label="Add cell below" onClick=${() => { const nc = insert(i + 1, "code"); focusCell(nc.key); }}>${Ic("plus", 13)}</button>
            <button class="ibtn sm" title="Ask AI" aria-label="Ask AI" onClick=${() => setAiOpen(aiOpen === c.key ? null : c.key)}>${Ic("sparkles", 13)}</button>
            <select aria-label="Cell type" value=${sql ? "sql" : c.type} onChange=${(e) => { const t = e.target.value; upd(c.key, (x) => (t === "sql" ? { type: "code", raw: withSql(x, { conn: (conns[0] || {}).name || "", var: "df", limit: 10000 }) } : { type: t, raw: sqlOf(x) ? withSql(x, null) : x.raw })); }}><option value="code">Code</option><option value="sql">SQL</option><option value="markdown">Markdown</option><option value="raw">Raw</option></select>
            <button class="ibtn sm" aria-label="Move up" disabled=${!i} onClick=${() => move(i, -1)}>${Ic("up", 13)}</button><button class="ibtn sm" aria-label="Move down" disabled=${i === cells.length - 1} onClick=${() => move(i, 1)}>${Ic("down", 13)}</button>
            ${c.type === "code" && (c.outputs.length > 0 || c.sqlres) && html`<button class="ibtn sm" title="Clear output" aria-label="Clear output" onClick=${() => upd(c.key, { outputs: [], sqlres: null, cleared: true })}>${Ic("x", 13)}</button>`}
            <button class="ibtn sm bad" aria-label="Delete cell" onClick=${() => remove(c.key)}>${Ic("trash", 13)}</button></div>
          ${sql && html`<div class="nbj-sqlbar">${Ic("database", 13)}<b>SQL</b>
            <select value=${sql.conn} onChange=${(e) => upd(c.key, (x) => ({ raw: withSql(x, { ...sql, conn: e.target.value }) }))} aria-label="Connection"><option value="">Connection…</option>${conns.map((q) => html`<option value=${q.name}>${q.name}</option>`)}</select>
            <label class="mute small">into DataFrame <input class="nbj-var" placeholder="none" value=${sql.var || ""} onInput=${(e) => upd(c.key, (x) => ({ raw: withSql(x, { ...sql, var: e.target.value.replace(/[^A-Za-z0-9_]/g, "") }) }))} aria-label="Python variable"/></label>
            <label class="mute small">rows <select value=${sql.limit || 10000} onChange=${(e) => upd(c.key, (x) => ({ raw: withSql(x, { ...sql, limit: +e.target.value }) }))}>${[100, 1000, 10000, 50000, 200000].map((n) => html`<option value=${n}>${n.toLocaleString()}</option>`)}</select></label>
            ${!conns.length && html`<span class="mute small">Add a database in SQL Studio first.</span>`}</div>`}
          ${c.type === "markdown" && sel !== c.key ? html`<div class="nb-md" title="Click to edit" onClick=${() => setSel(c.key)}>${c.src.trim() ? html`<${Md} text=${c.src}/>` : html`<span class="mute small">Empty text cell: click to write.</span>`}</div>`
            : html`<${CodeBox} value=${c.src} lang=${sql ? "sql" : c.type === "code" ? lang : c.type === "markdown" ? "markdown" : ""} autofocus=${sel === c.key && !c.src}
                onChange=${(v) => upd(c.key, { src: v })} onKey=${key(c, i)} delay=${settings.suggest.on && c.type === "code" && !sql ? settings.suggest.delay : 0}
                intel=${c.type === "code"} marks=${marksOf(c.key)} onAi=${(s2, m) => setAi({ s: s2, m: m || "" })} words=${sql ? SQL_WORDS : wordsFor(c.key)}
                complete=${sql ? sqlComplete(post, (conns.find((q) => q.name === sql.conn) || {}).id || "") : kern.k.id && kern.k.state !== "dead" ? (code, pos) => kern.complete(code, pos) : null}
                signature=${c.type !== "code" ? null : sql ? sqlSignature : kern.k.id && kern.k.state !== "dead" ? (code, pos) => kern.signature(code, pos) : null} suggest=${(prefix, suffix) => post("jupyter/ai/suggest", { prefix, suffix, lang, cells: ctxFor(c.key)(), ...kern.live() }).then((r) => r.text || "")}/>`}
          ${aiOpen === c.key && html`<${AiBar} c=${c} sql=${sql ? { ...sql, conn: (conns.find((q) => q.name === sql.conn) || {}).id || sql.conn } : null} conns=${conns} post=${post} ctx=${ctxFor(c.key)} lang=${lang} apply=${aiApply(c, i)} close=${() => setAiOpen(null)} failed=${failed}/>`}
          ${c.type === "code" && (c.outputs.length > 0 || c.sqlres) && html`<div class="nb-outs">
            ${c.sqlres && c.sqlres.ok && c.sqlres.columns && c.sqlres.columns.length ? html`<${Grid} res=${c.sqlres} name=${(sql && sql.conn) || "result"}/>${c.sqlres.assigned && html`<div class=${"small " + (c.sqlres.assigned.ok ? "mute" : "nbj-bad")}>${c.sqlres.assigned.ok ? "Also in the kernel as DataFrame " : "Could not assign "}<code>${c.sqlres.assigned.var}</code>${c.sqlres.assigned.error ? ": " + c.sqlres.assigned.error : ""}</div>`}`
              : c.sqlres && !c.sqlres.ok ? html`<pre class="nb-o err">${c.sqlres.error}</pre>`
              : c.outputs.map((o, j) => html`<${Out} key=${j} o=${o} post=${post}/>`)}
            ${failed && !aiOpen && html`<div><button class="btn sm" onClick=${() => setAiOpen(c.key)}>${Ic("bolt", 12)} Fix with AI</button></div>`}</div>`}
        </div></div>${adder(i + 1)}`; })}</div>
    <div class="ebar"><span class=${saveErr ? "nbj-bad" : ""} title=${saveErr}><i class=${"dot " + (saveErr ? "dead" : dirty ? "busy" : "idle")}></i>${saveErr ? "Auto-save failed" : busySave ? "Saving…" : dirty ? (auto ? "Saving soon" : "Unsaved changes") : "Saved"}</span><label class="nbj-auto" title="Save changes automatically"><input type="checkbox" checked=${auto} onChange=${(e) => { setAuto(e.target.checked); LS.set("auto", e.target.checked); }}/> Auto-save</label><span>${cells.length} cell${cells.length === 1 ? "" : "s"}</span><span>${lang}</span><span class="grow"></span>
      ${settings.suggest.on && ai.s && html`<span class=${ai.s === "err" ? "nbj-bad" : ""} title=${ai.m || "AI suggestions"}>${ai.s === "busy" ? html`<i class="spin"></i> AI thinking…` : ai.s === "err" ? "AI suggestions failed: " + ai.m.slice(0, 60) : ai.s === "none" ? "AI: no suggestion" : "AI ready"}</span>`}
      <span class="hint"><kbd>Shift</kbd>+<kbd>Enter</kbd> run · <kbd>${/Mac/.test(navigator.platform) ? "⌘" : "Ctrl"}</kbd>+<kbd>S</kbd> save${settings.suggest.on ? html` · <kbd>Tab</kbd> accept` : ""}</span>
      <span title=${kern.k.err || ""}><i class=${"dot " + (st === "idle" ? "idle" : st === "busy" || st === "starting" ? "busy" : "dead")}></i>${kern.k.name || "No kernel"}</span></div></div>`;
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
function FileView({ server, path, post, dirtyRef }) {
  const [d, setD] = useState(null), [text, setText] = useState(""), [base, setBase] = useState(""), [mode, setMode] = useState(""), [frame, setFrame] = useState(""), h = useHl(), kind = previewKind(path);
  useEffect(() => { setD(null); post("jupyter/file/get", { server, path }).then((r) => { setD(r); setText(r.format === "text" ? r.content || "" : ""); setBase(r.format === "text" ? r.content || "" : ""); }).catch((e) => setD({ err: e.message })); }, [server, path]);
  const dirty = !!d && d.format === "text" && text !== base;
  dirtyRef.current = dirty;
  useEffect(() => () => { dirtyRef.current = false; }, []);
  const wr = (t) => post("jupyter/file/put", { server, path, type: "file", format: "text", content: t }).then(() => setBase(t));
  useEffect(() => { if (!dirty || !LS.get("auto", true)) return; const t = setTimeout(() => { wr(text).catch((e) => toast("Auto-save failed: " + e.message)); }, 1500); return () => clearTimeout(t); }, [text, dirty]);
  useEffect(() => { setMode(""); setFrame(""); }, [path]);
  const view = mode || (kind ? "preview" : "edit");
  useEffect(() => { if (kind === "html" && view === "preview" && d && d.format === "text") post("jupyter/frame", { html: text }).then((r) => setFrame("/nbframe/" + r.id)).catch(() => {}); }, [kind, view, d, base]);
  if (!d) return html`<div class="empty"><div class="shine">Opening…</div></div>`;
  if (d.err) return html`<div class="empty"><div>${Ic("alert", 24)}<div>${d.err}</div></div></div>`;
  if (d.format === "base64") return /^image\//.test(d.mimetype || "") ? html`<div class="nbj-img"><img alt=${path} src=${"data:" + d.mimetype + ";base64," + d.content}/></div>` : html`<div class="empty"><div>${Ic("file", 24)}<div>${path.split("/").pop()} is a binary file (${d.mimetype || "unknown type"}).</div></div></div>`;
  const save = guard(async () => { await wr(text); toast("Saved " + path.split("/").pop()); });
  const seg = kind ? html`<span class="nbj-pv"><button class=${view === "preview" ? "on" : ""} onClick=${() => setMode("preview")}>${Ic("eye", 13)} Preview</button><button class=${view === "edit" ? "on" : ""} onClick=${() => setMode("edit")}>${Ic("edit", 13)} Edit</button></span>` : "";
  if (kind && view === "preview") return html`<div class="nb nbj"><div class="nbj-pvbar">${seg}<span class="mute small grow">${path}</span></div><div class="nbj-pvbody">${kind === "md" ? html`<div class="mdfile"><${Md} text=${text}/></div>`
      : kind === "html" ? (frame ? html`<iframe class="nbj-pvframe" sandbox="allow-scripts allow-popups" src=${frame} title=${path}></iframe>` : html`<div class="empty"><div class="shine">Rendering…</div></div>`)
      : kind === "svg" ? html`<div class="nbj-img"><img alt=${path} src=${"data:image/svg+xml;charset=utf-8," + encodeURIComponent(text)}/></div>`
      : html`<${CsvView} text=${text} sep=${/\.tsv$/i.test(path) ? "\t" : ","}/>`}</div></div>`;
  return html`<div class="nb nbj">${seg && html`<div class="nbj-pvbar">${seg}<span class="mute small grow">${path}</span></div>`}<div class="nb-cells" style="padding:10px 14px"><${CodeBox} value=${text} lang=${langOf(h, path)} onChange=${setText} onKey=${(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } }} label="File text"/></div>
    <div class="ebar"><span class="mute small grow">${dirty ? "Unsaved changes" : "Saved"} · ${path}</span><button class="btn" disabled=${!dirty} onClick=${() => setText(base)}>Revert</button><button class="btn acc" disabled=${!dirty} onClick=${save}>${Ic("check", 13)} Save</button></div></div>`;
}

// ---------- remote terminal (xterm.js on the server's terminal)
function RemoteTerm({ server, post, api, close }) {
  const host = useRef(), [msg, setMsg] = useState("Connecting…");
  useEffect(() => {
    let dead = false, name = "", t;
    (async () => {
      try {
        const [{ Terminal }, { FitAddon }] = await loadXterm();
        const dark = matchMedia("(prefers-color-scheme: dark)").matches;
        t = new Terminal({ fontFamily: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace", fontSize: 12.5, cursorBlink: true, scrollback: 5000, theme: dark ? { background: "#0e0e10", foreground: "#e5e5ea" } : { background: "#1c1c1e", foreground: "#e5e5ea" } });
        const fit = new FitAddon(); t.loadAddon(fit); t.open(host.current); try { fit.fit(); } catch (e) { /* hidden */ }
        const r = await post("jupyter/term/new", { server }); name = r.name; setMsg("");
        let q = Promise.resolve();
        t.onData((d) => { for (let i = 0; i < d.length; i += 4000) { const part = d.slice(i, i + 4000); q = q.then(() => post("jupyter/term/write", { server, name, data: part })).catch(() => {}); } });
        const size = () => { try { fit.fit(); post("jupyter/term/write", { server, name, rows: t.rows, cols: t.cols }).catch(() => {}); } catch (e) { /* hidden */ } };
        const ro = new ResizeObserver(size); ro.observe(host.current); size();
        let after = 0, chid = 0;
        while (!dead) {
          const p = await api(`jupyter/poll?server=${enc(server)}&kind=t&id=${enc(name)}&after=${after}&wait=15`);
          if (dead) break;
          if (chid && p.ch !== chid) after = 0;
          chid = p.ch;
          for (const ev of p.events) { after = ev.n; try { const m = JSON.parse(ev.d); if (m[0] === "stdout") t.write(m[1]); } catch (e) { /* not a terminal frame */ } }
          if (p.closed) { t.write("\r\n[terminal closed]\r\n"); break; }
        }
        ro.disconnect();
      } catch (e) { if (!dead) setMsg(e.message); }
    })();
    return () => { dead = true; if (name) post("jupyter/term/close", { server, name }).catch(() => {}); if (t) t.dispose(); };
  }, [server]);
  return html`<div class="nbj-termw"><div class="row nbj-termh"><b>Terminal</b><span class="mute small grow">${msg}</span><button class="ibtn sm" aria-label="Close terminal" onClick=${close}>${Ic("x", 13)}</button></div><div class="nbj-term" ref=${host}></div></div>`;
}

// ---------- workspace: file panel + tabs
function Workspace({ server, servers, conns, settings, models, post, api, go, use, setSettings, onManage }) {
  const sid = server.id;
  const [dir, setDir] = useState(LS.get("dir_" + sid, "")), [kids, setKids] = useState({}), [exp, setExp] = useState(() => LS.get("exp_" + sid, [])), [err, setErr] = useState(""), [loading, setLoading] = useState({});
  const [tabs, setTabs] = useState(() => LS.get("tabs_" + sid, [])), [cur, setCur] = useState(() => LS.get("cur_" + sid, "")), [panel, setPanel] = useState(() => LS.get("panel", "files")), [outline, setOutline] = useState({}), [jump, setJump] = useState(null);
  const [term, setTerm] = useState(false), [termOn, setTermOn] = useState(false), [q, setQ] = useState(""), [newMenu, setNewMenu] = useState(false), dirties = useRef({}), fileIn = useRef();
  const loadDir = (d) => { setLoading((l) => ({ ...l, [d]: 1 })); return post("jupyter/ls", { server: sid, path: d }).then((r) => { setKids((k) => ({ ...k, [r.path || ""]: r.items })); setErr(""); }).catch((e) => { setErr(e.message); setKids((k) => ({ ...k, [d]: k[d] || [] })); }).finally(() => setLoading((l) => { const n = { ...l }; delete n[d]; return n; })); };
  const refresh = () => { const ds = new Set(["", ...exp]); ds.forEach((d) => loadDir(d)); };
  useEffect(() => { setKids({}); setErr(""); setDir(LS.get("dir_" + sid, "")); const e = LS.get("exp_" + sid, []); setExp(e); ["", ...e].forEach((d) => loadDir(d)); setTabs(LS.get("tabs_" + sid, [])); setCur(LS.get("cur_" + sid, "")); setTermOn(false); setTerm(false); }, [sid]);
  useEffect(() => { LS.set("tabs_" + sid, tabs); LS.set("cur_" + sid, cur); }, [tabs, cur, sid]);
  useEffect(() => { LS.set("exp_" + sid, exp); LS.set("dir_" + sid, dir); }, [exp, dir, sid]);
  useEffect(() => { LS.set("panel", panel); }, [panel]);
  const up = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
  const toggle = (x) => { setDir(x.path); if (exp.includes(x.path)) setExp(exp.filter((e) => e !== x.path)); else { setExp([...exp, x.path]); if (!kids[x.path]) loadDir(x.path); } };
  const open = (p) => { setTabs((t) => (t.includes(p) ? t : [...t, p])); setCur(p); setDir(up(p)); };
  const closeTab = async (p) => {
    if (dirties.current[p] && dirties.current[p].current && !(await confirmBox({ title: "Close without saving?", text: p.split("/").pop() + " has unsaved changes.", ok: "Close", danger: true }))) return;
    delete dirties.current[p]; const rest = tabs.filter((x) => x !== p); setTabs(rest); if (cur === p) setCur(rest[rest.length - 1] || "");
  };
  const dref = (p) => (dirties.current[p] ||= { current: false });
  const reveal = (d) => { if (d && !exp.includes(d)) setExp((e) => [...e, d]); };
  const mk = guard(async (type) => {
    const v = await ask({ title: (type === "notebook" ? "New notebook" : type === "file" ? "New file" : "New folder") + (dir ? " in " + dir : ""), submit: "Create", fields: [{ name: "name", label: "Name", type: "text", value: type === "notebook" ? "Untitled.ipynb" : type === "file" ? "untitled.txt" : "new-folder" }] });
    if (!v || !v.name.trim()) return;
    const r = await post("jupyter/file/new", { server: sid, dir, type, name: v.name.trim() }); reveal(dir); await loadDir(dir); if (type !== "directory") open(r.path);
  });
  const rename = guard(async (it) => {
    const v = await ask({ title: "Rename", submit: "Rename", fields: [{ name: "name", label: "New name", type: "text", value: it.name }] });
    if (!v || !v.name.trim() || v.name === it.name) return;
    const to = (up(it.path) ? up(it.path) + "/" : "") + v.name.trim(); await post("jupyter/file/rename", { server: sid, path: it.path, to });
    setTabs((t) => t.map((x) => (x === it.path ? to : x))); if (cur === it.path) setCur(to); loadDir(up(it.path));
  });
  const del = guard(async (it) => {
    if (!(await confirmBox({ title: "Delete " + it.name + "?", text: "It is removed from the Jupyter server.", ok: "Delete", danger: true }))) return;
    await post("jupyter/file/delete", { server: sid, path: it.path }); setTabs((t) => t.filter((x) => x !== it.path && !x.startsWith(it.path + "/"))); if (cur === it.path) setCur(""); loadDir(up(it.path));
  });
  const upload = guard(async (files) => {
    for (const f of files) {
      if (f.size > 25e6) { toast(f.name + " is over 25 MB"); continue; }
      const b = new Uint8Array(await f.arrayBuffer()); let s = ""; for (let i = 0; i < b.length; i += 32768) s += String.fromCharCode.apply(null, b.subarray(i, i + 32768));
      await post("jupyter/file/put", { server: sid, path: (dir ? dir + "/" : "") + f.name, type: "file", format: "base64", content: btoa(s) });
    }
    toast("Uploaded"); reveal(dir); loadDir(dir);
  });
  const expose = guard(async (path) => {
    if (!(await confirmBox({ title: "Use " + path.split("/").pop() + " as a node?", text: "A work is created (switched off) with a Jupyter notebook node. Parameters are read from the cell tagged `parameters` (or the first cell) and become the work's inputs. In chat you can also ask: run " + path + " on " + server.name + ".", ok: "Create work" }))) return;
    const r = await post("jupyter/expose", { server: sid, path }); toast("Work created"); location.hash = "#/works/" + r.id;
  });
  const ic = (x) => (x.type === "directory" ? "folder" : x.type === "notebook" ? "book" : "file");
  const rowOf = (x, depth) => {
    const isDir = x.type === "directory", on = isDir ? exp.includes(x.path) : false;
    return html`<div class=${"nbj-f" + (cur === x.path ? " on" : "") + (isDir && dir === x.path ? " tgt" : "")} key=${x.path} style=${{ paddingLeft: 6 + depth * 14 + "px", "--d": depth }} role="treeitem" aria-expanded=${isDir ? on : undefined} tabindex="0" title=${x.path}
      onClick=${() => (isDir ? toggle(x) : open(x.path))} onKeyDown=${(e) => { if (e.key === "Enter") (isDir ? toggle(x) : open(x.path)); }}>
      <span class=${"nbj-chev" + (on ? " o" : "") + (isDir ? "" : " none")}>${isDir ? Ic("right", 12) : ""}</span><span class=${"nbj-fi " + x.type}>${Ic(ic(x), 14)}</span>
      <span class=${"nbj-fn" + (isDir ? " dir" : "")}><span class="sq-ell">${x.name}</span></span>${isDir ? (kids[x.path] ? html`<span class="nbj-ct">${kids[x.path].length}</span>` : null) : (x.size != null ? html`<span class="nbj-ct">${x.size < 1024 ? x.size + "B" : x.size < 1048576 ? Math.round(x.size / 1024) + "K" : (x.size / 1048576).toFixed(1) + "M"}</span>` : null)}
      <span class="nbj-fa"><button class="ibtn sm" title="Rename" aria-label="Rename" onClick=${(e) => { e.stopPropagation(); rename(x); }}>${Ic("edit", 12)}</button><button class="ibtn sm" title="Delete" aria-label="Delete" onClick=${(e) => { e.stopPropagation(); del(x); }}>${Ic("trash", 12)}</button></span></div>`;
  };
  const branch = (d, depth) => [...(kids[d] || [])].sort((a, b) => (a.type === "directory") !== (b.type === "directory") ? (a.type === "directory" ? -1 : 1) : String(a.name).localeCompare(String(b.name))).flatMap((x) => [rowOf(x, depth), x.type === "directory" && exp.includes(x.path) ? (kids[x.path] ? (kids[x.path].length ? branch(x.path, depth + 1) : html`<div class="nbj-none" style=${{ paddingLeft: 36 + (depth + 1) * 14 + "px" }}>Empty</div>`) : html`<div class="nbj-none" style=${{ paddingLeft: 36 + (depth + 1) * 14 + "px" }}><i class="spin"></i></div>`) : null]);
  const flat = q.trim() ? Object.values(kids).flat().filter((x) => x.name.toLowerCase().includes(q.toLowerCase())) : null;
  const rail = (id, icon, label, on, f) => html`<button class=${"nbj-rb" + (on ? " on" : "")} title=${label} aria-label=${label} aria-pressed=${on} onClick=${f}>${Ic(icon, 20)}</button>`;
  const filesOpen = panel === "files", outOpen = panel === "outline", setOpenP = panel === "settings", anyPanel = filesOpen || outOpen || setOpenP, heads = outline[cur] || [];
  const onOutline = useCallback((p, l) => setOutline((o) => (JSON.stringify(o[p]) === JSON.stringify(l) ? o : { ...o, [p]: l })), []);
  return html`<div class=${"nbj-ws2" + (anyPanel ? "" : " nopanel")}>
    <div class="nbj-rail">${rail("files", "folder", "Files", filesOpen, () => setPanel(filesOpen ? "" : "files"))}${rail("outline", "list", "Table of contents", outOpen, () => setPanel(outOpen ? "" : "outline"))}${rail("term", "terminal", "Terminal on " + server.name, term, () => { setTerm(!term); setTermOn(true); })}<span class="nbj-rsep"></span>${rail("set", "sliders", "Settings: server and AI help", setOpenP, () => setPanel(setOpenP ? "" : "settings"))}</div>
    ${filesOpen && html`<aside class="nbj-panel" aria-label="Files">
      <div class="nbj-ph"><b>Files</b><span class="nbj-srvname sq-ell" title=${server.name}>${server.name}</span><span class="grow"></span><button class="ibtn sm" title="Close panel" aria-label="Close panel" onClick=${() => setPanel("")}>${Ic("x", 14)}</button></div>
      <div class="nbj-pill">
        <${Pop} open=${newMenu} setOpen=${setNewMenu} up=${false} width=${220} button=${html`<button class="ibtn sm" title=${"New in " + (dir || "top folder")} aria-label="New" onClick=${() => setNewMenu(!newMenu)}>${Ic("plus", 16)}</button>`}>
          <div class="optlist"><h5>New in ${dir || "top folder"}</h5>${[["notebook", "book", "Notebook", ".ipynb with a live kernel"], ["file", "file", "Text file", "Python, CSV, Markdown …"], ["directory", "folder", "Folder", "Group files together"]].map(([t, i, n, d]) => html`<button class="opt" onClick=${() => { setNewMenu(false); mk(t); }}><span class="oi">${Ic(i, 16)}</span><span class="grow"><b>${n}</b><span class="d">${d}</span></span></button>`)}</div><//>
        <button class="ibtn sm" title="Upload files here" aria-label="Upload" onClick=${() => fileIn.current.click()}>${Ic("upload", 16)}</button>
        <button class="ibtn sm" title="Refresh" aria-label="Refresh" onClick=${refresh}>${Ic("refresh", 16)}</button>
        <button class="ibtn sm" title="Top folder" aria-label="Top folder" onClick=${() => setDir("")}>${Ic("home", 16)}</button>
        <input class="nbj-q" placeholder="Filter" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Filter files"/><input type="file" multiple hidden ref=${fileIn} onChange=${(e) => { upload([...e.target.files]); e.target.value = ""; }}/></div>
      <div class="nbj-list" role="tree">
        ${err && html`<div class="sq-test bad small">${Ic("alert", 13)} <span>${err}</span></div>`}
        ${!kids[""] && !err && html`<p class="mute small" style="padding:6px 8px"><i class="spin"></i> Connecting to ${server.name}…</p>`}
        ${flat ? (flat.length ? flat.map((x) => rowOf(x, 0)) : html`<p class="mute small" style="padding:10px 8px">No file matches.</p>`) : html`${dir && html`<div class="nbj-f up" role="button" tabindex="0" onClick=${() => setDir(up(dir))}><span class="nbj-chev none"></span><span class="nbj-fi">${Ic("folder", 17)}</span><span class="nbj-fn">..</span></div>`}${branch("", 0)}`}
        ${kids[""] && !kids[""].length && !err && !q && html`<p class="mute small" style="padding:10px 8px">This server has no files yet. Use + or upload.</p>`}</div></aside>`}
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
    <section class="nbj-main"><div class="sq-tabs" role="tablist">${tabs.map((p) => html`<div key=${p} role="tab" aria-selected=${p === cur} class=${"sq-tab" + (p === cur ? " on" : "")} onClick=${() => setCur(p)}>${Ic(p.endsWith(".ipynb") ? "book" : "file", 12)}<span class="sq-ell">${p.split("/").pop()}</span>
          <button class="ibtn sm" aria-label="Close tab" onClick=${(e) => { e.stopPropagation(); closeTab(p); }}>${Ic("x", 11)}</button></div>`)}</div>
      <div class="nbj-stage">${!tabs.length && html`<div class="sq-empty big">${Ic("book", 28)}<div>Open a notebook from the file list</div><div class="mute small">Or create one with + — cells run on ${server.name}'s kernel.</div></div>`}
        ${tabs.map((p) => html`<div key=${sid + p} class="nbj-tab" hidden=${p !== cur}>${p.endsWith(".ipynb") ? html`<${Notebook} server=${sid} path=${p} post=${post} api=${api} conns=${conns} settings=${settings} models=${models} dirtyRef=${dref(p)} onExpose=${expose} onOutline=${onOutline} jump=${jump}/>` : html`<${FileView} server=${sid} path=${p} post=${post} dirtyRef=${dref(p)}/>`}</div>`)}</div>
      ${termOn && html`<div class="nbj-termbox" hidden=${!term}><${RemoteTerm} server=${sid} post=${post} api=${api} close=${() => { setTerm(false); setTermOn(false); }}/></div>`}</section></div>`;
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
    : html`<${Workspace} key=${server.id} server=${server} conns=${conns} settings=${d.settings} models=${models} post=${post} api=${api} go=${go} servers=${d.servers} use=${use} setSettings=${(x) => setD((y) => ({ ...y, settings: x }))} onManage=${() => go("servers")}/>`}</div>`;
}
