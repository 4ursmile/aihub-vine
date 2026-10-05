/* AI Hub web app. Preact + htm from CDN (no build step, no eval -> strict CSP). All data from /api/v1. */
const { html, render, useState, useEffect, useRef, useCallback } = window.htmPreact;

// ---------- api / state
const tok = () => { try { return localStorage.getItem("aihub_token"); } catch { return null; } };
async function api(path, opt = {}) {
  const h = { "Content-Type": "application/json" };
  if (tok()) h.Authorization = "Bearer " + tok();
  const r = await fetch("/api/v1" + path, { ...opt, headers: h, body: opt.body ? JSON.stringify(opt.body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : r.statusText || "Request failed");
  return j;
}
const subs = new Set();
const store = { me: null, route: location.hash.slice(1) || "/", toast: null };
const set = (p) => { Object.assign(store, p); subs.forEach((f) => f({})); };
const useStore = () => { const [, f] = useState({}); useEffect(() => { subs.add(f); return () => subs.delete(f); }, []); return store; };
addEventListener("hashchange", () => { set({ route: location.hash.slice(1) || "/" }); scrollTo(0, 0); });
let tt; const toast = (m, err) => { set({ toast: { m, err } }); clearTimeout(tt); tt = setTimeout(() => set({ toast: null }), 3200); };
async function loadMe() { try { set({ me: tok() ? await api("/auth/me") : null }); } catch { try { localStorage.removeItem("aihub_token"); } catch {} set({ me: null }); } }
const initials = (n) => (String(n || "?").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("") || "?").toUpperCase();
const hue = (n) => { let h = 0; for (const c of String(n)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
const shown = (p) => (p && (p.display_name || p.username)) || "";
const Avatar = ({ p, size = 32 }) => p && p.avatar_url ? html`<img class="av" src=${p.avatar_url} alt="" width=${size} height=${size} style=${`width:${size}px;height:${size}px`} loading="lazy" />`
  : html`<span class="av" aria-hidden="true" style=${`width:${size}px;height:${size}px;font-size:${Math.round(size * .4)}px;background:hsl(${hue(p && p.username)} 55% 45%)`}>${initials(shown(p))}</span>`;
const can = (perm) => !!(store.me && store.me.permissions.includes(perm));
function saveBlob(blob, name) { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); }
const nav = (p) => (location.hash = "#" + p);
const fmt = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n || 0));
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 3600 ? Math.max(1, s / 60 | 0) + "m ago" : s < 86400 ? (s / 3600 | 0) + "h ago" : s < 2592000 ? (s / 86400 | 0) + "d ago" : new Date(t * 1000).toLocaleDateString(); };
const copy = async (t) => { try { await navigator.clipboard.writeText(t); toast("Copied"); } catch { toast("Copy failed", 1); } };
function useLoad(fn, deps) {
  const [s, setS] = useState({ loading: true });
  useEffect(() => { let on = 1; setS({ loading: true }); fn().then((d) => on && setS({ d }), (e) => on && setS({ e: e.message })); return () => { on = 0; }; }, deps);
  return s;
}
const debounced = (v, ms = 250) => { const [x, setX] = useState(v); useEffect(() => { const t = setTimeout(() => setX(v), ms); return () => clearTimeout(t); }, [v]); return x; };

// ---------- small components
const Search = ({ value, onInput, ph = "Search skills, agents, MCP servers…" }) => html`<div class="search">
  <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="9" r="6"/><path d="M14 14l4 4"/></svg>
  <input class="field" type="search" value=${value} placeholder=${ph} aria-label="Search" onInput=${(e) => onInput(e.target.value)} /></div>`;
const Cmd = ({ text }) => html`<div class="cmd"><code>${text}</code><button class="btn sec sm" onClick=${() => copy(text)}>Copy</button></div>`;
const Empty = ({ t, d }) => html`<div class="empty"><h3>${t}</h3><p>${d}</p></div>`;
const Err = ({ e }) => html`<${Empty} t="Something went wrong" d=${e} />`;
const Skels = ({ n = 6 }) => html`<div class="grid">${Array.from({ length: n }, (_, i) => html`<div class="skel" key=${i}></div>`)}</div>`;
const Stars = ({ r }) => r ? html`<span>★ ${r}</span>` : null;
const TYPE = { skill: "blue", agent: "green", mcp: "orange", tool: "", setup: "red" };
const Pkg = ({ p }) => html`<a class="card pkg" href=${"#/package/" + p.name}>
  <div class="row"><h3>${p.name}</h3><span class="sp"></span>${p.visibility === "private" && html`<span class="badge orange" title="Only people you share it with can see this">🔒 private</span>`}<span class=${"badge " + (TYPE[p.type] || "")}>${p.type}</span></div>
  <p>${p.description || "No description"}</p>
  <div class="meta"><span>v${p.latest_version || "–"}</span><span>↓ ${fmt(p.downloads)}</span><${Stars} r=${p.rating && p.rating.avg} /></div></a>`;

// ---------- pages
function Home() {
  const [q, setQ] = useState("");
  const m = useLoad(() => api("/meta"), []);
  const top = useLoad(() => api("/packages?sort=downloads&per_page=6"), []);
  const o = (m.d && m.d.overview) || {};
  return html`<div>
    <section class="hero"><h1>Everything your AI<br/>needs. One hub.</h1>
      <p class="lead">Discover, install and manage skills, agents and MCP servers for Claude Code, Codex and OpenCode.</p>
      <form class="search" style="margin-top:32px;max-width:520px;margin-inline:auto" onSubmit=${(e) => { e.preventDefault(); nav("/browse?q=" + encodeURIComponent(q)); }}>
        <${Search} value=${q} onInput=${setQ} /></form></section>
    <div class="card stats"><div class="stat"><b>${fmt(o.packages)}</b><span>Packages</span></div><div class="stat"><b>${fmt(o.versions)}</b><span>Releases</span></div>
      <div class="stat"><b>${fmt(o.downloads)}</b><span>Downloads</span></div><div class="stat"><b>${fmt(o.users)}</b><span>People</span></div></div>
    <div class="gap row"><h2>Popular</h2><span class="sp"></span><a href="#/browse">See all</a></div>
    <div style="margin-top:20px">${top.loading ? html`<${Skels} n=3 />` : top.e ? html`<${Err} e=${top.e} />` :
      top.d.items.length ? html`<div class="grid">${top.d.items.map((p) => html`<${Pkg} p=${p} key=${p.name} />`)}</div>` :
      html`<${Empty} t="Nothing here yet" d="Publish the first package with aihub dev publish." />`}</div></div>`;
}

function Browse() {
  const qs = new URLSearchParams((store.route.split("?")[1]) || "");
  const [q, setQ] = useState(qs.get("q") || ""); const [type, setType] = useState(""); const [sort, setSort] = useState(""); const [page, setPage] = useState(1);
  const dq = debounced(q); useEffect(() => setPage(1), [dq, type, sort]);
  const f = useLoad(() => api("/facets"), []);
  const r = useLoad(() => api(`/packages?q=${encodeURIComponent(dq)}&type=${type}&sort=${sort}&page=${page}&per_page=18`), [dq, type, sort, page]);
  const types = ["", ...Object.keys((f.d && f.d.types) || {})];
  return html`<div class="stack"><h1>Browse</h1><${Search} value=${q} onInput=${setQ} />
    <div class="row"><div class="seg" role="tablist">${types.map((t) => html`<button key=${t} class=${type === t ? "on" : ""} onClick=${() => setType(t)}>${t || "All"}</button>`)}</div>
      <span class="sp"></span><select class="field" aria-label="Sort" value=${sort} onChange=${(e) => setSort(e.target.value)}>
      <option value="">${dq ? "Relevance" : "Recently updated"}</option><option value="downloads">Most downloaded</option><option value="name">Name</option><option value="created">Newest</option></select></div>
    ${r.loading ? html`<${Skels} />` : r.e ? html`<${Err} e=${r.e} />` : !r.d.items.length ? html`<${Empty} t="No results" d=${dq ? `Nothing matches “${dq}”.` : "No packages yet."} />` :
      html`<div class="grid">${r.d.items.map((p) => html`<${Pkg} p=${p} key=${p.name} />`)}</div>
      <div class="row" style="justify-content:center;margin-top:24px"><button class="btn sec sm" disabled=${page <= 1} onClick=${() => setPage(page - 1)}>Previous</button>
        <span class="mut sm">${r.d.total} results · page ${page}</span><button class="btn sec sm" disabled=${page * r.d.per_page >= r.d.total} onClick=${() => setPage(page + 1)}>Next</button></div>`}</div>`;
}

