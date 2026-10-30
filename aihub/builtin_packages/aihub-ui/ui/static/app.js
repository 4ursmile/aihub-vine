// AI Hub UI. Preact + htm, marked, DOMPurify, highlight.js and OpenUI come from jsDelivr (pinned). No build step, no npm.
import { h, html, render, useState, useEffect, useRef, useCallback, useMemo } from "./vendor/preact-htm.js";
import { Ic } from "./icons.js";
import { toast, guard, setToaster, Seg, Pop, Switch, Modal, ask, AskHost, escPush, useInterval } from "./ui.js";
import { Md, AskCard, setWorkMaker, setBlockSharer, setNodeHost } from "./blocks.js";
import * as OUI from "./openui.js";
import { UsagePage, AlertBar, useAlerts } from "./usage.js";

// term.js, slides.js (+ export.js) and space.js load when first needed, not at start
const runInstall = (...a) => import("./term.js").then((m) => m.runInstall(...a));
const chatsMod = () => import("./chats.js");
const shareToSpace = (...a) => import("./space.js").then((m) => m.shareToSpace(...a));
function lazy(load, name) {
  let C = null, bad = "";
  return function Lazy(p) {
    const [, bump] = useState(0);
    useEffect(() => { if (!C) load().then((m) => { C = m[name]; bump(1); }).catch((x) => { bad = x.message || "could not load"; bump(1); }); }, []);
    return C ? h(C, p) : bad ? html`<div class="empty"><div>${Ic("alert", 24)}<div>${bad}</div></div></div>` : html`<div class="empty"><div class="shine">Loading…</div></div>`;
  };
}
const Viewer = lazy(() => import("./slides.js"), "Viewer"), Themes = lazy(() => import("./slides.js"), "Themes");

// ---------- api
// Accent palettes of the hub (same names as the hub's Settings > Site): [light, dark] -> accent, hover, tinted background.
const PALETTE = {
  blue: [["#0071e3", "#005bb8", "#e6f0fc"], ["#0a84ff", "#409cff", "#1d2b3d"]], indigo: [["#5856d6", "#4745b0", "#ececfa"], ["#8886f5", "#a3a2f7", "#2a2a44"]],
  purple: [["#a03fd0", "#8431ae", "#f3e8fa"], ["#bf5af2", "#cc7cf5", "#34264a"]], pink: [["#cc2d65", "#a82454", "#fae6ed"], ["#ff375f", "#ff6482", "#44222c"]],
  red: [["#d70015", "#b00011", "#fbe6e8"], ["#ff453a", "#ff6961", "#44231f"]], orange: [["#b35000", "#8f4000", "#faeedf"], ["#ff9f0a", "#ffb340", "#40301a"]],
  claude: [["#b9573a", "#9c4630", "#fbeee8"], ["#d97757", "#e8896b", "#3b2d27"]], green: [["#17803a", "#126630", "#e3f4e8"], ["#30d158", "#5ddc7c", "#1d3a27"]],
  teal: [["#087c7c", "#066363", "#e1f3f3"], ["#40c8e0", "#6dd5e8", "#1c3a40"]],
};
const FAVICON = "data:image/svg+xml," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='#14141f'/><path d='M6 26 16 6l10 20M10.5 19.5H16' fill='none' stroke='#ff6a3d' stroke-width='3.2' stroke-linejoin='miter' stroke-linecap='square'/><path d='M19.5 19.5h3' stroke='#19e6d2' stroke-width='3.2'/></svg>");
function applyBrand(b) {
  if (!b || !b.theme) return;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches, [a, h, bg] = PALETTE[b.theme][dark ? 1 : 0] ? PALETTE[b.theme][dark ? 1 : 0] : PALETTE.claude[0], st = document.documentElement.style;
  if (b.theme !== "claude") { st.setProperty("--accent", a); st.setProperty("--accent2", h); st.setProperty("--accentbg", bg); st.setProperty("--on", dark ? "#000" : "#fff"); }
  else ["--accent", "--accent2", "--accentbg", "--on"].forEach((k) => st.removeProperty(k));       // the built-in palette already is Claude orange
}

