// SQL Studio: a lightweight, browser-based SQL client. Saved connections (any SQLAlchemy database), schema browser, query tabs,
// result grid with export, and a Drivers tab that installs the extra packages a database needs into a chosen Python environment.
// Data: /api/sql/* (server.py -> aihubui/sqlstudio.py; queries run in a child Python of the connection's environment).
import { html, useState, useEffect, useRef, useMemo, useCallback } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, guard, Seg, Switch, confirmBox, ask } from "./ui.js";
import { CodeBox } from "./codebox.js";
import { sqlComplete, sqlSignature, SQL_WORDS, forgetSchema } from "./sqlintel.js";

const LS = { get(k, d) { try { const v = localStorage.getItem("sqls_" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }, set(k, v) { try { localStorage.setItem("sqls_" + k, JSON.stringify(v)); } catch (e) { /* private mode */ } } };
const uid = () => Math.random().toString(36).slice(2, 10);
const num = (n) => Number(n).toLocaleString();
const KW = "select from where group by order having limit offset join left right inner outer full cross on as and or not in is null like ilike between exists case when then else end union all distinct insert into values update set delete create table alter drop index view with asc desc count sum avg min max cast over partition returning using primary key foreign references default explain describe show pragma top true false".split(" ");
const KWRE = new RegExp("^(" + KW.join("|") + ")$", "i");
const DRIVER_LABEL = { postgresql: "PostgreSQL", mysql: "MySQL", sqlite: "SQLite", mssql: "SQL Server", "mssql-odbc": "SQL Server (ODBC)", oracle: "Oracle", trino: "Trino", duckdb: "DuckDB", other: "SQLAlchemy URL" };
const PORTS = { postgresql: "5432", mysql: "3306", mssql: "1433", "mssql-odbc": "1433", oracle: "1521", trino: "443" };
const FILE_DB = ["sqlite", "duckdb"];

// Brand icons come from a public icon host (Simple Icons). Offline or blocked: the image is dropped and the name alone is shown.
const ICON_HOST = "https://cdn.simpleicons.org/";
const ICON_SLUG = { postgresql: "postgresql", mysql: "mysql", sqlite: "sqlite", mssql: "microsoftsqlserver", "mssql-odbc": "microsoftsqlserver", oracle: "oracle", trino: "trino", duckdb: "duckdb",
  clickhouse: "clickhouse", snowflake: "snowflake", bigquery: "googlebigquery", redshift: "amazonredshift", databricks: "databricks", cockroachdb: "cockroachlabs", hana: "sap", db2: "ibm", teradata: "teradata" };
const iconBad = new Set();
function DbIcon({ k, size = 22 }) {
  const [bad, setBad] = useState(() => iconBad.has(k)), slug = ICON_SLUG[k];
  if (!slug || bad) return null;
  return html`<img class="sq-dbi" src=${ICON_HOST + slug} width=${size} height=${size} alt="" loading="lazy" referrerpolicy="no-referrer" onError=${() => { iconBad.add(k); setBad(true); }}/>`;
}

// `:name` bind parameters outside quotes and comments (mirrors sqlrunner.param_spans)
function paramNames(sql) {
  const out = [], n = sql.length; let i = 0;
  while (i < n) {
    const c = sql[i];
    if (c === "'" || c === '"' || c === "`") { let j = i + 1; while (j < n) { if (sql[j] === c) { if (sql[j + 1] === c) { j += 2; continue; } break; } j++; } i = j + 1; }
    else if (sql.startsWith("--", i)) { const j = sql.indexOf("\n", i); i = j < 0 ? n : j; }
    else if (sql.startsWith("/*", i)) { const j = sql.indexOf("*/", i + 2); i = j < 0 ? n : j + 2; }
    else if (c === ":") {
      if (sql[i + 1] === ":") { i += 2; continue; }
      const m = /^[A-Za-z_][A-Za-z0-9_]*/.exec(sql.slice(i + 1, i + 80)), prev = sql[i - 1];
      if (m && (i === 0 || (prev !== ":" && prev !== "\\" && !/[A-Za-z0-9_]/.test(prev)))) { if (!out.includes(m[0])) out.push(m[0]); i += 1 + m[0].length; } else i++;
    } else i++;
  }
  return out;
}

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

function Editor({ value, onChange, onRun, conn, post }) {
  const complete = useMemo(() => sqlComplete(post, conn || ""), [conn]);
  const key = (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); const t = e.target, sel = t.value.slice(t.selectionStart, t.selectionEnd); onRun(sel.trim() ? sel : null); } };
  const lines = value.split("\n").length;
  return html`<div class="sq-ed"><div class="sq-edscroll"><div class="sq-gut" aria-hidden="true">${Array.from({ length: Math.min(lines, 2000) }, (_, i) => html`<span>${i + 1}</span>`)}</div>
    <${CodeBox} value=${value} onChange=${onChange} lang="sql" nowrap=${true} intel=${true} complete=${complete} words=${SQL_WORDS} signature=${sqlSignature} onKey=${key} label="SQL editor" placeholder="select * from …   (Ctrl/⌘ + Enter runs; a selection runs alone)"/></div></div>`;
}

