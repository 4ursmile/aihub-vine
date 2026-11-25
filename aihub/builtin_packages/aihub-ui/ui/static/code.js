// Code view for the canvas Edit and Diff tabs: syntax colours (highlight.js, lazy, same vendored copy as chat code blocks),
// an editor with find/replace and its own undo/redo history. The editor is a transparent textarea over coloured layers.
import { html, useState, useEffect, useLayoutEffect, useRef, useMemo } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast } from "./ui.js";

let hlLib = null, hlLoad = null;
export const loadHl = () => (hlLoad ||= import("./vendor/highlight.js").then((m) => (hlLib = m.default)).catch(() => null));
export function useHl() {
  const [h, setH] = useState(hlLib);
  useEffect(() => { if (!h) loadHl().then((x) => x && setH(x)); }, []);
  return h;
}

const EXT = { mjs: "javascript", cjs: "javascript", jsx: "javascript", tsx: "typescript", mts: "typescript", cts: "typescript", vue: "xml", svelte: "xml", htm: "xml", svg: "xml", plist: "xml",
  toml: "ini", cfg: "ini", conf: "ini", properties: "ini", env: "bash", zsh: "bash", jsonl: "json", ndjson: "json", webmanifest: "json", h: "c", hpp: "cpp", cc: "cpp", hh: "cpp", mdx: "markdown",
  patch: "diff", kts: "kotlin", pyw: "python", ipynb: "json" };
const NAMES = { makefile: "makefile", gnumakefile: "makefile", dockerfile: "bash", ".bashrc": "bash", ".zshrc": "bash", ".profile": "bash", ".env": "bash", ".gitignore": "bash", ".editorconfig": "ini", ".npmrc": "ini" };
export function langOf(h, path) {
  if (!h) return null;
  const base = String(path || "").split(/[\\/]/).pop().toLowerCase(), dot = base.lastIndexOf(".");
  const l = NAMES[base] || (dot > 0 ? EXT[base.slice(dot + 1)] || base.slice(dot + 1) : null) || (base.startsWith(".env") ? "bash" : null);
  return l && l !== "txt" && h.getLanguage(l) ? l : null;
}
export const esc = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const MAX_HL = 1.5e6;
// highlighted HTML of the whole text (plain escaped text when there is no language or highlight.js is not loaded yet)
export function colorize(h, lang, text) {
  if (!h || !lang || text.length > MAX_HL) return esc(text);
  try { return h.highlight(text, { language: lang, ignoreIllegals: true }).value; } catch (e) { return esc(text); }
}
// split highlighted HTML into lines, closing the spans open at each line end and reopening them on the next line
export function splitHl(s) {
  const out = [], open = [], re = /<span[^>]*>|<\/span>|\n/g;
  let cur = "", last = 0, m;
  while ((m = re.exec(s))) {
    cur += s.slice(last, m.index); last = re.lastIndex;
    if (m[0] === "\n") { out.push(cur + "</span>".repeat(open.length)); cur = open.join(""); }
    else if (m[0][1] === "/") { open.pop(); cur += m[0]; }
    else { open.push(m[0]); cur += m[0]; }
  }
  out.push(cur + s.slice(last) + "</span>".repeat(open.length));
  return out;
}