function Package({ name }) {
  const { me } = useStore(); const [tab, setTab] = useState("Overview"); const [tick, setTick] = useState(0);
  const p = useLoad(() => api("/packages/" + name), [name, tick]);
  if (p.loading) return html`<div class="skel" style="min-height:300px"></div>`;
  if (p.e) return html`<${Err} e=${p.e} />`;
  const d = p.d, req = d.requires || {};
  return html`<div class="stack">
    <div class="row"><h1>${d.name}</h1><span class=${"badge " + (TYPE[d.type] || "")}>${d.type}</span></div>
    <p class="lead">${d.description}</p>
    <div class="row mut sm">${d.visibility === "private" && html`<span class="badge orange">🔒 private</span>`}<span>v${d.latest_version}</span><span>·</span><span>↓ ${fmt(d.downloads)}</span><${Stars} r=${d.rating.avg} /><span>· updated ${ago(d.updated)}</span>
      ${d.tags.map((t) => html`<span class="badge" key=${t}>${t}</span>`)}</div>
    <${Cmd} text=${"aihub install " + d.name} />
    <div class="seg">${["Overview", "Install", "Versions", "Usage", "Reviews", ...(d.access === "admin" ? ["Sharing"] : [])].map((t) => html`<button key=${t} class=${tab === t ? "on" : ""} onClick=${() => setTab(t)}>${t}</button>`)}</div>
    ${tab === "Overview" && html`<${Readme} name=${name} />`}
    ${tab === "Install" && html`<div class="list">
      <div class="li"><div class="t"><b>Command</b><span class="mono">aihub install ${d.name}</span></div></div>
      <div class="li"><div class="t"><b>Required commands</b><span>${(req.commands || []).length ? (req.commands || []).map((c) => (c.name || c) + (c.hint ? ` — ${c.hint}` : "")).join(" · ") : "None"}</span></div></div>
      <div class="li"><div class="t"><b>Depends on</b><span>${(req.packages || []).length ? (req.packages || []).map((x) => html`<a href=${"#/package/" + x.split(/[<>=!~]/)[0]} style="margin-right:10px">${x}</a>`) : "No other packages"}</span></div></div>
      <div class="li"><div class="t"><b>Supported systems</b><span>${(req.os || []).length ? req.os.join(", ") : "All"}</span></div></div></div>`}
    ${tab === "Versions" && html`<div class="list tscroll"><table><thead><tr><th>Version</th><th>Released</th><th>Size</th><th>Downloads</th><th>SHA-256</th></tr></thead><tbody>
      ${d.versions.map((v) => html`<tr key=${v.version}><td>${v.version} ${v.yanked ? html`<span class="badge red">yanked</span>` : ""}</td><td>${ago(v.created)}</td><td>${(v.size / 1024).toFixed(1)} KB</td><td>${fmt(v.downloads)}</td><td class="mono">${v.sha256.slice(0, 12)}</td></tr>`)}</tbody></table></div>`}
    ${tab === "Usage" && html`<${Usage} name=${name} />`}
    ${tab === "Sharing" && html`<${Sharing} name=${name} pkg=${d} onChange=${() => setTick(tick + 1)} />`}
    ${tab === "Reviews" && html`<${Reviews} name=${name} me=${me} onDone=${() => setTick(tick + 1)} />`}</div>`;
}

function PrincipalPicker({ onPick, exclude = [], ph = "Add a person or group…" }) {
  const [q, setQ] = useState(""); const dq = debounced(q, 200); const [res, setRes] = useState(null);
  useEffect(() => { let on = 1; if (dq.trim().length < 1) { setRes(null); return; }
    api("/groups/lookup?q=" + encodeURIComponent(dq)).then((r) => on && setRes(r), () => on && setRes(null)); return () => { on = 0; }; }, [dq]);
  const has = (t, n) => exclude.includes(t + ":" + n);
  const pick = (t, n) => { setQ(""); setRes(null); onPick(t, n); };
  return html`<div style="position:relative;flex:1;min-width:220px"><input class="field" placeholder=${ph} value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Find a person or group" />
    ${res && html`<div class="list pop" role="listbox">${res.groups.filter((g) => !has("group", g)).map((g) => html`<button type="button" class="li pick" key=${"g" + g} onClick=${() => pick("group", g)}><span class="av" style="width:28px;height:28px;background:var(--fg-3);font-size:12px">👥</span><div class="t"><b>${g}</b><span>group</span></div></button>`)}
      ${res.users.filter((x) => !has("user", x.username)).map((x) => html`<button type="button" class="li pick" key=${"u" + x.username} onClick=${() => pick("user", x.username)}><${Avatar} p=${x} size=${28} /><div class="t"><b>${shown(x)}</b><span>${x.display_name ? "@" + x.username : "person"}</span></div></button>`)}
      ${!res.groups.length && !res.users.length && html`<div class="li mut sm">No matches</div>`}</div>`}</div>`;
}
function Sharing({ name, pkg, onChange }) {
  const [tick, setTick] = useState(0); const a = useLoad(() => api(`/packages/${name}/access`), [name, tick]); const [lvl, setLvl] = useState("view");
  const meta = useLoad(() => api("/meta"), []); const allowPrivate = !meta.d || meta.d.allow_private;
  const refresh = () => { setTick(tick + 1); onChange(); };
  const setVis = async (v) => { try { await api(`/packages/${name}`, { method: "PATCH", body: { visibility: v } }); toast(v === "private" ? "Now private" : "Now public"); refresh(); } catch (e) { toast(e.message, 1); } };
  const grant = async (type, n, access) => { try { await api(`/packages/${name}/access`, { method: "PUT", body: { type, name: n, access } }); toast("Access updated"); refresh(); } catch (e) { toast(e.message, 1); } };
  const revoke = async (type, n) => { try { await api(`/packages/${name}/access/${type}/${encodeURIComponent(n)}`, { method: "DELETE" }); refresh(); } catch (e) { toast(e.message, 1); } };
  const addAdmin = async (type, n) => { if (type !== "user") return toast("Repo admins must be people, not groups", 1); try { await api(`/packages/${name}/maintainers`, { method: "POST", body: { username: n } }); toast("Repo admin added"); refresh(); } catch (e) { toast(e.message, 1); } };
  const rmAdmin = async (n) => { try { await api(`/packages/${name}/maintainers/${encodeURIComponent(n)}`, { method: "DELETE" }); refresh(); } catch (e) { toast(e.message, 1); } };
  if (a.loading) return html`<div class="skel"></div>`; if (a.e) return html`<${Err} e=${a.e} />`;
  const D = a.d, priv = D.visibility === "private";
  const taken = D.access.map((x) => x.type + ":" + x.name);
  return html`<div class="stack">
    <div class="list"><div class="li"><div class="t"><b>Visibility</b><span>${priv ? "Only repo admins and people or groups you share it with can find and install it." : "Anyone who can browse this hub can find and install it."}</span></div>
      <div class="seg"><button class=${!priv ? "on" : ""} onClick=${() => priv && setVis("public")}>Public</button><button class=${priv ? "on" : ""} disabled=${!allowPrivate && !priv} onClick=${() => !priv && setVis("private")}>Private</button></div></div></div>
    ${!allowPrivate && html`<p class="xs mut3">Private repositories are turned off by the administrator.</p>`}
    <h3>Share with people and groups</h3>
    <div class="row"><${PrincipalPicker} exclude=${taken} onPick=${(t, n) => grant(t, n, lvl)} />
      <div class="seg" role="group" aria-label="Access level"><button class=${lvl === "view" ? "on" : ""} onClick=${() => setLvl("view")}>Can view</button><button class=${lvl === "develop" ? "on" : ""} onClick=${() => setLvl("develop")}>Can develop</button></div></div>
    <p class="xs mut3"><b>View</b>: find, read and install. <b>Develop</b>: also publish new versions. Repo admins can change visibility and sharing.</p>
    ${D.access.length ? html`<div class="list">${D.access.map((x) => html`<div class="li" key=${x.type + x.name}>
      ${x.type === "group" ? html`<span class="av" style="width:36px;height:36px;background:var(--fg-3)">👥</span>` : html`<${Avatar} p=${{ username: x.name, display_name: x.display_name, avatar_url: x.avatar_v ? `/api/v1/users/${x.name}/avatar?v=${x.avatar_v}` : null }} size=${36} />`}
      <div class="t"><b>${x.type === "user" && x.display_name ? x.display_name : x.name}</b><span>${x.type === "group" ? `group · ${x.members} ${x.members === 1 ? "member" : "members"}` : "person"}</span></div>
      <select class="field" style="min-height:32px;font-size:14px" aria-label="Access" onChange=${(e) => grant(x.type, x.name, e.target.value)}><option value="view" selected=${x.access === "view"}>Can view</option><option value="develop" selected=${x.access === "develop"}>Can develop</option></select>
      <button class="btn danger sm" onClick=${() => revoke(x.type, x.name)}>Remove</button></div>`)}</div>` : html`<p class="mut sm">${priv ? "Not shared with anyone yet." : "Public repositories are visible to everyone; share only to grant develop access."}</p>`}
    <h3 class="gap">Repo admins</h3><p class="xs mut3">Full control of this repository, including sharing. The creator is a repo admin by default.</p>
    <div class="row"><${PrincipalPicker} ph="Add a repo admin…" exclude=${D.maintainers.map((m) => "user:" + m.username)} onPick=${addAdmin} /></div>
    <div class="list">${D.maintainers.map((m) => html`<div class="li" key=${m.username}><${Avatar} p=${m} size=${36} /><div class="t"><b>${shown(m)}</b><span>${m.role}</span></div>
      <button class="btn danger sm" disabled=${D.maintainers.length <= 1} onClick=${() => rmAdmin(m.username)}>Remove</button></div>`)}</div></div>`;
}

