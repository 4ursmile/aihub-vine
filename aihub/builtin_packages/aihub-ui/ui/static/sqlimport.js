// Data Studio: import a file into a table. CSV / TSV / TXT, JSON (array of objects or arrays), JSON Lines and Excel (.xlsx) are parsed here
// in the browser; the rows go to /api/sql/import in chunks (first chunk creates / replaces the table, the rest append), which inserts them
// through SQLAlchemy in the connection's Python (sqlrunner.import_rows; MongoDB: insert_many).
import { html, useState, useMemo, useRef, useEffect } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, Seg, Modal, Switch } from "./ui.js";
import { loadJSZip } from "./export.js";

const MAX_FILE = 200e6, TYPES = ["text", "integer", "real", "boolean", "date", "timestamp"];

// ---------- parsing
function sniff(text) {
  const first = text.slice(0, 20000).split(/\r?\n/).slice(0, 5);
  let best = ",", score = 0;
  for (const d of [",", "\t", ";", "|"]) { const n = first.map((l) => l.split(d).length - 1); const s = Math.min(...n); if (s > score) { score = s; best = d; } }
  return best;
}

// RFC 4180: quoted fields, "" escapes, newlines inside quotes, CRLF
function parseCsv(text, d) {
  const rows = []; let row = [], f = "", q = false, i = 0; const n = text.length;
  while (i < n) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { f += '"'; i += 2; continue; } q = false; i++; continue; } f += c; i++; continue; }
    if (c === '"' && f === "") { q = true; i++; }
    else if (c === d) { row.push(f); f = ""; i++; }
    else if (c === "\n" || c === "\r") { row.push(f); f = ""; rows.push(row); row = []; i += c === "\r" && text[i + 1] === "\n" ? 2 : 1; }
    else { f += c; i++; }
  }
  if (f !== "" || row.length) { row.push(f); rows.push(row); }
  return rows.filter((r) => r.length > 1 || r[0] !== "");
}

const scalar = (v) => (v == null || typeof v !== "object" ? v : JSON.stringify(v));
function fromObjects(list) {
  const cols = [], seen = new Set();
  for (const o of list.slice(0, 5000)) for (const k of Object.keys(o)) if (!seen.has(k)) { seen.add(k); cols.push(k); }
  return { head: cols, rows: list.map((o) => cols.map((k) => scalar(o[k]))) };
}
function parseJson(text, name) {
  let data;
  if (/\.(jsonl|ndjson)$/i.test(name)) data = text.split(/\r?\n/).filter((l) => l.trim()).map((l) => JSON.parse(l));
  else {
    data = JSON.parse(text);
    if (data && !Array.isArray(data) && typeof data === "object") { const k = Object.keys(data).find((x) => Array.isArray(data[x])); data = k ? data[k] : [data]; }
  }
  if (!Array.isArray(data) || !data.length) throw new Error("The JSON holds no rows (an array of objects is expected).");
  if (Array.isArray(data[0])) return { head: data[0].map(String), rows: data.slice(1).map((r) => r.map(scalar)), raw: true };
  return fromObjects(data.filter((o) => o && typeof o === "object"));
}

