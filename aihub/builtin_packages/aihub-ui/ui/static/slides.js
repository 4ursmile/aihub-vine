// Canvas viewer for slide decks and documents (files written with the aihub-slides conventions) + the Themes settings section.
import { html, useState, useEffect, useRef } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, guard, Pop, Modal, ask } from "./ui.js";
import { toPptx, toDocx, toMarkdown, safeName, loadJSZip } from "./export.js";

const KIND = (t) => (/name=["']aihub-kind["'][^>]*content=["']slides/i.test(t) || /<section[^>]+class=["'][^"']*\bslide\b/i.test(t) ? "slides"
  : /name=["']aihub-kind["'][^>]*content=["']doc/i.test(t) || /<article[^>]+class=["'][^"']*\bdoc\b/i.test(t) ? "doc" : "page");
const kinds = new Map();
export async function kindOf(url) {                       // slides | doc | page, cached per url+mtime
  if (kinds.has(url)) return kinds.get(url);
  try { const r = await fetch(url); const k = r.ok ? KIND(await r.text()) : "page"; kinds.set(url, k); return k; } catch (e) { return "page"; }
}

export function Viewer({ url, name, sandbox, ver }) {
  const [kind, setKind] = useState(null), [mode, setMode] = useState("show"), [info, setInfo] = useState({ n: 0, i: 0, titles: [], grid: false }), [menu, setMenu] = useState(false), [busy, setBusy] = useState(""), [full, setFull] = useState(false);
  const fr = useRef(), box = useRef(), pending = useRef({});
  useEffect(() => { setKind(null); setMode("show"); kindOf(url + "?v=" + ver).then(setKind); }, [url, ver]);
  useEffect(() => {
    const on = (e) => {
      const m = e.data; if (!m || !m.aihub || !fr.current || e.source !== fr.current.contentWindow) return;
      if (m.t === "info") setInfo({ n: m.n, i: m.i, titles: m.titles || [], grid: !!m.grid });
      else if (m.t === "extract" && pending.current[m.id]) { const p = pending.current[m.id]; delete pending.current[m.id]; m.error ? p.rej(new Error(m.error)) : p.ok(m.data); }
    };
    addEventListener("message", on); const fs = () => setFull(document.fullscreenElement === box.current); document.addEventListener("fullscreenchange", fs);
    return () => { removeEventListener("message", on); document.removeEventListener("fullscreenchange", fs); };
  }, []);
  const send = (m) => fr.current && fr.current.contentWindow && fr.current.contentWindow.postMessage({ aihub: 1, ...m }, "*");
  const extract = () => new Promise((ok, rej) => { const id = Math.random().toString(36).slice(2); pending.current[id] = { ok, rej }; send({ t: "extract", id }); setTimeout(() => { if (pending.current[id]) { delete pending.current[id]; rej(new Error("The page did not answer")); } }, 15000); });
  const run = (label, fn) => async () => { setMenu(false); setBusy(label); try { await fn(); } catch (e) { toast(e.message); } setBusy(""); };
  const pdf = () => { const f = document.createElement("iframe"); f.setAttribute("sandbox", sandbox); f.style.cssText = "position:fixed;left:-9999px;top:0;width:1280px;height:720px;border:0"; f.src = url + "?view=print&v=" + ver; document.body.append(f); setTimeout(() => f.remove(), 120000); toast("Choose Save as PDF in the print dialog"); };
  const base = safeName(name.replace(/\.html?$/i, ""));
  const items = kind === "slides" ? [["PowerPoint (.pptx)", "Editable text, lists, tables and pictures", run("pptx", async () => toPptx(await extract(), base))], ["PDF", "One slide per page", pdf], ["HTML", "The deck file itself", () => { setMenu(false); location.href = url + "?download=1"; }]]
    : kind === "doc" ? [["Word (.docx)", "Headings, lists, tables, pictures", run("docx", async () => toDocx(await extract(), base))], ["PDF", "A4 pages", pdf], ["Markdown (.md)", "Plain text with structure", run("md", async () => toMarkdown(await extract(), base))], ["HTML", "The document file itself", () => { setMenu(false); location.href = url + "?download=1"; }]] : [];
  if (kind === null) return html`<div class="empty"><div class="shine">Opening…</div></div>`;
  const show = kind !== "page" && mode === "show", src = url + "?" + (show ? (kind === "slides" ? "view=deck&" : "view=doc&") : "") + "v=" + ver;
  return html`<div class="viewer" ref=${box}><div class="vbar">
      ${kind !== "page" && html`<span class="tag">${Ic(kind === "slides" ? "slides" : "file", 12)} ${kind === "slides" ? "Slides" : "Document"}</span>`}<span class="grow mono small" style="overflow:hidden;text-overflow:ellipsis">${name}</span>
      ${kind !== "page" && html`<button class="ibtn" title=${mode === "show" ? "Show the raw page" : "Back to the presentation"} onClick=${() => setMode(mode === "show" ? "raw" : "show")}>${Ic(mode === "show" ? "code" : "eye", 15)}</button>`}
      ${items.length > 0 && html`<${Pop} open=${menu} setOpen=${setMenu} align="right" width=${290} up=${false} button=${html`<button class="btn acc" disabled=${!!busy} onClick=${() => setMenu(!menu)}>${Ic("download", 14)} ${busy ? "Exporting…" : "Export"}</button>`}>
        ${items.map(([t, d, fn]) => html`<button class="opt" onClick=${fn}><span class="grow"><b>${t}</b><span class="d">${d}</span></span></button>`)}<//>`}</div>
    <iframe ref=${fr} key=${src} sandbox=${sandbox} src=${src}></iframe>
    ${show && kind === "slides" && html`<div class="vnav"><button class="ibtn" title="Previous" onClick=${() => send({ t: "prev" })}>${Ic("left", 16)}</button><span class="small">${info.i + 1} / ${info.n}</span><button class="ibtn" title="Next" onClick=${() => send({ t: "next" })}>${Ic("right", 16)}</button>
      <span class="vtitle small mute">${info.titles[info.i] || ""}</span><button class=${"ibtn" + (info.grid ? " on" : "")} title="All slides (g)" onClick=${() => send({ t: "grid", on: !info.grid })}>${Ic("grid", 16)}</button>
      <button class="ibtn" title="Full screen" onClick=${() => (full ? document.exitFullscreen() : box.current.requestFullscreen && box.current.requestFullscreen())}>${Ic("expand", 16)}</button></div>`}</div>`;
}

// ---------- Themes (Settings): colors + fonts for slides and documents; made by hand or imported from a .pptx
const COLORS = [["bg", "Background"], ["fg", "Text"], ["muted", "Muted text"], ["accent", "Accent"], ["accent2", "Accent (dark)"], ["surface", "Panels"]];
const NEW = { name: "", colors: { bg: "#faf9f5", fg: "#141413", muted: "#73726c", accent: "#d97757", accent2: "#c6613f", surface: "#f0eee6" }, fonts: { head: "Georgia, serif", body: "system-ui, sans-serif" }, notes: "" };

export async function themeFromPptx(file) {
  const Z = await loadJSZip(), zip = await Z.loadAsync(await file.arrayBuffer());
  const f = Object.keys(zip.files).find((n) => /^ppt\/theme\/theme\d*\.xml$/.test(n));
  if (!f) throw new Error("This file has no theme (is it a .pptx?)");
  const xml = new DOMParser().parseFromString(await zip.file(f).async("string"), "application/xml"), q = (p, root = xml) => root.getElementsByTagNameNS("*", p);
  const col = (n) => { const e = q(n)[0]; if (!e) return null; const c = e.firstElementChild; return "#" + ((c.getAttribute("val") !== "windowText" && c.getAttribute("val") !== "window" ? c.getAttribute("val") : c.getAttribute("lastClr")) || "000000").toLowerCase().padStart(6, "0").slice(0, 6); };
  const font = (n) => { const e = q(n)[0]; const l = e && q("latin", e)[0]; const t = l && l.getAttribute("typeface"); return t ? `"${t}", system-ui, sans-serif` : null; };
  const t = { name: (xml.documentElement.getAttribute("name") || file.name.replace(/\.pptx$/i, "")).slice(0, 60), source: "pptx: " + file.name.slice(0, 60),
    colors: { bg: col("lt1") || NEW.colors.bg, fg: col("dk1") || NEW.colors.fg, muted: col("dk2") || NEW.colors.muted, accent: col("accent1") || NEW.colors.accent, accent2: col("accent2") || NEW.colors.accent2, surface: col("lt2") || NEW.colors.surface },
    fonts: { head: font("majorFont") || NEW.fonts.head, body: font("minorFont") || NEW.fonts.body }, notes: "" };
  const size = zip.file("ppt/presentation.xml") && /<p:sldSz[^>]*cx="(\d+)"[^>]*cy="(\d+)"/.exec(await zip.file("ppt/presentation.xml").async("string"));
  if (size) t.notes = "Imported slide size " + (size[1] / 914400).toFixed(2) + " x " + (size[2] / 914400).toFixed(2) + " in; slides here are 1280 x 720.";
  return t;
}

function ThemeForm({ t, close, saved, post }) {
  const [v, setV] = useState(t), set = (p) => setV({ ...v, ...p });
  const save = guard(async () => { const r = await post("themes/save", { theme: v }); saved(r.theme); });
  return html`<${Modal} close=${close} wide=${true}><h1>${t.id ? "Edit theme" : "New theme"}</h1><p class="sub">Slides and documents use these as CSS variables. Mention the theme in a chat as <code>@theme:name</code>.</p>
    <div class="field"><label>Name *</label><input value=${v.name} maxlength="60" onInput=${(e) => set({ name: e.target.value })}/></div>
    <div class="tcolors">${COLORS.map(([k, l]) => html`<label class="tc"><input type="color" value=${v.colors[k]} onInput=${(e) => set({ colors: { ...v.colors, [k]: e.target.value } })}/><span>${l}<code>${v.colors[k]}</code></span></label>`)}</div>
    <div class="fb-grid" style="padding:0;margin-top:10px"><label class="fb-f"><span>Heading font</span><input value=${v.fonts.head} onInput=${(e) => set({ fonts: { ...v.fonts, head: e.target.value } })}/></label>
      <label class="fb-f"><span>Body font</span><input value=${v.fonts.body} onInput=${(e) => set({ fonts: { ...v.fonts, body: e.target.value } })}/></label></div>
    <div class="field"><label>Notes for Claude (optional)</label><textarea rows="2" value=${v.notes} onInput=${(e) => set({ notes: e.target.value })} placeholder="Tone, logo placement, dos and don'ts"></textarea></div>
    <div class="tprev" style=${`background:${v.colors.bg};color:${v.colors.fg};font-family:${v.fonts.body}`}><b style=${`font-family:${v.fonts.head};font-size:22px;color:${v.colors.accent2}`}>${v.name || "Theme preview"}</b><div style=${`color:${v.colors.muted}`}>Body text sample in the body font</div>
      <span class="tchip" style=${`background:${v.colors.accent};color:${v.colors.bg}`}>Accent</span><span class="tchip" style=${`background:${v.colors.surface};color:${v.colors.fg}`}>Panel</span></div>
    <div class="row" style="margin-top:16px"><span class="grow"></span><button class="btn" onClick=${close}>Cancel</button><button class="btn acc" disabled=${!v.name.trim()} onClick=${save}>Save theme</button></div><//>`;
}

export function Themes({ api, post }) {
  const [list, setList] = useState([]), [edit, setEdit] = useState(null), pick = useRef();
  const load = guard(async () => setList((await api("themes")).themes));
  useEffect(() => { load(); }, []);
  const fromFile = guard(async (f) => { if (f) setEdit(await themeFromPptx(f)); });
  return html`<h2>Themes for slides and documents</h2><p class="sub">Colors and fonts Claude uses when you ask for a deck or document. Mention one in a chat with <code>@theme:name</code>.</p>
    ${list.map((t) => html`<div class="card row"><span class="swatch">${["bg", "surface", "accent", "accent2", "fg"].map((k) => html`<i style=${`background:${t.colors[k]}`}></i>`)}</span><div class="grow"><b>${t.name}</b> <code class="small">@theme:${t.id}</code><div class="mute small">${t.fonts.head.split(",")[0]} / ${t.fonts.body.split(",")[0]}${t.source ? " · from " + t.source : ""}</div></div>
      <button class="btn" onClick=${() => setEdit(t)}>Edit</button><button class="btn bad" onClick=${guard(async () => { if (await ask({ title: "Delete theme?", text: t.name, submit: "Delete", danger: true })) { await post("themes/delete", { id: t.id }); load(); } })}>${Ic("trash", 14)}</button></div>`)}
    ${!list.length && html`<div class="card mute">No themes yet.</div>`}
    <div class="row"><button class="btn acc" onClick=${() => setEdit({ ...NEW })}>${Ic("plus", 14)} New theme</button><button class="btn" onClick=${() => pick.current.click()}>${Ic("upload", 14)} Import from a PowerPoint (.pptx)</button>
      <input ref=${pick} type="file" accept=".pptx,.potx" style="display:none" onChange=${(e) => { fromFile(e.target.files[0]); e.target.value = ""; }}/></div>
    ${edit && html`<${ThemeForm} post=${post} t=${edit} close=${() => setEdit(null)} saved=${() => { setEdit(null); load(); toast("Theme saved"); }}/>`}`;
}