function Readme({ name }) {
  const r = useLoad(() => api(`/packages/${name}/readme`), [name]);
  if (r.loading) return html`<div class="skel"></div>`;
  return html`<div class="card md" dangerouslySetInnerHTML=${{ __html: (r.d && r.d.html) || "<p class='mut'>No README.</p>" }}></div>`;
}
function Usage({ name }) {
  const s = useLoad(() => api(`/packages/${name}/stats`), [name]);
  if (s.loading) return html`<div class="skel"></div>`; if (s.e) return html`<${Err} e=${s.e} />`;
  const k = s.d.by_kind, mx = Math.max(1, ...Object.values(k));
  return html`<div class="stack"><div class="card stats"><div class="stat"><b>${fmt(s.d.active_users)}</b><span>Active users · ${s.d.days}d</span></div>
    ${["install", "use", "update", "error"].map((x) => html`<div class="stat" key=${x}><b>${fmt(k[x] || 0)}</b><span>${x}s</span></div>`)}</div>
    ${!Object.keys(k).length && html`<p class="mut">No activity recorded yet.</p>`}</div>`;
}
function Reviews({ name, me, onDone }) {
  const [tick, setTick] = useState(0); const r = useLoad(() => api(`/packages/${name}/reviews`), [name, tick]);
  const [rating, setRating] = useState(5); const [body, setBody] = useState("");
  const post = async () => { try { await api(`/packages/${name}/reviews`, { method: "POST", body: { rating, body } }); toast("Thanks for your review"); setBody(""); setTick(tick + 1); onDone(); } catch (e) { toast(e.message, 1); } };
  return html`<div class="stack">${me ? html`<div class="card stack"><div class="row"><b>Your rating</b><div class="seg">${[1, 2, 3, 4, 5].map((n) => html`<button key=${n} class=${rating === n ? "on" : ""} onClick=${() => setRating(n)}>${"★".repeat(n)}</button>`)}</div></div>
      <textarea class="field" rows="3" style="padding:12px;resize:vertical" placeholder="Share your experience (optional)" value=${body} onInput=${(e) => setBody(e.target.value)}></textarea>
      <button class="btn" onClick=${post}>Submit review</button></div>` : html`<p class="mut"><a href="#/login">Sign in</a> to leave a review.</p>`}
    ${r.d && (r.d.reviews.length ? html`<div class="list">${r.d.reviews.map((x) => html`<div class="li" key=${x.username}><${Avatar} p=${x} size=${36} /><div class="t"><b>${shown(x)} <span class="mut3">${"★".repeat(x.rating)}</span></b>${x.title && html`<span class="xs mut3" style="display:block">${x.title}</span>`}<span>${x.body}</span></div><span class="xs mut3">${ago(x.created)}</span></div>`)}</div>` : html`<p class="mut">No reviews yet.</p>`)}</div>`;
}

function Rankings() {
  const [tab, setTab] = useState("packages"); const r = useLoad(() => api("/rankings/" + tab), [tab]);
  const label = { packages: "Top packages", developers: "Top developers", users: "Most active users" };
  const items = (r.d && r.d.items) || [], mx = Math.max(1, ...items.map((i) => i.count));
  return html`<div class="stack"><h1>Rankings</h1><p class="lead">Based on the last 30 days of real usage.</p>
    <div class="seg">${Object.keys(label).map((t) => html`<button key=${t} class=${tab === t ? "on" : ""} onClick=${() => setTab(t)}>${label[t]}</button>`)}</div>
    ${r.loading ? html`<div class="skel"></div>` : !items.length ? html`<${Empty} t="No data yet" d="Rankings appear once usage events arrive." />` :
      html`<div class="list">${items.map((i, n) => html`<div class="li" key=${i.name}><span class=${"rank " + (n < 3 ? "top" : "")}>${n + 1}</span>
        <div class="t"><b>${tab === "packages" ? html`<a href=${"#/package/" + i.name}>${i.name}</a>` : i.name}</b><div class="bar"><i style=${"width:" + (i.count / mx * 100) + "%"}></i></div></div><span class="n">${fmt(i.count)}</span></div>`)}</div>`}</div>`;
}

function Start() {
  const o = location.origin;
  const step = (n, t, d, c) => html`<div class="card stack"><div class="row"><span class="rank top">${n}</span><h3>${t}</h3></div><p class="mut">${d}</p>${c && html`<${Cmd} text=${c} />`}</div>`;
  return html`<div class="stack"><h1>Get started</h1><p class="lead">Up and running in under a minute. Python 3.9 or later.</p>
    ${step(1, "Install the CLI", "One command installs aihub and sets up usage reporting for Claude Code and OpenCode.", `curl -fsSL ${o}/install.sh | sh`)}
    ${step(2, "Sign in", "Create an account on this site, then log in from your terminal.", "aihub login")}
    ${step(3, "Install something", "You'll be asked which tools to connect it to. Everything is reversible.", "aihub install <package>")}
    ${step(4, "Publish your own", "Scaffold a manifest, then publish. Versions are immutable.", "aihub dev init && aihub dev publish")}</div>`;
}

