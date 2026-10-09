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
async function loadBrand() { try { const m = await api("/meta"); set({ brand: m }); document.title = m.name; document.documentElement.dataset.accent = m.theme || "blue"; } catch {} }
async function loadMe() { try { const me = tok() ? await api("/auth/me") : null; let showGroups = false; if (me) { try { showGroups = (await api("/groups/capabilities")).show_page; } catch {} } set({ me, showGroups }); } catch { try { localStorage.removeItem("aihub_token"); } catch {} set({ me: null }); } }
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
// navigator.clipboard only exists on HTTPS / localhost; on plain http (or a denied permission) fall back to a hidden textarea
const writeClip = async (t) => {
  try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(t); return true; } } catch {}
  const a = document.createElement("textarea"); a.value = t; a.setAttribute("readonly", ""); a.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(a); a.select(); a.setSelectionRange(0, t.length);
  let ok = false; try { ok = document.execCommand("copy"); } catch {} a.remove(); return ok;
};
const copy = async (t) => { (await writeClip(t)) ? toast("Copied") : toast("Copy failed", 1); };
function useLoad(fn, deps) {
  const [s, setS] = useState({ loading: true });
  // keep the previous data visible while a new request is in flight (no flash, controls keep their options)
  useEffect(() => { let on = 1; setS((p) => ({ d: p.d, loading: true })); fn().then((d) => on && setS({ d }), (e) => on && setS({ e: e.message })); return () => { on = 0; }; }, deps);
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
const Pkg = ({ p, i = 0 }) => html`<a class="card pkg spot" style=${"--i:" + Math.min(i, 12)} href=${"#/package/" + p.name}>
  <div class="row"><h3>${p.name}</h3><span class="sp"></span>${p.visibility === "private" && html`<span class="badge orange" title="Only people you share it with can see this">private</span>`}<span class=${"badge " + (TYPE[p.type] || "")}>${p.type}</span></div>
  <p>${p.description || "No description"}</p>
  <div class="meta"><span>v${p.latest_version || "–"}</span><span>↓ ${fmt(p.downloads)}</span><${Stars} r=${p.rating && p.rating.avg} /></div></a>`;

// ---------- pages
// ---------- motion helpers (all JS-driven motion is skipped under prefers-reduced-motion)
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const revealIO = "IntersectionObserver" in window && !reduced ? new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); revealIO.unobserve(e.target); } }), { rootMargin: "0px 0px -8% 0px", threshold: 0.08 }) : null;
// call once per page component: marks every .rv element visible as it scrolls into view
const useReveal = () => useEffect(() => { document.querySelectorAll(".rv:not(.in)").forEach((el) => (revealIO ? revealIO.observe(el) : el.classList.add("in"))); });
// spotlight cards follow the pointer through CSS variables, no component state involved
addEventListener("pointermove", (e) => { const c = e.target.closest && e.target.closest(".spot"); if (!c) return; const r = c.getBoundingClientRect(); c.style.setProperty("--mx", e.clientX - r.left + "px"); c.style.setProperty("--my", e.clientY - r.top + "px"); }, { passive: true });

function Count({ to }) {
  const ref = useRef();
  useEffect(() => {
    const el = ref.current, n = Number(to) || 0; if (!el) return;
    if (reduced || !("IntersectionObserver" in window)) { el.textContent = fmt(n); return; }
    let raf; const ob = new IntersectionObserver((es) => { if (!es[0].isIntersecting) return; ob.disconnect(); const t0 = performance.now();
      const step = (t) => { const k = Math.min(1, (t - t0) / 900); el.textContent = fmt(Math.round(n * (1 - Math.pow(1 - k, 3)))); if (k < 1) raf = requestAnimationFrame(step); }; raf = requestAnimationFrame(step); });
    el.textContent = fmt(0); ob.observe(el); return () => { ob.disconnect(); cancelAnimationFrame(raf); };
  }, [to]);
  return html`<span ref=${ref}>${fmt(to)}</span>`;
}

// types real package names after "aihub install"; the full name is kept in data-full so Copy always gets the whole command
function Typer({ words }) {
  const ref = useRef(), key = words.join();
  useEffect(() => {
    const el = ref.current; if (!el || !words.length) return;
    el.dataset.full = words[0]; if (reduced) { el.textContent = words[0]; return; }
    let i = 0, j = 0, dir = 1, t;
    const tick = () => { const w = words[i]; el.dataset.full = w; j += dir; el.textContent = w.slice(0, j); let d = dir > 0 ? 70 : 32;
      if (dir > 0 && j >= w.length) { dir = -1; d = 2000; } else if (dir < 0 && j <= 0) { dir = 1; i = (i + 1) % words.length; d = 380; } t = setTimeout(tick, d); };
    tick(); return () => clearTimeout(t);
  }, [key]);
  return html`<span class="typed" ref=${ref}>${words[0] || "package-name"}</span>`;
}

const since = (t) => { const s = Date.now() / 1000 - t; return s < 45 ? "just now" : ago(t); };
const VERB = { install: "installed", use: "used", update: "updated" };
function Activity() {
  const [items, setItems] = useState(null), [err, setErr] = useState(null), [, tick] = useState(0); const seen = useRef(new Set());
  useEffect(() => {
    let on = 1;
    const load = async () => {
      if (document.hidden) return;
      try { const r = await api("/activity"); if (!on) return;
        const rows = r.items.map((i) => ({ ...i, k: i.package + i.kind + i.ts })); const first = seen.current.size === 0;
        setItems(rows.map((x) => ({ ...x, fresh: !first && !seen.current.has(x.k) }))); rows.forEach((x) => seen.current.add(x.k)); setErr(null);
      } catch (e) { if (on) setErr(e.message); }
    };
    load(); const poll = setInterval(load, 20000), clock = setInterval(() => !document.hidden && tick((n) => n + 1), 30000);
    const vis = () => { if (!document.hidden) load(); }; document.addEventListener("visibilitychange", vis);
    return () => { on = 0; clearInterval(poll); clearInterval(clock); document.removeEventListener("visibilitychange", vis); };
  }, []);
  return html`<aside class="feed" aria-label="Live activity"><div class="feed-h"><h3>Happening now</h3><span class="live" title="Refreshes while this tab is open">live</span></div>
    ${err ? html`<p class="mut sm feed-e">Activity is unavailable right now.</p>` : !items ? html`<div class="feed-l">${[0, 1, 2, 3, 4].map((i) => html`<div class="skel line" key=${i}></div>`)}</div>` :
      !items.length ? html`<p class="mut sm feed-e">Nothing yet. Installs and usage show up here as they happen.</p>` :
      html`<ul class="feed-l">${items.slice(0, 7).map((x, n) => html`<li key=${x.k} class=${x.fresh ? "fresh" : ""} style=${"--i:" + n}>
        <span class=${"badge " + (TYPE[x.type] || "")}>${x.type}</span><a href=${"#/package/" + x.package}>${x.package}</a><span class="v">${VERB[x.kind] || x.kind}</span><time class="mut3">${since(x.ts)}</time></li>`)}</ul>`}</aside>`;
}

const catClass = (n) => ["a", "b", "c", "d", "e", "f"][n] || "x";
function Home() {
  const [q, setQ] = useState("");
  const m = useLoad(() => api("/meta"), []);
  const top = useLoad(() => api("/packages?sort=downloads&per_page=6"), []);
  const recent = useLoad(() => api("/packages?sort=updated&per_page=8"), []);
  const f = useLoad(() => api("/facets"), []);
  const o = (m.d && m.d.overview) || {}; const cats = (f.d && f.d.categories) || [];
  const names = ((top.d && top.d.items) || []).map((p) => p.name);
  useReveal();
  return html`<div class="home">
    <section class="hero"><div class="hero-copy"><h1>Install what your AI needs.</h1>
      <p class="lead">Find skills, agents and MCP servers for Claude Code, Codex and OpenCode, then install them with one command.</p>
      <form class="search" onSubmit=${(e) => { e.preventDefault(); nav("/browse?q=" + encodeURIComponent(q)); }}><${Search} value=${q} onInput=${setQ} /></form>
      <div class="cmd hero-cmd"><code>aihub install <${Typer} words=${names} /></code><button class="btn sec sm" onClick=${() => { const t = document.querySelector(".typed"); copy("aihub install " + ((t && t.dataset.full) || "<package>")); }}>Copy</button></div></div>
      <${Activity} /></section>
    <div class="stats3 rv"><div class="stat"><b><${Count} to=${o.packages} /></b><span>Packages</span></div><div class="stat"><b><${Count} to=${o.versions} /></b><span>Releases</span></div>
      <div class="stat"><b><${Count} to=${o.downloads} /></b><span>Downloads</span></div><div class="stat"><b><${Count} to=${o.users} /></b><span>People</span></div></div>
    <div class="gap rv"><h2>Browse by category</h2></div>
    ${f.loading && !f.d ? html`<div class="bento"><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>` : html`<div class="bento">${cats.map((c, n) => html`<a key=${c.id} href=${"#/browse?category=" + c.id} class=${"tile spot rv t-" + catClass(n)} style=${"--d:" + n * 70 + "ms"}>
      <div class="tile-in"><h3>${c.label}</h3><p>${c.desc}</p></div><span class="tile-n"><b>${c.count}</b> ${c.count === 1 ? "package" : "packages"}</span></a>`)}</div>`}
    <div class="gap row rv"><h2>Popular</h2><span class="sp"></span><a href="#/browse?sort=downloads">See all</a></div>
    <div style="margin-top:20px">${top.loading && !top.d ? html`<${Skels} n=3 />` : top.e ? html`<${Err} e=${top.e} />` :
      top.d.items.length ? html`<div class="grid">${top.d.items.map((p, i) => html`<${Pkg} p=${p} i=${i} key=${p.name} />`)}</div>` :
      html`<${Empty} t="Nothing here yet" d="Publish the first package with aihub dev publish." />`}</div>
    ${recent.d && recent.d.items.length ? html`<div class="gap rv"><h2>Recently updated</h2></div>
      <div class="strip rv">${recent.d.items.map((p) => html`<a class="chip-pkg spot" key=${p.name} href=${"#/package/" + p.name}><b>${p.name}</b><span>v${p.latest_version || "-"} / ${ago(p.updated)}</span></a>`)}</div>` : null}</div>`;
}

