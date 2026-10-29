// OpenUI island: React + @openuidev components, loaded only when a chat produces an OpenUI block (the shell stays Preact).
// One vendored bundle (static/vendor/lazy/openui.mjs, built by ui/tools/vendor_libs.py), no CDN; if it cannot load, the caller shows the source instead.
const V = { react: "18.3.1", dom: "18.3.1", lang: "1.0.2", ui: "0.18.1" };
let pending;

export function load() {
  pending ||= (async () => {
    const LAZY = new URL("./vendor/lazy/", import.meta.url).href;
    const m = await import(LAZY + "openui.mjs");
    for (const css of ["openui/openui-defaults.css", "openui/components.css"]) { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = LAZY + css; document.head.appendChild(l); }
    return { React: m.React.default || m.React, client: m.client, lang: m.lang, lib: m.lib };
  })();
  pending.catch(() => (pending = null));
  return pending;
}

/** The OpenUI Lang rules, as text, for the system prompt of a new chat. '' when the library cannot load. */
// Cached per library version, and time-boxed: New chat must never wait on loading the 3 MB bundle.
const PKEY = "aihub_openui_prompt_" + V.ui;
const cached = () => { try { return localStorage.getItem(PKEY) || ""; } catch (e) { return ""; } };
let building;
function buildPrompt() {
  building ||= load().then(({ lang, lib }) => {
    const L = lib.openuiLibrary, o = lib.openuiPromptOptions;
    const t = "# OpenUI Lang (canvas components)\n" + (typeof L.prompt === "function" ? L.prompt(o) : lang.generatePrompt(L, o));
    try { localStorage.setItem(PKEY, t); } catch (e) {}
    return t;
  }).catch(() => { building = null; return ""; });
  return building;
}
export async function languagePrompt(wait = 4000) {
  const c = cached();
  if (c) { buildPrompt(); return c; }
  return Promise.race([buildPrompt(), new Promise((r) => setTimeout(() => r(""), wait))]);
}
/** Warm the cache when the browser is idle, so the first New chat already has the rules. */
export function warm() { if (!cached()) (window.requestIdleCallback || setTimeout)(() => buildPrompt(), { timeout: 5000 }); }

/** Render OpenUI Lang `source` into `el`. Returns a function that unmounts. */
export async function mount(el, source, streaming = false) {
  const { React, client, lang, lib } = await load();
  const root = client.createRoot(el);
  root.render(React.createElement(lang.Renderer, { response: source, library: lib.openuiLibrary, isStreaming: streaming }));
  return () => root.unmount();
}