// Excel: worksheets read from the zip; numbers formatted as dates become ISO strings
const DATE_IDS = new Set([14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47]);
const xmlOf = async (z, p) => { const f = z.file(p); return f ? new DOMParser().parseFromString(await f.async("string"), "application/xml") : null; };
const colIx = (ref) => { let n = 0; for (const ch of /^[A-Z]+/.exec(ref || "A")[0]) n = n * 26 + ch.charCodeAt(0) - 64; return n - 1; };
function serialDate(v, time) {
  const ms = Math.round((v - 25569) * 86400000), d = new Date(ms);
  if (isNaN(d)) return v;
  const iso = d.toISOString();
  return time && v % 1 ? iso.slice(0, 19).replace("T", " ") : iso.slice(0, 10);
}
async function parseXlsx(buf) {
  const JSZip = await loadJSZip(), z = await JSZip.loadAsync(buf);
  const shared = [], ss = await xmlOf(z, "xl/sharedStrings.xml");
  if (ss) for (const si of ss.getElementsByTagName("si")) shared.push([...si.getElementsByTagName("t")].map((t) => t.textContent).join(""));
  const st = await xmlOf(z, "xl/styles.xml"), dateXf = [];
  if (st) {
    const custom = {};
    for (const nf of st.getElementsByTagName("numFmt")) custom[+nf.getAttribute("numFmtId")] = nf.getAttribute("formatCode") || "";
    const xfs = st.getElementsByTagName("cellXfs")[0];
    if (xfs) for (const xf of xfs.getElementsByTagName("xf")) {
      const id = +xf.getAttribute("numFmtId"), code = (custom[id] || "").replace(/"[^"]*"|\[[^\]]*\]/g, "");
      dateXf.push(DATE_IDS.has(id) ? (id >= 18 && id <= 22) || id >= 45 ? "t" : "d" : /[dy]/i.test(code) ? (/[hs]/i.test(code) ? "t" : "d") : /h.*m|m.*s/i.test(code) ? "t" : "");
    }
  }
  const wb = await xmlOf(z, "xl/workbook.xml"), rels = await xmlOf(z, "xl/_rels/workbook.xml.rels"), target = {};
  if (rels) for (const r of rels.getElementsByTagName("Relationship")) target[r.getAttribute("Id")] = "xl/" + r.getAttribute("Target").replace(/^\/?(xl\/)?/, "");
  const sheets = wb ? [...wb.getElementsByTagName("sheet")].map((s, i) => ({ name: s.getAttribute("name") || "Sheet" + (i + 1), path: target[s.getAttribute("r:id") || s.getAttributeNS("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id")] || "xl/worksheets/sheet" + (i + 1) + ".xml" })) : [];
  const read = async (path) => {
    const x = await xmlOf(z, path); if (!x) throw new Error("Worksheet missing: " + path);
    const out = [];
    for (const row of x.getElementsByTagName("row")) {
      const r = [];
      for (const c of row.getElementsByTagName("c")) {
        const i = colIx(c.getAttribute("r")), t = c.getAttribute("t"), vEl = c.getElementsByTagName("v")[0], raw = vEl ? vEl.textContent : "";
        let v;
        if (t === "s") v = shared[+raw] ?? "";
        else if (t === "inlineStr") v = [...c.getElementsByTagName("t")].map((e) => e.textContent).join("");
        else if (t === "b") v = raw === "1";
        else if (t === "str" || t === "e") v = raw;
        else if (raw === "") v = "";
        else { const n = Number(raw), k = dateXf[+(c.getAttribute("s") || 0)]; v = k ? serialDate(n, k === "t") : n; }
        while (r.length < i) r.push("");
        r[i] = v;
      }
      out.push(r);
    }
    while (out.length && out[out.length - 1].every((v) => v === "")) out.pop();
    return out;
  };
  return { sheets: sheets.length ? sheets : [{ name: "Sheet1", path: "xl/worksheets/sheet1.xml" }], read };
}

// ---------- columns: names and types
const clean = (s, i) => String(s ?? "").trim().replace(/\s+/g, "_").replace(/[^\p{L}\p{N}_]/gu, "").slice(0, 63) || "column_" + (i + 1);
function uniq(names) { const seen = {}; return names.map((n) => { const k = n.toLowerCase(); if (!seen[k]) { seen[k] = 1; return n; } seen[k]++; return n + "_" + seen[k]; }); }
export const tableName = (file) => { const b = file.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60) || "imported"; return /^\d/.test(b) ? "t_" + b : b; };
const RE = { integer: /^[-+]?(0|[1-9]\d{0,17})$/, real: /^[-+]?((0|[1-9]\d*)(\.\d*)?|\.\d+)([eE][-+]?\d+)?$/, boolean: /^(true|false)$/i, date: /^\d{4}-\d{2}-\d{2}$/, timestamp: /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/ };
function infer(rows, j) {
  let seen = 0; const ok = { integer: true, real: true, boolean: true, date: true, timestamp: true, when: true };   // when: dates and datetimes mixed
  const step = Math.max(1, Math.floor(rows.length / 20000));
  for (let i = 0; i < rows.length; i += step) {
    const v = rows[i][j];
    if (v == null || v === "") continue;
    seen++;
    if (typeof v === "number") { ok.boolean = ok.date = ok.timestamp = ok.when = false; if (!Number.isInteger(v)) ok.integer = false; continue; }
    if (typeof v === "boolean") { ok.integer = ok.real = ok.date = ok.timestamp = ok.when = false; continue; }
    const s = String(v).trim();
    for (const k in ok) if (ok[k] && k !== "when" && !RE[k].test(s)) ok[k] = false;
    if (ok.when && !RE.date.test(s) && !RE.timestamp.test(s)) ok.when = false;
    if (!Object.values(ok).some(Boolean)) return "text";
  }
  if (!seen) return "text";
  return ok.integer ? "integer" : ok.real ? "real" : ok.boolean ? "boolean" : ok.date ? "date" : ok.timestamp || ok.when ? "timestamp" : "text";
}
function convert(v, t) {
  if (v == null || (typeof v === "string" && v.trim() === "" && t !== "text")) return null;
  if (t === "integer" || t === "real") { if (typeof v === "number") return v; const n = Number(String(v).trim()); return isNaN(n) ? String(v) : n; }
  if (t === "boolean") { if (typeof v === "boolean") return v; const s = String(v).trim().toLowerCase(); return s === "true" || s === "1" || s === "yes" ? true : s === "false" || s === "0" || s === "no" ? false : null; }
  return typeof v === "string" ? v : String(v);
}