function Browse() {
  const qs = new URLSearchParams((store.route.split("?")[1]) || "");
  const [q, setQ] = useState(qs.get("q") || ""); const [type, setType] = useState(qs.get("type") || ""); const [sort, setSort] = useState(qs.get("sort") || "");
  const [cat, setCat] = useState(qs.get("category") || ""); const [tag, setTag] = useState(qs.get("tag") || ""); const [page, setPage] = useState(1);
  const dq = debounced(q); useEffect(() => setPage(1), [dq, type, sort, cat, tag]);
  // keep the address bar in sync without remounting the page (replaceState fires no hashchange)
  useEffect(() => { const p = new URLSearchParams(); [["q", dq], ["category", cat], ["type", type], ["tag", tag], ["sort", sort]].forEach(([k, v]) => v && p.set(k, v)); const s = p.toString(); history.replaceState(null, "", "#/browse" + (s ? "?" + s : "")); }, [dq, cat, type, tag, sort]);
  const f = useLoad(() => api("/facets"), []);
  const r = useLoad(() => api(`/packages?q=${encodeURIComponent(dq)}&type=${type}&sort=${sort}&category=${cat}&tag=${encodeURIComponent(tag)}&page=${page}&per_page=18`), [dq, type, sort, cat, tag, page]);
  const types = ["", ...Object.keys((f.d && f.d.types) || {})]; const cats = (f.d && f.d.categories) || [];
  const clear = () => { setQ(""); setType(""); setCat(""); setTag(""); };
  useReveal();
  return html`<div class="stack"><h1>Browse</h1><${Search} value=${q} onInput=${setQ} />
    <div class="chips" role="group" aria-label="Categories"><button class=${"chip " + (cat === "" ? "on" : "")} onClick=${() => setCat("")}>All</button>
      ${cats.map((c) => html`<button key=${c.id} class=${"chip " + (cat === c.id ? "on" : "")} onClick=${() => setCat(c.id)}>${c.label} <i>${c.count}</i></button>`)}
      ${tag && html`<button class="chip on" onClick=${() => setTag("")} title="Remove tag filter">#${tag} x</button>`}</div>
    <div class="row"><div class="seg" role="tablist">${types.map((t) => html`<button key=${t} class=${type === t ? "on" : ""} onClick=${() => setType(t)}>${t || "All types"}</button>`)}</div>
      <span class="sp"></span><select class="field" aria-label="Sort" value=${sort} onChange=${(e) => setSort(e.target.value)}>
      <option value="">${dq ? "Relevance" : "Recently updated"}</option><option value="downloads">Most downloaded</option><option value="rating">Best reviewed</option><option value="reviews">Most reviewed</option><option value="name">Name</option><option value="created">Newest</option></select></div>
    ${r.e ? html`<${Err} e=${r.e} />` : !r.d ? html`<${Skels} />` : !r.d.items.length ? html`<div class="empty"><h3>No results</h3><p>${dq ? `Nothing matches “${dq}”.` : "No packages match these filters."}</p><button class="btn sec sm" style="margin-top:14px" onClick=${clear}>Clear filters</button></div>` :
      html`<div class=${"grid results " + (r.loading ? "busy" : "")}>${r.d.items.map((p, i) => html`<${Pkg} p=${p} i=${i} key=${p.name} />`)}</div>
      <div class="row" style="justify-content:center;margin-top:24px"><button class="btn sec sm" disabled=${page <= 1} onClick=${() => setPage(page - 1)}>Previous</button>
        <span class="mut sm">${r.d.total} results, page ${page}</span><button class="btn sec sm" disabled=${page * r.d.per_page >= r.d.total} onClick=${() => setPage(page + 1)}>Next</button></div>`}</div>`;
}

function Related({ name }) {
  const r = useLoad(() => api(`/packages/${name}/related`), [name]);
  useReveal();
  if (!r.d || !r.d.items.length) return null;
  return html`<section class="rv related"><h2 class="gap">Related</h2><div class="grid" style="margin-top:20px">${r.d.items.map((p, i) => html`<${Pkg} p=${p} i=${i} key=${p.name} />`)}</div></section>`;
}

function Package({ name }) {
  const { me } = useStore(); const [tab, setTab] = useState("Overview"); const [tick, setTick] = useState(0);
  const p = useLoad(() => api("/packages/" + name), [name, tick]);
  const mt = useLoad(() => api("/meta"), []); const meta = mt.d || {};
  if (p.loading) return html`<div class="skel" style="min-height:300px"></div>`;
  if (p.e) return html`<${Err} e=${p.e} />`;
  const d = p.d, req = d.requires || {};
  return html`<div class="stack">
    <div class="row"><h1>${d.name}</h1><span class=${"badge " + (TYPE[d.type] || "")}>${d.type}</span></div>
    <p class="lead">${d.description}</p>
    <div class="row mut sm">${d.visibility === "private" && html`<span class="badge orange">private</span>`}<span>v${d.latest_version}</span><span>·</span><span>↓ ${fmt(d.downloads)}</span><${Stars} r=${d.rating.avg} /><span>· updated ${ago(d.updated)}</span>
      ${d.tags.map((t) => html`<a class="badge tag" key=${t} href=${"#/browse?tag=" + encodeURIComponent(t)}>${t}</a>`)}</div>
    <${Cmd} text=${"aihub install " + d.name} />
    <div class="seg">${["Overview", "Install", "Versions", "Usage", "Reviews", ...(d.access === "admin" ? ["Sharing"] : [])].map((t) => html`<button key=${t} class=${tab === t ? "on" : ""} onClick=${() => setTab(t)}>${t}</button>`)}</div>
    ${tab === "Overview" && html`<${Readme} name=${name} />`}
    ${tab === "Install" && html`<div class="list">
      <div class="li"><div class="t"><b>Command</b><span class="mono">aihub install ${d.name}</span></div></div>
      <div class="li"><div class="t"><b>Required commands</b><span>${(req.commands || []).length ? (req.commands || []).map((c) => (c.name || c) + (c.hint ? ` - ${c.hint}` : "")).join(" · ") : "None"}</span></div></div>
      <div class="li"><div class="t"><b>Depends on</b><span>${(req.packages || []).length ? (req.packages || []).map((x) => html`<a href=${"#/package/" + x.split(/[<>=!~]/)[0]} style="margin-right:10px">${x}</a>`) : "No other packages"}</span></div></div>
      <div class="li"><div class="t"><b>Supported systems</b><span>${(req.os || []).length ? req.os.join(", ") : "All"}</span></div></div></div>`}
    ${tab === "Versions" && html`<div class="list tscroll"><table><thead><tr><th>Version</th><th>Released</th><th>Size</th><th>Downloads</th><th>SHA-256</th>${meta.allow_source_download ? html`<th></th>` : null}</tr></thead><tbody>
      ${d.versions.map((v) => html`<tr key=${v.version}><td>${v.version} ${v.yanked ? html`<span class="badge red">yanked</span>` : ""}</td><td>${ago(v.created)}</td><td>${(v.size / 1024).toFixed(1)} KB</td><td>${fmt(v.downloads)}</td><td class="mono">${v.sha256.slice(0, 12)}</td>${meta.allow_source_download ? html`<td>${v.source ? html`<a class="btn sec sm" href=${v.source} target="_blank" rel="noopener noreferrer">Source ↗</a>` : null}</td>` : null}</tr>`)}</tbody></table></div>`}
    ${tab === "Usage" && html`<${Usage} name=${name} />`}
    ${tab === "Sharing" && html`<${Sharing} name=${name} pkg=${d} onChange=${() => setTick(tick + 1)} />`}
    ${tab === "Reviews" && html`<${Reviews} name=${name} me=${me} onDone=${() => setTick(tick + 1)} />`}
    <${Related} name=${name} /></div>`;
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
  const [rating, setRating] = useState(5); const [body, setBody] = useState(""); const [who, setWho] = useState("");
  const mt = useLoad(() => api("/meta"), []); const anonOk = !me && !!(mt.d && mt.d.anonymous_review);
  const post = async () => { if (anonOk && who.trim().length < 2) return toast("Please enter your name", 1);
    try { await api(`/packages/${name}/reviews`, { method: "POST", body: { rating, body, name: who.trim() } }); toast("Thanks for your review"); setBody(""); setTick(tick + 1); onDone(); } catch (e) { toast(e.message, 1); } };
  return html`<div class="stack">${me || anonOk ? html`<div class="card stack">${anonOk && html`<input class="field" required maxlength="40" placeholder="Your name (required)" aria-label="Your name" value=${who} onInput=${(e) => setWho(e.target.value)} />`}<div class="row"><b>Your rating</b><div class="seg">${[1, 2, 3, 4, 5].map((n) => html`<button key=${n} class=${rating === n ? "on" : ""} onClick=${() => setRating(n)}>${"★".repeat(n)}</button>`)}</div></div>
      <textarea class="field" rows="3" style="padding:12px;resize:vertical" placeholder="Share your experience (optional)" value=${body} onInput=${(e) => setBody(e.target.value)}></textarea>
      <button class="btn" disabled=${anonOk && who.trim().length < 2} onClick=${post}>Submit review</button></div>` : html`<p class="mut"><a href="#/login">Sign in</a> to leave a review.</p>`}
    ${r.d && (r.d.reviews.length ? html`<div class="list">${r.d.reviews.map((x) => html`<div class="li" key=${x.username || "anon:" + x.display_name}><${Avatar} p=${x.anonymous ? { username: x.display_name } : x} size=${36} /><div class="t"><b>${shown(x)} <span class="mut3">${"★".repeat(x.rating)}</span></b>${x.title && html`<span class="xs mut3" style="display:block">${x.title}</span>`}<span>${x.body}</span></div><span class="xs mut3">${ago(x.created)}</span></div>`)}</div>` : html`<p class="mut">No reviews yet.</p>`)}</div>`;
}

function Rankings() {
  const [tab, setTab] = useState("packages"); const r = useLoad(() => api("/rankings/" + tab), [tab]);
  const label = { packages: "Most used", reviews: "Top rated", reviewed: "Most reviewed", developers: "Top developers", users: "Most active users" };
  const items = (r.d && r.d.items) || [], mx = Math.max(1, ...items.map((i) => (tab === "reviews" ? i.score : i.count)));
  return html`<div class="stack"><h1>Rankings</h1><p class="lead">${tab === "reviews" || tab === "reviewed" ? "Based on all reviews, ever." : "Based on the last 30 days of real usage."}</p>
    <div class="seg">${Object.keys(label).map((t) => html`<button key=${t} class=${tab === t ? "on" : ""} onClick=${() => setTab(t)}>${label[t]}</button>`)}</div>
    ${r.loading ? html`<div class="skel"></div>` : !items.length ? html`<${Empty} t="No data yet" d="Rankings appear once usage events arrive." />` :
      html`<div class="list">${items.map((i, n) => html`<div class="li" key=${i.name}><span class=${"rank " + (n < 3 ? "top" : "")}>${n + 1}</span>
        <div class="t"><b>${tab === "packages" || tab === "reviews" || tab === "reviewed" ? html`<a href=${"#/package/" + i.name}>${i.name}</a>` : i.name}</b><div class="bar"><i style=${"width:" + ((tab === "reviews" ? i.score : i.count) / mx * 100) + "%"}></i></div></div>
        ${tab === "reviews" || tab === "reviewed" ? html`<span class="n" title=${i.count + " reviews"}>★ ${i.avg} <span class="mut3">(${fmt(i.count)})</span></span>` : html`<span class="n">${fmt(i.count)}</span>`}</div>`)}</div>`}</div>`;
}

const detectOS = () => { const u = (navigator.userAgentData && navigator.userAgentData.platform || navigator.platform || "") + " " + navigator.userAgent; return /win/i.test(u) && !/darwin/i.test(u) ? "windows" : /mac|iphone|ipad/i.test(u) ? "mac" : "linux"; };

function Start() {
  const m = useLoad(() => api("/meta"), []); const o = ((m.d && m.d.public_url) || location.origin).replace(/\/+$/, ""); const auto = detectOS(); const [os, setOs] = useState(auto); const [alt, setAlt] = useState(false);
  const cfg = useLoad(() => fetch("/api/v1/client-config").then((r) => r.ok ? r.json() : null).catch(() => null), []);
  const OS = { mac: "macOS", linux: "Linux", windows: "Windows" };
  const cmd = os === "windows" ? `irm ${o}/install.ps1 | iex` : `curl -fsSL ${o}/install.sh | sh`;
    const gitBranch = (m.d && m.d.cli_git_branch || "").trim(), gitSubdir = (m.d && m.d.cli_git_subdir || "").trim();
    const pip = `pip install "aihub-cli @ git+${(m.d && m.d.cli_git_url) || "<your-repo-url>"}${gitBranch ? "@" + gitBranch : ""}${gitSubdir ? "#subdirectory=" + gitSubdir : ""}"`;
  const note = os === "windows" ? "Run in PowerShell. Needs Python 3.9+ (python.org or `winget install Python.Python.3.12`). PATH is updated automatically; open a new terminal afterwards."
    : os === "mac" ? "Run in Terminal. Needs Python 3.9+ (preinstalled with Xcode tools, or `brew install python`). Then add it to PATH:"
    : "Run in your shell. Needs Python 3.9+ and curl (e.g. `sudo apt install python3 curl`). Then add it to PATH:";
  const path = os === "mac" ? `echo 'export PATH="$HOME/.aihub/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc` : os === "linux" ? `echo 'export PATH="$HOME/.aihub/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc` : null;
  const envs = os === "windows" ? `$env:LANGFUSE_BASE_URL="https://…"; $env:LANGFUSE_PUBLIC_KEY="pk-lf-…"; $env:LANGFUSE_SECRET_KEY="sk-lf-…"` : `export LANGFUSE_BASE_URL="https://…" LANGFUSE_PUBLIC_KEY="pk-lf-…" LANGFUSE_SECRET_KEY="sk-lf-…"`;
  const step = (n, t, d, c) => html`<div class="card stack"><div class="row"><span class="rank top">${n}</span><h3>${t}</h3></div><p class="mut">${d}</p>${c && html`<${Cmd} text=${c} />`}</div>`;
  if (m.loading) return html`<div class="skel"></div>`;
  const idx = cfg.d && cfg.d.index && cfg.d.index.url;
  return html`<div class="stack"><h1>Get started</h1><p class="lead">Up and running in under a minute. Python 3.9 or later, and git.</p>
    <div class="card stack"><div class="row"><span class="rank top">1</span><h3>Install the CLI</h3><span class="sp"></span>
      <div class="seg" role="tablist">${Object.keys(OS).map((k) => html`<button key=${k} class=${os === k ? "on" : ""} onClick=${() => setOs(k)}>${OS[k]}${k === auto ? " (detected)" : ""}</button>`)}</div></div>
      <p class="mut">Installs aihub and sets up usage reporting for Claude Code and OpenCode. ${note}</p>
      <${Cmd} text=${cmd} />${path && html`<${Cmd} text=${path} />`}
      <button class="btn sec sm" style="align-self:flex-start" onClick=${() => setAlt(!alt)} aria-expanded=${alt}>${alt ? "Hide" : "Can’t reach this server from your machine?"}</button>
      ${alt && html`<div class="stack"><p class="mut">Install straight from git instead. It needs only git and Python, not this website.</p><${Cmd} text=${pip} />
        <p class="mut">Then give the CLI your Langfuse settings. Export them (the CLI picks these up automatically), or run <code>aihub setup --manual</code> and it asks for each one:</p><${Cmd} text=${envs} /><${Cmd} text="aihub setup --manual" />
        <p class="mut">${idx ? html`The package index is <code>${idx}</code>; ` : ""}if git asks for a login, enter your username and an access token once. Your git credential helper remembers it.</p></div>`}</div>
    ${step(2, "Connect to Langfuse and the package index", "Pulls the index location and, if your admin shares them, the Langfuse keys. Asks you for anything missing. Refreshes itself later.", "aihub setup")}
    ${step(3, "Install something", "You'll be asked which tools to connect it to. Everything is reversible.", "aihub install <package>")}
    ${step(4, "Publish your own", "Scaffold a manifest, then publish. aihub commits and pushes to your git repo for you.", "aihub dev init && aihub dev publish")}</div>`;
}