// every match of the find box: [start, end] pairs (capped), the regex used, or the error of a bad pattern
const MAX_MATCH = 10000;
function findAll(text, q, cs, rx) {
  if (!q) return { m: [] };
  let re;
  try { re = new RegExp(rx ? q : q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gm" + (cs ? "" : "i")); } catch (e) { return { m: [], bad: e.message }; }
  const m = []; let x;
  while (m.length < MAX_MATCH && (x = re.exec(text))) { if (!x[0]) { re.lastIndex++; continue; } m.push([x.index, x.index + x[0].length]); }
  return { m, re };
}
function marksHtml(text, m, cur) {
  let s = "", at = 0;
  m.forEach(([a, b], i) => { s += esc(text.slice(at, a)) + `<mark class="fm${i === cur ? " cur" : ""}">` + esc(text.slice(a, b)) + "</mark>"; at = b; });
  return s + esc(text.slice(at));
}

const HIST_MAX = 300, HIST_CHARS = 3e7, MERGE_MS = 900, SYNC_HL = 60000, IND = "  ";
export const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const kindOf = (t) => !t ? "other" : t === "insertText" ? "type" : t.startsWith("delete") && !t.includes("Cut") ? "del" : "other";

// value/onChange = the text; `doc` changes when the file is (re)loaded from disk, which resets the history.
// `footer(tools)` renders the bar under the editor with { undo, redo, canUndo, canRedo, find }.
// `assist` (optional) = { complete(code, pos) -> {start, matches:[{t,k}]}, suggest(before, after) -> text, on (grey AI text), delay, onState(s, msg) }:
// a completion list (the file's names at once, the completer's answer a moment later; Ctrl+Space opens it) and AI ghost text at the end of a line.
const A_WORD = /[A-Za-z_][A-Za-z0-9_]*$/, A_IDENT = /[A-Za-z_][A-Za-z0-9_]{2,}/g, A_TRIG = /[A-Za-z0-9_.]$/, A_KIND = { function: "ƒ", method: "ƒ", class: "C", module: "M", instance: "v", statement: "v", keyword: "k", property: "p", path: "/" };
const sugLru = new Map();
let aMetrics = null;
export function CodeEditor({ value, onChange, doc, path, onSave, footer, assist }) {
  const h = useHl(), lang = useMemo(() => langOf(h, path), [h, path]);
  const ta = useRef(null), view = useRef(null), gut = useRef(null), pend = useRef(null), last = useRef(value), hist = useRef({ doc: {} });
  const [, setTick] = useState(0), bump = () => setTick((n) => n + 1);
  const H = hist.current;
  if (H.doc !== doc) { Object.assign(H, { doc, stack: [{ t: value, a: 0, b: 0 }], i: 0, kind: null, at: 0 }); last.current = value; }
  else if (value !== last.current) { record(value, "other", null, { a: value.length, b: value.length }, true); }

  function record(t, kind, pre, post, external) {
    if (t === H.stack[H.i].t) { last.current = t; return; }
    const now = Date.now(), merge = (kind === "type" || kind === "del") && H.kind === kind && now - H.at < MERGE_MS && H.i > 0;
    H.stack.length = H.i + 1;
    if (!merge && pre) Object.assign(H.stack[H.i], pre);                       // undo lands where the caret was before this edit
    const e = { t, ...(post || { a: ta.current ? ta.current.selectionEnd : 0, b: ta.current ? ta.current.selectionEnd : 0 }) };
    if (merge) H.stack[H.i] = e; else { H.stack.push(e); H.i++; }
    let size = H.stack.reduce((n, x) => n + x.t.length, 0);
    while (H.stack.length > 2 && (H.stack.length > HIST_MAX || size > HIST_CHARS)) { size -= H.stack.shift().t.length; H.i--; }
    H.kind = kind; H.at = now; last.current = t;
    if (!external) onChange(t);
  }
  const pre = useRef(null);
  const snapSel = () => { const t = ta.current; if (t) pre.current = { a: t.selectionStart, b: t.selectionEnd }; };
  const edit = (t, kind, post) => { record(t, kind, pre.current, post); if (post) pend.current = post; snapSel(); bump(); };
  const go = (d) => {
    const j = H.i + d; if (j < 0 || j >= H.stack.length) return;
    H.i = j; H.kind = null; const s = H.stack[j]; last.current = s.t; pend.current = { a: s.a, b: s.b, show: true }; onChange(s.t); bump();
  };
  const undo = () => go(-1), redo = () => go(1);

  // ---- completion list and AI ghost text
  const [pop, setPopS] = useState(null), [ghost, setGhostS] = useState({ at: 0, text: "" }), popR = useRef(null), ghR = useRef(ghost), atk = useRef(0), aT = useRef(0), cT = useRef(0);
  const setPop = (p) => { popR.current = p; setPopS(p); }, setGhost = (g) => { ghR.current = g; setGhostS(g); };
  useEffect(() => () => { clearTimeout(aT.current); clearTimeout(cT.current); }, []);
  useEffect(() => { setPop(null); setGhost({ at: 0, text: "" }); }, [doc]);
  const AS = assist || {};
  const acceptItem = (p, i) => { const t = ta.current; if (!t) return; const v = t.value, it = p.items[i], nv = v.slice(0, p.start) + it.t + v.slice(p.end), at = p.start + it.t.length; atk.current++; setPop(null); setGhost({ at: 0, text: "" }); edit(nv, "other", { a: at, b: at }); };
  const listAt = (v, at, now) => {
    const before = v.slice(0, at), m = A_WORD.exec(before), my = ++atk.current;
    let loc = null;
    if (m && before[before.length - m[0].length - 1] !== "." && v.length < 400000) {
      const w = m[0], out = [], seen = new Set();
      for (const x of v.match(A_IDENT) || []) if (x.length > w.length && x.startsWith(w) && !seen.has(x)) { seen.add(x); out.push({ t: x, k: "" }); if (out.length >= 30) break; }
      if (out.length) loc = { items: out, start: at - w.length, end: at, sel: 0 };
    }
    setPop(loc);
    clearTimeout(cT.current); if (!AS.complete) return;
    cT.current = setTimeout(async () => {
      let r; try { r = await AS.complete(v, at); } catch (e) { return; }
      const t = ta.current; if (atk.current !== my || !t || t.value !== v || t.selectionStart !== at) return;
      const ms = ((r && r.matches) || []).slice(0, 40);
      if (!ms.length) { if (!loc) setPop(null); return; }
      if (now && ms.length === 1) { acceptItem({ items: ms, start: r.start, end: at, sel: 0 }, 0); return; }
      setPop({ items: ms, start: r.start, end: at, sel: 0 });
    }, now ? 0 : 90);
  };
  const askAi = (v, at, wait, force) => {
    clearTimeout(aT.current); if (!AS.suggest || (!AS.on && !force) || v.length > SYNC_HL) return;
    const my = atk.current;
    aT.current = setTimeout(async () => {
      const t = ta.current; if (!t || t.value !== v || t.selectionStart !== at || document.activeElement !== t) return;
      const k = v.slice(Math.max(0, at - 800), at) + "\u0001" + v.slice(at, at + 120);
      let s = sugLru.get(k);
      if (s == null) { AS.onState && AS.onState("busy"); try { s = String((await AS.suggest(v.slice(0, at), v.slice(at))) || "").replace(/\s+$/, ""); } catch (e) { AS.onState && AS.onState("err", e.message); return; } AS.onState && AS.onState(s ? "ok" : "none"); sugLru.set(k, s); if (sugLru.size > 80) sugLru.delete(sugLru.keys().next().value); }
      if (atk.current === my && s && !popR.current) setGhost({ at, text: s });
    }, wait);
  };
  const afterInput = (v, at, kind) => {
    if (!assist) return;
    const g = ghR.current, typed = at - g.at;
    if (g.text) { if (kind === "insertText" && typed > 0 && v.slice(g.at, at) === g.text.slice(0, typed)) { setGhost({ at, text: g.text.slice(typed) }); if (g.text.length > typed) return; } else setGhost({ at: 0, text: "" }); }
    const ls = v.lastIndexOf("\n", at - 1) + 1, le = v.indexOf("\n", at), line = v.slice(ls, at), rest = v.slice(at, le < 0 ? v.length : le);
    const ins = (kind || "").startsWith("insert"), tailOk = /^[\s)\]}'"`,:;]*$/.test(rest), comment = /^\s*(#|\/\/|--)/.test(line);
    if (ins && A_TRIG.test(line) && !comment && !rest.trim()) listAt(v, at, false); else if (popR.current) setPop(null);
    if (!ins || !tailOk || v.slice(0, at).trim().length < 2 || (comment && kind !== "insertLineBreak")) return;
    if (!line.trim()) { const prev = v.slice(0, ls).replace(/\n+$/, "").split("\n").pop() || ""; if (!/[:{(\[]\s*$/.test(prev) && !/^\s*(#|\/\/)/.test(prev)) return; }
    askAi(v, at, Math.max(350, (AS.delay || 900) - 300));
  };
  const acceptGhost = (part) => {
    const g = ghR.current, t = ta.current; if (!g.text || !t) return;
    let x = g.text; if (part) { const m = /^\s*\S+/.exec(x); if (m && m[0].length < x.length) x = m[0]; }
    const v = t.value, nv = v.slice(0, g.at) + x + v.slice(g.at), at = g.at + x.length;
    atk.current++; setGhost({ at, text: part ? g.text.slice(x.length) : "" }); edit(nv, "other", { a: at, b: at });
  };
  const assistKey = (e) => {                                                  // true = handled
    if (!assist) return false;
    const p = popR.current, g = ghR.current, t = e.target, plain = !e.metaKey && !e.ctrlKey && !e.altKey;
    if (p) {
      if ((e.key === "ArrowDown" || e.key === "ArrowUp") && plain) { e.preventDefault(); const n = p.items.length; setPop({ ...p, sel: (p.sel + (e.key === "ArrowDown" ? 1 : n - 1)) % n }); return true; }
      if (((e.key === "Tab" && !e.shiftKey) || (e.key === "Enter" && !e.shiftKey)) && plain) { e.preventDefault(); acceptItem(p, p.sel); return true; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setPop(null); return true; }
    }
    if (g.text) {
      if (e.key === "Tab" && !e.shiftKey && plain) { e.preventDefault(); acceptGhost(false); return true; }
      if (e.key === "ArrowRight" && e.altKey && t.selectionStart === g.at) { e.preventDefault(); acceptGhost(true); return true; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setGhost({ at: 0, text: "" }); return true; }
    }
    if (e.ctrlKey && (e.key === " " || e.code === "Space")) { e.preventDefault(); listAt(t.value, t.selectionStart, true); return true; }
    if (e.altKey && (e.key === "\\" || e.code === "Backslash")) { e.preventDefault(); askAi(t.value, t.selectionStart, 0, true); return true; }
    return false;
  };
  const caretMoved = () => { const t = ta.current; if (!t) return; if (ghR.current.text && t.selectionStart !== ghR.current.at) setGhost({ at: 0, text: "" }); const p = popR.current; if (p && (t.selectionStart !== p.end || t.selectionEnd !== p.end)) setPop(null); };

  // find / replace
  const [fo, setFo] = useState(false), [rep, setRep] = useState(false), [q, setQ] = useState(""), [r, setR] = useState(""), [cs, setCs] = useState(false), [rx, setRx] = useState(false), [cur, setCur] = useState(0);
  const qIn = useRef(null), anchor = useRef(null), jump = useRef(false);
  const found = useMemo(() => fo ? findAll(value, q, cs, rx) : { m: [] }, [fo, value, q, cs, rx]);
  const M = found.m;
  useEffect(() => { if (fo) { anchor.current = ta.current ? ta.current.selectionStart : 0; jump.current = true; } }, [q, cs, rx, fo]);
  useEffect(() => {
    if (anchor.current !== null) { const k = M.findIndex(([a]) => a >= anchor.current); anchor.current = null; setCur(k < 0 ? 0 : k); }
    else if (cur >= M.length) setCur(Math.max(0, M.length - 1));
    if (jump.current) bump();                                                // reveal the match once `cur` is settled
  }, [M]);
  const openFind = (withRep) => {
    const t = ta.current, sel = t ? value.slice(t.selectionStart, t.selectionEnd) : "";
    if (sel && !sel.includes("\n") && sel.length < 200) setQ(rx ? sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") : sel);
    setFo(true); if (withRep) setRep(true);
    requestAnimationFrame(() => { qIn.current && (qIn.current.focus(), qIn.current.select()); });
  };
  const closeFind = () => {
    setFo(false); const t = ta.current, x = M[cur];
    if (t) { t.focus(); if (x) t.setSelectionRange(x[0], x[1]); }
  };
  const step = (d) => { if (!M.length) return; jump.current = true; setCur((c) => (c + d + M.length) % M.length); };
  const replaceOne = () => {
    const x = M[cur]; if (!x) return;
    const piece = value.slice(x[0], x[1]), by = rx ? piece.replace(new RegExp(found.re.source, found.re.flags.replace("g", "")), r) : r;
    anchor.current = x[0] + by.length; jump.current = true;
    edit(value.slice(0, x[0]) + by + value.slice(x[1]), "other", { a: x[0], b: x[0] + by.length });
  };
  const replaceAll = () => {
    if (!M.length || !found.re) return;
    const n = M.length, t = value.replace(found.re, rx ? r : () => r);
    edit(t, "other"); toast(`Replaced ${n}${n >= MAX_MATCH ? "+" : ""} match${n > 1 ? "es" : ""}`);
  };

  // colours: synchronous up to SYNC_HL chars, larger files are coloured shortly after typing stops (plain text meanwhile)
  const [slow, setSlow] = useState({ t: null, html: "" });
  const big = value.length > SYNC_HL;
  useEffect(() => {
    if (!big || !lang) return;
    const id = setTimeout(() => setSlow({ t: value, html: colorize(h, lang, value) }), 250); return () => clearTimeout(id);
  }, [value, big, lang, h]);
  const hlHtml = useMemo(() => {
    if (ghost.text && !big && ghost.at <= value.length) return colorize(h, lang, value.slice(0, ghost.at)) + '<span class="nbj-ghost">' + esc(ghost.text) + "</span>" + colorize(h, lang, value.slice(ghost.at));
    return !big ? colorize(h, lang, value) : slow.t === value ? slow.html : esc(value);
  }, [value, big, lang, h, slow, ghost]);
  const lines = useMemo(() => { let n = 1; for (let i = value.indexOf("\n"); i >= 0; i = value.indexOf("\n", i + 1)) n++; return n; }, [value]);
  const nums = useMemo(() => Array.from({ length: lines }, (_, i) => i + 1).join("\n"), [lines]);

  const sync = () => {
    const t = ta.current; if (!t || !view.current) return;
    const tr = `translate(${-t.scrollLeft}px,${-t.scrollTop}px)`;
    view.current.querySelectorAll(".ce-in").forEach((e) => { e.style.transform = tr; });
    if (gut.current) gut.current.style.transform = `translateY(${-t.scrollTop}px)`;
  };
  const reveal = (top, left, hgt) => {
    const t = ta.current; if (!t) return;
    if (top < t.scrollTop + 8 || top + hgt > t.scrollTop + t.clientHeight - 8) t.scrollTop = Math.max(0, top - t.clientHeight / 2);
    if (left < t.scrollLeft || left > t.scrollLeft + t.clientWidth - 40) t.scrollLeft = Math.max(0, left - t.clientWidth / 3);
    sync();
  };
  useLayoutEffect(() => {
    const t = ta.current, p = pend.current;
    if (t && p) {
      pend.current = null; t.setSelectionRange(p.a, p.b);
      if (p.show) { const ln = value.slice(0, p.a).split("\n").length - 1, lh = parseFloat(getComputedStyle(t).lineHeight) || 19; reveal(ln * lh, 0, lh); }
    }
    if (fo && jump.current && anchor.current === null && view.current) {
      const m = view.current.querySelector(".fm.cur");
      if (m) { jump.current = false; reveal(m.offsetTop, m.offsetLeft, m.offsetHeight); }
    }
    sync();
  });

  const lineStart = (t, i) => t.lastIndexOf("\n", i - 1) + 1;
  const indent = (t, out) => {
    const s = t.selectionStart, e = t.selectionEnd, v = t.value;
    if (!out && s === e) { t.setRangeText(IND, s, e, "end"); return edit(t.value, "other"); }
    const a = lineStart(v, s), b = e > s && v[e - 1] === "\n" ? e - 1 : e, block = v.slice(a, b), ls = block.split("\n");
    const nl = ls.map((l) => out ? l.replace(/^( {1,2}|\t)/, "") : IND + l), first = nl[0].length - ls[0].length;
    const nb = nl.join("\n"); t.setRangeText(nb, a, b);
    edit(t.value, "other", { a: Math.max(a, s + first), b: Math.max(a, e + nb.length - block.length) });
  };
  const onKey = (e) => {
    const t = e.target, mod = e.metaKey || e.ctrlKey, k = e.key.toLowerCase();
    snapSel();
    if (assistKey(e)) return;
    if (mod && k === "s") { e.preventDefault(); onSave && onSave(); }
    else if (mod && k === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    else if (mod && !isMac && k === "y") { e.preventDefault(); redo(); }
    else if (mod && k === "f") { e.preventDefault(); openFind(e.altKey); }
    else if ((e.ctrlKey && !isMac && k === "h") || (mod && e.altKey && k === "f")) { e.preventDefault(); openFind(true); }
    else if ((k === "f3" || (mod && k === "g")) && q) { e.preventDefault(); if (!fo) setFo(true); step(e.shiftKey ? -1 : 1); }
    else if (e.key === "Escape" && fo) { e.preventDefault(); e.stopPropagation(); setFo(false); }
    else if (e.key === "Tab" && !mod && !e.altKey) { e.preventDefault(); indent(t, e.shiftKey); }
    else if (e.key === "Enter" && !mod && !e.altKey && !e.shiftKey && !e.isComposing) {
      const ws = (t.value.slice(lineStart(t.value, t.selectionStart), t.selectionStart).match(/^[ \t]*/) || [""])[0];
      if (ws) { e.preventDefault(); t.setRangeText("\n" + ws, t.selectionStart, t.selectionEnd, "end"); edit(t.value, "other"); }
    }
    else if (/^(arrow|home|end|page)/.test(k)) H.kind = null;                 // moving the caret starts a new undo step
  };
  const fKey = (e) => {
    const mod = e.metaKey || e.ctrlKey, k = e.key.toLowerCase();
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeFind(); }
    else if (e.key === "Enter" && e.target === qIn.current) { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
    else if (e.key === "Enter") { e.preventDefault(); mod ? replaceAll() : replaceOne(); }
    else if (mod && k === "f") { e.preventDefault(); openFind(e.altKey); }
    else if (mod && k === "z") { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    else if (mod && k === "s") { e.preventDefault(); onSave && onSave(); }
  };
  const tog = (on, set, label, txt) => html`<button type="button" class=${"ce-tog" + (on ? " on" : "")} aria-pressed=${on} title=${label} onClick=${() => set(!on)}>${txt}</button>`;
  const count = !q ? "" : found.bad ? "Bad pattern" : M.length ? `${Math.min(cur + 1, M.length)} of ${M.length}${M.length >= MAX_MATCH ? "+" : ""}` : "No results";
  const tools = { undo, redo, canUndo: H.i > 0, canRedo: H.i < H.stack.length - 1, find: () => (fo ? closeFind() : openFind(false)), lang, finding: fo, ghost: !!ghost.text };

  return html`<div class="ce">
    ${fo && html`<div class="ce-find" onKeyDown=${fKey}>
      <button type="button" class="ibtn ce-rt" title=${rep ? "Hide replace" : "Show replace"} aria-label="Toggle replace" onClick=${() => setRep(!rep)}>${Ic(rep ? "down" : "right", 13)}</button>
      <div class="ce-fields">
        <div class="ce-row"><div class=${"ce-inp" + (found.bad ? " bad" : "")}>${Ic("search", 13)}<input ref=${qIn} placeholder="Find" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Find" spellcheck="false"/>
            ${tog(cs, setCs, "Match case", "Aa")}${tog(rx, setRx, "Regular expression", ".*")}</div>
          <span class=${"ce-cnt small" + (q && !M.length ? " none" : "")} title=${found.bad || ""}>${count}</span>
          <button type="button" class="ibtn" title="Previous (Shift+Enter)" aria-label="Previous match" disabled=${!M.length} onClick=${() => step(-1)}>${Ic("up", 14)}</button>
          <button type="button" class="ibtn" title="Next (Enter)" aria-label="Next match" disabled=${!M.length} onClick=${() => step(1)}>${Ic("down", 14)}</button>
          <button type="button" class="ibtn" title="Close (Esc)" aria-label="Close find" onClick=${closeFind}>${Ic("x", 14)}</button></div>
        ${rep && html`<div class="ce-row"><div class="ce-inp"><input placeholder=${rx ? "Replace ($1 = group 1)" : "Replace"} value=${r} onInput=${(e) => setR(e.target.value)} aria-label="Replace" spellcheck="false"/></div>
          <button type="button" class="btn sm" disabled=${!M.length} title="Replace this match (Enter)" onClick=${replaceOne}>Replace</button>
          <button type="button" class="btn sm" disabled=${!M.length} title=${"Replace all (" + (isMac ? "Cmd" : "Ctrl") + "+Enter)"} onClick=${replaceAll}>All</button></div>`}
      </div></div>`}
    <div class="ce-box">
      <div class="ce-gut" aria-hidden="true" style=${"width:calc(" + String(lines).length + "ch + 22px)"}><pre ref=${gut}>${nums}</pre></div>
      <div class="ce-view" ref=${view}>
        ${fo && M.length > 0 && html`<pre class="ce-l ce-marks" aria-hidden="true"><div class="ce-in" dangerouslySetInnerHTML=${{ __html: marksHtml(value, M, cur) + "\n" }}></div></pre>`}
        <pre class="ce-l syn" aria-hidden="true"><div class="ce-in" dangerouslySetInnerHTML=${{ __html: hlHtml + "\n" }}></div></pre>
        <textarea ref=${ta} class="ce-ta" spellcheck="false" wrap="off" autocapitalize="off" autocomplete="off" value=${value}
          onScroll=${() => { sync(); if (popR.current) setPop(null); }} onKeyDown=${onKey} onKeyUp=${caretMoved} onPointerDown=${() => { H.kind = null; }} onClick=${caretMoved} onBlur=${() => { if (popR.current) setPop(null); if (ghR.current.text) setGhost({ at: 0, text: "" }); }} onBeforeInput=${snapSel}
          onInput=${(e) => { edit(e.target.value, kindOf(e.inputType)); afterInput(e.target.value, e.target.selectionStart, e.inputType || ""); }}></textarea>
        ${pop && ta.current && (() => {                                 // below the caret (above it near the bottom of the view)
          const t = ta.current, cs = getComputedStyle(t);
          if (!aMetrics || aMetrics.font !== cs.font) { const c = document.createElement("canvas").getContext("2d"); c.font = cs.font; aMetrics = { font: cs.font, cw: c.measureText("MMMMMMMMMM").width / 10, lh: parseFloat(cs.lineHeight) || 19 }; }
          const ls = value.lastIndexOf("\n", pop.start - 1) + 1, row = value.slice(0, ls).split("\n").length - 1, col = pop.start - ls;
          const top = (parseFloat(cs.paddingTop) || 0) + (row + 1) * aMetrics.lh - t.scrollTop, left = Math.max(0, Math.min((parseFloat(cs.paddingLeft) || 0) + col * aMetrics.cw - t.scrollLeft, t.clientWidth - 260)), up = top + 230 > t.clientHeight && top - aMetrics.lh > 230;
          return html`<div class="nbj-pop" role="listbox" style=${up ? { left: left + "px", bottom: t.clientHeight - top + aMetrics.lh + "px" } : { left: left + "px", top: top + "px" }} onMouseDown=${(e) => e.preventDefault()}>
            ${pop.items.map((it, i) => html`<div key=${it.t} role="option" aria-selected=${i === pop.sel} class=${"nbj-opt" + (i === pop.sel ? " on" : "")} ref=${(el) => { if (el && i === pop.sel) el.scrollIntoView({ block: "nearest" }); }} onClick=${() => { acceptItem(pop, i); t.focus(); }}>
              <span class=${"nbj-k k-" + (it.k || "x")}>${A_KIND[it.k] || "·"}</span><span class="nbj-t">${it.t}</span></div>`)}</div>`; })()}
      </div>
    </div>
    ${footer && footer(tools)}</div>`;
}