// ---------- the dialog
export function ImportDialog({ conn, scope, post, close, done }) {
  const [file, setFile] = useState(null), [src, setSrc] = useState(null), [err, setErr] = useState(""), [busy, setBusy] = useState(false);
  const [delim, setDelim] = useState(","), [header, setHeader] = useState(true), [sheet, setSheet] = useState(0), [table, setTable] = useState(""), [mode, setMode] = useState("create");
  const [cols, setCols] = useState([]), [prog, setProg] = useState(null), [over, setOver] = useState(false), inp = useRef(), stopRef = useRef(false);
  const mongo = conn.driver === "mongodb", where = [scope && scope.catalog, scope && scope.schema].filter(Boolean).join(".");

  // grid = raw rows (array of arrays) of the chosen sheet / delimiter; header row split off here
  const grid = useMemo(() => {
    if (!src) return null;
    if (src.kind === "csv") return { rows: parseCsv(src.text, delim), headed: false };
    if (src.kind === "json") return { rows: src.data.rows, head: src.data.head, headed: !src.data.raw };
    return src.sheets ? { rows: src.cache[sheet] || [], headed: false } : null;
  }, [src, delim, sheet]);
  const body = useMemo(() => (!grid ? [] : grid.headed || !header ? grid.rows : grid.rows.slice(1)), [grid, header]);
  const width = useMemo(() => (!grid ? 0 : Math.max(grid.head ? grid.head.length : 0, ...grid.rows.slice(0, 2000).map((r) => r.length))), [grid]);
  const autoCols = useMemo(() => {
    if (!grid) return [];
    const h = grid.head || (header && grid.rows[0]) || [];
    const names = uniq(Array.from({ length: width }, (_, i) => clean(h[i], i)));
    return names.map((n, i) => ({ name: n, type: infer(body, i), on: true }));
  }, [grid, header, width, body]);
  useEffect(() => { setCols(autoCols); }, [autoCols]);                 // a new file / sheet / delimiter / header choice resets names and types

  const load = async (f) => {
    setErr(""); setSrc(null); setCols([]); setProg(null);
    if (!f) return;
    if (f.size > MAX_FILE) { setErr(f.name + " is over 200 MB."); return; }
    setFile(f); setTable(tableName(f.name)); setBusy(true);
    try {
      if (/\.xlsx$/i.test(f.name)) {
        const x = await parseXlsx(await f.arrayBuffer()), cache = {};
        cache[0] = await x.read(x.sheets[0].path);
        setSheet(0); setSrc({ kind: "xlsx", sheets: x.sheets, read: x.read, cache });
      } else if (/\.xls$/i.test(f.name)) throw new Error("Old .xls files are not supported: save it as .xlsx or CSV.");
      else {
        const text = (await f.text()).replace(/^﻿/, "");
        if (/\.(json|jsonl|ndjson)$/i.test(f.name) || /^\s*[[{]/.test(text.slice(0, 200))) setSrc({ kind: "json", data: parseJson(text, f.name) });
        else { setDelim(/\.tsv$/i.test(f.name) ? "\t" : sniff(text)); setSrc({ kind: "csv", text }); }
      }
    } catch (e) { setErr("Could not read " + f.name + ": " + e.message); setFile(null); }
    finally { setBusy(false); }
  };
  const pickSheet = async (i) => { setSheet(i); if (!src.cache[i]) { setBusy(true); try { const rows = await src.read(src.sheets[i].path); setSrc((s) => ({ ...s, cache: { ...s.cache, [i]: rows } })); } catch (e) { setErr(e.message); } finally { setBusy(false); } } };
  const setCol = (i, p) => setCols((cs) => cs.map((c, j) => (j === i ? { ...c, ...p } : c)));

  const run = async () => {
    const use = cols.map((c, i) => ({ ...c, i })).filter((c) => c.on && c.name.trim());
    if (!table.trim()) return setErr("Give the table a name.");
    if (!use.length) return setErr("Keep at least one column.");
    const dup = use.map((c) => c.name.trim().toLowerCase()).find((n, i, a) => a.indexOf(n) !== i);
    if (dup) return setErr("Two columns are named " + dup + ".");
    setErr(""); setBusy(true); stopRef.current = false;
    const columns = use.map((c) => ({ name: c.name.trim(), type: c.type }));
    const rowOf = (r) => use.map((c) => convert(r[c.i], c.type));
    const avg = Math.max(20, JSON.stringify(body.slice(0, 50).map(rowOf)).length / Math.max(1, Math.min(50, body.length)));
    const size = Math.max(200, Math.min(20000, Math.floor(4e6 / avg)));
    let sent = 0;
    try {
      for (let at = 0; at === 0 || at < body.length; at += size) {
        if (stopRef.current) throw new Error("Stopped after " + sent.toLocaleString() + " rows.");
        setProg({ done: sent, total: body.length });
        const r = await post("sql/import", { id: conn.id, table: table.trim(), mode: at === 0 ? mode : "append", columns, rows: body.slice(at, at + size).map(rowOf), ...(scope || {}) });
        if (!r.ok) throw new Error(r.error || "Import failed");
        sent += r.rows || 0;
      }
      setProg({ done: sent, total: body.length });
      toast("Imported " + sent.toLocaleString() + " rows into " + table.trim());
      done(table.trim());
    } catch (e) { setErr((sent ? sent.toLocaleString() + " rows were imported before this: " : "") + e.message); }
    finally { setBusy(false); }
  };

  const drop = (e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files && e.dataTransfer.files[0]; if (f) load(f); };
  const preview = body.slice(0, 8);
  return html`<${Modal} close=${() => !busy && close()} wide=${true}><div class="sqi">
    <h1>Import data</h1><p class="sub">Into <b>${conn.name}</b>${where ? html` · ${where}` : ""}${conn.readonly ? html` · <span class="bad">read-only connection: importing is off</span>` : ""}</p>
    <div class=${"sqi-drop" + (over ? " over" : "") + (file ? " has" : "")} onDragOver=${(e) => { e.preventDefault(); setOver(true); }} onDragLeave=${() => setOver(false)} onDrop=${drop} onClick=${() => !busy && inp.current.click()} role="button" tabindex="0" onKeyDown=${(e) => (e.key === "Enter" || e.key === " ") && inp.current.click()}>
      ${Ic(file ? "file" : "upload", 20)}<div class="grow">${file ? html`<b>${file.name}</b><div class="mute small">${file.size < 1048576 ? Math.max(1, Math.round(file.size / 1024)) + " KB" : (file.size / 1048576).toFixed(1) + " MB"}${grid ? " · " + body.length.toLocaleString() + " rows · " + width + " columns" : ""} · click to pick another</div>`
        : html`<b>Drop a file here or click to choose</b><div class="mute small">CSV, TSV, TXT, JSON, JSON Lines, Excel (.xlsx), up to 200 MB</div>`}</div>${busy && !prog && html`<i class="spin"></i>`}
      <input type="file" ref=${inp} hidden accept=".csv,.tsv,.txt,.json,.jsonl,.ndjson,.xlsx,text/csv,application/json" onChange=${(e) => { load(e.target.files[0]); e.target.value = ""; }}/></div>
    ${src && html`<div class="sqi-opts">
      ${src.kind === "csv" && html`<label class="sq-f"><span>Delimiter</span><select value=${delim} onChange=${(e) => setDelim(e.target.value)}><option value=",">Comma ,</option><option value=${"\t"}>Tab</option><option value=";">Semicolon ;</option><option value="|">Pipe |</option></select></label>`}
      ${src.kind === "xlsx" && src.sheets.length > 1 && html`<label class="sq-f"><span>Sheet</span><select value=${sheet} onChange=${(e) => pickSheet(+e.target.value)}>${src.sheets.map((s, i) => html`<option value=${i}>${s.name}</option>`)}</select></label>`}
      ${src.kind !== "json" && html`<div class="sq-f"><span>First row</span><div class="row" style="gap:8px;min-height:34px"><${Switch} on=${header} onChange=${setHeader} label="First row holds the column names"/><span class="small">${header ? "Column names" : "Data"}</span></div></div>`}
      <label class="sq-f sqi-tn"><span>${mongo ? "Collection" : "Table"}</span><input value=${table} onInput=${(e) => setTable(e.target.value)} autocomplete="off"/></label>
      <div class="sq-f"><span>If it exists</span><${Seg} value=${mode} onChange=${setMode} items=${[{ id: "create", label: "New" }, { id: "append", label: "Append" }, { id: "replace", label: "Replace" }]}/></div></div>
      <p class="mute small" style="margin:0">${mode === "create" ? "Creates the table; stops if one with that name exists." : mode === "append" ? "Adds the rows to the existing table. Column names must match its columns." : "Drops the table if it exists, then creates it again with these columns."}</p>
      ${cols.length > 0 && html`<div class="sqi-wrap"><table class="sq-t sqi-t"><thead><tr>${cols.map((c, i) => html`<th key=${i} class=${c.on ? "" : "off"}>
          <div class="sqi-ch"><input type="checkbox" checked=${c.on} onChange=${() => setCol(i, { on: !c.on })} aria-label=${"Import column " + c.name}/><input class="sqi-cn" value=${c.name} onInput=${(e) => setCol(i, { name: e.target.value })} aria-label="Column name"/></div>
          ${!mongo && mode !== "append" && html`<select class="sqi-ty" value=${c.type} onChange=${(e) => setCol(i, { type: e.target.value })} aria-label=${"Type of " + c.name}>${TYPES.map((t) => html`<option value=${t}>${t}</option>`)}</select>`}</th>`)}</tr></thead>
        <tbody>${preview.map((r, ri) => html`<tr key=${ri}>${cols.map((c, i) => html`<td key=${i} class=${(c.on ? "" : "off ") + (r[i] == null || r[i] === "" ? "nul" : typeof r[i] === "number" ? "num" : "")}>${r[i] == null || r[i] === "" ? "NULL" : String(r[i]).slice(0, 80)}</td>`)}</tr>`)}</tbody></table></div>
        ${body.length > preview.length && html`<p class="mute small" style="margin:0">Showing ${preview.length} of ${body.length.toLocaleString()} rows.</p>`}`}`}
    ${prog && html`<div class="updprog"><div class="bar"><span style=${"width:" + Math.round((100 * prog.done) / Math.max(1, prog.total)) + "%"}></span></div><span class="small mute">${prog.done.toLocaleString()} / ${prog.total.toLocaleString()} rows</span></div>`}
    ${err && html`<div class="sq-test bad">${Ic("alert", 15)} <span>${err}</span></div>`}
    <div class="row" style="gap:8px;justify-content:flex-end;margin-top:4px">${busy && prog ? html`<button class="btn bad" onClick=${() => { stopRef.current = true; }}>Stop</button>` : html`<button class="btn" onClick=${close}>Cancel</button>`}
      <button class="btn acc" disabled=${busy || !cols.length || conn.readonly} onClick=${run}>${busy && prog ? html`<i class="spin"></i>` : Ic("upload", 14)} Import ${body.length ? body.length.toLocaleString() + " rows" : ""}</button></div></div><//>`;
}