async function api(path, { method = "GET", body, signal } = {}) {
  const r = await fetch("/api/" + path, { method, signal, headers: method === "GET" ? {} : { "Content-Type": "application/json", "X-AIHub-UI": "1" }, body: body === undefined ? undefined : JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { const er = new Error(d.error || r.statusText); er.status = r.status; throw er; }
  return d;
}
// The system's own file/folder dialog (the server runs on this machine). -> path, "" when cancelled, null when this machine has none.
const nativePick = async (b) => { try { const r = await post("fs/pick", b); return r.unsupported ? null : r.path; } catch (e) { return null; } };
const post = (p, b) => api(p, { method: "POST", body: b || {} });
setWorkMaker(async (work) => (await post("works/make", { work })).work);
const put = (p, b) => api(p, { method: "PUT", body: b });
async function stream(path, body, on, signal) {
  const r = await fetch("/api/" + path, { method: "POST", signal, headers: { "Content-Type": "application/json", "X-AIHub-UI": "1" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  const rd = r.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await rd.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i).trim(); buf = buf.slice(i + 2);
      if (line.startsWith("data:")) try { on(JSON.parse(line.slice(5))); } catch (e) { /* skip a bad line */ }
    }
  }
}
const hilite = (t, q) => { const s = String(t || ""), i = s.toLowerCase().indexOf(String(q).toLowerCase()); return i < 0 || !q ? s : html`${s.slice(0, i)}<mark>${s.slice(i, i + q.length)}</mark>${s.slice(i + q.length)}`; };
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? "now" : s < 5400 ? Math.round(s / 60) + "m" : s < 129600 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d"; };
const num = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(Math.round(n || 0));
const pref = (k, d) => { try { return localStorage.getItem("aihub_ui_" + k) ?? d; } catch (e) { return d; } };
const setPref = (k, v) => { try { localStorage.setItem("aihub_ui_" + k, v); } catch (e) { /* private mode */ } };
const linkify = (t) => String(t).split(/(https:\/\/[^\s)"']+)/).map((p, i) => (i % 2 ? html`<a href=${p} target="_blank" rel="noopener noreferrer">${p}</a>` : p));

// ---------- pickers (model, effort, mode, context) and the slash menu
const MODE_INFO = {
  auto: ["sparkles", "Auto", "Claude decides when to ask before acting."],
  edits: ["edit", "Accept edits", "File edits go through; other risky actions are denied here."],
  plan: ["eye", "Plan", "Think and propose a plan first. Nothing is changed."],
  ask: ["shieldcheck", "Read only", "Anything that would change something is denied."],
  bypass: ["alert", "Bypass checks", "Runs every tool without asking. Use only in a throwaway folder."],
};
const EFFORT_INFO = { "": "Uses your Claude settings.", low: "Fast, minimal thinking.", medium: "Balanced.", high: "Thorough. Good default for code.", xhigh: "Very thorough, slower.", max: "Deepest reasoning. Slowest and most costly." };
const UI_CMDS = [
  { name: "compact", description: "Summarise the conversation to free up context", action: "send" },
  { name: "clear", description: "Start a fresh chat", action: "clear" },
  { name: "model", description: "Choose the model", action: "model" },
  { name: "effort", description: "Set the thinking effort", action: "effort" },
  { name: "mode", description: "Set the permission mode", action: "mode" },
  { name: "context", description: "Context window and auto-compact", action: "ctx" },
  { name: "engine", description: "Switch engine: Claude Code or Free chat (/engine free, /engine claude)", action: "engine" },
  { name: "mcp", description: "MCP servers: see what is connected and choose what this chat may use", action: "mcp" },
  { name: "canvas", description: "Show or hide the canvas", action: "canvas" },
  { name: "create-work", description: "Describe a work; it is designed and opened in Works (also /workflow)", action: "workflow" },
  { name: "workflow", description: "Same as /create-work", action: "workflow" },
];
const SLASH_ICON = { folder: "folder", file: "file", theme: "palette", chat: "chat", tool: "bolt", work: "flow", ui: "slash", command: "terminal", skill: "sparkles", plugin: "plug", package: "package" };

function Pill({ icon, lead, label, active, onClick, title, tone }) {
  return html`<button class=${"pill" + (active ? " on" : "") + (tone ? " t-" + tone : "")} onClick=${onClick} title=${title}>${lead || Ic(icon, 15)}<span>${label}</span><span class="chev">${Ic("down", 12)}</span></button>`;
}
// composer tones: only the settings worth noticing get colour
const MODE_TONE = { auto: "auto", edits: "acc", plan: "good", ask: "good", bypass: "bad" };
const EFFORT_TONE = { high: "acc", xhigh: "acc2", max: "hot" };
// effort clock: an arc that fills with the level; high and up pulse softly
const EffMeter = (v) => { const n = Math.max(0, LEVELS.indexOf(v)), c = 2 * Math.PI * 5;
  return html`<svg class=${"eclock" + (n >= 3 ? " live" : "")} width="15" height="15" viewBox="0 0 14 14" aria-hidden="true"><circle class="bg" cx="7" cy="7" r="5"/><circle class="fg" cx="7" cy="7" r="5" stroke-dasharray=${c} stroke-dashoffset=${c * (1 - n / 5)}/></svg>`; };
function ModelPick({ models, value, onChange, close }) {
  const [q, setQ] = useState("");
  const row = (m) => html`<button class=${"opt" + (m.id === value ? " on" : "")} onClick=${() => { onChange(m.id); close(); }}><span class="oi">${Ic("cpu", 16)}</span>
    <span class="grow"><b>${m.label} ${m.window >= 1e6 && html`<span class="tag acc">1M</span>`}${m.kind === "image" && html` <span class="tag">Image</span>`}${m.kind === "tts" && html` <span class="tag">Speech</span>`}</b><span class="d">${m.note}${m.real && m.real !== m.label.toLowerCase() && m.real !== m.label ? " · " + m.real : ""}</span></span><span class="ck">${Ic("check", 16)}</span></button>`;
  const ok = (m) => !q || (m.id + " " + m.label).toLowerCase().includes(q.toLowerCase()), g = (k) => models.filter((m) => (m.group || "alias") === k && ok(m));
  const al = g("alias"), pk = g("picker"), st = g("settings"), keys = useOptKeys();
  return html`<div ...${keys} tabindex="-1" class="optlist"><h5>Model</h5>${models.length > 10 && html`<input class="mq" placeholder="Search models" value=${q} onInput=${(e) => setQ(e.target.value)}/>`}
    <div class="mlist">${al.map(row)}${pk.length > 0 && html`<h5 style="margin-top:6px">Your model picker</h5>${pk.map(row)}`}${st.length > 0 && html`<h5 style="margin-top:6px">From your API</h5>${st.map(row)}`}
    ${!al.length && !pk.length && !st.length && html`<p class="mute small" style="padding:8px 10px">No model matches.</p>`}</div>
    <div class="small mute" style="padding:6px 10px 4px">Models come from your Claude settings.json (three tiers, <code>modelPicker.options</code>) and, in Free chat, the list your API returns. Changing model mid-chat rewrites the prompt cache.</div><${OptKeys} n=${models.length > 10 ? 0 : al.length + pk.length + st.length}/></div>`;
}
const LEVELS = ["", "low", "medium", "high", "xhigh", "max"];
function EffortPick({ value, onChange, close }) {
  const i = Math.max(0, LEVELS.indexOf(value)), pct = (i / (LEVELS.length - 1)) * 100, el = useRef(), name = (l) => l === "" ? "Auto" : l === "xhigh" ? "XHigh" : l[0].toUpperCase() + l.slice(1);
  useEffect(() => { el.current && el.current.focus(); }, []);
  const go = (k) => onChange(LEVELS[Math.max(0, Math.min(LEVELS.length - 1, k))]);
  const onKey = (e) => {
    const k = e.key, m = { ArrowRight: i + 1, ArrowUp: i + 1, ArrowLeft: i - 1, ArrowDown: i - 1, Home: 0, End: LEVELS.length - 1 }[k];
    if (m !== undefined) go(m); else if (/^[0-5]$/.test(k)) go(+k); else if (k === "Enter" || k === " ") close(); else return;
    e.preventDefault();
  };
  return html`<div class=${"eff lv" + i} ref=${el} tabindex="0" role="slider" aria-label="Thinking effort" aria-valuemin="0" aria-valuemax="5" aria-valuenow=${i} aria-valuetext=${name(value)} onKeyDown=${onKey}>
    <div class="row"><b>Effort</b><span class="grow"></span><span class="eff-now">${EffMeter(value)}${name(value)}</span></div>
    <div class="eff-track"><div class="eff-fill" style=${`width:${pct}%`}></div>${LEVELS.map((l, k) => html`<button tabindex="-1" class=${"eff-dot" + (k <= i ? " on" : "") + (k === i ? " cur" : "")} style=${`left:${(k / (LEVELS.length - 1)) * 100}%`} title=${name(l)} onClick=${() => onChange(l)}></button>`)}</div>
    <div class="eff-labels">${LEVELS.map((l) => html`<span class=${l === value ? "b" : ""}>${name(l)}</span>`)}</div>
    <div class="small eff-d">${EFFORT_INFO[value]}</div>
    <div class="eff-keys"><kbd>←</kbd><kbd>→</kbd> change · <kbd>0</kbd>–<kbd>5</kbd> jump · <kbd>Enter</kbd> done</div></div>`;
}
// keyboard for every composer panel: focus the search box or the current option on open; ↑ ↓ Home End move over
// options, MCP cards, switches and fields; 1-9 pick an option / toggle a card; Enter/Space choose (native click)
const OPT_NAV = ".opt, .mcp-card, .sw, .mq, .mcp-q input, input[type=range], .ctxp .btn";
const isText = (el) => el && el.tagName === "INPUT" && el.type !== "range";
function useOptKeys(ready = true) {
  const el = useRef();
  const items = () => el.current ? [...el.current.querySelectorAll(OPT_NAV)].filter((x) => !x.disabled && !(x.classList.contains("sw") && x.closest(".mcp-card"))) : [];
  useEffect(() => { const b = el.current && (el.current.querySelector(".mq") || el.current.querySelector(".opt.on") || items()[0]); b && b.focus(); }, [!!ready]);
  const onKeyDown = (e) => {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const all = items(), t = e.target, k = e.key, txt = isText(t);
    if (!all.length || !(t === el.current || all.includes(t))) return;
    const at = all.indexOf(t), picks = all.filter((x) => x.matches(".opt, .mcp-card"));
    const to = { ArrowDown: at + 1, ArrowUp: at < 0 ? all.length - 1 : at - 1, ...(txt ? {} : { Home: 0, End: all.length - 1 }) }[k];
    if (to !== undefined) all[(to + all.length) % all.length].focus();
    else if (txt && k === "Enter" && picks[0]) picks[0].click();
    else if (!txt && /^[1-9]$/.test(k) && picks[+k - 1]) picks[+k - 1].click();
    else if (!txt && (k === "Enter" || k === " ") && t.classList.contains("mcp-card")) { const sw = t.querySelector(".sw"); sw && sw.click(); }
    else return;
    e.preventDefault();
  };
  return { ref: el, onKeyDown };
}
const OptKeys = ({ n, what = "pick", enter = "choose" }) => html`<div class="eff-keys opt-keys"><kbd>↑</kbd><kbd>↓</kbd> move${n > 0 && html` · <kbd>1</kbd>–<kbd>${Math.min(9, n)}</kbd> ${what}`} · <kbd>Enter</kbd> ${enter}</div>`;
function ModePick({ modes, value, onChange, close }) {
  const keys = useOptKeys();
  return html`<div ...${keys} tabindex="-1" class="optlist"><h5>Permission mode</h5>${modes.map((m) => { const [ic, name, d] = MODE_INFO[m] || ["shield", m, ""]; return html`<button class=${"opt" + (m === value ? " on" : "") + (m === "bypass" ? " danger" : "")} onClick=${() => { onChange(m); close(); }}><span class="oi">${Ic(ic, 16)}</span><span class="grow"><b>${name}</b><span class="d">${d}</span></span><span class="ck">${Ic("check", 16)}</span></button>`; })}<${OptKeys} n=${modes.length}/></div>`;
}
const ENGINES = [["claude", "terminal", "Claude Code", "The full harness: tools, files in the working folder, skills, agents and MCP servers."],
  ["free", "zap", "Free chat", "Our own light harness: straight to the model, answers fast. No shell or files; it can use the MCP servers you tick below. Needs ANTHROPIC_API_KEY (Settings > Encrypted secrets) or your Claude settings env / key helper."]];
const MCP_TONE = { package: "package", claude: "terminal", ui: "sliders" };
function McpPanel({ servers, on, onChange, engine, compact }) {
  const [q, setQ] = useState(""), keys = useOptKeys(servers && servers.length);
  if (!servers) return html`<div class="mcp-empty"><div class="shine">Looking for MCP servers…</div></div>`;
  if (!servers.length) return html`<div class="mcp-empty">${Ic("plug", 26)}<b>No MCP servers yet</b><span>Install an MCP package or add one on the <a href="#/mcp">MCP servers</a> page.</span></div>`;
  const live = servers.filter((m) => !m.off && on.includes(m.name)).length, shown = servers.filter((m) => !q || (m.name + " " + (m.origin || "")).toLowerCase().includes(q.toLowerCase()));
  const toggle = (m, v) => onChange(v ? [...on, m.name] : on.filter((x) => x !== m.name));
  return html`<div ...${compact ? {} : keys} tabindex=${compact ? undefined : "-1"} class=${"mcp optlist" + (compact ? " compact" : "")}>
    <div class="mcp-head"><span class="mcp-sum"><i class=${"dot" + (live ? " live" : "")}></i><b>${engine === "free" ? live + " of " + servers.length + " on for this chat" : servers.length + " server" + (servers.length === 1 ? "" : "s")}</b></span>
      ${servers.length > 6 && html`<label class="mcp-q">${Ic("search", 13)}<input placeholder="Filter" value=${q} onInput=${(e) => setQ(e.target.value)}/></label>`}</div>
    <div class="mcp-grid">${shown.map((m) => { const act = engine === "free" && !m.off && on.includes(m.name);
      return html`<div class=${"mcp-card" + (act ? " on" : "") + (m.off ? " off" : "")} key=${m.name} tabindex="0" role=${engine === "free" ? "switch" : undefined} aria-checked=${engine === "free" ? act : undefined} aria-label=${m.name}>
        <span class="mcp-ico">${Ic("plug", 17)}<i class=${"dot" + (m.off ? " off" : act || engine !== "free" ? " live" : "")}></i></span>
        <span class="grow"><b>${m.name}</b><span class="d">${m.off ? "Switched off on the MCP page" : m.command ? String(m.command).slice(0, 70) : "MCP server"}</span>
          <span class="mcp-tags"><span class="tag">${m.origin || "server"}</span>${(m.tools_off || []).length > 0 && html`<span class="tag">${m.tools_off.length} hidden</span>`}${m.default && !m.off && html`<span class="tag acc">default</span>`}</span></span>
        ${engine === "free" ? html`<${Switch} on=${act} label=${"Use " + m.name} onChange=${(v) => !m.off && toggle(m, v)}/>` : html`<span class="tag acc">${Ic("check", 11)} ready</span>`}</div>`; })}</div>
    ${!shown.length && html`<p class="mute small" style="padding:8px 4px">No server matches.</p>`}
    <p class="mcp-note">${engine === "free" ? "Ticked servers start for each answer; the model may call their tools without asking." : "Claude Code uses every server it knows. Switch to Free chat (/engine free) to choose per chat."}</p>${!compact && engine === "free" && html`<${OptKeys} n=${shown.length} what="toggle" enter="toggle"/>`}</div>`;
}
function EnginePick({ value, onChange, close, mcpAll, mcpOn, onMcp }) {
  const keys = useOptKeys();
  return html`<div ...${keys} tabindex="-1" class="optlist"><h5>Engine</h5>${ENGINES.map(([k, ic, name, d]) => html`<button class=${"opt" + (k === value ? " on" : "")} onClick=${() => { onChange(k); close(); }}><span class="oi">${Ic(ic, 16)}</span><span class="grow"><b>${name}</b><span class="d">${d}</span></span><span class="ck">${Ic("check", 16)}</span></button>`)}
    ${value === "free" && html`<h5 style="margin-top:8px">Tools for free chat (MCP)</h5><div style="padding:0 6px"><${McpPanel} servers=${mcpAll} on=${mcpOn} onChange=${onMcp} engine="free" compact=${true}/></div>`}
    <div class="small mute" style="padding:6px 10px 4px">You can switch at any message (or type /engine): the earlier text carries over, tool calls do not.</div><${OptKeys} n=${ENGINES.length}/></div>`;
}
function Ring({ pct, size = 22 }) {
  const r = (size - 4) / 2, c = 2 * Math.PI * r;
  return html`<svg class="ring" width=${size} height=${size} viewBox=${`0 0 ${size} ${size}`}><circle class="bg" cx=${size / 2} cy=${size / 2} r=${r}/><circle class=${"fg" + (pct > 0.9 ? " hot" : "")} cx=${size / 2} cy=${size / 2} r=${r} stroke-dasharray=${c} stroke-dashoffset=${c * (1 - Math.min(1, pct))}/></svg>`;
}
function CtxPick({ used, win, prefs, savePrefs, compact, note, busy }) {
  const pct = win ? used / win : 0, keys = useOptKeys();
  return html`<div class="ctxp optlist" ...${keys} tabindex="-1"><div class="row"><b>Context window</b><span class="grow"></span>${win >= 1e6 && html`<span class="tag acc">1M</span>`}</div>
    <div class="big">${num(used)} <span class="mute" style="font:14px var(--font)">of ${num(win)} tokens · ${Math.round(pct * 100)}%</span></div>
    <div class="ctxbar"><i style=${`width:${Math.min(100, pct * 100)}%`}></i>${prefs.autocompact && html`<u style=${`left:${prefs.compact_pct}%`} title="auto-compact limit"></u>`}</div>
    <div class="small mute">${note || (used ? "What Claude re-reads each turn, mostly from cache." : "Nothing yet.")}</div>
    <div class="row" style="margin-top:14px"><div class="grow"><b>Auto-compact</b><div class="small mute">Summarise old turns when the limit is reached.</div></div><${Switch} on=${prefs.autocompact} label="Auto-compact" onChange=${(v) => savePrefs({ autocompact: v })}/></div>
    ${prefs.autocompact && html`<div style="margin-top:10px"><div class="row small"><span class="grow">Compact limit</span><b>${prefs.compact_pct}%</b> <span class="mute">≈ ${num((win * prefs.compact_pct) / 100)}</span></div>
      <input type="range" min="40" max="95" step="5" value=${prefs.compact_pct} style="width:100%" onChange=${(e) => savePrefs({ compact_pct: +e.target.value })}/></div>`}
    <div class="row" style="margin-top:12px"><button class="btn" disabled=${busy || !used} onClick=${compact}>${Ic("compact", 14)} Compact now</button></div><div class="eff-keys"><kbd>↑</kbd><kbd>↓</kbd> move · <kbd>←</kbd><kbd>→</kbd> limit · <kbd>Space</kbd> switch</div></div>`;
}
function SlashMenu({ items, idx, pick }) {
  const ref = useRef();
  useEffect(() => { const e = ref.current && ref.current.children[idx]; if (e && e.scrollIntoView) e.scrollIntoView({ block: "nearest" }); }, [idx]);
  if (!items.length) return null;
  return html`<div class="slash" ref=${ref}>${items.map((c, i) => html`<button class=${"opt" + (i === idx ? " hi" : "")} onMouseDown=${(e) => { e.preventDefault(); pick(c); }}><span class="oi">${Ic(SLASH_ICON[c.source] || "slash", 14)}</span>
    <span class="grow"><b>${["theme", "chat", "tool", "file", "folder"].includes(c.source) ? "@" : "/"}${c.name}</b><span class="d">${(c.description || "").slice(0, 110)}</span></span><span class="src">${c.sw ? html`<span class="swatch sm">${["bg", "accent", "fg"].map((k) => html`<i style=${`background:${c.sw[k]}`}></i>`)}</span>` : c.source === "ui" ? "" : c.source}</span></button>`)}</div>`;
}

// ---------- working folder: file tree, editor, diff, dialogs
const baseName = (p) => String(p || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || p;
function buildTree(files) {
  const root = { dirs: {}, files: [], n: 0 };
  for (const f of files) { const parts = f.path.split("/"); let n = root; n.n++; for (const d of parts.slice(0, -1)) { n = n.dirs[d] || (n.dirs[d] = { dirs: {}, files: [], n: 0 }); n.n++; } n.files.push({ ...f, name: parts[parts.length - 1] }); }
  return root;
}
const kbd = (fn) => (ev) => { if (ev.target === ev.currentTarget && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); fn(ev); } };
function FileTree({ files, sel, onPick, src }) {
  const [q, setQ] = useState(""), [tog, setTog] = useState({});
  const list = q ? files.filter((f) => f.path.toLowerCase().includes(q.toLowerCase())) : files, tree = buildTree(list), dflt = (depth) => !!q || depth < 1 || files.length <= 60;
  const walk = (n, base, depth) => [
    ...Object.keys(n.dirs).sort().map((d) => { const path = base + d, isOpen = tog[path] ?? dflt(depth);
      return html`<div class="tr dir" role="button" tabindex="0" aria-expanded=${isOpen} style=${`padding-left:${6 + depth * 14}px`} onClick=${() => setTog({ ...tog, [path]: !isOpen })} onKeyDown=${kbd(() => setTog({ ...tog, [path]: !isOpen }))}>${Ic(isOpen ? "down" : "right", 12)}${Ic("folder", 14)}<span class="t">${d}</span><span class="mute small">${n.dirs[d].n}</span></div>${isOpen && walk(n.dirs[d], path + "/", depth + 1)}`; }),
    ...n.files.sort((a, b) => a.name.localeCompare(b.name)).map((f) => html`<div class=${"tr" + (sel === f.path ? " on" : "")} role="button" tabindex="0" style=${`padding-left:${22 + depth * 14}px`} onClick=${() => onPick(f)} onKeyDown=${kbd(() => onPick(f))} title=${f.path}>${Ic("file", 14)}<span class="t">${f.name}</span><span class="mute small">${num(f.size)}B</span>
      <a class="ibtn" style="width:24px;height:24px" href=${src(f.path) + "?download=1"} download onClick=${(e) => e.stopPropagation()} title="Download" aria-label=${"Download " + f.name}>${Ic("download", 13)}</a></div>`)];
  return html`<div class="ftree">${files.length > 12 && html`<input class="fq" placeholder="Filter files" value=${q} onInput=${(e) => setQ(e.target.value)}/>`}
    ${list.length ? walk(tree, "", 0) : html`<p class="mute small" style="padding:10px">${files.length ? "No file matches." : "No files yet."}</p>`}</div>`;
}
// Drag handle that resizes a panel horizontally: `sign` is +1 when moving right makes the panel wider (left panel), -1 for a panel on the right.
function Grip({ cls, value, onChange, onDone, min, max, sign, reset }) {
  const down = (e) => {
    e.preventDefault(); const x0 = e.clientX, v0 = value(), clamp = (v) => Math.max(min, Math.min(typeof max === "function" ? max() : max, v));
    document.body.classList.add("dragging");
    let last; const mv = (m) => onChange((last = clamp(v0 + sign * (m.clientX - x0))));
    const up = () => { if (last !== undefined && onDone) onDone(last); document.body.classList.remove("dragging"); removeEventListener("pointermove", mv); removeEventListener("pointerup", up); removeEventListener("pointercancel", up); };
    addEventListener("pointermove", mv); addEventListener("pointerup", up); addEventListener("pointercancel", up);
  };
  return html`<div class=${"grip " + cls} role="separator" aria-orientation="vertical" title="Drag to resize (double-click to reset)" onPointerDown=${down} onDblClick=${reset}></div>`;
}
function Editor({ id, path, ver, onSaved, dirtyRef }) {
  const [d, setD] = useState(null), [text, setText] = useState(""), [dirty, setDirty] = useState(false), dr = useRef(false);
  dr.current = dirty; if (dirtyRef) dirtyRef.current = dirty;
  useEffect(() => () => { if (dirtyRef) dirtyRef.current = false; }, []);
  useEffect(() => { if (!dirty) return; const b = (e) => { e.preventDefault(); e.returnValue = ""; }; addEventListener("beforeunload", b); return () => removeEventListener("beforeunload", b); }, [dirty]);
  const fetchIt = () => api(`files/content?id=${id}&path=${encodeURIComponent(path)}`).then((r) => { setD({ ...r, path }); setText(r.text || ""); setDirty(false); }).catch((e) => setD({ err: e.message, path }));
  useEffect(() => {
    if (dr.current && d && d.path === path) return;                        // keep the user's unsaved edits when a turn touches other files
    fetchIt();
  }, [id, path, ver]);
  // the server refuses a save when the file changed on disk since it was read (409 changed): reload it, or overwrite it with the edits
  const save = guard(async (force) => {
    try { const r = await put("files/content", { id, path, text, ...(force === true || !d.mtime ? {} : { mtime: d.mtime }) }); setD((x) => ({ ...x, mtime: r && r.mtime !== undefined ? r.mtime : x.mtime })); setDirty(false); toast("Saved " + baseName(path)); onSaved && onSaved(); }
    catch (e) {
      if (e.status !== 409 && !/changed/i.test(e.message)) throw e;
      const v = await ask({ title: "File changed on disk", text: baseName(path) + " was modified (by Claude or another program) after you opened it.", submit: "Apply", fields: [{ name: "c", label: "What to do", type: "select", value: "reload", options: [{ value: "reload", label: "Reload from disk (my edits are lost)" }, { value: "over", label: "Overwrite the file with my edits" }] }] });
      if (!v) return; if (v.c === "reload") { await fetchIt(); toast("Reloaded " + baseName(path)); } else await save(true);
    }
  });
  if (!d) return html`<div class="empty"><div class="shine">Loading…</div></div>`;
  if (d.err || d.binary || d.big) return html`<div class="empty"><div>${Ic("file", 26)}<div>${d.err || (d.big ? "Too large to edit here (" + num(d.big) + "B)." : "This is a binary file.")}</div></div></div>`;
  return html`<div class="editor"><textarea spellcheck="false" wrap="off" value=${text} onInput=${(e) => { setText(e.target.value); setDirty(true); }}
      onKeyDown=${(e) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); } else if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const t = e.target, a = t.selectionStart; t.setRangeText("  ", a, t.selectionEnd, "end"); setText(t.value); setDirty(true); } }}></textarea>
    <div class="ebar"><span class="mute small grow">${dirty ? "Unsaved changes" : "Saved"} · ${text.split("\n").length} lines</span><button class="btn" disabled=${!dirty} onClick=${() => { setText(d.text || ""); setDirty(false); }}>Revert</button><button class="btn acc" disabled=${!dirty} onClick=${() => save()}>${Ic("check", 13)} Save</button></div></div>`;
}
function lineDiff(a, b) {
  const A = a === "" ? [] : a.split("\n"), B = b === "" ? [] : b.split("\n");
  let s = 0; while (s < A.length && s < B.length && A[s] === B[s]) s++;
  let ea = A.length, eb = B.length; while (ea > s && eb > s && A[ea - 1] === B[eb - 1]) { ea--; eb--; }
  const x = A.slice(s, ea), y = B.slice(s, eb), n = x.length, m = y.length, mid = [];
  if (n * m > 4e6) { x.forEach((t) => mid.push(["-", t])); y.forEach((t) => mid.push(["+", t])); }
  else {
    const w = m + 1, L = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i * w + j] = x[i] === y[j] ? L[(i + 1) * w + j + 1] + 1 : Math.max(L[(i + 1) * w + j], L[i * w + j + 1]);
    let i = 0, j = 0;
    while (i < n && j < m) { if (x[i] === y[j]) { mid.push([" ", x[i]]); i++; j++; } else if (L[(i + 1) * w + j] >= L[i * w + j + 1]) mid.push(["-", x[i++]]); else mid.push(["+", y[j++]]); }
    while (i < n) mid.push(["-", x[i++]]); while (j < m) mid.push(["+", y[j++]]);
  }
  return [...A.slice(0, s).map((t) => [" ", t]), ...mid, ...A.slice(ea).map((t) => [" ", t])];
}
function DiffView({ id, path, ver }) {
  const [base, setBase] = useState("turn"), [d, setD] = useState(null);
  useEffect(() => { setD(null); api(`files/diff?id=${id}&path=${encodeURIComponent(path)}&base=${base}`).then(setD).catch((e) => setD({ err: e.message })); }, [id, path, ver, base]);
  const seg = html`<div class="dbar"><${Seg} small value=${base} onChange=${setBase} items=${[{ id: "turn", label: "Since last message" }, { id: "git", label: "Since last commit" }]}/></div>`;
  if (!d) return html`<div class="diffv">${seg}<div class="empty"><div class="shine">Comparing…</div></div></div>`;
  if (d.err) return html`<div class="diffv">${seg}<div class="empty"><div>${Ic("alert", 24)}<div>${d.err}</div></div></div></div>`;
  if (d.base === null) return html`<div class="diffv">${seg}<div class="empty"><div>${Ic("layers", 26)}<div>${base === "git" ? "This file is not tracked in a git repository here." : "Nothing to compare yet: the snapshot is taken when you send a message."}${d.git && base === "turn" ? " Try “Since last commit”." : d.turn && base === "git" ? " Try “Since last message”." : ""}</div></div></div></div>`;
  const ops = lineDiff(d.base, d.now), changed = ops.map((o, i) => (o[0] !== " " ? i : -1)).filter((i) => i >= 0), add = ops.filter((o) => o[0] === "+").length, del = ops.filter((o) => o[0] === "-").length;
  if (!changed.length) return html`<div class="diffv">${seg}<div class="empty"><div>${Ic("check", 26)}<div>No changes.</div></div></div></div>`;
  const keep = new Set(); changed.forEach((i) => { for (let k = Math.max(0, i - 3); k <= Math.min(ops.length - 1, i + 3); k++) keep.add(k); });
  const rows = []; let o = 1, nn = 1, gap = 0;
  ops.forEach((op, i) => {
    if (keep.has(i)) { if (gap) { rows.push(html`<div class="dl gap">⋯ ${gap} unchanged line${gap > 1 ? "s" : ""}</div>`); gap = 0; }
      rows.push(html`<div class=${"dl " + (op[0] === "+" ? "add" : op[0] === "-" ? "del" : "")}><span class="ln">${op[0] === "+" ? "" : o}</span><span class="ln">${op[0] === "-" ? "" : nn}</span><span class="tx">${op[0]} ${op[1]}</span></div>`); } else gap++;
    if (op[0] !== "+") o++; if (op[0] !== "-") nn++;
  });
  if (gap) rows.push(html`<div class="dl gap">⋯ ${gap} unchanged line${gap > 1 ? "s" : ""}</div>`);
  return html`<div class="diffv">${seg}<div class="dsum"><span class="tag good">+${add}</span> <span class="tag bad">−${del}</span> <span class="mute small">${baseName(path)}</span></div><div class="dlist">${rows}</div></div>`;
}
function WorkDirDialog({ s, cur, busy, close, saved }) {
  const [v, setV] = useState(s.cwd || ""), [pick, setPick] = useState(false), [wait, setWait] = useState(false);
  const apply = guard(async (val) => { setWait(true); try { const r = await post("sessions/update", { id: s.id, cwd: val }); toast(val ? "Working folder set" : "Back to the private folder"); saved(r.session); } finally { setWait(false); } });
  return html`<${Modal} close=${close}><h1>Working folder</h1><p class="sub">Claude reads and writes files in this folder and the Files tab shows it. A folder that does not exist is created, and the chat moves into a project of the same name in the sidebar.</p>
    <div class="field"><label>Folder path</label><div class="row"><input class="grow mono" placeholder="/Users/you/projects/my-app" value=${v} onInput=${(e) => setV(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && v.trim() && apply(v)} autofocus/><button class="btn" onClick=${guard(async () => { const p = await nativePick({ dirs: true, start: v || cur, title: "Choose a working folder" }); if (p === null) setPick(true); else if (p) setV(p); })}>${Ic("folder", 14)} Browse…</button></div>
      <span class="mute small mono" style="overflow-wrap:anywhere">Now: ${cur}</span></div>
    <p class="mute small">Changing it starts a new Claude session: the conversation so far is carried over as text, and the next message rewrites the prompt cache.</p>
    ${busy && html`<p class="tag bad">This chat is answering. Wait for it to finish.</p>`}
    <div class="row" style="margin-top:14px">${s.cwd && html`<button class="btn" disabled=${wait || busy} onClick=${() => apply("")}>Use the private folder</button>`}<span class="grow"></span><button class="btn" onClick=${close}>Cancel</button><button class="btn acc" disabled=${!v.trim() || wait || busy || v.trim() === (s.cwd || "")} onClick=${() => apply(v)}>${wait ? "Applying…" : "Use this folder"}</button></div>
    ${pick && html`<${FilePicker} dirs=${true} title="Choose a working folder" start=${v} onPick=${(p) => { setPick(false); setV(p); }} close=${() => setPick(false)}/>`}<//>`;
}
function SoulDialog({ s, busy, close, saved }) {
  const [t, setT] = useState(s.soul ?? null), [wait, setWait] = useState(false), own = s.soul != null;
  useEffect(() => { if (!own) api("knowledge/note?name=soul").then((d) => setT(d.body)).catch(() => setT("")); }, []);
  const save = guard(async (soul) => { setWait(true); try { const extra = await OUI.languagePrompt(); const r = await post("sessions/update", { id: s.id, soul, extra }); toast(soul === null ? "Using your soul note" : "Soul set for this chat"); saved(r.session); } finally { setWait(false); } });
  return html`<${Modal} wide=${true} close=${close}><h1>Soul for this chat</h1><p class="sub">${own ? "This chat has its own soul. Your soul note (Knowledge) is not used here." : "This chat uses your soul note. Edit the text to give this chat its own persona; the note stays as it is."}</p>
    ${t === null ? html`<div class="shine">Loading…</div>` : html`<textarea rows="12" style="width:100%;font:13px var(--mono)" value=${t} onInput=${(e) => setT(e.target.value)} placeholder="Who should Claude be in this chat?"></textarea>`}
    <p class="mute small">Saving starts a new Claude session (the conversation is carried over as text) and the next message rewrites the prompt cache.</p>
    ${busy && html`<p class="tag bad">This chat is answering. Wait for it to finish.</p>`}
    <div class="row" style="margin-top:12px">${own && html`<button class="btn" disabled=${wait || busy} onClick=${() => save(null)}>Use my soul note</button>`}<span class="grow"></span><button class="btn" onClick=${close}>Cancel</button><button class="btn acc" disabled=${t === null || wait || busy || (own && t === s.soul)} onClick=${() => save(t)}>${wait ? "Saving…" : "Save for this chat"}</button></div><//>`;
}

// ---------- chat
let elSeq = 0;
const winOf = (models, id) => (models.find((m) => m.id === id) || models[0] || {}).window || 200000;
function WorkCard({ r, submit, cancel, toChat, busy }) {
  const ins = r.wk.inputs || [], [v, setV] = useState(() => Object.fromEntries(ins.map((f) => [f.name, f.default ?? (f.type === "checkbox" ? false : "")])));
  const bad = ins.find((f) => f.required && f.type !== "checkbox" && String(v[f.name] ?? "").trim() === "");
  const nodes = Object.entries(r.nodes || {}), done = nodes.filter(([, n]) => n.status === "ok" || n.status === "error").length;
  const fin = r.status === "done" || r.status === "failed";
  return html`<div class="msg"><div class="wrun"><div class="bk-head"><span class="bk-title">${Ic("flow", 14)}<span>${r.wk.title}</span></span>
      ${r.phase === "run" ? html`<span class=${"tag " + (r.status === "done" ? "good" : r.status === "failed" ? "bad" : "")}>${r.status || "starting"}</span>` : html`<button class="ibtn" style="width:24px;height:24px" title="Remove" aria-label="Remove" onClick=${cancel}>${Ic("x", 14)}</button>`}</div>
    ${r.phase === "input" ? html`<form onSubmit=${(e) => { e.preventDefault(); if (!bad) submit(v); }}><div class="fb-grid">${ins.map((f) => html`<label class=${"fb-f" + (f.type === "textarea" ? " wide" : "")}><span>${f.label}${f.required ? " *" : ""}</span>
        ${f.type === "textarea" ? html`<textarea rows="3" value=${v[f.name]} onInput=${(e) => setV({ ...v, [f.name]: e.target.value })}></textarea>`
        : f.type === "select" ? html`<select value=${v[f.name]} onChange=${(e) => setV({ ...v, [f.name]: e.target.value })}>${f.options.map((o) => html`<option>${o}</option>`)}</select>`
        : f.type === "checkbox" ? html`<input type="checkbox" checked=${!!v[f.name]} onChange=${(e) => setV({ ...v, [f.name]: e.target.checked })}/>`
        : html`<input type=${f.type === "number" ? "number" : "text"} value=${v[f.name]} onInput=${(e) => setV({ ...v, [f.name]: e.target.value })}/>`}</label>`)}</div>
      <div class="fb-foot"><button class="btn pri" type="submit" disabled=${!!bad}>${Ic("play", 13)} Run</button></div></form>`
    : html`<div class="wrun-b">${!fin && html`<div class="wsteps">${nodes.map(([id, n]) => html`<span class=${"wstep " + n.status}>${n.status === "ok" ? Ic("check", 11) : n.status === "error" ? Ic("x", 11) : html`<i></i>`}${(r.labels && r.labels[id]) || id}</span>`)}</div><span class="shine">Running the work… ${done}/${nodes.length || "?"}</span>`}
      ${r.status === "failed" && html`<div class="werr">${Ic("alert", 13)} ${r.err || "The work failed."}</div>`}
      ${r.result && html`<div class="wres">${r.result.title && html`<h3>${r.result.title}</h3>`}<${Md} text=${r.result.text}/></div>`}
      ${fin && !r.result && r.status === "done" && html`<span class="mute small">The work finished without text output. Add an Output node to choose what it answers.</span>`}</div>
      <div class="wfoot">${r.result && html`<button class="btn" onClick=${() => navigator.clipboard.writeText(r.result.text).then(() => toast("Copied"))}>${Ic("copy", 13)} Copy</button><button class="btn" disabled=${busy} onClick=${() => toChat("Result of the work \u201c" + r.wk.title + "\u201d:\n\n" + r.result.text)}>${Ic("send", 13)} Send to Claude</button>`}
        ${r.chat && html`<a class="small" href=${"#/chat/" + r.chat}>Run's chat and files ${Ic("right", 12)}</a>`}<span class="grow"></span><a class="small" href="#/works">Open in Works</a></div>`}</div></div>`;
}
const DIRECT_PROMPT = "Stop using tools and stop exploring. Answer my last question directly and concisely right now, using only what you already know or have found so far.";
const words = (t) => (String(t).trim().match(/\S+/g) || []).length;
const fmtT = (sec) => (sec < 60 ? sec + "s" : Math.floor(sec / 60) + "m " + (sec % 60) + "s");
// What the assistant is doing right now: phase, a live step list (tools and their results), reasoning as it streams, counters, and the Stop / Answer now buttons.
function Working({ act, since, live, steps, ctx, win, free, onStop, onDirect }) {
  const [n, setN] = useState(0), [openR, setOpenR] = useState(true), rb = useRef(), w0 = useRef(0);
  useEffect(() => { const t = setInterval(() => setN((x) => x + 1), 1000); return () => clearInterval(t); }, []);
  useEffect(() => { if (rb.current) rb.current.scrollTop = rb.current.scrollHeight; }, [live.think.length]);
  const sec = Math.max(0, Math.round((Date.now() - since) / 1000)), writing = live.text.length > 0;
  if (!writing) w0.current = 0; else if (!w0.current) w0.current = Date.now();
  const wsec = writing ? (Date.now() - w0.current) / 1000 : 0, speed = wsec >= 2 ? Math.round(live.text.length / 4 / wsec) : 0;
  const phase = writing ? "write" : act && act.k === "tool" ? "tool" : "think";
  const label = phase === "write" ? "Writing the answer" : phase === "tool" ? "Running " + act.name : live.think ? "Reasoning" : "Thinking";
  const done = steps.filter((x) => x.state !== "run").length, shown = steps.slice(-6), pct = win ? Math.min(100, Math.round((ctx / win) * 100)) : 0;
  return html`<div class=${"work2 p-" + phase}><i class="wbar"></i>
    <div class="w-head"><span class="spark">${Ic("sparkles", 16)}</span><b class="shine">${label}…</b><span class="w-time">${fmtT(sec)}</span><span class="grow"></span>
      ${(!free || steps.length > 0) && html`<button class="btn sm" title="Stop what it is doing and get a short answer from what it has so far" onClick=${onDirect}>${Ic("zap", 13)} Answer now</button>`}<button class="btn sm" onClick=${onStop}>${Ic("stop", 12)} Stop</button></div>
    <div class="w-phases"><span class=${"ph" + (phase === "think" ? " on" : live.think || steps.length || writing ? " done" : "")}>${Ic("sparkles", 12)} Reasoning</span>
      ${(!free || steps.length > 0) && html`<span class=${"ph" + (phase === "tool" ? " on" : steps.length ? " done" : "")}>${Ic("terminal", 12)} Tools${steps.length ? " · " + steps.length : ""}</span>`}
      <span class=${"ph" + (phase === "write" ? " on" : "")}>${Ic("edit", 12)} Answer</span></div>
    ${shown.length > 0 && html`<div class="w-steps">${steps.length > shown.length && html`<div class="mute small">+ ${steps.length - shown.length} earlier step${steps.length - shown.length > 1 ? "s" : ""}</div>`}
      ${shown.map((x) => html`<div class=${"w-step " + x.state}><span class="wi">${x.state === "ok" ? Ic("check", 13) : x.state === "bad" ? Ic("x", 13) : html`<i class="spin"></i>`}</span><b>${x.name}</b><span class="mute">${x.hint}</span></div>`)}</div>`}
    ${live.think && html`<div class="w-think"><button type="button" class="w-tt lnk" aria-expanded=${openR} onClick=${() => setOpenR(!openR)}>${Ic(openR ? "down" : "right", 12)} Reasoning <span class="mute">${words(live.think)} words</span></button>${openR && html`<div class="w-tb" ref=${rb}>${live.think.slice(-1400)}</div>`}</div>`}
    <div class="w-stats"><span>${Ic("clock", 12)} ${fmtT(sec)}</span>${(!free || steps.length > 0) && html`<span>${Ic("terminal", 12)} ${done}/${steps.length} step${steps.length === 1 ? "" : "s"}</span>`}${live.think && html`<span>${Ic("sparkles", 12)} ${num(live.think.length / 4)} reasoning tokens (est.)</span>`}${writing && html`<span>${Ic("edit", 12)} ${num(live.text.length / 4)} tokens written (est.)${speed ? " · ~" + num(speed) + " tok/s" : ""}</span>`}${ctx > 0 && html`<span>${Ic("layers", 12)} context ${num(ctx)}${win ? " · " + pct + "%" : ""}</span>`}</div></div>`;
}
// /create-work (/workflow) in a chat: Claude designs the work, which takes a minute; this shows it is on its way
function GenBar({ gen }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  const sec = Math.max(0, Math.round((now - gen.since) / 1000)), step = sec < 8 ? "Reading the request" : sec < 30 ? "Designing nodes and data flow" : sec < 60 ? "Checking and repairing the work" : "Almost there";
  return html`<div class="genbar" role="status" aria-live="polite"><div class="genbar-h">${Ic("flow", 15)}<b>Creating the workflow</b><span class="mute small grow">${gen.text.slice(0, 90)}</span><span class="mute small">${sec}s</span></div>
    <div class="genbar-track"><i></i></div><div class="mute small">${step}…</div></div>`;
}

// One shell inside a terminal tab. It stays mounted while another tab (or a hidden panel) is in front, so nothing running is lost.
function TermPane({ id, active, onEnd, regStop }) {
  const host = useRef(null), tm = useRef(null), [n, setN] = useState(0), [ended, setEnded] = useState(null);
  useEffect(() => {
    let gone = false; setEnded(null);
    (async () => { try { const x = await runInstall(host.current, { shell: true, session: id }, { post, api }, (code) => { if (!gone) { setEnded(code); onEnd(code); } }); if (gone) x.stop(); else tm.current = x; } catch (e) { if (!gone) { toast(e.message || "Cannot open a terminal"); setEnded(-1); onEnd(-1); } } })();
    return () => { gone = true; if (tm.current) { tm.current.stop(); tm.current = null; } };
  }, [id, n]);
  return html`<div class=${"ct-pane" + (active ? " on" : "")}><div class="ct-b" ref=${host}></div>${ended != null && html`<div class="ct-end"><span>Process exited (${ended})</span><button class="btn" onClick=${() => setN(n + 1)}>Restart</button></div>`}</div>`;
}

// Terminal panel like VS Code's: several shells as tabs, a "+" to open another, full screen, hide (shells keep running) and per-tab kill.
let termSeq = 0;
function ChatTerm({ id, show, hide }) {
  const root = useRef(null), mk = () => ({ k: ++termSeq, no: 0 });
  const [tabs, setTabs] = useState(() => { const t = mk(); t.no = 1; return [t]; }), [act, setAct] = useState(() => tabs[0].k), [full, setFull] = useState(false), [h, setH] = useState(() => +pref("termh", "280") || 280), [ex, setEx] = useState({});
  const add = () => setTabs((ts) => { if (ts.length >= 8) { toast("At most 8 terminals per chat"); return ts; } const t = mk(); t.no = Math.max(0, ...ts.map((x) => x.no)) + 1; setAct(t.k); return [...ts, t]; });
  const kill = (k) => setTabs((ts) => { const i = ts.findIndex((x) => x.k === k), rest = ts.filter((x) => x.k !== k); if (!rest.length) { const t = mk(); t.no = 1; setAct(t.k); setTimeout(hide, 0); return [t]; } if (act === k) setAct(rest[Math.min(i, rest.length - 1)].k); return rest; });
  const toggleFull = () => {
    const el = root.current;
    if (!full) { setFull(true); try { el.requestFullscreen && el.requestFullscreen().catch(() => {}); } catch (e) {} }
    else { setFull(false); try { document.fullscreenElement && document.exitFullscreen(); } catch (e) {} }
  };
  useEffect(() => { const f = () => { if (!document.fullscreenElement && root.current && !root.current.classList.contains("ct-fixed")) setFull(false); }; document.addEventListener("fullscreenchange", f); return () => { document.removeEventListener("fullscreenchange", f); try { document.fullscreenElement && document.exitFullscreen(); } catch (e) {} }; }, []);
  useEffect(() => { setPref("termh", String(h)); }, [h]);
  const drag = (e) => { e.preventDefault(); const y0 = e.clientY, h0 = h; document.body.classList.add("dragging-y"); const mv = (m) => setH(Math.max(120, Math.min(innerHeight - 220, h0 + y0 - m.clientY))); const up = () => { document.body.classList.remove("dragging-y"); removeEventListener("pointermove", mv); removeEventListener("pointerup", up); removeEventListener("pointercancel", up); }; addEventListener("pointermove", mv); addEventListener("pointerup", up); addEventListener("pointercancel", up); };
  return html`<div ref=${root} class=${"chatterm" + (full ? " ct-full" : "") + (show ? "" : " ct-off")} style=${full ? "" : "height:" + h + "px"}>
    ${!full && html`<div class="ct-grip" onPointerDown=${drag}></div>`}
    <div class="ct-h">${Ic("terminal", 14)}<div class="ct-tabs" role="tablist">${tabs.map((t) => html`<div key=${t.k} role="tab" aria-selected=${act === t.k} class=${"ct-tab" + (act === t.k ? " on" : "") + (ex[t.k] != null ? " dead" : "")} onClick=${() => setAct(t.k)} title=${ex[t.k] != null ? "Exited (" + ex[t.k] + ")" : "Terminal " + t.no}><span>Terminal ${t.no}</span><button class="ct-x" title="Kill this terminal" aria-label=${"Kill terminal " + t.no} onClick=${(e) => { e.stopPropagation(); kill(t.k); }}>${Ic("x", 11)}</button></div>`)}</div>
      <button class="ibtn" title="New terminal" aria-label="New terminal" onClick=${add}>${Ic("plus", 14)}</button><span class="grow" style="flex:1"></span>
      <button class="ibtn" title=${full ? "Exit full screen" : "Full screen"} aria-label=${full ? "Exit full screen" : "Full screen"} aria-pressed=${full} onClick=${toggleFull}>${Ic(full ? "shrink" : "expand", 14)}</button>
      <button class="ibtn" title="Hide the panel (terminals keep running)" aria-label="Hide the panel" onClick=${() => { if (full) toggleFull(); hide(); }}>${Ic("x", 14)}</button></div>
    <div class="ct-panes">${tabs.map((t) => html`<${TermPane} key=${t.k} id=${id} active=${act === t.k} onEnd=${(c) => setEx((x) => ({ ...x, [t.k]: c }))}/>`)}</div></div>`;
}

function ToolRow({ b, res, hint }) {
  const [open, setOpen] = useState(false);                                // the JSON is only built while the row is open
  return html`<details class="tool" onToggle=${(e) => setOpen(e.currentTarget.open)}><summary>${Ic("terminal", 14)}<b>${b.name}</b> <span class="mute" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${hint}</span>${res && res.error ? html`<span class="tag bad">error</span>` : ""}</summary>${open && html`<pre>${JSON.stringify(b.input, null, 2).slice(0, 3000)}${res ? "\n— result —\n" + res.text : ""}</pre>`}</details>`;
}
// One poller per generate job, alive until the job ends or is cancelled: a chat switch only re-attaches the progress bar, so the result is announced once.
const genJobs = new Map();
function pollGen(job, text, ui) {
  let j = genJobs.get(job.id);
  if (j) { j.ui = ui; ui({ text: text || job.prompt || "", since: job.since || Date.now() }); return j; }
  j = { ui, on: true, cancel() { this.on = false; } }; genJobs.set(job.id, j); ui({ text: text || job.prompt || "", since: job.since || Date.now() });
  (async () => {
    try {
      let bad = 0;
      while (j.on) {
        await new Promise((r) => setTimeout(r, 2000)); if (!j.on) return;
        let d; try { d = (await api("works/generate/status?id=" + job.id)).job; bad = 0; } catch (x) { if (++bad >= 5) throw x; continue; }
        if (d.state === "done") { toast("Created " + d.work.title + " (switched off)"); location.hash = "#/works/" + d.work.id; return; }
        if (d.state === "failed") { toast(d.error || "Could not create the workflow"); return; }
      }
    } catch (x) { toast(x.message || String(x)); }
    finally { genJobs.delete(job.id); if (j.ui) j.ui(null); try { localStorage.removeItem("aihub_genjob"); } catch (x) {} }
  })();
  return j;
}

function Chat({ id, boot, refresh, newChat, prefs, savePrefs, slash, onMenu, onBusy, onImport }) {
  const [s, setS] = useState(null), [hist, setHist] = useState([]), [files, setFiles] = useState([]), [sig, setSig] = useState("");
  const [live, setLive] = useState({ text: "", think: "" }), [busy, setBusy] = useState(false), [text, setText] = useState("");
  const [model, setModel] = useState(pref("model", "")), [effort, setEffort] = useState(pref("effort", "")), [mode, setMode] = useState(pref("mode", "auto"));
  const [ctx, setCtx] = useState(0), [pop, setPop] = useState(null), [sidx, setSidx] = useState(0), [signin, setSignin] = useState(false);
  const [fresh, setFresh] = useState(null);                                           // model list re-read from settings.json (boot is read once)
  const [ment, setMent] = useState(null), [gen, setGen] = useState(null);                 // @ menu data (loaded on first @), /workflow being designed
  useEffect(() => setMent(null), [id]);
  const [att, setAtt] = useState([]), [tab, setTab] = useState("files"), [open, setOpen] = useState(false), [prev, setPrev] = useState(""), [els, setEls] = useState([]);
  const [engine, setEngine] = useState(pref("engine", "claude")), busyRef = useRef(false), engineNow = useRef("claude"), [mcpAll, setMcpAll] = useState(null), [freeIds, setFreeIds] = useState(null);
  const [split, setSplit] = useState(pref("split", "0") === "1"), [dlg, setDlg] = useState(null), [hintOff, setHintOff] = useState(false), [term, setTerm] = useState(false), [termOn, setTermOn] = useState(false), [cw, setCw] = useState(() => +pref("canvasw", "0") || 0), [rail, setRail] = useState(pref("edrail", "1") === "1"), [ew, setEw] = useState(() => +pref("edw", "200") || 200), dirtyRef = useRef(false), [dirInfo, setDirInfo] = useState({ dir: "", def: "" });
  const [themes, setThemes] = useState([]), [since, setSince] = useState(0), [act, setAct] = useState(null), [wr, setWr] = useState([]), [works, setWorks] = useState([]);
  const end = useRef(), ctl = useRef(), ta = useRef(), sendRef = useRef(), sRef = useRef(null), pendingRef = useRef([]), stick = useRef(true), msgsRef = useRef(), actRef = useRef(null), alive = useRef(true), prevRef = useRef(""), fileIn = useRef(), [stuck, setStuck] = useState(true), [missing, setMissing] = useState(""), [dismissed, setDismissed] = useState(null);
  sRef.current = s;
  useEffect(() => () => { alive.current = false; }, []);
  const load = async () => {
    let d; try { d = await api("sessions/get?id=" + id); } catch (x) { if (sRef.current) toast(x.message); else setMissing(x.message || "Chat not found"); return; }
    setMissing(""); setS(d.session); if (d.session.started || d.session.engine === "free") setEngine(d.session.engine === "free" ? "free" : "claude"); setDirInfo({ dir: d.dir, def: d.default_dir }); setHist(d.history); setFiles(d.files); setSig(d.sig); setBusy(d.busy); setLive({ text: "", think: "" }); setCtx(d.session.ctx || 0);
    if (d.session.model !== undefined && d.session.started) setModel(d.session.model);
    if (d.session.started) setEffort(d.session.effort || "");
    setOpen(false); setPrev(""); setEls([]);
    if (d.busy) follow((on, sig) => stream("chat/attach", { id }, on, sig), d.since);          // a turn is still running here: replay it, then go live
  };
  useEffect(() => { load(); api("works").then((d) => setWorks(d.works)).catch(() => {}); api("themes").then((d) => setThemes(d.themes)).catch(() => {}); return () => ctl.current && ctl.current.abort(); }, [id]);
  useEffect(() => { setHintOff(false); setTermOn(term); }, [id]);
  // stick to the bottom only while the reader is at the bottom; scrolling up lets go, "Jump to latest" or sending takes it back
  const toEnd = (smooth) => { const m = msgsRef.current; if (m) m.scrollTo({ top: m.scrollHeight, behavior: smooth ? "smooth" : "instant" }); };
  const onScroll = () => { const m = msgsRef.current; if (!m) return; const near = m.scrollHeight - m.scrollTop - m.clientHeight < 80; stick.current = near; setStuck(near); };
  const jump = () => { stick.current = true; setStuck(true); toEnd(true); };
  useEffect(() => { if (stick.current) toEnd(hist.length > 1); }, [hist.length]);
  useEffect(() => { if (!stick.current) return; const r = requestAnimationFrame(() => toEnd(false)); return () => cancelAnimationFrame(r); }, [live.text.length, live.think.length, busy]);
  useEffect(() => setPref("split", split ? "1" : "0"), [split]);
  useEffect(() => setPref("engine", engine), [engine]);
  useEffect(() => { setPref("model", model); setPref("effort", effort); setPref("mode", mode); }, [model, effort, mode]);

  const win = prefs.window || winOf(boot.models.models, model), relPath = (p) => { p = (p || "").replace(/\\/g, "/"); const d = (dirInfo.dir || "").replace(/\\/g, "/").replace(/\/+$/, ""); if (d && p.startsWith(d + "/")) return p.slice(d.length + 1); const m = p.match(/sessions\/[0-9a-f]{8}\/(.+)$/); return m ? m[1] : null; };
  const showEl = (e) => { setEls((l) => [...l, { ...e, id: ++elSeq }].slice(-8)); setTab("elements"); setOpen(true); };
  const sm = /^\/([\w:.-]*)$/.exec(text), pool = sm ? [...UI_CMDS.map((c) => ({ ...c, source: "ui" })), ...works.map((x) => ({ name: "work:" + x.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""), description: "Run work: " + x.title, source: "work", wid: x.id })), ...slash.packages, ...slash.claude] : [];
  const items = sm ? pool.filter((c) => c.name.toLowerCase().includes(sm[1].toLowerCase())).sort((a, b) => (b.name.toLowerCase().startsWith(sm[1].toLowerCase()) ? 1 : 0) - (a.name.toLowerCase().startsWith(sm[1].toLowerCase()) ? 1 : 0)).slice(0, 40) : [];
  const mm = !sm && /(?:^|\s)@([\w:.@\/-]*)$/.exec(text), mq = mm ? mm[1].toLowerCase() : "";
  // @ menu: themes, other chats (@chat:<id>, add :N for its last N messages) and tools (@tool:<name>: skills, commands, MCP servers)
  useEffect(() => { if (!mm || ment) return; setMent({ chats: [], tools: [] }); let on = true;
    Promise.all([api("sessions").catch(() => ({ sessions: [] })), api("mcp").catch(() => ({ packages: [], claude: [], ui: [] }))]).then(([ss, mc]) => on && setMent({
      chats: (ss.sessions || []).filter((x) => x.id !== id).map((x) => ({ name: "chat:" + x.id, description: (x.title || "Chat") + " · its last 40 messages; type :N after it for another limit", source: "chat", key: (x.title || "").toLowerCase() })),
      tools: [...new Map([...[...(mc.packages || []), ...(mc.claude || []), ...(mc.ui || [])].map((x) => [x.name, "MCP server"]), ...[...slash.packages, ...slash.claude].map((x) => [x.name, x.description || x.source])]).entries()]
        .map(([n, d]) => ({ name: "tool:" + n, description: String(d || "tool"), source: "tool", key: n.toLowerCase() })) })); return () => { on = false; }; }, [!!mm]);
  const [pfiles, setPfiles] = useState(null), [pickF, setPickF] = useState(false);          // files of the working folder for @ (loaded on first @), "choose a file" dialog
  useEffect(() => setPfiles(null), [id]);
  useEffect(() => { if (mm && pfiles === null) { setPfiles([]); api("files?id=" + id).then((d) => setPfiles((d.files || []).map((f) => f.path))).catch(() => {}); } }, [!!mm, id]);
  const [srcs, setSrcs] = useState([]);
  useEffect(() => { if (!mm) { setSrcs([]); return; } let on = true; const t = setTimeout(() => api("works/sources?nodes=1&q=" + encodeURIComponent(mm[1])).then((d) => on && setSrcs(d.items || [])).catch(() => on && setSrcs([])), 150); return () => { on = false; clearTimeout(t); }; }, [mm && mm[1]]);
  const fileItems = mm && pfiles ? pfiles.filter((p) => p.toLowerCase().includes(mq.replace(/^file:/, ""))).sort((a, b) => (b.split("/").pop().toLowerCase().startsWith(mq.replace(/^file:/, "")) ? 1 : 0) - (a.split("/").pop().toLowerCase().startsWith(mq.replace(/^file:/, "")) ? 1 : 0)).slice(0, 8).map((p) => ({ name: "file:" + p, description: "file in the working folder", source: "file", key: "" })) : [];
  const fq = mq.replace(/^folder:/, ""), dirItems = mm && pfiles ? [...new Set(pfiles.flatMap((p) => { const a = p.split("/").slice(0, -1); return a.map((_, i) => a.slice(0, i + 1).join("/")); }))].filter((d) => d.toLowerCase().includes(fq)).sort((a, b) => (b.split("/").pop().toLowerCase().startsWith(fq) ? 1 : 0) - (a.split("/").pop().toLowerCase().startsWith(fq) ? 1 : 0) || a.length - b.length).slice(0, 5).map((d) => ({ name: "folder:" + d, description: "folder in the working folder (its file list is shared)", source: "folder", key: "" })) : [];
  const mitems = mm ? [...fileItems, ...dirItems, ...srcs, ...themes.map((t) => ({ name: "theme:" + t.id, description: t.name + " - colors " + t.colors.accent + ", fonts " + t.fonts.head.split(",")[0], source: "theme", sw: t.colors, key: t.name.toLowerCase() })),
    ...(ment ? [...ment.chats, ...ment.tools] : [])].filter((c) => (c.name + " " + c.key).toLowerCase().includes(mq)).slice(0, 30) : [];
  const menuItems = dismissed === text ? [] : sm ? items : mitems;
  useEffect(() => setSidx(0), [sm && sm[1], mm && mm[1]]);

  const runUi = (a) => { setText(""); if (a === "clear") newChat(); else if (a === "canvas") setOpen((o) => !o); else if (a === "mcp") openMcp(); else if (a === "engine") setPop("engine"); else if (a === "ctx" || a === "model" || a === "effort" || a === "mode") setPop(a); };
  const openMcp = () => { setDlg("mcp"); api("mcp/free").then((d) => setMcpAll(d.servers)).catch(() => setMcpAll([])); };
  const setEng = (v) => { setEngine(v); post("sessions/update", { id, engine: v }).catch(() => {}); };
  const saveMcp = (list) => post("sessions/update", { id, mcp: list }).then((r) => setS(r.session)).catch((e) => toast(e.message));
  const pickSlash = (c) => { if (["theme", "chat", "tool", "space", "knowledge", "file", "folder", "node", "work"].includes(c.source)) { setText(text.replace(/@[\w:.@\/-]*$/, "@" + c.name + " ")); ta.current && ta.current.focus(); return; } if (c.source === "ui" && c.action !== "send" && c.action !== "workflow") return runUi(c.action); setText("/" + c.name + " "); ta.current && ta.current.focus(); };

  const upWr = (key, p) => setWr((l) => l.map((x) => (x.key === key ? { ...x, ...p } : x)));
  const startWork = guard(async (key, wk, data, text) => {
    upWr(key, { phase: "run", status: "queued", nodes: {}, data });
    const [w, r] = await Promise.all([api("works/get?id=" + wk.id).then((d) => d.work), post("works/run", { id: wk.id, text, data })]);
    if (!r.run) return upWr(key, { phase: "run", status: "failed", err: "The work did not start (it has no Start node)." });
    upWr(key, { labels: Object.fromEntries(w.nodes.map((n) => [n.id, n.label || n.type])) });
    for (let i = 0; i < 800 && alive.current; i++) {
      await new Promise((ok) => setTimeout(ok, 1200)); if (!alive.current) break;
      const d = (await api(`works/run?id=${wk.id}&run=${r.run}`)).run;
      upWr(key, { status: d.status, nodes: d.nodes || {}, result: d.result, err: (Object.values(d.nodes || {}).find((n) => n.error) || {}).error, chat: d.chat });
      if (d.status === "done" || d.status === "failed") break;
    }
  });
  const runWork = guard(async (wk, input) => {
    setText(""); const key = Math.random().toString(36).slice(2);
    setWr((l) => [...l, { key, wk, phase: wk.inputs && wk.inputs.length ? "input" : "run", text: input || "" }]);
    if (!(wk.inputs && wk.inputs.length)) startWork(key, wk, {}, input || "run from chat");
  });
  // The design runs on the server as a job (one poller per job id, see pollGen); this chat only shows its progress while it is open.
  const genWork = guard(async (t) => { setText(""); setGen({ text: t, since: Date.now() });
    try { const job = (await post("works/generate/start", { prompt: t })).job; try { localStorage.setItem("aihub_genjob", job.id); } catch (e) {} pollGen(job, t, setGen); } catch (e) { setGen(null); throw e; } });
  useEffect(() => { let jid = ""; try { jid = localStorage.getItem("aihub_genjob") || ""; } catch (e) {}
    if (jid) api("works/generate/status?id=" + jid).then((r) => { if (r.job.state === "running") pollGen(r.job, undefined, setGen); else { try { localStorage.removeItem("aihub_genjob"); } catch (e) {} if (r.job.state === "done") { toast("Created " + r.job.work.title + " (switched off)"); location.hash = "#/works/" + r.job.work.id; } else toast(r.job.error || "Could not create the workflow"); } }).catch(() => { try { localStorage.removeItem("aihub_genjob"); } catch (e) {} });
    return () => { for (const j of genJobs.values()) if (j.ui === setGen) j.ui = null; }; }, []);
  // send() decides at once: a card result (override text) arriving mid-answer is queued and goes out when the turn ends. Returns false when nothing was sent or queued.
  const send = (override, opts = {}) => {
    const t0 = String(override !== undefined ? override : text).trim(); if (!t0) return false;
    if (busyRef.current) { if (override === undefined) return false; pendingRef.current.push(t0); toast("Queued: sent when the answer ends"); return true; }
    doSend(override, opts); return true;
  };
  const doSend = guard(async (override, opts = {}) => {
    let t = (override !== undefined ? override : text).trim();
    if (!t || busyRef.current) return;
    if (engine === "free" && /^\/compact\b/.test(t)) return toast("Free chat has no compaction: switch to Claude Code, or start a new chat.");
    const m = override === undefined && /^\/([\w:.-]+)\s*([\s\S]*)$/.exec(t);
    if (m) { const ui = UI_CMDS.find((c) => c.name === m[1]); if (m[1] === "workflow" || m[1] === "create-work") { if (!m[2]) return toast("Describe the work after /create-work, for example: /create-work pull data from git, build a dashboard, publish it as a space"); return genWork(m[2]); } if (m[1] === "engine" && m[2]) { const w = m[2].trim().toLowerCase(), e = /^(free|api|light)/.test(w) ? "free" : /^(claude|code|cc)/.test(w) ? "claude" : ""; if (!e) return toast("Use /engine claude or /engine free"); if (busyRef.current) return toast("Wait for the answer to finish first"); setText(""); setEng(e); return toast("Engine: " + (e === "free" ? "Free chat" : "Claude Code")); } if (ui && ui.action !== "send" && !m[2]) return runUi(ui.action); const wk = works.find((x) => "work:" + x.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") === m[1]); if (wk) return runWork(wk, m[2]); const pk = slash.packages.find((c) => c.name === m[1]); if (pk) t = pk.prompt.replace(/\$ARGUMENTS/g, m[2]); }
    if (mode === "bypass" && engine !== "free" && !confirm("Bypass checks lets Claude run any tool without asking. Continue?")) return;
    const typed = override === undefined, atts = typed ? att : [], draft = text;       // the draft comes back if the server never starts the turn
    if (typed) { setText(""); setAtt([]); if (ta.current) ta.current.style.height = "auto"; }
    stick.current = true; setStuck(true);
    const row = { k: "user", text: t, files: atts.map((a) => "attachments/" + a.name) };
    setHist((h) => [...h, row]);
    await follow((on, sig) => stream("chat", { id, text: t, model, effort: opts.effort !== undefined ? opts.effort : effort, mode, engine, attachments: atts.map((a) => ({ name: a.name, data: a.data })) }, on, sig), Date.now(),
      { fail: () => { setHist((h) => h.filter((x) => x !== row)); if (typed) { setText((c) => c || draft); setAtt((a) => (a.length ? a : atts)); } } });
  });
  // Follow a turn that runs on the server. Leaving the chat only closes this listener: the answer keeps running there and is saved.
  const setActK = (a) => { const p = actRef.current; if (p && p.k === a.k && p.name === a.name) return; actRef.current = a; setAct(a); };      // only a change of kind re-renders
  const follow = async (open, t0, hooks) => {
    busyRef.current = true; setBusy(true); onBusy && onBusy(id, true); setTimeout(refresh, 1200); setPop(null); setSince(t0 || Date.now()); actRef.current = null; setActK({ k: "think" }); setLive({ text: "", think: "" });
    if (ctl.current) ctl.current.abort();
    const mine = (ctl.current = new AbortController());
    let lastText = "", got = false;
    const buf = { text: "", think: "", raf: 0 };                                        // stream deltas collect here and reach the page once per animation frame
    const flush = () => { buf.raf = 0; const a = buf.text, b = buf.think; if (!a && !b) return; buf.text = buf.think = ""; setLive((l) => ({ text: l.text + a, think: l.think + b })); };
    const drop = () => { if (buf.raf) cancelAnimationFrame(buf.raf); buf.raf = 0; buf.text = buf.think = ""; };
    try {
      await open((ev) => {
        got = true;
        if (ev.k === "delta") { setActK({ k: "write" }); buf.text += ev.text; if (!buf.raf) buf.raf = requestAnimationFrame(flush); }
        else if (ev.k === "think") { setActK({ k: "think" }); buf.think += ev.text; if (!buf.raf) buf.raf = requestAnimationFrame(flush); }
        else if (ev.k === "ctx") setCtx(ev.used);
        else if (ev.k === "msg") { drop(); setLive({ text: "", think: "" }); setHist((h) => [...h, ev]); lastText = ev.blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n"); const tl = ev.blocks.filter((b) => b.type === "tool").pop(); if (tl) setActK({ k: "tool", name: tl.name, hint: toolHint(tl) }); }
        else if (ev.k === "files") {                                           // a report or page was made: show it on the canvas
          setHist((h) => [...h, ev]); refreshFiles();
          const f = ev.items.find((x) => /\.(html?|svg)$/i.test(x.path)) || ev.items.find((x) => /\.md$/i.test(x.path)); if (f && engineNow.current === "free" && !dirtyRef.current) { setPrev(f.path); setTab("preview"); setOpen(true); }
        }
        else if (ev.k === "tool_result" || ev.k === "result" || ev.k === "compact" || ev.k === "note") setHist((h) => [...h, ev]);
        else if (ev.k === "error") { setHist((h) => [...h, ev]); if (ev.auth) setSignin(true); }
        else if (ev.k === "file") {
          refreshFiles();
          const r = relPath(ev.path); if (r && /\.(html?|svg|md)$/i.test(r) && !dirtyRef.current) { setPrev(r); setTab("preview"); setOpen(true); }
        }
      }, mine.signal);
    } catch (e) { if (e.name !== "AbortError") { toast(e.message); if (!got && hooks && hooks.fail) hooks.fail(); } }
    drop();
    if (mine.signal.aborted) return;                                       // this page moved on (other chat, or a newer follow)
    busyRef.current = false; setBusy(false); onBusy && onBusy(id, false); actRef.current = null; setAct(null); setLive({ text: "", think: "" }); refresh();
    api("sessions/get?id=" + id).then((d) => setHist(d.history)).catch(() => {});             // same rows as the saved history, so delete-by-position lines up
    refreshFiles();
    const o = lastText.match(/```openui\n([\s\S]*?)```/); if (o) showEl({ lang: "openui", code: o[1] });
    const q = pendingRef.current.splice(0); if (q.length) setTimeout(() => sendRef.current(q.join("\n\n")), 0);      // results of cards that finished while the answer was still running
  };
  sendRef.current = send; busyRef.current = busy; engineNow.current = engine; prevRef.current = prev;
  const answerNow = guard(async () => {                                   // stop the harness and ask for a short direct answer in the same Claude session
    await post("chat/stop", { id });
    for (let i = 0; i < 60 && busyRef.current; i++) await new Promise((ok) => setTimeout(ok, 250));
    if (busyRef.current) return toast("Still stopping… try Answer now again in a moment");
    await sendRef.current(DIRECT_PROMPT, { effort: "low" });
  });
  const stop = guard(async () => { await post("chat/stop", { id }); });                // the stream then ends by itself with `done`
  const pick = (fl) => {
    fl = [...fl]; const have = att.reduce((n, a) => n + (a.size || 0), 0), add = fl.reduce((n, f) => n + f.size, 0);
    if (have + add > 25 * 1024 * 1024) return toast("Attachments are limited to 25 MB in total (" + Math.round((have + add) / 1048576) + " MB selected)");
    fl.forEach((f) => { const r = new FileReader(); r.onload = () => setAtt((a) => [...a, { name: f.name, size: f.size, data: r.result.split(",")[1], type: f.type, url: r.result }]); r.readAsDataURL(f); });
  };
  const onKey = (e) => {
    if (menuItems.length) { if (e.key === "ArrowDown") { e.preventDefault(); setSidx((i) => (i + 1) % menuItems.length); return; } if (e.key === "ArrowUp") { e.preventDefault(); setSidx((i) => (i + menuItems.length - 1) % menuItems.length); return; }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); pickSlash(menuItems[sidx]); return; } if (e.key === "Escape") { e.preventDefault(); setDismissed(text); return; } }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  };
  const delMsg = guard(async (i) => {
    if (busy) return;
    const v = await ask({ title: "Delete this message?", text: "It is removed from this page and the saved history. Claude's own memory of the chat is not edited.", submit: "Delete", danger: true });
    if (!v) return; await post("sessions/delmsg", { id, index: i }); setHist((h) => h.filter((_, j) => j !== i));
  });
  const copyMsg = (e) => { const t = e.k === "user" ? e.text : e.blocks.filter((b) => b.type === "text").map((b) => b.text).join("\n\n"); navigator.clipboard.writeText(t).then(() => toast("Copied"), () => toast("Copy failed")); };
  const branchFrom = guard(async (i) => {
    const v = await ask({ title: "Branch from here?", text: "A new chat gets this conversation up to this message and a copy of the working folder. This chat stays as it is.", submit: "Create branch" });
    if (!v) return; const r = await post("sessions/branch", { id, index: i }); refresh(); location.hash = "#/chat/" + r.session.id;
  });
  const [edit, setEdit] = useState(null);
  const saveEdit = guard(async () => {
    const t = edit.text.trim(), i = edit.i; if (!t || busy) return;
    if (hist.slice(i + 1).some((x) => x.k === "user" || x.k === "msg")) { const v = await ask({ title: "Edit and resend?", text: "This message and everything after it are replaced by the new answer. To keep the old version, branch first.", submit: "Resend" }); if (!v) return; }
    await post("sessions/rewind", { id, index: i }); setHist((h) => h.slice(0, i)); setEdit(null); send(t);
  });
  const acts = (e, i, branch) => html`<div class="mxs"><button class="mx" title="Copy" aria-label="Copy" onClick=${() => copyMsg(e)}>${Ic("copy", 17)}</button>${!busy && e.k === "user" && html`<button class="mx" title="Edit and resend" aria-label="Edit and resend" onClick=${() => setEdit({ i, text: e.text })}>${Ic("edit", 17)}</button>`}${!busy && branch && html`<button class="mx" title="Branch into a new chat" aria-label="Branch into a new chat" onClick=${() => branchFrom(i)}>${Ic("branch", 17)}</button>`}${!busy && html`<button class="mx del" title="Delete message" aria-label="Delete message" onClick=${() => delMsg(i)}>${Ic("trash", 17)}</button>`}</div>`;
  const rename = guard(async () => {
    const v = await ask({ title: "Rename chat", fields: [{ name: "title", label: "Name", value: s.title, required: true, max: 120 }], submit: "Rename" });
    if (!v) return; const r = await post("sessions/update", { id, title: v.title.trim() }); setS(r.session); refresh();
  });
  const { results, steps } = useMemo(() => {                            // only changes when the history does, not on every streamed token
    const results = Object.fromEntries(hist.filter((e) => e.k === "tool_result").map((e) => [e.id, e]));
    const lastUser = hist.map((e) => e.k).lastIndexOf("user");
    const steps = hist.slice(lastUser + 1).filter((e) => e.k === "msg").flatMap((e) => e.blocks.filter((b) => b.type === "tool" && b.name !== "AskUserQuestion").map((b) => ({ name: b.name, hint: toolHint(b), state: results[b.id] ? (results[b.id].error ? "bad" : "ok") : "run" })));
    return { results, steps };
  }, [hist]);
  const u = s ? s.usage : {}, tot = (u.input || 0) + (u.cache_read || 0) + (u.cache_write || 0);
  const refreshMem = guard(async () => {
    if (!confirm("Reload soul and notes into this chat? It starts a fresh Claude session and the next turn rewrites the prompt cache.")) return;
    const extra = await OUI.languagePrompt(); await post("sessions/update", { id, refresh_memory: true, extra }); toast("Memory reloaded"); load();
  });
  const shareRef = useRef(null);                                       // chat blocks share through this chat (blocks.js setBlockSharer)
  const nodeCat = useRef(null);                                      // runnable nodes of the catalogue (blocks.js NodeCard), run in this chat's folder
  useEffect(() => { let on = true; setNodeHost({ info: async (t) => { nodeCat.current = nodeCat.current || api("works/nodes").then((d) => d.nodes || []); return (await nodeCat.current).find((n) => n.type === t && n.runnable) || (nodeCat.current = null); },
    run: (b) => post("nodes/run", { ...b, id }),
    workInfo: async (wid) => ((await api("works")).works || []).find((w) => w.id === wid) || null,
    workRun: async (wid, data, text, onStep) => {
      const r = await post("works/run", { id: wid, text: text || "run from chat", data }); if (!r.run) throw new Error("The workflow did not start (it has no Start node).");
      for (let i = 0; i < 800; i++) {
        await new Promise((ok) => setTimeout(ok, 1200)); if (!on) throw new Error("Stopped: the chat was closed.");
        const d = (await api(`works/run?id=${wid}&run=${r.run}`)).run, nodes = d.nodes || {}, out = { status: d.status, nodes, result: d.result, err: (Object.values(nodes).find((n) => n.error) || {}).error };
        if (d.status === "done" || d.status === "failed") return out; onStep(out);
      }
      return { status: "failed", nodes: {}, err: "Timed out waiting for the workflow." };
    } }); return () => { on = false; setNodeHost(null); }; }, [id]);
  useEffect(() => { setBlockSharer((content, filename) => shareRef.current && shareRef.current(filename, false, content)); return () => setBlockSharer(null); }, []);
  const src = (p) => `/w/${id}/${sig}/${p.split("/").map(encodeURIComponent).join("/")}`;
  const pickList = engine === "free" && freeIds && freeIds.length ? (() => { const base = (fresh || boot.models).models, have = new Set(base.map((m) => m.id)); return [...base, ...freeIds.filter((x) => !have.has(x.id)).map((x) => ({ id: x.id, label: x.id, kind: x.kind, note: x.kind === "image" ? "Image generation model from your API." : x.kind === "tts" ? "Text-to-speech model from your API." : "Available to your API key.", group: "settings", window: win }))]; })() : (fresh || boot.models).models;
  const mdl = pickList.find((m) => m.id === model) || (model ? { id: model, label: model, group: "picker", kind: "chat", note: "Model of this chat (not in your settings list)." } : pickList[0]);
  const lastCompact = [...hist].reverse().find((e) => e.k === "compact");
  const canPrev = (p) => /\.(html?|svg|png|jpe?g|gif|webp|pdf|md|txt|docx|xlsx|pptx)$/i.test(p || ""), fver = (files.find((f) => f.path === prev) || {}).mtime || 0;
  const leaveOk = () => { if (!dirtyRef.current) return true; if (!confirm("Discard unsaved changes to " + baseName(prevRef.current) + "?")) return false; dirtyRef.current = false; return true; };
  const changeTab = (t) => { if (t !== tab && tab === "edit" && !leaveOk()) return; setTab(t); };
  const pickFile = (f) => { if (!leaveOk()) return; setPrev(f.path); setTab(canPrev(f.path) ? "preview" : "edit"); };
  const refreshFiles = () => api("files?id=" + id).then((d) => { setFiles(d.files); setSig(d.sig); }).catch(() => {});
  const noFile = html`<div class="empty"><div>${Ic("file", 26)}<div>Pick a file in Files first.</div></div></div>`;
  const previewEl = prev ? (/\.html?$/i.test(prev) ? html`<${Viewer} url=${src(prev)} name=${prev} ver=${fver} sandbox="allow-scripts allow-forms allow-modals allow-popups"/>`
    : canPrev(prev) ? html`<iframe key=${prev + files.length + fver} sandbox=${/\.pdf$/i.test(prev) ? undefined : "allow-scripts allow-forms allow-modals allow-popups"} src=${src(prev)}></iframe>`
    : html`<div class="empty"><div>${Ic("file", 26)}<div>No preview for this kind of file.<br/>Use Edit to read it.</div></div></div>`)
    : html`<div class="empty"><div>${Ic("eye", 26)}<div>Select a file in Files,<br/>or let Claude write an .html one.</div></div></div>`;
  const sendOut = guard(async () => { const m = await chatsMod(); await m.sendChat({ get: api, post, id, title: s.title, signedIn: boot.context.signed_in }); });
  const share = guard(async (path, bundle, content = null) => { const r = await shareToSpace(post, id, path, { bundle, content, signedIn: boot.context.signed_in }); if (r && r.url) { try { await navigator.clipboard.writeText(r.url); toast("Link copied: " + r.url); } catch (e) {} } if (r && content != null) refreshFiles(); });
  shareRef.current = share;
  const [proj, setProj] = useState([]), [pub, setPub] = useState(null);                 // aihub packages of the working folder: a Publish button shows only when there are some
  useEffect(() => { setProj([]); api("project/info?id=" + id).then((d) => setProj(d.packages || [])).catch(() => {}); }, [id, s && s.cwd, busy]);
  const openCode = guard(async (path) => { await post("files/vscode", { id, path: path || "" }); toast("Opening in VS Code"); });
  const switchTo = (f) => { if (f.path === prev) return; if (dirtyRef.current && !confirm("Discard unsaved changes to " + baseName(prev) + "?")) return; dirtyRef.current = false; setPrev(f.path); };
  const editEl = prev && html`<div class="edlay">${rail && html`<div class="edfiles" style=${"width:" + ew + "px"}><${FileTree} files=${files} sel=${prev} src=${src} onPick=${switchTo}/></div><${Grip} cls="ed-grip" value=${() => ew} onChange=${setEw} onDone=${(v) => setPref("edw", String(v))} min=${120} max=${420} sign=${1} reset=${() => { setEw(200); setPref("edw", "200"); }}/>`}<${Editor} key=${prev} id=${id} path=${prev} ver=${fver} onSaved=${refreshFiles} dirtyRef=${dirtyRef}/></div>`, diffEl = prev && html`<${DiffView} key=${prev} id=${id} path=${prev} ver=${fver}/>`;
  // the message list is rebuilt only when the history, the busy state, the edit box or the folder signature change, not for every streamed token
  const histEl = useMemo(() => hist.map((e, i) => e.k === "user" ? html`<div class="msg user">${edit && edit.i === i ? null : acts(e, i, true)}${edit && edit.i === i ? html`<div class="bub editing"><textarea rows="3" value=${edit.text} autofocus onInput=${(x) => setEdit({ ...edit, text: x.target.value })} onKeyDown=${(x) => { if (x.key === "Escape") setEdit(null); else if (x.key === "Enter" && (x.metaKey || x.ctrlKey)) saveEdit(); }}></textarea>
            <div class="row" style="justify-content:flex-end;gap:8px;margin-top:6px"><button class="btn" onClick=${() => setEdit(null)}>Cancel</button><button class="btn acc" disabled=${!edit.text.trim()} onClick=${saveEdit}>Resend</button></div></div>` : html`<div class="bub">${e.text}${(e.files || []).map((f) => html`<div class="chip" style="margin-top:6px">${Ic("clip", 12)} ${f.split("/").pop()}</div>`)}</div>`}</div>`
          : e.k === "msg" ? html`<div class="msg bot"><div class="body">${acts(e, i, true)}${e.blocks.map((b) => b.type === "text" ? html`<${Md} text=${b.text} onRender=${showEl} onSend=${(t) => sendRef.current(t)}/>`
              : b.type === "thinking" ? html`<details class="thinkd"><summary>${Ic("sparkles", 13)} <b>Reasoning</b> <span class="mute small">${words(b.text)} words</span></summary><div class="thinkb">${b.text}</div></details>`
              : b.name === "AskUserQuestion" && b.input && b.input.questions ? html`<${AskCard} spec=${b.input} onSend=${(t) => sendRef.current(t)} locked=${busy || hist.slice(i + 1).some((x) => x.k === "user")}/>`
              : html`<${ToolRow} b=${b} res=${results[b.id]} hint=${toolHint(b)}/>`)}</div></div>`
          : e.k === "files" ? html`<div class="msg"><div class="fcard"><div class="fc-h">${Ic("folder", 14)}<b>${e.items.some((x) => x.new) ? "Files created" : "Files changed"}</b><span class="mute small">${e.items.length} in this chat's folder</span></div>
              ${e.items.map((f) => { const open = /\.(html?|svg|png|jpe?g|gif|webp|pdf|md|txt)$/i.test(f.path), img = /\.(png|jpe?g|gif|webp|svg)$/i.test(f.path);
                return html`<div class="fc-r"><span class="fc-i">${img ? html`<img src=${src(f.path)} alt=""/>` : Ic(/\.(pptx?|docx?|xlsx?)$/i.test(f.path) ? "file" : /\.html?$/i.test(f.path) ? "eye" : "file", 16)}</span>
                  <span class="grow fc-n"><b>${f.path.split("/").pop()}</b><span class="mute small">${f.path.includes("/") ? f.path.split("/").slice(0, -1).join("/") + " · " : ""}${num(f.size)}B${f.new ? "" : " · changed"}</span></span>
                  ${open && html`<button class="btn" onClick=${() => { if (!dirtyRef.current || confirm("Discard unsaved changes to " + baseName(prevRef.current) + "?")) { dirtyRef.current = false; setPrev(f.path); setTab("preview"); setOpen(true); } }}>${Ic("eye", 13)} Open</button>`}
                  <button class="btn" title="Share to a Space on the hub" onClick=${() => share(f.path, false)}>${Ic("upload", 13)}</button><a class="btn" href=${src(f.path) + "?download=1"} download title="Download">${Ic("download", 13)}</a></div>`; })}
              <div class="fc-f"><button type="button" class="lnk" onClick=${() => { setTab("files"); setOpen(true); }}>All files ${Ic("right", 12)}</button></div></div></div>`
          : e.k === "result" ? html`<div class="stats"><span>${(e.ms / 1000).toFixed(1)}s</span><span>in ${num(e.usage.input)}</span><span title="Tokens the model wrote for this turn">${num(e.usage.output)} tokens written</span><span>cache read ${num(e.usage.cache_read)}</span><span>cache write ${num(e.usage.cache_write)}</span>${e.error && html`<span class="tag bad">failed</span>`}</div>`
          : e.k === "note" ? html`<div class="note-line">${Ic("info", 14)} ${e.text}</div>`
          : e.k === "compact" ? html`<div class="note-line">${Ic("compact", 14)} Context compacted · ${num(e.pre)} → ${num(e.post)} tokens (${e.trigger})</div>`
          : e.k === "error" ? html`<div class="msg"><span class="tag bad">${Ic("alert", 12)} error</span> ${linkify(e.text)} ${e.auth && html`<button class="btn" onClick=${() => setSignin(true)}>Sign in</button>`}</div>` : null), [hist, busy, edit, sig, boot]);
  if (!s) return missing ? html`<div class="empty"><div>${Ic("alert", 28)}<h1>Chat not found</h1><p class="mute">${missing}</p><div class="row" style="justify-content:center;gap:8px"><button class="btn acc" onClick=${newChat}>${Ic("plus", 14)} New chat</button><button class="btn" onClick=${load}>${Ic("refresh", 14)} Try again</button></div></div></div>` : html`<div class="empty"><div>${Ic("sparkles", 28)}<div>Loading…</div></div></div>`;
  return html`<div class="chatwrap">${signin && html`<${SignIn} close=${() => setSignin(false)}/>`}
    <div class=${"chat" + (engine === "free" ? "" : " eng-claude")}>
      ${pickF && html`<${FilePicker} ext="" title=${pickF === "dir" ? "Choose a folder to mention" : "Choose a file to mention"} start=${dirInfo.dir || ""} dirs=${pickF === "dir"} onPick=${(p) => { const kind = pickF === "dir" ? "@folder:" : "@file:"; setPickF(false); const d = (dirInfo.dir || "").replace(/\\/g, "/").replace(/\/+$/, ""), q = p.replace(/\\/g, "/"); setText((t) => (t && !/\s$/.test(t) ? t + " " : t) + kind + (d && q.startsWith(d + "/") ? q.slice(d.length + 1) : q) + " "); ta.current && ta.current.focus(); }} close=${() => setPickF(false)}/>`}
      ${dlg === "mcp" && html`<${Modal} wide=${true} close=${() => setDlg(null)}><div class="row"><h1 class="grow" style="margin:0">MCP servers</h1><button class="ibtn" aria-label="Close" onClick=${() => setDlg(null)}>${Ic("x", 17)}</button></div>
        <p class="sub" style="margin-top:4px">Engine: <b>${engine === "free" ? "Free chat" : "Claude Code"}</b> · <button class="linkbtn" onClick=${() => setEng(engine === "free" ? "claude" : "free")}>switch to ${engine === "free" ? "Claude Code" : "Free chat"}</button></p>
        <${McpPanel} servers=${mcpAll} on=${s.mcp || []} onChange=${saveMcp} engine=${engine}/>
        <div class="row" style="margin-top:14px"><a class="btn" href="#/mcp">${Ic("sliders", 14)} Manage servers</a><span class="grow"></span><button class="btn acc" onClick=${() => setDlg(null)}>Done</button></div><//>`}
      ${dlg === "dir" && html`<${WorkDirDialog} s=${s} cur=${dirInfo.dir} busy=${busy} close=${() => setDlg(null)} saved=${(n) => { setDlg(null); setS(n); load(); refresh(); }}/>`}
      ${dlg === "soul" && html`<${SoulDialog} s=${s} busy=${busy} close=${() => setDlg(null)} saved=${(n) => { setDlg(null); setS(n); load(); }}/>`}
      <div class="chat-top"><button class="ibtn burger" title="Chats and pages" aria-label="Chats and pages" onClick=${onMenu}>${Ic("menu", 18)}</button><b class="grow ctitle" title="Rename" onClick=${rename}>${s.title}</b><button class="ibtn" title="Rename chat" aria-label="Rename chat" onClick=${rename}>${Ic("edit", 16)}</button><button class="ibtn" title="Share or export this chat" aria-label="Share or export this chat" onClick=${sendOut}>${Ic("share", 16)}</button><button class="ibtn" title="Import a chat (link, text or file)" aria-label="Import a chat" onClick=${onImport}>${Ic("import", 16)}</button>
        <span class="dirwrap">${(() => { const fresh = !hist.length && !busy && !hintOff; return html`<button class=${"dirchip" + (s.cwd ? " set" : "") + (fresh ? " nudge" : "")} title=${"Working folder: " + dirInfo.dir + "\nClick to change"} onClick=${() => { setHintOff(true); setDlg("dir"); }}>${Ic("folder", fresh ? 17 : 14)}<span>${s.cwd ? baseName(s.cwd) : "Private folder"}</span></button>${fresh && !s.cwd && html`<div class="tip-hint" role="tooltip"><b>Choose a project</b><span>Pick your work project folder, or stay in the blank private one.</span><div class="row" style="gap:6px;margin-top:8px"><button class="btn acc" onClick=${() => { setHintOff(true); setDlg("dir"); }}>Choose project</button><button class="btn" onClick=${() => setHintOff(true)}>Blank</button></div></div>`}`; })()}</span>
        <button class=${"ibtn" + (s.soul != null ? " on" : "")} title=${s.soul != null ? "This chat has its own soul" : "Soul for this chat"} aria-label="Soul for this chat" onClick=${() => setDlg("soul")}>${Ic("user", 16)}</button>
        <button class="ibtn" title="Reload soul and notes (rewrites cache)" aria-label="Reload soul and notes (rewrites cache)" onClick=${refreshMem}>${Ic("refresh", 16)}</button>
        ${proj.length > 0 && html`<button class="btn acc" title="Publish this project to the hub (aihub dev publish -i)" onClick=${() => setPub({ name: proj.map((x) => x.name).join(", "), version: proj.length === 1 ? proj[0].version : "", path: dirInfo.dir, project: id, check: { ok: true } })}>${Ic("upload", 14)} Publish</button>`}
        <button class=${"ibtn" + (term ? " on" : "")} title="Terminal in the working folder" aria-label="Terminal in the working folder" aria-pressed=${term} onClick=${() => { setTermOn(true); setTerm(!term); }}>${Ic("terminal", 17)}</button>
        <button class=${"ibtn cvb" + (!hist.length && !busy && !open && !hintOff ? " nudge" : "")} title=${files.length ? `Canvas - ${files.length} file${files.length > 1 ? "s" : ""}` : "Canvas"} aria-label="Canvas" aria-pressed=${open} onClick=${() => setOpen(!open)} style=${open ? "color:var(--accent2)" : ""}>${Ic("panel", 18)}${files.length > 0 && !open && html`<i class="cnt">${files.length}</i>`}</button></div>
      <div class="msgs" ref=${msgsRef} onScroll=${onScroll}><div class="col">
        ${!hist.length && !busy && html`<div class="hello"><div><span class="herologo">${Ic("logo", 44, "")}</span><h1>What shall we work on?</h1><p>Claude Code runs in this chat's own folder with every skill, agent and MCP server aihub installed.<br/>Type <span class="kbd">/</span> for commands.</p></div></div>`}
        ${histEl}
        ${gen && html`<${GenBar} gen=${gen}/>`}
        ${live.text && html`<div class="msg bot"><div class="body"><${Md} live=${true} text=${live.text} onRender=${showEl}/></div></div>`}
        ${wr.map((r) => html`<${WorkCard} key=${r.key} r=${r} busy=${busy} submit=${(d) => startWork(r.key, r.wk, d, r.text)} cancel=${() => setWr((l) => l.filter((x) => x.key !== r.key))} toChat=${(t) => sendRef.current(t)}/>`)}
        ${busy && html`<${Working} act=${act} since=${since} live=${live} steps=${steps} ctx=${ctx} win=${win} free=${engine === "free"} onStop=${stop} onDirect=${answerNow}/>`}
        <div ref=${end}></div>
      </div>${!stuck && (hist.length > 0 || busy) && html`<div class="jumpw"><button class="jump" onClick=${jump}>${Ic("down", 14)} Jump to latest</button></div>`}</div>
      ${pub && html`<${PublishSteps} r=${pub} boot=${boot} close=${() => setPub(null)}/>`}
      <div class="composer">
        <div class=${"box" + (busy ? " busy" : "")} onDragOver=${(e) => e.preventDefault()} onDrop=${(e) => { e.preventDefault(); pick(e.dataTransfer.files); }}>
          ${(sm || mm) && html`<${SlashMenu} items=${menuItems} idx=${sidx} pick=${pickSlash}/>`}
          ${att.length ? html`<div>${att.map((a, i) => html`<span class="chip">${a.type.startsWith("image/") ? html`<img src=${a.url} height="16"/>` : Ic("file", 12)} ${a.name} <button type="button" class="chipx" aria-label=${"Remove " + a.name} onClick=${() => setAtt(att.filter((_, j) => j !== i))}>${Ic("x", 12)}</button></span>`)}</div>` : ""}
          <textarea ref=${ta} rows="1" placeholder=${engine === "free" ? "Ask the model directly…  (no tools, no files)" : "Message Claude…   / for commands"} value=${text} onInput=${(e) => { setText(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 240) + "px"; }}
            onPaste=${(e) => { const f = [...e.clipboardData.files]; if (f.length) { e.preventDefault(); pick(f); } }} onKeyDown=${onKey}></textarea>
          <div class="tools">
            <button class="ibtn" title="Attach files" aria-label="Attach files" onClick=${() => fileIn.current && fileIn.current.click()}>${Ic("clip", 17)}</button><input type="file" multiple hidden ref=${fileIn} onChange=${(e) => { pick(e.target.files); e.target.value = ""; }}/>
            <button class="ibtn" title="Choose a file from this project (adds @file:path)" aria-label="Choose a file from this project (adds @file:path)" onClick=${() => setPickF("file")}>${Ic("file", 17)}</button>
            <button class="ibtn" title="Choose a folder from this project (adds @folder:path)" aria-label="Choose a folder from this project (adds @folder:path)" onClick=${() => setPickF("dir")}>${Ic("folder", 17)}</button>
            <${Pop} open=${pop === "engine"} setOpen=${(v) => setPop(v ? "engine" : null)} button=${html`<${Pill} icon=${engine === "free" ? "zap" : "terminal"} label=${engine === "free" ? "Free chat" + ((s.mcp || []).length ? " · " + s.mcp.length + " tool" + (s.mcp.length > 1 ? "s" : "") : "") : "Claude Code"} tone=${engine === "free" ? "" : "claude"} active=${pop === "engine" || engine === "free"} title="Engine" onClick=${() => { setPop(pop === "engine" ? null : "engine"); if (!mcpAll) api("mcp/free").then((d) => setMcpAll(d.servers)).catch(() => setMcpAll([])); }}/>`}><${EnginePick} mcpAll=${mcpAll} mcpOn=${s.mcp || []} onMcp=${saveMcp} value=${engine} onChange=${setEng} close=${() => { setPop(null); ta.current && ta.current.focus(); }}/><//>
            <i class="tsep" aria-hidden="true"></i>
            ${engine !== "free" && html`<${Pop} open=${pop === "mode"} setOpen=${(v) => setPop(v ? "mode" : null)} button=${html`<${Pill} icon=${MODE_INFO[mode][0]} label=${MODE_INFO[mode][1]} tone=${MODE_TONE[mode]} active=${pop === "mode"} title=${"Permission mode: " + MODE_INFO[mode][1]} onClick=${() => setPop(pop === "mode" ? null : "mode")}/>`}><${ModePick} modes=${boot.modes} value=${mode} onChange=${setMode} close=${() => { setPop(null); ta.current && ta.current.focus(); }}/><//>`}
            <${Pop} open=${pop === "effort"} setOpen=${(v) => setPop(v ? "effort" : null)} button=${html`<${Pill} lead=${EffMeter(effort)} label=${effort ? (effort === "xhigh" ? "XHigh" : effort[0].toUpperCase() + effort.slice(1)) : "Effort"} tone=${EFFORT_TONE[effort]} active=${pop === "effort"} title=${"Thinking effort: " + (effort || "default")} onClick=${() => setPop(pop === "effort" ? null : "effort")}/>`}><${EffortPick} value=${effort} onChange=${setEffort} close=${() => { setPop(null); ta.current && ta.current.focus(); }}/><//>
            <span class="grow" style="flex:1"></span>
            <${Pop} open=${pop === "ctx"} setOpen=${(v) => setPop(v ? "ctx" : null)} align="right" button=${html`<button class="ctxbtn" title="Context window" aria-label="Context window" onClick=${() => setPop(pop === "ctx" ? null : "ctx")}><${Ring} pct=${win ? ctx / win : 0}/><span>${ctx ? Math.round((ctx / win) * 100) + "%" : ""}</span></button>`}>
              <${CtxPick} used=${ctx} win=${win} prefs=${prefs} savePrefs=${savePrefs} busy=${busy} compact=${() => { setPop(null); send("/compact"); }} note=${lastCompact && `Last compact: ${num(lastCompact.pre)} → ${num(lastCompact.post)}`}/><//>
            <${Pop} open=${pop === "model"} setOpen=${(v) => setPop(v ? "model" : null)} align="right" button=${html`<${Pill} icon="cpu" label=${mdl.label.length > 26 ? mdl.label.slice(0, 24) + "…" : mdl.label} active=${pop === "model"} title="Model" onClick=${() => { setPop(pop === "model" ? null : "model"); api("models").then(setFresh).catch(() => {}); if (engine === "free" && !freeIds) api("free/models").then((d) => setFreeIds(d.items || (d.models || []).map((id) => ({ id, kind: "chat" })))).catch(() => setFreeIds([])); }}/>`}><${ModelPick} models=${pickList} value=${model} onChange=${setModel} close=${() => setPop(null)}/><//>
            ${busy ? html`<button class="send stop" title="Stop" aria-label="Stop" onClick=${stop}>${Ic("stop", 16)}</button>` : html`<button class="send" title="Send" aria-label="Send" onClick=${() => send()} disabled=${!text.trim()}>${Ic("send", 17)}</button>`}
          </div>
        </div>
        <div class="foot">${u.turns ? `cache hit ${tot ? Math.round(100 * (u.cache_read || 0) / tot) : 0}% · read ${num(u.cache_read)} · written ${num(u.cache_write)} · ${u.turns} turns` + (u.cost ? ` · $${u.cost.toFixed(3)}` : "") : "Same chat, model and tools keep the prompt cache warm."}</div>
      </div>
      ${termOn && html`<${ChatTerm} key=${id} id=${id} show=${term} hide=${() => setTerm(false)}/>`}
    </div>
    <div class=${"canvas" + (open ? " open" : "") + (split && (tab === "edit" || tab === "diff") ? " wide" : "")} style=${cw ? "--cw:" + cw + "px" : ""}>
      ${open && html`<${Grip} cls="cv-grip" value=${() => document.querySelector(".canvas").getBoundingClientRect().width} onChange=${setCw} onDone=${(v) => setPref("canvasw", String(Math.round(v)))} min=${360} max=${() => Math.max(420, innerWidth - 272 - 320)} sign=${-1} reset=${() => { setCw(0); setPref("canvasw", "0"); }}/>`}
      <div class="ctabs"><${Seg} small value=${tab} onChange=${changeTab} items=${[{ id: "files", label: "Files", icon: "folder" }, { id: "preview", label: "Preview", icon: "eye" }, { id: "edit", label: "Edit", icon: "edit" }, { id: "diff", label: "Diff", icon: "layers" }, { id: "elements", label: "Elements", icon: "grid" }]}/><button class="ibtn cv-x" title="Close the canvas" aria-label="Close the canvas" onClick=${() => setOpen(false)}>${Ic("x", 16)}</button></div>
      ${(tab === "preview" || tab === "edit" || tab === "diff") && html`<div class="fbar"><span class="mono t" title=${prev}>${prev || "No file selected"}</span>${prev && canPrev(prev) && html`<a class="small" href=${src(prev)} target="_blank" rel="noopener">Open ${Ic("link", 12)}</a>`}${prev && !/\.(exe|dll|zip|gz|tar|bin)$/i.test(prev) && html`<button class="btn" title="Publish this file as a Space on the hub" onClick=${() => share(prev, false)}>${Ic("upload", 13)} Share</button>`}${/(^|\/)index\.html?$/i.test(prev || "") && html`<button class="btn" title="Publish the whole folder as a web page Space" onClick=${() => share(prev, true)}>${Ic("folder", 13)} Share bundle</button>`}
        ${tab === "edit" && html`<button class=${"ibtn" + (rail ? " on" : "")} style="width:26px;height:26px" title="Show the file list beside the editor" aria-label="Show the file list beside the editor" onClick=${() => { setRail(!rail); setPref("edrail", rail ? "0" : "1"); }}>${Ic("folder", 14)}</button>`}
        ${prev && html`<button class="btn" title="Open this file, in the project folder, in VS Code" onClick=${() => openCode(prev)}>${Ic("code", 13)} VS Code</button>`}
        ${(tab === "edit" || tab === "diff") && html`<button class=${"ibtn" + (split ? " on" : "")} style="width:26px;height:26px" title="Show the preview beside the editor or diff" aria-label="Show the preview beside the editor or diff" onClick=${() => setSplit(!split)}>${Ic("panel", 14)}</button>`}</div>`}
      <div class="cbody">
        ${tab === "files" && html`<div class="fdir"><span class="mono t" title=${dirInfo.dir}>${dirInfo.dir}</span><button class="btn" title="Open the project folder in VS Code" onClick=${() => openCode("")}>${Ic("code", 13)} VS Code</button><button class="btn" onClick=${() => setDlg("dir")}>${Ic("folder", 13)} Change</button></div>
          ${files.length ? html`<${FileTree} files=${files} sel=${prev} src=${src} onPick=${pickFile}/>` : html`<div class="empty"><div>${Ic("folder", 26)}<div>No files yet.<br/>Ask Claude to build something.</div></div></div>`}`}
        ${tab === "preview" && previewEl}
        ${(tab === "edit" || tab === "diff") && (!prev ? noFile : split && canPrev(prev) ? html`<div class="split"><div class="pane">${previewEl}</div><div class="pane">${tab === "edit" ? editEl : diffEl}</div></div>` : tab === "edit" ? editEl : diffEl)}
        ${tab === "elements" && (els.length ? els.map((e, i) => html`<${Element} key=${e.id} e=${e}/>`) : html`<div class="empty"><div>${Ic("layers", 26)}<div>Use Render on an html, svg or openui block<br/>to see it here.</div></div></div>`)}
      </div>
    </div>
  </div>`;
}
const toolHint = (b) => { const i = b.input || {}; return String(i.file_path || i.command || i.pattern || i.url || i.description || "").slice(0, 90); };
function Element({ e }) {
  const ref = useRef(), [err, setErr] = useState("");
  useEffect(() => { let un; if (e.lang === "openui") OUI.mount(ref.current, e.code).then((u) => (un = u)).catch((x) => setErr("OpenUI could not load (" + x.message + ")")); return () => un && un(); }, [e.code]);
  if (e.lang === "openui") return html`<div class="ou"><div ref=${ref}></div>${err && html`<p class="tag bad">${err}</p><pre>${e.code}</pre>`}</div>`;
  const doc = e.lang === "svg" ? `<body style="margin:0;display:grid;place-items:center">${e.code}` : e.code;
  return html`<iframe sandbox="allow-scripts" srcdoc=${doc} style="width:100%;height:380px;border:0;border-bottom:1px solid var(--line);background:#fff"></iframe>`;
}

// ---------- Claude sign-in (apiKeyHelper)
function SignIn({ close }) {
  const [st, setSt] = useState(null);
  const poll = () => api("auth").then(setSt).catch(() => {});
  useEffect(() => { guard(async () => { setSt(await post("auth/login")); })(); }, []);
  useInterval(poll, 1000);
  const lg = st && st.login;
  return html`<${Modal} close=${close}><h1>Sign in to Claude</h1><p class="sub">Running your <code>apiKeyHelper</code> (${st ? st.program : "…"}). Follow any link it prints; the helper keeps its own token.</p>
    <div class="io" style="min-height:90px;max-height:40vh">${lg ? lg.lines.map((l) => html`<div>${linkify(l)}</div>`) : "Starting…"}</div>
    <div class="row"><span class=${"tag " + (lg && lg.ok ? "good" : lg && lg.ok === false ? "bad" : "")}>${!lg ? "…" : lg.running ? "waiting for sign-in…" : lg.ok ? "signed in" : "failed"}</span><span class="grow"></span>
      ${lg && lg.ok === false && html`<button class="btn" onClick=${guard(async () => setSt(await post("auth/login")))}>Try again</button>`}<button class="btn pri" onClick=${close}>${lg && lg.ok ? "Done" : "Close"}</button></div>
    <p class="mute small">If the helper needs a terminal, run the same command from <code>~/.claude/settings.json</code> there once.</p><//>`;
}

// ---------- packages
function Packages({ focus }) {
  const [tab, setTab] = useState(focus ? "find" : "installed"), [list, setList] = useState([]), [q, setQ] = useState(focus || ""), [found, setFound] = useState([]), [busy, setBusy] = useState(""), [dlg, setDlg] = useState(null), [setup, setSetup] = useState(null);
  const load = guard(async () => setList((await api("packages")).packages));
  useEffect(() => { load(); }, []);
  useEffect(() => { if (tab === "find") { const t = setTimeout(guard(async () => setFound((await api("search?q=" + encodeURIComponent(q))).results)), 250); return () => clearTimeout(t); } }, [q, tab]);
  const act = (what, name) => guard(async () => { setBusy(name); try { const d = await post(what, { name }); toast(what + " " + name + " done"); return d; } finally { setBusy(""); load(); } });
  const have = new Set(list.map((p) => p.name));
  return html`<div class="page"><h1>Packages</h1><p class="sub">Skills, agents, MCP servers, tools, bots and services, managed by the aihub CLI.</p>
    <div class="row"><${Seg} value=${tab} onChange=${setTab} items=${[{ id: "installed", label: `Installed (${list.length})`, icon: "package" }, { id: "find", label: "Find", icon: "search" }]}/><span class="grow"></span>
      <button class="btn" disabled=${busy === "*"} onClick=${guard(async () => { setBusy("*"); try { await post("update", {}); toast("All packages are current"); } finally { setBusy(""); load(); } })}>${Ic("refresh", 14)} Update all</button></div>
    ${tab === "find" && html`<div class="row" style="margin-top:14px"><input class="grow" placeholder="Search packages…" value=${q} onInput=${(e) => setQ(e.target.value)} autofocus/></div>`}
    ${(tab === "installed" ? list : found).map((p) => html`<div class="card row"><span class="nico" style="width:34px;height:34px;border-radius:10px;background:var(--soft2);display:grid;place-items:center">${Ic(p.type === "mcp" ? "plug" : p.type === "bot" || p.type === "service" ? "server" : p.type === "skill" ? "sparkles" : "package", 17)}</span>
      <div class="grow"><b>${p.name}</b> <span class="mute">${p.version || p.latest_version || ""}</span> ${p.type && html`<span class="tag">${p.type}</span>`}
        ${tab === "installed" && p.enabled === false && html`<span class="tag bad">disabled</span>`}<div class="mute small">${p.description || (p.components || []).join(", ")}</div></div>
      ${tab === "installed" ? html`<button class="btn" disabled=${!!busy} onClick=${act(p.enabled === false ? "enable" : "disable", p.name)}>${p.enabled === false ? "Enable" : "Disable"}</button>
          <button class="btn" disabled=${!!busy} onClick=${act("update", p.name)}>Update</button><button class="btn bad" disabled=${!!busy} onClick=${() => confirm("Uninstall " + p.name + "?") && act("uninstall", p.name)()}>${Ic("trash", 14)}</button>`
        : have.has(p.name) ? html`<span class="tag good">${Ic("check", 12)} installed</span>` : html`<button class="btn acc" onClick=${() => setDlg(p)}>${Ic("download", 14)} Install</button>`}</div>`)}
    ${tab === "find" && !found.length && html`<p class="mute">No matches.</p>`}
    ${dlg && html`<${InstallDialog} p=${dlg} close=${() => setDlg(null)} done=${(r) => { setDlg(null); load(); if (r.setup && r.setup.length) setSetup(r.setup); }}/>`}
    ${setup && html`<${SetupDialog} items=${setup} close=${() => setSetup(null)}/>`}</div>`;
}
function InstallDialog({ p, close, done }) {
  const [o, setO] = useState({ term: true, yes: false, setup: true, indirect: false }), [busy, setBusy] = useState(false), [run, setRun] = useState(null), [fin, setFin] = useState(false);
  const host = useRef(), tm = useRef();
  const chk = (k, label, hint) => html`<label class="row" style="margin:10px 0;align-items:flex-start"><input type="checkbox" checked=${o[k]} onChange=${(e) => setO({ ...o, [k]: e.target.checked })}/><span><b>${label}</b><div class="mute small">${hint}</div></span></label>`;
  const go = guard(async () => { setBusy(true); try { const r = await post("install", { names: [p.name], yes: o.yes, indirect: o.indirect }); toast(p.name + " installed"); done(o.setup ? r : { ...r, setup: [] }); } finally { setBusy(false); } });
  const goTerm = guard(async () => {
    setRun({ state: "run" });
    try { tm.current = await runInstall(host.current, { names: [p.name], indirect: o.indirect, yes: o.yes }, { post, api }, (code, svc) => { setRun({ state: code === 0 ? "ok" : "bad", code, svc }); setFin(true); }); }
    catch (e) { setRun(null); throw e; }
  });
  const finish = guard(async () => {                                     // closes the terminal and the dialog
    const r = run; tm.current && tm.current.done();
    let items = []; if (o.setup && r.svc && r.svc.length) items = ((await api("services")).services || []).filter((x) => r.svc.includes(x.name) && x.env.length);
    done({ setup: items });
  });
  useEffect(() => () => tm.current && tm.current.stop && tm.current.stop(), []);
  const ok = run && run.state === "ok";
  return html`<${Modal} wide=${!!run} close=${() => !busy && !run && close()}><h1>${run ? "Installing " : "Install "}${p.name}</h1>
    <p class="sub">${!run ? p.description : run.state === "run" ? "This is a real terminal: answer any question the install asks, then press Enter." + (o.yes ? " Prompts are answered yes for you (--yes)." : "") : ok ? "Installed." : "The install ended with exit code " + run.code + "."}</p>
    <div class="termbox" ref=${host} style=${run ? "" : "display:none"}></div>
    ${run ? html`<div class="row" style="margin-top:14px">${run.state === "run" && html`<span class="shine">Working…</span>`}<span class="grow"></span>
      ${run.state === "run" ? html`<button class="btn" onClick=${() => { tm.current && tm.current.stop(); setRun(null); close(); }}>Stop</button>` : html`<button class="btn acc" onClick=${finish}>${ok ? "Close terminal" : "Close"}</button>`}</div>` : html`<div>
    ${chk("term", "Open a terminal", "Shows the install live and lets you answer questions the package's install script asks (keys, choices).")}
    ${chk("yes", "Answer yes to every prompt", "Registers with every detected AI tool without asking, as aihub install --yes. Leave it off if you want to choose in the terminal.")}
    ${chk("setup", "Open settings after install", "Fill in keys and options the package needs. Secrets stay in its private .env.")}
    ${chk("indirect", "Download through the hub", "No git login needed on this machine.")}
    <div class="row" style="margin-top:18px"><span class="grow"></span><button class="btn" onClick=${close} disabled=${busy}>Cancel</button><button class="btn acc" onClick=${o.term ? goTerm : go} disabled=${busy}>${busy ? "Installing…" : "Install"}</button></div></div>`}<//>
    ${run && fin && html`<${Modal} close=${() => setFin(false)}><h1>${ok ? "Install finished" : "Install failed"}</h1>
      <p class="sub">${ok ? p.name + " is installed." : p.name + " ended with exit code " + run.code + ". The terminal output above shows why."} Close the terminal now, or keep it open to read the output and close it yourself.</p>
      <div class="row" style="margin-top:14px"><span class="grow"></span><button class="btn" onClick=${() => setFin(false)}>Cancel</button><button class="btn acc" autofocus onClick=${finish}>Close</button></div><//>`}`;
}
function SetupDialog({ items, close }) {
  const [i, setI] = useState(0), it = items[i], [v, setV] = useState({}), [start, setStart] = useState(true);
  const save = guard(async () => {
    await put("services/env", { name: it.name, values: v });
    if (start && it.env.filter((e) => e.required).every((e) => v[e.name] || e.set || e.default)) await post("services/action", { name: it.name, action: "start" });
    toast(it.name + " saved"); setV({}); i + 1 < items.length ? setI(i + 1) : close();
  });
  return html`<${Modal}><h1>Set up ${it.name}</h1><p class="sub">${i + 1} of ${items.length}. Leave a secret blank to keep the stored value.</p>
    ${it.env.map((e) => html`<div class="field"><label>${e.name}${e.required ? " *" : ""}</label>
      <input type=${e.secret ? "password" : "text"} autocomplete="off" placeholder=${e.secret ? (e.set ? "•••••• stored" : "") : e.default} value=${v[e.name] ?? e.value} onInput=${(x) => setV({ ...v, [e.name]: x.target.value })}/><span class="mute small">${e.hint}</span></div>`)}
    <label class="row"><input type="checkbox" checked=${start} onChange=${(e) => setStart(e.target.checked)}/> Start ${it.name} now</label>
    <div class="row" style="margin-top:18px"><span class="grow"></span><button class="btn" onClick=${() => (i + 1 < items.length ? setI(i + 1) : close())}>Skip</button><button class="btn acc" onClick=${save}>Save</button></div><//>`;
}

// ---------- services
function Services() {
  const [list, setList] = useState([]), [log, setLog] = useState(null), [edit, setEdit] = useState(null);
  const bad = useRef({ n: 0, at: 0 });                                 // a failing poll goes quiet and backs off (5 s, 10 s, ... 60 s); only a manual refresh complains
  const load = async (manual) => {
    if (manual !== true && Date.now() < bad.current.at) return;
    try { setList((await api("services")).services); bad.current = { n: 0, at: 0 }; }
    catch (x) { const n = bad.current.n + 1; bad.current = { n, at: Date.now() + Math.min(60000, 5000 * 2 ** n) }; if (manual === true) toast(x.message); }
  };
  useEffect(() => { load(true); }, []);
  useInterval(load, 5000);
  const act = (name, action) => guard(async () => { await post("services/action", { name, action }); toast(name + ": " + action); load(true); });
  const logs = guard(async (n) => setLog({ name: n, text: (await api("services/logs?name=" + n)).log }));
  return html`<div class="page"><h1>Services</h1><p class="sub">Bots and services installed by aihub, run in the background by <code>aihub run</code>. <button class="btn" onClick=${() => load(true)}>${Ic("refresh", 14)} Refresh</button></p>
    ${!list.length && html`<div class="card mute">No bots or services installed. Find some under Packages.</div>`}
    ${list.map((s) => { const r = s.runs[0], alive = r && r.alive; return html`<div class="card"><div class="row"><span class="nico" style="width:34px;height:34px;border-radius:10px;background:var(--soft2);display:grid;place-items:center">${Ic("server", 17)}</span><div class="grow"><b>${s.name}</b> <span class="tag">${s.kind}</span> <span class=${"tag " + (alive ? "good" : "")}>${alive ? "running" : (r && r.status) || "stopped"}</span>${s.enabled === false ? html` <span class="tag bad">disabled</span>` : ""}</div>
        ${alive ? html`<button class="btn" onClick=${act(s.name, "restart")}>${Ic("refresh", 14)} Restart</button><button class="btn" onClick=${act(s.name, "stop")}>${Ic("stop", 12)} Stop</button>` : html`<button class="btn acc" onClick=${act(s.name, "start")}>${Ic("play", 13)} Start</button>`}
        <button class="btn" onClick=${() => logs(s.name)}>${Ic("terminal", 14)} Logs</button>${s.env.length ? html`<button class="btn" onClick=${() => setEdit(s)}>${Ic("sliders", 14)} Settings</button>` : ""}
        <button class="btn" onClick=${guard(async () => { await post(s.enabled === false ? "enable" : "disable", { name: s.name }); load(); })}>${s.enabled === false ? "Enable" : "Disable"}</button></div></div>`; })}
    ${log && html`<${Modal} close=${() => setLog(null)} wide=${true}><h1>${log.name} log</h1><div class="io" style="max-height:55vh">${log.text || "(empty)"}</div><button class="btn" onClick=${() => logs(log.name)}>${Ic("refresh", 14)} Refresh</button><//>`}
    ${edit && html`<${SetupDialog} items=${[edit]} close=${() => { setEdit(null); load(); }}/>`}</div>`;
}

// ---------- MCP
function McpFree() {
  const [list, setList] = useState(null), [tests, setTests] = useState({}), [busy, setBusy] = useState("");
  const load = guard(async () => setList((await api("mcp/free")).servers));
  useEffect(() => { load(); }, []);
  const set = (body) => post("mcp/settings", body).then((r) => setList(r.servers)).catch((e) => toast(e.message));
  const test = async (name) => { setBusy(name); try { setTests((t) => ({ ...t, [name]: { ...(t[name] || {}), wait: true } })); const r = await post("mcp/test", { name }); setTests((t) => ({ ...t, [name]: r })); } catch (e) { setTests((t) => ({ ...t, [name]: { ok: false, error: e.message, tools: [] } })); } finally { setBusy(""); } };
  return html`<div><h2>Free chat tools</h2><p class="sub">Which MCP servers and tools Free chat may use. “Allowed” is the master switch; “On in new chats” ticks it for every new free chat. Test starts the server, lists its tools and lets you hide single tools. In a chat you can also just ask the model to retry, enable or disable a server or tool.</p>
    ${!list ? html`<p class="mute">Loading…</p>` : !list.length ? html`<p class="mute">No stdio MCP server found.</p>` : list.map((s) => { const t = tests[s.name]; return html`<div class="card" key=${s.name}><div class="row"><div class="grow"><b>${s.name}</b> <span class="tag">${s.origin}</span>${s.tools_off.length ? html` <span class="tag">${s.tools_off.length} tool${s.tools_off.length > 1 ? "s" : ""} hidden</span>` : ""}<div class="mute small mono">${s.command}</div></div>
        <label class="row small" style="gap:6px"><${Switch} on=${!s.off} label="Allowed" onChange=${(v) => set({ name: s.name, off: !v })}/>Allowed</label>
        <label class="row small" style="gap:6px"><${Switch} on=${s.default && !s.off} label="On in new chats" onChange=${(v) => set({ name: s.name, default: v })}/>On in new chats</label>
        <button class="btn" disabled=${busy === s.name} onClick=${() => test(s.name)}>${busy === s.name ? "Starting…" : t && t.ok === false ? "Retry" : "Test"}</button></div>
        ${t && !t.wait && (t.ok ? html`<div style="margin-top:8px">${t.tools.map((x) => html`<label class="opt mcpopt" key=${x.name}><input type="checkbox" checked=${!s.tools_off.includes(x.name)} onChange=${(e) => set({ name: s.name, tool: x.name, tool_off: !e.target.checked })}/><span class="grow"><b>${x.name}</b><span class="d">${x.description}</span></span></label>`)}${!t.tools.length && html`<p class="mute small">The server runs but lists no tools.</p>`}</div>` : html`<p class="small" style="color:var(--bad,#c0392b);margin-top:8px">Could not start: ${t.error}</p>`)}</div>`; })}</div>`;
}

function Mcp() {
  const [d, setD] = useState({ packages: [], claude: [], ui: [] }), [f, setF] = useState({ name: "", command: "", args: "" });
  const load = guard(async () => setD(await api("mcp")));
  useEffect(() => { load(); }, []);
  const row = (n, sub, extra) => html`<div class="mcp-card" style="margin:8px 0"><span class="mcp-ico">${Ic("plug", 17)}</span><div class="grow"><b>${n}</b><span class="d">${sub}</span></div>${extra}</div>`;
  return html`<div class="page"><h1>MCP servers</h1><p class="sub">Every server Claude Code knows is available in chat, including the ones aihub packages register.</p>
    <${McpFree}/>
    <h2>From packages</h2>${d.packages.map((p) => row(p.name, p.enabled ? "enabled" : "disabled", html`<button class="btn" onClick=${guard(async () => { await post(p.enabled ? "disable" : "enable", { name: p.package }); load(); })}>${p.enabled ? "Disable" : "Enable"}</button>`))}${!d.packages.length && html`<p class="mute">None installed.</p>`}
    <h2>In Claude Code (~/.claude.json)</h2>${d.claude.map((s) => row(s.name, s.url || s.command + " " + s.args.join(" "), html`<span class="tag">${s.type}</span>`))}${!d.claude.length && html`<p class="mute">None.</p>`}
    <h2>Only for this UI's chats</h2>${d.ui.map((s) => row(s.name, s.command + " " + s.args.join(" "), html`<button class="btn bad" onClick=${guard(async () => { await post("mcp/remove", { name: s.name }); load(); })}>${Ic("trash", 14)}</button>`))}
    <div class="card"><div class="row"><input placeholder="name" value=${f.name} onInput=${(e) => setF({ ...f, name: e.target.value })}/><input placeholder="command (npx, uvx…)" value=${f.command} onInput=${(e) => setF({ ...f, command: e.target.value })}/><input class="grow" placeholder="arguments, space separated" value=${f.args} onInput=${(e) => setF({ ...f, args: e.target.value })}/>
      <button class="btn acc" onClick=${guard(async () => { await post("mcp/add", { name: f.name, command: f.command, args: f.args.split(/\s+/).filter(Boolean) }); setF({ name: "", command: "", args: "" }); load(); })}>${Ic("plus", 14)} Add</button></div></div></div>`;
}

// ---------- vault
function Vault({ embedded }) {
  const [d, setD] = useState(null), [f, setF] = useState({ url: "", user: "", token: "" });
  const load = guard(async () => setD((await api("vault")).vault));
  useEffect(() => { load(); }, []);
  return html`<div class=${embedded ? "" : "page"}>${embedded ? html`<h2>Vault</h2><p class="sub">Git logins for hubs and package repos. Stored by aihub in your OS credential store; this page never sees them again.</p>` : html`<h1>Vault</h1><p class="sub">Git logins for hubs and package repos. Stored by aihub in your OS credential store; this page never sees them again.</p>`}
    ${d && d.backend && html`<p class="mute small">Backend: ${d.backend}</p>`}
    ${((d && d.remotes) || []).map((r) => html`<div class="card row"><span class="nico" style="width:32px;height:32px;border-radius:9px;background:var(--soft2);display:grid;place-items:center">${Ic("key", 16)}</span><div class="grow"><b>${r.host}</b> <span class="mute">${r.username}</span> <span class=${"tag " + (r.login === "stored" ? "good" : "bad")}>${r.login}</span></div>
      <button class="btn" onClick=${guard(async () => { const t = await post("vault/test", { host: r.host }); toast(JSON.stringify((t.result || [])[0] || {}).slice(0, 120)); })}>Test</button>
      <button class="btn bad" onClick=${guard(async () => { if (confirm("Remove login for " + r.host + "?")) { await post("vault/remove", { host: r.host }); load(); } })}>${Ic("trash", 14)}</button></div>`)}
    <h2>Add a login</h2><div class="card row"><input class="grow" placeholder="https://git.example.com" value=${f.url} onInput=${(e) => setF({ ...f, url: e.target.value })}/><input placeholder="user" value=${f.user} onInput=${(e) => setF({ ...f, user: e.target.value })}/>
      <input type="password" autocomplete="off" placeholder="token" value=${f.token} onInput=${(e) => setF({ ...f, token: e.target.value })}/>
      <button class="btn acc" onClick=${guard(async () => { await post("vault/add", { url: f.url, user: f.user, token: f.token }); setF({ url: "", user: "", token: "" }); toast("Stored"); load(); })}>Save</button></div></div>`;
}

// ---------- knowledge (soul + memory notes)
function Knowledge({ focus }) {
  const [notes, setNotes] = useState([]), [cur, setCur] = useState("soul"), [body, setBody] = useState(""), [dirty, setDirty] = useState(false), [nn, setNn] = useState("");
  const list = guard(async () => setNotes((await api("knowledge")).notes));
  const open = guard(async (n) => { setBody((await api("knowledge/note?name=" + n)).body); setCur(n); setDirty(false); });
  useEffect(() => { list(); open(focus && /^[a-z0-9][a-z0-9._-]*$/.test(focus) ? focus : "soul"); }, []);
  const save = guard(async () => { const r = await put("knowledge/note", { name: cur, body }); setDirty(false); toast(r.synced === false ? "Saved locally (hub not reachable)" : "Saved"); list(); });
  const names = ["soul", ...notes.map((n) => n.name).filter((n) => n !== "soul")];
  return html`<div class="page" style="max-width:1100px"><h1>Knowledge</h1><p class="sub">Soul and notes are your assistant's memory. New chats read them once and freeze them so the prompt cache stays warm; use the refresh button in a chat to pick up changes.</p>
    <div class="row" style="align-items:flex-start"><div style="width:220px"><div class="card" style="padding:6px">${names.map((n) => html`<button class=${"item" + (n === cur ? " on" : "")} onClick=${() => open(n)}>${Ic(n === "soul" ? "sparkles" : "note", 15)}<span class="t">${n}</span></button>`)}</div>
      <div class="row"><input class="grow" placeholder="new-note" value=${nn} onInput=${(e) => setNn(e.target.value)}/><button class="btn" onClick=${() => { if (/^[a-z0-9][a-z0-9._-]*$/.test(nn)) { setCur(nn); setBody(""); setDirty(true); setNn(""); } else toast("lowercase letters, digits, . _ -"); }}>${Ic("plus", 14)}</button></div>
      <button class="btn" style="margin-top:10px" onClick=${guard(async () => { await post("knowledge/pull"); toast("Pulled from hub"); list(); open(cur); })}>${Ic("download", 14)} Pull from hub</button></div>
      <div class="grow"><textarea rows="24" style="font:13px var(--mono)" value=${body} onInput=${(e) => { setBody(e.target.value); setDirty(true); }}></textarea>
        <div class="row" style="margin-top:10px"><span class="mute small grow">${cur}.md${dirty ? " · unsaved" : ""}</span>${cur !== "soul" && html`<button class="btn bad" onClick=${guard(async () => { if (confirm("Delete " + cur + "?")) { await post("knowledge/delete", { name: cur }); list(); open("soul"); } })}>${Ic("trash", 14)}</button>`}<button class="btn acc" disabled=${!dirty} onClick=${save}>Save</button></div></div></div></div>`;
}

// ---------- works: palette, canvas, inspector
const NW = 230;
// first grid slot (300 x 140 apart, 4 rows per column) where a new 230 x 100 card touches no other card
const freeSpot = (nodes) => { for (let col = 0; col < 12; col++) for (let row = 0; row < 4; row++) { const x = 40 + col * 300, y = 40 + row * 140; if (!nodes.some((n) => Math.abs(n.x - x) < NW + 16 && Math.abs(n.y - y) < 112)) return { x, y }; } return { x: 40, y: 40 + nodes.length * 140 }; };
const nodeIcon = (type, sp) => ({ "start.manual": "play", "start.cron": "clock", "start.webhook": "inbox", "ai.claude": "sparkles", "flow.filter": "filter", "flow.template": "template", "flow.note": "note", "flow.memory": "layers", "start.teams": "chat", "send.teams": "send", "io.read_file": "file", "io.request": "link", "git.read": "code", "flow.output": "check" }[type] || (sp && sp.kind === "trigger" ? "zap" : "package"));
const nodeKind = (type, sp) => (sp && sp.kind === "trigger" ? "k-trigger" : type === "ai.claude" ? "k-ai" : type.startsWith("pkg:") ? "k-pkg" : "");
const summary = (n) => { const c = n.config || {}; return String(c.prompt || c.url || c.path || c.schedule || (c.source ? "memory: " + c.source : "") || c.title || c.regex || c.template || c.text || c.filter || "").slice(0, 90); };
function Palette({ cat, onPick, close, more }) {
  const [q, setQ] = useState(""), [hi, setHi] = useState(0), ref = useRef();
  useEffect(() => ref.current && ref.current.focus(), []);
  const all = Object.values(cat).filter((s) => !s.hidden && (!q || (s.title + " " + s.desc + " " + s.group + " " + s.type).toLowerCase().includes(q.toLowerCase())));
  const groups = {}; all.forEach((s) => (groups[s.group] ||= []).push(s));
  const flat = Object.values(groups).flat();
  const key = (e) => { if (e.key === "ArrowDown") { e.preventDefault(); setHi((i) => Math.min(flat.length - 1, i + 1)); } else if (e.key === "ArrowUp") { e.preventDefault(); setHi((i) => Math.max(0, i - 1)); } else if (e.key === "Enter" && flat[hi]) onPick(flat[hi].type); };
  return html`<${Modal} close=${close}><div style="margin:-22px;"><div class="palette" style="width:auto;border:0;border-radius:18px;box-shadow:none;animation:none"><div class="q">${Ic("search", 17)}<input ref=${ref} placeholder="Add a node: start, Claude, filter, send…" value=${q} onInput=${(e) => { setQ(e.target.value); setHi(0); }} onKeyDown=${key}/><span class="kbd">esc</span></div>
    <div class="plist">${Object.entries(groups).map(([g, items]) => html`<h5>${g}</h5>${items.map((s) => html`<button class=${"opt" + (flat[hi] === s ? " hi" : "")} onMouseEnter=${() => setHi(flat.indexOf(s))} onClick=${() => onPick(s.type)}><span class="oi">${Ic(nodeIcon(s.type, s), 16)}</span><span class="grow"><b>${s.title}</b><span class="d">${s.desc}</span></span><span class=${"tag" + (s.kind === "trigger" ? " acc" : "")}>${s.kind === "trigger" ? "start" : "step"}</span></button>`)}`)}
      ${!flat.length && html`<p class="mute" style="padding:16px">No node matches “${q}”. Install a node package to add more.</p>`}</div>
    ${more && html`<div class="row" style="padding:8px 12px;border-top:1px solid var(--line)"><span class="mute small grow">Need another node?</span><button class="btn" onClick=${more}>${Ic("layers", 13)} Create, share, install nodes</button></div>`}</div></div><//>`;
}

// ---------- prompt box: suggestions + highlight (what a field may suggest comes from its `assist` list: input, data, file, skill)
let slashCache = null;
const skillNames = () => { if (!slashCache) slashCache = api("slash").then((d) => [...(d.claude || []), ...(d.packages || [])].map((c) => ({ name: c.name, description: c.description || "" }))).catch(() => []); return slashCache; };
const escHtml = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const slugOf = (t) => String(t || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 30) || "node";
// the name `{{in.<slug>}}` uses for each node: the same rule as works.names_of (unique per work, never a bare number)
function nodeSlugs(w, cat) {
  const out = {}, used = new Set();
  w.nodes.forEach((n) => { const base = slugOf(n.label || (cat[n.type] && cat[n.type].title) || n.type || n.id); let sg = base, i = 1; while (used.has(sg) || /^\d+$/.test(sg)) { i++; sg = base + "_" + i; } used.add(sg); out[n.id] = sg; });
  return out;
}
const OUT_TAG = { text: "text", path: "path", json: "json", url: "link" };
const OUTPUT_HINT = "\n\nWrite the result to a file (a dashboard or site: a folder with index.html) and end your answer with this last line:\nOUTPUT: /full/path";
function highlight(text, assist, skills) {
  const re = /\{\{\s*(input|trigger|in\.\w+|data\.[\w.]*)\s*\}\}|(^|\s)(@[^\s]+)|(^|\s)\/([\w:.-]+)|^(OUTPUT:)/gm;
  let out = "", last = 0, m;
  while ((m = re.exec(text))) {
    if (m[1] !== undefined && assist.includes(m[1].startsWith("data") ? "data" : "input")) { out += escHtml(text.slice(last, m.index)) + `<mark class="t-var">${escHtml(m[0])}</mark>`; last = m.index + m[0].length; }
    else if (m[3] && assist.includes("file")) { out += escHtml(text.slice(last, m.index + m[2].length)) + `<mark class="t-file">${escHtml(m[3])}</mark>`; last = m.index + m[0].length; }
    else if (m[5] && assist.includes("skill") && skills.some((k) => k.name === m[5])) { out += escHtml(text.slice(last, m.index + m[4].length)) + `<mark class="t-skill">/${escHtml(m[5])}</mark>`; last = m.index + m[0].length; }
    else if (m[6] && assist.includes("input")) { out += escHtml(text.slice(last, m.index)) + `<mark class="t-out">OUTPUT:</mark>`; last = m.index + m[0].length; }
  }
  return out + escHtml(text.slice(last)) + "\n";
}
// where the caret is inside a textarea (a hidden copy of the text up to it), so the list opens under what you are typing
function caretXY(el, pos) {
  const cs = getComputedStyle(el), d = document.createElement("div");
  ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "borderTopWidth", "borderLeftWidth", "boxSizing", "overflowWrap", "tabSize"].forEach((k) => (d.style[k] = cs[k]));
  Object.assign(d.style, { position: "absolute", visibility: "hidden", top: "0", left: "-9999px", width: el.offsetWidth + "px", whiteSpace: "pre-wrap" });
  d.textContent = el.value.slice(0, pos); const sp = document.createElement("span"); sp.textContent = "​"; d.appendChild(sp); document.body.appendChild(d);
  const r = { x: sp.offsetLeft - el.scrollLeft, y: sp.offsetTop - el.scrollTop, h: parseFloat(cs.lineHeight) || 18 }; d.remove(); return r;
}
// ranked match: prefix of the name, then a word start, then anywhere (name or hint)
function rank(items, q) {
  if (!q) return items;
  const s = (it) => { const l = it.label.toLowerCase(), l2 = l.replace(/^(data|in)\./, ""), h = (it.hint || "").toLowerCase(); return l.startsWith(q) || l2.startsWith(q) ? 0 : l.includes("." + q) || l.includes("_" + q) ? 1 : l.includes(q) ? 2 : h.includes(q) ? 3 : 9; };
  return items.map((it) => [s(it), it]).filter((x) => x[0] < 9).sort((a, b) => a[0] - b[0]).map((x) => x[1]);
}
const Mark = ({ t, q }) => { const i = q ? t.toLowerCase().indexOf(q) : -1; return i < 0 ? t : html`${t.slice(0, i)}<b>${t.slice(i, i + q.length)}</b>${t.slice(i + q.length)}`; };
function PromptBox({ f, val, set, ctx, one }) {
  const assist = f.assist || [], ta = useRef(), bk = useRef(), list = useRef(), [pop, setPop] = useState(null), [skills, setSkills] = useState([]), seq = useRef(0), tm = useRef();
  useEffect(() => { if (assist.includes("skill")) skillNames().then(setSkills); }, []);
  useEffect(() => { if (one && ta.current) { ta.current.style.height = "auto"; ta.current.style.height = Math.max(36, ta.current.scrollHeight) + "px"; } }, [val]);
  useEffect(() => { const el = list.current && list.current.querySelector(".hi"); if (el) el.scrollIntoView({ block: "nearest" }); }, [pop && pop.hi, pop && pop.items.length]);
  const varItems = () => {                                  // everything {{ }} can say here, grouped: the input, each parent, run, data
    const items = [], n = ctx.parents.length;
    if (assist.includes("input")) {
      items.push({ label: "input", insert: "{{input}}", group: "Input of this node", hint: n > 1 ? "all " + n + " parents, one “## From …” block each" : n ? "text from " + ctx.last : "text from the previous node" });
      items.push({ label: "trigger", insert: "{{trigger}}", group: "Input of this node", hint: "text that started the run" });
      if (n > 1) ctx.parents.forEach((p) => items.push({ label: "in." + p.slug, insert: "{{in." + p.slug + "}}", group: "One parent only", hint: "only " + p.name + " · " + (OUT_TAG[p.out] || "text") }));
    }
    if (assist.includes("data")) ctx.keys.forEach((k) => items.push({ label: "data." + k.name, insert: "{{data." + k.name + "}}", group: "Data", hint: k.from + (k.name === "path" ? " · a file or folder path" : "") }));
    return items;
  };
  const open = (from, to, items, kind, q) => setPop((p) => ({ from, to, items, kind, q: q || "", hi: 0, x: null }));
  const place = (el, at) => { const c = caretPos(el, at); setPop((p) => p && { ...p, ...c }); };
  const find = async (el, force) => {
    const pos = el.selectionStart, before = el.value.slice(0, pos), my = ++seq.current;
    let m;
    if (force === "var") { open(pos, pos, varItems(), "var"); return place(el, pos); }
    if (assist.some((a) => a === "input" || a === "data") && (m = /\{\{\s*([\w.]*)$/.exec(before))) {
      const q = m[1].toLowerCase(), after = /^\s*\}\}/.exec(el.value.slice(pos));
      open(pos - m[0].length, pos + (after ? after[0].length : 0), rank(varItems(), q), "var", q);
      return place(el, pos);
    }
    if (assist.includes("file") && (m = /(^|\s)@([^\s]*)$/.exec(before))) {
      clearTimeout(tm.current);
      tm.current = setTimeout(async () => {
        const [d, sc] = await Promise.all([api("works/mentions?q=" + encodeURIComponent(m[2])).catch(() => ({ items: [] })), /^[\w:.@-]*$/.test(m[2]) ? api("works/sources?q=" + encodeURIComponent(m[2])).catch(() => ({ items: [] })) : { items: [] }]);
        if (my !== seq.current) return;
        setPop({ from: pos - m[2].length - 1, to: pos, items: [...sc.items.slice(0, 12).map((i) => ({ label: "@" + i.name, insert: "@" + i.name + " ", hint: i.source === "space" ? "space" : "note" })), ...d.items.map((i) => ({ label: "@" + i.path, insert: "@" + i.path + (i.dir ? "" : " "), hint: i.dir ? "folder" : "file", dir: i.dir }))], kind: "file", q: "", hi: 0, ...caretPos(el, pos) });
      }, 90);
      return;
    }
    if (assist.includes("skill") && (m = /(^|\s)\/([\w:.-]*)$/.exec(before))) {
      const q = m[2].toLowerCase();
      setPop({ from: pos - m[2].length - 1, to: pos, items: skills.filter((k) => k.name.toLowerCase().includes(q)).slice(0, 30).map((k) => ({ label: "/" + k.name, insert: "/" + k.name + " ", hint: k.description.slice(0, 80) })), kind: "skill", q, hi: 0, ...caretPos(el, pos) });
      return;
    }
    setPop(null);
  };
  const caretPos = (el, at) => { const c = caretXY(el, at); return { x: Math.max(0, Math.min(c.x, el.offsetWidth - 330)), y: c.y + c.h + 6 }; };
  const put = (text, from, to) => {                          // replace [from,to) and keep the caret after it
    const el = ta.current, v = el.value, next = v.slice(0, from) + text + v.slice(to);
    set(next); setPop(null);
    requestAnimationFrame(() => { const at = from + text.length; el.focus(); el.setSelectionRange(at, at); });
  };
  const accept = (it) => { put(it.insert, pop.from, pop.to); if (it.dir) setTimeout(() => ta.current && find(ta.current), 30); };
  const chip = (ins) => { const el = ta.current, a = el ? el.selectionStart : val.length, b = el ? el.selectionEnd : val.length; put(ins, a, b); };
  const onInput = (e) => {
    const el = e.target; let v = el.value;
    if (one && /\n/.test(v)) { v = v.replace(/\s*\n\s*/g, " "); el.value = v; }
    const pos = el.selectionStart;
    if (e.nativeEvent && e.nativeEvent.inputType === "insertText" && e.nativeEvent.data === "{" && v.slice(pos - 2, pos) === "{{" && !/^\s*\}\}/.test(v.slice(pos)) && assist.some((a) => a === "input" || a === "data")) {
      v = v.slice(0, pos) + "}}" + v.slice(pos); el.value = v; el.setSelectionRange(pos, pos);       // typing {{ closes itself
    }
    set(v); find(el);
  };
  const key = (e) => {
    if (e.key === " " && (e.ctrlKey || e.metaKey) && assist.some((a) => a === "input" || a === "data")) { e.preventDefault(); return find(e.target, "var"); }
    if (one && e.key === "Enter" && !(pop && pop.items.length)) { e.preventDefault(); return; }
    if (!pop || !pop.items.length) return;
    const hi = pop.hi || 0, n = pop.items.length;
    if (e.key === "ArrowDown") { e.preventDefault(); setPop({ ...pop, hi: (hi + 1) % n }); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setPop({ ...pop, hi: (hi - 1 + n) % n }); }
    else if (e.key === "PageDown") { e.preventDefault(); setPop({ ...pop, hi: Math.min(n - 1, hi + 5) }); }
    else if (e.key === "PageUp") { e.preventDefault(); setPop({ ...pop, hi: Math.max(0, hi - 5) }); }
    else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); accept(pop.items[hi]); }
    else if (e.key === "Escape") { e.stopPropagation(); setPop(null); }
  };
  const hasOut = /^OUTPUT:/m.test(val), ai = ctx.type && ctx.type.startsWith("ai.") && assist.includes("skill");
  const chips = assist.some((a) => a === "input" || a === "data") ? varItems().filter((i) => i.label === "input" || i.label.startsWith("in.") || i.label.startsWith("data.")).slice(0, 8) : [];
  let lastGroup = "";
  return html`<div class=${"pbx" + (one ? " one" : "")}><div class="pbx-box"><div class="pbx-hl" ref=${bk} aria-hidden="true" dangerouslySetInnerHTML=${{ __html: highlight(val, assist, skills) }}></div>
    <textarea ref=${ta} rows="1" style=${one ? "" : "min-height:112px"} value=${val} spellcheck="false" onInput=${onInput} onKeyDown=${key} onClick=${(e) => find(e.target)} onKeyUp=${(e) => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") find(e.target); }} onBlur=${() => setTimeout(() => setPop(null), 120)}
      onScroll=${(e) => { bk.current.scrollTop = e.target.scrollTop; }}></textarea>
    ${pop && pop.items.length ? html`<div class="pbx-pop" ref=${list} style=${pop.x == null ? "" : `left:${Math.max(0, Math.min(pop.x, 99999))}px;top:${pop.y}px`}>${pop.items.map((it, i) => { const g = it.group && it.group !== lastGroup ? ((lastGroup = it.group), html`<div class="pbx-g">${it.group}</div>`) : ""; return html`${g}<button class=${"opt" + (i === pop.hi ? " hi" : "")} onMouseMove=${() => pop.hi !== i && setPop({ ...pop, hi: i })} onMouseDown=${(e) => { e.preventDefault(); accept(it); }}><span class=${"pbx-k " + pop.kind}><${Mark} t=${it.label} q=${pop.kind === "var" ? pop.q : ""}/></span><span class="mute small">${it.hint}</span></button>`; })}<div class="pbx-foot">↑↓ choose · Enter insert · Esc close</div></div>`
      : pop && pop.kind === "var" && pop.q ? html`<div class="pbx-pop empty" style=${pop.x == null ? "" : `left:${pop.x}px;top:${pop.y}px`}><span class="mute small">Nothing called “${pop.q}” here. Connect a node first, or try {{input}}.</span></div>` : ""}</div>
    ${chips.length ? html`<div class="pbx-chips"><span class="mute small">Insert</span>${chips.map((c) => html`<button type="button" class=${"chip " + (c.label === "input" ? "in" : "")} title=${c.hint} onMouseDown=${(e) => e.preventDefault()} onClick=${() => chip(c.insert)}>${c.label === "input" ? "input" : c.label.replace(/^in\./, "").replace(/^data\./, "data ")}</button>`)}<span class="mute small" title="Ctrl+Space opens the full list">or type {{</span></div>` : ""}
    ${ai && ctx.needsPath ? html`<div class=${"pbx-need" + (hasOut ? " ok" : "")}>${hasOut ? Ic("check", 13) : Ic("alert", 13)}<span class="grow">${hasOut ? "This prompt asks for an OUTPUT: path, so " + ctx.needsPath + " can publish it." : ctx.needsPath + " after this node needs a file or folder path. Claude only answers with text, so it must end with an OUTPUT: line."}</span>${!hasOut && html`<button type="button" class="btn" onClick=${() => set(val.replace(/\s+$/, "") + OUTPUT_HINT)}>Add the instruction</button>`}</div>`
      : ai && !hasOut ? html`<div class="pbx-chips"><button type="button" class="chip" onMouseDown=${(e) => e.preventDefault()} onClick=${() => set(val.replace(/\s+$/, "") + OUTPUT_HINT)} title="Only needed when a later node must open a file Claude makes">+ Make Claude hand on a file (OUTPUT: path)</button></div>` : ""}
    <div class="pbx-tip mute small">${[assist.includes("file") && html`<span><mark class="t-file">@file</mark> a path</span>`, assist.includes("skill") && html`<span><mark class="t-skill">/skill</mark> a skill</span>`, assist.some((a) => a === "input" || a === "data") && html`<span>Ctrl+Space: all variables</span>`]}</div></div>`;
}
function Field({ f, v, set, boot, stored, ctx }) {
  const val = v === undefined ? f.default ?? "" : v;
  const common = { value: val, onInput: (e) => set(e.target.value) };
  return html`<div class="field"><label>${f.label}${f.required ? " *" : ""}</label>
    ${(f.type === "textarea" || (f.type === "text" && !f.secret)) && f.assist && f.assist.length && ctx ? html`<${PromptBox} f=${f} val=${String(val)} set=${set} ctx=${ctx} one=${f.type === "text"}/>`
    : f.type === "folder" ? html`<div class="row" style="gap:6px">${f.assist && f.assist.length && ctx ? html`<div class="grow"><${PromptBox} f=${f} val=${String(val)} set=${set} ctx=${ctx} one=${true}/></div>` : html`<input class="grow" type="text" ...${common}/>`}<button class="btn" type="button" onClick=${async () => { const p = await nativePick({ dirs: true, start: String(val).replace(/^@/, ""), title: "Choose a working folder" }); if (p) set(p); }}>${Ic("folder", 14)} Browse…</button></div>`
    : f.type === "code" ? html`<textarea class="codebox" rows="16" spellcheck="false" autocapitalize="off" autocomplete="off" wrap="off" ...${common} onKeyDown=${(e) => { if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.metaKey) { e.preventDefault(); const t = e.target, a = t.selectionStart; set(t.value.slice(0, a) + "    " + t.value.slice(t.selectionEnd)); requestAnimationFrame(() => { t.selectionStart = t.selectionEnd = a + 4; }); } }}></textarea>`
    : f.type === "textarea" ? html`<textarea rows="5" ...${common}></textarea>`
    : f.type === "checkbox" ? html`<${Switch} on=${!!val} onChange=${set} label=${f.label}/>`
    : f.type === "model" ? html`<select value=${val} onChange=${(e) => set(e.target.value)}>${boot.models.models.map((m) => html`<option value=${m.id}>${m.label}${m.real ? " · " + m.real : ""}</option>`)}</select>`
    : f.type === "effort" ? html`<select value=${val} onChange=${(e) => set(e.target.value)}>${LEVELS.map((x) => html`<option value=${x}>${x || "default"}</option>`)}</select>`
    : f.type === "mode" ? html`<select value=${val} onChange=${(e) => set(e.target.value)}>${boot.modes.map((m) => html`<option value=${m}>${(MODE_INFO[m] || [0, m])[1]}</option>`)}</select>`
    : f.type === "select" ? html`<select value=${val} onChange=${(e) => set(e.target.value)}>${f.options.map((o) => html`<option>${o}</option>`)}</select>`
    : f.secret ? html`<input type="password" autocomplete="off" placeholder=${stored ? "•••••• stored (type to replace)" : ""} value=${stored ? "" : val} onInput=${(e) => set(e.target.value)}/>`
    : html`<input type=${f.type === "number" ? "number" : "text"} ...${common}/>`}
    ${f.help && html`<span class="mute small">${f.help}</span>`}</div>`;
}
function IoStrip({ node, spec, ctx }) {
  const ps = ctx.parents, many = ps.length > 1, trig = spec.kind === "trigger";
  const pathField = String((node.config || {}).path || "").trim(), bad = spec.needs === "path" && !ctx.canPath && !pathField;
  return html`<div class="io-strip">
    ${!trig && html`<div class="io-row"><span class="io-k">Receives</span><div class="grow">${ps.length ? ps.map((p, k) => html`<div class="io-p"><span class="tag">${OUT_TAG[p.out] || "text"}</span> <b>${p.name}</b> ${many && html`<code class="mute small">{{in.${p.slug}}}</code>`}</div>`) : html`<span class="mute small">Nothing connected yet: draw a line from another node.</span>`}
      ${many && html`<div class="mute small" style="margin-top:4px">Several inputs: <code>{{input}}</code> puts them one under another with a “## From …” heading, in the order the lines were drawn; <code>{{in.name}}</code> takes just one. The node waits for all of them.</div>`}</div></div>`}
    <div class="io-row"><span class="io-k">Produces</span><div class="grow"><span class="tag">${OUT_TAG[spec.out] || "text"}</span> <span class="small">${spec.out_note || "text for the next node"}</span></div></div>
    ${spec.needs && html`<div class=${"io-row" + (bad ? " warn" : "")}><span class="io-k">Needs</span><div class="grow small">${bad ? Ic("alert", 12) : Ic("check", 12)} ${spec.needs_note || "a " + spec.needs + " from the node before"}${bad ? html`<div>Nothing before this node gives a path yet. Add a “File / folder (path)” or “Read from git (path)” node, or a Claude task that ends with OUTPUT: /path.</div>` : ""}</div></div>`}</div>`;
}
function Inspector({ w, node, spec, cat, run, boot, hook, trig, upd, close, remove }) {
  const [tab, setTab] = useState("settings"), rn = run && run.nodes && run.nodes[node.id];
  const incoming = w.edges.filter((e) => e.to === node.id), nameOf = (n) => n.label || (cat[n.type] && cat[n.type].title) || n.type;
  const slugs = nodeSlugs(w, cat);
  const fctx = (() => {                                  // what the fields may suggest: each direct parent (in edge order), the data keys of every ancestor, what comes after
    const seen = new Set(), keys = [], anc = [], q = [node.id];
    while (q.length) { const id = q.pop(); w.edges.filter((e) => e.to === id).forEach((e) => { if (seen.has(e.from)) return; seen.add(e.from); q.push(e.from); const n = w.nodes.find((x) => x.id === e.from); if (!n) return; anc.push(n);
      if (n.type === "start.manual") String((n.config || {}).inputs || "").split("\n").map((l) => l.split("|")[0].trim()).filter(Boolean).forEach((k) => keys.push({ name: k, from: "input of " + nameOf(n) }));
      ((cat[n.type] || {}).outputs || []).forEach((k) => keys.push({ name: k, from: "from " + nameOf(n) })); }); }
    const parents = incoming.map((e) => w.nodes.find((x) => x.id === e.from)).filter(Boolean).map((n) => ({ id: n.id, name: nameOf(n), slug: slugs[n.id], out: (cat[n.type] || {}).out || "text", note: (cat[n.type] || {}).out_note || "" }));
    const down = new Set(), dq = [node.id]; while (dq.length) { const id = dq.pop(); w.edges.filter((e) => e.from === id).forEach((e) => { if (!down.has(e.to)) { down.add(e.to); dq.push(e.to); } }); }
    const need = [...down].map((id) => w.nodes.find((x) => x.id === id)).filter((n) => n && (cat[n.type] || {}).needs === "path").map(nameOf);
    const canPath = anc.some((n) => (cat[n.type] || {}).out === "path" || (n.type || "").startsWith("ai."));
    return { type: node.type, parents, last: parents.map((p) => p.name).join(", "), keys: keys.filter((k, i) => keys.findIndex((o) => o.name === k.name) === i), needsPath: need.length ? need[0] : "", canPath };
  })();
  const set = (fn) => upd((c) => (fn(c.nodes.find((q) => q.id === node.id)), c));
  return html`<aside class="insp" key=${node.id}><div class="insp-h"><span class="nico">${Ic(nodeIcon(node.type, spec), 17)}</span><input class="grow" placeholder=${spec ? spec.title : node.type} value=${node.label || ""} onInput=${(e) => set((n) => (n.label = e.target.value))}/><button class="ibtn" onClick=${close}>${Ic("x", 16)}</button></div>
    <div class="insp-t"><${Seg} small value=${tab} onChange=${setTab} items=${[{ id: "settings", label: "Settings" }, { id: "data", label: "Data" }, { id: "about", label: "About" }]}/></div>
    <div class="insp-b">${tab === "settings" ? html`${spec && html`<${IoStrip} node=${node} spec=${spec} ctx=${fctx}/>`}${spec && spec.guide && html`<details class="guide" open=${!Object.keys(node.config).length}><summary>${Ic("book", 14)} How to set this up</summary><div class="guide-b">${linkify(spec.guide)}</div></details>`}${spec ? spec.fields.map((f) => html`<${Field} key=${node.id + f.name} ctx=${fctx} f=${f} v=${node.config[f.name]} stored=${node.config[f.name] === "__stored__"} boot=${boot} set=${(v) => set((n) => (n.config[f.name] = v))}/>`) : html`<p class="tag bad">${Ic("alert", 12)} This node's package is not installed.</p>`}
        ${trig && trig[node.id] && (() => { const t = trig[node.id]; return html`<div class="field"><label>Trigger process</label><span class=${"tag " + (t.running ? "good" : "bad")}>${t.running ? "Running" : "Stopped" + (t.exit != null ? " (exit " + t.exit + ")" : "")}</span>${t.fails ? html` <span class="mute small">${t.fails} restart${t.fails > 1 ? "s" : ""}</span>` : ""}${t.stderr && t.stderr.length ? html`<pre class="small" style="max-height:160px;overflow:auto;white-space:pre-wrap">${t.stderr.join("\n")}</pre>` : ""}</div>`; })()}
        ${node.type === "start.webhook" && html`<div class="field"><label>Webhook URL (local only)</label><input readonly value=${hook || "Save the work first"} onClick=${(e) => e.target.select()}/><span class="mute small">POST text or {"text":…,"data":{…}} here. The work must be On.</span></div>`}
        ${incoming.length ? html`<h2>Inputs</h2>${incoming.map((e) => html`<div class="row small" style="margin:4px 0"><span class="grow">${Ic("link", 12)} from ${(w.nodes.find((q) => q.id === e.from) || {}).label || ((w.nodes.find((q) => q.id === e.from) || {}).type)}</span><button type="button" class="lnk" onClick=${() => upd((c) => ((c.edges = c.edges.filter((x) => !(x.from === e.from && x.to === e.to))), c))}>disconnect</button></div>`)}` : ""}
        <div style="margin-top:18px"><button class="btn bad" onClick=${remove}>${Ic("trash", 14)} Delete node</button></div>`
      : tab === "data" ? (rn ? html`<dl class="kv"><dt>Status</dt><dd><span class=${"tag " + (rn.status === "ok" ? "good" : rn.status === "error" ? "bad" : "")}>${rn.status}</span> ${rn.note || ""}</dd>${rn.ended && rn.t0 ? html`<dt>Took</dt><dd>${(rn.ended - rn.t0).toFixed(1)}s</dd>` : ""}</dl>
          ${rn.error && html`<h2>Error</h2><div class="io" style="color:var(--bad)">${rn.error}</div>`}${rn.inputs && rn.inputs.length > 1 ? html`<h2>Inputs (${rn.inputs.length})</h2>${rn.inputs.map((x, k) => html`<div class="io-in"><div class="io-h"><span class="tag">${k + 1}</span><b>${x.label}</b><span class="tag">${OUT_TAG[x.kind] || "text"}</span><code class="mute small">{{in.${(fctx.parents.find((p) => p.id === x.from) || {}).slug || k + 1}}}</code></div><div class="io">${x.text}</div></div>`)}`
            : rn.input && html`<h2>Input</h2><div class="io">${rn.input}</div>`}${rn.paths && rn.paths.length ? html`<h2>Paths passed on</h2><div class="io">${rn.paths.join("\n")}</div><span class="mute small">Claude may read these folders; a Publish node can send them.</span>` : ""}${rn.output && html`<h2>Output</h2><div class="io">${rn.output}</div>`}${rn.log && html`<h2>Log</h2><div class="io">${rn.log}</div>`}`
        : html`<div class="empty" style="height:200px"><div>${Ic("layers", 24)}<div>Run the work, then pick a run<br/>to see this node's data.</div></div></div>`)
      : html`<dl class="kv"><dt>Type</dt><dd class="mono">${node.type}</dd><dt>Kind</dt><dd>${spec ? (spec.kind === "trigger" ? "Start (produces events)" : "Step") : "unknown"}</dd>${spec && spec.package ? html`<dt>Package</dt><dd>${spec.package}</dd>` : ""}<dt>Id</dt><dd class="mono">${node.id}</dd></dl><p class="mute">${spec && spec.desc}</p>${spec && spec.guide && html`<div class="guide-b">${linkify(spec.guide)}</div>`}`}</div></aside>`;
}
const Step = ({ n, state, title, children }) => html`<div class=${"step " + state}><span class="sn">${state === "ok" ? Ic("check", 13) : state === "bad" ? Ic("x", 13) : n}</span><div class="sb"><b>${title}</b>${children}</div></div>`;
function PublishSteps({ r, boot, close }) {
  const [ctx, setCtx] = useState(boot.context), [o, setO] = useState({ indirect: true, bump: false }), [run, setRun] = useState(null), host = useRef(), tm = useRef();
  const cmd = `aihub dev publish${o.indirect || r.project ? " -i" : ""}${o.bump ? " --bump patch" : ""} ${r.path}`;
  const recheck = guard(async () => { setCtx((await api("boot")).context); });
  const go = guard(async () => {
    setRun({ state: "run" });
    try { tm.current = await runInstall(host.current, r.project ? { project: r.project, bump: o.bump } : { publish: r.name, indirect: o.indirect, bump: o.bump }, { post, api }, (code) => setRun({ state: code === 0 ? "ok" : "bad", code })); }
    catch (e) { setRun(null); throw e; }
  });
  useEffect(() => () => tm.current && tm.current.stop && tm.current.stop(), []);
  const copy = (t) => navigator.clipboard.writeText(t).then(() => toast("Copied"));
  return html`<${Modal} wide=${true} close=${() => run && run.state === "run" ? null : close()}><h1>Publish ${r.name} ${r.version}</h1><p class="sub">${r.project ? "Uploads this project folder straight to the hub (no git needed). Check it holds no secrets first." : "Three steps. Secrets were removed from the package."}</p>
    <div class="steps">
      <${Step} n="1" state=${r.check && r.check.ok === false ? "bad" : "ok"} title=${r.project ? "Project with aihub.toml" : "Package built"}><div class="mute small">${r.check && r.check.ok === false ? r.check.text : "aihub dev validate: looks good"} · <code>${r.path}</code></div><//>
      <${Step} n="2" state=${ctx.signed_in ? "ok" : "todo"} title=${ctx.signed_in ? "Signed in as " + ctx.username : "Sign in to a hub"}>
        ${ctx.signed_in ? html`<div class="mute small">${ctx.hub}</div>` : html`<div class="mute small">Publishing needs an account that may publish. <a href="#/settings" onClick=${close}>Open Settings > Hub account</a> (or run <code>aihub login</code>), then <button type="button" class="lnk" onClick=${recheck}>check again</button>.</div>`}<//>
      <${Step} n="3" state=${run && run.state === "ok" ? "ok" : run && run.state === "bad" ? "bad" : "todo"} title="Run the publish command">
        <div class="pubopts"><label class="row" style="gap:7px"><input type="checkbox" checked=${o.indirect} disabled=${!!run} onChange=${(e) => setO({ ...o, indirect: e.target.checked })}/> Upload to the hub <span class="mute small">(-i: no git repository or git account needed)</span></label>
          <label class="row" style="gap:7px"><input type="checkbox" checked=${o.bump} disabled=${!!run} onChange=${(e) => setO({ ...o, bump: e.target.checked })}/> Bump the patch version first</label></div>
        <div class="cmd"><code>${cmd}</code><button class="ibtn" title="Copy" aria-label="Copy" onClick=${() => copy(cmd)}>${Ic("copy", 14)}</button></div>
        <div class="termbox" ref=${host} style=${run ? "margin-top:10px" : "display:none"}></div>
        ${run && run.state === "ok" && html`<p class="tag good" style="margin-top:10px">${Ic("check", 12)} Published. Anyone with access can now run <code>aihub install ${r.name}</code>.</p>`}
        ${run && run.state === "bad" && html`<p class="tag bad" style="margin-top:10px">${Ic("alert", 12)} The command ended with exit code ${run.code}. Read the terminal above, fix it, and run it again.</p>`}<//>
    </div>
    <div class="row" style="margin-top:16px">${run && run.state === "run" && html`<span class="shine">Publishing…</span>`}<span class="grow"></span>
      ${run && run.state === "run" ? html`<button class="btn" onClick=${() => { tm.current && tm.current.stop(); setRun(null); }}>Stop</button>`
        : html`<button class="btn" onClick=${close}>${run && run.state === "ok" ? "Done" : "Close"}</button><button class="btn acc" disabled=${!ctx.signed_in || (r.check && r.check.ok === false)} onClick=${() => { tm.current && tm.current.stop && tm.current.stop(); const h = host.current; if (h) h.innerHTML = ""; go(); }}>${Ic("play", 13)} ${run ? "Run again" : "Publish"}</button>`}</div><//>`;
}
const Risks = ({ list }) => list && list.length ? html`<div class="risks">${list.map((r) => html`<span class="risk">${Ic("shield", 11)} ${r}</span>`)}</div>` : null;
function ShareForm({ id, info, close, done, node }) {
  const [v, setV] = useState({ ...info, authors: info.authors.join(", "), tags: info.tags.join(", "), packages: (info.packages || []).join(", "), commands: (info.commands || []).join(", ") }), [tab, setTab] = useState("meta"), [busy, setBusy] = useState(false);
  const set = (p) => setV({ ...v, ...p }), list = (t) => t.split(",").map((x) => x.trim()).filter(Boolean);
  const osTog = (o) => set({ os: v.os.includes(o) ? v.os.filter((x) => x !== o) : [...v.os, o] });
  const go = guard(async () => { setBusy(true); try { done(await post(node ? "nodes/export" : "works/export", { id, name: v.name, version: v.version, description: v.description, authors: list(v.authors), tags: list(v.tags), license: v.license, os: v.os, packages: list(v.packages), commands: list(v.commands), readme: v.readme })); } finally { setBusy(false); } });
  return html`<${Modal} wide=${true} close=${close}><h1>${node ? "Share this node" : "Share as a workflow"}</h1><p class="sub">${node ? "Makes an aihub package (type node) you can publish. Anyone who installs it gets the node in Works > Add node. Do not put secrets in the program; password fields are filled in by each user." : "Makes an aihub package (type workflow) you can publish. Secrets are removed from it."}</p>
    <${Seg} small value=${tab} onChange=${setTab} items=${[{ id: "meta", label: "Details", icon: "info" }, { id: "readme", label: "README", icon: "book" }]}/>
    <${Risks} list=${info.risks}/>
    <div style="margin-top:12px;max-height:52vh;overflow:auto;padding:2px">${tab === "meta" ? html`<div class="fb-grid" style="padding:0">
      <label class="fb-f"><span>Package name</span><input value=${v.name} onInput=${(e) => set({ name: e.target.value })}/></label>
      <label class="fb-f"><span>Version</span><input value=${v.version} onInput=${(e) => set({ version: e.target.value })}/></label>
      <label class="fb-f wide"><span>Short description</span><input maxlength="300" value=${v.description} onInput=${(e) => set({ description: e.target.value })}/></label>
      <label class="fb-f"><span>Authors (comma separated)</span><input value=${v.authors} placeholder="Ada Lovelace <ada@example.com>" onInput=${(e) => set({ authors: e.target.value })}/></label>
      <label class="fb-f"><span>License</span><input value=${v.license} onInput=${(e) => set({ license: e.target.value })}/></label>
      <label class="fb-f wide"><span>Tags (comma separated)</span><input value=${v.tags} onInput=${(e) => set({ tags: e.target.value })}/></label>
      <div class="fb-f wide"><span>Works on</span><div class="row" style="gap:16px;font-weight:400">${["macos", "linux", "windows"].map((o) => html`<label class="row" style="gap:6px"><input type="checkbox" checked=${v.os.includes(o)} onChange=${() => osTog(o)}/>${o}</label>`)}<span class="mute small">none ticked = all</span></div></div>
      ${!node && html`<label class="fb-f wide"><span>Needs node packages (auto-detected)</span><input value=${v.packages} placeholder="aihub-nodes-teams" onInput=${(e) => set({ packages: e.target.value })}/></label>
      <label class="fb-f wide"><span>Needs commands on PATH</span><input value=${v.commands} placeholder="git" onInput=${(e) => set({ commands: e.target.value })}/></label>`}</div>`
      : html`<textarea class="readme-ed" value=${v.readme} onInput=${(e) => set({ readme: e.target.value })}></textarea>`}</div>
    <div class="row" style="margin-top:14px"><span class="grow"></span><button class="btn" onClick=${close}>Cancel</button><button class="btn acc" disabled=${busy || !v.name.trim() || !v.version.trim()} onClick=${go}>${busy ? "Building…" : "Build package"}</button></div><//>`;
}
// ---------- nodes: create, share, install
const FIELD_TYPES = ["text", "textarea", "number", "checkbox", "select", "password"];
function NodeEditor({ initial, close, saved }) {
  const blank = { id: "", title: "", kind: "action", group: "My nodes", description: "", timeout: 120, fields: [{ name: "text", label: "Text", type: "textarea", default: "{{input}}", assist: ["input", "data"] }], outputs: [] };
  const [n, setN] = useState(initial ? initial.node : blank), [code, setCode] = useState(initial ? initial.code : null), [busy, setBusy] = useState(false);
  useEffect(() => { if (code === null) api("nodes/starter").then((d) => setCode(d.code)); }, []);
  const set = (p) => setN({ ...n, ...p }), setF = (i, p) => set({ fields: n.fields.map((f, j) => (j === i ? { ...f, ...p } : f)) });
  const togA = (i, a) => { const cur = n.fields[i].assist || []; setF(i, { assist: cur.includes(a) ? cur.filter((x) => x !== a) : [...cur, a] }); };
  const save = guard(async () => { setBusy(true); try { const node = { ...n, timeout: +n.timeout || 120, outputs: typeof n.outputs === "string" ? n.outputs.split(",").map((x) => x.trim()).filter(Boolean) : n.outputs }; await post("nodes/save", { node, code }); toast("Node saved: Works > Add node > " + n.group); saved(); } finally { setBusy(false); } });
  return html`<${Modal} wide=${true} close=${close}><h1>${initial ? "Edit node" : "New node"}</h1><p class="sub">A node is a small program: it reads one JSON document on stdin and prints one JSON line. Fields become its settings form.</p>
    <div style="max-height:58vh;overflow:auto;padding:2px"><div class="fb-grid" style="padding:0">
      <label class="fb-f"><span>Id (lowercase)</span><input value=${n.id} disabled=${!!initial} onInput=${(e) => set({ id: e.target.value.toLowerCase() })}/></label>
      <label class="fb-f"><span>Title</span><input value=${n.title} onInput=${(e) => set({ title: e.target.value })}/></label>
      <label class="fb-f"><span>Kind</span><select value=${n.kind} onChange=${(e) => set({ kind: e.target.value })}><option value="action">action (a step)</option><option value="trigger">trigger (starts runs)</option></select></label>
      <label class="fb-f"><span>Group in Add node</span><input value=${n.group} onInput=${(e) => set({ group: e.target.value })}/></label>
      <label class="fb-f wide"><span>Description</span><input value=${n.description} onInput=${(e) => set({ description: e.target.value })}/></label>
      <label class="fb-f"><span>Timeout (s)</span><input type="number" value=${n.timeout} onInput=${(e) => set({ timeout: e.target.value })}/></label>
      <label class="fb-f"><span>Data keys it returns (comma separated)</span><input value=${Array.isArray(n.outputs) ? n.outputs.join(", ") : n.outputs} placeholder="path, id" onInput=${(e) => set({ outputs: e.target.value })}/></label></div>
      <h2>Settings fields</h2>
      ${n.fields.map((f, i) => html`<div class="card" style="padding:10px 12px;margin-bottom:8px"><div class="row" style="gap:8px;flex-wrap:wrap">
        <input style="width:120px" placeholder="name" value=${f.name} onInput=${(e) => setF(i, { name: e.target.value })}/><input class="grow" style="min-width:120px" placeholder="label" value=${f.label || ""} onInput=${(e) => setF(i, { label: e.target.value })}/>
        <select value=${f.type || "text"} onChange=${(e) => setF(i, { type: e.target.value })}>${FIELD_TYPES.map((t) => html`<option>${t}</option>`)}</select>
        <input style="width:140px" placeholder="default" value=${f.default || ""} onInput=${(e) => setF(i, { default: e.target.value })}/>
        <button class="ibtn" title="Remove field" aria-label="Remove field" onClick=${() => set({ fields: n.fields.filter((_, j) => j !== i) })}>${Ic("trash", 14)}</button></div>
        ${(f.type === "text" || f.type === "textarea") && html`<div class="row small" style="gap:14px;margin-top:8px;flex-wrap:wrap"><span class="mute">Suggest and highlight:</span>${[["input", "{{input}}"], ["data", "{{data.key}}"], ["file", "@file"], ["skill", "/skill"]].map(([a, l]) => html`<label class="row" style="gap:5px"><input type="checkbox" checked=${(f.assist || []).includes(a)} onChange=${() => togA(i, a)}/>${l}</label>`)}</div>`}</div>`)}
      <button class="btn" onClick=${() => set({ fields: [...n.fields, { name: "field" + (n.fields.length + 1), label: "", type: "text", default: "" }] })}>${Ic("plus", 14)} Add field</button>
      <h2>Program (Python)</h2><textarea class="readme-ed" spellcheck="false" value=${code || ""} onInput=${(e) => setCode(e.target.value)}></textarea>
      <p class="mute small">Password fields are stored encrypted by the UI and reach the program on stdin only. Never write a secret into the program: it is shared with the package.</p></div>
    <div class="row" style="margin-top:14px"><span class="grow"></span><button class="btn" onClick=${close}>Cancel</button><button class="btn acc" disabled=${busy || !/^[a-z0-9][a-z0-9._-]*$/.test(n.id) || code === null} onClick=${save}>${busy ? "Saving…" : "Save node"}</button></div><//>`;
}
function Nodes({ close, boot, changed }) {
  const [tab, setTab] = useState("mine"), [mine, setMine] = useState([]), [pk, setPk] = useState([]), [found, setFound] = useState([]), [q, setQ] = useState(""), [dlg, setDlg] = useState(null), [ed, setEd] = useState(null), [share, setShare] = useState(null), [shared, setShared] = useState(null);
  const loadMine = guard(async () => setMine((await api("nodes/local")).nodes));
  const loadPk = guard(async () => setPk((await api("nodes/packages")).packages));
  const find = guard(async () => setFound((await api("search?type=node&q=" + encodeURIComponent(q))).results));
  useEffect(() => { loadMine(); loadPk(); find(); }, []);
  const have = new Set(pk.map((m) => m.package));
  return html`<${Modal} wide=${true} close=${close}><h1>Nodes</h1><p class="sub">Nodes are the building blocks of a work. Create your own, share them as an aihub package (type node), or install nodes other people shared.</p>
    <${Seg} value=${tab} onChange=${setTab} items=${[{ id: "mine", label: `My nodes (${mine.length})`, icon: "layers" }, { id: "installed", label: `Installed (${pk.length})`, icon: "package" }, { id: "find", label: "Find on the hub", icon: "search" }]}/>
    <div style="margin-top:12px;max-height:50vh;overflow:auto">
    ${tab === "mine" ? html`<div class="row" style="margin-bottom:10px"><button class="btn acc" onClick=${() => setEd({})}>${Ic("plus", 14)} New node</button></div>
      ${!mine.length && html`<p class="mute">No node of your own yet. A new node starts from a working example.</p>`}
      ${mine.map((m) => html`<div class="card row"><div class="grow"><b>${m.node.title}</b> <span class=${"tag" + (m.node.kind === "trigger" ? " acc" : "")}>${m.node.kind === "trigger" ? "start" : "step"}</span><div class="mute small">${m.node.group} · pkg:local/${m.id}${m.node.description ? " · " + m.node.description : ""}</div></div>
        <button class="btn" onClick=${() => setEd(m)}>Edit</button>
        <button class="btn" onClick=${guard(async () => setShare({ id: m.id, info: await api("nodes/export-info?id=" + m.id) }))}>${Ic("upload", 14)} Share</button>
        <button class="btn bad" onClick=${guard(async () => { if (confirm("Delete node " + m.node.title + "? Works that use it stop working.")) { await post("nodes/delete", { id: m.id }); loadMine(); changed(); } })}>${Ic("trash", 14)}</button></div>`)}`
    : tab === "installed" ? html`${!pk.length && html`<p class="mute">No node package installed. Use “Find on the hub”, or run <code>aihub install NAME</code>.</p>`}
      ${pk.map((m) => html`<div class="card row"><div class="grow"><b>${m.package}</b> <span class="mute small">${m.version || ""}</span><div class="mute small">${m.nodes.join(", ")}${m.description ? " · " + m.description : ""}</div></div><span class="tag good">${Ic("check", 12)} ${m.enabled ? "ready" : "disabled"}</span></div>`)}`
    : html`<div class="row"><input class="grow" placeholder="Search node packages" value=${q} onInput=${(e) => setQ(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && find()}/><button class="btn" onClick=${find}>Search</button></div>
      ${!found.length && html`<p class="mute">Nothing found.</p>`}
      ${found.map((p) => html`<div class="card row"><div class="grow"><b>${p.name}</b> <span class="mute small">${p.latest_version || ""}</span><div class="mute small">${p.description}</div></div>
        ${have.has(p.name) ? html`<span class="tag good">${Ic("check", 12)} installed</span>` : html`<button class="btn acc" onClick=${() => setDlg(p)}>${Ic("download", 14)} Install</button>`}</div>`)}`}</div>
    <div class="row" style="margin-top:14px"><span class="grow"></span><button class="btn" onClick=${close}>Close</button></div>
    ${dlg && html`<${InstallDialog} p=${dlg} close=${() => setDlg(null)} done=${() => { setDlg(null); loadPk(); changed(); setTab("installed"); }}/>`}
    ${ed && html`<${NodeEditor} initial=${ed.node ? ed : null} close=${() => setEd(null)} saved=${() => { setEd(null); loadMine(); changed(); }}/>`}
    ${share && html`<${ShareForm} node=${true} id=${share.id} info=${share.info} close=${() => setShare(null)} done=${(r) => { setShare(null); setShared(r); }}/>`}
    ${shared && html`<${PublishSteps} r=${shared} boot=${boot} close=${() => setShared(null)}/>`}
<//>`;
}
function Workflows({ close, opened }) {
  const [tab, setTab] = useState("samples"), [samples, setSamples] = useState([]), [mine, setMine] = useState([]), [found, setFound] = useState([]), [q, setQ] = useState(""), [dlg, setDlg] = useState(null);
  const loadMine = guard(async () => setMine((await api("works/workflows")).workflows));
  const find = guard(async () => setFound((await api("search?type=workflow&q=" + encodeURIComponent(q))).results));
  useEffect(() => { loadMine(); find(); api("works/samples").then((d) => setSamples(d.samples)).catch(() => {}); }, []);
  const add = guard(async (m) => { const r = await post("works/import", { package: m.package, file: m.file }); if (r.risks && r.risks.length) toast("Added switched off. It " + r.risks.join("; ") + ". Review it before turning it on."); opened(r.work); });
  const have = new Set(mine.map((m) => m.package));
  return html`<${Modal} wide=${true} close=${close}><h1>Workflows</h1><p class="sub">Works that people shared as aihub packages (type workflow). Adding one copies it into Works, switched off, with empty secret fields.</p>
    <${Seg} value=${tab} onChange=${setTab} items=${[{ id: "samples", label: "Samples", icon: "sparkles" }, { id: "installed", label: `Installed (${mine.length})`, icon: "package" }, { id: "find", label: "Find on the hub", icon: "search" }]}/>
    <div style="margin-top:12px;max-height:50vh;overflow:auto">
    ${tab === "samples" ? html`${samples.map((m) => html`<div class="card row"><div class="grow"><b>${m.title}</b> <span class="mute small">${m.nodes} nodes</span><div class="mute small">${m.description}</div><${Risks} list=${m.risks}/></div><button class="btn acc" onClick=${() => add(m)}>Add to Works</button></div>`)}`
    : tab === "installed" ? html`${!mine.length && html`<p class="mute">No workflow package installed. Use “Find on the hub”, or run <code>aihub install NAME</code>.</p>`}
      ${mine.map((m) => html`<div class="card row"><div class="grow"><b>${m.title}</b><div class="mute small">${m.package} ${m.version || ""} · ${m.nodes} nodes${m.description ? " · " + m.description : ""}</div><${Risks} list=${m.risks}/></div>
        <button class="btn acc" onClick=${() => add(m)}>Add to Works</button></div>`)}`
    : html`<div class="row"><input class="grow" placeholder="Search workflows" value=${q} onInput=${(e) => setQ(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && find()}/><button class="btn" onClick=${find}>Search</button></div>
      ${!found.length && html`<p class="mute">Nothing found.</p>`}
      ${found.map((p) => html`<div class="card row"><div class="grow"><b>${p.name}</b> <span class="mute small">${p.latest_version || ""}</span><div class="mute small">${p.description}</div></div>
        ${have.has(p.name) ? html`<span class="tag good">${Ic("check", 12)} installed</span>` : html`<button class="btn acc" onClick=${() => setDlg(p)}>${Ic("download", 14)} Install</button>`}</div>`)}`}</div>
    <div class="row" style="margin-top:14px"><span class="grow"></span><button class="btn" onClick=${close}>Close</button></div>
    ${dlg && html`<${InstallDialog} p=${dlg} close=${() => setDlg(null)} done=${() => { setDlg(null); loadMine(); setTab("installed"); }}/>`}<//>`;
}
function Works({ boot, openId }) {
  const [trig, setTrig] = useState({});
  const [list, setList] = useState([]), [w, setW] = useState(null), [cat, setCat] = useState({}), [sel, setSel] = useState(null), [link, setLink] = useState(null), [mouse, setMouse] = useState(null);
  const [gen, setGen] = useState(""), [genBusy, setGenBusy] = useState(false), [wf, setWf] = useState(false), [nd, setNd] = useState(false), [shared, setShared] = useState(null), [share, setShare] = useState(null), [runs, setRuns] = useState([]), [run, setRun] = useState(null), [hook, setHook] = useState(""), [dirty, setDirty] = useState(false), [pal, setPal] = useState(null), graph = useRef();
  const load = guard(async () => setList((await api("works")).works));
  const loadCat = () => api("works/nodes").then((d) => setCat(Object.fromEntries(d.nodes.map((n) => [n.type, n])))).catch(() => {});
  useEffect(() => { load(); loadCat(); }, []);
  const open = guard(async (id) => { const d = await api("works/get?id=" + id); setW(d.work); setTrig(d.triggers || {}); setSel(null); setRun(null); setDirty(false); });
  useEffect(() => { if (openId) open(openId); }, [openId]);
  const refreshRuns = useCallback(() => w && w.id && api("works/runs?id=" + w.id).then((d) => setRuns(d.runs)).catch(() => {}), [w && w.id]);
  useEffect(() => { if (w && w.id) refreshRuns(); }, [w && w.id]);
  useInterval(() => { refreshRuns(); if (sel && w.enabled) api("works/get?id=" + w.id).then((d) => setTrig(d.triggers || {})).catch(() => {}); if (run && ["queued", "running"].includes(run.status)) api(`works/run?id=${w.id}&run=${run.id}`).then((d) => setRun(d.run)).catch(() => {}); }, w && w.id ? 2000 : null);
  const node = w && sel ? w.nodes.find((n) => n.id === sel) : null, spec = node ? cat[node.type] : null;
  useEffect(() => { setHook(""); if (w && w.id && node && node.type === "start.webhook") api(`works/hook?id=${w.id}&node=${node.id}`).then((d) => setHook(d.url)).catch(() => {}); }, [sel, w && w.id]);
  const upd = (fn) => { setDirty(true); setW((c) => fn({ ...c, nodes: c.nodes.map((n) => ({ ...n, config: { ...n.config } })), edges: [...c.edges] })); };
  const save = guard(async () => { const r = await post("works/save", { work: w }); setW(r.work); setDirty(false); load(); toast("Saved"); return r.work; });

  if (!w) return html`<div class="page wkpage"><h1>Works</h1><p class="sub">Flows that start on a schedule, a webhook, a message or a click, then run Claude tasks, filters and package nodes.</p>
    <div class="card"><b>Generate a workflow</b><p class="mute small" style="margin:4px 0 8px">Describe it in your words; Claude designs the nodes and opens the result (switched off). You can also say it in a chat with /workflow.</p>
      <textarea rows="3" style="width:100%" placeholder="Pull the sales repo from git, read the parquet files, build a dashboard and publish it as a space every Monday" value=${gen} onInput=${(e) => setGen(e.target.value)}></textarea>
      <button class="btn acc" style="margin-top:8px" disabled=${genBusy || !gen.trim()} onClick=${guard(async () => { setGenBusy(true); try { const r = await post("works/generate", { prompt: gen }); setGen(""); load(); location.hash = "#/works/" + r.work.id; toast("Created " + r.work.title + " (switched off)"); } finally { setGenBusy(false); } })}>${Ic("sparkles", 14)} ${genBusy ? "Designing..." : "Generate"}</button></div>
    <div class="row wkactions"><button class="btn" onClick=${() => setWf(true)}>${Ic("download", 14)} Workflows from packages</button><button class="btn" onClick=${() => setNd(true)}>${Ic("layers", 14)} Nodes: create, share, install</button></div>${nd && html`<${Nodes} close=${() => setNd(false)} boot=${boot} changed=${loadCat}/>`}
    <button class="btn acc" onClick=${() => { setW({ title: "Untitled work", enabled: false, nodes: [{ id: "s" + Math.random().toString(36).slice(2, 6), type: "start.manual", x: 60, y: 80, config: {} }], edges: [] }); setSel(null); setRuns([]); setRun(null); setDirty(true); }}>${Ic("plus", 14)} New work</button>
    ${!list.length && html`<div class="card mute" style="margin-top:16px">No works yet. Add a Start node, a Claude task, connect them, then switch the work On.</div>`}
    ${wf && html`<${Workflows} close=${() => setWf(false)} opened=${(nw) => { setWf(false); load(); setW(nw); setSel(null); setRun(null); setDirty(false); }}/>`}
    ${list.map((x) => html`<div class="card row"><span class="nico" style="width:34px;height:34px;border-radius:10px;background:var(--soft2);display:grid;place-items:center">${Ic("flow", 17)}</span><div class="grow"><b>${x.title}</b> <span class=${"tag " + (x.enabled ? "good" : "")}>${x.enabled ? "on" : "off"}</span> <span class="mute small">${x.nodes} nodes · ${ago(x.updated)}</span>${x.description && html`<div class="mute small">${x.description}</div>`}</div><button class="btn" onClick=${() => open(x.id)}>Open</button>
      <button class="btn bad" onClick=${guard(async () => { if (confirm("Delete " + x.title + "? Its stored node secrets go too.")) { await post("works/delete", { id: x.id }); load(); } })}>${Ic("trash", 14)}</button></div>`)}</div>`;

  const add = (type, pos) => { const sp = cat[type]; const cfg = {}; (sp ? sp.fields : []).forEach((f) => { if (f.default !== undefined && f.default !== "" && !f.secret) cfg[f.name] = f.default; });
    const id = "n" + Math.random().toString(36).slice(2, 7); upd((c) => (c.nodes.push({ id, type, ...(pos ? { x: pos.x, y: pos.y } : freeSpot(c.nodes)), label: "", config: cfg }), c)); setSel(id); setPal(null); };
  const drag = (n, e) => { if (e.target.closest("button")) return; e.preventDefault(); setSel(n.id); const sx = e.clientX - n.x, sy = e.clientY - n.y; const mv = (ev) => upd((c) => (Object.assign(c.nodes.find((q) => q.id === n.id), { x: Math.max(0, Math.round(ev.clientX - sx)), y: Math.max(0, Math.round(ev.clientY - sy)) }), c)); const up = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); }; addEventListener("pointermove", mv); addEventListener("pointerup", up); };
  const st = (id) => (run && run.nodes && run.nodes[id] ? run.nodes[id].status : "");
  const toggle = guard(async (on) => { const saved = dirty || !w.id ? await save() : w; const r = await post("works/enable", { id: saved.id, enabled: on }); setW(r.work); load(); toast(on ? "Work is on: triggers are live while this service runs" : "Work is off"); });
  const go = guard(async () => { const saved = dirty || !w.id ? await save() : w; const r = await post("works/run", { id: saved.id, node: node && cat[node.type] && cat[node.type].kind === "trigger" ? sel : undefined, text: prompt("Event text for this run", "manual run") || "manual run" }); toast("Run queued"); setTimeout(() => api(`works/run?id=${saved.id}&run=${r.run}`).then((d) => setRun(d.run)), 600); });
  const track = (e) => { if (!link || !graph.current) return; const r = graph.current.getBoundingClientRect(); setMouse({ x: e.clientX - r.left + graph.current.scrollLeft, y: e.clientY - r.top + graph.current.scrollTop }); };
  const P = (n, out) => ({ x: n.x + (out ? NW : 0), y: n.y + 27 });
  const curve = (a, b) => `M${a.x},${a.y} C${a.x + 80},${a.y} ${b.x - 80},${b.y} ${b.x},${b.y}`;
  return html`<div class="wk"><div class="wkbar"><button class="ibtn" title="Back" aria-label="Back" onClick=${() => { if (!dirty || confirm("Discard unsaved changes?")) { setW(null); load(); } }}>${Ic("left", 18)}</button>
      <input class="grow" style="font:600 16px var(--serif);border-color:transparent;background:transparent" value=${w.title} onInput=${(e) => upd((c) => ((c.title = e.target.value), c))}/>
      <label class="row small" style="gap:6px" title="How many runs are kept on disk, and offered to Memory nodes"><span class="mute">History</span><input type="number" min="1" max="500" style="width:64px" value=${w.keep || 40} onInput=${(e) => upd((c) => ((c.keep = Math.max(1, Math.min(500, +e.target.value || 40))), c))}/></label>
      ${w.id && html`<button class="btn" title="Share as an aihub workflow package" onClick=${guard(async () => { const saved = dirty ? await save() : w; setShare({ id: saved.id, info: await api("works/export-info?id=" + saved.id) }); })}>${Ic("upload", 14)} Share</button>`}
      <button class="btn" title="Re-arrange the nodes so none overlap" onClick=${guard(async () => { const r = await post("works/layout", { nodes: w.nodes, edges: w.edges }); upd((c) => (c.nodes.forEach((n) => { const p = r.positions[n.id]; if (p) { n.x = p.x; n.y = p.y; } }), c)); })}>${Ic("layers", 14)} Tidy</button>
      <button class="btn" onClick=${() => setPal({})}>${Ic("plus", 14)} Add node</button>
      <label class="row small" style="gap:8px"><${Switch} on=${w.enabled} onChange=${toggle} label="Work on"/>${w.enabled ? "On" : "Off"}</label>
      <button class="btn" onClick=${save} disabled=${!dirty}>Save</button><button class="btn acc" onClick=${go}>${Ic("play", 13)} Run now</button></div>
    <div class="wkdesc">${Ic("note", 13)}<input placeholder="Short description: what this work does (shown in lists and when shared)" maxlength="300" value=${w.description || ""} onInput=${(e) => upd((c) => ((c.description = e.target.value), c))}/></div>
    <div class="wkbody"><div class="graph" ref=${graph} onPointerMove=${track} onPointerDown=${(e) => { if (e.target.classList.contains("gin")) { setSel(null); setLink(null); } }}>
        <div class="gin" onDblClick=${(e) => { if (e.target.classList.contains("gin")) setPal({ x: e.nativeEvent.offsetX - 20, y: e.nativeEvent.offsetY - 20 }); }}>
          <svg width="2600" height="1700" style="position:absolute;inset:0;pointer-events:none">${w.edges.map((e) => { const a = w.nodes.find((n) => n.id === e.from), b = w.nodes.find((n) => n.id === e.to); return a && b ? html`<path class="edge" d=${curve(P(a, true), P(b, false))}/>` : null; })}
            ${link && mouse && (() => { const a = w.nodes.find((n) => n.id === link); return a ? html`<path class="edge live" d=${curve(P(a, true), mouse)}/>` : null; })()}</svg>
          ${w.nodes.map((n) => { const sp = cat[n.type]; return html`<div class=${`node ${nodeKind(n.type, sp)} ${st(n.id)} ${sel === n.id ? "sel" : ""}`} style=${`left:${n.x}px;top:${n.y}px;width:${NW}px`} onPointerDown=${() => setSel(n.id)}>
            ${(!sp || sp.kind !== "trigger") && html`<span class=${"port i" + (link ? " act" : "")} title="Input" onPointerUp=${() => { if (link && link !== n.id) upd((c) => (c.edges.some((e) => e.from === link && e.to === n.id) || c.edges.push({ from: link, to: n.id }), c)); setLink(null); }}></span>`}
            <span class=${"port o" + (link === n.id ? " act" : "")} title="Drag to connect, then click the next node's left dot" onPointerDown=${(e) => { e.stopPropagation(); setLink(n.id); setSel(n.id); }}></span>
            <header onPointerDown=${(e) => drag(n, e)}><span class="nico">${Ic(nodeIcon(n.type, sp), 16)}</span><div class="grow" style="min-width:0"><div class="nt">${n.label || (sp ? sp.title : n.type)}</div><div class="ns">${sp ? (sp.kind === "trigger" ? "Start" : sp.group) : "missing package"}</div></div><span class="st"></span>
              <button class="rm" title="Delete" onClick=${() => { upd((c) => ((c.nodes = c.nodes.filter((q) => q.id !== n.id)), (c.edges = c.edges.filter((x) => x.from !== n.id && x.to !== n.id)), c)); setSel(null); }}>${Ic("x", 13)}</button></header>
            <div class="nb">${summary(n) || html`<span class="mute">Not set up</span>`}</div></div>`; })}
        </div>${link && html`<div class="toast" style="position:sticky;bottom:12px;left:40%;display:inline-block">Click the left dot of the next node <button type="button" class="lnk" style="color:var(--accent)" onClick=${() => setLink(null)}>cancel</button></div>`}</div>
      ${node ? html`<${Inspector} w=${w} node=${node} spec=${spec} cat=${cat} run=${run} boot=${boot} hook=${hook} trig=${trig} upd=${upd} close=${() => setSel(null)} remove=${() => { upd((c) => ((c.nodes = c.nodes.filter((q) => q.id !== node.id)), (c.edges = c.edges.filter((x) => x.from !== node.id && x.to !== node.id)), c)); setSel(null); }}/>`
        : html`<aside class="insp"><div class="insp-h"><b class="grow">Runs</b><button class="ibtn" title="Refresh runs" aria-label="Refresh runs" onClick=${refreshRuns}>${Ic("refresh", 15)}</button></div><div class="insp-b">${!runs.length && html`<p class="mute small">No runs yet. Double-click the canvas or press Add node to build the flow.</p>`}
          ${runs.slice(0, 14).map((r) => html`<div class=${"item" + (run && run.id === r.id ? " on" : "")} onClick=${guard(async () => setRun((await api(`works/run?id=${w.id}&run=${r.id}`)).run))}><span class=${"tag " + (r.status === "done" ? "good" : r.status === "failed" ? "bad" : "")}>${r.status}</span><span class="t small">${r.via} · ${r.text}</span><span class="mute small">${ago(r.started)}</span></div>`)}
          ${run && run.chat && html`<a class="small" href=${"#/chat/" + run.chat}>Open the run's chat and files ${Ic("right", 12)}</a>`}</div></aside>`}</div>
    ${share && html`<${ShareForm} id=${share.id} info=${share.info} close=${() => setShare(null)} done=${(r) => { setShare(null); setShared(r); }}/>`}
    ${shared && html`<${PublishSteps} r=${shared} boot=${boot} close=${() => setShared(null)}/>`}
    ${pal && html`<${Palette} cat=${cat} more=${() => { setPal(null); setNd(true); }} close=${() => setPal(null)} onPick=${(t) => add(t, pal.x !== undefined ? pal : null)}/>`}</div>`;
}