// ---------- result grid
const isNum = (v) => typeof v === "number";
function csv(cols, rows) { const q = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v)); return [cols.map(q).join(","), ...rows.map((r) => r.map(q).join(","))].join("\n"); }
function download(name, text, type) { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000); }

export function Grid({ res, name }) {
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
  return html`<button type="button" class=${"sq-tile" + (on ? " on" : "")} onClick=${() => set(k)}><${DbIcon} k=${k} size=${20}/><span>${DRIVER_LABEL[k] || k}</span></button>`;
}

function ConnForm({ init, envs, ui, api, post, close, saved, onOpen, onDelete }) {
  const [f, setF] = useState(() => ({ driver: "postgresql", name: "", host: "", port: "5432", user: "", password: "", database: "", options: "", url: "", env: "", readonly: false, ...init }));
  const [test, setTest] = useState(null), [busy, setBusy] = useState(false), set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target ? e.target.value : e }));
  const drv = f.driver, file = FILE_DB.includes(drv), url = drv === "other";
  const pick = (k) => setF((x) => ({ ...x, driver: k, port: x.port && Object.values(PORTS).includes(x.port) || !x.port ? PORTS[k] || "" : x.port }));
  const run = guard(async () => { setBusy(true); setTest(null); try { setTest(await post("sql/test", { connection: f })); } finally { setBusy(false); } });
  const save = guard(async () => { setBusy(true); try { const r = await post("sql/save", { connection: f }); saved(r.connection); } finally { setBusy(false); } });
  const field = (label, k, ph, type = "text", cls = "") => html`<label class=${"sq-f " + cls}><span>${label}</span><input type=${type} value=${f[k]} placeholder=${ph || ""} onInput=${set(k)} autocomplete="off"/></label>`;
  const known = Object.keys(DRIVER_LABEL);
  return html`<div class="ms-edit"><div class="ms-hd"><h3 class="grow">${f.id ? f.name || "Edit connection" : "New connection"}</h3>${f.id && html`<button class="btn sm acc" onClick=${() => onOpen(f.id)}>${Ic("play", 12)} Open in Studio</button>`}</div>
    <div class="ms-sec"><h4>Database</h4><div class="sq-tiles">${known.map((k) => html`<${DriverTile} k=${k} on=${drv === k} set=${pick}/>`)}</div></div>
    <div class="ms-sec"><h4>Connection</h4>${field("Name", "name", "Production replica")}
      ${url ? html`<label class="sq-f"><span>SQLAlchemy URL ${f.url_masked && !f.url ? html`<em class="mute">saved: ${f.url_masked} (leave blank to keep)</em>` : ""}</span>
          <input type="password" value=${f.url} placeholder="postgresql+psycopg://user:password@host:5432/dbname" onInput=${set("url")} autocomplete="off"/>
          <small class="mute">Any dialect SQLAlchemy supports. Install its driver in the Drivers tab first.</small></label>`
      : file ? field("Database file", "database", drv === "duckdb" ? "/path/to/file.duckdb (empty = in memory)" : "/path/to/file.sqlite (empty = in memory)")
      : html`<div class="sq-grid2">${field("Host", "host", "db.example.com")}${field("Port", "port", PORTS[drv] || "")}${field("User", "user", "")}
          <label class="sq-f"><span>Password ${f.has_secret && !f.password ? html`<em class="mute">saved (leave blank to keep)</em>` : ""}</span><input type="password" value=${f.password} onInput=${set("password")} autocomplete="new-password"/></label>
          ${field("Database", "database", drv === "oracle" ? "service name" : drv === "trino" ? "catalog/schema" : "", "text", "span2")}${field("Extra options", "options", "sslmode=require&connect_timeout=10", "text", "span2")}</div>`}</div>
    <div class="ms-sec"><h4>Run</h4><label class="sq-f"><span>Python environment</span><select value=${f.env || ""} onChange=${set("env")}><option value="">UI's own Python</option>${envs.map((e) => html`<option value=${e.name}>${e.name}</option>`)}</select>
        <small class="mute">Queries run in this environment, so the drivers you install for it (Drivers tab) are found. ${ui ? "UI Python: " + ui : ""}</small></label>
      <div class="row sq-ro"><${Switch} on=${f.readonly} onChange=${(v) => setF((x) => ({ ...x, readonly: v }))} label="Read-only"/><div><b>Read-only</b><div class="mute small">Only SELECT, WITH, SHOW, EXPLAIN… statements are allowed on this connection.</div></div></div></div>
    ${test && test.missing ? html`<${MissingHint} info=${test} env=${f.env} api=${api} post=${post} small=${true} onDone=${run}/>`
      : test && html`<div class=${"sq-test " + (test.ok ? "ok" : "bad")}>${test.ok ? html`${Ic("check", 15)} Connected${test.ms != null ? " in " + test.ms + " ms" : ""} · ${test.dialect}${test.server ? " · " + test.server.slice(0, 80) : ""}` : html`${Ic("alert", 15)} <span>${test.error}</span>`}</div>`}
    <div class="ms-bar"><button class="btn" disabled=${busy} onClick=${run}>${busy ? html`<i class="spin"></i>` : Ic("bolt", 14)} Test connection</button><span class="grow"></span>
      ${f.id && html`<button class="btn bad" onClick=${() => onDelete(f)}>${Ic("trash", 13)} Delete</button>`}<button class="btn" onClick=${close}>Revert</button><button class="btn acc" disabled=${busy || !f.name.trim()} onClick=${save}>Save</button></div></div>`;
}

