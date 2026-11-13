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
import { loadXterm } from "./term.js";

const LS = { get(k, d) { try { const v = localStorage.getItem("nbj_" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }, set(k, v) { try { localStorage.setItem("nbj_" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } } };
const enc = encodeURIComponent, sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const uid = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
const join = (s) => (Array.isArray(s) ? s.join("") : typeof s === "string" ? s : "");
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
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

// ---------- the code box: coloured text under a transparent textarea; AI inline suggestion shows as grey ghost text, Tab accepts
function CodeBox({ value, onChange, lang, onKey, suggest, delay, autofocus, label }) {
  const h = useHl(), ta = useRef(), tm = useRef(), tick = useRef(0), [ghost, setGhost] = useState("");
  useEffect(() => { if (autofocus && ta.current) ta.current.focus(); }, []);
  useEffect(() => () => clearTimeout(tm.current), []);
  const hl = useMemo(() => colorize(h, lang, value) + (ghost ? '<span class="nbj-ghost">' + esc(ghost) + "</span>" : "") + "\n", [h, lang, value, ghost]);
  const change = (v) => {
    setGhost(""); tick.current++; onChange(v); clearTimeout(tm.current);
    if (!suggest || !delay || v.trim().length < 2) return;
    const my = tick.current;
    tm.current = setTimeout(async () => {
      const t = ta.current;
      if (!t || t.value !== v || t.selectionStart !== v.length || document.activeElement !== t) return;      // only at the end of the cell, while you are in it
      try { const s = (await suggest(v)).replace(/\s+$/, ""); if (tick.current === my && s) setGhost(s); } catch (e) { /* suggestions never interrupt */ }
    }, delay);
  };
  const key = (e) => {
    if (e.key === "Tab" && !e.shiftKey && ghost) { e.preventDefault(); const v = value + ghost; setGhost(""); tick.current++; onChange(v); requestAnimationFrame(() => { if (ta.current) ta.current.selectionStart = ta.current.selectionEnd = v.length; }); return; }
    if (e.key === "Escape" && ghost) { e.preventDefault(); e.stopPropagation(); setGhost(""); return; }
    if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const t = e.target, a = t.selectionStart, b = t.selectionEnd; change(value.slice(0, a) + "    " + value.slice(b)); requestAnimationFrame(() => { t.selectionStart = t.selectionEnd = a + 4; }); return; }
    onKey && onKey(e);
  };
  return html`<div class="nb-src syn"><pre aria-hidden="true" dangerouslySetInnerHTML=${{ __html: hl }}></pre><textarea ref=${ta} spellcheck="false" value=${value} onInput=${(e) => change(e.target.value)} onKeyDown=${key} onBlur=${() => setGhost("")} aria-label=${label || "Cell source"}></textarea></div>`;
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
  return { k, specs, exec, interrupt: () => op("interrupt"), restart: () => op("restart"), switchTo, reply: (value) => post("jupyter/kernel/op", { server, kid: k.id, op: "input", value }) };
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
function Notebook({ server, path, post, api, conns, settings, models, dirtyRef, onExpose }) {
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
  const save = guard(async () => {
    const nb = nbRef.current;
    if (kern.k.name && kern.specs.length) { const s = kern.specs.find((x) => x.name === kern.k.name); if (s) nb.metadata = { ...(nb.metadata || {}), kernelspec: { name: s.name, display_name: s.display, language: s.language } }; }
    await post("jupyter/file/put", { server, path, type: "notebook", content: JSON.parse(serializeNotebook(nb, cellsRef.current)) });
    setDirty(false); toast("Saved " + path.split("/").pop());
  });
  const aiApply = (c, i) => (prop, mode) => {
    if (mode === "replace") upd(c.key, (x) => ({ src: prop.code, raw: sqlOf(x) && prop.conn ? withSql(x, { ...sqlOf(x), conn: (conns.find((q) => q.id === prop.conn) || {}).name || sqlOf(x).conn }) : x.raw }));
    else insert(i + 1, sqlOf(c) ? "sql" : "code", { src: prop.code });
  };
  const key = (c, i) => (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
    else if (e.shiftKey && e.key === "Enter") { e.preventDefault(); runCell(c.key); if (i === cellsRef.current.length - 1) insert(i + 1, "code"); else setSel(cellsRef.current[i + 1].key); }
    else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); runCell(c.key); }
  };
  if (err) return html`<div class="empty"><div>${Ic("alert", 24)}<div>${err}</div></div></div>`;
  if (!cells) return html`<div class="empty"><div class="shine">Opening…</div></div>`;
  const adder = (at) => html`<div class="nb-add"><button type="button" onClick=${() => insert(at, "code")}>${Ic("plus", 12)} Code</button><button type="button" onClick=${() => insert(at, "markdown")}>${Ic("plus", 12)} Text</button>
    <button type="button" onClick=${() => insert(at, "sql")}>${Ic("database", 12)} SQL</button><button type="button" onClick=${() => { const c = insert(at, "code"); setAiOpen(c.key); }}>${Ic("sparkles", 12)} AI</button></div>`;
  const st = kern.k.state, busy = Object.keys(run).length > 0;
  const stateTag = html`<span class=${"tag " + (st === "idle" ? "good" : st === "busy" ? "acc" : st === "dead" ? "bad" : "")} title=${kern.k.err || ""}>${st === "busy" || st === "starting" ? html`<i class="spin"></i>` : Ic(st === "dead" ? "alert" : "check", 11)} ${st === "starting" ? "Starting kernel" : st === "dead" ? "Kernel offline" : st === "busy" ? "Busy" : "Idle"}</span>`;
  return html`<div class="nb nbj" onKeyDown=${(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } }}>
    <div class="nbj-bar"><select class="sq-conn" value=${kern.k.name} onChange=${(e) => kern.switchTo(e.target.value)} aria-label="Kernel" disabled=${!kern.specs.length}>${!kern.specs.length && html`<option value="">Kernel…</option>`}${kern.specs.map((s) => html`<option value=${s.name}>${s.display}</option>`)}</select>${stateTag}
      <button class="btn sm acc" disabled=${!sel || busy} title="Run the selected cell (Shift+Enter)" onClick=${() => sel && runCell(sel)}>${Ic("play", 12)} Run</button>
      <button class="btn sm" disabled=${busy} onClick=${() => runMany(cells.filter((c) => c.type === "code").map((c) => c.key))}>${Ic("play", 12)} Run all</button>
      ${busy && html`<button class="btn sm bad" onClick=${interrupt}>${Ic("stop", 12)} Interrupt</button>`}
      <button class="btn sm" onClick=${() => restart(false)}>${Ic("refresh", 12)} Restart</button><button class="btn sm" title="Restart, then run every cell" onClick=${() => restart(true)}>${Ic("refresh", 12)} Restart + run all</button><span class="grow"></span>
      <button class="btn sm" title="Create a work that runs this notebook; chat can run it too" onClick=${() => onExpose(path)}>${Ic("flow", 12)} Use as node</button>
      <button class="btn sm" disabled=${!dirty} onClick=${() => { setCells(null); load(); }}>Revert</button><button class="btn sm acc" disabled=${!dirty} onClick=${save}>${Ic("check", 12)} Save</button></div>
    <div class="nb-cells">${adder(0)}${cells.map((c, i) => {
      const sql = sqlOf(c), failed = !!errText(c), running = !!run[c.key], ec = c.raw && c.raw.execution_count;
      return html`<div key=${c.key} class=${"nb-cell " + c.type + (sql ? " sql" : "") + (sel === c.key ? " sel" : "")} onPointerDown=${() => setSel(c.key)}>
        <div class="nb-gut">${c.type === "code" ? html`<span class="mono">[${running ? "*" : sql ? "sql" : ec || " "}]</span>` : ""}</div>
        <div class="nb-body">
          <div class="nb-tools">${c.type === "code" && html`<button class="ibtn sm" title="Run (Shift+Enter)" aria-label="Run" disabled=${running} onClick=${() => runCell(c.key)}>${Ic("play", 13)}</button>`}
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
                suggest=${(prefix) => post("jupyter/ai/suggest", { prefix, suffix: "", lang, cells: ctxFor(c.key)() }).then((r) => r.text || "")}/>`}
          ${aiOpen === c.key && html`<${AiBar} c=${c} sql=${sql ? { ...sql, conn: (conns.find((q) => q.name === sql.conn) || {}).id || sql.conn } : null} conns=${conns} post=${post} ctx=${ctxFor(c.key)} lang=${lang} apply=${aiApply(c, i)} close=${() => setAiOpen(null)} failed=${failed}/>`}
          ${c.type === "code" && (c.outputs.length > 0 || c.sqlres) && html`<div class="nb-outs">
            ${c.sqlres && c.sqlres.ok && c.sqlres.columns && c.sqlres.columns.length ? html`<${Grid} res=${c.sqlres} name=${(sql && sql.conn) || "result"}/>${c.sqlres.assigned && html`<div class=${"small " + (c.sqlres.assigned.ok ? "mute" : "nbj-bad")}>${c.sqlres.assigned.ok ? "Also in the kernel as DataFrame " : "Could not assign "}<code>${c.sqlres.assigned.var}</code>${c.sqlres.assigned.error ? ": " + c.sqlres.assigned.error : ""}</div>`}`
              : c.sqlres && !c.sqlres.ok ? html`<pre class="nb-o err">${c.sqlres.error}</pre>`
              : c.outputs.map((o, j) => html`<${Out} key=${j} o=${o} post=${post}/>`)}
            ${failed && !aiOpen && html`<div><button class="btn sm" onClick=${() => setAiOpen(c.key)}>${Ic("bolt", 12)} Fix with AI</button></div>`}</div>`}
        </div></div>${adder(i + 1)}`; })}</div>
    <div class="ebar"><span class="mute small grow">${dirty ? "Unsaved changes" : "Saved"} · ${cells.length} cell${cells.length === 1 ? "" : "s"} · ${lang} · Shift+Enter runs and moves on, ${/Mac/.test(navigator.platform) ? "⌘" : "Ctrl+"}S saves${settings.suggest.on ? ", Tab accepts a suggestion" : ""}</span></div></div>`;
}

// ---------- other files
function FileView({ server, path, post, dirtyRef }) {
  const [d, setD] = useState(null), [text, setText] = useState(""), [base, setBase] = useState(""), h = useHl();
  useEffect(() => { setD(null); post("jupyter/file/get", { server, path }).then((r) => { setD(r); setText(r.format === "text" ? r.content || "" : ""); setBase(r.format === "text" ? r.content || "" : ""); }).catch((e) => setD({ err: e.message })); }, [server, path]);
  const dirty = !!d && d.format === "text" && text !== base;
  dirtyRef.current = dirty;
  useEffect(() => () => { dirtyRef.current = false; }, []);
  if (!d) return html`<div class="empty"><div class="shine">Opening…</div></div>`;
  if (d.err) return html`<div class="empty"><div>${Ic("alert", 24)}<div>${d.err}</div></div></div>`;
  if (d.format === "base64") return /^image\//.test(d.mimetype || "") ? html`<div class="nbj-img"><img alt=${path} src=${"data:" + d.mimetype + ";base64," + d.content}/></div>` : html`<div class="empty"><div>${Ic("file", 24)}<div>${path.split("/").pop()} is a binary file (${d.mimetype || "unknown type"}).</div></div></div>`;
  const save = guard(async () => { await post("jupyter/file/put", { server, path, type: "file", format: "text", content: text }); setBase(text); toast("Saved " + path.split("/").pop()); });
  return html`<div class="nb nbj"><div class="nb-cells" style="padding:10px 14px"><${CodeBox} value=${text} lang=${langOf(h, path)} onChange=${setText} onKey=${(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } }} label="File text"/></div>
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
function Workspace({ server, conns, settings, models, post, api, go }) {
  const [dir, setDir] = useState(LS.get("dir_" + server.id, "")), [items, setItems] = useState(null), [err, setErr] = useState(""), [tabs, setTabs] = useState(() => LS.get("tabs_" + server.id, [])), [cur, setCur] = useState(() => LS.get("cur_" + server.id, ""));
  const [term, setTerm] = useState(false), [termOn, setTermOn] = useState(false), [q, setQ] = useState(""), dirties = useRef({}), fileIn = useRef();
  const sid = server.id;
  const load = (d = dir) => { setErr(""); post("jupyter/ls", { server: sid, path: d }).then((r) => { setItems(r.items); setDir(r.path); LS.set("dir_" + sid, r.path); }).catch((e) => { setErr(e.message); setItems([]); }); };
  useEffect(() => { setItems(null); load(LS.get("dir_" + sid, "")); setTabs(LS.get("tabs_" + sid, [])); setCur(LS.get("cur_" + sid, "")); setTermOn(false); setTerm(false); }, [sid]);
  useEffect(() => { LS.set("tabs_" + sid, tabs); LS.set("cur_" + sid, cur); }, [tabs, cur, sid]);
  const open = (p) => { setTabs((t) => (t.includes(p) ? t : [...t, p])); setCur(p); };
  const closeTab = async (p) => {
    if (dirties.current[p] && dirties.current[p].current && !(await confirmBox({ title: "Close without saving?", text: p.split("/").pop() + " has unsaved changes.", ok: "Close", danger: true }))) return;
    delete dirties.current[p]; const rest = tabs.filter((x) => x !== p); setTabs(rest); if (cur === p) setCur(rest[rest.length - 1] || "");
  };
  const dref = (p) => (dirties.current[p] ||= { current: false });
  const parent = dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : "";
  const mk = guard(async (type) => {
    const v = await ask({ title: type === "notebook" ? "New notebook" : type === "file" ? "New file" : "New folder", submit: "Create", fields: [{ name: "name", label: "Name", type: "text", value: type === "notebook" ? "Untitled.ipynb" : type === "file" ? "untitled.txt" : "new-folder" }] });
    if (!v || !v.name.trim()) return;
    const r = await post("jupyter/file/new", { server: sid, dir, type, name: v.name.trim() }); load(); if (type !== "directory") open(r.path);
  });
  const rename = guard(async (it) => {
    const v = await ask({ title: "Rename", submit: "Rename", fields: [{ name: "name", label: "New name", type: "text", value: it.name }] });
    if (!v || !v.name.trim() || v.name === it.name) return;
    const to = (dir ? dir + "/" : "") + v.name.trim(); await post("jupyter/file/rename", { server: sid, path: it.path, to });
    setTabs((t) => t.map((x) => (x === it.path ? to : x))); if (cur === it.path) setCur(to); load();
  });
  const del = guard(async (it) => {
    if (!(await confirmBox({ title: "Delete " + it.name + "?", text: "It is removed from the Jupyter server.", ok: "Delete", danger: true }))) return;
    await post("jupyter/file/delete", { server: sid, path: it.path }); setTabs((t) => t.filter((x) => x !== it.path)); if (cur === it.path) setCur(""); load();
  });
  const upload = guard(async (files) => {
    for (const f of files) {
      if (f.size > 25e6) { toast(f.name + " is over 25 MB"); continue; }
      const b = new Uint8Array(await f.arrayBuffer()); let s = ""; for (let i = 0; i < b.length; i += 32768) s += String.fromCharCode.apply(null, b.subarray(i, i + 32768));
      await post("jupyter/file/put", { server: sid, path: (dir ? dir + "/" : "") + f.name, type: "file", format: "base64", content: btoa(s) });
    }
    toast("Uploaded"); load();
  });
  const expose = guard(async (path) => {
    if (!(await confirmBox({ title: "Use " + path.split("/").pop() + " as a node?", text: "A work is created (switched off) with a Jupyter notebook node. Parameters are read from the cell tagged `parameters` (or the first cell) and become the work's inputs. In chat you can also ask: run " + path + " on " + server.name + ".", ok: "Create work" }))) return;
    const r = await post("jupyter/expose", { server: sid, path }); toast("Work created"); location.hash = "#/works/" + r.id;
  });
  const shown = (items || []).filter((x) => !q.trim() || x.name.toLowerCase().includes(q.toLowerCase()));
  const ic = (x) => (x.type === "directory" ? "folder" : x.type === "notebook" ? "book" : "file");
  const crumbs = dir ? dir.split("/") : [];
  return html`<div class="sq-studio nbj-ws"><div class="sq-tree"><div class="sq-th"><b>Files</b><span class="grow"></span>
      <button class="ibtn sm" title="New notebook" aria-label="New notebook" onClick=${() => mk("notebook")}>${Ic("plus", 13)}</button><button class="ibtn sm" title="New file" aria-label="New file" onClick=${() => mk("file")}>${Ic("file", 13)}</button>
      <button class="ibtn sm" title="New folder" aria-label="New folder" onClick=${() => mk("directory")}>${Ic("folder", 13)}</button><button class="ibtn sm" title="Upload" aria-label="Upload" onClick=${() => fileIn.current.click()}>${Ic("upload", 13)}</button>
      <button class="ibtn sm" title="Refresh" aria-label="Refresh" onClick=${() => load()}>${Ic("refresh", 13)}</button><input type="file" multiple hidden ref=${fileIn} onChange=${(e) => { upload([...e.target.files]); e.target.value = ""; }}/></div>
    <div class="nbj-crumbs"><button onClick=${() => load("")}>${server.name}</button>${crumbs.map((c, i) => html`<span>/</span><button onClick=${() => load(crumbs.slice(0, i + 1).join("/"))}>${c}</button>`)}</div>
    <input class="sq-find" placeholder="Filter" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Filter files"/>
    <div class="sq-tl">${items === null && html`<p class="mute small" style="padding:6px 8px"><i class="spin"></i> Connecting to ${server.name}…</p>`}${err && html`<div class="sq-test bad small">${Ic("alert", 13)} <span>${err}</span></div>`}
      ${dir && html`<div class="sq-tr" onClick=${() => load(parent)}>${Ic("up", 13)}<span class="grow">..</span></div>`}
      ${shown.map((x) => html`<div class=${"sq-tr nbj-f" + (cur === x.path ? " on" : "")} key=${x.path} onClick=${() => (x.type === "directory" ? load(x.path) : open(x.path))} title=${x.path}>${Ic(ic(x), 13)}<span class="sq-ell grow">${x.name}</span>
        <button class="ibtn sm" aria-label="Rename" onClick=${(e) => { e.stopPropagation(); rename(x); }}>${Ic("edit", 12)}</button><button class="ibtn sm" aria-label="Delete" onClick=${(e) => { e.stopPropagation(); del(x); }}>${Ic("trash", 12)}</button></div>`)}
      ${items && !shown.length && !err && html`<p class="mute small" style="padding:6px 8px">${q ? "No file matches." : "This folder is empty."}</p>`}</div></div>
    <div class="sq-main"><div class="sq-tabs" role="tablist">${tabs.map((p) => html`<div key=${p} role="tab" aria-selected=${p === cur} class=${"sq-tab" + (p === cur ? " on" : "")} onClick=${() => setCur(p)}>${Ic(p.endsWith(".ipynb") ? "book" : "file", 12)}<span class="sq-ell">${p.split("/").pop()}</span>
          <button class="ibtn sm" aria-label="Close tab" onClick=${(e) => { e.stopPropagation(); closeTab(p); }}>${Ic("x", 11)}</button></div>`)}<span class="grow"></span>
        <button class=${"btn sm" + (term ? " acc" : "")} onClick=${() => { setTerm(!term); setTermOn(true); }}>${Ic("terminal", 12)} Terminal</button></div>
      <div class="nbj-stage">${!tabs.length && html`<div class="sq-empty big">${Ic("book", 28)}<div>Open a notebook from the file list</div><div class="mute small">Or create one with + — cells run on ${server.name}'s kernel.</div></div>`}
        ${tabs.map((p) => html`<div key=${sid + p} class="nbj-tab" hidden=${p !== cur}>${p.endsWith(".ipynb") ? html`<${Notebook} server=${sid} path=${p} post=${post} api=${api} conns=${conns} settings=${settings} models=${models} dirtyRef=${dref(p)} onExpose=${expose}/>` : html`<${FileView} server=${sid} path=${p} post=${post} dirtyRef=${dref(p)}/>`}</div>`)}</div>
      ${termOn && html`<div class="nbj-termbox" hidden=${!term}><${RemoteTerm} server=${sid} post=${post} api=${api} close=${() => { setTerm(false); setTermOn(false); }}/></div>`}</div></div>`;
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

function ServerForm({ init, post, close, saved }) {
  const [f, setF] = useState(() => ({ kind: "hub", name: "", url: "", user: "", token: "", verify: true, kernel: "", env: "jupyter", root: "", ...init })), [busy, setBusy] = useState(false), [test, setTest] = useState(null);
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target ? e.target.value : e }));
  const save = guard(async () => { setBusy(true); try { saved((await post("jupyter/save", { server: f })).server); } finally { setBusy(false); } });
  const check = guard(async () => { setBusy(true); setTest(null); try { const s = await post("jupyter/save", { server: f }); setF((x) => ({ ...x, id: s.server.id, token: "", has_secret: s.server.has_secret })); setTest(await post("jupyter/test", { server: s.server.id }).catch((e) => ({ ok: false, error: e.message }))); } finally { setBusy(false); } });
  const field = (label, k, ph, type = "text", help) => html`<label class="sq-f"><span>${label}</span><input type=${type} value=${f[k]} placeholder=${ph || ""} onInput=${set(k)} autocomplete="off"/>${help && html`<small class="mute">${help}</small>`}</label>`;
  const local = f.kind === "local";
  return html`<${Modal} close=${close} wide=${true}><div class="sq-form"><h3>${f.id ? "Edit server" : "Add a Jupyter server"}</h3>
    <div class="sq-tiles">${Object.keys(KIND).map((k) => html`<button type="button" class=${"sq-tile" + (f.kind === k ? " on" : "")} onClick=${() => setF((x) => ({ ...x, kind: k }))}><span>${KIND[k]}</span></button>`)}</div>
    ${field("Name", "name", local ? "Local Jupyter" : "Team hub")}
    ${f.kind === "hub" && html`${field("JupyterHub address", "url", "https://hub.example.com")}${field("Your user name", "user", "alice")}
      ${field("API token" + (f.has_secret && !f.token ? " (saved: leave blank to keep)" : ""), "token", "", "password", "Hub > Token > Request new API token. Your server is started automatically when it is not running.")}`}
    ${f.kind === "server" && html`${field("Server address", "url", "http://localhost:8888  or  https://host/jupyter")}${field("Token" + (f.has_secret && !f.token ? " (saved: leave blank to keep)" : ""), "token", "", "password", "From the URL Jupyter prints on start (?token=…), or leave blank when the server has none.")}`}
    ${local && html`<div class="sq-grid2">${field("Python environment", "env", "jupyter", "text", "Created and filled by Set up.")}${field("Notebook folder (optional)", "root", "~/notebooks")}</div>`}
    ${!local && html`<div class="row sq-ro"><${Switch} on=${f.verify} onChange=${(v) => setF((x) => ({ ...x, verify: v }))} label="Verify TLS"/><div><b>Verify the TLS certificate</b><div class="mute small">Turn off only for a self-signed server you trust.</div></div></div>`}
    ${test && html`<div class=${"sq-test " + (test.ok ? "ok" : "bad")}>${test.ok ? html`${Ic("check", 15)} Connected · Jupyter ${test.version} · kernels: ${(test.kernelspecs || []).map((s) => s.display).join(", ") || "none"}` : html`${Ic("alert", 15)} <span>${test.error}</span>`}</div>`}
    <div class="row sq-fa">${!local && html`<button class="btn" disabled=${busy || !f.name.trim()} onClick=${check}>${busy ? html`<i class="spin"></i>` : Ic("bolt", 14)} Save and test</button>`}<span class="grow"></span>
      <button class="btn" onClick=${close}>Cancel</button><button class="btn acc" disabled=${busy || !f.name.trim()} onClick=${save}>Save</button></div></div><//>`;
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
      toast("Local server added: press Set up on its card, then Start.");
      use(s.id, true);
    } finally { setOne(false); }
  });
  return html`<div class="sq-pane"><div class="row"><div class="grow"><h2 style="margin:0">Servers</h2><p class="mute small" style="margin:2px 0 0">Tokens are stored encrypted on this machine and never sent to the browser.</p></div>
      <button class="btn acc" onClick=${() => setEdit({})}>${Ic("plus", 14)} Add server</button></div>
    ${!servers.length && html`<div class="sq-hero">${Ic("book", 34)}<h3>Connect a Jupyter server</h3><p class="mute">Use your team's JupyterHub or any Jupyter server, or let AI Hub install and run one on this computer in one step.</p>
      <div class="row" style="gap:8px;justify-content:center"><button class="btn acc" disabled=${one} onClick=${oneClick}>${one ? html`<i class="spin"></i>` : Ic("zap", 14)} Set up a local server</button><button class="btn" onClick=${() => setEdit({})}>${Ic("plus", 14)} Add existing server</button></div></div>`}
    <div class="sq-cards">${servers.map((s) => html`<div class="sq-card" key=${s.id}><div class="row"><span class="sq-badge">${s.name[0]}</span><div class="grow"><b>${s.name}</b><div class="mute small sq-ell">${KIND[s.kind]}${s.url ? " · " + s.url : ""}${s.user ? " · " + s.user : ""}</div></div></div>
      ${s.kind === "local" && html`<${LocalCard} s=${s} post=${post} api=${api} reload=${reload}/>`}
      <div class="row sq-ca"><span class="grow"></span><button class="btn sm acc" onClick=${() => use(s.id)}>${Ic("play", 12)} Open</button><button class="ibtn" aria-label="Edit" onClick=${() => setEdit(s)}>${Ic("edit", 14)}</button>
        <button class="ibtn" aria-label="Delete" onClick=${guard(async () => { if (await confirmBox({ title: "Remove " + s.name + "?", text: "Only the saved connection is removed; files on the server stay.", ok: "Remove", danger: true })) { await post("jupyter/delete", { id: s.id }); reload(); } })}>${Ic("trash", 14)}</button></div></div>`)}</div>
    ${servers.length > 0 && !servers.some((x) => x.kind === "local") && html`<p class="mute small" style="margin-top:14px">No server of your own? <button class="btn sm" onClick=${oneClick}>${Ic("zap", 12)} Set up a local server</button></p>`}
    ${edit && html`<${ServerForm} init=${edit} post=${post} close=${() => setEdit(null)} saved=${(s) => { setEdit(null); reload(); toast("Saved " + s.name); }}/>`}</div>`;
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
  return html`<div class="sqs nbjp"><div class="sq-head"><span class="sq-logo">${Ic("book", 18)}</span><div class="grow"><h1 style="font-size:20px;margin:0">Notebook</h1><div class="mute small">Jupyter notebooks with a live kernel, SQL cells, a terminal and AI help</div></div>
      ${server && d.servers.length > 1 && view === "work" && html`<select class="sq-conn" value=${server.id} onChange=${(e) => use(e.target.value, true)} aria-label="Jupyter server">${d.servers.map((s) => html`<option value=${s.id}>${s.name}</option>`)}</select>`}
      <${Seg} value=${view} onChange=${go} items=${items}/></div>
    ${err && html`<div class="sq-test bad" style="margin:12px 20px">${Ic("alert", 15)} ${err}</div>`}
    ${!loaded ? html`<div class="sq-pane"><p class="mute">Loading…</p></div>`
    : view === "servers" || (view === "work" && !server) ? html`<${Servers} servers=${d.servers} post=${post} api=${api} reload=${reload} use=${use} edit=${edit} setEdit=${setEdit}/>`
    : view === "ai" ? html`<div class="sq-pane"><h2 style="margin:0 0 4px">AI models</h2><p class="mute small" style="margin:0 0 14px">Pick a model for each kind of help. They are used for every notebook.</p><${AiSettings} settings=${d.settings} models=${models} post=${post} setSettings=${(s) => setD((x) => ({ ...x, settings: s }))}/></div>`
    : html`<${Workspace} key=${server.id} server=${server} conns=${conns} settings=${d.settings} models=${models} post=${post} api=${api} go=${go}/>`}</div>`;
}