// ---------- settings
function HelperStatus() {
  const [st, setSt] = useState(null), [dlg, setDlg] = useState(false);
  useEffect(() => { api("auth").then(setSt).catch(() => {}); }, [dlg]);
  if (!st || !st.helper) return null;
  return html`<h2>Claude sign-in (apiKeyHelper)</h2><div class="card row"><div class="grow">Key helper: <b>${st.program}</b> <span class=${"tag " + (st.valid ? "good" : "")}>${st.valid ? "signed in" : "not checked / signed out"}</span>
      ${st.last_error && html`<div class="mute small">${linkify(st.last_error.slice(0, 300))}</div>`}</div>
    <button class="btn" onClick=${guard(async () => { const r = await post("auth/check"); setSt(r.status); toast(r.ok ? "Key helper works" : "Sign-in needed"); })}>Check</button><button class="btn acc" onClick=${() => setDlg(true)}>Sign in</button>${dlg && html`<${SignIn} close=${() => setDlg(false)}/>`}</div>`;
}
function FilePicker({ start, ext = ".json", title = "Choose a file", onPick, close, dirs = false }) {
  const [d, setD] = useState(null), [sel, setSel] = useState(""), [bad, setBad] = useState("");
  const go = async (p) => { try { setD(await api("fs/list?ext=" + encodeURIComponent(ext) + (dirs ? "&dirs=1" : "") + "&path=" + encodeURIComponent(p || ""))); setSel(""); setBad(""); } catch (x) { setBad(x.message || "Cannot list this folder"); if (d) toast(x.message); } };
  useEffect(() => { go(start); }, []);
  if (!d) return html`<${Modal} close=${close}><h1>${title}</h1>${bad ? html`<p class="tag bad">${bad}</p><div class="row" style="margin-top:14px"><span class="grow"></span><button class="btn" onClick=${() => go("")}>Try the home folder</button><button class="btn" onClick=${close}>Close</button></div>` : html`<div class="shine">Loading…</div>`}<//>`;
  const under = d.path.toLowerCase().startsWith(d.home.toLowerCase()), parts = d.path.slice(under ? d.home.length : 0).split(/[\\/]/).filter(Boolean); let acc = under ? d.home : "";
  const sep = d.path.includes("\\") ? "\\" : "/", full = (n) => d.path + (d.path.endsWith(sep) ? "" : sep) + n;
  return html`<${Modal} wide=${true} close=${close}><h1>${title}</h1>
    <div class="crumbs"><button type="button" class="lnk" onClick=${() => go(d.home)}>${Ic("folder", 13)} Home</button>${parts.map((p) => { acc = acc ? acc + (acc.endsWith(sep) ? "" : sep) + p : (/^[A-Za-z]:$/.test(p) ? p + sep : sep + p); const here = acc; return html`<span class="mute">/</span><button type="button" class="lnk" onClick=${() => go(here)}>${p}</button>`; })}</div>
    <div class="row small" style="gap:8px;margin:6px 0">${d.places.map((pl) => html`<button class="btn" style="padding:3px 10px" onClick=${() => go(pl.path)}>${pl.name}</button>`)}<span class="mute">${dirs ? "Only folders are shown." : "Only files ending " + ext + " are shown. Contents are never displayed."}</span></div>
    <div class="fpl">${d.parent && html`<div class="fpr" onClick=${() => go(d.parent)}>${Ic("up", 15)}<span class="grow">..</span></div>`}
      ${d.items.map((i) => html`<div class=${"fpr" + (sel === i.name ? " on" : "")} onClick=${() => (i.dir ? go(full(i.name)) : setSel(i.name))} onDblClick=${() => !i.dir && onPick(full(i.name))}>${Ic(i.dir ? "folder" : "file", 15)}<span class="grow">${i.name}</span>${!i.dir && html`<span class="mute small">${num(i.size)}B</span>`}</div>`)}
      ${!d.items.length && html`<p class="mute small" style="padding:14px">Nothing here.</p>`}</div>
    <div class="row" style="margin-top:14px"><span class="grow mono small" style="overflow:hidden;text-overflow:ellipsis">${sel ? full(sel) : d.path}</span><button class="btn" onClick=${close}>Cancel</button>${dirs ? html`<button class="btn acc" onClick=${() => onPick(d.path)}>Use this folder</button>` : html`<button class="btn acc" disabled=${!sel} onClick=${() => onPick(full(sel))}>Use this file</button>`}</div><//>`;
}
const WINDOWS = [[200000, "200K tokens"], [500000, "500K tokens"], [1000000, "1M tokens (default)"], [2000000, "2M tokens"]];
const UPD_STEPS = [["checking", "Check"], ["waiting", "Wait until idle"], ["updating", "Update"], ["restarting", "Restart"]];
function UpdateCard({ prefs, savePrefs }) {
  const [u, setU] = useState(null), [busy, setBusy] = useState(false), [down, setDown] = useState(false);
  const load = () => api("update").then((r) => { if (down) location.reload(); setU(r.update); }).catch(() => { if (u && u.restarting) setDown(true); });
  useEffect(() => { load(); }, []);
  const live = u && (u.restarting || ["updating", "restarting", "waiting"].includes(u.stage));
  useInterval(load, live || down ? 1500 : 20000);
  const check = async () => { setBusy(true); try { setU((await post("update/check", {})).update); } catch (e) { toast(e.message); } finally { setBusy(false); } };
  const act = async (what) => {
    if (!confirm(what === "update" ? "Install the newest version and restart now? Running chats and works are cut off." : "Restart AI Hub UI now? Running chats and works are cut off.")) return;
    try { setU((await post("update/apply", { what })).update); } catch (e) { toast(e.message); }
  };
  const st = u && (u.stage || (u.restarting ? "restarting" : "")), at = UPD_STEPS.findIndex((x) => x[0] === st);
  return html`<div class="card"><div class="row"><div class="grow"><b>Keep AI Hub UI up to date</b><div class="mute small">Checks for a newer aihub-ui every few hours. “Update and restart” waits until nothing is running (no chat answering, no work, no install), then updates and restarts this service; open pages reconnect.</div>
      ${u && html`<div class="small" style="margin-top:6px">Installed ${u.installed || "?"}${u.latest ? " · newest " + u.latest : ""}${u.newer ? html` · <b>update available</b>` : ""}${u.waiting ? " · waiting: " + u.waiting : ""}${u.error ? html`<span style="color:var(--bad,#c0392b)"> · ${u.error}</span>` : ""}</div>`}</div>
      <select value=${prefs.auto_update || "on"} onChange=${(e) => savePrefs({ auto_update: e.target.value })}><option value="on">Update and restart</option><option value="notify">Only tell me</option><option value="off">Off</option></select>
      <button class="btn" disabled=${busy || live} onClick=${check}>${busy ? "Checking…" : "Check now"}</button>
      ${u && u.newer && html`<button class="btn acc" disabled=${live} onClick=${() => act("update")}>Update now</button>`}
      <button class="btn" disabled=${live || down} onClick=${() => act("restart")}>${Ic("refresh", 14)} Restart now</button></div>
    ${(at >= 0 || down) && html`<div class="updprog" role="status"><ol>${UPD_STEPS.map(([k, l], i) => html`<li class=${down || i < at ? "done" : i === at ? "cur" : ""}><i></i>${l}</li>`)}</ol><div class="bar"><span class=${down ? "full" : "ind"}></span></div><div class="mute small">${down ? "Waiting for the service to come back…" : st === "waiting" ? "Waiting for running work to finish: " + (u.waiting || "") : st === "updating" ? "Installing aihub-ui " + (u.latest || "") + "…" : st === "restarting" ? "Restarting; this page reloads when it is back." : "Checking for a newer version…"}</div></div>`}</div>`;
}

