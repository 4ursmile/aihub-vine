// Markdown answer with rich blocks rendered in place: shell sessions, code, tables, charts (```chart) and forms (```form).
import { html, render, useState, useEffect, useRef } from "https://cdn.jsdelivr.net/npm/htm@3.1.1/preact/standalone.module.js";
import { marked } from "https://cdn.jsdelivr.net/npm/marked@12.0.2/lib/marked.esm.js";
import DOMPurify from "https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.es.mjs";
import hljs from "https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/es/highlight.min.js";
import { Ic } from "./icons.js";
import { toast } from "./ui.js";
import { Diagram, DIAGRAMS } from "./diagram.js";

marked.setOptions({ gfm: true, breaks: false });
const SHELL = new Set(["sh", "bash", "zsh", "shell", "console", "terminal", "fish", "powershell", "ps1", "cmd"]);
const PALETTE = ["#d97757", "#6a9bcc", "#788c5d", "#c4a05a", "#9b87b8", "#5f8f8a", "#b0645a"];

const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const iconEl = (name) => { const t = document.createElement("template"); render(Ic(name, 14), t.content); return t.content.firstChild; };
function btn(icon, label, fn) {
  const b = el("button", "bk-btn"); b.type = "button"; b.append(iconEl(icon), el("span", "", label));
  b.onclick = () => fn(b); return b;
}
function copyBtn(get) {
  return btn("copy", "Copy", (b) => navigator.clipboard.writeText(get()).then(() => { const s = b.querySelector("span"); s.textContent = "Copied"; setTimeout(() => (s.textContent = "Copy"), 1400); }));
}
function head(icon, label, ...actions) {
  const h = el("div", "bk-head"); const l = el("span", "bk-title"); l.append(iconEl(icon), el("span", "", label));
  const a = el("span", "bk-acts"); a.append(...actions); h.append(l, a); return h;
}

// ---------- zoom (the block fills the window; Esc, the backdrop or Close puts it back) and share (the block as a page, to a Space)
let shareBlock = null;
export const setBlockSharer = (fn) => { shareBlock = fn; };
const nudge = () => requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));      // echarts / leaflet re-measure
function zoom(box) {
  if (box.closest(".bk-zoom")) { box.closest(".bk-zoom")._close(); return; }
  const ph = document.createComment("zoomed"), ov = el("div", "bk-zoom"); box.replaceWith(ph); ov.append(box); document.body.append(ov);
  const key = (e) => { if (e.key === "Escape") close(); };
  const close = () => { document.removeEventListener("keydown", key); if (ph.parentNode) ph.replaceWith(box); else box.remove(); ov.remove(); nudge(); };
  ov._close = close; ov.onclick = (e) => { if (e.target === ov) close(); }; document.addEventListener("keydown", key); nudge();
}
const PAGE_CSS = ":root{--surface:#fff;--line:#e4e2dc;--mute:#6b6a65}body{margin:0;padding:24px;font:14px system-ui,sans-serif;background:#fff;color:#1f1e1c}svg,img{max-width:100%;height:auto}.ct{font:11px system-ui;fill:#6b6a65}.cg{stroke:#e4e2dc}";
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
export const pageOf = (title, body) => `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${esc(title)}</title><style>${PAGE_CSS}</style></head><body><h1 style="font-size:20px">${esc(title)}</h1>${body}</body></html>`;
// what a drawn block looks like now, as a page: its SVG, or its canvas as an image (null when neither, e.g. a map)
function snapshot(node, title) {
  const svg = node.querySelector("svg"), cv = node.querySelector("canvas");
  if (svg && svg.getBoundingClientRect().width > 40) return pageOf(title, svg.outerHTML);
  if (cv) try { return pageOf(title, `<img alt="${esc(title)}" src="${cv.toDataURL("image/png")}">`); } catch (e) { /* tainted */ }
  return null;
}
const fname = (t) => (String(t || "block").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 50) || "block");
function zoomBtn(box) { return btn("expand", "Zoom", () => zoom(box)); }
function shareBtn(get) {          // get() -> {content, filename} or null
  return shareBlock && btn("upload", "Share", () => { const v = get(); if (v && v.content) shareBlock(v.content, v.filename); else toast("Nothing to share yet"); });
}
// a Preact-drawn block (chart, diagram) gets a small bar of its own: the component owns its inner node
function richBar(d, title, canShare) {
  const bar = el("div", "bk-float"); bar.append(zoomBtn(d));
  const s = canShare && shareBtn(() => { const c = snapshot(d, title); return c && { content: c, filename: fname(title) + ".html" }; });
  if (s) bar.append(s);
  d.append(bar);
}