function Auth({ mode }) {
  const [u, setU] = useState(""); const [p, setP] = useState(""); const [e, setE] = useState(""); const [busy, setBusy] = useState(false); const reg = mode === "register";
  const go = async (ev) => { ev.preventDefault(); setBusy(true); setE("");
    try { if (reg) { const r = await api("/auth/register", { method: "POST", body: { username: u, password: p } }); if (r.status === "pending") { toast("Account created — awaiting admin approval"); return nav("/login"); } }
      const r = await api("/auth/login", { method: "POST", body: { username: u, password: p } }); localStorage.setItem("aihub_token", r.token); await loadMe(); nav("/"); }
    catch (x) { setE(x.message); } finally { setBusy(false); } };
  return html`<form class="form" onSubmit=${go}><h2>${reg ? "Create your account" : "Sign in"}</h2>
    <input class="field" placeholder="Username" autocomplete="username" value=${u} onInput=${(x) => setU(x.target.value)} required />
    <input class="field" type="password" placeholder="Password" autocomplete=${reg ? "new-password" : "current-password"} value=${p} onInput=${(x) => setP(x.target.value)} required minlength="6" />
    <button class="btn" style="width:100%;margin-top:16px" disabled=${busy}>${busy ? "…" : reg ? "Create account" : "Sign in"}</button><div class="msg" role="alert">${e}</div>
    <p class="center mut sm" style="text-align:center;margin-top:8px">${reg ? html`Have an account? <a href="#/login">Sign in</a>` : html`New here? <a href="#/register">Create an account</a>`}</p></form>`;
}