function Settings({ boot, reload, prefs, savePrefs, focus }) {
  const [sec, setSec] = useState({ names: [] }), [f, setF] = useState({ name: "", value: "" }), [lg, setLg] = useState({ username: "", password: "" }), card = useRef(), userIn = useRef(), [cw, setCw] = useState(String(prefs.window || 1000000)), [fm, setFm] = useState(prefs.free_model || "");
  useEffect(() => setCw(String(prefs.window || 1000000)), [prefs.window]);
  useEffect(() => setFm(prefs.free_model || ""), [prefs.free_model]);
  useEffect(() => { if (focus === "login" && card.current) { card.current.scrollIntoView({ block: "center", behavior: "smooth" }); userIn.current && userIn.current.focus(); } }, [focus]);
  const load = guard(async () => setSec(await api("secrets")));
  useEffect(() => { load(); }, []);
  const c = boot.context, cl = boot.claude, [sf, setSf] = useState(boot.claude.custom ? boot.claude.path : ""), [pick, setPick] = useState(false);
  const applySf = guard(async (v) => { if (!(await savePrefs({ claude_settings: v.trim() }))) return; await reload(); toast(v.trim() ? "Using " + v.trim() : "Using the default settings.json"); });
  return html`<div class="page"><h1>Settings</h1>
    <h2 id="login">Hub account ${!c.signed_in && html`<span class="tag acc">sign in</span>`}</h2><div class=${"card loginc" + (c.signed_in ? "" : " hl")} ref=${card}>${c.signed_in ? html`<div class="row"><div class="grow">Signed in as <b>${c.username}</b> on ${c.hub}</div><button class="btn" onClick=${guard(async () => { await post("logout"); reload(); })}>Sign out</button></div>`
      : html`<form class="row" onSubmit=${(e) => { e.preventDefault(); guard(async () => { await post("login", lg); setLg({ username: "", password: "" }); reload(); })(); }}><input ref=${userIn} placeholder="username" autocomplete="username" value=${lg.username} onInput=${(e) => setLg({ ...lg, username: e.target.value })}/><input type="password" autocomplete="current-password" placeholder="password" value=${lg.password} onInput=${(e) => setLg({ ...lg, password: e.target.value })}/>
        <button class="btn acc" type="submit" disabled=${!lg.username || !lg.password}>Sign in</button><span class="mute small">hub: ${c.hub}</span></form>`}
      <p class="mute small">Shared with the terminal: <code>aihub login</code> and this page use the same account.</p></div>
    <h2>Updates</h2>
    <${UpdateCard} prefs=${prefs} savePrefs=${savePrefs}/>
    <h2>Chats</h2>
    <div class="card"><div class="row"><div class="grow"><b>aihub policy guard in these chats</b><div class="mute small">${prefs.policy === "off" ? "Off for testing: the guard hook is skipped for chats started here. Your terminal and the global policy are untouched." : "On: tool calls in these chats are checked like in your terminal."}</div></div><${Switch} on=${prefs.policy === "on"} label="Policy guard" onChange=${(v) => savePrefs({ policy: v ? "on" : "off" })}/></div></div>
    <div class="card"><div class="row"><div class="grow"><b>Auto-compact</b><div class="mute small">Summarise older turns near the limit. Applies to chats and Claude nodes.</div></div><${Switch} on=${prefs.autocompact} label="Auto-compact" onChange=${(v) => savePrefs({ autocompact: v })}/></div>
      ${prefs.autocompact && html`<div class="row" style="margin-top:10px"><span class="small">Limit</span><input type="range" min="40" max="95" step="5" value=${prefs.compact_pct} class="grow" onChange=${(e) => savePrefs({ compact_pct: +e.target.value })}/><b>${prefs.compact_pct}%</b></div>`}</div>
    <div class="card"><div class="row"><div class="grow"><b>Online search</b><div class="mute small">Search (<kbd>⌘K</kbd>) always answers from this machine first: chats, and a local copy of the hub's Spaces and packages. On: when the local answer is thin it asks the hub once (per query, every 2 min) and keeps the answer, and the local copy is refreshed in the background while you search. Off: the hub is never asked; <code>@hub:</code> and <code>@package:</code> show only what is already cached.</div></div><${Switch} on=${prefs.online_search !== false} label="Online search" onChange=${(v) => savePrefs({ online_search: v })}/></div></div>
    <div class="card"><div class="row"><div class="grow"><b>Model picker options</b><div class="mute small">List the <code>modelPicker.options</code> of your settings.json (chat, image and text-to-speech models) in the model menu. On by default. Off: only Opus, Sonnet and Haiku (the three tiers).</div></div><${Switch} on=${!!prefs.model_picker} label="Model picker options" onChange=${async (v) => { if (await savePrefs({ model_picker: v })) reload(); }}/></div></div>
    <div class="card"><div class="row"><div class="grow"><b>Free chat model</b><div class="mute small">The model id free chat sends when the chat's model is Default or an alias. Empty = built-in default; if your key does not have it, the first free message finds one that works and fills this in.</div></div>
      <input style="width:260px" placeholder="e.g. claude-sonnet-4-5" value=${fm} onInput=${(e) => setFm(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && savePrefs({ free_model: fm.trim() })}/><button class="btn" disabled=${fm.trim() === (prefs.free_model || "")} onClick=${() => savePrefs({ free_model: fm.trim() })}>Save</button></div></div>
    <div class="card"><div class="row"><div class="grow"><b>Context window</b><div class="mute small">The size the context ring in chats measures against, and where auto-compact counts from. Claude Code only has 1M when the model supports it (pick a model marked 1M).</div></div>
      <select value=${WINDOWS.some((w) => w[0] === prefs.window) ? String(prefs.window) : "custom"} onChange=${(e) => e.target.value !== "custom" && savePrefs({ window: +e.target.value })}>${WINDOWS.map((w) => html`<option value=${w[0]}>${w[1]}</option>`)}<option value="custom">Custom…</option></select></div>
      <div class="row" style="margin-top:10px"><span class="small">Tokens</span><input type="number" min="50000" step="10000" style="width:160px" value=${cw} onInput=${(e) => setCw(e.target.value)} onChange=${() => +cw >= 50000 ? savePrefs({ window: +cw }) : toast("At least 50000 tokens")}/><span class="mute small">= ${num(+cw || 0)}</span></div></div>
    <${HelperStatus}/>
    <${Themes} api=${api} post=${post}/>
    <h2>Claude settings file</h2><div class="card"><div class="row"><div class="grow"><b>${cl.custom ? "Custom file" : "Default file"}</b><div class="mono small" style="overflow-wrap:anywhere">${cl.path}${cl.exists ? "" : " (not found)"}</div></div>
        <span class=${"tag " + (cl.exists ? "good" : "bad")}>${cl.exists ? "in use" : "missing"}</span></div>
      <div class="row" style="margin-top:10px"><input class="grow" placeholder="~/.claude/settings.json (default) - or the path of your own settings.json" value=${sf} onInput=${(e) => setSf(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && applySf(sf)}/>
        <button class="btn" onClick=${guard(async () => { const p = await nativePick({ ext: ".json", start: (sf || cl.path).replace(/[\\/][^\\/]*$/, ""), title: "Choose a Claude settings.json" }); if (p === null) setPick(true); else if (p) { setSf(p); applySf(p); } })}>${Ic("folder", 14)} Browse…</button><button class="btn acc" disabled=${sf.trim() === (cl.custom ? cl.path : "")} onClick=${() => applySf(sf)}>Use this file</button>${cl.custom && html`<button class="btn" onClick=${() => { setSf(""); applySf(""); }}>Use default</button>`}</div>
      <p class="mute small" style="margin:8px 0 0">Model list, effort, environment and the key helper are read from this file, and chats and Claude nodes start with <code>--settings</code> pointing at it (merged over your normal Claude settings). Everything else (policy guard, auto-compact, themes) follows the settings on this page.</p></div>
    ${pick && html`<${FilePicker} title="Choose a Claude settings.json" start=${cl.custom ? cl.path : cl.path} onPick=${(p) => { setPick(false); setSf(p); applySf(p); }} close=${() => setPick(false)}/>`}
    <h2>Claude Code (settings.json, read only)</h2><div class="card small">Default model: <b>${cl.model || "—"}</b> · effort: <b>${cl.effort || "—"}</b> · mode: <b>${cl.mode || "—"}</b><br/>
      ${Object.entries(cl.env).map(([k, v]) => html`<div class="mono">${k} = ${v === "set" ? "•••••• (hidden)" : v}</div>`)}
      <p class="mute">Models, login and environment come from Claude's own configuration. Secret values are never read into this page.</p></div>
    <${Vault} embedded=${true}/>
    <h2>Encrypted secrets for chats</h2><p class="sub">Extra environment variables given only to the Claude process of your chats (for example a provider key). Stored encrypted (<code>~/.aihub/ui/secrets.bin</code>, key file mode 0600); values are never shown again.</p>
    ${sec.error && html`<p class="tag bad">${sec.error}</p>`}
    ${sec.names.filter((n) => !n.startsWith("node:")).map((n) => html`<div class="card row"><span class="grow mono">${n}</span><span class="tag">${Ic("key", 12)} encrypted</span><button class="btn bad" onClick=${guard(async () => { await post("secrets/remove", { name: n }); load(); })}>${Ic("trash", 14)}</button></div>`)}
    <div class="card row"><input placeholder="NAME" value=${f.name} onInput=${(e) => setF({ ...f, name: e.target.value })}/><input class="grow" type="password" autocomplete="off" placeholder="value" value=${f.value} onInput=${(e) => setF({ ...f, value: e.target.value })}/>
      <button class="btn acc" onClick=${guard(async () => { await put("secrets", f); setF({ name: "", value: "" }); load(); })}>Store</button></div></div>`;
}