function Connections({ conns, envs, ui, api, post, reload, openStudio, edit, setEdit }) {
  const cur = edit || conns[0] || null, isNew = cur && !cur.id;
  const sub = (c) => (c.driver === "other" ? c.url_masked || "URL" : FILE_DB.includes(c.driver) ? c.database || "in memory" : (c.host || "?") + (c.port ? ":" + c.port : "") + (c.database ? "/" + c.database : ""));
  const del = guard(async (c) => { if (await confirmBox({ title: "Delete " + c.name + "?", text: "The saved password is removed too.", ok: "Delete", danger: true })) { await post("sql/delete", { id: c.id }); setEdit(null); reload(); } });
  return html`<div class="sq-pane"><div class="ms"><div class="ms-list ms-panel"><div class="ms-ph"><b>Connections</b><span>${conns.length}</span></div>
      ${conns.map((c) => html`<button key=${c.id} class=${"nbj-srow" + (cur && cur.id === c.id ? " on" : "")} onClick=${() => setEdit(c)}><${DbIcon} k=${c.driver} size=${20}/><span class="nbj-fn"><span class="sq-ell">${c.name}</span><small>${DRIVER_LABEL[c.driver] || c.driver} · ${sub(c)}</small></span>${c.readonly ? Ic("eye", 12) : ""}</button>`)}
      <button class=${"btn ms-add" + (isNew ? " on" : "")} onClick=${() => setEdit({})}>${Ic("plus", 14)} New connection</button><p class="mute small ms-note">Passwords are stored encrypted on this machine and never sent back to the browser.</p></div>
      ${cur ? html`<${ConnForm} key=${cur.id || "new"} init=${cur} envs=${envs} ui=${ui} api=${api} post=${post} onOpen=${openStudio} onDelete=${del} close=${() => setEdit(null)} saved=${(c) => { setEdit(c); reload(); toast("Saved " + c.name); }}/>`
      : html`<div class="ms-empty">${Ic("database", 34)}<h3>Connect your first database</h3><p style="margin:0;max-width:46ch">PostgreSQL, MySQL, SQL Server, Oracle, Trino, SQLite, DuckDB, or anything SQLAlchemy reaches with a URL.</p><button class="btn acc" onClick=${() => setEdit({})}>${Ic("plus", 14)} New connection</button></div>`}</div></div>`;
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
  return html`<div class="sq-pane"><p class="mute small" style="margin:0 0 12px">SQLAlchemy plus one small package per database. Install into the UI's Python or into an isolated environment.</p>
    <div class="sq-envbar" style="margin-top:0"><label class="sq-f"><span>Environment</span><select value=${env} onChange=${(e) => setEnv(e.target.value)}><option value="">UI's own Python</option>${envs.map((e) => html`<option value=${e.name}>${e.name}</option>`)}${env && !envs.some((e) => e.name === env) && html`<option value=${env}>${env} (new)</option>`}</select></label>
      <label class="sq-f"><span>Or create a new one</span><span class="row" style="gap:6px"><input style="width:170px" placeholder="name, e.g. sqlenv" value=${newEnv} onInput=${(e) => setNewEnv(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && create()} aria-label="New environment name"/><button class="btn" disabled=${!newEnv.trim() || running} onClick=${create}>${Ic("plus", 13)} Create</button></span></label></div>
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
    <div class="sq-drv">${list.map((x) => html`<div class=${"sq-dv" + (x.installed ? " ok" : "")} key=${x.key}><${DbIcon} k=${x.key} size=${26}/>
      <div class="grow" style="min-width:0"><b>${x.title}</b><div class="mute small sq-ell">${x.builtin ? "Built into Python, nothing to install" : (x.pips || []).join(" + ")}${x.version ? " · v" + x.version : ""}</div>${x.note && html`<div class="mute small">${x.note}</div>`}</div>
      ${x.installed ? html`<span class="tag good">${Ic("check", 11)} ready</span>` : html`<button class="btn sm acc" disabled=${running} onClick=${() => go(x.pips)}>${Ic("download", 13)} Install</button>`}
      ${x.other && html`<button class="ibtn" title="Start a connection with this URL template" aria-label="Use" onClick=${() => onUseUrl(x.url, env)}>${Ic("link", 14)}</button>`}</div>`)}</div>
    <p class="mute small" style="margin-top:14px">Not listed? Any SQLAlchemy dialect works: install its package here (create an environment and use <b>pip</b> in a terminal) and connect with the <b>SQLAlchemy URL</b> type.</p></div>`;
}

