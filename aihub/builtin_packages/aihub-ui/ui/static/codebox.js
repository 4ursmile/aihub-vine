// The code box shared by Notebook cells and SQL Studio: coloured text under a transparent textarea, a completion list and AI ghost text.
import { html, useState, useEffect, useRef, useMemo } from "./vendor/preact-htm.js";
import { useHl, colorize, esc } from "./code.js";

// ---------- the code box: coloured text under a transparent textarea, with code intelligence in two layers:
// 1) a completion list as you type (like Jupyter/Kaggle): names from this notebook appear at once, and the live kernel's answer (DataFrame columns,
//    module members, methods) replaces them a moment later. Free, no model. Up/Down move, Tab or Enter accept, Esc closes, Tab on a word opens it.
// 2) AI inline suggestion (grey text) once you pause and no list is open: cached, never while deleting, trimmed as you type the same letters.
export const WORD = /[A-Za-z_][A-Za-z0-9_]*$/;
export const IDENT = /[A-Za-z_][A-Za-z0-9_]{2,}/g, TRIG = /[A-Za-z0-9_.]$/;
export const KW = ["import", "from", "return", "lambda", "class", "def", "print", "range", "len", "enumerate", "isinstance", "Exception", "True", "False", "None", "with", "while", "continue", "break", "yield", "global", "assert", "raise", "except", "finally", "elif", "else", "for", "if", "in", "not", "and", "or", "pass", "try", "sum", "min", "max", "sorted", "zip", "list", "dict", "set", "tuple", "str", "int", "float", "open", "type"];
const lru = new Map(), KIND_IC = { function: "ƒ", method: "ƒ", class: "C", module: "M", instance: "v", statement: "v", keyword: "k", property: "p", path: "/" };
let metrics = null;
const measure = (ta) => {
  const cs = getComputedStyle(ta), key = cs.font;
  if (!metrics || metrics.key !== key) { const c = document.createElement("canvas").getContext("2d"); c.font = key; metrics = { key, cw: c.measureText("MMMMMMMMMM").width / 10, lh: parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.55 }; }
  return { ...metrics, pl: parseFloat(cs.paddingLeft) || 0, pt: parseFloat(cs.paddingTop) || 0, pr: parseFloat(cs.paddingRight) || 0 };
};
export function CodeBox({ value, onChange, lang, onKey, suggest, delay, autofocus, label, words, complete, intel, marks, onAi, placeholder, nowrap, signature }) {
  const h = useHl(), ta = useRef(), tm = useRef(), cm = useRef(), tick = useRef(0), cur = useRef(value), [ghost, setGhostS] = useState({ at: 0, text: "" }), [pop, setPopS] = useState(null), [tip, setTip] = useState(null), sigs = useRef(new Map());
  const gh = useRef(ghost), pp = useRef(null), setGhost = (g) => { gh.current = g; setGhostS(g); }, setPop = (x) => { pp.current = x; setPopS(x); };
  useEffect(() => { if (autofocus && ta.current) ta.current.focus(); }, []);
  useEffect(() => () => { clearTimeout(tm.current); clearTimeout(cm.current); }, []);
  cur.current = value;
  const hl = useMemo(() => {
    const g = ghost.text ? '<span class="nbj-ghost">' + esc(ghost.text) + "</span>" : "";
    if (!g || ghost.at >= value.length) return colorize(h, lang, value) + g;
    return colorize(h, lang, value.slice(0, ghost.at)) + g + colorize(h, lang, value.slice(ghost.at));
  }, [h, lang, value, ghost]);
  const mk = useMemo(() => { if (!marks || !marks.length) return ""; let o = "", at = 0; for (const [a, b, on] of marks) { if (a < at || b > value.length) continue; o += esc(value.slice(at, a)) + "<mark" + (on ? ' class="on"' : "") + ">" + esc(value.slice(a, b)) + "</mark>"; at = b; } return o + esc(value.slice(at)) + "\n"; }, [marks, value]);
  const localItems = (before) => {                           // names used in this notebook that start with the word being typed (not after a dot: that is the kernel's job)
    const m = WORD.exec(before); if (!m || !words || m[0].length < 1 || before[before.length - m[0].length - 1] === ".") return null;
    const w = m[0], out = [], lw = w.toLowerCase();
    for (const x of words()) if (x.length > w.length && (x.startsWith(w) || (w.length > 1 && x.toLowerCase().startsWith(lw)))) { out.push({ t: x, k: "" }); if (out.length >= 30) break; }
    return out.length ? { items: out, start: before.length - w.length, end: before.length, sel: 0 } : null;
  };
  const openList = (v, at, now) => {
    const before = v.slice(0, at), my = ++tick.current;
    const loc = localItems(before); if (loc) setPop(loc); else if (!now) setPop(null);
    clearTimeout(cm.current);
    if (!complete || /^\s*[%!]/.test(before.split("\n").pop())) return;
    cm.current = setTimeout(async () => {
      let r; try { r = await complete(v, at); } catch (e) { return; }
      if (tick.current !== my || !ta.current || ta.current.value !== v || ta.current.selectionStart !== at) return;
      const pre = v.slice(r.start, at), keep = pre.startsWith("_") || before.endsWith("._") ? () => true : (m) => !m.t.split(".").pop().startsWith("_");
      const ms = r.matches.filter(keep).slice(0, 40);
      if (!ms.length) { if (!loc) setPop(null); return; }
      if (now && ms.length === 1) { accept({ items: ms, start: r.start, end: at, sel: 0 }, 0, v); return; }
      setPop({ items: ms, start: r.start, end: at, sel: 0 });
    }, now ? 0 : 70);
  };
  // signature help: while the cursor is inside f( ... ), show f's parameters with the one being typed in bold (signature(code, pos) asks the kernel or the SQL table)
  const callAt = (v, at) => {
    let d = 0, commas = 0, i = at - 1, q = "";
    for (; i >= 0 && at - i < 2500; i--) { const c = v[i]; if (q) { if (c === q && v[i - 1] !== "\\") q = ""; continue; } if (c === '"' || c === "'") q = c; else if (c === ")" || c === "]" || c === "}") d++; else if (c === "(" || c === "[" || c === "{") { if (d === 0) break; d--; } else if (c === "," && d === 0) commas++; }
    return i >= 0 && v[i] === "(" ? { open: i, arg: commas } : null;
  };
  const showTip = (v, at) => {
    if (!signature) return;
    const c = callAt(v, at); if (!c) { setTip((t) => (t ? null : t)); return; }
    const key = v.slice(Math.max(0, c.open - 60), c.open), hit = sigs.current.get(key);
    const put = (r) => { if (r && r.sig) setTip({ ...r, open: c.open, arg: c.arg }); else setTip(null); };
    if (hit !== undefined) { put(hit); return; }
    signature(v.slice(0, c.open), c.open).then((r) => { sigs.current.set(key, r || null); if (ta.current && ta.current.selectionStart === at) put(r); }).catch(() => {});
  };
  const accept = (p, i, v0) => {
    const it = p.items[i], v = (v0 != null ? v0 : cur.current), nv = v.slice(0, p.start) + it.t + v.slice(p.end), at = p.start + it.t.length;
    tick.current++; setPop(null); setGhost({ at: 0, text: "" }); onChange(nv);
    requestAnimationFrame(() => { if (ta.current) { ta.current.selectionStart = ta.current.selectionEnd = at; showTip(nv, at); } });
  };
  const change = (v, at, kind) => {
    const prev = gh.current, was = cur.current; tick.current++; onChange(v); clearTimeout(tm.current);
    if (at == null) at = v.length;
    const typed = v.length - was.length, ins = (kind || "").startsWith("insert");
    if (prev.text && kind === "insertText" && typed > 0 && at === prev.at + typed && v.slice(prev.at, at) === prev.text.slice(0, typed) && v.slice(0, prev.at) === was.slice(0, prev.at)) {
      const rest = prev.text.slice(typed); setGhost({ at, text: rest }); if (rest) return;   // typed through the grey text
    } else if (prev.text) setGhost({ at, text: "" });
    if (signature && ins) showTip(v, at); else if (signature && tip && (kind || "").startsWith("delete")) showTip(v, at);
    const rest = v.slice(at).split("\n")[0], eol = !rest.trim(), tailOk = eol || /^[\s)\]}'"`,:;]*$/.test(rest);       // only closing brackets or quotes after the cursor
    if (intel && ins && eol && TRIG.test(v.slice(0, at)) && !/^\s*#/.test(v.slice(0, at).split("\n").pop())) { openList(v, at, false); }
    else if (pp.current && !((kind || "").startsWith("delete") && TRIG.test(v.slice(0, at)))) setPop(null);
    else if (pp.current) openList(v, at, false);
    if (!delay || (kind || "").startsWith("delete") || !tailOk || !suggest || v.slice(0, at).trim().length < 2) return;
    const lines = v.slice(0, at).split("\n"), line = lines[lines.length - 1], prevl = [...lines].slice(0, -1).reverse().find((x) => x.trim()) || "";
    const fresh = !line.trim() && lines.length > 1;                            // a new empty line: only worth asking after a comment or a line ending in ':' (write the block)
    if (fresh && !(/:\s*$/.test(prevl) || /^\s*#/.test(prevl))) return;
    if (/^\s*#/.test(line) && line.trim()) return;                             // typing a comment: wait for the line break
    sched(v, at, fresh ? 280 : Math.max(350, delay - 300));
  };
  // ask the model for grey text at AT once the cursor rests there; Alt+\ forces a request
  const sched = (v, at, wait) => {
    clearTimeout(tm.current);
    const my = tick.current, before = v.slice(0, at), after = v.slice(at);
    tm.current = setTimeout(async () => {
      const t = ta.current;
      if (!t || t.value !== v || t.selectionStart !== at || document.activeElement !== t) return;      // you moved on
      const k = before.slice(-800) + "\u0001" + after.slice(0, 120);
      let s = lru.get(k);
      if (s == null) { onAi && onAi("busy"); try { s = (await suggest(before, after)).replace(/\s+$/, ""); } catch (e) { onAi && onAi("err", e.message); return; } onAi && onAi(s ? "ok" : "none"); lru.set(k, s); if (lru.size > 80) lru.delete(lru.keys().next().value); }
      if (tick.current === my && s) setGhost({ at, text: s });
    }, wait);
  };
  const acceptGhost = (part) => {
    const g = gh.current; if (!g.text) return;
    let t = g.text; if (part) { const m = /^\s*\S+/.exec(t); if (m && m[0].length < t.length) t = m[0]; }
    const v = value.slice(0, g.at) + t + value.slice(g.at), at = g.at + t.length;
    tick.current++; setGhost({ at, text: part && t.length < g.text.length ? g.text.slice(t.length) : "" }); onChange(v);
    requestAnimationFrame(() => { if (ta.current) ta.current.selectionStart = ta.current.selectionEnd = at; });
  };
  const key = (e) => {
    const p = pp.current;
    if (p) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); const n = p.items.length; setPop({ ...p, sel: (p.sel + (e.key === "ArrowDown" ? 1 : n - 1)) % n }); return; }
      if ((e.key === "Tab" && !e.shiftKey) || (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey)) { e.preventDefault(); accept(p, p.sel); return; }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setPop(null); return; }
    }
    if (e.altKey && (e.key === "\\" || e.key === "]") && !gh.current.text && suggest) { e.preventDefault(); sched(value, e.target.selectionStart, 0); return; }
    if (e.key === "Tab" && !e.shiftKey && gh.current.text) { e.preventDefault(); acceptGhost(false); return; }
    if (e.key === "ArrowRight" && gh.current.text && !e.altKey && !e.shiftKey && e.target.selectionStart === gh.current.at && e.target.selectionEnd === e.target.selectionStart && e.target.selectionStart === value.length) { e.preventDefault(); acceptGhost(false); return; }
    if (e.key === "ArrowRight" && gh.current.text && e.target.selectionStart === gh.current.at && e.altKey) { e.preventDefault(); acceptGhost(true); return; }
    if (e.key === "Escape" && gh.current.text) { e.preventDefault(); e.stopPropagation(); setGhost({ at: 0, text: "" }); return; }
    if (e.key === "Tab" && !e.shiftKey) {
      const t = e.target, a = t.selectionStart, b = t.selectionEnd;
      if (intel && a === b && TRIG.test(value.slice(0, a)) && !/^\s*$/.test(value.slice(0, a).split("\n").pop())) { e.preventDefault(); openList(value, a, true); return; }   // Tab on a word = complete it
      e.preventDefault(); change(value.slice(0, a) + "    " + value.slice(b), a + 4, "insertText"); requestAnimationFrame(() => { t.selectionStart = t.selectionEnd = a + 4; }); return;
    }
    onKey && onKey(e);
  };
  const move = (e) => { if (signature && !pp.current) showTip(cur.current, e.target.selectionStart); if (gh.current.text && e.target.selectionStart !== gh.current.at) setGhost({ at: 0, text: "" }); const p = pp.current; if (p && (e.target.selectionStart !== p.end || e.target.selectionEnd !== p.end)) setPop(null); };
  let box = null;
  if (pop && ta.current) {                                  // below the caret (above it when the screen ends)
    const m = measure(ta.current), cols = Math.max(8, Math.floor((ta.current.clientWidth - m.pl - m.pr) / m.cw)), ls = value.slice(0, pop.start).split("\n");
    let rows = 0; for (let i = 0; i < ls.length - 1; i++) rows += Math.max(1, Math.ceil(ls[i].length / cols));
    rows += Math.floor(ls[ls.length - 1].length / cols);
    const top = m.pt + (rows + 1) * m.lh, left = Math.min(m.pl + (ls[ls.length - 1].length % cols) * m.cw, Math.max(0, ta.current.clientWidth - 260));
    const r = ta.current.getBoundingClientRect(), up = r.top + top + 230 > innerHeight && r.top + top - m.lh - 230 > 0;
    box = html`<div class="nbj-pop" role="listbox" style=${up ? { left: left + "px", bottom: ta.current.clientHeight - top + m.lh + "px" } : { left: left + "px", top: top + "px" }} onMouseDown=${(e) => e.preventDefault()}>
      ${pop.items.map((it, i) => html`<div key=${it.t} role="option" aria-selected=${i === pop.sel} class=${"nbj-opt" + (i === pop.sel ? " on" : "")} ref=${(el) => { if (el && i === pop.sel) el.scrollIntoView({ block: "nearest" }); }} onClick=${() => { accept(pop, i); if (ta.current) ta.current.focus(); }}>
        <span class=${"nbj-k k-" + (it.k || "x")}>${KIND_IC[it.k] || "·"}</span><span class="nbj-t">${it.t.includes(".") && it.t.startsWith(value.slice(pop.start, pop.end)) ? it.t.slice(it.t.lastIndexOf(".") + 1) : it.t}</span>${it.d && html`<small class="nbj-d">${it.d}</small>`}</div>`)}</div>`;
  }
  let tipEl = null;
  if (tip && ta.current) {
    const m = measure(ta.current), cols = Math.max(8, Math.floor((ta.current.clientWidth - m.pl - m.pr) / m.cw)), ls = cur.current.slice(0, tip.open).split("\n");
    let rows = 0; for (let i = 0; i < ls.length - 1; i++) rows += Math.max(1, Math.ceil(ls[i].length / cols));
    rows += Math.floor(ls[ls.length - 1].length / cols);
    const lineTop = m.pt + rows * m.lh, left = Math.min(m.pl + (ls[ls.length - 1].length % cols) * m.cw, Math.max(0, ta.current.clientWidth - 320)), r = ta.current.getBoundingClientRect(), below = r.top + lineTop < 90;
    const o = tip.sig.indexOf("("), name = o < 0 ? tip.sig : tip.sig.slice(0, o), rest = o < 0 ? "" : tip.sig.slice(o + 1);
    const ps = []; let dd = 0, cur0 = 0, qq = "", k = 0;
    for (; k < rest.length; k++) { const c = rest[k]; if (qq) { if (c === qq) qq = ""; continue; } if (c === '"' || c === "'") qq = c; else if ("([{".includes(c)) dd++; else if (")]}".includes(c)) { if (dd === 0) break; dd--; } else if (c === "," && dd === 0) { ps.push(rest.slice(cur0, k).trim()); cur0 = k + 1; } }
    if (rest.slice(cur0, k).trim()) ps.push(rest.slice(cur0, k).trim());
    const tail = rest.slice(k), act = Math.min(tip.arg, Math.max(0, ps.length - 1));
    tipEl = html`<div class="nbj-tip" role="tooltip" style=${below ? { left: left + "px", top: lineTop + m.lh + 4 + "px" } : { left: left + "px", bottom: ta.current.clientHeight - lineTop + 4 + "px" }}>
      <div class="nbj-sig"><span class="fn">${name}</span>(${ps.map((x, i) => html`${i ? ", " : ""}<span class=${i === act ? "on" : ""}>${x}</span>`)}${tail}</div>${tip.doc && html`<div class="nbj-doc">${tip.doc}</div>`}</div>`;
  }
  return html`<div class=${"nb-src syn" + (nowrap ? " nowrap" : "")}>${mk && html`<pre class="nbj-marks" aria-hidden="true" dangerouslySetInnerHTML=${{ __html: mk }}></pre>`}<pre aria-hidden="true" dangerouslySetInnerHTML=${{ __html: hl + "\n" }}></pre><textarea ref=${ta} rows="1" wrap=${nowrap ? "off" : undefined} placeholder=${placeholder} spellcheck="false" autocomplete="off" autocapitalize="off" value=${value} onInput=${(e) => change(e.target.value, e.target.selectionStart, e.inputType || "")} onKeyDown=${key} onKeyUp=${move} onClick=${move} onBlur=${() => { setGhost({ at: 0, text: "" }); setPop(null); setTip(null); }} aria-label=${label || "Cell source"}></textarea>${box}${tipEl}</div>`;
}

