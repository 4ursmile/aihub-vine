// Install terminal: xterm.js (vendored in static/vendor/lazy) on top of /api/term/*. Only an install of the named packages runs here, it is not a shell.
let lib;
const LAZY = new URL("./vendor/lazy/", import.meta.url).href;      // vendored copies (ui/tools/vendor_libs.py); no CDN
const css = (local) => { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = LAZY + local; document.head.append(l); };
export function loadXterm() {
  if (!lib) {
    css("xterm.css");
    lib = Promise.all([import(LAZY + "xterm.mjs"), import(LAZY + "addon-fit.mjs")]);
  }
  return lib;
}

const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

// ---------- shared by every terminal (chat shells, installs, Jupyter server terminals): clipboard keys and the command assist
const HKEY = "aihub_ui_termhist_", LSG = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } };
const hist = {
  get: (kind) => { try { return JSON.parse(LSG(HKEY + kind, "[]")) || []; } catch (e) { return []; } },
  add: (kind, cmd) => { if (!cmd || cmd.length > 500 || /^\s/.test(cmd)) return; const l = [cmd, ...hist.get(kind).filter((x) => x !== cmd)].slice(0, 400); try { localStorage.setItem(HKEY + kind, JSON.stringify(l)); } catch (e) { /* full */ } },
};
// terminal colours that follow the page: dark on a dark page, light (with ANSI colours readable on white) on a light one
export const termTheme = () => matchMedia("(prefers-color-scheme: dark)").matches
  ? { background: "#1c1c1e", foreground: "#e5e5ea", cursor: "#e5e5ea", selectionBackground: "#3a3a3c" }
  : { background: "#ffffff", foreground: "#1d1d1f", cursor: "#1d1d1f", selectionBackground: "#d1d1d6", black: "#1d1d1f", red: "#c4271d", green: "#17803a", yellow: "#8a6100", blue: "#1f5fbf", magenta: "#8a3fa0", cyan: "#0f7480", white: "#6e6e73",
      brightBlack: "#636366", brightRed: "#d70015", brightGreen: "#248a3d", brightYellow: "#9a6b00", brightBlue: "#2a6fdb", brightMagenta: "#9b4bb3", brightCyan: "#127f8c", brightWhite: "#3a3a3c" };