// ---------- studio
function Tree({ conn, env, api, post, onInsert, onOpen, head }) {
  const [data, setData] = useState(null), [err, setErr] = useState(null), [schema, setSchema] = useState(null), [open, setOpen] = useState({}), [cols, setCols] = useState({}), [q, setQ] = useState(""), [busy, setBusy] = useState(false);
  const load = (sch) => { if (sch == null) forgetSchema(conn); setBusy(true); setErr(null); post("sql/tables", { id: conn, schema: sch == null ? undefined : sch }).then((r) => { if (!r.ok) setErr(r); else { setData(r); setSchema(r.schema); } }).catch((e) => setErr({ error: e.message })).finally(() => setBusy(false)); };
  useEffect(() => { setData(null); setOpen({}); setCols({}); setQ(""); if (conn) load(); }, [conn]);
  const toggle = (t) => { const k = t.name; setOpen((o) => ({ ...o, [k]: !o[k] })); if (!cols[k]) post("sql/columns", { id: conn, schema, table: t.name }).then((r) => setCols((c) => ({ ...c, [k]: r.ok ? r.columns : [{ name: r.error, type: "" }] }))).catch(() => {}); };
  const tabs = (data ? data.tables : []).filter((t) => !q.trim() || t.name.toLowerCase().includes(q.toLowerCase()));
  return html`<div class="sq-tree"><div class="nbj-ph"><b>Schema</b><button class="ibtn sm" title="Refresh" aria-label="Refresh" onClick=${() => load(schema)}>${busy ? html`<i class="spin"></i>` : Ic("refresh", 14)}</button>${head}</div>
    ${data && data.schemas.length > 1 && html`<select class="sq-sel" value=${schema || ""} onChange=${(e) => load(e.target.value)} aria-label="Schema">${data.schemas.map((s) => html`<option value=${s}>${s}</option>`)}</select>`}
    <input class="sq-find" placeholder="Filter tables" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Filter tables"/>
    <div class="sq-tl">${err && (err.missing ? html`<${MissingHint} info=${err} env=${env} api=${api} post=${post} small=${true} onDone=${() => load()}/>` : html`<div class="sq-test bad small">${Ic("alert", 13)} <span>${err.error}</span></div>`)}
      ${tabs.map((t) => html`<div key=${t.name}><div class="sq-tr" onClick=${() => toggle(t)} onDblClick=${() => onOpen(t, schema)} title="Click: columns · Double-click: SELECT 100 rows"><i class=${"sq-chev" + (open[t.name] ? " o" : "")}>${Ic("right", 11)}</i>${Ic(t.kind === "view" ? "eye" : "table", 13)}<span class="sq-ell grow">${t.name}</span>
          <button class="ibtn sm" title="Insert name" aria-label="Insert name" onClick=${(e) => { e.stopPropagation(); onInsert(t.name); }}>${Ic("plus", 12)}</button></div>
        ${open[t.name] && html`<div class="sq-cl">${(cols[t.name] || []).map((c) => html`<div class="sq-co" key=${c.name} onClick=${() => onInsert(c.name)}><span class=${"sq-pk" + (c.pk ? " on" : "")}>${c.pk ? Ic("key", 10) : ""}</span><span class="sq-ell grow">${c.name}</span><em>${c.type}</em></div>`)}${!cols[t.name] && html`<div class="mute small" style="padding:2px 8px"><i class="spin"></i></div>`}</div>`}</div>`)}
      ${data && !tabs.length && html`<p class="mute small" style="padding:6px 8px">${q ? "No table matches." : "No tables in this schema."}</p>`}${data && data.more && html`<p class="mute small" style="padding:6px 8px">First 2000 shown, filter to narrow.</p>`}</div></div>`;
}

