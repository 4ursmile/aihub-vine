// Install terminal: xterm.js (jsDelivr) on top of /api/term/*. Only an install of the named packages runs here, it is not a shell.
let lib;
export function loadXterm() {
  if (!lib) {
    const l = document.createElement("link"); l.rel = "stylesheet"; l.href = "https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css"; document.head.appendChild(l);
    lib = Promise.all([import("https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/+esm"), import("https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/+esm")]);
  }
  return lib;
}

const b64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

// Runs the install in a terminal inside `host`. Calls onExit(code, services). Returns {stop}.
export async function runInstall(host, body, { post, api }, onExit) {
  const [{ Terminal }, { FitAddon }] = await loadXterm();
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const t = new Terminal({ fontFamily: "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace", fontSize: 12.5, cursorBlink: true, convertEol: false, scrollback: 5000,
    theme: dark ? { background: "#1a1917", foreground: "#e8e4dc" } : { background: "#26241f", foreground: "#ece8df" } });
  const fit = new FitAddon(); t.loadAddon(fit); t.open(host); try { fit.fit(); } catch (e) {}
  const r = await post("term/start", body), id = r.id; let pos = 0, dead = false, code = null;
  const send = (data) => post("term/write", { id, data }).catch(() => {});
  t.onData(send);
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
  return { stop: () => { dead = true; ro.disconnect(); post("term/write", { id, kill: true }).catch(() => {}); t.dispose(); }, done: () => { dead = true; } };
}