function Auth({ mode }) {
  const [u, setU] = useState(""); const [p, setP] = useState(""); const [e, setE] = useState(""); const [busy, setBusy] = useState(false); const reg = mode === "register";
  const go = async (ev) => { ev.preventDefault(); setBusy(true); setE("");
    try { if (reg) { const r = await api("/auth/register", { method: "POST", body: { username: u, password: p } }); if (r.status === "pending") { toast("Account created - awaiting admin approval"); return nav("/login"); } }
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
    setBusy(true); try { await api("/auth/password", { method: "POST", body: { old: f.old, new: f.nw } }); setF({ old: "", nw: "", again: "" }); toast("Password changed - other devices were signed out"); }
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
    ${fresh && html`<div class="card stack"><b>Copy your token now - it won’t be shown again.</b><${Cmd} text=${fresh} /></div>`}
    ${t.d && t.d.tokens.length ? html`<div class="list">${t.d.tokens.map((k) => html`<div class="li" key=${k.id}><div class="t"><b>${k.name || k.kind}</b><span>${k.kind} · created ${ago(k.created)}</span></div><button class="btn danger sm" onClick=${() => rm(k.id)}>Revoke</button></div>`)}</div>` : null}
    <h2 class="gap">My packages</h2>${mine.d && mine.d.packages.length ? html`<div class="list">${mine.d.packages.map((x) => html`<a class="li" href=${"#/package/" + x.name} key=${x.name}><div class="t"><b>${x.name}</b><span>${x.type} · v${x.latest_version}</span></div></a>`)}</div>` : html`<p class="mut">You haven’t published anything yet.</p>`}</div>`;
}

function Admin() {
  const { me } = useStore(); const [tab, setTab] = useState("People"); const [tick, setTick] = useState(0); const bump = () => setTick((x) => x + 1);
  if (!can("admin") && !can("reset_password")) return html`<${Empty} t="Admins only" d="You don’t have access to this page." />`;
  const tabs = can("admin") ? ["People", "Roles", "Sync", "Settings"] : ["People"];
  return html`<div class="stack"><h1>Admin</h1>${tabs.length > 1 && html`<div class="seg">${tabs.map((t) => html`<button key=${t} class=${tab === t ? "on" : ""} onClick=${() => setTab(t)}>${t}</button>`)}</div>`}
    ${tab === "People" && html`<${People} me=${me} tick=${tick} bump=${bump} />`}${tab === "Roles" && can("admin") && html`<${Roles} tick=${tick} bump=${bump} />`}
    ${tab === "Sync" && can("admin") && html`<${AdminSync} />`}${tab === "Settings" && can("admin") && html`<${AdminSettings} />`}</div>`;
}
const SC = { active: "green", pending: "orange", disabled: "red" };
function People({ me, tick, bump }) {
  const d = useLoad(async () => ({ users: (await api("/admin/users")).users, roles: Object.keys((await api("/admin/roles")).roles).filter((x) => x !== "anonymous") }), [tick]);
  const [q, setQ] = useState(""); const [batch, setBatch] = useState(false); const [ask, setAsk] = useState(null); const [pw, setPw] = useState(null);
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const st = async (u, status) => { try { await api(`/admin/users/${u}/status`, { method: "POST", body: { status } }); bump(); } catch (e) { toast(e.message, 1); } };
  const role = async (u, r) => { try { await api(`/admin/users/${u}/role`, { method: "POST", body: { role: r } }); toast("Role updated"); bump(); } catch (e) { toast(e.message, 1); } };
  const users = d.d.users.filter((u) => u.username.toLowerCase().includes(q.toLowerCase()));
  const adm = can("admin"), canReset = can("reset_password");
  const reset = async (u) => { try { const r = await api(`/admin/users/${u}/reset-password`, { method: "POST" }); setPw(r); setAsk(null); bump(); } catch (e) { toast(e.message, 1); setAsk(null); } };
  return html`<div class="stack"><div class="row"><div style="flex:1;min-width:200px"><${Search} value=${q} onInput=${setQ} ph="Filter people" /></div>${adm && html`<button class="btn" onClick=${() => setBatch(!batch)}>${batch ? "Close" : "Create accounts in bulk"}</button>`}</div>
    ${pw && html`<div class="card stack" style="border:1px solid var(--orange)"><b>New password for ${pw.username}</b><div class="row"><code class="mono" style="font-size:18px;user-select:all">${pw.password}</code><span class="sp"></span>
      <button class="btn sec sm" onClick=${() => { copy(pw.password) }}>Copy</button><button class="btn sm" onClick=${() => setPw(null)}>Done</button></div>
      <span class="xs mut3">Shown once. ${pw.username} was signed out everywhere and should change it after signing in (Account → Password).</span></div>`}
    ${batch && adm && html`<${BatchImport} roles=${d.d.roles} done=${bump} />`}
    <div class="list">${users.map((u) => html`<div class="li" key=${u.username}><${Avatar} p=${u} size=${36} /><div class="t"><b>${shown(u)}</b><span>${u.display_name ? u.username + " · " : ""}${u.title ? u.title + " · " : ""}${u.created ? "joined " + ago(u.created) : ""}</span></div>
      ${adm ? html`<select class="field" style="min-height:32px;font-size:14px" aria-label="Role" disabled=${u.username === me.username} onChange=${(e) => role(u.username, e.target.value)}>${d.d.roles.map((r) => html`<option value=${r} key=${r} selected=${r === u.role}>${r}</option>`)}</select>` : html`<span class="badge">${u.role}</span>`}
      <span class=${"badge " + SC[u.status]}>${u.status}</span>${adm && (u.status !== "active" ? html`<button class="btn sec sm" onClick=${() => st(u.username, "active")}>Approve</button>` : u.username !== me.username ? html`<button class="btn danger sm" onClick=${() => st(u.username, "disabled")}>Disable</button>` : null)}
      ${canReset && u.username !== me.username && (ask === u.username ? html`<button class="btn danger sm" onClick=${() => reset(u.username)}>Confirm reset</button><button class="btn sec sm" onClick=${() => setAsk(null)}>Cancel</button>` : html`<button class="btn sec sm" onClick=${() => setAsk(u.username)}>Reset password</button>`)}</div>`)}
      ${!users.length && html`<div class="li mut">No matches.</div>`}</div></div>`;
}
function BatchImport({ roles, done }) {
  const [file, setFile] = useState(null); const [role, setRole] = useState("user"); const [busy, setBusy] = useState(false); const [res, setRes] = useState(null);
  const tpl = () => saveBlob(new Blob(["username,display_name,title,role,group\nalice,Alice Nguyen,Data Analyst,user,data-team\nbob,Bob Martin,Engineering Manager,publisher,data-team; platform\ncarol,Carol Diaz,,,\n"], { type: "text/csv" }), "accounts-template.csv");
  const go = async () => { if (!file) return; setBusy(true); setRes(null);
    try { const r = await fetch(`/api/v1/admin/users/batch?role=${encodeURIComponent(role)}`, { method: "POST", headers: { Authorization: "Bearer " + tok(), "X-Aihub-Filename": file.name }, body: await file.arrayBuffer() });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Upload failed");
      const blob = await r.blob(); saveBlob(blob, "new-accounts.csv"); setRes({ created: +r.headers.get("X-Created"), skipped: +r.headers.get("X-Skipped") }); done();
    } catch (e) { toast(e.message, 1); } finally { setBusy(false); } };
  return html`<div class="card stack"><h3>Create accounts in bulk</h3>
    <p class="mut sm">Upload a <b>.csv</b> or <b>.xlsx</b> with a header row. Columns (any order): <code>username</code> (required), <code>display_name</code>, <code>title</code>, <code>role</code>, <code>group</code>. Put several groups in one cell separated by <code>;</code> or <code>,</code>. Groups must already exist - a row naming an unknown group is skipped and reported. Without a header row it reads <code>username, role</code>. The default role below is used when the role cell is empty. Passwords are generated for you and returned as a downloadable sheet - <b>this is the only time they are shown</b>. Up to 300 accounts per upload.</p>
    <div class="row"><input type="file" class="field" style="padding:9px" accept=".csv,.tsv,.txt,.xlsx" onChange=${(e) => setFile(e.target.files[0] || null)} aria-label="Spreadsheet file" />
      <label class="sm mut">Default role <select class="field" value=${role} onChange=${(e) => setRole(e.target.value)}>${roles.map((r) => html`<option value=${r} key=${r} selected=${r === role}>${r}</option>`)}</select></label>
      <button class="btn sec sm" onClick=${tpl}>Download template</button><span class="sp"></span><button class="btn" disabled=${!file || busy} onClick=${go}>${busy ? "Creating…" : "Create & download passwords"}</button></div>
    ${res && html`<div class="row"><span class="badge green">${res.created} created</span>${res.skipped ? html`<span class="badge orange">${res.skipped} skipped - see the “status” column in the sheet</span>` : null}<span class="xs mut3">Keep the sheet safe and delete it after sharing passwords.</span></div>`}</div>`;
}
function Roles({ tick, bump }) {
  const d = useLoad(async () => ({ ...(await api("/admin/roles")), anon: (await api("/admin/settings")).values }), [tick]); const [nn, setNn] = useState(""); const [nd, setNd] = useState("");
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const roles = d.d.detail, perms = d.d.permissions, locked = d.d.anon_locked || [];
  const anon = d.d.anon, ANON = [["public_browse", "Browse public packages", "Signed-out visitors can browse packages, rankings and docs"], ["public_install", "Install public packages", "The CLI can install public packages without signing in"]];
  const toggleAnon = async (k) => { try { await api("/admin/settings", { method: "PUT", body: { [k]: anon[k] === "1" ? "0" : "1" } }); bump(); } catch (e) { toast(e.message, 1); } };
  const toggle = async (r, p) => { const cur = new Set(r.permissions); cur.has(p) ? cur.delete(p) : cur.add(p);
    try { await api("/admin/roles/" + r.name, { method: "PUT", body: { permissions: [...cur] } }); bump(); } catch (e) { toast(e.message, 1); } };
  const create = async () => { try { await api("/admin/roles", { method: "POST", body: { name: nn, description: nd, permissions: [] } }); setNn(""); setNd(""); toast("Role created - now grant it permissions"); bump(); } catch (e) { toast(e.message, 1); } };
  const del = async (r) => { try { await api("/admin/roles/" + r.name, { method: "DELETE" }); toast("Role deleted"); bump(); } catch (e) { toast(e.message, 1); } };
  return html`<div class="stack"><p class="mut">Tick what each role may do. The <code>anonymous</code> role is for visitors who are not signed in; it can never hold admin, manage all, reset password, audit or create groups. Changes apply immediately. The <code>admin</code> role always keeps full access.</p>
    <div class="list tscroll"><table class="matrix"><thead><tr><th>Permission</th>${roles.map((r) => html`<th key=${r.name} style="text-align:center"><div>${r.name}</div><div class="xs mut3" style="font-weight:400">${r.users} ${r.users === 1 ? "person" : "people"}</div></th>`)}</tr></thead><tbody>
      ${perms.map((p) => html`<tr key=${p.key}><td><b style="font-weight:500">${p.label}</b><div class="xs mut3 mono">${p.key}</div></td>${roles.map((r) => html`<td key=${r.name} style="text-align:center">
        <input type="checkbox" class="chk" aria-label=${`${r.name}: ${p.label}`} checked=${r.permissions.includes(p.key)} disabled=${r.name === "admin" || (r.name === "anonymous" && locked.includes(p.key))} title=${r.name === "anonymous" && locked.includes(p.key) ? "Never available without signing in" : ""} onChange=${() => toggle(r, p.key)} /></td>`)}</tr>`)}
      ${ANON.map(([k, label, help]) => html`<tr key=${k}><td><b style="font-weight:500">${label}</b><div class="xs mut3">${help}</div></td>${roles.map((r) => html`<td key=${r.name} style="text-align:center">${r.name === "anonymous" ? html`<input type="checkbox" class="chk" aria-label=${`anonymous: ${label}`} checked=${anon[k] === "1"} onChange=${() => toggleAnon(k)} />` : html`<input type="checkbox" class="chk" checked disabled title="Signed-in accounts always can" aria-label=${`${r.name}: ${label}`} />`}</td>`)}</tr>`)}
      <tr><td class="mut sm">Remove role</td>${roles.map((r) => html`<td key=${r.name} style="text-align:center">${r.builtin ? html`<span class="xs mut3">built-in</span>` : html`<button class="btn danger sm" onClick=${() => del(r)}>Delete</button>`}</td>`)}</tr></tbody></table></div>
    <div class="card stack"><h3>New role</h3><div class="row"><input class="field" style="flex:1;min-width:160px" placeholder="Name, e.g. analyst" value=${nn} onInput=${(e) => setNn(e.target.value)} />
      <input class="field" style="flex:2;min-width:200px" placeholder="Description (optional)" value=${nd} onInput=${(e) => setNd(e.target.value)} /><button class="btn" disabled=${!nn} onClick=${create}>Create role</button></div>
      <p class="xs mut3">Lowercase letters, digits, - or _. Create it, then tick its permissions in the table above.</p></div></div>`;
}
function NumSetting({ x, v, busy, save }) {
  const [t, setT] = useState(v); const n = Number(t), ok = /^\d+$/.test(t) && n >= x.min && n <= x.max;
  return html`<div class="li"><div class="t"><b>${x.label}</b><span>${x.help}</span>${!ok && html`<span class="xs" style="color:var(--orange)">Enter a whole number from ${x.min} to ${x.max}.</span>`}</div>
    <div class="row"><input type="number" min=${x.min} max=${x.max} value=${t} aria-label=${x.label} style="width:7rem" onInput=${(e) => setT(e.target.value)} />
    <button class="btn sm" disabled=${busy || !ok || t === v} onClick=${() => save(n)}>Save</button></div></div>`;
}
function TextSetting({ x, v, busy, save, fromEnv, envName }) {
  const [t, setT] = useState(v || "");
  return html`<div class="li"><div class="t"><b>${x.label}</b><span>${x.help}</span>${fromEnv && html`<span class="mut sm" role="note">Set by environment (${envName}): that value is used, this saved setting is ignored.</span>`}</div>
    <div class="row">${x.kind === "html" ? html`<textarea class="field" rows="3" maxlength=${x.max} value=${t} aria-label=${x.label} style="width:min(32rem,100%)" onInput=${(e) => setT(e.target.value)} />` : html`<input class="field" type=${x.kind === "email" ? "email" : x.kind === "url" ? "url" : "text"} maxlength=${x.max} value=${t} aria-label=${x.label} style="width:min(16rem,100%)" onInput=${(e) => setT(e.target.value)} />`}
    <button class="btn sm" disabled=${busy || t.trim() === (v || "")} onClick=${() => save(t.trim())}>Save</button></div></div>`;
}
const ENV_NAME = { cli_git_url: "AIHUB_CLI_GIT_URL", cli_git_branch: "AIHUB_CLI_GIT_BRANCH", cli_git_subdir: "AIHUB_CLI_GIT_SUBDIR", langfuse_host: "LANGFUSE_BASE_URL",
  langfuse_public_key: "LANGFUSE_PUBLIC_KEY", index_url: "AIHUB_INDEX_URL", index_branch: "AIHUB_INDEX_BRANCH", index_path: "AIHUB_INDEX_PATH" };
function LogoSetting({ url, bump }) {
  const f = useRef(null); const [busy, setBusy] = useState(false);
  const pick = async (e) => { const file = e.target.files[0]; e.target.value = ""; if (!file) return; setBusy(true);
    try { const blob = file.type === "image/png" || file.type === "image/jpeg" || file.type === "image/webp" ? file : await toAvatarBlob(file);
      const r = await fetch("/api/v1/admin/logo", { method: "POST", headers: { Authorization: "Bearer " + tok() }, body: blob });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Upload failed"); toast("Logo updated"); await loadBrand(); bump(); } catch (x) { toast(x.message, 1); } finally { setBusy(false); } };
  const rm = async () => { try { await api("/admin/logo", { method: "DELETE" }); toast("Logo removed"); await loadBrand(); bump(); } catch (x) { toast(x.message, 1); } };
  return html`<div class="li"><div class="t"><b>Logo</b><span>PNG, JPEG or WebP, up to 256 KB and 1024 px. Shown beside the site name.</span></div>
    <div class="row">${url && html`<img class="logo" src=${url} alt="Current logo" />`}<button class="btn sec sm" disabled=${busy} onClick=${() => f.current.click()}>${url ? "Change" : "Upload"}</button>
    ${url && html`<button class="btn danger sm" onClick=${rm}>Remove</button>`}<input ref=${f} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange=${pick} /></div></div>`;
}
function AdminSettings() {
  const [tick, setTick] = useState(0); const d = useLoad(() => api("/admin/settings"), [tick]); const [busy, setBusy] = useState("");
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const set1 = async (k, v) => { setBusy(k); if (k === "cli_endpoint" && v) toast("Checking endpoint…"); try { await api("/admin/settings", { method: "PUT", body: { [k]: v } }); toast("Saved"); if (k === "theme_color") loadBrand(); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const sections = [["Site", ["site_name", "theme_color", "cli_endpoint", "contact_name", "contact_email", "contact_url", "contact_phone", "announcement"]], ["Access", ["signup", "public_browse", "public_install"]], ["Repositories", ["allow_private", "default_visibility"]], ["Groups", ["allow_group_creation"]]];
  const by = Object.fromEntries(d.d.schema.map((x) => [x.key, x]));
  const warn = d.d.values.public_install === "1";
  return html`<div class="stack"><div class="card row"><b>Server version</b><span class="mono">v${d.d.server_version}</span><span class="sp"></span><span class="xs mut3">CLIs on this hub self-update to this version (<code>aihub upgrade</code>)</span></div>${sections.map(([title, keys]) => html`<div class="stack" key=${title}><h3>${title}</h3><div class="list">${title === "Site" && html`<${LogoSetting} url=${d.d.logo_url} bump=${() => setTick(tick + 1)} />`}${keys.filter((k) => by[k]).map((k) => { const x = by[k], v = d.d.values[k]; if (x.type === "text") return html`<${TextSetting} key=${k} x=${x} v=${v} busy=${busy === k} save=${(n) => set1(k, n)} />`; if (x.type === "number") return html`<${NumSetting} key=${k} x=${x} v=${v} busy=${busy === k} save=${(n) => set1(k, n)} />`; const onoff = x.options.length === 2 && x.options[0] === "0";
      return html`<div class="li" key=${k}><div class="t"><b>${x.label}</b><span>${x.help}</span></div>
        ${onoff ? html`<label class="switch"><input type="checkbox" role="switch" checked=${v === "1"} disabled=${busy === k} onChange=${(e) => set1(k, e.target.checked ? "1" : "0")} aria-label=${x.label} /><i></i></label>`
        : html`<div class="seg">${x.options.map((o) => html`<button key=${o} class=${v === o ? "on" : ""} disabled=${busy === k} onClick=${() => v !== o && set1(k, o)}>${o}</button>`)}</div>`}</div>`; })}</div></div>`)}
    ${warn && html`<div class="card" style="border:1px solid var(--orange)"><b>Public install is on.</b> <span class="mut">Anyone who can reach this server can download <u>public</u> packages without signing in. Private packages always require access.</span></div>`}</div>`;
}

const SCHEDULES = [["Every minute", "60"], ["Every 5 min", "*/5 * * * *"], ["Hourly", "0 * * * *"], ["Daily 03:00", "0 3 * * *"]];
function SecretField({ label, help, v, onSave }) {
  const [t, setT] = useState("");
  return html`<div class="li"><div class="t"><b>${label}</b><span>${help}</span></div><div class="row"><input class="field" type="password" autocomplete="off" placeholder=${v === "set" ? "•••••••• (set)" : "not set"} value=${t} aria-label=${label} style="width:min(16rem,100%)" onInput=${(e) => setT(e.target.value)} /><button class="btn sm" disabled=${!t} onClick=${async () => { await onSave(t); setT(""); }}>Save</button></div></div>`;
}
function AdminSync() {
  const [tick, setTick] = useState(0); const d = useLoad(() => api("/admin/sync"), [tick]); const [busy, setBusy] = useState("");
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const { config: c, status: st, from_env: env } = d.d;
  const save = async (k, v) => { setBusy(k); try { await api("/admin/sync", { method: "PUT", body: { [k]: v } }); toast("Saved"); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const run = async (full) => { setBusy("run"); try { toast(full ? "Full re-sync…" : "Syncing…"); await api("/admin/sync/run" + (full ? "?full=true" : ""), { method: "POST" }); toast("Sync finished"); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const T = (k, label, help, ph) => html`<${TextSetting} x=${{ label, help, kind: "text", max: 300 }} v=${c[k]} busy=${busy === k} save=${(n) => save(k, n)} key=${k} fromEnv=${env && env[k]} envName=${ENV_NAME[k]} />`;
  const when = (t) => t ? new Date(t * 1000).toLocaleString() : "never";
  return html`<div class="stack">
    <div class="card stack"><div class="row"><h3>Sync status</h3><span class="sp"></span><button class="btn sec sm" disabled=${busy === "run"} onClick=${() => run(false)}>Sync now</button><button class="btn sec sm" disabled=${busy === "run"} onClick=${() => run(true)} title="Forget the cursor and look back over everything; duplicates are skipped">Full re-sync</button></div>
      <div class="kpis"><div class="stat"><b>${st.events_total || 0}</b><span>events this run</span></div><div class="stat"><b>${st.index_packages || 0}</b><span>packages in index</span></div><div class="stat"><b>${st.next_in != null ? st.next_in + "s" : "-"}</b><span>next sync in</span></div></div>
      <p class="mut sm">Last run: ${when(st.last_run)}${st.last_error ? html` · <b style="color:var(--red)">${st.last_error}</b>` : html` · <span style="color:var(--green)">OK</span>`}</p></div>
    <div class="stack"><h3>Schedule</h3><div class="list"><div class="li"><div class="t"><b>Pull interval</b><span>Seconds (10-86400) or a cron expression: <code>*/5 * * * *</code> every 5 min, <code>0 3 * * *</code> daily at 03:00. Applies immediately.</span></div>
      <div class="row"><input class="field" id="sched" defaultValue=${c.sync_interval} style="width:11rem" aria-label="Pull interval" /><button class="btn sm" disabled=${busy === "sync_interval"} onClick=${() => save("sync_interval", document.getElementById("sched").value)}>Save</button></div></div>
      <div class="li"><div class="t"><b>Presets</b></div><div class="seg">${SCHEDULES.map(([n, v]) => html`<button key=${v} class=${c.sync_interval === v ? "on" : ""} onClick=${() => save("sync_interval", v)}>${n}</button>`)}</div></div></div></div>
    <div class="stack"><h3>Langfuse</h3><div class="list">${env.langfuse && html`<div class="li mut">Keys are set in the server environment, and those win over the values below.</div>`}
      ${T("langfuse_host", "Host", "e.g. https://us.cloud.langfuse.com")}${T("langfuse_public_key", "Public key", "pk-lf-…")}
      <${SecretField} label="Secret key" help="sk-lf-… Write-only: never shown again." v=${c.langfuse_secret_key} onSave=${(v) => save("langfuse_secret_key", v)} /></div></div>
    <div class="stack"><h3>Package index</h3><div class="list">${T("index_url", "Git URL", "Repository holding the package index")}${T("index_branch", "Branch", "default: main")}${T("index_path", "Folder", "default: index (a folder index; an old index.json file also works)")}</div></div>
    <div class="stack"><h3>CLI setup</h3><div class="list">
      <div class="li"><div class="t"><b>Share Langfuse credentials with CLIs</b><span>When on, <code>aihub setup</code> hands the Langfuse keys (never git credentials) to signed-in users and to anyone with the enrollment code. Sent over HTTPS only; every hand-out is audited.</span></div><label class="switch"><input type="checkbox" role="switch" checked=${c.share_credentials === "1"} disabled=${busy === "share_credentials"} onChange=${(e) => save("share_credentials", e.target.checked ? "1" : "0")} aria-label="Share credentials" /><i></i></label></div>
      <${SecretField} label="Enrollment code" help="Give this to teammates: aihub setup --code <code>. Leave unset to share only with signed-in users." v=${c.enroll_code} onSave=${(v) => save("enroll_code", v)} />
      ${T("cli_git_url", "CLI git URL", "Where to pip install the CLI from when this server is unreachable")}${T("cli_git_branch", "Branch (optional)", "Leave empty to hide. Install from this branch instead of the default")}${T("cli_git_subdir", "Sub folder (optional)", "Leave empty to hide. Install from this folder of the repo")}${T("client_refresh_hours", "CLI refresh (hours)", "How often installed CLIs re-pull these settings (default 24)")}</div></div></div>`;
}
function Groups() {
  const { me } = useStore(); const [tick, setTick] = useState(0); const bump = () => setTick((x) => x + 1);
  const caps = useLoad(() => me ? api("/groups/capabilities") : Promise.resolve(null), [me && me.username, tick]);
  const d = useLoad(() => me ? api("/groups") : Promise.resolve({ groups: [] }), [me && me.username, tick]);
  const [sel, setSel] = useState(null); const [nn, setNn] = useState(""); const [nd, setNd] = useState("");
  if (!me) return html`<${Empty} t="Sign in required" d="Sign in to see your groups." />`;
  if (!d.d) return d.e ? html`<${Err} e=${d.e} />` : html`<div class="skel"></div>`;
  const c = caps.d || {};
  const create = async () => { try { await api("/groups", { method: "POST", body: { name: nn, description: nd } }); setSel(nn.trim().toLowerCase()); setNn(""); setNd(""); toast("Group created"); bump(); } catch (e) { toast(e.message, 1); } };
  const manageable = (g) => g.can_manage;
  return html`<div class="stack"><h1>Groups</h1><p class="lead">${c.is_admin ? "All groups on this hub." : "Groups you own or belong to."} Share repositories with a group and every member gets access.</p>
    <div class="two" style="align-items:start"><div class="stack"><div class="list">${d.d.groups.length ? d.d.groups.map((g) => html`<button class="li pick" key=${g.name} onClick=${() => setSel(g.name)} style=${sel === g.name ? "background:var(--fill)" : ""}><span class="av" style="width:36px;height:36px;background:var(--fg-3)">👥</span><div class="t"><b>${g.name}</b><span>${g.members} ${g.members === 1 ? "member" : "members"} · ${g.packages} shared ${g.packages === 1 ? "repo" : "repos"}${manageable(g) ? "" : " · member"}</span></div></button>`)
        : html`<div class="li mut">${c.can_create ? "No groups yet - create your first one." : "You’re not in any groups."}</div>`}</div>
      ${c.can_create && html`<div class="card stack"><h3>New group</h3><input class="field" placeholder="name, e.g. data-team" value=${nn} onInput=${(e) => setNn(e.target.value)} /><input class="field" placeholder="Description (optional)" value=${nd} onInput=${(e) => setNd(e.target.value)} />
        <button class="btn" disabled=${!nn.trim()} onClick=${create}>Create group</button></div>`}</div>
      ${sel ? html`<${GroupDetail} name=${sel} key=${sel} bump=${bump} onGone=${() => setSel(null)} />` : html`<${Empty} t="Select a group" d="Pick a group to see its members." />`}</div></div>`;
}
function GroupDetail({ name, bump, onGone }) {
  const [tick, setTick] = useState(0); const g = useLoad(() => api("/groups/" + name), [name, tick]); const [confirm, setConfirm] = useState(false);
  const [paste, setPaste] = useState("");
  if (g.loading && !g.d) return html`<div class="skel"></div>`;
  if (g.e) return html`<${Empty} t="Members only see their own membership" d="Only the group’s owner or an admin can view and edit its member list." />`;
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
const KINDS = ["install", "update", "uninstall", "use", "error"];
const DAYS = [["1", "Last 24 hours"], ["7", "Last 7 days"], ["30", "Last 30 days"], ["90", "Last 90 days"], ["0", "All time"]];
function Audit() {
  const [f, setF] = useState({ source: "tools", q: "", actor: "", action: "", package: "", kind: "", days: "7", page: 1 }); const [q, setQ] = useState(""); const [open, setOpen] = useState(null);
  useEffect(() => { const t = setTimeout(() => setF((x) => (x.q === q ? x : { ...x, q, page: 1 })), 300); return () => clearTimeout(t); }, [q]);
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v != null).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
  const d = useLoad(() => api("/audit?" + qs({ ...f, per_page: 50 })), [JSON.stringify(f)]);
  if (!can("audit")) return html`<${Empty} t="No access" d="You need the audit permission to view this page." />`;
  const up = (k, v) => setF((x) => ({ ...x, [k]: v, page: 1 })); const tools = f.source === "tools";
  const exportCsv = async () => { try { const r = await fetch("/api/v1/audit/export?" + qs(f), { headers: { Authorization: "Bearer " + tok() } }); if (!r.ok) throw new Error("Export failed"); saveBlob(await r.blob(), `audit-${f.source}.csv`); } catch (e) { toast(e.message, 1); } };
  const rows = d.d ? d.d.items : [], total = d.d ? d.d.total : 0, pages = Math.max(1, Math.ceil(total / 50));
  const stamp = (t) => new Date(t * 1000).toLocaleString();
  return html`<div class="stack"><div class="row"><h1>Security audit</h1><span class="sp"></span><button class="btn sec sm" onClick=${exportCsv}>Export CSV</button></div>
    <div class="seg">${[["tools", "Tool calls & installs"], ["admin", "Admin actions"]].map(([k, l]) => html`<button key=${k} class=${f.source === k ? "on" : ""} onClick=${() => { setQ(""); setF({ source: k, q: "", actor: "", action: "", package: "", kind: "", days: f.days, page: 1 }); }}>${l}</button>`)}</div>
    <div class="card stack"><div class="row" style="flex-wrap:wrap;gap:10px"><div style="flex:2;min-width:220px"><${Search} value=${q} onInput=${setQ} ph=${tools ? "Search parameters, commands, paths, hosts, people" : "Search actor, action, target, detail"} /></div>
      <select class="field" aria-label="Time range" value=${f.days} onChange=${(e) => up("days", e.target.value)}>${DAYS.map(([v, l]) => html`<option value=${v} key=${v}>${l}</option>`)}</select>
      <select class="field" aria-label="Person" value=${f.actor} onChange=${(e) => up("actor", e.target.value)}><option value="">${tools ? "Anyone" : "Any actor"}</option>${(d.d ? d.d.actors : []).map((a) => html`<option value=${a} key=${a}>${a}</option>`)}</select>
      ${tools && html`<select class="field" aria-label="Event" value=${f.kind} onChange=${(e) => up("kind", e.target.value)}><option value="">Any event</option>${KINDS.map((k) => html`<option value=${k} key=${k}>${k}</option>`)}</select>
        <input class="field" style="max-width:170px" placeholder="Package" value=${f.package} onInput=${(e) => up("package", e.target.value.trim())} />`}
      <input class="field" style="max-width:190px" placeholder=${tools ? "Tool / component" : "Action, e.g. user.role"} value=${f.action} onInput=${(e) => up("action", e.target.value)} /></div>
      <span class="xs mut3">${d.loading && !d.d ? "Loading…" : `${fmt(total)} ${total === 1 ? "record" : "records"}`}${tools ? ". Parameters are scrubbed of passwords, tokens and keys before they leave the machine. “(local)” = not signed in; the name is the OS user on that device and is self-reported." : ""}</span></div>
    ${d.e ? html`<${Err} e=${d.e} />` : html`<div class="list tscroll"><table><thead><tr><th>When</th><th>${tools ? "Who" : "Actor"}</th><th>${tools ? "Event" : "Action"}</th><th>${tools ? "Tool call" : "Target"}</th><th></th></tr></thead><tbody>
      ${rows.map((x) => { const k = x.id + f.source; return html`<tr key=${k} style="cursor:pointer" onClick=${() => setOpen(open === k ? null : k)}>
        <td class="mut sm" title=${stamp(x.ts)}>${ago(x.ts)}</td><td>${x.actor}${tools && x.host ? html`<div class="xs mut3">${x.host}</div>` : ""}</td>
        <td>${tools ? html`<span class=${"badge " + (x.kind === "error" ? "red" : x.kind === "use" ? "" : "green")}>${x.kind}</span>` : html`<code>${x.action}</code>`}</td>
        <td class="mut">${tools ? html`<b style="font-weight:500">${x.package}</b>${x.component ? html` · <code>${x.component}</code>` : ""}${x.detail ? html`<div class="xs mono" style="max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${x.detail}</div>` : ""}` : `${x.target} ${x.detail}`}</td>
        <td class="mut3 xs">${open === k ? "▾" : "▸"}</td></tr>${open === k && html`<tr key=${k + "d"}><td colspan="5"><div class="stack" style="padding:6px 0"><div class="xs mut">${stamp(x.ts)}${tools ? ` · source ${x.source || "cli"} · ip ${x.ip || "?"} · client ${String(x.client_id || "").slice(0, 8)}${x.cwd ? " · cwd " + x.cwd : ""}${x.username ? "" : x.local_user ? ` · OS user ${x.local_user} (unverified)` : ""}` : ""}</div>
          <pre class="mono xs" style="white-space:pre-wrap;word-break:break-all;margin:0">${tools ? (x.detail ? x.detail : "No parameters recorded.") : x.detail || "—"}</pre></div></td></tr>`}`; })}
      ${!rows.length && !d.loading && html`<tr><td colspan="5" class="mut">Nothing matches these filters.</td></tr>`}</tbody></table></div>`}
    <div class="row"><button class="btn sec sm" disabled=${f.page <= 1} onClick=${() => setF((x) => ({ ...x, page: x.page - 1 }))}>Previous</button><span class="xs mut3">Page ${f.page} of ${pages}</span><button class="btn sec sm" disabled=${f.page >= pages} onClick=${() => setF((x) => ({ ...x, page: x.page + 1 }))}>Next</button></div></div>`;
}

function Docs({ slug }) {
  const list = useLoad(() => api("/docs"), []); const cur = slug || (list.d && list.d.docs[0] && list.d.docs[0].slug);
  const d = useLoad(() => cur ? api("/docs/" + cur) : Promise.resolve(null), [cur]);
  return html`<div class="stack"><h1>Docs</h1><div class="row" style="align-items:flex-start;gap:28px">
    <nav class="list" style="min-width:200px;flex:0 0 220px" aria-label="Docs">${(list.d ? list.d.docs : []).map((x) => html`<a class="li" key=${x.slug} href=${"#/docs/" + x.slug} style=${x.slug === cur ? "font-weight:600" : ""}>${x.title}</a>`)}</nav>
    <div class="card md" style="flex:1;min-width:280px" dangerouslySetInnerHTML=${{ __html: (d.d && d.d.html) || (d.loading ? "" : "<p class='mut'>No documentation found.</p>") }}></div></div></div>`;
}


// ---------- dashboard (charts are plain SVG: no library, theme-aware)
const KC = { use: "var(--blue)", install: "var(--green)", update: "var(--orange)", error: "var(--red)", uninstall: "var(--fg-3)", publish: "var(--fg-2)" };
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

const DASH_KEYS = ["package", "type", "user", "source", "kind", "identity", "host", "q"];
const DASH_LABEL = { package: "Package", type: "Type", user: "Person", source: "Tool", kind: "Event", identity: "Identity", host: "Device", q: "Search" };
const DASH_KINDS = ["use", "install", "update", "uninstall", "error", "publish"];
const dashParams = () => { const q = new URLSearchParams((store.route.split("?")[1]) || ""); const o = { days: +q.get("days") || 30, from: q.get("from") || "", to: q.get("to") || "", tab: q.get("tab") || "overview", page: +q.get("page") || 1 }; DASH_KEYS.forEach((k) => (o[k] = q.get(k) || "")); return o; };
const dashHash = (o) => { const q = new URLSearchParams(); if (o.from || o.to) { if (o.from) q.set("from", o.from); if (o.to) q.set("to", o.to); } else if (o.days !== 30) q.set("days", o.days); DASH_KEYS.forEach((k) => o[k] && q.set(k, o[k])); if (o.tab !== "overview") q.set("tab", o.tab); if (o.page > 1) q.set("page", o.page); const t = q.toString(); return "/dashboard" + (t ? "?" + t : ""); };

function Heatmap({ grid }) {
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], mx = Math.max(1, ...grid.flat());
  const cells = [];                                   // flat list (no fragments): label, then 24 hour cells per weekday
  grid.forEach((row, d) => { cells.push(html`<span key=${"l" + d} class="xs mut3">${days[d]}</span>`); row.forEach((v, h) => cells.push(html`<i key=${d + "-" + h} title=${`${days[d]} ${String(h).padStart(2, "0")}:00 UTC: ${v}`} style=${`background:color-mix(in srgb,var(--blue) ${v ? 14 + Math.round(v / mx * 76) : 0}%,var(--fill))`}></i>`)); });
  return html`<div class="heat" role="img" aria-label="Activity by weekday and hour, UTC"><span></span>${[0, 6, 12, 18].map((h) => html`<span key=${h} class="xs mut3" style=${`grid-column:${h + 2} / span 6`}>${String(h).padStart(2, "0")}:00</span>`)}${cells}</div>`;
}

function Dashboard() {
  const { me } = useStore(); const f = dashParams(); const [tick, setTick] = useState(0); const [open, setOpen] = useState(false);
  const go = (patch) => nav(dashHash({ ...f, page: 1, ...patch }));
  const ser = new URLSearchParams(); if (f.from || f.to) { f.from && ser.set("from", f.from); f.to && ser.set("to", f.to); } ser.set("days", f.days); DASH_KEYS.forEach((k) => f[k] && ser.set(k, f[k]));
  const qs = ser.toString();
  const d = useLoad(() => me && can("view_dashboard") ? api("/dashboard?" + qs) : Promise.resolve(null), [me, qs, tick]);
  const log = useLoad(() => me && can("view_dashboard") && f.tab === "activity" ? api("/dashboard/events?per_page=25&page=" + f.page + "&" + qs) : Promise.resolve(null), [me, qs, f.tab, f.page, tick]);
  const pk = useLoad(() => api("/packages?per_page=100&sort=name"), []);
  const [series, setSeries] = useState({ use: true, install: true, error: true, update: false, publish: false });
  const [qd, setQd] = useState(f.q); useEffect(() => setQd(f.q), [f.q]);
  useEffect(() => { const t = setInterval(() => setTick((x) => x + 1), 60000); return () => clearInterval(t); }, []);
  if (!me) return html`<${Empty} t="Sign in required" d="Sign in to view the dashboard." />`;
  if (!can("view_dashboard")) return html`<${Empty} t="No access" d="Your role doesn’t include the usage dashboard. Ask an admin to grant “View the usage dashboard”." />`;
  const D = d.d, t = (D && D.totals) || {}, pv = (D && D.previous) || {}, o = (D && D.options) || {};
  const custom = !!(f.from || f.to), active = DASH_KEYS.filter((k) => f[k]);
  const sel = (k, label, opts, any) => html`<select class="field" aria-label=${label} value=${f[k]} onChange=${(e) => go({ [k]: e.target.value })}><option value="">${any}</option>${opts.map((x) => html`<option value=${x} key=${x} selected=${x === f[k]}>${x}</option>`)}</select>`;
  const exportCsv = () => { const ks = ["install", "use", "update", "uninstall", "error", "publish"]; saveBlob(new Blob([["date," + ks.join(","), ...D.daily.map((x) => [x.date, ...ks.map((k) => x[k] || 0)].join(","))].join("\n")], { type: "text/csv" }), `usage-${D.filters.from}_${D.filters.to}.csv`); };
  const seriesOn = Object.keys(series).filter((k) => series[k]);
  return html`<div class="stack"><div class="row"><h1>Dashboard</h1><span class="sp"></span>
      <div class="seg" role="group" aria-label="Period">${[7, 30, 90, 365].map((n) => html`<button key=${n} class=${!custom && f.days === n ? "on" : ""} onClick=${() => go({ days: n, from: "", to: "" })}>${n === 365 ? "1y" : n + "d"}</button>`)}<button class=${custom ? "on" : ""} onClick=${() => setOpen(!open)} aria-expanded=${open}>Custom</button></div>
      <button class="btn sec sm" onClick=${() => setTick(tick + 1)} title="Reload now">Refresh</button><button class="btn sec sm" onClick=${exportCsv} disabled=${!D}>Export CSV</button></div>
    ${(open || custom) && html`<div class="row filters" role="group" aria-label="Date range"><label class="sm mut" for="df">From</label><input id="df" class="field" type="date" value=${f.from || (D && D.filters.from) || ""} max=${D && D.filters.to} onChange=${(e) => go({ from: e.target.value, to: f.to || (D && D.filters.to) || "" })} /><label class="sm mut" for="dt">To</label><input id="dt" class="field" type="date" value=${f.to || (D && D.filters.to) || ""} onChange=${(e) => go({ to: e.target.value, from: f.from || (D && D.filters.from) || "" })} />${custom && html`<button class="btn sec sm" onClick=${() => go({ from: "", to: "" })}>Back to presets</button>`}</div>`}
    <div class="row filters" role="group" aria-label="Filters">
      <form class="row" style="gap:6px" onSubmit=${(e) => { e.preventDefault(); go({ q: qd.trim() }); }}><input class="field" type="search" placeholder="Search package or component" aria-label="Search package or component" value=${qd} onInput=${(e) => setQd(e.target.value)} style="width:15rem" /></form>
      <select class="field" aria-label="Package" value=${f.package} onChange=${(e) => go({ package: e.target.value })}><option value="">All packages</option>${(pk.d ? pk.d.items : []).map((p) => html`<option value=${p.name} key=${p.name} selected=${p.name === f.package}>${p.name}</option>`)}</select>
      ${sel("type", "Type", o.types || [], "Any type")}${sel("kind", "Event", DASH_KINDS, "Any event")}${sel("source", "Tool", o.sources || [], "Any tool")}
      <select class="field" aria-label="Identity" value=${f.identity} onChange=${(e) => go({ identity: e.target.value })}><option value="">Anyone</option><option value="signed-in" selected=${f.identity === "signed-in"}>Signed-in only</option><option value="anonymous" selected=${f.identity === "anonymous"}>Anonymous only</option></select>
      ${sel("user", "Person", o.users || [], "Any person")}${sel("host", "Device", o.hosts || [], "Any device")}</div>
    ${active.length > 0 && html`<div class="row" style="gap:8px" aria-label="Active filters">${active.map((k) => html`<button key=${k} class="chip" onClick=${() => go({ [k]: "" })} aria-label=${`Remove ${DASH_LABEL[k]} filter`}>${DASH_LABEL[k]}: <b>${f[k]}</b> ✕</button>`)}<button class="btn sec sm" onClick=${() => go(Object.fromEntries(DASH_KEYS.map((k) => [k, ""])))}>Clear all</button></div>`}
    <div class="seg" role="tablist" style="align-self:flex-start">${[["overview", "Overview"], ["activity", "Activity"]].map(([k, l]) => html`<button key=${k} role="tab" aria-selected=${f.tab === k} class=${f.tab === k ? "on" : ""} onClick=${() => go({ tab: k })}>${l}</button>`)}</div>
    ${d.loading && !D ? html`<${Skels} n=4 />` : d.e ? html`<${Err} e=${d.e} />` : D && f.tab === "overview" && html`<div class="stack">
      <div class="kpis"><${Kpi} label="Tool calls" value=${t.use || 0} prev=${pv.use || 0} /><${Kpi} label="Active people" value=${D.active_users} prev=${D.active_users_prev} />
        <${Kpi} label="Installs" value=${t.install || 0} prev=${pv.install || 0} /><${Kpi} label="Publishes" value=${t.publish || 0} prev=${pv.publish || 0} /><${Kpi} label="Errors" value=${t.error || 0} prev=${pv.error || 0} invert=${true} /></div>
      <div class="card stack"><div class="row"><h3>Activity</h3><span class="sp"></span>
        <div class="row" style="gap:6px">${Object.keys(series).map((k) => html`<button key=${k} class=${"chip" + (series[k] ? " on" : "")} aria-pressed=${series[k]} onClick=${() => setSeries({ ...series, [k]: !series[k] })}><i class="dot" style=${`background:${KC[k]}`}></i>${k}</button>`)}</div></div>
        <p class="xs mut3">${D.filters.from} to ${D.filters.to} · ${D.active_packages} active packages · ${D.clients} devices · UTC</p>
        ${Object.values(t).some((v) => v) && seriesOn.length ? html`<${LineChart} daily=${D.daily} series=${seriesOn} />` : html`<${Empty} t=${seriesOn.length ? "No activity in this view" : "Pick a series"} d=${seriesOn.length ? "Widen the period or clear a filter." : "Turn on at least one series above."} />`}</div>
      <div class="two"><div class="card stack"><h3>Most used packages</h3><${Bars} items=${D.top_packages} link=${true} /></div>
        <div class="card stack"><h3>Most used components</h3><${Bars} items=${(D.top_components || []).map((x) => ({ ...x, name: x.name }))} color="var(--green)" /></div></div>
      <div class="card stack"><div class="row"><h3>When people work</h3><span class="sp"></span><span class="xs mut3">weekday by hour, UTC</span></div><${Heatmap} grid=${D.heatmap} /></div>
      <div class="two"><div class="card stack"><h3>Top people</h3><${Bars} items=${D.top_users} color="var(--green)" /></div><div class="card stack"><h3>Devices</h3><${Bars} items=${D.by_host || []} color="var(--orange)" /></div></div>
      <div class="three"><div class="card stack"><h3>By type</h3><${Donut} items=${D.by_type} /></div><div class="card stack"><h3>By tool</h3><${Donut} items=${D.by_source} /></div><div class="card stack"><h3>Signed in vs anonymous</h3><${Donut} items=${D.by_identity || []} /></div></div>
      <div class="card stack"><div class="row"><h3>Top rated</h3><span class="sp"></span><span class="xs mut3">all time</span></div>${D.top_reviewed.length ? html`<div>${D.top_reviewed.slice(0, 8).map((x) => html`<div class="hb" key=${x.name}><span class="hl"><a href=${"#/package/" + x.name}>${x.name}</a></span><span class="mut sm">${x.reviews} ${x.reviews === 1 ? "review" : "reviews"}</span><b>${x.avg}</b></div>`)}</div>` : html`<p class="mut sm">No reviews yet.</p>`}</div></div>`}
    ${f.tab === "activity" && (log.loading && !log.d ? html`<${Skels} n=2 />` : log.e ? html`<${Err} e=${log.e} /> ` : log.d && html`<div class="card stack"><div class="row"><h3>Activity log</h3><span class="sp"></span><span class="xs mut3">${log.d.total} events</span></div>
      ${log.d.items.length ? html`<div class="tscroll"><table><thead><tr><th>When</th><th>Who</th><th>Event</th><th>Package</th><th>Component</th><th>Tool</th><th>Device</th></tr></thead><tbody>
        ${log.d.items.map((e, i) => html`<tr key=${i}><td class="mut sm" title=${new Date(e.ts * 1000).toISOString()}>${ago(e.ts)}</td><td>${e.actor}</td><td><span class=${"badge " + ({ use: "blue", install: "green", error: "red", update: "orange" }[e.kind] || "")}>${e.kind}</span></td><td><a href=${"#/package/" + e.package}>${e.package}</a>${e.version ? html` <span class="mut sm">${e.version}</span>` : ""}</td><td class="mono sm">${e.component || ""}</td><td class="sm">${e.source}</td><td class="sm mut">${e.host || ""}</td></tr>`)}</tbody></table></div>
        <div class="row"><button class="btn sec sm" disabled=${f.page <= 1} onClick=${() => nav(dashHash({ ...f, page: f.page - 1 }))}>Previous</button><span class="sm mut">Page ${log.d.page} of ${Math.max(1, Math.ceil(log.d.total / log.d.per_page))}</span><button class="btn sec sm" disabled=${f.page * log.d.per_page >= log.d.total} onClick=${() => nav(dashHash({ ...f, page: f.page + 1 }))}>Next</button></div>`
        : html`<${Empty} t="No events match" d="Widen the period or clear a filter." />`}</div>`)}</div>`;
}

// ---------- shell
function App() {
  const s = useStore(); const path = s.route.split("?")[0], seg = path.split("/").filter(Boolean);
  useEffect(() => { loadMe(); loadBrand(); }, []);
  const lk = useRef(), ind = useRef();
  const place = () => { const n = lk.current, i = ind.current; if (!n || !i) return; const a = n.querySelector("a.on"); if (!a) { i.style.opacity = 0; return; } i.style.opacity = 1; i.style.transform = `translateX(${a.offsetLeft}px) scaleX(${a.offsetWidth})`; };
  useEffect(() => { place(); }, [path]);
  useEffect(() => { addEventListener("resize", place); return () => removeEventListener("resize", place); }, []);
  const logout = async () => { try { await api("/auth/logout", { method: "POST" }); } catch {} try { localStorage.removeItem("aihub_token"); } catch {} set({ me: null }); nav("/"); };
  const on = (k) => (seg[0] === k ? "on" : "");
  const br = s.brand || {}, ct = br.contact || {};
  const page = { "": html`<${Home} />`, browse: html`<${Browse} key=${s.route} />`, package: html`<${Package} name=${seg[1]} key=${seg[1]} />`, rankings: html`<${Rankings} />`, docs: html`<${Docs} slug=${seg[1]} key=${seg[1]} />`, start: html`<${Start} />`,
    login: html`<${Auth} mode="login" />`, register: html`<${Auth} mode="register" />`, account: html`<${Account} />`, admin: html`<${Admin} />`, dashboard: html`<${Dashboard} />`, audit: html`<${Audit} />`, groups: html`<${Groups} />` }[seg[0] || ""] || html`<${Empty} t="Page not found" d="That page doesn’t exist." />`;
  return html`<div>${br.announcement && html`<div class="announce" role="status" dangerouslySetInnerHTML=${{ __html: br.announcement }}></div>`}<header class="nav"><div class="nav-in"><a class="brand glow" href="#/">${br.logo_url && html`<img class="logo" src=${br.logo_url} alt="" />`}<span>${br.name || "AI Hub"}</span></a>
    <nav class="links" aria-label="Main" ref=${lk}><i class="ind" ref=${ind}></i><a class=${on("browse")} href="#/browse">Browse</a><a class=${on("rankings")} href="#/rankings">Rankings</a><a class=${on("start")} href="#/start">Get started</a><a class=${on("docs")} href="#/docs">Docs</a></nav>
    <div class="r">${s.me ? html`<a href="#/account" class="me-link"><${Avatar} p=${s.me} size=${24} /><span>${shown(s.me)}</span></a>${s.showGroups ? html`<a class=${on("groups")} href="#/groups">Groups</a>` : ""}${can("view_dashboard") ? html`<a href="#/dashboard">Dashboard</a>` : ""}${can("audit") ? html`<a class=${on("audit")} href="#/audit">Audit</a>` : ""}${can("admin") || can("reset_password") ? html`<a href="#/admin">Admin</a>` : ""}<a href="#/" onClick=${logout}>Sign out</a>` :
      html`<a href="#/login">Sign in</a>`}</div></div></header>
    <main><div class="page" key=${path}>${page}</div></main>
    <footer class="foot"><b class="glow">${br.logo_url && html`<img class="logo sm" src=${br.logo_url} alt="" />`}${br.name || "AI Hub"}</b><span>Skills, agents and MCP servers for your team.</span>
      ${(ct.name || ct.email || ct.url || ct.phone) && html`<span class="contact" aria-label="Contact">${ct.name && html`<span>${ct.name}</span>`}${ct.email && html`<a href=${"mailto:" + ct.email}>${ct.email}</a>`}${ct.phone && html`<span>${ct.phone}</span>`}${ct.url && /^https?:\/\//i.test(ct.url) && html`<a href=${ct.url} target="_blank" rel="noopener noreferrer">Support</a>`}</span>`}<nav aria-label="Footer"><a href="#/browse">Browse</a><a href="#/rankings">Rankings</a><a href="#/start">Get started</a><a href="#/docs">Docs</a></nav>${br.server_version && html`<span class="ver xs">v${br.server_version}</span>`}</footer>
    ${s.toast && html`<div class=${"toast " + (s.toast.err ? "err" : "")} role="status">${s.toast.m}</div>`}</div>`;
}
render(html`<${App} />`, document.getElementById("app"));

// ambient dot field: ease the highlight toward the pointer; the loop stops itself once it has caught up
(() => {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const root = document.documentElement.style; let tx = innerWidth / 2, ty = innerHeight * 0.3, x = tx, y = ty, raf = 0;
  const step = () => { x += (tx - x) * 0.14; y += (ty - y) * 0.14;
    root.setProperty("--bx", x.toFixed(1) + "px"); root.setProperty("--by", y.toFixed(1) + "px");
    root.setProperty("--px", ((x - innerWidth / 2) * -0.02).toFixed(1) + "px"); root.setProperty("--py", ((y - innerHeight / 2) * -0.02).toFixed(1) + "px");
    raf = Math.abs(tx - x) + Math.abs(ty - y) > 0.5 ? requestAnimationFrame(step) : 0; };
  addEventListener("pointermove", (e) => { tx = e.clientX; ty = e.clientY; if (!raf) raf = requestAnimationFrame(step); }, { passive: true });
})();