// Magic SQL: describe what you want, the model writes it with the schemas of this connection (and any others ticked) as context
function AskAi({ conns, conn, tab, res, post, apply }) {
  const [q, setQ] = useState(""), [busy, setBusy] = useState(false), [note, setNote] = useState(""), [more, setMore] = useState([]), [pick, setPick] = useState(false);
  const failed = res && !res.ok && res.error && !res.missing;
  const go = guard(async (fix) => {
    if (!conn) return toast("Pick a connection first");
    const hasSql = !!tab.sql.trim();
    if (!fix && !q.trim()) return toast("Describe what you want");
    setBusy(true); setNote("");
    try {
      const r = await post("sql/ask", { ask: fix ? "Fix the error." + (q.trim() ? " " + q : "") : q, conn: conn.id, sql: tab.sql, error: fix && failed ? res.error : "", others: more });
      if (!r.sql) { setNote(r.note || "No SQL came back."); return; }
      apply(r, fix || (hasSql && /\b(fix|change|add|remove|replace|rewrite|instead|also|only)\b/i.test(q)) ? "fix" : "new");
      setNote(r.note || ""); setQ("");
    } finally { setBusy(false); }
  });
  const others = conns.filter((c) => !conn || c.id !== conn.id);
  return html`<div class="sq-ai"><div class="row" style="gap:6px">${Ic("sparkles", 15)}
      <input class="grow" placeholder=${"Ask AI: " + (tab.sql.trim() ? "change this query, or describe a new one" : "e.g. top 10 customers by revenue last month")} value=${q} disabled=${busy}
        onInput=${(e) => setQ(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && !e.shiftKey && go(false)} aria-label="Ask AI to write SQL"/>
      ${others.length > 0 && html`<div class="sq-hw"><button class="btn sm" title="Let the AI also see the schemas of other connections" onClick=${() => setPick(!pick)} aria-expanded=${pick}>${Ic("database", 13)} ${more.length ? "+" + more.length : "Context"}</button>
        ${pick && html`<div class="sq-hist"><div class="row sq-hh"><b class="grow">Also give the AI the schema of</b></div>${others.map((c) => html`<label class="sq-hi"><span><input type="checkbox" checked=${more.includes(c.id)} onChange=${() => setMore((m) => m.includes(c.id) ? m.filter((x) => x !== c.id) : [...m, c.id])}/> ${c.name}</span></label>`)}</div>`}</div>`}
      <button class="btn acc" disabled=${busy || !conn} onClick=${() => go(false)}>${busy ? html`<i class="spin"></i>` : Ic("sparkles", 13)} Generate</button>
      ${failed && html`<button class="btn" disabled=${busy} title="Send the error and the SQL to the AI" onClick=${() => go(true)}>${Ic("bolt", 13)} Fix error</button>`}</div>
    ${note && html`<div class="mute small sq-ainote">${note} <button class="ibtn sm" aria-label="Dismiss" onClick=${() => setNote("")}>${Ic("x", 11)}</button></div>`}</div>`;
}

