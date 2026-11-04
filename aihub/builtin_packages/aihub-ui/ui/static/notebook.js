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
const cellsOf = (nb) => nb.cells.map((c) => ({ key: c.id || newId(), type: c.cell_type === "markdown" || c.cell_type === "raw" ? c.cell_type : "code", src: joinSrc(c.source), outputs: c.outputs || [], raw: c }));

function CellCode({ value, onChange, lang, autofocus, onKey }) {
  const h = useHl(), ta = useRef();
  useEffect(() => { if (autofocus && ta.current) ta.current.focus(); }, []);
  const hl = useMemo(() => colorize(h, lang, value) + "\n", [h, lang, value]);
  const key = (e) => {
    if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const t = e.target, a = t.selectionStart, b = t.selectionEnd; onChange(value.slice(0, a) + "    " + value.slice(b)); requestAnimationFrame(() => { t.selectionStart = t.selectionEnd = a + 4; }); return; }
    onKey && onKey(e);
  };
  return html`<div class="nb-src syn"><pre aria-hidden="true" dangerouslySetInnerHTML=${{ __html: hl }}></pre><textarea ref=${ta} spellcheck="false" value=${value} onInput=${(e) => onChange(e.target.value)} onKeyDown=${key} aria-label="Cell source"></textarea></div>`;
}
function Output({ o }) {
  const t = o.output_type, d = o.data || {};
  if (t === "stream") { const s = joinSrc(o.text).replace(ANSI, ""); return html`<pre class=${"nb-o " + (o.name === "stderr" ? "err" : "")}>${s.slice(0, MAX_OUT)}${s.length > MAX_OUT ? "\n…" : ""}</pre>`; }
  if (t === "error") return html`<pre class="nb-o err">${((o.traceback || []).join("\n") || (o.ename + ": " + o.evalue)).replace(ANSI, "").slice(0, MAX_OUT)}</pre>`;
  if (t === "execute_result" || t === "display_data") {
    for (const k of ["image/png", "image/jpeg", "image/gif"]) if (d[k]) return html`<div class="nb-o img"><img alt="output" src=${"data:" + k + ";base64," + joinSrc(d[k]).replace(/\s+/g, "")}/></div>`;
    if (d["image/svg+xml"]) return html`<div class="nb-o img" dangerouslySetInnerHTML=${{ __html: DOMPurify.sanitize(joinSrc(d["image/svg+xml"]), { USE_PROFILES: { svg: true, svgFilters: true } }) }}></div>`;
    if (d["text/html"]) return html`<div class="nb-o html" dangerouslySetInnerHTML=${{ __html: DOMPurify.sanitize(joinSrc(d["text/html"]).slice(0, MAX_OUT * 5), { FORBID_TAGS: ["style", "form", "input", "button"] }) }}></div>`;
    if (d["text/markdown"]) return html`<div class="nb-o"><${Md} text=${joinSrc(d["text/markdown"])}/></div>`;
    if (d["text/plain"]) { const s = joinSrc(d["text/plain"]).replace(ANSI, ""); return html`<pre class="nb-o">${s.slice(0, MAX_OUT)}</pre>`; }
    const kinds = Object.keys(d); return kinds.length ? html`<div class="nb-o mute small">[${kinds.join(", ")}]: shown in Jupyter</div>` : null;
  }
  return null;
}

export function Notebook({ id, path, ver, api, put, onSaved, dirtyRef }) {
  const [d, setD] = useState(null), [cells, setCells] = useState([]), [orig, setOrig] = useState([]), [base, setBase] = useState(""), [edit, setEdit] = useState(null), [sel, setSel] = useState(null);
  const nbRef = useRef(null), dr = useRef(false);
  const text = useMemo(() => (nbRef.current ? serializeNotebook(nbRef.current, cells) : ""), [cells]);
  const dirty = !!d && !d.err && !!nbRef.current && text !== base;
  dr.current = dirty; if (dirtyRef) dirtyRef.current = dirty;
  useEffect(() => () => { if (dirtyRef) dirtyRef.current = false; }, []);
  useEffect(() => { if (!dirty) return; const b = (e) => { e.preventDefault(); e.returnValue = ""; }; addEventListener("beforeunload", b); return () => removeEventListener("beforeunload", b); }, [dirty]);
  const load = () => api(`files/content?id=${id}&path=${encodeURIComponent(path)}`).then((r) => {
    if (r.binary || r.big) { setD({ err: r.big ? "This notebook is too large to edit here." : "This file is not text." }); return; }
    try { const nb = parseNotebook(r.text || ""); nbRef.current = nb; const cs = cellsOf(nb); setCells(cs); setOrig(cs); setBase(serializeNotebook(nb, cs)); setD(r); setEdit(null); }
    catch (e) { nbRef.current = null; setD({ err: "Cannot read this notebook: " + e.message + ". Use the Edit tab to fix the JSON." }); }
  }).catch((e) => setD({ err: e.message }));
  useEffect(() => { if (dr.current && d) return; load(); }, [id, path, ver]);
  const lang = nbRef.current ? langOfNb(nbRef.current) : "python";
  const upd = (key, f) => setCells((cs) => cs.map((c) => (c.key === key ? { ...c, ...f(c) } : c)));
  const insert = (at, type) => { const c = { key: newId(), type, src: "", outputs: [], raw: null }; setCells((cs) => [...cs.slice(0, at), c, ...cs.slice(at)]); setEdit(c.key); setSel(c.key); };
  const move = (i, by) => setCells((cs) => { const j = i + by; if (j < 0 || j >= cs.length) return cs; const n = [...cs]; [n[i], n[j]] = [n[j], n[i]]; return n; });
  const remove = (key) => setCells((cs) => cs.filter((c) => c.key !== key));
  const save = guard(async (force) => {
    const body = text;
    try { const r = await put("files/content", { id, path, text: body, ...(force === true || !d.mtime ? {} : { mtime: d.mtime }) }); setBase(body); setOrig(cells); setD((x) => ({ ...x, mtime: r && r.mtime !== undefined ? r.mtime : x.mtime })); toast("Saved " + path.split("/").pop()); onSaved && onSaved(); }
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
