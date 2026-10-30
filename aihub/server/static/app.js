/* AI Hub web app. Preact + htm from CDN (no build step, no eval -> strict CSP). All data from /api/v1. */
const { html, render, useState, useEffect, useRef, useCallback } = window.htmPreact;

// ---------- api / state
const BASE = ((document.getElementById("base") || {}).content || "").replace(/\/+$/, ""); // path prefix behind a reverse proxy ("" when served at the root)
const U = (p) => BASE + p;
const tok = () => { try { return localStorage.getItem("aihub_token") || sessionStorage.getItem("aihub_token"); } catch { return null; } };
const keepToken = (t, remember) => { try { sessionStorage.removeItem("aihub_token"); localStorage.removeItem("aihub_token"); (remember ? localStorage : sessionStorage).setItem("aihub_token", t); } catch {} };
async function api(path, opt = {}) {
  const h = { "Content-Type": "application/json" };
  if (tok()) h.Authorization = "Bearer " + tok();
  const r = await fetch(U("/api/v1" + path), { ...opt, headers: h, body: opt.body ? JSON.stringify(opt.body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : r.statusText || "Request failed");
  return j;
}
const subs = new Set();
const store = { me: null, route: location.hash.slice(1) || "/", toast: null };
const set = (p) => { Object.assign(store, p); subs.forEach((f) => f({})); };
const useStore = () => { const [, f] = useState({}); useEffect(() => { subs.add(f); return () => subs.delete(f); }, []); return store; };
addEventListener("hashchange", () => { const old = store.route.split("?")[0], now = location.hash.slice(1) || "/"; set({ route: now }); if (old !== now.split("?")[0]) scrollTo(0, 0); });   // a filter / tab / group click on the same page keeps the scroll position
let tt; const toast = (m, err) => { set({ toast: { m, err } }); clearTimeout(tt); tt = setTimeout(() => set({ toast: null }), 3200); };
const FAVICON0 = (document.getElementById("favicon") || {}).href;
function setFavicon(url) {
  const l = document.getElementById("favicon"); if (!l) return;
  const put = (h, t) => { l.removeAttribute("type"); if (t) l.type = t; l.href = h; };
  if (!url) return put(FAVICON0);
  const im = new Image(); // fit the logo into a square canvas (keep ratio) so the tab does not squash it
  im.onload = () => { try {
    const w = im.naturalWidth || 1, h = im.naturalHeight || 1, S = 64, k = Math.min(S / w, S / h);
    const c = document.createElement("canvas"); c.width = c.height = S;
    c.getContext("2d").drawImage(im, (S - w * k) / 2, (S - h * k) / 2, w * k, h * k);
    put(c.toDataURL("image/png"), "image/png");
  } catch { put(url); } };
  im.onerror = () => put(FAVICON0);
  im.src = url;
}
async function loadBrand() { try { const m = await api("/meta"); set({ brand: m }); document.title = m.name; setFavicon(m.logo_url); document.documentElement.dataset.accent = m.theme || "blue"; } catch {} }
async function loadMe() { try { const me = tok() ? await api("/auth/me") : null; let showGroups = false; if (me) { try { showGroups = (await api("/groups/capabilities")).show_page; } catch {} } set({ me, showGroups }); } catch { try { localStorage.removeItem("aihub_token"); } catch {} set({ me: null }); } }
const initials = (n) => (String(n || "?").trim().split(/\s+/).map((w) => w[0]).slice(0, 2).join("") || "?").toUpperCase();
const hue = (n) => { let h = 0; for (const c of String(n)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
const shown = (p) => (p && (p.display_name || p.username)) || "";
const Avatar = ({ p, size = 32 }) => p && p.avatar_url ? html`<img decoding="async" class="av" src=${p.avatar_url} alt="" width=${size} height=${size} style=${`width:${size}px;height:${size}px`} loading="lazy" />`
  : html`<span class="av" aria-hidden="true" style=${`width:${size}px;height:${size}px;font-size:${Math.round(size * .4)}px;background:hsl(${hue(p && p.username)} 55% 45%)`}>${initials(shown(p))}</span>`;
const can = (perm) => !!(store.me ? store.me.permissions.includes(perm) : store.brand && (store.brand.anonymous_perms || []).includes(perm));
// Download through the API in parallel Range chunks (same idea as `aihub download`): the first request tells the size, the rest run 4 at a time.
async function fetchChunks(url, headers, onProgress, chunk = 4 << 20, workers = 4) {
  const bad = async (r) => { let m = r.statusText || "Download failed"; try { m = (await r.json()).detail || m; } catch (e) {} return new Error(m); };
  const first = await fetch(url, { headers: { ...headers, Range: `bytes=0-${chunk - 1}` } });
  if (!first.ok) throw await bad(first);
  const part0 = await first.blob(), cr = first.headers.get("Content-Range") || "", rev = first.headers.get("X-Aihub-Rev");
  const total = first.status === 206 && /\/(\d+)$/.test(cr) ? +cr.split("/")[1] : part0.size;
  if (first.status !== 206 || total <= part0.size) { onProgress && onProgress(1); return part0; }
  const n = Math.ceil(total / chunk), parts = new Array(n); parts[0] = part0; let done = part0.size, next = 1;
  onProgress && onProgress(done / total);
  const lane = async () => {
    while (next < n) {
      const i = next++, a = i * chunk, b = Math.min(total, a + chunk) - 1;
      for (let t = 0; ; t++) {
        try {
          const r = await fetch(url, { headers: { ...headers, Range: `bytes=${a}-${b}` } });
          if (r.status !== 206) throw await bad(r);
          if (rev && r.headers.get("X-Aihub-Rev") !== rev) throw new Error("The package changed while downloading; try again");
          const bl = await r.blob(); if (bl.size !== b - a + 1) throw new Error("Short chunk");
          parts[i] = bl; done += bl.size; onProgress && onProgress(done / total); break;
        } catch (e) { if (t >= 3) throw e; await new Promise((s) => setTimeout(s, 400 * 2 ** t)); }
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(workers, n - 1) }, lane));
  return new Blob(parts, { type: "application/gzip" });
}
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
const copy = async (t) => { const ok = await writeClip(t); ok ? toast("Copied") : toast("Copy failed", 1); return ok; };
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
  <input class="field" type="search" value=${value} placeholder=${ph} aria-label="Search" aria-keyshortcuts="/" onInput=${(e) => onInput(e.target.value)} /></div>`;
const Cmd = ({ text }) => { const [ok, setOk] = useState(false); const go = async () => { if (await copy(text)) { setOk(true); setTimeout(() => setOk(false), 1600); } }; return html`<div class="cmd"><code>${text}</code><button class="btn sec sm" aria-live="polite" onClick=${go}>${ok ? "Copied ✓" : "Copy"}</button></div>`; };
const Empty = ({ t, d }) => html`<div class="empty"><h3>${t}</h3><p>${d}</p></div>`;
const Err = ({ e }) => html`<${Empty} t="Something went wrong" d=${e} />`;
const Skels = ({ n = 6 }) => html`<div class="grid">${Array.from({ length: n }, (_, i) => html`<div class="skel" key=${i}></div>`)}</div>`;
const Stars = ({ r }) => r ? html`<span>★ ${r}</span>` : null;
const TYPE = { skill: "blue", agent: "green", mcp: "orange", tool: "", setup: "red", bot: "blue", service: "green" };
const Pkg = ({ p, i = 0 }) => html`<a class="card pkg" style=${"--i:" + Math.min(i, 12)} href=${"#/package/" + p.name}>
  <div class="row"><h3>${p.name}</h3><span class="sp"></span>${p.visibility === "private" && html`<span class="badge orange" title="Only people you share it with can see this">private</span>`}<span class=${"badge " + (TYPE[p.type] || "")}>${p.type}</span></div>
  <p>${p.description || "No description"}</p>
  <div class="meta"><span>v${p.latest_version || "–"}</span><span>↓ ${fmt(p.downloads)}</span><${Stars} r=${p.rating && p.rating.avg} /></div></a>`;

// ---------- pages
// ---------- motion helpers (all JS-driven motion is skipped under prefers-reduced-motion)
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const revealIO = "IntersectionObserver" in window && !reduced ? new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); revealIO.unobserve(e.target); } }), { rootMargin: "0px 0px -8% 0px", threshold: 0.08 }) : null;
// call once per page component: marks every .rv element visible as it scrolls into view
const useReveal = () => useEffect(() => { document.querySelectorAll(".rv:not(.in)").forEach((el) => (revealIO ? revealIO.observe(el) : el.classList.add("in"))); });

function Count({ to }) {
  const ref = useRef(), shown = useRef(null);       // shown = number on screen once revealed: a later change rolls from it, not from 0
  useEffect(() => {
    const el = ref.current, n = Number(to) || 0; if (!el) return;
    if (reduced || !("IntersectionObserver" in window)) { el.textContent = fmt(n); shown.current = n; return; }
    let raf; const roll = (a, ms) => { const t0 = performance.now(); el.classList.remove("bump"); if (shown.current !== null && n !== a) { void el.offsetWidth; el.classList.add("bump"); }
      const step = (t) => { const k = Math.min(1, (t - t0) / ms), x = Math.round(a + (n - a) * (1 - Math.pow(1 - k, 3))); el.textContent = fmt(x); shown.current = x; if (k < 1) raf = requestAnimationFrame(step); }; raf = requestAnimationFrame(step); };
    if (shown.current !== null) { roll(shown.current, 700); return () => cancelAnimationFrame(raf); }
    const ob = new IntersectionObserver((es) => { if (!es[0].isIntersecting) return; ob.disconnect(); roll(0, 900); });
    el.textContent = fmt(0); ob.observe(el); return () => { ob.disconnect(); cancelAnimationFrame(raf); };
  }, [to]);
  return html`<span ref=${ref}></span>`;           // no vdom children: the effect owns the text, so a re-render (e.g. the Copy toast) cannot touch it
}

const since = (t) => { const s = Date.now() / 1000 - t; return s < 45 ? "just now" : ago(t); };
const VERB = { install: "installed", use: "used", update: "updated", publish: "published" };
function Activity({ onNew }) {
  const [items, setItems] = useState(null), [err, setErr] = useState(null), [, tick] = useState(0); const seen = useRef(new Set());
  useEffect(() => {
    let on = 1;
    const load = async () => {
      if (document.hidden) return;
      try { const r = await api("/activity"); if (!on) return;
        const rows = r.items.map((i) => ({ ...i, k: i.package + i.kind + i.ts })); const first = seen.current.size === 0;
        const marked = rows.map((x) => ({ ...x, fresh: !first && !seen.current.has(x.k) })); setItems(marked); rows.forEach((x) => seen.current.add(x.k)); setErr(null);
        if (onNew && marked.some((x) => x.fresh)) onNew();
      } catch (e) { if (on) setErr(e.message); }
    };
    load(); const poll = setInterval(load, 20000), clock = setInterval(() => !document.hidden && tick((n) => n + 1), 30000);
    const vis = () => { if (!document.hidden) load(); }; document.addEventListener("visibilitychange", vis);
    return () => { on = 0; clearInterval(poll); clearInterval(clock); document.removeEventListener("visibilitychange", vis); };
  }, []);
  return html`<aside class="feed" aria-label="Live activity"><div class="feed-h"><h3>Happening now</h3><span class="live" title="Refreshes while this tab is open">live</span></div>
    ${err ? html`<p class="mut sm feed-e">Activity is unavailable right now.</p>` : !items ? html`<div class="feed-l">${[0, 1, 2, 3, 4].map((i) => html`<div class="skel line" key=${i}></div>`)}</div>` :
      !items.length ? html`<p class="mut sm feed-e">Nothing yet. Publishes, installs and usage show up here as they happen.</p>` :
      html`<ul class="feed-l">${items.slice(0, 6).map((x, n) => html`<li key=${x.k} class=${x.fresh ? "fresh" : ""} style=${"--i:" + n}>
        <span class=${"badge " + (TYPE[x.type] || "")}>${x.type}</span><a href=${"#/package/" + x.package}>${x.package}</a><span class="v">${VERB[x.kind] || x.kind}${x.kind === "publish" && x.version ? " " + x.version : ""}</span><time class="mut3">${since(x.ts)}</time></li>`)}</ul>`}</aside>`;
}

// pointer light on the carousel (fine pointers only): ONE listener, rAF-throttled, writes two CSS vars on the hovered .sheen
const fine = !reduced && matchMedia("(hover: hover) and (pointer: fine)").matches;
function useSheen() {
  const fn = useRef(null);
  if (fine && !fn.current) { let raf = 0, ev = null;
    fn.current = (e) => { ev = e; if (raf) return; raf = requestAnimationFrame(() => { raf = 0; const el = ev.target.closest && ev.target.closest(".sheen"); if (!el) return;
      const r = el.getBoundingClientRect(); el.style.setProperty("--mx", ((ev.clientX - r.left) / r.width * 100).toFixed(1) + "%"); el.style.setProperty("--my", ((ev.clientY - r.top) / r.height * 100).toFixed(1) + "%"); }); }; }
  return fn.current;
}
const TINT = { skill: "var(--blue)", agent: "var(--green)", mcp: "var(--orange)", tool: "var(--tint-d)", setup: "var(--red)", bot: "var(--tint-a)", service: "var(--tint-b)" };
const Glyph = ({ p, big }) => html`<span class=${"ft-ico" + (big ? " big" : "")} style=${"--tc:" + (TINT[p.type] || "var(--blue)")} aria-hidden="true">${(p.name || "?").replace(/^aihub-/, "").charAt(0).toUpperCase()}</span>`;
const FT_MS = 7000;
// featured packages (Admin > Settings > Home page): a snap-scroll carousel that advances when the active dot's fill animation ends,
// so pausing (hover, focus, hidden tab, off screen) is just animation-play-state; no timers. Older hubs without /highlights: nothing shown.
function Highlights() {
  const h = useLoad(() => api("/highlights").catch(() => null), []);
  const d = h.d, items = (d && d.items) || [];
  if (!items.length || d.style === "off") return null;
  const head = html`<div class="gap row rv"><h2>${d.pinned ? "Featured" : "Popular right now"}</h2><span class="sp"></span>${!d.pinned && html`<a href="#/browse?sort=downloads">See all</a>`}</div>`;
  if (d.style === "cards") return html`${head}<div class="ft-cards rv">${items.map((p, i) => html`<a key=${p.name} class="ft-card sheen" style=${"--i:" + i} href=${"#/package/" + p.name}>
    <${Glyph} p=${p} /><div class="ft-t"><b>${p.name}</b><span>${p.description || "No description"}</span></div><span class="mut3 xs">v${p.latest_version || "–"}</span></a>`)}</div>`;
  return html`${head}<${Slides} items=${items} />`;
}
function Slides({ items }) {
  const track = useRef(), box = useRef(), aim = useRef(-1); const [at, setAt] = useState(0), [hold, setHold] = useState(false), [off, setOff] = useState(false), [hid, setHid] = useState(document.hidden);
  const go = (i) => { const t = track.current; if (!t) return; const n = (i + items.length) % items.length, el = t.children[n]; if (el) { aim.current = n; t.scrollTo({ left: el.offsetLeft, behavior: reduced ? "auto" : "smooth" }); } setAt(n); };
  useEffect(() => {
    const t = track.current, b = box.current; if (!t || !("IntersectionObserver" in window)) return;
    // slides passed on the way to a target (go) are ignored until the target arrives; a swipe (no target) is followed as is
    const io = new IntersectionObserver((es) => es.forEach((e) => { if (!e.isIntersecting) return; const i = +e.target.dataset.i;
      if (aim.current < 0 || aim.current === i) { aim.current = -1; setAt(i); } }), { root: t, threshold: 0.6 });
    [...t.children].forEach((c) => io.observe(c));
    const vis = new IntersectionObserver(([e]) => setOff(!e.isIntersecting)); vis.observe(b);
    const tab = () => setHid(document.hidden); tab(); document.addEventListener("visibilitychange", tab);
    return () => { io.disconnect(); vis.disconnect(); document.removeEventListener("visibilitychange", tab); };
  }, [items.length]);
  const key = (e) => { if (e.key === "ArrowRight") { e.preventDefault(); go(at + 1); } else if (e.key === "ArrowLeft") { e.preventDefault(); go(at - 1); } };
  const paused = hold || off || hid || items.length < 2, sheen = useSheen();
  return html`<section ref=${box} class="ft rv" data-paused=${paused ? "1" : null} role="region" aria-roledescription="carousel" aria-label="Featured packages" tabindex="0" onKeyDown=${key} onPointerMove=${sheen}
      onPointerEnter=${() => setHold(true)} onPointerLeave=${() => setHold(false)} onFocusIn=${() => setHold(true)} onFocusOut=${(e) => !e.currentTarget.contains(e.relatedTarget) && setHold(false)}>
    <div class="ft-track" ref=${track} onPointerDown=${() => (aim.current = -1)} onWheel=${() => (aim.current = -1)} onTouchStart=${() => (aim.current = -1)}>${items.map((p, i) => html`<article key=${p.name} class=${"ft-slide sheen" + (i === at ? " on" : "")} data-i=${i} aria-roledescription="slide" aria-label=${(i + 1) + " of " + items.length + ": " + p.name} inert=${i !== at} style=${"--tc:" + (TINT[p.type] || "var(--blue)") + ";cursor:pointer"} onClick=${(e) => { if (!e.target.closest("a,button,input,.ft-cmd")) location.hash = "#/package/" + p.name; }}>
      <div class="ft-copy"><div class="row ft-eye"><span class=${"badge " + (TYPE[p.type] || "")}>${p.type}</span>${p.category && html`<span class="mut3 xs">${p.category}</span>`}</div>
        <h3><a href=${"#/package/" + p.name}>${p.name}</a></h3><p>${p.description || "No description"}</p>
        <div class="meta"><span>v${p.latest_version || "–"}</span><span>↓ ${fmt(p.downloads)}</span><${Stars} r=${p.rating && p.rating.avg} />${p.updated && html`<span>updated ${ago(p.updated)}</span>`}</div>
        <div class="ft-cmd"><${Cmd} text=${"aihub install " + p.name} /></div></div>
      <${Glyph} p=${p} big=${1} /></article>`)}</div>
    ${items.length > 1 && html`<div class="ft-bar"><button class="ft-arw" aria-label="Previous" onClick=${() => go(at - 1)}><svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 2 4 7l5 5"/></svg></button>
      <div class="ft-dots" role="group" aria-label="Choose slide">${items.map((p, i) => html`<button key=${p.name} class=${i === at ? "on" : ""} aria-label=${"Show " + p.name} aria-current=${i === at} onClick=${() => go(i)}>${i === at && html`<i style=${"animation-duration:" + FT_MS + "ms"} onAnimationEnd=${() => go(at + 1)}></i>`}</button>`)}</div>
      <button class="ft-arw" aria-label="Next" onClick=${() => go(at + 1)}><svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m5 2 5 5-5 5"/></svg></button></div>`}</section>`;
}

const catClass = (n) => ["a", "b", "c", "d", "e", "f"][n] || "x";
function Home() {
  const [q, setQ] = useState("");
  const m = useLoad(() => api("/meta"), []);
  const top = useLoad(() => api("/packages?sort=downloads&per_page=6"), []);
  const recent = useLoad(() => api("/packages?sort=updated&per_page=8"), []);
  const spo = useLoad(() => api("/spaces/overview").catch(() => null), []);
  const [bump, setBump] = useState(0);            // new events in the feed -> refresh the numbers (at most once per feed poll)
  const ov = useLoad(() => bump ? api("/stats/overview").catch(() => null) : Promise.resolve(null), [bump]);
  const o = ov.d || (m.d && m.d.overview) || {}; const sps = ((spo.d && spo.d.top) || []).slice(0, 6);
  const onNew = useCallback(() => setBump((n) => n + 1), []);
  useReveal();
  return html`<div class="home">
    <section class="hero"><div class="hero-copy"><h1>Install what your AI needs.</h1>
      <p class="lead">Find skills, agents and MCP servers for Claude Code, Codex and OpenCode, then install them with one command.</p>
      <form class="search" onSubmit=${(e) => { e.preventDefault(); nav("/browse?q=" + encodeURIComponent(q)); }}><${Search} value=${q} onInput=${setQ} /></form></div>
      <${Activity} onNew=${onNew} /></section>
    <${Highlights} />
    <div class="stats3 rv"><div class="stat" style=${"--n:"+Math.max(3,fmt(Number(o.packages)||0).length)}><b><${Count} to=${o.packages} /></b><span>Packages</span></div><div class="stat" style=${"--n:"+Math.max(3,fmt(Number(o.versions)||0).length)}><b><${Count} to=${o.versions} /></b><span>Releases</span></div>
      <div class="stat" style=${"--n:"+Math.max(3,fmt(Number(o.downloads)||0).length)}><b><${Count} to=${o.downloads} /></b><span>Downloads</span></div><div class="stat" style=${"--n:"+Math.max(3,fmt(Number(o.users)||0).length)}><b><${Count} to=${o.users} /></b><span>People</span></div></div>
    ${sps.length ? html`<div class="gap row rv"><h2>Space highlights</h2><span class="sp"></span><a href="#/spaces">All spaces</a></div>
    <div class="bento">${sps.map((s, n) => html`<a key=${s.ref} href=${"#/s/" + s.ref} class=${"tile rv t-" + catClass(n)} style=${"--d:" + n * 70 + "ms"}>
      <div class="tile-in"><span class="badge">${SKIND[s.kind] || s.kind}</span><h3>${s.title}</h3><p>${s.description || s.ref}</p></div>
      <span class="tile-n"><b>${fmt(s.views)}</b> views</span></a>`)}</div>` : null}
    <div class="gap row rv"><h2>Trending</h2><span class="sp"></span><a href="#/browse?sort=downloads">See all</a></div>
    <div style="margin-top:20px">${top.loading && !top.d ? html`<${Skels} n=3 />` : top.e ? html`<${Err} e=${top.e} />` :
      top.d.items.length ? html`<div class="grid">${top.d.items.map((p, i) => html`<${Pkg} p=${p} i=${i} key=${p.name} />`)}</div>` :
      html`<${Empty} t="Nothing here yet" d="Publish the first package with aihub dev publish." />`}</div>
    ${recent.d && recent.d.items.length ? html`<div class="gap rv"><h2>Recently updated</h2></div>
      <div class="strip rv">${recent.d.items.map((p) => html`<a class="chip-pkg" key=${p.name} href=${"#/package/" + p.name}><b>${p.name}</b><span>v${p.latest_version || "-"} / ${ago(p.updated)}</span></a>`)}</div>` : null}</div>`;
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
    <div class="chips" role="group" aria-label="Categories"><button class=${"chip " + (cat === "" ? "on" : "")} aria-pressed=${cat === ""} onClick=${() => setCat("")}>All</button>
      ${cats.map((c) => html`<button key=${c.id} class=${"chip " + (cat === c.id ? "on" : "")} aria-pressed=${cat === c.id} onClick=${() => setCat(c.id)}>${c.label} <i>${c.count}</i></button>`)}
      ${tag && html`<button class="chip on" onClick=${() => setTag("")} title="Remove tag filter">#${tag} x</button>`}</div>
    <div class="row"><div class="seg" role="group" aria-label="Package type">${types.map((t) => html`<button key=${t} class=${type === t ? "on" : ""} aria-pressed=${type === t} onClick=${() => setType(t)}>${t || "All types"}</button>`)}</div>
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

const PKT = { skill: "var(--blue)", agent: "var(--green)", mcp: "var(--orange)", tool: "var(--fg-2)", setup: "var(--red)", bot: "var(--tint-d)", service: "var(--green)" };
const PKG_GLYPH = {
  skill: '<path d="M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9z"/><path d="M19 3v4M17 5h4"/>',
  agent: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c0-3.9 3.1-6.5 7-6.5s7 2.6 7 6.5"/>',
  mcp: '<path d="M9 3v5M15 3v5"/><path d="M6 8h12v4a6 6 0 0 1-12 0z"/><path d="M12 18v3"/>',
  tool: '<path d="M14.5 6.5a4 4 0 0 0 5 5L13 18l-3-3 6.5-6.5"/><path d="M10 15l-5 5"/>',
  setup: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
  bot: '<rect x="4.5" y="8" width="15" height="11" rx="3"/><path d="M12 4v4"/><circle cx="9" cy="13.5" r="1"/><circle cx="15" cy="13.5" r="1"/>',
  service: '<rect x="4" y="4" width="16" height="6" rx="2"/><rect x="4" y="14" width="16" height="6" rx="2"/><path d="M8 7h.01M8 17h.01"/>',
};
const PkgTile = ({ type, size = 72 }) => html`<span class="pk-tile" aria-hidden="true" style=${`--gt:${PKT[type] || "var(--blue)"};width:${size}px;height:${size}px`}><svg viewBox="0 0 24 24" width=${Math.round(size * 0.54)} height=${Math.round(size * 0.54)} fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" dangerouslySetInnerHTML=${{ __html: PKG_GLYPH[type] || PKG_GLYPH.skill }}></svg></span>`;
const Authors = ({ list }) => list.map((a, i) => html`<span key=${i}>${i ? ", " : ""}${a.url && /^https?:\/\//i.test(a.url) ? html`<a href=${a.url} target="_blank" rel="noopener noreferrer">${a.name || a.email}</a>` : a.email ? html`<a href=${"mailto:" + a.email}>${a.name || a.email}</a>` : a.name}</span>`);
const Req = ({ req, dl }) => {
  const cmds = req.commands || [], pk = req.packages || [], os = req.os || [];
  const Row = ({ k, children }) => html`<div class="pk-req"><dt>${k}</dt><dd>${children}</dd></div>`;
  const body = html`<${Row} k="Required commands">${cmds.length ? cmds.map((c, i) => html`<span key=${i} class="pk-pill"><code>${c.name || c}</code>${c.hint ? html` <span class="mut">${c.hint}</span>` : ""}</span>`) : html`<span class="mut">None</span>`}<//>
    <${Row} k="Depends on">${pk.length ? pk.map((x) => html`<a key=${x} class="pk-pill" href=${"#/package/" + x.split(/[<>=!~]/)[0]}>${x}</a>`) : html`<span class="mut">No other packages</span>`}<//>
    <${Row} k="Supported systems">${os.length ? os.map((o) => html`<span key=${o} class="pk-pill">${o}</span>`) : html`<span class="mut">All</span>`}<//>`;
  return dl ? html`<dl class="pk-reqs">${body}</dl>` : body;
};
function Package({ name }) {
  const { me } = useStore(); const [tab, setTab] = useState("Overview"); const [tick, setTick] = useState(0); const [dlp, setDlp] = useState({});
  const p = useLoad(() => api("/packages/" + name), [name, tick]);
  const download = async (v) => {
    setDlp((x) => ({ ...x, [v]: 0 }));
    try {
      const t = tok(), blob = await fetchChunks(U(`/api/v1/index/packages/${encodeURIComponent(name)}/transfer/${encodeURIComponent(v)}`), t ? { Authorization: "Bearer " + t } : {}, (f) => setDlp((x) => ({ ...x, [v]: f })));
      saveBlob(blob, `${name}-${v}.tar.gz`);
    } catch (e) { toast(e.message, 1); }
    setDlp((x) => { const y = { ...x }; delete y[v]; return y; });
  };
  const mt = useLoad(() => api("/meta"), []); const meta = mt.d || {};
  if (p.loading) return html`<div class="skel" style="min-height:300px"></div>`;
  if (p.e) return html`<${Err} e=${p.e} />`;
  const d = p.d, req = d.requires || {}, tabs = ["Overview", "Install", "Versions", "Usage", "Reviews", ...(d.access === "admin" ? ["Sharing"] : [])];
  const latest = (d.versions || []).find((v) => v.version === d.latest_version) || (d.versions || [])[0];
  const rating = d.rating && d.rating.avg, nrev = (d.rating && d.rating.count) || 0;
  const facts = [["Version", "v" + d.latest_version], ["Downloads", fmt(d.downloads)], [nrev ? "Rating · " + nrev + (nrev === 1 ? " review" : " reviews") : "Rating", rating ? "★ " + rating : "No ratings yet"], ["Updated", ago(d.updated)]];
  return html`<div class="pk">
    <header class="pk-hero"><${PkgTile} type=${d.type} name=${d.name} size=${120} />
      <div class="pk-id"><div class="pk-title"><h1>${d.name}</h1></div>
        <p class="lead">${d.description}</p>
        <div class="pk-tags"><span class=${"badge " + (TYPE[d.type] || "")}>${d.type}</span>${d.visibility === "private" && html`<span class="badge orange">private</span>`}${d.license ? html`<span class="badge blue pk-lic" title="License"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M10 12h5M10 16h5"/></svg>${d.license}</span>` : ""}${(d.tags || []).map((t) => html`<a class="badge tag" key=${t} href=${"#/browse?tag=" + encodeURIComponent(t)}>${t}</a>`)}</div>
        ${(d.authors || []).length ? html`<p class="pk-by sm mut">By <${Authors} list=${d.authors} /></p>` : ""}</div></header>
    <div class="pk-install"><div class="pk-install-h"><b>Install</b><span class="sm mut">Run this in a terminal</span></div><${Cmd} text=${"aihub install " + d.name} /></div>
    <dl class="pk-facts">${facts.map(([k, v]) => html`<div key=${k}><dd>${v}</dd><dt>${k}</dt></div>`)}</dl>
    <div class="pk-body">
      <div class="pk-main stack">
        <div class="seg" role="group" aria-label="Package sections">${tabs.map((t) => html`<button key=${t} class=${tab === t ? "on" : ""} aria-pressed=${tab === t} onClick=${() => setTab(t)}>${t}</button>`)}</div>
        ${tab === "Overview" && html`<${Readme} name=${name} />`}
        ${tab === "Install" && html`<div class="list">
          <div class="li"><div class="t"><b>Command</b><span class="mono">aihub install ${d.name}</span></div></div>
          <div class="li"><div class="t"><b>Required commands</b><span>${(req.commands || []).length ? (req.commands || []).map((c) => (c.name || c) + (c.hint ? ` - ${c.hint}` : "")).join(" · ") : "None"}</span></div></div>
          <div class="li"><div class="t"><b>Depends on</b><span>${(req.packages || []).length ? (req.packages || []).map((x) => html`<a href=${"#/package/" + x.split(/[<>=!~]/)[0]} style="margin-right:10px">${x}</a>`) : "No other packages"}</span></div></div>
          <div class="li"><div class="t"><b>Supported systems</b><span>${(req.os || []).length ? req.os.join(", ") : "All"}</span></div></div></div>`}
        ${tab === "Versions" && html`<div class="list tscroll"><table><thead><tr><th>Version</th><th>Released</th><th>Size</th><th>Downloads</th><th>SHA-256</th>${meta.allow_source_download || d.versions.some((v) => v.archive) ? html`<th></th>` : null}</tr></thead><tbody>
          ${d.versions.map((v) => html`<tr key=${v.version}><td>${v.version} ${v.yanked ? html`<span class="badge red">yanked</span>` : ""}</td><td>${ago(v.created)}</td><td>${(v.size / 1024).toFixed(1)} KB</td><td>${fmt(v.downloads)}</td><td class="mono">${(v.sha256 || "").slice(0, 12)}</td>${v.archive ? html`<td><button class="btn sec sm" disabled=${v.version in dlp} onClick=${() => download(v.version)} title="Download the package archive (.tar.gz) from this hub">${v.version in dlp ? "Downloading " + Math.round(dlp[v.version] * 100) + "%" : "Download"}</button></td>` : meta.allow_source_download ? html`<td>${v.source ? html`<a class="btn sec sm" href=${v.source} target="_blank" rel="noopener noreferrer">Source ↗</a>` : null}</td>` : d.versions.some((x) => x.archive) ? html`<td></td>` : null}</tr>`)}</tbody></table></div>`}
        ${tab === "Usage" && html`<${Usage} name=${name} />`}
        ${tab === "Sharing" && html`<${Sharing} name=${name} pkg=${d} onChange=${() => setTick(tick + 1)} />`}
        ${tab === "Reviews" && html`<${Reviews} name=${name} me=${me} onDone=${() => setTick(tick + 1)} />`}
      </div>
      <aside class="pk-side" aria-label="Package details"><section class="pk-card"><h3>Requirements</h3><${Req} req=${req} dl=${true} /></section>
        <section class="pk-card"><h3>Latest release</h3><dl class="pk-reqs"><div class="pk-req"><dt>Version</dt><dd><span class="pk-pill">v${d.latest_version}</span></dd></div>
          ${latest && latest.created ? html`<div class="pk-req"><dt>Released</dt><dd>${ago(latest.created)}</dd></div>` : ""}
          ${latest && latest.size ? html`<div class="pk-req"><dt>Size</dt><dd>${(latest.size / 1024).toFixed(1)} KB</dd></div>` : ""}
          <div class="pk-req"><dt>All versions</dt><dd>${(d.versions || []).length} <button type="button" class="pk-link" onClick=${() => setTab("Versions")}>view</button></dd></div></dl></section></aside></div>
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
      <div class="seg" role="group" aria-label="Access level"><button class=${lvl === "view" ? "on" : ""} aria-pressed=${lvl === "view"} onClick=${() => setLvl("view")}>Can view</button><button class=${lvl === "develop" ? "on" : ""} aria-pressed=${lvl === "develop"} onClick=${() => setLvl("develop")}>Can develop</button></div></div>
    <p class="xs mut3"><b>View</b>: find, read and install. <b>Develop</b>: also publish new versions. Repo admins can change visibility and sharing.</p>
    ${D.access.length ? html`<div class="list">${D.access.map((x) => html`<div class="li" key=${x.type + x.name}>
      ${x.type === "group" ? html`<span class="av" style="width:36px;height:36px;background:var(--fg-3)">👥</span>` : html`<${Avatar} p=${{ username: x.name, display_name: x.display_name, avatar_url: x.avatar_v ? U(`/api/v1/users/${x.name}/avatar?v=${x.avatar_v}`) : null }} size=${36} />`}
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
  return html`<div class="stack">${me || anonOk ? html`<div class="card stack">${anonOk && html`<input class="field" required maxlength="40" placeholder="Your name (required)" aria-label="Your name" value=${who} onInput=${(e) => setWho(e.target.value)} />`}<div class="row"><b>Your rating</b><div class="seg" role="group" aria-label="Rating">${[1, 2, 3, 4, 5].map((n) => html`<button key=${n} class=${rating === n ? "on" : ""} aria-pressed=${rating === n} aria-label=${n + (n === 1 ? " star" : " stars")} onClick=${() => setRating(n)}>${"★".repeat(n)}</button>`)}</div></div>
      <textarea class="field" rows="3" style="padding:12px;resize:vertical" placeholder="Share your experience (optional)" value=${body} onInput=${(e) => setBody(e.target.value)}></textarea>
      <button class="btn" disabled=${anonOk && who.trim().length < 2} onClick=${post}>Submit review</button></div>` : html`<p class="mut"><a href="#/login">Sign in</a> to leave a review.</p>`}
    ${r.d && (r.d.reviews.length ? html`<div class="list">${r.d.reviews.map((x) => html`<div class="li" key=${x.username || "anon:" + x.display_name}><${Avatar} p=${x.anonymous ? { username: x.display_name } : x} size=${36} /><div class="t"><b>${shown(x)} <span class="mut3">${"★".repeat(x.rating)}</span></b>${x.title && html`<span class="xs mut3" style="display:block">${x.title}</span>`}<span>${x.body}</span></div><span class="xs mut3">${ago(x.created)}</span></div>`)}</div>` : html`<p class="mut">No reviews yet.</p>`)}</div>`;
}

function Rankings() {
  const [tab, setTab] = useState("packages"); const r = useLoad(() => api("/rankings/" + tab), [tab]);
  const label = { packages: "Most used", reviews: "Top rated", reviewed: "Most reviewed", developers: "Top developers", users: "Most active users" };
  const items = (r.d && r.d.items) || [], mx = Math.max(1, ...items.map((i) => (tab === "reviews" ? i.score : i.count)));
  return html`<div class="stack"><h1>Rankings</h1><p class="lead">${tab === "reviews" || tab === "reviewed" ? "Based on all reviews, ever." : "Based on the last 30 days of real usage."}</p>
    <div class="seg">${Object.keys(label).map((t) => html`<button key=${t} class=${tab === t ? "on" : ""} aria-pressed=${tab === t} onClick=${() => setTab(t)}>${label[t]}</button>`)}</div>
    ${r.loading ? html`<div class="skel"></div>` : !items.length ? html`<${Empty} t="No data yet" d="Rankings appear once usage events arrive." />` :
      html`<div class="list">${items.map((i, n) => html`<div class="li" key=${i.name}><span class=${"rank " + (n < 3 ? "top" : "")}>${n + 1}</span>
        <div class="t"><b>${tab === "packages" || tab === "reviews" || tab === "reviewed" ? html`<a href=${"#/package/" + i.name}>${i.name}</a>` : i.name}</b><div class="bar"><i style=${"width:" + ((tab === "reviews" ? i.score : i.count) / mx * 100) + "%"}></i></div></div>
        ${tab === "reviews" || tab === "reviewed" ? html`<span class="n" title=${i.count + " reviews"}>★ ${i.avg} <span class="mut3">(${fmt(i.count)})</span></span>` : html`<span class="n">${fmt(i.count)}</span>`}</div>`)}</div>`}</div>`;
}

const detectOS = () => { const u = (navigator.userAgentData && navigator.userAgentData.platform || navigator.platform || "") + " " + navigator.userAgent; return /win/i.test(u) && !/darwin/i.test(u) ? "windows" : /mac|iphone|ipad/i.test(u) ? "mac" : "linux"; };

function Start() {
  const m = useLoad(() => api("/meta"), []); const o = ((m.d && m.d.public_url) || location.origin).replace(/\/+$/, ""); const auto = detectOS(); const [os, setOs] = useState(auto); const [alt, setAlt] = useState(false);
  const cfg = useLoad(() => fetch(U("/api/v1/client-config")).then((r) => r.ok ? r.json() : null).catch(() => null), []);
  const OS = { mac: "macOS", linux: "Linux", windows: "Windows" };
  const cmd = os === "windows" ? `irm ${o}/install.ps1 | iex` : `curl -fsSL ${o}/install.sh | sh`;
    const gitBranch = (m.d && m.d.cli_git_branch || "").trim(), gitSubdir = (m.d && m.d.cli_git_subdir || "").trim();
    const pip = `pip install "aihub-cli @ git+${(m.d && m.d.cli_git_url) || "<your-repo-url>"}${gitBranch ? "@" + gitBranch : ""}${gitSubdir ? "#subdirectory=" + gitSubdir : ""}"`;
  const note = os === "windows" ? "Run in PowerShell. Needs Python 3.9+ (python.org or `winget install Python.Python.3.12`). PATH is updated automatically; open a new terminal afterwards."
    : os === "mac" ? "Run in Terminal. Needs Python 3.9+ (preinstalled with Xcode tools, or `brew install python`). Then add it to PATH:"
    : "Run in your shell. Needs Python 3.9+ and curl (e.g. `sudo apt install python3 curl`). Then add it to PATH:";
  const path = os === "mac" ? `echo 'export PATH="$HOME/.aihub/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc` : os === "linux" ? `echo 'export PATH="$HOME/.aihub/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc` : null;
  const envs = os === "windows" ? `$env:AIHUB_INDEX_URL="https://…"` : `export AIHUB_INDEX_URL="https://…"`;
  const step = (n, t, d, c) => html`<div class="card stack"><div class="row"><span class="rank top">${n}</span><h3>${t}</h3></div><p class="mut">${d}</p>${c && html`<${Cmd} text=${c} />`}</div>`;
  if (m.loading) return html`<div class="skel"></div>`;
  const idx = cfg.d && cfg.d.index && cfg.d.index.url;
  return html`<div class="stack"><h1>Get started</h1><p class="lead">Up and running in under a minute. Python 3.9 or later, and git.</p>
    <div class="card stack"><div class="row"><span class="rank top">1</span><h3>Install the CLI</h3><span class="sp"></span>
      <div class="seg" role="group" aria-label="Operating system">${Object.keys(OS).map((k) => html`<button key=${k} class=${os === k ? "on" : ""} aria-pressed=${os === k} onClick=${() => setOs(k)}>${OS[k]}${k === auto ? " (detected)" : ""}</button>`)}</div></div>
      <p class="mut">Installs aihub and sets up usage reporting for Claude Code and OpenCode. ${note}</p>
      <${Cmd} text=${cmd} />${path && html`<${Cmd} text=${path} />`}
      <button class="btn sec sm" style="align-self:flex-start" onClick=${() => setAlt(!alt)} aria-expanded=${alt}>${alt ? "Hide" : "Can’t reach this server from your machine?"}</button>
      ${alt && html`<div class="stack"><p class="mut">Install straight from git instead. It needs only git and Python, not this website.</p><${Cmd} text=${pip} />
        <p class="mut">Then tell the CLI where the package index is. Export it (the CLI picks it up automatically), or run <code>aihub setup --manual</code> and it asks:</p><${Cmd} text=${envs} /><${Cmd} text="aihub setup --manual" />
        <p class="mut">${idx ? html`The package index is <code>${idx}</code>; ` : ""}if git asks for a login, enter your username and an access token once. <code>aihub setup</code> registers a small credential helper for this host (Keychain on macOS, DPAPI on Windows, Secret Service or a private file on Linux), so it is remembered and not asked again. If it is already set up, nothing changes.</p></div>`}</div>
    ${step(2, "Connect to the package index", "Pulls the index location and the usage inbox from this server, and makes sure git remembers your login (already set up? it just says so). Asks only for anything missing. Refreshes itself later.", "aihub setup")}
    ${step(3, "Install something", "You'll be asked which tools to connect it to. Everything is reversible.", "aihub install <package>")}
    ${step(4, "Publish your own", "Scaffold a manifest, then publish. aihub commits and pushes to your git repo for you.", "aihub dev init && aihub dev publish")}</div>`;
}

const pwScore = (p) => !p ? 0 : Math.min(4, (p.length >= 6) + (p.length >= 10) + (/[a-z]/.test(p) && /[A-Z]/.test(p)) + (/\d/.test(p) && /[^\w\s]/.test(p)));
const PW_WORD = ["", "Weak", "Fair", "Good", "Strong"];
const EyeIcon = ({ off }) => html`<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" />${off && html`<path d="M4 4l16 16" />`}</svg>`;
// one floating-label field; `children` = trailing control (show/hide button)
function AuField({ id, label, value, set, type = "text", invalid, children, ...rest }) {
  return html`<div class=${"au-f" + (invalid ? " bad" : "")}><input id=${id} class="au-in" type=${type} placeholder=" " value=${value} onInput=${(e) => set(e.target.value)} aria-invalid=${invalid ? "true" : "false"} ...${rest} />
    <label for=${id}>${label}</label>${children}</div>`;
}
function Auth({ mode }) {
  const [u, setU] = useState(""); const [p, setP] = useState(""); const [p2, setP2] = useState(""); const [rem, setRem] = useState(true); const [e, setE] = useState(""); const [busy, setBusy] = useState(false);
  const [show, setShow] = useState(false); const [caps, setCaps] = useState(false); const [done, setDone] = useState(false); const [shakes, setShakes] = useState(0);
  const reg = mode === "register", br = store.brand || {}, card = useRef(null);
  const mismatch = reg && p2 && p !== p2, score = pwScore(p);
  useEffect(() => { setE(""); setP2(""); }, [mode]);
  const fail = (m) => { setE(m); setShakes((n) => n + 1); };
  // specular light + slight tilt follow the pointer (fine pointers only; no re-render, CSS vars on the card)
  const still = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce), (pointer: coarse)").matches;
  const move = (ev) => { const c = card.current; if (!c || still) return; const r = c.getBoundingClientRect(), x = (ev.clientX - r.left) / r.width, y = (ev.clientY - r.top) / r.height;
    c.style.setProperty("--mx", (x * 100).toFixed(1) + "%"); c.style.setProperty("--my", (y * 100).toFixed(1) + "%");
    c.style.setProperty("--rx", (Math.max(-1, Math.min(1, 0.5 - y)) * 4).toFixed(2) + "deg"); c.style.setProperty("--ry", (Math.max(-1, Math.min(1, x - 0.5)) * 5).toFixed(2) + "deg"); };
  const leave = () => { const c = card.current; if (c) { c.style.setProperty("--rx", "0deg"); c.style.setProperty("--ry", "0deg"); } };
  const go = async (ev) => { ev.preventDefault(); if (busy || done) return; if (reg && p !== p2) return fail("The two passwords do not match"); setBusy(true); setE("");
    try { if (reg) { const r = await api("/auth/register", { method: "POST", body: { username: u, password: p } }); if (r.status === "pending") { toast("Account created - awaiting admin approval"); return nav("/login"); } }
      const r = await api("/auth/login", { method: "POST", body: { username: u, password: p, remember: rem } }); keepToken(r.token, rem); setDone(true);
      await Promise.all([loadMe(), new Promise((ok) => setTimeout(ok, 520))]); nav("/"); }
    catch (x) { fail(x.message); } finally { setBusy(false); } };
  const capsKey = (ev) => ev.getModifierState && setCaps(ev.getModifierState("CapsLock"));
  const eye = html`<button type="button" class="au-eye" onClick=${() => setShow(!show)} aria-label=${show ? "Hide password" : "Show password"} aria-pressed=${show}><${EyeIcon} off=${show} /></button>`;
  return html`<section class="au" onPointerMove=${move} onPointerLeave=${leave}>
    <div class="au-orbs" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
    <form ref=${card} class=${"au-card" + (done ? " ok" : "")} style=${shakes ? `animation-name:au-shake${shakes % 2}` : ""} onSubmit=${go}>
      <span class="au-sheen" aria-hidden="true"></span><span class="au-ring" aria-hidden="true"></span>
      <div class="au-brand">${br.logo_url ? html`<img decoding="async" class="logo" src=${br.logo_url} alt="" />` : html`<span class="au-mark" aria-hidden="true"></span>`}<span>${br.name || "AI Hub"}</span></div>
      <div class=${"au-seg" + (reg ? " r" : "")} role="tablist" aria-label="Account"><i class="au-pill" aria-hidden="true"></i>
        <a role="tab" href="#/login" aria-selected=${!reg}>Sign in</a><a role="tab" href="#/register" aria-selected=${reg}>Create account</a></div>
      <div class="au-head" key=${mode}><h1>${reg ? "Create your account" : "Welcome back"}</h1>
        <p class="mut">${reg ? "Publish and install skills, agents and MCP servers with your team." : "Sign in to install, publish and track your packages."}</p></div>
      <div class="au-fields">
        <${AuField} id="au-u" label="Username" value=${u} set=${setU} autocomplete="username" autocapitalize="none" spellcheck="false" required />
        <${AuField} id="au-p" label="Password" value=${p} set=${setP} type=${show ? "text" : "password"} autocomplete=${reg ? "new-password" : "current-password"} required minlength="6" onKeyUp=${capsKey} onKeyDown=${capsKey}>${eye}<//>
        <div class=${"au-more" + (reg ? " open" : "")} aria-hidden=${!reg}><div>
          <div class="au-meter" data-s=${score} aria-live="polite"><span><i></i><i></i><i></i><i></i></span><b class="xs">${p ? PW_WORD[score] : "At least 6 characters"}</b></div>
          <${AuField} id="au-p2" label="Re-enter password" value=${p2} set=${setP2} type=${show ? "text" : "password"} autocomplete="new-password" required=${reg} minlength="6" invalid=${mismatch} tabindex=${reg ? 0 : -1} disabled=${!reg}>
            ${reg && p2 && html`<span class=${"au-tick" + (mismatch ? " no" : "")} aria-hidden="true">${mismatch ? "✕" : "✓"}</span>`}<//>
        </div></div>
      </div>
      <div class="au-row">${caps ? html`<span class="au-caps xs">⇪ Caps Lock is on</span>` : html`<span></span>`}
        <label class="au-rem"><input type="checkbox" checked=${rem} onChange=${(x) => setRem(x.target.checked)} /><i aria-hidden="true"></i>Remember me</label></div>
      <button class=${"au-go" + (busy ? " busy" : "") + (done ? " done" : "")} disabled=${busy || mismatch || done}><span class="au-go-t">${reg ? "Create account" : "Sign in"}</span><span class="au-spin" aria-hidden="true"></span><span class="au-ok" aria-hidden="true">✓</span></button>
      <div class="msg" role="alert">${e}</div>
      <p class="au-alt sm mut">${reg ? html`Already have an account? <a href="#/login">Sign in</a>` : html`New here? <a href="#/register">Create an account</a>`}</p>
    </form></section>`;
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
    try { const blob = await toAvatarBlob(f); const r = await fetch(U("/api/v1/auth/avatar"), { method: "POST", headers: { Authorization: "Bearer " + tok() }, body: blob });
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

const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];
function AdminListen() {
  const [st, setSt] = useState({ listening: false, lines: [], seq: 0 }); const [busy, setBusy] = useState(false); const [lvl, setLvl] = useState("INFO"); const [q, setQ] = useState(""); const [follow, setFollow] = useState(true); const [paused, setPaused] = useState(false);
  const box = useRef(null); const seq = useRef(0); const lines = useRef([]);
  const merge = (r) => { if (r.seq < seq.current) { lines.current = []; seq.current = 0; } lines.current = lines.current.concat(r.lines).slice(-2000); seq.current = Math.max(seq.current, r.seq); setSt({ listening: r.listening, since: r.since, by: r.by, lines: lines.current, seq: r.seq }); };
  const poll = async () => { try { merge(await api("/admin/logs?after=" + seq.current)); } catch (e) { setSt((x) => ({ ...x, error: e.message })); } };
  useEffect(() => { poll(); }, []);
  useEffect(() => { if (!st.listening || paused) return; const t = setInterval(poll, 1500); return () => clearInterval(t); }, [st.listening, paused]);
  useEffect(() => { if (follow && box.current) box.current.scrollTop = box.current.scrollHeight; }, [st.lines.length, follow]);
  const toggle = async (on) => { setBusy(true); try { merge(await api("/admin/logs/listen", { method: "POST", body: { on, after: seq.current } })); setPaused(false); } catch (e) { toast(e.message, 1); } finally { setBusy(false); } };
  const clear = async () => { try { await api("/admin/logs/listen", { method: "POST", body: { clear: true } }); } catch {} lines.current = []; setSt((x) => ({ ...x, lines: [] })); };
  const min = LOG_LEVELS.indexOf(lvl), f = q.trim().toLowerCase();
  const shown = st.lines.filter((l) => Math.max(0, LOG_LEVELS.indexOf(l.level)) >= min && (!f || (l.name + " " + l.msg).toLowerCase().includes(f)));
  const col = (l) => l.level === "ERROR" || l.level === "CRITICAL" ? "var(--red)" : l.level === "WARNING" ? "var(--orange)" : l.level === "DEBUG" ? "var(--fg-3)" : "inherit";
  const t = (ts) => new Date(ts * 1000).toLocaleTimeString();
  return html`<div class="stack"><div class="card stack"><div class="row" style="flex-wrap:wrap"><h3>Server log</h3><span class=${"badge " + (st.listening ? "green" : "")}>${st.listening ? (paused ? "paused" : "listening") : "off"}</span><span class="sp"></span>
      ${st.listening ? html`<button class="btn sec sm" onClick=${() => setPaused(!paused)}>${paused ? "Resume" : "Pause"}</button><button class="btn sm danger" disabled=${busy} onClick=${() => toggle(false)}>Stop listening</button>` : html`<button class="btn sm" disabled=${busy} onClick=${() => toggle(true)}>Start listening</button>`}
      <button class="btn sec sm" onClick=${clear}>Clear</button></div>
    <p class="mut sm">Shows what the server logs from the moment you start (sync, backup, git, updates, requests). Secrets are masked. Nothing is stored on disk; the last 2000 lines are kept in memory, and listening stops by itself 2 minutes after the last viewer leaves.${st.listening && st.by ? " Started by " + st.by + "." : ""}</p>
    <div class="row" style="flex-wrap:wrap"><div class="seg">${LOG_LEVELS.map((x) => html`<button key=${x} class=${lvl === x ? "on" : ""} onClick=${() => setLvl(x)}>${x === "DEBUG" ? "All" : x === "INFO" ? "Info+" : x === "WARNING" ? "Warnings+" : "Errors"}</button>`)}</div>
      <input class="field" style="flex:1;min-width:160px" placeholder="Filter text" aria-label="Filter log lines" value=${q} onInput=${(e) => setQ(e.target.value)} />
      <label class="row sm" style="gap:6px"><input type="checkbox" checked=${follow} onChange=${(e) => setFollow(e.target.checked)} /> Follow</label></div>
    ${st.error && html`<p class="sm" role="alert" style="color:var(--red)">${st.error}</p>`}
    <div ref=${box} class="mono xs vscroll" tabindex="0" role="log" aria-live="polite" aria-label="Server log" style="max-height:60vh;min-height:12rem;overflow:auto;white-space:pre-wrap;word-break:break-word;padding:8px;border-radius:8px;background:var(--fill)">
      ${shown.length ? shown.map((l) => html`<div key=${l.seq} style=${"color:" + col(l)}><span class="mut3">${t(l.ts)}</span> <b>${l.level}</b> <span class="mut3">${l.name}</span> ${l.msg}</div>`) : html`<span class="mut">${st.listening ? "Waiting for log lines…" : "Not listening. Press Start listening."}</span>`}</div>
    <p class="xs mut3">${shown.length} of ${st.lines.length} lines shown</p></div></div>`;
}
// Admin: a side nav with every tab; under the open tab, its sections (collapsible Folds, else card headings) to jump to.
const ADMIN_TABS = [["People", "Accounts, roles, passwords"], ["Roles", "Permission matrix"], ["Sync", "Index, inbox, load limits"], ["Backup", "Git backup, git login"], ["Updates", "Self-update, rollback"], ["Listen", "Live server log"], ["Settings", "Site, access, repositories"]];
function useAdminSections(box, tab) {
  const [secs, setSecs] = useState([]); const [cur, setCur] = useState("");
  useEffect(() => {
    const el = box.current; if (!el) return;
    const scan = () => {
      let found = [...el.querySelectorAll("section.fold[data-fold]")].map((x) => ({ id: x.id, t: x.dataset.fold, open: x.classList.contains("open") }));
      if (!found.length) found = [...el.querySelectorAll(".card h2, .card h3, :scope > div > h2")].filter((h) => h.textContent.trim()).slice(0, 12).map((h, i) => { if (!h.id) h.id = "adm-h-" + i; return { id: h.id, t: h.textContent.trim().slice(0, 40), open: true }; });
      setSecs((p) => (JSON.stringify(p) === JSON.stringify(found) ? p : found));
    };
    scan(); const mo = new MutationObserver(scan); mo.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
    return () => mo.disconnect();
  }, [tab]);
  useEffect(() => {
    if (!secs.length || !("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver((es) => { const v = es.filter((e) => e.isIntersecting).sort((x, y) => x.boundingClientRect.top - y.boundingClientRect.top)[0]; if (v) setCur(v.target.id); }, { rootMargin: "-80px 0px -60% 0px" });
    secs.forEach((x) => { const n = document.getElementById(x.id); if (n) io.observe(n); }); return () => io.disconnect();
  }, [secs]);
  const go = (id) => { const n = document.getElementById(id); if (!n) return; if (n.matches("section.fold:not(.open)")) { const b = n.querySelector(".fold-h button"); if (b) b.click(); }
    setCur(id); requestAnimationFrame(() => n.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" })); };
  return { secs, cur, go };
}
function Admin() {
  const { me } = useStore(); const [tick, setTick] = useState(0); const bump = () => setTick((x) => x + 1);
  const tabs = can("admin") ? ADMIN_TABS.map((x) => x[0]) : ["People"];
  const [tab0, setTab0] = useState(() => { try { return localStorage.getItem("aihub_admin_tab") || "People"; } catch { return "People"; } });
  const tab = tabs.includes(tab0) ? tab0 : "People";        // permissions arrive after the first render: keep the remembered tab until then
  const setTab = (t) => { setTab0(t); try { localStorage.setItem("aihub_admin_tab", t); } catch {} window.scrollTo({ top: 0 }); };
  const box = useRef(null); const sec = useAdminSections(box, tab);
  if (!can("admin") && !can("reset_password")) return html`<${Empty} t="Admins only" d="You don’t have access to this page." />`;
  const body = html`${tab === "People" && html`<${People} me=${me} tick=${tick} bump=${bump} />`}${tab === "Roles" && can("admin") && html`<${Roles} tick=${tick} bump=${bump} />`}
    ${tab === "Sync" && can("admin") && html`<${AdminSync} />`}${tab === "Backup" && can("admin") && html`<${AdminBackup} />`}${tab === "Updates" && can("admin") && html`<${AdminUpdates} />`}${tab === "Listen" && can("admin") && html`<${AdminListen} />`}${tab === "Settings" && can("admin") && html`<${AdminSettings} />`}`;
  if (tabs.length < 2) return html`<div class="stack"><h1>Admin</h1>${body}</div>`;
  const hint = Object.fromEntries(ADMIN_TABS);
  return html`<div class="stack"><h1>Admin</h1><div class="adm">
    <aside class="adm-nav" aria-label="Admin sections"><nav>${tabs.map((t) => html`<div key=${t} class="adm-g">
      <button type="button" class=${"adm-t" + (tab === t ? " on" : "")} aria-current=${tab === t ? "page" : null} onClick=${() => setTab(t)}><b>${t}</b><span>${hint[t]}</span></button>
      ${tab === t && sec.secs.length > 1 && html`<ul class="adm-s">${sec.secs.map((x) => html`<li key=${x.id}><button type="button" class=${sec.cur === x.id ? "on" : ""} onClick=${() => sec.go(x.id)}>${x.t}${!x.open && html`<i class="adm-closed" title="collapsed" aria-label="collapsed"></i>`}</button></li>`)}</ul>`}</div>`)}</nav></aside>
    <div class="adm-main" ref=${box}>${body}</div></div></div>`;
}

// ---------- Running: bots started with `aihub run` on people's machines. Commands are queued; the run takes them at its next report.
const RUN_BADGE = { running: "green", waiting: "orange", starting: "orange", failed: "red", offline: "red" };
const runDur = (n) => { n = Math.max(0, Math.round(n || 0)); return n >= 86400 ? (n / 86400 | 0) + "d " + (n % 86400 / 3600 | 0) + "h" : n >= 3600 ? (n / 3600 | 0) + "h " + (n % 3600 / 60 | 0) + "m" : n >= 60 ? (n / 60 | 0) + "m " + n % 60 + "s" : n + "s"; };
const splitArgs = (t) => [...String(t || "").matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)].map((m) => m[1] ?? m[2] ?? m[3]);
const joinArgs = (a) => (a || []).map((x) => (/[\s"']/.test(x) || !x ? JSON.stringify(x) : x)).join(" ");
const RUN_PRESETS = [["None", ""], ["Every 5 min", "5m"], ["Hourly", "1h"], ["Daily 08:00", "0 8 * * *"], ["Weekdays 08:00", "0 8 * * 1-5"]];
function Running() {
  const { me } = useStore(); const [scope, setScope] = useState("mine"); const [d, setD] = useState(null); const [e, setE] = useState(""); const [open, setOpen] = useState(""); const [busy, setBusy] = useState(""); const [q, setQ] = useState("");
  const load = useCallback(async () => { try { setD(await api("/runs?scope=" + scope)); setE(""); } catch (x) { setE(x.message); } }, [scope]);
  useEffect(() => { load(); const t = setInterval(() => { if (!document.hidden) load(); }, 4000); return () => clearInterval(t); }, [load]);
  if (!me) return html`<${Empty} t="Sign in" d="Sign in to see the bots you run with aihub run." />`;
  if (!d) return e ? html`<${Err} e=${e} />` : html`<div class="skel"></div>`;
  const send = async (r, action, extra = {}) => { setBusy(r.key + action); try { await api("/runs/" + r.key + "/command", { method: "POST", body: { action, ...extra } }); toast(action === "set" ? "Change sent. It applies within seconds" : "Sent: " + action); load(); } catch (x) { toast(x.message, 1); } finally { setBusy(""); } };
  const forget = async (r) => { try { await api("/runs/" + r.key, { method: "DELETE" }); toast("Removed from the list"); load(); } catch (x) { toast(x.message, 1); } };
  const qq = q.trim().toLowerCase(); const items = d.items.filter((r) => !qq || [r.name, r.bot, r.host, r.owner, r.status].join(" ").toLowerCase().includes(qq));
  const n = (st) => d.items.filter((r) => r.status === st).length; const now = Date.now() / 1000;
  return html`<div class="stack"><div class="row" style="flex-wrap:wrap;align-items:flex-end"><div><h1>Running</h1><p class="mut">Bots started with <code>aihub run</code> on your machines. Stop, start, restart or change them here; a change reaches the machine at its next report (a few seconds while this page is open).</p></div><span class="sp"></span>
      ${d.is_admin && html`<div class="seg" role="group" aria-label="Whose runs">${[["mine", "Mine"], ["all", "Everyone"]].map(([k, l]) => html`<button key=${k} class=${scope === k ? "on" : ""} aria-pressed=${scope === k} onClick=${() => setScope(k)}>${l}</button>`)}</div>`}</div>
    <div class="kpis"><div class="stat"><b>${n("running")}</b><span>running</span></div><div class="stat"><b>${n("waiting")}</b><span>waiting (schedule / restart)</span></div><div class="stat"><b>${n("stopped")}</b><span>stopped</span></div><div class="stat"><b>${d.items.filter((r) => !r.online).length}</b><span>not connected</span></div></div>
    ${d.items.length ? html`<input class="field" type="search" placeholder="Filter by name, bot, machine, person, status" aria-label="Filter runs" value=${q} onInput=${(x) => setQ(x.target.value)} />` : ""}
    ${!d.items.length ? html`<div class="card stack"><h3>Nothing running yet</h3><p class="mut">Start a bot on a machine signed in to this hub. It runs in the background and shows up here.</p><${Cmd} text="aihub run aihub-bot -- --cron" /><p class="xs mut3">Other forms: <code>aihub run BOT --fg</code> (this terminal) · <code>--schedule 1h</code> · <code>--name NAME</code> · <code>aihub run list</code> · <code>aihub run logs NAME -f</code></p></div>`
      : html`<div class="list">${items.map((r) => { const on = open === r.key; const B = (a) => busy === r.key + a;
        const live = r.online; const up = r.status === "running" && r.last_start ? "up " + runDur(now - r.last_start) : r.status === "waiting" && r.next_at ? "next in " + runDur(r.next_at - now) : r.last_end ? "ended " + ago(r.last_end) : "";
        return html`<div class="run" key=${r.key}><div class="li run-h">
          <span class=${"run-dot " + (RUN_BADGE[r.status] || "")} aria-hidden="true"></span>
          <div class="t"><b>${r.name}${r.name !== r.bot && html` <span class="mut sm">· ${r.bot}</span>`}</b><span>${r.host || "?"}${r.os ? " · " + r.os : ""}${d.scope === "all" ? " · " + (r.owner || "?") : ""}${r.mode === "fg" ? " · terminal" : ""}${r.schedule ? " · schedule " + r.schedule : ""} · restart ${r.restart || "no"}</span></div>
          <div class="run-m"><span class=${"badge " + (RUN_BADGE[r.status] || "")}>${r.status}${r.pending ? " · " + r.pending + " queued" : ""}</span><span class="xs mut3">${up}${r.runs ? " · " + r.runs + (r.runs === 1 ? " start" : " starts") : ""}${r.last_exit != null ? " · exit " + r.last_exit : ""}</span></div>
          <div class="row run-a">${live ? html`${["running", "waiting", "starting"].includes(r.status) ? html`<button class="btn sec sm" disabled=${B("stop")} onClick=${() => send(r, "stop")} title="Stop the bot; the supervisor waits so it can be started again from here">Stop</button>` : html`<button class="btn sm" disabled=${B("start")} onClick=${() => send(r, "start")}>Start</button>`}
            <button class="btn sec sm" disabled=${B("restart")} onClick=${() => send(r, "restart")}>Restart</button>` : html`<button class="btn sec sm" onClick=${() => forget(r)} title="Remove this entry; a run that reports again comes back">Remove</button>`}
            <button class="btn sec sm" aria-expanded=${on} onClick=${() => setOpen(on ? "" : r.key)}>${on ? "Close" : "Details"}</button></div></div>
          ${r.error && html`<p class="sm run-err" role="alert">${r.error}</p>`}
          ${on && html`<div class="run-b"><${RunEdit} r=${r} send=${send} busy=${busy} /><${RunLog} r=${r} /></div>`}</div>`; })}</div>`}
    ${d.items.length ? html`<p class="xs mut3">A run that has not reported for ${d.online_secs} s is shown as not connected (machine asleep, offline, or the run ended). Start it again there with <code>aihub run start NAME</code>.</p>` : ""}</div>`;
}
function RunEdit({ r, send, busy }) {
  const [sc, setSc] = useState(r.schedule || ""); const [rs, setRs] = useState(r.restart || "no"); const [ar, setAr] = useState(joinArgs(r.args));
  const changed = sc.trim() !== (r.schedule || "") || rs !== (r.restart || "no") || ar.trim() !== joinArgs(r.args);
  const save = () => { const b = {}; if (sc.trim() !== (r.schedule || "")) b.schedule = sc.trim(); if (rs !== (r.restart || "no")) b.restart = rs; if (ar.trim() !== joinArgs(r.args)) b.args = splitArgs(ar); send(r, "set", b); };
  return html`<div class="stack run-edit"><h3 class="sm">Behaviour</h3>
    <div class="li"><div class="t"><b>Schedule</b><span>Empty = run once (or keep running). Seconds, <code>30s</code> / <code>5m</code> / <code>2h</code> / <code>1d</code>, or cron (<code>0 8 * * 1-5</code> = weekdays 08:00, the machine's time).</span></div>
      <div class="stack"><input class="field" value=${sc} placeholder="none" aria-label="Schedule" onInput=${(x) => setSc(x.target.value)} />
      <div class="chips">${RUN_PRESETS.map(([l, v]) => html`<button type="button" key=${l} class=${"chip" + (sc.trim() === v ? " on" : "")} onClick=${() => setSc(v)}>${l}</button>`)}</div></div></div>
    <div class="li"><div class="t"><b>When it ends</b><span>Restart policy: on-failure restarts after a non-zero exit (2 s, doubling to 5 min).</span></div>
      <div class="seg">${["no", "on-failure", "always"].map((o) => html`<button key=${o} class=${rs === o ? "on" : ""} aria-pressed=${rs === o} onClick=${() => setRs(o)}>${o}</button>`)}</div></div>
    <div class="li"><div class="t"><b>Arguments</b><span>${r.mine ? "Passed to the bot at its next start (quote values with spaces)." : "Only the owner of a run can change its arguments."}</span></div><input class="field mono" value=${ar} placeholder="--cron" aria-label="Bot arguments" disabled=${!r.mine} onInput=${(x) => setAr(x.target.value)} /></div>
    <div class="row"><span class="sp"></span>${changed && html`<button class="btn sec sm" onClick=${() => { setSc(r.schedule || ""); setRs(r.restart || "no"); setAr(joinArgs(r.args)); }}>Reset</button>`}
      <button class="btn sm" disabled=${!changed || !r.online || busy === r.key + "set"} onClick=${save}>Apply</button>${r.online && html`<button class="btn danger sm" disabled=${busy === r.key + "quit"} onClick=${() => confirm("End " + r.name + " and its supervisor? It can only be started again on its machine.") && send(r, "quit")}>End run</button>`}</div></div>`;
}
function RunLog({ r }) {
  const [lines, setLines] = useState([]); const [follow, setFollow] = useState(true); const [q, setQ] = useState(""); const seq = useRef(0); const box = useRef(null); const all = useRef([]);
  useEffect(() => { let on = 1; seq.current = 0; all.current = [];
    const poll = async () => { try { const x = await api("/runs/" + r.key + "/log?after=" + seq.current); if (!on) return; if (x.seq < seq.current) all.current = []; all.current = all.current.concat(x.lines).slice(-1000); seq.current = x.seq; setLines(all.current); } catch {} };
    poll(); const t = setInterval(() => { if (!document.hidden) poll(); }, 2000); return () => { on = 0; clearInterval(t); }; }, [r.key]);
  useEffect(() => { if (follow && box.current) box.current.scrollTop = box.current.scrollHeight; }, [lines.length, follow]);
  const qq = q.toLowerCase(); const shown = qq ? lines.filter((l) => l.line.toLowerCase().includes(qq)) : lines;
  return html`<div class="stack"><div class="row run-log-h" style="flex-wrap:wrap"><h3 class="sm">Output</h3><span class="xs mut3">live tail · memory only, last 1000 lines</span><span class="sp"></span>
      <input class="field" placeholder="Filter" aria-label="Filter output" value=${q} onInput=${(x) => setQ(x.target.value)} />
      <label class="row sm" style="gap:6px"><input type="checkbox" checked=${follow} onChange=${(x) => setFollow(x.target.checked)} /> Follow</label></div>
    <div ref=${box} class="mono xs vscroll run-log" tabindex="0" role="log" aria-live="polite" aria-label=${"Output of " + r.name}>
      ${shown.length ? shown.map((l) => html`<div key=${l.seq} class=${l.line.startsWith("[aihub") ? "run-sys" : ""}><span class="mut3">${new Date(l.ts * 1000).toLocaleTimeString()}</span> ${l.line}</div>`) : html`<span class="mut">${r.online ? "Waiting for output… (lines arrive with the next report)" : "No output kept on the hub. On the machine: aihub run logs " + r.name}</span>`}</div></div>`;
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
    try { const r = await fetch(U(`/api/v1/admin/users/batch?role=${encodeURIComponent(role)}`), { method: "POST", headers: { Authorization: "Bearer " + tok(), "X-Aihub-Filename": file.name }, body: await file.arrayBuffer() });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Upload failed");
      const blob = await r.blob(); saveBlob(blob, "new-accounts.csv"); setRes({ created: +r.headers.get("X-Created"), skipped: +r.headers.get("X-Skipped") }); done();
    } catch (e) { toast(e.message, 1); } finally { setBusy(false); } };
  return html`<div class="card stack"><h3>Create accounts in bulk</h3>
    <p class="mut sm">Upload a <b>.csv</b> or <b>.xlsx</b> with a header row. Columns (any order): <code>username</code> (required), <code>display_name</code>, <code>title</code>, <code>role</code>, <code>group</code>. Put several groups in one cell separated by <code>;</code> or <code>,</code>. A group that does not exist yet is created, then the people are added. Without a header row it reads <code>username, role</code>. The default role below is used when the role cell is empty. Passwords are generated for you and returned as a downloadable sheet - <b>this is the only time they are shown</b>. Up to 300 accounts per upload.</p>
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
// Collapsible sections for long admin tabs. `init` lists every section key (false = closed by default); the choice is remembered per tab in this browser.
function useFolds(scope, init) {
  const key = "aihub_folds_" + scope, keys = Object.keys(init);
  const [st, setSt] = useState(() => { try { return JSON.parse(localStorage.getItem(key)) || {}; } catch { return {}; } });
  const open = (k) => (k in st ? !!st[k] : init[k] !== false);
  const put = (n) => { setSt(n); try { localStorage.setItem(key, JSON.stringify(n)); } catch {} };
  return { open, keys, openCount: keys.filter(open).length, toggle: (k) => put({ ...st, [k]: !open(k) }), all: (v) => put(Object.fromEntries(keys.map((k) => [k, v]))) };
}
function FoldBar({ f, children }) {
  return html`<div class="foldbar"><span class="xs mut3" aria-live="polite">${f.openCount} of ${f.keys.length} sections open</span><span class="sp"></span>${children}
    <button type="button" class="btn sec sm" disabled=${f.openCount === f.keys.length} onClick=${() => f.all(true)}>Expand all</button>
    <button type="button" class="btn sec sm" disabled=${f.openCount === 0} onClick=${() => f.all(false)}>Collapse all</button></div>`;
}
function Fold({ f, k, title, hint, badge, children }) {
  const on = f.open(k), id = "fold-" + k;
  return html`<section id=${"sec-" + k} data-fold=${typeof title === "string" ? title : k} class=${"fold" + (on ? " open" : "")}><h3 class="fold-h"><button type="button" aria-expanded=${on} aria-controls=${id} onClick=${() => f.toggle(k)}>
      <svg class="fold-chev" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 2l4 4-4 4"/></svg>
      <span class="fold-t">${title}</span>${badge != null && badge !== "" && html`<span class="badge">${badge}</span>`}${hint && html`<span class="fold-s">${hint}</span>`}</button></h3>
    ${on && html`<div class="fold-b" id=${id}>${children}</div>`}</section>`;
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
// Settings > Home page: ordered pinned list (saved as the comma text setting home_pinned), add by search
function PinnedSetting({ x, v, busy, save }) {
  const start = (v || "").split(/[,\s]+/).filter(Boolean); const [list, setList] = useState(start), [q, setQ] = useState(""); const dq = debounced(q);
  const r = useLoad(() => dq.trim() ? api("/packages?per_page=8&q=" + encodeURIComponent(dq.trim())) : Promise.resolve(null), [dq]);
  const opts = ((r.d && r.d.items) || []).filter((p) => !list.includes(p.name));
  const add = (n) => { n = (n || "").trim().toLowerCase(); if (n && !list.includes(n) && list.length < 12) setList([...list, n]); setQ(""); };
  const move = (i, d) => { const l = [...list], j = i + d; if (j < 0 || j >= l.length) return; [l[i], l[j]] = [l[j], l[i]]; setList(l); };
  const dirty = list.join(",") !== start.join(",");
  return html`<div class="li pin-set"><div class="t"><b>${x.label}</b><span>${x.help}</span></div>
    ${list.length ? html`<ol class="pin-l">${list.map((n, i) => html`<li key=${n}><span class="mut3 xs">${i + 1}</span><b>${n}</b><span class="sp"></span>
      <button class="btn sec sm" aria-label=${"Move " + n + " up"} disabled=${i === 0} onClick=${() => move(i, -1)}>↑</button><button class="btn sec sm" aria-label=${"Move " + n + " down"} disabled=${i === list.length - 1} onClick=${() => move(i, 1)}>↓</button>
      <button class="btn sec sm" aria-label=${"Remove " + n} onClick=${() => setList(list.filter((y) => y !== n))}>Remove</button></li>`)}</ol>` : html`<p class="mut sm">Nothing pinned: the home page shows the most downloaded packages.</p>`}
    <div class="row"><input class="field" list="pin-opts" placeholder="Add a package…" value=${q} aria-label="Add a pinned package" style="width:min(16rem,100%)" disabled=${list.length >= 12}
        onInput=${(e) => setQ(e.target.value)} onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); add(q); } }} />
      <datalist id="pin-opts">${opts.map((p) => html`<option key=${p.name} value=${p.name}>${p.description || ""}</option>`)}</datalist>
      <button class="btn sec sm" disabled=${!q.trim()} onClick=${() => add(q)}>Add</button><span class="sp"></span>
      <button class="btn sm" disabled=${busy || !dirty} onClick=${() => save(list.join(", "))}>Save</button></div></div>`;
}
const ENV_NAME = { cli_git_url: "AIHUB_CLI_GIT_URL", cli_git_branch: "AIHUB_CLI_GIT_BRANCH", cli_git_subdir: "AIHUB_CLI_GIT_SUBDIR", telemetry_url: "AIHUB_TELEMETRY_URL", telemetry_branch: "AIHUB_TELEMETRY_BRANCH", telemetry_inbox_branch: "AIHUB_TELEMETRY_INBOX_BRANCH",
  index_url: "AIHUB_INDEX_URL", index_branch: "AIHUB_INDEX_BRANCH", index_path: "AIHUB_INDEX_PATH" };
function LogoSetting({ url, bump }) {
  const f = useRef(null); const [busy, setBusy] = useState(false);
  const pick = async (e) => { const file = e.target.files[0]; e.target.value = ""; if (!file) return; setBusy(true);
    try { const blob = file.type === "image/png" || file.type === "image/jpeg" || file.type === "image/webp" ? file : await toAvatarBlob(file);
      const r = await fetch(U("/api/v1/admin/logo"), { method: "POST", headers: { Authorization: "Bearer " + tok() }, body: blob });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Upload failed"); toast("Logo updated"); await loadBrand(); bump(); } catch (x) { toast(x.message, 1); } finally { setBusy(false); } };
  const rm = async () => { try { await api("/admin/logo", { method: "DELETE" }); toast("Logo removed"); await loadBrand(); bump(); } catch (x) { toast(x.message, 1); } };
  return html`<div class="li"><div class="t"><b>Logo</b><span>PNG, JPEG or WebP, up to 256 KB and 1024 px. Shown beside the site name.</span></div>
    <div class="row">${url && html`<img decoding="async" class="logo" src=${url} alt="Current logo" />`}<button class="btn sec sm" disabled=${busy} onClick=${() => f.current.click()}>${url ? "Change" : "Upload"}</button>
    ${url && html`<button class="btn danger sm" onClick=${rm}>Remove</button>`}<input ref=${f} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange=${pick} /></div></div>`;
}
function AdminSettings() {
  const [tick, setTick] = useState(0); const d = useLoad(() => api("/admin/settings"), [tick]); const [busy, setBusy] = useState("");
  const f = useFolds("settings", { Site: true, "Home page": false, Access: true, Repositories: false, Groups: false });
  if (d.loading && !d.d) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const set1 = async (k, v) => { setBusy(k); if (k === "cli_endpoint" && v) toast("Checking endpoint…"); try { await api("/admin/settings", { method: "PUT", body: { [k]: v } }); toast("Saved"); if (k === "theme_color") loadBrand(); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const sections = [["Site", ["site_name", "theme_color", "cli_endpoint", "contact_name", "contact_email", "contact_url", "contact_phone", "announcement"]], ["Home page", ["home_highlight", "home_pinned"]], ["Access", ["signup", "login_domains", "public_browse", "public_install"]], ["Repositories", ["allow_private", "default_visibility"]], ["Groups", ["allow_group_creation"]]];
  const by = Object.fromEntries(d.d.schema.map((x) => [x.key, x]));
  const warn = d.d.values.public_install === "1";
  return html`<div class="stack"><div class="card row"><b>Server version</b><span class="mono">v${d.d.server_version}</span><span class="sp"></span><span class="xs mut3">CLIs on this hub self-update to this version (<code>aihub upgrade</code>)</span></div><${FoldBar} f=${f} />${sections.map(([title, keys]) => html`<${Fold} key=${title} f=${f} k=${title} title=${title} badge=${keys.filter((k) => by[k]).length}><div class="list">${title === "Site" && html`<${LogoSetting} url=${d.d.logo_url} bump=${() => setTick(tick + 1)} />`}${keys.filter((k) => by[k]).map((k) => { const x = by[k], v = d.d.values[k]; if (k === "home_pinned") return html`<${PinnedSetting} key=${k} x=${x} v=${v} busy=${busy === k} save=${(n) => set1(k, n)} />`; if (x.type === "text") return html`<${TextSetting} key=${k} x=${x} v=${v} busy=${busy === k} save=${(n) => set1(k, n)} />`; if (x.type === "number") return html`<${NumSetting} key=${k} x=${x} v=${v} busy=${busy === k} save=${(n) => set1(k, n)} />`; const onoff = x.options.length === 2 && x.options[0] === "0";
      return html`<div class="li" key=${k}><div class="t"><b>${x.label}</b><span>${x.help}</span></div>
        ${onoff ? html`<label class="switch"><input type="checkbox" role="switch" checked=${v === "1"} disabled=${busy === k} onChange=${(e) => set1(k, e.target.checked ? "1" : "0")} aria-label=${x.label} /><i></i></label>`
        : html`<div class="seg">${x.options.map((o) => html`<button key=${o} class=${v === o ? "on" : ""} disabled=${busy === k} onClick=${() => v !== o && set1(k, o)}>${o}</button>`)}</div>`}</div>`; })}</div><//>`)}
    ${warn && html`<div class="card" style="border:1px solid var(--orange)"><b>Public install is on.</b> <span class="mut">Anyone who can reach this server can download <u>public</u> packages without signing in. Private packages always require access.</span></div>`}</div>`;
}

const SCHEDULES = [["Every minute", "60"], ["Every 5 min", "300"], ["Every 15 min", "*/15 * * * *"], ["Hourly", "0 * * * *"]];
function SecretField({ label, help, v, onSave }) {
  const [t, setT] = useState("");
  return html`<div class="li"><div class="t"><b>${label}</b><span>${help}</span></div><div class="row"><input class="field" type="password" autocomplete="off" placeholder=${v === "set" ? "•••••••• (set)" : "not set"} value=${t} aria-label=${label} style="width:min(16rem,100%)" onInput=${(e) => setT(e.target.value)} /><button class="btn sm" disabled=${!t} onClick=${async () => { await onSave(t); setT(""); }}>Save</button></div></div>`;
}
function AdminSync() {
  const [tick, setTick] = useState(0); const d = useLoad(() => api("/admin/sync"), [tick]); const [busy, setBusy] = useState("");
  const fd = useFolds("sync", { legacy: false, schedule: true, inbox: false, load: false, index: false, cli: false });
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const { config: c, status: st, from_env: env } = d.d; const ing = st.ingest;
  const dur = (n) => n >= 3600 ? Math.round(n / 360) / 10 + " h" : n >= 60 ? Math.round(n / 60) + " min" : n + " s";
  const save = async (k, v) => { setBusy(k); try { await api("/admin/sync", { method: "PUT", body: { [k]: v } }); toast("Saved"); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const run = async (full) => { setBusy("run"); try { toast(full ? "Full re-sync…" : "Syncing…"); await api("/admin/sync/run" + (full ? "?full=true" : ""), { method: "POST" }); toast("Sync finished"); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const T = (k, label, help, ph) => html`<${TextSetting} x=${{ label, help, kind: "text", max: 300 }} v=${c[k]} busy=${busy === k} save=${(n) => save(k, n)} key=${k} fromEnv=${env && env[k]} envName=${ENV_NAME[k]} />`;
  const when = (t) => t ? new Date(t * 1000).toLocaleString() : "never";
  return html`<div class="stack">
    <div class="card stack"><div class="row"><h3>Sync status</h3><span class="sp"></span><button class="btn sec sm" disabled=${busy === "run"} onClick=${() => run(false)}>Sync now</button><button class="btn sec sm" disabled=${busy === "run"} onClick=${() => run(true)} title="Forget the cursor and look back over everything; duplicates are skipped">Full re-sync</button></div>
      <div class="kpis"><div class="stat"><b>${st.events_total || 0}</b><span>events this run</span></div><div class="stat"><b>${st.index_packages || 0}</b><span>packages in index</span></div><div class="stat"><b>${st.next_in != null ? st.next_in + "s" : "-"}</b><span>next sync in</span></div></div>
      <p class="mut sm">Last run: ${when(st.last_run)}${st.last_error ? html` · <b style="color:var(--red)">${st.last_error}</b>` : html` · <span style="color:var(--green)">OK</span>`}</p>
      ${st.publish_error && html`<p class="sm"><b style="color:var(--red)">A published package is not in the index yet (retrying): ${st.publish_error}</b></p>`}
      <p class="mut sm">Archive: ${c.telemetry_archive === "0" ? "off" : html`${st.archive_last ? "last written " + when(st.archive_last) : "nothing written since the server started"}${st.archive_batches ? html` · ${st.archive_batches} batch(es) this run` : ""}${st.archive_files ? html` · ${st.archive_files} file(s) on <code>${c.telemetry_branch || "telemetry-archive"}</code>` : ""}${st.archive_commit ? html` · commit <code>${st.archive_commit}</code>` : ""}`}${st.inbox_pending ? html` · ${st.inbox_pending} batch(es) were waiting in the inbox` : ""}</p></div>
    <${FoldBar} f=${fd} /><${Fold} f=${fd} k="legacy" title="Legacy git sync" hint=${c.git_telemetry_sync === "1" || c.git_index_sync === "1" ? "in use" : "off"}><div class="list"><div class="li"><div class="t"><b>Git telemetry inbox sync</b><span>Legacy, off by default. Reads usage batches from the git inbox and archives events to git. Events sent to the hub over HTTP are unaffected.</span></div><label class="switch"><input type="checkbox" role="switch" checked=${c.git_telemetry_sync === "1"} disabled=${busy === "git_telemetry_sync"} onChange=${(e) => save("git_telemetry_sync", e.target.checked ? "1" : "0")} aria-label="Git telemetry inbox sync" /><i></i></label></div><div class="li"><div class="t"><b>Git index sync</b><span>Legacy, off by default. Reads the package index from git and pushes publishes back to it. Sync now runs both regardless.</span></div><label class="switch"><input type="checkbox" role="switch" checked=${c.git_index_sync === "1"} disabled=${busy === "git_index_sync"} onChange=${(e) => save("git_index_sync", e.target.checked ? "1" : "0")} aria-label="Git index sync" /><i></i></label></div></div><//>
    <${Fold} f=${fd} k="schedule" title="Schedule" hint=${/^\d+$/.test(c.sync_interval) ? "every " + dur(+c.sync_interval) : c.sync_interval}><div class="list"><div class="li"><div class="t"><b>Pull interval</b><span>Seconds (10-86400) or a cron expression: <code>*/5 * * * *</code> every 5 min, <code>0 3 * * *</code> daily at 03:00. Applies immediately.</span></div>
      <div class="row"><input class="field" id="sched" defaultValue=${c.sync_interval} style="width:11rem" aria-label="Pull interval" /><button class="btn sm" disabled=${busy === "sync_interval"} onClick=${() => save("sync_interval", document.getElementById("sched").value)}>Save</button></div></div>
      <div class="li"><div class="t"><b>Presets</b></div><div class="seg">${SCHEDULES.map(([n, v]) => html`<button key=${v} class=${c.sync_interval === v ? "on" : ""} onClick=${() => save("sync_interval", v)}>${n}</button>`)}</div></div></div><//>
    <${Fold} f=${fd} k="inbox" title="Usage inbox"><div class="list">${T("telemetry_url", "Git URL", "Repo CLIs push usage batches to (refs/telemetry/*). Empty = the default, then the package index repo")}${T("telemetry_inbox_branch", "Inbox branch", "default: telemetry-inbox. The one branch CLIs add usage files to; the server empties it")}${T("telemetry_branch", "Archive branch", "default: telemetry-archive")}
      <div class="li"><div class="t"><b>Archive batches to git</b><span>Before deleting an ingested batch, copy it to the archive branch so git keeps a durable copy of every event.</span></div><label class="switch"><input type="checkbox" role="switch" checked=${c.telemetry_archive !== "0"} disabled=${busy === "telemetry_archive"} onChange=${(e) => save("telemetry_archive", e.target.checked ? "1" : "0")} aria-label="Archive batches" /><i></i></label></div>
      <div class="li"><div class="t"><b>Home page “Happening now” shows</b><span>Which events appear in the live feed. None ticked = default (install, use, update, publish).</span></div><div class="seg">${DASH_KINDS.map((k) => { const cur = (c.live_kinds || "").split(",").filter(Boolean), on = cur.includes(k); return html`<button key=${k} class=${on ? "on" : ""} aria-pressed=${on} disabled=${busy === "live_kinds"} onClick=${() => save("live_kinds", (on ? cur.filter((x) => x !== k) : [...cur, k]).join(","))}>${k}</button>`; })}</div></div>
      <div class="li"><div class="t"><b>Localize user names for analysis</b><span>Count one person once, by name only: a signed-in account, an anonymous run on that person's machine (OS user) and an e-mail all merge into the same lowercase name in rankings, the dashboard and top developers.</span></div><label class="switch"><input type="checkbox" role="switch" checked=${c.localize_users !== "0"} disabled=${busy === "localize_users"} onChange=${(e) => save("localize_users", e.target.checked ? "1" : "0")} aria-label="Localize user names" /><i></i></label></div></div><//>
    <${Fold} f=${fd} k="load" title="Throughput and git load">
      <p class="mut sm">CLIs send usage to this hub first and read the package index from it; git is only their fallback. Events are held in memory and written in batches, and git is touched rarely and never at the same time as the backup. Blank = default.</p>
      <p class="mut sm">${ing ? html`In memory now: ${ing.buffered || 0} event(s) · ${ing.stored || 0} stored since start in ${ing.flushes || 0} write(s) · ${ing.duplicates || 0} repeat(s) skipped · ${ing.rate_limited || 0} × 429 · ${ing.busy || 0} × 503` : ""}${st.held_batches ? html` · ${st.held_batches} batch(es) waiting for the git push${st.git_flush_in != null ? " (in " + dur(st.git_flush_in) + ")" : ""}` : ""}${st.staged_publishes ? html` · ${st.staged_publishes} publish(es) served, waiting for the index push${st.index_push_in != null ? " (in " + dur(st.index_push_in) + ")" : ""}` : ""}${st.deferred ? html` · <b>${st.deferred}</b>` : ""}</p>
      <div class="list">${(d.d.limits || []).filter((x) => x.key !== "backup_jitter" && x.key !== "backup_jitter_max").map((x) => html`<${TextSetting} x=${{ label: x.label, help: "Allowed " + x.min + " to " + x.max + ", default " + x.default + ".", kind: "text", max: 7 }} v=${c[x.key]} busy=${busy === x.key} save=${(n) => save(x.key, n)} key=${x.key} />`)}</div><//>
    <${Fold} f=${fd} k="index" title="Package index"><div class="list">${T("index_url", "Git URL", "Repository holding the package index")}${T("index_branch", "Branch", "default: main")}${T("index_path", "Folder", "default: index (a folder index; an old index.json file also works)")}</div><//>
    <${Fold} f=${fd} k="cli" title="CLI setup"><div class="list">
      ${T("cli_git_url", "CLI git URL", "Where to pip install the CLI from when this server is unreachable")}${T("cli_git_branch", "Branch (optional)", "Leave empty to hide. Install from this branch instead of the default")}${T("cli_git_subdir", "Sub folder (optional)", "Leave empty to hide. Install from this folder of the repo")}${T("client_refresh_hours", "CLI refresh (hours)", "How often installed CLIs re-pull these settings (default 24)")}</div><//></div>`;
}

const UPD_STATE = { confirmed: ["green", "Updated"], probation: ["orange", "Watching"], applied: ["green", "Switched"], available: ["blue", "Available"], up_to_date: ["", "Up to date"],
  failed: ["red", "Failed"], rolled_back: ["red", "Rolled back"], rollback_failed: ["red", "Rollback failed"], skipped: ["", "Skipped"], running: ["blue", "Running"], conflict: ["orange", "Needs you"] };
const UPD_SCHEDULES = [["Hourly", "0 * * * *"], ["Every 6 hours", "0 */6 * * *"], ["Daily 04:00", "0 4 * * *"], ["Weekly (Sun 04:00)", "0 4 * * 0"]];
const STAGE_ICON = { done: "✓", failed: "✕", blocked: "!", warn: "!", skipped: "–" };
const UpdBadge = ({ s }) => { const [c, t] = UPD_STATE[s] || ["", s]; return html`<span class=${"badge " + c}>${t}</span>`; };
function UpdStages({ stages, compact }) {
  if (!stages || !stages.length) return null;
  return html`<ol class=${"stages" + (compact ? " compact" : "")} aria-label="Update progress">${stages.map((s) => {
    const pct = s.progress && s.progress[1] ? Math.min(100, Math.round(100 * s.progress[0] / s.progress[1])) : null;
    const secs = s.started && s.finished ? ((s.finished - s.started)).toFixed(1) + " s" : "";
    return html`<li key=${s.key} class=${"stg " + s.state} aria-current=${s.state === "running" ? "step" : null}>
      <span class="dot" aria-hidden="true">${STAGE_ICON[s.state] || ""}</span><span class="lbl">${s.label}</span>
      <span class="sub">${s.state === "running" && pct !== null ? pct + "%" : s.detail || (s.state === "pending" ? "waiting" : s.state)}${secs && s.state === "done" ? " · " + secs : ""}</span>
      ${s.state === "running" && pct !== null && html`<i class="bar"><b style=${"width:" + pct + "%"}></b></i>`}</li>`;
  })}</ol>`;
}
function UpdRun({ id }) {
  const [run, setRun] = useState(null);
  useEffect(() => { let on = 1, t; setRun(null); const go = async () => { try { const r = await api("/admin/update/runs/" + id); if (!on) return; setRun(r); if (r.state === "running" || r.state === "probation") t = setTimeout(go, 1500); } catch { if (on) t = setTimeout(go, 3000); } }; go(); return () => { on = 0; clearTimeout(t); }; }, [id]);
  if (!run) return html`<div class="skel"></div>`;
  return html`<div class="stack"><${UpdStages} stages=${run.stages} />
    <div class="updlog" role="log" aria-label="Update log">${run.log.map((e, i) => html`<div key=${i} class=${"ul " + e.l}><time>${new Date(e.t * 1000).toLocaleTimeString()}</time><span>${e.m}</span></div>`)}</div></div>`;
}
const blockPick = (v) => (v === undefined || v === null ? "" : typeof v === "object" ? "custom" : v);
function ConflictBlock({ b, val, set }) {
  const pick = blockPick(val);
  return html`<div class=${"cblk" + (pick ? " done" : "")}>
    <div class="cols"><div class=${"side" + (pick === "ours" || pick === "both" ? " sel" : "")}><b>Mine (this hub)</b><pre>${b.ours || "(empty)"}</pre></div>
      <div class=${"side" + (pick === "theirs" || pick === "both" ? " sel" : "")}><b>Incoming (update)</b><pre>${b.theirs || "(empty)"}</pre></div></div>
    ${pick === "custom" && html`<textarea class="field mono" rows="6" aria-label="Edit the result" value=${val.text} onInput=${(e) => set({ text: e.target.value })}></textarea>`}
    <div class="seg" role="group" aria-label="Choose for this block">${[["Keep mine", "ours"], ["Take incoming", "theirs"], ["Both", "both"]].map(([n, v]) => html`<button key=${v} class=${pick === v ? "on" : ""} aria-pressed=${pick === v} onClick=${() => set(v)}>${n}</button>`)}
      <button class=${pick === "custom" ? "on" : ""} aria-pressed=${pick === "custom"} onClick=${() => set({ text: pick === "custom" ? val.text : b.ours + b.theirs })}>Edit</button></div></div>`;
}
function Context({ text, first, last }) {
  const lines = text.replace(/\n$/, "").split("\n");
  let shown = lines, gap = 0;
  if (lines.length > 7) { const a = first ? 0 : 3, z = last ? 0 : 3; shown = [...lines.slice(0, a), null, ...lines.slice(lines.length - z)]; gap = lines.length - a - z; }
  return html`<pre class="ctx">${shown.map((l) => (l === null ? "⋯ " + gap + " unchanged lines ⋯" : l)).join("\n")}</pre>`;
}
const fileDone = (f, r) => !!r && (r.mode === "ours" || r.mode === "theirs" || (r.mode === "hunks" && r.choices.length === f.conflicts && r.choices.every((c) => c !== null && c !== undefined)));
function ConflictResolver({ busy, submit }) {
  const d = useLoad(() => api("/admin/update/conflict"), []); const [res, setRes] = useState({});
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const files = d.d.files; const left = files.filter((f) => !fileDone(f, res[f.path])).length;
  const put = (p, v) => setRes((r) => ({ ...r, [p]: v }));
  const setBlock = (f, bi, v) => { const cur = res[f.path]; const ch = cur && cur.mode === "hunks" ? cur.choices.slice() : Array(f.conflicts).fill(null); ch[bi] = v; put(f.path, { mode: "hunks", choices: ch }); };
  return html`<div class="stack resolver">${files.map((f) => {
    const r = res[f.path]; let bi = -1; const n = f.segments ? f.segments.length : 0;
    return html`<section class="cfile" key=${f.path} aria-label=${"Conflict in " + f.path}>
      <div class="row"><b class="mono">${f.path}</b><span class=${"badge " + (fileDone(f, r) ? "green" : "orange")}>${fileDone(f, r) ? "resolved" : f.kind === "text" ? f.conflicts + " block" + (f.conflicts === 1 ? "" : "s") : f.kind}</span><span class="sp"></span>
        <div class="seg" role="group" aria-label="Whole file"><button class=${r && r.mode === "ours" ? "on" : ""} aria-pressed=${!!r && r.mode === "ours"} onClick=${() => put(f.path, { mode: "ours" })}>Keep mine</button>
          <button class=${r && r.mode === "theirs" ? "on" : ""} aria-pressed=${!!r && r.mode === "theirs"} onClick=${() => put(f.path, { mode: "theirs" })}>Take incoming</button></div></div>
      ${f.note && html`<p class="mut sm">${f.note}</p>`}
      ${f.kind === "text" && f.segments.map((sg, si) => { if (sg.t === "text") return html`<${Context} key=${si} text=${sg.text} first=${si === 0} last=${si === n - 1} />`; bi += 1; const k = bi;
        return html`<${ConflictBlock} key=${si} b=${sg} val=${r && r.mode === "hunks" ? r.choices[k] : undefined} set=${(v) => setBlock(f, k, v)} />`; })}</section>`; })}
    <div class="row"><span class="mut sm">${left ? left + " file" + (left === 1 ? "" : "s") + " still to decide" : "All files decided."}</span><span class="sp"></span>
      <button class="btn" disabled=${left > 0 || busy} onClick=${() => submit(res)}>Apply and update</button></div></div>`;
}
function AdminUpdates() {
  const [d, setD] = useState(null); const [err, setErr] = useState(""); const [busy, setBusy] = useState(""); const [open, setOpen] = useState(""); const [tick, setTick] = useState(0);
  const [runs, setRuns] = useState([]); const [vers, setVers] = useState([]); const [down, setDown] = useState(false); const [resolve, setResolve] = useState(false);
  const fd = useFolds("updates", { auto: true, versions: true, history: true });
  const load = async () => { try { const [v, r] = await Promise.all([api("/admin/update"), api("/admin/update/runs")]); setD(v); setRuns(r.runs); setErr(""); setDown(false); if (v.git) setVers((await api("/admin/update/versions")).versions); } catch (e) { if (d) setDown(true); else setErr(e.message); } };
  useEffect(() => { load(); }, [tick]);
  const live = d && (d.status.running || (d.pending && d.pending.state === "probation")) || down;
  useEffect(() => { if (!live) return; const t = setInterval(() => setTick((x) => x + 1), 2500); return () => clearInterval(t); }, [live]);
  if (err) return html`<${Err} e=${err} />`; if (!d) return html`<div class="skel"></div>`;
  const { config: c, status: st, source: src, pending: pd, conflict: cf } = d;
  const on = c.update_enabled === "1"; const envLock = d.from_env.update_enabled;
  const act = async (k, path, opts, ok) => { setBusy(k); try { await api(path, Object.assign({ method: "POST" }, opts)); toast(ok); setTick((x) => x + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const save = async (k, v) => { setBusy(k); try { await api("/admin/update", { method: "PUT", body: { [k]: v } }); toast("Saved"); setTick((x) => x + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const back = (v) => { if (confirm("Roll back to " + v.short + " (" + v.subject + ")? The hub is verified, switched and restarted, then watched.")) act("rb-" + v.sha, "/admin/update/rollback", { body: { target: v.sha } }, "Rollback started"); };
  const sendResolution = async (files) => { setBusy("resolve"); try { await api("/admin/update/resolve", { method: "POST", body: { files } }); toast("Update started"); setResolve(false); setTick((x) => x + 1); } catch (e) { toast(e.message, 1); setTick((x) => x + 1); } finally { setBusy(""); } };
  const when = (t) => t ? new Date(t * 1000).toLocaleString() : "never";
  const next = st.next_in >= 3600 ? Math.round(st.next_in / 3600) + " h" : st.next_in >= 60 ? Math.round(st.next_in / 60) + " min" : st.next_in + " s";
  const av = st.available; const idle = !st.running && !(pd && pd.state === "probation");
  const liveId = st.running || (pd && pd.state === "probation" && pd.run) || "";
  const sw = (k, label, help) => html`<div class="li"><div class="t"><b>${label}</b><span>${help}</span></div><label class="switch"><input type="checkbox" role="switch" checked=${c[k] === "1"} disabled=${busy === k} onChange=${(e) => save(k, e.target.checked ? "1" : "0")} aria-label=${label} /><i></i></label></div>`;
  const sel = (k, label, help, opts) => html`<div class="li"><div class="t"><b>${label}</b><span>${help}</span></div><div class="seg">${opts.map(([n, v]) => html`<button key=${v} class=${c[k] === v ? "on" : ""} aria-pressed=${c[k] === v} disabled=${busy === k} onClick=${() => save(k, v)}>${n}</button>`)}</div></div>`;
  const mode = !on ? "Auto-update is off: nothing runs by itself. “Check now” and “Update now” still work."
    : c.update_auto_apply === "1" ? "Auto-update is on: it checks " + c.update_schedule + " and installs a version by itself once it passes verification. A conflict it cannot resolve waits for you here."
    : "Auto-update is on: it checks " + c.update_schedule + " and only tells you when a version is available. You press “Update now”.";
  const last = runs[0];
  return html`<div class="stack">
    ${down && html`<div class="card" role="status"><b>The hub is restarting…</b> <span class="mut sm">This page reconnects by itself.</span></div>`}
    ${cf && html`<div class="card stack conflict" role="alert"><div class="row"><h3>Manual update required</h3><span class="sp"></span><${UpdBadge} s="conflict" /></div>
      <p class="sm">${cf.files.length} file${cf.files.length === 1 ? "" : "s"} could not be merged automatically${cf.version ? " (incoming v" + cf.version + ")" : ""}. The hub keeps running <code>${src ? src.short : "the current version"}</code>; nothing was changed. Decide what to keep, and the update continues from there.</p>
      <div class="list">${cf.files.map((f) => html`<div class="li" key=${f.path}><div class="t"><b class="mono">${f.path}</b><span>${f.note || (f.kind === "text" ? f.conflicts + " conflicting block" + (f.conflicts === 1 ? "" : "s") : f.kind)}</span></div></div>`)}</div>
      <div class="row"><button class="btn" onClick=${() => setResolve(!resolve)} aria-expanded=${resolve}>${resolve ? "Hide resolver" : "Resolve conflicts"}</button>
        <button class="btn sec" disabled=${!idle} onClick=${() => act("dismiss", "/admin/update/conflict", { method: "DELETE" }, "Dismissed")} title="Forget this conflict; the next check looks again">Dismiss</button></div>
      ${resolve && html`<${ConflictResolver} busy=${busy === "resolve"} submit=${sendResolution} />`}</div>`}
    ${pd && pd.state === "probation" && html`<div class="card stack" role="status"><div class="row"><h3>Watching the new version</h3><span class="sp"></span><${UpdBadge} s="probation" /></div>
      <p class="mut sm">The hub switched to <code>${pd.to.slice(0, 8)}</code> and checks its own health for ${pd.probation} s. If it fails, it goes back to <code>${pd.from.slice(0, 8)}</code> on its own.</p></div>`}
    ${!d.git && html`<div class="card"><b>Self-update is not available</b><p class="mut sm">${d.why}. Run the hub from a git clone (<code>git clone … && python -m aihub.server</code>) to use it.</p></div>`}
    <div class="card stack"><div class="row wrap"><h3>Software update</h3><span class="sp"></span>
      <div class="row autosw" title=${envLock ? "Set by environment (AIHUB_UPDATE_ENABLED)" : "Turn scheduled automatic updates on or off"}><b id="au-l">Auto-update</b><span class=${"badge " + (on ? "green" : "")}>${on ? "On" : "Off"}</span>
        <label class="switch"><input type="checkbox" role="switch" aria-labelledby="au-l" checked=${on} disabled=${busy === "update_enabled" || envLock || !d.git} onChange=${(e) => save("update_enabled", e.target.checked ? "1" : "0")} /><i></i></label></div></div>
      <p class="mut sm" role="status">${mode}${envLock ? " Set by environment (AIHUB_UPDATE_ENABLED): that value is used and this switch is locked." : ""}</p>
      <div class="row wrap">
        <button class="btn sec sm" disabled=${!d.git || !idle || busy === "check"} onClick=${() => act("check", "/admin/update/check", {}, "Checking…")} title="Fetch and compare; nothing is changed">Check now</button>
        <button class="btn sm" disabled=${!d.git || !idle || busy === "run"} onClick=${() => act("run", "/admin/update/run", {}, "Update started")} title="Fetch, merge, verify, switch and restart">Update now</button>
        ${src && src.skip && av && av.tip === src.skip && html`<button class="btn sec sm" disabled=${!idle} onClick=${() => act("force", "/admin/update/run?force=1", {}, "Retrying…")} title="This commit failed before; try it again">Update anyway</button>`}</div>
      ${src && html`<div class="kpis"><div class="stat"><b>${src.short}</b><span>running commit${src.dirty ? " (local changes)" : ""}</span></div><div class="stat"><b>${av ? "+" + av.behind : "0"}</b><span>${av ? "commits to pull" + (av.version ? " · v" + av.version : "") : "up to date"}</span></div>
        <div class="stat"><b>${on ? next : "-"}</b><span>next automatic check</span></div><div class="stat"><b>${st.running ? "…" : st.last_check ? ago(st.last_check) : "-"}</b><span>last check</span></div></div>
      <p class="mut sm">Source: <code>${src.url || "(none)"}</code>, branch <code>${src.branch}</code>. Checkout: <code>${src.path}</code>.</p>`}
      ${av && av.subjects.length > 0 && html`<div class="list">${av.subjects.map((x, i) => html`<div class="li" key=${i}><div class="t mono">${x}</div></div>`)}</div>`}
      ${st.last_error && html`<p role="alert" class="sm" style="color:var(--red)">Last problem: ${st.last_error}</p>`}</div>
    ${(liveId || (last && last.stages)) && html`<div class="card stack"><div class="row"><h3>${liveId ? "Progress" : "Latest run"}</h3><span class="sp"></span>${!liveId && last && html`<${UpdBadge} s=${last.state} />`}</div>
      ${liveId ? html`<${UpdRun} id=${liveId} />` : html`<${UpdStages} stages=${last.stages} /><p class="mut sm">${last.title || ""} · ${when(last.ts)}</p>`}</div>`}
    <${FoldBar} f=${fd} />
    <${Fold} f=${fd} k="auto" title="Automatic update" hint=${on ? (c.update_auto_apply === "1" ? "installs by itself" : "reports only") : "off"}><div class="list">
      ${sw("update_auto_apply", "Apply automatically", "On: a new version that passes verification is switched in at once. Off: the schedule only looks and reports “available”.")}
      <div class="li"><div class="t"><b>Schedule</b><span>Seconds (10-86400) or a cron expression: <code>0 4 * * *</code> daily at 04:00.</span></div>
        <div class="row"><input class="field" id="up-sched" defaultValue=${c.update_schedule} style="width:11rem" aria-label="Update schedule" /><button class="btn sm" disabled=${busy === "update_schedule"} onClick=${() => save("update_schedule", document.getElementById("up-sched").value)}>Save</button></div></div>
      <div class="li"><div class="t"><b>Presets</b></div><div class="seg">${UPD_SCHEDULES.map(([n, v]) => html`<button key=${v} class=${c.update_schedule === v ? "on" : ""} onClick=${() => save("update_schedule", v)}>${n}</button>`)}</div></div>
      ${sel("update_conflict", "If local changes conflict", "Ask me: clean merges go through, a real conflict waits here for you to resolve it side by side. Remote wins: the new code replaces the clashing files. Local wins: your version stays. Stop: the update fails and nothing changes.", [["Ask me", "manual"], ["Remote wins", "remote"], ["Local wins", "local"], ["Stop", "abort"]])}
      ${sw("update_tests", "Run the test suite", "Slow (a few minutes), but the strictest check. The boot test always runs.")}
      ${sw("update_backup", "Back up the database first", "Only when scheduled backups are on.")}
      ${sw("update_pip", "Install dependencies", "If requirements changed, run pip install in the hub’s Python before the boot test.")}
      ${sel("update_restart", "Restart method", "Replace this process by itself, or exit and let systemd, Docker or another supervisor start it again.", [["Restart itself", "exec"], ["Exit (supervisor)", "exit"]])}
      <${TextSetting} x=${{ label: "Probation (seconds)", help: "After a switch the new version must stay healthy this long (10-900, default 60), or it is rolled back.", kind: "text", max: 3 }} v=${c.update_probation} busy=${busy === "update_probation"} save=${(n) => save("update_probation", n)} />
      <${TextSetting} x=${{ label: "Source URL", help: "Blank = this checkout’s origin. A different git URL to pull from (the server’s git login is used).", kind: "text", max: 300 }} v=${c.update_url} busy=${busy === "update_url"} save=${(n) => save("update_url", n)} />
      <${TextSetting} x=${{ label: "Source branch", help: "Blank = the checked-out branch’s upstream.", kind: "text", max: 100 }} v=${c.update_branch} busy=${busy === "update_branch"} save=${(n) => save("update_branch", n)} /></div><//>
    <${Fold} f=${fd} k="versions" title="Versions" badge=${vers.length || ""}>
      <p class="mut sm">Roll back to any commit of this checkout. It is verified like an update, switched, restarted and watched.</p>
      ${vers.length ? html`<div class="list vscroll" tabindex="0" role="region" aria-label="Versions, scrollable">${vers.map((v) => html`<div class="li" key=${v.sha}><div class="t"><b class="mono">${v.short} ${v.current ? html`<span class="badge green">running</span>` : null} ${v.deployed ? html`<span class="badge blue">deployed${v.deployed.version ? " v" + v.deployed.version : ""}</span>` : null}</b><span>${v.subject} · ${ago(v.ts)}${v.author ? " · " + v.author : ""}</span></div>
        ${!v.current && html`<button class="btn sec sm" disabled=${!d.git || !idle || busy === "rb-" + v.sha} onClick=${() => back(v)}>Roll back</button>`}</div>`)}</div>` : html`<p class="mut sm">No history available.</p>`}<//>
    <${Fold} f=${fd} k="history" title="History" badge=${runs.length || ""}>
      ${runs.length ? html`<div class="list vscroll" tabindex="0" role="region" aria-label="Update history, scrollable">${runs.map((r) => html`<div key=${r.id}><button class="li updrow" aria-expanded=${open === r.id} onClick=${() => setOpen(open === r.id ? "" : r.id)}><div class="t"><b>${r.kind === "rollback" ? "Rollback" : "Update"} <${UpdBadge} s=${r.state} /></b><span>${r.title || ""}${r.version_from || r.version_to ? " · " + (r.version_from || "?") + " → " + (r.version_to || "?") : ""} · ${when(r.ts)} · ${r.trigger}${r.user ? " by " + r.user : ""}</span>
        ${r.stages && open !== r.id && html`<${UpdStages} stages=${r.stages} compact=${true} />`}</div></button>
        ${open === r.id && html`<${UpdRun} id=${r.id} />`}</div>`)}</div>` : html`<p class="mut sm">Nothing yet. A scheduled check that finds no new commit is not recorded.</p>`}<//></div>`;
}

const BACKUP_SCHEDULES = [["Hourly", "0 * * * *"], ["Every 6 hours", "0 */6 * * *"], ["Daily 03:00", "0 3 * * *"], ["Weekly (Sun 03:00)", "0 3 * * 0"]];
function GitLogins() {
  const [tick, setTick] = useState(0); const d = useLoad(() => api("/admin/git-login"), [tick]); const [busy, setBusy] = useState(""); const [f, setF] = useState({ host: "", user: "", secret: "" });
  if (d.loading && !d.d) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const { logins, error, suggest } = d.d; const host = f.host || (suggest && suggest[0]) || "";
  const call = async (k, fn, ok) => { setBusy(k); try { const r = await fn(); if (r && r.message) toast(r.message, r.ok ? 0 : 1); else if (ok) toast(ok); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const save = () => call("save", () => api("/admin/git-login", { method: "PUT", body: { host, user: f.user, secret: f.secret } }).then(() => setF({ host: "", user: "", secret: "" })), "Git login saved");
  const when = (t) => t ? new Date(t * 1000).toLocaleString() : "";
  return html`<div class="stack"><h3>Git login (fallback)</h3>
    <p class="mut sm">Used by backup, sync and installs only when the server’s usual git login (AIHUB_GIT_TOKEN, the vault, git’s own helpers) is missing or refused, so nothing changes while that works. Stored encrypted in the data folder, never in the database or a backup, and never shown again. Use a personal access token with read and write access to the backup repo.</p>
    ${error && html`<p class="sm" role="alert" style="color:var(--red)">Saved logins cannot be read: ${error}. Remove them and save again.</p>`}
    <div class="list">${logins.map((l) => html`<div class="li" key=${l.host}><div class="t"><b>${l.host}</b><span>user <code>${l.user}</code> · token saved${l.by ? " by " + l.by : ""}${l.ts ? " · " + when(l.ts) : ""}</span></div>
        <div class="row"><button class="btn sec sm" disabled=${!!busy} onClick=${() => call("test" + l.host, () => api("/admin/git-login/test", { method: "POST", body: { host: l.host } }))}>Test</button>
        <button class="btn sec sm" disabled=${!!busy} onClick=${() => call("rm" + l.host, () => api("/admin/git-login/" + encodeURIComponent(l.host), { method: "DELETE" }), "Removed")}>Remove</button></div></div>`)}
      <form class="li" onSubmit=${(e) => { e.preventDefault(); if (host && f.secret) save(); }}><div class="t"><b>${logins.some((l) => l.host === host) ? "Replace login" : "Add login"}</b><span>Host (or repo URL), user name (blank = oauth2; Bitbucket/Gitea want the real account), token.</span></div>
        <div class="row" style="flex-wrap:wrap"><input class="field" aria-label="Git host" placeholder="git.example.com" value=${host} onInput=${(e) => setF({ ...f, host: e.target.value })} style="width:13rem" />
          <input class="field" aria-label="User name" placeholder="user (optional)" autocomplete="off" value=${f.user} onInput=${(e) => setF({ ...f, user: e.target.value })} style="width:9rem" />
          <input class="field" type="password" aria-label="Token" placeholder="token" autocomplete="new-password" value=${f.secret} onInput=${(e) => setF({ ...f, secret: e.target.value })} style="width:13rem" />
          <button class="btn sm" type="submit" disabled=${busy === "save" || !host || !f.secret}>Save</button></div></form></div></div>`;
}
function AdminBackup() {
  const [tick, setTick] = useState(0); const d = useLoad(() => api("/admin/backup"), [tick]); const [busy, setBusy] = useState("");
  if (d.loading) return html`<div class="skel"></div>`; if (d.e) return html`<${Err} e=${d.e} />`;
  const { config: c, status: st, from_env: env, target: tg } = d.d;
  const on = c.backup_enabled === "1";
  const save = async (k, v) => { setBusy(k); try { await api("/admin/backup", { method: "PUT", body: { [k]: v } }); toast("Saved"); setTick(tick + 1); } catch (e) { toast(e.message, 1); } finally { setBusy(""); } };
  const run = async () => { setBusy("run"); try { toast("Backing up…"); await api("/admin/backup/run", { method: "POST" }); toast("Backup finished"); setTick(tick + 1); } catch (e) { toast(e.message, 1); setTick(tick + 1); } finally { setBusy(""); } };
  const when = (t) => t ? new Date(t * 1000).toLocaleString() : "never";
  const next = st.next_in != null ? (st.next_in >= 3600 ? Math.round(st.next_in / 3600) + " h" : st.next_in >= 60 ? Math.round(st.next_in / 60) + " min" : st.next_in + " s") : "-";
  return html`<div class="stack">
    <div class="card stack"><div class="row"><h3>Backup status</h3><span class="sp"></span><button class="btn sec sm" disabled=${busy === "run"} onClick=${run} title="Back up now, whether or not the schedule is on">Back up now</button></div>
      <div class="kpis"><div class="stat"><b>${on ? "On" : "Off"}</b><span>schedule</span></div><div class="stat"><b>${on ? next : "-"}</b><span>next backup in</span></div><div class="stat"><b>${st.rows || 0}</b><span>rows in last backup</span></div><div class="stat"><b>${st.files || 0}</b><span>files</span></div></div>
      <p class="mut sm">Last run: ${when(st.last_run)}${st.last_error ? html` · <b style="color:var(--red)">${st.last_error}</b>` : st.last_run ? html` · <span style="color:var(--green)">OK</span>` : ""}${st.last_commit ? html` · commit <code>${st.last_commit}</code>` : ""}</p>
      <p class="mut sm">Tables are exported as small gzipped files under <code>backups/</code> in the package index repo${tg && tg.url ? html` (<code>${tg.url}</code>, branch <code>${tg.branch}</code>)` : ""}. Passwords, tokens and secret settings are hashed first. If the database is ever lost, the server restores from this.</p></div>
    <div class="stack"><h3>Schedule</h3><div class="list">
      <div class="li"><div class="t"><b>Scheduled backups</b><span>${on ? "Runs on the schedule below." : "Off: nothing runs on a schedule. “Back up now” still works."}</span>${env && env.backup_enabled && html`<span class="mut sm" role="note">Set by environment (AIHUB_BACKUP_ENABLED): that value is used, this switch is ignored.</span>`}</div>
        <label class="switch"><input type="checkbox" role="switch" checked=${on} disabled=${busy === "backup_enabled" || (env && env.backup_enabled)} onChange=${(e) => save("backup_enabled", e.target.checked ? "1" : "0")} aria-label="Scheduled backups" /><i></i></label></div>
      <div class="li"><div class="t"><b>Backup schedule</b><span>Seconds (10-86400) or a cron expression: <code>0 3 * * *</code> daily at 03:00. Applies immediately.</span></div>
        <div class="row"><input class="field" id="bk-sched" defaultValue=${c.backup_schedule} style="width:11rem" aria-label="Backup schedule" /><button class="btn sm" disabled=${busy === "backup_schedule"} onClick=${() => save("backup_schedule", document.getElementById("bk-sched").value)}>Save</button></div></div>
      <div class="li"><div class="t"><b>Presets</b></div><div class="seg">${BACKUP_SCHEDULES.map(([n, v]) => html`<button key=${v} class=${c.backup_schedule === v ? "on" : ""} onClick=${() => save("backup_schedule", v)}>${n}</button>`)}</div></div>
      <${TextSetting} x=${{ label: "Rows per file", help: "Events and audit log are split into files of this many rows (1000-1000000, default 20000). Smaller files make smaller git diffs.", kind: "text", max: 7 }} v=${c.backup_events_chunk} busy=${busy === "backup_events_chunk"} save=${(n) => save("backup_events_chunk", n)} />
      <${TextSetting} x=${{ label: "Start time spread (%)", help: "Each run starts up to this percent of the wait earlier or later at random (0-50, default 10), so backups never form a fixed pattern on the git host.", kind: "text", max: 3 }} v=${c.backup_jitter} busy=${busy === "backup_jitter"} save=${(n) => save("backup_jitter", n)} />
      <${TextSetting} x=${{ label: "Spread limit (seconds)", help: "...but never by more than this (0-3600, default 600).", kind: "text", max: 5 }} v=${c.backup_jitter_max} busy=${busy === "backup_jitter_max"} save=${(n) => save("backup_jitter_max", n)} />
      <div class="li"><div class="t"><b>Gap between sync and backup</b><span>The backup and the event sync never use git within the set time of each other (Sync > Throughput and git load, “Minimum seconds between git work”). A job that has to wait just waits.${st.waiting ? " A backup is waiting for it right now." : ""}</span></div></div></div></div>
    <${GitLogins} /></div>`;
}
const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
function Groups() {
  const { me } = useStore(); const [tick, setTick] = useState(0); const bump = () => setTick((x) => x + 1);
  const caps = useLoad(() => me ? api("/groups/capabilities") : Promise.resolve(null), [me && me.username, tick]);
  const d = useLoad(() => me ? api("/groups") : Promise.resolve({ groups: [] }), [me && me.username, tick]);
  const [sel, setSel] = useState(null); const [nn, setNn] = useState(""); const [nd, setNd] = useState(""); const [q, setQ] = useState(""); const [pane, setPane] = useState("list"); const [mk, setMk] = useState(false);
  if (!me) return html`<${Empty} t="Sign in required" d="Sign in to see your groups." />`;
  if (!d.d) return d.e ? html`<${Err} e=${d.e} />` : html`<div class="skel"></div>`;
  const c = caps.d || {}; const all = d.d.groups;
  const create = async () => { try { await api("/groups", { method: "POST", body: { name: nn, description: nd } }); setSel(nn.trim().toLowerCase()); setPane("edit"); setNn(""); setNd(""); setMk(false); toast("Group created"); bump(); } catch (e) { toast(e.message, 1); } };
  const f = q.trim().toLowerCase(), list = all.filter((g) => !f || g.name.toLowerCase().includes(f));
  const pick = (n) => { setSel(n); setPane("edit"); };
  return html`<div class="stack"><div class="kn-head"><div><h1>Groups</h1><p class="lead">${c.is_admin ? "All groups on this hub." : "Groups you own or belong to."} A repository shared with a group is open to all of its members.</p></div>
      ${c.can_create && html`<button class="btn" aria-expanded=${mk} onClick=${() => { setMk(!mk); setPane("list"); }}>${mk ? "Close" : "New group"}</button>`}</div>
    <div class=${"kn gr pane-" + pane}>
      <aside class="kn-list" aria-label="Groups">
        ${mk && c.can_create && html`<form class="gr-new" onSubmit=${(e) => { e.preventDefault(); if (nn.trim()) create(); }}><input class="field" aria-label="Group name" placeholder="Name, e.g. data-team" value=${nn} onInput=${(e) => setNn(e.target.value)} /><input class="field" aria-label="Description" placeholder="Description (optional)" value=${nd} onInput=${(e) => setNd(e.target.value)} />
          <div class="row"><button class="btn sm" type="submit" disabled=${!nn.trim()}>Create group</button><button class="btn sec sm" type="button" onClick=${() => setMk(false)}>Cancel</button></div></form>`}
        ${all.length > 5 && html`<${Search} value=${q} onInput=${setQ} ph="Filter groups" />`}
        <div class="kn-sec"><span>${c.is_admin ? "All groups" : "Your groups"}</span><span>${all.length}</span></div>
        ${list.length ? list.map((g) => html`<button type="button" key=${g.name} class=${"kn-row gr-row" + (sel === g.name ? " on" : "")} aria-current=${sel === g.name ? "true" : "false"} onClick=${() => pick(g.name)}>
            <span class="gr-t"><b>${g.name}</b><span>${plural(g.members, "member")} · ${plural(g.packages, "repo")}</span></span>${g.can_manage ? (g.manager && !c.is_admin ? html`<span class="gr-role">Manager</span>` : "") : html`<span class="gr-role">Member</span>`}</button>`)
          : html`<p class="kn-none">${all.length ? "No group matches." : c.can_create ? "No groups yet. Use New group to make the first one." : "You are not in any groups."}</p>`}
      </aside>
      <section class="kn-ed gr-ed" aria-label="Group">${sel ? html`<${GroupDetail} name=${sel} repos=${(all.find((x) => x.name === sel) || {}).packages || 0} key=${sel} bump=${bump} onBack=${() => setPane("list")} onGone=${() => { setSel(null); setPane("list"); }} />` : html`<div class="gr-empty"><h3>No group selected</h3><p class="mut">Choose a group from the list to view and manage its members.</p></div>`}</section></div></div>`;
}
function GroupDetail({ name, repos, bump, onGone, onBack }) {
  const [tick, setTick] = useState(0); const g = useLoad(() => api("/groups/" + name), [name, tick]); const [asking, setAsking] = useState(false); const [typed, setTyped] = useState("");
  const [paste, setPaste] = useState(""); const [mf, setMf] = useState("");
  if (g.loading && !g.d) return html`<div class="gr-pad"><div class="skel line"></div><div class="skel line"></div></div>`;
  if (g.e) return html`<div class="gr-empty"><h3>Members only see their own membership</h3><p class="mut">Only the group’s owner, its managers or an admin can view and edit its member list.</p><button class="btn sec sm kn-back" onClick=${onBack}>All groups</button></div>`;
  const re = () => { setTick((n) => n + 1); bump(); };
  const add = async (names) => { try { const r = await api(`/groups/${name}/members`, { method: "POST", body: { usernames: names } }); toast(r.missing.length ? `Added; not found: ${r.missing.join(", ")}` : "Members added", r.missing.length ? 1 : 0); setPaste(""); re(); } catch (e) { toast(e.message, 1); } };
  const rm = async (u) => { try { await api(`/groups/${name}/members/${encodeURIComponent(u)}`, { method: "DELETE" }); re(); } catch (e) { toast(e.message, 1); } };
  const setMgr = async (u, flag) => { try { await api(`/groups/${name}/members/${encodeURIComponent(u)}/manager`, { method: "PUT", body: { manager: flag } }); toast(flag ? "Now a manager" : "No longer a manager"); re(); } catch (e) { toast(e.message, 1); } };
  const del = async () => { try { await api("/groups/" + name, { method: "DELETE" }); toast("Group deleted"); bump(); onGone(); } catch (e) { toast(e.message, 1); } };
  const members = g.d.members, mfl = mf.trim().toLowerCase(), mshown = members.filter((m) => !mfl || (shown(m) + " " + m.username).toLowerCase().includes(mfl));
  return html`<div class="gr-detail">
    <div class="kn-bar gr-bar"><button type="button" class="btn sec sm kn-back" onClick=${onBack}>All groups</button>
      <div class="kn-title" style="display:block"><h2>${name}</h2><p class="sm mut gr-meta">${plural(members.length, "member")} · ${repos + (repos === 1 ? " shared repository" : " shared repositories")}${g.d.description ? " · " + g.d.description : ""}</p></div></div>
    <div class="gr-sec"><div class="gr-sh"><h3>Members</h3>${members.length > 6 && html`<input class="field gr-find" aria-label="Filter members" placeholder="Filter members" value=${mf} onInput=${(e) => setMf(e.target.value)} />`}</div>
      <div class="gr-add"><${PrincipalPicker} ph="Add a person by name or username…" exclude=${members.map((m) => "user:" + m.username).concat(["group:*"])} onPick=${(t, n) => t === "user" ? add([n]) : toast("Pick a person", 1)} />
        <details class="kn-help"><summary>Add several by username</summary><div class="stack" style="margin-top:8px"><textarea class="field" rows="3" style="padding:12px;height:auto" aria-label="Usernames" placeholder="Separate usernames with commas, spaces or new lines" value=${paste} onInput=${(e) => setPaste(e.target.value)}></textarea>
          <button class="btn sec sm" style="width:fit-content" disabled=${!paste.trim()} onClick=${() => add(paste.split(/[\s,;]+/).filter(Boolean))}>Add</button></div></details></div>
      ${members.length ? html`<div class="tscroll"><table class="gr-tbl"><thead><tr><th>Member</th><th>Role</th><th><span class="sr">Actions</span></th></tr></thead><tbody>
        ${mshown.map((m) => html`<tr key=${m.username}><td><div class="gr-who"><${Avatar} p=${m} size=${32} /><div class="t"><b>${shown(m)}</b><span>${m.title ? m.title + " · " : ""}@${m.username}</span></div></div></td>
          <td class=${m.manager ? "" : "mut"}>${m.manager ? "Manager" : "Member"}</td>
          <td class="gr-act"><button type="button" class="gr-link" aria-label=${(m.manager ? "Remove manager role from " : "Make manager: ") + shown(m)} onClick=${() => setMgr(m.username, !m.manager)}>${m.manager ? "Remove manager" : "Make manager"}</button><button type="button" class="gr-link bad" aria-label=${"Remove " + shown(m) + " from the group"} onClick=${() => rm(m.username)}>Remove</button></td></tr>`)}
        ${!mshown.length && html`<tr><td colspan="3" class="mut">No member matches “${mf}”.</td></tr>`}</tbody></table></div>` : html`<p class="mut sm">This group has no members yet.</p>`}</div>
    ${g.d.can_delete && html`<div class="gr-sec"><h3>Delete group</h3><p class="sm mut">Removes the group and every repository share that points to it. Members keep their accounts.</p>
      ${asking ? html`<div class="kn-add"><input class="field" style="flex:1;min-width:160px" aria-label=${"Type " + name + " to confirm"} placeholder=${"Type " + name + " to confirm"} value=${typed} onInput=${(e) => setTyped(e.target.value)} /><button class="btn danger sm" disabled=${typed.trim() !== name} onClick=${del}>Delete group</button><button class="btn sec sm" onClick=${() => { setAsking(false); setTyped(""); }}>Cancel</button></div>` : html`<button class="btn danger sm" style="width:fit-content" onClick=${() => setAsking(true)}>Delete this group…</button>`}</div>`}</div>`;
}
const KINDS = ["install", "update", "uninstall", "use", "error"];
const DAYS = [["1", "Last 24 hours"], ["7", "Last 7 days"], ["30", "Last 30 days"], ["90", "Last 90 days"], ["0", "All time"]];
function Audit() {
  const [f, setF] = useState({ source: "tools", q: "", actor: "", action: "", package: "", kind: "", host: "", os: "", days: "7", page: 1 }); const [q, setQ] = useState(""); const [open, setOpen] = useState(null);
  useEffect(() => { const t = setTimeout(() => setF((x) => (x.q === q ? x : { ...x, q, page: 1 })), 300); return () => clearTimeout(t); }, [q]);
  const qs = (o) => Object.entries(o).filter(([, v]) => v !== "" && v != null).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&");
  const d = useLoad(() => api("/audit?" + qs({ ...f, per_page: 50 })), [JSON.stringify(f)]);
  if (!can("audit")) return html`<${Empty} t="No access" d="You need the audit permission to view this page." />`;
  const up = (k, v) => setF((x) => ({ ...x, [k]: v, page: 1 })); const tools = f.source === "tools";
  const exportCsv = async () => { try { const r = await fetch(U("/api/v1/audit/export?" + qs(f)), { headers: { Authorization: "Bearer " + tok() } }); if (!r.ok) throw new Error("Export failed"); saveBlob(await r.blob(), `audit-${f.source}.csv`); } catch (e) { toast(e.message, 1); } };
  const rows = d.d ? d.d.items : [], total = d.d ? d.d.total : 0, pages = Math.max(1, Math.ceil(total / 50));
  const stamp = (t) => new Date(t * 1000).toLocaleString();
  return html`<div class="stack"><div class="row"><h1>Security audit</h1><span class="sp"></span><button class="btn sec sm" onClick=${exportCsv}>Export CSV</button></div>
    <div class="seg">${[["tools", "Tool calls & installs"], ["admin", "Admin actions"]].map(([k, l]) => html`<button key=${k} class=${f.source === k ? "on" : ""} onClick=${() => { setQ(""); setF({ source: k, q: "", actor: "", action: "", package: "", kind: "", host: "", os: "", days: f.days, page: 1 }); }}>${l}</button>`)}</div>
    <div class="card stack"><div class="row" style="flex-wrap:wrap;gap:10px"><div style="flex:2;min-width:220px"><${Search} value=${q} onInput=${setQ} ph=${tools ? "Search parameters, commands, paths, hosts, people" : "Search actor, action, target, detail"} /></div>
      <select class="field" aria-label="Time range" value=${f.days} onChange=${(e) => up("days", e.target.value)}>${DAYS.map(([v, l]) => html`<option value=${v} key=${v}>${l}</option>`)}</select>
      <select class="field" aria-label="Person" value=${f.actor} onChange=${(e) => up("actor", e.target.value)}><option value="">${tools ? "Anyone" : "Any actor"}</option>${(d.d ? d.d.actors : []).map((a) => html`<option value=${a} key=${a}>${a}</option>`)}</select>
      ${tools && html`<select class="field" aria-label="Event" value=${f.kind} onChange=${(e) => up("kind", e.target.value)}><option value="">Any event</option>${KINDS.map((k) => html`<option value=${k} key=${k}>${k}</option>`)}</select>
        <select class="field" aria-label="Device" value=${f.host} onChange=${(e) => up("host", e.target.value)}><option value="">Any device</option>${(d.d ? d.d.hosts || [] : []).map((a) => html`<option value=${a} key=${a}>${a}</option>`)}</select>
        <select class="field" aria-label="OS" value=${f.os} onChange=${(e) => up("os", e.target.value)}><option value="">Any OS</option>${(d.d ? d.d.oses || [] : []).map((a) => html`<option value=${a} key=${a}>${a}</option>`)}</select>
        <input class="field" style="max-width:170px" placeholder="Package" value=${f.package} onInput=${(e) => up("package", e.target.value.trim())} />`}
      <input class="field" style="max-width:190px" placeholder=${tools ? "Tool / component" : "Action, e.g. user.role"} value=${f.action} onInput=${(e) => up("action", e.target.value)} /></div>
      <span class="xs mut3">${d.loading && !d.d ? "Loading…" : `${fmt(total)} ${total === 1 ? "record" : "records"}`}${tools ? ". Parameters are scrubbed of passwords, tokens and keys before they leave the machine. “(local)” = not signed in; the name is the OS user on that device and is self-reported." : ""}</span></div>
    ${d.e ? html`<${Err} e=${d.e} />` : html`<div class="list tscroll"><table><thead><tr><th>When</th><th>${tools ? "Who" : "Actor"}</th><th>${tools ? "Event" : "Action"}</th><th>${tools ? "Tool call" : "Target"}</th><th></th></tr></thead><tbody>
      ${rows.map((x) => { const k = x.id + f.source; return html`<tr key=${k} style="cursor:pointer" onClick=${() => setOpen(open === k ? null : k)}>
        <td class="mut sm" title=${stamp(x.ts)}>${ago(x.ts)}</td><td>${x.actor}${tools && (x.host || x.os) ? html`<div class="xs mut3">${[x.host, x.os].filter(Boolean).join(" · ")}</div>` : ""}</td>
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


// ---------- personal knowledge (private notes + the bot soul; `aihub knowledge pull` mirrors them). Notes can be shared with a user or group.
const SOUL0 = "# Soul\n\nWho the assistant is for me: tone, language, what to always / never do.\n";
const knSize = (t) => { const w = (t.trim().match(/\S+/g) || []).length; return w + (w === 1 ? " word" : " words") + " · " + t.length + " characters"; };
const KnRow = ({ on, onClick, title, sub, badge, pin }) => html`<button type="button" class=${"kn-row" + (pin ? " pin" : "") + (on ? " on" : "")} aria-current=${on ? "true" : "false"} onClick=${onClick}><span class="kn-t">${title}</span>${sub ? html`<span class="kn-s">${sub}</span>` : ""}${badge ? html`<span class="badge">${badge}</span>` : ""}</button>`;
function Knowledge() {
  const { me } = useStore(); const [tick, setTick] = useState(0); const [cur, setCur] = useState("soul"); const [owner, setOwner] = useState(null);
  const [text, setText] = useState(""); const [orig, setOrig] = useState(""); const [nn, setNn] = useState(""); const [q, setQ] = useState(""); const [pane, setPane] = useState("list");
  const [access, setAccess] = useState("read"); const [kind, setKind] = useState("user"); const [who, setWho] = useState("");
  const list = useLoad(() => me ? api("/me/knowledge") : Promise.resolve({ items: [] }), [me && me.username, tick]);
  const shared = useLoad(() => me ? api("/knowledge/shared") : Promise.resolve({ items: [] }), [me && me.username, tick]);
  const shares = useLoad(() => me && !owner && cur !== "soul" ? api("/me/knowledge/" + cur + "/shares").catch(() => ({ items: [] })) : Promise.resolve({ items: [] }), [me && me.username, cur, owner, tick]);
  const url = owner ? "/knowledge/shared/" + owner + "/" + cur : "/me/knowledge/" + cur;
  const sharedItem = owner && shared.d ? shared.d.items.find((i) => i.owner === owner && i.name === cur) : null;
  const readOnly = !!owner && !(sharedItem && sharedItem.access === "write");
  useEffect(() => { if (!me) return; let on = 1; api(url).then((d) => { if (on) { setText(d.body); setOrig(d.body); } }, () => { if (on) { setText(!owner && cur === "soul" ? SOUL0 : ""); setOrig(""); } }); return () => { on = 0; }; }, [cur, owner, me && me.username]);
  const dirty = !readOnly && text !== orig;
  const save = async () => { if (readOnly || !dirty) return; try { await api(url, { method: "PUT", body: { body: text } }); setOrig(text); toast("Saved"); setTick((n) => n + 1); } catch (e) { toast(e.message, 1); } };
  const saveRef = useRef(); saveRef.current = save;
  useEffect(() => { const k = (e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (saveRef.current) saveRef.current(); } }; addEventListener("keydown", k); return () => removeEventListener("keydown", k); }, []);
  useEffect(() => { if (!dirty) return; const b = (e) => { e.preventDefault(); e.returnValue = ""; }; addEventListener("beforeunload", b); return () => removeEventListener("beforeunload", b); }, [dirty]);
  if (!me) return html`<${Empty} t="Sign in required" d="Your notes are private to your account." />`;
  const open = (n, o) => { if (dirty && !confirm("Discard unsaved changes to " + cur + "?")) return; setCur(n); setOwner(o || null); setPane("edit"); };
  const del = async () => { if (!confirm("Delete " + cur + "? Everyone it is shared with loses it.")) return; try { await api("/me/knowledge/" + cur, { method: "DELETE" }); setCur("soul"); setOwner(null); setPane("list"); setTick((n) => n + 1); } catch (e) { toast(e.message, 1); } };
  const add = () => { const n = nn.trim().toLowerCase(); if (!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(n)) return toast("Use lowercase letters, digits, . _ -", 1); setNn(""); open(n); };
  const share = async () => { const w = who.trim(); if (!w) return toast("Enter a " + kind + " name", 1);
    try { await api("/me/knowledge/" + cur + "/shares", { method: "PUT", body: { [kind]: w, access } }); toast("Shared with " + w); setWho(""); setTick((n) => n + 1); } catch (e) { toast(e.message, 1); } };
  const unshare = async (sh) => { try { await api("/me/knowledge/" + cur + "/shares", { method: "DELETE", body: { [sh.type]: sh.name } }); setTick((n) => n + 1); } catch (e) { toast(e.message, 1); } };
  const mine = list.d && list.d.items.some((i) => i.name === cur);
  const f = q.trim().toLowerCase(), hit = (s) => !f || s.toLowerCase().includes(f);
  const names = (list.d ? list.d.items.map((i) => i.name) : []).filter((n) => n !== "soul");
  const shown = names.filter(hit), sharedItems = (shared.d ? shared.d.items : []).filter((i) => hit(i.owner + "/" + i.name));
  const X = html`<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M2 2l8 8M10 2l-8 8"/></svg>`;
  return html`<div class="stack"><div class="kn-head"><div><h1>Knowledge</h1><p class="lead">Notes your bots and assistants read.</p></div>
      <details class="kn-help"><summary>How notes reach your tools</summary><p><b>soul</b> is the persona a bot loads first and is always private. Other notes can be shared with a person or a group. <code>aihub knowledge pull</code> mirrors them to <code>~/.aihub/knowledge</code> (shared notes under <code>shared/</code>); edit there and run <code>aihub knowledge push</code>. <a href="#/docs/bots">How to build a bot</a>.</p></details></div>
    <div class=${"kn pane-" + pane}>
      <aside class="kn-list" aria-label="Notes">
        <form class="kn-new" onSubmit=${(e) => { e.preventDefault(); add(); }}><input class="field" aria-label="New note name" placeholder="New note name" value=${nn} onInput=${(e) => setNn(e.target.value)} /><button class="btn sm" type="submit">Add</button></form>
        <${Search} value=${q} onInput=${setQ} ph="Filter notes" />
        ${hit("soul") && html`<div class="kn-sec"><span>Persona</span></div><${KnRow} pin=${true} on=${!owner && cur === "soul"} onClick=${() => open("soul")} title="soul" badge="private" />`}
        <div class="kn-sec"><span>Your notes</span><span>${names.length}</span></div>
        ${shown.length ? shown.map((n) => html`<${KnRow} key=${n} on=${!owner && n === cur} onClick=${() => open(n)} title=${n} />`) : html`<p class="kn-none">${names.length ? "No note matches." : "No notes yet. Name one above to start."}</p>`}
        ${sharedItems.length ? html`<div class="kn-sec"><span>Shared with me</span><span>${sharedItems.length}</span></div>${sharedItems.map((i) => html`<${KnRow} key=${i.owner + "/" + i.name} on=${owner === i.owner && cur === i.name} onClick=${() => open(i.name, i.owner)} title=${i.name} sub=${i.owner} badge=${i.access === "write" ? "can edit" : "read"} />`)}` : ""}
      </aside>
      <section class="kn-ed" aria-label="Editor">
        <div class="kn-bar"><button type="button" class="btn sec sm kn-back" onClick=${() => setPane("list")}>All notes</button>
          <div class="kn-title"><h2>${owner ? owner + " / " : ""}${cur}.md</h2>${owner ? html`<span class="badge">${readOnly ? "read-only" : "you can edit"}</span>` : cur === "soul" ? html`<span class="badge blue">persona</span>` : ""}</div>
          <span class=${"kn-state" + (dirty ? " dirty" : "")} aria-live="polite">${readOnly ? "" : dirty ? "Unsaved changes" : "Saved"}</span>
          ${!owner && cur !== "soul" && mine && html`<button class="btn sec sm danger" onClick=${del}>Delete</button>`}${!readOnly && html`<button class="btn sm" disabled=${!dirty} onClick=${save}>Save</button>`}</div>
        <textarea class="field mono kn-text" aria-label="Note text" readOnly=${readOnly} value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
        <div class="kn-foot"><span>${knSize(text)}</span><span>${readOnly ? "Read-only" : "Ctrl+S or ⌘S saves"}</span></div>
        ${!owner && cur !== "soul" ? html`<div class="kn-share"><h3>Sharing</h3>
          ${!mine ? html`<p class="mut sm">Save this note first, then you can share it.</p>` : html`
          ${shares.d && shares.d.items.length ? html`<div class="kn-chips">${shares.d.items.map((sh) => html`<span class="kn-chip" key=${sh.type + sh.name}><span class="kn-av" aria-hidden="true">${(sh.name[0] || "?").toUpperCase()}</span><span>${sh.type === "group" ? "group " : ""}<b>${sh.name}</b></span><span class="badge">${sh.access}</span><button type="button" class="kn-x" aria-label=${"Stop sharing with " + sh.name} onClick=${() => unshare(sh)}>${X}</button></span>`)}</div>` : html`<p class="mut sm">Private. Only you can see this note.</p>`}
          <div class="kn-add"><select class="field" aria-label="Share with" value=${kind} onChange=${(e) => setKind(e.target.value)}><option value="user">User</option><option value="group">Group</option></select>
            <input class="field" style="flex:1;min-width:140px" aria-label="Name" placeholder=${kind === "group" ? "group name" : "username"} value=${who} onInput=${(e) => setWho(e.target.value)} />
            <select class="field" aria-label="Access" value=${access} onChange=${(e) => setAccess(e.target.value)}><option value="read">Can read</option><option value="write">Can edit</option></select>
            <button class="btn sm" onClick=${share}>Share</button></div>
          <p class="xs mut">Do not put secrets in a shared note. Editors overwrite the whole note.</p>`}</div>` : ""}
        ${!owner && cur === "soul" ? html`<p class="kn-share xs mut">soul is your persona and cannot be shared.</p>` : ""}
      </section></div></div>`;
}

// ---------- dashboard (charts are plain SVG: no library, theme-aware)
const KC = { use: "var(--blue)", install: "var(--green)", update: "var(--orange)", error: "var(--red)", uninstall: "var(--fg-3)", publish: "var(--fg-2)" };
const delta = (a, b) => { if (!b) return a ? { t: "new", c: "green" } : null; const p = Math.round((a - b) / b * 100); return { t: (p >= 0 ? "▲ " : "▼ ") + Math.abs(p) + "%", c: p >= 0 ? "green" : "red" }; };
const Kpi = ({ label, value, prev, invert }) => { const d = delta(value, prev);
  return html`<div class="card stat"><span>${label}</span><b>${fmt(value)}</b>${d ? html`<span class=${"badge " + (invert ? (d.c === "green" ? "red" : "green") : d.c)}>${d.t} vs prior</span>` : html`<span class="xs mut3">no prior data</span>`}</div>`; };

function LineChart({ daily, series, colors = KC, f = fmt, labels = {} }) {
  const W = 760, H = 220, P = { l: 34, r: 10, t: 10, b: 24 }, n = daily.length;
  const mx = Math.max(1, ...daily.map((d) => Math.max(...series.map((k) => d[k] || 0))));
  const nice = Math.ceil(mx / 4) * 4 || 4, x = (i) => P.l + (n < 2 ? 0 : i * (W - P.l - P.r) / (n - 1)), y = (v) => P.t + (H - P.t - P.b) * (1 - v / nice);
  const [hv, setHv] = useState(null);
  const move = (e) => { const r = e.currentTarget.getBoundingClientRect(); const px = (e.clientX - r.left) / r.width * W; setHv(Math.max(0, Math.min(n - 1, Math.round((px - P.l) / ((W - P.l - P.r) / Math.max(1, n - 1)))))); };
  const step = Math.ceil(n / 6);
  return html`<div style="position:relative"><svg viewBox=${`0 0 ${W} ${H}`} style="width:100%;height:auto;display:block" role="img" aria-label="Daily activity chart" onMouseMove=${move} onMouseLeave=${() => setHv(null)}>
    ${[0, 1, 2, 3, 4].map((i) => html`<g key=${i}><line x1=${P.l} x2=${W - P.r} y1=${y(nice * i / 4)} y2=${y(nice * i / 4)} stroke="var(--sep)" stroke-width="1"/><text x=${P.l - 6} y=${y(nice * i / 4) + 4} text-anchor="end" font-size="11" fill="var(--fg-3)">${f(nice * i / 4)}</text></g>`)}
    ${daily.map((d, i) => i % step === 0 ? html`<text key=${i} x=${x(i)} y=${H - 6} text-anchor="middle" font-size="11" fill="var(--fg-3)">${d.date.slice(5)}</text>` : null)}
    ${series.map((k) => html`<g key=${k}><path d=${daily.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d[k] || 0).toFixed(1)}`).join("")} fill="none" stroke=${colors[k]} stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
      ${n < 3 && daily.map((d, i) => html`<circle key=${i} cx=${x(i)} cy=${y(d[k] || 0)} r="3.5" fill=${colors[k]}/>`)}${hv !== null && html`<circle cx=${x(hv)} cy=${y(daily[hv][k] || 0)} r="4" fill=${colors[k]} stroke="var(--surface)" stroke-width="2"/>`}</g>`)}
    ${hv !== null && html`<line x1=${x(hv)} x2=${x(hv)} y1=${P.t} y2=${H - P.b} stroke="var(--fg-3)" stroke-dasharray="3 3"/>`}</svg>
    ${hv !== null && html`<div class="tip" style=${`left:${Math.min(78, Math.max(2, x(hv) / W * 100))}%`}><b>${daily[hv].date}</b>${series.map((k) => html`<div key=${k}><i style=${"background:" + colors[k]}></i>${labels[k] || k} <b>${f(daily[hv][k] || 0)}</b></div>`)}</div>`}
    <div class="row xs mut" style="gap:16px;margin-top:6px">${series.map((k) => html`<span key=${k}><i class="dot" style=${"background:" + colors[k]}></i>${labels[k] || k}</span>`)}</div></div>`;
}
const Bars = ({ items, color = "var(--blue)", link, f = fmt }) => { const mx = Math.max(1, ...items.map((i) => i.count));
  return items.length ? html`<div>${items.map((i) => html`<div class="hb" key=${i.name}><span class="hl">${link ? html`<a href=${"#/package/" + i.name}>${i.name}</a>` : i.name}</span><span class="ht"><i style=${`width:${i.count / mx * 100}%;background:${color}`}></i></span><span class="hn">${f(i.count)}</span></div>`)}</div>` : html`<p class="mut sm">No data in this period.</p>`; };
const Donut = ({ items, f = fmt }) => { const tot = items.reduce((a, b) => a + b.count, 0); if (!tot) return html`<p class="mut sm">No data in this period.</p>`;
  const pal = ["var(--blue)", "var(--green)", "var(--orange)", "var(--red)", "var(--fg-3)"]; let acc = 0, R = 42, C = 2 * Math.PI * R;
  return html`<div class="row" style="gap:20px;flex-wrap:nowrap"><svg viewBox="0 0 100 100" width="120" height="120" style="flex:none;transform:rotate(-90deg)" role="img" aria-label="Breakdown">
    ${items.map((it, i) => { const len = it.count / tot * C, el = html`<circle key=${it.name} cx="50" cy="50" r=${R} fill="none" stroke=${pal[i % 5]} stroke-width="14" stroke-dasharray=${`${len} ${C - len}`} stroke-dashoffset=${-acc}/>`; acc += len; return el; })}</svg>
    <div class="sm" style="min-width:0">${items.map((it, i) => html`<div key=${it.name}><i class="dot" style=${"background:" + pal[i % 5]}></i>${it.name} <b>${Math.round(it.count / tot * 100)}%</b> <span class="mut3">${f(it.count)}</span></div>`)}</div></div>`; };

const OS_PAL = ["var(--blue)", "var(--green)", "var(--orange)", "var(--red)", "var(--fg-3)"];
const osColors = (names) => Object.fromEntries(names.map((n, i) => [n, OS_PAL[i % 5]]));
const DASH_KEYS = ["package", "type", "user", "source", "kind", "identity", "host", "os", "q"];
const DASH_LABEL = { package: "Package", type: "Type", user: "Person", source: "Tool", kind: "Event", identity: "Identity", host: "Device", os: "OS", q: "Search" };
const DASH_KINDS = ["use", "install", "update", "uninstall", "error", "publish"];
const dashParams = () => { const q = new URLSearchParams((store.route.split("?")[1]) || ""); const o = { days: +q.get("days") || 30, from: q.get("from") || "", to: q.get("to") || "", tab: q.get("tab") || "overview", page: +q.get("page") || 1 }; DASH_KEYS.forEach((k) => (o[k] = q.get(k) || "")); return o; };
const dashHash = (o) => { const q = new URLSearchParams(); if (o.from || o.to) { if (o.from) q.set("from", o.from); if (o.to) q.set("to", o.to); } else if (o.days !== 30) q.set("days", o.days); DASH_KEYS.forEach((k) => o[k] && q.set(k, o[k])); if (o.tab !== "overview") q.set("tab", o.tab); if (o.page > 1) q.set("page", o.page); const t = q.toString(); return "/dashboard" + (t ? "?" + t : ""); };

function Heatmap({ grid }) {
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], mx = Math.max(1, ...grid.flat());
  const cells = [];                                   // flat list (no fragments): label, then 24 hour cells per weekday
  grid.forEach((row, d) => { cells.push(html`<span key=${"l" + d} class="xs mut3">${days[d]}</span>`); row.forEach((v, h) => cells.push(html`<i key=${d + "-" + h} title=${`${days[d]} ${String(h).padStart(2, "0")}:00 UTC: ${v}`} style=${`background:color-mix(in srgb,var(--blue) ${v ? 14 + Math.round(v / mx * 76) : 0}%,var(--fill))`}></i>`)); });
  return html`<div class="heat" role="img" aria-label="Activity by weekday and hour, UTC"><span></span>${[0, 6, 12, 18].map((h) => html`<span key=${h} class="xs mut3" style=${`grid-column:${h + 2} / span 6`}>${String(h).padStart(2, "0")}:00</span>`)}${cells}</div>`;
}

// ---------- dashboard PDF report: summary -> trend -> breakdown -> notes. Built as a static HTML string (inline SVG, literal colours),
// printed from a hidden iframe so the browser's "Save as PDF" produces it; no library, no network, nothing leaves the page.
const rEsc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const RC = { use: "#0a64d6", install: "#1a8f43", update: "#c26a00", error: "#c9302c", uninstall: "#6b7280", publish: "#7c3aed" };
function reportHtml(D, f, brand) {
  const t = D.totals || {}, pv = D.previous || {}, sum = (o) => Object.values(o).reduce((a, b) => a + (+b || 0), 0), n = (v) => Number(v || 0).toLocaleString("en-US");
  const total = sum(t), prevTotal = sum(pv), errRate = total ? (t.error || 0) / total : 0;
  const pct = (a, b) => (b ? Math.round((a - b) / b * 100) : null);
  const chg = (a, b) => { const p = pct(a, b); return p === null ? (a ? "new" : "–") : (p >= 0 ? "▲ " : "▼ ") + Math.abs(p) + "%"; };
  const cls = (a, b, inv) => { const p = pct(a, b); return p === null || p === 0 ? "" : ((p > 0) !== !!inv) ? "up" : "down"; };
  const share = (items) => { const s = items.reduce((a, b) => a + b.count, 0); return items.map((i) => ({ ...i, pc: s ? i.count / s : 0 })); };
  const table = (head, rows) => rows.length ? `<table><thead><tr>${head.map((h, i) => `<th${i ? ' class="r"' : ""}>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>` : `<p class="mut">No data in this period.</p>`;
  const ranking = (items, label, limit = 10) => table(["#", label, "Events", "Share"], share(items.slice(0, limit)).map((i, k) => `<tr><td class="mut">${k + 1}</td><td>${rEsc(i.name)}<div class="bar"><i style="width:${Math.max(2, i.pc * 100)}%"></i></div></td><td class="r">${n(i.count)}</td><td class="r">${(i.pc * 100).toFixed(1)}%</td></tr>`));
  const split = (items, label) => table([label, "Events", "Share"], share(items).map((i) => `<tr><td>${rEsc(i.name)}</td><td class="r">${n(i.count)}</td><td class="r">${(i.pc * 100).toFixed(1)}%</td></tr>`));
  const days = D.daily || [], ks = ["use", "install", "error"].filter((k) => days.some((d) => d[k]));
  const chart = (() => { if (!days.length || !ks.length) return `<p class="mut">No activity in this period.</p>`;
    const W = 700, H = 200, P = { l: 38, r: 8, t: 8, b: 22 }, c = days.length, mx = Math.max(1, ...days.map((d) => Math.max(...ks.map((k) => d[k] || 0)))), top = Math.ceil(mx / 4) * 4 || 4;
    const x = (i) => P.l + (c < 2 ? 0 : i * (W - P.l - P.r) / (c - 1)), y = (v) => P.t + (H - P.t - P.b) * (1 - v / top), step = Math.ceil(c / 8);
    return `<svg viewBox="0 0 ${W} ${H}" width="100%">${[0, 1, 2, 3, 4].map((i) => `<line x1="${P.l}" x2="${W - P.r}" y1="${y(top * i / 4)}" y2="${y(top * i / 4)}" stroke="#e5e7eb"/><text x="${P.l - 5}" y="${y(top * i / 4) + 3}" text-anchor="end" font-size="9" fill="#6b7280">${rEsc(fmt(top * i / 4))}</text>`).join("")}`
      + days.map((d, i) => i % step === 0 ? `<text x="${x(i)}" y="${H - 6}" text-anchor="middle" font-size="9" fill="#6b7280">${rEsc(d.date.slice(5))}</text>` : "").join("")
      + ks.map((k) => `<path d="${days.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d[k] || 0).toFixed(1)}`).join("")}" fill="none" stroke="${RC[k]}" stroke-width="1.8" stroke-linejoin="round"/>${c < 3 ? days.map((d, i) => `<circle cx="${x(i)}" cy="${y(d[k] || 0)}" r="3" fill="${RC[k]}"/>`).join("") : ""}`).join("")
      + `</svg><div class="legend">${ks.map((k) => `<span><i style="background:${RC[k]}"></i>${k}</span>`).join("")}</div>`; })();
  const WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], grid = D.heatmap || [], gmx = Math.max(1, ...grid.flat());
  const heat = grid.length ? `<table class="heat"><thead><tr><th></th>${Array.from({ length: 24 }, (_, h) => `<th>${h % 3 ? "" : String(h).padStart(2, "0")}</th>`).join("")}</tr></thead><tbody>${grid.map((row, d) => `<tr><th>${WD[d].slice(0, 3)}</th>${row.map((v) => `<td style="background:rgba(10,100,214,${v ? (0.12 + 0.8 * v / gmx).toFixed(2) : 0})"></td>`).join("")}</tr>`).join("")}</tbody></table><p class="mut small">Weekday by hour, UTC. Darker means more events.</p>` : "";
  // key findings: plain sentences computed from the same numbers, so the summary and the tables cannot disagree
  const peak = days.reduce((a, d) => (sum(Object.fromEntries(Object.entries(d).filter(([k]) => k !== "date"))) > (a.v || 0) ? { d: d.date, v: sum(Object.fromEntries(Object.entries(d).filter(([k]) => k !== "date"))) } : a), {});
  let pd = 0, ph = 0, pvv = 0; grid.forEach((row, d) => row.forEach((v, h) => { if (v > pvv) { pvv = v; pd = d; ph = h; } }));
  const tp = share(D.top_packages || []), top3 = tp.slice(0, 3).reduce((a, b) => a + b.pc, 0), os = share(D.by_os || []), idn = share(D.by_identity || []);
  const F = [];
  F.push(`${n(total)} events were recorded (${chg(total, prevTotal)} vs the previous period of the same length), from ${n(D.active_users)} active people on ${n(D.clients)} devices across ${n(D.active_packages)} packages.`);
  if (t.use) F.push(`Tools were called ${n(t.use)} times${pv.use !== undefined ? ` (${chg(t.use, pv.use || 0)})` : ""}; ${n(t.install || 0)} installs and ${n(t.publish || 0)} publishes.`);
  if (total) F.push(`Errors make up ${(errRate * 100).toFixed(1)}% of events (${n(t.error || 0)}, ${chg(t.error || 0, pv.error || 0)} vs prior)${errRate > 0.05 ? ", above the 5% level worth investigating" : ", within a healthy range"}.`);
  if (peak.d) F.push(`Busiest day: ${peak.d} with ${n(peak.v)} events.${pvv ? ` Busiest slot: ${WD[pd]} around ${String(ph).padStart(2, "0")}:00 UTC.` : ""}`);
  if (tp.length) F.push(`The most used package is ${tp[0].name} (${(tp[0].pc * 100).toFixed(0)}% of package usage); the top ${Math.min(3, tp.length)} account for ${(top3 * 100).toFixed(0)}%${top3 > 0.8 && tp.length > 3 ? ", so usage is concentrated" : ""}.`);
  if (os.length) F.push(`Most common OS: ${os[0].name} (${(os[0].pc * 100).toFixed(0)}%).`);
  const anon = idn.find((i) => /anon/i.test(i.name)); if (anon) F.push(`${(anon.pc * 100).toFixed(0)}% of events come from anonymous (not signed-in) users.`);
  const filt = DASH_KEYS.filter((k) => f[k]).map((k) => `${DASH_LABEL[k]}: ${rEsc(f[k])}`);
  const kpi = (l, a, b, inv) => `<div class="kpi"><span>${l}</span><b>${n(a)}</b><em class="${cls(a, b, inv)}">${chg(a, b)} vs prior</em></div>`;
  const now = new Date().toISOString().replace("T", " ").slice(0, 16) + " UTC";
  return `<!doctype html><html><head><meta charset="utf-8"><title>${rEsc((brand.name || "AI Hub") + " usage report " + D.filters.from + " to " + D.filters.to)}</title><style>
@page{size:A4;margin:16mm 14mm}*{box-sizing:border-box}body{font:12px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;color:#111827;margin:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}
header{border-bottom:3px solid #0a64d6;padding-bottom:10px;margin-bottom:14px}h1{font-size:24px;margin:0}h2{font-size:15px;margin:22px 0 8px;padding-bottom:4px;border-bottom:1px solid #d1d5db}h2 b{color:#0a64d6;margin-right:6px}h3{font-size:12px;margin:14px 0 6px}
.meta{color:#6b7280;font-size:11px;margin-top:4px}.mut{color:#6b7280}.small{font-size:10px}.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:10px 0}.kpi{border:1px solid #d1d5db;border-radius:6px;padding:8px;break-inside:avoid}
.kpi span{display:block;font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em}.kpi b{display:block;font-size:20px}.kpi em{font-style:normal;font-size:10px;color:#6b7280}.up{color:#1a8f43!important}.down{color:#c9302c!important}
ul{margin:6px 0;padding-left:18px}li{margin:3px 0}table{width:100%;border-collapse:collapse;margin:4px 0;break-inside:auto}tr{break-inside:avoid}th,td{padding:4px 6px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}th{font-size:10px;color:#6b7280;text-transform:uppercase;background:#f3f4f6}.r{text-align:right}
.bar{height:4px;background:#e5e7eb;border-radius:2px;margin-top:3px}.bar i{display:block;height:4px;background:#0a64d6;border-radius:2px}.legend{display:flex;gap:14px;font-size:10px;color:#6b7280}.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}.cols>div{break-inside:avoid}.heat td{height:13px;padding:0;border:1px solid #fff;background:#f3f4f6}.heat th{padding:1px 2px;font-size:8px;background:none;border:0}
section{break-inside:auto}footer{margin-top:22px;border-top:1px solid #d1d5db;padding-top:6px;color:#6b7280;font-size:10px}.pb{break-before:page}
</style></head><body>
<header><div class="meta">${rEsc(brand.name || "AI Hub")}</div><h1>Usage report</h1><div class="meta">Period ${rEsc(D.filters.from)} to ${rEsc(D.filters.to)} (UTC) · Generated ${now}${filt.length ? " · Filters: " + filt.join(", ") : " · No filters (all activity)"}</div></header>
<section><h2><b>1</b>Executive summary</h2>
<div class="kpis">${kpi("Tool calls", t.use || 0, pv.use || 0)}${kpi("Active people", D.active_users, D.active_users_prev)}${kpi("Installs", t.install || 0, pv.install || 0)}${kpi("Publishes", t.publish || 0, pv.publish || 0)}${kpi("Errors", t.error || 0, pv.error || 0, true)}</div>
<h3>Key findings</h3><ul>${F.map((x) => `<li>${rEsc(x)}</li>`).join("")}</ul></section>
<section><h2><b>2</b>Activity over time</h2>${chart}
<h3>Totals by event kind</h3>${table(["Event", "This period", "Previous", "Change"], Object.keys(RC).map((k) => `<tr><td>${k}</td><td class="r">${n(t[k])}</td><td class="r">${n(pv[k])}</td><td class="r ${cls(t[k] || 0, pv[k] || 0, k === "error")}">${chg(t[k] || 0, pv[k] || 0)}</td></tr>`))}
<h3>When people work</h3>${heat}</section>
<section class="pb"><h2><b>3</b>Breakdown: what is used</h2><div class="cols"><div><h3>Most used packages</h3>${ranking(D.top_packages || [], "Package")}</div><div><h3>Most used components</h3>${ranking(D.top_components || [], "Component")}</div></div>
<h3>Top rated packages (all time)</h3>${table(["Package", "Reviews", "Average"], (D.top_reviewed || []).slice(0, 8).map((x) => `<tr><td>${rEsc(x.name)}</td><td class="r">${n(x.reviews)}</td><td class="r">${rEsc(x.avg)}</td></tr>`))}</section>
<section><h2><b>4</b>Breakdown: who uses it</h2><div class="cols"><div><h3>Top developers</h3>${ranking(D.top_developers || [], "Developer", 8)}</div><div><h3>Top people</h3>${ranking(D.top_users || [], "Person", 8)}</div></div>
<div class="cols"><div><h3>Signed in vs anonymous</h3>${split(D.by_identity || [], "Identity")}</div><div><h3>Devices</h3>${ranking(D.by_host || [], "Device", 8)}</div></div></section>
<section><h2><b>5</b>Breakdown: how it is used</h2><div class="cols"><div><h3>By type</h3>${split(D.by_type || [], "Type")}</div><div><h3>By tool</h3>${split(D.by_source || [], "Tool")}</div></div>
<h3>By operating system</h3>${split(D.by_os || [], "OS")}</section>
<section><h2><b>6</b>Notes</h2><ul class="small"><li>Counts are events received by the hub in the period; "previous" is the preceding period of equal length.</li><li>All times are UTC. Percent changes against a zero baseline are shown as "new".</li><li>Rankings merge one person across accounts when "localize users" is on in Admin settings.</li></ul></section>
<footer>${rEsc(brand.name || "AI Hub")} · generated from the live dashboard on ${now}</footer></body></html>`;
}
function printReport(D, f) {
  const fr = document.createElement("iframe"); fr.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0"; fr.setAttribute("aria-hidden", "true");
  fr.onload = () => { try { fr.contentWindow.focus(); fr.contentWindow.print(); } finally { setTimeout(() => fr.remove(), 60000); } };
  fr.srcdoc = reportHtml(D, f, store.brand || {}); document.body.appendChild(fr);
}

function Dashboard() {
  const { me } = useStore(); const f = dashParams(); const [tick, setTick] = useState(0); const [open, setOpen] = useState(false);
  const go = (patch) => nav(dashHash({ ...f, page: 1, ...patch }));
  const ser = new URLSearchParams(); if (f.from || f.to) { f.from && ser.set("from", f.from); f.to && ser.set("to", f.to); } ser.set("days", f.days); DASH_KEYS.forEach((k) => f[k] && ser.set(k, f[k]));
  const qs = ser.toString();
  const d = useLoad(() => can("view_dashboard") ? api("/dashboard?" + qs) : Promise.resolve(null), [me, qs, tick, store.brand]);
  const log = useLoad(() => can("view_dashboard") && f.tab === "activity" ? api("/dashboard/events?per_page=25&page=" + f.page + "&" + qs) : Promise.resolve(null), [me, qs, f.tab, f.page, tick, store.brand]);
  const pk = useLoad(() => api("/packages?per_page=100&sort=name"), []);
  const [series, setSeries] = useState({ use: true, install: true, error: true, update: false, publish: false });
  const [qd, setQd] = useState(f.q); useEffect(() => setQd(f.q), [f.q]);
  useEffect(() => { const t = setInterval(() => setTick((x) => x + 1), 60000); return () => clearInterval(t); }, []);
  if (!can("view_dashboard")) return html`<${Empty} t="No access" d="Your role doesn’t include the usage dashboard. Ask an admin to grant “View the usage dashboard”." />`;
  const D = d.d, t = (D && D.totals) || {}, pv = (D && D.previous) || {}, o = (D && D.options) || {};
  const custom = !!(f.from || f.to), active = DASH_KEYS.filter((k) => f[k]);
  const sel = (k, label, opts, any) => html`<select class="field" aria-label=${label} value=${f[k]} onChange=${(e) => go({ [k]: e.target.value })}><option value="">${any}</option>${opts.map((x) => html`<option value=${x} key=${x} selected=${x === f[k]}>${x}</option>`)}</select>`;
  const exportCsv = () => { const ks = ["install", "use", "update", "uninstall", "error", "publish"]; saveBlob(new Blob([["date," + ks.join(","), ...D.daily.map((x) => [x.date, ...ks.map((k) => x[k] || 0)].join(","))].join("\n")], { type: "text/csv" }), `usage-${D.filters.from}_${D.filters.to}.csv`); };
  const seriesOn = Object.keys(series).filter((k) => series[k]);
  return html`<div class="stack"><div class="row"><h1>Dashboard</h1><span class="sp"></span>
      <div class="seg" role="group" aria-label="Period">${[7, 30, 90, 365].map((n) => html`<button key=${n} class=${!custom && f.days === n ? "on" : ""} onClick=${() => go({ days: n, from: "", to: "" })}>${n === 365 ? "1y" : n + "d"}</button>`)}<button class=${custom ? "on" : ""} onClick=${() => setOpen(!open)} aria-expanded=${open}>Custom</button></div>
      <button class="btn sec sm" onClick=${() => setTick(tick + 1)} title="Reload now">Refresh</button><button class="btn sec sm" onClick=${exportCsv} disabled=${!D}>Export CSV</button><button class="btn sec sm" onClick=${() => printReport(D, f)} disabled=${!D} title="Open the print dialog; choose Save as PDF">Export PDF</button></div>
    ${(open || custom) && html`<div class="row filters" role="group" aria-label="Date range"><label class="sm mut" for="df">From</label><input id="df" class="field" type="date" value=${f.from || (D && D.filters.from) || ""} max=${D && D.filters.to} onChange=${(e) => go({ from: e.target.value, to: f.to || (D && D.filters.to) || "" })} /><label class="sm mut" for="dt">To</label><input id="dt" class="field" type="date" value=${f.to || (D && D.filters.to) || ""} onChange=${(e) => go({ to: e.target.value, from: f.from || (D && D.filters.from) || "" })} />${custom && html`<button class="btn sec sm" onClick=${() => go({ from: "", to: "" })}>Back to presets</button>`}</div>`}
    <div class="row filters" role="group" aria-label="Filters">
      <form class="row" style="gap:6px" onSubmit=${(e) => { e.preventDefault(); go({ q: qd.trim() }); }}><input class="field" type="search" placeholder="Search package or component" aria-label="Search package or component" value=${qd} onInput=${(e) => setQd(e.target.value)} style="width:15rem" /></form>
      <select class="field" aria-label="Package" value=${f.package} onChange=${(e) => go({ package: e.target.value })}><option value="">All packages</option>${(pk.d ? pk.d.items : []).map((p) => html`<option value=${p.name} key=${p.name} selected=${p.name === f.package}>${p.name}</option>`)}</select>
      ${sel("type", "Type", o.types || [], "Any type")}${sel("source", "Tool", o.sources || [], "Any tool")}
      <select class="field" aria-label="Identity" value=${f.identity} onChange=${(e) => go({ identity: e.target.value })}><option value="">Anyone</option><option value="signed-in" selected=${f.identity === "signed-in"}>Signed-in only</option><option value="anonymous" selected=${f.identity === "anonymous"}>Anonymous only</option></select>
      ${sel("user", "Person", o.users || [], "Any person")}${sel("host", "Device", o.hosts || [], "Any device")}${sel("os", "OS", o.oses || [], "Any OS")}</div>
    <div class="row" role="group" aria-label="Event kinds" style="gap:6px"><span class="sm mut">Events</span><button class=${!f.kind ? "chip on" : "chip"} onClick=${() => go({ kind: "" })} aria-pressed=${!f.kind}>All</button>${DASH_KINDS.map((k) => { const on = f.kind.split(",").includes(k); return html`<button key=${k} class=${on ? "chip on" : "chip"} aria-pressed=${on} onClick=${() => go({ kind: (on ? f.kind.split(",").filter((x) => x && x !== k) : [...f.kind.split(",").filter(Boolean), k]).join(",") })}>${k}</button>`; })}</div>
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
      <div class="two"><div class="card stack"><h3>Top developers</h3><${Bars} items=${D.top_developers || []} color="var(--blue)" /></div><div class="card stack"><h3>Top people</h3><${Bars} items=${D.top_users} color="var(--green)" /></div></div>
      <div class="two"><div class="card stack"><h3>Devices</h3><${Bars} items=${D.by_host || []} color="var(--orange)" /></div><div class="card stack"><h3>By OS</h3><${Donut} items=${D.by_os || []} /></div></div>
      ${(D.by_os_daily && D.by_os_daily.series.length > 0) && html`<div class="card stack"><div class="row"><h3>Activity by OS</h3><span class="sp"></span><span class="xs mut3">all events per day, UTC</span></div><${LineChart} daily=${D.by_os_daily.daily} series=${D.by_os_daily.series} colors=${osColors(D.by_os_daily.series)} /></div>`}
      <div class="three"><div class="card stack"><h3>By type</h3><${Donut} items=${D.by_type} /></div><div class="card stack"><h3>By tool</h3><${Donut} items=${D.by_source} /></div><div class="card stack"><h3>Signed in vs anonymous</h3><${Donut} items=${D.by_identity || []} /></div></div>
      <div class="card stack"><div class="row"><h3>Top rated</h3><span class="sp"></span><span class="xs mut3">all time</span></div>${D.top_reviewed.length ? html`<div>${D.top_reviewed.slice(0, 8).map((x) => html`<div class="hb" key=${x.name}><span class="hl"><a href=${"#/package/" + x.name}>${x.name}</a></span><span class="mut sm">${x.reviews} ${x.reviews === 1 ? "review" : "reviews"}</span><b>${x.avg}</b></div>`)}</div>` : html`<p class="mut sm">No reviews yet.</p>`}</div></div>`}
    ${f.tab === "activity" && (log.loading && !log.d ? html`<${Skels} n=2 />` : log.e ? html`<${Err} e=${log.e} /> ` : log.d && html`<div class="card stack"><div class="row"><h3>Activity log</h3><span class="sp"></span><span class="xs mut3">${log.d.total} events</span></div>
      ${log.d.items.length ? html`<div class="tscroll"><table><thead><tr><th>When</th><th>Who</th><th>Event</th><th>Package</th><th>Component</th><th>Tool</th><th>Device</th><th>OS</th></tr></thead><tbody>
        ${log.d.items.map((e, i) => html`<tr key=${i}><td class="mut sm" title=${new Date(e.ts * 1000).toISOString()}>${ago(e.ts)}</td><td>${e.actor}</td><td><span class=${"badge " + ({ use: "blue", install: "green", error: "red", update: "orange" }[e.kind] || "")}>${e.kind}</span></td><td><a href=${"#/package/" + e.package}>${e.package}</a>${e.version ? html` <span class="mut sm">${e.version}</span>` : ""}</td><td class="mono sm">${e.component || ""}</td><td class="sm">${e.source}</td><td class="sm mut">${e.host || ""}</td><td class="sm mut">${e.os || ""}</td></tr>`)}</tbody></table></div>
        <div class="row"><button class="btn sec sm" disabled=${f.page <= 1} onClick=${() => nav(dashHash({ ...f, page: f.page - 1 }))}>Previous</button><span class="sm mut">Page ${log.d.page} of ${Math.max(1, Math.ceil(log.d.total / log.d.per_page))}</span><button class="btn sec sm" disabled=${f.page * log.d.per_page >= log.d.total} onClick=${() => nav(dashHash({ ...f, page: f.page + 1 }))}>Next</button></div>`
        : html`<${Empty} t="No events match" d="Widen the period or clear a filter." />`}</div>`)}</div>`;
}

// ---------- usage (tokens and estimated cost, per person and per group; uploaded by the CLI after each answer turn)
const USAGE_KEYS = ["scope", "group", "tool", "model", "user"];
const usageParams = () => { const q = new URLSearchParams((store.route.split("?")[1]) || ""); const o = { days: +q.get("days") || 30, from: q.get("from") || "", to: q.get("to") || "", tab: q.get("tab") || "summary", rank: q.get("rank") || "cost", range: ["today", "month"].includes(q.get("range")) ? q.get("range") : "" }; USAGE_KEYS.forEach((k) => (o[k] = q.get(k) || "")); o.scope = o.scope === "groups" ? "groups" : "me"; return o; };
const usageHash = (o) => { const q = new URLSearchParams(); if (o.range) q.set("range", o.range); else if (o.from || o.to) { o.from && q.set("from", o.from); o.to && q.set("to", o.to); } else if (o.days !== 30) q.set("days", o.days); USAGE_KEYS.forEach((k) => o[k] && !(k === "scope" && o[k] === "me") && q.set(k, o[k])); if (o.tab !== "summary") q.set("tab", o.tab); if (o.rank !== "cost") q.set("rank", o.rank); const t = q.toString(); return "/usage" + (t ? "?" + t : ""); };
const utcDay = (d = new Date()) => d.toISOString().slice(0, 10);
const RANGES = { today: () => [utcDay(), utcDay()], month: () => [utcDay().slice(0, 8) + "01", utcDay()] };   // resolved on every load, so a saved link keeps meaning "today"
const usd = (v) => "$" + (v >= 100 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(3));
const RANKS = [["cost", "Cost"], ["tokens", "Tokens"], ["requests", "Requests"], ["inp", "Input"], ["outp", "Output"], ["cache_write", "Cache write"], ["cache_read", "Cache read"], ["cache_hit", "Cache rate"]];
const UCOL = { cost: "var(--orange)", tokens: "var(--blue)", requests: "var(--green)", inp: "var(--blue)", outp: "var(--blue)", cache_write: "var(--orange)", cache_read: "var(--green)", cache_hit: "var(--green)" };
const usdAxis = (v) => "$" + (v >= 100 ? Math.round(v) : v >= 10 ? v.toFixed(0) : v.toFixed(1));
const ufmt = (k) => (k === "cost" ? usd : k === "cache_hit" ? (v) => Math.round(v * 100) + "%" : fmt);
const uval = (r, k) => (k === "cost" ? usd(r.cost || 0) : k === "cache_hit" ? Math.round((r.cache_hit || 0) * 100) + "%" : fmt(r[k] || 0));

// a tiny trend line for a KPI card: one value per day, newest on the right
function Spark({ vals, color }) {
  const n = vals.length; if (!n) return null; const W = 120, H = 34, mx = Math.max(1e-9, ...vals), x = (i) => (n < 2 ? W - 4 : 3 + i * (W - 7) / (n - 1)), y = (v) => H - 4 - (H - 8) * v / mx;
  const line = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  return html`<svg class="spark" viewBox=${`0 0 ${W} ${H}`} aria-hidden="true" style="width:100%;height:auto;display:block;margin-top:6px">${n > 1 && html`<path d=${line + `L${x(n - 1)},${H}L${x(0)},${H}Z`} fill=${color} fill-opacity=".14"/>`}${n > 1 && html`<path d=${line} fill="none" stroke=${color} stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>`}<circle cx=${x(n - 1)} cy=${y(vals[n - 1])} r="2.6" fill=${color}/></svg>`;
}

function UsageTable({ rows, name, sub, rank, onRank, empty }) {
  const sorted = [...(rows || [])].sort((a, b) => (b[rank] || 0) - (a[rank] || 0)), mx = Math.max(1e-9, ...sorted.map((r) => r[rank]));
  if (!sorted.length) return html`<p class="mut sm">${empty || "No usage in this view."}</p>`;
  return html`<div class="tscroll"><table><thead><tr><th>#</th><th>${name.label}</th>${RANKS.map(([k, l]) => html`<th key=${k} style="text-align:right"><button class=${"chip" + (rank === k ? " on" : "")} aria-pressed=${rank === k} onClick=${() => onRank(k)} title=${"Rank by " + l.toLowerCase()}>${l}${rank === k ? " ↓" : ""}</button></th>`)}<th style="width:22%"></th></tr></thead>
    <tbody>${sorted.map((r, i) => html`<tr key=${name.key(r)}><td class="mut sm">${i + 1}</td><td>${name.cell(r)}${sub ? html`<div class="xs mut3">${sub(r)}</div>` : ""}</td>${RANKS.map(([k]) => html`<td key=${k} class=${k === rank ? "" : "mut"} style="text-align:right;font-variant-numeric:tabular-nums">${uval(r, k)}</td>`)}<td><span class="ht"><i style=${`width:${Math.max(2, r[rank] / mx * 100)}%;background:${UCOL[rank]}`}></i></span></td></tr>`)}</tbody></table></div>`;
}


const pk = (n) => (n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n || 0));
const dur = (m) => (m < 1 ? "<1m" : m < 60 ? m + "m" : m < 1440 ? Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") + "m" : Math.floor(m / 1440) + "d");
const HINT_ICON = { warn: "var(--orange)", info: "var(--blue)" };

function UsageHints({ hints, onOpen }) {
  if (!hints.length) return html`<p class="mut sm">No hints: nothing stands out in this view.</p>`;
  return html`<div class="stack" style="gap:10px">${hints.map((h) => html`<div class="card" key=${h.id} style=${`border-left:4px solid ${HINT_ICON[h.level]};padding:14px 16px`}>
    <div class="row" style="gap:10px"><b>${h.title}</b><span class="sp"></span>${h.amount ? html`<span class="badge">${usd(h.amount)} affected</span>` : ""}<span class="xs mut3">${h.count} ${h.count === 1 ? "session" : "sessions"}</span></div>
    <p class="sm mut" style="margin:6px 0 0">${h.detail}</p></div>`)}</div>`;
}

function UsageSessions({ qs, rank, f, go, who }) {
  const [tick] = useState(0); const [by, setBy] = useState("cost");
  const d = useLoad(() => api("/usage/sessions?" + qs + "&limit=100"), [qs, tick]);
  if (d.loading && !d.d) return html`<${Skels} n=3 />`; if (d.e) return html`<${Err} e=${d.e} />`;
  const D = d.d, t = D.totals || {}, rows = [...D.sessions].sort((a, b) => b[by] - a[by]), mx = Math.max(1, ...rows.map((r) => r.peak));
  const col = (k, l) => html`<th style="text-align:right"><button class=${"chip" + (by === k ? " on" : "")} aria-pressed=${by === k} onClick=${() => setBy(k)}>${l}${by === k ? " ↓" : ""}</button></th>`;
  return html`<div class="stack">
    <div class="kpis">${[["Sessions", fmt(t.sessions || 0), "in this view"], ["Peak context", pk(t.peak || 0), `median ${pk(t.median_peak || 0)}`], ["Over 150k context", String(t.big_sessions || 0), "sessions to compact"], ["Avg requests", String(t.avg_requests || 0), "per session"]]
      .map(([l, v, n]) => html`<div class="card stat" key=${l}><span>${l}</span><b>${v}</b><span class="xs mut3">${n}</span></div>`)}</div>
    <div class="card stack"><div class="row"><h3>Usage hints</h3><span class="sp"></span><span class="xs mut3">from the numbers of the sessions below</span></div><${UsageHints} hints=${D.hints || []} /></div>
    <div class="card stack"><div class="row"><h3>Sessions</h3><span class="sp"></span><span class="xs mut3">${D.truncated ? "top 100 shown · " : ""}no titles or folders leave a machine, a session is its short id</span></div>
      ${rows.length ? html`<div class="tscroll"><table><thead><tr><th>Session</th>${f.scope === "groups" ? html`<th>Person</th>` : ""}<th>Model</th>${col("cost", "Cost")}${col("peak", "Peak context")}${col("requests", "Requests")}${col("cache_hit", "Cache hit")}${col("minutes", "Length")}<th>Flags</th></tr></thead>
        <tbody>${rows.map((r) => html`<tr key=${r.sid}><td><span class="mono sm">${r.id}</span><div class="xs mut3">${r.day} · ${r.tool}</div></td>${f.scope === "groups" ? html`<td>${who(r)}</td>` : ""}<td class="mono xs" style="max-width:9rem;overflow-wrap:anywhere">${r.model}</td>
          <td style="text-align:right;font-variant-numeric:tabular-nums">${usd(r.cost)}</td>
          <td style="text-align:right"><div class="row" style="gap:6px;justify-content:flex-end;flex-wrap:nowrap"><span class="ht" style="width:3.2rem"><i style=${`width:${Math.max(2, r.peak / mx * 100)}%;background:${r.peak > 150000 ? "var(--red)" : "var(--blue)"}`}></i></span><b class=${r.peak > 150000 ? "" : "mut"} style="font-variant-numeric:tabular-nums">${pk(r.peak)}</b></div></td>
          <td style="text-align:right">${r.requests}</td><td style="text-align:right" class=${r.cache_hit < 0.5 ? "" : "mut"}>${Math.round(r.cache_hit * 100)}%</td><td style="text-align:right" class="mut">${dur(r.minutes)}</td>
          <td class="xs" style="white-space:nowrap">${r.big ? html`<span class="badge red" title=${r.big + " requests over 150k tokens of context"}>big ${r.big}</span>` : ""}${r.cold ? html` <span class="badge orange" title=${r.cold + " cache rebuilds after an idle gap"}>cold ${r.cold}</span>` : ""}${r.compact ? html` <span class="badge green" title="context was compacted">compact</span>` : ""}</td></tr>`)}</tbody></table></div>`
        : html`<${Empty} t="No sessions yet" d="Sessions appear after the next answer turn of Claude Code, or run “aihub usage-sync” on your machine." />`}</div></div>`;
}

// Group scope for the usage page. Empty selection in the URL = every group of mine; edits are drafted here and applied once.
function GroupPicker({ groups, picked, onApply }) {
  const ids = groups.map((g) => String(g.id)), key = picked.join(",") + "|" + ids.join(",");
  const base = () => new Set(picked.length ? picked.filter((x) => ids.includes(x)) : ids);
  const [open, setOpen] = useState(false); const [q, setQ] = useState(""); const [draft, setDraft] = useState(base);
  useEffect(() => { setDraft(base()); }, [key]);
  useEffect(() => { if (!open) return; const k = (e) => { if (e.key === "Escape") setOpen(false); }; addEventListener("keydown", k); return () => removeEventListener("keydown", k); }, [open]);
  const cur = base(), fl = q.trim().toLowerCase(), rows = groups.filter((g) => !fl || g.name.toLowerCase().includes(fl));
  const change = (fn) => setDraft((d) => { const n = new Set(d); fn(n); return n; });
  const on = (g) => draft.has(String(g.id));
  const same = draft.size === cur.size && [...draft].every((x) => cur.has(x));
  const emit = (set) => onApply(set.size === ids.length ? "" : ids.filter((x) => set.has(x)).join(","));
  const names = groups.filter((g) => cur.has(String(g.id))).map((g) => g.name), all = cur.size === ids.length;
  const apply = () => { emit(draft); setOpen(false); };
  return html`<div class="gp"><div class="row gp-bar">
      <button type="button" class="btn sec sm gp-open" aria-expanded=${open} aria-controls="gp-panel" onClick=${() => setOpen(!open)}>Groups<b>${all ? "All " + ids.length : cur.size + " of " + ids.length}</b><svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style=${open ? "transform:rotate(180deg)" : ""}><path d="M2 4l4 4 4-4"/></svg></button>
      ${!all && names.slice(0, 4).map((n) => { const g = groups.find((x) => x.name === n); return html`<button type="button" key=${g.id} class="chip" title="Remove from the selection" onClick=${() => { const s2 = new Set(cur); s2.delete(String(g.id)); emit(s2.size ? s2 : new Set(ids)); }}>${n} <span aria-hidden="true">✕</span><span class="sr">remove ${n}</span></button>`; })}
      ${!all && names.length > 4 && html`<span class="xs mut3">+${names.length - 4} more</span>`}
      ${!all && html`<button type="button" class="chip" onClick=${() => onApply("")}>Reset to all</button>`}</div>
    ${open && html`<div class="gp-panel" id="gp-panel" role="group" aria-label="Choose groups">
      <div class="gp-tools"><div class="gp-find"><${Search} value=${q} onInput=${setQ} ph=${"Filter " + ids.length + " groups"} /></div>
        <button type="button" class="btn sec sm" onClick=${() => change((n) => rows.forEach((g) => n.add(String(g.id))))}>Select all${fl ? " shown" : ""}</button>
        <button type="button" class="btn sec sm" onClick=${() => change((n) => rows.forEach((g) => n.delete(String(g.id))))}>Clear${fl ? " shown" : ""}</button>
        <button type="button" class="btn sec sm" onClick=${() => change((n) => rows.forEach((g) => { const i = String(g.id); n.has(i) ? n.delete(i) : n.add(i); }))}>Invert</button></div>
      <div class="gp-list" tabindex="0" role="region" aria-label="Groups, scrollable">${rows.map((g) => html`<label class=${"gp-row" + (on(g) ? " on" : "")} key=${g.id}>
          <input type="checkbox" class="chk" checked=${on(g)} onChange=${() => change((n) => { const i = String(g.id); n.has(i) ? n.delete(i) : n.add(i); })} />
          <span class="gp-n">${g.name}</span><span class="xs mut3">${plural(g.members, "member")}</span>
          <button type="button" class="gp-only" title=${"Only " + g.name} onClick=${() => setDraft(new Set([String(g.id)]))}>Only</button></label>`)}
        ${!rows.length && html`<p class="kn-none">No group matches “${q}”.</p>`}</div>
      <div class="gp-foot"><span class="sm mut" aria-live="polite">${draft.size} of ${ids.length} selected${draft.size === 0 ? " · pick at least one" : ""}</span><span class="sp"></span>
        <button type="button" class="btn sec sm" onClick=${() => { setDraft(base()); setOpen(false); }}>Cancel</button>
        <button type="button" class="btn sm" disabled=${same || draft.size === 0} onClick=${apply}>Apply</button></div></div>`}</div>`;
}

function UsageHub() {
  const { me } = useStore(); const f = usageParams(); const [tick, setTick] = useState(0); const [open, setOpen] = useState(false); const [nd, setNd] = useState(String(f.days));
  const go = (patch) => nav(usageHash({ ...f, ...patch }));
  useEffect(() => setNd(String(f.days)), [f.days]);
  useEffect(() => { const t = setInterval(() => setTick((x) => x + 1), 60000); return () => clearInterval(t); }, []);
  const signed = !!(me || tok());                          // the token is there at once; `me` arrives a round trip later and must not delay the first request
  const allowed = can("view_usage");                       // brand / account arrive after the first render: a load that decided "no" must run again once it flips
  const gl = useLoad(() => signed && allowed ? api("/usage/groups") : Promise.resolve(null), [signed, allowed]);
  const ser = new URLSearchParams(); ser.set("scope", f.scope); if (f.range) { const [a, b] = RANGES[f.range](); ser.set("from", a); ser.set("to", b); } else if (f.from || f.to) { f.from && ser.set("from", f.from); f.to && ser.set("to", f.to); } else ser.set("days", f.days); USAGE_KEYS.slice(1).forEach((k) => f[k] && ser.set(k, f[k]));
  const qs = ser.toString();
  const d = useLoad(() => signed && allowed ? api("/usage?" + qs) : Promise.resolve(null), [signed, allowed, qs, tick]);
  const one = f.range === "today" || (!!f.from && f.from === f.to);                   // a single day has no trend: the sparklines then show the last 7 days
  const q7 = new URLSearchParams(ser); q7.delete("from"); q7.delete("to"); q7.set("days", 7);
  const wk = useLoad(() => signed && allowed && one ? api("/usage?" + q7.toString()) : Promise.resolve(null), [signed, allowed, one, q7.toString(), tick]);
  if (!can("view_usage")) return html`<${Empty} t="No access" d="Your role doesn’t include token usage. Ask an admin to grant “Token usage” in Admin → Roles." />`;
  if (!signed) return html`<${Empty} t="Sign in to see usage" d="Usage is tied to your account. Sign in, then run “aihub login” on your machine." />`;
  const D = d.d && (d.d.scope || f.scope) === f.scope ? d.d : null;       // the answer for the OTHER scope (Me <-> My groups) lacks by_group etc.: wait for the new one instead of crashing
  const t = (D && D.totals) || {}, o = (D && D.options) || {}, groups = (gl.d && gl.d.groups) || [], custom = !f.range && !!(f.from || f.to), preset = !f.range && !custom && [7, 30, 90, 365].includes(f.days);
  const picked = f.group ? f.group.split(",").filter(Boolean) : [];
  const toggle = (id) => { const s = String(id); go({ group: (picked.includes(s) ? picked.filter((x) => x !== s) : [...picked, s]).join(",") }); };
  const sel = (k, label, opts, any) => html`<select class="field" aria-label=${label} value=${f[k]} onChange=${(e) => go({ [k]: e.target.value })}><option value="">${any}</option>${opts.map((x) => html`<option value=${x} key=${x} selected=${x === f[k]}>${x}</option>`)}</select>`;
  const who = (r) => r.display_name && r.display_name !== r.username ? r.display_name : r.username;
  const exportCsv = () => saveBlob(new Blob([["date,requests,input,output,cache_write,cache_read,cost", ...D.daily.map((x) => [x.date, x.requests, x.inp, x.outp, x.cache_write, x.cache_read, x.cost].join(","))].join("\n")], { type: "text/csv" }), `token-usage-${D.filters.from}_${D.filters.to}.csv`);
  const active = USAGE_KEYS.slice(2).filter((k) => f[k]);
  const days = D && D.daily ? D.daily : [], last = days[days.length - 1], td = last && D.filters.to === utcDay() && last.date === D.filters.to ? last : null;
  const trend = one && wk.d && wk.d.daily ? wk.d.daily : days, yd = one && trend.length > 1 ? trend[trend.length - 2] : null;
  const tabs = f.scope === "groups" ? [["summary", "Summary"], ["sessions", "Sessions & hints"], ["models", "Models"], ["people", "People"], ["groups", "Groups"]] : [["summary", "Summary"], ["sessions", "Sessions & hints"], ["models", "Models"]];
  const tab = tabs.some(([k]) => k === f.tab) ? f.tab : "summary";
  const modelRows = (D ? D.by_model : []).map((m) => ({ ...m, key: m.tool + "/" + m.model }));
  return html`<div class="stack"><div class="row"><h1>Usage</h1><span class="sp"></span>
      <div class="seg" role="group" aria-label="Whose usage"><button class=${f.scope === "me" ? "on" : ""} onClick=${() => go({ scope: "me", group: "", user: "", tab: "summary" })}>Me</button><button class=${f.scope === "groups" ? "on" : ""} onClick=${() => go({ scope: "groups", tab: "summary" })} disabled=${!groups.length && !gl.loading} title=${groups.length ? "" : "You are not in a group"}>My groups</button></div></div>
    <div class="row"><div class="seg" role="group" aria-label="Period"><button class=${f.range === "today" ? "on" : ""} onClick=${() => go({ range: "today", from: "", to: "" })}>Today</button><button class=${f.range === "month" ? "on" : ""} onClick=${() => go({ range: "month", from: "", to: "" })}>This month</button>${[7, 30, 90, 365].map((n) => html`<button key=${n} class=${preset && f.days === n ? "on" : ""} onClick=${() => go({ days: n, range: "", from: "", to: "" })}>${n === 365 ? "1y" : n + "d"}</button>`)}<button class=${!f.range && !preset ? "on" : ""} onClick=${() => setOpen(!open)} aria-expanded=${open}>Custom</button></div>
      ${!f.range && (open || !preset) && html`<form class="row" style="gap:6px" onSubmit=${(e) => { e.preventDefault(); const n = parseInt(nd, 10); if (n >= 1 && n <= 366) go({ days: n, range: "", from: "", to: "" }); }}><label class="sm mut" for="ud">Last</label><input id="ud" class="field" type="number" min="1" max="366" value=${nd} onInput=${(e) => setNd(e.target.value)} style="width:5.5rem" /><span class="sm mut">days</span><button class="btn sec sm" type="submit">Apply</button>
        <label class="sm mut" for="uf">or from</label><input id="uf" class="field" type="date" value=${f.from || (D && D.filters.from) || ""} max=${D && D.filters.to} onChange=${(e) => go({ from: e.target.value, range: "", to: f.to || (D && D.filters.to) || "" })} /><label class="sm mut" for="ut">to</label><input id="ut" class="field" type="date" value=${f.to || (D && D.filters.to) || ""} onChange=${(e) => go({ to: e.target.value, range: "", from: f.from || (D && D.filters.from) || "" })} /></form>`}
      <span class="sp"></span><button class="btn sec sm" onClick=${() => setTick(tick + 1)}>Refresh</button><button class="btn sec sm" onClick=${exportCsv} disabled=${!D || !D.daily}>Export CSV</button></div>
    ${f.scope === "groups" && groups.length > 0 && html`<${GroupPicker} groups=${groups} picked=${picked} onApply=${(g) => go({ group: g })} />`}
    <div class="row filters" role="group" aria-label="Filters">${sel("tool", "Tool", o.tools || [], "Any tool")}${sel("model", "Model", o.models || [], "Any model")}${f.scope === "groups" && sel("user", "Person", ((D && D.members) || []).map((m) => m.username), "Anyone")}</div>
    ${active.length > 0 && html`<div class="row" style="gap:8px" aria-label="Active filters">${active.map((k) => html`<button key=${k} class="chip" onClick=${() => go({ [k]: "" })}>${k}: <b>${f[k]}</b> ✕</button>`)}</div>`}
    ${d.loading && D && html`<div class="ubar" role="progressbar" aria-label="Loading"></div>`}
    ${!D && !d.e ? html`<${Skels} n=4 />` : d.e ? html`<${Err} e=${d.e} />` : D && D.empty ? html`<${Empty} t="No groups yet" d="You are not in a group. Ask a group owner to add you, or look at your own usage with “Me”." />` : D && html`<div class="stack" style=${d.loading ? "opacity:.55;transition:opacity .15s" : "transition:opacity .15s"} aria-busy=${!!d.loading}>
      <div class="kpis">${[["Est. cost", usd(t.cost || 0), td && days.length > 1 ? `today ${usd(td.cost)}` : yd ? `yesterday ${usd(yd.cost)}` : "list-price estimate", "cost", "var(--orange)"], ["Tokens", fmt(t.tokens || 0), "input + output + cache", "tokens", "var(--blue)"], ["Requests", fmt(t.requests || 0), td && days.length > 1 ? `today ${fmt(td.requests)}` : yd ? `yesterday ${fmt(yd.requests)}` : "model calls", "requests", "var(--green)"],
        f.scope === "groups" ? ["Active people", String(D.people || 0), "with usage in this view", null] : ["Per request", t.requests ? usd(t.cost / t.requests) : "–", t.requests ? `${fmt(Math.round(t.tokens / t.requests))} tokens each` : "no requests", null]].map(([l, v, n, k, c]) => html`<div class="card stat" key=${l}><span>${l}</span><b>${v}</b><span class="xs mut3">${n}</span>${k && html`<${Spark} vals=${trend.map((x) => x[k] || 0)} color=${c} />`}</div>`)}</div>
      <div class="kpis" aria-label="Token breakdown">${[["Input", "inp", "var(--blue)", "fresh input tokens"], ["Output", "outp", "var(--blue)", "generated tokens"], ["Cache write", "cache_write", "var(--orange)", "input stored in the cache"], ["Cache read", "cache_read", "var(--green)", "input served from the cache"], ["Cache rate", "cache_hit", "var(--green)", "cache read ÷ all input"]]
        .map(([l, k, c, n]) => html`<div class="card stat" key=${k}><span>${l}</span><b>${uval(t, k)}</b><span class="xs mut3">${k === "cache_hit" ? n : `${t.tokens ? Math.round((t[k] || 0) / t.tokens * 100) : 0}% of tokens · ${n}`}</span><${Spark} vals=${trend.map((x) => x[k] || 0)} color=${c} /></div>`)}</div>
      <p class="xs mut3">${D.filters.from} to ${D.filters.to} · UTC days · cost is an estimate from list prices${t.unpriced ? ` · ${fmt(t.unpriced)} requests use a model with no known price` : ""}</p>
      <div class="seg" role="tablist" style="align-self:flex-start">${tabs.map(([k, l]) => html`<button key=${k} role="tab" aria-selected=${tab === k} class=${tab === k ? "on" : ""} onClick=${() => go({ tab: k })}>${l}</button>`)}</div>
      ${tab === "summary" && html`<div class="stack"><div class="card stack"><div class="row"><h3>Per day</h3><span class="sp"></span><span class="xs mut3">estimated cost</span></div>${days.length && t.requests ? html`<${LineChart} daily=${days} series=${["cost"]} colors=${{ cost: "var(--orange)" }} f=${usdAxis} labels=${{ cost: "Est. cost" }} />` : html`<${Empty} t="No usage in this view" d="Widen the period, clear a filter, or run “aihub usage-sync” on your machine." />`}</div>
        <div class="card stack"><h3>Tokens per day</h3>${days.length && t.requests ? html`<${LineChart} daily=${days} series=${["inp", "outp", "cache_write", "cache_read"]} colors=${{ inp: "var(--blue)", outp: "var(--red)", cache_write: "var(--orange)", cache_read: "var(--green)" }} labels=${{ inp: "Input", outp: "Output", cache_write: "Cache write", cache_read: "Cache read" }} />` : html`<p class="mut sm">No data.</p>`}</div>
        <div class="two"><div class="card stack"><h3>Top models</h3><${Bars} items=${[...modelRows].sort((a, b) => b[f.rank] - a[f.rank]).slice(0, 6).map((m) => ({ name: m.model, count: m[f.rank] }))} color=${UCOL[f.rank]} f=${ufmt(f.rank)} /></div>
          <div class="card stack"><h3>By tool</h3><${Donut} items=${(D.by_tool || []).map((x) => ({ name: x.tool, count: x[f.rank === "cache_hit" ? "cost" : f.rank] || 0 }))} f=${ufmt(f.rank === "cache_hit" ? "cost" : f.rank)} /></div></div>
        ${f.scope === "groups" && html`<div class="two"><div class="card stack"><h3>Top people</h3><${Bars} items=${[...(D.by_user || [])].sort((a, b) => b[f.rank] - a[f.rank]).slice(0, 6).map((u) => ({ name: who(u), count: u[f.rank] }))} color="var(--green)" f=${ufmt(f.rank)} /></div>
          <div class="card stack"><h3>Groups</h3><${Bars} items=${[...(D.by_group || [])].sort((a, b) => b[f.rank] - a[f.rank]).slice(0, 6).map((g) => ({ name: g.name, count: g[f.rank] }))} color="var(--blue)" f=${ufmt(f.rank)} /></div></div>`}
        <div class="row" style="gap:6px"><span class="sm mut">Rank by</span>${RANKS.map(([k, l]) => html`<button key=${k} class=${f.rank === k ? "chip on" : "chip"} aria-pressed=${f.rank === k} onClick=${() => go({ rank: k })}>${l}</button>`)}</div></div>`}
      ${tab === "sessions" && html`<${UsageSessions} qs=${qs} rank=${f.rank} f=${f} go=${go} who=${who} />`}
      ${tab === "models" && html`<div class="card stack"><div class="row"><h3>Model breakdown</h3><span class="sp"></span><span class="xs mut3">click a column to rank</span></div><${UsageTable} rows=${modelRows} rank=${f.rank} onRank=${(k) => go({ rank: k })} name=${{ label: "Model", key: (r) => r.key, cell: (r) => html`<span class="mono sm">${r.model}</span>` }} sub=${(r) => r.tool} /></div>`}
      ${tab === "people" && html`<div class="card stack"><div class="row"><h3>People</h3><span class="sp"></span><span class="xs mut3">ranked within the chosen groups, each person once</span></div><${UsageTable} rows=${D.by_user || []} rank=${f.rank} onRank=${(k) => go({ rank: k })} name=${{ label: "Person", key: (r) => r.username, cell: (r) => html`<a href=${"#" + usageHash({ ...f, user: r.username, tab: "summary" })}>${who(r)}</a>` }} sub=${(r) => (r.display_name && r.display_name !== r.username ? r.username : "")} /></div>`}
      ${tab === "groups" && html`<div class="card stack"><div class="row"><h3>Groups</h3><span class="sp"></span><span class="xs mut3">a person in two groups counts in both rows, once in the totals</span></div><${UsageTable} rows=${D.by_group || []} rank=${f.rank} onRank=${(k) => go({ rank: k })} name=${{ label: "Group", key: (r) => r.id, cell: (r) => html`<a href=${"#" + usageHash({ ...f, group: String(r.id), tab: "people" })}>${r.name}</a>` }} sub=${(r) => r.members + " members"} /></div>`}
    </div>`}</div>`;
}

// Top-right menu: a trigger plus a small panel of links. On phones the panel is always open inside the burger menu (CSS).
// ---------- spaces: documents, slides, reports, PDFs, sheets, dashboards. Address author@space-name, link #/s/author@space-name
const SKIND = { doc: "Doc", slides: "Slides", report: "Report", pdf: "PDF", sheet: "Sheet", dashboard: "Dashboard", html: "HTML page" };
const SK_ORDER = ["slides", "doc", "report", "dashboard", "sheet", "pdf", "html"];
const SK_HELP = ".pptx or an .html deck · .docx .md .txt · .html report or dashboard (or a .zip site) · .xlsx .csv · .pdf";
const hashQ = () => new URLSearchParams((location.hash.split("?")[1] || ""));
const setHashQ = (k, v) => { const [p, q] = location.hash.split("?"), ps = new URLSearchParams(q || ""); v == null ? ps.delete(k) : ps.set(k, v); history.replaceState(null, "", (p || "#/") + (ps.toString() ? "?" + ps : "")); };
const sendFile = (method, path, file, onProg) => new Promise((ok, no) => {          // raw body with upload progress
  const x = new XMLHttpRequest(); x.open(method, U("/api/v1" + path)); if (tok()) x.setRequestHeader("Authorization", "Bearer " + tok());
  x.setRequestHeader("Content-Type", "application/octet-stream");
  x.upload.onprogress = (e) => e.lengthComputable && onProg && onProg(e.loaded / e.total);
  x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch {} x.status < 300 ? ok(j) : no(new Error(typeof j.detail === "string" ? j.detail : x.statusText || "Upload failed")); };
  x.onerror = () => no(new Error("Network error")); x.send(file);
});
const slugOf = (n) => String(n || "").replace(/\.(tar\.gz|[^.]+)$/i, "").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[-._]+|[-._]+$/g, "").slice(0, 64);
const SpCard = ({ s, i = 0 }) => html`<a class="card sp-card" style=${"--i:" + Math.min(i, 12) + ";--h:" + hue(s.slug)} href=${"#/s/" + s.ref}>
  <div class="sp-tile"><b>${SKIND[s.kind] || s.kind}</b>${s.visibility === "private" && html`<span class="badge orange">private</span>`}</div>
  <div class="sp-body"><h3>${s.title}</h3><p class="sp-ref">${s.ref}</p><p>${s.description || ""}</p>
    <div class="meta"><span>${fmt(s.views)} views</span><span>${ago(s.updated)}</span></div></div></a>`;
function SpCarousel({ items }) {
  const [i, setI] = useState(0), [hold, setHold] = useState(false), n = items.length;
  useEffect(() => { if (reduced || hold || n < 2) return; const t = setInterval(() => setI((x) => (x + 1) % n), 6500); return () => clearInterval(t); }, [hold, n]);
  useEffect(() => { const f = () => setHold(document.hidden); document.addEventListener("visibilitychange", f); return () => document.removeEventListener("visibilitychange", f); }, []);
  if (!n) return null;
  return html`<section class="sp-car" aria-roledescription="carousel" aria-label="Top spaces" onMouseEnter=${() => setHold(true)} onMouseLeave=${() => setHold(false)} onFocusIn=${() => setHold(true)} onFocusOut=${() => setHold(false)}>
    <div class="sp-car-track" style=${"transform:translateX(-" + i * 100 + "%)"}>${items.map((s, k) => html`<a key=${s.ref} class="sp-slide" style=${"--h:" + hue(s.slug)} href=${"#/s/" + s.ref} aria-hidden=${k !== i} tabindex=${k === i ? 0 : -1}>
      <div class="sp-slide-in"><span class="badge">${SKIND[s.kind]}</span><h2>${s.title}</h2><p>${s.description || s.ref}</p><div class="meta"><span>${s.ref}</span><span>${fmt(s.views)} views</span></div></div></a>`)}</div>
    ${n > 1 && html`<div class="sp-car-ctl"><button class="btn sec sm" aria-label="Previous" onClick=${() => setI((i + n - 1) % n)}>‹</button>${items.map((s, k) => html`<button key=${s.ref} class=${"sp-dot" + (k === i ? " on" : "")} aria-label=${"Show " + s.title} onClick=${() => setI(k)}></button>`)}<button class="btn sec sm" aria-label="Next" onClick=${() => setI((i + 1) % n)}>›</button></div>`}</section>`;
}
function SpaceForm({ onDone, space }) {         // create a space (or, with `space`, send a new version of it) from the browser
  const [file, setFile] = useState(null), [name, setName] = useState(""), [title, setTitle] = useState(""), [kind, setKind] = useState("auto"), [vis, setVis] = useState("public");
  const [view, setView] = useState("fit"), [pct, setPct] = useState(null), [drag, setDrag] = useState(false), [desc, setDesc] = useState("");
  const pick = (f) => { if (!f) return; setFile(f); if (!space && !name) setName(slugOf(f.name)); if (!space && !title) setTitle(f.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ")); };
  const go = async (e) => {
    e.preventDefault(); if (!file) return toast("Choose a file first", 1);
    const q = new URLSearchParams({ filename: encodeURIComponent(file.name) });
    if (kind !== "auto") q.set("kind", kind);
    if (!space) { if (name) q.set("name", name); if (title) q.set("title", title); if (desc) q.set("description", desc); q.set("visibility", vis); q.set("view", view); }
    setPct(0);
    try { const r = space ? await sendFile("PUT", "/spaces/" + space + "/content?" + q, file, setPct) : await sendFile("POST", "/spaces?" + q, file, setPct); toast(r.created ? "Space published" : "Space updated"); onDone(r.space); }
    catch (er) { toast(er.message, 1); setPct(null); }
  };
  return html`<form class="card sp-form" onSubmit=${go}>
    <label class=${"sp-drop" + (drag ? " on" : "")} onDragOver=${(e) => { e.preventDefault(); setDrag(true); }} onDragLeave=${() => setDrag(false)} onDrop=${(e) => { e.preventDefault(); setDrag(false); pick(e.dataTransfer.files[0]); }}>
      <input type="file" onChange=${(e) => pick(e.target.files[0])} />
      <b>${file ? file.name : "Drop a file here or click to choose"}</b><span>${file ? (file.size / 1048576).toFixed(1) + " MB" : SK_HELP}</span></label>
    ${!space && html`<div class="sp-grid2">
      <label>Name<input class="field" value=${name} placeholder="q3-review" onInput=${(e) => setName(slugOf(e.target.value))} /><small>Your link will be <code>${(store.me && store.me.username) || "you"}@${name || "name"}</code></small></label>
      <label>Title<input class="field" value=${title} onInput=${(e) => setTitle(e.target.value)} /></label></div>
      <label>Description<input class="field" value=${desc} placeholder="One line shown on cards" onInput=${(e) => setDesc(e.target.value)} /></label>`}
    <div class="sp-grid2"><label>Kind<select class="field" value=${kind} onChange=${(e) => setKind(e.target.value)}><option value="auto">Detect from the file</option>${SK_ORDER.map((k) => html`<option value=${k}>${SKIND[k]}</option>`)}</select></label>
      ${!space && html`<label>Opens as<select class="field" value=${view} onChange=${(e) => setView(e.target.value)}><option value="fit">Fit the screen</option><option value="canvas">Canvas (inside the page)</option><option value="full">Full screen</option></select></label>`}</div>
    ${!space && html`<div class="row"><div class="seg" role="group" aria-label="Who can see it">${["public", "private"].map((v) => html`<button type="button" class=${vis === v ? "on" : ""} onClick=${() => setVis(v)}>${v === "public" ? "Public link" : "Private"}</button>`)}</div>
      <span class="sp-hint">${vis === "public" ? "Anyone with the link, signed in or not." : "Only you, plus people and groups you share it with."}</span></div>`}
    ${pct != null && html`<div class="ubar" style="position:static"><i style=${"width:" + Math.round(pct * 100) + "%"}></i></div>`}
    <div class="row"><button class="btn" disabled=${pct != null}>${pct != null ? "Uploading…" : space ? "Upload new version" : "Publish space"}</button><span class="sp-hint">or from a terminal: <code>aihub space publish ${file ? file.name : "FILE"}</code></span></div></form>`;
}
function Spaces() {
  const { me } = useStore(); const qs = hashQ();
  const [q, setQ] = useState(qs.get("q") || ""), [kind, setKind] = useState(qs.get("kind") || ""), [tab, setTab] = useState(qs.get("tab") || "all"), [mk, setMk] = useState(qs.get("new") === "1");
  const dq = debounced(q), find = !!(dq || kind || tab === "mine");
  const ov = useLoad(() => api("/spaces/overview"), []);
  const res = useLoad(() => find ? api("/spaces?" + new URLSearchParams({ q: dq, kind, sort: dq ? "top" : "new", limit: 60, ...(tab === "mine" ? { mine: "true" } : {}) })) : Promise.resolve(null), [dq, kind, tab, me && me.username]);
  useReveal();
  const kinds = (ov.d && ov.d.kinds) || {};
  return html`<div class="stack"><div class="kn-head"><div><h1>Spaces</h1><p class="lead">Slides, docs, reports, sheets and dashboards your team can open straight from a link.</p></div>
      ${me && can("publish") && html`<button class="btn" aria-expanded=${mk} onClick=${() => setMk(!mk)}>${mk ? "Close" : "New space"}</button>`}</div>
    ${mk && html`<${SpaceForm} onDone=${(s) => nav("/s/" + s.ref)} />`}
    <div class="sp-find"><${Search} value=${q} onInput=${setQ} ph="Find a space: title, author, tag…" />
      <div class="sp-chips">${me && html`<button class=${"chip" + (tab === "mine" ? " on" : "")} onClick=${() => setTab(tab === "mine" ? "all" : "mine")}>Mine</button>`}
        <button class=${"chip" + (!kind ? " on" : "")} onClick=${() => setKind("")}>All</button>${SK_ORDER.filter((k) => kinds[k] || kind === k).map((k) => html`<button key=${k} class=${"chip" + (kind === k ? " on" : "")} onClick=${() => setKind(kind === k ? "" : k)}>${SKIND[k]} <i>${kinds[k] || 0}</i></button>`)}</div></div>
    ${find ? (res.d ? (res.d.items.length ? html`<div class="grid">${res.d.items.map((s, i) => html`<${SpCard} key=${s.ref} s=${s} i=${i} />`)}</div>` : html`<${Empty} t="No spaces found" d=${tab === "mine" ? "You have not published any yet. Use New space or aihub space publish." : "Try other words, or clear the filters."} />`) : res.e ? html`<${Err} e=${res.e} />` : html`<${Skels} n=${6} />`)
      : ov.d ? (!ov.d.top.length ? html`<${Empty} t="No spaces yet" d=${me ? "Publish the first one with New space, or: aihub space publish deck.pptx" : "Sign in to publish the first one."} />` : html`
        <${SpCarousel} items=${ov.d.carousel} />
        <section><h2 class="sp-h">Top spaces</h2><div class="grid">${ov.d.top.slice(0, 6).map((s, i) => html`<${SpCard} key=${s.ref} s=${s} i=${i} />`)}</div></section>
        <section><h2 class="sp-h">Recently updated</h2><div class="grid">${ov.d.recent.slice(0, 6).map((s, i) => html`<${SpCard} key=${s.ref} s=${s} i=${i} />`)}</div></section>`)
      : ov.e ? html`<${Err} e=${ov.e} />` : html`<${Skels} n=${6} />`}</div>`;
}
// ---- viewers
const SI = { canvas: "M3 5h14v10H3zM6 17.5h8", fit: "M3 10h14M6 7l-3 3 3 3M14 7l3 3-3 3", full: "M4 8V4h4M16 8V4h-4M4 12v4h4M16 12v4h-4", link: "M8.5 11.5l3-3M7.5 9L6 10.5a2.8 2.8 0 004 4L11.5 13M12.5 11L14 9.5a2.8 2.8 0 00-4-4L8.5 7", expand: "M11 4h5v5M16 4l-6 6M8 5H4v11h11v-4", down: "M10 3v9M6 9l4 4 4-4M4 16h12", gear: "M10 7a3 3 0 100 6 3 3 0 000-6zM10 2v2M10 16v2M2 10h2M16 10h2M4.6 4.6l1.4 1.4M14 14l1.4 1.4M4.6 15.4L6 14M14 6l1.4-1.4", share: "M15 5.5a2 2 0 100-.01M5 10a2 2 0 100-.01M15 14.5a2 2 0 100-.01M6.8 9l6.4-3M6.8 11l6.4 3", trash: "M4 6h12M8 6V4h4v2M6 6l1 10h8l1-10" };
const SIc = ({ d }) => html`<svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${d} /></svg>`;
const TRN = ["fade", "slide", "zoom", "flip"];
// One slide = one sandboxed iframe on a fixed-size stage, keyed by slide number so a frame is never remounted; the neighbours load in the background.
// Kept deliberately plain: switching slides only flips which frame is visible (a 150 ms fade, or none).
function SpFrame({ d, idx, k, on }) {
  const s = d.slides[idx], body = d.source === "html" ? s.html.replace(/^<section class="/, '<section class="active present is-active current ') : s.html;
  return html`<div class=${"sp-fr " + (on ? "cur" : "off")}><div class="sp-fr-in" style=${`width:${d.w}px;height:${d.h}px;transform:translate(-50%,-50%) scale(${k})`}>
    <iframe sandbox="" scrolling="no" tabindex="-1" title=${"Slide " + (idx + 1)} width=${d.w} height=${d.h} srcdoc=${`<!doctype html><meta charset="utf-8"><base target="_blank"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;position:relative;font-family:sans-serif}${d.css}</style><body>${body}</body>`}></iframe></div></div>`;
}
function SlidePlayer({ d, st, mode }) {
  const n = d.slides.length, wrap = useRef(), [k, setK] = useState(1), [i, setI] = useState(() => Math.min(n - 1, Math.max(0, (+hashQ().get("slide") || 1) - 1)));
  const [play, setPlay] = useState(+st.autoplay > 0), down = useRef(null), last = useRef(0), swiped = useRef(false);
  useEffect(() => { const el = wrap.current; if (!el) return; let raf = 0; const f = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { const r = el.getBoundingClientRect(); if (r.width) setK(mode === "canvas" ? 1 : Math.min(r.width / d.w, r.height / d.h)); }); };
    f(); const ro = new ResizeObserver(f); ro.observe(el); return () => { cancelAnimationFrame(raf); ro.disconnect(); }; }, [d, mode]);
  const [hid, setHid] = useState(() => document.hidden);
  useEffect(() => { const f = () => setHid(document.hidden); document.addEventListener("visibilitychange", f); return () => document.removeEventListener("visibilitychange", f); }, []);
  const go = useCallback((to, auto) => {
    if (!auto) setPlay(false);                                               // any manual move stops auto-advance (it used to keep running over the user's choice)
    const now = Date.now(); if (now - last.current < 160) return;            // one step per gesture: a held key or a long swipe cannot run through the deck
    const t = st.loop ? (to + n) % n : Math.max(0, Math.min(n - 1, to)); if (t === i) return;
    last.current = now; setI(t); setHashQ("slide", t ? t + 1 : null);
  }, [i, n, st.loop]);
  useEffect(() => { if (!play || hid || !(+st.autoplay > 0)) return; const t = setTimeout(() => { if (i >= n - 1 && !st.loop) setPlay(false); else go(i + 1, true); }, st.autoplay * 1000); return () => clearTimeout(t); }, [play, hid, i, st.autoplay, go]);
  useEffect(() => {
    const key = (e) => { if (e.repeat || /^(INPUT|TEXTAREA|SELECT)$/.test((e.target || {}).tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      const m = { ArrowRight: 1, ArrowDown: 1, PageDown: 1, " ": 1, ArrowLeft: -1, ArrowUp: -1, PageUp: -1 }[e.key];
      if (m) { e.preventDefault(); go(i + m); } else if (e.key === "Home") go(0); else if (e.key === "End") go(n - 1); };
    addEventListener("keydown", key); return () => removeEventListener("keydown", key);
  }, [go, i, n]);
  const up = (e) => { const dx = e.clientX - (down.current ?? e.clientX); down.current = null; swiped.current = Math.abs(dx) > 40; if (swiped.current) go(i + (dx < 0 ? 1 : -1)); };
  const hit = (e) => { if (swiped.current) { swiped.current = false; return; } const r = e.currentTarget.getBoundingClientRect(), left = e.clientX - r.left < r.width / 3; go(i + (left ? -1 : 1)); };
  const shown = [...new Set([i - 1, i, i + 1].map((x) => (st.loop ? (x + n) % n : x)).filter((x) => x >= 0 && x < n))];
  const sty = mode === "canvas" ? `width:${d.w}px;height:${d.h}px` : mode === "fit" ? `aspect-ratio:${d.w}/${d.h};width:min(100%,calc((100vh - 140px) * ${d.w / d.h}));margin:0 auto` : "";
  return html`<div class=${"sp-player sp-p-" + mode}><div class="sp-cv"><div class=${"sp-slides sp-r-" + mode + (reduced || st.transition === "none" ? " nofx" : "")} ref=${wrap} style=${sty}>
      ${shown.map((x) => html`<${SpFrame} key=${x} d=${d} idx=${x} k=${k} on=${x === i} />`)}
      <div class="sp-hit" onClick=${hit} onPointerDown=${(e) => { down.current = e.clientX; }} onPointerUp=${up} onPointerCancel=${() => { down.current = null; }}></div>
      <div class="sp-pill" role="group" aria-label="Slide controls"><button aria-label="Previous slide" disabled=${i === 0 && !st.loop} onClick=${() => go(i - 1)}>‹</button><span aria-live="polite">${i + 1} <i>/</i> ${n}</span>
        <button aria-label="Next slide" disabled=${i === n - 1 && !st.loop} onClick=${() => go(i + 1)}>›</button>
        ${+st.autoplay > 0 && html`<button class="pp" aria-label=${play ? "Pause" : "Play"} onClick=${() => setPlay(!play)}>${play ? "❚❚" : "▶"}</button>`}</div>
      <div class="sp-prog"><i style=${"width:" + ((i + 1) / n) * 100 + "%"}></i></div></div></div></div>`;
}
function SheetView({ d }) {
  const [t, setT] = useState(0), [f, setF] = useState(""), [lim, setLim] = useState(300); const sh = d.sheets[t] || { rows: [] }, fl = f.trim().toLowerCase();
  const head = sh.rows[0] || [], body = sh.rows.slice(1).filter((r) => !fl || r.some((c) => String(c).toLowerCase().includes(fl))), w = Math.max(head.length, ...body.slice(0, 50).map((r) => r.length));
  return html`<div class="sp-sheet"><div class="row">${d.sheets.length > 1 && html`<div class="seg">${d.sheets.map((s, i) => html`<button key=${i} class=${t === i ? "on" : ""} onClick=${() => { setT(i); setLim(300); }}>${s.name}</button>`)}</div>`}
      <input class="field" type="search" style="max-width:260px" placeholder="Filter rows" value=${f} onInput=${(e) => { setF(e.target.value); setLim(300); }} /><span class="sp-hint">${body.length} rows${sh.cut ? ` (first ${sh.rows.length} of ${sh.total})` : ""}</span></div>
    <div class="sp-tw"><table><thead><tr><th class="n">#</th>${Array.from({ length: w }, (_, c) => html`<th key=${c}>${head[c] || ""}</th>`)}</tr></thead>
      <tbody>${body.slice(0, lim).map((r, ri) => html`<tr key=${ri}><td class="n">${ri + 2}</td>${Array.from({ length: w }, (_, c) => html`<td key=${c}>${r[c] || ""}</td>`)}</tr>`)}</tbody></table>
      ${body.length > lim && html`<button class="btn sec sm" onClick=${() => setLim(lim + 500)}>Show more</button>`}</div></div>`;
}
function FrameView({ s }) {
  const u = useLoad(() => api("/spaces/" + s.ref + "/viewurl"), [s.ref, s.rev]);
  if (!u.d) return u.e ? html`<${Err} e=${u.e} />` : html`<div class="skel" style="height:60vh"></div>`;
  return html`<iframe class="sp-frame" title=${s.title} src=${u.d.url} allow="fullscreen" allowfullscreen sandbox=${s.kind === "pdf" ? null : "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-downloads allow-modals"}></iframe>`;
}
function Av({ n, t }) { return html`<span class=${"sp-av " + t} style=${"--h:" + hue(n)} aria-hidden="true">${t === "group" ? "#" : (n[0] || "?").toUpperCase()}</span>`; }
function Dlg({ title, onClose, cls = "", children }) {
  const r = useRef();
  useEffect(() => { const d = r.current; if (d && !d.open) d.showModal(); return () => { if (d && d.open) d.close(); }; }, []);   // native modal: top layer, so the page's transformed ancestors cannot trap it
  return html`<dialog class=${"dlg " + cls} ref=${r} aria-label=${title} onCancel=${(e) => { e.preventDefault(); onClose(); }} onClick=${(e) => { if (e.target === r.current) onClose(); }}>
    <div class="dlg-h"><h3>${title}</h3><button type="button" class="btn sec sm" onClick=${onClose} aria-label="Close">Close</button></div><div class="dlg-b">${children}</div></dialog>`;
}
function SpaceShare({ s, onClose }) {
  const [tick, setTick] = useState(0), [pick, setPick] = useState({}), [flt, setFlt] = useState(""), [type, setType] = useState(""), [acc, setAcc] = useState("view"), [busy, setBusy] = useState(false);
  const a = useLoad(() => api("/spaces/" + s.ref + "/access"), [s.ref, tick]);
  const all = useLoad(() => api("/groups/lookup?limit=500"), [s.ref]);
  const have = new Set(((a.d && a.d.access) || []).map((x) => x.type + ":" + x.name)), q = flt.trim().toLowerCase();
  const cand = [...((all.d && all.d.groups) || []).map((n) => ({ type: "group", name: n, label: n })), ...((all.d && all.d.users) || []).map((u) => ({ type: "user", name: u.username, label: u.display_name ? u.display_name + " (" + u.username + ")" : u.username }))]
    .filter((c) => c.name !== s.owner && !have.has(c.type + ":" + c.name) && (!q || c.label.toLowerCase().includes(q)));
  const key = (c) => c.type + ":" + c.name, chosen = Object.keys(pick).filter((k) => pick[k]);
  const toggle = (c) => setPick({ ...pick, [key(c)]: !pick[key(c)] });
  const share = async () => { setBusy(true);
    try { const items = chosen.map((k) => { const [type, ...r] = k.split(":"); return { type, name: r.join(":") }; });
      try { await api("/spaces/" + s.ref + "/access", { method: "PUT", body: { items, access: acc } }); }
      catch (er) { if (!/type must be/.test(er.message)) throw er;            // an older hub only knows {type, name}: share one by one
        for (const it of items) await api("/spaces/" + s.ref + "/access", { method: "PUT", body: { ...it, access: acc } }); }
      toast("Shared with " + items.length); setPick({}); setTick(tick + 1); }
    catch (er) { toast(er.message, 1); } setBusy(false); };
  const rm = async (x) => { try { await api("/spaces/" + s.ref + "/access/" + x.type + "/" + encodeURIComponent(x.name), { method: "DELETE" }); setTick(tick + 1); } catch (er) { toast(er.message, 1); } };
  const setLevel = async (x, access) => { try { await api("/spaces/" + s.ref + "/access", { method: "PUT", body: { type: x.type, name: x.name, access } }); setTick(tick + 1); } catch (er) { toast(er.message, 1); } };
  const shown = type ? cand.filter((c) => c.type === type) : cand, mine = (a.d && a.d.access) || [];
  return html`<${Dlg} title="Who can open it" cls="sp-share" onClose=${onClose}><p class="sp-hint">${s.visibility === "public" ? "Public: anyone with the link. Sharing below only matters if you make it private." : "Private: you, plus these people and groups."}</p>
    <div class="sp-tools"><input class="field" type="search" placeholder="Filter people and groups" value=${flt} onInput=${(e) => setFlt(e.target.value)} />
      <div class="sp-chips">${[["", "All"], ["user", "People"], ["group", "Groups"]].map(([k, l]) => html`<button type="button" key=${k} class=${"chip" + (type === k ? " on" : "")} onClick=${() => setType(k)}>${l}</button>`)}
        <button class="chip" type="button" onClick=${() => { const n = { ...pick }; shown.forEach((c) => { n[key(c)] = !shown.every((x) => pick[key(x)]); }); setPick(n); }} disabled=${!shown.length}>${shown.length && shown.every((c) => pick[key(c)]) ? "Clear" : "Select all"}</button></div></div>
    <ul class="sp-pick">${all.d ? shown.map((c) => html`<li key=${key(c)}><label class=${"sp-prow" + (pick[key(c)] ? " on" : "")}><input type="checkbox" checked=${!!pick[key(c)]} onChange=${() => toggle(c)} /><${Av} n=${c.name} t=${c.type} /><span class="sp-pn"><b>${c.label}</b></span><span class="badge">${c.type === "group" ? "group" : "person"}</span></label></li>`) : html`<li class="sp-hint">Loading…</li>`}${all.d && !shown.length && html`<li class="sp-hint">Nobody left to pick.</li>`}</ul>
    <div class="sp-bar"><select class="field sm" value=${acc} onChange=${(e) => setAcc(e.target.value)} aria-label="Access level"><option value="view">can view</option><option value="edit">can edit</option></select>
      <button class="btn sm" onClick=${share} disabled=${!chosen.length || busy}>${busy ? "Sharing…" : chosen.length ? "Share with " + chosen.length : "Share"}</button></div>
    <h3 style="margin-top:12px">Already shared <span class="mute">${mine.length || ""}</span></h3>
    <ul class="sp-pick">${mine.map((x) => html`<li key=${x.type + x.name}><div class="sp-prow"><${Av} n=${x.name} t=${x.type} /><span class="sp-pn"><b>${x.name}</b><small>${x.type === "group" ? "group" : "person"}</small></span>
      <select class="field sm" value=${x.access} onChange=${(e) => setLevel(x, e.target.value)} aria-label=${"Access for " + x.name}><option value="view">can view</option><option value="edit">can edit</option></select>
      <button class="btn sec sm" onClick=${() => rm(x)} aria-label=${"Remove " + x.name}>Remove</button></div></li>`)}${a.d && !mine.length && html`<li class="sp-hint">Not shared with anyone yet.</li>`}</ul><//>`;
}
function SpaceSettings({ s, onSaved }) {
  const st = s.settings || {}, [f, setF] = useState({ title: s.title, description: s.description, tags: s.tags.join(", "), view: st.view || "fit", transition: st.transition || "auto", autoplay: st.autoplay || 0, loop: !!st.loop, visibility: s.visibility });
  const [nv, setNv] = useState(false); const up = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const save = async (e) => { e.preventDefault(); try { const b = { ...f, autoplay: +f.autoplay || 0 }; if (!s.can_manage) delete b.visibility; onSaved(await api("/spaces/" + s.ref, { method: "PATCH", body: b })); toast("Saved"); } catch (er) { toast(er.message, 1); } };
  return html`<div class="sp-panel"><form class="stack" onSubmit=${save}><h3>Settings</h3>
    <div class="sp-grid2"><label>Title<input class="field" value=${f.title} onInput=${up("title")} /></label><label>Tags<input class="field" value=${f.tags} placeholder="finance, q3" onInput=${up("tags")} /></label></div>
    <label>Description<input class="field" value=${f.description} onInput=${up("description")} /></label>
    <div class="sp-grid2"><label>Default size when opened<select class="field" value=${f.view} onChange=${up("view")}><option value="fit">Fit the screen</option><option value="canvas">Canvas (inside the page)</option><option value="full">Full screen</option></select></label>
      ${s.can_manage && html`<label>Visibility<select class="field" value=${f.visibility} onChange=${up("visibility")}><option value="public">Public link</option><option value="private">Private</option></select></label>`}</div>
    ${s.kind === "slides" && html`<div class="sp-grid2"><label>Transition<select class="field" value=${f.transition} onChange=${up("transition")}><option value="auto">As in the file</option><option value="none">None</option><option value="fade">Fade</option><option value="slide">Slide</option><option value="zoom">Zoom</option><option value="flip">Flip</option></select></label>
      <label>Auto-advance (seconds, 0 = off)<input class="field" type="number" min="0" max="3600" value=${f.autoplay} onInput=${up("autoplay")} /></label></div>
      <label class="sp-check"><input type="checkbox" checked=${f.loop} onChange=${up("loop")} /> Start over after the last slide</label>`}
    <div class="row"><button class="btn">Save</button><button type="button" class="btn sec" onClick=${() => setNv(!nv)}>${nv ? "Cancel" : "Upload a new version"}</button></div></form>
    ${nv && html`<${SpaceForm} space=${s.ref} onDone=${(x) => { setNv(false); onSaved(x); }} />`}</div>`;
}
function SpacePage({ sref: rf }) {
  const { me } = useStore(); const [tick, setTick] = useState(0), [panel, setPanel] = useState(null), [local, setLocal] = useState(null), [ask, setAsk] = useState(false);
  const m = useLoad(() => api("/spaces/" + rf), [rf, tick, me && me.username]);
  const s = local && m.d && local.rev >= m.d.rev ? { ...m.d, ...local } : m.d;
  const [mode, setMode] = useState(null), stage = useRef(), [pre, setPre] = useState(null);
  const eff = mode || hashQ().get("mode") || (s && s.settings.view) || "fit";
  const needData = s && ["slides", "doc", "sheet"].includes(s.kind);
  const data = useLoad(() => needData ? api("/spaces/" + rf + "/data") : Promise.resolve(null), [rf, s && s.rev, needData]);
  useEffect(() => { api("/spaces/" + rf + "/view", { method: "POST" }).catch(() => {}); }, [rf]);
  useEffect(() => { document.body.classList.toggle("sp-full", eff === "full"); return () => document.body.classList.remove("sp-full"); }, [eff]);
  useEffect(() => {          // Fit = wide focus mode: the page column opens up to almost the full width and the hub bar hides (move to the top edge, or tap the handle, to bring it back)
    const b = document.body; if (eff !== "fit" || !s) return;
    b.classList.add("sp-wide");
    const mv = (e) => { const nav = e.clientY < 14 || (b.classList.contains("nav-peek") && e.clientY < 90); b.classList.toggle("nav-peek", nav); };
    const fin = (e) => { if (e.target.closest && e.target.closest(".nav")) b.classList.add("nav-peek"); };
    addEventListener("mousemove", mv); addEventListener("focusin", fin);
    return () => { removeEventListener("mousemove", mv); removeEventListener("focusin", fin); b.classList.remove("sp-wide", "nav-peek"); };
  }, [eff, !!s]);
  useEffect(() => { const f = () => { if (!document.fullscreenElement && eff === "full") { setMode(pre || "fit"); setPre(null); } }; document.addEventListener("fullscreenchange", f); return () => document.removeEventListener("fullscreenchange", f); }, [eff, pre]);
  useEffect(() => { const k = (e) => { if (e.key === "Escape" && eff === "full") pick(pre || "fit"); }; addEventListener("keydown", k); return () => removeEventListener("keydown", k); }, [eff, pre]);
  if (!s) return m.e ? html`<${Empty} t="Space not found" d=${me ? "It may be private, or the address is wrong. " + m.e : "It may be private: sign in, then open the link again."} />` : html`<div class="skel" style="height:60vh"></div>`;
  const pick = (v) => {
    if (v === eff) return;
    if (v === "full") { setPre(eff); const el = stage.current; if (el && el.requestFullscreen) el.requestFullscreen().catch(() => {}); }   // inside the click, or the browser refuses
    else { setPre(null); if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); }
    setMode(v);
  };
  const link = (extra) => location.origin + location.pathname.replace(/\/$/, "/") + "#/s/" + s.ref + (extra ? "?" + extra : "");
  const download = async () => { try { const r = await fetch(U("/api/v1/spaces/" + s.ref + "/file"), { headers: tok() ? { Authorization: "Bearer " + tok() } : {} }); if (!r.ok) throw new Error("Download failed"); saveBlob(await r.blob(), s.filename || s.slug); } catch (e) { toast(e.message, 1); } };
  const del = async () => { try { await api("/spaces/" + s.ref, { method: "DELETE" }); toast("Space deleted"); nav("/spaces"); } catch (e) { toast(e.message, 1); setAsk(false); } };
  const body = !needData ? html`<${FrameView} s=${s} />` : data.d ? (s.kind === "slides" ? html`<${SlidePlayer} key=${s.rev} d=${data.d} st=${s.settings || {}} mode=${eff} />` : s.kind === "sheet" ? html`<${SheetView} d=${data.d} />` : html`<article class="sp-doc" dangerouslySetInnerHTML=${{ __html: data.d.html }}></article>`) : data.e ? html`<${Err} e=${data.e} />` : html`<div class="skel" style="height:50vh"></div>`;
  const Ib = ({ d, label, on, onClick, cls = "" }) => html`<button class=${"sp-ib " + cls + (on ? " on" : "")} title=${label} aria-label=${label} aria-pressed=${on == null ? null : !!on} onClick=${onClick}><${SIc} d=${d} /><span class="lb">${label}</span></button>`;
  return html`<div class=${"stack sp-page sp-m-" + eff}>
    ${eff === "fit" && html`<button class="sp-peek" aria-label="Show the hub menu" onClick=${() => document.body.classList.toggle("nav-peek")}><svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 8l5 5 5-5"/></svg></button>`}
    <div class="sp-head"><a class="sp-back" href="#/spaces">‹ Spaces</a><h1>${s.title}</h1>
      <p class="sp-ref"><${Avatar} p=${{ username: s.owner, display_name: s.owner_name, avatar_url: s.owner_avatar }} size=${18} /> ${s.ref}<span class="badge blue">${SKIND[s.kind]}</span>${s.visibility === "private" && html`<span class="badge orange">private</span>`}<span>${fmt(s.views)} views · updated ${ago(s.updated)}</span></p>
      ${s.description && html`<p class="sp-desc">${s.description}</p>`}</div>
    <div class="sp-tools" role="toolbar" aria-label="Space tools">
      <div class="sp-modes" role="group" aria-label="Size">${[["canvas", "Canvas", SI.canvas], ["fit", "Fit width", SI.fit], ["full", "Full screen", SI.full]].map(([v, l, d]) => html`<button key=${v} class=${eff === v ? "on" : ""} aria-pressed=${eff === v} onClick=${() => pick(v)}><${SIc} d=${d} /><span class="lb">${l}</span></button>`)}</div>
      <span class="sp-sep"></span>
      <${Ib} d=${SI.link} label="Copy link" onClick=${() => copy(link())} /><${Ib} d=${SI.expand} label="Full screen link" onClick=${() => copy(link("mode=full"))} /><${Ib} d=${SI.down} label="Download" onClick=${download} />
      ${(s.can_edit || s.can_manage) && html`<span class="sp-sep"></span>`}
      ${s.can_edit && html`<${Ib} d=${SI.gear} label="Settings" on=${panel === "set"} onClick=${() => setPanel(panel === "set" ? null : "set")} />`}
      ${s.can_manage && html`<${Ib} d=${SI.share} label="Share" on=${panel === "share"} onClick=${() => setPanel(panel === "share" ? null : "share")} />`}
      ${s.can_manage && (ask ? html`<span class="sp-ask">Delete <b>${s.ref}</b> for everyone?<button class="btn sec sm" onClick=${() => setAsk(false)}>Cancel</button><button class="btn danger sm" onClick=${del}>Delete</button></span>` : html`<${Ib} d=${SI.trash} label="Delete" cls="warn" onClick=${() => setAsk(true)} />`)}</div>
    ${panel === "set" && s.can_edit && html`<${SpaceSettings} s=${s} onSaved=${(x) => { setLocal(x); setTick(tick + 1); setPanel(null); }} />`}
    ${panel === "share" && s.can_manage && html`<${SpaceShare} s=${s} onClose=${() => setPanel(null)} />`}
    <div class=${"sp-stage sp-k-" + s.kind + " sp-s-" + eff} ref=${stage}>${eff === "full" && html`<div class="sp-fl"><button class="btn sec sm" onClick=${() => pick(pre || "fit")}>Exit full screen</button></div>`}${body}</div></div>`;
}
function NavMenu({ label, lead, items }) {
  const [open, setOpen] = useState(false); const ref = useRef();
  useEffect(() => {
    if (!open) return;
    const away = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const key = (e) => { if (e.key === "Escape") { setOpen(false); const b = ref.current && ref.current.querySelector("button"); if (b) b.focus(); } };
    addEventListener("pointerdown", away); addEventListener("keydown", key); return () => { removeEventListener("pointerdown", away); removeEventListener("keydown", key); };
  }, [open]);
  const act = items.some((i) => i.on);
  return html`<div class=${"dd" + (open ? " open" : "") + (act ? " on" : "")} ref=${ref}>
    <button type="button" class="dd-b" aria-haspopup="true" aria-expanded=${open} onClick=${() => setOpen(!open)}>${lead}<span>${label}</span><svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 4l4 4 4-4"/></svg></button>
    <div class="dd-p">${items.map((i) => html`<a key=${i.k} href=${i.href} class=${i.on ? "on" : ""} aria-current=${i.on ? "page" : null} onClick=${(e) => { setOpen(false); i.onClick && i.onClick(e); }}>${i.label}</a>`)}</div></div>`;
}

// ---------- shell
function App() {
  const s = useStore(); const path = s.route.split("?")[0], seg = path.split("/").filter(Boolean);
  useEffect(() => { loadMe(); loadBrand(); }, []);
  const lk = useRef(), ind = useRef();
  const place = () => { const n = lk.current, i = ind.current; if (!n || !i) return; const a = n.querySelector("a.on"); if (!a) { i.style.opacity = 0; return; } i.style.opacity = 1; i.style.transform = `translateX(${a.offsetLeft}px) scaleX(${a.offsetWidth})`; };
  useEffect(() => { place(); }, [path]);
  const first = useRef(true);
  const bare = new URLSearchParams(s.route.split("?")[1] || "").get("bare") === "1" && seg[0] === "s";   // a space link without the hub header and footer (for embedding)
  useEffect(() => { document.body.classList.toggle("bare", bare); return () => document.body.classList.remove("bare"); }, [bare]);
  const [menu, setMenu] = useState(false);
  useEffect(() => { setMenu(false); }, [s.route]);
  useEffect(() => { if (!menu) return; const k = (e) => { if (e.key === "Escape") setMenu(false); }; addEventListener("keydown", k); return () => removeEventListener("keydown", k); }, [menu]);
  useEffect(() => {
    const k = (e) => { const t = e.target, typing = t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
      const slash = e.key === "/" && !typing && !e.metaKey && !e.ctrlKey, cmdk = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k";
      if (slash || cmdk) { const f = document.querySelector(".search input"); if (f) { e.preventDefault(); f.focus(); f.select(); } else if (!typing || cmdk) { e.preventDefault(); nav("/browse"); } }
      else if (e.key === "Escape" && typing && t.type === "search") t.blur(); };
    addEventListener("keydown", k); return () => removeEventListener("keydown", k);
  }, []);
  useEffect(() => {
    const top = document.getElementById("top"); if (!top || !window.IntersectionObserver) return;
    const io = new IntersectionObserver(([e]) => document.body.classList.toggle("scrolled", !e.isIntersecting));
    io.observe(top); return () => io.disconnect();
  }, []);
  useEffect(() => {
    const moved = !first.current; first.current = false;
    const t = requestAnimationFrame(() => { const h1 = document.querySelector("main h1"); document.title = (h1 && h1.textContent ? h1.textContent.trim() + " · " : "") + ((s.brand && s.brand.name) || "AI Hub"); const h = h1 || document.getElementById("main"); if (h && moved) { if (!h.hasAttribute("tabindex")) h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); } });
    return () => cancelAnimationFrame(t);
  }, [path]);
  useEffect(() => { addEventListener("resize", place); return () => removeEventListener("resize", place); }, []);
  const logout = async () => { try { await api("/auth/logout", { method: "POST" }); } catch {} try { localStorage.removeItem("aihub_token"); sessionStorage.removeItem("aihub_token"); } catch {} set({ me: null }); nav("/"); };
  const on = (k) => (seg[0] === k ? "on" : "");
  const nl = (k, label) => ({ k, label, href: "#/" + k, on: seg[0] === k });
  const ws = s.me ? [nl("knowledge", "Knowledge"), nl("running", "Running"), can("view_usage") && nl("usage", "Usage"), s.showGroups && nl("groups", "Groups")].filter(Boolean) : [];
  const mg = s.me ? [can("view_dashboard") && nl("dashboard", "Dashboard"), can("audit") && nl("audit", "Audit"), (can("admin") || can("reset_password")) && nl("admin", "Admin")].filter(Boolean) : [];
  const navGroup = (label, items) => items.length > 1 ? html`<${NavMenu} label=${label} items=${items} />` : items.length ? html`<a class=${items[0].on ? "on" : ""} href=${items[0].href}>${items[0].label}</a>` : "";
  const br = s.brand || {}, ct = br.contact || {};
  const page = { "": html`<${Home} />`, browse: html`<${Browse} key=${s.route} />`, package: html`<${Package} name=${seg[1]} key=${seg[1]} />`, rankings: html`<${Rankings} />`, docs: html`<${Docs} slug=${seg[1]} key=${seg[1]} />`, start: html`<${Start} />`,
    login: html`<${Auth} mode="login" />`, register: html`<${Auth} mode="register" />`, account: html`<${Account} />`, admin: html`<${Admin} />`, knowledge: html`<${Knowledge} />`, dashboard: html`<${Dashboard} />`, usage: html`<${UsageHub} />`, running: html`<${Running} />`, audit: html`<${Audit} />`, groups: html`<${Groups} />`, spaces: html`<${Spaces} key=${s.route.split("?")[0]} />`, s: html`<${SpacePage} sref=${decodeURIComponent(seg[1] || "")} key=${seg[1]} />` }[seg[0] || ""] || html`<${Empty} t="Page not found" d="That page doesn’t exist." />`;
  return html`<div><a class="skip" href="#/" onClick=${(e) => { e.preventDefault(); const m = document.getElementById("main"); if (m) m.focus(); }}>Skip to content</a>${br.announcement && html`<div class="announce" role="status" dangerouslySetInnerHTML=${{ __html: br.announcement }}></div>`}<header class="nav"><div class="nav-in"><a class="brand glow" href="#/">${br.logo_url && html`<img decoding="async" class="logo" src=${br.logo_url} alt="" />`}<span>${br.name || "AI Hub"}</span></a>
    <button type="button" class="burger" aria-label="Menu" aria-expanded=${menu} aria-controls="menu" onClick=${() => setMenu(!menu)}><i></i><i></i><i></i></button>
    <div id="menu" class=${"menu" + (menu ? " open" : "")} onClick=${(e) => { if (e.target.closest && e.target.closest("a")) setMenu(false); }}><nav class="links" aria-label="Main" ref=${lk}><i class="ind" ref=${ind}></i><a class=${on("browse")} href="#/browse">Browse</a><a class=${on("rankings")} href="#/rankings">Rankings</a><a class=${on("spaces") || on("s")} href="#/spaces">Spaces</a><a class=${on("start")} href="#/start">Get started</a><a class=${on("docs")} href="#/docs">Docs</a></nav>
    <div class="r">${s.me ? html`${navGroup("Workspace", ws)}${navGroup("Manage", mg)}<${NavMenu} label=${shown(s.me)} lead=${html`<${Avatar} p=${s.me} size=${22} />`} items=${[{ k: "account", label: "Account", href: "#/account", on: seg[0] === "account" }, { k: "out", label: "Sign out", href: "#/", onClick: logout }]} />` :
      html`${can("view_dashboard") ? html`<a href="#/dashboard">Dashboard</a>` : ""}<a href="#/login">Sign in</a>`}</div></div></div></header>
    <main id="main" tabindex="-1">${seg[0] === "login" || seg[0] === "register" ? html`<div class="page au-page" key="auth">${page}</div>` : html`<div class="page" key=${path}>${page}</div>`}</main>
    <footer class="foot"><b class="glow">${br.logo_url && html`<img decoding="async" class="logo sm" src=${br.logo_url} alt="" />`}${br.name || "AI Hub"}</b><span>Skills, agents and MCP servers for your team.</span>
      ${(ct.name || ct.email || ct.url || ct.phone) && html`<span class="contact" aria-label="Contact">${ct.name && html`<span>${ct.name}</span>`}${ct.email && html`<a href=${"mailto:" + ct.email}>${ct.email}</a>`}${ct.phone && html`<span>${ct.phone}</span>`}${ct.url && /^https?:\/\//i.test(ct.url) && html`<a href=${ct.url} target="_blank" rel="noopener noreferrer">Support</a>`}</span>`}<nav aria-label="Footer"><a href="#/browse">Browse</a><a href="#/rankings">Rankings</a><a href="#/start">Get started</a><a href="#/docs">Docs</a></nav>${br.server_version && html`<span class="ver xs">v${br.server_version}</span>`}</footer>
    ${s.toast && html`<div class=${"toast " + (s.toast.err ? "err" : "")} role="status">${s.toast.m}</div>`}</div>`;
}
render(html`<${App} />`, document.getElementById("app"));