function Studio({ conns, post, api, envs, go, init }) {
  const [tabs, setTabs] = useState(() => { const t = LS.get("tabs", null); return Array.isArray(t) && t.length ? t : [{ id: uid(), name: "Query 1", sql: "", conn: "" }]; });
  const [cur, setCur] = useState(() => { const c = LS.get("cur", ""); return c; });
  const [results, setResults] = useState({}), [running, setRunning] = useState({}), [limit, setLimit] = useState(LS.get("limit", 1000)), [hist, setHist] = useState(() => LS.get("hist", [])), [showHist, setShowHist] = useState(false);
  const [files, setFiles] = useState([]), [panel, setPanelS] = useState(() => LS.get("panel", "schema")), setPanel = (v) => { setPanelS(v); LS.set("panel", v); };
  const loadFiles = useCallback(() => api("sql/files").then((r) => setFiles(r.files || [])).catch(() => {}), []);
  useEffect(() => { loadFiles(); }, []);
  const runId = useRef({}), tab = tabs.find((t) => t.id === cur) || tabs[0];
  useEffect(() => { const w = setTimeout(() => LS.set("tabs", tabs.map((t) => ({ id: t.id, name: t.name, sql: t.sql, conn: t.conn, params: t.params || {}, file: t.file || "" }))), 400); return () => clearTimeout(w); }, [tabs]);       // not on every keystroke
  useEffect(() => { LS.set("cur", tab.id); }, [tab.id]);
  useEffect(() => { LS.set("limit", limit); }, [limit]);
  useEffect(() => { if (init && conns.some((c) => c.id === init)) patch({ conn: init }); }, [init]);
  const conn = conns.find((c) => c.id === tab.conn) || null;
  // SQL handed over from a chat block: a new tab on the connection named in its `-- conn:` line
  const takeInbox = useCallback(() => {
    let m = null; try { m = JSON.parse(localStorage.getItem("sqls_inbox") || "null"); localStorage.removeItem("sqls_inbox"); } catch (e) { /* none */ }
    if (!m || !m.sql || Date.now() - m.at > 60000) return;
    const cm = m.sql.match(/^\s*--\s*conn:\s*(.+)/i), nm = cm ? cm[1].trim().toLowerCase() : "";
    const c = nm && conns.find((x) => x.id === nm || x.name.toLowerCase() === nm);
    const t = { id: uid(), name: "From chat", sql: m.sql.trim() + "\n", conn: c ? c.id : tab.conn };
    setTabs((ts) => [...ts, t]); setCur(t.id); toast(c ? "Opened on " + c.name : "Opened in a new tab");
  }, [conns, tab.conn]);
  useEffect(() => { if (conns.length) takeInbox(); }, [conns.length]);
  useEffect(() => { addEventListener("sqlstudio:inbox", takeInbox); return () => removeEventListener("sqlstudio:inbox", takeInbox); }, [takeInbox]);
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
    try { const r = await post("sql/run", { id: conn.id, sql, limit, run: rid, params: usedParams(sel != null ? sel : tab.sql) }); setResults((x) => ({ ...x, [id]: r })); }
    catch (e) { setResults((x) => ({ ...x, [id]: { ok: false, error: e.message } })); }
    finally { setRunning((r) => ({ ...r, [id]: false })); }
  });
  const names = useMemo(() => paramNames(tab.sql), [tab.sql]);
  const usedParams = (sql) => { const o = {}; for (const n of paramNames(sql)) o[n] = (tab.params || {})[n] ?? ""; return o; };
  const setParam = (n, v) => patch({ params: { ...(tab.params || {}), [n]: v } });
  // Save = a named SQL file with its connection and parameter values as defaults; the "SQL file" work node runs it and may override the parameters
  const saveFile = guard(async () => {
    if (!tab.sql.trim()) return toast("Write a statement first");
    let name = tab.file ? tab.name : "";
    if (!name || /^(Query \d+|From chat|AI query)$/.test(name)) { const v = await ask({ title: "Save SQL file", submit: "Save", fields: [{ name: "name", label: "Name", type: "text", value: /^(Query \d+|From chat|AI query)$/.test(tab.name) ? "" : tab.name }] }); if (!v || !v.name.trim()) return; name = v.name.trim(); }
    const r = await post("sql/file/save", { file: { id: tab.file || undefined, name, sql: tab.sql, conn: tab.conn, params: names.map((n) => ({ name: n, value: String((tab.params || {})[n] ?? "") })) } });
    patch({ file: r.file.id, name: r.file.name }); loadFiles(); toast("Saved " + r.file.name + (names.length ? " with " + names.length + " parameter" + (names.length === 1 ? "" : "s") : ""));
  });
  const openFile = (f) => {
    const ex = tabs.find((t) => t.file === f.id);
    if (ex) { setCur(ex.id); return; }
    const t = { id: uid(), name: f.name, sql: f.sql, conn: conns.some((c) => c.id === f.conn) ? f.conn : tab.conn, params: Object.fromEntries((f.params || []).map((p) => [p.name, p.value])), file: f.id };
    setTabs((ts) => [...ts, t]); setCur(t.id);
  };
  const delFile = guard(async (f) => { if (!(await confirmBox({ title: "Delete " + f.name + "?", text: "Works that use this file will fail until it is saved again.", ok: "Delete", danger: true }))) return; await post("sql/file/delete", { id: f.id }); loadFiles(); });
  const stop = () => post("sql/cancel", { run: runId.current[tab.id] }).catch(() => {});
  const res = results[tab.id], busy = !!running[tab.id];
  // AI answer: an empty editor is filled; a fix replaces the SQL (the old one goes to History); anything else opens a new tab, nothing is lost
  const applyAi = (r, mode) => {
    const cid = r.conn && conns.some((c) => c.id === r.conn) ? r.conn : tab.conn;
    if (mode === "fix" && tab.sql.trim()) { setHist((h) => { const n = [{ sql: tab.sql, conn: tab.conn, name: (conn && conn.name) || "", at: Date.now() }, ...h].slice(0, 60); LS.set("hist", n); return n; }); patch({ sql: r.sql + "\n", conn: cid }); }
    else if (!tab.sql.trim()) patch({ sql: r.sql + "\n", conn: cid });
    else { const t = { id: uid(), name: "AI query", sql: r.sql + "\n", conn: cid }; setTabs((ts) => [...ts, t]); setCur(t.id); }
  };
  const open = (t, schema) => { const nm = (schema && !/^(main|public|dbo)$/i.test(schema) ? schema + "." : "") + t.name; patch({ sql: "select * from " + nm + " limit 100" }); setTimeout(() => run("select * from " + nm + " limit 100"), 0); };
  if (!conns.length) return html`<div class="sq-pane setup"><div class="sq-hero">${Ic("database", 34)}<h3>No connection yet</h3><p class="mute">Add a database to start writing SQL.</p><button class="btn acc" onClick=${() => go("connections")}>${Ic("plus", 14)} New connection</button></div></div>`;
  const rail = (id, icon, title) => html`<button class=${"nbj-rb" + (panel === id ? " on" : "")} title=${title} aria-label=${title} aria-pressed=${panel === id} onClick=${() => setPanel(panel === id ? "" : id)}>${Ic(icon, 20)}</button>`;
  const addParam = guard(async () => { const v = await ask({ title: "Add a parameter", submit: "Insert", fields: [{ name: "name", label: "Name (letters, digits, _)", type: "text", value: "" }] }); const n = v && v.name.trim().replace(/[^A-Za-z0-9_]/g, "_"); if (!n) return; insert(":" + n); });
  const close = html`<span class="grow"></span><button class="ibtn sm" title="Close panel" aria-label="Close panel" onClick=${() => setPanel("")}>${Ic("x", 14)}</button>`;
  return html`<div class=${"nbj-ws2 sqw" + (panel ? "" : " nopanel")}>
    <div class="nbj-rail">${rail("schema", "database", "Schema")}${rail("files", "file", "Saved SQL files")}${rail("hist", "clock", "History")}<span class="nbj-rsep"></span>${rail("set", "sliders", "Settings: connection and rows")}</div>
    ${panel === "schema" && html`<aside class="nbj-panel" aria-label="Schema"><${Tree} conn=${tab.conn} env=${conn ? conn.env : ""} api=${api} post=${post} onInsert=${insert} onOpen=${open} head=${close}/></aside>`}
    ${panel === "files" && html`<aside class="nbj-panel" aria-label="Saved SQL files"><div class="nbj-ph"><b>Files</b>${close}</div>
      <div class="nbj-list">${!files.length && html`<p class="mute small" style="padding:10px 8px">Nothing saved yet. Write a query, add :parameters if you like, and press Save.</p>`}
        ${files.map((f) => html`<div class="nbj-srow sqf" key=${f.id}><button class="sqf-o" onClick=${() => openFile(f)}><span class="nbj-fn"><span class="sq-ell">${f.name}</span><small>${(f.params || []).length ? (f.params || []).map((q) => ":" + q.name).join(" ") + " · " : ""}${(conns.find((c) => c.id === f.conn) || {}).name || "no connection"}</small></span></button><button class="ibtn sm" aria-label="Delete" onClick=${() => delFile(f)}>${Ic("trash", 12)}</button></div>`)}</div></aside>`}
    ${panel === "hist" && html`<aside class="nbj-panel" aria-label="History"><div class="nbj-ph"><b>History</b>${close}</div>
      <div class="nbj-list">${!hist.length && html`<p class="mute small" style="padding:10px 8px">Nothing yet.</p>`}
        ${hist.map((h) => html`<button class="nbj-srow" onClick=${() => patch({ sql: h.sql, conn: conns.some((c) => c.id === h.conn) ? h.conn : tab.conn })}><span class="nbj-fn"><code class="sq-ell">${h.sql.replace(/\s+/g, " ").slice(0, 80)}</code><small>${h.name} · ${new Date(h.at).toLocaleString()}</small></span></button>`)}
        ${hist.length > 0 && html`<button class="btn sm nbj-manage" onClick=${() => { setHist([]); LS.set("hist", []); }}>Clear history</button>`}</div></aside>`}
    ${panel === "set" && html`<aside class="nbj-panel" aria-label="Settings"><div class="nbj-ph"><b>Settings</b>${close}</div>
      <div class="nbj-list nbj-setbody"><h6 class="nbj-sec">Connection <span>${conns.length}</span></h6>
        ${conns.map((c) => html`<button key=${c.id} class=${"nbj-srow" + (c.id === tab.conn ? " on" : "")} onClick=${() => patch({ conn: c.id })} aria-pressed=${c.id === tab.conn}><${DbIcon} k=${c.driver} size=${18}/><span class="nbj-fn"><span class="sq-ell">${c.name}</span><small>${DRIVER_LABEL[c.driver] || c.driver}${c.readonly ? " · read-only" : ""}</small></span>${c.id === tab.conn ? Ic("check", 14) : ""}</button>`)}
        <button class="btn sm nbj-manage" onClick=${() => go("connections")}>${Ic("plus", 12)} Add or edit connections</button>
        <h6 class="nbj-sec">Results</h6>
        <label class="nbj-srow" style="cursor:default"><span class="nbj-fn"><span>Row limit</span><small>Rows fetched per query</small></span><select value=${limit} onChange=${(e) => setLimit(+e.target.value)} aria-label="Row limit">${[100, 500, 1000, 5000, 20000, 50000].map((n) => html`<option value=${n}>${num(n)}</option>`)}</select></label></div></aside>`}
    <section class="nbj-main"><div class="sq-tabs" role="tablist">${tabs.map((t) => html`<div key=${t.id} role="tab" aria-selected=${t.id === tab.id} class=${"sq-tab" + (t.id === tab.id ? " on" : "")} onClick=${() => setCur(t.id)}>${running[t.id] ? html`<i class="spin"></i>` : Ic("code", 12)}<span class="sq-ell">${t.name}</span><button class="ibtn sm" aria-label="Close tab" onClick=${(e) => { e.stopPropagation(); closeTab(t.id); }}>${Ic("x", 11)}</button></div>`)}
        <button class="ibtn" title="New query tab" aria-label="New query tab" onClick=${() => addTab()}>${Ic("plus", 14)}</button></div>
      <div class="sq-bar"><select class="sq-conn" value=${tab.conn} onChange=${(e) => patch({ conn: e.target.value })} aria-label="Connection"><option value="">Select a connection…</option>${conns.map((c) => html`<option value=${c.id}>${c.name}${c.readonly ? " (read-only)" : ""}</option>`)}</select>
        ${busy ? html`<button class="btn bad" onClick=${stop}>${Ic("stop", 12)} Stop</button>` : html`<button class="btn acc" disabled=${!conn} onClick=${() => run()}>${Ic("play", 13)} Run <kbd>⌘↵</kbd></button>`}
        <span class="mute small">${num(limit)} rows max</span><span class="grow"></span>
        <button class="btn sm" title="Add a :parameter to the query" onClick=${addParam}>${Ic("sliders", 13)} Parameter</button>
        <button class="btn sm" title="Save as a SQL file (parameters, connection) that a work node can run" onClick=${saveFile}>${Ic("save", 13)} Save${tab.file ? "" : " as file"}</button>
        <button class="btn sm" title="Rename this tab" aria-label="Rename" onClick=${guard(async () => { const n = prompt("Tab name", tab.name); if (n && n.trim()) patch({ name: n.trim().slice(0, 40) }); })}>${Ic("edit", 13)}</button></div>
      <${AskAi} conns=${conns} conn=${conn} tab=${tab} res=${res} post=${post} apply=${applyAi}/>
      ${names.length === 0 && html`<div class="sq-params dim"><span class="mute small">${Ic("sliders", 13)} Parameters</span><span class="mute small grow">Type <code>:name</code> in the query (e.g. <code>where id = :id</code>) and a value box appears here.</span><button class="btn sm" onClick=${addParam}>${Ic("plus", 12)} Add parameter</button></div>`}
      ${names.length > 0 && html`<div class="sq-params"><span class="mute small">${Ic("sliders", 13)} Parameters</span>${names.map((n) => html`<label class="sq-pm" key=${n}><code>:${n}</code><input value=${(tab.params || {})[n] ?? ""} placeholder="value" onInput=${(e) => setParam(n, e.target.value)} aria-label=${"Parameter " + n}/></label>`)}
        <span class="mute small grow">Bound safely by the driver. Save the file to keep these values as defaults; a work node can override them.</span></div>`}
      <${Editor} value=${tab.sql} onChange=${(v) => patch({ sql: v })} onRun=${run} conn=${conn} post=${post}/>
      <div class="sq-out">${busy && html`<div class="sq-load"><i class="spin"></i> Running…<button class="btn sm" onClick=${stop}>Stop</button></div>`}
        ${!busy && !res && html`<div class="sq-empty big">${Ic("table", 26)}<div>Results appear here</div><div class="mute small">Double-click a table in the tree to preview it.</div></div>`}
        ${!busy && res && res.ok && (res.columns.length ? html`<${Grid} res=${res} name=${(conn && conn.name) || "result"}/>` : html`<div class="sq-test ok" style="margin:14px">${Ic("check", 15)} Done${res.rowcount != null && res.rowcount >= 0 ? " · " + num(res.rowcount) + " row" + (res.rowcount === 1 ? "" : "s") + " affected" : ""} · ${res.ms} ms</div>`)}
        ${!busy && res && !res.ok && (res.missing ? html`<div class="sq-errbox"><${MissingHint} info=${res} env=${conn ? conn.env : ""} api=${api} post=${post} onDone=${() => run()}/></div>`
          : html`<div class="sq-errbox"><div class="sq-test bad">${Ic("alert", 15)} <span>${res.error}</span></div></div>`)}</div></section></div>`;
}

