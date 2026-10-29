// Diagrams drawn from CODE in an answer, by libraries loaded lazily from static/vendor/lazy (vendored, no CDN): ```mermaid (flowchart, sequence, mindmap, ER, gantt,
// class, state, timeline, pie, git graph...), ```dot / ```graphviz (Graphviz), ```echarts (any ECharts option as JSON: graph, tree, treemap, sankey,
// radar, heatmap, scatter...), ```map (Leaflet: markers, lines, GeoJSON on OpenStreetMap tiles). A library is fetched only when a block needs it.
import { html, useState, useEffect, useRef } from "./vendor/preact-htm.js";
import DOMPurify from "./vendor/purify.js";
import { Ic } from "./icons.js";

const LAZY = new URL("./vendor/lazy/", import.meta.url).href;      // vendored copies (ui/tools/vendor_libs.py); no CDN
export const DIAGRAMS = { mermaid: "Diagram", dot: "Graph", graphviz: "Graph", echarts: "Chart", map: "Map" };

const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;
const libs = {};
const once = (key, make) => libs[key] || (libs[key] = make().catch((e) => { delete libs[key]; throw e; }));
const script = (src) => new Promise((ok, no) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = () => no(new Error("could not load " + src)); document.head.append(s); });
const css = (local) => { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = LAZY + local; document.head.append(l); };

const loadMermaid = () => once("mermaid", async () => (await import(LAZY + "mermaid.mjs")).default);
const loadViz = () => once("viz", async () => (await import(LAZY + "viz.mjs")).instance());
const loadEcharts = () => once("echarts", async () => { await script(LAZY + "echarts.min.js"); return window.echarts; });
const loadLeaflet = () => once("leaflet", async () => { css("leaflet/leaflet.css"); await script(LAZY + "leaflet/leaflet.js"); return window.L; });

let queue = Promise.resolve(), seq = 0;                         // mermaid renders one at a time (it keeps global state)
const inLine = (fn) => { const r = queue.then(fn); queue = r.catch(() => {}); return r; };
const cache = new Map();                                        // lang + theme + code -> svg text, so a re-render of the answer never flashes
const remember = (k, v) => { cache.set(k, v); if (cache.size > 60) cache.delete(cache.keys().next().value); return v; };

async function mermaidSvg(code) {
  const k = "m" + dark() + code; if (cache.has(k)) return cache.get(k);
  const m = await loadMermaid();
  return inLine(async () => {
    m.initialize({ startOnLoad: false, securityLevel: "strict", theme: dark() ? "dark" : "neutral", flowchart: { htmlLabels: false }, fontFamily: "ui-sans-serif, -apple-system, Segoe UI, Inter, sans-serif" });
    return remember(k, (await m.render("mmd" + ++seq, code)).svg);
  });
}
async function dotSvg(code) {
  const k = "d" + code; if (cache.has(k)) return cache.get(k);
  const viz = await loadViz();
  const lay = code.match(/layout\s*=\s*"?(\w+)/);                       // graph [layout=neato] picks the engine, default dot
  const svg = viz.renderString(code, { format: "svg", engine: lay ? lay[1] : "dot" });
  return remember(k, DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } }));
}

// ---- echarts: JSON only (no functions), theme follows the page
function drawEcharts(host, spec) {
  return loadEcharts().then((E) => {
    if (!spec || typeof spec !== "object") throw new Error("echarts block must be a JSON option object");
    const c = E.init(host, dark() ? "dark" : null, { renderer: "canvas" });
    c.setOption({ backgroundColor: "transparent", animationDuration: 600, ...spec });
    const ro = new ResizeObserver(() => c.resize()); ro.observe(host);
    return { png: () => c.getDataURL({ pixelRatio: 2, backgroundColor: dark() ? "#30302e" : "#fff" }), stop: () => { ro.disconnect(); c.dispose(); } };
  });
}

