// Code nodes in the Works editor. EnvBox: which Python a node runs with, what its settings and code need, one-click install (or the one
// command). MagicBar: an LLM fills the node's fields from what you describe; nothing changes until Apply. CodeField: the inline editor of
// a code field (colours, undo, find). CodeLab: the full-screen editor of a field that runs the node as it is now and previews the result.
import { html, useState, useEffect, useRef, useMemo } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, ask, escPush, Seg } from "./ui.js";
import { CodeEditor, useHl, colorize } from "./code.js";
import { RunLog } from "./blocks.js";

async function api(path, body) {
  const r = await fetch("/api/" + path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-AIHub-UI": "1" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
const copy = (t) => navigator.clipboard.writeText(t).then(() => toast("Copied"), () => toast("Could not copy"));
const FILE = { python: "node.py", sql: "query.sql", json: "data.json", markdown: "text.md", md: "text.md", yaml: "x.yaml", bash: "x.sh", ini: "x.ini" };
export const fileFor = (f) => FILE[f.lang || (f.type === "code" ? "python" : "")] || "text.txt";
const LANG_NAME = { python: "Python", sql: "SQL", json: "JSON", markdown: "Markdown", md: "Markdown", yaml: "YAML", bash: "Shell", ini: "key = value" };
export const langName = (f) => LANG_NAME[f.lang || (f.type === "code" ? "python" : "")] || "Text";
// the settings a node runs with when a field was never touched: its defaults (never secrets)
export const defaultsOf = (spec) => Object.fromEntries((spec.fields || []).filter((f) => f.default !== undefined && !f.secret).map((f) => [f.name, f.default]));
const importsOf = (code) => (String(code || "").match(/^\s*(?:from|import)\s+[\w.]+/gm) || []).join(",");
const ENV_KEYS = { "db.sql": ["driver", "frame", "write_table", "output", "file_format", "url"] };
const NAME_RE = /^[a-z][a-z0-9_-]{0,30}$/;
const CmdLine = ({ cmd, label }) => html`<div class="envb-cmd">${label && html`<span class="mute small">${label}</span>`}<code title=${cmd}>${cmd}</code><button type="button" class="ibtn" title="Copy" aria-label="Copy the command" onClick=${() => copy(cmd)}>${Ic("copy", 12)}</button></div>`;

export function EnvBox({ w, node, spec, set }) {
  const ef = spec.env, cfg = node.config || {}, val = String(cfg[ef] ?? "");
  const code = (spec.fields || []).find((f) => f.type === "code" && (f.lang || "python") === "python");
  const sig = JSON.stringify([val, (ENV_KEYS[node.type] || []).map((k) => cfg[k]), code ? importsOf(cfg[code.name] ?? code.default) : "", node.requires || []]);
  const [c, setC] = useState(null), [envs, setEnvs] = useState([]), [busy, setBusy] = useState(false), [err, setErr] = useState(""), [job, setJob] = useState(null), [log, setLog] = useState(false), [custom, setCustom] = useState(false), [extra, setExtra] = useState("");
  const seq = useRef(0);
  const load = async (fresh) => {
    const my = ++seq.current; setBusy(true);
    try { const r = await api("works/env/check", { type: node.type, config: { ...defaultsOf(spec), ...cfg }, requires: node.requires, fresh: !!fresh }); if (my === seq.current) { setC(r.check); setEnvs(r.envs || []); setErr(""); } }
    catch (e) { if (my === seq.current) setErr(e.message); } finally { if (my === seq.current) setBusy(false); }
  };
  useEffect(() => { const t = setTimeout(() => load(false), c ? 600 : 0); return () => clearTimeout(t); }, [sig]);
  useEffect(() => {
    if (!job || job.state !== "running") return;
    let stop = false, after = 0, lines = [];
    const tick = async () => {
      if (stop) return;
      try { const r = await api("works/env/job?id=" + job.id + "&after=" + after); if (stop) return; lines = lines.concat(r.job.lines || []).slice(-500); after = r.job.next; setJob({ ...r.job, lines });
        if (r.job.state === "running") setTimeout(tick, 700); else { load(true); toast(r.job.state === "done" ? "Installed" : "Install failed"); } }
      catch (e) { setTimeout(tick, 1500); }
    };
    tick(); return () => { stop = true; };
  }, [job && job.id]);
  const install = async (pkgs) => { try { const r = await api("works/env/install", { env: val, packages: pkgs, type: node.type }); setJob({ ...r.job, lines: [] }); setLog(true); } catch (e) { toast(e.message); } };
  const pick = async (v) => {
    if (v === "__new") {
      const r = await ask({ title: "New environment", text: "A separate Python environment under ~/.aihub/ui/envs, made on the first install. Nodes that use the same name share it.", submit: "Use it",
        fields: [{ name: "name", label: "Name", value: node.type === "etl.spark" ? "spark" : "data", required: true }] });
      if (!r) return; const nm = String(r.name || "").trim().toLowerCase();
      if (!NAME_RE.test(nm)) { toast("A name starts with a letter: letters, digits, - and _"); return; }
      setCustom(false); set(nm); return;
    }
    if (v === "__path") { setCustom(true); return; }
    setCustom(false); set(v);
  };
  const named = envs.some((e) => e.name === val) || !!(c && c.name && c.env === val);
  const showPath = custom || (val && !named && !NAME_RE.test(val));
  const running = job && job.state === "running", miss = c ? c.missing || [] : [], pk = c ? c.packages || [] : [];
  const status = busy && !c ? "Checking…" : err ? "Cannot check" : !c ? "" : running ? "Installing…" : c.ok ? "Ready" : miss.length ? miss.length + " missing" : c.java && !c.java.ok ? "Java missing" : c.error ? "Problem" : "Ready";
  const tone = !c || running ? "" : c.ok ? "good" : "bad";
  return html`<div class="card pre-b envb"><div class="row">${Ic("cpu", 14)}<div class="grow small"><b>Environment</b> · the Python this node runs with and what it needs</div>
      ${status && html`<span class=${"tag " + tone}>${status}</span>`}<button type="button" class="ibtn" title="Check again" aria-label="Check the environment again" disabled=${busy} onClick=${() => load(true)}>${Ic("refresh", 13)}</button></div>
    <div class="row" style="margin-top:8px;gap:6px"><select class="grow" aria-label="Python environment" value=${showPath ? "__path" : val} onChange=${(e) => pick(e.target.value)}>
      <option value="">The UI's Python${node.type === "etl.spark" ? " (or $PYSPARK_PYTHON)" : ""}</option>
      ${envs.map((e) => html`<option value=${e.name}>${e.name}${e.exists ? "" : " (broken)"}</option>`)}
      ${val && NAME_RE.test(val) && !envs.some((e) => e.name === val) ? html`<option value=${val}>${val} (new: made on install)</option>` : ""}
      <option value="__new">New environment…</option><option value="__path">A python or venv path…</option></select></div>
    ${showPath && html`<input class="mono" style="margin-top:6px" placeholder="/path/to/.venv or /usr/bin/python3.12" value=${val} onChange=${(e) => set(e.target.value.trim())}/>`}
    ${c && html`<div class="mute small mono envb-py" title=${c.python}>${c.exists ? "Python " + (c.version || "?") + " · " : "not created yet · "}${c.python}</div>`}
    ${c && c.managed_python && html`<div class="small envb-warn">${Ic("alert", 12)} This Python belongs to the system and refuses pip installs: choose New environment… instead.</div>`}
    ${pk.length ? html`<div class="envb-pk">${pk.map((p) => html`<span class=${"envb-p " + (p.installed ? "ok" : "no")} title=${p.why + (p.installed ? "" : ": " + p.pip)}>${Ic(p.installed ? "check" : "x", 11)} ${p.module}${p.version ? html` <i>${p.version}</i>` : ""}</span>`)}</div>`
      : c && !c.error ? html`<div class="mute small" style="margin-top:6px">Needs nothing beyond Python itself.</div>` : ""}
    ${c && c.java && html`<div class="envb-pk"><span class=${"envb-p " + (c.java.ok ? "ok" : "no")} title=${c.java.path || "no java found"}>${Ic(c.java.ok ? "check" : "x", 11)} Java${c.java.version ? html` <i>${c.java.version}</i>` : ""}</span></div>
      ${!c.java.ok && html`<${CmdLine} label="Spark needs Java 17:" cmd=${c.java.hint}/>`}`}
    ${err && html`<div class="small envb-warn">${Ic("alert", 12)} ${err}</div>`}
    ${c && c.error && html`<div class="small envb-warn">${Ic("alert", 12)} ${c.error}</div>`}
    ${miss.length > 0 && html`<div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap"><button type="button" class="btn acc" disabled=${running} onClick=${() => install(miss)}>${Ic("download", 13)} ${c.exists ? "Install " + miss.join(", ") : "Create “" + (c.name || val) + "” and install"}</button></div>
      <${CmdLine} label="or run it yourself:" cmd=${c.command}/>${w && w.id ? html`<${CmdLine} label="or for the whole work:" cmd=${"aihub work env install " + w.id}/>` : ""}`}
    ${c && c.exists && !miss.length && html`<details class="envb-more"><summary class="small mute">Install another package…</summary><div class="row" style="gap:6px;margin-top:6px"><input class="grow mono" placeholder="e.g. openpyxl requests>=2" value=${extra} onInput=${(e) => setExtra(e.target.value)} onKeyDown=${(e) => { if (e.key === "Enter" && extra.trim()) { e.preventDefault(); install(extra.trim().split(/\s+/)); setExtra(""); } }}/>
      <button type="button" class="btn" disabled=${running || !extra.trim()} onClick=${() => { install(extra.trim().split(/\s+/)); setExtra(""); }}>Install</button></div></details>`}
    ${job && html`<div class="envb-job"><div class="row small" style="gap:8px"><span class=${"tag " + (job.state === "done" ? "good" : job.state === "failed" ? "bad" : "")}>${running ? "Installing " + (job.packages || []).join(", ") + "…" : job.state === "done" ? "Installed" : job.state}</span><span class="grow"></span>
        ${running && html`<button type="button" class="lnk small" onClick=${() => api("works/env/cancel", { id: job.id }).catch(() => {})}>Cancel</button>`}<button type="button" class="lnk small" onClick=${() => setLog(!log)}>${log ? "Hide log" : "Log"}</button>${!running && html`<button type="button" class="lnk small" onClick=${() => setJob(null)}>Close</button>`}</div>
      ${job.error && html`<div class="small envb-warn">${job.error}</div>`}${log && html`<pre class="envb-log">${(job.lines || []).join("\n") || "Starting…"}</pre>`}</div>`}</div>`;
}

// What Magic ask may know about the node's place in the work: the parents, the data keys of every node before it, the last run.
export const magicContext = (ctx, rn, more) => ({
  parents: ((ctx && ctx.parents) || []).map((p) => ({ name: p.name, out: p.out, note: p.note })),
  keys: ((ctx && ctx.keys) || []).map((k) => ({ name: k.name, from: k.from, desc: k.desc, example: k.example })),
  last: rn ? { input: String(rn.input || "").slice(0, 3000), output: String(rn.output || "").slice(0, 3000), error: String(rn.error || "").slice(0, 3000), log: String(rn.log || "").slice(0, 1500) } : {},
  ...(more || {}),
});

export function MagicBar({ node, spec, ctx, rn, field, current, onApply, more }) {
  const h = useHl();
  const [q, setQ] = useState(""), [busy, setBusy] = useState(false), [res, setRes] = useState(null), [err, setErr] = useState(""), [show, setShow] = useState({}), [, tick] = useState(0), t0 = useRef(0);
  useEffect(() => { if (!busy) return; const t = setInterval(() => tick((n) => n + 1), 500); return () => clearInterval(t); }, [busy]);
  const fl = field ? (spec.fields || []).find((f) => f.name === field) : null;
  const go = async () => {
    if (!q.trim() || busy) return; setBusy(true); t0.current = Date.now(); setErr(""); setRes(null); setShow({});
    try { setRes(await api("works/magic", { type: node.type, config: current ? current() : { ...defaultsOf(spec), ...node.config }, ask: q.trim(), field, context: magicContext(ctx, rn, more && more()) })); }
    catch (e) { setErr(e.message); } finally { setBusy(false); }
  };
  const entries = res ? Object.entries(res.config || {}) : [];
  const labelOf = (k) => ((spec.fields || []).find((f) => f.name === k) || {}).label || k;
  const apply = () => { onApply(res.config); toast(entries.length === 1 ? labelOf(entries[0][0]) + " updated" : entries.length + " fields updated"); setRes(null); setQ(""); };
  const long = (v) => typeof v === "string" && (v.includes("\n") || v.length > 90);
  const langOfField = (k) => { const f = (spec.fields || []).find((x) => x.name === k); return f && f.type === "code" ? f.lang || "python" : null; };
  return html`<div class=${"mg" + (busy ? " busy" : "")}>
    <div class="mg-in"><span class="mg-ic">${Ic("sparkles", 15)}</span><textarea rows="1" aria-label="Magic ask" placeholder=${fl ? "Ask Magic to write or change this " + (fl.type === "code" ? langName(fl) : (fl.label || field).toLowerCase()) + ", e.g. “only last week's rows”" : "Magic ask: what should this node do?"}
        value=${q} disabled=${busy} onInput=${(e) => { setQ(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 140) + "px"; }}
        onKeyDown=${(e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); go(); } }}></textarea>
      <button type="button" class="btn acc sm" disabled=${busy || !q.trim()} onClick=${go}>${busy ? "Thinking " + Math.round((Date.now() - t0.current) / 1000) + "s" : "Ask"}</button></div>
    ${err && html`<div class="small envb-warn">${Ic("alert", 12)} ${err}</div>`}
    ${res && html`<div class="mg-res">${res.note && html`<div class="small">${res.note}</div>`}
      ${entries.length > 0 && html`<ul class="mg-ch">${entries.map(([k, v]) => html`<li><b>${labelOf(k)}</b> ${long(v) ? html`<span class="mute small">${String(v).split("\n").length} lines</span> <button type="button" class="lnk small" onClick=${() => setShow({ ...show, [k]: !show[k] })}>${show[k] ? "hide" : "show"}</button>` : html`<code>${String(v).slice(0, 90)}</code>`}
          ${show[k] && long(v) && html`<pre class="mg-pre syn" dangerouslySetInnerHTML=${{ __html: colorize(h, langOfField(k), String(v)) }}></pre>`}</li>`)}</ul>`}
      ${res.dropped && res.dropped.length > 0 && html`<div class="mute small">Not used: ${res.dropped.map((d) => labelOf(d.field) + " (" + d.why + ")").join("; ")}</div>`}
      <div class="row" style="gap:6px;margin-top:8px"><button type="button" class="btn acc sm" disabled=${!entries.length} onClick=${apply}>${Ic("check", 12)} Apply</button><button type="button" class="btn sm" onClick=${() => setRes(null)}>Discard</button>
        <span class="grow"></span><span class="mute small" title=${res.model || ""}>${res.engine === "api" ? "direct API" : "Claude Code"}</span></div></div>`}</div>`;
}

export function CodeField({ f, val, set, docKey, onExpand, runnable }) {
  return html`<div class="cf"><div class="cf-ed"><${CodeEditor} value=${val} onChange=${set} doc=${docKey} path=${fileFor(f)}/></div>
    <div class="cf-bar"><span class="mute small">${langName(f)}</span><span class="grow"></span><button type="button" class="lnk small" title=${runnable ? "A large editor that runs the node and previews the result" : "A large editor"} onClick=${onExpand}>${Ic("expand", 12)} ${runnable ? "Expand · Run & preview" : "Expand"}</button></div></div>`;
}

// the first lists of objects in a result: what the preview shows as tables
function tablesOf(d) {
  const out = [];
  if (!d || typeof d !== "object") return out;
  if (Array.isArray(d)) return d.length && typeof d[0] === "object" && d[0] && !Array.isArray(d[0]) ? [["result", d]] : out;
  for (const [k, v] of Object.entries(d)) if (Array.isArray(v) && v.length && v[0] && typeof v[0] === "object" && !Array.isArray(v[0]) && k !== "steps") { out.push([k, v]); if (out.length >= 3) break; }
  return out;
}
function Grid({ rows, cols }) {
  const ks = cols && cols.length ? cols : [...new Set(rows.slice(0, 200).flatMap((r) => Object.keys(r)))];
  const cell = (v) => v === null || v === undefined ? html`<span class="mute">null</span>` : typeof v === "object" ? JSON.stringify(v) : String(v);
  return html`<div class="lab-grid"><table><thead><tr><th class="mute">#</th>${ks.map((k) => html`<th>${k}</th>`)}</tr></thead><tbody>${rows.slice(0, 500).map((r, i) => html`<tr><td class="mute">${i + 1}</td>${ks.map((k) => html`<td>${cell(r[k])}</td>`)}</tr>`)}</tbody></table></div>`;
}
const secs = (r) => r && r.started ? (((r.ended || Date.now() / 1000) - r.started)).toFixed(1) + "s" : "";
function Preview({ run, hl, onTab }) {
  if (!run) return html`<div class="lab-empty small mute">${Ic("play", 18)}<div>Run executes the node with these settings and shows what it hands on: a table, JSON or text.<br/>${"Cmd/Ctrl+Enter runs it, Shift adds: only the selected text."}<br/>It runs for real (a SQL write writes, a mail node sends).</div></div>`;
  if (run.status === "running") return html`<div class="lab-empty small"><span class="spin"></span><div>Running… ${secs(run)} <button type="button" class="lnk small" onClick=${() => onTab("log")}>Show the log</button></div></div>`;
  if (run.status !== "done") {
    const e = run.error || run.status, env = /not installed|No module named|environment/i.test(e);
    return html`<div class="lab-out"><div class="lab-meta"><span class="tag bad">${run.status}</span> <span class="mute small">${secs(run)}</span></div><pre class="lab-err">${e}</pre>
      ${env && html`<div class="small mute">${Ic("cpu", 12)} Install what is missing in the node's Environment box (Settings, behind this editor), then run again.</div>`}</div>`;
  }
  const r = run.result || {}, d = r.data, tabs = tablesOf(d), rest = d && typeof d === "object" && !Array.isArray(d) ? Object.fromEntries(Object.entries(d).filter(([k]) => !tabs.some(([t]) => t === k))) : d;
  return html`<div class="lab-out"><div class="lab-meta"><span class="tag good">ok</span> <span class="mute small">${secs(run)}${d && d.row_count != null ? " · " + d.row_count + " row" + (d.row_count === 1 ? "" : "s") + (d.truncated ? " (preview cut)" : "") : ""}${d && d.frame ? " · " + d.frame : ""}</span></div>
    ${tabs.map(([k, rows]) => html`<div class="lab-sec"><div class="lab-st">${k === "rows" ? "Result" : k} <span class="mute small">${rows.length} shown</span></div><${Grid} rows=${rows} cols=${k === "rows" && d.columns ? d.columns : null}/></div>`)}
    ${rest && (typeof rest !== "object" || Object.keys(rest).length > 0) && html`<details class="lab-sec" open=${!tabs.length}><summary class="lab-st">Data <span class="mute small">{{data.key}} for the next nodes</span></summary><pre class="lab-json syn" dangerouslySetInnerHTML=${{ __html: colorize(hl, "json", JSON.stringify(rest, null, 2)) }}></pre></details>`}
    ${!tabs.length && r.text && html`<div class="lab-sec"><div class="lab-st">Output <span class="mute small">{{input}} of the next node</span></div><pre class="lab-txt">${r.text}</pre></div>`}
    ${run.files && run.files.length > 0 && html`<div class="lab-sec"><div class="lab-st">Files</div>${run.files.slice(0, 20).map((x) => html`<div class="mono small">${x.path} <span class="mute">${x.size} B</span></div>`)}</div>`}</div>`;
}

export function CodeLab({ w, node, spec, f, set, setField, ctx, rn, close }) {
  const hl = useHl(), cfg = node.config || {}, val = String(cfg[f.name] ?? f.default ?? "");
  const runnable = !!spec.runnable && (f.type === "code" || f.lang);
  const [input, setInput] = useState(rn && rn.input ? String(rn.input) : ""), [dataTxt, setDataTxt] = useState(""), [run, setRun] = useState(null), [tab, setTab] = useState("result");
  const ed = useRef(), cr = useRef(close); cr.current = close;
  useEffect(() => escPush(() => cr.current()), []);
  const sel = () => { const t = ed.current && ed.current.querySelector(".ce-ta"); return t && t.selectionEnd > t.selectionStart ? t.value.slice(t.selectionStart, t.selectionEnd) : ""; };
  const start = async (only) => {
    if (run && run.status === "running") return;
    const selection = only ? sel() : "";
    if (only && !selection.trim()) { toast("Select the part to run first"); return; }
    let data;
    if (dataTxt.trim()) { try { data = JSON.parse(dataTxt); if (!data || typeof data !== "object" || Array.isArray(data)) throw 0; } catch (e) { toast("Data must be a JSON object"); setTab("input"); return; } }
    setTab("result");
    try { const r = await api("works/try", { work: w.id || "", node: { id: node.id, type: node.type, config: cfg, credential: node.credential, preset: node.preset, vault: node.vault }, input, data, field: f.name, selection });
      setRun({ id: r.run, status: "running", started: Date.now() / 1000 }); }
    catch (e) { setRun({ status: "failed", error: e.message, started: Date.now() / 1000, ended: Date.now() / 1000 }); }
  };
  useEffect(() => {
    if (!run || !run.id || run.status !== "running") return;
    let stop = false;
    const tick = async () => { if (stop) return; try { const r = await api("works/try/status?run=" + run.id); if (stop) return; setRun(r.run); if (r.run.status === "running") setTimeout(tick, 600); } catch (e) { if (!stop) setRun((x) => ({ ...x, status: "failed", error: e.message })); } };
    const t = setTimeout(tick, 350); return () => { stop = true; clearTimeout(t); };
  }, [run && run.id]);
  const stopRun = () => run && run.id && api("works/stop", { run: run.id }).catch((e) => toast(e.message));
  const keys = (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && runnable) { e.preventDefault(); start(e.shiftKey); } };
  const busy = run && run.status === "running";
  return html`<div class="lab" role="dialog" aria-modal="true" aria-label=${(node.label || spec.title) + ": " + f.label} onKeyDown=${keys}>
    <header class="lab-h"><span class="nico">${Ic("code", 16)}</span><div class="grow lab-t"><b>${node.label || spec.title}</b><span class="mute"> · ${f.label}</span> <span class="tag">${langName(f)}</span></div>
      ${runnable && html`<button type="button" class="btn" title="Run only the selected text (Shift+Cmd/Ctrl+Enter)" disabled=${busy} onClick=${() => start(true)}>Run selection</button>
        ${busy ? html`<button type="button" class="btn bad" onClick=${stopRun}>${Ic("stop", 13)} Stop</button>` : html`<button type="button" class="btn acc" title="Run the node with these settings (Cmd/Ctrl+Enter)" onClick=${() => start(false)}>${Ic("play", 13)} Run</button>`}`}
      <button type="button" class="ibtn" title="Close (Esc). Your edits are kept in the node." aria-label="Close the editor" onClick=${close}>${Ic("x", 17)}</button></header>
    <div class="lab-mg"><${MagicBar} node=${node} spec=${spec} ctx=${ctx} rn=${rn} field=${f.name} current=${() => ({ ...defaultsOf(spec), ...cfg })} more=${() => ({ selection: sel(), ...(run && run.status !== "running" && run.status !== "done" ? { last: { error: String(run.error || "").slice(0, 3000), input } } : {}) })}
      onApply=${(c) => { Object.entries(c).forEach(([k, v]) => (k === f.name ? set(v) : setField(k, v))); }}/></div>
    <div class=${"lab-b" + (runnable ? " split" : "")}>
      <div class="lab-ed" ref=${ed}><${CodeEditor} value=${val} onChange=${set} doc=${"lab:" + node.id + f.name} path=${fileFor(f)}/></div>
      ${runnable && html`<div class="lab-p"><div class="lab-pt"><${Seg} small value=${tab} onChange=${setTab} items=${[{ id: "result", label: "Result" }, { id: "log", label: "Log" }, { id: "input", label: "Input" }]}/></div>
        <div class="lab-pb">${tab === "result" ? html`<${Preview} run=${run} hl=${hl} onTab=${setTab}/>`
          : tab === "log" ? (run && run.id ? html`<${RunLog} gkey=${run.id} live=${busy} get=${(after) => api("works/log?work=try&run=" + run.id + "&node=node&after=" + (after || 0))}/>` : html`<div class="lab-empty small mute">Run it to see its log.</div>`)
          : html`<div class="lab-inp"><label class="small"><b>Input</b> <span class="mute">= {{input}}: what the node before would hand over${rn && rn.input ? " (taken from the last run)" : ""}</span></label><textarea rows="6" class="mono" value=${input} onInput=${(e) => setInput(e.target.value)} placeholder="Text or JSON coming in. Empty = nothing."></textarea>
            <label class="small"><b>Data</b> <span class="mute">= {{data.key}}: a JSON object, e.g. {"day": "2026-10-31", "rows": [...]}</span></label><textarea rows="6" class="mono" value=${dataTxt} onInput=${(e) => setDataTxt(e.target.value)} placeholder="{}"></textarea></div>`}</div></div>`}</div></div>`;
}