export function SqlStudioPage({ api, post, arg }) {
  const [view, setView] = useState(() => (arg && ["studio", "connections", "drivers"].includes(arg.split("/")[0]) ? arg.split("/")[0] : LS.get("view", "studio")));
  const [d, setD] = useState({ connections: [], envs: [], ui: "" }), [loaded, setLoaded] = useState(false), [edit, setEdit] = useState(null), [err, setErr] = useState(""), [init, setInit] = useState("");
  const reload = useCallback(() => api("sql/connections").then((x) => { setD(x); setLoaded(true); setErr(""); }).catch((e) => { setErr(e.message); setLoaded(true); }), []);
  useEffect(() => { reload(); }, []);
  const go = (v) => { setView(v); LS.set("view", v); };
  useEffect(() => { document.title = "SQL Studio"; }, []);
  const items = [{ id: "studio", label: "Studio", icon: "terminal" }, { id: "connections", label: "Connections", icon: "database" }, { id: "drivers", label: "Drivers", icon: "download" }];
  return html`<div class="sqs"><div class="sq-head"><span class="sq-logo">${Ic("database", 18)}</span><h1>SQL Studio</h1><span class="grow"></span>
      <${Seg} value=${view} onChange=${go} items=${items}/></div>
    ${err && html`<div class="sq-test bad" style="margin:12px 20px">${Ic("alert", 15)} ${err}</div>`}
    ${!loaded ? html`<div class="sq-pane"><p class="mute">Loading…</p></div>`
    : view === "studio" ? html`<${Studio} conns=${d.connections} post=${post} api=${api} envs=${d.envs} go=${(v) => { go(v); if (v === "connections") setEdit({}); }} init=${init}/>`
    : view === "connections" ? html`<${Connections} conns=${d.connections} envs=${d.envs} ui=${d.ui} api=${api} post=${post} reload=${reload} edit=${edit} setEdit=${setEdit} openStudio=${(id) => { setInit(id); go("studio"); }}/>`
    : html`<${Drivers} api=${api} post=${post} envs=${d.envs} ui=${d.ui} reloadEnvs=${reload} onUseUrl=${(url, env) => { go("connections"); setEdit({ driver: "other", url, env, name: "" }); }}/>`}</div>`;
}