function shellBlock(code) {
  const box = el("div", "bk term");
  const body = el("pre", "term-body"); const lines = code.replace(/\n$/, "").split("\n");
  lines.forEach((ln) => { const row = el("div", "tl"); const m = ln.match(/^(\s*)([$#>%])\s(.*)$/);
    if (m) { row.append(el("span", "tp", m[2]), el("span", "tc", m[3])); } else { row.className = "tl out"; row.textContent = ln || " "; } body.append(row); });
  box.append(head("terminal", "Shell", copyBtn(() => lines.map((l) => l.replace(/^\s*[$#>%]\s/, "")).filter((l, i) => /^\s*[$#>%]\s/.test(lines[i])).join("\n") || code)), body);
  return box;
}
function codeBlock(pre, code, lang, onRender) {          // replaces PRE by the framed block (PRE moves inside it)
  const box = el("div", "bk cb"); const acts = [];
  if (onRender && ["html", "svg", "openui"].includes(lang)) acts.push(btn("play", "Render", () => onRender({ lang, code: code.textContent })));
  acts.push(copyBtn(() => code.textContent));
  if (lang === "html" || lang === "svg") { const s = shareBtn(() => ({ content: lang === "svg" ? pageOf("Graphic", code.textContent) : code.textContent, filename: (lang === "svg" ? "graphic" : "page") + ".html" })); if (s) acts.push(s); }
  acts.push(zoomBtn(box));
  if (lang && hljs.getLanguage(lang)) try { hljs.highlightElement(code); } catch (e) { /* plain */ }
  if (code.textContent.length > 1200) box.classList.add("long");                    // a report or page: keep the chat short, scroll inside
  pre.replaceWith(box); box.append(head("code", lang || "code", ...acts), pre);
}
function tableBlock(t) {
  const box = el("div", "bk tb"); const rows = () => [...t.querySelectorAll("tr")].map((r) => [...r.children].map((c) => c.textContent));
  const csv = () => rows().map((r) => r.map((c) => '"' + c.replace(/"/g, '""') + '"').join(",")).join("\n");
  const n = t.querySelectorAll("tbody tr").length;
  const scroll = el("div", "tb-scroll"); t.replaceWith(box); scroll.append(t);
  box.append(head("table", n + (n === 1 ? " row" : " rows"), ...[copyBtn(csv), shareBtn(() => ({ content: csv(), filename: "table.csv" })), zoomBtn(box)].filter(Boolean)), scroll);
  t.querySelectorAll("th").forEach((th, i) => { th.classList.add("sortable"); let dir = 1; th.onclick = () => {
    const body = t.tBodies[0]; const arr = [...body.rows]; const val = (r) => r.cells[i].textContent.trim();
    arr.sort((a, b) => { const x = parseFloat(val(a).replace(/[^0-9.-]/g, "")), y = parseFloat(val(b).replace(/[^0-9.-]/g, "")); return (isNaN(x) || isNaN(y) ? val(a).localeCompare(val(b)) : x - y) * dir; });
    dir = -dir; arr.forEach((r) => body.append(r)); t.querySelectorAll("th").forEach((h) => h.removeAttribute("data-sort")); th.setAttribute("data-sort", dir === -1 ? "asc" : "desc"); }; });
}

const mdHtml = (t) => DOMPurify.sanitize(marked.parse(t || ""), { USE_PROFILES: { html: true } });
const unclosed = (t) => ((t || "").match(/^\s*```/gm) || []).length % 2 === 1;

export function Md({ text, live, onRender, onSend }) {
  // Incremental: the answer is split into top-level blocks; a block whose source did not change keeps its DOM node, so tables,
  // charts and cards that are already written never flash while the rest streams. Only the block being written is rebuilt.
  const ref = useRef(), pend = useRef(text), raf = useRef(0), blocks = useRef([]), prevLive = useRef(live);
  const build = (tok, links, isLast, t) => {
    const holder = el("div", "mdb"); holder.style.display = "contents"; const mounted = []; let rich = false;
    const part = [tok]; part.links = links;
    holder.innerHTML = DOMPurify.sanitize(marked.parser(part), { USE_PROFILES: { html: true } });
    holder.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    const open = live && isLast && unclosed(t); const pres = [...holder.querySelectorAll("pre")];
    pres.forEach((pre, idx) => {
      const code = pre.querySelector("code"); if (!code) return;
      if (open && idx === pres.length - 1) {                                // the fence still being written: a short box that follows the tail, so it never overflows the screen
        const lg = ((code.className.match(/language-(\S+)/) || [])[1] || "code").toLowerCase(), n = code.textContent.split("\n").length;
        const box = el("div", "bk cb writing"); pre.replaceWith(box); box.append(head("code", `Writing ${lg}… ${n} line${n === 1 ? "" : "s"}`), pre);
        requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; }); return;
      }
      const lang = ((code.className.match(/language-(\S+)/) || [])[1] || "").toLowerCase();
      if (lang === "workflow") {
        let spec; try { spec = JSON.parse(code.textContent); } catch (e) { return; }
        if (!spec || !Array.isArray(spec.nodes)) return;
        rich = true; const d = el("div", "bk rich"); pre.replaceWith(d); mounted.push(d); render(html`<${WorkCard} spec=${spec} locked=${live}/>`, d); return;
      }
      if (DIAGRAMS[lang]) {                                                   // mermaid / dot / echarts / map: drawn from code by a CDN library
        rich = true; const d = el("div", "bk rich"), m = el("div"); d.append(m); pre.replaceWith(d); mounted.push(m); render(html`<${Diagram} lang=${lang} code=${code.textContent}/>`, m);
        if (!live) richBar(d, lang === "map" ? "Map" : "Diagram", lang !== "map"); return;
      }
      if (lang === "chart" || lang === "form" || lang === "ask") {
        let spec; try { spec = JSON.parse(code.textContent); } catch (e) { return; }
        rich = true; const d = el("div", "bk rich"), m = el("div"); d.append(m); pre.replaceWith(d); mounted.push(m);
        if (lang === "chart" && !live) richBar(d, spec.title || "Chart", true);
        render(lang === "chart" ? html`<${Chart} spec=${spec}/>` : lang === "ask" ? html`<${AskCard} spec=${spec} onSend=${onSend} locked=${live || !onSend}/>` : html`<${FormBlock} spec=${spec} onSend=${onSend} disabled=${live}/>`, m); return;
      }
      if (lang === "svg") rich = true;
      if (lang === "svg" && !live) {                                      // a graphic is shown in the answer itself, no side canvas
        const clean = DOMPurify.sanitize(code.textContent, { USE_PROFILES: { svg: true, svgFilters: true } });
        if (clean.includes("<svg")) { const box = el("div", "bk svgbox"); box.append(head("chart", "Graphic", ...[copyBtn(() => code.textContent), shareBtn(() => ({ content: pageOf("Graphic", clean), filename: "graphic.html" })), zoomBtn(box)].filter(Boolean)));
          const v = el("div", "svgv"); v.innerHTML = clean; box.append(v); pre.replaceWith(box); return; }
      }
      if (SHELL.has(lang)) pre.replaceWith(shellBlock(code.textContent)); else codeBlock(pre, code, lang, onRender);
    });
    holder.querySelectorAll("table").forEach((t2) => { if (!t2.closest(".bk")) tableBlock(t2); });
    return { node: holder, mounted, rich };
  };
  const drop = (b) => { b.mounted.forEach((d) => render(null, d)); b.node.remove(); };
  const paint = () => {
    raf.current = 0; const root = ref.current; if (!root) return; const t = pend.current || "";
    const all = marked.lexer(t), toks = all.filter((k) => k.type !== "space"), list = blocks.current;
    const liveChanged = prevLive.current !== live; prevLive.current = live;
    root.querySelectorAll(".caret").forEach((c) => c.remove());
    toks.forEach((tok, i) => {
      const isLast = i === toks.length - 1, old = list[i];
      if (old && old.raw === tok.raw && !(isLast && live) && !(liveChanged && old.rich)) return;
      const nb = build(tok, all.links, isLast, t); nb.raw = tok.raw;
      if (old) { old.mounted.forEach((d) => render(null, d)); old.node.replaceWith(nb.node); } else root.append(nb.node);
      list[i] = nb;
    });
    while (list.length > toks.length) drop(list.pop());
    if (live) { const last = root.lastElementChild && root.lastElementChild.lastElementChild; const c = el("span", "caret");
      (last && !last.classList.contains("bk") ? last : root.lastElementChild || root).append(c); }
  };
  useEffect(() => { pend.current = text; if (!raf.current) raf.current = requestAnimationFrame(paint); }, [text, live]);
  useEffect(() => () => { cancelAnimationFrame(raf.current); blocks.current.forEach((b) => b.mounted.forEach((d) => render(null, d))); }, []);
  return html`<div class="md" ref=${ref}></div>`;
}

// ---------- chart (bar / line / pie) as plain SVG
const nice = (max) => { if (max <= 0) return 1; const p = Math.pow(10, Math.floor(Math.log10(max))); const f = max / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; };
const fmt = (n) => (Math.abs(n) >= 1e6 ? (n / 1e6).toFixed(1) + "M" : Math.abs(n) >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(Math.round(n * 100) / 100));
export function Chart({ spec }) {
  const type = ["bar", "line", "pie"].includes(spec.type) ? spec.type : "bar";
  const labels = (spec.labels || []).map(String).slice(0, 60);
  const series = (spec.series || []).slice(0, 7).map((s, i) => ({ name: String(s.name || "Series " + (i + 1)), data: (s.data || []).map(Number).map((v) => (isFinite(v) ? v : 0)).slice(0, 60) }));
  if (!series.length) return html`<div class="bk-empty">No data</div>`;
  const W = 640, H = 280, L = 44, R = 12, T = 12, B = 34;
  const n = Math.max(labels.length, ...series.map((s) => s.data.length));
  const max = nice(Math.max(...series.flatMap((s) => s.data), 0)), min = Math.min(0, ...series.flatMap((s) => s.data));
  const y = (v) => T + (H - T - B) * (1 - (v - min) / ((max - min) || 1)), x = (i) => L + (W - L - R) * (n <= 1 ? 0.5 : (i + 0.5) / n);
  const ticks = [0, 1, 2, 3, 4].map((i) => min + ((max - min) * i) / 4);
  let body;
  if (type === "pie") {
    const data = series[0].data, tot = data.reduce((a, b) => a + Math.max(b, 0), 0) || 1; let a0 = -Math.PI / 2; const cx = 150, cy = 140, r = 105;
    body = html`<g>${data.map((v, i) => { const a1 = a0 + (Math.max(v, 0) / tot) * Math.PI * 2, big = a1 - a0 > Math.PI ? 1 : 0;
      const d = `M${cx},${cy} L${cx + r * Math.cos(a0)},${cy + r * Math.sin(a0)} A${r},${r} 0 ${big} 1 ${cx + r * Math.cos(a1)},${cy + r * Math.sin(a1)} Z`; a0 = a1;
      return html`<path d=${d} fill=${PALETTE[i % 7]} stroke="var(--surface)" stroke-width="2"><title>${labels[i] || i}: ${fmt(v)} (${Math.round((v / tot) * 100)}%)</title></path>`; })}</g>
      <g>${data.slice(0, 12).map((v, i) => html`<g transform=${`translate(320,${30 + i * 20})`}><rect width="10" height="10" rx="2" fill=${PALETTE[i % 7]}/><text x="16" y="9" class="ct">${(labels[i] || i) + "  " + Math.round((v / tot) * 100) + "%"}</text></g>`)}</g>`;
  } else {
    const bw = Math.min(28, ((W - L - R) / n) / (series.length + 0.6));
    body = html`<g>${ticks.map((t) => html`<g><line x1=${L} x2=${W - R} y1=${y(t)} y2=${y(t)} class="cg"/><text x=${L - 6} y=${y(t) + 4} text-anchor="end" class="ct">${fmt(t)}</text></g>`)}
      ${Array.from({ length: n }, (_, i) => html`<text x=${x(i)} y=${H - 14} text-anchor="middle" class="ct">${(labels[i] || i + 1).slice(0, 10)}</text>`)}
      ${type === "bar" ? series.map((s, si) => s.data.map((v, i) => html`<rect x=${x(i) - (bw * series.length) / 2 + si * bw} y=${Math.min(y(v), y(0))} width=${bw - 2} height=${Math.abs(y(v) - y(0))} rx="3" fill=${PALETTE[si % 7]} class="bar"><title>${s.name} · ${labels[i] || i + 1}: ${fmt(v)}</title></rect>`))
        : series.map((s, si) => html`<g><path d=${s.data.map((v, i) => (i ? "L" : "M") + x(i) + "," + y(v)).join(" ")} fill="none" stroke=${PALETTE[si % 7]} stroke-width="2.2" stroke-linejoin="round" class="ln"/>${s.data.map((v, i) => html`<circle cx=${x(i)} cy=${y(v)} r="3.4" fill=${PALETTE[si % 7]}><title>${s.name} · ${labels[i] || i + 1}: ${fmt(v)}</title></circle>`)}</g>`)}</g>`;
  }
  return html`<div class="chart"><div class="bk-head"><span class="bk-title">${Ic("chart", 14)}<span>${spec.title ? String(spec.title).slice(0, 80) : "Chart"}</span></span></div>
    <svg viewBox=${`0 0 ${W} ${H}`} role="img" aria-label=${spec.title || "chart"}>${body}</svg>
    ${type !== "pie" && series.length > 1 && html`<div class="legend">${series.map((s, i) => html`<span><i style=${`background:${PALETTE[i % 7]}`}></i>${s.name}</span>`)}</div>`}</div>`;
}

// ---------- form: the answer asks, the submit sends the values back as the next message
export function FormBlock({ spec, onSend, disabled }) {
  const fields = (spec.fields || []).slice(0, 20).filter((f) => f && /^[\w.-]{1,40}$/.test(f.name || ""));
  const [v, setV] = useState(() => Object.fromEntries(fields.map((f) => [f.name, f.default ?? (f.type === "checkbox" ? false : "")]))), [done, setDone] = useState(false);
  const ok = fields.every((f) => !f.required || (f.type === "checkbox" ? true : String(v[f.name] ?? "").trim() !== ""));
  const submit = (e) => { e.preventDefault(); if (!ok || done || !onSend) return; setDone(true); onSend("Form " + (spec.title ? "“" + spec.title + "” " : "") + "response:\n```json\n" + JSON.stringify(v, null, 2) + "\n```"); };
  return html`<form class="fb" onSubmit=${submit}><div class="bk-head"><span class="bk-title">${Ic("form", 14)}<span>${spec.title || "Form"}</span></span>${done && html`<span class="tag good">${Ic("check", 12)} sent</span>`}</div>
    ${spec.description && html`<p class="mute small fb-d">${spec.description}</p>`}
    <div class="fb-grid">${fields.map((f) => { const set = (x) => setV((c) => ({ ...c, [f.name]: x })); const dis = done || disabled;
      return html`<label class=${"fb-f" + (f.type === "textarea" ? " wide" : "")}><span>${f.label || f.name}${f.required ? " *" : ""}</span>
        ${f.type === "textarea" ? html`<textarea rows="3" value=${v[f.name]} disabled=${dis} onInput=${(e) => set(e.target.value)}></textarea>`
        : f.type === "select" ? html`<select value=${v[f.name]} disabled=${dis} onChange=${(e) => set(e.target.value)}><option value="">Choose…</option>${(f.options || []).map((o) => html`<option>${String(o)}</option>`)}</select>`
        : f.type === "checkbox" ? html`<input type="checkbox" checked=${!!v[f.name]} disabled=${dis} onChange=${(e) => set(e.target.checked)}/>`
        : html`<input type=${f.type === "number" ? "number" : "text"} value=${v[f.name]} disabled=${dis} onInput=${(e) => set(f.type === "number" ? e.target.value : e.target.value)}/>`}</label>`; })}</div>
    <div class="fb-foot"><button class="btn pri" type="submit" disabled=${!ok || done || disabled}>${spec.submit || "Send"}</button></div></form>`;
}

// ---------- questions: the same card for Claude's AskUserQuestion tool and for a ```ask block. Submit sends the answers as the next message.
export const answersText = (qs, pick) => "My answers:\n" + qs.map((q, i) => "- " + (q.question || q.header || "Question " + (i + 1)) + " -> " + (pick[i] && pick[i].length ? pick[i].join("; ") : "(no answer)")).join("\n");
export function AskCard({ spec, onSend, locked }) {
  const qs = (spec.questions || []).slice(0, 6).filter((q) => q && q.question), [pick, setPick] = useState(() => qs.map(() => [])), [other, setOther] = useState(() => qs.map(() => "")), [sent, setSent] = useState(false), [step, setStep] = useState(0);
  if (!qs.length) return null;
  const choose = (qi, label) => setPick((p) => p.map((a, i) => i !== qi ? a : qs[qi].multiSelect ? (a.includes(label) ? a.filter((x) => x !== label) : [...a, label]) : [label]));
  const final = qs.map((q, i) => [...pick[i], ...(other[i].trim() ? [other[i].trim()] : [])]);
  const ready = final.every((a) => a.length), dis = sent || locked || !onSend;
  const advance = (i) => { if (!q_multi(i) && i < qs.length - 1) setTimeout(() => setStep((s) => Math.max(s, i + 1)), 160); };
  const q_multi = (i) => qs[i].multiSelect;
  const go = () => { if (!ready || dis) return; setSent(true); onSend(answersText(qs, final)); };
  return html`<div class=${"ask" + (sent ? " sent" : "")}><div class="bk-head"><span class="bk-title">${Ic("chat", 14)}<span>${qs.length > 1 ? "A few questions" : "A question"}</span></span>${sent && html`<span class="tag good">${Ic("check", 12)} answered</span>`}</div>
    ${qs.map((q, qi) => html`<section class=${"aq" + (qi > step && !sent ? " later" : "")} style=${`--i:${qi}`}>
      <div class="aq-h">${q.header && html`<span class="chip2">${String(q.header).slice(0, 24)}</span>`}<b>${q.question}</b>${q.multiSelect && html`<span class="mute small">pick any</span>`}</div>
      <div class="aopts">${(q.options || []).slice(0, 8).map((o) => { const label = String(o.label || o), on = pick[qi].includes(label);
        return html`<button type="button" class=${"aopt" + (on ? " on" : "")} disabled=${dis} aria-pressed=${on} onClick=${() => { choose(qi, label); advance(qi); }}>
          <span class=${"abox" + (q.multiSelect ? " sq" : "")}>${on && Ic("check", 12)}</span><span><b>${label}</b>${o.description && html`<span class="mute small adesc">${o.description}</span>`}</span></button>`; })}
        <label class="aother"><span class="mute small">Something else</span><input type="text" placeholder="Type your own answer" disabled=${dis} value=${other[qi]} onInput=${(e) => setOther((o) => o.map((x, i) => i === qi ? e.target.value : x))}/></label></div></section>`)}
    <div class="fb-foot"><span class="mute small grow">${final.filter((a) => a.length).length} of ${qs.length} answered</span><button class="btn pri" disabled=${!ready || dis} onClick=${go}>${sent ? "Sent" : "Send answers"}</button></div></div>`;
}


// ---------- workflow: an answer proposes a work; the button saves it (switched off) in Works and opens it
let makeWork = null;
export const setWorkMaker = (fn) => { makeWork = fn; };
export function WorkCard({ spec, locked }) {
  const [state, setState] = useState("");
  const go = async () => { setState("busy"); try { const w = await makeWork(spec); setState("done"); location.hash = "#/works/" + w.id; } catch (e) { setState(""); toast(e.message || String(e)); } };
  const types = [...new Set(spec.nodes.map((n) => n.type))];
  return html`<div class="ask"><div class="bk-head"><span class="bk-title">${Ic("flow", 14)}<span>Workflow: ${spec.title || "Untitled"}</span></span>${state === "done" && html`<span class="tag good">${Ic("check", 12)} created</span>`}</div>
    <p class="small" style="margin:6px 0">${spec.description || ""}</p><p class="mute small">${spec.nodes.length} nodes: ${types.join(", ")}</p>
    <button class="btn acc" disabled=${locked || state === "busy" || state === "done"} onClick=${go}>${Ic("plus", 14)} Create in Works</button></div>`;
}
