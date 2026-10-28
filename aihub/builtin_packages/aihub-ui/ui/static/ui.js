// Small UI primitives: toast, popover, segmented control with a sliding indicator, switch, modal.
import { html, useState, useEffect, useRef, useLayoutEffect } from "https://cdn.jsdelivr.net/npm/htm@3.1.1/preact/standalone.module.js";
import { Ic } from "./icons.js";

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
    const k = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", d); document.addEventListener("keydown", k);
    return () => { document.removeEventListener("pointerdown", d); document.removeEventListener("keydown", k); };
  }, [open]);
  return html`<div class="pw" ref=${ref}>${button}${open && html`<div class=${"pop " + (up ? "up " : "down ") + align} style=${width ? `width:${width}px` : ""}>${children}</div>`}</div>`;
}

export const Switch = ({ on, onChange, label }) => html`<button class=${"sw" + (on ? " on" : "")} role="switch" aria-checked=${on} aria-label=${label} onClick=${() => onChange(!on)}><i></i></button>`;

export function Modal({ children, close, wide }) {
  useEffect(() => { const k = (e) => e.key === "Escape" && close && close(); document.addEventListener("keydown", k); return () => document.removeEventListener("keydown", k); }, []);
  return html`<div class="modal" onPointerDown=${(e) => e.target === e.currentTarget && close && close()}><div class=${"sheet" + (wide ? " wide" : "")}>${children}</div></div>`;
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