// ---------- shell
// ---------- Spotlight: centered search over pages and chats (Cmd/Ctrl+K)
function ImportLazy(p) {
  const [M, setM] = useState(null);
  useEffect(() => { chatsMod().then((m) => setM(() => m.ImportDialog)); }, []);
  return M ? html`<${M} ...${p}/>` : null;
}
// Plain words: chat titles match at once (client side), message hits follow from the server. `@alias:words` searches one source; an alias
// is only suggested while it is being typed, and nothing runs until it is complete. Answers are cached (SPOT_TTL) and stale requests aborted.
const ALIASES = [
  { k: "hub", icon: "layers", ex: "author@name", d: "Spaces and chats shared on the hub" },
  { k: "open", icon: "link", ex: "https://hub/#/s/author@name", d: "Import a chat from a hub link" },
  { k: "folder", icon: "folder", ex: "my-app or ~/code/", d: "New chat working in a folder (your projects first)" },
  { k: "workflow", icon: "flow", ex: "words", d: "Your workflows" },
  { k: "knowledge", icon: "book", ex: "words", d: "Your knowledge notes" },
  { k: "package", icon: "package", ex: "words", d: "Installed packages and the hub catalogue" }];
const SPOT_TTL = 10000, spotCache = new Map();
const spotTtl = (path) => (path.includes("deep=1") ? SPOT_TTL : 3000);      // local answers are cheap: re-read soon (the mirror grows); hub ones are kept
const spotFresh = (path) => { const c = spotCache.get(path); return !!c && Date.now() - c.at < spotTtl(path); };
const spotGet = (path) => {                                           // one request per path per TTL; a running one is shared (never aborted: others may wait on it)
  if (spotFresh(path)) return spotCache.get(path).p;
  const p = api(path).catch((e) => { spotCache.delete(path); throw e; });
  spotCache.set(path, { at: Date.now(), p });
  if (spotCache.size > 120) spotCache.delete(spotCache.keys().next().value);
  return p;
};
const REF_RX = /^[A-Za-z0-9][\w.-]{0,63}@[A-Za-z0-9][\w.-]{0,63}$/;
const hubRef = (t) => { t = (t || "").trim(); if (/^https?:/i.test(t)) { if (!t.includes("#/s/")) return null; try { t = decodeURIComponent(t.split("#/s/").pop().split("?")[0]); } catch (e) { return null; } } return REF_RX.test(t) ? t : null; };
const parseSpot = (t) => {                                             // -> {alias, words} | {typing: prefix} | {words}
  const m = /^@([a-z]*)(?:([:\s])([\s\S]*))?$/i.exec(t);
  if (!m) return { words: t };
  const k = m[1].toLowerCase(), hit = ALIASES.find((x) => x.k === k);
  return hit && m[2] ? { alias: k, words: m[3].trim() } : { typing: k };
};
const fit = (q, ...xs) => { const w = q.toLowerCase().split(/\s+/).filter(Boolean), s = xs.join(" ").toLowerCase(); return w.every((x) => s.includes(x)); };
function Spotlight({ close, go, sessions, nav, hub, importLink, projects = [], dirs = {}, newChat, newChatIn }) {
  const [q, setQ] = useState(""), [idx, setIdx] = useState(0), [remote, setRemote] = useState({ key: "", rows: null }), [local, setLocal] = useState({}), [busy, setBusy] = useState(false), box = useRef(), inp = useRef();
  const t = q.trim(), P = parseSpot(t), lc = (P.words || "").toLowerCase();
  useEffect(() => { const off = escPush(close); return off; }, []);
  const need = P.alias === "workflow" ? "works" : P.alias === "knowledge" ? "knowledge" : P.alias === "package" ? "packages" : "";
  useEffect(() => { if (need && !local[need]) spotGet(need).then((d) => setLocal((l) => ({ ...l, [need]: d }))).catch(() => setLocal((l) => ({ ...l, [need]: {} }))); }, [need]);
  // remote half: message search (plain, 2+ letters), hub spaces, hub packages. Debounced; a newer query aborts the older one.
  const kind = P.alias || (P.typing !== undefined ? "@" : "");
  const rpath = P.typing !== undefined ? "" : P.alias === "folder" ? (P.words ? "spot/folders?q=" + encodeURIComponent(P.words) : "") : P.alias === "hub" ? "spot/hub?q=" + encodeURIComponent(P.words) : P.alias === "package" ? "spot/pkg?q=" + encodeURIComponent(P.words)
    : !P.alias && t.length >= 2 ? "sessions/search?q=" + encodeURIComponent(t) : "";
  useEffect(() => {
    if (!rpath) { setBusy(false); return; }
    let live = true, h2;                                               // a newer query makes this answer stale: it is dropped
    const got = (path, d) => { if (!live) return; setRemote({ key: rpath, kind, rows: d.results || d.items || [], offline: !!d.offline, indexing: !!d.indexing });
      if (d.more) { h2 = setTimeout(() => spotGet(path + "&deep=1").then((x) => got(path + "&deep=1", x)).catch(() => live && setBusy(false)), 120); } else setBusy(false); };   // the hub only when local is thin
    const run = () => { setBusy(true); spotGet(rpath).then((d) => got(rpath, d)).catch((e) => { if (live) { setRemote({ key: rpath, kind, rows: [], err: e.message }); setBusy(false); } }); };
    if (spotFresh(rpath)) { run(); return () => { live = false; clearTimeout(h2); }; }
    const h = setTimeout(run, P.alias ? 60 : 80);                     // everything below is answered on this machine: a short debounce is enough
    return () => { live = false; clearTimeout(h); clearTimeout(h2); };
  }, [rpath]);
  const rem = !rpath ? null : remote.key === rpath || remote.kind === kind ? remote.rows : null;     // the last answer of the same kind stays (dimmed) until the new one comes
  const fill = (k) => { setQ("@" + k + ":"); if (inp.current) inp.current.focus(); };
  let rows = [], hint = "";
  const projRows = (w, n, group) => projects.filter((f) => fit(w, f, dirs[f] || "")).slice(0, n).map((f) => ({ key: "f" + f, group, icon: "folder", title: w ? hilite(f, w.split(/\s+/)[0]) : f,
    sub: dirs[f] ? dirs[f].replace(/^\/(Users|home)\/[^/]+/, "~") : "Project", tag: "new chat", run: () => newChat(f) }));
  if (P.typing !== undefined) {
    rows = ALIASES.filter((a) => a.k.startsWith(P.typing)).map((a) => ({ key: "a" + a.k, group: "Search in", icon: a.icon, title: html`<span class="sp-alias">@${a.k}:</span>`, sub: a.d + " · e.g. @" + a.k + ":" + a.ex, keep: true, run: () => fill(a.k) }));
    hint = "Tab or ↵ picks the alias";
  } else if (P.alias === "open") {
    const ref = hubRef(P.words);
    rows = ref ? [{ key: "imp", group: "Hub link", icon: "import", title: "Import chat " + ref, sub: "A new chat that knows the shared conversation", run: () => importLink(ref) },
      ...(hub ? [{ key: "web", group: "Hub link", icon: "link", title: "Open " + ref + " on the hub", sub: hub.replace(/^https?:\/\//, ""), run: () => open(hub + "/#/s/" + ref, "_blank", "noopener") }] : [])] : [];
    hint = ref ? "↵ imports" : "Paste a hub link (…/#/s/author@name) or author@name";
  } else if (P.alias === "hub") {
    const ref = hubRef(P.words);
    rows = [...(ref ? [{ key: "imp", group: "Hub", icon: "import", title: "Import chat " + ref, sub: "Exact reference", run: () => importLink(ref) }] : []),
      ...(rem || []).filter((x) => x.ref !== ref || !x.chat).map((x) => ({ key: "h" + x.ref, group: P.words ? "Spaces on the hub" : "Chats shared on the hub", icon: x.chat ? "chat" : "layers", title: x.title || x.ref, sub: x.ref + (x.description ? " · " + x.description : ""), tag: x.chat ? "import" : x.kind,
        run: () => x.chat ? importLink(x.ref) : hub && open(hub + "/#/s/" + x.ref, "_blank", "noopener") }))];
    hint = remote.err && rem ? remote.err : remote.offline ? "Online search is off (Settings): cached spaces only" : busy && rem ? "Asking the hub…" : "Shared chats import; other spaces open on the hub";
  } else if (P.alias === "folder") {
    const linked = new Set(Object.values(dirs));                         // folders that already are a project are listed once, as the project
    rows = [...projRows(lc, 12, "Your projects"),
      ...(rem || []).filter((d) => !linked.has(d.path)).map((d) => ({ key: "d" + d.path, group: "Folders", icon: "folder", title: lc ? hilite(d.name, lc.split(/\s+/)[0]) : d.name, sub: d.short, tag: d.proj ? "project" : "", run: () => newChatIn(d.path) }))];
    hint = remote.indexing ? "Indexing your folders… (results grow)" : "↵ starts a new chat working in that folder";
  } else if (P.alias === "workflow") {
    rows = ((local.works || {}).works || []).filter((w) => fit(lc, w.title, w.description || "")).slice(0, 30).map((w) => ({ key: "w" + w.id, group: "Workflows", icon: "flow", title: lc ? hilite(w.title, lc.split(/\s+/)[0]) : w.title, sub: w.description || (w.enabled ? "Enabled" : "Workflow"), run: () => go("works/" + w.id) }));
  } else if (P.alias === "knowledge") {
    rows = ((local.knowledge || {}).notes || []).filter((n) => fit(lc, n.name)).slice(0, 30).map((n) => ({ key: "k" + n.name, group: "Knowledge", icon: n.name === "soul" ? "sparkles" : "note", title: n.name, sub: Math.max(1, Math.round(n.size / 1024)) + " KB", run: () => go("knowledge/" + n.name) }));
  } else if (P.alias === "package") {
    const mine = ((local.packages || {}).packages || []).filter((p) => fit(lc, p.name, p.description || "")), have = new Set(mine.map((p) => p.name));
    rows = [...mine.slice(0, 12).map((p) => ({ key: "i" + p.name, group: "Installed", icon: "package", title: p.name, sub: p.description || p.version || "", tag: p.type, run: () => go("packages") })),
      ...(rem || []).filter((p) => !have.has(p.name)).slice(0, 20).map((p) => ({ key: "f" + p.name, group: "On the hub", icon: "download", title: p.name, sub: p.description || p.latest_version || "", tag: p.type, run: () => go("packages/find/" + encodeURIComponent(p.name)) }))];
    hint = remote.offline ? "Online search is off (Settings): local copy only" : "Answered from the local catalogue";
  } else {
    const pages = nav.filter(([k, , l]) => !lc || (k + " " + l).toLowerCase().includes(lc)).map(([k, ic, l]) => ({ key: "p" + k, group: "Pages", icon: ic, title: l, sub: "Page", run: () => go(k) }));
    const titles = t ? sessions.filter((c) => fit(lc, c.title)) : sessions.slice(0, 8), seen = new Set();
    const chat = (c, snip) => ({ key: "c" + c.id, group: t ? "Chats" : "Recent chats", icon: "chat", title: t ? hilite(c.title, t) : c.title, sub: snip ? hilite(c.snippet, t) : c.folder ? "Project " + c.folder : "Chat", run: () => go("chat/" + c.id) });
    const chats = [...(rem || []).map((c) => (seen.add(c.id), chat(c, !!c.snippet))), ...titles.filter((c) => !seen.has(c.id)).slice(0, 30).map((c) => chat(c, false))];
    const viaLink = hubRef(t) ? [{ key: "lnk", group: "Hub link", icon: "import", title: html`Import with <span class="sp-alias">@open:</span>`, sub: t, keep: true, run: () => setQ("@open:" + t) }] : [];
    rows = t ? [...viaLink, ...chats, ...projRows(lc, 4, "Projects"), ...pages.slice(0, 4)] : [...chats, ...pages];
    hint = t.length >= 2 ? "Titles and what was said" : "Type @ to search folders, the hub, workflows, knowledge or packages";
  }
  const loading = busy || (need && !local[need]);
  useEffect(() => setIdx(0), [t]);
  useEffect(() => { if (idx >= rows.length && rows.length) setIdx(rows.length - 1); }, [rows.length]);
  useEffect(() => { const e = box.current && box.current.querySelector(".sp-row.hi"); if (e && e.scrollIntoView) e.scrollIntoView({ block: "nearest" }); }, [idx]);
  const pick = (r) => { if (!r) return; r.run(); if (!r.keep) close(); };
  const key = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setIdx((i) => Math.min(rows.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setIdx((i) => Math.max(0, i - 1)); }
    else if (e.key === "Tab" && P.typing !== undefined && rows[idx]) { e.preventDefault(); pick(rows[idx]); }
    else if (e.key === "Backspace" && P.alias && !P.words && /^@\w+:$/.test(q)) { e.preventDefault(); setQ("@"); }       // one key drops the alias
    else if (e.key === "Enter") { e.preventDefault(); pick(rows[idx]); }
  };
  const empty = loading ? "Searching…" : P.alias === "open" ? "Paste a hub link or author@name after @open:" : P.typing !== undefined ? "No such alias. Try @hub: @open: @workflow: @knowledge: @package:" : "Nothing matches.";
  return html`<div class="spot" onPointerDown=${(e) => e.target === e.currentTarget && close()}><div class="spot-box" role="dialog" aria-modal="true" aria-label="Search">
    <div class="spot-in">${Ic("search", 20)}${P.alias && html`<span class="sp-pill">@${P.alias}</span>`}<input ref=${inp} autofocus placeholder="Search chats and pages, or type @" aria-label="Search" value=${P.alias ? q.replace(/^@\w+[:\s]/, "") : q} onInput=${(e) => setQ(P.alias ? "@" + P.alias + ":" + e.target.value : e.target.value)} onKeyDown=${key}/><kbd>esc</kbd></div>
    <div class="spot-bar" style=${loading ? "" : "visibility:hidden"}></div>
    <div class="spot-list" ref=${box} style=${loading && rows.length ? "opacity:.75;transition:opacity .2s" : "transition:opacity .2s"}>${rows.map((r, i) => html`${(i === 0 || rows[i - 1].group !== r.group) && html`<div class="sp-h">${r.group}</div>`}<button class=${"sp-row" + (i === idx ? " hi" : "")} key=${r.key} onMouseMove=${() => i !== idx && setIdx(i)} onClick=${() => pick(r)}><span class="oi">${Ic(r.icon, 16)}</span><span class="grow"><b>${r.title}</b><span class="d">${r.sub}</span></span>${r.tag && html`<span class="tg">${r.tag}</span>`}<span class="sp-go">${Ic("right", 14)}</span></button>`)}
      ${!rows.length && html`<p class="mute" style="padding:22px;text-align:center">${empty}</p>`}</div>
    <div class="spot-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span><span class="grow"></span><span>${hint}</span></div></div></div>`;
}
const SORTS = [["recent", "Recent activity"], ["created", "Newest first"], ["oldest", "Oldest first"], ["name", "Name A–Z"]];
const sortChats = (list, how) => { const l = [...list]; if (how === "created") l.sort((a, b) => (b.created || 0) - (a.created || 0)); else if (how === "oldest") l.sort((a, b) => (a.created || 0) - (b.created || 0)); else if (how === "name") l.sort((a, b) => String(a.title).localeCompare(String(b.title), undefined, { sensitivity: "base", numeric: true })); else l.sort((a, b) => (b.updated || 0) - (a.updated || 0)); return l; };
const NAV = [["packages", "package", "Packages"], ["services", "server", "Services"], ["mcp", "plug", "MCP servers"], ["works", "flow", "Works"], ["knowledge", "book", "Knowledge"], ["usage", "chart", "Usage"]];
function App() {
  const [sw, setSw] = useState(() => +pref("sidew", "272") || 272);
  const [boot, setBoot] = useState(null), [sess, setSess] = useState({ folders: [], sessions: [] }), [route, setRoute] = useState(location.hash.slice(2) || "chat"), [msg, setMsg] = useState(""), [err, setErr] = useState("");
  const [spotOpen, setSpotOpen] = useState(false), [sortBy, setSortBy] = useState(() => pref("chatsort", "recent")), [sortPop, setSortPop] = useState(false), [drawer, setDrawer] = useState(false);
  const [folded, setFolded] = useState(() => { try { return JSON.parse(pref("chatfold", "[]")) || []; } catch (e) { return []; } });
  const fold = (k) => setFolded((l) => { const n = l.includes(k) ? l.filter((x) => x !== k) : [...l, k]; setPref("chatfold", JSON.stringify(n)); return n; });
  const chooseSort = (k) => { setSortBy(k); setPref("chatsort", k); setSortPop(false); };
  useEffect(() => { const h = (e) => { if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") { e.preventDefault(); setSpotOpen(true); } }; addEventListener("keydown", h); return () => removeEventListener("keydown", h); }, []);      // <=860px: the sidebar is a drawer
  const [prefs, setPrefs] = useState({ policy: "off", autocompact: true, compact_pct: 80, window: 1000000 }), [slash, setSlash] = useState({ claude: [], packages: [] });
  setToaster(setMsg);
  const [brand, setBrand] = useState({});
  useEffect(() => { api("brand").then((b) => { setBrand(b || {}); applyBrand(b); }).catch(() => {}); }, []);
  const hubName = brand.name || "AI Hub", Logo = (n) => Ic("logo", n);
  useEffect(() => { const k = route.split("/")[0] || "chat", l = (NAV.find((n) => n[0] === k) || [])[2] || k.charAt(0).toUpperCase() + k.slice(1); document.title = hubName + " \u00b7 " + l; }, [route, hubName]);       // tab title = hub name + open page
  const reloadBoot = () => api("boot").then(setBoot).catch((e) => setErr(e.message));
  const [running, setRunning] = useState({});
  const reloadSess = () => api("sessions").then((d) => { setSess(d); setRunning((r) => { const idle = Object.keys(r).filter((k) => r[k] && d.sessions.some((x) => x.id === k && !x.busy)); if (!idle.length) return r; const n = { ...r }; idle.forEach((k) => delete n[k]); return n; }); }).catch(() => {});                                     // chats this page is streaming right now (shown before the next sessions poll)
  const markBusy = useCallback((sid, v) => setRunning((r) => (!!r[sid] === v ? r : { ...r, [sid]: v || undefined })), []);
  const alerts = useAlerts(api);
  const savePrefs = guard(async (p) => { const r = await put("prefs", p); setPrefs(r.prefs); return r.prefs; });
  useEffect(() => { OUI.warm(); reloadBoot(); reloadSess(); api("prefs").then((d) => setPrefs(d.prefs)).catch(() => {}); api("slash").then(setSlash).catch(() => {}); const h = () => setRoute(location.hash.slice(2) || "chat"); addEventListener("hashchange", h); return () => removeEventListener("hashchange", h); }, []);
  useInterval(reloadSess, sess.sessions.some((x) => x.busy || running[x.id]) ? 4000 : null);      // chats answering in the background
  const go = (r) => { setDrawer(false); location.hash = "#/" + r; };
  useEffect(() => { setDrawer(false); }, [route]);
  useEffect(() => { if (!drawer) return; return escPush(() => setDrawer(false)); }, [drawer]);
  const [imp, setImp] = useState(false), [impExtra, setImpExtra] = useState("");
  useEffect(() => { if (imp) OUI.languagePrompt().then(setImpExtra).catch(() => {}); }, [imp]);
  const importLink = guard(async (ref) => { toast("Importing " + ref + "…"); const extra = await OUI.languagePrompt().catch(() => ""); const r = await post("sessions/import", { link: ref, extra }); toast("Imported " + r.messages + " messages" + (r.files ? " and " + r.files + " files" : "")); await reloadSess(); go("chat/" + r.session.id); });
  const newChatIn = guard(async (cwd) => { const extra = await OUI.languagePrompt(); const r = await post("sessions", { cwd, extra }); await reloadSess(); go("chat/" + r.session.id); });     // a folder on disk: it becomes a project
  const newChat = guard(async (folder = "") => { const extra = await OUI.languagePrompt(); const r = await post("sessions", { folder: typeof folder === "string" ? folder : "", extra }); await reloadSess(); go("chat/" + r.session.id); });
  if (err) return html`<div class="empty"><div>${Ic("alert", 28)}<h1>AI Hub UI</h1><p>${err}</p><p class="mute">Open the address from <code>~/.aihub/ui/url.txt</code>.</p></div></div>`;
  if (!boot) return html`<div class="empty"><div>${Ic("logo", 30)}<div>Starting…</div></div></div>`;
  const [page, arg] = route.split("/");
  const groups = [["", sortChats(sess.sessions.filter((s) => !s.folder), sortBy)], ...sess.folders.map((f) => [f, sortChats(sess.sessions.filter((s) => s.folder === f), sortBy)])];
  const move = guard(async (s) => {
    const v = await ask({ title: "Move chat", text: s.title, submit: "Move", fields: [
      { name: "folder", label: "Project", type: "select", value: s.folder || "", options: [{ value: "", label: "No project" }, ...sess.folders.map((f) => ({ value: f, label: f })), { value: "\u0000new", label: "New project…" }] },
      { name: "name", label: "New project name", required: true, placeholder: "Project X", max: 60, when: (x) => x.folder === "\u0000new" }] });
    if (!v) return; const to = v.folder === "\u0000new" ? v.name.trim() : v.folder;
    if (to && !sess.folders.includes(to)) await post("folders", { folders: [...sess.folders, to] }); await post("sessions/update", { id: s.id, folder: to }); reloadSess();
  });
  const rename = guard(async (s) => { const v = await ask({ title: "Rename chat", fields: [{ name: "title", label: "Name", value: s.title, required: true, max: 120 }], submit: "Rename" }); if (!v) return; await post("sessions/update", { id: s.id, title: v.title.trim() }); reloadSess(); });
  const delChat = guard(async (s) => { const v = await ask({ title: "Delete this chat?", text: "\u201c" + s.title + "\u201d and the files in its working folder are removed. This cannot be undone.", submit: "Delete chat", danger: true }); if (!v) return; await post("sessions/delete", { id: s.id }); reloadSess(); if (arg === s.id) go("chat"); });
  const delProject = guard(async (f) => {
    const n = sess.sessions.filter((x) => x.folder === f).length;
    const v = await ask({ title: "Delete project \u201c" + f + "\u201d?", text: (n ? n + " chat" + (n === 1 ? "" : "s") + " in it move" + (n === 1 ? "s" : "") + " back to Chats and are kept. " : "") + "Files in its working folder are not touched.", submit: "Delete project", danger: true });
    if (!v) return; await post("folders", { folders: sess.folders.filter((x) => x !== f) }); reloadSess();
  });
  const newFolder = guard(async () => { const v = await ask({ title: "New project", text: "Projects group chats in the sidebar. Choosing a working folder for a chat makes a project of the same name.", fields: [{ name: "name", label: "Project name", required: true, placeholder: "Project X", max: 60 }], submit: "Create project" }); if (!v) return;
    const n = v.name.trim(); if (sess.folders.includes(n)) return toast("That project already exists"); await post("folders", { folders: [...sess.folders, n] }); reloadSess(); });
  const c = boot.context;
  return html`<div class="shell" style=${"--sw:" + sw + "px"}>${drawer && html`<div class="sdback" onClick=${() => setDrawer(false)}></div>`}<aside class=${"side" + (drawer ? " open" : "")}><${Grip} cls="sd-grip" value=${() => sw} onChange=${setSw} onDone=${(v) => setPref("sidew", String(Math.round(v)))} min=${200} max=${460} sign=${1} reset=${() => { setSw(272); setPref("sidew", "272"); }}/>
      <div class="brand"><span class="logo">${Logo(16)}</span>${hubName}</div>
      <div class="nav"><button class="srow" onClick=${() => { setDrawer(false); newChat(); }}>${Ic("plus", 17)}<span class="grow">New chat</span></button><button class="srow" onClick=${() => { setDrawer(false); setSpotOpen(true); }} aria-label="Search chats and pages">${Ic("search", 17)}<span class="grow">Search</span><kbd>${/Mac|iP/.test(navigator.platform) ? "⌘K" : "Ctrl K"}</kbd></button></div>
      <div class="nav" onClick=${() => setDrawer(false)}>${NAV.map(([k, ic, l]) => html`<a class=${page === k ? "on" : ""} href=${"#/" + k}>${Ic(ic, 17)}${l}</a>`)}</div>
      <div class="hist">${groups.map(([f, items]) => !!(f || items.length) && html`<h6 class="gh"><button class="gtoggle" aria-expanded=${!folded.includes(f)} title=${folded.includes(f) ? "Expand" : "Collapse"} onClick=${() => fold(f)}><span class=${"chev" + (folded.includes(f) ? " shut" : "")}>${Ic("down", 12)}</span><span class="gname" title=${(sess.folder_dirs || {})[f] ? "Working folder: " + sess.folder_dirs[f] : ""}>${f || "Chats"}</span>${(sess.folder_dirs || {})[f] ? Ic("folder", 12) : ""}<span class="cnt">${items.length}</span></button>
          <span class="hbtns">${!f && html`<button class="ibtn gnew" style="width:28px;height:28px" title="New chat" aria-label="New chat" onClick=${() => { setDrawer(false); newChat(); }}>${Ic("plus", 17)}</button><${Pop} open=${sortPop} setOpen=${setSortPop} up=${false} align="right" button=${html`<button class="ibtn" style="width:28px;height:28px" title=${"Sort: " + (SORTS.find((x) => x[0] === sortBy) || SORTS[0])[1]} aria-label="Sort chats" onClick=${() => setSortPop(!sortPop)}>${Ic("filter", 16)}</button>`}><h5>Sort by</h5>${SORTS.map(([k, l]) => html`<button class=${"opt" + (k === sortBy ? " on" : "")} onClick=${() => chooseSort(k)}><span class="grow"><b>${l}</b></span><span class="ck">${Ic("check", 15)}</span></button>`)}<//>`}
          ${f && html`<button class="ibtn" style="width:28px;height:28px" title="New chat in this project" aria-label=${"New chat in " + f} onClick=${() => newChat(f)}>${Ic("plus", 17)}</button><button class="ibtn" style="width:28px;height:28px" title="Delete project" aria-label=${"Delete project " + f} onClick=${() => delProject(f)}>${Ic("trash", 17)}</button>`}</span></h6>
        ${!folded.includes(f) && items.map((s) => html`<div class=${"item" + (arg === s.id ? " on" : "")}><button class="rowmain" onClick=${() => go("chat/" + s.id)} aria-current=${arg === s.id ? "page" : undefined}>${s.busy || running[s.id] ? html`<span class="runDot" role="img" title="Answering…" aria-label="Answering"></span>` : Ic("chat", 15)}<span class="t">${s.title}</span></button>
          <span class="acts"><button class="x" title="Rename" aria-label=${"Rename " + s.title} onClick=${() => rename(s)}>${Ic("edit", 18)}</button><button class="x" title="Move to project" aria-label=${"Move " + s.title + " to a project"} onClick=${() => move(s)}>${Ic("folder", 18)}</button><button class="x" title="Delete" aria-label=${"Delete " + s.title} onClick=${() => delChat(s)}>${Ic("trash", 18)}</button></span></div>`)}`)}
        <h6><span></span><button class="linkbtn" onClick=${newFolder}>${Ic("folder", 14)} New project</button></h6></div>
      <div class="acct item" role="button" tabindex="0" aria-label="Account and settings" onClick=${() => go(c.signed_in ? "settings" : "settings/login")} onKeyDown=${(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(c.signed_in ? "settings" : "settings/login"); } }}><div class="av">${(c.username || "?")[0].toUpperCase()}</div><div class="grow"><b>${c.signed_in ? c.username : "Sign in"}</b><div class="mute small">${c.hub_configured ? c.hub.replace(/^https?:\/\//, "") : "local only"}</div></div>${Ic("sliders", 16)}</div>
    </aside>${imp && html`<${ImportLazy} get=${api} post=${post} close=${() => setImp(false)} extra=${impExtra} done=${async (n) => { setImp(false); await reloadSess(); go("chat/" + n.id); }}/>`}${spotOpen && html`<${Spotlight} close=${() => setSpotOpen(false)} go=${go} sessions=${sortChats(sess.sessions, "recent")} nav=${NAV} hub=${boot.context.hub_configured ? boot.context.hub : ""} importLink=${importLink} projects=${sess.folders} dirs=${sess.folder_dirs || {}} newChat=${newChat} newChatIn=${newChatIn}/>`}
    <main class="main">${!(page === "chat" && arg) && html`<button class="ibtn burger fab" title="Chats and pages" aria-label="Chats and pages" onClick=${() => setDrawer(true)}>${Ic("menu", 18)}</button>`}<${AlertBar} alerts=${alerts}/>${page === "chat" && arg ? html`<${Chat} key=${arg} id=${arg} onBusy=${markBusy} onImport=${() => setImp(true)} onMenu=${() => setDrawer(true)} boot=${boot} refresh=${reloadSess} newChat=${() => newChat()} prefs=${prefs} savePrefs=${savePrefs} slash=${slash}/>`
      : page === "packages" ? html`<${Packages} key=${route} focus=${route.startsWith("packages/find/") ? decodeURIComponent(route.slice(14)) : ""}/>` : page === "services" ? html`<${Services}/>` : page === "mcp" ? html`<${Mcp}/>`
      : page === "works" ? html`<${Works} boot=${boot} openId=${arg}/>` : page === "knowledge" ? html`<${Knowledge} key=${route} focus=${arg}/>` : page === "usage" ? html`<${UsagePage} api=${api} prefs=${prefs} savePrefs=${savePrefs}/>` : page === "settings" || page === "vault" ? html`<${Settings} boot=${boot} reload=${reloadBoot} prefs=${prefs} savePrefs=${savePrefs} focus=${arg}/>`
      : html`<div class="hello"><div><span class="herologo">${Logo(44)}</span><h1>${hubName}</h1><p>Chat with Claude Code, manage packages, services, MCP servers and logins.</p><button class="btn acc" onClick=${() => newChat()}>${Ic("plus", 14)} New chat</button></div></div>`}</main>
    ${html`<div class="toast-live" role="status" aria-live="polite">${msg && html`<div class="toast">${msg}</div>`}</div>`}<${AskHost}/></div>`;
}
render(html`<${App}/>`, document.getElementById("app"));