export const termSuggestOn = () => LSG("aihub_ui_termsuggest", "1") === "1";
export const setTermSuggest = (on) => { try { localStorage.setItem("aihub_ui_termsuggest", on ? "1" : "0"); } catch (e) { /* private mode */ } };
const aiCache = new Map();
const PROMPT = /^.*?[$%#>❯➜»]\s(.*)$/;

// Wires TERM (xterm.js) in HOST: Ctrl/Cmd+C copies a selection (else it is Ctrl+C), Ctrl/Cmd+V and Shift+Insert paste, right click copies the
// selection or pastes. Assist: grey text after the cursor from this terminal's history, or from AI after a pause (→ accepts, Alt+→ one word,
// Esc hides); Ctrl+Space lists history and completer matches; a line starting with "# " is a request: AI proposes the command (→ puts it in).
// opts = { send(data), kind (history key), complete(line, pos) -> {start, matches}, suggest(prefix) -> text, note(msg) }. Returns { onData, dispose, copy, paste }.
export function wireTerm(t, host, { send, kind = "local", complete, suggest, note = () => {} }) {
  let pasted = false;
  const pasteText = (text) => { if (text) { pasted = true; t.paste(String(text).replace(/\r?\n/g, "\r")); } t.focus(); };
  const readPaste = async () => { try { pasteText(await navigator.clipboard.readText()); } catch (e) { note("Allow clipboard access, or use the browser menu: Edit > Paste"); } };
  const onPaste = () => { pasted = true; };
  host.addEventListener("paste", onPaste, true);
  const viaApi = () => { pasted = false; setTimeout(() => { if (!pasted) readPaste(); }, 120); };
  const copySel = () => { const s = t.getSelection(); if (!s) return false; try { navigator.clipboard.writeText(s).catch(() => { try { document.execCommand("copy"); } catch (e) { /* no clipboard */ } }); } catch (e) { /* no clipboard */ } return true; };
  const onCtx = (e) => { e.preventDefault(); if (t.hasSelection()) { copySel(); t.clearSelection(); note("Copied"); } else readPaste(); };
  host.addEventListener("contextmenu", onCtx);

  // ---- assist: the line being typed is read back from the screen (the shell echoes it), so history recall and Tab completion are seen too
  const layer = document.createElement("div"); layer.className = "tg-layer"; host.appendChild(layer);
  const gEl = document.createElement("span"); gEl.className = "tg-ghost"; layer.appendChild(gEl);
  const lEl = document.createElement("div"); lEl.className = "tg-list"; lEl.setAttribute("role", "listbox"); layer.appendChild(lEl);
  let typed = "", typedAt = 0, gh = null, list = null, upT = 0, aiT = 0, gen = 0, cT = 0, cgen = 0;
  const inputNow = () => {
    const b = t.buffer.active; if (b.viewportY !== b.baseY) return null;
    let y = b.baseY + b.cursorY, line = b.getLine(y); if (!line) return null;
    const full = line.translateToString(true); if (full.length > b.cursorX) return null;          // text after the cursor: not at the end of the line
    let left = full.slice(0, b.cursorX);
    while (line && line.isWrapped && y > 0) { y--; line = b.getLine(y); if (line) left = line.translateToString(false) + left; }
    if (typed && left.endsWith(typed)) return typed;
    const m = PROMPT.exec(left); return m ? m[1] : null;
  };
  const cell = () => { const scr = host.querySelector(".xterm-screen"); if (!scr) return null; const r = scr.getBoundingClientRect(), hr = host.getBoundingClientRect(); return { x: r.left - hr.left, y: r.top - hr.top, w: r.width / t.cols, h: r.height / t.rows }; };
  const place = (elx, col, row) => { const c = cell(); if (!c) return false; elx.style.left = c.x + col * c.w + "px"; elx.style.top = c.y + row * c.h + "px"; elx.style.font = (t.options.fontSize || 12.5) + "px/" + c.h + "px " + (t.options.fontFamily || "monospace"); elx.style.maxWidth = Math.max(0, (t.cols - col) * c.w) + "px"; return true; };
  const hideGhost = () => { gh = null; gEl.textContent = ""; gEl.style.display = "none"; };
  const hideList = () => { list = null; lEl.style.display = "none"; lEl.textContent = ""; };
  const showGhost = (text, input, replace) => {
    const b = t.buffer.active; if (!text || !place(gEl, b.cursorX, b.cursorY)) return hideGhost();
    gh = { text, input, replace }; gEl.textContent = replace ? "  → " + text : text; gEl.style.display = "block";
  };
  const askAi = (input, force) => {
    clearTimeout(aiT); if (!suggest || (!termSuggestOn() && !force)) return;
    const req = input.startsWith("# "), my = ++gen;
    aiT = setTimeout(async () => {
      const b = t.buffer.active, ctx = [];                                          // the last screen lines give the model the folder, the last command and its error
      for (let y = Math.max(0, b.baseY + b.cursorY - 14); y < b.baseY + b.cursorY; y++) { const l = b.getLine(y); if (l) ctx.push(l.translateToString(true)); }
      const prefix = "# terminal (" + kind.split(":")[0] + "), recent screen:\n" + ctx.filter((x) => x.trim()).map((x) => "# " + x.slice(0, 200)).join("\n") + "\n" + (req ? input + "\n" : input);
      const k = (req ? "r:" : "c:") + ctx.slice(-3).join("|") + "\u0001" + input;
      let s = aiCache.get(k);
      if (s == null) { try { s = String((await suggest(prefix)) || ""); } catch (e) { note("Suggestion failed: " + e.message); return; } s = (s.split("\n").find((x) => x.trim()) || "").replace(/^\$\s+/, "").replace(/\s+$/, ""); aiCache.set(k, s); if (aiCache.size > 100) aiCache.delete(aiCache.keys().next().value); }
      if (my !== gen || inputNow() !== input || !s) return;
      if (req) showGhost(s.trim(), input, true); else if (!s.startsWith(input) || s.length > input.length) showGhost(s.startsWith(input) ? s.slice(input.length) : s, input, false);
    }, force ? 0 : 900);
  };
  const update = () => {
    if (list) return;
    const input = inputNow();
    if (input == null || Date.now() - typedAt > 4000 || input.trim().length < 2) { hideGhost(); clearTimeout(aiT); return; }
    if (gh && !gh.replace && input.length > gh.input.length && (gh.input + gh.text).startsWith(input)) { showGhost((gh.input + gh.text).slice(input.length), input, false); return; }
    if (input.startsWith("# ")) { if (!gh || gh.input !== input) { hideGhost(); askAi(input); } return; }
    const h = hist.get(kind).find((x) => x.startsWith(input) && x !== input);
    if (h) { clearTimeout(aiT); clearTimeout(cT); showGhost(h.slice(input.length), input, false); return; }
    hideGhost();
    if (complete && /\S$/.test(input)) askComp(input); else askAi(input);
  };
  // no history match: the completer's first match (command, git subcommand, file or folder of the shell's folder) as grey text, else AI
  const askComp = (input) => {
    clearTimeout(cT); clearTimeout(aiT); const my = ++cgen;
    cT = setTimeout(async () => {
      let r = null; try { r = await complete(input, input.length); } catch (e) { /* no completer */ }
      if (my !== cgen || inputNow() !== input) return;
      const n = r ? input.length - r.start : 0, m = r && (r.matches || []).find((x) => x.t.length > n);
      if (m) showGhost(m.t.slice(n), input, false); else askAi(input);
    }, 90);
  };
  const later = () => { clearTimeout(upT); upT = setTimeout(update, 35); };
  const curMove = t.onCursorMove(later);
  const put = (text, input, replace) => { hideGhost(); hideList(); typedAt = Date.now(); if (replace) send("\x15" + text); else send(text); typed = replace ? text : input + text; t.focus(); };
  const openList = async () => {
    const input = inputNow(); if (input == null) return note("Put the cursor at the end of the command line");
    const word = (/[^\s|;&<>()'"`]*$/.exec(input) || [""])[0], items = [];
    for (const x of hist.get(kind)) if (x.startsWith(input) && x !== input && items.length < 6) items.push({ t: x, d: "history", rest: x.slice(input.length) });
    if (complete) { try { const r = await complete(input, input.length); for (const m of (r.matches || []).slice(0, 10)) if (!items.some((x) => x.rest === m.t.slice(word.length))) items.push({ t: m.t, d: m.k === "function" ? "command" : m.k === "folder" || m.k === "file" ? m.k : "", rest: m.t.slice(input.length - r.start) }); } catch (e) { /* no completer */ } }
    if (!items.length) return note(input ? "No match" : "Type the start of a command first");
    hideGhost(); list = { items, sel: 0, input }; drawList();
  };
  const drawList = () => {
    const b = t.buffer.active; lEl.textContent = "";
    list.items.forEach((it, i) => { const r = document.createElement("div"); r.className = "tg-opt" + (i === list.sel ? " on" : ""); r.setAttribute("role", "option"); const a = document.createElement("span"); a.textContent = it.t; r.append(a); if (it.d) { const d = document.createElement("small"); d.textContent = it.d; r.append(d); } r.onmousedown = (e) => { e.preventDefault(); put(it.rest, list.input, false); }; lEl.append(r); });
    place(lEl, Math.max(0, b.cursorX - list.input.length), b.cursorY + 1); lEl.style.display = "block"; lEl.style.font = ""; lEl.style.maxWidth = "";
    const c = cell(); if (c && b.cursorY > t.rows / 2) { lEl.style.top = ""; lEl.style.bottom = host.clientHeight - (c.y + b.cursorY * c.h) + "px"; } else lEl.style.bottom = "";
  };
  const key = (e) => {                                                       // false = swallowed, undefined = not ours
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    if (list) {
      if ((e.key === "ArrowDown" || e.key === "ArrowUp") && plain) { list.sel = (list.sel + (e.key === "ArrowDown" ? 1 : list.items.length - 1)) % list.items.length; drawList(); e.preventDefault(); return false; }
      if ((e.key === "Enter" || e.key === "Tab") && plain) { const it = list.items[list.sel]; put(it.rest, list.input, false); e.preventDefault(); return false; }
      if (e.key === "Escape") { hideList(); e.preventDefault(); return false; }
      hideList();
    }
    if (gh && e.key === "ArrowRight" && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
      e.preventDefault();
      if (gh.replace) { put(gh.text, gh.input, true); return false; }
      const w = e.altKey ? (/^\s*\S+/.exec(gh.text) || [gh.text])[0] : gh.text; put(w, gh.input, false); return false;
    }
    if (gh && e.key === "Escape") { hideGhost(); clearTimeout(aiT); return true; }
    if (e.ctrlKey && (e.key === " " || e.code === "Space")) { e.preventDefault(); openList(); return false; }
    if (e.altKey && (e.key === "\\" || e.code === "Backslash")) { const i = inputNow(); if (i != null) { typedAt = Date.now(); askAi(i, true); } e.preventDefault(); return false; }
    return undefined;
  };
  t.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    const r = key(e); if (r !== undefined) return r;
    const k = e.key.toLowerCase(), mod = e.ctrlKey || e.metaKey;
    if (mod && k === "v") { viaApi(); return true; }                                   // native paste first, API as a fallback
    if (e.shiftKey && k === "insert") { viaApi(); return true; }
    if (mod && k === "c" && (e.shiftKey || t.hasSelection() || e.metaKey)) { if (copySel()) note("Copied"); e.preventDefault(); return false; }   // Ctrl+C with a selection copies, else it is Ctrl+C
    if (mod && k === "a" && (e.shiftKey || e.metaKey)) { t.selectAll(); return false; }
    if (e.metaKey && k === "k") { t.clear(); return false; }
    return true;
  });
  const onData = (d) => {                                                    // what the user typed: keeps the line, the history and the timing
    typedAt = Date.now();
    if (d === "\r") { const c = (inputNow() ?? typed).trim(); if (c && !c.startsWith("# ")) hist.add(kind, c); typed = ""; hideGhost(); hideList(); clearTimeout(aiT); return; }
    if (d === "\x7f" || d === "\b") typed = typed.slice(0, -1);
    else if (d === "\x03" || d === "\x15") { typed = ""; hideGhost(); }
    else if (d.startsWith("\x1b") || d === "\t") typed = "";
    else if (!/[\x00-\x1f]/.test(d)) typed += d;
    later();
  };
  return { onData, copy: copySel, paste: readPaste, dispose: () => { clearTimeout(upT); clearTimeout(aiT); clearTimeout(cT); curMove.dispose(); layer.remove(); host.removeEventListener("paste", onPaste, true); host.removeEventListener("contextmenu", onCtx); } };
}

// Runs the install in a terminal inside `host`. Calls onExit(code, services). Returns {stop}.
export async function runInstall(host, body, { post, api }, onExit) {
  // jsDelivr can be blocked or offline: never wait for it forever, fall back to a plain text view with a one-line input.
  let libs = null;
  try { libs = await Promise.race([loadXterm(), new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 8000))]); } catch (e) { lib = null; }
  if (!libs) return plainRun(host, body, { post, api }, onExit);
  const [{ Terminal }, { FitAddon }] = libs;
  const t = new Terminal({ fontFamily: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace", fontSize: 12.5, cursorBlink: true, convertEol: false, scrollback: 5000, theme: termTheme() });
  const fit = new FitAddon(); t.loadAddon(fit); t.open(host); try { fit.fit(); } catch (e) {}
  const r = body.attach ? { id: body.attach, services: [] } : await post("term/start", body), id = r.id; let pos = 0, dead = false, code = null;
  // Input goes out ONE request at a time, in order (parallel posts could reorder a paste), in chunks the server accepts whole.
  let q = Promise.resolve();
  const send = (data) => {
    for (let i = 0; i < data.length; i += 4000) { const part = data.slice(i, i + 4000); q = q.then(() => post("term/write", { id, data: part })).catch(() => {}); }
  };
  // a shell gets the assist (history, completer for commands on this machine's PATH, AI); an install only the clipboard keys
  const W = wireTerm(t, host, { send, kind: body.shell ? "local" : "install", note: (m) => note(m),
    complete: body.shell ? (line, at) => post("code/complete", { code: line, pos: at, lang: "sh", term: id }) : null,
    suggest: body.shell ? (prefix) => post("jupyter/ai/suggest", { prefix, suffix: "", lang: "bash", cells: [] }).then((x) => x.text || "") : null });
  t.onData((d) => { W.onData(d); send(d); });
  // Small toolbar floating in the corner.
  const bar = document.createElement("div"); bar.className = "termtools";
  const msg = document.createElement("span"); let mt;
  function note(s) { msg.textContent = s; clearTimeout(mt); mt = setTimeout(() => { msg.textContent = ""; }, 4000); }
  const btn = (label, tip, fn) => { const b = document.createElement("button"); b.type = "button"; b.textContent = label; b.title = tip; b.onmousedown = (e) => e.preventDefault(); b.onclick = fn; bar.appendChild(b); };
  btn("Paste", "Paste from the clipboard (Ctrl+V)", W.paste);
  btn("Copy", "Copy the selection (Ctrl+Shift+C)", () => { if (W.copy()) note("Copied"); else note("Select text first"); });
  btn("Clear", "Clear the screen", () => { t.clear(); t.focus(); });
  bar.appendChild(msg); host.appendChild(bar);
  const size = () => { try { fit.fit(); post("term/write", { id, rows: t.rows, cols: t.cols }).catch(() => {}); } catch (e) {} };
  const ro = new ResizeObserver(size); ro.observe(host); size(); t.focus();
  (async () => {
    while (!dead) {
      try {
        const d = await api(`term/read?id=${id}&after=${pos}`);
        if (d.data) t.write(b64(d.data)); pos = d.pos;
        if (d.code !== null && d.code !== undefined) { code = d.code; break; }
      } catch (e) { if (e && e.status === 404) { code = -1; break; } await new Promise((ok) => setTimeout(ok, 1000)); }      // 404: the server no longer has it (restarted, or reaped)
    }
    ro.disconnect(); if (!dead) onExit(code, r.services || []);
  })();
  const close = () => { dead = true; ro.disconnect(); W.dispose(); bar.remove(); t.dispose(); };
  // stop = end the process; detach = leave it running on the server (its output stays buffered) and attach again later with {attach: id}
  return { id, stop: () => { close(); post("term/write", { id, kill: true }).catch(() => {}); }, detach: close, done: () => { dead = true; } };
}

// Fallback without xterm.js: output in a <pre>, input through a line box (answers to prompts, Enter sends).
async function plainRun(host, body, { post, api }, onExit) {
  const pre = document.createElement("pre"), row = document.createElement("form"), inp = document.createElement("input");
  pre.style.cssText = "margin:0;padding:10px;max-height:300px;overflow:auto;white-space:pre-wrap;background:#1c1c1e;color:#e5e5ea;font:12.5px ui-monospace,Menlo,Consolas,monospace;border-radius:8px 8px 0 0";
  row.style.cssText = "display:flex"; inp.placeholder = "Type an answer and press Enter"; inp.style.cssText = "flex:1;font:12.5px ui-monospace,Menlo,monospace;padding:6px 10px;border-radius:0 0 8px 8px";
  row.appendChild(inp); host.append(pre, row);
  const r = await post("term/start", body), id = r.id; let pos = 0, dead = false;
  row.onsubmit = (e) => { e.preventDefault(); post("term/write", { id, data: inp.value + "\r" }).catch(() => {}); inp.value = ""; };
  const dec = new TextDecoder();
  (async () => {
    let code = null;
    while (!dead) {
      try {
        const d = await api(`term/read?id=${id}&after=${pos}`);
        if (d.data) { pre.textContent += dec.decode(b64(d.data)).replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "").replace(/\r(?!\n)/g, ""); pre.scrollTop = pre.scrollHeight; }
        pos = d.pos;
        if (d.code !== null && d.code !== undefined) { code = d.code; break; }
      } catch (e) { await new Promise((ok) => setTimeout(ok, 1000)); }
    }
    if (!dead) onExit(code, r.services || []);
  })();
  return { stop: () => { dead = true; post("term/write", { id, kill: true }).catch(() => {}); pre.remove(); row.remove(); }, done: () => { dead = true; } };
}
