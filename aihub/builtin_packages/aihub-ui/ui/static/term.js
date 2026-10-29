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

// Runs the install in a terminal inside `host`. Calls onExit(code, services). Returns {stop}.
export async function runInstall(host, body, { post, api }, onExit) {
  // jsDelivr can be blocked or offline: never wait for it forever, fall back to a plain text view with a one-line input.
  let libs = null;
  try { libs = await Promise.race([loadXterm(), new Promise((_, no) => setTimeout(() => no(new Error("timeout")), 8000))]); } catch (e) { lib = null; }
  if (!libs) return plainRun(host, body, { post, api }, onExit);
  const [{ Terminal }, { FitAddon }] = libs;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const t = new Terminal({ fontFamily: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace", fontSize: 12.5, cursorBlink: true, convertEol: false, scrollback: 5000,
    theme: dark ? { background: "#1a1917", foreground: "#e8e4dc" } : { background: "#26241f", foreground: "#ece8df" } });
  const fit = new FitAddon(); t.loadAddon(fit); t.open(host); try { fit.fit(); } catch (e) {}
  const r = await post("term/start", body), id = r.id; let pos = 0, dead = false, code = null;
  // Input goes out ONE request at a time, in order (parallel posts could reorder a paste), in chunks the server accepts whole.
  let q = Promise.resolve();
  const send = (data) => {
    for (let i = 0; i < data.length; i += 4000) { const part = data.slice(i, i + 4000); q = q.then(() => post("term/write", { id, data: part })).catch(() => {}); }
  };
  t.onData(send);
  // Paste: the native paste event is the normal route (keeps bracketed paste); where a browser/webview never fires it (old embedded
  // shells), Ctrl+V / Ctrl+Shift+V / Shift+Insert / right click / the Paste button read the clipboard API instead.
  let pasted = false;
  const pasteText = (text) => { if (text) { pasted = true; t.paste(String(text).replace(/\r?\n/g, "\r")); } t.focus(); };
  const readPaste = async () => { try { pasteText(await navigator.clipboard.readText()); } catch (e) { note("Allow clipboard access, or use the browser menu: Edit > Paste"); } };
  host.addEventListener("paste", () => { pasted = true; }, true);
  const viaApi = () => { pasted = false; setTimeout(() => { if (!pasted) readPaste(); }, 120); };
  const copySel = () => { const s = t.getSelection(); if (!s) return false; try { navigator.clipboard.writeText(s); } catch (e) { document.execCommand && document.execCommand("copy"); } return true; };
  t.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    const k = e.key.toLowerCase(), mod = e.ctrlKey || e.metaKey;
    if (mod && k === "v") { viaApi(); return true; }                                   // native paste first, API as a fallback
    if (e.shiftKey && k === "insert") { viaApi(); return true; }
    if (mod && k === "c" && (e.shiftKey || t.hasSelection())) { copySel(); e.preventDefault(); return false; }   // Ctrl+C with a selection copies, else it is Ctrl+C
    if (mod && k === "a" && e.shiftKey) { t.selectAll(); return false; }
    return true;
  });
  host.addEventListener("contextmenu", (e) => { e.preventDefault(); if (t.hasSelection()) { copySel(); t.clearSelection(); } else readPaste(); });
  // Small toolbar floating in the corner.
  const bar = document.createElement("div"); bar.className = "termtools";
  const msg = document.createElement("span"); let mt;
  function note(s) { msg.textContent = s; clearTimeout(mt); mt = setTimeout(() => { msg.textContent = ""; }, 4000); }
  const btn = (label, tip, fn) => { const b = document.createElement("button"); b.type = "button"; b.textContent = label; b.title = tip; b.onmousedown = (e) => e.preventDefault(); b.onclick = fn; bar.appendChild(b); };
  btn("Paste", "Paste from the clipboard (Ctrl+V)", readPaste);
  btn("Copy", "Copy the selection (Ctrl+Shift+C)", () => { if (copySel()) note("Copied"); else note("Select text first"); });
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
      } catch (e) { await new Promise((ok) => setTimeout(ok, 1000)); }
    }
    ro.disconnect(); if (!dead) onExit(code, r.services || []);
  })();
  return { stop: () => { dead = true; ro.disconnect(); post("term/write", { id, kill: true }).catch(() => {}); bar.remove(); t.dispose(); }, done: () => { dead = true; } };
}

// Fallback without xterm.js: output in a <pre>, input through a line box (answers to prompts, Enter sends).
async function plainRun(host, body, { post, api }, onExit) {
  const pre = document.createElement("pre"), row = document.createElement("form"), inp = document.createElement("input");
  pre.style.cssText = "margin:0;padding:10px;max-height:300px;overflow:auto;white-space:pre-wrap;background:#26241f;color:#ece8df;font:12.5px ui-monospace,Menlo,Consolas,monospace;border-radius:8px 8px 0 0";
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