// ---- map: {center:[lat,lng], zoom, markers:[{lat,lng,label}], lines:[{points:[[lat,lng]..], label}], geojson:{...}} on OpenStreetMap tiles
const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
function drawMap(host, spec) {
  return loadLeaflet().then((L) => {
    if (!spec || typeof spec !== "object") throw new Error("map block must be a JSON object");
    const map = L.map(host, { scrollWheelZoom: false });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" }).addTo(map);
    const group = L.featureGroup().addTo(map), tip = (layer, label) => { if (label) layer.bindPopup(document.createTextNode(String(label).slice(0, 300))); return layer; };
    (spec.markers || []).slice(0, 500).forEach((m) => { const la = num(m && m.lat), ln = num(m && (m.lng ?? m.lon)); if (la !== null && ln !== null) tip(L.marker([la, ln]), m.label || m.name).addTo(group); });
    (spec.lines || []).slice(0, 100).forEach((l) => { const pts = ((l && l.points) || []).filter((p) => Array.isArray(p) && num(p[0]) !== null && num(p[1]) !== null).slice(0, 5000);
      if (pts.length > 1) tip(L.polyline(pts, { color: "#d97757", weight: 4 }), l.label).addTo(group); });
    if (spec.geojson && typeof spec.geojson === "object") try { L.geoJSON(spec.geojson, { onEachFeature: (f, ly) => { const p = f.properties || {}; tip(ly, p.name || p.label || p.title); } }).addTo(group); } catch (e) { /* bad geojson: the rest still draws */ }
    const c = Array.isArray(spec.center) && num(spec.center[0]) !== null && num(spec.center[1]) !== null ? spec.center : null;
    if (c) map.setView(c, num(spec.zoom) ?? 5); else if (group.getLayers().length) map.fitBounds(group.getBounds().pad(0.2), { maxZoom: 14 }); else map.setView([20, 0], 2);
    setTimeout(() => map.invalidateSize(), 50);
    return { stop: () => map.remove() };
  });
}

const download = (name, href) => { const a = document.createElement("a"); a.href = href; a.download = name; a.click(); };
const svgUrl = (svg) => URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));

export function Diagram({ lang, code, title }) {
  const host = useRef(), [state, setState] = useState("busy"), [msg, setMsg] = useState(""), [svg, setSvg] = useState(""), api = useRef(null);
  const canvas = lang === "echarts" || lang === "map";
  useEffect(() => {
    let dead = false; setState("busy");
    const fail = (e) => { if (!dead) { setMsg(String((e && e.message) || e).split("\n").slice(0, 3).join(" ").slice(0, 300)); setState("err"); } };
    const json = () => { try { return JSON.parse(code); } catch (e) { throw new Error("invalid JSON: " + e.message); } };
    (async () => {
      if (lang === "mermaid" || lang === "dot" || lang === "graphviz") {
        const s = await (lang === "mermaid" ? mermaidSvg(code) : dotSvg(code)); if (dead) return;
        setSvg(s); setState("ok");
      } else {
        const h = host.current, spec = json(); const a = await (lang === "echarts" ? drawEcharts(h, spec) : drawMap(h, spec));
        if (dead) a.stop(); else { api.current = a; setState("ok"); }
      }
    })().catch(fail);
    return () => { dead = true; if (api.current) { api.current.stop(); api.current = null; } };
  }, [lang, code]);
  const copy = (e) => navigator.clipboard.writeText(code).then(() => { const s = e.currentTarget.querySelector("span"); s.textContent = "Copied"; setTimeout(() => (s.textContent = "Copy"), 1400); });
  const save = () => (canvas ? api.current && api.current.png && download("chart.png", api.current.png()) : download("diagram.svg", svgUrl(svg)));
  return html`<div class=${"dg dg-" + lang}><div class="bk-head"><span class="bk-title">${Ic(lang === "echarts" ? "chart" : "layers", 14)}<span>${title || DIAGRAMS[lang]}</span></span>
      <span class="bk-acts">${state === "ok" && lang !== "map" && html`<button type="button" class="bk-btn" onClick=${save}>${Ic("download", 14)}<span>${canvas ? "PNG" : "SVG"}</span></button>`}
        <button type="button" class="bk-btn" onClick=${copy}>${Ic("copy", 14)}<span>Copy</span></button></span></div>
    ${state === "busy" && html`<div class="dg-busy">Drawing…</div>`}
    ${state === "err" && html`<div class="dg-err"><b>Could not draw this ${lang} block.</b> <span class="mute small">${msg}</span><pre>${code}</pre></div>`}
    ${canvas ? html`<div class=${"dg-host dg-" + lang + "-host" + (state === "err" ? " off" : "")} ref=${host}></div>`
      : state === "ok" && html`<div class="dg-svg" dangerouslySetInnerHTML=${{ __html: svg }}></div>`}</div>`;
}
