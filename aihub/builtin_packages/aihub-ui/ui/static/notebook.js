// Jupyter notebook (.ipynb) editor for the canvas: cells are edited in place (code with highlighting, markdown rendered until clicked),
// added, removed, moved and retyped; outputs are shown (text, errors, images, sanitized HTML) and can be cleared. Saving writes nbformat 4
// JSON (1-space indent, `source` as a list of lines, everything else of the file kept) and refuses when the file changed on disk since it was read.
// There is no kernel here: run the notebook in Jupyter / VS Code, the outputs it writes show up on the next load.
import { html, useState, useEffect, useRef, useMemo } from "./vendor/preact-htm.js";
import DOMPurify from "./vendor/purify.js";
import { Ic } from "./icons.js";
import { toast, guard, ask, confirmBox } from "./ui.js";
import { Md } from "./blocks.js";
import { useHl, colorize, isMac } from "./code.js";

const MAX_OUT = 200000;                                                  // chars of one text output shown (the file keeps all of it)
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
const joinSrc = (s) => (Array.isArray(s) ? s.join("") : typeof s === "string" ? s : "");
const toSrc = (t) => { const out = t.split(/(?<=\n)/); return t === "" ? [] : out; };       // nbformat: a list of lines, each but the last ending in \n
const newId = () => Math.random().toString(16).slice(2, 10) + Math.random().toString(16).slice(2, 6);
const langOfNb = (nb) => String(((nb.metadata || {}).language_info || {}).name || ((nb.metadata || {}).kernelspec || {}).language || "python").toLowerCase();

export function parseNotebook(text) {
  const nb = JSON.parse(text);
  if (!nb || typeof nb !== "object" || !Array.isArray(nb.cells)) throw new Error("not a Jupyter notebook (no cells)");
  return nb;
}
// -> the file text for NB with CELLS ({type, src, raw}): each cell keeps the fields it had (outputs, metadata, attachments, id)
export function serializeNotebook(nb, cells) {
  const minor = nb.nbformat === 4 ? nb.nbformat_minor || 0 : 5, ids = minor >= 5;
  const out = { ...nb, nbformat: 4, nbformat_minor: minor, cells: cells.map((c) => {
    const base = { ...(c.raw || {}) }; base.cell_type = c.type; base.source = toSrc(c.src); base.metadata = base.metadata || {};
    if (ids && !base.id) base.id = c.key;
    if (c.type === "code") { base.outputs = c.outputs || []; base.execution_count = c.cleared ? null : base.execution_count ?? null; }
    else { delete base.outputs; delete base.execution_count; }
    return base;
  }) };
  return JSON.stringify(out, null, 1) + "\n";
}
export const cellsOf = (nb) => nb.cells.map((c) => ({ key: c.id || newId(), type: c.cell_type === "markdown" || c.cell_type === "raw" ? c.cell_type : "code", src: joinSrc(c.source), outputs: c.outputs || [], raw: c }));

