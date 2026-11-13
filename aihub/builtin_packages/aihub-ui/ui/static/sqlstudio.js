// SQL Studio: a lightweight, browser-based SQL client. Saved connections (any SQLAlchemy database), schema browser, query tabs,
// result grid with export, and a Drivers tab that installs the extra packages a database needs into a chosen Python environment.
// Data: /api/sql/* (server.py -> aihubui/sqlstudio.py; queries run in a child Python of the connection's environment).
import { html, useState, useEffect, useRef, useMemo, useCallback } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, guard, Seg, Modal, Switch, confirmBox } from "./ui.js";

const LS = { get(k, d) { try { const v = localStorage.getItem("sqls_" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }, set(k, v) { try { localStorage.setItem("sqls_" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } } };
const uid = () => Math.random().toString(36).slice(2, 10);
const num = (n) => Number(n).toLocaleString();
const KW = "select from where group by order having limit offset join left right inner outer full cross on as and or not in is null like ilike between exists case when then else end union all distinct insert into values update set delete create table alter drop index view with asc desc count sum avg min max cast over partition returning using primary key foreign references default explain describe show pragma top true false".split(" ");
const KWRE = new RegExp("^(" + KW.join("|") + ")$", "i");
const DRIVER_LABEL = { postgresql: "PostgreSQL", mysql: "MySQL", sqlite: "SQLite", mssql: "SQL Server", "mssql-odbc": "SQL Server (ODBC)", oracle: "Oracle", trino: "Trino", duckdb: "DuckDB", other: "SQLAlchemy URL" };
const PORTS = { postgresql: "5432", mysql: "3306", mssql: "1433", "mssql-odbc": "1433", oracle: "1521", trino: "443" };
const FILE_DB = ["sqlite", "duckdb"];

// ---------- SQL syntax colouring (a <pre> under the transparent textarea)
function hl(sql) {
  const out = [], re = /(--[^\n]*|\/\*[\s\S]*?(?:\*\/|$))|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?|`[^`]*`?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z_0-9]*)|([\s\S])/g;
  let m;
  while ((m = re.exec(sql))) {
    if (m[1]) out.push(html`<i class="sq-c">${m[1]}</i>`);
    else if (m[2]) out.push(html`<i class="sq-s">${m[2]}</i>`);
    else if (m[3]) out.push(html`<i class="sq-i">${m[3]}</i>`);
    else if (m[4]) out.push(html`<i class="sq-n">${m[4]}</i>`);
    else if (m[5] && KWRE.test(m[5])) out.push(html`<i class="sq-k">${m[5]}</i>`);
    else out.push(m[0]);
  }
  return out;
}

function Editor({ value, onChange, onRun, ro }) {
  const ta = useRef(), pre = useRef();
  const sync = () => { if (pre.current && ta.current) { pre.current.scrollTop = ta.current.scrollTop; pre.current.scrollLeft = ta.current.scrollLeft; } };
  const key = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); const t = ta.current, sel = t.value.slice(t.selectionStart, t.selectionEnd); onRun(sel.trim() ? sel : null); return; }
    if (e.key === "Tab") { e.preventDefault(); const t = ta.current, a = t.selectionStart, b = t.selectionEnd; t.value = t.value.slice(0, a) + "  " + t.value.slice(b); t.selectionStart = t.selectionEnd = a + 2; onChange(t.value); }
  };
  const lines = value.split("\n").length;
  return html`<div class="sq-ed"><div class="sq-gut" aria-hidden="true">${Array.from({ length: Math.min(lines, 400) }, (_, i) => html`<span>${i + 1}</span>`)}</div>
    <div class="sq-code"><pre ref=${pre} aria-hidden="true">${hl(value)}\n</pre>
      <textarea ref=${ta} value=${value} spellcheck="false" autocapitalize="off" autocomplete="off" readOnly=${ro} placeholder="select * from …   (Ctrl/⌘ + Enter runs; a selection runs alone)"
        aria-label="SQL editor" onInput=${(e) => onChange(e.target.value)} onScroll=${sync} onKeyDown=${key}></textarea></div></div>`;
}

// ---------- result grid
const isNum = (v) => typeof v === "number";
function csv(cols, rows) { const q = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v)); return [cols.map(q).join(","), ...rows.map((r) => r.map(q).join(","))].join("\n"); }
function download(name, text, type) { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000); }

function Grid({ res, name }) {
  const [sort, setSort] = useState(null), [page, setPage] = useState(0), [q, setQ] = useState(""), PS = 200;
  useEffect(() => { setSort(null); setPage(0); setQ(""); }, [res]);
  const rows = useMemo(() => {
    let r = res.rows;
    if (q.trim()) { const s = q.toLowerCase(); r = r.filter((x) => x.some((v) => v != null && String(v).toLowerCase().includes(s))); }
    if (sort) { const [i, d] = sort; r = [...r].sort((a, b) => { const x = a[i], y = b[i]; if (x == null) return 1; if (y == null) return -1; return (isNum(x) && isNum(y) ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true })) * d; }); }
    return r;
  }, [res, sort, q]);
  const pages = Math.max(1, Math.ceil(rows.length / PS)), cur = Math.min(page, pages - 1), shown = rows.slice(cur * PS, cur * PS + PS);
  const cyc = (i) => setSort(!sort || sort[0] !== i ? [i, 1] : sort[1] === 1 ? [i, -1] : null);
  const copyCell = (v) => { try { navigator.clipboard.writeText(v == null ? "" : String(v)); toast("Copied"); } catch (e) { /* no clipboard */ } };
  return html`<div class="sq-res"><div class="sq-rbar"><span class="tag good">${Ic("check", 12)} ${num(res.rows.length)} row${res.rows.length === 1 ? "" : "s"}${res.truncated ? "+" : ""}</span>
      <span class="mute small">${res.ms} ms${res.statements > 1 ? " · " + res.statements + " statements, last shown" : ""}${res.truncated ? " · limited: raise the row limit for more" : ""}</span><span class="grow"></span>
      <input class="sq-find" placeholder="Filter rows" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Filter rows"/>
      <button class="btn sm" title="Copy as CSV" onClick=${() => { try { navigator.clipboard.writeText(csv(res.columns, rows)); toast("CSV copied"); } catch (e) { /* ignore */ } }}>${Ic("copy", 13)} Copy</button>
      <button class="btn sm" onClick=${() => download((name || "result") + ".csv", csv(res.columns, rows), "text/csv")}>${Ic("download", 13)} CSV</button>
      <button class="btn sm" onClick=${() => download((name || "result") + ".json", JSON.stringify(rows.map((r) => Object.fromEntries(res.columns.map((c, i) => [c, r[i]]))), null, 1), "application/json")}>${Ic("download", 13)} JSON</button></div>
    <div class="sq-scroll"><table class="sq-t"><thead><tr><th class="sq-rn">#</th>${res.columns.map((c, i) => html`<th onClick=${() => cyc(i)} title="Sort"><span>${c}</span>${sort && sort[0] === i && html`<em>${sort[1] === 1 ? "▲" : "▼"}</em>`}</th>`)}</tr></thead>
      <tbody>${shown.map((r, ri) => html`<tr key=${cur * PS + ri}><td class="sq-rn">${cur * PS + ri + 1}</td>${r.map((v) => html`<td class=${v == null ? "nul" : isNum(v) ? "num" : ""} title=${v == null ? "NULL" : String(v).slice(0, 300)} onDblClick=${() => copyCell(v)}>${v == null ? "NULL" : typeof v === "boolean" ? String(v) : String(v)}</td>`)}</tr>`)}</tbody></table>
      ${!rows.length && html`<div class="sq-empty">${q ? "No row matches the filter." : "The query returned no rows."}</div>`}</div>
    ${pages > 1 && html`<div class="sq-pg"><button class="btn sm" disabled=${cur === 0} onClick=${() => setPage(cur - 1)}>${Ic("left", 12)}</button><span class="small mute">Page ${cur + 1} of ${pages}</span><button class="btn sm" disabled=${cur >= pages - 1} onClick=${() => setPage(cur + 1)}>${Ic("right", 12)}</button></div>`}</div>`;
}

// ---------- installing packages (shared by the Drivers tab and the "driver missing" hint)
function useInstaller(post, api, onDone) {
  const [job, setJob] = useState(null), t = useRef();
  const stop = () => { clearTimeout(t.current); };
  useEffect(() => stop, []);
  const poll = (id, after = 0, lines = []) => {
    api("works/env/job?id=" + id + "&after=" + after).then((r) => {
      const j = r.job, all = lines.concat(j.lines || []).slice(-400);
      setJob({ ...j, lines: all });
      if (j.state === "running") t.current = setTimeout(() => poll(id, j.next, all), 700); else if (onDone) onDone(j);
    }).catch((e) => { setJob((x) => x && { ...x, state: "error", error: e.message }); });
  };
  const start = guard(async (env, packages) => { stop(); const r = await post("works/env/install", { env, packages }); setJob({ ...r.job, lines: [] }); poll(r.job.id); });
  const cancel = () => job && post("works/env/cancel", { id: job.id }).catch(() => {});
  return { job, start, cancel, clear: () => { stop(); setJob(null); } };
}

function InstallLog({ ins }) {
  const { job } = ins, ref = useRef();
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [job && job.lines.length]);
  if (!job) return null;
  const run = job.state === "running";
  return html`<div class="sq-job"><div class="row"><span class=${"tag " + (run ? "acc" : job.state === "done" ? "good" : "bad")}>${run ? html`<i class="spin"></i>` : job.state === "error" ? Ic("x", 12) : Ic("check", 12)} ${run ? "Installing" : job.state === "error" ? "Failed" : "Installed"}</span>
    <code class="grow sq-ell">${(job.packages || []).join("  ")}</code>${run ? html`<button class="btn sm bad" onClick=${ins.cancel}>Cancel</button>` : html`<button class="ibtn" aria-label="Dismiss" onClick=${ins.clear}>${Ic("x", 14)}</button>`}</div>
    <pre class="sq-log" ref=${ref}>${job.lines.join("\n") || "Starting…"}${job.error ? "\n" + job.error : ""}</pre></div>`;
}

// Shown instead of an error when SQLAlchemy / a driver is not installed: only browsing and running need them, nothing else does.
function MissingHint({ info, env, api, post, onDone, small }) {
  const ins = useInstaller(post, api, (j) => { if (j.state === "done" && onDone) onDone(); });
  const run = ins.job && ins.job.state === "running";
  const names = (info.missing || []).map((p) => p.replace(/[<>=\[].*$/, "")).join(" + ");
  return html`<div class=${"sq-hint" + (small ? " small" : "")}><div class="sq-hh2">${Ic("download", small ? 15 : 18)}<div class="grow"><b>Install ${names} to continue</b>
      <div class="mute small">Needed only to browse and run SQL${env ? " (environment: " + env + ")" : ""}. Everything else keeps working.</div></div></div>
    <div class="row" style="gap:8px;flex-wrap:wrap"><button class="btn acc" disabled=${run} onClick=${() => ins.start(env || "", info.missing)}>${run ? html`<i class="spin"></i>` : Ic("download", 14)} Install</button>
      <button class="btn" onClick=${() => { try { navigator.clipboard.writeText(info.command || ""); toast("Command copied"); } catch (e) { /* no clipboard */ } }}>${Ic("copy", 13)} Copy command</button></div>
    ${info.command && html`<code class="sq-cmd">${info.command}</code>`}<${InstallLog} ins=${ins}/></div>`;
}

// ---------- connections
function DriverTile({ k, on, set }) {
  return html`<button type="button" class=${"sq-tile" + (on ? " on" : "")} onClick=${() => set(k)}><span class=${"sq-badge d-" + k.replace(/[^a-z]/g, "")}>${(DRIVER_LABEL[k] || k)[0]}</span><span>${DRIVER_LABEL[k] || k}</span></button>`;
}

function ConnForm({ init, envs, ui, api, post, close, saved }) {
  const [f, setF] = useState(() => ({ driver: "postgresql", name: "", host: "", port: "5432", user: "", password: "", database: "", options: "", url: "", env: "", readonly: false, ...init }));
  const [test, setTest] = useState(null), [busy, setBusy] = useState(false), set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target ? e.target.value : e }));
  const drv = f.driver, file = FILE_DB.includes(drv), url = drv === "other";
  const pick = (k) => setF((x) => ({ ...x, driver: k, port: x.port && Object.values(PORTS).includes(x.port) || !x.port ? PORTS[k] || "" : x.port }));
  const run = guard(async () => { setBusy(true); setTest(null); try { setTest(await post("sql/test", { connection: f })); } finally { setBusy(false); } });
  const save = guard(async () => { setBusy(true); try { const r = await post("sql/save", { connection: f }); saved(r.connection); } finally { setBusy(false); } });
  const field = (label, k, ph, type = "text", cls = "") => html`<label class=${"sq-f " + cls}><span>${label}</span><input type=${type} value=${f[k]} placeholder=${ph || ""} onInput=${set(k)} autocomplete="off"/></label>`;
  const known = Object.keys(DRIVER_LABEL);
  return html`<${Modal} close=${close} wide=${true}><div class="sq-form"><h3>${f.id ? "Edit connection" : "New connection"}</h3>
    <div class="sq-tiles">${known.map((k) => html`<${DriverTile} k=${k} on=${drv === k} set=${pick}/>`)}</div>
    ${field("Name", "name", "Production replica")}
    ${url ? html`<label class="sq-f"><span>SQLAlchemy URL ${f.url_masked && !f.url ? html`<em class="mute">saved: ${f.url_masked} (leave blank to keep)</em>` : ""}</span>
        <input type="password" value=${f.url} placeholder="postgresql+psycopg://user:password@host:5432/dbname" onInput=${set("url")} autocomplete="off"/>
        <small class="mute">Any dialect SQLAlchemy supports. Install its driver in the Drivers tab first.</small></label>`
    : file ? field("Database file", "database", drv === "duckdb" ? "/path/to/file.duckdb (empty = in memory)" : "/path/to/file.sqlite (empty = in memory)")
    : html`<div class="sq-grid2">${field("Host", "host", "db.example.com")}${field("Port", "port", PORTS[drv] || "")}${field("User", "user", "")}
        <label class="sq-f"><span>Password ${f.has_secret && !f.password ? html`<em class="mute">saved (leave blank to keep)</em>` : ""}</span><input type="password" value=${f.password} onInput=${set("password")} autocomplete="new-password"/></label>
        ${field("Database", "database", drv === "oracle" ? "service name" : drv === "trino" ? "catalog/schema" : "", "text", "span2")}${field("Extra options", "options", "sslmode=require&connect_timeout=10", "text", "span2")}</div>`}
    <label class="sq-f"><span>Python environment</span><select value=${f.env} onChange=${set("env")}><option value="">UI's own Python</option>${envs.map((e) => html`<option value=${e.name}>${e.name}</option>`)}</select>
      <small class="mute">Queries run in this environment, so the drivers you install for it (Drivers tab) are found. ${ui ? "UI Python: " + ui : ""}</small></label>
    <div class="row sq-ro"><${Switch} on=${f.readonly} onChange=${(v) => setF((x) => ({ ...x, readonly: v }))} label="Read-only"/><div><b>Read-only</b><div class="mute small">Only SELECT, WITH, SHOW, EXPLAIN… statements are allowed on this connection.</div></div></div>
    ${test && test.missing ? html`<${MissingHint} info=${test} env=${f.env} api=${api} post=${post} small=${true} onDone=${run}/>`
      : test && html`<div class=${"sq-test " + (test.ok ? "ok" : "bad")}>${test.ok ? html`${Ic("check", 15)} Connected${test.ms != null ? " in " + test.ms + " ms" : ""} · ${test.dialect}${test.server ? " · " + test.server.slice(0, 80) : ""}` : html`${Ic("alert", 15)} <span>${test.error}</span>`}</div>`}
    <div class="row sq-fa"><button class="btn" disabled=${busy} onClick=${run}>${busy ? html`<i class="spin"></i>` : Ic("bolt", 14)} Test connection</button><span class="grow"></span>
      <button class="btn" onClick=${close}>Cancel</button><button class="btn acc" disabled=${busy || !f.name.trim()} onClick=${save}>Save</button></div></div><//>`;
}

function Connections({ conns, envs, ui, api, post, reload, openStudio, edit, setEdit }) {
  return html`<div class="sq-pane"><div class="row"><div class="grow"><h2 style="margin:0">Connections</h2><p class="mute small" style="margin:2px 0 0">Passwords are stored encrypted on this machine and never sent back to the browser.</p></div>
      <button class="btn acc" onClick=${() => setEdit({})}>${Ic("plus", 14)} New connection</button></div>
    ${!conns.length && html`<div class="sq-hero">${Ic("database", 34)}<h3>Connect your first database</h3><p class="mute">PostgreSQL, MySQL, SQL Server, Oracle, Trino, SQLite, DuckDB, or anything SQLAlchemy reaches with a URL.</p><button class="btn acc" onClick=${() => setEdit({})}>${Ic("plus", 14)} New connection</button></div>`}
    <div class="sq-cards">${conns.map((c) => html`<div class="sq-card" key=${c.id}><div class="row"><span class=${"sq-badge d-" + c.driver.replace(/[^a-z]/g, "")}>${(DRIVER_LABEL[c.driver] || c.driver)[0]}</span>
        <div class="grow"><b>${c.name}</b><div class="mute small sq-ell">${DRIVER_LABEL[c.driver] || c.driver} · ${c.driver === "other" ? c.url_masked || "URL" : FILE_DB.includes(c.driver) ? c.database || "in memory" : (c.host || "?") + (c.port ? ":" + c.port : "") + (c.database ? "/" + c.database : "")}</div></div>
        ${c.readonly && html`<span class="tag">${Ic("eye", 11)} read-only</span>`}</div>
      <div class="row sq-ca"><span class="mute small grow">${c.env ? "env: " + c.env : "UI Python"}</span><button class="btn sm acc" onClick=${() => openStudio(c.id)}>${Ic("play", 12)} Open</button>
        <button class="ibtn" title="Edit" aria-label="Edit" onClick=${() => setEdit(c)}>${Ic("edit", 14)}</button>
        <button class="ibtn" title="Delete" aria-label="Delete" onClick=${guard(async () => { if (await confirmBox({ title: "Delete " + c.name + "?", text: "The saved password is removed too.", ok: "Delete", danger: true })) { await post("sql/delete", { id: c.id }); reload(); } })}>${Ic("trash", 14)}</button></div></div>`)}</div>
    ${edit && html`<${ConnForm} init=${edit} envs=${envs} ui=${ui} api=${api} post=${post} close=${() => setEdit(null)} saved=${(c) => { setEdit(null); reload(); toast("Saved " + c.name); }}/>`}</div>`;
}

// ---------- drivers
function Drivers({ api, post, envs, ui, onUseUrl, reloadEnvs }) {
  const [env, setEnv] = useState(LS.get("env", "")), [d, setD] = useState(null), [err, setErr] = useState(""), [newEnv, setNewEnv] = useState(""), [q, setQ] = useState("");
  const load = useCallback(() => api("sql/drivers?env=" + encodeURIComponent(env)).then((x) => { setD(x); setErr(x.error || ""); }).catch((e) => setErr(e.message)), [env]);
  useEffect(() => { setD(null); load(); LS.set("env", env); }, [env]);
  const ins = useInstaller(post, api, () => { load(); reloadEnvs(); });
  const target = env || "";
  const need = (pips) => { const l = [...pips]; if (d && d.sqlalchemy && !d.sqlalchemy.installed) l.unshift("sqlalchemy>=2"); return l; };
  const go = (pips) => ins.start(target, need(pips));
  const create = () => { const n = newEnv.trim().toLowerCase(); if (!/^[a-z][a-z0-9_-]{0,30}$/.test(n)) return toast("Use a short lowercase name (letters, digits, - _)"); setNewEnv(""); setEnv(n); ins.start(n, ["sqlalchemy>=2"]); };
  const list = (d ? d.drivers : []).filter((x) => !q.trim() || (x.title + x.key + (x.pips || []).join(" ")).toLowerCase().includes(q.toLowerCase()));
  const running = ins.job && ins.job.state === "running";
  return html`<div class="sq-pane"><div class="row" style="align-items:flex-end;flex-wrap:wrap"><div class="grow"><h2 style="margin:0">Drivers</h2><p class="mute small" style="margin:2px 0 0">SQLAlchemy plus one small package per database. Install into the UI's Python or into an isolated environment.</p></div>
      <label class="sq-f" style="margin:0"><span>Environment</span><select value=${env} onChange=${(e) => setEnv(e.target.value)}><option value="">UI's own Python</option>${envs.map((e) => html`<option value=${e.name}>${e.name}</option>`)}${env && !envs.some((e) => e.name === env) && html`<option value=${env}>${env} (new)</option>`}</select></label>
      <div class="row" style="gap:6px"><input style="width:150px" placeholder="new env name" value=${newEnv} onInput=${(e) => setNewEnv(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && create()} aria-label="New environment name"/><button class="btn" disabled=${!newEnv.trim() || running} onClick=${create}>${Ic("plus", 13)} Create</button></div></div>
    ${err && html`<div class="sq-test bad">${Ic("alert", 15)} <span>${err}</span></div>`}
    ${d && html`<div class="sq-env"><span class="tag">${Ic("terminal", 11)} ${d.python}</span>${d.version && html`<span class="tag">Python ${d.version}</span>`}
      ${d.managed_python && !d.venv && html`<span class="tag bad" title="This Python refuses system-wide pip installs; use a separate environment">externally managed: create an environment</span>`}
      ${d.pip === false && d.exists && html`<span class="tag warn">no pip here</span>`}</div>`}
    ${d && html`<div class="sq-core"><span class=${"sq-dot " + (d.sqlalchemy.installed ? "ok" : "off")}></span><div class="grow"><b>SQLAlchemy</b><div class="mute small">The core every connection uses${d.sqlalchemy.version ? " · v" + d.sqlalchemy.version : ""}</div></div>
      ${d.sqlalchemy.installed ? html`<span class="tag good">${Ic("check", 11)} installed</span>` : html`<button class="btn sm acc" disabled=${running || !d.exists && !env} onClick=${() => ins.start(target, ["sqlalchemy>=2"])}>${Ic("download", 13)} Install</button>`}</div>`}
    <${InstallLog} ins=${ins}/>
    <div class="row" style="margin:14px 0 8px"><input class="sq-find" style="width:260px" placeholder="Search databases" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Search drivers"/><span class="grow"></span>
      <button class="btn sm" onClick=${() => { setD(null); load(); }}>${Ic("refresh", 13)} Re-check</button></div>
    ${!d && !err && html`<p class="mute">Checking the environment…</p>`}
    <div class="sq-drv">${list.map((x) => html`<div class=${"sq-dv" + (x.installed ? " ok" : "")} key=${x.key}><span class=${"sq-badge d-" + x.key.replace(/[^a-z]/g, "")}>${x.title[0]}</span>
      <div class="grow" style="min-width:0"><b>${x.title}</b><div class="mute small sq-ell">${x.builtin ? "Built into Python, nothing to install" : (x.pips || []).join(" + ")}${x.version ? " · v" + x.version : ""}</div>${x.note && html`<div class="mute small">${x.note}</div>`}</div>
      ${x.installed ? html`<span class="tag good">${Ic("check", 11)} ready</span>` : html`<button class="btn sm acc" disabled=${running} onClick=${() => go(x.pips)}>${Ic("download", 13)} Install</button>`}
      ${x.other && html`<button class="ibtn" title="Start a connection with this URL template" aria-label="Use" onClick=${() => onUseUrl(x.url, env)}>${Ic("link", 14)}</button>`}</div>`)}</div>
    <p class="mute small" style="margin-top:14px">Not listed? Any SQLAlchemy dialect works: install its package here (create an environment and use <b>pip</b> in a terminal) and connect with the <b>SQLAlchemy URL</b> type.</p></div>`;
}

// ---------- studio
function Tree({ conn, env, api, post, onInsert, onOpen }) {
  const [data, setData] = useState(null), [err, setErr] = useState(null), [schema, setSchema] = useState(null), [open, setOpen] = useState({}), [cols, setCols] = useState({}), [q, setQ] = useState(""), [busy, setBusy] = useState(false);
  const load = (sch) => { setBusy(true); setErr(null); post("sql/tables", { id: conn, schema: sch == null ? undefined : sch }).then((r) => { if (!r.ok) setErr(r); else { setData(r); setSchema(r.schema); } }).catch((e) => setErr({ error: e.message })).finally(() => setBusy(false)); };
  useEffect(() => { setData(null); setOpen({}); setCols({}); setQ(""); if (conn) load(); }, [conn]);
  const toggle = (t) => { const k = t.name; setOpen((o) => ({ ...o, [k]: !o[k] })); if (!cols[k]) post("sql/columns", { id: conn, schema, table: t.name }).then((r) => setCols((c) => ({ ...c, [k]: r.ok ? r.columns : [{ name: r.error, type: "" }] }))).catch(() => {}); };
  const tabs = (data ? data.tables : []).filter((t) => !q.trim() || t.name.toLowerCase().includes(q.toLowerCase()));
  return html`<div class="sq-tree"><div class="sq-th"><b>Schema</b><span class="grow"></span><button class="ibtn sm" title="Refresh" aria-label="Refresh" onClick=${() => load(schema)}>${busy ? html`<i class="spin"></i>` : Ic("refresh", 13)}</button></div>
    ${data && data.schemas.length > 1 && html`<select class="sq-sel" value=${schema || ""} onChange=${(e) => load(e.target.value)} aria-label="Schema">${data.schemas.map((s) => html`<option value=${s}>${s}</option>`)}</select>`}
    <input class="sq-find" placeholder="Filter tables" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Filter tables"/>
    <div class="sq-tl">${err && (err.missing ? html`<${MissingHint} info=${err} env=${env} api=${api} post=${post} small=${true} onDone=${() => load()}/>` : html`<div class="sq-test bad small">${Ic("alert", 13)} <span>${err.error}</span></div>`)}
      ${tabs.map((t) => html`<div key=${t.name}><div class="sq-tr" onClick=${() => toggle(t)} onDblClick=${() => onOpen(t, schema)} title="Click: columns · Double-click: SELECT 100 rows"><i class=${"sq-chev" + (open[t.name] ? " o" : "")}>${Ic("right", 11)}</i>${Ic(t.kind === "view" ? "eye" : "table", 13)}<span class="sq-ell grow">${t.name}</span>
          <button class="ibtn sm" title="Insert name" aria-label="Insert name" onClick=${(e) => { e.stopPropagation(); onInsert(t.name); }}>${Ic("plus", 12)}</button></div>
        ${open[t.name] && html`<div class="sq-cl">${(cols[t.name] || []).map((c) => html`<div class="sq-co" key=${c.name} onClick=${() => onInsert(c.name)}><span class=${"sq-pk" + (c.pk ? " on" : "")}>${c.pk ? Ic("key", 10) : ""}</span><span class="sq-ell grow">${c.name}</span><em>${c.type}</em></div>`)}${!cols[t.name] && html`<div class="mute small" style="padding:2px 8px"><i class="spin"></i></div>`}</div>`}</div>`)}
      ${data && !tabs.length && html`<p class="mute small" style="padding:6px 8px">${q ? "No table matches." : "No tables in this schema."}</p>`}${data && data.more && html`<p class="mute small" style="padding:6px 8px">First 2000 shown, filter to narrow.</p>`}</div></div>`;
}

function Studio({ conns, post, api, envs, go, init }) {
  const [tabs, setTabs] = useState(() => { const t = LS.get("tabs", null); return Array.isArray(t) && t.length ? t : [{ id: uid(), name: "Query 1", sql: "", conn: "" }]; });
  const [cur, setCur] = useState(() => { const c = LS.get("cur", ""); return c; });
  const [results, setResults] = useState({}), [running, setRunning] = useState({}), [limit, setLimit] = useState(LS.get("limit", 1000)), [hist, setHist] = useState(() => LS.get("hist", [])), [showHist, setShowHist] = useState(false);
  const runId = useRef({}), tab = tabs.find((t) => t.id === cur) || tabs[0];
  useEffect(() => { LS.set("tabs", tabs.map((t) => ({ id: t.id, name: t.name, sql: t.sql, conn: t.conn }))); }, [tabs]);
  useEffect(() => { LS.set("cur", tab.id); }, [tab.id]);
  useEffect(() => { LS.set("limit", limit); }, [limit]);
  useEffect(() => { if (init && conns.some((c) => c.id === init)) patch({ conn: init }); }, [init]);
  const conn = conns.find((c) => c.id === tab.conn) || null;
  useEffect(() => { if (!tab.conn && conns.length) patch({ conn: conns[0].id }); }, [conns, tab.id, tab.conn]);
  const patch = (p) => setTabs((ts) => ts.map((t) => (t.id === tab.id ? { ...t, ...p } : t)));
  const addTab = (sql = "", name) => { const t = { id: uid(), name: name || "Query " + (tabs.length + 1), sql, conn: tab.conn }; setTabs((ts) => [...ts, t]); setCur(t.id); };
  const closeTab = (id) => { if (tabs.length === 1) { patch({ sql: "", name: "Query 1" }); setResults((r) => ({ ...r, [id]: null })); return; } const i = tabs.findIndex((t) => t.id === id); const rest = tabs.filter((t) => t.id !== id); setTabs(rest); if (cur === id || tab.id === id) setCur(rest[Math.max(0, i - 1)].id); };
  const insert = (txt) => patch({ sql: tab.sql + (tab.sql && !/\s$/.test(tab.sql) ? " " : "") + txt });
  const run = guard(async (sel) => {
    const sql = (sel != null ? sel : tab.sql).trim(); const id = tab.id;
    if (!conn) return toast("Pick a connection first");
    if (!sql) return toast("Write a statement first");
    const rid = uid(); runId.current[id] = rid; setRunning((r) => ({ ...r, [id]: true })); setResults((r) => ({ ...r, [id]: null }));
    setHist((h) => { const n = [{ sql, conn: conn.id, name: conn.name, at: Date.now() }, ...h.filter((x) => x.sql !== sql)].slice(0, 60); LS.set("hist", n); return n; });
    try { const r = await post("sql/run", { id: conn.id, sql, limit, run: rid }); setResults((x) => ({ ...x, [id]: r })); }
    catch (e) { setResults((x) => ({ ...x, [id]: { ok: false, error: e.message } })); }
    finally { setRunning((r) => ({ ...r, [id]: false })); }
  });
  const stop = () => post("sql/cancel", { run: runId.current[tab.id] }).catch(() => {});
  const res = results[tab.id], busy = !!running[tab.id];
  const open = (t, schema) => { const nm = (schema && !/^(main|public|dbo)$/i.test(schema) ? schema + "." : "") + t.name; patch({ sql: "select * from " + nm + " limit 100" }); setTimeout(() => run("select * from " + nm + " limit 100"), 0); };
  if (!conns.length) return html`<div class="sq-pane"><div class="sq-hero">${Ic("database", 34)}<h3>No connection yet</h3><p class="mute">Add a database to start writing SQL.</p><button class="btn acc" onClick=${() => go("connections")}>${Ic("plus", 14)} New connection</button></div></div>`;
  return html`<div class="sq-studio"><${Tree} conn=${tab.conn} env=${conn ? conn.env : ""} api=${api} post=${post} onInsert=${insert} onOpen=${open}/>
    <div class="sq-main"><div class="sq-tabs" role="tablist">${tabs.map((t) => html`<div key=${t.id} role="tab" aria-selected=${t.id === tab.id} class=${"sq-tab" + (t.id === tab.id ? " on" : "")} onClick=${() => setCur(t.id)}>${running[t.id] ? html`<i class="spin"></i>` : Ic("code", 12)}<span class="sq-ell">${t.name}</span><button class="ibtn sm" aria-label="Close tab" onClick=${(e) => { e.stopPropagation(); closeTab(t.id); }}>${Ic("x", 11)}</button></div>`)}
        <button class="ibtn" title="New query tab" aria-label="New query tab" onClick=${() => addTab()}>${Ic("plus", 14)}</button></div>
      <div class="sq-bar"><select class="sq-conn" value=${tab.conn} onChange=${(e) => patch({ conn: e.target.value })} aria-label="Connection"><option value="">Select a connection…</option>${conns.map((c) => html`<option value=${c.id}>${c.name}${c.readonly ? " (read-only)" : ""}</option>`)}</select>
        ${busy ? html`<button class="btn bad" onClick=${stop}>${Ic("stop", 12)} Stop</button>` : html`<button class="btn acc" disabled=${!conn} onClick=${() => run()}>${Ic("play", 13)} Run <kbd>⌘↵</kbd></button>`}
        <label class="sq-lim mute small">Rows <select value=${limit} onChange=${(e) => setLimit(+e.target.value)} aria-label="Row limit">${[100, 500, 1000, 5000, 20000, 50000].map((n) => html`<option value=${n}>${num(n)}</option>`)}</select></label><span class="grow"></span>
        <div class="sq-hw"><button class="btn sm" onClick=${() => setShowHist(!showHist)} aria-expanded=${showHist}>${Ic("clock", 13)} History</button>
          ${showHist && html`<div class="sq-hist"><div class="row sq-hh"><b class="grow">Recent queries</b><button class="btn sm" onClick=${() => { setHist([]); LS.set("hist", []); }}>Clear</button></div>${!hist.length && html`<p class="mute small" style="padding:10px">Nothing yet.</p>`}
            ${hist.map((h) => html`<button class="sq-hi" onClick=${() => { patch({ sql: h.sql, conn: conns.some((c) => c.id === h.conn) ? h.conn : tab.conn }); setShowHist(false); }}><code>${h.sql.replace(/\s+/g, " ").slice(0, 140)}</code><span class="mute small">${h.name} · ${new Date(h.at).toLocaleString()}</span></button>`)}</div>`}</div>
        <button class="btn sm" title="Rename this tab" onClick=${guard(async () => { const n = prompt("Tab name", tab.name); if (n && n.trim()) patch({ name: n.trim().slice(0, 40) }); })}>${Ic("edit", 13)}</button></div>
      <${Editor} value=${tab.sql} onChange=${(v) => patch({ sql: v })} onRun=${run} ro=${false}/>
      <div class="sq-out">${busy && html`<div class="sq-load"><i class="spin"></i> Running…<button class="btn sm" onClick=${stop}>Stop</button></div>`}
        ${!busy && !res && html`<div class="sq-empty big">${Ic("table", 26)}<div>Results appear here</div><div class="mute small">Double-click a table in the tree to preview it.</div></div>`}
        ${!busy && res && res.ok && (res.columns.length ? html`<${Grid} res=${res} name=${(conn && conn.name) || "result"}/>` : html`<div class="sq-test ok" style="margin:14px">${Ic("check", 15)} Done${res.rowcount != null && res.rowcount >= 0 ? " · " + num(res.rowcount) + " row" + (res.rowcount === 1 ? "" : "s") + " affected" : ""} · ${res.ms} ms</div>`)}
        ${!busy && res && !res.ok && (res.missing ? html`<div class="sq-errbox"><${MissingHint} info=${res} env=${conn ? conn.env : ""} api=${api} post=${post} onDone=${() => run()}/></div>`
          : html`<div class="sq-errbox"><div class="sq-test bad">${Ic("alert", 15)} <span>${res.error}</span></div></div>`)}</div></div></div>`;
}

export function SqlStudioPage({ api, post, arg }) {
  const [view, setView] = useState(() => (arg && ["studio", "connections", "drivers"].includes(arg.split("/")[0]) ? arg.split("/")[0] : LS.get("view", "studio")));
  const [d, setD] = useState({ connections: [], envs: [], ui: "" }), [loaded, setLoaded] = useState(false), [edit, setEdit] = useState(null), [err, setErr] = useState(""), [init, setInit] = useState("");
  const reload = useCallback(() => api("sql/connections").then((x) => { setD(x); setLoaded(true); setErr(""); }).catch((e) => { setErr(e.message); setLoaded(true); }), []);
  useEffect(() => { reload(); }, []);
  const go = (v) => { setView(v); LS.set("view", v); };
  useEffect(() => { document.title = "SQL Studio"; }, []);
  const items = [{ id: "studio", label: "Studio", icon: "terminal" }, { id: "connections", label: "Connections", icon: "database" }, { id: "drivers", label: "Drivers", icon: "download" }];
  return html`<div class="sqs"><div class="sq-head"><span class="sq-logo">${Ic("database", 18)}</span><div class="grow"><h1 style="font-size:20px;margin:0">SQL Studio</h1><div class="mute small">Browse, query and export from any SQLAlchemy database</div></div>
      <${Seg} value=${view} onChange=${go} items=${items}/></div>
    ${err && html`<div class="sq-test bad" style="margin:12px 20px">${Ic("alert", 15)} ${err}</div>`}
    ${!loaded ? html`<div class="sq-pane"><p class="mute">Loading…</p></div>`
    : view === "studio" ? html`<${Studio} conns=${d.connections} post=${post} api=${api} envs=${d.envs} go=${(v) => { go(v); if (v === "connections") setEdit({}); }} init=${init}/>`
    : view === "connections" ? html`<${Connections} conns=${d.connections} envs=${d.envs} ui=${d.ui} api=${api} post=${post} reload=${reload} edit=${edit} setEdit=${setEdit} openStudio=${(id) => { setInit(id); go("studio"); }}/>`
    : html`<${Drivers} api=${api} post=${post} envs=${d.envs} ui=${d.ui} reloadEnvs=${reload} onUseUrl=${(url, env) => { go("connections"); setEdit({ driver: "other", url, env, name: "" }); }}/>`}</div>`;
}
