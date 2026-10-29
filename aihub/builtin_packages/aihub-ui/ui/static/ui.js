// Small UI primitives: toast, popover, segmented control with a sliding indicator, switch, modal.
import { html, useState, useEffect, useRef, useLayoutEffect } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";

// ---------- Esc stack: only the top-most layer (modal, popover, zoom, drawer) reacts to Esc
const layers = [];
export const escPush = (fn) => { const o = { fn }; layers.push(o); return () => { const i = layers.indexOf(o); if (i >= 0) layers.splice(i, 1); }; };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && layers.length && !e.defaultPrevented) layers[layers.length - 1].fn(e); });
// setInterval that sleeps while the tab is hidden and fires once when it comes back; `ms` null = off. The callback is read fresh on every tick.
export function useInterval(fn, ms) {
  const f = useRef(); f.current = fn;
  useEffect(() => {
    if (ms == null) return;
    const tick = () => { if (!document.hidden) f.current(); }, t = setInterval(tick, ms);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", tick); };
  }, [ms]);
}

let toastSet = null;
export const setToaster = (f) => { toastSet = f; };
export const toast = (t) => { if (toastSet) { toastSet(t); clearTimeout(toast.t); toast.t = setTimeout(() => toastSet(""), 3500); } };
export const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message); } };

export function Seg({ items, value, onChange, small }) {
  const ref = useRef(), [ind, setInd] = useState({ l: 0, w: 0 });
  useLayoutEffect(() => { const b = ref.current && ref.current.querySelector('[data-on="1"]'); if (b) setInd({ l: b.offsetLeft, w: b.offsetWidth }); }, [value, items.length]);
  return html`<div class=${"seg" + (small ? " sm" : "")} ref=${ref} role="tablist"><i class="ind" style=${`transform:translateX(${ind.l}px);width:${ind.w}px`}></i>
    ${items.map((it) => html`<button role="tab" data-on=${it.id === value ? "1" : "0"} class=${it.id === value ? "on" : ""} onClick=${() => onChange(it.id)}>${it.icon && Ic(it.icon, 14)}<span>${it.label}</span></button>`)}</div>`;
}

export function Pop({ open, setOpen, button, children, up = true, align = "left", width }) {
  const ref = useRef();
  useEffect(() => {
    if (!open) return;
    const d = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const off = escPush(() => setOpen(false));
    document.addEventListener("pointerdown", d);
    return () => { document.removeEventListener("pointerdown", d); off(); };
  }, [open]);
  return html`<div class="pw" ref=${ref}>${button}${open && html`<div class=${"pop " + (up ? "up " : "down ") + align} style=${width ? `width:${width}px` : ""}>${children}</div>`}</div>`;
}

export const Switch = ({ on, onChange, label }) => html`<button class=${"sw" + (on ? " on" : "")} role="switch" aria-checked=${on} aria-label=${label} onClick=${() => onChange(!on)}><i></i></button>`;

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
export function Modal({ children, close, wide }) {
  const cr = useRef(), box = useRef(); cr.current = close;                 // `close` is read through a ref: the Esc handler lives for the whole life of the dialog
  useEffect(() => {
    const prev = document.activeElement, off = escPush(() => cr.current && cr.current());
    const b = box.current; if (b && !b.contains(document.activeElement)) { const f = b.querySelector("input,select,textarea") || b.querySelector(FOCUSABLE); if (f) f.focus(); else { b.tabIndex = -1; b.focus(); } }
    return () => { off(); if (prev && prev.focus && document.contains(prev)) try { prev.focus(); } catch (e) { /* gone */ } };
  }, []);
  const trap = (e) => {
    if (e.key !== "Tab" || !box.current) return;
    const l = [...box.current.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null);
    if (!l.length) { e.preventDefault(); return; }
    const a = document.activeElement;
    if (e.shiftKey && (a === l[0] || !box.current.contains(a))) { e.preventDefault(); l[l.length - 1].focus(); } else if (!e.shiftKey && (a === l[l.length - 1] || !box.current.contains(a))) { e.preventDefault(); l[0].focus(); }
  };
  return html`<div class="modal" onPointerDown=${(e) => e.target === e.currentTarget && cr.current && cr.current()} onKeyDown=${trap}><div class=${"sheet" + (wide ? " wide" : "")} role="dialog" aria-modal="true" ref=${box}>${children}</div></div>`;
}

// ---------- small form dialogs (replace prompt()/confirm()): ask({title, text, fields, submit, danger}) -> values or null
let asker = () => Promise.resolve(null);
export const ask = (spec) => asker(spec);
export function AskHost() {
  const [q, setQ] = useState(null);
  useEffect(() => { asker = (spec) => new Promise((ok) => setQ({ spec, ok, n: Math.random() })); return () => { asker = () => Promise.resolve(null); }; }, []);
  if (!q) return null;
  const done = (v) => { q.ok(v); setQ(null); };
  return html`<${AskForm} key=${q.n} spec=${q.spec} done=${done}/>`;
}
function AskForm({ spec, done }) {
  const fields = spec.fields || [], [v, setV] = useState(() => Object.fromEntries(fields.map((f) => [f.name, f.value ?? ""]))), first = useRef();
  useEffect(() => { first.current && first.current.focus(); first.current && first.current.select && first.current.select(); }, []);
  const shown = fields.filter((f) => !f.when || f.when(v)), bad = shown.find((f) => f.required && !String(v[f.name] ?? "").trim());
  const submit = (e) => { e.preventDefault(); if (!bad) done(v); };
  return html`<${Modal} close=${() => done(null)}><form onSubmit=${submit}><h1>${spec.title}</h1>${spec.text && html`<p class="sub">${spec.text}</p>`}
    ${shown.map((f, i) => html`<div class="field"><label>${f.label}${f.required ? " *" : ""}</label>
      ${f.type === "select" ? html`<select ref=${i === 0 ? first : null} value=${v[f.name]} onChange=${(e) => setV({ ...v, [f.name]: e.target.value })}>${f.options.map((o) => html`<option value=${o.value}>${o.label}</option>`)}</select>`
      : html`<input ref=${i === 0 ? first : null} type="text" maxlength=${f.max || 120} placeholder=${f.placeholder || ""} value=${v[f.name]} onInput=${(e) => setV({ ...v, [f.name]: e.target.value })}/>`}
      ${f.help && html`<span class="mute small">${f.help}</span>`}</div>`)}
    <div class="row" style="margin-top:18px"><span class="grow"></span><button type="button" class="btn" onClick=${() => done(null)}>Cancel</button>
      <button type="submit" class=${"btn " + (spec.danger ? "bad" : "acc")} disabled=${!!bad}>${spec.submit || "OK"}</button></div></form><//>`;
}