export function CellCode({ value, onChange, lang, autofocus, onKey }) {
  const h = useHl(), ta = useRef();
  useEffect(() => { if (autofocus && ta.current) ta.current.focus(); }, []);
  const hl = useMemo(() => colorize(h, lang, value) + "\n", [h, lang, value]);
  const key = (e) => {
    if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const t = e.target, a = t.selectionStart, b = t.selectionEnd; onChange(value.slice(0, a) + "    " + value.slice(b)); requestAnimationFrame(() => { t.selectionStart = t.selectionEnd = a + 4; }); return; }
    onKey && onKey(e);
  };
  return html`<div class="nb-src syn"><pre aria-hidden="true" dangerouslySetInnerHTML=${{ __html: hl }}></pre><textarea ref=${ta} spellcheck="false" value=${value} onInput=${(e) => onChange(e.target.value)} onKeyDown=${key} aria-label="Cell source"></textarea></div>`;
}
// sanitising HTML is the expensive part of showing an output: do it once per distinct text, and reuse the whole rendered output while the output object is unchanged
const sanCache = new Map();
function clean(src, cfg) {
  const k = src.length + ":" + src.slice(0, 80) + src.slice(-80) + (cfg.USE_PROFILES ? "s" : "h");
  const hit = sanCache.get(k);
  if (hit && hit.src === src) return hit.out;
  const out = DOMPurify.sanitize(src, cfg);
  sanCache.set(k, { src, out });
  if (sanCache.size > 120) sanCache.delete(sanCache.keys().next().value);
  return out;
}
const outCache = new WeakMap();
export function Output({ o }) {
  if (!o || typeof o !== "object") return renderOutput(o || {});
  let v = outCache.get(o);
  if (v === undefined) { v = renderOutput(o); outCache.set(o, v); }
  return v;
}
// a terminal's carriage return: what follows \r writes over the start of the line (tqdm, pip, any progress bar). A trailing \r is kept so the
// next chunk of the stream still overwrites the line; \r\n is a plain line end. Folding while streaming keeps a long progress loop small.
export function fixCR(s) {
  if (typeof s !== "string" || s.indexOf("\r") < 0) return s;
  return s.replace(/\r+\n/g, "\n").split("\n").map((l) => {
    if (l.indexOf("\r") < 0) return l;
    const parts = l.split("\r"), pend = parts[parts.length - 1] === "";
    let out = ""; for (const p of parts) out = p + out.slice(p.length);
    return pend ? out + "\r" : out;
  }).join("\n");
}
// ---------- ipywidgets (tqdm.notebook / tqdm.auto progress bars, labels, boxes): the kernel's comm messages keep the models here,
// a widget output shows its live model. widgetSnap() writes the model as text into text/plain so a saved notebook still shows the last state.
export const W_MIME = "application/vnd.jupyter.widget-view+json";
const wModels = new Map(), wSubs = new Set();
let wQueued = false;
const wRef = (v) => (typeof v === "string" && v.startsWith("IPY_MODEL_") ? v.slice(10) : "");
const wPing = () => { if (wQueued) return; wQueued = true; requestAnimationFrame(() => { wQueued = false; wSubs.forEach((f) => f()); }); };
// kernel event {t, c} -> true when it is a comm message (handled here, never an output of a cell)
export function widgetEvent(e) {
  if (!e || !/^comm_(open|msg|close)$/.test(e.t)) return false;
  const c = e.c || {}, id = c.comm_id, d = c.data || {};
  if (!id) return true;
  if (e.t === "comm_open") { if (/jupyter\.widget$/.test(c.target_name || "")) wModels.set(id, { ...(d.state || {}) }); else return true; }
  else if (e.t === "comm_msg") { const m = wModels.get(id); if (!m || (d.method !== "update" && d.method !== "echo_update")) return true; wModels.set(id, { ...m, ...(d.state || {}) }); }
  else { const m = wModels.get(id); if (!m) return true; wModels.set(id, { ...m, _closed: true }); }
  if (wModels.size > 5000) wModels.delete(wModels.keys().next().value);
  wPing(); return true;
}
const htmlText = (v) => { try { return new DOMParser().parseFromString(String(v || ""), "text/html").body.textContent.replace(/\u00a0/g, " "); } catch (e) { return String(v || ""); } };
const wKind = (m) => String(m._model_name || "").replace(/Model$/, "");
const wHidden = (m) => { const l = wModels.get(wRef(m.layout)) || {}; return m._closed || l.display === "none" || l.visibility === "hidden"; };
function wText(id, depth = 0) {
  const m = wModels.get(id); if (!m || depth > 8 || wHidden(m)) return "";
  const k = wKind(m), desc = m.description ? m.description + " " : "";
  if (/Box$/.test(k)) return (m.children || []).map((c) => wText(wRef(c), depth + 1)).join(/^VBox|GridBox$/.test(k) ? "\n" : "");
  if (/Progress$/.test(k)) { const lo = +m.min || 0, hi = +m.max || 0, f = hi > lo ? Math.max(0, Math.min(1, ((+m.value || 0) - lo) / (hi - lo))) : 0, n = Math.round(f * 10); return desc + "|" + "█".repeat(n) + " ".repeat(10 - n) + "|"; }
  if (/^HTML/.test(k)) return desc + htmlText(m.value);
  return m.value != null && typeof m.value !== "object" ? desc + String(m.value) : desc.trim();
}
// outputs -> the same with each live widget's text/plain set to what it shows now (call when a cell is done)
export function widgetSnap(outs) {
  let ch = false;
  const r = (outs || []).map((o) => { const v = o && o.data && o.data[W_MIME], id = v && v.model_id; if (!id || !wModels.has(id)) return o; const t = wText(id); if (!t || t === joinSrc(o.data["text/plain"])) return o; ch = true; return { ...o, data: { ...o.data, "text/plain": t } }; });
  return ch ? r : outs;
}
function wView(id, depth) {
  const m = wModels.get(id); if (!m || depth > 8 || wHidden(m)) return null;
  const k = wKind(m), desc = m.description && html`<span class="nbw-d">${m.description}</span>`;
  if (/Box$/.test(k)) return html`<div class=${"nbw-box" + (/^VBox|GridBox$/.test(k) ? " col" : "")}>${(m.children || []).map((c) => wView(wRef(c), depth + 1))}</div>`;
  if (/Progress$/.test(k)) { const lo = +m.min || 0, hi = +m.max || 0, f = hi > lo ? Math.max(0, Math.min(1, ((+m.value || 0) - lo) / (hi - lo))) : 0;
    return html`<span class="nbw-pw">${desc}<span class=${"nbw-bar " + (m.bar_style || "")} role="progressbar" aria-valuemin=${lo} aria-valuemax=${hi} aria-valuenow=${+m.value || 0}><i style=${"width:" + (f * 100).toFixed(2) + "%"}></i></span></span>`; }
  if (/^HTML/.test(k)) return html`<span class="nbw-h">${desc}<span dangerouslySetInnerHTML=${{ __html: clean(String(m.value || ""), { FORBID_TAGS: ["style", "form", "input", "button"] }) }}></span></span>`;
  if (k === "Label" || (m.value != null && typeof m.value !== "object")) return html`<span class="nbw-h">${desc}<span>${String(m.value ?? "")}</span></span>`;
  return desc || null;
}
function WidgetOut({ o }) {
  const [, tick] = useState(0);
  useEffect(() => { const f = () => tick((n) => n + 1); wSubs.add(f); return () => { wSubs.delete(f); }; }, []);
  const d = o.data || {}, id = (d[W_MIME] || {}).model_id;
  if (id && wModels.has(id)) { const v = wView(id, 0); return v ? html`<div class="nb-o nbw">${v}</div>` : null; }
  const s = joinSrc(d["text/plain"]).replace(ANSI, "");
  if (!s || /^\w+\((children|value|layout|description)=/.test(s)) return html`<div class="nb-o mute small">Interactive widget: run the cell to see it.</div>`;
  return html`<pre class="nb-o">${s.slice(0, MAX_OUT)}</pre>`;
}
function renderOutput(o) {
  const t = o.output_type, d = o.data || {};
  if (t === "stream") { const s = fixCR(joinSrc(o.text)).replace(/\r/g, "").replace(ANSI, ""); return html`<pre class=${"nb-o " + (o.name === "stderr" ? "err" : "")}>${s.slice(0, MAX_OUT)}${s.length > MAX_OUT ? "\n…" : ""}</pre>`; }
  if (t === "error") return html`<pre class="nb-o err">${((o.traceback || []).join("\n") || (o.ename + ": " + o.evalue)).replace(ANSI, "").slice(0, MAX_OUT)}</pre>`;
  if (t === "execute_result" || t === "display_data") {
    if (d[W_MIME]) return html`<${WidgetOut} o=${o}/>`;
    for (const k of ["image/png", "image/jpeg", "image/gif"]) if (d[k]) return html`<div class="nb-o img"><img alt="output" src=${"data:" + k + ";base64," + joinSrc(d[k]).replace(/\s+/g, "")}/></div>`;
    if (d["image/svg+xml"]) return html`<div class="nb-o img" dangerouslySetInnerHTML=${{ __html: clean(joinSrc(d["image/svg+xml"]), { USE_PROFILES: { svg: true, svgFilters: true } }) }}></div>`;
    if (d["text/html"]) return html`<div class="nb-o html" dangerouslySetInnerHTML=${{ __html: clean(joinSrc(d["text/html"]).slice(0, MAX_OUT * 5), { FORBID_TAGS: ["style", "form", "input", "button"] }) }}></div>`;
    if (d["text/markdown"]) return html`<div class="nb-o"><${Md} text=${joinSrc(d["text/markdown"])}/></div>`;
    if (d["text/plain"]) { const s = joinSrc(d["text/plain"]).replace(ANSI, ""); return html`<pre class="nb-o">${s.slice(0, MAX_OUT)}</pre>`; }
    const kinds = Object.keys(d); return kinds.length ? html`<div class="nb-o mute small">[${kinds.join(", ")}]: shown in Jupyter</div>` : null;
  }
  return null;
}

export function Notebook({ id, path, ver, api, put, onSaved, dirtyRef }) {
  const [d, setD] = useState(null), [cells, setCells] = useState([]), [orig, setOrig] = useState([]), [edit, setEdit] = useState(null), [sel, setSel] = useState(null);
  const nbRef = useRef(null), dr = useRef(false);
  // dirty = the cells are not the ones last loaded / saved (identity: every edit makes a new array). Serializing the whole notebook
  // (outputs and images included) on each key to compare texts made typing slow in big notebooks; it now happens on save only.
  const dirty = !!d && !d.err && !!nbRef.current && cells !== orig;
  dr.current = dirty; if (dirtyRef) dirtyRef.current = dirty;
  useEffect(() => () => { if (dirtyRef) dirtyRef.current = false; }, []);
  useEffect(() => { if (!dirty) return; const b = (e) => { e.preventDefault(); e.returnValue = ""; }; addEventListener("beforeunload", b); return () => removeEventListener("beforeunload", b); }, [dirty]);
  const load = () => api(`files/content?id=${id}&path=${encodeURIComponent(path)}`).then((r) => {
    if (r.binary || r.big) { setD({ err: r.big ? "This notebook is too large to edit here." : "This file is not text." }); return; }
    try { const nb = parseNotebook(r.text || ""); nbRef.current = nb; const cs = cellsOf(nb); setCells(cs); setOrig(cs); setD(r); setEdit(null); }
    catch (e) { nbRef.current = null; setD({ err: "Cannot read this notebook: " + e.message + ". Use the Edit tab to fix the JSON." }); }
  }).catch((e) => setD({ err: e.message }));
  useEffect(() => { if (dr.current && d) return; load(); }, [id, path, ver]);
  const lang = nbRef.current ? langOfNb(nbRef.current) : "python";
  const upd = (key, f) => setCells((cs) => cs.map((c) => (c.key === key ? { ...c, ...f(c) } : c)));
  const insert = (at, type) => { const c = { key: newId(), type, src: "", outputs: [], raw: null }; setCells((cs) => [...cs.slice(0, at), c, ...cs.slice(at)]); setEdit(c.key); setSel(c.key); };
  const move = (i, by) => setCells((cs) => { const j = i + by; if (j < 0 || j >= cs.length) return cs; const n = [...cs]; [n[i], n[j]] = [n[j], n[i]]; return n; });
  const remove = (key) => setCells((cs) => cs.filter((c) => c.key !== key));
  const save = guard(async (force) => {
    const body = serializeNotebook(nbRef.current, cells);
    try { const r = await put("files/content", { id, path, text: body, ...(force === true || !d.mtime ? {} : { mtime: d.mtime }) }); setOrig(cells); setD((x) => ({ ...x, mtime: r && r.mtime !== undefined ? r.mtime : x.mtime })); toast("Saved " + path.split("/").pop()); onSaved && onSaved(); }
    catch (e) {
      if (e.status !== 409 && !/changed/i.test(e.message)) throw e;
      const v = await ask({ title: "Notebook changed on disk", text: path.split("/").pop() + " was modified (by Claude, Jupyter or another program) after you opened it.", submit: "Apply", fields: [{ name: "c", label: "What to do", type: "select", value: "reload", options: [{ value: "reload", label: "Reload from disk (my edits are lost)" }, { value: "over", label: "Overwrite the file with my edits" }] }] });
      if (!v) return; if (v.c === "reload") { dr.current = false; await load(); toast("Reloaded"); } else await save(true);
    }
  });
  const onKey = (c, i) => (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
    else if (e.key === "Escape") { e.preventDefault(); setEdit(null); }
    else if (e.shiftKey && e.key === "Enter") { e.preventDefault(); setEdit(null); if (i === cells.length - 1) insert(i + 1, c.type === "markdown" ? "markdown" : "code"); else { setSel(cells[i + 1].key); setEdit(cells[i + 1].key); } }
  };
  if (!d) return html`<div class="empty"><div class="shine">Loading…</div></div>`;
  if (d.err) return html`<div class="empty"><div>${Ic("alert", 24)}<div>${d.err}</div></div></div>`;
  const mk = isMac ? "⌘" : "Ctrl+", outs = cells.reduce((n, c) => n + (c.type === "code" ? c.outputs.length : 0), 0);
  const adder = (at) => html`<div class="nb-add"><button type="button" onClick=${() => insert(at, "code")}>${Ic("plus", 12)} Code</button><button type="button" onClick=${() => insert(at, "markdown")}>${Ic("plus", 12)} Text</button></div>`;
  return html`<div class="nb" onKeyDown=${(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); } }}>
    <div class="nb-cells">${adder(0)}${cells.map((c, i) => { const on = edit === c.key || (c.type === "code" && sel === c.key);
      return html`<div key=${c.key} class=${"nb-cell " + c.type + (sel === c.key ? " sel" : "")} onPointerDown=${() => setSel(c.key)}>
        <div class="nb-gut">${c.type === "code" ? html`<span class="mono">[${(c.raw && c.raw.execution_count) || " "}]</span>` : c.type === "raw" ? "raw" : ""}</div>
        <div class="nb-body">
          <div class="nb-tools"><select aria-label="Cell type" value=${c.type} onChange=${(e) => upd(c.key, () => ({ type: e.target.value }))}><option value="code">Code</option><option value="markdown">Markdown</option><option value="raw">Raw</option></select>
            <button class="ibtn sm" title="Move up" aria-label="Move up" disabled=${!i} onClick=${() => move(i, -1)}>${Ic("up", 13)}</button><button class="ibtn sm" title="Move down" aria-label="Move down" disabled=${i === cells.length - 1} onClick=${() => move(i, 1)}>${Ic("down", 13)}</button>
            ${c.type === "code" && c.outputs.length > 0 && html`<button class="ibtn sm" title="Clear this cell's output" aria-label="Clear output" onClick=${() => upd(c.key, () => ({ outputs: [], cleared: true }))}>${Ic("x", 13)}</button>`}
            <button class="ibtn sm bad" title="Delete cell" aria-label="Delete cell" onClick=${() => remove(c.key)}>${Ic("trash", 13)}</button></div>
          ${c.type === "markdown" && edit !== c.key ? html`<div class="nb-md" title="Double-click to edit" onDblClick=${() => setEdit(c.key)}>${c.src.trim() ? html`<${Md} text=${c.src}/>` : html`<span class="mute small">Empty text cell: double-click to write.</span>`}</div>`
            : html`<${CellCode} value=${c.src} lang=${c.type === "code" ? lang : c.type === "markdown" ? "markdown" : ""} autofocus=${edit === c.key && on} onChange=${(v) => upd(c.key, () => ({ src: v }))} onKey=${onKey(c, i)}/>`}
          ${c.type === "code" && c.outputs.length > 0 && html`<div class="nb-outs">${c.outputs.map((o, j) => html`<${Output} key=${j} o=${o}/>`)}</div>`}
        </div></div>${adder(i + 1)}`; })}</div>
    <div class="ebar"><span class="mute small grow">${dirty ? "Unsaved changes" : "Saved"} · ${cells.length} cell${cells.length === 1 ? "" : "s"} · ${lang} · Shift+Enter next cell, Esc done, ${mk}S save</span>
      ${outs > 0 && html`<button class="btn" title="Remove every output (smaller file, clean diffs)" onClick=${guard(async () => { if (await confirmBox({ title: "Clear all outputs?", text: "Outputs come back when the notebook is run again.", ok: "Clear" })) setCells((cs) => cs.map((c) => (c.type === "code" ? { ...c, outputs: [], cleared: true } : c))); })}>Clear outputs</button>`}
      <button class="btn" disabled=${!dirty} onClick=${() => { setCells(orig); setEdit(null); }}>Revert</button><button class="btn acc" disabled=${!dirty} onClick=${() => save()}>${Ic("check", 13)} Save</button></div></div>`;
}