async function toAvatarBlob(file) {
  if (!/^image\/(png|jpeg|webp)$/.test(file.type)) throw new Error("Choose a PNG, JPEG or WebP image");
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => no(new Error("That image could not be read")); i.src = url; });
    const side = Math.min(img.width, img.height), S = 256, cv = document.createElement("canvas"); cv.width = cv.height = S;
    cv.getContext("2d").drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, S, S);   // centre-crop square
    return await new Promise((ok, no) => cv.toBlob((b) => (b ? ok(b) : no(new Error("Could not process image"))), "image/webp", 0.88));
  } finally { URL.revokeObjectURL(url); }
}
function ProfileCard({ me }) {
  const [dn, setDn] = useState(me.display_name || ""); const [ti, setTi] = useState(me.title || ""); const [busy, setBusy] = useState(false); const fileRef = useRef(null);
  const dirty = dn.trim() !== (me.display_name || "") || ti.trim() !== (me.title || "");
  const save = async (e) => { e.preventDefault(); setBusy(true); try { await api("/auth/me", { method: "PATCH", body: { display_name: dn, title: ti } }); await loadMe(); toast("Profile saved"); } catch (x) { toast(x.message, 1); } finally { setBusy(false); } };
  const pick = async (e) => { const f = e.target.files[0]; e.target.value = ""; if (!f) return; setBusy(true);
    try { const blob = await toAvatarBlob(f); const r = await fetch("/api/v1/auth/avatar", { method: "POST", headers: { Authorization: "Bearer " + tok() }, body: blob });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Upload failed"); await loadMe(); toast("Photo updated"); } catch (x) { toast(x.message, 1); } finally { setBusy(false); } };
  const remove = async () => { try { await api("/auth/avatar", { method: "DELETE" }); await loadMe(); toast("Photo removed"); } catch (x) { toast(x.message, 1); } };
  return html`<form class="card stack" onSubmit=${save}><div class="row" style="gap:20px"><${Avatar} p=${me} size=${84} />
      <div class="stack" style="flex:1;min-width:200px"><div class="row"><button type="button" class="btn sec sm" disabled=${busy} onClick=${() => fileRef.current.click()}>${me.avatar_url ? "Change photo" : "Upload photo"}</button>
        ${me.avatar_url && html`<button type="button" class="btn danger sm" onClick=${remove}>Remove</button>`}</div>
        <span class="xs mut3">PNG, JPEG or WebP. Resized to 256×256 in your browser.</span></div>
      <input ref=${fileRef} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange=${pick} aria-label="Profile photo" /></div>
    <label class="stack sm"><span class="mut">Display name</span><input class="field" maxlength="60" placeholder=${me.username} value=${dn} onInput=${(e) => setDn(e.target.value)} /></label>
    <label class="stack sm"><span class="mut">Job title</span><input class="field" maxlength="80" placeholder="e.g. Staff Engineer" value=${ti} onInput=${(e) => setTi(e.target.value)} /></label>
    <div class="row"><span class="xs mut3">Username <b>${me.username}</b> can’t be changed.</span><span class="sp"></span><button class="btn" disabled=${!dirty || busy}>Save changes</button></div></form>`;
}
function PasswordCard() {
  const [f, setF] = useState({ old: "", nw: "", again: "" }); const [busy, setBusy] = useState(false); const [err, setErr] = useState("");
  const set1 = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const strength = f.nw.length >= 12 ? 3 : f.nw.length >= 8 ? 2 : f.nw.length >= 6 ? 1 : 0;
  const submit = async (e) => { e.preventDefault(); setErr("");
    if (f.nw !== f.again) return setErr("The new passwords don’t match.");
    setBusy(true); try { await api("/auth/password", { method: "POST", body: { old: f.old, new: f.nw } }); setF({ old: "", nw: "", again: "" }); toast("Password changed — other devices were signed out"); }
    catch (x) { setErr(x.message); } finally { setBusy(false); } };
  return html`<form class="card stack" onSubmit=${submit}>
    <input type="text" autocomplete="username" value=${store.me && store.me.username} hidden readonly />
    <label class="stack sm"><span class="mut">Current password</span><input class="field" type="password" autocomplete="current-password" required value=${f.old} onInput=${set1("old")} /></label>
    <label class="stack sm"><span class="mut">New password</span><input class="field" type="password" autocomplete="new-password" required minlength="6" value=${f.nw} onInput=${set1("nw")} />
      <span class="meter" aria-hidden="true"><i class=${"s" + strength} style=${`width:${strength * 33.4}%`}></i></span></label>
    <label class="stack sm"><span class="mut">Confirm new password</span><input class="field" type="password" autocomplete="new-password" required value=${f.again} onInput=${set1("again")} /></label>
    <div class="msg" role="alert" style="text-align:left;margin:0">${err}</div>
    <div class="row"><span class="xs mut3">At least 6 characters. Changing it signs out your other devices.</span><span class="sp"></span><button class="btn" disabled=${busy || !f.old || !f.nw}>Change password</button></div></form>`;
}
function Account() {
  const { me } = useStore(); const [tick, setTick] = useState(0); const [name, setName] = useState(""); const [fresh, setFresh] = useState("");
  const t = useLoad(() => me ? api("/auth/tokens") : Promise.resolve({ tokens: [] }), [me && me.username, tick]);
  const mine = useLoad(() => me ? api("/me/packages") : Promise.resolve({ packages: [] }), [me && me.username]);
  if (!me) return html`<${Empty} t="Sign in required" d="Please sign in to view your account." />`;
  const mk = async () => { try { setFresh((await api("/auth/tokens", { method: "POST", body: { name } })).token); setName(""); setTick(tick + 1); } catch (e) { toast(e.message, 1); } };
  const rm = async (id) => { await api("/auth/tokens/" + id, { method: "DELETE" }); setTick(tick + 1); };
  return html`<div class="stack" style="max-width:640px;margin:0 auto"><div class="row" style="gap:16px"><${Avatar} p=${me} size=${56} /><div><h1 style="font-size:32px">${shown(me)}</h1>
      <div class="row sm mut" style="gap:8px">${me.title && html`<span>${me.title}</span><span>·</span>`}<span>@${me.username}</span><span class="badge blue">${me.role}</span></div></div></div>
    <h2 class="gap">Profile</h2><${ProfileCard} me=${me} key=${me.username + (me.avatar_url || "")} />
    <h2 class="gap">Password</h2><${PasswordCard} />
    <h2 class="gap">API tokens</h2><p class="mut">For CI or scripts. <code>aihub login</code> creates one automatically.</p>
    <div class="row"><input class="field" style="flex:1;min-width:200px" placeholder="Token name" value=${name} onInput=${(e) => setName(e.target.value)} /><button class="btn" onClick=${mk}>Create token</button></div>
    ${fresh && html`<div class="card stack"><b>Copy your token now — it won’t be shown again.</b><${Cmd} text=${fresh} /></div>`}
    ${t.d && t.d.tokens.length ? html`<div class="list">${t.d.tokens.map((k) => html`<div class="li" key=${k.id}><div class="t"><b>${k.name || k.kind}</b><span>${k.kind} · created ${ago(k.created)}</span></div><button class="btn danger sm" onClick=${() => rm(k.id)}>Revoke</button></div>`)}</div>` : null}
    <h2 class="gap">My packages</h2>${mine.d && mine.d.packages.length ? html`<div class="list">${mine.d.packages.map((x) => html`<a class="li" href=${"#/package/" + x.name} key=${x.name}><div class="t"><b>${x.name}</b><span>${x.type} · v${x.latest_version}</span></div></a>`)}</div>` : html`<p class="mut">You haven’t published anything yet.</p>`}</div>`;
}

function Admin() {
  const { me } = useStore(); const [tab, setTab] = useState("People"); const [tick, setTick] = useState(0); const bump = () => setTick((x) => x + 1);
  if (!can("admin")) return html`<${Empty} t="Admins only" d="You don’t have access to this page." />`;
  return html`<div class="stack"><h1>Admin</h1><div class="seg">${["People", "Groups", "Roles", "Settings", "Audit"].map((t) => html`<button key=${t} class=${tab === t ? "on" : ""} onClick=${() => setTab(t)}>${t}</button>`)}</div>
    ${tab === "People" && html`<${People} me=${me} tick=${tick} bump=${bump} />`}${tab === "Groups" && html`<${Groups} tick=${tick} bump=${bump} />`}${tab === "Roles" && html`<${Roles} tick=${tick} bump=${bump} />`}
    ${tab === "Settings" && html`<${AdminSettings} />`}${tab === "Audit" && html`<${Audit} tick=${tick} />`}</div>`;
}
const SC = { active: "green", pending: "orange", disabled: "red" };
function People({ me, tick, bump }) {
  const d = useLoad(async () => ({ users: (await api("/admin/users")).users, roles: Object.keys((await api("/admin/roles")).roles) }), [tick]);
  const [q, setQ] = useState(""); const [batch, setBatch] = useState(false);
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const st = async (u, status) => { try { await api(`/admin/users/${u}/status`, { method: "POST", body: { status } }); bump(); } catch (e) { toast(e.message, 1); } };
  const role = async (u, r) => { try { await api(`/admin/users/${u}/role`, { method: "POST", body: { role: r } }); toast("Role updated"); bump(); } catch (e) { toast(e.message, 1); } };
  const users = d.d.users.filter((u) => u.username.toLowerCase().includes(q.toLowerCase()));
  return html`<div class="stack"><div class="row"><div style="flex:1;min-width:200px"><${Search} value=${q} onInput=${setQ} ph="Filter people" /></div><button class="btn" onClick=${() => setBatch(!batch)}>${batch ? "Close" : "Create accounts in bulk"}</button></div>
    ${batch && html`<${BatchImport} roles=${d.d.roles} done=${bump} />`}
    <div class="list">${users.map((u) => html`<div class="li" key=${u.username}><${Avatar} p=${u} size=${36} /><div class="t"><b>${shown(u)}</b><span>${u.display_name ? u.username + " · " : ""}${u.title ? u.title + " · " : ""}${u.created ? "joined " + ago(u.created) : ""}</span></div>
      <select class="field" style="min-height:32px;font-size:14px" aria-label="Role" disabled=${u.username === me.username} onChange=${(e) => role(u.username, e.target.value)}>${d.d.roles.map((r) => html`<option value=${r} key=${r} selected=${r === u.role}>${r}</option>`)}</select>
      <span class=${"badge " + SC[u.status]}>${u.status}</span>${u.status !== "active" ? html`<button class="btn sec sm" onClick=${() => st(u.username, "active")}>Approve</button>` : u.username !== me.username ? html`<button class="btn danger sm" onClick=${() => st(u.username, "disabled")}>Disable</button>` : null}</div>`)}
      ${!users.length && html`<div class="li mut">No matches.</div>`}</div></div>`;
}
function BatchImport({ roles, done }) {
  const [file, setFile] = useState(null); const [role, setRole] = useState("user"); const [busy, setBusy] = useState(false); const [res, setRes] = useState(null);
  const tpl = () => saveBlob(new Blob(["username,display_name,title,role\nalice,Alice Nguyen,Data Analyst,user\nbob,Bob Martin,Engineering Manager,publisher\ncarol,Carol Diaz,,\n"], { type: "text/csv" }), "accounts-template.csv");
  const go = async () => { if (!file) return; setBusy(true); setRes(null);
    try { const r = await fetch(`/api/v1/admin/users/batch?role=${encodeURIComponent(role)}`, { method: "POST", headers: { Authorization: "Bearer " + tok(), "X-Aihub-Filename": file.name }, body: await file.arrayBuffer() });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Upload failed");
      const blob = await r.blob(); saveBlob(blob, "new-accounts.csv"); setRes({ created: +r.headers.get("X-Created"), skipped: +r.headers.get("X-Skipped") }); done();
    } catch (e) { toast(e.message, 1); } finally { setBusy(false); } };
  return html`<div class="card stack"><h3>Create accounts in bulk</h3>
    <p class="mut sm">Upload a <b>.csv</b> or <b>.xlsx</b> with a header row. Columns (any order): <code>username</code> (required), <code>display_name</code>, <code>title</code>, <code>role</code>. Without a header row it reads <code>username, role</code>. The default role below is used when the role cell is empty. Passwords are generated for you and returned as a downloadable sheet — <b>this is the only time they are shown</b>. Up to 300 accounts per upload.</p>
    <div class="row"><input type="file" class="field" style="padding:9px" accept=".csv,.tsv,.txt,.xlsx" onChange=${(e) => setFile(e.target.files[0] || null)} aria-label="Spreadsheet file" />
      <label class="sm mut">Default role <select class="field" value=${role} onChange=${(e) => setRole(e.target.value)}>${roles.map((r) => html`<option key=${r}>${r}</option>`)}</select></label>
      <button class="btn sec sm" onClick=${tpl}>Download template</button><span class="sp"></span><button class="btn" disabled=${!file || busy} onClick=${go}>${busy ? "Creating…" : "Create & download passwords"}</button></div>
    ${res && html`<div class="row"><span class="badge green">${res.created} created</span>${res.skipped ? html`<span class="badge orange">${res.skipped} skipped — see the “status” column in the sheet</span>` : null}<span class="xs mut3">Keep the sheet safe and delete it after sharing passwords.</span></div>`}</div>`;
}
function Roles({ tick, bump }) {
  const d = useLoad(() => api("/admin/roles"), [tick]); const [nn, setNn] = useState(""); const [nd, setNd] = useState("");
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const roles = d.d.detail, perms = d.d.permissions;
  const toggle = async (r, p) => { const cur = new Set(r.permissions); cur.has(p) ? cur.delete(p) : cur.add(p);
    try { await api("/admin/roles/" + r.name, { method: "PUT", body: { permissions: [...cur] } }); bump(); } catch (e) { toast(e.message, 1); } };
  const create = async () => { try { await api("/admin/roles", { method: "POST", body: { name: nn, description: nd, permissions: [] } }); setNn(""); setNd(""); toast("Role created — now grant it permissions"); bump(); } catch (e) { toast(e.message, 1); } };
  const del = async (r) => { try { await api("/admin/roles/" + r.name, { method: "DELETE" }); toast("Role deleted"); bump(); } catch (e) { toast(e.message, 1); } };
  return html`<div class="stack"><p class="mut">Tick what each role may do. Changes apply immediately. The <code>admin</code> role always keeps full access.</p>
    <div class="list tscroll"><table class="matrix"><thead><tr><th>Permission</th>${roles.map((r) => html`<th key=${r.name} style="text-align:center"><div>${r.name}</div><div class="xs mut3" style="font-weight:400">${r.users} ${r.users === 1 ? "person" : "people"}</div></th>`)}</tr></thead><tbody>
      ${perms.map((p) => html`<tr key=${p.key}><td><b style="font-weight:500">${p.label}</b><div class="xs mut3 mono">${p.key}</div></td>${roles.map((r) => html`<td key=${r.name} style="text-align:center">
        <input type="checkbox" class="chk" aria-label=${`${r.name}: ${p.label}`} checked=${r.permissions.includes(p.key)} disabled=${r.name === "admin"} onChange=${() => toggle(r, p.key)} /></td>`)}</tr>`)}
      <tr><td class="mut sm">Remove role</td>${roles.map((r) => html`<td key=${r.name} style="text-align:center">${r.builtin ? html`<span class="xs mut3">built-in</span>` : html`<button class="btn danger sm" onClick=${() => del(r)}>Delete</button>`}</td>`)}</tr></tbody></table></div>
    <div class="card stack"><h3>New role</h3><div class="row"><input class="field" style="flex:1;min-width:160px" placeholder="Name, e.g. analyst" value=${nn} onInput=${(e) => setNn(e.target.value)} />
      <input class="field" style="flex:2;min-width:200px" placeholder="Description (optional)" value=${nd} onInput=${(e) => setNd(e.target.value)} /><button class="btn" disabled=${!nn} onClick=${create}>Create role</button></div>
      <p class="xs mut3">Lowercase letters, digits, - or _. Create it, then tick its permissions in the table above.</p></div></div>`;
}
function AdminSettings() {
  const [tick, setTick] = useState(0); const d = useLoad(() => api("/admin/settings"), [tick]); const [busy, setBusy] = useState("");
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const set1 = async (k, v) => { setBusy(k); try { await api("/admin/settings", { method: "PUT", body: { [k]: v } }); toast("Saved"); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const sections = [["Access", ["signup", "public_browse", "public_install"]], ["Repositories", ["allow_private", "default_visibility"]], ["Groups", ["allow_group_creation"]]];
  const by = Object.fromEntries(d.d.schema.map((x) => [x.key, x]));
  const warn = d.d.values.public_install === "1";
  return html`<div class="stack">${sections.map(([title, keys]) => html`<div class="stack" key=${title}><h3>${title}</h3><div class="list">${keys.filter((k) => by[k]).map((k) => { const x = by[k], v = d.d.values[k], onoff = x.options.length === 2 && x.options[0] === "0";
      return html`<div class="li" key=${k}><div class="t"><b>${x.label}</b><span>${x.help}</span></div>
        ${onoff ? html`<label class="switch"><input type="checkbox" role="switch" checked=${v === "1"} disabled=${busy === k} onChange=${(e) => set1(k, e.target.checked ? "1" : "0")} aria-label=${x.label} /><i></i></label>`
        : html`<div class="seg">${x.options.map((o) => html`<button key=${o} class=${v === o ? "on" : ""} disabled=${busy === k} onClick=${() => v !== o && set1(k, o)}>${o}</button>`)}</div>`}</div>`; })}</div></div>`)}
    ${warn && html`<div class="card" style="border:1px solid var(--orange)"><b>Public install is on.</b> <span class="mut">Anyone who can reach this server can download <u>public</u> packages without signing in. Private packages always require access.</span></div>`}</div>`;
}
function Groups({ tick, bump }) {
  const d = useLoad(() => api("/groups"), [tick]); const [sel, setSel] = useState(null); const [nn, setNn] = useState(""); const [nd, setNd] = useState("");
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const create = async () => { try { await api("/groups", { method: "POST", body: { name: nn, description: nd } }); setSel(nn.toLowerCase()); setNn(""); setNd(""); toast("Group created"); bump(); } catch (e) { toast(e.message, 1); } };
  return html`<div class="stack"><p class="mut">Groups let you share repositories with many people at once. Add someone to a group and they gain every access the group has.</p>
    <div class="two" style="align-items:start"><div class="stack"><div class="list">${d.d.groups.length ? d.d.groups.map((g) => html`<button class="li pick" key=${g.name} onClick=${() => setSel(g.name)} style=${sel === g.name ? "background:var(--fill)" : ""}><span class="av" style="width:36px;height:36px;background:var(--fg-3)">👥</span><div class="t"><b>${g.name}</b><span>${g.members} ${g.members === 1 ? "member" : "members"} · ${g.packages} shared ${g.packages === 1 ? "repo" : "repos"}</span></div></button>`) : html`<div class="li mut">No groups yet.</div>`}</div>
      <div class="card stack"><h3>New group</h3><input class="field" placeholder="name, e.g. data-team" value=${nn} onInput=${(e) => setNn(e.target.value)} /><input class="field" placeholder="Description (optional)" value=${nd} onInput=${(e) => setNd(e.target.value)} />
        <button class="btn" disabled=${!nn} onClick=${create}>Create group</button></div></div>
      ${sel ? html`<${GroupDetail} name=${sel} key=${sel} bump=${bump} onGone=${() => setSel(null)} />` : html`<${Empty} t="Select a group" d="Pick a group to manage its members." />`}</div></div>`;
}
function GroupDetail({ name, bump, onGone }) {
  const [tick, setTick] = useState(0); const g = useLoad(() => api("/groups/" + name), [name, tick]); const [confirm, setConfirm] = useState(false);
  const [paste, setPaste] = useState("");
  if (g.loading) return html`<div class="skel"></div>`; if (g.e) return html`<${Err} e=${g.e} />`;
  const re = () => { setTick(tick + 1); bump(); };
  const add = async (names) => { try { const r = await api(`/groups/${name}/members`, { method: "POST", body: { usernames: names } }); toast(r.missing.length ? `Added; not found: ${r.missing.join(", ")}` : "Members added", r.missing.length ? 1 : 0); setPaste(""); re(); } catch (e) { toast(e.message, 1); } };
  const rm = async (u) => { try { await api(`/groups/${name}/members/${encodeURIComponent(u)}`, { method: "DELETE" }); re(); } catch (e) { toast(e.message, 1); } };
  const del = async () => { try { await api("/groups/" + name, { method: "DELETE" }); toast("Group deleted"); bump(); onGone(); } catch (e) { toast(e.message, 1); } };
  return html`<div class="stack"><div class="row"><h2>${name}</h2><span class="sp"></span>${confirm ? html`<span class="sm mut">Delete this group and its sharing?</span><button class="btn danger sm" onClick=${del}>Yes, delete</button><button class="btn sec sm" onClick=${() => setConfirm(false)}>Cancel</button>` : html`<button class="btn danger sm" onClick=${() => setConfirm(true)}>Delete</button>`}</div>
    ${g.d.description && html`<p class="mut">${g.d.description}</p>`}
    <div class="row"><${PrincipalPicker} ph="Add a person…" exclude=${g.d.members.map((m) => "user:" + m.username).concat(["group:*"])} onPick=${(t, n) => t === "user" ? add([n]) : toast("Pick a person", 1)} /></div>
    <details><summary class="sm mut" style="cursor:pointer">Add many at once</summary><div class="stack" style="margin-top:8px"><textarea class="field" rows="3" style="padding:12px" placeholder="Usernames separated by commas, spaces or new lines" value=${paste} onInput=${(e) => setPaste(e.target.value)}></textarea>
      <button class="btn sec sm" style="width:fit-content" disabled=${!paste.trim()} onClick=${() => add(paste.split(/[\s,;]+/).filter(Boolean))}>Add usernames</button></div></details>
    <div class="list">${g.d.members.length ? g.d.members.map((m) => html`<div class="li" key=${m.username}><${Avatar} p=${m} size=${36} /><div class="t"><b>${shown(m)}</b><span>${m.title || "@" + m.username}</span></div><button class="btn danger sm" onClick=${() => rm(m.username)}>Remove</button></div>`) : html`<div class="li mut">No members yet.</div>`}</div></div>`;
}
function Audit({ tick }) {
  const d = useLoad(() => api("/admin/audit"), [tick]); if (d.loading) return html`<div class="skel"></div>`;
  return html`<div class="list tscroll"><table><tbody>${d.d.audit.map((a) => html`<tr key=${a.id}><td class="mut sm">${ago(a.ts)}</td><td>${a.actor}</td><td><code>${a.action}</code></td><td class="mut">${a.target} ${a.detail}</td></tr>`)}</tbody></table></div>`;
}

function Docs({ slug }) {
  const list = useLoad(() => api("/docs"), []); const cur = slug || (list.d && list.d.docs[0] && list.d.docs[0].slug);
  const d = useLoad(() => cur ? api("/docs/" + cur) : Promise.resolve(null), [cur]);
  return html`<div class="stack"><h1>Docs</h1><div class="row" style="align-items:flex-start;gap:28px">
    <nav class="list" style="min-width:200px;flex:0 0 220px" aria-label="Docs">${(list.d ? list.d.docs : []).map((x) => html`<a class="li" key=${x.slug} href=${"#/docs/" + x.slug} style=${x.slug === cur ? "font-weight:600" : ""}>${x.title}</a>`)}</nav>
    <div class="card md" style="flex:1;min-width:280px" dangerouslySetInnerHTML=${{ __html: (d.d && d.d.html) || (d.loading ? "" : "<p class='mut'>No documentation found.</p>") }}></div></div></div>`;
}


// ---------- dashboard (charts are plain SVG: no library, theme-aware)
const KC = { use: "var(--blue)", install: "var(--green)", update: "var(--orange)", error: "var(--red)", uninstall: "var(--fg-3)" };
const delta = (a, b) => { if (!b) return a ? { t: "new", c: "green" } : null; const p = Math.round((a - b) / b * 100); return { t: (p >= 0 ? "▲ " : "▼ ") + Math.abs(p) + "%", c: p >= 0 ? "green" : "red" }; };
const Kpi = ({ label, value, prev, invert }) => { const d = delta(value, prev);
  return html`<div class="card stat"><span>${label}</span><b>${fmt(value)}</b>${d ? html`<span class=${"badge " + (invert ? (d.c === "green" ? "red" : "green") : d.c)}>${d.t} vs prior</span>` : html`<span class="xs mut3">no prior data</span>`}</div>`; };

function LineChart({ daily, series }) {
  const W = 760, H = 220, P = { l: 34, r: 10, t: 10, b: 24 }, n = daily.length;
  const mx = Math.max(1, ...daily.map((d) => Math.max(...series.map((k) => d[k] || 0))));
  const nice = Math.ceil(mx / 4) * 4 || 4, x = (i) => P.l + (n < 2 ? 0 : i * (W - P.l - P.r) / (n - 1)), y = (v) => P.t + (H - P.t - P.b) * (1 - v / nice);
  const [hv, setHv] = useState(null);
  const move = (e) => { const r = e.currentTarget.getBoundingClientRect(); const px = (e.clientX - r.left) / r.width * W; setHv(Math.max(0, Math.min(n - 1, Math.round((px - P.l) / ((W - P.l - P.r) / Math.max(1, n - 1)))))); };
  const step = Math.ceil(n / 6);
  return html`<div style="position:relative"><svg viewBox=${`0 0 ${W} ${H}`} style="width:100%;height:auto;display:block" role="img" aria-label="Daily activity chart" onMouseMove=${move} onMouseLeave=${() => setHv(null)}>
    ${[0, 1, 2, 3, 4].map((i) => html`<g key=${i}><line x1=${P.l} x2=${W - P.r} y1=${y(nice * i / 4)} y2=${y(nice * i / 4)} stroke="var(--sep)" stroke-width="1"/><text x=${P.l - 6} y=${y(nice * i / 4) + 4} text-anchor="end" font-size="11" fill="var(--fg-3)">${fmt(Math.round(nice * i / 4))}</text></g>`)}
    ${daily.map((d, i) => i % step === 0 ? html`<text key=${i} x=${x(i)} y=${H - 6} text-anchor="middle" font-size="11" fill="var(--fg-3)">${d.date.slice(5)}</text>` : null)}
    ${series.map((k) => html`<g key=${k}><path d=${daily.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d[k] || 0).toFixed(1)}`).join("")} fill="none" stroke=${KC[k]} stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
      ${hv !== null && html`<circle cx=${x(hv)} cy=${y(daily[hv][k] || 0)} r="4" fill=${KC[k]} stroke="var(--surface)" stroke-width="2"/>`}</g>`)}
    ${hv !== null && html`<line x1=${x(hv)} x2=${x(hv)} y1=${P.t} y2=${H - P.b} stroke="var(--fg-3)" stroke-dasharray="3 3"/>`}</svg>
    ${hv !== null && html`<div class="tip" style=${`left:${Math.min(78, Math.max(2, x(hv) / W * 100))}%`}><b>${daily[hv].date}</b>${series.map((k) => html`<div key=${k}><i style=${"background:" + KC[k]}></i>${k} <b>${daily[hv][k] || 0}</b></div>`)}</div>`}
    <div class="row xs mut" style="gap:16px;margin-top:6px">${series.map((k) => html`<span key=${k}><i class="dot" style=${"background:" + KC[k]}></i>${k}</span>`)}</div></div>`;
}
const Bars = ({ items, color = "var(--blue)", link }) => { const mx = Math.max(1, ...items.map((i) => i.count));
  return items.length ? html`<div>${items.map((i) => html`<div class="hb" key=${i.name}><span class="hl">${link ? html`<a href=${"#/package/" + i.name}>${i.name}</a>` : i.name}</span><span class="ht"><i style=${`width:${i.count / mx * 100}%;background:${color}`}></i></span><span class="hn">${fmt(i.count)}</span></div>`)}</div>` : html`<p class="mut sm">No data in this period.</p>`; };
const Donut = ({ items }) => { const tot = items.reduce((a, b) => a + b.count, 0); if (!tot) return html`<p class="mut sm">No data in this period.</p>`;
  const pal = ["var(--blue)", "var(--green)", "var(--orange)", "var(--red)", "var(--fg-3)"]; let acc = 0, R = 42, C = 2 * Math.PI * R;
  return html`<div class="row" style="gap:20px;flex-wrap:nowrap"><svg viewBox="0 0 100 100" width="120" height="120" style="flex:none;transform:rotate(-90deg)" role="img" aria-label="Breakdown">
    ${items.map((it, i) => { const len = it.count / tot * C, el = html`<circle key=${it.name} cx="50" cy="50" r=${R} fill="none" stroke=${pal[i % 5]} stroke-width="14" stroke-dasharray=${`${len} ${C - len}`} stroke-dashoffset=${-acc}/>`; acc += len; return el; })}</svg>
    <div class="sm" style="min-width:0">${items.map((it, i) => html`<div key=${it.name}><i class="dot" style=${"background:" + pal[i % 5]}></i>${it.name} <b>${Math.round(it.count / tot * 100)}%</b> <span class="mut3">${fmt(it.count)}</span></div>`)}</div></div>`; };

function Dashboard() {
  const { me } = useStore(); const [days, setDays] = useState(30); const [pkg, setPkg] = useState(""); const [tick, setTick] = useState(0);
  const [fl, setFl] = useState({ type: "", user: "", source: "", kind: "" }); const setF = (k) => (e) => setFl({ ...fl, [k]: e.target.value });
  const qs = `days=${days}` + (pkg ? "&package=" + encodeURIComponent(pkg) : "") + Object.entries(fl).filter(([, v]) => v).map(([k, v]) => `&${k}=${encodeURIComponent(v)}`).join("");
  const d = useLoad(() => me && can("view_dashboard") ? api("/dashboard?" + qs) : Promise.resolve(null), [me, qs, tick]);
  const nFilters = Object.values(fl).filter(Boolean).length + (pkg ? 1 : 0);
  useEffect(() => { const t = setInterval(() => setTick((x) => x + 1), 60000); return () => clearInterval(t); }, []);
  const pk = useLoad(() => api("/packages?per_page=100&sort=name"), []);
  if (!me) return html`<${Empty} t="Sign in required" d="Sign in to view the dashboard." />`;
  if (!can("view_dashboard")) return html`<${Empty} t="No access" d="Your role doesn’t include the usage dashboard. Ask an admin to grant “View the usage dashboard”." />`;
  const D = d.d, t = (D && D.totals) || {}, pv = (D && D.previous) || {};
  const exportCsv = () => { const rows = ["date,install,use,update,uninstall,error", ...D.daily.map((x) => [x.date, x.install, x.use, x.update, x.uninstall, x.error].join(","))]; saveBlob(new Blob([rows.join("\n")], { type: "text/csv" }), `usage-${days}d${pkg ? "-" + pkg : ""}.csv`); };
  return html`<div class="stack"><div class="row"><h1>Dashboard</h1><span class="sp"></span>
      <select class="field" aria-label="Package" value=${pkg} onChange=${(e) => setPkg(e.target.value)}><option value="">All packages</option>${(pk.d ? pk.d.items : []).map((p) => html`<option value=${p.name} key=${p.name} selected=${p.name === pkg}>${p.name}</option>`)}</select>
      <div class="seg">${[7, 30, 90, 365].map((n) => html`<button key=${n} class=${days === n ? "on" : ""} onClick=${() => setDays(n)}>${n === 365 ? "1y" : n + "d"}</button>`)}</div>
      <button class="btn sec sm" onClick=${exportCsv} disabled=${!D}>Export CSV</button></div>
    <div class="row filters" role="group" aria-label="Filters"><span class="sm mut">Filter</span>
      <select class="field" aria-label="Type" value=${fl.type} onChange=${setF("type")}><option value="">Any type</option>${((D && D.options.types) || []).map((x) => html`<option key=${x} selected=${x === fl.type}>${x}</option>`)}</select>
      <select class="field" aria-label="Person" value=${fl.user} onChange=${setF("user")}><option value="">Anyone</option>${((D && D.options.users) || []).map((x) => html`<option key=${x} selected=${x === fl.user}>${x}</option>`)}</select>
      <select class="field" aria-label="Tool" value=${fl.source} onChange=${setF("source")}><option value="">Any tool</option>${((D && D.options.sources) || []).map((x) => html`<option key=${x} selected=${x === fl.source}>${x}</option>`)}</select>
      <select class="field" aria-label="Event" value=${fl.kind} onChange=${setF("kind")}><option value="">Any event</option>${["use", "install", "update", "uninstall", "error"].map((x) => html`<option key=${x} selected=${x === fl.kind}>${x}</option>`)}</select>
      ${nFilters > 0 && html`<button class="btn sec sm" onClick=${() => { setFl({ type: "", user: "", source: "", kind: "" }); setPkg(""); }}>Clear ${nFilters}</button>`}</div>
    ${d.loading && !D ? html`<${Skels} n=4 />` : d.e ? html`<${Err} e=${d.e} />` : D && html`<div class="stack">
      <div class="kpis"><${Kpi} label="Tool calls" value=${t.use || 0} prev=${pv.use || 0} /><${Kpi} label="Active people" value=${D.active_users} prev=${D.active_users_prev} />
        <${Kpi} label="Installs" value=${t.install || 0} prev=${pv.install || 0} /><${Kpi} label="Errors" value=${t.error || 0} prev=${pv.error || 0} invert=${true} /></div>
      <div class="card stack"><div class="row"><h3>Activity</h3><span class="sp"></span><span class="xs mut3">${D.active_packages} active packages · ${D.clients} devices · UTC</span></div>
        ${Object.values(t).some((v) => v) ? html`<${LineChart} daily=${D.daily} series=${["use", "install", "error"]} />` : html`<${Empty} t="No activity yet" d="Events appear here as people install and use packages." />`}</div>
      <div class="two"><div class="card stack"><h3>Top packages</h3><${Bars} items=${D.top_packages} link=${true} /></div><div class="card stack"><h3>Top people</h3><${Bars} items=${D.top_users} color="var(--green)" /></div></div>
      <div class="two"><div class="card stack"><h3>By type</h3><${Donut} items=${D.by_type} /></div><div class="card stack"><h3>By tool</h3><${Donut} items=${D.by_source} /></div></div>
      <div class="card stack"><h3>Recent activity</h3>${D.recent.length ? html`<div class="tscroll"><table><thead><tr><th>When</th><th>Who</th><th>Event</th><th>Package</th><th>Component</th></tr></thead><tbody>
        ${D.recent.map((e, i) => html`<tr key=${i}><td class="mut sm">${ago(e.ts)}</td><td>${e.actor}</td><td><span class=${"badge " + ({ use: "blue", install: "green", error: "red", update: "orange" }[e.kind] || "")}>${e.kind}</span></td><td><a href=${"#/package/" + e.package}>${e.package}</a></td><td class="mono mut">${e.component || ""}</td></tr>`)}</tbody></table></div>` : html`<p class="mut sm">Nothing yet.</p>`}</div></div>`}</div>`;
}

// ---------- shell
function App() {
  const s = useStore(); const path = s.route.split("?")[0], seg = path.split("/").filter(Boolean);
  useEffect(() => { loadMe(); }, []);
  const logout = async () => { try { await api("/auth/logout", { method: "POST" }); } catch {} try { localStorage.removeItem("aihub_token"); } catch {} set({ me: null }); nav("/"); };
  const on = (k) => (seg[0] === k ? "on" : "");
  const page = { "": html`<${Home} />`, browse: html`<${Browse} key=${s.route} />`, package: html`<${Package} name=${seg[1]} key=${seg[1]} />`, rankings: html`<${Rankings} />`, docs: html`<${Docs} slug=${seg[1]} key=${seg[1]} />`, start: html`<${Start} />`,
    login: html`<${Auth} mode="login" />`, register: html`<${Auth} mode="register" />`, account: html`<${Account} />`, admin: html`<${Admin} />`, dashboard: html`<${Dashboard} />` }[seg[0] || ""] || html`<${Empty} t="Page not found" d="That page doesn’t exist." />`;
  return html`<div><header class="nav"><div class="nav-in"><a class="brand" href="#/">AI Hub</a>
    <nav class="links" aria-label="Main"><a class=${on("browse")} href="#/browse">Browse</a><a class=${on("rankings")} href="#/rankings">Rankings</a><a class=${on("start")} href="#/start">Get started</a><a class=${on("docs")} href="#/docs">Docs</a></nav>
    <div class="r">${s.me ? html`<a href="#/account" class="me-link"><${Avatar} p=${s.me} size=${24} /><span>${shown(s.me)}</span></a>${can("view_dashboard") ? html`<a href="#/dashboard">Dashboard</a>` : ""}${can("admin") ? html`<a href="#/admin">Admin</a>` : ""}<a href="#/" onClick=${logout}>Sign out</a>` :
      html`<a href="#/login">Sign in</a>`}</div></div></header>
    <main>${page}</main><footer class="foot">AI Hub · internal registry for AI tooling</footer>
    ${s.toast && html`<div class=${"toast " + (s.toast.err ? "err" : "")} role="status">${s.toast.m}</div>`}</div>`;
}
render(html`<${App} />`, document.getElementById("app"));
